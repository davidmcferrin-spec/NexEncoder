# NexEncoder NOC agent API

Contract for NexNOC. The agent is `python3 -m nexencoder.noc`. The operator console
(Apache and PHP on `http://127.0.0.1:8780/`, with `python3 -m nexencoder.console` as the
live feed on `127.0.0.1:8788`) is a different process, has no login, and is not this API.

API version: `v1`. Path prefix: `/api/noc/v1`. Software version is `identity.version`
and can change without an API version bump. New fields may be added to `v1` objects.
Removing a field or changing a field's type requires `/api/noc/v2`.

All bodies are JSON, UTF-8. Whitespace does not matter. Unknown query parameters are
ignored. A `token`, `access_token`, or `password` query parameter is rejected with
400 so the secret is not written to an access log.

## Pipeline

Each encoder has two outputs, `A` and `B`. Each output is one systemd service on the
shared CPU. There is no far-end trunk. Delivery is toward a CDN.

| Stage | Output A and B |
|---|---|
| Capture, SDI | FFmpeg DeckLink (`yuv422p10`), one process per output |
| Capture, SRT input | FFmpeg lavfi clock. A second FFmpeg process listens for the caller. The listener is not the CDN output. |
| Encode | FFmpeg `libx264` (high profile) and AAC 192 kbit/s stereo 48 kHz |
| SCTE-35 | TSDuck `tsp` `spliceinject` |
| SRT egress | TSDuck `tsp -O srt` (caller or listener, 1500 ms latency). Not FFmpeg. Not GStreamer. |
| HLS egress | FFmpeg `-f hls` copy. Push is HTTP PUT. Pull writes `/var/lib/nexencoder/hls/<id>/index.m3u8` with 2 second segments. |
| Preview | The encode process publishes RTSP to MediaMTX at `rtsp://127.0.0.1:8554/inputA` (and `inputB`). WHEP is on port 8889. |

GStreamer is installed for HTML CG and NDI layers. It is not on the program encode or
the CDN egress. `pipeline.gstreamer` is `false` on both outputs.

MediaMTX is not monitored. The agent does not call `systemctl` on `mediamtx.service`
and does not probe the WHEP port. It only reports the preview URL so NexNOC can open
a hover preview or a multiview.

SRT rendezvous is not a saved mode. `transport.mode` for SRT is `listener` or `caller`.

The local console still has no login. SRT listeners still bind all interfaces
(`srt://0.0.0.0:<port>` for an output listener, and `0.0.0.0:9001` / `:9002` for an
SRT input). Those sockets are the media path. This API is the authenticated control
path and does not change them.

## Authentication

Every `/api/noc/v1` route, including health, requires:

```
Authorization: Bearer <token>
```

The token is the first non-empty, non-comment line of the token file. The default
path is `/etc/nexencoder/noc.token`, outside the web tree and outside `web/`. The
compare hashes both strings with SHA-256 and then uses `hmac.compare_digest`, so a
wrong token of a different length does not return early. An empty token does not
match. The process refuses to start (exit 78, which systemd does not restart) unless
the token is 16 to 256 characters from `[A-Za-z0-9._~+/-]`.

`allow` in the config is an optional list of IP addresses and CIDR blocks. An empty
list allows every source address; the token is still required. A source that is not
on the list gets 403. The token is still compared, so a denied address is not a
faster path than a wrong token.

`trust_proxy` defaults to false. The logged and allowlisted address is the TCP peer.
Set `trust_proxy` to true only when the agent is bound to localhost behind a reverse
proxy. In that mode the client address is the first address in `X-Forwarded-For`.
A client that can reach the agent directly can otherwise put an allowlisted address
in that header.

Failed authentication is logged with the caller address and a reason
(`missing_token`, `bad_token`, `ip_denied`). The token is not logged.

## TLS

The agent speaks HTTP. Production should not leave that on the WAN in the clear.

Pick one:

1. Reverse proxy (preferred). Bind the agent to `127.0.0.1` (`"listen": "127.0.0.1"`)
   and terminate TLS on Caddy or nginx. Proxy `https://<encoder>:8790` to
   `http://127.0.0.1:8790`. Leave `trust_proxy` false unless the allowlist must see
   the original client, in which case bind only to localhost and set `trust_proxy`
   true. A Caddy snippet:

   ```
   enc-01.example:8790 {
       reverse_proxy 127.0.0.1:8790
   }
   ```

