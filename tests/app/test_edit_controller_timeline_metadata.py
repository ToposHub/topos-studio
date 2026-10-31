from __future__ import annotations

from dataclasses import replace

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


class _MediaPool:
    def __init__(self) -> None:
        self.projects: list[EditProject] = []

    def set_project(self, project: EditProject) -> None:
        self.projects.append(project)


class _EditPage:
    def __init__(self) -> None:
        self.timeline: TimelineSequence | None = None

    def set_timeline(self, timeline: TimelineSequence) -> None:
        self.timeline = timeline


class _Controller(EditControllerMixin):
    def __init__(self, project: EditProject) -> None:
        self._current_edit_project = project
        self._current_timeline_sequences: dict[str, TimelineSequence] = {}
        self.edit_page = _EditPage()
        self.media_pool_panel = _MediaPool()
        self.timeline_preview_renderer = None
        self.preview_requests = 0

    def _request_current_timeline_preview_frame(self) -> None:
        self.preview_requests += 1


class _Renderer:
    def __init__(self) -> None:
        self.sequences: list[TimelineSequence] = []
        self.projects: list[EditProject] = []
        self._project: EditProject | None = None

    def _build_sequence_render_signature(self, sequence: TimelineSequence) -> tuple[object, ...]:
        tracks: list[tuple[object, ...]] = []
        for track in sequence.video_tracks:
            if not track.clips and not track.transitions:
                continue
            tracks.append((
                track.track_id,
                tuple(
                    (
                        clip.clip_id,
                        clip.asset_id,
                        clip.timeline_start,
                        clip.timeline_duration,
                        clip.source_start,
                        clip.source_duration,
                    )
                    for clip in track.clips
                ),
            ))
        return (sequence.width, sequence.height, float(sequence.fps), tuple(tracks))

    def set_sequence(self, sequence: TimelineSequence) -> None:
        self.sequences.append(sequence)

    def set_project(self, project: EditProject) -> None:
        self.projects.append(project)
        self._project = project


def test_timeline_change_updates_media_pool_reference_fps() -> None:
    timeline_id = "sequence-25"
    project = EditProject.create("Timeline metadata")
    project = replace(
        project,
        timelines={
            timeline_id: TimelineSequenceRef(
                timeline_id=timeline_id,
                name="Sequence 1",
                fps=24.0,
                width=1920,
                height=1080,
                format_settings={"start_timecode": "01:00:00:00"},
            ),
        },
        active_timeline_id=timeline_id,
    )
    sequence = TimelineSequence(
        timeline_id=timeline_id,
        name="Sequence 1",
        fps=Rational(25, 1),
        width=1920,
        height=1080,
    )
    controller = _Controller(project)

    controller._on_edit_timeline_changed(sequence)

    assert controller._current_edit_project.timelines[timeline_id].fps == 25.0
    assert (
        controller._current_edit_project.timelines[timeline_id].format_settings
        == {"start_timecode": "01:00:00:00"}
    )
    assert controller.edit_page.timeline == sequence
    assert controller.media_pool_panel.projects[-1] is controller._current_edit_project


def test_empty_track_change_does_not_force_renderer_preview_request() -> None:
    timeline_id = "sequence-empty-track"
    project = EditProject.create("Timeline metadata")
    clip = ClipInstance(
        clip_id="clip-1",
        asset_id="asset-1",
        track_id="v1",
        timeline_start=0,
        timeline_duration=24,
        source_start=0,
        source_duration=24,
    )
    base = TimelineSequence(
        timeline_id=timeline_id,
        name="Sequence 1",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
    )
    with_empty_track = TimelineSequence(
        timeline_id=timeline_id,
        name="Sequence 1",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            base.video_tracks[0],
            EditTrack("v2", "V2", "video", 1, clips=()),
        ),
    )
    controller = _Controller(project)
    renderer = _Renderer()
    controller.timeline_preview_renderer = renderer
    controller._current_timeline_sequences[timeline_id] = base

    controller._on_edit_timeline_changed(with_empty_track)

    assert controller._current_timeline_sequences[timeline_id] == with_empty_track
    assert controller.edit_page.timeline == with_empty_track
    assert renderer.sequences == [with_empty_track]
    assert controller.preview_requests == 0


def test_timeline_change_publishes_new_project_snapshot_to_preview_renderer() -> None:
    """Comp Clip graphs must be visible to the timeline preview worker."""
    timeline_id = "sequence-comp-clip"
    project = EditProject.create("Composite playback")
    sequence = TimelineSequence(
        timeline_id=timeline_id,
        name="Sequence 1",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
    )
    controller = _Controller(project)
    renderer = _Renderer()
    renderer._project = project
    controller.timeline_preview_renderer = renderer
    controller._current_timeline_sequences[timeline_id] = sequence

    graph = {"composite_id": "graph-1", "nodes": [], "connections": []}
    controller._current_edit_project = replace(
        project,
        compositions={"graph-1": graph},
    )

    controller._on_edit_timeline_changed(sequence)

    assert renderer.projects
    assert renderer.projects[-1] is controller._current_edit_project
    assert renderer.projects[-1].compositions["graph-1"] == graph
