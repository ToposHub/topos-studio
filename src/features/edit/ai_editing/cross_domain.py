"""Cross-domain orchestration: revision chain, render contract, dirty range, scorecard (IX-01~IX-06).

The audit found that revision chains, render contracts and cost controls are
all single-domain — editorial, color and composite each maintain their own
revision / hash without referencing each other.  This module defines the
**cross-domain glue** that lets a change in one domain correctly invalidate
downstream domains.

All types are additive: they compose with the existing per-domain plan /
revision / snapshot types without modifying them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# Domain identifiers
# --------------------------------------------------------------------------- #

DOMAIN_EDITORIAL = "editorial"
DOMAIN_COLOR = "color"
DOMAIN_COMPOSITE = "composite"
DOMAIN_DELIVERY = "delivery"

ALL_DOMAINS: frozenset[str] = frozenset({
    DOMAIN_EDITORIAL, DOMAIN_COLOR, DOMAIN_COMPOSITE, DOMAIN_DELIVERY,
})


# --------------------------------------------------------------------------- #
# Cross-domain revision reference (IX-01)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DomainArtifactRef:
    """A typed reference to one domain's plan / artifact.

    Carries the domain, the artifact id, the revision / hash the plan was
    built against, so downstream domains can detect when their upstream
    changed.
    """

    domain: str
    artifact_id: str           # plan_id / grade_plan_id / comp_plan_id
    revision: int
    content_hash: str

    def __post_init__(self) -> None:
        if self.domain not in ALL_DOMAINS:
            raise ValueError(f"Unknown domain: {self.domain!r}")
        if not self.artifact_id:
            raise ValueError("DomainArtifactRef.artifact_id is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class CrossDomainDependency:
    """One upstream → downstream dependency edge (IX-01).

    When the upstream changes (different revision or hash), the downstream
    plan is stale and must be recomputed for the affected range only.
    """

    downstream_domain: str
    upstream: DomainArtifactRef
    affected_clip_ids: tuple[str, ...] = ()    # empty = full recompute

    def is_stale(self, observed: DomainArtifactRef) -> bool:
        if observed.domain != self.upstream.domain:
            return False
        if observed.artifact_id != self.upstream.artifact_id:
            return True
        if observed.revision != self.upstream.revision:
            return True
        if observed.content_hash != self.upstream.content_hash:
            return True
        return False


def _group_observed_by_domain(
    observed_upstreams: Iterable[DomainArtifactRef],
) -> dict[str, tuple[DomainArtifactRef, ...]]:
    """按域分组保留**全部**观察到的 ref（AUD-13302，不再同域 last-wins）。"""
    grouped: dict[str, list[DomainArtifactRef]] = {}
    for ref in observed_upstreams:
        grouped.setdefault(ref.domain, []).append(ref)
    return {domain: tuple(refs) for domain, refs in grouped.items()}


def _dep_is_stale(
    dep: CrossDomainDependency,
    observed_by_domain: Mapping[str, tuple[DomainArtifactRef, ...]],
) -> bool:
    """确定性的 stale 判定，结果与观察顺序无关（AUD-13302）。

    匹配规则：同域内按 ``(domain, artifact_id)`` 精确对齐依赖声明的上游；
    同名 artifact 的 revision/content_hash 任一变化即 stale；上游 artifact
    在同域已观察不到（被同域其他 artifact 取代）时同样判 stale；整域未
    观察到时不判 stale（无法与"上游未变更"区分）。
    """
    candidates = observed_by_domain.get(dep.upstream.domain, ())
    if not candidates:
        return False
    same_artifact = tuple(
        ref for ref in candidates if ref.artifact_id == dep.upstream.artifact_id
    )
    if not same_artifact:
        return True
    return any(dep.is_stale(ref) for ref in same_artifact)


@dataclass(frozen=True)
class DependencyGraph:
    """Collection of cross-domain dependencies + stale detection (IX-01/IX-03)."""

    dependencies: tuple[CrossDomainDependency, ...] = ()

    def stale_downstream(
        self,
        observed_upstreams: Iterable[DomainArtifactRef],
    ) -> tuple[str, ...]:
        """Return downstream domain names whose upstream changed."""
        observed_by_domain = _group_observed_by_domain(observed_upstreams)
        stale: set[str] = set()
        for dep in self.dependencies:
            if _dep_is_stale(dep, observed_by_domain):
                stale.add(dep.downstream_domain)
        return tuple(sorted(stale))

    def dirty_clip_ids(
        self,
        observed_upstreams: Iterable[DomainArtifactRef],
    ) -> tuple[str, ...]:
        """Return the union of affected clip ids from stale upstreams."""
        observed_by_domain = _group_observed_by_domain(observed_upstreams)
        dirty: set[str] = set()
        for dep in self.dependencies:
            if _dep_is_stale(dep, observed_by_domain):
                dirty.update(dep.affected_clip_ids)
        return tuple(sorted(dirty))


# --------------------------------------------------------------------------- #
# Shared render contract (IX-02)
# --------------------------------------------------------------------------- #


#: ``RenderContract.is_consistent_with`` 可比较/可豁免的字段集合（AUD-13303）。
_CONSISTENCY_FIELDS: frozenset[str] = frozenset({
    "width", "height", "fps", "color_pipeline", "transfer_function",
    "alpha_mode", "proxy_tier", "time_range", "effects_enabled",
})


@dataclass(frozen=True)
class RenderContract:
    """The single definition of how a frame / clip must be rendered (IX-02).

    Every preview and export across every domain must declare which contract
    it used; a mismatch between preview and export is a consistency bug.
    """

    width: int
    height: int
    fps: float
    color_pipeline: str = "rec709"
    transfer_function: str = "sdr"
    alpha_mode: str = "premultiplied"     # premultiplied / straight / none
    proxy_tier: str = "full"               # proxy / full
    time_range: Mapping[str, Any] = field(default_factory=dict)
    effects_enabled: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("RenderContract dimensions must be positive")
        if self.fps <= 0:
            raise ValueError("fps must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "color_pipeline": self.color_pipeline,
            "transfer_function": self.transfer_function,
            "alpha_mode": self.alpha_mode,
            "proxy_tier": self.proxy_tier,
            "time_range": dict(self.time_range),
            "effects_enabled": list(self.effects_enabled),
        }

    def hash(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_consistent_with(
        self,
        other: "RenderContract",
        *,
        tolerance: float = 0.001,
        ignore: Iterable[str] = (),
    ) -> bool:
        """Return whether two contracts would produce visually consistent output.

        默认比较**全部**渲染语义字段（AUD-13303）：预览关特效、导出开特效
        （``effects_enabled`` 不同）、代理档位（``proxy_tier``）或时间段
        （``time_range``）不同都判不一致——预览与导出之间的任何语义偏差都
        是一致性缺陷（IX-02）。若调用方确要豁免个别字段（例如只比对代理
        预览的分辨率/色彩管线），用 ``ignore`` 显式声明豁免字段名，而不是
        静默忽略；未知字段名抛 ``ValueError`` 以便定位配置错误。
        """
        exemptions = frozenset(ignore)
        unknown = exemptions - _CONSISTENCY_FIELDS
        if unknown:
            raise ValueError(
                f"unknown is_consistent_with exemption(s): {sorted(unknown)}; "
                f"valid fields: {sorted(_CONSISTENCY_FIELDS)}"
            )

        def exempt(name: str) -> bool:
            return name in exemptions

        return (
            (exempt("width") or self.width == other.width)
            and (exempt("height") or self.height == other.height)
            and (exempt("fps") or abs(self.fps - other.fps) < tolerance)
            and self.color_pipeline == other.color_pipeline
            and self.transfer_function == other.transfer_function
            and self.alpha_mode == other.alpha_mode
            and (exempt("proxy_tier") or self.proxy_tier == other.proxy_tier)
            and (exempt("effects_enabled")
                 or frozenset(self.effects_enabled) == frozenset(other.effects_enabled))
            and (exempt("time_range")
                 or dict(self.time_range) == dict(other.time_range))
        )


# --------------------------------------------------------------------------- #
# Protected regions (IX-04)
# --------------------------------------------------------------------------- #


REGION_CLIP = "clip"
REGION_GRADE_NODE = "grade_node"
REGION_COMP_GROUP = "comp_group"
REGION_SUBTITLE_TRACK = "subtitle_track"
REGION_BRAND_LAYER = "brand_layer"

SUPPORTED_REGION_KINDS: frozenset[str] = frozenset({
    REGION_CLIP, REGION_GRADE_NODE, REGION_COMP_GROUP,
    REGION_SUBTITLE_TRACK, REGION_BRAND_LAYER,
})


@dataclass(frozen=True)
class ProtectedRegion:
    """A user-locked region that planners must not overwrite (IX-04)."""

    region_id: str
    kind: str
    target_ref: str               # clip_id / node_id / group_id / track_id
    domain: str
    locked_by_user: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_REGION_KINDS:
            raise ValueError(f"Unknown region kind: {self.kind!r}")
        if self.domain not in ALL_DOMAINS:
            raise ValueError(f"Unknown domain: {self.domain!r}")


class ProtectedRegionRegistry:
    """Append-only registry of user-locked regions across all domains."""

    def __init__(self) -> None:
        self._regions: dict[str, ProtectedRegion] = {}

    def lock(self, region: ProtectedRegion) -> None:
        self._regions[region.region_id] = region

    def unlock(self, region_id: str) -> bool:
        return self._regions.pop(region_id, None) is not None

    def is_protected(self, domain: str, target_ref: str) -> bool:
        return any(
            r.domain == domain and r.target_ref == target_ref
            for r in self._regions.values()
        )

    def conflicts_for_domain(self, domain: str, target_refs: Iterable[str]) -> tuple[ProtectedRegion, ...]:
        ref_set = set(target_refs)
        return tuple(
            r for r in self._regions.values()
            if r.domain == domain and r.target_ref in ref_set
        )

    def all_regions(self) -> tuple[ProtectedRegion, ...]:
        return tuple(self._regions.values())


# --------------------------------------------------------------------------- #
# Cross-domain scorecard (IX-06)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DomainScore:
    """One domain's quality score for the final mission scorecard."""

    domain: str
    score: float                 # 0..1
    threshold_met: bool
    evidence_refs: tuple[str, ...] = ()
    open_risks: tuple[str, ...] = ()
    human_override: float | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.domain not in ALL_DOMAINS:
            raise ValueError(f"Unknown domain: {self.domain!r}")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be in [0, 1]")


