"""
Tests for scene_cut.subclips — Add Cuts to Media Pool workflow.

Validates:
- create_subclip_asset: SceneSubclipSpec -> MediaAsset conversion
- find_parent_bin: Bin location logic
- check_existing_subclips: Duplicate detection
- add_subclips_to_project: Main workflow (append/replace/cancel)
- Helper functions: is_subclip, get_source_range, etc.
- EditProject subclip methods: import_subclip, with_subclips_added

Reference:
- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md P6
"""

from __future__ import annotations

import pytest
import uuid
import time

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    ProjectBin,
)
from src.features.edit.core.scene_cut.models import (
    SceneSubclipSpec,
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutKind,
    SceneCutDecision,
    SceneCutSettings,
    generate_analysis_id,
)
from src.features.edit.core.scene_cut.subclips import (
    DUPLICATE_REPLACE,
    DUPLICATE_APPEND,
    DUPLICATE_CANCEL,
    SUBCLIP_IS_SUBCLIP,
    SUBCLIP_PARENT_ASSET_ID,
    SUBCLIP_SOURCE_START,
    SUBCLIP_SOURCE_DURATION,
    SUBCLIP_ANALYSIS_ID,
    ExistingSubclipReport,
    AddSubclipsResult,
    create_subclip_asset,
    find_parent_bin,
    check_existing_subclips,
    remove_existing_subclips,
    add_subclips_to_bin,
    add_subclips_to_project,
    is_subclip,
    get_parent_asset_id,
    get_subclip_source_range,
    get_subclips_for_analysis,
    remove_subclips_for_analysis,
    get_scene_index,
    validate_subclip_for_timeline,
)


# ===================================================================
# Test Fixtures
# ===================================================================

def _make_parent_asset(
    asset_id: str = "parent-001",
    path: str = "/path/to/media.mp4",
    display_name: str = "media.mp4",
) -> MediaAsset:
    """Create a sample parent video asset."""
    return MediaAsset(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=display_name,
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "duration_seconds": 41.667,
            "total_frames": 1000,
            "codec_name": "h264",
        },
        proxy_status="none",
        created_at=time.time(),
    )


def _make_subclip_spec(
    parent_asset_id: str = "parent-001",
    source_path: str = "/path/to/media.mp4",
    scene_index: int = 1,
    source_start: int = 0,
    source_duration: int = 100,
    display_name: str = "media.mp4 Scene 001",
    confidence: float = 0.90,
) -> SceneSubclipSpec:
    """Create a sample subclip specification."""
    return SceneSubclipSpec(
        parent_asset_id=parent_asset_id,
        source_path=source_path,
        scene_index=scene_index,
        source_start=source_start,
        source_duration=source_duration,
        start_seconds=source_start / 24.0,
        duration_seconds=source_duration / 24.0,
        display_name=display_name,
        start_timecode="00:00:00:00",
        end_timecode="00:00:04:04",
        thumbnail_source_frame=source_start,
        detection_confidence=confidence,
        detection_kind=SceneCutKind.HARD_CUT,
    )


def _make_analysis(
    asset_id: str = "parent-001",
    source_path: str = "/path/to/media.mp4",
    candidates_count: int = 3,
) -> SceneCutAnalysis:
    """Create a sample scene cut analysis with accepted candidates."""
    analysis_id = generate_analysis_id(asset_id)
    candidates = []

    # Create accepted candidates at frame 100, 260, 700
    for frame in [100, 260, 700]:
        candidates.append(
            SceneCutCandidate(
                source_frame=frame,
                pts=frame * 1000,
                seconds=frame / 24.0,
                score=0.85,
                confidence=0.90,
                kind=SceneCutKind.HARD_CUT,
                decision=SceneCutDecision.ACCEPTED,
            )
        )

    return SceneCutAnalysis(
        analysis_id=analysis_id,
        asset_id=asset_id,
        source_path=source_path,
        clip_id=None,
        settings=SceneCutSettings(),
        media_fingerprint="fp-test-001",
        candidates=tuple(candidates),
        created_at=time.time(),
    )


