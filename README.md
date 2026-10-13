# NexEncoder

Two-input HD-SDI to SRT/HLS encoder for a Dell R4xx server (R440 realistic minimum) with a
Blackmagic DeckLink Duo / Duo 2.

- Inputs: 2 independent HD-SDI (1080i / 720p) with CEA-608/708 captions and SCTE-104
- Outputs per input: SRT (caller or listener) or HLS (push or pull), H.264 6-18 Mbps + AAC stereo
- Captions: 708 re-embedded as A/53 SEI; 608 carried inside the 708 CDP
- SCTE: 104 translated to SCTE-35 splice_info_section, muxed on a data PID
- Monitor: 25-50% proxy over WebRTC (MediaMTX) with caption and SCTE sideband over WebSocket

No Node. No Docker. Runs on the host under systemd (Ubuntu 24.04 LTS).

## Setup

    ./setup.sh

Ubuntu 24.04 LTS. Packages come from APT (no pip, no venv): Python, GStreamer,
Prometheus, and node_exporter. `setup.sh` downloads FFmpeg 9.0.2 and builds it into
`/usr/local` with DeckLink, libx264, and libopus. That build needs the Desktop Video
headers in `/opt/decklink-sdk`. It also installs a pinned MediaMTX v1.21.1 binary,
`nexencoder-console.service`, and the `nexencoder-channel@` template. TSDuck and the
Desktop Video driver are checked for and not downloaded. Channel units stay stopped
until you start them. A crashed channel, console, or MediaMTX is restarted with no
start limit. A channel whose frame count stops moving is restarted by its supervisor.
A bad channel config exits 78 and stays stopped until the config is fixed.

    sudo systemctl start nexencoder-channel@A nexencoder-channel@B

## Tests

    python3 -m pytest -q

## Operator console

`web/` is the operator console, served by the control process so the pages can read metrics and manage units:

    python3 -m nexencoder.console

It listens on `127.0.0.1:8788`. The UI uses the NexApp theme kit and shell. Raster, DeckLink
device, bitrate, and SRT/HLS settings are stored by `POST /api/config` (restart the channel
unit to apply them). Metrics charts CPU, memory, disk, GPU, and uptime from Prometheus, plus
every `nexencoder_*` series. Measured bitrate and resolution appear after a channel's ffmpeg
progress file is updating. Services can start, stop, restart, enable, and disable the
allowlisted units, including `mediamtx` and `nexencoder-channel@A` / `@B`.

The monitor page plays each input from MediaMTX at `http://<host>:8889/inputA/whep` and
`inputB`. Caption and splice notes show up when something posts them to `/api/sideband`.
`POST /api/channels/A/scte` translates a SCTE-104 message and sends the SCTE-35 section to
the channel's spliceinject port.

## Modules (`nexencoder/`)

| Module | Purpose |
|---|---|
| `cdp.py` | CEA-708 Caption Distribution Packet parse/build (checksum verified) |
| `dtvcc.py` | DTVCC packet assembly and service-block split |
| `cea708.py` | 708 service decoder: windows, text, scrolling (plain text, no styling) |
| `cea608.py` | 608 decoder CC1-CC4: pop-on, roll-up, paint-on, special/extended chars |
| `captions.py` | One CDP in, 608 + 708 caption events out |
| `scte104.py` | SCTE-104 multiple_operation_message parse/build |
| `scte35.py` | SCTE-35 splice_info_section parse/build (CRC verified) |
| `translate.py` | SCTE-104 to SCTE-35 translation |
| `ts.py` | MPEG-TS packetizer and section extractor for SCTE-35 PIDs |
| `inject.py` | SCTE-104 injector: scenarios to SCTE-35 TS file plus JSON log, with self-check |
| `metrics.py` | Prometheus exposition for input and output state |
| `console.py` | Operator console, `/metrics`, config, preview, Prometheus proxy, service control |
| `signal.py` | 1080i / 720p rasters and DeckLink format codes |
| `decklink.py` | ffmpeg device-list and status JSON parsers |
| `vanc.py` | Scan a VANC buffer for CEA-708 CDPs and SCTE-104 |
| `pipeline.py` | ffmpeg capture, tsp spliceinject, SRT, and HLS argv |
| `channels.py` | Saved channel config and the metrics snapshot it produces |
| `channel.py` | One-channel supervisor: capture, spliceinject, egress |
| `status.py` | ffmpeg progress file used for live bitrate and signal |
| `sideband.py` | Caption and splice notes for the monitor page |
| `crc.py` | CRC-32/MPEG-2 |

## Target hardware

- **Host:** Dell R440 or newer (realistic floor; R415 is too slow for two 1080i x264 encodes),
  or a Minisforum MS-01 (i5-12600H is enough for two channels; i9-13900H for headroom or four).
  The MS-01 has one half-height PCIe 4.0 x16 slot (x8 speed), so it needs a low-profile card.
- **Capture card (per owner, both have a reference input and support 1080i59.94):**
  DeckLink Duo 2 Mini (PCIe 2.0 x4, enough for two HD inputs) or DeckLink 8K Pro Mini
  (PCIe 3.0 x8, 12G-SDI, room for four channels or 4K later). Full-height cards do not fit the MS-01.
- **Driver:** Desktop Video 16 and matching SDK. Check its release notes against the Ubuntu 24.04
  kernel in use before deploying.
- **Not yet benchmarked.** CPU headroom figures are estimates until measured on the real unit.

## Status

- [x] Phase 0: repo scaffold, `setup.sh`
- [x] Phase 1: DeckLink capture argv (`yuv422p10`, one process per input, format codes) and VANC scan. Live card proof is still open.
- [x] Phase 2: parsers, translator, TS packetizer, injector with tests (2 TSDuck tests skip without `tstables`)
- [x] Phase 3: H.264/AAC, tsp SCTE-35 insert, SRT caller/listener, HLS push/pull, channel supervisor
- [x] Phase 4: MediaMTX preview (RTSP publish, WHEP on the monitor page) and caption/SCTE sideband. The operator console uses the NexApp theme.
- [x] Phase 5: `nexencoder-console`, `nexencoder-channel@`, and `mediamtx` units; `setup.sh` installs ffmpeg, GStreamer, Prometheus, and MediaMTX

Live proof still open: a DeckLink signal, a real VANC capture, TSDuck `tstables` on a generated section, and a CPU benchmark of two 1080i encodes.

## SCTE-104 injector

Generates a TS containing SCTE-35 sections from realistic SCTE-104 scenarios. Every message is
serialized, re-parsed, and translated by the production code, then re-extracted from the TS and
compared (non-zero exit on mismatch).

    python3 -m nexencoder.inject --scenario basic --fps 29.97 --out basic.ts --log basic.json
    tstables --pid 0x1F0 --xml-output - basic.ts      # TSDuck cross-check

Scenarios: `basic`, `immediate`, `cancel`, `heartbeat`, `mixed`. The generated sections were
decoded by an independent SCTE-35 library (threefive) and matched field for field (event ID,
PTS, duration, auto-return, CRC). TSDuck itself has not been run against them yet.

## Open verification items (need real captures)

- SCTE-104 `message_size` convention (assumed to include the leading 0xFFFF) and SMPTE 2010
  ANC payload-descriptor handling. Validate against the supplied captures in `captures/`.
- SCTE-104 operations beyond splice_request, splice_null and time_signal_request are
  surfaced as unknown, not translated (e.g. segmentation descriptors).
- 708 styling, delays, and Asian P16 characters are parsed and skipped, not rendered.

## Not in scope

SCTE-224 (ESNI). It is not carried in SDI.
