"""Host-side autonomy helpers for Motion Graphic HTML revisions.

The edit path runs as a small autonomous pipeline:

    explore (dossier) -> classify (simple | complex) -> [plan -> steps]
    -> execute (per-step revision) -> verify (deterministic postconditions)

Everything here is deterministic (regex/stdlib only).  The one LLM touchpoint
is the *planner* prompt builders — the caller transports that call through the
existing HTML chat channel, so no new transport/threading surface is needed.

Design rules:
* The classifier is deliberately biased toward ``simple``: a wrong "complex"
  verdict costs an extra model round trip and can fragment a cohesive tweak;
  a wrong "simple" verdict merely means one bigger single-shot revision.
* The planner may collapse its own plan to one step — the model is allowed to
  disagree with the host's complexity guess.
* Postconditions are derived from the user's own words (quoted copy, explicit
  colors, page add/remove intents).  They gate application: a revision that
  provably fails the user's request is never written.
"""

from __future__ import annotations

import html as _html
import json
import re
from colorsys import rgb_to_hsv
from dataclasses import dataclass
from typing import Iterable

from ..core.html_page_deck import detect_page_count

MAX_EDIT_PLAN_STEPS = 4

_PAGE_CONTAINER_RE = re.compile(
    r"<(?P<tag>section|article|div|main|aside|header|footer)\b[^>]*"
    r"\bdata-topos-page\s*=\s*[\"'][^\"']*[\"'][^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(
    r"<title[^>]*>(?P<value>.*?)</title>", re.IGNORECASE | re.DOTALL,
)
_H1_RE = re.compile(
    r"<h1[^>]*>(?P<value>.*?)</h1>", re.IGNORECASE | re.DOTALL,
)
_QUOTED_RE = re.compile(r"[「“\"]([^」”\"]{1,80})[」”\"]")
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\b")

# Structural / sweeping requests need a plan: they touch many pages at once
# or change the deck's shape, where a single-shot prompt easily loses parts.
_COMPLEX_MARKERS = (
    "增加一页", "加一页", "新增一页", "加一页", "插入一页", "再加一页",
    "删除一页", "删掉一页", "去掉一页", "移除一页", "减少一页",
    "重新设计", "重新排版", "重新布局", "重构", "改版",
    "整体风格", "整体配色", "全部页面", "所有页", "每一页", "每个页面",
    "风格改成", "换成另一种风格", "配色方案", "视觉体系",
)
# Single-intent edit verbs: counting these distinguishes "one tweak" from
# "a list of tweaks" without needing any semantic model.
_EDIT_VERB_RE = re.compile(
    r"修改|改成|改为|换成|替换|调整|更新|去掉|删掉|移除|加上|添加|"
    r"变大|变小|加大|减小|改成|统一|对齐|换|改",
)
_CLAUSE_SEPARATOR_RE = re.compile(r"[；;]|然后|同时|另外|以及|再(?=[加添改换调])")


@dataclass(frozen=True)
class EditDossier:
    """A cheap, deterministic snapshot of the page the edit targets."""

    page_count: int
    page_texts: tuple[str, ...]
    title: str
    has_inline_script: bool

    def summary(self) -> str:
        lines = [f"- 页数：{self.page_count}"]
        if self.title:
            lines.append(f"- 标题：{self.title[:60]}")
        for index, text in enumerate(self.page_texts[:MAX_EDIT_PLAN_STEPS + 2], start=1):
            if text:
                lines.append(f"- 第 {index} 页文本片段：{text[:60]}")
        if len(self.page_texts) > MAX_EDIT_PLAN_STEPS + 2:
            lines.append(f"- （其余 {len(self.page_texts) - MAX_EDIT_PLAN_STEPS - 2} 页从略）")
        lines.append(
            f"- 内嵌脚本：{'有（JS 动效在逐帧捕获时自动对齐时间线）' if self.has_inline_script else '无'}"
        )
        return "\n".join(lines)


