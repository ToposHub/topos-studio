"""SG-05: deterministic text layout — wrapping, alignment, ellipsis.

The layout is a pure function of a (run, measurer); the default measurer is a
stable per-codepoint heuristic so CJK/Latin mixed wrapping is reproducible
across machines.  A real metrics backend is injected by passing a different
:class:`TextMeasurer` — that substitution is covered here too.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.text_layout import (
    LineBox, TextAlign, TextFragment, TextRun, TextSpan,
    cjk_aware_measurer, layout_text, measure_text_height,
)


def _para(text: str, **kw) -> TextRun:
    return TextRun(spans=(TextSpan(text, font_size=24.0),), **kw)


class TestWrapping:
    def test_short_text_is_one_line(self):
        boxes = layout_text(_para("hello", max_width=800.0))
        assert len(boxes) == 1
        assert boxes[0].width <= 800.0

    def test_long_text_wraps_within_max_width(self):
        boxes = layout_text(_para("one two three four five six seven eight nine ten",
                                  max_width=120.0))
        assert len(boxes) > 1
        assert all(b.width <= 120.0 + 0.1 for b in boxes)

    def test_cjk_wraps_per_character(self):
        # CJK breaks anywhere; a long run of CJK wraps even with no spaces.
        boxes = layout_text(_para("人工智能代理四步骤教学动画", max_width=96.0))
        assert len(boxes) > 1
        assert all(b.width <= 96.0 + 0.1 for b in boxes)

    def test_mixed_cjk_latin_wraps_stably(self):
        run = _para("Hello 世界 this is mixed 段落 content", max_width=160.0)
        b1 = layout_text(run)
        b2 = layout_text(run)
        assert [(f.text, round(f.x, 2), round(f.y, 2)) for box in b1 for f in box.fragments] == \
               [(f.text, round(f.x, 2), round(f.y, 2)) for box in b2 for f in box.fragments]

    def test_empty_paragraph_returns_no_lines(self):
        assert layout_text(TextRun(spans=(TextSpan(""),))) == ()
        assert layout_text(TextRun(spans=())) == ()

    def test_line_height_stacks_vertically(self):
        boxes = layout_text(_para("a b c d e f g h i j k l m n o p",
                                  max_width=100.0, line_height=1.5))
        ys = [b.y for b in boxes]
        assert ys == sorted(ys)
        assert all(b.height == pytest.approx(24.0 * 1.5) for b in boxes)


class TestAlignment:
    def test_left_starts_at_zero(self):
        boxes = layout_text(_para("short", max_width=400.0, align=TextAlign.LEFT))
        assert boxes[0].fragments[0].x == pytest.approx(0.0)

    def test_right_shifts_to_edge(self):
        boxes = layout_text(_para("short", max_width=400.0, align=TextAlign.RIGHT))
        last = boxes[0].fragments[-1]
        assert last.x + last.width == pytest.approx(400.0)

    def test_centers_between_edges(self):
        boxes = layout_text(_para("short", max_width=400.0, align=TextAlign.CENTER))
        first = boxes[0].fragments[0]
        last = boxes[0].fragments[-1]
        slack_left = first.x
        slack_right = 400.0 - (last.x + last.width)
        assert slack_left == pytest.approx(slack_right)

    def test_justify_fills_non_last_lines(self):
        boxes = layout_text(_para("one two three four five six", max_width=400.0,
                                  align=TextAlign.JUSTIFY))
        # All but the last line stretch to the full max_width.
        for box in boxes[:-1]:
            assert box.width == pytest.approx(400.0)


class TestEllipsis:
    def test_max_lines_truncates_with_ellipsis(self):
        run = TextRun(spans=(TextSpan("one two three four five six seven eight", 20.0),),
                      max_width=120.0, max_lines=1, ellipsis="\u2026")
        boxes = layout_text(run)
        assert len(boxes) == 1
        assert boxes[-1].fragments[-1].text == "\u2026"
        assert boxes[-1].width <= 120.0 + 0.1

    def test_no_max_lines_keeps_everything(self):
        run = _para("a b c d e f g h", max_width=80.0)  # max_lines default 0
        boxes = layout_text(run)
        # Nothing truncated; no ellipsis fragment introduced.
        assert all(f.text != "\u2026" for box in boxes for f in box.fragments)


class TestPluggableMeasurer:
    def test_custom_measurer_changes_widths(self):
        class Monospace:
            def measure(self, text, fs):
                return len(text) * fs  # every char exactly 1em

        run = TextRun(spans=(TextSpan("hello", 10.0),), max_width=100.0)
        box = layout_text(run, measurer=Monospace())[0]
        assert box.width == pytest.approx(50.0)  # 5 chars * 10px

    def test_default_measurer_is_deterministic(self):
        m = cjk_aware_measurer()
        assert m.measure("hello", 10.0) == m.measure("hello", 10.0)
        # CJK char measures wider than a latin char at the same size.
        assert m.measure("世", 10.0) > m.measure("a", 10.0)


class TestHeight:
    def test_height_is_sum_of_line_heights(self):
        run = _para("a b c d e f g h i j", max_width=80.0, line_height=1.2)
        boxes = layout_text(run)
        assert measure_text_height(run) == pytest.approx(sum(b.height for b in boxes))

    def test_empty_height_is_zero(self):
        assert measure_text_height(TextRun(spans=(TextSpan(""),))) == 0.0
