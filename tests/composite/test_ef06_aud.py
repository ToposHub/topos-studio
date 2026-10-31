"""Regression tests for batch EF-06 audit fixes (2026-10-01).

Covers the 21 Suggestion-level findings in ``src/features/composite/ui``:

* AUD-10305 — stale DragValue controls are destroyed, not just hidden
* AUD-10309 — MaterialPanel._refresh_current resets _guard in finally
* AUD-10311 — preset combo reset is guarded against signal re-entry
* AUD-10610 — backdrop commands are copy-on-write (no unlocked in-place edit)
* AUD-10809 — perf panel caches the trace count instead of polling per frame
* AUD-10816 — outliner filter is a single O(n) pass and reveals ancestors
* AUD-11020 — parser crash maps to ERROR/unknown, never silent VALID
* AUD-11021 — empty expression maps to the documented EXPR_ERR_EMPTY code
* AUD-11027 — docs say Ctrl/Cmd+Enter commits (plain Enter = newline)
* AUD-11108 — Cmd↔Ctrl alias fold at lookup time
* AUD-11307 — compute_cornerpin_delta_params carries the DORMANT marker
* AUD-11310 — mask corner drag matches Transform Shift semantics
* AUD-11311 — feather docstrings say 0-0.5 for both node types
* AUD-11410 — PREVIEW enum doc matches the (FINAL-identical) param table
* AUD-9409  — package docstring no longer advertises non-exported classes
* AUD-9508  — graph publish is generation-gated (skips to_dict when clean)
* AUD-9711  — dead _space_pressed stub and its unreachable branches removed
* AUD-9714  — quad-view clear color resolved once per paint, not per quadrant
* AUD-9908  — compute_speeds aligned with the CALLER's input order
* AUD-9916  — set_graph_view(None) destroys the outgoing placeholder
* AUD-9917  — graph canvas signals are forwarded through the host surface
"""

from __future__ import annotations

import ast
import copy
import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.roto_shape_node import RotoShapeNode
from src.features.composite.ui import graph_model as graph_model_mod
from src.features.composite.ui.graph_model import GraphPoint, compute_speeds
from src.features.composite.ui.graph_view import GraphCanvasHost
from src.features.composite.ui.keyframes_model import KeyframesModel
from src.features.composite.ui.keyframes_tree_view import KeyframesTreeView
from src.features.composite.ui.material_panel import MaterialPanel
from src.features.composite.ui.node_graph_commands import (
    MoveBackdropCommand,
    ModifyBackdropCommand,
    ResizeBackdropCommand,
)
from src.features.composite.ui.perf_panel import (
    PerfPanel,
    _TRACE_COUNT_REFRESH_INTERVAL,
)
from src.features.composite.ui.scene_outliner_panel import SceneOutlinerPanel
from src.features.composite.ui.timeline_expression import (
    EXPR_ERR_EMPTY,
    EXPR_ERR_SYNTAX,
    EXPR_ERR_UNKNOWN,
    EXPRESSION_ERROR,
    EXPRESSION_VALID,
    ExpressionError,
    ExpressionErrorType,
    ExpressionResult,
    derive_expression_status,
)
import src.features.composite.ui.timeline_expression_editor as _expr_editor_module
from src.features.composite.ui.timeline_expression_editor import (
    _ExpressionTextArea,
)
from src.features.composite.ui.timeline_shortcuts import (
    CMD_ADD_KEYFRAME,
    ShortcutRegistry,
)
from src.features.composite.ui.viewport3d_state import (
    QUALITY_PROFILE_PARAMS,
    ViewportQualityProfile,
)
from src.shared.ui.widgets.drag_value import DragValue


# ── helpers ──────────────────────────────────────────────────────────────

def _material_summary() -> dict:
    return {
        "material_id": "body",
        "name": "Body",
        "mesh_count": 2,
        "base_color": [0.2, 0.3, 0.4],
        "metallic": 0.1,
        "roughness": 0.8,
        "opacity": 1.0,
        "slots": {},
    }


