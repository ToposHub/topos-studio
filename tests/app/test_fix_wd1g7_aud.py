"""wd1-g7 批 D（Major×13，多控制器）修复回归测试。

覆盖条目：
* AUD-2800 —— closeEvent 的 Discard 分支不再把脏状态写回主工程文件。
* AUD-2801 —— dropEvent 改走 _open_media_path 路由（视频/图像/邻接工程）。
* AUD-2802 —— DeckLink 监视输出回读的 makeCurrent 补 doneCurrent（含嵌套
  所有权跟踪与 viewport 抓取 finally）。
* AUD-2803 —— _SourceViewerDecodeWorker 异常路径释放已解码 GPU 帧。
* AUD-2804 —— load_video 失败后旧视频会话状态保留（面板/路径/缓存）。
* AUD-2805 —— 图像加载替换异类工程前先走保存确认（_ensure_image_project
  / load_image 双入口）。
* AUD-2901 —— _composite_startup_fallback 异常分支不再引用未定义 _logger。
* AUD-2902 —— request_render 渲染管线异常带堆栈记录并提示状态栏。
* AUD-2903 —— 图层结构/重置操作统一先压撤销历史。
* AUD-2920 —— get_asset_status 契约对齐：pending/running/failed/stale 可达。
* AUD-3000 —— menu_toolbar_controller 导入 QLabel（GPU 信息标签可用）。
* AUD-3001 —— panel_opacity 对停靠 dock 生效 + 启动时统一应用。
* GAP-007 —— file_controller.py 低覆盖（22%）补测：本文件新增
  load_video/load_image/dropEvent/关闭确认/DeckLink 回读/解码 worker 等
  路径的行为锁定测试。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = [pytest.mark.app, pytest.mark.regression]

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDockWidget,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QWidget,
)

from src.app.controllers import file_controller as fc
from src.app.controllers.file_controller import FileOpsMixin
from src.app.controllers.layer_controller import LayerOpsMixin
from src.app.controllers.media_analysis_controller import (
    MediaAnalysisController,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_STALE,
    STATUS_UNKNOWN,
)


# ═══════════════════════════════════════════════════════════════════════
# AUD-2800: closeEvent 的 Discard 语义
# ═══════════════════════════════════════════════════════════════════════


class _QuestionBox:
    """替身 QMessageBox：可编程的 question() 返回值。"""

    StandardButton = QMessageBox.StandardButton
    reply: Any = None
    asked: int = 0

    @staticmethod
    def question(*_args: Any, **_kwargs: Any) -> Any:
        _QuestionBox.asked += 1
        return _QuestionBox.reply


def _make_close_host(*, dirty: bool, filepath: str | None = "/p/x.topos") -> Any:
    """_confirm_save_before_close / _persist_active_project_before_close 宿主。"""
    host = SimpleNamespace(
        _current_edit_project=object() if dirty or filepath else None,
        _current_edit_project_filepath=filepath,
        edit_project_dirty=dirty,
        save_router=MagicMock(),
        save_file=MagicMock(),
    )
    # _has_unsaved_changes 复用 mixin 真实实现（锁定其判脏语义）。
    host._has_unsaved_changes = lambda: FileOpsMixin._has_unsaved_changes(host)
    return host


def test_close_discard_skips_final_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户选 Discard：closeEvent 收尾不得再 save_router.save() 写主工程。"""
    monkeypatch.setattr(fc, "QMessageBox", _QuestionBox)
    _QuestionBox.asked = 0
    _QuestionBox.reply = QMessageBox.StandardButton.Discard

    host = _make_close_host(dirty=True)
    assert FileOpsMixin._confirm_save_before_close(host) is True
    assert host._user_discarded_changes is True

    FileOpsMixin._persist_active_project_before_close(host)
    host.save_router.save.assert_not_called()


