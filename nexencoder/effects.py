"""Squeeze, CG layers, and the files the capture filter reads.

A preset stores a four-corner squeeze and up to four CG layers. Each layer
has a level from −4 to +4. Level 0 is the video input, so a CG layer cannot
use it. Recall and normalize write those files in place. They do not restart
the channel, so the VANC path and ``spliceinject`` keep running.

Corner order is top-left, top-right, bottom-left, bottom-right, in raster
pixels. Frame 0 of an N-frame move is the quad already on air. Frame N is
the target. N = 0 cuts on the frame the command is applied.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .signal import signal_mode

MAX_LAYERS = 4
MAX_FRAMES = 600
STILL_SECONDS = 8
LEVELS = frozenset({-4, -3, -2, -1, 1, 2, 3, 4})
SDI_FILL = "DeckLink Duo (3)"
SDI_KEY = "DeckLink Duo (4)"
HTML_ORIGIN = "http://127.0.0.1"
WAITING_MODES = frozenset({"slate", "loop", "bars"})


def identity_fractions() -> tuple[tuple[float, float], ...]:
    """Corners as fractions of the raster. 0 is the left or top edge, 1 is the far edge."""
    return ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0))


def pixel_corners(corners) -> bool:
    """Values outside −2..2 are raster pixels from an older preset."""
    return any(abs(x) > 2 or abs(y) > 2 for x, y in corners)


def to_fractions(corners, width: int, height: int):
    return tuple((float(x) / width, float(y) / height) for x, y in corners)


def scale_corners(corners, width: int, height: int):
    return tuple((float(x) * width, float(y) * height) for x, y in corners)


def identity_corners(width: int, height: int) -> tuple[tuple[float, float], ...]:
    return ((0.0, 0.0), (float(width), 0.0), (0.0, float(height)), (float(width), float(height)))


def lerp_corners(start, target, index: int, frames: int):
    """Linear step. ``frames`` of 0 returns ``target`` immediately."""
    if frames <= 0:
        return tuple((float(x), float(y)) for x, y in target)
    u = min(1.0, max(0, index) / frames)
    out = []
    for (x0, y0), (x1, y1) in zip(start, target):
        out.append((x0 + (x1 - x0) * u, y0 + (y1 - y0) * u))
    return tuple(out)


def field_corners(corners):
    """Full-raster corners in one field. Y is halved; X stays."""
    return tuple((float(x), float(y) / 2.0) for x, y in corners)


def corners_close(left, right, eps: float = 0.5) -> bool:
    return all(abs(a - b) <= eps and abs(c - d) <= eps for (a, c), (b, d) in zip(left, right))


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    n = len(rhs)
    aug = [row[:n] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(aug[row][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        scale = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= scale
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor == 0:
                continue
            for j in range(col, n + 1):
                aug[row][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]


def homography(dest_corners, source_corners) -> tuple[float, ...] | None:
    """Map an output pixel onto the source. None when the quad is degenerate."""
    rows = []
    rhs = []
    for (u, v), (x, y) in zip(source_corners, dest_corners):
        rows.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        rhs.append(float(u))
        rows.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        rhs.append(float(v))
    solved = _solve(rows, rhs)
    if solved is None:
        return None
    return tuple(solved) + (1.0,)


def map_point(matrix: tuple[float, ...], x: float, y: float) -> tuple[float, float] | None:
    weight = matrix[6] * x + matrix[7] * y + matrix[8]
    if abs(weight) < 1e-9:
        return None
    return (
        (matrix[0] * x + matrix[1] * y + matrix[2]) / weight,
        (matrix[3] * x + matrix[4] * y + matrix[5]) / weight,
    )


def _pixel(buf: bytes | bytearray, width: int, x: int, y: int) -> tuple[int, int, int, int]:
    at = (y * width + x) * 4
    return buf[at], buf[at + 1], buf[at + 2], buf[at + 3]


def sample_rgba(buf: bytes | bytearray, width: int, height: int, x: float, y: float):
    if x < -0.5 or y < -0.5 or x >= width - 0.5 or y >= height - 0.5:
        return 0, 0, 0, 0
    x0 = math.floor(x)
    y0 = math.floor(y)
    x1 = min(int(x0) + 1, width - 1)
    y1 = min(int(y0) + 1, height - 1)
    x0c = max(int(x0), 0)
    y0c = max(int(y0), 0)
    fx = x - x0
    fy = y - y0
    p00 = _pixel(buf, width, x0c, y0c)
    p10 = _pixel(buf, width, x1, y0c)
    p01 = _pixel(buf, width, x0c, y1)
    p11 = _pixel(buf, width, x1, y1)
    out = []
    for channel in range(4):
        top = p00[channel] * (1 - fx) + p10[channel] * fx
        bot = p01[channel] * (1 - fx) + p11[channel] * fx
        out.append(int(top * (1 - fy) + bot * fy + 0.5))
    return out[0], out[1], out[2], out[3]


def _put(buf: bytearray, width: int, x: int, y: int, pixel) -> None:
    at = (y * width + x) * 4
    buf[at:at + 4] = bytes(pixel)


def warp_rgba(src: bytes, width: int, height: int, corners) -> bytearray:
    """Squeeze ``src`` so its corners land on ``corners``. Outside the quad is clear."""
    matrix = homography(corners, identity_corners(width, height))
    out = bytearray(width * height * 4)
    if matrix is None:
        return out
    for y in range(height):
        for x in range(width):
            mapped = map_point(matrix, x + 0.5, y + 0.5)
            if mapped is None:
                continue
            _put(out, width, x, y, sample_rgba(src, width, height, mapped[0] - 0.5, mapped[1] - 0.5))
    return out


def split_fields(src: bytes, width: int, height: int) -> tuple[bytes, bytes]:
    stride = width * 4
    half = height // 2
    top = bytearray(stride * half)
    bottom = bytearray(stride * half)
    for y in range(half):
        top[y * stride:(y + 1) * stride] = src[(2 * y) * stride:(2 * y + 1) * stride]
        bottom[y * stride:(y + 1) * stride] = src[(2 * y + 1) * stride:(2 * y + 2) * stride]
    return bytes(top), bytes(bottom)


def weave_fields(top: bytes, bottom: bytes, width: int, height: int) -> bytes:
    stride = width * 4
    half = height // 2
    out = bytearray(stride * height)
    for y in range(half):
        out[(2 * y) * stride:(2 * y + 1) * stride] = top[y * stride:(y + 1) * stride]
        out[(2 * y + 1) * stride:(2 * y + 2) * stride] = bottom[y * stride:(y + 1) * stride]
    return bytes(out)


def squeeze_rgba(src: bytes, width: int, height: int, corners, interlaced: bool = False) -> bytes:
    if height % 2 or not interlaced:
        return bytes(warp_rgba(src, width, height, corners))
    half = height // 2
    top, bottom = split_fields(src, width, height)
    warped_top = warp_rgba(top, width, half, field_corners(corners))
    warped_bottom = warp_rgba(bottom, width, half, field_corners(corners))
    return weave_fields(warped_top, warped_bottom, width, height)


def _over(dst: bytearray, at: int, src) -> None:
    sa = src[3] / 255.0
    if sa <= 0:
        return
    if sa >= 1:
        dst[at:at + 4] = bytes(src[:4])
        return
    for channel in range(3):
        dst[at + channel] = int(src[channel] * sa + dst[at + channel] * (1 - sa) + 0.5)
    dst[at + 3] = int(min(255, src[3] + dst[at + 3] * (1 - sa) + 0.5))


def composite_rgba(video: bytes, width: int, height: int, layers: list[dict]) -> bytes:
    """Black, then levels below 0, then the primary, then levels above 0.

    −4 is furthest behind the video and +4 is the top. Level 0 is the video.
    """
    out = bytearray(width * height * 4)
    for at in range(0, len(out), 4):
        out[at:at + 4] = b"\x00\x00\x00\xff"

    def paint(pixels: bytes) -> None:
        if len(pixels) != width * height * 4:
            return
        for at in range(0, len(pixels), 4):
            _over(out, at, pixels[at:at + 4])

    ordered = sorted(
        (layer for layer in layers if isinstance(layer.get("level"), int) and layer["level"] != 0),
        key=lambda item: item["level"],
    )
    video_drawn = False
    for layer in ordered:
        if not video_drawn and layer["level"] > 0:
            paint(video)
            video_drawn = True
        pixels = layer.get("rgba")
        if isinstance(pixels, (bytes, bytearray)):
            paint(bytes(pixels))
    if not video_drawn:
        paint(video)
    return bytes(out)


class SqueezeRuntime:
    """Frame clock shared with the capture filter. One ``advance`` per input frame."""

    def __init__(self, width: int, height: int) -> None:
        ident = identity_corners(width, height)
        self.width = width
        self.height = height
        self.current = ident
        self.start = ident
        self.target = ident
        self.frames = 0
        self.step = 0
        self.seq = 0

    def apply_new(self, seq: int, frames: int, corners) -> None:
        self.start = self.current
        self.target = tuple((float(x), float(y)) for x, y in corners)
        self.frames = max(0, int(frames))
        self.step = 0
        self.seq = int(seq)

    def advance(self):
        self.current = lerp_corners(self.start, self.target, self.step, self.frames)
        if self.frames > 0 and self.step < self.frames:
            self.step += 1
        return self.current

    def resume(self, run: dict, state: dict | None) -> None:
        """Restore a move after the capture process restarts, then take a new seq."""
        if run and state and run.get("seq") == state.get("seq"):
            self.seq = int(run["seq"])
            self.frames = int(run["frames"])
            self.step = int(run["step"])
            self.current = _corners(run["current"])
            self.start = _corners(run["start"])
            self.target = _corners(run["target"])
            return
        if run:
            self.current = _corners(run["current"])
        if state:
            self.apply_new(state["seq"], state["frames"], state["corners"])


def _corners(raw) -> tuple[tuple[float, float], ...]:
    if len(raw) != 4:
        raise ValueError("a quad needs four corners")
    out = []
    for point in raw:
        if len(point) != 2:
            raise ValueError("a corner needs x and y")
        x = float(point[0])
        y = float(point[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("corner coordinates must be finite")
        if not -8192 <= x <= 16384 or not -8192 <= y <= 16384:
            raise ValueError("corner coordinates are out of range")
        out.append((x, y))
    return tuple(out)


def absolute_cg_url(url: str, origin: str = HTML_ORIGIN) -> str:
    text = url.strip()
    if any(ord(ch) < 32 or ch.isspace() for ch in text):
        raise ValueError("CG URL cannot contain spaces or control characters")
    if len(text) > 2000:
        raise ValueError("CG URL is too long")
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("/") and not text.startswith("//"):
        return origin.rstrip("/") + text
    raise ValueError("CG URL must be http, https, or a path on this console")


def _ndi_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("NDI source needs a name")
    name = " ".join(value.split())
    if not name or len(name) > 200:
        raise ValueError("NDI source name must be 1 to 200 characters")
    if '"' in name or "\\" in name:
        raise ValueError("NDI source name cannot contain quotes")
    return name


def _source(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("layer source must be an object")
    kind = raw.get("type")
    if kind == "html":
        return {"type": "html", "url": absolute_cg_url(str(raw.get("url") or ""))}
    if kind == "sdi-key":
        return {"type": "sdi-key"}
    if kind == "ndi":
        return {"type": "ndi", "name": _ndi_name(raw.get("name"))}
    raise ValueError("layer source must be html, ndi, or sdi-key")


def normalize_effect(raw: object, channel_format: str | None = None) -> dict:
    """Return one effect with legal corners, frame counts, and at most four layers."""
    if not isinstance(raw, dict):
        raise ValueError("effect must be an object")
    format_name = raw.get("format") or channel_format
    if format_name is not None and not isinstance(format_name, str):
        raise ValueError("effect needs a raster")
    mode = signal_mode(format_name) if isinstance(format_name, str) and format_name else None
    supplied = raw.get("corners")
    if supplied:
        corners = _corners(supplied)
        if pixel_corners(corners):
            if mode is None:
                raise ValueError("pixel corners need a raster")
            corners = to_fractions(corners, mode.width, mode.height)
    else:
        corners = identity_fractions()
        if mode is None:
            format_name = "1080i59.94"
            mode = signal_mode(format_name)
    layers_in = raw.get("layers") or []
    if not isinstance(layers_in, list):
        raise ValueError("layers must be a list")
    if len(layers_in) > MAX_LAYERS:
        raise ValueError(f"a channel can hold {MAX_LAYERS} CG layers")
    layers = []
    seen: set[int] = set()
    keys = 0
    for item in layers_in:
        if not isinstance(item, dict):
            raise ValueError("a layer must be an object")
        level = item.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or level not in LEVELS:
            raise ValueError("layer level must be -4 to -1 or +1 to +4")
        if level in seen:
            raise ValueError("two layers share a level")
        seen.add(level)
        source = _source(item.get("source"))
        if source["type"] == "sdi-key":
            keys += 1
        layers.append({"level": level, "source": source})
    if keys > 1:
        raise ValueError("only one SDI fill/key pair can be on a channel")
    return {
        "format": mode.format_name if mode is not None else "",
        "corners": corners,
        "in_frames": _frames(raw.get("in_frames", 0)),
        "out_frames": _frames(raw.get("out_frames", 0)),
        "layers": layers,
    }


def _frames(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("frame count must be an integer")
    if not 0 <= value <= MAX_FRAMES:
        raise ValueError(f"frame count must be 0 to {MAX_FRAMES}")
    return value


def normalize_preset(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("preset must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
        raise ValueError("preset name must be 1 to 64 characters")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("preset name cannot contain control characters")
    effect = normalize_effect(raw)
    effect["name"] = name.strip()
    return effect


def presets_path(state_dir: Path) -> Path:
    return state_dir / "effects.json"


def effect_path(state_dir: Path, channel_id: str) -> Path:
    return state_dir / f"{channel_id}.effect"


def run_path(state_dir: Path, channel_id: str) -> Path:
    return state_dir / f"{channel_id}.effect.run"


def layers_path(state_dir: Path, channel_id: str) -> Path:
    return state_dir / f"{channel_id}.layers.json"


def live_path(state_dir: Path, channel_id: str) -> Path:
    return state_dir / f"{channel_id}.live.json"


def wait_path(state_dir: Path, channel_id: str) -> Path:
    return state_dir / f"{channel_id}.wait"


def signal_path(state_dir: Path, channel_id: str) -> Path:
    return state_dir / f"{channel_id}.signal"


def bars_path(state_dir: Path, channel_id: str) -> Path:
    return state_dir / f"{channel_id}.bars"


def loop_slot_path(state_dir: Path, channel_id: str) -> Path:
    return cg_dir(state_dir) / f"{channel_id}-loop.nxcg"


def live_slot_path(state_dir: Path, channel_id: str) -> Path:
    return cg_dir(state_dir) / f"{channel_id}-live.nxcg"


def pcm_path(state_dir: Path, channel_id: str) -> Path:
    return cg_dir(state_dir) / f"{channel_id}-live.pcm"


def still_path(state_dir: Path, channel_id: str) -> Path:
    return state_dir / f"{channel_id}.jpg"


def cg_dir(state_dir: Path) -> Path:
    return state_dir / "cg"


def slot_path(state_dir: Path, channel_id: str, slot: int) -> Path:
    return cg_dir(state_dir) / f"{channel_id}-{slot}.nxcg"


def keyfill_path(state_dir: Path) -> Path:
    return cg_dir(state_dir) / "keyfill.nxcg"


def load_presets(state_dir: Path) -> list[dict]:
    path = presets_path(state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    presets = []
    for item in raw:
        try:
            presets.append(normalize_preset(item))
        except ValueError:
            continue
    return presets


def save_presets(state_dir: Path, presets: list[dict]) -> list[dict]:
    clean = [normalize_preset(item) for item in presets]
    if len({item["name"] for item in clean}) != len(clean):
        raise ValueError("preset names must be unique")
    state_dir.mkdir(parents=True, exist_ok=True)
    path = presets_path(state_dir)
    path.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    return clean


def upsert_preset(state_dir: Path, raw: dict) -> dict:
    preset = normalize_preset(raw)
    current = [item for item in load_presets(state_dir) if item["name"] != preset["name"]]
    current.append(preset)
    current.sort(key=lambda item: item["name"])
    save_presets(state_dir, current)
    return preset


def delete_preset(state_dir: Path, name: str) -> None:
    current = [item for item in load_presets(state_dir) if item["name"] != name]
    save_presets(state_dir, current)


def _public_corners(corners) -> list[list[float]]:
    return [[x, y] for x, y in corners]


def preset_json(preset: dict) -> dict:
    return {
        "name": preset["name"],
        "format": preset["format"],
        "corners": _public_corners(preset["corners"]),
        "in_frames": preset["in_frames"],
        "out_frames": preset["out_frames"],
        "layers": preset["layers"],
    }


def render_state(seq: int, frames: int, corners, slots: list[tuple[int, str]]) -> str:
    """Text the ``nexeffect`` filter reads. Level 0 is an empty slot."""
    lines = ["NXSQ 1", f"seq {int(seq)}", f"frames {int(frames)}"]
    for x, y in corners:
        lines.append(f"{x:.6f} {y:.6f}")
    if len(slots) != MAX_LAYERS:
        raise ValueError("the filter state has four layer slots")
    for index, (level, path) in enumerate(slots):
        token = path if path else "-"
        if any(ch.isspace() for ch in token):
            raise ValueError("layer path cannot contain spaces")
        lines.append(f"slot {index} {int(level)} {token}")
    return "\n".join(lines) + "\n"


def parse_state(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "NXSQ 1":
        raise ValueError("effect state is not NXSQ 1")
    if len(lines) != 3 + 4 + MAX_LAYERS:
        raise ValueError("effect state is incomplete")
    seq = _labeled_int(lines[1], "seq")
    frames = _labeled_int(lines[2], "frames")
    corners = []
    for line in lines[3:7]:
        parts = line.split()
        if len(parts) != 2:
            raise ValueError("corner line needs x and y")
        corners.append((float(parts[0]), float(parts[1])))
    slots = []
    for index, line in enumerate(lines[7:]):
        parts = line.split()
        if len(parts) != 4 or parts[0] != "slot" or int(parts[1]) != index:
            raise ValueError("slot line is malformed")
        slots.append((int(parts[2]), parts[3]))
    return {"seq": seq, "frames": frames, "corners": tuple(corners), "slots": slots}


def _labeled_int(line: str, label: str) -> int:
    parts = line.split()
    if len(parts) != 2 or parts[0] != label:
        raise ValueError(f"expected {label}")
    return int(parts[1])


def _read_seq(path: Path) -> int:
    try:
        parsed = parse_state(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(parsed["seq"])


def _slot_plan(state_dir: Path, channel_id: str, effect: dict) -> tuple[list[tuple[int, int, str]], list[dict]]:
    slots: list[tuple[int, str]] = [(0, "-")] * MAX_LAYERS
    manifest = []
    for index, layer in enumerate(effect["layers"]):
        source = layer["source"]
        if source["type"] == "sdi-key":
            path = keyfill_path(state_dir)
        else:
            path = slot_path(state_dir, channel_id, index)
        slots[index] = (layer["level"], path.as_posix())
        manifest.append({
            "slot": index,
            "level": layer["level"],
            "source": source,
            "path": path.as_posix(),
        })
    return slots, manifest


def commit_effect(
    state_dir: Path,
    channel_id: str,
    effect: dict,
    frames: int,
    preset_name: str = "",
    raster: str | None = None,
) -> dict:
    """Write the layer manifest, then the filter state. The filter polls the state.

    Preset corners are fractions of the frame. ``raster`` is the channel's
    current format, so one preset applies to 720p, 1080i, and 1080p.
    """
    if channel_id not in ("A", "B"):
        raise ValueError("channel must be A or B")
    mode = signal_mode(raster or effect["format"])
    pixels = scale_corners(effect["corners"], mode.width, mode.height)
    slots, manifest = _slot_plan(state_dir, channel_id, effect)
    seq = _read_seq(effect_path(state_dir, channel_id)) + 1
    body = render_state(seq, frames, pixels, slots)
    cg_dir(state_dir).mkdir(parents=True, exist_ok=True)
    layers_path(state_dir, channel_id).write_text(json.dumps({
        "format": mode.format_name,
        "format_code": mode.format_code,
        "width": mode.width,
        "height": mode.height,
        "interlaced": mode.interlaced,
        "layers": manifest,
    }, indent=2) + "\n", encoding="utf-8")
    live_path(state_dir, channel_id).write_text(json.dumps({
        "preset": preset_name,
        "out_frames": effect["out_frames"],
        "format": mode.format_name,
    }, indent=2) + "\n", encoding="utf-8")
    target = effect_path(state_dir, channel_id)
    tmp = target.with_suffix(".effect.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return {"seq": seq, "frames": frames, "layers": len(manifest)}


def _channel_format(config: dict, channel_id: str) -> str:
    try:
        return config["inputs"][channel_id]["format"]
    except (KeyError, TypeError) as exc:
        raise ValueError("channel raster is not configured") from exc


def _load_live(state_dir: Path, channel_id: str) -> dict:
    try:
        raw = json.loads(live_path(state_dir, channel_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def recall_effect(state_dir: Path, channel_id: str, raw: dict, config: dict) -> dict:
    channel_format = _channel_format(config, channel_id)
    preset_name = ""
    if isinstance(raw.get("preset"), str) and raw["preset"].strip():
        preset_name = raw["preset"].strip()
        match = next((item for item in load_presets(state_dir) if item["name"] == preset_name), None)
        if match is None:
            raise ValueError(f"unknown preset {preset_name}")
        effect = match
    elif isinstance(raw.get("effect"), dict):
        effect = normalize_effect(raw["effect"], channel_format)
    else:
        raise ValueError("recall needs a preset name or an effect")
    written = commit_effect(
        state_dir, channel_id, effect, effect["in_frames"], preset_name, raster=channel_format,
    )
    written["channel"] = channel_id
    written["preset"] = preset_name
    written["html"] = any(layer["source"]["type"] == "html" for layer in effect["layers"])
    written["ndi"] = any(layer["source"]["type"] == "ndi" for layer in effect["layers"])
    return written


def normalize_channel(state_dir: Path, channel_id: str, raw: dict, config: dict) -> dict:
    channel_format = _channel_format(config, channel_id)
    mode = signal_mode(channel_format)
    live = _load_live(state_dir, channel_id)
    if "frames" in raw and raw["frames"] is not None:
        frames = _frames(raw["frames"])
    else:
        frames = _frames(live.get("out_frames", 0)) if isinstance(live.get("out_frames", 0), int) else 0
    effect = {
        "format": mode.format_name,
        "corners": identity_fractions(),
        "in_frames": frames,
        "out_frames": frames,
        "layers": [],
    }
    written = commit_effect(state_dir, channel_id, effect, frames, "", raster=channel_format)
    written["channel"] = channel_id
    written["normalized"] = True
    return written


def write_text_atomic(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def write_flag(path: Path, enabled: bool) -> None:
    write_text_atomic(path, "1\n" if enabled else "0\n")


def read_flag(path: Path, default: bool = False) -> bool:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    if text == "1":
        return True
    if text == "0":
        return False
    return default


def render_wait(waiting: str, ident: str, loop: str, live: str = "") -> str:
    """Text the capture filter reads for the −5 picture and the +5 ident."""
    if waiting not in WAITING_MODES:
        raise ValueError("waiting picture must be slate, loop, or bars")
    ident_line = " ".join(ident.split()) or "-"
    loop_line = (loop or "").strip() or "-"
    live_line = (live or "").strip() or "-"
    if any(mark in line for line in (loop_line, live_line) for mark in ("\n", "\r")):
        raise ValueError("loop path cannot contain newlines")
    return (
        f"NXWAIT 1\nwaiting {waiting}\nident {ident_line}\n"
        f"loop {loop_line}\nlive {live_line}\n"
    )


def write_wait(
    state_dir: Path, channel_id: str, waiting: str, ident: str, loop: str, live: str = ""
) -> None:
    write_text_atomic(wait_path(state_dir, channel_id), render_wait(waiting, ident, loop, live))


def escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")
