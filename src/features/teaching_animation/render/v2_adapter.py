"""W0-B: adapt the existing SVG renderer behind the frozen V2 Protocol.

This is the proof that the renderer-neutral contracts (``FrameContext`` /
``RenderPlan`` / ``SceneRendererV2``) wrap the current SVG path WITHOUT
rewriting it and WITHOUT changing its pixels.  The adapter:

* converts a ``FrameContext`` to the legacy ``RenderContext`` / ``AnimationFrame``;
* delegates to ``SvgSceneRenderer`` for the actual SVG generation;
* rasterizes to RGBA via the existing ``FrameAdapter`` (QSvgRenderer, CPU);
* attaches a minimal ``RenderPlan`` + ``RenderEvidence`` so preview/export now
  speak the frozen contract.

Byte-equivalence with the legacy path is asserted in the W0-B tests so a
future swap (HTML/GPU backend) cannot silently change preview/export pixels.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from dataclasses import replace
from typing import Any, Mapping

from ..core.contracts import (
    AlphaMode,
    AnimationFrame,
    Capability,
    FrameContext,
    RenderContext,
    RenderedFrame,
    RenderEvidence,
    RenderPass,
    RenderPlan,
    RendererResult,
    RendererSession,
)
from .effect_capabilities import RenderProfile, read_effect_declarations, resolve_effect_chain
from ..core.composition_schema import read_instances, read_masks, read_mattes
from ..core.professional import read_particle_emitters, read_scene_color_pipeline
from .frame_adapter import FrameAdapter
from .scene_renderer import CancellationToken, RenderCancelled
from .svg_renderer import SvgSceneRenderer


RENDERER_ID = "teaching-svg"
RENDERER_BUILD = "teaching-animation-svg/0.2"


def frame_context_to_render_context(context: FrameContext, *, locale: str = "en-US") -> RenderContext:
    """Map a frozen FrameContext to the legacy RenderContext the SVG path expects."""
    surface = context.surface
    transparent = surface.alpha_mode == AlphaMode.PREMULT
    return RenderContext(
        fps=context.fps,
        width=surface.pixel_width,
        height=surface.pixel_height,
        renderer_version=RENDERER_BUILD,
        backend="svg",
        locale=locale,
        transparent=transparent,
        html_validation_background=context.html_validation_background,
        allow_uniform_frames=context.allow_uniform_frames,
        render_profile=context.surface.proxy_tier.value,
        working_color_space=context.surface.colorspace.value,
    )


def build_svg_render_plan(scene: Any, profile: RenderProfile | None = None) -> RenderPlan:
    """Describe the SVG rasterization as ordered passes for evidence/cache keys.

    The SVG backend collapses source+transform+layout+effect+composite into one
    rasterization, but we still emit one pass per visible element plus the
    background composite pass so the plan is introspectable and capability-
    declared.  This is descriptive metadata today; the W4 GPU renderer will use
    the same plan to drive real passes.
    """
    elements = getattr(scene, "elements", None) or ()
    profile = profile or RenderProfile.preview()
    passes: list[RenderPass] = [RenderPass(stage="composite", target="__background__")]
    capability_fallback: list[str] = []
    for element in elements:
        if not getattr(element, "visible", True):
            continue
        passes.append(RenderPass(
            stage="source",
            target=getattr(element, "element_id", ""),
            capability=Capability.SVG_RASTER,
        ))
        declarations = read_effect_declarations(getattr(element, "style", None))
        chain = resolve_effect_chain(declarations, "svg", profile)
        capability_fallback.extend(chain.capability_fallback)
        for resolved in chain.applied:
            passes.append(RenderPass(
                stage="effect",
                target=getattr(element, "element_id", ""),
                capability=Capability.SVG_RASTER,
                params={
                    "effect_type": resolved.declaration.effect_type,
                    "applied_effect_type": resolved.applied_effect_type,
                    "status": resolved.status,
                },
            ))
    metadata = getattr(scene, "metadata", {})
    for instance in read_instances(metadata):
        passes.append(RenderPass(stage="composite", target=instance.instance_id,
                                 capability=Capability.SVG_RASTER,
                                 params={"source_scene_id": instance.source_scene_id}))
    for mask in read_masks(metadata):
        passes.append(RenderPass(stage="effect", target=mask.target,
                                 capability=Capability.SVG_RASTER,
                                 params={"mask_id": mask.mask_id, "kind": mask.kind.value}))
    for matte in read_mattes(metadata):
        passes.append(RenderPass(stage="effect", target=matte.target,
                                 capability=Capability.SVG_RASTER,
                                 params={"matte_id": matte.matte_id, "source": matte.source, "mode": matte.mode.value}))
    emitters = read_particle_emitters(metadata)
    if emitters:
        passes.append(RenderPass(stage="effect", target="__particles__",
                                 capability=Capability.GPU_EFFECT,
                                 params={"emitter_count": len(emitters), "svg_fallback": True}))
    pipeline = read_scene_color_pipeline(metadata)
    if pipeline.is_hdr:
        passes.append(RenderPass(stage="composite", target="__color_pipeline__",
                                 capability=Capability.GPU_EFFECT,
                                 params={"working_space": pipeline.working_space, "bit_depth": pipeline.bit_depth, "tone_map": pipeline.tone_map}))
    return RenderPlan(
        passes=tuple(passes),
        required_capabilities=(Capability.SVG_RASTER,),
        cache_key_inputs={
            "effect_fallback": ",".join(sorted(capability_fallback)),
            "professional": hashlib.sha256(
                repr((tuple(item.to_dict() for item in emitters), pipeline.to_dict())).encode("utf-8")
            ).hexdigest(),
        },
        fail_policy="export_block" if profile.block_on_unsupported else "preview_placeholder",
    )


@dataclass(frozen=True)
class SvgRendererSession:
    """Holds the prepared inner renderer + resolved contexts (owned by V2)."""

    inner: SvgSceneRenderer
    render_context: RenderContext
    plan: RenderPlan


class SvgSceneRendererV2:
    """Frozen-Protocol wrapper around the legacy SVG renderer.

    Implements ``SceneRendererV2``.  Resources (the inner renderer, its asset
    resolver) are created in ``prepare`` and released in ``close``; nothing
    here touches the UI thread or any GPU context.
    """

    def __init__(self, cancellation: CancellationToken | None = None) -> None:
        self._cancellation = cancellation

    @property
    def renderer_id(self) -> str:
        return RENDERER_ID

    @property
    def renderer_build(self) -> str:
        return RENDERER_BUILD

    def prepare(self, scene: Any, context: FrameContext) -> RendererSession:
        render_context = frame_context_to_render_context(context)
        metadata = getattr(scene, "metadata", None)
        if not isinstance(metadata, Mapping) and isinstance(scene, Mapping):
            metadata = scene.get("metadata", {})
        pipeline = read_scene_color_pipeline(metadata)
        render_context = replace(
            render_context,
            working_color_space=pipeline.working_space,
            bit_depth=pipeline.bit_depth,
        )
        inner = SvgSceneRenderer(self._cancellation)
        inner.prepare(scene, render_context)
        document = getattr(inner, "_document", None) or scene
        profile_factory = {
            "draft": RenderProfile.draft,
            "interactive": RenderProfile.interactive,
            "preview": RenderProfile.preview,
            "final": RenderProfile.final,
        }.get(context.surface.proxy_tier.value, RenderProfile.preview)
        plan = build_svg_render_plan(document, profile_factory())
        return SvgRendererSession(inner=inner, render_context=render_context, plan=plan)  # type: ignore[return-value]

    def render(
        self,
        session: RendererSession,
        context: FrameContext,
        cancel_token: Any | None = None,
    ) -> RendererResult:
        if not isinstance(session, SvgRendererSession):
            raise TypeError("SvgSceneRendererV2.render expects an SvgRendererSession")
        if self._cancellation is not None and self._cancellation.is_cancelled:
            raise RuntimeError("render cancelled before start")
        # AUD-30410: ``cancel_token`` is the V2 protocol's cancellation
        # channel (the HTML-side implementation consumes it too).  Honour it
        # duck-typed before rasterizing so an already-cancelled job stops
        # here instead of completing a full CPU rasterization nobody wants.
        if cancel_token is not None and bool(getattr(cancel_token, "is_cancelled", False)):
            raise RenderCancelled("render cancelled before start")
        started = time.monotonic()
        animation_frame = AnimationFrame(context.frame, context.fps)
        rendered = session.inner.render_frame(animation_frame)
        # Rasterize the SVG to RGBA on the CPU (same QSvgRenderer path the host
        # already uses), so the V2 output is byte-identical to the legacy path.
        rgba = FrameAdapter.from_rendered_frame(rendered)
        rgba_frame = RenderedFrame(
            frame=rgba.frame,
            width=rgba.width,
            height=rgba.height,
            pixel_format="rgba8",
            data=rgba.data,
            backend="svg",
            metadata={
                **dict(rendered.metadata),
                "renderer_version": RENDERER_BUILD,
                "frame": context.frame,
            },
        )
        duration_ms = (time.monotonic() - started) * 1000.0
        evidence = RenderEvidence(
            renderer_id=self.renderer_id,
            renderer_build=self.renderer_build,
            capability_fallback=tuple(
                item for item in session.plan.cache_key_inputs.get("effect_fallback", "").split(",")
                if item
            ),
            render_duration_ms=duration_ms,
            frame_hash=hashlib.sha256(rgba.data).hexdigest(),
            diagnostics={
                "plan_stages": [p.stage for p in session.plan.passes],
                "alpha_mode": context.surface.alpha_mode.value,
                "colorspace": context.surface.colorspace.value,
                "proxy_tier": context.surface.proxy_tier.value,
                "dpr": context.surface.dpr,
                "color_pipeline": rendered.metadata.get("color_pipeline", {}),
                "hdr_fallback": rendered.metadata.get("hdr_fallback", ""),
            },
        )
        return RendererResult(frame=rgba_frame, evidence=evidence)

    def close(self, session: RendererSession) -> None:
        if isinstance(session, SvgRendererSession):
            session.inner.cleanup()


__all__ = [
    "RENDERER_BUILD",
    "RENDERER_ID",
    "SvgRendererSession",
    "SvgSceneRendererV2",
    "build_svg_render_plan",
    "frame_context_to_render_context",
]