def _keyframe_model() -> KeyframesModel:
    graph = NodeGraph()
    graph.add_node(TransformNode())
    model = KeyframesModel()
    model.rebuild(graph)
    return model


def _viewer_module_path() -> Path:
    import src.features.composite.ui.viewer_handles as vh
    return Path(inspect.getfile(vh))


# ── AUD-10305 ────────────────────────────────────────────────────────────

def test_aud10305_value_controls_destroyed_when_scrolled_out(qapp):
    tree = KeyframesTreeView(_keyframe_model(), header_height=30)
    tree.resize(400, 120)  # room for only a few rows

    def _provider(node_id: str, param_name: str) -> dict:
        return {"min_val": 0.0, "max_val": 1.0, "value": 0.5,
                "decimals": 2, "step": 0.01}

    tree._value_control_provider = _provider
    tree._scroll_y = 0
    tree._sync_value_controls()
    created_controls = list(tree._value_controls.values())
    assert created_controls, "fixture produced no visible param rows"

    # Scroll far past the end of the track: every previously visible row is
    # out of the viewport now, so its control must be DESTROYED (popped and
    # detached via deleteLater), not merely hidden.
    tree._scroll_y = 10_000_000
    tree._sync_value_controls()
    assert len(tree._value_controls) == 0
    # Flush the deferred deletions (headless: no running event loop) and
    # require the controls to be genuinely destroyed — the wrapped C++
    # object must be gone, not merely a hidden child of the tree.
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for control in created_controls:
        with pytest.raises(RuntimeError):
            control.parent()


# ── AUD-10309 ────────────────────────────────────────────────────────────

def test_aud10309_guard_released_after_exception(qapp):
    panel = MaterialPanel()
    panel.set_materials([_material_summary()])
    assert panel.current_material_id() == "body"

    edit = panel._slot_edits["base_color"]

    def _boom(_text: str) -> None:
        raise RuntimeError("simulated widget failure")

    edit.setText = _boom  # shadow: next _refresh_current raises mid-guard
    try:
        with pytest.raises(RuntimeError):
            panel._refresh_current()
        # The guard must be back to False even on the exception path.
        assert panel._guard is False
    finally:
        del edit.setText  # un-shadow before Qt touches the widget again

    # Positive path: a normal refresh keeps the guard released too.
    panel._refresh_current()
    assert panel._guard is False


# ── AUD-10311 ────────────────────────────────────────────────────────────

def test_aud10311_preset_reset_does_not_reenter_handler(qapp):
    panel = MaterialPanel()
    panel.set_materials([_material_summary()])
    assert panel._preset_combo.count() >= 2, "fixture needs at least one preset"

    # The combo stays connected to a wrapper that mirrors the production
    # entry condition (guard check) and counts how often the handler BODY
    # actually runs — a guarded re-entry must not run it a second time.
    body_calls: list[int] = []
    original = MaterialPanel._on_preset_changed

    def _spy(idx: int) -> None:
        if panel._guard:
            return  # guard swallowed this emission (production behaviour)
        body_calls.append(idx)
        original(panel, idx)

    panel._preset_combo.currentIndexChanged.disconnect(panel._on_preset_changed)
    panel._preset_combo.currentIndexChanged.connect(_spy)
    try:
        panel._preset_combo.setCurrentIndex(1)  # first real preset
    finally:
        panel._preset_combo.currentIndexChanged.disconnect(_spy)

    # The handler's own setCurrentIndex(0) re-emits the signal; with the fix
    # the guard is held across the reset so the body runs exactly once.
    assert body_calls == [1]


# ── AUD-10610 ────────────────────────────────────────────────────────────

def _backdrop_graph() -> NodeGraph:
    graph = NodeGraph()
    graph.set_backdrop("bd1", {"title": "A", "position": [0.0, 0.0],
                               "size": [100.0, 80.0]})
    return graph


