"""F4.5 · Qualifier 亮度区间动画——五环验收测试。

验收锚点（计划 §F4.5，选定参数 = Qualifier 亮度区间 lum_low/lum_high）：
- 记录/求值/曲线轨道/逐参数复制/静态切换五环贯通；
- 人物从阴影走向高光时限定器随帧变化（区间逐帧插值）；
- 随机 seek/反放/非零入点/变速取一致值（绝对关键帧 + 幂等求值）；
- Undo/重开（序列化轨道）/成片一致（同一求值函数是唯一入口）；
- 白名单外字段不得被 ◆ 暗示支持（is_qualifier_animatable 唯一判据）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.color.core.grade_keyframes import (
    GradeKeyframeTrack,
    clear_qualifier_animation,
    copy_named_tracks,
    evaluate_qualifier_keyframes,
    is_qualifier_animatable,
    parse_qualifier_track_name,
    qualifier_track_name,
    record_qualifier_keyframe,
)

pytestmark = getattr(pytest.mark, "color_core", pytest.mark.edit_core)


def _layer(params="__default__", tracks=None):
    if params == "__default__":
        params = {"lum_low": 0.0, "lum_high": 1.0}
    return SimpleNamespace(
        qualifier_params=dict(params) if params is not None else None,
        grade_keyframes=tracks if tracks is not None else {})


def _walk_tracks() -> dict:
    """人物从阴影走向高光：亮度区间随帧收窄再放宽（两轨道）。"""
    low = GradeKeyframeTrack(qualifier_track_name("lum_low"))
    low.set_keyframe(0, 0.0, "linear")
    low.set_keyframe(48, 0.30, "linear")
    low.set_keyframe(96, 0.10, "linear")
    high = GradeKeyframeTrack(qualifier_track_name("lum_high"))
    high.set_keyframe(0, 0.45, "linear")
    high.set_keyframe(48, 0.95, "linear")
    high.set_keyframe(96, 0.80, "linear")
    return {low.param_name: low, high.param_name: high}


class TestRecordAndEvaluate:
    def test_record_reads_current_values(self):
        layer = _layer({"lum_low": 0.12, "lum_high": 0.88})
        wrote = record_qualifier_keyframe(layer, 24, ["lum_low", "lum_high"])
        assert wrote == 2
        assert layer.grade_keyframes[
            qualifier_track_name("lum_low")].value_at(24, 0.0) == 0.12

    def test_record_skips_non_whitelisted(self):
        layer = _layer({"lum_low": 0.1, "lum_high": 0.9, "invert": True})
        assert record_qualifier_keyframe(
            layer, 0, ["invert", "sat_high", "lum_low"]) == 1

    def test_qualifier_follows_subject_shadow_to_highlight(self):
        """人物从阴影走向高光：区间逐帧插值变化（中点值可预测）。"""
        layer = _layer(tracks=_walk_tracks())
        assert evaluate_qualifier_keyframes(layer, 24)
        assert layer.qualifier_params["lum_low"] == pytest.approx(0.15)
        assert layer.qualifier_params["lum_high"] == pytest.approx(0.70)

    def test_random_seek_reverse_nonzero_inset_consistent(self):
        """随机 seek/反放/非零入点：同帧取同值（绝对关键帧 + 幂等）。"""
        layer = _layer(tracks=_walk_tracks())
        frames = [96, 10, 48, 0, 73, 24, 48, 96, 5]
        snapshots = {}
        for f in frames:
            evaluate_qualifier_keyframes(layer, f)
            snapshots.setdefault(f, (layer.qualifier_params["lum_low"],
                                     layer.qualifier_params["lum_high"]))
        for f, (lo, hi) in snapshots.items():
            evaluate_qualifier_keyframes(layer, f)  # 幂等重放
            assert layer.qualifier_params["lum_low"] == pytest.approx(lo)
            assert layer.qualifier_params["lum_high"] == pytest.approx(hi)
        # 反放顺序独立：帧 48 是关键帧（绝对值 0.30/0.95）
        assert snapshots[48] == (pytest.approx(0.30), pytest.approx(0.95))

    def test_speed_change_maps_to_source_frames(self):
        """变速（2x）：时间线帧 t 对应源帧 2t——按源帧求值即一致。"""
        layer = _layer(tracks=_walk_tracks())
        for timeline_frame in (12, 24, 36):  # 源帧 24/48/72
            source = timeline_frame * 2
            evaluate_qualifier_keyframes(layer, source)
        evaluate_qualifier_keyframes(layer, 48)
        assert layer.qualifier_params["lum_low"] == pytest.approx(0.30)

    def test_disabled_qualifier_tracks_skipped(self):
        layer = _layer(params=None, tracks=_walk_tracks())  # 无 qualifier
        assert evaluate_qualifier_keyframes(layer, 48) is False

    def test_undo_friendly_evaluation_does_not_touch_tracks(self):
        """求值只写参数不写轨道（undo 恢复轨道后重放一致）。"""
        layer = _layer(tracks=_walk_tracks())
        before = {name: list(t.frames()) for name, t
                  in layer.grade_keyframes.items()}
        for f in (0, 24, 48, 96):
            evaluate_qualifier_keyframes(layer, f)
        after = {name: list(t.frames()) for name, t
                 in layer.grade_keyframes.items()}
        assert before == after


class TestPerParamCopyAndStaticSwitch:
    def test_copy_named_tracks_takes_only_named(self):
        tracks = _walk_tracks()
        grade = GradeKeyframeTrack("exposure")
        grade.set_keyframe(0, 5.0)
        tracks["exposure"] = grade
        copied = copy_named_tracks(
            tracks, [qualifier_track_name("lum_low"), "exposure"])
        assert set(copied) == {qualifier_track_name("lum_low"), "exposure"}
        # 深拷贝隔离：目标层删轨道不影响源
        copied[qualifier_track_name("lum_low")].set_keyframe(100, 9.0)
        assert tracks[qualifier_track_name("lum_low")].value_at(100, 0) != 9.0

    def test_static_switch_restores_base_values(self):
        layer = _layer({"lum_low": 0.2, "lum_high": 0.8},
                       tracks=_walk_tracks())
        assert clear_qualifier_animation(layer.grade_keyframes) == 2
        evaluate_qualifier_keyframes(layer, 48)  # 无轨道 → 参数不动
        assert layer.qualifier_params["lum_low"] == 0.2
        assert layer.qualifier_params["lum_high"] == 0.8


class TestRegistryGate:
    def test_diamond_marker_gate(self):
        """白名单外字段不得被 ◆ 暗示支持（唯一判据）。

        N10-B：白名单从 lum 双字段扩到 HSL 三带 11 个数值字段——
        离散开关（enabled/invert/…）与 key 后处理强度（blur/clean_*/
        denoise，涉 N02-B key_pipeline 逐帧声明）仍不可动画。
        """
        for field in ("lum_low", "lum_high", "lum_soft_low", "sat_low",
                      "hue_center"):
            assert is_qualifier_animatable(field), field
        for field in ("invert", "enabled", "blur", "clean_black", "denoise",
                      "points"):
            assert not is_qualifier_animatable(field)
        assert parse_qualifier_track_name("qualifier.invert") is None
        assert parse_qualifier_track_name("mask.s1.center") is None

    def test_tracks_serialize_through_layer_payload(self):
        """重开/序列化：轨道经既有 grade_keyframes 载荷往返（求值一致）。"""
        tracks = _walk_tracks()
        payload = {name: t.to_dict() for name, t in tracks.items()}
        restored = {}
        for name, data in payload.items():
            track = GradeKeyframeTrack(name)
            for kf in data.get("keyframes", []):
                track.set_keyframe(
                    int(kf.get("frame", 0)), float(kf.get("value", 0)),
                    kf.get("interpolation", "linear"))
            restored[name] = track
        layer = _layer(tracks=restored)
        evaluate_qualifier_keyframes(layer, 48)  # 关键帧帧 = 绝对值
        assert layer.qualifier_params["lum_low"] == pytest.approx(0.30)
        evaluate_qualifier_keyframes(layer, 24)  # 插值帧与首测一致
        assert layer.qualifier_params["lum_low"] == pytest.approx(0.15)
