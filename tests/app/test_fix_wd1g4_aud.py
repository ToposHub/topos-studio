"""回归测试：REMEDIATION_PLAN 批 D（wd1r-g4）composite_controller 七条 Major。

覆盖 AUD-2401 / 2402 / 2403 / 2404 / 2405 / 2406 / 2407 的修复行为
（证据：docs/audit/2026-09-28/batches/batch-024-app-composite-controller.md）：

* AUD-2401 视频/序列渲染回调经 Qt 信号桥封送回 UI 线程；去掉逐帧与
  派发前 ``processEvents()`` 重入窗口；同步图片渲染加忙碌态屏蔽
  重复触发。
* AUD-2402 渲染进行中拒绝再次启动（is_exporting 守卫）；回调闭包比对
  本次 exporter 实例，旧任务回调不得拆掉新任务的取消过滤器/引用。
* AUD-2403 exporter 构造/派发同步抛异常时走与 on_error 相同的 UI
  状态复位（进度条、取消过滤器、光标、状态栏）。
* AUD-2404 宽泛 except 吞错四处收敛：缓存失效失败、切页序列化失败、
  comp clip 解析失败、ICC 嵌入失败全部 warning 级留痕（切页序列化
  失败另经状态栏告知用户）；teardown 收窄为 RuntimeError。
* AUD-2405 ``_last_metadata`` 属性存在但为 None 时渲染输出不再
  AttributeError（None 归一后再读 aov）。
* AUD-2406 工程文件图数据数值字段经 ``_safe_int`` 校验（字符串/垃圾
  值回落默认值，负值钳制），页面切换与渲染输出路径不再裸 int() 崩溃。
* AUD-2407 单帧导出线性保持由格式能力表 ``_LINEAR_IMAGE_FORMATS``
  驱动，而非单点 ``ext == "exr"`` 比较。
"""
from __future__ import annotations

import logging
import os
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication
from unittest.mock import MagicMock

from src.app.controllers import composite_controller as cc
from src.app.controllers.composite_controller import (
    _LINEAR_IMAGE_FORMATS,
    _make_render_callback_bridge,
    CompositeControllerMixin,
)

# 模态 QMessageBox 替身：warning/critical 不允许在 offscreen 测试里真的 exec。
_MOCK_QMESSAGEBOX = MagicMock(name="QMessageBox")


class _BareHost(CompositeControllerMixin):
    """只带 mixin 方法的裸宿主：供 unbound 调用需要 self._safe_int 等场景。"""


# ═══════════════════════════════════════════════════════════════════════
# Fakes（纯 Python 记录型控件；无 GL / 无引擎线程 / 无模态框）
# ═══════════════════════════════════════════════════════════════════════


class _StatusBar:
    def __init__(self) -> None:
        self.messages: List[Tuple[str, int]] = []

    def showMessage(self, msg: str, ms: int = 0) -> None:
        self.messages.append((msg, ms))

    def currentMessage(self) -> str:
        return self.messages[-1][0] if self.messages else ""


class _ProgressBar:
    def __init__(self) -> None:
        self.shown = False
        self.range: Optional[Tuple[int, int]] = None
        self.value: Optional[int] = None
        self.filters: List[Any] = []
        self.tooltip: Optional[str] = None
        self.cursor: Any = None

    def setRange(self, a: int, b: int) -> None:
        self.range = (a, b)

    def setValue(self, v: int) -> None:
        self.value = v

    def show(self) -> None:
        self.shown = True

    def hide(self) -> None:
        self.shown = False

    def installEventFilter(self, f: Any) -> None:
        self.filters.append(f)

    def removeEventFilter(self, f: Any) -> None:
        if f in self.filters:
            self.filters.remove(f)

    def setToolTip(self, t: str) -> None:
        self.tooltip = t

    def setCursor(self, c: Any) -> None:
        self.cursor = c

    def unsetCursor(self) -> None:
        self.cursor = None


class _ConfigManager:
    def __init__(self) -> None:
        self.values: Dict[str, Any] = {}
        self.saved = 0

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value

    def save_config(self) -> None:
        self.saved += 1


