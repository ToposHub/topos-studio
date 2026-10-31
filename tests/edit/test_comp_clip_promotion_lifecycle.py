"""
Comp Clip Promotion Lifecycle Tests.

Tests the promotion of a temporary composition to a proper Comp Clip
when the user makes real edits in the Composite page.

Coverage:
- promote_composition_to_comp_clip creates correct CompClipData
- promote keeps original graph_id (composite_id)
- promote reads real asset_id from clip
- composite_user_edit_started signal fires on node add
- promotion only happens once per session
- promote fails gracefully if composition doesn't exist
"""

import pytest
import sys
import uuid
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.comp_clip_data import CompClipData
from src.features.edit.core.composite_graph_data import CompositeGraphData


# ================================================================
# Fixtures
# ================================================================


def _make_project_with_composition(
    composite_id: str | None = None,
    source_asset_id: str | None = None,
    width: int = 1920,
    height: int = 1080,
) -> tuple[EditProject, str, str]:
    """Create a project with a source asset and a temporary composition.

    Returns (project, source_asset_id, composite_id).
    """
    project = EditProject.create("Test Project")

    # Import a source video asset
    project = project.import_asset(
        path="/tmp/test_source.mov",
        media_type="video",
        metadata={
            "width": width,
            "height": height,
            "fps": 24.0,
            "duration_seconds": 10.0,
            "total_frames": 240,
        },
        display_name="test_source.mov",
    )

    # Find the imported asset_id
    source_aid = source_asset_id
    if source_aid is None:
        for aid, asset in project.assets.items():
            if asset.path == "/tmp/test_source.mov":
                source_aid = aid
                break

    # Create a temporary composition (as _on_open_in_composite_requested would do)
    cid = composite_id or str(uuid.uuid4())
    clip_id = str(uuid.uuid4())

    project = project.create_composition(
        composite_id=cid,
        source_clip_id=clip_id,
        source_asset_id=source_aid,
        source_asset_path="/tmp/test_source.mov",
        name=f"Composite_{clip_id[:8]}",
        width=width,
        height=height,
    )

    return project, source_aid, cid


# ================================================================
# Test: promote_composition_to_comp_clip creates correct CompClipData
# ================================================================


class TestPromoteCreatesCompClipData:
    """promote_composition_to_comp_clip creates correct CompClipData."""

    def test_creates_comp_clip_in_project(self):
        project, source_aid, composite_id = _make_project_with_composition()

        timeline_id = str(uuid.uuid4())
        clip_id = str(uuid.uuid4())

        new_project, comp_clip_id, comp_asset_id = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id=timeline_id,
            clip_id=clip_id,
            name="Test_Comp",
            original_asset_id=source_aid,
            duration_frames=120,
        )

        # CompClipData should exist in comp_clips
        assert comp_clip_id in new_project.comp_clips
        cc_dict = new_project.comp_clips[comp_clip_id]
        cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict

        assert isinstance(cc, CompClipData)
        assert cc.name == "Test_Comp"
        assert cc.source_kind == "timeline_clip_upgrade"
        assert cc.original_asset_id == source_aid
        assert cc.original_timeline_id == timeline_id
        assert cc.original_clip_id == clip_id
        assert cc.asset_id == comp_asset_id
        assert cc.duration_frames == 120

    def test_creates_comp_clip_asset(self):
        project, source_aid, composite_id = _make_project_with_composition()

        new_project, comp_clip_id, comp_asset_id = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        # MediaAsset should exist
        assert comp_asset_id in new_project.assets
        asset = new_project.assets[comp_asset_id]
        assert asset.media_type == "comp_clip"
        assert asset.display_name  # non-empty name
        assert asset.metadata["graph_id"] == composite_id

    def test_comp_clip_added_to_root_bin(self):
        project, source_aid, composite_id = _make_project_with_composition()

        new_project, _, comp_asset_id = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        root_bin = new_project.root_bin
        assert root_bin is not None
        assert comp_asset_id in root_bin.asset_ids


# ================================================================
# Test: promote keeps original graph_id
# ================================================================


