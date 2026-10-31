"""B5 release evidence, privacy telemetry and gray rollout tests."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.kill_switch import Capability, KillSwitch
from src.features.edit.ai_editing.release_evidence import (
    AccessibilityEvidence,
    GrayRolloutConfig,
    ReleaseEvidenceBundle,
    TelemetryCollector,
)


def test_telemetry_is_opt_in_and_redacts_content_path_and_secret() -> None:
    telemetry = TelemetryCollector(opt_in=False)
    assert not telemetry.emit("generation", metadata={"prompt": "secret text", "ok": 1})
    telemetry.set_opt_in(True)
    assert telemetry.emit(
        "generation", status="succeeded",
        metadata={"prompt": "do not store", "source_path": "/tmp/a.mp4",
                  "api_key": "token", "provider": "seedance", "cost": 0.2},
    )
    exported = telemetry.export()
    json.dumps(exported)
    text = repr(exported)
    assert "do not store" not in text and "/tmp/a.mp4" not in text and "token" not in text
    assert exported["events"][0]["metadata"] == {"provider": "seedance", "cost": 0.2}
    telemetry.clear()
    assert telemetry.events() == ()


def test_gray_rollout_is_deterministic_and_killable() -> None:
    kill_switch = KillSwitch()
    config = GrayRolloutConfig(Capability.GENERATION, channel="alpha", percentage=100.0)
    assert config.enabled_for("cohort-a", kill_switch=kill_switch)
    kill_switch.disable(Capability.GENERATION, reason="incident")
    assert not config.enabled_for("cohort-a", kill_switch=kill_switch)


def test_release_evidence_fails_closed_until_all_hard_items_exist() -> None:
    incomplete = ReleaseEvidenceBundle(version="1.0", channel="beta")
    assert not incomplete.releasable
    assert "eval_missing_or_failed" in incomplete.blockers
    assert "gray_rollout_missing" in incomplete.blockers
    complete = ReleaseEvidenceBundle(
        version="1.0", channel="beta", eval_report_id="eval-1", eval_passed=True,
        eval_corpus_version="ai-editing-2026.08",
        rubric_report_id="rubric-1", rubric_passed=True,
        performance_report_id="perf-1", security_report_id="sec-1",
        privacy_audit_passed=True, kill_switch_verified=True,
        telemetry_opt_in_documented=True,
        gray_rollout=GrayRolloutConfig(Capability.GENERATION, channel="beta", percentage=10.0),
        accessibility=AccessibilityEvidence(True, True, True, True),
        provider_smoke_passed=True, provider_cost_report_id="cost-1",
        cancellation_recovery_passed=True, rollback_runbook_ref="runbook-1",
    )
    assert complete.releasable
    assert complete.to_dict()["releasable"] is True
