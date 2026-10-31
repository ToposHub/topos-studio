from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.commands import SceneCommandStack, SetSceneExpressionsCommand
from src.features.teaching_animation.core.model import SceneExpression, SceneMarker, TeachingSceneDocument
from src.features.teaching_animation.render.evaluator import evaluate
from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer
from src.features.teaching_animation.render.v2_adapter import SvgSceneRendererV2
from src.features.teaching_animation.core.contracts import FrameContext, ProxyTier


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(
        "expression runtime", scene_id="expression-runtime", width=320, height=180, duration_frames=24,
    ).with_marker_added(SceneMarker("beat", 4, "beat"))


def test_expression_is_scene_owned_serializable_and_frame_deterministic() -> None:
    scene = _scene()
    expression = SceneExpression("move", "title_ignored", "x", "frame * 2")
    # Use the stable title id from this scene rather than relying on generated ids.
    title = scene.elements[0]
    expression = replace(expression, element_id=title.element_id, source="frame * 2 + markers['beat']")
    scene = replace(scene, expressions=(expression,))
    evaluated = evaluate(scene, 3)
    assert evaluated.elements[0].transform.x == pytest.approx(10.0)
    assert evaluated.diagnostics == ()
    restored = TeachingSceneDocument.from_json(scene.to_json())
    assert restored.expressions == scene.expressions
    assert evaluate(restored, 3) == evaluated


def test_expression_command_is_undoable_and_rejects_unsafe_source() -> None:
    scene = _scene()
    title_id = scene.elements[0].element_id
    command = SetSceneExpressionsCommand((SceneExpression("opacity", title_id, "opacity", "clamp(frame / 12, 0, 1)"),))
    stack = SceneCommandStack()
    updated = stack.execute(command, scene)
    assert len(updated.expressions) == 1
    restored = stack.undo(updated)
    assert restored == scene
    redone = stack.redo(restored)
    assert redone == updated
    with pytest.raises(ValueError, match="invalid expression"):
        stack.execute(
            SetSceneExpressionsCommand((SceneExpression("bad", title_id, "x", "__import__('os')"),)),
            scene,
        )


def test_loaded_invalid_expression_uses_default_and_reports_diagnostic() -> None:
    scene = _scene()
    title_id = scene.elements[0].element_id
    invalid = SceneExpression("broken", title_id, "x", "unknown_name", default=42.0)
    evaluated = evaluate(replace(scene, expressions=(invalid,)), 2)
    assert evaluated.elements[0].transform.x == pytest.approx(42.0)
    assert evaluated.diagnostics and "broken" in evaluated.diagnostics[0]


def test_expression_fallback_diagnostic_reaches_render_metadata() -> None:
    scene = _scene()
    title_id = scene.elements[0].element_id
    scene = replace(scene, expressions=(SceneExpression("broken", title_id, "x", "unknown_name", default=42.0),))
    renderer = SvgSceneRenderer()
    renderer.prepare(scene, RenderContext(fps=scene.fps, width=scene.width, height=scene.height))
    try:
        frame = renderer.render_frame(AnimationFrame(2, scene.fps))
    finally:
        renderer.cleanup()
    assert frame.metadata["expression_diagnostics"]


def test_preview_and_export_v2_use_identical_expression_frame_semantics() -> None:
    scene = _scene()
    title_id = scene.elements[0].element_id
    scene = replace(scene, expressions=(SceneExpression("move", title_id, "x", "frame * 3 + seed", seed=5),))
    preview_renderer = SvgSceneRendererV2()
    export_renderer = SvgSceneRendererV2()
    preview_context = FrameContext.for_scene(scene, 7, proxy_tier=ProxyTier.PREVIEW)
    export_context = FrameContext.for_scene(scene, 7, proxy_tier=ProxyTier.FINAL)
    preview_session = preview_renderer.prepare(scene, preview_context)
    export_session = export_renderer.prepare(scene, export_context)
    try:
        preview = preview_renderer.render(preview_session, preview_context)
        exported = export_renderer.render(export_session, export_context)
    finally:
        preview_renderer.close(preview_session)
        export_renderer.close(export_session)
    assert preview.frame.data == exported.frame.data
    assert preview.evidence.frame_hash == exported.evidence.frame_hash
