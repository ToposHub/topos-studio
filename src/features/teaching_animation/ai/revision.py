# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Versioned revision lineage for Motion Graphic proposals (MG-08).

MG-08 turns "继续修改直到满意" from a state flag into a real version graph.
Each round of feedback produces a new :class:`MotionGraphicRevision` that:

* records the structured :class:`FeedbackPatch` (what the user asked to change
  — copy / palette / pacing / duration / layout / audio / brand);
* links to its parent revision so the lineage is reconstructable;
* stores the resulting :class:`CreativeProposal` and, optionally, the compiled
  :class:`MotionGraphicPlan`;
* tracks the user's decision (pending / accepted / rejected / superseded) and,
  when rejected, the reason category.

Only an *accepted* revision can be confirmed for execution, and confirmations
are bound to a specific revision through the approval scope's ``plan_hash`` /
``scene_hash`` — so an old revision can never be quietly applied after the
user has moved on.

Design rules:

* revisions are append-only and immutable; "editing" a revision creates a new
  one with ``parent_id`` pointing back;
* the lineage is owned by :class:`MotionGraphicRevisionLog`, a small in-memory
  store with a typed API the Assistant UI will bind to;
* no raw prompts or secrets are stored on the patch — only the structured
  field keys and a short, sanitized ``note``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import uuid
from typing import Any, Mapping

from .error_codes import MOTION_GRAPHIC_REVISION_CONFLICT, MotionGraphicFailure
from .proposal import CreativeProposal, MotionGraphicPlan


# --------------------------------------------------------------------------- #
# Feedback categories
# --------------------------------------------------------------------------- #
#
# Closed vocabulary of "what aspect of the proposal is the user commenting
# on".  Keeping it closed lets the UI route the patch to the right planner
# adapter ("only change copy" vs "change the whole palette") and lets us
# report coverage ("user has now commented on copy, pacing and palette").

FEEDBACK_COPY = "copy"                 # wording / on-screen text / CTA
FEEDBACK_PALETTE = "palette"           # colours / mood
FEEDBACK_PACING = "pacing"             # rhythm / beat durations
FEEDBACK_DURATION = "duration"         # total length
FEEDBACK_LAYOUT = "layout"             # element positions / composition
FEEDBACK_AUDIO = "audio"               # narration / music / sfx
FEEDBACK_BRAND = "brand"               # brand tokens / logo policy
FEEDBACK_PLATFORM = "platform"         # aspect / safe area / delivery profile
FEEDBACK_COST = "cost"                 # budget / paid asset scope
FEEDBACK_QUALITY = "quality"           # readability / contrast / render risk
FEEDBACK_OTHER = "other"

SUPPORTED_FEEDBACK_CATEGORIES: frozenset[str] = frozenset({
    FEEDBACK_COPY, FEEDBACK_PALETTE, FEEDBACK_PACING, FEEDBACK_DURATION,
    FEEDBACK_LAYOUT, FEEDBACK_AUDIO, FEEDBACK_BRAND, FEEDBACK_PLATFORM,
    FEEDBACK_COST, FEEDBACK_QUALITY, FEEDBACK_OTHER,
})


# --------------------------------------------------------------------------- #
# FeedbackPatch
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FeedbackPatch:
    """A structured user comment on a proposal.

    Attributes:
        patch_id: Stable identifier.
        categories: Subset of :data:`SUPPORTED_FEEDBACK_CATEGORIES` touched by
            this feedback.  Drives planner routing.
        requested_changes: Mapping of "what to change" — kept loose so each
            category can evolve without breaking the schema (e.g.
            ``{"copy.title": "你的 AI 剪辑伙伴"}``).
        note: Short user note, already sanitized (no prompt / secret).
        created_at: Wall clock for ordering.
    """

    patch_id: str
    categories: tuple[str, ...]
    requested_changes: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.patch_id:
            raise ValueError("FeedbackPatch.patch_id is required")
        if not self.categories:
            raise ValueError("FeedbackPatch.categories cannot be empty")
        unknown = set(self.categories) - SUPPORTED_FEEDBACK_CATEGORIES
        if unknown:
            raise ValueError(
                f"Unsupported feedback categories: {sorted(unknown)}; "
                f"expected subset of {sorted(SUPPORTED_FEEDBACK_CATEGORIES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "categories": list(self.categories),
            "requested_changes": _copy_json(dict(self.requested_changes)),
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FeedbackPatch":
        cats = data.get("categories", [])
        return cls(
            patch_id=str(data.get("patch_id", "")),
            categories=tuple(str(c) for c in cats) if isinstance(cats, (list, tuple)) else (),
            requested_changes=_copy_json(dict(data.get("requested_changes", {}))),
            note=str(data.get("note", "")),
            created_at=float(data.get("created_at", 0.0) or 0.0),
        )


# --------------------------------------------------------------------------- #
# Revision status
# --------------------------------------------------------------------------- #


class RevisionStatus(str):
    """Lifecycle of one revision."""


STATUS_PENDING = "pending"             # generated, waiting for user review
STATUS_ACCEPTED = "accepted"           # user picked this revision
STATUS_REJECTED = "rejected"           # user explicitly rejected
STATUS_SUPERSEDED = "superseded"       # replaced by a newer revision
STATUS_FAILED = "failed"                # generation failed for this revision

ACTIVE_STATUSES: frozenset[str] = frozenset({STATUS_PENDING, STATUS_ACCEPTED})
TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_REJECTED, STATUS_SUPERSEDED, STATUS_FAILED})


