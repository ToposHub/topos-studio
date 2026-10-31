"""Deterministic design-quality signals for freeform Motion Graphic HTML.

Browser validation proves that a page paints; it cannot tell whether the page
is only a lone headline or carries enough hierarchy and information for a
short-form explainer.  This module deliberately makes no subjective claim that
one visual style is "beautiful".  It provides small, explainable signals that
are safe to feed back to the author model:

* every page has enough visible information or intentional visual material;
* pages expose a readable headline/content hierarchy;
* the document has a minimal visual language (layout + palette/motion);
* the delivery viewport is not accidentally used as a fixed web-page canvas;
* consecutive pages are not accidental near-duplicates.

The evaluation uses only sanitized HTML and stdlib regexes.  It is therefore
deterministic, offline, and suitable for a bounded pre-preview repair pass.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
from html.parser import HTMLParser
import re
from typing import Any


_PAGE_RE = re.compile(
    r"<(?P<tag>section|article|div|main|aside|header|footer)\b[^>]*"
    r"\bdata-topos-page\s*=\s*[\"'][^\"']*[\"'][^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_RE = re.compile(r"<h[1-3]\b", re.IGNORECASE)
# Models commonly implement a visual heading with a ``div``/``span`` and a
# class such as ``section-title`` instead of a semantic h1-h3 tag.  Treat the
# common naming convention as an explicit heading signal too: this keeps the
# completeness check useful without forcing a particular HTML authoring style.
_TITLE_LIKE_RE = re.compile(
    r"<(?:div|p|span|header)\b[^>]*\b(?:class|data-role)\s*=\s*[\"']"
    r"[^\"']*(?:title|headline|heading|display|hero)[^\"']*[\"'][^>]*>",
    re.IGNORECASE,
)
_TEXT_BLOCK_RE = re.compile(
    r"<(?:p|li|span|figcaption|button|label|dt|dd|blockquote)\b",
    re.IGNORECASE,
)
_VISUAL_RE = re.compile(
    r"<(?:svg|canvas|img|video|picture)\b|<(?:path|circle|rect|polygon|line|use)\b",
    re.IGNORECASE,
)
_CONTENT_UNIT_RE = re.compile(
    r"<(?:li|article|table|figure|dl)\b|\b(?:card|stat|metric|step|chip|"
    r"timeline|node|feature|item)\b",
    re.IGNORECASE,
)
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)", re.IGNORECASE)
_STYLE_RE = re.compile(r"<style\b[^>]*>(?P<value>.*?)</style>", re.IGNORECASE | re.DOTALL)
_INLINE_STYLE_RE = re.compile(
    r"\bstyle\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_FLUID_SIZE_RE = re.compile(
    r"\b(?:width|height|min-width|min-height|max-width|max-height)\s*:\s*"
    r"[^;{}]*(?:\d+(?:\.\d+)?(?:vw|vh|vmin|vmax|%)|clamp\()",
    re.IGNORECASE,
)
_REFLOW_LAYOUT_RE = re.compile(
    r"(?:@media\b|\bflex-wrap\s*:\s*wrap\b|"
    r"\bgrid-template-(?:columns|rows)\s*:[^;{}]*\b\d*\.?\d+fr\b)",
    re.IGNORECASE,
)
_LAYOUT_RE = re.compile(r"\bdisplay\s*:\s*(?:grid|flex)\b", re.IGNORECASE)
_FIXED_DIMENSION_RE = re.compile(
    r"\b(?P<axis>width|height|min-width|min-height|max-width|max-height)\s*:\s*"
    r"(?P<value>\d+(?:\.\d+)?)px\b",
    re.IGNORECASE,
)

_PAGE_TAGS = frozenset({"section", "article", "div", "main", "aside", "header", "footer"})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


class _PageStructureParser(HTMLParser):
    """Find page roots that are nested or left open by a model response."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, str | None]] = []
        self.issues: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {
            str(key).lower(): str(value or "").strip()
            for key, value in attrs
            if key
        }

    def _start(
        self, tag: str, attrs: list[tuple[str, str | None]], *, push: bool,
    ) -> None:
        lowered = str(tag or "").lower()
        values = self._attrs(attrs)
        marker = values.get("data-topos-page") or None
        if marker is not None:
            ancestors = [value for _, value in self._stack if value is not None]
            if ancestors:
                self.issues.append(
                    f"第 {marker} 页嵌套在第 {ancestors[-1]} 页内，页面边界不是独立兄弟节点"
                )
        if push and lowered not in _VOID_TAGS:
            self._stack.append((lowered, marker if lowered in _PAGE_TAGS else None))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, push=True)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, push=False)

    def handle_endtag(self, tag: str) -> None:
        lowered = str(tag or "").lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == lowered:
                del self._stack[index:]
                return

    def finish(self) -> tuple[str, ...]:
        # HTML5 permits the final optional closing tags at end-of-document.
        # An unclosed final page is therefore not blocking on its own; if a
        # later page is accidentally placed inside it, ``_start`` has already
        # emitted the precise nested-page diagnostic.
        return tuple(dict.fromkeys(self.issues))


