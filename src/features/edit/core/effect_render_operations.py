"""FX-C02: hierarchical render-operation plan for Adjustment Clips.

Audit FX-C02 (the most important correctness bug): Adjustment Clips were
applied as a FULL-SCREEN post-composite pass AFTER all tracks were
composited, so they incorrectly affected the tracks ABOVE them. The
DaVinci Adjustment Clip contract is the opposite — an adjustment
influences ONLY the layers below it, never the layers above.

This module expresses the fix as pure data: a flat, ordered tuple of
``RenderOperation`` values that a renderer executes top-to-bottom. The
ordering encodes the scope rule:

    Composite the bottom layer → Composite the next layer up → …
    → when an Adjustment Clip is reached, APPLY ITS EFFECT CHAIN and its
      Transform/Opacity to the accumulated result SO FAR (i.e. everything below it) →
    → continue compositing the layers above (untouched by that adj).

Because it is pure data, the same plan drives GPU preview, CPU fallback
and export (audit P2/P3), and it can be unit-tested without any GL
fixture — which is exactly what the audit's P0 test matrix requires.

The builder is additive: it does NOT replace ``FrameRenderPlan`` (whose
``layers`` still include adjustment clips for backwards compatibility).

Runtime status (AUD-16808): the GPU preview renderer no longer runs the
legacy full-screen post-composite pass. When adjustment layers are
present it composites with z-order SCOPE via
``EditGpuPreviewRenderer._composite_with_adjustment_scope`` (whose
ordering contract is the pure-data ``_build_scoped_composite_work``),
so the FX-C02 correctness rule is enforced in production. The plan now
also has a production CONSUMER: ``timeline_preview_renderer`` builds the
ops each frame and drives its adjustment / solid layer classification
from them (single classification authority — the producer and the GPU
scoped compositor can no longer drift apart). The module stays the
renderer-independent, GL-free statement of the scope contract for
export / CPU fallback when they migrate onto the plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from src.features.edit.core.timeline_render_plan import ClipLayerInfo, FrameRenderPlan


# ── Asset-type resolver protocol ─────────────────────────────────────

class AssetTypeResolver(Protocol):
    """Resolves an asset_id to its media_type string.

    Mirrors the ``_asset_resolver.get_asset_type`` interface the preview
    renderer already uses, so the same resolver can be injected here.
    """

    def get_asset_type(self, asset_id: str) -> Optional[str]: ...


# ── Operation types ──────────────────────────────────────────────────

@dataclass(frozen=True)
class RenderClip:
    """Composite a normal (non-adjustment) layer onto the accumulator.

    Carries the original ``ClipLayerInfo`` so each renderer can pull the
    decode frame / transform / opacity / crop it already understands.
    """

    layer: ClipLayerInfo
    # True when this clip is a generator that produces a solid colour
    # (Solid Color) rather than a decoded media frame. Lets renderers pick
    # their paint path without re-resolving the asset type.
    is_solid: bool = False


@dataclass(frozen=True)
class ApplyAdjustment:
    """Apply an Adjustment Clip's effects and native controls to the result.

    Per audit rule 3.1.2/3.1.3, this is inserted at the adjustment's own
    z-order position: only the layers BELOW it have been composited into
    the accumulator at this point, so the effect cannot leak upward. The
    layers above are emitted as later ``RenderClip`` ops.
    """

    layer: ClipLayerInfo
    # The adjustment's effect chain snapshot (tuple of EffectInstance),
    # frozen at plan-build time so the render thread is race-free.
    effects: tuple[Any, ...]
    # Adjustment-native geometry/compositing controls.  They operate on the
    # accumulated result below this operation, before later (upper) layers.
    transform: tuple[Any, ...] = ()
    opacity: float = 1.0
    # T-P1-22 ColorStack 参数快照（dict 或 None）。与 timeline_preview_renderer
    # 下发的三元组第三项一致：仅有 grade、无 effects 的调整剪辑不是 no-op。
    grade: Optional[dict] = None


# A render operation is one of the above.
RenderOperation = RenderClip | ApplyAdjustment


# ── Builder ──────────────────────────────────────────────────────────

# Default asset type for generator clips.
_ADJUSTMENT_TYPE = "adjustment"
_SOLID_TYPE = "solid"


def build_render_operations(
    frame_plan: FrameRenderPlan,
    asset_resolver: Optional[AssetTypeResolver] = None,
    effects_snapshot: Optional[Callable[[ClipLayerInfo], tuple[Any, ...]]] = None,
    grade_snapshot: Optional[Callable[[ClipLayerInfo], Optional[dict]]] = None,
) -> tuple[RenderOperation, ...]:
    """Build the ordered render-operation list for a single frame.

    Args:
        frame_plan: the per-frame plan (``layers`` are bottom→top by
            track index, per ``FrameRenderPlan`` docstring).
        asset_resolver: resolves asset_id → media_type. Required to tell
            adjustment / solid layers apart from normal media. When None,
            no layer is treated as adjustment/solid (legacy behaviour).
        effects_snapshot: callable returning the effect chain for an
            adjustment layer (the same snapshot the preview renderer
            builds). When None, the adjustment's ``clip.effects`` tuple
            is used directly.
        grade_snapshot: callable returning the adjustment's ColorStack
            参数快照（dict 或 None，T-P1-22 三元组第三项）。When None,
            adjustments carry ``grade=None``（纯 effects/Transform 路径，
            如既有单测）。

    Returns:
        Ordered tuple of ``RenderOperation``. A normal layer becomes a
        ``RenderClip``; an adjustment layer becomes an ``ApplyAdjustment``
        at its own z-position. The result is a flat sequence a renderer
        walks top-to-bottom: composite up to the adjustment, apply it,
        keep compositing above.

    FX-C02 scope guarantee: because ``ApplyAdjustment`` is emitted IN
    PLACE (not appended after all composites), a renderer following this
    order can never apply an adjustment to a layer above it.
    """
    if not frame_plan.has_content:
        return ()

    # Layers arrive bottom→top by track index (see FrameRenderPlan). The
    # operation list preserves that order; adjustments are inserted where
    # they sit, so each one applies to exactly the layers below it.
    ops: list[RenderOperation] = []
    for layer in frame_plan.layers:
        asset_type = (
            asset_resolver.get_asset_type(layer.asset_id)
            if asset_resolver is not None
            else None
        )

        if asset_type == _ADJUSTMENT_TYPE:
            if effects_snapshot is not None:
                effects = effects_snapshot(layer)
            else:
                effects = tuple(getattr(layer.clip, "effects", ()) or ())
            grade = grade_snapshot(layer) if grade_snapshot is not None else None
            transform = tuple(getattr(layer, "transform", ()) or ())
            opacity = float(getattr(layer, "opacity", 1.0))
            from src.features.edit.core.adjustment_composite import transform_is_identity
            # An identity adjustment with no effects AND no ColorStack grade
            # is a no-op (a grade-only adjustment still grades the frame —
            # the T-P1-22 producer contract).  Non-identity Transform or
            # Opacity must still be emitted as an operation.
            if (
                not effects
                and grade is None
                and (not transform or transform_is_identity(transform))
                and abs(opacity - 1.0) <= 1e-4
            ):
                continue
            ops.append(ApplyAdjustment(
                layer=layer,
                effects=effects,
                transform=transform,
                opacity=opacity,
                grade=grade,
            ))
        elif asset_type == _SOLID_TYPE:
            ops.append(RenderClip(layer=layer, is_solid=True))
        else:
            ops.append(RenderClip(layer=layer, is_solid=False))

    return tuple(ops)


def adjustment_indices(ops: tuple[RenderOperation, ...]) -> tuple[int, ...]:
    """Positions of every ``ApplyAdjustment`` op — handy for tests/debug."""
    return tuple(i for i, op in enumerate(ops) if isinstance(op, ApplyAdjustment))


def clip_indices(ops: tuple[RenderOperation, ...]) -> tuple[int, ...]:
    """Positions of every ``RenderClip`` op."""
    return tuple(i for i, op in enumerate(ops) if isinstance(op, RenderClip))