def _make_project_with_parent(
    parent_asset: MediaAsset,
) -> tuple[EditProject, MediaAsset]:
    """
    Create a project with a parent asset.

    Returns tuple of (project, actual_parent_asset) because import_asset
    generates a new UUID, not the one we specify.
    """
    project = EditProject.create("Test Project")
    project = project.import_asset(
        path=parent_asset.path,
        media_type=parent_asset.media_type,
        metadata=parent_asset.metadata,
        display_name=parent_asset.display_name,
    )
    # Get the actual imported asset (with generated UUID)
    actual_parent = project.get_asset_by_path(parent_asset.path)
    return project, actual_parent


# ===================================================================
# Test: create_subclip_asset
# ===================================================================

class TestCreateSubclipAsset:
    """Tests for create_subclip_asset function."""

    def test_creates_media_asset_with_new_uuid(self):
        """Subclip should have new unique asset_id."""
        parent = _make_parent_asset()
        spec = _make_subclip_spec(parent_asset_id=parent.asset_id)
        analysis_id = "analysis-test-001"

        subclip = create_subclip_asset(spec, analysis_id, parent)

        assert subclip.asset_id != parent.asset_id
        assert isinstance(subclip.asset_id, str)
        assert len(subclip.asset_id) == 36  # UUID format

    def test_subclip_has_correct_metadata(self):
        """Subclip metadata should contain subclip-specific fields."""
        parent = _make_parent_asset()
        spec = _make_subclip_spec(
            parent_asset_id=parent.asset_id,
            source_start=100,
            source_duration=200,
            scene_index=2,
        )
        analysis_id = "analysis-test-002"

        subclip = create_subclip_asset(spec, analysis_id, parent)

        assert subclip.metadata.get(SUBCLIP_IS_SUBCLIP) is True
        assert subclip.metadata.get(SUBCLIP_PARENT_ASSET_ID) == parent.asset_id
        assert subclip.metadata.get(SUBCLIP_SOURCE_START) == 100
        assert subclip.metadata.get(SUBCLIP_SOURCE_DURATION) == 200
        assert subclip.metadata.get(SUBCLIP_ANALYSIS_ID) == analysis_id
        assert subclip.metadata.get("scene_index") == 2

    def test_subclip_uses_parent_path(self):
        """Subclip should reference parent file path, not create new file."""
        parent = _make_parent_asset(path="/original/path/video.mp4")
        spec = _make_subclip_spec(source_path="/original/path/video.mp4")

        subclip = create_subclip_asset(spec, "analysis-001", parent)

        assert subclip.path == parent.path

    def test_subclip_inherits_parent_proxy_info(self):
        """Subclip should inherit proxy_path and proxy_status."""
        parent = _make_parent_asset()
        parent_with_proxy = parent.with_proxy_status("ready").with_proxy_path("/proxy/path.mp4")
        spec = _make_subclip_spec()

        subclip = create_subclip_asset(spec, "analysis-001", parent_with_proxy)

        assert subclip.proxy_status == "ready"
        assert subclip.proxy_path == "/proxy/path.mp4"

    def test_subclip_has_display_name_from_spec(self):
        """Subclip display_name should match spec.display_name."""
        parent = _make_parent_asset(display_name="my_clip.mov")
        spec = _make_subclip_spec(display_name="my_clip.mov Scene 003")

        subclip = create_subclip_asset(spec, "analysis-001", parent)

        assert subclip.display_name == "my_clip.mov Scene 003"

    def test_subclip_media_type_is_video(self):
        """Subclip should always be video type (per plan section 4.1)."""
        parent = _make_parent_asset()
        spec = _make_subclip_spec()

        subclip = create_subclip_asset(spec, "analysis-001", parent)

        assert subclip.media_type == "video"

    def test_subclip_duration_metadata_matches_spec(self):
        """Subclip duration metadata should reflect source range."""
        parent = _make_parent_asset()
        spec = _make_subclip_spec(source_duration=150)

        subclip = create_subclip_asset(spec, "analysis-001", parent)

        assert subclip.metadata.get("duration_seconds") == spec.duration_seconds
        assert subclip.metadata.get("total_frames") == 150


# ===================================================================
# Test: find_parent_bin
# ===================================================================

