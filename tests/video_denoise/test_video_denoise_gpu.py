"""视频降噪 GPU 测试（需要独立 ModernGL 上下文）。"""

from __future__ import annotations

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")

from src.shared.video.denoise.params import (
    SpatialDenoiseParams,
    TemporalDenoiseParams,
    VideoDenoiseParams,
)
from src.features.color.core.video_denoise_pipeline import VideoDenoisePipeline

pytestmark = pytest.mark.gpu_renderer


def _context():
    try:
        return moderngl.create_standalone_context()
    except Exception as exc:
        pytest.skip(f"standalone ModernGL context unavailable: {exc}")


def _noise_frame(ctx, w, h, seed, noise=0.08, moving_block=True):
    rng = np.random.default_rng(seed)
    base = np.zeros((h, w, 3), dtype=np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    base[..., 0] = 0.4 + 0.2 * np.sin(xx / 24.0 + seed)
    base[..., 1] = 0.45 + 0.15 * np.cos(yy / 31.0)
    base[..., 2] = 0.35 + 0.1 * np.sin((xx + yy) / 40.0)
    if moving_block:
        # 方块每帧移动 12px（4 帧后 48px > 方块宽 40px，旧位置完全腾空）
        bx, by = 40 + seed * 12, 30 + seed * 6
        base[by:by + 40, bx:bx + 40, :] = [0.9, 0.7, 0.2]
    noisy = np.clip(base + rng.normal(0, noise, (h, w, 3)), 0, 1)
    tex = ctx.texture((w, h), 4, dtype="f2")
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex.write(np.dstack(
        [noisy.astype(np.float16), np.ones((h, w, 1), dtype=np.float16)]
    ).tobytes())
    return tex


def _solid_frame(ctx, w, h, value):
    data = np.empty((h, w, 4), dtype=np.float16)
    data[..., :3] = np.asarray(value, dtype=np.float16)
    data[..., 3] = 1.0
    tex = ctx.texture((w, h), 4, dtype="f2")
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex.write(data.tobytes())
    return tex


def _read(ctx, tex):
    fbo = ctx.framebuffer([tex])
    arr = np.frombuffer(fbo.read(components=4, dtype="f2"), dtype=np.float16)
    arr = arr.reshape(tex.size[1], tex.size[0], 4).astype(np.float32)
    fbo.release()
    return arr


def _hf_var(arr, y0, y1, x0, x1):
    """高频方差（噪声代理）：静止区域内与 3x3 均值之差。"""
    g = arr[y0:y1, x0:x1, 0] * 0.2126 + arr[y0:y1, x0:x1, 1] * 0.7152 \
        + arr[y0:y1, x0:x1, 2] * 0.0722
    from numpy.lib.stride_tricks import sliding_window_view
    win = sliding_window_view(g, (3, 3))
    return float(((g[1:-1, 1:-1] - win.mean(axis=(-2, -1))) ** 2).mean())


W, H = 256, 192


def test_spatial_three_modes_reduce_noise_and_keep_edges():
    ctx = _context()
    from src.features.color.core.video_denoise_gl import (
        QuadRenderer, RenderSurfaceCache,
    )
    from src.features.color.core.video_spatial_denoise import VideoSpatialDenoiser

    quad = QuadRenderer(ctx)
    surfaces = RenderSurfaceCache(ctx)
    denoiser = VideoSpatialDenoiser(ctx, quad, surfaces)
    tex = _noise_frame(ctx, W, H, seed=0, moving_block=False)
    try:
        arr0 = _read(ctx, tex)
        base_var = _hf_var(arr0, 10, 100, 10, 200)
        for mode in ("fast", "better", "enhanced"):
            p = SpatialDenoiseParams(enabled=True, mode=mode, radius=4,
                                     luma=70, chroma=70, detail_recovery=0.3)
            out = denoiser.process(tex, p)
            arr = _read(ctx, out)
            var = _hf_var(arr, 10, 100, 10, 200)
            assert var < base_var * 0.7, f"{mode}: noise not reduced {var} vs {base_var}"
            # 结构保留：低频轮廓与原图一致
            assert abs(arr[..., :3].mean() - arr0[..., :3].mean()) < 0.05
            assert np.isfinite(arr).all()
    finally:
        denoiser.release()
        surfaces.release_all()
        quad.release()
        tex.release()
        ctx.release()


def test_video_denoise_preserves_hdr_values():
    """内部 float 降噪不能把 HDR 高光静默截到 1.0。"""
    ctx = _context()
    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_params(VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=2, luma=80, chroma=40),
        spatial=SpatialDenoiseParams(enabled=True, mode="fast", radius=2,
                                     luma=60, chroma=40)), "preview")
    try:
        f0 = _solid_frame(ctx, W, H, 2.0)
        f1 = _solid_frame(ctx, W, H, 2.0)
        pipeline.process(f0, 0, 1, "hdr")
        out = pipeline.process(f1, 1, 1, "hdr")
        arr = _read(ctx, out)
        assert float(arr[..., :3].mean()) > 1.5
        assert float(arr[..., :3].max()) > 1.5
        f0.release()
        f1.release()
    finally:
        pipeline.release()
        ctx.release()


