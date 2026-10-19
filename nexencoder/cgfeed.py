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
import queue
import subprocess
import threading
import time
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


def png_size(png: bytes) -> tuple[int, int]:
    """Width and height from the IHDR, or (0, 0) when it is not a PNG."""
    if not png.startswith(_PNG_SIG) or len(png) < 24 or png[12:16] != b"IHDR":
        return 0, 0
    width, height = struct.unpack_from(">II", png, 16)
    return int(width), int(height)


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


def with_lower_priority(argv: list[str], *, demote: bool) -> list[str]:
    """Run a renderer below the capture so its startup cannot stall DeckLink."""
    if demote:
        return ["/usr/bin/nice", "-n", "10", *argv]
    return list(argv)


CHROME_PROFILES = Path("/var/lib/nexencoder/chrome")
# Puppeteer gives a browser 30 s to show its first page. This renderer runs
# under nice 10 behind two encodes, so it gets a sliver of the CPU; the old
# 8 s budget expired on every start and the layer never came up.
PAGE_WAIT = 30.0
_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def chromium_profile(slot_path: str | Path, root: Path = CHROME_PROFILES) -> Path:
    """One profile per layer slot, reused across restarts.

    A fresh profile on every start is a cold start every time and leaves a
    directory behind for each attempt. The slot file is ``<channel>-<slot>``,
    which is unique on the host and readable in a listing.
    """
    return root / Path(slot_path).stem


def prepare_profile(profile: Path) -> None:
    """Make the profile usable after a killed Chrome and drop the old per-port dirs."""
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("cannot create Chrome profile %s: %s", profile, exc)
        return
    for name in _SINGLETON_FILES:
        try:
            (profile / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("cannot clear %s in %s: %s", name, profile, exc)
    try:
        siblings = list(profile.parent.iterdir())
    except OSError:
        return
    for entry in siblings:
        if entry.name.isdigit() and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)


def chromium_argv(browser: str, port: int, url: str, width: int, height: int, profile: str | Path | None = None) -> list[str]:
    # The console service is root. Chrome refuses that sandbox, and each layer
    # needs its own profile or the second page loses the singleton lock.
    if profile is None:
        profile = CHROME_PROFILES / str(port)
    return [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-sandbox", "--disable-dev-shm-usage",
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-sync",
        "--disable-component-update", "--disable-default-apps", "--disable-breakpad",
        "--disable-domain-reliability",
        "--disable-component-extensions-with-background-pages",
        "--disable-features=Translate,MediaRouter,OptimizationHints,PushMessaging",
        "--force-device-scale-factor=1",
        "--default-background-color=00000000",
        f"--user-data-dir={Path(profile).as_posix()}",
        f"--remote-debugging-port={port}",
        # Without this, current Chrome omits webSocketDebuggerUrl from the page list.
        "--remote-allow-origins=*",
        f"--window-size={width},{height}",
        url,
    ]


def chromium_env(base: dict | None = None) -> dict:
    """Environment for one headless Chrome.

    ``disabled:`` is not a dbus address. Chrome rejects it on every attempt
    and the page target never appears. Unset means there is no session bus.
    """
    env = dict(os.environ if base is None else base)
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    return env


def browser_socket_url(line: str) -> str:
    """The browser websocket Chrome prints once the debugging port is open."""
    mark = "DevTools listening on "
    at = line.find(mark)
    if at < 0:
        return ""
    url = line[at + len(mark):].strip().split()[0]
    if url.startswith("ws://") or url.startswith("wss://"):
        return url
    return ""


def choose_page_target(infos, url: str) -> tuple[str, str]:
    """Pick a page target id and its current URL. A tab is only a fallback.

    ``/json/list`` on current headless Chrome has no page websocket. The
    browser socket's target list does. Prefer the page already on ``url``.
    """
    if not isinstance(infos, list):
        return "", ""
    pages: list[tuple[str, str]] = []
    tabs: list[tuple[str, str]] = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        target_id = info.get("targetId")
        if not isinstance(target_id, str) or not target_id:
            continue
        current = info.get("url")
        current = current if isinstance(current, str) else ""
        if info.get("type") == "page":
            pages.append((target_id, current))
        elif info.get("type") == "tab":
            tabs.append((target_id, current))
    for group in (pages, tabs):
        for target_id, current in group:
            if current == url:
                return target_id, current
        if group:
            return group[0]
    return "", ""


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


