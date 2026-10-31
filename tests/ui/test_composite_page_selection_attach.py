"""T61 integration tests: CompositePage wires Spline↔Keyframes selection sync.

The page-level glue (CompositePage.set_keyframes_panel / set_spline_panel)
must attach the Spline to the Keyframes panel's shared selection model
regardless of which panel was bound first. These tests exercise both bind
orders through the real page API.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.spline_panel import SplinePanel
from src.features.composite.ui.timeline_view_model import make_param_row_id
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0


def _graph():
    g = NodeGraph()
    node = TransformNode()
    track = KeyframeTrack(param_name="rotation", min_val=-360.0,
                          max_val=360.0)
    for f in (0, 25, 50):
        track.add_keyframe(Keyframe(f, float(f)))
    node.set_keyframe_track("rotation", track)
    g.add_node(node)
    return g, node


def _build_page(qapp):
    g, node = _graph()
    page = CompositePage()
    page._node_graph = g
    page._composite_duration_frames = CLIP_FRAMES
    kp = KeyframesPanel()
    sp = SplinePanel()
    return page, g, node, kp, sp


# ── bind order: Keyframes first, then Spline ──────────────────────────


def test_keyframes_first_then_spline_attaches(qapp):
    """The default bind order (Keyframes panel exists before the Spline
    dock is opened) must attach the Spline on set_spline_panel."""
    page, g, node, kp, sp = _build_page(qapp)
    page.set_keyframes_panel(kp)
    page.set_spline_panel(sp)
    # The Spline's _shared_selection_model should now be the Keyframes
    # panel's selection model.
    assert sp._shared_selection_model is kp.selection_model
    # And a curve selection in the Spline back-syncs to Keyframes.
    sp._on_curve_selected(f"{node.id}:rotation")
    assert make_param_row_id(node.id, "rotation") in \
        kp.selection_model.selection.rows


# ── bind order: Spline first, then Keyframes ──────────────────────────


def test_spline_first_then_keyframes_attaches(qapp):
    """Reverse bind order (Spline dock opened before Keyframes): the
    Keyframes binding must re-attach the Spline so sync is live."""
    page, g, node, kp, sp = _build_page(qapp)
    page.set_spline_panel(sp)
    # Before Keyframes is bound, the Spline has no shared model.
    assert getattr(sp, "_shared_selection_model", None) is None
    page.set_keyframes_panel(kp)
    # Now it should be attached.
    assert sp._shared_selection_model is kp.selection_model
    sp._on_curve_selected(f"{node.id}:rotation")
    assert make_param_row_id(node.id, "rotation") in \
        kp.selection_model.selection.rows


# ── forward direction still works through the page ────────────────────


def test_keyframes_param_select_forward_syncs_spline(qapp):
    """Selecting a param in Keyframes still selects the Spline curve via
    the page wiring (T56 forward direction, end-to-end)."""
    page, g, node, kp, sp = _build_page(qapp)
    page.set_keyframes_panel(kp)
    page.set_spline_panel(sp)
    kp._on_param_selected(node.id, "rotation")
    curve = sp._model.curve_by_id(f"{node.id}:rotation")
    assert curve is not None and curve.selected is True


# ── detaching one panel doesn't crash the other ───────────────────────


def test_set_spline_panel_none_does_not_crash(qapp):
    """Clearing the Spline panel (dock closed) must not raise even if the
    shared-selection attach was live."""
    page, g, node, kp, sp = _build_page(qapp)
    page.set_keyframes_panel(kp)
    page.set_spline_panel(sp)
    # Should not raise.
    page.set_spline_panel(None)
    assert page._spline_panel is None
