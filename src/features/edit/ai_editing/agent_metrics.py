"""Agent run quality metrics (plan §A7 §6 / §9 performance gates).

A passive subscriber that watches the :class:`AgentOrchestrator` event stream
and records the performance / quality signals the release gate depends on:

- **Latency**: time-to-first-event and time-to-first-token (plan §9:
  "≤ 200 ms to first visible phase").
- **Duration**: total run wall time.
- **Cancel responsiveness**: cancel_requested → cancelled gap (plan §9:
  "≤ 200 ms UI response").
- **Outcome mix**: succeeded / partial / failed / cancelled counts.
- **Activity**: tool-call count, artifact count, compression count.

The collector never blocks the orchestrator (it only reads immutable events)
and stores no secrets — only timestamps, counts and the terminal state.

Usage::

    collector = AgentMetricsCollector()
    unsubscribe = collector.attach(orchestrator)
    ...
    run_metrics = collector.get_metrics(run_id)
    summary = collector.summary()
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator


@dataclass(frozen=True)
class RunMetrics:
    """Quality metrics for one finished (or in-flight) run."""

    run_id: str
    outcome: str = ""  # terminal state, "" if still running
    first_event_latency_ms: float | None = None
    first_token_latency_ms: float | None = None
    duration_ms: float | None = None
    cancel_latency_ms: float | None = None
    tool_call_count: int = 0
    artifact_count: int = 0
    compression_count: int = 0

    @property
    def is_terminal(self) -> bool:
        return bool(self.outcome) and self.outcome in AgentRunState.TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "first_event_latency_ms": self.first_event_latency_ms,
            "first_token_latency_ms": self.first_token_latency_ms,
            "duration_ms": self.duration_ms,
            "cancel_latency_ms": self.cancel_latency_ms,
            "tool_call_count": self.tool_call_count,
            "artifact_count": self.artifact_count,
            "compression_count": self.compression_count,
        }


@dataclass
class _RunTracker:
    """Mutable per-run accumulator (internal)."""

    run_id: str
    start_time: float
    first_event_time: float | None = None
    first_token_time: float | None = None
    cancel_request_time: float | None = None
    terminal_time: float | None = None
    outcome: str = ""
    tool_call_count: int = 0
    artifact_count: int = 0
    compression_count: int = 0


class AgentMetricsCollector:
    """Passive quality-metrics subscriber for the unified AgentRun (plan §A7 §6)."""

    # AUD-12704：终态 tracker 的默认保留上限。超过后按 terminal_time 淘汰最旧的
    # 终态 tracker（进行中的 run 永不淘汰），避免长会话中随进程生命周期无界增长。
    DEFAULT_MAX_TERMINAL_TRACKERS = 512

    def __init__(
        self, *, time_fn: Callable[[], float] | None = None,
        max_terminal_trackers: int = DEFAULT_MAX_TERMINAL_TRACKERS,
    ) -> None:
        self._time = time_fn or time.monotonic
        self._trackers: dict[str, _RunTracker] = {}
        if int(max_terminal_trackers) <= 0:
            raise ValueError("max_terminal_trackers must be positive")
        self._max_terminal_trackers = int(max_terminal_trackers)
        # AUD-12702：_on_event 在 orchestrator worker 线程回调插入/更新
        # tracker，而查询接口（get_metrics/all_metrics/summary）在 UI 线程
        # 调用——无锁并发可致 "dictionary changed size during iteration" 与
        # 计数丢失。一把轻量锁覆盖全部读写（操作极短，不阻塞 orchestrator）。
        self._lock = threading.RLock()

    # ---------------- attachment ---------------- #

    def attach(self, orchestrator: AgentOrchestrator) -> Callable[[], None]:
        """Subscribe to *orchestrator*; returns an unsubscribe callable."""

        return orchestrator.subscribe(self._on_event)

    def record_first_token(self, run_id: str) -> None:
        """Mark the first streamed token for *run_id* (called by the controller).

        Stream deltas are not orchestrator events, so the controller calls this
        on the first ``stream_delta`` signal to populate ``first_token_latency``.
        """

        with self._lock:
            tracker = self._trackers.get(run_id)
            if tracker is not None and tracker.first_token_time is None:
                tracker.first_token_time = self._time()

    def prune(self, keep_last_n: int | None = None) -> int:
        """Evict the oldest *terminal* trackers (AUD-12704).

        Only trackers that reached a terminal outcome are removable; in-flight
        runs are never evicted.  Called automatically when a new tracker is
        created and can also be invoked by the host to shrink further.

        Args:
            keep_last_n: keep at most this many terminal trackers; ``None``
                uses the collector's ``max_terminal_trackers`` bound.

        Returns:
            Number of trackers evicted.
        """

        if keep_last_n is not None and int(keep_last_n) < 0:
            raise ValueError("keep_last_n cannot be negative")
        with self._lock:
            return self._prune_locked(keep_last_n)

    def _prune_locked(self, keep_last_n: int | None) -> int:
        limit = (
            self._max_terminal_trackers
            if keep_last_n is None
            else int(keep_last_n)
        )
        terminal: list[tuple[float, str]] = [
            (tracker.terminal_time or 0.0, run_id)
            for run_id, tracker in self._trackers.items()
            if tracker.outcome
        ]
        excess = len(terminal) - limit
        if excess <= 0:
            return 0
        terminal.sort()
        for _, run_id in terminal[:excess]:
            self._trackers.pop(run_id, None)
        return excess

    # ---------------- queries ---------------- #

    def get_metrics(self, run_id: str) -> RunMetrics | None:
        with self._lock:
            tracker = self._trackers.get(run_id)
            if tracker is None:
                return None
            return self._build_metrics(tracker)

    def all_metrics(self) -> tuple[RunMetrics, ...]:
        with self._lock:
            # 快照后构建：迭代期间不受并发 _on_event 插入影响
            trackers = list(self._trackers.values())
            return tuple(self._build_metrics(t) for t in trackers)

    def summary(self) -> dict[str, Any]:
        """Aggregate metrics across all observed runs (plan §A7 §6 release gate)."""

        with self._lock:
            runs = list(self._trackers.values())
        if not runs:
            return {"total_runs": 0}
        terminal = [t for t in runs if t.outcome]
        durations = [t.terminal_time - t.start_time for t in terminal if t.terminal_time]
        token_latencies = [
            t.first_token_time - t.start_time
            for t in runs
            if t.first_token_time is not None
        ]
        cancel_latencies = [
            t.terminal_time - t.cancel_request_time
            for t in terminal
            if t.cancel_request_time is not None and t.terminal_time is not None
        ]
        outcome_counts: dict[str, int] = {}
        for t in terminal:
            outcome_counts[t.outcome] = outcome_counts.get(t.outcome, 0) + 1
        return {
            "total_runs": len(runs),
            "terminal_runs": len(terminal),
            "outcomes": outcome_counts,
            "avg_duration_ms": _avg_ms(durations),
            "avg_first_token_latency_ms": _avg_ms(token_latencies),
            "avg_cancel_latency_ms": _avg_ms(cancel_latencies),
            "total_tool_calls": sum(t.tool_call_count for t in runs),
            "total_artifacts": sum(t.artifact_count for t in runs),
        }

    # ---------------- internals ---------------- #

    def _on_event(self, event: Any) -> None:
        kind = getattr(event, "kind", "")
        run_id = getattr(event, "run_id", "")
        if not run_id:
            return
        with self._lock:
            now = self._time()
            tracker = self._trackers.get(run_id)
            if tracker is None:
                # First sighting: create a tracker anchored at now. run.created is
                # the canonical start, but any event for an unseen run is treated
                # as the start so a missed created event still yields metrics.
                tracker = _RunTracker(run_id=run_id, start_time=now)
                self._trackers[run_id] = tracker
                # AUD-12704：新 run 到来时淘汰最旧的终态 tracker，防止无界增长。
                self._prune_locked(None)
            if tracker.first_event_time is None:
                tracker.first_event_time = now

            if kind == AgentEventKind.RUN_TOOL_STARTED:
                tracker.tool_call_count += 1
            elif kind == AgentEventKind.ARTIFACT_STAGED:
                tracker.artifact_count += 1
            elif kind == AgentEventKind.RUN_CHECKPOINT_SAVED:
                # Usage-recorded events carry compression_count in A4; checkpoint
                # events are a reasonable proxy for compression activity.
                tracker.compression_count += 1
            elif kind == AgentEventKind.RUN_CANCEL_REQUESTED:
                tracker.cancel_request_time = now
            elif kind == AgentEventKind.RUN_CANCELLED:
                tracker.terminal_time = now
                tracker.outcome = AgentRunState.CANCELLED
                # AUD-12704：进入终态后立即执行淘汰，保证上限即时成立。
                self._prune_locked(None)
            elif kind == AgentEventKind.RUN_COMPLETED:
                tracker.terminal_time = now
                # Distinguish partial from full success via the payload if present.
                payload = getattr(event, "payload", {}) or {}
                tracker.outcome = (
                    AgentRunState.PARTIAL_SUCCESS
                    if payload.get("partial")
                    else AgentRunState.SUCCEEDED
                )
                self._prune_locked(None)
            elif kind == AgentEventKind.RUN_FAILED:
                tracker.terminal_time = now
                tracker.outcome = AgentRunState.FAILED
                self._prune_locked(None)

    @staticmethod
    def _build_metrics(tracker: _RunTracker) -> RunMetrics:
        duration = None
        if tracker.terminal_time is not None:
            duration = (tracker.terminal_time - tracker.start_time) * 1000.0
        first_event = None
        if tracker.first_event_time is not None:
            first_event = (tracker.first_event_time - tracker.start_time) * 1000.0
        first_token = None
        if tracker.first_token_time is not None:
            first_token = (tracker.first_token_time - tracker.start_time) * 1000.0
        cancel = None
        if (
            tracker.cancel_request_time is not None
            and tracker.terminal_time is not None
        ):
            cancel = (tracker.terminal_time - tracker.cancel_request_time) * 1000.0
        return RunMetrics(
            run_id=tracker.run_id,
            outcome=tracker.outcome,
            first_event_latency_ms=first_event,
            first_token_latency_ms=first_token,
            duration_ms=duration,
            cancel_latency_ms=cancel,
            tool_call_count=tracker.tool_call_count,
            artifact_count=tracker.artifact_count,
            compression_count=tracker.compression_count,
        )


def _avg_ms(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values) * 1000.0, 1)


__all__ = [
    "AgentMetricsCollector",
    "RunMetrics",
]
