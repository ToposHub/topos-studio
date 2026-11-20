"""Timeline drag, persistence, and playback/export parity for real clip overlaps."""
from __future__ import annotations

import wave
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.audio_playback import AudioPlaybackManager
from src.features.edit.core.commands import CommandStack, MoveClipsOverwriteCommand
from src.features.edit.core.nested_sequence import (
    create_nested_sequence_asset,
    flatten_nested_audio_tracks,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.stem_export import (
    StemSpec, StemType, create_clip_decode_fn, render_stem_pcm,
)
from src.features.edit.core.timeline_export import TimelineExportConfig, TimelineExporter
from src.features.edit.core.timeline_ops import validate_timeline
from src.features.edit.core.timeline_v2 import (
    AudioOverlapError, ClipInstance, EditTrack, TimelineSequence,
)
from src.features.edit.ui.timeline_items import TransitionItem
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.media.metadata import Rational


FPS = 25
SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = SAMPLE_RATE // FPS


def _sequence(second_start: int = 80, *, manual_fades: bool = True) -> TimelineSequence:
    left = ClipInstance(
        "left", "left", "a1", 10, 50, 3, 50,
        fade_out_duration=20 if manual_fades else 0, fade_curve_out="linear",
    )
    right = ClipInstance(
        "right", "right", "a1", second_start, 50, 4, 50,
        fade_in_duration=20 if manual_fades else 0, fade_curve_in="linear",
    )
    return TimelineSequence(
        "master", "Crossfade", fps=Rational(FPS, 1),
        audio_tracks=(EditTrack("a1", "Dialogue", "audio", 0, clips=(left, right)),),
    )


def _move(clip_id: str, start: int) -> dict[str, Any]:
    return {
        "clip_id": clip_id, "source_track_id": "a1", "target_track_id": "a1",
        "new_start": start,
    }


@pytest.mark.parametrize("audio_only", [False, True])
def test_drag_creates_crossfade_and_handles_edit_curves(qtbot: Any, audio_only: bool) -> None:
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1100, 530)
    view.set_audio_only(audio_only)
    original = _sequence()
    left, right = original.audio_tracks[0].clips
    view.load_timeline(
        original.with_clip_updated(replace(left, fade_in_duration=8))
        .with_clip_updated(replace(right, fade_out_duration=8))
    )
    view.set_zoom(5.0)
    view.set_snap_enabled(False)
    view.show()
    qtbot.waitExposed(view)

    item = view._items_map["right"]
    start = view.mapFromScene(item.mapToScene(QPointF(53, 32)))
    target = start - QPoint(30 * 5, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), target, 10)
    assert view._drag_crossfades
    outgoing, incoming = (view._items_map[clip_id] for clip_id in ("left", "right"))
    assert outgoing._fade_end_is_visible("in") and not outgoing._fade_end_is_visible("out")
    assert incoming._fade_end_is_visible("out") and not incoming._fade_end_is_visible("in")
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    sequence = view.get_timeline()
    left, right = sequence.audio_tracks[0].clips
    fade, = sequence.audio_tracks[0].transitions
    assert not view._drag_crossfades
    assert not outgoing._fade_end_is_visible("out") and not incoming._fade_end_is_visible("in")
    assert not outgoing._fade_curve_handle_points("out")
    assert not incoming._fade_curve_handle_points("in")
    for clip, hidden_end in ((outgoing, "out"), (incoming, "in")):
        clip_rect = QRectF(0, 0, clip._width, clip._track_height)
        x, y = clip._fade_handle_position(clip_rect, hidden_end)
        assert clip._fade_handle_at(QPointF(x, y)) is None
    assert (left.timeline_start, right.timeline_start) == (10, 50)
    assert (left.source_start, right.source_start) == (3, 4)
    assert (left.fade_out_duration, right.fade_in_duration) == (20, 20)
    assert (fade.start_frame, fade.end_frame) == (50, 60)
    assert validate_timeline(sequence) == []

    overlay = view._items_map[fade.transition_id]
    assert isinstance(overlay, TransitionItem)
    assert overlay.shape().contains(QPointF(4, 7))
    assert not overlay.shape().contains(QPointF(20, 22))
    start = view.mapFromScene(overlay.mapToScene(QPointF(4, 7)))
    end = start + QPoint(0, 26)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    adjusted, = view.get_timeline().audio_tracks[0].transitions
    assert adjusted.overlap_curve_adjustments == pytest.approx((26 / 36, 26 / 36))
    view.undo()
    assert view.get_timeline().audio_tracks[0].transitions[0].overlap_curve_adjustments == (0, 0)
    view.undo()
    assert view.get_timeline().audio_tracks[0].transitions == ()
    assert view._items_map["left"]._fade_end_is_visible("out")
    assert view._items_map["right"]._fade_end_is_visible("in")
    view.redo()
    assert view.get_timeline().audio_tracks[0].transitions[0].transition_id == fade.transition_id
    outgoing, incoming = (view._items_map[clip_id] for clip_id in ("left", "right"))
    assert not outgoing._fade_end_is_visible("out") and not incoming._fade_end_is_visible("in")

    start = view.mapFromScene(incoming.mapToScene(QPointF(100, 32)))
    target = start + QPoint(30 * 5, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), target, 10)
    assert outgoing._fade_end_is_visible("out") and incoming._fade_end_is_visible("in")
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    assert not outgoing._fade_end_is_visible("out") and not incoming._fade_end_is_visible("in")

    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), target, 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    assert view.get_timeline().audio_tracks[0].transitions == ()
    assert outgoing._fade_end_is_visible("out") and incoming._fade_end_is_visible("in")
    assert view.get_timeline().get_clip_by_id("left").fade_out_duration == 20
    assert view.get_timeline().get_clip_by_id("right").fade_in_duration == 20


