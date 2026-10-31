"""Batch A: hierarchy (SG-01) + style schema (SG-06) + binding (MT-07) + beat (MT-08)."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core import (
    Affine2d, AlphaCompositing, BlendMode,
    Gradient, GradientStop, Hierarchy, Shadow, Stroke, StyleSchema,
    effective_visibility, validate_hierarchy, world_affine,
)
from src.features.teaching_animation.core.design_tokens import default_brand_tokens
from src.features.teaching_animation.core.model import SceneTransform
from src.features.teaching_animation.motion import (
    Beat, BeatGrid, BindingTarget, MotionBinding, format_timecode, narration_alignment,
)


# ---------------------------------------------------------------------------
# SG-01 hierarchy
# ---------------------------------------------------------------------------

class TestHierarchyValidation:
    def test_valid_chain_passes(self) -> None:
        h = Hierarchy(parents={"card": "group", "group": "root"})
        assert validate_hierarchy(["root", "group", "card"], h) == ()

    def test_missing_parent_reported(self) -> None:
        h = Hierarchy(parents={"card": "ghost"})
        issues = validate_hierarchy(["card"], h)
        assert any(i.code == "missing_parent" for i in issues)

    def test_cycle_detected(self) -> None:
        # a -> b -> a forms a cycle
        h = Hierarchy(parents={"a": "b", "b": "a"})
        issues = validate_hierarchy(["a", "b"], h)
        assert any(i.code == "cycle" for i in issues)


class TestWorldTransform:
    def test_parent_translation_applied_to_child(self) -> None:
        h = Hierarchy(parents={"child": "parent"})
        transforms = {
            "parent": SceneTransform(x=100.0, y=50.0),
            "child": SceneTransform(x=10.0, y=20.0),
        }
        world = world_affine("child", transforms, h)
        # child local (10,20) + parent translate (100,50) -> (110,70)
        assert world.transform_point(0.0, 0.0) == (pytest.approx(110.0), pytest.approx(70.0))

    def test_scale_inherited(self) -> None:
        h = Hierarchy(parents={"child": "parent"})
        transforms = {
            "parent": SceneTransform(x=0.0, y=0.0, scale_x=2.0, scale_y=2.0),
            "child": SceneTransform(x=5.0, y=0.0),
        }
        world = world_affine("child", transforms, h)
        # parent scales everything 2x; child origin (5,0) -> (10,0)
        assert world.transform_point(0.0, 0.0)[0] == pytest.approx(10.0)


class TestVisibility:
    def test_hidden_ancestor_hides_descendant(self) -> None:
        h = Hierarchy(parents={"child": "parent"}, hidden={"parent"})
        assert effective_visibility("child", h) is False

    def test_solo_isolates_subtree(self) -> None:
        h = Hierarchy(parents={"a": "root", "b": "root"}, solo={"a"})
        assert effective_visibility("a", h) is True
        assert effective_visibility("b", h) is False

    def test_no_solo_shows_all_visible(self) -> None:
        h = Hierarchy(parents={"a": "root"})
        assert effective_visibility("a", h) is True


class TestHierarchyRoundTrip:
    def test_round_trip(self) -> None:
        h = Hierarchy(parents={"c": "p"}, locked={"p"}, solo={"c"}, hidden={"x"})
        restored = Hierarchy.from_dict(h.to_dict())
        assert restored.parents == h.parents
        assert restored.locked == {"p"} and restored.solo == {"c"}


# ---------------------------------------------------------------------------
# SG-06 style schema
# ---------------------------------------------------------------------------

class TestStyleSchema:
    def test_round_trip(self) -> None:
        style = StyleSchema(fill="#fff", stroke=Stroke("#000", 2.0),
                            gradient=Gradient(stops=(GradientStop(0.0, "#06c"), GradientStop(1.0, "#fff"))),
                            shadow=Shadow(blur=12.0), blend_mode=BlendMode.MULTIPLY,
                            alpha=AlphaCompositing.PREMULTIPLIED)
        restored = StyleSchema.from_dict(style.to_dict())
        assert restored.fill == "#fff"
        assert restored.blend_mode == BlendMode.MULTIPLY
        assert restored.alpha == AlphaCompositing.PREMULTIPLIED
        assert restored.stroke.width == 2.0

    def test_from_legacy_style(self) -> None:
        legacy = {"color": "#fff", "stroke": "#000", "stroke_width": 1.5, "opacity": 0.8}
        schema = StyleSchema.from_style(legacy)
        assert schema.fill == "#fff"
        assert schema.stroke.width == 1.5
        assert schema.opacity == 0.8

    def test_resolved_replaces_tokens(self) -> None:
        schema = StyleSchema(fill="$palette.primary")
        flat = schema.resolved(default_brand_tokens())
        assert flat["fill"] == "#3b82f6"

    def test_invalid_opacity(self) -> None:
        with pytest.raises(ValueError):
            StyleSchema(opacity=1.5)

    def test_invalid_gradient_single_stop(self) -> None:
        with pytest.raises(ValueError):
            Gradient(stops=(GradientStop(0.0, "#000"),))


# ---------------------------------------------------------------------------
# MT-07 motion binding
# ---------------------------------------------------------------------------

class TestMotionBinding:
    def test_one_driver_many_targets(self) -> None:
        binding = MotionBinding(
            binding_id="entrance", driver="spring1",
            targets=(
                BindingTarget("card", "scale_x", scale=1.0, offset=0.0),
                BindingTarget("card", "y", scale=-40.0, offset=0.0),
                BindingTarget("card", "opacity", scale=1.0, offset=0.0),
            ),
        )
        out = binding.evaluate(0.5)
        assert out["card.scale_x"] == 0.5
        assert out["card.y"] == -20.0
        assert out["card.opacity"] == 0.5

    def test_duplicate_targets_rejected(self) -> None:
        with pytest.raises(ValueError):
            MotionBinding(binding_id="b", driver="d",
                          targets=(BindingTarget("e", "x"), BindingTarget("e", "x")))

    def test_round_trip(self) -> None:
        binding = MotionBinding("b", "d", (BindingTarget("e", "x", scale=2.0, offset=1.0),))
        assert MotionBinding.from_dict(binding.to_dict()) == binding


# ---------------------------------------------------------------------------
# MT-08 beat + timecode
# ---------------------------------------------------------------------------

class TestBeatGrid:
    def test_frames_per_beat_at_120bpm_24fps(self) -> None:
        grid = BeatGrid(bpm=120.0, fps_num=24, fps_den=1)
        # 120 BPM => 0.5s/beat => 12 frames/beat
        assert grid.frames_per_beat == pytest.approx(12.0)

    def test_frame_at_beat_round_trips(self) -> None:
        grid = BeatGrid(bpm=120.0, fps_num=24, fps_den=1, anchor_frame=0)
        f = grid.frame_at_beat(4)
        assert grid.beat_at_frame(f) == pytest.approx(4.0)

    def test_anchor_offset(self) -> None:
        grid = BeatGrid(bpm=120.0, fps_num=24, fps_den=1, anchor_frame=48)
        assert grid.frame_at_beat(0) == 48


class TestTimecode:
    def test_zero(self) -> None:
        assert format_timecode(0, 24, 1) == "00:00:00:00"

    def test_one_second(self) -> None:
        assert format_timecode(24, 24, 1) == "00:00:01:00"

    def test_roll_over(self) -> None:
        # 1 minute + 2 sec + 5 frames at 24fps = 60*24 + 2*24 + 5 = 1493
        assert format_timecode(1493, 24, 1) == "00:01:02:05"


class TestNarrationAlignment:
    def test_nearest_beat_and_distance(self) -> None:
        grid = BeatGrid(bpm=120.0, fps_num=24, fps_den=1)   # 12 frames/beat
        beats = (Beat("b0", 0.0), Beat("b1", 1.0))
        # cue at frame 10 (beat ~0.833) -> nearest is b1 at index 1, distance ~0.167
        nearest, dist = narration_alignment(10, grid, beats)
        assert nearest is not None and nearest.beat_id == "b1"
        assert dist == pytest.approx(0.167, abs=1e-2)


# ── efr-small 批误删在用导入回归（Mapping 运行时 isinstance） ────────────


class TestMotionBindingFromDictRuntimeMapping:
    def test_from_dict_with_dict_targets(self):
        """from_dict 的 isinstance(t, Mapping) 是运行时代码。

        efr-small 批曾把 typing.Mapping 当作仅注解使用而删除导入，
        反序列化含 targets 的数据即 NameError。
        """
        binding = MotionBinding.from_dict({
            "binding_id": "b1", "driver": "linear",
            "targets": [
                {"element_id": "e1", "property": "x", "scale": 2.0},
                "not-a-mapping-should-be-skipped",
            ],
        })
        assert len(binding.targets) == 1
        assert binding.targets[0].element_id == "e1"
        assert binding.targets[0].scale == 2.0