# --------------------------------------------------------------------------- #
# Revision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicRevision:
    """One versioned step in the "modify until satisfied" loop (MG-08).

    A revision is created by:

    * the initial proposal (``parent_id=None``);
    * a feedback patch applied to a parent revision (``parent_id`` set, patch
      carried along).

    It carries the proposal and, if the user has moved past the "compare
    ideas" stage, the compiled plan.  Only one revision per lineage may be
    ``accepted`` at a time.
    """

    revision_id: str
    lineage_id: str
    parent_id: str | None
    proposal: CreativeProposal
    feedback: FeedbackPatch | None = None
    plan: MotionGraphicPlan | None = None
    status: str = STATUS_PENDING
    reject_reason: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.revision_id:
            raise ValueError("MotionGraphicRevision.revision_id is required")
        if not self.lineage_id:
            raise ValueError("MotionGraphicRevision.lineage_id is required")
        if self.status not in {
            STATUS_PENDING, STATUS_ACCEPTED, STATUS_REJECTED,
            STATUS_SUPERSEDED, STATUS_FAILED,
        }:
            raise ValueError(f"Unknown revision status: {self.status!r}")

    @property
    def is_accepted(self) -> bool:
        return self.status == STATUS_ACCEPTED

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def has_plan(self) -> bool:
        return self.plan is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "lineage_id": self.lineage_id,
            "parent_id": self.parent_id,
            "proposal": self.proposal.to_dict(),
            "feedback": self.feedback.to_dict() if self.feedback else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "created_at": self.created_at,
        }


# --------------------------------------------------------------------------- #
# Revision log
# --------------------------------------------------------------------------- #


