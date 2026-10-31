"""P0-1 tests: playhead seek → preview re-render + UI sync (plan §2 P0-1).

Validates that a frame change from any animation panel:
  - updates CompositePage._composite_current_frame
  - triggers _evaluate_graph() so the viewer re-renders the animated frame
  - syncs the Keyframes/Spline/Properties panels without recursion
And that toolbar prev/next-keyframe buttons emit the seek signal.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def panel_with_graph(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation")
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(10, 45.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    panel = KeyframesPanel()
    panel.set_node_graph(g)
    return panel, g, t


# ── toolbar prev/next emit seek ──────────────────────────────────────────

def test_goto_next_keyframe_emits_seek(panel_with_graph):
    """P0-1: the toolbar next-keyframe button must emit current_frame_changed
    so CompositePage re-renders. The old code only updated visuals silently."""
    panel, g, t = panel_with_graph
    panel.set_current_frame(3)
    received = []
    panel.current_frame_changed.connect(received.append)
    panel._goto_next_keyframe()
    assert 10 in received  # next keyframe after 3 is 10


def test_goto_prev_keyframe_emits_seek(panel_with_graph):
    panel, g, t = panel_with_graph
    panel.set_current_frame(7)
    received = []
    panel.current_frame_changed.connect(received.append)
    panel._goto_prev_keyframe()
    assert 0 in received  # prev keyframe before 7 is 0


def test_seek_to_frame_emits(panel_with_graph):
    """seek_to_frame (user seek) emits; set_current_frame (write-back) does not."""
    panel, g, t = panel_with_graph
    received = []
    panel.current_frame_changed.connect(received.append)
    panel.seek_to_frame(5)
    assert 5 in received
    received.clear()
    panel.set_current_frame(8)  # silent write-back
    assert received == []


# ── CompositePage.set_composite_current_frame triggers re-render ─────────

def _make_minimal_page(qapp):
    """Build a CompositePage with mocked renderer so we can observe evaluate calls."""
    from src.features.composite.ui.composite_page import CompositePage
    page = CompositePage()
    page._node_graph = NodeGraph()
    page._node_graph.add_node(TransformNode())
    # Ensure optional panel attributes exist (set_composite_current_frame reads them).
    if not hasattr(page, '_properties_panel'):
        page._properties_panel = None
    if not hasattr(page, '_keyframes_panel'):
        page._keyframes_panel = None
    if not hasattr(page, '_spline_panel'):
        page._spline_panel = None
    # Stub evaluate to count calls (avoids needing a GL renderer).
    page._evaluate_graph = MagicMock()
    return page


def test_set_composite_current_frame_triggers_evaluate(qapp):
    page = _make_minimal_page(qapp)
    page._evaluate_graph.reset_mock()
    page.set_composite_current_frame(10)
    assert page._composite_current_frame == 10
    page._evaluate_graph.assert_called_once()


def test_set_composite_current_frame_same_frame_no_rebuild(qapp):
    """No-op when the frame didn't change (avoid needless re-renders)."""
    page = _make_minimal_page(qapp)
    page.set_composite_current_frame(5)
    page._evaluate_graph.reset_mock()
    page.set_composite_current_frame(5)  # same
    page._evaluate_graph.assert_not_called()


def test_set_frame_syncs_keyframes_and_spline_panels(qapp):
    """P0-1: a frame change writes back to the Keyframes + Spline panels."""
    page = _make_minimal_page(qapp)
    kf = MagicMock()
    sp = MagicMock()
    page._keyframes_panel = kf
    page._spline_panel = sp
    page.set_composite_current_frame(7)
    kf.set_current_frame.assert_called_once_with(7)
    sp.set_current_frame.assert_called_once_with(7)


def test_set_frame_anti_recursion_guard(qapp):
    """The guard prevents a panel write-back from re-entering set_frame."""
    page = _make_minimal_page(qapp)
    # Simulate: during sync, the panel's set_current_frame somehow re-emits
    # back into set_composite_current_frame. The guard must block it.
    call_count = {"n": 0}

    def _recursive_set(frame):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Pretend the panel echoes back.
            page.set_composite_current_frame(frame)
    kf = MagicMock()
    kf.set_current_frame.side_effect = lambda f: _recursive_set(f)
    page._keyframes_panel = kf
    page.set_composite_current_frame(3)
    # Should have entered the sync path once, and the recursive call returned early.
    assert call_count["n"] == 1