2. Built-in TLS. Set `tls_cert` and `tls_key` to PEM files readable by the
   `nexencoder` user. The process wraps the socket with TLS 1.2 or newer and exits
   78 if the files do not load. The preview URL scheme is separate (`preview_scheme`);
   MediaMTX stays on its own port.

If the process binds something other than loopback and no certificate is set, it
logs a warning and still serves. The token and the allowlist are then the only gate.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/noc/v1/health` | Cheap poll. No systemctl, GPU, or per-output read. |
| GET | `/api/noc/v1/status` | One combined document. This is the poll NexNOC should use for state. |
| POST | `/api/noc/v1/outputs/A/start` | Start output A. Also `stop` and `restart`. `B` is the other output. |
| POST | `/api/noc/v1/outputs/start` | Start both outputs. Also `stop` and `restart`. |
| POST | `/api/noc/v1/control` | Same actions with a JSON body. |

Control body:

```json
{"action": "restart", "outputs": ["A"]}
```

`outputs` may be `"all"`, `"A"`, `"B"`, or a list of those. Omitted means both.
Any other id, including `mediamtx` or the console unit, is 400 and does not run
`systemctl`.

Control returns 200 when every requested unit accepted the action, and 502 when any
call failed. The body still lists each output's resulting state. `systemctl start`
on an already-active unit is success.

Every control attempt is logged at info with the caller address, the action, the
output ids, and the result. That includes rejected ids.

Other errors:

| HTTP | `error` | When |
|---|---|---|
| 400 | `bad_request` | Bad JSON, bad action, unknown output, token in the query |
| 401 | `unauthorized` | Missing or wrong bearer token. `detail` is `missing_token` or `bad_token`. |
| 403 | `forbidden` | Source address is outside `allow` |
| 404 | `not_found` | Path is outside `/api/noc/v1` |
| 405 | `method_not_allowed` | Known path, wrong method |
| 502 | (control body) | `systemctl` failed. `outputs[].detail` has the stderr, truncated. |

```json
{"ok": false, "error": "unauthorized", "detail": "bad_token"}
```

## Health

```json
{
  "ok": true,
  "api_version": "v1",
  "encoder_id": "enc-studio-1",
  "hostname": "enc-host",
  "version": "0.1.0",
  "uptime_seconds": 500
}
```

`uptime_seconds` is host uptime from `/proc/uptime`, or null if that file cannot be
read. `encoder_id` is `encoder_id` from the config, or the hostname when that string
is empty.

## Status

`GET /api/noc/v1/status` is the combined poll. `ok` means the document was built.
An output that is down is still `ok: true`; look at `outputs[].failed` and
`encoding`.

Preview host is `preview_host` from the config when that is set. Otherwise it is the
request `Host` header, then the hostname. Only a hostname or an IP address is
accepted; a `Host` value with an extra port is ignored. An IPv6 address is written
with brackets. The WHEP port defaults to 8889, which is MediaMTX `webrtcAddress` in
`config/mediamtx.yml`.

```json
{
  "ok": true,
  "api_version": "v1",
  "collected_at": "2026-10-17T17:50:00Z",
  "identity": {
    "encoder_id": "enc-studio-1",
    "hostname": "enc-host",
    "model": "NexEncoder",
    "version": "0.1.0",
    "site": "Studio A",
    "location": "Rack 4",
    "uptime_seconds": 500
  },
  "host": {
    "cpu_percent": 22.5,
    "load": {"1m": 0.2, "5m": 0.2, "15m": 0.2},
    "memory": {
      "total_bytes": 68719476736,
      "available_bytes": 34359738368,
      "used_percent": 50.0
    },
    "temperatures_c": [
      {"name": "x86_pkg_temp", "celsius": 48.0, "source": "thermal"}
    ],
    "disks": [
      {
        "mount": "/",
        "fstype": "ext4",
        "total_bytes": 1000000000000,
        "used_bytes": 200000000000,
        "available_bytes": 800000000000,
        "used_percent": 20.0
      }
    ],
    "nics": [
      {"name": "eno1", "rx_bps": 12000000, "tx_bps": 8400000}
    ],
    "gpu": {
      "available": true,
      "source": "nvidia-smi",
      "devices": [
        {
          "index": 0,
          "name": "NVIDIA RTX A2000",
          "vendor": "nvidia",
          "utilization_percent": 12.0,
          "encoder_percent": 34.0,
          "decoder_percent": 1.0,
          "memory_used_bytes": 536870912,
          "memory_total_bytes": 8589934592,
          "temperature_c": 55.0
        }
      ],
      "detail": ""
    },
    "uptime_seconds": 500
  },
  "outputs": [
    {
      "id": "A",
      "unit": "nexencoder-channel@A.service",
      "active_state": "active",
      "sub_state": "running",
      "failed": false,
      "encoding": true,
      "connected": true,
      "connected_evidence": "srt-stats",
      "uptime_seconds": 3600,
      "service_restarts": 2,
      "pipeline_runs": 2,
      "last_error": "pipeline stalled at frame 9",
      "last_error_unix": 1780000000.0,
      "input": {
        "id": "A",
        "source": "sdi",
        "format": "1080i59.94",
        "device": "DeckLink Duo 2 (1)",
        "srt_port": null
      },
      "video": {
        "codec": "h264",
        "encoder": "libx264",
        "profile": "high",
        "bitrate_configured_bps": 8000000,
        "bitrate_measured_bps": 8100500.0,
        "width": 1920,
        "height": 1080,
        "format": "1080i59.94",
        "scan": "interlaced",
        "field_rate_hz": 59.94,
        "fps_nominal": 29.97,
        "fps_measured": 29.97,
        "dropped_frames": 2,
        "dup_frames": 1
      },
      "audio": {
        "codec": "aac",
        "bitrate_configured_bps": 192000,
        "channels": 2,
        "sample_rate_hz": 48000,
        "levels_dbfs": null,
        "levels_available": false
      },
      "pipeline": {
        "capture": "ffmpeg-decklink",
        "encode": "ffmpeg-libx264",
        "audio_encode": "ffmpeg-aac",
        "scte35": "tsduck-spliceinject",
        "egress": "tsduck-srt",
        "preview": "ffmpeg-rtsp-mediamtx",
        "gstreamer": false
      },
      "preview": {
        "path": "inputA",
        "whep": "http://enc.example:8889/inputA/whep"
      },
      "transport": {
        "protocol": "srt",
        "mode": "listener",
        "config_mode": "srt-listener",
        "destination": "srt://0.0.0.0:9000?mode=listener",
        "latency_ms": 1500,
        "segment_seconds": null,
        "srt": {
          "available": true,
          "fresh": true,
          "source": "tsduck",
          "age_seconds": 1.2,
          "rtt_ms": 12.5,
          "packets_lost": 3,
          "packets_lost_interval": 0,
          "retransmits": 4,
          "retransmits_interval": 1,
          "packets_dropped": 1,
          "bandwidth_bps": 8200000
        }
      }
    },
    {
      "id": "B",
      "unit": "nexencoder-channel@B.service",
      "active_state": "inactive",
      "sub_state": "dead",
      "failed": false,
      "encoding": false,
      "connected": false,
      "connected_evidence": "pipeline-down",
      "uptime_seconds": null,
      "service_restarts": 0,
      "pipeline_runs": null,
      "last_error": "",
      "last_error_unix": null,
      "input": {
        "id": "B",
        "source": "sdi",
        "format": "1080i59.94",
        "device": "DeckLink Duo 2 (2)",
        "srt_port": null
      },
      "video": {
        "codec": "h264",
        "encoder": "libx264",
        "profile": "high",
        "bitrate_configured_bps": 10000000,
        "bitrate_measured_bps": null,
        "width": 1920,
        "height": 1080,
        "format": "1080i59.94",
        "scan": "interlaced",
        "field_rate_hz": 59.94,
        "fps_nominal": 29.97,
        "fps_measured": null,
        "dropped_frames": null,
        "dup_frames": null
      },
      "audio": {
        "codec": "aac",
        "bitrate_configured_bps": 192000,
        "channels": 2,
        "sample_rate_hz": 48000,
        "levels_dbfs": null,
        "levels_available": false
      },
      "pipeline": {
        "capture": "ffmpeg-decklink",
        "encode": "ffmpeg-libx264",
        "audio_encode": "ffmpeg-aac",
        "scte35": "tsduck-spliceinject",
        "egress": "ffmpeg-hls",
        "preview": "ffmpeg-rtsp-mediamtx",
        "gstreamer": false
      },
      "preview": {
        "path": "inputB",
        "whep": "http://enc.example:8889/inputB/whep"
      },
      "transport": {
        "protocol": "hls",
        "mode": "push",
        "config_mode": "hls-push",
        "destination": "https://user:REDACTED@cdn.example/live/b.m3u8?token=REDACTED&list=1",
        "latency_ms": null,
        "segment_seconds": 2,
        "srt": null
      }
    }
  ],
  "limitations": [
    "audio_levels_unavailable",
    "hls_push_connected_is_process"
  ]
}
```

The memory and disk byte counts in that example are illustrative. The field names and
types are what the process returns. A host with no GPU and no thermal zones reports
`gpu.available: false` and adds `gpu_unavailable` and `temperatures_unavailable` to
`limitations` instead of inventing zeros.

### Identity

| Field | Meaning |
|---|---|
| `encoder_id` | Configured label, else hostname. Stable for the life of the config. |
| `hostname` | `socket.gethostname()` |
| `model` | `NexEncoder` |
| `version` | Package version |
| `site`, `location` | Free-text labels from `noc.json`. Empty string when unset. Newlines are flattened. |
| `uptime_seconds` | Host uptime, seconds |

### Host

| Field | Meaning |
|---|---|
| `cpu_percent` | Busy percent since the previous sample in this process. Null on the first status poll and when two polls are under 200 ms apart. |
| `load` | 1, 5, and 15 minute load averages. |
| `memory` | From `/proc/meminfo`. `used_percent` is `1 - available/total`. |
| `temperatures_c` | Thermal zones and hwmon inputs, plus a GPU temperature when the GPU tool reports one. Readings at 0 C or at 150 C and above are dropped. |
| `disks` | Real mounts. tmpfs, overlay, snap, and the usual virtual filesystems are skipped. |
| `nics` | `rx_bps` and `tx_bps` are bits per second since the previous sample. `lo` is omitted. Null on the first poll. |
| `gpu` | See below. |
| `uptime_seconds` | Same host uptime as identity. |

GPU order: `nvidia-smi` (utilization, encoder, decoder, memory, temperature), then
`intel_gpu_top -J` (render busy, Video engine as decoder, VideoEnhance as encoder),
then a DRM `gpu_busy_percent` file (utilization only). Vendor is taken from the PCI
id when the source is sysfs (`nvidia`, `intel`, `amd`, or `drm`). `vainfo` does not
report utilization and is not called. The GPU sample is cached for 5 seconds.
`intel_gpu_top` is allowed about 1.5 seconds and then stopped.

### Output

`id` is `A` or `B` and does not change. `unit` is `nexencoder-channel@A.service` or
`nexencoder-channel@B.service`.

| Field | Meaning |
|---|---|
| `active_state`, `sub_state` | `systemctl show`. `unknown` when systemctl is missing. |
| `failed` | Active state is `failed`, or the unit result is an error result while it is not active. |
| `encoding` | The unit is active and the FFmpeg progress file is under 8 seconds old with a frame count above zero. If systemctl cannot be read, the progress file alone decides. |
| `connected` | See evidence below. `null` means the agent could not tell. |
| `uptime_seconds` | Seconds since systemd `ActiveEnterTimestamp` while the unit is active. Null when the unit is not active or the timestamp did not parse. |
| `service_restarts` | systemd `NRestarts` for the unit. |
| `pipeline_runs` | How many times this process of the supervisor has spawned the encode pipeline. 1 means it has not restarted the pipeline internally. Null if the file has not been written. A systemd restart of the unit starts the count over at 1 and increments `service_restarts`. |
| `last_error` | Latest supervisor fault (stall, exec failure, child exit). Kept after recovery. Empty when there has been none. |
| `last_error_unix` | When that fault was recorded. |

`connected_evidence`:

| Value | `connected` | Meaning |
|---|---|---|
| `srt-stats` | true | A TSDuck stats report younger than 30 seconds. Stats are only emitted while packets flow. |
| `udp-socket` | true | `/proc/net/udp` shows an established UDP socket on the listener port, or toward the caller port. |
| `no-peer` | false | Those files were readable and no peer socket was found. A listener with nobody calling looks like this. |
| `unknown` | null | SRT, and neither stats nor the UDP table could be read. |
| `hls-playlist` | true | Pull mode and `index.m3u8` was rewritten in the last 15 seconds. |
| `hls-playlist-stale` | false | The playlist exists but is older than that. |
| `hls-playlist-missing` | false | Pull mode and the playlist is not there. |
| `hls-push-process` | true | Push mode and the encode pipeline is up. FFmpeg exits when the PUT target fails, which restarts the pipeline, so a steady encode is the signal. There is no CDN acknowledgement. |
| `pipeline-down` | false | Push mode and the encode is not running. |

`video.bitrate_measured_bps` is the FFmpeg progress bitrate (multiplex, so audio and
container are included), not a pure elementary video rate. `bitrate_configured_bps`
is the configured video rate (6 to 18 Mbit/s). `fps_nominal` for 1080i is the frame
rate (29.97 or 25), not the field rate. `field_rate_hz` carries the field rate.
Dropped and duplicated frames come from `drop_frames` and `dup_frames` in that same
progress file, and are null when the file is stale.

`audio.levels_dbfs` is always null. The encode progress file has no levels, and the
agent does not tap the audio graph. NexNOC should meter the WHEP preview in the browser.

`input.srt_port` is 9001 for A and 9002 for B when the input source is `srt`, and
null for SDI. That port is the contribution listener, not the CDN output.

`transport.destination` is the CDN target with secrets removed: userinfo passwords
become `REDACTED`, and query keys `token`, `key`, `secret`, `password`, `pass`,
`signature`, `sig`, `auth`, `access_token`, `passphrase`, `api_key`, and `apikey`
do too. The host and path stay. SRT caller destinations are `srt://<host>:<port>?mode=caller`.
Listener destinations are `srt://0.0.0.0:<port>?mode=listener`. Pull destinations are
the local playlist path. The encoder config has no SRT passphrase field.

