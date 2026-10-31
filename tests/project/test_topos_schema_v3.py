"""
Tests for .topos schema v3 upgrade.

Schema v3 rules (inherits all v2 rules):
- Standalone Color projects only support images (standalone_image)
- Video color grading must go through Edit timeline clips (timeline_clip)
- ColorStackData has scope, source_kind, timeline_id, clip_id fields
- Manifest schema_version == 3
- Default composite graph is Input → Topos Color → Output (3 nodes)
- Topos Color node stores color_stack_id reference (not independent copy)
- Explicit Comp Clip creation (no auto-promote on page switch)
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile

import pytest

from src.shared.project.manifest import (
    ToposManifest,
    TOPOS_SCHEMA_VERSION,
    PROJECT_MODE_STANDALONE_IMAGE,
    PROJECT_MODE_EDIT_PROJECT,
)
from src.shared.project.topos_project import ToposProject, MediaAssetRef
from src.shared.project.topos_project_io import ToposProjectIO
from src.shared.project.color_stack import ColorStackData
from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    ColorStackData as EditColorStackData,
)


# -----------------------------------------------------------------------
# Schema v2 constants
# -----------------------------------------------------------------------

class TestSchemaV2Constants:
    """Verify schema v3 constants are defined correctly."""

    def test_schema_version_is_3(self):
        assert TOPOS_SCHEMA_VERSION == 3

    def test_standalone_image_constant(self):
        assert PROJECT_MODE_STANDALONE_IMAGE == "standalone_image"

    def test_edit_project_constant(self):
        assert PROJECT_MODE_EDIT_PROJECT == "edit_project"


# -----------------------------------------------------------------------
# Manifest schema v2
# -----------------------------------------------------------------------

class TestManifestSchemaV2:
    """Verify manifest defaults to schema v3."""

    def test_default_schema_version(self):
        manifest = ToposManifest.create(project_id="test-1")
        assert manifest.schema_version == 3

    def test_default_project_mode(self):
        manifest = ToposManifest.create(project_id="test-1")
        assert manifest.project_mode == "standalone_image"

    def test_standalone_image_mode_valid(self):
        manifest = ToposManifest.create(
            project_id="test-1",
            project_mode="standalone_image",
        )
        assert manifest.project_mode == "standalone_image"

    def test_edit_project_mode_valid(self):
        manifest = ToposManifest.create(
            project_id="test-1",
            project_mode="edit_project",
        )
        assert manifest.project_mode == "edit_project"

    def test_single_asset_mode_rejected(self):
        """single_asset is no longer a valid project_mode."""
        with pytest.raises(ValueError, match="Invalid project_mode"):
            ToposManifest.create(
                project_id="test-1",
                project_mode="single_asset",
            )

    def test_roundtrip_preserves_schema_version(self):
        manifest = ToposManifest.create(project_id="test-1")
        data = manifest.to_dict()
        assert data["schema_version"] == 3
        restored = ToposManifest.from_dict(data)
        assert restored.schema_version == 3

    def test_v2_manifest_auto_migrates_to_v3(self):
        """Loading a v2 manifest auto-migrates to v3."""
        data = {
            "format": "topos_project_archive",
            "schema_version": 2,
            "project_id": "test-migrate",
            "project_mode": "standalone_image",
        }
        restored = ToposManifest.from_dict(data)
        assert restored.schema_version == 3


# -----------------------------------------------------------------------
# ToposProject schema v2
# -----------------------------------------------------------------------

class TestToposProjectSchemaV2:
    """Verify ToposProject schema v2 behavior."""

    def test_create_standalone_image_with_jpg(self):
        project = ToposProject.create_standalone_image("Photo", "/tmp/photo.jpg")
        assert project.project_mode == "standalone_image"
        assert project.primary_asset.media_type == "image"
        assert project.primary_color_stack_id is not None

    def test_create_standalone_image_with_png(self):
        project = ToposProject.create_standalone_image("Photo", "/tmp/photo.png")
        assert project.project_mode == "standalone_image"

    def test_create_standalone_image_with_tiff(self):
        project = ToposProject.create_standalone_image("Photo", "/tmp/photo.tiff")
        assert project.project_mode == "standalone_image"

    def test_create_standalone_image_with_exr(self):
        project = ToposProject.create_standalone_image("HDR", "/tmp/render.exr")
        assert project.project_mode == "standalone_image"

    def test_create_standalone_image_rejects_video(self):
        """create_standalone_image must reject video files."""
        with pytest.raises(ValueError, match="only accepts image files"):
            ToposProject.create_standalone_image("Video", "/tmp/clip.mp4")

    def test_create_standalone_image_rejects_mov(self):
        with pytest.raises(ValueError, match="only accepts image files"):
            ToposProject.create_standalone_image("Video", "/tmp/clip.mov")

    def test_create_single_asset_rejects_video(self):
        """create_single_asset must reject non-image media_type."""
        with pytest.raises(ValueError, match="no longer supports"):
            ToposProject.create_single_asset("Video", "/tmp/clip.mp4", media_type="video")

    def test_create_single_asset_accepts_image(self):
        """create_single_asset with image still works (delegates to create_standalone_image)."""
        project = ToposProject.create_single_asset("Photo", "/tmp/photo.jpg", media_type="image")
        assert project.project_mode == "standalone_image"

    def test_default_project_mode_is_standalone_image(self):
        project = ToposProject.create(name="Test")
        assert project.project_mode == "standalone_image"


# -----------------------------------------------------------------------
# ColorStackData schema v2 fields
# -----------------------------------------------------------------------

class TestColorStackDataSchemaV2:
    """Verify ColorStackData has schema v2 ownership fields."""

    def test_create_standalone_image_factory(self):
        stack = ColorStackData.create_standalone_image(
            asset_id="asset-1", name="Image Grade"
        )
        assert stack.scope == "asset_default"
        assert stack.source_kind == "standalone_image"
        assert stack.timeline_id is None
        assert stack.clip_id is None
        assert stack.asset_id == "asset-1"

    def test_create_timeline_clip_factory(self):
        stack = ColorStackData.create_timeline_clip(
            asset_id="asset-1",
            timeline_id="timeline-1",
            clip_id="clip-1",
            name="Clip Grade",
        )
        assert stack.scope == "clip_local"
        assert stack.source_kind == "timeline_clip"
        assert stack.timeline_id == "timeline-1"
        assert stack.clip_id == "clip-1"
        assert stack.asset_id == "asset-1"

    def test_to_dict_includes_v2_fields(self):
        stack = ColorStackData.create_timeline_clip(
            asset_id="a1", timeline_id="t1", clip_id="c1"
        )
        d = stack.to_dict()
        assert d["scope"] == "clip_local"
        assert d["source_kind"] == "timeline_clip"
        assert d["timeline_id"] == "t1"
        assert d["clip_id"] == "c1"

    def test_from_dict_reads_v2_fields(self):
        data = {
            "stack_id": "s1",
            "asset_id": "a1",
            "name": "Test",
            "scope": "clip_local",
            "source_kind": "timeline_clip",
            "timeline_id": "t1",
            "clip_id": "c1",
        }
        stack = ColorStackData.from_dict(data)
        assert stack.scope == "clip_local"
        assert stack.source_kind == "timeline_clip"
        assert stack.timeline_id == "t1"
        assert stack.clip_id == "c1"

    def test_from_dict_defaults_v2_fields(self):
        """When v2 fields are missing, sensible defaults are used."""
        data = {"stack_id": "s1", "asset_id": "a1"}
        stack = ColorStackData.from_dict(data)
        assert stack.scope == "clip_local"
        assert stack.source_kind == "timeline_clip"
        assert stack.timeline_id is None
        assert stack.clip_id is None

    def test_with_methods_preserve_v2_fields(self):
        """All with_* methods must preserve v2 ownership fields."""
        stack = ColorStackData.create_timeline_clip(
            asset_id="a1", timeline_id="t1", clip_id="c1"
        )
        updated = stack.with_name("New Name")
        assert updated.scope == stack.scope
        assert updated.source_kind == stack.source_kind
        assert updated.timeline_id == stack.timeline_id
        assert updated.clip_id == stack.clip_id

        updated2 = stack.with_grading_params({"exposure": 1.0})
        assert updated2.timeline_id == "t1"
        assert updated2.clip_id == "c1"


# -----------------------------------------------------------------------
# EditProject ColorStackData schema v2 fields
# -----------------------------------------------------------------------

class TestEditProjectColorStackDataSchemaV2:
    """Verify EditProject's ColorStackData has schema v2 ownership fields."""

    def test_create_standalone_image_factory(self):
        stack = EditColorStackData.create_standalone_image(
            asset_id="asset-1", name="Image Grade"
        )
        assert stack.scope == "asset_default"
        assert stack.source_kind == "standalone_image"
        assert stack.timeline_id is None
        assert stack.clip_id is None

    def test_create_timeline_clip_factory(self):
        stack = EditColorStackData.create_timeline_clip(
            asset_id="asset-1",
            timeline_id="timeline-1",
            clip_id="clip-1",
            name="Clip Grade",
        )
        assert stack.scope == "clip_local"
        assert stack.source_kind == "timeline_clip"
        assert stack.timeline_id == "timeline-1"
        assert stack.clip_id == "clip-1"

    def test_to_dict_includes_v2_fields(self):
        stack = EditColorStackData.create_timeline_clip(
            asset_id="a1", timeline_id="t1", clip_id="c1"
        )
        d = stack.to_dict()
        assert d["scope"] == "clip_local"
        assert d["source_kind"] == "timeline_clip"
        assert d["timeline_id"] == "t1"
        assert d["clip_id"] == "c1"

    def test_from_dict_reads_v2_fields(self):
        data = {
            "stack_id": "s1",
            "asset_id": "a1",
            "scope": "clip_local",
            "source_kind": "timeline_clip",
            "timeline_id": "t1",
            "clip_id": "c1",
        }
        stack = EditColorStackData.from_dict(data)
        assert stack.scope == "clip_local"
        assert stack.source_kind == "timeline_clip"
        assert stack.timeline_id == "t1"
        assert stack.clip_id == "c1"


