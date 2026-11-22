"""Full-frame gallery rendering keeps source edges in portrait and wide media."""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QStyleOptionViewItem, QWidget

from src.app.ui.native_files_gallery import FilesCardDelegate
from src.app.ui.native_files_panel import FilesEntryModel
from src.shared.i18n.i18n import I18N


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


def test_grade_badge_is_drawn_only_for_a_confirmed_photo(qtbot: QtBot) -> None:
    class _TextPainter(QPainter):
        def __init__(self, output: QImage) -> None:
            super().__init__(output)
            self.labels: list[str] = []

        def drawText(self, rect, alignment, text) -> None:
            self.labels.append(alignment if isinstance(alignment, str) else text)
            super().drawText(rect, alignment, text)

    widget = QWidget()
    qtbot.addWidget(widget)
    model = FilesEntryModel(widget)
    model.set_entries([
        {"name": "photo.dng", "path": "/fixture/photo.dng", "type": "image"},
        {"name": "photo.jpg", "path": "/fixture/photo.jpg", "type": "image"},
        {"name": "sample.mov", "path": "/fixture/sample.mov", "type": "video"},
    ])
    delegate = FilesCardDelegate(
        FilesEntryModel.ENTRY_ROLE, lambda _: None, widget,
        is_graded=lambda path: not path.endswith(".jpg"))
    for row, expected in enumerate((True, False, False)):
        index = model.index(row, 0)
        output = QImage(delegate.sizeHint(None, index), QImage.Format.Format_RGB32)
        output.fill(Qt.GlobalColor.black)
        option = QStyleOptionViewItem()
        option.rect = QRect(output.rect())
        painter = _TextPainter(output)
        delegate.paint(painter, option, index)
        painter.end()
        assert (I18N.tr("files.native.graded", "Graded") in painter.labels) is expected