_CHROME_QUIET = (
    "Failed to connect to the bus",
    "NameHasOwner",
    "DEPRECATED_ENDPOINT",
)


def _drain(pipe, label: str, on_line=None) -> None:
    quiet = label == "chromium"
    try:
        for line in iter(pipe.readline, b""):
            text = line.decode("utf-8", "replace").rstrip()
            if on_line is not None and text:
                on_line(text)
            if not text or (quiet and any(mark in text for mark in _CHROME_QUIET)):
                continue
            log.warning("%s %s", label, text)
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
        f"Origin: http://{host}:{port}\r\n"
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


def describe_targets(infos) -> str:
    """One line of what the browser socket reported, for the journal."""
    if not isinstance(infos, list):
        return "no target list"
    parts = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        kind = info.get("type") or "?"
        current = info.get("url") or ""
        parts.append(f"{kind} {current}".rstrip())
    if not parts:
        return "no targets"
    return "targets: " + "; ".join(parts)


def _create_page(call, url: str, deadline: float) -> tuple[str, str]:
    created = call("Target.createTarget", {"url": url}, deadline=deadline)
    target_id = created.get("targetId")
    if not isinstance(target_id, str) or not target_id:
        return "", ""
    return target_id, url


def _session_for(call, target_id: str, deadline: float) -> str:
    attached = call(
        "Target.attachToTarget",
        {"targetId": target_id, "flatten": True},
        deadline=deadline,
    )
    session_id = attached.get("sessionId")
    if not isinstance(session_id, str):
        return ""
    return session_id


def _attach_page(call, holder: "_Running", url: str, wait: float = PAGE_WAIT) -> tuple[str, str]:
    """Return (session id, reason) for the bug page on an open browser socket.

    The browser prints its socket before it has a tab. Rather than wait for
    that tab, ask for one as soon as the list has none. The reason is empty on
    success and says what the browser reported when it is not.
    """
    deadline = time.monotonic() + wait
    polls = 0
    seen = "no answer from the browser"
    target_id = ""
    current = ""
    while time.monotonic() < deadline and not holder.stop.is_set():
        polls += 1
        try:
            infos = call("Target.getTargets", deadline=deadline).get("targetInfos")
        except OSError as exc:
            seen = f"Target.getTargets failed: {exc}"
            break
        seen = describe_targets(infos)
        target_id, current = choose_page_target(infos, url)
        if target_id:
            break
        try:
            target_id, current = _create_page(call, url, deadline)
        except OSError as exc:
            seen = f"{seen}; Target.createTarget failed: {exc}"
        if target_id:
            break
        if holder.stop.wait(0.5):
            return "", "stopped"
    if holder.stop.is_set():
        return "", "stopped"
    if not target_id:
        return "", f"after {polls} polls, {seen}"
    late = time.monotonic() + 5
    try:
        session_id = _session_for(call, target_id, late)
    except OSError as exc:
        return "", f"Target.attachToTarget failed: {exc}"
    if not session_id:
        return "", "Target.attachToTarget gave no session"
    try:
        call("Page.enable", session_id=session_id, deadline=late)
    except OSError as exc:
        log.info("CG page %s was not usable (%s); opening a new one", target_id, exc)
        try:
            target_id, current = _create_page(call, url, late)
            if not target_id:
                return "", "Target.createTarget gave no target"
            session_id = _session_for(call, target_id, late)
            if not session_id:
                return "", "Target.attachToTarget gave no session"
            call("Page.enable", session_id=session_id, deadline=late)
        except OSError as again:
            return "", f"second page failed: {again}"
    if current != url:
        try:
            call("Page.navigate", {"url": url}, session_id, deadline=late)
        except OSError as exc:
            return "", f"Page.navigate failed: {exc}"
    return session_id, ""