def _visible_text(fragment: str) -> str:
    text = _TAG_RE.sub(" ", str(fragment or ""))
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def build_edit_dossier(document: str) -> EditDossier:
    """Read the current page state the way an editor would eyeball it."""
    text = str(document or "")
    page_count = max(1, detect_page_count(text))
    marks = list(_PAGE_CONTAINER_RE.finditer(text))
    page_texts: list[str] = []
    if marks:
        for index, match in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
            page_texts.append(_visible_text(text[match.end():end])[:100])
    else:
        # No explicit deck markers (or markers only in CSS): treat the whole
        # document as one page for excerpt purposes; page_count above already
        # used the conventional-class fallback from detect_page_count.
        page_texts = [_visible_text(text)[:100]] if page_count == 1 else []
    title_match = _TITLE_RE.search(text) or _H1_RE.search(text)
    title = _visible_text(title_match.group("value")) if title_match else ""
    return EditDossier(
        page_count=page_count,
        page_texts=tuple(page_texts),
        title=title,
        has_inline_script=bool(re.search(r"<script\b", text, re.IGNORECASE)),
    )


def classify_edit_complexity(text: str, dossier: EditDossier) -> tuple[str, str]:
    """Decide between a direct single-shot revision and a planned pipeline.

    Returns ``("simple"|"complex", reason)``.  Biased toward simple: complex
    requires structural markers OR several distinct edit intents.
    """
    value = str(text or "")
    for marker in _COMPLEX_MARKERS:
        if marker in value:
            return "complex", f"涉及结构性改动（{marker}）"
    verb_hits = len(_EDIT_VERB_RE.findall(value))
    separators = len(_CLAUSE_SEPARATOR_RE.findall(value))
    if verb_hits >= 3:
        return "complex", f"包含 {verb_hits} 处不同的修改意图"
    if verb_hits >= 2 and separators >= 2:
        return "complex", "多个修改点分散在多个从句中"
    if dossier.page_count >= 4 and re.search(r"(全部|所有|每[一]?[个]?页|整体)", value):
        return "complex", "多页整体性调整"
    return "simple", ""


# ---------------------------------------------------------------------- #
# Planner contract
# ---------------------------------------------------------------------- #

EDIT_PLAN_SYSTEM_PROMPT = (
    "你是 Topos Color Motion Graphic 网页的修改规划师。你会收到：用户的修改需求、"
    "当前网页的状态档案、以及当前网页的完整源码。请先核对用户描述的问题/需求在当前"
    "网页中的实际状态（问题是否存在、涉及哪些页面），再输出一个分步修改计划。\n"
    "只输出一个 JSON 对象（不要 Markdown、不要解释文字）：\n"
    '{"understanding": "对用户需求的一句话理解", "issues_found": '
    '"核对当前网页后的发现（问题是否存在、涉及哪些页面；没有则写 无）", '
    '"steps": [{"summary": "一步的简短概括", "instruction": '
    '"给执行者的自包含修改指令：说明改哪几页、从什么改成什么"}]}\n'
    "规则：\n"
    "1. 如果核对后发现这其实是一两处局部改动，steps 只给 1 步，instruction 里写清"
    "全部改动。\n"
    "2. 结构性改动（增删页面、整体改风格/配色、多页联动）拆成 2-4 步，每步独立可"
    "完成、可验证，步与步之间不重复。\n"
    "3. 如果用户描述的问题在当前网页中并不存在或已经满足，在 issues_found 里说明，"
    "并把 steps 收缩为真正必要的最小修改（可以是保持现状的小优化）。\n"
    "4. 不要在计划里直接执行修改，也不要输出 HTML。"
)


@dataclass(frozen=True)
class EditStep:
    summary: str
    instruction: str


@dataclass(frozen=True)
class EditPlan:
    understanding: str
    issues_found: str
    steps: tuple[EditStep, ...]


