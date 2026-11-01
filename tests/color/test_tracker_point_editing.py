"""Tracker point editing is explicit, cancellable, and preserves observation IDs."""
import pytest

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QGraphicsScene

from src.features.color.ui.tracker_overlay import FeaturePointStatus, OverlayState, TrackerOverlay
from src.features.color.ui.tracker_panel import TrackerPanel
from src.features.color.ui.tracker_status import PanelState


class MouseEvent:
    def __init__(self, x: float, y: float, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self.position = QPointF(x, y)
        self.mouse_button = button
        self.accepted = False

    def scenePos(self) -> QPointF:
        return self.position

    def button(self) -> Qt.MouseButton:
        return self.mouse_button

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


_scenes: list[QGraphicsScene] = []


@pytest.fixture(autouse=True)
def cleanup_scenes(qapp):
    yield
    for scene in _scenes:
        scene.clear()
        scene.deleteLater()
    _scenes.clear()
    qapp.processEvents()


def overlay_fixture() -> TrackerOverlay:
    overlay = TrackerOverlay()
    scene = QGraphicsScene()
    _scenes.append(scene)
    scene.addItem(overlay)
    overlay.set_frame_size(1000.0, 500.0)
    overlay.set_overlay_active(True)
    overlay.set_state(OverlayState.TRACKER_READY)
    overlay.set_features([(0.2, 0.3, FeaturePointStatus.VALID),
                          (0.4, 0.6, FeaturePointStatus.WEAK)], feature_ids=[41, 96])
    return overlay


def test_feature_click_uses_stable_id_and_requires_interactive(qapp):
    overlay = overlay_fixture()
    seen = []
    overlay.feature_selected.connect(seen.append)
    event = MouseEvent(200, 150)
    overlay.mousePressEvent(event)
    assert event.accepted is False
    assert overlay.selected_feature_id is None
    overlay.set_interactive(True)
    overlay.mousePressEvent(event)
    assert event.accepted is True
    assert overlay.selected_feature_id == 41
    assert seen == [41]
    overlay.set_features([(0.4, 0.6, FeaturePointStatus.VALID),
                          (0.2, 0.3, FeaturePointStatus.VALID)], feature_ids=[96, 41])
    assert overlay.selected_feature_id == 41
    overlay.set_features([(0.4, 0.6, FeaturePointStatus.VALID)], feature_ids=[96])
    assert overlay.selected_feature_id is None
    assert seen == [41, -1]


def test_insert_is_one_click_normalized_and_escape_or_right_click_cancels(qapp):
    overlay = overlay_fixture()
    seen = []
    modes = []
    overlay.feature_inserted.connect(lambda x, y: seen.append((x, y)))
    overlay.insert_mode_changed.connect(modes.append)
    assert overlay.set_insert_mode(True) is False
    overlay.set_interactive(True)
    assert overlay.set_insert_mode(True) is True
    overlay.mousePressEvent(MouseEvent(700, 200))
    assert seen == [(0.7, 0.4)]
    assert not overlay.insert_mode_active
    assert modes == [True, False]
    overlay.set_insert_mode(True)
    overlay.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert not overlay.insert_mode_active
    overlay.set_insert_mode(True)
    overlay.mousePressEvent(MouseEvent(200, 150, Qt.MouseButton.RightButton))
    assert not overlay.insert_mode_active
    assert len(seen) == 1


def test_tracking_or_hiding_resets_selection_insertion_and_prevents_edits(qapp):
    overlay = overlay_fixture()
    overlay.set_interactive(True)
    overlay.select_feature(41)
    overlay.set_insert_mode(True)
    overlay.set_state(OverlayState.TRACKING)
    assert overlay.selected_feature_id is None
    assert not overlay.insert_mode_active
    assert overlay.set_insert_mode(True) is False
    event = MouseEvent(200, 150)
    overlay.mousePressEvent(event)
    assert not event.accepted
    overlay.set_state(OverlayState.TRACKED)
    overlay.select_feature(41)
    overlay.set_insert_mode(True)
    overlay.set_overlay_active(False)
    assert overlay.selected_feature_id is None
    assert not overlay.insert_mode_active
    assert overlay.set_insert_mode(True) is False


def test_insert_outside_frame_does_not_create_point(qapp):
    overlay = overlay_fixture()
    overlay.set_interactive(True)
    overlay.set_insert_mode(True)
    seen = []
    overlay.feature_inserted.connect(lambda x, y: seen.append((x, y)))
    overlay.mousePressEvent(MouseEvent(1200, 200))
    assert seen == []
    assert overlay.insert_mode_active


def test_point_controls_signal_and_run_state(qapp):
    panel = TrackerPanel()
    panel.set_bound_shape("Window 1")
    assert not panel.btn_insert_point.isEnabled()
    panel.interactive_box.setChecked(True)
    assert panel.btn_insert_point.isEnabled()
    assert not panel.btn_set_point.isEnabled()
    panel.set_selected_feature(41)
    assert panel.btn_set_point.isEnabled()
    assert panel.btn_delete_point.isEnabled()
    seen = []
    panel.insert_point_requested.connect(lambda: seen.append("insert"))
    panel.set_point_requested.connect(lambda: seen.append("set"))
    panel.delete_point_requested.connect(lambda: seen.append("delete"))
    panel.btn_insert_point.click()
    panel.btn_set_point.click()
    panel.btn_delete_point.click()
    assert seen == ["insert", "set", "delete"]
    assert panel.btn_insert_point.isChecked()
    panel.set_state(PanelState.RUNNING)
    assert not panel.btn_insert_point.isChecked()
    for button in (panel.btn_insert_point, panel.btn_set_point, panel.btn_delete_point):
        assert not button.isEnabled()
    panel.set_state(PanelState.SUCCESS)
    panel.interactive_box.setChecked(False)
    assert panel._selected_feature_id is None
    assert not panel.btn_set_point.isEnabled()


def test_real_viewer_click_selects_feature_and_inserts_inside_mask(qapp, qtbot, monkeypatch):
    from PyQt6.QtCore import QRectF
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QWidget
    from src.features.color.core.layer import Layer, MaskShape
    from src.features.color.ui import canvas as canvas_module

    # A raster viewport exercises QGraphicsView's real input dispatch without
    # creating a native GL context in a headless UI test.
    monkeypatch.setattr(canvas_module, "GLCanvasViewport", QWidget)
    canvas = canvas_module.Canvas()
    qtbot.addWidget(canvas)
    canvas.resize(640, 400)
    canvas.logical_size = QRectF(0.0, 0.0, 1000.0, 500.0).size()
    canvas.scene.setSceneRect(0.0, 0.0, 1000.0, 500.0)
    shape = MaskShape(MaskShape.RECT)
    shape.center = [0.5, 0.5]
    shape.size = [0.8, 0.8]
    layer = Layer("points")
    layer.mask_shapes = [shape]
    canvas.update_mask_overlay(layer)
    canvas.show_tracker_overlay(1000.0, 500.0, (0.1, 0.1, 0.8, 0.8), (0.0, 0.0, 1.0, 1.0))
    overlay = canvas.tracker_overlay
    overlay.set_features([(0.5, 0.5, FeaturePointStatus.VALID)], feature_ids=[731])
    overlay.set_interactive(True)
    canvas.show()
    canvas.fitInView(canvas.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
    qapp.processEvents()
    selected = []
    inserted = []
    canvas.tracker_feature_selected.connect(selected.append)
    canvas.tracker_feature_inserted.connect(lambda x, y: inserted.append((x, y)))
    # Center overlaps both the mask interior and its transform handle.
    QTest.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton,
                     pos=canvas.mapFromScene(QPointF(500.0, 250.0)))
    assert selected == [731]
    overlay.set_insert_mode(True)
    QTest.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton,
                     pos=canvas.mapFromScene(QPointF(700.0, 300.0)))
    assert len(inserted) == 1
    assert abs(inserted[0][0] - 0.7) < 0.01
    assert abs(inserted[0][1] - 0.6) < 0.01
    assert not overlay.insert_mode_active
    canvas.close()
