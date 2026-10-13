/*
 * Program audio, silence, or a 1 kHz tone.
 *
 * The hold path is the NXWAIT file. A.signal and A.bars beside it, and an
 * optional NXPC ring, decide the samples. Color bars always get the tone.
 * A waiting slate or loop with no input is silent. A locked input passes
 * the program audio, or the SRT PCM ring when that file is present.
 *
 * Marker: nexencoder tone
 */

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "libavutil/opt.h"
#include "avfilter.h"
#include "filters.h"
#include "audio.h"

#define NXPC_CAP 48000

typedef struct NexToneContext {
    const AVClass *class;
    char *hold_path;
    char *pcm_path;
    double phase;
    uint64_t read_count;
} NexToneContext;

static int read_flag_file(const char *path, int fallback)
{
    FILE *file;
    int value = fallback;
    if (!path || !path[0])
        return fallback;
    file = fopen(path, "r");
    if (!file)
        return fallback;
    if (fscanf(file, " %d", &value) != 1)
        value = fallback;
    fclose(file);
    return value ? 1 : 0;
}

static void sibling_file(char *dst, size_t size, const char *wait_path, const char *suffix)
{
    const char *dot, *slash;
    size_t stem;
    snprintf(dst, size, "%s", wait_path ? wait_path : "");
    slash = strrchr(dst, '/');
    dot = strrchr(dst, '.');
    if (!dot || (slash && dot < slash))
        return;
    stem = (size_t)(dot - dst);
    snprintf(dst + stem, size - stem, ".%s", suffix);
}

static int waiting_is_bars(const char *path)
{
    FILE *file;
    char line[256];
    if (!path || !path[0])
        return 0;
    file = fopen(path, "r");
    if (!file)
        return 0;
    while (fgets(line, sizeof line, file)) {
        if (!strncmp(line, "waiting bars", 12)) {
            fclose(file);
            return 1;
        }
    }
    fclose(file);
    return 0;
}

static int pull_pcm(NexToneContext *s, int16_t *dst, int samples)
{
    FILE *file;
    uint64_t written = 0;
    int filled = 0;
    unsigned char magic[4];
    if (!s->pcm_path || !s->pcm_path[0])
        return 0;
    file = fopen(s->pcm_path, "rb");
    if (!file)
        return 0;
    if (fread(magic, 1, 4, file) != 4 || memcmp(magic, "NXPC", 4) ||
        fread(&written, 8, 1, file) != 1) {
        fclose(file);
        return 0;
    }
    if (written > s->read_count + NXPC_CAP)
        s->read_count = written - NXPC_CAP / 2;
    while (filled < samples && s->read_count < written) {
        uint64_t index = s->read_count % NXPC_CAP;
        int16_t pair[2];
        if (fseek(file, 12 + (long)index * 4, SEEK_SET) != 0)
            break;
        if (fread(pair, 2, 2, file) != 2)
            break;
        dst[filled * 2] = pair[0];
        dst[filled * 2 + 1] = pair[1];
        s->read_count++;
        filled++;
    }
    fclose(file);
    return filled;
}

static int filter_frame(AVFilterLink *inlink, AVFrame *frame)
{
    NexToneContext *s = inlink->dst->priv;
    int16_t *samples;
    char signal_path[1024], bars_path[1024];
    int signal_ok = 1, bars = 0, tone = 0, i, filled;
    int channels = frame->ch_layout.nb_channels;
    if (channels < 1)
        channels = 1;
    if (frame->format != AV_SAMPLE_FMT_S16)
        return ff_filter_frame(inlink->dst->outputs[0], frame);
    sibling_file(signal_path, sizeof signal_path, s->hold_path, "signal");
    sibling_file(bars_path, sizeof bars_path, s->hold_path, "bars");
    signal_ok = read_flag_file(signal_path, 1);
    bars = read_flag_file(bars_path, 0);
    tone = bars || (!signal_ok && waiting_is_bars(s->hold_path));
    samples = (int16_t *)frame->data[0];
    if (tone) {
        for (i = 0; i < frame->nb_samples; i++) {
            int16_t value;
            int ch;
            s->phase += 1000.0 / 48000.0;
            if (s->phase >= 1.0)
                s->phase -= 1.0;
            value = (int16_t)(sin(s->phase * 2.0 * 3.141592653589793) * 8000.0);
            for (ch = 0; ch < channels; ch++)
                samples[i * channels + ch] = value;
        }
    } else if (!signal_ok) {
        memset(samples, 0, (size_t)frame->nb_samples * channels * sizeof(int16_t));
    } else if (s->pcm_path && s->pcm_path[0]) {
        filled = pull_pcm(s, samples, frame->nb_samples);
        if (filled < frame->nb_samples) {
            memset(samples + filled * channels, 0,
                   (size_t)(frame->nb_samples - filled) * channels * sizeof(int16_t));
        }
    }
    return ff_filter_frame(inlink->dst->outputs[0], frame);
}

static const AVFilterPad inputs[] = {
    {
        .name = "default",
        .type = AVMEDIA_TYPE_AUDIO,
        .filter_frame = filter_frame,
    },
};

#define OFFSET(x) offsetof(NexToneContext, x)
#define FLAGS AV_OPT_FLAG_AUDIO_PARAM | AV_OPT_FLAG_FILTERING_PARAM
static const AVOption nextone_options[] = {
    { "hold", "NXWAIT file", OFFSET(hold_path), AV_OPT_TYPE_STRING, {.str = NULL}, 0, 0, FLAGS },
    { "pcm", "NXPC ring from an SRT caller", OFFSET(pcm_path), AV_OPT_TYPE_STRING, {.str = NULL}, 0, 0, FLAGS },
    { NULL }
};

AVFILTER_DEFINE_CLASS(nextone);

const FFFilter ff_af_nextone = {
    .p.name = "nextone",
    .p.description = NULL_IF_CONFIG_SMALL("nexencoder tone"),
    .p.priv_class = &nextone_class,
    .priv_size = sizeof(NexToneContext),
    FILTER_INPUTS(inputs),
    FILTER_OUTPUTS(ff_audio_default_filterpad),
    FILTER_SAMPLEFMTS(AV_SAMPLE_FMT_S16),
};