def parse_edit_plan(response: str) -> EditPlan | None:
    """Tolerantly parse the planner's JSON; ``None`` means 'not a plan'."""
    text = str(response or "").strip()
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    steps: list[EditStep] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary", "") or "").strip()
        instruction = str(item.get("instruction", "") or "").strip()
        if summary and instruction:
            steps.append(EditStep(summary=summary[:80], instruction=instruction[:600]))
        if len(steps) >= MAX_EDIT_PLAN_STEPS:
            break
    if not steps:
        return None
    return EditPlan(
        understanding=str(payload.get("understanding", "") or "").strip()[:200],
        issues_found=str(payload.get("issues_found", "") or "").strip()[:300],
        steps=tuple(steps),
    )


# ---------------------------------------------------------------------- #
# Deterministic postconditions derived from the user's own words
# ---------------------------------------------------------------------- #

# Page add/remove intent must carry an explicit count ("加一页" / "删除两页")
# or an ordinal page target ("删除第2页" / "增加第 4 页").  A bare verb+页 is a
# normal copy edit about page *content* — "为每一页添加页码", "删除页面底部的
# 版权信息", "减少页面之间的留白" — and must NOT be treated as a page-count
# change (the old optional-count form did exactly that, AUD-26902).
_ADD_PAGE_QUANTITY = r"(?:增加|新增|插入|再加|添加|加)\s*(?:一|两|三|四|五|几|\d+)\s*页(?!码|面)"
# After an ordinal ("第 N 页"), possessive/content nouns ("第2页的标题",
# "第2页底部") mean an edit to that page's content, not a page-count change;
# a trailing label ("增加第 4 页总结") still counts as adding a page.
_PAGE_ORDINAL_TAIL = (
    r"(?:的|里|中|内|上|间|顶部|底部|标题|内容|文案|元素|图片|背景|按钮|页脚|页眉|页码)"
)
_ADD_PAGE_ORDINAL = (
    r"(?:增加|新增|插入|再加|添加|加)\s*第\s*(?:[1-9]\d*|[一二三四五六七八九十])\s*页"
    f"(?!{_PAGE_ORDINAL_TAIL})"
)
_ADD_PAGE_RE = re.compile(f"{_ADD_PAGE_QUANTITY}|{_ADD_PAGE_ORDINAL}")
_REMOVE_PAGE_QUANTITY = r"(?:删除|删掉|去掉|移除|减少|减掉)\s*(?:一|两|三|四|五|几|\d+)\s*页(?!码|面)"
_REMOVE_PAGE_ORDINAL = (
    r"(?:删除|删掉|去掉|移除)\s*第\s*(?:[1-9]\d*|[一二三四五六七八九十])\s*页"
    f"(?!{_PAGE_ORDINAL_TAIL})"
)
_REMOVE_PAGE_RE = re.compile(f"{_REMOVE_PAGE_QUANTITY}|{_REMOVE_PAGE_ORDINAL}")
# A quoted string counts as "must be removed" only when a removal verb leads
# *directly* into the quote.  Negated verbs ("不要删除「X」") keep X, and a
# rewrite target after a clause break ("删掉A，把文案改成「B」") is a *new*
# value, not a deletion target (AUD-26903).  The span between verb and quote
# therefore excludes clause punctuation and all quote characters, and common
# negations are rejected via lookbehind.
_REMOVE_QUOTED_RE = re.compile(
    r"(?<!不要)(?<!不需要)(?<!无需)(?<!别)"
    r"(删除|删掉|去掉|移除)"
    r"[^「」“”\"，。、；：,.;:!?！？]{0,12}"
    r"[「“\"]([^」”\"]{1,80})[」”\"]"
)
_EXPLICIT_NOOP_RE = re.compile(
    r"(?:如果|若|要是).{0,32}(?:已经|已是|已有).{0,32}"
    r"(?:不要|无需|不需要|不必).{0,24}(?:改|修改|变更)",
)
_PAGE_NUMBER_RE = re.compile(r"第\s*([1-9]\d*)\s*页")
_STYLE_RE = re.compile(r"<style\b[^>]*>(?P<value>.*?)</style>", re.IGNORECASE | re.DOTALL)
_PRESERVE_OTHER_PAGES_RE = re.compile(
    r"(?:其他|其余|剩余).{0,24}(?:页|页面).{0,24}"
    r"(?:不要|不应|别|保持).{0,12}(?:改|变|动)",
)
_PRESERVE_VISUAL_RE = re.compile(
    r"(?:视觉|样式|排版|配色|风格).{0,16}"
    r"(?:不要|不应|别|保持).{0,10}(?:改|变|动)",
)
_EXPLICIT_PAGE_ORDINAL_RE = re.compile(
    r"第\s*([1-9]\d*|[一二三四五六七八九十])\s*页"
)
_CHINESE_PAGE_NUMBERS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_PALETTE_TARGET_RE = re.compile(
    r"(?:改成|改为|改用|换成|使用|采用|主色(?:调)?(?:为|用)?|配色(?:为|用)?|以)\s*"
    r"(?P<family>橙(?:色(?:系|调)?|色系)?|蓝(?:色(?:系|调)?|色系)?|orange|blue)",
    re.IGNORECASE,
)
_RICH_PAGE_REQUEST_RE = re.compile(
    r"(?:每[一]?页|每个页面).{0,16}(?:内容|信息).{0,8}(?:丰富|充实)|"
    r"(?:每[一]?页|每个页面).{0,16}(?:丰富|充实)|"
    r"(?:内容|信息).{0,8}(?:更|再)?(?:丰富|充实)",
)
_STYLE_BLOCK_RE = re.compile(
    r"<style\b[^>]*>(?P<value>.*?)</style>", re.IGNORECASE | re.DOTALL,
)
_INLINE_STYLE_RE = re.compile(
    r"\bstyle\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_CSS_HEX_COLOR_RE = re.compile(
    r"#(?P<value>[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b"
)


