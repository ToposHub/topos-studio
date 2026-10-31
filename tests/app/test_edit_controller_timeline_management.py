from __future__ import annotations

from dataclasses import replace

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
    TransitionInstance,
)
from src.shared.media.metadata import Rational


class _MediaPool:
    def __init__(self) -> None:
        self.projects: list[EditProject] = []
        self.current_timeline_ids: list[str | None] = []

    def set_project(self, project: EditProject) -> None:
        self.projects.append(project)

    def set_current_timeline(self, timeline_id: str | None) -> None:
        self.current_timeline_ids.append(timeline_id)


class _TimelinePanel:
    def __init__(self) -> None:
        self.loaded: list[TimelineSequence] = []
        self.clear_count = 0
        self.projects: list[EditProject] = []

    def load_timeline(self, timeline: TimelineSequence) -> None:
        self.loaded.append(timeline)

    def clear(self) -> None:
        self.clear_count += 1

    def set_project(self, project) -> None:
        # 对齐真实 TimelinePanel.set_project（edit_controller._on_timeline_switched 调用）
        self.projects.append(project)


class _EditPage:
    def __init__(self) -> None:
        self.timeline: TimelineSequence | None = None

    def set_timeline(self, timeline: TimelineSequence | None) -> None:
        self.timeline = timeline


class _Controller(EditControllerMixin):
    def __init__(self, project: EditProject) -> None:
        self._current_edit_project = project
        self._current_timeline_sequences: dict[str, TimelineSequence] = {}
        self.media_pool_panel = _MediaPool()
        self.timeline_panel = _TimelinePanel()
        self.edit_page = _EditPage()
        self.timeline_preview_renderer = None
        self.renderer_initializations = 0

    def _init_timeline_preview_renderer(self) -> None:
        self.renderer_initializations += 1


def _timeline(timeline_id: str, fps: Rational = Rational(24, 1)) -> TimelineSequence:
    video_clip = ClipInstance(
        clip_id=f"{timeline_id}-video",
        asset_id="asset-video",
        track_id=f"{timeline_id}-v1",
        timeline_start=24,
        timeline_duration=24,
        source_start=0,
        source_duration=24,
    )
    audio_clip = ClipInstance(
        clip_id=f"{timeline_id}-audio",
        asset_id="asset-audio",
        track_id=f"{timeline_id}-a1",
        timeline_start=24,
        timeline_duration=24,
        source_start=0,
        source_duration=24,
    )
    transition = TransitionInstance(
        transition_id=f"{timeline_id}-transition",
        transition_type="cross_dissolve",
        track_id=f"{timeline_id}-v1",
        left_clip_id=f"{timeline_id}-video",
        right_clip_id=f"{timeline_id}-video",
        center_frame=24,
        duration=12,
    )
    return TimelineSequence(
        timeline_id=timeline_id,
        name=timeline_id,
        fps=fps,
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(
            EditTrack(
                track_id=f"{timeline_id}-v1",
                name="V1",
                track_type="video",
                clips=(video_clip,),
                transitions=(transition,),
            ),
        ),
        audio_tracks=(
            EditTrack(
                track_id=f"{timeline_id}-a1",
                name="A1",
                track_type="audio",
                clips=(audio_clip,),
            ),
        ),
        range_start=24,
        range_end=48,
    )


def test_timeline_ref_materializes_when_opened() -> None:
    timeline_id = "sequence-2997"
    project = EditProject.create("Timeline management")
    project = replace(
        project,
        timelines={
            timeline_id: TimelineSequenceRef(
                timeline_id=timeline_id,
                name="Sequence 29.97",
                fps=29.97,
                width=2560,
                height=1440,
                sample_rate=48000,
                format_settings={"start_timecode": "01:00:00:00"},
            ),
        },
        active_timeline_id=timeline_id,
    )
    controller = _Controller(project)

    controller._on_timeline_switched(timeline_id)

    sequence = controller._current_timeline_sequences[timeline_id]
    assert sequence.fps == Rational(30000, 1001)
    assert sequence.width == 2560
    assert sequence.format_settings["start_timecode"] == "01:00:00:00"
    assert len(sequence.video_tracks) == 1
    assert len(sequence.audio_tracks) == 1
    assert controller.timeline_panel.loaded[-1] is sequence


def test_timeline_timebase_change_preserves_absolute_clip_timing() -> None:
    controller = _Controller(EditProject.create("Timeline retime"))
    source = _timeline("sequence-24")

    retimed = controller._with_timeline_fps(source, Rational(25, 1))

    video_clip = retimed.video_tracks[0].clips[0]
    audio_clip = retimed.audio_tracks[0].clips[0]
    transition = retimed.video_tracks[0].transitions[0]
    assert retimed.fps == Rational(25, 1)
    assert (video_clip.timeline_start, video_clip.timeline_duration) == (25, 25)
    assert (audio_clip.timeline_start, audio_clip.timeline_duration) == (25, 25)
    assert (video_clip._timeline_fps_num, video_clip._timeline_fps_den) == (25, 1)
    assert (transition.center_frame, transition.duration) == (25, 12)
    assert (retimed.range_start, retimed.range_end) == (25, 50)


def test_timeline_timebase_change_accepts_legacy_float_fps() -> None:
    controller = _Controller(EditProject.create("Timeline retime legacy"))
    source = replace(_timeline("sequence-float"), fps=24.0)

    retimed = controller._with_timeline_fps(source, Rational(25, 1))

    assert retimed.fps == Rational(25, 1)
    assert retimed.video_tracks[0].clips[0].timeline_start == 25


def test_timeline_delete_updates_project_sequence_and_active_timeline_together() -> None:
    project = EditProject.create("Timeline deletion")
    project = replace(
        project,
        timelines={
            "first": TimelineSequenceRef("first", "First", fps=24.0),
            "second": TimelineSequenceRef("second", "Second", fps=25.0),
        },
        active_timeline_id="first",
    )
    controller = _Controller(project)
    controller._current_timeline_sequences = {
        "first": _timeline("first"),
        "second": _timeline("second", Rational(25, 1)),
    }

    assert controller._remove_timeline_state("first") is True

    assert "first" not in controller._current_edit_project.timelines
    assert "first" not in controller._current_timeline_sequences
    assert controller._current_edit_project.active_timeline_id == "second"
    assert controller.timeline_panel.loaded[-1].timeline_id == "second"
    assert controller.media_pool_panel.projects[-1] is controller._current_edit_project


def test_sequence_format_settings_survive_serialization() -> None:
    sequence = replace(
        _timeline("format-sequence"),
        format_settings={
            "pixel_aspect_ratio": 1.0,
            "field_order": "progressive",
            "start_timecode": "01:00:00:00",
        },
    )

    restored = TimelineSequence.from_dict(sequence.to_dict())

    assert restored.format_settings == sequence.format_settings
