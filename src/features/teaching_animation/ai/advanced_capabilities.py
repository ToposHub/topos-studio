# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Advanced Motion Graphic capability registry (MG-28).

MG-28 is the long-tail of advanced creative features: multi-scene narrative,
shot templates, chart / code highlighting, character / lip-sync, collaborative
review, cross-language versions, accessibility output, richer render backends.
None of these belong in the B0–B4 critical path, but each one *will* be
requested eventually — and when it is, it must declare its schema, preview /
export compatibility, quality gate, fallback and rollback so it cannot
silently break the safe-by-default pipeline.

This module is the **capability declaration framework**: every advanced
feature registers a :class:`AdvancedCapability` with explicit compatibility
and fallback metadata.  The planner / quality gate / export job query the
registry to refuse "this feature cannot preview on this profile" up front
rather than failing mid-render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


# --------------------------------------------------------------------------- #
# Capability ids (closed vocabulary — adding one is a deliberate act)
# --------------------------------------------------------------------------- #


CAP_MULTI_SCENE_NARRATIVE = "multi_scene_narrative"
CAP_SHOT_TEMPLATES = "shot_templates"
CAP_CHART_HIGHLIGHT = "chart_highlight"
CAP_CODE_HIGHLIGHT = "code_highlight"
CAP_CHARACTER_LIPSYNC = "character_lipsync"
CAP_COLLABORATIVE_REVIEW = "collaborative_review"
CAP_CROSS_LANGUAGE = "cross_language"
CAP_ACCESSIBILITY_OUTPUT = "accessibility_output"
CAP_3D_CAMERA = "camera_3d"
CAP_PARTICLES = "particles"
CAP_EXPRESSIONS = "expressions"

ADVANCED_CAPABILITIES: frozenset[str] = frozenset({
    CAP_MULTI_SCENE_NARRATIVE,
    CAP_SHOT_TEMPLATES,
    CAP_CHART_HIGHLIGHT,
    CAP_CODE_HIGHLIGHT,
    CAP_CHARACTER_LIPSYNC,
    CAP_COLLABORATIVE_REVIEW,
    CAP_CROSS_LANGUAGE,
    CAP_ACCESSIBILITY_OUTPUT,
    CAP_3D_CAMERA,
    CAP_PARTICLES,
    CAP_EXPRESSIONS,
})


# --------------------------------------------------------------------------- #
# Compatibility + fallback declarations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CapabilityCompatibility:
    """What a capability can / cannot do across preview and export."""

    preview_backends: tuple[str, ...] = ("svg",)
    export_backends: tuple[str, ...] = ("svg", "html")
    requires_gpu: bool = False
    requires_network: bool = False
    min_delivery_profile: str | None = None
    incompatible_codecs: tuple[str, ...] = ()
    incompatible_aspects: tuple[str, ...] = ()

    def supports_codec(self, codec: str) -> bool:
        return codec not in self.incompatible_codecs

    def supports_aspect(self, aspect: str) -> bool:
        return aspect not in self.incompatible_aspects

    def to_dict(self) -> dict[str, Any]:
        return {
            "preview_backends": list(self.preview_backends),
            "export_backends": list(self.export_backends),
            "requires_gpu": self.requires_gpu,
            "requires_network": self.requires_network,
            "min_delivery_profile": self.min_delivery_profile,
            "incompatible_codecs": list(self.incompatible_codecs),
            "incompatible_aspects": list(self.incompatible_aspects),
        }


class FallbackStrategy(str):
    """What happens when a capability cannot run."""


FALLBACK_DISABLE = "disable"                # drop the feature, keep rendering
FALLBACK_SIMPLIFY = "simplify"               # render a simpler version
FALLBACK_TEMPLATE = "template"               # use a built-in template look
FALLBACK_REQUIRE_USER_DECISION = "require_user_decision"

ALL_FALLBACK_STRATEGIES: frozenset[str] = frozenset({
    FALLBACK_DISABLE, FALLBACK_SIMPLIFY,
    FALLBACK_TEMPLATE, FALLBACK_REQUIRE_USER_DECISION,
})


