/*
 * NexEncoder squeeze and CG stack for the pinned FFmpeg 9.0.2 build.
 *
 * The filter reads NXSQ state written by nexencoder.effects. Corner order is
 * top-left, top-right, bottom-left, bottom-right. Frame 0 of an N-frame move
 * is the quad already on air; frame N is the target; N = 0 cuts.
 * On 1080i the homography runs per field, then the fields are woven and the
 * CG layers are composited in full-raster space. A/53 side data is copied
 * from the primary frame after the stack is flattened. SCTE-104 never
 * enters this filter; the DeckLink demuxer forwards it over UDP.
 *
 * Keep the lerp, the field split, and the composite order (levels below 0,
 * the primary at level 0, then levels above 0) aligned with nexencoder.effects.
 */

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "libavutil/avstring.h"
#include "libavutil/avutil.h"
#include "libavutil/common.h"
#include "libavutil/imgutils.h"
#include "libavutil/mem.h"
#include "libavutil/opt.h"
#include "libavutil/pixdesc.h"
#include "libavutil/time.h"
#include "libswscale/swscale.h"
#include "avfilter.h"
#include "filters.h"
#include "video.h"

#define NX_SLOTS 2
#define NX_HEADER 32

typedef struct LayerSlot {
    int level;
    char path[768];
} LayerSlot;

typedef struct NexEffectContext {
    const AVClass *class;
    char *state_path;
    char *hold_path;
    int interlaced;
    int signal_ok;
    int bars;
    char waiting[16];
    char ident[128];
    char loop_path[768];
    char live_path[768];

    int width;
    int height;
    int booted;
    int restored;
    int seq;
    int frames;
    int step;
    double start[8];
    double target[8];
    double current[8];
    LayerSlot slots[NX_SLOTS];

    struct SwsContext *sws_to;
    struct SwsContext *sws_from;
    uint8_t *rgba;
    uint8_t *warped;
    uint8_t *comp;
    uint8_t *field_src;
    uint8_t *field_warp;
    uint8_t *layer;
    uint8_t *wait_cache;
    int wait_kind;
    int64_t cost_us[4];
    int cost_frames;
    uint8_t *slot_cache[NX_SLOTS];
    uint32_t slot_seq[NX_SLOTS];
    int slot_cached[NX_SLOTS];
    char slot_cache_path[NX_SLOTS][768];
    char run_path[1024];
    char run_tmp[1024];
} NexEffectContext;

static const char nexencoder_effect_stack_marker[] = "nexencoder effect stack";

static void identity_corners(double dst[8], int w, int h)
{
    dst[0] = 0;     dst[1] = 0;
    dst[2] = w;     dst[3] = 0;
    dst[4] = 0;     dst[5] = h;
    dst[6] = w;     dst[7] = h;
}

static int corners_identity(const double c[8], int w, int h)
{
    double ident[8];
    int i;
    identity_corners(ident, w, h);
    for (i = 0; i < 8; i++)
        if (fabs(c[i] - ident[i]) > 0.5)
            return 0;
    return 1;
}

static int solve8(double m[8][9])
{
    int col, row, pivot, j;
    for (col = 0; col < 8; col++) {
        pivot = col;
        for (row = col + 1; row < 8; row++)
            if (fabs(m[row][col]) > fabs(m[pivot][col]))
                pivot = row;
        if (fabs(m[pivot][col]) < 1e-12)
            return -1;
        if (pivot != col) {
            for (j = col; j < 9; j++) {
                double tmp = m[col][j];
                m[col][j] = m[pivot][j];
                m[pivot][j] = tmp;
            }
        }
        for (j = col + 1; j < 9; j++)
            m[col][j] /= m[col][col];
        m[col][col] = 1.0;
        for (row = 0; row < 8; row++) {
            double factor;
            if (row == col)
                continue;
            factor = m[row][col];
            if (factor == 0)
                continue;
            for (j = col; j < 9; j++)
                m[row][j] -= factor * m[col][j];
        }
    }
    return 0;
}

static int homography(const double dest[8], const double source[8], double h[9])
{
    double m[8][9];
    int i;
    for (i = 0; i < 4; i++) {
        double x = dest[i * 2], y = dest[i * 2 + 1];
        double u = source[i * 2], v = source[i * 2 + 1];
        double *r0 = m[i * 2];
        double *r1 = m[i * 2 + 1];
        r0[0] = x; r0[1] = y; r0[2] = 1; r0[3] = 0; r0[4] = 0; r0[5] = 0;
        r0[6] = -u * x; r0[7] = -u * y; r0[8] = u;
        r1[0] = 0; r1[1] = 0; r1[2] = 0; r1[3] = x; r1[4] = y; r1[5] = 1;
        r1[6] = -v * x; r1[7] = -v * y; r1[8] = v;
    }
    if (solve8(m) < 0)
        return -1;
    for (i = 0; i < 8; i++)
        h[i] = m[i][8];
    h[8] = 1.0;
    return 0;
}

static int map_point(const double h[9], double x, double y, double *ox, double *oy)
{
    double w = h[6] * x + h[7] * y + h[8];
    if (fabs(w) < 1e-9)
        return -1;
    *ox = (h[0] * x + h[1] * y + h[2]) / w;
    *oy = (h[3] * x + h[4] * y + h[5]) / w;
    return 0;
}