class TestFindParentBin:
    """Tests for find_parent_bin function."""

    def test_returns_target_bin_if_valid(self):
        """Should return target_bin_id if it exists in project."""
        parent = _make_parent_asset()
        project, actual_parent = _make_project_with_parent(parent)

        # Create a new bin
        project = project.create_bin("My Bin")
        my_bin_id = [bid for bid in project.bins if project.bins[bid].name == "My Bin"][0]

        result = find_parent_bin(project, actual_parent.asset_id, target_bin_id=my_bin_id)

        assert result == my_bin_id

    def test_returns_bin_containing_parent(self):
        """Should return bin that contains parent asset."""
        parent = _make_parent_asset()
        project, actual_parent = _make_project_with_parent(parent)

        result = find_parent_bin(project, actual_parent.asset_id)

        # Parent should be in root bin after import
        assert result == project.root_bin_id

    def test_returns_root_bin_as_fallback(self):
        """Should fallback to root bin if parent not found in any bin."""
        parent = _make_parent_asset()
        project = EditProject.create("Empty Project")

        result = find_parent_bin(project, parent.asset_id)

        # Should return root bin even though parent not in project
        assert result == project.root_bin_id

    def test_returns_none_if_no_bins(self):
        """Should return None if project has no bins."""
        project = EditProject(
            project_id=str(uuid.uuid4()),
            name="No Bins",
            assets={},
            bins={},
            timelines={},
            compositions={},
            color_stacks={},
            comp_clips={},
            render_cache_manifests={},
            render_cache_settings=None,
            root_bin_id="",
        )

        result = find_parent_bin(project, "any-asset-id")

        assert result is None


# ===================================================================
# Test: check_existing_subclips
# ===================================================================

class TestCheckExistingSubclips:
    """Tests for check_existing_subclips function."""

    def test_empty_project_has_no_existing(self):
        """Empty project should report no existing subclips."""
        project = EditProject.create("Empty")

        report = check_existing_subclips(project, "analysis-001", "parent-001")

        assert report.has_existing is False
        assert report.count == 0
        assert report.existing_subclip_ids == ()

    def project_with_existing_subclips(self):
        """Create project with existing subclips from same analysis."""
        parent = _make_parent_asset()
        project, actual_parent = _make_project_with_parent(parent)
        analysis_id = "analysis-existing-001"

        # Create and add existing subclip using actual_parent's asset_id
        spec = _make_subclip_spec(parent_asset_id=actual_parent.asset_id)
        existing_subclip = create_subclip_asset(spec, analysis_id, actual_parent)
        project = project.import_subclip(existing_subclip)

        return project, actual_parent, analysis_id, existing_subclip

    def test_detects_existing_from_same_analysis(self):
        """Should detect subclips from same analysis."""
        project, actual_parent, analysis_id, existing = self.project_with_existing_subclips()

        report = check_existing_subclips(project, analysis_id, actual_parent.asset_id)

        assert report.has_existing is True
        assert report.count == 1
        assert existing.asset_id in report.existing_subclip_ids

    def test_ignores_subclips_from_different_analysis(self):
        """Should not count subclips from different analysis."""
        project, actual_parent, analysis_id, existing = self.project_with_existing_subclips()

        # Check for different analysis
        report = check_existing_subclips(project, "different-analysis", actual_parent.asset_id)

        assert report.has_existing is False

    def test_ignores_subclips_from_different_parent(self):
        """Should not count subclips from different parent."""
        project, actual_parent, analysis_id, existing = self.project_with_existing_subclips()

        # Check for different parent
        report = check_existing_subclips(project, analysis_id, "different-parent")

        assert report.has_existing is False

    def test_includes_existing_names(self):
        """Report should include existing subclip names."""
        project, actual_parent, analysis_id, existing = self.project_with_existing_subclips()

        report = check_existing_subclips(project, analysis_id, actual_parent.asset_id)

        assert existing.display_name in report.existing_subclip_names


# ===================================================================
# Test: add_subclips_to_project (Main Workflow)
# ===================================================================

