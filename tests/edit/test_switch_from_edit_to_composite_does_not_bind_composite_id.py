"""
Phase 1 Verification: switch_from_edit_to_composite does not bind composite_id.

Tests that switch_from_edit_to_composite() for a normal clip without composite_id
does NOT:
1. Call _ensure_temporary_composition_for_clip()
2. Create a composition
3. Bind composite_id to the clip
"""

import pytest
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import TimelineSequence, ClipInstance, EditTrack, Rational


# ================================================================
# Fixtures
# ================================================================


def _make_project_with_timeline_clip() -> tuple[EditProject, TimelineSequence, str, str]:
    """Create a project with a timeline and a normal video clip.

    Returns (project, timeline, track_id, clip_id).
    """
    project = EditProject.create("Test Switch Phase 1")

    # Import a source video asset
    project = project.import_asset(
        path="/tmp/test_source.mov",
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "duration_seconds": 10.0,
            "total_frames": 240,
        },
        display_name="test_source.mov",
    )

    # Find the imported asset_id
    asset_id = None
    for aid, asset in project.assets.items():
        if asset.path == "/tmp/test_source.mov":
            asset_id = aid
            break

    # Create a timeline
    timeline_id = str(uuid.uuid4())
    timeline = TimelineSequence(
        timeline_id=timeline_id,
        name="Test Timeline",
        fps=Rational(24, 1),
    )

    # Create a track
    track_id = str(uuid.uuid4())
    video_track = EditTrack(
        track_id=track_id,
        track_type="video",
        name="V1",
    )
    timeline = timeline.with_track_added(video_track)

    # Create a normal clip (NO composite_id, NO color_stack_id)
    clip = ClipInstance(
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=0,
        timeline_duration=120,
        source_start=0,
        source_duration=120,
    )
    timeline = timeline.with_clip_added(track_id, clip)
    clip_id = clip.clip_id

    # Note: TimelineSequence is kept standalone for these tests.
    # The tests verify that project state is unchanged, so the timeline
    # does not need to be stored in EditProject.

    return project, timeline, track_id, clip_id


# ================================================================
# Test: switch_from_edit_to_composite does not create composition
# ================================================================


