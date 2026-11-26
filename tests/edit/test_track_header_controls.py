"""Contract tests for timeline track-header controls."""

from __future__ import annotations

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QFont, QFontMetrics, QImage, QPainter
from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView, QStyleOptionGraphicsItem
import pytest

from src.features.edit.ui.timeline_items import TrackHeaderItem
from src.features.edit.ui.timeline_layout import TRACK_HEADER_WIDTH
from src.shared.ui.style_sheet import UI_MONO_FONT_FAMILIES


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


@pytest.mark.parametrize("track_type", ["video", "audio"])
def test_track_header_hides_count_then_name_as_height_shrinks(qapp, track_type: str) -> None:
    header = _header(track_type)
    header.set_clip_count(5)
    header.set_track_height(55)
    count = header._get_clip_count_rect()
    assert not count.isEmpty()
    assert count.bottom() < 55
    assert header._get_button_at_pos(header._get_name_rect().center()) == "name"

    header.set_track_height(54)
    assert header._get_clip_count_rect().isEmpty()
    name = header._get_name_rect()
    assert name.bottom() < 54
    assert header._get_button_at_pos(name.center()) == "name"

    header.set_track_height(42)
    assert header._get_name_rect().isEmpty()
    assert header._get_button_at_pos(name.center()) is None
    assert header._get_button_at_pos(header._get_track_badge_rect().center()) == "target"


