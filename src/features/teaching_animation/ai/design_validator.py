"""Design / render / accessibility level validation for Motion Graphic scenes.

The existing :mod:`teaching_animation.core.validator` enforces the *schema*
contract — required fields, types, ranges, references and keyframe ordering.
That layer answers "is this a well-formed scene document?".  MG-10 needs a
second layer that answers "will this scene actually *work* as a Motion
Graphic clip the user will be happy with?" — design intent, render safety and
basic accessibility.

This module provides that second layer as a pure, Qt-free function
:func:`validate_scene_design` that takes a scene (or its dict form) plus an
optional :class:`DesignValidationOptions` and returns a
:class:`DesignValidationReport` with stable diagnostic codes (one per finding)
mapped to the :mod:`error_codes` vocabulary.

Design rules implemented (MG-10 acceptance):

* canvas bounds / safe-area / overlap (design level)
* minimum font size and text overflow (accessibility level)
* contrast between text fill and the element behind it (accessibility level)
* keyframe range + animation count + complexity ceiling (render level)
* duplicate element ids / duplicate animation ids (render level)
* duration vs requested brief duration (consistency level)
* unresolved asset references (render level)
* flicker risk (opacity flipping between 0 and 1 across adjacent keyframes)

Findings are deliberately separable into ``errors`` (block preview/commit) and
``warnings`` (the user may proceed).  The MG-10 acceptance criterion is that
"不合法方案在预览前被解释性阻断；每个 finding 定位元素与修复建议" — so each
finding carries an element id and a short suggestion.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .error_codes import (
    MOTION_GRAPHIC_ASSET_UNRESOLVED,
    MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS,
    MOTION_GRAPHIC_COMPLEXITY_LIMIT,
    MOTION_GRAPHIC_DUPLICATE_ELEMENT,
    MOTION_GRAPHIC_LOW_CONTRAST,
    MOTION_GRAPHIC_TEXT_OVERFLOW,
)


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DesignValidationOptions:
    """Tunable thresholds for the design validator.

    Defaults are conservative for a 1080p-ish 15s clip.  Callers (delivery
    profile, accessibility preset) can override any field without touching the
    validator itself.
    """

    #: Fraction of the canvas reserved as safe area on each side (0.0–0.5).
    #: Title-safe historically ~5%, but Motion Graphic copy often sits closer
    #: to the edge; 0.04 keeps logos/captions inside broadcast-safe by default.
    safe_area_ratio: float = 0.04

    #: Minimum on-canvas font size in scene units.  Below this, body copy is
    #: unreadable at typical export resolutions.
    min_font_size: float = 18.0

    #: Recommended font size; below it we warn (not block).
    recommended_font_size: float = 24.0

    #: Minimum contrast ratio (WCAG AA threshold for large text is 3:1, AA for
    #: normal text 4.5:1).  We use 3.0 as the warning floor and 4.5 as the
    #: error floor; both are overridable.
    min_contrast_warning: float = 3.0
    min_contrast_error: float = 4.5

    #: Maximum elements before we warn about render cost.
    max_elements_warning: int = 40

    #: Maximum elements before we hard-block (the scene is almost certainly a
    #: planner bug, not a real design).
    max_elements_error: int = 120

    #: Maximum animations before we warn about render cost.
    max_animations_warning: int = 60

    #: Maximum animations before we hard-block.
    max_animations_error: int = 200

    #: Maximum number of keyframes per track before we warn.
    max_keyframes_per_track: int = 64

    #: Tolerance (in scene units) for treating two elements as overlapping.
    overlap_tolerance: float = 1.0

    #: When True, elements partially outside the canvas are errors; when False
    #: they are warnings (some designs intentionally bleed off-canvas).
    strict_bounds: bool = True


_DEFAULT_OPTIONS = DesignValidationOptions()


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DesignFinding:
    """One design-level diagnostic finding.

    Attributes:
        code: Stable error-code vocabulary entry from :mod:`error_codes`.
        severity: ``"error"`` blocks preview/commit; ``"warning"`` is advisory.
        element_id: Element the finding is about (``""`` for scene-level).
        message: Short, user-facing description.
        suggestion: One-line fix the user can act on.
        path: Dotted path inside the scene document for precise navigation.
    """

    code: str
    severity: str
    element_id: str
    message: str
    suggestion: str
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "element_id": self.element_id,
            "message": self.message,
            "suggestion": self.suggestion,
            "path": self.path,
        }


@dataclass(frozen=True)
class DesignValidationReport:
    """Aggregated result of :func:`validate_scene_design`."""

    findings: tuple[DesignFinding, ...] = ()

    @property
    def errors(self) -> tuple[DesignFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[DesignFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def finding_count(self) -> int:
        return len(self.findings)

    def findings_for(self, element_id: str) -> tuple[DesignFinding, ...]:
        return tuple(f for f in self.findings if f.element_id == element_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "finding_count": self.finding_count,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [finding.to_dict() for finding in self.findings],
        }


# --------------------------------------------------------------------------- #
# Color utilities (WCAG contrast)
# --------------------------------------------------------------------------- #


def _parse_color(value: Any) -> tuple[int, int, int] | None:
    """Parse ``#rgb`` / ``#rrggbb`` / ``rgb(r,g,b)`` into an (r, g, b) tuple."""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return tuple(int(round(float(c))) for c in value[:3])  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.startswith("rgb"):
        inner = text[text.find("(") + 1: text.find(")")]
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        if len(parts) >= 3:
            try:
                return tuple(int(round(float(p))) for p in parts[:3])  # type: ignore[return-value]
            except (TypeError, ValueError):
                return None
        return None
    if text.startswith("#"):
        hex_text = text[1:]
        if len(hex_text) == 3:
            hex_text = "".join(ch * 2 for ch in hex_text)
        if len(hex_text) != 6:
            return None
        try:
            return (
                int(hex_text[0:2], 16),
                int(hex_text[2:4], 16),
                int(hex_text[4:6], 16),
            )
        except ValueError:
            return None
    return None


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def _channel(value: int) -> float:
        s = value / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(foreground: Any, background: Any) -> float | None:
    """Return the WCAG contrast ratio between two colors, or ``None``.

    ``None`` means at least one color was unparseable — callers should skip the
    contrast check rather than guess.  A ratio of 1.0 means identical colors;
    the maximum theoretical ratio is 21:1 (white on black).
    """

    fg = _parse_color(foreground)
    bg = _parse_color(background)
    if fg is None or bg is None:
        return None
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _scene_to_dict(scene: Any) -> Mapping[str, Any]:
    if isinstance(scene, Mapping):
        return scene
    if hasattr(scene, "to_dict"):
        return scene.to_dict()
    raise TypeError("validate_scene_design expects a scene model or its dict form")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _bbox(transform: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    x = _number(transform.get("x"))
    y = _number(transform.get("y"))
    w = _number(transform.get("width"))
    h = _number(transform.get("height"))
    if x is None or y is None or w is None or h is None:
        return None
    return (x, y, x + w, y + h)


def _bbox_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    tolerance: float,
) -> bool:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    if ax2 <= bx1 + tolerance or bx2 <= ax1 + tolerance:
        return False
    if ay2 <= by1 + tolerance or by2 <= ay1 + tolerance:
        return False
    return True


