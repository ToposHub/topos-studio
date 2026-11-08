"""R03：Voiceover 预卷/延迟时码落点。

反例（VO_PREROLL_ALIGNMENT）：take 文件**包含**已采集的预卷与输入
延迟 PCM，``_place_voiceover_clip`` 却固定 ``timeline_start=anchor、
source_start=0``——把预卷头对到锚点，锚点内容晚（预卷+延迟）帧
（24fps anchor 240、1s 预卷时锚点标记落 264）。

修复契约：保留预卷（素材不裁剪），clip 从文件首样本的真实时间线
帧位起放（``take.first_sample_frame``，未钳制可为负；退化用
``time_reference_samples>0`` 反推；都缺 → 锚点回退）；首样本在零点
前 → 裁掉零前内容、clip 从 0 起放。锚点采样误差 ≤ 1 帧。
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.recording_manager import (
    first_sample_timeline_frame,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


# ── 首样本帧位 helper（未钳制） ──────────────────────────────────────


def test_r03_first_sample_frame_helper_exact_and_negative():
    # 无预卷/延迟：首样本 = in-point
    assert first_sample_timeline_frame(100, 25.0) == 100.0
    # 预卷 2s + 20ms 延迟 @25fps → 前移 50.5 帧
    assert first_sample_timeline_frame(
        100, 25.0, pre_roll_captured_seconds=2.0, latency_ms=20.0
    ) == pytest.approx(49.5)
    # 未钳制：跨零点为负（1s 预卷 @24fps → -24）
    assert first_sample_timeline_frame(
        0, 24.0, pre_roll_captured_seconds=1.0) == pytest.approx(-24.0)
    # 负延迟按 0 处理；序列起始偏移参与
    assert first_sample_timeline_frame(
        0, 24.0, pre_roll_captured_seconds=1.0, latency_ms=-50.0,
    ) == pytest.approx(-24.0)
    assert first_sample_timeline_frame(
        0, 25.0, sequence_start_frame=3600 * 25) == 90000.0
    # 非法 fps：退回 in-point + 偏移
    assert first_sample_timeline_frame(40, 0.0) == 40.0


# ── 落 clip 环境 ─────────────────────────────────────────────────────


class _Take:
    """take 替身：R03 消费的字段（first_sample_frame 可为负）。"""

    def __init__(self, *, duration_seconds: float = 2.0,
                 first_sample_frame: int | None = None,
                 time_reference_samples: int = 0,
                 sample_rate: int = 48000) -> None:
        self.file_path = "/tmp/r03.wav"
        self.take_id = "take-r03"
        self.name = "r03"
        self.take_number = 1
        self.scene_name = ""
        self.talent_name = ""
        self.status = "completed"
        self.duration_seconds = duration_seconds
        self.peak_dbfs = -6.0
        self.start_timecode = "00:00:00:00"
        self.time_reference_samples = time_reference_samples
        self.in_point_frame = 0
        self.first_sample_frame = first_sample_frame
        self.notes = ""
        self.channels = 1
        self.sample_rate = sample_rate


class _PanelStub:
    def __init__(self, view: Any) -> None:
        self._view = view

    def get_timeline_view(self) -> Any:
        return self._view


class _FakeEditPage:
    def __init__(self, timeline: TimelineSequence) -> None:
        self._current_timeline = timeline


def _timeline(fps: Rational = Rational(24, 1),
              clips: tuple[ClipInstance, ...] = ()) -> TimelineSequence:
    return TimelineSequence(
        "vo", "VO", width=64, height=48, fps=fps,
        video_tracks=(EditTrack("v1", "V1", "video", 0),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=clips),),
    )


@pytest.fixture()
def env(qtbot):
    """controller 替身 + 真实 TimelineView（fps 24，a1 空轨）。"""
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    timeline = _timeline()
    view.load_timeline(timeline)
    project = EditProject("p1", "P1", assets={
        "vo-asset": MediaAsset(
            "vo-asset", "/tmp/r03.wav", "audio", "vo-asset",
            metadata={"sample_rate": 48000, "total_frames": 500})})
    view.set_project(project)

    ctrl = EditControllerMixin.__new__(EditControllerMixin)
    ctrl._current_edit_project = project
    ctrl.edit_page = _FakeEditPage(timeline)
    ctrl.timeline_panel = _PanelStub(view)
    ctrl.timeline_preview_renderer = None
    ctrl.status_bar = None
    ctrl._voiceover_sessions = {}
    return ctrl, view, project


def _session(anchor: int, generation: int = 1) -> dict:
    return {
        "project_id": "p1", "timeline_id": "vo", "track_id": "a1",
        "anchor_frame": anchor, "generation": generation,
    }


def _place(ctrl, view, session: dict, take: _Take) -> None:
    ctrl._voiceover_sessions["a1"] = session
    asset = ctrl._current_edit_project.get_asset_by_id("vo-asset")
    ctrl._place_voiceover_clip(session, asset, take)


def _placed_clip(view):
    clips = view.get_timeline().audio_tracks[0].clips
    return clips[0] if clips else None


def _anchor_sample(clip, anchor: int, fps: float, sr: int = 48000) -> float:
    """锚点帧经落位 clip 映射回文件采样（impulse 位置）。"""
    return (clip.source_start + (anchor - clip.timeline_start)) / fps * sr


# ── 落点矩阵 ─────────────────────────────────────────────────────────


def test_r03_no_preroll_places_at_anchor(env):
    ctrl, view, _ = env
    _place(ctrl, view, _session(anchor=240),
           _Take(first_sample_frame=240))
    clip = _placed_clip(view)
    assert clip is not None
    assert clip.timeline_start == 240
    assert clip.source_start == 0
    assert clip.timeline_duration == 48  # 2s × 24
    assert _anchor_sample(clip, 240, 24.0) == pytest.approx(0.0)


def test_r03_one_second_preroll_anchored_by_first_sample(env):
    """S 反例：1s 预卷 @24fps anchor240 → 首样本 216，clip 从 216 起放，
    锚点内容（impulse@240）映射到文件样本 48000 而不是 0。"""
    ctrl, view, _ = env
    _place(ctrl, view, _session(anchor=240),
           _Take(first_sample_frame=240 - 24))
    clip = _placed_clip(view)
    assert clip is not None
    assert clip.timeline_start == 216
    assert clip.source_start == 0
    assert clip.timeline_duration == 48
    assert _anchor_sample(clip, 240, 24.0) == pytest.approx(48000.0)


def test_r03_partial_preroll_uses_actual_capture(env):
    """设备打开吃掉部分预滚（实际并入 0.4s）→ 按实际量前移 10 帧。"""
    ctrl, view, _ = env
    _place(ctrl, view, _session(anchor=240),
           _Take(first_sample_frame=240 - round(0.4 * 24)))
    clip = _placed_clip(view)
    assert clip.timeline_start == 230


def test_r03_latency_extends_lead(env):
    """1s 预卷 + 50ms 输入延迟 → 前移 24 + round(1.2) = 25 帧。"""
    ctrl, view, _ = env
    _place(ctrl, view, _session(anchor=240),
           _Take(first_sample_frame=240 - 24 - round(0.05 * 24)))
    clip = _placed_clip(view)
    assert clip.timeline_start == 215


def test_r03_anchor_near_zero_crops_head(env):
    """anchor 5、首样本 -19：clip 从 0 起放、source_start=19 裁掉零前
    内容，锚点 impulse 映射 = 19+5 = 24 帧 = 锚点-首样本。"""
    ctrl, view, _ = env
    _place(ctrl, view, _session(anchor=5),
           _Take(first_sample_frame=-19))
    clip = _placed_clip(view)
    assert clip is not None
    assert clip.timeline_start == 0
    assert clip.source_start == 19
    assert clip.timeline_duration == 48 - 19
    assert _anchor_sample(clip, 5, 24.0) == pytest.approx(24 / 24.0 * 48000)


def test_r03_entirely_before_timeline_skips_clip(env):
    """内容全部在零点前（首样本 -70、总长 48）→ 不落 clip，素材不丢。"""
    ctrl, view, project = env
    _place(ctrl, view, _session(anchor=2),
           _Take(first_sample_frame=-70))
    assert _placed_clip(view) is None
    assert not view._command_stack._undo_stack
    assert project.get_asset_by_id("vo-asset") is not None


def test_r03_drop_frame_fps_impulse_within_one_frame(qtbot):
    """29.97fps：存储帧位四舍五入 ≤0.5 帧，锚点采样误差 ≤ 1 帧采样数。"""
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    fps = Rational(30000, 1001)
    view.load_timeline(_timeline(fps=fps))
    project = EditProject("p1", "P1", assets={
        "vo-asset": MediaAsset(
            "vo-asset", "/tmp/r03.wav", "audio", "vo-asset",
            metadata={"sample_rate": 48000, "total_frames": 500})})
    view.set_project(project)
    ctrl = EditControllerMixin.__new__(EditControllerMixin)
    ctrl._current_edit_project = project
    ctrl.edit_page = _FakeEditPage(view.get_timeline())
    ctrl.timeline_panel = _PanelStub(view)
    ctrl.timeline_preview_renderer = None
    ctrl.status_bar = None
    ctrl._voiceover_sessions = {}

    fps_value = 30000.0 / 1001.0
    anchor = 601
    true_first = anchor - 1.0 * fps_value  # 连续真值 ≈ 570.97
    take = _Take(first_sample_frame=int(round(true_first)),
                 sample_rate=96000)
    _place(ctrl, view, _session(anchor=anchor), take)

    clip = _placed_clip(view)
    assert clip is not None
    # impulse 真实文件位置（96kHz）：anchor 相对连续首样本的秒数
    true_impulse = (anchor - true_first) / fps_value * 96000
    tolerance = math.ceil(96000 / fps_value)  # 1 帧的采样数
    assert abs(_anchor_sample(clip, anchor, fps_value, sr=96000)
               - true_impulse) <= tolerance


def test_r03_fallback_derives_from_time_reference(env):
    """旧字段回退：first_sample_frame 缺失但 time_reference>0 → 反推。"""
    ctrl, view, _ = env
    # 216 帧 @24fps、48kHz → 432000 采样
    _place(ctrl, view, _session(anchor=240),
           _Take(time_reference_samples=432000))
    clip = _placed_clip(view)
    assert clip is not None
    assert clip.timeline_start == 216


def test_r03_fallback_legacy_anchor_when_no_timebase(env):
    """完全无时间基准：维持锚点对齐（旧行为不回归）。"""
    ctrl, view, _ = env
    _place(ctrl, view, _session(anchor=240), _Take())
    clip = _placed_clip(view)
    assert clip is not None
    assert clip.timeline_start == 240
    assert clip.source_start == 0


def test_r03_occupied_check_uses_real_range(env, qtbot):
    """冲突检查按真实落位区间（含预卷头），不是旧的锚点区间。"""
    ctrl, view, _ = env
    blocker = ClipInstance("block", "vo-asset", "a1", 220, 10, 0, 10)
    view.load_timeline(_timeline(clips=(blocker,)))
    # [216, 264) 与 blocker [220,230) 冲突 → 跳过（旧检查看
    # [240, 288) 会漏过 220-230 的占用）
    _place(ctrl, view, _session(anchor=240),
           _Take(first_sample_frame=216))
    placed = [c for c in view.get_timeline().audio_tracks[0].clips
              if c.clip_id != "block"]
    assert placed == []


def test_r03_undo_removes_clip_keeps_asset(env):
    ctrl, view, project = env
    _place(ctrl, view, _session(anchor=240),
           _Take(first_sample_frame=216))
    clip = _placed_clip(view)
    assert clip is not None
    view.undo()
    assert view.get_timeline().audio_tracks[0].clips == ()
    # 原录音素材仍在工程（undo 只移除 clip，不删 asset）
    assert project.get_asset_by_id(clip.asset_id) is not None
