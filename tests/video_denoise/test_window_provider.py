"""邻帧窗口提供器（UI 侧）纯 CPU 测试：帧转换与调度语义。"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PyQt6")

from src.shared.video.denoise.window_provider import (
    VideoDenoiseWindowProvider,
    _frame_to_rgba_f16,
)
from src.shared.video.denoise.params import VideoDenoiseParams, TemporalDenoiseParams


class _FakePacked:
    def __init__(self, w, h, fmt='bgr24', dtype=np.uint8, frame_index=0):
        self.width, self.height = w, h
        self.pixel_format = fmt
        self.is_planar = False
        self.planes = None
        if dtype == np.uint8:
            self.data = np.random.randint(0, 255, (h, w, 3), dtype=np.uint8)
        else:
            self.data = np.random.randint(0, 65535, (h, w, 3), dtype=np.uint16)
        self.metadata = None
        self.bit_depth = 8 if dtype == np.uint8 else 16
        self.frame_index = frame_index


class _FakeMeta:
    def __init__(self, matrix='bt709', rng='limited'):
        self.color_matrix = matrix
        self.color_range = rng


class _FakePlanar:
    def __init__(self, w, h, frame_index=0, matrix='bt709', rng='limited'):
        self.width, self.height = w, h
        self.pixel_format = 'yuv420p'
        self.is_planar = True
        self.frame_index = frame_index
        self.bit_depth = 8
        self.metadata = _FakeMeta(matrix, rng)
        # 灰色 YUV（Y=128, U=V=128 → 中性灰 RGB）
        self.planes = (
            np.full((h, w), 128, dtype=np.uint8),
            np.full((h // 2, w // 2), 128, dtype=np.uint8),
            np.full((h // 2, w // 2), 128, dtype=np.uint8),
        )


def test_packed_bgr_conversion_and_downscale():
    rgba = _frame_to_rgba_f16(_FakePacked(64, 48), 1920)
    assert rgba.shape == (48, 64, 4)
    assert rgba.dtype == np.float16
    assert 0.0 <= float(rgba.min()) and float(rgba.max()) <= 1.0
    assert float(rgba[..., 3].mean()) == 1.0  # alpha 全 1

    # 超宽帧按比例缩到 max_width
    rgba2 = _frame_to_rgba_f16(_FakePacked(3840, 2160), 1920)
    assert rgba2.shape == (1080, 1920, 4)


def test_planar_yuv_conversion_neutral_gray():
    rgba = _frame_to_rgba_f16(_FakePlanar(64, 48), 1920)
    assert rgba.shape == (48, 64, 4)
    rgb = rgba[..., :3].astype(np.float32)
    # 中性 YUV → RGB 三通道接近相等
    spread = float(np.abs(rgb[..., 0] - rgb[..., 1]).max())
    assert spread < 0.02, f"BT.709 中性灰转换偏差过大: {spread}"


def test_invalid_frame_returns_none():
    class Bad:
        width = height = 0

    assert _frame_to_rgba_f16(Bad(), 1920) is None


def test_schedule_skips_when_temporal_inactive():
    """temporal 关闭时 schedule 静默忽略（不创建工作线程任务）。"""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    provider = VideoDenoiseWindowProvider()
    params = VideoDenoiseParams()  # temporal 默认关闭
    assert not params.temporal.is_active()
    provider.schedule('/tmp/x.mp4', 10, params)  # 应静默返回
    # 无异常即通过


def test_schedule_enqueues_with_active_temporal(qtbot=None):
    """temporal 启用时 schedule 派发后台任务（帧服务不可用 → 空 payload）。"""
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer

    app = QApplication.instance() or QApplication([])
    from dataclasses import replace

    params = replace(
        VideoDenoiseParams(),
        temporal=TemporalDenoiseParams(enabled=True, luma=70.0, chroma=60.0),
    )

    provider = VideoDenoiseWindowProvider()
    received = []
    provider.window_ready.connect(lambda payload: received.append(payload))

    provider.schedule('/definitely/not/a/video.mp4', 10, params)
    # 处理 QTimer(0) + 后台线程（open_clip 失败 → 空 payload）
    deadline_ms = 3000
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_ms / 1000:
        app.processEvents()
        if received:
            break
        time.sleep(0.02)

    assert received, "window_ready 未发射"
    # 文件不存在 → 空 payload（清除语义）
    assert received[0] == {}
    provider.shutdown()