def test_temporal_accumulates_on_static_scene():
    ctx = _context()
    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_params(VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=2, luma=85, chroma=70),
        spatial=SpatialDenoiseParams(enabled=False)), "preview")
    try:
        tex0 = _noise_frame(ctx, W, H, seed=0, moving_block=False)
        var0 = _hf_var(_read(ctx, tex0), 10, 100, 10, 200)
        last = None
        for i in range(6):
            tex = _noise_frame(ctx, W, H, seed=i, moving_block=False)
            last = pipeline.process(tex, i, 1, "clip")
            tex.release()
        var6 = _hf_var(_read(ctx, last), 10, 100, 10, 200)
        # 递归时域滤波的理论稳态保留率 ≈ (1-w)/(1+w)；本参数下约 45-55%。
        # 时域降噪设计上保守（防鬼影），40%+ 的噪声方差下降即视为有效。
        assert var6 < var0 * 0.6, f"temporal accumulation too weak: {var6} vs {var0}"
        tex0.release()
    finally:
        pipeline.release()
        ctx.release()


def test_scene_cut_and_seek_reset():
    ctx = _context()
    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_params(VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=3, luma=90, chroma=0),
        spatial=SpatialDenoiseParams(enabled=False)), "preview")
    try:
        # 累积 3 帧
        for i in range(3):
            pipeline.process(_noise_frame(ctx, W, H, seed=i), i, 1, "clip")
        # 场景 A（亮）-> 场景 B（暗），帧号跳变模拟 seek
        scene_b = ctx.texture((W, H), 4, dtype="f2")
        scene_b.write(np.dstack([
            np.full((H, W, 3), 0.05, np.float16),
            np.ones((H, W, 1), np.float16)]).tobytes())
        out = pipeline.process(scene_b, 100, 1, "clip")  # delta 跳变 → 直通
        arr = _read(ctx, out)
        assert float(np.abs(arr[..., :3] - 0.05).max()) < 1e-3, \
            "seek 后首帧必须精确直通，不能带 seek 前历史"
        scene_b.release()
    finally:
        pipeline.release()
        ctx.release()


def test_moving_object_not_smeared():
    ctx = _context()
    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_params(VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=2, luma=70, chroma=40),
        spatial=SpatialDenoiseParams(enabled=True, mode="fast", radius=3,
                                     luma=50, chroma=50)), "preview")
    try:
        last = None
        for i in range(5):
            tex = _noise_frame(ctx, W, H, seed=i)  # 方块每帧移动
            last = pipeline.process(tex, i, 1, "clip")
            tex.release()
        arr = _read(ctx, last)
        # 方块当前位置中心必须是亮的（不被时域/空域抹掉或拖影成背景）
        # frame4: bx=40+48=88..128, by=30+24=54..94 → 中心 (108, 74)
        block_r = arr[74, 108, 0]
        bg_r = arr[5, 5, 0]
        assert block_r > bg_r + 0.2, \
            f"运动方块被抹掉/拖影: block={block_r} bg={bg_r}"
        # frame0 方块位置 (40..80, 30..70) 中心 (60, 50)——frame4 已完全腾空
        old_r = arr[50, 60, 0]
        assert old_r < block_r - 0.2, f"旧位置残留拖影: old={old_r} block={block_r}"
    finally:
        pipeline.release()
        ctx.release()


def test_disabled_params_passthrough_exact():
    ctx = _context()
    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_params(VideoDenoiseParams(), "preview")  # 全关
    try:
        tex = _noise_frame(ctx, W, H, seed=3)
        out = pipeline.process(tex, 0, 1, "clip")
        assert out is tex, "关闭降噪时必须零开销直通"
        tex.release()
    finally:
        pipeline.release()
        ctx.release()


