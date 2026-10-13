"""Patch the FFmpeg 9.0.2 DeckLink demuxer so one capture can forward VANC.

Stock FFmpeg turns CEA-708 into A/53 side data and ignores every other ANC
packet. SCTE-104 (DID 0x41 / SDID 0x07) never leaves the process, and the
DeckLink device cannot be opened a second time to read it. The patched
demuxer sends the caption CDP and the SCTE-104 user data to the channel
supervisor as UDP datagrams on 127.0.0.1.

The datagram is 12 bytes of header plus the ANC user data: kind (1 SCTE-104,
2 CDP), three zero bytes, then the video PTS scaled to 90 kHz as a big-endian
uint64. ``NEXENCODER_VANC_PORT`` selects the destination. When it is unset the
send is a no-op and capture behaves as upstream FFmpeg.
"""

from __future__ import annotations

from pathlib import Path

MARKER = "nexencoder_vanc_send"
ANCHOR = 'Unknown meta data DID = 0x%.2x SDID = 0x%.2x\\n'
CC_CALL = "data = vanc_to_cc(avctx, buf, width, data_len);"

INCLUDES = """\
#if defined(__linux__)
#include <arpa/inet.h>
#include <cstdlib>
#include <cstring>
#include <netinet/in.h>
#include <sys/socket.h>
#endif
"""

HELPER = r"""static void nexencoder_vanc_send(AVFormatContext *avctx, const AVPacket *pkt,
                                 uint8_t kind, const uint16_t *words, int count)
{
#if defined(__linux__)
    static int sock = -1;
    static int port = -2;
    if (port == -2) {
        const char *env = getenv("NEXENCODER_VANC_PORT");
        port = env ? atoi(env) : 0;
        if (port > 0 && port < 65536) {
            sock = socket(AF_INET, SOCK_DGRAM, 0);
            av_log(avctx, AV_LOG_INFO, "nexencoder VANC forwarder on port %d\n", port);
        } else {
            port = 0;
        }
    }
    if (port <= 0 || sock < 0 || count <= 0 || count > 4000 || words == nullptr)
        return;
    uint8_t frame[4012];
    uint64_t pts = 0;
    if (pkt && pkt->pts != AV_NOPTS_VALUE && pkt->pts >= 0 && pkt->stream_index >= 0 &&
        (unsigned)pkt->stream_index < avctx->nb_streams) {
        AVStream *st = avctx->streams[pkt->stream_index];
        pts = (uint64_t)av_rescale_q(pkt->pts, st->time_base, av_make_q(1, 90000));
    }
    frame[0] = kind;
    frame[1] = frame[2] = frame[3] = 0;
    for (int i = 0; i < 8; i++)
        frame[4 + i] = (uint8_t)((pts >> (8 * (7 - i))) & 0xff);
    for (int i = 0; i < count; i++)
        frame[12 + i] = (uint8_t)(words[i] & 0xff);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    addr.sin_addr.s_addr = htonl(0x7f000001);
    sendto(sock, frame, (size_t)(12 + count), 0, (struct sockaddr *)&addr, sizeof(addr));
#else
    (void)avctx;
    (void)pkt;
    (void)kind;
    (void)words;
    (void)count;
#endif
}

"""

SCTE_BRANCH = """\
} else if (did == 0x41 && sdid == 0x07) {
            if (check_vanc_parity_checksum(buf, len, buf[len - 1]) < 0) {
                av_log(avctx, AV_LOG_WARNING, "VANC parity or checksum incorrect\\n");
                goto skip_packet;
            }
            clear_parity_bits(buf, len);
            if (len > 7)
                nexencoder_vanc_send(avctx, pkt, 1, buf + 6, len - 7);
        } else {\
"""

CC_SEND = (
    "if (len > 7)\n"
    "                nexencoder_vanc_send(avctx, pkt, 2, buf + 6, len - 7);\n"
    "            " + CC_CALL
)


def patch_decklink_source(text: str) -> str:
    """Return the demuxer source with the VANC forwarder inserted.

    A source that already contains the marker is returned unchanged.
    """
    if MARKER in text:
        return text
    anchor = text.find(ANCHOR)
    if anchor < 0:
        raise ValueError("decklink_dec.cpp has no VANC metadata branch to patch")
    else_at = text.rfind("} else {", 0, anchor)
    if else_at < 0 or anchor - else_at > 400:
        raise ValueError("decklink_dec.cpp VANC else branch was not where the patch expects it")
    if CC_CALL not in text:
        raise ValueError("decklink_dec.cpp has no CEA-708 VANC decoder to hook")
    include_at = text.find('#include "decklink_dec.h"')
    helper_at = text.find("static uint8_t *get_metadata(")
    if include_at < 0 or helper_at < 0:
        raise ValueError("decklink_dec.cpp is missing the include or get_metadata anchor")
    include_end = text.find("\n", include_at)
    text = text[:include_end + 1] + INCLUDES + text[include_end + 1:]
    # Offsets after the include insertion.
    shift = len(INCLUDES)
    helper_at += shift
    else_at += shift
    text = text[:helper_at] + HELPER + text[helper_at:]
    shift = len(HELPER)
    else_at += shift
    text = text[:else_at] + SCTE_BRANCH + text[else_at + len("} else {"):]
    if text.count(CC_CALL) != 1:
        raise ValueError("decklink_dec.cpp CEA-708 call is not unique after the SCTE insert")
    text = text.replace(CC_CALL, CC_SEND, 1)
    if MARKER not in text:
        raise ValueError("VANC forwarder marker missing after patch")
    return text


def patch_file(path: str | Path) -> None:
    file = Path(path)
    original = file.read_text(encoding="utf-8")
    updated = patch_decklink_source(original)
    if updated != original:
        file.write_text(updated, encoding="utf-8")