def _compact_source(value: str) -> str:
    """Normalize formatting only, preserving authored HTML/CSS semantics."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _compact_css(value: str) -> str:
    """Ignore serializer-only whitespace while comparing stylesheet content."""
    compact = _compact_source(value)
    # The sanitizer normalizes e.g. ``@media(max-width:700px)`` to
    # ``@media (max-width:700px)``.  Whitespace around CSS delimiters is not a
    # visual change, while declarations and values remain otherwise intact.
    return re.sub(r"\s*([{}:;,()])\s*", r"\1", compact)


def _page_sources(document: str) -> tuple[str, ...]:
    """Return page container source slices in deck order.

    This deliberately does not attempt a full HTML parser: page boundaries are
    a host-owned ``data-topos-page`` contract, and retaining their authored
    slices lets the deterministic gate detect unintended changes to pages the
    user explicitly asked us to leave alone.
    """
    source = str(document or "")
    marks = list(_PAGE_CONTAINER_RE.finditer(source))
    return tuple(
        _compact_source(source[match.start(): marks[index + 1].start() if index + 1 < len(marks) else len(source)])
        for index, match in enumerate(marks)
    )


def _preservation_issues(text: str, original: str, revised: str) -> list[str]:
    """Verify explicit 'only change X' preservation promises.

    It is intentionally scoped to numbered-page requests.  Without an exact
    page target the host cannot know which source slice is permitted to move,
    so it should not reject a legitimate change.  Style blocks are checked
    separately when the user explicitly says the visual design must remain.
    """
    targets = {int(match.group(1)) - 1 for match in _PAGE_NUMBER_RE.finditer(text)}
    issues: list[str] = []
    if targets and _PRESERVE_OTHER_PAGES_RE.search(text):
        before_pages = _page_sources(original)
        after_pages = _page_sources(revised)
        for index, before in enumerate(before_pages):
            if index in targets:
                continue
            after = after_pages[index] if index < len(after_pages) else ""
            if before != after:
                issues.append(f"用户要求保持不变的第 {index + 1} 页发生了改动")
    if _PRESERVE_VISUAL_RE.search(text):
        before_styles = tuple(
            _compact_css(match.group("value"))
            for match in _STYLE_RE.finditer(original)
        )
        after_styles = tuple(
            _compact_css(match.group("value"))
            for match in _STYLE_RE.finditer(revised)
        )
        if before_styles != after_styles:
            issues.append("用户要求保持视觉不变，但样式表发生了改动")
    return issues


def deterministic_minimal_html_edit(user_text: str, document: str) -> str | None:
    """Apply one provably safe, source-preserving title substitution.

    A language model is unnecessarily risky for the narrow request "only
    replace page N's plain H1 and change nothing else".  When the user makes
    that scope explicit, the host can change precisely one HTML text node and
    still route the result through the normal sanitizer and browser verifier.
    Any nested title markup, ambiguous target, or broader request returns
    ``None`` and remains on the ordinary model-driven edit path.
    """
    text = str(user_text or "")
    if not re.search(r"(?:只|仅)\s*把?\s*第\s*[1-9]\d*\s*页", text):
        return None
    if "主标题" not in text:
        return None
    if not (_PRESERVE_OTHER_PAGES_RE.search(text) and _PRESERVE_VISUAL_RE.search(text)):
        return None
    page_match = _PAGE_NUMBER_RE.search(text)
    quotes = [value.strip() for value in _QUOTED_RE.findall(text) if value.strip()]
    if page_match is None or len(quotes) != 1:
        return None
    source = str(document or "")
    marks = list(_PAGE_CONTAINER_RE.finditer(source))
    page_index = int(page_match.group(1)) - 1
    if page_index < 0 or page_index >= len(marks):
        return None
    page_start = marks[page_index].start()
    page_end = marks[page_index + 1].start() if page_index + 1 < len(marks) else len(source)
    fragment = source[page_start:page_end]
    headings = list(_H1_RE.finditer(fragment))
    if len(headings) != 1:
        return None
    heading = headings[0]
    # Replacing an H1 containing spans/icons/animations would itself be a
    # visual rewrite.  Only plain-text titles qualify for this exact path.
    if "<" in heading.group("value") or "&" in heading.group("value"):
        return None
    replacement = _html.escape(quotes[0], quote=False)
    revised_fragment = (
        fragment[:heading.start("value")]
        + replacement
        + fragment[heading.end("value"):]
    )
    return source[:page_start] + revised_fragment + source[page_end:]


def edit_postcondition_issues(
    user_text: str, original_html: str, revised_html: str,
) -> list[str]:
    """Check the revision against requirements literally stated by the user.

    These are deliberately conservative: only requirements the user wrote in a
    machine-checkable form (quoted copy, explicit hex colors, page add/remove)
    become gates.  Empty list means "no stated requirement was violated".
    """
    text = str(user_text or "")
    original = str(original_html or "")
    revised = str(revised_html or "")
    revised_plain = _visible_text(revised)
    issues: list[str] = []

    removed_quotes = {
        match.group(2) for match in _REMOVE_QUOTED_RE.finditer(text)
    }
    for quote in _QUOTED_RE.findall(text):
        if not quote.strip():
            continue
        if quote in removed_quotes:
            if quote in revised_plain or quote in revised:
                issues.append(f"要求删除的「{quote}」仍然出现在修改后的网页中")
        elif quote not in revised_plain and quote not in revised:
            issues.append(f"要求的「{quote}」没有出现在修改后的网页中")

    for color in _HEX_COLOR_RE.findall(text):
        if color.lower() not in revised.lower():
            issues.append(f"要求的颜色 {color} 没有出现在修改后的网页中")

    original_pages = detect_page_count(original)
    revised_pages = detect_page_count(revised)
    if _ADD_PAGE_RE.search(text) and revised_pages <= original_pages:
        issues.append(
            f"要求增加页面，但页数仍为 {revised_pages}（原 {original_pages}）"
        )
    if _REMOVE_PAGE_RE.search(text) and revised_pages >= original_pages:
        issues.append(
            f"要求删除页面，但页数仍为 {revised_pages}（原 {original_pages}）"
        )
    issues.extend(_preservation_issues(text, original, revised))
    return issues


@dataclass(frozen=True)
class HtmlGenerationIntentReport:
    """Literal, deterministic acceptance result for a newly authored deck.

    Unlike an edit, a new document has no earlier page count to compare
    against.  Therefore this report gates only requirements that can be
    observed directly in the generated source: quoted copy, exact hex colors,
    supported named palette families, rich-page requests, and an explicitly
    numbered page (for example, ``第 4 页``).
    """

    requirements: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.issues

    def repair_feedback(self) -> str:
        if not self.issues:
            return ""
        return "明确需求验收未通过：" + "；".join(self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "requirements": list(self.requirements),
            "issues": list(self.issues),
            "passed": self.passed,
        }


def _unique_strings(values: Iterable[str]) -> tuple[str, ...]:
    """Return non-empty strings once, retaining the user's stated order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = str(value or "").strip()
        if compact and compact not in seen:
            result.append(compact)
            seen.add(compact)
    return tuple(result)


