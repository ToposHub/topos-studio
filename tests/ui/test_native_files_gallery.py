"""Full-frame gallery rendering keeps source edges in portrait and wide media."""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtCore import QRect, QSettings, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QMenu, QStyleOptionViewItem, QWidget

from src.app.ui.native_files_gallery import FilesCardDelegate
from src.app.ui.native_files_panel import FilesEntryModel
from src.features.files.contracts import Entry, UserMetadata
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


def test_library_card_ratings_hide_together_and_can_move_below_names(qtbot: QtBot, tmp_path) -> None:
    from src.app.ui.native_files_panel import NativeFilesPanel
    from src.app.ui.native_files_prefs import NativeFilesPreferences

    settings_path = str(tmp_path / "files.ini")
    prefs = NativeFilesPreferences(QSettings(settings_path, QSettings.Format.IniFormat))
    panel = NativeFilesPanel(home_path=str(tmp_path), preferences=prefs)
    qtbot.addWidget(panel)
    entries = [
        Entry(str(tmp_path / name), name, "image", user=UserMetadata(rating=rating))
        for name, rating in (("wide.jpg", 3), ("portrait.jpg", 5))
    ]
    panel.model.set_entries(entries)
    panel.resize(950, 600)
    panel.show()
    view = panel.icon_view
    delegate = panel.card_delegate
    qtbot.waitUntil(lambda: view.visualRect(panel.proxy.index(1, 0)).isValid())
    assert delegate.show_rating_badges
    view.setAspectRatio(entries[1].path, 0.2)
    qtbot.waitUntil(lambda: not delegate.show_rating_badges)
    view.setAspectRatio(entries[1].path, 1.2)
    qtbot.waitUntil(lambda: delegate.show_rating_badges)

    menu = QMenu(panel)
    panel._card_display_menu(menu)
    rating = next(action for action in menu.actions()
                  if action.text() == I18N.tr("files.native.card.rating", "星级评分"))
    assert not rating.isChecked()
    rating.trigger()
    assert rating.isChecked()
    assert delegate.rating_at_bottom
    panel._set_card_metadata(frozenset({"rating", "resolution", "size", "tags"}))
    rect = view.visualRect(panel.proxy.index(0, 0))
    assert delegate.name_rect(rect).height() == 20
    restored = NativeFilesPanel(
        home_path=str(tmp_path),
        preferences=NativeFilesPreferences(QSettings(settings_path, QSettings.Format.IniFormat)),
    )
    qtbot.addWidget(restored)
    assert restored.card_delegate.rating_at_bottom
    assert restored.card_delegate.caption_fields == {"rating", "resolution", "size", "tags"}
