"""Autonomous multi-step task executor (plan → approve → execute → verify → recover).

This is the component that turns a high-level user goal ("给视频加字幕并调暖
色调") into a fully autonomous multi-step AgentRun: the model **plans** concrete
tool-callable steps, the user **approves** write operations through an
:class:`ApprovalBridge`, the executor **runs** each step via the
:class:`ToolFacade`, then the model **verifies** whether the goal was achieved
and **recovers** (one replan attempt) if it was not.

Design rules (hard guarantees):

- **is_write never trusted from model output.** Whether a step is a write is
  determined from the tool definition's ``ToolPermission``, not from the JSON
  the model produced.  The model cannot escalate a read tool to write.
- **ApprovalGrant is host-only.** The executor never mints grants; it obtains
  them from the injected :class:`ApprovalBridge`.  Model-supplied tokens in
  ``tool_args`` are discarded by the facade (unchanged).
- **Bounded.** ``max_steps`` (default 8), ``max_replans`` (default 1).  No
  infinite loop.
- **Cancel-aware.** Every phase boundary checks ``orchestrator.is_run_active``.
- **Empty plan = FAILED, not SUCCEEDED.** Never fake success.
- **Verification is a hard gate.** If the verifier says "not achieved", the run
  cannot be SUCCEEDED — it either replans or fails.
- **Tool output is untrusted.** Tool results are JSON-serialised before being
  fed back to the model; they cannot alter system instructions.

The executor is synchronous and Qt-free (run on a background thread, like
``ChatRunDriver``).  All progress is observable through ``AgentOrchestrator``
events.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
    AgentTask,
    AgentTaskKind,
    ContextUsage,
)
from src.features.edit.ai_editing.agent_orchestrator import (
    AgentOrchestrator,
    RunNotFoundError,
    RunNotTransitionableError,
)
from src.features.edit.ai_editing.capability_catalog import CapabilityCatalog
from src.features.edit.ai_editing.agent_task_prompts import (
    build_planning_system_prompt,
    build_planning_user_prompt,
    build_summary_prompt,
    build_verification_system_prompt,
)
from src.features.edit.ai_editing.context_budget import (
    estimate_window_usage,
    usage_from_llm,
)
from src.features.edit.ai_editing.llm_client import LLMClient, LLMError
from src.features.edit.ai_editing.tool_invocation import (
    find_tool_definition,
    tool_requires_approval,
    ToolInvocationRunner,
)
from src.features.edit.ai_editing.tool_operation_ledger import ToolOperationLedger
from src.features.edit.ai_editing.tool_result import ToolResult

#: Factory that builds an :class:`LLMClient` for a resolved profile.
ClientFactory = Callable[[Any], LLMClient | None]


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaskStep:
    """One allow-listed step in a task plan."""

    step_id: str
    description: str
    tool_name: str
    tool_args: dict[str, Any]
    expected_outcome: str = ""
    permission: str = ""

    @property
    def is_write(self) -> bool:
        """Whether this step needs host approval (looked up from tool definition).

        Never trusted from model JSON — always derived from the real
        :class:`ToolDefinition` registered in ``tool_facade``.
        """

        if self.permission:
            return self.permission in {"write", "expensive", "file_write"}
        return tool_requires_approval(self.tool_name)


@dataclass(frozen=True)
class TaskPlan:
    """A decomposed plan for one user goal."""

    plan_id: str
    goal: str
    steps: tuple[TaskStep, ...]
    rationale: str = ""

    @property
    def needs_approval(self) -> bool:
        return any(s.is_write for s in self.steps)

    @property
    def step_count(self) -> int:
        return len(self.steps)


@dataclass(frozen=True)
class StepResult:
    """Outcome of executing one step."""

    step_id: str
    tool_name: str
    succeeded: bool
    outcome: str  # "success" | "denied" | "failure"
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class AgentTaskResult:
    """Final outcome of an autonomous task execution."""

    run_id: str
    plan: TaskPlan | None = None
    step_results: tuple[StepResult, ...] = ()
    verification_passed: bool = False
    verification_notes: str = ""
    reply: str = ""
    terminal_state: str = ""
    usage: ContextUsage | None = None
    replan_count: int = 0


# --------------------------------------------------------------------------- #
# Approval bridge
# --------------------------------------------------------------------------- #


class ApprovalBridge(Protocol):
    """Obtain approval for a plan that contains write steps."""

    def request_approval(
        self, run_id: str, plan: TaskPlan,
    ) -> Any:
        """Return an approval object (e.g. ``ApprovalGrant``) if approved, ``None`` if denied."""
        ...


class AlwaysApproveBridge:
    """Test-only bridge: always approves with a sentinel object.

    The sentinel is passed as ``approval=`` to ``tool_facade.invoke()``.  In a
    real deployment, replace this with a bridge that issues actual
    :class:`ApprovalGrant` objects via the host's :class:`ApprovalService`.
    """

    _SENTINEL = "host-approved"

    def request_approval(self, run_id: str, plan: TaskPlan) -> Any:
        return self._SENTINEL


class NeverApproveBridge:
    """Bridge that always denies — used when autonomous mode is off."""

    def request_approval(self, run_id: str, plan: TaskPlan) -> Any:
        return None


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #


#: Maximum steps in one plan (enforced on top of the prompt instruction).
_MAX_STEPS = 10

#: Maximum replan attempts after verification failure.
_MAX_REPLANS = 1


class AgentTaskExecutor:
    """Run an autonomous multi-step task as a unified AgentRun."""

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        *,
        client_factory: ClientFactory,
        tool_facade: Any,
        profile: Any,
        approval_bridge: ApprovalBridge | None = None,
        operation_ledger: ToolOperationLedger | None = None,
        capability_catalog: CapabilityCatalog | None = None,
        tool_allowlist: tuple[str, ...] = (),
        max_steps: int = _MAX_STEPS,
        system_instruction: str = "",
    ) -> None:
        self._orchestrator = orchestrator
        self._client_factory = client_factory
        self._tool_facade = tool_facade
        self._profile = profile
        # Default: deny write steps unless a bridge is provided.
        self._approval_bridge = approval_bridge
        self._operation_ledger = operation_ledger
        self._capability_catalog = capability_catalog or getattr(
            tool_facade, "capability_catalog", None,
        )
        if max_steps <= 0:
            raise ValueError("agent task max_steps must be positive")
        self._tool_allowlist = frozenset(str(name) for name in tool_allowlist if str(name).strip())
        self._max_steps = min(_MAX_STEPS, int(max_steps))
        self._system_instruction = str(system_instruction or "").strip()

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def execute(
        self,
        *,
        session_id: str,
        user_goal: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.4,
        max_tokens: int = 4096,
        on_delta: Callable[[str], None] | None = None,
        on_plan: Callable[[TaskPlan], None] | None = None,
    ) -> AgentTaskResult:
        """Execute an autonomous multi-step task end-to-end.

        Flow:
        1. Create AgentRun (mode="agent")
        2. Gather context (project snapshot)
        3. Plan (LLM call → TaskPlan)
        4. If plan needs approval → WAITING_APPROVAL → bridge
        5. Execute each step via ToolFacade
        6. Verify (LLM call → achieved?)
        7. If verification fails → replan once
        8. Summarise + finish

        AUD-12800：取消/审批竞态下（如 ``request_approval`` 阻塞等待用户期间
        UI 线程已把 run 置为 CANCELLED）orchestrator 的状态转换会抛
        ``RunNotTransitionableError``/``ValueError``。此类竞态异常不得逃逸
        ``execute()``（调用方 worker 线程对 executor.execute 无防护，未处理
        异常会破坏 AgentTaskResult 契约）——顶层兜底为按 run 实际状态收敛的
        AgentTaskResult。
        """

        task = AgentTask(
            session_id=session_id,
            title=user_goal.strip().splitlines()[0][:60] if user_goal.strip() else "agent task",
            user_goal=user_goal,
            kind=AgentTaskKind.AGENT,
        )
        run_id = self._orchestrator.start_task(task, mode="agent")
        try:
            return self._execute_run(
                run_id,
                user_goal=user_goal,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                on_delta=on_delta,
                on_plan=on_plan,
            )
        except (RunNotTransitionableError, RunNotFoundError, ValueError) as exc:
            reason = f"执行中止（状态竞态）: {str(exc)[:200]}"
            run = self._orchestrator.get_run(run_id)
            terminal = (
                run.state if run is not None and run.is_terminal
                else AgentRunState.FAILED
            )
            if run is None or not run.is_terminal:
                try:
                    self._orchestrator.fail(run_id, reason=reason)
                except (RunNotTransitionableError, RunNotFoundError, ValueError):
                    # 双重竞态：run 已在别处终结；下方结果按实际状态返回
                    pass
            return AgentTaskResult(
                run_id=run_id, terminal_state=terminal, reply=reason,
            )

    def _execute_run(
        self,
        run_id: str,
        *,
        user_goal: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.4,
        max_tokens: int = 4096,
        on_delta: Callable[[str], None] | None = None,
        on_plan: Callable[[TaskPlan], None] | None = None,
    ) -> AgentTaskResult:
        """Run body of :meth:`execute` for an already-created *run_id*."""

        client = self._client_factory(self._profile) if self._profile else None
        if client is None:
            reason = "无法为当前 Profile 构造模型客户端"
            self._orchestrator.fail(run_id, reason=reason)
            return AgentTaskResult(
                run_id=run_id, terminal_state=AgentRunState.FAILED, reply=reason,
            )

        window_limit = self._window_limit_for(self._profile)
        estimated = estimate_window_usage(messages, window_limit=window_limit)

        all_usage: list[Any] = []
        replan_count = 0
        failure_context = ""
        first_iteration = True

        while True:
            # ---- cancellation check ----
            if not self._orchestrator.is_run_active(run_id):
                return AgentTaskResult(
                    run_id=run_id, terminal_state=AgentRunState.CANCELLED,
                )

            # ---- gather context ----
            # First iteration: GATHERING_CONTEXT → PLANNING.
            # Replan iterations: already in EXECUTING, skip the transition.
            if first_iteration:
                if self._orchestrator.is_run_active(run_id):
                    self._orchestrator.mark_context_ready(run_id)

            project_snapshot = self._gather_context(run_id)

            if not self._orchestrator.is_run_active(run_id):
                return AgentTaskResult(
                    run_id=run_id, terminal_state=AgentRunState.CANCELLED,
                )

            # ---- plan (LLM call) ----
            plan = self._plan(
                run_id, client, user_goal, project_snapshot,
                failure_context=failure_context,
                temperature=temperature, max_tokens=max_tokens,
                on_delta=on_delta,
            )
            self._collect_usage(client, all_usage)

            if plan is None:
                reason = "规划失败：模型未返回有效的 JSON 计划"
                self._orchestrator.fail(run_id, reason=reason)
                return AgentTaskResult(
                    run_id=run_id, terminal_state=AgentRunState.FAILED, reply=reason,
                )

            if plan.step_count == 0:
                reason = "规划失败：返回了空计划"
                self._orchestrator.fail(run_id, reason=reason)
                return AgentTaskResult(
                    run_id=run_id, terminal_state=AgentRunState.FAILED, reply=reason,
                )

            if on_plan is not None:
                try:
                    on_plan(plan)
                except Exception:  # noqa: BLE001
                    pass

            # ---- transition to EXECUTING ----
            approval_obj: Any = None
            if plan.needs_approval:
                approval_obj = self._request_approval(run_id, plan)
                # AUD-12800：等待审批期间 run 可能已被取消（终态），先于
                # "拒绝"分支判定——竞态取消按 CANCELLED 收尾，不误报为拒绝。
                if not self._orchestrator.is_run_active(run_id):
                    return AgentTaskResult(
                        run_id=run_id, plan=plan,
                        terminal_state=AgentRunState.CANCELLED,
                    )
                if approval_obj is None:
                    reason = "用户拒绝了写操作审批"
                    self._orchestrator.fail(run_id, reason=reason)
                    return AgentTaskResult(
                        run_id=run_id, plan=plan,
                        terminal_state=AgentRunState.FAILED, reply=reason,
                    )
            elif first_iteration:
                # No write steps on first iteration: PLANNING → EXECUTING.
                if self._orchestrator.is_run_active(run_id):
                    self._orchestrator.mark_plan_ready(run_id)
            # Replan without write steps: already in EXECUTING, nothing to do.

            # ---- execute steps ----
            if not self._orchestrator.is_run_active(run_id):
                return AgentTaskResult(
                    run_id=run_id, plan=plan,
                    terminal_state=AgentRunState.CANCELLED,
                )

            step_results = self._execute_steps(
                run_id, plan, approval_obj,
            )

            if not self._orchestrator.is_run_active(run_id):
                return AgentTaskResult(
                    run_id=run_id, plan=plan,
                    step_results=tuple(step_results),
                    terminal_state=AgentRunState.CANCELLED,
                )

            # ---- verify (LLM call while still in EXECUTING) ----
            final_snapshot = self._gather_context(run_id)
            verification = self._verify(
                run_id, client, user_goal, step_results, final_snapshot,
                temperature=temperature, max_tokens=max_tokens,
            )
            self._collect_usage(client, all_usage)

            if not self._orchestrator.is_run_active(run_id):
                return AgentTaskResult(
                    run_id=run_id, plan=plan,
                    step_results=tuple(step_results),
                    terminal_state=AgentRunState.CANCELLED,
                )

            # ---- recover or finish ----
            if verification["achieved"]:
                # Success! Summarise, then transition VERIFYING → SUCCEEDED.
                summary = self._summarize(
                    client, user_goal, step_results, verification["notes"],
                    temperature=temperature, max_tokens=max_tokens,
                    on_delta=on_delta,
                )
                self._collect_usage(client, all_usage)

                final_usage = self._merge_usage(all_usage, estimated)
                if final_usage is not None:
                    self._orchestrator.record_usage(run_id, final_usage)

                # EXECUTING → VERIFYING → SUCCEEDED.
                # AUD-12800：summarize 的 LLM 调用期间 run 可被取消——
                # 终态检查先行，避免对 CANCELLED run 调 grant 类转换抛异常。
                if not self._orchestrator.is_run_active(run_id):
                    return AgentTaskResult(
                        run_id=run_id, plan=plan,
                        step_results=tuple(step_results),
                        terminal_state=AgentRunState.CANCELLED,
                    )
                self._orchestrator.begin_verification(run_id)
                self._orchestrator.finish_verification(run_id, success=True)

                return AgentTaskResult(
                    run_id=run_id,
                    plan=plan,
                    step_results=tuple(step_results),
                    verification_passed=True,
                    verification_notes=verification["notes"],
                    reply=summary,
                    terminal_state=AgentRunState.SUCCEEDED,
                    usage=final_usage,
                    replan_count=replan_count,
                )

            # Verification failed — replan?
            if replan_count < _MAX_REPLANS:
                replan_count += 1
                failure_context = verification["notes"]
                first_iteration = False
                # Stay in EXECUTING; loop back to planning with failure context.
                continue

            # Out of replans — transition to VERIFYING then FAILED.
            reason = f"目标未达成：{verification['notes']}"
            final_usage = self._merge_usage(all_usage, estimated)
            if final_usage is not None:
                self._orchestrator.record_usage(run_id, final_usage)
            # AUD-12800：同成功路径——终态检查先行，竞态取消按 CANCELLED 收尾。
            if not self._orchestrator.is_run_active(run_id):
                return AgentTaskResult(
                    run_id=run_id, plan=plan,
                    step_results=tuple(step_results),
                    terminal_state=AgentRunState.CANCELLED,
                    usage=final_usage,
                    replan_count=replan_count,
                )
            self._orchestrator.begin_verification(run_id)
            self._orchestrator.finish_verification(run_id, success=False,
                                                    reason=reason)
            return AgentTaskResult(
                run_id=run_id,
                plan=plan,
                step_results=tuple(step_results),
                verification_passed=False,
                verification_notes=verification["notes"],
                reply=reason,
                terminal_state=AgentRunState.FAILED,
                usage=final_usage,
                replan_count=replan_count,
            )

    # ------------------------------------------------------------------ #
    # phase: gather context
    # ------------------------------------------------------------------ #

    def _gather_context(self, run_id: str) -> dict[str, Any]:
        """Call ``get_project_snapshot`` to gather project state for planning."""

        try:
            result = self._tool_facade.invoke("topos.get_project_snapshot", {})
            if isinstance(result, dict) and result.get("ok"):
                return result.get("data", result)
            return {"available": False, "note": "project snapshot unavailable"}
        except Exception:  # noqa: BLE001
            return {"available": False, "note": "project snapshot failed"}

    # ------------------------------------------------------------------ #
    # phase: plan
    # ------------------------------------------------------------------ #

    def _plan(
        self,
        run_id: str,
        client: Any,
        goal: str,
        project_snapshot: dict[str, Any],
        *,
        failure_context: str = "",
        temperature: float = 0.4,
        max_tokens: int = 4096,
        on_delta: Callable[[str], None] | None = None,
    ) -> TaskPlan | None:
        """Call the LLM to decompose the goal into a :class:`TaskPlan`."""

        tools = self._get_tool_catalog()
        system_prompt = build_planning_system_prompt(tools, project_snapshot)
        if self._system_instruction:
            system_prompt = self._system_instruction + "\n\n" + system_prompt
        user_prompt = build_planning_user_prompt(goal, failure_context)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Emit planning event (replan indicator only; RUN_PLAN_STARTED was
        # already emitted by mark_context_ready's state transition).
        if failure_context:
            self._emit(run_id, AgentEventKind.RUN_TOOL_PROGRESS,
                       {"phase": "replan", "reason": failure_context[:200]})

        try:
            raw = client.complete(messages, temperature=temperature, max_tokens=max_tokens)
        except LLMError:
            self._emit(run_id, AgentEventKind.RUN_PLAN_READY, {"ok": False})
            return None
        except Exception:  # noqa: BLE001
            self._emit(run_id, AgentEventKind.RUN_PLAN_READY, {"ok": False})
            return None

        if on_delta and raw:
            try:
                on_delta(f"[规划完成] {raw[:200]}...\n")
            except Exception:  # noqa: BLE001
                pass

        plan = self._parse_plan_json(raw, goal)
        self._emit(run_id, AgentEventKind.RUN_PLAN_READY, {
            "ok": plan is not None,
            "step_count": plan.step_count if plan else 0,
        })
        return plan

    def _parse_plan_json(self, raw: str, goal: str) -> TaskPlan | None:
        """Parse the model's JSON response into a :class:`TaskPlan`.

        Handles markdown fences, leading/trailing text, and validates the
        structure.  Returns ``None`` on unrecoverable parse failure.
        """

        cleaned = _strip_markdown_fence(raw or "")
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            # Try to extract the first {...} block.
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except (json.JSONDecodeError, TypeError):
                    return None
            else:
                return None

        steps_raw = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps_raw, list) or not steps_raw:
            return None

        steps: list[TaskStep] = []
        for i, s in enumerate(steps_raw[:self._max_steps]):
            if not isinstance(s, dict):
                continue
            tool_name = str(s.get("tool_name", "")).strip()
            if not tool_name:
                continue
            if self._tool_allowlist and tool_name not in self._tool_allowlist:
                continue
            # Validate tool exists in the catalog.
            defn = find_tool_definition(tool_name)
            plugin_capability = None
            if defn is None:
                resolver = getattr(self._tool_facade, "plugin_capability_for_tool", None)
                if callable(resolver):
                    try:
                        resolved = resolver(tool_name)
                        plugin_capability = resolved[1] if resolved is not None else None
                    except Exception:  # noqa: BLE001 - planner admission fails closed
                        plugin_capability = None
            if defn is None and plugin_capability is None:
                continue
            entry = (
                self._capability_catalog.get(tool_name)
                if self._capability_catalog is not None else None
            )
            if self._capability_catalog is not None and entry is None and plugin_capability is None:
                continue
            steps.append(TaskStep(
                step_id=f"step-{i + 1}",
                description=str(s.get("description", ""))[:200],
                tool_name=tool_name,
                tool_args=_coerce_args(s.get("tool_args", {})),
                expected_outcome=str(s.get("expected_outcome", ""))[:200],
                permission=(
                    entry.permission if entry is not None
                    else str(getattr(plugin_capability, "permission", ""))
                ),
            ))

        if not steps:
            return None

        return TaskPlan(
            plan_id=uuid.uuid4().hex[:16],
            goal=goal,
            steps=tuple(steps),
            rationale=str(data.get("rationale", ""))[:300],
        )

    # ------------------------------------------------------------------ #
    # phase: approval
    # ------------------------------------------------------------------ #

    def _request_approval(self, run_id: str, plan: TaskPlan) -> Any:
        """Transition to WAITING_APPROVAL and call the approval bridge."""

        from src.features.edit.ai_editing.agent_models import (
            ApprovalRequest, ApprovalRisk,
        )

        write_steps = [s for s in plan.steps if s.is_write]
        approval = ApprovalRequest(
            run_id=run_id,
            tool_name="agent.multi_step_write",
            risk=ApprovalRisk.WRITE,
            scope_summary=f"{len(write_steps)} 个写操作："
                          + ", ".join(s.description[:30] for s in write_steps[:3]),
            expected_change="执行自主多步任务中的写操作",
            can_undo=True,
        )
        self._orchestrator.request_approval(run_id, approval)

        # Call the bridge (may block for user interaction).
        if self._approval_bridge is None:
            # No bridge → auto-deny.  WAITING_APPROVAL → FAILED is legal.
            return None

        granted = self._approval_bridge.request_approval(run_id, plan)
        if granted is None:
            return None
        if not self._orchestrator.is_run_active(run_id):
            # AUD-12800：等待审批期间 run 已被取消（终态无出边）——丢弃过期
            # 授权并返回 None；调用方的 is_run_active 检查会按 CANCELLED 收尾，
            # 不再对终态 run 调用 grant_approval 抛 RunNotTransitionableError。
            return None
        try:
            # WAITING_APPROVAL → EXECUTING.
            self._orchestrator.grant_approval(run_id)
        except RunNotTransitionableError:
            if not self._orchestrator.is_run_active(run_id):
                return None  # 检查与转换之间的残余竞态：run 已终结
            raise
        return granted

    # ------------------------------------------------------------------ #
    # phase: execute
    # ------------------------------------------------------------------ #

    def _execute_steps(
        self,
        run_id: str,
        plan: TaskPlan,
        approval_obj: Any,
    ) -> list[StepResult]:
        """Execute each step in the plan via the ToolFacade."""

        results: list[StepResult] = []
        runner = ToolInvocationRunner(
            self._orchestrator,
            self._tool_facade,
            operation_ledger=self._operation_ledger,
            capability_catalog=self._capability_catalog,
        )
        for step in plan.steps:
            if not self._orchestrator.is_run_active(run_id):
                break

            # Write steps get the approval object; read steps don't need it.
            approval = approval_obj if step.is_write else None
            raw = runner.invoke(
                run_id,
                step.tool_name,
                step.tool_args,
                approval=approval,
                call_id=step.step_id,
            )
            sr = self._tool_result_to_step_result(step, raw)
            results.append(sr)

        return results

    @staticmethod
    def _tool_result_to_step_result(step: TaskStep, raw: ToolResult) -> StepResult:
        """Convert the structured invocation result into the task result shape."""

        error = raw.error_message or "; ".join(
            diagnostic.message for diagnostic in raw.diagnostics if diagnostic.message
        )
        return StepResult(
            step_id=step.step_id,
            tool_name=step.tool_name,
            succeeded=raw.succeeded,
            outcome=raw.outcome,
            data=dict(raw.data or {}),
            error=error[:300],
        )

    @staticmethod
    def _facade_to_step_result(step: TaskStep, raw: Any) -> StepResult:
        """Convert a raw ToolFacade.invoke() dict into a StepResult."""

        if not isinstance(raw, dict):
            return StepResult(
                step_id=step.step_id, tool_name=step.tool_name,
                succeeded=False, outcome="failure",
                error="tool returned non-dict",
            )
        ok = bool(raw.get("ok", False))
        data = raw.get("data") or {k: v for k, v in raw.items() if k != "diagnostics"}
        diagnostics = raw.get("diagnostics") or []
        # Check for permission denied in diagnostics.
        diag_codes = [
            str(d.get("code", "")) if isinstance(d, dict) else str(d)
            for d in diagnostics
        ]
        is_denied = any("PERMISSION_DENIED" in c for c in diag_codes)
        if ok:
            outcome = "success"
        elif is_denied:
            outcome = "denied"
        else:
            outcome = "failure"
        error_parts = [
            str(d.get("message", "")) if isinstance(d, dict) else str(d)
            for d in diagnostics
        ]
        return StepResult(
            step_id=step.step_id,
            tool_name=step.tool_name,
            succeeded=ok,
            outcome=outcome,
            data=data if isinstance(data, dict) else {"raw": str(data)[:500]},
            error="; ".join(e for e in error_parts if e)[:300],
        )

    # ------------------------------------------------------------------ #
    # phase: verify
    # ------------------------------------------------------------------ #

    def _verify(
        self,
        run_id: str,
        client: Any,
        goal: str,
        step_results: list[StepResult],
        final_snapshot: dict[str, Any],
        *,
        temperature: float = 0.4,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Ask the LLM whether the goal was achieved."""

        # Note: verification start/finish events are emitted by the
        # orchestrator's begin_verification / finish_verification transitions.
        # Here we only signal progress for the LLM sub-call.
        self._emit(run_id, AgentEventKind.RUN_TOOL_PROGRESS,
                   {"phase": "verification_llm"})

        steps_data = [
            {
                "step_id": sr.step_id,
                "tool_name": sr.tool_name,
                "succeeded": sr.succeeded,
                "outcome": sr.outcome,
                "error": sr.error,
            }
            for sr in step_results
        ]
        prompt = build_verification_system_prompt(goal, steps_data, final_snapshot)
        messages = [{"role": "user", "content": prompt}]

        try:
            raw = client.complete(messages, temperature=temperature, max_tokens=max_tokens)
        except Exception:  # noqa: BLE001
            # If verification call fails, be conservative: not achieved.
            return {"achieved": False, "notes": "验证调用失败，保守判定为未达成"}

        return self._parse_verification_json(raw)

    @staticmethod
    def _parse_verification_json(raw: str) -> dict[str, Any]:
        """Parse the verification JSON response."""

        cleaned = _strip_markdown_fence(raw or "")
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except (json.JSONDecodeError, TypeError):
                    return {"achieved": False, "notes": "验证结果解析失败"}
            else:
                return {"achieved": False, "notes": "验证结果解析失败"}
        achieved = bool(data.get("achieved", False)) if isinstance(data, dict) else False
        notes = str(data.get("notes", ""))[:300] if isinstance(data, dict) else ""
        return {"achieved": achieved, "notes": notes}

    # ------------------------------------------------------------------ #
    # phase: summarise
    # ------------------------------------------------------------------ #

    def _summarize(
        self,
        client: Any,
        goal: str,
        step_results: list[StepResult],
        verification_notes: str,
        *,
        temperature: float = 0.4,
        max_tokens: int = 4096,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        """Generate a user-facing summary of the task execution."""

        steps_data = [
            {
                "step_id": sr.step_id, "tool_name": sr.tool_name,
                "succeeded": sr.succeeded, "outcome": sr.outcome,
            }
            for sr in step_results
        ]
        prompt = build_summary_prompt(goal, steps_data, verification_notes)
        messages = [{"role": "user", "content": prompt}]
        try:
            reply = client.complete(messages, temperature=temperature, max_tokens=max_tokens)
        except Exception:  # noqa: BLE001
            # Fallback summary.
            ok_count = sum(1 for sr in step_results if sr.succeeded)
            return f"任务完成：{ok_count}/{len(step_results)} 步成功。{verification_notes}"

        if on_delta and reply:
            try:
                on_delta(reply)
            except Exception:  # noqa: BLE001
                pass

        return reply

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _get_tool_catalog(self) -> list[dict[str, Any]]:
        """Return the OpenAI-format tool catalog for the planning prompt."""

        try:
            tool_defs = self._tool_facade.list_tools()
        except Exception:  # noqa: BLE001
            return []
        catalog: list[dict[str, Any]] = []
        for td in tool_defs:
            if self._tool_allowlist and getattr(td, "name", "") not in self._tool_allowlist:
                continue
            try:
                catalog.append(td.to_openai_tool())
            except Exception:  # noqa: BLE001
                continue
        return catalog

    def _emit(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Safely emit an orchestrator event (never crashes the run)."""

        try:
            self._orchestrator.emit(run_id, kind, payload=payload)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _collect_usage(client: Any, sink: list[Any]) -> None:
        """Append ``client.last_usage`` to the sink if present."""

        usage = getattr(client, "last_usage", None)
        if usage is not None:
            sink.append(usage)

    @staticmethod
    def _merge_usage(
        all_usage: list[Any], estimated: ContextUsage,
    ) -> ContextUsage | None:
        """Merge multiple LLM call usages into one ContextUsage."""

        if not all_usage:
            return estimated
        total_prompt = 0
        total_completion = 0
        for u in all_usage:
            total_prompt += int(getattr(u, "prompt_tokens", 0) or 0)
            total_completion += int(getattr(u, "completion_tokens", 0) or 0)
        if total_prompt == 0 and total_completion == 0:
            return estimated
        return usage_from_llm(
            type("U", (), {
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "total_tokens": total_prompt + total_completion,
            })(),
            window_usage=estimated,
        )

    @staticmethod
    def _window_limit_for(profile: Any) -> int | None:
        if profile is None:
            return None
        limits = getattr(profile, "limits", None)
        if limits is None:
            return None
        value = getattr(limits, "max_context_tokens", None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #


def _strip_markdown_fence(text: str) -> str:
    """Remove markdown code fences and extract the JSON body."""

    value = (text or "").strip()
    if not value:
        return ""
    # Strip ```json ... ``` or ``` ... ```
    value = re.sub(r"^```(?:json)?\s*\n?", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\n?```\s*$", "", value)
    return value.strip()


def _coerce_args(raw: Any) -> dict[str, Any]:
    """Ensure tool_args is a plain dict."""

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except (json.JSONDecodeError, TypeError):
            return {"raw": raw}
    if raw is None:
        return {}
    return {"value": raw}


__all__ = [
    "AgentTaskExecutor",
    "AgentTaskResult",
    "AlwaysApproveBridge",
    "ApprovalBridge",
    "NeverApproveBridge",
    "StepResult",
    "TaskPlan",
    "TaskStep",
]
