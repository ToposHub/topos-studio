"""Render Color's actual UI chrome with DARK_THEME and a non-GL canvas.

Run from any directory using the project's Python environment.  The script
locates its checkout from this file and writes the PNGs beside itself.
"""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace


def main() -> None:
    output = Path(__file__).resolve().parent
    project = output.parents[2]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_SCALE_FACTOR", "1")
    os.environ.setdefault("TOPOS_TEST_MODE", "1")
    sys.path.insert(0, str(project))

    from PyQt6.QtCore import QPoint, QRect, Qt
    from PyQt6.QtGui import QBrush, QColor, QImage, QPainter
    from PyQt6.QtWidgets import QApplication, QGraphicsScene, QVBoxLayout, QWidget

    from src.features.color.ui.color_viewport_chrome import ColorViewportChrome
    from src.features.color.ui.gl_viewport import VIEWER_BACKGROUND
    from src.shared.ui.style_sheet import DARK_THEME

    test_helpers = runpy.run_path(str(project / "tests/color/test_color_viewport_chrome.py"))
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_THEME)
    window = QWidget()
    window.setWindowTitle("Color Viewport — empty canvas UI validation")
    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    canvas = test_helpers["_CanvasHost"](window)
    canvas.setFrameStyle(0)
    canvas.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    canvas.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    canvas.setScene(QGraphicsScene(canvas))
    backdrop = QColor.fromRgbF(*VIEWER_BACKGROUND)
    canvas.setBackgroundBrush(QBrush(backdrop))
    chrome = ColorViewportChrome(canvas)
    layout.addWidget(canvas.viewport_menu)
    layout.addWidget(canvas, 1)
    canvas.viewport_menu.set_timeline_choices({"timeline": SimpleNamespace(name="Timeline 1")}, "timeline")
    canvas.video_control_bar.show_for_video()
    canvas.video_control_bar.set_total_frames(1000)
    canvas.video_control_bar.set_current_frame(400)
    chrome.update_zoom(0.7)
    for width, height in ((1000, 600), (700, 480), (500, 350)):
        window.resize(width, height)
        window.show()
        app.processEvents()
        canvas.update_video_control_bar_position()
        chrome.position_title()
        app.processEvents()
        image = window.grab().toImage()
        assert window.size().width() == width, "UI minimum width expanded unexpectedly"
        assert chrome.toolbar.height() == 30
        assert chrome.transport.slider.height() == 20
        assert chrome.row.height() == 36
        assert not chrome.toolbar._timeline_selector.isHidden()
        assert chrome.toolbar._timeline_selector.geometry().center().x() == chrome.toolbar.rect().center().x()
        assert image.pixelColor(width // 2, height // 2).name() == "#191919"
        destination = output / f"color_viewport_{width}x{height}.png"
        if not image.save(str(destination)):
            raise RuntimeError(f"Could not save {destination}")
        print(destination)
        if width == 1000:
            # Native-scale chrome strips; no resampling or fabricated controls.
            detail = QImage(width, 92, QImage.Format.Format_ARGB32)
            detail.fill(backdrop)
            painter = QPainter(detail)
            painter.drawImage(QPoint(0, 0), image, QRect(0, 0, width, 30))
            painter.drawImage(QPoint(0, 36), image, QRect(0, height - 56, width, 56))
            painter.end()
            detail.save(str(output / "chrome_detail.png"))
    window.hide()
    window.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    main()
