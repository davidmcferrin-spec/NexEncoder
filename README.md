# NexEncoder

Two-input HD-SDI to SRT/HLS encoder for a Dell R4xx server (R440 realistic minimum) with a
Blackmagic DeckLink Duo / Duo 2.

- Inputs: 2 channels, each SDI or an SRT listener (1080i / 1080p / 720p). SDI carries CEA-608/708 and SCTE-104
- Outputs per input: SRT (caller or listener) or HLS (push or pull), H.264 6-18 Mbps + AAC stereo
- Captions: 708 re-embedded as A/53 SEI; 608 carried inside the 708 CDP
- SCTE: 104 translated to SCTE-35 splice_info_section, muxed on a data PID
- Monitor: 25-50% proxy over WebRTC (MediaMTX) with caption and SCTE sideband over WebSocket

No Node. No Docker. Runs on the host under systemd (Ubuntu 24.04 LTS).

## Setup

    ./setup.sh

Ubuntu 24.04 LTS. Packages come from APT (no pip, no venv): Python, GStreamer,
Prometheus, and node_exporter. `setup.sh` downloads FFmpeg 9.0.2 and builds it into
`/usr/local` with DeckLink (`--enable-nonfree`), OpenSSL (`--enable-version3`), libx264,
and libopus. The build patches the DeckLink demuxer so caption CDPs and SCTE-104 ride a
localhost UDP feed to the channel supervisor, which inserts SCTE-35. `setup.sh` installs
the DeckLink SDK from `Blackmagic_DeckLink_SDK_*.zip` in `/home/nexstar` (or the home
directory of the user who ran sudo) into `/opt/decklink-sdk`, and installs the
`desktopvideo` package from `Blackmagic_Desktop_Video_Linux_*.tar.gz` in that same
place. Neither archive is downloaded. It also installs pinned TSDuck 3.45-4798
(the Ubuntu 24.04 package from the official release, skipped when `tsp` is already
on `PATH`), a pinned MediaMTX v1.21.1 binary, `nexencoder-console.service`, the
`nexencoder-channel@` template, `nexencoder-decklink-configure.service`, and
`nexencoder-noc.service`. `setup.sh` builds `decklink-configure` from the SDK
headers and, before the channels start, sets Duo 2 and 8K Pro connectors to
half-duplex inputs so connectors 1–4 are separate capture pins. It enables
and restarts Apache, Prometheus, node_exporter, MediaMTX, the console, the NOC agent,
and `nexencoder-channel@A` and `@B`. A crashed channel, console, or MediaMTX is restarted
with no start limit. A channel whose frame count stops moving is restarted by its supervisor.
A bad channel config exits 78 and stays stopped until the config is fixed.

## Tests

    python3 -m pytest -q

## Operator console

Apache and PHP serve the pages from `/var/lib/nexencoder/www` (copied from `web/` by
`setup.sh`) on port 80. The UI uses the NexApp theme kit and shell.
`python3 -m nexencoder.console` is the live feed on `127.0.0.1:8788`. The browser talks
only to Apache. PHP calls that feed for config, presets, the service list, and the live
notes, still, log, and metrics. Preview video is WebRTC from MediaMTX, not from Apache.

Raster, DeckLink device, bitrate, and SRT/HLS settings are stored by that config save
(restart the channel unit to apply them). Metrics charts CPU, memory, disk, GPU, and uptime
from Prometheus, plus every `nexencoder_*` series. Measured bitrate and resolution appear
after a channel's ffmpeg progress file is updating. Services can start, stop, restart,
enable, and disable the allowlisted units, including `mediamtx` and `nexencoder-channel@A` / `@B`.

The monitor page plays each input from MediaMTX at `http://<host>:8889/inputA/whep` and
`inputB`. The channel posts caption and splice notes to `/api/sideband` as VANC arrives.
`POST /api/channels/A/scte` translates a SCTE-104 message and sends the SCTE-35 section to
the channel's spliceinject port. spliceinject listens on `127.0.0.1:19101` (A)
and `:19102` (B).

