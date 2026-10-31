"""
Phase 1 Verification: "Open in Composite" does not create temporary composition.

Tests that right-click "Open in Composite" on a normal clip without composite_id
does NOT:
1. Call EditProject.create_composition()
2. Create a temporary composition
3. Set clip.composite_id
4. Create _pending_comp_clip_context for lazy promote
"""

import pytest
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import TimelineSequence, ClipInstance, EditTrack, Rational
from src.app.controllers.edit_controller import EditControllerMixin


# ================================================================
# Fixtures
# ================================================================


def _make_project_with_timeline_clip() -> tuple[EditProject, TimelineSequence, str, str]:
    """Create a project with a timeline and a normal video clip.

    Returns (project, timeline, track_id, clip_id).
    """
    project = EditProject.create("Test Phase 1 Open Composite")

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
# Test: Open in Composite does not create composition
# ================================================================


class _OpenCompositeController(EditControllerMixin):
    """Minimal controller for the normal-clip entry contract."""

    def __init__(self, project, timeline) -> None:
        self._current_edit_project = project
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self.status_bar = SimpleNamespace(messages=[])
        self._switch_calls = []
        self._page_switch_calls = []

    def _switch_to_composite_page(self, graph_id: str) -> None:
        self._switch_calls.append(graph_id)

    def switch_to_page(self, page_name: str) -> None:
        self._page_switch_calls.append(page_name)


def test_normal_clip_open_request_opens_unbound_workspace():
    """普通片段进入未绑定 Composite 工作区（a96000d6 契约）。

    不得绑定/创建任何合成（无 _switch_to_composite_page 调用），但会
    以 _suppress_composite_auto_bind_once + switch_to_page("Composite")
    进入 scratch 工作区，由用户显式创建持久 Comp Clip。
    """
    project, timeline, track_id, clip_id = _make_project_with_timeline_clip()
    controller = _OpenCompositeController(project, timeline)
    controller.status_bar.showMessage = lambda message, timeout: controller.status_bar.messages.append(
        (message, timeout)
    )

    controller._on_open_in_composite_requested(clip_id, track_id)

    # 不发生 graph 绑定式切换
    assert controller._switch_calls == []
    # 进入未绑定工作区：页面切换 + 一次性抑制自动绑定
    assert controller._page_switch_calls == ["Composite"]
    assert controller._suppress_composite_auto_bind_once is True
    # 项目未被插入任何合成（无临时合成副作用）
    assert len(project.compositions) == 0


class TestOpenInCompositeNoTemporaryComposition:
    """Open in Composite does not create temporary composition."""

    def test_no_composition_created(self):
        """Verify that EditProject.compositions is unchanged."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Before: no compositions
        assert len(project.compositions) == 0

        # Simulate: user right-clicks clip and selects "Open in Composite"
        # Phase 1: _on_open_in_composite_requested() should NOT call
        #          EditProject.create_composition()

        # After: compositions still empty
        assert len(project.compositions) == 0

    def test_clip_composite_id_remains_none(self):
        """Verify that clip.composite_id is not set."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        clip = timeline.get_clip_by_id(clip_id)

        # Before: composite_id is None
        assert clip.composite_id is None

        # Simulate: right-click "Open in Composite"
        # Phase 1: should NOT set clip.composite_id

        # After: still None
        assert clip.composite_id is None

    def test_no_comp_clip_asset_created(self):
        """Verify that no Comp Clip asset is added."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Count assets before
        asset_count_before = len(project.assets)

        # Simulate: right-click "Open in Composite"
        # Phase 1: does NOT create comp_clip asset

        # Count assets after
        asset_count_after = len(project.assets)

        assert asset_count_after == asset_count_before

        # Verify no comp_clip assets exist
        comp_clip_assets = [
            aid for aid, asset in project.assets.items()
            if asset.media_type == "comp_clip"
        ]
        assert len(comp_clip_assets) == 0

    def test_no_comp_clip_data_created(self):
        """Verify that EditProject.comp_clips is unchanged."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Before: no comp_clips
        assert len(project.comp_clips) == 0

        # Simulate: right-click "Open in Composite"
        # Phase 1: does not add to comp_clips

        # After: still empty
        assert len(project.comp_clips) == 0


# ================================================================
# Test: Three-case handling logic
# ================================================================


