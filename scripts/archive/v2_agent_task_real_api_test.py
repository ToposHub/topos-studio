#!/usr/bin/env python3
"""Real-API test for the autonomous AgentTaskExecutor.

Drives the full plan → approve → execute → verify → recover lifecycle with a
real language model (glm-5.2 or whatever the active profile is) and a simulated
tool facade that mimics a real editing session.  This validates that:

1. The LLM can produce a valid JSON plan from the planning prompt.
2. The executor correctly parses, executes, and verifies the plan.
3. Read-only tools are called and their results are fed back.
4. The verification LLM call produces a meaningful verdict.
5. The summary LLM call produces a user-facing report.
6. Events are emitted correctly through the AgentRun lifecycle.

The tool facade is **simulated** (no real EditSession is open), so write tools
return canned success/denied responses.  This is deliberate: the test validates
the *agent loop logic*, not the tool execution itself (which is already covered
by the ToolFacade's own tests).

Run::

    PYTHONPATH=$(pwd) PYTHONUNBUFFERED=1 .venv/bin/python -u scripts/v2_agent_task_real_api_test.py
"""

from __future__ import annotations

import json
import sys
import time

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.agent_task_executor import (
    AgentTaskExecutor,
    AgentTaskResult,
    AlwaysApproveBridge,
    TaskPlan,
)
from src.features.edit.ai_editing.credential_store import HybridCredentialStore
from src.features.edit.ai_editing.llm_client import make_client
from src.features.edit.ai_editing.provider_profiles import ProfileRegistry
from src.features.edit.ai_editing.tool_facade import (
    ToolDefinition,
    ToolPermission,
)

results: list[tuple[str, bool, str]] = []


def _banner(text: str) -> None:
    print(f"\n{'=' * 72}\n  {text}\n{'=' * 72}", flush=True)


