"""B10：时间线 Voiceover 完成事务（RecordingManager 替身模拟）。

链路：EditPage voiceover 入口（voiceover=True）→ controller 捕获
project/timeline/track/anchor/generation 会话 → take 完成 → asset 导入
（一次）+ 目标轨锚点落 clip（单步 undo）。守卫：迟到/换代、切 timeline、
删轨、锁轨、锚点冲突、取消 take——素材保留，零 clip 写入。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_ui


def _asset(asset_id: str, frames: int = 1000) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=f"/tmp/{asset_id}.wav", media_type="audio",
        display_name=asset_id,
        metadata={"audio_info": {"channels": 1, "sample_rate": 48000},
                  "total_frames": frames},
    )


def _timeline(*, locked: bool = False) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="vo", name="VO", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, is_locked=locked),),
    )


class _FakeTake:
    """TakeInfo 替身：覆盖 take_metadata_for_asset 消费的全部字段。"""

    def __init__(self, path: str, duration_seconds: float = 2.0) -> None:
        self.file_path = path
        self.take_id = "take-001"
        self.name = "take"
        self.take_number = 1
        self.scene_name = ""
        self.talent_name = ""
        self.status = "completed"
        self.duration_seconds = duration_seconds
        self.peak_dbfs = -6.0
        self.start_timecode = "00:00:00:00"
        self.time_reference_samples = 0
        self.in_point_frame = 0
        self.notes = ""
        self.channels = 1
        self.sample_rate = 48000


class _FakeManager:
    """RecordingManager 替身：捕获回调，由测试显式触发 take 完成。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.on_take_completed = None
        self.on_error = None
        self.on_level = None
        self.on_state_changed = None
        self.on_monitor_block = None
        self.enable_live_post_roll = False
        self.is_recording = False

    @staticmethod
    def list_input_devices():
        return []

    def start_recording(self, take_number=None, in_point_frame=None):
        return True

    def stop_recording(self):
        return None


class _FakeMediaPool:
    def __init__(self, project: EditProject) -> None:
        self._project = project

    def get_command_stack(self):
        return None  # 回退直接 import_asset 路径


class _FakeEditPage:
    def __init__(self, timeline: TimelineSequence) -> None:
        self._current_timeline = timeline


def _controller(qtbot: Any, timeline: TimelineSequence,
                project: EditProject) -> Any:
    from src.app.controllers.edit_controller import EditControllerMixin

    ctrl = EditControllerMixin.__new__(EditControllerMixin)
    ctrl._current_edit_project = project
    ctrl.media_pool_panel = _FakeMediaPool(project)
    ctrl.edit_page = _FakeEditPage(timeline)
    ctrl.timeline_panel = None  # 由 vo_env 用 _PanelStub(view) 覆盖
    ctrl.timeline_preview_renderer = None
    return ctrl


class _PanelStub:
    """最小 TimelinePanel 替身：提供 get_timeline_view。"""

    def __init__(self, view: Any) -> None:
        self.timeline_view = view
        self._view = view

    def get_timeline_view(self) -> Any:
        return self._view


@pytest.fixture()
def vo_env(qtbot, monkeypatch, tmp_path):
    """controller + 真实 TimelineView + 替身 manager 工厂。"""
    import src.features.edit.core.recording_manager as rm
    from src.features.edit.ui.timeline_view import TimelineView

    created: dict[str, Any] = {}

    def _factory(config: Any) -> _FakeManager:
        mgr = _FakeManager(config)
        created.setdefault("managers", []).append(mgr)
        return mgr

    monkeypatch.setattr(rm, "RecordingManager", _factory)

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline())
    project = EditProject("p1", "P1", assets={"asset": _asset("asset")})
    view.set_project(project)

    ctrl = _controller(qtbot, view.get_timeline(), project)
    ctrl.timeline_panel = _PanelStub(view)
    return ctrl, view, created, tmp_path


def _trigger_take(mgr: _FakeManager, path: str,
                  duration: float = 2.0) -> None:
    mgr.on_take_completed(_FakeTake(path, duration))


def test_b10_voiceover_places_clip_at_anchor_single_undo(
        vo_env, monkeypatch, tmp_path) -> None:
    """验收：take 完成 → 正确帧/轨落 clip，单步 undo，一次导入。"""
    ctrl, view, created, tmp = vo_env
    wav = tmp / "take1.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(
        "src.features.edit.core.project._probe_audio_info",
        lambda path: {"channels": 1, "sample_rate": 48000})

    ctrl._start_recording("a1", voiceover=True)
    assert ctrl._voiceover_sessions["a1"]["timeline_id"] == "vo"
    assert ctrl._voiceover_sessions["a1"]["anchor_frame"] == 0

    _trigger_take(created["managers"][0], str(wav), duration=2.0)

    timeline = view.get_timeline()
    project = ctrl._current_edit_project
    # 素材已导入（一次）
    assert project.get_asset_by_path(str(wav)) is not None
    # clip 落在 a1 @ 0，时长 2s×24fps=48
    audio_clips = timeline.audio_tracks[0].clips
    assert len(audio_clips) == 1
    clip = audio_clips[0]
    assert clip.timeline_start == 0
    assert clip.timeline_duration == 48
    assert clip.asset_id == project.get_asset_by_path(str(wav)).asset_id
    # 单步 undo 移除 clip
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().audio_tracks[0].clips == ()


