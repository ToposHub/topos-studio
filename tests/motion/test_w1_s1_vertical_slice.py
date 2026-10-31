"""W1 S1 vertical slice: the four-step teaching flow gate (plan §6 S1).

This is the acceptance test for the whole motion wave.  It builds the canonical
"15s, four step cards, sequential spring pop, 12-frame stagger, CTA" scene using
ONLY high-level motion commands — no hand-written keyframes — and asserts:

* cards pop in sequence (progressive visibility via scale);
* changing the stagger interval ripples later cards automatically;
* the AI-style edit "card 3 later / no overshoot / brand-blue CTA" is one command;
* everything is fully undoable and produces a readable SceneDiff;
* the same scene renders deterministically at 0 / mid / end / marker frames via
  the frozen V2 renderer contract.

This is the proof that "依次弹出" no longer needs 40 hand-rolled keyframes.
"""

from __future__ import annotations

import copy

import pytest

from src.features.teaching_animation.core.commands import (
    AddElementCommand, SceneCommandStack, UpdateElementCommand,
)
from src.features.teaching_animation.core.contracts import FrameContext, CanvasSpace, RenderSurface, AlphaMode
from src.features.teaching_animation.core.model import (
    SCENE_SCHEMA_VERSION, SceneElement, SceneTransform, TeachingSceneDocument,
)
from src.features.teaching_animation.motion import (
    ApplyMotionPresetCommand, SetStaggerIntervalCommand, StaggerElementsCommand,
    compile_preset_to_track, scene_diff,
)
from src.features.teaching_animation.motion.presets import create_default_preset_registry
from src.features.teaching_animation.render.v2_adapter import SvgSceneRendererV2
from src.shared.keyframe import KeyframeTrack


# ---------------------------------------------------------------------------
# Scene scaffolding (built with structural commands, not hand-written tracks)
# ---------------------------------------------------------------------------

def _text(element_id: str, x: float, y: float, content: str, font_size: float = 28.0) -> SceneElement:
    return SceneElement(
        element_id=element_id, type="text",
        transform=SceneTransform(x=x, y=y, width=420.0, height=80.0),
        style={"color": "#e5e7eb", "font_size": font_size},
        content=content,
    )


def _card(element_id: str, x: float, y: float) -> SceneElement:
    return SceneElement(
        element_id=element_id, type="shape", z_index=1,
        transform=SceneTransform(x=x, y=y, width=380.0, height=140.0),
        style={"fill": "#1e293b"},
        content={"kind": "rect"},
    )


def _s1_scene() -> TeachingSceneDocument:
    scene = TeachingSceneDocument(
        scene_id="s1-four-steps", schema_version=SCENE_SCHEMA_VERSION,
        name="AI Agent Four Steps", width=1280, height=720,
        fps_num=24, fps_den=1, duration_frames=360,  # 15s @ 24fps
        background={"color": "#0b1220"},
        elements=(),
        animations=(),
    )
    stack = SceneCommandStack()
    scene = stack.execute(AddElementCommand(_text("title", 80.0, 60.0, "AI Agent in 4 Steps", font_size=44.0)), scene)
    for index in range(4):
        scene = stack.execute(AddElementCommand(_card(f"card_{index + 1}", 80.0 + index * 290.0, 220.0)), scene)
    scene = stack.execute(AddElementCommand(_text("cta", 80.0, 560.0, "Get started →", font_size=36.0)), scene)
    return scene


# ---------------------------------------------------------------------------
# S1 gate tests
# ---------------------------------------------------------------------------

