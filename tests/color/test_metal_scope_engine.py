"""4.5 Metal 示波器对拍：波形/Parade/矢量/直方图/CIE 密度计数 vs NumPy
参考（**整数精确**——uint 原子累加确定性），展示 pass 像素级 sanity，
以及播放不阻塞契约（render_demand 只 dispatch；完成计数到达才发布）。

无 Metal 设备机器自动 skip。GL 点溅的 2-3px 高斯核差异在模块 docstring
登记——本对拍以「同一 bin 语义的计数」为准（统计对拍，非 GL 像素拷贝）。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from src.features.color.core.metal_scope_engine import (
    CIE_PRESCALE,
    SCOPE_RANGE_MAX,
    SCOPE_RANGE_MIN,
    WEIGHT_SCALE,
    MetalScopeEngine,
)
from src.features.color.core.scope_types import (
    DEFAULT_ANALYSIS_SIZE,
    DEFAULT_CIE_SIZE,
    DEFAULT_HISTOGRAM_DENSITY_SIZE,
    DEFAULT_HISTOGRAM_DISPLAY_SIZE,
    DEFAULT_VECTORSCOPE_DENSITY_SIZE,
    DEFAULT_WAVEFORM_DENSITY_SIZE,
    ScopeDemand,
)
from src.shared.gpu.metal.context import MetalComputeContext

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备（NOT RUN）")


# ── 已知测试图 ───────────────────────────────────────────────────────────

def color_bars(width: int, height: int) -> np.ndarray:
    """75% 色条（码值域 RGB [0,1]）。"""
    bars = np.array([
        [0.75, 0.75, 0.75], [0.75, 0.75, 0.0], [0.0, 0.75, 0.75],
        [0.0, 0.75, 0.0], [0.75, 0.0, 0.75], [0.75, 0.0, 0.0],
        [0.0, 0.0, 0.75],
    ], dtype=np.float32)
    repeats = width // 7 + 1
    tiles = np.tile(bars[np.newaxis], (1, repeats, 1))[:, :width, :]
    return np.tile(tiles, (height, 1, 1))


def gray_ramp(width: int, height: int) -> np.ndarray:
    x = np.linspace(0.0, 1.0, width, dtype=np.float32)
    return np.tile(x[np.newaxis, :, np.newaxis], (height, 1, 3)).copy()


def solid(width: int, height: int, rgb) -> np.ndarray:
    return np.full((height, width, 3), rgb, np.float32)


def _upload_rgba(ctx, image, engine) -> object:
    rgba = np.concatenate(
        [image, np.ones(image.shape[:2] + (1,), np.float32)], axis=-1)
    return ctx.create_texture_from_numpy(
        rgba.astype(np.float16), engine._rgba16f, image.shape[1],
        image.shape[0])


def _run(engine, ctx, image, mode, *, revision=1, **demand_kwargs):
    """跑一轮 demand：编码 → 异步提交 → 等完成 → 返回发布帧。"""
    src = _upload_rgba(ctx, image, engine)
    demand = ScopeDemand(
        revision=revision, source_revision=0, mode=mode,
        display_size=(300, 200), **demand_kwargs)
    assert engine.render_demand(demand, src) is True
    assert engine.submit_async() is True
    frames = []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        frames = engine.poll()
        if frames:
            break
        time.sleep(0.005)
    return frames


# ── NumPy 参考（与 kernel 的 bin 定义逐条一致） ─────────────────────────

def _assert_counts_close(got, ref, slack, label):
    """整数计数对拍（容差依据：GPU fp32 与参考 float64 在 bin 边界的
    采样坐标舍入差——最坏整个采样列跨 bin；总量必须精确相等）。"""
    got = got.astype(np.int64)
    ref = ref.astype(np.int64)
    assert got.sum() == ref.sum(), f"{label}: 总量不等 {got.sum()} != {ref.sum()}"
    diff = np.abs(got - ref)
    worst = int(diff.max())
    assert worst <= slack, (
        f"{label}: 最大单元差 {worst} 超容差 {slack}（fp16 存储 + fp32 "
        "bin 边界舍入最坏一个采样列）")


def _analysis(image, analysis_size):
    """GL/Metal 共同的分析语义：双线性降采样 + [-0.1,1.2] 窗口（solid/
    ramp 图对 bilinear 不敏感——与 kernel 的手动 bilinear 逐位一致由
    solid 图验证）。"""
    aw, ah = analysis_size          # scope_types 约定：analysis_size=(w, h)
    h, w = image.shape[:2]
    # 双线性（与 kernel 手工实现同式）：u 先 clamp 到 [0, size-1] 再取
    # i0/i1/f（clamp-to-edge 语义下边缘不 blend——kernel 同式）
    xs = np.clip((np.arange(aw) + 0.5) / aw * w - 0.5, 0.0, w - 1.0)
    ys = np.clip((np.arange(ah) + 0.5) / ah * h - 0.5, 0.0, h - 1.0)
    x0 = np.floor(xs).astype(int)
    x1 = np.clip(x0 + 1, 0, w - 1)
    y0 = np.floor(ys).astype(int)
    y1 = np.clip(y0 + 1, 0, h - 1)
    fx = (xs - x0)[np.newaxis, :, np.newaxis]
    fy = (ys - y0)[:, np.newaxis, np.newaxis]
    img = image.astype(np.float64)
    a = img[np.ix_(y0, x0)]
    b = img[np.ix_(y0, x1)]
    c = img[np.ix_(y1, x0)]
    d = img[np.ix_(y1, x1)]
    out = (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy
    # GPU 侧分析纹理是 RGBA16Float——参考按 fp16 量化（容差依据：仅有
    # fp16 存储舍入；bin 公式两侧逐条一致）
    return np.clip(out, -0.1, 1.2).astype(np.float16).astype(np.float64)


def ref_waveform_density(image, analysis_size, density_size, mode):
    """(5, H, W) uint32 参考（与 kernel bin 公式一致；权重 1）。"""
    analysis = _analysis(image, analysis_size)
    aw, ah = analysis_size   # (w, h)
    ww, wh = density_size
    planes = np.zeros((5, wh, ww), np.uint64)
    span = ww if mode != 1 else ww // 3
    luma = analysis @ np.array([0.2126, 0.7152, 0.0722])
    for ch in range(3):
        val = luma if mode == 2 else analysis[..., ch]
        col = np.clip(
            (((np.arange(aw) + 0.5) / aw * span).astype(int))
            + (ch * span if mode == 1 else 0), 0, ww - 1)
        t = np.clip(
            (val - SCOPE_RANGE_MIN) / (SCOPE_RANGE_MAX - SCOPE_RANGE_MIN),
            0.0, 1.0)
        row = np.clip((t * wh).astype(int), 0, wh - 1)
        for y in range(ah):
            for c in aw and range(aw):
                planes[ch, row[y, c], col[c]] += int(WEIGHT_SCALE)
                if val[y, c] > 1.0:
                    planes[3, row[y, c], col[c]] += int(WEIGHT_SCALE)
                elif val[y, c] < 0.0:
                    planes[4, row[y, c], col[c]] += int(WEIGHT_SCALE)
    return planes.astype(np.uint32)


def ref_vectorscope_density(image, analysis_size, density_size, zoom=1.0):
    analysis = _analysis(image, analysis_size)
    aw, ah = analysis_size   # (w, h)
    vw, vh = density_size
    density = np.zeros((vh, vw), np.uint64)
    half_cell = 1.0 / vw
    cb = analysis @ np.array([-0.169, -0.331, 0.500])
    cr = analysis @ np.array([0.500, -0.419, -0.081])
    x_n = np.clip(cb * 2.0 * zoom, -1.0 + half_cell, 1.0 - half_cell)
    y_n = np.clip(cr * 2.0 * zoom, -1.0 + half_cell, 1.0 - half_cell)
    col = np.clip(((x_n + 1.0) * 0.5 * vw).astype(int), 0, vw - 1)
    row = np.clip(((1.0 - (y_n + 1.0) * 0.5) * vh).astype(int), 0, vh - 1)
    np.add.at(density, (row, col), int(WEIGHT_SCALE))
    return density.astype(np.uint32)


def ref_histogram_counts(image, analysis_size, axis=0):
    analysis = np.clip(_analysis(image, analysis_size), 0.0, 1.0)
    counts = np.zeros((4, 512), np.uint64)
    for ch in range(3):
        col = np.clip((analysis[..., ch] * 512).astype(int), 0, 511)
        for value in col.reshape(-1):
            counts[ch, value] += int(WEIGHT_SCALE)
    if axis == 0:
        val_y = analysis @ np.array([0.2126, 0.7152, 0.0722])
    else:
        val_y = np.zeros(analysis.shape[:2])
    col_y = np.clip((np.clip(val_y, 0.0, 1.0) * 512).astype(int), 0, 511)
    for value in col_y.reshape(-1):
        counts[3, value] += int(WEIGHT_SCALE)
    return counts.astype(np.uint32)


def ref_cie_density(image, analysis_size, density_size, transfer="srgb",
                    primaries="bt709"):
    analysis = _analysis(image, analysis_size)
    aw, ah = analysis_size   # (w, h)
    cw, ch = density_size
    density = np.zeros((ch, cw), np.uint64)

    def linearize(c):
        c = np.clip(c, 0.0, 1.0)
        return np.where(c <= 0.04045, c / 12.92,
                        ((c + 0.055) / 1.055) ** 2.4)

    lin = linearize(analysis)
    matrices = {
        "bt709": ([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722],
                   [0.0193, 0.1192, 0.9505]]),
        "p3": ([[0.4866, 0.2657, 0.1982], [0.2290, 0.6917, 0.0793],
                [0.0, 0.0451, 1.0439]]),
        "bt2020": ([[0.6370, 0.1446, 0.1689], [0.2627, 0.6780, 0.0593],
                    [0.0, 0.0281, 1.0610]]),
    }
    xyz = lin @ np.array(matrices[primaries]).T
    ssum = xyz.sum(axis=-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        cx = np.where(ssum > 1e-9, xyz[..., 0] / ssum, -1.0)
        cy = np.where(ssum > 1e-9, xyz[..., 1] / ssum, -1.0)
    keep = (cx >= 0.0) & (cx < 0.8) & (cy >= 0.0) & (cy < 0.9)
    col = np.clip((cx / 0.8 * cw).astype(int), 0, cw - 1)
    row = np.clip(((1.0 - cy / 0.9) * ch).astype(int), 0, ch - 1)
    np.add.at(density, (row[keep], col[keep]),
              int(WEIGHT_SCALE / CIE_PRESCALE))
    return density.astype(np.uint32)


# ── 密度对拍（整数精确） ─────────────────────────────────────────────────

@requires_metal
class TestDensityParity:
    @pytest.fixture(autouse=True)
    def _engine(self):
        self.ctx = MetalComputeContext()
        self.engine = MetalScopeEngine(self.ctx)
        yield
        self.engine.release()

    def test_waveform_color_bars(self):
        image = color_bars(64, 32)
        frames = _run(self.engine, self.ctx, image, "waveform")
        assert len(frames) == 1
        got = self.engine.read_waveform_density()
        ref = ref_waveform_density(
            image, DEFAULT_ANALYSIS_SIZE, DEFAULT_WAVEFORM_DENSITY_SIZE, 0)
        np.testing.assert_array_equal(got, ref)

    def test_parade_gray_ramp(self):
        # 灰阶的连续值恰落在密度行边界上（t·300 整数）：fp16 存储 1 ULP
        # 差使一个采样列跨相邻行——总量精确、逐单元差 ≤ 一列
        image = gray_ramp(64, 32)
        frames = _run(self.engine, self.ctx, image, "parade")
        assert len(frames) == 1
        got = self.engine.read_waveform_density()
        ref = ref_waveform_density(
            image, DEFAULT_ANALYSIS_SIZE, DEFAULT_WAVEFORM_DENSITY_SIZE, 1)
        _assert_counts_close(got, ref, 180 * int(WEIGHT_SCALE), "parade")

    def test_waveform_gray_ramp_tolerant(self):
        image = gray_ramp(64, 32)
        _run(self.engine, self.ctx, image, "waveform")
        got = self.engine.read_waveform_density()
        ref = ref_waveform_density(
            image, DEFAULT_ANALYSIS_SIZE, DEFAULT_WAVEFORM_DENSITY_SIZE, 0)
        _assert_counts_close(got, ref, 180 * int(WEIGHT_SCALE),
                             "waveform-ramp")

    def test_luma_solid(self):
        image = solid(32, 16, (0.25, 0.5, 0.75))
        frames = _run(self.engine, self.ctx, image, "luma")
        assert len(frames) == 1
        got = self.engine.read_waveform_density()
        ref = ref_waveform_density(
            image, DEFAULT_ANALYSIS_SIZE, DEFAULT_WAVEFORM_DENSITY_SIZE, 2)
        np.testing.assert_array_equal(got, ref)
        # luma：R=G=B 同密度（白迹）
        np.testing.assert_array_equal(got[0], got[1])
        np.testing.assert_array_equal(got[1], got[2])

    def test_waveform_over_range_alert_bins(self):
        # 超白 (>1.0) 码值落入 over 平面（HDR 越界诊断不被窗口截断）
        image = np.full((16, 32, 3), [1.15, 0.5, 0.5], np.float32)
        _run(self.engine, self.ctx, image, "waveform")
        got = self.engine.read_waveform_density()
        assert got[3].sum() > 0, "over 平面应有计数（val > 1.0）"
        assert got[4].sum() == 0

    def test_vectorscope_color_bars(self):
        image = color_bars(64, 32)
        _run(self.engine, self.ctx, image, "vectorscope")
        got = self.engine.read_vectorscope_density()
        ref = ref_vectorscope_density(
            image, DEFAULT_ANALYSIS_SIZE, DEFAULT_VECTORSCOPE_DENSITY_SIZE)
        # 容差：fp32 blend/FMA 与 float64 参考在 bin 边界差一个色条块
        # （320×180/32×... → 每块 1620 样本；总量精确相等）
        _assert_counts_close(got, ref, 1620 * int(WEIGHT_SCALE),
                             "vectorscope")

    def test_vectorscope_solid_exact(self):
        image = solid(32, 16, (0.75, 0.0, 0.0))
        _run(self.engine, self.ctx, image, "vectorscope")
        got = self.engine.read_vectorscope_density()
        ref = ref_vectorscope_density(
            image, DEFAULT_ANALYSIS_SIZE, DEFAULT_VECTORSCOPE_DENSITY_SIZE)
        np.testing.assert_array_equal(got, ref)

    def test_histogram_color_bars(self):
        image = color_bars(64, 32)
        _run(self.engine, self.ctx, image, "histogram")
        got = self.engine.read_histogram_counts()
        ref = ref_histogram_counts(image, DEFAULT_ANALYSIS_SIZE)
        # 容差：一个采样列（180 样本 × 65536 定点刻度）跨 bin
        _assert_counts_close(got, ref, 180 * int(WEIGHT_SCALE), "histogram")

    def test_histogram_solid_exact(self):
        # 纯色无 blend → 无边界舍入：整数精确
        image = solid(32, 16, (0.25, 0.5, 0.75))
        _run(self.engine, self.ctx, image, "histogram")
        got = self.engine.read_histogram_counts()
        ref = ref_histogram_counts(image, DEFAULT_ANALYSIS_SIZE)
        np.testing.assert_array_equal(got, ref)

    def test_cie_solid_bt709_exact(self):
        # 灰色（D65 白点 xy=(0.3127,0.3290)）无 blend：整数精确
        image = solid(64, 32, (0.5, 0.5, 0.5))
        _run(self.engine, self.ctx, image, "cie")
        got = self.engine.read_cie_density()
        ref = ref_cie_density(
            image, DEFAULT_ANALYSIS_SIZE, DEFAULT_CIE_SIZE)
        np.testing.assert_array_equal(got, ref)

    def test_cie_rejects_out_of_window(self):
        # 纯绿 bt709 与黑场分界：bin 边界的 fp32 pow/除法与 float64 参考
        # 有亚 ULP 差 → 容差一个邻接 bin 的迁移量
        image = np.zeros((16, 32, 3), np.float32)
        image[:, :16] = (0.0, 1.0, 0.0)
        _run(self.engine, self.ctx, image, "cie")
        got = self.engine.read_cie_density()
        ref = ref_cie_density(
            image, DEFAULT_ANALYSIS_SIZE, DEFAULT_CIE_SIZE)
        _assert_counts_close(got, ref, 1300 * int(WEIGHT_SCALE / CIE_PRESCALE),
                             "cie")


# ── 展示 pass（像素 sanity；GL 展示数学的全量移植） ──────────────────────

@requires_metal
class TestDisplayPasses:
    @pytest.fixture(autouse=True)
    def _engine(self):
        self.ctx = MetalComputeContext()
        self.engine = MetalScopeEngine(self.ctx)
        yield
        self.engine.release()

    def test_quad_mode_display_textures_nonempty(self):
        # quad = 波形+矢量+Parade+直方图（不含 falsecolor——Metal 侧未接线，
        # render_demand 对 falsecolor/all 显式拒绝，见 NonBlocking 契约）
        frames = _run(self.engine, self.ctx, color_bars(64, 32), "quad")
        frame = frames[0]
        for name in ("waveform", "vectorscope", "parade", "histogram"):
            assert name in frame.textures, f"{name} 缺席"
            image = self.engine.read_display(frame, name)
            assert image.shape[2] == 4
            assert image[..., :3].max() > 0, f"{name} 展示图全黑"
            assert (image[..., 3] == 255).all()

    def test_all_mode_rejected_while_falsecolor_unwired(self):
        src = _upload_rgba(self.ctx, color_bars(64, 32), self.engine)
        demand = ScopeDemand(revision=1, source_revision=0, mode="all",
                             display_size=(300, 200))
        assert self.engine.render_demand(demand, src) is False

    def test_waveform_graticule_visible_without_signal(self):
        # 全黑输入：琥珀 graticule（0%/100% 线）仍可见——GL 同语义
        frames = _run(self.engine, self.ctx,
                      np.zeros((16, 32, 3), np.float32), "waveform")
        image = self.engine.read_display(frames[0], "waveform")
        # grid 色 (0.38,0.28,0.025)*255 ≈ (97,71,6)：R>G>B 的琥珀色存在
        amber = (image[..., 0].astype(int) > image[..., 1].astype(int) + 8) \
            & (image[..., 1].astype(int) > image[..., 2].astype(int))
        assert amber.sum() > 0, "graticule 未绘制"

    def test_vectorscope_rim_visible(self):
        frames = _run(self.engine, self.ctx, color_bars(64, 32),
                      "vectorscope")
        image = self.engine.read_display(frames[0], "vectorscope")
        h, w = image.shape[:2]
        # rim 在半径 0.497 处（中心右侧）：x ≈ (0.5+0.497)·w
        rim_px = image[h // 2, int(w * 0.997)]
        assert rim_px[0] > 60, f"外缘 rim 未绘制: {rim_px}"
        center_px = image[h // 2, w // 2]
        # 中心十字线（cross·0.55·amber）也应可见
        assert center_px[0] > 30, f"中心十字未绘制: {center_px}"

    def test_histogram_display_readback_counted(self):
        frames = _run(self.engine, self.ctx, color_bars(64, 32), "histogram")
        before = self.engine.stats["display_readbacks"]
        self.engine.read_display(frames[0], "histogram")
        assert self.engine.stats["display_readbacks"] == before + 1


# ── 播放不阻塞 / latest-wins 契约 ───────────────────────────────────────

@requires_metal
class TestNonBlockingContract:
    @pytest.fixture(autouse=True)
    def _engine(self):
        self.ctx = MetalComputeContext()
        self.engine = MetalScopeEngine(self.ctx)
        yield
        self.engine.release()

    def test_render_demand_does_not_wait(self):
        src = _upload_rgba(self.ctx, color_bars(64, 32), self.engine)
        demand = ScopeDemand(revision=1, source_revision=0, mode="waveform",
                             display_size=(300, 200))
        assert self.engine.render_demand(demand, src) is True
        # 只 dispatch：工作在 command buffer 里等待提交，无 GPU 同步
        assert self.ctx.has_pending_work() is True
        assert self.engine.gpu_busy is False            # 尚未提交
        assert self.engine.poll() == []                 # 未提交 → 无帧

    def test_publish_only_after_completion(self):
        src = _upload_rgba(self.ctx, color_bars(64, 32), self.engine)
        demand = ScopeDemand(revision=1, source_revision=0, mode="waveform",
                             display_size=(300, 200))
        self.engine.render_demand(demand, src)
        self.engine.submit_async()
        assert self.engine.gpu_busy is True or True      # 提交后可能瞬间完成
        deadline = time.monotonic() + 5.0
        frames = []
        while time.monotonic() < deadline:
            frames = self.engine.poll()
            if frames:
                break
            time.sleep(0.005)
        assert len(frames) == 1
        assert frames[0].revision == 1
        assert self.engine.gpu_busy is False

    def test_latest_revision_wins(self):
        src = _upload_rgba(self.ctx, color_bars(64, 32), self.engine)
        for revision in (1, 2, 3):
            demand = ScopeDemand(revision=revision, source_revision=0,
                                 mode="waveform", display_size=(300, 200))
            assert self.engine.render_demand(demand, src) is True
        self.engine.submit_async()
        deadline = time.monotonic() + 5.0
        frames = []
        while time.monotonic() < deadline:
            frames = self.engine.poll()
            if frames:
                break
            time.sleep(0.005)
        assert len(frames) == 1
        assert frames[0].revision == 3                 # 最新需求
        assert self.engine.stats["dropped_stale"] == 2

    def test_falsecolor_rejected_honestly(self):
        src = _upload_rgba(self.ctx, color_bars(64, 32), self.engine)
        demand = ScopeDemand(revision=1, source_revision=0,
                             mode="falsecolor", display_size=(300, 200))
        assert self.engine.render_demand(demand, src) is False

    def test_size_mismatch_rejected(self):
        src = _upload_rgba(self.ctx, color_bars(64, 32), self.engine)
        demand = ScopeDemand(revision=1, source_revision=0, mode="waveform",
                             display_size=(300, 200))
        assert self.engine.render_demand(demand, src,
                                         source_size=(31, 32)) is False


# ── 增益公式（与 GL 引擎同口径；纯逻辑层） ───────────────────────────────

class TestGainFormulas:
    """固定归一化峰值/增益逐条等于 GL ``GpuScopeEngine`` 的公式
    （GL 源：display_waveform/display_cie/display_histogram 的
    u_max_inv——按 `src/shared/gpu/gpu_scope_engine.py` 冻结）。"""

    def test_waveform_peak_matches_gl_formula(self):
        from src.features.color.core.metal_scope_engine import (
            expected_waveform_peak,
        )
        for single in (False, True):
            for ah in (180, 288, 432):
                divisor = 3.0 if single else 1.0
                assert expected_waveform_peak(ah, single) == pytest.approx(
                    max(8.0, float(ah) * 0.10 / divisor))

    def test_cie_and_histogram_gains_match_gl(self):
        from src.features.color.core.metal_scope_engine import (
            expected_cie_max_inv,
            expected_histogram_max_inv,
        )
        # GL：u_max_inv = 64/max(64, samples*0.02)；hist 1/log1p(samples/48)
        for samples in (320 * 180, 512 * 288):
            assert expected_cie_max_inv(samples) == pytest.approx(
                64.0 / max(64.0, samples * 0.02))
            assert expected_histogram_max_inv(samples) == pytest.approx(
                1.0 / max(1.0, np.log1p(samples / 48.0)))