def test_track_headers_share_chrome_and_video_visibility_sits_next_to_auto_select(qapp) -> None:
    video, audio = _header("video"), _header("audio")
    assert video._get_source_patch_rect() == audio._get_source_patch_rect()
    assert video._get_track_badge_rect() == audio._get_track_badge_rect()
    sync, visibility = video._get_sync_lock_button_rect(), video._get_mute_button_rect()
    assert video._get_button_at_pos(sync.center()) == "sync_lock"
    assert visibility.left() - sync.right() == 2
    assert visibility.center().y() == sync.center().y()

    backgrounds = set()
    for header in (video, audio):
        for locked in (False, True):
            header.set_locked(locked)
            header.set_source_patch_enabled(not locked)
            image = QImage(TRACK_HEADER_WIDTH, 52, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            try:
                header.paint(painter, QStyleOptionGraphicsItem())
            finally:
                painter.end()
            x = TRACK_HEADER_WIDTH - 3
            backgrounds.add((image.pixelColor(x, 10).name(), image.pixelColor(x, 33).name()))
    assert len(backgrounds) == 1
    controls_color, details_color = backgrounds.pop()
    assert controls_color != details_color

    video.set_track_height(32)
    image = QImage(TRACK_HEADER_WIDTH, 32, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        video.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()
    assert image.pixelColor(TRACK_HEADER_WIDTH - 3, 10).name() == controls_color


def test_audio_controls_have_distinct_hit_targets_and_signals(qapp) -> None:
    header = _header("audio")
    header.set_track_height(66)
    assert header._get_button_at_pos(header._get_volume_slider_rect().center()) != "volume"
    header.set_audio_mix_controls(True)
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
    header._update_volume_visual_from_pos(QPointF(150, 42))
    header._finish_volume_drag()

    assert requests["mute"] == [("audio-1", True)]
    assert requests["solo"] == [("audio-1", True)]
    assert requests["lock"] == [("audio-1", True)]
    assert len(requests["volume"]) == 1
    assert 0.0 <= requests["volume"][0][1] <= 2.0


def test_track_header_icons_paint_at_their_new_optical_size(qapp) -> None:
    header = _header("audio")
    image = QImage(TRACK_HEADER_WIDTH, 52, QImage.Format.Format_ARGB32_Premultiplied)
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


@pytest.mark.parametrize("height", [60, 66, 120])
def test_mixer_fader_paints_two_independent_peak_bars(qapp, height: int) -> None:
    header = _header("audio")
    header.set_track_height(height)
    header.set_audio_workspace_style(True)
    header.set_recording_control(True, "idle")
    gain = header._get_volume_slider_rect()
    pan = header._get_pan_rect()
    assert pan.height() == 22
    assert gain.center().y() == pan.center().y()
    assert gain.bottom() <= height - 6 and pan.bottom() <= height - 6
    assert gain.top() >= height / 2 and pan.top() >= height / 2
    assert header._get_button_at_pos(gain.center()) == "volume"
    assert header._get_button_at_pos(pan.center()) == "pan"
    assert header._get_button_at_pos(header._get_record_button_rect().center()) == "record"
    assert not gain.intersects(pan) and not gain.intersects(header._get_record_button_rect())
    header.set_meter_peaks((-3.0, -50.0))

    image = QImage(TRACK_HEADER_WIDTH, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        header.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()
    x = int(gain.right() - 12)
    y = int(gain.center().y())
    assert image.pixelColor(x, y - 2) != image.pixelColor(x, y + 2)


def test_mixer_controls_hide_when_track_is_short_or_viewport_is_narrow(qtbot) -> None:
    scene = QGraphicsScene()
    view = QGraphicsView(scene)
    qtbot.addWidget(view)
    header = _header("audio")
    scene.addItem(header)
    header.set_track_height(60)
    header.set_audio_workspace_style(True)
    header.set_recording_control(True, "idle")
    view.resize(450, 140)
    view.show()
    assert header._shows_audio_controls()
    header.set_track_height(59)
    assert not header._shows_audio_controls()
    for point in (header._get_volume_slider_rect().center(), header._get_pan_rect().center()):
        assert header._get_button_at_pos(point) not in {"volume", "pan"}
    assert header._get_button_at_pos(header._get_record_button_rect().center()) == "record"

    header.set_track_height(66)
    view.resize(175, 140)
    assert not header._shows_audio_controls()
    view.resize(450, 140)
    assert header._shows_audio_controls()


def test_mixer_icon_row_keeps_each_track_action_aligned(qapp) -> None:
    header = _header("audio")
    header.set_audio_workspace_style(True)
    header.set_recording_control(True, "armed")
    targets = (
        ("source_patch", header._get_source_patch_rect()),
        ("target", header._get_track_badge_rect()),
        ("lock", header._get_lock_button_rect()),
        ("record", header._get_record_button_rect()),
        ("solo", header._get_solo_button_rect()),
        ("mute", header._get_mute_button_rect()),
    )
    assert len({rect.center().y() for _, rect in targets}) == 1
    assert all(left.right() < right.left() for (_, left), (_, right) in zip(targets, targets[1:]))
    for name, rect in targets:
        assert header._get_button_at_pos(rect.center()) == name
    assert header._get_sync_lock_button_rect().isEmpty()
    header.set_audio_workspace_style(False)
    assert header._get_button_at_pos(header._get_sync_lock_button_rect().center()) == "sync_lock"


@pytest.mark.parametrize("locked", [False, True])
def test_mixer_lock_has_no_button_background_and_values_have_units(qapp, locked: bool) -> None:
    header = _header("audio")
    header.set_track_height(66)
    header.set_audio_workspace_style(True)
    header.set_locked(locked)
    header._hover_button = "lock"
    header.set_volume(0.5)
    header.set_pan(-0.24)
    assert header._mixer_volume_db_text() == "-6.0"
    assert "dB" in header._button_tooltip("volume")
    assert header._mixer_pan_text() == "L 24%"

    image = QImage(TRACK_HEADER_WIDTH, 66, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        header.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()
    assert image.pixelColor(86, 13) == image.pixelColor(75, 13)
    pan_readout = header._get_pan_readout_rect()
    font = QFont(UI_MONO_FONT_FAMILIES)
    font.setPixelSize(9)
    assert QFontMetrics(font).horizontalAdvance("R 100%") <= pan_readout.width()
    assert pan_readout.right() < header.boundingRect().right()
    for left, right in ((8, 58), (int(pan_readout.left()), int(pan_readout.right()))):
        assert any(
            image.pixelColor(x, y).lightness() > 100
            for x in range(left, right) for y in range(46, 58)
        )


@pytest.mark.parametrize("pan", [-0.75, 0.0, 0.75])
def test_mixer_pan_knob_uses_track_color_on_the_adjusted_side(qapp, pan: float) -> None:
    header = _header("audio")
    header.set_track_height(66)
    header.set_audio_workspace_style(True)
    header.set_track_color("#B67CF8")
    header.set_pan(pan)

    image = QImage(TRACK_HEADER_WIDTH, 66, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        header.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()

    knob = header._get_pan_rect().toRect()
    left, right = set(), set()
    for y in range(knob.top(), knob.bottom() + 1):
        for x in range(knob.left(), knob.right() + 1):
            color = image.pixelColor(x, y)
            if color.blue() - color.green() > 60 and color.red() > 100:
                side = left if x < knob.center().x() else right
                side.add((x, y))
    if pan < 0.0:
        assert len(left) > len(right)
    elif pan > 0.0:
        assert len(right) > len(left)
    else:
        assert not left and not right
