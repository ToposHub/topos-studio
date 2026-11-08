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
    # Two independent editing units; a reciprocal A/V pair is one unit in
    # the Resolve-style selection UI and remains a valid Replace target.
    timeline = _sequence_with_clip()
    other = replace(timeline.get_clip_by_id("vc1"), clip_id="vc2",
                    timeline_start=180, linked_clip_id=None)
    timeline = timeline.with_clip_added("v1", other)
    view = _view(qtbot, timeline)
    view.set_selection(_selection_with("vc1", "vc2"))
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


# ── B03/B04/B05：Replace 源窗/成员/流预检回归 ─────────────────────────


def test_b03_speed_point_clip_keeps_curve_window(qtbot: Any) -> None:
    """B03 反例一：速度点片按曲线积分求源窗，采样不再尾部夹帧。

    48 帧 clip，base 1x + 点(16→2x)，原 source_duration=80；替换后
    源窗必须是 80（旧代码写 48，尾段 [0,16,32,47,47,47]）。
    """
    from src.features.edit.core.clip_params import SpeedParams, SpeedPoint

    timeline = _sequence_with_clip()
    curved = replace(
        timeline.get_clip_by_id("vc1"),
        speed=SpeedParams(speed=1.0, speed_points=(
            SpeedPoint(source_offset=16, speed=2.0),)),
        source_start=0, source_duration=80)
    timeline = timeline.with_track_updated(
        replace(timeline.video_tracks[0], clips=(curved,)))
    view = _view(qtbot, timeline, assets={
        "old": _asset("old", frames=200),
        "new": _asset("new", frames=400),
    })
    view.set_selection(_selection_with("vc1"))

    assert view.replace_clip_with_source("new", 0, 47) is True

    clip = view.get_timeline().get_clip_by_id("vc1")
    assert clip.source_start == 0
    assert clip.source_duration == 80  # 曲线积分，不是 48×1
    assert clip.speed.speed_points == curved.speed.speed_points
    # 逐帧采样连续：与替换前同样的映射（audit 基线 [0,16,32,48,64,78]）
    sampled = [
        clip.timeline_to_source_frame(clip.timeline_start + t)
        for t in (0, 16, 24, 32, 40, 47)]
    assert sampled == [0, 16, 32, 48, 64, 78]


def test_b03_linked_partner_gets_own_window(qtbot: Any) -> None:
    """B03 反例二：伙伴按自身速度独立求源窗（2x 伙伴需要 192）。

    24fps 时间线，主视频 1x、音频伙伴 2x，各 48 帧；换 48fps 有声源。
    """
    timeline = _sequence_with_clip()
    from src.features.edit.core.clip_params import SpeedParams

    audio = replace(
        timeline.get_clip_by_id("ac1"),
        speed=SpeedParams(speed=2.0), source_duration=96)
    timeline = timeline.with_track_updated(
        replace(timeline.audio_tracks[0], clips=(audio,)))
    view = _view(qtbot, timeline, assets={
        "old": _asset("old", frames=200),
        "new_av": _asset("new_av", frames=400, fps=48.0, audio=True),
    })
    view.set_selection(_selection_with("vc1"))

    assert view.replace_clip_with_source("new_av", 0, 47) is True

    new_timeline = view.get_timeline()
    video = new_timeline.get_clip_by_id("vc1")
    partner = new_timeline.get_clip_by_id("ac1")
    assert video.source_duration == 96   # 48 × 1x × (48/24)
    assert partner.source_duration == 192  # 48 × 2x × (48/24)
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    restored = view.get_timeline()
    assert restored.get_clip_by_id("vc1").asset_id == "old"
    assert restored.get_clip_by_id("ac1").asset_id == "old"


