"""B2-1: viewer Resolution menu (Full/Half/Quarter/Auto) → proxy_scale.

The viewer owns the UI tier selection and persists it in viewer_state; the
page maps it onto RenderRequest.proxy_scale and, for "auto", downgrades to
half while the user interacts with the view.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.composite_core

RESOLUTION_MODES = {"full", "half", "quarter", "auto"}


def _make_viewer(qapp):
    from src.features.composite.ui.composite_viewport import CompositeViewer
    viewer = CompositeViewer()
    yield viewer
    viewer.close()


class TestViewerResolutionMode:
    def test_invalid_mode_falls_back_to_full(self, qapp):
        viewer = next(_make_viewer(qapp))
        spy = []
        viewer.resolution_mode_changed.connect(spy.append)
        viewer.set_resolution_mode("ultra")
        assert viewer._resolution_mode == "full"
        assert spy == []

    def test_set_mode_emits_once_and_syncs_toolbar(self, qapp):
        viewer = next(_make_viewer(qapp))
        spy = []
        viewer.resolution_mode_changed.connect(spy.append)
        viewer.set_resolution_mode("half")
        assert viewer._resolution_mode == "half"
        assert spy == ["half"]
        action = viewer._toolbar._resolution_actions["half"]
        assert action.isChecked()
        # Same value again: no duplicate emission.
        viewer.set_resolution_mode("half")
        assert spy == ["half"]

    def test_state_round_trip(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.set_resolution_mode("quarter")
        state = viewer.get_state()
        assert state["resolution_mode"] == "quarter"

        other = next(_make_viewer(qapp))
        spy = []
        other.resolution_mode_changed.connect(spy.append)
        other.set_state(state)
        assert other._resolution_mode == "quarter"
        assert spy == ["quarter"]
        # Restoring the same value must not re-emit (no spurious re-evals).
        other.set_state(state)
        assert spy == ["quarter"]

    def test_state_without_key_keeps_mode(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.set_resolution_mode("half")
        viewer.set_state({"zoom_level": 1.0})
        assert viewer._resolution_mode == "half"

    def test_auto_interaction_hint_burst(self, qapp):
        viewer = next(_make_viewer(qapp))
        spy = []
        viewer.auto_proxy_interaction.connect(spy.append)
        viewer._note_auto_proxy_interaction()
        # A burst of events (wheel/drag) must emit True only once.
        viewer._note_auto_proxy_interaction()
        viewer._note_auto_proxy_interaction()
        assert spy == [True]
        # Idle timer firing ends the burst.
        viewer._auto_proxy_idle_timer.timeout.emit()
        assert viewer._auto_proxy_interacting is False
        assert spy == [True, False]


class TestPageResolutionMode:
    """Bind the real page handlers against a stub (no Qt page init)."""

    def _make_page_stub(self):
        from src.features.composite.ui.composite_page import CompositePage

        class _Page:
            _apply_resolution_mode = CompositePage._apply_resolution_mode
            _on_resolution_mode_changed = (
                CompositePage._on_resolution_mode_changed)
            _on_auto_proxy_interaction = (
                CompositePage._on_auto_proxy_interaction)
            _set_preview_quality = CompositePage._set_preview_quality
            _RESOLUTION_MODE_SCALES = CompositePage._RESOLUTION_MODE_SCALES

            def __init__(self):
                self._preview_quality = 1.0
                self._resolution_auto = False
                self._auto_proxy_saved_quality = None
                self._force_next_view_refresh = False
                self.evals = []

            def _evaluate_graph(self):
                self.evals.append(self._preview_quality)

        return _Page()

    def test_modes_map_onto_proxy_scale(self):
        page = self._make_page_stub()
        page._apply_resolution_mode("half")
        assert page._preview_quality == 0.5
        assert page._resolution_auto is False
        page._apply_resolution_mode("quarter")
        assert page._preview_quality == 0.25
        page._apply_resolution_mode("full")
        assert page._preview_quality == 1.0
        page._apply_resolution_mode("garbage")
        assert page._preview_quality == 1.0
        # Every tier change forced a refresh and re-evaluated exactly once.
        assert page.evals == [0.5, 0.25, 1.0]
        assert page._force_next_view_refresh is True

    def test_auto_idles_at_full_and_downgrades_on_interaction(self):
        page = self._make_page_stub()
        page._apply_resolution_mode("half")
        page._apply_resolution_mode("auto")
        assert page._resolution_auto is True
        assert page._preview_quality == 1.0

        page._on_auto_proxy_interaction(True)
        assert page._preview_quality == 0.5
        assert page._auto_proxy_saved_quality == 1.0
        page._on_auto_proxy_interaction(False)
        assert page._preview_quality == 1.0
        assert page._auto_proxy_saved_quality is None

    def test_auto_never_touches_user_chosen_quarter(self):
        page = self._make_page_stub()
        page._resolution_auto = True
        page._preview_quality = 0.25  # user picked Quarter before Auto
        page._on_auto_proxy_interaction(True)
        assert page._preview_quality == 0.25
        assert page._auto_proxy_saved_quality is None
        page._on_auto_proxy_interaction(False)
        assert page._preview_quality == 0.25

    def test_auto_restore_ignores_stray_end_events(self):
        page = self._make_page_stub()
        page._apply_resolution_mode("auto")
        page._on_auto_proxy_interaction(False)
        assert page._preview_quality == 1.0
        assert page.evals == []

    def test_same_scale_skips_evaluation(self):
        page = self._make_page_stub()
        page._apply_resolution_mode("full")  # already full
        assert page.evals == []
        assert page._preview_quality == 1.0
