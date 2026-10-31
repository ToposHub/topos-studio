"""Unified Agent V2 domain models (plan §4.1 / §4.2 / §A0).

This module is the **single** typed vocabulary for the unified Agent Runtime
that the plan introduces so ordinary chat, Skill execution and Motion Graphic
generation all share one observable, cancellable, recoverable run model.

It deliberately coexists with the existing modules — ``assistant_events``
(the UI-facing TAEP state machine), ``agent_loop`` (the bounded Observe→Verify
loop collaborators) and ``ai_harness.invocation_models`` (the workflow run
record).  A0 only freezes the contract; nothing is wired to a live executor
yet, so importing this module never changes behaviour (see
:mod:`agent_feature_flags`).

Hard rules baked into the types (plan §4.2 / §9):

- State transitions are validated.  An illegal flip (e.g. ``SUCCEEDED →
  EXECUTING``) raises ``ValueError``; nothing silently degrades to an
  ``IDLE``-like state.
- Every object has versioned ``to_dict`` / ``from_dict`` so a checkpoint
  written today can be migrated, not discarded.
- Persisted payloads are redacted via ``sanitize_for_storage``: no secret,
  absolute path or unbounded prompt body is stored.

The schema version lives at the bottom of the module and must be bumped when
any persisted field changes shape.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.features.ai_harness.invocation_models import sanitize_for_storage


# ---------------------------------------------------------------------- #
# Schema version
# ---------------------------------------------------------------------- #

#: Bumped whenever a persisted field changes shape.  ``from_dict`` of an older
#: version must migrate, never drop data silently.
AGENT_MODELS_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------- #
# Unified run state machine (plan §4.2)
# ---------------------------------------------------------------------- #


class AgentRunState:
    """The unified, observable state shared by chat / Skill / Motion Graphic.

    Drawn verbatim from plan §4.2::

        CREATED
          ↓
        GATHERING_CONTEXT
          ↓
        PLANNING
          ├─→ WAITING_USER_INPUT
          ├─→ WAITING_APPROVAL
          └─→ EXECUTING
                  ↓
               VERIFYING
                  ├─→ SUCCEEDED
                  ├─→ PARTIAL_SUCCESS
                  ├─→ FAILED
                  ├─→ CANCELLED
                  └─→ PAUSED_FOR_RESUME

    Hard rules (plan §4.2):

    - ``transition()`` is the only sanctioned way to move between states; an
      illegal flip raises ``ValueError`` instead of silently landing in a
      permissive default.
    - Terminal states have no outgoing edges except an explicit ``retry``
      (which always starts a brand-new run, handled by the orchestrator, not
      by mutating this run).
    - Every non-terminal state may transition to ``CANCELLED`` (the orchestrator
      applies this when a cancellation is confirmed).
    """

    CREATED = "created"
    GATHERING_CONTEXT = "gathering_context"
    PLANNING = "planning"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_APPROVAL = "waiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED_FOR_RESUME = "paused_for_resume"

    TERMINAL_STATES: frozenset[str] = frozenset(
        {SUCCEEDED, PARTIAL_SUCCESS, FAILED, CANCELLED}
    )
    #: States that block automatic continuation and need either user input or
    #: an explicit resume/cancel decision (plan §3 thinking states).
    USER_BLOCKED_STATES: frozenset[str] = frozenset(
        {WAITING_USER_INPUT, WAITING_APPROVAL, PAUSED_FOR_RESUME}
    )

    # Legal forward edges.  CANCELLED is reachable from every non-terminal state
    # (resolved dynamically in :meth:`can_transition`).
    _TRANSITIONS: dict[str, frozenset[str]] = {
        CREATED: frozenset({GATHERING_CONTEXT, FAILED, CANCELLED}),
        GATHERING_CONTEXT: frozenset(
            {PLANNING, WAITING_USER_INPUT, FAILED, CANCELLED}
        ),
        PLANNING: frozenset(
            {WAITING_USER_INPUT, WAITING_APPROVAL, EXECUTING, FAILED, CANCELLED}
        ),
        WAITING_USER_INPUT: frozenset({PLANNING, FAILED, CANCELLED}),
        WAITING_APPROVAL: frozenset(
            {EXECUTING, PLANNING, FAILED, CANCELLED}
        ),
        EXECUTING: frozenset(
            {VERIFYING, WAITING_APPROVAL, FAILED, CANCELLED}
        ),
        VERIFYING: frozenset(
            {SUCCEEDED, PARTIAL_SUCCESS, FAILED, CANCELLED, PAUSED_FOR_RESUME}
        ),
        PAUSED_FOR_RESUME: frozenset({GATHERING_CONTEXT, CANCELLED}),
        SUCCEEDED: frozenset(),
        PARTIAL_SUCCESS: frozenset(),
        FAILED: frozenset(),
        CANCELLED: frozenset(),
    }

    @staticmethod
    def is_terminal(state: str) -> bool:
        return state in AgentRunState.TERMINAL_STATES

    @staticmethod
    def is_user_blocked(state: str) -> bool:
        return state in AgentRunState.USER_BLOCKED_STATES

    @staticmethod
    def can_transition(from_state: str, to_state: str) -> bool:
        allowed = AgentRunState._TRANSITIONS.get(from_state, frozenset())
        if to_state in allowed:
            return True
        # Every non-terminal state can be cancelled.
        if (
            to_state == AgentRunState.CANCELLED
            and from_state not in AgentRunState.TERMINAL_STATES
        ):
            return True
        return False

    @staticmethod
    def transition(from_state: str, to_state: str) -> str:
        """Return *to_state* if the move is legal, else raise ``ValueError``."""

        if not AgentRunState.can_transition(from_state, to_state):
            raise ValueError(
                f"illegal agent run state transition: {from_state!r} -> {to_state!r}"
            )
        return to_state

    @staticmethod
    def normalize(state: object) -> str:
        """Coerce an arbitrary value to a known state id, else ``FAILED``.

        Unknown / stale state ids must never silently become a permissive
        ``CREATED``-like state (plan §A0 step 4); they fall back to a safe
        terminal so the run is flagged for inspection.
        """

        candidate = str(state or "").strip().lower()
        if candidate in AgentRunState._TRANSITIONS:
            return candidate
        return AgentRunState.FAILED


# ---------------------------------------------------------------------- #
# AgentTaskKind / AgentTask (plan §4.1)
# ---------------------------------------------------------------------- #

#: Stable kind values so routing and recovery never guess the response
#: protocol from raw text (plan §2.7 / §A0 step 8).
class AgentTaskKind:
    """What a task produces, used by the router and the recovery layer."""

    CHAT = "chat"
    AGENT = "agent"
    MOTION_GRAPHIC = "motion_graphic"
    IMAGE = "image"
    VIDEO = "video"
    MUSIC = "music"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    VISION_UNDERSTANDING = "vision_understanding"
    EMBEDDING = "embedding"
    ANALYSIS = "analysis"
    EXPORT = "export"
    UNKNOWN = "unknown"

    KNOWN: frozenset[str] = frozenset(
        {
            CHAT, AGENT, MOTION_GRAPHIC, IMAGE, VIDEO, MUSIC,
            SPEECH_TO_TEXT, TEXT_TO_SPEECH, VISION_UNDERSTANDING,
            EMBEDDING, ANALYSIS, EXPORT,
        }
    )


@dataclass(frozen=True)
class AgentTask:
    """A user's real goal: what they want accomplished (plan §4.1).

    A task can be attempted by several runs (retry / resume); the task itself
    is stable while runs come and go.  It carries no credential or absolute
    path.
    """

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str = ""
    title: str = ""
    user_goal: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    kind: str = AgentTaskKind.CHAT
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id cannot be empty")
        kind = str(self.kind or "").strip().lower()
        if kind not in AgentTaskKind.KNOWN:
            # Unknown kind stays explicit instead of degrading to chat so the
            # router fails closed with a visible reason (plan §2.7).
            kind = AgentTaskKind.UNKNOWN
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "title", self.title.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "title": sanitize_for_storage(self.title),
            "user_goal": sanitize_for_storage(self.user_goal),
            "acceptance_criteria": [
                sanitize_for_storage(item) for item in self.acceptance_criteria
            ],
            "kind": self.kind,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AgentTask":
        return AgentTask(
            task_id=str(d.get("task_id", "")) or uuid.uuid4().hex,
            session_id=str(d.get("session_id", "")),
            title=str(d.get("title", "")),
            user_goal=str(d.get("user_goal", "")),
            acceptance_criteria=tuple(d.get("acceptance_criteria", [])),
            kind=str(d.get("kind", AgentTaskKind.CHAT)),
            created_at=float(d.get("created_at", 0.0)),
        )


# ---------------------------------------------------------------------- #
# ContextUsage (plan §4.1 / §A4)
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContextUsage:
    """Unified token budget for the top bar (plan §A4 §4.1).

    Keeps the *current window* tokens separate from the *session total* so the
    UI never confuses "what is about to be sent" with "what was spent so far".
    Every actual value is paired with an ``is_estimated`` flag so a missing
    provider usage number is never disguised as ground truth.
    """

    window_prompt: int | None = None
    window_prompt_is_estimated: bool = True
    window_limit: int | None = None
    reserved_output: int | None = None
    actual_prompt: int | None = None
    actual_completion: int | None = None
    session_total_tokens: int = 0
    compression_count: int = 0
    last_updated: float = field(default_factory=time.time)

    @property
    def window_total(self) -> int | None:
        """Prompt + reserved output for the about-to-send window, if known."""

        if self.window_prompt is None:
            return None
        reserved = self.reserved_output or 0
        return self.window_prompt + reserved

    @property
    def window_ratio(self) -> float | None:
        """Used / limit for the current window, or ``None`` if unknown."""

        if (
            self.window_prompt is None
            or not self.window_limit
        ):
            return None
        return self.window_prompt / float(self.window_limit)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_prompt": self.window_prompt,
            "window_prompt_is_estimated": self.window_prompt_is_estimated,
            "window_limit": self.window_limit,
            "reserved_output": self.reserved_output,
            "actual_prompt": self.actual_prompt,
            "actual_completion": self.actual_completion,
            "session_total_tokens": self.session_total_tokens,
            "compression_count": self.compression_count,
            "last_updated": self.last_updated,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ContextUsage":
        return ContextUsage(
            window_prompt=_opt_int(d.get("window_prompt")),
            window_prompt_is_estimated=bool(d.get("window_prompt_is_estimated", True)),
            window_limit=_opt_int(d.get("window_limit")),
            reserved_output=_opt_int(d.get("reserved_output")),
            actual_prompt=_opt_int(d.get("actual_prompt")),
            actual_completion=_opt_int(d.get("actual_completion")),
            session_total_tokens=int(d.get("session_total_tokens", 0)),
            compression_count=int(d.get("compression_count", 0)),
            last_updated=float(d.get("last_updated", 0.0)),
        )


# ---------------------------------------------------------------------- #
# AgentMessage (plan §4.1 / §A5)
# ---------------------------------------------------------------------- #


class AgentMessageStatus:
    """Lifecycle of a persisted message (plan §A5 §5.2)."""

    DRAFT = "draft"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    KNOWN: frozenset[str] = frozenset({DRAFT, STREAMING, COMPLETED, FAILED, CANCELLED})


@dataclass(frozen=True)
class AgentMessage:
    """One chat message bound to a run (plan §4.1).

    Carries the optional usage / artifact refs so a single message card can
    show tokens, tool calls and produced artifacts without re-deriving them.
    """

    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = ""
    role: str = "user"  # user / assistant / system / tool
    content: str = ""
    status: str = AgentMessageStatus.COMPLETED
    usage: ContextUsage | None = None
    artifact_refs: tuple[str, ...] = ()
    timestamp: float = field(default_factory=time.time)
    error_code: str = ""

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("message_id cannot be empty")
        if self.role not in ("user", "assistant", "system", "tool"):
            object.__setattr__(self, "role", "user")
        status = str(self.status or "").strip().lower()
        if status not in AgentMessageStatus.KNOWN:
            status = AgentMessageStatus.COMPLETED
        object.__setattr__(self, "status", status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "run_id": self.run_id,
            "role": self.role,
            "content": sanitize_for_storage(self.content),
            "status": self.status,
            "usage": self.usage.to_dict() if self.usage else None,
            "artifact_refs": list(self.artifact_refs),
            "timestamp": self.timestamp,
            "error_code": self.error_code,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AgentMessage":
        usage_payload = d.get("usage")
        return AgentMessage(
            message_id=str(d.get("message_id", "")) or uuid.uuid4().hex,
            run_id=str(d.get("run_id", "")),
            role=str(d.get("role", "user")),
            content=str(d.get("content", "")),
            status=str(d.get("status", AgentMessageStatus.COMPLETED)),
            usage=ContextUsage.from_dict(usage_payload) if usage_payload else None,
            artifact_refs=tuple(d.get("artifact_refs", [])),
            timestamp=float(d.get("timestamp", 0.0)),
            error_code=str(d.get("error_code", "")),
        )


# ---------------------------------------------------------------------- #
# AgentEvent (plan §4.1 / §A1 event set)
# ---------------------------------------------------------------------- #


class AgentEventKind:
    """Minimum event vocabulary for the unified run (plan §A1).

    Events are the single source of truth for the Run Drawer and the audit
    log; the orchestrator emits one per state flip / tool boundary.
    """

    RUN_CREATED = "run.created"
    RUN_CONTEXT_STARTED = "run.context_started"
    RUN_CONTEXT_READY = "run.context_ready"
    RUN_PLAN_STARTED = "run.plan_started"
    RUN_PLAN_READY = "run.plan_ready"
    RUN_WAITING_USER_INPUT = "run.waiting_user_input"
    RUN_WAITING_APPROVAL = "run.waiting_approval"
    RUN_REQUEST_HEADER = "run.request_header"
    RUN_REQUEST_RETRY = "run.request_retry"
    RUN_INBOX_ENQUEUED = "run.inbox_enqueued"
    RUN_INBOX_DROPPED = "run.inbox_dropped"
    RUN_PRE_STEP = "run.pre_step"
    RUN_TOOL_STARTED = "run.tool_started"
    RUN_TOOL_PROGRESS = "run.tool_progress"
    RUN_TOOL_FINISHED = "run.tool_finished"
    RUN_VERIFICATION_STARTED = "run.verification_started"
    RUN_VERIFICATION_FINISHED = "run.verification_finished"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    RUN_CANCELLED = "run.cancelled"
    RUN_CHECKPOINT_SAVED = "run.checkpoint_saved"
    RUN_USAGE_RECORDED = "run.usage_recorded"
    ARTIFACT_STAGED = "artifact.staged"

    KNOWN: frozenset[str] = frozenset(
        {
            RUN_CREATED, RUN_CONTEXT_STARTED, RUN_CONTEXT_READY,
            RUN_PLAN_STARTED, RUN_PLAN_READY, RUN_WAITING_USER_INPUT,
            RUN_WAITING_APPROVAL, RUN_REQUEST_HEADER, RUN_REQUEST_RETRY,
            RUN_INBOX_ENQUEUED, RUN_INBOX_DROPPED, RUN_PRE_STEP,
            RUN_TOOL_STARTED,
            RUN_TOOL_PROGRESS,
            RUN_TOOL_FINISHED, RUN_VERIFICATION_STARTED,
            RUN_VERIFICATION_FINISHED, RUN_COMPLETED, RUN_FAILED,
            RUN_CANCEL_REQUESTED, RUN_CANCELLED, RUN_CHECKPOINT_SAVED,
            RUN_USAGE_RECORDED, ARTIFACT_STAGED,
        }
    )


@dataclass(frozen=True)
class AgentEvent:
    """An immutable record of one observable run step (plan §4.1)."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = ""
    kind: str = ""
    timestamp: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)
    parent_id: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id cannot be empty")
        kind = str(self.kind or "").strip().lower()
        # Unknown event kinds are kept verbatim (forward compatibility) but the
        # caller is expected to validate; nothing silently maps to a "safe" kind.
        object.__setattr__(self, "kind", kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "payload": sanitize_for_storage(dict(self.payload or {})),
            "parent_id": self.parent_id,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AgentEvent":
        return AgentEvent(
            event_id=str(d.get("event_id", "")) or uuid.uuid4().hex,
            run_id=str(d.get("run_id", "")),
            kind=str(d.get("kind", "")),
            timestamp=float(d.get("timestamp", 0.0)),
            payload=dict(d.get("payload", {})),
            parent_id=str(d.get("parent_id", "")),
        )


# ---------------------------------------------------------------------- #
# ApprovalRequest (plan §4.1 / §A2)
# ---------------------------------------------------------------------- #


class ApprovalRisk:
    """Risk tier for a tool action (plan §A2 permission matrix)."""

    READ_ONLY = "read_only"
    PREVIEW = "preview"
    PROPOSAL = "proposal"
    WRITE = "write"
    EXPENSIVE = "expensive"

    KNOWN: frozenset[str] = frozenset(
        {READ_ONLY, PREVIEW, PROPOSAL, WRITE, EXPENSIVE}
    )
    #: Risk tiers that require an explicit host grant before execution.
    REQUIRES_GRANT: frozenset[str] = frozenset({PROPOSAL, WRITE, EXPENSIVE})


@dataclass(frozen=True)
class ApprovalRequest:
    """A tool-level approval ask shown to the user (plan §A2).

    Distinct from :class:`approval_service.ApprovalGrant` — this is the
    *request* the UI renders; the host issues a grant only after the user
    clicks confirm.  It carries enough to render a card (scope summary, diff,
    reversibility, expiry) without leaking secrets.
    """

    approval_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = ""
    tool_name: str = ""
    risk: str = ApprovalRisk.WRITE
    scope_summary: str = ""
    expected_change: str = ""
    diff_summary: dict[str, Any] | None = None
    can_undo: bool = False
    single_use: bool = True
    expires_at: float = 0.0  # 0 = no expiry (orchestrator applies a default)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id cannot be empty")
        if not self.tool_name:
            raise ValueError("tool_name cannot be empty")
        risk = str(self.risk or "").strip().lower()
        if risk not in ApprovalRisk.KNOWN:
            risk = ApprovalRisk.WRITE  # fail safe: assume write-level risk
        object.__setattr__(self, "risk", risk)

    @property
    def requires_grant(self) -> bool:
        return self.risk in ApprovalRisk.REQUIRES_GRANT

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "tool_name": self.tool_name,
            "risk": self.risk,
            "scope_summary": sanitize_for_storage(self.scope_summary),
            "expected_change": sanitize_for_storage(self.expected_change),
            "diff_summary": sanitize_for_storage(self.diff_summary)
            if self.diff_summary is not None
            else None,
            "can_undo": self.can_undo,
            "single_use": self.single_use,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ApprovalRequest":
        return ApprovalRequest(
            approval_id=str(d.get("approval_id", "")) or uuid.uuid4().hex,
            run_id=str(d.get("run_id", "")),
            tool_name=str(d.get("tool_name", "")),
            risk=str(d.get("risk", ApprovalRisk.WRITE)),
            scope_summary=str(d.get("scope_summary", "")),
            expected_change=str(d.get("expected_change", "")),
            diff_summary=dict(d["diff_summary"]) if d.get("diff_summary") else None,
            can_undo=bool(d.get("can_undo", False)),
            single_use=bool(d.get("single_use", True)),
            expires_at=float(d.get("expires_at", 0.0)),
            created_at=float(d.get("created_at", 0.0)),
        )


# ---------------------------------------------------------------------- #
# ArtifactRef (plan §4.1) — V2 artifact reference
# ---------------------------------------------------------------------- #


class ArtifactKind:
    """What an artifact contains, used by verify / preview / recovery."""

    HTML_DOCUMENT = "html_document"
    SCENE_JSON = "scene_json"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TRANSCRIPT = "transcript"
    EMBEDDING = "embedding"
    EDIT_PLAN = "edit_plan"
    TEXT = "text"
    OTHER = "other"

    KNOWN: frozenset[str] = frozenset(
        {
            HTML_DOCUMENT, SCENE_JSON, IMAGE, VIDEO, AUDIO,
            TRANSCRIPT, EMBEDDING, EDIT_PLAN, TEXT, OTHER,
        }
    )


@dataclass(frozen=True)
class ArtifactRef:
    """A pointer to a produced, previewable, verifiable result.

    Keeps only an id / kind / provenance; the heavy payload lives in the
    artifact store (plan §2.6 / §7 ``artifact_store``).  Long HTML documents
    are referenced by hash, never inlined into a chat bubble.
    """

    artifact_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = ArtifactKind.TEXT
    #: Opaque pointer resolved by the artifact store (path, hash or store key).
    location: str = ""
    preview_ref: str = ""
    provenance: str = ""
    payload_hash: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id cannot be empty")
        kind = str(self.kind or "").strip().lower()
        if kind not in ArtifactKind.KNOWN:
            kind = ArtifactKind.OTHER
        object.__setattr__(self, "kind", kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "location": self.location,
            "preview_ref": self.preview_ref,
            "provenance": sanitize_for_storage(self.provenance),
            "payload_hash": self.payload_hash,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ArtifactRef":
        return ArtifactRef(
            artifact_id=str(d.get("artifact_id", "")) or uuid.uuid4().hex,
            kind=str(d.get("kind", ArtifactKind.TEXT)),
            location=str(d.get("location", "")),
            preview_ref=str(d.get("preview_ref", "")),
            provenance=str(d.get("provenance", "")),
            payload_hash=str(d.get("payload_hash", "")),
            created_at=float(d.get("created_at", 0.0)),
        )


# ---------------------------------------------------------------------- #
# AgentRun (plan §4.1)
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgentRun:
    """One observable, cancellable, recoverable attempt at a task.

    ``state`` follows :class:`AgentRunState`.  Runs are immutable snapshots;
    the orchestrator derives a new run on each transition so subscribers always
    see a consistent point-in-time view (plan §4.2 rule 2).
    """

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str = ""
    mode: str = "chat"
    state: str = AgentRunState.CREATED
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    checkpoint_id: str = ""
    last_event_id: str = ""
    context_usage: ContextUsage | None = None
    artifact_refs: tuple[str, ...] = ()
    error_code: str = ""
    #: Diagnostic reason for the current state (e.g. why a mode is unavailable).
    state_reason: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id cannot be empty")
        if not self.task_id:
            raise ValueError("task_id cannot be empty")
        object.__setattr__(self, "state", AgentRunState.normalize(self.state))

    @property
    def is_terminal(self) -> bool:
        return AgentRunState.is_terminal(self.state)

    @property
    def is_user_blocked(self) -> bool:
        return AgentRunState.is_user_blocked(self.state)

    def with_state(self, new_state: str, *, reason: str = "") -> "AgentRun":
        """Return a copy advanced to *new_state*, validating the transition."""

        validated = AgentRunState.transition(self.state, new_state)
        ended = self.ended_at
        if AgentRunState.is_terminal(validated) and not ended:
            ended = time.time()
        return AgentRun(
            run_id=self.run_id,
            task_id=self.task_id,
            mode=self.mode,
            state=validated,
            started_at=self.started_at,
            ended_at=ended,
            checkpoint_id=self.checkpoint_id,
            last_event_id=self.last_event_id,
            context_usage=self.context_usage,
            artifact_refs=self.artifact_refs,
            error_code=self.error_code,
            state_reason=reason or self.state_reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": AGENT_MODELS_SCHEMA_VERSION,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "mode": self.mode,
            "state": self.state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "checkpoint_id": self.checkpoint_id,
            "last_event_id": self.last_event_id,
            "context_usage": self.context_usage.to_dict()
            if self.context_usage
            else None,
            "artifact_refs": list(self.artifact_refs),
            "error_code": self.error_code,
            "state_reason": sanitize_for_storage(self.state_reason),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AgentRun":
        usage_payload = d.get("context_usage")
        return AgentRun(
            run_id=str(d.get("run_id", "")) or uuid.uuid4().hex,
            task_id=str(d.get("task_id", "")),
            mode=str(d.get("mode", "chat")),
            state=str(d.get("state", AgentRunState.CREATED)),
            started_at=float(d.get("started_at", 0.0)),
            ended_at=float(d.get("ended_at", 0.0)),
            checkpoint_id=str(d.get("checkpoint_id", "")),
            last_event_id=str(d.get("last_event_id", "")),
            context_usage=ContextUsage.from_dict(usage_payload)
            if usage_payload
            else None,
            artifact_refs=tuple(d.get("artifact_refs", [])),
            error_code=str(d.get("error_code", "")),
            state_reason=str(d.get("state_reason", "")),
        )


# ---------------------------------------------------------------------- #
# AgentCheckpoint (plan §4.1 / §A5)
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgentCheckpoint:
    """A recoverable snapshot of an in-flight run (plan §A5 / §A0).

    Stores only IDs, counters and hashes — never secrets, full prompts or
    large payloads (plan §A5 hard constraints).  ``protected_context`` lists
    the keys that the compressor must never drop (project canvas, fps, scene
    revision, ...).
    """

    checkpoint_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = ""
    state: str = AgentRunState.CREATED
    completed_steps: tuple[str, ...] = ()
    messages_digest: str = ""
    context_hash: str = ""
    protected_context: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id cannot be empty")
        object.__setattr__(self, "state", AgentRunState.normalize(self.state))

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "state": self.state,
            "completed_steps": list(self.completed_steps),
            "messages_digest": self.messages_digest,
            "context_hash": self.context_hash,
            "protected_context": list(self.protected_context),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AgentCheckpoint":
        return AgentCheckpoint(
            checkpoint_id=str(d.get("checkpoint_id", "")) or uuid.uuid4().hex,
            run_id=str(d.get("run_id", "")),
            state=str(d.get("state", AgentRunState.CREATED)),
            completed_steps=tuple(d.get("completed_steps", [])),
            messages_digest=str(d.get("messages_digest", "")),
            context_hash=str(d.get("context_hash", "")),
            protected_context=tuple(d.get("protected_context", [])),
            created_at=float(d.get("created_at", 0.0)),
        )


# ---------------------------------------------------------------------- #
# HTML generation protocol version (plan §A0 step 8)
# ---------------------------------------------------------------------- #


class ResponsePayloadKind:
    """Disambiguates a model response so the router never guesses from text.

    Plan §A0 step 8: the unified agent must not infer the response type by
    inspecting raw text.  Every generation path declares its payload kind.
    """

    HTML_DOCUMENT = "html_document"
    SCENE_JSON = "scene_json"
    STREAM_TEXT = "stream_text"
    TOOL_CALL = "tool_call"

    KNOWN: frozenset[str] = frozenset(
        {HTML_DOCUMENT, SCENE_JSON, STREAM_TEXT, TOOL_CALL}
    )


#: Current HTML / generation transport protocol version.  Bumped when the
#: sanitizer contract, page marker convention or transport shape changes so a
#: stale proposal can be detected and re-validated instead of applied blindly.
HTML_GENERATION_PROTOCOL_VERSION: str = "1.0"


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


def _opt_int(value: object) -> int | None:
    """Coerce to ``int`` or ``None`` without raising on empty / None."""

    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "AGENT_MODELS_SCHEMA_VERSION",
    "AgentRunState",
    "AgentTaskKind",
    "AgentTask",
    "ContextUsage",
    "AgentMessageStatus",
    "AgentMessage",
    "AgentEventKind",
    "AgentEvent",
    "ApprovalRisk",
    "ApprovalRequest",
    "ArtifactKind",
    "ArtifactRef",
    "AgentRun",
    "AgentCheckpoint",
    "ResponsePayloadKind",
    "HTML_GENERATION_PROTOCOL_VERSION",
]
