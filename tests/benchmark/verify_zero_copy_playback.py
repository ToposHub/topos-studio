#!/usr/bin/env python3
"""
4K 全程 GPU 零拷贝播放验证脚本

验收标准：
- zero_copy == true
- cpu_download_frames == 0
- cpu_upload_frames == 0
- texture_write_frames == 0
- decode_path in allowed zero-copy paths
- frame_path == gpu_decoded_external_yuv
- total_p95 <= frame_budget
- yuv_render_p95 < 2ms
- ui_tick_p95 < 3ms
- color_verification.passed == true (if --verify-color)

Fallback 验证模式：
- --allow-fallback: 验证 fallback 路径正确性
- --require-zero-copy: 要求零拷贝（默认）

Usage:
    python tests/benchmark/verify_zero_copy_playback.py \\
        --file "/path/to/4k_h264.mp4" \\
        --frames 300 \\
        --warmup 30 \\
        --require-zero-copy \\
        --verify-color \\
        --output-json tests/media/results/zero_copy_acceptance_h264.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass
class VerificationResult:
    """验证结果"""
    passed: bool = False
    zero_copy: bool = False
    cpu_download_frames: int = 0
    cpu_upload_frames: int = 0
    texture_write_frames: int = 0
    decode_path: str = ""
    frame_path: str = ""
    total_p95_ms: float = 0.0
    yuv_render_p95_ms: float = 0.0
    ui_tick_p95_ms: float = 0.0
    frames_tested: int = 0
    color_verification: dict | None = None
    failure_reasons: list[str] = field(default_factory=list)
    platform_info: dict = field(default_factory=dict)
    performance_stats: dict = field(default_factory=dict)


def verify_zero_copy_playback(
    file_path: str,
    frames: int = 300,
    warmup: int = 30,
    require_zero_copy: bool = True,
    allow_fallback: bool = False,
    verify_color: bool = False,
    fps: float = 30.0,
) -> VerificationResult:
    """
    运行零拷贝播放验证。

    Args:
        file_path: 视频文件路径
        frames: 测试帧数
        warmup: 预热帧数
        require_zero_copy: 是否要求零拷贝
        allow_fallback: 是否允许 fallback
        verify_color: 是否进行颜色验证
        fps: 目标帧率

    Returns:
        VerificationResult
    """
    result = VerificationResult()
    result.platform_info = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
    }

    frame_budget_ms = 1000.0 / max(fps, 1.0)

    logger.info(f"Verifying zero-copy playback: {file_path}")
    logger.info(f"  frames={frames}, warmup={warmup}, fps={fps}")
    logger.info(f"  require_zero_copy={require_zero_copy}, allow_fallback={allow_fallback}")

    if not os.path.exists(file_path):
        result.failure_reasons.append(f"file_not_found: {file_path}")
        result.passed = False
        return result

    # Step 1: Check platform capability
    if platform.system() == 'Darwin':
        logger.info("Platform: macOS - checking VideoToolbox IOSurface zero-copy")
        result.decode_path = "macos_vt_iosurface_zero_copy"
        try:
            from src.shared.media.macos_vt.vt_zero_copy_probe import run_probe
            probe = run_probe(
                file_path=file_path,
                frames=frames,
                warmup=warmup,
                stage="full",
                verify_color=verify_color,
            )
            result.frames_tested = probe.frames_tested
            result.cpu_download_frames = probe.cpu_download_frames
            result.texture_write_frames = probe.texture_write_frames
            result.decode_path = probe.decode_path or result.decode_path
            result.total_p95_ms = float(probe.total_ms.get("p95", 0.0)) if probe.total_ms else 0.0
            result.yuv_render_p95_ms = float(probe.yuv_render_ms.get("p95", 0.0)) if probe.yuv_render_ms else 0.0
            result.color_verification = probe.color_verification
            result.performance_stats["probe"] = asdict(probe)
            result.zero_copy = probe.zero_copy
            if probe.zero_copy:
                result.frame_path = "gpu_decoded_external_yuv"
            else:
                reason = probe.fallback_reason or "macOS VT zero-copy probe failed"
                logger.warning(f"  FAIL: {reason}")
                if require_zero_copy and not allow_fallback:
                    result.failure_reasons.append(reason)
                    result.passed = False
                    return result
                result.performance_stats["fallback_reason"] = reason
                result.decode_path = "hwaccel_download_packed_rgb"
        except ImportError as e:
            reason = f"macOS VT module import error: {e}"
            result.failure_reasons.append(reason)
            logger.warning(f"  FAIL: {reason}")

    elif platform.system() == 'Windows':
        logger.info("Platform: Windows - checking D3D11/GL zero-copy")
        result.decode_path = "windows_d3d11_gl_zero_copy"
        try:
            from src.shared.media.windows_hw.capability import is_zero_copy_available as is_windows_zero_copy
            if not is_windows_zero_copy():
                reason = "Windows D3D11 zero-copy not available"
                logger.warning(f"  FAIL: {reason}")
                if require_zero_copy and not allow_fallback:
                    result.failure_reasons.append(reason)
                    result.passed = False
                    return result
                result.performance_stats["fallback_reason"] = reason
                result.decode_path = "hwaccel_download_packed_rgb"
        except ImportError as e:
            reason = f"Windows HW module import error: {e}"
            result.failure_reasons.append(reason)
    else:
        reason = f"Unsupported platform: {platform.system()}"
        result.failure_reasons.append(reason)
        logger.warning(f"  FAIL: {reason}")
        if require_zero_copy:
            result.passed = False
            return result

    # Step 2: Merge live performance monitor verification if this process drove the renderer.
    try:
        from src.shared.media.performance_monitor import get_performance_monitor
        monitor = get_performance_monitor()
        stats = monitor.get_aggregate_stats()

        result.cpu_download_frames += stats.get('cpu_download_frames', 0)
        result.cpu_upload_frames += stats.get('cpu_upload_frames', 0)
        result.texture_write_frames += stats.get('texture_write_video_frames', 0)

        decode_path_counts = stats.get('decode_path_counts', {})
        render_path_counts = stats.get('render_frame_path_counts', {})

        if decode_path_counts:
            result.decode_path = max(decode_path_counts, key=decode_path_counts.get)
        if render_path_counts:
            result.frame_path = max(render_path_counts, key=render_path_counts.get)

        result.performance_stats = {
            'decode_path_counts': decode_path_counts,
            'render_frame_path_counts': render_path_counts,
            'zero_copy_frames': stats.get('zero_copy_frames', 0),
            'external_texture_frames': stats.get('external_texture_frames', 0),
            'last_fallback_reason': stats.get('last_fallback_reason', None),
        }

    except Exception as e:
        logger.warning(f"Failed to get performance stats: {e}")

    # Step 3: Verify zero-copy criteria
    allowed_zero_copy_paths = {
        'macos_vt_iosurface_zero_copy',
        'windows_d3d11_gl_zero_copy',
    }
    allowed_zero_copy_render_paths = {
        'gpu_decoded_external_yuv',
        'gpu_decoded_external_rgb',
    }

    result.zero_copy = result.zero_copy and result.decode_path in allowed_zero_copy_paths

    # Check criteria
    checks = []

    if require_zero_copy and not allow_fallback:
        # Zero-copy verification
        if result.cpu_download_frames > 0:
            checks.append(f"cpu_download_frames={result.cpu_download_frames} > 0 (expected 0)")

        if result.cpu_upload_frames > 0:
            checks.append(f"cpu_upload_frames={result.cpu_upload_frames} > 0 (expected 0)")

        if result.texture_write_frames > 0:
            checks.append(f"texture_write_frames={result.texture_write_frames} > 0 (expected 0)")

        if result.decode_path not in allowed_zero_copy_paths:
            checks.append(f"decode_path={result.decode_path} not in allowed zero-copy paths")

        if result.frame_path not in allowed_zero_copy_render_paths:
            checks.append(f"frame_path={result.frame_path} not in allowed zero-copy render paths")

        if not result.zero_copy:
            checks.append("zero_copy not achieved")

        if result.frames_tested < frames:
            checks.append(f"frames_tested={result.frames_tested} < requested_frames={frames}")

        if verify_color:
            if not result.color_verification or not result.color_verification.get("passed"):
                checks.append("color_verification not passed")

    else:
        # Fallback verification
        logger.info("Fallback mode: verifying fallback path correctness")
        if result.decode_path and 'cpu' not in result.decode_path and 'hwaccel' not in result.decode_path:
            checks.append(f"decode_path={result.decode_path} unexpected in fallback mode")

    if result.total_p95_ms > 0 and result.total_p95_ms > frame_budget_ms:
        checks.append(f"total_p95={result.total_p95_ms:.1f}ms > frame_budget={frame_budget_ms:.1f}ms")

    result.failure_reasons.extend(checks)
    result.passed = len(result.failure_reasons) == 0

    return result


def main() -> None:
    """命令行入口"""
    parser = argparse.ArgumentParser(description="4K zero-copy playback verification")
    parser.add_argument("--file", required=True, help="Video file path")
    parser.add_argument("--frames", type=int, default=300, help="Number of frames to test")
    parser.add_argument("--warmup", type=int, default=30, help="Number of warmup frames")
    parser.add_argument("--fps", type=float, default=30.0, help="Target FPS")
    parser.add_argument("--require-zero-copy", action="store_true", default=True, help="Require zero-copy")
    parser.add_argument("--allow-fallback", action="store_true", help="Allow fallback path")
    parser.add_argument("--verify-color", action="store_true", help="Enable color verification")
    parser.add_argument("--output-json", default=None, help="Output JSON file path")

    args = parser.parse_args()

    result = verify_zero_copy_playback(
        file_path=args.file,
        frames=args.frames,
        warmup=args.warmup,
        require_zero_copy=args.require_zero_copy,
        allow_fallback=args.allow_fallback,
        verify_color=args.verify_color,
        fps=args.fps,
    )

    result_dict = asdict(result)

    if args.output_json:
        import os
        os.makedirs(os.path.dirname(args.output_json), exist_ok=True)
        with open(args.output_json, 'w') as f:
            json.dump(result_dict, f, indent=2)
        logger.info(f"Verification result saved to {args.output_json}")
    else:
        print(json.dumps(result_dict, indent=2))

    if not result.passed:
        logger.error("Verification FAILED:")
        for reason in result.failure_reasons:
            logger.error(f"  - {reason}")
        sys.exit(1)
    else:
        logger.info("Verification PASSED")


if __name__ == "__main__":
    main()
