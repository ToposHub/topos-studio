"""W01 Replace Edit + W04 Voiceover 宿主接线（契约 D1/D2）。

W01：Replace 按源对齐帧替换——保留足迹/速度/效果/链接，仅换素材与
源窗；linked 伙伴成对宏事务单步 undo；源不足/多选/无目标拒绝零写入。
W04：Voiceover 桥接既有 ADR 链——目标音轨取有效音频目的轨，同轨
再触发 = 停止导入，无上下文不操作。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from PyQt6.QtCore import QObject

from src.features.edit.core.commands import (
    MacroCommand,
    ReplaceClipWithSourceCommand,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_ui


def _asset(asset_id: str, *, frames: int, fps: float = 24.0,
           audio: bool = False) -> MediaAsset:
    metadata: dict[str, Any] = {"fps": fps, "total_frames": frames}
    if audio:
        metadata["audio_info"] = {"channels": 2, "channel_layout": "stereo"}
    return MediaAsset(
        asset_id=asset_id, path=f"/tmp/{asset_id}.mp4", media_type="video",
        display_name=asset_id, metadata=metadata,
    )


def _sequence_with_clip() -> TimelineSequence:
    video_clip = ClipInstance(
        clip_id="vc1", asset_id="old", track_id="v1",
        timeline_start=100, timeline_duration=48,
        source_start=10, source_duration=48,
        fade_in_duration=6, label_color="#3aaea4",
    )
    audio_clip = ClipInstance(
        clip_id="ac1", asset_id="old", track_id="a1",
        timeline_start=100, timeline_duration=48,
        source_start=10, source_duration=48,
        linked_clip_id="vc1",
    )
    video = replace(video_clip, linked_clip_id="ac1")
    return TimelineSequence(
        timeline_id="replace-w01", name="Replace", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(video,)),),
        audio_tracks=(EditTrack(
            "a1", "A1", "audio", 0, clips=(audio_clip,)),),
    )


def _view(qtbot: Any, timeline: TimelineSequence | None = None,
          assets: dict | None = None) -> Any:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline if timeline is not None else _sequence_with_clip())
    view.set_project(EditProject(
        "p", "P",
        assets=assets if assets is not None else {
            "old": _asset("old", frames=200),
            "new": _asset("new", frames=200),
            "new_av": _asset("new_av", frames=200, audio=True),
            "short": _asset("short", frames=30),
        }))
    return view


# ── W01：命令层 ───────────────────────────────────────────────────────


def test_replace_command_roundtrip_and_undo() -> None:
    timeline = _sequence_with_clip()
    cmd = ReplaceClipWithSourceCommand.create(
        clip_id="vc1", track_id="v1", new_asset_id="new",
        new_source_start=50, new_source_duration=48,
        new_fps_meta=(48, 1, 24, 1, None),
    )
    new_timeline = cmd.apply(timeline)
    clip = new_timeline.get_clip_by_id("vc1")
    assert clip.asset_id == "new"
    assert clip.source_start == 50 and clip.source_duration == 48
    assert (clip._source_fps_num, clip._source_fps_den) == (48, 1)
    # 保留项：足迹/速度/淡变/label/链接
    assert clip.timeline_start == 100 and clip.timeline_duration == 48
    assert clip.fade_in_duration == 6 and clip.label_color == "#3aaea4"
    assert clip.linked_clip_id == "ac1"
    restored = cmd.undo(new_timeline)
    back = restored.get_clip_by_id("vc1")
    assert back.asset_id == "old"
    assert (back.source_start, back.source_duration) == (10, 48)
    assert back._source_fps_num is None  # 旧片段原本无 fps 元数据
    # 序列化往返
    data = cmd.to_dict()
    revived = ReplaceClipWithSourceCommand.from_dict(data)
    assert revived.new_source_duration == 48
    assert revived.new_fps_meta == (48, 1, 24, 1, None)


def test_replace_command_rejects_bad_values() -> None:
    timeline = _sequence_with_clip()
    with pytest.raises(ValueError):
        ReplaceClipWithSourceCommand.create(
            "vc1", "v1", "new", -1, 48).apply(timeline)
    with pytest.raises(ValueError):
        ReplaceClipWithSourceCommand.create(
            "vc1", "v1", "new", 10, 0).apply(timeline)


# ── W01：view 层事务 ──────────────────────────────────────────────────


def test_replace_selected_clip_preserves_footprint(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_selection(_selection_with("vc1"))
    assert view.replace_clip_with_source("new", 20, 67) is True
    clip = view.get_timeline().get_clip_by_id("vc1")
    assert clip.asset_id == "new"
    assert clip.source_start == 20 and clip.source_duration == 48
    assert clip.timeline_start == 100 and clip.timeline_duration == 48
    assert clip.fade_in_duration == 6
    # 单步 undo 完整还原
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    clip = view.get_timeline().get_clip_by_id("vc1")
    assert clip.asset_id == "old" and clip.source_start == 10


def test_replace_without_selection_uses_playhead_clip(qtbot: Any) -> None:
    view = _view(qtbot)
    view._playhead_frame = 120  # vc1: [100, 148)
    assert view.replace_clip_with_source("new", 0, 47) is True
    assert view.get_timeline().get_clip_by_id("vc1").asset_id == "new"


def test_replace_linked_pair_single_undo(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_selection(_selection_with("vc1"))
    assert view.replace_clip_with_source("new_av", 5, 52) is True
    timeline = view.get_timeline()
    assert timeline.get_clip_by_id("vc1").asset_id == "new_av"
    assert timeline.get_clip_by_id("ac1").asset_id == "new_av"
    assert timeline.get_clip_by_id("ac1").source_start == 5
    assert len(view._command_stack._undo_stack) == 1  # 宏 = 单步
    view.undo()
    timeline = view.get_timeline()
    assert timeline.get_clip_by_id("vc1").asset_id == "old"
    assert timeline.get_clip_by_id("ac1").asset_id == "old"
    assert timeline.get_clip_by_id("ac1").source_start == 10


def test_replace_source_insufficient_rejected(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_selection(_selection_with("vc1"))
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    before = view.get_timeline()
    # short 素材从 20 起只剩 10 帧 < 需要 48 帧
    assert view.replace_clip_with_source("short", 20, 67) is False
    assert messages and "shorter" in messages[0]
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_replace_multi_selection_rejected(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_selection(_selection_with("vc1", "ac1"))
    assert view.replace_clip_with_source("new", 0, 47) is False
    assert not view._command_stack._undo_stack


def test_replace_no_target_rejected(qtbot: Any) -> None:
    view = _view(qtbot)
    view._playhead_frame = 0  # 不在任何 clip 上
    assert view.replace_clip_with_source("new", 0, 47) is False


def test_replace_missing_asset_rejected(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_selection(_selection_with("vc1"))
    assert view.replace_clip_with_source("ghost", 0, 47) is False


def test_replace_mixed_fps_needs_scaled_source(qtbot: Any) -> None:
    # 48fps 源进 24fps 时间线：48 时间线帧需要 96 源帧
    timeline = _sequence_with_clip()
    view = _view(qtbot, timeline, assets={
        "old": _asset("old", frames=200),
        "new48": _asset("new48", frames=400, fps=48.0),
    })
    view.set_selection(_selection_with("vc1"))
    assert view.replace_clip_with_source("new48", 0, 47) is True
    clip = view.get_timeline().get_clip_by_id("vc1")
    assert clip.source_duration == 96
    assert (clip._source_fps_num, clip._source_fps_den) == (48, 1)


def _selection_with(*clip_ids: str) -> Any:
    from src.features.edit.core.selection import TimelineSelection

    track_of = {"vc1": "v1", "ac1": "a1"}
    selection = TimelineSelection()
    for index, clip_id in enumerate(clip_ids):
        selection = selection.with_clip_selected(
            clip_id, track_of.get(clip_id, "v1"), "replace-w01",
            replace=(index == 0),
        )
    return selection


# ── W04：Voiceover 桥接 ───────────────────────────────────────────────


class _FakeRecordingMain(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.started: list[str] = []
        self.stopped: list[str] = []
        self._recording_managers: dict[str, Any] = {}
        self.dock_adr_take_sheet = None

    def _start_recording(self, track_id: str) -> None:
        self.started.append(track_id)
        self._recording_managers[track_id] = _FakeManager()

    def _stop_recording_and_import(self, track_id: str) -> None:
        self.stopped.append(track_id)
        self._recording_managers.pop(track_id, None)


class _FakeManager:
    is_recording = True


def _page(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from src.features.edit.ui.edit_page import EditPage

    monkeypatch.setattr(EditPage, "_ensure_audio_manager", lambda self: None)
    page = EditPage()
    qtbot.addWidget(page)
    return page


def _attach(qtbot: Any, page: Any) -> Any:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    qtbot.addWidget(panel)
    panel.load_timeline(_sequence_with_clip())
    panel.get_timeline_view().set_project(EditProject(
        "p", "P", assets={
            "old": _asset("old", frames=200),
            "new": _asset("new", frames=200),
        }))
    page.set_timeline_panel(panel)
    return panel


def test_voiceover_starts_on_effective_audio_track(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page(qtbot, monkeypatch)
    panel = _attach(qtbot, page)
    main = _FakeRecordingMain()
    page._main_window = main
    # 蓝徽章选 a1 → 有效音频目的轨
    panel.get_timeline_view()._on_source_patch_requested("a1", True)

    page._on_timeline_voiceover_requested()
    assert main.started == ["a1"]
    # 同轨再触发 = 停止导入
    page._on_timeline_voiceover_requested()
    assert main.stopped == ["a1"]
    assert main.started == ["a1"]


def test_voiceover_without_context_is_noop(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page(qtbot, monkeypatch)
    main = _FakeRecordingMain()
    page._main_window = main
    # 无 panel：不启动
    page._on_timeline_voiceover_requested()
    assert main.started == []


def test_voiceover_without_controller_is_noop(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page(qtbot, monkeypatch)
    _attach(qtbot, page)  # 有时间线上下文但无宿主 controller
    page._main_window = None
    page._on_timeline_voiceover_requested()  # 不抛异常


def test_replace_requested_from_toolbar_reaches_view(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """W09+W01：工具栏 Replace 按钮 → EditPage → view.replace。"""
    page = _page(qtbot, monkeypatch)
    panel = _attach(qtbot, page)

    class _Viewer(QObject):
        in_point = 0
        out_point = 10

        @property
        def current_asset(self) -> Any:
            from src.features.edit.core.project import MediaAsset

            return MediaAsset("new", "/tmp/new.mp4", "video", "new",
                              {"fps": 24.0, "total_frames": 200})

    page._source_viewer = _Viewer()
    page._sync_source_edit_buttons()  # In/Out 有效 → Replace 按钮可用
    panel.get_timeline_view()._playhead_frame = 120
    panel.btn_replace.click()
    assert panel.get_timeline_view().get_timeline().get_clip_by_id(
        "vc1").asset_id == "new"