@pytest.mark.parametrize("cmd_factory", [
    lambda g: MoveBackdropCommand(graph=g, backdrop_id="bd1",
                                  old_position=(0.0, 0.0),
                                  new_position=(5.0, 6.0)),
    lambda g: ResizeBackdropCommand(graph=g, backdrop_id="bd1",
                                    old_size=(100.0, 80.0),
                                    new_size=(200.0, 160.0)),
    lambda g: ModifyBackdropCommand(graph=g, backdrop_id="bd1",
                                    old_data={"title": "A"},
                                    new_data={"title": "B"}),
])
def test_aud10610_backdrop_commands_are_copy_on_write(cmd_factory):
    graph = _backdrop_graph()
    live_before = copy.deepcopy(graph.backdrops["bd1"])
    committed: list[dict] = []

    def _recording_set(backdrop_id: str, data: dict) -> None:
        committed.append(copy.deepcopy(data))
        # Deliberately do NOT commit: the command must not already have
        # mutated the live dict as an unlocked side effect.

    graph.set_backdrop = _recording_set  # type: ignore[method-assign]

    cmd = cmd_factory(graph)
    cmd.redo()
    assert graph.backdrops["bd1"] == live_before, (
        "redo() must not mutate the engine's live backdrops dict in place")
    assert committed, "redo() must hand the updated copy to set_backdrop()"


def test_aud10610_backdrop_move_undo_redo_via_real_graph():
    graph = _backdrop_graph()
    cmd = MoveBackdropCommand(graph=graph, backdrop_id="bd1",
                              old_position=(0.0, 0.0), new_position=(5.0, 6.0))
    cmd.redo()
    assert graph.backdrops["bd1"]["position"] == [5.0, 6.0]
    cmd.undo()
    assert graph.backdrops["bd1"]["position"] == [0.0, 0.0]


# ── AUD-10809 ────────────────────────────────────────────────────────────

def test_aud10809_trace_count_cached_not_polled_per_frame(qapp, tmp_path):
    from src.features.composite.utils.perf_monitor import PerfMonitor

    monitor = PerfMonitor()
    monitor.begin_frame()
    monitor.record_frame(
        request_id=1, total_eval_ms=1.0, cache_hits=0, cache_misses=1,
        cache_evictions=0, active_textures=0, vram_mb=0.0)
    snapshot = monitor.get_snapshot()

    panel = PerfPanel()
    provider_calls = {"n": 0}

    def _provider() -> list:
        provider_calls["n"] += 1
        return []

    panel.set_history_provider(_provider)

    for _ in range(_TRACE_COUNT_REFRESH_INTERVAL):
        panel.update_from_snapshot(snapshot)

    # One refresh on the first frame (counter starts at 0) + at most one
    # more at the interval boundary — NOT one provider poll per frame.
    assert provider_calls["n"] <= 2
    assert f"Trace samples: {len([])}" in panel._text_edit.toPlainText()

    # Export must still report the exact count.
    out = tmp_path / "perf.json"
    assert panel.export_report(str(out)) is True
    payload = __import__("json").loads(out.read_text(encoding="utf-8"))
    assert payload["sample_count"] == 0


# ── AUD-10816 ────────────────────────────────────────────────────────────

def _outliner_hierarchy() -> dict:
    return {
        "node_id": "a", "name": "GroupA", "node_type": "group",
        "children": [
            {"node_id": "b", "name": "MiddleB", "node_type": "group",
             "children": [
                 {"node_id": "c", "name": "TargetC", "node_type": "mesh",
                  "children": []},
             ]},
            {"node_id": "d", "name": "UnrelatedD", "node_type": "mesh",
             "children": []},
        ],
    }


def test_aud10816_filter_reveals_full_ancestor_chain(qapp):
    panel = SceneOutlinerPanel()
    panel.set_hierarchy(_outliner_hierarchy())

    panel._apply_filter("targetc")

    items = panel._items
    assert not items["c"].isHidden()
    assert not items["b"].isHidden(), "the direct parent of a match must stay visible"
    assert items["b"].isExpanded(), "ancestors of a match must be expanded"
    assert not items["a"].isHidden()
    assert items["a"].isExpanded()
    assert items["d"].isHidden()

    # Empty filter restores everything and must not touch expansion.
    panel._apply_filter("")
    for item in items.values():
        assert not item.isHidden()


# ── AUD-11020 ────────────────────────────────────────────────────────────

