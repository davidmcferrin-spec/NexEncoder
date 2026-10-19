"""Patch the FFmpeg 9.0.2 DeckLink muxer so SDI playback carries captions and SCTE-104.

Stock FFmpeg writes VANC only when it is built with libklvanc, which this
project does not. The patched muxer always asks the card for VANC, builds a
CEA-708 CDP from A/53 side data, and attaches an SCTE-104 packet queued from
UDP. The word layout matches ``nexencoder.sdi_vanc``. ``NEXENCODER_SDI_SCTE_PORT``
selects the socket. When it is unset, captions still go out and the splice
queue stays empty.
"""

from __future__ import annotations

import re
from pathlib import Path

MARKER = "nexencoder_sdi_anc"
WARN = "Could not enable video output with VANC! Trying without..."
SCHEDULE = "ScheduleVideoFrame(frame,"
FUNCTION = "static int decklink_write_video_packet("

HELPER = r"""#if defined(__linux__)
#include <arpa/inet.h>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

/* nexencoder SDI ancillary. Words match nexencoder.sdi_vanc. */
static uint16_t nexencoder_anc_word(uint8_t value)
{
    uint8_t ones = 0;
    for (uint8_t bit = value; bit; bit >>= 1)
        ones ^= (uint8_t)(bit & 1);
    return (uint16_t)value | ((uint16_t)ones << 8) | ((uint16_t)(ones ^ 1) << 9);
}

static uint16_t nexencoder_checksum(const uint16_t *words, int count)
{
    unsigned total = 0;
    for (int i = 0; i < count; i++)
        total = (total + (words[i] & 0x1FF)) & 0x1FF;
    unsigned bit9 = (total & 0x100) ? 0u : 1u;
    return (uint16_t)(total | (bit9 << 9));
}

static int nexencoder_vanc_words(uint8_t did, uint8_t sdid, const uint8_t *payload, int length,
                                 uint16_t *out, int cap)
{
    if (length < 0 || length > 255 || cap < length + 7)
        return 0;
    int n = 0;
    out[n++] = 0x000;
    out[n++] = 0x3FF;
    out[n++] = 0x3FF;
    int body = n;
    out[n++] = nexencoder_anc_word(did);
    out[n++] = nexencoder_anc_word(sdid);
    out[n++] = nexencoder_anc_word((uint8_t)length);
    for (int i = 0; i < length; i++)
        out[n++] = nexencoder_anc_word(payload[i]);
    out[n] = nexencoder_checksum(out + body, n - body);
    return n + 1;
}

static void nexencoder_store_v210(uint8_t *dst, int width, const uint16_t *words, int count)
{
    int bytes = ((width + 5) / 6) * 16;
    if (bytes <= 0)
        return;
    for (int i = 0; i < bytes; i++)
        dst[i] = 0;
    int padded = count;
    while (padded % 3)
        padded++;
    for (int i = 0; i < padded; i += 3) {
        unsigned w0 = i < count ? (words[i] & 0x3FF) : 0;
        unsigned w1 = i + 1 < count ? (words[i + 1] & 0x3FF) : 0;
        unsigned w2 = i + 2 < count ? (words[i + 2] & 0x3FF) : 0;
        unsigned group = w0 | (w1 << 10) | (w2 << 20);
        int at = (i / 3) * 4;
        if (at + 4 > bytes)
            return;
        dst[at] = (uint8_t)group;
        dst[at + 1] = (uint8_t)(group >> 8);
        dst[at + 2] = (uint8_t)(group >> 16);
        dst[at + 3] = (uint8_t)(group >> 24);
    }
}

static int nexencoder_rate_code(int64_t rate_num, int64_t rate_den)
{
    struct { int code; int num; int den; } rates[] = {
        {1, 24000, 1001}, {2, 24, 1}, {3, 25, 1}, {4, 30000, 1001},
        {5, 30, 1}, {6, 50, 1}, {7, 60000, 1001}, {8, 60, 1},
    };
    if (rate_num <= 0 || rate_den <= 0)
        return 4;
    for (size_t i = 0; i < sizeof(rates) / sizeof(rates[0]); i++) {
        int64_t delta = rate_num * rates[i].den - rate_den * rates[i].num;
        if (delta < 0)
            delta = -delta;
        if (delta * 100 < rate_den * rates[i].num)
            return rates[i].code;
    }
    return 4;
}

static int nexencoder_cdp(const uint8_t *cc, int cc_bytes, int rate, uint16_t seq,
                          uint8_t *out, int cap)
{
    if (!cc || cc_bytes < 3 || cc_bytes % 3 || cc_bytes > 31 * 3)
        return 0;
    int count = cc_bytes / 3;
    int body = 4 + 2 + count * 3 + 3;
    int total = 3 + body + 1;
    if (total > cap || rate < 1 || rate > 8)
        return 0;
    int n = 0;
    out[n++] = 0x96;
    out[n++] = 0x69;
    out[n++] = (uint8_t)total;
    out[n++] = (uint8_t)((rate << 4) | 0x0F);
    out[n++] = 0x43;
    out[n++] = (uint8_t)(seq >> 8);
    out[n++] = (uint8_t)seq;
    out[n++] = 0x72;
    out[n++] = (uint8_t)(0xE0 | count);
    for (int i = 0; i < cc_bytes; i++)
        out[n++] = cc[i];
    out[n++] = 0x74;
    out[n++] = (uint8_t)(seq >> 8);
    out[n++] = (uint8_t)seq;
    unsigned sum = 0;
    for (int i = 0; i < n; i++)
        sum += out[i];
    out[n++] = (uint8_t)((-sum) & 0xFF);
    return n;
}

struct nexencoder_scte_msg {
    uint64_t pts;
    int length;
    uint8_t data[255];
};

static int nexencoder_scte_sock = -1;
static int nexencoder_scte_port = -2;
static struct nexencoder_scte_msg nexencoder_scte_q[16];
static int nexencoder_scte_count = 0;
static uint16_t nexencoder_cdp_seq = 0;

static void nexencoder_scte_bind(AVFormatContext *avctx)
{
#if defined(__linux__)
    if (nexencoder_scte_port != -2)
        return;
    const char *env = getenv("NEXENCODER_SDI_SCTE_PORT");
    nexencoder_scte_port = env ? atoi(env) : 0;
    if (nexencoder_scte_port <= 0 || nexencoder_scte_port >= 65536) {
        nexencoder_scte_port = 0;
        return;
    }
    nexencoder_scte_sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (nexencoder_scte_sock < 0) {
        nexencoder_scte_port = 0;
        return;
    }
    int one = 1;
    setsockopt(nexencoder_scte_sock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    fcntl(nexencoder_scte_sock, F_SETFL, O_NONBLOCK);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)nexencoder_scte_port);
    addr.sin_addr.s_addr = htonl(0x7f000001);
    if (bind(nexencoder_scte_sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        av_log(avctx, AV_LOG_WARNING, "nexencoder SDI ancillary could not bind port %d\n",
               nexencoder_scte_port);
        close(nexencoder_scte_sock);
        nexencoder_scte_sock = -1;
        nexencoder_scte_port = 0;
        return;
    }
    av_log(avctx, AV_LOG_INFO, "nexencoder SDI ancillary on port %d\n", nexencoder_scte_port);
#else
    (void)avctx;
    nexencoder_scte_port = 0;
#endif
}

static void nexencoder_scte_poll(void)
{
#if defined(__linux__)
    if (nexencoder_scte_port <= 0 || nexencoder_scte_sock < 0)
        return;
    uint8_t buf[528];
    for (;;) {
        ssize_t got = recv(nexencoder_scte_sock, buf, sizeof(buf), 0);
        if (got < 12)
            return;
        if (buf[0] != 1)
            continue;
        uint64_t pts = 0;
        for (int i = 0; i < 8; i++)
            pts = (pts << 8) | buf[4 + i];
        int length = (int)got - 12;
        if (length <= 0 || length > 255)
            continue;
        if (nexencoder_scte_count == 16) {
            memmove(&nexencoder_scte_q[0], &nexencoder_scte_q[1], 15 * sizeof(nexencoder_scte_q[0]));
            nexencoder_scte_count = 15;
        }
        struct nexencoder_scte_msg *msg = &nexencoder_scte_q[nexencoder_scte_count++];
        msg->pts = pts;
        msg->length = length;
        memcpy(msg->data, buf + 12, (size_t)length);
    }
#else
    return;
#endif
}

static const uint8_t *nexencoder_a53(AVPacket *pkt, enum AVCodecID codec, size_t *size)
{
    const uint8_t *data = av_packet_get_side_data(pkt, AV_PKT_DATA_A53_CC, size);
    if (data && *size)
        return data;
    if (codec == AV_CODEC_ID_WRAPPED_AVFRAME && pkt->data) {
        AVFrame *frame = (AVFrame *)pkt->data;
        AVFrameSideData *sd = av_frame_get_side_data(frame, AV_FRAME_DATA_A53_CC);
        if (sd && sd->data && sd->size) {
            *size = sd->size;
            return sd->data;
        }
    }
    *size = 0;
    return NULL;
}

static void nexencoder_sdi_anc(AVFormatContext *avctx, struct decklink_ctx *ctx, AVPacket *pkt,
                               decklink_frame *frame, AVStream *st)
{
    nexencoder_scte_bind(avctx);
    nexencoder_scte_poll();
    size_t cc_size = 0;
    const uint8_t *cc = nexencoder_a53(pkt, st->codecpar->codec_id, &cc_size);
    uint64_t frame_pts = 0;
    int have_pts = 0;
    if (pkt->pts != AV_NOPTS_VALUE) {
        frame_pts = (uint64_t)av_rescale_q(pkt->pts, st->time_base, av_make_q(1, 90000));
        have_pts = 1;
    }
    int write_cc = cc && cc_size >= 3;
    int scte_index = -1;
    for (int i = 0; i < nexencoder_scte_count; i++) {
        struct nexencoder_scte_msg *msg = &nexencoder_scte_q[i];
        int64_t delta = have_pts ? (int64_t)frame_pts - (int64_t)msg->pts : 0;
        int due = !have_pts || msg->pts == 0 || (delta >= 0 && delta < 90000LL * 2);
        int late = have_pts && delta >= 90000LL * 2;
        if (late) {
            memmove(&nexencoder_scte_q[i], &nexencoder_scte_q[i + 1],
                    (size_t)(nexencoder_scte_count - i - 1) * sizeof(nexencoder_scte_q[0]));
            nexencoder_scte_count--;
            i--;
            continue;
        }
        if (due && scte_index < 0)
            scte_index = i;
    }
    if (!write_cc && scte_index < 0)
        return;
    IDeckLinkVideoFrameAncillary *vanc = NULL;
    if (ctx->dlo->CreateAncillaryData(bmdFormat10BitYUV, &vanc) != S_OK || !vanc)
        return;
    int stored = 0;
    if (write_cc) {
        uint8_t cdp[200];
        int rate = nexencoder_rate_code(ctx->bmd_tb_den, ctx->bmd_tb_num);
        int n = nexencoder_cdp(cc, (int)cc_size, rate, nexencoder_cdp_seq, cdp, (int)sizeof(cdp));
        uint16_t words[300];
        int count = n ? nexencoder_vanc_words(0x61, 0x01, cdp, n, words, 300) : 0;
        void *buf = NULL;
        if (count && vanc->GetBufferForVerticalBlankingLine(11, &buf) == S_OK && buf) {
            nexencoder_store_v210((uint8_t *)buf, ctx->bmd_width, words, count);
            nexencoder_cdp_seq++;
            stored = 1;
        }
    }
    if (scte_index >= 0) {
        struct nexencoder_scte_msg *msg = &nexencoder_scte_q[scte_index];
        uint16_t words[600];
        int count = nexencoder_vanc_words(0x41, 0x07, msg->data, msg->length, words, 600);
        void *buf = NULL;
        if (count && vanc->GetBufferForVerticalBlankingLine(13, &buf) == S_OK && buf) {
            nexencoder_store_v210((uint8_t *)buf, ctx->bmd_width, words, count);
            stored = 1;
        }
        memmove(&nexencoder_scte_q[scte_index], &nexencoder_scte_q[scte_index + 1],
                (size_t)(nexencoder_scte_count - scte_index - 1) * sizeof(nexencoder_scte_q[0]));
        nexencoder_scte_count--;
    }
    if (stored)
        frame->SetAncillaryData(vanc);
    vanc->Release();
}

"""

