from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from src.app.controllers.composite_controller import CompositeControllerMixin
from src.app.main_window import PageContextManager, PageType
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence


class _FakeCompositePage:
    def __init__(self) -> None:
        self.loaded_data = None
        self.context_calls = []

    def load_composite_data(self, data) -> None:
        self.loaded_data = data

    def set_timeline_clip_context(self, runtime_context=None, gpu_provider=None) -> None:
        self.context_calls.append((runtime_context, gpu_provider))


class _Controller(CompositeControllerMixin):
    def __init__(self, project: EditProject, timeline: TimelineSequence, playhead: int) -> None:
        self._current_edit_project = project
        self._current_timeline_sequences = {timeline.timeline_id: timeline}
        self._page_context_manager = PageContextManager()
        self._composite_available = True
        self._current_composite_id = ""
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.timeline_panel = SimpleNamespace(get_playhead_frame=lambda: playhead)
        self.composite_page = _FakeCompositePage()
        self.switched_context = None
        self.switched_page = None

    def switch_to_page_with_context(self, context) -> None:
        self.switched_context = context

    def switch_to_page(self, page_name: str) -> None:
        self.switched_page = page_name


def _project_with_timeline_clip() -> tuple[EditProject, TimelineSequence, ClipInstance]:
    project = EditProject.create("Composite Context Test")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "total_frames": 200,
            "duration_seconds": 200 / 24.0,
        },
        display_name="source.mov",
    )
    asset_id = next(asset.asset_id for asset in project.assets.values() if asset.path == "/tmp/source.mov")

    timeline = TimelineSequence(
        timeline_id="tl-context",
        name="Timeline",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = timeline.with_track_added(track)
    clip = ClipInstance(
        clip_id="clip-context",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=100,
        timeline_duration=40,
        source_start=50,
        source_duration=40,
        color_stack_id="stack-context",
    )
    timeline = timeline.with_clip_added("v1", clip)
    return project, timeline, clip


def test_timeline_backed_graph_open_uses_graph_clip_context() -> None:
    project, timeline, clip = _project_with_timeline_clip()
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_comp_clip_from_timeline_clip(
        timeline_id=timeline.timeline_id,
        clip_id=clip.clip_id,
        timeline=timeline,
    )

    controller = _Controller(project, timeline, playhead=123)
    controller._switch_to_composite_page(graph_id)

    context = controller.switched_context
    assert context is not None
    assert context.target_page == PageType.COMPOSITE
    assert context.composite_id == graph_id
    assert context.timeline_id == timeline.timeline_id
    assert context.clip_id == clip.clip_id
    assert context.asset_id == clip.asset_id
    assert context.timeline_frame == 123
    assert context.clip_local_frame == 23
    assert context.source_frame == 73
    assert context.color_stack_id == "stack-context"
    assert controller.switched_page is None


def test_timeline_comp_clip_names_are_project_unique_and_numbered() -> None:
    project, timeline, clip = _project_with_timeline_clip()

    project, _first_comp_id, _first_asset_id, first_graph_id = (
        project.create_comp_clip_from_timeline_clip(
            timeline_id=timeline.timeline_id,
            clip_id=clip.clip_id,
            timeline=timeline,
        )
    )
    first_graph = project.compositions[first_graph_id]
    assert first_graph["name"] == "Comp Clip 1"

    project, _second_comp_id, _second_asset_id, second_graph_id = (
        project.create_comp_clip_from_timeline_clip(
            timeline_id=timeline.timeline_id,
            clip_id=clip.clip_id,
            timeline=timeline,
        )
    )
    second_graph = project.compositions[second_graph_id]
    assert second_graph["name"] == "Comp Clip 2"


def test_timeline_context_uses_live_clip_range_and_timeline_format() -> None:
    project, timeline, clip = _project_with_timeline_clip()
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_comp_clip_from_timeline_clip(
        timeline_id=timeline.timeline_id,
        clip_id=clip.clip_id,
        timeline=timeline,
    )
    # Simulate a graph saved before the Edit clip was trimmed. The current
    # timeline segment must win when building the runtime context.
    project.compositions[graph_id]["duration_frames"] = 999

    controller = _Controller(project, timeline, playhead=100)
    controller._current_project_id = project.project_id
    controller.engine = SimpleNamespace()
    controller._timeline_clip_frame_provider = None
    context = controller._page_context_manager.create_context_for_edit_to_composite(
        clip_id=clip.clip_id,
        composite_id=graph_id,
        asset_id=clip.asset_id,
        timeline_id=timeline.timeline_id,
        timeline_frame=clip.timeline_start,
        source_frame=clip.source_start,
        clip_local_frame=0,
    )

    controller._setup_timeline_clip_context(context, graph_id, clip.clip_id)

    runtime, _provider = controller.composite_page.context_calls[-1]
    assert runtime.width == timeline.width
    assert runtime.height == timeline.height
    assert runtime.fps == 24.0
    assert runtime.duration_frames == clip.timeline_duration


def test_timeline_backed_graph_open_clamps_playhead_to_clip_start() -> None:
    project, timeline, clip = _project_with_timeline_clip()
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_comp_clip_from_timeline_clip(
        timeline_id=timeline.timeline_id,
        clip_id=clip.clip_id,
        timeline=timeline,
    )

    controller = _Controller(project, timeline, playhead=500)
    controller._switch_to_composite_page(graph_id)

    context = controller.switched_context
    assert context.timeline_frame == clip.timeline_start
    assert context.clip_local_frame == 0
    assert context.source_frame == clip.source_start


def test_empty_comp_clip_open_clears_stale_timeline_context() -> None:
    project = EditProject.create("Empty Comp Context Test")
    project, comp_clip_id, _comp_asset_id, graph_id = project.create_empty_comp_clip()
    timeline = TimelineSequence(timeline_id="tl-empty", name="Timeline")

    controller = _Controller(project, timeline, playhead=0)
    controller._switch_to_composite_page(graph_id)

    assert project.active_comp_clip_id == comp_clip_id
    assert controller.switched_context is None
    assert controller.switched_page == "Composite"
    assert controller.composite_page.loaded_data == project.get_composition_by_id(graph_id)
    assert controller.composite_page.context_calls == [(None, None)]


def test_empty_comp_clip_open_does_not_leak_auto_bind_suppress_flag() -> None:
    """Opening a graph while already on Composite must not leak the one-shot
    auto-bind suppress flag: switch_to_page early-returns there, so nobody
    consumes it, and a stale True makes the next tab return skip workspace
    restore and reset the open composition."""
    project = EditProject.create("Suppress Flag Leak Test")
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_empty_comp_clip()
    timeline = TimelineSequence(timeline_id="tl-suppress", name="Timeline")

    controller = _Controller(project, timeline, playhead=0)
    controller._switch_to_composite_page(graph_id)

    assert controller.switched_page == "Composite"
    assert controller._suppress_composite_auto_bind_once is False


def test_legacy_graph_open_restores_missing_composite_identity() -> None:
    """Graphs keyed by ID still activate Composite when their payload is old."""
    project = EditProject.create("Legacy Composite Open Test")
    project, comp_clip_id, asset_id, graph_id = project.create_empty_comp_clip()
    legacy_graph = dict(project.compositions[graph_id])
    legacy_graph.pop("composite_id", None)
    legacy_graph.pop("comp_clip_id", None)
    legacy_graph.pop("comp_asset_id", None)
    legacy_graph.pop("source_kind", None)
    project = replace(project, compositions={graph_id: legacy_graph})
    timeline = TimelineSequence(timeline_id="tl-legacy", name="Timeline")

    controller = _Controller(project, timeline, playhead=0)
    controller._switch_to_composite_page(graph_id)

    assert controller.composite_page.loaded_data["composite_id"] == graph_id
    assert controller.composite_page.loaded_data["comp_clip_id"] == comp_clip_id
    assert controller.composite_page.loaded_data["comp_asset_id"] == asset_id
    assert controller.composite_page.loaded_data["source_kind"] == "empty_comp_clip"


def test_unchanged_composite_graph_does_not_rewrite_edit_project() -> None:
    """Playback graph evaluation must not create a new project revision."""
    project, timeline, clip = _project_with_timeline_clip()
    project, _comp_clip_id, _comp_asset_id, graph_id = project.create_comp_clip_from_timeline_clip(
        timeline_id=timeline.timeline_id,
        clip_id=clip.clip_id,
        timeline=timeline,
    )
    controller = _Controller(project, timeline, playhead=clip.timeline_start)
    graph_data = project.compositions[graph_id]

    controller._sync_composite_graph_to_edit_project(graph_id, graph_data)

    assert controller._current_edit_project is project
