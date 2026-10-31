# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Creative templates, style tokens and brand system (MG-13).

MG-13 gives the planner a reusable library so the LLM is not designing every
clip from zero.  Three layers:

* :class:`CreativeTemplate` — a full clip template (product launch / data
  explainer / flow diagram / title pack / tutorial / social card) with its
  beat structure, style tokens, asset scope and editable fields;
* :class:`StyleTokenSet` — palette + typography + motion tokens referenced
  by templates and proposals;
* :class:`BrandProfile` — the brand-level tokens (colours, fonts, logo
  policy, forbidden content) that override template defaults.

The registry is append-only and versioned; templates declare their
compatibility (which delivery profiles / codec capabilities they support) so
the planner can refuse "this template cannot export transparent ProRes"
up front instead of failing at export time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


# --------------------------------------------------------------------------- #
# Template categories
# --------------------------------------------------------------------------- #


CATEGORY_PRODUCT_LAUNCH = "product_launch"
CATEGORY_DATA_EXPLAINER = "data_explainer"
CATEGORY_FLOW_DIAGRAM = "flow_diagram"
CATEGORY_TITLE_PACK = "title_pack"
CATEGORY_TUTORIAL = "tutorial"
CATEGORY_SOCIAL_CARD = "social_card"

SUPPORTED_CATEGORIES: frozenset[str] = frozenset({
    CATEGORY_PRODUCT_LAUNCH,
    CATEGORY_DATA_EXPLAINER,
    CATEGORY_FLOW_DIAGRAM,
    CATEGORY_TITLE_PACK,
    CATEGORY_TUTORIAL,
    CATEGORY_SOCIAL_CARD,
})


# --------------------------------------------------------------------------- #
# Style tokens
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StyleTokenSet:
    """Reusable palette + typography + motion tokens."""

    token_id: str
    label: str
    palette: Mapping[str, str] = field(default_factory=dict)
    typography: Mapping[str, Any] = field(default_factory=dict)
    motion: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "label": self.label,
            "palette": dict(self.palette),
            "typography": dict(self.typography),
            "motion": dict(self.motion),
        }


# --------------------------------------------------------------------------- #
# Brand profile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BrandProfile:
    """Brand-level tokens that override template defaults.

    Forbidden content (words, colours, asset ids) is enforced by the planner:
    a template proposing a forbidden colour is rejected before preview.
    """

    brand_id: str
    label: str
    palette: Mapping[str, str] = field(default_factory=dict)
    typography: Mapping[str, Any] = field(default_factory=dict)
    logo_policy: Mapping[str, Any] = field(default_factory=dict)
    forbidden_words: tuple[str, ...] = ()
    forbidden_colors: tuple[str, ...] = ()
    forbidden_asset_ids: tuple[str, ...] = ()
    protected_product_colors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "label": self.label,
            "palette": dict(self.palette),
            "typography": dict(self.typography),
            "logo_policy": dict(self.logo_policy),
            "forbidden_words": list(self.forbidden_words),
            "forbidden_colors": list(self.forbidden_colors),
            "forbidden_asset_ids": list(self.forbidden_asset_ids),
            "protected_product_colors": list(self.protected_product_colors),
        }

    def conflicts(self, tokens: StyleTokenSet) -> tuple[str, ...]:
        """Return the forbidden colours the token set would introduce."""
        reasons: list[str] = []
        for key, value in tokens.palette.items():
            normalized = str(value).lower().strip()
            for forbidden in self.forbidden_colors:
                if normalized == str(forbidden).lower().strip():
                    reasons.append(f"palette.{key} 使用了被禁用的颜色 {forbidden}")
        return tuple(reasons)


# --------------------------------------------------------------------------- #
# Creative template
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TemplateCapabilities:
    """What a template can / cannot do across preview and export."""

    supports_transparent_export: bool = False
    supports_4k: bool = True
    supports_vertical: bool = True
    supports_square: bool = True
    min_duration_seconds: float = 3.0
    max_duration_seconds: float = 60.0
    editable_fields: tuple[str, ...] = ()
    required_asset_kinds: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "supports_transparent_export": self.supports_transparent_export,
            "supports_4k": self.supports_4k,
            "supports_vertical": self.supports_vertical,
            "supports_square": self.supports_square,
            "min_duration_seconds": self.min_duration_seconds,
            "max_duration_seconds": self.max_duration_seconds,
            "editable_fields": list(self.editable_fields),
            "required_asset_kinds": list(self.required_asset_kinds),
        }


@dataclass(frozen=True)
class TemplateBeat:
    """One beat in a template's narrative structure."""

    beat_id: str
    label: str
    duration_seconds: float
    on_screen_copy: str = ""
    motion_hint: str = ""
    layout_role: str = ""              # hero / supporting / caption / cta

    def to_dict(self) -> dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "label": self.label,
            "duration_seconds": self.duration_seconds,
            "on_screen_copy": self.on_screen_copy,
            "motion_hint": self.motion_hint,
            "layout_role": self.layout_role,
        }


