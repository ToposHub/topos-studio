"""Composed timeline pixels keep functional sections visibly separated."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, SubtitleClip, TimelineSequence
from src.features.edit.ui.timeline_items import CLIP_SELECTED_BORDER, PLAYHEAD_COLOR, RULER_HEIGHT, TRACK_HEADER_WIDTH
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.media.metadata import Rational
from src.shared.ui.style_sheet import C, DARK_THEME

pytestmark = pytest.mark.edit_ui


def _sequence(video: int = 2, audio: int = 2, subtitles: int = 1) -> TimelineSequence:
    tracks: dict[str, list[EditTrack]] = {"video": [], "audio": [], "subtitle": []}
    for kind, count in (("video", video), ("audio", audio), ("subtitle", subtitles)):
        for index in range(count):
            track_id = f"{kind[0]}{index + 1}"
            clips = (() if kind == "subtitle" else (ClipInstance(
                clip_id=f"clip-{track_id}", asset_id="media", track_id=track_id,
                timeline_start=0, timeline_duration=2000, source_start=0, source_duration=2000,
            ),))
            cues = ((SubtitleClip(clip_id=f"cue-{track_id}", track_id=track_id,
                                  timeline_start=0, timeline_duration=2000, text="Caption"),)
                    if kind == "subtitle" else ())
            tracks[kind].append(EditTrack(
                track_id, track_id.upper(), kind, index, clips=clips, subtitle_clips=cues,
                is_locked=kind == "audio" and index == 1,
            ))
    return TimelineSequence(
        "section-pixels", "Sections", fps=Rational(24, 1),
        video_tracks=tuple(tracks["video"]), audio_tracks=tuple(tracks["audio"]),
        subtitle_tracks=tuple(tracks["subtitle"]),
    )


def _view(qtbot: QtBot, sequence: TimelineSequence | None, height: int = 650) -> TimelineView:
    view = TimelineView()
    qtbot.addWidget(view)
    view.setStyleSheet(DARK_THEME + view.styleSheet())
    view.resize(1000, height)
    view.set_project(EditProject("section-project", "Sections", assets={
        "media": MediaAsset("media", "/tmp/topos-section-pixels-missing.mp4", "video", "Media",
                            metadata={"fps": 24.0, "total_frames": 3000}),
    }))
    if sequence is not None:
        view.load_timeline(sequence)
    view.set_zoom(3.0)
    view.show()
    return view


def _render(view: TimelineView, dpr: int) -> QImage:
    # Render the whole widget, including its opaque child timecode label.
    # Rendering only the scene would miss both fixed-header drawing passes.
    image = QImage(view.width() * dpr, view.height() * dpr, QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.magenta)
    painter = QPainter(image)
    QWidget.render(view, painter)
    painter.end()
    return image


def _assert_rule(
    view: TimelineView, image: QImage, y: int, dpr: int, *, header_only: bool = False,
) -> None:
    origin = view.viewport().mapTo(view, QPoint())
    assert 0 <= y < view.viewport().height()
    right = TRACK_HEADER_WIDTH if header_only else view.viewport().width() - 20
    for x in range(4, right):
        for dx in range(dpr):
            for dy in range(dpr):
                color = image.pixelColor((origin.x() + x) * dpr + dx, (origin.y() + y) * dpr + dy)
                assert color == QColor(C.PANEL_DIVIDER), f"Broken divider at ({x}, {y}), DPR {dpr}"


def _assert_track_rule(view: TimelineView, image: QImage, y: int, dpr: int) -> None:
    """Track rows divide headers; only the V/A bar divides the canvas."""
    _assert_rule(view, image, y, dpr, header_only=True)
    splitter = view._layout_engine.area_layout().splitter_rect
    common = {RULER_HEIGHT - 1, view.mapFromScene(QPointF(0, splitter.top())).y(),
              view.mapFromScene(QPointF(0, splitter.bottom())).y() - 1}
    if y in common:
        _assert_rule(view, image, y, dpr)
        return
    origin = view.viewport().mapTo(view, QPoint())
    for x in range(TRACK_HEADER_WIDTH + 20, view.viewport().width() - 20):
        for dy in range(dpr):
            color = image.pixelColor((origin.x() + x) * dpr, (origin.y() + y) * dpr + dy)
            assert color != QColor(C.PANEL_DIVIDER), f"Track rule leaked into canvas at ({x}, {y})"


def _assert_corner(view: TimelineView, image: QImage, dpr: int) -> None:
    origin = view.viewport().mapTo(view, QPoint())
    for y in (3, RULER_HEIGHT - 2, RULER_HEIGHT - 1, view.viewport().height() - 3):
        for dx in range(dpr):
            color = image.pixelColor((origin.x() + TRACK_HEADER_WIDTH - 1) * dpr + dx,
                                     (origin.y() + y) * dpr)
            assert color == QColor(C.PANEL_DIVIDER)
    _assert_rule(view, image, RULER_HEIGHT - 1, dpr)


@pytest.mark.parametrize("dpr", [1, 2])
def test_track_dividers_stay_in_headers_and_keep_canvas_unruled(
    qapp: QApplication, qtbot: QtBot, dpr: int,
) -> None:
    view = _view(qtbot, _sequence())
    qapp.processEvents()
    playhead = view._playhead_item
    view._playhead_item = None

    def check() -> None:
        qapp.processEvents()
        image = _render(view, dpr)
        _assert_corner(view, image, dpr)
        # Header rows stay individually divided while ordinary canvas gaps
        # retain their background. The shared V/A bar remains full width.
        for track_id in ("v1", "v2", "a1", "a2", "s1"):
            header = view._track_headers[track_id]
            y = view.mapFromScene(header.scenePos()).y() + header._track_height
            _assert_track_rule(view, image, y, dpr)
            if header._is_topmost:
                _assert_track_rule(view, image, view.mapFromScene(header.scenePos()).y() - 1, dpr)

    check()
    view.horizontalScrollBar().setValue(80)
    check()
    view._on_track_height_changed("v2", 111)
    view.resize(1037, 711)
    check()

    # The playhead is the final scene overlay: the black horizontal rail
    # must not punch a hole through its colored stem.
    view._playhead_item = playhead
    view.set_playhead(100)
    qapp.processEvents()
    image = _render(view, dpr)
    header = view._track_headers["v1"]
    y = view.mapFromScene(header.scenePos()).y() + header._track_height
    point = view.mapFromScene(QPointF(playhead.scenePos().x(), 0))
    origin = view.viewport().mapTo(view, QPoint())
    colors = [image.pixelColor((origin.x() + point.x()) * dpr + dx, (origin.y() + y) * dpr)
              for dx in range(-2 * dpr, 2 * dpr + 1)]
    assert PLAYHEAD_COLOR in colors


@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("loaded", [False, True], ids=["no-project-timeline", "zero-tracks"])
def test_empty_workspace_keeps_ruler_and_area_sections(
    qapp: QApplication, qtbot: QtBot, dpr: int, loaded: bool,
) -> None:
    view = _view(qtbot, _sequence(0, 0, 0) if loaded else None)
    qapp.processEvents()
    view._playhead_item = None
    image = _render(view, dpr)
    _assert_corner(view, image, dpr)
    if loaded:
        area = view._layout_engine.area_layout()
        rows = (view.mapFromScene(QPointF(0, area.splitter_rect.top())).y(),
                view.mapFromScene(QPointF(0, area.splitter_rect.bottom())).y() - 1)
    else:
        rows = (RULER_HEIGHT + (view.viewport().height() - RULER_HEIGHT) // 2,)
    for y in rows:
        _assert_rule(view, image, y, dpr)


@pytest.mark.parametrize("dpr", [1, 2])
def test_selected_clip_frames_survive_header_rules_and_va_divider(
    qapp: QApplication, qtbot: QtBot, dpr: int,
) -> None:
    view = _view(qtbot, _sequence())
    qapp.processEvents()
    view._playhead_item = None
    unselected = _render(view, dpr)
    origin = view.viewport().mapTo(view, QPoint())
    # V2 is beside header-only rules; A1 starts below the shared V/A bar.
    # Their upper and lower clip frames must both remain visible.
    for track_id in ("v2", "a1"):
        item = view._items_map[f"clip-{track_id}"]
        item.set_selected(True)
        qapp.processEvents()
        selected = _render(view, dpr)
        top_left = view.mapFromScene(item.scenePos())
        x = origin.x() + top_left.x() + 150
        for y in (top_left.y(), top_left.y() + item._track_height - 1):
            for dy in range(dpr):
                pixel = QPoint(x * dpr, (origin.y() + y) * dpr + dy)
                before = unselected.pixelColor(pixel)
                after = selected.pixelColor(pixel)
                # The clip's subtle upper highlight blends a little white
                # over amber. Both rows must still carry the actual accent.
                assert max(abs(after.red() - CLIP_SELECTED_BORDER.red()),
                           abs(after.green() - CLIP_SELECTED_BORDER.green()),
                           abs(after.blue() - CLIP_SELECTED_BORDER.blue())) <= 8
                assert after != before
        _assert_track_rule(view, selected, top_left.y() - 1, dpr)
        _assert_track_rule(view, selected, top_left.y() + item._track_height, dpr)
        item.set_selected(False)


@pytest.mark.parametrize("dpr", [1, 2])
def test_scrolled_tracks_cannot_draw_rails_through_other_regions(
    qapp: QApplication, qtbot: QtBot, dpr: int,
) -> None:
    view = _view(qtbot, _sequence(4, 5, 4), height=400)
    qapp.processEvents()
    view._playhead_item = None
    state = view.layout_state()
    state.update(video_scroll_offset=100, audio_scroll_offset=90, subtitle_scroll_offset=30)
    view.set_layout_state(state)
    qapp.processEvents()
    image = _render(view, dpr)
    _assert_corner(view, image, dpr)
    area = view._layout_engine.area_layout()
    regions = {"video": area.video_area, "audio": area.audio_area, "subtitle": area.subtitle_area}
    invalid_rows: set[int] = set()
    valid_rows = {RULER_HEIGHT - 1}
    timeline = view.get_timeline()
    for track in timeline.video_tracks + timeline.audio_tracks + timeline.subtitle_tracks:
        header = view._track_headers[track.track_id]
        y = view.mapFromScene(header.scenePos()).y() + header._track_height
        scene_y = view.mapToScene(QPoint(400, y)).y()
        if regions[track.track_type].top() <= scene_y < regions[track.track_type].bottom():
            if RULER_HEIGHT <= y < view.viewport().height():
                valid_rows.add(y)
        elif RULER_HEIGHT <= y < view.viewport().height():
            invalid_rows.add(y)
    for rect in (area.video_area, area.audio_area, area.subtitle_area, area.splitter_rect):
        for scene_y in (rect.top(), rect.bottom()):
            boundary = view.mapFromScene(QPointF(0, scene_y)).y()
            valid_rows.update((boundary - 1, boundary, boundary + 1))
    candidates = invalid_rows - valid_rows
    assert candidates, "Fixture must contain tracks scrolled beyond their own region"
    origin = view.viewport().mapTo(view, QPoint())
    for y in candidates:
        colors = [image.pixelColor((origin.x() + x) * dpr, (origin.y() + y) * dpr)
                  for x in range(4, TRACK_HEADER_WIDTH - 2)]
        assert any(color != QColor(C.PANEL_DIVIDER) for color in colors), f"Stray header rule at {y}"
