"""GAP-027 补测：main_window.py 高风险路径（批 D 主窗侧最后缺口）。

针对 GAP_REPORT Top-30 #27（src/app/main_window.py，19% 覆盖、缺失约 942
行）补测以下高风险路径（与 test_fix_wd2g9_aud.py 已覆盖的 closeEvent 停机
序列互补，不重复）：

* PageContext / PageContextManager（页面切换上下文序列化与 dirty 追踪）
* 窗口标题工作模式标签（_update_title_work_mode）
* 音频后端启动诊断（AM-01：降级告警 / 健康 / 探测失败）
* eventFilter 键盘路由（Space 走带优先链、文本框抑制、Composite 逐帧键）
* Space 首拒链各 handler 的守卫分支（页面不符/无视频/图片模式/is_loaded 异常）
* on_engine_error（进度条复位 + 错误上报）
* _script_timeline_context（脚本 API 只读时间线上下文）

不可测路径（真机 GL / 完整 create_panels 面板树）以 test_mode=True 的
MainWindow 或逻辑宿主替身绕开，只测非渲染逻辑。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.app import main_window as mw_mod  # noqa: E402
from src.app.main_window import (  # noqa: E402
    MainWindow,
    PageContext,
    PageContextManager,
    PageType,
)

pytestmark = pytest.mark.app


@pytest.fixture(scope="module")
def main_window(qapp):
    """模块级复用的轻量 MainWindow（test_mode 不建面板/不碰 GL）。

    teardown 走真实 close（含引擎线程停机）再 deleteLater——只 deleteLater
    不停线程时，C++ 对象析构会与存活的 AsyncRenderer 线程相撞（Qt abort）。
    """
    window = MainWindow(test_mode=True)
    yield window
    from PyQt6.QtTest import QTest

    try:
        window.close()
    except Exception:
        pass
    try:
        window.deleteLater()
    except Exception:
        pass
    QTest.qWait(50)


def _clean_instance_stubs(mw: MainWindow) -> None:
    """清除上一用例挂上的实例级替身，恢复类级方法解析。"""
    for key in (
        "video_controller", "composite_page", "composite_viewer",
        "timeline_panel", "source_viewer_widget", "program_viewer_widget",
        "on_video_play_toggled", "status_bar", "progress_bar",
        "_audio_backend_warning_label", "_audio_capabilities",
    ):
        mw.__dict__.pop(key, None)


def _key_event(key) -> Any:
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent

    return QKeyEvent(
        QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier,
    )


# ═══════════════════════════════════════════════════════════════════════
# PageContext / PageContextManager（P9 页面切换上下文）
# ═══════════════════════════════════════════════════════════════════════


def test_page_context_dict_round_trip() -> None:
    ctx = PageContext(
        source_page=PageType.EDIT,
        target_page=PageType.COLOR,
        clip_id="c1",
        asset_id="a1",
        timeline_frame=12,
        source_frame=34,
        clip_local_frame=5,
        color_stack_id="s1",
        composite_id="x1",
        timeline_id="t1",
        deliver_source="timeline",
    )

    restored = PageContext.from_dict(ctx.to_dict())

    assert restored == ctx


def test_page_context_from_dict_applies_defaults_for_missing_keys() -> None:
    restored = PageContext.from_dict({})

    assert restored.source_page is PageType.COLOR
    assert restored.target_page is PageType.COLOR
    assert restored.clip_id is None
    assert restored.timeline_frame is None
    assert restored.deliver_source == "color"


def test_context_manager_tracks_current_and_last_per_page() -> None:
    manager = PageContextManager()
    to_color = manager.create_context_for_edit_to_color(clip_id="c1")
    to_composite = manager.create_context_for_edit_to_composite(clip_id="c1")

    assert manager.get_current_context() is None

    manager.set_current_context(to_color)
    manager.set_current_context(to_composite)

    assert manager.get_current_context() is to_composite
    # 每页保留自己的最后上下文
    assert manager.get_last_context(PageType.COLOR) is to_color
    assert manager.get_last_context(PageType.COMPOSITE) is to_composite
    assert manager.get_last_context(PageType.DELIVER) is None


def test_context_manager_dirty_tracking() -> None:
    manager = PageContextManager()
    assert manager.any_page_dirty() is False

    manager.set_page_dirty(PageType.EDIT, True)
    assert manager.is_page_dirty(PageType.EDIT) is True
    assert manager.any_page_dirty() is True

    manager.set_page_dirty(PageType.EDIT, False)
    assert manager.any_page_dirty() is False
    assert manager.is_page_dirty(PageType.LIBRARY) is False


def test_create_context_for_edit_to_color_carries_clip_frames() -> None:
    manager = PageContextManager()
    ctx = manager.create_context_for_edit_to_color(
        clip_id="clip-9", asset_id="asset-2", timeline_id="t-1",
        timeline_frame=100, source_frame=240, clip_local_frame=40,
        color_stack_id="stack-7",
    )

    assert ctx.source_page is PageType.EDIT
    assert ctx.target_page is PageType.COLOR
    assert (ctx.clip_id, ctx.asset_id, ctx.timeline_id) == ("clip-9", "asset-2", "t-1")
    assert (ctx.timeline_frame, ctx.source_frame, ctx.clip_local_frame) == (100, 240, 40)
    assert ctx.color_stack_id == "stack-7"


def test_create_context_for_edit_to_composite_marks_composite_source() -> None:
    ctx = PageContextManager().create_context_for_edit_to_composite(
        clip_id="c1", composite_id="comp-1", create_new=False)

    assert ctx.target_page is PageType.COMPOSITE
    assert ctx.composite_id == "comp-1"
    assert ctx.deliver_source == "composite"  # 合成片段导出来源预置


def test_create_context_for_to_deliver_routes_source_page() -> None:
    manager = PageContextManager()
    from_color = manager.create_context_for_to_deliver("color")
    from_timeline = manager.create_context_for_to_deliver("timeline", timeline_id="t3")

    assert from_color.source_page is PageType.COLOR
    assert from_color.deliver_source == "color"
    assert from_timeline.source_page is PageType.EDIT
    assert from_timeline.timeline_id == "t3"
    assert from_timeline.deliver_source == "timeline"
    assert from_color.target_page is from_timeline.target_page is PageType.DELIVER


# ═══════════════════════════════════════════════════════════════════════
# 窗口标题工作模式标签（窗口状态可见性）
# ═══════════════════════════════════════════════════════════════════════


def test_title_work_mode_tag_appended_for_studio(main_window, monkeypatch) -> None:
    import src.shared.feature_flags as flags

    monkeypatch.setattr(flags, "get_product_mode", lambda: "studio")
    monkeypatch.setattr(flags, "get_work_mode", lambda _cfg: "image")
    mw = main_window
    mw.setWindowTitle("Topos")

    mw._update_title_work_mode()

    assert mw.windowTitle() == "Topos — Image Mode"


def test_title_work_mode_tag_replaced_on_mode_switch(main_window, monkeypatch) -> None:
    import src.shared.feature_flags as flags

    monkeypatch.setattr(flags, "get_product_mode", lambda: "studio")
    monkeypatch.setattr(flags, "get_work_mode", lambda _cfg: "image")
    mw = main_window
    mw.setWindowTitle("Topos")
    mw._update_title_work_mode()

    monkeypatch.setattr(flags, "get_work_mode", lambda _cfg: "video")
    mw._update_title_work_mode()

    title = mw.windowTitle()
    assert title == "Topos — Video Mode"
    assert "Image Mode" not in title  # 旧标签被剥离，不叠标签


def test_title_work_mode_untouched_when_already_tagged(
        main_window, monkeypatch) -> None:
    import src.shared.feature_flags as flags

    monkeypatch.setattr(flags, "get_product_mode", lambda: "studio")
    monkeypatch.setattr(flags, "get_work_mode", lambda _cfg: "image")
    mw = main_window
    mw.setWindowTitle("My Project — Image Mode")

    mw._update_title_work_mode()

    assert mw.windowTitle() == "My Project — Image Mode"


def test_title_work_mode_skipped_for_non_studio_products(
        main_window, monkeypatch) -> None:
    import src.shared.feature_flags as flags

    monkeypatch.setattr(flags, "get_product_mode", lambda: "color")
    monkeypatch.setattr(flags, "get_work_mode", lambda _cfg: "image")
    mw = main_window
    mw.setWindowTitle("Topos Color")

    mw._update_title_work_mode()

    assert mw.windowTitle() == "Topos Color"  # 非 studio 产品不贴模式标签


def test_title_work_mode_tolerates_flag_errors(main_window, monkeypatch) -> None:
    import src.shared.feature_flags as flags

    def _boom() -> str:
        raise RuntimeError("flag store unavailable")

    monkeypatch.setattr(flags, "get_product_mode", _boom)
    mw = main_window
    mw.setWindowTitle("Topos")

    mw._update_title_work_mode()  # 不得抛出

    assert mw.windowTitle() == "Topos"


# ═══════════════════════════════════════════════════════════════════════
# 音频后端启动诊断（AM-01）
# ═══════════════════════════════════════════════════════════════════════


def _patch_audio_caps(monkeypatch, caps: Any = None, error: Exception | None = None):
    import src.features.edit.core.audio_capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "reset_audio_capabilities_cache", Mock())
    if error is not None:
        monkeypatch.setattr(
            caps_mod, "get_audio_capabilities",
            Mock(side_effect=error),
        )
    else:
        monkeypatch.setattr(
            caps_mod, "get_audio_capabilities",
            Mock(return_value=caps),
        )
    return caps_mod


def _wait_for_audio_snapshot(mw: MainWindow, timeout_ms: int = 5000) -> None:
    """AUD-3513 后探测在后台线程执行：等待结果经信号回到 UI 线程。"""
    from PyQt6.QtTest import QTest
    import time

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        QTest.qWait(20)
        if mw.__dict__.get("_audio_capabilities") is not None:
            return


def test_audio_diagnostics_degraded_shows_permanent_warning(
        main_window, monkeypatch) -> None:
    caps = SimpleNamespace(disabled_components=lambda: ["pyav", "scipy"])
    _patch_audio_caps(monkeypatch, caps=caps)
    mw = main_window
    _clean_instance_stubs(mw)
    status_bar = MagicMock(name="status_bar")
    mw.status_bar = status_bar

    try:
        mw._run_audio_backend_diagnostics()
        _wait_for_audio_snapshot(mw)

        assert mw._audio_capabilities is caps  # 快照供 EQ/LUFS UI 读取
        status_bar.addPermanentWidget.assert_called_once()
        label = status_bar.addPermanentWidget.call_args[0][0]
        assert mw._audio_backend_warning_label is label
    finally:
        label = mw.__dict__.get("_audio_backend_warning_label")
        if label is not None:
            label.deleteLater()


def test_audio_diagnostics_healthy_is_silent(main_window, monkeypatch) -> None:
    caps = SimpleNamespace(disabled_components=lambda: [])
    _patch_audio_caps(monkeypatch, caps=caps)
    mw = main_window
    _clean_instance_stubs(mw)
    status_bar = MagicMock(name="status_bar")
    mw.status_bar = status_bar

    try:
        mw._run_audio_backend_diagnostics()
        _wait_for_audio_snapshot(mw)

        status_bar.addPermanentWidget.assert_not_called()
        assert "_audio_backend_warning_label" not in mw.__dict__
    finally:
        mw.__dict__.pop("_audio_capabilities", None)


def test_audio_diagnostics_probe_failure_does_not_block_startup(
        main_window, monkeypatch) -> None:
    from PyQt6.QtTest import QTest

    _patch_audio_caps(monkeypatch, error=RuntimeError("device probe failed"))
    mw = main_window
    _clean_instance_stubs(mw)
    status_bar = MagicMock(name="status_bar")
    mw.status_bar = status_bar

    mw._run_audio_backend_diagnostics()  # 诊断失败只记日志，不阻断
    QTest.qWait(150)  # 给后台线程时间暴露任何同步误用

    status_bar.addPermanentWidget.assert_not_called()
    mw.__dict__.pop("status_bar", None)


# ═══════════════════════════════════════════════════════════════════════
# eventFilter 键盘路由（快捷键接线）
# ═══════════════════════════════════════════════════════════════════════


def _clear_focus(qapp) -> None:
    widget = qapp.focusWidget()
    if widget is not None:
        widget.clearFocus()


def test_space_on_color_page_toggles_video_playback(
        main_window, qapp, monkeypatch) -> None:
    from PyQt6.QtCore import Qt

    mw = main_window
    _clean_instance_stubs(mw)
    _clear_focus(qapp)
    mw.current_page = "Color"
    mw.video_controller = SimpleNamespace(is_video_loaded=lambda: True)
    toggle = Mock(name="on_video_play_toggled")
    mw.on_video_play_toggled = toggle

    handled = mw.eventFilter(mw, _key_event(Qt.Key.Key_Space))

    assert handled is True
    toggle.assert_called_once()


def test_space_does_not_toggle_when_no_video_loaded(
        main_window, qapp) -> None:
    from PyQt6.QtCore import Qt

    mw = main_window
    _clean_instance_stubs(mw)
    _clear_focus(qapp)
    mw.current_page = "Color"
    mw.video_controller = SimpleNamespace(is_video_loaded=lambda: False)
    toggle = Mock(name="on_video_play_toggled")
    mw.on_video_play_toggled = toggle

    assert mw.eventFilter(mw, _key_event(Qt.Key.Key_Space)) is False
    toggle.assert_not_called()


def test_space_suppressed_while_text_widget_has_focus(
        main_window, qapp) -> None:
    """文本控件聚焦时 Space 属于输入（输入框编辑不得被走带劫持）。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QLineEdit

    mw = main_window
    _clean_instance_stubs(mw)
    mw.current_page = "Color"
    mw.video_controller = SimpleNamespace(is_video_loaded=lambda: True)
    toggle = Mock(name="on_video_play_toggled")
    mw.on_video_play_toggled = toggle

    edit = QLineEdit()
    try:
        edit.show()
        qapp.setActiveWindow(edit)
        edit.setFocus()
        QTest.qWait(50)
        if qapp.focusWidget() is not edit:
            pytest.skip("offscreen 平台下无法建立焦点")

        assert mw.eventFilter(mw, _key_event(Qt.Key.Key_Space)) is False
        toggle.assert_not_called()
        edit.clearFocus()
    finally:
        edit.deleteLater()