# --------------------------------------------------------------------------- #
# Validators (one per concern)
# --------------------------------------------------------------------------- #


def _check_canvas_bounds(
    elements: list[Mapping[str, Any]],
    width: int,
    height: int,
    options: DesignValidationOptions,
    findings: list[DesignFinding],
) -> None:
    safe_dx = width * options.safe_area_ratio
    safe_dy = height * options.safe_area_ratio
    for index, element in enumerate(elements):
        element_id = str(element.get("element_id", ""))
        transform = element.get("transform", {})
        if not isinstance(transform, Mapping):
            continue
        bbox = _bbox(transform)
        if bbox is None:
            continue
        x1, y1, x2, y2 = bbox
        fully_out = x2 <= 0 or y2 <= 0 or x1 >= width or y1 >= height
        partially_out = (
            x1 < -options.overlap_tolerance
            or y1 < -options.overlap_tolerance
            or x2 > width + options.overlap_tolerance
            or y2 > height + options.overlap_tolerance
        )
        if fully_out:
            severity = "error"
            message = f"元素 {element_id} 完全在画布之外。"
        elif partially_out:
            severity = "error" if options.strict_bounds else "warning"
            message = f"元素 {element_id} 部分超出画布边界。"
        else:
            continue
        findings.append(DesignFinding(
            code=MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS,
            severity=severity,
            element_id=element_id,
            message=message,
            suggestion="将元素移回画布内，或缩小尺寸后再预览。",
            path=f"elements[{index}].transform",
        ))
        continue

    # Safe-area check (warning only): warn if a text element's bbox pokes into
    # the title-safe margin.  We only flag text here because logos / shapes are
    # commonly designed to bleed.
    for index, element in enumerate(elements):
        if element.get("type") != "text":
            continue
        element_id = str(element.get("element_id", ""))
        transform = element.get("transform", {})
        if not isinstance(transform, Mapping):
            continue
        bbox = _bbox(transform)
        if bbox is None:
            continue
        x1, y1, x2, y2 = bbox
        if x1 < safe_dx or y1 < safe_dy or x2 > width - safe_dx or y2 > height - safe_dy:
            findings.append(DesignFinding(
                code=MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS,
                severity="warning",
                element_id=element_id,
                message=f"文本元素 {element_id} 越过安全边距，可能在部分平台被裁切。",
                suggestion="将文本整体内移到安全区内。",
                path=f"elements[{index}].transform",
            ))


