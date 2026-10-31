"""W6-W9 tests: action contract, provider resume, cross-domain cost, early validation, VE advanced, skills."""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.action_contract import (
    ActionContract,
    COMPENSATION_UNDO,
    CompensationSpec,
    POST_CLIP_EXISTS,
    PostconditionSpec,
    evaluate_postconditions,
)
from src.features.edit.ai_editing.provider_resume import (
    EXT_FAILED,
    EXT_IN_PROGRESS,
    EXT_SUCCEEDED,
    EXT_UNKNOWN,
    ExternalJobRecord,
    RECONCILE_ASK_USER,
    RECONCILE_RESUME,
    RECONCILE_RETRY,
    RECONCILE_SKIP,
    make_fingerprint,
    reconcile_inflight,
)
from src.features.edit.ai_editing.cross_domain import (
    CrossDomainDependency,
    DependencyGraph,
    DomainArtifactRef,
    DOMAIN_COLOR,
    DOMAIN_COMPOSITE,
    DOMAIN_EDITORIAL,
)
from src.features.edit.ai_editing.cross_domain_cost import (
    compute_dirty_range,
    predict_recompute_cost,
    validate_brief_against_delivery,
)
from src.features.edit.ai_editing.editorial_advanced import (
    ActiveAnglePlan,
    AudioDrivenEditPlan,
    BeatGrid,
    BRollCandidate,
    detect_conflicts,
    EffectCatalogEntry,
    EffectEligibility,
    LinkedVersion,
    LongFormAnalysisStrategy,
    MulticamSyncEvidence,
    SYNC_WAVEFORM,
    TranscriptRevision,
    VersionDerivationPlan,
)
from src.features.edit.ai_editing.professional_skills import (
    RECOMMENDED_SKILLS,
    SKILL_BRAND_LOOK,
    SKILL_GREEN_SCREEN_COMP,
    SKILL_INTERVIEW_HIGHLIGHTS,
    recommended_skill_registry,
)


# --------------------------------------------------------------------------- #
# OS-10: action contract
# --------------------------------------------------------------------------- #


def test_action_contract_postcondition_eval() -> None:
    contract = ActionContract(
        action_id="a1",
        estimated_cost=0.5,
        postconditions=(
            PostconditionSpec(kind=POST_CLIP_EXISTS, target_ref="c1"),
            PostconditionSpec(kind="clip_duration", target_ref="c1",
                              expected={"frames": 360}),
        ),
        compensation=CompensationSpec(strategy=COMPENSATION_UNDO, undo_tool="topos.undo"),
    )
    assert contract.has_postconditions
    assert contract.is_reversible

    def check(spec):
        if spec.kind == POST_CLIP_EXISTS:
            return type("F", (), {
                "kind": spec.kind, "target_ref": spec.target_ref,
                "passed": True, "severity": "error", "message": "ok",
            })()
        return type("F", (), {
            "kind": spec.kind, "target_ref": spec.target_ref,
            "passed": False, "severity": "error", "message": "duration mismatch",
        })()

    from src.features.edit.ai_editing.action_contract import PostconditionFinding
    def proper_check(spec):
        return PostconditionFinding(
            kind=spec.kind, target_ref=spec.target_ref,
            passed=(spec.kind == POST_CLIP_EXISTS),
            severity=spec.severity, message="ok" if spec.kind == POST_CLIP_EXISTS else "mismatch",
        )
    result = evaluate_postconditions(contract, check_fn=proper_check)
    assert not result.all_passed
    assert result.triggered_compensation


def test_action_contract_none_compensation_not_reversible() -> None:
    contract = ActionContract(action_id="a1")
    assert not contract.is_reversible


# --------------------------------------------------------------------------- #
# OS-09: provider resume
# --------------------------------------------------------------------------- #


def test_fingerprint_is_stable() -> None:
    a = make_fingerprint("openai", "gpt-4", {"prompt": "hi"}, {"temp": 0.7})
    b = make_fingerprint("openai", "gpt-4", {"prompt": "hi"}, {"temp": 0.7})
    assert a.fingerprint == b.fingerprint


def test_fingerprint_differs_on_different_input() -> None:
    a = make_fingerprint("openai", "gpt-4", {"prompt": "hi"})
    b = make_fingerprint("openai", "gpt-4", {"prompt": "bye"})
    assert a.fingerprint != b.fingerprint


