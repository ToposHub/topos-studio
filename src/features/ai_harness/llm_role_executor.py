"""LlmRoleExecutor —— model-backed 子智能体角色执行器（plan §10.5 P3.1）。

用真实模型驱动的 :class:`RoleExecutor` 替代确定性 stub（``DeterministicRoleExecutor``）。
满足 plan §10.5 P3.1：

- 每个角色只能拿**最小** ``ContextSlice`` 和允许工具（不继承父级全部工具/密钥）。
- 子任务有 token / 成本 / 工具次数 / 超时；超出降级，不静默吞。
- 失败 / 降级和可保留 artifact；**没有任何角色能直接获得 ApprovalGrant 或写时间线**
  （由 ``SingleWriterPolicy`` + ``assert_subagent_no_write`` 强制）。

设计（plan §10.5 / §10.1 实施纪律）：

- ``ModelClient`` Protocol 是注入点：真实 LLM client（OpenAI / Qwen / 本地）由
  host 装配；本模块只定义协议 + 把模型输出**校验**成 typed artifact
  （``ScriptDraft`` / ``EvidenceSet`` / ``StoryOutline`` / ``QaReport``）。
  这样满足 ``task_graph._artifact_kind_matches`` 的硬类型契约（不校验 → 节点 FAILED）。
- 失败降级：模型异常 / 超时 / 输出无法解析 → fallback 到 ``DeterministicRoleExecutor``
  的同角色实现（保证子任务仍产 artifact，而非静默 FAILED），并记录降级诊断。
- 每角色 budget / allowlist 由构造函数注入（``ContextSlice`` 当前不带这些字段，
  避免改 Protocol；budget 字段已存在于 ``TaskBudget``）。

红线（plan §10.5 / §1.3）：

- 子智能体无独立 approval（``SingleWriterPolicy`` 强制；本执行器不持有 ApprovalGrant）。
- 外部输入不可信：模型输出经 schema 校验才接受；不直接当命令执行。
- 不在执行器里写时间线 / 不调 provider 写接口。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.features.edit.ai_editing.skill_runtime import DeterministicRoleExecutor
from src.features.edit.ai_editing.task_artifacts import (
    EvidenceSet,
    QaReport,
    ScriptDraft,
    StoryOutline,
    SubagentArtifact,
)
from src.features.edit.ai_editing.task_graph import ContextSlice, TaskBudget, TaskRole


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# ModelClient 协议（注入点；真实 LLM 由 host 提供）
# ---------------------------------------------------------------------- #


class ModelClient(Protocol):
    """模型客户端协议（host 注入；本模块不实现真实推理）。

    约定：输入 prompt + 角色 + 上下文摘要，返回结构化 JSON 文本（由执行器校验成
    typed artifact）。``generate`` 应尊重 ``max_tokens`` 与 ``timeout_ms``。

    真实实现可能调 OpenAI / Qwen / 本地 vLLM；本协议只约束签名与返回形态。
    """

    def generate(
        self,
        *,
        system: str,
        user: str,
        role: str,
        max_tokens: int | None = None,
        timeout_ms: int | None = None,
    ) -> "ModelResponse":
        ...


@dataclass(frozen=True)
class ModelResponse:
    """模型响应（执行器据此校验成 typed artifact）。"""

    text: str = ""
    finish_reason: str = ""        # stop / length / timeout / error
    tokens_used: int = 0
    cost_units: float = 0.0
    error: str = ""                # 非空表示模型调用失败


# ---------------------------------------------------------------------- #
# 角色 prompt 构造（最小 ContextSlice）
# ---------------------------------------------------------------------- #


_ROLE_SYSTEM_PROMPTS: dict[TaskRole, str] = {
    TaskRole.CREATIVE_DIRECTOR: (
        "你是创意总监。基于 brief 和证据，产出脚本草稿（受众/语调/钩子/行动号召）。"
        "只输出 JSON，schema: {audience, tone, hooks:[{angle,draft_text,rationale}], "
        "call_to_action}。不编造证据。"
    ),
    TaskRole.MEDIA_LIBRARIAN: (
        "你是素材管理员。基于证据引用，整理证据集与缺口。"
        "只输出 JSON，schema: {citations:[{segment_id,asset_id,kind,trust,note}], "
        "gaps:[{description,severity}], licensing_notes}。"
    ),
    TaskRole.STORY_EDITOR: (
        "你是故事编辑。基于上游脚本与证据，产出故事大纲（beats）。"
        "只输出 JSON，schema: {beats:[{label,description,evidence_ref,confidence}], "
        "storyboard_hints}。"
    ),
    TaskRole.QA_REVIEWER: (
        "你是 QA 审查员。检查上游产物的证据/时长/许可证问题。"
        "只输出 JSON，schema: {findings:[{category,severity,message,suggested_action}], "
        "approved, summary}。"
    ),
}


def _build_user_prompt(role: TaskRole, context: ContextSlice,
                       upstream: tuple[SubagentArtifact, ...]) -> str:
    """把最小 ContextSlice + 上游 artifact 摘要拼成 user prompt。

    红线：不传完整 brief / 不传 secret / 不传绝对路径（ContextSlice 本就不含这些）。
    """
    parts: list[str] = []
    if context.brief_summary:
        parts.append(f"Brief 摘要：{context.brief_summary}")
    if context.evidence_refs:
        parts.append("证据引用：" + ", ".join(context.evidence_refs[:20]))
    if context.asset_ids:
        parts.append("素材：" + ", ".join(context.asset_ids[:20]))
    if upstream:
        up_summary = []
        for a in upstream:
            d = a.to_dict() if hasattr(a, "to_dict") else {}
            up_summary.append(json.dumps(d, ensure_ascii=False)[:500])
        parts.append("上游产物：\n" + "\n".join(up_summary))
    return "\n\n".join(parts) or "(无额外上下文)"


# ---------------------------------------------------------------------- #
# JSON → typed artifact 校验（满足 _artifact_kind_matches 硬契约）
# ---------------------------------------------------------------------- #


def _parse_json(text: str) -> dict[str, Any] | None:
    """容错 JSON 解析（模型输出可能有 markdown fence / 前后噪声）。"""
    if not text:
        return None
    t = text.strip()
    # 去 markdown code fence
    if t.startswith("```"):
        lines = t.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        t = "\n".join(lines)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # 尝试找第一个 { ... } 块
        start = t.find("{")
        end = t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _validate_script_draft(data: dict[str, Any], skill_id: str,
                            skill_version: str, context: ContextSlice) -> ScriptDraft | None:
    """把模型 JSON 校验成 ScriptDraft（满足 _artifact_kind_matches）。"""
    try:
        from src.features.edit.ai_editing.task_artifacts import HookIdea
        hooks = tuple(
            HookIdea(
                hook_id=str(h.get("hook_id", f"h{i}")),
                angle=str(h.get("angle", "")),
                draft_text=str(h.get("draft_text", h.get("text", ""))),
                rationale=str(h.get("rationale", "")),
            )
            for i, h in enumerate(data.get("hooks", []) or [])
        )
        return ScriptDraft(
            draft_id=f"llm_cd_{int(time.time())}",
            audience=str(data.get("audience", "")),
            tone=str(data.get("tone", "")),
            hooks=hooks,
            call_to_action=str(data.get("call_to_action", "")),
            produced_by="creative_director",
            skill_id=skill_id, skill_version=skill_version,
            evidence_refs=context.evidence_refs,
        )
    except Exception:  # noqa: BLE001
        return None


def _validate_evidence_set(data: dict[str, Any], skill_id: str,
                            skill_version: str, context: ContextSlice) -> EvidenceSet | None:
    try:
        from src.features.edit.ai_editing.task_artifacts import (
            EvidenceCitation, EvidenceGap,
        )
        citations = tuple(
            EvidenceCitation(
                segment_id=str(c.get("segment_id", "")),
                asset_id=str(c.get("asset_id", "")),
                kind=str(c.get("kind", "")),
                trust=str(c.get("trust", "unverified")),
                note=str(c.get("note", "")),
            )
            for c in (data.get("citations", []) or [])
        )
        gaps = tuple(
            EvidenceGap(
                gap_id=str(g.get("gap_id", f"g{i}")),
                description=str(g.get("description", "")),
                severity=str(g.get("severity", "info")),
                suggested_action=str(g.get("suggested_action", "")),
            )
            for i, g in enumerate(data.get("gaps", []) or [])
        )
        return EvidenceSet(
            set_id=f"llm_ml_{int(time.time())}",
            citations=citations, gaps=gaps,
            licensing_notes=str(data.get("licensing_notes", "")),
            produced_by="media_librarian",
            skill_id=skill_id, skill_version=skill_version,
            evidence_refs=context.evidence_refs,
        )
    except Exception:  # noqa: BLE001
        return None


def _validate_story_outline(data: dict[str, Any], skill_id: str,
                             skill_version: str, context: ContextSlice) -> StoryOutline | None:
    try:
        from src.features.edit.ai_editing.task_artifacts import StoryBeat
        beats = tuple(
            StoryBeat(
                beat_id=str(b.get("beat_id", f"b{i}")),
                label=str(b.get("label", "")),
                description=str(b.get("description", "")),
                evidence_ref=str(b.get("evidence_ref", "")),
                confidence=float(b.get("confidence", 0.5)),
                suggested_range=str(b.get("suggested_range", "")),
            )
            for i, b in enumerate(data.get("beats", []) or [])
        )
        return StoryOutline(
            outline_id=f"llm_se_{int(time.time())}",
            beats=beats,
            storyboard_hints=str(data.get("storyboard_hints", "")),
            produced_by="story_editor",
            skill_id=skill_id, skill_version=skill_version,
            evidence_refs=context.evidence_refs,
        )
    except Exception:  # noqa: BLE001
        return None


def _validate_qa_report(data: dict[str, Any], skill_id: str,
                         skill_version: str) -> QaReport | None:
    try:
        from src.features.edit.ai_editing.task_artifacts import QaFinding
        findings = tuple(
            QaFinding(
                finding_id=str(f.get("finding_id", f"f{i}")),
                category=str(f.get("category", "")),
                severity=str(f.get("severity", "info")),
                message=str(f.get("message", "")),
                suggested_action=str(f.get("suggested_action", "")),
            )
            for i, f in enumerate(data.get("findings", []) or [])
        )
        return QaReport(
            report_id=f"llm_qa_{int(time.time())}",
            findings=findings,
            approved=bool(data.get("approved", False)),
            summary=str(data.get("summary", "")),
            produced_by="qa_reviewer",
            skill_id=skill_id, skill_version=skill_version,
            evidence_refs=(),
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------- #
# LlmRoleExecutor
# ---------------------------------------------------------------------- #


@dataclass
class RoleBudget:
    """单角色的 token / 成本 / 工具次数 / 超时预算（plan §10.5 P3.1）。"""

    max_tokens: int | None = None
    max_cost_units: float | None = None
    timeout_ms: int | None = None
    max_tool_calls: int | None = None


@dataclass
class LlmRoleExecutor:
    """model-backed RoleExecutor（plan §10.5 P3.1）。

    实现 ``RoleExecutor`` 协议（duck-typed；不继承避免 Protocol 元类冲突）。

    用法（host 装配）::

        executor = LlmRoleExecutor(
            model_client=real_llm_client,
            skill_id="topos.voiceover_polish",
            skill_version="1.0.0",
            role_budgets={TaskRole.CREATIVE_DIRECTOR: RoleBudget(max_tokens=2000)},
        )
        runtime = SkillRuntime(registry, policy, role_executor=executor)

    红线：

    - 不持有 ApprovalGrant / 不写时间线（SingleWriterPolicy 强制）。
    - 模型输出经 schema 校验才接受；失败降级到 DeterministicRoleExecutor。
    - 每角色独立 budget；超限降级并记录（不静默）。
    """

    model_client: ModelClient | None = None
    skill_id: str = ""
    skill_version: str = ""
    #: 每角色 budget（缺省用 TaskBudget 默认 / RoleBudget 默认）。
    role_budgets: dict[TaskRole, RoleBudget] = field(default_factory=dict)
    #: 降级开关：模型失败时是否回退到确定性 stub（默认 True，保证子任务仍产 artifact）。
    fallback_to_deterministic: bool = True
    #: 内部确定性 executor（降级用）。
    _deterministic: DeterministicRoleExecutor = field(
        default_factory=lambda: DeterministicRoleExecutor()
    )
    #: 累计成本 / token（供 host 查询；不在执行器内做硬限速，由 budget 判断）。
    total_cost_units: float = 0.0
    total_tokens: int = 0
    #: 降级记录（role -> 次数）。
    degradation_counts: dict[str, int] = field(default_factory=dict)
    #: 最近一次执行的公开来源；TaskGraph 会把它写入 TaskResult，避免 fallback 冒充 LLM。
    last_execution_source: str = ""
    last_degradation_reason: str = ""
    last_usage: dict[str, Any] = field(default_factory=dict)
    _pending_degradation_reasons: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # 同步 skill_id/version 到降级 executor
        self._deterministic = DeterministicRoleExecutor(
            skill_id=self.skill_id, skill_version=self.skill_version,
        )

    # ---------------- RoleExecutor 协议 ---------------- #

    def execute(
        self,
        role: TaskRole,
        context: ContextSlice,
        upstream: tuple[SubagentArtifact, ...],
    ) -> SubagentArtifact:
        """执行单个角色：调模型 → 校验 → 失败降级。"""
        self.last_execution_source = ""
        self.last_degradation_reason = ""
        self.last_usage = {}
        # 1. 模型不可用 → 直接降级
        if self.model_client is None:
            self._record_degradation(role, "no_model_client")
            return self._fallback(role, context, upstream)

        budget = self.role_budgets.get(role, RoleBudget())
        system = _ROLE_SYSTEM_PROMPTS.get(role, "你是 AI 助手。只输出 JSON。")
        user = _build_user_prompt(role, context, upstream)

        # 2. 调模型（尊重 timeout/budget）
        try:
            resp = self.model_client.generate(
                system=system, user=user, role=role.value,
                max_tokens=budget.max_tokens, timeout_ms=budget.timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001 — 模型异常必须降级，不向上冒泡
            self._record_degradation(role, f"model_exception:{type(exc).__name__}")
            _LOG.warning("LlmRoleExecutor model failed for %s: %s", role, exc)
            return self._fallback(role, context, upstream)

        # 3. 模型返回错误 / 超时
        if resp.error or resp.finish_reason == "timeout":
            self._record_degradation(role, resp.finish_reason or resp.error or "model_error")
            return self._fallback(role, context, upstream)

        # 4. 成本 / token 累计
        self.last_usage = {
            "tokens": resp.tokens_used,
            "cost_units": resp.cost_units,
            "tool_calls": 0,
        }
        self.total_cost_units += resp.cost_units
        self.total_tokens += resp.tokens_used
        if budget.max_cost_units is not None and self.total_cost_units > budget.max_cost_units:
            self._record_degradation(role, "budget_cost_exceeded")
            return self._fallback(role, context, upstream)

        # 5. 校验模型输出成 typed artifact
        data = _parse_json(resp.text)
        artifact = self._validate(role, data, context) if data else None
        if artifact is None:
            self._record_degradation(role, "output_validation_failed")
            _LOG.warning("LlmRoleExecutor output validation failed for %s", role)
            return self._fallback(role, context, upstream)

        self.last_execution_source = "llm"
        return artifact

    def execute_with_budget(
        self,
        role: TaskRole,
        context: ContextSlice,
        upstream: tuple[SubagentArtifact, ...],
        task_budget: TaskBudget,
    ) -> SubagentArtifact:
        """让 TaskGraph 节点预算覆盖角色默认预算，并向模型传递更小上限。"""

        current = self.role_budgets.get(role)
        merged = RoleBudget(
            max_tokens=_min_limit(
                current.max_tokens if current else None, task_budget.max_tokens,
            ),
            max_cost_units=_min_limit(
                current.max_cost_units if current else None, task_budget.max_cost,
            ),
            timeout_ms=_min_limit(
                current.timeout_ms if current else None, task_budget.timeout_ms,
            ),
            max_tool_calls=_min_limit(
                current.max_tool_calls if current else None, task_budget.max_tool_calls,
            ),
        )
        if current is None:
            self.role_budgets[role] = merged
            try:
                return self.execute(role, context, upstream)
            finally:
                self.role_budgets.pop(role, None)
        self.role_budgets[role] = merged
        try:
            return self.execute(role, context, upstream)
        finally:
            self.role_budgets[role] = current

    # ---------------- 内部 ---------------- #

    def _validate(
        self, role: TaskRole, data: dict[str, Any], context: ContextSlice,
    ) -> SubagentArtifact | None:
        if role == TaskRole.CREATIVE_DIRECTOR:
            return _validate_script_draft(data, self.skill_id, self.skill_version, context)
        if role == TaskRole.MEDIA_LIBRARIAN:
            return _validate_evidence_set(data, self.skill_id, self.skill_version, context)
        if role == TaskRole.STORY_EDITOR:
            return _validate_story_outline(data, self.skill_id, self.skill_version, context)
        if role == TaskRole.QA_REVIEWER:
            return _validate_qa_report(data, self.skill_id, self.skill_version)
        return None

    def _fallback(
        self, role: TaskRole, context: ContextSlice,
        upstream: tuple[SubagentArtifact, ...],
    ) -> SubagentArtifact:
        """降级到 DeterministicRoleExecutor（保证子任务仍产 typed artifact）。"""
        if not self.fallback_to_deterministic:
            raise RuntimeError(
                f"LlmRoleExecutor: role {role} failed and fallback disabled"
            )
        self.last_execution_source = "deterministic_fallback"
        # 该字段会被宿主/SkillRuntime 展示；不能只写日志后返回看似成功的 artifact。
        self.last_degradation_reason = self._pending_degradation_reasons.get(
            role.value, "unknown",
        )
        return self._deterministic.execute(role, context, upstream)

    def _record_degradation(self, role: TaskRole, reason: str) -> None:
        key = f"{role.value}:{reason}"
        self.degradation_counts[key] = self.degradation_counts.get(key, 0) + 1
        self._pending_degradation_reasons[role.value] = reason


def _min_limit(first: int | float | None, second: int | float | None) -> int | float | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


__all__ = [
    "LlmRoleExecutor",
    "ModelClient",
    "ModelResponse",
    "RoleBudget",
]
