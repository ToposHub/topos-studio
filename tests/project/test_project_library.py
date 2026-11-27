"""Tests for the video-mode project browser data layer."""

from __future__ import annotations

import json
import shutil
import zipfile

import pytest
from pathlib import Path

from src.shared.project.library import (
    delete_project,
    duplicate_project,
    ensure_project_path,
    get_project_backup_dir,
    list_project_backups,
    normalize_project_name,
    read_project_meta,
    rename_project,
    restore_project_backup,
    scan_video_projects,
)
import src.shared.project.library as project_library
from src.shared.config.config_manager import ConfigManager


def _write_project(path, *, mode: str, asset_path: str | None = None, project_id: str | None = None) -> None:
    manifest = {
        "project_id": project_id or Path(path).stem,
        "project_mode": mode,
        "created_at": 1.0,
        "modified_at": 2.0,
    }
    project = {
        "name": "Demo Project",
        "assets": {},
        "timelines": {},
    }
    if asset_path:
        project["assets"]["asset-1"] = {
            "asset_id": "asset-1",
            "path": asset_path,
            "media_type": "video",
        }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("project.json", json.dumps(project))


def test_project_meta_resolves_preview_asset_relative_to_project(tmp_path):
    project_path = tmp_path / "Demo.topos"
    _write_project(project_path, mode="edit_project", asset_path="media/demo.mp4")

    meta = read_project_meta(str(project_path))

    assert meta is not None
    assert meta.name == "Demo Project"
    assert meta.preview_path == str(tmp_path / "media" / "demo.mp4")


def test_scan_video_projects_excludes_image_projects(tmp_path):
    _write_project(tmp_path / "Video.topos", mode="edit_project")
    _write_project(tmp_path / "Image.topos", mode="standalone_image")

    class Config:
        def get(self, key, default=None):
            return str(tmp_path) if key == "project_library_directory" else default

    projects = scan_video_projects(Config())

    assert [project.name for project in projects] == ["Demo Project"]
    assert all(project.project_mode == "edit_project" for project in projects)


def test_scan_hides_backup_and_repeated_extension_projects(tmp_path):
    _write_project(tmp_path / "Demo.topos", mode="edit_project")
    _write_project(tmp_path / "Demo.topos.topos", mode="edit_project")
    backup_dir = get_project_backup_dir(tmp_path / "Demo.topos")
    _write_project(backup_dir / "Demo_20260808_120000_000000.topos", mode="edit_project")

    class Config:
        def get(self, key, default=None):
            return str(tmp_path) if key == "project_library_directory" else default

    projects = scan_video_projects(Config())
    assert [project.path for project in projects] == [str(tmp_path / "Demo.topos")]


def test_legacy_sidecar_backup_is_migrated_and_can_be_restored(tmp_path):
    project_path = tmp_path / "Demo.topos"
    legacy_path = tmp_path / "Demo.topos.bak"
    _write_project(project_path, mode="edit_project")
    shutil.copy2(project_path, legacy_path)

    backups = list_project_backups(project_path)

    assert len(backups) == 1
    assert backups[0].path.startswith(str(tmp_path / "backups"))
    assert not legacy_path.exists()

    project_path.write_bytes(b"not the current archive")
    assert restore_project_backup(project_path, backups[0].path)
    assert zipfile.is_zipfile(project_path)


def test_scan_video_projects_includes_registered_and_recent_locations(tmp_path):
    default_dir = tmp_path / "default"
    custom_dir = tmp_path / "custom"
    recent_dir = tmp_path / "recent"
    default_dir.mkdir()
    custom_dir.mkdir()
    recent_dir.mkdir()
    _write_project(default_dir / "Default.topos", mode="edit_project")
    _write_project(custom_dir / "Custom.topos", mode="edit_project")
    recent_path = recent_dir / "Recent.topos"
    _write_project(recent_path, mode="edit_project")

    class Config:
        def get(self, key, default=None):
            values = {
                "project_library_directory": str(default_dir),
                "project_library_directories": [str(custom_dir)],
                "recent_projects": [str(recent_path)],
            }
            return values.get(key, default)

    projects = scan_video_projects(Config())
    assert len(projects) == 3
    assert {project.path for project in projects} == {
        str(default_dir / "Default.topos"),
        str(custom_dir / "Custom.topos"),
        str(recent_path),
    }