def _palette_family(value: str) -> str:
    """Normalize only palette names that have a stable CSS-hue oracle."""
    compact = str(value or "").lower()
    if compact in {"orange", "橙", "橙色", "橙色系", "橙色调"}:
        return "orange"
    if compact in {"blue", "蓝", "蓝色", "蓝色系", "蓝色调"}:
        return "blue"
    return ""


def _css_color_values(document: str) -> tuple[str, ...]:
    """Read hexadecimal visual colors without treating page copy as CSS."""
    source = str(document or "")
    styles = "\n".join(
        [match.group("value") for match in _STYLE_BLOCK_RE.finditer(source)]
        + [match.group("value") for match in _INLINE_STYLE_RE.finditer(source)]
    )
    return tuple(match.group("value") for match in _CSS_HEX_COLOR_RE.finditer(styles))


def _is_palette_color(value: str, family: str) -> bool:
    """Classify a hexadecimal CSS color conservatively by HSV hue."""
    raw = str(value or "")
    if len(raw) == 3:
        raw = "".join(character * 2 for character in raw)
    if len(raw) == 8:
        raw = raw[:6]  # alpha does not affect hue.
    if len(raw) != 6:
        return False
    try:
        red, green, blue = (int(raw[index:index + 2], 16) / 255.0 for index in range(0, 6, 2))
    except ValueError:
        return False
    hue, saturation, brightness = rgb_to_hsv(red, green, blue)
    degrees = hue * 360.0
    if saturation < 0.30 or brightness < 0.22:
        return False
    if family == "orange":
        return 10.0 <= degrees <= 55.0
    if family == "blue":
        return 195.0 <= degrees <= 255.0
    return False