def _page_structure_issues(document: str) -> tuple[str, ...]:
    """Return deterministic page-boundary diagnostics for sanitized HTML."""
    parser = _PageStructureParser()
    try:
        parser.feed(str(document or ""))
        parser.close()
    except Exception:  # noqa: BLE001 - malformed model HTML is a diagnostic
        return ("页面 HTML 结构无法完成静态解析，需重新生成独立页面容器",)
    return parser.finish()


def _visible_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _css_source(document: str) -> str:
    """Collect style blocks and inline declarations without parsing user JS."""
    return "\n".join(
        [match.group("value") for match in _STYLE_RE.finditer(document)]
        + [match.group("value") for match in _INLINE_STYLE_RE.finditer(document)]
    )


def _page_fragments(document: str) -> tuple[str, ...]:
    source = str(document or "")
    marks = list(_PAGE_RE.finditer(source))
    if not marks:
        return (source,) if source.strip() else ()
    return tuple(
        source[mark.start(): marks[index + 1].start() if index + 1 < len(marks) else len(source)]
        for index, mark in enumerate(marks)
    )


def _word_set(text: str) -> frozenset[str]:
    """Small CJK/Latin-insensitive token set used only for duplicate warnings."""
    compact = re.sub(r"\s+", "", str(text or ""))
    cjk_bigrams = {compact[index:index + 2] for index in range(max(0, len(compact) - 1))}
    latin = set(re.findall(r"[a-z0-9]{2,}", compact.lower()))
    return frozenset(cjk_bigrams | latin)


@dataclass(frozen=True)
class HtmlPageDesignSignal:
    """Explainable design signals for a single authored page."""

    page_index: int
    visible_characters: int
    heading_count: int
    text_block_count: int
    content_unit_count: int
    visual_unit_count: int
    richness_score: float
    findings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "visible_characters": self.visible_characters,
            "heading_count": self.heading_count,
            "text_block_count": self.text_block_count,
            "content_unit_count": self.content_unit_count,
            "visual_unit_count": self.visual_unit_count,
            "richness_score": round(self.richness_score, 3),
            "findings": list(self.findings),
        }


