"""Vendor captions for one channel, applied at the encode.

A datagram on the channel's caption port becomes an NXCC file. The
``nexcaption`` filter reads that file and attaches A/53 side data before
libx264. An idle file leaves the VANC side data on the frame. The filter
is the only insert; this module never sits on the ``ffmpeg | tsp`` pipe.

Datagrams, first byte then the payload:

0. Clear. Return the channel to VANC.
1. One line of caption text, UTF-8, at most 32 printable characters.
   Encoded as CEA-608 roll-up (CC1) and attached on the next frame, then
   padded until the hold expires.
2. One CDP. Its cc_data triplets are attached on every frame until the hold
   expires, so a 708 service stays in the bitstream.
3. Raw cc_data triplets, attached on every frame the same way.

Packets are applied only while the channel caption source is ``vendor``.
The default is ``vanc``.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

from .cdp import parse_cdp
from .effects import write_text_atomic
from .errors import ParseError
from .pipeline import CAPTION_PORT

log = logging.getLogger("nexencoder.caption")

MAGIC = b"NXCC"
VERSION = 1
MODE_IDLE = 0
MODE_EVERY = 1
MODE_ONCE = 2
HEADER_LEN = 12
MAX_TRIPLETS = 31
HOLD_SECONDS = 8.0
KIND_CLEAR = 0
KIND_TEXT = 1
KIND_CDP = 2
KIND_CC = 3
SOURCES = ("vanc", "vendor")
_ROW15_PAC = (0x14, 0x60)


def caption_path(state_dir: Path, channel_id: str) -> Path:
    return Path(state_dir) / f"{channel_id}.cc"


def caption_source_path(state_dir: Path, channel_id: str) -> Path:
    return Path(state_dir) / f"{channel_id}.caption-source"


def read_caption_source(state_dir: Path, channel_id: str) -> str:
    try:
        text = caption_source_path(state_dir, channel_id).read_text(encoding="utf-8").strip()
    except OSError:
        return "vanc"
    if text not in SOURCES:
        return "vanc"
    return text


def write_caption_source(state_dir: Path, channel_id: str, source: str) -> str:
    """Select ``vanc`` or ``vendor``. ``vanc`` also idles the caption file."""
    if channel_id not in CAPTION_PORT:
        raise ValueError("channel must be A or B")
    if source not in SOURCES:
        raise ValueError("caption source must be vanc or vendor")
    write_text_atomic(caption_source_path(state_dir, channel_id), source + "\n")
    if source == "vanc":
        write_caption_file(caption_path(state_dir, channel_id), MODE_IDLE, 0, b"")
    return source


def pack_caption(mode: int, seq: int, data: bytes) -> bytes:
    """NXCC file the ``nexcaption`` filter reads. ``data`` is cc_data triplets."""
    if mode not in (MODE_IDLE, MODE_EVERY, MODE_ONCE):
        raise ValueError("caption mode is not idle, every, or once")
    if seq < 0 or seq > 0xFFFFFFFF:
        raise ValueError("caption sequence does not fit in uint32")
    if len(data) % 3 != 0 or len(data) > MAX_TRIPLETS * 3:
        raise ValueError("caption data must be at most 31 cc_data triplets")
    return (
        MAGIC
        + bytes([VERSION, mode])
        + int(seq).to_bytes(4, "big")
        + len(data).to_bytes(2, "big")
        + data
    )


def write_caption_file(path: Path, mode: int, seq: int, data: bytes) -> None:
    body = pack_caption(mode, seq, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(path)


def parse_caption_file(data: bytes) -> tuple[int, int, bytes]:
    """Return mode, sequence, and cc_data. Raises ValueError on a bad file."""
    if len(data) < HEADER_LEN or data[:4] != MAGIC or data[4] != VERSION:
        raise ValueError("caption file header is not NXCC")
    mode = data[5]
    if mode not in (MODE_IDLE, MODE_EVERY, MODE_ONCE):
        raise ValueError("caption file mode is unknown")
    seq = int.from_bytes(data[6:10], "big")
    size = int.from_bytes(data[10:12], "big")
    if size % 3 != 0 or size > MAX_TRIPLETS * 3 or len(data) != HEADER_LEN + size:
        raise ValueError("caption file length does not match its triplets")
    return mode, seq, data[HEADER_LEN:]


def _parity(value: int) -> int:
    seven = value & 0x7F
    return seven | (0x00 if bin(seven).count("1") % 2 else 0x80)


def _triplet(first: int, second: int) -> bytes:
    return bytes([0xFC, _parity(first), _parity(second)])


def sanitize_caption_line(text: str) -> str:
    """Printable ASCII, trimmed, at most 32 characters."""
    raw = text.replace("\r", "\n").split("\n", 1)[0]
    kept = []
    for char in raw:
        code = ord(char)
        if 0x20 <= code <= 0x7E:
            kept.append(char)
        elif char == "\t":
            kept.append(" ")
        else:
            break
        if len(kept) >= 32:
            break
    return "".join(kept).strip()


def cc_from_text(text: str, *, rolling: bool) -> tuple[bytes, bool]:
    """CEA-608 roll-up 2 on row 15, as A/53 field-1 triplets."""
    line = sanitize_caption_line(text)
    if not line:
        raise ValueError("caption text is empty")
    parts = [_triplet(0x14, 0x25)]
    if rolling:
        parts.append(_triplet(0x14, 0x2D))
    parts.append(_triplet(*_ROW15_PAC))
    raw = line.encode("ascii")
    if len(raw) % 2:
        raw += b" "
    for index in range(0, len(raw), 2):
        parts.append(_triplet(raw[index], raw[index + 1]))
    return b"".join(parts), True


def cc_from_cdp(payload: bytes) -> bytes:
    parsed = parse_cdp(payload)
    if not parsed.triplets:
        raise ValueError("CDP has no cc_data")
    if len(parsed.triplets) > MAX_TRIPLETS:
        raise ValueError("CDP has more than 31 cc_data triplets")
    out = bytearray()
    for item in parsed.triplets:
        marker = 0xF8 | (0x04 if item.valid else 0) | (item.cc_type & 0x03)
        out += bytes([marker, item.data1 & 0xFF, item.data2 & 0xFF])
    return bytes(out)


class CaptionIngest:
    """Turn datagrams into the caption file. The socket stays outside this class."""

    def __init__(self, channel_id: str, state_dir: Path, hold_seconds: float = HOLD_SECONDS) -> None:
        if channel_id not in CAPTION_PORT:
            raise ValueError("channel must be A or B")
        self.channel_id = channel_id
        self.state_dir = Path(state_dir)
        self.path = caption_path(self.state_dir, channel_id)
        self.hold = hold_seconds
        self.seq = 0
        self.rolling = False
        self.last_text = ""
        self.armed_at = 0.0
        self.active = False

    def handle(self, packet: bytes, now: float) -> str:
        if not packet:
            return "rejected"
        kind = packet[0]
        payload = packet[1:]
        if kind == KIND_CLEAR:
            self._idle()
            return "cleared"
        if read_caption_source(self.state_dir, self.channel_id) != "vendor":
            return "ignored"
        try:
            if kind == KIND_TEXT:
                return self._text(payload, now)
            if kind == KIND_CDP:
                data = cc_from_cdp(payload)
                mode = MODE_EVERY
            elif kind == KIND_CC:
                if not payload or len(payload) % 3 != 0:
                    return "rejected"
                data = payload
                mode = MODE_EVERY
            else:
                return "rejected"
        except (ParseError, ValueError):
            log.debug("channel %s dropped a caption datagram", self.channel_id, exc_info=True)
            return "rejected"
        self._arm(mode, data, now)
        return "applied"

    def expire(self, now: float) -> bool:
        if self.active and now - self.armed_at >= self.hold:
            self._idle()
            return True
        return False

    def _text(self, payload: bytes, now: float) -> str:
        try:
            line = sanitize_caption_line(payload.decode("utf-8"))
        except UnicodeError:
            return "rejected"
        if not line:
            return "rejected"
        if line == self.last_text and self.active:
            self.armed_at = now
            return "applied"
        data, self.rolling = cc_from_text(line, rolling=self.rolling)
        self.last_text = line
        self._arm(MODE_ONCE, data, now)
        return "applied"

    def _arm(self, mode: int, data: bytes, now: float) -> None:
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        write_caption_file(self.path, mode, self.seq, data)
        self.armed_at = now
        self.active = True

    def _idle(self) -> None:
        write_caption_file(self.path, MODE_IDLE, self.seq, b"")
        self.active = False
        self.rolling = False
        self.last_text = ""


def serve_captions(channel_id: str, state_dir: Path, stop: threading.Event) -> None:
    """Bind the channel caption port until ``stop`` is set."""
    port = CAPTION_PORT[channel_id]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        log.warning("channel %s could not bind the caption port %s", channel_id, port, exc_info=True)
        sock.close()
        return
    sock.settimeout(0.5)
    ingest = CaptionIngest(channel_id, state_dir)
    log.info("channel %s listening for vendor captions on 127.0.0.1:%s", channel_id, port)
    try:
        while not stop.is_set():
            try:
                packet, _addr = sock.recvfrom(4096)
            except TimeoutError:
                ingest.expire(time.monotonic())
                continue
            except OSError:
                break
            try:
                result = ingest.handle(packet, time.monotonic())
            except Exception:
                log.warning("channel %s caption datagram failed", channel_id, exc_info=True)
                continue
            if result == "applied":
                log.info("channel %s applied a vendor caption", channel_id)
            elif result == "cleared":
                log.info("channel %s vendor captions cleared", channel_id)
    finally:
        sock.close()


def start_caption_thread(channel_id: str, state_dir: Path, stop: threading.Event) -> threading.Thread:
    thread = threading.Thread(
        target=serve_captions,
        args=(channel_id, state_dir, stop),
        name=f"nexencoder-caption-{channel_id}",
        daemon=True,
    )
    thread.start()
    return thread