# -----------------------------------------------------------------------
# EditProject timeline clip grading integration
# -----------------------------------------------------------------------

class TestTimelineClipGradingIntegration:
    """End-to-end test: create EditProject with video clip, save .topos, verify v2."""

    def test_save_and_load_edit_project_with_clip_color_stack(self):
        """Create EditProject with video clip + color stack, save, verify v2 metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Create EditProject with a video asset
            project = EditProject.create("Test Edit")
            project = project.import_asset(
                path="/tmp/test_video.mp4",
                media_type="video",
                metadata={"width": 1920, "height": 1080, "fps": 24.0},
            )

            # Get the asset_id
            asset_id = None
            for aid, asset in project.assets.items():
                if asset.path == "/tmp/test_video.mp4":
                    asset_id = aid
                    break
            assert asset_id is not None

            # 2. Create a timeline
            project = project.create_timeline("Main Timeline", fps=24.0)
            timeline_id = project.active_timeline_id
            assert timeline_id is not None

            # 3. Create a clip-local color stack
            clip_id = "clip-001"
            stack = EditColorStackData.create_timeline_clip(
                asset_id=asset_id,
                timeline_id=timeline_id,
                clip_id=clip_id,
                name="Video Clip Grade",
            )

            # Add stack to project
            project = EditProject(
                project_id=project.project_id,
                name=project.name,
                assets=project.assets,
                bins=project.bins,
                timelines=project.timelines,
                compositions=project.compositions,
                color_stacks={stack.stack_id: stack},
                root_bin_id=project.root_bin_id,
                active_timeline_id=project.active_timeline_id,
                created_at=project.created_at,
                modified_at=project.modified_at,
            )

            # 4. Convert and save via ToposProjectIO
            from src.features.edit.core.edit_project_io import convert_edit_project_to_topos_project
            topos_project = convert_edit_project_to_topos_project(project)

            assert topos_project.project_mode == "edit_project"

            project_path = os.path.join(tmpdir, "test_edit.topos")
            success = ToposProjectIO.save(
                topos_project,
                project_path,
                full_color_stacks_data={
                    stack.stack_id: stack.to_dict()
                },
            )
            assert success

            # 5. Verify manifest has schema_version == 3
            with zipfile.ZipFile(project_path, "r") as zf:
                manifest_data = json.loads(zf.read("manifest.json"))
                assert manifest_data["schema_version"] == 3
                assert manifest_data["project_mode"] == "edit_project"

                project_data = json.loads(zf.read("project.json"))
                # Verify project.json ColorStackRef has scope
                stacks = project_data.get("color_stacks", {})
                assert len(stacks) == 1
                stack_ref = list(stacks.values())[0]
                assert stack_ref.get("scope") == "clip_local"

                # Verify color/color_stacks.json has full v2 fields
                color_stacks_json = json.loads(zf.read("color/color_stacks.json"))
                assert len(color_stacks_json) == 1
                full_stack = list(color_stacks_json.values())[0]
                assert full_stack.get("source_kind") == "timeline_clip"
                assert full_stack.get("timeline_id") == timeline_id
                assert full_stack.get("clip_id") == clip_id

    def test_load_rejects_standalone_video_project(self):
        """Loading a project with project_mode='single_asset' and video primary asset must fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "video_project.topos")

            manifest_data = {
                "format": "topos_project_archive",
                "schema_version": 1,
                "project_id": "test-video-project",
                "project_mode": "single_asset",
            }
            project_data = {
                "project_id": "test-video-project",
                "name": "Video Project",
                "project_mode": "single_asset",
                "assets": {
                    "asset-1": {
                        "asset_id": "asset-1",
                        "path": "/tmp/video.mp4",
                        "media_type": "video",
                        "display_name": "Video",
                        "is_offline": False,
                    }
                },
                "bins": {},
                "timelines": {},
                "color_stacks": {},
                "compositions": {},
                "deliver_presets": {},
                "ui_state": {},
                "settings": {},
                "primary_asset_id": "asset-1",
            }

            with zipfile.ZipFile(project_path, "w") as zf:
                zf.writestr("manifest.json", json.dumps(manifest_data))
                zf.writestr("project.json", json.dumps(project_data))

            result_project, result_data = ToposProjectIO.load(project_path)
            # Should be rejected (None) because single_asset + video
            assert result_project is None
            assert result_data is None

    def test_load_rejects_standalone_image_with_non_image(self):
        """Loading a project with project_mode='standalone_image' and non-image primary must fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "bad_project.topos")

            manifest_data = {
                "format": "topos_project_archive",
                "schema_version": 2,
                "project_id": "test-bad-project",
                "project_mode": "standalone_image",
            }
            project_data = {
                "project_id": "test-bad-project",
                "name": "Bad Project",
                "project_mode": "standalone_image",
                "assets": {
                    "asset-1": {
                        "asset_id": "asset-1",
                        "path": "/tmp/video.mp4",
                        "media_type": "video",
                        "display_name": "Video",
                        "is_offline": False,
                    }
                },
                "bins": {},
                "timelines": {},
                "color_stacks": {},
                "compositions": {},
                "deliver_presets": {},
                "ui_state": {},
                "settings": {},
                "primary_asset_id": "asset-1",
            }

            with zipfile.ZipFile(project_path, "w") as zf:
                zf.writestr("manifest.json", json.dumps(manifest_data))
                zf.writestr("project.json", json.dumps(project_data))

            result_project, result_data = ToposProjectIO.load(project_path)
            assert result_project is None
            assert result_data is None


# -----------------------------------------------------------------------
# SaveRouter video enforcement
# -----------------------------------------------------------------------

class TestSaveRouterVideoEnforcement:
    """Verify SaveRouter rejects video in single-asset save path."""

    def test_unified_color_save_requires_project(self):
        from src.shared.project.save_router import SaveRouter
        from unittest.mock import MagicMock

        router = SaveRouter()
        mock_engine = MagicMock()
        mock_engine.layers = []
        mock_engine.active_layer_index = 0

        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_current_file_path": lambda: "/test/video.mp4",
            "get_engine": lambda: mock_engine,
            "get_current_edit_project": lambda: None,
        })

        result = router._save_from_color()
        assert result is False

    def test_unified_color_save_accepts_image_project(self):
        from src.shared.project.save_router import SaveRouter
        from src.features.edit.core.project import EditProject
        from unittest.mock import MagicMock

        router = SaveRouter()
        mock_engine = MagicMock()
        mock_engine.layers = []
        mock_engine.active_layer_index = 0

        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_current_file_path": lambda: "/test/image.jpg",
            "get_engine": lambda: mock_engine,
            "get_current_edit_project": lambda: EditProject.create_image_project(
                "Image", "/test/image.jpg"
            ),
            "get_edit_project_io": lambda: MagicMock(
                save_project=MagicMock(return_value=True)
            ),
            "get_ui_state": lambda: None,
            "get_current_timeline_sequences": lambda: None,
            "on_edit_project_updated": lambda project: None,
        })

        result = router._save_from_color()
        assert result is True

    def test_save_color_timeline_clip_saves_correctly(self):
        """Color save with TIMELINE_CLIP session routes to EditProject successfully."""
        from src.shared.project.save_router import SaveRouter
        from src.features.color.core.color_session import ColorSession, SourceKind
        from unittest.mock import MagicMock

        router = SaveRouter()
        mock_engine = MagicMock()
        mock_engine.layers = []
        mock_engine.active_layer_index = 0

        mock_edit_project = MagicMock()
        mock_edit_project.color_stacks = {}
        mock_edit_project.project_id = "proj-1"
        mock_edit_project.name = "Test"
        mock_edit_project.assets = {}
        mock_edit_project.bins = {}
        mock_edit_project.timelines = {}
        mock_edit_project.compositions = {}
        mock_edit_project.root_bin_id = None
        mock_edit_project.active_timeline_id = None
        mock_edit_project.created_at = 0.0
        mock_edit_project.modified_at = 0.0

        session = ColorSession.create_timeline_clip(
            project_id="proj-1",
            asset_id="asset-1",
            clip_id="clip-1",
            color_stack_id="stack-1",
            source_path="/test/video.mp4",
            timeline_id="timeline-1",
        )
        assert session.source_kind == SourceKind.TIMELINE_CLIP

        mock_edit_project_io = MagicMock()
        mock_edit_project_io.save_project.return_value = True

        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_engine": lambda: mock_engine,
            "get_current_color_session": lambda: session,
            "get_current_edit_project": lambda: mock_edit_project,
            "get_edit_project_filepath": lambda: "/test/project.topos",
            "get_edit_project_io": lambda: mock_edit_project_io,
            "get_ui_state": lambda: None,
            "get_current_timeline_sequences": lambda: None,
            "on_edit_project_updated": lambda p: None,
        })

        success = router.save()
        assert success
        mock_edit_project_io.save_project.assert_called_once()

    def test_save_color_timeline_clip_no_edit_project_fails(self):
        """Color save with TIMELINE_CLIP session fails if no EditProject."""
        from src.shared.project.save_router import SaveRouter
        from src.features.color.core.color_session import ColorSession, SourceKind
        from unittest.mock import MagicMock

        router = SaveRouter()
        mock_engine = MagicMock()
        mock_engine.layers = []
        mock_engine.active_layer_index = 0

        session = ColorSession.create_timeline_clip(
            project_id="proj-1",
            asset_id="asset-1",
            clip_id="clip-1",
            color_stack_id="stack-1",
            source_path="/test/video.mp4",
            timeline_id="timeline-1",
        )

        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_engine": lambda: mock_engine,
            "get_current_color_session": lambda: session,
            "get_current_edit_project": lambda: None,
            "get_edit_project_filepath": lambda: "/test/project.topos",
        })

        success = router.save()
        assert not success


# -----------------------------------------------------------------------
# Schema v3 specific tests
# -----------------------------------------------------------------------

class TestSchemaV3Semantics:
    """Verify schema v3 specific rules."""

    def test_default_graph_has_topos_color(self):
        """v3 default composite graph must be Input → Topos Color → Output."""
        from src.features.composite.core.node_engine import NodeGraph
        graph = NodeGraph()
        graph.create_default_graph()
        assert len(graph.nodes) == 3
        node_types = {n.node_type for n in graph.nodes.values()}
        assert "topos_color" in node_types
        assert len(graph.connections) == 2

    def test_topos_color_node_uses_color_stack_reference(self):
        """v3 Topos Color node stores color_stack_id, not independent copy."""
        from src.features.composite.core.node_engine import NodeGraph
        graph = NodeGraph()
        graph.create_default_graph()
        topos_node = next(n for n in graph.nodes.values() if n.node_type == "topos_color")
        # Should have color_stack_id parameter (empty string when no stack assigned)
        assert hasattr(topos_node, 'parameters')
        assert 'color_stack_id' in topos_node.parameters

    def test_v1_manifest_auto_migrates_to_v3(self):
        """Loading a v1 manifest auto-migrates to v3."""
        data = {
            "format": "topos_project_archive",
            "schema_version": 1,
            "project_id": "test-v1-migrate",
            "project_mode": "standalone_image",
        }
        restored = ToposManifest.from_dict(data)
        assert restored.schema_version == 3

    def test_standalone_image_with_video_rejected(self):
        """Schema v3 rejects standalone_image project with video primary asset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "bad_project.topos")
            manifest_data = {
                "format": "topos_project_archive",
                "schema_version": 3,
                "project_id": "test-v3-video",
                "project_mode": "standalone_image",
            }
            project_data = {
                "project_id": "test-v3-video",
                "name": "Bad Video Project",
                "project_mode": "standalone_image",
                "assets": {
                    "asset-1": {
                        "asset_id": "asset-1",
                        "path": "/tmp/video.mp4",
                        "media_type": "video",
                        "display_name": "Video",
                        "is_offline": False,
                    }
                },
                "bins": {},
                "timelines": {},
                "color_stacks": {},
                "compositions": {},
                "deliver_presets": {},
                "ui_state": {},
                "settings": {},
                "primary_asset_id": "asset-1",
            }

            with zipfile.ZipFile(project_path, "w") as zf:
                zf.writestr("manifest.json", json.dumps(manifest_data))
                zf.writestr("project.json", json.dumps(project_data))

            result_project, result_data = ToposProjectIO.load(project_path)
            assert result_project is None
            assert result_data is None

    def test_schema_v3_saves_comp_clip_graph(self):
        """Verify comp_clip's graph is saved correctly with nodes and connections."""
        from src.features.edit.core.project import EditProject
        project = EditProject(project_id="test-comp-clip-graph", name="comp-clip-graph-test")
        # create_empty_comp_clip returns (new_project, comp_clip_id, comp_asset_id, graph_id)
        project, comp_clip_id, comp_asset_id, graph_id = project.create_empty_comp_clip()
        assert comp_clip_id is not None

        # Check the graph has the correct structure
        compositions = project.compositions
        assert graph_id in compositions
        graph_data = compositions[graph_id]
        nodes = graph_data.get("nodes", [])
        assert len(nodes) >= 2, f"Expected at least 2 nodes, got {nodes}"
        node_types = {n.get("type", "") for n in nodes}
        assert "output" in node_types, f"Expected output node in {node_types}"
        # Should have connections between nodes
        connections = graph_data.get("connections", [])
        assert len(connections) >= 1, f"Expected at least 1 connection, got {connections}"

    def test_schema_v3_allows_comp_clip_asset_refs(self):
        """Autosave/project conversion must accept virtual comp_clip assets."""
        ref = MediaAssetRef(
            asset_id="comp-asset-1",
            path="topos://comp_clip/comp-asset-1",
            media_type="comp_clip",
            display_name="Comp 1",
        )
        assert ref.media_type == "comp_clip"

    def test_edit_project_conversion_preserves_comp_clip_assets(self):
        """Edit autosave should not fail when media pool contains comp_clip assets."""
        from src.features.edit.core.edit_project_io import convert_edit_project_to_topos_project

        asset = MediaAsset(
            asset_id="comp-asset-1",
            path="topos://comp_clip/comp-asset-1",
            media_type="comp_clip",
            display_name="Comp 1",
            metadata={"graph_id": "graph-1", "total_frames": 120, "fps": 24.0},
        )
        project = EditProject(
            project_id="project-with-comp-asset",
            name="project-with-comp-asset",
            assets={asset.asset_id: asset},
        )

        topos_project = convert_edit_project_to_topos_project(project)

        assert topos_project.assets[asset.asset_id].media_type == "comp_clip"

    def test_schema_v3_default_composite_graph_has_topos_color(self):
        """Verify that the default composite graph (NodeGraph.create_default_graph) includes Topos Color."""
        from src.features.composite.core.node_engine import NodeGraph
        graph = NodeGraph()
        graph.create_default_graph()

        # Default graph must have Input, Topos Color, and Output
        node_types = {n.node_type for n in graph.nodes.values()}
        assert "topos_color" in node_types, f"Expected topos_color in default graph: {node_types}"
        assert len(graph.nodes) == 3
        assert len(graph.connections) == 2