@dataclass(frozen=True)
class HtmlDesignQualityReport:
    """A non-subjective, user-visible summary of HTML design completeness."""

    pages: tuple[HtmlPageDesignSignal, ...]
    visual_language_score: float
    page_diversity_score: float
    responsive_layout_score: float
    fixed_canvas_rules: int
    overall_score: float
    warnings: tuple[str, ...] = ()
    page_structure_issues: tuple[str, ...] = ()

    @property
    def needs_repair(self) -> bool:
        return bool(
            any(page.richness_score < 0.42 for page in self.pages)
            or self.visual_language_score < 0.30
            or self.fixed_canvas_rules > 0
            or bool(self.page_structure_issues)
        )

    def repair_feedback(self) -> str:
        """Return a concrete, bounded instruction for one author-model retry."""
        weak_pages = [page for page in self.pages if page.richness_score < 0.42]
        details = []
        for page in weak_pages[:4]:
            details.append(
                f"第 {page.page_index} 页只有 {page.visible_characters} 个可见字符、"
                f"{page.content_unit_count} 个内容单元和 {page.visual_unit_count} 个视觉单元"
            )
        if self.visual_language_score < 0.30:
            details.append("整套页面缺少明确的布局与色彩/动效视觉语言")
        if self.fixed_canvas_rules:
            details.append(
                f"检测到 {self.fixed_canvas_rules} 处将网页尺寸直接锁定为交付视口像素"
            )
        if self.page_structure_issues:
            details.extend(self.page_structure_issues[:3])
        detail = "；".join(details) or "页面信息密度不足"
        return (
            "本地设计完整度检查发现：" + detail + "。请保留主题与时长，"
            "为每一页补足明确标题、至少两组有意义的信息单元（解释、步骤、数据或案例）"
            "和与主题相关的图形/版式层级；不要只放孤立大标题或重复上一页。"
            "交付尺寸仅是视频捕获视口：html/body/页面容器请使用 100vw、100vh、"
            "百分比、fr、clamp() 或媒体查询自适应，不能写死为交付宽高的 px 画布。"
            "每个 data-topos-page 必须是独立的兄弟容器，不能嵌套在另一个页面内；"
            "如果页面边界不完整，请补齐 HTML 标签后再提交。"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_score": round(self.overall_score, 3),
            "visual_language_score": round(self.visual_language_score, 3),
            "page_diversity_score": round(self.page_diversity_score, 3),
            "responsive_layout_score": round(self.responsive_layout_score, 3),
            "fixed_canvas_rules": self.fixed_canvas_rules,
            "page_structure_issues": list(self.page_structure_issues),
            "needs_repair": self.needs_repair,
            "warnings": list(self.warnings),
            "pages": [page.to_dict() for page in self.pages],
        }


def _page_signal(fragment: str, page_index: int) -> HtmlPageDesignSignal:
    text = _visible_text(fragment)
    visible_characters = len(re.sub(r"\s+", "", text))
    heading_count = len(_HEADING_RE.findall(fragment)) + len(_TITLE_LIKE_RE.findall(fragment))
    text_blocks = len(_TEXT_BLOCK_RE.findall(fragment))
    content_units = len(_CONTENT_UNIT_RE.findall(fragment))
    visual_units = min(4, len(_VISUAL_RE.findall(fragment)))

    copy_score = min(1.0, visible_characters / 72.0)
    unit_score = min(1.0, (text_blocks + content_units + visual_units) / 5.0)
    hierarchy_score = 1.0 if heading_count else 0.35
    score = max(0.0, min(1.0, 0.50 * copy_score + 0.35 * unit_score + 0.15 * hierarchy_score))

    findings: list[str] = []
    if visible_characters < 24 and visual_units == 0:
        findings.append("页面可见信息过少，像占位标题而非完整叙事段落")
    if heading_count == 0:
        findings.append("缺少明确标题层级")
    if content_units < 2 and visual_units == 0:
        findings.append("缺少可支撑叙事的内容单元或主题图形")
    return HtmlPageDesignSignal(
        page_index=page_index,
        visible_characters=visible_characters,
        heading_count=heading_count,
        text_block_count=text_blocks,
        content_unit_count=content_units,
        visual_unit_count=visual_units,
        richness_score=score,
        findings=tuple(findings),
    )


def _visual_language_score(document: str) -> float:
    styles = _css_source(document)
    colors = {value.lower() for value in _COLOR_RE.findall(styles)}
    has_layout = bool(re.search(r"\b(?:grid|flex)\b", styles, re.IGNORECASE))
    has_gradient = "gradient(" in styles.lower()
    has_motion = bool(re.search(r"@keyframes|\banimation(?:-|:)", styles, re.IGNORECASE))
    palette = min(1.0, len(colors) / 3.0)
    return min(1.0, 0.45 * palette + 0.30 * float(has_layout) + 0.15 * float(has_gradient) + 0.10 * float(has_motion))


