"""E5/FUP-07：GPU 渲染线程——所有权、latest-wins、前台帧、deferred release。

需要真 GL（cocoa/真显卡）；无 GL 环境（CI offscreen）整组跳过——与
``tools/verify_native_gpu_preview.py`` 口径一致：CPU 侧通过不冒充 GPU
验证。像素断言全部在渲染线程（probe）或 ``grabFramebuffer`` 上做。
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtGui import QSurfaceFormat  # noqa: E402
from pytestqt.qtbot import QtBot  # noqa: E402

from src.shared.playback.gl_render_worker import (  # noqa: E402
    GLVideoRenderWorker, _RenderThread,
)
from src.shared.playback.gl_video_canvas import GLVideoCanvas  # noqa: E402
from src.shared.playback.yuv_frame import YuvFrame  # noqa: E402


def test_video_canvas_uses_compatible_window_surface_format(qtbot: QtBot) -> None:
    previous = QSurfaceFormat.defaultFormat()
    host_format = QSurfaceFormat(previous)
    host_format.setVersion(4, 5)
    host_format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    host_format.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    host_format.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    host_format.setRedBufferSize(8)
    host_format.setGreenBufferSize(8)
    host_format.setBlueBufferSize(8)
    host_format.setAlphaBufferSize(8)
    host_format.setDepthBufferSize(24)
    host_format.setStencilBufferSize(8)
    host_format.setSamples(4)
    QSurfaceFormat.setDefaultFormat(host_format)
    try:
        canvas = GLVideoCanvas()
        qtbot.addWidget(canvas)
        expected = QSurfaceFormat(host_format)
        expected.setSamples(0)
        assert canvas.format() == expected
        assert QSurfaceFormat.defaultFormat() == host_format
    finally:
        QSurfaceFormat.setDefaultFormat(previous)


def _yuv_frame(color: str, w: int = 8, h: int = 6) -> YuvFrame:
    """8-bit 420 合成帧：red=(Y128,U0,V255) / blue=(Y128,U255,V0)。"""
    y = np.full((h, w), 128, np.uint8)
    u = np.full((h // 2, w // 2), 0 if color == "red" else 255, np.uint8)
    v = np.full((h // 2, w // 2), 255 if color == "red" else 0, np.uint8)
    return YuvFrame(y, u, v, bit_depth=8, subsampling="420",
                    pixel_format="420", source_width=w, source_height=h)


@pytest.fixture()
def gl_canvas(qtbot):
    """每用例一块真 GL 画布；无 GL 环境整组跳过。"""
    canvas = GLVideoCanvas()
    canvas.resize(120, 90)
    canvas.show()
    qtbot.waitUntil(lambda: canvas._ctx is not None or canvas._gpu_error,
                    timeout=8000)
    if canvas._ctx is None:
        canvas.hide()
        canvas.deleteLater()
        pytest.skip(f"无 GL 环境（{canvas._gpu_error}），E5 真 GPU 用例跳过")
    yield canvas
    canvas._release_gl()
    canvas.hide()
    canvas.deleteLater()


def _wait_rendered(qtbot, worker, minimum: int = 1):
    qtbot.waitUntil(lambda: worker.rendered_frames >= minimum, timeout=8000)


def _front_pixels(worker):
    """在渲染线程读回前台帧（RGBA8，含 letterbox）。"""
    def read(state):
        entry = state["fbos"].get("front")
        if not entry:
            return None
        _tex, fbo, w, h = entry
        raw = fbo.read(components=4, dtype="f1", alignment=1)
        return np.frombuffer(raw, np.uint8).reshape(h, w, 4)
    return worker.probe(read)


def _assert_redish(pixel):
    assert pixel[0] > 180, f"期望红色主导，得到 {pixel}"
    assert pixel[2] < 80, f"蓝色应当很低，得到 {pixel}"


def _assert_blackish(pixel):
    assert all(channel < 40 for channel in pixel[:3]), f"期望黑边，得到 {pixel}"


# ---------------------------------------------------------------------------
# 所有权：GL 在渲染线程，GUI 只提交与呈现
# ---------------------------------------------------------------------------

def test_worker_renders_off_gui_thread(qtbot, gl_canvas):
    canvas = gl_canvas
    worker = canvas._render_worker
    if worker is None or not worker.alive:
        pytest.skip(f"渲染线程不可用（{canvas._gpu_error}），回退路径由其他用例覆盖")
    worker.submit_size(canvas._size_cache or (120, 90))
    worker.submit_frame(_yuv_frame("red"))
    _wait_rendered(qtbot, worker)
    # 线程断言：上传/转换/绘制发生在专用渲染线程，不是 GUI 线程
    assert worker.render_thread_name
    assert worker.render_thread_name != threading.current_thread().name


def test_front_frame_letterbox_pixels(qtbot, gl_canvas):
    canvas = gl_canvas
    worker = canvas._render_worker
    if worker is None or not worker.alive:
        pytest.skip("渲染线程不可用")
    canvas.refresh_size_cache()
    worker.submit_size(canvas._size_cache or (120, 90))
    worker.submit_frame(_yuv_frame("red", w=8, h=4))   # 2:1 → 上下黑边
    _wait_rendered(qtbot, worker)
    pixels = _front_pixels(worker)
    assert pixels is not None and pixels.shape[2] == 4
    height, width = pixels.shape[:2]
    _assert_redish(pixels[height // 2, width // 2])     # 中心=画面
    _assert_blackish(pixels[2, 2])                      # 角=letterbox 黑边
    _assert_blackish(pixels[height - 3, width - 3])


def test_latest_wins_drops_stale_queued_frames(qtbot, gl_canvas):
    canvas = gl_canvas
    worker = canvas._render_worker
    if worker is None or not worker.alive:
        pytest.skip("渲染线程不可用")
    worker.submit_frame(_yuv_frame("blue"))
    worker.submit_frame(_yuv_frame("red"))   # 立即覆盖：排队旧帧被丢弃
    _wait_rendered(qtbot, worker)
    qtbot.wait(120)                           # 让最终帧完成发布
    pixels = _front_pixels(worker)
    _assert_redish(pixels[pixels.shape[0] // 2, pixels.shape[1] // 2])


def test_gui_present_paints_front(qtbot, gl_canvas):
    """端到端：GUI paintGL 呈现渲染线程前台帧（跨上下文纹理采样）。"""
    canvas = gl_canvas
    worker = canvas._render_worker
    if worker is None or not worker.alive:
        pytest.skip("渲染线程不可用")
    worker.submit_size(canvas._size_cache or (120, 90))
    qtbot.wait(80)  # 先排空显示窗口首次产生的交换事件
    with qtbot.waitSignal(canvas.frameSwapped, timeout=8000):
        canvas.set_image(_yuv_frame("red", w=8, h=6))
    assert worker.rendered_frames == 1
    image = canvas.grabFramebuffer()
    assert not image.isNull()
    center = image.pixelColor(image.width() // 2, image.height() // 2)
    assert center.red() > 150, f"中心应为画面（红主导），得到 {center.name()}"


# ---------------------------------------------------------------------------
# deferred release：正常关停在渲染线程释放并销账
# ---------------------------------------------------------------------------

def test_stop_releases_resources_and_cleans_registry(qtbot, gl_canvas):
    """独立画布验证完整关停（不复用模块级画布，避免影响其他用例）。"""
    canvas = GLVideoCanvas()
    canvas.resize(64, 48)
    canvas.show()
    qtbot.waitUntil(lambda: canvas._ctx is not None or canvas._gpu_error,
                    timeout=8000)
    if canvas._ctx is None:
        canvas.hide()
        canvas.deleteLater()
        pytest.skip("无 GL 环境")
    try:
        worker = canvas._render_worker
        if worker is None or not worker.alive:
            pytest.skip("渲染线程不可用")
        worker.submit_frame(_yuv_frame("red"))
        _wait_rendered(qtbot, worker)
        registry = worker.registry
        assert registry.record_count() == 2   # uploader + fbos 在账
        assert worker.stop()                  # 渲染线程有界退出
        qtbot.waitUntil(lambda: not worker.alive, timeout=3000)
        # 正常关停：所属线程释放 + 销账（台账真实，不靠 clear）
        assert registry.record_count() == 0
        canvas._render_worker = None          # 已由 stop 收尾
    finally:
        canvas._release_gl()
        canvas.hide()
        canvas.deleteLater()


def test_worker_init_failure_falls_back_to_gui_direct(qtbot, monkeypatch):
    """渲染线程初始化失败：画布回退既有 GUI 直绘路径（行为不变约束）。"""
    def _broken_init(self, state):
        raise RuntimeError("injected worker failure")

    # 先打补丁再 show——确保 initializeGL 里的 worker 走注入的失败路径
    monkeypatch.setattr(_RenderThread, "_init_gl", _broken_init)
    canvas = GLVideoCanvas()
    canvas.resize(64, 48)
    canvas.show()
    try:
        qtbot.waitUntil(lambda: canvas._ctx is not None or canvas._gpu_error,
                        timeout=8000)
        if canvas._ctx is None:
            pytest.skip("无 GL 环境")
        qtbot.waitUntil(lambda: canvas._uploader is not None
                        or canvas._gpu_error, timeout=8000)
        assert canvas._render_worker is None      # worker 退役
        assert canvas._uploader is not None       # 直绘回退就绪
        assert canvas._gpu_active
        # 直绘路径照常可用（GUI 线程内完成，作为回退被如实接受）
        canvas.set_image(_yuv_frame("red"))
        qtbot.wait(80)
        assert canvas._frame is not None
    finally:
        canvas._release_gl()
        canvas.hide()
        canvas.deleteLater()
