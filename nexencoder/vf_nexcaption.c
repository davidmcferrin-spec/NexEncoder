/*
 * NexEncoder vendor caption slot for the pinned FFmpeg 9.0.2 build.
 *
 * The filter reads an NXCC file written by nexencoder.caption_feed and, while
 * that file is active, replaces AV_FRAME_DATA_A53_CC on the program frame.
 * Idle contents leave whatever side data nexeffect copied from VANC. Mode
 * "every" attaches the triplets on each frame (a CDP or a raw cc_data
 * stream). Mode "once" attaches them on the frame where the sequence
 * changes, then sends one CEA-608 null triplet so a roll-up command is not
 * repeated and VANC does not mix into the vendor track.
 *
 * nexencoder caption slot
 */

#include <stdio.h>
#include <string.h>

#include "libavutil/frame.h"
#include "libavutil/mem.h"
#include "libavutil/opt.h"
#include "avfilter.h"
#include "filters.h"
#include "video.h"

#define NXCC_HEADER 12
#define NXCC_MAX 93

typedef struct NexCaptionContext {
    const AVClass *class;
    char *path;
    uint32_t last_seq;
    int have_seq;
    int padding;
} NexCaptionContext;

static uint32_t nxcc_be32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static void nxcc_attach(AVFrame *frame, const uint8_t *data, int size)
{
    AVFrameSideData *sd;

    av_frame_remove_side_data(frame, AV_FRAME_DATA_A53_CC);
    sd = av_frame_new_side_data(frame, AV_FRAME_DATA_A53_CC, size);
    if (sd)
        memcpy(sd->data, data, (size_t)size);
}

static int nxcc_read(NexCaptionContext *s, uint8_t *mode, uint32_t *seq,
                     uint8_t *data, int *size)
{
    FILE *file;
    uint8_t header[NXCC_HEADER];
    unsigned len;

    *mode = 0;
    *seq = 0;
    *size = 0;
    if (!s->path || !s->path[0])
        return 0;
    file = fopen(s->path, "rb");
    if (!file)
        return 0;
    if (fread(header, 1, NXCC_HEADER, file) != NXCC_HEADER) {
        fclose(file);
        return 0;
    }
    if (memcmp(header, "NXCC", 4) != 0 || header[4] != 1) {
        fclose(file);
        return 0;
    }
    len = ((unsigned)header[10] << 8) | header[11];
    if (header[5] > 2 || (len % 3) != 0 || len > NXCC_MAX) {
        fclose(file);
        return 0;
    }
    if (len && fread(data, 1, len, file) != len) {
        fclose(file);
        return 0;
    }
    fclose(file);
    *mode = header[5];
    *seq = nxcc_be32(header + 6);
    *size = (int)len;
    return 1;
}

static int filter_frame(AVFilterLink *inlink, AVFrame *frame)
{
    AVFilterContext *ctx = inlink->dst;
    NexCaptionContext *s = ctx->priv;
    uint8_t mode = 0, data[NXCC_MAX];
    uint32_t seq = 0;
    int size = 0;
    static const uint8_t null_cc[3] = { 0xFC, 0x80, 0x80 };
    const uint8_t *attach = NULL;
    int attach_size = 0;
    int ret;

    if (!nxcc_read(s, &mode, &seq, data, &size) || mode == 0) {
        s->padding = 0;
        return ff_filter_frame(inlink->dst->outputs[0], frame);
    }
    if (mode == 1 && size > 0) {
        attach = data;
        attach_size = size;
        s->have_seq = 1;
        s->last_seq = seq;
        s->padding = 0;
    } else if (mode == 2 && size > 0) {
        if (!s->have_seq || seq != s->last_seq) {
            attach = data;
            attach_size = size;
            s->have_seq = 1;
            s->last_seq = seq;
            s->padding = 1;
        } else if (s->padding) {
            attach = null_cc;
            attach_size = 3;
        }
    }
    if (!attach)
        return ff_filter_frame(inlink->dst->outputs[0], frame);
    ret = av_frame_make_writable(frame);
    if (ret < 0)
        return ret;
    nxcc_attach(frame, attach, attach_size);
    return ff_filter_frame(inlink->dst->outputs[0], frame);
}

static av_cold int init(AVFilterContext *ctx)
{
    av_log(ctx, AV_LOG_INFO, "nexencoder caption slot\n");
    return 0;
}

static const AVFilterPad inputs[] = {
    {
        .name = "default",
        .type = AVMEDIA_TYPE_VIDEO,
        .filter_frame = filter_frame,
    },
};

#define OFFSET(x) offsetof(NexCaptionContext, x)
#define FLAGS AV_OPT_FLAG_VIDEO_PARAM | AV_OPT_FLAG_FILTERING_PARAM
static const AVOption nexcaption_options[] = {
    { "file", "NXCC caption file", OFFSET(path), AV_OPT_TYPE_STRING, {.str = NULL}, 0, 0, FLAGS },
    { NULL }
};

AVFILTER_DEFINE_CLASS(nexcaption);

const FFFilter ff_vf_nexcaption = {
    .p.name = "nexcaption",
    .p.description = NULL_IF_CONFIG_SMALL("nexencoder caption slot"),
    .p.priv_class = &nexcaption_class,
    .priv_size = sizeof(NexCaptionContext),
    .init = init,
    FILTER_INPUTS(inputs),
    FILTER_OUTPUTS(ff_video_default_filterpad),
    FILTER_PIXFMTS(AV_PIX_FMT_YUV420P),
};
