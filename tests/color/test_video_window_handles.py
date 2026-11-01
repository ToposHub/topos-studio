"""Video window cages keep screen-sized targets and commit real geometry."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QPointF, QSize
from PyQt6.QtWidgets import QGraphicsScene, QWidget

from src.features.color.ui.canvas import Canvas, MaskShapeItem

pytestmark = pytest.mark.usefixtures('qapp')


@pytest.fixture
def window_item():
    items = []

    def make(kind: str = 'circle', video: bool = True):
        scene = QGraphicsScene()
        canvas = SimpleNamespace(
            logical_size=QSize(1000, 600), active_shape_id='window',
            _video_window_mode=video, mask_shape_changed=MagicMock(),
            interaction_started=MagicMock(), interaction_ended=MagicMock(),
        )
        item = MaskShapeItem('window', kind, canvas)
        scene.addItem(item)
        item.update_from_data(QPointF(500, 300), [120, 80], 0.0, 4.13)
        items.append((scene, item))
        return item, canvas

    yield make
    for scene, item in items:
        item._sync_timer.stop()
        scene.clear()


def test_video_circle_has_eight_size_handles_and_internal_rotation(window_item):
    item, _ = window_item()
    sizes = [key for key in item.handles if key.startswith('resize_')]
    assert len(sizes) == 8
    assert item.handles['resize_tl'].pos() == QPointF(-120, -80)
    assert item.handles['resize_br'].pos() == QPointF(120, 80)
    assert item.handles['rotate'].pos() == QPointF(0, -40)
    handle = item.handles['resize_br']
    assert not handle.rect().contains(QPointF(7, 0))
    assert handle.shape().contains(QPointF(7, 0))


@pytest.mark.parametrize('kind', ['circle', 'rect'])
def test_corner_resize_preserves_ratio_and_updates_other_handles(window_item, kind):
    item, canvas = window_item(kind)
    corner = item.handles['resize_br'].pos()
    item.handles['resize_br'].setPos(corner * 2.0)
    assert item.size == pytest.approx([240, 160])
    assert item.size[0] / item.size[1] == pytest.approx(1.5)
    factor = 1.0 if kind == 'circle' else .5
    assert item.handles['resize_w'].pos() == QPointF(240 * factor, 0)
    assert item.handles['resize_tl'].pos() == QPointF(-240 * factor, -160 * factor)
    assert item.handles['rotate'].pos() == QPointF(0, -80 * factor)
    assert canvas.mask_shape_changed.emit.call_args.args[1]['size'] == pytest.approx([.24, 160 / 600])


def test_axis_resize_updates_corner_and_opposite_handle_during_drag(window_item):
    item, _ = window_item()
    item.handles['resize_w']._is_dragging = True
    item.handles['resize_w'].setPos(180, 0)
    assert item.size == [180, 80]
    assert item.handles['resize_w_neg'].pos() == QPointF(-180, 0)
    assert item.handles['resize_tr'].pos() == QPointF(180, -80)
    item.handles['resize_w']._is_dragging = False


def test_rectangle_feather_commits_only_the_dragged_softness_edge(window_item):
    item, canvas = window_item('rect')
    item.update_from_data(QPointF(500, 300), [240, 160], 0.0, 4.13,
                          edge_softness=[1.0, 2.0, 3.0, 4.0])
    assert item.handles['feather_l'].pos() == QPointF(-125, 0)
    assert item.handles['feather_r'].pos() == QPointF(130, 0)
    assert item.handles['feather_t'].pos() == QPointF(0, -95)
    assert item.handles['feather_b'].pos() == QPointF(0, 100)
    item.handles['feather_l'].setPos(-140, 0)
    assert item.edge_softness == [4.0, 2.0, 3.0, 4.0]
    assert item.feather == 4.13
    assert canvas.mask_shape_changed.emit.call_args.args[1]['edge_softness'] == [4.0, 2.0, 3.0, 4.0]


def test_circle_feather_uses_image_width_and_moves_all_four_handles(window_item):
    item, canvas = window_item()
    item.handles['feather_r'].setPos(160, 0)
    assert item.feather == pytest.approx(10.0)
    assert item.handles['feather_l'].pos() == QPointF(-160, 0)
    assert item.handles['feather_t'].pos() == QPointF(0, -120)
    assert canvas.mask_shape_changed.emit.call_args.args[1]['feather'] == pytest.approx(10.0)


def test_video_mode_reuses_cage_and_image_mode_keeps_old_handles(window_item):
    item, _ = window_item(video=False)
    assert 'resize_br' not in item.handles
    assert item.handles['rotate'].pos() == QPointF(0, -100)
    item.set_video_window_mode(True)
    corner = item.handles['resize_br']
    item.set_video_window_mode(True)
    assert item.handles['resize_br'] is corner
    item.set_video_window_mode(False)
    assert not corner.isVisible()
    assert item.handles['rotate'].pos() == QPointF(0, -100)
    assert item.handles['resize_w'].rect().width() == 8.0


def test_gradient_center_tracks_endpoint_changes_and_translation(window_item):
    item, canvas = window_item('gradient')
    item.update_from_data(QPointF(500, 300), [120, 80], 0.0, 0.0,
                          grad_points=[[.4, .5], [.6, .5]])
    assert item.handles['center'].pos() == QPointF(0, 0)
    item.handles['grad_end'].setPos(200, 0)
    assert item.handles['center'].pos() == QPointF(50, 0)
    item.moveBy(20, 30)
    item._sync_back()
    assert canvas.mask_shape_changed.emit.call_args.args[1]['grad_points'] == [[.42, .55], [.72, .55]]


def test_canvas_explicit_video_style_reaches_existing_and_new_items(qapp):
    with patch('src.features.color.ui.canvas.GLCanvasViewport', QWidget):
        canvas = Canvas()
    canvas.logical_size = QSize(1000, 600)
    item = MaskShapeItem('first', 'rect', canvas)
    canvas.scene.addItem(item)
    canvas.mask_items['first'] = item
    canvas.set_video_window_mode(True)
    assert item._video_window_mode
    assert MaskShapeItem('next', 'circle', canvas)._video_window_mode
    canvas.set_video_window_mode(False)
    assert not item._video_window_mode
    canvas.close()
