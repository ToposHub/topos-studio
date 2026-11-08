"""Render the real monitor chrome with lightweight, empty canvas doubles."""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication
from src.features.edit.ui.edit_viewport import EditViewport
from src.shared.ui.style_sheet import DARK_THEME


def main() -> None:
    """Save UI layout evidence; no decoder, media or GPU renderer is started."""
    helpers = runpy.run_path(str(ROOT / "tests/edit/test_edit_viewport.py"))
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(DARK_THEME)
    program = helpers["_Program"]()
    source = helpers["_Source"]()
    widget = EditViewport(program, source)
    for canvas in (source.viewer, program.get_viewer()):
        canvas.setStyleSheet("background: #191919;")
        canvas.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    program.get_toolbar().set_timeline_choices(
        {"timeline": SimpleNamespace(name="Timeline 1")}, "timeline")
    source.toolbar.set_source_name("Camera A.mov")
    source.toolbar.set_timecode("00:01:12:08", 25.0)
    source.toolbar.set_duration("00:02:35:00")
    program.get_transport_bar().update_timecode("01:00:16:00")
    widget.set_program_range(3125, 25.0)
    widget.set_program_position(400)
    for width, height, dual in (
        (1364, 840, False), (1364, 840, True),
        (1280, 720, False), (1280, 720, True),
        (800, 480, False), (800, 480, True),
        (640, 400, True), (320, 300, False),
    ):
        widget.resize(width, height)
        widget.set_dual_view(dual)
        widget.show()
        app.processEvents()
        assert (widget.width(), widget.height()) == (width, height)
        output = Path(__file__).parent / f"{width}x{height}_{'dual' if dual else 'single'}.png"
        assert widget.grab().save(str(output))
        print(output.name)
        if width == 1364 and not dual:
            # Present the actual controls together without a large empty
            # video rectangle; each strip retains its native logical size.
            detail = QPixmap(width, 120)
            detail.fill(QColor("#191919"))
            painter = QPainter(detail)
            painter.drawPixmap(0, 0, widget.program_header.grab())
            painter.drawPixmap(0, 62, widget.program_slider.grab())
            painter.drawPixmap(0, 86, widget.program_footer.grab())
            painter.end()
            assert detail.save(str(Path(__file__).parent / "chrome_preview.png"))
    widget.close()


if __name__ == "__main__":
    main()
