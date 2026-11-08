"""Real hit routing and persistent feedback for fade, trim and roll edits."""
from __future__ import annotations

from dataclasses import replace
import math
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QStyleOptionGraphicsItem

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import ClipItem, EditTool, RollHandleItem
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_ui


def _sequence() -> TimelineSequence:
    videos, audios = [], []
    for index in range(3):
        clip = ClipInstance(
            f"v{index}", "source", "video", 100 + index * 100, 100,
            100 + index * 100, 100, linked_clip_id=f"a{index}",
        )
        videos.append(clip)
        audios.append(replace(clip, clip_id=f"a{index}", track_id="audio", linked_clip_id=f"v{index}"))
    return TimelineSequence(
        "affordance-gestures", "Affordance gestures", fps=Rational(24, 1),
        video_tracks=(EditTrack("video", "V1", "video", 0, clips=tuple(videos)),),
        audio_tracks=(EditTrack("audio", "A1", "audio", 0, clips=tuple(audios)),),
    )


def _view(qtbot: Any, tool: EditTool = EditTool.SELECT, zoom: float = 3.0) -> TimelineView:
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1600, 650)
    view.show()
    view.load_timeline(_sequence())
    view.set_zoom(zoom)
    view.set_snap_enabled(False)
    view.set_tool(tool)
    for item in view._items_map.values():
        if isinstance(item, ClipItem):
            item._max_source_frames = 1000
    QTest.qWait(5)
    return view


def _point(view: TimelineView, clip_id: str, x: float, y: float) -> QPoint:
    return view.mapFromScene(view._items_map[clip_id].mapToScene(QPointF(x, y)))


def _gesture(view: TimelineView, point: QPoint, delta: int, cancel: bool = False) -> None:
    end = point + QPoint(delta, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), end, 10)
    if cancel:
        QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)


def _assert_single_undo(view: TimelineView, original: TimelineSequence) -> None:
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().to_dict() == replace(original, revision=view.get_timeline().revision).to_dict()


