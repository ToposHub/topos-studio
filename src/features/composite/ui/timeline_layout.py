"""Timeline geometry + semantic style tokens + icon spec (P3 data, T24-T36/T76).

Pure-data module centralising every layout constant, semantic color token
and icon geometry the AE-style timeline paints. The plan (§16) is explicit
that the screenshot text spec is the *executable* requirement, not the
image — so this module captures §16.1–16.9 as testable data.

Why centralise:

* the P3 view rewrite (KeyframesPanel/TreeView/TimelineView) reads these
  constants instead of hard-coding hex codes and pixel counts, so the
  layout is adjustable in one place and testable off-screen (T36);
* the semantic-token map (§16.8) is the bridge between the plan's reference
  colors and the project's existing ``C.*`` palette — views resolve a token
  to a real color at paint time, leaving room for theme switching (T76);
* the icon geometry spec (§16.9) lets ``timeline_icons.py`` (a later batch)
  draw every AE-like icon with QPainter from the same source of truth,
  including the four state colors (normal/hover/checked/disabled) the plan
  requires.

Design rules:

* GUI-free. No QWidget/QPainter/QColor. Token resolution returns a hex
  string; the caller wraps it in QColor. This keeps the module unit-
  testable and reusable by the Graph Editor.
* One constant per spec line. Names mirror the plan's §3/§16 wording so a
  future maintainer can cross-reference.
* Backed by the existing ``COMPOSITE_KEYFRAMES_*`` style where possible:
  the resolver prefers a project ``C.*`` token, falling back to the §16.8
  reference hex only when no project token exists yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple


# ── §3.1 / §16.1 panel geometry ─────────────────────────────────────────

DEFAULT_DOCK_HEIGHT = 300
MIN_DOCK_HEIGHT = 180
TOOLBAR_HEIGHT = 32
RULER_HEIGHT = 30          # == KEYFRAMES_RULER_H in keyframes_model.py
ROW_HEIGHT = 24            # == KEYFRAMES_ROW_H
EXPRESSION_ROW_HEIGHT = 22
EXPRESSION_ERROR_ROW_HEIGHT = 32
FOOTER_HEIGHT = 36
LEFT_COLUMN_DEFAULT_WIDTH = 330
LEFT_COLUMN_MIN_WIDTH = 240
LEFT_RIGHT_DIVIDER = 1
KEYFRAME_DIAMOND_PX = 7
KEYFRAME_HIT_RADIUS_PX = 10
KEYFRAME_SELECTED_DIAMOND_PX = 9
PLAYHEAD_LINE_PX = 1
PLAYHEAD_TRIANGLE_PX = 8

# Default property/timeline balance. The previous 43/57 split made the Name
# column absorb a large empty region on wide docks; 32/68 keeps the AE-style
# controls close to the property labels while leaving the timeline readable.
DEFAULT_SPLIT_RATIO = (32, 68)
NARROW_PANEL_WIDTH = 900       # below this, left min = 310, right min = 420
NARROW_LEFT_MIN = 240
NARROW_RIGHT_MIN = 420


# ── §3.3 / §16.4 left-column widths ─────────────────────────────────────
# Fixed columns in left-to-right order. ``name`` is the canonical id used
# by the future ColumnModel (T75/T80); ``width`` is the default; ``min`` is
# the floor below which the column never shrinks.

@dataclass(frozen=True)
class ColumnSpec:
    name: str
    label: str
    width: int
    min_width: int = 0
    visible_by_default: bool = True
    resizable: bool = True


LEFT_COLUMNS: Tuple[ColumnSpec, ...] = (
    ColumnSpec("eye", "Eye", 22, min_width=20),
    # Optional AE layer switches stay available through the column model but
    # are collapsed by default; painting an empty 22 px cell for every row
    # creates the large gaps users see in a compact property list.
    ColumnSpec("solo", "Solo", 0, min_width=0),
    ColumnSpec("lock", "Lock", 20, min_width=18),
    ColumnSpec("shy", "Shy", 0, min_width=0),
    ColumnSpec("label", "Label", 8, min_width=8, resizable=False),
    ColumnSpec("index", "Index", 0, min_width=0,
               visible_by_default=False),
    ColumnSpec("name", "Name", 0, min_width=80),    # 0 = absorbs remainder
    # AE-style property controls.  Keep the keyframe and binding cells next
    # to the value column so a property reads as: name | ◆ | value | link.
    ColumnSpec("keyframe", "Key", 22, min_width=20,
               resizable=False),
    ColumnSpec("value", "Value", 64, min_width=44),
    ColumnSpec("bind", "Bind", 22, min_width=20,
               resizable=False),
    ColumnSpec("expr", "Expr", 18, min_width=0),
)


def left_column_widths(total_width: int) -> Dict[str, int]:
    """Resolve column widths for a given total left-panel width.

    The ``name`` column absorbs the remainder after every fixed column
    takes its default (or min, if space is tight). Returns a dict keyed by
    column name → pixel width. Used by the off-screen layout test (T36) to
    assert no two columns overlap.
    """
    # The old resolver returned ``name=min_width`` even when the fixed
    # columns had already consumed the available width.  At the real panel
    # width (232 px in the old dock) that made the geometry 270 px wide and
    # the painter consequently drew text on top of the following columns.
    # Resolve the complete row as one budget and *always* tile exactly
    # ``total_width``.  Optional AE switches collapse before the Name column
    # is allowed to become unusably narrow.
    total_width = max(0, int(total_width))
    fixed = [c for c in LEFT_COLUMNS if c.name != "name"]
    name_col = next(c for c in LEFT_COLUMNS if c.name == "name")

    defaults = {c.name: max(0, int(c.width)) for c in fixed}
    fixed_default = sum(defaults.values())
    if total_width >= fixed_default + name_col.min_width:
        defaults["name"] = total_width - fixed_default
        return defaults

    # At normal compact sizes retain all switches but shrink them to their
    # declared minima. The remaining width belongs to the Name column.
    mins = {c.name: max(0, int(c.min_width)) for c in fixed}
    fixed_min = sum(mins.values())
    if total_width >= fixed_min + name_col.min_width:
        mins["name"] = total_width - fixed_min
        return mins

    # Below the absolute compact budget, keep the controls users actually
    # need to identify/select a layer (Eye, Lock, Label, Name, Value) and
    # collapse secondary switches first. This is preferable to painting
    # outside the widget or letting a label overlap an icon.
    compact_priority = (
        # These are the controls that remain useful at the smallest supported
        # splitter width: identify the row, set a keyframe, edit its value,
        # and expose the parameter/layer pick-whip.
        "eye", "lock", "label", "keyframe", "value", "bind")
    widths = {c.name: 0 for c in fixed}
    remaining = total_width
    for name in compact_priority:
        requested = mins.get(name, 0)
        take = min(requested, remaining)
        widths[name] = take
        remaining -= take
    widths["name"] = min(name_col.min_width, remaining)
    remaining -= widths["name"]
    # Any extra pixels go to Name. If the widget is exceptionally narrow,
    # Name receives the remaining space (possibly zero) and still never
    # spills past the right edge.
    widths["name"] += max(0, remaining)
    return widths


def column_x_offsets(total_width: int) -> List[Tuple[str, int, int]]:
    """Return [(name, x_start, x_end), ...] in left-to-right paint order."""
    widths = left_column_widths(total_width)
    out: List[Tuple[str, int, int]] = []
    x = 0
    for col in LEFT_COLUMNS:
        w = widths[col.name]
        out.append((col.name, x, x + w))
        x += w
    return out


# ── §16.8 semantic color tokens ─────────────────────────────────────────
# Reference hex values from the plan's §16.8 table. The resolver maps each
# to a project C.* token where one exists; new tokens (grid_major, …) are
# added to the project palette in the P3 view batch. Views MUST call
# resolve_token() rather than reading these hexes directly.

REFERENCE_TOKENS: Dict[str, str] = {
    "timeline_bg": "#17181a",
    "timeline_bg_alt": "#1d1f22",
    "header_bg": "#24262a",
    # Both work areas share the same base tone; only layer header rows and
    # the intentional out-of-range shade add contrast.
    "left_panel_bg": "#1c1d20",
    # AE gives each layer/node header row a subtle lift while keeping the
    # shared left/right work-area backgrounds at the same base tone.
    "layer_row_bg": "#202124",
    "grid_major": "#303338",
    "grid_minor": "#24262a",
    "border": "#3a3c42",
    "text_primary": "#d4d7dc",
    "text_secondary": "#9ba0a8",
    "text_hint": "#666b73",
    "accent_blue": "#21a7ff",
    "accent_orange": "#e8872f",
    "error": "#e55353",
    "playhead": "#e53935",     # existing KEYFRAME_PLAYHEAD
    "keyframe_selected": "#ff8800",  # existing KEYFRAME_SELECTED/ORANGE
}


def _project_tokens() -> Mapping[str, str]:
    """Return the project's C.* token dict, lazily imported.

    Returns an empty mapping if the style module isn't importable (e.g. in
    a stripped-down test environment); callers fall back to REFERENCE_TOKENS.
    """
    try:
        from src.shared.ui.style_sheet import C
        # C is a namespace object; gather its UPPER-case hex attributes.
        return {k: v for k, v in vars(C).items()
                if isinstance(v, str) and v.startswith("#")}
    except Exception:
        return {}


# Map of semantic token → project C.* attribute name (where one exists).
# Tokens NOT in this map fall back to their REFERENCE_TOKENS hex until the
# project palette grows them (T76 tracks the migration).
_TOKEN_TO_PROJECT: Dict[str, str] = {
    "text_primary": "TEXT_PRIMARY",
    "text_secondary": "TEXT_SECONDARY",
    "text_hint": "TEXT_HINT",
    "border": "BORDER_MID",
    "error": "ERROR",
    "accent_blue": "ACCENT_BLUE",
    "accent_orange": "ORANGE",
    "playhead": "KEYFRAME_PLAYHEAD",
    "keyframe_selected": "KEYFRAME_SELECTED",
    "timeline_bg": "BG_PANEL",
    "timeline_bg_alt": "BG_PANEL_ALT",
    "header_bg": "BG_HEADER",
    "left_panel_bg": "KEYFRAMES_BG",
}


def resolve_token(name: str) -> str:
    """Resolve a semantic token to a hex string.

    Prefers the matching project C.* token (so theme changes flow through),
    falls back to the §16.8 reference hex. Never raises — an unknown token
    returns whatever ``text_secondary`` resolves to (so a typo inherits the
    project palette's secondary text color, not a hard-coded hex).
    """
    if name not in REFERENCE_TOKENS:
        # Unknown token: inherit text_secondary so the fallback respects the
        # active theme. We resolve text_secondary directly to avoid infinite
        # recursion (it's always in REFERENCE_TOKENS).
        return resolve_token("text_secondary")
    project_attr = _TOKEN_TO_PROJECT.get(name)
    if project_attr:
        project = _project_tokens()
        if project_attr in project:
            return project[project_attr]
    return REFERENCE_TOKENS[name]


# ── §3.5 / §16.6 keyframe shape spec ────────────────────────────────────
# Shapes must encode state, not color alone (plan §16.6 F1 — colorblind-safe).

@dataclass(frozen=True)
class KeyframeShapeSpec:
    """Geometry + fill/border for one keyframe visual state."""
    state: str  # "normal" | "selected" | "hover" | "hold" | "locked" | "expr_driven"
    shape: str  # "diamond" | "square" | "diamond_bar" | "none"
    size_px: int
    border_px: int
    fill_token: str   # semantic token for the fill
    border_token: str  # semantic token for the border


KEYFRAME_SHAPES: Tuple[KeyframeShapeSpec, ...] = (
    KeyframeShapeSpec("normal", "diamond", KEYFRAME_DIAMOND_PX, 1,
                      "accent_orange", "border"),
    KeyframeShapeSpec("selected", "diamond", KEYFRAME_SELECTED_DIAMOND_PX, 1,
                      "accent_orange", "text_primary"),
    KeyframeShapeSpec("hover", "diamond", KEYFRAME_SELECTED_DIAMOND_PX, 1,
                      "accent_orange", "text_primary"),
    KeyframeShapeSpec("hold", "square", KEYFRAME_DIAMOND_PX, 1,
                      "accent_orange", "border"),
    KeyframeShapeSpec("locked", "diamond", KEYFRAME_DIAMOND_PX, 1,
                      "text_hint", "text_hint"),
    KeyframeShapeSpec("expr_driven", "none", 0, 0, "timeline_bg_alt",
                      "timeline_bg_alt"),
)


def shape_for_state(state: str) -> KeyframeShapeSpec:
    for s in KEYFRAME_SHAPES:
        if s.state == state:
            return s
    return KEYFRAME_SHAPES[0]


# ── §16.9 icon geometry spec ─────────────────────────────────────────────
# Each icon the timeline needs, with its QPainter drawing steps as a short
# string the icon painter interprets. Four state colors (normal/hover/
# checked/disabled) come from the token resolver.

@dataclass(frozen=True)
class IconSpec:
    name: str
    icon_px: int = 14
    # Drawing recipe — interpreted by timeline_icons.py (later batch).
    # Kept as data so the icon set is enumerable + testable without QPainter.
    recipe: str = ""
    tooltip_key: str = ""  # i18n key for the tooltip


ICON_SPECS: Tuple[IconSpec, ...] = (
    IconSpec("diamond", recipe="polygon(0,-4)(4,0)(0,4)(-4,0)"),
    IconSpec("diamond_plus", recipe="diamond+plus(3px)"),
    IconSpec("play", recipe="triangle(left_flat,right_point)"),
    IconSpec("pause", recipe="two_bars(2px)"),
    IconSpec("fast_backward", recipe="triangle+left_edge"),
    IconSpec("fast_forward", recipe="triangle+right_edge"),
    IconSpec("step_backward", recipe="bar+triangle_left"),
    IconSpec("step_forward", recipe="triangle_right+bar"),
    IconSpec("lock", recipe="rect_body+arc_shackle"),
    IconSpec("lock_open", recipe="rect_body+broken_arc"),
    IconSpec("eye", recipe="ellipse+center_dot"),
    IconSpec("eye_off", recipe="ellipse+slash"),
    IconSpec("solo", recipe="filled_circle"),
    IconSpec("shy", recipe="eye+slash_variant"),
    IconSpec("expand", recipe="chevron_down(2_lines)"),
    IconSpec("collapse", recipe="chevron_up(2_lines)"),
    IconSpec("snap", recipe="magnet_U+two_short_bars"),
    IconSpec("expression", recipe="equals_two_lines"),
    IconSpec("expression_error", recipe="equals+corner_dot"),
    IconSpec("playhead", recipe="inverted_triangle(8px)+line"),
    IconSpec("bezier", recipe="two_dots+two_handle_lines"),
    IconSpec("fit", recipe="four_corner_arrows"),
    IconSpec("zoom_in", recipe="magnifier+plus"),
    IconSpec("zoom_out", recipe="magnifier+minus"),
    IconSpec("search", recipe="magnifier"),
    IconSpec("filter", recipe="funnel"),
    IconSpec("graph_editor", recipe="curve+axes"),
)


def icon_names() -> List[str]:
    return [s.name for s in ICON_SPECS]


# ── §3.2 toolbar button metrics ─────────────────────────────────────────

TOOLBAR_BTN_W = 24
TOOLBAR_BTN_H = 22
TOOLBAR_ICON_PX = 14
TOOLBAR_BTN_GAP = 3   # 2-4 px per spec; 3 is the midpoint


# ── §16.6 row painting state colors ─────────────────────────────────────

@dataclass(frozen=True)
class RowStateStyle:
    """Semantic paint recipe for one row visual state (§3.4)."""
    state: str  # "node" | "node_selected" | "param" | "param_selected" |
                # "locked" | "expression" | "expression_error"
    bg_token: str
    text_token: str


ROW_STATE_STYLES: Tuple[RowStateStyle, ...] = (
    RowStateStyle("node", "left_panel_bg", "text_secondary"),
    RowStateStyle("node_selected", "accent_blue", "text_primary"),
    RowStateStyle("param", "timeline_bg_alt", "text_secondary"),
    RowStateStyle("param_selected", "timeline_bg", "text_primary"),
    RowStateStyle("locked", "timeline_bg_alt", "text_hint"),
    RowStateStyle("expression", "timeline_bg_alt", "text_secondary"),
    RowStateStyle("expression_error", "error", "error"),
)


def row_style(state: str) -> RowStateStyle:
    for s in ROW_STATE_STYLES:
        if s.state == state:
            return s
    return ROW_STATE_STYLES[0]


__all__ = [
    # geometry
    "DEFAULT_DOCK_HEIGHT", "MIN_DOCK_HEIGHT", "TOOLBAR_HEIGHT",
    "RULER_HEIGHT", "ROW_HEIGHT", "EXPRESSION_ROW_HEIGHT",
    "EXPRESSION_ERROR_ROW_HEIGHT", "FOOTER_HEIGHT",
    "LEFT_COLUMN_DEFAULT_WIDTH", "LEFT_COLUMN_MIN_WIDTH",
    "LEFT_RIGHT_DIVIDER", "KEYFRAME_DIAMOND_PX", "KEYFRAME_HIT_RADIUS_PX",
    "KEYFRAME_SELECTED_DIAMOND_PX", "PLAYHEAD_LINE_PX",
    "PLAYHEAD_TRIANGLE_PX", "DEFAULT_SPLIT_RATIO", "NARROW_PANEL_WIDTH",
    "NARROW_LEFT_MIN", "NARROW_RIGHT_MIN", "TOOLBAR_BTN_W",
    "TOOLBAR_BTN_H", "TOOLBAR_ICON_PX", "TOOLBAR_BTN_GAP",
    # columns
    "ColumnSpec", "LEFT_COLUMNS", "left_column_widths",
    "column_x_offsets",
    # tokens
    "REFERENCE_TOKENS", "resolve_token",
    # shapes
    "KeyframeShapeSpec", "KEYFRAME_SHAPES", "shape_for_state",
    # icons
    "IconSpec", "ICON_SPECS", "icon_names",
    # rows
    "RowStateStyle", "ROW_STATE_STYLES", "row_style",
]
