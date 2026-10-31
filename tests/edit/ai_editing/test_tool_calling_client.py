"""M10 LLM tool-calling loop 单元测试（§16.2 Mode B / §4.1 / §6.2 规则 7）。

覆盖：
- ToolCallRequest / ToolCallResponse 数据合同
- DeterministicToolProvider：脚本消耗 → finish
- run_tool_loop：收敛路径 / 多轮 tool calls / 预算上限 / 模型异常
- 每个 tool call 经 invoke_fn + 审计（ToolCallLog）
- tool result 回填 messages（role=tool）
- approval 由宿主注入（模型无法伪造）

红线对照：
- 复用 ToolFacade（§3.2 / §16.5 row1）
- 确定性 stub（§16.2 Mode B 测试）
- 预算/循环上限（§6.2 规则 7，防无限自我反思）
- tool 输出是非可信数据（§1.2 不变量 #2）
- approval 不来自模型（§0 红线第二条）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.tool_call_log import ToolCallLog
from src.features.edit.ai_editing.tool_calling_client import (
    DeterministicToolProvider,
    ToolCallRequest,
    ToolCallResponse,
    ToolLoopBudget,
    run_tool_loop,
)


# ================================================================
# 数据合同
# ================================================================


class TestDataContract:
    def test_tool_call_request_defaults(self):
        r = ToolCallRequest(call_id="c1", tool_name="topos.search_assets")
        assert r.arguments == {}

    def test_tool_call_response_finish_default(self):
        r = ToolCallResponse(text="done")
        assert r.stop_reason == "stop"
        assert r.finish is False
        assert r.tool_calls == ()


# ================================================================
# DeterministicToolProvider
# ================================================================


class TestDeterministicProvider:
    def test_consumes_script_in_order(self):
        p = DeterministicToolProvider(script=(
            ("topos.search_assets", {"text": "产品"}),
            ("topos.get_asset_analysis", {"asset_id": "a1"}),
        ))
        r1 = p.complete_with_tools([], [])
        assert len(r1.tool_calls) == 1
        assert r1.tool_calls[0].tool_name == "topos.search_assets"
        assert r1.tool_calls[0].arguments == {"text": "产品"}
        assert r1.finish is False

        r2 = p.complete_with_tools([], [])
        assert r2.tool_calls[0].tool_name == "topos.get_asset_analysis"

    def test_finish_when_script_exhausted(self):
        p = DeterministicToolProvider(script=(("topos.search_assets", {}),))
        p.complete_with_tools([], [])  # 消耗第一条
        r = p.complete_with_tools([], [])
        assert r.finish is True
        assert r.tool_calls == ()
        assert r.stop_reason == "stop"

    def test_reset(self):
        p = DeterministicToolProvider(script=(("t", {}),))
        p.complete_with_tools([], [])
        p.reset()
        r = p.complete_with_tools([], [])
        assert len(r.tool_calls) == 1


# ================================================================
# run_tool_loop 收敛路径
# ================================================================


def _invoke_ok(name, args, **kw):
    return {"ok": True, "tool": name, "echo_args": args}


class TestRunToolLoop:
    def test_no_tool_calls_immediately_converges(self):
        """模型一开始就 finish → 0 轮 tool call。"""
        client = DeterministicToolProvider(script=())  # 立即 finish
        result = run_tool_loop(client, _invoke_ok, [{"role": "user", "content": "hi"}], [])
        assert result.converged is True
        assert result.tool_calls_made == 0
        assert result.iterations == 1
        assert "最终回复" in result.final_text

    def test_single_tool_call_then_converge(self):
        client = DeterministicToolProvider(script=(
            ("topos.search_assets", {"text": "产品"}),
        ))
        log = ToolCallLog()
        result = run_tool_loop(
            client, _invoke_ok, [{"role": "user", "content": "找产品"}], [],
            call_log=log,
        )
        assert result.converged is True
        assert result.tool_calls_made == 1
        assert len(result.tool_results) == 1
        assert result.tool_results[0]["tool"] == "topos.search_assets"

    def test_multiple_tool_calls_then_converge(self):
        client = DeterministicToolProvider(script=(
            ("topos.search_assets", {"text": "产品"}),
            ("topos.get_asset_analysis", {"asset_id": "a1"}),
            ("topos.validate_edit_plan", {"plan": {}}),
        ))
        result = run_tool_loop(client, _invoke_ok, [{"role": "user", "content": "x"}], [])
        assert result.tool_calls_made == 3
        assert result.converged is True

    def test_each_call_audited(self):
        """§4.2 step 1：每个 tool call 经 ToolCallLog 审计。"""
        client = DeterministicToolProvider(script=(
            ("topos.search_assets", {}),
            ("topos.get_capabilities", {}),
        ))
        log = ToolCallLog()
        run_tool_loop(
            client, _invoke_ok, [{"role": "user", "content": "x"}], [],
            call_log=log, client_session_id="loop-1", skill_id="s1",
        )
        assert log.count == 2
        for rec in log.records():
            assert rec.client == "tool_calling_loop"
            assert rec.client_session_id == "loop-1"
            assert rec.skill_id == "s1"

    def test_tool_result_backfilled_to_messages(self):
        """tool result 回填 messages（role=tool）供下轮使用。"""
        captured_messages: list = []

        class CapturingProvider:
            def __init__(self):
                self._done = False

            def complete_with_tools(self, messages, tools, **kw):
                captured_messages.append(list(messages))
                if not self._done:
                    self._done = True
                    return ToolCallResponse(
                        tool_calls=(ToolCallRequest(call_id="c1", tool_name="topos.search_assets"),),
                        stop_reason="tool_use",
                    )
                return ToolCallResponse(text="done", finish=True)

        run_tool_loop(CapturingProvider(), _invoke_ok, [{"role": "user", "content": "x"}], [])
        # 第二轮看到的 messages 应含 role=tool 的回填
        assert len(captured_messages) >= 2
        second_turn = captured_messages[1]
        tool_msgs = [m for m in second_turn if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        assert tool_msgs[0]["tool_call_id"] == "c1"

    def test_max_iterations_budget(self):
        """§6.2 规则 7：超 max_iterations → 终止（防无限循环）。"""
        # provider 永远返回 tool call，不 finish
        class InfiniteProvider:
            def complete_with_tools(self, messages, tools, **kw):
                return ToolCallResponse(
                    tool_calls=(ToolCallRequest(call_id="c", tool_name="topos.search_assets"),),
                    stop_reason="tool_use",
                )

        result = run_tool_loop(
            InfiniteProvider(), _invoke_ok, [{"role": "user", "content": "x"}], [],
            budget=ToolLoopBudget(max_iterations=3, max_tool_calls=10),
        )
        assert result.terminated_reason == "max_iterations"
        assert any(d["code"] == "TAEP_QUOTA_EXCEEDED" for d in result.diagnostics)

    def test_max_tool_calls_budget(self):
        """§6.2 规则 7：超 max_tool_calls → 终止。"""
        class InfiniteProvider:
            def complete_with_tools(self, messages, tools, **kw):
                return ToolCallResponse(
                    tool_calls=(ToolCallRequest(call_id="c", tool_name="topos.search_assets"),),
                    stop_reason="tool_use",
                )

        result = run_tool_loop(
            InfiniteProvider(), _invoke_ok, [{"role": "user", "content": "x"}], [],
            budget=ToolLoopBudget(max_iterations=100, max_tool_calls=4),
        )
        assert result.terminated_reason == "max_iterations"
        assert result.tool_calls_made == 4

    def test_model_exception_visible(self):
        """模型抛异常 → terminated=error，诊断可见（不吞）。"""
        class FailingProvider:
            def complete_with_tools(self, messages, tools, **kw):
                raise RuntimeError("model endpoint down")

        result = run_tool_loop(FailingProvider(), _invoke_ok, [{"role": "user", "content": "x"}], [])
        assert result.terminated_reason == "error"
        assert any("model endpoint down" in d.get("message", "") or "model endpoint down" in str(d.get("details", "")) for d in result.diagnostics)

    def test_cancel_requested_before_first_iteration(self):
        """红线修复（§0 第三条）：cancel_requested=True 在首轮 model 调用前终止。

        terminated_reason=="cancelled"，iterations=0，无 tool call 执行。
        """
        called: list = []

        class TrackingProvider:
            def complete_with_tools(self, messages, tools, **kw):
                called.append(1)  # 不应被调
                return ToolCallResponse(text="x")

        result = run_tool_loop(
            TrackingProvider(), _invoke_ok, [{"role": "user", "content": "x"}], [],
            cancel_requested=lambda: True,
        )
        assert result.terminated_reason == "cancelled"
        assert result.iterations == 0
        assert result.tool_calls_made == 0
        assert called == []  # model 未被调用

    def test_cancel_mid_loop_after_some_calls(self):
        """取消信号在某个 tool call 之前触发 → 中断，已执行的 tool call 仍计入审计。"""
        call_count = {"n": 0}

        class TwoThenMore:
            def complete_with_tools(self, messages, tools, **kw):
                call_count["n"] += 1
                # 前 2 轮各返回 1 个 tool call，第 3 轮 finish
                if call_count["n"] <= 2:
                    return ToolCallResponse(
                        tool_calls=(ToolCallRequest(call_id="c", tool_name="topos.search_assets"),),
                        stop_reason="tool_use",
                    )
                return ToolCallResponse(text="done", finish=True)

        # 第 2 个 tool call 之前 cancel
        gate = {"cancel": False}

        def cancel_requested():
            return gate["cancel"]

        def invoke(name, args, **kw):
            gate["cancel"] = True  # 第 1 个 tool call 后置取消
            return {"ok": True}

        result = run_tool_loop(
            TwoThenMore(), invoke, [{"role": "user", "content": "x"}], [],
            cancel_requested=cancel_requested,
        )
        assert result.terminated_reason == "cancelled"
        assert result.tool_calls_made == 1  # 第 1 个执行了，第 2 个之前被取消

    def test_max_iterations_zero_does_not_crash(self):
        """max_iterations=0 → range 空，循环不进，iterations=0，不崩（for-else 触发）。"""
        class Dummy:
            def complete_with_tools(self, messages, tools, **kw):
                raise AssertionError("不应被调用")

        result = run_tool_loop(
            Dummy(), _invoke_ok, [{"role": "user", "content": "x"}], [],
            budget=ToolLoopBudget(max_iterations=0, max_tool_calls=0),
        )
        assert result.iterations == 0
        assert result.terminated_reason == "max_iterations"
        assert any(d["code"] == "TAEP_QUOTA_EXCEEDED" for d in result.diagnostics)

    def test_approval_grant_id_passed_through(self):
        """approval 由宿主注入：approval_grant_id 透传到 invoke_fn 的 approval 参数。"""
        captured: dict = {}

        def invoke(name, args, **kw):
            captured.update(kw)
            return {"ok": True}

        client = DeterministicToolProvider(script=(("topos.apply_edit_plan", {}),))
        log = ToolCallLog()
        run_tool_loop(
            client, invoke, [{"role": "user", "content": "x"}], [],
            approval_grant_id="grant-xyz", call_log=log,
        )
        # approval 转发到 invoke_fn（ToolFacade.invoke 的授权参数）
        assert captured.get("approval") == "grant-xyz"
        # 同时记入审计（approval_grant_id 字段）
        assert log.records()[0].approval_grant_id == "grant-xyz"

    def test_result_to_dict_json_serializable(self):
        import json
        client = DeterministicToolProvider(script=(("topos.search_assets", {}),))
        result = run_tool_loop(client, _invoke_ok, [{"role": "user", "content": "x"}], [])
        json.dumps(result.to_dict())  # 不抛
