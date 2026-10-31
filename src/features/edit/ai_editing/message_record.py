"""Full-content message record with backward-compatible migration (plan §A5).

The legacy ``MessageSummary`` only stored a short snippet (<= 500 chars), so
switching sessions or recovering after a restart could not restore the real
conversation.  ``MessageRecord`` upgrades that to the **full redacted body**
plus message status, token usage and artifact refs, while still reading old
summary data without losing it.

Hard rules (plan §A5 §5.1 / §10.1):

- **Redacted before storage.**  ``to_dict`` runs the body through
  ``sanitize_for_storage`` and sets ``redacted=True`` when sanitisation
  changed anything, so a reader always knows the stored text was filtered.
- **Bounded body.**  Oversized output is truncated at ``MESSAGE_CONTENT_LIMIT``
  with a visible marker; the full payload belongs in an artifact ref, not the
  chat log (plan §A5 §5.1 / §A4 §4.2 step 13).
- **Old data is migrated, not dropped.**  ``from_dict`` accepts the legacy
  ``MessageSummary`` shape (``text`` field, no status/usage) and marks
  ``content_complete=False`` so the UI can show it came from a summary.
- **No secrets.**  The record never stores API keys, absolute paths or full
  sensitive prompts; only redacted, rebuildable references.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.features.ai_harness.invocation_models import sanitize_for_storage
from src.features.edit.ai_editing.agent_models import (
    AgentMessage,
)
from src.features.edit.ai_editing.session_models import MessageSummary


#: Schema version for persisted message records.
MESSAGE_RECORD_VERSION: int = 1

#: Soft cap on a single message body (plan §A5 §5.1).  Oversized output is
#: truncated and the full payload is kept as an artifact reference instead.
MESSAGE_CONTENT_LIMIT: int = 64_000

#: Visible marker appended when a body is truncated.
_TRUNCATION_MARKER = "\n…[内容过长已截断，完整内容见 artifact 引用]"


class MessageRecordStatus:
    """Persisted message lifecycle (plan §A5 §5.2 step 2)."""

    DRAFT = "draft"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    KNOWN: frozenset[str] = frozenset(
        {DRAFT, STREAMING, COMPLETED, FAILED, CANCELLED}
    )


@dataclass(frozen=True)
class MessageRecord:
    """One persisted, redacted chat message (plan §A5 §5.1)."""

    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    role: str = "user"  # user / assistant / system / tool
    content: str = ""
    status: str = MessageRecordStatus.COMPLETED
    run_id: str = ""
    artifact_refs: tuple[str, ...] = ()
    #: Serialised :class:`ContextUsage` (a plain dict), or ``None``.
    usage: dict[str, Any] | None = None
    timestamp: float = field(default_factory=time.time)
    #: ``True`` when the stored body was altered by sanitisation.
    redacted: bool = False
    #: ``False`` when migrated from a legacy summary (body may be incomplete).
    content_complete: bool = True
    #: Backward-compat field carried over from ``MessageSummary``.
    decision_id: str = ""

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("message_id cannot be empty")
        if self.role not in ("user", "assistant", "system", "tool"):
            object.__setattr__(self, "role", "user")
        status = str(self.status or "").strip().lower()
        if status not in MessageRecordStatus.KNOWN:
            status = MessageRecordStatus.COMPLETED
        object.__setattr__(self, "status", status)

    # ---------------- serialization ---------------- #

    def to_dict(self) -> dict[str, Any]:
        truncated, was_truncated = truncate_for_storage(self.content)
        redacted_body = sanitize_for_storage(truncated)
        redacted = was_truncated or redacted_body != truncated
        return {
            "schema_version": MESSAGE_RECORD_VERSION,
            "message_id": self.message_id,
            "role": self.role,
            "content": redacted_body,
            "status": self.status,
            "run_id": self.run_id,
            "artifact_refs": list(self.artifact_refs),
            "usage": self.usage,
            "timestamp": self.timestamp,
            "redacted": redacted,
            "content_complete": self.content_complete,
            "decision_id": self.decision_id,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MessageRecord":
        # Accept both the new ``content`` key and the legacy ``text`` key.
        content = str(d.get("content", d.get("text", "")))
        # Legacy summaries have no status / content_complete; mark incomplete.
        content_complete = bool(d.get("content_complete", "content" in d))
        usage = d.get("usage")
        return MessageRecord(
            message_id=str(d.get("message_id", "")) or uuid.uuid4().hex,
            role=str(d.get("role", "user")),
            content=content,
            status=str(d.get("status", MessageRecordStatus.COMPLETED)),
            run_id=str(d.get("run_id", "")),
            artifact_refs=tuple(d.get("artifact_refs", [])),
            usage=dict(usage) if isinstance(usage, dict) else None,
            timestamp=_coerce_timestamp(d.get("timestamp")),
            redacted=bool(d.get("redacted", False)),
            content_complete=content_complete,
            decision_id=str(d.get("decision_id", "")),
        )

    # ---------------- conversions ---------------- #

    @staticmethod
    def from_summary(summary: MessageSummary) -> "MessageRecord":
        """Migrate a legacy :class:`MessageSummary` (plan §10.1 §2).

        The summary's short snippet becomes the body, flagged
        ``content_complete=False`` so the UI can show it may be truncated.
        """

        artifact_id = summary.artifact_id
        return MessageRecord(
            message_id=summary.message_id,
            role=summary.role,
            content=summary.text,
            status=MessageRecordStatus.COMPLETED,
            run_id=summary.run_id,
            artifact_refs=(artifact_id,) if artifact_id else (),
            timestamp=summary.timestamp,
            content_complete=False,
            decision_id=summary.decision_id,
        )

    @staticmethod
    def from_agent_message(message: AgentMessage) -> "MessageRecord":
        """Persist a runtime :class:`AgentMessage` (full body + usage)."""

        usage = message.usage.to_dict() if message.usage is not None else None
        return MessageRecord(
            message_id=message.message_id,
            role=message.role,
            content=message.content,
            status=message.status,
            run_id=message.run_id,
            artifact_refs=message.artifact_refs,
            usage=usage,
            timestamp=message.timestamp,
            content_complete=True,
        )


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


def _coerce_timestamp(value: Any) -> float:
    """Defensive timestamp coercion for persisted records (AUD-13915).

    其余 ``from_dict`` 字段都做防御性转换，唯独 timestamp 遇到损坏值（如
    ``"abc"`` 或嵌套结构）会抛 ValueError/TypeError，一条坏消息即可让整个
    会话恢复失败。与"Old data is migrated, not dropped"目标一致：转换失败
    回退 0.0（确定性排序，坏记录沉底），不中断加载。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def truncate_for_storage(
    content: str, *, limit: int = MESSAGE_CONTENT_LIMIT
) -> tuple[str, bool]:
    """Return ``(body, was_truncated)`` for *content*.

    Oversized output is cut at *limit* and a visible marker is appended so a
    reader never silently loses the tail (plan §A5 §5.1).
    """

    body = str(content or "")
    if len(body) <= limit:
        return body, False
    return body[:limit] + _TRUNCATION_MARKER, True


__all__ = [
    "MESSAGE_CONTENT_LIMIT",
    "MESSAGE_RECORD_VERSION",
    "MessageRecord",
    "MessageRecordStatus",
    "truncate_for_storage",
]
