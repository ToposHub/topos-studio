"""ContextBundle — 可检查、可预算、可移除的模型上下文（M5 / §7.1）。

计划 §7.1：每个模型调用使用可审计的 ``ContextBundle``，每块均有 token 估算、优先级、
过期规则和可见的"为什么包含"。优先级：

    硬约束/用户锁定 > 当前选区/交付目标 > 已批准决策 > 高置信证据 > 最近会话摘要 > 普通历史

绝对路径、secret、未授权原始媒体、无关长转录**不进入** Bundle（§7.1 末段）。

设计目标（M5 地基，不改 UI）：

- **只描述、不抓取**：``ContextBundle`` 是 ``ContextService`` 聚合产出的不可变快照；本身
  不持有 service 引用，不主动读工程数据。抓取逻辑在 ContextService。
- **block 化**：``ContextBlock`` 是最小单元，每块自报 token 估算 / 优先级 / 过期规则 /
  为何包含 / 来源。``ContextBundle`` 是 block 列表 + 总预算统计。
- **可检查/可排除**：``exclude_block`` 返回移除某块的新 bundle（供 UI"排除此上下文"）；
  ``with_budget`` 应用预算上限并标记被截断的块。
- **prompt 装配**：``to_messages`` 把 blocks 按 priority 排序拼成 LLM 消息列表（替换
  ``llm_variant_planner._build_messages`` 的硬编码 2-message）。
- **不存 secret/绝对路径**：所有 block 的 content 在装入前由 ContextService 过滤
  （sanitize_untrusted），ContextBundle 假定已脱敏。

与既有结构的关系：ContextBundle **包装不复制** —— brief 块引用 ``EditorialBrief``，
evidence 块引用 ``SemanticSegment``，decisions 块引用 ``DecisionStore.planning_constraints``
的输出。这些 producer 不改；ContextService 是唯一组装者。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Any


# ---------------------------------------------------------------------- #
# ContextBlockPriority（§7.1 优先级表）
# ---------------------------------------------------------------------- #


class ContextBlockPriority(IntEnum):
    """ContextBlock 优先级（§7.1）。数值越大越优先保留（低优先级先被压缩/丢弃）。

    硬约束/用户锁定 > 当前选区/交付目标 > 已批准决策 > 高置信证据 > 最近会话摘要 > 普通历史。
    """

    HISTORY = 10                # 普通历史消息（最低，先被压缩）
    RECENT_SUMMARY = 20         # 最近会话摘要
    EVIDENCE = 30               # 高置信证据（转录/标签/shot）
    APPROVED_DECISIONS = 40     # 已批准决策（accepted/locked）
    DELIVERY_TARGET = 50        # 当前选区/交付目标（brief 核心字段）
    HARD_CONSTRAINT = 60        # 硬约束/用户锁定（最高，绝不被压缩丢弃）


class ContextBlockKind(str):
    """ContextBlock 类型标签（供 UI 分组展示与排除）。自由字符串，常用值见常量。"""

    pass


# 常用 block kind（非穷举；ContextService 可加自定义 kind）
KIND_SYSTEM_POLICY = "system_policy"
KIND_TOOL_MANIFEST = "tool_manifest"
KIND_BRIEF = "brief"
KIND_SNAPSHOT = "snapshot"
KIND_SELECTION = "selection"
KIND_EVIDENCE = "evidence"
KIND_DECISIONS = "decisions"
KIND_HISTORY = "history"
KIND_HELP_REFS = "help_refs"


class ContextExpiry(str):
    """ContextBlock 过期规则标签（§7.1 "过期规则"）。"""

    pass


EXPIRY_SESSION = "session"          # 会话内有效（切换会话即过期）
EXPIRY_REVISION = "revision"        # 绑定工程 revision（revision 变即 stale）
EXPIRY_PLAN = "plan"                # 绑定当前 plan（plan 应用后即过期）
EXPIRY_NEVER = "never"              # 不过期（系统策略 / tool manifest）


# ---------------------------------------------------------------------- #
# ContextBlock
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContextBlock:
    """ContextBundle 的一块（§7.1）。

    每块自报：``kind``（分组标签）、``content``（已脱敏的载荷，可为 str / dict / list）、
    ``priority``（压缩时丢弃顺序）、``expiry``（过期规则）、``why_included``（人类可读的
    "为什么包含"，供 UI 展示）、``provenance``（来源标记，如 brief_revision / asset_id）。
    """

    kind: str
    content: Any
    priority: ContextBlockPriority
    token_estimate: int
    why_included: str
    provenance: dict[str, Any] = field(default_factory=dict)
    expiry: str = EXPIRY_SESSION
    # 装配到 prompt 时使用的 role（system/user）。evidence/decisions 通常并入 user。
    prompt_role: str = "system"
    # 该块是否被预算截断（with_budget 置位；UI 据此显示"已截断"）
    truncated: bool = False

    @property
    def id(self) -> str:
        """块唯一标识（kind + provenance 摘要；供 UI 排除/恢复引用）。"""
        prov_key = "|".join(f"{k}={v}" for k, v in sorted(self.provenance.items()))
        return f"{self.kind}:{prov_key}" if prov_key else self.kind


# ---------------------------------------------------------------------- #
# TokenBudget
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class TokenBudget:
    """ContextBundle 的预算（§7.2）。

    ``limit`` 为目标 token 上限（来自 provider profile / 用户设置）；``reserved_for_response``
    是为模型回复预留的额度（不计入输入 bundle）。``input_limit = limit - reserved``。
    """

    limit: int = 32_000
    reserved_for_response: int = 4_000

    @property
    def input_limit(self) -> int:
        """输入侧可用 token 上限（总限 - 回复预留）。"""
        return max(0, self.limit - self.reserved_for_response)

    @property
    def compress_threshold(self) -> int:
        """触发结构化摘要压缩的阈值（§7.2：达 60% 预算）。"""
        return int(self.input_limit * 0.6)


# ---------------------------------------------------------------------- #
# ContextBundle
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContextBundle:
    """一次模型调用的完整可检查上下文（M5 / §7.1）。

    不可变；``exclude_block`` / ``with_budget`` 返回新 bundle。``to_messages`` 装配 LLM 输入。
    """

    blocks: tuple[ContextBlock, ...] = ()
    budget: TokenBudget = field(default_factory=TokenBudget)
    # 被预算挤出的 block id（with_budget 置位；UI 据此显示"X 块因预算未纳入"）
    omitted_block_ids: tuple[str, ...] = ()
    # policy 版本（系统策略 + agent policy；§7.1 "系统策略版本"）
    policy_version: str = "taep/1.0"

    # ---------------- 统计 ---------------- #

    @property
    def total_tokens(self) -> int:
        """所有 block 的 token 估算之和。"""
        return sum(b.token_estimate for b in self.blocks)

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    def blocks_by_kind(self) -> dict[str, list[ContextBlock]]:
        """按 kind 分组（UI 分组展示用）。"""
        result: dict[str, list[ContextBlock]] = {}
        for b in self.blocks:
            result.setdefault(b.kind, []).append(b)
        return result

    def get_block(self, block_id: str) -> ContextBlock | None:
        for b in self.blocks:
            if b.id == block_id:
                return b
        return None

    # ---------------- 不可变变换 ---------------- #

    def with_block(self, block: ContextBlock) -> "ContextBundle":
        """追加一块（返回新 bundle）。"""
        return replace(self, blocks=self.blocks + (block,))

    def exclude_block(self, block_id: str) -> "ContextBundle":
        """移除一块（UI"排除此上下文"，§7.1 可排除）。

        被排除的块不进 omitted（omitted 表示"因预算挤出"；排除是用户主动行为）。
        """
        new_blocks = tuple(b for b in self.blocks if b.id != block_id)
        return replace(self, blocks=new_blocks)

    def with_budget(self) -> "ContextBundle":
        """应用预算上限：超限时按 priority 升序丢弃低优先级 block（§7.2 压缩前置）。

        HARD_CONSTRAINT 级别绝不丢弃（§7.1：硬约束/用户锁定最优先）。
        返回新 bundle，被丢弃的 block id 记入 omitted_block_ids。
        """
        limit = self.budget.input_limit
        if self.total_tokens <= limit or not self.blocks:
            return self
        # 按 priority 降序排（高优先级先保留），同 priority 按原顺序
        indexed = list(enumerate(self.blocks))
        indexed.sort(key=lambda x: (-x[1].priority, x[0]))
        kept: list[tuple[int, ContextBlock]] = []
        used = 0
        omitted_ids: list[str] = []
        for orig_idx, block in indexed:
            if block.priority >= ContextBlockPriority.HARD_CONSTRAINT:
                # 硬约束永不丢
                kept.append((orig_idx, block))
                used += block.token_estimate
                continue
            if used + block.token_estimate <= limit:
                kept.append((orig_idx, block))
                used += block.token_estimate
            else:
                omitted_ids.append(block.id)
        # 恢复原顺序
        kept.sort(key=lambda x: x[0])
        return replace(
            self,
            blocks=tuple(b for _, b in kept),
            omitted_block_ids=tuple(omitted_ids),
        )

    @property
    def needs_compression(self) -> bool:
        """是否达到压缩阈值（§7.2：60% 预算触发结构化摘要）。"""
        return self.total_tokens >= self.budget.compress_threshold

    # ---------------- prompt 装配 ---------------- #

    def to_messages(self) -> list[dict[str, Any]]:
        """把 blocks 按 priority 装配为 LLM messages（替换硬编码 prompt，§7.1）。

        规则：
        - 按 priority 降序排（HARD_CONSTRAINT 最先，HISTORY 最后）。
        - system role 的块合并成一条 system 消息（开头）；user role 块合并成 user 消息。
        - 每块带可见标题（``[kind]``）便于调试/可审计。
        - content 非 str 时 JSON 序列化。
        """
        import json

        sorted_blocks = sorted(self.blocks, key=lambda b: (-b.priority, b.kind))
        system_parts: list[str] = []
        user_parts: list[str] = []
        for b in sorted_blocks:
            content = b.content
            if isinstance(content, str):
                text = content
            else:
                try:
                    text = json.dumps(content, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    text = str(content)
            marker = f"[{b.kind}]"
            line = f"{marker}\n{text}" if text else marker
            if b.prompt_role == "system":
                system_parts.append(line)
            else:
                user_parts.append(line)
        messages: list[dict[str, Any]] = []
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})
        if user_parts:
            messages.append({"role": "user", "content": "\n\n".join(user_parts)})
        return messages


__all__ = [
    "ContextBlockPriority",
    "ContextBlockKind",
    "ContextExpiry",
    "ContextBlock",
    "ContextBundle",
    "TokenBudget",
    "KIND_SYSTEM_POLICY",
    "KIND_TOOL_MANIFEST",
    "KIND_BRIEF",
    "KIND_SNAPSHOT",
    "KIND_SELECTION",
    "KIND_EVIDENCE",
    "KIND_DECISIONS",
    "KIND_HISTORY",
    "KIND_HELP_REFS",
    "EXPIRY_SESSION",
    "EXPIRY_REVISION",
    "EXPIRY_PLAN",
    "EXPIRY_NEVER",
]