def test_space_ignored_on_non_transport_pages(main_window, qapp) -> None:
    from PyQt6.QtCore import Qt

    mw = main_window
    _clean_instance_stubs(mw)
    _clear_focus(qapp)
    mw.current_page = "Library"
    mw.video_controller = SimpleNamespace(is_video_loaded=lambda: True)
    toggle = Mock(name="on_video_play_toggled")
    mw.on_video_play_toggled = toggle

    assert mw.eventFilter(mw, _key_event(Qt.Key.Key_Space)) is False
    toggle.assert_not_called()


def test_color_space_shortcut_survives_is_loaded_errors(main_window, qapp) -> None:
    """is_video_loaded 抛异常（控制器半 teardown）不得让按键崩溃。"""
    mw = main_window
    _clean_instance_stubs(mw)
    _clear_focus(qapp)
    mw.current_page = "Color"

    def _boom() -> bool:
        raise RuntimeError("controller torn down")

    mw.video_controller = SimpleNamespace(is_video_loaded=_boom)
    toggle = Mock(name="on_video_play_toggled")
    mw.on_video_play_toggled = toggle

    assert MainWindow._handle_color_space_playback_shortcut(mw) is False
    toggle.assert_not_called()


def test_arrow_key_on_composite_steps_comp_clip_frames(
        main_window, qapp) -> None:
    from PyQt6.QtCore import Qt

    mw = main_window
    _clean_instance_stubs(mw)
    _clear_focus(qapp)
    mw.current_page = "Composite"
    next_frame = Mock(name="comp_next_frame")
    mw.composite_page = SimpleNamespace(
        is_comp_clip_playback_active=lambda: True,
        comp_next_frame=next_frame,
        _composite_current_frame=7,
    )
    bar = Mock(name="video_control_bar")
    mw.composite_viewer = SimpleNamespace(video_control_bar=bar)

    handled = mw.eventFilter(mw, _key_event(Qt.Key.Key_Right))

    assert handled is True
    next_frame.assert_called_once()
    bar.set_current_frame.assert_called_once_with(7)