def test_close_save_choice_still_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户选 Save：正常保存路径不受 Discard 标志影响。"""
    monkeypatch.setattr(fc, "QMessageBox", _QuestionBox)
    _QuestionBox.reply = QMessageBox.StandardButton.Save

    host = _make_close_host(dirty=True)
    host.save_file = MagicMock(side_effect=lambda: setattr(host, "edit_project_dirty", False))
    assert FileOpsMixin._confirm_save_before_close(host) is True
    assert host._user_discarded_changes is False

    FileOpsMixin._persist_active_project_before_close(host)
    host.save_router.save.assert_called_once_with()


def test_close_without_unsaved_changes_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    """无未保存修改：不弹窗，收尾保存照常（回归锁定）。"""
    monkeypatch.setattr(fc, "QMessageBox", _QuestionBox)
    _QuestionBox.asked = 0

    host = _make_close_host(dirty=False, filepath=None)
    assert FileOpsMixin._confirm_save_before_close(host) is True
    assert _QuestionBox.asked == 0

    FileOpsMixin._persist_active_project_before_close(host)
    host.save_router.save.assert_called_once_with()


# ═══════════════════════════════════════════════════════════════════════
# AUD-2801: dropEvent 路由
# ═══════════════════════════════════════════════════════════════════════


class _FakeUrl:
    def __init__(self, path: str) -> None:
        self._path = path

    def toLocalFile(self) -> str:
        return self._path


class _FakeMimeData:
    def __init__(self, urls: list) -> None:
        self._urls = urls

    def urls(self) -> list:
        return self._urls


class _FakeDropEvent:
    def __init__(self, files: list) -> None:
        self._mime = _FakeMimeData([_FakeUrl(f) for f in files])

    def mimeData(self) -> _FakeMimeData:
        return self._mime


def test_drop_event_routes_through_open_media_path() -> None:
    """拖入文件走 _open_media_path 分发，不再一律 load_image。"""
    host = SimpleNamespace(
        _open_media_path=MagicMock(),
        load_image=MagicMock(),
    )
    FileOpsMixin.dropEvent(host, _FakeDropEvent(["/media/a.mov", "/media/b.png"]))

    host._open_media_path.assert_called_once_with("/media/a.mov")
    host.load_image.assert_not_called()


def test_open_media_path_routes_video_to_load_video() -> None:
    """_open_media_path 对视频走 load_video（dropEvent 修复的路由依据）。"""
    host = SimpleNamespace(
        current_page="Edit",
        load_video=MagicMock(),
        load_image=MagicMock(),
        _reject_standalone_video_for_color=MagicMock(),
        save_router=MagicMock(),
        _open_project_path=MagicMock(),
        _read_project_mode_from_manifest=MagicMock(return_value=None),
    )
    FileOpsMixin._open_media_path(host, "/media/a.mp4")
    host.load_video.assert_called_once_with("/media/a.mp4")
    host.load_image.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# AUD-2802: DeckLink 回读的 makeCurrent/doneCurrent
# ═══════════════════════════════════════════════════════════════════════


class _FakeGLContext:
    pass


def _patch_qopenglcontext(monkeypatch: pytest.MonkeyPatch, current: Any) -> None:
    """把 PyQt6.QtGui.QOpenGLContext 换成受控替身（currentContext 可编程）。"""

    class _FakeQOpenGLContext:
        @staticmethod
        def currentContext() -> Any:
            return current

    monkeypatch.setattr("PyQt6.QtGui.QOpenGLContext", _FakeQOpenGLContext)


class _RecordingViewport:
    """记录 makeCurrent/doneCurrent 的 GLCanvasViewport 替身。"""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self.make_calls = 0
        self.done_calls = 0

    def context(self) -> Any:
        return self._ctx

    def makeCurrent(self) -> None:
        self.make_calls += 1

    def doneCurrent(self) -> None:
        self.done_calls += 1


def test_make_gl_viewport_current_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    """上下文未 current → 取得并报告所有权；已 current（嵌套）→ 复用不释放。"""
    widget_ctx = _FakeGLContext()
    other_ctx = _FakeGLContext()
    _patch_qopenglcontext(monkeypatch, other_ctx)

    viewport = _RecordingViewport(widget_ctx)
    host = SimpleNamespace()

    # 上下文归别的对象 → 需要取得
    assert FileOpsMixin._make_gl_viewport_current(host, viewport) is True
    assert viewport.make_calls == 1
    FileOpsMixin._release_gl_viewport_current(host, viewport, True, source="t")
    assert viewport.done_calls == 1

    # 上下文已归本 widget（嵌套调用）→ 复用，不 makeCurrent
    _patch_qopenglcontext(monkeypatch, widget_ctx)
    assert FileOpsMixin._make_gl_viewport_current(host, viewport) is False
    assert viewport.make_calls == 1
    # 无所有权时 release 是 no-op
    FileOpsMixin._release_gl_viewport_current(host, viewport, False, source="t")
    assert viewport.done_calls == 1


def test_grab_viewport_releases_context_on_render_failure(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """viewport 抓取中途抛错：finally 必须补 doneCurrent（此前完全没有）。"""
    widget_ctx = _FakeGLContext()
    _patch_qopenglcontext(monkeypatch, _FakeGLContext())

    viewport = _RecordingViewport(widget_ctx)

    def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("scope view exploded")

    engine = SimpleNamespace(
        renderer=SimpleNamespace(current_front_tex_id=7),
        render_scope_view=_boom,
    )
    host = FileOpsMixin.__new__(FileOpsMixin)
    host.engine = engine
    host.canvas = SimpleNamespace(gl_viewport=viewport)
    host._decklink_monitor_debug_log = lambda *a, **k: None
    controller = SimpleNamespace(config=SimpleNamespace(width=1920, height=1080))

    assert FileOpsMixin._grab_color_viewport_monitor_frame(host, controller) is None
    assert viewport.make_calls == 1
    assert viewport.done_calls == 1


def test_grab_viewport_releases_context_on_success(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """viewport 抓取成功路径同样释放上下文。"""
    import numpy as np

    widget_ctx = _FakeGLContext()
    _patch_qopenglcontext(monkeypatch, _FakeGLContext())

    viewport = _RecordingViewport(widget_ctx)
    engine = SimpleNamespace(
        renderer=SimpleNamespace(current_front_tex_id=7),
        render_scope_view=lambda **_kw: np.zeros((1080, 1920, 3), dtype=np.uint8),
    )
    host = FileOpsMixin.__new__(FileOpsMixin)
    host.engine = engine
    host.canvas = SimpleNamespace(gl_viewport=viewport)
    host._decklink_monitor_debug_log = lambda *a, **k: None
    controller = SimpleNamespace(config=SimpleNamespace(width=1920, height=1080))

    frame = FileOpsMixin._grab_color_viewport_monitor_frame(host, controller)
    assert frame is not None and frame.shape[:2] == (1080, 1920)
    assert viewport.make_calls == 1
    assert viewport.done_calls == 1


class _FakeGLModule:
    """离屏替身 GL 模块：真实 PyOpenGL 在无当前上下文时会段错误，
    这里以纯 Python 替身驱动 _submit_color_monitor_direct_readback 全流程。"""

    GL_READ_FRAMEBUFFER_BINDING = 1
    GL_DRAW_FRAMEBUFFER_BINDING = 2
    GL_PIXEL_PACK_BUFFER_BINDING = 3
    GL_PIXEL_PACK_BUFFER = 4
    GL_READ_FRAMEBUFFER = 5
    GL_DRAW_FRAMEBUFFER = 6
    GL_COLOR_ATTACHMENT0 = 7
    GL_TEXTURE_2D = 8
    GL_FRAMEBUFFER_COMPLETE = 9
    GL_RGBA = 10
    GL_UNSIGNED_BYTE = 11
    GL_PACK_ALIGNMENT = 12

    fail_at: str | None = None

    def _maybe_raise(self, name: str) -> None:
        if self.fail_at == name:
            raise RuntimeError(f"GL {name} failed (no context)")

    def glGetIntegerv(self, _key: int) -> int:
        self._maybe_raise("glGetIntegerv")
        return 0

    def glGenFramebuffers(self, _n: int) -> list:
        self._maybe_raise("glGenFramebuffers")
        return [5]

    def glGenBuffers(self, _n: int) -> list:
        return [6, 7]

    def glBindFramebuffer(self, *_args: Any) -> None:
        pass

    def glFramebufferTexture2D(self, *_args: Any) -> None:
        pass

    def glReadBuffer(self, *_args: Any) -> None:
        pass

    def glCheckFramebufferStatus(self, _key: int) -> int:
        self._maybe_raise("glCheckFramebufferStatus")
        return self.GL_FRAMEBUFFER_COMPLETE

    def glBindBuffer(self, *_args: Any) -> None:
        pass

    def glPixelStorei(self, *_args: Any) -> None:
        pass

    def glReadPixels(self, *_args: Any, **_kwargs: Any) -> bytes:
        self._maybe_raise("glReadPixels")
        return bytes(64 * 64 * 4)


def _install_fake_opengl(monkeypatch: pytest.MonkeyPatch, gl: _FakeGLModule) -> None:
    """让 file_controller 内的 ``from OpenGL import GL as gl`` 取到替身。"""
    import types

    fake_opengl = types.ModuleType("OpenGL")
    fake_opengl.GL = gl
    monkeypatch.setitem(sys.modules, "OpenGL", fake_opengl)
    monkeypatch.setitem(sys.modules, "OpenGL.GL", gl)


def _direct_readback_host(viewport: _RecordingViewport) -> Any:
    host = FileOpsMixin.__new__(FileOpsMixin)
    # 顶层调用：声明取得上下文所有权（真实调用方为信号槽）。
    host._make_gl_viewport_current = lambda vp: True
    host._decklink_monitor_warning_log = lambda *a, **k: None
    host._decklink_monitor_debug_log = lambda *a, **k: None
    host._decklink_color_direct_readback_last_emit = 0.0
    host.video_controller = SimpleNamespace(current_frame=0)
    host.engine = SimpleNamespace()
    host.canvas = SimpleNamespace(gl_viewport=viewport)
    return host


def test_direct_readback_releases_context_on_success(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """直接回读成功：取得的上下文在 finally 中 doneCurrent。"""
    gl = _FakeGLModule()
    _install_fake_opengl(monkeypatch, gl)
    viewport = _RecordingViewport(_FakeGLContext())
    host = _direct_readback_host(viewport)
    controller = SimpleNamespace(
        config=SimpleNamespace(width=64, height=64, fps=25.0, mode="HD"),
        submit_video_frame=MagicMock(return_value=True),
    )

    result = FileOpsMixin._submit_color_monitor_direct_readback(
        host, tex_id=11, controller=controller, width=64, height=64,
        source="test",
    )
    assert result is True
    assert viewport.done_calls == 1


def test_direct_readback_releases_context_on_gl_failure(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """直接回读 GL 中途失败：finally 仍必须 doneCurrent。"""
    gl = _FakeGLModule()
    gl.fail_at = "glReadPixels"
    _install_fake_opengl(monkeypatch, gl)
    viewport = _RecordingViewport(_FakeGLContext())
    host = _direct_readback_host(viewport)
    controller = SimpleNamespace(
        config=SimpleNamespace(width=64, height=64, fps=25.0, mode="HD"),
        submit_video_frame=MagicMock(return_value=True),
    )

    result = FileOpsMixin._submit_color_monitor_direct_readback(
        host, tex_id=11, controller=controller, width=64, height=64,
        source="test",
    )
    assert result is False
    assert viewport.done_calls == 1


# ═══════════════════════════════════════════════════════════════════════
# AUD-2803: 解码 worker 的 GPU 帧所有权
# ═══════════════════════════════════════════════════════════════════════


def _drive_decode_worker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create_raises: bool = False,
    submit_ok: bool = True,
) -> tuple[list, list, list]:
    """跑一轮 _SourceViewerDecodeWorker：返回 (released, cpu_fallback, submitted)。"""
    import src.features.edit.facade as edit_facade
    import src.shared.media.gpu_frame as gpu_frame_mod
    from src.app.controllers.file_controller import _SourceViewerDecodeWorker

    sentinel_frame = SimpleNamespace(name="decoded-frame")
    released: list = []
    emitted: list = []
    expect_emit = create_raises or not submit_ok

    monkeypatch.setattr(
        gpu_frame_mod, "safe_release_gpu_frame",
        lambda frame: released.append(frame))

    if create_raises:
        def _boom(**_kwargs: Any) -> None:
            raise RuntimeError("task construction failed")
        monkeypatch.setattr(edit_facade, "create_edit_render_task", _boom)
    else:
        monkeypatch.setattr(
            edit_facade, "create_edit_render_task",
            lambda **_kwargs: {"task": True})

    class _Manager:
        def get_frame(self, _path: str, _frame: int) -> tuple:
            return sentinel_frame, "gpu"

    class _Renderer:
        def __init__(self) -> None:
            self.submitted: list = []

        def submit(self, task: Any) -> bool:
            self.submitted.append(task)
            return submit_ok

    renderer = _Renderer()
    worker = _SourceViewerDecodeWorker(_Manager(), renderer)
    # DirectConnection：测试线程无事件循环，queued 信号永远收不到；
    # 直接连接让回调在 worker 线程内同步执行。
    worker.signals.cpu_fallback_requested.connect(
        lambda f: emitted.append(f), Qt.ConnectionType.DirectConnection)

    worker.request("/video.mov", 42, interactive=False, generation=1)
    worker.start()

    def _settled() -> bool:
        if expect_emit:
            # 失败路径：release 与 cpu_fallback 都要落地。
            return bool(released) and bool(emitted)
        return bool(renderer.submitted)

    deadline = time.time() + 5.0
    while time.time() < deadline and not _settled():
        time.sleep(0.01)
    worker.stop()

    return released, emitted, renderer.submitted


def test_decode_worker_releases_frame_when_task_construction_raises(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """create_edit_render_task 抛异常：已解码帧必须释放（此前泄漏）。"""
    released, emitted, _submitted = _drive_decode_worker(
        monkeypatch, create_raises=True)
    assert len(released) == 1, "异常路径必须释放已解码帧（AUD-2803）"
    assert emitted == [42]


def test_decode_worker_releases_frame_when_submit_rejects(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """submit 返回 False：释放帧并请求 CPU 兜底（回归锁定）。"""
    released, emitted, _submitted = _drive_decode_worker(
        monkeypatch, submit_ok=False)
    assert len(released) == 1
    assert emitted == [42]


def test_decode_worker_transfers_ownership_on_successful_submit(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """submit 成功：帧所有权移交渲染任务，worker 不得释放。"""
    released, emitted, submitted = _drive_decode_worker(
        monkeypatch, submit_ok=True)
    assert released == [], "提交成功后帧归渲染队列，worker 不得释放"
    assert emitted == []
    assert len(submitted) == 1


# ═══════════════════════════════════════════════════════════════════════
# AUD-2804: load_video 失败保留旧会话
# ═══════════════════════════════════════════════════════════════════════


def _make_load_video_host(*, load_ok: bool) -> Any:
    video_controller = MagicMock()
    video_controller.load_video.return_value = load_ok
    video_controller.get_video_info.return_value = {
        "fps": 24.0, "width": 64, "height": 64, "total_frames": 10,
    }
    host = SimpleNamespace(
        current_page="Edit",
        current_file_path="/old/session.mov",
        current_file_is_video=True,
        video_controller=video_controller,
        canvas=MagicMock(),
        status_bar=MagicMock(),
        save_router=MagicMock(),
        grading_panel=MagicMock(),
        hsl_curves_panel=MagicMock(),
        effects_panel=MagicMock(),
        denoise_panel=MagicMock(),
        _composite_available=False,
        composite_viewer=None,
        video_player=None,
        _topos_video_player=None,
        _libav_video_player=None,
        _get_work_mode=lambda: "all",
        _clear_color_timeline_clip_playback_context=lambda: None,
        _add_to_recent_files=MagicMock(),
        _sync_color_timeline_clip_video_controls=MagicMock(),
        _connect_video_control_bar=MagicMock(),
        _load_video_project=MagicMock(),
        _schedule_video_denoise_window=MagicMock(),
        _video_denoise_window_provider_obj=None,
    )
    return host


def test_load_video_failure_preserves_old_session() -> None:
    """加载失败：面板/save_router/路径/最近文件全部保留旧会话状态。"""
    host = _make_load_video_host(load_ok=False)

    FileOpsMixin.load_video(host, "/bad/new_video.mov")

    assert host.current_file_path == "/old/session.mov"
    assert host.current_file_is_video is True
    host.grading_panel.reset_all.assert_not_called()
    host.save_router.clear_cache.assert_not_called()
    host._add_to_recent_files.assert_not_called()
    host._load_video_project.assert_not_called()
    host.status_bar.showMessage.assert_called_once()


def test_load_video_success_applies_session_swaps_after_load() -> None:
    """加载成功：面板重置/缓存清理/最近文件在新会话生效。"""
    host = _make_load_video_host(load_ok=True)

    FileOpsMixin.load_video(host, "/ok/new_video.mov")

    assert host.current_file_path == "/ok/new_video.mov"
    assert host.current_file_is_video is True
    host.grading_panel.reset_all.assert_called_once()
    host.save_router.clear_cache.assert_called_once()
    host._add_to_recent_files.assert_called_once_with("/ok/new_video.mov")
    host._load_video_project.assert_called_once_with("/ok/new_video.mov")


# ═══════════════════════════════════════════════════════════════════════
# AUD-2805: 图像加载替换异类工程的保存确认
# ═══════════════════════════════════════════════════════════════════════


def _timeline_project() -> Any:
    return SimpleNamespace(timelines=["t1"], get_asset_by_path=lambda _p: None)


def test_confirm_replacement_runs_close_lifecycle_for_timeline_project() -> None:
    """替换时间线工程：走 _close_current_project 保存确认。"""
    closed: list = []
    host = SimpleNamespace(
        _current_edit_project=_timeline_project(),
        _close_current_project=lambda: (closed.append(1), True)[1],
    )
    assert FileOpsMixin._confirm_project_replacement_for_image(host, "/x/new.png") is True
    assert closed == [1]


def test_confirm_replacement_aborts_when_user_cancels() -> None:
    """用户取消关闭确认：图像加载必须中止（返回 False）。"""
    host = SimpleNamespace(
        _current_edit_project=_timeline_project(),
        _close_current_project=lambda: False,
    )
    assert FileOpsMixin._confirm_project_replacement_for_image(host, "/x/new.png") is False


def test_confirm_replacement_same_asset_project_skips_close() -> None:
    """同一图像自己的工程（无时间线且含该素材）：不发生替换、不弹确认。"""
    closed: list = []
    project = SimpleNamespace(
        timelines=[],
        get_asset_by_path=lambda p: SimpleNamespace(path=p),
    )
    host = SimpleNamespace(
        _current_edit_project=project,
        _close_current_project=lambda: (closed.append(1), True)[1],
    )
    assert FileOpsMixin._confirm_project_replacement_for_image(host, "/x/a.png") is True
    assert closed == []


def test_confirm_replacement_no_project_proceeds() -> None:
    host = SimpleNamespace(_current_edit_project=None)
    assert FileOpsMixin._confirm_project_replacement_for_image(host, "/x/a.png") is True


def test_ensure_image_project_aborts_without_creating_when_cancelled() -> None:
    """_ensure_image_project 兜底：确认被取消时不创建新工程、返回 False。"""
    project = _timeline_project()
    host = FileOpsMixin.__new__(FileOpsMixin)
    host._current_edit_project = project
    host._close_current_project = lambda: False
    host.media_pool_panel = MagicMock()
    host.timeline_panel = MagicMock()
    host.save_router = MagicMock()
    host._sync_mg_preset_project_root = MagicMock()
    host.setWindowTitle = MagicMock()
    assert FileOpsMixin._ensure_image_project(host, "/x/a.png") is False
    assert host._current_edit_project is project, "取消时原工程必须原样保留"


def test_load_image_aborts_before_decode_when_replacement_cancelled(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """load_image：替换确认被取消 → 解码/工程创建都不发生。"""
    load_image_calls: list = []

    def _record_load(path: str) -> tuple:
        load_image_calls.append(path)
        return (None, None, None)

    monkeypatch.setattr(fc.FileIO, "load_image", _record_load)

    project = _timeline_project()
    host = FileOpsMixin.__new__(FileOpsMixin)
    host.current_file_path = None
    host._current_edit_project = project
    host._close_current_project = lambda: False
    host.unload_video = MagicMock()
    host.save_router = MagicMock()
    host.canvas = MagicMock()
    host.mask_panel = MagicMock()
    host.engine = MagicMock()

    FileOpsMixin.load_image(host, "/x/a.png", create_standalone_session=True)

    assert load_image_calls == [], "取消后不得继续解码"
    assert host._current_edit_project is project


# ═══════════════════════════════════════════════════════════════════════
# AUD-2901: _composite_startup_fallback 的 _logger NameError
# ═══════════════════════════════════════════════════════════════════════


def _make_composite_fallback_host() -> Any:
    def _raise_hook(_page: str) -> None:
        raise ValueError("enter_page_hook failed")

    composite_page = SimpleNamespace(
        _is_active=False,
        initialize_renderer=MagicMock(),
        update=MagicMock(),
        requires_color_output_input=lambda: False,
    )
    return SimpleNamespace(
        _composite_adapter=None,
        _composite_startup_pending=True,
        current_page="Composite",
        _composite_available=True,
        composite_viewer=SimpleNamespace(update=MagicMock()),
        dock_viewport=SimpleNamespace(setWidget=MagicMock()),
        composite_page=composite_page,
        _composite_enter_needed_on_startup=True,
        _composite_timeline_context_established=False,
        enter_page_hook=_raise_hook,
    )


def test_composite_startup_fallback_tolerates_hook_failure(
        caplog: pytest.LogCaptureFixture) -> None:
    """enter_page_hook 抛错：容错分支记录告警并继续，不再 NameError。"""
    host = _make_composite_fallback_host()

    with caplog.at_level(logging.WARNING, logger="src.app.controllers.layer_controller"):
        LayerOpsMixin._composite_startup_fallback(host)  # 不得抛 NameError

    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING"
        and "Composite fallback enter_page_hook failed" in r.getMessage()
    ]
    assert warnings, "应经模块级 logger 记录告警（原实现引用未定义 _logger）"
    assert host.composite_page._is_active is True


# ═══════════════════════════════════════════════════════════════════════
# AUD-2902: request_render 异常可见性
# ═══════════════════════════════════════════════════════════════════════


def test_request_render_logs_pipeline_exception_with_traceback(
        caplog: pytest.LogCaptureFixture) -> None:
    """渲染管线异常：带堆栈记录 + 状态栏提示，不再单行无堆栈吞掉。"""

    class _Engine:
        layers = []

        def render_pipeline(self, **_kwargs: Any) -> None:
            raise RuntimeError("pipeline invariant broken")

        def get_active_layer(self):
            return None

    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    ctrl.engine = _Engine()
    ctrl.layer_panel = MagicMock()
    ctrl.status_bar = MagicMock()
    ctrl._invalidate_video_render_params_if_playing = lambda: None

    with caplog.at_level(logging.ERROR, logger="src.app.controllers.layer_controller"):
        ctrl.request_render(render_reason="grading")  # 不得向上抛

    errors = [
        r for r in caplog.records
        if r.levelname == "ERROR" and "request_render failed" in r.getMessage()
    ]
    assert errors, "必须记录渲染失败"
    assert errors[0].exc_info is not None, "必须保留完整堆栈（AUD-2902）"
    ctrl.status_bar.showMessage.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# AUD-2903: 图层结构/重置操作的撤销历史
# ═══════════════════════════════════════════════════════════════════════


class _HistoryEngine:
    """带真实 HistoryManager 的引擎替身（结构操作语义与 ImageEngine 对齐）。"""

    def __init__(self) -> None:
        from src.features.color.core.layer import Layer

        self._layer_cls = Layer
        self.layers = [Layer("L1"), Layer("L2")]
        self.img_full_original = __import__("numpy").zeros((4, 4, 3), dtype="uint8")
        self.active_layer_index = 0
        self.history = None  # set by factory
        self.calls: list = []

    def get_current_image(self):
        return self.img_full_original

    def get_active_layer(self):
        return self.layers[self.active_layer_index] if self.layers else None

    def set_active_layer(self, index: int) -> None:
        if 0 <= index < len(self.layers):
            self.active_layer_index = index

    def add_layer(self):
        layer = self._layer_cls(f"Layer {len(self.layers) + 1}")
        self.layers.append(layer)
        self.active_layer_index = len(self.layers) - 1
        return layer

    def remove_layer(self, index: int) -> None:
        if len(self.layers) > 1:
            self.layers.pop(index)
            self.active_layer_index = max(
                0, min(self.active_layer_index, len(self.layers) - 1))

    def discard_changes(self) -> None:
        self.calls.append("discard")
        layer = self.get_active_layer()
        if layer is not None:
            layer.reset_params()

    def reset_all_layers(self) -> None:
        self.calls.append("reset_all")
        for layer in self.layers:
            layer.reset_params()

    def reset_flip(self) -> None:
        self.calls.append("reset_flip")

    def render_pipeline(self, **_kwargs: Any) -> None:
        self.calls.append("render")


def _make_history_ctrl() -> tuple:
    from src.features.color.core.history import HistoryManager

    engine = _HistoryEngine()
    engine.history = HistoryManager()
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    ctrl.engine = engine
    ctrl.layer_panel = MagicMock()
    ctrl.status_bar = MagicMock()
    ctrl.request_render = MagicMock()
    ctrl.update_from_layer = MagicMock()
    ctrl.raw_panel = MagicMock()
    ctrl.canvas = MagicMock()
    ctrl.config_manager = SimpleNamespace(get=lambda _k, _d=None: False)
    ctrl._trigger_autosave = MagicMock()
    ctrl.mask_panel = MagicMock()
    ctrl.plugin_panel = MagicMock()
    ctrl.update_canvas = MagicMock()
    return ctrl, engine


def _snapshot_layers(engine: _HistoryEngine, index: int) -> list:
    return engine.history.undo_stack[index]["layers"]


def test_layer_added_pushes_pre_add_history() -> None:
    """新增图层：压入变更前快照（Ctrl+Z 回到新增前）。"""
    ctrl, engine = _make_history_ctrl()
    ctrl.on_layer_added()

    assert len(engine.layers) == 3
    assert len(engine.history.undo_stack) == 1
    assert len(_snapshot_layers(engine, 0)) == 2, "快照必须封存新增前的 2 层"


def test_layer_removed_pushes_history_and_skips_last_layer() -> None:
    """删除图层压栈；最后一层引擎拒绝删除时不产生空撤销步骤。"""
    ctrl, engine = _make_history_ctrl()
    ctrl.on_layer_removed(0)
    assert len(engine.layers) == 1
    assert len(engine.history.undo_stack) == 1
    assert len(_snapshot_layers(engine, 0)) == 2

    # 只剩 1 层：引擎 no-op，不得压栈
    ctrl.on_layer_removed(0)
    assert len(engine.layers) == 1
    assert len(engine.history.undo_stack) == 1, "no-op 删除不得产生空撤销步骤"


def test_layer_moved_pushes_history() -> None:
    """图层移动前压栈（快照记录移动前顺序）。"""
    ctrl, engine = _make_history_ctrl()
    order_before = [l.name for l in engine.layers]
    ctrl.on_layer_moved(0, 1)
    assert [l.name for l in engine.layers] == list(reversed(order_before))
    assert len(engine.history.undo_stack) == 1
    assert [d["name"] for d in _snapshot_layers(engine, 0)] == order_before


def test_layer_visibility_and_rename_push_history_only_on_change() -> None:
    """显隐/重命名：值变化压栈，no-op 不压栈。"""
    ctrl, engine = _make_history_ctrl()

    ctrl.on_layer_visibility_changed(0, engine.layers[0].visible)  # no-op
    assert len(engine.history.undo_stack) == 0

    ctrl.on_layer_visibility_changed(0, not engine.layers[0].visible)
    assert len(engine.history.undo_stack) == 1

    ctrl.on_layer_renamed(0, engine.layers[0].name)  # no-op
    assert len(engine.history.undo_stack) == 1

    ctrl.on_layer_renamed(0, "renamed")
    assert len(engine.history.undo_stack) == 2
    assert engine.layers[0].name == "renamed"


def test_adjustments_reset_pushes_pre_reset_params() -> None:
    """discard_changes 前压栈：快照保留重置前的调色参数。"""
    ctrl, engine = _make_history_ctrl()
    engine.layers[0].grading_params["exposure"] = 1.5

    ctrl.on_adjustments_reset()

    assert "discard" in engine.calls
    assert len(engine.history.undo_stack) == 1
    snapshot = _snapshot_layers(engine, 0)
    assert snapshot[0]["grading_params"]["exposure"] == 1.5, (
        "撤销快照必须封存重置前的参数（AUD-2903）")
    assert engine.layers[0].grading_params["exposure"] == 0


def test_reset_all_pushes_history() -> None:
    """Reset All（含翻转复位）前压栈。"""
    ctrl, engine = _make_history_ctrl()
    engine.layers[1].grading_params["exposure"] = -0.5

    ctrl.on_reset_all_click()

    assert "reset_all" in engine.calls and "reset_flip" in engine.calls
    assert len(engine.history.undo_stack) == 1
    snapshot = _snapshot_layers(engine, 0)
    assert snapshot[1]["grading_params"]["exposure"] == -0.5


def test_layer_history_skipped_without_image() -> None:
    """无图像会话：压栈为 no-op（与 push_state 语义一致），不抛错。"""
    ctrl, engine = _make_history_ctrl()
    engine.img_full_original = None
    ctrl.on_layer_added()
    assert len(engine.history.undo_stack) == 0


# ═══════════════════════════════════════════════════════════════════════
# AUD-2920: get_asset_status 契约
# ═══════════════════════════════════════════════════════════════════════


def _make_analysis_controller(project: Any = None) -> MediaAnalysisController:
    from src.features.edit.ai_editing.analysis_scheduler import AnalysisJobScheduler
    from src.features.edit.ai_editing.analysis_service import AnalysisService

    if project is None:
        # 默认工程桩：无素材（指纹不可解析 → 不误报 stale）。
        project = SimpleNamespace(project_id="p1", revision=1, assets={})
    return MediaAnalysisController(
        analysis_service=AnalysisService(),
        scheduler=AnalysisJobScheduler(),
        project_getter=lambda: project,
    )


def test_asset_status_pending_running_failed_reachable(qapp) -> None:
    """瞬态 pending/running/failed 从生命周期缓存可查（此前恒不可达）。"""
    ctrl = _make_analysis_controller()
    assert ctrl.get_asset_status("a1") == STATUS_UNKNOWN

    # propose（公共入口）→ pending
    ctrl.request_analysis_draft(["a1"])
    assert ctrl.get_asset_status("a1") == STATUS_PENDING

    # 执行中（生命周期信号）→ running
    ctrl._emit_status("a1", STATUS_RUNNING)
    assert ctrl.get_asset_status("a1") == STATUS_RUNNING

    # 最近一次失败 → failed
    ctrl._emit_status("a1", STATUS_FAILED)
    assert ctrl.get_asset_status("a1") == STATUS_FAILED


def test_asset_status_stale_on_fingerprint_mismatch(qapp, tmp_path) -> None:
    """service 报 ready 但素材指纹已变（mtime 变化）→ stale。"""
    from src.features.edit.ai_editing.analysis_models import (
        AssetAnalysis,
        AssetFingerprint,
    )
    from src.features.edit.ai_editing.analysis_scheduler import AnalysisJobScheduler
    from src.features.edit.ai_editing.analysis_service import AnalysisService
    from src.features.edit.ai_editing.editorial_models import (
        FrameRange,
        SegmentKind,
        SemanticSegment,
    )

    media = tmp_path / "clip.mov"
    media.write_bytes(b"x" * 16)
    stat = media.stat()
    fingerprint = AssetFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns)

    service = AnalysisService()
    segment = SemanticSegment(
        segment_id="s1", asset_id="a1",
        source_range=FrameRange(start=0, duration=10),
        kind=SegmentKind.SHOT)
    service.ingest(
        "a1",
        AssetAnalysis(asset_id="a1", fingerprint=fingerprint, fps="24/1"),
        (segment,))

    project = SimpleNamespace(
        project_id="p1", revision=1,
        assets={"a1": SimpleNamespace(path=str(media))})
    ctrl = MediaAnalysisController(
        analysis_service=service,
        scheduler=AnalysisJobScheduler(),
        project_getter=lambda: project)

    assert ctrl.get_asset_status("a1") == STATUS_READY

    # 素材被修改（mtime 推移）→ ready 降级 stale
    later = time.time() + 10
    os.utime(media, (later, later))
    assert ctrl.get_asset_status("a1") == STATUS_STALE


def test_asset_status_ready_without_fingerprint_source(qapp, tmp_path) -> None:
    """无法解析当前指纹（无 project_getter）：不误报 stale，保持 ready。"""
    from src.features.edit.ai_editing.analysis_models import (
        AssetAnalysis,
        AssetFingerprint,
    )
    from src.features.edit.ai_editing.analysis_scheduler import AnalysisJobScheduler
    from src.features.edit.ai_editing.analysis_service import AnalysisService
    from src.features.edit.ai_editing.editorial_models import (
        FrameRange,
        SegmentKind,
        SemanticSegment,
    )

    service = AnalysisService()
    segment = SemanticSegment(
        segment_id="s1", asset_id="a1",
        source_range=FrameRange(start=0, duration=10),
        kind=SegmentKind.SHOT)
    service.ingest(
        "a1",
        AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint(size=1, mtime_ns=1), fps="24/1"),
        (segment,))

    ctrl = MediaAnalysisController(
        analysis_service=service, scheduler=AnalysisJobScheduler())
    assert ctrl.get_asset_status("a1") == STATUS_READY


def test_project_change_clears_transient_status(qapp) -> None:
    """工程切换：瞬态状态缓存清空，查询回到 service 派生。"""
    ctrl = _make_analysis_controller()
    ctrl._emit_status("a1", STATUS_FAILED)
    assert ctrl.get_asset_status("a1") == STATUS_FAILED

    ctrl.on_project_changed("p2")
    assert ctrl.get_asset_status("a1") == STATUS_UNKNOWN


# ═══════════════════════════════════════════════════════════════════════
# AUD-3000: QLabel 导入
# ═══════════════════════════════════════════════════════════════════════


def test_menu_toolbar_module_imports_qlabel() -> None:
    """QLabel 必须已在模块导入（原实现在 GPU 信息标签处必然 NameError 被吞）。"""
    from PyQt6.QtWidgets import QLabel
    from src.app.controllers import menu_toolbar_controller as mtc

    assert mtc.QLabel is QLabel


def test_gpu_info_label_no_longer_name_errors(qapp) -> None:
    """开启 show_gpu_info 时 setup_statusbar 完整执行（QLabel 已导入）。"""
    host = _make_status_host_cls()(panel_opacity=100, show_gpu_info=True)
    host.setup_statusbar()
    # provider 不可用时走 debug 日志分支；关键断言：不再因 QLabel 缺失的
    # NameError 中断状态栏装配（Ready 消息必然出现）。
    assert host.status_bar.currentMessage() == "Ready"


# ═══════════════════════════════════════════════════════════════════════
# AUD-3001: panel_opacity 生效路径
# ═══════════════════════════════════════════════════════════════════════


def _make_opacity_host(qapp) -> tuple:
    """构造带两个真实 dock（一停靠一浮动）的 MenuToolbarMixin 宿主。"""
    from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin

    window = QMainWindow()
    docked = QDockWidget("A", window)
    window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, docked)
    floating = QDockWidget("B", window)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, floating)
    floating.setFloating(True)

    host = MenuToolbarMixin.__new__(MenuToolbarMixin)
    host.all_docks = [docked, floating]
    return host, window, docked, floating


def test_apply_panel_opacity_docked_uses_graphics_effect(qapp) -> None:
    """停靠 dock：windowOpacity 无效 → QGraphicsOpacityEffect 生效。"""
    from PyQt6.QtWidgets import QGraphicsOpacityEffect

    host, window, docked, floating = _make_opacity_host(qapp)
    host.apply_panel_opacity(0.85)

    effect = docked.graphicsEffect()
    assert isinstance(effect, QGraphicsOpacityEffect)
    assert abs(effect.opacity() - 0.85) < 1e-6

    # 浮动 dock 是顶层窗口：windowOpacity 生效（offscreen 平台按 8bit 量化）
    assert abs(floating.windowOpacity() - 0.85) < 1 / 255


def test_apply_panel_opacity_full_removes_effects(qapp) -> None:
    """opacity=1.0：移除效果，恢复零合成开销路径。"""
    host, window, docked, floating = _make_opacity_host(qapp)
    host.apply_panel_opacity(0.85)
    host.apply_panel_opacity(1.0)

    assert docked.graphicsEffect() is None
    assert abs(floating.windowOpacity() - 1.0) < 1 / 255


def test_appearance_preview_routes_panel_opacity(qapp) -> None:
    """偏好对话框的实时预览经 _on_appearance_preview → apply_panel_opacity。"""
    host, window, docked, _floating = _make_opacity_host(qapp)
    host._on_appearance_preview("panel_opacity", 85)
    assert docked.graphicsEffect() is not None
    assert abs(docked.graphicsEffect().opacity() - 0.85) < 1e-6


def test_panel_opacity_reapplied_on_float_toggle(qapp) -> None:
    """dock 浮动/停靠切换后重套正确机制（topLevelChanged 接线）。"""
    from PyQt6.QtWidgets import QGraphicsOpacityEffect

    host, window, docked, _floating = _make_opacity_host(qapp)
    host._panel_opacity = 1.0
    host._wire_panel_opacity_reapply()
    host.apply_panel_opacity(0.9)

    # 停靠态：效果
    assert isinstance(docked.graphicsEffect(), QGraphicsOpacityEffect)
    # 切到浮动：重套为 windowOpacity，效果移除
    docked.setFloating(True)
    assert abs(docked.windowOpacity() - 0.9) < 1 / 255
    assert docked.graphicsEffect() is None


def _make_status_host_cls():
    """setup_statusbar 的最小宿主类（QMainWindow + MenuToolbarMixin 语义）。"""
    from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin

    class _StatusHost(QMainWindow, MenuToolbarMixin):

        def __init__(self, *, panel_opacity: int, show_gpu_info: bool = False) -> None:
            super().__init__()
            self._cfg = {"panel_opacity": panel_opacity, "show_gpu_info": show_gpu_info}
            self.config_manager = SimpleNamespace(get=self._get_cfg)
            self.all_docks: list = []
            self._toggle_video_export_pause = lambda: None
            self._cancel_video_export = lambda: None

        def _get_cfg(self, key: str, default: Any = None) -> Any:
            return self._cfg.get(key, default)

    return _StatusHost


def test_setup_statusbar_applies_persisted_panel_opacity(qapp) -> None:
    """启动路径：setup_statusbar 读取持久化的 panel_opacity 并统一应用。"""
    from PyQt6.QtWidgets import QGraphicsOpacityEffect

    host = _make_status_host_cls()(panel_opacity=85)
    dock = QDockWidget("D", host)
    host.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    host.all_docks = [dock]

    host.setup_statusbar()

    effect = dock.graphicsEffect()
    assert isinstance(effect, QGraphicsOpacityEffect), (
        "启动时必须应用持久化的 panel_opacity（此前全库无启动应用路径）")
    assert abs(effect.opacity() - 0.85) < 1e-6
    assert isinstance(host.status_bar, QStatusBar)
