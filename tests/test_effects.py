"""Squeeze math, layer order, presets, and the filter state file."""

import pytest

from nexencoder.channels import save_config
from nexencoder.effects import (
    SqueezeRuntime,
    commit_effect,
    composite_rgba,
    corners_close,
    field_corners,
    homography,
    identity_corners,
    lerp_corners,
    map_point,
    normalize_channel,
    normalize_effect,
    parse_state,
    recall_effect,
    render_state,
    sample_rgba,
    squeeze_rgba,
    upsert_preset,
)


def test_zero_frames_cuts_and_n_frames_lands_on_the_target():
    start = identity_corners(1920, 1080)
    target = ((100.0, 50.0), (1800.0, 40.0), (80.0, 1000.0), (1700.0, 1040.0))
    assert lerp_corners(start, target, 0, 0) == target
    assert lerp_corners(start, target, 0, 15) == start
    assert corners_close(lerp_corners(start, target, 15, 15), target, eps=0.001)
    halfway = lerp_corners(start, target, 15, 30)
    assert halfway[0] == pytest.approx((50.0, 25.0))


def test_runtime_counts_frames_from_the_quad_already_on_air():
    runtime = SqueezeRuntime(8, 8)
    target = ((2.0, 2.0), (6.0, 2.0), (2.0, 6.0), (6.0, 6.0))
    runtime.apply_new(1, 2, target)
    assert runtime.advance() == identity_corners(8, 8)
    assert runtime.advance()[0] == pytest.approx((1.0, 1.0))
    assert corners_close(runtime.advance(), target, eps=0.001)
    assert corners_close(runtime.advance(), target, eps=0.001)


def test_resume_holds_a_finished_move_and_a_new_seq_starts_from_there():
    runtime = SqueezeRuntime(8, 8)
    target = ((2.0, 2.0), (6.0, 2.0), (2.0, 6.0), (6.0, 6.0))
    runtime.apply_new(1, 0, target)
    runtime.advance()
    run = {
        "seq": runtime.seq,
        "frames": runtime.frames,
        "step": runtime.step,
        "current": runtime.current,
        "start": runtime.start,
        "target": runtime.target,
    }
    restored = SqueezeRuntime(8, 8)
    restored.resume(run, {"seq": 1, "frames": 0, "corners": target})
    assert restored.current == runtime.current
    restarted = SqueezeRuntime(8, 8)
    restarted.resume(run, {"seq": 2, "frames": 0, "corners": identity_corners(8, 8)})
    assert restarted.start == target
    assert restarted.seq == 2
    assert restarted.advance() == identity_corners(8, 8)


def test_homography_maps_a_centered_squeeze():
    width, height = 1920, 1080
    source = identity_corners(width, height)
    dest = ((width / 4, height / 4), (3 * width / 4, height / 4), (width / 4, 3 * height / 4), (3 * width / 4, 3 * height / 4))
    matrix = homography(dest, source)
    assert matrix is not None
    top_left = map_point(matrix, width / 4, height / 4)
    center = map_point(matrix, width / 2, height / 2)
    assert top_left == pytest.approx((0.0, 0.0), abs=0.02)
    assert center == pytest.approx((width / 2, height / 2), abs=0.02)
    outside = map_point(matrix, 0, 0)
    assert outside is not None
    assert sample_rgba(b"\xff" * (width * height * 4), width, height, outside[0], outside[1])[3] == 0
    assert homography(((0, 0), (0, 0), (0, 0), (0, 0)), source) is None


def test_interlaced_squeeze_keeps_each_field_on_its_own_lines():
    width, height = 4, 4
    frame = bytearray(width * height * 4)
    for y in range(height):
        color = (255, 0, 0, 255) if y % 2 == 0 else (0, 0, 255, 255)
        for x in range(width):
            at = (y * width + x) * 4
            frame[at:at + 4] = bytes(color)
    squeezed = squeeze_rgba(bytes(frame), width, height, identity_corners(width, height), interlaced=True)
    for y in range(height):
        pixel = squeezed[y * width * 4:(y * width * 4) + 4]
        expected = (255, 0, 0, 255) if y % 2 == 0 else (0, 0, 255, 255)
        assert tuple(pixel) == expected
    assert field_corners(((0, 0), (4, 0), (0, 4), (4, 4)))[2][1] == 2


def test_layers_stack_from_minus_four_through_the_video_to_plus_four():
    video = bytes([
        0, 0, 0, 0,
        0, 0, 0, 0,
        0, 0, 255, 255,
        0, 0, 255, 255,
        0, 0, 255, 255,
    ])
    far = bytes([0, 255, 0, 255, 0, 255, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    near = bytes([0, 0, 0, 0, 255, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    plus = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 0, 255, 0, 0, 0, 0])
    top = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 255, 255, 255, 255, 255, 255])
    out = composite_rgba(video, 5, 1, [
        {"level": 2, "rgba": top},
        {"level": -1, "rgba": near},
        {"level": 1, "rgba": plus},
        {"level": -4, "rgba": far},
    ])
    assert out[0:4] == b"\x00\xff\x00\xff"
    assert out[4:8] == b"\xff\x00\x00\xff"
    assert out[8:12] == b"\x00\x00\xff\xff"
    assert out[12:16] == b"\xff\xff\xff\xff"
    assert out[16:20] == b"\xff\xff\xff\xff"


