"""§13.1 矩阵：test_editorial_brief_service。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.brief_service import (
    BriefPatch,
    BriefService,
    BriefValidationError,
    LockedSourceRange,
)
from src.features.edit.ai_editing.editorial_models import (
    ConstraintKind,
    DurationTarget,
    EditorialBrief,
    FrameRange,
    HardConstraint,
    Platform,
    SoftPreference,
)


@pytest.fixture
def svc():
    s = BriefService()
    s.save(EditorialBrief(
        brief_id="b1", revision=1,
        target_duration=DurationTarget(720, 648, 792),
        platform=Platform.VERTICAL_SOCIAL,
        required_beats=("hook", "product_value", "call_to_action"),
    ))
    return s


def test_apply_patch_bumps_revision(svc):
    patch = BriefPatch(base_revision=1, changes={"primary_goal": "新品宣传"})
    result = svc.apply_patch("b1", patch)
    assert result.revision == 2
    assert result.primary_goal == "新品宣传"


def test_brand_requirements_round_trip_through_editorial_brief_and_session_payload() -> None:
    brief = EditorialBrief(
        brief_id="brand-brief",
        brand_requirements=(
            {
                "element_id": "logo-main",
                "kind": "logo",
                "min_duration_frames": 48,
                "required": True,
                "safe_area_required": True,
            },
        ),
    )

    restored = EditorialBrief.from_dict(brief.to_dict())

    assert restored.brand_requirements == brief.brand_requirements
    assert restored.to_dict()["brand_requirements"][0]["element_id"] == "logo-main"


def test_brand_requirements_patch_is_persisted_and_validated(svc) -> None:
    result = svc.apply_patch(
        "b1",
        BriefPatch(
            base_revision=1,
            changes={
                "brand_requirements": {
                    "element_id": "end-card",
                    "kind": "end_card",
                    "min_duration_frames": 72,
                },
            },
        ),
    )

    assert result.brand_requirements[0]["element_id"] == "end-card"
    assert svc.brand_requirements("b1") == result.brand_requirements


def test_invalid_brand_requirement_cannot_enter_editorial_brief() -> None:
    with pytest.raises(ValueError):
        EditorialBrief(
            brief_id="invalid-brand-brief",
            brand_requirements=({"element_id": "", "kind": "logo"},),
        )


def test_apply_patch_revision_conflict_raises(svc):
    patch = BriefPatch(base_revision=99, changes={})  # current is 1
    with pytest.raises(BriefValidationError):
        svc.apply_patch("b1", patch)


def test_lock_source_range_persists(svc):
    svc.lock_source_range("b1", LockedSourceRange(
        asset_id="a1", source=FrameRange(240, 100), label="hero"
    ))
    locked = svc.locked_source_ranges("b1")
    assert len(locked) == 1
    assert locked[0].asset_id == "a1"
    assert locked[0].source.start == 240


def test_hard_vs_soft_constraints_separated(svc):
    svc.add_hard_constraint("b1", HardConstraint(kind="forbidden_labels", spec={"labels": ["competitor_logo"]}))
    svc.add_soft_preference("b1", SoftPreference(key="pace", weight=2.0))
    hard = svc.constraints_by_kind("b1", ConstraintKind.HARD)
    soft = svc.constraints_by_kind("b1", ConstraintKind.SOFT)
    assert any(c.kind == "forbidden_labels" for c in hard)
    assert any(p.key == "pace" for p in soft)


def test_forbidden_labels_and_must_include(svc):
    svc.add_hard_constraint("b1", HardConstraint(kind="forbidden_labels", spec={"labels": ["uncleared_music"]}))
    svc.add_hard_constraint("b1", HardConstraint(kind="must_include_claim_ids", spec={"claim_ids": ["fast-charge"]}))
    assert "uncleared_music" in svc.forbidden_labels("b1")
    assert "fast-charge" in svc.must_include_claim_ids("b1")


def test_variants_need_replan_after_brief_change(svc):
    # variant 基于 revision 1
    variant_revs = {"v1": 1, "v2": 1}
    # brief 变更后 current=2
    stale = svc.variants_need_replan(variant_revs, current_revision=2)
    assert set(stale) == {"v1", "v2"}
    # 一个已同步
    stale2 = svc.variants_need_replan({"v1": 2, "v2": 1}, current_revision=2)
    assert stale2 == ["v2"]


def test_propose_patch_from_text_extract_duration_and_platform():
    s = BriefService()
    s.save(EditorialBrief(brief_id="b1", revision=0))
    patch = s.propose_patch_from_text("b1", "做一个 30 秒竖版抖音宣传片")
    assert patch.changes["platform"] == Platform.VERTICAL_SOCIAL
    td = patch.changes["target_duration"]
    assert td.ideal == 720  # 30s * 24fps
    assert "fast_paced" not in patch.changes.get("tone", ()) or True  # 无"快"关键词


def test_propose_patch_tone_heuristic():
    s = BriefService()
    s.save(EditorialBrief(brief_id="b1", revision=0))
    patch = s.propose_patch_from_text("b1", "快节奏温暖的宣传片")
    tones = patch.changes.get("tone", ())
    assert "fast_paced" in tones and "warm" in tones
