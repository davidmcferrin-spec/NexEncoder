# Vendor sidecar

A vendor ships a container image. NexEncoder stays on the host. The image runs beside one channel, reads that channel's program stream, and may insert SCTE-35 or captions. It does not sit between `ffmpeg` and `tsp`. If the container exits, the channel keeps running.

The operator adds the image on the Vendors page: a lowercase name, the image reference, channel A or B, and optional arguments that are passed to the image. Start runs `nexencoder-vendor@<name>`. That unit executes `docker run` on the host network and removes the container when it exits.

## What the container receives

The unit sets these environment variables. Ports are fixed per channel.

| Variable | Channel A | Channel B |
|---|---|---|
| `NEXENCODER_CHANNEL` | `A` | `B` |
| `NEXENCODER_CAPTION_PORT` | `19301` | `19302` |
| `NEXENCODER_SPLICE_PORT` | `19101` | `19102` |
| `NEXENCODER_FEED` | `239.255.1.1:19001` | `239.255.1.1:19002` |

`NEXENCODER_FEED` is the encoded program after graphics and after SCTE-35 insertion: one MPEG-TS, H.264 and AAC, multicast on the loopback interface. Join it with local address `127.0.0.1`. Video and audio are not split into separate streams.

Host networking is required. A bridge network cannot see `127.0.0.1` or that multicast group.

## SCTE-35

Send one SCTE-35 section per UDP datagram to `127.0.0.1` on `NEXENCODER_SPLICE_PORT`. The datagram is the section bytes only. `spliceinject` places them on PID `0x1F0` and repeats each section three times.

A splice that carries a PTS must use the program clock from `NEXENCODER_FEED`. An immediate splice does not. SDI VANC uses the same port, so event ids have to stay distinct from splices that arrive on the input.

This port is open whenever the channel is running. It is not gated by the caption source.

## Captions

Captions are written into the H.264 bitstream at the encode (`libx264` with `a53cc`). The `nexcaption` filter reads a file the channel updates from this socket and attaches the side data on the uncompressed frame. `setup.sh` rebuilds the pinned FFmpeg when that filter is missing. Send captions to `127.0.0.1` on `NEXENCODER_CAPTION_PORT`. Do not decode the program and re-encode it to add captions. That would replace the VANC captions and add a second generation.

The channel applies these datagrams only while its caption source is `vendor`. The default is `vanc`, which keeps SDI CEA-608/708 and ignores vendor packets. Starting a sidecar sets the source to `vendor`. Stopping the last sidecar for that channel sets it back to `vanc`. The Vendors page can switch a channel either way without restarting it.

The first byte of the datagram selects the payload:

| First byte | Rest of the datagram |
|---|---|
| `0` | Empty. Clear vendor captions and return to VANC immediately. |
| `1` | One UTF-8 line, printable ASCII, at most 32 characters. Encoded as CEA-608 roll-up (CC1, row 15) and attached on the next frame, then held as null padding. |
| `2` | One Caption Distribution Packet. Its cc_data triplets, including 708, are attached on every frame. |
| `3` | Raw A/53 cc_data triplets (three bytes each, at most 31). Attached on every frame. |

A CDP is the payload that keeps 708. A text line produces 608 only. The same line sent again refreshes the hold and does not roll the caption a second time.

If datagrams stop, the vendor track stays up for 8 seconds and then the encode returns to VANC side data. A clear datagram returns immediately.

```python
import os
import socket

port = int(os.environ["NEXENCODER_CAPTION_PORT"])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(b"\x01Hello captions", ("127.0.0.1", port))
```

## What not to do

Do not replace `tsp`, bind a DeckLink device, or publish on the program multicast address. Do not expect the NOC token. That token can restart a channel. The sidecar has the sockets above and nothing else on the control plane.

The operator console on port 80 has no login. Do not scrape it. The Vendors page is how an operator installs and stops the image.