def test_reconcile_skips_completed() -> None:
    fp = make_fingerprint("p", "m", {"x": 1})
    record = ExternalJobRecord(job_id="j1", fingerprint=fp, state=EXT_SUCCEEDED)
    decisions = reconcile_inflight([record], known_completed_fingerprints={fp.fingerprint})
    assert decisions[0].decision == RECONCILE_SKIP


def test_reconcile_resumes_in_progress() -> None:
    fp = make_fingerprint("p", "m", {"x": 1})
    record = ExternalJobRecord(job_id="j1", fingerprint=fp, state=EXT_IN_PROGRESS)
    decisions = reconcile_inflight([record])
    assert decisions[0].decision == RECONCILE_RESUME


def test_reconcile_retries_failed() -> None:
    fp = make_fingerprint("p", "m", {"x": 1})
    record = ExternalJobRecord(job_id="j1", fingerprint=fp, state=EXT_FAILED)
    decisions = reconcile_inflight([record])
    assert decisions[0].decision == RECONCILE_RETRY


def test_reconcile_asks_on_unknown() -> None:
    fp = make_fingerprint("p", "m", {"x": 1})
    record = ExternalJobRecord(job_id="j1", fingerprint=fp, state=EXT_UNKNOWN)
    decisions = reconcile_inflight([record])
    assert decisions[0].decision == RECONCILE_ASK_USER


# --------------------------------------------------------------------------- #
# IX-03: dirty range + cost
# --------------------------------------------------------------------------- #


def _dep_graph() -> DependencyGraph:
    return DependencyGraph(dependencies=(
        CrossDomainDependency(
            downstream_domain=DOMAIN_COLOR,
            upstream=DomainArtifactRef(domain=DOMAIN_EDITORIAL, artifact_id="ep1",
                                       revision=1, content_hash="h1"),
            affected_clip_ids=("c1", "c2"),
        ),
        CrossDomainDependency(
            downstream_domain=DOMAIN_COMPOSITE,
            upstream=DomainArtifactRef(domain=DOMAIN_EDITORIAL, artifact_id="ep1",
                                       revision=1, content_hash="h1"),
            affected_clip_ids=("c1",),
        ),
    ))


def test_dirty_range_targets_affected_clips() -> None:
    graph = _dep_graph()
    dirty = compute_dirty_range(graph, DOMAIN_EDITORIAL, changed_clip_ids=("c1",))
    assert "c1" in dirty.clip_ids
    assert not dirty.full_recompute


def test_predict_cost_reports_per_domain() -> None:
    graph = _dep_graph()
    report = predict_recompute_cost(graph, DOMAIN_EDITORIAL, ("c1",), budget_limit=10.0)
    assert report.total_estimated_cost > 0
    domains = [c.domain for c in report.domain_costs]
    assert DOMAIN_COLOR in domains
    assert DOMAIN_COMPOSITE in domains
    assert not report.exceeds_budget


def test_predict_cost_flags_budget_exceeded() -> None:
    graph = _dep_graph()
    report = predict_recompute_cost(graph, DOMAIN_EDITORIAL, ("c1",), budget_limit=0.01)
    assert report.exceeds_budget
    assert report.recommended_tier_downgrade == "lowres"


# --------------------------------------------------------------------------- #
# IX-05: early delivery validation
# --------------------------------------------------------------------------- #


def test_early_validation_blocks_wrong_aspect() -> None:
    report = validate_brief_against_delivery(
        canvas_aspect="9:16", duration_seconds=15.0, platform="youtube",
        profile_supports_aspect=False,
    )
    assert not report.is_valid
    assert any(f.code == "ASPECT_UNSUPPORTED" for f in report.errors)


def test_early_validation_blocks_long_duration() -> None:
    report = validate_brief_against_delivery(
        canvas_aspect="16:9", duration_seconds=120.0, platform="shorts",
        profile_max_duration=60.0,
    )
    assert any(f.code == "DURATION_EXCEEDS_PLATFORM" for f in report.errors)


