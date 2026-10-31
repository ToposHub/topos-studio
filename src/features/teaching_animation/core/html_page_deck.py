"""Page-deck timing shared by the Motion Graphic editor and HTML export.

The model owns the visual design of each page.  This module owns only the
page boundaries: page containers are marked with ``data-topos-page`` and the
editor/exporter can then resolve a frame or a keyboard action to one page.
"""

from __future__ import annotations

from dataclasses import dataclass
import html as _html
import json
import math
import re
from typing import Any, Mapping


PAGE_TIMING_KEY = "page_timing"
PAGE_MARKER_ATTR = "data-topos-page"
DEFAULT_PAGE_SECONDS = 5.0
DEFAULT_PAGE_TRANSITION = "cut"
DEFAULT_PAGE_TRANSITION_SECONDS = 0.4
PAGE_TRANSITIONS: tuple[str, ...] = (
    DEFAULT_PAGE_TRANSITION,
    "crossfade",
    "slide-left", "slide-right", "slide-up", "slide-down",
    "zoom",
    "wipe-left", "wipe-right", "wipe-up", "wipe-down",
)
DEFAULT_GLOBAL_BACKGROUND = "#111827"

# P1-UI-01：宿主拥有的 DOM 节点唯一清单。注入方（本模块的 deck 运行时与
# 背景标记）与清理方（html_browser_preview 的 DevTools Apply 序列化、
# 导出前净化）都必须引用本清单生成选择器——不允许各自手写子集，否则
# 「Apply 写回 authored 文档」的所有权契约会因遗漏而失败
# （历史缺陷：topos-page-background-style/controller 未进清理列表）。
HOST_OWNED_DOM_IDS = (
    "topos-page-deck-editor-controller",
    "topos-page-deck-editor-runtime",
    "topos-page-deck-controller",
    "topos-page-deck-capture",
    "topos-page-background-style",
    "topos-page-background-controller",
)

# Background values are authored through the editor or arrive from a
# sanitized HTML document.  Keep the host-owned style injection deliberately
# narrow: a background value must never be able to terminate a declaration or
# introduce markup/script into the generated page-deck controller.
_SAFE_BACKGROUND_RE = re.compile(
    r"^(?:transparent|currentcolor|[a-z]+|"
    r"#[0-9a-f]{3,8}|"
    r"(?:rgb|rgba|hsl|hsla|lab|lch|oklab|oklch|color)\([^<>{};'\"`]*\)|"
    r"(?:linear-gradient|radial-gradient|conic-gradient)\([^<>{};'\"`]*\))$",
    re.IGNORECASE,
)


def safe_background_value(value: Any, *, fallback: str = "") -> str:
    """Return a CSS background value safe for host-owned style injection."""
    text = str(value or "").strip()
    if not text or not _SAFE_BACKGROUND_RE.fullmatch(text):
        return str(fallback or "")
    return text


def scene_background_css(background: Any) -> str:
    """Read the document background color/CSS value with a stable fallback."""
    value: Any = background
    if isinstance(background, Mapping):
        value = background.get("css", background.get("color", ""))
    return safe_background_value(value, fallback=DEFAULT_GLOBAL_BACKGROUND)


def _background_values(
    global_background: Any,
    page_backgrounds: tuple[Any, ...] | list[Any] = (),
) -> tuple[str, tuple[str, ...]]:
    """Normalize global and page overrides before building browser markup."""
    global_css = scene_background_css(global_background)
    pages = tuple(
        safe_background_value(value)
        for value in (page_backgrounds or ())
    )
    return global_css, pages


def _background_style(global_background: Any) -> str:
    global_css, _pages = _background_values(global_background)
    return (
        "html,body{"
        f"background:{global_css} !important;"
        "}"
    )


