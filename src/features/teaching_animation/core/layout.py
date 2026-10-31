"""SG-03 / UX-12: Auto Layout — deterministic stack/grid layout resolver.

Auto Layout turns a set of children with sizing intent (hug / fill / fixed) into
concrete rectangles on a canvas, honouring padding, gap, alignment and safe-area.
It runs BEFORE animation evaluation, so an author (or AI) can say "four cards in
a vertical stack, 16px gap, centred, safe-area aware" and the layout recomputes
deterministically when the canvas changes (16:9 → 9:16).

This is the structured-scene half of the Figma-style layout contract (UX-12).
The HTML/Flex half (FH-13) lives behind the W0-A gate; this structured resolver
is fully active because it never touches a browser.

``SceneLayout`` (below) persists layout intent on a scene via ``scene.metadata``
(no schema-version bump — same pattern as Hierarchy) so the evaluator can re-run
the resolver every frame against the live canvas: change the canvas 16:9→9:16
and the group re-flows.  Elements opt in by being listed in the layout's
``child_ids``; everything else keeps its absolute transform byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class LayoutDirection(str, Enum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


class SizingMode(str, Enum):
    HUG = "hug"       # size to content (preferred_size)
    FILL = "fill"     # expand to fill available main-axis space
    FIXED = "fixed"   # use fixed_size


class AlignMode(str, Enum):
    START = "start"
    CENTER = "center"
    END = "end"
    STRETCH = "stretch"


@dataclass(frozen=True)
class Rect:
    """A resolved layout rectangle in design pixels."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if self.width < 0.0 or self.height < 0.0:
            raise ValueError("rect width/height cannot be negative")

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class LayoutChild:
    """One child to lay out, with its sizing intent and preferred (hug) size."""

    child_id: str
    sizing: SizingMode = SizingMode.HUG
    fixed_width: float = 0.0
    fixed_height: float = 0.0
    preferred_width: float = 0.0   # used by HUG
    preferred_height: float = 0.0
    min_width: float = 0.0
    min_height: float = 0.0

    def __post_init__(self) -> None:
        if not self.child_id:
            raise ValueError("child_id cannot be empty")

    def resolved_main(self, direction: LayoutDirection) -> float:
        if self.sizing == SizingMode.FIXED:
            return self.fixed_width if direction == LayoutDirection.HORIZONTAL else self.fixed_height
        return self.preferred_width if direction == LayoutDirection.HORIZONTAL else self.preferred_height

    def resolved_cross(self, direction: LayoutDirection) -> float:
        if self.sizing == SizingMode.FIXED:
            return self.fixed_height if direction == LayoutDirection.HORIZONTAL else self.fixed_width
        return self.preferred_height if direction == LayoutDirection.HORIZONTAL else self.preferred_width


@dataclass(frozen=True)
class AutoLayout:
    """Stack/grid layout config applied to a parent rect."""

    direction: LayoutDirection = LayoutDirection.VERTICAL
    padding: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # left, top, right, bottom
    gap: float = 0.0
    align: AlignMode = AlignMode.START
    safe_area: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # insets from canvas edge

    def __post_init__(self) -> None:
        if len(self.padding) != 4:
            raise ValueError("padding must be a 4-tuple (left,top,right,bottom)")
        if len(self.safe_area) != 4:
            raise ValueError("safe_area must be a 4-tuple")
        if self.gap < 0.0:
            raise ValueError("gap must be non-negative")
        if any(p < 0.0 for p in self.padding):
            raise ValueError("padding must be non-negative")
        if any(s < 0.0 for s in self.safe_area):
            raise ValueError("safe_area must be non-negative")

    def content_rect(self, canvas: Rect) -> Rect:
        """Inner rect after safe-area insets + padding."""
        pl, pt, pr, pb = self.padding
        sl, st, sr, sb = self.safe_area
        x = canvas.x + sl + pl
        y = canvas.y + st + pt
        width = max(0.0, canvas.width - sl - sr - pl - pr)
        height = max(0.0, canvas.height - st - sb - pt - pb)
        return Rect(x=x, y=y, width=width, height=height)


