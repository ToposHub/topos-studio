"""B16：FadeCurve 模型 + 视听统一消费者。

模型：``ClipInstance.fade_curve``（None=legacy：视频 linear/音频
equal_power，旧结果逐帧逐样本不变）；白名单 linear|equal_power|
smooth；序列化非默认才写出，未知值清洗不阻断加载。
命令：``SetFadeCurveCommand``（快照 undo、no-op 同一实例、白名单拒绝）。
消费者：``fade_curve_factor``（视频 render plan 同式）与
``apply_clip_fade``（实时/离线/Stem 共用）。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import (
    apply_clip_fade,
    fade_curve_factor,
)
from src.features.edit.core.commands import SetFadeCurveCommand
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _timeline(clip: ClipInstance) -> TimelineSequence:
    return TimelineSequence(
        "fade-curve", "FadeCurve", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


def _clip(**kwargs: Any) -> ClipInstance:
    return ClipInstance(
        "c1", "asset", "v1", 0, 100, 0, 100,
        fade_in_duration=kwargs.get("fade_in", 10),
        fade_out_duration=kwargs.get("fade_out", 10),
        fade_curve=kwargs.get("fade_curve"))


# ── 模型与序列化 ─────────────────────────────────────────────────────


def test_b16_model_field_validation_and_roundtrip():
    clip = _clip(fade_curve="smooth")
    assert clip.fade_curve == "smooth"
    data = clip.to_dict()
    assert data["fade_curve"] == "smooth"
    revived = ClipInstance.from_dict(data)
    assert revived.fade_curve == "smooth"
    # legacy：默认 None 不写出，缺键读 None
    legacy = _clip()
    assert "fade_curve" not in legacy.to_dict() or \
        legacy.to_dict()["fade_curve"] is None
    assert ClipInstance.from_dict(
        {**legacy.to_dict()}).fade_curve is None
    # 未知/损坏值清洗为 None（不阻断加载），构造路径拒绝新写
    assert ClipInstance.from_dict({
        **legacy.to_dict(), "fade_curve": "future_shape"}).fade_curve is None
    with pytest.raises(ValueError):
        _clip(fade_curve="exponential")


def test_b16_command_apply_undo_and_noop():
    timeline = _timeline(_clip())
    cmd = SetFadeCurveCommand.create("c1", "v1", "equal_power")
    changed = cmd.apply(timeline)
    assert changed.get_clip_by_id("c1").fade_curve == "equal_power"
    undone = cmd.undo(changed)
    assert undone.get_clip_by_id("c1").fade_curve is None

    # no-op：同值 → 同一实例（不入历史）
    same = SetFadeCurveCommand.create("c1", "v1", None)
    assert same.apply(timeline) is timeline
    # 白名单外拒绝
    with pytest.raises(ValueError):
        SetFadeCurveCommand.create("c1", "v1", "scurve").apply(timeline)
    # 序列化往返（含 _previous）
    cmd2 = SetFadeCurveCommand.create("c1", "v1", "smooth")
    cmd2.apply(timeline)
    revived = SetFadeCurveCommand.from_dict(cmd2.to_dict())
    undone2 = revived.undo(_timeline(_clip(fade_curve="smooth")))
    assert undone2.get_clip_by_id("c1").fade_curve is None


# ── 曲线取值（视频 render plan 同式） ────────────────────────────────


def test_b16_curve_values():
    assert fade_curve_factor(None, 0.5) == pytest.approx(0.5)  # 视频默认
    assert fade_curve_factor("linear", 0.25) == pytest.approx(0.25)
    assert fade_curve_factor("equal_power", 0.5) == pytest.approx(
        math.sin(math.pi / 4))
    assert fade_curve_factor("smooth", 0.5) == pytest.approx(0.5)
    assert fade_curve_factor("smooth", 0.25) == pytest.approx(
        3 * 0.0625 - 2 * 0.015625)
    # 端点
    for shape in (None, "linear", "equal_power", "smooth"):
        assert fade_curve_factor(shape, 0.0) == pytest.approx(0.0)
        assert fade_curve_factor(shape, 1.0) == pytest.approx(1.0)


def test_b16_video_render_plan_uses_curve():
    """fade_curve=None 视频逐帧线性（旧结果）；smooth 中点值改变。"""
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan

    base = _clip(fade_in=10, fade_out=0)

    def _opacity_at(clip: ClipInstance, frame: int) -> float:
        plan = TimelineRenderPlan(_timeline(clip))
        frame_plan = plan.get_frame_plan(frame)
        layers = frame_plan.layers if hasattr(frame_plan, "layers") else ()
        assert layers, "expected a clip layer at frame"
        return float(layers[0].opacity)

    assert _opacity_at(base, 5) == pytest.approx(0.5)  # legacy 线性不变
    curved = _clip(fade_in=10, fade_out=0, fade_curve="smooth")
    assert _opacity_at(curved, 5) == pytest.approx(0.5)  # smoothstep 过中点
    # 1/4 处可区分：linear 0.3 vs smooth 0.216
    assert _opacity_at(base, 3) == pytest.approx(0.3)
    assert _opacity_at(curved, 3) == pytest.approx(0.216)


# ── 音频消费者（实时/离线/Stem 共用 apply_clip_fade） ────────────────


def _fade_block(fade_curve: str | None, samples: int = 480,
                 current_frame: int = 0):
    block = np.ones((2, samples), dtype=np.float64)
    return apply_clip_fade(
        block,
        clip_timeline_start=0, clip_timeline_duration=100,
        current_frame=current_frame,
        fade_in_frames=24, fade_out_frames=24,
        fps=24.0, sample_rate=48000.0,
        fade_curve=fade_curve)


# 24 帧 × 2000 样本/帧 = 48000 样本淡入；分母 47999（内部 -1）
_FI = 24 * 48000.0 / 24.0


def test_b16_audio_default_preserves_legacy_equal_power():
    """fade_curve=None：与旧 equal_power 路径逐样本一致。"""
    positions = np.arange(480, dtype=np.float64)
    legacy_env = np.sin(np.pi / 2 * (positions / (_FI - 1.0)))
    out = _fade_block(None)
    assert np.allclose(out[0, :480], legacy_env, atol=1e-12)


def test_b16_audio_curve_shapes():
    linear = _fade_block("linear")[0, :200]
    smooth = _fade_block("smooth")[0, :200]
    power = _fade_block("equal_power")[0, :200]
    t = 100 / (_FI - 1.0)
    # 小进度处 equal_power > linear > smooth
    assert power[100] > linear[100] > smooth[100]
    assert linear[100] == pytest.approx(t)
    assert power[100] == pytest.approx(math.sin(math.pi / 2 * t))
    assert smooth[100] == pytest.approx(3 * t * t - 2 * t * t * t)
    # 尾部淡出：clip 末端块上线性镜像收零（fade-out 起点在 152000 样本）
    tail = _fade_block("linear", samples=1600, current_frame=99)
    gains = tail[0]
    assert np.all(np.diff(gains) < 0)  # 单调下降
    t_end = (199599 - 152000) / (_FI - 1.0)
    assert gains[-1] == pytest.approx(1.0 - t_end)


def test_b16_snapshot_carries_curve_for_rebuild_signatures():
    from src.features.edit.core.audio_graph_snapshot import (
        build_audio_graph_snapshot,
    )
    from src.features.edit.core.timeline_v2 import ClipInstance

    def _audio_timeline(curve: str | None) -> TimelineSequence:
        clip = ClipInstance(
            "ac", "asset", "a1", 0, 100, 0, 100,
            fade_in_duration=10, fade_out_duration=10, fade_curve=curve)
        return TimelineSequence(
            "fade-curve", "FadeCurve", width=64, height=48,
            fps=Rational(24, 1),
            video_tracks=(EditTrack("v1", "V1", "video", 0),),
            audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(clip,)),),
        )

    snap = build_audio_graph_snapshot(_audio_timeline("smooth"))
    assert snap.clips[0].fade_curve == "smooth"
    # 签名包含曲线（改曲线触发解码器重建）
    snap2 = build_audio_graph_snapshot(_audio_timeline("linear"))
    assert snap.stable_hash != snap2.stable_hash