class _FakeExporter:
    """CompositeRenderExporter 替身：记录回调、可注入失败。"""

    created: List["_FakeExporter"] = []

    def __init__(self, parent: Any = None, export_exc: Optional[Exception] = None,
                 init_exc: Optional[Exception] = None) -> None:
        if init_exc is not None:
            raise init_exc
        self.parent = parent
        self.export_exc = export_exc
        self.is_exporting = False
        self.export_calls: List[Dict[str, Any]] = []
        self.cleaned = False
        self.cancelled = False
        _FakeExporter.created.append(self)

    def export(self, settings: Dict[str, Any], context: Dict[str, Any],
               on_finished: Any = None, on_error: Any = None,
               on_progress: Any = None) -> None:
        self.export_calls.append({
            "settings": settings, "context": context,
            "on_finished": on_finished, "on_error": on_error,
            "on_progress": on_progress,
        })
        self.is_exporting = True
        if self.export_exc is not None:
            raise self.export_exc

    def cancel(self) -> None:
        self.cancelled = True

    def cleanup(self) -> None:
        self.cleaned = True
        self.is_exporting = False


class _VideoHost(CompositeControllerMixin):
    """视频/序列渲染路径的最小宿主（MainWindow 形状）。"""

    def __init__(self) -> None:
        self.status_bar = _StatusBar()
        self.progress_bar = _ProgressBar()
        self.config_manager = _ConfigManager()
        self._current_edit_project = None
        self._current_timeline_sequences = {}
        self._current_project_id = "p1"
        self.engine = SimpleNamespace()
        self._open_folder_calls: List[str] = []

    def _import_qmessagebox(self) -> Any:
        return _MOCK_QMESSAGEBOX

    def _open_export_folder(self, path: str) -> None:
        self._open_folder_calls.append(path)


class _ImageHost(CompositeControllerMixin):
    """同步图片渲染路径的最小宿主。"""

    def __init__(self, embed_fails: bool = False) -> None:
        self.status_bar = _StatusBar()
        self.progress_bar = _ProgressBar()
        self.config_manager = _ConfigManager()
        self._open_folder_calls: List[str] = []
        self.failures: List[Tuple[str, str]] = []
        self.get_image_calls = 0
        self._render_calls: List[Dict[str, Any]] = []
        self._embed_fails = embed_fails
        self.engine = SimpleNamespace(get_current_image=self._get_image)
        self.color_manager = SimpleNamespace(
            convert_for_export=lambda img, source_space=None: img,
            embed_icc=self._embed_icc,
        )
        self._source_color_space = None

    def _get_image(self, force_full_res: bool = False) -> np.ndarray:
        self.get_image_calls += 1
        return np.zeros((2, 2, 3), dtype=np.float32)

    def _embed_icc(self, path: str) -> None:
        if self._embed_fails:
            raise RuntimeError("icc boom")

    def _render_composite_full_res(self, color_img: Any, keep_linear: bool = False,
                                   render_pass: str = "beauty") -> np.ndarray:
        self._render_calls.append({"keep_linear": keep_linear,
                                   "render_pass": render_pass})
        return np.zeros((2, 2, 3), dtype=np.float32)

    def _open_export_folder(self, path: str) -> None:
        self._open_folder_calls.append(path)

    def _import_qmessagebox(self) -> Any:
        return _MOCK_QMESSAGEBOX

    def _fail_image_render(self, path: str, detail: str) -> None:
        self.failures.append((path, detail))


_VIDEO_SETTINGS: Dict[str, Any] = {
    "start_frame": 0, "end_frame": 10, "kind": "video_file",
    "output_path": "/tmp/out.mp4",
}


@pytest.fixture
def video_host(qapp, monkeypatch: pytest.MonkeyPatch) -> _VideoHost:
    """装配视频渲染宿主：假 exporter + 假色彩描述符。"""
    import src.features.composite.render.composite_render_worker as crw
    import src.shared.color.color_space_descriptor as csd

    _FakeExporter.created = []
    monkeypatch.setattr(crw, "CompositeRenderExporter", _FakeExporter)
    monkeypatch.setattr(csd, "descriptor_from_config",
                        lambda *a, **k: None)
    return _VideoHost()


