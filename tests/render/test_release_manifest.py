from __future__ import annotations

from src.features.teaching_animation.core.contracts import RenderContext, RenderEvidence, RenderedFrame
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.render.consistency import ComparisonMode
from src.features.teaching_animation.render.release_manifest import (
    build_release_manifest,
    compare_rendered_frame_sequences,
)
from src.features.teaching_animation.render.v2_adapter import SvgSceneRendererV2
from src.features.teaching_animation.core.contracts import FrameContext, ProxyTier
from src.features.teaching_animation.core.model import SceneElement, SceneTransform


def test_release_manifest_carries_scene_surface_and_frame_evidence() -> None:
    scene = TeachingSceneDocument.create_default(
        "manifest", scene_id="manifest-scene", width=8, height=6, duration_frames=4)
    context = RenderContext(fps=24, width=8, height=6, backend="svg", render_profile="preview")
    evidence = (RenderEvidence(
        renderer_id="teaching-svg",
        renderer_build="build-1",
        frame_hash="hash-1",
        capability_fallback=("glow:dropped",),
    ),)
    manifest = build_release_manifest(scene, context, evidence)
    payload = manifest.to_dict()
    assert payload["scene_id"] == "manifest-scene"
    assert payload["renderer_build"] == "build-1"
    assert payload["surface"]["alpha_mode"] == "straight"
    assert payload["capability_fallback"] == ["glow:dropped"]
    assert len(payload["manifest_hash"]) == 64


def test_rendered_frame_sequences_use_strict_rgba_consistency() -> None:
    data = bytes([12, 34, 56, 255]) * (4 * 3)
    first = RenderedFrame(0, 4, 3, "rgba8", data)
    second = RenderedFrame(0, 4, 3, "rgba8", data)
    results = compare_rendered_frame_sequences((first,), (second,), mode=ComparisonMode.STRICT)
    assert len(results) == 1 and results[0].passed


def test_v2_evidence_carries_svg_effect_fallback() -> None:
    from src.features.teaching_animation.render.effect_capabilities import EffectDeclaration

    base = TeachingSceneDocument.create_default("fallback", scene_id="fallback-scene", width=64, height=48, duration_frames=4)
    element = SceneElement(
        "fallback-card", "shape", transform=SceneTransform(width=20, height=20),
        style={"effects": [EffectDeclaration("glow").to_dict()]}, content={"kind": "rect"},
    )
    scene = base.with_element_added(element)
    renderer = SvgSceneRendererV2()
    context = FrameContext.for_scene(scene, 0, proxy_tier=ProxyTier.PREVIEW)
    session = renderer.prepare(scene, context)
    try:
        evidence = renderer.render(session, context).evidence
    finally:
        renderer.close(session)
    assert any("glow" in item for item in evidence.capability_fallback)