static void sample_rgba(const uint8_t *src, int w, int h, double x, double y, uint8_t out[4])
{
    int x0, y0, x1, y1, x0c, y0c, c, wx, wy;
    double fx, fy;
    const uint8_t *p00, *p10, *p01, *p11;
    if (x < -0.5 || y < -0.5 || x >= w - 0.5 || y >= h - 0.5) {
        out[0] = out[1] = out[2] = out[3] = 0;
        return;
    }
    x0 = (int)floor(x);
    y0 = (int)floor(y);
    x1 = FFMIN(x0 + 1, w - 1);
    y1 = FFMIN(y0 + 1, h - 1);
    x0c = FFMAX(x0, 0);
    y0c = FFMAX(y0, 0);
    fx = x - x0;
    fy = y - y0;
    p00 = src + (y0c * w + x0c) * 4;
    p10 = src + (y0c * w + x1) * 4;
    p01 = src + (y1 * w + x0c) * 4;
    p11 = src + (y1 * w + x1) * 4;
    wx = (int)(fx * 256.0);
    wy = (int)(fy * 256.0);
    for (c = 0; c < 4; c++) {
        int top = p00[c] * (256 - wx) + p10[c] * wx;
        int bot = p01[c] * (256 - wx) + p11[c] * wx;
        out[c] = (uint8_t)((top * (256 - wy) + bot * wy + 32768) >> 16);
    }
}

static void warp_rgba(const uint8_t *src, uint8_t *dst, int w, int h, const double corners[8])
{
    double source[8], matrix[9];
    double lo_x = corners[0], hi_x = corners[0], lo_y = corners[1], hi_y = corners[1];
    int x, y, i, x_from, x_to, y_from, y_to, affine;
    memset(dst, 0, (size_t)w * h * 4);
    identity_corners(source, w, h);
    if (homography(corners, source, matrix) < 0)
        return;
    /* Only the box around the squeezed picture can be non-zero; the rest of
     * the frame stays cleared. */
    for (i = 1; i < 4; i++) {
        lo_x = FFMIN(lo_x, corners[i * 2]);
        hi_x = FFMAX(hi_x, corners[i * 2]);
        lo_y = FFMIN(lo_y, corners[i * 2 + 1]);
        hi_y = FFMAX(hi_y, corners[i * 2 + 1]);
    }
    x_from = FFMAX(0, (int)floor(lo_x) - 1);
    x_to = FFMIN(w, (int)ceil(hi_x) + 1);
    y_from = FFMAX(0, (int)floor(lo_y) - 1);
    y_to = FFMIN(h, (int)ceil(hi_y) + 1);
    /* A plain squeeze or move has no perspective, so no divide per pixel. */
    affine = fabs(matrix[6]) < 1e-12 && fabs(matrix[7]) < 1e-12 && fabs(matrix[8] - 1.0) < 1e-12;
    for (y = y_from; y < y_to; y++) {
        double sx = matrix[0] * (x_from + 0.5) + matrix[1] * (y + 0.5) + matrix[2];
        double sy = matrix[3] * (x_from + 0.5) + matrix[4] * (y + 0.5) + matrix[5];
        for (x = x_from; x < x_to; x++) {
            uint8_t pixel[4];
            if (affine) {
                sample_rgba(src, w, h, sx - 0.5, sy - 0.5, pixel);
                sx += matrix[0];
                sy += matrix[3];
            } else {
                double px, py;
                if (map_point(matrix, x + 0.5, y + 0.5, &px, &py) < 0)
                    continue;
                sample_rgba(src, w, h, px - 0.5, py - 0.5, pixel);
            }
            memcpy(dst + ((size_t)y * w + x) * 4, pixel, 4);
        }
    }
}

static void over_pixel(uint8_t *dst, const uint8_t *src)
{
    double sa;
    int c;
    if (src[3] == 0)
        return;
    if (src[3] == 255) {
        memcpy(dst, src, 4);
        return;
    }
    sa = src[3] / 255.0;
    for (c = 0; c < 3; c++)
        dst[c] = (uint8_t)(src[c] * sa + dst[c] * (1.0 - sa) + 0.5);
    dst[3] = (uint8_t)FFMIN(255, (int)(src[3] + dst[3] * (1.0 - sa) + 0.5));
}

static int read_state(NexEffectContext *s, int *seq, int *frames, double corners[8], LayerSlot slots[NX_SLOTS])
{
    FILE *file;
    char line[1024];
    int i;
    if (!s->state_path)
        return 0;
    file = fopen(s->state_path, "r");
    if (!file)
        return 0;
    if (!fgets(line, sizeof line, file) || strncmp(line, "NXSQ 1", 6) != 0) {
        fclose(file);
        return 0;
    }
    if (fscanf(file, " seq %d", seq) != 1 || fscanf(file, " frames %d", frames) != 1) {
        fclose(file);
        return 0;
    }
    for (i = 0; i < 4; i++) {
        if (fscanf(file, " %lf %lf", &corners[i * 2], &corners[i * 2 + 1]) != 2) {
            fclose(file);
            return 0;
        }
    }
    for (i = 0; i < NX_SLOTS; i++) {
        int index = -1, level = 0;
        char path[768];
        if (fscanf(file, " slot %d %d %767s", &index, &level, path) != 3 || index != i) {
            fclose(file);
            return 0;
        }
        slots[i].level = level;
        snprintf(slots[i].path, sizeof slots[i].path, "%s", path);
    }
    fclose(file);
    return 1;
}