def _patch_file_io_save(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.shared.io.file_io as file_io_mod
    monkeypatch.setattr(
        file_io_mod.FileIO, "save_image",
        staticmethod(lambda path, image, options=None: True))


# ═══════════════════════════════════════════════════════════════════════
# AUD-2401：回调经 Qt 信号桥封送 + 去重入窗口 + 图片渲染忙碌态
# ═══════════════════════════════════════════════════════════════════════


def test_aud2401_exporter_receives_signal_bridge_emitters(
        video_host: _VideoHost) -> None:
    """export() 收到的必须是信号 emit（桥接），而非直连 UI 控件的闭包。"""
    video_host._run_composite_video_render(
        dict(_VIDEO_SETTINGS), {"nodes": []}, None)

    exporter = video_host._composite_render_exporter
    assert isinstance(exporter, _FakeExporter) and exporter.export_calls
    for key in ("on_finished", "on_error", "on_progress"):
        cb = exporter.export_calls[0][key]
        assert getattr(cb, "__name__", "") == "emit", (
            f"{key} 必须经 Qt 信号桥（bound signal emit）封送回 UI 线程"
            "（AUD-2401），不得把裸 UI 闭包交给 exporter")
    assert video_host._render_callback_bridge is not None


def test_aud2401_bridge_marshals_cross_thread_emit_to_ui_thread(
        qapp: QApplication) -> None:
    """工作线程 emit → 队列投递 → 槽在 UI 线程执行。"""
    bridge = _make_render_callback_bridge()
    seen: List[Any] = []

    def slot(current: int, total: int) -> None:
        seen.append((current, total, QThread.currentThreadId()))

    bridge.progress.connect(slot)
    main_tid = int(QThread.currentThreadId())

    worker = threading.Thread(target=lambda: bridge.progress.emit(3, 10))
    worker.start()
    worker.join()

    assert seen == [], "跨线程 emit 不得在工作线程同步执行 UI 槽（AUD-2401）"
    qapp.processEvents()
    assert len(seen) == 1
    assert (seen[0][0], seen[0][1]) == (3, 10)
    assert int(seen[0][2]) == main_tid, "槽必须经队列连接在 UI 线程执行（AUD-2401）"

    bridge.deleteLater()
    qapp.processEvents()


def test_aud2401_video_render_never_calls_process_events(
        video_host: _VideoHost,
        monkeypatch: pytest.MonkeyPatch) -> None:
    counter = {"n": 0}
    monkeypatch.setattr(
        QApplication, "processEvents",
        staticmethod(lambda *a, **k: counter.__setitem__("n", counter["n"] + 1)))

    video_host._run_composite_video_render(
        dict(_VIDEO_SETTINGS), {"nodes": []}, None)
    exporter = video_host._composite_render_exporter
    assert exporter is not None and exporter.export_calls
    # 模拟 exporter 回调（桥 emit → UI 槽）逐帧推进
    exporter.export_calls[0]["on_progress"](5, 11)
    exporter.export_calls[0]["on_finished"]("/tmp/out.mp4")

    assert counter["n"] == 0, (
        "视频/序列渲染路径不得再调用 processEvents()（逐帧/派发前重入窗口，"
        "AUD-2401）")
    assert video_host.progress_bar.shown is False  # finished 已隐藏


def test_aud2401_image_render_busy_guard_blocks_reentrant_trigger(
        qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_file_io_save(monkeypatch)
    host = _ImageHost()
    outer_settings = {"output_path": "/tmp/a.png", "image_format": "png"}

    # processEvents 重入窗口内用户再次触发同步渲染
    def _fake_process_events(*a: Any, **k: Any) -> None:
        CompositeControllerMixin._run_composite_image_render(
            host, {"output_path": "/tmp/again.png", "image_format": "png"})

    monkeypatch.setattr(QApplication, "processEvents",
                        staticmethod(_fake_process_events))

    CompositeControllerMixin._run_composite_image_render(host, outer_settings)

    assert host.get_image_calls == 1, (
        "processEvents 重入窗口内的再次触发必须被忙碌态拒绝（AUD-2401）")
    assert len(host._render_calls) == 1
    assert host._open_folder_calls == ["/tmp/a.png"]
    assert getattr(host, "_composite_image_rendering", None) is False, (
        "渲染结束后忙碌态必须复位")


# ═══════════════════════════════════════════════════════════════════════
# AUD-2402：重复启动守卫 + 回调实例比对
# ═══════════════════════════════════════════════════════════════════════


def test_aud2402_busy_exporter_rejects_second_render(
        video_host: _VideoHost) -> None:
    busy = _FakeExporter()
    busy.is_exporting = True
    video_host._composite_render_exporter = busy

    video_host._run_composite_video_render(
        dict(_VIDEO_SETTINGS), {"nodes": []}, None)

    # 未创建新 exporter，未覆盖引用，未装配任何 UI 状态
    assert video_host._composite_render_exporter is busy
    assert not busy.export_calls
    assert len(_FakeExporter.created) == 1  # 只有 fixture 里的 busy
    assert video_host.progress_bar.shown is False
    assert video_host.progress_bar.filters == []
    _MOCK_QMESSAGEBOX.warning.assert_called_once()


def test_aud2402_finish_cleans_own_exporter_and_ui(
        video_host: _VideoHost) -> None:
    video_host._run_composite_video_render(
        dict(_VIDEO_SETTINGS), {"nodes": []}, None)
    exporter = video_host._composite_render_exporter
    assert isinstance(exporter, _FakeExporter)

    exporter.export_calls[0]["on_finished"]("/tmp/out.mp4")

    assert video_host.progress_bar.shown is False
    assert exporter.cleaned is True
    assert video_host._composite_render_exporter is None
    assert video_host.progress_bar.filters == [], "取消过滤器必须摘除"
    assert getattr(video_host, "_render_cancel_filter", None) is None
    assert getattr(video_host, "_render_callback_bridge", None) is None
    assert video_host._open_folder_calls == ["/tmp/out.mp4"]
    assert video_host.config_manager.values["last_path"] == "/tmp"


def test_aud2402_stale_callbacks_cannot_touch_newer_render(
        video_host: _VideoHost) -> None:
    """旧 exporter 的回调在新渲染接管后不得隐藏进度条/清引用/清过滤器。"""
    video_host._run_composite_video_render(
        dict(_VIDEO_SETTINGS), {"nodes": []}, None)
    old = video_host._composite_render_exporter
    assert isinstance(old, _FakeExporter) and old.export_calls

    newer = _FakeExporter()
    newer.is_exporting = True
    video_host._composite_render_exporter = newer
    video_host.progress_bar.show()

    _MOCK_QMESSAGEBOX.reset_mock()
    old.export_calls[0]["on_finished"]("/tmp/old.mp4")
    old.export_calls[0]["on_error"]("boom")

    assert video_host.progress_bar.shown is True, "旧回调不得隐藏新渲染的进度条"
    assert video_host._composite_render_exporter is newer, "旧回调不得清掉新渲染引用"
    assert newer.cleaned is False and newer.cancelled is False
    assert video_host.progress_bar.filters, "旧回调不得摘掉新渲染的取消过滤器"
    assert video_host._open_folder_calls == []
    _MOCK_QMESSAGEBOX.critical.assert_not_called()


def test_aud2402_stale_idle_exporter_cleaned_before_new_render(
        video_host: _VideoHost) -> None:
    stale = _FakeExporter()  # is_exporting False：残留的已完成 exporter
    stale_filter = object()
    video_host._composite_render_exporter = stale
    # 模拟上一轮渲染的真实残留：过滤器既挂在进度条上也留在属性上
    video_host._render_cancel_filter = stale_filter
    video_host.progress_bar.filters.append(stale_filter)

    video_host._run_composite_video_render(
        dict(_VIDEO_SETTINGS), {"nodes": []}, None)

    assert stale.cleaned is True, "残留 exporter 必须先清理再装配新渲染"
    assert stale_filter not in video_host.progress_bar.filters
    new = video_host._composite_render_exporter
    assert new is not None and new is not stale
    assert video_host.progress_bar.filters == [
        video_host._render_cancel_filter]


# ═══════════════════════════════════════════════════════════════════════
# AUD-2403：export() 同步抛异常 → 与 on_error 相同的 UI 复位
# ═══════════════════════════════════════════════════════════════════════


def _dispatch_failure_video_host(
        video_host: _VideoHost, monkeypatch: pytest.MonkeyPatch,
        export_exc: Optional[Exception], init_exc: Optional[Exception],
) -> None:
    import src.features.composite.render.composite_render_worker as crw

    class _BrokenExporter(_FakeExporter):
        def __init__(self, parent: Any = None) -> None:
            super().__init__(parent, export_exc=export_exc,
                             init_exc=init_exc)

    _FakeExporter.created = []
    monkeypatch.setattr(crw, "CompositeRenderExporter", _BrokenExporter)
    _MOCK_QMESSAGEBOX.reset_mock()
    video_host._run_composite_video_render(
        dict(_VIDEO_SETTINGS), {"nodes": []}, None)


def test_aud2403_export_sync_exception_resets_ui(
        video_host: _VideoHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _dispatch_failure_video_host(
        video_host, monkeypatch,
        export_exc=ValueError("bad settings"), init_exc=None)

    assert video_host.progress_bar.shown is False, "进度条不得残留（AUD-2403）"
    assert video_host.progress_bar.filters == [], "取消过滤器不得残留"
    assert getattr(video_host, "_render_cancel_filter", None) is None
    assert video_host.progress_bar.tooltip == ""
    assert video_host.progress_bar.cursor is None, "手形光标不得残留"
    assert video_host._composite_render_exporter is None
    assert _MOCK_QMESSAGEBOX.critical.called
    assert any("渲染失败" in msg for msg, _ in video_host.status_bar.messages)
    broken = _FakeExporter.created[0]
    assert broken.cleaned is True


def test_aud2403_exporter_constructor_exception_resets_ui(
        video_host: _VideoHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _dispatch_failure_video_host(
        video_host, monkeypatch,
        export_exc=None, init_exc=RuntimeError("no GL context"))

    assert video_host.progress_bar.shown is False
    assert video_host.progress_bar.filters == []
    assert getattr(video_host, "_render_cancel_filter", None) is None
    assert video_host.progress_bar.tooltip == ""
    assert getattr(video_host, "_composite_render_exporter", None) is None
    assert _MOCK_QMESSAGEBOX.critical.called


# ═══════════════════════════════════════════════════════════════════════
# AUD-2404：宽泛 except 收敛（不吞错、可见留痕）
# ═══════════════════════════════════════════════════════════════════════


def test_aud2404_cache_invalidation_failure_is_logged(
        caplog: pytest.LogCaptureFixture) -> None:
    broken_cache = SimpleNamespace(
        invalidate_comp_clip_cache=lambda cid: (_ for _ in ()).throw(
            RuntimeError("cache boom")))
    host = _BareHost()
    host._current_edit_project = None  # type: ignore[attr-defined]
    host.timeline_preview_renderer = broken_cache  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING, logger=cc.__name__):
        CompositeControllerMixin._on_composite_graph_changed(host, "c1", {})

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and r.exc_info]
    assert warnings, "缓存失效失败必须带 exc_info 记 warning（不再裸 pass）"


def test_aud2404_leave_flush_serialize_failure_notifies_user(
        caplog: pytest.LogCaptureFixture) -> None:
    def _boom() -> Dict[str, Any]:
        raise RuntimeError("serialize boom")

    page = SimpleNamespace(_current_composite_id="c1",
                           get_composite_data=_boom)
    status = _StatusBar()
    host = SimpleNamespace(
        composite_page=page,
        _current_composite_id="",
        _current_edit_project=None,
        status_bar=status,
    )

    with caplog.at_level(logging.WARNING, logger=cc.__name__):
        CompositeControllerMixin._flush_composite_to_edit_before_leave(host)

    assert any(r.levelno == logging.WARNING for r in caplog.records), (
        "切页序列化失败（用户编辑面临丢失）必须 warning 级留痕")
    assert status.messages, "序列化失败必须经状态栏告知用户，不得静默 return"


def test_aud2404_remember_active_comp_clip_narrowed_exceptions(
        caplog: pytest.LogCaptureFixture) -> None:
    def _raise(cid: str) -> Any:
        raise ValueError("corrupt comp clip dict")

    project = SimpleNamespace(
        get_comp_clip_by_graph_id=_raise,
        comp_clips={},
    )
    host = SimpleNamespace(_current_edit_project=project)

    with caplog.at_level(logging.WARNING, logger=cc.__name__):
        CompositeControllerMixin._remember_active_comp_clip(host, "c1")

    assert any(r.levelno == logging.WARNING for r in caplog.records), (
        "comp clip 解析失败必须 warning 级留痕（不再 debug 静默）")


def test_aud2404_icc_embed_failure_warns_but_save_succeeds(
        qapp: QApplication, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    _patch_file_io_save(monkeypatch)
    host = _ImageHost(embed_fails=True)

    with caplog.at_level(logging.WARNING, logger=cc.__name__):
        CompositeControllerMixin._run_composite_image_render(
            host, {"output_path": "/tmp/out.png", "image_format": "png"})

    assert any(r.levelno == logging.WARNING and r.exc_info
               for r in caplog.records), (
        "ICC 嵌入失败必须带堆栈记 warning（不再裸 pass，AUD-2404）")
    assert host._open_folder_calls == ["/tmp/out.png"], (
        "嵌入失败不得掩盖已成功的图片保存")
    assert host.failures == []


# ═══════════════════════════════════════════════════════════════════════
# AUD-2405：_last_metadata=None 不再 AttributeError
# ═══════════════════════════════════════════════════════════════════════


def test_aud2405_none_last_metadata_does_not_crash() -> None:
    host = SimpleNamespace(composite_page=SimpleNamespace(_last_metadata=None))
    passes = CompositeControllerMixin._collect_available_render_passes(
        host, {"nodes": []})
    assert passes == {"beauty"}


def test_aud2405_missing_last_metadata_attribute() -> None:
    page = SimpleNamespace()  # 属性整体缺失
    host = SimpleNamespace(composite_page=page)
    assert CompositeControllerMixin._collect_available_render_passes(
        host, {"nodes": []}) == {"beauty"}


def test_aud2405_aov_metadata_unions_scene3d_extras() -> None:
    host = SimpleNamespace(composite_page=SimpleNamespace(
        _last_metadata={"aov": {"cryptomatte": 1, "rgba": 2}}))
    graph = {"nodes": [{"type": "scene3d"}]}
    passes = CompositeControllerMixin._collect_available_render_passes(
        host, graph)
    assert passes == {"beauty", "cryptomatte", "rgba", "depth", "object_id"}


# ═══════════════════════════════════════════════════════════════════════
# AUD-2406：工程文件数值字段 _safe_int 校验
# ═══════════════════════════════════════════════════════════════════════


def test_aud2406_safe_int_units() -> None:
    safe = CompositeControllerMixin._safe_int
    assert safe("3840px", 1920, minimum=1) == 1920, "垃圾字符串回落默认值"
    assert safe(None, 1920, minimum=1) == 1920
    assert safe(0, 1920, minimum=1) == 1920, "0 维持旧 `or` 回退语义"
    assert safe("", 1920, minimum=1) == 1920
    assert safe("3840", 1920, minimum=1) == 3840, "合法数字字符串可用"
    assert safe(3840.9, 1920, minimum=1) == 3840
    assert safe(-5, 1920, minimum=1) == 1, "非法负数钳制到 minimum"
    assert safe("120", 0) == 120
    assert safe("abc", 0) == 0


def test_aud2406_resolve_output_resolution_survives_corrupt_data() -> None:
    host = _BareHost()
    resolve = CompositeControllerMixin._resolve_output_resolution

    corrupt = {"nodes": [{
        "type": "output",
        "parameters": {"format_mode": "custom",
                       "width": "3840px", "height": None},
    }]}
    assert resolve(host, corrupt) == (1920, 1080), (
        "损坏的 Output 节点宽高必须回落默认值，不得 ValueError 逃逸（AUD-2406）")

    str_ok = {"nodes": [{
        "type": "output",
        "parameters": {"format_mode": "project",
                       "width": "3840", "height": 2160},
    }]}
    assert resolve(host, str_ok) == (3840, 2160)

    negative = {"nodes": [{
        "type": "output",
        "parameters": {"format_mode": "custom",
                       "width": -8, "height": 1080},
    }]}
    assert resolve(host, negative) == (1, 1080), "负宽高钳制为正"


def _setup_clip_context_host(
    graph: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Tuple[List[Dict[str, Any]], SimpleNamespace]:
    """为 _setup_timeline_clip_context 装配假 facade + 记录型宿主。"""
    import src.features.composite.facade as comp_facade
    import src.features.edit.facade as edit_facade

    runtime_calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        comp_facade, "make_composite_runtime_context",
        lambda **kw: runtime_calls.append(kw) or SimpleNamespace(**kw))

    class _Provider:
        def set_engine(self, engine: Any) -> None: pass
        def set_allow_external_gpu_frames(self, flag: bool) -> None: pass
        def set_frame_ready_callback(self, cb: Any) -> None: pass
        def set_project(self, project: Any) -> None: pass
        def set_timeline_sequences(self, seqs: Any) -> None: pass

    monkeypatch.setattr(edit_facade, "create_timeline_clip_frame_provider",
                        lambda: _Provider())

    page = SimpleNamespace(
        set_timeline_clip_context=lambda runtime_context, gpu_provider: None,
        _video_node_frame_ready=None,
    )
    host = _BareHost()
    host.composite_page = page
    host._current_edit_project = SimpleNamespace(
        get_composition_by_id=lambda cid: graph)
    host._current_timeline_sequences = {}
    host._current_project_id = "p1"
    host.edit_page = None
    host.engine = SimpleNamespace()
    host._timeline_clip_frame_provider = None
    context = SimpleNamespace(timeline_id="t1", timeline_frame=5,
                              source_frame=0, clip_local_frame=0,
                              asset_id="a1")
    return runtime_calls, host, context  # type: ignore[return-value]


def test_aud2406_setup_context_survives_corrupt_graph_dimensions(
        qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_calls, host, context = _setup_clip_context_host(
        {"width": "3840px", "height": None, "duration_frames": "abc"},
        monkeypatch)

    CompositeControllerMixin._setup_timeline_clip_context(
        host, context, "graph-1", "clip-1")

    kw = runtime_calls[0]
    assert kw["width"] == 1920 and kw["height"] == 1080, (
        "损坏的图数据宽高必须回落默认值，页面切换不得崩溃（AUD-2406）")
    assert kw["duration_frames"] == 0


def test_aud2406_setup_context_accepts_numeric_strings(
        qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_calls, host, context = _setup_clip_context_host(
        {"width": "3840", "height": "2160", "duration_frames": "120"},
        monkeypatch)

    CompositeControllerMixin._setup_timeline_clip_context(
        host, context, "graph-1", "clip-1")

    kw = runtime_calls[0]
    assert kw["width"] == 3840 and kw["height"] == 2160
    assert kw["duration_frames"] == 120


# ═══════════════════════════════════════════════════════════════════════
# AUD-2407：线性保持由格式能力表驱动
# ═══════════════════════════════════════════════════════════════════════


def test_aud2407_linear_format_capability_table() -> None:
    assert "exr" in _LINEAR_IMAGE_FORMATS
    # 渲染对话框当前全部显示格式（composite_render_dialog._IMAGE_FORMATS）
    # 都不得按线性处理（tiff 在本控制器与 FileIO 均为 uint16 显示编码）。
    assert {"png", "jpeg", "tiff", "webp"}.isdisjoint(_LINEAR_IMAGE_FORMATS)


@pytest.mark.parametrize("fmt,expected", [("exr", True), ("png", False),
                                          ("tiff", False), ("webp", False)])
def test_aud2407_keep_linear_driven_by_table(
        qapp: QApplication, monkeypatch: pytest.MonkeyPatch,
        fmt: str, expected: bool) -> None:
    _patch_file_io_save(monkeypatch)
    host = _ImageHost()

    CompositeControllerMixin._run_composite_image_render(
        host, {"output_path": f"/tmp/out.{fmt}", "image_format": fmt})

    assert host._render_calls[0]["keep_linear"] is expected, (
        f"{fmt} 的线性保持必须由 _LINEAR_IMAGE_FORMATS 决定（AUD-2407）")
