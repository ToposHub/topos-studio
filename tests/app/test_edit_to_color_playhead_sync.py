"""Plain Edit→Color page switches must follow the Edit playhead.

A bare tab click into Color used to keep whatever clip the page showed when
it was last visible. These tests pin the routed behaviour: switch_to_page
resolves the clip under the playhead and enters Color through the explicit
timeline-clip path at that exact frame, while gap/contextual entries keep
the legacy plain switch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.app.controllers.page_controller import PageManagementMixin
from src.app.main_window import PageContextManager
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

def _clip(clip_id: str, asset_id: str, timeline_start: int, source_start: int) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id="v1",
        timeline_start=timeline_start,
        timeline_duration=40,
        source_start=source_start,
        source_duration=40,
        color_stack_id=f"stack-{clip_id}",
    )


def _project() -> tuple[EditProject, str]:
    project = EditProject.create("Edit Color Playhead Sync")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0, "total_frames": 300},
    )
    asset_id = next(iter(project.assets))
    # create_timeline marks the new timeline as active_timeline_id.
    return project.create_timeline("Main Timeline"), asset_id


def _timeline(clips: tuple[ClipInstance, ...], timeline_id: str) -> TimelineSequence:
    timeline = TimelineSequence(
        timeline_id=timeline_id,
        name="Main Timeline",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
    )
    timeline = timeline.with_track_added(
        EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    )
    for clip in clips:
        timeline = timeline.with_clip_added("v1", clip)
    return timeline


class _SyncController(PageManagementMixin):
    """Edit-page stand-in exercising the switch_to_page playhead route."""

    def __init__(
        self,
        project: EditProject | None,
        timeline: TimelineSequence | None,
        playhead: int,
    ) -> None:
        self.current_page = "Edit"
        self._current_edit_project = project
        self._current_timeline_sequences = (
            {timeline.timeline_id: timeline} if timeline is not None else {}
        )
        self._page_context_manager = PageContextManager()
        self._contextual_page_switch_in_progress = False
        self._edit_available = False
        self.feature_registry = None
        self.edit_page = None
        self.timeline_panel = SimpleNamespace(get_playhead_frame=lambda: playhead)
        # Plain-switch bookkeeping stubs. When the playhead route claims the
        # switch, none of these may be touched.
        self.canvas = MagicMock()
        self.engine = SimpleNamespace()
        self.mask_panel = SimpleNamespace(
            btn_brush=SimpleNamespace(setChecked=lambda checked: None),
        )
        self.qualifier_panel = SimpleNamespace(
            set_picker_active=lambda active: None,
        )
        self.status_bar = SimpleNamespace(showMessage=lambda *a, **k: None)
        self.dock_properties = None
        self.page_buttons: dict[str, object] = {}
        self.page_layouts: dict[str, object] = {}
        self.update_all_dock_titlebars = MagicMock()
        self.playhead_routes: list[tuple[str, str, int]] = []

    def saveState(self):  # noqa: N802 - Qt naming convention
        return MagicMock(name="saved-layout-state")


def _three_clip_controller(playhead: int) -> tuple[_SyncController, list[ClipInstance]]:
    project, asset_id = _project()
    clips = [
        _clip("clip-c1", asset_id, 0, 0),
        _clip("clip-c2", asset_id, 40, 10),
        _clip("clip-c3", asset_id, 80, 100),
    ]
    controller = _SyncController(
        project,
        _timeline(tuple(clips), project.active_timeline_id),
        playhead,
    )
    return controller, clips


def test_playhead_on_third_clip_opens_that_clip_at_playhead_frame() -> None:
    controller, _clips = _three_clip_controller(playhead=90)
    routed_contexts: list[object] = []
    controller.switch_to_page_with_context = routed_contexts.append

    controller.switch_to_page("Color")

    assert len(routed_contexts) == 1
    context = routed_contexts[0]
    assert context.clip_id == "clip-c3"
    assert context.timeline_frame == 90
    assert context.clip_local_frame == 10
    assert context.source_frame == 110
    # The plain switch must not run: the contextual path performs it.
    assert controller.current_page == "Edit"
    controller.canvas.cancel_active_tools.assert_not_called()


def test_playhead_on_second_clip_keeps_exact_position() -> None:
    controller, _clips = _three_clip_controller(playhead=55)
    routed_contexts: list[object] = []
    controller.switch_to_page_with_context = routed_contexts.append

    controller.switch_to_page("Color")

    assert len(routed_contexts) == 1
    context = routed_contexts[0]
    assert context.clip_id == "clip-c2"
    assert context.timeline_frame == 55
    assert context.clip_local_frame == 15
    assert context.source_frame == 25


def test_playhead_in_gap_falls_back_to_plain_switch(qapp) -> None:
    controller, _clips = _three_clip_controller(playhead=200)
    controller.open_timeline_clip_in_color = MagicMock()

    controller.switch_to_page("Color")

    controller.open_timeline_clip_in_color.assert_not_called()
    assert controller.current_page == "Color"
    controller.canvas.cancel_active_tools.assert_called_once()


def test_contextual_entry_does_not_reenter_playhead_route(qapp) -> None:
    controller, _clips = _three_clip_controller(playhead=90)
    controller._contextual_page_switch_in_progress = True
    controller.open_timeline_clip_in_color = MagicMock()

    controller.switch_to_page("Color")

    controller.open_timeline_clip_in_color.assert_not_called()
    assert controller.current_page == "Color"


def test_real_context_wrapper_skips_playhead_route_and_resets_guard(qapp) -> None:
    """The live switch_to_page_with_context guard covers the inner plain switch."""
    controller, clips = _three_clip_controller(playhead=90)
    controller.open_timeline_clip_in_color = MagicMock()
    controller._handle_enter_color_with_context = MagicMock()
    third_clip = clips[2]
    context = controller._page_context_manager.create_context_for_edit_to_color(
        clip_id=third_clip.clip_id,
        asset_id=third_clip.asset_id,
        timeline_id=controller._current_edit_project.active_timeline_id,
        timeline_frame=90,
        source_frame=110,
        clip_local_frame=10,
        color_stack_id=third_clip.color_stack_id,
    )

    controller.switch_to_page_with_context(context)

    # The inner plain switch ran exactly once and the playhead route stayed
    # out of it; the guard flag must be reset by the time the wrapper returns.
    controller.open_timeline_clip_in_color.assert_not_called()
    controller.canvas.cancel_active_tools.assert_called_once()
    controller._handle_enter_color_with_context.assert_called_once_with(context)
    assert controller._contextual_page_switch_in_progress is False


def test_boundary_frame_covers_next_clip_not_previous() -> None:
    """get_clip_at is half-open: the clip end frame belongs to the next clip."""
    controller, clips = _three_clip_controller(playhead=80)
    controller.open_timeline_clip_in_color = MagicMock()

    handled = controller._switch_to_color_following_edit_playhead()

    assert handled is True
    controller.open_timeline_clip_in_color.assert_called_once_with(
        controller._current_edit_project.active_timeline_id, clips[2].clip_id, 80
    )


def test_force_switch_skips_playhead_route(qapp) -> None:
    """force=True is the dock-restore path; it must keep the plain behaviour."""
    controller, _clips = _three_clip_controller(playhead=90)
    controller.open_timeline_clip_in_color = MagicMock()

    controller.switch_to_page("Color", force=True)

    controller.open_timeline_clip_in_color.assert_not_called()
    assert controller.current_page == "Color"


def test_unavailable_page_fallback_to_color_keeps_plain_switch(qapp) -> None:
    """A Composite/Deliver tab falling back to Color must not hijack the clip."""
    controller, _clips = _three_clip_controller(playhead=90)
    controller._composite_available = False
    controller._deliver_available = False
    controller._edit_available = False
    controller.open_timeline_clip_in_color = MagicMock()

    controller.switch_to_page("Composite")

    assert controller._is_page_available("Composite") is False
    controller.open_timeline_clip_in_color.assert_not_called()
    assert controller.current_page == "Color"


def test_switch_from_deliver_does_not_follow_playhead(qapp) -> None:
    """Deliver shares the timeline panel; only Edit entries may route."""
    controller, _clips = _three_clip_controller(playhead=90)
    controller.current_page = "Deliver"
    controller.open_timeline_clip_in_color = MagicMock()

    controller.switch_to_page("Color")

    controller.open_timeline_clip_in_color.assert_not_called()
    assert controller.current_page == "Color"


def test_non_visual_asset_under_playhead_falls_back() -> None:
    """A video-track clip whose asset is not video/image must not route."""
    project, _asset_id = _project()
    audio_asset_id = "asset-audio"
    clips = [ClipInstance(
        clip_id="clip-audio",
        asset_id=audio_asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=40,
        source_start=0,
        source_duration=40,
    )]
    controller = _SyncController(
        project,
        _timeline(tuple(clips), project.active_timeline_id),
        playhead=10,
    )
    project_assets = dict(controller._current_edit_project.assets)
    project_assets[audio_asset_id] = SimpleNamespace(
        is_video=False, is_image=False, metadata={},
    )
    controller._current_edit_project = SimpleNamespace(
        active_timeline_id=controller._current_edit_project.active_timeline_id,
        assets=project_assets,
    )
    controller.open_timeline_clip_in_color = MagicMock()

    assert controller._switch_to_color_following_edit_playhead() is False
    controller.open_timeline_clip_in_color.assert_not_called()


def test_overlap_prefers_topmost_video_track() -> None:
    """AUD-3103 (fixed): overlapping video tracks resolve topmost-first.

    The Edit→Color playhead route now shares the topmost-enabled-clip
    resolver with the Composite playhead follow (_find_clip_under_
    composite_playhead), mirroring the edit preview's layering: the highest
    video-track index wins and disabled clips are gaps.  The old code took
    the first hit in natural track order (and ignored is_enabled), which
    opened the bottom clip while the Composite path opened the top one.
    """
    project, asset_id = _project()
    clip_v1 = ClipInstance(
        clip_id="clip-v1",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        color_stack_id="stack-clip-v1",
    )
    clip_v2 = ClipInstance(
        clip_id="clip-v2",
        asset_id=asset_id,
        track_id="v2",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        color_stack_id="stack-clip-v2",
    )
    timeline = _timeline((clip_v1,), project.active_timeline_id)
    timeline = timeline.with_track_added(
        EditTrack(track_id="v2", name="V2", track_type="video", index=1)
    )
    timeline = timeline.with_clip_added("v2", clip_v2)
    controller = _SyncController(project, timeline, playhead=60)
    controller.open_timeline_clip_in_color = MagicMock()

    handled = controller._switch_to_color_following_edit_playhead()

    assert handled is True
    controller.open_timeline_clip_in_color.assert_called_once_with(
        project.active_timeline_id, "clip-v2", 60
    )


def test_overlap_skips_disabled_topmost_clip() -> None:
    """AUD-3103: a disabled topmost clip is a gap — the next layer wins."""
    project, asset_id = _project()
    clip_v1 = ClipInstance(
        clip_id="clip-v1",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        color_stack_id="stack-clip-v1",
    )
    clip_v2 = ClipInstance(
        clip_id="clip-v2",
        asset_id=asset_id,
        track_id="v2",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        color_stack_id="stack-clip-v2",
        is_enabled=False,
    )
    timeline = _timeline((clip_v1,), project.active_timeline_id)
    timeline = timeline.with_track_added(
        EditTrack(track_id="v2", name="V2", track_type="video", index=1)
    )
    timeline = timeline.with_clip_added("v2", clip_v2)
    controller = _SyncController(project, timeline, playhead=60)
    controller.open_timeline_clip_in_color = MagicMock()

    handled = controller._switch_to_color_following_edit_playhead()

    assert handled is True
    controller.open_timeline_clip_in_color.assert_called_once_with(
        project.active_timeline_id, "clip-v1", 60
    )


def test_route_disabled_without_project_or_timeline() -> None:
    project, _asset_id = _project()
    no_project = _SyncController(None, None, playhead=10)
    no_timeline = _SyncController(project, None, playhead=10)

    assert no_project._switch_to_color_following_edit_playhead() is False
    assert no_timeline._switch_to_color_following_edit_playhead() is False


def test_route_reads_edit_page_frame_when_panel_unavailable() -> None:
    project, asset_id = _project()
    clips = [_clip("clip-c1", asset_id, 0, 0)]
    controller = _SyncController(
        project, _timeline(tuple(clips), project.active_timeline_id), playhead=0
    )
    controller.timeline_panel = SimpleNamespace(
        get_playhead_frame=MagicMock(side_effect=AttributeError("no panel")),
    )
    controller.edit_page = SimpleNamespace(_current_frame=7)
    controller.open_timeline_clip_in_color = MagicMock()

    handled = controller._switch_to_color_following_edit_playhead()

    assert handled is True
    controller.open_timeline_clip_in_color.assert_called_once_with(
        project.active_timeline_id, "clip-c1", 7
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
