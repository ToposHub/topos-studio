"""Model-driven tool loop for the unified AgentRun (plan §A2).

When the active profile speaks the tool-calling protocol, the agent can do
more than answer text: it observes, calls ``topos.*`` tools through the
controlled :class:`ToolInvocationRunner`, feeds the (redacted, untrusted)
results back to the model and loops until it produces a final answer or the
budget runs out.

This is the AgentRun-native counterpart of ``run_tool_loop``: same messages
contract (OpenAI-style ``role=tool`` / ``tool_call_id``) and budget semantics,
but every tool execution goes through :class:`ToolInvocationRunner` so it
becomes a structured :class:`ToolResult`, emits ``run.tool_started`` /
``run.tool_finished`` events, and respects the approval gate (a write tool
without a host grant is DENIED, not silently executed).

Hard rules (plan §A2 §10 / §1.2 #2):

- **Bounded.** ``max_iterations`` / ``max_tool_calls`` terminate the loop with
  a readable reason; no unbounded self-reflection.
- **Untrusted tool output.** Tool results are JSON-serialised before being fed
  back; the model cannot use them to alter system instructions.
- **Approval never from the model.** Write tools are DENIED without a host
  grant; the loop reports that as a tool result, not a crash.
- **Cancel-aware.** A ``cancel_check`` callable is polled before each model
  call and each tool call.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from src.features.edit.ai_editing.agent_inbox import AgentInbox
from src.features.edit.ai_editing.tool_facade import ToolExecutionMode
from src.features.edit.ai_editing.tool_invocation import ToolInvocationRunner
from src.features.edit.ai_editing.request_recovery import ModelRequestRecovery
from src.features.edit.ai_editing.tool_result_pruner import ToolResultPruner
from src.features.edit.ai_editing.tool_result import ToolResult


class _ToolCallingClient(Protocol):
    """Subset of ToolCallingClient needed by the loop (structural typing)."""

    def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = ...,
        max_tokens: int | None = ...,
    ) -> Any: ...


#: Cancel predicate: return True to stop the loop at the next checkpoint.
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class AgentToolLoopBudget:
    """Hard limits that prevent unbounded tool use (plan §A2 §10)."""

    max_iterations: int = 8
    max_tool_calls: int = 16
    max_parallel_tool_calls: int = 4

    def __post_init__(self) -> None:
        if (
            self.max_iterations <= 0
            or self.max_tool_calls <= 0
            or self.max_parallel_tool_calls <= 0
        ):
            raise ValueError("tool loop budget must be positive")


# Termination reasons.
_CONVERGED = "converged"
_MAX_ITERATIONS = "max_iterations"
_MAX_TOOL_CALLS = "max_tool_calls"
_ERROR = "error"
_CANCELLED = "cancelled"


@dataclass(frozen=True)
class AgentToolLoopResult:
    """Outcome of one model-driven tool loop."""

    final_text: str
    iterations: int = 0
    tool_calls_made: int = 0
    tool_results: tuple[ToolResult, ...] = ()
    terminated_reason: str = _CONVERGED
    error: str = ""

    @property
    def converged(self) -> bool:
        return self.terminated_reason == _CONVERGED

    @property
    def cancelled(self) -> bool:
        return self.terminated_reason == _CANCELLED

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_text_len": len(self.final_text),
            "iterations": self.iterations,
            "tool_calls_made": self.tool_calls_made,
            "tool_results": [r.to_dict() for r in self.tool_results],
            "terminated_reason": self.terminated_reason,
            "error": self.error,
        }


class AgentToolLoop:
    """Run a model-driven tool loop within an AgentRun (plan §A2).

    Consecutive tools explicitly marked ``parallel`` are dispatched through a
    bounded rolling pool. Exclusive tools form barriers, and all tool result
    messages are appended in the model's original order. This mirrors the
    useful part of DeepSeek Harness's scheduler without allowing arbitrary
    tools to become concurrent by accident.
    """

    def __init__(
        self,
        tool_runner: ToolInvocationRunner,
        *,
        budget: AgentToolLoopBudget | None = None,
        result_pruner: ToolResultPruner | None = None,
        request_recovery: ModelRequestRecovery | None = None,
        inbox: AgentInbox | None = None,
    ) -> None:
        self._runner = tool_runner
        self._budget = budget or AgentToolLoopBudget()
        self._result_pruner = result_pruner or ToolResultPruner()
        self._request_recovery = request_recovery
        self._inbox = inbox

    def run(
        self,
        run_id: str,
        client: _ToolCallingClient,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.6,
        max_tokens: int | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> AgentToolLoopResult:
        """Loop model ↔ tools until convergence or budget exhaustion."""

        local_messages = list(messages)
        advertised_tool_names = _tool_names(tools)
        iterations = 0
        tool_calls_made = 0
        all_results: list[ToolResult] = []
        final_text = ""

        while iterations < self._budget.max_iterations:
            if cancel_check is not None and cancel_check():
                return self._result(final_text, iterations, tool_calls_made, all_results, _CANCELLED)
            iterations += 1
            if self._inbox is not None:
                pre_step = self._inbox.claim(run_id)
                local_messages.extend(pre_step.to_messages())

            try:
                def request() -> Any:
                    return client.complete_with_tools(
                        local_messages,
                        tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )

                if self._request_recovery is None:
                    response = request()
                else:
                    response = self._request_recovery.call(
                        request,
                        run_id=run_id,
                        operation_name="tool_loop.model_request",
                        cancel_check=cancel_check,
                    )
            except Exception as exc:  # noqa: BLE001 - model failure must be visible
                if getattr(exc, "code", "") == "LLM_CANCELLED":
                    return self._result(
                        final_text,
                        iterations,
                        tool_calls_made,
                        all_results,
                        _CANCELLED,
                        error=str(exc),
                    )
                return self._result(
                    final_text, iterations, tool_calls_made, all_results, _ERROR, error=str(exc),
                )

            final_text = getattr(response, "text", "") or final_text
            tool_calls = getattr(response, "tool_calls", ()) or ()
            finish = bool(getattr(response, "finish", False))

            # Convergence: the model produced a final answer (no tool calls).
            if finish or not tool_calls:
                return self._result(final_text, iterations, tool_calls_made, all_results, _CONVERGED)

            call_ids = [str(getattr(tc, "call_id", "") or "").strip() for tc in tool_calls]
            if any(not call_id for call_id in call_ids):
                return self._result(
                    final_text,
                    iterations,
                    tool_calls_made,
                    all_results,
                    _ERROR,
                    error="model returned a tool call without a call_id",
                )
            if len(call_ids) != len(set(call_ids)):
                return self._result(
                    final_text,
                    iterations,
                    tool_calls_made,
                    all_results,
                    _ERROR,
                    error="model returned duplicate tool call ids",
                )

            # OpenAI protocol: assistant(tool_calls) message MUST precede the
            # role="tool" result messages.  Build the assistant envelope first.
            assistant_tool_calls: list[dict[str, Any]] = []
            for tc in tool_calls:
                tool_name = str(getattr(tc, "tool_name", ""))
                call_id = str(getattr(tc, "call_id", ""))
                arguments = dict(getattr(tc, "arguments", {}) or {})
                assistant_tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        # arguments must be a JSON *string* in the assistant
                        # tool_calls envelope (OpenAI API requirement).
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                )
            local_messages.append(
                {"role": "assistant", "content": final_text, "tool_calls": assistant_tool_calls}
            )

            # Execute tools in model order, using parallel groups only where
            # the registry explicitly allows it. Results from a completed
            # group are then fed back as role="tool" messages in model order.
            call_index = 0
            while call_index < len(tool_calls):
                if cancel_check is not None and cancel_check():
                    skipped = self._skip_calls(run_id, tuple(tool_calls[call_index:]))
                    all_results.extend(skipped)
                    return self._result(final_text, iterations, tool_calls_made, all_results, _CANCELLED)
                remaining = self._budget.max_tool_calls - tool_calls_made
                if remaining <= 0:
                    skipped = self._skip_calls(run_id, tuple(tool_calls[call_index:]))
                    all_results.extend(skipped)
                    return self._result(
                        final_text, iterations, tool_calls_made, all_results, _MAX_TOOL_CALLS,
                    )

                first_call = tool_calls[call_index]
                mode = self._execution_mode(
                    str(getattr(first_call, "tool_name", "")), advertised_tool_names
                )
                group_end = call_index + 1
                if mode == ToolExecutionMode.PARALLEL:
                    while (
                        group_end < len(tool_calls)
                        and group_end - call_index < remaining
                        and self._execution_mode(
                            str(getattr(tool_calls[group_end], "tool_name", "")),
                            advertised_tool_names,
                        ) == ToolExecutionMode.PARALLEL
                    ):
                        group_end += 1
                group = tuple(tool_calls[call_index:group_end])
                results, cancelled = self._execute_group(
                    run_id,
                    group,
                    cancel_check=cancel_check,
                    advertised_tool_names=advertised_tool_names,
                )
                all_results.extend(results)
                for tc, result in zip(group, results):
                    tool_name = str(getattr(tc, "tool_name", ""))
                    call_id = str(getattr(tc, "call_id", ""))
                    # Feed the (untrusted, redacted) tool result back to the model.
                    local_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": tool_name,
                            "content": _tool_result_to_text(result, self._result_pruner),
                        }
                    )
                tool_calls_made += sum(
                    result.error_code != "TOOL_NOT_STARTED" for result in results
                )
                call_index += len(results)
                # Already-started calls are drained and recorded before a
                # cancellation is surfaced; not-yet-started calls are skipped.
                if cancelled:
                    return self._result(final_text, iterations, tool_calls_made, all_results, _CANCELLED)
                if tool_calls_made >= self._budget.max_tool_calls:
                    return self._result(
                        final_text, iterations, tool_calls_made, all_results, _MAX_TOOL_CALLS,
                    )

        # Loop exited via while-condition: max_iterations reached without convergence.
        return self._result(final_text, iterations, tool_calls_made, all_results, _MAX_ITERATIONS)

    def _execute_group(
        self,
        run_id: str,
        tool_calls: tuple[Any, ...],
        *,
        cancel_check: CancelCheck | None,
        advertised_tool_names: frozenset[str],
    ) -> tuple[list[ToolResult], bool]:
        """Execute one exclusive call or one bounded parallel group.

        The returned list is always model-ordered, even though parallel calls
        may finish in a different order. A cancellation drains already-started
        futures and records synthetic ``TOOL_NOT_STARTED`` results for calls
        that never entered the runner.
        """

        if not tool_calls:
            return [], False
        is_parallel = (
            len(tool_calls) > 1
            and self._execution_mode(
                str(getattr(tool_calls[0], "tool_name", "")), advertised_tool_names
            )
            == ToolExecutionMode.PARALLEL
        )
        if not is_parallel:
            results: list[ToolResult] = []
            for index, tc in enumerate(tool_calls):
                if cancel_check is not None and cancel_check():
                    results.extend(self._skip_calls(run_id, tool_calls[index:]))
                    return results, True
                results.append(self._invoke_one(run_id, tc, advertised_tool_names))
            return results, bool(cancel_check is not None and cancel_check())

        max_workers = min(self._budget.max_parallel_tool_calls, len(tool_calls))
        ordered: list[ToolResult | None] = [None] * len(tool_calls)
        in_flight: dict[Future[ToolResult], int] = {}
        next_index = 0
        cancelled = False

        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="topos-ai-tool",
        ) as executor:
            while next_index < len(tool_calls) and len(in_flight) < max_workers:
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    break
                future = executor.submit(
                    self._invoke_one,
                    run_id,
                    tool_calls[next_index],
                    advertised_tool_names,
                )
                in_flight[future] = next_index
                next_index += 1

            while in_flight:
                done, _ = wait(
                    tuple(in_flight), timeout=0.05, return_when=FIRST_COMPLETED
                )
                for future in done:
                    index = in_flight.pop(future)
                    try:
                        ordered[index] = future.result()
                    except Exception as exc:  # noqa: BLE001 - preserve call position
                        tool_name = str(getattr(tool_calls[index], "tool_name", ""))
                        ordered[index] = ToolResult.failure(
                            tool_name or "unknown",
                            error_message=str(exc),
                            error_code="TOOL_SCHEDULER_EXCEPTION",
                        )
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    continue
                while next_index < len(tool_calls) and len(in_flight) < max_workers:
                    future = executor.submit(
                        self._invoke_one,
                        run_id,
                        tool_calls[next_index],
                        advertised_tool_names,
                    )
                    in_flight[future] = next_index
                    next_index += 1

        for index, result in enumerate(ordered):
            if result is None:
                ordered[index] = self._record_skipped(
                    run_id,
                    tool_calls[index],
                    reason="tool call was not started before cancellation",
                )

        return [result for result in ordered if result is not None], cancelled or bool(
            cancel_check is not None and cancel_check()
        )

    def _skip_calls(self, run_id: str, tool_calls: tuple[Any, ...]) -> list[ToolResult]:
        return [self._record_skipped(run_id, tool_call) for tool_call in tool_calls]

    def _record_skipped(
        self, run_id: str, tool_call: Any, *, reason: str = "tool call was not started",
    ) -> ToolResult:
        tool_name = str(getattr(tool_call, "tool_name", "") or "unknown")
        call_id = str(getattr(tool_call, "call_id", "") or "")
        recorder = getattr(self._runner, "record_skipped", None)
        if callable(recorder):
            try:
                return recorder(
                    run_id,
                    tool_name,
                    call_id=call_id,
                    reason=reason,
                )
            except Exception as exc:  # noqa: BLE001 - preserve closure in result
                return ToolResult.failure(
                    tool_name,
                    error_code="TOOL_TRACE_EXCEPTION",
                    error_message=str(exc),
                )
        return ToolResult.failure(
            tool_name,
            error_code="TOOL_NOT_STARTED",
            error_message=reason,
        )

    def _execution_mode(
        self, tool_name: str, advertised_tool_names: frozenset[str]
    ) -> ToolExecutionMode:
        """Read scheduler metadata from the runner and fail closed for legacy runners."""

        if tool_name not in advertised_tool_names:
            return ToolExecutionMode.EXCLUSIVE
        resolver = getattr(self._runner, "execution_mode", None)
        if not callable(resolver):
            return ToolExecutionMode.EXCLUSIVE
        try:
            mode = resolver(tool_name)
        except Exception:  # noqa: BLE001 - scheduling safety takes precedence
            return ToolExecutionMode.EXCLUSIVE
        if isinstance(mode, ToolExecutionMode):
            return mode
        try:
            return ToolExecutionMode(str(mode).lower())
        except ValueError:
            return ToolExecutionMode.EXCLUSIVE

    def _invoke_one(
        self,
        run_id: str,
        tool_call: Any,
        advertised_tool_names: frozenset[str],
    ) -> ToolResult:
        """Invoke one tool and normalize an unexpected runner exception."""

        tool_name = str(getattr(tool_call, "tool_name", ""))
        if tool_name not in advertised_tool_names:
            return ToolResult.failure(
                tool_name or "unknown",
                error_code="TOOL_NOT_EXPOSED",
                error_message="tool call is not present in the host-advertised tool catalog",
            )
        try:
            arguments = dict(getattr(tool_call, "arguments", {}) or {})
            call_id = str(getattr(tool_call, "call_id", "") or "")
            invoke = self._runner.invoke
            try:
                signature = inspect.signature(invoke)
                parameters = signature.parameters
                supports_call_id = (
                    "call_id" in parameters
                    or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values()
                    )
                )
            except (TypeError, ValueError):
                supports_call_id = False
            if supports_call_id:
                return invoke(run_id, tool_name, arguments, call_id=call_id)
            # Keep third-party/legacy runners source-compatible; the built-in
            # runner receives the original model call id above.
            return invoke(run_id, tool_name, arguments)
        except Exception as exc:  # noqa: BLE001 - runner failures stay in the trace
            return ToolResult.failure(
                tool_name or "unknown",
                error_message=str(exc),
                error_code="TOOL_EXCEPTION",
            )

    @staticmethod
    def _result(
        final_text: str,
        iterations: int,
        tool_calls_made: int,
        results: list[ToolResult],
        reason: str,
        *,
        error: str = "",
    ) -> AgentToolLoopResult:
        return AgentToolLoopResult(
            final_text=final_text,
            iterations=iterations,
            tool_calls_made=tool_calls_made,
            tool_results=tuple(results),
            terminated_reason=reason,
            error=error,
        )


def _tool_result_to_text(
    result: ToolResult,
    pruner: ToolResultPruner | None = None,
) -> str:
    """Serialise a ToolResult into model-readable text (untrusted, redacted).

    Plan §1.2 #2: tool output is untrusted data; the model cannot use it to
    alter system instructions.  Only the structured payload is serialised.
    """

    renderer = pruner or ToolResultPruner()
    return renderer.render(result.to_dict()).text


def _tool_names(tools: list[dict[str, Any]]) -> frozenset[str]:
    """Extract host-advertised function names from an OpenAI tools catalog."""

    names: set[str] = set()
    for definition in tools:
        if not isinstance(definition, dict):
            continue
        function = definition.get("function")
        name = function.get("name", "") if isinstance(function, dict) else ""
        if name:
            names.add(str(name))
    return frozenset(names)


__all__ = [
    "AgentToolLoop",
    "AgentToolLoopBudget",
    "AgentToolLoopResult",
    "CancelCheck",
]