class TestS1StaggerPop:
    def test_four_cards_pop_in_sequence_without_handwritten_keyframes(self) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        card_ids = tuple(f"card_{i + 1}" for i in range(4))
        # One high-level command → four cards spring-pop with a 12-frame stagger.
        cmd = StaggerElementsCommand(
            element_ids=card_ids, preset_id="pop", param_name="scale_x",
            interval=12, duration=18, anchor_frame=24,
        )
        animated = stack.execute(cmd, scene)

        # Each card got exactly one compiled track; no hand-written keyframes.
        added = scene_diff(scene, animated)
        assert len(added["added_animations"]) == 4
        # The compiled tracks target the right elements/property.
        by_element = {a.element_id: a for a in animated.animations if a.element_id in card_ids}
        assert set(by_element) == set(card_ids)
        for animation in by_element.values():
            assert animation.property == "scale_x"
            assert isinstance(animation.track, KeyframeTrack)

    def test_progressive_scale_visibility(self) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        card_ids = tuple(f"card_{i + 1}" for i in range(4))
        animated = stack.execute(StaggerElementsCommand(
            element_ids=card_ids, preset_id="pop", param_name="scale_x",
            interval=12, duration=18, anchor_frame=24,
        ), scene)

        # Card 1 starts at frame 24; card 4 starts at 24 + 3*12 = 60.
        def scale_at(element_id: str, frame: int) -> float:
            anim = next(a for a in animated.animations if a.element_id == element_id)
            return anim.track.get_value(frame)

        # At frame 24 only card 1 has begun; cards 2-4 are still at scale 0.
        assert scale_at("card_1", 24) == pytest.approx(0.0, abs=1e-6)
        assert scale_at("card_2", 24) == pytest.approx(0.0, abs=1e-6)
        # After each card's settle window it reaches ~1.0.
        assert scale_at("card_1", 60) == pytest.approx(1.0, abs=2e-2)
        assert scale_at("card_4", 120) == pytest.approx(1.0, abs=2e-2)
        # Card 4 has not started at frame 50 (start=60).
        assert scale_at("card_4", 50) == pytest.approx(0.0, abs=1e-6)


class TestS1Ripple:
    def test_changing_interval_shifts_later_cards(self) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        card_ids = tuple(f"card_{i + 1}" for i in range(4))
        stagger = StaggerElementsCommand(
            element_ids=card_ids, preset_id="pop", param_name="scale_x",
            interval=12, duration=18, anchor_frame=24,
        )
        animated = stack.execute(stagger, scene)
        plan_id = stagger._plan_id
        assert plan_id is not None

        # Widen the interval to 24 frames; later cards ripple later.
        widened = stack.execute(SetStaggerIntervalCommand(plan_id=plan_id, interval=24), animated)

        def start_frame(element_id: str, doc: TeachingSceneDocument) -> int:
            anim = next(a for a in doc.animations if a.element_id == element_id)
            return min(kf.frame for kf in anim.track.keyframes)

        # Card starts: anchor 24 + index*interval.
        starts_12 = {eid: start_frame(eid, animated) for eid in card_ids}
        starts_24 = {eid: start_frame(eid, widened) for eid in card_ids}
        assert starts_12 == {"card_1": 24, "card_2": 36, "card_3": 48, "card_4": 60}
        assert starts_24 == {"card_1": 24, "card_2": 48, "card_3": 72, "card_4": 96}
        # Card 1 is unaffected; later cards moved.
        assert starts_24["card_1"] == starts_12["card_1"]
        assert starts_24["card_4"] > starts_12["card_4"]

    def test_set_interval_undo_restores_original_starts(self) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        card_ids = tuple(f"card_{i + 1}" for i in range(4))
        animated = stack.execute(StaggerElementsCommand(
            element_ids=card_ids, preset_id="pop", param_name="scale_x",
            interval=12, duration=18, anchor_frame=24,
        ), scene)
        plan_id = animated.metadata["motion_plans"]
        plan_id = next(iter(plan_id))
        widened = stack.execute(SetStaggerIntervalCommand(plan_id=plan_id, interval=24), animated)
        restored = stack.undo(widened)
        assert restored is not None

        def starts(doc: TeachingSceneDocument) -> tuple[int, ...]:
            return tuple(min(kf.frame for kf in next(a for a in doc.animations if a.element_id == eid).track.keyframes)
                         for eid in card_ids)
        assert starts(restored) == starts(animated)


