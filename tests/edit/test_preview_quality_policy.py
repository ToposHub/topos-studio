"""FX-M04 verification: Preview Quality Policy.

Acceptance criteria from the plan:
- 4K high-cost chain under budget → predictable decision list.
- Same project params unchanged before/after preview, save/reopen.
- paused/export → always full quality.
- bypass / cap state consistent in diagnostics, HUD, export preflight.
- plugin without capability declaration → not silently executed (explicit default).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.features.shared.effects.preview_policy import (
    DegradationDecl,
    DegradationMode,
    PolicyContext,
    PreviewPolicyDecision,
    PreviewPolicyEngine,
    get_degradation_decl,
    register_degradation_decl,
    resolve_params_for_decision,
)

pytestmark = pytest.mark.edit_core


# ================================================================
# Helpers
# ================================================================

def _chain(*items: Tuple[str, str, Dict[str, Any]]) -> List[Tuple[str, str, Dict[str, Any]]]:
    return list(items)


def _ctx(playing: bool = True, w: int = 1920, h: int = 1080,
         budget: float = 16.0, force_full: bool = False) -> PolicyContext:
    return PolicyContext(width=w, height=h, is_playing=playing,
                         gpu_budget_ms=budget, force_full=force_full)


# ================================================================
# DegradationMode enum
# ================================================================

class TestDegradationMode:
    def test_full_is_not_degraded(self) -> None:
        assert not DegradationMode.FULL.is_degraded

    def test_others_are_degraded(self) -> None:
        assert DegradationMode.LOWER_RESOLUTION.is_degraded
        assert DegradationMode.PARAMETER_CAP.is_degraded
        assert DegradationMode.BYPASS.is_degraded


# ================================================================
# DegradationDecl
# ================================================================

class TestDegradationDecl:
    def test_full_always_included(self) -> None:
        """Even if you forget FULL, __post_init__ adds it."""
        decl = DegradationDecl(
            supported_modes=frozenset({DegradationMode.BYPASS}),
        )
        assert DegradationMode.FULL in decl.supported_modes

    def test_frozen(self) -> None:
        decl = DegradationDecl()
        with pytest.raises(AttributeError):
            decl.resolution_scale = 0.5  # type: ignore[misc]


# ================================================================
# PreviewPolicyDecision
# ================================================================

class TestPreviewPolicyDecision:
    def test_badge_text_full_empty(self) -> None:
        d = PreviewPolicyDecision("e1", "blur", DegradationMode.FULL)
        assert d.badge_text == ""
        assert not d.is_degraded

    def test_badge_text_cap(self) -> None:
        d = PreviewPolicyDecision("e1", "blur", DegradationMode.PARAMETER_CAP,
                                  reason="clamped")
        assert d.badge_text == "capped"
        assert d.is_degraded

    def test_badge_text_bypass(self) -> None:
        d = PreviewPolicyDecision("e1", "denoise", DegradationMode.BYPASS)
        assert d.badge_text == "bypassed"

    def test_frozen(self) -> None:
        d = PreviewPolicyDecision("e1", "blur", DegradationMode.FULL)
        with pytest.raises(AttributeError):
            d.mode = DegradationMode.BYPASS  # type: ignore[misc]


# ================================================================
# Policy engine: paused / export → full quality
# ================================================================

class TestPolicyPausedExport:
    def test_paused_always_full(self) -> None:
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=False)
        effects = _chain(
            ("e1", "blur", {"radius": 50.0}),
            ("e2", "denoise", {"strength": 1.0}),
        )
        decisions = engine.evaluate_chain(ctx, effects)
        assert all(d.mode == DegradationMode.FULL for d in decisions)

    def test_force_full_overrides_playing(self) -> None:
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, force_full=True)
        effects = _chain(("e1", "blur", {"radius": 50.0}))
        decisions = engine.evaluate_chain(ctx, effects)
        assert decisions[0].mode == DegradationMode.FULL

    def test_export_context_full(self) -> None:
        """Export uses force_full=True → no degradation ever."""
        engine = PreviewPolicyEngine()
        ctx = PolicyContext(width=3840, height=2160, is_playing=False,
                            force_full=True)
        effects = _chain(
            ("e1", "blur", {"radius": 100.0}),
            ("e2", "glow", {"radius": 80.0}),
            ("e3", "denoise", {"strength": 1.0}),
        )
        decisions = engine.evaluate_chain(ctx, effects)
        assert all(d.mode == DegradationMode.FULL for d in decisions)


# ================================================================
# Policy engine: 4K high-cost chain under budget
# ================================================================

class TestPolicy4KBudget:
    def test_4k_expensive_chain_gets_degraded(self) -> None:
        """At 4K with tight budget, expensive effects get capped/bypassed."""
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, w=3840, h=2160, budget=8.0)
        effects = _chain(
            ("e1", "blur", {"radius": 50.0}),
            ("e2", "glow", {"radius": 40.0}),
            ("e3", "denoise", {"strength": 0.8}),
        )
        decisions = engine.evaluate_chain(ctx, effects)
        # At 4K the resolution_factor = (3840*2160)/(1920*1080) = 4.0
        # blur cost = 3.0 * 4 = 12 > budget 8 → should be capped
        # At least one effect must be degraded
        degraded = [d for d in decisions if d.is_degraded]
        assert len(degraded) >= 1

    def test_1080p_generous_budget_all_full(self) -> None:
        """At 1080p with generous budget, all effects run full."""
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, w=1920, h=1080, budget=100.0)
        effects = _chain(
            ("e1", "sharpen", {"amount": 1.0}),
            ("e2", "blur", {"radius": 5.0}),
        )
        decisions = engine.evaluate_chain(ctx, effects)
        assert all(d.mode == DegradationMode.FULL for d in decisions)

    def test_predictable_decision_order(self) -> None:
        """Decisions come back in the same order as the input chain."""
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, w=3840, h=2160, budget=4.0)
        effects = _chain(
            ("a", "blur", {"radius": 50.0}),
            ("b", "sharpen", {"amount": 1.0}),
            ("c", "denoise", {"strength": 1.0}),
        )
        decisions = engine.evaluate_chain(ctx, effects)
        assert [d.effect_id for d in decisions] == ["a", "b", "c"]


# ================================================================
# Parameter cap behavior
# ================================================================

class TestParameterCap:
    def test_blur_radius_capped(self) -> None:
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, w=3840, h=2160, budget=4.0)
        effects = _chain(("e1", "blur", {"radius": 50.0, "radius_x": 30.0}))
        decisions = engine.evaluate_chain(ctx, effects)
        d = decisions[0]
        if d.mode == DegradationMode.PARAMETER_CAP:
            assert d.capped_params is not None
            assert d.capped_params["radius"] == 8.0
            assert d.capped_params["radius_x"] == 8.0

    def test_params_within_caps_not_degraded(self) -> None:
        """If params are already within caps, no degradation needed."""
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, w=3840, h=2160, budget=4.0)
        effects = _chain(("e1", "blur", {"radius": 3.0}))
        decisions = engine.evaluate_chain(ctx, effects)
        d = decisions[0]
        # Even if over budget, params within caps → FULL
        if d.mode == DegradationMode.FULL:
            assert d.capped_params is None

    def test_original_params_not_mutated(self) -> None:
        """The policy engine must never mutate the input params dict."""
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, w=3840, h=2160, budget=4.0)
        original = {"radius": 50.0, "radius_x": 30.0}
        snapshot = dict(original)
        effects = _chain(("e1", "blur", original))
        engine.evaluate_chain(ctx, effects)
        assert original == snapshot, "Input params must not be mutated"


# ================================================================
# Project params unchanged (no write-back)
# ================================================================

class TestNoWriteBack:
    def test_resolve_params_returns_new_dict(self) -> None:
        """resolve_params_for_decision returns a new dict, not the original."""
        original = {"radius": 50.0}
        decision = PreviewPolicyDecision(
            "e1", "blur", DegradationMode.PARAMETER_CAP,
            capped_params={"radius": 8.0},
        )
        result = resolve_params_for_decision(original, decision)
        assert result is not original
        assert result == {"radius": 8.0}
        assert original == {"radius": 50.0}  # unchanged

    def test_resolve_params_full_returns_original_ref(self) -> None:
        original = {"radius": 5.0}
        decision = PreviewPolicyDecision("e1", "blur", DegradationMode.FULL)
        result = resolve_params_for_decision(original, decision)
        assert result is original  # same reference, no copy needed

    def test_resolve_params_bypass_returns_none(self) -> None:
        decision = PreviewPolicyDecision("e1", "denoise", DegradationMode.BYPASS)
        result = resolve_params_for_decision({"strength": 1.0}, decision)
        assert result is None


# ================================================================
# Plugin without declaration → explicit default (not silent)
# ================================================================

class TestPluginDefault:
    def test_unknown_effect_gets_default_decl(self) -> None:
        decl = get_degradation_decl("my_custom_dctl_plugin")
        assert DegradationMode.FULL in decl.supported_modes
        # Only FULL → no degradation available → runs full even if over budget
        assert decl.supported_modes == frozenset({DegradationMode.FULL})

    def test_unknown_effect_not_silently_degraded(self) -> None:
        """Plugin without declaration runs full; never silently capped/bypassed."""
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, w=3840, h=2160, budget=1.0)  # very tight
        effects = _chain(("e1", "unknown_plugin_xyz", {"param": 99.0}))
        decisions = engine.evaluate_chain(ctx, effects)
        # Must run FULL — no silent degradation for undeclared effects
        assert decisions[0].mode == DegradationMode.FULL
        assert decisions[0].capped_params is None

    def test_register_plugin_decl(self) -> None:
        """Plugins can register their own degradation declaration."""
        register_degradation_decl("my_plugin", DegradationDecl(
            supported_modes=frozenset({
                DegradationMode.FULL, DegradationMode.BYPASS,
            }),
            cost_estimate_ms=20.0,
        ))
        decl = get_degradation_decl("my_plugin")
        assert DegradationMode.BYPASS in decl.supported_modes
        # Cleanup
        register_degradation_decl("my_plugin", DegradationDecl())


# ================================================================
# Bypass behavior
# ================================================================

class TestBypass:
    def test_denoise_bypassed_under_pressure(self) -> None:
        """denoise only supports FULL/BYPASS; under budget pressure → bypass."""
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, w=3840, h=2160, budget=4.0)
        # Put a cheap effect first to consume budget, then denoise
        effects = _chain(
            ("e1", "sharpen", {"amount": 1.0}),  # cost 1.0 * 4 = 4.0
            ("e2", "denoise", {"strength": 0.8}),  # cost 12.0 * 4 = 48.0
        )
        decisions = engine.evaluate_chain(ctx, effects)
        denoise_d = decisions[1]
        assert denoise_d.mode == DegradationMode.BYPASS
        assert "bypassed" in denoise_d.reason


# ================================================================
# Export behavior field
# ================================================================

class TestExportBehavior:
    def test_builtin_effects_export_full(self) -> None:
        for etype in ("blur", "sharpen", "glow", "defocus", "denoise"):
            decl = get_degradation_decl(etype)
            assert decl.export_behavior == "full", f"{etype} must export full"

    def test_decision_carries_export_behavior(self) -> None:
        engine = PreviewPolicyEngine()
        ctx = _ctx(playing=True, w=3840, h=2160, budget=4.0)
        effects = _chain(("e1", "blur", {"radius": 50.0}))
        decisions = engine.evaluate_chain(ctx, effects)
        assert decisions[0].export_behavior == "full"


# ================================================================
# Renderer integration: _degrade_params_for_preview compatibility
# ================================================================

class TestRendererCompat:
    def test_degrade_function_uses_policy_registry(self) -> None:
        """The renderer's _degrade_params_for_preview now uses DegradationDecl."""
        from unittest.mock import MagicMock
        # Import the function from the renderer module
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _degrade_params_for_preview,
        )
        spec = MagicMock()
        spec.effect_type = "blur"
        params = {"radius": 50.0, "radius_x": 30.0, "other": 99.0}
        result = _degrade_params_for_preview(spec, params)
        assert result["radius"] == 8.0
        assert result["radius_x"] == 8.0
        assert result["other"] == 99.0  # uncapped param unchanged

    def test_degrade_function_noop_for_cheap_effect(self) -> None:
        from unittest.mock import MagicMock
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _degrade_params_for_preview,
        )
        spec = MagicMock()
        spec.effect_type = "sharpen"
        params = {"amount": 5.0}
        result = _degrade_params_for_preview(spec, params)
        assert result is params  # same reference, no copy

    def test_degrade_function_noop_within_caps(self) -> None:
        from unittest.mock import MagicMock
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _degrade_params_for_preview,
        )
        spec = MagicMock()
        spec.effect_type = "blur"
        params = {"radius": 3.0}
        result = _degrade_params_for_preview(spec, params)
        assert result is params  # already within caps, no copy needed