`transport.latency_ms` is 1500 for SRT and null for HLS. `segment_seconds` is 2 for
HLS and null for SRT.

`transport.srt` is null when the output is HLS. For SRT, numbers older than 30
seconds are cleared and `fresh` is false. `packets_lost` and `retransmits` are
cumulative send counters. The `*_interval` fields are the last reporting interval.
`bandwidth_bps` is the send rate from TSDuck's `rate-mbps` (megabits per second
converted to bits per second). The plain-text TSDuck log has no rate, so
`bandwidth_bps` stays null if only that text was seen.

`limitations` is a list of stable tokens. NexNOC can show them; it should not treat
an absent token as a failure.

| Token | Meaning |
|---|---|
| `cpu_percent_needs_two_samples` | First status poll in this process, or the polls were under 200 ms apart. |
| `cpu_unavailable` | `/proc/stat` could not be read. |
| `nic_throughput_needs_two_samples` | Same two-sample rule for NIC rates. |
| `nic_unavailable` | `/proc/net/dev` could not be read. |
| `memory_unavailable` | `/proc/meminfo` had no `MemTotal`. |
| `load_unavailable` | `/proc/loadavg` could not be read. |
| `disk_unavailable` | No real mount could be measured. |
| `uptime_unavailable` | `/proc/uptime` could not be read. |
| `gpu_unavailable` | No nvidia-smi sample, no intel_gpu_top sample, and no DRM busy file. |
| `gpu_encoder_decoder_unavailable` | A GPU was found but neither encoder nor decoder percent was reported. |
| `temperatures_unavailable` | No thermal zone, hwmon, or GPU temperature. |
| `audio_levels_unavailable` | Always present. Levels are not measured on the encode. |
| `srt_stats_unavailable` | At least one SRT output has no fresh TSDuck report. |
| `hls_push_connected_is_process` | At least one output is HLS push. `connected` means the pipeline is up. |
| `systemctl_unavailable` | `systemctl show` failed for an output. |
| `output_uptime_unavailable` | An active unit had no parseable start timestamp. |
| `host_metrics_failed` | The host sampler raised. Outputs are still filled in. |

