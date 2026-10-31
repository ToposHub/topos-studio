"""阶段 8：透视 overlay 四角、边中点和锁定交互。"""

import pytest

pytestmark = pytest.mark.usefixtures("qapp")

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtWidgets import QGraphicsScene

from src.features.color.ui.tracker_overlay import (
    OverlayState,
    TrackerOverlay,
)


class FakeMouseEvent:
    def __init__(self, pos, button=Qt.MouseButton.LeftButton):
        self._pos = QPointF(*pos)
        self._button = button
        self.accepted = False

    def scenePos(self):
        return self._pos

    def button(self):
        return self._button

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


def make_overlay():
    overlay = TrackerOverlay()
    scene = QGraphicsScene()
    scene.addItem(overlay)
    overlay._test_scene = scene
    overlay.set_frame_size(1000, 500)
    overlay.set_pattern_region((0.3, 0.3, 0.4, 0.3))
    overlay.set_search_region((0.2, 0.2, 0.6, 0.5))
    overlay.set_tracking_mode("homography")
    overlay.set_homography_corners([
        (0.25, 0.3), (0.75, 0.25), (0.8, 0.7), (0.2, 0.75)
    ])
    overlay.set_overlay_active(True)
    return overlay


def test_homography_mode_exposes_corner_and_edge_handles(qapp):
    overlay = make_overlay()
    assert overlay.is_homography_mode
    assert len(overlay._homography_handles) == 8
    assert all(handle.isVisible() for handle in overlay._homography_handles)
    assert overlay.homography_corners()[0] == pytest.approx((0.25, 0.3))


def test_corner_drag_commits_full_corner_list(qapp):
    overlay = make_overlay()
    commits = []
    overlay.homography_corners_committed.connect(commits.append)
    overlay.mousePressEvent(FakeMouseEvent((250, 150)))
    overlay.mouseMoveEvent(FakeMouseEvent((270, 160)))
    assert overlay.homography_corners()[0] == pytest.approx((0.27, 0.32))
    assert commits == []
    overlay.mouseReleaseEvent(FakeMouseEvent((270, 160)))
    assert len(commits) == 1
    assert commits[0][0] == pytest.approx((0.27, 0.32))
    assert overlay.state == OverlayState.TRACKED


def test_locked_corner_does_not_start_drag(qapp):
    overlay = make_overlay()
    overlay.set_homography_corner_locked(0, True)
    assert overlay._hit_part(QPointF(250, 150)) is None
    assert 0 in overlay.locked_homography_corners()
