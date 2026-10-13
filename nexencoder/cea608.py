"""CEA-608 (line 21) caption decoder.

Feed it the byte pairs from cc_type 0 (field 1) and cc_type 1 (field 2) triplets.
Decodes captions CC1-CC4 (pop-on, roll-up, paint-on). Text services (T1-T4) are
recognised and ignored. Styling (colour, italics, underline) is not rendered; the
output is plain text per caption row, which is what the monitor overlay needs.

Behaviour notes:
- Byte pairs failing odd parity are dropped and counted in ``parity_errors``.
- Null pairs are ignored and do not affect duplicate-control-code suppression.
- A control pair identical to the immediately preceding control pair on the same
  field is treated as the standard redundant transmission and ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

COLS = 32

_BASIC = {0x2A: "á", 0x5C: "é", 0x5E: "í", 0x5F: "ó", 0x60: "ú", 0x7B: "ç",
          0x7C: "÷", 0x7D: "Ñ", 0x7E: "ñ", 0x7F: "■"}
_SPECIAL = "®°½¿™¢£♪à èâêîôû"  # 0x30..0x3F (0x39 is a transparent space)
_EXT1 = "ÁÉÓÚÜü‘¡*'—©℠•“”ÀÂÇÈÊËëÎÏïÔÙùÛ«»"  # 0x12 0x20..0x3F
_EXT2 = "ÃãÍÌìÒòÕõ{}\\^_|~ÄäÖöß¥¤│ÅåØø┌┐└┘"  # 0x13 0x20..0x3F

_PAC_ROWS = {
    (0x11, False): 1, (0x11, True): 2, (0x12, False): 3, (0x12, True): 4,
    (0x15, False): 5, (0x15, True): 6, (0x16, False): 7, (0x16, True): 8,
    (0x17, False): 9, (0x17, True): 10, (0x10, False): 11, (0x13, False): 12,
    (0x13, True): 13, (0x14, False): 14, (0x14, True): 15,
}

_RCL, _BS, _DER, _RU2, _RU3, _RU4 = 0x20, 0x21, 0x24, 0x25, 0x26, 0x27
_RDC, _TR, _RTD, _EDM, _CR, _ENM, _EOC = 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F


@dataclass(frozen=True)
class Caption608Event:
    channel: int  # 1..4 (CC1..CC4)
    mode: str  # "popon" | "rollup" | "painton"
    lines: tuple[str, ...]  # visible rows, top to bottom; empty tuple = cleared

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _odd_parity(b: int) -> bool:
    return bin(b & 0xFF).count("1") % 2 == 1


class _Channel:
    def __init__(self) -> None:
        self.mode: Optional[str] = None
        self.roll = 2
        self.base_row = 15
        self.row = 15
        self.col = 0
        self.text_mode = False
        self.displayed: dict[int, list[str]] = {}
        self.hidden: dict[int, list[str]] = {}
        self.dirty = False

    def lines(self) -> tuple[str, ...]:
        out = []
        for row in sorted(self.displayed):
            s = "".join(self.displayed[row]).strip()
            if s:
                out.append(s)
        return tuple(out)

    def _target(self) -> dict[int, list[str]]:
        return self.hidden if self.mode == "popon" else self.displayed

    def write(self, ch: str) -> None:
        if self.mode is None or self.text_mode:
            return
        row = self.base_row if self.mode == "rollup" else self.row
        mem = self._target()
        cells = mem.setdefault(row, [" "] * COLS)
        if self.col < COLS:
            cells[self.col] = ch
            self.col += 1
        if mem is self.displayed:
            self.dirty = True

    def backspace(self) -> None:
        if self.col > 0:
            self.col -= 1
            row = self.base_row if self.mode == "rollup" else self.row
            mem = self._target()
            if row in mem:
                mem[row][self.col] = " "
                if mem is self.displayed:
                    self.dirty = True

    def delete_to_end(self) -> None:
        row = self.base_row if self.mode == "rollup" else self.row
        mem = self._target()
        if row in mem:
            for c in range(self.col, COLS):
                mem[row][c] = " "
            if mem is self.displayed:
                self.dirty = True

    def enter_rollup(self, rows: int) -> None:
        if self.mode != "rollup":
            self.displayed, self.hidden = {}, {}
            self.base_row = 15
        self.mode, self.roll, self.text_mode = "rollup", rows, False
        self.base_row = max(self.base_row, rows)
        self.col = 0
        self.dirty = True

    def carriage_return(self) -> None:
        if self.mode != "rollup":
            return
        top = self.base_row - self.roll + 1
        for r in range(top, self.base_row):
            nxt = self.displayed.get(r + 1)
            if nxt is None:
                self.displayed.pop(r, None)
            else:
                self.displayed[r] = nxt
        self.displayed.pop(self.base_row, None)
        for r in [k for k in self.displayed if k < top]:
            del self.displayed[r]
        self.col = 0
        self.dirty = True

    def set_pac(self, row: int, col: int) -> None:
        self.row, self.col = row, col
        if self.mode == "rollup":
            self.base_row = max(row, self.roll)


class Cea608Decoder:
    """Stateful decoder for both fields. Call ``feed`` once per byte pair."""

    def __init__(self) -> None:
        self._channels = {n: _Channel() for n in (1, 2, 3, 4)}
        self._current = {1: 1, 2: 3}
        self._last_ctrl: dict[int, Optional[tuple[int, int]]] = {1: None, 2: None}
        self.parity_errors = 0

    def feed(self, field: int, b1: int, b2: int) -> list[Caption608Event]:
        if field not in (1, 2):
            raise ValueError("field must be 1 or 2")
        if not (_odd_parity(b1) and _odd_parity(b2)):
            self.parity_errors += 1
            return []
        b1 &= 0x7F
        b2 &= 0x7F
        if b1 == 0 and b2 == 0:
            return []
        if 0x10 <= b1 <= 0x1F:
            if self._last_ctrl[field] == (b1, b2):
                return []
            self._last_ctrl[field] = (b1, b2)
            self._control(field, b1, b2)
        else:
            self._last_ctrl[field] = None
            ch = self._channels[self._current[field]]
            for b in (b1, b2):
                if b >= 0x20:
                    ch.write(_BASIC.get(b, chr(b)))
        ch = self._channels[self._current[field]]
        if ch.dirty:
            ch.dirty = False
            return [Caption608Event(self._current[field], ch.mode or "", ch.lines())]
        return []

    def _control(self, field: int, b1: int, b2: int) -> None:
        number = (field - 1) * 2 + (1 if b1 & 0x08 else 0) + 1
        self._current[field] = number
        ch = self._channels[number]
        base = b1 & 0xF7

        if b2 >= 0x40:
            row = _PAC_ROWS.get((base, bool(b2 & 0x20)))
            if row is not None:
                col = ((b2 & 0x0E) >> 1) * 4 if b2 & 0x10 else 0
                ch.set_pac(row, col)
        elif base in (0x14, 0x15) and 0x20 <= b2 <= 0x2F:
            self._misc(ch, b2)
        elif base == 0x11 and 0x20 <= b2 <= 0x2F:
            ch.write(" ")  # mid-row style code occupies one space
        elif base == 0x11 and 0x30 <= b2 <= 0x3F:
            ch.write(_SPECIAL[b2 - 0x30])
        elif base in (0x12, 0x13) and 0x20 <= b2 <= 0x3F:
            table = _EXT1 if base == 0x12 else _EXT2
            ch.backspace()
            ch.write(table[b2 - 0x20])
        elif base == 0x17 and 0x21 <= b2 <= 0x23:
            ch.col = min(COLS, ch.col + (b2 - 0x20))

    @staticmethod
    def _misc(ch: _Channel, code: int) -> None:
        if code == _RCL:
            ch.mode, ch.text_mode = "popon", False
        elif code == _RDC:
            ch.mode, ch.text_mode = "painton", False
        elif code in (_RU2, _RU3, _RU4):
            ch.enter_rollup(code - _RU2 + 2)
        elif code in (_TR, _RTD):
            ch.text_mode = True
        elif code == _ENM:
            ch.hidden = {}
        elif code == _EDM:
            ch.displayed = {}
            ch.dirty = True
        elif code == _EOC:
            ch.displayed, ch.hidden = ch.hidden, {}
            ch.mode, ch.text_mode = "popon", False
            ch.dirty = True
        elif code == _CR:
            ch.carriage_return()
        elif code == _BS:
            ch.backspace()
        elif code == _DER:
            ch.delete_to_end()
