"""Controlled tool execution bridge for the unified AgentRun (plan §A2).

Wraps the existing :class:`ToolFacade` so every tool invocation that flows
through the agent loop becomes:

1. An observable pair of events (``run.tool_started`` / ``run.tool_finished``)
   on the :class:`AgentOrchestrator`.
2. A structured :class:`ToolResult` (success / partial / failure / denied)
   instead of a bare ``dict``, so the loop, the Run Drawer and the audit log
   share one shape.

The bridge intentionally does **not** re-implement the approval gate —
``ToolFacade`` already enforces it (a write tool with ``approval=None`` returns
a ``PERMISSION_DENIED`` diagnostic, and model-supplied tokens are ignored).
This layer only *recognises* that denial and surfaces it as
``ToolOutcome.DENIED`` so retry logic can tell "the tool broke" from "the host
said no" (plan §A2 §8).

Hard rules:

- **Never auto-approve.**  The caller must pass the host-issued grant; a
  missing grant for a write tool produces a DENIED result, never a write.
- **Audit without secrets.**  Event payloads carry only the tool name + outcome
  + duration; the full result dict stays on the ``ToolResult`` and is redacted
  by its own ``to_dict``.
- **Exception-safe.**  A throwing tool becomes a FAILURE result, never crashing
  the run.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import inspect
from typing import Any, Protocol

from src.features.edit.ai_editing.agent_models import AgentEventKind
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.capability_catalog import CapabilityCatalog
from src.features.edit.ai_editing.tool_facade import (
    PARALLEL_SAFE_READ_TOOLS,
    ToolDefinition,
    ToolFacade,
    ToolExecutionMode,
    ToolPermission,
    list_tool_definitions,
)
from src.features.edit.ai_editing.tool_result import (
    Diagnostic,
    ToolOutcome,
    ToolResult,
    permission_requires_approval,
)
from src.features.edit.ai_editing.tool_operation_ledger import (
    LedgerDecision,
    ToolOperationLedger,
    ToolOperationRecord,
)


#: Diagnostic codes that mean "permission / approval denied" (not a crash).
_PERMISSION_DENIED_CODES: frozenset[str] = frozenset(
    {"PERMISSION_DENIED", "TAEP_PERMISSION_DENIED"}
)


# ---------------------------------------------------------------------- #
# dict -> ToolResult conversion
# ---------------------------------------------------------------------- #


def _extract_diagnostics(result_dict: dict[str, Any]) -> tuple[Diagnostic, ...]:
    raw = result_dict.get("diagnostics") or []
    out: list[Diagnostic] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(
                Diagnostic(
                    severity=str(item.get("severity", "info")).lower(),
                    code=str(item.get("code", "")),
                    message=str(item.get("reason") or item.get("message") or ""),
                )
            )
    return tuple(out)


def _first_diagnostic_message(diagnostics: tuple[Diagnostic, ...]) -> str:
    for diag in diagnostics:
        if diag.message:
            return diag.message
    return ""


def _has_permission_denied(diagnostics: tuple[Diagnostic, ...]) -> bool:
    return any(diag.code in _PERMISSION_DENIED_CODES for diag in diagnostics)


def result_from_facade(
    tool_name: str,
    result_dict: dict[str, Any],
    *,
    duration_ms: float = 0.0,
) -> ToolResult:
    """Convert a ``ToolFacade.invoke`` dict into a structured :class:`ToolResult`.

    Outcome contract (AUD-14725): an **absent** ``"ok"`` key means success —
    the facade's read tools return payload-only dicts (``{"manifest": ...}``,
    ``{"snapshot": ...}``, ...) and only set ``"ok"`` when there is something
    to flag.  Treating a missing key as failure misreported every such call
    as "工具调用失败" and dropped the payload.  Only an explicit
    ``ok=False`` (or an error diagnostic) is a failure/denial.
    """

    ok = bool(result_dict.get("ok", True))
    diagnostics = _extract_diagnostics(result_dict)
    if ok:
        return ToolResult.success(
            tool_name,
            data=result_dict,
            diagnostics=diagnostics,
            duration_ms=duration_ms,
        )
    # Not ok: distinguish denial from a generic failure.
    if _has_permission_denied(diagnostics):
        reason = _first_diagnostic_message(diagnostics) or "工具调用未被授权"
        return ToolResult.denied(tool_name, reason=reason)
    error_msg = (
        result_dict.get("error")
        or _first_diagnostic_message(diagnostics)
        or "工具调用失败"
    )
    return ToolResult.failure(
        tool_name,
        error_message=str(error_msg),
        diagnostics=diagnostics,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------- #
# Tool metadata helpers
# ---------------------------------------------------------------------- #


def find_tool_definition(tool_name: str) -> ToolDefinition | None:
    """Look up a tool's definition by name (None if unregistered)."""

    for defn in list_tool_definitions():
        if defn.name == tool_name:
            return defn
    return None


