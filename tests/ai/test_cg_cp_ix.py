"""W3/W4 domain tests: color grade plan, composite plan, cross-domain orchestration.

Locks the CG-01~09 / CP-01~09 / IX-01~06 contracts.
"""

from __future__ import annotations

import pytest

from src.features.color.ai.color_context import (
    HDR_PQ,
    HDR_HLG,
    SDR,
    ColorContextSnapshot,
    ColorGradePlan,
    ColorMeasurement,
    GradeOperation,
    HDRGuardReport,
    LookRecipe,
    OP_LUT,
    OP_PRIMARY,
    SPACE_REC2020,
    SPACE_REC709,
    ShotMatchReport,
    ShotMatchSuggestion,
    compare_grade,
    validate_hdr,
)
from src.features.composite.ai.composite_plan import (
    CompositeBrief,
    CompositePlan,
    EFFECT_HUD,
    EFFECT_SCREEN_REPLACEMENT,
    NodeGraphOperation,
    validate_composite_plan,
)
from src.features.edit.ai_editing.cross_domain import (
    CrossDomainDependency,
    CrossDomainScorecard,
    DependencyGraph,
    DomainArtifactRef,
    DomainScore,
    ProtectedRegion,
    ProtectedRegionRegistry,
    RenderContract,
    REGION_CLIP,
    DOMAIN_COLOR,
    DOMAIN_COMPOSITE,
    DOMAIN_EDITORIAL,
    DOMAIN_DELIVERY,
)


# --------------------------------------------------------------------------- #
# CG-01: ColorContextSnapshot
# --------------------------------------------------------------------------- #


def _ctx(**overrides) -> ColorContextSnapshot:
    base = dict(project_id="p1", timeline_id="t1")
    base.update(overrides)
    return ColorContextSnapshot(**base)


def test_color_context_detects_hdr() -> None:
    assert not _ctx().is_hdr
    assert _ctx(timeline_transfer_function=HDR_PQ).is_hdr
    assert _ctx(timeline_transfer_function=HDR_HLG).is_hdr


def test_color_context_lock_protection() -> None:
    ctx = _ctx(locked_clip_ids=("c1",), locked_grade_node_ids=("n1",))
    assert ctx.is_locked("c1")
    assert ctx.is_locked("c2", node_id="n1")
    assert not ctx.is_locked("c2")


def test_color_context_rejects_unknown_space() -> None:
    with pytest.raises(ValueError):
        _ctx(working_space="fake_space")


# --------------------------------------------------------------------------- #
# CG-03: ColorGradePlan
# --------------------------------------------------------------------------- #


def _grade_plan(**overrides) -> ColorGradePlan:
    base = dict(
        plan_id="gp1", project_id="p1", timeline_id="t1",
        context_snapshot_hash="hash", base_revision=1,
        operations=(
            GradeOperation(op_id="op1", kind=OP_PRIMARY, clip_id="c1", parameters={"gain": 1.1}),
        ),
    )
    base.update(overrides)
    return ColorGradePlan(**base)


def test_grade_plan_hash_stable() -> None:
    plan = _grade_plan()
    assert plan.plan_hash() == _grade_plan().plan_hash()


def test_grade_plan_affected_clips() -> None:
    plan = _grade_plan(operations=(
        GradeOperation(op_id="1", kind=OP_PRIMARY, clip_id="c1"),
        GradeOperation(op_id="2", kind=OP_PRIMARY, clip_id="c2"),
        GradeOperation(op_id="3", kind=OP_PRIMARY, clip_id="c1"),
    ))
    assert plan.affected_clip_ids() == ("c1", "c2")


# --------------------------------------------------------------------------- #
# CG-06: HDR guard
# --------------------------------------------------------------------------- #


def test_hdr_guard_blocks_sdr_lut_on_hdr() -> None:
    ctx = _ctx(timeline_transfer_function=HDR_PQ, working_space=SPACE_REC2020)
    plan = _grade_plan(operations=(
        GradeOperation(op_id="1", kind=OP_LUT, clip_id="c1",
                       parameters={"transfer_function": SDR}),
    ))
    report = validate_hdr(plan, ctx)
    assert report.is_blocking
    assert any(v.code == "HDR_SDR_LUT_ON_HDR_TIMELINE" for v in report.errors)