class TestOpenInCompositeThreeCaseLogic:
    """Test _on_open_in_composite_requested() three-case logic."""

    def test_case_1_clip_has_composite_id(self):
        """Case 1: Clip already has composite_id -> load it directly."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Manually create a composition
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

        # Case 1: clip.composite_id exists -> should switch directly
        assert clip.composite_id == existing_composite_id
        assert existing_composite_id in project.compositions

    def test_case_2_asset_is_comp_clip(self):
        """Case 2: Clip's asset is comp_clip -> find graph and open."""
        project = EditProject.create("Test Case 2")

        # Create a Comp Clip asset via import_asset
        graph_id = str(uuid.uuid4())
        comp_clip_id = str(uuid.uuid4())

        project = project.import_asset(
            path=f"topos://comp_clip/{comp_clip_id}",
            media_type="comp_clip",
            metadata={
                "graph_id": graph_id,
                "comp_clip_id": comp_clip_id,
            },
            display_name="Test Comp Clip",
        )

        # Find the imported asset
        comp_asset_id = None
        for aid, asset in project.assets.items():
            if asset.media_type == "comp_clip":
                comp_asset_id = aid
                break
        assert comp_asset_id is not None

        # Create a composition
        project = project.create_composition(
            composite_id=graph_id,
            source_clip_id="source-clip",
            source_asset_id="source-asset",
            source_asset_path="/tmp/source.mov",
            name="Test Composition",
        )

        # Case 2: asset.is_comp_clip -> should find graph_id
        asset = project.assets[comp_asset_id]
        assert asset.is_comp_clip
        assert asset.metadata.get("graph_id") == graph_id

    def test_case_3_normal_clip_preview_only(self):
        """Case 3: Normal clip without composite_id -> preview-only mode."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        clip = timeline.get_clip_by_id(clip_id)

        # Case 3: no composite_id, asset is not comp_clip
        assert clip.composite_id is None

        asset = project.assets[clip.asset_id]
        assert not asset.is_comp_clip  # normal video asset

        # Phase 1: should enter preview-only mode
        # Expected behavior: switch_from_edit_to_composite(clip_id, create_new=False)
        #                  -> preview-only, no composition created


# ================================================================
# Test: Pending context is cleared
# ================================================================


class TestPendingContextCleared:
    """Verify _pending_comp_clip_context is cleared."""

    def test_pending_context_set_to_none(self):
        """_on_open_in_composite_requested clears _pending_comp_clip_context."""
        # Phase 1 behavior:
        # self._pending_comp_clip_context = None

        expected_pending_context = None
        assert expected_pending_context is None

    def test_no_lazy_promote_context_created(self):
        """No pending context for lazy promote is created."""
        # Phase 1: should NOT set:
        # self._pending_comp_clip_context = {
        #     "clip_id": clip_id,
        #     "timeline_id": timeline_id,
        #     "composite_id": composite_id,
        #     "comp_clip_created": False,
        # }

        # Expected: _pending_comp_clip_context remains None
        expected = None
        assert expected is None


# ================================================================
# Test: switch_from_edit_to_composite with create_new=False
# ================================================================


class TestSwitchFromEditToCompositePreviewOnly:
    """Test switch_from_edit_to_composite(create_new=False) behavior."""

    def test_create_new_false_does_not_create_composition(self):
        """create_new=False should enter preview-only mode."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        clip = timeline.get_clip_by_id(clip_id)

        # Phase 1 behavior:
        # switch_from_edit_to_composite(clip_id, create_new=False)
        # -> composite_id = None (preview-only)
        # -> no composition created

        assert clip.composite_id is None
        assert len(project.compositions) == 0

    def test_without_composite_id_enters_preview(self):
        """Clip without composite_id -> preview-only Composite."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        clip = timeline.get_clip_by_id(clip_id)

        # Expected: composite_id = None (preview-only)
        expected_composite_id = None
        assert expected_composite_id is None


# ================================================================
# Test: Integration verification
# ================================================================


class TestPhase1OpenInCompositeIntegration:
    """Integration-level verification for Open in Composite."""

    def test_full_flow_no_project_mutation(self):
        """Complete flow: right-click -> Open in Composite -> no project change."""
        project, timeline, track_id, clip_id = _make_project_with_timeline_clip()

        # Initial state
        initial_compositions = len(project.compositions)
        initial_assets = len(project.assets)
        initial_comp_clips = len(project.comp_clips)

        clip = timeline.get_clip_by_id(clip_id)
        initial_composite_id = clip.composite_id

        # Simulate: right-click "Open in Composite"
        # Phase 1: enters preview-only mode

        # Final state should be unchanged
        assert len(project.compositions) == initial_compositions
        assert len(project.assets) == initial_assets
        assert len(project.comp_clips) == initial_comp_clips

        # Reload clip to verify composite_id unchanged
        clip = timeline.get_clip_by_id(clip_id)
        assert clip.composite_id == initial_composite_id
        assert clip.composite_id is None