def _render(item: ClipItem) -> QImage:
    image = QImage(math.ceil(item._width), math.ceil(item._track_height), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    option = QStyleOptionGraphicsItem()
    option.exposedRect = QRectF(0, 0, item._width, item._track_height)
    painter = QPainter(image)
    try:
        item.paint(painter, option)
    finally:
        painter.end()
    return image


@pytest.mark.parametrize("tool", [EditTool.SELECT, EditTool.TRIM])
@pytest.mark.parametrize("edge,clip_id,direction", [("left", "v0", 1), ("right", "v2", -1)])
def test_top_corner_edits_fade_in_both_selection_and_trim_modes(
    qtbot: Any, tool: EditTool, edge: str, clip_id: str, direction: int,
) -> None:
    view = _view(qtbot, tool)
    before = view.get_timeline()
    item = view._items_map[clip_id]
    x = 3.0 if edge == "left" else item._width - 3.0
    _gesture(view, _point(view, clip_id, x, 4), direction * 30)
    result = view.get_timeline().get_clip_by_id(clip_id)
    original = before.get_clip_by_id(clip_id)
    assert (result.timeline_start, result.timeline_duration, result.source_start, result.source_duration) == (
        original.timeline_start, original.timeline_duration, original.source_start, original.source_duration)
    assert (result.fade_in_duration if edge == "left" else result.fade_out_duration) == 10
    _assert_single_undo(view, before)


@pytest.mark.parametrize("edge,clip_id,direction", [("left", "v0", 1), ("right", "v2", -1)])
def test_side_below_top_tab_trims_instead_of_fading(qtbot: Any, edge: str, clip_id: str, direction: int) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    item = view._items_map[clip_id]
    x = 3.0 if edge == "left" else item._width - 3.0
    _gesture(view, _point(view, clip_id, x, 32), direction * 30)
    result = view.get_timeline().get_clip_by_id(clip_id)
    assert result.timeline_duration == 90
    assert result.timeline_start == before.get_clip_by_id(clip_id).timeline_start + (10 if edge == "left" else 0)
    assert result.fade_in_duration == result.fade_out_duration == 0
    _assert_single_undo(view, before)


@pytest.mark.parametrize("clip_id,edge,direction", [("v0", "right", -1), ("v1", "left", 1)])
def test_shared_cut_top_corners_remain_independent_fade_targets(
    qtbot: Any, clip_id: str, edge: str, direction: int,
) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    item = view._items_map[clip_id]
    x = 3.0 if edge == "left" else item._width - 3.0
    point = _point(view, clip_id, x, 4)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert view.scene().mouseGrabberItem() is item
    QTest.mouseMove(view.viewport(), point + QPoint(direction * 30, 0), 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(direction * 30, 0))
    result = view.get_timeline()
    assert result.get_clip_by_id("v0").timeline_end == before.get_clip_by_id("v0").timeline_end
    assert result.get_clip_by_id("v1").timeline_start == before.get_clip_by_id("v1").timeline_start
    assert (result.get_clip_by_id(clip_id).fade_in_duration if edge == "left"
            else result.get_clip_by_id(clip_id).fade_out_duration) == 10
    _assert_single_undo(view, before)


def test_shared_cut_below_top_tabs_rolls_both_sides_and_linked_audio(qtbot: Any) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    point = _point(view, "v0", view._items_map["v0"]._width, 32)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert isinstance(view.scene().mouseGrabberItem(), RollHandleItem)
    QTest.mouseMove(view.viewport(), point + QPoint(30, 0), 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(30, 0))
    result = view.get_timeline()
    for prefix in ("v", "a"):
        assert result.get_clip_by_id(f"{prefix}0").timeline_duration == 110
        assert result.get_clip_by_id(f"{prefix}1").timeline_start == 210
        assert result.get_clip_by_id(f"{prefix}1").timeline_duration == 90
    _assert_single_undo(view, before)


@pytest.mark.parametrize("clip_id,edge", [("v0", "left"), ("v2", "right")])
def test_clicking_an_endpoint_keeps_its_bracket_after_hover_leaves_without_editing(
    qtbot: Any, clip_id: str, edge: str,
) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    item = view._items_map[clip_id]
    x = 3.0 if edge == "left" else item._width - 3.0
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, clip_id, x, 32))
    QTest.mouseMove(view.viewport(), QPoint(300, 100))
    assert item._selected_edit_edge == edge
    assert view._items_map[clip_id.replace("v", "a")]._selected_edit_edge == edge
    assert view.get_timeline().to_dict() == before.to_dict()
    assert not view._command_stack._undo_stack
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=_point(view, clip_id, item._width / 2, 32))
    assert all(item._selected_edit_edge is None for item in view._items_map.values() if isinstance(item, ClipItem))
    assert not view._command_stack._undo_stack


def test_clicking_seam_selects_both_brackets_and_hover_leave_does_not_clear_them(qtbot: Any) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    point = _point(view, "v0", view._items_map["v0"]._width, 32)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), QPoint(300, 100))
    for prefix in ("v", "a"):
        assert view._items_map[f"{prefix}0"]._selected_edit_edge == "right"
        assert view._items_map[f"{prefix}1"]._selected_edit_edge == "left"
    assert view.get_timeline().to_dict() == before.to_dict()
    assert not view._command_stack._undo_stack


@pytest.mark.parametrize("gesture", ["fade", "trim", "roll"])
def test_escape_cancels_affordance_drag_and_late_release_cannot_commit(qtbot: Any, gesture: str) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    item = view._items_map["v0"]
    x, y = {"fade": (3, 4), "trim": (3, 32), "roll": (item._width, 32)}[gesture]
    _gesture(view, _point(view, "v0", x, y), 30, cancel=True)
    assert view.get_timeline().to_dict() == before.to_dict()
    assert not view._command_stack._undo_stack
    assert view._interaction.is_idle


def test_a_short_clip_still_exposes_and_edits_its_fade_tab(qtbot: Any) -> None:
    view = _view(qtbot, zoom=0.6)
    item = view._items_map["v0"]
    assert 24 < item._width < 100
    QTest.mouseMove(view.viewport(), _point(view, "v0", item._width / 2, 30))
    image = _render(item)
    assert any(min(image.pixelColor(x, y).red(), image.pixelColor(x, y).green(), image.pixelColor(x, y).blue()) > 220
               for x in range(0, 8) for y in range(2, 8))
    before = view.get_timeline()
    _gesture(view, _point(view, "v0", 3, 4), 12)
    assert view.get_timeline().get_clip_by_id("v0").fade_in_duration == 20
    _assert_single_undo(view, before)


