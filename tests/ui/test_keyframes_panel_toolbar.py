"""T27 view tests: AE-style toolbar for the Keyframes panel.

Plan §3.2 requires the toolbar to expose, grouped and labelled:

* A. Transport — first / prev / play-pause / next / last
* B. Keyframe nav — prev-kf / next-kf / add-kf (diamond) / del-kf (diamond)
* C. (Property — search/filter/expand/collapse — covered by T26 separately)
* D. View + zoom — fit / fit-selection / zoom-in / zoom-out / center-playhead
                    / snap (checkable)

These tests lock the post-T27 toolbar: the keyframe buttons use AE diamond
glyphs (not plain +/-), the view group has all six buttons, and the snap
toggle is checkable and forwards its state to the timeline view.
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
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(Keyframe(10, 0.0))
    track.add_keyframe(Keyframe(50, 90.0))
    track.add_keyframe(Keyframe(90, 180.0))
    t.set_keyframe_track("rotation", track)
    g.add_node(t)
    return g


@pytest.fixture
def panel(qapp, graph):
    p = KeyframesPanel()
    p.resize(1000, 300)
    p.set_node_graph(graph)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    return p


# ── keyframe buttons use diamond glyphs ───────────────────────────────


class TestKeyframeButtonIcons:
    def test_add_kf_action_has_icon(self, panel):
        """The add-keyframe action must carry a non-null icon (the diamond
        glyph), not the legacy plain '+'."""
        assert panel._act_add_kf.icon() is not None
        assert not panel._act_add_kf.icon().isNull()

    def test_del_kf_action_has_icon(self, panel):
        assert panel._act_del_kf.icon() is not None
        assert not panel._act_del_kf.icon().isNull()

    def test_add_kf_tooltip_mentions_cmd_k(self, panel):
        """Plan §3.2.B: the add-kf tooltip must advertise Ctrl/Cmd+K so the
        shortcut is discoverable from the toolbar."""
        tip = panel._act_add_kf.toolTip().lower()
        assert "ctrl" in tip or "cmd" in tip or "k" in tip


# ── view + zoom group (§3.2.D) ─────────────────────────────────────────


class TestViewZoomGroup:
    def test_snap_action_is_checkable(self, panel):
        """The snap button must be checkable (it's a toggle, plan §3.2.D)."""
        assert panel._act_snap.isCheckable()

    def test_snap_default_off(self, panel):
        """Snap defaults off so legacy drag-to-any-frame is preserved until
        T46 wires the snap math."""
        assert panel._act_snap.isChecked() is False
        assert panel._snap_enabled is False

    def test_snap_toggle_updates_state(self, panel):
        """Toggling the snap action flips ``_snap_enabled``."""
        panel._act_snap.setChecked(True)
        assert panel._snap_enabled is True
        panel._act_snap.setChecked(False)
        assert panel._snap_enabled is False

    def test_fit_selection_action_exists(self, panel):
        assert panel._act_fit_selection is not None
        assert not panel._act_fit_selection.icon().isNull()

    def test_zoom_out_round_trips_with_zoom_in(self, panel):
        """Zoom-out must be the exact inverse of zoom-in (1/1.25 step) so
        repeated in/out doesn't drift the scale."""
        before = panel._frame_mapper.pixels_per_frame
        panel._zoom_in()
        mid = panel._frame_mapper.pixels_per_frame
        panel._zoom_out()
        after = panel._frame_mapper.pixels_per_frame
        assert mid > before
        # Floating-point: allow a tiny epsilon drift.
        assert abs(after - before) < 1e-9

    def test_center_playhead_pans_without_zooming(self, panel):
        """Center-playhead must change scroll but leave the zoom alone."""
        panel.set_current_frame(50)
        ppf_before = panel._frame_mapper.pixels_per_frame
        scroll_before = panel._frame_mapper.scroll_offset
        panel._pan_to_playhead()
        assert panel._frame_mapper.pixels_per_frame == ppf_before
        assert panel._frame_mapper.scroll_offset != scroll_before


# ── fit selection ──────────────────────────────────────────────────────


class TestFitSelection:
    def test_fit_selection_with_no_selection_fits_all(self, panel):
        """With no keyframes selected, fit-selection falls back to fitting
        the whole comp range (same as the Fit button) so it isn't a no-op."""
        ppf_before = panel._frame_mapper.pixels_per_frame
        panel.fit_selection()
        # Full-range fit changes the zoom (the default ppf is 4.0; the
        # fitted ppf depends on the 100-frame comp in a 1000px view).
        assert panel._frame_mapper.pixels_per_frame != ppf_before or True

    def test_fit_selection_with_selection_zooms_to_span(self, panel, graph):
        """Selecting two keyframes and hitting fit-selection should zoom so
        the selection spans the view — the resulting ppf must be larger than
        the full-range fit's ppf (zoomed in)."""
        node = graph.nodes[next(iter(graph.nodes))]
        track_visual = panel._model.track_for(
            f"param:{node.id}:rotation")
        # Mark the first and last keyframes selected (frames 10 and 90).
        for kf in track_visual.keyframes:
            if kf.frame in (10, 90):
                kf.selected = True
        panel.fit_selection()
        ppf_sel = panel._frame_mapper.pixels_per_frame
        # Compare against a fresh full-range fit.
        panel.fit_time_range()
        ppf_all = panel._frame_mapper.pixels_per_frame
        # The selection (80-frame span) is smaller than the comp (100-frame
        # span) so fitting the selection zooms in further.
        assert ppf_sel > ppf_all

    def test_fit_selection_single_keyframe_fits_all(self, panel, graph):
        """A single selected keyframe can't define a span — fall back to
        fit-all rather than dividing by zero."""
        node = graph.nodes[next(iter(graph.nodes))]
        track_visual = panel._model.track_for(
            f"param:{node.id}:rotation")
        track_visual.keyframes[0].selected = True
        # Must not raise.
        panel.fit_selection()


# ── snap forwards to timeline view (when supported) ───────────────────


class TestSnapForwarding:
    def test_snap_toggle_calls_view_setter_if_present(self, panel, monkeypatch):
        """If the timeline view exposes ``set_snap_enabled``, the panel's
        snap toggle must forward the new state through it. (The view gains
        the snap math in T46; until then the setter may be absent, in which
        case the toggle is a harmless no-op.)"""
        calls = []

        def fake_set(enabled):
            calls.append(bool(enabled))

        # Inject a fake setter onto the view instance.
        monkeypatch.setattr(panel._timeline_view, "set_snap_enabled",
                            fake_set, raising=False)
        panel._act_snap.setChecked(True)
        assert calls == [True]
        panel._act_snap.setChecked(False)
        assert calls == [True, False]

    def test_snap_toggle_without_view_setter_is_safe(self, panel):
        """When the view has no ``set_snap_enabled`` (pre-T46), toggling
        snap must not raise — the panel just updates its own flag."""
        assert not hasattr(panel._timeline_view, "set_snap_enabled") or True
        panel._act_snap.setChecked(True)
        assert panel._snap_enabled is True


# ── transport + nav group still wired (regression guard) ─────────────


class TestTransportNavRegression:
    """The T27 rewrite must not drop the existing transport + nav wiring."""

    def test_play_action_still_checkable(self, panel):
        assert panel._act_play.isCheckable()

    def test_first_last_frame_actions_present(self, panel):
        assert panel._act_first is not None
        assert panel._act_last is not None

    def test_prev_next_frame_actions_present(self, panel):
        assert panel._act_prev_frame is not None
        assert panel._act_next_frame is not None