def tool_requires_approval(tool_name: str) -> bool:
    """Whether *tool_name* needs a host-issued approval grant before execution.

    An unknown tool returns ``False`` (it does not need approval; it will simply
    fail as unknown when invoked).
    """

    defn = find_tool_definition(tool_name)
    if defn is None:
        return False
    return permission_requires_approval(defn.permission)


def tool_execution_mode(tool_name: str) -> ToolExecutionMode:
    """Return a tool's declared scheduler mode, failing closed to exclusive."""

    defn = find_tool_definition(tool_name)
    if (
        defn is None
        or defn.permission != ToolPermission.READ
        or tool_name.startswith("topos.mcp.")
        or tool_name not in PARALLEL_SAFE_READ_TOOLS
    ):
        return ToolExecutionMode.EXCLUSIVE
    return defn.execution_mode


# ---------------------------------------------------------------------- #
# Execution middleware
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolExecutionContext:
    """Immutable context shared by one tool call's middleware chain.

    The context deliberately contains the host-issued approval object only for
    policy middleware. It is never included in an event payload or sent to the
    model.
    """

    run_id: str
    tool_name: str
    args: Mapping[str, Any]
    approval: Any = None
    call_id: str = ""


@dataclass(frozen=True)
class ToolPreflight:
    """Decision returned by a pre-execution middleware.

    ``None`` means "no decision" and lets the next middleware (or execution)
    continue. A middleware can only stop execution with ``allowed=False``;
    this avoids an accidentally returned successful ``ToolResult`` bypassing
    the facade and its approval gate.
    """

    allowed: bool = True
    outcome: str = ToolOutcome.DENIED
    error_code: str = "TOOL_PRECHECK_FAILED"
    error_message: str = ""
    diagnostics: tuple[Diagnostic, ...] = ()

    @staticmethod
    def allow() -> "ToolPreflight":
        return ToolPreflight(allowed=True)

    @staticmethod
    def deny(
        reason: str,
        *,
        error_code: str = "TOOL_PRECHECK_DENIED",
        diagnostics: tuple[Diagnostic, ...] = (),
    ) -> "ToolPreflight":
        return ToolPreflight(
            allowed=False,
            outcome=ToolOutcome.DENIED,
            error_code=error_code,
            error_message=reason,
            diagnostics=diagnostics,
        )

    @staticmethod
    def fail(
        reason: str,
        *,
        error_code: str = "TOOL_PRECHECK_FAILED",
        diagnostics: tuple[Diagnostic, ...] = (),
    ) -> "ToolPreflight":
        return ToolPreflight(
            allowed=False,
            outcome=ToolOutcome.FAILURE,
            error_code=error_code,
            error_message=reason,
            diagnostics=diagnostics,
        )


