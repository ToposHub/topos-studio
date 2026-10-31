#!/usr/bin/env python3
"""Metal 视频零拷贝垂直链路 benchmark（计划 Phase 5 P5-06）。

独占测量 Metal 侧链路（不与 OpenGL 混跑、不接默认播放）：

    VT 解码（--file 模式）→ CVMetalTextureCache 导入 → MSL YUV→RGBA16F
    转换（compute）→ finish 同步 → ring 释放

分段计时：import / convert_encode / gpu_wait（finish）/ total，
输出 p50/p95/mean，归档 JSON + .md 配对（P9C-02 同构）。

用法：
    # 合成 NV12 源（默认 1280x720，无需素材托管）
    python tools/bench_metal_video_zero_copy.py --frames 120 --warmup 30

    # 真实文件（须 VT 硬解可用格式；自动跑 metal_zero_copy_gate）
    TOPOS_METAL_VIDEO_PREVIEW=1 python tools/bench_metal_video_zero_copy.py \
        --file /path/to/h264.mp4 --frames 120

注意（§0.2 A′ 决策）：本机（Intel + AMD 6900XT）结果只代表降级路径
基线；Apple Silicon 主目标机到位后须复测。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np

RESULTS_DIR = REPO / "tests" / "media" / "results"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def _stats(values: list[float]) -> dict:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "n": 0}
    return {
        "p50": round(_percentile(values, 0.50), 4),
        "p95": round(_percentile(values, 0.95), 4),
        "mean": round(statistics.fmean(values), 4),
        "n": len(values),
    }


def _make_synthetic_buffer(width: int, height: int, pixel_format: str):
    """合成 NV12/P010 CVPixelBuffer（渐变图案，无需素材托管）。"""
    import Quartz

    fmt = (
        Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        if pixel_format == "nv12"
        else Quartz.kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange
    )
    status, pb = Quartz.CVPixelBufferCreate(
        None, width, height, fmt,
        {Quartz.kCVPixelBufferIOSurfacePropertiesKey: {}}, None,
    )
    if status != 0 or pb is None:  # CVReturn 显式 ==0 判断
        raise RuntimeError(f"CVPixelBufferCreate 失败 status={status}")

    from src.shared.media.macos_vt.cv_metal_bridge import (
        lock_pixel_buffer_planes,
        unlock_pixel_buffer_base_address,
    )

    planes = lock_pixel_buffer_planes(pb)
    (y_base, y_bpr), (uv_base, uv_bpr) = planes
    if pixel_format == "nv12":
        y_rows = np.zeros((height, y_bpr), dtype=np.uint8)
        y_rows[:, :width] = (
            (np.arange(width)[None, :] * 7 + np.arange(height)[:, None] * 3 + 16) % 256
        ).astype(np.uint8)
        ctypes.memmove(y_base, y_rows.tobytes(), y_rows.nbytes)
        uv = np.empty((height // 2, width), dtype=np.uint8)
        uv[:, 0::2] = 128  # 中性色度（时序测试不关心色彩正确性）
        uv[:, 1::2] = 128
        uv_rows = np.zeros((height // 2, uv_bpr), dtype=np.uint8)
        uv_rows[:, :width] = uv
        ctypes.memmove(uv_base, uv_rows.tobytes(), uv_rows.nbytes)
    else:  # p010：10-bit 左对齐进 16-bit
        y_rows = np.zeros((height, y_bpr // 2), dtype="<u2")
        y_rows[:, :width] = (
            ((np.arange(width)[None, :] + np.arange(height)[:, None]) % 1024) << 6
        ).astype("<u2")
        ctypes.memmove(y_base, y_rows.tobytes(), y_rows.nbytes)
        uv_rows = np.full((height // 2, uv_bpr // 2), 512 << 6, dtype="<u2")
        ctypes.memmove(uv_base, uv_rows.tobytes(), uv_rows.nbytes)
    unlock_pixel_buffer_base_address(pb)
    return pb


def _run_synthetic(args) -> dict:
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.gpu.metal.kernels_video import build_yuv_convert_params
    from src.shared.media.macos_vt.cv_metal_bridge import (
        CVMetalVideoBridge,
        convert_planes_to_rgba16f,
    )

    metal_ctx = MetalComputeContext()
    bridge = CVMetalVideoBridge(metal_ctx=metal_ctx, ring_size=args.ring)
    if not bridge.initialize():
        raise RuntimeError(f"bridge init failed: {bridge.last_fallback_reason()}")

    pb = _make_synthetic_buffer(args.width, args.height, args.pixel_format)
    bit_depth = 8 if args.pixel_format == "nv12" else 10
    params = build_yuv_convert_params(bit_depth, "limited", "bt709")

    import_ms: list[float] = []
    encode_ms: list[float] = []
    gpu_ms: list[float] = []
    total_ms: list[float] = []
    width, height = args.width, args.height
    try:
        for i in range(args.warmup + args.frames):
            t0 = time.perf_counter()
            result = bridge.import_pixel_buffer(pb, i, args.pixel_format, bit_depth)
            t1 = time.perf_counter()
            if result is None:
                raise RuntimeError(f"import failed: {bridge.last_fallback_reason()}")
            planes, gpu_release = result
            y_tex = planes[0].resource.native_handle
            uv_tex = planes[1].resource.native_handle
            out_tex = convert_planes_to_rgba16f(
                metal_ctx, y_tex, uv_tex, params, width, height
            )
            t2 = time.perf_counter()
            metal_ctx.finish()  # 单同步点（无每帧多 fence）
            t3 = time.perf_counter()
            bridge.ring.track(i, gpu_release)
            bridge.ring.mark_submitted()
            bridge.ring.on_gpu_complete()  # finish 已同步
            bridge.ring.release_completed()

            if i >= args.warmup:
                import_ms.append((t1 - t0) * 1e3)
                encode_ms.append((t2 - t1) * 1e3)
                gpu_ms.append((t3 - t2) * 1e3)
                total_ms.append((t3 - t0) * 1e3)
    finally:
        bridge.destroy()

    return {
        "mode": "synthetic",
        "pixel_format": args.pixel_format,
        "size": f"{args.width}x{args.height}",
        "import": _stats(import_ms),
        "convert_encode": _stats(encode_ms),
        "gpu_wait": _stats(gpu_ms),
        "total": _stats(total_ms),
    }


def _run_file(args) -> dict:
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.gpu.metal.kernels_video import build_yuv_convert_params
    from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

    source = MacOSVtZeroCopySource()
    if not source.open(args.file):
        raise RuntimeError(f"open failed: {source.fallback_reason}")
    info = source.metadata
    decode_ms: list[float] = []
    import_ms: list[float] = []
    encode_ms: list[float] = []
    gpu_ms: list[float] = []
    total_ms: list[float] = []

    metal_ctx = MetalComputeContext()
    consumed = 0
    try:
        for i in range(args.warmup + args.frames):
            t0 = time.perf_counter()
            vt_frame = source.read_next_pixel_buffer()
            if vt_frame is None:
                break
            t1 = time.perf_counter()
            gpu_frame = source.import_pixel_buffer_frame_metal(vt_frame)
            t2 = time.perf_counter()
            if gpu_frame is None:
                raise RuntimeError(
                    f"metal import failed: {source.fallback_reason}"
                )
            y_tex = gpu_frame.planes[0].resource.native_handle
            uv_tex = gpu_frame.planes[1].resource.native_handle
            params = build_yuv_convert_params(
                gpu_frame.bit_depth, gpu_frame.color_range, gpu_frame.color_matrix
            )
            from src.shared.media.macos_vt.cv_metal_bridge import (
                convert_planes_to_rgba16f,
            )

            out_tex = convert_planes_to_rgba16f(
                metal_ctx, y_tex, uv_tex, params,
                gpu_frame.width, gpu_frame.height,
            )
            t3 = time.perf_counter()
            metal_ctx.finish()
            t4 = time.perf_counter()
            # P5-05 生命周期：finish（fence 完成）之后才释放
            from src.shared.media.gpu_frame import safe_release_gpu_frame

            safe_release_gpu_frame(gpu_frame)
            if i >= args.warmup:
                decode_ms.append((t1 - t0) * 1e3)
                import_ms.append((t2 - t1) * 1e3)
                encode_ms.append((t3 - t2) * 1e3)
                gpu_ms.append((t4 - t3) * 1e3)
                total_ms.append((t4 - t0) * 1e3)
                consumed += 1
    finally:
        source.close()

    if consumed < min(10, args.frames):
        raise RuntimeError(f"有效帧不足（{consumed}），文件模式结果不可信")
    return {
        "mode": "file",
        "file": str(args.file),
        "size": f"{info.width}x{info.height}",
        "decode": _stats(decode_ms),
        "metal_import": _stats(import_ms),
        "convert_encode": _stats(encode_ms),
        "gpu_wait": _stats(gpu_ms),
        "total": _stats(total_ms),
    }


def _write_markdown(results: dict, path: Path) -> None:
    lines = [
        "# Metal 视频零拷贝 benchmark（Phase 5 P5-06）",
        "",
        f"- 时间：{results['timestamp']}",
        f"- 模式：{results['mode']}　尺寸：{results['size']}　"
        f"环境：{results['device']}（{results['platform_note']}）",
        "",
        "| 阶段 | p50 (ms) | p95 (ms) | mean (ms) | n |",
        "|---|---:|---:|---:|---:|",
    ]
    for seg in ("decode", "metal_import", "import", "convert_encode", "gpu_wait", "total"):
        if seg in results and isinstance(results[seg], dict):
            s = results[seg]
            lines.append(
                f"| {seg} | {s['p50']} | {s['p95']} | {s['mean']} | {s['n']} |"
            )
    lines += [
        "",
        "> 门禁语义：本报告为本机（Intel + AMD 6900XT 降级路径）基线，",
        "> 仅作不退化判定；Apple Silicon 主目标机到位后复测（§0.2 A′）。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="", help="真实视频文件（VT 硬解格式）")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--pixel-format", choices=["nv12", "p010"], default="nv12")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--ring", type=int, default=4)
    parser.add_argument("--output-prefix", default="metal_video_zero_copy_bench")
    args = parser.parse_args()

    from src.shared.gpu.metal.context import MetalComputeContext

    ok, reason = MetalComputeContext.availability_reason()
    if not ok:
        print(f"Metal 不可用：{reason}")
        return 1

    results = _run_file(args) if args.file else _run_synthetic(args)
    results["timestamp"] = datetime.now().isoformat(timespec="seconds")
    results["device"] = MetalComputeContext().device_name
    results["platform_note"] = "Intel + AMD 6900XT 降级路径（A′ 主目标机待复测）"

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{args.output_prefix}.json"
    md_path = RESULTS_DIR / f"{args.output_prefix}.md"
    json_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _write_markdown(results, md_path)
    print(f"JSON -> {json_path}")
    print(f"MD   -> {md_path}")
    for seg in ("decode", "metal_import", "import", "convert_encode", "gpu_wait", "total"):
        if seg in results and isinstance(results[seg], dict):
            s = results[seg]
            print(f"  {seg:>14}: p50={s['p50']:8.3f}  p95={s['p95']:8.3f}  n={s['n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