class TestS1AIStyleEdit:
    def test_apply_preset_to_single_element_is_undoable(self) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        cmd = ApplyMotionPresetCommand(
            element_id="cta", preset_id="fade", param_name="opacity",
            start_frame=300, end_frame=330,
        )
        animated = stack.execute(cmd, scene)
        cta_anim = next(a for a in animated.animations if a.element_id == "cta")
        assert cta_anim.property == "opacity"
        assert cta_anim.track.get_value(300) == pytest.approx(0.0, abs=1e-6)
        assert cta_anim.track.get_value(330) == pytest.approx(1.0, abs=1e-6)
        # Undo removes it cleanly.
        restored = stack.undo(animated)
        assert restored is not None
        assert not any(a.element_id == "cta" for a in restored.animations)

    def test_brand_color_edit_is_structural_command(self) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        # "CTA 改为品牌蓝" is a structural UpdateElementCommand, not a track edit.
        updated = stack.execute(UpdateElementCommand(
            element_id="cta", updates={"style": {"color": "#2563eb", "font_size": 36.0}},
        ), scene)
        assert updated.element_by_id("cta").style["color"] == "#2563eb"
        restored = stack.undo(updated)
        assert restored is not None
        assert restored.element_by_id("cta").style["color"] == "#e5e7eb"


class TestS1RoundTripAndRender:
    def test_motion_plan_persists_through_json_round_trip(self) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        card_ids = tuple(f"card_{i + 1}" for i in range(4))
        animated = stack.execute(StaggerElementsCommand(
            element_ids=card_ids, preset_id="pop", param_name="scale_x",
            interval=12, duration=18, anchor_frame=24,
        ), scene)
        serialized = animated.to_json()
        restored = TeachingSceneDocument.from_json(serialized)
        assert restored == animated
        # Plan survived.
        assert "motion_plans" in restored.metadata
        # Animations survived.
        assert len(restored.animations) == 4

    def test_overshoot_removed_via_no_overshoot_param(self) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        cmd = ApplyMotionPresetCommand(
            element_id="card_3", preset_id="pop", param_name="scale_x",
            start_frame=48, end_frame=48 + 40, params={"overshoot": False},
        )
        animated = stack.execute(cmd, scene)
        anim = next(a for a in animated.animations if a.element_id == "card_3")
        # With overshoot clamped, scale never exceeds 1.0.
        assert max(kf.value for kf in anim.track.keyframes) <= 1.0 + 1e-9

    @pytest.mark.parametrize("frame", [0, 24, 60, 120, 359])
    def test_renders_deterministically_at_key_frames(self, frame: int) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        card_ids = tuple(f"card_{i + 1}" for i in range(4))
        scene = stack.execute(StaggerElementsCommand(
            element_ids=card_ids, preset_id="pop", param_name="scale_x",
            interval=12, duration=18, anchor_frame=24,
        ), scene)
        # Render via the frozen V2 contract — same path preview and export use.
        fc = FrameContext(
            frame=frame, fps_num=scene.fps_num, fps_den=scene.fps_den,
            canvas=CanvasSpace.from_dimensions(scene.width, scene.height),
            surface=RenderSurface(scene.width, scene.height, dpr=1.0, alpha_mode=AlphaMode.PREMULT),
        )
        renderer = SvgSceneRendererV2()
        session = renderer.prepare(scene, fc)
        try:
            result = renderer.render(session, fc)
        finally:
            renderer.close(session)
        assert result.frame.pixel_format == "rgba8"
        assert result.frame.width == scene.width
        assert result.frame.height == scene.height
        # Reproducible: rendering the same frame twice yields the same bytes.
        session2 = renderer.prepare(scene, fc)
        try:
            result2 = renderer.render(session2, fc)
        finally:
            renderer.close(session2)
        assert bytes(result.frame.data) == bytes(result2.frame.data)
        assert result.evidence.frame_hash == result2.evidence.frame_hash


class TestSceneDiffReadable:
    def test_diff_reports_added_animations_and_metadata(self) -> None:
        scene = _s1_scene()
        stack = SceneCommandStack()
        before = copy.deepcopy(scene)
        animated = stack.execute(StaggerElementsCommand(
            element_ids=("card_1", "card_2"), preset_id="pop", param_name="scale_x",
            interval=12, duration=18, anchor_frame=0,
        ), scene)
        diff = scene_diff(before, animated)
        assert len(diff["added_animations"]) == 2
        assert diff["metadata_changed"] is True
        assert diff["duration_changed"] is False
