"""SG-05: deterministic text layout — wrap, align, ellipsis.

The plan calls for a typography model with span-level rich text, line height,
alignment, automatic wrapping, ellipsis and **deterministic** font metrics so
CJK/Latin mixed wrapping is stable across preview and export (SG-05
acceptance: "CJK/英文混排换行稳定；缺字/字体替换可诊断").

Real glyph metrics need a font-measurement backend (FreeType / Qt metrics),
which the headless core does not link.  This module therefore defines the
layout algorithm as a pure function of a *pluggable* :class:`TextMeasurer`: the
default :func:`cjk_aware_measurer` is a deterministic per-codepoint heuristic
(CJK = 1.0 em, space = 0.28 em, other = 0.55 em) that makes wrapping stable
and testable today; a real metrics backend is injected later by passing a
different measurer.  Missing-glyph diagnostics stay the job of
:class:`~src.features.teaching_animation.core.font_registry.FontRegistry`
(coverage + fallback chains), which composes with this layer without coupling.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, Sequence


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class TextAlign(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


@dataclass(frozen=True)
class TextSpan:
    """One styled run of text inside a paragraph."""

    text: str
    font_size: float = 24.0
    color: str = "#ffffff"
    weight: str = "regular"
    letter_spacing: float = 0.0      # em, added per character


@dataclass(frozen=True)
class TextRun:
    """A paragraph: ordered spans + layout parameters."""

    spans: tuple[TextSpan, ...] = ()
    max_width: float = 800.0
    line_height: float = 1.3         # multiplier on the max font size in a line
    align: TextAlign = TextAlign.LEFT
    max_lines: int = 0               # 0 = unlimited
    ellipsis: str = "\u2026"

    def __post_init__(self) -> None:
        if self.max_width <= 0.0:
            raise ValueError("max_width must be positive")
        if self.line_height <= 0.0:
            raise ValueError("line_height must be positive")
        if self.max_lines < 0:
            raise ValueError("max_lines cannot be negative")


@dataclass(frozen=True)
class TextFragment:
    """A positioned piece of a span on a laid-out line."""

    text: str
    x: float                         # offset within the line box
    y: float                         # baseline of the line
    width: float
    font_size: float
    color: str
    weight: str


@dataclass(frozen=True)
class LineBox:
    """One laid-out line: its fragments and box geometry."""

    fragments: tuple[TextFragment, ...]
    y: float                         # top of the line box
    height: float
    width: float                     # actual content width (<= max_width)


# ---------------------------------------------------------------------------
# Measurer
# ---------------------------------------------------------------------------

class TextMeasurer(Protocol):
    """Width of ``text`` at ``font_size`` in design pixels."""

    def measure(self, text: str, font_size: float) -> float: ...


def _is_cjk(codepoint: int) -> bool:
    """Common CJK + full-width ranges (deterministic, table-free)."""
    return (
        0x1100 <= codepoint <= 0x115F or      # Hangul Jamo
        0x2E80 <= codepoint <= 0x303F or      # CJK radicals / punctuation
        0x3040 <= codepoint <= 0x30FF or      # Hiragana / Katakana
        0x3300 <= codepoint <= 0x4DBF or      # CJK ext + compat
        0x4E00 <= codepoint <= 0x9FFF or      # CJK Unified Ideographs
        0xAC00 <= codepoint <= 0xD7AF or      # Hangul syllables
        0xF900 <= codepoint <= 0xFAFF or      # CJK compat ideographs
        0xFF00 <= codepoint <= 0xFFEF         # Fullwidth / Halfwidth
    )


def cjk_aware_measurer() -> TextMeasurer:
    """Deterministic default measurer.

    Per-codepoint width in em units: CJK/full-width = 1.0, space = 0.28, other
    (latin/digit/punct) = 0.55.  Stable across machines; a real metrics backend
    can replace this by passing a different :class:`TextMeasurer`.
    """
    return _FunctionMeasurer(_cjk_aware_measure)


def _cjk_aware_measure(text: str, font_size: float) -> float:
    width_em = 0.0
    for ch in text:
        cp = ord(ch)
        if _is_cjk(cp):
            width_em += 1.0
        elif ch.isspace():
            width_em += 0.28
        else:
            width_em += 0.55
    return width_em * font_size


class _FunctionMeasurer:
    """Wrap a ``measure(text, font_size) -> float`` callable as a TextMeasurer."""

    def __init__(self, fn: Callable[[str, float], float]) -> None:
        self._fn = fn

    def measure(self, text: str, font_size: float) -> float:
        return self._fn(text, font_size)


# ---------------------------------------------------------------------------
# Internal: break units
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Unit:
    text: str
    width: float
    font_size: float
    color: str
    weight: str
    breakable_before: bool
    is_space: bool


def _span_to_units(span: TextSpan, measurer: TextMeasurer) -> list[_Unit]:
    """Split a span into layout units (words / CJK chars / spaces)."""
    units: list[_Unit] = []
    buf: list[str] = []
    fs = span.font_size

    def flush_word() -> None:
        if not buf:
            return
        word = "".join(buf)
        units.append(_Unit(word, measurer.measure(word, fs) + span.letter_spacing * fs * len(word),
                           fs, span.color, span.weight, breakable_before=True, is_space=False))
        buf.clear()

    for ch in span.text:
        cp = ord(ch)
        if ch.isspace():
            flush_word()
            units.append(_Unit(ch, measurer.measure(ch, fs), fs, span.color, span.weight,
                               breakable_before=False, is_space=True))
        elif _is_cjk(cp):
            flush_word()
            units.append(_Unit(ch, measurer.measure(ch, fs) + span.letter_spacing * fs,
                               fs, span.color, span.weight, breakable_before=True, is_space=False))
        else:
            buf.append(ch)
    flush_word()
    return units


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def layout_text(
    run: TextRun,
    *,
    measurer: TextMeasurer | None = None,
    origin_y: float = 0.0,
) -> tuple[LineBox, ...]:
    """Greedy-wrap a paragraph into line boxes.

    Pure function of (run, measurer).  Returns an empty tuple for an empty
    paragraph.
    """
    if not run.spans or not any(span.text for span in run.spans):
        return ()
    measure = measurer or cjk_aware_measurer()

    # Flatten spans into units.
    units: list[_Unit] = []
    for span in run.spans:
        units.extend(_span_to_units(span, measure))
    # Drop leading spaces on the whole paragraph.
    while units and units[0].is_space:
        units.pop(0)

    # Greedy line fill.
    raw_lines: list[list[_Unit]] = []
    current: list[_Unit] = []
    current_w = 0.0
    for unit in units:
        would_wrap = current and current_w + unit.width > run.max_width + 1e-6 and unit.breakable_before
        if would_wrap:
            # drop trailing space on the line we're closing
            while current and current[-1].is_space:
                removed = current.pop()
                current_w -= removed.width
            raw_lines.append(current)
            current = []
            current_w = 0.0
            if unit.is_space:
                # skip a space that would start a new line
                continue
        current.append(unit)
        current_w += unit.width
    while current and current[-1].is_space:
        current.pop()
    if current:
        raw_lines.append(current)

    # max_lines + ellipsis truncation.
    if run.max_lines and len(raw_lines) > run.max_lines:
        kept = raw_lines[: run.max_lines]
        # 截断预留按截断前行的最大字号（保守方向：预留只多不少）。
        pre_fs = _line_font_size(kept[-1])
        kept[-1] = _truncate_line_for_ellipsis(
            kept[-1], run.max_width,
            measure.measure(run.ellipsis, pre_fs or 24.0),
        )
        # append the ellipsis as a synthetic unit on the last line
        # AUD-27801: 合成省略号单元的宽度必须按它自己携带的 font_size
        # 测量——截断可能删掉携带最大字号的单元，截断前行字号不再等于
        # 截断后行字号；宽度与 font_size 不一致会让渲染端按字号重排时
        # 产生位置/预留偏差。截断后行为空时回退截断前字号，保持省略号
        # 视觉字号稳定。
        fs = _line_font_size(kept[-1]) or pre_fs or 24.0
        ellipsis_w = measure.measure(run.ellipsis, fs)
        kept[-1].append(_Unit(run.ellipsis, ellipsis_w, fs, kept[-1][-1].color if kept[-1] else "#ffffff",
                              "regular", breakable_before=False, is_space=False))
        raw_lines = kept

    # Build line boxes with alignment + y stacking.
    boxes: list[LineBox] = []
    cursor_y = origin_y
    for line_units in raw_lines:
        fs = _line_font_size(line_units)
        height = fs * run.line_height
        # Compute fragment x offsets and content width.
        x = 0.0
        fragments: list[TextFragment] = []
        for unit in line_units:
            fragments.append(TextFragment(unit.text, x, cursor_y + fs, unit.width,
                                          unit.font_size, unit.color, unit.weight))
            x += unit.width
        content_w = x
        # Horizontal alignment.
        slack = max(0.0, run.max_width - content_w)
        if run.align == TextAlign.CENTER:
            shift = slack / 2.0
        elif run.align == TextAlign.RIGHT:
            shift = slack
        else:
            shift = 0.0
        if shift:
            fragments = [TextFragment(f.text, f.x + shift, f.y, f.width, f.font_size,
                                      f.color, f.weight) for f in fragments]
        # JUSTIFY: spread slack across non-last lines' inter-word gaps.
        if run.align == TextAlign.JUSTIFY and slack > 0.0 and line_units is not raw_lines[-1]:
            gaps = sum(1 for u in line_units if u.is_space)
            if gaps:
                extra = slack / gaps
                running = 0.0
                justified: list[TextFragment] = []
                for frag, unit in zip(fragments, line_units):
                    justified.append(TextFragment(frag.text, frag.x + running, frag.y, frag.width,
                                                  frag.font_size, frag.color, frag.weight))
                    if unit.is_space:
                        running += extra
                fragments = justified
                content_w = run.max_width
        boxes.append(LineBox(fragments=tuple(fragments), y=cursor_y, height=height,
                             width=content_w))
        cursor_y += height
    return tuple(boxes)


def _line_font_size(units: Sequence[_Unit]) -> float:
    return max((u.font_size for u in units), default=0.0)


def _truncate_line_for_ellipsis(
    units: list[_Unit], max_width: float, ellipsis_w: float,
) -> list[_Unit]:
    """Drop trailing units until the remaining fit alongside the ellipsis."""
    keep = list(units)
    while keep and sum(u.width for u in keep) + ellipsis_w > max_width + 1e-6:
        keep.pop()
    # also drop a trailing space so the ellipsis hugs the text
    while keep and keep[-1].is_space:
        keep.pop()
    return keep


def measure_text_height(
    run: TextRun,
    *,
    measurer: TextMeasurer | None = None,
) -> float:
    """Convenience: total laid-out paragraph height (0 if empty)."""
    boxes = layout_text(run, measurer=measurer)
    if not boxes:
        return 0.0
    return sum(b.height for b in boxes)


__all__ = [
    "LineBox",
    "TextAlign",
    "TextFragment",
    "TextMeasurer",
    "TextRun",
    "TextSpan",
    "cjk_aware_measurer",
    "layout_text",
    "measure_text_height",
]
