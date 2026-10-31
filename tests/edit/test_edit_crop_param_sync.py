"""Regression tests for consecutive Crop parameter edits."""

from types import SimpleNamespace

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.clip_params import CropParams, SpeedParams
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


def _make_timeline() -> TimelineSequence:
    clip = ClipInstance(
        clip_id="clip-1",
        asset_id="asset-1",
        track_id="track-1",
        timeline_start=0,
        timeline_duration=24,
        source_start=0,
        source_duration=24,
        speed=SpeedParams(speed=1.0),
        crop=CropParams(),
    )
    return TimelineSequence(
        timeline_id="timeline-1",
        name="Test",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
    ).with_track_added(
        EditTrack(track_id="track-1", name="Video 1", track_type="video", index=0)
    ).with_clip_added("track-1", clip)


def test_consecutive_crop_edits_accumulate_all_sides_and_feather() -> None:
    """Editing one side must not rebuild the next value from a stale clip."""
    timeline = _make_timeline()
    panel = SimpleNamespace(_current_clips=[timeline.get_clip_by_id("clip-1")])

    def update_current_clip(clip) -> None:
        panel._current_clips[0] = clip

    panel.update_current_clip = update_current_clip

    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller.edit_page = SimpleNamespace(
        _current_timeline=timeline,
        _command_stack=SimpleNamespace(
            execute=lambda command, current: command.apply(current),
        ),
    )
    controller.edit_properties_panel = panel
    controller.timeline_preview_renderer = None
    controller._request_program_viewer_update = lambda: None

    for param_name, value in (
        ("left", 25.0),
        ("right", 20.0),
        ("top", 15.0),
        ("bottom", 10.0),
        ("feather", 12.0),
    ):
        current = panel._current_clips[0].crop
        if param_name == "feather":
            new_crop = current.with_feather(value)
        else:
            new_crop = current.with_crop(**{param_name: value / 100.0})
        controller._on_edit_clip_param_changed(
            "clip-1", "crop", param_name, new_crop,
        )

    crop = controller.edit_page._current_timeline.get_clip_by_id("clip-1").crop
    assert crop == CropParams(
        left=0.25, right=0.20, top=0.15, bottom=0.10, feather=12.0,
    )
