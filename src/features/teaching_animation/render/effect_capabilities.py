"""W4: scene-layer effect capability bridge (RD-02, RD-04, UX-04, UX-05).

The shared ``EffectSpec`` catalog (``src/features/shared/effects/``) is the
unified GPU effect description for Edit/Composite/Color.  The Teaching scene
layer does NOT run GLSL itself; it *declares* an effect chain on a layer and
asks "which backend can run this, and what happens when one cannot?".

This module is the renderer-neutral bridge:

* :class:`EffectDeclaration` — a lightweight, serializable scene-side effect
  reference (effect_type + params + version) that attaches to an element style.
* :class:`EffectCapabilityMatrix` — per-backend (svg/gpu/html) support for each
  catalog effect_type: EXACT / APPROXIMATE / UNSUPPORTED + cost + fallback.
  This is RD-04 ("SVG fallback capability matrix").
* :class:`RenderProfile` — draft/interactive/preview/final, mapping to the W0-B
  ``ProxyTier`` and a per-profile effect-drop policy (UX-04).
* :func:`resolve_effect_chain` — turn declarations + backend + profile into a
  resolved chain (which run, which drop/downgrade) plus diagnostics that feed
  the W0-B ``RenderPlan`` effect passes and ``RenderEvidence.capability_fallback``.
* :class:`DirtyRange` — frame range + bbox invalidated by an element change, for
  incremental cache invalidation (UX-04/UX-05).

Everything here is pure data/logic — no GLSL, no GPU — so it is fully testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..core.contracts import ProxyTier


class BackendEffectSupport(str, Enum):
    EXACT = "exact"                # backend renders the effect faithfully
    APPROXIMATE = "approximate"    # backend approximates (e.g. SVG filters)
    UNSUPPORTED = "unsupported"    # backend cannot render; drop or fallback


class RenderProfileName(str, Enum):
    DRAFT = "draft"
    INTERACTIVE = "interactive"
    PREVIEW = "preview"
    FINAL = "final"


@dataclass(frozen=True)
class EffectDeclaration:
    """A scene-side reference to a catalog effect, attached to an element/layer."""

    effect_type: str                        # e.g. "blur", "glow" — a catalog key
    params: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.effect_type:
            raise ValueError("effect_type cannot be empty")
        if self.version <= 0:
            raise ValueError("effect version must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {"effect_type": self.effect_type, "params": dict(self.params),
                "version": self.version, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EffectDeclaration":
        return cls(effect_type=str(data.get("effect_type", "")),
                   params=dict(data.get("params", {})),
                   version=int(data.get("version", 1)),
                   enabled=bool(data.get("enabled", True)))


def read_effect_declarations(style: Mapping[str, Any] | None) -> tuple[EffectDeclaration, ...]:
    """Read the versioned effect chain stored in an element style.

    ``effects`` is intentionally an additive style key.  Older scenes do not
    have it and therefore resolve to an empty chain.  Invalid entries are
    ignored here; scene validation/export preflight can report them without
    making the renderer unable to open an otherwise editable scene.
    """
    if not isinstance(style, Mapping):
        return ()
    raw = style.get("effects", ())
    if not isinstance(raw, (list, tuple)):
        return ()
    declarations: list[EffectDeclaration] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            declarations.append(EffectDeclaration.from_dict(item))
        except (TypeError, ValueError):
            continue
    return tuple(declarations)


def write_effect_declarations(
    style: Mapping[str, Any] | None,
    declarations: tuple[EffectDeclaration, ...],
) -> dict[str, Any]:
    """Return a detached style mapping with a serialized effect chain."""
    updated = dict(style or {})
    updated["effects"] = [declaration.to_dict() for declaration in declarations]
    return updated


def read_scene_gpu_effects(scene: Any) -> tuple[EffectDeclaration, ...]:
    """Read an optional scene-level GPU chain for Composite integration.

    Element chains remain renderer-neutral and are consumed by each backend.
    ``metadata.gpu_effects`` is an explicit scene-level post-composite chain;
    it exists for the current Composite source-node bridge until the retained
    GPU scene renderer can execute separate element surfaces.
    """
    metadata = getattr(scene, "metadata", None)
    if not isinstance(metadata, Mapping):
        return ()
    raw = metadata.get("gpu_effects", ())
    if not isinstance(raw, (list, tuple)):
        return ()
    return read_effect_declarations({"effects": raw})


@dataclass(frozen=True)
class BackendEffectCapability:
    """One cell of the capability matrix."""

    support: BackendEffectSupport = BackendEffectSupport.UNSUPPORTED
    cost: str = "medium"                    # low | medium | high
    fallback_effect_type: str | None = None  # suggested replacement


# Per-backend matrix.  GPU = exact for the catalog's GPU effects; SVG approximates
# the filter-primitive subset; HTML (CSS filter) covers blur/shadow/glow via CSS
# but only when the W0-A gate passes.  Unknown effect_types default to UNSUPPORTED.
_SVG_MATRIX: dict[str, BackendEffectCapability] = {
    "blur": BackendEffectCapability(BackendEffectSupport.APPROXIMATE, "medium"),
    "shadow": BackendEffectCapability(BackendEffectSupport.APPROXIMATE, "medium"),
    "color_matrix": BackendEffectCapability(BackendEffectSupport.EXACT, "low"),
    "displacement": BackendEffectCapability(BackendEffectSupport.APPROXIMATE, "high"),
    "glow": BackendEffectCapability(BackendEffectSupport.UNSUPPORTED, "high"),
    "sharpen": BackendEffectCapability(BackendEffectSupport.UNSUPPORTED, "medium"),
    "defocus": BackendEffectCapability(BackendEffectSupport.UNSUPPORTED, "high"),
    "denoise": BackendEffectCapability(BackendEffectSupport.UNSUPPORTED, "high"),
    "edge_detect": BackendEffectCapability(BackendEffectSupport.APPROXIMATE, "medium"),
    "motion_blur": BackendEffectCapability(BackendEffectSupport.UNSUPPORTED, "high"),
}

_GPU_MATRIX: dict[str, BackendEffectCapability] = {
    "blur": BackendEffectCapability(BackendEffectSupport.EXACT, "medium"),
    "shadow": BackendEffectCapability(BackendEffectSupport.EXACT, "medium"),
    "color_matrix": BackendEffectCapability(BackendEffectSupport.EXACT, "low"),
    "displacement": BackendEffectCapability(BackendEffectSupport.EXACT, "high"),
    "glow": BackendEffectCapability(BackendEffectSupport.EXACT, "high"),
    "sharpen": BackendEffectCapability(BackendEffectSupport.EXACT, "medium"),
    "defocus": BackendEffectCapability(BackendEffectSupport.EXACT, "high"),
    "denoise": BackendEffectCapability(BackendEffectSupport.EXACT, "high"),
    "edge_detect": BackendEffectCapability(BackendEffectSupport.EXACT, "medium"),
    "motion_blur": BackendEffectCapability(BackendEffectSupport.EXACT, "high"),
}

_HTML_MATRIX: dict[str, BackendEffectCapability] = {
    "blur": BackendEffectCapability(BackendEffectSupport.EXACT, "low"),
    "shadow": BackendEffectCapability(BackendEffectSupport.EXACT, "low"),
    "glow": BackendEffectCapability(BackendEffectSupport.EXACT, "medium"),
    "color_matrix": BackendEffectCapability(BackendEffectSupport.APPROXIMATE, "medium"),
    "displacement": BackendEffectCapability(BackendEffectSupport.UNSUPPORTED, "high"),
    "sharpen": BackendEffectCapability(BackendEffectSupport.UNSUPPORTED, "medium"),
    "motion_blur": BackendEffectCapability(BackendEffectSupport.UNSUPPORTED, "high"),
}

_BACKENDS: dict[str, dict[str, BackendEffectCapability]] = {
    "svg": _SVG_MATRIX, "gpu": _GPU_MATRIX, "html": _HTML_MATRIX,
}


class EffectCapabilityMatrix:
    """Per-backend support lookup for catalog effect_types."""

    def __init__(self, backends: Mapping[str, Mapping[str, BackendEffectCapability]] | None = None) -> None:
        self._backends = {bid: dict(matrix) for bid, matrix in (backends or _BACKENDS).items()}

    def support(self, backend: str, effect_type: str) -> BackendEffectCapability:
        matrix = self._backends.get(backend, {})
        return matrix.get(effect_type, BackendEffectCapability())

    def register(self, backend: str, effect_type: str, capability: BackendEffectCapability) -> None:
        self._backends.setdefault(backend, {})[effect_type] = capability

    def backends(self) -> tuple[str, ...]:
        return tuple(sorted(self._backends))


@dataclass(frozen=True)
class RenderProfile:
    """A named render quality tier that maps to a ProxyTier + effect policy."""

    name: RenderProfileName
    proxy_tier: ProxyTier
    drop_high_cost: bool = False       # drop "high" cost effects in this profile
    block_on_unsupported: bool = False # export fails closed when an effect is unsupported

    @classmethod
    def draft(cls) -> "RenderProfile":
        return cls(RenderProfileName.DRAFT, ProxyTier.DRAFT, drop_high_cost=True, block_on_unsupported=False)

    @classmethod
    def interactive(cls) -> "RenderProfile":
        return cls(RenderProfileName.INTERACTIVE, ProxyTier.INTERACTIVE, drop_high_cost=True, block_on_unsupported=False)

    @classmethod
    def preview(cls) -> "RenderProfile":
        return cls(RenderProfileName.PREVIEW, ProxyTier.PREVIEW, drop_high_cost=False, block_on_unsupported=False)

    @classmethod
    def final(cls) -> "RenderProfile":
        return cls(RenderProfileName.FINAL, ProxyTier.FINAL, drop_high_cost=False, block_on_unsupported=True)


@dataclass(frozen=True)
class ResolvedEffect:
    declaration: EffectDeclaration
    support: BackendEffectSupport
    cost: str
    applied_effect_type: str           # may differ from declaration if fallback applied
    status: str                        # run | approximated | dropped | blocked | fallback


@dataclass(frozen=True)
class ResolvedEffectChain:
    """Outcome of resolving a declared chain against a backend + profile."""

    applied: tuple[ResolvedEffect, ...]
    diagnostics: tuple[str, ...]
    blocked: bool                       # True if export profile blocks on unsupported

    @property
    def capability_fallback(self) -> tuple[str, ...]:
        return tuple(f"{e.declaration.effect_type}:{e.status}" for e in self.applied if e.status != "run")


def resolve_effect_chain(
    declarations: tuple[EffectDeclaration, ...],
    backend: str,
    profile: RenderProfile,
    matrix: EffectCapabilityMatrix | None = None,
) -> ResolvedEffectChain:
    """Resolve a declared effect chain for one backend + profile (RD-02/RD-04)."""
    matrix = matrix or EffectCapabilityMatrix()
    applied: list[ResolvedEffect] = []
    diagnostics: list[str] = []
    blocked = False
    for declaration in declarations:
        if not declaration.enabled:
            applied.append(ResolvedEffect(declaration, BackendEffectSupport.EXACT, "low",
                                          declaration.effect_type, "dropped"))
            diagnostics.append(f"{declaration.effect_type}: disabled by author")
            continue
        cap = matrix.support(backend, declaration.effect_type)
        if profile.drop_high_cost and cap.cost == "high":
            applied.append(ResolvedEffect(declaration, cap.support, cap.cost,
                                          declaration.effect_type, "dropped"))
            diagnostics.append(f"{declaration.effect_type}: dropped (high cost in {profile.name.value} profile)")
            continue
        if cap.support == BackendEffectSupport.UNSUPPORTED:
            if cap.fallback_effect_type and matrix.support(backend, cap.fallback_effect_type).support != BackendEffectSupport.UNSUPPORTED:
                applied.append(ResolvedEffect(declaration, cap.support, cap.cost,
                                              cap.fallback_effect_type, "fallback"))
                diagnostics.append(f"{declaration.effect_type}: unsupported, fallback to {cap.fallback_effect_type}")
            elif profile.block_on_unsupported:
                applied.append(ResolvedEffect(declaration, cap.support, cap.cost,
                                              declaration.effect_type, "blocked"))
                diagnostics.append(f"{declaration.effect_type}: unsupported and export blocks")
                blocked = True
            else:
                applied.append(ResolvedEffect(declaration, cap.support, cap.cost,
                                              declaration.effect_type, "dropped"))
                diagnostics.append(f"{declaration.effect_type}: unsupported, dropped in {backend}")
        elif cap.support == BackendEffectSupport.APPROXIMATE:
            applied.append(ResolvedEffect(declaration, cap.support, cap.cost,
                                          declaration.effect_type, "approximated"))
            diagnostics.append(f"{declaration.effect_type}: approximated on {backend}")
        else:
            applied.append(ResolvedEffect(declaration, cap.support, cap.cost,
                                          declaration.effect_type, "run"))
    return ResolvedEffectChain(tuple(applied), tuple(diagnostics), blocked)


@dataclass(frozen=True)
class DirtyRange:
    """Frame range + bbox invalidated by an element/animation change (UX-04/05)."""

    start_frame: int
    end_frame: int                      # exclusive
    bbox_x: float = 0.0
    bbox_y: float = 0.0
    bbox_width: float | None = None     # None = full canvas
    bbox_height: float | None = None

    def __post_init__(self) -> None:
        if self.start_frame < 0:
            raise ValueError("start_frame must be non-negative")
        if self.end_frame <= self.start_frame:
            raise ValueError("end_frame must exceed start_frame")

    @property
    def is_full_canvas(self) -> bool:
        return self.bbox_width is None or self.bbox_height is None

    def overlaps(self, other: "DirtyRange") -> bool:
        if self.end_frame <= other.start_frame or other.end_frame <= self.start_frame:
            return False
        return True

    def union(self, other: "DirtyRange") -> "DirtyRange":
        start = min(self.start_frame, other.start_frame)
        end = max(self.end_frame, other.end_frame)
        if self.is_full_canvas or other.is_full_canvas:
            return DirtyRange(start, end)
        x0 = min(self.bbox_x, other.bbox_x)
        y0 = min(self.bbox_y, other.bbox_y)
        x1 = max(self.bbox_x + (self.bbox_width or 0.0), other.bbox_x + (other.bbox_width or 0.0))
        y1 = max(self.bbox_y + (self.bbox_height or 0.0), other.bbox_y + (other.bbox_height or 0.0))
        return DirtyRange(start, end, x0, y0, x1 - x0, y1 - y0)


def dirty_range_for_element(
    element_id: str,
    *,
    active_frames: tuple[int, int],
    bbox: tuple[float, float, float, float] | None = None,
) -> DirtyRange:
    """Build a DirtyRange for one element's time window + optional bbox."""
    start, end = active_frames
    if bbox is None:
        return DirtyRange(start, end)
    return DirtyRange(start, end, bbox[0], bbox[1], bbox[2], bbox[3])


__all__ = [
    "BackendEffectCapability",
    "BackendEffectSupport",
    "DirtyRange",
    "EffectCapabilityMatrix",
    "EffectDeclaration",
    "read_effect_declarations",
    "read_scene_gpu_effects",
    "write_effect_declarations",
    "RenderProfile",
    "RenderProfileName",
    "ResolvedEffect",
    "ResolvedEffectChain",
    "dirty_range_for_element",
    "resolve_effect_chain",
]