@dataclass(frozen=True)
class CreativeTemplate:
    """A reusable Motion Graphic clip template."""

    template_id: str
    version: int
    display_name: str
    category: str
    description: str = ""
    default_beats: tuple[TemplateBeat, ...] = ()
    style_tokens: StyleTokenSet | None = None
    capabilities: TemplateCapabilities = field(default_factory=TemplateCapabilities)
    asset_scope: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.template_id:
            raise ValueError("CreativeTemplate.template_id is required")
        if self.version <= 0:
            raise ValueError("CreativeTemplate.version must be positive")
        if not self.display_name:
            raise ValueError("CreativeTemplate.display_name is required")
        if self.category not in SUPPORTED_CATEGORIES:
            raise ValueError(
                f"Unsupported template category: {self.category!r}; "
                f"expected one of {sorted(SUPPORTED_CATEGORIES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "display_name": self.display_name,
            "category": self.category,
            "description": self.description,
            "default_beats": [beat.to_dict() for beat in self.default_beats],
            "style_tokens": self.style_tokens.to_dict() if self.style_tokens else None,
            "capabilities": self.capabilities.to_dict(),
            "asset_scope": list(self.asset_scope),
            "metadata": dict(self.metadata),
        }


# --------------------------------------------------------------------------- #
# Registries
# --------------------------------------------------------------------------- #


# Templates that declare ``supports_4k=False`` cap out at a 1080p-class frame
# (long edge 1920).  Anything above that is a 4K-class request and must be
# refused up front instead of failing at export time (AUD-26800).
NON_4K_MAX_EDGE_PIXELS = 1920


class CreativeTemplateRegistry:
    """Versioned registry of creative templates + style tokens + brands."""

    def __init__(self) -> None:
        self._templates: dict[str, CreativeTemplate] = {}
        self._tokens: dict[str, StyleTokenSet] = {}
        self._brands: dict[str, BrandProfile] = {}

    # ---------------- templates ---------------- #

    def register_template(self, template: CreativeTemplate, *, replace: bool = False) -> bool:
        if template.template_id in self._templates and not replace:
            return False
        self._templates[template.template_id] = template
        return True

    def template(self, template_id: str) -> CreativeTemplate | None:
        return self._templates.get(template_id)

    def templates_by_category(self, category: str) -> tuple[CreativeTemplate, ...]:
        return tuple(t for t in self._templates.values() if t.category == category)

    def all_templates(self) -> tuple[CreativeTemplate, ...]:
        return tuple(self._templates[key] for key in sorted(self._templates))

    # ---------------- style tokens ---------------- #

    def register_tokens(self, tokens: StyleTokenSet, *, replace: bool = False) -> bool:
        if tokens.token_id in self._tokens and not replace:
            return False
        self._tokens[tokens.token_id] = tokens
        return True

    def tokens(self, token_id: str) -> StyleTokenSet | None:
        return self._tokens.get(token_id)

    def all_tokens(self) -> tuple[StyleTokenSet, ...]:
        return tuple(self._tokens[key] for key in sorted(self._tokens))

    # ---------------- brands ---------------- #

    def register_brand(self, brand: BrandProfile, *, replace: bool = False) -> bool:
        if brand.brand_id in self._brands and not replace:
            return False
        self._brands[brand.brand_id] = brand
        return True

    def brand(self, brand_id: str) -> BrandProfile | None:
        return self._brands.get(brand_id)

    def all_brands(self) -> tuple[BrandProfile, ...]:
        return tuple(self._brands[key] for key in sorted(self._brands))

    # ---------------- compatibility checks ---------------- #

    def compatibility_issues(
        self,
        template: CreativeTemplate,
        *,
        duration_seconds: float,
        canvas_aspect: str,
        transparent: bool,
        width: int | None = None,
        height: int | None = None,
    ) -> tuple[str, ...]:
        """Return reasons a template is incompatible with the request.

        ``width``/``height`` are the requested export frame size; when both are
        given, a frame whose long edge exceeds
        :data:`NON_4K_MAX_EDGE_PIXELS` is refused for templates that do not
        declare ``supports_4k``.
        """
        issues: list[str] = []
        caps = template.capabilities
        if duration_seconds < caps.min_duration_seconds:
            issues.append(
                f"模板最短 {caps.min_duration_seconds}s，请求 {duration_seconds}s"
            )
        if duration_seconds > caps.max_duration_seconds:
            issues.append(
                f"模板最长 {caps.max_duration_seconds}s，请求 {duration_seconds}s"
            )
        if canvas_aspect == "9:16" and not caps.supports_vertical:
            issues.append("模板不支持竖屏 9:16")
        if canvas_aspect == "1:1" and not caps.supports_square:
            issues.append("模板不支持 1:1")
        if transparent and not caps.supports_transparent_export:
            issues.append("模板不支持透明通道导出")
        if width is not None and height is not None:
            if (
                max(int(width), int(height)) > NON_4K_MAX_EDGE_PIXELS
                and not caps.supports_4k
            ):
                issues.append(
                    f"模板不支持 4K 分辨率（请求 {int(width)}×{int(height)}，"
                    f"上限长边 {NON_4K_MAX_EDGE_PIXELS}px）"
                )
        return tuple(issues)