## Control response

`POST /api/noc/v1/outputs/A/restart` with a valid token:

```json
{
  "ok": true,
  "api_version": "v1",
  "action": "restart",
  "caller": "203.0.113.8",
  "outputs": [
    {
      "id": "A",
      "unit": "nexencoder-channel@A.service",
      "ok": true,
      "detail": "",
      "active_state": "active",
      "sub_state": "running",
      "failed": false,
      "service_restarts": 3,
      "uptime_seconds": 1
    }
  ]
}
```

`caller` is the address the agent used for the allowlist, so NexNOC can confirm which
source was seen. Both-output actions return two entries, A then B, even when one of
them failed.

The agent runs `systemctl start|stop|restart` on those two unit names only. `show`
does not need root. The unit runs as user `nexencoder`. On Ubuntu 24.04, polkit rule
`/etc/polkit-1/rules.d/50-nexencoder-noc.rules` allows that user to start, stop, and
restart only `nexencoder-channel@A` and `@B`. Enable, disable, and every other unit
are not granted.

If polkit is not enforcing, set `"use_sudo": true` in `noc.json` and restart the
agent. It then runs `sudo -n systemctl <action> <unit>`. The sudoers file
`/etc/sudoers.d/nexencoder-noc` allows those six commands and nothing else. The
service unit does not set `NoNewPrivileges`, because that would make the sudo
fallback fail.

