"""Qt-free view-model for the Motion Graphic proposal card (MG-02).

MG-02 replaces the old ``set_motion_graphic_proposal(request_id, title,
details)`` text-only card with a typed model the UI can render from.  The
card needs to answer — without the user opening the dev log — "what will be
generated, where will it land, is it a real model result, what does it cost,
what are the risks".

This module defines that view-model as plain dataclasses plus a builder that
pulls the right fields from a :class:`MotionGraphicPlan` (or a fallback
decision) and stamps provenance.  The Qt widget in
``ai_edit_assistant_panel.py`` will be adapted in a later step to consume
this model instead of two loose strings; keeping the model Qt-free means the
same data structure can drive tests, accessibility tools and the audit log.

Design rules:

* every field the user needs to make a "satisfied / not satisfied" decision
  is present and typed;
* provenance (``source_label`` / ``is_fallback``) is always set so a fallback
  can never masquerade as a custom model result;
* the model is immutable and serializable for snapshot/restore.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .error_codes import suggested_next_step
from .fallback import ACTION_CANCEL, ACTION_CHOOSE_TEMPLATE, ACTION_EDIT_BRIEF, ACTION_RETRY, MotionGraphicFallbackDecision
from .proposal import SOURCE_LLM, CreativeProposal, MotionGraphicPlan, is_fallback_source


# --------------------------------------------------------------------------- #
# Element / beat summaries shown on the card
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ElementSummary:
    """One row in the "elements this clip will create" list."""

    element_id: str
    kind: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "kind": self.kind,
            "description": self.description,
        }


@dataclass(frozen=True)
class BeatSummary:
    """One row in the "pacing / beat sheet" list."""

    label: str
    start_seconds: float
    duration_seconds: float
    on_screen_copy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start_seconds": self.start_seconds,
            "duration_seconds": self.duration_seconds,
            "on_screen_copy": self.on_screen_copy,
        }


# --------------------------------------------------------------------------- #
# Card model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicProposalCard:
    """Typed, Qt-free model for the proposal confirmation card (MG-02).

    Every field maps to a region of the card UI:

    * ``title`` / ``summary`` → header + intro paragraph;
    * ``elements`` → "scene elements" list (count + role per element);
    * ``beats`` → "pacing" list (label + time + on-screen copy);
    * ``duration_seconds`` / ``canvas_aspect`` / ``canvas_size`` → format row;
    * ``target_track_label`` / ``target_insert_frame`` → insertion row;
    * ``risks`` / ``estimated_cost_units`` / ``quality_warnings`` → risk row;
    * ``source_label`` / ``is_fallback`` / ``provider`` → provenance row, the
      bit that stops a fallback from masquerading as a custom result;
    * ``scene_hash`` / ``plan_hash`` / ``diff_hash`` → hash row, so the user
      can see "this is exactly the plan that will be applied";
    * ``actions`` → ordered button labels the card should show;
    * ``status`` → draft / waiting / applied / superseded, driving styling.
    """

    card_id: str
    title: str
    summary: str
    brief_id: str
    brief_revision: int
    revision_id: str | None = None
    lineage_id: str | None = None
    elements: tuple[ElementSummary, ...] = ()
    beats: tuple[BeatSummary, ...] = ()
    duration_seconds: float = 0.0
    canvas_aspect: str = "16:9"
    canvas_size: tuple[int, int] = (1920, 1080)
    target_track_label: str | None = None
    target_insert_frame: int | None = None
    risks: tuple[str, ...] = ()
    estimated_cost_units: float = 0.0
    quality_warnings: tuple[str, ...] = ()
    quality_errors: tuple[str, ...] = ()
    source_label: str = SOURCE_LLM
    provider: str | None = None
    model_id: str | None = None
    scene_hash: str | None = None
    plan_hash: str | None = None
    diff_hash: str | None = None
    preview_hash: str | None = None
    actions: tuple[str, ...] = ()
    status: str = "draft"

    @property
    def is_fallback(self) -> bool:
        return is_fallback_source(self.source_label)

    @property
    def has_blocking_quality_errors(self) -> bool:
        return bool(self.quality_errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "title": self.title,
            "summary": self.summary,
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "revision_id": self.revision_id,
            "lineage_id": self.lineage_id,
            "elements": [item.to_dict() for item in self.elements],
            "beats": [item.to_dict() for item in self.beats],
            "duration_seconds": self.duration_seconds,
            "canvas_aspect": self.canvas_aspect,
            "canvas_size": list(self.canvas_size),
            "target_track_label": self.target_track_label,
            "target_insert_frame": self.target_insert_frame,
            "risks": list(self.risks),
            "estimated_cost_units": self.estimated_cost_units,
            "quality_warnings": list(self.quality_warnings),
            "quality_errors": list(self.quality_errors),
            "source_label": self.source_label,
            "provider": self.provider,
            "model_id": self.model_id,
            "scene_hash": self.scene_hash,
            "plan_hash": self.plan_hash,
            "diff_hash": self.diff_hash,
            "preview_hash": self.preview_hash,
            "actions": list(self.actions),
            "status": self.status,
        }


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _element_summaries_from_scene(plan: MotionGraphicPlan) -> tuple[ElementSummary, ...]:
    scene = plan.proposed_scene
    summaries: list[ElementSummary] = []
    for element in scene.elements:
        content = element.content
        if isinstance(content, Mapping):
            description = str(content.get("kind") or content.get("text") or "")
        else:
            description = str(content or "")
        summaries.append(ElementSummary(
            element_id=element.element_id,
            kind=element.type,
            description=description[:120],
        ))
    return tuple(summaries)


def _beat_summaries_from_proposal(proposal: CreativeProposal) -> tuple[BeatSummary, ...]:
    return tuple(
        BeatSummary(
            label=beat.label,
            start_seconds=beat.start_seconds,
            duration_seconds=beat.duration_seconds,
            on_screen_copy=beat.on_screen_copy,
        )
        for beat in proposal.beats
    )


def build_plan_card(
    plan: MotionGraphicPlan,
    proposal: CreativeProposal,
    *,
    card_id: str,
    target_track_label: str | None = None,
    preview_hash: str | None = None,
    quality_report: Mapping[str, Any] | None = None,
    revision_id: str | None = None,
    lineage_id: str | None = None,
    actions: tuple[str, ...] | None = None,
    status: str = "waiting",
) -> MotionGraphicProposalCard:
    """Build a card model from a compiled plan + its source proposal.

    The card inherits provenance from the plan (which in turn inherited it
    from the proposal), so a fallback plan is labelled as a fallback on the
    card without any extra wiring.
    """

    quality = quality_report or {}
    quality_errors = tuple(str(msg) for msg in quality.get("errors", []))
    quality_warnings = tuple(str(msg) for msg in quality.get("warnings", []))

    if actions is None:
        # Default affordances for a normal plan awaiting confirmation.
        actions = ("continue_editing", "cancel", "confirm")

    return MotionGraphicProposalCard(
        card_id=card_id,
        title=proposal.title,
        summary=proposal.summary,
        brief_id=plan.brief_id,
        brief_revision=plan.brief_revision,
        revision_id=revision_id,
        lineage_id=lineage_id,
        elements=_element_summaries_from_scene(plan),
        beats=_beat_summaries_from_proposal(proposal),
        duration_seconds=proposal.duration_seconds,
        canvas_aspect=_canvas_aspect_from_scene(plan.proposed_scene),
        canvas_size=(plan.proposed_scene.width, plan.proposed_scene.height),
        target_track_label=target_track_label,
        target_insert_frame=plan.target_insert_frame,
        risks=plan.estimated_risks,
        estimated_cost_units=plan.estimated_cost_units,
        quality_warnings=quality_warnings,
        quality_errors=quality_errors,
        source_label=plan.source,
        provider=plan.provider,
        model_id=plan.model_id,
        scene_hash=plan.scene_hash,
        plan_hash=plan.plan_hash,
        diff_hash=plan.diff_hash,
        preview_hash=preview_hash,
        actions=actions,
        status=status,
    )


def build_fallback_card(
    decision: MotionGraphicFallbackDecision,
    *,
    card_id: str,
    brief_id: str,
    brief_revision: int,
    title: str = "生成未完成",
    summary: str | None = None,
    revision_id: str | None = None,
    lineage_id: str | None = None,
) -> MotionGraphicProposalCard:
    """Build a card for a failed generation that may offer the template opt-in.

    The card never claims the result is a custom model output: ``source_label``
    is set from the decision state, and ``summary`` explains what went wrong
    plus the suggested next step.
    """

    from .fallback import source_for_fallback

    if decision.is_success:
        # Defensive: callers should use build_plan_card for success.
        source_label = SOURCE_LLM
        actions: tuple[str, ...] = ("continue_editing", "cancel", "confirm")
        status = "waiting"
        message = summary or "生成已完成，请查看方案。"
    else:
        source_label = source_for_fallback(decision.state)
        # Build a clear, labelled summary that explains the failure.
        hint = suggested_next_step(decision.error_code)
        message = summary or f"{decision.message} {hint}".strip()
        action_map = {
            ACTION_RETRY: "retry",
            ACTION_EDIT_BRIEF: "edit_brief",
            ACTION_CHOOSE_TEMPLATE: "choose_template",
            ACTION_CANCEL: "cancel",
        }
        actions = tuple(
            action_map[a] for a in decision.next_actions if a in action_map
        ) or ("cancel",)
        status = "failed"

    return MotionGraphicProposalCard(
        card_id=card_id,
        title=title,
        summary=message,
        brief_id=brief_id,
        brief_revision=brief_revision,
        revision_id=revision_id,
        lineage_id=lineage_id,
        source_label=source_label,
        actions=actions,
        status=status,
        quality_errors=(decision.message,) if decision.is_failure else (),
        risks=tuple(decision.details.get("reasons", [])) if decision.is_failure else (),
    )


def _canvas_aspect_from_scene(scene: Any) -> str:
    width = int(getattr(scene, "width", 0) or 0)
    height = int(getattr(scene, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return "16:9"
    if width == height:
        return "1:1"
    if width > height:
        ratio = width / height
        if abs(ratio - 16 / 9) < 0.05:
            return "16:9"
        if abs(ratio - 4 / 3) < 0.05:
            return "4:3"
        if abs(ratio - 21 / 9) < 0.1:
            return "21:9"
        return "16:9"
    ratio = height / width
    if abs(ratio - 16 / 9) < 0.05:
        return "9:16"
    return "9:16"


__all__ = [
    "MotionGraphicProposalCard",
    "ElementSummary",
    "BeatSummary",
    "build_plan_card",
    "build_fallback_card",
]