def test_early_validation_blocks_transparent_on_non_transparent() -> None:
    report = validate_brief_against_delivery(
        canvas_aspect="16:9", duration_seconds=15.0, platform="web",
        transparent=True, profile_supports_transparent=False,
    )
    assert any(f.code == "TRANSPARENCY_UNSUPPORTED" for f in report.errors)


def test_early_validation_passes_compatible_brief() -> None:
    report = validate_brief_against_delivery(
        canvas_aspect="16:9", duration_seconds=15.0, platform="youtube",
    )
    assert report.is_valid


# --------------------------------------------------------------------------- #
# VE advanced
# --------------------------------------------------------------------------- #


def test_multicam_sync_evidence_validates() -> None:
    ev = MulticamSyncEvidence(
        group_id="g1", angle_clip_ids=("a", "b"),
        sync_method=SYNC_WAVEFORM, confidence=0.9,
    )
    assert ev.confidence == 0.9


def test_active_angle_needs_confirmation_when_low_confidence() -> None:
    plan = ActiveAnglePlan(group_id="g1", cuts=(), confidence=0.5)
    assert plan.needs_confirmation


def test_beat_grid_beats_in_range() -> None:
    grid = BeatGrid(asset_id="a1", bpm=120.0, beat_frames=(0, 12, 24, 36, 48))
    assert grid.beats_in_range(10, 40) == (12, 24, 36)


def test_broll_candidate_auto_insert_gate() -> None:
    licensed = BRollCandidate(
        asset_id="a1", source_clip_id=None, in_frame=0, out_frame=100,
        reason="match", confidence=0.9, license_state="ok",
        brand_compliant=True, is_suggestion=False,
    )
    assert licensed.may_auto_insert
    unlicensed = BRollCandidate(
        asset_id="a2", source_clip_id=None, in_frame=0, out_frame=100,
        reason="match", confidence=0.9, license_state="missing",
    )
    assert not unlicensed.may_auto_insert


def test_detect_conflicts_finds_overlap() -> None:
    conflicts = detect_conflicts(
        ai_target_clip_ids=("c1", "c2", "c3"),
        human_changed_clip_ids=("c2", "c4"),
    )
    assert len(conflicts) == 1
    assert conflicts[0].clip_id == "c2"


def test_version_derivation_covers_aspects() -> None:
    plan = VersionDerivationPlan(
        master_plan_id="m1",
        versions=(
            LinkedVersion(version_id="v1", master_plan_id="m1",
                          canvas_aspect="16:9", duration_target_seconds=30.0),
            LinkedVersion(version_id="v2", master_plan_id="m1",
                          canvas_aspect="9:16", duration_target_seconds=30.0),
        ),
    )
    assert plan.version_count == 2
    assert "16:9" in plan.aspects_covered()
    assert "9:16" in plan.aspects_covered()


def test_long_form_hierarchy_depth() -> None:
    from src.features.edit.ai_editing.editorial_advanced import HierarchicalSummary
    strategy = LongFormAnalysisStrategy(
        asset_id="a1", total_duration_seconds=7200.0,
        summaries=(
            HierarchicalSummary(level="chapter", label="Ch1", start_seconds=0, duration_seconds=600),
            HierarchicalSummary(level="scene", label="S1", start_seconds=0, duration_seconds=60),
        ),
    )
    assert strategy.hierarchy_depth == 2


# --------------------------------------------------------------------------- #
# §5.2 professional skills
# --------------------------------------------------------------------------- #


def test_recommended_skills_cover_all_domains() -> None:
    domains = {s.domain for s in RECOMMENDED_SKILLS}
    assert "editorial" in domains
    assert "color" in domains
    assert "composite" in domains
    assert "cross_domain" in domains


def test_skill_registry_returns_all_8() -> None:
    registry = recommended_skill_registry()
    assert len(registry) == 8
    assert "topos.interview_highlights" in registry
    assert "topos.green_screen_comp" in registry


def test_skill_separates_auto_vs_confirmation() -> None:
    interview = SKILL_INTERVIEW_HIGHLIGHTS
    assert "analyze_assets" in interview.default_auto_scope
    assert "insert_timeline_clip" in interview.requires_confirmation


def test_green_screen_requires_user_decision_fallback() -> None:
    assert SKILL_GREEN_SCREEN_COMP.fallback_strategy == "require_user_decision"