## Config file

`/etc/nexencoder/noc.json` (template: `config/noc.json`):

| Key | Default | Meaning |
|---|---|---|
| `encoder_id` | empty | Identity label. Empty uses the hostname. |
| `site`, `location` | empty | Free-text labels. |
| `listen` | `0.0.0.0` | Bind address. Use `127.0.0.1` behind a proxy. |
| `port` | `8790` | Bind port. |
| `allow` | `[]` | IP or CIDR allowlist. Empty allows any address that has the token. |
| `preview_host` | empty | Host placed in WHEP URLs. Empty uses the request Host header. |
| `preview_port` | `8889` | MediaMTX WebRTC port. |
| `preview_scheme` | `http` | `http` or `https`. Independent of the agent's own TLS. |
| `tls_cert`, `tls_key` | empty | PEM paths for built-in TLS. Set both or neither. |
| `token_file` | `/etc/nexencoder/noc.token` | Bearer token. Must not live under `web/`. |
| `use_sudo` | false | Use the sudoers fallback for control. |
| `trust_proxy` | false | Trust the first `X-Forwarded-For` address. |
| `hls_root` | `/var/lib/nexencoder/hls` | Where pull playlists are written. |
| `encoder_config` | `/var/lib/nexencoder/config.json` | Channel config the console saves. |
| `state_dir` | `/var/lib/nexencoder/state` | Progress, SRT stats, run count, last error. |

