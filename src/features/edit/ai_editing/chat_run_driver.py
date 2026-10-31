"""Drive an ordinary chat request through a unified AgentRun (plan §A1 §4-5).

This is the integration glue that turns "the user typed something" into a
complete, observable ``AgentRun`` by composing the pieces built in A0 / A1 /
A1.5 / A4:

    run_chat()
      ├─ AssistantCapabilityRouter.resolve()      → fail closed if unavailable
      ├─ AgentOrchestrator.start_task()           → CREATED → GATHERING_CONTEXT
      ├─ context_budget.estimate_window_usage()   → pre-send estimate
      ├─ injected LLMClient.complete/stream       → the actual model call
      ├─ context_budget.usage_from_llm()          → real usage → record_usage
      └─ AgentOrchestrator.finish_verification()  → SUCCEEDED / FAILED

The driver is intentionally synchronous and Qt-free: the controller is
expected to run it on a background thread (the existing ``_ChatWorker``
pattern) and pass the resulting events to the UI.  ``client_factory`` is
injected so the whole flow is testable with a fake client and no network.

Hard guarantees:

- **Fail closed first.**  If the capability router says the mode is blocking,
  the model is never called; the run is created and moved straight to FAILED
  with the router's reason so the user sees *why*.
- **Never fake success.**  An empty reply ends in FAILED, not SUCCEEDED
  (plan §4.2 rule 4).
- **Real usage when available.**  ``last_usage`` is bridged into the run; a
  missing usage keeps the estimate instead of faking a zero.
- **Cancellation is visible.**  The run is checked for cancellation at each
  phase boundary; a cancelled run never records a success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Callable

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
    AgentTask,
    AgentTaskKind,
    ContextUsage,
)
from src.features.edit.ai_editing.agent_inbox import AgentInbox
from src.features.edit.ai_editing.capability_catalog import CapabilityCatalog
from src.features.edit.ai_editing.agent_presets import (
    AgentPreset,
    AgentPresetRegistry,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.assistant_capability_router import (
    AssistantCapabilityRouter,
)
from src.features.edit.ai_editing.context_budget import (
    accumulate_session_total,
    estimate_window_usage,
    usage_from_llm,
    would_exceed_limit,
)
from src.features.edit.ai_editing.llm_client import LLMClient, LLMError
from src.features.edit.ai_editing.request_recovery import (
    ModelRequestRecovery,
    RequestRecoveryPolicy,
)
from src.features.edit.ai_editing.tool_operation_ledger import ToolOperationLedger

logger = logging.getLogger(__name__)


#: Factory that builds an LLMClient for a resolved profile (injected for testing).
ClientFactory = Callable[[Any], LLMClient | None]


@dataclass(frozen=True)
class ChatRunResult:
    """Outcome of one chat run, for the controller to render."""

    run_id: str
    reply: str = ""
    usage: ContextUsage | None = None
    error: str = ""
    error_code: str = ""
    blocked: bool = False
    cancelled: bool = False
    terminal_state: str = ""

    @property
    def succeeded(self) -> bool:
        return self.terminal_state == AgentRunState.SUCCEEDED


_MODE_TO_TASK_KIND: dict[str, str] = {
    "chat": AgentTaskKind.CHAT,
    "agent": AgentTaskKind.AGENT,
    "motion_graphic": AgentTaskKind.MOTION_GRAPHIC,
    "image": AgentTaskKind.IMAGE,
    "video": AgentTaskKind.VIDEO,
    "music": AgentTaskKind.MUSIC,
}


class ChatRunDriver:
    """Run a chat request as a unified AgentRun (plan §A1 §4-5)."""

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        router: AssistantCapabilityRouter,
        *,
        client_factory: ClientFactory,
        profile_registry: Any | None = None,
        tool_facade: Any | None = None,
        tool_middlewares: tuple[Any, ...] | None = None,
        request_recovery_policy: RequestRecoveryPolicy | None = None,
        inbox: AgentInbox | None = None,
        preset_registry: AgentPresetRegistry | None = None,
        preset_plugin_host: Any | None = None,
        approval_bridge: Any | None = None,
        operation_ledger: ToolOperationLedger | None = None,
        capability_catalog: CapabilityCatalog | None = None,
        postcondition_checker: Any | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._router = router
        self._client_factory = client_factory
        self._profiles = profile_registry
        # A6: 确定性后置条件验收（"模型不得自报成功"）。默认装配内建检查器，
        # 此前该框架从未接线，成功判定只有空回复内联检查。
        if postcondition_checker is None:
            from .postcondition_checker import create_default_checker

            postcondition_checker = create_default_checker()
        self._postcondition_checker = postcondition_checker
        # B2: 可选的 ToolFacade，启用后普通对话可走 AgentToolLoop（只读工具）。
        self._tool_facade = tool_facade
        # Optional host policies/metrics can extend the runner without
        # modifying AgentToolLoop or the approval implementation.
        self._tool_middlewares = tuple(tool_middlewares or ())
        self._request_recovery_policy = request_recovery_policy or RequestRecoveryPolicy()
        self._inbox = inbox or AgentInbox()
        self._inbox.set_event_sink(self._emit_inbox_event)
        self._preset_registry = preset_registry
        self._preset_plugin_host = preset_plugin_host
        # No approval bridge means writes are denied.  The old default
        # ``AlwaysApproveBridge`` made autonomous writes look approved even
        # when no host confirmation existed.
        self._approval_bridge = approval_bridge
        self._operation_ledger = operation_ledger
        self._capability_catalog = capability_catalog or getattr(
            tool_facade, "capability_catalog", None,
        )

    def run_chat(
        self,
        *,
        session_id: str,
        user_text: str,
        messages: list[dict[str, Any]],
        mode: str = "chat",
        temperature: float = 0.6,
        max_tokens: int | None = None,
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
        preset_id: str = "",
    ) -> ChatRunResult:
        """Execute one chat request end-to-end and return the result."""

        resolution = self._router.resolve(mode)
        preset = None
        if preset_id:
            if self._preset_plugin_host is not None:
                preset = self._preset_plugin_host.get(preset_id)
            elif self._preset_registry is not None:
                preset = self._preset_registry.get(preset_id)

        task = AgentTask(
            session_id=session_id,
            title=user_text.strip().splitlines()[0][:60] if user_text.strip() else "chat",
            user_goal=user_text,
            kind=_MODE_TO_TASK_KIND.get(mode, AgentTaskKind.CHAT),
        )
        run_id = self._orchestrator.start_task(task, mode=mode)

        if preset_id and preset is None:
            reason = f"未知 Agent Preset：{preset_id}"
            self._orchestrator.fail(run_id, reason=reason)
            return ChatRunResult(
                run_id=run_id,
                error=reason,
                blocked=True,
                terminal_state=AgentRunState.FAILED,
            )

        # ----- fail closed: never call the model for an unavailable mode -----
        if resolution.is_blocking:
            self._orchestrator.fail(run_id, reason=resolution.reason)
            return ChatRunResult(
                run_id=run_id,
                error=resolution.reason,
                blocked=True,
                terminal_state=AgentRunState.FAILED,
            )
        # Defense-in-depth (plan §P0 red line 3): a demo/stub provider must
        # never masquerade as a real *media* generator.  For plain chat a demo
        # language model returning canned text is acceptable (no artifact), but
        # image/video/music with only a demo provider would fabricate output, so
        # it is hard-blocked here regardless of the runnable flag.
        if (
            mode != "chat"
            and getattr(resolution, "availability", "") == "demo_only"
        ):
            reason = (
                f"{mode} 生成能力仅有演示占位 Provider，无法产出真实素材（fail closed）"
            )
            self._orchestrator.fail(run_id, reason=reason)
            return ChatRunResult(
                run_id=run_id,
                error=reason,
                blocked=True,
                terminal_state=AgentRunState.FAILED,
            )

        # ----- agent mode: delegate to AgentTaskExecutor -----
        if mode == "agent" and self._tool_facade is not None:
            return self._run_agent_task(
                run_id=run_id,
                resolution=resolution,
                session_id=session_id,
                user_text=user_text,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
                on_delta=on_delta,
                preset=preset,
            )

        # ----- gather → plan → execute -----
        if self._orchestrator.is_run_active(run_id):
            self._orchestrator.mark_context_ready(run_id)
        if self._orchestrator.is_run_active(run_id):
            self._orchestrator.mark_plan_ready(run_id)

        if not self._orchestrator.is_run_active(run_id):
            return ChatRunResult(
                run_id=run_id, cancelled=True, terminal_state=AgentRunState.CANCELLED
            )

        profile = self._profile_for(resolution)
        if preset is not None and preset.profile_id and self._profiles is not None:
            try:
                preset_profile = self._profiles.get(preset.profile_id)
            except Exception:  # pragma: no cover - defensive registry boundary
                preset_profile = None
            if preset_profile is not None:
                profile = preset_profile
        window_limit = self._window_limit_for(profile)
        messages = list(messages)
        if preset is not None and preset.system_instruction.strip():
            messages.insert(0, {
                "role": "system",
                "content": preset.system_instruction,
            })
        self._claim_inbox_pre_step(run_id, messages)
        estimated = estimate_window_usage(messages, window_limit=window_limit)
        if preset is not None:
            if preset.temperature is not None:
                temperature = preset.temperature
            if max_tokens is None and preset.max_tokens is not None:
                max_tokens = preset.max_tokens

        # ----- the actual model call -----
        client = self._client_factory(profile) if profile is not None else None
        if client is None:
            reason = "无法为当前 Profile 构造模型客户端"
            self._orchestrator.fail(run_id, reason=reason)
            return ChatRunResult(
                run_id=run_id,
                error=reason,
                terminal_state=AgentRunState.FAILED,
            )

        # B2: 当 profile 支持 tool_call 且 ToolFacade 可用时，优先走工具循环。
        # Agent 能自己查项目上下文（素材/字幕/场景），而非要求用户手动描述。
        # 所有写工具结构性 DENIED（AgentToolLoop 不传 approval → ToolFacade 拒绝）。
        # 若模型不支持 tools API（HTTP 400），自动降级到普通 complete/stream。
        use_tool_loop = self._should_use_tool_loop(profile, client)
        # AUD-13202：本 run 内所有请求的 token 总量（逐轮累计，真实 usage
        # 缺失时按发送前的窗口估算兜底）；工具循环成功时不累计（其用量由
        # 循环自身上报，此处保持 0 = 沿用 last_usage 单次读数）。
        session_total = 0
        self._emit_request_header(
            run_id,
            resolution=resolution,
            profile=profile,
            mode=mode,
            stream=stream,
            messages=messages,
            estimated=estimated,
            tool_names=self._tool_names_for_trace(preset) if use_tool_loop else (),
            request_mode="tool_loop" if use_tool_loop else "completion",
        )
        if use_tool_loop:
            try:
                reply = self._run_with_tool_loop(
                    run_id, client, messages, profile,
                    temperature=temperature, max_tokens=max_tokens,
                    on_delta=on_delta,
                    preset=preset,
                )
            except LLMError as exc:
                # Degrade to plain completion when the provider rejects tools
                # (e.g. model without function-calling support returns 400).
                if exc.code in ("LLM_HTTP_ERROR", "LLM_INVALID_RESPONSE"):
                    use_tool_loop = False  # fall through to plain complete
                    self._emit_request_header(
                        run_id,
                        resolution=resolution,
                        profile=profile,
                        mode=mode,
                        stream=stream,
                        messages=messages,
                        estimated=estimated,
                        tool_names=(),
                        request_mode="completion",
                        degraded_from="tool_loop",
                    )
                else:
                    self._orchestrator.fail(run_id, reason=str(exc))
                    return ChatRunResult(
                        run_id=run_id,
                        error=str(exc),
                        error_code=exc.code,
                        terminal_state=AgentRunState.FAILED,
                    )
            except Exception:  # noqa: BLE001 — 非预期失败降级前必须留痕（AUD-13201）
                # 工具循环内含 middleware/ledger/pruner/多次模型调用等整条
                # 子系统；任何非 LLMError 异常（中间件 TypeError、账本 bug
                # 等）不得被静默吞掉后伪装成一次普通 completion——先记录
                # 完整堆栈，再以降级事件补发与实际请求一致的 request header。
                logger.exception(
                    "run %s: tool loop failed unexpectedly; degrading to plain completion",
                    run_id,
                )
                use_tool_loop = False
                self._emit_request_header(
                    run_id,
                    resolution=resolution,
                    profile=profile,
                    mode=mode,
                    stream=stream,
                    messages=messages,
                    estimated=estimated,
                    tool_names=(),
                    request_mode="completion",
                    degraded_from="tool_loop",
                )

        if not use_tool_loop:
            try:
                recovery = ModelRequestRecovery(
                    self._orchestrator,
                    policy=self._request_recovery_policy,
                )
                cancel_check = lambda: not self._orchestrator.is_run_active(run_id)
                followup_turns = 0
                while True:
                    if stream:
                        reply = recovery.call(
                            lambda: client.stream_complete(
                                messages,
                                on_delta=on_delta,
                                temperature=temperature,
                                max_tokens=max_tokens,
                            ),
                            run_id=run_id,
                            operation_name="stream_complete",
                            cancel_check=cancel_check,
                        )
                    else:
                        reply = recovery.call(
                            lambda: client.complete(
                                messages,
                                temperature=temperature,
                                max_tokens=max_tokens,
                            ),
                            run_id=run_id,
                            operation_name="complete",
                            cancel_check=cancel_check,
                        )
                    turn_usage = getattr(client, "last_usage", None)
                    session_total = accumulate_session_total(session_total, turn_usage)
                    if turn_usage is None:
                        # provider 未回报 usage：按本轮实际发送的窗口估算累计
                        #（此处的 ``estimated`` 仍是本轮发送前的估算）。
                        session_total += int(estimated.window_prompt or 0)
                    if followup_turns >= 4 or not self._inbox.has_pending(run_id):
                        break
                    messages.append({"role": "assistant", "content": reply or ""})
                    if not self._claim_inbox_pre_step(run_id, messages):
                        break
                    # AUD-13202：每轮追问后重估窗口预算——追加的消息持续变多，
                    # 超出 profile 窗口（含预留输出）即停止追问，不再把窗口
                    # 推过上限而无任何压缩/告警。
                    estimated = estimate_window_usage(messages, window_limit=window_limit)
                    if would_exceed_limit(estimated):
                        logger.info(
                            "run %s: follow-up window would exceed limit "
                            "(~%d prompt tokens, limit %s); stopping follow-ups",
                            run_id, estimated.window_prompt or 0, window_limit,
                        )
                        break
                    followup_turns += 1
            except LLMError as exc:
                if exc.code == "LLM_CANCELLED":
                    return ChatRunResult(
                        run_id=run_id,
                        cancelled=True,
                        terminal_state=AgentRunState.CANCELLED,
                    )
                self._orchestrator.fail(run_id, reason=str(exc))
                return ChatRunResult(
                    run_id=run_id,
                    error=str(exc),
                    error_code=exc.code,
                    terminal_state=AgentRunState.FAILED,
                )
            except Exception as exc:  # pragma: no cover - defensive catch-all
                self._orchestrator.fail(run_id, reason=str(exc))
                return ChatRunResult(
                    run_id=run_id,
                    error=str(exc),
                    terminal_state=AgentRunState.FAILED,
                )

        # ----- re-check cancellation after the model call returns -----
        # The HTTP stream cannot be interrupted mid-flight; a user cancel
        # during the call moves the run to CANCELLED via the orchestrator.
        # Re-checking here guarantees a cancelled run is never recorded as a
        # success and its late deltas are classified as cancelled downstream
        # (plan §P0 — no late results after cancel).
        if not self._orchestrator.is_run_active(run_id):
            return ChatRunResult(
                run_id=run_id,
                reply=reply,
                cancelled=True,
                terminal_state=AgentRunState.CANCELLED,
            )

        # ----- bridge real usage (never fake a missing number) -----
        llm_usage = getattr(client, "last_usage", None)
        if llm_usage is not None:
            final_usage = usage_from_llm(
                llm_usage,
                window_usage=estimated,
                # session_total==0（工具循环/未累计）时回退到单次读数语义。
                session_total_tokens=session_total or None,
            )
        elif session_total:
            # AUD-13202：provider 未回报 usage 且本次 run 发生过请求时，保留
            # 逐轮累计的估算总量（估算值仍带 estimated 标记，不伪装实测）。
            final_usage = replace(estimated, session_total_tokens=session_total)
        else:
            final_usage = estimated
        self._orchestrator.record_usage(run_id, final_usage)

        # ----- verify: deterministic postconditions, not self-reported -----
        # A6: 通过 PostconditionChecker 运行注册的确定性检查（chat 任务内建
        # "回复非空"，此前为内联判断；框架化后可按任务种类扩展）。
        self._orchestrator.begin_verification(run_id)
        from .postcondition_checker import VerificationContext

        verification = self._postcondition_checker.check(
            VerificationContext(
                task_kind=_MODE_TO_TASK_KIND.get(mode, AgentTaskKind.CHAT),
                reply=reply,
            ),
        )
        success = verification.outcome != "failed"
        done = self._orchestrator.finish_verification(
            run_id,
            success=success,
            reason="" if success else "；".join(verification.reasons) or "后置条件未通过",
        )

        return ChatRunResult(
            run_id=run_id,
            reply=reply,
            usage=final_usage,
            terminal_state=done.state,
        )

    def _emit_request_header(
        self,
        run_id: str,
        *,
        resolution: Any,
        profile: Any,
        mode: str,
        stream: bool,
        messages: list[dict[str, Any]],
        estimated: ContextUsage,
        tool_names: tuple[str, ...],
        request_mode: str,
        degraded_from: str = "",
    ) -> None:
        """Record safe model-request metadata without storing prompt content.

        ``degraded_from`` 非空时表示该请求是从其他请求模式降级而来
        （AUD-13201）：补发的 header 与实际发出的请求一致，并显式记录
        降级来源，避免 trace 中残留与事实不符的 ``request_mode``。
        """

        roles: dict[str, int] = {}
        for message in messages:
            role = str(message.get("role", "unknown"))
            roles[role] = roles.get(role, 0) + 1
        provider_id = str(
            getattr(profile, "provider_kind", "")
            or getattr(resolution, "provider_id", "")
            or "unknown"
        )
        model_id = str(getattr(profile, "model_id", "") or "unknown")
        payload: dict[str, Any] = {
            "provider_id": provider_id,
            "model_id": model_id,
            "mode": mode,
            "request_mode": request_mode,
            "stream": bool(stream),
            "tool_names": list(tool_names),
            "context": {
                "message_count": len(messages),
                "message_roles": roles,
                "estimated_prompt_tokens": estimated.window_prompt,
                "window_limit": estimated.window_limit,
                "reserved_output": estimated.reserved_output,
            },
        }
        if degraded_from:
            payload["degraded_from"] = degraded_from
        self._orchestrator.emit(
            run_id,
            AgentEventKind.RUN_REQUEST_HEADER,
            payload=payload,
        )

    def _tool_names_for_trace(self, preset: AgentPreset | None = None) -> tuple[str, ...]:
        facade = self._tool_facade
        if facade is None:
            return ()
        try:
            definitions = facade.list_tools()
        except Exception:  # noqa: BLE001 - trace metadata must not block a request
            return ()
        names: list[str] = []
        for definition in definitions or ():
            name = str(getattr(definition, "name", "") or "")
            if name and (
                preset is None
                or not preset.tool_allowlist
                or name in preset.tool_allowlist
            ):
                names.append(name)
        return tuple(names)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _run_agent_task(
        self,
        *,
        run_id: str,
        resolution: Any,
        session_id: str,
        user_text: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int | None,
        stream: bool,
        on_delta: Callable[[str], None] | None,
        preset: AgentPreset | None = None,
    ) -> ChatRunResult:
        """Delegate to :class:`AgentTaskExecutor` for autonomous multi-step tasks.

        The run has already been created (``run_id``) and the mode has been
        resolved as non-blocking.  This method constructs an
        :class:`AgentTaskExecutor` with the driver's existing orchestrator,
        client factory and tool facade, runs it, and translates the
        :class:`AgentTaskResult` back into a :class:`ChatRunResult`.
        """

        from src.features.edit.ai_editing.agent_task_executor import (
            AgentTaskExecutor,
        )

        profile = self._profile_for(resolution)
        if preset is not None and preset.profile_id and self._profiles is not None:
            try:
                preset_profile = self._profiles.get(preset.profile_id)
            except Exception:  # pragma: no cover - defensive registry boundary
                preset_profile = None
            if preset_profile is not None:
                profile = preset_profile
        if preset is not None:
            if preset.temperature is not None:
                temperature = preset.temperature
            if preset.max_tokens is not None:
                max_tokens = preset.max_tokens
        executor = AgentTaskExecutor(
            self._orchestrator,
            client_factory=self._client_factory,
            tool_facade=self._tool_facade,
            profile=profile,
            approval_bridge=self._approval_bridge,
            operation_ledger=self._operation_ledger,
            capability_catalog=self._capability_catalog,
            tool_allowlist=preset.tool_allowlist if preset is not None else (),
            max_steps=preset.max_tool_calls if preset is not None else 10,
            system_instruction=preset.system_instruction if preset is not None else "",
        )

        task_result = executor.execute(
            session_id=session_id,
            user_goal=user_text,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
            on_delta=on_delta,
        )

        cat = "cancelled" if task_result.terminal_state == AgentRunState.CANCELLED else (
            "failed" if task_result.terminal_state == AgentRunState.FAILED else "reply"
        )
        return ChatRunResult(
            run_id=task_result.run_id,
            reply=task_result.reply,
            usage=task_result.usage,
            error=task_result.reply if cat == "failed" else "",
            terminal_state=task_result.terminal_state,
            cancelled=cat == "cancelled",
        )

    def _profile_for(self, resolution: Any) -> Any:
        profile_id = getattr(resolution, "profile_id", "")
        if not profile_id or self._profiles is None:
            return None
        try:
            return self._profiles.get(profile_id)
        except Exception:  # pragma: no cover - defensive
            return None

    def _should_use_tool_loop(self, profile: Any, client: Any) -> bool:
        """B2: 决定是否启用工具循环（三个前置条件全满足才行）。"""

        if self._tool_facade is None:
            return False
        # client 必须实现 complete_with_tools（OpenAI tools API）。
        if not callable(getattr(client, "complete_with_tools", None)):
            return False
        # profile.modalities 必须包含 tool_call（§16.4 能力驱动门控）。
        modalities = getattr(profile, "modalities", ()) or ()
        return "tool_call" in modalities

    def _run_with_tool_loop(
        self,
        run_id: str,
        client: Any,
        messages: list[dict[str, Any]],
        profile: Any,
        *,
        temperature: float,
        max_tokens: int | None,
        on_delta: Callable[[str], None] | None,
        preset: AgentPreset | None = None,
    ) -> str:
        """B2: 运行模型驱动的工具循环，返回最终回复文本。"""

        from src.features.edit.ai_editing.agent_tool_loop import (
            AgentToolLoop,
            AgentToolLoopBudget,
        )
        from src.features.edit.ai_editing.tool_invocation import (
            ToolInputValidationMiddleware,
            ToolInvocationRunner,
        )
        from src.features.edit.ai_editing.tool_result_pruner import ToolResultPruner

        runner = ToolInvocationRunner(
            self._orchestrator,
            self._tool_facade,
            middlewares=(ToolInputValidationMiddleware(), *self._tool_middlewares),
            operation_ledger=self._operation_ledger,
            capability_catalog=self._capability_catalog,
        )
        loop = AgentToolLoop(
            runner,
            budget=AgentToolLoopBudget(
                max_iterations=preset.max_iterations if preset else 8,
                max_tool_calls=preset.max_tool_calls if preset else 16,
                max_parallel_tool_calls=preset.max_parallel_tool_calls if preset else 4,
            ),
            result_pruner=ToolResultPruner(),
            request_recovery=ModelRequestRecovery(
                self._orchestrator,
                policy=self._request_recovery_policy,
            ),
            inbox=self._inbox,
        )

        # 获取工具定义（只读级），转换为 OpenAI tools API 格式。
        tool_defs = self._tool_facade.list_tools()
        if preset is not None and preset.tool_allowlist:
            allowed = set(preset.tool_allowlist)
            tool_defs = [tool for tool in tool_defs if tool.name in allowed]
        tools = [t.to_openai_tool() for t in tool_defs]

        # 取消检查：run 不再 active 时通知循环终止。
        def cancel_check() -> bool:
            return not self._orchestrator.is_run_active(run_id)

        result = loop.run(
            run_id, client, messages, tools,
            temperature=temperature, max_tokens=max_tokens,
            cancel_check=cancel_check,
        )

        # Propagate errors from the tool loop — a stale final_text from an
        # earlier iteration must not masquerade as a successful reply.
        if result.terminated_reason == "error" and result.error:
            raise LLMError(
                f"工具循环出错：{result.error}", code="LLM_TOOL_LOOP_ERROR",
            )

        final_text = result.final_text
        # 推送最终文本给 UI（converged 或 max_iterations 均可能有有效文本）。
        if final_text and on_delta is not None and result.converged:
            try:
                on_delta(final_text)
            except Exception:  # noqa: BLE001
                pass

        return final_text

    def _claim_inbox_pre_step(self, run_id: str, messages: list[dict[str, Any]]) -> bool:
        step = self._inbox.claim(run_id)
        if step.empty:
            return False
        messages.extend(step.to_messages())
        return True

    def _emit_inbox_event(
        self, run_id: str, kind: str, payload: dict[str, Any],
    ) -> None:
        event_kind = {
            "run.inbox_enqueued": AgentEventKind.RUN_INBOX_ENQUEUED,
            "run.inbox_dropped": AgentEventKind.RUN_INBOX_DROPPED,
            "run.pre_step": AgentEventKind.RUN_PRE_STEP,
        }.get(kind)
        if event_kind is None:
            return
        try:
            self._orchestrator.emit(run_id, event_kind, payload=payload)
        except Exception:  # noqa: BLE001 - inbox trace must not block the request
            pass

    @property
    def inbox(self) -> AgentInbox:
        """Expose the bounded queue to the host/controller, not the executor."""

        return self._inbox

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


def classify_chat_result(result: ChatRunResult) -> str:
    """Classify a finished run into a signal category for the UI worker.

    Returns one of ``"reply"`` / ``"failed"`` / ``"cancelled"``.  Used by the
    Qt worker (:class:`AgentV2ChatWorker`) to decide which signal to emit, so
    the existing controller callbacks (``_on_chat_finished`` /
    ``_on_chat_failed`` / ``_on_chat_cancelled``) can be reused unchanged.

    A blocking capability resolution is treated as a failure so the user sees
    the router's reason instead of an empty success.
    """

    if result.cancelled:
        return "cancelled"
    if result.terminal_state == AgentRunState.FAILED:
        return "failed"
    if result.blocked:
        return "failed"
    return "reply"


__all__ = [
    "ChatRunDriver",
    "ChatRunResult",
    "ClientFactory",
    "classify_chat_result",
]
