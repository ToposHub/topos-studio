"""SG-07: structured graphics primitives — paths, shapes, connectors.

The plan (SG-07) calls for richer graphics than plain rects: rounded rects,
arrows, callouts and — most importantly — connectors whose route **recomputes
deterministically when the bound nodes move** ("流程图节点移动后连接线自动重算
且可动画").  The process-flow / timeline templates ship ad-hoc arrow rects;
this module factoring those into reusable, pure-data primitives so the same
shape definition renders identically across SVG/GPU/HTML and a connector can
be re-routed from node rectangles alone.

Everything here is pure data + pure functions: no rendering, no wall clock.
A :class:`Path` is an ordered list of SVG-style commands; ``to_svg_d()``
serialises it to the ``d`` attribute of an SVG ``<path>``.  Geometric boolean
operations (union/intersect) on arbitrary paths require a polygon-clipping
library and are intentionally NOT faked here — :func:`combine_paths` only
concatenates subpaths (compound path), which is enough for stroke/fill
rendering.  True booleans are left as a documented deferred capability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .layout import Rect


# ---------------------------------------------------------------------------
# Path model
# ---------------------------------------------------------------------------

class PathOp(str, Enum):
    MOVE = "M"
    LINE = "L"
    CURVE = "C"     # cubic bezier: 6 coords (x1 y1 x2 y2 x y)
    QUAD = "Q"      # quadratic bezier: 4 coords (x1 y1 x y)
    CLOSE = "Z"


# (op_value, coords) — coords is a flat tuple of floats whose length is
# implied by the op (M/L: 2, Q: 4, C: 6, Z: 0).
_Command = tuple[str, tuple[float, ...]]


def _fmt(value: float) -> str:
    """Format a coordinate without trailing zeros (3,0 -> "3"; 3.5 -> "3.5").

    AUD-27508：非有限坐标（NaN/±Inf，退化几何的典型产物）不得写入
    SVG path——``f"{nan:.3f}"`` 会产出 "nan"/"inf" 污染 path 数据、
    导致整段语法失效。降级为 "0" 保持 path 可解析，退化段由上游
    可视化兜底。
    """
    if not math.isfinite(value):
        return "0"
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text else "0"


@dataclass(frozen=True)
class Path:
    """An ordered list of SVG-style path commands."""

    commands: tuple[_Command, ...] = ()
    closed: bool = False

    def to_svg_d(self) -> str:
        """Serialise to an SVG ``d`` string (e.g. ``"M 0 0 L 10 10 Z"``)."""
        parts: list[str] = []
        for op, coords in self.commands:
            parts.append(op)
            parts.extend(_fmt(c) for c in coords)
        if self.closed and (not self.commands or self.commands[-1][0] != PathOp.CLOSE.value):
            parts.append(PathOp.CLOSE.value)
        return " ".join(parts)

    def translate(self, dx: float, dy: float) -> "Path":
        """Return a copy shifted by (dx, dy)."""
        moved: list[_Command] = []
        for op, coords in self.commands:
            if op in (PathOp.MOVE.value, PathOp.LINE.value):
                moved.append((op, (coords[0] + dx, coords[1] + dy)))
            elif op == PathOp.QUAD.value:
                moved.append((op, (coords[0] + dx, coords[1] + dy,
                                   coords[2] + dx, coords[3] + dy)))
            elif op == PathOp.CURVE.value:
                moved.append((op, tuple(c + (dx if i % 2 == 0 else dy) for i, c in enumerate(coords))))
            else:
                moved.append((op, coords))
        return Path(commands=tuple(moved), closed=self.closed)

    def bbox(self) -> Rect:
        """Axis-aligned bounding box over all command coordinates."""
        xs: list[float] = []
        ys: list[float] = []
        for _op, coords in self.commands:
            for i, value in enumerate(coords):
                (xs if i % 2 == 0 else ys).append(value)
        if not xs or not ys:
            return Rect(0.0, 0.0, 0.0, 0.0)
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        return Rect(x=x0, y=y0, width=x1 - x0, height=y1 - y0)


# ---------------------------------------------------------------------------
# Shape primitive builders (pure functions -> Path)
# ---------------------------------------------------------------------------

def rect_path(x: float, y: float, width: float, height: float) -> Path:
    """A closed rectangle path."""
    return Path(commands=(
        (PathOp.MOVE.value, (x, y)),
        (PathOp.LINE.value, (x + width, y)),
        (PathOp.LINE.value, (x + width, y + height)),
        (PathOp.LINE.value, (x, y + height)),
    ), closed=True)


def rounded_rect_path(x: float, y: float, width: float, height: float,
                      radius: float) -> Path:
    """A closed rounded-rectangle path using four cubic-bezier corners.

    The corner radius is clamped to half the shorter side so the shape stays
    valid for narrow rects.
    """
    w = max(0.0, width)
    h = max(0.0, height)
    r = max(0.0, min(radius, w / 2.0, h / 2.0))
    if r == 0.0:
        return rect_path(x, y, w, h)
    # cubic bezier corner handle offset (kappa for a circular approximation).
    k = r * 0.5522847498
    x0, y0 = x, y
    x1, y1 = x + w, y + h
    cmds: list[_Command] = [
        (PathOp.MOVE.value, (x0 + r, y0)),
        (PathOp.LINE.value, (x1 - r, y0)),
        (PathOp.CURVE.value, (x1 - r + k, y0, x1, y0 + r - k, x1, y0 + r)),
        (PathOp.LINE.value, (x1, y1 - r)),
        (PathOp.CURVE.value, (x1, y1 - r + k, x1 - r + k, y1, x1 - r, y1)),
        (PathOp.LINE.value, (x0 + r, y1)),
        (PathOp.CURVE.value, (x0 + r - k, y1, x0, y1 - r + k, x0, y1 - r)),
        (PathOp.LINE.value, (x0, y0 + r)),
        (PathOp.CURVE.value, (x0, y0 + r - k, x0 + r - k, y0, x0 + r, y0)),
    ]
    return Path(commands=tuple(cmds), closed=True)


def circle_path(cx: float, cy: float, radius: float) -> Path:
    """A circle approximated by four cubic-bezier quarters (kappa arcs)."""
    r = max(0.0, radius)
    if r == 0.0:
        return Path(commands=((PathOp.MOVE.value, (cx, cy)),), closed=True)
    k = r * 0.5522847498
    cmds: list[_Command] = [
        (PathOp.MOVE.value, (cx + r, cy)),
        (PathOp.CURVE.value, (cx + r, cy + k, cx + k, cy + r, cx, cy + r)),
        (PathOp.CURVE.value, (cx - k, cy + r, cx - r, cy + k, cx - r, cy)),
        (PathOp.CURVE.value, (cx - r, cy - k, cx - k, cy - r, cx, cy - r)),
        (PathOp.CURVE.value, (cx + k, cy - r, cx + r, cy - k, cx + r, cy)),
    ]
    return Path(commands=tuple(cmds), closed=True)


def polygon_path(points: Sequence[tuple[float, float]]) -> Path:
    """A closed polygon through the given points."""
    if not points:
        return Path()
    cmds: list[_Command] = [(PathOp.MOVE.value, (points[0][0], points[0][1]))]
    for px, py in points[1:]:
        cmds.append((PathOp.LINE.value, (px, py)))
    return Path(commands=tuple(cmds), closed=True)


def arrow_path(x1: float, y1: float, x2: float, y2: float,
               head_size: float = 12.0) -> Path:
    """A stroked arrow: shaft from (x1,y1)→(x2,y2) plus a V arrowhead.

    Rendered as one open path with a pen-up move before each head line, so a
    single ``<path stroke=...>`` draws the whole arrow.
    """
    import math

    hs = max(0.0, head_size)
    angle = math.atan2(y2 - y1, x2 - x1)
    if hs == 0.0:
        return Path(commands=((PathOp.MOVE.value, (x1, y1)), (PathOp.LINE.value, (x2, y2))))
    left = (x2 - hs * math.cos(angle - math.pi / 6.0), y2 - hs * math.sin(angle - math.pi / 6.0))
    right = (x2 - hs * math.cos(angle + math.pi / 6.0), y2 - hs * math.sin(angle + math.pi / 6.0))
    cmds: list[_Command] = [
        (PathOp.MOVE.value, (x1, y1)),
        (PathOp.LINE.value, (x2, y2)),
        (PathOp.MOVE.value, left),
        (PathOp.LINE.value, (x2, y2)),
        (PathOp.LINE.value, right),
    ]
    return Path(commands=tuple(cmds), closed=False)


# ---------------------------------------------------------------------------
# Connectors — the SG-07 "recompute when nodes move" primitive
# ---------------------------------------------------------------------------

class AnchorSide(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


class Routing(str, Enum):
    ORTHOGONAL = "orthogonal"   # elbow / Z-route
    STRAIGHT = "straight"       # direct line


def anchor_point(rect: Rect, side: AnchorSide) -> tuple[float, float]:
    """Midpoint of a rectangle's side — the connector attachment point."""
    if side == AnchorSide.LEFT:
        return (rect.x, rect.y + rect.height / 2.0)
    if side == AnchorSide.RIGHT:
        return (rect.x + rect.width, rect.y + rect.height / 2.0)
    if side == AnchorSide.TOP:
        return (rect.x + rect.width / 2.0, rect.y)
    return (rect.x + rect.width / 2.0, rect.y + rect.height)  # BOTTOM


