"""B5 advanced capability registry tests (MG-28).

MG-28 acceptance: every new advanced capability has a schema, preview/export
compatibility declaration, quality gate hook, fallback strategy and stability
tier — and unregistered capabilities are refused by the planner.

These tests lock the registry contract; concrete capabilities are added in
later batches by registering :class:`AdvancedCapability` records.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.ai.advanced_capabilities import (
    ADVANCED_CAPABILITIES,
    CAP_3D_CAMERA,
    CAP_ACCESSIBILITY_OUTPUT,
    CAP_CHARACTER_LIPSYNC,
    CAP_CHART_HIGHLIGHT,
    CAP_CODE_HIGHLIGHT,
    CAP_CROSS_LANGUAGE,
    CAP_EXPRESSIONS,
    CAP_MULTI_SCENE_NARRATIVE,
    AdvancedCapability,
    AdvancedCapabilityRegistry,
    CapabilityCompatibility,
    FALLBACK_DISABLE,
    FALLBACK_REQUIRE_USER_DECISION,
    FALLBACK_SIMPLIFY,
    create_default_advanced_registry,
)


def test_default_registry_seeds_candidate_capabilities() -> None:
    registry = create_default_advanced_registry()
    seeded = {cap.capability_id for cap in registry.all_capabilities()}
    # A representative subset from the plan's MG-28 list.
    for expected in (
        CAP_MULTI_SCENE_NARRATIVE, CAP_CHART_HIGHLIGHT, CAP_CODE_HIGHLIGHT,
        CAP_CHARACTER_LIPSYNC, CAP_CROSS_LANGUAGE, CAP_ACCESSIBILITY_OUTPUT,
        CAP_3D_CAMERA,
    ):
        assert expected in seeded
    assert CAP_EXPRESSIONS in seeded


def test_expression_capability_declares_real_scope_and_quality_gates() -> None:
    capability = create_default_advanced_registry().require(CAP_EXPRESSIONS)
    assert capability.compatibility.preview_backends == ("svg", "moderngl")
    assert capability.compatibility.export_backends == ("svg", "mp4")
    assert set(capability.quality_checks) == {
        "expression_sandbox", "expression_determinism", "expression_budget",
    }
    assert capability.metadata["html_layer_expression_support"] is False


def test_capability_requires_known_id() -> None:
    with pytest.raises(ValueError):
        AdvancedCapability(
            capability_id="not_in_vocabulary",
            display_name="x", description="x",
        )


def test_capability_rejects_unknown_fallback() -> None:
    with pytest.raises(ValueError):
        AdvancedCapability(
            capability_id=CAP_CHART_HIGHLIGHT,
            display_name="x", description="x",
            fallback_strategy="magic",
        )


def test_registry_refuses_unknown_capability() -> None:
    registry = AdvancedCapabilityRegistry()
    with pytest.raises(KeyError):
        registry.require(CAP_CHART_HIGHLIGHT)


def test_registry_compatibility_flags_missing_gpu() -> None:
    registry = create_default_advanced_registry()
    lipsync = registry.require(CAP_CHARACTER_LIPSYNC)
    issues = registry.compatibility_issues(
        lipsync,
        codec="mp4", aspect="16:9",
        preview_backend="moderngl", has_gpu=False,
    )
    assert any("GPU" in issue for issue in issues)


def test_registry_compatibility_flags_incompatible_aspect() -> None:
    registry = create_default_advanced_registry()
    cap_3d = registry.require(CAP_3D_CAMERA)
    issues = registry.compatibility_issues(
        cap_3d,
        codec="mp4", aspect="1:1",
        preview_backend="moderngl", has_gpu=True,
    )
    assert any("1:1" in issue for issue in issues)


def test_registry_stable_capabilities_filters_experimental() -> None:
    registry = create_default_advanced_registry()
    stable = registry.stable_capabilities()
    # Accessibility is marked 'ga' in the seed; experimental ones are excluded.
    stable_ids = {cap.capability_id for cap in stable}
    assert CAP_ACCESSIBILITY_OUTPUT in stable_ids
    assert CAP_3D_CAMERA not in stable_ids


def test_round_trip_serialization() -> None:
    registry = create_default_advanced_registry()
    chart = registry.require(CAP_CHART_HIGHLIGHT)
    payload = chart.to_dict()
    assert payload["capability_id"] == CAP_CHART_HIGHLIGHT
    assert payload["fallback_strategy"] == FALLBACK_DISABLE
    assert "preview_backends" in payload["compatibility"]


def test_every_advanced_capability_can_be_constructed() -> None:
    """Smoke-test every id in the closed vocabulary."""
    for cap_id in ADVANCED_CAPABILITIES:
        capability = AdvancedCapability(
            capability_id=cap_id,
            display_name=cap_id,
            description="test",
            compatibility=CapabilityCompatibility(),
        )
        assert capability.capability_id == cap_id