def _requested_palette_families(request: str) -> tuple[str, ...]:
    return _unique_strings(
        _palette_family(match.group("family"))
        for match in _PALETTE_TARGET_RE.finditer(str(request or ""))
    )


def generation_intent_report(
    requirement_texts: Iterable[str] | str, generated_html: str,
) -> HtmlGenerationIntentReport:
    """Check explicit build requirements before staging a new HTML deck.

    The requirement texts comprise both the initial request and retained plan
    feedback.  This deliberately avoids subjective claims such as "looks more
    premium": only requirements with a stable host-side oracle become blocking
    acceptance conditions.  At present that includes quoted copy, exact colors,
    orange/blue palette requests, rich-page requests, and numbered pages.
    """
    texts = (requirement_texts,) if isinstance(requirement_texts, str) else requirement_texts
    request = "\n".join(str(value or "") for value in texts if str(value or "").strip())
    rendered = str(generated_html or "")
    rendered_plain = _visible_text(rendered)
    requirements: list[str] = []
    issues: list[str] = []

    removed_quotes = {
        match.group(2).strip()
        for match in _REMOVE_QUOTED_RE.finditer(request)
        if match.group(2).strip()
    }
    for quote in _unique_strings(_QUOTED_RE.findall(request)):
        if quote in removed_quotes:
            requirements.append(f"不包含文案「{quote}」")
            if quote in rendered_plain or quote in rendered:
                issues.append(f"要求删除的「{quote}」仍出现在生成网页中")
        else:
            requirements.append(f"包含文案「{quote}」")
            if quote not in rendered_plain and quote not in rendered:
                issues.append(f"要求的「{quote}」没有出现在生成网页中")

    for color in _unique_strings(_HEX_COLOR_RE.findall(request)):
        requirements.append(f"使用颜色 {color.lower()}")
        if color.lower() not in rendered.lower():
            issues.append(f"要求的颜色 {color.lower()} 没有出现在生成网页中")

    css_colors = _css_color_values(rendered)
    for family in _requested_palette_families(request):
        label = "橙色色系" if family == "orange" else "蓝色色系"
        requirements.append(f"使用{label}")
        if not any(_is_palette_color(value, family) for value in css_colors):
            issues.append(f"要求使用{label}，但样式中没有检测到对应主色")

    requested_page_numbers: list[int] = []
    for match in _EXPLICIT_PAGE_ORDINAL_RE.finditer(request):
        value = match.group(1)
        number = int(value) if value.isdecimal() else _CHINESE_PAGE_NUMBERS.get(value, 0)
        if number > 0:
            requested_page_numbers.append(number)
    if requested_page_numbers:
        minimum_page_count = max(requested_page_numbers)
        requirements.append(f"至少 {minimum_page_count} 页")
        actual_page_count = detect_page_count(rendered)
        if actual_page_count < minimum_page_count:
            issues.append(
                f"要求包含第 {minimum_page_count} 页，但生成网页只有 {actual_page_count} 页"
            )

    if _RICH_PAGE_REQUEST_RE.search(request):
        requirements.append("每页信息丰富")
        # The shared deterministic design evaluator already measures each
        # page's visible copy, hierarchy and content/visual units.  A slightly
        # stronger threshold is only activated when the user explicitly asks
        # for richer pages; ordinary creative briefs retain the baseline gate.
        from .html_design_quality import evaluate_html_design

        quality = evaluate_html_design(rendered)
        thin_pages = [
            page.page_index for page in quality.pages if page.richness_score < 0.58
        ]
        if thin_pages:
            pages = "、".join(str(index) for index in thin_pages)
            issues.append(f"用户要求每页内容丰富，但第 {pages} 页信息密度仍不足")

    return HtmlGenerationIntentReport(
        requirements=_unique_strings(requirements),
        issues=_unique_strings(issues),
    )


