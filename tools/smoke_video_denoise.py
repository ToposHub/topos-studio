"""GL 冒烟测试：验证视频降噪所有着色器编译 + 全管线运行（独立上下文）。"""
import sys
import numpy as np

sys.path.insert(0, ".")

import moderngl

from src.shared.video.denoise.params import (
    SpatialDenoiseParams, TemporalDenoiseParams, VideoDenoiseParams,
)
from src.features.color.core.video_denoise_pipeline import VideoDenoisePipeline


def make_noise_frame(ctx, w, h, seed, noise=0.08):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.zeros((h, w, 3), dtype=np.float32)
    base[..., 0] = 0.4 + 0.2 * np.sin(xx / 24.0 + seed)
    base[..., 1] = 0.45 + 0.15 * np.cos(yy / 31.0)
    base[..., 2] = 0.35 + 0.1 * np.sin((xx + yy) / 40.0)
    # 一个运动方块（跨帧移动 6px）
    bx = 40 + seed * 6
    by = 30 + seed * 4
    base[by:by+60, bx:bx+60, :] = [0.9, 0.7, 0.2]
    noisy = np.clip(base + rng.normal(0, noise, (h, w, 3)), 0, 1)
    tex = ctx.texture((w, h), 4, dtype="f2")
    data = np.dstack([noisy.astype(np.float16), np.ones((h, w, 1), dtype=np.float16)])
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex.write(data.tobytes())
    return tex


def read_back(ctx, tex, w, h):
    fbo = ctx.framebuffer([tex])
    data = fbo.read(components=4, dtype="f2")
    arr = np.frombuffer(data, dtype=np.float16).reshape(h, w, 4).astype(np.float32)
    fbo.release()
    return arr


def variance_of_luma(arr):
    y = 0.2126*arr[...,0] + 0.7152*arr[...,1] + 0.0722*arr[...,2]
    # 高频方差近似：与 3x3 均值的差
    k = np.ones((3,3))/9.0
    from numpy.lib.stride_tricks import sliding_window_view
    win = sliding_window_view(y, (3,3))
    mean = win.mean(axis=(-2,-1))
    center = y[1:-1,1:-1]
    return float(((center - mean)**2).mean())


def main():
    ctx = moderngl.create_standalone_context()
    print(f"GL context: {ctx.info['GL_RENDERER']}")
    W, H = 320, 240

    params = VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=2, luma=60, chroma=40,
                                        motion="fast", threshold=0.5, blend=0.6),
        spatial=SpatialDenoiseParams(enabled=True, mode="fast", radius=3, luma=50,
                                     chroma=50, threshold=0.5, detail_recovery=0.4),
    )

    pipeline = VideoDenoisePipeline(ctx)
    pipeline.set_params(params, "preview")
    assert pipeline.is_active()

    for mode in ("fast", "better", "enhanced"):
        pipeline.set_params(VideoDenoiseParams(
            temporal=TemporalDenoiseParams(enabled=True, frames=3, luma=60, chroma=40),
            spatial=SpatialDenoiseParams(enabled=True, mode=mode, radius=4, luma=60, chroma=60),
        ), "preview")
        # 顺序喂 5 帧（模拟连续播放）
        outputs = []
        noise_levels = []
        for i in range(5):
            tex = make_noise_frame(ctx, W, H, i)
            out = pipeline.process(tex, frame_index=i, generation_id=1, clip_key="c1")
            arr = read_back(ctx, out, W, H)
            outputs.append(arr)
            # 静止区域（避开运动方块）噪声方差应下降
            noise_levels.append(variance_of_luma(arr[10:100, 180:300]))
        print(f"[{mode}] still-region luma var over frames: "
              f"{['%.5f' % v for v in noise_levels]}")
        assert noise_levels[-1] < noise_levels[0] * 0.8, \
            f"{mode}: noise not reduced ({noise_levels})"
        # 运动方块必须仍存在（检查方块中心亮度）
        arr = outputs[-1]
        bx, by = 40 + 4*6 + 30, 30 + 4*4 + 30
        block_val = arr[by, bx, 0]
        bg_val = arr[10, 10, 0]
        print(f"[{mode}] moving block R={block_val:.3f} vs bg R={bg_val:.3f}")
        assert block_val > bg_val + 0.15, f"{mode}: moving object smeared away!"

    # seek 重置测试：帧号跳变后第一帧应直通（无历史污染）
    pipeline.set_params(VideoDenoiseParams(
        temporal=TemporalDenoiseParams(enabled=True, frames=2, luma=80, chroma=0),
        spatial=SpatialDenoiseParams(enabled=False),
    ), "preview")
    for i in range(3):
        pipeline.process(make_noise_frame(ctx, W, H, i), i, 1, "c1")
    # 场景切换：完全不同的画面
    scene2 = make_noise_frame(ctx, W, H, 50, noise=0.02)
    out = pipeline.process(scene2, 100, 1, "c1")  # 帧号跳变
    arr = read_back(ctx, out, W, H)
    err = float(np.abs(arr[..., :3] - read_back(ctx, scene2, W, H)[..., :3]).max())
    print(f"[scene-cut] passthrough max err = {err:.4f}")
    assert err < 1e-3, "frame jump should passthrough exactly"

    # 连续帧但内容突变（模拟镜头硬切，帧号连续）
    out = pipeline.process(scene2, 3, 1, "c1")  # 上一帧是 frame2(噪声图)，此帧是 scene2
    # scene cut 检测在 shader 内逐像素处理；输出不应崩溃
    arr2 = read_back(ctx, out, W, H)
    print(f"[hard-cut] output stats: mean={arr2[..., :3].mean():.3f} "
          f"finite={np.isfinite(arr2).all()}")

    stats = pipeline.get_stats()
    print("stats:", {k: v for k, v in stats.items() if k != 'temporal'})
    print("temporal stats:", stats["temporal"])

    pipeline.release()
    ctx.release()
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
