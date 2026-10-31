"""W0 agent kernel tests: feedback store, run context, replan policy, capability catalog, missions, evidence.

These lock the OS-07 / AG-02 / OS-02 / OS-03 / OS-01 / OS-05 / OS-04 gaps
identified by the audit, using the same Qt-free contract pattern.
"""

from __future__ import annotations

import time

import pytest

from src.features.edit.ai_editing.capability_catalog import (
    CANCEL_ATOMIC,
    DOMAIN_EDITORIAL,
    IDEMPOTENCY_REQUEST_KEY,
    CapabilityCatalog,
    CapabilityCatalogEntry,
    CompensationAction,
    create_default_capability_catalog,
)
from src.features.edit.ai_editing.evidence_kinds import (
    EXTENDED_EVIDENCE_KINDS,
    EVIDENCE_BEAT,
    EVIDENCE_COLOR,
    EVIDENCE_TRANSCRIPT,
    EvidenceRecord,
    DomainSnapshot,
    ExtendedProjectSnapshot,
)
from src.features.edit.ai_editing.feedback_store import (
    CAT_COLOR,
    CAT_PACING,
    DOMAIN_EDITORIAL as FEEDBACK_DOMAIN_EDITORIAL,
    FeedbackStore,
)
from src.features.edit.ai_editing.missions import (
    MISSION_ACTIVE,
    MISSION_SUCCEEDED,
    MISSION_WAITING_USER,
    MissionStore,
    SuccessCriterion,
)
from src.features.edit.ai_editing.replan_policy import (
    DECISION_CONTINUE,
    DECISION_FINISH,
    DECISION_REQUEST_CLARIFICATION,
    DECISION_RETRY_LOCALLY,
    DECISION_ROLLBACK,
    DECISION_SWITCH_TOOL,
    ReplanLimits,
    ReplanPolicy,
    TRIGGER_ACTION_FAILED,
    TRIGGER_ACTION_SUCCEEDED,
    TRIGGER_REVISION_CONFLICT,
    TRIGGER_VERIFY_PASSED,
    TRIGGER_VERIFY_WARNINGS,
)
from src.features.edit.ai_editing.run_context import (
    PERMISSION_CONFIRM_EACH,
    PERMISSION_PROPOSAL,
    PERMISSION_RESTRICTED_AUTO,
    RunContext,
    validate_run_context,
)


# --------------------------------------------------------------------------- #
# OS-07: feedback store
# --------------------------------------------------------------------------- #


def test_feedback_store_creates_and_queries_by_artifact() -> None:
    store = FeedbackStore()
    patch = store.create(
        project_id="p1", run_id="r1", domain=FEEDBACK_DOMAIN_EDITORIAL,
        categories=(CAT_PACING, CAT_COLOR),
        requested_changes={"pacing": "faster"},
        artifact_refs=("plan-1",),
        note="too slow",
    )
    assert patch.patch_id
    by_artifact = store.for_artifact("plan-1")
    assert len(by_artifact) == 1
    assert by_artifact[0].patch_id == patch.patch_id


def test_feedback_store_categories_covered() -> None:
    store = FeedbackStore()
    store.create(project_id="p1", domain="editorial", categories=(CAT_PACING,), artifact_refs=("a",))
    store.create(project_id="p1", domain="editorial", categories=(CAT_COLOR,), artifact_refs=("b",))
    covered = store.categories_covered(project_id="p1")
    assert CAT_PACING in covered
    assert CAT_COLOR in covered


def test_feedback_patch_rejects_unknown_category() -> None:
    from src.features.edit.ai_editing.feedback_store import GeneralFeedbackPatch
    with pytest.raises(ValueError):
        GeneralFeedbackPatch(
            patch_id="x", project_id="p1", run_id=None,
            domain="editorial", categories=("not_a_category",),
        )


# --------------------------------------------------------------------------- #
# AG-02: run context
# --------------------------------------------------------------------------- #


def _ctx(**overrides) -> RunContext:
    base = dict(run_id="r1", project_id="p1")
    base.update(overrides)
    return RunContext(**base)


def test_run_context_fingerprint_stable() -> None:
    a = _ctx()
    b = _ctx()
    assert a.fingerprint() == b.fingerprint()


def test_run_context_permission_drives_write() -> None:
    assert not _ctx(permission=PERMISSION_PROPOSAL).may_write
    assert _ctx(permission=PERMISSION_CONFIRM_EACH).may_write
    assert _ctx(permission=PERMISSION_RESTRICTED_AUTO).may_auto_write