def route_orthogonal(start: tuple[float, float], end: tuple[float, float]) -> Path:
    """A deterministic elbow route between two anchor points.

    Uses the midpoint X as the elbow column, which collapses to a straight
    line when both anchors share an X (same vertical alignment).
    """
    sx, sy = start
    ex, ey = end
    mid_x = (sx + ex) / 2.0
    if abs(sx - ex) < 0.5:                       # near-vertical → straight
        return Path(commands=((PathOp.MOVE.value, (sx, sy)), (PathOp.LINE.value, (ex, ey))))
    cmds: list[_Command] = [
        (PathOp.MOVE.value, (sx, sy)),
        (PathOp.LINE.value, (mid_x, sy)),
        (PathOp.LINE.value, (mid_x, ey)),
        (PathOp.LINE.value, (ex, ey)),
    ]
    return Path(commands=tuple(cmds), closed=False)


def route_straight(start: tuple[float, float], end: tuple[float, float]) -> Path:
    return Path(commands=((PathOp.MOVE.value, start), (PathOp.LINE.value, end)), closed=False)


@dataclass(frozen=True)
class Connector:
    """A connector bound to two nodes by id + anchor sides.

    The geometry is *derived* from the node rectangles via :meth:`path_for`,
    so when a node moves the caller simply recomputes from the new rects — no
    stale stored geometry.  This is the SG-07 contract: 连接线自动重算.
    """

    connector_id: str
    source_id: str
    target_id: str
    source_side: AnchorSide = AnchorSide.RIGHT
    target_side: AnchorSide = AnchorSide.LEFT
    routing: Routing = Routing.ORTHOGONAL

    def __post_init__(self) -> None:
        if not self.connector_id or not self.source_id or not self.target_id:
            raise ValueError("connector_id, source_id and target_id cannot be empty")
        if self.source_id == self.target_id:
            raise ValueError("a connector cannot link a node to itself")

    def path_for(self, source_rect: Rect, target_rect: Rect) -> Path:
        """Recompute the connector path from the (possibly moved) node rects."""
        start = anchor_point(source_rect, self.source_side)
        end = anchor_point(target_rect, self.target_side)
        if self.routing == Routing.STRAIGHT:
            return route_straight(start, end)
        return route_orthogonal(start, end)


