"""W02/W09：蓝色源目的轨路由 + 工具栏请求集中 bind/unbind。

W02（契约 D2）：源 Insert/Overwrite/Append 与拖放音频配对统一经
``resolve_source_destination_track_id`` 解析落轨——显式蓝路由真正生效、
显式关闭 = 该流不落、无条目回退红 target（既有语义）、陈旧条目不悬空。
红 target 独立于蓝徽章不受影响；蓝路由是 UI 状态，undo 不改变它。

W09（契约 D1 附）：EditPage 集中 bind/unbind——换面板先断旧连新，
每个请求每次触发只执行一次；无上下文不操作；未接线的宿主能力回写
按钮状态避免"假打开"。
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import EditTrack, Rational, TimelineSequence

pytestmark = pytest.mark.edit_ui


# ── fixtures ───────────────────────────────────────────────────────────


def _av_asset(asset_id: str = "av1") -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path="/tmp/av.mp4", media_type="video",
        display_name="av",
        metadata={"audio_info": {"channels": 2, "channel_layout": "stereo"}},
    )


def _silent_video_asset(asset_id: str = "sv1") -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path="/tmp/silent.mp4", media_type="video",
        display_name="silent", metadata={},
    )


def _audio_asset(asset_id: str = "au1") -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path="/tmp/voice.wav", media_type="audio",
        display_name="voice", metadata={},
    )


def _sequence() -> TimelineSequence:
    return TimelineSequence(
        timeline_id="dest-wiring", name="Wiring", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(
            EditTrack("v1", "V1", "video", 0),
            EditTrack("v2", "V2", "video", 1),
        ),
        audio_tracks=(
            EditTrack("a1", "A1", "audio", 0),
            EditTrack("a2", "A2", "audio", 1),
        ),
    )


def _view(qtbot: Any, assets: dict | None = None) -> Any:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_sequence())
    view.set_project(EditProject("p", "P", assets=assets or {}))
    return view


def _set_blue_route(view: Any, track_id: str, enabled: bool) -> None:
    view._on_source_patch_requested(track_id, enabled)


def _track_clip_ids(view: Any, track_id: str) -> list[str]:
    track = view.get_timeline().get_track_by_id(track_id)
    assert track is not None
    return [clip.clip_id for clip in track.clips]


# ── W02：解析器三态 ────────────────────────────────────────────────────


def test_stream_disabled_distinguishes_unset_vs_disabled(qtbot: Any) -> None:
    view = _view(qtbot)
    # 无条目（旧工程/从未设置）：不是关闭，回退红 target
    assert view.source_destination_stream_disabled("video") is False
    assert view.resolve_source_destination_track_id("video") == "v1"
    # 显式选中 v2
    _set_blue_route(view, "v2", True)
    assert view.source_destination_stream_disabled("video") is False
    assert view.resolve_source_destination_track_id("video") == "v2"
    # 再点一次 = 显式关闭该流
    _set_blue_route(view, "v2", False)
    assert view.source_destination_stream_disabled("video") is True
    assert view.resolve_source_destination_track_id("video") is None
    # 音频侧独立
    assert view.source_destination_stream_disabled("audio") is False
    assert view.resolve_source_destination_track_id("audio") == "a1"


def test_no_explicit_route_falls_back_to_red_target(qtbot: Any) -> None:
    view = _view(qtbot)
    view._target_track_id = "v2"  # 红 target 独立设置
    assert view.resolve_source_destination_track_id("video") == "v2"
    # 蓝徽章选中 v1 后蓝路由优先；红 target 不被改写
    _set_blue_route(view, "v1", True)
    assert view.resolve_source_destination_track_id("video") == "v1"
    assert view._target_track_id == "v2"


def test_stale_route_falls_back_after_track_deletion(qtbot: Any) -> None:
    view = _view(qtbot)
    _set_blue_route(view, "a2", True)
    assert view.resolve_source_destination_track_id("audio") == "a2"
    # 删除 a2（经既有命令）：陈旧条目回退，不留悬空 ID
    from src.features.edit.core.commands import DeleteTrackCommand

    view._execute_command(DeleteTrackCommand.create("a2"))
    assert view.resolve_source_destination_track_id("audio") == "a1"
    assert ("dest-wiring", "audio") not in view._source_patch_routes


# ── W02：插入/覆盖/追加四入口消费蓝路由 ────────────────────────────────


def test_insert_lands_on_blue_video_destination(qtbot: Any) -> None:
    view = _view(qtbot, {"sv1": _silent_video_asset()})
    _set_blue_route(view, "v2", True)
    assert view.insert_clip("sv1", 0, 23) is True
    assert _track_clip_ids(view, "v2") and not _track_clip_ids(view, "v1")


def test_linked_insert_pairs_audio_on_blue_audio_destination(qtbot: Any) -> None:
    view = _view(qtbot, {"av1": _av_asset()})
    _set_blue_route(view, "a2", True)
    assert view.insert_clip("av1", 0, 23) is True
    assert len(_track_clip_ids(view, "v1")) == 1
    assert len(_track_clip_ids(view, "a2")) == 1
    assert not _track_clip_ids(view, "a1")


def test_blue_video_disabled_av_source_inserts_audio_only(qtbot: Any) -> None:
    view = _view(qtbot, {"av1": _av_asset()})
    _set_blue_route(view, "v1", False)  # 显式关闭视频流
    assert view.insert_clip("av1", 0, 23) is True
    assert not _track_clip_ids(view, "v1") and not _track_clip_ids(view, "v2")
    assert len(_track_clip_ids(view, "a1")) == 1


def test_blue_audio_disabled_skips_linked_pairing(qtbot: Any) -> None:
    view = _view(qtbot, {"av1": _av_asset()})
    _set_blue_route(view, "a1", False)
    assert view.insert_clip("av1", 0, 23) is True
    assert len(_track_clip_ids(view, "v1")) == 1
    assert not _track_clip_ids(view, "a1") and not _track_clip_ids(view, "a2")


def test_blue_video_disabled_silent_video_rejected(qtbot: Any) -> None:
    view = _view(qtbot, {"sv1": _silent_video_asset()})
    _set_blue_route(view, "v1", False)
    before = view.get_timeline()
    assert view.insert_clip("sv1", 0, 23) is False
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_blue_both_disabled_rejects_all_entries(qtbot: Any) -> None:
    view = _view(qtbot, {"av1": _av_asset()})
    _set_blue_route(view, "v1", False)
    _set_blue_route(view, "a1", False)
    for entry in (view.insert_clip, view.overwrite_clip, view.append_clip):
        assert entry("av1", 0, 23) is False
    assert not view._command_stack._undo_stack


def test_overwrite_and_append_route_to_blue(qtbot: Any) -> None:
    view = _view(qtbot, {"sv1": _silent_video_asset()})
    _set_blue_route(view, "v2", True)
    assert view.overwrite_clip("sv1", 0, 23) is True
    assert _track_clip_ids(view, "v2") and not _track_clip_ids(view, "v1")
    assert view.append_clip("sv1", 0, 23) is True
    assert len(_track_clip_ids(view, "v2")) == 2


def test_undo_keeps_blue_route(qtbot: Any) -> None:
    view = _view(qtbot, {"sv1": _silent_video_asset()})
    _set_blue_route(view, "v2", True)
    assert view.insert_clip("sv1", 0, 23) is True
    view.undo()
    assert view.resolve_source_destination_track_id("video") == "v2"
    assert view.source_patch_track_id("video") == "v2"
    assert not _track_clip_ids(view, "v2")


# ── W09：集中 bind/unbind ─────────────────────────────────────────────


class _FakePanel(QObject):
    """最小面板替身：仅暴露 W09 绑定面。"""

    source_insert_requested = pyqtSignal()
    source_overwrite_requested = pyqtSignal()
    source_append_requested = pyqtSignal()
    source_replace_requested = pyqtSignal()
    source_patch_changed = pyqtSignal(str, str, bool)
    keyframe_tray_requested = pyqtSignal(bool)
    voiceover_requested = pyqtSignal()
    flag_requested = pyqtSignal(str)


class _FakeSourceViewer(QObject):
    in_point = 0
    out_point = 10

    @property
    def current_asset(self) -> Any:
        return None


def test_rebind_disconnects_previous_panel(qtbot: Any,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.ui.edit_page import EditPage

    monkeypatch.setattr(EditPage, "_ensure_audio_manager", lambda self: None)
    page = EditPage()
    qtbot.addWidget(page)
    panel1 = _FakePanel()
    panel2 = _FakePanel()
    calls: list[str] = []
    page._on_timeline_source_insert = lambda: calls.append("insert")  # type: ignore[assignment]

    page._bind_timeline_panel_requests(panel1)
    panel1.source_insert_requested.emit()
    assert calls == ["insert"]

    # 换面板：旧连接必须先断开——旧面板 emit 不再触发任何执行
    page._bind_timeline_panel_requests(panel2)
    panel1.source_insert_requested.emit()
    assert calls == ["insert"]
    panel2.source_insert_requested.emit()
    assert calls == ["insert", "insert"]
    assert page._panel_request_generation == 2


def test_binder_binds_all_eight_requests_once(qtbot: Any,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.ui.edit_page import EditPage

    monkeypatch.setattr(EditPage, "_ensure_audio_manager", lambda self: None)
    page = EditPage()
    qtbot.addWidget(page)
    panel = _FakePanel()
    counts = {"insert": 0, "patch": 0, "tray": 0, "voice": 0, "flag": 0,
              "replace": 0}

    page._on_timeline_source_insert = lambda: counts.__setitem__("insert", counts["insert"] + 1)  # type: ignore[assignment]
    page._on_timeline_source_patch_changed = lambda *a: counts.__setitem__("patch", counts["patch"] + 1)  # type: ignore[assignment]
    page._on_timeline_keyframe_tray_requested = lambda v: counts.__setitem__("tray", counts["tray"] + 1)  # type: ignore[assignment]
    page._on_timeline_voiceover_requested = lambda: counts.__setitem__("voice", counts["voice"] + 1)  # type: ignore[assignment]
    page._on_timeline_flag_requested = lambda c: counts.__setitem__("flag", counts["flag"] + 1)  # type: ignore[assignment]
    page._on_timeline_source_replace = lambda: counts.__setitem__("replace", counts["replace"] + 1)  # type: ignore[assignment]

    page._bind_timeline_panel_requests(panel)
    panel.source_insert_requested.emit()
    panel.source_replace_requested.emit()
    panel.source_patch_changed.emit("video", "v2", True)
    panel.keyframe_tray_requested.emit(True)
    panel.voiceover_requested.emit()
    panel.flag_requested.emit("#ff0000")
    assert counts == {"insert": 1, "patch": 1, "tray": 1, "voice": 1,
                      "flag": 1, "replace": 1}


def test_patch_slot_without_context_is_noop(qtbot: Any,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.ui.edit_page import EditPage

    monkeypatch.setattr(EditPage, "_ensure_audio_manager", lambda self: None)
    page = EditPage()
    qtbot.addWidget(page)
    # 无 panel/view/timeline：不抛异常、不崩溃
    page._on_timeline_source_patch_changed("video", "v2", True)


def test_unhosted_tray_request_reverts_button(qtbot: Any) -> None:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    qtbot.addWidget(panel)
    emitted: list[bool] = []
    panel.keyframe_tray_requested.connect(emitted.append)
    panel.btn_keyframe_tray.setChecked(True)  # 用户点击 → 发请求
    assert emitted == [True]

    # 宿主未真正提供 Tray：回写为未选中，且不再次发信号（防递归）
    panel.set_keyframe_tray_checked(False)
    assert panel.btn_keyframe_tray.isChecked() is False
    assert emitted == [True]
