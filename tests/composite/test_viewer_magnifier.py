"""B2-3: viewer magnifier loupe — state, key toggle, sampling math.

Drawing and the framebuffer readback need a real GL context (skipped
offscreen); the state machine and the pure helpers are covered here.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.composite_core


def _make_viewer(qapp):
    from src.features.composite.ui.composite_viewport import CompositeViewer
    viewer = CompositeViewer()
    yield viewer
    viewer.close()


class TestMagnifierState:
    def test_toggle_and_zoom_validation(self, qapp):
        viewer = next(_make_viewer(qapp))
        assert viewer._magnifier_enabled is False
        assert viewer._magnifier_zoom == 8.0

        viewer.set_magnifier_enabled(True)
        assert viewer._magnifier_enabled is True
        assert viewer._toolbar._magnifier_toggle_action.isChecked()

        viewer.set_magnifier_zoom(4.0)
        assert viewer._magnifier_zoom == 4.0
        # Any value ≥6 snaps to 8x; below to 4x — no arbitrary zoom levels.
        viewer.set_magnifier_zoom(100.0)
        assert viewer._magnifier_zoom == 8.0
        viewer.set_magnifier_zoom(0.5)
        assert viewer._magnifier_zoom == 4.0

    def test_toggle_method_flips(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.toggle_magnifier()
        assert viewer._magnifier_enabled is True
        viewer.toggle_magnifier()
        assert viewer._magnifier_enabled is False

    def test_key_z_toggles(self, qapp):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        viewer = next(_make_viewer(qapp))
        viewer.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Z,
            Qt.KeyboardModifier.NoModifier, "z"))
        assert viewer._magnifier_enabled is True
        viewer.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Z,
            Qt.KeyboardModifier.NoModifier, "z"))
        assert viewer._magnifier_enabled is False

    def test_state_round_trip(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.set_magnifier_enabled(True)
        viewer.set_magnifier_zoom(4.0)
        state = viewer.get_state()
        assert state["magnifier_enabled"] is True
        assert state["magnifier_zoom"] == 4.0

        other = next(_make_viewer(qapp))
        other.set_state(state)
        assert other._magnifier_enabled is True
        assert other._magnifier_zoom == 4.0


class TestMagnifierSamplingMath:
    def test_source_span_scales_with_zoom(self):
        from src.features.composite.ui.composite_viewport import (
            CompositeViewer)
        span8 = CompositeViewer._magnifier_source_span_px(160.0, 8.0, 1.0)
        span4 = CompositeViewer._magnifier_source_span_px(160.0, 4.0, 1.0)
        assert span8 == 20
        assert span4 == 40
        # 4x samples a wider region than 8x — never the other way round.
        assert span4 > span8

    def test_source_span_never_degenerate(self):
        from src.features.composite.ui.composite_viewport import (
            CompositeViewer)
        assert CompositeViewer._magnifier_source_span_px(160.0, 0.0, 1.0) >= 1
        assert CompositeViewer._magnifier_source_span_px(0.0, 8.0, 1.0) >= 1
        # Retina DPR samples more device pixels for the same window.
        assert CompositeViewer._magnifier_source_span_px(160.0, 8.0, 2.0) == 40
