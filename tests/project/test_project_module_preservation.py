"""
Tests for .topos module data preservation (Phase B).

Ensures that saving from one product preserves module data that belongs
to other products (e.g., saving from topos-color does not delete
edit/timeline_sequences.json or composite/graphs.json).
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile

import pytest

from src.shared.project.manifest import ToposManifest
from src.shared.project.payload_cache import ProjectArchivePayload
from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_io import ToposProjectIO, ToposProjectIOConfig


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _make_topos_zip(
    path: str,
    project: ToposProject | None = None,
    *,
    extra_modules: dict[str, dict] | None = None,
    extra_binary: dict[str, bytes] | None = None,
    manifest_overrides: dict | None = None,
) -> str:
    """
    Create a minimal .topos ZIP at *path* for testing.

    Returns the path.
    """
    if project is None:
        project = ToposProject.create_single_asset(
            name="Test",
            asset_path="/tmp/test.jpg",
            media_type="image",
        )

    manifest_data = ToposManifest.create(
        project_id=project.project_id,
        project_mode=project.project_mode,
        features=project.features,
        primary_asset_id=project.primary_asset_id,
    ).to_dict()
    if manifest_overrides:
        manifest_data.update(manifest_overrides)

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest_data))
        zf.writestr("project.json", json.dumps(project.to_dict()))

        if extra_modules:
            for arc_path, data in extra_modules.items():
                zf.writestr(arc_path, json.dumps(data))

        if extra_binary:
            for arc_path, raw in extra_binary.items():
                zf.writestr(arc_path, raw)

    return path


def _list_zip_entries(path: str) -> list[str]:
    """Return sorted list of names inside a ZIP."""
    with zipfile.ZipFile(path, "r") as zf:
        return sorted(zf.namelist())


def _read_zip_json(path: str, arc_name: str) -> dict:
    """Read and parse a JSON entry from a ZIP."""
    with zipfile.ZipFile(path, "r") as zf:
        return json.loads(zf.read(arc_name))


def _read_zip_bytes(path: str, arc_name: str) -> bytes:
    """Read raw bytes from a ZIP entry."""
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read(arc_name)


# -----------------------------------------------------------------------
# ProjectArchivePayload unit tests
# -----------------------------------------------------------------------

class TestProjectArchivePayload:
    """Unit tests for ProjectArchivePayload."""

    def test_read_from_zip_captures_all_entries(self, tmp_path):
        """All JSON and binary entries in a ZIP are captured."""
        zf_path = str(tmp_path / "test.topos")
        project = ToposProject.create_single_asset("T", "/tmp/a.jpg", media_type="image")

        _make_topos_zip(
            zf_path,
            project,
            extra_modules={
                "edit/timeline_sequences.json": {"seq1": {"fps": 24}},
                "composite/graphs.json": {"nodes": [1, 2, 3]},
            },
            extra_binary={
                "assets/baked/thumb.dat": b"\x89PNG",
            },
        )

        with zipfile.ZipFile(zf_path, "r") as zf:
            payload = ProjectArchivePayload.read_from_zip(zf)

        assert "seq1" in payload.module_payloads["edit/timeline_sequences.json"]
        assert "nodes" in payload.module_payloads["composite/graphs.json"]
        assert payload.unknown_files["assets/baked/thumb.dat"] == b"\x89PNG"

    def test_read_from_empty_zip(self, tmp_path):
        """An empty ZIP produces an empty payload."""
        zf_path = str(tmp_path / "empty.topos")
        with zipfile.ZipFile(zf_path, "w") as zf:
            pass  # no entries

        with zipfile.ZipFile(zf_path, "r") as zf:
            payload = ProjectArchivePayload.read_from_zip(zf)

        assert payload.manifest == {}
        assert payload.project_json == {}
        assert payload.module_payloads == {}
        assert payload.unknown_files == {}

    def test_load_from_nonexistent_file(self):
        """Loading from a non-existent path returns empty payload."""
        payload = ProjectArchivePayload.load_from_file("/no/such/file.topos")
        assert payload.manifest == {}

    def test_set_module_overwrites(self):
        """set_module adds a new or overwrites an existing module."""
        payload = ProjectArchivePayload()
        payload.set_module("color/color_stacks.json", {"stacks": {}})
        assert payload.get_module("color/color_stacks.json") == {"stacks": {}}

        payload.set_module("color/color_stacks.json", {"stacks": {"new": True}})
        assert payload.get_module("color/color_stacks.json") == {"stacks": {"new": True}}

    def test_set_module_moves_from_unknown(self):
        """If a path was in unknown_files, set_module removes it."""
        payload = ProjectArchivePayload()
        payload.unknown_files["future.json"] = b'{"x":1}'
        payload.set_module("future.json", {"x": 2})
        assert "future.json" not in payload.unknown_files
        assert payload.get_module("future.json") == {"x": 2}

    def test_remove_module(self):
        """remove_module drops from both dicts."""
        payload = ProjectArchivePayload()
        payload.set_module("edit/media_pool.json", {"a": 1})
        payload.remove_module("edit/media_pool.json")
        assert payload.get_module("edit/media_pool.json") is None

    def test_unknown_json_file_parsed(self, tmp_path):
        """Unknown .json files are parsed into module_payloads."""
        zf_path = str(tmp_path / "test.topos")
        with zipfile.ZipFile(zf_path, "w") as zf:
            zf.writestr("future_feature.json", json.dumps({"cool": True}))

        with zipfile.ZipFile(zf_path, "r") as zf:
            payload = ProjectArchivePayload.read_from_zip(zf)

        assert payload.module_payloads["future_feature.json"] == {"cool": True}

    def test_corrupt_json_falls_back_to_bytes(self, tmp_path):
        """Corrupt .json files are preserved as raw bytes."""
        zf_path = str(tmp_path / "test.topos")
        with zipfile.ZipFile(zf_path, "w") as zf:
            zf.writestr("broken.json", "not valid json{{{")

        with zipfile.ZipFile(zf_path, "r") as zf:
            payload = ProjectArchivePayload.read_from_zip(zf)

        assert payload.unknown_files["broken.json"] == b"not valid json{{{"

    def test_write_to_zip_roundtrip(self, tmp_path):
        """Payload round-trips through write -> read."""
        zf_path = str(tmp_path / "rt.topos")
        payload = ProjectArchivePayload()
        payload.set_manifest({"format": "topos_project_archive", "project_id": "abc"})
        payload.set_project_json({"name": "Test"})
        payload.set_module("color/color_stacks.json", {"s1": {}})
        payload.unknown_files["assets/thumb.png"] = b"\x89PNG"

        with zipfile.ZipFile(zf_path, "w") as zf:
            payload.write_to_zip(zf)

        with zipfile.ZipFile(zf_path, "r") as zf:
            loaded = ProjectArchivePayload.read_from_zip(zf)

        assert loaded.manifest["project_id"] == "abc"
        assert loaded.project_json["name"] == "Test"
        assert loaded.module_payloads["color/color_stacks.json"] == {"s1": {}}
        assert loaded.unknown_files["assets/thumb.png"] == b"\x89PNG"


# -----------------------------------------------------------------------
# Integration: ToposProjectIO.save() preserves unknown modules
# -----------------------------------------------------------------------

class TestToposProjectIOPreservation:
    """Verify that ToposProjectIO.save() preserves unknown module data."""

    def _base_project(self, tmp_path):
        """Create a base project with a .topos file containing all modules."""
        project = ToposProject.create_single_asset("Multi", "/tmp/img.jpg")
        zf_path = str(tmp_path / "multi.topos")

        edit_timeline = {
            "seq-001": {
                "name": "Main Edit",
                "fps": 24,
                "tracks": [
                    {"type": "video", "clips": [{"id": "c1", "start": 0, "end": 100}]}
                ],
            }
        }
        composite = {
            "comp-001": {
                "name": "Comp A",
                "nodes": [
                    {"id": "n1", "type": "blend", "params": {"opacity": 0.5}},
                    {"id": "n2", "type": "transform", "params": {"x": 10}},
                ],
            }
        }
        deliver = {"preset-001": {"name": "H264 Master", "format": "h264"}}
        future_data = {"version": 2, "custom_markers": [1, 2, 3]}

        _make_topos_zip(
            zf_path,
            project,
            extra_modules={
                "edit/timeline_sequences.json": edit_timeline,
                "composite/graphs.json": composite,
                "deliver/presets.json": deliver,
                "future_feature.json": future_data,
            },
            extra_binary={
                "assets/thumbnails/thumb.jpg": b"\xff\xd8\xff\xe0",
            },
        )
        return zf_path, project, edit_timeline, composite, deliver, future_data

    def test_color_save_preserves_edit_timeline(self, tmp_path):
        """Saving color data preserves edit/timeline_sequences.json."""
        zf_path, project, edit_timeline, _, _, _ = self._base_project(tmp_path)

        # Save only color data
        color_stacks = {project.primary_color_stack_id: {
            "stack_id": project.primary_color_stack_id,
            "layers": [{"grading_params": {"exposure": 0.5}}],
        }}

        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data=color_stacks,
        )
        assert result is True

        # edit timeline must be preserved
        preserved = _read_zip_json(zf_path, "edit/timeline_sequences.json")
        assert preserved == edit_timeline

        # color data must be written
        color = _read_zip_json(zf_path, "color/color_stacks.json")
        assert "layers" in color[project.primary_color_stack_id]

    def test_color_save_preserves_composite_graphs(self, tmp_path):
        """Saving color data preserves composite/graphs.json."""
        zf_path, project, _, composite, _, _ = self._base_project(tmp_path)

        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data={project.primary_color_stack_id: {"layers": []}},
        )
        assert result is True

        preserved = _read_zip_json(zf_path, "composite/graphs.json")
        assert preserved == composite

    def test_composite_save_preserves_edit_timeline(self, tmp_path):
        """Saving composite data preserves edit/timeline_sequences.json."""
        zf_path, project, edit_timeline, _, _, _ = self._base_project(tmp_path)

        new_composite = {"comp-002": {"name": "Comp B", "nodes": []}}
        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_compositions_data=new_composite,
        )
        assert result is True

        preserved = _read_zip_json(zf_path, "edit/timeline_sequences.json")
        assert preserved == edit_timeline
        actual_comp = _read_zip_json(zf_path, "composite/graphs.json")
        assert actual_comp == new_composite

    def test_unknown_future_feature_preserved_byte_for_byte(self, tmp_path):
        """Unknown module files (future_feature.json) are preserved exactly."""
        zf_path, project, _, _, _, future_data = self._base_project(tmp_path)

        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data={project.primary_color_stack_id: {"layers": []}},
        )
        assert result is True

        preserved = _read_zip_json(zf_path, "future_feature.json")
        assert preserved == future_data

    def test_binary_assets_preserved(self, tmp_path):
        """Binary assets (thumbnails, etc.) are preserved."""
        zf_path, project, _, _, _, _ = self._base_project(tmp_path)

        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data={project.primary_color_stack_id: {"layers": []}},
        )
        assert result is True

        raw = _read_zip_bytes(zf_path, "assets/thumbnails/thumb.jpg")
        assert raw == b"\xff\xd8\xff\xe0"

    def test_manifest_updated_after_save(self, tmp_path):
        """manifest.json gets updated product metadata and timestamps on save."""
        zf_path, project, _, _, _, _ = self._base_project(tmp_path)

        old_mode = os.environ.get("TOPOS_PRODUCT_MODE")
        os.environ["TOPOS_PRODUCT_MODE"] = "topos-color"
        try:
            result = ToposProjectIO.save(
                project=project,
                filepath=zf_path,
                full_color_stacks_data={project.primary_color_stack_id: {"layers": []}},
            )
        finally:
            if old_mode is None:
                os.environ.pop("TOPOS_PRODUCT_MODE", None)
            else:
                os.environ["TOPOS_PRODUCT_MODE"] = old_mode

        assert result is True
        manifest = _read_zip_json(zf_path, "manifest.json")
        assert manifest["format"] == "topos_project_archive"
        assert manifest["project_id"] == project.project_id
        assert "modified_at" in manifest
        assert manifest["last_saved_by"] == "topos-color"
        assert set(manifest["present_modules"]) >= {
            "color",
            "edit",
            "composite",
            "deliver",
        }

    def test_empty_module_payload_overwrites_existing_module(self, tmp_path):
        """Passing an empty dict is an intentional module clear, not preserve-old."""
        zf_path, project, _, composite, _, _ = self._base_project(tmp_path)

        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data={},
        )
        assert result is True

        assert _read_zip_json(zf_path, "color/color_stacks.json") == {}
        assert _read_zip_json(zf_path, "composite/graphs.json") == composite

    def test_empty_project_saves_correctly(self, tmp_path):
        """A brand-new project (no existing file) saves correctly."""
        zf_path = str(tmp_path / "new_project.topos")
        project = ToposProject.create_single_asset("New", "/tmp/img.jpg")

        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data={project.primary_color_stack_id: {"layers": []}},
        )
        assert result is True
        assert os.path.exists(zf_path)

        entries = _list_zip_entries(zf_path)
        assert "manifest.json" in entries
        assert "project.json" in entries
        assert "color/color_stacks.json" in entries

    def test_roundtrip_preserves_edit_data(self, tmp_path):
        """Full round-trip: load -> modify color -> save -> reload -> verify edit unchanged."""
        zf_path, project, edit_timeline, _, _, _ = self._base_project(tmp_path)

        # 1. Load
        loaded_project, full_data = ToposProjectIO.load(zf_path)
        assert loaded_project is not None

        # 2. Modify color data
        new_color = {
            project.primary_color_stack_id: {
                "stack_id": project.primary_color_stack_id,
                "layers": [{"grading_params": {"exposure": 2.0, "contrast": 1.5}}],
                "raw_params": {"enabled": True},
            }
        }

        # 3. Save
        result = ToposProjectIO.save(
            project=loaded_project,
            filepath=zf_path,
            full_color_stacks_data=new_color,
        )
        assert result is True

        # 4. Reload
        loaded2, full_data2 = ToposProjectIO.load(zf_path)
        assert loaded2 is not None

        # 5. Verify edit timeline is still intact
        assert full_data2 is not None
        assert "timeline_sequences" in full_data2
        assert full_data2["timeline_sequences"] == edit_timeline

        # 6. Verify color data was updated
        assert "color_stacks" in full_data2
        cs = full_data2["color_stacks"]
        stack = cs[project.primary_color_stack_id]
        assert stack["layers"][0]["grading_params"]["exposure"] == 2.0
        assert stack["layers"][0]["grading_params"]["contrast"] == 1.5

    def test_save_preserves_deliver_presets(self, tmp_path):
        """Saving color data preserves deliver/presets.json."""
        zf_path, project, _, _, deliver, _ = self._base_project(tmp_path)

        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data={project.primary_color_stack_id: {"layers": []}},
        )
        assert result is True

        preserved = _read_zip_json(zf_path, "deliver/presets.json")
        assert preserved == deliver

    def test_multiple_saves_preserve_data(self, tmp_path):
        """Repeated saves do not lose data."""
        zf_path, project, edit_timeline, composite, deliver, future = self._base_project(tmp_path)

        # Save 3 times with different color data each time
        for i in range(3):
            color = {
                project.primary_color_stack_id: {
                    "layers": [{"grading_params": {"iteration": i}}],
                }
            }
            result = ToposProjectIO.save(
                project=project,
                filepath=zf_path,
                full_color_stacks_data=color,
            )
            assert result is True

        # All non-color modules should survive all 3 saves
        assert _read_zip_json(zf_path, "edit/timeline_sequences.json") == edit_timeline
        assert _read_zip_json(zf_path, "composite/graphs.json") == composite
        assert _read_zip_json(zf_path, "deliver/presets.json") == deliver
        assert _read_zip_json(zf_path, "future_feature.json") == future


# -----------------------------------------------------------------------
# Edge cases
# -----------------------------------------------------------------------

class TestPreservationEdgeCases:
    """Edge cases for module preservation."""

    def test_save_to_path_with_spaces(self, tmp_path):
        """Paths with spaces work correctly."""
        spaced_dir = tmp_path / "my projects" / "2026 work"
        spaced_dir.mkdir(parents=True, exist_ok=True)
        zf_path = str(spaced_dir / "test.topos")

        project = ToposProject.create_single_asset("Space", "/tmp/img.jpg")
        _make_topos_zip(
            zf_path,
            project,
            extra_modules={"edit/timeline_sequences.json": {"seq": {"fps": 30}}},
        )

        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data={project.primary_color_stack_id: {"layers": []}},
        )
        assert result is True
        assert _read_zip_json(zf_path, "edit/timeline_sequences.json") == {"seq": {"fps": 30}}

    def test_overwrite_single_module_preserves_rest(self, tmp_path):
        """Overwriting just one module (e.g., deliver presets) preserves everything else."""
        zf_path = str(tmp_path / "test.topos")
        project = ToposProject.create_single_asset("T", "/tmp/img.jpg")

        color_data = {project.primary_color_stack_id: {"layers": [{"exposure": 1.0}]}}
        edit_data = {"seq1": {"clips": []}}

        _make_topos_zip(
            zf_path,
            project,
            extra_modules={
                "color/color_stacks.json": color_data,
                "edit/timeline_sequences.json": edit_data,
            },
        )

        # Save only deliver presets
        new_deliver = {"p1": {"name": "ProRes"}}
        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            deliver_presets_data=new_deliver,
        )
        assert result is True

        # Color and edit preserved
        assert _read_zip_json(zf_path, "color/color_stacks.json") == color_data
        assert _read_zip_json(zf_path, "edit/timeline_sequences.json") == edit_data
        assert _read_zip_json(zf_path, "deliver/presets.json") == new_deliver

    def test_old_manifest_missing_new_fields_gets_defaults(self, tmp_path):
        """Old manifests missing Phase 3B fields get sensible defaults."""
        zf_path = str(tmp_path / "old.topos")
        project = ToposProject.create_single_asset("Old", "/tmp/img.jpg")

        manifest_data = {
            "format": "topos_project_archive",
            "schema_version": 1,
            "project_id": project.project_id,
            "project_mode": "standalone_image",
            # Missing: last_saved_by, present_modules, etc.
        }

        with zipfile.ZipFile(zf_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest_data))
            zf.writestr("project.json", json.dumps(project.to_dict()))

        # Save should not crash
        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data={project.primary_color_stack_id: {"layers": []}},
        )
        assert result is True

        # Manifest should still be valid
        saved_manifest = _read_zip_json(zf_path, "manifest.json")
        assert saved_manifest["format"] == "topos_project_archive"
        assert saved_manifest["project_id"] == project.project_id

    def test_write_empty_project_then_save_preserves_nothing(self, tmp_path):
        """Saving a new project with no modules then re-saving preserves structure."""
        zf_path = str(tmp_path / "empty.topos")
        project = ToposProject.create_single_asset("E", "/tmp/img.jpg")

        # First save: no modules
        result = ToposProjectIO.save(project=project, filepath=zf_path)
        assert result is True

        # Second save: add color data
        color = {project.primary_color_stack_id: {"layers": [{"exposure": 1.0}]}}
        result = ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            full_color_stacks_data=color,
        )
        assert result is True

        # Verify color data
        cs = _read_zip_json(zf_path, "color/color_stacks.json")
        assert cs == color

    def test_load_replaces_cache_so_removed_modules_do_not_reappear(self, tmp_path):
        """Reloading an updated archive must not read stale cache files."""
        zf_path = str(tmp_path / "cache-refresh.topos")
        project = ToposProject.create_single_asset("Cache", "/tmp/img.jpg")

        _make_topos_zip(
            zf_path,
            project,
            extra_modules={"ai/plans.json": {"plans": [{"id": "old-plan"}]}},
        )
        _loaded, first_data = ToposProjectIO.load(zf_path)
        assert first_data is not None
        assert first_data["ai_plans"]["plans"][0]["id"] == "old-plan"

        # Simulate an updated archive which no longer has any AI module.
        _make_topos_zip(zf_path, project)
        _loaded, second_data = ToposProjectIO.load(zf_path)

        assert second_data is not None
        assert "ai_plans" not in second_data

    def test_load_rejects_archive_members_outside_project_cache(self, tmp_path):
        """A malformed archive must not write files outside .topos_cache."""
        zf_path = str(tmp_path / "unsafe.topos")
        outside_path = tmp_path / "escaped.json"
        project = ToposProject.create_single_asset("Unsafe", "/tmp/img.jpg")

        with zipfile.ZipFile(zf_path, "w") as zf:
            manifest = ToposManifest.create(
                project_id=project.project_id,
                project_mode=project.project_mode,
                features=project.features,
                primary_asset_id=project.primary_asset_id,
            )
            zf.writestr("manifest.json", json.dumps(manifest.to_dict()))
            zf.writestr("project.json", json.dumps(project.to_dict()))
            zf.writestr("../escaped.json", "must not be written")

        loaded, full_data = ToposProjectIO.load(zf_path)

        assert loaded is None
        assert full_data is None
        assert not outside_path.exists()
