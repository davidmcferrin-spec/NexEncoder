"""Turn DeckLink VANC datagrams into SCTE-35 sections and monitor notes.

FFmpeg, patched by ``decklink_patch``, owns the card and sends one UDP
datagram per ANC packet. SCTE-104 is translated and handed to spliceinject.
Caption text is posted to the console when it changes. Repeated copies of the
same SCTE-104 payload, which the SDI repeats for the whole pre-roll, are
inserted once.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from .errors import TranslateError
from .pipeline import SDI_SCTE_PORT, SPLICE_PORT, VANC_PORT
from .translate import translate_mom
from .vanc import events_from_vanc, scan_vanc

log = logging.getLogger("nexencoder.vanc")

KIND_SCTE = 1
KIND_CDP = 2
HEADER_LEN = 12
REPEAT_SECONDS = 30.0
CONSOLE_URL = "http://127.0.0.1:8788/api/sideband"


def pack_vanc_datagram(kind: int, pts: int, payload: bytes) -> bytes:
    if kind not in (KIND_SCTE, KIND_CDP):
        raise ValueError(f"unknown VANC kind {kind}")
    if pts < 0 or pts > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("PTS does not fit in uint64")
    return bytes([kind, 0, 0, 0]) + pts.to_bytes(8, "big") + payload


def parse_vanc_datagram(packet: bytes) -> tuple[int, int, bytes]:
    if len(packet) < HEADER_LEN:
        raise ValueError("VANC datagram is shorter than its header")
    kind = packet[0]
    if kind not in (KIND_SCTE, KIND_CDP):
        raise ValueError(f"unknown VANC kind {kind}")
    if packet[1:4] != b"\x00\x00\x00":
        raise ValueError("VANC datagram header is not aligned")
    pts = int.from_bytes(packet[4:12], "big")
    return kind, pts, packet[12:]


def post_sideband(event: dict, url: str = CONSOLE_URL, timeout: float = 0.5) -> None:
    body = json.dumps(event).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Requested-With": "NexEncoder"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        response.read()


class VancForwarder:
    """Decode datagrams. Network I/O stays behind the two callbacks."""

    def __init__(
        self,
        channel_id: str,
        send_section: Callable[[bytes, int], None],
        post: Callable[[dict], None] = post_sideband,
        repeat_seconds: float = REPEAT_SECONDS,
        sdi_send: Callable[[bytes, int], None] | None = None,
    ) -> None:
        if channel_id not in SPLICE_PORT:
            raise ValueError("channel must be A or B")
        self.channel_id = channel_id
        self.splice_port = SPLICE_PORT[channel_id]
        self._send_section = send_section
        self._post = post
        self._sdi = sdi_send
        self._repeat = repeat_seconds
        self._sent: dict[bytes, float] = {}
        self._caption: tuple[tuple[str, str], ...] = ()

    def handle(self, packet: bytes, now: float, splice_ready: bool) -> None:
        try:
            kind, pts, payload = parse_vanc_datagram(packet)
        except ValueError:
            log.debug("dropped a VANC datagram", exc_info=True)
            return
        if kind == KIND_SCTE:
            self._splice(payload, pts, now, splice_ready)
        else:
            self._caption_note(payload)

    def _splice(self, payload: bytes, pts: int, now: float, splice_ready: bool) -> None:
        self._sent = {key: seen for key, seen in self._sent.items() if now - seen < self._repeat}
        if payload in self._sent:
            return
        if not splice_ready:
            return
        found = scan_vanc(payload)
        if not found["splices"]:
            log.debug("SCTE-104 datagram had no multiple_operation_message")
            self._sent[payload] = now
            return
        self._sent[payload] = now
        if self._sdi is not None:
            try:
                self._sdi(payload, pts)
            except OSError:
                log.warning("channel %s could not forward SCTE-104 to SDI", self.channel_id, exc_info=True)
        for mom in found["splices"]:
            try:
                sections = translate_mom(mom, pts)
            except TranslateError as exc:
                log.warning("SCTE-104 was not translated: %s", exc)
                continue
            for section in sections:
                self._send_section(section, self.splice_port)
            if sections:
                log.info("channel %s inserted %d SCTE-35 section(s) at pts %d", self.channel_id, len(sections), pts)
        for event in events_from_vanc(self.channel_id, payload):
            if event["kind"] != "scte104":
                continue
            self._notify(event)

    def _caption_note(self, payload: bytes) -> None:
        events = [event for event in events_from_vanc(self.channel_id, payload) if event["kind"] == "caption"]
        key = tuple((event["label"], event["text"]) for event in events)
        if not key or key == self._caption:
            return
        self._caption = key
        for event in events:
            self._notify(event)

    def _notify(self, event: dict) -> None:
        try:
            self._post(event)
        except (OSError, URLError, TimeoutError, ValueError):
            log.warning("sideband post failed", exc_info=True)


def serve_vanc(
    channel_id: str,
    stop: threading.Event,
    splice_ready: Callable[[], bool],
    send_section: Callable[[bytes, int], None] | None = None,
    post: Callable[[dict], None] = post_sideband,
) -> None:
    """Bind the channel's VANC port until ``stop`` is set."""
    port = VANC_PORT[channel_id]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        log.warning("channel %s could not bind the VANC port %s", channel_id, port, exc_info=True)
        sock.close()
        return
    sock.settimeout(0.5)
    forwarder = VancForwarder(
        channel_id, send_section or _udp_section, post,
        sdi_send=lambda payload, pts: send_sdi_scte(channel_id, payload, pts),
    )
    log.info("channel %s listening for VANC on 127.0.0.1:%s", channel_id, port)
    try:
        while not stop.is_set():
            try:
                packet, _addr = sock.recvfrom(8192)
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                forwarder.handle(packet, time.monotonic(), splice_ready())
            except Exception:
                log.warning("VANC datagram failed", exc_info=True)
    finally:
        sock.close()


def send_sdi_scte(channel_id: str, payload: bytes, pts: int) -> None:
    """Hand one SCTE-104 message to the SDI playback muxer."""
    port = SDI_SCTE_PORT[channel_id]
    packet = pack_vanc_datagram(KIND_SCTE, pts, payload)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(packet, ("127.0.0.1", port))
    finally:
        sock.close()


def _udp_section(section: bytes, port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(section, ("127.0.0.1", port))
    finally:
        sock.close()


def start_vanc_thread(
    channel_id: str,
    stop: threading.Event,
    splice_ready: Callable[[], bool],
) -> threading.Thread:
    thread = threading.Thread(
        target=serve_vanc,
        args=(channel_id, stop, splice_ready),
        name=f"nexencoder-vanc-{channel_id}",
        daemon=True,
    )
    thread.start()
    return thread