def test_frame_step_shortcut_disabled_in_image_color_mode(
        main_window, qapp) -> None:
    from PyQt6.QtCore import Qt

    mw = main_window
    _clean_instance_stubs(mw)
    _clear_focus(qapp)
    mw.current_page = "Composite"
    mw.composite_page = SimpleNamespace(_is_image_color_mode=True)

    assert mw.eventFilter(mw, _key_event(Qt.Key.Key_Left)) is False


def test_event_filter_passes_non_key_events_through(main_window) -> None:
    from PyQt6.QtCore import QEvent, QSize
    from PyQt6.QtGui import QResizeEvent

    mw = main_window
    event = QResizeEvent(QSize(10, 10), QSize(20, 20))

    assert mw.eventFilter(mw, event) is False
    assert event.type() == QEvent.Type.Resize


# ═══════════════════════════════════════════════════════════════════════
# Space 首拒链 handler 的守卫分支
# ═══════════════════════════════════════════════════════════════════════


def test_motion_graphic_editor_gets_first_refusal_on_space(main_window, qapp) -> None:
    """可见 MG 编辑器优先吃掉 Space（modeless Tool 窗口不被宿主抢键）。"""
    from PyQt6.QtWidgets import QWidget

    mw = main_window
    editor = QWidget()
    try:
        editor.setObjectName("MotionGraphicEditor")
        handled_spy = Mock(return_value=True)
        editor.handle_space_playback_shortcut = handled_spy  # type: ignore[attr-defined]
        editor.show()

        assert mw._handle_motion_graphic_space_playback() is True
        handled_spy.assert_called_once()

        editor.hide()
        handled_spy.reset_mock()
        assert mw._handle_motion_graphic_space_playback() is False
        handled_spy.assert_not_called()
    finally:
        editor.deleteLater()