def test_group_move_preserves_curve_and_bad_drop_preserves_sources() -> None:
    sequence = _sequence(50)
    track = sequence.audio_tracks[0]
    fade = replace(track.transitions[0], transition_type="audio:linear",
                   params={"overlap_crossfade": 1, "incoming_curve": -0.4})
    sequence = sequence.with_track_updated(replace(track, transitions=(fade,)))
    stack = CommandStack()
    moved = stack.execute(MoveClipsOverwriteCommand.create(
        [_move("left", 15), _move("right", 55)], audio_crossfades=True,
    ), sequence)
    updated, = moved.audio_tracks[0].transitions
    assert updated.transition_id == fade.transition_id
    assert updated.params == fade.params
    assert (updated.start_frame, updated.end_frame) == (55, 65)
    assert TimelineSequence.from_dict(moved.to_dict()).to_dict() == moved.to_dict()

    with pytest.raises(AudioOverlapError):
        stack.execute(MoveClipsOverwriteCommand.create(
            [_move("right", 15)], audio_crossfades=True,
        ), moved)
    assert len(stack._undo_stack) == 1
    assert stack.undo(moved).to_dict() == sequence.to_dict()
    assert stack.redo(sequence).to_dict() == moved.to_dict()

    apart = stack.execute(MoveClipsOverwriteCommand.create(
        [_move("right", 100)], audio_crossfades=True,
    ), moved)
    assert apart.audio_tracks[0].transitions == ()
    assert apart.get_clip_by_id("left").fade_out_duration == 20
    assert apart.get_clip_by_id("right").fade_in_duration == 20


def test_cropped_nested_crossfade_keeps_original_envelope_phase() -> None:
    child = _sequence(50)
    asset = create_nested_sequence_asset(child)
    nested = ClipInstance("nested", asset.asset_id, "v", 0, 6, 52, 6)
    parent = TimelineSequence(
        "parent", "Parent", fps=child.fps,
        video_tracks=(EditTrack("v", "Video", "video", 0, clips=(nested,)),),
    )
    virtual, = flatten_nested_audio_tracks(
        parent, {child.timeline_id: child}.get, {asset.asset_id: asset}.get, strict=True,
    )
    fade, = virtual.transitions
    assert (fade.start_frame, fade.end_frame) == (0, 6)
    assert fade.audio_envelope_range == (-2, 8)


def _write_stereo(path: Path, channel: int) -> None:
    pcm = np.zeros((64 * SAMPLES_PER_FRAME, 2), dtype="<i2")
    pcm[:, channel] = 8192
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm.tobytes())


@pytest.mark.parametrize("adjusted", [False, True])
def test_existing_fades_join_continuously_in_playback_master_and_stems(
    tmp_path: Path, adjusted: bool,
) -> None:
    assets = {}
    for channel, side in enumerate(("left", "right")):
        path = tmp_path / f"{side}.wav"
        _write_stereo(path, channel)
        assets[side] = MediaAsset(side, str(path), "audio", side, {})
    project = EditProject("project", "Crossfade", assets=assets)
    sequence = _sequence(50)
    track = sequence.audio_tracks[0]
    if adjusted:
        from src.features.edit.core.fade_curve_spec import fade_curve_from_midpoint

        shape = fade_curve_from_midpoint(0.25)
        left, right = track.clips
        track = replace(track, clips=(
            left.with_fade_end_curve("out", shape),
            right.with_fade_end_curve("in", shape),
        ))
    crossfade = replace(track.transitions[0], transition_type="audio:linear")
    sequence = sequence.with_track_updated(replace(track, transitions=(crossfade,)))
    decode, close = create_clip_decode_fn(project, FPS, SAMPLE_RATE)
    try:
        stem = render_stem_pcm(
            sequence, StemSpec(StemType.MASTER, "Master", (), include_master=True),
            SAMPLE_RATE, decode_clip_fn=decode,
        )
    finally:
        close()
    export = TimelineExporter(project, sequence, Mock())._render_audio_master(
        0, 100, TimelineExportConfig(output_path=str(tmp_path / "out.mov")),
        encoder=Mock(),
    )
    audio = AudioPlaybackManager()
    audio.set_timeline_fps(FPS)
    try:
        for clip in track.clips:
            assert audio.add_track(
                track.track_id, Path(assets[clip.asset_id].path),
                source_start=clip.source_start, source_fps=FPS,
                timeline_start=clip.timeline_start, timeline_duration=clip.timeline_duration,
                replace_existing=False, clip_id=clip.clip_id,
                fade_in_frames=clip.fade_in_duration, fade_out_frames=clip.fade_out_duration,
                fade_curve_in=clip.fade_curve_in, fade_curve_out=clip.fade_curve_out,
            )
        audio.set_audio_transitions(track.track_id, [crossfade])
        playback = np.concatenate([
            audio._mixer.mix_samples(frame, FPS, SAMPLES_PER_FRAME)[0]
            for frame in range(100)
        ], axis=1)
    finally:
        audio.cleanup()

    np.testing.assert_allclose(stem, export, atol=3e-5)
    np.testing.assert_allclose(playback, export, atol=3e-5)
    first, last = 50 * SAMPLES_PER_FRAME, 60 * SAMPLES_PER_FRAME
    midpoint = (first + last) // 2
    expected = 0.03125 if adjusted else 0.0625
    np.testing.assert_allclose(export[:, midpoint], (expected, expected), atol=1e-5)
    assert abs(export[0, first] - export[0, first - 1]) < 1e-5
    assert abs(export[1, last] - export[1, last - 1]) < 1e-5