static int read_run(NexEffectContext *s)
{
    FILE *file;
    int seq, frames, step, i;
    double current[8], start[8], target[8];
    char line[64];
    if (!s->run_path[0])
        return 0;
    file = fopen(s->run_path, "r");
    if (!file)
        return 0;
    if (!fgets(line, sizeof line, file) || strncmp(line, "NXRUN 1", 7) != 0) {
        fclose(file);
        return 0;
    }
    if (fscanf(file, " seq %d frames %d step %d", &seq, &frames, &step) != 3) {
        fclose(file);
        return 0;
    }
    for (i = 0; i < 4; i++)
        if (fscanf(file, " %lf %lf", &current[i * 2], &current[i * 2 + 1]) != 2) {
            fclose(file);
            return 0;
        }
    for (i = 0; i < 4; i++)
        if (fscanf(file, " %lf %lf", &start[i * 2], &start[i * 2 + 1]) != 2) {
            fclose(file);
            return 0;
        }
    for (i = 0; i < 4; i++)
        if (fscanf(file, " %lf %lf", &target[i * 2], &target[i * 2 + 1]) != 2) {
            fclose(file);
            return 0;
        }
    fclose(file);
    s->seq = seq;
    s->frames = frames;
    s->step = step;
    memcpy(s->current, current, sizeof current);
    memcpy(s->start, start, sizeof start);
    memcpy(s->target, target, sizeof target);
    return 1;
}

static void write_run(NexEffectContext *s)
{
    FILE *file;
    int i;
    if (!s->run_tmp[0])
        return;
    file = fopen(s->run_tmp, "w");
    if (!file)
        return;
    fprintf(file, "NXRUN 1\nseq %d\nframes %d\nstep %d\n", s->seq, s->frames, s->step);
    for (i = 0; i < 4; i++)
        fprintf(file, "%.6f %.6f\n", s->current[i * 2], s->current[i * 2 + 1]);
    for (i = 0; i < 4; i++)
        fprintf(file, "%.6f %.6f\n", s->start[i * 2], s->start[i * 2 + 1]);
    for (i = 0; i < 4; i++)
        fprintf(file, "%.6f %.6f\n", s->target[i * 2], s->target[i * 2 + 1]);
    fclose(file);
    rename(s->run_tmp, s->run_path);
}

static void apply_new(NexEffectContext *s, int seq, int frames, const double corners[8])
{
    memcpy(s->start, s->current, sizeof s->current);
    memcpy(s->target, corners, sizeof s->target);
    s->frames = frames < 0 ? 0 : frames;
    s->step = 0;
    s->seq = seq;
}

static void advance_squeeze(NexEffectContext *s)
{
    double u;
    int i;
    if (s->frames <= 0)
        u = 1.0;
    else
        u = FFMIN(1.0, s->step / (double)s->frames);
    for (i = 0; i < 8; i++)
        s->current[i] = s->start[i] + (s->target[i] - s->start[i]) * u;
    if (s->frames > 0 && s->step < s->frames)
        s->step++;
}

static int layers_active(const LayerSlot slots[NX_SLOTS])
{
    int i;
    for (i = 0; i < NX_SLOTS; i++)
        if (slots[i].level != 0)
            return 1;
    return 0;
}

static void unpremultiply(uint8_t *px, int w, int h)
{
    size_t i, count = (size_t)w * h;
    for (i = 0; i < count; i++) {
        uint8_t *pixel = px + i * 4;
        unsigned alpha = pixel[3];
        if (alpha == 255)
            continue;
        if (alpha == 0) {
            pixel[0] = pixel[1] = pixel[2] = 0;
            continue;
        }
        pixel[0] = (uint8_t)FFMIN(255, (pixel[0] * 255u + alpha / 2) / alpha);
        pixel[1] = (uint8_t)FFMIN(255, (pixel[1] * 255u + alpha / 2) / alpha);
        pixel[2] = (uint8_t)FFMIN(255, (pixel[2] * 255u + alpha / 2) / alpha);
    }
}

/* Returns 1 for a new frame in dst, 2 when the slot still holds the frame
 * numbered *seen (dst is untouched), and 0 when nothing consistent could be
 * read. A writer mid-update gives 0. Callers that keep the last frame show it
 * instead of dropping the layer for that video frame. */
