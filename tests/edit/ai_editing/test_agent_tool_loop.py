"""AgentToolLoop tests (plan §A2).

Validates the model-driven tool loop that lets the agent call ``topos.*``
tools through the controlled ToolInvocationRunner, feed redacted results back
to the model, and converge or stop on budget / cancel / error.
"""

from __future__ import annotations

import threading
import time

import pytest

from src.features.edit.ai_editing.agent_tool_loop import (
    AgentToolLoop,
    AgentToolLoopBudget,
)
from src.features.edit.ai_editing.tool_calling_client import (
    ToolCallRequest,
    ToolCallResponse,
)
from src.features.edit.ai_editing.tool_invocation import tool_execution_mode
from src.features.edit.ai_editing.tool_result_pruner import (
    ToolResultPruneConfig,
    ToolResultPruner,
)
from src.features.edit.ai_editing.tool_result import ToolOutcome, ToolResult


# ---------------------------------------------------------------------- #
# fakes
# ---------------------------------------------------------------------- #


class _MockRunner:
    """Records calls; returns configurable ToolResults."""

    def __init__(self, outcome: str = ToolOutcome.SUCCESS) -> None:
        self.calls: list[str] = []
        self._outcome = outcome

    def invoke(self, run_id, tool_name, args, *, approval=None):
        self.calls.append(tool_name)
        if self._outcome == ToolOutcome.DENIED:
            return ToolResult.denied(tool_name, reason="no grant")
        if self._outcome == ToolOutcome.FAILURE:
            return ToolResult.failure(tool_name, error_message="boom")
        return ToolResult.success(tool_name, data={"echo": tool_name})


class _ParallelRunner(_MockRunner):
    """Runner that makes overlap and completion ordering observable."""

    def __init__(self, delays: dict[str, float]) -> None:
        super().__init__()
        self._delays = delays
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.lifecycle: list[tuple[str, str]] = []

    def execution_mode(self, tool_name):
        return tool_execution_mode(tool_name)

    def invoke(self, run_id, tool_name, args, *, approval=None):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.lifecycle.append(("start", tool_name))
        time.sleep(self._delays.get(tool_name, 0.0))
        with self._lock:
            self.lifecycle.append(("end", tool_name))
            self.active -= 1
        return ToolResult.success(tool_name, data={"echo": tool_name})


class _ExplodingParallelRunner(_ParallelRunner):
    def invoke(self, run_id, tool_name, args, *, approval=None):
        if tool_name == "topos.search_help":
            raise RuntimeError("analysis worker failed")
        return super().invoke(run_id, tool_name, args, approval=approval)


class _ScriptedClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0

    def complete_with_tools(self, messages, tools, **kw):
        r = self._responses[self._i]
        self._i += 1
        return r


def _tool_call(call_id="c1", name="topos.search_assets", args=None):
    return ToolCallRequest(call_id=call_id, tool_name=name, arguments=args or {})


_TEST_TOOLS = [
    {"type": "function", "function": {"name": name}}
    for name in (
        "topos.get_capabilities",
        "topos.search_assets",
        "topos.get_asset_analysis",
        "topos.search_help",
        "topos.explain_diagnostic",
        "topos.validate_edit_plan",
        "topos.apply_edit_plan",
    )
]


# ---------------------------------------------------------------------- #
# Convergence
# ---------------------------------------------------------------------- #


