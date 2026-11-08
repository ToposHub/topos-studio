"""R09：两端独立淡入/淡出曲线——模型、命令、消费者、UI 入口。

缺口（复审 R09）：B16 是一个 ``fade_curve`` 两端共用且无
``SetFadeCurveCommand`` 的 UI 消费者——不是 Resolve 的两端独立曲线。

契约：

- 模型：``fade_curve_in/out`` 独立字段（None → 回落共享 ``fade_curve``
  → legacy 默认），In smooth / Out linear 可共存；
- 命令：``SetFadeEndCurveCommand`` 单端修改不触碰另一端与共享字段；
  undo 含未支持原值（R05 语义）；无传播到互链伙伴（淡化是 clip 级）；
- 消费者：视频 render plan / 音频 apply_clip_fade / 实时快照签名 /
  导出字典按**各自生效曲线**取值（预览=实时=导出）；
- UI：片段右键菜单 Fade Curve → In/Out 子菜单提交命令（锁轨禁用）；
- legacy：旧工程（全 None）视频 linear / 音频 equal_power 逐帧逐样本
  不变；自由 Bezier 明确不提供。
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import apply_clip_fade
from src.features.edit.core.commands import (
    MacroCommand,
    SetFadeEndCurveCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(**kwargs: Any) -> ClipInstance:
    base = dict(
        clip_id="c1", asset_id="asset", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
        fade_in_duration=10, fade_out_duration=10,
    )
    base.update(kwargs)
    return ClipInstance(**base)


def _timeline(clip: ClipInstance, track_id: str = "v1",
              track_type: str = "video") -> TimelineSequence:
    track = EditTrack(track_id, "T", track_type, 0, clips=(clip,))
    if track_type == "video":
        return TimelineSequence(
            "r09", "R09", width=64, height=48, fps=Rational(24, 1),
            video_tracks=(track,), audio_tracks=())
    return TimelineSequence(
        "r09", "R09", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(), audio_tracks=(track,))


# ── 模型：独立字段 + 生效回落 ───────────────────────────────────────


def test_r09_model_independent_ends_and_fallback():
    clip = _clip(fade_curve_in="smooth", fade_curve_out="linear")
    assert clip.effective_fade_curve_in == "smooth"
    assert clip.effective_fade_curve_out == "linear"
    # 回落：端字段 None → 共享 fade_curve（B16 旧字段）
    shared = _clip(fade_curve="equal_power")
    assert shared.effective_fade_curve_in == "equal_power"
    assert shared.effective_fade_curve_out == "equal_power"
    # 端字段覆盖共享
    override = _clip(
        fade_curve="equal_power", fade_curve_in="linear")
    assert override.effective_fade_curve_in == "linear"
    assert override.effective_fade_curve_out == "equal_power"
    # 全 None = legacy
    assert _clip().effective_fade_curve_in is None
    assert _clip().effective_fade_curve_out is None
    # 构造校验
    with pytest.raises(ValueError):
        _clip(fade_curve_in="bezier")
    with pytest.raises(ValueError):
        _clip(fade_curve_out="scurve")


def test_r09_model_roundtrip_and_legacy_bytes():
    clip = _clip(fade_curve_in="smooth", fade_curve_out="linear")
    data = clip.to_dict()
    assert data["fade_curve_in"] == "smooth"
    assert data["fade_curve_out"] == "linear"
    revived = ClipInstance.from_dict(json.loads(json.dumps(data)))
    assert revived.fade_curve_in == "smooth"
    assert revived.fade_curve_out == "linear"
    # 未知原值两端各自保留（R05 语义）
    raw = ClipInstance.from_dict(
        _clip().to_dict() | {
            "fade_curve_in": "v4_in", "fade_curve_out": "v4_out"})
    assert raw.fade_curve_in is None
    assert raw.fade_end_curve_raw("in") == "v4_in"
    assert raw.fade_end_curve_raw("out") == "v4_out"
    assert raw.to_dict()["fade_curve_in"] == "v4_in"
    assert raw.to_dict()["fade_curve_out"] == "v4_out"


# ── 命令：单端修改/undo/no-op ───────────────────────────────────────


def test_r09_command_single_end_preserves_other():
    timeline = _timeline(_clip(fade_curve="equal_power"))
    cmd = SetFadeEndCurveCommand.create("c1", "v1", "in", "smooth")
    changed = cmd.apply(timeline)
    clip = changed.get_clip_by_id("c1")
    assert clip.fade_curve_in == "smooth"
    assert clip.fade_curve_out is None            # 另一端不动
    assert clip.fade_curve == "equal_power"       # 共享字段不动
    assert clip.effective_fade_curve_in == "smooth"
    assert clip.effective_fade_curve_out == "equal_power"

    undone = cmd.undo(changed)
    assert undone.get_clip_by_id("c1").fade_curve_in is None
    assert undone.get_clip_by_id("c1").fade_curve == "equal_power"


def test_r09_command_noop_reset_and_errors():
    timeline = _timeline(_clip(fade_curve_in="smooth"))
    # 同值 no-op：同一实例（不入历史）
    same = SetFadeEndCurveCommand.create("c1", "v1", "in", "smooth")
    assert same.apply(timeline) is timeline
    # 显式重置 None 是真修改（清除端字段）
    reset = SetFadeEndCurveCommand.create("c1", "v1", "in", None)
    assert reset.apply(timeline) is not timeline
    # 白名单外拒绝
    with pytest.raises(ValueError):
        SetFadeEndCurveCommand.create(
            "c1", "v1", "in", "bezier").apply(timeline)
    with pytest.raises(ValueError):
        SetFadeEndCurveCommand.create(
            "c1", "v1", "both", "linear").apply(timeline)


def test_r09_command_undo_restores_raw_and_serializes():
    timeline = _timeline(ClipInstance.from_dict(
        _clip().to_dict() | {"fade_curve_out": "future_out"}))
    cmd = SetFadeEndCurveCommand.create("c1", "v1", "out", "linear")
    changed = cmd.apply(timeline)
    assert changed.get_clip_by_id("c1").fade_curve_out == "linear"

    revived = SetFadeEndCurveCommand.from_dict(
        json.loads(json.dumps(cmd.to_dict())))
    undone = revived.undo(changed)
    clip = undone.get_clip_by_id("c1")
    assert clip.fade_curve_out is None
    assert clip.fade_end_curve_raw("out") == "future_out"
    assert clip.to_dict()["fade_curve_out"] == "future_out"


def test_r09_command_does_not_propagate_to_linked_partner():
    from dataclasses import replace as _r
    main = _r(_clip(), linked_clip_id="a")
    partner = _r(_clip(clip_id="a", track_id="a1"), linked_clip_id="c1")
    timeline = TimelineSequence(
        "r09", "R09", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(main,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(partner,)),),
    )
    changed = SetFadeEndCurveCommand.create(
        "c1", "v1", "in", "smooth").apply(timeline)
    # 淡化是 clip 级：伙伴两端与共享字段都不动（音频有自己的默认曲线）
    partner_after = changed.get_clip_by_id("a")
    assert partner_after.fade_curve_in is None
    assert partner_after.fade_curve is None
    assert partner_after.effective_fade_curve_in is None


# ── 消费者：预览=实时=导出 ──────────────────────────────────────────


def _video_opacity(clip: ClipInstance, frame: int) -> float:
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan

    plan = TimelineRenderPlan(_timeline(clip))
    layers = plan.get_frame_plan(frame).layers
    assert layers
    return float(layers[0].opacity)


def test_r09_video_in_smooth_out_linear_coexist():
    """In smooth / Out linear 共存：淡入 1/4 处 smooth≈0.216 ≠ linear 0.3；
    淡出方向进度 = 距末端比例（frame 93 距末端 7/10 → 70% 剩余不透明）。"""
    both = _clip(fade_curve_in="smooth", fade_curve_out="linear")
    # 淡入 1/4 处（frame 3/10）：smooth 0.216
    assert _video_opacity(both, 3) == pytest.approx(0.216)
    # 淡出（frame 93，距末端 7/10）：linear 剩余 0.7
    assert _video_opacity(both, 93) == pytest.approx(0.7)
    # 反过来：in linear / out smooth
    flipped = _clip(fade_curve_in="linear", fade_curve_out="smooth")
    assert _video_opacity(flipped, 3) == pytest.approx(0.3)
    # smoothstep(0.7) = 3×0.49 − 2×0.343 = 0.784
    assert _video_opacity(flipped, 93) == pytest.approx(0.784)


def test_r09_audio_envelope_per_end():
    """音频包络逐端采样：淡入 equal_power、淡出 linear 同块共存。"""
    block = np.ones((2, 480), dtype=np.float64)
    out = apply_clip_fade(
        block,
        clip_timeline_start=0, clip_timeline_duration=100,
        current_frame=0,
        fade_in_frames=24, fade_out_frames=24,
        fps=24.0, sample_rate=48000.0,
        fade_curve_in="equal_power", fade_curve_out="linear")
    fi = 24 * 2000.0  # 淡入样本数
    t = 100 / (fi - 1.0)
    # 头部按 equal_power 淡入
    assert out[0, 100] == pytest.approx(math.sin(math.pi / 2 * t))
    assert out[0, 100] > t  # equal_power 高于 linear
    # 尾部块（frame 99）：块起点 198000，淡出起点 152000、长度 48000
    tail = apply_clip_fade(
        block, clip_timeline_start=0, clip_timeline_duration=100,
        current_frame=99,
        fade_in_frames=24, fade_out_frames=24,
        fps=24.0, sample_rate=48000.0,
        fade_curve_in="equal_power", fade_curve_out="linear",
    )
    # linear 镜像：gain = 1 − (pos − out_start)/(48000 − 1)
    t0 = (198000 - 152000) / (48000 - 1)
    assert tail[0, 0] == pytest.approx(1.0 - t0)
    assert np.all(np.diff(tail[0]) < 0)  # linear 单调下降


def test_r09_snapshot_hash_per_end_independent():
    from src.features.edit.core.audio_graph_snapshot import (
        build_audio_graph_snapshot,
    )

    def _audio(clip: ClipInstance) -> Any:
        return build_audio_graph_snapshot(
            _timeline(clip, "a1", "audio"))

    base = _audio(_clip(clip_id="ac", track_id="a1"))
    in_changed = _audio(_clip(
        clip_id="ac", track_id="a1", fade_curve_in="smooth"))
    out_changed = _audio(_clip(
        clip_id="ac", track_id="a1", fade_curve_out="smooth"))
    assert base.stable_hash != in_changed.stable_hash
    assert base.stable_hash != out_changed.stable_hash
    assert in_changed.stable_hash != out_changed.stable_hash
    assert in_changed.clips[0].fade_curve_in == "smooth"


def test_r09_legacy_defaults_unchanged():
    """全 None：视频 linear、音频 equal_power（B16 契约不回归）。"""
    legacy = _clip()
    assert _video_opacity(legacy, 3) == pytest.approx(0.3)  # linear
    block = np.ones((1, 480), dtype=np.float64)
    out = apply_clip_fade(
        block, clip_timeline_start=0, clip_timeline_duration=100,
        current_frame=0, fade_in_frames=24, fade_out_frames=24,
        fps=24.0, sample_rate=48000.0)
    fi = 24 * 2000.0
    t = 100 / (fi - 1.0)
    assert out[0, 100] == pytest.approx(math.sin(math.pi / 2 * t))


# ── UI：菜单 → 单命令提交 ───────────────────────────────────────────


def test_r09_view_menu_action_commits_single_undo(qtbot):
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline(_clip()))

    view._on_fade_end_curve_requested("c1", "v1", "in", "smooth")

    clip = view.get_timeline().get_clip_by_id("c1")
    assert clip.fade_curve_in == "smooth"
    assert clip.fade_curve_out is None
    assert clip.fade_curve is None
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_clip_by_id("c1").fade_curve_in is None


def test_r09_view_menu_locked_track_zero_write(qtbot):
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(TimelineSequence(
        "r09", "R09", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(_clip(),), is_locked=True),),
        audio_tracks=()))
    before = view.get_timeline()

    view._on_fade_end_curve_requested("c1", "v1", "out", "linear")

    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_r09_macro_single_undo_roundtrip():
    timeline = _timeline(_clip())
    cmd = SetFadeEndCurveCommand.create("c1", "v1", "in", "smooth")
    macro = MacroCommand(command_id="m", label="fade", sub_commands=(cmd,))
    changed, _updated = macro.apply(timeline)
    assert changed.get_clip_by_id("c1").fade_curve_in == "smooth"
    undone = macro.undo(changed)
    assert undone.get_clip_by_id("c1").fade_curve_in is None
