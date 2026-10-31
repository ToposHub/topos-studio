"""Professional skill pack definitions (§5.2 / OS-11).

The audit found that the existing skill framework (``skill_manifest.py`` /
``skill_registry.py``) is complete but only has 3 official skills.  The plan
§5.2 recommends 8 first-wave professional skills.  This module defines their
typed declarations so they can be registered and used.

Each skill is a "controlled workflow", not a prompt: it declares input form,
available tools, default permissions, preview artifacts, quality gates,
failure recovery, budget and acceptance criteria.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ProfessionalSkill:
    """One professional skill definition (§5.2)."""

    skill_id: str
    label: str
    description: str
    domain: str
    default_auto_scope: tuple[str, ...]
    requires_confirmation: tuple[str, ...]
    input_fields: tuple[Mapping[str, Any], ...] = ()
    quality_gates: tuple[str, ...] = ()
    fallback_strategy: str = "disable"
    budget_estimate: float = 5.0
    acceptance_criteria: tuple[str, ...] = ()
    stability_tier: str = "beta"

    def __post_init__(self) -> None:
        if not self.skill_id:
            raise ValueError("skill_id is required")
        if self.domain not in {"editorial", "color", "composite", "cross_domain"}:
            raise ValueError(f"Unknown domain: {self.domain!r}")
        if self.stability_tier not in {"experimental", "beta", "ga"}:
            raise ValueError(f"Unknown stability tier: {self.stability_tier!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "label": self.label,
            "description": self.description,
            "domain": self.domain,
            "default_auto_scope": list(self.default_auto_scope),
            "requires_confirmation": list(self.requires_confirmation),
            "input_fields": [dict(f) for f in self.input_fields],
            "quality_gates": list(self.quality_gates),
            "fallback_strategy": self.fallback_strategy,
            "budget_estimate": self.budget_estimate,
            "acceptance_criteria": list(self.acceptance_criteria),
            "stability_tier": self.stability_tier,
        }

    def to_manifest(self) -> Any:
        """Convert to a ``SkillManifest`` for registration in ``SkillRegistry``.

        Maps the professional-skill fields onto the richer manifest schema.
        The resulting manifest has ``source=LOCAL_DEV`` (unverified) unless
        ``stability_tier`` is ``"ga"``.
        """
        from src.features.edit.ai_editing.skill_manifest import (
            SkillManifest,
            SkillRisk,
            SkillSource,
        )

        # Determine risk level from auto-scope and confirmation requirements.
        write_actions = {"insert_timeline_clip", "replace_clip", "final_export",
                         "paid_generation", "apply_grade", "apply_comp"}
        if any(a in self.requires_confirmation for a in write_actions):
            risk = SkillRisk.WRITE_REQUIRES_APPROVAL
        elif not self.requires_confirmation and all(
            "analyze" in s or "propose" in s or "preview" in s or "plan" in s
            for s in self.default_auto_scope
        ):
            risk = SkillRisk.READ_ONLY
        else:
            risk = SkillRisk.PROPOSAL_ONLY

        source = (
            SkillSource.OFFICIAL if self.stability_tier == "ga"
            else SkillSource.LOCAL_DEV
        )

        # Build JSON-schema fragment from input_fields.
        properties = {}
        required: list[str] = []
        for f in self.input_fields:
            name = str(f.get("name", ""))
            if name:
                properties[name] = {"type": str(f.get("type", "string"))}
                if f.get("required"):
                    required.append(name)

        return SkillManifest(
            skill_id=self.skill_id,
            version="1.0.0",
            title=self.label,
            description=self.description,
            author="Topos Color",
            source=source,
            risk=risk,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
            } if properties else {},
            degrade_to=self.fallback_strategy,
            ui_card={
                "domain": self.domain,
                "quality_gates": list(self.quality_gates),
                "acceptance_criteria": list(self.acceptance_criteria),
                "budget_estimate": self.budget_estimate,
                "stability_tier": self.stability_tier,
            },
        )


SKILL_INTERVIEW_HIGHLIGHTS = ProfessionalSkill(
    skill_id="topos.interview_highlights",
    label="访谈精剪",
    description="把访谈剪成 30–90 秒重点片段，保留核心观点和字幕。",
    domain="editorial",
    default_auto_scope=("analyze_assets", "propose_brief", "plan_creative", "render_lowres_preview"),
    requires_confirmation=("insert_timeline_clip", "paid_generation", "final_export"),
    input_fields=(
        {"name": "source_asset_ids", "type": "array", "required": True},
        {"name": "target_duration", "type": "number", "required": True},
        {"name": "key_topics", "type": "array"},
    ),
    quality_gates=("transcript_coverage", "jump_cut_density", "subtitle_readability", "loudness"),
    acceptance_criteria=(
        "核心观点覆盖率 >= 80%",
        "字幕阅读速度 <= 20 字/秒",
        "响度符合平台 profile",
        "跳剪密度 <= 0.3",
    ),
)

SKILL_PRODUCT_AD = ProfessionalSkill(
    skill_id="topos.product_ad",
    label="产品广告",
    description="用产品素材制作 15/30 秒广告和多平台版本。",
    domain="editorial",
    default_auto_scope=("analyze_assets", "propose_brief", "plan_creative",
                        "render_lowres_preview", "render_storyboard"),
    requires_confirmation=("insert_timeline_clip", "paid_generation", "final_export"),
    input_fields=(
        {"name": "product_asset_ids", "type": "array", "required": True},
        {"name": "duration", "type": "number", "required": True},
        {"name": "platforms", "type": "array"},
        {"name": "cta_text", "type": "string"},
    ),
    quality_gates=("cta_coverage", "brand_color", "safe_area", "platform_spec"),
    acceptance_criteria=(
        "卖点 + CTA 覆盖完整",
        "品牌色在容差范围内",
        "safe area 内无越界元素",
        "各平台规格达标",
    ),
)

SKILL_PODCAST_SOCIAL_CUT = ProfessionalSkill(
    skill_id="topos.podcast_social_cut",
    label="播客社媒切片",
    description="从播客创建横竖屏社媒切片。",
    domain="editorial",
    default_auto_scope=("analyze_assets", "propose_brief", "plan_creative", "render_lowres_preview"),
    requires_confirmation=("insert_timeline_clip", "paid_generation", "final_export"),
    input_fields=(
        {"name": "source_asset_ids", "type": "array", "required": True},
        {"name": "clip_duration", "type": "number"},
        {"name": "target_aspects", "type": "array"},
    ),
    quality_gates=("argument_complete", "speaker_correct", "reframe_safe", "subtitle_loudness"),
    acceptance_criteria=(
        "论点完整（不截断句子）",
        "说话人标注正确",
        "竖屏 reframe 主体在安全区内",
        "字幕 + 响度合格",
    ),
)

SKILL_COURSE_CLEANUP = ProfessionalSkill(
    skill_id="topos.course_cleanup",
    label="教程清理",
    description="教程去停顿、章节化、重点字幕与 Motion Graphic 提示。",
    domain="cross_domain",
    default_auto_scope=("analyze_assets", "propose_brief", "detect_silence",
                        "chapter_detection", "render_lowres_preview"),
    requires_confirmation=("delete_segments", "reorder_segments", "insert_motion_graphic", "final_export"),
    input_fields=(
        {"name": "source_asset_ids", "type": "array", "required": True},
        {"name": "silence_threshold_db", "type": "number"},
    ),
    quality_gates=("semantic_continuity", "chapter_coverage", "undo_safety"),
    acceptance_criteria=(
        "讲解语义连续（不因删静音打断句子）",
        "章节覆盖全部内容",
        "所有删除可撤销",
    ),
)

SKILL_CAMERA_MATCH_GRADE = ProfessionalSkill(
    skill_id="topos.camera_match_grade",
    label="多机位基础匹配",
    description="多机位基础匹配与中性校正。",
    domain="color",
    default_auto_scope=("analyze_color", "propose_grade_plan", "render_preview_stills"),
    requires_confirmation=("apply_grade_plan", "lut_import", "final_export"),
    input_fields=(
        {"name": "clip_ids", "type": "array", "required": True},
        {"name": "reference_clip_id", "type": "string"},
    ),
    quality_gates=("color_metadata", "skin_tone", "clipping", "hdr_gamut"),
    acceptance_criteria=(
        "匹配后色温差异 <= 200K",
        "肤色在 vectorscope 肤色线 ±15° 内",
        "无新增 clipping",
        "HDR 高光未损坏",
    ),
    fallback_strategy="simplify",
)

SKILL_BRAND_LOOK = ProfessionalSkill(
    skill_id="topos.brand_look",
    label="品牌 Look",
    description="套用品牌 Look，同时保护产品色与肤色。",
    domain="color",
    default_auto_scope=("analyze_color", "propose_grade_plan", "render_preview_stills"),
    requires_confirmation=("apply_grade_plan", "final_export"),
    input_fields=(
        {"name": "clip_ids", "type": "array", "required": True},
        {"name": "brand_profile_id", "type": "string", "required": True},
    ),
    quality_gates=("brand_color_tolerance", "skin_protection", "logo_visibility", "preview_export_consistency"),
    acceptance_criteria=(
        "品牌色 DeltaE <= 容差",
        "肤色未偏移",
        "Logo 可见",
        "preview 与 export 色彩一致",
    ),
)

SKILL_GREEN_SCREEN_COMP = ProfessionalSkill(
    skill_id="topos.green_screen_comp",
    label="绿幕合成",
    description="绿幕抠像、溢色处理、背景替换和整合。",
    domain="composite",
    default_auto_scope=("analyze_composability", "propose_comp_plan", "render_preview_proxy"),
    requires_confirmation=("apply_comp_plan", "import_external_background", "final_render"),
    input_fields=(
        {"name": "foreground_clip_id", "type": "string", "required": True},
        {"name": "background_ref", "type": "string"},
    ),
    quality_gates=("matte_edge", "spill_suppression", "temporal_stability", "integration_quality"),
    acceptance_criteria=(
        "matte 边缘无溢色",
        "帧间 matte 稳定（闪烁 <= 阈值）",
        "色彩 / 模糊 / 颗粒一致性合格",
    ),
    fallback_strategy="require_user_decision",
)

SKILL_SCREEN_REPLACEMENT = ProfessionalSkill(
    skill_id="topos.screen_replacement",
    label="屏幕替换",
    description="屏幕跟踪、角点定位、内容替换与遮挡。",
    domain="composite",
    default_auto_scope=("analyze_track", "propose_comp_plan", "render_preview_proxy"),
    requires_confirmation=("apply_comp_plan", "import_generated_content", "final_render"),
    input_fields=(
        {"name": "target_clip_id", "type": "string", "required": True},
        {"name": "replacement_content_ref", "type": "string"},
    ),
    quality_gates=("track_confidence", "cut_boundary", "occlusion", "motion_blur"),
    acceptance_criteria=(
        "track confidence >= 0.8",
        "镜头切换处不延续旧 track",
        "遮挡区域正确处理",
        "motion blur 与场景一致",
    ),
    fallback_strategy="require_user_decision",
)

RECOMMENDED_SKILLS: tuple[ProfessionalSkill, ...] = (
    SKILL_INTERVIEW_HIGHLIGHTS,
    SKILL_PRODUCT_AD,
    SKILL_PODCAST_SOCIAL_CUT,
    SKILL_COURSE_CLEANUP,
    SKILL_CAMERA_MATCH_GRADE,
    SKILL_BRAND_LOOK,
    SKILL_GREEN_SCREEN_COMP,
    SKILL_SCREEN_REPLACEMENT,
)


def recommended_skill_registry() -> dict[str, ProfessionalSkill]:
    return {skill.skill_id: skill for skill in RECOMMENDED_SKILLS}


# --------------------------------------------------------------------------- #
# Namespace reconciliation (OS-11)
# --------------------------------------------------------------------------- #
# The official ``skill_manifest.py`` defines 3 shipped skills with IDs like
# ``topos.product_ad_30s``.  The recommended professional skills above use
# broader IDs like ``topos.product_ad``.  These aliases let callers resolve
# either form to a registered skill, preventing "skill not found" errors when
# a user or agent references a skill by its official or recommended name.

SKILL_ALIASES: dict[str, str] = {
    # Recommended → Official (when the official skill is a narrower version)
    "topos.product_ad": "topos.product_ad_30s",
    # Official → Recommended (for richer metadata lookup)
    "topos.product_ad_30s": "topos.product_ad",
    # Interview highlights vs voiceover polish are distinct but related
    "topos.interview_highlights": "topos.voiceover_polish",
}


def resolve_skill_id(skill_id: str) -> str:
    """Resolve a skill ID through the alias table.

    Returns the canonical ID.  If the input is already canonical (no alias
    needed), it is returned unchanged.  This enables both
    ``topos.product_ad`` and ``topos.product_ad_30s`` to look up the same
    skill definition.
    """
    return SKILL_ALIASES.get(str(skill_id or ""), str(skill_id or ""))


def register_recommended_skills(registry: Any) -> list[str]:
    """Register all recommended professional skills into a ``SkillRegistry``.

    Converts each ``ProfessionalSkill`` to a ``SkillManifest`` via
    ``to_manifest()`` and calls ``registry.register()``.  Returns the list of
    successfully registered skill IDs (duplicates are silently skipped).
    """
    registered: list[str] = []
    for skill in RECOMMENDED_SKILLS:
        existing = registry.get(skill.skill_id)
        if existing is not None:
            continue  # Already registered — skip
        try:
            manifest = skill.to_manifest()
            diags = registry.register(manifest, enabled=True)
            if not diags:
                registered.append(skill.skill_id)
        except Exception:  # noqa: BLE001
            continue
    return registered


__all__ = [
    "ProfessionalSkill",
    "RECOMMENDED_SKILLS",
    "recommended_skill_registry",
    "SKILL_ALIASES",
    "resolve_skill_id",
    "register_recommended_skills",
    "SKILL_INTERVIEW_HIGHLIGHTS",
    "SKILL_PRODUCT_AD",
    "SKILL_PODCAST_SOCIAL_CUT",
    "SKILL_COURSE_CLEANUP",
    "SKILL_CAMERA_MATCH_GRADE",
    "SKILL_BRAND_LOOK",
    "SKILL_GREEN_SCREEN_COMP",
    "SKILL_SCREEN_REPLACEMENT",
]