def _page_diversity_score(fragments: tuple[str, ...]) -> float:
    if len(fragments) <= 1:
        return 1.0
    similarities: list[float] = []
    token_sets = [_word_set(_visible_text(fragment)) for fragment in fragments]
    for before, after in zip(token_sets, token_sets[1:]):
        union = before | after
        similarity = len(before & after) / len(union) if union else 1.0
        similarities.append(similarity)
    return max(0.0, min(1.0, 1.0 - (sum(similarities) / len(similarities))))


def _responsive_layout_signal(
    document: str, *, viewport_width: float, viewport_height: float,
) -> tuple[float, int]:
    """Return an explainable static responsiveness score and hard canvas count.

    This is intentionally a source-level signal, not a claim about subjective
    composition.  The browser remains the authority for paintability.  A rule
    is considered a hard canvas lock only when it exactly targets the current
    delivery width or height, which is the pattern that turns ``1920×1080``
    from a capture viewport into a non-responsive webpage.
    """
    styles = _css_source(document)
    has_fluid_sizing = bool(_FLUID_SIZE_RE.search(styles))
    has_reflow_layout = bool(_REFLOW_LAYOUT_RE.search(styles))
    has_layout = bool(_LAYOUT_RE.search(styles))
    locks = 0
    for match in _FIXED_DIMENSION_RE.finditer(styles):
        axis = match.group("axis").lower()
        value = float(match.group("value"))
        extent = viewport_height if "height" in axis else viewport_width
        if abs(value - max(1.0, float(extent))) <= 1.0:
            locks += 1
    score = (
        0.55 * float(has_fluid_sizing)
        + 0.30 * float(has_reflow_layout)
        + 0.15 * float(has_layout)
    )
    if locks:
        score = max(0.0, score - min(0.75, 0.35 * locks))
    return min(1.0, score), locks


def evaluate_html_design(
    document: str, *, viewport_width: float = 1920.0, viewport_height: float = 1080.0,
) -> HtmlDesignQualityReport:
    """Assess page richness, visual language and responsive-layout signals."""
    source = str(document or "")
    fragments = _page_fragments(source)
    pages = tuple(_page_signal(fragment, index + 1) for index, fragment in enumerate(fragments))
    language = _visual_language_score(source)
    diversity = _page_diversity_score(fragments)
    responsive, fixed_canvas_rules = _responsive_layout_signal(
        source,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )
    page_structure_issues = _page_structure_issues(source)
    richness = sum(page.richness_score for page in pages) / len(pages) if pages else 0.0
    structure_penalty = min(0.30, 0.15 * len(page_structure_issues))
    overall = max(
        0.0,
        min(1.0, 0.57 * richness + 0.18 * language + 0.13 * diversity + 0.12 * responsive - structure_penalty),
    )
    warnings: list[str] = []
    weak = [str(page.page_index) for page in pages if page.richness_score < 0.42]
    if weak:
        warnings.append("第 " + "、".join(weak) + " 页信息密度偏低")
    if language < 0.30:
        warnings.append("缺少可辨识的布局与色彩/动效视觉语言")
    if fixed_canvas_rules:
        warnings.append(
            "检测到 " + str(fixed_canvas_rules)
            + " 处将交付视口写死为 px 画布，已要求改为响应式布局"
        )
    if page_structure_issues:
        warnings.extend(page_structure_issues[:3])
    elif responsive < 0.30:
        warnings.append("未检测到明确的响应式尺寸或重排信号，建议补充流式布局")
    if len(pages) > 1 and diversity < 0.12:
        warnings.append("相邻页面文案高度重复，叙事推进不足")
    return HtmlDesignQualityReport(
        pages=pages,
        visual_language_score=language,
        page_diversity_score=diversity,
        responsive_layout_score=responsive,
        fixed_canvas_rules=fixed_canvas_rules,
        overall_score=overall,
        warnings=tuple(warnings),
        page_structure_issues=page_structure_issues,
    )


__all__ = [
    "HtmlDesignQualityReport",
    "HtmlPageDesignSignal",
    "evaluate_html_design",
]
