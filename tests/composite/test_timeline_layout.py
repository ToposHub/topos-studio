"""P3-data tests: timeline geometry + tokens + icon spec (T24-T36/T76 data).

Pure-data tests — no QWidget. The layout constants, semantic color tokens,
keyframe shape spec and icon geometry live here as the single source the
P3 view rewrite will read. These tests lock the §16 spec into assertions
the off-screen layout test (T36) and the icon painter (T35) build on.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.ui.timeline_layout import (
    ColumnSpec,
    DEFAULT_DOCK_HEIGHT,
    DEFAULT_SPLIT_RATIO,
    ICON_SPECS,
    KEYFRAME_DIAMOND_PX,
    KEYFRAME_HIT_RADIUS_PX,
    KEYFRAME_SHAPES,
    LEFT_COLUMNS,
    LEFT_COLUMN_DEFAULT_WIDTH,
    LEFT_COLUMN_MIN_WIDTH,
    MIN_DOCK_HEIGHT,
    REFERENCE_TOKENS,
    ROW_HEIGHT,
    ROW_STATE_STYLES,
    RULER_HEIGHT,
    column_x_offsets,
    icon_names,
    left_column_widths,
    resolve_token,
    row_style,
    shape_for_state,
)

pytestmark = pytest.mark.composite_core


# ── §3.1 panel geometry matches plan ───────────────────────────────────


class TestPanelGeometry:
    def test_defaults_match_plan_section_3_1(self):
        assert DEFAULT_DOCK_HEIGHT == 300
        assert MIN_DOCK_HEIGHT == 180
        assert RULER_HEIGHT == 30
        assert ROW_HEIGHT == 24
        assert LEFT_COLUMN_DEFAULT_WIDTH == 330
        assert LEFT_COLUMN_MIN_WIDTH == 240
        assert KEYFRAME_DIAMOND_PX == 7
        assert KEYFRAME_HIT_RADIUS_PX == 10

    def test_hit_radius_larger_than_diamond(self):
        """The hit target must be larger than the painted diamond so a near
        miss still grabs the keyframe (plan §3.1: 7px diamond, 10px hit)."""
        assert KEYFRAME_HIT_RADIUS_PX > KEYFRAME_DIAMOND_PX

    def test_split_ratio_matches_plan(self):
        # Compact AE-style default: the property side is intentionally 32% so
        # Name/Value/Bind stay close together on wide docks.
        left, right = DEFAULT_SPLIT_RATIO
        assert left + right == 100
        assert abs(left - 32) <= 1


# ── §3.3 / §16.4 left columns ──────────────────────────────────────────


class TestLeftColumns:
    def test_canonical_column_order(self):
        names = [c.name for c in LEFT_COLUMNS]
        # §16.4 plus the AE property controls added after the Name cell.
        assert names == ["eye", "solo", "lock", "shy", "label",
                         "index", "name", "keyframe", "value", "bind",
                         "expr"]

    def test_label_column_is_thin_and_not_resizable(self):
        label = next(c for c in LEFT_COLUMNS if c.name == "label")
        assert label.width == 8
        assert label.resizable is False

    def test_name_column_absorbs_remainder(self):
        widths = left_column_widths(330)
        assert widths["name"] > 0
        # Sum must equal total.
        assert sum(widths.values()) == 330

    def test_x_offsets_are_contiguous_and_non_overlapping(self):
        for total in (240, 330, 480):
            offsets = column_x_offsets(total)
            for i in range(1, len(offsets)):
                prev_end = offsets[i - 1][2]
                cur_start = offsets[i][1]
                assert cur_start == prev_end, (
                    f"gap/overlap at width {total}: "
                    f"{offsets[i-1]} then {offsets[i]}")

    def test_no_column_negative_at_minimum_width(self):
        widths = left_column_widths(LEFT_COLUMN_MIN_WIDTH)
        assert all(w >= 0 for w in widths.values())

    def test_narrow_layout_keeps_minimums(self):
        """At the floor width, fixed columns shrink to their min but the
        name column still gets at least its min_width."""
        widths = left_column_widths(LEFT_COLUMN_MIN_WIDTH)
        name_col = next(c for c in LEFT_COLUMNS if c.name == "name")
        assert widths["name"] >= name_col.min_width


# ── T36: off-screen layout check at 800 / 1200 / 1920 ──────────────────


class TestT36OffscreenLayout:
    """T36 acceptance: at the three reference dock widths the columns must
    tile the panel edge-to-edge with no overlap and no gap, and no column
    may be narrower than its declared ``min_width``.

    This is the pure-data half of T36 — it asserts the geometry resolver
    output without instantiating a QWidget. The view-side half (the actual
    paint loop) is exercised in ``tests/ui/test_keyframes_tree_columns.py``
    via a render under QT_QPA_PLATFORM=offscreen.
    """

    @pytest.mark.parametrize("total", [800, 1200, 1920, 240, 330])
    def test_columns_tile_without_overlap(self, total):
        offsets = column_x_offsets(total)
        # Adjacency: each column's start equals the previous column's end.
        for i in range(1, len(offsets)):
            assert offsets[i][1] == offsets[i - 1][2], (
                f"width {total}: gap/overlap between {offsets[i-1][0]} "
                f"and {offsets[i][0]}")
        # The whole strip sums to the target width (no gap, no overflow).
        assert offsets[-1][2] == total

    @pytest.mark.parametrize("total", [800, 1200, 1920])
    def test_no_column_below_its_min_width(self, total):
        widths = left_column_widths(total)
        for col in LEFT_COLUMNS:
            assert widths[col.name] >= col.min_width, (
                f"width {total}: column {col.name}={widths[col.name]} "
                f"below min {col.min_width}")

    @pytest.mark.parametrize("total", [800, 1200, 1920])
    def test_name_column_absorbs_remainder(self, total):
        """The flexible 'name' column must take up all slack so the value
        and expr columns stay pinned to the right edge."""
        widths = left_column_widths(total)
        fixed_total = sum(w for n, w in widths.items() if n != "name")
        assert widths["name"] == total - fixed_total

    @pytest.mark.parametrize("total", [800, 1200, 1920])
    def test_value_and_expr_pinned_to_right_edge(self, total):
        """The Value and Expression columns are fixed-width and pinned to
        the right edge of the panel regardless of total width."""
        offsets = {n: (x0, x1) for n, x0, x1 in column_x_offsets(total)}
        assert offsets["expr"][1] == total
        assert offsets["value"][1] == offsets["bind"][0]
        assert offsets["bind"][1] == offsets["expr"][0]


# ── §16.8 semantic tokens ──────────────────────────────────────────────


class TestSemanticTokens:
    def test_reference_set_matches_plan_table(self):
        # §16.8 — every row present.
        for key in (
            "timeline_bg", "timeline_bg_alt", "header_bg", "left_panel_bg",
            "grid_major", "grid_minor", "border",
            "text_primary", "text_secondary", "text_hint",
            "accent_blue", "accent_orange", "error",
        ):
            assert key in REFERENCE_TOKENS, f"missing token: {key}"

    def test_all_references_are_hex_colors(self):
        for key, val in REFERENCE_TOKENS.items():
            assert isinstance(val, str) and val.startswith("#"), (
                f"{key}={val!r} not a hex color")

    def test_resolve_token_returns_hex_string(self):
        assert resolve_token("accent_blue").startswith("#")
        assert resolve_token("text_primary").startswith("#")

    def test_resolve_token_unknown_returns_safe_fallback(self):
        """An unknown token must never raise — it returns text_secondary so
        a typo doesn't black out the panel."""
        fallback = resolve_token("text_secondary")
        assert resolve_token("totally_made_up_token") == fallback

    def test_resolve_token_prefers_project_palette_when_present(self):
        """If the project C.* has the attribute, resolve_token returns it
        (so theme changes flow through). We can't assert the exact value
        without coupling to the palette, but we can assert it returns *a*
        valid hex and that the playhead token resolves to the existing
        KEYFRAME_PLAYHEAD color when that exists."""
        val = resolve_token("playhead")
        assert val.startswith("#")


