"""Append-only session event log with validated event-boundary forks."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from src.features.ai_harness.invocation_models import sanitize_for_storage


@dataclass(frozen=True)
class SessionEvent:
    session_id: str
    seq: int
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = ""
    run_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.session_id or self.seq < 0 or not self.kind.strip():
            raise ValueError("invalid session event")
        if not self.run_id:
            object.__setattr__(self, "run_id", f"session:{self.session_id}")
        object.__setattr__(self, "payload", sanitize_for_storage(dict(self.payload or {})))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seq": self.seq,
            "event_id": self.event_id,
            "kind": self.kind,
            "run_id": self.run_id,
            "payload": sanitize_for_storage(self.payload),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class SessionFork:
    source_session_id: str
    fork_session_id: str
    source_seq: int
    event_count: int
    parent_fork_session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_session_id": self.source_session_id,
            "fork_session_id": self.fork_session_id,
            "source_seq": self.source_seq,
            "event_count": self.event_count,
            "parent_fork_session_id": self.parent_fork_session_id,
        }


class SessionForkError(ValueError):
    """Raised when a requested fork is not on a known event boundary."""


class SessionEventLog:
    """Bounded event log whose fork operation never copies live provider state.

    线程契约（AUD-22321）：append 可能来自事件发布线程，fork / events 可能
    来自宿主（UI）线程；内部由 ``threading.Lock`` 保护——append 的 seq
    计算（读 rows[-1] 再 +1）是 check-then-act，无锁时两个线程可分出相同
    seq；fork 的边界校验与拷贝也按同一锁序列化。
    """

    def __init__(self, *, max_events_per_session: int = 4000) -> None:
        if max_events_per_session <= 0:
            raise ValueError("max_events_per_session must be positive")
        self._max_events = int(max_events_per_session)
        self._lock = threading.Lock()
        self._events: dict[str, list[SessionEvent]] = {}
        self._forks: dict[str, SessionFork] = {}

    def append(
        self,
        session_id: str,
        kind: str,
        *,
        run_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> SessionEvent:
        session_id = str(session_id or "").strip()
        # SessionEvent.__post_init__ 会调用 sanitize_for_storage（纯函数、
        # 无锁需求）；seq 计算与追加必须在锁内原子完成。
        with self._lock:
            rows = self._events.setdefault(session_id, [])
            event = SessionEvent(
                session_id=session_id,
                seq=rows[-1].seq + 1 if rows else 0,
                kind=str(kind),
                run_id=run_id,
                payload=dict(payload or {}),
            )
            rows.append(event)
            if len(rows) > self._max_events:
                del rows[: len(rows) - self._max_events]
        return event

    def events(self, session_id: str, *, through_seq: int | None = None) -> tuple[SessionEvent, ...]:
        with self._lock:
            rows = tuple(self._events.get(str(session_id), ()))
        if through_seq is None:
            return rows
        return tuple(event for event in rows if event.seq <= int(through_seq))

    def fork(
        self,
        source_session_id: str,
        *,
        at_seq: int,
        fork_session_id: str | None = None,
    ) -> SessionFork:
        source_session_id = str(source_session_id or "").strip()
        with self._lock:
            source = list(self._events.get(source_session_id, ()))
            boundary = int(at_seq)
            valid_sequences = {event.seq for event in source}
            if boundary >= 0 and boundary not in valid_sequences:
                raise SessionForkError("fork must target an existing event boundary")
            if boundary < -1:
                raise SessionForkError("fork sequence cannot be below -1")
            child_id = str(fork_session_id or uuid.uuid4().hex).strip()
            if not child_id or child_id in self._events:
                raise SessionForkError("fork session id already exists or is empty")
            copied = [
                replace(event, session_id=child_id, event_id=uuid.uuid4().hex)
                for event in source if event.seq <= boundary
            ]
            self._events[child_id] = copied
            fork = SessionFork(
                source_session_id=source_session_id,
                fork_session_id=child_id,
                source_seq=boundary,
                event_count=len(copied),
            )
            self._forks[child_id] = fork
            return fork

    def fork_info(self, session_id: str) -> SessionFork | None:
        with self._lock:
            return self._forks.get(str(session_id))


__all__ = ["SessionEvent", "SessionEventLog", "SessionFork", "SessionForkError"]