def test_window_mode_reduces_noise():
    ctx = _context()
    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_params(VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=3, luma=70, chroma=50),
        spatial=SpatialDenoiseParams(enabled=False)), "paused")
    try:
        frames = [_noise_frame(ctx, W, H, seed=i, moving_block=False)
                  for i in range(5)]
        cur = frames[-1]
        var0 = _hf_var(_read(ctx, cur), 10, 100, 10, 200)
        out = pipeline.process_window(cur, frames[:-1])
        var = _hf_var(_read(ctx, out), 10, 100, 10, 200)
        assert var < var0 * 0.75, f"窗口模式降噪不足: {var} vs {var0}"
        for t in frames:
            t.release()
    finally:
        pipeline.release()
        ctx.release()


def test_window_replay_does_not_contaminate_causal_history():
    """暂停窗口重放使用独立 scratch，不得改变后续播放输出。"""
    ctx = _context()
    params = VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=3, luma=80, chroma=60),
        spatial=SpatialDenoiseParams(enabled=False),
    )
    polluted = VideoDenoisePipeline(ctx)
    clean = VideoDenoisePipeline(ctx)
    polluted.set_params(params, "preview")
    clean.set_params(params, "preview")
    frames = [_noise_frame(ctx, W, H, seed=i, moving_block=False) for i in range(3)]
    try:
        polluted.process(frames[0], 0, 1, "clip")
        clean.process(frames[0], 0, 1, "clip")
        polluted.process_window(frames[1], [frames[0]])
        out_polluted = polluted.process(frames[1], 1, 1, "clip")
        out_clean = clean.process(frames[1], 1, 1, "clip")
        diff = np.abs(_read(ctx, out_polluted) - _read(ctx, out_clean))
        assert float(diff[..., :3].max()) < 1e-3
    finally:
        for frame in frames:
            frame.release()
        polluted.release()
        clean.release()
        ctx.release()


def test_debug_overlay_tints_by_confidence():
    """调试叠加：静态画面高置信度 → 青色调（G>R）。"""
    ctx = _context()
    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_debug_overlay(True)
    pipeline.set_params(VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=2, luma=70, chroma=60),
        spatial=SpatialDenoiseParams(enabled=False)), "preview")
    try:
        f0 = _noise_frame(ctx, W, H, seed=0, moving_block=False)
        f1 = _noise_frame(ctx, W, H, seed=1, moving_block=False)
        pipeline.process(f0, 0, 1, "clip", quality_mode="preview")
        out = pipeline.process(f1, 1, 1, "clip", quality_mode="preview")
        arr = _read(ctx, out).astype(np.float32)
        # 静态高置信度区域被青色 (0.15,1.0,0.85) 混合 → G 明显高于 R
        assert arr[..., 1].mean() > arr[..., 0].mean() + 0.05, (
            f"叠加未生效: R={arr[..., 0].mean():.3f} G={arr[..., 1].mean():.3f}"
        )
        f0.release()
        f1.release()
    finally:
        pipeline.release()
        ctx.release()


def test_debug_overlay_off_passthrough():
    """叠加关闭时输出与常规降噪一致（不额外产生表面/着色）。"""
    ctx = _context()
    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_params(VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=2, luma=70, chroma=60),
        spatial=SpatialDenoiseParams(enabled=False)), "preview")
    try:
        f0 = _noise_frame(ctx, W, H, seed=0, moving_block=False)
        f1 = _noise_frame(ctx, W, H, seed=1, moving_block=False)
        pipeline.process(f0, 0, 1, "clip", quality_mode="preview")
        out = pipeline.process(f1, 1, 1, "clip", quality_mode="preview")
        arr = _read(ctx, out).astype(np.float32)
        # 无叠加：中性灰画面 R/G/B 接近
        assert abs(arr[..., 0].mean() - arr[..., 1].mean()) < 0.05
        f0.release()
        f1.release()
    finally:
        pipeline.release()
        ctx.release()


# ---------------------------------------------------------------------------
# G-P1 缓慢平移回归（2026-08-24 伪影修复的测试盲区补齐）
# ---------------------------------------------------------------------------

def _pan_stripes_frame(ctx, w, h, frame_idx, pan_px=2.5, period=8.0,
                       amp=0.12, noise=0.05):
    """低对比度细条纹 + 整体缓慢平移 + 噪声（涂抹拖影的最敏感场景）。

    返回 (带噪纹理, 干净 ndarray)。条纹沿 x 方向，相位随帧平移。
    """
    rng = np.random.default_rng(1000 + frame_idx)
    xx = np.arange(w, dtype=np.float32) - frame_idx * pan_px
    phase = 2.0 * np.pi * xx / period
    stripe = amp * np.sin(phase)[None, :] * np.ones((h, 1), dtype=np.float32)
    clean = np.full((h, w, 4), 0.5, dtype=np.float32)
    clean[..., 0] += stripe
    clean[..., 1] += stripe
    clean[..., 2] += stripe
    noisy = np.clip(clean + rng.normal(0, noise, clean.shape), 0.0, 1.0)
    tex = ctx.texture((w, h), 4, dtype="f2")
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex.write(noisy.astype(np.float16))
    return tex, clean