def test_run_context_asset_scope() -> None:
    ctx = _ctx(selected_asset_ids=("a1", "a2"))
    assert ctx.asset_in_scope("a1")
    assert not ctx.asset_in_scope("a3")
    # AUD-14310：None = 显式不限（整工程）；空集 = 拒绝全部（fail-closed），
    # 不再被解释为"全工程素材均授权"。
    unrestricted = _ctx(selected_asset_ids=None)
    assert unrestricted.asset_in_scope("anything")
    empty = _ctx(selected_asset_ids=())
    assert not empty.asset_in_scope("anything")
    assert not empty.asset_in_scope("a1")


def test_validate_run_context_detects_stale_revision() -> None:
    ctx = _ctx(base_revision=5, permission=PERMISSION_RESTRICTED_AUTO, budget_units=10.0)
    issues = validate_run_context(ctx, observed_revision=7)
    assert any(i.code == "STALE_REVISION" for i in issues)


def test_validate_run_context_requires_budget_for_auto() -> None:
    ctx = _ctx(permission=PERMISSION_RESTRICTED_AUTO)
    issues = validate_run_context(ctx)
    assert any(i.code == "BUDGET_REQUIRED_FOR_AUTO" for i in issues)


# --------------------------------------------------------------------------- #
# OS-02: replan policy
# --------------------------------------------------------------------------- #


def test_replan_policy_continues_on_success() -> None:
    policy = ReplanPolicy()
    decision = policy.decide(TRIGGER_ACTION_SUCCEEDED)
    assert decision.kind == DECISION_CONTINUE


def test_replan_policy_retries_then_escalates() -> None:
    policy = ReplanPolicy(ReplanLimits(max_retries_per_step=1, max_tool_switches=1))
    # First failure → retry.
    d1 = policy.decide(TRIGGER_ACTION_FAILED, step_id="s1", alternative_tool="t2")
    assert d1.kind == DECISION_RETRY_LOCALLY
    # Second failure → switch tool.
    d2 = policy.decide(TRIGGER_ACTION_FAILED, step_id="s1", alternative_tool="t2")
    assert d2.kind == DECISION_SWITCH_TOOL
    # Third failure → request clarification (no rollback target given).
    d3 = policy.decide(TRIGGER_ACTION_FAILED, step_id="s1")
    assert d3.kind == DECISION_REQUEST_CLARIFICATION


def test_replan_policy_revision_conflict_requests_clarification() -> None:
    policy = ReplanPolicy()
    decision = policy.decide(TRIGGER_REVISION_CONFLICT)
    assert decision.kind == DECISION_REQUEST_CLARIFICATION
    assert decision.clarification_question


def test_replan_policy_cycle_limit_finishes() -> None:
    policy = ReplanPolicy(ReplanLimits(max_cycles=2))
    policy.begin_cycle()
    policy.begin_cycle()
    decision = policy.decide(TRIGGER_VERIFY_WARNINGS)
    assert decision.kind == DECISION_FINISH


def test_replan_policy_quality_warning_retries() -> None:
    policy = ReplanPolicy()
    decision = policy.decide(TRIGGER_VERIFY_WARNINGS, step_id="s1")
    assert decision.kind == DECISION_RETRY_LOCALLY


# --------------------------------------------------------------------------- #
# OS-03: capability catalog
# --------------------------------------------------------------------------- #


def test_default_catalog_has_core_tools() -> None:
    catalog = create_default_capability_catalog()
    assert catalog.get("topos.apply_edit_plan") is not None
    assert catalog.get("topos.request_export") is not None


def test_catalog_refuses_undeclared_tool() -> None:
    catalog = create_default_capability_catalog()
    with pytest.raises(KeyError):
        catalog.require("totally_made_up_tool")


def test_catalog_missing_evidence_for_tool() -> None:
    catalog = create_default_capability_catalog()
    missing = catalog.missing_evidence_for("topos.propose_edit_plan", available_evidence=("transcript",))
    assert "shot" in missing


def test_catalog_entry_compensation() -> None:
    catalog = create_default_capability_catalog()
    apply = catalog.require("topos.apply_edit_plan")
    assert apply.compensation is not None
    assert apply.compensation.undo_tool == "topos.undo_last_commit"
    assert apply.writes_timeline


def test_catalog_entry_rejects_unknown_domain() -> None:
    with pytest.raises(ValueError):
        CapabilityCatalogEntry(name="x", domain="fake_domain", permission="read", description="")


