"""P11 release manifest and safe default tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.release_manifest import (
    ReleaseManifest,
    build_release_manifest,
)


def test_beta_manifest_records_providers_and_safe_defaults() -> None:
    class Provider:
        provider_id = "remote-video"

    manifest = build_release_manifest(
        version="2026.08.09-beta.1",
        channel="beta",
        providers=(Provider(),),
        test_report_id="eval-1",
        quality_gate_passed=True,
        test_matrix_passed=True,
        commercial_audit_passed=True,
        commercial_audit_report_id="audit-1",
        commercial_acceptance_passed=True,
        commercial_acceptance_report_id="commercial-acceptance-1",
    )
    assert manifest.releasable
    assert manifest.supported_providers == ("remote-video",)
    assert manifest.default_flags["agent_auto_mode"] is False
    assert manifest.to_dict()["releasable"] is True
    assert manifest.release_blockers == ()


def test_stable_manifest_requires_rollback_and_quality_gate() -> None:
    assert not ReleaseManifest(
        version="1", channel="stable", quality_gate_passed=True,
        test_report_id="eval-1", test_matrix_passed=True,
        commercial_audit_passed=True, commercial_audit_report_id="audit-1",
        commercial_acceptance_passed=True,
        commercial_acceptance_report_id="commercial-acceptance-1",
    ).releasable
    assert not ReleaseManifest(
        version="1", channel="stable", quality_gate_passed=False,
        rollback_version="0.9",
    ).releasable
    assert ReleaseManifest(
        version="1", channel="stable", quality_gate_passed=True,
        rollback_version="0.9", test_report_id="eval-1",
        test_matrix_passed=True, commercial_audit_passed=True,
        commercial_audit_report_id="audit-1",
        commercial_acceptance_passed=True,
        commercial_acceptance_report_id="commercial-acceptance-1",
    ).releasable


def test_quality_gate_alone_is_not_release_evidence() -> None:
    manifest = ReleaseManifest(
        version="1", channel="beta", quality_gate_passed=True,
    )

    assert not manifest.releasable
    assert manifest.release_blockers == (
        "test_report_missing",
        "test_matrix_failed_or_missing",
        "commercial_audit_failed_or_missing",
        "commercial_audit_report_missing",
        "commercial_panel_acceptance_failed_or_missing",
        "commercial_panel_acceptance_report_missing",
    )


def test_release_manifest_requires_panel_acceptance_even_with_legacy_evidence() -> None:
    manifest = ReleaseManifest(
        version="1",
        channel="beta",
        quality_gate_passed=True,
        test_report_id="eval-1",
        test_matrix_passed=True,
        commercial_audit_passed=True,
        commercial_audit_report_id="audit-1",
    )

    assert not manifest.releasable
    assert manifest.release_blockers == (
        "commercial_panel_acceptance_failed_or_missing",
        "commercial_panel_acceptance_report_missing",
    )