class _ExprNode:
    def has_expression(self, _param: str) -> bool:
        return True

    def get_expression(self, _param: str) -> str:
        return "value * 2"

    def is_param_animated(self, _param: str) -> bool:
        return False


def test_aud11020_parser_crash_maps_to_error_not_valid(monkeypatch):
    import src.features.composite.ui.timeline_expression as te

    def _crash(self, _text):  # noqa: ANN001
        raise ValueError("simulated parser bug")

    monkeypatch.setattr(te.ExpressionParser, "validate", _crash)
    status = derive_expression_status(_ExprNode(), "opacity")
    assert status.state == EXPRESSION_ERROR
    assert status.error_code == EXPR_ERR_UNKNOWN


def test_aud11020_valid_and_syntax_paths_unaffected():
    ok = derive_expression_status(_ExprNode(), "opacity")
    assert ok.state == EXPRESSION_VALID

    node = _ExprNode()
    node.get_expression = lambda _p: "value +* 2"  # type: ignore[assignment]
    bad = derive_expression_status(node, "opacity")
    assert bad.state == EXPRESSION_ERROR
    assert bad.error_code == EXPR_ERR_SYNTAX


# ── AUD-11021 ────────────────────────────────────────────────────────────

def test_aud11021_empty_expression_yields_expr_empty():
    node = _ExprNode()
    node.get_expression = lambda _p: "   "  # type: ignore[assignment]
    status = derive_expression_status(node, "opacity")
    assert status.state == EXPRESSION_ERROR
    assert status.error_code == EXPR_ERR_EMPTY
    assert EXPR_ERR_EMPTY  # the constant is live, not a dead definition


# ── AUD-11027 ────────────────────────────────────────────────────────────

def test_aud11027_docs_commit_with_ctrl_enter_and_plain_enter_newlines(qapp):
    # The documented contract (module + widget docstrings) is Ctrl/Cmd+Enter.
    module_doc = _expr_editor_module.__doc__ or ""
    area_doc = _ExpressionTextArea.__doc__ or ""
    for doc in (module_doc, area_doc):
        assert "Enter / Ctrl+Enter" not in doc
    assert "Ctrl/Cmd+Enter" in module_doc

    # Behaviour: plain Enter inserts a newline (no commit); Ctrl+Enter commits.
    area = _ExpressionTextArea()
    commits: list[int] = []
    area.commit_requested.connect(lambda: commits.append(1))

    plain = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                      Qt.KeyboardModifier.NoModifier)
    area.keyPressEvent(plain)
    assert commits == []

    ctrl = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                     Qt.KeyboardModifier.ControlModifier)
    area.keyPressEvent(ctrl)
    assert len(commits) == 1


# ── AUD-11108 ────────────────────────────────────────────────────────────

def test_aud11108_cmd_ctrl_alias_fold_at_lookup():
    reg = ShortcutRegistry()
    reg.bindings.clear()
    reg.set_binding("Ctrl+K", CMD_ADD_KEYFRAME)  # only the Win spelling bound
    assert reg.command_for("Cmd+K") == CMD_ADD_KEYFRAME, (
        "lookup must fold Cmd↔Ctrl aliases")

    reg.bindings.clear()
    reg.set_binding("Cmd+P", "focus.position")  # only the macOS spelling
    assert reg.command_for("Ctrl+P") == "focus.position"

    # Exact (whitespace-normalised) match wins over the alias fallback, so
    # both spellings stay independently bindable in one registry.
    reg.bindings.clear()
    reg.set_binding("Ctrl+K", "a")
    reg.set_binding("Cmd+K", "b")
    assert reg.command_for("Cmd+K") == "b"
    assert reg.command_for("Ctrl+K") == "a"
    assert "Cmd+K" in reg.bindings and "Ctrl+K" in reg.bindings

    # Unbound sequences still miss.
    assert reg.command_for("Ctrl+O") is None


# ── AUD-11307 ────────────────────────────────────────────────────────────

