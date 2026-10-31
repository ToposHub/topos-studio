"""Cross-domain cost control + early delivery validation (IX-03 / IX-05).

IX-03 adds dirty-range cost control so "修改字幕不触发无关的全片 comp".  The
existing composite engine has internal dirty detection (``upstream_revisions``
per node port); this module extends that idea *across* domains with a shared
dependency graph, preview-resolution tiering and budget prediction.

IX-05 validates delivery specs *at brief stage* rather than at export time,
so an incompatible asset/effect/codec is discovered before the user invests
in a plan.  The existing ``validate_platform_delivery`` runs at the delivery
gate (export time); this module provides the brief-stage early validator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .cross_domain import (
    DOMAIN_COLOR,
    DOMAIN_COMPOSITE,
    DOMAIN_DELIVERY,
    DOMAIN_EDITORIAL,
    DependencyGraph,
)


# --------------------------------------------------------------------------- #
# IX-03: Dirty range + cost prediction
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DirtyRange:
    """The set of clips / frames affected by an upstream change."""

    clip_ids: tuple[str, ...] = ()
    frame_ranges: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    full_recompute: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.clip_ids and not self.full_recompute

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_ids": list(self.clip_ids),
            "frame_ranges": {k: list(v) for k, v in self.frame_ranges.items()},
            "full_recompute": self.full_recompute,
        }


@dataclass(frozen=True)
class DomainRecomputeCost:
    """Estimated cost to recompute one domain for a dirty range."""

    domain: str
    affected_clip_count: int
    estimated_cost_units: float
    preview_resolution_tier: str = "lowres"    # lowres / fullres
    can_skip: bool = False
    skip_reason: str = ""
    #: AUD-13305：``True`` 表示全量重算的 clip 数只能保守估计（下限），
    #: 实际成本可能高于 ``estimated_cost_units``。
    estimate_is_floor: bool = False


@dataclass(frozen=True)
class CrossDomainCostReport:
    """Aggregated recompute cost across all downstream domains (IX-03)."""

    dirty: DirtyRange
    domain_costs: tuple[DomainRecomputeCost, ...] = ()
    total_estimated_cost: float = 0.0
    exceeds_budget: bool = False
    budget_limit: float | None = None
    recommended_tier_downgrade: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dirty": self.dirty.to_dict(),
            "domain_costs": [
                {"domain": c.domain, "affected_clips": c.affected_clip_count,
                 "estimated_cost": c.estimated_cost_units, "can_skip": c.can_skip,
                 "skip_reason": c.skip_reason, "tier": c.preview_resolution_tier,
                 "estimate_is_floor": c.estimate_is_floor}
                for c in self.domain_costs
            ],
            "total_estimated_cost": self.total_estimated_cost,
            "exceeds_budget": self.exceeds_budget,
            "budget_limit": self.budget_limit,
            "recommended_tier_downgrade": self.recommended_tier_downgrade,
        }


def compute_dirty_range(
    graph: DependencyGraph,
    changed_domain: str,
    changed_clip_ids: Iterable[str] = (),
) -> DirtyRange:
    """Compute the downstream dirty range when one domain changes (IX-03).

    If the dependency declares specific ``affected_clip_ids``, only those
    are dirty.  If empty, the whole downstream domain must recompute.
    """

    clips: set[str] = set(changed_clip_ids)
    full = False
    for dep in graph.dependencies:
        if dep.upstream.domain != changed_domain:
            continue
        if dep.affected_clip_ids:
            clips.update(dep.affected_clip_ids)
        else:
            full = True
    return DirtyRange(clip_ids=tuple(sorted(clips)), full_recompute=full)


#: AUD-13305：``full_recompute`` 且图内/调用方都拿不到总 clip 数时的保守
#: 计费下限。全量重算的预估成本至少按此 clip 数计费，保证它不会低于一次
#: 局部（少数 clip）重算的预估——否则预算门禁恰好在最需要控制的场景失效。
_FULL_RECOMPUTE_FALLBACK_CLIP_COUNT: int = 8


def _graph_known_clip_count(
    graph: DependencyGraph,
    changed_clip_ids: Iterable[str],
) -> int:
    """图内可见的 clip 总数（全部依赖声明的受影响 clip 并集 + 本次变更 clip）。"""
    known: set[str] = set(changed_clip_ids)
    for dep in graph.dependencies:
        known.update(dep.affected_clip_ids)
    return len(known)


def predict_recompute_cost(
    graph: DependencyGraph,
    changed_domain: str,
    changed_clip_ids: Iterable[str],
    *,
    cost_per_clip: Mapping[str, float] | None = None,
    budget_limit: float | None = None,
    total_clip_count: int | None = None,
) -> CrossDomainCostReport:
    """Estimate the cost of recomputing downstream domains (IX-03).

    ``cost_per_clip`` maps domain → cost per affected clip.  If the total
    exceeds ``budget_limit``, the report recommends a tier downgrade
    (fullres → lowres) as a cheaper alternative.

    ``total_clip_count`` 是全量重算（依赖声明空 ``affected_clip_ids``）时
    的真实 clip 总数（AUD-13305）。缺失时用图内可见 clip 并集做保守估计，
    并至少按 ``_FULL_RECOMPUTE_FALLBACK_CLIP_COUNT`` 计费；此类估计在报告
    中以 ``estimate_is_floor`` 标注为下限。
    """

    default_costs = {DOMAIN_EDITORIAL: 0.2, DOMAIN_COLOR: 0.5, DOMAIN_COMPOSITE: 1.0, DOMAIN_DELIVERY: 0.3}
    if cost_per_clip:
        default_costs.update(cost_per_clip)

    # Materialize once: the iterable is consumed by both helpers below.
    changed_clips = tuple(changed_clip_ids)
    dirty = compute_dirty_range(graph, changed_domain, changed_clips)
    affected = dirty.clip_ids if not dirty.full_recompute else ("__all__",)
    clip_count = len(affected)
    estimate_is_floor = False
    if dirty.full_recompute:
        # AUD-13305：全量重算不得按 1 个 clip 计费（否则整片 composite 重算
        # 的预估低于重算 3 个 clip，exceeds_budget 几乎必然不触发）。
        # clip 数优先取调用方提供的总数，其次用图内可见 clip 并集，均缺失时
        # 按保守下限计费；除"调用方总数且不少于其他来源"外均标注为下限估计。
        graph_known = _graph_known_clip_count(graph, changed_clips)
        clip_count = max(
            int(total_clip_count or 0),
            graph_known,
            _FULL_RECOMPUTE_FALLBACK_CLIP_COUNT,
        )
        estimate_is_floor = (
            total_clip_count is None or clip_count > int(total_clip_count)
        )

    domain_costs: list[DomainRecomputeCost] = []
    total = 0.0
    for dep in graph.dependencies:
        if dep.upstream.domain != changed_domain:
            continue
        per_clip = default_costs.get(dep.downstream_domain, 0.5)
        count = clip_count
        cost = per_clip * max(1, count)
        total += cost
        can_skip = False
        skip_reason = ""
        if dirty.is_empty:
            can_skip = True
            skip_reason = "无受影响片段"
        domain_costs.append(DomainRecomputeCost(
            domain=dep.downstream_domain,
            affected_clip_count=count,
            estimated_cost_units=cost,
            can_skip=can_skip,
            skip_reason=skip_reason,
            estimate_is_floor=estimate_is_floor,
        ))

    exceeds = budget_limit is not None and total > budget_limit
    return CrossDomainCostReport(
        dirty=dirty,
        domain_costs=tuple(domain_costs),
        total_estimated_cost=total,
        exceeds_budget=exceeds,
        budget_limit=budget_limit,
        recommended_tier_downgrade="lowres" if exceeds else None,
    )


# --------------------------------------------------------------------------- #
# IX-05: Early delivery validation at brief stage
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EarlyDeliveryFinding:
    code: str
    severity: str             # error (blocks brief) / warning
    message: str
    field: str = ""
    fix_suggestion: str = ""


@dataclass(frozen=True)
class EarlyDeliveryReport:
    """Result of validating a brief against its delivery profile (IX-05)."""

    findings: tuple[EarlyDeliveryFinding, ...] = ()
    profile_id: str | None = None

    @property
    def errors(self) -> tuple[EarlyDeliveryFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "is_valid": self.is_valid,
            "error_count": len(self.errors),
            "findings": [
                {"code": f.code, "severity": f.severity, "message": f.message,
                 "field": f.field, "fix_suggestion": f.fix_suggestion}
                for f in self.findings
            ],
        }


def validate_brief_against_delivery(
    *,
    canvas_aspect: str,
    duration_seconds: float,
    platform: str,
    codec: str = "h264",
    transparent: bool = False,
    profile_max_duration: float | None = None,
    profile_supports_transparent: bool = False,
    profile_supports_aspect: bool = True,
    profile_supports_codec: bool = True,
    asset_license_states: Iterable[str] = (),
    profile_id: str | None = None,
) -> EarlyDeliveryReport:
    """Validate a brief against delivery constraints *before* planning (IX-05).

    Catches: incompatible aspect, duration exceeding platform cap, transparent
    request on non-transparent profile, unsupported codec, unlicensed assets.
    Each error is actionable with a fix_suggestion so the user can fix the
    brief before any generation cost is incurred.
    """

    findings: list[EarlyDeliveryFinding] = []

    if not profile_supports_aspect:
        findings.append(EarlyDeliveryFinding(
            code="ASPECT_UNSUPPORTED",
            severity="error",
            field="canvas_aspect",
            message=f"交付规格不支持画幅 {canvas_aspect!r}。",
            fix_suggestion="更改画幅或选择兼容的交付 profile。",
        ))

    if profile_max_duration is not None and duration_seconds > profile_max_duration:
        findings.append(EarlyDeliveryFinding(
            code="DURATION_EXCEEDS_PLATFORM",
            severity="error",
            field="duration_seconds",
            message=f"时长 {duration_seconds}s 超过平台上限 {profile_max_duration}s。",
            fix_suggestion=f"缩短到 {profile_max_duration}s 以内或选择其他平台。",
        ))

    if transparent and not profile_supports_transparent:
        findings.append(EarlyDeliveryFinding(
            code="TRANSPARENCY_UNSUPPORTED",
            severity="error",
            field="transparent",
            message="该交付规格不支持透明通道。",
            fix_suggestion="关闭透明需求或选择支持透明的 profile。",
        ))

    if not profile_supports_codec:
        findings.append(EarlyDeliveryFinding(
            code="CODEC_UNSUPPORTED",
            severity="error",
            field="codec",
            message=f"交付规格不支持编码 {codec!r}。",
            fix_suggestion="更换编码或选择兼容的 profile。",
        ))

    licenses = list(asset_license_states)
    if any(state in {"missing", "restricted"} for state in licenses):
        findings.append(EarlyDeliveryFinding(
            code="ASSET_LICENSE_MISSING",
            severity="error",
            message="存在缺少许可的素材，不能用于商业导出。",
            fix_suggestion="替换为已授权素材或获取许可。",
        ))

    if duration_seconds < 3.0:
        findings.append(EarlyDeliveryFinding(
            code="DURATION_TOO_SHORT",
            severity="warning",
            field="duration_seconds",
            message="时长短于 3 秒，多数平台可能不予推荐。",
            fix_suggestion="考虑延长到至少 3 秒。",
        ))

    return EarlyDeliveryReport(
        findings=tuple(findings),
        profile_id=profile_id,
    )


__all__ = [
    "DirtyRange",
    "DomainRecomputeCost",
    "CrossDomainCostReport",
    "EarlyDeliveryFinding",
    "EarlyDeliveryReport",
    "compute_dirty_range",
    "predict_recompute_cost",
    "validate_brief_against_delivery",
]