@dataclass(frozen=True)
class CrossDomainScorecard:
    """Aggregated quality assessment across all domains (IX-06).

    The score is advisory — it can never override a quality red line.
    :attr:`blocking_issues` is the union of unmet thresholds; any blocking
    issue means the mission is not deliverable regardless of the score.
    """

    scores: tuple[DomainScore, ...] = ()
    weights: Mapping[str, float] = field(default_factory=dict)

    @property
    def weighted_score(self) -> float:
        if not self.scores:
            return 0.0
        total_weight = 0.0
        weighted = 0.0
        for s in self.scores:
            w = self.weights.get(s.domain, 1.0)
            value = s.human_override if s.human_override is not None else s.score
            weighted += value * w
            total_weight += w
        return weighted / total_weight if total_weight > 0 else 0.0

    @property
    def blocking_issues(self) -> tuple[DomainScore, ...]:
        return tuple(s for s in self.scores if not s.threshold_met)

    @property
    def is_deliverable(self) -> bool:
        """Scorecard is advisory; deliverability requires all thresholds met."""
        return not self.blocking_issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "weighted_score": self.weighted_score,
            "is_deliverable": self.is_deliverable,
            "blocking_count": len(self.blocking_issues),
            "scores": [
                {"domain": s.domain, "score": s.score, "threshold_met": s.threshold_met,
                 "open_risks": list(s.open_risks), "human_override": s.human_override}
                for s in self.scores
            ],
            "weights": dict(self.weights),
        }


__all__ = [
    "DomainArtifactRef",
    "CrossDomainDependency",
    "DependencyGraph",
    "RenderContract",
    "ProtectedRegion",
    "ProtectedRegionRegistry",
    "DomainScore",
    "CrossDomainScorecard",
    "ALL_DOMAINS",
    "SUPPORTED_REGION_KINDS",
    "DOMAIN_EDITORIAL",
    "DOMAIN_COLOR",
    "DOMAIN_COMPOSITE",
    "DOMAIN_DELIVERY",
    "REGION_CLIP",
    "REGION_GRADE_NODE",
    "REGION_COMP_GROUP",
    "REGION_SUBTITLE_TRACK",
    "REGION_BRAND_LAYER",
]