ENABLE = """\
    if (ctx->dlo->EnableVideoOutput(ctx->bmd_mode, bmdVideoOutputVANC) != S_OK) {
        av_log(avctx, AV_LOG_WARNING, "Could not enable video output with VANC! Trying without...\\n");
        if (ctx->dlo->EnableVideoOutput(ctx->bmd_mode, bmdVideoOutputFlagDefault) != S_OK) {
            av_log(avctx, AV_LOG_ERROR, "Could not enable video output!\\n");
            return -1;
        }
    }
"""


def _replace_enable(text: str) -> str:
    warn_at = text.find(WARN)
    if warn_at < 0:
        raise ValueError("decklink_enc.cpp does not try VANC output")
    if_at = text.rfind("if (", 0, warn_at)
    err_at = text.find('Could not enable video output!\\n', warn_at)
    if if_at < 0 or err_at < 0:
        raise ValueError("decklink_enc.cpp VANC enable block was not where the patch expects it")
    ret_at = text.find("return -1;", err_at)
    brace = text.find("}", ret_at)
    end = text.find("\n", brace)
    if ret_at < 0 or brace < 0 or end < 0:
        raise ValueError("decklink_enc.cpp VANC enable block does not return")
    return text[:if_at] + ENABLE + text[end + 1:]


