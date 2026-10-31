"""B1 proposal / revision / fallback / card / preview contract tests.

These tests lock the MG-02 / MG-03 / MG-04 / MG-05 / MG-08 acceptance
criteria for the satisfaction layer:

* a creative proposal forms, hashes and round-trips (MG-04)
* a plan compiles from a proposal via the dry-run registry, embedding the
  TeachingAIProposal so base_hash re-verification still works (MG-04)
* an empty / failing dry-run raises a stable MotionGraphicFailure instead of
  silently producing a fallback plan (MG-04 / MG-05)
* the revision log supports an append-only lineage and refuses to patch old
  revisions; only one revision may be accepted at a time (MG-08)
* the fallback classifier maps real exceptions to the three stable states and
  never auto-produces a template (MG-05)
* the proposal card model carries provenance, quality flags and hashes so the
  UI never has to parse free text (MG-02)
* storyboard sampling is deterministic; preview hash is stable for the same
  plan / scene / profile (MG-03)
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.ai.brief import MotionGraphicBrief
from src.features.teaching_animation.ai.commands import (
    TeachingAICommand,
    TeachingAICommandRegistry,
)
from src.features.teaching_animation.ai.error_codes import (
    MOTION_GRAPHIC_INVALID_SCENE_PLAN,
    MOTION_GRAPHIC_LLM_PARSE_FAILED,
    MOTION_GRAPHIC_LLM_TIMEOUT,
    MOTION_GRAPHIC_OPTIONAL_TEMPLATE_FALLBACK,
    MOTION_GRAPHIC_REVISION_CONFLICT,
    MotionGraphicFailure,
)
from src.features.teaching_animation.ai.fallback import (
    ACTION_CANCEL,
    ACTION_CHOOSE_TEMPLATE,
    ACTION_RETRY,
    FALLBACK_INVALID_SCENE_PLAN,
    FALLBACK_NONE,
    FALLBACK_OPTIONAL_TEMPLATE,
    FALLBACK_PROVIDER_UNAVAILABLE,
    FallbackPolicy,
    classify_generation_failure,
)
from src.features.teaching_animation.ai.preview import (
    PREVIEW_RESOLUTIONS,
    RenderProfile,
    StoryboardFrame,
    build_preview,
    compute_preview_hash,
    sample_storyboard_frames,
    summarize_scene_diff,
)
from src.features.teaching_animation.ai.proposal import (
    SOURCE_FALLBACK_INVALID_PLAN,
    SOURCE_LLM,
    SOURCE_TEMPLATE,
    CreativeProposal,
    MotionGraphicPlan,
    NarrativeBeat,
    StyleTokens,
    build_plan,
    is_fallback_source,
)
from src.features.teaching_animation.ai.proposal_card import (
    MotionGraphicProposalCard,
    build_fallback_card,
    build_plan_card,
)
from src.features.teaching_animation.ai.revision import (
    FEEDBACK_COPY,
    FEEDBACK_PACING,
    FEEDBACK_PALETTE,
    FeedbackPatch,
    MotionGraphicRevisionLog,
    STATUS_ACCEPTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
)
from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _brief() -> MotionGraphicBrief:
    return MotionGraphicBrief(
        brief_id="mg-brief-1",
        topic="介绍 AI Agent",
        duration_seconds=15.0,
        canvas_aspect="16:9",
    )


def _proposal(brief: MotionGraphicBrief | None = None, **overrides) -> CreativeProposal:
    brief = brief or _brief()
    base = dict(
        proposal_id="mg-prop-1",
        brief_id=brief.brief_id,
        brief_revision=brief.revision,
        brief_hash=brief.brief_hash(),
        title="方案 A · 科技冷色",
        summary="15 秒介绍 AI Agent 的四个步骤。",
        beats=(
            NarrativeBeat("b1", "开场", 0.0, 3.0, on_screen_copy="AI Agent"),
            NarrativeBeat("b2", "理解", 3.0, 3.0),
            NarrativeBeat("b3", "规划", 6.0, 3.0),
            NarrativeBeat("b4", "执行 + 验证", 9.0, 6.0),
        ),
        style=StyleTokens(palette={"background": "#0f172a", "accent": "#38bdf8"}),
    )
    base.update(overrides)
    return CreativeProposal(**base)


def _base_scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(
        "Base", width=1920, height=1080, duration_frames=360,
    )


def _commands_for(scene: TeachingSceneDocument) -> list[TeachingAICommand]:
    element = SceneElement(
        element_id="title",
        type="text",
        transform=SceneTransform(x=100, y=100, width=600, height=120),
        style={"font_size": 64, "color": "#ffffff"},
        content="AI Agent",
    )
    return [TeachingAICommand("add_element", {"element": element.to_dict()})]


def _plan(proposal: CreativeProposal | None = None) -> MotionGraphicPlan:
    proposal = proposal or _proposal()
    registry = TeachingAICommandRegistry()
    scene = _base_scene()
    return build_plan(
        proposal=proposal,
        base_scene=scene,
        commands=_commands_for(scene),
        registry=registry,
        target_track_id="graphics-1",
        target_insert_frame=0,
    )


# --------------------------------------------------------------------------- #
# MG-04: CreativeProposal + MotionGraphicPlan
# --------------------------------------------------------------------------- #


def test_creative_proposal_round_trips_and_hashes() -> None:
    proposal = _proposal()
    restored = CreativeProposal.from_dict(proposal.to_dict())
    assert restored.proposal_hash() == proposal.proposal_hash()
    assert restored.duration_seconds == pytest.approx(15.0)
    assert restored.source == SOURCE_LLM
    assert not restored.is_fallback


def test_creative_proposal_rejects_empty_title() -> None:
    with pytest.raises(ValueError):
        _proposal(title=" ")


def test_build_plan_embeds_dry_run_and_hashes() -> None:
    proposal = _proposal()
    plan = _plan(proposal)
    assert plan.commands
    assert plan.scene_hash
    assert plan.plan_hash
    assert plan.diff_hash == plan.proposal_dry_run.diff_hash
    # The plan's proposed scene reflects the add_element command.
    assert plan.proposed_scene.element_by_id("title") is not None


def test_build_plan_propagates_fallback_source_from_proposal() -> None:
    proposal = _proposal(source=SOURCE_FALLBACK_INVALID_PLAN)
    plan = _plan(proposal)
    assert plan.is_fallback
    assert plan.source == SOURCE_FALLBACK_INVALID_PLAN


def test_build_plan_raises_stable_failure_on_empty_commands() -> None:
    proposal = _proposal()
    registry = TeachingAICommandRegistry()
    with pytest.raises(MotionGraphicFailure) as exc_info:
        build_plan(
            proposal=proposal,
            base_scene=_base_scene(),
            commands=[],
            registry=registry,
        )
    assert exc_info.value.code == MOTION_GRAPHIC_INVALID_SCENE_PLAN


def test_build_plan_raises_stable_failure_on_invalid_command() -> None:
    proposal = _proposal()
    registry = TeachingAICommandRegistry()
    bogus = TeachingAICommand("not_a_real_command", {})
    with pytest.raises(MotionGraphicFailure):
        build_plan(
            proposal=proposal,
            base_scene=_base_scene(),
            commands=[bogus],
            registry=registry,
        )


# --------------------------------------------------------------------------- #
# MG-08: revision log
# --------------------------------------------------------------------------- #


def test_revision_log_starts_lineage_with_initial_proposal() -> None:
    log = MotionGraphicRevisionLog()
    revision = log.add_initial(_proposal())
    assert revision.status == STATUS_PENDING
    assert revision.parent_id is None
    assert log.latest_for(revision.lineage_id) is revision
    assert log.lineage(revision.lineage_id) == (revision,)


def test_revision_log_appends_feedback_and_supersedes_pending_parent() -> None:
    log = MotionGraphicRevisionLog()
    first = log.add_initial(_proposal())
    patch = FeedbackPatch(
        patch_id="fp-1",
        categories=(FEEDBACK_COPY,),
        requested_changes={"title": "你的 AI 剪辑伙伴"},
        note="标题更亲切",
    )
    child = log.add_feedback(first.revision_id, _proposal(title="方案 A2"), patch)

    assert child.parent_id == first.revision_id
    assert child.feedback is patch
    # Parent was pending → now superseded.
    refreshed_first = log.get(first.revision_id)
    assert refreshed_first.status == STATUS_SUPERSEDED
    assert log.latest_for(first.lineage_id) is child


def test_revision_log_refuses_patch_on_old_revision() -> None:
    log = MotionGraphicRevisionLog()
    first = log.add_initial(_proposal())
    patch1 = FeedbackPatch(patch_id="fp-1", categories=(FEEDBACK_COPY,))
    patch2 = FeedbackPatch(patch_id="fp-2", categories=(FEEDBACK_PALETTE,))
    log.add_feedback(first.revision_id, _proposal(title="v2"), patch1)

    # Trying to patch the now-old `first` must fail with a stable code.
    with pytest.raises(MotionGraphicFailure) as exc_info:
        log.add_feedback(first.revision_id, _proposal(title="v3"), patch2)
    assert exc_info.value.code == MOTION_GRAPHIC_REVISION_CONFLICT


def test_revision_log_accept_replaces_previous_accept() -> None:
    log = MotionGraphicRevisionLog()
    first = log.add_initial(_proposal())
    patch = FeedbackPatch(patch_id="fp-1", categories=(FEEDBACK_PACING,))
    second = log.add_feedback(first.revision_id, _proposal(title="v2"), patch)

    log.accept(first.revision_id)
    assert log.accepted_revision(first.lineage_id).revision_id == first.revision_id

    log.accept(second.revision_id)
    # Accepting the second supersedes the first.
    assert log.get(first.revision_id).status == STATUS_SUPERSEDED
    assert log.accepted_revision(first.lineage_id).revision_id == second.revision_id


def test_revision_log_reject_then_accept_blocked() -> None:
    log = MotionGraphicRevisionLog()
    revision = log.add_initial(_proposal())
    log.reject(revision.revision_id, reason="节奏不对")
    assert log.get(revision.revision_id).status == STATUS_REJECTED
    with pytest.raises(MotionGraphicFailure):
        log.accept(revision.revision_id)


def test_revision_log_attach_plan_only_to_active_revisions() -> None:
    log = MotionGraphicRevisionLog()
    revision = log.add_initial(_proposal())
    plan = _plan(revision.proposal)
    log.attach_plan(revision.revision_id, plan)
    assert log.get(revision.revision_id).plan is plan

    log.reject(revision.revision_id)
    with pytest.raises(MotionGraphicFailure):
        log.attach_plan(revision.revision_id, plan)


def test_feedback_patch_rejects_unknown_category() -> None:
    with pytest.raises(ValueError):
        FeedbackPatch(patch_id="fp-1", categories=("not_a_category",))


# --------------------------------------------------------------------------- #
# MG-05: fallback
# --------------------------------------------------------------------------- #


def test_classify_generation_failure_maps_timeout() -> None:
    decision = classify_generation_failure(None, reason="request timed out")
    assert decision.state == FALLBACK_PROVIDER_UNAVAILABLE
    assert decision.error_code == MOTION_GRAPHIC_LLM_TIMEOUT
    assert decision.allows_template
    assert ACTION_RETRY in decision.next_actions
    assert ACTION_CHOOSE_TEMPLATE in decision.next_actions


def test_classify_generation_failure_maps_parse_error() -> None:
    failure = MotionGraphicFailure(MOTION_GRAPHIC_LLM_PARSE_FAILED, "bad json")
    decision = classify_generation_failure(failure)
    assert decision.state == FALLBACK_INVALID_SCENE_PLAN
    assert decision.error_code == MOTION_GRAPHIC_LLM_PARSE_FAILED


def test_classify_generation_success_returns_none_state() -> None:
    decision = classify_generation_failure(None)
    assert decision.state == FALLBACK_NONE
    assert decision.is_success
    assert not decision.allows_template


def test_fallback_policy_never_auto_templates() -> None:
    failure = classify_generation_failure(None, reason="timed out")
    policy = FallbackPolicy()
    # On failure the template *may* be offered, but the decision itself is
    # still a failure, not a template result.
    assert policy.may_use_template(failure)
    assert failure.state != FALLBACK_OPTIONAL_TEMPLATE
    # Only an explicit user choice turns it into a template decision.
    chosen = policy.template_chosen()
    assert chosen.state == FALLBACK_OPTIONAL_TEMPLATE
    assert chosen.error_code == MOTION_GRAPHIC_OPTIONAL_TEMPLATE_FALLBACK


def test_fallback_policy_blocked_when_template_disabled() -> None:
    policy = FallbackPolicy(template_allowed=False)
    failure = classify_generation_failure(None, reason="timed out")
    assert not policy.may_use_template(failure)
    with pytest.raises(MotionGraphicFailure):
        policy.template_chosen()


# --------------------------------------------------------------------------- #
# MG-02: proposal card
# --------------------------------------------------------------------------- #


def test_build_plan_card_carries_provenance_and_hashes() -> None:
    proposal = _proposal()
    plan = _plan(proposal)
    card = build_plan_card(
        plan, proposal,
        card_id="card-1",
        target_track_label="Graphics",
        quality_report={"errors": ["对比度过低"], "warnings": ["字号偏小"]},
    )
    assert isinstance(card, MotionGraphicProposalCard)
    assert card.title == proposal.title
    assert card.source_label == SOURCE_LLM
    assert not card.is_fallback
    assert card.plan_hash == plan.plan_hash
    assert card.scene_hash == plan.scene_hash
    assert card.canvas_aspect in {"16:9", "1:1", "9:16"}
    assert card.target_track_label == "Graphics"
    assert card.has_blocking_quality_errors
    assert "对比度过低" in card.quality_errors
    # Default affordances for a plan awaiting confirmation.
    assert "confirm" in card.actions


def test_build_fallback_card_never_claims_custom_success() -> None:
    failure = classify_generation_failure(None, reason="timed out")
    card = build_fallback_card(
        failure,
        card_id="card-fail",
        brief_id="mg-brief-1",
        brief_revision=1,
    )
    assert card.is_fallback
    assert card.source_label != SOURCE_LLM
    assert card.status == "failed"
    assert "confirm" not in card.actions
    assert "cancel" in card.actions


def test_card_round_trips_through_dict() -> None:
    proposal = _proposal()
    plan = _plan(proposal)
    card = build_plan_card(plan, proposal, card_id="card-1")
    payload = card.to_dict()
    assert payload["source_label"] == SOURCE_LLM
    assert payload["canvas_size"] == [1920, 1080]
    assert payload["plan_hash"] == plan.plan_hash


# --------------------------------------------------------------------------- #
# MG-03: preview sampling + hashing
# --------------------------------------------------------------------------- #


def test_sample_storyboard_frames_is_deterministic_and_includes_endpoints() -> None:
    scene = _base_scene()
    frames = sample_storyboard_frames(scene, count=5)
    again = sample_storyboard_frames(scene, count=5)
    assert frames == again
    assert frames[0] == 0
    assert frames[-1] == scene.duration_frames - 1
    assert len(frames) == 5


def test_sample_storyboard_frames_handles_short_scenes() -> None:
    short = TeachingSceneDocument.create_default(
        "Short", width=64, height=48, duration_frames=3,
    )
    frames = sample_storyboard_frames(short, count=5)
    assert len(frames) == 5
    assert all(0 <= f < short.duration_frames for f in frames)


def test_compute_preview_hash_is_stable_for_same_inputs() -> None:
    scene = _base_scene()
    profile = RenderProfile(width=640, height=360, fps=24.0)
    diff = summarize_scene_diff(proposed_scene=scene)
    h1 = compute_preview_hash(
        plan_hash="plan-1", scene_hash="scene-1",
        render_profile=profile, storyboard_indices=(0, 90, 180, 269, 359),
        diff_summary=diff,
    )
    h2 = compute_preview_hash(
        plan_hash="plan-1", scene_hash="scene-1",
        render_profile=profile, storyboard_indices=(0, 90, 180, 269, 359),
        diff_summary=diff,
    )
    assert h1 == h2
    # Different plan → different hash.
    h3 = compute_preview_hash(
        plan_hash="plan-2", scene_hash="scene-1",
        render_profile=profile, storyboard_indices=(0, 90, 180, 269, 359),
        diff_summary=diff,
    )
    assert h1 != h3


def test_build_preview_marks_failure_when_all_frames_errored() -> None:
    plan = _plan()
    profile = RenderProfile(width=640, height=360, fps=24.0)
    frames = (
        StoryboardFrame(0, "first", 0.0, profile, render_error="render failed"),
        StoryboardFrame(180, "middle", 7.5, profile, render_error="render failed"),
    )
    preview = build_preview(
        plan_hash=plan.plan_hash,
        scene_hash=plan.scene_hash,
        scene=plan.proposed_scene,
        storyboard=frames,
        render_profile=profile,
    )
    assert preview.is_failed
    assert not preview.is_playable


def test_summarize_scene_diff_reports_added_elements_for_new_clip() -> None:
    plan = _plan()
    summary = summarize_scene_diff(
        proposed_scene=plan.proposed_scene,
        timeline_track_id="graphics-1",
        timeline_track_label="Graphics",
        timeline_insert_frame=0,
    )
    assert "title" in summary.added_element_ids
    lines = summary.summary_lines()
    assert any("新增" in line for line in lines)
    assert any("Graphics" in line for line in lines)
