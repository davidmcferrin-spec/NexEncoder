"""Prometheus exposition for inputs and outputs."""

from nexencoder.metrics import InputStatus, OutputStatus, Snapshot, render_metrics


def _samples(text: str, name: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(name + "{") or line.startswith(name + " ")]


def test_idle_snapshot_omits_unmeasured_series():
    text = render_metrics()
    assert "nexencoder_up 1" in text
    assert 'nexencoder_input_up{input="A"} 0' in text
    assert 'nexencoder_input_up{input="B"} 0' in text
    assert 'nexencoder_output_connected{output="A"} 0' in text
    assert 'nexencoder_output_info{output="A",mode="srt-listener"} 1' in text
    assert _samples(text, "nexencoder_output_video_bitrate_bps") == []
    assert _samples(text, "nexencoder_input_width") == []
    assert _samples(text, "nexencoder_output_rtt_seconds") == []
    assert 'nexencoder_output_dropped_frames_total{output="A"} 0' in text


def test_measured_values_and_label_escaping():
    snap = Snapshot(
        inputs=(InputStatus("A", up=True, format_name='1080i "main"', width=1920, height=1080,
                            field_rate_hz=59.94, captions_up=True, scte104_messages=3),),
        outputs=(OutputStatus("A", up=True, connected=True, mode="srt-caller",
                              video_bitrate_bps=8_000_000, video_bitrate_target_bps=8_000_000,
                              rtt_seconds=0.025, packets_lost=2, dropped_frames=1),),
    )
    text = render_metrics(snap)
    assert 'format="1080i \\"main\\""' in text
    assert 'raster="1920x1080"' in text
    assert 'field_rate_hz="59.94"' in text
    assert 'nexencoder_input_width{input="A"} 1920' in text
    assert 'nexencoder_input_field_rate_hz{input="A"} 59.94' in text
    assert 'nexencoder_output_video_bitrate_bps{output="A"} 8000000' in text
    assert 'nexencoder_output_rtt_seconds{output="A"} 0.025' in text
    assert 'nexencoder_output_packets_lost_total{output="A"} 2' in text
    assert 'nexencoder_input_scte104_messages_total{input="A"} 3' in text
