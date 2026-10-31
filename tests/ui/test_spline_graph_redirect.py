"""T129 Spline→Graph-mode redirect tests.

Locks the rewire (plan §22.1 / T129): the standalone Spline entry still
works (the panel shows), but opening it redirects focus to the SHARED
Graph Editor (the Keyframes panel's graph mode) instead of maintaining a
second curve-editor state.

Contract:
  * The Spline panel emits graph_mode_redirect on a real (user) show, but
    NOT on the first (page-enter layout) show.
  * CompositePage._on_spline_graph_mode_redirect switches the Keyframes
    panel to 'graph' view mode (and raises the Keyframes dock when known).
  * The old Spline panel itself stays functional (set_node_graph /
    set_current_frame still work) — no second state is CREATED, but the
    panel isn't broken either.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtGui import QShowEvent

from src.features.composite.ui.spline_panel import SplinePanel


pytestmark = pytest.mark.composite_core


@pytest.fixture
def panel(qapp):
    p = SplinePanel()
    p.resize(400, 300)
    return p


# ── T129: graph_mode_redirect signal ──────────────────────────────────


class TestRedirectSignal:
    def test_first_show_does_not_redirect(self, panel, qapp):
        """The first showEvent (page-enter layout) is skipped — no redirect."""
        received = []
        panel.graph_mode_redirect.connect(lambda: received.append(True))
        ev = QShowEvent()
        panel.showEvent(ev)
        assert received == []
        assert panel._initial_show_done is True

    def test_second_show_emits_redirect(self, panel, qapp):
        """A subsequent (user-driven) show fires graph_mode_redirect."""
        received = []
        panel.graph_mode_redirect.connect(lambda: received.append(True))
        # First show (layout pass).
        panel.showEvent(QShowEvent())
        assert received == []
        # Second show (user opened the entry).
        panel.showEvent(QShowEvent())
        assert received == [True]  # signal fired once

    def test_third_show_still_redirects(self, panel, qapp):
        """Every show after the first redirects (not just the second)."""
        received = []
        panel.graph_mode_redirect.connect(lambda: received.append(True))
        for _ in range(5):
            panel.showEvent(QShowEvent())
        # First is skipped, the next 4 redirect.
        assert len(received) == 4


# ── T129: old entry still functional ──────────────────────────────────


class TestOldEntryFunctional:
    def test_panel_still_sets_node_graph(self, panel, qapp):
        """The Spline panel's API isn't broken by the rewire — set_node_graph
        still works (the old entry remains usable, plan '旧入口仍可用')."""
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode
        from src.shared.keyframe import Keyframe, KeyframeTrack
        ng = NodeGraph()
        node = TransformNode()
        ng.add_node(node)
        track = KeyframeTrack(param_name="rotation")
        track.add_keyframe(Keyframe(0, 0.0))
        node.set_keyframe_track("rotation", track)
        # Should not raise.
        panel.set_node_graph(ng)

    def test_panel_still_sets_current_frame(self, panel, qapp):
        """set_current_frame still works (the panel isn't broken)."""
        panel.set_current_frame(42)
        # No exception = pass.


# ── T129: CompositePage handler switches to graph mode ───────────────


class TestCompositePageHandler:
    def test_handler_switches_keyframes_to_graph_mode(self, qapp):
        """_on_spline_graph_mode_redirect flips the Keyframes panel to
        graph mode (the shared Graph Editor)."""
        from src.features.composite.ui.composite_page import CompositePage
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        from src.features.composite.ui.keyframes_model import \
            CompositeTimelineState
        page = CompositePage()
        kp = KeyframesPanel()
        kp.set_timeline_state(CompositeTimelineState(start_frame=0,
                                                     end_frame=100))
        page.set_keyframes_panel(kp)
        assert kp.view_mode == "timeline"
        # Fire the redirect handler.
        page._on_spline_graph_mode_redirect()
        assert kp.view_mode == "graph"

    def test_handler_no_keyframes_panel_is_safe(self, qapp):
        """With no Keyframes panel bound, the handler is a safe no-op."""
        from src.features.composite.ui.composite_page import CompositePage
        page = CompositePage()
        # Should not raise.
        page._on_spline_graph_mode_redirect()

    def test_set_spline_panel_wires_redirect(self, qapp):
        """set_spline_panel connects graph_mode_redirect to the handler."""
        from src.features.composite.ui.composite_page import CompositePage
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        from src.features.composite.ui.keyframes_model import \
            CompositeTimelineState
        page = CompositePage()
        kp = KeyframesPanel()
        kp.set_timeline_state(CompositeTimelineState(start_frame=0,
                                                     end_frame=100))
        page.set_keyframes_panel(kp)
        sp = SplinePanel()
        page.set_spline_panel(sp)
        assert kp.view_mode == "timeline"
        # Trigger the panel's redirect (second show).
        sp.showEvent(QShowEvent())  # first (layout) — skipped
        sp.showEvent(QShowEvent())  # second — redirects
        # The handler ran → Keyframes panel is now in graph mode.
        assert kp.view_mode == "graph"