def test_hdr_guard_warns_on_peak_exceed() -> None:
    ctx = _ctx(timeline_transfer_function=HDR_PQ, working_space=SPACE_REC2020)
    plan = _grade_plan()
    measurements = {"c1": ColorMeasurement(clip_id="c1", peak_luma=1200.0)}
    report = validate_hdr(plan, ctx, measurements=measurements, peak_nit_limit=1000.0)
    assert any(v.code == "HDR_PEAK_EXCEEDS_LIMIT" for v in report.violations)


def test_hdr_guard_allows_compatible_plan() -> None:
    ctx = _ctx()
    plan = _grade_plan()
    report = validate_hdr(plan, ctx)
    assert not report.is_blocking


# --------------------------------------------------------------------------- #
# CG-04: shot match
# --------------------------------------------------------------------------- #


def test_shot_match_separates_auto_vs_confirmation() -> None:
    report = ShotMatchReport(
        reference_clip_id="ref1",
        suggestions=(
            ShotMatchSuggestion(clip_id="c1", reference_clip_id="ref1",
                                confidence=0.9, suggested_operations=()),
            ShotMatchSuggestion(clip_id="c2", reference_clip_id="ref1",
                                confidence=0.5, suggested_operations=()),
            ShotMatchSuggestion(clip_id="c3", reference_clip_id="ref1",
                                confidence=0.9, suggested_operations=(),
                                excluded=True, exclude_reason="user locked"),
        ),
    )
    auto = report.auto_applicable()
    confirm = report.needs_confirmation()
    assert len(auto) == 1
    assert auto[0].clip_id == "c1"
    assert len(confirm) == 1
    assert confirm[0].clip_id == "c2"


# --------------------------------------------------------------------------- #
# CG-09: grade diff
# --------------------------------------------------------------------------- #


def test_compare_grade_detects_clipping_regression() -> None:
    before = {"c1": ColorMeasurement(clip_id="c1", clipping_ratio=0.01)}
    after = {"c1": ColorMeasurement(clip_id="c1", clipping_ratio=0.05)}
    report = compare_grade(before, after, regression_threshold=0.01)
    assert report.should_auto_rollback
    assert any(f.metric == "clipping_ratio" and f.regressed for f in report.findings)


# --------------------------------------------------------------------------- #
# CP-01: CompositeBrief + Plan
# --------------------------------------------------------------------------- #


def test_composite_brief_rejects_empty_scope() -> None:
    with pytest.raises(ValueError):
        CompositeBrief(
            brief_id="b1", project_id="p1", timeline_id="t1",
            shot_scope=(), desired_effect=EFFECT_HUD,
        )


def test_composite_plan_hash_stable() -> None:
    plan = CompositePlan(
        plan_id="cp1", brief_id="b1", project_id="p1", timeline_id="t1",
        base_generation=1,
        operations=(NodeGraphOperation(op_id="1", kind="add_node", node_type="blur"),),
    )
    same = CompositePlan(
        plan_id="cp2", brief_id="b1", project_id="p1", timeline_id="t1",
        base_generation=1,
        operations=(NodeGraphOperation(op_id="1", kind="add_node", node_type="blur"),),
    )
    assert plan.plan_hash() == same.plan_hash()


# --------------------------------------------------------------------------- #
# CP-03/09: composite plan validation
# --------------------------------------------------------------------------- #


def test_validate_composite_plan_flags_gpu_unavailable() -> None:
    plan = CompositePlan(
        plan_id="cp1", brief_id="b1", project_id="p1", timeline_id="t1",
        base_generation=1,
        operations=(NodeGraphOperation(op_id="1", kind="add_node", node_type="ai_generate",
                                       requires_gpu=True),),
    )
    report = validate_composite_plan(plan, available_node_types=("ai_generate",), gpu_available=False)
    assert not report.is_deliverable
    assert any(f.code == "GPU_REQUIRED_UNAVAILABLE" for f in report.errors)


def test_validate_composite_plan_flags_unknown_node_type() -> None:
    plan = CompositePlan(
        plan_id="cp1", brief_id="b1", project_id="p1", timeline_id="t1",
        base_generation=1,
        operations=(NodeGraphOperation(op_id="1", kind="add_node", node_type="magic"),),
    )
    report = validate_composite_plan(plan, available_node_types=("blur", "grade"))
    assert any(f.code == "UNKNOWN_NODE_TYPE" for f in report.errors)