The Effects page grabs a clean still of the selected input every 8 seconds, so a squeeze
and its CG layers can be lined up on the real picture and saved as a preset. Corner
positions are fractions of the frame, so a preset recalled on 720p, 1080i, or 1080p
lands on the same part of the picture.
`POST /api/channels/A/effects/recall` applies a preset on the running filter graph.
`POST /api/channels/A/effects/normalize` returns that input to full raster and clears the
CG. Neither call restarts the channel. CEA-608/708 stay on the primary frame as A/53 SEI,
and SCTE-104 still arrives as VANC datagrams and leaves as SCTE-35. A CG layer is a live HTML page (WPE, or Chromium capturing PNG with the
background omitted so transparency survives), an NDI source with alpha when
GStreamer `ndisrc` is installed, or the shared SDI fill/key pair on
DeckLink Duo (3) and (4). Each layer takes a level from −4 to +4; 0 is the video.
A channel holds at most four layers, and two layers cannot share a level.

Input A is DeckLink Duo (1) or an SRT listener on port 9001. Input B is DeckLink Duo (2)
or port 9002. Auto, beside each raster menu, reads the locked SDI mode or the connected
SRT caller and saves that raster when it is one of the six this encoder accepts. SDI Auto
needs the channel stopped, because the probe opens the sub-device. An unmatched picture
is reported and left unsaved. Restart the channel after Auto so capture uses the new raster. Output B's listener defaults to port 9101 so it does not take an input port.
Layer −5 is a waiting picture (a slate, a 10–30 second loop, or color bars) and stays on
the output when SDI unlocks or the SRT caller drops. The encode keeps running.
`POST /api/channels/A/bars` with `{"enabled": true}` puts SMPTE bars and an ident
(host address and input number) on layer +5 without restarting the channel. While bars
are the picture, the output audio is a 1 kHz tone. Captions and SCTE are not required
in that mode, and an SRT input has no VANC.

## NOC agent

NexNOC polls a separate process, `python3 -m nexencoder.noc`. It is not the operator
console. The pages are Apache on port 80, with no login. The live feed stays on
`127.0.0.1:8788`. The agent listens on
`0.0.0.0:8790` (override in `/etc/nexencoder/noc.json`) and requires a bearer token
from `/etc/nexencoder/noc.token`. The contract, example payloads, TLS, and the
install and token-rotation steps are in [docs/NOC-AGENT-API.md](docs/NOC-AGENT-API.md).

`setup.sh` creates the `nexencoder` system user, the token file (mode 640), a polkit
rule, and a sudoers drop-in that can start, stop, and restart only
`nexencoder-channel@A` and `@B`.
Re-run `./setup.sh` on an existing host to install the agent. It does not replace an
existing token or `noc.json`.

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
| `console.py` | Live JSON feed: `/metrics`, config, preview, Prometheus proxy, service control |
| `noc.py` | Token-authenticated NOC agent (`/api/noc/v1`) |
| `hoststats.py` | CPU, memory, disk, NIC, temperature, and GPU samples for the agent |
| `srtstats.py` | TSDuck SRT statistics probe and parser |
| `signal.py` | 1080i / 1080p / 720p rasters and DeckLink format codes |
| `decklink.py` | ffmpeg device-list and status JSON parsers |
| `vanc.py` | Scan a VANC buffer for CEA-708 CDPs and SCTE-104 |
| `vanc_feed.py` | VANC datagrams to SCTE-35 and monitor notes |
| `decklink_patch.py` | Insert the VANC forwarder into the FFmpeg 9.0.2 DeckLink demuxer |
| `decklink_install.py` | Local Desktop Video archive and DeckLink SDK headers for `setup.sh` |
| `pipeline.py` | ffmpeg capture, tsp spliceinject, SRT, and HLS argv |
| `livefeed.py` | SRT ingest, waiting-loop feeder, and the DeckLink signal flag |
| `effects.py` | Squeeze quads, CG layers, presets, recall and normalize |
| `cgfeed.py` | HTML and SDI fill/key feeders for the CG slots |
| `effect_patch.py` | Install the `nexeffect` filter into the FFmpeg 9.0.2 build |
| `channels.py` | Saved channel config and the metrics snapshot it produces |
| `channel.py` | One-channel supervisor: capture, spliceinject, egress |
| `status.py` | ffmpeg progress file used for live bitrate and signal |
| `sideband.py` | Caption and splice notes for the monitor page |
| `crc.py` | CRC-32/MPEG-2 |

