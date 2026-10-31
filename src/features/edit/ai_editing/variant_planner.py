"""TAEP Variant Planner（Phase 2，补齐）。

输入 ``EditorialBrief + SemanticSegment + 用户锁定``，输出**多个**可比较的
``StoryOutline + EditDecision + EditPlan``（TAEP §5.5 / §12 P2）。

它是 AI 剪辑的“大脑”，但**只生成 proposal，不能 commit**。

设计要点（TAEP §5.5 / §12 P2）：

- 至少给出两个可比较的方案，而非无解释地改一条时间线。
- hard constraints 只能 pass/fail；duration fit、beat coverage、pace 等只用于排序，
  且必须保留 evidence 与 alternatives（TAEP §5.5 关键规则：用户不能被迫相信单一分数）。
- 用户锁定/已接受/已拒绝的 decision 必须在重规划时保留（读 decision_service 约束）。
- 首版是**确定性规则引擎**（deterministic fake planner），用于 E2E 测试与无云场景；
  真实模型 provider 在 P6/P9 接入，替换 ``_propose_decisions`` 钩子。

参考：docs/ai_editing_protocol_and_implementation_plan_2026-07-31.md §5.5, §12 P2, §15 MVP
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from src.features.edit.ai_editing.brief_service import BriefService, LockedSourceRange
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
from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanMode,
    PlanTarget,
)
from src.features.edit.ai_editing.semantic_index import SemanticIndex


# ---------------------------------------------------------------------- #
# 规划策略
# ---------------------------------------------------------------------- #


class VariantStrategy(str):
    """variant 生成策略标签（人类可读，用于对比卡）。"""


# 首版内置两个可比较策略（TAEP §5.5：至少两个方案）
STRATEGY_A_TALKING_HEAD = "A 口播优先"  # 以 transcript 段为主，长 take
STRATEGY_B_BROLL = "B B-roll 优先"  # 以 shot/tag 段为主，快切


@dataclass(frozen=True)
class PlannerInput:
    """planner 的输入（不可变快照）。"""

    brief_id: str
    brief_revision: int
    asset_id: str  # 主素材
    timeline_id: str  # 目标 timeline
    project_id: str
    base_revision: int  # timeline base revision（乐观锁）
    analysis_snapshot_hash: str = ""
    target_duration: int = 720  # 帧（默认 30s@24fps）


@dataclass(frozen=True)
class PlannerConstraints:
    """从 brief + decision_service 提取的硬约束（重规划必须遵守）。"""

    locked_ranges: tuple[LockedSourceRange, ...] = ()
    rejected_subjects: tuple[str, ...] = ()  # asset_id:start-dur
    must_preserve_decisions: tuple[str, ...] = ()


# ---------------------------------------------------------------------- #
# VariantPlanner
# ---------------------------------------------------------------------- #


class VariantPlanner:
    """生成多个可比较的剪辑方案（proposal only，不 commit）。

    首版是确定性规则引擎：按 beat 顺序从 transcript/shot segment 选段，两种策略
    在选段密度与时长分配上不同，产出可比较的 A/B 方案。真实模型在 P6 接入。
    """

    def __init__(self, brief_service: BriefService | None = None) -> None:
        self._brief_service = brief_service or BriefService()

    @property
    def brief_service(self) -> BriefService:
        return self._brief_service

    def plan(
        self,
        inp: PlannerInput,
        index: SemanticIndex,
        *,
        constraints: PlannerConstraints | None = None,
        target_beats: tuple[str, ...] = ("hook", "product_value", "call_to_action"),
        strategies: tuple[str, ...] = (STRATEGY_A_TALKING_HEAD, STRATEGY_B_BROLL),
        on_plan_compiled: "Callable[[EditPlan, EditVariant], None] | None" = None,
    ) -> list[EditVariant]:
        """生成至少两个可比较的 variant。

        流程：
        1. 读取 brief 约束（锁定/forbidden/must_include）。
        2. 对每个策略：按 beat 顺序从 segment 选段（避开 rejected，保留 locked）。
        3. 构建 StoryOutline + EditDecision（带 evidence/alternatives）。
        4. 编译 EditPlan（add_clip operations）。
        5. 计算 scores（hard pass/fail + duration fit + beat coverage）。

        Args:
            on_plan_compiled: 可选回调；每编译出一个 plan 就以
                ``(plan, variant)`` 调用一次。供 ``AssistantService`` 把 plan 存入
                ``PlanRepository``（M2.1），使 variant 的 ``edit_plan_ref`` 能解析。
                不提供时行为与旧版一致（plan 编译后丢弃，仅保留 plan_id 引用）。
        """
        constraints = constraints or PlannerConstraints()
        brief_hard = self._load_brief_hard_constraints(inp.brief_id)
        merged_locked = tuple(constraints.locked_ranges) + brief_hard

        variants: list[EditVariant] = []
        for strategy in strategies:
            variant, plan = self._build_variant(
                inp, index, strategy, merged_locked,
                constraints.rejected_subjects, target_beats,
            )
            if on_plan_compiled is not None and plan is not None:
                on_plan_compiled(plan, variant)
            variants.append(variant)
        return variants

    # ---------------- 内部：单 variant 构建 ---------------- #

    def _build_variant(
        self,
        inp: PlannerInput,
        index: SemanticIndex,
        strategy: str,
        locked: tuple[LockedSourceRange, ...],
        rejected: tuple[str, ...],
        target_beats: tuple[str, ...],
    ) -> "tuple[EditVariant, EditPlan]":
        # 选段：按 beat 把目标时长切分，从 transcript/shot 选 evidence
        beat_ranges = self._allocate_beat_ranges(target_beats, inp.target_duration, strategy)
        outline: list[StoryBeat] = []
        decisions: list[EditDecision] = []

        for beat_id, beat_range in beat_ranges:
            segment = self._select_segment_for_beat(
                inp.asset_id, beat_range, index, strategy, locked, rejected
            )
            evidence = (segment.segment_id,) if segment else ()
            outline.append(StoryBeat(
                beat_id=beat_id,
                target_range=beat_range,
                evidence=evidence,
                notes=strategy,
            ))
            if segment is not None:
                # 生成 decision（带 alternatives）
                alternatives = self._alternatives_for_segment(inp.asset_id, segment, index)
                decisions.append(EditDecision(
                    decision_id=f"dec-{beat_id}-{uuid.uuid4().hex[:6]}",
                    action="select_source_range",
                    subject=SourceRef(segment.asset_id, segment.source_range),
                    supports=(beat_id,),
                    evidence_refs=evidence,
                    alternatives_considered=tuple(alternatives),
                    confidence=segment.confidence,
                    explanation=self._explain(beat_id, segment, strategy),
                    state=DecisionState.PROPOSED,
                ))

        # 编译 EditPlan
        plan = self._compile_plan(inp, decisions, outline)

        # 评分（硬约束 pass/fail 基于锁定区间的真实覆盖情况计算）
        scores = self._score(inp, outline, decisions, beat_ranges, locked)

        variant = EditVariant(
            variant_id=f"variant-{uuid.uuid4().hex[:8]}",
            brief_id=inp.brief_id,
            brief_revision=inp.brief_revision,
            analysis_snapshot_hash=inp.analysis_snapshot_hash,
            status=VariantStatus.PROPOSED,
            label=strategy,
            outline=tuple(outline),
            decisions=tuple(decisions),
            scores=scores,
            edit_plan_ref=plan.plan_id,
        )
        return variant, plan

    # ---------------- 内部：beat 时长分配 ---------------- #

    def _allocate_beat_ranges(
        self, beats: tuple[str, ...], total: int, strategy: str
    ) -> list[tuple[str, FrameRange]]:
        """把总时长按 beat 分配。两种策略分配权重不同（可比较）。"""
        if not beats:
            return []
        # A 策略：hook 短、中段长、cta 短（口播展开）
        # B 策略：hook 较长、中段紧凑、cta 适中（B-roll 吸引）
        if strategy == STRATEGY_A_TALKING_HEAD:
            weights = {"hook": 0.15, "product_value": 0.6, "call_to_action": 0.25}
        else:
            weights = {"hook": 0.3, "product_value": 0.5, "call_to_action": 0.2}
        # 归一化（未列出的 beat 平分剩余）
        listed = sum(weights.get(b, 0) for b in beats)
        remainder = max(0.0, 1.0 - listed)
        unlisted_count = sum(1 for b in beats if b not in weights)
        result: list[tuple[str, FrameRange]] = []
        cursor = 0
        for b in beats:
            w = weights.get(b, remainder / unlisted_count if unlisted_count else 0)
            dur = max(1, int(total * w))
            result.append((b, FrameRange(cursor, dur)))
            cursor += dur
        return result

    # ---------------- 内部：segment 选择（带约束）---------------- #

    def _select_segment_for_beat(
        self,
        asset_id: str,
        beat_range: FrameRange,
        index: SemanticIndex,
        strategy: str,
        locked: tuple[LockedSourceRange, ...],
        rejected: tuple[str, ...],
    ) -> SemanticSegment | None:
        """为某 beat 选一个 evidence segment（避开 rejected，优先 locked/user_verified）。"""
        # 优先 transcript（口播策略）/ shot（B-roll 策略）
        prefer_kind = "transcript" if strategy == STRATEGY_A_TALKING_HEAD else "shot"
        from src.features.edit.ai_editing.editorial_models import SegmentKind

        candidates = index.query(
            asset_id=asset_id,
            kind=SegmentKind(prefer_kind),
            within=beat_range,
        )
        if not candidates:
            candidates = index.query(asset_id=asset_id, within=beat_range)
        # 过滤 rejected
        def _subject_sig(s: SemanticSegment) -> str:
            return f"{s.asset_id}:{s.source_range.start}-{s.source_range.duration}"
        candidates = [c for c in candidates if _subject_sig(c) not in rejected]
        # AUD-14920：用户锁定的 source range 必须优先（TAEP §5.3：锁定永远
        # 优先于 AI 重规划）。与锁定区间相交的候选排到最前；sort 稳定，
        # 组内保持 query 的 user_verified/confidence 排序。
        locked_ranges = tuple(lr.source for lr in locked if lr.asset_id == asset_id)
        candidates.sort(
            key=lambda c: not any(c.source_range.overlaps(r) for r in locked_ranges)
        )
        if not candidates:
            return None
        # locked 优先、user_verified 优先、confidence 高优先（query 已排序，取首个）
        return candidates[0]

    def _alternatives_for_segment(
        self, asset_id: str, chosen: SemanticSegment, index: SemanticIndex
    ) -> list[str]:
        """取同 asset 相邻的其他候选作为备选（TAEP §5.5：保留 alternatives）。"""

        peers = index.query(asset_id=asset_id, kind=chosen.kind)
        return [
            p.segment_id for p in peers
            if p.segment_id != chosen.segment_id
        ][:3]

    def _explain(self, beat_id: str, segment: SemanticSegment, strategy: str) -> str:
        payload = segment.payload or {}
        text = payload.get("text", "")
        if text:
            return f"[{strategy}] {beat_id}: 选用转录“{text[:30]}”（置信度 {segment.confidence:.2f}）"
        return f"[{strategy}] {beat_id}: 选用镜头段 [{segment.source_range.start}, {segment.source_range.end})"

    # ---------------- 内部：EditPlan 编译 ---------------- #

    def _compile_plan(
        self, inp: PlannerInput, decisions: list[EditDecision], outline: list[StoryBeat]
    ) -> EditPlan:
        """把 decisions 编译为 add_clip operations 的 EditPlan。"""
        ops: list[EditOperation] = []
        for beat_id, beat_range in [(b.beat_id, b.target_range) for b in outline]:
            # 找对应 decision
            dec = next((d for d in decisions if beat_id in d.supports), None)
            if dec is None or dec.subject is None:
                continue
            ops.append(EditOperation(
                op_id=f"op-{beat_id}-{uuid.uuid4().hex[:6]}",
                kind=OperationKind.ADD_CLIP,
                params={
                    "clip_id": f"clip-{beat_id}",
                    "track_id": "v1",
                    "asset_id": dec.subject.asset_id,
                    "source": dec.subject.source.to_dict(),
                    "timeline": beat_range.to_dict(),
                },
                intent={"reason": self._explain(beat_id, SemanticSegment(
                    segment_id="planned", asset_id=dec.subject.asset_id,
                    source_range=dec.subject.source,
                    kind=__import__("src.features.edit.ai_editing.editorial_models", fromlist=["SegmentKind"]).SegmentKind.TRANSCRIPT,
                ), "")},
            ))
        return EditPlan.create(
            target=PlanTarget(inp.project_id, inp.timeline_id),
            base_revision=inp.base_revision,
            operations=tuple(ops),
            mode=PlanMode.PREVIEW,
            title=f"{outline[0].notes if outline else 'plan'}",
            brief_id=inp.brief_id,
        )

    # ---------------- 内部：评分 ---------------- #

    def _score(
        self,
        inp: PlannerInput,
        outline: list[StoryBeat],
        decisions: list[EditDecision],
        beat_ranges: list[tuple[str, FrameRange]],
        locked: tuple[LockedSourceRange, ...] = (),
    ) -> VariantScores:
        """计算仅用于排序的评分（hard 必须 pass/fail）。"""
        # beat coverage：有 evidence 的 beat 占比
        covered = sum(1 for b in outline if b.evidence)
        total = len(beat_ranges) or 1
        beat_coverage = covered / total

        # duration fit：实际选段总时长 / 目标
        actual = sum(b.target_range.duration for b in outline)
        duration_fit = min(1.0, actual / max(1, inp.target_duration))

        # pace：decisions 数 / 目标时长（越密越快；归一化到 0..1）
        pace = min(1.0, len(decisions) / max(1, inp.target_duration / 120))

        # AUD-14920：硬约束 pass/fail 必须真实计算。每个用户锁定的 source
        # range 都必须被某个 decision 的 subject 覆盖（相交）；任一锁定区间
        # 未出现在方案中即 fail，不得向用户断言假的 pass 徽章。
        selected_sources = [
            d.subject.source for d in decisions if d.subject is not None
        ]
        locked_missed = tuple(
            lr for lr in locked
            if not any(sel.overlaps(lr.source) for sel in selected_sources)
        )
        hard_passed = not locked_missed

        explanation = "仅作排序依据，不替代人工判断。"
        if not hard_passed:
            explanation += (
                f" 硬约束未通过：{len(locked_missed)} 个用户锁定区间未被任何"
                "选段覆盖。"
            )

        return VariantScores(
            hard_constraints_passed=hard_passed,
            duration_fit=round(duration_fit, 3),
            beat_coverage=round(beat_coverage, 3),
            pace_estimate=round(pace, 3),
            explanation=explanation,
        )

    # ---------------- 内部：brief 约束加载 ---------------- #

    def _load_brief_hard_constraints(
        self, brief_id: str
    ) -> tuple[LockedSourceRange, ...]:
        """从 brief_service 读取锁定的 source range（重规划必须保留）。"""
        return tuple(self._brief_service.locked_source_ranges(brief_id))


# ---------------------------------------------------------------------- #
# 比较（TAEP §7 topos.compare_variants）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class VariantComparison:
    """多个 variant 的结构化比较（TAEP §7 compare_variants 输出）。"""

    by_variant: dict[str, dict[str, Any]] = field(default_factory=dict)

    def rank_by(self, dimension: str = "duration_fit") -> list[str]:
        """按某维度排序返回 variant_id 列表（降序）。"""
        return sorted(
            self.by_variant.keys(),
            key=lambda vid: self.by_variant[vid].get(dimension, 0.0),
            reverse=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"by_variant": dict(self.by_variant)}


def compare_variants(variants: list[EditVariant]) -> VariantComparison:
    """比较多个 variant：时长、约束覆盖、证据、关键差异（TAEP §7）。"""
    by: dict[str, dict[str, Any]] = {}
    for v in variants:
        dur = sum(b.target_range.duration for b in v.outline)
        by[v.variant_id] = {
            "label": v.label,
            "duration_frames": dur,
            "hard_constraints_passed": v.scores.hard_constraints_passed,
            "beat_coverage": v.scores.beat_coverage,
            "duration_fit": v.scores.duration_fit,
            "pace_estimate": v.scores.pace_estimate,
            "decision_count": len(v.decisions),
            "evidence_count": sum(len(d.evidence_refs) for d in v.decisions),
        }
    return VariantComparison(by_variant=by)