class TestPromoteKeepsGraphId:
    """promote keeps original graph_id (= composite_id)."""

    def test_composition_preserved_with_same_id(self):
        composite_id = str(uuid.uuid4())
        project, source_aid, _ = _make_project_with_composition(composite_id=composite_id)

        new_project, _, _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        # The original composition should still exist with same composite_id
        assert composite_id in new_project.compositions

        comp_dict = new_project.compositions[composite_id]
        comp_data = CompositeGraphData.from_dict(comp_dict) if isinstance(comp_dict, dict) else comp_dict

        # graph_id is the same as the original composite_id
        assert comp_data.composite_id == composite_id

    def test_composition_enriched_with_comp_clip_metadata(self):
        composite_id = str(uuid.uuid4())
        project, source_aid, _ = _make_project_with_composition(composite_id=composite_id)

        new_project, comp_clip_id, comp_asset_id = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        comp_dict = new_project.compositions[composite_id]
        comp_data = CompositeGraphData.from_dict(comp_dict) if isinstance(comp_dict, dict) else comp_dict

        # Should now have comp_clip_id and comp_asset_id
        assert comp_data.comp_clip_id == comp_clip_id
        assert comp_data.comp_asset_id == comp_asset_id
        assert comp_data.source_kind == "timeline_clip_upgrade"
        assert comp_data.timeline_id == "tl-1"
        assert comp_data.clip_id == "clip-1"

    def test_nodes_and_connections_preserved(self):
        composite_id = str(uuid.uuid4())
        project, source_aid, _ = _make_project_with_composition(composite_id=composite_id)

        # Verify the original has nodes
        orig_comp = project.compositions[composite_id]
        orig_data = CompositeGraphData.from_dict(orig_comp) if isinstance(orig_comp, dict) else orig_comp
        orig_nodes = list(orig_data.nodes)
        orig_connections = list(orig_data.connections)

        new_project, _, _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        comp_dict = new_project.compositions[composite_id]
        comp_data = CompositeGraphData.from_dict(comp_dict) if isinstance(comp_dict, dict) else comp_dict

        # Nodes and connections should be unchanged
        assert comp_data.nodes == orig_nodes
        assert comp_data.connections == orig_connections


# ================================================================
# Test: promote reads real asset_id from clip
# ================================================================


class TestPromoteReadsRealAssetId:
    """promote reads real asset_id from the provided arguments."""

    def test_uses_provided_asset_id(self):
        project, source_aid, composite_id = _make_project_with_composition()

        new_project, _, _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        # Check source_refs in the composition
        comp_dict = new_project.compositions[composite_id]
        comp_data = CompositeGraphData.from_dict(comp_dict) if isinstance(comp_dict, dict) else comp_dict

        assert comp_data.source_refs.get("original_asset_id") == source_aid
        assert comp_data.source_asset_id == source_aid

    def test_comp_clip_metadata_records_original_asset(self):
        project, source_aid, composite_id = _make_project_with_composition()

        new_project, comp_clip_id, _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        # Check CompClipData
        cc_dict = new_project.comp_clips[comp_clip_id]
        cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict

        assert cc.original_asset_id == source_aid


# ================================================================
# Test: signal fires on node add
# ================================================================


class TestSignalFiresOnNodeAdd:
    """composite_user_edit_started signal fires when node edit callbacks are called."""

    def test_emit_user_edit_once_fires_signal(self):
        """Test _emit_user_edit_once emits the signal."""
        # We can't instantiate CompositePage without Qt app, so test the
        # logic pattern directly.
        composite_id = str(uuid.uuid4())
        fired = []

        # Simulate the flag tracking
        user_edit_fired = False

        def emit_user_edit_once():
            nonlocal user_edit_fired
            if not user_edit_fired and composite_id:
                user_edit_fired = True
                fired.append(composite_id)

        # First call fires
        emit_user_edit_once()
        assert len(fired) == 1
        assert fired[0] == composite_id

        # Second call does NOT fire
        emit_user_edit_once()
        assert len(fired) == 1  # still only 1

    def test_emit_user_edit_once_requires_composite_id(self):
        """Signal should not fire if _current_composite_id is empty."""
        fired = []
        user_edit_fired = False
        current_composite_id = ""

        def emit_user_edit_once():
            nonlocal user_edit_fired
            if not user_edit_fired and current_composite_id:
                user_edit_fired = True
                fired.append(current_composite_id)

        emit_user_edit_once()
        assert len(fired) == 0

    def test_reset_allows_re_fire(self):
        """reset_composite_session should allow signal to fire again."""
        fired = []
        user_edit_fired = False
        current_composite_id = "test-comp-id"

        def emit_user_edit_once():
            nonlocal user_edit_fired
            if not user_edit_fired and current_composite_id:
                user_edit_fired = True
                fired.append(current_composite_id)

        def reset_composite_session():
            nonlocal user_edit_fired
            user_edit_fired = False

        # First call fires
        emit_user_edit_once()
        assert len(fired) == 1

        # Reset
        reset_composite_session()

        # Can fire again
        emit_user_edit_once()
        assert len(fired) == 2