def test_delete_undo_and_duplicate_preserve_crossfade_settings(qtbot: Any) -> None:
    from src.features.edit.core.commands import DeleteClipCommand, RemoveTransitionCommand
    from src.features.edit.core.selection import TimelineSelection

    sequence = _sequence(50)
    track = sequence.audio_tracks[0]
    fade = replace(track.transitions[0], transition_type="audio:linear",
                   params={"overlap_crossfade": 1, "incoming_curve": -0.4, "symmetrical": False})
    sequence = sequence.with_track_updated(replace(track, transitions=(fade,)))

    stack = CommandStack()
    deleted = stack.execute(DeleteClipCommand.create("a1", "right"), sequence)
    assert deleted.audio_tracks[0].transitions == ()
    assert stack.undo(deleted).to_dict() == sequence.to_dict()
    disabled = stack.execute(RemoveTransitionCommand.create(
        sequence, fade.transition_id, "a1",
    ), sequence)
    assert disabled.audio_tracks[0].transitions[0].params["overlap_enabled"] is False
    assert stack.undo(disabled).to_dict() == sequence.to_dict()

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(sequence)
    selection = TimelineSelection().with_clip_selected("right", "a1", sequence.timeline_id)
    selection = selection.with_clip_selected("left", "a1", sequence.timeline_id, replace=False)
    view.set_selection(selection)
    view._duplicate_selected_clips()
    original, duplicated = view.get_timeline().audio_tracks[0].transitions
    assert original.transition_id == fade.transition_id
    assert duplicated.transition_id != fade.transition_id
    assert duplicated.transition_type == fade.transition_type
    assert duplicated.params == fade.params
    assert (duplicated.start_frame, duplicated.end_frame) == (140, 150)
    view.undo()
    assert len(view.get_timeline().audio_tracks[0].clips) == 2


def test_very_long_crossfade_only_evaluates_audio_block(monkeypatch: Any) -> None:
    from src.features.edit.core import audio_dsp

    monkeypatch.setattr(
        audio_dsp, "_crossfade_envelopes_cached",
        Mock(side_effect=AssertionError("Long overlap tried to allocate its entire envelope")),
    )
    duration = 25 * 60 * 10
    block = np.ones((2, 512), dtype=np.float32)
    kwargs = dict(transition_start_frame=0, transition_duration_frames=duration,
                  current_frame=duration // 2, curve="equal_power",
                  fps=FPS, sample_rate=SAMPLE_RATE)
    left = audio_dsp.apply_crossfade_block(block, role="left", **kwargs)
    right = audio_dsp.apply_crossfade_block(block, role="right", **kwargs)
    np.testing.assert_allclose(left**2 + right**2, 1.0, atol=1e-12)


def test_crossfade_shades_waveform_without_border_or_label_overlay() -> None:
    from PyQt6.QtGui import QColor, QImage, QPainter

    from src.features.edit.ui.timeline_items import paint_overlap_crossfade

    base = QColor("#228b67")
    image = QImage(100, 70, QImage.Format.Format_ARGB32)
    image.fill(base)
    painter = QPainter(image)
    paint_overlap_crossfade(painter, QRectF(10, 0, 80, 70), "audio:equal_power")
    painter.end()

    assert image.pixelColor(50, 8).green() < base.green()
    assert image.pixelColor(50, 40) == base
    assert image.pixelColor(50, 60) == base
    assert image.pixelColor(9, 8) == base
    assert image.pixelColor(10, 8).red() < base.red()
