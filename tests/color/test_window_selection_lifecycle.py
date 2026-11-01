"""Window creation/deletion keeps real Canvas selection and numeric editing aligned."""
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QWidget

from src.features.color.ui.canvas import Canvas
from src.features.color.ui.mask_panel import MaskPanel
from tests.app.test_color_controller_coverage import _ColorHost

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.mark.parametrize("kind", ["circle", "rect", "gradient"])
def test_add_window_stays_selected_and_accepts_numeric_edit(qapp, kind: str) -> None:
    host = _ColorHost()
    with patch("src.features.color.ui.canvas.GLCanvasViewport", QWidget):
        canvas = Canvas()
    host.canvas = canvas
    canvas.logical_size = QSize(1000, 600)
    panel = MaskPanel()
    host.mask_panel = panel
    panel.set_image_only_tools_visible(False)
    panel.shape_updated.connect(host.on_mask_shape_updated)
    panel.shape_selected.connect(host.on_panel_shape_selected)
    host.on_mask_shape_added(kind)
    shape = host.engine.layers[0].mask_shapes[-1]
    if kind == "circle":
        assert shape.size == pytest.approx([.15, .25])
        assert shape.feather == pytest.approx(4.13)
    elif kind == "rect":
        assert shape.size == [.4, .4]
        assert shape.edge_softness == [1.25] * 4
    assert canvas.active_shape_id == shape.id
    assert canvas.mask_items[shape.id].isSelected()
    assert panel.video_panel._selected_id == shape.id
    assert panel.video_panel.controls["pan"].isEnabled()
    panel.video_panel.controls["pan"].setValue(60.0)
    host.process_mask_update()
    assert shape.center[0] == pytest.approx(0.6)
    assert canvas.active_shape_id == shape.id
    assert canvas.mask_items[shape.id].isSelected()
    host.mask_update_timer.stop()
    canvas.close()
    host.close()


def test_deleting_other_window_keeps_selected_canvas_shape() -> None:
    host = _ColorHost()
    with patch("src.features.color.ui.canvas.GLCanvasViewport", QWidget):
        canvas = Canvas()
    host.canvas = canvas
    canvas.logical_size = QSize(1000, 600)
    host.on_mask_shape_added("circle")
    first = host.engine.layers[0].mask_shapes[-1]
    host.on_mask_shape_added("rect")
    active = host.engine.layers[0].mask_shapes[-1]
    host.on_mask_shape_removed(first.id)
    assert canvas.active_shape_id == active.id
    assert canvas.mask_items[active.id].isSelected()
    assert first.id not in canvas.mask_items
    canvas.close()
    host.close()
