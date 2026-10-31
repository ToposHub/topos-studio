"""Creative proposal and Motion Graphic plan contracts (MG-04).

MG-04 splits the old "one LLM call → final scene" path into three stages:

1. **Brief** (already in :mod:`brief`): the editable user intent.
2. **Creative proposal**: 1–3 low-cost textual ideas — narrative beat sheet,
   copy, palette, pacing, element sketch — produced by the LLM *without*
   compiling a full scene.  The user compares and picks one (or asks for a
   revision).
3. **Scene plan**: the chosen proposal is compiled into the stable, restricted
   scene command vocabulary (``TeachingAICommand``).  The plan is dry-run,
   hashed and previewed; it only becomes a scene after the user confirms.

This file defines the typed objects for stages 2 and 3.  The LLM is restricted
to emitting the JSON schema of :class:`CreativeProposal` /
:class:`MotionGraphicPlan`; it never receives the approval grant, the
CommandStack or any host write capability.

Design rules:

* both objects are immutable and hashable so they can be cached, diffed and
  referenced from :class:`~src.features.teaching_animation.ai.approval_scope.MotionGraphicApprovalScope`;
* every object links back to the brief revision it was generated against, so a
  brief edit invalidates older proposals automatically;
* the plan embeds the dry-run :class:`~src.features.teaching_animation.ai.commands.TeachingAIProposal`
  so the host can re-verify ``base_hash`` at apply time;
* fallback provenance (MG-05) is explicit: every proposal/plan carries a
  ``source`` that says whether it came from the LLM, an explicit user-chosen
  template, or a failed-generation fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import uuid
from typing import Any, Mapping, Sequence

from ..core.model import TeachingSceneDocument
from .commands import TeachingAICommand, TeachingAIProposal
from .error_codes import (
    MOTION_GRAPHIC_INVALID_SCENE_PLAN,
    MotionGraphicFailure,
)


# --------------------------------------------------------------------------- #
# Provenance (MG-05)
# --------------------------------------------------------------------------- #


class ProposalSource(str):
    """Where a proposal/plan came from.

    MG-05 requires that a model failure can never masquerade as a custom
    result.  ``source`` makes that visible on the card so the user always knows
    whether they are looking at a real model output, an explicit template, or a
    last-resort fallback.
    """


SOURCE_LLM = "llm"                              # real model output
SOURCE_TEMPLATE = "template"                     # user explicitly picked a built-in template
SOURCE_FALLBACK_PROVIDER_UNAVAILABLE = "fallback_provider_unavailable"
SOURCE_FALLBACK_INVALID_PLAN = "fallback_invalid_plan"
SOURCE_FALLBACK_OPTIONAL_TEMPLATE = "fallback_optional_template"

#: Sources that represent an *explicit* fallback (model failure).  These must
#: always be labelled on the card and can never be silently presented as
#: ``SOURCE_LLM``.
FALLBACK_SOURCES: frozenset[str] = frozenset({
    SOURCE_FALLBACK_PROVIDER_UNAVAILABLE,
    SOURCE_FALLBACK_INVALID_PLAN,
    SOURCE_FALLBACK_OPTIONAL_TEMPLATE,
})


def is_fallback_source(source: str) -> bool:
    return source in FALLBACK_SOURCES


# --------------------------------------------------------------------------- #
# Beat / style tokens
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NarrativeBeat:
    """One beat of the creative narrative / storyboard.

    Beats carry the creative intent (copy on screen, motion, transition) plus
    optional **structured** fields (``motion_preset_id``, ``transition``,
    ``render_cost``, ``asset_refs``) that let the planner map a beat directly
    to scene commands without parsing free text.  The free-text hints stay for
    human-readable notes; the structured fields are additive and default empty
    so existing proposals round-trip unchanged (AQ-01 acceptance: the user can
    see per-segment timing, motion, transition, assets and cost before
    confirmation).
    """

    beat_id: str
    label: str
    start_seconds: float
    duration_seconds: float
    on_screen_copy: str = ""
    motion_hint: str = ""
    layout_hint: str = ""
    notes: str = ""
    # AQ-01 structured fields (additive; empty = unspecified).
    motion_preset_id: str = ""        # e.g. "pop", "fade", "slide" (MotionPreset id)
    transition: str = ""              # in/out transition: cut|dissolve|wipe|slide
    render_cost: str = ""             # low|medium|high hint for preview gating
    asset_refs: tuple[str, ...] = ()  # asset ids this beat depends on

    def to_dict(self) -> dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "label": self.label,
            "start_seconds": self.start_seconds,
            "duration_seconds": self.duration_seconds,
            "on_screen_copy": self.on_screen_copy,
            "motion_hint": self.motion_hint,
            "layout_hint": self.layout_hint,
            "notes": self.notes,
            "motion_preset_id": self.motion_preset_id,
            "transition": self.transition,
            "render_cost": self.render_cost,
            "asset_refs": list(self.asset_refs),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NarrativeBeat":
        raw_refs = data.get("asset_refs", ())
        return cls(
            beat_id=str(data.get("beat_id", "")),
            label=str(data.get("label", "")),
            start_seconds=float(data.get("start_seconds", 0.0)),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            on_screen_copy=str(data.get("on_screen_copy", "")),
            motion_hint=str(data.get("motion_hint", "")),
            layout_hint=str(data.get("layout_hint", "")),
            notes=str(data.get("notes", "")),
            motion_preset_id=str(data.get("motion_preset_id", "")),
            transition=str(data.get("transition", "")),
            render_cost=str(data.get("render_cost", "")),
            asset_refs=tuple(raw_refs) if isinstance(raw_refs, (list, tuple)) else (),
        )


@dataclass(frozen=True)
class StyleTokens:
    """Colour / typography / motion tokens proposed for one creative idea.

    Kept loose (mappings) so the LLM has room to describe intent ("科技冷色",
    "标题用粗体"), while still being structured enough for the planner to map
    to concrete scene element styles later.
    """

    palette: Mapping[str, str] = field(default_factory=dict)
    typography: Mapping[str, Any] = field(default_factory=dict)
    motion: Mapping[str, Any] = field(default_factory=dict)
    references: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "palette": dict(self.palette),
            "typography": dict(self.typography),
            "motion": dict(self.motion),
            "references": list(self.references),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StyleTokens":
        refs = data.get("references", [])
        return cls(
            palette=_copy_json(dict(data.get("palette", {}))),
            typography=_copy_json(dict(data.get("typography", {}))),
            motion=_copy_json(dict(data.get("motion", {}))),
            references=tuple(str(r) for r in refs) if isinstance(refs, (list, tuple)) else (),
        )


# --------------------------------------------------------------------------- #
# Creative proposal
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CreativeProposal:
    """A single, comparable creative idea (MG-04 stage 2).

    The proposal is *cheap*: it does not contain scene commands, only the
    narrative intent.  Two or three of these are presented side by side so the
    user can pick one ("方案 A/B/C") or ask for a revision before any scene
    work happens.

    Attributes:
        proposal_id: Stable identifier.
        brief_id / brief_revision / brief_hash: The brief this proposal was
            generated against.  Editing the brief invalidates the proposal.
        title: Short label for the card ("方案 A · 科技冷色")。
        summary: One-paragraph description of the idea.
        beats: Ordered narrative beats (the "beat sheet").
        style: Style tokens (palette, typography, motion).
        element_sketch: Loose description of planned elements (count + roles).
        estimated_cost_units: Cost estimate for paid assets in this idea.
        estimated_risks: Risk flags ("uses paid image", "low-contrast palette").
        source: Provenance — :data:`SOURCE_LLM` for real model output, one of
            the fallback constants otherwise.
        provider / model_id: Provenance for the audit event (MG-23).
        rationale: Short LLM-provided explanation, useful when comparing.
    """

    proposal_id: str
    brief_id: str
    brief_revision: int
    brief_hash: str
    title: str
    summary: str
    beats: tuple[NarrativeBeat, ...] = ()
    style: StyleTokens = field(default_factory=StyleTokens)
    element_sketch: Mapping[str, Any] = field(default_factory=dict)
    estimated_cost_units: float = 0.0
    estimated_risks: tuple[str, ...] = ()
    source: str = SOURCE_LLM
    provider: str | None = None
    model_id: str | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.proposal_id:
            raise ValueError("CreativeProposal.proposal_id is required")
        if not self.brief_id:
            raise ValueError("CreativeProposal.brief_id is required")
        if self.brief_revision < 1:
            raise ValueError("CreativeProposal.brief_revision must be >= 1")
        if not self.brief_hash:
            raise ValueError("CreativeProposal.brief_hash is required")
        if not self.title.strip():
            raise ValueError("CreativeProposal.title is required")
        if self.estimated_cost_units < 0:
            raise ValueError("CreativeProposal.estimated_cost_units cannot be negative")

    @property
    def is_fallback(self) -> bool:
        return is_fallback_source(self.source)

    @property
    def duration_seconds(self) -> float:
        return sum(beat.duration_seconds for beat in self.beats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "brief_hash": self.brief_hash,
            "title": self.title,
            "summary": self.summary,
            "beats": [beat.to_dict() for beat in self.beats],
            "style": self.style.to_dict(),
            "element_sketch": _copy_json(dict(self.element_sketch)),
            "estimated_cost_units": self.estimated_cost_units,
            "estimated_risks": list(self.estimated_risks),
            "source": self.source,
            "provider": self.provider,
            "model_id": self.model_id,
            "rationale": self.rationale,
        }

    def proposal_hash(self) -> str:
        payload = {
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "brief_hash": self.brief_hash,
            "title": self.title,
            "summary": self.summary,
            "beats": [beat.to_dict() for beat in self.beats],
            "style": self.style.to_dict(),
            "element_sketch": _copy_json(dict(self.element_sketch)),
            "estimated_risks": list(self.estimated_risks),
            "source": self.source,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CreativeProposal":
        beats_data = data.get("beats", [])
        beats = tuple(
            NarrativeBeat.from_dict(item)
            for item in beats_data
            if isinstance(item, Mapping)
        )
        style_data = data.get("style", {})
        return cls(
            proposal_id=str(data.get("proposal_id", "")),
            brief_id=str(data.get("brief_id", "")),
            brief_revision=int(data.get("brief_revision", 1)),
            brief_hash=str(data.get("brief_hash", "")),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            beats=beats,
            style=StyleTokens.from_dict(style_data) if isinstance(style_data, Mapping) else StyleTokens(),
            element_sketch=_copy_json(dict(data.get("element_sketch", {}))),
            estimated_cost_units=float(data.get("estimated_cost_units", 0.0) or 0.0),
            estimated_risks=tuple(str(r) for r in data.get("estimated_risks", [])),
            source=str(data.get("source", SOURCE_LLM)),
            provider=data.get("provider"),
            model_id=data.get("model_id"),
            rationale=str(data.get("rationale", "")),
        )


# --------------------------------------------------------------------------- #
# Scene plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicPlan:
    """The compiled, dry-run, hash-bound execution plan (MG-04 stage 3).

    Built from a chosen :class:`CreativeProposal` by compiling it to the
    restricted scene command vocabulary.  The plan is *immutable*: any edit
    produces a new plan with a new ``plan_id`` and ``plan_hash``.

    Attributes:
        plan_id: Stable identifier.
        proposal_ref: Id of the proposal this plan was compiled from.
        brief_id / brief_revision / brief_hash: Inherited binding to the brief.
        commands: The restricted, dry-run-safe command tuple.
        proposal_dry_run: The :class:`TeachingAIProposal` produced by
            ``TeachingAICommandRegistry.dry_run``.  Carries ``base_hash``,
            ``proposed_scene``, ``diff`` and ``diff_hash``.
        scene_hash: sha256 of the proposed scene (from the dry run).
        plan_hash: sha256 of the full plan payload (used by approval scope).
        target_track_id / target_insert_frame: Where the clip will land if
            confirmed.  Bound into the approval scope.
        estimated_cost_units / estimated_risks: Carry-over from the proposal,
            updated with render cost.
        source: Provenance (MG-05).
        provider / model_id: Audit provenance.
        quality_checks: Results of the design validator run on the proposed
            scene (MG-10).  Errors here block preview/commit.
    """

    plan_id: str
    proposal_ref: str
    brief_id: str
    brief_revision: int
    brief_hash: str
    commands: tuple[TeachingAICommand, ...]
    proposal_dry_run: TeachingAIProposal
    scene_hash: str
    plan_hash: str
    target_track_id: str | None = None
    target_insert_frame: int | None = None
    estimated_cost_units: float = 0.0
    estimated_risks: tuple[str, ...] = ()
    source: str = SOURCE_LLM
    provider: str | None = None
    model_id: str | None = None
    quality_checks: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("MotionGraphicPlan.plan_id is required")
        if not self.proposal_ref:
            raise ValueError("MotionGraphicPlan.proposal_ref is required")
        if self.brief_revision < 1:
            raise ValueError("MotionGraphicPlan.brief_revision must be >= 1")
        if not self.brief_hash:
            raise ValueError("MotionGraphicPlan.brief_hash is required")
        if not self.commands:
            raise ValueError("MotionGraphicPlan.commands cannot be empty")
        if not self.scene_hash:
            raise ValueError("MotionGraphicPlan.scene_hash is required")
        if not self.plan_hash:
            raise ValueError("MotionGraphicPlan.plan_hash is required")
        if self.estimated_cost_units < 0:
            raise ValueError("MotionGraphicPlan.estimated_cost_units cannot be negative")

    @property
    def is_fallback(self) -> bool:
        return is_fallback_source(self.source)

    @property
    def proposed_scene(self) -> TeachingSceneDocument:
        return self.proposal_dry_run.proposed_scene

    @property
    def diff_hash(self) -> str:
        return self.proposal_dry_run.diff_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "proposal_ref": self.proposal_ref,
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "brief_hash": self.brief_hash,
            "commands": [cmd.to_dict() for cmd in self.commands],
            "scene_hash": self.scene_hash,
            "plan_hash": self.plan_hash,
            "diff_hash": self.diff_hash,
            "target_track_id": self.target_track_id,
            "target_insert_frame": self.target_insert_frame,
            "estimated_cost_units": self.estimated_cost_units,
            "estimated_risks": list(self.estimated_risks),
            "source": self.source,
            "provider": self.provider,
            "model_id": self.model_id,
            "quality_checks": _copy_json(dict(self.quality_checks)),
        }


def build_plan(
    *,
    proposal: CreativeProposal,
    base_scene: TeachingSceneDocument,
    commands: Sequence[TeachingAICommand],
    registry: Any,
    target_track_id: str | None = None,
    target_insert_frame: int | None = None,
    source: str | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    quality_checks: Mapping[str, Any] | None = None,
) -> MotionGraphicPlan:
    """Compile a chosen proposal into a typed, hash-bound plan.

    ``registry`` is a :class:`~src.features.teaching_animation.ai.commands.TeachingAICommandRegistry`
    (passed in rather than imported to avoid a hard dependency at module load
    time).  The dry run runs the commands against ``base_scene``; the resulting
    :class:`TeachingAIProposal` is embedded in the plan so apply-time
    ``base_hash`` re-verification works exactly as before.

    Raises :class:`MotionGraphicFailure` (``MOTION_GRAPHIC_INVALID_SCENE_PLAN``)
    when the dry run fails or produces an empty diff — the host turns that
    into a stable error card instead of a silent fallback.
    """

    command_tuple = tuple(commands)
    try:
        dry_run = registry.dry_run(base_scene, command_tuple)
    except Exception as exc:  # noqa: BLE001 - surface as stable failure
        raise MotionGraphicFailure(
            MOTION_GRAPHIC_INVALID_SCENE_PLAN,
            f"场景命令编译失败：{exc}",
            details={"proposal_id": proposal.proposal_id},
        ) from exc
    if dry_run.is_empty:
        raise MotionGraphicFailure(
            MOTION_GRAPHIC_INVALID_SCENE_PLAN,
            "场景命令没有产生任何变化。",
            details={"proposal_id": proposal.proposal_id},
        )

    scene = dry_run.proposed_scene
    scene_hash = _hash_scene(scene)
    plan_source = source or proposal.source
    plan_payload = {
        "plan_id": "",  # filled after id is generated; hash excludes it
        "proposal_ref": proposal.proposal_id,
        "brief_id": proposal.brief_id,
        "brief_revision": proposal.brief_revision,
        "brief_hash": proposal.brief_hash,
        "commands": [cmd.to_dict() for cmd in command_tuple],
        "scene_hash": scene_hash,
        "diff_hash": dry_run.diff_hash,
        "target_track_id": target_track_id,
        "target_insert_frame": target_insert_frame,
        "source": plan_source,
    }
    plan_hash = _hash_payload(plan_payload)
    return MotionGraphicPlan(
        plan_id=f"mg-plan-{uuid.uuid4().hex}",
        proposal_ref=proposal.proposal_id,
        brief_id=proposal.brief_id,
        brief_revision=proposal.brief_revision,
        brief_hash=proposal.brief_hash,
        commands=command_tuple,
        proposal_dry_run=dry_run,
        scene_hash=scene_hash,
        plan_hash=plan_hash,
        target_track_id=target_track_id,
        target_insert_frame=target_insert_frame,
        estimated_cost_units=proposal.estimated_cost_units,
        estimated_risks=proposal.estimated_risks,
        source=plan_source,
        provider=provider or proposal.provider,
        model_id=model_id or proposal.model_id,
        quality_checks=_copy_json(dict(quality_checks or {})),
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _copy_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in value]
    return value


def _hash_scene(scene: TeachingSceneDocument) -> str:
    payload = json.dumps(scene.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hash_payload(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "CreativeProposal",
    "MotionGraphicPlan",
    "NarrativeBeat",
    "StyleTokens",
    "ProposalSource",
    "SOURCE_LLM",
    "SOURCE_TEMPLATE",
    "SOURCE_FALLBACK_PROVIDER_UNAVAILABLE",
    "SOURCE_FALLBACK_INVALID_PLAN",
    "SOURCE_FALLBACK_OPTIONAL_TEMPLATE",
    "FALLBACK_SOURCES",
    "is_fallback_source",
    "build_plan",
]
