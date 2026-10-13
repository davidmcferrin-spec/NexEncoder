# NexEncoder: project notes for Claude

## Purpose
Two independent HD-SDI inputs (1080i / 720p) on a Blackmagic DeckLink Duo or Duo 2,
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
  `nexencoder.vanc` scans ANC bytes for CEA-708 CDPs and SCTE-104. A DeckLink SDK process can
  replace the ffmpeg input later; the argv and the scanner are what run now.
- Encode/mux: FFmpeg libx264 (6–18 Mbps) and AAC, TSDuck `spliceinject` for SCTE-35, then
  `tsp` SRT or FFmpeg HLS. GStreamer is installed for the host; the live path is ffmpeg | tsp.
- Control plane: `python3 -m nexencoder.console` (stdlib HTTP, vanilla JS). Caption and splice
  notes are posted to `/api/sideband` and the monitor page polls them. Config is saved by the
  console and applied by restarting `nexencoder-channel@A` / `@B`.
- Packages: APT only (Ubuntu 24.04 LTS), plus a pinned FFmpeg 9.0.2 built with DeckLink
  and the pinned MediaMTX binary. No pip, no venv. TSDuck and Desktop Video are checked
  for, not downloaded. Channel units restart forever; a stalled frame count restarts the pipeline.
- Preview: MediaMTX. ffmpeg publishes RTSP to `127.0.0.1:8554`; the monitor page plays WHEP.

## Hardware (see README "Target hardware")
- Host: Dell R440+ or Minisforum MS-01 (low-profile cards only). Card: DeckLink Duo 2 Mini or
  8K Pro Mini, Desktop Video 16. Both cards have a reference input and support 1080i59.94.

## Layout
- `nexencoder/` Python package (parsers, translators, control plane)
- `web/` operator console (NexApp theme; Metrics and Services talk to `nexencoder.console`)
- `tests/` pytest suite
- `setup.sh` one-command install (added in this repo, root level)
- `captures/` real VANC fixtures supplied by the owner

## Status
See README.md "Status" for the current phase checklist. Keep both files updated.
