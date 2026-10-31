/* R-20 spike：libavformat 私有 FourCC "TPIC" 写出/读回验证（一次性调研代码，不入门禁）。
 *
 * 验证问题（risk_register R-20）：
 *   S1. movenc 是否接受 AV_CODEC_ID_NONE + codec_tag='TPIC' 的视频流写出？
 *   S2. 读回（libavformat demux）能否拿到逐字节一致的 packet + 正确 PTS？
 *   S3. 非 Topos reader（ffprobe/ffmpeg CLI）是否安全：识别为未知 codec、不崩、可跳过？
 *   S4. FastStart（+movflags faststart）与普通布局均可打开？
 *   S5. 自定义元数据（tpcC 候选载体）能否写入并读回？
 *
 * 用法：spike_movenc <out_dir>
 * 输出：每用例一个 .mov + stdout 结构化 PASS/FAIL 行。
 */
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libavutil/dict.h>
#include <libavutil/pixfmt.h>
#include <libavutil/rational.h>

#include "../include/topos_codec.h"

#define FRAME_W 320
#define FRAME_H 240
#define FRAME_COUNT 6
#define FPS 24

/* —— 确定性合成帧（每帧不同 → 读回可按序号核对）—— */
static uint64_t g_rng;
static uint32_t rng32(void)
{
    g_rng ^= g_rng >> 12;
    g_rng ^= g_rng << 25;
    g_rng ^= g_rng >> 27;
    return (uint32_t)((g_rng * 0x2545F4914F6CDD1Dull) >> 32);
}

static void fill_plane(uint16_t* p, uint32_t w, uint32_t h, uint32_t frame_idx)
{
    for (uint32_t y = 0; y < h; ++y) {
        for (uint32_t x = 0; x < w; ++x) {
            uint32_t grad = (x * 400u / w + y * 400u / h + frame_idx * 37u) % 1024u;
            int32_t noise = (int32_t)(rng32() % 41u) - 20;
            int32_t v = (int32_t)grad + noise;
            if (v < 0) { v = 0; }
            if (v > 1023) { v = 1023; }
            p[(size_t)y * w + x] = (uint16_t)v;
        }
    }
}

static int32_t encode_test_frame(uint32_t idx, uint8_t** out, size_t* out_size)
{
    topos_frame_config cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.struct_size = (uint32_t)sizeof(cfg);
    cfg.abi_version = TOPOS_CODEC_ABI_VERSION;
    cfg.visible_width = FRAME_W;
    cfg.visible_height = FRAME_H;
    cfg.profile = 3u;
    cfg.bit_depth = 10u;
    cfg.qmatrix_id = 1u;
    cfg.qp_base = 32u;
    cfg.color_range = 1u;
    cfg.color_primaries = 1u;
    cfg.color_transfer = 1u;
    cfg.color_matrix = 1u;

    uint32_t w2 = (FRAME_W + 1u) / 2u;
    uint16_t* y = (uint16_t*)malloc((size_t)FRAME_W * FRAME_H * 2u);
    uint16_t* u = (uint16_t*)malloc((size_t)w2 * FRAME_H * 2u);
    uint16_t* v = (uint16_t*)malloc((size_t)w2 * FRAME_H * 2u);
    if (y == NULL || u == NULL || v == NULL) {
        free(y); free(u); free(v);
        return TC_ERR_OUT_OF_MEMORY;
    }
    g_rng = 0x9E3779B97F4A7C15ull ^ ((uint64_t)idx * 0x2545F4914F6CDD1Dull);
    fill_plane(y, FRAME_W, FRAME_H, idx);
    fill_plane(u, w2, FRAME_H, idx + 1u);
    fill_plane(v, w2, FRAME_H, idx + 2u);

    topos_frame_input in;
    memset(&in, 0, sizeof(in));
    in.struct_size = (uint32_t)sizeof(in);
    in.abi_version = TOPOS_CODEC_ABI_VERSION;
    in.planes[0] = y;
    in.planes[1] = u;
    in.planes[2] = v;
    in.strides[0] = (size_t)FRAME_W * 2u;
    in.strides[1] = (size_t)w2 * 2u;
    in.strides[2] = (size_t)w2 * 2u;

    size_t bound = tc_frame_packet_bound(&cfg);
    uint8_t* pkt = (uint8_t*)malloc(bound);
    topos_frame_stats stats;
    int32_t rc = TC_OK;
    if (pkt == NULL) {
        rc = TC_ERR_OUT_OF_MEMORY;
    } else {
        rc = tc_frame_encode(&cfg, &in, pkt, bound, &stats);
    }
    free(y); free(u); free(v);
    if (rc != TC_OK) { free(pkt); return rc; }
    *out = pkt;
    *out_size = stats.packet_size;
    return TC_OK;
}

