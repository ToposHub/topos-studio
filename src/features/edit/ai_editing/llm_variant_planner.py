"""LLM-backed Variant Planner（TAEP §5.5 / §12 P6）。

用真实大模型替代 ``VariantPlanner`` 的规则引擎：把 brief + 可用 segment 摘要喂给
LLM，让它产出**至少两个**可比较的剪辑方案（每个 beat 选哪个 segment、用什么节奏），
然后**复用现有可靠的 ``_compile_plan`` 把决策编译成 EditPlan**。

设计取舍（重要）：
- 不让 LLM 直接生成完整 EditPlan JSON——那需要它精确产出 OperationKind/params/
  FrameRange 等严格 schema，不同模型遵循度差异大、极易解析失败。
- 改为让 LLM 做"创意决策"（选段 + 节奏 + beat 分配），planner 用与规则引擎完全
  相同的编译路径（``_compile_plan``）把决策落到 EditPlan。这样创意用 LLM、
  结构正确性靠已验证的编译器，鲁棒性最高。
- LLM 输出 JSON 失败时抛 ``LLMPlannerError``，由 ``AssistantService`` 转为
  FAILED 状态并回显给用户（不崩 service）。

与 ``VariantPlanner.plan()`` 同签名，可经 ``AssistantService(planner=...)`` 直接注入。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - annotation-only imports
    from src.features.edit.ai_editing.context_service import ContextService, ContextSources

from src.features.edit.ai_editing.brief_service import BriefService
from src.features.edit.ai_editing.editorial_models import (
    DecisionState,
    EditDecision,
    EditVariant,
    FrameRange,
    SemanticSegment,
    SourceRef,
    StoryBeat,
    VariantScores,
    VariantStatus,
)
from src.features.edit.ai_editing.llm_client import LLMClient, make_client
from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanMode,
    PlanTarget,
)
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    CredentialStore,
)
from src.features.edit.ai_editing.structured_output import (
    PLANNER_SCHEMA_VERSION,
    StructuredOutputError,
    validate_planner_output,
)
from src.features.edit.ai_editing.evidence_policy import validate_evidence_selection
from src.features.edit.ai_editing.semantic_index import SemanticIndex
from src.features.edit.ai_editing.variant_planner import (
    PlannerConstraints,
    PlannerInput,
)

logger = logging.getLogger(__name__)


class LLMPlannerError(RuntimeError):
    """LLM 规划失败（LLM 调用 / JSON 解析 / 决策校验），含可读原因。"""


class UnavailablePlanner:
    """Explicit planner used when a configured model fails local startup checks.

    This is intentionally not the deterministic ``VariantPlanner`` fallback:
    production configuration errors must be visible to the user and must not be
    mistaken for a successful model-backed plan.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def plan(self, *args: Any, **kwargs: Any) -> list[EditVariant]:
        raise LLMPlannerError(self.reason)


@dataclass(frozen=True)
class _LLMDecision:
    """LLM 为单个 beat 产出的决策（创意层，编译前）。"""

    beat_id: str
    segment_id: str  # 选用的 SemanticSegment.segment_id
    confidence: float
    explanation: str


@dataclass(frozen=True)
class _LLMVariant:
    """LLM 产出的一个方案（创意层）。"""

    label: str
    rationale: str
    decisions: tuple[_LLMDecision, ...]


# ---------------------------------------------------------------------- #
# LLMVariantPlanner
# ---------------------------------------------------------------------- #