def _cross_position(cross_size: float, available: float, align: AlignMode) -> float:
    """Offset within the cross axis for a child of ``cross_size``."""
    if align == AlignMode.START:
        return 0.0
    if align == AlignMode.CENTER:
        return max(0.0, (available - cross_size) / 2.0)
    if align == AlignMode.END:
        return max(0.0, available - cross_size)
    return 0.0  # STRETCH handled by caller (sizing)


def resolve_stack(children: Sequence[LayoutChild], canvas: Rect, layout: AutoLayout) -> dict[str, Rect]:
    """Resolve a 1D stack layout to a {child_id: Rect} map.

    FILL children share the remaining main-axis space after HUG/FIXED children
    and gaps.  STRETCH alignment makes a child fill the cross axis.  The result
    is deterministic and a pure function of (children, canvas, layout).
    """
    if not children:
        return {}
    direction = layout.direction
    content = layout.content_rect(canvas)
    horizontal = direction == LayoutDirection.HORIZONTAL

    # Resolve base main sizes; collect FILL children for leftover distribution.
    base_main: dict[str, float] = {}
    fill_ids: list[str] = []
    used_main = 0.0
    for index, child in enumerate(children):
        if index > 0:
            used_main += layout.gap
        if child.sizing == SizingMode.FILL:
            fill_ids.append(child.child_id)
            base_main[child.child_id] = 0.0
        else:
            size = max(child.resolved_main(direction), 0.0)
            base_main[child.child_id] = size
            used_main += size

    available_main = (content.width if horizontal else content.height)
    leftover = max(0.0, available_main - used_main)
    per_fill = (leftover / len(fill_ids)) if fill_ids else 0.0
    for fid in fill_ids:
        # honour min size for fill children
        child = next(c for c in children if c.child_id == fid)
        min_main = child.min_width if horizontal else child.min_height
        base_main[fid] = max(per_fill, min_main)

    # Place along main axis; resolve cross axis + alignment.
    result: dict[str, Rect] = {}
    cursor = content.x if horizontal else content.y
    cross_available = content.height if horizontal else content.width
    for child in children:
        main_size = base_main[child.child_id]
        if layout.align == AlignMode.STRETCH:
            cross_size = cross_available
        else:
            cross_size = max(child.resolved_cross(direction), 0.0)
        cross_off = _cross_position(cross_size, cross_available, layout.align)
        if horizontal:
            result[child.child_id] = Rect(x=cursor, y=content.y + cross_off, width=main_size, height=cross_size)
        else:
            result[child.child_id] = Rect(x=content.x + cross_off, y=cursor, width=cross_size, height=main_size)
        cursor += main_size + layout.gap
    return result


