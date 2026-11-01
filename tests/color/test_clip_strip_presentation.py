"""Reference clip-strip presentation and navigation behavior."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QPixmap, QShortcut, QWheelEvent

from src.features.color.ui import timeline_clip_strip as ui


def entry(index: int = 0) -> ui.ColorClipEntry:
    return ui.ColorClipEntry(
        timeline_id="timeline", clip_id=f"clip-{index}", track_id="v1",
        track_name="V1", asset_id="asset", asset_name="source.mov",
        asset_path="/source.mov", media_type="video", timeline_start=index * 48,
        timeline_duration=48, source_start=index * 96, source_duration=48,
        color_stack_id=None, color_shared=False, codec_label="H.264 High",
    )


@pytest.fixture
def strip(qtbot, monkeypatch):
    monkeypatch.setattr(ui.ThumbnailProvider, "instance", lambda: MagicMock())
    widget = ui.TimelineClipStrip()
    widget._bottom_mode = ui.BottomLabelMode.CODEC
    qtbot.addWidget(widget)
    widget.resize(640, 147)
    widget._entries = [entry(i) for i in range(16)]
    widget._rebuild_cards()
    widget.show()
    qtbot.waitUntil(lambda: widget._cards[0]._thumb_widget.geometry().bottom()
                   < widget._cards[0]._bottom_label.geometry().top())
    return widget


def test_metadata_does_not_cover_picture(strip):
    card = strip._cards[0]
    assert card._header.geometry().bottom() < card._thumb_widget.geometry().top()
    assert card._thumb_widget.geometry().bottom() < card._bottom_label.geometry().top()
    assert card._header.index_label.text() == "01"
    assert card._header.timecode_label.text() == "00:00:00:00"
    assert card._header.track_label.text() == "V1"
    assert abs(card._thumb_widget.width() / card._thumb_widget.height() - 16 / 9) < 0.02
    assert card._bottom_label.text() == "H.264 High"
    assert card._bottom_label.alignment() & Qt.AlignmentFlag.AlignLeft
    assert not strip._nav_container.isVisible()


def test_selected_border_is_on_picture_only(strip):
    card = strip._cards[0]
    pixmap = QPixmap(160, 90)
    pixmap.fill(QColor("#366044"))
    card.set_preview_override(pixmap)
    strip.set_active_clip(card.entry.clip_id)
    image = card.grab().toImage()
    picture = card._thumb_widget.geometry()
    assert image.pixelColor(picture.left() + 1, picture.center().y()).name() == ui._BORDER_SELECTED
    assert image.pixelColor(picture.center()).name() == "#366044"
    card.set_selected(False)
    plain = card.grab().toImage()
    assert plain.pixelColor(picture.left() + 1, picture.center().y()).name() != ui._BORDER_SELECTED
    # Selecting a clip must not paint the metadata/footer blue.
    assert image.pixelColor(0, 0) == plain.pixelColor(0, 0)
    assert image.pixelColor(0, card.height() - 1) == plain.pixelColor(0, card.height() - 1)
    strip.set_active_clip(card.entry.clip_id)
    assert "#e4e4e6" in card._bottom_label.styleSheet()
    strip.set_active_clip(strip._cards[1].entry.clip_id)
    assert ui._TEXT_SECONDARY in card._bottom_label.styleSheet()


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("device_scale", [1, 2])
def test_picture_is_clipped_to_rounded_border(strip, selected: bool, device_scale: int) -> None:
    card = strip._cards[0]
    picture = QPixmap(160, 90)
    picture.fill(QColor("#e8dd45"))
    card.set_preview_override(picture)
    card.set_selected(selected)
    thumb = card._thumb_widget
    rendered = QPixmap(thumb.width() * device_scale, thumb.height() * device_scale)
    rendered.setDevicePixelRatio(device_scale)
    rendered.fill(Qt.GlobalColor.transparent)
    thumb.render(rendered)
    image = rendered.toImage()

    # A rectangular source must not leak outside any of the rounded corners.
    for x in (0, image.width() - 1):
        for y in (0, image.height() - 1):
            assert image.pixelColor(x, y).name() == ui._BG_DARK

    # The outline meets the outer edge; there is no source-colored strip
    # outside an inset border, or a second dark line inside the picture.
    middle = image.height() // 2
    border = ui._BORDER_SELECTED if selected else "#131315"
    assert image.pixelColor(0, middle).name() == border
    border_width = 2 if selected else 1
    assert image.pixelColor(border_width * device_scale, middle).name() == "#e8dd45"


def test_grade_appears_on_number_badge_and_survives_clip_switch(strip):
    strip._entries = [replace(entry(0), color_graded=True), entry(1)]
    strip._rebuild_cards()
    graded, plain = strip._cards
    strip.set_active_clip("clip-1")
    assert graded._header.index_label.property("graded")
    assert not plain._header.index_label.property("graded")

    def colored_border_pixels(card):
        image = card._header.index_label.grab().toImage()
        return sum(max(c.red(), c.green(), c.blue()) - min(c.red(), c.green(), c.blue()) > 30
                   for x in range(image.width()) for y in range(image.height())
                   for c in [image.pixelColor(x, y)])

    assert colored_border_pixels(graded) > 10
    assert colored_border_pixels(plain) == 0
    strip._entries = [entry(0), entry(1)]
    strip._rebuild_cards()
    assert not strip._cards[0]._header.index_label.property("graded")


def test_click_header_activates_once_and_focuses_strip(strip, qtbot):
    received = []
    strip.clip_activated.connect(lambda *args: received.append(args))
    card = strip._cards[2]
    qtbot.mouseClick(card._header, Qt.MouseButton.LeftButton)
    assert received == [("timeline", "clip-2", 96, 192)]
    assert strip.focusPolicy() == Qt.FocusPolicy.StrongFocus


def test_external_selection_scrolls_into_view(strip):
    strip.set_active_clip("clip-15")
    bar = strip._scroll_area.horizontalScrollBar()
    assert bar.value() > 0
    card = strip._cards[-1]
    assert card.x() >= bar.value()
    assert card.x() + card.width() <= bar.value() + strip._scroll_area.viewport().width()
    strip.set_active_clip("clip-0")
    assert bar.value() == 0


def test_thumbnail_size_can_grow_after_shrinking(strip, qtbot):
    original_height = strip._cards[0].height()
    strip._set_card_size(ui.CardSize.SMALL)
    qtbot.waitUntil(lambda: strip._cards[0].height() < original_height)
    strip._set_card_size(ui.CardSize.LARGE)
    qtbot.waitUntil(lambda: strip._cards[0].height() > original_height)
    assert strip._cards[0]._thumb_widget.width() > 170


def test_scrollbar_only_shows_for_overflow(strip, qtbot):
    bar = strip._scroll_area.horizontalScrollBar()
    qtbot.waitUntil(bar.isVisible)
    assert bar.maximum() > 0
    strip._entries = [entry(0), entry(1)]
    strip._rebuild_cards()
    qtbot.waitUntil(lambda: not bar.isVisible())
    assert bar.maximum() == 0
    strip._entries = [entry(i) for i in range(16)]
    strip._rebuild_cards()
    qtbot.waitUntil(bar.isVisible)
    assert bar.pageStep() < strip._container.width()


@pytest.mark.parametrize("over_picture", [False, True])
def test_vertical_mouse_wheel_scrolls_horizontally(strip, qtbot, over_picture):
    viewport = strip._scroll_area.viewport()
    event = QWheelEvent(QPointF(20, 30), QPointF(viewport.mapToGlobal(QPoint(20, 30))),
                        QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    from PyQt6.QtWidgets import QApplication
    QApplication.sendEvent(strip._cards[0]._thumb_widget if over_picture else viewport, event)
    assert strip._scroll_area.horizontalScrollBar().value() > 0
    assert strip._scroll_area.verticalScrollBar().value() == 0


def test_keyboard_navigation_uses_strip_focus(strip, qtbot):
    strip.activateWindow()
    strip.setFocus()
    strip.set_active_clip("clip-0")
    qtbot.keyClick(strip, Qt.Key.Key_Down)
    assert strip._active_clip_id == "clip-1"
    qtbot.keyClick(strip, Qt.Key.Key_Up)
    assert strip._active_clip_id == "clip-0"
    assert all(shortcut.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut
               for shortcut in strip.findChildren(QShortcut))


@pytest.mark.parametrize("metadata,expected", [
    ({"codec_name": "h264", "codec_profile": "High"}, "H.264 High"),
    ({"codec": "hevc", "codec_profile": "Main 10"}, "H.265 Main 10"),
    ({"codec_name": "prores", "codec_profile": "HQ"}, "ProRes 422 HQ"),
    ({}, ""),
    ({"codec": 123, "codec_profile": []}, ""),
])
def test_codec_comes_from_imported_metadata(metadata, expected):
    assert ui._asset_codec_label(SimpleNamespace(metadata=metadata, path="/test.mov")) == expected


def test_unknown_container_does_not_claim_to_be_prores(strip):
    card = ui.TimelineClipCard(replace(entry(), codec_label=""))
    assert card._bottom_label.text() != "ProRes"
    card.deleteLater()


@pytest.mark.parametrize("change", [
    {"source_start": 12}, {"asset_path": "/replacement.mov"},
    {"color_stack_id": "new-stack"}, {"color_graded": False},
])
def test_changed_source_or_reset_invalidates_old_grade_snapshot(strip, monkeypatch, change):
    old = replace(entry(0), color_graded=True)
    untouched = entry(1)
    strip._all_entries = [old, untouched]
    strip._current_timeline_id = "timeline"
    strip._graded_preview_cache = {"clip-0": QPixmap(32, 18), "clip-1": QPixmap(32, 18)}
    monkeypatch.setattr(ui, "build_clip_entries", lambda *args: [replace(old, **change), untouched])
    strip.set_project_and_timeline(None, SimpleNamespace(timeline_id="timeline", fps=24.0))
    assert "clip-0" not in strip._graded_preview_cache
    assert "clip-1" in strip._graded_preview_cache


def test_pasted_shared_stack_discards_all_affected_snapshots(strip):
    strip._all_entries = [replace(entry(0), color_stack_id="shared"),
                          replace(entry(1), color_stack_id="shared"),
                          replace(entry(2), color_stack_id="other")]
    strip._graded_preview_cache = {f"clip-{i}": QPixmap(32, 18) for i in range(3)}
    strip.invalidate_graded_preview_stacks({"shared"})
    assert set(strip._graded_preview_cache) == {"clip-2"}