def test_composite_space_shortcut_timeline_bound_toggles_controller(
        main_window, qapp) -> None:
    mw = main_window
    _clean_instance_stubs(mw)
    _clear_focus(qapp)
    mw.current_page = "Composite"
    play = Mock(name="controller.play")
    pause = Mock(name="controller.pause")
    controller = SimpleNamespace(is_playing=False, play=play, pause=pause)
    mw.composite_page = SimpleNamespace(is_comp_clip_playback_active=lambda: False)
    mw.composite_viewer = SimpleNamespace(video_control_bar=Mock())
    mw.timeline_panel = SimpleNamespace(get_playback_controller=lambda: controller)

    assert mw._handle_composite_space_playback_shortcut() is True
    play.assert_called_once()
    pause.assert_not_called()

    controller.is_playing = True
    assert mw._handle_composite_space_playback_shortcut() is True
    pause.assert_called_once()


def test_composite_space_shortcut_without_controller_declines(
        main_window, qapp) -> None:
    mw = main_window
    _clean_instance_stubs(mw)
    _clear_focus(qapp)
    mw.current_page = "Composite"
    mw.composite_page = SimpleNamespace(is_comp_clip_playback_active=lambda: False)
    mw.composite_viewer = SimpleNamespace(video_control_bar=Mock())
    mw.timeline_panel = SimpleNamespace(
        get_playback_controller=Mock(side_effect=AttributeError("no controller")))

    assert mw._handle_composite_space_playback_shortcut() is False