static int read_layer_seq(const char *path, int w, int h, uint8_t *dst, uint32_t *seen)
{
    FILE *file;
    uint8_t header[NX_HEADER];
    uint32_t seq1, seq2, width, height;
    size_t bytes;
    int spins;
    if (!path || path[0] == '-' || path[0] == 0)
        return 0;
    file = fopen(path, "rb");
    if (!file)
        return 0;
    for (spins = 0; spins < 4; spins++) {
        if (fseek(file, 0, SEEK_SET) != 0)
            break;
        if (fread(header, 1, NX_HEADER, file) != NX_HEADER)
            break;
        if (memcmp(header, "NXCG", 4) != 0)
            break;
        seq1 = (uint32_t)header[4] | ((uint32_t)header[5] << 8) |
               ((uint32_t)header[6] << 16) | ((uint32_t)header[7] << 24);
        width = (uint32_t)header[8] | ((uint32_t)header[9] << 8) |
                ((uint32_t)header[10] << 16) | ((uint32_t)header[11] << 24);
        height = (uint32_t)header[12] | ((uint32_t)header[13] << 8) |
                 ((uint32_t)header[14] << 16) | ((uint32_t)header[15] << 24);
        if ((seq1 & 1u) || seq1 == 0 || width != (uint32_t)w || height != (uint32_t)h)
            break;
        if (seen && *seen == seq1) {
            fclose(file);
            return 2;
        }
        bytes = (size_t)w * h * 4;
        if (fread(dst, 1, bytes, file) != bytes)
            break;
        if (fseek(file, 4, SEEK_SET) != 0 || fread(header, 1, 4, file) != 4)
            break;
        seq2 = (uint32_t)header[0] | ((uint32_t)header[1] << 8) |
               ((uint32_t)header[2] << 16) | ((uint32_t)header[3] << 24);
        if (seq1 == seq2) {
            uint32_t flags = (uint32_t)header[20] | ((uint32_t)header[21] << 8) |
                             ((uint32_t)header[22] << 16) | ((uint32_t)header[23] << 24);
            if (flags & 1u)
                unpremultiply(dst, w, h);
            if (seen)
                *seen = seq1;
            fclose(file);
            return 1;
        }
    }
    fclose(file);
    return 0;
}

static int read_layer(const char *path, int w, int h, uint8_t *dst)
{
    return read_layer_seq(path, w, h, dst, NULL) == 1;
}

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
    const char *dot;
    const char *slash;
    size_t stem;
    if (!wait_path) {
        dst[0] = 0;
        return;
    }
    snprintf(dst, size, "%s", wait_path);
    slash = strrchr(dst, '/');
#ifdef _WIN32
    {
        const char *back = strrchr(dst, '\\');
        if (!slash || (back && back > slash))
            slash = back;
    }
#endif
    dot = strrchr(dst, '.');
    if (!dot || (slash && dot < slash))
        return;
    stem = (size_t)(dot - dst);
    snprintf(dst + stem, size - stem, ".%s", suffix);
}

static void read_hold(NexEffectContext *s)
{
    FILE *file;
    char line[1024];
    char bars_path[1024];
    char signal_path[1024];
    s->signal_ok = 1;
    s->bars = 0;
    snprintf(s->waiting, sizeof s->waiting, "slate");
    s->ident[0] = 0;
    snprintf(s->loop_path, sizeof s->loop_path, "-");
    snprintf(s->live_path, sizeof s->live_path, "-");
    if (!s->hold_path || !s->hold_path[0])
        return;
    sibling_file(signal_path, sizeof signal_path, s->hold_path, "signal");
    sibling_file(bars_path, sizeof bars_path, s->hold_path, "bars");
    s->signal_ok = read_flag_file(signal_path, 1);
    s->bars = read_flag_file(bars_path, 0);
    file = fopen(s->hold_path, "r");
    if (!file)
        return;
    if (!fgets(line, sizeof line, file) || strncmp(line, "NXWAIT 1", 8) != 0) {
        fclose(file);
        return;
    }
    while (fgets(line, sizeof line, file)) {
        size_t len = strlen(line);
        while (len && (line[len - 1] == '\n' || line[len - 1] == '\r'))
            line[--len] = 0;
        if (!strncmp(line, "waiting ", 8))
            av_strlcpy(s->waiting, line + 8, sizeof s->waiting);
        else if (!strncmp(line, "ident ", 6))
            av_strlcpy(s->ident, line + 6, sizeof s->ident);
        else if (!strncmp(line, "loop ", 5))
            av_strlcpy(s->loop_path, line + 5, sizeof s->loop_path);
        else if (!strncmp(line, "live ", 5))
            av_strlcpy(s->live_path, line + 5, sizeof s->live_path);
    }
    fclose(file);
}