def test_fade_tab_is_rectangular_and_fade_does_not_cover_clip_name_strip(qtbot: Any) -> None:
    view = _view(qtbot)
    item = view._items_map["v0"]
    center = _point(view, "v0", item._width / 2, 32)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
    QTest.mouseMove(view.viewport(), center)
    baseline = _render(item)

    def white_width(y: int) -> int:
        return sum(min(baseline.pixelColor(x, y).red(), baseline.pixelColor(x, y).green(), baseline.pixelColor(x, y).blue()) > 220
                   for x in range(0, 10))

    assert white_width(3) >= 3 and white_width(7) >= 3
    assert abs(white_width(3) - white_width(7)) <= 1
    _gesture(view, _point(view, "v0", 3, 4), 90)
    QTest.mouseMove(view.viewport(), center)
    faded = _render(view._items_map["v0"])
    for y in range(faded.height() - 17, faded.height() - 3):
        assert [faded.pixel(x, y) for x in range(4, faded.width() - 4)] == [
            baseline.pixel(x, y) for x in range(4, baseline.width() - 4)]


@pytest.mark.parametrize("clip_id,edge", [("v0", "left"), ("v2", "right")])
def test_selected_trim_bracket_does_not_turn_into_a_wide_colored_block(qtbot: Any, clip_id: str, edge: str) -> None:
    view = _view(qtbot)
    item = view._items_map[clip_id]
    x = 3.0 if edge == "left" else item._width - 3.0
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, clip_id, x, 32))
    QTest.mouseMove(view.viewport(), QPoint(300, 100))
    image = _render(item)
    colored = [x for x in range(image.width()) if (
        image.pixelColor(x, 32).green() > image.pixelColor(x, 32).red() * 1.3
        and image.pixelColor(x, 32).green() > image.pixelColor(x, 32).blue() * 1.3)]
    assert colored
    assert all(x < 5 if edge == "left" else x >= image.width() - 5 for x in colored)


@pytest.mark.parametrize("tool", [EditTool.SELECT, EditTool.TRIM])
def test_selected_cut_comma_period_nudges_the_cut_and_preserves_total_length(qtbot: Any, tool: EditTool) -> None:
    view = _view(qtbot, tool)
    original = view.get_timeline()
    point = _point(view, "v0", view._items_map["v0"]._width, 32)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.keyClick(view, Qt.Key.Key_Period)
    result = view.get_timeline()
    for prefix in ("v", "a"):
        assert result.get_clip_by_id(f"{prefix}0").timeline_duration == 101
        assert result.get_clip_by_id(f"{prefix}1").timeline_start == 201
        assert result.get_clip_by_id(f"{prefix}1").timeline_end == 300
    _assert_single_undo(view, original)


def test_selected_head_nudge_uses_the_selected_side_and_shift_is_five_frames(qtbot: Any) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, "v0", 3, 32))
    QTest.keyClick(view, Qt.Key.Key_Period, Qt.KeyboardModifier.ShiftModifier)
    for clip_id in ("v0", "a0"):
        clip = view.get_timeline().get_clip_by_id(clip_id)
        assert clip.timeline_start == 105 and clip.timeline_duration == 95
        assert clip.source_start == 105
    _assert_single_undo(view, original)


def test_selecting_a_cut_replaces_an_unrelated_body_selection(qtbot: Any) -> None:
    view = _view(qtbot)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=_point(view, "v2", view._items_map["v2"]._width / 2, 32))
    assert view._items_map["v2"]._is_selected
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=_point(view, "v0", view._items_map["v0"]._width, 32))
    assert not view._items_map["v2"]._is_selected
    assert set(view._selected_edit_edges) == {"v0", "v1", "a0", "a1"}
    assert not view._command_stack._undo_stack