## Target hardware

- **Host:** Dell R440 or newer (realistic floor; R415 is too slow for two 1080i x264 encodes),
  or a Minisforum MS-01. For encode only, the i5-12600H is enough for two channels.
  With the squeeze and HTML CG layers, use the i9-13900H and 64 GB. One or two
  simple HTML layers fit. Four animated HTML layers on both channels at once
  needs a larger host. The MS-01 has one half-height PCIe 4.0 x16 slot (x8 speed),
  so it needs a low-profile card. On a Duo 2 Mini the four connectors are two program
  inputs plus one shared fill/key pair. The card ships in full duplex, which hides
  connectors 3 and 4. `decklink-configure --apply-inputs` (run by
  `nexencoder-decklink-configure` before the channels open the card) switches each
  pin to an independent input.
- **Capture card (per owner, both have a reference input and support 1080i59.94):**
  DeckLink Duo 2 Mini (PCIe 2.0 x4, enough for two HD inputs) or DeckLink 8K Pro Mini
  (PCIe 3.0 x8, 12G-SDI, room for four channels or 4K later). Full-height cards do not fit the MS-01.
- **Driver:** Desktop Video 15 or 16 and the matching SDK, as
  `Blackmagic_Desktop_Video_Linux_*.tar.gz` and `Blackmagic_DeckLink_SDK_*.zip` in
  `/home/nexstar`. Check the release notes against the Ubuntu 24.04 kernel before deploying.
- **Not yet benchmarked.** CPU headroom figures are estimates until measured on the real unit.

## Status

- [x] Phase 0: repo scaffold, `setup.sh`
- [x] Phase 1: DeckLink capture argv (`yuv422p10`, one process per input, format codes) and VANC scan. Live card proof is still open.
- [x] DeckLink connectors: `decklink-configure` sets Duo 2 and 8K Pro pins to half-duplex inputs before the channels start.
- [x] Phase 2: parsers, translator, TS packetizer, injector with tests (2 TSDuck tests skip without `tstables`)
- [x] Phase 3: H.264/AAC, tsp SCTE-35 insert, SRT caller/listener, HLS push/pull, channel supervisor
- [x] Phase 4: MediaMTX preview (RTSP publish, WHEP on the monitor page) and caption/SCTE sideband. The operator console uses the NexApp theme.
- [x] Phase 5: `nexencoder-console`, `nexencoder-channel@`, and `mediamtx` units; `setup.sh` installs ffmpeg, GStreamer, Prometheus, and MediaMTX
- [x] Phase 6: four-corner squeeze, up to four CG layers (HTML or one SDI fill/key), effect builder, preset recall and normalize
- [x] Phase 7: SDI or SRT listener per input, waiting picture on layer −5, bars and ident on layer +5, fractional presets, 1080p
- [x] NOC agent: `/api/noc/v1` identity, host metrics, per-output status, and start/stop/restart. See `docs/NOC-AGENT-API.md`.

Live proof still open: a DeckLink signal, a real VANC capture, TSDuck `tstables` on a generated section, a CPU benchmark of two 1080i encodes, and a live recall of a squeeze plus an HTML layer on the MS-01.

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
