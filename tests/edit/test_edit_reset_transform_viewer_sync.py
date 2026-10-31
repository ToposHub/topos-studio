"""Regression tests for Edit Transform reset synchronization."""

from types import SimpleNamespace

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


def test_reset_transform_pushes_default_to_program_viewer() -> None:
    """Resetting properties must also replace the viewer handle transform."""
    transform = TransformParams(translate_x=320.0, scale_x=2.0, scale_y=2.0)
    clip = ClipInstance(
        clip_id="clip-1",
        asset_id="asset-1",
        track_id="track-1",
        timeline_start=0,
        timeline_duration=24,
        source_start=0,
        source_duration=24,
        speed=SpeedParams(speed=1.0),
        transform=transform,
        crop=CropParams(),
        opacity=1.0,
    )
    timeline = TimelineSequence(
        timeline_id="timeline-1",
        name="Test",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
    ).with_track_added(
        EditTrack(track_id="track-1", name="Video 1", track_type="video", index=0)
    ).with_clip_added("track-1", clip)

    viewer = SimpleNamespace(is_transform_tool_active=lambda: True)
    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller.edit_page = SimpleNamespace(
        _current_timeline=timeline,
        _program_viewer=viewer,
        _command_stack=SimpleNamespace(
            execute=lambda command, current: command.apply(current),
        ),
    )
    controller.timeline_preview_renderer = None
    controller.edit_properties_panel = SimpleNamespace(_current_clips=[clip])
    controller._show_clip_in_properties = lambda _clip: None
    controller._request_program_viewer_update = lambda: None
    pushed = []
    controller._push_selected_clip_to_transform_viewer = lambda: pushed.append(True)

    controller._on_edit_reset_transform(["clip-1"])

    reset_clip = controller.edit_page._current_timeline.get_clip_by_id("clip-1")
    assert reset_clip is not None
    assert reset_clip.transform == TransformParams()
    assert pushed == [True]


def test_playhead_change_clears_stale_transform_handles_and_restores_them() -> None:
    """Transform handles must disappear on another clip and return on scrub back."""
    first_clip = ClipInstance(
        clip_id="clip-1",
        asset_id="asset-1",
        track_id="track-1",
        timeline_start=0,
        timeline_duration=24,
        source_start=0,
        source_duration=24,
        speed=SpeedParams(speed=1.0),
        transform=TransformParams(translate_x=24.0),
        crop=CropParams(),
        opacity=1.0,
    )
    second_clip = ClipInstance(
        clip_id="clip-2",
        asset_id="asset-2",
        track_id="track-1",
        timeline_start=24,
        timeline_duration=24,
        source_start=0,
        source_duration=24,
        speed=SpeedParams(speed=1.0),
        transform=TransformParams(),
        crop=CropParams(),
        opacity=1.0,
    )
    timeline = TimelineSequence(
        timeline_id="timeline-1",
        name="Test",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
    ).with_track_added(
        EditTrack(track_id="track-1", name="Video 1", track_type="video", index=0)
    ).with_clip_added("track-1", first_clip).with_clip_added("track-1", second_clip)

    calls = []
    viewer = SimpleNamespace(
        is_transform_tool_active=lambda: True,
        set_selected_clip_transform=lambda clip_id, transform, width, height: calls.append(
            (clip_id, transform, width, height)
        ),
    )
    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller.edit_page = SimpleNamespace(
        _current_timeline=timeline,
        _program_viewer=viewer,
    )
    controller.edit_properties_panel = SimpleNamespace(_current_clips=[first_clip])

    controller._on_edit_playhead_changed(30)
    assert calls[-1][0] is None

    controller._on_edit_playhead_changed(12)
    assert calls[-1][0] == "clip-1"
    assert calls[-1][1] == first_clip.transform