def test_aud11307_dormant_marker_and_live_exports_intact():
    text = _viewer_module_path().read_text(encoding="utf-8")
    marker_pos = text.find("def compute_cornerpin_delta_params")
    assert marker_pos > 0
    head = text[max(0, marker_pos - 600):marker_pos]
    assert "DORMANT" in head and "AUD-11307" in head, (
        "the one truly unreferenced export must carry the DORMANT marker")
    # The other five "dead" exports from the audit line are alive (used by
    # edit/ui controllers + tests) and must remain importable public API.
    import src.features.composite.ui.viewer_handles as vh
    for name in ("get_transform_handle_definitions", "get_mask_handle_definitions",
                 "get_roto_handle_definitions", "find_nearest_handle",
                 "hit_test_handle_screen"):
        assert callable(getattr(vh, name))


# ── AUD-11310 ────────────────────────────────────────────────────────────

_MASK_PARAMS = {"center_x": 0.5, "center_y": 0.5, "width": 0.4,
                "height": 0.2, "rotation": 0.0, "feather": 0.0}


def test_aud11310_mask_corner_plain_drag_is_uniform():
    from src.features.composite.ui.viewer_handles import compute_mask_delta_params

    out = compute_mask_delta_params(
        "mask_corner_br", (100.0, 50.0), (110.0, 60.0),
        dict(_MASK_PARAMS), 200, 100, modifiers=0)
    aspect_before = _MASK_PARAMS["width"] / _MASK_PARAMS["height"]
    aspect_after = out["width"] / out["height"]
    assert aspect_after == pytest.approx(aspect_before), (
        "plain corner drag must scale uniformly (Transform convention)")


def test_aud11310_mask_corner_shift_frees_axes():
    from src.features.composite.ui.viewer_handles import (
        compute_mask_delta_params,
        _SHIFT_MODIFIER_MASK,
    )

    out = compute_mask_delta_params(
        "mask_corner_br", (100.0, 50.0), (110.0, 60.0),
        dict(_MASK_PARAMS), 200, 100, modifiers=_SHIFT_MODIFIER_MASK)
    # Shift must FREE the axes: width tracks dx only, height tracks dy only.
    assert out["width"] == pytest.approx(0.5)   # 0.4 * (1 + 10/(0.2*200))
    assert out["height"] == pytest.approx(0.4)  # 0.2 * (1 + 10/(0.1*100))
    assert (out["width"] / out["height"]) != pytest.approx(
        _MASK_PARAMS["width"] / _MASK_PARAMS["height"])


# ── AUD-11311 ────────────────────────────────────────────────────────────

def test_aud11311_feather_docstrings_say_half_range():
    text = _viewer_module_path().read_text(encoding="utf-8")
    assert "0-1 for RotoShapeNode" not in text, (
        "docstrings must not claim RotoShapeNode feather reaches 1.0")

    feather_defs = [pd for pd in RotoShapeNode.PARAM_DEFS
                    if getattr(pd, "name", "") == "feather"]
    assert feather_defs, "RotoShapeNode must define a feather param"
    assert float(feather_defs[0].max_val) == pytest.approx(0.5)


# ── AUD-11410 ────────────────────────────────────────────────────────────

def test_aud11410_preview_doc_matches_param_table():
    doc = inspect.getdoc(ViewportQualityProfile) or ""
    assert "简化 PBR + 可选 MSAA" not in doc, (
        "PREVIEW doc must match the param table (which equals FINAL)")
    assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.PREVIEW] == \
        QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FINAL]


# ── AUD-9409 ─────────────────────────────────────────────────────────────

def test_aud9409_package_docstring_matches_exports():
    import src.features.composite.ui as composite_ui_pkg

    doc = composite_ui_pkg.__doc__ or ""
    for phantom in ("CompositeViewer", "CompositeViewerBar",
                    "PixelInspectorBar"):
        assert phantom not in doc, (
            f"docstring advertises non-exported symbol {phantom}")
    for name in composite_ui_pkg.__all__:
        assert hasattr(composite_ui_pkg, name), name


# ── AUD-9508 ─────────────────────────────────────────────────────────────

