"""W6-G6 regression tests for AUD-16200 / AUD-2701 / AUD-2702.

Common theme: undo and parameter-edit paths must never rebuild domain
objects (TimelineSequence / EditProject / MediaAsset) through manual
constructors — a manual constructor silently drops every field added after
the original set, which is data loss on undo / on every parameter edit.

The "new field must not be lost" guards below iterate
``dataclasses.fields(...)`` of the *current* class definition, so any field
added in the future is covered automatically: if a rebuild path regresses
to a manual constructor that misses it, these tests fail without needing
to be touched.
"""

from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.edit_core

from src.shared.media.metadata import Rational
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.core.commands import (
    CommandStack,
    InsertLinkedAssetCommand,
)
from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    SmartBin,
)
from src.app.controllers.edit_controller import EditControllerMixin


# ================================================================
# AUD-16200: InsertLinkedAssetCommand.undo field preservation
# ================================================================

def _rich_timeline() -> TimelineSequence:
    """A timeline exercising every non-track field of TimelineSequence.

    Distinctive values make any field dropped by a manual rebuild stand
    out immediately (defaults would hide the loss).
    """
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    a1 = EditTrack(track_id="a1", name="A1", track_type="audio", index=0)
    blocker = ClipInstance(
        clip_id="blocker_audio",
        asset_id="asset_other",
        track_id="a1",
        timeline_start=0,
        timeline_duration=200,
        source_start=0,
        source_duration=200,
        speed=1.0,
    )
    subtitle_clip = SubtitleClip(
        clip_id="sub_1",
        track_id="s1",
        timeline_start=5,
        timeline_duration=40,
        text="must survive undo",
    )
    s1 = EditTrack(
        track_id="s1",
        name="S1",
        track_type="subtitle",
        index=0,
        subtitle_clips=(subtitle_clip,),
    )
    vol_automation = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
    vol_automation.add_keyframe(
        Keyframe(frame=0, value=0.5, interpolation=InterpolationType.LINEAR)
    )
    vol_automation.add_keyframe(
        Keyframe(frame=48, value=1.9, interpolation=InterpolationType.LINEAR)
    )
    tl = TimelineSequence(
        timeline_id="tl-16200",
        name="Rich Timeline",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        sample_rate=48000,
        format_settings={"custom_flag": 7},
        markers=(),
        range_start=3,
        range_end=99,
        master_volume=1.75,
        master_muted=True,
        master_automation={"volume": vol_automation},
        master_automation_mode="read",
        caption_graphics_extra={"style": "bold"},
        revision=41,
    )
    tl = tl.with_track_added(v1)
    tl = tl.with_track_added(a1)
    tl = tl.with_track_updated(a1.with_clip_added(blocker))
    tl = tl.with_track_added(s1)
    return tl


def _linked_pair(start: int, duration: int) -> tuple[ClipInstance, ClipInstance]:
    video_clip = ClipInstance(
        clip_id="v_new",
        asset_id="asset_av",
        track_id="v1",
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
        speed=1.0,
        linked_clip_id="a_new",
    )
    audio_clip = ClipInstance(
        clip_id="a_new",
        asset_id="asset_av",
        track_id="a1",
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
        speed=1.0,
        linked_clip_id="v_new",
    )
    return video_clip, audio_clip


