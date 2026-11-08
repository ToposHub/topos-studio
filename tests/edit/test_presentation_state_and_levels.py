"""W07/W08：时间线展示偏好保存恢复 + 电平表订阅生命周期。

W07（计划 §6）：展示偏好集中为有版本 payload（缩略图/波形/zoom
preset 与自定义 ppf+scroll/Position Lock/Tray/源目的轨 UI）；恢复
零内容命令、零 revision；非法值安全回退；未知字段 UIState 级保留。
W08：audio_level_changed → panel.update_audio_levels 跟随当前面板，
重附先断旧连新，停机断开，不重复连接。
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from src.features.edit.core.timeline_v2 import EditTrack, Rational, TimelineSequence

pytestmark = pytest.mark.edit_ui


def _timeline() -> TimelineSequence:
    return TimelineSequence(
        timeline_id="pres", name="Pres", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0),
                      EditTrack("v2", "V2", "video", 1)),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


def _view(qtbot: Any) -> Any:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline())
    return view


# ── W07：view 级展示偏好 ──────────────────────────────────────────────


def test_view_presentation_roundtrip_zero_commands(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_thumbnail_display_mode("filmstrip")
    view.set_waveforms_visible(False)
    view.set_position_lock_enabled(True)
    view._on_source_patch_requested("v2", True)
    view._on_source_patch_requested("a1", False)
    exported = view.export_presentation_state()
    assert exported["thumbnail_mode"] == "filmstrip"
    assert exported["waveforms_visible"] is False
    assert exported["position_lock"] is True
    assert exported["source_destination_ui"] == {
        "pres": {"video": "v2", "audio": None}}

    fresh = _view(qtbot)
    revision_before = fresh.get_timeline().revision
    fresh.apply_presentation_state(exported)
    assert fresh.thumbnail_display_mode() == "filmstrip"
    assert fresh.waveforms_visible() is False
    assert fresh.is_position_lock_enabled() is True
    assert fresh.resolve_source_destination_track_id("video") == "v2"
    assert fresh.source_destination_stream_disabled("audio") is True
    # 恢复零命令、零 revision
    assert not fresh._command_stack._undo_stack
    assert fresh.get_timeline().revision == revision_before


def test_presentation_restore_invalid_values_fallback(qtbot: Any) -> None:
    view = _view(qtbot)
    view.apply_presentation_state({
        "thumbnail_mode": "hologram",     # 非法枚举 → 忽略
        "waveforms_visible": "yes",       # 非 bool → 忽略
        "position_lock": 3,               # 非 bool → 忽略
        "source_destination_ui": {"pres": {"subtitle": "x", "video": 42}},
        "zoom_preset": "galaxy",
    })
    assert view.thumbnail_display_mode() == "auto"
    assert view.waveforms_visible() is True
    assert view.is_position_lock_enabled() is False
    assert view.resolve_source_destination_track_id("video") == "v1"
    assert ("pres", "subtitle") not in view._source_patch_routes


def test_stale_route_after_restore_falls_back(qtbot: Any) -> None:
    view = _view(qtbot)
    view.apply_presentation_state({
        "source_destination_ui": {"pres": {"video": "v-deleted"}},
    })
    # 轨不存在 → 回退红 target（v1），不留悬空
    assert view.resolve_source_destination_track_id("video") == "v1"
    assert ("pres", "video") not in view._source_patch_routes


# ── W07：panel 级（zoom/Tray）与 UIState ─────────────────────────────


def _panel(qtbot: Any) -> Any:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    qtbot.addWidget(panel)
    panel.resize(1200, 460)
    panel.load_timeline(_timeline())
    return panel


def test_panel_presentation_roundtrip(qtbot: Any) -> None:
    panel = _panel(qtbot)
    panel.set_keyframe_tray_visible(True)
    exported = panel.export_presentation_state()
    assert exported["zoom_preset"] in {"custom", "full", "detail"}
    assert exported["keyframe_tray_visible"] is True

    fresh = _panel(qtbot)
    revision_before = fresh.get_timeline_view().get_timeline().revision
    exported["zoom_preset"] = "custom"
    exported["custom_pixels_per_frame"] = 3.5
    exported["custom_scroll"] = 120
    fresh.apply_presentation_state(exported)
    assert fresh._zoom_preset == "custom"
    assert fresh._custom_zoom[0] == pytest.approx(3.5)
    assert fresh.is_keyframe_tray_visible() is True
    assert not fresh.get_timeline_view()._command_stack._undo_stack
    assert fresh.get_timeline_view().get_timeline().revision == revision_before


def test_uistate_presentation_roundtrip_and_legacy() -> None:
    from src.features.edit.core.serialization import UIState

    state = UIState(active_timeline_id="pres", playhead_frame=7)
    state.timeline_presentation = {
        "thumbnail_mode": "none",
        "future_field": {"keep": True},
    }
    revived = UIState.from_dict(state.to_dict())
    assert revived.timeline_presentation["thumbnail_mode"] == "none"
    # 未知字段重存保留
    assert revived.timeline_presentation["future_field"] == {"keep": True}
    # 旧包缺键 → None
    legacy = UIState.from_dict({"active_timeline_id": "pres"})
    assert legacy.timeline_presentation is None


# ── W08：电平订阅生命周期 ─────────────────────────────────────────────


class _FakeAudioManager(QObject):
    audio_level_changed = pyqtSignal(list)
    audio_track_levels_changed = pyqtSignal(list)

    def cleanup(self) -> None:  # W08：shutdown 路径幂等
        pass

    def stop(self) -> None:
        pass


def _page(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from src.features.edit.ui.edit_page import EditPage

    monkeypatch.setattr(EditPage, "_ensure_audio_manager", lambda self: None)
    page = EditPage()
    qtbot.addWidget(page)
    return page


def test_level_feed_follows_attached_panel(qtbot: Any,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page(qtbot, monkeypatch)
    manager = _FakeAudioManager()
    page._audio_manager = manager
    panel1 = _panel(qtbot)
    panel2 = _panel(qtbot)
    feed1: list = []
    feed2: list = []
    panel1.update_audio_levels = feed1.append  # type: ignore[assignment]
    panel2.update_audio_levels = feed2.append  # type: ignore[assignment]

    page.set_timeline_panel(panel1)
    page._reconnect_timeline_level_meter()
    manager.audio_level_changed.emit([-12.0, -12.0])
    assert feed1 == [[-12.0, -12.0]] and feed2 == []

    # 重附面板：旧 panel 断开，新 panel 接收；不产生重复投递
    page.set_timeline_panel(panel2)
    manager.audio_level_changed.emit([-6.0, -6.0])
    assert feed1 == [[-12.0, -12.0]]  # 旧 panel 不再响应
    assert feed2 == [[-6.0, -6.0]]

    # 幂等：重复重连不叠加（一次 emit 只一次投递）
    page._reconnect_timeline_level_meter()
    manager.audio_level_changed.emit([-3.0, -3.0])
    assert feed2 == [[-6.0, -6.0], [-3.0, -3.0]]


def test_shutdown_disconnects_level_feed(qtbot: Any,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page(qtbot, monkeypatch)
    manager = _FakeAudioManager()
    page._audio_manager = manager
    panel = _panel(qtbot)
    feed: list = []
    panel.update_audio_levels = feed.append  # type: ignore[assignment]
    page.set_timeline_panel(panel)
    page._reconnect_timeline_level_meter()

    page.shutdown_audio_playback()
    assert page._timeline_level_connection is None
    manager.audio_level_changed.emit([-1.0, -1.0])
    assert feed == []


def test_no_double_connect_when_manager_created_after_panel(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """先附 panel 再建 manager（真实顺序之一）——同样只连一条。"""
    page = _page(qtbot, monkeypatch)
    panel = _panel(qtbot)
    feed: list = []
    panel.update_audio_levels = feed.append  # type: ignore[assignment]
    page.set_timeline_panel(panel)

    manager = _FakeAudioManager()
    page._audio_manager = manager
    page._reconnect_timeline_level_meter()
    page._reconnect_timeline_level_meter()  # 重复触发（防御）
    manager.audio_level_changed.emit([-9.0, -9.0])
    assert feed == [[-9.0, -9.0]]


# ── B07/B09：未知字段/版本保留 + 跨工程上下文隔离 ─────────────────────


def test_b07_unknown_fields_and_version_roundtrip(qtbot: Any) -> None:
    """B07 反例：含 future 字段/version=2 的 payload apply→export 不丢。"""
    view = _view(qtbot)
    payload = {
        "version": 2,
        "thumbnail_mode": "filmstrip",
        "future_field": {"keep": True},
        "source_destination_ui": {
            "pres": {"video": "v2", "future_stream": "v9"},
        },
    }
    view.apply_presentation_state(payload)
    # 已知键按当前语义解释
    assert view._thumbnail_display_mode == "filmstrip"
    exported = view.export_presentation_state()
    # 未知顶层字段与未来版本号原样回写
    assert exported["future_field"] == {"keep": True}
    assert exported["version"] == 2
    # 路由嵌套未知键保留，已知路由不丢
    routes = exported["source_destination_ui"]
    assert routes["pres"]["future_stream"] == "v9"
    assert routes["pres"]["video"] == "v2"
    # 再次 apply→export 往返稳定
    fresh = _view(qtbot)
    fresh.apply_presentation_state(exported)
    assert fresh.export_presentation_state() == exported


def test_b07_legacy_payload_exports_current_version(qtbot: Any) -> None:
    """旧包无 version：export 写当前版本 1，其他未知键仍保留。"""
    view = _view(qtbot)
    view.apply_presentation_state({
        "thumbnail_mode": "auto", "legacy_extra": 7,
    })
    exported = view.export_presentation_state()
    assert exported["version"] == 1
    assert exported["legacy_extra"] == 7


def test_b09_project_switch_isolates_routes_and_extras(qtbot: Any) -> None:
    """B09 反例：同 timeline ID 不同 project——旧工程禁用路由不得继承。"""
    from src.features.edit.core.project import EditProject

    view = _view(qtbot)
    project_a = EditProject("pa", "A")
    project_b = EditProject("pb", "B")
    view.set_project(project_a)
    view._on_source_patch_requested("v1", False)  # 关闭蓝视频路由
    view.apply_presentation_state({"future_field": {"a": 1}})
    assert view.source_destination_stream_disabled("video") is True

    view.set_project(project_b)  # 同 timeline_id="pres" 的另一工程
    assert view.source_destination_stream_disabled("video") is False
    assert view.export_presentation_state().get("future_field") is None
    # 无任何显式条目（含嵌套扩展）——路由表整体为空
    assert view.export_presentation_state()["source_destination_ui"] == {}


def test_b09_same_project_refresh_keeps_routes(qtbot: Any) -> None:
    """同工程刷新（asset 更新等触发的 set_project）不丢路由与扩展。"""
    from src.features.edit.core.project import EditProject

    view = _view(qtbot)
    project_a = EditProject("pa", "A")
    view.set_project(project_a)
    view._on_source_patch_requested("v2", True)
    view.apply_presentation_state({"future_field": {"a": 1}})

    view.set_project(EditProject("pa", "A refreshed"))

    assert view.resolve_source_destination_track_id("video") == "v2"
    assert view.export_presentation_state()["future_field"] == {"a": 1}


def test_b09_clear_resets_extras(qtbot: Any) -> None:
    view = _view(qtbot)
    view.apply_presentation_state({"future_field": {"a": 1}})
    view.clear()
    exported = view.export_presentation_state()
    assert "future_field" not in exported
    assert exported["version"] == 1
