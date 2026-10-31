"""P8 tests: timeline UI session-state + ColumnModel (T67/T75/T80).

Pure-data tests — no QWidget. The session-state model captures the durable
timeline preferences (plan §12) and the ColumnModel exposes the show/hide/
reorder/resize surface the plan (§16.4) wants. Both serialise to/from
JSON-safe dicts for project-file embedding.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.ui import timeline_layout
from src.features.composite.ui.timeline_session import (
    TIMELINE_SESSION_VERSION,
    ColumnConfig,
    ColumnModel,
    TimelineSessionState,
    migrate_session_state,
)

pytestmark = pytest.mark.composite_core


# ── ColumnModel basics ─────────────────────────────────────────────────


class TestColumnModelBasics:
    def test_defaults_match_layout_spec(self):
        cm = ColumnModel()
        spec_names = [s.name for s in timeline_layout.LEFT_COLUMNS]
        cfg_names = [c.name for c in cm.configs]
        assert cfg_names == spec_names
        # Default visibility comes from the spec.
        for cfg, spec in zip(cm.configs, timeline_layout.LEFT_COLUMNS):
            assert cfg.visible == spec.visible_by_default

    def test_name_column_is_always_visible(self):
        """You can't hide the 'name' column — it's the flexible-width column
        the parameter label lives in."""
        cm = ColumnModel()
        assert cm.set_visible("name", False) is False
        assert cm.is_visible("name") is True

    def test_set_visible_toggles_other_columns(self):
        cm = ColumnModel()
        assert cm.set_visible("lock", False) is True
        assert cm.is_visible("lock") is False
        assert cm.set_visible("lock", True) is True
        assert cm.is_visible("lock") is True

    def test_set_visible_noop_returns_false(self):
        cm = ColumnModel()
        # 'eye' is visible by default; setting it visible again is a no-op.
        assert cm.set_visible("eye", True) is False


# ── column widths ──────────────────────────────────────────────────────


class TestColumnWidths:
    def test_default_widths_sum_to_total(self):
        cm = ColumnModel()
        widths = cm.column_widths(330)
        assert sum(widths.values()) == 330
        # name absorbs the remainder.
        assert widths["name"] > 0

    def test_user_width_overrides_spec(self):
        cm = ColumnModel()
        cm.set_width("value", 100)
        widths = cm.column_widths(400)
        assert widths["value"] == 100

    def test_width_clamped_to_spec_minimum(self):
        cm = ColumnModel()
        # 'value' spec min is 48; try to shrink below it.
        cm.set_width("value", 10)
        widths = cm.column_widths(400)
        spec = next(s for s in timeline_layout.LEFT_COLUMNS
                    if s.name == "value")
        assert widths["value"] >= spec.min_width

    def test_hidden_column_excluded_from_widths(self):
        cm = ColumnModel()
        cm.set_visible("value", False)
        widths = cm.column_widths(330)
        assert "value" not in widths


# ── column reorder ─────────────────────────────────────────────────────


class TestColumnReorder:
    def test_move_column_changes_order(self):
        cm = ColumnModel()
        # Move 'value' (default order 7) to position 2.
        assert cm.move_column("value", 2) is True
        ordered = [c.name for c in sorted(cm.configs, key=lambda c: c.order)]
        assert ordered.index("value") == 2

    def test_move_preserves_other_relative_order(self):
        cm = ColumnModel()
        cm.move_column("expr", 0)
        ordered = [c.name for c in sorted(cm.configs, key=lambda c: c.order)]
        assert ordered[0] == "expr"
        # The rest keep their relative order.
        rest = ordered[1:]
        assert rest == [s.name for s in timeline_layout.LEFT_COLUMNS
                        if s.name != "expr"]

    def test_move_unknown_column_returns_false(self):
        cm = ColumnModel()
        assert cm.move_column("not_a_column", 0) is False

    def test_move_clamps_to_valid_range(self):
        cm = ColumnModel()
        cm.move_column("lock", 9999)  # way past the end
        ordered = [c for c in sorted(cm.configs, key=lambda c: c.order)]
        assert ordered[-1].name == "lock"


# ── serialise / deserialise ────────────────────────────────────────────


class TestColumnModelSerialise:
    def test_round_trip_preserves_visibility_and_widths(self):
        cm = ColumnModel()
        cm.set_visible("lock", False)
        cm.set_width("value", 100)
        cm.move_column("expr", 0)
        data = cm.to_dict()
        restored = ColumnModel.from_dict(data)
        assert restored.is_visible("lock") is False
        assert restored.width_for("value") == 100
        # Order preserved.
        ordered = [c.name for c in sorted(restored.configs,
                                          key=lambda c: c.order)]
        assert ordered[0] == "expr"

    def test_unknown_columns_in_payload_are_dropped(self):
        """Forward-compat: a newer file may have columns this build doesn't
        paint. They must be silently dropped, not crash."""
        cm = ColumnModel.from_dict({
            "columns": [
                {"name": "future_column", "visible": True, "width": 50,
                 "order": 0},
                {"name": "value", "visible": False, "width": 0, "order": 1},
            ]
        })
        cfg = cm._find("future_column")
        assert cfg is None  # dropped
        assert cm.is_visible("value") is False  # known one applied

    def test_malformed_column_entry_skipped(self):
        cm = ColumnModel.from_dict({
            "columns": [
                "not a dict",  # malformed
                {"name": "value", "visible": True},
            ]
        })
        # 'value' was applied; the malformed entry didn't crash.
        assert cm.is_visible("value") is True

    def test_empty_payload_returns_defaults(self):
        cm = ColumnModel.from_dict({})
        assert cm.is_visible("eye") is True  # spec default


# ── TimelineSessionState ───────────────────────────────────────────────


class TestSessionState:
    def test_defaults_are_sane(self):
        s = TimelineSessionState()
        assert s.filter_mode == "all"
        assert s.display_mode == "time"
        assert s.ae_nav_enabled is True
        assert s.snap_enabled is True
        assert s.schema_version == TIMELINE_SESSION_VERSION
        assert isinstance(s.columns, ColumnModel)

    def test_round_trip_preserves_all_fields(self):
        s = TimelineSessionState()
        s.expanded_node_ids = {"abc", "def"}
        s.filter_mode = "animated"
        s.pixels_per_frame = 8.0
        s.horizontal_scroll = 120.0
        s.display_mode = "timecode"
        s.ae_nav_enabled = False
        s.snap_enabled = False
        s.show_expression_rows = False
        s.selected_param_row = ("abc", "rotation")
        s.columns.set_visible("lock", False)
        data = s.to_dict()
        restored = TimelineSessionState.from_dict(data)
        assert restored.expanded_node_ids == {"abc", "def"}
        assert restored.filter_mode == "animated"
        assert restored.pixels_per_frame == 8.0
        assert restored.horizontal_scroll == 120.0
        assert restored.display_mode == "timecode"
        assert restored.ae_nav_enabled is False
        assert restored.snap_enabled is False
        assert restored.show_expression_rows is False
        assert restored.selected_param_row == ("abc", "rotation")
        assert restored.columns.is_visible("lock") is False

    def test_invalid_filter_mode_falls_back_to_all(self):
        s = TimelineSessionState.from_dict({"filter_mode": "bogus"})
        assert s.filter_mode == "all"

    def test_invalid_display_mode_falls_back_to_time(self):
        s = TimelineSessionState.from_dict({"display_mode": "bogus"})
        assert s.display_mode == "time"

    def test_pixels_per_frame_clamped(self):
        s = TimelineSessionState.from_dict({"pixels_per_frame": 99999.0})
        assert s.pixels_per_frame <= 256.0
        s = TimelineSessionState.from_dict({"pixels_per_frame": 0.001})
        assert s.pixels_per_frame >= 0.05

    def test_non_numeric_pixels_per_frame_falls_back(self):
        s = TimelineSessionState.from_dict({"pixels_per_frame": "not a number"})
        assert s.pixels_per_frame == 4.0

    def test_selected_param_row_must_be_pair(self):
        s = TimelineSessionState.from_dict(
            {"selected_param_row": ["only_one"]})
        assert s.selected_param_row is None
        s = TimelineSessionState.from_dict(
            {"selected_param_row": ["a", "b"]})
        assert s.selected_param_row == ("a", "b")

    def test_non_mapping_payload_returns_defaults(self):
        s = TimelineSessionState.from_dict("not a dict")  # type: ignore[arg-type]
        assert s.filter_mode == "all"
        assert s.expanded_node_ids == set()

    def test_expanded_node_ids_filtered_to_strings(self):
        s = TimelineSessionState.from_dict(
            {"expanded_node_ids": ["a", "", None, 0, "b"]})
        # Empty / None dropped; non-strings stringified-but-falsy dropped.
        assert s.expanded_node_ids == {"a", "b"}


# ── migration ──────────────────────────────────────────────────────────


class TestMigration:
    def test_migrate_v1_passes_through(self):
        s = migrate_session_state({
            "schema_version": 1,
            "filter_mode": "animated",
        })
        assert s.schema_version == 1
        assert s.filter_mode == "animated"

    def test_migrate_missing_schema_defaults_to_current(self):
        s = migrate_session_state({})
        assert s.schema_version == TIMELINE_SESSION_VERSION

    def test_render_data_never_in_session_state(self):
        """Plan §12 contract: the session state must not contain anything
        that influences the render result. Spot-check the serialised keys."""
        s = TimelineSessionState()
        data = s.to_dict()
        # No node-graph mutation fields.
        for forbidden in ("keyframe_tracks", "expressions", "time_range",
                          "parameters", "node_id", "value"):
            assert forbidden not in data, (
                f"render data leaked into session state: {forbidden}")