def test_b03_partner_window_insufficient_rejects_whole_pair(qtbot: Any) -> None:
    """B03：伙伴所需源窗不足时整次拒绝，零写入。"""
    timeline = _sequence_with_clip()
    from src.features.edit.core.clip_params import SpeedParams

    audio = replace(
        timeline.get_clip_by_id("ac1"),
        speed=SpeedParams(speed=2.0), source_duration=96)
    timeline = timeline.with_track_updated(
        replace(timeline.audio_tracks[0], clips=(audio,)))
    # 48fps 只有 300 帧：视频需要 96、音频需要 192——从 200 起不足
    view = _view(qtbot, timeline, assets={
        "old": _asset("old", frames=200),
        "new_av": _asset("new_av", frames=300, fps=48.0, audio=True),
    })
    view.set_selection(_selection_with("vc1"))
    before = view.get_timeline()

    assert view.replace_clip_with_source("new_av", 200, 247) is False

    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_b03_dangling_link_does_not_pull_partner(qtbot: Any) -> None:
    """B03 补充：单向/悬空链接不把无关片段拉进替换事务。"""
    timeline = _sequence_with_clip()
    # 主片指向一个不存在的伙伴 ID
    dangling = replace(timeline.get_clip_by_id("vc1"), linked_clip_id="ghost")
    timeline = timeline.with_track_updated(
        replace(timeline.video_tracks[0], clips=(dangling,)))
    view = _view(qtbot, timeline)
    view.set_selection(_selection_with("vc1"))

    assert view.replace_clip_with_source("new_av", 0, 47) is True

    assert view.get_timeline().get_clip_by_id("vc1").asset_id == "new_av"
    assert view.get_timeline().get_clip_by_id("ac1").asset_id == "old"


def test_b04_freeze_remaps_to_new_alignment_frame(qtbot: Any) -> None:
    """B04 反例：冻结片换 In=200 后冻结帧=200，不再映射旧素材帧 5。"""
    timeline = _sequence_with_clip()
    frozen = replace(
        timeline.get_clip_by_id("vc1"),
        freeze_source_frame=5, source_start=0, source_duration=48)
    timeline = timeline.with_track_updated(
        replace(timeline.video_tracks[0], clips=(frozen,)))
    view = _view(qtbot, timeline, assets={
        "old": _asset("old", frames=200),
        "new": _asset("new", frames=400),
    })
    view.set_selection(_selection_with("vc1"))

    assert view.replace_clip_with_source("new", 200, 247) is True

    clip = view.get_timeline().get_clip_by_id("vc1")
    assert clip.source_start == 200
    assert clip.source_duration == 1  # 冻结片只播一帧
    assert clip.freeze_source_frame == 200
    assert clip.timeline_to_source_frame(clip.timeline_start) == 200
    assert clip.timeline_to_source_frame(
        clip.timeline_start + clip.timeline_duration - 1) == 200
    # undo 完整还原冻结帧与源窗
    view.undo()
    back = view.get_timeline().get_clip_by_id("vc1")
    assert back.freeze_source_frame == 5
    assert (back.source_start, back.source_duration) == (0, 48)
    assert back.asset_id == "old"


def test_b05_audio_source_rejected_for_video_track(qtbot: Any) -> None:
    """B05 反例：纯音频源 Replace 视频轨 clip 必须拒绝、零写入。"""
    from src.features.edit.core.project import MediaAsset

    audio_asset = MediaAsset(
        asset_id="pure_audio", path="/tmp/pa.mp3", media_type="audio",
        display_name="pure_audio",
        metadata={"fps": 48.0, "total_frames": 400,
                  "audio_info": {"channels": 2}},
    )
    view = _view(qtbot, assets={
        "old": _asset("old", frames=200), "pure_audio": audio_asset})
    view.set_selection(_selection_with("vc1"))
    before = view.get_timeline()

    assert view.replace_clip_with_source("pure_audio", 0, 47) is False

    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_b05_silent_source_rejected_for_audio_track(qtbot: Any) -> None:
    """B05：无声视频源不能替换音频轨目标（选中音频成员）。"""
    view = _view(qtbot)  # new：video 无 audio_info
    view.set_selection(_selection_with("ac1"))
    before = view.get_timeline()

    assert view.replace_clip_with_source("new", 0, 47) is False

    assert view.get_timeline() is before