class MotionGraphicRevisionLog:
    """Append-only, in-memory store of revisions for one Motion Graphic brief.

    The log enforces the MG-08 invariants:

    * only one accepted revision at a time — accepting a new one supersedes
      the previous accepted revision;
    * only the latest *pending* revision of a lineage can receive a feedback
      patch (prevents forking the lineage silently);
    * terminal revisions cannot transition back to pending.

    All mutating methods take the internal ``RLock``, so the "parent must be
    the lineage latest" check and the store write in :meth:`add_feedback` are
    one atomic section even when a generation callback lands on a worker
    thread while the UI thread sends feedback (AUD-27012).  Reads are not
    locked: revisions are immutable and dict reads are atomic under CPython.
    """

    def __init__(self) -> None:
        self._revisions: dict[str, MotionGraphicRevision] = {}
        self._lineage_latest: dict[str, str] = {}
        self._lock = threading.RLock()

    # ---------------- queries ---------------- #

    def get(self, revision_id: str) -> MotionGraphicRevision | None:
        return self._revisions.get(revision_id)

    def latest_for(self, lineage_id: str) -> MotionGraphicRevision | None:
        revision_id = self._lineage_latest.get(lineage_id)
        if revision_id is None:
            return None
        return self._revisions.get(revision_id)

    def lineage(self, lineage_id: str) -> tuple[MotionGraphicRevision, ...]:
        """Return the lineage ordered from oldest to newest."""
        ordered: list[MotionGraphicRevision] = []
        latest = self.latest_for(lineage_id)
        seen: set[str] = set()
        while latest is not None and latest.revision_id not in seen:
            ordered.append(latest)
            seen.add(latest.revision_id)
            if latest.parent_id is None:
                break
            latest = self._revisions.get(latest.parent_id)
        ordered.reverse()
        return tuple(ordered)

    def accepted_revision(self, lineage_id: str) -> MotionGraphicRevision | None:
        for revision in self.lineage(lineage_id):
            if revision.status == STATUS_ACCEPTED:
                return revision
        return None

    def all_revisions(self) -> tuple[MotionGraphicRevision, ...]:
        return tuple(self._revisions.values())

    # ---------------- mutations ---------------- #

    def add_initial(self, proposal: CreativeProposal, *, created_at: float = 0.0) -> MotionGraphicRevision:
        """Add the first revision of a new lineage."""
        with self._lock:
            revision = MotionGraphicRevision(
                revision_id=f"mg-rev-{uuid.uuid4().hex}",
                lineage_id=f"mg-lineage-{uuid.uuid4().hex}",
                parent_id=None,
                proposal=proposal,
                status=STATUS_PENDING,
                created_at=created_at,
            )
            self._store(revision)
            return revision

    def add_feedback(
        self,
        parent_id: str,
        new_proposal: CreativeProposal,
        feedback: FeedbackPatch,
        *,
        created_at: float = 0.0,
    ) -> MotionGraphicRevision:
        """Append a feedback-driven child revision.

        ``parent_id`` must be the *latest* revision of its lineage.  Patching
        an older revision would silently fork the history; we refuse that with
        a stable error so the UI can tell the user "请基于最新版本继续修改".

        The latest check and the store write run under the log lock, so two
        concurrent :meth:`add_feedback` calls (e.g. worker callback racing the
        UI) cannot both pass the guard and fork the lineage (AUD-27012).
        """

        with self._lock:
            parent = self._revisions.get(parent_id)
            if parent is None:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "找不到要修改的版本。",
                    details={"parent_id": parent_id},
                )
            latest_id = self._lineage_latest.get(parent.lineage_id)
            if latest_id != parent_id:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "只能基于最新版本继续修改；旧版本已被取代。",
                    details={
                        "parent_id": parent_id,
                        "latest_id": latest_id,
                        "lineage_id": parent.lineage_id,
                    },
                )
            # Same-brief guard: the new proposal must come from the same brief
            # revision as its parent, otherwise the lineage is mixing briefs.
            if (
                new_proposal.brief_id != parent.proposal.brief_id
                or new_proposal.brief_revision != parent.proposal.brief_revision
            ):
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "新方案与父版本的需求版本不一致；请基于当前需求重新生成。",
                    details={
                        "parent_brief_revision": parent.proposal.brief_revision,
                        "new_brief_revision": new_proposal.brief_revision,
                    },
                )
            # Supersede the parent if it was still pending: a new revision means
            # the user moved on.
            if parent.status == STATUS_PENDING:
                self._replace(parent.revision_id, STATUS_SUPERSEDED)

            revision = MotionGraphicRevision(
                revision_id=f"mg-rev-{uuid.uuid4().hex}",
                lineage_id=parent.lineage_id,
                parent_id=parent.revision_id,
                proposal=new_proposal,
                feedback=feedback,
                status=STATUS_PENDING,
                created_at=created_at,
            )
            self._store(revision)
            return revision

    def attach_plan(self, revision_id: str, plan: MotionGraphicPlan) -> MotionGraphicRevision:
        """Attach a compiled plan to a pending revision."""
        with self._lock:
            revision = self._revisions.get(revision_id)
            if revision is None:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "找不到目标版本。",
                    details={"revision_id": revision_id},
                )
            if revision.status not in ACTIVE_STATUSES:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "不能为已结束的版本附加计划。",
                    details={"revision_id": revision_id, "status": revision.status},
                )
            updated = MotionGraphicRevision(
                revision_id=revision.revision_id,
                lineage_id=revision.lineage_id,
                parent_id=revision.parent_id,
                proposal=revision.proposal,
                feedback=revision.feedback,
                plan=plan,
                status=revision.status,
                reject_reason=revision.reject_reason,
                created_at=revision.created_at,
            )
            self._revisions[revision_id] = updated
            return updated

    def accept(self, revision_id: str) -> MotionGraphicRevision:
        """Mark a revision as the user's pick, superseding any previous accept.

        AUD-27010: REJECTED and FAILED revisions are terminal *outcomes* — a
        rejected or failed generation must never become the accepted pick.
        SUPERSEDED is deliberately still acceptable: re-accepting an older
        revision is the product's explicit "roll back to this version" path
        (locked by test_revision_log_accept_replaces_previous_accept), and it
        does not violate the log invariant because the invariant covers
        transitions back to *pending*, not a later accept.
        """
        with self._lock:
            revision = self._revisions.get(revision_id)
            if revision is None:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "找不到要接受的版本。",
                    details={"revision_id": revision_id},
                )
            if revision.status in (STATUS_REJECTED, STATUS_FAILED):
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "已拒绝或生成失败的版本不能再接受。",
                    details={"revision_id": revision_id, "status": revision.status},
                )
            # Supersede any previously accepted revision in the same lineage.
            previous = self.accepted_revision(revision.lineage_id)
            if previous is not None and previous.revision_id != revision_id:
                self._replace(previous.revision_id, STATUS_SUPERSEDED)
            updated = MotionGraphicRevision(
                revision_id=revision.revision_id,
                lineage_id=revision.lineage_id,
                parent_id=revision.parent_id,
                proposal=revision.proposal,
                feedback=revision.feedback,
                plan=revision.plan,
                status=STATUS_ACCEPTED,
                reject_reason="",
                created_at=revision.created_at,
            )
            self._revisions[revision_id] = updated
            return updated

    def reject(self, revision_id: str, *, reason: str = "") -> MotionGraphicRevision:
        with self._lock:
            revision = self._revisions.get(revision_id)
            if revision is None:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "找不到要拒绝的版本。",
                    details={"revision_id": revision_id},
                )
            if revision.status == STATUS_ACCEPTED:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "已接受的版本不能直接拒绝；请改用新的反馈生成新版本。",
                    details={"revision_id": revision_id},
                )
            updated = MotionGraphicRevision(
                revision_id=revision.revision_id,
                lineage_id=revision.lineage_id,
                parent_id=revision.parent_id,
                proposal=revision.proposal,
                feedback=revision.feedback,
                plan=revision.plan,
                status=STATUS_REJECTED,
                reject_reason=reason,
                created_at=revision.created_at,
            )
            self._revisions[revision_id] = updated
            return updated

    def mark_failed(self, revision_id: str, *, reason: str = "") -> MotionGraphicRevision:
        with self._lock:
            revision = self._revisions.get(revision_id)
            if revision is None:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "找不到目标版本。",
                    details={"revision_id": revision_id},
                )
            updated = MotionGraphicRevision(
                revision_id=revision.revision_id,
                lineage_id=revision.lineage_id,
                parent_id=revision.parent_id,
                proposal=revision.proposal,
                feedback=revision.feedback,
                plan=revision.plan,
                status=STATUS_FAILED,
                reject_reason=reason,
                created_at=revision.created_at,
            )
            self._revisions[revision_id] = updated
            return updated

    # ---------------- internals ---------------- #

    def _store(self, revision: MotionGraphicRevision) -> None:
        self._revisions[revision.revision_id] = revision
        self._lineage_latest[revision.lineage_id] = revision.revision_id

    def _replace(self, revision_id: str, new_status: str) -> None:
        old = self._revisions.get(revision_id)
        if old is None:
            return
        self._revisions[revision_id] = MotionGraphicRevision(
            revision_id=old.revision_id,
            lineage_id=old.lineage_id,
            parent_id=old.parent_id,
            proposal=old.proposal,
            feedback=old.feedback,
            plan=old.plan,
            status=new_status,
            reject_reason=old.reject_reason,
            created_at=old.created_at,
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


__all__ = [
    "FeedbackPatch",
    "MotionGraphicRevision",
    "MotionGraphicRevisionLog",
    "RevisionStatus",
    "SUPPORTED_FEEDBACK_CATEGORIES",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "STATUS_PENDING",
    "STATUS_ACCEPTED",
    "STATUS_REJECTED",
    "STATUS_SUPERSEDED",
    "STATUS_FAILED",
    "FEEDBACK_COPY",
    "FEEDBACK_PALETTE",
    "FEEDBACK_PACING",
    "FEEDBACK_DURATION",
    "FEEDBACK_LAYOUT",
    "FEEDBACK_AUDIO",
    "FEEDBACK_BRAND",
    "FEEDBACK_PLATFORM",
    "FEEDBACK_COST",
    "FEEDBACK_QUALITY",
    "FEEDBACK_OTHER",
]