class LLMVariantPlanner:
    """用真实 LLM 生成 ≥2 个可比较剪辑方案。

    通过 ``make_client`` 根据 profile 构造对应协议的 client；规划流程：
    1. 从 SemanticIndex 取主素材的全部 segment 作为候选喂给 LLM。
    2. 构造 system prompt（角色 + 输出 JSON schema 约束）。
    3. 调 ``LLMClient.complete(json_mode=True)``。
    4. 解析 JSON → 每个 variant 的创意决策。
    5. 复用 ``_compile_plan`` 把决策编译成 EditPlan（结构与规则引擎一致）。
    """

    def __init__(
        self,
        profile: AiProviderProfile,
        credential_store: CredentialStore,
        brief_service: BriefService | None = None,
        client: LLMClient | None = None,
        context_service: "ContextService | None" = None,
        context_sources_factory: "Callable[..., ContextSources] | None" = None,
        reasoning_effort: str = "medium",
    ) -> None:
        self._profile = profile
        self._brief_service = brief_service or BriefService()
        # client 可注入（测试用）；否则按 profile 工厂构造
        self._client = client or make_client(profile, credential_store)
        # M5：可选 ContextService。提供时用 ContextBundle 装配 prompt（替换硬编码 2-message），
        # 使 snapshot/decisions/tool manifest 进上下文，且每块可检查/可预算/可排除（§7.1）。
        # context_sources_factory：生产宿主注入，签名 (candidates, brief_ctx, inp, target_beats)
        # → ContextSources。None 时走旧 _build_messages 路径（向后兼容）。
        self._context_service = context_service
        self._context_sources_factory = context_sources_factory
        self._reasoning_effort = (
            reasoning_effort if reasoning_effort in ("low", "medium", "high") else "medium"
        )

    @property
    def brief_service(self) -> BriefService:
        return self._brief_service

    # ------------------------------------------------------------------ #
    # 主接口（与 VariantPlanner.plan 同签名）
    # ------------------------------------------------------------------ #

    def plan(
        self,
        inp: PlannerInput,
        index: SemanticIndex,
        *,
        constraints: PlannerConstraints | None = None,
        target_beats: tuple[str, ...] = ("hook", "product_value", "call_to_action"),
        strategies: tuple[str, ...] = (),
        on_plan_compiled: "Callable[[EditPlan, EditVariant], None] | None" = None,
    ) -> list[EditVariant]:
        """生成至少两个可比较的 variant（由 LLM 驱动）。

        ``strategies`` 参数被忽略——改由 LLM 自主决定方案多样性（在 prompt 里
        要求"至少两个风格不同的方案"）。
        """
        constraints = constraints or PlannerConstraints()
        # 1. 收集候选 segment（主素材；过滤被拒绝的）
        candidates = self._collect_candidates(inp, index, constraints)
        if not candidates:
            raise LLMPlannerError(
                f"素材 {inp.asset_id} 没有可用的分析片段（semantic segment），"
                "请先对该素材进行分析。"
            )

        # 2. 读 brief 上下文
        brief_ctx = self._brief_context(inp.brief_id)

        # 3. 构造 prompt 并调用 LLM
        #    M5：若注入了 ContextService，经 ContextBundle 装配（snapshot/decisions/
        #    tool manifest 进上下文，每块可检查/可预算/可排除，§7.1）；否则走旧硬编码路径。
        messages = self._build_prompt_messages(
            brief_ctx, candidates, target_beats, inp,
        )
        # 当前各 provider 没有统一的 reasoning_effort 参数；用温度与提示强度
        # 做 transport-neutral 映射，保证低/中/高选择对后续请求生效。
        temperature = {"low": 0.8, "medium": 0.6, "high": 0.3}[self._reasoning_effort]
        raw = self._client.complete(messages, json_mode=True, temperature=temperature)
        llm_variants = self._parse_variants(raw, target_beats)
        if len(llm_variants) < 2:
            raise LLMPlannerError(
                f"LLM 返回的方案不足 2 个（得到 {len(llm_variants)} 个）。"
            )

        # 4. 把每个创意方案编译成 EditVariant + EditPlan
        results: list[EditVariant] = []
        seg_lookup = {s.segment_id: s for s in candidates}
        for lv in llm_variants:
            beat_ids = tuple(decision.beat_id for decision in lv.decisions)
            missing_beats = tuple(
                beat for beat in target_beats if beat not in beat_ids
            )
            evidence_check = validate_evidence_selection(
                (decision.segment_id for decision in lv.decisions),
                seg_lookup,
                required_count=len(target_beats),
                allowed_asset_ids=(inp.asset_id,),
            )
            if missing_beats or not evidence_check.valid:
                details = []
                if missing_beats:
                    details.append(f"missing target beats: {list(missing_beats)}")
                if not evidence_check.valid:
                    details.append(evidence_check.reason)
                raise LLMPlannerError(
                    "LLM_EVIDENCE_INVALID: " + "; ".join(details)
                )
            variant, plan = self._compile_variant(
                inp, lv, seg_lookup, target_beats
            )
            if on_plan_compiled is not None and plan is not None:
                on_plan_compiled(plan, variant)
            results.append(variant)
        return results

    # ------------------------------------------------------------------ #
    # 内部：候选收集
    # ------------------------------------------------------------------ #

    def _collect_candidates(
        self,
        inp: PlannerInput,
        index: SemanticIndex,
        constraints: PlannerConstraints,
    ) -> list[SemanticSegment]:
        segments = [s for s in index.for_asset(inp.asset_id)]
        # 过滤被拒绝的 subject（格式 "asset_id:start-dur"）
        rejected = set(constraints.rejected_subjects)
        return [
            s for s in segments
            if f"{s.asset_id}:{s.source_range.start}-{s.source_range.duration}"
            not in rejected
        ]

    # ------------------------------------------------------------------ #
    # 内部：brief 上下文
    # ------------------------------------------------------------------ #

    def _brief_context(self, brief_id: str) -> dict[str, Any]:
        brief = self._brief_service.get(brief_id)
        if brief is None:
            return {"id": brief_id, "summary": "(未找到 brief，请用默认意图)"}
        # EditorialBrief 的字段：取关键意图字段（宽容处理不同版本）
        return {
            "id": brief_id,
            "title": getattr(brief, "title", ""),
            "target_platform": getattr(brief, "target_platform", ""),
            "target_duration_frames": getattr(brief, "target_duration_frames", ""),
            "audience": getattr(brief, "audience", ""),
            "must_include": list(getattr(brief, "must_include", []) or []),
            "forbidden": list(getattr(brief, "forbidden", []) or []),
        }

    # ------------------------------------------------------------------ #
    # 内部：prompt 构造
    # ------------------------------------------------------------------ #

    def _build_prompt_messages(
        self,
        brief_ctx: dict[str, Any],
        candidates: list[SemanticSegment],
        target_beats: tuple[str, ...],
        inp: "PlannerInput",
    ) -> list[dict[str, str]]:
        """构造 LLM prompt（M5：优先走 ContextBundle 路径；否则旧硬编码）。

        ContextBundle 路径（§7.1）：把 brief/snapshot/decisions/evidence/tool manifest
        各装成 block，按 priority 装配 system+user 消息。每块可检查/可预算/可排除，
        替换旧的硬编码 2-message prompt。旧路径保留向后兼容（无 ContextService 注入时）。
        """
        if self._context_service is not None and self._context_sources_factory is not None:
            return self._build_messages_via_context(
                brief_ctx, candidates, target_beats, inp,
            )
        return self._build_messages(brief_ctx, candidates, target_beats, inp)

    def _build_messages_via_context(
        self,
        brief_ctx: dict[str, Any],
        candidates: list[SemanticSegment],
        target_beats: tuple[str, ...],
        inp: "PlannerInput",
    ) -> list[dict[str, str]]:
        """经 ContextBundle 装配 prompt（M5 / §7.1）。

        要求 ``_context_sources_factory`` 提供 ContextSources（生产宿主注入，封装对
        BriefService/snapshot/DecisionStore/AnalysisService/tool_facade 的读取）。
        """
        assert self._context_service is not None
        assert self._context_sources_factory is not None
        sources = self._context_sources_factory(
            candidates=candidates,
            brief_ctx=brief_ctx,
            inp=inp,
            target_beats=target_beats,
        )
        # output_schema_hint：告诉模型期望的 JSON 结构（旧 system prompt 里的 schema 说明
        # 现经 user 消息的 user_request 块携带，保持模型输出契约不变）。
        schema_hint = (
            "严格按以下 JSON schema 输出（只输出 JSON，不要 markdown 代码块）：\n"
            '{"schema_version":"' + PLANNER_SCHEMA_VERSION + '","variants":[{"label":"...","rationale":"...","decisions":'
            '[{"beat_id":"...","segment_id":"...","confidence":0.0-1.0,"explanation":"..."}]}]}\n'
            "规则：至少 2 个风格不同的方案；decisions 必须覆盖 target_beats；"
            "segment_id 必须来自候选证据块；confidence 为 0.0-1.0。"
        )
        from src.features.edit.ai_editing.context_bundle import TokenBudget
        from src.features.edit.ai_editing.context_service import ContextBuildRequest

        request = ContextBuildRequest(
            sources=sources,
            user_request="生成至少两个风格不同的剪辑方案。",
            selected_asset_ids=(inp.asset_id,),
            target_beats=target_beats,
            target_duration=inp.target_duration,
            budget=TokenBudget(limit=self._profile.limits.max_context_tokens),
            output_schema_hint=schema_hint,
        )
        bundle = self._context_service.build_bundle(request)
        messages = bundle.to_messages()
        if not messages:
            # 极端情况（无任何 block）→ 兜底旧路径，避免空 prompt
            return self._build_messages(brief_ctx, candidates, target_beats, inp)
        return messages

    def _build_messages(
        self,
        brief_ctx: dict[str, Any],
        candidates: list[SemanticSegment],
        target_beats: tuple[str, ...],
        inp: PlannerInput,
    ) -> list[dict[str, str]]:
        seg_descriptions = [
            {
                "segment_id": s.segment_id,
                "asset_id": s.asset_id,
                "kind": s.kind.value if hasattr(s.kind, "value") else str(s.kind),
                "source": s.source_range.to_dict(),
                # label/tags/transcript 存在 segment.payload 里（TAEP §5.4 最小字段）
                "label": str(s.payload.get("label", "") or ""),
                "tags": list(s.payload.get("tags", []) or []),
                "transcript": str(s.payload.get("transcript", "") or "")[:200],
                "user_verified": s.user_verified,
            }
            for s in candidates
        ]

        system = (
            "你是专业的视频剪辑导演助手。基于给定的素材片段和创作意图，"
            "产出至少两个**风格不同**的可比较剪辑方案。\n\n"
            "严格按以下 JSON schema 输出（只输出 JSON，不要 markdown 代码块）：\n"
            f'{{"schema_version":"{PLANNER_SCHEMA_VERSION}", "variants": [\n'
            "    {\n"
            '      "label": "方案A的人类可读名",\n'
            '      "rationale": "为什么这么剪",\n'
            '      "decisions": [\n'
            '        {"beat_id": "...", "segment_id": "...", '
            '"confidence": 0.0-1.0, "explanation": "..."}\n'
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "规则：\n"
            "1. 至少 2 个方案，风格要不同（如口播优先 vs 快切 B-roll）。\n"
            "2. 每个方案的 decisions 必须覆盖所有 target_beats。\n"
            "3. segment_id 必须从给定的候选片段中选择，不能臆造。\n"
            "4. confidence 是 0.0-1.0 的浮点，表示你对这个选择的把握。\n"
            "5. target_duration 是目标成片总帧数，分配给各 beat 时长之和应接近它。"
        )

        user = (
            f"创作意图（brief）：\n{json.dumps(brief_ctx, ensure_ascii=False)}\n\n"
            f"目标 beat（叙事节拍）：{list(target_beats)}\n"
            f"目标总时长（帧）：{inp.target_duration}\n\n"
            f"可用素材片段（候选）：\n{json.dumps(seg_descriptions, ensure_ascii=False)}"
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    # ------------------------------------------------------------------ #
    # 内部：解析 LLM 输出
    # ------------------------------------------------------------------ #

    def _parse_variants(
        self, raw: str, target_beats: tuple[str, ...]
    ) -> list[_LLMVariant]:
        """解析 LLM 返回的 JSON 为创意方案列表。"""
        # 容错：剥离可能的 markdown 代码块包裹
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            envelope = validate_planner_output(text)
        except StructuredOutputError as exc:
            raise LLMPlannerError(f"{exc.code}: {exc}") from exc
        raw_variants = envelope.variants

        results: list[_LLMVariant] = []
        for rv in raw_variants:
            if not isinstance(rv, dict):
                continue
            decisions: list[_LLMDecision] = []
            for rd in rv.get("decisions", []):
                if not isinstance(rd, dict):
                    continue
                try:
                    decisions.append(_LLMDecision(
                        beat_id=str(rd["beat_id"]),
                        segment_id=str(rd["segment_id"]),
                        confidence=max(0.0, min(1.0, float(rd.get("confidence", 0.5)))),
                        explanation=str(rd.get("explanation", "")),
                    ))
                except (KeyError, ValueError, TypeError):
                    continue
            if not decisions:
                continue
            results.append(_LLMVariant(
                label=str(rv.get("label", "未命名方案")),
                rationale=str(rv.get("rationale", "")),
                decisions=tuple(decisions),
            ))
        return results

    # ------------------------------------------------------------------ #
    # 内部：编译创意方案 → EditVariant + EditPlan
    # ------------------------------------------------------------------ #

    def _compile_variant(
        self,
        inp: PlannerInput,
        lv: _LLMVariant,
        seg_lookup: dict[str, SemanticSegment],
        target_beats: tuple[str, ...],
    ) -> "tuple[EditVariant, EditPlan]":
        """把创意决策编译成 EditVariant + EditPlan。

        复用与规则引擎一致的 add_clip 结构（track_id=v1，timeline 按 beat 分配）。
        """
        # 按 target_beats 平均分配目标时长
        beat_ranges = _allocate_beat_ranges(target_beats, inp.target_duration)

        outline: list[StoryBeat] = []
        decisions: list[EditDecision] = []
        ops: list[EditOperation] = []

        # 建立 beat_id → LLM decision 的查找
        llm_by_beat = {d.beat_id: d for d in lv.decisions}

        for beat_id, beat_range in beat_ranges:
            llm_dec = llm_by_beat.get(beat_id)
            # 选段：优先 LLM 指定的；找不到则跳过该 beat
            if llm_dec is None or llm_dec.segment_id not in seg_lookup:
                continue
            seg = seg_lookup[llm_dec.segment_id]

            evidence = (seg.segment_id,)
            outline.append(StoryBeat(
                beat_id=beat_id,
                target_range=beat_range,
                evidence=evidence,
                notes=llm_dec.explanation,
            ))
            decision_id = f"dec-{beat_id}-{uuid.uuid4().hex[:6]}"
            decisions.append(EditDecision(
                decision_id=decision_id,
                action="select_source_range",
                subject=SourceRef(asset_id=seg.asset_id, source=seg.source_range),
                supports=(beat_id,),
                evidence_refs=evidence,
                alternatives_considered=(),
                confidence=llm_dec.confidence,
                explanation=llm_dec.explanation,
                state=DecisionState.PROPOSED,
            ))
            ops.append(EditOperation(
                op_id=f"op-{beat_id}-{uuid.uuid4().hex[:6]}",
                kind=OperationKind.ADD_CLIP,
                params={
                    "clip_id": f"clip-{beat_id}",
                    "track_id": "v1",
                    "asset_id": seg.asset_id,
                    "source": seg.source_range.to_dict(),
                    "timeline": beat_range.to_dict(),
                },
                intent={"reason": llm_dec.explanation, "beat": beat_id},
            ))

        plan = EditPlan.create(
            target=PlanTarget(project_id=inp.project_id, timeline_id=inp.timeline_id),
            base_revision=inp.base_revision,
            operations=tuple(ops),
            mode=PlanMode.PREVIEW,
            title=lv.label,
            rationale=lv.rationale,
            brief_id=inp.brief_id,
            analysis_snapshot_hash=inp.analysis_snapshot_hash,
        )

        variant_id = f"var-{uuid.uuid4().hex[:10]}"
        variant = EditVariant(
            variant_id=variant_id,
            brief_id=inp.brief_id,
            brief_revision=inp.brief_revision,
            analysis_snapshot_hash=inp.analysis_snapshot_hash,
            status=VariantStatus.PROPOSED,
            label=lv.label,
            outline=tuple(outline),
            decisions=tuple(decisions),
            scores=_score_variant(outline, target_beats),
            edit_plan_ref=plan.plan_id,
        )
        return variant, plan


# ---------------------------------------------------------------------- #
# 辅助函数
# ---------------------------------------------------------------------- #


def _allocate_beat_ranges(
    beats: tuple[str, ...], total: int
) -> list[tuple[str, FrameRange]]:
    """把目标总时长平均分给各 beat（与规则引擎策略一致）。

    规则引擎（variant_planner）对每个 beat 都用 ``max(1, ...)`` 保证正时长；
    这里保持一致：``total`` 非正时抛可读的 ``LLMPlannerError``（模块契约：
    规划失败抛 LLMPlannerError），``total < len(beats)`` 时每个 beat 至少
    占 1 帧，绝不让 FrameRange 收到非正 duration。
    """
    if not beats:
        return []
    if total <= 0:
        raise LLMPlannerError(
            f"target_duration 必须为正数帧（当前 {total}），无法为 {len(beats)} 个 beat 分配时长"
        )
    per = max(1, total // len(beats))
    result: list[tuple[str, FrameRange]] = []
    pos = 0
    for i, beat_id in enumerate(beats):
        dur = per + (total - per * len(beats)) if i == len(beats) - 1 else per
        dur = max(1, dur)
        result.append((beat_id, FrameRange(start=pos, duration=dur)))
        pos += dur
    return result


def _score_variant(
    outline: list[StoryBeat], target_beats: tuple[str, ...]
) -> VariantScores:
    """粗略评分：beat 覆盖率（LLM 方案的质量主要由后续 validate 兜底）。"""
    covered = {b.beat_id for b in outline}
    coverage = len(covered & set(target_beats)) / max(1, len(target_beats))
    return VariantScores(
        hard_constraints_passed=True,
        duration_fit=0.8,
        beat_coverage=coverage,
        pace_estimate=0.7,
        explanation=f"LLM 方案，覆盖 {len(covered)}/{len(target_beats)} 个 beat",
    )
