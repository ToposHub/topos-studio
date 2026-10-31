"""
Test: Add Cuts to Media Pool integration.

SC-REM-16: End-to-end test for the "Add Cuts to Media Pool" workflow:
- Create SceneCutAnalysis with accepted candidates
- Call add_cuts_to_media_pool via controller
- Verify project.assets contains new subclips with correct metadata
"""

import pytest
import time
from unittest.mock import MagicMock

from src.features.edit.core.scene_cut_controller import (
    SceneCutController,
    DetectionContext,
)
from src.features.edit.core.scene_cut.models import (
    SceneCutSettings,
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutKind,
    SceneCutDecision,
    SceneCutMetrics,
    FrameTimePoint,
    generate_analysis_id,
)
from src.features.edit.core.scene_cut.subclips import (
    is_subclip,
    get_parent_asset_id,
)
from src.features.edit.core.project import EditProject, MediaAsset


# ================================================================
# Fixtures
# ================================================================

def _make_video_asset(
    asset_id: str = "video-001",
    total_frames: int = 2400,
    fps: float = 24.0,
) -> MediaAsset:
    """Create a video MediaAsset."""
    return MediaAsset(
        asset_id=asset_id,
        path=f"/fake/{asset_id}.mp4",
        display_name=f"Video_{asset_id}",
        media_type="video",
        metadata={
            "fps": fps,
            "total_frames": total_frames,
            "duration_seconds": total_frames / fps,
            "width": 1920,
            "height": 1080,
        },
    )


def _make_analysis_with_cuts(
    asset_id: str = "video-001",
    cut_frames: list[int] = None,
) -> SceneCutAnalysis:
    """Create a SceneCutAnalysis with accepted cuts."""
    cut_frames = cut_frames or [100, 200, 300]

    candidates = []
    for i, frame in enumerate(cut_frames):
        metrics = SceneCutMetrics(
            luma_delta=0.5,
            hist_delta=0.8,
            edge_delta=0.3,
            color_delta=0.4,
            black_score=0.0,
            flash_score=0.0,
            motion_score=0.2,
            combined_score=0.6,
        )
        candidates.append(SceneCutCandidate(
            source_frame=frame,
            pts=frame * 1000,
            seconds=frame / 24.0,
            score=0.6,
            confidence=0.85,
            kind=SceneCutKind.HARD_CUT,
            decision=SceneCutDecision.ACCEPTED,
            metrics=metrics,
            reasons=("high_score",),
        ))

    return SceneCutAnalysis(
        analysis_id=generate_analysis_id(asset_id),
        asset_id=asset_id,
        source_path=f"/fake/{asset_id}.mp4",
        settings=SceneCutSettings(sensitivity=0.55),
        candidates=tuple(candidates),
        created_at=time.time(),
        detector_version="1.0.0",
    )


def _make_project_with_asset() -> tuple[EditProject, MediaAsset]:
    """Create an EditProject with a video asset."""
    project = EditProject(project_id="test-project-001", name="Test Project")
    asset = _make_video_asset()
    project.assets[asset.asset_id] = asset
    return project, asset


# ================================================================
# Tests
# ================================================================

class TestAddCutsToMediaPool:
    """Test the Add Cuts to Media Pool workflow."""

    def test_add_accepted_cuts_creates_subclips(self):
        """Accepted cuts should create subclips in the project."""
        project, asset = _make_project_with_asset()
        analysis = _make_analysis_with_cuts(asset.asset_id, [100, 200])

        controller = SceneCutController(
            project_getter=lambda: project,
            project_setter=lambda p: None,
        )

        result = controller.add_cuts_to_media_pool(analysis, asset)

        assert result is not None
        assert result.added_count == 3  # 2 cuts = 3 subclips (before, between, after)
        assert len(result.added_assets) == 3

    def test_no_accepted_cuts_returns_none(self):
        """If no candidates are accepted, should return None."""
        project, asset = _make_project_with_asset()
        # All candidates PENDING (not accepted)
        analysis = _make_analysis_with_cuts(asset.asset_id, [100])
        pending_candidates = tuple(
            SceneCutCandidate(
                source_frame=c.source_frame,
                pts=c.pts,
                seconds=c.seconds,
                score=c.score,
                confidence=c.confidence,
                kind=c.kind,
                decision=SceneCutDecision.PENDING,  # Not accepted
                metrics=c.metrics,
                reasons=c.reasons,
            )
            for c in analysis.candidates
        )
        analysis = SceneCutAnalysis(
            analysis_id=analysis.analysis_id,
            asset_id=analysis.asset_id,
            source_path=analysis.source_path,
            settings=analysis.settings,
            candidates=pending_candidates,
        )

        controller = SceneCutController(
            project_getter=lambda: project,
            project_setter=lambda p: None,
        )

        result = controller.add_cuts_to_media_pool(analysis, asset)
        assert result is None

    def test_no_project_returns_none(self):
        """If no project available, should return None."""
        asset = _make_video_asset()
        analysis = _make_analysis_with_cuts(asset.asset_id, [100])

        controller = SceneCutController(
            project_getter=lambda: None,
            project_setter=lambda p: None,
        )

        result = controller.add_cuts_to_media_pool(analysis, asset)
        assert result is None

    def test_subclip_metadata_correct(self):
        """Subclips should have correct metadata (source_start, is_subclip, parent)."""
        project, asset = _make_project_with_asset()
        analysis = _make_analysis_with_cuts(asset.asset_id, [100, 200])

        controller = SceneCutController(
            project_getter=lambda: project,
            project_setter=lambda p: None,
        )

        result = controller.add_cuts_to_media_pool(analysis, asset)
        assert result is not None

        for subclip_asset in result.added_assets:
            assert is_subclip(subclip_asset)
            assert get_parent_asset_id(subclip_asset) == asset.asset_id
            assert subclip_asset.metadata.get("source_start") is not None
            assert subclip_asset.metadata.get("source_duration") is not None
            assert subclip_asset.metadata.get("source_total_frames") == asset.metadata["total_frames"]

    def test_parent_asset_unchanged(self):
        """Parent asset should not be modified."""
        project, asset = _make_project_with_asset()
        original_path = asset.path
        original_name = asset.display_name

        analysis = _make_analysis_with_cuts(asset.asset_id, [100])

        controller = SceneCutController(
            project_getter=lambda: project,
            project_setter=lambda p: None,
        )

        controller.add_cuts_to_media_pool(analysis, asset)

        # Parent asset should be unchanged
        assert asset.path == original_path
        assert asset.display_name == original_name
        assert not is_subclip(asset)

    def test_subclip_emits_signal(self):
        """Controller should emit subclips_added signal."""
        project, asset = _make_project_with_asset()
        analysis = _make_analysis_with_cuts(asset.asset_id, [100])

        controller = SceneCutController(
            project_getter=lambda: project,
            project_setter=lambda p: None,
        )

        signal_received = []
        controller.subclips_added.connect(lambda r: signal_received.append(r))

        controller.add_cuts_to_media_pool(analysis, asset)

        assert len(signal_received) == 1
        assert signal_received[0].added_count == 2  # 1 cut = 2 subclips
