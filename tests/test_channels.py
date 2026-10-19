"""VANC buffer scan and the metrics snapshot a config produces."""

from nexencoder.cdp import CC_608_FIELD1, CC_DTVCC_DATA, CC_DTVCC_START, CcTriplet, build_cdp
from nexencoder.channels import normalize_config, snapshot_for
from nexencoder.metrics import render_metrics
from nexencoder import scte104 as s104
from nexencoder.status import parse_ffmpeg_progress, signals_from_progress
from nexencoder.vanc import scan_vanc


def _cdp() -> bytes:
    triplets = [
        CcTriplet(True, CC_608_FIELD1, 0x94, 0x2C),
        CcTriplet(True, CC_DTVCC_START, 0x42, 0xFE),
        CcTriplet(False, CC_DTVCC_DATA, 0x00, 0x00),
    ]
    return build_cdp(4, 7, triplets)


def test_scan_finds_a_cdp_and_a_splice_around_junk():
    mom = s104.build_mom(s104.Mom(0, 0, 1, 0, 0, s104.TIME_NONE, b"", (s104.SpliceNull104(),)))
    payload = b"\xff\xff\x00\x01" + b"\x00\x10" + _cdp() + b"\x11\x22" + mom + b"\x00"
    found = scan_vanc(payload)
    assert len(found["captions"]) == 1
    assert found["captions"][0].sequence == 7
    assert len(found["splices"]) == 1
    assert isinstance(found["splices"][0].operations[0], s104.SpliceNull104)


def test_normalize_keeps_legal_values_and_drops_the_rest():
    raw = {
        "inputs": {"A": {"format": "720p50", "device": " DeckLink Duo (1) "}, "C": {"format": "2160p"}},
        "outputs": {
            "A": {"mode": "hls-push", "bitrate": 99, "host": "bad host", "port": 0, "url": "ftp://nope"},
            "B": {"mode": "srt-caller", "bitrate": 18, "host": "origin.example", "port": 9001, "url": ""},
        },
    }
    config = normalize_config(raw)
    assert config["inputs"]["A"]["format"] == "720p50"
    assert config["inputs"]["A"]["device"] == "DeckLink Duo (1)"
    assert config["inputs"]["B"]["format"] == "1080i59.94"
    assert config["inputs"]["A"]["bitrate"] == 8
    assert config["outputs"]["A"][0]["mode"] == "hls-push"
    assert config["outputs"]["A"][0]["url"] == ""
    assert config["outputs"]["A"][0]["id"] == "1"
    assert config["outputs"]["B"][0]["host"] == "origin.example"
    assert config["inputs"]["B"]["bitrate"] == 18
    assert config["outputs"]["B"][0]["port"] == 9001
    assert config["inputs"]["A"]["source"] == "sdi"
    listener = normalize_config({
        "outputs": {"B": {"mode": "srt-listener", "port": 9001}},
    })
    assert listener["outputs"]["B"][0]["port"] == 9101


def test_each_input_keeps_up_to_four_destinations():
    crowded = normalize_config({
        "inputs": {"A": {"bitrate": 10}},
        "outputs": {
            "A": [{"bitrate": 18, "mode": "hls-pull", "url": f"https://origin.example/{i}"} for i in range(5)],
            "B": [
                {"mode": "srt-listener", "port": 9101},
                {"id": "2", "mode": "srt-listener", "port": 9101},
            ],
        },
    })
    assert crowded["inputs"]["A"]["bitrate"] == 10
    assert len(crowded["outputs"]["A"]) == 4
    assert [item["id"] for item in crowded["outputs"]["A"]] == ["1", "2", "3", "4"]
    assert crowded["outputs"]["B"][0]["port"] == 9101
    assert crowded["outputs"]["B"][1]["port"] == 9200
    text = render_metrics(snapshot_for(crowded, {"A": {"running": True, "bitrate_bps": 8_000_000}}))
    assert text.count("nexencoder_output_video_bitrate_bps{") == 1
    assert 'destination="1"' in text and 'destination="4"' in text


def test_snapshot_omits_resolution_until_the_input_locks():
    config = normalize_config({})
    idle = render_metrics(snapshot_for(config))
    assert 'format="1080i59.94"' in idle
    assert "nexencoder_output_video_bitrate_target_bps{" in idle
    assert "nexencoder_input_width{" not in idle
    live = render_metrics(snapshot_for(config, {"A": {"locked": True, "running": True, "bitrate_bps": 8_100_000}}))
    assert "nexencoder_input_width" in live
    assert "8100000" in live


def test_progress_file_is_fresh_only_while_frames_move(tmp_path):
    text = "frame=40\nfps=29.97\nbitrate=8100.5kbits/s\ndrop_frames=2\nprogress=continue\n"
    fields = parse_ffmpeg_progress(text + "frame=41\nbitrate=N/A\nprogress=continue\n")
    assert fields["frame"] == "41"
    path = tmp_path / "A.progress"
    path.write_text(text, encoding="utf-8")
    fresh = signals_from_progress(tmp_path, now=path.stat().st_mtime + 1)
    assert fresh["A"]["locked"] is True
    assert fresh["A"]["bitrate_bps"] == 8_100_500
    stale = signals_from_progress(tmp_path, now=path.stat().st_mtime + 30)
    assert stale["A"]["locked"] is False
    assert stale["A"]["bitrate_bps"] is None
    assert stale["A"]["dropped"] == 2
