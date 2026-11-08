"""B14：高级速度 draft 接收链——契约/能力/单事务执行/明确诊断。

链路：view 对话框高级意图 → EditPage 接收 → EditController 编排 →
``speed_settings.plan_speed_settings_edit``（版本、类型、枚举、Speed×
Duration 一致性、能力矩阵）→ ``SetClipSpeedSettingsCommand`` 单事务
提交（单步 undo）。不支持/校验失败零写入 + 诊断。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.speed_settings import (
    SPEED_SETTINGS_VERSION,
    normalize_settings,
    plan_speed_settings_edit,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(**kwargs: Any) -> ClipInstance:
    speed = kwargs.pop("speed", SpeedParams(speed=1.0))
    animated = kwargs.pop("animated", False)
    clip = ClipInstance(
        kwargs.pop("clip_id", "v"), "asset",
        kwargs.pop("track_id", "v1"), 0,
        kwargs.pop("duration", 100), 0,
        kwargs.pop("source_duration", 100), speed=speed)
    if animated:
        clip = clip.with_keyframe_added("opacity", 50, 0.5)
    return clip


def _timeline(clip: ClipInstance,
              partner: ClipInstance | None = None,
              follower: ClipInstance | None = None) -> TimelineSequence:
    video_clips = (clip,)
    if follower is not None:
        video_clips = video_clips + (follower,)
    audio_clips = (partner,) if partner is not None else ()
    return TimelineSequence(
        "b14", "B14", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=video_clips),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=audio_clips),),
    )


def _settings(**overrides: Any) -> dict[str, Any]:
    base = {
        "speed": 2.0, "reverse": False, "freeze": False,
        "preserve_pitch": False, "ripple": False,
        "keyframe_timing": "stretch", "duration_frames": 50,
    }
    base.update(overrides)
    return base


# ── 契约校验 ─────────────────────────────────────────────────────────


def test_b14_normalize_rejects_bad_contract():
    with pytest.raises(ValueError):
        normalize_settings({"version": 2, "speed": 1.0,
                            "duration_frames": 10})
    with pytest.raises(ValueError):
        normalize_settings(_settings(speed="fast"))
    with pytest.raises(ValueError):
        normalize_settings(_settings(speed=0.01))
    with pytest.raises(ValueError):
        normalize_settings(_settings(duration_frames=0))
    with pytest.raises(ValueError):
        normalize_settings(_settings(keyframe_timing="hold"))
    with pytest.raises(ValueError):
        normalize_settings(_settings(ripple="yes"))
    ok = normalize_settings(_settings())
    assert ok["version"] == SPEED_SETTINGS_VERSION
    assert ok["operation"] == "dialog"


def test_b14_speed_duration_consistency_enforced():
    """Speed 与 Duration 表达同一意图：2x×100 源帧=50；请求 40 拒绝。"""
    timeline = _timeline(_clip())
    plan = plan_speed_settings_edit(
        timeline, "v1", "v", _settings(duration_frames=40))
    assert not plan.executable and plan.error
    assert "implies 50 frames" in plan.error
    consistent = plan_speed_settings_edit(
        timeline, "v1", "v", _settings(duration_frames=50))
    assert consistent.executable


def test_b14_capability_matrix_rejects_curves_and_frozen():
    curved = _clip(speed=SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0),)))
    plan = plan_speed_settings_edit(
        _timeline(curved), "v1", "v", _settings())
    assert plan.unsupported and "Speed-point curves" in plan.unsupported

    frozen = _clip()
    frozen = frozen.with_freeze_frame(5)
    plan = plan_speed_settings_edit(
        _timeline(frozen), "v1", "v", _settings())
    assert plan.unsupported and "already frozen" in plan.unsupported


def test_b14_missing_clip_returns_error():
    plan = plan_speed_settings_edit(
        _timeline(_clip()), "v1", "ghost", _settings())
    assert plan.error and "not found" in plan.error.lower()


# ── 单事务执行 ───────────────────────────────────────────────────────


def test_b14_basic_settings_single_undo():
    timeline = _timeline(_clip())
    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())
    stack = CommandStack()
    changed = stack.execute(plan.command, timeline)
    clip = changed.get_clip_by_id("v")
    assert clip.timeline_duration == 50
    assert clip.speed.speed == 2.0
    assert len(stack._undo_stack) == 1
    undone = stack.undo(changed)
    assert undone.get_clip_by_id("v").timeline_duration == 100
    assert undone.get_clip_by_id("v").speed.speed == 1.0


def test_b14_ripple_shifts_followers_both_directions():
    follower = ClipInstance(
        "f", "asset", "v1", 100, 40, 0, 40)
    timeline = _timeline(_clip(), follower=follower)
    plan = plan_speed_settings_edit(
        timeline, "v1", "v", _settings(ripple=True))

    changed = plan.command.apply(timeline)

    # 缩短 50 帧 → 后继左移闭合间隙
    assert changed.get_clip_by_id("v").timeline_duration == 50
    assert changed.get_clip_by_id("f").timeline_start == 50
    undone = plan.command.undo(changed)
    assert undone.get_clip_by_id("f").timeline_start == 100
    assert undone.get_clip_by_id("v").timeline_duration == 100

    # 变长 0.5x → 后继右移让位
    plan2 = plan_speed_settings_edit(
        timeline, "v1", "v",
        _settings(speed=0.5, duration_frames=200, ripple=True))
    changed2 = plan2.command.apply(timeline)
    assert changed2.get_clip_by_id("f").timeline_start == 200


def test_b14_freeze_sets_source_in_and_requested_duration():
    timeline = _timeline(_clip())
    plan = plan_speed_settings_edit(
        timeline, "v1", "v", _settings(freeze=True, duration_frames=70))
    assert plan.executable
    changed = plan.command.apply(timeline)
    clip = changed.get_clip_by_id("v")
    assert clip.freeze_source_frame == clip.source_start
    assert clip.timeline_duration == 70
    assert clip.timeline_to_source_frame(0) == clip.source_start
    assert clip.timeline_to_source_frame(69) == clip.source_start
    undone = plan.command.undo(changed)
    assert undone.get_clip_by_id("v").freeze_source_frame is None
    assert undone.get_clip_by_id("v").timeline_duration == 100


def test_b14_keyframe_timing_maintain_and_stretch():
    stretched = plan_speed_settings_edit(
        _timeline(_clip(animated=True)), "v1", "v", _settings())
    changed = stretched.command.apply(
        _timeline(_clip(animated=True)))
    key = changed.get_clip_by_id("v").animated_params[
        "opacity"].keyframes.keyframes[0]
    # stretch：50/100 → key 50→25
    assert key.frame == 25

    maintained = plan_speed_settings_edit(
        _timeline(_clip(animated=True)), "v1", "v",
        _settings(keyframe_timing="maintain"))
    changed2 = maintained.command.apply(
        _timeline(_clip(animated=True)))
    key2 = changed2.get_clip_by_id("v").animated_params[
        "opacity"].keyframes.keyframes[0]
    # maintain：关键帧保持绝对帧位
    assert key2.frame == 50


def test_b14_linked_partner_included_in_transaction():
    clip = replace(_clip(), linked_clip_id="a")
    partner = replace(
        _clip(clip_id="a", track_id="a1"), linked_clip_id="v")
    timeline = _timeline(clip, partner=partner)
    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())

    changed = plan.command.apply(timeline)

    assert changed.get_clip_by_id("a").timeline_duration == 50
    assert changed.get_clip_by_id("a").speed.speed == 2.0
    undone = plan.command.undo(changed)
    assert undone.get_clip_by_id("a").timeline_duration == 100


def test_b14_noop_settings_identity():
    timeline = _timeline(_clip())
    plan = plan_speed_settings_edit(
        timeline, "v1", "v",
        _settings(speed=1.0, duration_frames=100))
    assert plan.command.apply(timeline) is timeline


# ── 接收链（EditPage→Controller 编排） ─────────────────────────────


class _ReceptionHarness:
    """最小宿主：EditPage + panel + 真 view + controller 编排。"""

    def __init__(self, qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.edit.core.project import EditProject, MediaAsset
        from src.features.edit.ui.edit_page import EditPage
        from src.features.edit.ui.timeline_panel import TimelinePanel
        from src.app.controllers.edit_controller import EditControllerMixin

        monkeypatch.setattr(
            EditPage, "_ensure_audio_manager", lambda self: None)
        page = EditPage()
        qtbot.addWidget(page)
        panel = TimelinePanel()
        qtbot.addWidget(panel)
        panel.load_timeline(_timeline(_clip()))
        asset = MediaAsset(
            "asset", "/tmp/a.mp4", "video", "asset",
            metadata={"fps": 24.0, "total_frames": 1000})
        panel.get_timeline_view().set_project(
            EditProject("p", "P", assets={"asset": asset}))
        page.set_timeline_panel(panel)

        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        ctrl.timeline_panel = panel
        self.page, self.panel, self.ctrl = page, panel, ctrl
        self.view = panel.get_timeline_view()

    def requests(self) -> list:
        return self.page._panel_request_connections and []


def test_b14_reception_chain_commits_ripple_through_host(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _ReceptionHarness(qtbot, monkeypatch)
    before = harness.view.get_timeline()

    # 宿主缺编排入口时：请求→明确反馈，零写入
    class _OldHost:
        pass
    harness.page._main_window = _OldHost()
    requests: list[tuple] = []
    harness.view.clip_speed_settings_requested.connect(
        lambda *args: requests.append(args))
    harness.page._on_timeline_clip_speed_settings("v", "v1", _settings(
        ripple=True))
    assert requests == []
    assert harness.view.get_timeline() is before

    # 宿主有编排入口：ripple 单事务提交
    class _Host:
        apply_clip_speed_settings = harness.ctrl.apply_clip_speed_settings
        timeline_panel = harness.panel
        status_bar = None
    harness.page._main_window = _Host.__new__(_Host)
    harness.page._main_window.timeline_panel = harness.panel
    ok = harness.ctrl.apply_clip_speed_settings(
        "v", "v1", _settings(ripple=True))
    assert ok is True
    assert harness.view.get_timeline().get_clip_by_id("v").timeline_duration == 50
    assert len(harness.view._command_stack._undo_stack) == 1


def test_b14_reception_chain_diagnoses_unsupported(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _ReceptionHarness(qtbot, monkeypatch)
    curved = _clip(speed=SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0),)))
    harness.view.load_timeline(_timeline(curved))
    before = harness.view.get_timeline()

    ok = harness.ctrl.apply_clip_speed_settings(
        "v", "v1", _settings())

    assert ok is False
    assert harness.view.get_timeline() is before
    assert not harness.view._command_stack._undo_stack


def test_b14_view_clear_point_menu_action(qtbot: Any) -> None:
    """stage-3：单点删除并入前段速度，不动其它点。"""
    from src.features.edit.ui.timeline_view import TimelineView

    curved = _clip(speed=SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0),
        SpeedPoint(source_offset=60, speed=0.5),
    )))
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline(curved))

    view._on_retime_action_requested("v", "v1", "clear_point", 16, None)

    params = view.get_timeline().get_clip_by_id("v").speed
    assert [p.source_offset for p in params.speed_points] == [60]
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    restored = view.get_timeline().get_clip_by_id("v").speed
    assert [p.source_offset for p in restored.speed_points] == [16, 60]


def test_b14_view_clear_first_boundary_rejected(qtbot: Any) -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    curved = _clip(speed=SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0),)))
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline(curved))
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    before = view.get_timeline()

    view._on_retime_action_requested("v", "v1", "clear_point", 0, None)

    assert messages
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack
