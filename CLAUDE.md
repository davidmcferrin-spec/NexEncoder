# NexEncoder: project notes for Claude

## Purpose
Two inputs, each SDI or an SRT listener (1080i / 1080p / 720p), on a Blackmagic DeckLink Duo or Duo 2,
hosted in a Dell R415/R430/R440/R450 (R440 is the realistic minimum). Each input is
encoded to H.264/AAC and delivered as SRT (caller or listener) or HLS (push or pull).
CEA-608/708 captions and SCTE-104 are carried through (708 as A/53 SEI, 104 translated
to SCTE-35) and shown on a low-latency WebRTC proxy preview via MediaMTX.

## Hard constraints (owner preferences)
- NO Node, Deno, Bun, npm, or any JS build toolchain. Browser JS is plain static files only.
- NO Docker or containers. Runs directly on the host (Ubuntu 24.04 LTS) under systemd.
- Root-level `setup.sh` installs everything with one command.
- SCTE-224 is out of scope.
- Production quality: error handling, logging, tests. No placeholders, no TODOs.

## Stack
- Capture: FFmpeg DeckLink, one process per input (`yuv422p10` so VANC becomes A/53 side data).
 `setup.sh` patches that demuxer to UDP the CEA-708 CDP and SCTE-104 user data to the channel
 supervisor. `nexencoder.vanc` scans those bytes; SCTE-104 is translated and sent to spliceinject.
- Encode/mux: FFmpeg libx264 (6–18 Mbps) and AAC, TSDuck `spliceinject` for SCTE-35, then
  `tsp` SRT or FFmpeg HLS. GStreamer is installed for the host; the live path is ffmpeg | tsp.
- Control plane: Apache and PHP serve the operator pages on port 80 (no login). `python3 -m nexencoder.console` is the live feed on `127.0.0.1:8788`. PHP calls that feed. Admin saves are PHP scripts. `python3 -m nexencoder.noc` is a separate bearer-token API for NexNOC (`/api/noc/v1`, contract in `docs/NOC-AGENT-API.md`). Caption and splice
  notes are posted to `/api/sideband` and the dashboard polls them. Config is saved through
  PHP and applied by restarting `nexencoder-channel@A` / `@B`. The dashboard can restart that unit. Input and output settings open from that page. `inputs.php`, `outputs.php`, and `monitor.php` redirect there. The Services page can copy the visible journal. Its support-bundle bar, separate from the unit list, downloads one redacted zip of every encoder service plus host details such as lsmod and dmesg, for 24, 36, 48, or 72 hours, or the whole journal. The NOC token is not included. Clear log on the selected unit hides older lines in that journal view. Clear All on that bar rotates and vacuums the host journal without downloading a zip. Services keep running. Operator actions are appended to the state directory audit log, that file is included in the support zip, and Clear All does not erase it. Effect recall, normalize,
  and bars-with-ident do not restart the channel. Input A is DeckLink Duo (1) or SRT
  listener port 9001; input B is DeckLink Duo (2) or port 9002. Output B listens on 9101.
  Auto, in each input's settings, reads the locked SDI mode from `decklink-status`, or the SRT caller's stream banner, and saves that raster.
  Layer −5 is the waiting picture. Layer +5 is bars plus an ident. A tone replaces audio
  while bars are the picture. Preset corners are fractions of the frame.
  Up to two CG layers each take a level from −4 to +4; 0 is the video. HTML is painted live by
  WPE or Chromium with alpha kept. Ubuntu 24.04 does not package WPE, so `setup.sh` installs Google Chrome for that path.
  Chromium runs under nice 10, gets 30 s to show a page, and keeps one profile per layer in `/var/lib/nexencoder/chrome/<channel>-<slot>`.
  `python3 -m nexencoder.cgfeed --probe URL` runs that renderer once outside the channel and reports the first frame. An NDI source can occupy a layer when ndisrc is installed.
  One SDI fill/key pair, DeckLink inputs 3 and 4, is shared. `setup.sh` builds
  `decklink-configure` and `nexencoder-decklink-configure` sets the card to
  half-duplex inputs before those units open it, so a factory full-duplex Duo 2
  exposes all four connectors. 608/708 stay A/53 side data on the
  primary frame. SCTE-104 still leaves the demuxer as UDP and returns as SCTE-35. SRT inputs have no VANC.
- Packages: APT only (Ubuntu 24.04 LTS), plus a pinned FFmpeg 9.0.2 built with DeckLink,
  pinned TSDuck 3.45-4798, and the pinned MediaMTX binary. No pip, no venv. TSDuck is
  downloaded from the official release when `tsp` is not already on PATH.
  Desktop Video and the DeckLink SDK are installed from archives in `/home/nexstar`, not downloaded. Channel units restart forever; a stalled frame count restarts the pipeline.
- Preview: MediaMTX. A separate ffmpeg decodes the program multicast feed and publishes RTSP to `127.0.0.1:8554`; the dashboard plays WHEP. That picture is the SRT output, including squeeze, graphics, and bars. The page can play the preview audio in the browser, with a mute button, volume slider, and horizontal level meters under each picture. The preview process restarts on its own and does not restart the channel.

## Hardware (see README "Target hardware")
- Host: Dell R440+ or Minisforum MS-01 (low-profile cards only). Card: DeckLink Duo 2 Mini or
  8K Pro Mini, Desktop Video 15 or 16. Both cards have a reference input and support 1080i59.94.

## Layout
- `nexencoder/` Python package (parsers, translators, control plane)
- `web/` operator console (Apache + PHP, NexApp theme; live data from `nexencoder.console`)
- `tests/` pytest suite
- `setup.sh` one-command install (added in this repo, root level)
- `captures/` real VANC fixtures supplied by the owner

## Status
See README.md "Status" for the current phase checklist. Keep both files updated.
