"""P4 view tests: AE navigation shortcuts wired into KeyframesPanel.

Verifies the panel-level shortcut rewire (batch 8a) without synthesising
Qt key events — we exercise the bound handlers directly through the
panel's navigation/add methods and confirm the AE-habits toggle flips
behaviour. The pure dispatcher logic is covered by
test_timeline_shortcuts.py; this file locks the *panel integration*.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0


@pytest.fixture
def graph():
    g = NodeGraph()
    g.add_node(TransformNode())
    return g


@pytest.fixture
def panel(qapp, graph):
    p = KeyframesPanel()
    p.resize(1000, 300)

    # track_changed → persist on the node (mirrors CompositePage slot).
    def on_track_changed(node_id, param_name, keyframes):
        node = graph.get_node(node_id)
        if node is None:
            return
        if not keyframes:
            node.remove_keyframe_track(param_name)
            return
        pdef = node.get_param_defs().get(param_name)
        track = KeyframeTrack(
            param_name=param_name,
            min_val=pdef.min_val if pdef else 0.0,
            max_val=pdef.max_val if pdef else 1.0)
        for kf_data in keyframes:
            track.add_keyframe(Keyframe.from_dict(kf_data))
        node.set_keyframe_track(param_name, track)
    p.track_changed.connect(on_track_changed)

    p.set_node_graph(graph)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    return p


def _seed_rotation_keyframes(graph, frames=(0, 25, 50)):
    node = graph.nodes[next(iter(graph.nodes))]
    track = KeyframeTrack(param_name="rotation",
                          min_val=-360.0, max_val=360.0)
    for f in frames:
        track.add_keyframe(Keyframe(f, float(f)))
    node.set_keyframe_track("rotation", track)
    return node


# ── AE mode (default) ──────────────────────────────────────────────────


class TestAENavModeDefault:
    def test_ae_nav_enabled_by_default(self, panel):
        assert panel.ae_nav_enabled is True

    def test_K_handler_is_navigation_in_ae_mode(self, panel, graph):
        """In AE mode the plain-K binding fires _goto_next_keyframe, not
        add_keyframe_at_playhead (P0 §6.4 conflict resolved)."""
        node = _seed_rotation_keyframes(graph, frames=(0, 25, 50))
        panel.set_node_graph(graph)  # rebuild to pick up the new track
        panel.seek_to_frame(10)  # between 0 and 25
        # The bound nav handler moves the playhead to the next keyframe.
        panel._goto_next_keyframe()
        assert panel._state.current_frame == 25

    def test_J_handler_navigates_to_previous(self, panel, graph):
        node = _seed_rotation_keyframes(graph, frames=(0, 25, 50))
        panel.set_node_graph(graph)
        panel.seek_to_frame(30)  # between 25 and 50
        panel._goto_prev_keyframe()
        assert panel._state.current_frame == 25

    def test_cmd_K_adds_keyframe_in_ae_mode(self, panel, graph):
        """Ctrl/Cmd+K is the AE-style add binding; in AE mode this is how
        the user adds a keyframe (plain K navigates)."""
        node = _seed_rotation_keyframes(graph, frames=(0, 50))
        panel.set_node_graph(graph)
        # Select rotation so add has a target.
        panel._on_param_selected(node.id, "rotation")
        panel.seek_to_frame(25)
        # The Cmd+K handler is wired to add_keyframe_at_playhead.
        panel.add_keyframe_at_playhead()
        track = node.keyframe_tracks["rotation"]
        frames = sorted(int(kf["frame"]) for kf in track["keyframes"])
        assert 25 in frames


# ── legacy mode (T06 AE-habits switch) ─────────────────────────────────


class TestLegacyModeToggle:
    def test_set_ae_nav_enabled_false_restores_legacy_K(self, panel):
        assert panel.ae_nav_enabled is True
        panel.set_ae_nav_enabled(False)
        assert panel.ae_nav_enabled is False
        # In legacy mode the plain-K binding is rebuilt to point at
        # add_keyframe_at_playhead; the nav bindings are removed.
        assert getattr(panel, "_shortcut_add_kf", None) is not None
        assert getattr(panel, "_shortcut_nav_next", None) is None
        assert getattr(panel, "_shortcut_nav_prev", None) is None

    def test_set_ae_nav_enabled_true_restores_nav(self, panel):
        panel.set_ae_nav_enabled(False)
        panel.set_ae_nav_enabled(True)
        assert panel.ae_nav_enabled is True
        assert getattr(panel, "_shortcut_nav_next", None) is not None
        assert getattr(panel, "_shortcut_nav_prev", None) is not None
        assert getattr(panel, "_shortcut_add_kf", None) is None

    def test_toggle_is_idempotent(self, panel):
        panel.set_ae_nav_enabled(True)
        panel.set_ae_nav_enabled(True)  # no-op
        assert panel.ae_nav_enabled is True

    def test_cmd_K_adds_in_legacy_mode_too(self, panel, graph):
        """The Ctrl/Cmd+K binding stays wired in BOTH modes so the AE add
        path is always reachable."""
        panel.set_ae_nav_enabled(False)
        node = _seed_rotation_keyframes(graph, frames=(0, 50))
        panel.set_node_graph(graph)
        panel._on_param_selected(node.id, "rotation")
        panel.seek_to_frame(25)
        panel.add_keyframe_at_playhead()
        track = node.keyframe_tracks["rotation"]
        assert 25 in [int(kf["frame"]) for kf in track["keyframes"]]


# ── I / O work-area shortcuts unchanged ────────────────────────────────


class TestWorkAreaShortcuts:
    def test_I_sets_play_in_at_playhead(self, panel):
        panel.seek_to_frame(20)
        panel.set_play_in_at_playhead()
        assert panel._play_in == 20

    def test_O_sets_play_out_at_playhead(self, panel):
        panel.seek_to_frame(80)
        panel.set_play_out_at_playhead()
        assert panel._play_out == 80

    def test_IO_shortcuts_survive_ae_toggle(self, panel):
        """The I/O bindings are rebuilt on every toggle — verify they keep
        working after flipping AE mode."""
        panel.set_ae_nav_enabled(False)
        panel.set_ae_nav_enabled(True)
        assert getattr(panel, "_shortcut_set_in", None) is not None
        assert getattr(panel, "_shortcut_set_out", None) is not None