# --------------------------------------------------------------------------- #
# Built-in library
# --------------------------------------------------------------------------- #


def create_default_creative_registry() -> CreativeTemplateRegistry:
    """Seed the registry with the MG-13 starter library."""
    registry = CreativeTemplateRegistry()

    # Style tokens
    registry.register_tokens(StyleTokenSet(
        token_id="tech_cool",
        label="科技冷色",
        palette={"background": "#0f172a", "accent": "#38bdf8", "text": "#f8fafc"},
        typography={"font_family": "sans", "title_size": 72, "body_size": 32},
        motion={"easing": "ease_in_out", "default_duration": 0.6},
    ))
    registry.register_tokens(StyleTokenSet(
        token_id="warm_social",
        label="社媒暖色",
        palette={"background": "#fff7ed", "accent": "#f97316", "text": "#1f2937"},
        typography={"font_family": "sans", "title_size": 64, "body_size": 28},
        motion={"easing": "ease_out", "default_duration": 0.4},
    ))

    # Templates
    registry.register_template(CreativeTemplate(
        template_id="product_launch_15s",
        version=1,
        display_name="产品发布 15 秒",
        category=CATEGORY_PRODUCT_LAUNCH,
        description="钩子 → 痛点 → 产品 → CTA，适合 15/30 秒广告。",
        default_beats=(
            TemplateBeat("hook", "钩子", 2.0, layout_role="hero"),
            TemplateBeat("pain", "痛点", 3.0, layout_role="supporting"),
            TemplateBeat("product", "产品", 6.0, layout_role="hero"),
            TemplateBeat("cta", "CTA", 4.0, layout_role="cta"),
        ),
        style_tokens=registry.tokens("tech_cool"),
        capabilities=TemplateCapabilities(
            min_duration_seconds=10.0,
            max_duration_seconds=45.0,
            editable_fields=("copy", "palette", "cta_text"),
        ),
    ))
    registry.register_template(CreativeTemplate(
        template_id="tutorial_intro_15s",
        version=1,
        display_name="教程片头 15 秒",
        category=CATEGORY_TUTORIAL,
        description="标题 + 4 步骤介绍，适合教学动画。",
        default_beats=(
            TemplateBeat("title", "标题", 3.0, layout_role="hero"),
            TemplateBeat("step_1", "步骤 1", 3.0, layout_role="supporting"),
            TemplateBeat("step_2", "步骤 2", 3.0, layout_role="supporting"),
            TemplateBeat("step_3", "步骤 3", 3.0, layout_role="supporting"),
            TemplateBeat("step_4", "步骤 4", 3.0, layout_role="supporting"),
        ),
        style_tokens=registry.tokens("tech_cool"),
        capabilities=TemplateCapabilities(
            min_duration_seconds=10.0,
            max_duration_seconds=30.0,
            editable_fields=("title", "step_labels", "palette"),
        ),
    ))
    registry.register_template(CreativeTemplate(
        template_id="social_card_9x16",
        version=1,
        display_name="社媒卡片 9:16",
        category=CATEGORY_SOCIAL_CARD,
        description="竖屏社媒卡片，强 CTA。",
        default_beats=(
            TemplateBeat("hook", "钩子", 2.0, layout_role="hero"),
            TemplateBeat("message", "信息", 4.0, layout_role="supporting"),
            TemplateBeat("cta", "CTA", 2.0, layout_role="cta"),
        ),
        style_tokens=registry.tokens("warm_social"),
        capabilities=TemplateCapabilities(
            supports_4k=False,
            min_duration_seconds=5.0,
            max_duration_seconds=30.0,
            editable_fields=("copy", "palette"),
        ),
    ))

    return registry


__all__ = [
    "BrandProfile",
    "CreativeTemplate",
    "CreativeTemplateRegistry",
    "StyleTokenSet",
    "TemplateBeat",
    "TemplateCapabilities",
    "SUPPORTED_CATEGORIES",
    "NON_4K_MAX_EDGE_PIXELS",
    "CATEGORY_PRODUCT_LAUNCH",
    "CATEGORY_DATA_EXPLAINER",
    "CATEGORY_FLOW_DIAGRAM",
    "CATEGORY_TITLE_PACK",
    "CATEGORY_TUTORIAL",
    "CATEGORY_SOCIAL_CARD",
    "create_default_creative_registry",
]
