"""Editable layer styles: versioned effect data and compositing math (B08).

Style stacks map to versioned :class:`LayerStyleEffect` data with an
explicit order; the compositing math here is the shared reference
implementation (CPU arrays) — preview and export must agree with it, and
the drop-shadow golden below is the pixel contract.  Original PSD style
descriptions, the conversion version and the source identity ride along so
an unreproducible style can fall back to B07's merged semantics with a
diagnostic instead of silently degrading.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

STYLE_DATA_VERSION = 1

# Kind registry — the first batch (B08 §1) is verified against PSD output.
STYLE_KIND_COLOR_OVERLAY = "color_overlay"
STYLE_KIND_DROP_SHADOW = "drop_shadow"
STYLE_KIND_OUTER_GLOW = "outer_glow"
# Planned, not yet verified → stay merged (diagnosed), never guessed.
STYLE_KIND_STROKE = "stroke"
STYLE_KIND_INNER_SHADOW = "inner_shadow"
STYLE_KIND_GRADIENT_OVERLAY = "gradient_overlay"

EDITABLE_STYLE_KINDS = frozenset({
    STYLE_KIND_COLOR_OVERLAY, STYLE_KIND_DROP_SHADOW, STYLE_KIND_OUTER_GLOW,
})
KNOWN_STYLE_KINDS = EDITABLE_STYLE_KINDS | {
    STYLE_KIND_STROKE, STYLE_KIND_INNER_SHADOW, STYLE_KIND_GRADIENT_OVERLAY,
}


@dataclass(frozen=True)
class LayerStyleEffect:
    """One style, versioned, with its PSD provenance (B08 §3)."""

    kind: str
    order: int                      # explicit application order
    params: Dict[str, Any] = field(default_factory=dict)
    # Original PSD style description + conversion identity for round-trips.
    source_description: str = ""
    conversion_version: int = STYLE_DATA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "order": int(self.order),
            "params": dict(self.params),
            "source_description": self.source_description,
            "conversion_version": int(self.conversion_version),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["LayerStyleEffect"]:
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        if not isinstance(kind, str) or kind not in KNOWN_STYLE_KINDS:
            return None
        params = data.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            return cls(
                kind=kind, order=int(data.get("order", 0) or 0),
                params=dict(params),
                source_description=str(data.get("source_description", "") or ""),
                conversion_version=int(
                    data.get("conversion_version", STYLE_DATA_VERSION)
                    or STYLE_DATA_VERSION),
            )
        except (TypeError, ValueError):
            return None

    def fingerprint(self) -> str:
        payload = f"{self.kind}|{self.order}|{sorted(self.params.items())}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class StyleStackDiagnostic:
    """Why a style (or stack) stays merged instead of editable."""

    kind: str
    reason: str   # unknown_kind | unverified_kind | param_mismatch

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "reason": self.reason}


def plan_style_stack(styles: Sequence[LayerStyleEffect]
                     ) -> Tuple[Tuple[LayerStyleEffect, ...],
                                Tuple[StyleStackDiagnostic, ...]]:
    """Split a stack into editable effects and merge-requiring diagnostics.

    Unverified or unknown kinds keep B07's merged semantics — never a
    silent approximation (B08 §3).
    """
    editable: List[LayerStyleEffect] = []
    diagnostics: List[StyleStackDiagnostic] = []
    for style in sorted(styles, key=lambda s: s.order):
        if style.kind in EDITABLE_STYLE_KINDS:
            editable.append(style)
        elif style.kind in KNOWN_STYLE_KINDS:
            diagnostics.append(StyleStackDiagnostic(
                style.kind, "unverified_kind"))
        else:
            diagnostics.append(StyleStackDiagnostic(style.kind, "unknown_kind"))
    return tuple(editable), tuple(diagnostics)


# ── Compositing math (shared reference implementation) ─────────────────────


def shadow_under(foreground: Tuple[float, float, float, float],
                 shadow_rgb: Tuple[float, float, float],
                 shadow_alpha: float) -> Tuple[float, float, float, float]:
    """Composite a shadow pixel *under* straight-alpha foreground (B08 golden).

    Input color space is the composition working space; the shadow alpha
    has already been blurred and offset upstream.  The foreground is NOT
    premultiplied here and the source layer opacity must not be applied a
    second time.
    """
    r, g, b, a = foreground
    sr, sg, sb = shadow_rgb
    coverage = (1.0 - a) * shadow_alpha
    return (r + sr * coverage, g + sg * coverage, b + sb * coverage,
            a + coverage)


def color_over(foreground: Tuple[float, float, float, float],
               color_rgb: Tuple[float, float, float],
               opacity: float) -> Tuple[float, float, float, float]:
    """Color overlay *over* straight-alpha foreground (source layer opacity
    applies exactly once via ``opacity``)."""
    r, g, b, a = foreground
    cr, cg, cb = color_rgb
    w = float(opacity)
    return (r * (1.0 - w) + cr * w,
            g * (1.0 - w) + cg * w,
            b * (1.0 - w) + cb * w,
            a)


def style_halo_extent(params: Dict[str, Any], default_radius: float = 0.0
                      ) -> float:
    """Tile halo window for a shadow/glow: filter radius + offset distance.

    The halo includes the blur filter radius and the shadow offset so a
    tile never clips the effect at its border (B08 §2).
    """
    try:
        radius = float(params.get("radius", default_radius) or 0.0)
        offset_x = float(params.get("offset_x", 0.0) or 0.0)
        offset_y = float(params.get("offset_y", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    offset = (offset_x ** 2 + offset_y ** 2) ** 0.5
    return radius * 2.0 + offset


def apply_style_stack_rgba(foreground_rgba, styles: Sequence[LayerStyleEffect],
                           ) -> "Any":
    """Apply an ordered editable stack to an ``(h, w, 4)`` float array.

    Pure-CPU reference path used by tests and diagnostics: the shadow
    alpha is blurred upstream of this call (pass pre-blurred alpha via the
    style params' ``shadow_alpha_plane``); without it the shadow uses the
    layer alpha directly with the style's opacity.  HDR values pass
    through unclamped.
    """
    import numpy as np

    image = np.asarray(foreground_rgba, dtype=np.float32).copy()
    if image.ndim != 3 or image.shape[2] < 4:
        raise ValueError("apply_style_stack_rgba expects (h, w, 4) RGBA")
    for style in sorted(styles, key=lambda s: s.order):
        if style.kind == STYLE_KIND_COLOR_OVERLAY:
            color = style.params.get("color", (1.0, 1.0, 1.0))
            opacity = float(style.params.get("opacity", 1.0) or 0.0)
            w = min(max(opacity, 0.0), 1.0)
            image[..., 0] = image[..., 0] * (1.0 - w) + color[0] * w
            image[..., 1] = image[..., 1] * (1.0 - w) + color[1] * w
            image[..., 2] = image[..., 2] * (1.0 - w) + color[2] * w
        elif style.kind in (STYLE_KIND_DROP_SHADOW, STYLE_KIND_OUTER_GLOW):
            shadow_rgb = tuple(style.params.get("color", (0.0, 0.0, 0.0)))
            shadow_opacity = float(style.params.get("opacity", 1.0) or 0.0)
            plane = style.params.get("shadow_alpha_plane")
            alpha = (np.asarray(plane, dtype=np.float32)
                     if plane is not None else image[..., 3])
            shadow_alpha = np.clip(alpha * shadow_opacity, 0.0, 1.0)
            coverage = (1.0 - image[..., 3]) * shadow_alpha
            for c in range(3):
                image[..., c] = image[..., c] + shadow_rgb[c] * coverage
            image[..., 3] = image[..., 3] + coverage
    return image
