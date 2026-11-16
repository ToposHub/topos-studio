"""Full-frame gallery rendering keeps source edges in portrait and wide media."""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QStyleOptionViewItem, QWidget

from src.app.ui.native_files_gallery import FilesCardDelegate
from src.app.ui.native_files_panel import FilesEntryModel


@pytest.mark.parametrize("size", [(80, 160), (240, 80)])
def test_thumbnail_keeps_both_source_edges(qtbot: QtBot, size: tuple[int, int]) -> None:
    widget = QWidget()
    qtbot.addWidget(widget)
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.green)
    painter = QPainter(image)
    if size[1] > size[0]:
        painter.fillRect(0, 0, size[0], 16, Qt.GlobalColor.red)
        painter.fillRect(0, size[1] - 16, size[0], 16, Qt.GlobalColor.blue)
    else:
        painter.fillRect(0, 0, 24, size[1], Qt.GlobalColor.red)
        painter.fillRect(size[0] - 24, 0, 24, size[1], Qt.GlobalColor.blue)
    painter.end()
    model = FilesEntryModel(widget)
    model.set_entries([{"name": "frame.png", "path": "/fixture/frame.png", "type": "image"}])
    delegate = FilesCardDelegate(FilesEntryModel.ENTRY_ROLE, lambda _: image, widget)
    index = model.index(0, 0)
    output = QImage(delegate.sizeHint(None, index), QImage.Format.Format_RGB32)
    output.fill(Qt.GlobalColor.black)
    option = QStyleOptionViewItem()
    option.rect = QRect(output.rect())
    painter = QPainter(output)
    delegate.paint(painter, option, index)
    painter.end()
    colors = {output.pixelColor(x, y).name()
              for x in range(output.width()) for y in range(output.height())}
    # Edge markers vanish with cover/crop scaling, even when the center looks correct.
    assert {"#ff0000", "#0000ff"} <= colors