class _StubGraph:
    """NodeGraph stand-in exposing generation + a counted to_dict()."""

    def __init__(self) -> None:
        self._generation = 5
        self.to_dict_calls = 0

    @property
    def generation(self) -> int:
        return self._generation

    def to_dict(self) -> dict:
        self.to_dict_calls += 1
        return {"generation": self._generation}

    def snapshot(self, **_kwargs):  # noqa: ANN003
        return MagicMock(name="snapshot")


def _bare_page(monkeypatch):
    """CompositePage skeleton with only what _build_render_request touches.

    ``CompositePage.__new__`` skips the QWidget base init, so EVERY
    attribute the request path reads must exist in the instance ``__dict__``
    (a miss would fall through to sip and raise ``RuntimeError``). The one
    read-only ``comp_clip_fps`` property is replaced on the class for the
    duration of the test.
    """
    from src.features.composite.ui.composite_page import CompositePage

    # Read-only property → must be neutralised at class level.
    monkeypatch.setattr(
        CompositePage, "comp_clip_fps", property(lambda self: 24.0))

    page = CompositePage.__new__(CompositePage)
    page._renderer = MagicMock()
    page._node_graph = _StubGraph()
    page._is_active = True
    page._current_composite_id = "comp1"
    page._last_published_graph_data = None
    page._last_published_graph_generation = -1
    page._request_id_gen = MagicMock()
    page._request_id_gen.next_id = MagicMock(return_value=1)
    page._last_submitted_request_id = 0
    page._runtime_context = None
    page._last_color_texture_id = None
    page._last_color_width = 0
    page._last_color_height = 0
    page._last_color_revision = 0
    page._current_keyframe_frame = MagicMock(return_value=0)
    page._apply_view_mode_overrides = MagicMock(
        side_effect=lambda snapshot: snapshot)
    page._current_render_mode = "preview"
    page._timeline_clip_gpu_provider = None
    page._project = None
    page._refresh_asset_dependency_panel = MagicMock()
    page.composite_graph_changed = MagicMock()
    # Remaining attributes reached via getattr(...) in the request tail —
    # shadowed so no lookup ever reaches the un-initialised sip object.
    page._inject_viewer3d_scene_snapshot = None
    page._interaction_active = False
    page._nav3d_active = False
    page._resolve_viewer_node_id = MagicMock(return_value=None)
    page._view_signature = MagicMock(return_value="sig")
    page._is_quad_view = MagicMock(return_value=False)
    page._single_ortho_view_code = MagicMock(return_value=None)
    page._quad_active_view_code = MagicMock(return_value=None)
    page._selected_3d_node_id_for_request = MagicMock(return_value=None)
    page._isolate_3d_node_id_for_request = MagicMock(return_value=None)
    page._global_3d_scene_settings = MagicMock(return_value=None)
    page._preview_quality = 1.0
    page._output_target_name = "display"
    page._output_target_node_id = None
    page._build_playback_drive = MagicMock(return_value=None)
    page._viewer_user_roi_bbox = MagicMock(return_value=None)
    return page


def test_aud9508_publish_is_generation_gated(monkeypatch):
    page = _bare_page(monkeypatch)
    graph = page._node_graph

    page._build_render_request()
    assert graph.to_dict_calls == 1
    assert page.composite_graph_changed.emit.call_count == 1

    # Same generation → the hot path must skip to_dict() entirely.
    page._build_render_request()
    assert graph.to_dict_calls == 1
    assert page.composite_graph_changed.emit.call_count == 1

    # A real mutation (generation bump) → one fresh serialization + publish.
    graph._generation = 6
    page._build_render_request()
    assert graph.to_dict_calls == 2
    assert page.composite_graph_changed.emit.call_count == 2


# ── AUD-9711 ─────────────────────────────────────────────────────────────

def test_aud9711_dead_space_pressed_stub_removed():
    import src.features.composite.ui.composite_viewport as cv

    text = Path(inspect.getfile(cv)).read_text(encoding="utf-8")
    assert "def _space_pressed" not in text
    assert "self._space_pressed()" not in text


# ── AUD-9714 ─────────────────────────────────────────────────────────────

