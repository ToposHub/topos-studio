#include "deblock.h"

#include "../../include/topos_codec.h"
#include "../common/tpool.h"
#include "../simd/deblock_avx2.h"
#include "../simd/dispatch.h"
#include "../transform/quant.h"

#include <stdlib.h>

/* ---- 强度参数（spec v2 冻结值；调优走 A/B 战役并入档） ---- */
#define DB_ALPHA_MIN 3u
#define DB_ALPHA_MAX 48u
/* step_px = max(1, scale·3/173056)：DC 步 (scale>>4)/10816 × AC 增益 3。 */
#define DB_STEP_NUM 3u
#define DB_STEP_DEN 173056u
/* 弱滤波权重 d = (W0·(q0−p0) + W1·(p1−q1) + 8) >> 4，令 Δ = q0−p0。
 * 两个必须同时成立的端点约束（否则滤波器自己造伪影）：
 *   纯 DC 台阶（p1=p0=A, q0=q1=A+Δ，即 p1−q1 = −Δ）：要求 d = Δ/2
 *     ⇒ (W0 − W1)/16 = 1/2 ⇒ W0 − W1 = 8；
 *   均匀斜坡（斜率 s，p1−q1 = −3s，Δ = s）是**真实梯度**，滤波须恒等
 *     ⇒ (W0 − 3·W1)/16 = 0 ⇒ W0 = 3·W1。
 * 联立 ⇒ W1 = 4、W0 = 12：d = (12Δ + 4(p1−q1) + 8) >> 4 —— 台阶整段抹平、
 * 斜坡严格零响应。v1 的 (4,1) 同构（斜坡零响应）但台阶响应仅 3Δ/16 ≈ 19%，
 * 实测块界台阶只降 44%（与 ProRes 同输入对照仍差 4.3×）。 */
#define DB_W0 12
#define DB_W1 4
#define DB_ROUND 8u
#define DB_SHIFT 4u
/* 扩散：主校正 d 同时以 d>>DB_SPREAD_SHIFT 加到 p1/q1，把台阶从"边界一步"
 * 摊成 4 像素单调过渡。只劈开边界台阶而不扩散，等于把台阶搬进块内
 * （p1→p0 冒出新台阶，块效应指标转负 = 新的周期性条纹）；铺开后边界与
 * 块内差分同时趋零。0 = 关。 */
#define DB_SPREAD_SHIFT 1

#ifdef TC_DB_TUNE
#include <stdio.h>
#endif

/* 调优旋钮：默认即冻结值（DB_* 宏）。TC_DB_TUNE 构建可经环境变量覆盖，
 * 仅用于 A/B 扫参（不随发布构建发布；冻结后以宏值为唯一权威）。 */
static int32_t g_w0 = DB_W0;
static int32_t g_w1 = DB_W1;
static int32_t g_shift = (int32_t)DB_SHIFT;
static uint32_t g_alpha_max = DB_ALPHA_MAX;
static uint32_t g_step_num = DB_STEP_NUM;
static uint32_t g_step_den = DB_STEP_DEN;
static uint32_t g_spread = (uint32_t)DB_SPREAD_SHIFT;

#ifdef TC_DB_TUNE
static int g_tune_loaded = 0;
static void db_tune_load(void)
{
    if (g_tune_loaded) { return; }
    g_tune_loaded = 1;
    const char* e;
    if ((e = getenv("DB_W0")) != NULL) { g_w0 = atoi(e); }
    if ((e = getenv("DB_W1")) != NULL) { g_w1 = atoi(e); }
    if ((e = getenv("DB_SHIFT")) != NULL) { g_shift = atoi(e); }
    if ((e = getenv("DB_ALPHA_MAX")) != NULL) { g_alpha_max = (uint32_t)atoi(e); }
    if ((e = getenv("DB_STEP_NUM")) != NULL) { g_step_num = (uint32_t)atoi(e); }
    if ((e = getenv("DB_STEP_DEN")) != NULL) { g_step_den = (uint32_t)atoi(e); }
    if ((e = getenv("DB_SPREAD_SHIFT")) != NULL) { g_spread = (uint32_t)atoi(e); }
    fprintf(stderr, "[deblock tune] W0=%d W1=%d shift=%d alpha_max=%u "
            "step=%u/%u spread=%u\n", g_w0, g_w1, g_shift,
            g_alpha_max, g_step_num, g_step_den, g_spread);
}
#define DB_TUNE_INIT() db_tune_load()
#else
#define DB_TUNE_INIT() ((void)0)
#endif