/* —— mux：AV_CODEC_ID_NONE + codec_tag TPIC —— */
static int mux_case(const char* path, int faststart, int custom_meta,
                    uint8_t* const frames[FRAME_COUNT], const size_t sizes[FRAME_COUNT])
{
    AVFormatContext* oc = NULL;
    int rc = avformat_alloc_output_context2(&oc, NULL, "mov", path);
    if (rc < 0 || oc == NULL) {
        printf("  mux: alloc_output_context2 FAIL rc=%d\n", rc);
        return 1;
    }
    AVStream* st = avformat_new_stream(oc, NULL);
    if (st == NULL) { avformat_free_context(oc); return 1; }
    AVCodecParameters* par = st->codecpar;
    par->codec_type = AVMEDIA_TYPE_VIDEO;
    par->codec_id = AV_CODEC_ID_NONE;               /* S1 核心问题 */
    par->codec_tag = MKTAG('T', 'P', 'I', 'C');
    par->width = FRAME_W;
    par->height = FRAME_H;
    par->format = AV_PIX_FMT_NONE;
    st->time_base = (AVRational){ 1, FPS };
    st->avg_frame_rate = (AVRational){ FPS, 1 };
    st->sample_aspect_ratio = (AVRational){ 1, 1 };
    par->color_primaries = AVCOL_PRI_BT709;
    par->color_trc = AVCOL_TRC_BT709;
    par->color_space = AVCOL_SPC_BT709;
    par->color_range = AVCOL_RANGE_MPEG;
    par->chroma_location = AVCHROMA_LOC_LEFT; /* 4:2:2 co-sited 左 */

    AVDictionary* opts = NULL;
    if (faststart && custom_meta) {
        av_dict_set(&opts, "movflags", "+faststart+use_metadata_tags", 0);
    } else if (faststart) {
        av_dict_set(&opts, "movflags", "+faststart", 0);
    } else if (custom_meta) {
        av_dict_set(&opts, "movflags", "+use_metadata_tags", 0);
    }
    if (custom_meta) {
        av_dict_set(&oc->metadata, "tpcC", "010301010a010101", 0); /* 十六进制占位 */
        av_dict_set(&oc->metadata, "topos_version", "2.0-spike", 0);
    }

    rc = avio_open(&oc->pb, path, AVIO_FLAG_WRITE);
    if (rc < 0) {
        printf("  mux: avio_open FAIL rc=%d\n", rc);
        av_dict_free(&opts);
        avformat_free_context(oc);
        return 1;
    }
    rc = avformat_write_header(oc, &opts);
    if (rc < 0) {
        char err[AV_ERROR_MAX_STRING_SIZE] = { 0 };
        av_strerror(rc, err, sizeof(err));
        printf("  mux: write_header FAIL rc=%d (%s)  ← S1 答案：movenc 拒绝\n", rc, err);
        av_dict_free(&opts);
        avformat_free_context(oc);
        return 2; /* 结构性失败 */
    }
    for (int i = 0; i < FRAME_COUNT; ++i) {
        AVPacket* pkt = av_packet_alloc();
        av_new_packet(pkt, (int)sizes[i]);
        memcpy(pkt->data, frames[i], sizes[i]);
        pkt->stream_index = 0;
        /* movenc 会在 write_header 时提升 track timescale（24→12288）并改写
         * st->time_base —— pts 必须按 write_header 之后的 st->time_base 换算。 */
        int64_t t = av_rescale_q(i, (AVRational){ 1, FPS }, st->time_base);
        pkt->pts = t;
        pkt->dts = t;
        pkt->duration = av_rescale_q(1, (AVRational){ 1, FPS }, st->time_base);
        rc = av_interleaved_write_frame(oc, pkt);
        av_packet_free(&pkt);
        if (rc < 0) {
            printf("  mux: write packet %d FAIL rc=%d\n", i, rc);
            av_dict_free(&opts);
            av_write_trailer(oc);
            avio_closep(&oc->pb);
            avformat_free_context(oc);
            return 1;
        }
    }
    av_write_trailer(oc);
    av_dict_free(&opts);
    avio_closep(&oc->pb);
    avformat_free_context(oc);
    printf("  mux: OK (faststart=%d custom_meta=%d)\n", faststart, custom_meta);
    return 0;
}