def explicit_noop_reason(user_text: str, dossier: EditDossier) -> str:
    """Return a safe no-op explanation for an explicitly conditional edit.

    This deliberately handles only a narrow shape: the user identifies one
    numbered page, quotes the desired visible copy, and explicitly asks not
    to change anything if it is already present.  Broader semantic equivalence
    remains an LLM task; guessing there risks suppressing a legitimate edit.
    """
    text = str(user_text or "")
    if not _EXPLICIT_NOOP_RE.search(text):
        return ""
    page_match = _PAGE_NUMBER_RE.search(text)
    quoted = [value.strip() for value in _QUOTED_RE.findall(text) if value.strip()]
    if page_match is None or not quoted:
        return ""
    index = int(page_match.group(1)) - 1
    if index < 0 or index >= len(dossier.page_texts):
        return ""
    page_text = dossier.page_texts[index]
    if all(value in page_text for value in quoted):
        return f"第 {index + 1} 页已包含指定文案「{quoted[0]}」，无需修改"
    return ""


__all__ = [
    "EDIT_PLAN_SYSTEM_PROMPT", "MAX_EDIT_PLAN_STEPS",
    "EditDossier", "EditPlan", "EditStep",
    "build_edit_dossier", "classify_edit_complexity", "parse_edit_plan",
    "deterministic_minimal_html_edit", "edit_postcondition_issues", "explicit_noop_reason",
    "HtmlGenerationIntentReport", "generation_intent_report",
]