def test_b05_subtitle_source_rejected(qtbot: Any) -> None:
    from src.features.edit.core.project import MediaAsset

    subtitle = MediaAsset(
        asset_id="subs", path="/tmp/s.srt", media_type="subtitle",
        display_name="subs", metadata={})
    view = _view(qtbot, assets={
        "old": _asset("old", frames=200), "subs": subtitle})
    view.set_selection(_selection_with("vc1"))

    assert view.replace_clip_with_source("subs", 0, 0) is False
    assert view.get_timeline().get_clip_by_id("vc1").asset_id == "old"


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


# ── B02：Voiceover 返回契约与边界分支 ────────────────────────────────


def _sequence_without_audio() -> TimelineSequence:
    video_clip = ClipInstance(
        clip_id="vc1", asset_id="old", track_id="v1",
        timeline_start=0, timeline_duration=48,
        source_start=0, source_duration=48,
    )
    return TimelineSequence(
        timeline_id="voiceover-b02", name="VO", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(video_clip,)),),
        audio_tracks=(),
    )


def _attach_no_audio(qtbot: Any, page: Any) -> Any:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    qtbot.addWidget(panel)
    panel.load_timeline(_sequence_without_audio())
    panel.get_timeline_view().set_project(EditProject(
        "p", "P", assets={"old": _asset("old", frames=200)}))
    page.set_timeline_panel(panel)
    return panel


def test_b02_creates_track_by_id_contract_and_starts(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B01 反例修复验收：无音频轨时按 track ID 契约消费，无异常。

    原缺陷：ensure_target_audio_track 返回 str 却被读 .track_id，
    AttributeError 后留下新建轨 + history 的半状态。
    """
    page = _page(qtbot, monkeypatch)
    panel = _attach_no_audio(qtbot, page)
    main = _FakeRecordingMain()
    page._main_window = main

    page._on_timeline_voiceover_requested()  # 原代码在此抛 AttributeError

    assert len(main.started) == 1
    created_id = main.started[0]
    view = panel.get_timeline_view()
    timeline = view.get_timeline()
    assert timeline.get_track_by_id(created_id) is not None
    assert timeline.get_track_by_id(created_id).track_type == "audio"
    # 建轨本身是一条可撤销命令（半状态已成完整可撤销事务）
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_track_by_id(created_id) is None


def test_b02_start_failure_rolls_back_created_track(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B02③：启动失败不留下不明轨——本次手势创建的轨被撤销。"""
    page = _page(qtbot, monkeypatch)
    panel = _attach_no_audio(qtbot, page)

    class _FailingMain(_FakeRecordingMain):
        def _start_recording(self, track_id: str) -> None:
            raise OSError("device unavailable")

    page._main_window = _FailingMain()
    with pytest.raises(OSError):
        page._on_timeline_voiceover_requested()
    view = panel.get_timeline_view()
    assert not view.get_timeline().audio_tracks
    assert not view._command_stack._undo_stack


def test_b02_locked_destination_track_refused(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """B02④：锁定的音频目的轨明确拒绝，不进入录音会话。"""
    page = _page(qtbot, monkeypatch)
    panel = _attach(qtbot, page)
    view = panel.get_timeline_view()
    locked = replace(panel.get_timeline_view().get_timeline()
                     .get_track_by_id("a1"), is_locked=True)
    view._timeline = view.get_timeline().with_track_updated(locked)
    main = _FakeRecordingMain()
    page._main_window = main

    page._on_timeline_voiceover_requested()

    assert main.started == []
    assert len(view._command_stack._undo_stack) == 0


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