def _backing_color(
    text_bbox: tuple[float, float, float, float] | None,
    elements: list[Mapping[str, Any]],
    scene_bg: Any,
    tolerance: float,
) -> Any:
    """Colour directly behind a text box: the topmost opaque element that fully
    contains the text, else the scene background.

    Text commonly sits on cards / shapes rather than the bare canvas, so the
    contrast check must compare against the element actually behind the glyphs
    (MG-10 docstring promise, AUD-26806).  Only *containing* elements count —
    a shape that merely clips a corner is not what the reader sees behind the
    text body.
    """
    if text_bbox is None:
        return scene_bg
    tx1, ty1, tx2, ty2 = text_bbox
    best_z: int | None = None
    best_color: Any = None
    for order, element in enumerate(elements):
        if element.get("type") == "text":
            continue
        if element.get("visible", True) is False:
            continue
        style = element.get("style", {})
        if not isinstance(style, Mapping):
            continue
        opacity = _number(style.get("opacity"))
        if opacity is not None and opacity < 1.0:
            continue  # semi-transparent backers blend into what is below
        color = style.get("fill") or style.get("color") or style.get("background")
        if _parse_color(color) is None:
            continue
        transform = element.get("transform", {})
        if not isinstance(transform, Mapping):
            continue
        bbox = _bbox(transform)
        if bbox is None:
            continue
        bx1, by1, bx2, by2 = bbox
        contains = (
            bx1 <= tx1 + tolerance and by1 <= ty1 + tolerance
            and bx2 >= tx2 - tolerance and by2 >= ty2 - tolerance
        )
        if not contains:
            continue
        raw_z = _number(element.get("z_index"))
        z_index = (int(raw_z) if raw_z is not None else 0) * 1_000_000 + order
        if best_z is None or z_index >= best_z:
            best_z = z_index
            best_color = color
    return best_color if best_color is not None else scene_bg