/* 5x7 glyphs, bit 4 is the leftmost column. Indexed from ASCII 32. */
static const uint8_t nx_font[][7] = {
    {0,0,0,0,0,0,0},
    {0x04,0x04,0x04,0x04,0x04,0,0x04},
    {0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},
    {0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},
    {0,0,0,0,0,0,0},
    {0,0,0,0,0,0,0},
    {0,0,0,0,0,0x04,0},
    {0,0,0,0,0,0,0},
    {0x0E,0x11,0x13,0x15,0x19,0x11,0x0E},
    {0x04,0x0C,0x04,0x04,0x04,0x04,0x0E},
    {0x0E,0x11,0x01,0x06,0x08,0x10,0x1F},
    {0x1E,0x01,0x01,0x0E,0x01,0x01,0x1E},
    {0x02,0x06,0x0A,0x12,0x1F,0x02,0x02},
    {0x1F,0x10,0x1E,0x01,0x01,0x11,0x0E},
    {0x06,0x08,0x10,0x1E,0x11,0x11,0x0E},
    {0x1F,0x01,0x02,0x04,0x08,0x08,0x08},
    {0x0E,0x11,0x11,0x0E,0x11,0x11,0x0E},
    {0x0E,0x11,0x11,0x0F,0x01,0x02,0x0C},
    {0,0x04,0,0,0,0x04,0},
    {0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},{0,0,0,0,0,0,0},
    {0x0E,0x11,0x11,0x1F,0x11,0x11,0x11},
    {0x1E,0x11,0x11,0x1E,0x11,0x11,0x1E},
    {0x0E,0x11,0x10,0x10,0x10,0x11,0x0E},
    {0x1E,0x11,0x11,0x11,0x11,0x11,0x1E},
    {0x1F,0x10,0x10,0x1E,0x10,0x10,0x1F},
    {0x1F,0x10,0x10,0x1E,0x10,0x10,0x10},
    {0x0E,0x11,0x10,0x17,0x11,0x11,0x0F},
    {0x11,0x11,0x11,0x1F,0x11,0x11,0x11},
    {0x0E,0x04,0x04,0x04,0x04,0x04,0x0E},
    {0x01,0x01,0x01,0x01,0x11,0x11,0x0E},
    {0x11,0x12,0x14,0x18,0x14,0x12,0x11},
    {0x10,0x10,0x10,0x10,0x10,0x10,0x1F},
    {0x11,0x1B,0x15,0x15,0x11,0x11,0x11},
    {0x11,0x19,0x15,0x13,0x11,0x11,0x11},
    {0x0E,0x11,0x11,0x11,0x11,0x11,0x0E},
    {0x1E,0x11,0x11,0x1E,0x10,0x10,0x10},
    {0x0E,0x11,0x11,0x11,0x15,0x12,0x0D},
    {0x1E,0x11,0x11,0x1E,0x14,0x12,0x11},
    {0x0E,0x11,0x10,0x0E,0x01,0x11,0x0E},
    {0x1F,0x04,0x04,0x04,0x04,0x04,0x04},
    {0x11,0x11,0x11,0x11,0x11,0x11,0x0E},
    {0x11,0x11,0x11,0x11,0x11,0x0A,0x04},
    {0x11,0x11,0x11,0x15,0x15,0x1B,0x11},
    {0x11,0x11,0x0A,0x04,0x0A,0x11,0x11},
    {0x11,0x11,0x0A,0x04,0x04,0x04,0x04},
    {0x1F,0x01,0x02,0x04,0x08,0x10,0x1F},
};

static void put_px(uint8_t *dst, int w, int h, int x, int y, const uint8_t rgba[4])
{
    size_t at;
    if (x < 0 || y < 0 || x >= w || y >= h)
        return;
    at = ((size_t)y * w + x) * 4;
    dst[at] = rgba[0];
    dst[at + 1] = rgba[1];
    dst[at + 2] = rgba[2];
    dst[at + 3] = rgba[3];
}

static void draw_text(uint8_t *dst, int w, int h, const char *text, int origin_y)
{
    static const uint8_t white[4] = {255, 255, 255, 255};
    int scale = w >= 1600 ? 6 : 4;
    int count = text ? (int)strlen(text) : 0;
    int glyph_w = 6 * scale;
    int total, origin_x, i, row, col, sy, sx;
    if (!count)
        return;
    total = count * glyph_w;
    origin_x = (w - total) / 2;
    if (origin_y < 0)
        origin_y = h - 10 * scale;
    for (i = 0; i < count; i++) {
        int ch = text[i];
        const uint8_t *rows;
        if (ch >= 'a' && ch <= 'z')
            ch -= 32;
        if (ch < 32 || ch > 90)
            ch = 32;
        rows = nx_font[ch - 32];
        for (row = 0; row < 7; row++) {
            for (col = 0; col < 5; col++) {
                if (!(rows[row] & (0x10 >> col)))
                    continue;
                for (sy = 0; sy < scale; sy++)
                    for (sx = 0; sx < scale; sx++)
                        put_px(dst, w, h, origin_x + i * glyph_w + col * scale + sx,
                               origin_y + row * scale + sy, white);
            }
        }
    }
}

static void draw_bars(uint8_t *dst, int w, int h)
{
    static const uint8_t colors[7][4] = {
        {255, 255, 255, 255}, {255, 255, 0, 255}, {0, 255, 255, 255},
        {0, 255, 0, 255}, {255, 0, 255, 255}, {255, 0, 0, 255}, {0, 0, 255, 255},
    };
    int y, x, band = h - h / 8;
    for (y = 0; y < h; y++) {
        for (x = 0; x < w; x++) {
            const uint8_t *color = y < band ? colors[(x * 7) / w] : (const uint8_t *)"\x00\x00\x00\xff";
            size_t at = ((size_t)y * w + x) * 4;
            dst[at] = color[0];
            dst[at + 1] = color[1];
            dst[at + 2] = color[2];
            dst[at + 3] = 255;
        }
    }
}

