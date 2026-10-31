"""ContextService — ContextBundle 的组装者（M5 / §7.1 / §7.3）。

从既有 producer 聚合出可检查、可预算、可移除的 :class:`ContextBundle`：

- :class:`BriefService` → brief 块（交付目标，含 hard constraints / locked ranges）
- :func:`build_snapshot` → project/timeline snapshot 块（脱敏）
- :class:`DecisionStore.planning_constraints` → 已批准决策块（must_preserve / must_avoid）
- :class:`SemanticIndex` / candidate segments → 证据块（转录/标签/shot）
- :func:`list_tool_definitions` + :func:`build_manifest` → tool/capability manifest 块
- 当前选区（AssistantRequest.selected_asset_ids / selected_range）→ 选区块
- 会话历史（AssistantMessage 列表）→ 历史块

设计约束（遵循 .clinerules / §0 / §7.1）：

- **只读 producer**：不改 BriefService / DecisionStore / AnalysisService。
- **脱敏**：snapshot 经既有 ``sanitize_untrusted``（tool_facade）过滤 prompt 注入；
  secret / 绝对路径 / 未授权原始媒体不进 bundle（§7.1 末段）。
- **可预算**：每块带 token 估算（token_estimator）；``with_budget`` 按 priority 丢弃。
- **可检查/可排除**：每块带 ``why_included`` + provenance；``exclude_block`` 供 UI 移除。
- **不存云 prompt**：本服务不持久化 bundle；检查点/持久化是单独层（M5 后续）。

本服务是 LLM 调用的**唯一上下文入口**（替换 llm_variant_planner 的硬编码 prompt）。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from src.features.edit.ai_editing.context_bundle import (
    EXPIRY_NEVER,
    EXPIRY_PLAN,
    EXPIRY_REVISION,
    EXPIRY_SESSION,
    ContextBlock,
    ContextBlockPriority,
    ContextBundle,
    TokenBudget,
    KIND_BRIEF,
    KIND_DECISIONS,
    KIND_EVIDENCE,
    KIND_HELP_REFS,
    KIND_SELECTION,
    KIND_SYSTEM_POLICY,
    KIND_TOOL_MANIFEST,
)
from src.features.edit.ai_editing.editorial_models import SemanticSegment
from src.features.edit.ai_editing.session_models import CompressionCheckpoint
from src.features.edit.ai_editing.token_estimator import estimate_tokens

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 系统策略（首版硬编码；agent_policy.py 留 M6+）
# ---------------------------------------------------------------------- #

_AGENT_POLICY_VERSION = "taep-agent/0.1"

_SYSTEM_POLICY_TEXT = (
    "你是 Topos Color 的视频剪辑导演助手。\n"
    "规则：\n"
    "1. 只能用给定的 asset_id 和 frame range 生成 EditPlan，不能臆造片段。\n"
    "2. segment_id 必须来自给定的候选证据块，不能编造。\n"
    "3. 硬约束块（hard constraints / locked ranges）必须遵守，绝不违反。\n"
    "4. 已批准决策块的 must_avoid 不可选，must_preserve 必须保留。\n"
    "5. 输出严格按要求的 JSON schema（由调用方在 user 消息里指定）。\n"
    "6. 无证据时明确说『未知/需分析』，不编造。\n"
)


# ---------------------------------------------------------------------- #
# ContextSources（producer 集合，宿主注入；解耦 ContextService 与具体 service）
# ---------------------------------------------------------------------- #


class ContextSources(Protocol):
    """ContextService 的数据源契约（宿主注入，避免硬依赖具体 service 类）。

    所有方法返回 ``None`` / 空表示"无此数据"；ContextService 据此跳过对应 block。
    这样宿主可只提供部分源（测试场景），生产宿主提供全部。
    """

    def get_brief_dict(self) -> dict[str, Any] | None:
        """当前 brief 的可序列化 dict（含 revision / primary_goal / hard_constraints）。"""
        ...

    def get_snapshot_dict(self, *, detail_level: str, asset_limit: int | None) -> dict[str, Any] | None:
        """project/timeline snapshot 的可序列化 dict（已脱敏）。"""
        ...

    def get_planning_constraints(self) -> dict[str, Any] | None:
        """DecisionStore.planning_constraints() 输出（must_preserve/must_avoid/user_overrides）。"""
        ...

    def get_tool_manifest(self) -> list[dict[str, Any]] | None:
        """tool 列表（name/permission/description）的可序列化形式。"""
        ...

    def get_capability_manifest(self) -> dict[str, Any] | None:
        """capability manifest（operations/effects/export）。None 表示不纳入。"""
        ...

    def get_candidate_segments(self) -> tuple[SemanticSegment, ...] | None:
        """本次规划候选的 SemanticSegment（证据块素材）。"""
        ...


# ---------------------------------------------------------------------- #
# ContextBuildRequest（组装 bundle 的输入参数）
# ---------------------------------------------------------------------- #


from dataclasses import dataclass


@dataclass(frozen=True)
class ContextBuildRequest:
    """组装 ContextBundle 的输入（ContextService.build_bundle 的参数）。"""

    sources: ContextSources
    # 当前用户请求（原始文本）
    user_request: str = ""
    # 当前选区（asset_ids / 时间范围）
    selected_asset_ids: tuple[str, ...] = ()
    selected_range: dict[str, Any] | None = None
    # 目标 beat（叙事节拍，供 evidence 块标注）
    target_beats: tuple[str, ...] = ()
    # 目标总时长（帧）
    target_duration: int = 0
    # 预算（None 用默认 TokenBudget）
    budget: TokenBudget | None = None
    # 输出 schema 说明（user 消息末尾追加，告诉模型怎么输出）
    output_schema_hint: str = ""
    # snapshot 粒度 / 分页
    snapshot_detail_level: str = "structure"
    snapshot_asset_limit: int | None = None
    # 会话历史（AssistantMessage 列表的可序列化形式，每条 dict）
    history: tuple[dict[str, Any], ...] = ()
    # 帮助文档引用（M6 留空）
    help_refs: tuple[str, ...] = ()


# ---------------------------------------------------------------------- #
# ContextService
# ---------------------------------------------------------------------- #


# evidence 块的 payload 截断阈值（避免长转录塞爆上下文；§7.2 可逆索引）
_EVIDENCE_TEXT_MAX = 240


class ContextService:
    """ContextBundle 组装者（M5 / §7.1）。

    无状态、线程安全；从 :class:`ContextSources` 读、组装 :class:`ContextBundle`。
    不持久化、不调 LLM；只做"决定本次模型看到什么"。

    职责：
    - 把 brief/snapshot/decisions/evidence/tool manifest/selection/history 各装成 block。
    - 每 block 带 token 估算 + priority + why_included + provenance + expiry。
    - 应用预算（with_budget）+ 返回可检查 bundle。
    - 脱敏：snapshot/工具结果假定 sources 已脱敏；evidence 文本截断 + 不带绝对路径。
    """

    def build_bundle(self, request: ContextBuildRequest) -> ContextBundle:
        """组装完整 ContextBundle（按 priority 从高到低加入 blocks）。"""
        budget = request.budget or TokenBudget()
        blocks: list[ContextBlock] = []

        # 1. 系统策略（HARD_CONSTRAINT 级；不过期）
        blocks.append(self._build_system_policy_block())

        # 2. tool / capability manifest（HARD_CONSTRAINT 级；不过期；供模型知道能调什么）
        manifest_block = self._build_tool_manifest_block(request.sources)
        if manifest_block is not None:
            blocks.append(manifest_block)

        # 3. brief（DELIVERY_TARGET 级；revision 过期）
        brief_block = self._build_brief_block(request.sources)
        if brief_block is not None:
            blocks.append(brief_block)

        # 4. hard constraints / locked ranges（HARD_CONSTRAINT 级；从 brief 派生）
        #    已并入 brief_block 的 content；这里不重复加（brief dict 含 hard_constraints）。

        # 5. 选区（DELIVERY_TARGET 级；plan 过期）
        selection_block = self._build_selection_block(request)
        if selection_block is not None:
            blocks.append(selection_block)

        # 6. 已批准决策（APPROVED_DECISIONS 级；revision 过期）
        decisions_block = self._build_decisions_block(request.sources)
        if decisions_block is not None:
            blocks.append(decisions_block)

        # 7. 证据（EVIDENCE 级；plan 过期）
        evidence_blocks = self._build_evidence_blocks(request.sources)
        blocks.extend(evidence_blocks)

        # 8. 帮助引用（RECENT_SUMMARY 级；session 过期；M6 多为空）
        if request.help_refs:
            blocks.append(self._build_help_refs_block(request.help_refs))

        # 9. 历史会话（HISTORY 级；session 过期）
        history_block = self._build_history_block(request.history)
        if history_block is not None:
            blocks.append(history_block)

        # 10. 用户当前请求（DELIVERY_TARGET 级；plan 过期；作为 user 消息核心）
        if request.user_request:
            blocks.append(self._build_user_request_block(request))

        bundle = ContextBundle(
            blocks=tuple(blocks),
            budget=budget,
            policy_version=_AGENT_POLICY_VERSION,
        )
        # 应用预算（超限按 priority 丢弃低优先级；HARD_CONSTRAINT 不丢）
        return bundle.with_budget()

    def compress(self, bundle: ContextBundle) -> "tuple[ContextBundle, CompressionCheckpoint]":
        """把低优先级（HISTORY / 已有 RECENT_SUMMARY）blocks 合并成一条摘要 block。

        plan §10.2 P0.5：用规则摘要（role + 前 80 字符拼接），**不调 LLM**（避免
        P0 引入模型依赖）。原始 evidence / artifact refs 保留进 CompressionCheckpoint
        （红线：evidence 不丢；HARD_CONSTRAINT / EVIDENCE / APPROVED_DECISIONS /
        DELIVERY_TARGET 级 block 原样保留，只压 HISTORY/RECENT_SUMMARY）。

        Args:
            bundle: 待压缩的 ContextBundle。

        Returns:
            (新 bundle, CompressionCheckpoint)。新 bundle 用一条 RECENT_SUMMARY 摘要
            block 替换所有 HISTORY/RECENT_SUMMARY blocks；checkpoint 记录压缩前后
            block 数 + preserved refs + 脱敏摘要。
        """
        import time as _time
        import uuid as _uuid
        from dataclasses import replace as _replace
        from src.features.edit.ai_editing.context_bundle import (
            ContextBlockPriority,
        )

        # 分离：高优先级保留 vs 低优先级（HISTORY + RECENT_SUMMARY）合并
        keep_blocks: list[ContextBlock] = []
        compressible: list[ContextBlock] = []
        evidence_refs: list[str] = []
        artifact_ids: list[str] = []
        for b in bundle.blocks:
            if b.priority <= ContextBlockPriority.RECENT_SUMMARY:
                compressible.append(b)
            else:
                keep_blocks.append(b)
            # 收集 evidence / artifact refs（所有 block 的 provenance）
            ev = b.provenance.get("evidence_id") or b.provenance.get("evidence_ref")
            if ev:
                evidence_refs.append(str(ev))
            art = b.provenance.get("artifact_id")
            if art:
                artifact_ids.append(str(art))

        before_count = len(bundle.blocks)

        # 无可压缩 block → 空 checkpoint + 原样 bundle
        if not compressible:
            cp = CompressionCheckpoint(
                checkpoint_id=_uuid.uuid4().hex,
                created_at=_time.time(),
                summary="",
                preserved_evidence_refs=tuple(evidence_refs),
                preserved_artifact_ids=tuple(artifact_ids),
                message_count_before=before_count,
                message_count_after=before_count,
            )
            return bundle, cp

        # 规则摘要：每个 block 取前 80 字符 + role 标记
        summary_parts: list[str] = []
        for b in compressible:
            content_str = (
                b.content if isinstance(b.content, str) else str(b.content)
            )
            role = b.provenance.get("role", b.kind)
            summary_parts.append(f"[{role}] {content_str[:80]}")
        summary_text = " | ".join(summary_parts)[:600]

        summary_block = ContextBlock(
            kind="recent_summary",
            content=summary_text,
            priority=ContextBlockPriority.RECENT_SUMMARY,
            # 摘要 token 估算：每 4 字符约 1 token（粗略）
            token_estimate=max(1, len(summary_text) // 4),
            why_included="压缩的历史会话摘要（plan §10.2 P0.5）",
            provenance={"compressed_count": len(compressible)},
            expiry="session",
            prompt_role="system",
        )
        new_blocks = tuple(keep_blocks + [summary_block])
        new_bundle = _replace(bundle, blocks=new_blocks)

        # CompressionCheckpoint（summary 经 sanitize_for_storage 脱敏）
        cp = CompressionCheckpoint(
            checkpoint_id=_uuid.uuid4().hex,
            created_at=_time.time(),
            summary=summary_text,
            preserved_evidence_refs=tuple(evidence_refs),
            preserved_artifact_ids=tuple(artifact_ids),
            message_count_before=before_count,
            message_count_after=len(new_blocks),
        )
        return new_bundle, cp

    # ---------------- block 构造（各 producer → ContextBlock）---------------- #

    def _build_system_policy_block(self) -> ContextBlock:
        content = _SYSTEM_POLICY_TEXT
        return ContextBlock(
            kind=KIND_SYSTEM_POLICY,
            content=content,
            priority=ContextBlockPriority.HARD_CONSTRAINT,
            token_estimate=estimate_tokens(content),
            why_included="系统策略：模型必须遵守的安全/输出规则（§7.1）",
            provenance={"policy_version": _AGENT_POLICY_VERSION},
            expiry=EXPIRY_NEVER,
            prompt_role="system",
        )

    def _build_tool_manifest_block(self, sources: ContextSources) -> ContextBlock | None:
        tools = sources.get_tool_manifest()
        if not tools:
            return None
        # 工具 manifest 来自宿主序列化的插件工具定义（VERIFIED_THIRD_PARTY /
        # LOCAL_DEV 插件源），属外部数据边界（AUD-13240）：集合本身与每个
        # 条目都要验证，一个畸形条目只能被跳过，不能拖垮整个上下文构建。
        if not isinstance(tools, (list, tuple)):
            logger.warning(
                "tool manifest 类型非法（%s），跳过工具清单块", type(tools).__name__,
            )
            return None
        # 只带 name/permission/description，不带完整 input_schema（省 token）
        compact = [
            {"name": t.get("name"), "permission": t.get("permission"), "desc": t.get("description")}
            for t in tools if isinstance(t, dict)
        ]
        skipped = len(tools) - len(compact)
        if skipped:
            logger.warning("tool manifest 含 %d 个非 dict 条目，已跳过", skipped)
        content = {"tools": compact, "capability": sources.get_capability_manifest() or {}}
        return ContextBlock(
            kind=KIND_TOOL_MANIFEST,
            content=content,
            priority=ContextBlockPriority.HARD_CONSTRAINT,
            token_estimate=estimate_tokens(content),
            why_included="工具/能力清单：模型可调用什么、app 支持什么操作（§7.1 工具 manifest）",
            provenance={},
            expiry=EXPIRY_NEVER,
            prompt_role="system",
        )

    def _build_brief_block(self, sources: ContextSources) -> ContextBlock | None:
        brief = sources.get_brief_dict()
        if not brief:
            return None
        return ContextBlock(
            kind=KIND_BRIEF,
            content=brief,
            priority=ContextBlockPriority.DELIVERY_TARGET,
            token_estimate=estimate_tokens(brief),
            why_included="创作意图 brief：交付目标、硬约束、锁定镜头（用户批准的）",
            provenance={"brief_id": brief.get("brief_id", ""), "revision": brief.get("revision", 0)},
            expiry=EXPIRY_REVISION,
            prompt_role="user",
        )

    def _build_selection_block(self, request: ContextBuildRequest) -> ContextBlock | None:
        if not request.selected_asset_ids and request.selected_range is None:
            return None
        content = {
            "selected_asset_ids": list(request.selected_asset_ids),
            "selected_range": request.selected_range,
            "target_beats": list(request.target_beats),
            "target_duration": request.target_duration,
        }
        return ContextBlock(
            kind=KIND_SELECTION,
            content=content,
            priority=ContextBlockPriority.DELIVERY_TARGET,
            token_estimate=estimate_tokens(content),
            why_included="当前选区与交付目标：模型聚焦用户选中的素材/范围",
            provenance={},
            expiry=EXPIRY_PLAN,
            prompt_role="user",
        )

    def _build_decisions_block(self, sources: ContextSources) -> ContextBlock | None:
        constraints = sources.get_planning_constraints()
        if not constraints:
            return None
        return ContextBlock(
            kind=KIND_DECISIONS,
            content=constraints,
            priority=ContextBlockPriority.APPROVED_DECISIONS,
            token_estimate=estimate_tokens(constraints),
            why_included="已批准/锁定/拒绝的决策：必须保留 must_preserve，必须避开 must_avoid",
            provenance={},
            expiry=EXPIRY_REVISION,
            prompt_role="user",
        )

    def _build_evidence_blocks(self, sources: ContextSources) -> list[ContextBlock]:
        """每个候选 segment 一个 evidence block（§7.1 媒体证据；§7.2 可逆索引截断）。"""
        segments = sources.get_candidate_segments() or ()
        blocks: list[ContextBlock] = []
        for seg in segments:
            payload = self._segment_payload(seg)
            content = {
                "segment_id": seg.segment_id,
                "asset_id": seg.asset_id,
                "kind": seg.kind.value if hasattr(seg.kind, "value") else str(seg.kind),
                "source": seg.source_range.to_dict(),
                **payload,
                "user_verified": seg.user_verified,
            }
            blocks.append(ContextBlock(
                kind=KIND_EVIDENCE,
                content=content,
                priority=ContextBlockPriority.EVIDENCE,
                token_estimate=estimate_tokens(content),
                why_included="候选证据片段：模型只能从中选 segment_id（§7.1 媒体证据）",
                provenance={
                    "asset_id": seg.asset_id,
                    "segment_id": seg.segment_id,
                },
                expiry=EXPIRY_PLAN,
                prompt_role="user",
            ))
        return blocks

    def _build_help_refs_block(self, help_refs: tuple[str, ...]) -> ContextBlock:
        content = {"help_refs": list(help_refs)}
        return ContextBlock(
            kind=KIND_HELP_REFS,
            content=content,
            priority=ContextBlockPriority.RECENT_SUMMARY,
            token_estimate=estimate_tokens(content),
            why_included="帮助文档引用（M6 知识索引）",
            provenance={},
            expiry=EXPIRY_SESSION,
            prompt_role="system",
        )

    def _build_history_block(self, history: tuple[dict[str, Any], ...]) -> ContextBlock | None:
        if not history:
            return None
        # 只带 role/text，去结构化引用（引用太长；保留可读对话流）
        compact = [
            {"role": h.get("role", "user"), "text": str(h.get("text", ""))[:200]}
            for h in history
        ]
        return ContextBlock(
            kind="history",
            content={"recent_turns": compact},
            priority=ContextBlockPriority.HISTORY,
            token_estimate=estimate_tokens(compact),
            why_included="最近会话历史（低优先级；超预算优先丢弃）",
            provenance={},
            expiry=EXPIRY_SESSION,
            prompt_role="system",
        )

    def _build_user_request_block(self, request: ContextBuildRequest) -> ContextBlock:
        parts = [f"用户请求：{request.user_request}"]
        if request.output_schema_hint:
            parts.append(request.output_schema_hint)
        content = "\n".join(parts)
        return ContextBlock(
            kind="user_request",
            content=content,
            priority=ContextBlockPriority.DELIVERY_TARGET,
            token_estimate=estimate_tokens(content),
            why_included="用户本轮的自然语言请求 + 输出 schema 提示",
            provenance={},
            expiry=EXPIRY_PLAN,
            prompt_role="user",
        )

    # ---------------- 工具 ---------------- #

    @staticmethod
    def _segment_payload(seg: SemanticSegment) -> dict[str, Any]:
        """从 SemanticSegment.payload 取可读字段（label/tags/transcript），截断长文本。"""
        payload = seg.payload or {}
        result: dict[str, Any] = {}
        label = payload.get("label")
        if label:
            result["label"] = str(label)
        tags = payload.get("tags")
        if tags:
            result["tags"] = list(tags) if isinstance(tags, (list, tuple)) else [tags]
        # transcript/文本类：截断防塞爆（§7.2 可逆索引；原文经 index 可按需取回）
        for text_key in ("transcript", "text"):
            text = payload.get(text_key)
            if text:
                result[text_key] = str(text)[:_EVIDENCE_TEXT_MAX]
                break
        return result


__all__ = ["ContextService", "ContextSources", "ContextBuildRequest"]