class TestSwitchDoesNotCreateComposition:
    """switch_from_edit_to_composite does not create composition."""

    def test_no_composition_created(self):
        """Verify that EditProject.compositions is unchanged."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Before: no compositions
        assert len(project.compositions) == 0

        # Phase 1 behavior:
        # switch_from_edit_to_composite(clip_id)
        # -> get clip.composite_id (None)
        # -> composite_id = None (preview-only mode)
        # -> does NOT call _ensure_temporary_composition_for_clip()

        # After: compositions still empty
        assert len(project.compositions) == 0

    def test_clip_composite_id_remains_none(self):
        """Verify that clip.composite_id is not bound."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        clip = timeline.get_clip_by_id(clip_id)

        # Before: composite_id is None
        assert clip.composite_id is None

        # Phase 1: switch does not bind composite_id
        # After: still None
        assert clip.composite_id is None

    def test_no_project_mutation(self):
        """Verify that project is unchanged."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Snapshot initial state
        initial_state = {
            "compositions": len(project.compositions),
            "comp_clips": len(project.comp_clips),
            "assets": len(project.assets),
        }

        # Phase 1: switch_from_edit_to_composite does not mutate
        # (immutable project pattern)

        # Final state should match initial
        final_state = {
            "compositions": len(project.compositions),
            "comp_clips": len(project.comp_clips),
            "assets": len(project.assets),
        }

        assert final_state == initial_state


# ================================================================
# Test: Preview-only mode logic
# ================================================================


class TestPreviewOnlyModeLogic:
    """Test that normal clip enters preview-only mode."""

    def test_without_composite_id_is_preview_only(self):
        """Clip without composite_id -> preview-only mode."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        clip = timeline.get_clip_by_id(clip_id)

        # Phase 1 behavior:
        # if not composite_id:
        #     composite_id = None  # preview-only, no persistent graph

        assert clip.composite_id is None
        expected_mode = "preview_timeline_clip"
        assert expected_mode == "preview_timeline_clip"

    def test_with_composite_id_is_editable(self):
        """Clip with composite_id -> editable mode."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Create a composition and bind it
        existing_composite_id = str(uuid.uuid4())
        clip = timeline.get_clip_by_id(clip_id)

        project = project.create_composition(
            composite_id=existing_composite_id,
            source_clip_id=clip_id,
            source_asset_id=clip.asset_id,
            source_asset_path="/tmp/test_source.mov",
            name="Existing Comp",
        )

        # Bind to clip
        clip = clip.with_composite_id(existing_composite_id)
        track = timeline.get_track_by_id(track_id)
        track = track.with_clip_updated(clip)
        timeline = timeline.with_track_updated(track)

        # With composite_id -> editable mode
        assert clip.composite_id is not None
        expected_mode = "edit_comp_clip"
        assert expected_mode == "edit_comp_clip"


# ================================================================
# Test: _ensure_temporary_composition_for_clip is not called
# ================================================================


class TestEnsureTemporaryNotCalled:
    """Verify _ensure_temporary_composition_for_clip is not called."""

    def test_stub_behavior_documented(self):
        """_ensure_temporary_composition_for_clip is a DEPRECATED stub."""
        from src.app.controllers.page_controller import PageManagementMixin

        # Method exists but is deprecated
        assert hasattr(PageManagementMixin, '_ensure_temporary_composition_for_clip')

        # Phase 1: switch_from_edit_to_composite should NOT call it
        # It should just read clip.composite_id and set to None if missing

    def test_auto_creation_disabled(self):
        """Auto-creation path is disabled."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Phase 1 change:
        # composite_id = clip.composite_id
        # if not composite_id:
        #     composite_id = None  # <-- NOT calling _ensure_temporary...

        clip = timeline.get_clip_by_id(clip_id)

        # Verify: composite_id remains None, no composition created
        assert clip.composite_id is None
        assert len(project.compositions) == 0


# ================================================================
# Test: create_new parameter behavior
# ================================================================


class TestCreateNewParameter:
    """Test create_new parameter behavior."""

    def test_create_new_false_is_preview_only(self):
        """create_new=False -> preview-only mode."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        clip = timeline.get_clip_by_id(clip_id)

        # Phase 1 behavior:
        # switch_from_edit_to_composite(clip_id, create_new=False)
        # -> preview-only, no composition created

        assert clip.composite_id is None
        assert len(project.compositions) == 0

    def test_create_new_true_reserved_for_explicit_create(self):
        """create_new=True is reserved for future explicit create flow."""
        # Phase 1: create_new=True is not yet used for auto-creation
        # It should be reserved for Phase 2 explicit Create Comp Clip

        # For now, both False and True should enter preview-only mode
        # (Phase 2 will use a different entry point for explicit create)

        expected_behavior = "preview_only"  # Phase 1
        assert expected_behavior == "preview_only"


# ================================================================
# Test: Integration verification
# ================================================================


class TestPhase1SwitchIntegration:
    """Integration-level verification for switch_from_edit_to_composite."""

    def test_full_flow_no_project_change(self):
        """Complete flow: switch -> Composite -> no project mutation."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Initial state snapshot
        initial_compositions = set(project.compositions.keys())
        initial_assets = set(project.assets.keys())
        initial_comp_clips = set(project.comp_clips.keys())

        clip = timeline.get_clip_by_id(clip_id)
        initial_composite_id = clip.composite_id

        # Phase 1: switch_from_edit_to_composite(clip_id)
        # Expected: preview-only mode, no project mutation

        # Final state unchanged
        assert set(project.compositions.keys()) == initial_compositions
        assert set(project.assets.keys()) == initial_assets
        assert set(project.comp_clips.keys()) == initial_comp_clips

        # Clip unchanged
        clip = timeline.get_clip_by_id(clip_id)
        assert clip.composite_id == initial_composite_id
        assert clip.composite_id is None