def _check_text_legibility(
    elements: list[Mapping[str, Any]],
    background: Mapping[str, Any],
    options: DesignValidationOptions,
    findings: list[DesignFinding],
) -> None:
    scene_bg_color = background.get("color") if isinstance(background, Mapping) else None
    for index, element in enumerate(elements):
        if element.get("type") != "text":
            continue
        element_id = str(element.get("element_id", ""))
        style = element.get("style", {})
        if not isinstance(style, Mapping):
            style = {}
        font_size = _number(style.get("font_size") or style.get("fontSize"))
        if font_size is not None:
            if font_size < options.min_font_size:
                findings.append(DesignFinding(
                    code=MOTION_GRAPHIC_TEXT_OVERFLOW,
                    severity="error",
                    element_id=element_id,
                    message=(
                        f"文本元素 {element_id} 字号 {font_size:.1f} 小于最小可读字号 "
                        f"{options.min_font_size:.1f}。"
                    ),
                    suggestion="加大字号，或缩短文案以适配更紧凑的版式。",
                    path=f"elements[{index}].style.font_size",
                ))
            elif font_size < options.recommended_font_size:
                findings.append(DesignFinding(
                    code=MOTION_GRAPHIC_TEXT_OVERFLOW,
                    severity="warning",
                    element_id=element_id,
                    message=(
                        f"文本元素 {element_id} 字号 {font_size:.1f} 低于推荐字号 "
                        f"{options.recommended_font_size:.1f}，可能在导出时不清晰。"
                    ),
                    suggestion="如非刻意，建议使用更大字号。",
                    path=f"elements[{index}].style.font_size",
                ))

        # Heuristic text-overflow: compare approximate text width to bbox width.
        content = element.get("content")
        if isinstance(content, str) and content and font_size and font_size > 0:
            transform = element.get("transform", {})
            bbox = _bbox(transform) if isinstance(transform, Mapping) else None
            if bbox is not None:
                approx_width = _estimate_text_width(content, font_size)
                # Allow some slack: a text box may legitimately wrap.
                if approx_width > (bbox[2] - bbox[0]) * 1.6:
                    findings.append(DesignFinding(
                        code=MOTION_GRAPHIC_TEXT_OVERFLOW,
                        severity="warning",
                        element_id=element_id,
                        message=f"文本元素 {element_id} 的文案可能超出文本框宽度。",
                        suggestion="缩短文案、减小字号或加宽文本框。",
                        path=f"elements[{index}].content",
                    ))

        # Contrast check: foreground fill vs the element directly behind the
        # text (topmost opaque containing shape), falling back to the scene
        # background when the text sits on the bare canvas.
        transform = element.get("transform", {})
        text_bbox = _bbox(transform) if isinstance(transform, Mapping) else None
        bg_color = _backing_color(text_bbox, elements, scene_bg_color, options.overlap_tolerance)
        fg_color = style.get("color") or style.get("fill")
        ratio = contrast_ratio(fg_color, bg_color)
        if ratio is not None:
            if ratio < options.min_contrast_error:
                findings.append(DesignFinding(
                    code=MOTION_GRAPHIC_LOW_CONTRAST,
                    severity="error",
                    element_id=element_id,
                    message=(
                        f"文本元素 {element_id} 与背景对比度 {ratio:.2f}:1，"
                        f"低于可读性下限 {options.min_contrast_error:.1f}:1。"
                    ),
                    suggestion="加深文字颜色或换更浅的背景以满足可读性。",
                    path=f"elements[{index}].style.color",
                ))
            elif ratio < options.min_contrast_warning:
                findings.append(DesignFinding(
                    code=MOTION_GRAPHIC_LOW_CONTRAST,
                    severity="warning",
                    element_id=element_id,
                    message=(
                        f"文本元素 {element_id} 与背景对比度 {ratio:.2f}:1，"
                        f"低于推荐对比度 {options.min_contrast_warning:.1f}:1。"
                    ),
                    suggestion="建议提升前景与背景的明暗差。",
                    path=f"elements[{index}].style.color",
                ))