def test_preset_limits_layers_and_raster():
    good = {
        "name": "sidebar",
        "format": "1080i59.94",
        "corners": [[200, 0], [1920, 0], [200, 1080], [1920, 1080]],
        "in_frames": 12,
        "out_frames": 0,
        "layers": [
            {"level": -1, "source": {"type": "html", "url": "/cg/bug.html"}},
            {"level": 2, "source": {"type": "sdi-key"}},
        ],
    }
    effect = normalize_effect(good)
    assert effect["layers"][0]["source"]["url"].startswith("http://127.0.0.1/cg/bug.html")
    assert effect["layers"][1]["source"] == {"type": "sdi-key"}
    ndi = normalize_effect({
        "format": "1080i59.94",
        "layers": [{"level": 3, "source": {"type": "ndi", "name": "  GRAPHICS (NDI)  "}}],
    })
    assert ndi["layers"][0]["source"] == {"type": "ndi", "name": "GRAPHICS (NDI)"}
    assert len(effect["layers"]) == 2
    with pytest.raises(ValueError, match="2 CG layers"):
        normalize_effect({
            "format": "1080i59.94",
            "layers": good["layers"] + [
                {"level": 4, "source": {"type": "html", "url": "https://cg.example/c"}},
            ],
        })
    with pytest.raises(ValueError, match="one SDI"):
        normalize_effect({
            "format": "1080i59.94",
            "layers": [
                {"level": 1, "source": {"type": "sdi-key"}},
                {"level": -1, "source": {"type": "sdi-key"}},
            ],
        })
    with pytest.raises(ValueError, match="share a level"):
        normalize_effect({
            "format": "1080i59.94",
            "layers": [
                {"level": -2, "source": {"type": "html", "url": "https://cg.example/a"}},
                {"level": -2, "source": {"type": "html", "url": "https://cg.example/b"}},
            ],
        })
    with pytest.raises(ValueError, match="level"):
        normalize_effect({
            "format": "1080i59.94",
            "layers": [{"level": 0, "source": {"type": "html", "url": "https://cg.example/a"}}],
        })
    with pytest.raises(ValueError):
        normalize_effect({"format": "1080i59.94", "in_frames": -1})
    with pytest.raises(ValueError):
        normalize_effect({"format": "1080i59.94", "layers": [
            {"level": 1, "source": {"type": "html", "url": "http://cg.example/a b"}},
        ]})


def test_recall_and_normalize_write_filter_state_without_a_new_pipeline(tmp_path):
    config_path = tmp_path / "config.json"
    save_config(config_path, {
        "inputs": {"A": {"format": "1080i59.94", "device": "DeckLink Duo (1)"}},
        "outputs": {"A": {"mode": "srt-listener", "bitrate": 8, "host": "", "port": 9000, "url": ""}},
    })
    from nexencoder.channels import load_config
    config = load_config(config_path)
    preset = upsert_preset(tmp_path, {
        "name": "sidebar",
        "format": "1080i59.94",
        "corners": [[480, 0], [1920, 0], [480, 1080], [1920, 1080]],
        "in_frames": 10,
        "out_frames": 8,
        "layers": [{"level": -4, "source": {"type": "html", "url": "https://cg.example/bug"}}],
    })
    recalled = recall_effect(tmp_path, "A", {"preset": preset["name"]}, config)
    state = parse_state((tmp_path / "A.effect").read_text(encoding="utf-8"))
    assert recalled["frames"] == 10
    assert recalled["html"] is True
    assert state["frames"] == 10
    assert state["corners"][0] == pytest.approx((480.0, 0.0))
    assert len(state["slots"]) == 2
    assert state["slots"][0][0] == -4
    assert state["slots"][1] == (0, "-")
    omitted = normalize_channel(tmp_path, "A", {}, config)
    assert omitted["frames"] == 8
    normalized = normalize_channel(tmp_path, "A", {"frames": 0}, config)
    cleared = parse_state((tmp_path / "A.effect").read_text(encoding="utf-8"))
    assert normalized["frames"] == 0
    assert cleared["seq"] == state["seq"] + 2
    assert cleared["corners"] == identity_corners(1920, 1080)
    assert all(slot[0] == 0 for slot in cleared["slots"])
    roundtrip = parse_state(render_state(cleared["seq"], cleared["frames"], cleared["corners"], cleared["slots"]))
    assert roundtrip["seq"] == cleared["seq"]


def test_recall_scales_a_preset_onto_the_channel_raster(tmp_path):
    from nexencoder.channels import load_config
    save_config(tmp_path / "config.json", {})
    config = load_config(tmp_path / "config.json")
    upsert_preset(tmp_path, {
        "name": "hd720",
        "format": "720p59.94",
        "corners": [[320, 180], [960, 180], [320, 540], [960, 540]],
        "in_frames": 0,
        "out_frames": 0,
        "layers": [],
    })
    recall_effect(tmp_path, "A", {"preset": "hd720"}, config)
    state = parse_state((tmp_path / "A.effect").read_text(encoding="utf-8"))
    assert state["corners"][0] == pytest.approx((480.0, 270.0))
    assert state["corners"][1] == pytest.approx((1440.0, 270.0))


def test_commit_roundtrip_used_by_the_filter(tmp_path):
    effect = normalize_effect({
        "format": "720p50",
        "in_frames": 0,
        "out_frames": 4,
        "corners": [[0, 0], [1280, 0], [0, 720], [1000, 700]],
        "layers": [],
    })
    commit_effect(tmp_path, "B", effect, 4)
    parsed = parse_state((tmp_path / "B.effect").read_text(encoding="utf-8"))
    assert parsed["frames"] == 4
    assert parsed["corners"][3] == pytest.approx((1000.0, 700.0))
