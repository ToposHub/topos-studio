#!/usr/bin/env python
"""视频降噪基准测试工具（M0 性能基线 + 真实素材验证）。

用法:
    .venv/bin/python tools/benchmark_video_denoise.py [视频路径 ...]
    .venv/bin/python tools/benchmark_video_denoise.py --frames 60 --start 30
    .venv/bin/python tools/benchmark_video_denoise.py --mode window   # 窗口模式（前后邻帧）

对每个视频测量并输出:
    - 解码耗时（首帧 / 后续帧 / 缓存命中）
    - GPU 时域降噪耗时（glFinish 计时，含运动估计）
    - GPU 空域降噪耗时（fast/better/enhanced 三模式）
    - 端到端吞吐（帧/秒，含上传）
    - 视觉对比图 PNG（original / temporal / spatial / combined）

默认遍历 tests/test_videos 下的全部视频。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np


def to_rgb_float(frame) -> np.ndarray:
    """DecodedFrame -> float32 RGB (H, W, 3) 0-1。"""
    import cv2

    if getattr(frame, "planes", None) and len(frame.planes) >= 2:
        planes = frame.planes
        y = planes[0]
        y = y.astype(np.float32) / (65535.0 if y.dtype == np.uint16 else 255.0)
        if len(planes) >= 3 and planes[1].ndim == 2:  # yuv420p / yuv422p
            u = planes[1].astype(np.float32) / 255.0
            v = planes[2].astype(np.float32) / 255.0
            u = cv2.resize(u, (y.shape[1], y.shape[0]), interpolation=cv2.INTER_LINEAR)
            v = cv2.resize(v, (y.shape[1], y.shape[0]), interpolation=cv2.INTER_LINEAR)
            # OpenCV's YCrCb order is Y, Cr, Cb; planar 4:2:0 is Y, U(Cb),
            # V(Cr).  Swapping these two planes is visible as a hue error in
            # the benchmark and can hide real denoise regressions.
            ycrcb = np.stack([y, v, u], axis=-1)
            return np.clip(cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2RGB), 0, 1)
        if len(planes) >= 2 and planes[1].ndim == 3:  # nv12 / p010 (H, W/2, 2)
            uv = planes[1].astype(np.float32) / 255.0
            uv = cv2.resize(uv, (y.shape[1], y.shape[0]), interpolation=cv2.INTER_LINEAR)
            ycrcb = np.stack([y, uv[..., 1], uv[..., 0]], axis=-1)
            return np.clip(cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2RGB), 0, 1)
        return np.stack([y, y, y], axis=-1)

    data = frame.data
    if data is None:
        raise ValueError("frame has no data")
    if data.dtype == np.uint16:
        data = data.astype(np.float32) / 65535.0
    elif data.dtype != np.float32:
        data = data.astype(np.float32) / 255.0
    if data.shape[-1] == 4:
        data = data[..., :3]
    if getattr(frame, "pixel_format", "").startswith(("bgr",)):
        data = data[..., ::-1]
    return np.ascontiguousarray(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="*", help="视频文件路径（默认 tests/test_videos/*）")
    parser.add_argument("--frames", type=int, default=48, help="测试帧数")
    parser.add_argument("--start", type=int, default=24, help="起始帧号")
    parser.add_argument("--quality", default="master", choices=["proxy", "master"],
                        help="解码质量")
    parser.add_argument("--mode", default="causal", choices=["causal", "window"],
                        help="时域模式：causal=因果递归, window=前后邻帧窗口")
    parser.add_argument("--window", type=int, default=2, help="窗口模式前后帧数")
    parser.add_argument("--max-size", type=int, default=1920,
                        help="超过此宽度的帧降采样（0=不限制）")
    parser.add_argument("--png-out", default="tests/video_denoise/output",
                        help="对比图输出目录")
    parser.add_argument("--no-png", action="store_true", help="不输出对比图")
    parser.add_argument("--quality-mode", default="preview",
                        choices=["preview", "paused", "export"],
                        help="降噪质量模式（影响参数裁剪）")
    args = parser.parse_args()

    videos = args.videos
    if not videos:
        test_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tests", "test_videos")
        videos = sorted(
            os.path.join(test_dir, f) for f in os.listdir(test_dir)
            if f.lower().endswith((".mov", ".mp4", ".m4v"))
        )

    import moderngl
    from src.shared.video.frame_provider_service import get_frame_provider_service
    from src.shared.video.denoise.params import (
        SpatialDenoiseParams, TemporalDenoiseParams, VideoDenoiseParams,
    )
    from src.features.color.core.video_denoise_pipeline import VideoDenoisePipeline

    ctx = moderngl.create_standalone_context()
    print(f"GL: {ctx.info['GL_RENDERER']} ({ctx.info['GL_VERSION']})")

    service = get_frame_provider_service()
    pipeline = VideoDenoisePipeline(ctx)

    # 典型可用参数（中等强度）
    params = VideoDenoiseParams(
        temporal=TemporalDenoiseParams(
            enabled=True, frames=2, luma=60, chroma=40,
            motion="fast" if args.quality_mode == "preview" else "balanced",
            threshold=0.5, blend=0.6, scene_cut_sensitivity=0.5),
        spatial=SpatialDenoiseParams(
            enabled=True, mode="fast", radius=3, luma=50, chroma=50,
            threshold=0.5, detail_recovery=0.4),
    )
    pipeline.set_params(params, args.quality_mode)

    os.makedirs(args.png_out, exist_ok=True)
    overall = {}

    for video_path in videos:
        name = os.path.splitext(os.path.basename(video_path))[0]
        if not os.path.exists(video_path):
            print(f"[skip] {video_path} 不存在")
            continue
        clip_id = None
        try:
            clip_id = service.open_clip(video_path)
        except Exception as exc:
            print(f"[skip] 打开失败 {name}: {exc}")
            continue
        if not clip_id:
            print(f"[skip] 打开失败 {name}")
            continue

        try:
            _bench_one(ctx, service, pipeline, clip_id, name, video_path, args)
        except Exception:
            import traceback
            traceback.print_exc()
            print(f"[fail] {name}")
        finally:
            try:
                service.close_clip(clip_id)
            except Exception:
                pass

    pipeline.release()
    ctx.release()
    return 0


def _bench_one(ctx, service, pipeline, clip_id, name, video_path, args):
    import moderngl
    from src.shared.video.denoise.params import (
        SpatialDenoiseParams, TemporalDenoiseParams, VideoDenoiseParams,
    )

    total = getattr(service, "get_stats", dict)() if hasattr(service, "get_stats") else {}

    n_frames = args.frames
    start = args.start

    # ---- 解码 + 上传 ----
    decode_times = []
    upload_times = []
    textures = []
    tex_frames = []
    first = time.perf_counter()
    for i in range(n_frames):
        t0 = time.perf_counter()
        frame = service.request_frame(clip_id, start + i, args.quality)
        decode_times.append(time.perf_counter() - t0)
        if frame is None:
            continue
        t1 = time.perf_counter()
        rgb = to_rgb_float(frame)
        h, w = rgb.shape[:2]
        if args.max_size and w > args.max_size:
            import cv2
            scale = args.max_size / w
            rgb = cv2.resize(rgb, (args.max_size, int(h * scale)),
                             interpolation=cv2.INTER_AREA)
            h, w = rgb.shape[:2]
        tex = ctx.texture((w, h), 4, dtype="f2")
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        frame_bytes = np.ascontiguousarray(
            np.dstack([rgb, np.ones((h, w, 1), np.float32)])
        ).astype(np.float16).tobytes()
        tex.write(frame_bytes)
        upload_times.append(time.perf_counter() - t1)
        textures.append((start + i, tex))
        tex_frames.append(frame_bytes)
    warmup = time.perf_counter() - first

    if not textures:
        print(f"[fail] {name}: 无可用帧")
        return

    w, h = textures[0][1].size
    print(f"\n=== {name} ({w}x{h}, {len(textures)} 帧, quality={args.quality}) ===")
    dec = np.array(decode_times) * 1000
    upl = np.array(upload_times) * 1000
    print(f"  解码: first={dec[0]:.1f}ms p50={np.median(dec):.1f}ms "
          f"mean={dec.mean():.1f}ms  上传: mean={upl.mean():.1f}ms "
          f"  总预热={warmup*1000:.0f}ms")

    # ---- 降噪基准 ----
    def run_stage(desc, fn, fresh=False):
        """fresh=True: 每帧先重新 write 纹理数据（模拟真实播放的新纹理首采样），
        只计 process+finish 时间。fresh=False: 热纹理稳态（管线吞吐上界）。"""
        fn(textures[0][0], textures[0][1], warmup=True)
        ctx.finish()
        t0 = time.perf_counter()
        for n, (idx, tex) in enumerate(textures):
            if fresh:
                tex.write(tex_frames[n])
            fn(idx, tex)
        ctx.finish()
        elapsed = time.perf_counter() - t0
        ms = elapsed / len(textures) * 1000
        tag = " [fresh tex]" if fresh else ""
        print(f"  {desc:28s} {ms:7.2f} ms/帧  ({len(textures)/elapsed:6.1f} fps){tag}")
        return ms

    stats = {}

    # baseline（无降噪直通）
    def baseline(idx, tex, warmup=False):
        pass
    stats["baseline"] = run_stage("baseline (passthrough)", baseline)

    # temporal only（因果）
    pipeline.set_params(VideoDenoiseParams(
        temporal=params_temporal(), spatial=SpatialDenoiseParams(enabled=False)),
        args.quality_mode)

    def temporal_causal(idx, tex, warmup=False):
        pipeline.process(tex, idx, 1, name)
    stats["temporal_causal"] = run_stage("temporal (causal)", temporal_causal, fresh=True)
    stats["temporal_causal_warm"] = run_stage("temporal (causal, warm)", temporal_causal)

    # temporal window 模式
    if args.mode == "window" and len(textures) > 2 * args.window + 1:
        k = args.window
        window_frames = [(idx, tex) for idx, tex in textures]

        def temporal_window(idx, tex, warmup=False):
            neighbors = [t for i, t in window_frames if abs(i - idx) <= k and i != idx]
            pipeline.process_window(tex, neighbors)
        stats["temporal_window"] = run_stage("temporal (window ±%d)" % k, temporal_window)

    # spatial 三模式
    for mode in ("fast", "better", "enhanced"):
        sp = SpatialDenoiseParams(
            enabled=True, mode=mode, radius=4, luma=50, chroma=50,
            detail_recovery=0.4)

        def spatial_only(idx, tex, warmup=False, _p=sp):
            pipeline._spatial.process(tex, _p)
        stats[f"spatial_{mode}"] = run_stage(f"spatial ({mode})", spatial_only)

    # 组合（temporal + spatial）
    pipeline.set_params(VideoDenoiseParams(
        temporal=params_temporal(),
        spatial=SpatialDenoiseParams(
            enabled=True, mode="fast", radius=3, luma=50, chroma=50,
            detail_recovery=0.4)),
        args.quality_mode)

    def combined(idx, tex, warmup=False):
        pipeline.process(tex, idx, 1, name)
    stats["combined"] = run_stage("temporal+spatial combined", combined, fresh=True)

    pstats = pipeline.get_stats()
    print(f"  pipeline: surfaces={pstats['surfaces']} "
          f"({pstats['surface_bytes']/1048576:.1f}MB) "
          f"temporal avg={pstats['temporal_ms']:.2f}ms "
          f"spatial avg={pstats['spatial_ms']:.2f}ms "
          f"resets={pstats['temporal'].get('resets', 0)}")

    # ---- 视觉对比图 ----
    if not args.no_png and len(textures) >= 8:
        try:
            _write_comparison_pngs(ctx, pipeline, textures, name, args)
        except Exception:
            import traceback
            traceback.print_exc()


def params_temporal():
    from src.shared.video.denoise.params import TemporalDenoiseParams
    return TemporalDenoiseParams(
        enabled=True, frames=2, luma=60, chroma=40, motion="fast",
        threshold=0.5, blend=0.6)


def _write_comparison_pngs(ctx, pipeline, textures, name, args):
    import cv2
    from src.shared.video.denoise.params import (
        SpatialDenoiseParams, TemporalDenoiseParams, VideoDenoiseParams,
    )

    # 选中间偏后的帧（时域历史已累积）
    pick = len(textures) // 2 + 4
    idx, tex = textures[min(pick, len(textures) - 1)]

    def read(t):
        fbo = ctx.framebuffer([t])
        arr = np.frombuffer(fbo.read(components=4, dtype="f2"), dtype=np.float16)
        arr = arr.reshape(t.size[1], t.size[0], 4).astype(np.float32)[..., :3]
        fbo.release()
        return arr

    def save(tag, arr):
        out = os.path.join(args.png_out, f"{name}_{idx}_{tag}.png")
        cv2.imwrite(out, (np.clip(arr, 0, 1)[..., ::-1] * 255).astype(np.uint8))

    original = read(tex)
    save("0_original", original)

    # temporal only
    pipeline.set_params(VideoDenoiseParams(
        temporal=params_temporal(),
        spatial=SpatialDenoiseParams(enabled=False)), args.quality_mode)
    for i, t in textures[:pick + 1]:
        out_tex = pipeline.process(t, i, 1, name)
    save("1_temporal", read(out_tex))

    # spatial only——直接调用降噪器，绕过 for_quality 的模式裁剪，对比真实模式画质
    sp_fast = SpatialDenoiseParams(
        enabled=True, mode="fast", radius=4, luma=50, chroma=50,
        detail_recovery=0.4)
    save("2_spatial_fast", read(pipeline._spatial.process(tex, sp_fast)))

    sp_enh = SpatialDenoiseParams(
        enabled=True, mode="enhanced", radius=5, luma=50, chroma=50,
        detail_recovery=0.4)
    save("3_spatial_enhanced", read(pipeline._spatial.process(tex, sp_enh)))

    # combined
    pipeline.set_params(VideoDenoiseParams(
        temporal=params_temporal(),
        spatial=SpatialDenoiseParams(
            enabled=True, mode="fast", radius=3, luma=50, chroma=50,
            detail_recovery=0.4)), args.quality_mode)
    for i, t in textures[:pick + 1]:
        out_tex = pipeline.process(t, i, 1, name)
    save("4_combined", read(out_tex))
    print(f"  对比图已输出到 {args.png_out}/{name}_{idx}_*.png")


if __name__ == "__main__":
    sys.exit(main())
