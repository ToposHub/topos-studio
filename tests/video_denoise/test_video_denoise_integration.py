"""视频降噪真实渲染线程集成测试（AsyncRenderer 全链路）。

与 test_video_denoise_gpu.py（独立 standalone ModernGL 上下文直接调管线）
不同，这里构造真实的 AsyncRenderer 渲染线程（QOpenGLContext + 共享上下文
+ 命令队列 + GL mutex），验证 M5/M4.2 的生产接入路径：

1. 渲染线程启动后 VideoDenoisePipeline 在线程内创建；
2. SET_VIDEO_DENOISE 参数经命令队列投递生效；
3. RENDER_VIDEO_FRAME（播放路径）→ preview 质量降噪；
4. 暂停态 + SET_VIDEO_DENOISE_WINDOW → 窗口重放（window_frames 统计）；
5. 窗口帧号不匹配 → 自动回退因果模式；
6. seek（generation 跳变）后时域状态重置，不串帧。

需要可用的 QOpenGLContext（offscreen 平台）；不可用时跳过。
"""

from __future__ import annotations

import os
import sys
import time

# macOS 的 offscreen 平台插件不支持 QOpenGLContext，保持默认平台
# （现有 tests/video/test_video_async_renderer.py 同款做法）。
if os.environ.get('QT_QPA_PLATFORM') == 'offscreen' and sys.platform == 'darwin':
    os.environ.pop('QT_QPA_PLATFORM', None)

import numpy as np
import pytest

pytest.importorskip("PyQt6")

from src.shared.video.render_commands import RenderCommand
from src.shared.video.denoise.params import VideoDenoiseParams

pytestmark = [pytest.mark.gpu_renderer, pytest.mark.integration]


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------

def _wait_until(cond, timeout_s: float = 15.0, interval_s: float = 0.05):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(interval_s)
    return False


