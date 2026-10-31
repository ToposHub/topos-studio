"""W3 Auto Layout: stack/grid/fill/align/safe-area (SG-03, UX-12)."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.layout import (
    AlignMode, AutoLayout, LayoutChild, LayoutDirection, Rect, SizingMode,
    resolve_grid, resolve_stack,
)


def _child(cid: str, w: float = 100.0, h: float = 50.0, *, sizing=SizingMode.HUG) -> LayoutChild:
    return LayoutChild(cid, sizing=sizing, preferred_width=w, preferred_height=h)


class TestVerticalStack:
    def test_hug_children_stacked_with_gap(self) -> None:
        canvas = Rect(0.0, 0.0, 400.0, 400.0)
        layout = AutoLayout(direction=LayoutDirection.VERTICAL, padding=(10, 10, 10, 10), gap=8.0)
        rects = resolve_stack([_child("a", 100, 50), _child("b", 100, 50)], canvas, layout)
        assert rects["a"].y == 10.0
        assert rects["b"].y == 10.0 + 50.0 + 8.0  # padding + height + gap
        assert rects["a"].x == 10.0

    def test_fill_children_share_leftover(self) -> None:
        canvas = Rect(0.0, 0.0, 100.0, 300.0)
        layout = AutoLayout(direction=LayoutDirection.VERTICAL, gap=0.0)
        children = [_child("fixed", 100, 100), LayoutChild("fill1", sizing=SizingMode.FILL),
                    LayoutChild("fill2", sizing=SizingMode.FILL)]
        rects = resolve_stack(children, canvas, layout)
        # 300 - 100 fixed = 200 leftover, split into 100 each
        assert rects["fixed"].height == 100.0
        assert rects["fill1"].height == pytest.approx(100.0)
        assert rects["fill2"].height == pytest.approx(100.0)

    def test_align_center_cross_axis(self) -> None:
        canvas = Rect(0.0, 0.0, 400.0, 200.0)
        layout = AutoLayout(direction=LayoutDirection.VERTICAL, align=AlignMode.CENTER)
        rects = resolve_stack([_child("a", 100, 40)], canvas, layout)
        # centered horizontally in 400-wide content: (400-100)/2 = 150
        assert rects["a"].x == pytest.approx(150.0)

    def test_stretch_fills_cross_axis(self) -> None:
        canvas = Rect(0.0, 0.0, 400.0, 200.0)
        layout = AutoLayout(direction=LayoutDirection.VERTICAL, align=AlignMode.STRETCH)
        rects = resolve_stack([_child("a", 100, 40)], canvas, layout)
        assert rects["a"].width == pytest.approx(400.0)

    def test_safe_area_insets_applied(self) -> None:
        canvas = Rect(0.0, 0.0, 1920.0, 1080.0)
        layout = AutoLayout(direction=LayoutDirection.VERTICAL, safe_area=(80.0, 60.0, 80.0, 60.0), padding=(0, 0, 0, 0))
        rects = resolve_stack([_child("a", 100, 50)], canvas, layout)
        assert rects["a"].x == 80.0
        assert rects["a"].y == 60.0


class TestHorizontalStack:
    def test_horizontal_placement(self) -> None:
        canvas = Rect(0.0, 0.0, 400.0, 100.0)
        layout = AutoLayout(direction=LayoutDirection.HORIZONTAL, padding=(10, 10, 10, 10), gap=12.0)
        rects = resolve_stack([_child("a", 80, 50), _child("b", 80, 50)], canvas, layout)
        assert rects["a"].x == 10.0
        assert rects["b"].x == 10.0 + 80.0 + 12.0
        assert rects["a"].height == 50.0


class TestGrid:
    def test_uniform_cells(self) -> None:
        canvas = Rect(0.0, 0.0, 300.0, 200.0)
        kids = [_child(f"c{i}", 1, 1) for i in range(6)]
        rects = resolve_grid(kids, canvas, columns=3, gap=10.0)
        assert len(rects) == 6
        # cell width = (300 - 2*10)/3 = 93.33
        assert rects["c0"].width == pytest.approx((300.0 - 20.0) / 3.0, rel=1e-3)
        # second row starts lower
        assert rects["c3"].y > rects["c0"].y

    def test_invalid_columns(self) -> None:
        with pytest.raises(ValueError):
            resolve_grid([_child("a")], Rect(0, 0, 10, 10), columns=0)


class TestValidation:
    def test_negative_gap_rejected(self) -> None:
        with pytest.raises(ValueError):
            AutoLayout(gap=-1.0)

    def test_negative_padding_rejected(self) -> None:
        with pytest.raises(ValueError):
            AutoLayout(padding=(-1, 0, 0, 0))

    def test_empty_children_returns_empty(self) -> None:
        assert resolve_stack([], Rect(0, 0, 10, 10), AutoLayout()) == {}
