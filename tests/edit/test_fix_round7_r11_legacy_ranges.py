"""第七轮审计 0B 回归：旧工程动画值域迁移（R11）。

C01 把新建参数的值域换成了权威 schema（clip_params.clip_param_range），
但旧工程持久化的 AnimatableParam/KeyframeTrack 仍带旧默认值域
（translate/rotation 常见 [0,100]）——求值钳制走 track 的
min_val/max_val，负平移/大角度关键帧加载后采样全为 0，旧轨上新增
负关键帧也继续被钳。

修复语义：已知 clip 参数（opacity/transform.*）在 from_dict 加载时与
with_keyframe_added 打点前迁移到权威值域；关键帧 raw、base_value、
未知参数名的持久化值域原样保留。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import AddKeyframeCommand
from src.features.edit.core.clip_params import clip_param_range
from src.features.edit.core.effect_instance import AnimatableParam
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.keyframe import Keyframe, KeyframeTrack
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _clip(clip_id="c1", track_id="v1") -> ClipInstance:
    return ClipInstance(clip_id, "asset", track_id, 0, 100, 0, 100)


def _seq(clip) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-r11", name="R11", width=1920, height=1080,
        fps=Rational(25, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
    )


def _add_kf(clip, frame, value, param="transform.translate_x"):
    cmd = AddKeyframeCommand(
        command_id=f"k{frame}", label="kf", track_id="v1", clip_id="c1",
        param_name=param, frame=frame, value=value)
    return cmd.apply(_seq(clip)).get_track_by_id("v1").get_clip_by_id("c1")


def _legacy_clip():
    """模拟旧工程：raw 关键帧 -250/-500 但值域仍是旧默认 [0,100]。"""
    clip = _clip()
    clip = _add_kf(clip, 0, -250.0)
    clip = _add_kf(clip, 50, -500.0)
    d = clip.to_dict()
    legacy = d["animated_params"]["transform.translate_x"]
    legacy["min_value"] = 0.0
    legacy["max_value"] = 100.0
    legacy["keyframes"]["min_val"] = 0.0
    legacy["keyframes"]["max_val"] = 100.0
    return clip, d


class TestR11LegacyRangeMigration:
    def test_from_dict_repairs_legacy_range_and_samples_raw(self):
        """旧工程加载：负关键帧 raw 保留、值域迁移、采样恢复 -250/-500
        （旧实现 [0,100] 域下采样全 0）。"""
        _, legacy_dict = _legacy_clip()
        restored = ClipInstance.from_dict(legacy_dict)

        param = restored.animated_params["transform.translate_x"]
        lo, hi = clip_param_range("transform.translate_x")
        assert param.min_value == lo and param.max_value == hi
        assert param.keyframes.min_val == lo
        assert param.keyframes.max_val == hi
        raw = [kf.value for kf in param.keyframes.keyframes]
        assert raw == [-250.0, -500.0], "关键帧 raw 不得被迁移改写"

        assert restored.get_param_at_frame(
            "transform.translate_x", 0) == pytest.approx(-250.0)
        assert restored.get_param_at_frame(
            "transform.translate_x", 25) == pytest.approx(-375.0)
        assert restored.get_param_at_frame(
            "transform.translate_x", 50) == pytest.approx(-500.0)

    def test_legacy_track_new_negative_keyframe_not_clamped(self):
        """旧域参数上新增负关键帧：先迁移再打点（不再被旧域钳 0）。"""
        clip = ClipInstance(
            "c1", "asset", "v1", 0, 100, 0, 100,
            animated_params={
                "transform.rotation": AnimatableParam(
                    base_value=0.0,
                    keyframes=KeyframeTrack(
                        param_name="transform.rotation",
                        keyframes=[Keyframe(0, 45.0)],
                        min_val=0.0, max_val=100.0),
                    min_value=0.0, max_value=100.0),
            },
        )
        clip = _add_kf(clip, 50, -90.0, param="transform.rotation")
        assert clip.get_param_at_frame(
            "transform.rotation", 50) == pytest.approx(-90.0), \
            "旧域参数新增负关键帧不得被钳 0"
        assert clip.get_param_at_frame(
            "transform.rotation", 0) == pytest.approx(45.0)

    def test_unknown_param_range_preserved(self):
        """未知参数名的持久化值域原样保留（不做 schema 迁移）。"""
        clip = ClipInstance(
            "c1", "asset", "v1", 0, 100, 0, 100,
            animated_params={
                "custom.foo": AnimatableParam(
                    base_value=2.0,
                    keyframes=KeyframeTrack(
                        param_name="custom.foo",
                        keyframes=[Keyframe(0, 4.0)],
                        min_val=0.0, max_val=5.0),
                    min_value=0.0, max_value=5.0),
            },
        )
        restored = ClipInstance.from_dict(clip.to_dict())
        param = restored.animated_params["custom.foo"]
        assert param.min_value == 0.0 and param.max_value == 5.0
        assert param.keyframes.min_val == 0.0
        assert restored.get_param_at_frame("custom.foo", 0) == pytest.approx(4.0)

    def test_roundtrip_keeps_repaired_range(self):
        """迁移后的值域经 to_dict/from_dict 往返保持（重开一致）。"""
        _, legacy_dict = _legacy_clip()
        restored = ClipInstance.from_dict(legacy_dict)
        again = ClipInstance.from_dict(restored.to_dict())
        assert again.get_param_at_frame(
            "transform.translate_x", 25) == pytest.approx(-375.0)
        lo, hi = clip_param_range("transform.translate_x")
        param = again.animated_params["transform.translate_x"]
        assert (param.min_value, param.max_value) == (lo, hi)