def _noise_bgr(w: int, h: int, seed: int, noise: float = 0.06) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.full((h, w, 3), 96, dtype=np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    base[..., 0] += 60 * np.sin(xx / 21.0 + seed)
    base[..., 1] += 40 * np.cos(yy / 17.0 + seed)
    base[..., 2] += 30 * np.sin((xx + yy) / 33.0)
    noisy = np.clip(base + rng.normal(0, noise * 255.0, (h, w, 3)), 0, 255)
    return noisy.astype(np.uint8)


def _neighbor_rgba(w: int, h: int, seed: int) -> np.ndarray:
    bgr = _noise_bgr(w, h, seed)
    rgb = bgr[..., ::-1].astype(np.float32) / 255.0
    alpha = np.ones(rgb.shape[:2] + (1,), dtype=np.float32)
    return np.ascontiguousarray(
        np.concatenate([rgb, alpha], axis=2).astype(np.float16)
    )


def _denoise_params_dict() -> dict:
    from dataclasses import replace

    params = VideoDenoiseParams()
    params = replace(
        params,
        temporal=replace(
            params.temporal, enabled=True, luma=70.0, chroma=60.0
        ),
        spatial=replace(
            params.spatial, enabled=True, mode='fast', luma=50.0, chroma=40.0
        ),
    )
    return params.to_dict()


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def renderer(qapp):
    """构造真实 AsyncRenderer 并启动渲染线程。"""
    from PyQt6.QtGui import QOpenGLContext, QOffscreenSurface, QSurfaceFormat
    from src.features.color.core.async_renderer import AsyncRenderer

    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    main_context = QOpenGLContext()
    main_context.setFormat(fmt)
    if not main_context.create():
        pytest.skip("无法创建 QOpenGLContext（无 GPU/离屏支持）")

    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    main_context.makeCurrent(surface)

    r = AsyncRenderer(main_context)
    r.start()
    if not _wait_until(lambda: r.ctx is not None, timeout_s=20.0):
        r.is_running = False
        r.wait(3000)
        pytest.skip("渲染线程初始化失败（ModernGL 上下文不可用）")
    # 管线在 YUV 自检之后创建（run() 前段），需等初始化全部完成
    if not _wait_until(lambda: r.video_denoise_pipeline is not None, timeout_s=30.0):
        r.is_running = False
        r.wait(3000)
        pytest.skip("VideoDenoisePipeline 初始化失败")
    yield r

    r.is_running = False
    r.wait(5000)
    surface.destroy()


def _submit_frame(r, frame_index: int, generation: int, seed: int,
                  w=320, h=180, reason='seek'):
    r.submit(RenderCommand.RENDER_VIDEO_FRAME, {
        'frame': _noise_bgr(w, h, seed),
        'frame_index': frame_index,
        'generation_id': generation,
        'render_params': {},
        'reason': reason,
        'quality': 'master',
    })
    return _wait_until(
        lambda: r.debug_last_rendered_video_frame_index == frame_index
    )


# ----------------------------------------------------------------------
# 测试
# ----------------------------------------------------------------------

def test_playback_frames_denoised_via_render_thread(renderer):
    """播放路径：连续帧经命令队列进入渲染线程，preview 降噪生效。"""
    r = renderer
    r.submit(RenderCommand.SET_VIDEO_DENOISE, {
        'params': _denoise_params_dict(), 'quality_mode': 'preview',
    })

    r.video_playback_active = True  # 播放中 → preview 质量
    try:
        stats0 = dict(r.video_denoise_pipeline.stats)
        ok = True
        for i in range(3):
            ok = _submit_frame(r, i, generation=1, seed=i, reason='playback') and ok
        assert ok, "播放帧未被渲染线程处理"

        assert _wait_until(
            lambda: r.video_denoise_pipeline.stats['frames']
            >= stats0.get('frames', 0) + 3
        ), "降噪管线未处理播放帧"
    finally:
        r.video_playback_active = False


def test_seek_generation_reset(renderer):
    """seek（generation 跳变 + 帧号跳跃）后时域历史重置，不串帧。"""
    r = renderer
    temporal = r.video_denoise_pipeline._temporal

    # 连续帧 10→11（正常累积）
    assert _submit_frame(r, 10, generation=2, seed=10)
    assert _submit_frame(r, 11, generation=2, seed=11)
    assert _wait_until(
        lambda: temporal.stats.get('frames_processed', 0) > 0
    )

    # seek：generation +3 且帧号跳跃到 100 → 状态必须重置
    assert _submit_frame(r, 100, generation=5, seed=100)
    # 重置后 last_frame 应为 100（历史从当前帧重建）
    assert _wait_until(lambda: temporal.state.last_frame_index == 100)


def test_paused_window_replay(renderer):
    """暂停态 + 匹配的邻帧窗口 → 走窗口重放路径。"""
    r = renderer
    assert _submit_frame(r, 50, generation=6, seed=50)

    w, h = 320, 180
    window_frames0 = r.video_denoise_pipeline.stats.get('window_frames', 0)
    r.submit(RenderCommand.SET_VIDEO_DENOISE_WINDOW, {
        'path': '/tmp/fake.mp4',
        'center_frame': 50,
        'neighbors': [
            (49, _neighbor_rgba(w, h, 49)),
            (48, _neighbor_rgba(w, h, 48)),
            (51, _neighbor_rgba(w, h, 51)),
            (52, _neighbor_rgba(w, h, 52)),
        ],
    })

    # 暂停态静态重绘（grading 语义，与控制器 request_render 相同）
    r.submit(RenderCommand.RENDER_FRAME, {
        'render_reason': 'grading',
        'viewport_size': (w, h),
        'layers': [],
        'raw_preview': {'exposure': 0.0, 'gains': (1.0, 1.0, 1.0)},
        'film_grain': {},
        'visible_rect': (0, 0, float(w), float(h)),
        'show_mask': False,
        'active_layer_index': 0,
    })

    assert _wait_until(
        lambda: r.video_denoise_pipeline.stats.get('window_frames', 0)
        >= window_frames0 + 1
    ), "暂停态邻帧窗口未被消费"


def test_paused_window_mismatch_falls_back(renderer):
    """窗口帧号与当前帧不匹配 → 忽略窗口，回退因果模式（不崩溃）。"""
    r = renderer
    assert _submit_frame(r, 51, generation=7, seed=51)

    w, h = 320, 180
    window_frames0 = r.video_denoise_pipeline.stats.get('window_frames', 0)
    frames0 = r.video_denoise_pipeline.stats.get('frames', 0)
    r.submit(RenderCommand.SET_VIDEO_DENOISE_WINDOW, {
        'path': '/tmp/fake.mp4',
        'center_frame': 999,  # 与当前帧 51 不匹配
        'neighbors': [(998, _neighbor_rgba(w, h, 998))],
    })
    r.submit(RenderCommand.RENDER_FRAME, {
        'render_reason': 'grading',
        'viewport_size': (w, h),
        'layers': [],
        'raw_preview': {'exposure': 0.0, 'gains': (1.0, 1.0, 1.0)},
        'film_grain': {},
        'visible_rect': (0, 0, float(w), float(h)),
        'show_mask': False,
        'active_layer_index': 0,
    })

    # 渲染完成且未消费窗口（frames 增长，window_frames 不变）
    assert _wait_until(
        lambda: r.video_denoise_pipeline.stats.get('frames', 0) >= frames0 + 1
    ), "回退渲染未完成"
    time.sleep(0.3)  # 确保没有迟到的窗口消费
    assert r.video_denoise_pipeline.stats.get('window_frames', 0) == window_frames0


def test_window_clear_and_reset_history(renderer):
    """空窗口清除 + RESET_VIDEO_DENOISE_HISTORY 命令正常。"""
    r = renderer
    r.submit(RenderCommand.SET_VIDEO_DENOISE_WINDOW, {})
    r.submit(RenderCommand.RESET_VIDEO_DENOISE_HISTORY, None)
    assert _submit_frame(r, 52, generation=8, seed=52)
    # 渲染器仍存活且可继续渲染
    assert r.video_denoise_pipeline.is_active()