# --------------------------------------------------------------------------- #
# Capability record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AdvancedCapability:
    """One declared advanced feature (MG-28).

    Attributes:
        capability_id: One of :data:`ADVANCED_CAPABILITIES`.
        display_name: User-facing label.
        description: Short explanation.
        schema_version: Schema version of the feature payload.
        input_schema: Mapping describing the feature's input fields (kept
            loose so each feature evolves independently).
        compatibility: Preview / export / codec / aspect compatibility.
        fallback_strategy: What to do when the capability cannot run.
        quality_checks: Extra quality-check ids the gate should run when this
            capability is present.
        stability_tier: experimental / beta / ga — drives rollout defaults.
        metadata: Extra hints (link to docs, owner team, etc.).
    """

    capability_id: str
    display_name: str
    description: str
    schema_version: int = 1
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    compatibility: CapabilityCompatibility = field(default_factory=CapabilityCompatibility)
    fallback_strategy: str = FALLBACK_DISABLE
    quality_checks: tuple[str, ...] = ()
    stability_tier: str = "experimental"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.capability_id not in ADVANCED_CAPABILITIES:
            raise ValueError(
                f"Unknown advanced capability: {self.capability_id!r}; "
                f"register it first if you intend to add a new one."
            )
        if self.fallback_strategy not in ALL_FALLBACK_STRATEGIES:
            raise ValueError(
                f"Unknown fallback strategy: {self.fallback_strategy!r}"
            )
        if self.stability_tier not in {"experimental", "beta", "ga"}:
            raise ValueError(f"Unknown stability tier: {self.stability_tier!r}")
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "display_name": self.display_name,
            "description": self.description,
            "schema_version": self.schema_version,
            "input_schema": dict(self.input_schema),
            "compatibility": self.compatibility.to_dict(),
            "fallback_strategy": self.fallback_strategy,
            "quality_checks": list(self.quality_checks),
            "stability_tier": self.stability_tier,
            "metadata": dict(self.metadata),
        }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class AdvancedCapabilityRegistry:
    """Append-only registry of declared advanced capabilities.

    The registry is the single place the planner / gate / export job asks
    "is this capability available, compatible and what do we do if it fails?".
    Capabilities not registered here are refused by the planner, so an
    unannounced feature can never silently change behaviour.
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, AdvancedCapability] = {}

    def register(self, capability: AdvancedCapability, *, replace: bool = False) -> bool:
        if capability.capability_id in self._capabilities and not replace:
            return False
        self._capabilities[capability.capability_id] = capability
        return True

    def get(self, capability_id: str) -> AdvancedCapability | None:
        return self._capabilities.get(capability_id)

    def require(self, capability_id: str) -> AdvancedCapability:
        capability = self.get(capability_id)
        if capability is None:
            raise KeyError(f"Unregistered advanced capability: {capability_id!r}")
        return capability

    def all_capabilities(self) -> tuple[AdvancedCapability, ...]:
        return tuple(self._capabilities[key] for key in sorted(self._capabilities))

    def stable_capabilities(self) -> tuple[AdvancedCapability, ...]:
        return tuple(
            cap for cap in self._capabilities.values()
            if cap.stability_tier in {"beta", "ga"}
        )

    def compatibility_issues(
        self,
        capability: AdvancedCapability,
        *,
        codec: str,
        aspect: str,
        preview_backend: str,
        has_gpu: bool,
    ) -> tuple[str, ...]:
        issues: list[str] = []
        compat = capability.compatibility
        if preview_backend not in compat.preview_backends:
            issues.append(f"预览后端 {preview_backend!r} 不支持该能力")
        if codec not in compat.export_backends or not compat.supports_codec(codec):
            issues.append(f"导出编码 {codec!r} 不支持该能力")
        if not compat.supports_aspect(aspect):
            issues.append(f"画幅 {aspect!r} 不支持该能力")
        if compat.requires_gpu and not has_gpu:
            issues.append("该能力需要 GPU，但当前环境不可用")
        return tuple(issues)


# --------------------------------------------------------------------------- #
# Seeded catalog (experimental defaults; override per release)
# --------------------------------------------------------------------------- #


def create_default_advanced_registry() -> AdvancedCapabilityRegistry:
    """Seed the registry with MG-28 candidate capabilities (all experimental)."""
    registry = AdvancedCapabilityRegistry()

    registry.register(AdvancedCapability(
        capability_id=CAP_MULTI_SCENE_NARRATIVE,
        display_name="多场景叙事",
        description="把一个片段拆成多场景叙事段落。",
        compatibility=CapabilityCompatibility(
            preview_backends=("svg", "html"),
            export_backends=("svg", "html", "mp4"),
        ),
        fallback_strategy=FALLBACK_SIMPLIFY,
        quality_checks=("scene_transitions",),
        stability_tier="experimental",
    ))
    registry.register(AdvancedCapability(
        capability_id=CAP_CHART_HIGHLIGHT,
        display_name="图表高亮",
        description="在数据图表中按节奏高亮关键数据点。",
        compatibility=CapabilityCompatibility(
            preview_backends=("svg", "html", "moderngl"),
            export_backends=("svg", "html", "mp4"),
        ),
        fallback_strategy=FALLBACK_DISABLE,
        quality_checks=("chart_readable",),
        stability_tier="beta",
    ))
    registry.register(AdvancedCapability(
        capability_id=CAP_CODE_HIGHLIGHT,
        display_name="代码高亮",
        description="把代码片段按语法高亮并按行揭示。",
        compatibility=CapabilityCompatibility(
            preview_backends=("svg", "html"),
            export_backends=("svg", "html", "mp4"),
        ),
        fallback_strategy=FALLBACK_DISABLE,
        quality_checks=("code_overflow",),
        stability_tier="beta",
    ))
    registry.register(AdvancedCapability(
        capability_id=CAP_CHARACTER_LIPSYNC,
        display_name="角色口型同步",
        description="根据配音驱动角色口型动画。",
        compatibility=CapabilityCompatibility(
            preview_backends=("moderngl",),
            export_backends=("mp4",),
            requires_gpu=True,
            requires_network=True,
        ),
        fallback_strategy=FALLBACK_REQUIRE_USER_DECISION,
        quality_checks=("lip_sync_drift",),
        stability_tier="experimental",
    ))
    registry.register(AdvancedCapability(
        capability_id=CAP_CROSS_LANGUAGE,
        display_name="多语言版本",
        description="从同一方案派生多语言版本。",
        compatibility=CapabilityCompatibility(
            preview_backends=("svg", "html"),
            export_backends=("svg", "html", "mp4"),
            requires_network=True,
        ),
        fallback_strategy=FALLBACK_DISABLE,
        stability_tier="beta",
    ))
    registry.register(AdvancedCapability(
        capability_id=CAP_ACCESSIBILITY_OUTPUT,
        display_name="无障碍输出",
        description="为片段生成音频描述 / 字幕 / 高对比度版本。",
        compatibility=CapabilityCompatibility(
            preview_backends=("svg", "html"),
            export_backends=("svg", "html", "mp4"),
        ),
        fallback_strategy=FALLBACK_DISABLE,
        quality_checks=("accessibility_coverage",),
        stability_tier="ga",
    ))
    registry.register(AdvancedCapability(
        capability_id=CAP_3D_CAMERA,
        display_name="3D 摄像机",
        description="在 2D 场景中模拟 3D 摄像机运动。",
        compatibility=CapabilityCompatibility(
            preview_backends=("moderngl",),
            export_backends=("mp4",),
            requires_gpu=True,
            incompatible_aspects=("1:1",),
        ),
        fallback_strategy=FALLBACK_SIMPLIFY,
        stability_tier="experimental",
    ))
    registry.register(AdvancedCapability(
        capability_id=CAP_PARTICLES,
        display_name="粒子特效",
        description="确定性的粒子系统特效层；无 GPU 时使用 SVG 简化降级。",
        compatibility=CapabilityCompatibility(
            preview_backends=("svg", "moderngl"),
            export_backends=("svg", "mp4"),
            requires_gpu=False,
        ),
        fallback_strategy=FALLBACK_SIMPLIFY,
        quality_checks=("particle_bounds", "particle_determinism"),
        stability_tier="beta",
    ))
    registry.register(AdvancedCapability(
        capability_id=CAP_EXPRESSIONS,
        display_name="受控场景表达式",
        description="使用受限纯函数表达式驱动元素属性；预览、导出和 Composite 使用同一帧求值语义。",
        compatibility=CapabilityCompatibility(
            preview_backends=("svg", "moderngl"),
            export_backends=("svg", "mp4"),
            requires_gpu=False,
        ),
        fallback_strategy=FALLBACK_DISABLE,
        quality_checks=("expression_sandbox", "expression_determinism", "expression_budget"),
        stability_tier="beta",
        metadata={
            "source_of_truth": "SceneExpression",
            "cross_process_golden": True,
            "html_layer_expression_support": False,
        },
    ))

    return registry


__all__ = [
    "AdvancedCapability",
    "AdvancedCapabilityRegistry",
    "CapabilityCompatibility",
    "FallbackStrategy",
    "ADVANCED_CAPABILITIES",
    "ALL_FALLBACK_STRATEGIES",
    "FALLBACK_DISABLE",
    "FALLBACK_SIMPLIFY",
    "FALLBACK_TEMPLATE",
    "FALLBACK_REQUIRE_USER_DECISION",
    "CAP_MULTI_SCENE_NARRATIVE",
    "CAP_SHOT_TEMPLATES",
    "CAP_CHART_HIGHLIGHT",
    "CAP_CODE_HIGHLIGHT",
    "CAP_CHARACTER_LIPSYNC",
    "CAP_COLLABORATIVE_REVIEW",
    "CAP_CROSS_LANGUAGE",
    "CAP_ACCESSIBILITY_OUTPUT",
    "CAP_3D_CAMERA",
    "CAP_PARTICLES",
    "CAP_EXPRESSIONS",
    "create_default_advanced_registry",
]
