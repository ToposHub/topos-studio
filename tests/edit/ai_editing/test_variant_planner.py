"""§13.1 矩阵：test_variant_planner。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.brief_service import BriefService
from src.features.edit.ai_editing.editorial_models import (
    DecisionState,
    DurationTarget,
    EditorialBrief,
    FrameRange,
    Platform,
    SegmentKind,
    SemanticSegment,
)
from src.features.edit.ai_editing.semantic_index import SemanticIndex
from src.features.edit.ai_editing.variant_planner import (
    PlannerConstraints,
    PlannerInput,
    STRATEGY_A_TALKING_HEAD,
    STRATEGY_B_BROLL,
    VariantPlanner,
    compare_variants,
)


@pytest.fixture
def setup():
    bs = BriefService()
    bs.save(EditorialBrief(
        brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
        target_duration=DurationTarget(720, 648, 792),
        required_beats=("hook", "product_value", "call_to_action"),
    ))
    idx = SemanticIndex()
    for i, (s, d, t) in enumerate([(0, 108, "开场反应"), (108, 432, "产品核心卖点"), (540, 180, "立即点击")]):
        idx.add(SemanticSegment(segment_id=f"t{i}", asset_id="a1",
            source_range=FrameRange(s, d), kind=SegmentKind.TRANSCRIPT,
            payload={"text": t}, confidence=0.85 + i * 0.04, user_verified=True))
    for i, (s, d) in enumerate([(0, 72), (72, 72), (540, 72)]):
        idx.add(SemanticSegment(segment_id=f"s{i}", asset_id="a1",
            source_range=FrameRange(s, d), kind=SegmentKind.SHOT, confidence=0.9))
    inp = PlannerInput(brief_id="b1", brief_revision=1, asset_id="a1",
        timeline_id="t1", project_id="p1", base_revision=0, target_duration=720)
    return bs, idx, inp


def test_planner_produces_at_least_two_variants(setup):
    bs, idx, inp = setup
    planner = VariantPlanner(bs)
    variants = planner.plan(inp, idx)
    assert len(variants) >= 2
    labels = {v.label for v in variants}
    assert STRATEGY_A_TALKING_HEAD in labels
    assert STRATEGY_B_BROLL in labels


def test_variants_have_outline_decisions_and_plan(setup):
    bs, idx, inp = setup
    variants = VariantPlanner(bs).plan(inp, idx)
    for v in variants:
        assert len(v.outline) == 3  # 3 beats
        assert len(v.decisions) >= 1
        assert v.edit_plan_ref  # 有编译的 plan
        # 每个 decision 带 evidence + alternatives
        for d in v.decisions:
            assert d.evidence_refs
            assert d.alternatives_considered is not None


def test_variants_scores_within_bounds(setup):
    bs, idx, inp = setup
    variants = VariantPlanner(bs).plan(inp, idx)
    for v in variants:
        assert 0.0 <= v.scores.beat_coverage <= 1.0
        assert 0.0 <= v.scores.duration_fit <= 1.0
        assert v.scores.hard_constraints_passed is True


def test_planner_respects_rejected_decisions(setup):
    bs, idx, inp = setup
    # 拒绝 t0 的选择范围
    rejected = ("a1:0-108",)
    planner = VariantPlanner(bs)
    variants = planner.plan(inp, idx, constraints=PlannerConstraints(rejected_subjects=rejected))
    # 任何 decision 不应选中 t0
    for v in variants:
        for d in v.decisions:
            sig = f"{d.subject.asset_id}:{d.subject.source.start}-{d.subject.source.duration}"
            assert sig != "a1:0-108"


def test_planner_respects_locked_ranges(setup):
    bs, idx, inp = setup
    from src.features.edit.ai_editing.brief_service import LockedSourceRange
    locked = (LockedSourceRange(asset_id="a1", source=FrameRange(108, 432), label="must use"),)
    variants = VariantPlanner(bs).plan(
        inp, idx, constraints=PlannerConstraints(locked_ranges=locked)
    )
    # 至少一个 variant 的 decision 包含锁定范围
    all_sigs = set()
    for v in variants:
        for d in v.decisions:
            if d.subject:
                all_sigs.add(f"{d.subject.asset_id}:{d.subject.source.start}-{d.subject.source.duration}")
    assert "a1:108-432" in all_sigs


def test_compare_variants_ranking(setup):
    bs, idx, inp = setup
    variants = VariantPlanner(bs).plan(inp, idx)
    comp = compare_variants(variants)
    ranked = comp.rank_by("beat_coverage")
    assert len(ranked) == len(variants)
    # to_dict 含每个 variant 的比较维度
    d = comp.to_dict()
    for vid in ranked:
        assert "duration_frames" in d["by_variant"][vid]


def test_proposed_decisions_have_proposed_state(setup):
    bs, idx, inp = setup
    variants = VariantPlanner(bs).plan(inp, idx)
    for v in variants:
        for dec in v.decisions:
            assert dec.state == DecisionState.PROPOSED


def test_two_strategies_produce_different_beat_allocation(setup):
    """A/B 两种策略的 beat 时长分配应不同（可比较）。"""
    bs, idx, inp = setup
    planner = VariantPlanner(bs)
    v_a = next(v for v in planner.plan(inp, idx, strategies=(STRATEGY_A_TALKING_HEAD,)) )
    v_b = next(v for v in planner.plan(inp, idx, strategies=(STRATEGY_B_BROLL,)) )
    a_hook = next(b.target_range.duration for b in v_a.outline if b.beat_id == "hook")
    b_hook = next(b.target_range.duration for b in v_b.outline if b.beat_id == "hook")
    # A 策略 hook=0.15*720=108, B 策略 hook=0.3*720=216
    assert a_hook < b_hook
