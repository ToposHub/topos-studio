"""R05：未知 fade_curve 的前向兼容——打开→保存→重开原值不丢。

反例（future_curve_roundtrip）：新版工程带 ``fade_curve="future_exp"``，
旧程序 ``from_dict`` 把它清洗为 None，``_extra_fields`` 虽保留了原值，
但 ``to_dict`` 先写出 ``"fade_curve": None`` 再 ``setdefault``（不覆
盖已存在键），保存结果仍是 None——曲线定义被静默洗掉。

契约（R05）：

- 加载：未知原值保留在 extras，渲染按 legacy 默认**降级**（不冒充
  已支持），日志给出脏值诊断；
- 保存：未被用户改写时原样回写原值（两轮往返不丢）；
- 用户显式写：设置已知曲线或重置 None 都覆盖原值（用户意图优先）；
- undo：与未知原值一致（set→undo 恢复"原值+降级"状态）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.features.edit.core.commands import SetFadeCurveCommand
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "clip_id": "c1", "asset_id": "asset", "track_id": "v1",
        "timeline_start": 0, "timeline_duration": 100,
        "source_start": 0, "source_duration": 100,
    }
    base.update(overrides)
    return base


def _timeline(clip: ClipInstance) -> TimelineSequence:
    return TimelineSequence(
        "fc", "FC", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


# ── 打开→保存→重开：原值不丢 ─────────────────────────────────────────


def test_r05_future_string_survives_roundtrip():
    """future 字符串：两轮 open→save→reopen 原值不丢，渲染降级为 None。"""
    data = _clip_dict(fade_curve="future_exp")
    for _ in range(2):
        clip = ClipInstance.from_dict(data)
        assert clip.fade_curve is None  # 渲染 legacy 降级，不冒充支持
        assert clip.fade_curve_raw == "future_exp"
        data = clip.to_dict()
    assert data["fade_curve"] == "future_exp"


def test_r05_future_structure_survives_roundtrip():
    """未来结构（dict 载荷）同样原样往返。"""
    future = {"version": 2, "shape": "bezier", "points": [0, 1]}
    clip = ClipInstance.from_dict(_clip_dict(fade_curve=future))
    assert clip.fade_curve is None
    assert clip.fade_curve_raw == future
    revived = ClipInstance.from_dict(json.loads(json.dumps(clip.to_dict())))
    assert revived.fade_curve_raw == future


def test_r05_package_level_json_roundtrip():
    """真实工程包（JSON 文件字节级）往返不丢。"""
    sequence = _timeline(ClipInstance.from_dict(
        _clip_dict(fade_curve="v4_curve")))
    packed = json.dumps(sequence.to_dict())
    reloaded = TimelineSequence.from_dict(json.loads(packed))
    clip = reloaded.get_clip_by_id("c1")
    assert clip.fade_curve is None
    assert clip.fade_curve_raw == "v4_curve"
    assert json.dumps(reloaded.to_dict()) == packed  # 再存字节不变


def test_r05_dirty_value_diagnosis_logged(caplog):
    with caplog.at_level("WARNING", logger="src.features.edit.core.timeline_v2"):
        ClipInstance.from_dict(_clip_dict(fade_curve="future_exp"))
    assert any("fade_curve" in rec.message for rec in caplog.records)


# ── 用户显式写覆盖原值 ───────────────────────────────────────────────


def test_r05_explicit_known_curve_overwrites_raw():
    clip = ClipInstance.from_dict(_clip_dict(fade_curve="future_exp"))
    written = clip.with_fade_curve("smooth")
    assert written.fade_curve == "smooth"
    assert written.fade_curve_raw is None
    assert written.to_dict()["fade_curve"] == "smooth"


def test_r05_explicit_reset_clears_raw():
    """用户显式重置 None = legacy 默认（清原值，不是恢复原值）。"""
    clip = ClipInstance.from_dict(_clip_dict(fade_curve="future_exp"))
    reset = clip.with_fade_curve(None)
    assert reset.fade_curve is None
    assert reset.fade_curve_raw is None
    assert "fade_curve" not in reset.to_dict() or \
        reset.to_dict()["fade_curve"] is None


def test_r05_known_value_never_enters_raw():
    """已知值正常往返，不误入 extras 原值通道。"""
    clip = ClipInstance.from_dict(_clip_dict(fade_curve="smooth"))
    assert clip.fade_curve == "smooth"
    assert clip.fade_curve_raw is None
    assert clip.to_dict()["fade_curve"] == "smooth"


# ── undo 与未知原值一致 ──────────────────────────────────────────────


def test_r05_undo_restores_raw_value():
    timeline = _timeline(ClipInstance.from_dict(
        _clip_dict(fade_curve="future_exp")))
    cmd = SetFadeCurveCommand.create("c1", "v1", "smooth")
    changed = cmd.apply(timeline)
    assert changed.get_clip_by_id("c1").fade_curve == "smooth"

    undone = cmd.undo(changed)
    clip = undone.get_clip_by_id("c1")
    assert clip.fade_curve is None
    assert clip.fade_curve_raw == "future_exp"
    assert clip.to_dict()["fade_curve"] == "future_exp"


def test_r05_undo_after_serialization_restores_raw():
    timeline = _timeline(ClipInstance.from_dict(
        _clip_dict(fade_curve="future_exp")))
    cmd = SetFadeCurveCommand.create("c1", "v1", "smooth")
    changed = cmd.apply(timeline)
    revived = SetFadeCurveCommand.from_dict(json.loads(
        json.dumps(cmd.to_dict())))

    undone = revived.undo(changed)
    clip = undone.get_clip_by_id("c1")
    assert clip.fade_curve_raw == "future_exp"


def test_r05_reset_on_legacy_is_still_noop():
    """旧工程（无曲线）重置 None：no-op 同一实例，不进历史。"""
    timeline = _timeline(ClipInstance.from_dict(_clip_dict()))
    cmd = SetFadeCurveCommand.create("c1", "v1", None)
    assert cmd.apply(timeline) is timeline


# ── 旧 None 默认消费不变 ─────────────────────────────────────────────


def test_r05_legacy_none_renders_default_curves():
    """fade_curve=None（含原值降级）消费者按 legacy 默认，不冒充支持。"""
    from src.features.edit.core.audio_dsp import fade_curve_factor

    clip = ClipInstance.from_dict(_clip_dict(fade_curve="future_exp"))
    assert clip.fade_curve is None
    # 视频消费者：None → linear
    assert fade_curve_factor(None, 0.25) == pytest.approx(0.25)
    # 已知值不受影响
    assert fade_curve_factor("smooth", 0.25) == pytest.approx(
        3 * 0.0625 - 2 * 0.015625)