def _record(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# --------------------------------------------------------------------------- #
# Simulated tool facade (mimics a real editing session without Qt/EditSession)
# --------------------------------------------------------------------------- #


class SimulatedToolFacade:
    """A fake facade that returns realistic-looking tool results."""

    def __init__(self):
        self.invoke_log: list[tuple[str, dict]] = []

    def list_tools(self, *, max_permission=ToolPermission.READ):
        return [
            ToolDefinition(
                name="topos.get_project_snapshot",
                permission=ToolPermission.READ,
                description="返回当前项目的只读快照（时间线、素材、轨道）",
                input_schema={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="topos.search_assets",
                permission=ToolPermission.READ,
                description="按文本搜索项目中的素材",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            ),
            ToolDefinition(
                name="topos.validate_edit_plan",
                permission=ToolPermission.PREVIEW,
                description="校验编辑计划，返回 diff + diagnostics",
                input_schema={
                    "type": "object",
                    "required": ["plan"],
                    "properties": {"plan": {"type": "object"}},
                },
            ),
            ToolDefinition(
                name="topos.apply_edit_plan",
                permission=ToolPermission.WRITE,
                description="原子提交编辑计划到时间线",
                input_schema={
                    "type": "object",
                    "required": ["plan"],
                    "properties": {"plan": {"type": "object"}},
                },
            ),
        ]

    def invoke(self, tool_name, args=None, *, approval=None, **kw):
        self.invoke_log.append((tool_name, args or {}))
        if tool_name == "topos.get_project_snapshot":
            return {
                "ok": True,
                "data": {
                    "project_id": "demo-project",
                    "timelines": [
                        {"id": "timeline-1", "name": "主时间线",
                         "clip_count": 5, "duration_seconds": 120},
                    ],
                    "assets": [
                        {"id": "asset-1", "name": "sunset.mp4", "kind": "video"},
                        {"id": "asset-2", "name": "intro.png", "kind": "image"},
                    ],
                },
            }
        if tool_name == "topos.search_assets":
            query = (args or {}).get("query", "")
            return {
                "ok": True,
                "data": {
                    "results": [
                        {"id": "asset-1", "name": f"sunset_{query}.mp4",
                         "kind": "video", "score": 0.92},
                    ],
                },
            }
        if tool_name == "topos.validate_edit_plan":
            return {
                "ok": True,
                "data": {
                    "valid": True,
                    "diff_summary": "2 clips modified, 1 effect added",
                    "diagnostics": [],
                },
            }
        if tool_name == "topos.apply_edit_plan":
            if approval is None:
                return {"ok": False, "diagnostics": [
                    {"code": "PERMISSION_DENIED", "message": "no approval grant"}]}
            return {
                "ok": True,
                "data": {
                    "new_revision": 42,
                    "undo_group": "undo-abc",
                    "applied_operations": 3,
                },
            }
        return {"ok": False, "diagnostics": [{"code": "UNKNOWN_TOOL",
                                               "message": f"unknown: {tool_name}"}]}


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def _run_test(
    label: str,
    executor: AgentTaskExecutor,
    orch: AgentOrchestrator,
    *,
    user_goal: str,
    messages: list[dict] | None = None,
) -> tuple[bool, AgentTaskResult]:
    """Run one agent task test and print detailed diagnostics."""

    _banner(f"测试: {label}")
    print(f"  用户目标: {user_goal}", flush=True)
    t0 = time.perf_counter()

    plans_received: list[TaskPlan] = []
    deltas: list[str] = []

    try:
        result = executor.execute(
            session_id="agent-test",
            user_goal=user_goal,
            messages=messages or [{"role": "user", "content": user_goal}],
            temperature=0.4,
            max_tokens=4096,
            on_delta=deltas.append,
            on_plan=plans_received.append,
        )
    except Exception as exc:
        print(f"  ✗ 异常: {type(exc).__name__}: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        return False, None

    elapsed = time.perf_counter() - t0
    events = [e.kind for e in orch.get_events(result.run_id)]

    print(f"\n  终态: {result.terminal_state}  耗时: {elapsed:.1f}s", flush=True)
    print(f"  事件流: {' → '.join(events)}", flush=True)

    if plans_received:
        plan = plans_received[0]
        print(f"  计划: {plan.step_count} 步 | {plan.rationale[:100]}", flush=True)
        for step in plan.steps:
            write_tag = " [写]" if step.is_write else ""
            print(f"    {step.step_id}: {step.description}{write_tag} → {step.tool_name}")

    if result.step_results:
        for sr in result.step_results:
            mark = "✓" if sr.succeeded else "✗"
            print(f"    {mark} {sr.step_id} ({sr.tool_name}): {sr.outcome}"
                  + (f" | {sr.error[:60]}" if sr.error else ""))

    print(f"  验证: {'通过' if result.verification_passed else '未通过'}"
          f" | {result.verification_notes[:100]}", flush=True)
    if result.usage:
        ap = getattr(result.usage, "actual_prompt", None)
        ac = getattr(result.usage, "actual_completion", None)
        total = (ap or 0) + (ac or 0) if ap or ac else None
        print(f"  Token: prompt={ap} completion={ac} total={total}", flush=True)
    if result.replan_count:
        print(f"  重规划次数: {result.replan_count}", flush=True)

    reply_preview = (result.reply or "").strip().replace("\n", " ")[:200]
    print(f"  回复: {reply_preview!r}", flush=True)

    ok = result.terminal_state == AgentRunState.SUCCEEDED and result.verification_passed
    return ok, result


def main() -> int:
    _banner("V2 自主多步执行 — AgentTaskExecutor 真实 API 测试")

    store = HybridCredentialStore(prefer="file")
    registry = ProfileRegistry()
    if not registry.load_from_user_file():
        print("无法加载 ~/.topos/ai_profiles.json，终止。")
        return 2

    active = registry.active_profile
    print(f"激活 Profile: {active.display_name} ({active.model_id})")

    # ---- Test 1: Read-only exploration task ----
    orch1 = AgentOrchestrator()
    facade1 = SimulatedToolFacade()
    executor1 = AgentTaskExecutor(
        orch1,
        client_factory=lambda p: make_client(p, store),
        tool_facade=facade1,
        profile=active,
        approval_bridge=AlwaysApproveBridge(),
    )
    ok1, result1 = _run_test(
        "纯读任务 · 项目结构探索",
        executor1, orch1,
        user_goal="查看当前项目的结构，告诉我有几条时间线、多少素材，然后总结一下。",
    )
    # In the simulated env, verification may pass or fail depending on LLM mood.
    # The key success criterion is: the loop ran (plan → execute → verify → terminal).
    loop_ok1 = (
        result1 is not None
        and result1.terminal_state in (AgentRunState.SUCCEEDED, AgentRunState.FAILED)
        and result1.plan is not None
        and result1.plan.step_count > 0
    )
    _record("纯读任务 · Agent 循环完整跑通", loop_ok1,
            f"终态={result1.terminal_state if result1 else '?'}"
            f" 步数={result1.plan.step_count if result1 and result1.plan else '?'}"
            f" 工具调用数: {len(facade1.invoke_log)}")
    # Must have called at least get_project_snapshot
    tool_names = [c[0] for c in facade1.invoke_log]
    has_snapshot = "topos.get_project_snapshot" in tool_names
    _record("调用了 get_project_snapshot", has_snapshot)

    # ---- Test 2: Multi-step with write ----
    orch2 = AgentOrchestrator()
    facade2 = SimulatedToolFacade()
    executor2 = AgentTaskExecutor(
        orch2,
        client_factory=lambda p: make_client(p, store),
        tool_facade=facade2,
        profile=active,
        approval_bridge=AlwaysApproveBridge(),
    )
    ok2, result2 = _run_test(
        "多步任务 · 查看 + 校验 + 应用编辑",
        executor2, orch2,
        user_goal=(
            "先看一下项目的当前状态，然后创建一个编辑计划来给主时间线添加一个色彩效果，"
            "先校验计划再应用到时间线上。"
        ),
    )
    # The loop ran through WAITING_APPROVAL → write tool → verify. In a real
    # editing session, the project state would change and verification would
    # pass. In this simulated env, verification may reject — both are valid.
    loop_ok2 = (
        result2 is not None
        and result2.terminal_state in (AgentRunState.SUCCEEDED, AgentRunState.FAILED)
        and result2.plan is not None
        and any(s.is_write for s in result2.plan.steps)
    )
    _record("多步任务 · 含写步骤的 Agent 循环", loop_ok2,
            f"终态={result2.terminal_state if result2 else '?'}"
            f" 写步骤={'有' if result2 and result2.plan and any(s.is_write for s in result2.plan.steps) else '无'}"
            f" 工具调用数: {len(facade2.invoke_log)}")
    has_validate = "topos.validate_edit_plan" in [c[0] for c in facade2.invoke_log]
    _record("模型规划了 validate_edit_plan", has_validate)

    # ---- Test 3: Multi-turn context ----
    orch3 = AgentOrchestrator()
    facade3 = SimulatedToolFacade()
    executor3 = AgentTaskExecutor(
        orch3,
        client_factory=lambda p: make_client(p, store),
        tool_facade=facade3,
        profile=active,
        approval_bridge=AlwaysApproveBridge(),
    )
    ok3, result3 = _run_test(
        "多轮上下文 · 搜索素材",
        executor3, orch3,
        user_goal="搜索项目中所有视频素材，查看项目的整体结构，然后告诉我素材列表。",
        messages=[
            {"role": "user", "content": "我有一个包含日落视频的项目。"},
            {"role": "assistant", "content": "好的，我可以帮你分析项目中的素材。"},
            {"role": "user", "content": "搜索项目中所有视频素材，查看项目的整体结构，然后告诉我素材列表。"},
        ],
    )
    _record("多轮上下文 · 搜索素材", ok3,
            f"工具调用数: {len(facade3.invoke_log)}")

    # ---- Test 4: Verification rejection (hard goal → FAILED, safety net) ----
    orch4 = AgentOrchestrator()
    facade4 = SimulatedToolFacade()
    executor4 = AgentTaskExecutor(
        orch4,
        client_factory=lambda p: make_client(p, store),
        tool_facade=facade4,
        profile=active,
        approval_bridge=AlwaysApproveBridge(),
    )
    ok4, result4 = _run_test(
        "验证安全网 · 不可达目标 → FAILED",
        executor4, orch4,
        user_goal=(
            "搜索项目中所有视频素材，分析它们的逐帧画质，"
            "然后为每个素材生成一份详细的技术分析报告。"
        ),
    )
    # This goal is NOT achievable with read-only tools (no quality analysis tool).
    # The verifier should catch this and the run should FAIL (not fake success).
    # This demonstrates the verification safety net.
    safety_ok = (
        result4 is not None
        and result4.terminal_state == AgentRunState.FAILED
        and not result4.verification_passed
        and result4.replan_count >= 1  # at least one replan attempt
    )
    _record("验证安全网 · 不可达 → FAILED+重规划", safety_ok,
            f"终态={result4.terminal_state if result4 else '?'}"
            f" 重规划={result4.replan_count if result4 else '?'}")

    # ---- Summary ----
    _banner("汇总")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for label, ok, detail in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {label}" + (f" — {detail}" if detail else ""))
    print(f"\n  {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