static void draw_slate(uint8_t *dst, int w, int h)
{
    size_t i, pixels = (size_t)w * h;
    for (i = 0; i < pixels; i++) {
        dst[i * 4] = 12;
        dst[i * 4 + 1] = 28;
        dst[i * 4 + 2] = 48;
        dst[i * 4 + 3] = 255;
    }
    draw_text(dst, w, h, "WAITING FOR INPUT", h / 2 - (w >= 1600 ? 21 : 14));
}

static void paint_waiting(uint8_t *dst, int w, int h, NexEffectContext *s)
{
    int bars = !strcmp(s->waiting, "bars");
    int kind = bars ? 1 : 2;
    size_t bytes = (size_t)w * h * 4;
    if (!bars && !strcmp(s->waiting, "loop") && s->loop_path[0] && strcmp(s->loop_path, "-") &&
        read_layer(s->loop_path, w, h, s->layer)) {
        memcpy(dst, s->layer, bytes);
        return;
    }
    /* Bars and the slate never change, so they are drawn once and copied. */
    if (s->wait_cache && s->wait_kind == kind) {
        memcpy(dst, s->wait_cache, bytes);
        return;
    }
    if (bars)
        draw_bars(dst, w, h);
    else
        draw_slate(dst, w, h);
    if (s->wait_cache) {
        memcpy(s->wait_cache, dst, bytes);
        s->wait_kind = kind;
    }
}

static void draw_video(uint8_t *dst, const uint8_t *video, size_t pixels, int covered)
{
    size_t i;
    if (covered) {
        memcpy(dst, video, pixels * 4);
        return;
    }
    for (i = 0; i < pixels; i++)
        over_pixel(dst + i * 4, video + i * 4);
}

/* The newest consistent frame of a layer slot. The HTML feeder rewrites its
 * file about fifteen times a second while the picture runs at sixty, so many
 * video frames land on a rewrite. Those show the previous frame instead of
 * losing the layer, and an unchanged slot costs a header read, not 8 MB. */
static const uint8_t *slot_frame(NexEffectContext *s, int index, int w, int h)
{
    const char *path = s->slots[index].path;
    int same = s->slot_cached[index] && !strcmp(s->slot_cache_path[index], path);
    uint32_t seen = same ? s->slot_seq[index] : 0;
    if (read_layer_seq(path, w, h, s->layer, &seen) == 1) {
        uint8_t *spare = s->slot_cache[index];
        s->slot_cache[index] = s->layer;
        s->layer = spare;
        s->slot_seq[index] = seen;
        s->slot_cached[index] = 1;
        snprintf(s->slot_cache_path[index], sizeof s->slot_cache_path[index], "%s", path);
        return s->slot_cache[index];
    }
    return same ? s->slot_cache[index] : NULL;
}

static void composite(uint8_t *dst, const uint8_t *video, int w, int h,
                      NexEffectContext *s)
{
    size_t pixels = (size_t)w * h;
    size_t i;
    int order[NX_SLOTS];
    int count = 0;
    int n, j, drawn_video = 0;
    /* Live video that fills the frame hides the waiting picture. */
    int covered = s->signal_ok && corners_identity(s->current, w, h);
    if (!covered)
        paint_waiting(dst, w, h, s);
    for (n = 0; n < NX_SLOTS; n++) {
        if (s->slots[n].level != 0)
            order[count++] = n;
        else
            s->slot_cached[n] = 0;
    }
    for (n = 1; n < count; n++) {
        int item = order[n];
        j = n;
        while (j > 0 && s->slots[order[j - 1]].level > s->slots[item].level) {
            order[j] = order[j - 1];
            j--;
        }
        order[j] = item;
    }
    for (n = 0; n < count; n++) {
        int index = order[n];
        const uint8_t *layer;
        if (!drawn_video && s->signal_ok && s->slots[index].level > 0) {
            draw_video(dst, video, pixels, covered);
            drawn_video = 1;
        }
        layer = slot_frame(s, index, w, h);
        if (!layer)
            continue;
        for (i = 0; i < pixels; i++)
            over_pixel(dst + i * 4, layer + i * 4);
    }
    if (!drawn_video && s->signal_ok)
        draw_video(dst, video, pixels, covered);
    if (s->bars) {
        draw_bars(dst, w, h);
        if (s->ident[0] && strcmp(s->ident, "-"))
            draw_text(dst, w, h, s->ident, h - h / 10);
    }
}

static void force_opaque(uint8_t *rgba, int w, int h)
{
    size_t i, pixels = (size_t)w * h;
    for (i = 0; i < pixels; i++)
        rgba[i * 4 + 3] = 255;
}