class TestAddSubclipsToProject:
    """Tests for main add_subclips_to_project workflow."""

    def test_append_strategy_adds_new_subclips(self):
        """Append strategy should add subclips without removing existing."""
        parent = _make_parent_asset()
        project, actual_parent = _make_project_with_parent(parent)

        # Add existing subclip first
        analysis1 = _make_analysis(asset_id=actual_parent.asset_id)
        spec1 = _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=1)
        existing = create_subclip_asset(spec1, analysis1.analysis_id, actual_parent)
        project = project.import_subclip(existing)

        # Now add new subclips from different analysis with append
        analysis2 = _make_analysis(asset_id=actual_parent.asset_id)
        spec2 = _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=2)
        spec3 = _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=3)
        specs = (spec2, spec3)

        result = add_subclips_to_project(
            project=project,
            analysis=analysis2,
            specs=specs,
            parent_asset=actual_parent,
            duplicate_strategy=DUPLICATE_APPEND,
        )

        assert result.project is not None
        assert result.added_count == 2
        assert result.removed_count == 0
        assert result.duplicate_strategy == DUPLICATE_APPEND
        # Should still have existing subclip
        assert existing.asset_id in result.project.assets

    def test_replace_strategy_removes_existing(self):
        """Replace strategy should remove existing then add new."""
        parent = _make_parent_asset()
        project, actual_parent = _make_project_with_parent(parent)
        analysis = _make_analysis(asset_id=actual_parent.asset_id)

        # Add existing subclip first
        spec1 = _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=1)
        existing = create_subclip_asset(spec1, analysis.analysis_id, actual_parent)
        project = project.import_subclip(existing)

        # Add new subclips with replace strategy
        spec2 = _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=2)
        spec3 = _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=3)
        specs = (spec2, spec3)

        result = add_subclips_to_project(
            project=project,
            analysis=analysis,
            specs=specs,
            parent_asset=actual_parent,
            duplicate_strategy=DUPLICATE_REPLACE,
        )

        assert result.added_count == 2
        assert result.removed_count == 1
        # Existing should be removed
        assert existing.asset_id not in result.project.assets

    def test_cancel_strategy_returns_unchanged(self):
        """Cancel strategy should return unchanged project."""
        parent = _make_parent_asset()
        project, actual_parent = _make_project_with_parent(parent)
        analysis = _make_analysis(asset_id=actual_parent.asset_id)

        # Add existing subclip
        spec1 = _make_subclip_spec(parent_asset_id=actual_parent.asset_id)
        existing = create_subclip_asset(spec1, analysis.analysis_id, actual_parent)
        project = project.import_subclip(existing)

        # Try to add with cancel strategy (should not add)
        spec2 = _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=2)
        specs = (spec2,)

        result = add_subclips_to_project(
            project=project,
            analysis=analysis,
            specs=specs,
            parent_asset=actual_parent,
            duplicate_strategy=DUPLICATE_CANCEL,
        )

        assert result.added_count == 0
        assert result.project.assets.keys() == project.assets.keys()
        assert len(result.warnings) > 0

    def test_adds_to_parent_bin(self):
        """Subclips should be grouped in a child bin next to the parent."""
        parent = _make_parent_asset()
        project, actual_parent = _make_project_with_parent(parent)
        analysis = _make_analysis(asset_id=actual_parent.asset_id)

        spec = _make_subclip_spec(parent_asset_id=actual_parent.asset_id)
        specs = (spec,)

        result = add_subclips_to_project(
            project=project,
            analysis=analysis,
            specs=specs,
            parent_asset=actual_parent,
        )

        # The generated folder should be a child of the parent's bin.
        parent_bin = project.find_bin_for_asset(actual_parent.asset_id)
        scene_cut_bin = result.project.get_bin_by_id(result.bin_id)
        assert scene_cut_bin is not None
        assert result.bin_id != parent_bin.bin_id
        assert scene_cut_bin.parent_id == parent_bin.bin_id
        assert scene_cut_bin.name == f"{actual_parent.display_name} - 场景剪切"
        assert result.added_assets[0].asset_id in scene_cut_bin.asset_ids

    def test_reuses_scene_cut_bin_for_same_parent(self):
        """Repeated detections for one source should reuse one folder."""
        parent = _make_parent_asset()
        project, actual_parent = _make_project_with_parent(parent)
        analysis1 = _make_analysis(asset_id=actual_parent.asset_id)
        analysis2 = _make_analysis(asset_id=actual_parent.asset_id)
        spec = _make_subclip_spec(parent_asset_id=actual_parent.asset_id)

        first = add_subclips_to_project(
            project=project,
            analysis=analysis1,
            specs=(spec,),
            parent_asset=actual_parent,
        )
        second = add_subclips_to_project(
            project=first.project,
            analysis=analysis2,
            specs=(spec,),
            parent_asset=actual_parent,
        )

        assert second.bin_id == first.bin_id
        scene_cut_bin = second.project.get_bin_by_id(second.bin_id)
        assert scene_cut_bin is not None
        assert len(scene_cut_bin.asset_ids) == 2

    def test_raises_for_missing_parent(self):
        """Should raise ValueError if parent not in project."""
        parent = _make_parent_asset(asset_id="missing-parent")
        project = EditProject.create("Empty Project")
        analysis = _make_analysis(asset_id="missing-parent")
        spec = _make_subclip_spec(parent_asset_id="missing-parent")

        with pytest.raises(ValueError, match="not found in project"):
            add_subclips_to_project(
                project=project,
                analysis=analysis,
                specs=(spec,),
                parent_asset=parent,
            )

    def test_adds_multiple_specs(self):
        """Should add all specs to project."""
        parent = _make_parent_asset()
        project, actual_parent = _make_project_with_parent(parent)
        analysis = _make_analysis(asset_id=actual_parent.asset_id)

        specs = (
            _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=1, source_start=0),
            _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=2, source_start=100),
            _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=3, source_start=260),
            _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=4, source_start=700),
        )

        result = add_subclips_to_project(
            project=project,
            analysis=analysis,
            specs=specs,
            parent_asset=actual_parent,
        )

        assert result.added_count == 4
        assert len(result.project.assets) == 1 + 4  # parent + 4 subclips