A missing file, or a key of the wrong type, falls back to the default for that key.
Bad allowlist entries are skipped and logged.

Channel config (`encoder.json`) is still the console's file. Output mode is
`srt-listener`, `srt-caller`, `hls-push`, or `hls-pull`. Bitrate is the video rate
in Mbit/s.

## Install and update

On an Ubuntu 24.04 host that already has the repo, re-run:

```
./setup.sh
```

That is idempotent. It creates the `nexencoder` system user if needed, adds the user
to `systemd-journal`, `video`, and `render` when those groups exist, installs the
unit, the polkit rule, and the sudoers file, and enables `nexencoder-noc`. The video
and render groups are what let `nvidia-smi`, `intel_gpu_top`, and the DRM busy file
be read without root; a missing group is skipped and GPU fields stay empty. It
writes `/etc/nexencoder/noc.json` only when that file is absent. It generates
`/etc/nexencoder/noc.token` only when that file is absent or empty, mode 640, owner
`root`, group `nexencoder`. A re-run does not rotate a token that is already there.
A re-run also refreshes `nexencoder-channel@.service`, which sets `UMask=0022` so
progress and SRT stats stay mode 644 and this user can read them. The channel
journal redacts URL userinfo passwords and query secrets before it logs an argv
or a tsp stderr line.

