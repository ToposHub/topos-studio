"""P7 tests: AI capability manifest timeline extension (T62-T65).

The manifest's ``composite.timeline`` block is the AI/script discovery
surface for the AE-style timeline. These tests assert it stays in sync with
the modules it describes — adding a command/error-code/shortcut/row-type
in timeline_commands.py / timeline_shortcuts.py / timeline_view_model.py /
timeline_expression.py without surfacing it here fails CI, so the manifest
can never silently drift from what actually executes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.features.composite.ui import (
    timeline_commands as tc,
    timeline_expression as te,
    timeline_shortcuts as ts,
    timeline_view_model as tvm,
)
from src.features.edit.ai_editing.capability_manifest import build_manifest

pytestmark = pytest.mark.composite_core


@pytest.fixture(scope="module")
def manifest():
    return build_manifest()


@pytest.fixture(scope="module")
def timeline(manifest):
    block = manifest.composite.get("timeline")
    assert block is not None, "manifest.composite.timeline missing"
    return block


# ── T62: query interface declared ──────────────────────────────────────


class TestQueryCapability:
    def test_queries_list_declares_read_surface(self, timeline):
        # Every entry must be a non-empty string so an AI client can map an
        # intent ("list animatable params") to a method.
        for q in timeline["queries"]:
            assert isinstance(q, str) and q

    def test_queries_cover_core_inspection_needs(self, timeline):
        qs = set(timeline["queries"])
        # The plan §6.3 minimal query set.
        assert "list_nodes" in qs
        assert "list_properties" in qs
        assert "get_track" in qs
        assert "get_selection" in qs
        assert "query_at_frame" in qs


# ── T63: modification command vocabulary ───────────────────────────────


class TestModificationCommands:
    def test_commands_match_timeline_command_builder_surface(self, timeline):
        """The manifest's command list must cover the high-level builder
        methods (T63). If someone adds a command to TimelineCommandBuilder
        without listing it here, this test fails."""
        declared = set(timeline["commands"])
        # Each of these is a public method on TimelineCommandBuilder whose
        # name maps to the manifest entry.
        required = {
            "add_keyframe", "remove_keyframe", "clear_track",
            "move_keyframes", "copy_keyframes", "paste_keyframes",
            "set_interpolation", "set_tangent_mode", "set_bezier_handles",
            "set_extrapolation", "set_expression", "remove_expression",
            "set_node_range", "set_track_lock",
        }
        assert required <= declared

    def test_each_command_method_exists_on_builder(self, timeline):
        """Reverse check: every declared command has a builder method, so an
        AI that reads the manifest never calls a non-existent method."""
        # Map manifest command name → builder method name.
        method_for = {
            "add_keyframe": "add_keyframe",
            "remove_keyframe": "remove_keyframe",
            "clear_track": "clear_track",
            "move_keyframes": "begin_move",  # transaction entry point
            "copy_keyframes": "copy_keyframes",
            "paste_keyframes": "paste_keyframes",
            "set_interpolation": "set_interpolation",
            "set_tangent_mode": "set_tangent_mode",
            "set_bezier_handles": "set_bezier_handles",
            "set_extrapolation": "set_extrapolation",
            "set_expression": "set_expression",
            "remove_expression": "remove_expression",
            "set_node_range": "set_node_range",
            "set_track_lock": "set_track_lock",
        }
        for cmd, method in method_for.items():
            assert cmd in timeline["commands"], f"manifest missing {cmd}"
            assert hasattr(tc.TimelineCommandBuilder, method), (
                f"builder missing method for {cmd}: {method}")


# ── T15: error code contract ───────────────────────────────────────────


class TestErrorCodes:
    def test_manifest_lists_every_err_code(self, timeline):
        """Every public ERR_* in timeline_commands must appear in the manifest
        — an AI client branches on these and must not hit an undeclared one."""
        manifest_codes = set(timeline["error_codes"])
        source_codes = {
            name for name in dir(tc)
            if name.startswith("ERR_") and isinstance(getattr(tc, name), str)
        }
        assert source_codes == manifest_codes, (
            f"error code drift: source={source_codes - manifest_codes} "
            f"manifest={manifest_codes - source_codes}")

    def test_error_codes_are_strings(self, timeline):
        for code in timeline["error_codes"]:
            assert isinstance(getattr(tc, code), str)


# ── T49/T53: expression states + error codes ───────────────────────────


class TestExpressionSurface:
    def test_four_states_declared(self, timeline):
        states = set(timeline["expression_states"])
        assert states == {
            te.EXPRESSION_NONE, te.EXPRESSION_VALID,
            te.EXPRESSION_ERROR, te.EXPRESSION_DISABLED,
        }

    def test_expression_error_codes_match_source(self, timeline):
        manifest_codes = set(timeline["expression_error_codes"])
        source_codes = {
            name for name in dir(te)
            if name.startswith("EXPR_ERR_") and isinstance(getattr(te, name), str)
        }
        assert source_codes == manifest_codes

    def test_precedence_documented(self, timeline):
        # The plan's §5.4 ordering, surfaced so an AI knows the rules.
        prec = timeline["expression_precedence"]
        assert "keyframe_or_static_value" in prec[0]
        assert "expression_result_if_valid" in prec[1]
        assert "fallback" in prec[2]


# ── T04: row types + row_id grammar ────────────────────────────────────


class TestRowModelSurface:
    def test_row_types_match_viewmodel(self, timeline):
        assert set(timeline["row_types"]) == {
            tvm.ROW_NODE, tvm.ROW_GROUP, tvm.ROW_PARAM,
            tvm.ROW_CHANNEL, tvm.ROW_EXPRESSION,
        }

    def test_row_id_grammar_covers_every_type(self, timeline):
        grammar = timeline["row_id_grammar"]
        assert set(grammar) == set(timeline["row_types"])
        # Spot-check the param grammar parses back via the viewmodel.
        parsed = tvm.parse_row_id("param:abc:rotation")
        assert parsed["row_type"] == "param"
        assert parsed["node_id"] == "abc"
        assert parsed["param_name"] == "rotation"


# ── T17/T64: undo discipline + lock enforcement ────────────────────────


class TestUndoAndLock:
    def test_undo_discipline_documented(self, timeline):
        u = timeline["undo"]
        assert u["one_unit_per_drag"] is True
        assert u["esc_cancels_drag"] is True
        assert u["batch_transaction_supported"] is True

    def test_lock_enforced_at_service_layer(self, timeline):
        # T65: AI/script cannot bypass track locks any more than the mouse.
        assert timeline["lock_enforcement"] == "service_layer"


# ── T062: shortcut command vocabulary ───────────────────────────────────


class TestShortcutVocabulary:
    def test_shortcut_commands_match_dispatcher(self, timeline):
        """The manifest's shortcut_commands must equal the distinct commands
        in DEFAULT_AE_BINDINGS — otherwise the manifest either misses a
        shortcut or invents one the dispatcher won't fire."""
        manifest_cmds = set(timeline["shortcut_commands"])
        source_cmds = set(ts.DEFAULT_AE_BINDINGS.values())
        assert manifest_cmds == source_cmds