class TestInsertLinkedAssetUndoPreservesFields:
    """AUD-16200: undo must return the exact pre-apply timeline."""

    def test_undo_after_auto_track_creation_preserves_every_field(self) -> None:
        original = _rich_timeline()
        video_clip, audio_clip = _linked_pair(start=10, duration=50)

        cmd = InsertLinkedAssetCommand.create(
            video_track_id="v1",
            video_clip=video_clip,
            audio_track_id="a1",  # overlaps blocker at [0,200) -> auto A2
            audio_clip=audio_clip,
        )
        stack = CommandStack()
        applied = stack.execute(cmd, original)

        # Sanity: apply really did auto-create an audio track (the branch
        # whose undo used to rebuild the timeline via a manual constructor).
        assert len(applied.audio_tracks) == 2, (
            "precondition: overlap must force auto-created audio track"
        )
        assert applied.get_track_by_id("v1").get_clip_by_id("v_new") is not None
        assert applied.get_clip_by_id("a_new") is not None

        undone = stack.undo(applied)
        assert undone is not None

        # Auto-created empty track is cleaned up, original track survives.
        assert len(undone.audio_tracks) == 1
        assert undone.get_track_by_id("a1") is not None
        assert undone.get_track_by_id("a1").get_clip_by_id("blocker_audio") is not None
        assert undone.get_clip_by_id("v_new") is None
        assert undone.get_clip_by_id("a_new") is None

        # Dynamic guard: every declared field of the *current*
        # TimelineSequence definition must round-trip apply+undo unchanged.
        # A future field dropped by a regressed manual rebuild fails here.
        for field in dataclasses.fields(TimelineSequence):
            assert getattr(undone, field.name) == getattr(original, field.name), (
                f"undo dropped TimelineSequence.{field.name}"
            )

    def test_undo_preserves_serialized_snapshot(self) -> None:
        original = _rich_timeline()
        video_clip, audio_clip = _linked_pair(start=20, duration=30)
        cmd = InsertLinkedAssetCommand.create(
            video_track_id="v1",
            video_clip=video_clip,
            audio_track_id="a1",
            audio_clip=audio_clip,
        )
        stack = CommandStack()
        applied = stack.execute(cmd, original)
        undone = stack.undo(applied)

        assert undone is not None
        # Serialization snapshot: any field that to_dict round-trips must
        # be identical after undo (subtitle track, master bus, format, ...).
        assert undone.to_dict() == original.to_dict()

    def test_undo_without_auto_track_also_preserves_fields(self) -> None:
        """Plain path (no auto track) must be equally loss-less."""
        original = _rich_timeline()
        video_clip, audio_clip = _linked_pair(start=300, duration=50)
        cmd = InsertLinkedAssetCommand.create(
            video_track_id="v1",
            video_clip=video_clip,
            audio_track_id="a1",  # [300,350) vs blocker [0,200): no overlap
            audio_clip=audio_clip,
        )
        stack = CommandStack()
        applied = stack.execute(cmd, original)
        assert len(applied.audio_tracks) == 1
        undone = stack.undo(applied)

        assert undone is not None
        for field in dataclasses.fields(TimelineSequence):
            assert getattr(undone, field.name) == getattr(original, field.name), (
                f"undo dropped TimelineSequence.{field.name}"
            )


# ================================================================
# AUD-2701 + AUD-2702: generator colour/params handlers
# ================================================================

def _solid_project() -> EditProject:
    asset = MediaAsset(
        asset_id="solid-1",
        path="",
        media_type="solid",
        display_name="Solid Colour",
        metadata={"color": [0.1, 0.2, 0.3], "generator_params": {}},
        keywords=("intro", "bg"),
        rating=4,
        flag="reject",
        color_label="orange",
        comment="keep my log fields",
        reel="R1",
        scene="sc42",
        shot="sh7",
        take="tk3",
    )
    smart_bins = {
        "sb-rated": SmartBin(
            bin_id="sb-rated",
            name="Rated 4+",
            filter_type="custom",
            filter_params={"min_rating": 4},
        ),
    }
    return EditProject(
        project_id="proj-2701",
        name="Generator Project",
        assets={"solid-1": asset},
        smart_bins=smart_bins,
        revision=17,
    )


def _make_controller(project: EditProject) -> EditControllerMixin:
    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller._current_edit_project = project
    controller._request_program_viewer_update = lambda: None
    # No timeline_panel attribute on the bare instance: the handler's
    # hasattr-guarded propagation is skipped, which is all we need here.
    controller.edit_properties_panel = SimpleNamespace(
        _current_generator_params={}
    )
    return controller