def _prepare_page(call, session_id: str, width: int, height: int) -> None:
    """Size the viewport to the slot and keep the page background clear.

    ``--window-size`` is the outer window. New headless Chrome takes its tab
    strip and toolbar out of that, so a 1920x1080 window paints 1920x993 and
    every frame missed the slot. The metrics override sets the viewport itself.
    """
    call("Emulation.setDeviceMetricsOverride", {
        "width": width,
        "height": height,
        "deviceScaleFactor": 1,
        "mobile": False,
    }, session_id)
    call("Emulation.setDefaultBackgroundColorOverride", {
        "color": {"r": 0, "g": 0, "b": 0, "a": 0},
    }, session_id)


def run_chromium_feeder(
    browser: str,
    url: str,
    writer: SlotWriter,
    holder: "_Running",
    spawn=subprocess.Popen,
    profile: Path | None = None,
) -> None:
    """Paint one HTML URL with Chromium and write BGRA frames into the slot."""
    port = _free_port()
    if profile is None:
        profile = chromium_profile(writer.path)
    prepare_profile(profile)
    # nice is inherited by Chrome's children, so the whole startup stays
    # behind the DeckLink capture. preexec_fn is unsafe in a threaded process.
    proc = None
    sock = None
    started = time.monotonic()
    try:
        proc = spawn(
            with_lower_priority(
                chromium_argv(browser, port, url, writer.width, writer.height, profile),
                demote=os.name == "posix" and os.path.isfile("/usr/bin/nice"),
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=chromium_env(),
        )
        holder.proc = proc
        sockets: queue.Queue[str] = queue.Queue()

        def on_line(text: str) -> None:
            found = browser_socket_url(text)
            if found:
                sockets.put(found)

        if proc.stderr is not None:
            threading.Thread(
                target=_drain, args=(proc.stderr, "chromium", on_line),
                name="nexencoder-cg-log", daemon=True,
            ).start()
        browser_url = ""
        deadline = time.monotonic() + PAGE_WAIT
        while time.monotonic() < deadline and not holder.stop.is_set():
            if proc.poll() is not None:
                raise OSError(f"Chromium exited with status {proc.returncode} before opening a devtools socket")
            try:
                browser_url = sockets.get(timeout=0.2)
            except queue.Empty:
                continue
            if browser_url:
                break
        if not browser_url:
            if holder.stop.is_set():
                return
            raise OSError(f"Chromium did not open a devtools socket in {time.monotonic() - started:.0f}s")
        parsed = urlparse(browser_url)
        if not parsed.hostname or not parsed.port:
            raise OSError("Chromium did not open a devtools socket")
        sock = _ws_handshake(parsed.hostname, parsed.port, parsed.path or "/")
        sock.settimeout(5)
        call_id = 0

        def call(method: str, params: dict | None = None, session_id: str | None = None, deadline: float | None = None) -> dict:
            nonlocal call_id
            call_id += 1
            expect = call_id
            body: dict = {"id": expect, "method": method, "params": params or {}}
            if session_id:
                body["sessionId"] = session_id
            _ws_send(sock, json.dumps(body))
            while not holder.stop.is_set():
                if deadline is not None and time.monotonic() > deadline:
                    raise OSError(f"{method} was not answered in time")
                try:
                    message = _ws_recv(sock)
                except (TimeoutError, socket.timeout):
                    continue
                if not message:
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if payload.get("id") != expect:
                    continue
                if payload.get("error"):
                    raise OSError(str(payload["error"]))
                result = payload.get("result")
                return result if isinstance(result, dict) else {}
            return {}

        session_id, reason = _attach_page(call, holder, url)
        if not session_id:
            if holder.stop.is_set():
                return
            status = proc.poll()
            alive = "still running" if status is None else f"exited with status {status}"
            raise OSError(
                f"Chromium did not open a devtools page in {time.monotonic() - started:.0f}s "
                f"({reason}; browser {alive})"
            )
        log.info("CG page %s is up after %.1fs", url, time.monotonic() - started)
        _prepare_page(call, session_id, writer.width, writer.height)
        wrong_size_noted = False
        while not holder.stop.is_set():
            started = time.monotonic()
            result = call("Page.captureScreenshot", {
                "format": "png",
                "omitBackground": True,
                "fromSurface": True,
            }, session_id)
            frame = result.get("data")
            if isinstance(frame, str):
                png = base64.b64decode(frame)
                try:
                    pixels = decode_png_bgra(png)
                except (ValueError, zlib.error) as exc:
                    log.warning("HTML frame dropped: %s", exc)
                else:
                    if len(pixels) == writer.nbytes:
                        writer.write(pixels)
                    elif not wrong_size_noted:
                        wrong_size_noted = True
                        got_w, got_h = png_size(png)
                        log.warning(
                            "HTML frames are %sx%s but the slot is %sx%s; they are dropped until Chrome resizes",
                            got_w, got_h, writer.width, writer.height,
                        )
            # Ten frames a second is enough for a bug. A tighter loop keeps
            # Chrome busy and the DeckLink capture overruns.
            remain = max(0.05, (1 / 10) - (time.monotonic() - started))
            if holder.stop.wait(remain):
                break
    except OSError as exc:
        log.warning("HTML renderer stopped: %s", exc)
        holder.stop.wait(5)
    except Exception:
        log.warning("HTML renderer stopped", exc_info=True)
        holder.stop.wait(5)
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


def finished_keys(running: dict) -> list[str]:
    """Feeders whose thread ended on its own. A halt() is not a failure."""
    done = []
    for key, holder in running.items():
        thread = getattr(holder, "thread", None)
        if thread is None or thread.is_alive() or holder.stop.is_set():
            continue
        done.append(key)
    return done


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
    for key in finished_keys(running):
        log.warning("CG feeder %s stopped; starting it again", key)
        running.pop(key).halt()
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


def probe_html(url: str, seconds: float, browser: str | None = None, width: int = 1280, height: int = 720) -> int:
    """Run the Chromium feeder once, outside the channel, and report what happened.

    ``python3 -m nexencoder.cgfeed --probe http://127.0.0.1/cg/bug.html`` on the
    encoder host shows Chrome's own messages, the time to the first painted
    frame, and how much of that frame is opaque. Exit status 0 means a frame
    arrived; 1 means the page never painted in the time given.
    """
    import tempfile

    if browser is None:
        browser = next(
            (name for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser") if shutil.which(name)),
            "",
        )
    if not browser:
        print("no Chromium or Google Chrome on PATH", flush=True)
        return 2
    with tempfile.TemporaryDirectory(prefix="nexencoder-probe-") as temp:
        slot = Path(temp) / "probe-0.nxcg"
        writer = SlotWriter(slot, width, height)
        holder = _Running()
        holder.thread = threading.Thread(
            target=run_chromium_feeder,
            args=(browser, url, writer, holder),
            kwargs={"profile": Path(temp) / "profile"},
            name="nexencoder-probe",
            daemon=True,
        )
        print(f"starting {browser} on {url} at {width}x{height} for up to {seconds:.0f}s", flush=True)
        begin = time.monotonic()
        holder.thread.start()
        first = None
        frame = None
        while time.monotonic() - begin < seconds and holder.thread.is_alive():
            got = read_slot(slot, width, height)
            if got is not None:
                frame = got
                if first is None:
                    first = time.monotonic() - begin
                    print(f"first frame after {first:.1f}s (slot seq {writer.seq})", flush=True)
            time.sleep(0.25)
        holder.halt()
        if frame is None:
            print("no frame was painted", flush=True)
            return 1
        opaque = sum(1 for at in range(3, len(frame), 4) if frame[at])
        total = width * height
        print(
            f"{writer.seq // 2} frames in {time.monotonic() - begin:.1f}s; "
            f"last frame has {opaque} of {total} pixels with alpha ({100 * opaque / total:.1f}%)",
            flush=True,
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python3 -m nexencoder.cgfeed", description=probe_html.__doc__)
    parser.add_argument("--probe", metavar="URL", required=True, help="HTML page to paint once with Chromium")
    parser.add_argument("--seconds", type=float, default=PAGE_WAIT + 15, help="how long to wait for the first frame")
    parser.add_argument("--browser", default=None, help="browser binary; the default is the first one on PATH")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return probe_html(args.probe, args.seconds, args.browser, args.width, args.height)


if __name__ == "__main__":
    raise SystemExit(main())