# ===================================================================
# Test: Helper Functions
# ===================================================================

class TestHelperFunctions:
    """Tests for helper functions."""

    def test_is_subclip_detects_subclip(self):
        """is_subclip should return True for subclip assets."""
        parent = _make_parent_asset()
        spec = _make_subclip_spec()
        subclip = create_subclip_asset(spec, "analysis-001", parent)

        assert is_subclip(subclip) is True
        assert is_subclip(parent) is False

    def test_get_parent_asset_id(self):
        """get_parent_asset_id should return parent ID."""
        parent = _make_parent_asset(asset_id="parent-abc")
        spec = _make_subclip_spec(parent_asset_id="parent-abc")
        subclip = create_subclip_asset(spec, "analysis-001", parent)

        result = get_parent_asset_id(subclip)

        assert result == "parent-abc"
        assert get_parent_asset_id(parent) is None

    def test_get_subclip_source_range(self):
        """get_subclip_source_range should return (start, duration)."""
        parent = _make_parent_asset()
        spec = _make_subclip_spec(source_start=100, source_duration=150)
        subclip = create_subclip_asset(spec, "analysis-001", parent)

        start, duration = get_subclip_source_range(subclip)

        assert start == 100
        assert duration == 150

    def test_get_subclip_source_range_raises_for_non_subclip(self):
        """Should raise ValueError for non-subclip."""
        parent = _make_parent_asset()

        with pytest.raises(ValueError, match="is not a subclip"):
            get_subclip_source_range(parent)

    def test_get_scene_index(self):
        """get_scene_index should return scene index."""
        parent = _make_parent_asset()
        spec = _make_subclip_spec(scene_index=5)
        subclip = create_subclip_asset(spec, "analysis-001", parent)

        assert get_scene_index(subclip) == 5
        assert get_scene_index(parent) == 0

    def test_validate_subclip_for_timeline(self):
        """validate_subclip_for_timeline should validate metadata."""
        parent = _make_parent_asset()
        spec = _make_subclip_spec()
        subclip = create_subclip_asset(spec, "analysis-001", parent)

        is_valid, error = validate_subclip_for_timeline(subclip)

        assert is_valid is True
        assert error == ""

    def test_validate_subclip_missing_field(self):
        """Should return error for missing required field."""
        # Create asset with incomplete metadata
        incomplete = MediaAsset(
            asset_id=str(uuid.uuid4()),
            path="/path/to/file.mp4",
            media_type="video",
            display_name="Incomplete",
            metadata={"is_subclip": True},  # Missing other fields
        )

        is_valid, error = validate_subclip_for_timeline(incomplete)

        assert is_valid is False
        assert "Missing required field" in error


# ===================================================================
# Test: EditProject Subclip Methods
# ===================================================================