def test_aud9714_quad_clear_color_resolved_outside_quadrant_loop():
    import src.features.composite.ui.composite_viewport as cv

    tree = ast.parse(
        Path(inspect.getfile(cv)).read_text(encoding="utf-8"))
    quad_loops = [node for node in ast.walk(tree)
                  if isinstance(node, ast.For)
                  and isinstance(node.iter, ast.Name)
                  and node.iter.id == "QUAD_VIEW_ORDER"]
    assert quad_loops, "quad paint loop not found"
    for loop in quad_loops:
        for sub in ast.walk(loop):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_viewport_bg_color_rgb"):
                pytest.fail(
                    "_viewport_bg_color_rgb() must be hoisted out of the "
                    "per-quadrant loop (re-merges global settings 4x/frame)")
    # Sanity: the hoisted call still exists somewhere in the module.
    assert "_viewport_bg_color_rgb" in Path(
        inspect.getfile(cv)).read_text(encoding="utf-8")


# ── AUD-9908 ─────────────────────────────────────────────────────────────

def test_aud9908_compute_speeds_aligned_with_input_order():
    def _pt(frame: float, value: float) -> GraphPoint:
        return GraphPoint(row_id="r", node_id="n", parameter_name="p",
                          channel=None, frame=frame, value=value)

    # Input deliberately UNORDERED: first point is the middle keyframe.
    pts = [_pt(50.0, 100.0), _pt(0.0, 0.0), _pt(100.0, 0.0)]
    speeds = compute_speeds(pts, fps=24.0)

    assert len(speeds) == 3
    # Segment starting at frame 0 climbs 0→100 over 50 frames: +48/s.
    assert speeds[1] == pytest.approx(48.0)
    # Segment starting at frame 50 falls 100→0 over 50 frames: -48/s.
    assert speeds[0] == pytest.approx(-48.0)
    # Tail mirrors the previous segment's speed.
    assert speeds[2] == pytest.approx(-48.0)

    # Ordered input keeps the documented behaviour.
    ordered = [_pt(0.0, 0.0), _pt(50.0, 100.0)]
    out = compute_speeds(ordered, fps=24.0)
    assert out == pytest.approx([48.0, 48.0])


# ── AUD-9916 ─────────────────────────────────────────────────────────────

def test_aud9916_reset_destroys_outgoing_placeholder(qapp):
    host = GraphCanvasHost(QWidget())
    old = host._placeholder
    assert old is not None
    assert old.parent() is host

    host.set_graph_view(None)
    assert host._placeholder is not old
    # The outgoing placeholder must be DETACHED from the host (setParent
    # + deleteLater), not merely removed from the layout while staying a
    # hidden child (AUD-9916 leak).
    assert old.parent() is None
    assert old not in host.findChildren(QLabel)
    assert host._placeholder.parent() is host


# ── AUD-9917 ─────────────────────────────────────────────────────────────

class _FakeGraphCanvas(QWidget):
    current_frame_changed = pyqtSignal(int)
    vertical_scroll_changed = pyqtSignal(int)
    track_changed = pyqtSignal(str, str, list)
    range_changed = pyqtSignal(str, dict)
    play_range_changed = pyqtSignal(int, int)
    display_mode_changed = pyqtSignal(str)


def test_aud9917_graph_canvas_signals_forwarded(qapp):
    host = GraphCanvasHost(QWidget())
    canvas = _FakeGraphCanvas()
    host.set_graph_view(canvas)

    received: list[int] = []
    host.current_frame_changed.connect(received.append)
    canvas.current_frame_changed.emit(7)
    assert received == [7], "graph canvas signals must reach the host surface"

    # Re-installing the same canvas must not double-bind the signals.
    host.set_graph_view(canvas)
    canvas.current_frame_changed.emit(9)
    assert received == [7, 9]


def test_aud9917_timeline_forwarding_still_works(qapp):
    timeline = _FakeGraphCanvas()
    host = GraphCanvasHost(timeline)
    received: list[int] = []
    host.current_frame_changed.connect(received.append)
    timeline.current_frame_changed.emit(3)
    assert received == [3]
