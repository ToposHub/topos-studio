"""Contract tests for timeline track-header controls."""

from __future__ import annotations

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QStyleOptionGraphicsItem

from src.features.edit.ui.timeline_items import TrackHeaderItem


def _header(track_type: str) -> TrackHeaderItem:
    return TrackHeaderItem(
        track_id=f"{track_type}-1",
        track_name=f"{track_type.title()} 1",
        track_index=0,
        track_type=track_type,
        track_height=52,
    )


def test_video_eye_is_a_visibility_control(qapp) -> None:
    header = _header("video")
    eye_center = header._get_mute_button_rect().center()

    assert header._get_button_at_pos(eye_center) == "visibility"

    requests: list[tuple[str, bool]] = []
    header.visibility_changed.connect(lambda track_id, visible: requests.append((track_id, visible)))
    header._toggle_visibility()

    assert requests == [("video-1", False)]


def test_audio_controls_have_distinct_hit_targets_and_signals(qapp) -> None:
    header = _header("audio")
    requests: dict[str, list[object]] = {"mute": [], "solo": [], "lock": [], "volume": []}
    header.mute_changed.connect(lambda track_id, value: requests["mute"].append((track_id, value)))
    header.solo_changed.connect(lambda track_id, value: requests["solo"].append((track_id, value)))
    header.lock_changed.connect(lambda track_id, value: requests["lock"].append((track_id, value)))
    header.volume_changed.connect(lambda track_id, value: requests["volume"].append((track_id, value)))

    assert header._get_button_at_pos(header._get_lock_button_rect().center()) == "lock"
    assert header._get_button_at_pos(header._get_solo_button_rect().center()) == "solo"
    assert header._get_button_at_pos(header._get_mute_button_rect().center()) == "mute"
    assert header._get_button_at_pos(header._get_volume_slider_rect().center()) == "volume"

    header._toggle_mute()
    header._toggle_solo()
    header._toggle_lock()
    header._start_volume_drag(type("Event", (), {"pos": lambda self: QPointF(160, 42)})())
    header._finish_volume_drag()

    assert requests["mute"] == [("audio-1", True)]
    assert requests["solo"] == [("audio-1", True)]
    assert requests["lock"] == [("audio-1", True)]
    assert len(requests["volume"]) == 1
    assert 0.0 <= requests["volume"][0][1] <= 2.0


def test_track_header_icons_paint_at_their_new_optical_size(qapp) -> None:
    header = _header("audio")
    image = QImage(220, 52, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        header.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()

    assert any(
        image.pixelColor(x, y).alpha() > 0
        for y in range(image.height())
        for x in range(image.width())
    )