def _estimate_text_width(text: str, font_size: float) -> float:
    """Rough monospace-ish width estimate; only used for overflow heuristics.

    CJK characters are approximately square; Latin characters average ~0.55em.
    This is deliberately conservative (overestimates width) so the warning
    fires early rather than after the user has approved a broken layout.
    """

    width = 0.0
    for char in text:
        if ord(char) > 0x2E80:  # CJK and beyond
            width += font_size
        else:
            width += font_size * 0.55
    return width


def _check_complexity(
    elements: list[Mapping[str, Any]],
    animations: list[Mapping[str, Any]],
    options: DesignValidationOptions,
    findings: list[DesignFinding],
) -> None:
    element_count = len(elements)
    if element_count > options.max_elements_error:
        findings.append(DesignFinding(
            code=MOTION_GRAPHIC_COMPLEXITY_LIMIT,
            severity="error",
            element_id="",
            message=f"场景包含 {element_count} 个元素，超过硬上限 {options.max_elements_error}。",
            suggestion="减少元素数量或拆分为多个场景。",
            path="elements",
        ))
    elif element_count > options.max_elements_warning:
        findings.append(DesignFinding(
            code=MOTION_GRAPHIC_COMPLEXITY_LIMIT,
            severity="warning",
            element_id="",
            message=f"场景包含 {element_count} 个元素，渲染开销可能较高。",
            suggestion="考虑精简元素以加快预览。",
            path="elements",
        ))

    animation_count = len(animations)
    if animation_count > options.max_animations_error:
        findings.append(DesignFinding(
            code=MOTION_GRAPHIC_COMPLEXITY_LIMIT,
            severity="error",
            element_id="",
            message=f"场景包含 {animation_count} 条动画，超过硬上限 {options.max_animations_error}。",
            suggestion="减少动画数量或合并关键帧。",
            path="animations",
        ))
    elif animation_count > options.max_animations_warning:
        findings.append(DesignFinding(
            code=MOTION_GRAPHIC_COMPLEXITY_LIMIT,
            severity="warning",
            element_id="",
            message=f"场景包含 {animation_count} 条动画，预览可能变慢。",
            suggestion="考虑减少不必要的动画。",
            path="animations",
        ))

    # Per-track keyframe ceiling + flicker heuristic.
    for index, animation in enumerate(animations):
        if not isinstance(animation, Mapping):
            continue
        animation_id = str(animation.get("animation_id", f"animations[{index}]"))
        keyframes = animation.get("keyframes")
        if not isinstance(keyframes, list):
            continue
        if len(keyframes) > options.max_keyframes_per_track:
            findings.append(DesignFinding(
                code=MOTION_GRAPHIC_COMPLEXITY_LIMIT,
                severity="warning",
                element_id=str(animation.get("element_id", "")),
                message=(
                    f"动画 {animation_id} 含 {len(keyframes)} 个关键帧，"
                    f"超过单轨建议上限 {options.max_keyframes_per_track}。"
                ),
                suggestion="精简关键帧或拆分为多段动画。",
                path=f"animations[{index}].keyframes",
            ))
        property_name = str(animation.get("property") or animation.get("param_name") or "")
        if property_name == "opacity" and len(keyframes) >= 3:
            values = []
            for kf in keyframes:
                if isinstance(kf, Mapping):
                    value = _number(kf.get("value"))
                    if value is not None:
                        values.append(value)
            flips = sum(
                1 for a, b in zip(values, values[1:])
                if (a < 0.5 <= b) or (b < 0.5 <= a)
            )
            if flips >= 3:
                findings.append(DesignFinding(
                    code=MOTION_GRAPHIC_COMPLEXITY_LIMIT,
                    severity="warning",
                    element_id=str(animation.get("element_id", "")),
                    message=(
                        f"动画 {animation_id} 在透明度上出现 {flips} 次显隐切换，"
                        "存在闪烁风险。"
                    ),
                    suggestion="减少透明度的反复切换或加入缓动。",
                    path=f"animations[{index}].keyframes",
                ))