def _stripe_std(arr):
    """条纹幅度估计：去均值后的行向标准差（中心区域）。"""
    h, w = arr.shape[:2]
    lum = arr[h // 4: 3 * h // 4, w // 4: 3 * w // 4, 0]
    return float(lum.std())


def test_motion_field_accuracy_slow_pan():
    """G-P1 运动场精度（缓慢平移判别性测试）。

    修复前：3x3 支撑整数搜索在缓动（1/4 res <1px/帧）下运动场分裂
    （一半像素 0px、一半 ±4px），std≈4px 且置信度虚高 → 双向错位
    历史高置信融合 = 涂抹/重影伪影。修复后（5x5 支撑 + 亚像素细化 +
    运动场平滑）std 应 <1.5px，mean 误差 <1.5px。
    """
    import cv2
    from src.features.color.core.video_denoise_gl import (
        QuadRenderer, RenderSurfaceCache)
    from src.features.color.core.video_temporal_denoise import (
        VideoTemporalDenoiser)

    ctx = _context()
    quad = QuadRenderer(ctx)
    surf = RenderSurfaceCache(ctx)
    td = VideoTemporalDenoiser(ctx, quad, surf)
    try:
        w, h = 320, 240
        qw, qh = w // 4, h // 4
        rng0 = np.random.default_rng(42)
        content = rng0.normal(0, 1, (h, w)).astype(np.float32)
        content = cv2.GaussianBlur(content, (0, 0), 1.8)
        content = content / (content.std() + 1e-6) * 0.045

        def tex(shift_px, seed):
            mat = np.float32([[1, 0, shift_px], [0, 1, 0]])
            d = cv2.warpAffine(content, mat, (w, h))
            a = np.clip(
                0.5 + d + np.random.default_rng(seed).normal(0, 0.05, (h, w)),
                0, 1)
            t = ctx.texture((w, h), 4, dtype="f2")
            t.filter = (moderngl.LINEAR, moderngl.LINEAR)
            t.write(np.stack([a, a, a, np.ones_like(a)], -1).astype(np.float16))
            return t

        cur, prev = tex(5.0, 1), tex(2.5, 0)   # 平移 2.5px/帧
        lc = surf.get(("mf_lc", qw, qh), qw, qh)
        lp = surf.get(("mf_lp", qw, qh), qw, qh)
        td._downsample_luma(cur, lc, w, h)
        td._downsample_luma(prev, lp, w, h)
        p = TemporalDenoiseParams(enabled=True, frames=2, luma=80, chroma=60)
        motion = td._estimate_motion(lc.texture, lp.texture, p, qw, qh)
        assert motion is not None

        fbo = ctx.framebuffer([motion.texture])
        m = np.frombuffer(
            fbo.read(components=4, dtype="f2"), dtype=np.float16
        ).reshape(qh, qw, 4).astype(np.float32)
        fbo.release()

        x = -m[qh // 4: 3 * qh // 4, qw // 4: 3 * qw // 4, 0] * w
        truth = 2.5
        assert abs(float(x.mean()) - truth) < 1.5, (
            f"运动场均值偏差过大: mean={x.mean():.2f} truth={truth}"
        )
        assert float(x.std()) < 1.5, (
            f"运动场分裂/抖动: std={x.std():.2f}（修复前 ≈4.1）"
        )
        cur.release()
        prev.release()
    finally:
        surf.release_all()
        quad.release()
        ctx.release()


def test_slow_pan_static_region_still_denoises():
    """缓慢平移修复后，静止区域仍保持降噪能力（不能因噎废食）。"""
    ctx = _context()
    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_params(VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=2, luma=70, chroma=50),
        spatial=SpatialDenoiseParams(enabled=False)), "preview")
    try:
        static = [_noise_frame(ctx, 320, 240, seed=i, moving_block=False)
                  for i in range(6)]
        for i in range(len(static) - 1):
            pipeline.process(static[i], i, 2, "clip", quality_mode="preview")
        out = pipeline.process(static[-1], len(static) - 1, 2, "clip",
                               quality_mode="preview")
        var0 = _hf_var(_read(ctx, static[-1]), 40, 120, 40, 200)
        var = _hf_var(_read(ctx, out), 40, 120, 40, 200)
        assert var < var0 * 0.6, f"静止区域降噪失效: {var} vs {var0}"
        for t in static:
            t.release()
    finally:
        pipeline.release()
        ctx.release()