def _insert_call(text: str) -> str:
    at = text.find(SCHEDULE)
    if at < 0:
        raise ValueError("decklink_enc.cpp does not schedule a video frame")
    line = text.rfind("\n", 0, at) + 1
    indent = re.match(r"[ \t]*", text[line:]).group(0)
    call = f"{indent}nexencoder_sdi_anc(avctx, ctx, pkt, frame, st);\n"
    return text[:line] + call + text[line:]


def patch_decklink_muxer(text: str) -> str:
    """Return the muxer source with SDI caption and SCTE-104 insertion.

    A source that already contains the marker is returned unchanged.
    """
    if MARKER in text:
        return text
    fn_at = text.find(FUNCTION)
    if fn_at < 0:
        raise ValueError("decklink_enc.cpp has no video packet writer")
    text = _replace_enable(text)
    fn_at = text.find(FUNCTION)
    text = text[:fn_at] + HELPER + text[fn_at:]
    text = _insert_call(text)
    if MARKER not in text or "nexencoder SDI ancillary" not in text:
        raise ValueError("SDI ancillary marker missing after patch")
    return text


def patch_file(path: str | Path) -> None:
    file = Path(path)
    original = file.read_text(encoding="utf-8")
    updated = patch_decklink_muxer(original)
    if updated != original:
        file.write_text(updated, encoding="utf-8")
