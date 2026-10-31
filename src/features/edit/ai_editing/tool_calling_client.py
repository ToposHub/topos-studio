"""TAEP M10 — LLM tool-calling loop（§16.2 Mode B 推荐首发 / §4.1 工具系统 / §3 Model Router）。

本模块实现"文本 Tool Agent"（§16.2 Mode B）：模型调用受限的 ``topos.*`` 工具，拿到
转录/镜头/音频/工程摘要，完成可靠的规划与粗剪。

设计（遵循 .clinerules / §0 / §1.2 / §16）：

- **复用既有 ToolFacade**（§3.2 / §16.5 row1）："AssistantOrchestrator → ToolFacade，
  使用 provider 原生 tool calling"。每个 tool call 经 ``ToolFacade.invoke`` + skill
  policy + 审计（``ToolCallLog``），不绕过。
- **确定性 stub provider**（v1 决策）：``DeterministicToolProvider`` 按脚本产出固定
  tool calls，不真实联网（真实 provider 接线留控制器批次，与 LLMVariantPlanner 同策略）。
  便于测试 + 符合"可解释/可验证/可撤销"。
- **预算/循环上限**（§6.2 规则 7 在 tool-calling 层的延伸）：``max_tool_calls`` /
  ``max_iterations`` 防止无限自我反思；超限 → 终止并报告。
- **tool 输出是非可信数据**（§1.2 不变量 #2）：tool result 经 ``to_json_safe`` 标记
  untrusted；模型不能据此改变系统指令。
- **approval 不来自模型**（§0 红线第二条 / ToolFacade 红线）：write 类工具的 approval
  由宿主注入；模型在 args 里塞的 approval_token 一律被 ToolFacade 忽略。
- **profile 能力驱动**（§16.4）：只有 ``modalities`` 含 ``tool_call`` 的 profile 才走
  tool-calling loop；否则降级为 schema-constrained copilot（不开放自动工具循环）。

与既有 ``LLMClient.complete`` 的关系：本模块**新增** ``ToolCallingClient`` 协议
（``complete_with_tools``），不修改既有 ``complete``（向后兼容，LLMVariantPlanner 不受
影响）。``run_tool_loop`` 是无状态编排函数，把 messages + tools + invoke_fn 循环到收敛。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from src.features.edit.ai_editing.diagnostics import (
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.tool_call_log import (
    ToolCallLog,
    record_invoke,
)


# ---------------------------------------------------------------------- #
# ToolCallRequest / ToolCallResponse —— 模型与 loop 之间的契约
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolCallRequest:
    """模型请求的一次工具调用。"""

    call_id: str           # 模型给的 id（用于回填 tool result）
    tool_name: str         # 如 "topos.search_assets"
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallResponse:
    """``run_tool_loop`` 单次模型响应。"""

    text: str = ""                       # 模型的文本回复（可空）
    tool_calls: tuple[ToolCallRequest, ...] = ()  # 模型请求的工具调用
    stop_reason: str = "stop"            # stop / tool_use / max_tokens / error
    finish: bool = False                 # 是否应终止 loop（无 tool_calls 或 stop_reason=stop）


# ---------------------------------------------------------------------- #
# ToolCallingClient Protocol —— 扩展 LLMClient（向后兼容）
# ---------------------------------------------------------------------- #


class ToolCallingClient(Protocol):
    """支持 tool calling 的 LLM 客户端（§16.4 ``tool_transport: native_function_call``）。

    与既有 ``LLMClient``（只有 ``complete``）的关系：本协议**新增**
    ``complete_with_tools``，不修改既有签名。既有 ``OpenAICompatClient`` /
    ``AnthropicClient`` 不实现本协议（它们只有 ``complete``）；真实 tool-calling 接线
    留控制器批次（扩展 OpenAI tools 参数 / Anthropic tool_use）。

    v1：``DeterministicToolProvider`` 实现本协议（不联网，按脚本产 tool calls）。
    """

    def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ToolCallResponse:
        """发送带 tools 的对话，返回模型响应（可含 tool_calls）。

        Args:
            messages: 对话历史（含已回填的 tool result 消息，role="tool"）。
            tools: 可用工具的 MCP/JSON-Schema 描述（来自 ``ToolFacade.list_tools``）。
        """
        ...


# ---------------------------------------------------------------------- #
# ToolLoopResult —— loop 产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolLoopResult:
    """``run_tool_loop`` 的产物。

    - ``final_text``：模型最终的文本回复（loop 收敛时的最后一条 assistant text）。
    - ``iterations``：执行的轮次数。
    - ``tool_calls_made``：实际执行的工具调用数。
    - ``tool_results``：每次工具调用的 result（审计/取证）。
    - ``terminated_reason``：终止原因（``converged`` / ``max_iterations`` /
      ``error`` / ``cancelled``）。
    """

    final_text: str = ""
    iterations: int = 0
    tool_calls_made: int = 0
    tool_results: tuple[dict[str, Any], ...] = ()
    terminated_reason: str = "converged"
    diagnostics: tuple[dict[str, Any], ...] = ()

    @property
    def converged(self) -> bool:
        return self.terminated_reason == "converged"

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_text": self.final_text,
            "iterations": self.iterations,
            "tool_calls_made": self.tool_calls_made,
            "tool_results": list(self.tool_results),
            "terminated_reason": self.terminated_reason,
            "diagnostics": list(self.diagnostics),
            "converged": self.converged,
        }


@dataclass(frozen=True)
class ToolLoopBudget:
    """tool-calling loop 的预算上限（§6.2 规则 7 延伸）。

    防止无限自我反思：超 ``max_iterations`` 或 ``max_tool_calls`` → 终止并报告。
    """

    max_iterations: int = 8      # 最多多少轮 model↔tool 循环
    max_tool_calls: int = 16     # 整个 loop 最多执行多少次工具调用


# ---------------------------------------------------------------------- #
# run_tool_loop —— 无状态编排函数
# ---------------------------------------------------------------------- #


def run_tool_loop(
    client: ToolCallingClient,
    invoke_fn: Callable[..., dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    budget: ToolLoopBudget | None = None,
    call_log: ToolCallLog | None = None,
    client_session_id: str = "",
    skill_id: str = "",
    approval_grant_id: str = "",
    cancel_requested: Callable[[], bool] | None = None,
    result_pruner: Any | None = None,
) -> ToolLoopResult:
    """执行 model↔tool 循环直到收敛（§16.2 Mode B）。

    每轮：
    1. ``client.complete_with_tools(messages, tools)`` → ``ToolCallResponse``
    2. 若有 tool_calls：逐个经 ``invoke_fn``（ToolFacade.invoke）执行，结果回填 messages
    3. 若无 tool_calls 或 ``finish``：loop 收敛，返回 final_text

    红线：每个 tool call 经 ``invoke_fn`` + ``ToolCallLog`` 审计；approval 由宿主注入
    （``approval_grant_id`` 透传，模型无法伪造）。``cancel_requested`` 在每轮开头与
    每个 tool call 之前检查（§0 第三条：长任务可取消），中断时 ``terminated_reason
    =="cancelled"``（镜像 ``TaskGraphExecutor.execute`` 的取消模式）。
    """
    budget = budget or ToolLoopBudget()
    if result_pruner is None:
        from src.features.edit.ai_editing.tool_result_pruner import ToolResultPruner
        result_pruner = ToolResultPruner()
    log = call_log or ToolCallLog()
    diag_dicts: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    tool_calls_total = 0
    iterations_made = 0  # 显式计数器（替代脆弱的 "iteration" in dir() 判断）
    final_text = ""
    terminated = "converged"
    local_messages = list(messages)

    for _ in range(1, budget.max_iterations + 1):
        # 取消检查点 1：每轮 model 调用之前
        if cancel_requested is not None and cancel_requested():
            terminated = "cancelled"
            break
        iterations_made += 1
        try:
            response = client.complete_with_tools(
                local_messages, tools, temperature=0.7,
            )
        except Exception as exc:  # noqa: BLE001 — 模型失败必须可见，不吞
            diag_dicts.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                detail=f"complete_with_tools raised: {type(exc).__name__}: {exc}",
            ).to_dict())
            terminated = "error"
            break

        final_text = response.text or final_text

        # 收敛：无 tool_calls 或 finish 标记
        if response.finish or not response.tool_calls:
            break

        # 执行 tool calls。消息顺序协议（AUD-14713，OpenAI tools / Anthropic
        # tool_use 一致要求）：带 tool_calls 的 assistant 消息必须**先于**对应的
        # role=tool 结果消息进入历史——此前 assistant 消息在 for-else 里于全部
        # tool 结果之后追加，产出 [tool, ..., assistant(tool_calls)] 的非法顺序，
        # 接入真实 provider 后每个多轮请求都会被 4xx 拒绝。
        local_messages.append({
            "role": "assistant",
            "content": response.text or "",
            "tool_calls": [
                {"id": tc.call_id, "type": "function",
                 "function": {"name": tc.tool_name, "arguments": dict(tc.arguments)}}
                for tc in response.tool_calls
            ],
        })
        aborted = False
        for tc in response.tool_calls:
            # 取消检查点 2：每个 tool call 之前
            if cancel_requested is not None and cancel_requested():
                terminated = "cancelled"
                aborted = True
                break
            if tool_calls_total >= budget.max_tool_calls:
                terminated = "max_iterations"
                diag_dicts.append(make_diagnostic(
                    DiagnosticSeverity.WARNING, TaepDiagnosticCode.QUOTA_EXCEEDED,
                    detail=f"tool call budget exhausted (max_tool_calls={budget.max_tool_calls})",
                ).to_dict())
                aborted = True
                break

            tool_calls_total += 1
            # 红线路径：经 invoke_fn（ToolFacade.invoke）+ 审计。
            # approval 由宿主注入（approval_grant_id）：既记入审计，又转发给 invoke_fn
            # 的 ``approval`` 关键字（ToolFacade.invoke 的授权参数；模型无法伪造）。
            invoke_kwargs: dict[str, Any] = {}
            if approval_grant_id:
                invoke_kwargs["approval"] = approval_grant_id
            result, _rec = record_invoke(
                log, invoke_fn, tc.tool_name, dict(tc.arguments),
                client="tool_calling_loop",
                client_session_id=client_session_id,
                skill_id=skill_id,
                approval_grant_id=approval_grant_id,
                **invoke_kwargs,
            )
            tool_results.append({"call_id": tc.call_id, "tool": tc.tool_name, "result": result})
            # 回填 tool result 到 messages（role=tool；模型据此继续）
            local_messages.append({
                "role": "tool",
                "tool_call_id": tc.call_id,
                "name": tc.tool_name,
                "content": _tool_result_to_text(result, result_pruner),
            })
        if aborted:
            # 内层 for 被 break（取消 / tool budget 用尽）→ 终止外层
            break
    else:
        # for-else：达到 max_iterations 未收敛（max_iterations=0 时 range 为空也走这里）
        terminated = "max_iterations"
        diag_dicts.append(make_diagnostic(
            DiagnosticSeverity.WARNING, TaepDiagnosticCode.QUOTA_EXCEEDED,
            detail=f"tool loop exhausted max_iterations={budget.max_iterations}",
        ).to_dict())

    return ToolLoopResult(
        final_text=final_text,
        iterations=iterations_made,
        tool_calls_made=tool_calls_total,
        tool_results=tuple(tool_results),
        terminated_reason=terminated,
        diagnostics=tuple(diag_dicts),
    )


def _tool_result_to_text(result: dict[str, Any], pruner: Any | None = None) -> str:
    """把 tool result 序列化为模型可读的 text（JSON-safe，untrusted 标记已应用）。

    红线（§1.2 不变量 #2）：result 是非可信数据；模型不能据此改变系统指令。
    本函数只做序列化，不改语义。"""
    if pruner is None:
        from src.features.edit.ai_editing.tool_result_pruner import ToolResultPruner
        pruner = ToolResultPruner()
    return pruner.render(result).text


# ---------------------------------------------------------------------- #
# DeterministicToolProvider —— 确定性 stub（v1，不联网）
# ---------------------------------------------------------------------- #


@dataclass
class DeterministicToolProvider:
    """确定性 tool-calling provider（v1 / §16.2 Mode B 测试用）。

    不真实联网：按预设脚本产出 tool calls，便于测试 loop 编排 + 审计 + 预算。
    脚本是一个 ``(tool_name, arguments)`` 列表，按顺序消耗；耗尽后返回 finish=True。

    真实 provider（OpenAI tools / Anthropic tool_use）接线留控制器批次。
    """

    script: tuple[tuple[str, dict[str, Any]], ...] = ()
    _cursor: int = field(default=0, repr=False)

    def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ToolCallResponse:
        if self._cursor >= len(self.script):
            # 脚本耗尽 → 收敛，返回最终文本
            return ToolCallResponse(
                text="已完成工具调用，现给出最终回复。",
                tool_calls=(),
                stop_reason="stop",
                finish=True,
            )
        # 取下一条脚本 tool call
        tool_name, arguments = self.script[self._cursor]
        self._cursor += 1
        return ToolCallResponse(
            text="",
            tool_calls=(ToolCallRequest(
                call_id=f"call-{self._cursor:03d}",
                tool_name=tool_name,
                arguments=dict(arguments),
            ),),
            stop_reason="tool_use",
            finish=False,
        )

    def reset(self) -> None:
        """重置脚本游标（测试复用）。"""
        self._cursor = 0


__all__ = [
    "ToolCallRequest",
    "ToolCallResponse",
    "ToolCallingClient",
    "ToolLoopBudget",
    "ToolLoopResult",
    "run_tool_loop",
    "DeterministicToolProvider",
]