def resolve_grid(
    children: Sequence[LayoutChild],
    canvas: Rect,
    *,
    columns: int,
    rows: int = 0,
    gap: float = 0.0,
    padding: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
    safe_area: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
) -> dict[str, Rect]:
    """Resolve a fixed columns×rows grid (uniform cells)."""
    if columns <= 0:
        raise ValueError("grid columns must be positive")
    if rows < 0:
        raise ValueError("grid rows must be non-negative")
    if len(children) == 0:
        return {}
    pl, pt, pr, pb = padding
    sl, st, sr, sb = safe_area
    inner_x = canvas.x + sl + pl
    inner_y = canvas.y + st + pt
    inner_w = max(0.0, canvas.width - sl - sr - pl - pr)
    inner_h = max(0.0, canvas.height - st - sb - pt - pb)
    actual_rows = rows if rows > 0 else max(1, (len(children) + columns - 1) // columns)
    cell_w = max(0.0, (inner_w - gap * (columns - 1)) / columns)
    cell_h = max(0.0, (inner_h - gap * (actual_rows - 1)) / actual_rows)
    result: dict[str, Rect] = {}
    for index, child in enumerate(children):
        col = index % columns
        row = index // columns
        if rows and row >= rows:
            break
        result[child.child_id] = Rect(
            x=inner_x + col * (cell_w + gap),
            y=inner_y + row * (cell_h + gap),
            width=cell_w,
            height=cell_h,
        )
    return result


def distribute_spacing(positions: Sequence[float], canvas: Rect, axis: str = "x") -> tuple[float, ...]:
    """Return per-item offsets that evenly distribute items along an axis.

    Helper for align/distribute UI; deterministic given positions.
    """
    if not positions:
        return ()
    span = max(positions) - min(positions) if len(positions) > 1 else 0.0
    extent = canvas.width if axis == "x" else canvas.height
    if span <= 0.0 or extent <= span:
        return tuple(0.0 for _ in positions)
    offset = (extent - span) / 2.0 - min(positions)
    return tuple(offset for _ in positions)


# --------------------------------------------------------------------------- #
# SceneLayout — persistent layout intent (SG-03 render-time pass)
# --------------------------------------------------------------------------- #

_SCENE_LAYOUT_KEY = "scene_layout"


@dataclass(frozen=True)
class ChildSizing:
    """Per-child sizing override for a laid-out element."""

    sizing: SizingMode = SizingMode.HUG
    fixed_width: float = 0.0
    fixed_height: float = 0.0
    min_width: float = 0.0
    min_height: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sizing": self.sizing.value, "fixed_width": self.fixed_width,
            "fixed_height": self.fixed_height, "min_width": self.min_width,
            "min_height": self.min_height,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ChildSizing":
        data = data or {}
        try:
            sizing = SizingMode(str(data.get("sizing", "hug")))
        except ValueError:
            sizing = SizingMode.HUG
        return cls(
            sizing=sizing,
            fixed_width=float(data.get("fixed_width", 0.0) or 0.0),
            fixed_height=float(data.get("fixed_height", 0.0) or 0.0),
            min_width=float(data.get("min_width", 0.0) or 0.0),
            min_height=float(data.get("min_height", 0.0) or 0.0),
        )


@dataclass(frozen=True)
class SceneLayout:
    """Persistent layout intent for a group of elements (SG-03).

    Stored under ``scene.metadata["scene_layout"]`` so the evaluator re-runs the
    resolver every frame against the live canvas.  ``mode="stack"`` uses
    ``layout`` (an :class:`AutoLayout`); ``mode="grid"`` uses the grid params.
    Only ``child_ids`` elements are repositioned; all other elements keep their
    absolute transform.  ``preferred_size`` for HUG children is the element's own
    transform width/height; ``sizing`` overrides let one child FILL or be FIXED.
    """

    mode: str = "stack"                          # "stack" | "grid"
    child_ids: tuple[str, ...] = ()
    layout: AutoLayout = field(default_factory=AutoLayout)
    sizing: Mapping[str, ChildSizing] = field(default_factory=dict)
    # Grid-only parameters.
    columns: int = 1
    rows: int = 0
    grid_gap: float = 0.0
    grid_padding: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    grid_safe_area: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        if self.mode not in {"stack", "grid"}:
            raise ValueError(f"unknown SceneLayout mode: {self.mode}")
        if self.mode == "grid" and self.columns <= 0:
            raise ValueError("grid columns must be positive")
        if not self.child_ids:
            raise ValueError("SceneLayout child_ids cannot be empty")

    def resolve(
        self,
        canvas_width: float,
        canvas_height: float,
        preferred_sizes: Mapping[str, tuple[float, float]],
    ) -> dict[str, Rect]:
        """Resolve this layout to a {child_id: Rect} map against the canvas.

        ``preferred_sizes`` maps child_id → (preferred_width, preferred_height)
        (typically the element's own transform dimensions, used for HUG sizing).
        Children not present in ``preferred_sizes`` default to (0, 0).
        """
        children: list[LayoutChild] = []
        for cid in self.child_ids:
            pw, ph = preferred_sizes.get(cid, (0.0, 0.0))
            ov = self.sizing.get(cid)
            if ov is not None:
                children.append(LayoutChild(
                    child_id=cid, sizing=ov.sizing,
                    fixed_width=ov.fixed_width, fixed_height=ov.fixed_height,
                    preferred_width=pw, preferred_height=ph,
                    min_width=ov.min_width, min_height=ov.min_height))
            else:
                children.append(LayoutChild(
                    child_id=cid, preferred_width=pw, preferred_height=ph))
        canvas = Rect(x=0.0, y=0.0, width=canvas_width, height=canvas_height)
        if self.mode == "grid":
            return resolve_grid(
                children, canvas, columns=self.columns, rows=self.rows,
                gap=self.grid_gap, padding=self.grid_padding, safe_area=self.grid_safe_area)
        return resolve_stack(children, canvas, self.layout)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "child_ids": list(self.child_ids),
            "layout": _auto_layout_to_dict(self.layout),
            "sizing": {k: v.to_dict() for k, v in self.sizing.items()},
            "columns": self.columns, "rows": self.rows, "grid_gap": self.grid_gap,
            "grid_padding": list(self.grid_padding), "grid_safe_area": list(self.grid_safe_area),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SceneLayout":
        data = data or {}
        layout_raw = data.get("layout") or {}
        sizing_raw = data.get("sizing") or {}
        return cls(
            mode=str(data.get("mode", "stack")),
            child_ids=tuple(str(c) for c in (data.get("child_ids") or [])),
            layout=_auto_layout_from_dict(layout_raw),
            sizing={str(k): ChildSizing.from_dict(v) for k, v in sizing_raw.items()},  # type: ignore[union-attr]
            columns=int(data.get("columns", 1) or 1),
            rows=int(data.get("rows", 0) or 0),
            grid_gap=float(data.get("grid_gap", 0.0) or 0.0),
            grid_padding=tuple(float(v) for v in (data.get("grid_padding") or (0.0, 0.0, 0.0, 0.0))),
            grid_safe_area=tuple(float(v) for v in (data.get("grid_safe_area") or (0.0, 0.0, 0.0, 0.0))),
        )


def _auto_layout_to_dict(layout: AutoLayout) -> dict[str, Any]:
    return {
        "direction": layout.direction.value, "padding": list(layout.padding),
        "gap": layout.gap, "align": layout.align.value, "safe_area": list(layout.safe_area),
    }


def _auto_layout_from_dict(data: Mapping[str, Any]) -> AutoLayout:
    data = data or {}
    try:
        direction = LayoutDirection(str(data.get("direction", "vertical")))
    except ValueError:
        direction = LayoutDirection.VERTICAL
    try:
        align = AlignMode(str(data.get("align", "start")))
    except ValueError:
        align = AlignMode.START
    return AutoLayout(
        direction=direction, align=align, gap=float(data.get("gap", 0.0) or 0.0),
        padding=tuple(float(v) for v in (data.get("padding") or (0.0, 0.0, 0.0, 0.0))),
        safe_area=tuple(float(v) for v in (data.get("safe_area") or (0.0, 0.0, 0.0, 0.0))),
    )


def read_scene_layout(metadata: Mapping[str, Any]) -> SceneLayout | None:
    """Read a SceneLayout from scene.metadata, or None when no layout is set."""
    raw = metadata.get(_SCENE_LAYOUT_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    try:
        return SceneLayout.from_dict(raw)
    except Exception:  # noqa: BLE001 - a malformed layout must never break rendering
        return None


def write_scene_layout_into(metadata: Mapping[str, Any], layout: SceneLayout) -> dict[str, Any]:
    """Return a new metadata dict with the SceneLayout stored under its key."""
    new = dict(metadata) if isinstance(metadata, Mapping) else {}
    new[_SCENE_LAYOUT_KEY] = layout.to_dict()
    return new


__all__ = [
    "AlignMode",
    "AutoLayout",
    "ChildSizing",
    "LayoutChild",
    "LayoutDirection",
    "Rect",
    "SceneLayout",
    "SizingMode",
    "distribute_spacing",
    "read_scene_layout",
    "resolve_grid",
    "resolve_stack",
    "write_scene_layout_into",
]