def recompute_connectors(
    connectors: Sequence[Connector],
    rects_by_node: dict[str, Rect],
) -> dict[str, Path]:
    """Recompute every connector against a node-id → rect map.

    Pure function: given the current node rectangles, returns
    ``{connector_id: Path}``.  Missing nodes are skipped (and absent from the
    result) rather than raising — a deleted node simply drops its connectors.
    """
    result: dict[str, Path] = {}
    for conn in connectors:
        src = rects_by_node.get(conn.source_id)
        tgt = rects_by_node.get(conn.target_id)
        if src is None or tgt is None:
            continue
        result[conn.connector_id] = conn.path_for(src, tgt)
    return result


# ---------------------------------------------------------------------------
# Compound paths
# ---------------------------------------------------------------------------

def combine_paths(paths: Sequence[Path]) -> Path:
    """Concatenate several paths into one compound path (multiple subpaths).

    This is NOT a geometric boolean union: it only stitches the ``d`` data so
    a single ``<path>`` can render several closed outlines (e.g. a shape with
    a hole).  True boolean operations need a clipping library and are deferred.
    """
    cmds: list[_Command] = []
    for p in paths:
        cmds.extend(p.commands)
        if p.closed:
            cmds.append((PathOp.CLOSE.value, ()))
    return Path(commands=tuple(cmds), closed=False)  # closure baked into commands


__all__ = [
    "AnchorSide",
    "Connector",
    "Path",
    "PathOp",
    "Routing",
    "anchor_point",
    "arrow_path",
    "circle_path",
    "combine_paths",
    "polygon_path",
    "recompute_connectors",
    "rect_path",
    "rounded_rect_path",
    "route_orthogonal",
    "route_straight",
]