class TestEditProjectSubclipMethods:
    """Tests for EditProject subclip management methods."""

    def test_import_subclip_adds_to_project(self):
        """import_subclip should add subclip to project."""
        project = EditProject.create("Test")
        parent = _make_parent_asset()
        project = project.import_asset(
            path=parent.path,
            media_type=parent.media_type,
            metadata=parent.metadata,
            display_name=parent.display_name,
        )
        actual_parent = project.get_asset_by_path(parent.path)

        spec = _make_subclip_spec(parent_asset_id=actual_parent.asset_id)
        subclip = create_subclip_asset(spec, "analysis-001", actual_parent)

        new_project = project.import_subclip(subclip)

        assert subclip.asset_id in new_project.assets
        assert is_subclip(new_project.assets[subclip.asset_id])

    def test_import_subclip_raises_for_non_subclip(self):
        """Should raise ValueError for non-subclip asset."""
        project = EditProject.create("Test")
        parent = _make_parent_asset()

        with pytest.raises(ValueError, match="is not a subclip"):
            project.import_subclip(parent)

    def test_with_subclips_added_batch_add(self):
        """with_subclips_added should add multiple subclips."""
        project = EditProject.create("Test")
        parent = _make_parent_asset()
        project = project.import_asset(
            path=parent.path,
            media_type=parent.media_type,
            metadata=parent.metadata,
            display_name=parent.display_name,
        )
        actual_parent = project.get_asset_by_path(parent.path)

        subclips = tuple(
            create_subclip_asset(
                _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=i),
                "analysis-001",
                actual_parent,
            )
            for i in range(1, 4)
        )

        new_project = project.with_subclips_added(subclips)

        assert len(new_project.assets) == 4  # parent + 3 subclips

    def test_get_subclips_for_parent(self):
        """get_subclips_for_parent should return subclips for given parent."""
        project = EditProject.create("Test")
        parent1 = _make_parent_asset(asset_id="parent-001", display_name="video1.mp4", path="/path/v1.mp4")
        parent2 = _make_parent_asset(asset_id="parent-002", display_name="video2.mp4", path="/path/v2.mp4")

        project = project.import_asset(
            path=parent1.path,
            media_type=parent1.media_type,
            metadata=parent1.metadata,
            display_name=parent1.display_name,
        )
        actual_parent1 = project.get_asset_by_path(parent1.path)
        project = project.import_asset(
            path=parent2.path,
            media_type=parent2.media_type,
            metadata=parent2.metadata,
            display_name=parent2.display_name,
        )
        actual_parent2 = project.get_asset_by_path(parent2.path)

        # Add subclips for parent1
        subclip1 = create_subclip_asset(
            _make_subclip_spec(parent_asset_id=actual_parent1.asset_id, source_path=parent1.path),
            "analysis-001",
            actual_parent1,
        )
        project = project.import_subclip(subclip1)

        # Add subclip for parent2
        subclip2 = create_subclip_asset(
            _make_subclip_spec(parent_asset_id=actual_parent2.asset_id, source_path=parent2.path),
            "analysis-002",
            actual_parent2,
        )
        project = project.import_subclip(subclip2)

        result = project.get_subclips_for_parent(actual_parent1.asset_id)

        assert len(result) == 1
        assert result[0].asset_id == subclip1.asset_id

    def test_get_subclips_for_analysis(self):
        """get_subclips_for_analysis should return subclips for given analysis."""
        project = EditProject.create("Test")
        parent = _make_parent_asset()
        project = project.import_asset(
            path=parent.path,
            media_type=parent.media_type,
            metadata=parent.metadata,
            display_name=parent.display_name,
        )
        actual_parent = project.get_asset_by_path(parent.path)

        # Add subclips from different analyses
        subclip1 = create_subclip_asset(
            _make_subclip_spec(parent_asset_id=actual_parent.asset_id),
            "analysis-A",
            actual_parent,
        )
        subclip2 = create_subclip_asset(
            _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=2),
            "analysis-B",
            actual_parent,
        )
        project = project.import_subclip(subclip1)
        project = project.import_subclip(subclip2)

        result = project.get_subclips_for_analysis("analysis-A")

        assert len(result) == 1
        assert result[0].asset_id == subclip1.asset_id

    def test_remove_subclips_for_analysis(self):
        """remove_subclips_for_analysis should remove all subclips from analysis."""
        project = EditProject.create("Test")
        parent = _make_parent_asset()
        project = project.import_asset(
            path=parent.path,
            media_type=parent.media_type,
            metadata=parent.metadata,
            display_name=parent.display_name,
        )
        actual_parent = project.get_asset_by_path(parent.path)

        # Add subclips
        subclip1 = create_subclip_asset(
            _make_subclip_spec(parent_asset_id=actual_parent.asset_id),
            "analysis-X",
            actual_parent,
        )
        subclip2 = create_subclip_asset(
            _make_subclip_spec(parent_asset_id=actual_parent.asset_id, scene_index=2),
            "analysis-X",
            actual_parent,
        )
        project = project.import_subclip(subclip1)
        project = project.import_subclip(subclip2)

        # Remove all from analysis-X
        new_project = project.remove_subclips_for_analysis("analysis-X")

        assert subclip1.asset_id not in new_project.assets
        assert subclip2.asset_id not in new_project.assets
        assert actual_parent.asset_id in new_project.assets  # Parent still exists

    def test_find_bin_for_asset(self):
        """find_bin_for_asset should return bin containing asset."""
        project = EditProject.create("Test")
        parent = _make_parent_asset()
        project = project.import_asset(
            path=parent.path,
            media_type=parent.media_type,
            metadata=parent.metadata,
            display_name=parent.display_name,
        )
        actual_parent = project.get_asset_by_path(parent.path)

        bin_result = project.find_bin_for_asset(actual_parent.asset_id)

        assert bin_result is not None
        assert actual_parent.asset_id in bin_result.asset_ids


