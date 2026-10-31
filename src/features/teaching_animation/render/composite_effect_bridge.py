"""Composite-side execution bridge for Teaching scene effect declarations.

The bridge deliberately delegates compilation and scratch-texture ownership to
the shared ``EffectPassRunner``.  It is only called from a Composite render
thread by ``MotionGraphicNode``; no UI code receives a GL object.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .effect_capabilities import (
    EffectDeclaration,
    RenderProfile,
    ResolvedEffectChain,
    resolve_effect_chain,
)
from ..core.professional import SceneColorPipeline


@dataclass(frozen=True)
class CompositeEffectResult:
    """Texture result plus the capability evidence for one effect chain."""

    texture_id: int
    width: int
    height: int
    chain: ResolvedEffectChain


class CompositeEffectBridge:
    """Run shared catalog effects on one current Composite GL context."""

    def __init__(self, gl_context: Any) -> None:
        self._gl_context = gl_context
        self._runner: Any | None = None

    def _ensure_runner(self) -> Any:
        if self._runner is None:
            from src.features.shared.effects.effect_pass_runner import EffectPassRunner

            self._runner = EffectPassRunner(self._gl_context)
        return self._runner

    def apply(
        self,
        input_texture_id: int,
        width: int,
        height: int,
        declarations: tuple[EffectDeclaration, ...],
        *,
        profile: RenderProfile,
        dtype: str = "f1",
        color_pipeline: SceneColorPipeline | None = None,
    ) -> CompositeEffectResult:
        """Run declarations in order, returning the final texture glo.

        The resolver is shared with SVG/HTML planning.  GPU declarations in
        the current catalog (blur, glow, sharpen, and the other catalog
        effects) execute as exact shared ``EffectSpec`` passes.  A missing
        catalog entry is diagnosed and leaves the input unchanged.
        """
        if color_pipeline is not None:
            dtype = dtype_for_color_pipeline(color_pipeline)
        chain = resolve_effect_chain(declarations, "gpu", profile)
        current = input_texture_id
        if current <= 0 or not declarations:
            return CompositeEffectResult(current, width, height, chain)

        from src.features.shared.effects import get_effect_catalog

        catalog = get_effect_catalog()
        runner = self._ensure_runner()
        for resolved in chain.applied:
            if resolved.status not in {"run", "approximated", "fallback"}:
                continue
            effect_type = resolved.applied_effect_type
            params = dict(resolved.declaration.params)
            if effect_type == "motion_blur":
                effect_type = "blur"
                params = {"blur_type": "motion", **params}
            spec = catalog.get_spec(effect_type)
            if spec is None:
                continue
            current = runner.run(
                spec,
                current,
                params,
                width,
                height,
                dtype=dtype,
            )
        return CompositeEffectResult(current, width, height, chain)

    def cleanup(self) -> None:
        """Release programs and scratch textures on the owning GL thread."""
        if self._runner is not None:
            self._runner.release()
            self._runner = None

    def resource_diagnostics(self) -> dict[str, Any]:
        """Return a context-local texture/FBO snapshot for soak evidence."""
        if self._runner is None:
            return {
                "allocated": False,
                "texture_count": 0,
                "fbo_count": 0,
                "program_cache_size": 0,
                "vao_cache_size": 0,
            }
        return self._runner.get_surface_diagnostics()


def dtype_for_color_pipeline(pipeline: SceneColorPipeline) -> str:
    """Map the scene color contract to ModernGL's float texture dtype."""
    if pipeline.bit_depth >= 32:
        return "f4"
    if pipeline.is_hdr or pipeline.bit_depth > 8:
        return "f2"
    return "f1"


__all__ = ["CompositeEffectBridge", "CompositeEffectResult", "dtype_for_color_pipeline"]
