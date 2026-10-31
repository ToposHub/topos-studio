"""ToolInvocationRunner tests (plan §A2).

Validates the bridge that turns a ToolFacade.invoke dict into a structured
ToolResult plus a pair of orchestrator events, and recognises permission
denials as DENIED rather than generic failures.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentTask,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.tool_invocation import (
    ToolExecutionContext,
    ToolInputValidationMiddleware,
    ToolInvocationRunner,
    ToolPreflight,
    ToolTimeoutPolicy,
    find_tool_definition,
    result_from_facade,
    tool_execution_mode,
    tool_requires_approval,
)
from src.features.edit.ai_editing.tool_facade import ToolExecutionMode
from src.features.edit.ai_editing.tool_result import ToolOutcome, ToolResult


# ---------------------------------------------------------------------- #
# result_from_facade
# ---------------------------------------------------------------------- #


class TestResultFromFacade:
    def test_success(self):
        r = result_from_facade("topos.search_assets", {"ok": True, "count": 2})
        assert r.succeeded
        assert r.data["count"] == 2

    def test_failure_with_error(self):
        r = result_from_facade("topos.render_preview", {"ok": False, "error": "no timeline"})
        assert r.failed
        assert r.error_message == "no timeline"

    def test_permission_denied_is_denied(self):
        r = result_from_facade(
            "topos.apply_edit_plan",
            {
                "ok": False,
                "diagnostics": [
                    {"severity": "error", "code": "PERMISSION_DENIED", "reason": "no grant"}
                ],
            },
        )
        # A denial must be DENIED, not a generic FAILURE.
        assert r.outcome == ToolOutcome.DENIED
        assert r.is_denied
        assert "no grant" in r.error_message

    def test_diagnostics_extracted(self):
        r = result_from_facade(
            "topos.search_assets",
            {
                "ok": True,
                "diagnostics": [{"severity": "warning", "code": "PARTIAL", "reason": "hint"}],
            },
        )
        assert r.succeeded
        assert len(r.diagnostics) == 1
        assert r.diagnostics[0].code == "PARTIAL"

    def test_missing_ok_defaults_to_success(self):
        # AUD-14725（锁定旧行为协议：原断言 failed 锁定了"缺 ok 即失败"的
        # 误判 bug）：facade 契约是成功返回可省略 "ok" 键（如 {"manifest": ...}），
        # 只有显式 ok=False 才是失败。
        r = result_from_facade("t", {"manifest": {"tools": []}})
        assert r.succeeded
        assert r.data == {"manifest": {"tools": []}}


# ---------------------------------------------------------------------- #
# tool_requires_approval
# ---------------------------------------------------------------------- #


class TestToolRequiresApproval:
    def test_read_tool_does_not_require_approval(self):
        # search_assets is a read-only tool.
        assert tool_requires_approval("topos.search_assets") is False

    def test_write_tool_requires_approval(self):
        # apply_edit_plan is a write tool.
        assert tool_requires_approval("topos.apply_edit_plan") is True

    def test_unknown_tool_does_not_require_approval(self):
        # Unknown tools fail as unknown when invoked; they don't need a grant.
        assert tool_requires_approval("topos.does_not_exist") is False

    def test_find_tool_definition(self):
        defn = find_tool_definition("topos.search_assets")
        assert defn is not None
        assert defn.name == "topos.search_assets"
        assert find_tool_definition("topos.bogus") is None

    def test_execution_mode_fails_closed_for_unknown_tool(self):
        assert tool_execution_mode("topos.get_capabilities") == ToolExecutionMode.PARALLEL
        assert tool_execution_mode("topos.search_assets") == ToolExecutionMode.EXCLUSIVE
        assert tool_execution_mode("topos.validate_edit_plan") == ToolExecutionMode.EXCLUSIVE
        assert tool_execution_mode("topos.unknown") == ToolExecutionMode.EXCLUSIVE


# ---------------------------------------------------------------------- #
# ToolInvocationRunner
# ---------------------------------------------------------------------- #


class _MockFacade:
    def __init__(self, *, deny_write=True):
        self._deny_write = deny_write
        self.calls: list = []

    def invoke(self, name, args, *, approval=None):
        self.calls.append((name, args, approval))
        if name == "topos.search_assets":
            return {"ok": True, "count": 2}
        if name == "topos.apply_edit_plan":
            if approval is None and self._deny_write:
                return {
                    "ok": False,
                    "diagnostics": [
                        {"severity": "error", "code": "PERMISSION_DENIED", "reason": "no grant"}
                    ],
                }
            return {"ok": True, "applied": True}
        return {"ok": False, "error": "unknown tool"}

    def throw(self, name, args, *, approval=None):
        raise RuntimeError("boom")


@pytest.fixture
def runner_with_run():
    orch = AgentOrchestrator()
    events: list = []
    orch.subscribe(lambda e: events.append(e))
    run_id = orch.start_task(AgentTask(session_id="s1", title="tools"))
    facade = _MockFacade()
    runner = ToolInvocationRunner(orch, facade)
    return runner, orch, events, run_id, facade


class TestToolInvocationRunner:
    def test_read_tool_succeeds_with_events(self, runner_with_run):
        runner, orch, events, run_id, _ = runner_with_run
        result = runner.invoke(run_id, "topos.search_assets", {"query": "foo"})
        assert result.succeeded
        kinds = [e.kind for e in events]
        assert AgentEventKind.RUN_TOOL_STARTED in kinds
        assert AgentEventKind.RUN_TOOL_FINISHED in kinds

    def test_write_without_approval_is_denied(self, runner_with_run):
        runner, _, _, run_id, _ = runner_with_run
        result = runner.invoke(run_id, "topos.apply_edit_plan", {"plan": {}}, approval=None)
        assert result.is_denied
        assert result.outcome == ToolOutcome.DENIED

    def test_write_with_approval_succeeds(self, runner_with_run):
        runner, _, _, run_id, _ = runner_with_run
        result = runner.invoke(
            run_id, "topos.apply_edit_plan", {"plan": {}}, approval="grant-1"
        )
        assert result.succeeded

    def test_tool_exception_becomes_failure(self):
        orch = AgentOrchestrator()
        run_id = orch.start_task(AgentTask(session_id="s1", title="boom"))
        runner = ToolInvocationRunner(orch, _MockFacade())
        # Monkey-patch the facade to throw.
        runner._facade.invoke = _MockFacade.throw.__get__(runner._facade)  # type: ignore[method-assign]
        result = runner.invoke(run_id, "topos.search_assets")
        assert result.failed
        assert "boom" in result.error_message
        assert result.error_code == "TOOL_EXCEPTION"

    def test_finished_event_carries_outcome(self, runner_with_run):
        runner, _, events, run_id, _ = runner_with_run
        runner.invoke(run_id, "topos.search_assets", call_id="model-call-7")
        started = next(
            e for e in events if e.kind == AgentEventKind.RUN_TOOL_STARTED
        )
        finished = next(
            e for e in events if e.kind == AgentEventKind.RUN_TOOL_FINISHED
        )
        assert started.payload["call_id"] == "model-call-7"
        assert finished.payload["tool_name"] == "topos.search_assets"
        assert finished.payload["call_id"] == "model-call-7"
        assert finished.payload["outcome"] == ToolOutcome.SUCCESS
        assert "duration_ms" in finished.payload

    def test_pre_execute_can_deny_before_facade(self, runner_with_run):
        _, orch, events, run_id, facade = runner_with_run
        calls: list[str] = []

        class DenyMiddleware:
            def pre_execute(self, context: ToolExecutionContext):
                calls.append(f"pre:{context.call_id}")
                return ToolPreflight.deny("blocked by host policy", error_code="HOST_POLICY")

            def post_execute(self, context, result):
                calls.append(f"post:{result.error_code}")
                return result

        runner = ToolInvocationRunner(
            orch,
            facade,
            middlewares=(DenyMiddleware(),),
        )
        result = runner.invoke(run_id, "topos.search_assets", {"query": "secret"})

        assert result.is_denied
        assert result.error_code == "HOST_POLICY"
        assert facade.calls == []
        assert calls[0].startswith("pre:")
        assert calls[1] == "post:HOST_POLICY"
        finished = next(e for e in events if e.kind == AgentEventKind.RUN_TOOL_FINISHED)
        assert finished.payload["outcome"] == ToolOutcome.DENIED

    def test_pipeline_runs_pre_then_execute_then_post(self, runner_with_run):
        _, orch, _, run_id, facade = runner_with_run
        order: list[str] = []

        class Middleware:
            def pre_execute(self, context: ToolExecutionContext):
                order.append("pre")
                return None

            def post_execute(self, context, result: ToolResult):
                order.append("post")
                return replace(result, data={**result.data, "postprocessed": True})

        runner = ToolInvocationRunner(orch, facade, middlewares=(Middleware(),))
        result = runner.invoke(run_id, "topos.search_assets")

        assert order == ["pre", "post"]
        assert result.succeeded
        assert result.data["postprocessed"] is True
        assert facade.calls == [("topos.search_assets", None, None)]

    def test_post_execute_cannot_upgrade_denial(self, runner_with_run):
        _, orch, _, run_id, facade = runner_with_run

        class UnsafeMiddleware:
            def pre_execute(self, context):
                return None

            def post_execute(self, context, result):
                return ToolResult.success(context.tool_name, data={"unsafe": True})

        runner = ToolInvocationRunner(orch, facade, middlewares=(UnsafeMiddleware(),))
        result = runner.invoke(run_id, "topos.apply_edit_plan", {"plan": {}}, approval=None)

        assert result.is_denied
        assert result.error_code == "TOOL_DENIED"

    def test_post_execute_cannot_downgrade_denial_to_partial(self, runner_with_run):
        _, orch, _, run_id, facade = runner_with_run

        class UnsafeMiddleware:
            def pre_execute(self, context):
                return None

            def post_execute(self, context, result):
                return ToolResult.partial(context.tool_name, data={"unsafe": True})

        runner = ToolInvocationRunner(orch, facade, middlewares=(UnsafeMiddleware(),))
        result = runner.invoke(run_id, "topos.apply_edit_plan", {"plan": {}}, approval=None)

        assert result.is_denied
        assert result.error_code == "TOOL_DENIED"

    def test_input_validation_middleware_fails_closed(self, runner_with_run):
        _, orch, _, run_id, facade = runner_with_run
        runner = ToolInvocationRunner(
            orch,
            facade,
            middlewares=(ToolInputValidationMiddleware(),),
        )

        result = runner.invoke(run_id, "topos.search_assets", ["not", "an", "object"])

        assert result.failed
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"
        assert facade.calls == []

    def test_slow_legacy_facade_keeps_confirmed_outcome_with_timeout_diagnostic(self):
        # AUD-14726（锁定旧行为协议：原断言锁定"已成功副作用被改判 TOOL_TIMEOUT
        # 失败"的 bug）：慢但**已完成**的调用保留真实 outcome 与数据，超时事实
        # 作为附加 warning diagnostic 携带——报失败会诱发重试/重复提交。
        orch = AgentOrchestrator()
        run_id = orch.start_task(AgentTask(session_id="s1", title="timeout"))
        clock = [0.0]

        class SlowFacade:
            def invoke(self, name, args, *, approval=None):
                clock[0] = 1.0
                return {"ok": True, "value": "late"}

        runner = ToolInvocationRunner(
            orch,
            SlowFacade(),
            time_fn=lambda: clock[0],
            timeout_policy=ToolTimeoutPolicy(default_timeout_seconds=0.1),
        )
        result = runner.invoke(run_id, "topos.search_assets")
        assert result.succeeded
        assert result.data["value"] == "late"
        assert result.error_code != "TOOL_TIMEOUT"
        assert any(d.code == "TOOL_TIMEOUT" for d in result.diagnostics)

    def test_unknown_outcome_after_deadline_is_tool_timeout_failure(self):
        # AUD-14726：结果未知（异常路径 + 已过 deadline）才标 TOOL_TIMEOUT 失败。
        orch = AgentOrchestrator()
        run_id = orch.start_task(AgentTask(session_id="s1", title="boom"))
        clock = [0.0]

        class SlowThrowingFacade:
            def invoke(self, name, args, *, approval=None):
                clock[0] = 1.0
                raise RuntimeError("timed out mid-flight")

        runner = ToolInvocationRunner(
            orch,
            SlowThrowingFacade(),
            time_fn=lambda: clock[0],
            timeout_policy=ToolTimeoutPolicy(default_timeout_seconds=0.1),
        )
        result = runner.invoke(run_id, "topos.search_assets")
        assert result.failed
        assert result.error_code == "TOOL_TIMEOUT"