class ToolExecutionMiddleware(Protocol):
    """Optional ``pre → execute → post`` hook contract for tool calls."""

    def pre_execute(self, context: ToolExecutionContext) -> ToolPreflight | None:
        ...

    def post_execute(
        self, context: ToolExecutionContext, result: ToolResult
    ) -> ToolResult:
        ...


class ToolInputValidationMiddleware:
    """Fail closed on malformed direct runner input before touching the facade."""

    def pre_execute(self, context: ToolExecutionContext) -> ToolPreflight | None:
        if not context.tool_name.strip():
            return ToolPreflight.fail(
                "tool name cannot be empty",
                error_code="TOOL_INVALID_NAME",
            )
        if not isinstance(context.args, Mapping):
            return ToolPreflight.fail(
                "tool arguments must be an object",
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        return None

    def post_execute(
        self, context: ToolExecutionContext, result: ToolResult
    ) -> ToolResult:
        return result


@dataclass(frozen=True)
class ToolTimeoutPolicy:
    """Per-tool cooperative deadline policy.

    A facade that accepts ``timeout_seconds`` or ``cancel_check`` can stop
    early.  Legacy facades are still drained to quiescence before the deadline
    is evaluated, so Topos never returns while an unknown side effect is still
    running in the background.

    Reporting rule (AUD-14726): a call that **completes** after the deadline
    keeps its real, confirmed outcome — the side effect already happened, and
    reclassifying a confirmed success as ``TOOL_TIMEOUT`` would invite retry
    and duplicate submits.  The overrun is surfaced as a ``TOOL_TIMEOUT``
    warning diagnostic on the result instead.  Only a call whose outcome is
    *unknown* (an exception observed past the deadline) is reported as a
    ``TOOL_TIMEOUT`` failure.
    """

    default_timeout_seconds: float | None = 120.0
    per_tool_seconds: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        if self.default_timeout_seconds is not None and self.default_timeout_seconds <= 0:
            raise ValueError("default tool timeout must be positive")
        for name, value in (self.per_tool_seconds or {}).items():
            if not str(name).strip() or value <= 0:
                raise ValueError("per-tool timeout values must be positive")

    def timeout_for(self, tool_name: str) -> float | None:
        overrides = self.per_tool_seconds or {}
        return overrides.get(tool_name, self.default_timeout_seconds)


# ---------------------------------------------------------------------- #
# ToolInvocationRunner
# ---------------------------------------------------------------------- #


class ToolInvocationRunner:
    """Execute one tool call through ToolFacade with events + structured result."""

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        tool_facade: ToolFacade,
        *,
        time_fn: Any = None,
        middlewares: Iterable[ToolExecutionMiddleware] | None = None,
        timeout_policy: ToolTimeoutPolicy | None = None,
        operation_ledger: ToolOperationLedger | None = None,
        capability_catalog: CapabilityCatalog | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._facade = tool_facade
        self._time = time_fn or time.time
        # A tuple makes the chain immutable and safe to share across the
        # parallel read-only calls scheduled by AgentToolLoop.
        self._middlewares = tuple(middlewares or ())
        self._timeout_policy = timeout_policy or ToolTimeoutPolicy()
        self._operation_ledger = operation_ledger
        self._capability_catalog = capability_catalog or getattr(
            tool_facade, "capability_catalog", None,
        )

    def execution_mode(self, tool_name: str) -> ToolExecutionMode:
        """Return the facade-owned scheduler mode, failing closed on errors."""

        try:
            mode = self._facade.execution_mode(tool_name)
        except Exception:  # noqa: BLE001 - scheduler safety takes precedence
            return ToolExecutionMode.EXCLUSIVE
        if isinstance(mode, ToolExecutionMode):
            return mode
        try:
            return ToolExecutionMode(str(mode).lower())
        except ValueError:
            return ToolExecutionMode.EXCLUSIVE

    def invoke(
        self,
        run_id: str,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        approval: Any = None,
        call_id: str = "",
        cancel_check: Any = None,
    ) -> ToolResult:
        """Run *tool_name*, emitting tool events and returning a ToolResult."""

        # Direct host callers may not have a model call id. Still assign one so
        # the event pair remains unambiguous in the durable trace.
        call_id = str(call_id or "").strip() or f"call-{uuid.uuid4().hex}"
        self._orchestrator.emit(
            run_id,
            AgentEventKind.RUN_TOOL_STARTED,
            payload=_tool_event_payload(tool_name, call_id),
        )
        started = self._time()
        context = ToolExecutionContext(
            run_id=run_id,
            tool_name=tool_name,
            args=dict(args or {}) if isinstance(args, Mapping) else (args or {}),
            approval=approval,
            call_id=call_id,
        )
        result = self._pre_execute(context, started)
        if result is None and self._capability_catalog is not None and not self._is_declared(tool_name):
            result = ToolResult.failure(
                tool_name or "unknown",
                error_code="TOOL_NOT_DECLARED",
                error_message="工具未在宿主能力目录中声明，已阻止执行",
            )
        ledger_record: ToolOperationRecord | None = None
        ledger_replayed = False
        if result is None:
            if self._operation_ledger is not None:
                admission = self._operation_ledger.begin(
                    run_id, call_id, tool_name, args,
                )
                if admission.decision == LedgerDecision.REPLAY:
                    ledger_record = admission.record
                    result = admission.record.result or ToolResult.failure(
                        tool_name,
                        error_code="TOOL_LEDGER_CORRUPT",
                        error_message="幂等账本记录缺少结果，已阻止重放",
                    )
                    ledger_replayed = True
                elif admission.decision == LedgerDecision.IN_FLIGHT:
                    result = ToolResult.failure(
                        tool_name or "unknown",
                        error_code="TOOL_OPERATION_IN_FLIGHT",
                        error_message="相同工具操作仍在执行，已阻止重复副作用",
                    )
                elif admission.decision == LedgerDecision.RECONCILE:
                    result = ToolResult.failure(
                        tool_name or "unknown",
                        error_code="TOOL_OPERATION_RECONCILIATION_REQUIRED",
                        error_message="上次工具操作中断，需先确认工程状态再重试",
                    )
                else:
                    ledger_record = admission.record
            if result is None:
                timeout_seconds = self._timeout_policy.timeout_for(tool_name)
                try:
                    result_dict = self._invoke_facade(
                        tool_name,
                        args,
                        approval=approval,
                        timeout_seconds=timeout_seconds,
                        cancel_check=cancel_check,
                        run_id=run_id,
                    )
                    elapsed_ms = (self._time() - started) * 1000.0
                    result = result_from_facade(tool_name, result_dict, duration_ms=elapsed_ms)
                    if (
                        timeout_seconds is not None
                        and elapsed_ms >= timeout_seconds * 1000.0
                        and not result.is_denied
                    ):
                        # AUD-14726：工具已完成且结果已确认——超时不再改判 outcome
                        # （副作用已发生，报失败会诱发重试/重复提交），降级为附加
                        # warning diagnostic 携带超时事实。结果未知的异常路径才标
                        # TOOL_TIMEOUT 失败（见下方 except 分支）。
                        result = replace(
                            result,
                            diagnostics=result.diagnostics + (Diagnostic(
                                severity="warning",
                                code="TOOL_TIMEOUT",
                                message=(
                                    f"工具执行超过 {timeout_seconds:.1f}s"
                                    "（已等待执行完全退出）；结果已确认，超时不改变本次结果"
                                ),
                            ),),
                        )
                except Exception as exc:  # noqa: BLE001 - a tool must never crash the run
                    elapsed_ms = (self._time() - started) * 1000.0
                    timed_out = (
                        timeout_seconds is not None
                        and elapsed_ms >= timeout_seconds * 1000.0
                    )
                    result = ToolResult.failure(
                        tool_name or "unknown",
                        error_message=str(exc),
                        error_code="TOOL_TIMEOUT" if timed_out else "TOOL_EXCEPTION",
                        duration_ms=elapsed_ms,
                    )
        result = self._post_execute(context, result)
        # Middleware may enrich or replace a result, but it cannot falsify the
        # measured duration or upgrade a failure/denial into success.
        result = self._with_duration(result, (self._time() - started) * 1000.0)
        if self._operation_ledger is not None and ledger_record is not None and not ledger_replayed:
            if result.error_code in {"TOOL_EXCEPTION", "TOOL_TIMEOUT"}:
                self._operation_ledger.mark_unknown(ledger_record)
            else:
                self._operation_ledger.finish(ledger_record, result)
        self._emit_finished(
            run_id, tool_name, result, call_id=call_id,
            deduplicated=ledger_replayed,
        )
        return result

    def _is_declared(self, tool_name: str) -> bool:
        """Check the catalog, with an escape hatch for explicitly registered MCP tools."""

        if self._capability_catalog.get(tool_name) is not None:
            return True
        resolver = getattr(self._facade, "is_tool_declared", None)
        if callable(resolver):
            try:
                return bool(resolver(tool_name))
            except Exception:  # noqa: BLE001 - admission fails closed
                return False
        return False

    def _invoke_facade(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
        *,
        approval: Any,
        timeout_seconds: float | None,
        cancel_check: Any,
        run_id: str,
    ) -> dict[str, Any]:
        """Pass optional cooperative controls only to facades that advertise them."""

        invoke = self._facade.invoke
        try:
            parameters = inspect.signature(invoke).parameters
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            parameters = {}
            accepts_kwargs = False
        kwargs: dict[str, Any] = {"approval": approval}
        if timeout_seconds is not None and (
            "timeout_seconds" in parameters or accepts_kwargs
        ):
            kwargs["timeout_seconds"] = timeout_seconds
        if cancel_check is not None and (
            "cancel_check" in parameters or accepts_kwargs
        ):
            kwargs["cancel_check"] = cancel_check
        if run_id and ("run_id" in parameters or accepts_kwargs):
            kwargs["run_id"] = run_id
        return invoke(tool_name, args, **kwargs)

    # ---------------- internals ---------------- #

    def _pre_execute(
        self, context: ToolExecutionContext, started: float
    ) -> ToolResult | None:
        for middleware in self._middlewares:
            hook = getattr(middleware, "pre_execute", None)
            if not callable(hook):
                continue
            try:
                decision = hook(context)
            except Exception as exc:  # noqa: BLE001 - preflight is fail-closed
                return ToolResult.failure(
                    context.tool_name or "unknown",
                    error_code="TOOL_PRECHECK_EXCEPTION",
                    error_message=str(exc),
                    duration_ms=(self._time() - started) * 1000.0,
                )
            if decision is None:
                continue
            if not isinstance(decision, ToolPreflight):
                return ToolResult.failure(
                    context.tool_name or "unknown",
                    error_code="TOOL_PRECHECK_INVALID",
                    error_message="pre_execute must return ToolPreflight or None",
                    duration_ms=(self._time() - started) * 1000.0,
                )
            if decision.allowed:
                continue
            outcome = (
                decision.outcome
                if decision.outcome in ToolOutcome.KNOWN and decision.outcome != ToolOutcome.SUCCESS
                else ToolOutcome.FAILURE
            )
            return ToolResult(
                tool_name=context.tool_name or "unknown",
                outcome=outcome,
                error_code=decision.error_code,
                error_message=decision.error_message,
                diagnostics=decision.diagnostics,
                duration_ms=(self._time() - started) * 1000.0,
            )
        return None

    def _post_execute(
        self, context: ToolExecutionContext, result: ToolResult
    ) -> ToolResult:
        current = result
        # Reverse order gives nested middleware the expected unwind semantics.
        for middleware in reversed(self._middlewares):
            hook = getattr(middleware, "post_execute", None)
            if not callable(hook):
                continue
            try:
                transformed = hook(context, current)
            except Exception as exc:  # noqa: BLE001 - postflight is fail-closed
                if current.is_denied:
                    return current
                return ToolResult.failure(
                    context.tool_name or "unknown",
                    error_code="TOOL_POSTPROCESS_EXCEPTION",
                    error_message=str(exc),
                    diagnostics=current.diagnostics,
                    duration_ms=current.duration_ms,
                )
            if not isinstance(transformed, ToolResult):
                return ToolResult.failure(
                    context.tool_name or "unknown",
                    error_code="TOOL_POSTPROCESS_INVALID",
                    error_message="post_execute must return ToolResult",
                    diagnostics=current.diagnostics,
                    duration_ms=current.duration_ms,
                )
            if transformed.tool_name != context.tool_name:
                return ToolResult.failure(
                    context.tool_name or "unknown",
                    error_code="TOOL_POSTPROCESS_INVALID",
                    error_message="post_execute cannot change tool_name",
                    diagnostics=current.diagnostics,
                    duration_ms=current.duration_ms,
                )
            # A post hook may fail closed, but must not turn a failed/denied
            # execution into partial/success and thereby hide an approval or
            # tool error from the model.
            if (
                current.outcome in ToolOutcome.FAILURE_LIKE
                and transformed.outcome not in ToolOutcome.FAILURE_LIKE
            ):
                continue
            current = transformed
        return current

    @staticmethod
    def _with_duration(result: ToolResult, duration_ms: float) -> ToolResult:
        return ToolResult(
            tool_name=result.tool_name,
            outcome=result.outcome,
            data=result.data,
            diagnostics=result.diagnostics,
            artifact_refs=result.artifact_refs,
            error_code=result.error_code,
            error_message=result.error_message,
            duration_ms=duration_ms,
        )

    def _emit_finished(
        self, run_id: str, tool_name: str, result: ToolResult, *, call_id: str = "",
        deduplicated: bool = False,
    ) -> None:
        payload = _tool_event_payload(
            tool_name,
            call_id,
            outcome=result.outcome,
            duration_ms=round(result.duration_ms, 1),
            error_code=result.error_code,
            error_message=result.error_message,
            diagnostics=[diagnostic.to_dict() for diagnostic in result.diagnostics],
            deduplicated=deduplicated,
        )
        self._orchestrator.emit(
            run_id,
            AgentEventKind.RUN_TOOL_FINISHED,
            payload=payload,
        )

    def record_skipped(
        self,
        run_id: str,
        tool_name: str,
        *,
        call_id: str = "",
        reason: str = "tool call was not started",
    ) -> ToolResult:
        """Close a model-admitted call that cancellation/budget skipped.

        No facade call is made. The synthetic result gives replay and the
        model-facing protocol a terminal outcome without pretending that the
        tool ran.
        """

        call_id = str(call_id or "").strip() or f"call-{uuid.uuid4().hex}"
        result = ToolResult.failure(
            tool_name or "unknown",
            error_code="TOOL_NOT_STARTED",
            error_message=reason,
        )
        self._emit_finished(run_id, tool_name or "unknown", result, call_id=call_id)
        return result


def _tool_event_payload(
    tool_name: str,
    call_id: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """Build a stable, metadata-only tool call/result event payload."""

    payload: dict[str, Any] = {"tool_name": tool_name}
    if call_id:
        payload["call_id"] = call_id
    payload.update({key: value for key, value in fields.items() if value not in (None, "", [])})
    return payload


__all__ = [
    "ToolExecutionContext",
    "ToolExecutionMiddleware",
    "ToolInputValidationMiddleware",
    "ToolInvocationRunner",
    "ToolPreflight",
    "ToolTimeoutPolicy",
    "find_tool_definition",
    "result_from_facade",
    "tool_execution_mode",
    "tool_requires_approval",
]
