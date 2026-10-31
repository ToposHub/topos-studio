"""Real shared-GPU gate for the MotionGraphic Composite bridge."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.render.composite_effect_bridge import CompositeEffectBridge
from src.features.teaching_animation.core.professional import SceneColorPipeline
from src.features.teaching_animation.core.model import SceneExpression, TeachingSceneDocument
from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer
from src.features.teaching_animation.scene_store import get_shared_scene_store
from src.features.teaching_animation.render.effect_capabilities import (
    EffectDeclaration,
    RenderProfile,
)


pytestmark = pytest.mark.gpu_renderer


def _standalone_context_or_skip():
    moderngl = pytest.importorskip("moderngl")
    try:
        return moderngl.create_standalone_context()
    except Exception as exc:
        pytest.skip(f"standalone ModernGL context unavailable: {exc}")


def test_motion_blur_shared_shader_runs_and_releases_resources() -> None:
    """Exercise the real catalog variant, not only the declaration resolver."""
    context = _standalone_context_or_skip()
    source = context.texture((32, 32), 4, dtype="f1")
    source.write(bytes([128, 64, 32, 255]) * (32 * 32))
    bridge = CompositeEffectBridge(context)
    try:
        result = bridge.apply(
            source.glo,
            32,
            32,
            (EffectDeclaration("motion_blur", {"radius": 4.0, "angle": 45.0}),),
            profile=RenderProfile.preview(),
            dtype="f1",
        )
        assert result.texture_id > 0
        assert result.texture_id != source.glo
        assert result.chain.capability_fallback == ()
    finally:
        bridge.cleanup()
        source.release()
        context.release()


def test_hdr_texture_fbo_soak_keeps_surface_counts_bounded() -> None:
    """A repeated HDR pass reuses three scratch textures/FBOs and releases all."""
    context = _standalone_context_or_skip()
    source = context.texture((32, 32), 4, dtype="f1")
    source.write(bytes([128, 64, 32, 255]) * (32 * 32))
    bridge = CompositeEffectBridge(context)
    try:
        for _ in range(64):
            result = bridge.apply(
                source.glo,
                32,
                32,
                (EffectDeclaration("motion_blur", {"radius": 4.0, "angle": 45.0}),),
                profile=RenderProfile.preview(),
                color_pipeline=SceneColorPipeline("rec2020", 16),
            )
            assert result.texture_id > 0
        snapshot = bridge.resource_diagnostics()
        assert snapshot["allocated"] is True
        assert snapshot["dtype"] == "f2"
        assert snapshot["texture_count"] == 3
        assert snapshot["fbo_count"] == 3
    finally:
        bridge.cleanup()
        assert bridge.resource_diagnostics()["allocated"] is False
        source.release()
        context.release()


def test_composite_motion_graphic_uses_expression_frame_semantics() -> None:
    """The real Composite source node must consume the shared evaluator."""
    context = _standalone_context_or_skip()
    scene = TeachingSceneDocument.create_default(
        "Composite expression", scene_id="composite-expression-gate",
        width=64, height=36, duration_frames=12,
    )
    title_id = scene.elements[0].element_id
    scene = replace(scene, expressions=(SceneExpression(
        "move-title", title_id, "x", "frame * 2.0", default=0.0,
    ),))
    store = get_shared_scene_store()
    store.register(scene)
    node = None
    try:
        reference = SvgSceneRenderer()
        reference.prepare(scene, RenderContext(
            fps=scene.fps, width=scene.width, height=scene.height, transparent=True,
        ))
        try:
            reference_frame_0 = reference.render_frame(AnimationFrame(0, scene.fps)).data
            reference_frame_7 = reference.render_frame(AnimationFrame(7, scene.fps)).data
        finally:
            reference.cleanup()
        assert reference_frame_0 != reference_frame_7

        from src.features.composite.core.eval_context import CompositeEvalContext
        from src.features.teaching_animation.composite_node import MotionGraphicNode

        node = MotionGraphicNode()
        node.parameters["scene_id"] = scene.scene_id
        eval_context = CompositeEvalContext(gl=context, frame_number=0)
        first = node._do_evaluate({}, eval_context, None)
        eval_context.frame_number = 7
        second = node._do_evaluate({}, eval_context, None)
        assert first["image"].bit_depth == 8
        assert second["image"].bit_depth == 8
        assert first["image"].revision == 1
        assert second["image"].revision == 2
    finally:
        if node is not None:
            node.cleanup()
        store.remove(scene.scene_id)
        context.release()