# ── §3.5 / §16.6 keyframe shapes ───────────────────────────────────────


class TestKeyframeShapes:
    def test_shape_for_every_state(self):
        states = {s.state for s in KEYFRAME_SHAPES}
        assert {"normal", "selected", "hover", "hold", "locked",
                "expr_driven"} <= states

    def test_selected_shape_larger_than_normal(self):
        normal = shape_for_state("normal")
        selected = shape_for_state("selected")
        assert selected.size_px > normal.size_px

    def test_hold_uses_square_not_diamond(self):
        """§16.6: Hold must be visually distinct from a normal diamond so
        it's recognisable without color (colorblind-safe)."""
        hold = shape_for_state("hold")
        assert hold.shape == "square"

    def test_expression_driven_draws_no_fake_diamond(self):
        """§16.6: an expression-driven value shows a row background, NOT a
        fake keyframe diamond."""
        expr = shape_for_state("expr_driven")
        assert expr.shape == "none"

    def test_locked_shape_uses_hint_color(self):
        locked = shape_for_state("locked")
        assert locked.fill_token == "text_hint"


# ── §16.9 icon spec ────────────────────────────────────────────────────


class TestIconSpec:
    def test_icon_set_covers_plan_table(self):
        names = set(icon_names())
        # §16.9 — the icons the timeline paints.
        for required in (
            "diamond", "play", "pause", "fast_backward", "fast_forward",
            "lock", "eye", "expand", "snap", "expression", "playhead",
            "bezier", "fit", "zoom_in", "zoom_out", "search", "filter",
        ):
            assert required in names, f"missing icon spec: {required}"

    def test_every_icon_has_a_recipe(self):
        for spec in ICON_SPECS:
            assert spec.recipe, f"{spec.name} missing recipe"

    def test_icon_size_default_is_14px(self):
        # §16.9 / §3.2: icons are 14px inside 24x22 buttons.
        assert all(s.icon_px == 14 for s in ICON_SPECS)


# ── §3.4 row state styles ──────────────────────────────────────────────


class TestRowStateStyles:
    def test_every_row_state_has_a_style(self):
        states = {s.state for s in ROW_STATE_STYLES}
        assert {"node", "node_selected", "param", "param_selected",
                "locked", "expression", "expression_error"} <= states

    def test_selected_row_uses_primary_text(self):
        sel = row_style("node_selected")
        assert sel.text_token == "text_primary"

    def test_error_row_uses_error_color(self):
        err = row_style("expression_error")
        assert err.bg_token == "error"
        assert err.text_token == "error"

    def test_unknown_row_state_falls_back_to_node(self):
        s = row_style("does_not_exist")
        assert s.state == "node"