static void squeeze_fields(NexEffectContext *s, const double corners[8])
{
    int w = s->width, h = s->height, half = h / 2, y, stride = w * 4;
    double field[8];
    int i;
    for (i = 0; i < 4; i++) {
        field[i * 2] = corners[i * 2];
        field[i * 2 + 1] = corners[i * 2 + 1] / 2.0;
    }
    for (y = 0; y < half; y++)
        memcpy(s->field_src + (size_t)y * stride, s->rgba + (size_t)(2 * y) * stride, stride);
    warp_rgba(s->field_src, s->field_warp, w, half, field);
    for (y = 0; y < half; y++)
        memcpy(s->warped + (size_t)(2 * y) * stride, s->field_warp + (size_t)y * stride, stride);
    for (y = 0; y < half; y++)
        memcpy(s->field_src + (size_t)y * stride, s->rgba + (size_t)(2 * y + 1) * stride, stride);
    warp_rgba(s->field_src, s->field_warp, w, half, field);
    for (y = 0; y < half; y++)
        memcpy(s->warped + (size_t)(2 * y + 1) * stride, s->field_warp + (size_t)y * stride, stride);
}

static int config_input(AVFilterLink *inlink)
{
    AVFilterContext *ctx = inlink->dst;
    NexEffectContext *s = ctx->priv;
    size_t bytes;
    int i;
    if (inlink->format != AV_PIX_FMT_YUV420P)
        return AVERROR(EINVAL);
    if (inlink->w < 2 || inlink->h < 2 || inlink->w > 2048 || inlink->h > 1088)
        return AVERROR(EINVAL);
    s->width = inlink->w;
    s->height = inlink->h;
    if ((s->height & 1) && s->interlaced)
        s->interlaced = 0;
    bytes = (size_t)s->width * s->height * 4;
    s->rgba = av_malloc(bytes);
    s->warped = av_malloc(bytes);
    s->comp = av_malloc(bytes);
    s->field_src = av_malloc(bytes / 2 + 4);
    s->field_warp = av_malloc(bytes / 2 + 4);
    s->layer = av_malloc(bytes);
    s->wait_cache = av_malloc(bytes);
    s->wait_kind = 0;
    for (i = 0; i < NX_SLOTS; i++) {
        s->slot_cache[i] = av_malloc(bytes);
        s->slot_cached[i] = 0;
        if (!s->slot_cache[i])
            return AVERROR(ENOMEM);
    }
    if (!s->rgba || !s->warped || !s->comp || !s->field_src || !s->field_warp || !s->layer)
        return AVERROR(ENOMEM);
    s->sws_to = sws_getContext(s->width, s->height, AV_PIX_FMT_YUV420P,
                               s->width, s->height, AV_PIX_FMT_RGBA,
                               SWS_BILINEAR, NULL, NULL, NULL);
    s->sws_from = sws_getContext(s->width, s->height, AV_PIX_FMT_RGBA,
                                 s->width, s->height, AV_PIX_FMT_YUV420P,
                                 SWS_BILINEAR, NULL, NULL, NULL);
    if (!s->sws_to || !s->sws_from)
        return AVERROR(EINVAL);
    identity_corners(s->current, s->width, s->height);
    memcpy(s->start, s->current, sizeof s->current);
    memcpy(s->target, s->current, sizeof s->current);
    av_log(ctx, AV_LOG_INFO, "%s\n", nexencoder_effect_stack_marker);
    if (s->state_path && strlen(s->state_path) + 5 < sizeof s->run_path) {
        snprintf(s->run_path, sizeof s->run_path, "%s.run", s->state_path);
        snprintf(s->run_tmp, sizeof s->run_tmp, "%s.run.tmp", s->state_path);
    }
    return 0;
}