def test_b10_session_binds_anchor_from_playhead(vo_env, monkeypatch,
                                                tmp_path) -> None:
    """锚点 = 会话捕获时播放头帧（非完成时）。"""
    ctrl, view, created, tmp = vo_env
    wav = tmp / "take2.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(
        "src.features.edit.core.project._probe_audio_info",
        lambda path: {"channels": 1, "sample_rate": 48000})

    def _context(*args, **kwargs):
        return (Rational(24, 1), 120)

    monkeypatch.setattr(type(ctrl), "_adr_timeline_context",
                        lambda self: (Rational(24, 1), 120))
    ctrl._start_recording("a1", voiceover=True)
    _trigger_take(created["managers"][0], str(wav))

    clip = view.get_timeline().audio_tracks[0].clips[0]
    assert clip.timeline_start == 120


def test_b10_switched_timeline_does_not_place(vo_env, monkeypatch,
                                              tmp_path) -> None:
    """录制中切 timeline：素材导入，不落 clip。"""
    ctrl, view, created, tmp = vo_env
    wav = tmp / "take3.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(
        "src.features.edit.core.project._probe_audio_info",
        lambda path: {"channels": 1, "sample_rate": 48000})
    ctrl._start_recording("a1", voiceover=True)

    other = TimelineSequence(
        timeline_id="other", name="Other", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),))
    ctrl.edit_page._current_timeline = other

    _trigger_take(created["managers"][0], str(wav))

    assert ctrl._current_edit_project.get_asset_by_path(str(wav)) is not None
    assert view.get_timeline().audio_tracks[0].clips == ()
    assert not view._command_stack._undo_stack


def test_b10_deleted_track_does_not_place(vo_env, monkeypatch,
                                          tmp_path) -> None:
    """录制中删轨：素材导入，不落 clip、不抛异常。"""
    ctrl, view, created, tmp = vo_env
    wav = tmp / "take4.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(
        "src.features.edit.core.project._probe_audio_info",
        lambda path: {"channels": 1, "sample_rate": 48000})
    ctrl._start_recording("a1", voiceover=True)

    timeline = view.get_timeline()
    timeline = timeline.with_track_updated(
        timeline.audio_tracks[0]._rebuild(clips=()))
    view.load_timeline(timeline.with_track_removed("a1"))

    _trigger_take(created["managers"][0], str(wav))

    assert ctrl._current_edit_project.get_asset_by_path(str(wav)) is not None
    assert not view._command_stack._undo_stack


def test_b10_late_take_after_new_session_does_not_place(
        vo_env, monkeypatch, tmp_path) -> None:
    """换代守卫：同轨新会话后，旧 take 迟到完成不落 clip。"""
    ctrl, view, created, tmp = vo_env
    wav1 = tmp / "take5a.wav"
    wav2 = tmp / "take5b.wav"
    wav1.write_bytes(b"RIFF")
    wav2.write_bytes(b"RIFF2")
    monkeypatch.setattr(
        "src.features.edit.core.project._probe_audio_info",
        lambda path: {"channels": 1, "sample_rate": 48000})
    ctrl._start_recording("a1", voiceover=True)
    first_mgr = created["managers"][0]
    # 同轨又开新 voiceover 会话（generation 递增）
    ctrl._start_recording("a1", voiceover=True)

    _trigger_take(first_mgr, str(wav1))

    assert view.get_timeline().audio_tracks[0].clips == ()
    assert ctrl._current_edit_project.get_asset_by_path(str(wav1)) is not None


def test_b10_cancelled_take_discards_session(vo_env) -> None:
    """取消/失败 take：会话被消费，后续完成不落 clip。"""
    ctrl, view, created, _ = vo_env
    ctrl._start_recording("a1", voiceover=True)
    mgr = created["managers"][0]
    take = _FakeTake("/tmp/x.wav")
    take.status = "aborted"
    mgr.on_take_completed(take)
    assert "a1" not in ctrl._voiceover_sessions


def test_b10_anchor_conflict_imports_without_clip(vo_env, monkeypatch,
                                                  tmp_path) -> None:
    """锚点区间被占：素材导入，clip 不覆盖既有内容。"""
    ctrl, view, created, tmp = vo_env
    wav = tmp / "take6.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(
        "src.features.edit.core.project._probe_audio_info",
        lambda path: {"channels": 1, "sample_rate": 48000})
    # 预置一个占据 [0, 100) 的 clip
    from src.features.edit.core.commands import AddClipCommand
    blocker = ClipInstance(
        "blk", "asset", "a1", 0, 100, 0, 100)
    view.execute_command(AddClipCommand.create("a1", blocker))
    ctrl._start_recording("a1", voiceover=True)

    _trigger_take(created["managers"][0], str(wav))

    timeline = view.get_timeline()
    assert len(timeline.audio_tracks[0].clips) == 1  # 只有 blocker
    assert ctrl._current_edit_project.get_asset_by_path(str(wav)) is not None


def test_b10_adr_material_collection_unchanged(vo_env, monkeypatch,
                                               tmp_path) -> None:
    """ADR/混音器素材采集路径（voiceover=False）：只导入，不落 clip。"""
    ctrl, view, created, tmp = vo_env
    wav = tmp / "take7.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(
        "src.features.edit.core.project._probe_audio_info",
        lambda path: {"channels": 1, "sample_rate": 48000})
    ctrl._start_recording("a1")  # 素材采集

    _trigger_take(created["managers"][0], str(wav))

    assert ctrl._current_edit_project.get_asset_by_path(str(wav)) is not None
    assert view.get_timeline().audio_tracks[0].clips == ()
    assert not hasattr(ctrl, "_voiceover_sessions") or not ctrl._voiceover_sessions