def test_rename_and_duplicate_preserve_archive_data(tmp_path):
    source = tmp_path / "Original.topos"
    _write_project(source, mode="edit_project")

    renamed = rename_project(source, "Renamed")
    renamed_meta = read_project_meta(renamed)
    assert renamed_meta is not None
    assert renamed_meta.name == "Renamed"

    copied = duplicate_project(renamed)
    copied_meta = read_project_meta(copied)
    assert copied_meta is not None
    assert copied_meta.name == "Renamed Copy"


def test_rename_migrates_timestamped_backups_to_the_new_project_name(tmp_path):
    source = tmp_path / "Original.topos"
    _write_project(source, mode="edit_project")
    backup_dir = get_project_backup_dir(source)
    old_backup = backup_dir / "Original_20260812_040600_000001.topos"
    _write_project(old_backup, mode="edit_project", project_id="Original")

    renamed = rename_project(source, "Renamed")

    backups = list_project_backups(renamed)
    assert [Path(item.path).name for item in backups] == [
        "Renamed_20260812_040600_000001.topos"
    ]
    assert not old_backup.exists()


def test_backup_list_recovers_orphaned_backups_from_a_previous_rename(tmp_path):
    source = tmp_path / "Original.topos"
    _write_project(source, mode="edit_project", project_id="stable-project-id")
    renamed = Path(rename_project(source, "Renamed"))
    backup_dir = get_project_backup_dir(renamed)
    orphan = backup_dir / "Original_20260812_040600_000001.topos"
    _write_project(orphan, mode="edit_project", project_id="stable-project-id")

    backups = list_project_backups(renamed)

    assert len(backups) == 1
    assert Path(backups[0].path).name.startswith("Renamed_")
    assert not orphan.exists()


def test_delete_project_removes_only_its_own_archived_backups(tmp_path, monkeypatch):
    source = tmp_path / "Delete Me.topos"
    other = tmp_path / "Keep Me.topos"
    _write_project(source, mode="edit_project")
    _write_project(other, mode="edit_project")
    backup_dir = get_project_backup_dir(source)
    own_backup = backup_dir / "Delete Me_20260812_040600_000001.topos"
    other_backup = backup_dir / "Keep Me_20260812_040600_000001.topos"
    _write_project(own_backup, mode="edit_project", project_id="Delete Me")
    _write_project(other_backup, mode="edit_project", project_id="Keep Me")

    def _delete_for_test(target):
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    monkeypatch.setattr(project_library, "_move_to_trash", _delete_for_test)
    moved = delete_project(source)

    assert str(source) in moved
    assert str(own_backup) in moved
    assert not source.exists()
    assert not own_backup.exists()
    assert other.exists()
    assert other_backup.exists()


def test_shared_backup_directory_isolated_by_stable_project_id(tmp_path, monkeypatch):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    archive_dir = tmp_path / "archive"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "Untitled Project.topos"
    second = second_dir / "Untitled Project.topos"
    _write_project(first, mode="edit_project", project_id="first-id")
    _write_project(second, mode="edit_project", project_id="second-id")
    archive_dir.mkdir()
    first_backup = archive_dir / "Untitled Project_20260812_040600_000001.topos"
    second_backup = archive_dir / "Untitled Project_20260812_040600_000002.topos"
    _write_project(first_backup, mode="edit_project", project_id="first-id")
    _write_project(second_backup, mode="edit_project", project_id="second-id")

    class Config:
        def get(self, key, default=None):
            return str(archive_dir) if key == "backup_directory" else default

    config = Config()
    assert [Path(item.path) for item in list_project_backups(first, config)] == [first_backup]
    assert [Path(item.path) for item in list_project_backups(second, config)] == [second_backup]

    monkeypatch.setattr(project_library, "_move_to_trash", lambda target: target.unlink())
    delete_project(first, config)
    assert not first_backup.exists()
    assert second_backup.exists()


def test_restore_preserves_current_project_and_keeps_the_current_name(tmp_path):
    project_path = tmp_path / "Current Name.topos"
    _write_project(project_path, mode="edit_project")
    backup_dir = get_project_backup_dir(project_path)
    backup_path = backup_dir / "Current Name_20260812_040600_000001.topos"
    _write_project(backup_path, mode="edit_project", project_id="Current Name")

    assert restore_project_backup(project_path, backup_path)

    restored = read_project_meta(str(project_path))
    assert restored is not None
    assert restored.name == "Current Name"
    backups = list_project_backups(project_path)
    assert len(backups) == 2
    assert any("before_restore" in Path(item.path).name for item in backups)


