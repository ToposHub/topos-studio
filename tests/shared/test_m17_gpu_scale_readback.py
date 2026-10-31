"""M17.1b · GPU 缩放后回读候选路径（离屏 A/B 度量）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.shared_core

from src.shared.video.monitor_gpu_scale import (
    ab_metrics,
    cpu_resize_baseline,
    gpu_scale_readback,
)


def _gradient_frame(h=2160, w=3840):
    """4K 渐变+棋盘帧（缩放非平凡性）。"""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = xx / max(1, w - 1)
    g = yy / max(1, h - 1)
    b = ((xx // 32 + yy // 32) % 2).astype(np.float32) * 0.5
    a = np.ones_like(r)
    return (np.stack([r, g, b, a], axis=-1) * 255).round().astype(np.uint8)


class TestGpuScaleReadback:
    def test_gpu_close_to_cpu_baseline(self):
        """GPU 双线性缩放 vs cv2 INTER_LINEAR 基线：峰值差小（同族
        采样器的实现差，非语义差）。"""
        try:
            import moderngl  # noqa: F401
        except Exception:
            pytest.skip("moderngl unavailable")
        src = _gradient_frame()
        gpu = gpu_scale_readback(src, 1920, 1080)
        cpu = cpu_resize_baseline(src, 1920, 1080)
        assert gpu is not None and cpu is not None
        assert gpu.shape == (1080, 1920, 4)
        metrics = ab_metrics(src, 1920, 1080, gpu=gpu, cpu=cpu)
        assert metrics["gpu_vs_cpu_max_abs"] is not None
        # uint8 域：同族双线性实现差（半开像素中心约定）上界
        assert metrics["gpu_vs_cpu_max_abs"] <= 8.0
        # 平坦区严格一致（渐变中带）
        flat_diff = np.abs(
            gpu[540, 900:1000, 0].astype(np.int32)
            - cpu[540, 900:1000, 0].astype(np.int32))
        assert flat_diff.max() <= 2

    def test_readback_bytes_reduced(self):
        """4K→1080p：回读字节 ≈ 1/4（33MB→8.2MB 口径）。"""
        src = _gradient_frame()
        metrics = ab_metrics(src, 1920, 1080)
        assert metrics["full_frame_bytes"] == 3840 * 2160 * 4
        assert metrics["target_bytes"] == 1920 * 1080 * 4
        assert metrics["readback_ratio"] == pytest.approx(0.25, abs=0.01)

    def test_float32_frame_supported(self):
        try:
            import moderngl  # noqa: F401
        except Exception:
            pytest.skip("moderngl unavailable")
        src = (_gradient_frame(240, 320).astype(np.float32)) / 255.0
        gpu = gpu_scale_readback(src, 160, 120)
        assert gpu is not None and gpu.dtype == np.float32
        assert gpu.shape == (120, 160, 4)

    def test_invalid_inputs_return_none(self):
        src = _gradient_frame(64, 64)
        assert gpu_scale_readback(None, 32, 32) is None
        assert gpu_scale_readback(src[..., :3], 32, 32) is None
        assert gpu_scale_readback(src, 0, 32) is None

    def test_ab_metrics_without_gpu(self):
        src = _gradient_frame(240, 320)
        metrics = ab_metrics(src, 160, 120)
        assert metrics["gpu_available"] is False
        assert metrics["gpu_vs_cpu_max_abs"] is None
        assert metrics["readback_ratio"] == pytest.approx(0.25, abs=0.02)
