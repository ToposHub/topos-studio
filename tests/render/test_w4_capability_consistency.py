"""W4 effect capability matrix + render profiles + dirty range + consistency."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.render.effect_capabilities import (
    BackendEffectSupport, DirtyRange, EffectCapabilityMatrix, EffectDeclaration,
    RenderProfile, RenderProfileName, dirty_range_for_element, read_effect_declarations,
    resolve_effect_chain, write_effect_declarations,
)
from src.features.teaching_animation.core.commands import (
    SceneCommandStack, SetElementEffectsCommand, SetProfessionalCapabilitiesCommand,
    SetSceneGpuEffectsCommand,
)
from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer
from src.features.teaching_animation.render.v2_adapter import build_svg_render_plan
from src.features.teaching_animation.render.composite_effect_bridge import CompositeEffectBridge
from src.features.teaching_animation.core.professional import ParticleEmitter, SceneColorPipeline, particle_positions
from src.features.teaching_animation.render.consistency import (
    ComparisonMode, FrameSnapshot, compare_frames, golden_evidence,
)


def _frame(width: int, height: int, fill: int) -> FrameSnapshot:
    return FrameSnapshot(width=width, height=height, data=bytes([fill, fill, fill, 255]) * (width * height))


# ---------------------------------------------------------------------------
# Effect capability matrix (RD-02 / RD-04)
# ---------------------------------------------------------------------------

class TestCapabilityMatrix:
    def test_svg_approximates_blur(self) -> None:
        matrix = EffectCapabilityMatrix()
        cap = matrix.support("svg", "blur")
        assert cap.support == BackendEffectSupport.APPROXIMATE

    def test_svg_unsupported_glow(self) -> None:
        matrix = EffectCapabilityMatrix()
        assert matrix.support("svg", "glow").support == BackendEffectSupport.UNSUPPORTED

    def test_gpu_exact_for_glow(self) -> None:
        matrix = EffectCapabilityMatrix()
        assert matrix.support("gpu", "glow").support == BackendEffectSupport.EXACT

    def test_unknown_effect_defaults_unsupported(self) -> None:
        matrix = EffectCapabilityMatrix()
        assert matrix.support("svg", "totally_made_up").support == BackendEffectSupport.UNSUPPORTED

    def test_register_custom_capability(self) -> None:
        from src.features.teaching_animation.render.effect_capabilities import BackendEffectCapability
        matrix = EffectCapabilityMatrix()
        matrix.register("svg", "my_effect", BackendEffectCapability(BackendEffectSupport.EXACT, "low"))
        assert matrix.support("svg", "my_effect").support == BackendEffectSupport.EXACT


class TestResolveEffectChain:
    def test_gpu_runs_all_supported(self) -> None:
        chain = (EffectDeclaration("blur"), EffectDeclaration("glow"))
        result = resolve_effect_chain(chain, "gpu", RenderProfile.preview())
        assert all(e.status == "run" for e in result.applied)
        assert not result.blocked
        assert result.capability_fallback == ()

    def test_svg_approximates_blur_drops_glow_in_preview(self) -> None:
        chain = (EffectDeclaration("blur"), EffectDeclaration("glow"))
        result = resolve_effect_chain(chain, "svg", RenderProfile.preview())
        statuses = {e.declaration.effect_type: e.status for e in result.applied}
        assert statuses["blur"] == "approximated"
        assert statuses["glow"] == "dropped"

    def test_final_profile_blocks_on_unsupported(self) -> None:
        chain = (EffectDeclaration("glow"),)
        result = resolve_effect_chain(chain, "svg", RenderProfile.final())
        assert result.blocked
        assert any(e.status == "blocked" for e in result.applied)

    def test_draft_drops_high_cost(self) -> None:
        chain = (EffectDeclaration("blur"), EffectDeclaration("defocus"))
        result = resolve_effect_chain(chain, "gpu", RenderProfile.draft())
        statuses = {e.declaration.effect_type: e.status for e in result.applied}
        assert statuses["blur"] == "run"           # medium cost stays
        assert statuses["defocus"] == "dropped"     # high cost dropped in draft

    def test_disabled_declaration_dropped(self) -> None:
        chain = (EffectDeclaration("blur", enabled=False),)
        result = resolve_effect_chain(chain, "gpu", RenderProfile.preview())
        assert result.applied[0].status == "dropped"
        assert "disabled by author" in result.diagnostics[0]

    def test_diagnostics_and_capability_fallback_feed_evidence(self) -> None:
        chain = (EffectDeclaration("blur"), EffectDeclaration("glow"))
        result = resolve_effect_chain(chain, "svg", RenderProfile.preview())
        assert result.diagnostics
        # glow dropped -> capability_fallback records it
        assert any("glow" in fb for fb in result.capability_fallback)


class TestRenderProfile:
    def test_profiles_map_to_proxy_tiers(self) -> None:
        assert RenderProfile.draft().proxy_tier.value == "draft"
        assert RenderProfile.interactive().proxy_tier.value == "interactive"
        assert RenderProfile.preview().proxy_tier.value == "preview"
        assert RenderProfile.final().proxy_tier.value == "final"

    def test_final_blocks_on_unsupported(self) -> None:
        assert RenderProfile.final().block_on_unsupported is True
        assert RenderProfile.draft().block_on_unsupported is False


# ---------------------------------------------------------------------------
# Dirty range (UX-04 / UX-05)
# ---------------------------------------------------------------------------

class TestDirtyRange:
    def test_full_canvas_when_no_bbox(self) -> None:
        dr = dirty_range_for_element("card", active_frames=(0, 24))
        assert dr.is_full_canvas is True

    def test_bounded_bbox(self) -> None:
        dr = dirty_range_for_element("card", active_frames=(0, 24), bbox=(10, 20, 100, 50))
        assert dr.is_full_canvas is False
        assert dr.bbox_width == 100.0

    def test_overlap_and_union(self) -> None:
        a = DirtyRange(0, 10, 0, 0, 50, 50)
        b = DirtyRange(5, 15, 40, 0, 50, 50)
        assert a.overlaps(b) is True
        u = a.union(b)
        assert u.start_frame == 0 and u.end_frame == 15
        assert u.bbox_x == 0.0

    def test_no_overlap(self) -> None:
        assert DirtyRange(0, 10).overlaps(DirtyRange(20, 30)) is False

    def test_invalid_range(self) -> None:
        with pytest.raises(ValueError):
            DirtyRange(10, 10)


# ---------------------------------------------------------------------------
# Preview/export consistency (AQ-04 / AQ-05)
# ---------------------------------------------------------------------------

class TestConsistency:
    def test_strict_identical_passes(self) -> None:
        a = _frame(4, 4, 100)
        b = _frame(4, 4, 100)
        result = compare_frames(a, b, mode=ComparisonMode.STRICT)
        assert result.passed is True

    def test_strict_mismatch_fails(self) -> None:
        a = _frame(4, 4, 100)
        b = _frame(4, 4, 101)
        result = compare_frames(a, b, mode=ComparisonMode.STRICT)
        assert result.passed is False
        assert result.issues[0].code == "hash_mismatch"

    def test_dimension_mismatch(self) -> None:
        a = _frame(4, 4, 100)
        b = _frame(8, 8, 100)
        result = compare_frames(a, b)
        assert result.passed is False
        assert result.issues[0].code == "dimension_mismatch"

    def test_perceptual_within_tolerance(self) -> None:
        a = _frame(4, 4, 100)
        b = _frame(4, 4, 101)  # diff of 1 per channel
        result = compare_frames(a, b, mode=ComparisonMode.PERCEPTUAL, perceptual_tolerance=2.0)
        assert result.passed is True
        assert result.mean_abs_diff == pytest.approx(1.0)

    def test_perceptual_exceeds_tolerance(self) -> None:
        a = _frame(4, 4, 100)
        b = _frame(4, 4, 110)
        result = compare_frames(a, b, mode=ComparisonMode.PERCEPTUAL, perceptual_tolerance=2.0)
        assert result.passed is False
        assert result.issues[0].code == "perceptual_exceeded"

    def test_alpha_mismatch_reported(self) -> None:
        a = FrameSnapshot(2, 2, bytes([10, 10, 10, 255]) * 4)
        b = FrameSnapshot(2, 2, bytes([10, 10, 10, 200]) * 4)
        result = compare_frames(a, b, mode=ComparisonMode.PERCEPTUAL, alpha_tolerance=0)
        assert result.passed is False
        assert result.issues[0].code == "alpha_mismatch"

    def test_golden_evidence_serializable(self) -> None:
        snap = _frame(4, 4, 100)
        ev = golden_evidence(snap, frame=12, label="steps_mid")
        assert ev["frame"] == 12 and ev["width"] == 4 and len(ev["sha256"]) == 64


class TestEffectDeclarationRoundTrip:
    def test_round_trip(self) -> None:
        decl = EffectDeclaration("blur", {"radius": 4.0}, version=2, enabled=False)
        restored = EffectDeclaration.from_dict(decl.to_dict())
        assert restored == decl

    def test_style_chain_round_trip_is_additive(self) -> None:
        style = {"fill": "#fff", "effects": [{"effect_type": "blur", "params": {"radius": 6}}]}
        chain = read_effect_declarations(style)
        assert chain == (EffectDeclaration("blur", {"radius": 6}),)
        restored = write_effect_declarations(style, chain)
        assert restored["fill"] == "#fff"
        assert restored["effects"] == [chain[0].to_dict()]

    def test_effect_command_is_one_undoable_scene_change(self) -> None:
        element = SceneElement(
            element_id="card", type="shape",
            transform=SceneTransform(width=100, height=60),
            style={"fill": "#fff"}, content={"kind": "rect"},
        )
        scene = TeachingSceneDocument.create_default(name="effects").with_element_added(element)
        stack = SceneCommandStack()
        updated = stack.execute(
            SetElementEffectsCommand("card", (EffectDeclaration("blur", {"radius": 5}),)), scene)
        assert read_effect_declarations(updated.element_by_id("card").style)[0].effect_type == "blur"
        assert stack.undo(updated) == scene

    def test_scene_gpu_effect_command_round_trips_metadata(self) -> None:
        scene = TeachingSceneDocument.create_default(name="effects")
        stack = SceneCommandStack()
        effect = EffectDeclaration("glow", {"radius": 9})
        updated = stack.execute(SetSceneGpuEffectsCommand((effect,)), scene)
        assert updated.metadata["gpu_effects"] == [effect.to_dict()]
        assert stack.undo(updated) == scene

    def test_render_plan_contains_effect_pass_and_fallback_evidence(self) -> None:
        element = SceneElement(
            element_id="card", type="shape",
            style={"effects": [EffectDeclaration("blur", {"radius": 5}).to_dict()]},
            content={"kind": "rect"},
        )
        scene = TeachingSceneDocument.create_default(name="effects").with_element_added(element)
        plan = build_svg_render_plan(scene)
        effect_passes = [item for item in plan.passes if item.stage == "effect"]
        assert effect_passes[0].params["effect_type"] == "blur"
        assert effect_passes[0].params["status"] == "approximated"

    def test_composite_bridge_uses_shared_catalog_runner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.calls = []

            def run(self, spec, texture_id, params, width, height, *, dtype):
                self.calls.append((spec, texture_id, params, width, height, dtype))
                return 77

            def release(self) -> None:
                pass

        class FakeCatalog:
            def get_spec(self, effect_type):
                return "blur-spec" if effect_type == "blur" else None

        fake_runner = FakeRunner()
        bridge = CompositeEffectBridge(object())
        bridge._runner = fake_runner
        monkeypatch.setattr(
            "src.features.shared.effects.get_effect_catalog", lambda: FakeCatalog())
        result = bridge.apply(
            3, 320, 180, (EffectDeclaration("blur", {"radius": 6}),),
            profile=RenderProfile.preview(), dtype="f1",
        )
        assert result.texture_id == 77
        assert fake_runner.calls == [("blur-spec", 3, {"radius": 6}, 320, 180, "f1")]

    def test_motion_blur_bridge_maps_to_shared_blur_variant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeRunner:
            def run(self, spec, texture_id, params, width, height, *, dtype):
                assert spec == "blur-spec"
                assert params["blur_type"] == "motion"
                return 88
            def release(self):
                pass
        class FakeCatalog:
            def get_spec(self, effect_type):
                return "blur-spec" if effect_type == "blur" else None
        bridge = CompositeEffectBridge(object())
        bridge._runner = FakeRunner()
        monkeypatch.setattr("src.features.shared.effects.get_effect_catalog", lambda: FakeCatalog())
        result = bridge.apply(3, 64, 64, (EffectDeclaration("motion_blur", {"radius": 5, "angle": 90}),), profile=RenderProfile.preview())
        assert result.texture_id == 88


def test_w6_particle_positions_and_hdr_contracts_are_deterministic() -> None:
    emitter = ParticleEmitter("p", count=4, seed=9)
    assert particle_positions(emitter, 12) == particle_positions(emitter, 12)
    assert len(particle_positions(emitter, 12)) == 4
    pipeline = SceneColorPipeline("rec2020", 16, "aces", "premult")
    assert pipeline.is_hdr
    assert SceneColorPipeline.from_dict(pipeline.to_dict()) == pipeline


def test_w6_professional_command_renders_svg_fallback_and_reports_hdr() -> None:
    scene = TeachingSceneDocument.create_default(name="professional", width=64, height=48, duration_frames=8)
    pipeline = SceneColorPipeline("rec2020", 16, "aces", "premult")
    emitter = ParticleEmitter("spark", count=2, seed=3, origin=(0.25, 0.5))
    stack = SceneCommandStack()
    updated = stack.execute(
        SetProfessionalCapabilitiesCommand(pipeline=pipeline, emitters=(emitter,)), scene)

    renderer = SvgSceneRenderer()
    renderer.prepare(updated, RenderContext(fps=24, width=64, height=48))
    rendered = renderer.render_frame(AnimationFrame(2, 24))
    renderer.cleanup()

    svg = rendered.data.decode("utf-8")
    assert 'data-emitter-id="spark"' in svg
    assert rendered.metadata["color_pipeline"] == pipeline.to_dict()
    assert rendered.metadata["hdr_fallback"] == "svg_8bit_tonemap"
    assert stack.undo(updated) == scene