static int filter_frame(AVFilterLink *inlink, AVFrame *in)
{
    AVFilterContext *ctx = inlink->dst;
    NexEffectContext *s = ctx->priv;
    AVFilterLink *outlink = ctx->outputs[0];
    AVFrame *out;
    int seq = 0, frames = 0, parsed, clean, ret;
    double corners[8];
    LayerSlot slots[NX_SLOTS];
    uint8_t *dst_data[4];
    int dst_stride[4];
    const uint8_t *src_data[4];
    int src_stride[4];
    int64_t t0, t1, t2, t3;

    read_hold(s);
    parsed = read_state(s, &seq, &frames, corners, slots);
    s->restored = 0;
    if (!s->booted) {
        s->booted = 1;
        if (parsed && read_run(s) && s->seq == seq) {
            memcpy(s->slots, slots, sizeof slots);
            s->restored = 1;
        } else if (parsed) {
            if (s->seq != seq)
                apply_new(s, seq, frames, corners);
            memcpy(s->slots, slots, sizeof slots);
        }
    } else if (parsed && seq != s->seq) {
        apply_new(s, seq, frames, corners);
        memcpy(s->slots, slots, sizeof slots);
    } else if (parsed) {
        memcpy(s->slots, slots, sizeof slots);
    }
    if (parsed && !s->restored)
        advance_squeeze(s);

    clean = s->signal_ok && !s->bars &&
            !(s->live_path[0] && strcmp(s->live_path, "-")) &&
            corners_identity(s->current, s->width, s->height) && !layers_active(s->slots);
    out = ff_get_video_buffer(outlink, s->width, s->height);
    if (!out) {
        av_frame_free(&in);
        return AVERROR(ENOMEM);
    }
    ret = av_frame_copy_props(out, in);
    if (ret < 0) {
        av_frame_free(&in);
        av_frame_free(&out);
        return ret;
    }
    if (clean) {
        ret = av_frame_copy(out, in);
        av_frame_free(&in);
        if (ret < 0) {
            av_frame_free(&out);
            return ret;
        }
        if (parsed)
            write_run(s);
        return ff_filter_frame(outlink, out);
    }

    dst_data[0] = s->rgba;
    dst_data[1] = dst_data[2] = dst_data[3] = NULL;
    dst_stride[0] = s->width * 4;
    dst_stride[1] = dst_stride[2] = dst_stride[3] = 0;
    t0 = av_gettime_relative();
    sws_scale(s->sws_to, (const uint8_t *const *)in->data, in->linesize,
              0, s->height, dst_data, dst_stride);
    force_opaque(s->rgba, s->width, s->height);
    t1 = av_gettime_relative();
    if (s->signal_ok && s->live_path[0] && strcmp(s->live_path, "-") &&
        read_layer(s->live_path, s->width, s->height, s->layer))
        memcpy(s->rgba, s->layer, (size_t)s->width * s->height * 4);
    if (corners_identity(s->current, s->width, s->height)) {
        memcpy(s->warped, s->rgba, (size_t)s->width * s->height * 4);
    } else if (s->interlaced) {
        squeeze_fields(s, s->current);
    } else {
        warp_rgba(s->rgba, s->warped, s->width, s->height, s->current);
    }
    t2 = av_gettime_relative();
    composite(s->comp, s->warped, s->width, s->height, s);
    t3 = av_gettime_relative();
    src_data[0] = s->comp;
    src_data[1] = src_data[2] = src_data[3] = NULL;
    src_stride[0] = s->width * 4;
    src_stride[1] = src_stride[2] = src_stride[3] = 0;
    sws_scale(s->sws_from, src_data, src_stride, 0, s->height, out->data, out->linesize);
    s->cost_us[0] += t1 - t0;
    s->cost_us[1] += t2 - t1;
    s->cost_us[2] += t3 - t2;
    s->cost_us[3] += av_gettime_relative() - t3;
    if (++s->cost_frames >= 300) {
        int64_t total = s->cost_us[0] + s->cost_us[1] + s->cost_us[2] + s->cost_us[3];
        /* A frame at 60 fps is 16.7 ms; the filter owns one thread of it. */
        if (total / s->cost_frames > 10000)
            av_log(ctx, AV_LOG_WARNING,
                   "nexeffect averages %.1f ms a frame over %d: to-rgb %.1f, squeeze %.1f, composite %.1f, to-yuv %.1f\n",
                   total / 1000.0 / s->cost_frames, s->cost_frames,
                   s->cost_us[0] / 1000.0 / s->cost_frames, s->cost_us[1] / 1000.0 / s->cost_frames,
                   s->cost_us[2] / 1000.0 / s->cost_frames, s->cost_us[3] / 1000.0 / s->cost_frames);
        memset(s->cost_us, 0, sizeof s->cost_us);
        s->cost_frames = 0;
    }
    av_frame_free(&in);
    if (parsed)
        write_run(s);
    return ff_filter_frame(outlink, out);
}

static av_cold void uninit(AVFilterContext *ctx)
{
    NexEffectContext *s = ctx->priv;
    sws_freeContext(s->sws_to);
    sws_freeContext(s->sws_from);
    av_freep(&s->rgba);
    av_freep(&s->warped);
    av_freep(&s->comp);
    av_freep(&s->field_src);
    av_freep(&s->field_warp);
    av_freep(&s->layer);
    av_freep(&s->wait_cache);
    for (int i = 0; i < NX_SLOTS; i++)
        av_freep(&s->slot_cache[i]);
}

static const AVFilterPad inputs[] = {
    {
        .name = "default",
        .type = AVMEDIA_TYPE_VIDEO,
        .filter_frame = filter_frame,
        .config_props = config_input,
    },
};

#define OFFSET(x) offsetof(NexEffectContext, x)
#define FLAGS AV_OPT_FLAG_VIDEO_PARAM | AV_OPT_FLAG_FILTERING_PARAM
static const AVOption nexeffect_options[] = {
    { "state", "NXSQ state file", OFFSET(state_path), AV_OPT_TYPE_STRING, {.str = NULL}, 0, 0, FLAGS },
    { "hold", "NXWAIT file; signal and bars are siblings", OFFSET(hold_path), AV_OPT_TYPE_STRING, {.str = NULL}, 0, 0, FLAGS },
    { "interlaced", "squeeze each field, then weave", OFFSET(interlaced), AV_OPT_TYPE_BOOL, {.i64 = 0}, 0, 1, FLAGS },
    { NULL }
};

AVFILTER_DEFINE_CLASS(nexeffect);

const FFFilter ff_vf_nexeffect = {
    .p.name = "nexeffect",
    .p.description = NULL_IF_CONFIG_SMALL("nexencoder effect stack"),
    .p.priv_class = &nexeffect_class,
    .priv_size = sizeof(NexEffectContext),
    .uninit = uninit,
    FILTER_INPUTS(inputs),
    FILTER_OUTPUTS(ff_video_default_filterpad),
    FILTER_PIXFMTS(AV_PIX_FMT_YUV420P),
};
