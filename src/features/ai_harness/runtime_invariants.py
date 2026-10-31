"""Installable runtime invariants for AgentRun event streams.

The checks are intentionally diagnostic rather than state-mutating. A host can
install the engine in strict mode during tests, or keep it observational in a
production editor and surface violations in the Run Drawer diagnostics.

Strict-mode note (AUD-22315): the engine is wired through
``orchestrator.subscribe`` and ``AgentOrchestrator._notify_all`` deliberately
isolates subscriber exceptions (``except Exception: pass``).  A ``RuntimeError``
raised from inside the subscription callback is therefore swallowed there.
Strict mode hence does BOTH of these things:

- records the violation and logs it at ``logging.ERROR`` with a traceback, so
  the violation is visible even when the raise is swallowed; and
- keeps the exception retrievable via :meth:`raise_if_violated` / the
  :attr:`violations` snapshot so a test host can assert after the fact::

      engine = RuntimeInvariantEngine(orchestrator, strict=True)
      engine.install()
      ...drive the orchestrator...
      engine.raise_if_violated()   # re-raises the first strict violation
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from src.features.edit.ai_editing.agent_models import AgentEventKind
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvariantViolation:
    code: str
    run_id: str
    message: str
    event_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "run_id": self.run_id,
            "message": self.message,
            "event_id": self.event_id,
        }


InvariantCheck = Callable[[Any, tuple[Any, ...]], InvariantViolation | None]

#: 终态事件种类（AUD-22318：RETRY_AFTER_TERMINAL 的增量索引依据）。
_TERMINAL_KINDS = frozenset({
    AgentEventKind.RUN_COMPLETED,
    AgentEventKind.RUN_FAILED,
    AgentEventKind.RUN_CANCELLED,
})


class RuntimeInvariantEngine:
    """Observe an orchestrator and report impossible event sequences.

    线程契约（AUD-22317）：订阅回调可能来自不同线程（orchestrator 的发布方
    决定），``_seen`` / ``_violations`` / ``_strict_errors`` 与增量索引
    （AUD-22318）统一由 ``self._lock`` 保护，避免同一违例被并发重复记录。
    """

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        *,
        strict: bool = False,
        checks: tuple[InvariantCheck, ...] = (),
    ) -> None:
        self._orchestrator = orchestrator
        self._strict = bool(strict)
        self._checks: list[InvariantCheck] = [self._check_core]
        self._checks.extend(checks)
        self._lock = threading.Lock()
        self._violations: list[InvariantViolation] = []
        self._seen: set[tuple[str, str, str]] = set()
        self._strict_errors: list[RuntimeError] = []
        # AUD-22318：按到达顺序维护的增量索引（替代每事件回放全量历史的
        # O(n²) 扫描）。run_id -> 已出现 TOOL_STARTED 的 call_id 集合；
        # 已出现终态事件的 run_id 集合。
        self._tool_starts: dict[str, set[str]] = {}
        self._terminal_runs: set[str] = set()
        self._unsubscribe: Callable[[], None] | None = None

    def install(self) -> Callable[[], None]:
        """Subscribe once and return an idempotent uninstall callback."""

        if self._unsubscribe is None:
            self._unsubscribe = self._orchestrator.subscribe(self._on_event)
        return self.uninstall

    def uninstall(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    @property
    def violations(self) -> tuple[InvariantViolation, ...]:
        with self._lock:
            return tuple(self._violations)

    def clear(self) -> None:
        with self._lock:
            self._violations.clear()
            self._seen.clear()
            self._strict_errors.clear()
            self._tool_starts.clear()
            self._terminal_runs.clear()

    def raise_if_violated(self) -> None:
        """Re-raise the first strict-mode violation, if any (AUD-22315).

        The orchestrator's subscriber dispatch swallows exceptions raised from
        subscription callbacks, so a strict ``RuntimeError`` raised inside
        ``_on_event`` never reaches the installing host.  Hosts/tests call
        this after driving the orchestrator to make strict violations
        fail loudly.
        """

        with self._lock:
            if self._strict_errors:
                raise self._strict_errors[0]

    def check_run(self, run_id: str) -> tuple[InvariantViolation, ...]:
        events = tuple(self._orchestrator.get_events(run_id))
        found: list[InvariantViolation] = []
        for event in events:
            for check in self._checks:
                violation = check(event, events)
                if violation is not None:
                    found.append(violation)
        return tuple(found)

    def _on_event(self, event: Any) -> None:
        # AUD-22318：核心两条检查改走 O(1) 增量索引，不再对每个事件回放
        # 全量历史（整 run 累计 O(n²)）。自定义检查的合约需要完整事件流，
        # 仅在确有自定义检查时才取一次历史快照。
        found: list[InvariantViolation] = []
        core_violation = self._check_core_indexed(event)
        if core_violation is not None:
            found.append(core_violation)
        custom_checks = self._checks[1:]
        if custom_checks:
            events = tuple(
                self._orchestrator.get_events(getattr(event, "run_id", ""))
            )
            for check in custom_checks:
                violation = check(event, events)
                if violation is not None:
                    found.append(violation)
        if not found:
            return
        with self._lock:
            fresh: list[InvariantViolation] = []
            for violation in found:
                key = (violation.run_id, violation.code, violation.event_id)
                if key in self._seen:
                    continue  # 同一违例（跨线程重复回调）只记录一次
                self._seen.add(key)
                self._violations.append(violation)
                fresh.append(violation)
        if not fresh:
            return
        if self._strict:
            first = fresh[0]
            error = RuntimeError(
                f"runtime invariant violated: {first.code}: {first.message}"
            )
            # AUD-22315: the orchestrator swallows subscriber exceptions,
            # so the raise alone would vanish.  Record + log the strict
            # error so it is visible (log) and retrievable
            # (raise_if_violated) even when the raise below is swallowed.
            with self._lock:
                self._strict_errors.append(error)
            _LOG.error(
                "runtime invariant violated (strict): %s: %s (run=%s event=%s)",
                first.code, first.message, first.run_id,
                first.event_id,
                exc_info=error,
            )
            raise error

    def _check_core_indexed(self, event: Any) -> InvariantViolation | None:
        """:meth:`_check_core` 的 O(1) 增量版（AUD-22318）。

        语义与全量扫描一致：TOOL_STARTED 集合 / 终态标记都由按到达顺序
        索引的历史事件累积而来（事件流 append-only 且订阅者按序收到）。
        """
        kind = str(getattr(event, "kind", "") or "")
        run_id = str(getattr(event, "run_id", "") or "")
        event_id = str(getattr(event, "event_id", "") or "")
        with self._lock:
            violation: InvariantViolation | None = None
            if kind == AgentEventKind.RUN_TOOL_FINISHED:
                payload = getattr(event, "payload", {}) or {}
                call_id = str(payload.get("call_id", "") or "")
                error_code = str(payload.get("error_code", "") or "")
                if (
                    call_id
                    and error_code != "TOOL_NOT_STARTED"
                    and call_id not in self._tool_starts.get(run_id, ())
                ):
                    violation = InvariantViolation(
                        "TOOL_FINISH_WITHOUT_START",
                        run_id,
                        f"tool call {call_id} finished without a matching start",
                        event_id,
                    )
            elif (
                kind == AgentEventKind.RUN_REQUEST_RETRY
                and run_id in self._terminal_runs
            ):
                violation = InvariantViolation(
                    "RETRY_AFTER_TERMINAL",
                    run_id,
                    "model request retry was emitted after the run became terminal",
                    event_id,
                )
            self._index_event_locked(event, kind, run_id)
            return violation

    def _index_event_locked(self, event: Any, kind: str, run_id: str) -> None:
        """把单个事件并入增量索引。调用方必须已持有 ``self._lock``。"""
        if kind == AgentEventKind.RUN_TOOL_STARTED:
            payload = getattr(event, "payload", {}) or {}
            call_id = str(payload.get("call_id", "") or "")
            if call_id:
                self._tool_starts.setdefault(run_id, set()).add(call_id)
        elif kind in _TERMINAL_KINDS:
            self._terminal_runs.add(run_id)

    @staticmethod
    def _check_core(event: Any, events: tuple[Any, ...]) -> InvariantViolation | None:
        run_id = str(getattr(event, "run_id", "") or "")
        event_id = str(getattr(event, "event_id", "") or "")
        kind = str(getattr(event, "kind", "") or "")
        if kind == AgentEventKind.RUN_TOOL_FINISHED:
            payload = getattr(event, "payload", {}) or {}
            call_id = str(payload.get("call_id", "") or "")
            error_code = str(payload.get("error_code", "") or "")
            if call_id and error_code != "TOOL_NOT_STARTED":
                started = any(
                    str(getattr(candidate, "kind", "")) == AgentEventKind.RUN_TOOL_STARTED
                    and str((getattr(candidate, "payload", {}) or {}).get("call_id", "")) == call_id
                    for candidate in events
                )
                if not started:
                    return InvariantViolation(
                        "TOOL_FINISH_WITHOUT_START",
                        run_id,
                        f"tool call {call_id} finished without a matching start",
                        event_id,
                    )
        if kind == AgentEventKind.RUN_REQUEST_RETRY:
            prior_terminal = any(
                str(getattr(candidate, "kind", "")) in {
                    AgentEventKind.RUN_COMPLETED,
                    AgentEventKind.RUN_FAILED,
                    AgentEventKind.RUN_CANCELLED,
                }
                for candidate in events[:-1]
            )
            if prior_terminal:
                return InvariantViolation(
                    "RETRY_AFTER_TERMINAL",
                    run_id,
                    "model request retry was emitted after the run became terminal",
                    event_id,
                )
        return None


__all__ = ["InvariantViolation", "RuntimeInvariantEngine"]
