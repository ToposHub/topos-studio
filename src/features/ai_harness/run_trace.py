"""统一 AI 运行轨迹与派生投影。

DeepSeek Harness 的一个很值得借鉴的设计是：运行记录是 append-only 的事实
来源，UI、恢复和诊断都从事实重新投影，而不是各自维护一份容易漂移的状态。

Topos 已经有两条事件流：``HarnessRuntime`` 的 ``RunEvent`` 和编辑助手
``AgentOrchestrator`` 的 ``AgentEvent``。本模块不强行合并两套领域模型，而是
提供一个很薄的、线程安全的适配层：

* 两种事件统一成带 session watermark 的 ``TraceEvent``；
* ``RunTraceProjection`` 从事件重新计算阶段、工具调用、usage、artifact 和
  诊断摘要；
* ``RunTraceStore`` 提供去重、有限保留、导出/恢复，供 Run Drawer、工程保存和
  重启后的复盘使用。

本模块纯 Python，不依赖 Qt / OpenGL / provider。payload 会经过脱敏和有界截断，
避免把完整 prompt、模型输出、绝对路径或 secret 变成新的持久化泄漏面。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import islice
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal

from src.features.ai_harness.invocation_models import (
    RunEvent,
    sanitize_for_storage,
)
from src.features.ai_harness.session_fork import SessionFork, SessionForkError


TRACE_SCHEMA_VERSION = 1
TraceSource = Literal["runtime", "assistant"]
TraceSubscriber = Callable[["TraceEvent"], None]


def _bounded_trace_value(
    value: Any,
    *,
    max_string: int = 1200,
    max_items: int = 48,
    depth: int = 0,
) -> Any:
    """Return a JSON-friendly, bounded diagnostic payload.

    Trace is for replay/diagnostics, not for storing the full conversation or a
    tool result. Keeping the bound here, after ``sanitize_for_storage``, makes
    every ingestion path safe by default.
    """

    if depth >= 5:
        return "<trace-depth-limit>"
    if isinstance(value, str):
        bounded = value[:max_string]
        sanitized = sanitize_for_storage(bounded)
        if len(value) <= max_string and len(sanitized) <= max_string:
            return sanitized
        return sanitized[:max_string] + "…<trace-truncated>"
    if isinstance(value, Mapping):
        items = list(islice(value.items(), max_items))
        result = {
            str(sanitize_for_storage(key)): _bounded_trace_value(
                item,
                max_string=max_string,
                max_items=max_items,
                depth=depth + 1,
            )
            for key, item in items
        }
        if len(value) > max_items:
            result["_trace_truncated_items"] = len(value) - max_items
        return result
    if isinstance(value, (list, tuple)):
        result = [
            _bounded_trace_value(
                item,
                max_string=max_string,
                max_items=max_items,
                depth=depth + 1,
            )
            for item in list(value)[:max_items]
        ]
        if len(value) > max_items:
            result.append(f"<trace-truncated-items:{len(value) - max_items}>")
        return result
    return sanitize_for_storage(value)


@dataclass(frozen=True)
class TraceEvent:
    """One normalized event in one assistant session's append-only trace."""

    seq: int
    event_id: str
    run_id: str
    source: TraceSource
    kind: str
    timestamp: float = field(default_factory=time.time)
    payload: Mapping[str, Any] = field(default_factory=dict)
    node_id: str = ""
    parent_id: str = ""

    def __post_init__(self) -> None:
        if self.seq < 0:
            raise ValueError("trace seq cannot be negative")
        if not self.event_id:
            raise ValueError("trace event_id cannot be empty")
        if not self.run_id:
            raise ValueError("trace run_id cannot be empty")
        if self.source not in ("runtime", "assistant"):
            raise ValueError(f"unsupported trace source: {self.source!r}")
        object.__setattr__(self, "kind", str(self.kind or "").strip().lower())
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(
                dict(_bounded_trace_value(dict(self.payload or {})))
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "source": self.source,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "payload": _bounded_trace_value(dict(self.payload)),
            "node_id": self.node_id,
            "parent_id": self.parent_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TraceEvent":
        if not isinstance(data, dict):
            raise TypeError("trace event must be an object")
        source = str(data.get("source", ""))
        if source not in ("runtime", "assistant"):
            raise ValueError("trace event source is invalid")
        payload = data.get("payload", {})
        return cls(
            seq=int(data.get("seq", -1)),
            event_id=str(data.get("event_id", "")),
            run_id=str(data.get("run_id", "")),
            source=source,  # type: ignore[arg-type]
            kind=str(data.get("kind", "")),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
            payload=dict(payload) if isinstance(payload, dict) else {},
            node_id=str(data.get("node_id", "")),
            parent_id=str(data.get("parent_id", "")),
        )


@dataclass(frozen=True)
class RunTraceProjection:
    """Derived, UI-friendly state for one run at one session watermark."""

    session_id: str
    run_id: str
    as_of_seq: int = -1
    event_count: int = 0
    last_event_kind: str = ""
    status: str = ""
    phase: str = ""
    started_at: float | None = None
    ended_at: float | None = None
    tool_call_count: int = 0
    tool_completed_count: int = 0
    tool_failed_count: int = 0
    pending_tool_calls: tuple[str, ...] = ()
    artifact_count: int = 0
    total_tokens: int = 0
    last_diagnostic: dict[str, Any] = field(default_factory=dict)
    trace_truncated: bool = False
    dropped_event_count: int = 0

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return max(0, int((self.ended_at - self.started_at) * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "as_of_seq": self.as_of_seq,
            "event_count": self.event_count,
            "last_event_kind": self.last_event_kind,
            "status": self.status,
            "phase": self.phase,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "tool_call_count": self.tool_call_count,
            "tool_completed_count": self.tool_completed_count,
            "tool_failed_count": self.tool_failed_count,
            "pending_tool_calls": list(self.pending_tool_calls),
            "artifact_count": self.artifact_count,
            "total_tokens": self.total_tokens,
            "last_diagnostic": sanitize_for_storage(self.last_diagnostic),
            "trace_truncated": self.trace_truncated,
            "dropped_event_count": self.dropped_event_count,
        }


@dataclass(frozen=True)
class TraceRestoreReport:
    restored_sessions: tuple[str, ...] = ()
    restored_events: int = 0
    skipped_entries: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.skipped_entries

    def to_dict(self) -> dict[str, Any]:
        return {
            "restored_sessions": list(self.restored_sessions),
            "restored_events": self.restored_events,
            "skipped_entries": list(self.skipped_entries),
            "ok": self.ok,
        }


class RunTraceStore:
    """Thread-safe append-only trace store with projection and persistence.

    ``max_events_per_session`` is a deliberate safety valve. Once the limit is
    reached, the oldest events are dropped but sequence numbers continue to
    advance, so consumers can see that a projection is partial instead of
    mistaking it for a complete replay.
    """

    def __init__(self, *, max_events_per_session: int = 4000) -> None:
        if max_events_per_session <= 0:
            raise ValueError("max_events_per_session must be positive")
        self._max_events = max_events_per_session
        self._events: dict[str, list[TraceEvent]] = {}
        self._next_seq: dict[str, int] = {}
        self._dropped: dict[str, int] = {}
        self._seen_ids: dict[str, set[tuple[str, str]]] = {}
        self._subscribers: list[TraceSubscriber] = []
        self._lock = threading.RLock()

    def subscribe(self, fn: TraceSubscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(fn)

        def _unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return _unsubscribe

    def append(
        self,
        *,
        session_id: str,
        run_id: str,
        source: TraceSource,
        kind: str,
        payload: dict[str, Any] | None = None,
        event_id: str = "",
        timestamp: float | None = None,
        node_id: str = "",
        parent_id: str = "",
    ) -> TraceEvent | None:
        """Append one event; duplicate ``(source, event_id)`` is ignored."""

        session_id = str(session_id or "default")
        event_id = str(event_id or uuid.uuid4().hex)
        key = (source, event_id)
        with self._lock:
            seen = self._seen_ids.setdefault(session_id, set())
            if key in seen:
                return None
            seq = self._next_seq.get(session_id, 0)
            event = TraceEvent(
                seq=seq,
                event_id=event_id,
                run_id=str(run_id),
                source=source,
                kind=kind,
                timestamp=float(timestamp if timestamp is not None else time.time()),
                payload=dict(payload or {}),
                node_id=node_id,
                parent_id=parent_id,
            )
            self._events.setdefault(session_id, []).append(event)
            self._next_seq[session_id] = seq + 1
            seen.add(key)
            if len(self._events[session_id]) > self._max_events:
                self._events[session_id].pop(0)
                self._dropped[session_id] = self._dropped.get(session_id, 0) + 1
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:
                # Trace observers are diagnostics/UI conveniences. They must
                # never become a new failure path for the assistant run.
                continue
        return event

    def append_runtime_event(
        self, event: RunEvent, *, session_id: str,
    ) -> TraceEvent | None:
        return self.append(
            session_id=session_id,
            run_id=event.run_id,
            source="runtime",
            kind=event.kind,
            payload=event.payload,
            event_id=event.event_id,
            timestamp=event.timestamp,
            node_id=event.node_id,
        )

    def append_agent_event(
        self, event: Any, *, session_id: str,
    ) -> TraceEvent | None:
        return self.append(
            session_id=session_id,
            run_id=str(getattr(event, "run_id", "")),
            source="assistant",
            kind=str(getattr(event, "kind", "")),
            payload=dict(getattr(event, "payload", {}) or {}),
            event_id=str(getattr(event, "event_id", "")),
            timestamp=float(getattr(event, "timestamp", time.time()) or time.time()),
            parent_id=str(getattr(event, "parent_id", "")),
        )

    def events(
        self, *, session_id: str, run_id: str | None = None,
    ) -> tuple[TraceEvent, ...]:
        with self._lock:
            values = tuple(self._events.get(str(session_id), ()))
        if run_id is None:
            return values
        return tuple(event for event in values if event.run_id == run_id)

    def project(self, *, session_id: str, run_id: str) -> RunTraceProjection:
        session_id = str(session_id or "default")
        with self._lock:
            events = tuple(
                event for event in self._events.get(session_id, ())
                if event.run_id == run_id
            )
            dropped = self._dropped.get(session_id, 0)
        return project_trace_events(
            session_id=session_id,
            run_id=str(run_id),
            events=events,
            dropped_event_count=dropped,
        )

    def project_session(self, *, session_id: str) -> tuple[RunTraceProjection, ...]:
        session_id = str(session_id or "default")
        with self._lock:
            run_ids = tuple(dict.fromkeys(
                event.run_id for event in self._events.get(session_id, ())
            ))
        return tuple(self.project(session_id=session_id, run_id=run_id) for run_id in run_ids)

    def fork_session(
        self,
        source_session_id: str,
        *,
        at_seq: int,
        fork_session_id: str | None = None,
    ) -> SessionFork:
        """Fork trace facts at an existing event boundary.

        The child receives new event IDs and its own sequence watermark, but
        no provider worker, approval grant or live session object is copied.
        Hosts can then attach a normal chat/session catalog entry to the child.
        """

        source_session_id = str(source_session_id or "default")
        boundary = int(at_seq)
        child_id = str(fork_session_id or uuid.uuid4().hex).strip()
        with self._lock:
            source = tuple(self._events.get(source_session_id, ()))
            if boundary >= 0 and boundary not in {event.seq for event in source}:
                raise SessionForkError("fork must target an existing trace boundary")
            if boundary < -1 or not child_id or child_id in self._events:
                raise SessionForkError("invalid or already-used fork session id")
            copied = [
                TraceEvent(
                    seq=event.seq,
                    event_id=uuid.uuid4().hex,
                    run_id=event.run_id,
                    source=event.source,
                    kind=event.kind,
                    timestamp=event.timestamp,
                    payload=dict(event.payload),
                    node_id=event.node_id,
                    parent_id=event.parent_id,
                )
                for event in source if event.seq <= boundary
            ]
            self._events[child_id] = copied
            self._next_seq[child_id] = max((event.seq for event in copied), default=-1) + 1
            self._dropped[child_id] = 0
            self._seen_ids[child_id] = {
                (event.source, event.event_id) for event in copied
            }
        return SessionFork(
            source_session_id=source_session_id,
            fork_session_id=child_id,
            source_seq=boundary,
            event_count=len(copied),
        )

    def export_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = {
                "schema_version": TRACE_SCHEMA_VERSION,
                "max_events_per_session": self._max_events,
                "sessions": {
                    session_id: {
                        "next_seq": self._next_seq.get(session_id, 0),
                        "dropped_event_count": self._dropped.get(session_id, 0),
                        "events": [event.to_dict() for event in events],
                    }
                    for session_id, events in self._events.items()
                },
            }
        return sanitize_for_storage(snapshot)

    def restore_snapshot(self, snapshot: dict[str, Any]) -> TraceRestoreReport:
        if not isinstance(snapshot, dict):
            return TraceRestoreReport(skipped_entries=("snapshot_not_object",))
        if int(snapshot.get("schema_version", 0) or 0) != TRACE_SCHEMA_VERSION:
            return TraceRestoreReport(skipped_entries=("snapshot_schema_unsupported",))
        sessions = snapshot.get("sessions")
        if not isinstance(sessions, dict):
            return TraceRestoreReport(skipped_entries=("snapshot_sessions_invalid",))

        restored_sessions: list[str] = []
        skipped: list[str] = []
        restored_events = 0
        # Parse every session into detached values first. A malformed metadata
        # field must not leave a half-restored store behind if the caller keeps
        # the existing in-memory trace after a failed load.
        pending: dict[
            str, tuple[list[TraceEvent], int, int, set[tuple[str, str]]]
        ] = {}
        for session_id, raw_session in sessions.items():
            sid = str(session_id)
            if not isinstance(raw_session, dict):
                skipped.append(f"{sid}:not_object")
                continue
            raw_events = raw_session.get("events", ())
            if not isinstance(raw_events, (list, tuple)):
                skipped.append(f"{sid}:events_invalid")
                continue
            rows: list[TraceEvent] = []
            seen: set[tuple[str, str]] = set()
            for index, raw_event in enumerate(raw_events):
                try:
                    event = TraceEvent.from_dict(raw_event)
                except (TypeError, ValueError, KeyError, OverflowError) as exc:
                    skipped.append(f"{sid}:event_{index}:{type(exc).__name__}")
                    continue
                key = (event.source, event.event_id)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(event)
            rows.sort(key=lambda event: event.seq)
            seqs = [event.seq for event in rows]
            if len(seqs) != len(set(seqs)):
                skipped.append(f"{sid}:duplicate_seq")
                continue
            if any(
                current != previous + 1
                for previous, current in zip(seqs, seqs[1:])
            ):
                skipped.append(f"{sid}:seq_gap")
                continue
            try:
                raw_dropped = int(raw_session.get("dropped_event_count", 0) or 0)
                raw_next_seq = int(
                    raw_session.get("next_seq", max(seqs, default=-1) + 1) or 0
                )
            except (TypeError, ValueError, OverflowError):
                skipped.append(f"{sid}:metadata_invalid")
                continue
            if raw_dropped < 0 or raw_next_seq < 0:
                skipped.append(f"{sid}:metadata_invalid")
                continue
            if len(rows) > self._max_events:
                overflow = len(rows) - self._max_events
                rows = rows[-self._max_events:]
            else:
                overflow = 0
            max_seq = max((event.seq for event in rows), default=-1)
            pending[sid] = (
                rows,
                max(raw_next_seq, max_seq + 1),
                raw_dropped + overflow,
                seen,
            )
            restored_sessions.append(sid)
            restored_events += len(rows)

        with self._lock:
            for sid, (rows, next_seq, dropped, seen) in pending.items():
                self._events[sid] = rows
                self._next_seq[sid] = next_seq
                self._dropped[sid] = dropped
                self._seen_ids[sid] = seen
        return TraceRestoreReport(
            restored_sessions=tuple(restored_sessions),
            restored_events=restored_events,
            skipped_entries=tuple(skipped),
        )

    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._events.clear()
                self._dropped.clear()
                self._seen_ids.clear()
                return
            sid = str(session_id)
            self._events.pop(sid, None)
            self._dropped.pop(sid, None)
            self._seen_ids.pop(sid, None)


def project_trace_events(
    *,
    session_id: str,
    run_id: str,
    events: Iterable[TraceEvent],
    dropped_event_count: int = 0,
) -> RunTraceProjection:
    """Pure fold of trace events into a user-facing run summary."""

    ordered = tuple(sorted(events, key=lambda event: event.seq))
    if not ordered:
        return RunTraceProjection(
            session_id=session_id,
            run_id=run_id,
            trace_truncated=dropped_event_count > 0,
            dropped_event_count=dropped_event_count,
        )

    status = ""
    phase = ""
    started_at: float | None = None
    ended_at: float | None = None
    tool_started: set[str] = set()
    tool_completed = 0
    tool_failed = 0
    artifacts = 0
    total_tokens = 0
    last_diagnostic: dict[str, Any] = {}
    last_event = ordered[-1]
    phase_by_kind = {
        "run.context_started": "context",
        "run.context_ready": "context",
        "run.plan_started": "planning",
        "run.plan_ready": "planning",
        "run.tool_started": "tool",
        "run.tool_progress": "tool",
        "run.tool_finished": "tool",
        "run.verification_started": "verification",
        "run.verification_finished": "verification",
        "node.queued": "execution",
        "node.started": "execution",
        "node.progress": "execution",
        "node.waiting_input": "waiting_input",
    }
    status_by_kind = {
        "run.created": "queued",
        "run.waiting_user_input": "waiting_input",
        "run.waiting_approval": "waiting_approval",
        "run.completed": "succeeded",
        "run.failed": "failed",
        "run.cancelled": "cancelled",
        "run.cancel_requested": "cancelling",
        "run.state_changed": "",
    }

    for event in ordered:
        kind = event.kind
        payload = event.payload
        if kind in phase_by_kind:
            phase = phase_by_kind[kind]
        if kind in status_by_kind:
            mapped = str(payload.get("status", "")) if kind == "run.state_changed" else status_by_kind[kind]
            if mapped:
                status = mapped
        if kind in ("run.created", "run.context_started") and started_at is None:
            started_at = event.timestamp
        if kind in {
            "run.completed", "run.failed", "run.cancelled", "run.state_changed",
        }:
            candidate = str(payload.get("status", ""))
            if kind != "run.state_changed":
                candidate = status_by_kind.get(kind, "")
            if candidate in {"succeeded", "partial_success", "failed", "cancelled", "skipped"}:
                status = candidate
                ended_at = event.timestamp
        if kind == "run.tool_started":
            call_key = str(
                payload.get("step_id")
                or payload.get("call_id")
                or payload.get("tool_name")
                or event.event_id
            )
            tool_started.add(call_key)
        elif kind == "run.tool_finished":
            call_key = str(
                payload.get("step_id")
                or payload.get("call_id")
                or payload.get("tool_name")
                or ""
            )
            if call_key:
                tool_started.discard(call_key)
            tool_completed += 1
            if str(payload.get("outcome", "success")) not in {"success", "succeeded", "ok"}:
                tool_failed += 1
        elif kind in {"artifact.staged", "artifact.produced"}:
            artifacts += 1
        elif kind == "run.usage_recorded":
            usage = (
                payload.get("usage")
                if isinstance(payload.get("usage"), Mapping)
                else payload
            )
            for key in ("total_tokens", "tokens", "total"):
                try:
                    if key in usage:
                        total_tokens += max(0, int(usage.get(key, 0) or 0))
                        break
                except (TypeError, ValueError):
                    break
        elif kind == "cost.recorded":
            # Cost is intentionally not mixed into token count; the event is
            # retained for future projections without inventing a unit.
            pass
        is_failed_tool = (
            kind == "run.tool_finished"
            and str(payload.get("outcome", "success"))
            not in {"success", "succeeded", "ok"}
        )
        if kind in {"diagnostic", "run.failed"} or is_failed_tool:
            candidate = payload.get("diagnostic", payload)
            if isinstance(candidate, Mapping):
                last_diagnostic = dict(candidate)

    return RunTraceProjection(
        session_id=session_id,
        run_id=run_id,
        as_of_seq=last_event.seq,
        event_count=len(ordered),
        last_event_kind=last_event.kind,
        status=status,
        phase=phase,
        started_at=started_at,
        ended_at=ended_at,
        tool_call_count=tool_completed + len(tool_started),
        tool_completed_count=tool_completed,
        tool_failed_count=tool_failed,
        pending_tool_calls=tuple(sorted(tool_started)),
        artifact_count=artifacts,
        total_tokens=total_tokens,
        last_diagnostic=_bounded_trace_value(last_diagnostic),
        trace_truncated=dropped_event_count > 0,
        dropped_event_count=dropped_event_count,
    )


__all__ = [
    "TRACE_SCHEMA_VERSION",
    "RunTraceProjection",
    "RunTraceStore",
    "TraceEvent",
    "TraceRestoreReport",
    "project_trace_events",
]