The package has to be readable by `nexencoder`. `setup.sh` adds read and execute
permission on the `nexencoder/` tree and warns if `sudo -u nexencoder python3 -c
'import nexencoder'` fails. Parent directories of the checkout must be executable by
that user as well.

Manual equivalent, from the repo root:

```
sudo useradd --system --home-dir /var/lib/nexencoder --shell /usr/sbin/nologin --comment "NexEncoder NOC agent" nexencoder || true
sudo usermod -aG systemd-journal nexencoder || true
sudo usermod -aG video nexencoder || true
sudo usermod -aG render nexencoder || true
sudo mkdir -p /etc/nexencoder /var/lib/nexencoder/hls /var/lib/nexencoder/state /etc/polkit-1/rules.d
sudo chmod 0755 /var/lib/nexencoder /var/lib/nexencoder/hls /var/lib/nexencoder/state
sudo cp config/noc.json /etc/nexencoder/noc.json
python3 -c 'import secrets; print(secrets.token_hex(32))' | sudo tee /etc/nexencoder/noc.token >/dev/null
sudo chown root:nexencoder /etc/nexencoder/noc.token
sudo chmod 0640 /etc/nexencoder/noc.token
sudo cp config/nexencoder-noc.rules /etc/polkit-1/rules.d/50-nexencoder-noc.rules
sudo visudo -cf config/sudoers-nexencoder-noc
sudo cp config/sudoers-nexencoder-noc /etc/sudoers.d/nexencoder-noc
sudo chmod 0440 /etc/sudoers.d/nexencoder-noc
sudo cp systemd/nexencoder-noc.service /etc/systemd/system/nexencoder-noc.service
```

Replace `@ROOT@` in the unit with the repo path, then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now nexencoder-noc
```

Edit `/etc/nexencoder/noc.json` and set `encoder_id`, `site`, `location`, and `allow`
to the NexNOC hosts. Restart the agent after that edit.

Rotate the token without printing it into a log:

```
python3 -c 'import secrets; print(secrets.token_hex(32))' | sudo tee /etc/nexencoder/noc.token >/dev/null
sudo chown root:nexencoder /etc/nexencoder/noc.token
sudo chmod 0640 /etc/nexencoder/noc.token
sudo systemctl restart nexencoder-noc
```

Give the new value to NexNOC out of band. A world-readable token file logs a warning
at start and still serves.

Check:

```
curl -sS -H "Authorization: Bearer $(sudo cat /etc/nexencoder/noc.token)" \
  http://127.0.0.1:8790/api/noc/v1/health
```

Channel units are unchanged. Start them the same way as before:

```
sudo systemctl start nexencoder-channel@A nexencoder-channel@B
```

## What the agent cannot see

- Audio levels. Use the browser meter on the WHEP preview.
- A GPU encoder or decoder percent when the only signal is DRM `gpu_busy_percent`.
  VAAPI (`vainfo`) does not report load.
- SRT RTT, loss, retransmits, and bandwidth until TSDuck's `srt` plugin accepts
  `--statistics-interval` and `--json-line`. The supervisor checks `tsp -O srt --help`
  and omits the flags on older TSDuck so a missing option cannot take the output
  down. Until a report arrives, `transport.srt.fresh` is false.
- SRT bandwidth from the human-readable TSDuck log. That text has no send rate.
- Whether an HLS push origin accepted the last segment, beyond FFmpeg staying up.
- Whether any CDN client is pulling an HLS playlist. `connected` for pull means the
  local playlist is being rewritten.
- MediaMTX health. The preview URL is built from config. A dead MediaMTX still
  shows a WHEP URL.
- Per-PID bitrate, audio loudness, or caption presence. Those stay on the local
  console and on Prometheus.