def _assert_project_fields_preserved(
    old: EditProject, new: EditProject
) -> None:
    """Dynamic guard: only assets/modified_at may differ between builds."""
    for field in dataclasses.fields(EditProject):
        if field.name in ("assets", "modified_at"):
            continue
        assert getattr(new, field.name) == getattr(old, field.name), (
            f"project rebuild dropped EditProject.{field.name}"
        )


def _assert_asset_fields_preserved(old: MediaAsset, new: MediaAsset) -> None:
    """Dynamic guard: only metadata may differ between asset builds."""
    for field in dataclasses.fields(MediaAsset):
        if field.name == "metadata":
            continue
        assert getattr(new, field.name) == getattr(old, field.name), (
            f"asset rebuild dropped MediaAsset.{field.name}"
        )


class TestGeneratorColorChanged:
    """AUD-2701 + AUD-2702 for _on_edit_generator_color_changed."""

    def test_color_change_keeps_smart_bins_revision_and_log_fields(self) -> None:
        project = _solid_project()
        controller = _make_controller(project)

        controller._on_edit_generator_color_changed(
            "clip-1", "solid-1", (0.9, 0.8, 0.7)
        )

        new_project = controller._current_edit_project
        assert new_project is not project

        _assert_project_fields_preserved(project, new_project)
        # Explicit spot checks (these were the concrete losses):
        assert new_project.smart_bins == project.smart_bins
        assert new_project.smart_bins["sb-rated"].name == "Rated 4+"
        assert new_project.revision == 17

        new_asset = new_project.assets["solid-1"]
        _assert_asset_fields_preserved(project.assets["solid-1"], new_asset)
        assert new_asset.keywords == ("intro", "bg")
        assert new_asset.rating == 4
        assert new_asset.flag == "reject"
        assert new_asset.color_label == "orange"
        assert new_asset.comment == "keep my log fields"
        # And the intended edit did land:
        assert new_asset.metadata["color"] == [0.9, 0.8, 0.7]
        # The rest of the metadata survives too.
        assert "generator_params" in new_asset.metadata


class TestGeneratorParamsChanged:
    """AUD-2701 + AUD-2702 for _on_edit_generator_params_changed."""

    def test_params_change_keeps_smart_bins_revision_and_log_fields(self) -> None:
        project = _solid_project()
        controller = _make_controller(project)

        params = {
            "mode": "gradient",
            "color_r": 0.25,
            "color_g": 0.5,
            "color_b": 0.75,
        }
        controller._on_edit_generator_params_changed(
            "clip-1", "solid-1", params
        )

        new_project = controller._current_edit_project
        assert new_project is not project

        _assert_project_fields_preserved(project, new_project)
        assert new_project.smart_bins == project.smart_bins
        assert new_project.revision == 17

        new_asset = new_project.assets["solid-1"]
        _assert_asset_fields_preserved(project.assets["solid-1"], new_asset)
        assert new_asset.keywords == ("intro", "bg")
        assert new_asset.rating == 4
        assert new_asset.flag == "reject"
        assert new_asset.color_label == "orange"
        assert new_asset.comment == "keep my log fields"

        assert new_asset.metadata["generator_params"] == params
        assert new_asset.metadata["color"] == [0.25, 0.5, 0.75]
        # Panel cache is refreshed from the submitted params.
        assert controller.edit_properties_panel._current_generator_params == params

    def test_params_change_does_not_share_metadata_object(self) -> None:
        """The new asset must not alias the old metadata dict."""
        project = _solid_project()
        old_asset = project.assets["solid-1"]
        controller = _make_controller(project)

        controller._on_edit_generator_params_changed(
            "clip-1", "solid-1", {"color_r": 0.0, "color_g": 0.0, "color_b": 1.0}
        )

        new_asset = controller._current_edit_project.assets["solid-1"]
        assert new_asset.metadata is not old_asset.metadata
        assert old_asset.metadata["color"] == [0.1, 0.2, 0.3]
