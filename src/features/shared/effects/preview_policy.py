"""FX-M04: Preview Quality Policy — unified, declarative degradation decisions.

Design goals (from the audit plan):
1. Immutable ``PreviewPolicyDecision`` per effect per frame: full / lower_resolution /
   parameter_cap / bypass, each carrying a reason, estimated cost, and export behavior.
2. Every built-in and plugin EffectSpec declares supported degradation modes via
   ``DegradationDecl``; no declaration → explicit default (full or bypass), never
   silent hardcoding scattered in the renderer.
3. The renderer asks the policy engine once per frame for a decision list; playback
   stop / paused / export → all decisions revert to FULL.
4. Inspector badges and viewer HUD consume the same decision objects for display.
5. Decisions NEVER write back to ``EffectInstance.params``; capped values live only
   in the frame-local evaluation context.

Integration points:
- ``EditGpuPreviewRenderer._apply_clip_effects`` calls
  ``PreviewPolicyEngine.evaluate_chain(...)`` instead of the old ad-hoc
  ``_degrade_params_for_preview``.
- ``EffectSpec.preview_cost`` seeds the default declaration when no explicit
  ``DegradationDecl`` is registered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ================================================================
# Degradation mode enum
# ================================================================

class DegradationMode(Enum):
    """How an effect may be degraded during real-time preview."""
    FULL = auto()              # No degradation; run at full quality.
    LOWER_RESOLUTION = auto()  # Run at reduced internal resolution.
    PARAMETER_CAP = auto()     # Clamp expensive parameters to safe caps.
    BYPASS = auto()            # Skip the effect entirely during playback.

    @property
    def is_degraded(self) -> bool:
        """Whether this mode is anything other than FULL."""
        return self != DegradationMode.FULL


# ================================================================
# Degradation declaration (per effect type)
# ================================================================

@dataclass(frozen=True)
class DegradationDecl:
    """Declares what degradation modes an effect supports and how.

    Registered per effect_type. The policy engine consults this to decide
    which mode to apply under budget pressure.

    Attributes:
        supported_modes: Modes this effect can operate in (must include FULL).
        param_caps: For PARAMETER_CAP mode — {param_name: max_value}.
        resolution_scale: For LOWER_RESOLUTION mode — fraction of full res
            (e.g. 0.5 = half resolution).
        export_behavior: What happens at export time. Always "full" for
            built-in effects; plugins may declare "blocked" or "degraded".
        cost_estimate_ms: Rough per-frame GPU cost at full quality (for
            budget allocation). 0 means negligible.
    """
    supported_modes: FrozenSet[DegradationMode] = frozenset({DegradationMode.FULL})
    param_caps: Dict[str, float] = field(default_factory=dict)
    resolution_scale: float = 1.0
    export_behavior: str = "full"  # "full" | "blocked" | "degraded"
    cost_estimate_ms: float = 0.0

    def __post_init__(self) -> None:
        if DegradationMode.FULL not in self.supported_modes:
            object.__setattr__(
                self, "supported_modes",
                self.supported_modes | {DegradationMode.FULL},
            )


# ================================================================
# Preview policy decision (immutable, per effect per frame)
# ================================================================

@dataclass(frozen=True)
class PreviewPolicyDecision:
    """Immutable decision for one effect in one frame.

    The renderer reads ``mode`` and ``capped_params`` to configure execution.
    The UI reads ``reason`` and ``mode`` for badge/HUD display.
    Nothing here mutates the project model.
    """
    effect_id: str
    effect_type: str
    mode: DegradationMode
    reason: str = ""
    capped_params: Optional[Dict[str, Any]] = None
    export_behavior: str = "full"
    cost_estimate_ms: float = 0.0

    @property
    def is_degraded(self) -> bool:
        """Whether the decision degrades this effect for this frame."""
        return self.mode.is_degraded

    @property
    def badge_text(self) -> str:
        """Short label for Inspector/HUD display."""
        return {
            DegradationMode.FULL: "",
            DegradationMode.LOWER_RESOLUTION: "½-res",
            DegradationMode.PARAMETER_CAP: "capped",
            DegradationMode.BYPASS: "bypassed",
        }.get(self.mode, "")


# ================================================================
# Policy context (frame-local inputs)
# ================================================================

@dataclass(frozen=True)
class PolicyContext:
    """Inputs the policy engine uses to make decisions for one frame.

    Constructed fresh each frame by the renderer; never stored on the
    project model.
    """
    width: int
    height: int
    is_playing: bool = False
    gpu_budget_ms: float = 16.0  # target frame time budget for effects
    force_full: bool = False   # True when paused / export / scrubbing single frame


# ================================================================
# Default declarations registry
# ================================================================

# Built-in declarations derived from the existing _PREVIEW_PARAM_CAPS and
# EffectSpec.preview_cost values. Plugin effects without an explicit
# declaration get _DEFAULT_DECL (full only, no degradation).
_BUILTIN_DECLS: Dict[str, DegradationDecl] = {
    "blur": DegradationDecl(
        supported_modes=frozenset({
            DegradationMode.FULL, DegradationMode.PARAMETER_CAP,
        }),
        param_caps={"radius": 8.0, "radius_x": 8.0, "radius_y": 8.0},
        export_behavior="full",
        cost_estimate_ms=3.0,
    ),
    "defocus": DegradationDecl(
        supported_modes=frozenset({
            DegradationMode.FULL, DegradationMode.PARAMETER_CAP,
        }),
        param_caps={"max_radius": 8.0},
        export_behavior="full",
        cost_estimate_ms=4.0,
    ),
    "glow": DegradationDecl(
        supported_modes=frozenset({
            DegradationMode.FULL, DegradationMode.PARAMETER_CAP,
            DegradationMode.LOWER_RESOLUTION,
        }),
        param_caps={"radius": 8.0},
        resolution_scale=0.5,
        export_behavior="full",
        cost_estimate_ms=5.0,
    ),
    "erode_dilate": DegradationDecl(
        supported_modes=frozenset({
            DegradationMode.FULL, DegradationMode.PARAMETER_CAP,
        }),
        param_caps={"size": 8.0},
        export_behavior="full",
        cost_estimate_ms=2.0,
    ),
    "sharpen": DegradationDecl(
        supported_modes=frozenset({DegradationMode.FULL}),
        export_behavior="full",
        cost_estimate_ms=1.0,
    ),
    "denoise": DegradationDecl(
        supported_modes=frozenset({
            DegradationMode.FULL, DegradationMode.BYPASS,
        }),
        export_behavior="full",
        cost_estimate_ms=12.0,
    ),
    "edge_detect": DegradationDecl(
        supported_modes=frozenset({DegradationMode.FULL}),
        export_behavior="full",
        cost_estimate_ms=1.5,
    ),
}

# Fallback for unknown / plugin effects: run at full, no degradation declared.
_DEFAULT_DECL = DegradationDecl(
    supported_modes=frozenset({DegradationMode.FULL}),
    export_behavior="full",
    cost_estimate_ms=0.0,
)


def get_degradation_decl(effect_type: str) -> DegradationDecl:
    """Look up the degradation declaration for an effect type.

    Falls back to _DEFAULT_DECL (full only) for unknown types.

    Args:
        effect_type: The ``EffectSpec.effect_type`` to look up.

    Returns:
        The registered :class:`DegradationDecl` (FULL-only default if absent).
    """
    return _BUILTIN_DECLS.get(effect_type, _DEFAULT_DECL)


def register_degradation_decl(effect_type: str, decl: DegradationDecl) -> None:
    """Register or override a degradation declaration (for plugins).

    Registration semantics: mutates the process-wide module-level registry
    and takes effect immediately for subsequent ``evaluate_chain`` calls.
    Intended to be called once at plugin load time (UI thread); there is no
    locking, so concurrent registration during active playback is not
    supported.

    Args:
        effect_type: The effect type the declaration applies to.
        decl: The declaration; FULL is force-added if missing.
    """
    _BUILTIN_DECLS[effect_type] = decl


# ================================================================
# Policy engine
# ================================================================

class PreviewPolicyEngine:
    """Generates per-effect degradation decisions for one frame.

    Stateless between frames — call ``evaluate_chain`` each frame with the
    current context and effect list. The engine is cheap (no GPU calls,
    pure arithmetic on declarations).

    Usage::

        engine = PreviewPolicyEngine()
        decisions = engine.evaluate_chain(ctx, effects)
        for dec in decisions:
            params = dec.capped_params if dec.capped_params else original_params
            if dec.mode == DegradationMode.BYPASS:
                continue  # skip this effect
            runner.run(spec, tex, params, w, h)
    """

    def evaluate_chain(
        self,
        ctx: PolicyContext,
        effects: List[Tuple[str, str, Dict[str, Any]]],
    ) -> List[PreviewPolicyDecision]:
        """Produce a decision for each effect in the chain.

        Args:
            ctx: Frame-local context (resolution, playback state, budget).
            effects: List of (effect_id, effect_type, params) tuples in
                chain order.

        Returns:
            One PreviewPolicyDecision per effect, same order as input.
        """
        # Force full quality when not playing or explicitly requested.
        if ctx.force_full or not ctx.is_playing:
            return [
                PreviewPolicyDecision(
                    effect_id=eid,
                    effect_type=etype,
                    mode=DegradationMode.FULL,
                    reason="full quality (paused/export/force)",
                    export_behavior=get_degradation_decl(etype).export_behavior,
                )
                for eid, etype, _ in effects
            ]

        # Playing: apply budget-aware degradation.
        decisions: List[PreviewPolicyDecision] = []
        remaining_budget = ctx.gpu_budget_ms
        # Scale budget by resolution relative to 1080p baseline.
        pixel_count = ctx.width * ctx.height
        baseline_pixels = 1920 * 1080
        resolution_factor = pixel_count / baseline_pixels if baseline_pixels > 0 else 1.0

        for eid, etype, params in effects:
            decl = get_degradation_decl(etype)
            estimated_cost = decl.cost_estimate_ms * resolution_factor

            if estimated_cost <= remaining_budget or not decl.supported_modes - {DegradationMode.FULL}:
                # Within budget or no degradation available → run full.
                decisions.append(PreviewPolicyDecision(
                    effect_id=eid,
                    effect_type=etype,
                    mode=DegradationMode.FULL,
                    reason="within budget",
                    export_behavior=decl.export_behavior,
                    cost_estimate_ms=estimated_cost,
                ))
                remaining_budget -= estimated_cost
                continue

            # Over budget: pick the best available degradation.
            mode, capped, reason = self._pick_degradation(decl, params, etype)
            decisions.append(PreviewPolicyDecision(
                effect_id=eid,
                effect_type=etype,
                mode=mode,
                reason=reason,
                capped_params=capped,
                export_behavior=decl.export_behavior,
                cost_estimate_ms=estimated_cost * 0.3 if mode.is_degraded else estimated_cost,
            ))
            # Degraded effects cost less; deduct reduced estimate.
            if mode == DegradationMode.BYPASS:
                pass  # zero cost
            elif mode == DegradationMode.PARAMETER_CAP:
                remaining_budget -= estimated_cost * 0.3
            elif mode == DegradationMode.LOWER_RESOLUTION:
                remaining_budget -= estimated_cost * decl.resolution_scale
            else:
                remaining_budget -= estimated_cost

        return decisions

    @staticmethod
    def _pick_degradation(
        decl: DegradationDecl,
        params: Dict[str, Any],
        effect_type: str,
    ) -> Tuple[DegradationMode, Optional[Dict[str, Any]], str]:
        """Choose the least-intrusive degradation mode available."""
        # Prefer PARAMETER_CAP (keeps effect visible, just cheaper).
        params_within_caps = False
        if DegradationMode.PARAMETER_CAP in decl.supported_modes and decl.param_caps:
            capped = dict(params)
            any_capped = False
            for name, cap_val in decl.param_caps.items():
                if name in capped:
                    try:
                        v = float(capped[name])
                        if v > cap_val:
                            capped[name] = cap_val
                            any_capped = True
                    except (TypeError, ValueError):
                        continue
            if any_capped:
                return (
                    DegradationMode.PARAMETER_CAP,
                    capped,
                    f"parameter cap ({effect_type}): expensive params clamped for playback",
                )
            # AUD-26410: params already within caps — do NOT return FULL
            # here.  This branch is only reached when the effect is over
            # budget, so keep probing the declared LOWER_RESOLUTION /
            # BYPASS modes (e.g. glow declares {FULL, PARAMETER_CAP,
            # LOWER_RESOLUTION} and must still drop to half resolution
            # when stacked or at 4K resolution_factor).
            params_within_caps = True

        # Next: LOWER_RESOLUTION.
        if DegradationMode.LOWER_RESOLUTION in decl.supported_modes:
            return (
                DegradationMode.LOWER_RESOLUTION,
                None,
                f"lower resolution ({decl.resolution_scale:.0%}) for playback",
            )

        # Last resort: BYPASS.
        if DegradationMode.BYPASS in decl.supported_modes:
            return (
                DegradationMode.BYPASS,
                None,
                f"bypassed ({effect_type}): too expensive for real-time playback",
            )

        # No further degradation declared → must run full even if over budget.
        return (
            DegradationMode.FULL,
            None,
            "params already within caps" if params_within_caps else
            "no degradation declared; running full despite budget",
        )


# ================================================================
# Convenience: apply decisions to params (frame-local, no mutation)
# ================================================================

def resolve_params_for_decision(
    original_params: Dict[str, Any],
    decision: PreviewPolicyDecision,
) -> Optional[Dict[str, Any]]:
    """Return the params to use for this frame, or None to skip the effect.

    NEVER mutates ``original_params``. Returns a new dict when capping,
    or the original dict reference when running full.
    """
    if decision.mode == DegradationMode.BYPASS:
        return None
    if decision.capped_params is not None:
        return decision.capped_params
    return original_params
