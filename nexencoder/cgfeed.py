"""CG feeders for the four layer slots.

HTML is a live page: WPE when that plugin is installed, otherwise Chromium
capturing PNG with the background omitted so CSS alpha survives. NDI is a
GStreamer ndisrc into the same BGRA slot. Fill/key is one shared DeckLink pair.
A missing renderer leaves the slot file unwritten, which the filter treats
as a transparent layer. The primary picture, captions, and SCTE path stay up.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlparse

from .effects import SDI_FILL, SDI_KEY, cg_dir, layers_path

log = logging.getLogger("nexencoder.cg")

HEADER = 32
MAGIC = b"NXCG"
FLAG_PREMULTIPLIED = 1
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_NDI_READY: bool | None = None
_NDI_MISSING_LOGGED = False


def pack_header(seq: int, width: int, height: int, nbytes: int, flags: int = 0) -> bytes:
    return struct.pack("<4sIIIIIII", MAGIC, seq, width, height, nbytes, flags, 0, 0)


def read_slot(path: Path, width: int, height: int, spins: int = 4) -> bytes | None:
    """Return the latest BGRA frame, or None when the writer is mid-update."""
    try:
        handle = path.open("rb")
    except OSError:
        return None
    nbytes = width * height * 4
    try:
        for _ in range(spins):
            handle.seek(0)
            header = handle.read(HEADER)
            if len(header) != HEADER or header[:4] != MAGIC:
                return None
            seq1, got_w, got_h, got_n, _a, _b, _c = struct.unpack_from("<IIIIIII", header, 4)
            if seq1 == 0 or seq1 & 1 or got_w != width or got_h != height or got_n != nbytes:
                return None
            pixels = handle.read(nbytes)
            if len(pixels) != nbytes:
                return None
            handle.seek(4)
            seq2 = struct.unpack("<I", handle.read(4))[0]
            if seq1 == seq2:
                return pixels
        return None
    finally:
        handle.close()


class SlotWriter:
    """Seqlock frame file. Readers ignore odd sequence numbers."""

    def __init__(self, path: Path, width: int, height: int, premultiplied: bool = False) -> None:
        if width < 2 or height < 2:
            raise ValueError("slot raster is too small")
        self.path = Path(path)
        self.width = width
        self.height = height
        self.nbytes = width * height * 4
        self.flags = FLAG_PREMULTIPLIED if premultiplied else 0
        self.seq = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"\x00" * (HEADER + self.nbytes))

    def _seq(self, seq: int) -> None:
        with self.path.open("r+b") as handle:
            handle.seek(0)
            handle.write(pack_header(seq, self.width, self.height, self.nbytes, self.flags))
            handle.flush()

    def write(self, pixels: bytes) -> None:
        if len(pixels) != self.nbytes:
            raise ValueError("frame size does not match the slot")
        odd = (self.seq + 1) | 1
        self._seq(odd)
        with self.path.open("r+b") as handle:
            handle.seek(HEADER)
            handle.write(pixels)
            handle.flush()
        even = odd + 1
        self._seq(even)
        self.seq = even


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    if abs(estimate - left) <= abs(estimate - up) and abs(estimate - left) <= abs(estimate - up_left):
        return left
    if abs(estimate - up) <= abs(estimate - up_left):
        return up
    return up_left


def decode_png_bgra(png: bytes) -> bytes:
    """Turn an 8-bit RGB or RGBA PNG into straight BGRA. The alpha channel is kept."""
    if not png.startswith(_PNG_SIG):
        raise ValueError("CG frame is not a PNG")
    offset = len(_PNG_SIG)
    width = height = 0
    color = 0
    compressed = bytearray()
    while offset + 8 <= len(png):
        length = struct.unpack_from(">I", png, offset)[0]
        kind = png[offset + 4:offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(png):
            raise ValueError("truncated PNG")
        chunk = png[start:end]
        if kind == b"IHDR":
            width, height, depth, color, compress, png_filter, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or color not in (2, 6) or compress != 0 or png_filter != 0 or interlace != 0:
                raise ValueError("CG PNG must be 8-bit RGB or RGBA without interlacing")
        elif kind == b"IDAT":
            compressed += chunk
        elif kind == b"IEND":
            break
        offset = end + 4
    if width < 1 or height < 1 or not compressed:
        raise ValueError("CG PNG has no image")
    channels = 4 if color == 6 else 3
    raw = zlib.decompress(bytes(compressed))
    stride = width * channels
    expected = height * (stride + 1)
    if len(raw) < expected:
        raise ValueError("CG PNG rows are short")
    rows = []
    cursor = 0
    for y in range(height):
        mode = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        prior = rows[-1] if rows else bytearray(stride)
        if mode == 1:
            for i in range(stride):
                row[i] = (row[i] + (row[i - channels] if i >= channels else 0)) & 255
        elif mode == 2:
            for i in range(stride):
                row[i] = (row[i] + prior[i]) & 255
        elif mode == 3:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + ((left + prior[i]) // 2)) & 255
        elif mode == 4:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                up_left = prior[i - channels] if i >= channels else 0
                row[i] = (row[i] + _paeth(left, prior[i], up_left)) & 255
        elif mode != 0:
            raise ValueError("CG PNG filter is unknown")
        rows.append(row)
    out = bytearray(width * height * 4)
    at = 0
    for row in rows:
        if channels == 4:
            for x in range(0, stride, 4):
                out[at] = row[x + 2]
                out[at + 1] = row[x + 1]
                out[at + 2] = row[x]
                out[at + 3] = row[x + 3]
                at += 4
        else:
            for x in range(0, stride, 3):
                out[at] = row[x + 2]
                out[at + 1] = row[x + 1]
                out[at + 2] = row[x]
                out[at + 3] = 255
                at += 4
    return bytes(out)


def wpe_argv(url: str, width: int, height: int) -> list[str]:
    return [
        "gst-launch-1.0", "-q",
        "wpesrc", f"location={url}", "draw-background=false",
        "!", "videoconvert",
        "!", "videoscale",
        "!", "videorate",
        "!", f"video/x-raw,format=BGRA,width={width},height={height},framerate=30/1",
        "!", "fdsink", "fd=1", "sync=false",
    ]


def key_argv(format_code: str) -> list[str]:
    """Fill is DeckLink input 3 and key is input 4. The pair is not configurable."""
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-f", "decklink", "-raw_format", "uyvy422", "-format_code", format_code, "-i", SDI_FILL,
        "-f", "decklink", "-raw_format", "uyvy422", "-format_code", format_code, "-i", SDI_KEY,
        "-filter_complex", "[1:v]format=gray[k];[0:v][k]alphamerge,format=bgra,fps=30",
        "-f", "rawvideo", "-pix_fmt", "bgra", "pipe:1",
    ]


def chromium_argv(browser: str, port: int, url: str, width: int, height: int) -> list[str]:
    # The console service is root. Chrome refuses that sandbox, and each layer
    # needs its own profile or the second page loses the singleton lock.
    return [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-sandbox", "--disable-dev-shm-usage",
        "--no-first-run", "--force-device-scale-factor=1",
        "--default-background-color=00000000",
        f"--user-data-dir=/var/lib/nexencoder/chrome/{port}",
        f"--remote-debugging-port={port}",
        f"--window-size={width},{height}",
        url,
    ]


def ndi_argv(name: str, width: int, height: int) -> list[str]:
    """Receive one NDI source. BGRA from NDI is premultiplied; the slot flag says so."""
    if '"' in name or "\\" in name:
        raise ValueError("NDI source name cannot contain quotes")
    return [
        "gst-launch-1.0", "-q",
        "ndisrc", f'ndi-name="{name}"',
        "!", "videoconvert",
        "!", "videoscale",
        "!", "videorate",
        "!", f"video/x-raw,format=BGRA,width={width},height={height},framerate=30/1",
        "!", "fdsink", "fd=1", "sync=false",
    ]


def ndi_available(inspect=None) -> bool:
    """True when GStreamer's ndisrc element is installed. The NDI SDK is not downloaded."""
    if inspect is not None:
        return bool(inspect())
    global _NDI_READY
    if _NDI_READY is None:
        _NDI_READY = _ndi_probe()
    return _NDI_READY