def _check_duplicates(
    elements: list[Mapping[str, Any]],
    animations: list[Mapping[str, Any]],
    findings: list[DesignFinding],
) -> None:
    seen_elements: set[str] = set()
    for index, element in enumerate(elements):
        element_id = str(element.get("element_id", ""))
        if not element_id:
            continue
        if element_id in seen_elements:
            findings.append(DesignFinding(
                code=MOTION_GRAPHIC_DUPLICATE_ELEMENT,
                severity="error",
                element_id=element_id,
                message=f"元素 ID {element_id} 在场景中重复出现。",
                suggestion="为每个元素使用唯一 ID。",
                path=f"elements[{index}].element_id",
            ))
        else:
            seen_elements.add(element_id)

    element_id_set = seen_elements
    seen_animations: set[str] = set()
    for index, animation in enumerate(animations):
        if not isinstance(animation, Mapping):
            continue
        animation_id = str(animation.get("animation_id", ""))
        if animation_id and animation_id in seen_animations:
            findings.append(DesignFinding(
                code=MOTION_GRAPHIC_DUPLICATE_ELEMENT,
                severity="error",
                element_id=str(animation.get("element_id", "")),
                message=f"动画 ID {animation_id} 在场景中重复出现。",
                suggestion="为每条动画使用唯一 ID。",
                path=f"animations[{index}].animation_id",
            ))
        else:
            seen_animations.add(animation_id)
        target = str(animation.get("element_id", ""))
        if target and element_id_set and target not in element_id_set:
            findings.append(DesignFinding(
                code=MOTION_GRAPHIC_DUPLICATE_ELEMENT,
                severity="error",
                element_id=animation_id,
                message=f"动画 {animation_id} 引用了不存在的元素 {target}。",
                suggestion="检查动画的 element_id 是否拼写正确。",
                path=f"animations[{index}].element_id",
            ))


def _check_assets(
    elements: list[Mapping[str, Any]],
    asset_refs: list[Mapping[str, Any]],
    findings: list[DesignFinding],
) -> None:
    asset_ids = {
        str(asset.get("asset_id"))
        for asset in asset_refs
        if isinstance(asset, Mapping) and asset.get("asset_id")
    }
    for index, element in enumerate(elements):
        element_type = element.get("type")
        if element_type not in {"image", "svg"}:
            continue
        element_id = str(element.get("element_id", ""))
        ref = element.get("asset_ref")
        if not ref:
            # inline content (svg path data) is allowed; only flag missing ref
            # for image elements that clearly need one.
            if element_type == "image" and not element.get("content"):
                findings.append(DesignFinding(
                    code=MOTION_GRAPHIC_ASSET_UNRESOLVED,
                    severity="warning",
                    element_id=element_id,
                    message=f"图片元素 {element_id} 未引用任何素材且没有内嵌内容。",
                    suggestion="为元素指定 asset_ref 或提供内嵌内容。",
                    path=f"elements[{index}].asset_ref",
                ))
            continue
        if ref not in asset_ids:
            findings.append(DesignFinding(
                code=MOTION_GRAPHIC_ASSET_UNRESOLVED,
                severity="error",
                element_id=element_id,
                message=f"元素 {element_id} 引用了未声明的素材 {ref}。",
                suggestion="在 asset_refs 中登记该素材或修正引用。",
                path=f"elements[{index}].asset_ref",
            ))