# --------------------------------------------------------------------------- #
# IX-01: dependency graph
# --------------------------------------------------------------------------- #


def test_dependency_graph_detects_stale_downstream() -> None:
    dep = CrossDomainDependency(
        downstream_domain=DOMAIN_COLOR,
        upstream=DomainArtifactRef(domain=DOMAIN_EDITORIAL, artifact_id="edit-plan-1",
                                   revision=1, content_hash="h1"),
        affected_clip_ids=("c1", "c2"),
    )
    graph = DependencyGraph(dependencies=(dep,))
    # Upstream changed → color is stale.
    observed = (DomainArtifactRef(domain=DOMAIN_EDITORIAL, artifact_id="edit-plan-1",
                                  revision=2, content_hash="h2"),)
    assert DOMAIN_COLOR in graph.stale_downstream(observed)
    assert "c1" in graph.dirty_clip_ids(observed)


def test_dependency_graph_no_stale_when_unchanged() -> None:
    dep = CrossDomainDependency(
        downstream_domain=DOMAIN_COLOR,
        upstream=DomainArtifactRef(domain=DOMAIN_EDITORIAL, artifact_id="ep1",
                                   revision=1, content_hash="h1"),
    )
    graph = DependencyGraph(dependencies=(dep,))
    observed = (DomainArtifactRef(domain=DOMAIN_EDITORIAL, artifact_id="ep1",
                                  revision=1, content_hash="h1"),)
    assert graph.stale_downstream(observed) == ()


# --------------------------------------------------------------------------- #
# IX-02: render contract
# --------------------------------------------------------------------------- #


def test_render_contract_consistency() -> None:
    a = RenderContract(width=1920, height=1080, fps=24.0)
    b = RenderContract(width=1920, height=1080, fps=24.0)
    c = RenderContract(width=1920, height=1080, fps=30.0)
    assert a.is_consistent_with(b)
    assert not a.is_consistent_with(c)


def test_render_contract_hash_stable() -> None:
    a = RenderContract(width=1920, height=1080, fps=24.0)
    b = RenderContract(width=1920, height=1080, fps=24.0)
    assert a.hash() == b.hash()


# --------------------------------------------------------------------------- #
# IX-04: protected regions
# --------------------------------------------------------------------------- #


def test_protected_region_blocks_planner() -> None:
    registry = ProtectedRegionRegistry()
    registry.lock(ProtectedRegion(
        region_id="r1", kind=REGION_CLIP, target_ref="clip-1", domain=DOMAIN_EDITORIAL,
    ))
    assert registry.is_protected(DOMAIN_EDITORIAL, "clip-1")
    assert not registry.is_protected(DOMAIN_EDITORIAL, "clip-2")
    conflicts = registry.conflicts_for_domain(DOMAIN_EDITORIAL, ("clip-1", "clip-2"))
    assert len(conflicts) == 1


# --------------------------------------------------------------------------- #
# IX-06: cross-domain scorecard
# --------------------------------------------------------------------------- #


def test_scorecard_deliverable_when_all_thresholds_met() -> None:
    card = CrossDomainScorecard(
        scores=(
            DomainScore(domain=DOMAIN_EDITORIAL, score=0.9, threshold_met=True),
            DomainScore(domain=DOMAIN_COLOR, score=0.85, threshold_met=True),
            DomainScore(domain=DOMAIN_COMPOSITE, score=0.8, threshold_met=True),
        ),
    )
    assert card.is_deliverable
    assert card.weighted_score > 0.8


def test_scorecard_blocked_when_any_threshold_fails() -> None:
    card = CrossDomainScorecard(
        scores=(
            DomainScore(domain=DOMAIN_EDITORIAL, score=0.9, threshold_met=True),
            DomainScore(domain=DOMAIN_COLOR, score=0.3, threshold_met=False),
        ),
    )
    assert not card.is_deliverable
    assert len(card.blocking_issues) == 1


def test_scorecard_human_override_adjusts_score() -> None:
    card = CrossDomainScorecard(
        scores=(
            DomainScore(domain=DOMAIN_EDITORIAL, score=0.5, threshold_met=True,
                        human_override=0.9),
        ),
    )
    assert card.weighted_score == pytest.approx(0.9)