class TestConvergence:
    def test_converges_immediately_without_tool_calls(self):
        loop = AgentToolLoop(_MockRunner())
        client = _ScriptedClient([ToolCallResponse(text="hello", finish=True)])
        result = loop.run("r1", client, [{"role": "user", "content": "hi"}], _TEST_TOOLS)
        assert result.converged
        assert result.final_text == "hello"
        assert result.tool_calls_made == 0

    def test_executes_one_tool_then_converges(self):
        runner = _MockRunner()
        loop = AgentToolLoop(runner)
        client = _ScriptedClient(
            [
                ToolCallResponse(text="", tool_calls=(_tool_call(),)),
                ToolCallResponse(text="found it", finish=True),
            ]
        )
        result = loop.run("r1", client, [{"role": "user", "content": "search"}], _TEST_TOOLS)
        assert result.converged
        assert result.tool_calls_made == 1
        assert result.final_text == "found it"
        assert runner.calls == ["topos.search_assets"]

    def test_rejects_missing_or_duplicate_model_call_ids(self):
        runner = _MockRunner()
        loop = AgentToolLoop(runner)
        missing = loop.run(
            "r1",
            _ScriptedClient([
                ToolCallResponse(text="", tool_calls=(_tool_call(""),)),
            ]),
            [{"role": "user", "content": "search"}],
            _TEST_TOOLS,
        )
        duplicate = loop.run(
            "r2",
            _ScriptedClient([
                ToolCallResponse(
                    text="",
                    tool_calls=(
                        _tool_call("same"),
                        _tool_call("same", "topos.get_capabilities"),
                    ),
                ),
            ]),
            [{"role": "user", "content": "search"}],
            _TEST_TOOLS,
        )

        assert missing.terminated_reason == "error"
        assert "without a call_id" in missing.error
        assert duplicate.terminated_reason == "error"
        assert "duplicate tool call ids" in duplicate.error
        assert runner.calls == []

    def test_assistant_tool_calls_precede_tool_results(self):
        """OpenAI protocol: assistant(tool_calls) message MUST come before
        role='tool' result messages.  Also arguments must be JSON string."""
        runner = _MockRunner()
        loop = AgentToolLoop(runner)
        captured_messages: list[list] = []

        class _CapturingClient(_ScriptedClient):
            def complete_with_tools(self, messages, tools, **kw):
                captured_messages.append(list(messages))
                return super().complete_with_tools(messages, tools, **kw)

        client = _CapturingClient(
            [
                ToolCallResponse(text="", tool_calls=(_tool_call("c1", "topos.search_assets"),)),
                ToolCallResponse(text="done", finish=True),
            ]
        )
        loop.run("r1", client, [{"role": "user", "content": "go"}], _TEST_TOOLS)

        # Second call's messages must have: user → assistant(tool_calls) → tool(result)
        msgs = captured_messages[1]
        roles = [m.get("role") for m in msgs]
        assert "assistant" in roles
        assistant_idx = roles.index("assistant")
        tool_idx = roles.index("tool")
        assert assistant_idx < tool_idx, "assistant(tool_calls) must precede tool results"
        # Arguments in assistant tool_calls must be JSON string.
        assistant_msg = msgs[assistant_idx]
        for tc in assistant_msg.get("tool_calls", []):
            args = tc["function"]["arguments"]
            assert isinstance(args, str), f"arguments must be str, got {type(args)}"

    def test_tool_result_sent_to_model_is_pruned_but_returned_result_is_full(self):
        runner = _MockRunner()
        loop = AgentToolLoop(
            runner,
            result_pruner=ToolResultPruner(
                ToolResultPruneConfig(threshold_chars=120, head_chars=50, tail_chars=20)
            ),
        )
        captured: list[list[dict]] = []

        class CapturingClient(_ScriptedClient):
            def complete_with_tools(self, messages, tools, **kw):
                captured.append(list(messages))
                return super().complete_with_tools(messages, tools, **kw)

        result = loop.run(
            "r1",
            CapturingClient([
                ToolCallResponse(text="", tool_calls=(_tool_call(),)),
                ToolCallResponse(text="done", finish=True),
            ]),
            [{"role": "user", "content": "search"}],
            _TEST_TOOLS,
        )

        tool_message = captured[1][-1]
        assert "tool result middle pruned" in tool_message["content"]
        assert result.tool_results[0].data["echo"] == "topos.search_assets"

    def test_multi_round_loop(self):
        runner = _MockRunner()
        loop = AgentToolLoop(runner)
        client = _ScriptedClient(
            [
                ToolCallResponse(text="", tool_calls=(_tool_call("c1", "topos.search_assets"),)),
                ToolCallResponse(
                    text="",
                    tool_calls=(_tool_call("c2", "topos.get_asset_analysis"),),
                ),
                ToolCallResponse(text="analysis done", finish=True),
            ]
        )
        result = loop.run("r1", client, [{"role": "user", "content": "analyze"}], _TEST_TOOLS)
        assert result.converged
        assert result.tool_calls_made == 2
        assert result.iterations == 3
        assert runner.calls == ["topos.search_assets", "topos.get_asset_analysis"]

    def test_tool_results_recorded(self):
        loop = AgentToolLoop(_MockRunner())
        client = _ScriptedClient(
            [
                ToolCallResponse(text="", tool_calls=(_tool_call(),)),
                ToolCallResponse(text="done", finish=True),
            ]
        )
        result = loop.run("r1", client, [{"role": "user", "content": "x"}], _TEST_TOOLS)
        assert len(result.tool_results) == 1
        assert result.tool_results[0].succeeded

    def test_parallel_read_tools_overlap_but_results_keep_model_order(self):
        """DeepSeek-style parallel group with an exclusive barrier.

        The first two tools are explicitly parallel-safe. ``validate_edit_plan``
        is a preview operation and therefore remains an exclusive barrier.
        """

        runner = _ParallelRunner({
            "topos.get_capabilities": 0.04,
            "topos.search_help": 0.01,
            "topos.validate_edit_plan": 0.0,
            "topos.explain_diagnostic": 0.0,
        })
        captured_messages: list[list[dict]] = []

        class _CapturingClient(_ScriptedClient):
            def complete_with_tools(self, messages, tools, **kw):
                captured_messages.append(list(messages))
                return super().complete_with_tools(messages, tools, **kw)

        client = _CapturingClient([
            ToolCallResponse(text="", tool_calls=(
                _tool_call("c1", "topos.get_capabilities"),
                _tool_call("c2", "topos.search_help"),
                _tool_call("c3", "topos.validate_edit_plan"),
                _tool_call("c4", "topos.explain_diagnostic"),
            )),
            ToolCallResponse(text="done", finish=True),
        ])
        loop = AgentToolLoop(
            runner,
            budget=AgentToolLoopBudget(max_parallel_tool_calls=2),
        )

        result = loop.run("r1", client, [{"role": "user", "content": "inspect"}], _TEST_TOOLS)

        assert result.converged
        assert result.tool_calls_made == 4
        assert runner.max_active == 2
        lifecycle = runner.lifecycle
        validation_start = lifecycle.index(("start", "topos.validate_edit_plan"))
        assert validation_start > lifecycle.index(("end", "topos.get_capabilities"))
        assert validation_start > lifecycle.index(("end", "topos.search_help"))
        assert [r.tool_name for r in result.tool_results] == [
            "topos.get_capabilities",
            "topos.search_help",
            "topos.validate_edit_plan",
            "topos.explain_diagnostic",
        ]
        tool_messages = [m for m in captured_messages[1] if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2", "c3", "c4"]

    def test_parallel_exception_preserves_result_order(self):
        runner = _ExplodingParallelRunner({
            "topos.get_capabilities": 0.01,
            "topos.search_help": 0.0,
        })
        client = _ScriptedClient([
            ToolCallResponse(text="", tool_calls=(
                _tool_call("c1", "topos.get_capabilities"),
                _tool_call("c2", "topos.search_help"),
            )),
            ToolCallResponse(text="done", finish=True),
        ])
        result = AgentToolLoop(
            runner,
            budget=AgentToolLoopBudget(max_parallel_tool_calls=2),
        ).run("r1", client, [{"role": "user", "content": "inspect"}], _TEST_TOOLS)

        assert result.converged
        assert [r.tool_name for r in result.tool_results] == [
            "topos.get_capabilities",
            "topos.search_help",
        ]
        assert result.tool_results[0].succeeded
        assert result.tool_results[1].failed
        assert result.tool_results[1].error_code == "TOOL_EXCEPTION"

    def test_cancel_stops_rolling_pool_after_started_calls(self):
        runner = _ParallelRunner({
            "topos.get_capabilities": 0.03,
            "topos.search_help": 0.03,
            "topos.explain_diagnostic": 0.0,
        })
        checks = {"count": 0}

        def cancel_after_initial_dispatch():
            checks["count"] += 1
            return checks["count"] >= 5

        client = _ScriptedClient([
            ToolCallResponse(text="", tool_calls=(
                _tool_call("c1", "topos.get_capabilities"),
                _tool_call("c2", "topos.search_help"),
                _tool_call("c3", "topos.explain_diagnostic"),
                _tool_call("c4", "topos.explain_diagnostic"),
            )),
        ])
        result = AgentToolLoop(
            runner,
            budget=AgentToolLoopBudget(max_parallel_tool_calls=2),
        ).run(
            "r1", client, [{"role": "user", "content": "inspect"}], _TEST_TOOLS,
            cancel_check=cancel_after_initial_dispatch,
        )

        assert result.cancelled
        assert result.tool_calls_made == 2
        assert [name for kind, name in runner.lifecycle if kind == "start"] == [
            "topos.get_capabilities",
            "topos.search_help",
        ]


# ---------------------------------------------------------------------- #
# Budget
# ---------------------------------------------------------------------- #


class TestBudget:
    def test_max_iterations_terminates(self):
        loop = AgentToolLoop(
            _MockRunner(), budget=AgentToolLoopBudget(max_iterations=2, max_tool_calls=10)
        )
        # Always returns a tool call -> never converges.
        client = _ScriptedClient(
            [ToolCallResponse(text="", tool_calls=(_tool_call(),))] * 10
        )
        result = loop.run("r1", client, [{"role": "user", "content": "x"}], _TEST_TOOLS)
        assert result.terminated_reason == "max_iterations"
        assert result.iterations == 2

    def test_max_tool_calls_terminates(self):
        loop = AgentToolLoop(
            _MockRunner(), budget=AgentToolLoopBudget(max_iterations=10, max_tool_calls=2)
        )
        # Each iteration requests 3 tool calls; the 3rd hits the budget.
        client = _ScriptedClient(
            [
                ToolCallResponse(
                    text="",
                    tool_calls=(
                        _tool_call("c1"),
                        _tool_call("c2"),
                        _tool_call("c3"),
                    ),
                )
            ]
            * 10
        )
        result = loop.run("r1", client, [{"role": "user", "content": "x"}], _TEST_TOOLS)
        assert result.terminated_reason == "max_tool_calls"
        assert result.tool_calls_made == 2

    def test_budget_must_be_positive(self):
        with pytest.raises(ValueError):
            AgentToolLoopBudget(max_iterations=0)
        with pytest.raises(ValueError):
            AgentToolLoopBudget(max_tool_calls=0)
        with pytest.raises(ValueError):
            AgentToolLoopBudget(max_parallel_tool_calls=0)


# ---------------------------------------------------------------------- #
# Cancel + error
# ---------------------------------------------------------------------- #


class TestCancelAndError:
    def test_cancel_before_model_call(self):
        loop = AgentToolLoop(_MockRunner())
        client = _ScriptedClient([ToolCallResponse(text="x", finish=True)])
        result = loop.run(
            "r1", client, [{"role": "user", "content": "x"}], _TEST_TOOLS,
            cancel_check=lambda: True,
        )
        assert result.cancelled
        assert result.terminated_reason == "cancelled"

    def test_cancel_between_tool_calls(self):
        runner = _MockRunner()
        loop = AgentToolLoop(runner)
        calls = {"n": 0}

        def cancel_after_one():
            calls["n"] += 1
            return calls["n"] > 2  # cancel on the 3rd checkpoint

        client = _ScriptedClient(
            [
                ToolCallResponse(text="", tool_calls=(_tool_call("c1"), _tool_call("c2"),)),
            ]
            * 5
        )
        result = loop.run(
            "r1", client, [{"role": "user", "content": "x"}], _TEST_TOOLS,
            cancel_check=cancel_after_one,
        )
        assert result.cancelled

    def test_model_exception_becomes_error(self):
        class BoomClient:
            def complete_with_tools(self, *a, **k):
                raise RuntimeError("network down")

        loop = AgentToolLoop(_MockRunner())
        result = loop.run("r1", BoomClient(), [{"role": "user", "content": "x"}], _TEST_TOOLS)
        assert result.terminated_reason == "error"
        assert "network down" in result.error

    def test_unadvertised_tool_is_not_invoked(self):
        runner = _MockRunner()
        client = _ScriptedClient([
            ToolCallResponse(text="", tool_calls=(_tool_call("c1", "topos.search_assets"),)),
            ToolCallResponse(text="tool unavailable", finish=True),
        ])
        result = AgentToolLoop(runner).run(
            "r1", client, [{"role": "user", "content": "x"}], []
        )

        assert result.cancelled is False
        assert result.converged
        assert result.tool_calls_made == 1
        assert runner.calls == []
        assert result.tool_results[0].error_code == "TOOL_NOT_EXPOSED"


# ---------------------------------------------------------------------- #
# Denied tool (approval gate)
# ---------------------------------------------------------------------- #


class TestDeniedTool:
    def test_denied_tool_recorded_not_crash(self):
        # A write tool without approval is DENIED by ToolFacade; the loop must
        # record it as a tool result and keep going, not crash.
        runner = _MockRunner(outcome=ToolOutcome.DENIED)
        loop = AgentToolLoop(runner)
        client = _ScriptedClient(
            [
                ToolCallResponse(text="", tool_calls=(_tool_call("c1", "topos.apply_edit_plan"),)),
                ToolCallResponse(text="cannot apply without approval", finish=True),
            ]
        )
        result = loop.run("r1", client, [{"role": "user", "content": "apply"}], _TEST_TOOLS)
        assert result.converged
        assert result.tool_results[0].is_denied
        assert result.tool_results[0].outcome == ToolOutcome.DENIED

    def test_denial_reason_is_visible_to_next_model_turn(self):
        runner = _MockRunner(outcome=ToolOutcome.DENIED)
        captured: list[list[dict]] = []

        class _CapturingClient(_ScriptedClient):
            def complete_with_tools(self, messages, tools, **kw):
                captured.append(list(messages))
                return super().complete_with_tools(messages, tools, **kw)

        client = _CapturingClient([
            ToolCallResponse(text="", tool_calls=(_tool_call("c1", "topos.apply_edit_plan"),)),
            ToolCallResponse(text="approval needed", finish=True),
        ])
        AgentToolLoop(runner).run(
            "r1", client, [{"role": "user", "content": "apply"}], _TEST_TOOLS
        )

        tool_message = next(message for message in captured[1] if message.get("role") == "tool")
        assert "denied" in tool_message["content"]
        assert "TOOL_DENIED" in tool_message["content"]
