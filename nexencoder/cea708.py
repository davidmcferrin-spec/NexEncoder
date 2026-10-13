"""CEA-708 (DTVCC) service decoder producing plain text per service.

Models caption windows (DefineWindow, display/hide/toggle/clear/delete, pen location,
carriage-return scrolling). Styling (pen/window colours, fonts, justify, print
direction), delays and Asian-language P16 characters are parsed and skipped, not
rendered. Windows are not width-clipped, so text an encoder sends is never hidden.
Commands split across service blocks are reassembled from a small carry-over buffer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from .cdp import CcTriplet
from .dtvcc import DtvccAssembler, split_service_blocks
from .errors import ParseError

log = logging.getLogger(__name__)

_G2 = {
    0x20: " ", 0x21: " ", 0x25: "…", 0x2A: "Š", 0x2C: "Œ", 0x30: "█", 0x31: "‘",
    0x32: "’", 0x33: "“", 0x34: "”", 0x35: "•", 0x39: "™", 0x3A: "š", 0x3C: "œ",
    0x3D: "℠", 0x3F: "Ÿ", 0x76: "⅛", 0x77: "⅜", 0x78: "⅝", 0x79: "⅞", 0x7A: "│",
    0x7B: "┐", 0x7C: "└", 0x7D: "─", 0x7E: "┘", 0x7F: "┌",
}

_C1_PARAMS = {0x88: 1, 0x89: 1, 0x8A: 1, 0x8B: 1, 0x8C: 1, 0x8D: 1, 0x8E: 0, 0x8F: 0,
              0x90: 2, 0x91: 3, 0x92: 2, 0x97: 4}
_MAX_PENDING = 64


@dataclass(frozen=True)
class Caption708Event:
    service: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class _Window:
    def __init__(self, rows: int, cols: int, visible: bool, anchor_v: int, anchor_h: int):
        self.row_count, self.col_count = rows, cols
        self.visible, self.anchor_v, self.anchor_h = visible, anchor_v, anchor_h
        self.cells: list[list[str]] = [[] for _ in range(rows)]
        self.pen_row = 0
        self.pen_col = 0

    def clear(self) -> None:
        self.cells = [[] for _ in range(self.row_count)]
        self.pen_row = self.pen_col = 0

    def put(self, ch: str) -> None:
        while self.pen_row >= len(self.cells):
            self.cells.append([])
        row = self.cells[self.pen_row]
        while len(row) <= self.pen_col:
            row.append(" ")
        row[self.pen_col] = ch
        self.pen_col += 1

    def backspace(self) -> None:
        if self.pen_col > 0:
            self.pen_col -= 1
            if self.pen_row < len(self.cells) and self.pen_col < len(self.cells[self.pen_row]):
                self.cells[self.pen_row][self.pen_col] = " "

    def carriage_return(self) -> None:
        self.pen_col = 0
        if self.pen_row + 1 < self.row_count:
            self.pen_row += 1
            return
        while len(self.cells) < self.row_count:
            self.cells.append([])
        self.cells.pop(0)
        self.cells.append([])
        self.pen_row = self.row_count - 1

    def horizontal_cr(self) -> None:
        if self.pen_row < len(self.cells):
            self.cells[self.pen_row] = []
        self.pen_col = 0

    def lines(self) -> list[str]:
        out = []
        for row in self.cells:
            s = "".join(row).strip()
            if s:
                out.append(s)
        return out


class Service708:
    """Decoder state for one caption service (1-63)."""

    def __init__(self, number: int) -> None:
        self.number = number
        self.windows: dict[int, _Window] = {}
        self.current: Optional[int] = None
        self._pending = b""
        self._last: tuple[str, ...] = ()

    def snapshot(self) -> tuple[str, ...]:
        ordered = sorted(
            (w for w in self.windows.items() if w[1].visible),
            key=lambda kv: (kv[1].anchor_v, kv[1].anchor_h, kv[0]),
        )
        out: list[str] = []
        for _, w in ordered:
            out.extend(w.lines())
        return tuple(out)

    def feed(self, block: bytes) -> Optional[Caption708Event]:
        data = self._pending + block
        pos = 0
        while pos < len(data):
            nxt = self._step(data, pos)
            if nxt < 0:
                break
            pos = nxt
        self._pending = data[pos:]
        if len(self._pending) > _MAX_PENDING:
            log.warning("service %d: discarding oversized incomplete command", self.number)
            self._pending = b""
        lines = self.snapshot()
        if lines != self._last:
            self._last = lines
            return Caption708Event(self.number, lines)
        return None

    def _put(self, ch: str) -> None:
        w = self.windows.get(self.current) if self.current is not None else None
        if w is not None:
            w.put(ch)

    def _step(self, d: bytes, pos: int) -> int:
        """Process one code at ``pos``. Returns the next position or -1 if incomplete."""
        b = d[pos]
        if 0x20 <= b <= 0x7F:
            self._put("♪" if b == 0x7F else chr(b))
            return pos + 1
        if b >= 0xA0:
            self._put(chr(b))
            return pos + 1
        if b <= 0x1F:
            return self._c0(d, pos, b)
        return self._c1(d, pos, b)

    def _c0(self, d: bytes, pos: int, b: int) -> int:
        w = self.windows.get(self.current) if self.current is not None else None
        if b == 0x10:
            return self._ext(d, pos)
        if 0x11 <= b <= 0x17:
            return pos + 2 if pos + 2 <= len(d) else -1
        if 0x18 <= b <= 0x1F:
            return pos + 3 if pos + 3 <= len(d) else -1
        if w is not None:
            if b == 0x08:
                w.backspace()
            elif b == 0x0C:
                w.clear()
            elif b == 0x0D:
                w.carriage_return()
            elif b == 0x0E:
                w.horizontal_cr()
        return pos + 1

    def _ext(self, d: bytes, pos: int) -> int:
        if pos + 2 > len(d):
            return -1
        e = d[pos + 1]
        if 0x20 <= e <= 0x7F:
            self._put(_G2.get(e, "?"))
            return pos + 2
        if e <= 0x1F:
            skip = 0 if e < 0x08 else 1 if e < 0x10 else 2 if e < 0x18 else 3
        elif 0x80 <= e <= 0x9F:
            if e < 0x88:
                skip = 4
            elif e < 0x90:
                skip = 5
            else:
                if pos + 3 > len(d):
                    return -1
                skip = 1 + (d[pos + 2] & 0x3F)
        else:
            if e >= 0xA0:
                self._put("?")
            return pos + 2
        return pos + 2 + skip if pos + 2 + skip <= len(d) else -1

    def _c1(self, d: bytes, pos: int, b: int) -> int:
        n = 6 if 0x98 <= b <= 0x9F else _C1_PARAMS.get(b, 0)
        if pos + 1 + n > len(d):
            return -1
        p = d[pos + 1 : pos + 1 + n]
        if b <= 0x87:
            if (b - 0x80) in self.windows:
                self.current = b - 0x80
        elif 0x88 <= b <= 0x8C:
            self._bitmap_cmd(b, p[0])
        elif b == 0x8F:
            self.windows.clear()
            self.current = None
        elif b == 0x92:
            w = self.windows.get(self.current) if self.current is not None else None
            if w is not None:
                w.pen_row, w.pen_col = p[0] & 0x0F, p[1] & 0x3F
        elif b >= 0x98:
            self._define(b - 0x98, p)
        return pos + 1 + n

    def _bitmap_cmd(self, cmd: int, bitmap: int) -> None:
        for wid in range(8):
            if not bitmap & (1 << wid) or wid not in self.windows:
                continue
            w = self.windows[wid]
            if cmd == 0x88:
                w.clear()
            elif cmd == 0x89:
                w.visible = True
            elif cmd == 0x8A:
                w.visible = False
            elif cmd == 0x8B:
                w.visible = not w.visible
            elif cmd == 0x8C:
                del self.windows[wid]
                if self.current == wid:
                    self.current = None

    def _define(self, wid: int, p: bytes) -> None:
        visible = bool(p[0] & 0x20)
        anchor_v, anchor_h = p[1] & 0x7F, p[2]
        rows, cols = (p[3] & 0x0F) + 1, (p[4] & 0x3F) + 1
        w = self.windows.get(wid)
        if w is not None and (w.row_count, w.col_count) == (rows, cols):
            w.visible, w.anchor_v, w.anchor_h = visible, anchor_v, anchor_h
        else:
            self.windows[wid] = _Window(rows, cols, visible, anchor_v, anchor_h)
        self.current = wid


class Cea708Decoder:
    """Assembles DTVCC packets from cc_data triplets and decodes every service seen."""

    def __init__(self) -> None:
        self._asm = DtvccAssembler()
        self._services: dict[int, Service708] = {}
        self.packet_errors = 0

    @property
    def assembler(self) -> DtvccAssembler:
        return self._asm

    def feed(self, triplets: Iterable[CcTriplet]) -> list[Caption708Event]:
        events: list[Caption708Event] = []
        for t in triplets:
            for packet in self._asm.feed(t):
                try:
                    blocks = list(split_service_blocks(packet))
                except ParseError as exc:
                    self.packet_errors += 1
                    log.warning("bad DTVCC packet: %s", exc)
                    continue
                for number, block in blocks:
                    svc = self._services.setdefault(number, Service708(number))
                    ev = svc.feed(block)
                    if ev is not None:
                        events.append(ev)
        return events
