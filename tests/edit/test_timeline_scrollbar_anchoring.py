"""Independent timeline scrollbars stay anchored to their visible regions."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPoint, QPointF, QRect, Qt
from PyQt6.QtWidgets import QApplication, QScrollBar, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, SubtitleClip, TimelineSequence
from src.features.edit.ui.timeline_items import RULER_HEIGHT
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.media.metadata import Rational
from src.shared.ui.style_sheet import DARK_THEME

pytestmark = pytest.mark.edit_ui


def _sequence(overflow: bool = True) -> TimelineSequence:
    count = 8 if overflow else 1
    video = tuple(EditTrack(f"v{i}", f"V{i}", "video", i) for i in range(count))
    audio = tuple(EditTrack(f"a{i}", f"A{i}", "audio", i) for i in range(count))
    video = (EditTrack("v0", "V0", "video", 0, clips=(ClipInstance(
        "long-clip", "unknown-media", "v0", 0, 4000, 0, 4000,
    ),)),) + video[1:]
    subtitles = tuple(EditTrack(f"s{i}", f"S{i}", "subtitle", i, subtitle_clips=(SubtitleClip(
        clip_id=f"cue{i}", track_id=f"s{i}", timeline_start=0, timeline_duration=4000, text="Caption",
    ),)) for i in range(count))
    return TimelineSequence("scroll-anchors", "Anchors", fps=Rational(24, 1),
                            video_tracks=video, audio_tracks=audio, subtitle_tracks=subtitles)


def _hosted_view(qtbot: QtBot, *, overflow: bool = True) -> tuple[QWidget, TimelineView]:
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(7, 9, 11, 13)
    view = TimelineView()
    view.setStyleSheet(DARK_THEME + view.styleSheet())
    layout.addWidget(view)
    view.load_timeline(_sequence(overflow))
    view.set_zoom(3.0)
    host.resize(1018, 440)
    host.show()
    return host, view


def _bars(view: TimelineView) -> dict[str, QScrollBar]:
    return {"video": view._video_v_scroll, "audio": view._audio_v_scroll,
            "subtitle": view._subtitle_v_scroll}


def _assert_anchors(view: TimelineView) -> None:
    area = view._layout_engine.area_layout()
    regions = {"video": area.video_area, "audio": area.audio_area, "subtitle": area.subtitle_area}
    viewport = view.viewport()
    for name, bar in _bars(view).items():
        assert bar.isVisible(), f"Fixture must overflow the {name} region"
        assert bar.isEnabled()
        # Compare in viewport coordinates regardless of which widget owns
        # the bar. This catches native viewport.scroll moving its children.
        rect = QRect(bar.mapTo(view, QPoint()) - viewport.mapTo(view, QPoint()), bar.size())
        assert rect.right() == viewport.rect().right() - 1
        top = view.mapFromScene(QPointF(0, regions[name].top())).y()
        bottom = view.mapFromScene(QPointF(0, regions[name].bottom())).y()
        assert rect.top() == max(RULER_HEIGHT, top)
        assert rect.bottom() == min(viewport.height(), bottom) - 1


def test_scrollbars_stay_at_region_edges_through_native_scroll_and_layout_changes(
    qapp: QApplication, qtbot: QtBot,
) -> None:
    host, view = _hosted_view(qtbot)
    qapp.processEvents()
    _assert_anchors(view)
    before = {name: QRect(bar.mapTo(view, QPoint()), bar.size()) for name, bar in _bars(view).items()}
    assert view.horizontalScrollBar().maximum() > 200
    for offset in (70, 180, 0):
        # Drive the real QGraphicsView scrollbar; do not call the scrollbar
        # geometry synchronizer after it, which would hide the drift bug.
        view.horizontalScrollBar().setValue(offset)
        qapp.processEvents()
        assert {name: QRect(bar.mapTo(view, QPoint()), bar.size())
                for name, bar in _bars(view).items()} == before
        _assert_anchors(view)

    view.set_zoom(4.0)
    qapp.processEvents()
    _assert_anchors(view)
    state = view.layout_state()
    state["video_area_ratio"] = 0.65
    view.set_layout_state(state)
    host.resize(1118, 477)
    qapp.processEvents()
    _assert_anchors(view)
    local = {name: bar.mapTo(view, QPoint()) for name, bar in _bars(view).items()}
    global_origin = view.mapToGlobal(QPoint())
    global_positions = {name: bar.mapToGlobal(QPoint()) for name, bar in _bars(view).items()}
    host.move(host.pos() + QPoint(63, 47))
    qapp.processEvents()
    delta = view.mapToGlobal(QPoint()) - global_origin
    assert not delta.isNull()
    for name, bar in _bars(view).items():
        assert bar.mapTo(view, QPoint()) == local[name]
        assert bar.mapToGlobal(QPoint()) - global_positions[name] == delta
    _assert_anchors(view)


def test_horizontal_scrollbar_blends_with_canvas_and_vertical_keeps_track(
    qapp: QApplication, qtbot: QtBot,
) -> None:
    host, view = _hosted_view(qtbot)
    qapp.processEvents()
    horizontal = view.horizontalScrollBar()
    vertical = view._video_v_scroll
    assert horizontal.isVisible() and vertical.isVisible()
    horizontal.setValue(horizontal.maximum())
    vertical.setValue(vertical.minimum())
    qapp.processEvents()

    pixmap = host.grab()
    image = pixmap.toImage()
    ratio = pixmap.devicePixelRatio()

    def rendered_color(bar: QScrollBar, point: QPoint) -> str:
        pos = bar.mapTo(host, point)
        return image.pixelColor(round(pos.x() * ratio), round(pos.y() * ratio)).name()

    assert rendered_color(horizontal, QPoint(5, horizontal.height() // 2)) == "#27272b"
    assert rendered_color(horizontal, QPoint(horizontal.width() - 8, horizontal.height() // 2)) == "#34363e"
    assert rendered_color(vertical, QPoint(vertical.width() // 2, vertical.height() - 3)) == "#1b1c21"


@pytest.mark.parametrize("region", ["video", "audio", "subtitle"])
def test_native_scrollbar_drag_changes_only_its_own_region(
    qapp: QApplication, qtbot: QtBot, region: str,
) -> None:
    _, view = _hosted_view(qtbot)
    qapp.processEvents()
    _assert_anchors(view)
    for bar in _bars(view).values():
        bar.setValue((bar.minimum() + bar.maximum()) // 2)
    qtbot.waitUntil(lambda: not view._scroll_layout_timer.isActive())
    before = view.layout_state()
    bar = _bars(view)[region]
    option = QStyleOptionSlider()
    bar.initStyleOption(option)
    handle = bar.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar, option,
                                       QStyle.SubControl.SC_ScrollBarSlider, bar)
    assert handle.isValid()
    start = handle.center()
    qtbot.mousePress(bar, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(bar, pos=start + QPoint(0, 12))
    qtbot.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=start + QPoint(0, 12))
    key = f"{region}_scroll_offset"
    qtbot.waitUntil(lambda: view.layout_state()[key] != before[key])
    qtbot.waitUntil(lambda: not view._scroll_layout_timer.isActive())
    after = view.layout_state()
    assert after[key] == bar.value()
    assert {name: value for name, value in after.items() if name != key} == {
        name: value for name, value in before.items() if name != key
    }
    _assert_anchors(view)


def test_regions_without_overflow_hide_scrollbars_after_reloading(
    qapp: QApplication, qtbot: QtBot,
) -> None:
    _, view = _hosted_view(qtbot)
    qapp.processEvents()
    _assert_anchors(view)
    view.load_timeline(_sequence(overflow=False))
    qapp.processEvents()
    for bar in _bars(view).values():
        assert not bar.isVisible()
        assert not bar.isEnabled()
        assert bar.minimum() == bar.maximum()
