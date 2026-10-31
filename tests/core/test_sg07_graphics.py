"""SG-07: structured graphics primitives — paths, shapes, connectors.

Pure-logic coverage for the path model, the shape builders, and the
connector recompute contract ("流程图节点移动后连接线自动重算且可动画").
"""

from __future__ import annotations

import math

import pytest

from src.features.teaching_animation.core import (
    AnchorSide, Connector, Path, PathOp, Routing, Rect,
    anchor_point, arrow_path, circle_path, combine_paths, polygon_path,
    recompute_connectors, rect_path, rounded_rect_path, route_orthogonal,
)


class TestPathModel:
    def test_rect_path_serialises_to_svg_d(self):
        p = rect_path(0.0, 0.0, 10.0, 20.0)
        d = p.to_svg_d()
        assert d.startswith("M 0 0")
        assert d.endswith("Z")
        assert "L 10 0" in d and "L 10 20" in d

    def test_translate_shifts_all_points(self):
        p = rect_path(0.0, 0.0, 10.0, 10.0)
        moved = p.translate(5.0, -3.0)
        assert moved.bbox().x == pytest.approx(5.0)
        assert moved.bbox().y == pytest.approx(-3.0)

    def test_bbox_of_polygon(self):
        p = polygon_path([(1.0, 1.0), (4.0, 1.0), (4.0, 5.0)])
        bb = p.bbox()
        assert (bb.x, bb.y, bb.width, bb.height) == (1.0, 1.0, 3.0, 4.0)

    def test_empty_path_is_empty(self):
        assert Path().to_svg_d() == ""

    def test_number_formatting_strips_trailing_zeros(self):
        p = Path(commands=((PathOp.MOVE.value, (3.0, 3.5)),))
        assert p.to_svg_d() == "M 3 3.5"


class TestShapePrimitives:
    def test_rounded_rect_clamps_radius_to_half_side(self):
        # A very large radius must not exceed half the shorter side.
        p = rounded_rect_path(0.0, 0.0, 10.0, 10.0, radius=100.0)
        # Still produces a valid closed path with curve commands.
        assert PathOp.CURVE.value in p.to_svg_d()
        assert p.closed

    def test_zero_radius_falls_back_to_rect(self):
        p = rounded_rect_path(0.0, 0.0, 10.0, 10.0, radius=0.0)
        assert PathOp.CURVE.value not in p.to_svg_d()
        assert p.closed

    def test_circle_path_has_four_curves(self):
        p = circle_path(5.0, 5.0, 3.0)
        curves = [c for c in p.commands if c[0] == PathOp.CURVE.value]
        assert len(curves) == 4

    def test_arrow_head_size_zero_is_shaft_only(self):
        p = arrow_path(0.0, 0.0, 10.0, 0.0, head_size=0.0)
        # 1 move + 1 line, no arrowhead.
        assert len(p.commands) == 2

    def test_arrow_points_along_direction(self):
        p = arrow_path(0.0, 0.0, 10.0, 0.0, head_size=6.0)
        # The shaft line ends at the tip (10, 0).
        lines = [c for c in p.commands if c[0] == PathOp.LINE.value]
        assert lines[0][1] == (10.0, 0.0)
        # The arrowhead has two head lines meeting at the tip.
        assert len(lines) == 3


class TestConnectors:
    def test_anchor_points_are_side_midpoints(self):
        r = Rect(0.0, 0.0, 10.0, 10.0)
        assert anchor_point(r, AnchorSide.LEFT) == (0.0, 5.0)
        assert anchor_point(r, AnchorSide.RIGHT) == (10.0, 5.0)
        assert anchor_point(r, AnchorSide.TOP) == (5.0, 0.0)
        assert anchor_point(r, AnchorSide.BOTTOM) == (5.0, 10.0)

    def test_connector_rejects_self_loop_and_empty_ids(self):
        with pytest.raises(ValueError):
            Connector("c", "a", "a")
        with pytest.raises(ValueError):
            Connector("", "a", "b")
        with pytest.raises(ValueError):
            Connector("c", "a", "")

    def test_orthogonal_route_is_straight_when_aligned(self):
        # Same X → degenerates to a straight line (2 commands).
        p = route_orthogonal((5.0, 0.0), (5.0, 20.0))
        assert len(p.commands) == 2

    def test_orthogonal_route_uses_midpoint_elbow(self):
        p = route_orthogonal((0.0, 0.0), (20.0, 10.0))
        # M start → L mid,sy → L mid,ey → L end.
        assert len(p.commands) == 4
        # The elbow column is the midpoint X = 10.
        elbow_x = p.commands[1][1][0]
        assert elbow_x == pytest.approx(10.0)

    def test_connector_recomputes_when_node_moves(self):
        """The SG-07 contract: moving a node recomputes the connector path."""
        conn = Connector("c1", "a", "b",
                         source_side=AnchorSide.RIGHT, target_side=AnchorSide.LEFT)
        a = Rect(0.0, 0.0, 40.0, 40.0)
        b = Rect(100.0, 0.0, 40.0, 40.0)
        path_before = conn.path_for(a, b)
        # Move node b down by 50px.
        b_moved = Rect(100.0, 50.0, 40.0, 40.0)
        path_after = conn.path_for(a, b_moved)
        assert path_before.to_svg_d() != path_after.to_svg_d()
        # The new path now reaches y=70 (b's vertical centre after the move).
        assert "70" in path_after.to_svg_d()

    def test_recompute_connectors_skips_missing_nodes(self):
        conn = Connector("c1", "a", "b")
        rects = {"a": Rect(0.0, 0.0, 10.0, 10.0)}  # b is gone
        assert recompute_connectors([conn], rects) == {}

    def test_recompute_connectors_reroutes_all(self):
        c1 = Connector("c1", "a", "b")
        c2 = Connector("c2", "b", "c")
        rects = {"a": Rect(0.0, 0.0, 10.0, 10.0),
                 "b": Rect(50.0, 0.0, 10.0, 10.0),
                 "c": Rect(100.0, 0.0, 10.0, 10.0)}
        out = recompute_connectors([c1, c2], rects)
        assert set(out) == {"c1", "c2"}
        assert all(isinstance(p, Path) for p in out.values())

    def test_straight_routing(self):
        conn = Connector("c1", "a", "b", routing=Routing.STRAIGHT)
        p = conn.path_for(Rect(0.0, 0.0, 10.0, 10.0), Rect(50.0, 0.0, 10.0, 10.0))
        assert len(p.commands) == 2  # just M + L


class TestCompoundPath:
    def test_combine_concatenates_subpaths(self):
        a = rect_path(0.0, 0.0, 10.0, 10.0)
        b = rect_path(20.0, 20.0, 5.0, 5.0)
        compound = combine_paths([a, b])
        d = compound.to_svg_d()
        # Two subpaths, each closed, separated by another M.
        assert d.count("M") == 2
        assert d.count("Z") == 2


# ── AUD-27508：非有限坐标不得污染 SVG path ───────────────────────────────


def test_path_to_svg_d_rejects_non_finite_coordinates():
    """NaN/±Inf 坐标降级为 "0"，path 语法保持有效。

    历史实现 f"{nan:.3f}" 产出 "nan"/"inf"，整段 d 属性解析失效。
    """
    import math

    from src.features.teaching_animation.core.graphics import Path, PathOp

    path = Path(commands=(
        (PathOp.MOVE.value, (float("nan"), float("inf"))),
        (PathOp.LINE.value, (-math.inf, 5.0)),
    ))
    d = path.to_svg_d()

    assert "nan" not in d and "inf" not in d
    assert d == "M 0 0 L 0 5"
