from __future__ import annotations

from PyQt6.QtWidgets import QApplication, QMainWindow

from src.app.controllers.dock_layout_controller import DockLayoutMixin


def _clamp(qapp: QApplication, x: int, y: int, w: int, h: int) -> tuple:
    window = QMainWindow()
    window.setGeometry(x, y, w, h)
    DockLayoutMixin._ensure_window_on_screen(window)
    geom = window.geometry()
    window.deleteLater()
    return geom.x(), geom.y(), geom.width(), geom.height()


def test_oversized_saved_geometry_is_clamped_to_screen(qapp):
    screen = qapp.primaryScreen().geometry()
    x, y, w, h = _clamp(qapp, -460, -1206, screen.width() + 900, screen.height() + 2000)

    assert w <= screen.width()
    assert h <= screen.height()
    assert x >= screen.left()
    assert y >= screen.top()
    assert x + w <= screen.right() + 1
    assert y + h <= screen.bottom() + 1


def test_offscreen_window_is_moved_onto_a_screen(qapp):
    screen = qapp.primaryScreen().geometry()
    w, h = 400, 300
    x, y, _, _ = _clamp(qapp, screen.right() + 100, screen.bottom() + 100, w, h)

    assert screen.left() <= x <= screen.right()
    assert screen.top() <= y <= screen.bottom()


def test_fit_window_is_left_untouched(qapp):
    screen = qapp.primaryScreen().geometry()
    original = (10, 10, 600, 400)
    result = _clamp(qapp, *original)

    assert result == original


def test_geometry_from_vanished_secondary_monitor_recovers(qapp):
    """F1.4：布局保存在已拔掉的右侧副屏（主屏外大偏移）→ 恢复回落主屏。"""
    screen = qapp.primaryScreen().geometry()
    w, h = 1280, 800
    x, y = screen.right() + 1920, 200  # 保存时副屏存在，现已拔掉
    nx, ny, nw, nh = _clamp(qapp, x, y, w, h)
    assert screen.left() <= nx and nx + nw <= screen.right() + 1
    assert screen.top() <= ny and ny + nh <= screen.bottom() + 1