# ===================================================================
# Test: Integration - Full Workflow
# ===================================================================

class TestFullWorkflow:
    """Integration tests for full Add Cuts to Media Pool workflow."""

    def test_full_workflow_no_existing(self):
        """Complete workflow with no existing subclips."""
        # Create project and import parent
        project = EditProject.create("My Project")
        parent = _make_parent_asset()
        project = project.import_asset(
            path=parent.path,
            media_type=parent.media_type,
            metadata=parent.metadata,
            display_name=parent.display_name,
        )
        actual_parent = project.get_asset_by_path(parent.path)

        # Run scene detection (simulated with pre-made analysis)
        analysis = _make_analysis(asset_id=actual_parent.asset_id)

        # Generate subclip specs from analysis
        from src.features.edit.core.scene_cut.models import generate_scene_subclips
        specs = generate_scene_subclips(
            analysis=analysis,
            total_source_frames=1000,
            total_duration_seconds=41.667,
            parent_display_name=actual_parent.display_name,
            fps=24.0,
        )

        # Add to media pool
        result = add_subclips_to_project(
            project=project,
            analysis=analysis,
            specs=specs,
            parent_asset=actual_parent,
            duplicate_strategy=DUPLICATE_APPEND,
        )

        # Verify results
        assert result.added_count == len(specs)
        assert result.duplicate_strategy == DUPLICATE_APPEND
        assert all(s.asset_id in result.project.assets for s in result.added_assets)

        # All subclips should be in the generated scene-cut bin.
        scene_cut_bin = result.project.get_bin_by_id(result.bin_id)
        assert scene_cut_bin is not None
        for asset in result.added_assets:
            assert asset.asset_id in scene_cut_bin.asset_ids

    def test_undo_workflow(self):
        """Undo: remove subclips for analysis."""
        project = EditProject.create("Undo Test")
        parent = _make_parent_asset()
        project = project.import_asset(
            path=parent.path,
            media_type=parent.media_type,
            metadata=parent.metadata,
            display_name=parent.display_name,
        )
        actual_parent = project.get_asset_by_path(parent.path)

        analysis = _make_analysis(asset_id=actual_parent.asset_id)
        spec = _make_subclip_spec(parent_asset_id=actual_parent.asset_id)
        specs = (spec,)

        # Add subclips
        result = add_subclips_to_project(
            project=project,
            analysis=analysis,
            specs=specs,
            parent_asset=actual_parent,
        )

        # Verify added
        assert result.added_count == 1

        # Undo: remove by analysis ID
        undo_project = result.project.remove_subclips_for_analysis(analysis.analysis_id)

        # Verify removed
        assert len(undo_project.assets) == 1  # Only parent remains
        assert actual_parent.asset_id in undo_project.assets
