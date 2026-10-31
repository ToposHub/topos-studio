"""B2-4: viewer background tri-state — checkerboard / black / white.

State machine, clear-colour mapping and persistence; the GL draw itself
needs a real context and is covered by manual QA.
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.composite_core


def _make_viewer(qapp):
    from src.features.composite.ui.composite_viewport import CompositeViewer
    viewer = CompositeViewer()
    yield viewer
    viewer.close()


class TestBackgroundMode:
    def test_default_and_valid_modes(self, qapp):
        viewer = next(_make_viewer(qapp))
        assert viewer._background_mode == "checker"
        spy = []
        viewer.background_mode_changed.connect(spy.append)
        viewer.set_background_mode("black")
        assert viewer._background_mode == "black"
        assert spy == ["black"]
        viewer.set_background_mode("white")
        assert viewer._background_mode == "white"
        assert spy == ["black", "white"]

    def test_invalid_and_case_normalized(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.set_background_mode("BLACK")
        assert viewer._background_mode == "black"
        viewer.set_background_mode("sepia")
        assert viewer._background_mode == "checker"

    def test_same_value_no_signal(self, qapp):
        viewer = next(_make_viewer(qapp))
        spy = []
        viewer.background_mode_changed.connect(spy.append)
        viewer.set_background_mode("checker")
        assert spy == []

    def test_toolbar_radio_syncs(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.set_background_mode("white")
        assert viewer._toolbar._background_actions["white"].isChecked()
        assert not viewer._toolbar._background_actions["checker"].isChecked()

    def test_state_round_trip(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.set_background_mode("black")
        state = viewer.get_state()
        assert state["background_mode"] == "black"

        other = next(_make_viewer(qapp))
        other.set_state(state)
        assert other._background_mode == "black"
        assert other._toolbar._background_actions["black"].isChecked()


class TestClearColorMapping:
    def test_black_and_white_override(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer._background_mode = "black"
        assert viewer._viewport_clear_color_rgb() == (0.0, 0.0, 0.0)
        viewer._background_mode = "white"
        assert viewer._viewport_clear_color_rgb() == (1.0, 1.0, 1.0)

    def test_checker_keeps_scene_colour(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer._viewport_bg_color_rgb = lambda: (0.2, 0.3, 0.4)
        viewer._background_mode = "checker"
        assert viewer._viewport_clear_color_rgb() == (0.2, 0.3, 0.4)


def test_paintgl_uses_tri_state_clear_and_gates_checker():
    from src.features.composite.ui.composite_viewport import CompositeViewer

    clear_src = inspect.getsource(CompositeViewer.paintGL)
    # The 2D path clears through the tri-state helper…
    assert "_viewport_clear_color_rgb" in clear_src
    # …and the checkerboard draw is gated on the background mode.
    assert '_background_mode == "checker"' in clear_src
