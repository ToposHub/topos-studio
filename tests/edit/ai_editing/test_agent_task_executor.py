# -*- coding: utf-8 -*-
"""Unit tests for the autonomous AgentTaskExecutor (no network, all fakes).

These tests validate the plan → approve → execute → verify → recover lifecycle
using fake LLM clients and a fake tool facade so the executor's logic is
exercised deterministically without any API calls.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.agent_task_executor import (
    AgentTaskExecutor,
    AgentTaskResult,
    AlwaysApproveBridge,
    NeverApproveBridge,
    StepResult,
    TaskPlan,
    TaskStep,
)
from src.features.edit.ai_editing.tool_facade import (
    ToolDefinition,
    ToolPermission,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeLLMClient:
    """Returns canned responses in sequence."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._call_index = 0
        self.last_usage = None
        self.calls: list[list[dict]] = []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        if self._call_index < len(self._responses):
            resp = self._responses[self._call_index]
            self._call_index += 1
            return resp
        return '{"achieved": true, "notes": "done"}'

    def stream_complete(self, messages, **kw):
        return self.complete(messages, **kw)


class FakeToolFacade:
    """Minimal facade that returns canned tool results."""

    def __init__(self, tool_results: dict[str, dict] | None = None):
        self._tool_results = tool_results or {}
        self.invoke_calls: list[tuple[str, dict, dict]] = []

    def list_tools(self, *, max_permission=ToolPermission.READ):
        # Return a minimal set of tool definitions.
        return [
            ToolDefinition(
                name="topos.get_project_snapshot",
                permission=ToolPermission.READ,
                description="Get project state",
                input_schema={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="topos.search_assets",
                permission=ToolPermission.READ,
                description="Search assets",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            ),
            ToolDefinition(
                name="topos.validate_edit_plan",
                permission=ToolPermission.PREVIEW,
                description="Validate a plan",
                input_schema={
                    "type": "object",
                    "required": ["plan"],
                    "properties": {"plan": {"type": "object"}},
                },
            ),
            ToolDefinition(
                name="topos.apply_edit_plan",
                permission=ToolPermission.WRITE,
                description="Apply a plan",
                input_schema={
                    "type": "object",
                    "required": ["plan"],
                    "properties": {"plan": {"type": "object"}},
                },
            ),
        ]

    def invoke(self, tool_name, args=None, *, approval=None, **kw):
        self.invoke_calls.append((tool_name, args or {}, {"approval": approval}))
        result = self._tool_results.get(tool_name)
        if result is not None:
            return dict(result)
        # Default: read tools succeed with empty data, write tools need approval.
        from src.features.edit.ai_editing.tool_invocation import (
            find_tool_definition,
        )
        defn = find_tool_definition(tool_name)
        if defn and defn.permission == ToolPermission.WRITE and approval is None:
            return {"ok": False, "diagnostics": [{"code": "PERMISSION_DENIED",
                                                   "message": "no approval"}]}
        return {"ok": True, "data": {"tool": tool_name, "args": args}}


def _plan_json(steps: list[dict], rationale: str = "test") -> str:
    """Build a valid planning JSON response."""
    return json.dumps({"rationale": rationale, "steps": steps}, ensure_ascii=False)


_NO_BRIDGE = object()  # sentinel: distinguish "not provided" from "explicitly None"


def _make_executor(
    llm_responses: list[str],
    tool_results: dict | None = None,
    approval_bridge=_NO_BRIDGE,
    *,
    tool_allowlist: tuple[str, ...] = (),
    max_steps: int = 10,
    system_instruction: str = "",
) -> tuple[AgentTaskExecutor, AgentOrchestrator, FakeLLMClient, FakeToolFacade]:
    """Build an executor wired with fakes."""
    orch = AgentOrchestrator()
    client = FakeLLMClient(llm_responses)
    facade = FakeToolFacade(tool_results)
    profile = MagicMock()
    profile.limits = MagicMock(max_context_tokens=200000)
    bridge = AlwaysApproveBridge() if approval_bridge is _NO_BRIDGE else approval_bridge
    executor = AgentTaskExecutor(
        orch,
        client_factory=lambda p: client,
        tool_facade=facade,
        profile=profile,
        approval_bridge=bridge,
        tool_allowlist=tool_allowlist,
        max_steps=max_steps,
        system_instruction=system_instruction,
    )
    return executor, orch, client, facade


# --------------------------------------------------------------------------- #
# Tests: planning
# --------------------------------------------------------------------------- #


class TestPlanning:
    def test_preset_allowlist_and_step_budget_apply_before_execution(self):
        plan_json = _plan_json([
            {"description": "allowed", "tool_name": "topos.get_project_snapshot", "tool_args": {}},
            {"description": "not allowed", "tool_name": "topos.search_assets", "tool_args": {}},
        ])
        executor, _, _, _ = _make_executor(
            [plan_json],
            tool_allowlist=("topos.get_project_snapshot",),
            max_steps=1,
        )

        plan = executor._parse_plan_json(plan_json, "查看工程")

        assert plan is not None
        assert [step.tool_name for step in plan.steps] == ["topos.get_project_snapshot"]

    def test_preset_system_instruction_is_added_to_planning_prompt(self):
        plan_json = _plan_json([
            {"description": "read", "tool_name": "topos.get_project_snapshot", "tool_args": {}},
        ])
        executor, _, client, _ = _make_executor(
            [plan_json, '{"achieved": true, "notes": "ok"}', "done"],
            system_instruction="只报告可验证事实。",
        )

        result = executor.execute(
            session_id="test", user_goal="查看工程",
            messages=[{"role": "user", "content": "查看工程"}],
        )

        assert result.terminal_state == AgentRunState.SUCCEEDED
        assert "只报告可验证事实。" in client.calls[0][0]["content"]

    def test_plan_decomposition(self):
        """LLM returns 3 steps → correctly parsed into TaskPlan."""
        plan_json = _plan_json([
            {"description": "read project", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "snapshot"},
            {"description": "search assets", "tool_name": "topos.search_assets",
             "tool_args": {"query": "sunset"}, "expected_outcome": "found"},
            {"description": "summarise", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "done"},
        ])
        # Verification response (achieved=true), summary response.
        executor, orch, client, facade = _make_executor([
            plan_json,
            '{"achieved": true, "notes": "all good"}',
            "任务完成！",
        ])

        result = executor.execute(
            session_id="test", user_goal="查看项目并总结",
            messages=[{"role": "user", "content": "查看项目并总结"}],
        )

        assert result.terminal_state == AgentRunState.SUCCEEDED
        assert result.plan is not None
        assert result.plan.step_count == 3
        assert result.verification_passed
        assert len(result.step_results) == 3

    def test_empty_plan_fails(self):
        """Model returns zero steps → FAILED (never fake success)."""
        executor, orch, _, _ = _make_executor([
            _plan_json([]),
        ])
        result = executor.execute(
            session_id="test", user_goal="do nothing",
            messages=[{"role": "user", "content": "test"}],
        )
        assert result.terminal_state == AgentRunState.FAILED

    def test_invalid_json_plan_fails(self):
        """Model returns garbage → FAILED."""
        executor, orch, _, _ = _make_executor([
            "this is not json at all",
        ])
        result = executor.execute(
            session_id="test", user_goal="test",
            messages=[{"role": "user", "content": "test"}],
        )
        assert result.terminal_state == AgentRunState.FAILED

    def test_json_with_markdown_fence_parsed(self):
        """Model wraps JSON in ```json ... ``` fence → still parsed."""
        fenced = '```json\n' + _plan_json([
            {"description": "step1", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "ok"},
        ]) + '\n```'
        executor, orch, _, _ = _make_executor([
            fenced,
            '{"achieved": true, "notes": "ok"}',
            "done",
        ])
        result = executor.execute(
            session_id="test", user_goal="test",
            messages=[{"role": "user", "content": "test"}],
        )
        assert result.terminal_state == AgentRunState.SUCCEEDED
        assert result.plan.step_count == 1

    def test_unknown_tool_filtered(self):
        """Model references a non-existent tool → that step is dropped."""
        plan_json = _plan_json([
            {"description": "read", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "ok"},
            {"description": "bad", "tool_name": "topos.nonexistent_tool",
             "tool_args": {}, "expected_outcome": "ok"},
        ])
        executor, orch, _, _ = _make_executor([
            plan_json,
            '{"achieved": true, "notes": "ok"}',
            "done",
        ])
        result = executor.execute(
            session_id="test", user_goal="test",
            messages=[{"role": "user", "content": "test"}],
        )
        assert result.terminal_state == AgentRunState.SUCCEEDED
        # Only the valid step survives.
        assert result.plan.step_count == 1


# --------------------------------------------------------------------------- #
# Tests: execution
# --------------------------------------------------------------------------- #


class TestExecution:
    def test_read_step_executes_without_approval(self):
        """Read tools execute immediately — no approval bridge needed."""
        plan_json = _plan_json([
            {"description": "read", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "ok"},
        ])
        # No approval bridge at all → read-only plan should still work.
        executor, orch, client, facade = _make_executor([
            plan_json,
            '{"achieved": true, "notes": "ok"}',
            "done",
        ], approval_bridge=None)

        result = executor.execute(
            session_id="test", user_goal="read project",
            messages=[{"role": "user", "content": "read project"}],
        )
        assert result.terminal_state == AgentRunState.SUCCEEDED
        # Verify the tool was actually called.
        assert len(facade.invoke_calls) >= 1
        assert facade.invoke_calls[0][0] == "topos.get_project_snapshot"

    def test_write_step_needs_approval(self):
        """Write tools trigger WAITING_APPROVAL — AlwaysApproveBridge grants it."""
        plan_json = _plan_json([
            {"description": "apply", "tool_name": "topos.apply_edit_plan",
             "tool_args": {"plan": {"test": 1}}, "expected_outcome": "applied"},
        ])
        executor, orch, client, facade = _make_executor([
            plan_json,
            '{"achieved": true, "notes": "ok"}',
            "done",
        ], approval_bridge=AlwaysApproveBridge())

        result = executor.execute(
            session_id="test", user_goal="write something",
            messages=[{"role": "user", "content": "write"}],
        )
        assert result.terminal_state == AgentRunState.SUCCEEDED
        assert result.plan.needs_approval
        # The write tool should have been called with the approval sentinel.
        write_calls = [c for c in facade.invoke_calls if c[0] == "topos.apply_edit_plan"]
        assert len(write_calls) >= 1
        assert write_calls[0][2]["approval"] is not None

    def test_write_step_denied_without_bridge(self):
        """No bridge → write plan fails immediately."""
        plan_json = _plan_json([
            {"description": "apply", "tool_name": "topos.apply_edit_plan",
             "tool_args": {"plan": {"test": 1}}, "expected_outcome": "applied"},
        ])
        executor, orch, _, _ = _make_executor([
            plan_json,
        ], approval_bridge=None)

        result = executor.execute(
            session_id="test", user_goal="write",
            messages=[{"role": "user", "content": "write"}],
        )
        assert result.terminal_state == AgentRunState.FAILED

    def test_approval_denied_terminates(self):
        """NeverApproveBridge → plan with writes → FAILED."""
        plan_json = _plan_json([
            {"description": "apply", "tool_name": "topos.apply_edit_plan",
             "tool_args": {"plan": {}}, "expected_outcome": "applied"},
        ])
        executor, orch, _, _ = _make_executor([
            plan_json,
        ], approval_bridge=NeverApproveBridge())

        result = executor.execute(
            session_id="test", user_goal="write",
            messages=[{"role": "user", "content": "write"}],
        )
        assert result.terminal_state == AgentRunState.FAILED

    def test_tool_failure_recorded_but_continues(self):
        """A tool returning failure is recorded — execution continues."""
        plan_json = _plan_json([
            {"description": "step1", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "ok"},
            {"description": "step2", "tool_name": "topos.search_assets",
             "tool_args": {"query": "x"}, "expected_outcome": "ok"},
        ])
        facade_results = {
            "topos.search_assets": {"ok": False, "diagnostics": [{"code": "NOT_FOUND",
                                                                    "message": "no match"}]},
        }
        executor, orch, _, facade = _make_executor([
            plan_json,
            '{"achieved": true, "notes": "ok"}',
            "done",
        ], tool_results=facade_results)

        result = executor.execute(
            session_id="test", user_goal="test",
            messages=[{"role": "user", "content": "test"}],
        )
        assert result.terminal_state == AgentRunState.SUCCEEDED
        # step2 should be failure.
        step2 = [sr for sr in result.step_results if sr.tool_name == "topos.search_assets"]
        assert len(step2) == 1
        assert not step2[0].succeeded


# --------------------------------------------------------------------------- #
# Tests: verification + recovery
# --------------------------------------------------------------------------- #


class TestVerification:
    def test_verification_pass_succeeds(self):
        """Verifier says achieved=true → SUCCEEDED."""
        plan_json = _plan_json([
            {"description": "read", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "ok"},
        ])
        executor, orch, _, _ = _make_executor([
            plan_json,
            '{"achieved": true, "notes": "great"}',
            "summary",
        ])
        result = executor.execute(
            session_id="test", user_goal="test",
            messages=[{"role": "user", "content": "test"}],
        )
        assert result.terminal_state == AgentRunState.SUCCEEDED
        assert result.verification_passed
        assert result.reply == "summary"

    def test_verification_fail_triggers_replan(self):
        """First verify fails → replan → second verify passes → SUCCEEDED."""
        plan1 = _plan_json([
            {"description": "read", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "ok"},
        ])
        plan2 = _plan_json([
            {"description": "read2", "tool_name": "topos.search_assets",
             "tool_args": {"query": "fix"}, "expected_outcome": "ok"},
        ])
        executor, orch, _, _ = _make_executor([
            plan1,                                    # first plan
            '{"achieved": false, "notes": "bad"}',   # first verify fails
            plan2,                                    # replan
            '{"achieved": true, "notes": "fixed"}',  # second verify passes
            "fixed summary",
        ])
        result = executor.execute(
            session_id="test", user_goal="test",
            messages=[{"role": "user", "content": "test"}],
        )
        assert result.terminal_state == AgentRunState.SUCCEEDED
        assert result.replan_count == 1

    def test_second_verification_fail_terminates(self):
        """Both verifications fail → FAILED (max replans=1)."""
        plan = _plan_json([
            {"description": "read", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "ok"},
        ])
        executor, orch, _, _ = _make_executor([
            plan,                                     # first plan
            '{"achieved": false, "notes": "bad1"}',   # first verify fails
            plan,                                     # replan (same plan)
            '{"achieved": false, "notes": "bad2"}',   # second verify fails
        ])
        result = executor.execute(
            session_id="test", user_goal="test",
            messages=[{"role": "user", "content": "test"}],
        )
        assert result.terminal_state == AgentRunState.FAILED
        assert not result.verification_passed


# --------------------------------------------------------------------------- #
# Tests: cancellation
# --------------------------------------------------------------------------- #


class TestCancellation:
    def test_cancel_before_execution(self):
        """Cancel during planning → CANCELLED."""
        plan_json = _plan_json([
            {"description": "read", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "ok"},
        ])
        orch = AgentOrchestrator()
        client = FakeLLMClient([
            plan_json,
            '{"achieved": true, "notes": "ok"}',
            "done",
        ])
        facade = FakeToolFacade()
        profile = MagicMock()
        profile.limits = MagicMock(max_context_tokens=200000)
        executor = AgentTaskExecutor(
            orch,
            client_factory=lambda p: client,
            tool_facade=facade,
            profile=profile,
            approval_bridge=AlwaysApproveBridge(),
        )

        # Cancel before execute.
        def on_plan(plan: TaskPlan):
            orch.request_cancel_for_session("test")

        result = executor.execute(
            session_id="test", user_goal="test",
            messages=[{"role": "user", "content": "test"}],
            on_plan=on_plan,
        )
        assert result.terminal_state == AgentRunState.CANCELLED


# --------------------------------------------------------------------------- #
# Tests: events
# --------------------------------------------------------------------------- #


class TestEvents:
    def test_events_emitted_for_happy_path(self):
        """A successful run emits the expected event sequence."""
        plan_json = _plan_json([
            {"description": "read", "tool_name": "topos.get_project_snapshot",
             "tool_args": {}, "expected_outcome": "ok"},
        ])
        executor, orch, _, _ = _make_executor([
            plan_json,
            '{"achieved": true, "notes": "ok"}',
            "done",
        ])
        result = executor.execute(
            session_id="test", user_goal="test",
            messages=[{"role": "user", "content": "test"}],
        )
        events = [e.kind for e in orch.get_events(result.run_id)]
        # Must have the core lifecycle events.
        assert AgentEventKind.RUN_CREATED in events
        assert AgentEventKind.RUN_CONTEXT_STARTED in events
        assert AgentEventKind.RUN_CONTEXT_READY in events
        assert AgentEventKind.RUN_PLAN_STARTED in events
        assert AgentEventKind.RUN_PLAN_READY in events
        assert AgentEventKind.RUN_TOOL_STARTED in events
        assert AgentEventKind.RUN_TOOL_FINISHED in events
        assert AgentEventKind.RUN_VERIFICATION_STARTED in events
        assert AgentEventKind.RUN_COMPLETED in events


# --------------------------------------------------------------------------- #
# Tests: is_write security
# --------------------------------------------------------------------------- #


class TestSecurity:
    def test_is_write_from_tool_definition_not_model(self):
        """is_write is derived from the tool registry, NOT from model JSON."""

        # Even if the model says "is_write": false, a WRITE tool is still write.
        step = TaskStep(
            step_id="s1", description="test",
            tool_name="topos.apply_edit_plan",
            tool_args={"plan": {}},
        )
        assert step.is_write  # Derived from ToolPermission.WRITE

    def test_read_tool_not_write(self):
        step = TaskStep(
            step_id="s1", description="test",
            tool_name="topos.get_project_snapshot",
            tool_args={},
        )
        assert not step.is_write

    def test_plan_needs_approval_property(self):
        plan_with_write = TaskPlan(
            plan_id="p1", goal="g",
            steps=(
                TaskStep(step_id="s1", description="r",
                         tool_name="topos.get_project_snapshot", tool_args={}),
                TaskStep(step_id="s2", description="w",
                         tool_name="topos.apply_edit_plan", tool_args={"plan": {}}),
            ),
        )
        assert plan_with_write.needs_approval

        plan_readonly = TaskPlan(
            plan_id="p2", goal="g",
            steps=(
                TaskStep(step_id="s1", description="r",
                         tool_name="topos.get_project_snapshot", tool_args={}),
            ),
        )
        assert not plan_readonly.needs_approval