# --------------------------------------------------------------------------- #
# OS-01: missions
# --------------------------------------------------------------------------- #


def test_mission_store_create_with_criteria() -> None:
    store = MissionStore()
    mission = store.create(
        project_id="p1",
        objective_text="制作 15 秒广告",
        success_criteria=(
            SuccessCriterion(check="delivery_quality_gate_passed"),
            SuccessCriterion(check="user_approved"),
        ),
        budget_units=20.0,
    )
    assert mission.is_active
    assert not mission.success_criteria_met


def test_mission_auto_succeeds_when_all_criteria_met() -> None:
    store = MissionStore()
    mission = store.create(
        project_id="p1",
        objective_text="test",
        success_criteria=(SuccessCriterion(check="gate"),),
    )
    store.update_criterion(mission.mission_id, "gate", met=True)
    refreshed = store.get(mission.mission_id)
    assert refreshed.status == MISSION_SUCCEEDED


def test_mission_parent_child_hierarchy() -> None:
    store = MissionStore()
    parent = store.create(project_id="p1", objective_text="parent")
    child = store.create(project_id="p1", objective_text="child", parent_run_id=parent.mission_id)
    children = store.children_of(parent.mission_id)
    assert len(children) == 1
    assert children[0].mission_id == child.mission_id


def test_mission_decision_log() -> None:
    store = MissionStore()
    mission = store.create(project_id="p1", objective_text="test")
    decision = store.add_decision(mission.mission_id, kind="user_accept", reason="looks good")
    refreshed = store.get(mission.mission_id)
    assert refreshed.decisions
    assert refreshed.decisions[-1].decision_id == decision.decision_id


def test_mission_describe_progress() -> None:
    store = MissionStore()
    mission = store.create(
        project_id="p1",
        objective_text="制作广告",
        success_criteria=(SuccessCriterion(check="gate", label="质量门"),),
    )
    text = mission.describe_progress()
    assert "制作广告" in text
    assert "0/1" in text


# --------------------------------------------------------------------------- #
# OS-05: evidence kinds + OS-04: project snapshot
# --------------------------------------------------------------------------- #


def test_extended_evidence_kcludes_new_domains() -> None:
    assert EVIDENCE_BEAT in EXTENDED_EVIDENCE_KINDS
    assert EVIDENCE_COLOR in EXTENDED_EVIDENCE_KINDS


def test_evidence_record_round_trip() -> None:
    record = EvidenceRecord(
        evidence_id="e1",
        kind=EVIDENCE_COLOR,
        asset_id="a1",
        model_id="color-probe-v2",
        confidence=0.92,
        payload={"warmth": 0.6},
        summary="warm tone",
    )
    assert record.kind == EVIDENCE_COLOR
    assert not record.is_expired()


def test_evidence_record_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        EvidenceRecord(evidence_id="e1", kind="not_a_kind", asset_id="a1")


def test_extended_snapshot_fingerprint_stable() -> None:
    snap = ExtendedProjectSnapshot(
        project_id="p1", timeline_id="t1", base_revision=1,
        domains=(
            DomainSnapshot(domain="editorial", revision=1, fingerprint="abc"),
            DomainSnapshot(domain="color", revision=2, fingerprint="def"),
        ),
    )
    assert snap.fingerprint() == snap.fingerprint()
    snap2 = ExtendedProjectSnapshot(
        project_id="p1", timeline_id="t1", base_revision=1,
        domains=(
            DomainSnapshot(domain="editorial", revision=1, fingerprint="changed"),
            DomainSnapshot(domain="color", revision=2, fingerprint="def"),
        ),
    )
    assert snap.fingerprint() != snap2.fingerprint()


def test_extended_snapshot_stale_domains() -> None:
    snap_a = ExtendedProjectSnapshot(
        project_id="p1", timeline_id="t1", base_revision=1,
        domains=(
            DomainSnapshot(domain="editorial", revision=1, fingerprint="h1"),
            DomainSnapshot(domain="color", revision=1, fingerprint="h2"),
        ),
    )
    snap_b = ExtendedProjectSnapshot(
        project_id="p1", timeline_id="t1", base_revision=1,
        domains=(
            DomainSnapshot(domain="editorial", revision=1, fingerprint="h1_changed"),
            DomainSnapshot(domain="color", revision=1, fingerprint="h2"),
        ),
    )
    stale = snap_a.stale_domains(snap_b)
    assert stale == ("editorial",)