def _check_overlap(
    elements: list[Mapping[str, Any]],
    options: DesignValidationOptions,
    findings: list[DesignFinding],
) -> None:
    """Warn when two visible text elements overlap (readability risk).

    We only flag text-on-text overlap because shape stacking is intentional in
    most Motion Graphic designs.  The pairwise check is O(n²); scenes beyond
    ``max_elements_error`` skip it entirely — the complexity hard-limit error
    already blocks the preview (checked earlier by ``_check_complexity``), so
    spending quadratic time on an adversarial LLM scene would only slow the
    generation-confirm path down (AUD-26809).
    """

    if len(elements) > options.max_elements_error:
        return

    text_boxes: list[tuple[str, int, tuple[float, float, float, float]]] = []
    for index, element in enumerate(elements):
        if element.get("type") != "text":
            continue
        if element.get("visible", True) is False:
            continue
        transform = element.get("transform", {})
        if not isinstance(transform, Mapping):
            continue
        bbox = _bbox(transform)
        if bbox is None:
            continue
        text_boxes.append((str(element.get("element_id", "")), index, bbox))

    for i in range(len(text_boxes)):
        for j in range(i + 1, len(text_boxes)):
            id_a, idx_a, box_a = text_boxes[i]
            id_b, idx_b, box_b = text_boxes[j]
            if _bbox_overlap(box_a, box_b, options.overlap_tolerance):
                findings.append(DesignFinding(
                    code=MOTION_GRAPHIC_TEXT_OVERFLOW,
                    severity="warning",
                    element_id=id_a,
                    message=f"文本元素 {id_a} 与 {id_b} 的边界框重叠。",
                    suggestion="调整布局以避免文字相互遮挡。",
                    path=f"elements[{idx_a}].transform",
                ))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def validate_scene_design(
    scene: Any,
    *,
    options: DesignValidationOptions | None = None,
    expected_duration_frames: int | None = None,
) -> DesignValidationReport:
    """Run design / accessibility / render-safety checks on a scene.

    Args:
        scene: A :class:`TeachingSceneDocument` or its dict form.
        options: Tunable thresholds; ``None`` uses conservative defaults.
        expected_duration_frames: When provided, a mismatch between the scene's
            ``duration_frames`` and the brief's expected duration is reported as
            a warning (the user asked for 15s but the plan is 14s).

    Returns:
        A :class:`DesignValidationReport` with stable-code findings.
    """

    opts = options or _DEFAULT_OPTIONS
    data = _scene_to_dict(scene)
    findings: list[DesignFinding] = []

    width = int(data.get("width", 0) or 0)
    height = int(data.get("height", 0) or 0)
    duration_frames = int(data.get("duration_frames", 0) or 0)
    background = data.get("background", {})
    if not isinstance(background, Mapping):
        background = {}

    raw_elements = data.get("elements", [])
    elements: list[Mapping[str, Any]] = [
        element for element in raw_elements if isinstance(element, Mapping)
    ]
    raw_animations = data.get("animations", [])
    animations: list[Mapping[str, Any]] = [
        animation for animation in raw_animations if isinstance(animation, Mapping)
    ]
    raw_assets = data.get("asset_refs", [])
    asset_refs: list[Mapping[str, Any]] = [
        asset for asset in raw_assets if isinstance(asset, Mapping)
    ]

    if width > 0 and height > 0:
        _check_canvas_bounds(elements, width, height, opts, findings)
    _check_text_legibility(elements, background, opts, findings)
    _check_complexity(elements, animations, opts, findings)
    _check_duplicates(elements, animations, findings)
    _check_assets(elements, asset_refs, findings)
    _check_overlap(elements, opts, findings)

    if (
        expected_duration_frames is not None
        and expected_duration_frames > 0
        and duration_frames > 0
        and abs(duration_frames - expected_duration_frames) > max(1, expected_duration_frames // 20)
    ):
        findings.append(DesignFinding(
            code=MOTION_GRAPHIC_TEXT_OVERFLOW,
            severity="warning",
            element_id="",
            message=(
                f"场景时长 {duration_frames} 帧与需求 {expected_duration_frames} 帧不一致。"
            ),
            suggestion="检查动画终点帧或更新需求时长。",
            path="duration_frames",
        ))

    return DesignValidationReport(findings=tuple(findings))


__all__ = [
    "DesignValidationOptions",
    "DesignFinding",
    "DesignValidationReport",
    "validate_scene_design",
    "contrast_ratio",
]