/* —— demux：读回全部 packet 并与源逐字节比对 —— */
static int demux_verify(const char* path, uint8_t* const frames[FRAME_COUNT],
                        const size_t sizes[FRAME_COUNT])
{
    AVFormatContext* ic = NULL;
    int rc = avformat_open_input(&ic, path, NULL, NULL);
    if (rc < 0) {
        char err[AV_ERROR_MAX_STRING_SIZE] = { 0 };
        av_strerror(rc, err, sizeof(err));
        printf("  demux: open FAIL rc=%d (%s)\n", rc, err);
        return 1;
    }
    if (ic->nb_streams < 1) { printf("  demux: no streams\n"); avformat_close_input(&ic); return 1; }
    AVStream* st = ic->streams[0];
    unsigned tag = (unsigned)st->codecpar->codec_tag;
    printf("  demux: streams=%u tag=%c%c%c%c codec_id=%d dims=%dx%d nb_frames=%" PRId64 "\n",
           (unsigned)ic->nb_streams,
           (char)(tag >> 24), (char)(tag >> 16), (char)(tag >> 8), (char)tag,
           (int)st->codecpar->codec_id, st->codecpar->width, st->codecpar->height,
           st->nb_frames == AV_NOPTS_VALUE ? -1 : (int64_t)st->nb_frames);

    int idx = 0;
    int mismatches = 0;
    int64_t ticks_per_frame = av_rescale_q(1, (AVRational){ 1, FPS }, st->time_base);
    AVPacket* pkt = av_packet_alloc();
    while (av_read_frame(ic, pkt) >= 0) {
        if (pkt->stream_index == 0) {
            if (idx < FRAME_COUNT) {
                int64_t expect_pts = (int64_t)idx * ticks_per_frame;
                int bytes_ok = (pkt->size == (int)sizes[idx]) &&
                               (memcmp(pkt->data, frames[idx], sizes[idx]) == 0);
                int pts_ok = pkt->pts == expect_pts;
                if (!bytes_ok || !pts_ok) { ++mismatches; }
                printf("  pkt %d: size=%d expect=%zu pts=%" PRId64 " expect_pts=%" PRId64
                       " ticks/frame=%" PRId64 " bytes=%s pts=%s\n",
                       idx, pkt->size, sizes[idx], (int64_t)pkt->pts, expect_pts,
                       ticks_per_frame, bytes_ok ? "MATCH" : "MISMATCH",
                       pts_ok ? "MATCH" : "MISMATCH");
                ++idx;
            } else {
                ++mismatches;
            }
        }
        av_packet_unref(pkt);
    }
    av_packet_free(&pkt);
    if (idx != FRAME_COUNT) { printf("  demux: frame count %d != %d\n", idx, FRAME_COUNT); ++mismatches; }

    /* S5：自定义元数据读回 */
    AVDictionaryEntry* e = av_dict_get(ic->metadata, "tpcC", NULL, 0);
    printf("  meta tpcC = %s\n", e ? e->value : "(absent)");
    e = av_dict_get(ic->metadata, "topos_version", NULL, 0);
    printf("  meta topos_version = %s\n", e ? e->value : "(absent)");

    avformat_close_input(&ic);
    printf("  demux: %s (%d mismatches)\n", mismatches == 0 ? "PASS" : "FAIL", mismatches);
    return mismatches == 0 ? 0 : 1;
}

int main(int argc, char** argv)
{
    if (argc != 2) { fprintf(stderr, "usage: spike_movenc <out_dir>\n"); return 2; }

    uint8_t* frames[FRAME_COUNT];
    size_t sizes[FRAME_COUNT];
    for (int i = 0; i < FRAME_COUNT; ++i) {
        int32_t rc = encode_test_frame((uint32_t)i, &frames[i], &sizes[i]);
        if (rc != TC_OK) {
            fprintf(stderr, "frame %d encode failed: %d (%s)\n", i, (int)rc, tc_status_message(rc));
            return 1;
        }
        printf("frame %d: %zu bytes\n", i, sizes[i]);
    }

    char path[512];
    int failures = 0;

    printf("== case A: plain mov ==\n");
    snprintf(path, sizeof(path), "%s/spike_a.mov", argv[1]);
    failures += mux_case(path, 0, 0, frames, sizes) != 0;
    failures += demux_verify(path, frames, sizes) != 0;

    printf("== case B: +faststart ==\n");
    snprintf(path, sizeof(path), "%s/spike_b.mov", argv[1]);
    failures += mux_case(path, 1, 0, frames, sizes) != 0;
    failures += demux_verify(path, frames, sizes) != 0;

    printf("== case C: +faststart +use_metadata_tags ==\n");
    snprintf(path, sizeof(path), "%s/spike_c.mov", argv[1]);
    failures += mux_case(path, 1, 1, frames, sizes) != 0;
    failures += demux_verify(path, frames, sizes) != 0;

    printf("== summary: %s ==\n", failures == 0 ? "ALL SPIKE CHECKS PASSED" : "FAILURES PRESENT");
    for (int i = 0; i < FRAME_COUNT; ++i) { free(frames[i]); }
    return failures == 0 ? 0 : 1;
}