def _ndi_probe() -> bool:
    if shutil.which("gst-inspect-1.0") is None:
        return False
    try:
        done = subprocess.run(
            ["gst-inspect-1.0", "ndisrc"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def feeder_key(layer: dict) -> str:
    source = layer["source"]
    if source["type"] == "html":
        return f"html:{layer['slot']}:{source['url']}"
    if source["type"] == "ndi":
        return f"ndi:{layer['slot']}:{source['name']}"
    return "sdi"


def plan_changes(running: dict[str, dict], desired: list[dict]) -> tuple[list[str], list[dict]]:
    """Slots whose source changed stop. New sources start. The key is the source, not the z-order."""
    want = {feeder_key(layer): layer for layer in desired}
    stop = [key for key in running if key not in want]
    start = [layer for key, layer in want.items() if key not in running]
    return stop, start


def _wpe_present(runner=None) -> bool:
    if runner is None:
        runner = subprocess.run
    try:
        done = runner(
            ["gst-inspect-1.0", "wpesrc"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(done, "returncode", 1) == 0


def detect_html_backend(which=shutil.which, inspect=None) -> str:
    if which("gst-launch-1.0") and (inspect if inspect is not None else _wpe_present)():
        return "wpe"
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if which(name):
            return name
    return "none"


def _browser_name(backend: str) -> str | None:
    if backend in ("none", "wpe"):
        return None
    return backend


def load_layer_spec(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("layers"), list):
        return None
    return raw


def update_key_owner(state_dir: Path, channel_id: str, needed: bool) -> str:
    """Record whether this channel needs the shared fill/key. The lowest id runs it."""
    directory = cg_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "keyfill.lock"
    refs_path = directory / "keyfill.refs"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            refs = set(json.loads(refs_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            refs = set()
        refs = {item for item in refs if item in ("A", "B")}
        if needed:
            refs.add(channel_id)
        else:
            refs.discard(channel_id)
        refs_path.write_text(json.dumps(sorted(refs)) + "\n", encoding="utf-8")
        if not refs:
            return ""
        return sorted(refs)[0]
    finally:
        os.close(fd)


def _drain(pipe, label: str) -> None:
    try:
        for line in iter(pipe.readline, b""):
            log.warning("%s %s", label, line.decode("utf-8", "replace").rstrip())
    except OSError:
        log.warning("%s log stopped", label)


def _read_exact(pipe, size: int, stop: threading.Event) -> bytes | None:
    buf = bytearray()
    while len(buf) < size and not stop.is_set():
        chunk = pipe.read(size - len(buf))
        if not chunk:
            return None
        buf += chunk
    if len(buf) != size:
        return None
    return bytes(buf)


def _stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_pipe_feeder(argv: list[str], writer: SlotWriter, holder: "_Running", spawn=subprocess.Popen) -> None:
    proc = spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    holder.proc = proc
    if proc.stderr is not None:
        threading.Thread(target=_drain, args=(proc.stderr, argv[0]), name="nexencoder-cg-log", daemon=True).start()
    try:
        while not holder.stop.is_set():
            if proc.stdout is None:
                break
            frame = _read_exact(proc.stdout, writer.nbytes, holder.stop)
            if frame is None:
                break
            writer.write(frame)
    finally:
        _stop_proc(proc)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _ws_handshake(host: str, port: int, path: str) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise OSError("devtools handshake closed")
        buf += chunk
        if len(buf) > 65536:
            sock.close()
            raise OSError("devtools handshake is too large")
    status = buf.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        sock.close()
        raise OSError("devtools did not upgrade")
    return sock


def _ws_send(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    header += mask
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_recv(sock: socket.socket) -> str:
    def read(count: int) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            chunk = sock.recv(count - len(buf))
            if not chunk:
                raise OSError("devtools socket closed")
            buf += chunk
        return bytes(buf)

    first = read(2)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read(8))[0]
    if length > 20_000_000:
        raise OSError("devtools frame is too large")
    data = read(length)
    opcode = first[0] & 0x0F
    if opcode == 0x8:
        raise OSError("devtools socket closed")
    if opcode != 0x1:
        return ""
    return data.decode("utf-8", "replace")


def run_chromium_feeder(
    browser: str,
    url: str,
    writer: SlotWriter,
    holder: "_Running",
    spawn=subprocess.Popen,
) -> None:
    """Paint one HTML URL with Chromium and write BGRA frames into the slot."""
    port = _free_port()
    proc = spawn(
        chromium_argv(browser, port, url, writer.width, writer.height),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    holder.proc = proc
    if proc.stderr is not None:
        threading.Thread(target=_drain, args=(proc.stderr, "chromium"), name="nexencoder-cg-log", daemon=True).start()
    sock = None
    try:
        target = ""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not holder.stop.is_set():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as response:
                    pages = json.loads(response.read().decode("utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.2)
                continue
            if isinstance(pages, list):
                for page in pages:
                    if isinstance(page, dict) and page.get("webSocketDebuggerUrl"):
                        target = str(page["webSocketDebuggerUrl"])
                        break
            if target:
                break
        if not target:
            raise OSError("Chromium did not open a devtools page")
        parsed = urlparse(target)
        sock = _ws_handshake(parsed.hostname or "127.0.0.1", parsed.port or port, parsed.path or "/")
        sock.settimeout(5)
        call_id = 0

        def call(method: str, params: dict | None = None) -> dict:
            nonlocal call_id
            call_id += 1
            expect = call_id
            _ws_send(sock, json.dumps({"id": expect, "method": method, "params": params or {}}))
            while not holder.stop.is_set():
                try:
                    message = _ws_recv(sock)
                except (TimeoutError, socket.timeout):
                    continue
                if not message:
                    continue
                payload = json.loads(message)
                if payload.get("id") != expect:
                    continue
                if payload.get("error"):
                    raise OSError(str(payload["error"]))
                result = payload.get("result")
                return result if isinstance(result, dict) else {}
            return {}

        call("Page.enable")
        call("Emulation.setDefaultBackgroundColorOverride", {
            "color": {"r": 0, "g": 0, "b": 0, "a": 0},
        })
        while not holder.stop.is_set():
            started = time.monotonic()
            result = call("Page.captureScreenshot", {
                "format": "png",
                "omitBackground": True,
                "fromSurface": True,
            })
            frame = result.get("data")
            if isinstance(frame, str):
                try:
                    pixels = decode_png_bgra(base64.b64decode(frame))
                except (ValueError, zlib.error) as exc:
                    log.warning("HTML frame dropped: %s", exc)
                else:
                    if len(pixels) == writer.nbytes:
                        writer.write(pixels)
                    else:
                        log.warning("HTML frame is %s bytes, slot wants %s", len(pixels), writer.nbytes)
            remain = (1 / 30) - (time.monotonic() - started)
            if remain > 0 and holder.stop.wait(remain):
                break
    finally:
        if sock is not None:
            sock.close()
        _stop_proc(proc)


class _Running:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

    def halt(self) -> None:
        self.stop.set()
        _stop_proc(self.proc)
        if self.thread is not None:
            self.thread.join(timeout=3)


def _start_layer(layer: dict, spec: dict, backend: str, spawn) -> _Running:
    holder = _Running()
    width = int(spec["width"])
    height = int(spec["height"])
    source = layer["source"]
    premultiplied = source["type"] == "ndi" or (source["type"] == "html" and backend == "wpe")
    writer = SlotWriter(Path(layer["path"]), width, height, premultiplied=premultiplied)
    if source["type"] == "sdi-key":
        argv = key_argv(spec["format_code"])
        thread = threading.Thread(
            target=run_pipe_feeder, args=(argv, writer, holder, spawn), name="nexencoder-keyfill", daemon=True,
        )
    elif source["type"] == "ndi":
        argv = ndi_argv(source["name"], width, height)
        thread = threading.Thread(
            target=run_pipe_feeder, args=(argv, writer, holder, spawn), name="nexencoder-ndi", daemon=True,
        )
    elif backend == "wpe":
        argv = wpe_argv(source["url"], width, height)
        thread = threading.Thread(
            target=run_pipe_feeder, args=(argv, writer, holder, spawn), name="nexencoder-html", daemon=True,
        )
    elif _browser_name(backend):
        thread = threading.Thread(
            target=run_chromium_feeder,
            args=(backend, source["url"], writer, holder, spawn),
            name="nexencoder-html",
            daemon=True,
        )
    else:
        log.error("no HTML renderer for %s; the layer stays transparent", source.get("url"))
        return holder
    holder.thread = thread
    thread.start()
    return holder


def reconcile_once(
    state_dir: Path,
    channel_id: str,
    running: dict,
    spawn=subprocess.Popen,
    backend: str | None = None,
) -> dict:
    """Start and stop feeders so they match the layer manifest. Returns the new table."""
    spec = load_layer_spec(layers_path(state_dir, channel_id))
    desired = []
    needs_key = False
    chosen = backend if backend is not None else detect_html_backend()
    if spec:
        for layer in spec["layers"]:
            source = layer.get("source") or {}
            if source.get("type") == "sdi-key":
                needs_key = True
                desired.append(layer)
            elif source.get("type") == "html" and chosen != "none":
                desired.append(layer)
            elif source.get("type") == "ndi" and ndi_available():
                desired.append(layer)
            elif source.get("type") == "ndi":
                global _NDI_MISSING_LOGGED
                if not _NDI_MISSING_LOGGED:
                    log.error("NDI receiver is not installed; %s stays transparent", source.get("name"))
                    _NDI_MISSING_LOGGED = True
    owner = update_key_owner(state_dir, channel_id, needs_key)
    if owner != channel_id:
        desired = [layer for layer in desired if (layer.get("source") or {}).get("type") != "sdi-key"]
    stop, start = plan_changes(running, desired)
    for key in stop:
        running.pop(key).halt()
    for layer in start:
        if spec is None:
            continue
        running[feeder_key(layer)] = _start_layer(layer, spec, chosen, spawn)
    return running


def supervise(
    state_dir: Path,
    channel_id: str,
    stop: threading.Event,
    spawn=subprocess.Popen,
    backend: str | None = None,
) -> None:
    running: dict = {}
    while not stop.is_set():
        try:
            running = reconcile_once(state_dir, channel_id, running, spawn, backend)
        except Exception:
            log.warning("CG supervisor for %s failed", channel_id, exc_info=True)
        stop.wait(0.5)
    update_key_owner(state_dir, channel_id, False)
    for feeder in running.values():
        feeder.halt()