def test_composite_space_shortcut_image_color_mode_declines(
        main_window, qapp) -> None:
    mw = main_window
    _clean_instance_stubs(mw)
    _clear_focus(qapp)
    mw.current_page = "Composite"
    mw.composite_page = SimpleNamespace(_is_image_color_mode=True)

    assert mw._handle_composite_space_playback_shortcut() is False


def test_focus_is_within_guards_none_and_plain_objects() -> None:
    assert MainWindow._focus_is_within(None, SimpleNamespace()) is False
    assert MainWindow._focus_is_within(object(), None) is False
    # 非 widget 容器（无 isAncestorOf）安全返回 False
    assert MainWindow._focus_is_within(object(), SimpleNamespace()) is False


def test_focus_is_within_walks_real_widget_tree(qapp) -> None:
    from PyQt6.QtWidgets import QWidget

    outer = QWidget()
    inner = QWidget(outer)
    try:
        assert MainWindow._focus_is_within(inner, outer) is True
        other = QWidget()
        try:
            assert MainWindow._focus_is_within(other, outer) is False
        finally:
            other.deleteLater()
    finally:
        outer.deleteLater()


def test_raise_edit_viewer_dock_tolerates_deleted_dock(main_window) -> None:
    """关闭中的 dock（C++ 对象已析构）不得让快捷键路径崩溃。"""
    from PyQt6 import sip
    from PyQt6.QtWidgets import QDockWidget

    dock = QDockWidget()
    mw = main_window
    mw.dock_source_viewer = dock  # type: ignore[attr-defined]
    sip.delete(dock)

    mw._raise_edit_viewer_dock("dock_source_viewer")  # 不抛出即通过

    mw.__dict__.pop("dock_source_viewer", None)
    mw._raise_edit_viewer_dock("dock_source_viewer")  # 属性缺失同样安全