static inline uint32_t deblock_alpha(uint32_t table_sel, uint32_t qp)
{
    const uint32_t scale = tc_qp_scale_tbl(table_sel, qp);
    uint32_t step = (scale * g_step_num + g_step_den / 2u) / g_step_den;
    if (step < 1u) { step = 1u; }
    uint32_t a = step * 2u;
    if (a < DB_ALPHA_MIN) { a = DB_ALPHA_MIN; }
    if (a > g_alpha_max) { a = g_alpha_max; }
    return a;
}

static inline int32_t db_clip3(int32_t v, int32_t lo, int32_t hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

static inline int32_t db_clamp(int32_t v, int32_t maxv)
{
    return v < 0 ? 0 : (v > maxv ? maxv : v);
}

/* 门控：m0 = |q0−p0|（跨边界台阶）、m1 = |p1−p0|、m2 = |q1−q0|（两侧局部
 * 起伏）。三者同时低于 alpha/beta 才判为量化台阶而非真实边缘/纹理。
 *
 * v2 曾以 (uint32_t)v < t 代替 |v| < t"省三条取绝对指令"——该等价式只在
 * v ≥ 0 时成立：负差转无符号是巨值、比较必然失败，门控因此退化成"只滤
 * q0 > p0 一侧"。反向台阶（q0 < p0，实测占 4K proxy 边界 45.2%、占帧内
 * 台阶能量 53.4%）全部漏滤，块效应残留近半。此处按幅值判，两侧同权。 */
static inline uint32_t db_absu(int32_t v)
{
    return (uint32_t)(v < 0 ? -v : v);
}

static inline int db_gate(int32_t p1, int32_t p0, int32_t q0, int32_t q1,
                          int32_t alpha, int32_t beta, int32_t maxv)
{
    const uint32_t step = db_absu(q0 - p0);
    /* The midpoint clamp gives d == 0 for steps of zero or one. Skip
     * those writes unless the old filter would also clamp a sample. */
    return (step < (uint32_t)alpha)
           && (step >= 2u || (p1 | p0 | q0 | q1) > maxv)
           && (db_absu(p1 - p0) < (uint32_t)beta)
           && (db_absu(q1 - q0) < (uint32_t)beta);
}

/* 校正量 d = (W0·Δ + W1·(p1−q1) + 8) >> 4（Δ = q0−p0），双重上界：
 *  1) |d| ≤ clip（= alpha/2，量化步长域）；
 *  2) |d| ≤ |Δ|/2 且 sign(d) = sign(Δ)——**不得越过 p0/q0 的中点**。
 * 第 2 条是必需的：纯台阶邻域下 (12,4) 恰好给 d = Δ/2（台阶整段抹平），
 * 但在**振铃/过冲**邻域（p1−q1 与 Δ 同号——线条边缘经 DCT 量化后极常见）
 * 权重和可让 d 超过 Δ，p0 越过 q0 把台阶**翻到另一侧**：幅度不减、位置
 * 互换，实测在高细节帧上块效应指标反而恶化（逐帧 block_v 4.31→7.04）。
 * 中点上界保证滤波是"单调收敛"而非振荡，同时保留纯台阶的满响应。 */
static inline int32_t db_delta(int32_t p1, int32_t p0, int32_t q0, int32_t q1,
                               int32_t clip)
{
    int32_t d = (g_w0 * (q0 - p0) + g_w1 * (p1 - q1)
                 + (int32_t)DB_ROUND) >> g_shift;
    const int32_t delta = q0 - p0;
    const int32_t half = (delta >= 0) ? (delta >> 1) : -((-delta) >> 1);
    if (half >= 0) {
        if (d > half) { d = half; }
        if (d < 0) { d = 0; }
    } else {
        if (d < half) { d = half; }
        if (d > 0) { d = 0; }
    }
    return db_clip3(d, -clip, clip);
}

/* 就地施加一次校正：p0 += d、q0 −= d、p1 += d>>s、q1 −= d>>s（d 已钳位）。
 * 相邻边界的依赖窗口（{x0−2..x0+1} 与 {x0+6..x0+9}）不相交，故同趟内各
 * 边界读取的都是原始像素——"逐边界就地更新"与"先算后统一写回"逐位等价。
 *
 * 保持"门控不过就一个像素都不碰"的分支写法。曾试过无分支 select 版（把
 * gate ? clamp(...) : 原值 写成 select，好让 clang 向量化 H 趟——H 趟确实
 * 向量化了，输出也逐位一致），但**实测无收益**：同一码流的重复测量在本机
 * 有 ±12% 噪声（同二进制 2K proxy 16 线程 296–338 fps），两版落在这个带内，
 * 分不出差别；而无条件计算要在门控稀疏处白算 apply。故保留标量分支版，
 * 若要真做 SIMD，必须走"保留门控跳过的掩码/早退路径"（见 §5 SIMD 计划）。 */
static inline void db_apply(uint16_t* p1, uint16_t* p0, uint16_t* q0,
                            uint16_t* q1, int32_t d, int32_t maxv)
{
    *p0 = (uint16_t)db_clamp((int32_t)*p0 + d, maxv);
    *q0 = (uint16_t)db_clamp((int32_t)*q0 - d, maxv);
    if (g_spread != 0u) {
        const int32_t ds = d >> g_spread;
        *p1 = (uint16_t)db_clamp((int32_t)*p1 + ds, maxv);
        *q1 = (uint16_t)db_clamp((int32_t)*q1 - ds, maxv);
    }
}

/* 垂直边界：**逐行**遍历（行主序连续访问），每行只走 8 对齐的边界列。
 * 首版按"块行带 × 8 行跨 stride"遍历（8 行跨 stride 的零散访问 + 每点一个
 * 数据相关分支），是二倍以上解码开销的来源；逐行扫描把内层工作集压到一行。
 * 每行边界列间隔 8、依赖窗口互不重叠，故可就地更新。 */
static void deblock_v_pass(uint16_t* pix, int32_t stride, uint32_t w,
                           uint32_t h, uint32_t unit,
                           int32_t alpha, int32_t beta,
                           int32_t clip, int32_t maxv, int avx2)
{
    (void)avx2;
    for (uint32_t y = 0u; y < h; ++y) {
        uint16_t* row = pix + (size_t)y * (size_t)stride;
        uint32_t x0 = unit;
#if defined(__x86_64__) || defined(_M_X64)
        if (avx2 && unit == 8u) {
            x0 = tc_deblock_vertical_row_avx2(row, w, alpha, beta, clip, maxv);
        }
#endif
        for (; x0 + 1u < w; x0 += unit) {
            const int32_t p1 = row[x0 - 2], p0 = row[x0 - 1];
            const int32_t q0 = row[x0], q1 = row[x0 + 1];
            if (db_gate(p1, p0, q0, q1, alpha, beta, maxv)) {
                db_apply(row + x0 - 2, row + x0 - 1, row + x0, row + x0 + 1,
                         db_delta(p1, p0, q0, q1, clip), maxv);
            }
        }
    }
}

/* 水平边界：整条边界上**每个 x 都是边界位置**，故一趟平坦扫描覆盖全部列
 * （无 8 对齐掩码、无无效位置），循环体内无跨行分支，便于向量化。
 * 跨水平边界的样本序列沿**垂直**方向：p1 在 p0 上一行、q1 在 q0 下一行。
 * v1 误取 pu[x−1]/qu[x+1]（同一行内相邻列）——校正方向虽对，但"台阶 vs
 * 斜坡"判别用了与边界无关的横向抽头，水平边界台阶因此降不下去（A/B：
 * block_h 已归零而 block_v 仍有 1.2–1.5）。y0 是 8 的倍数且 ≥ 8，故
 * y0−2 ≥ 6、y0+1 ≤ h−7 恒在界内。
 * r_begin/r_end 限定处理的边界行区间（并行切片用）：各边界行的读写窗口
 * {y0−2..y0+1} 相隔 unit、互不相交（unit ≥ 4 时），故任意区间划分都与串行
 * 逐位一致。 */
static void deblock_h_range(uint16_t* pix, int32_t stride, uint32_t w,
                            uint32_t unit, uint32_t r_begin, uint32_t r_end,
                            int32_t alpha, int32_t beta, int32_t clip,
                            int32_t maxv, int avx2)
{
    (void)avx2;
    for (uint32_t r = r_begin; r < r_end; ++r) {
        const uint32_t y0 = r * unit;
        uint16_t* pu2 = pix + (size_t)(y0 - 2u) * (size_t)stride;
        uint16_t* pu = pix + (size_t)(y0 - 1u) * (size_t)stride;
        uint16_t* qu = pix + (size_t)y0 * (size_t)stride;
        uint16_t* qu2 = pix + (size_t)(y0 + 1u) * (size_t)stride;
        /* 整行稠密 stencil：每个 x 都跨同一条边界，逐 x 互不相干 */
        uint32_t x = 0u;
#if defined(__x86_64__) || defined(_M_X64)
        if (avx2) {
            x = tc_deblock_horizontal_row_avx2(pu2, pu, qu, qu2, w, alpha, beta, clip, maxv);
        }
#endif
        for (; x < w; ++x) {
            const int32_t p1 = pu2[x], p0 = pu[x];
            const int32_t q0 = qu[x], q1 = qu2[x];
            if (db_gate(p1, p0, q0, q1, alpha, beta, maxv)) {
                db_apply(pu2 + x, pu + x, qu + x, qu2 + x,
                         db_delta(p1, p0, q0, q1, clip), maxv);
            }
        }
    }
}

/* A horizontal edge at row r * unit reads and writes four rows
 * [r * unit - 2, r * unit + 2). Split bands just before that window.
 * Vertical filtering only touches its own row. Each band can therefore
 * finish both passes locally, with the same per-pixel ordering as a
 * whole-plane vertical pass followed by a whole-plane horizontal pass. */
#define DB_PAR_MIN_SAMPLES (64u * 1024u)
#define DB_MAX_PLANES 3u
#define DB_BANDS_PER_WORKER 2u
#define DB_MAX_BANDS (DB_BANDS_PER_WORKER * TC_SLICE_MAX_THREADS)

typedef struct db_band {
    const tc_deblock_plane_view* plane;
    uint32_t first_row;
    uint32_t end_row;
    uint32_t first_edge;
    uint32_t end_edge;
    uint32_t unit;
    int32_t alpha;
    int32_t beta;
    int32_t clip;
    int32_t maxv;
} db_band;

static void db_band_run(void* ctx)
{
    const db_band* band = (const db_band*)ctx;
    const tc_deblock_plane_view* plane = band->plane;
    int avx2 = 0;
#if (defined(__x86_64__) || defined(_M_X64)) && !defined(TC_DB_TUNE)
    avx2 = band->maxv <= 4095 && tc_simd_avx2_enabled();
#endif
    deblock_v_pass(plane->pix + (size_t)band->first_row * (size_t)plane->stride,
                   plane->stride, plane->width,
                   band->end_row - band->first_row, band->unit,
                   band->alpha, band->beta, band->clip, band->maxv, avx2);
    deblock_h_range(plane->pix, plane->stride, plane->width, band->unit,
                    band->first_edge, band->end_edge, band->alpha,
                    band->beta, band->clip, band->maxv, avx2);
}

void tc_deblock_slice_band(const tc_deblock_plane_view* plane, uint32_t block_y,
                           uint32_t block_rows, uint32_t qp, uint32_t table_sel,
                           uint8_t bit_depth, int boundary_only)
{
    const uint32_t unit = 8u;
    const uint32_t w = plane->width, h = plane->height;
    if (plane->pix == NULL || plane->stride <= 0 || (uint32_t)plane->stride < w ||
        qp > TC_QP_MAX || bit_depth < 8u || bit_depth > 16u ||
        w < 2u * unit || h < 2u * unit || w % unit != 0u || h % unit != 0u) {
        return;
    }
    const uint32_t visible_blocks = h / unit;
    if (block_y >= visible_blocks || (boundary_only && block_y == 0u)) { return; }
    const uint32_t remaining = visible_blocks - block_y;
    const uint32_t end_block = block_y + (block_rows < remaining ? block_rows : remaining);
    const int32_t alpha = (int32_t)deblock_alpha(table_sel, qp);
    db_band band = {
        plane,
        boundary_only ? 0u : block_y * unit,
        boundary_only ? 0u : end_block * unit,
        boundary_only ? block_y : block_y + 1u,
        boundary_only ? block_y + 1u : end_block,
        unit, alpha, alpha / 2 + 2, alpha >> 1,
        (int32_t)((1u << bit_depth) - 1u),
    };
    db_band_run(&band);
}

static uint32_t deblock_worker_count(uint32_t max_workers)
{
    const uint32_t workers = max_workers != 0u ? max_workers : (uint32_t)tc_dev_thread_count();
    return workers <= TC_SLICE_MAX_THREADS ? workers : TC_SLICE_MAX_THREADS;
}

static uint32_t deblock_enqueue_planes(const tc_deblock_plane_view* planes, uint32_t count,
                                       uint32_t qp, uint32_t table_sel, uint8_t bit_depth,
                                       uint32_t unit, uint32_t workers,
                                       tc_job* jobs, db_band* bands, uint32_t job_count)
{
    if (planes == NULL || count == 0u || count > DB_MAX_PLANES || qp > TC_QP_MAX ||
        bit_depth < 8u || bit_depth > 16u || unit < 4u || unit > 8u) {
        return job_count;
    }

    DB_TUNE_INIT();
    const int32_t alpha = (int32_t)deblock_alpha(table_sel, qp);
    const int32_t beta = alpha / 2 + 2;
    const int32_t clip = alpha >> 1;
    const int32_t maxv = (int32_t)((1u << bit_depth) - 1u);
    for (uint32_t p = 0; p < count; ++p) {
        const tc_deblock_plane_view* plane = &planes[p];
        const uint32_t w = plane->width;
        const uint32_t h = plane->height;
        if (plane->pix == NULL || plane->stride <= 0 || (uint32_t)plane->stride < w ||
            w < 2u * unit || h < 2u * unit ||
            w % unit != 0u || h % unit != 0u) {
            continue;
        }
        const uint32_t edges = h / unit - 1u;
        uint32_t band_count = workers <= 1u || (uint64_t)w * h < DB_PAR_MIN_SAMPLES
            ? 1u : workers * DB_BANDS_PER_WORKER;
        if (band_count > edges) { band_count = edges; }
        for (uint32_t i = 0; i < band_count; ++i) {
            const uint32_t first_edge = 1u + (uint32_t)(((uint64_t)edges * i) / band_count);
            const uint32_t end_edge = 1u + (uint32_t)(((uint64_t)edges * (i + 1u)) / band_count);
            db_band band = {
                plane, i == 0u ? 0u : first_edge * unit - 2u,
                i + 1u == band_count ? h : end_edge * unit - 2u,
                first_edge, end_edge, unit, alpha, beta, clip, maxv,
            };
            if (band_count == 1u) {
                db_band_run(&band);
            } else {
                bands[job_count] = band;
                jobs[job_count] = (tc_job){db_band_run, &bands[job_count]};
                job_count++;
            }
        }
    }
    return job_count;
}

static void deblock_planes_unit(const tc_deblock_plane_view* planes, uint32_t count,
                                uint32_t qp, uint32_t table_sel, uint8_t bit_depth,
                                uint32_t unit, uint32_t max_workers)
{
    const uint32_t workers = deblock_worker_count(max_workers);
    tc_job jobs[DB_MAX_PLANES * DB_MAX_BANDS];
    db_band bands[DB_MAX_PLANES * DB_MAX_BANDS];
    const uint32_t job_count = deblock_enqueue_planes(
        planes, count, qp, table_sel, bit_depth, unit, workers, jobs, bands, 0u);
    if (job_count != 0u) {
        (void)tc_parallel_for(jobs, job_count, workers);
    }
}

void tc_deblock_planes(const tc_deblock_plane_view* planes, uint32_t count,
                       uint32_t qp, uint32_t table_sel, uint8_t bit_depth,
                       uint32_t max_workers)
{
    deblock_planes_unit(planes, count, qp, table_sel, bit_depth, 8u, max_workers);
}

void tc_deblock_frames(const tc_deblock_frame_view* frames, uint32_t count,
                      uint32_t max_workers)
{
    if (frames == NULL || count == 0u || count > TC_DEBLOCK_BATCH_SIZE) { return; }
    if (count == 1u) {
        const tc_deblock_frame_view* frame = &frames[0];
        tc_deblock_planes(frame->planes, frame->plane_count, frame->qp,
                          frame->table_sel, frame->bit_depth, max_workers);
        return;
    }

    const uint32_t workers = deblock_worker_count(max_workers);
    tc_job jobs[TC_DEBLOCK_BATCH_SIZE * DB_MAX_PLANES * DB_MAX_BANDS];
    db_band bands[TC_DEBLOCK_BATCH_SIZE * DB_MAX_PLANES * DB_MAX_BANDS];
    uint32_t job_count = 0u;
    for (uint32_t i = 0u; i < count; ++i) {
        const tc_deblock_frame_view* frame = &frames[i];
        job_count = deblock_enqueue_planes(frame->planes, frame->plane_count,
                                           frame->qp, frame->table_sel, frame->bit_depth,
                                           8u, workers, jobs, bands, job_count);
    }
    if (job_count != 0u) {
        (void)tc_parallel_for(jobs, job_count, workers);
    }
}

void tc_deblock_plane(uint16_t* pix, int32_t stride_pix, uint32_t w, uint32_t h,
                      uint32_t qp, uint32_t table_sel, uint8_t bit_depth)
{
    const tc_deblock_plane_view plane = {pix, stride_pix, w, h};
    tc_deblock_planes(&plane, 1u, qp, table_sel, bit_depth, 0u);
}

void tc_deblock_plane_scaled(uint16_t* pix, int32_t stride_pix, uint32_t w,
                             uint32_t h, uint32_t qp, uint32_t table_sel,
                             uint8_t bit_depth, uint32_t unit)
{
    const tc_deblock_plane_view plane = {pix, stride_pix, w, h};
    deblock_planes_unit(&plane, 1u, qp, table_sel, bit_depth, unit, 0u);
}
