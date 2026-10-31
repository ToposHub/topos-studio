"""Multi-select align/distribute geometry (UX-08 phase-3a) — Qt-free pure logic."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.model import SceneElement, SceneTransform
from src.features.teaching_animation.ui.align import (
    ALIGN_MODES, build_alignment_updates, compute_alignment,
)


def _el(eid: str, x: float, y: float, w: float = 100.0, h: float = 50.0) -> SceneElement:
    return SceneElement(
        element_id=eid, type="shape",
        transform=SceneTransform(x=x, y=y, width=w, height=h),
        style={}, content={"kind": "rect"})


class TestAlign:
    def test_align_left(self) -> None:
        out = compute_alignment([_el("a", 10, 0), _el("b", 200, 0)], "align_left")
        assert out["a"][0] == 10 and out["b"][0] == 10

    def test_align_right_different_widths(self) -> None:
        out = compute_alignment([_el("a", 10, 0, w=100), _el("b", 200, 0, w=50)], "align_right")
        # right edges: a=110, b=250 → max=250 → a.x=250-100=150, b.x=250-50=200
        assert out["a"][0] == 150 and out["b"][0] == 200

    def test_align_center_h(self) -> None:
        out = compute_alignment([_el("a", 0, 0, w=100), _el("b", 300, 0, w=100)], "align_center_h")
        # selection box: left=0, right=400 → center=200 → x = 200-50 = 150 each
        assert out["a"][0] == 150 and out["b"][0] == 150

    def test_align_top(self) -> None:
        out = compute_alignment([_el("a", 0, 10), _el("b", 0, 200)], "align_top")
        assert out["a"][1] == 10 and out["b"][1] == 10

    def test_align_bottom_different_heights(self) -> None:
        out = compute_alignment([_el("a", 0, 0, h=50), _el("b", 0, 200, h=100)], "align_bottom")
        # bottoms: a=50, b=300 → max=300 → a.y=300-50=250, b.y=300-100=200
        assert out["a"][1] == 250 and out["b"][1] == 200

    def test_align_middle_v(self) -> None:
        out = compute_alignment([_el("a", 0, 0, h=100), _el("b", 0, 300, h=100)], "align_middle_v")
        # box top=0, bottom=400 → center=200 → y = 200-50 = 150 each
        assert out["a"][1] == 150 and out["b"][1] == 150

    def test_preserves_other_axis(self) -> None:
        out = compute_alignment([_el("a", 10, 77), _el("b", 200, 99)], "align_left")
        assert out["a"][1] == 77 and out["b"][1] == 99  # y unchanged


class TestDistribute:
    def test_distribute_h_evenly_spaces_centers(self) -> None:
        els = [_el("a", 0, 0), _el("b", 100, 0), _el("c", 500, 0)]  # w=100
        out = compute_alignment(els, "distribute_h")
        # centers: 50, 150, 550 → first=50 last=550 step=250 → 50, 300, 550
        # x = center - 50 → 0, 250, 500
        assert out["a"][0] == 0
        assert out["b"][0] == 250
        assert out["c"][0] == 500

    def test_distribute_v(self) -> None:
        els = [_el("a", 0, 0, h=50), _el("b", 0, 100, h=50), _el("c", 0, 500, h=50)]
        out = compute_alignment(els, "distribute_v")
        # centers y: 25, 125, 525 → step 250 → 25, 275, 525 → y = center - 25
        assert out["a"][1] == 0
        assert out["b"][1] == 250
        assert out["c"][1] == 500


class TestEdgeCases:
    def test_fewer_than_two_returns_empty(self) -> None:
        assert compute_alignment([_el("a", 0, 0)], "align_left") == {}
        assert compute_alignment([], "align_left") == {}

    def test_unknown_mode_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_alignment([_el("a", 0, 0), _el("b", 1, 1)], "align_diagonal")

    def test_all_modes_are_declared(self) -> None:
        # sanity: the modes exercised here are all declared
        for m in ("align_left", "align_right", "align_center_h", "align_top",
                  "align_bottom", "align_middle_v", "distribute_h", "distribute_v"):
            assert m in ALIGN_MODES


class TestBuildUpdates:
    def test_omits_noop_and_produces_transforms(self) -> None:
        # a is already at the min x (0); align_left is a no-op for a.
        els = [_el("a", 0, 0), _el("b", 200, 0)]
        updates = build_alignment_updates(els, "align_left")
        assert "a" not in updates            # no-op omitted
        assert updates["b"]["transform"].x == 0
        # width/height preserved
        assert updates["b"]["transform"].width == 100
        assert updates["b"]["transform"].height == 50