def test_option_roll_temporarily_leaves_audio_edit_point_unchanged(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QApplication

    view = _view(qtbot)
    original_audio = view.get_timeline().audio_tracks
    monkeypatch.setattr(QApplication, "keyboardModifiers", staticmethod(lambda: Qt.KeyboardModifier.AltModifier))
    point = _point(view, "v0", view._items_map["v0"]._width, 32)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, point)
    QTest.mouseMove(view.viewport(), point + QPoint(30, 0), 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, point + QPoint(30, 0))
    assert view.get_timeline().get_clip_by_id("v0").timeline_duration == 110
    assert view.get_timeline().audio_tracks == original_audio
    assert view._linked_selection_enabled
    assert set(view._selected_edit_edges) == {"v0", "v1"}


def test_meeting_fade_endpoints_can_still_be_grabbed_individually(qtbot: Any) -> None:
    view = _view(qtbot)
    clip = view.get_timeline().get_clip_by_id("v0")
    view.load_timeline(view.get_timeline().with_clip_updated(replace(clip, fade_in_duration=50, fade_out_duration=50)))
    item = view._items_map["v0"]
    midpoint = item._width / 2.0
    assert item._fade_handle_at(QPointF(midpoint - 2, 4)) == "in"
    assert item._fade_handle_at(QPointF(midpoint + 2, 4)) == "out"


def test_keyboard_nudge_during_mouse_roll_does_not_commit_a_second_edit(qtbot: Any) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    point = _point(view, "v0", view._items_map["v0"]._width, 32)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), point + QPoint(30, 0), 10)
    QTest.keyClick(view, Qt.Key.Key_Period)
    assert view.get_timeline() is original
    assert not view._command_stack._undo_stack
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(30, 0))
    assert view.get_timeline().get_clip_by_id("v0").timeline_duration == 110
    _assert_single_undo(view, original)


def test_retime_controls_do_not_leave_an_invisible_fade_target(qtbot: Any) -> None:
    view = _view(qtbot)
    view._on_retime_controls_toggled("v0", "video", True)
    item = view._items_map["v0"]
    assert item._fade_handle_at(QPointF(3, 4)) is None
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, "v0", 8, 8))
    assert not item._retime_controls_visible
    assert not item._drag_pending
    assert not view._command_stack._undo_stack


@pytest.mark.parametrize("key,direction", [(Qt.Key.Key_Less, -1), (Qt.Key.Key_Greater, 1)])
def test_native_shifted_punctuation_nudges_the_cut_five_frames(qtbot: Any, key: Qt.Key, direction: int) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=_point(view, "v0", view._items_map["v0"]._width, 32))
    QTest.keyClick(view, key, Qt.KeyboardModifier.ShiftModifier)
    assert view.get_timeline().get_clip_by_id("v0").timeline_duration == 100 + direction * 5
    assert view.get_timeline().get_clip_by_id("a0").timeline_duration == 100 + direction * 5
    _assert_single_undo(view, original)


def test_keyboard_trim_refuses_unequal_linked_source_limits(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view(qtbot, EditTool.TRIM)
    timeline = view.get_timeline()
    timeline = timeline.with_clip_updated(replace(timeline.get_clip_by_id("v0"), source_start=890))
    timeline = timeline.with_clip_updated(replace(timeline.get_clip_by_id("a0"), source_start=897))
    view.load_timeline(timeline)
    monkeypatch.setattr(view, "_get_asset_duration", lambda _asset: 1000)
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    point = _point(view, "v0", view._items_map["v0"]._width - 5, 32)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.keyClick(view, Qt.Key.Key_Period, Qt.KeyboardModifier.ShiftModifier)
    assert view.get_timeline() is timeline
    assert not view._command_stack._undo_stack
    assert messages


@pytest.mark.parametrize("tool,shape", [(EditTool.BLADE, Qt.CursorShape.CrossCursor),
                                        (EditTool.HAND, Qt.CursorShape.OpenHandCursor),
                                        (EditTool.ZOOM, Qt.CursorShape.CrossCursor)])
def test_non_trim_tools_do_not_advertise_a_fade_action_at_top_corners(qtbot: Any, tool: EditTool, shape: Qt.CursorShape) -> None:
    view = _view(qtbot, tool)
    QTest.mouseMove(view.viewport(), _point(view, "v0", 3, 4))
    item = view._items_map["v0"]
    assert item._hover_fade_handle is None
    assert item.cursor().shape() == shape