def test_project_names_and_paths_have_one_extension():
    assert normalize_project_name("test05.topos.topos") == "test05"
    assert ensure_project_path("/tmp/test05.topos.topos") == "/tmp/test05.topos"


def test_offline_library_is_not_created_and_remains_excluded_until_added(tmp_path):
    config = ConfigManager(tmp_path / "config.json")
    library = tmp_path / "local"
    library.mkdir()
    offline = tmp_path / "external" / "library"
    recent = str(offline / "Recent.topos")
    config.set("project_library_directory", str(library), auto_save=False)
    config.set("recent_projects", [recent], auto_save=True)

    _projects, locations = project_library.scan_project_libraries(config)
    assert {location.path: location.available for location in locations} == {
        str(library): True,
        str(offline): False,
    }
    assert not offline.exists()

    project_library.remove_project_library_dir(config, offline)
    project_library.register_project_library_dir(config, offline)
    assert project_library.get_project_library_dirs(ConfigManager(config.config_path)) == [library]
    assert config.get("recent_projects") == [recent]

    project_library.register_project_library_dir(config, offline, explicit=True)
    assert project_library.get_project_library_dirs(ConfigManager(config.config_path)) == [library, offline]


def test_media_inspection_checks_relative_embedded_and_generated_assets_without_writes(tmp_path):
    path = tmp_path / "Media.topos"
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "clip.mov").write_bytes(b"video")
    assets = {
        "relative": {"path": "/former/clip.mov", "path_relative": "media/clip.mov"},
        "embedded": {"path": "", "resource_uri": "topos-res://embedded"},
        "legacy-resource": {"path": "/former/embedded.mov"},
        "generated": {"path": "topos://generator/solid"},
        "broken": {"path": "/former/missing.mov", "display_name": "Missing clip"},
    }
    entries = [
        {
            "resource_id": name,
            "asset_id": name,
            "kind": "video",
            "archive_path": f"resources/{name}.mov",
            "size_bytes": 3,
            "sha256": "a" * 64,
            "roles": ["source_media"],
        }
        for name in ["embedded", "legacy-resource"]
    ]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.json", json.dumps({"assets": {"fallback": {"path": "/missing"}}}))
        archive.writestr("edit/media_pool.json", json.dumps(assets))
        archive.writestr("resources/resources.json", json.dumps({"resources": entries}))
        for entry in entries:
            archive.writestr(entry["archive_path"], b"abc")
    original = path.read_bytes()

    assert project_library.inspect_project_media(str(path)) == (5, ("Missing clip",))
    assert path.read_bytes() == original
    assert {entry.name for entry in tmp_path.iterdir()} == {"Media.topos", "media"}


def test_backup_copy_keeps_relative_media_and_does_not_touch_original(tmp_path):
    original = tmp_path / "Original.topos"
    _write_project(original, mode="edit_project")
    backups = tmp_path / "backups"
    backups.mkdir()
    backup = backups / "Original_20260812_040600.topos"
    _write_project(backup, mode="edit_project", asset_path="clip.mov", project_id="Original")
    (tmp_path / "clip.mov").touch()
    with zipfile.ZipFile(backup, "a") as archive:
        archive.writestr("extensions/unknown.dat", b"preserve this")
    snapshot = original.read_bytes(), backup.read_bytes()
    existing = tmp_path / "Recovered.topos"
    existing.write_bytes(b"existing")

    recovered = duplicate_project(backup, "Recovered", destination_directory=original.parent)

    assert recovered == str(tmp_path / "Recovered 2.topos")
    assert existing.read_bytes() == b"existing"
    assert (original.read_bytes(), backup.read_bytes()) == snapshot
    assert project_library.inspect_project_media(recovered) == (1, ())
    with zipfile.ZipFile(recovered) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        project = json.loads(archive.read("project.json"))
        assert manifest["project_id"] == project["project_id"] != "Original"
        assert archive.read("extensions/unknown.dat") == b"preserve this"


def test_invalid_backup_cannot_replace_project_or_leave_a_partial_copy(tmp_path):
    original = tmp_path / "Original.topos"
    _write_project(original, mode="edit_project")
    original_bytes = original.read_bytes()
    backup = tmp_path / "Invalid.topos"
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr("project.json", "{}")
    assert not restore_project_backup(original, backup)
    assert original.read_bytes() == original_bytes
    with pytest.raises(ValueError):
        duplicate_project(backup, "Recovered")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["Invalid.topos", "Original.topos"]