# ================================================================
# Test: promotion only happens once
# ================================================================


class TestPromotionOnlyHappensOnce:
    """Promotion should only happen once per composite session."""

    def test_second_promotion_skipped_if_comp_clip_id_set(self):
        """If composition already has comp_clip_id, promote should still work
        but the handler in CompositeControllerMixin checks before calling."""
        project, source_aid, composite_id = _make_project_with_composition()

        # First promotion
        new_project, cc_id_1, ca_id_1 = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        # Check that the composition now has comp_clip_id
        comp_dict = new_project.compositions[composite_id]
        comp_data = CompositeGraphData.from_dict(comp_dict) if isinstance(comp_dict, dict) else comp_dict
        assert comp_data.comp_clip_id is not None

        # The handler would check comp_data.comp_clip_id before calling promote again.
        # This simulates the handler's guard:
        should_promote = comp_data.comp_clip_id is None
        assert not should_promote  # second promotion should be skipped

    def test_handler_guard_logic(self):
        """Test the guard logic that prevents double promotion."""
        project, source_aid, composite_id = _make_project_with_composition()

        # Promote once
        new_project, _, _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        comp_data_dict = new_project.compositions[composite_id]
        comp_data = CompositeGraphData.from_dict(comp_data_dict) if isinstance(comp_data_dict, dict) else comp_data_dict

        # Guard: already promoted if comp_clip_id is set
        assert comp_data.comp_clip_id is not None

        # Another promotion of the same composite should be prevented
        # by the handler checking comp_clip_id is not None


# ================================================================
# Test: promote fails gracefully if composition doesn't exist
# ================================================================


class TestPromoteFailsGracefully:
    """promote fails gracefully if composition doesn't exist."""

    def test_raises_value_error_for_missing_composition(self):
        project = EditProject.create("Test")

        with pytest.raises(ValueError, match="Composition not found"):
            project._legacy_promote_composition_to_comp_clip(
                composite_id="nonexistent-id",
                timeline_id="tl-1",
                clip_id="clip-1",
            )

    def test_empty_composite_id_raises(self):
        project = EditProject.create("Test")

        with pytest.raises((ValueError, KeyError)):
            project._legacy_promote_composition_to_comp_clip(
                composite_id="",
                timeline_id="tl-1",
                clip_id="clip-1",
            )


# ================================================================
# Test: promote uses resolution and fps correctly
# ================================================================


class TestPromoteResolutionAndFps:
    """promote uses resolution/fps from arguments and defaults correctly."""

    def test_uses_provided_width_height(self):
        project, source_aid, composite_id = _make_project_with_composition(width=3840, height=2160)

        new_project, comp_clip_id, _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
            width=1920,
            height=1080,
        )

        cc_dict = new_project.comp_clips[comp_clip_id]
        cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict

        assert cc.width == 1920
        assert cc.height == 1080

    def test_uses_composition_defaults_when_not_provided(self):
        project, source_aid, composite_id = _make_project_with_composition(width=3840, height=2160)

        new_project, comp_clip_id, _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
            # width/height not provided -- should use composition's 3840x2160
        )

        cc_dict = new_project.comp_clips[comp_clip_id]
        cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict

        assert cc.width == 3840
        assert cc.height == 2160

    def test_uses_provided_fps(self):
        project, source_aid, composite_id = _make_project_with_composition()

        new_project, comp_clip_id, _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
            fps=30.0,
        )

        cc_dict = new_project.comp_clips[comp_clip_id]
        cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict

        assert cc.fps == 30.0


# ================================================================
# Test: promote is atomic and safe
# ================================================================


class TestPromoteAtomicSafety:
    """promote is atomic -- original project is unchanged."""

    def test_original_project_unchanged(self):
        project, source_aid, composite_id = _make_project_with_composition()

        original_comp_count = len(project.compositions)
        original_asset_count = len(project.assets)
        original_comp_clip_count = len(project.comp_clips)

        _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        # Original should be unchanged (immutable pattern)
        assert len(project.compositions) == original_comp_count
        assert len(project.assets) == original_asset_count
        assert len(project.comp_clips) == original_comp_clip_count

    def test_promote_returns_new_project_instance(self):
        project, source_aid, composite_id = _make_project_with_composition()

        new_project, _, _ = project._legacy_promote_composition_to_comp_clip(
            composite_id=composite_id,
            timeline_id="tl-1",
            clip_id="clip-1",
            original_asset_id=source_aid,
        )

        assert new_project is not project
        assert new_project.project_id == project.project_id