# ═══════════════════════════════════════════════════════════════════════
# on_engine_error（引擎错误上报接线）
# ═══════════════════════════════════════════════════════════════════════


def test_on_engine_error_resets_progress_and_reports(main_window, monkeypatch) -> None:
    from PyQt6.QtWidgets import QMessageBox, QProgressBar

    monkeypatch.setattr(mw_mod, "QMessageBox", MagicMock(spec=QMessageBox))
    mw = main_window
    _clean_instance_stubs(mw)
    mw.progress_bar = QProgressBar()
    status_bar = MagicMock(name="status_bar")
    mw.status_bar = status_bar

    mw.on_engine_error("shader compile failed")

    assert mw.progress_bar.isHidden() is True  # 进度条复位，UI 可恢复
    status_bar.showMessage.assert_called_once_with("Error: shader compile failed", 5000)
    mw_mod.QMessageBox.critical.assert_called_once()
    mw.progress_bar.deleteLater()
    mw.__dict__.pop("progress_bar", None)
    mw.__dict__.pop("status_bar", None)


# ═══════════════════════════════════════════════════════════════════════
# _script_timeline_context（脚本 API 只读上下文 provider，P1-COLOR-24）
# ═══════════════════════════════════════════════════════════════════════


def _timeline_host(project: Any = None, sequence: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        edit_page=None if sequence is None else SimpleNamespace(_current_timeline=sequence),
        _current_edit_project=project,
    )


def test_script_context_without_timeline_or_project_returns_empty() -> None:
    host = _timeline_host()

    assert MainWindow._script_timeline_context(host, "list_clips") == []
    assert MainWindow._script_timeline_context(host, "clip_grade", "c1") is None


def test_script_context_lists_clips_with_grade_flags() -> None:
    clips = [
        SimpleNamespace(clip_id="b", asset_id="a2", track_id="V1",
                        timeline_start=20, timeline_duration=10,
                        source_start=0, source_duration=10,
                        color_stack_id="graded"),
        SimpleNamespace(clip_id="a", asset_id="a1", track_id="V1",
                        timeline_start=0, timeline_duration=10,
                        source_start=0, source_duration=10,
                        color_stack_id=None),
    ]
    sequence = SimpleNamespace(clips=clips)
    project = SimpleNamespace(color_stacks={"graded": {"curves_data": {"lift": []}}})
    host = _timeline_host(project=project, sequence=sequence)

    result = MainWindow._script_timeline_context(host, "list_clips")

    assert [c["clip_id"] for c in result] == ["a", "b"]  # 按 timeline_start 排序
    by_id = {c["clip_id"]: c for c in result}
    assert by_id["a"]["has_grade"] is False  # 无调色栈
    assert by_id["b"]["has_grade"] is True  # 带曲线数据
    assert by_id["b"]["color_stack_id"] == "graded"


def test_script_context_clip_grade_snapshot_fields() -> None:
    stack = {
        "grading_params": {"exposure": 0.5},
        "lut_path": "/luts/v.cube",
        "lut_intensity": 0.7,
        "curves_data": {"gamma": []},
        "qualifier_params": {"enabled": True},
    }
    sequence = SimpleNamespace(clips=[
        SimpleNamespace(clip_id="c1", color_stack_id="s1"),
    ])
    host = _timeline_host(project=SimpleNamespace(color_stacks={"s1": stack}),
                          sequence=sequence)

    grade = MainWindow._script_timeline_context(host, "clip_grade", "c1")

    assert grade is not None
    assert grade["grading_params"] == {"exposure": 0.5}
    assert grade["lut_path"] == "/luts/v.cube"
    assert grade["lut_intensity"] == pytest.approx(0.7)
    assert grade["has_curves"] is True
    assert grade["has_qualifier"] is True


def test_script_context_clip_grade_missing_clip_or_stack_is_none() -> None:
    sequence = SimpleNamespace(clips=[SimpleNamespace(clip_id="c1", color_stack_id="s1")])
    host = _timeline_host(project=SimpleNamespace(color_stacks={}), sequence=sequence)

    assert MainWindow._script_timeline_context(host, "clip_grade", "nope") is None
    assert MainWindow._script_timeline_context(host, "clip_grade", "c1") is None
    assert MainWindow._script_timeline_context(host, "unknown_what") is None