def _background_script(
    global_background: Any,
    page_backgrounds: tuple[Any, ...] | list[Any] = (),
) -> str:
    """Build the small host controller that applies page-local overrides."""
    global_css, pages = _background_values(global_background, page_backgrounds)
    payload = json.dumps(
        {"global": global_css, "pages": list(pages)},
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return f"""
<script id="topos-page-background-controller">
(() => {{
  const config = {payload};
  const apply = () => {{
    const pages = Array.from(document.querySelectorAll('[data-topos-page]'));
    pages.forEach((page, index) => {{
      const value = config.pages[index] || '';
      if (value) page.style.setProperty('background', value, 'important');
      else page.style.removeProperty('background');
    }});
  }};
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', apply, {{once: true}});
  }} else {{
    apply();
  }}
}})();
</script>
"""


def _background_markup(
    global_background: Any,
    page_backgrounds: tuple[Any, ...] | list[Any] = (),
) -> str:
    """Return host-owned CSS + JS for preview/runtime page backgrounds."""
    return (
        f"<style id=\"topos-page-background-style\">"
        f"{_background_style(global_background)}</style>"
        + _background_script(global_background, page_backgrounds)
    )

# A ``data-topos-page`` element is a page boundary owned by the host, not a
# host-authored visual template.  Older versions forced every page to
# ``position:absolute; inset:0; width:100%; height:100%``.  That made ordinary
# browser decks such as ``.slide { width:88vw; display:none }`` lose their
# centered card, rounded corners and internal composition when imported.  The
# model is now allowed to choose the page's own size and layout.  The host
# only supplies a stable box model; page visibility is handled separately by
# the active-page rules below.
_PAGE_LAYOUT_CSS = (
    "[data-topos-page]{box-sizing:border-box}"
    "[data-topos-page].topos-page-auto-box{position:absolute!important;inset:0!important;"
    "width:100%!important;height:100%!important;min-width:0!important;min-height:0!important}"
)

# The host does not impose a layout on authored pages.  It only repairs the
# one failure mode that makes an otherwise populated page impossible to
# capture: every visible node is outside the current viewport.  The repair is
# applied at runtime, after the page's own CSS/animation state has settled,
# and only for that page/frame.  It preserves the authored ``translate``
# value and records a diagnostic attribute so capture evidence can explain
# what happened.
_PAGE_VIEWPORT_RECOVERY_JS = r"""
const recoverPageViewport = (page) => {
  if (!page) return false;
  if (!page.hasAttribute('data-topos-original-translate')) {
    page.setAttribute(
      'data-topos-original-translate',
      page.style.getPropertyValue('translate') || ''
    );
  }
  const originalTranslate = page.getAttribute('data-topos-original-translate') || '';
  page.removeAttribute('data-topos-page-recovered');
  page.style.removeProperty('translate');
  if (originalTranslate) page.style.setProperty('translate', originalTranslate);
  const vw = window.innerWidth || 0;
  const vh = window.innerHeight || 0;
  if (vw <= 0 || vh <= 0) return false;
  const intersects = (rect) =>
    rect.right > 0 && rect.bottom > 0 && rect.left < vw && rect.top < vh;
  const descendants = Array.from(page.querySelectorAll('*'));
  const visible = descendants.filter((node) => {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    const opacity = Number.parseFloat(style.opacity || '1');
    return rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' && style.visibility !== 'hidden' && opacity > 0;
  });
  // A page containing only text/background is still recoverable; otherwise
  // use its own box as the single visible primitive.  For normal composed
  // pages, descendants are used so an on-canvas page background does not hide
  // an off-canvas content group from the recovery check.
  if (!visible.length && !descendants.length) {
    const rect = page.getBoundingClientRect();
    const style = getComputedStyle(page);
    const opacity = Number.parseFloat(style.opacity || '1');
    if (rect.width > 0 && rect.height > 0 &&
        style.display !== 'none' && style.visibility !== 'hidden' && opacity > 0) {
      visible.push(page);
    }
  }
  if (!visible.length || visible.some((node) => intersects(node.getBoundingClientRect()))) {
    return false;
  }
  const rects = visible.map((node) => node.getBoundingClientRect());
  const bounds = rects.reduce((acc, rect) => ({
    left: Math.min(acc.left, rect.left),
    top: Math.min(acc.top, rect.top),
    right: Math.max(acc.right, rect.right),
    bottom: Math.max(acc.bottom, rect.bottom),
  }), { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity });
  const margin = Math.max(4, Math.min(vw, vh) * 0.02);
  let dx = 0;
  let dy = 0;
  if (bounds.right <= 0) dx = margin - bounds.right;
  else if (bounds.left >= vw) dx = (vw - margin) - bounds.right;
  if (bounds.bottom <= 0) dy = margin - bounds.bottom;
  else if (bounds.top >= vh) dy = (vh - margin) - bounds.bottom;
  if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return false;
  page.style.setProperty('translate', `${dx}px ${dy}px`, 'important');
  const recovered = visible.some((node) => intersects(node.getBoundingClientRect()));
  if (!recovered) {
    page.style.removeProperty('translate');
    if (originalTranslate) page.style.setProperty('translate', originalTranslate);
    return false;
  }
  page.setAttribute('data-topos-page-recovered', '1');
  return true;
};
"""

# Browser-owned transition surface.  The capture bridge sets one numeric
# progress variable per frame; the same rules are animated by the visible
# editor preview when its page button changes.  Model-authored page CSS is
# restored automatically as soon as the transition attributes are removed.
_PAGE_TRANSITION_CSS = r"""
@property --topos-transition-progress {
  syntax: "<number>";
  inherits: false;
  initial-value: 1;
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition-role] {
  --topos-transition-progress: 1;
  visibility: visible!important;
  pointer-events: none!important;
  opacity: var(--topos-transition-opacity, 1)!important;
  transform: var(--topos-transition-transform, none)!important;
  clip-path: var(--topos-transition-clip, none)!important;
  transition: --topos-transition-progress var(--topos-transition-duration, 0s)
    cubic-bezier(.22,.61,.36,1);
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition-role="enter"] {
  --topos-transition-opacity: var(--topos-transition-progress);
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition-role="leave"] {
  --topos-transition-opacity: calc(1 - var(--topos-transition-progress));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="slide-left"][data-topos-transition-role="enter"] {
  --topos-transition-transform: translateX(calc((1 - var(--topos-transition-progress)) * 100%));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="slide-left"][data-topos-transition-role="leave"] {
  --topos-transition-transform: translateX(calc(var(--topos-transition-progress) * -100%));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="slide-right"][data-topos-transition-role="enter"] {
  --topos-transition-transform: translateX(calc((var(--topos-transition-progress) - 1) * 100%));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="slide-right"][data-topos-transition-role="leave"] {
  --topos-transition-transform: translateX(calc(var(--topos-transition-progress) * 100%));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="slide-up"][data-topos-transition-role="enter"] {
  --topos-transition-transform: translateY(calc((1 - var(--topos-transition-progress)) * 100%));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="slide-up"][data-topos-transition-role="leave"] {
  --topos-transition-transform: translateY(calc(var(--topos-transition-progress) * -100%));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="slide-down"][data-topos-transition-role="enter"] {
  --topos-transition-transform: translateY(calc((var(--topos-transition-progress) - 1) * 100%));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="slide-down"][data-topos-transition-role="leave"] {
  --topos-transition-transform: translateY(calc(var(--topos-transition-progress) * 100%));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="zoom"][data-topos-transition-role="enter"] {
  --topos-transition-transform: scale(calc(.94 + var(--topos-transition-progress) * .06));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="zoom"][data-topos-transition-role="leave"] {
  --topos-transition-transform: scale(calc(1 + var(--topos-transition-progress) * .06));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="wipe-left"][data-topos-transition-role="enter"] {
  --topos-transition-clip: inset(0 0 0 calc((1 - var(--topos-transition-progress)) * 100%));
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="wipe-right"][data-topos-transition-role="enter"] {
  --topos-transition-clip: inset(0 calc((1 - var(--topos-transition-progress)) * 100%) 0 0);
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="wipe-up"][data-topos-transition-role="enter"] {
  --topos-transition-clip: inset(calc((1 - var(--topos-transition-progress)) * 100%) 0 0 0);
}
[data-topos-page][data-topos-transitioning="1"][data-topos-transition="wipe-down"][data-topos-transition-role="enter"] {
  --topos-transition-clip: inset(0 0 calc((1 - var(--topos-transition-progress)) * 100%) 0);
}
"""

_PAGE_START_RE = re.compile(
    r"<(?P<tag>section|article|div|main|aside|header|footer)\b"
    r"(?P<attrs>[^>]*?)>", re.IGNORECASE | re.DOTALL,
)
_CLASS_RE = re.compile(r"\bclass\s*=\s*(['\"])(?P<value>.*?)\1", re.IGNORECASE | re.DOTALL)
_PAGE_CLASS_RE = re.compile(
    # Match ``page``, ``slide``, ``screen`` and numbered variants such as
    # ``slide-2``/``screen_03``.  Do not mistake ordinary inner classes such
    # as ``slide-header``, ``slide-title`` or ``slide-body`` for page roots.
    r"(?:^|\s)(?:page|slide|screen)(?:[-_\s]*\d+)?(?=$|\s)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PageDeckTiming:
    """Validated timing metadata for one page deck."""

    page_count: int
    mode: str = "default"
    default_seconds: float = DEFAULT_PAGE_SECONDS
    durations_seconds: tuple[float, ...] = ()
    transition: str = DEFAULT_PAGE_TRANSITION
    transition_seconds: float = DEFAULT_PAGE_TRANSITION_SECONDS

    def __post_init__(self) -> None:
        if self.page_count <= 0:
            raise ValueError("page_count must be positive")
        if self.mode not in {"default", "custom"}:
            raise ValueError("page timing mode must be default or custom")
        if self.transition not in PAGE_TRANSITIONS:
            raise ValueError(f"page transition must be one of {PAGE_TRANSITIONS}")
        # AUD-27602: NaN is neither <= 0 nor > 0, so plain sign checks let a NaN
        # duration through until round(NaN) raises deep in page_start_frame.
        # Validate finiteness alongside the sign.
        if not math.isfinite(self.default_seconds) or self.default_seconds <= 0:
            raise ValueError("default_seconds must be a positive finite number")
        if not math.isfinite(self.transition_seconds) or self.transition_seconds < 0:
            raise ValueError("transition_seconds must be a finite non-negative number")
        if self.mode == "custom":
            if len(self.durations_seconds) != self.page_count:
                raise ValueError("custom timing must provide one duration per page")
            if any(not math.isfinite(value) or value <= 0 for value in self.durations_seconds):
                raise ValueError("page durations must be positive finite numbers")

    @property
    def durations(self) -> tuple[float, ...]:
        if self.mode == "custom":
            return self.durations_seconds
        return (self.default_seconds,) * self.page_count

    @property
    def total_seconds(self) -> float:
        return sum(self.durations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_count": self.page_count,
            "mode": self.mode,
            "default_seconds": self.default_seconds,
            "durations_seconds": list(self.durations_seconds),
            "transition": self.transition,
            "transition_seconds": self.transition_seconds,
        }


def detect_page_count(document: str) -> int:
    """Count explicit page markers or conventional page/slide containers."""
    text = str(document or "")
    # Do not count CSS selectors such as ``[data-topos-page]`` or
    # ``[data-topos-page="1"]``.  The previous text-wide search treated those
    # selectors as real pages, so a six-page deck with page-specific CSS could
    # suddenly be timed as twelve pages and show repeated late frames.
    explicit = [
        match
        for match in _PAGE_START_RE.finditer(text)
        if re.search(
            rf"\b{re.escape(PAGE_MARKER_ATTR)}\s*=",
            match.group("attrs"),
            re.IGNORECASE,
        )
    ]
    if explicit:
        return max(1, len(explicit))
    count = 0
    for match in _PAGE_START_RE.finditer(text):
        class_match = _CLASS_RE.search(match.group("attrs"))
        if class_match and _PAGE_CLASS_RE.search(class_match.group("value")):
            count += 1
    return max(1, count)


def ensure_page_markers(document: str) -> str:
    """Add stable page markers to conventional page/slide containers.

    Existing ``data-topos-page`` markup is left untouched.  A document with no
    explicit/conventional page containers is considered a single-page deck and
    is returned byte-for-byte unchanged.
    """
    text = str(document or "")
    if not text or any(
        re.search(
            rf"\b{re.escape(PAGE_MARKER_ATTR)}\s*=",
            match.group("attrs"),
            re.IGNORECASE,
        )
        for match in _PAGE_START_RE.finditer(text)
    ):
        return text
    candidates: list[tuple[int, int]] = []
    for match in _PAGE_START_RE.finditer(text):
        class_match = _CLASS_RE.search(match.group("attrs"))
        if class_match and _PAGE_CLASS_RE.search(class_match.group("value")):
            candidates.append((match.start(), match.end()))
    if not candidates:
        return text
    # Apply from right to left so offsets remain valid.
    result = text
    for index, (start, end) in reversed(list(enumerate(candidates, start=1))):
        tag = result[start:end]
        close = tag.rfind(">")
        if close < 0:
            continue
        tag = tag[:close] + f' {PAGE_MARKER_ATTR}="{index}"' + tag[close:]
        result = result[:start] + tag + result[end:]
    return result


def _meta_tag_content(document: str, name: str) -> str:
    """Content of ``<meta name="...">`` with any attribute order; '' if absent."""
    tag = re.search(
        rf"<meta\b[^>]*\bname\s*=\s*([\"']){re.escape(name)}\1[^>]*>",
        str(document or ""),
        re.IGNORECASE,
    )
    if not tag:
        return ""
    content = re.search(r"\bcontent\s*=\s*([\"'])(.*?)\1", tag.group(0), re.IGNORECASE)
    return (content.group(2) if content else "").strip()


def parse_model_page_timing(
    document: str, *, page_count: int, total_seconds: float,
) -> PageDeckTiming:
    """Build page timing, honouring the model's optional authored rhythm.

    The model may declare per-page durations in its own document head::

        <meta name="topos-page-durations" content="2.4,3.6,5">

    and optionally a page transition::

        <meta name="topos-page-transition" content="crossfade">
        <meta name="topos-page-transition-seconds" content="0.4">

    The host still owns the clip's total length (the user asked for N
    seconds), so declared durations are scaled proportionally to
    ``total_seconds``: the model authors the *rhythm*, the timeline keeps the
    *length*.  Missing or inconsistent declarations fall back to an equal
    split with hard cuts, which remains the editor-adjustable default.
    """
    count = max(1, int(page_count))
    total = max(0.001, float(total_seconds))
    equal = max(0.001, total / count)

    durations: tuple[float, ...] = ()
    try:
        declared = [
            float(piece)
            for piece in re.split(r"[,\s]+", _meta_tag_content(document, "topos-page-durations"))
            if piece.strip()
        ]
    except ValueError:
        declared = []
    if declared and len(declared) == count and all(value > 0 for value in declared):
        factor = total / sum(declared)
        scaled = tuple(round(value * factor, 3) for value in declared)
        if all(value > 0 for value in scaled):
            durations = scaled

    transition = _meta_tag_content(document, "topos-page-transition").strip().lower()
    if transition not in PAGE_TRANSITIONS:
        transition = DEFAULT_PAGE_TRANSITION
    if transition == DEFAULT_PAGE_TRANSITION:
        transition_seconds = 0.0
    else:
        try:
            declared_seconds = float(
                _meta_tag_content(document, "topos-page-transition-seconds")
            )
        except ValueError:
            declared_seconds = DEFAULT_PAGE_TRANSITION_SECONDS
        transition_seconds = min(5.0, max(0.0, declared_seconds))

    return PageDeckTiming(
        count,
        "custom" if durations else "default",
        equal,
        durations,
        transition=transition,
        transition_seconds=transition_seconds,
    )


def _safe_page_float(value: Any, fallback: float) -> float:
    """Guarded float() for externally-persisted page-timing values.

    AUD-27601: conversion failures (``"abc"``, None, containers) used to raise
    ValueError straight out of the load/export chain.  AUD-27602: NaN/inf are
    truthy and fail every ``<= 0`` / ``> 0`` comparison, so they must be
    rejected by isfinite here rather than slipping into PageDeckTiming.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def page_timing_for(
    metadata: Mapping[str, Any] | None,
    *,
    page_count: int,
    fallback_default_seconds: float = DEFAULT_PAGE_SECONDS,
) -> PageDeckTiming:
    """Read metadata defensively, repairing old/missing layer values."""
    raw = metadata.get(PAGE_TIMING_KEY, {}) if metadata else {}
    raw = raw if isinstance(raw, Mapping) else {}
    # The HTML is authoritative; stale metadata from an edited document must
    # not make custom durations address the wrong number of pages.
    try:
        count = max(1, int(page_count or raw.get("page_count", 1)))
    except (TypeError, ValueError):
        count = 1
    # AUD-27601: same try/except discipline as transition_seconds below — a
    # corrupted value falls back instead of crashing scene load/export.
    default = _safe_page_float(raw.get("default_seconds"), fallback_default_seconds)
    if default <= 0:
        default = fallback_default_seconds
    mode = str(raw.get("mode", "default"))
    durations_raw = raw.get("durations_seconds", ())
    if isinstance(durations_raw, (list, tuple)):
        # Bad entries degrade to 0.0, which the custom-mode gate below rejects,
        # falling the deck back to default timing.
        durations = tuple(_safe_page_float(value, 0.0) for value in durations_raw)
    else:
        durations = ()
    if mode == "custom" and len(durations) == count and all(value > 0 for value in durations):
        mode_value = "custom"
    else:
        mode_value = "default"
    transition = str(raw.get("transition", DEFAULT_PAGE_TRANSITION)).strip().lower()
    if transition not in PAGE_TRANSITIONS:
        transition = DEFAULT_PAGE_TRANSITION
    try:
        transition_seconds = float(
            raw.get("transition_seconds", DEFAULT_PAGE_TRANSITION_SECONDS)
        )
    except (TypeError, ValueError):
        transition_seconds = DEFAULT_PAGE_TRANSITION_SECONDS
    transition_seconds = max(0.0, transition_seconds)
    return PageDeckTiming(
        count, mode_value, default, durations,
        transition=transition, transition_seconds=transition_seconds,
    )


def timing_metadata(timing: PageDeckTiming) -> dict[str, Any]:
    return {PAGE_TIMING_KEY: timing.to_dict()}


def page_index_at_frame(frame: int, fps: float, timing: PageDeckTiming) -> int:
    """Return the zero-based page active at a timeline frame."""
    elapsed = max(0.0, float(frame)) / max(0.001, float(fps))
    cursor = 0.0
    for index, duration in enumerate(timing.durations):
        cursor += duration
        if elapsed < cursor:
            return index
    return timing.page_count - 1


def page_start_frame(index: int, fps: float, timing: PageDeckTiming) -> int:
    """Return the timeline frame that activates a page.

    Page decks deliberately use one source of truth for both directions:
    ``page_index_at_frame`` maps playback/scrubbing to a page, while this
    helper maps the editor's previous/next/page-button actions back to the
    exact same boundary.  This is the host equivalent of pressing an arrow
    key in a web presentation; it never changes the model-authored HTML.
    """
    page = max(0, min(int(index), timing.page_count - 1))
    return max(0, round(sum(timing.durations[:page]) * max(0.001, float(fps))))


def _has_background_customization(
    global_background: Any,
    page_backgrounds: tuple[Any, ...] | list[Any] = (),
) -> bool:
    """True 当存在用户定制的背景（默认全局色 / 空 / inherit 视为未定制）。"""
    if str(global_background or "").strip().lower() != str(
            DEFAULT_GLOBAL_BACKGROUND).lower():
        return True
    for bg in page_backgrounds or ():
        value = str(bg or "").strip().lower()
        if value and value != "inherit":
            return True
    return False


def page_deck_runtime_html(
    document: str,
    timing: PageDeckTiming,
    *,
    global_background: Any = DEFAULT_GLOBAL_BACKGROUND,
    page_backgrounds: tuple[Any, ...] | list[Any] = (),
) -> str:
    """Wrap a sanitized document with trusted export navigation.

    The host-owned controller is appended only for explicit HTML export, where
    keyboard navigation is expected and useful. It does not alter the
    model-authored document or remove its browser code.
    """
    text = str(document or "").strip()
    if timing.page_count <= 1:
        # P1-RENDER-01：单页导出仅在确有背景定制时注入 host-owned 背景
        # 标记。默认背景 + 无逐页定制时，注入的 scaffolding 会以
        # !important 覆盖 authored 的 body 背景，破坏「导出 = authored
        # 来源」契约（多页 deck 的导航控制器是明确的导出特性，保留）。
        if not _has_background_customization(global_background, page_backgrounds):
            return text
        markup = _background_markup(global_background, page_backgrounds)
        if re.search(r"</body\s*>", text, re.IGNORECASE):
            return re.sub(r"</body\s*>", markup + "</body>", text, count=1, flags=re.IGNORECASE)
        return text + markup
    # Numeric values only; keep JSON syntax intact inside a script data block
    # (HTML entities are not decoded in <script> text).
    durations_json = json.dumps(list(timing.durations), separators=(",", ":"))
    controller = f"""
<style id="topos-page-deck-controller">
{_background_style(global_background)}
{_PAGE_LAYOUT_CSS}
{_PAGE_TRANSITION_CSS}
[data-topos-page][data-topos-hidden="1"]{{display:none!important;pointer-events:none!important}}
[data-topos-page].topos-page-active{{visibility:visible!important;pointer-events:auto!important}}
[data-topos-node-id].topos-node-selected{{outline:2px solid #ff8800!important;outline-offset:3px!important;cursor:pointer!important}}
</style>
<script>
(() => {{
  const pages = Array.from(document.querySelectorAll('[data-topos-page]'));
  if (!pages.length) return;
  const durations = {durations_json};
  const transitionName = "{timing.transition}";
  const transitionSeconds = {timing.transition_seconds:g};
  {_PAGE_VIEWPORT_RECOVERY_JS}
  let current = 0;
  let timer = 0;
  const clearTransition = (page) => {{
    page.removeAttribute('data-topos-transitioning');
    page.removeAttribute('data-topos-transition-role');
    page.removeAttribute('data-topos-transition');
    page.style.removeProperty('--topos-transition-progress');
    page.style.removeProperty('--topos-transition-duration');
  }};
  const prepareTransition = (page, role, progress) => {{
    page.setAttribute('data-topos-transitioning', '1');
    page.setAttribute('data-topos-transition-role', role);
    page.setAttribute('data-topos-transition', transitionName);
    page.style.setProperty('--topos-transition-duration', `${{transitionSeconds}}s`);
    page.style.setProperty('--topos-transition-progress', String(progress));
  }};
  const show = (next) => {{
    const previous = current;
    const target = Math.max(0, Math.min(pages.length - 1, next));
    current = target;
    const transitioning = previous !== target && transitionName !== 'cut' && transitionSeconds > 0;
    pages.forEach((page) => clearTransition(page));
    pages.forEach((page, index) => {{
      const active = index === current;
      const visibleForTransition = transitioning && index === previous;
      if (!active && !visibleForTransition) {{
        page.setAttribute('data-topos-hidden', '1');
      }} else {{
        page.removeAttribute('data-topos-hidden');
      }}
      page.classList.toggle('topos-page-active', active);
      // Compatibility with ordinary browser slide decks.  Many authored
      // pages use ``.slide.active`` to switch display/entrance animations;
      // exposing the same state keeps those pages visually identical after
      // the host takes ownership of keyboard/timeline navigation.
      page.classList.toggle('active', active);
      // Only supply a full-canvas containing block when the authored page has
      // no usable box after its own active state is applied.  This preserves
      // centered cards and custom page dimensions while keeping the common
      // A position:absolute page wrapper remains renderable when it has no
      // usable authored box.
      if (active || visibleForTransition) {{
        const rect = page.getBoundingClientRect();
        page.classList.toggle('topos-page-auto-box', rect.width < 1 || rect.height < 1 || transitioning);
      }}
    }});
    if (transitioning) {{
      prepareTransition(pages[previous], 'leave', 0);
      prepareTransition(pages[target], 'enter', 0);
      window.requestAnimationFrame(() => pages.forEach((page) =>
        page.style.setProperty('--topos-transition-progress', '1')));
    }}
    recoverPageViewport(pages[current]);
    window.dispatchEvent(new CustomEvent('topos-page-change', {{detail: {{index: current}}}}));
    window.clearTimeout(timer);
    timer = window.setTimeout(() => show((current + 1) % pages.length), (durations[current] || {timing.default_seconds:g}) * 1000);
  }};
  const move = (delta) => show(current + delta);
  document.addEventListener('keydown', (event) => {{
    if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(event.key)) {{ event.preventDefault(); move(1); }}
    if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key)) {{ event.preventDefault(); move(-1); }}
    if (event.key === 'Home') {{ event.preventDefault(); show(0); }}
    if (event.key === 'End') {{ event.preventDefault(); show(pages.length - 1); }}
  }});
  show(0);
}})();
</script>
{_background_script(global_background, page_backgrounds)}
"""
    if re.search(r"</body\s*>", text, re.IGNORECASE):
        return re.sub(r"</body\s*>", controller + "</body>", text, count=1, flags=re.IGNORECASE)
    return text + controller


def page_deck_editor_html(
    document: str,
    timing: PageDeckTiming,
    *,
    global_background: Any = DEFAULT_GLOBAL_BACKGROUND,
    page_backgrounds: tuple[Any, ...] | list[Any] = (),
) -> str:
    """Prepare the live editor document for interactive page navigation.

    The editor is a browser surface, not a rendered-video surface.  The model
    HTML/CSS remains the page content; this tiny host-owned controller only
    supplies page visibility, keyboard navigation, and a bridge callback for
    the editor UI.  Unlike :func:`page_deck_runtime_html`, it has no automatic
    timer—the Qt timeline/play control owns playback timing.
    """
    text = str(document or "").strip()
    durations_json = json.dumps(list(timing.durations), separators=(",", ":"))
    controller = f"""
<style id="topos-page-deck-editor-controller">
{_background_style(global_background)}
{_PAGE_LAYOUT_CSS}
{_PAGE_TRANSITION_CSS}
[data-topos-page][data-topos-hidden="1"]{{display:none!important;pointer-events:none!important}}
[data-topos-page].topos-page-active{{visibility:visible!important;pointer-events:auto!important}}
[data-topos-node-id].topos-node-selected{{outline:2px solid #ff8800!important;outline-offset:3px!important;cursor:pointer!important}}
</style>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<script id="topos-page-deck-editor-runtime">
(() => {{
  const pages = Array.from(document.querySelectorAll('[data-topos-page]'));
  const durations = {durations_json};
  const transitionName = "{timing.transition}";
  const transitionSeconds = {timing.transition_seconds:g};
  {_PAGE_VIEWPORT_RECOVERY_JS}
  let current = 0;
  let bridge = null;
  const clearTransition = (page) => {{
    page.removeAttribute('data-topos-transitioning');
    page.removeAttribute('data-topos-transition-role');
    page.removeAttribute('data-topos-transition');
    page.style.removeProperty('--topos-transition-progress');
    page.style.removeProperty('--topos-transition-duration');
  }};
  const prepareTransition = (page, role, progress) => {{
    page.setAttribute('data-topos-transitioning', '1');
    page.setAttribute('data-topos-transition-role', role);
    page.setAttribute('data-topos-transition', transitionName);
    page.style.setProperty('--topos-transition-duration', `${{transitionSeconds}}s`);
    page.style.setProperty('--topos-transition-progress', String(progress));
  }};
  const notify = (index) => {{
    if (bridge && bridge.notifyPageChanged) bridge.notifyPageChanged(index);
    window.dispatchEvent(new CustomEvent('topos-page-change', {{detail: {{index}}}}));
  }};
  const show = (next, animate = true) => {{
    if (!pages.length) return 0;
    const previous = current;
    current = Math.max(0, Math.min(pages.length - 1, Number(next) || 0));
    const transitioning = animate && previous !== current && transitionName !== 'cut' && transitionSeconds > 0;
    pages.forEach((page) => clearTransition(page));
    pages.forEach((page, index) => {{
      const active = index === current;
      const visibleForTransition = transitioning && index === previous;
      if (!active && !visibleForTransition) {{
        page.setAttribute('data-topos-hidden', '1');
      }} else {{
        page.removeAttribute('data-topos-hidden');
      }}
      page.classList.toggle('topos-page-active', active);
      page.classList.toggle('active', active);
      if (active || visibleForTransition) {{
        const rect = page.getBoundingClientRect();
        page.classList.toggle('topos-page-auto-box', rect.width < 1 || rect.height < 1 || transitioning);
      }}
    }});
    if (transitioning) {{
      prepareTransition(pages[previous], 'leave', 0);
      prepareTransition(pages[current], 'enter', 0);
      window.requestAnimationFrame(() => pages.forEach((page) =>
        page.style.setProperty('--topos-transition-progress', '1')));
    }}
    recoverPageViewport(pages[current]);
    notify(current);
    return current;
  }};
  window.toposPageDeck = {{
    pages,
    durations,
    transition: transitionName,
    transitionSeconds,
    show,
    next: () => show(current + 1),
    previous: () => show(current - 1),
    current: () => current,
  }};
  document.addEventListener('keydown', (event) => {{
    if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(event.key)) {{ event.preventDefault(); show(current + 1); }}
    if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key)) {{ event.preventDefault(); show(current - 1); }}
    if (event.key === 'Home') {{ event.preventDefault(); show(0); }}
    if (event.key === 'End') {{ event.preventDefault(); show(pages.length - 1); }}
  }});
  document.addEventListener('click', (event) => {{
    const node = event.target.closest && event.target.closest('[data-topos-node-id]');
    if (node && bridge && bridge.notifyElementSelected) bridge.notifyElementSelected(node.dataset.toposNodeId || '');
  }});
  if (window.QWebChannel && window.qt && qt.webChannelTransport) {{
    new QWebChannel(qt.webChannelTransport, (channel) => {{
      bridge = channel.objects.toposPageBridge;
      // The native overlay handlers (select / in-place edit / draw gestures)
      // call window.toposPageBridge directly; expose the same bridge object
      // so editor-owned DOM interaction reaches Python without going through
      // this closure.
      window.toposPageBridge = bridge;
      show(current, false);
    }});
  }} else {{
    show(0, false);
  }}
}})();
</script>
{_background_script(global_background, page_backgrounds)}
"""
    if re.search(r"</body\s*>", text, re.IGNORECASE):
        return re.sub(r"</body\s*>", controller + "</body>", text, count=1, flags=re.IGNORECASE)
    return text + controller


def page_deck_capture_html(
    document: str,
    timing: PageDeckTiming,
    *,
    global_background: Any = DEFAULT_GLOBAL_BACKGROUND,
    page_backgrounds: tuple[Any, ...] | list[Any] = (),
) -> str:
    """Inject CSS/runtime data used by the privileged deterministic seek bridge."""
    text = str(document or "").strip()
    duration_attr = _html.escape(json.dumps(list(timing.durations), separators=(",", ":")), quote=True)
    transition_attr = _html.escape(timing.transition, quote=True)
    transition_seconds = f"{timing.transition_seconds:g}"
    marker = (
        f'<meta data-topos-page-deck="1" data-topos-page-durations="{duration_attr}"'
        f' data-topos-page-transition="{transition_attr}"'
        f' data-topos-page-transition-seconds="{transition_seconds}">'
    )
    style = (
        '<style id="topos-page-deck-capture">'
        # P1-RENDER-01：仅在有背景定制时注入全局背景覆盖（见下方说明）
        + (_background_style(global_background)
           if _has_background_customization(global_background, page_backgrounds)
           else "")
        + _PAGE_LAYOUT_CSS
        + _PAGE_TRANSITION_CSS
        # Hidden pages must not remain in normal document flow.  Visibility
        # alone made page 3+ start below the preceding hidden pages; the
        # viewport-recovery fallback then translated the page only partly back
        # into view, leaving a conspicuous blank strip in exported video.
        + '[data-topos-page][data-topos-hidden="1"]{display:none!important;'
        'pointer-events:none!important}'
        '[data-topos-page][data-topos-active="1"]{opacity:1!important;'
        'visibility:visible!important;pointer-events:auto!important}'
        # The seek bridge writes the transition progress directly for each
        # frame.  Disabling the browser's own interpolation here makes
        # capture deterministic; the editor/export runtime keeps the animated
        # transition rule above for interactive playback.
        '[data-topos-page][data-topos-transitioning="1"]{transition:none!important}'
        '</style>'
    )
    # P1-RENDER-01/P1-UI-01：背景样式/脚本仅在确有背景定制时注入。
    # 未定制时注入的 html,body{background:...!important} 会覆盖 authored
    # 的文档背景（宿主 scaffolding 不得改变 authored 文档的呈现）。
    if _has_background_customization(global_background, page_backgrounds):
        background_script = _background_script(global_background, page_backgrounds)
    else:
        background_script = ""
    if re.search(r"</head\s*>", text, re.IGNORECASE):
        text = re.sub(r"</head\s*>", marker + style + "</head>", text, count=1, flags=re.IGNORECASE)
    else:
        # Keep <!doctype html> as the document's first token.  Prefixing the
        # trusted marker before a doctype makes Chromium enter quirks mode and
        # changes percentage/flex/grid layout in otherwise valid model HTML.
        html_open = re.search(r"<html\b[^>]*>", text, re.IGNORECASE)
        if html_open:
            insert_at = html_open.end()
            text = (
                text[:insert_at]
                + "<head>" + marker + style + "</head>"
                + text[insert_at:]
            )
        else:
            text = marker + style + text
    if background_script:
        if re.search(r"</body\s*>", text, re.IGNORECASE):
            return re.sub(
                r"</body\s*>", background_script + "</body>",
                text, count=1, flags=re.IGNORECASE,
            )
        return text + background_script
    return text


__all__ = [
    "DEFAULT_GLOBAL_BACKGROUND", "DEFAULT_PAGE_SECONDS", "DEFAULT_PAGE_TRANSITION",
    "DEFAULT_PAGE_TRANSITION_SECONDS", "PAGE_TRANSITIONS",
    "PAGE_MARKER_ATTR", "PAGE_TIMING_KEY", "safe_background_value",
    "scene_background_css",
    "PageDeckTiming", "detect_page_count", "ensure_page_markers",
    "page_timing_for", "timing_metadata", "page_index_at_frame",
    "page_start_frame", "parse_model_page_timing",
    "page_deck_runtime_html", "page_deck_editor_html", "page_deck_capture_html",
]
