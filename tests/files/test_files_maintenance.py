from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.features.files.repository.catalog import FilesCatalog
from src.features.files.repository.locator import FilesDataLocator
from src.features.files.services.maintenance import FilesMaintenance, MaintenanceError, main


@pytest.fixture
def fixture(tmp_path):
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    source = tmp_path / "素材"
    source.mkdir()
    media = source / "photo.txt"
    media.write_bytes(b"source stays untouched")
    library = catalog.register_library(source)
    catalog.upsert_asset(library.library_id, media, size=media.stat().st_size)
    catalog.patch_user_fields(library.library_id, media, rating=5, comment="saved")
    tag = catalog.create_tag(library.library_id, "tag")
    catalog.tag_asset(library.library_id, media, tag)
    collection = catalog.create_collection(library.library_id, "collection")
    catalog.add_to_collection(collection, media)
    trash = catalog.locator.trash_dir(library.library_id)
    trash.mkdir(parents=True, exist_ok=True)
    (trash / "payload").write_bytes(b"recoverable")
    (trash / "empty-directory").mkdir()
    catalog.locator.plugins_state_file.parent.mkdir(parents=True, exist_ok=True)
    catalog.locator.plugins_state_file.write_text('{"enabled":true}', encoding="utf-8")
    yield catalog, media, library
    catalog.close()


def test_backup_restore_complete_new_catalog_and_source_unchanged(fixture, tmp_path):
    catalog, media, library = fixture
    before = (hashlib.sha256(media.read_bytes()).hexdigest(), media.stat().st_mtime_ns)
    backup = FilesMaintenance(catalog).backup()
    manifest = FilesMaintenance.verify(backup)
    assert manifest["counts"]["assets"] == 1
    assert manifest["counts"]["collection_items"] == 1
    destination = tmp_path / "restored" / "files-data"
    FilesMaintenance.restore(backup, destination)
    restored = FilesCatalog(FilesDataLocator(destination))
    try:
        assert restored.asset(library.library_id, media)["rating"] == 5
        assert restored.tags_of(library.library_id, media) == ["tag"]
        assert (restored.locator.trash_dir(library.library_id) / "payload").read_bytes() == b"recoverable"
        assert (restored.locator.trash_dir(library.library_id) / "empty-directory").is_dir()
        assert restored.locator.plugins_state_file.read_text() == '{"enabled":true}'
    finally:
        restored.close()
    assert before == (hashlib.sha256(media.read_bytes()).hexdigest(), media.stat().st_mtime_ns)
    assert not any(name.startswith("cache/") for name in manifest["files"])
    assert not any(name.startswith("backups/") for name in manifest["files"])


@pytest.mark.parametrize("damage", ["digest", "missing", "extra", "traversal", "symlink"])
def test_damaged_backup_is_rejected_without_partial_restore(fixture, tmp_path, damage):
    catalog, _, _ = fixture
    backup = FilesMaintenance(catalog).backup()
    if damage == "digest":
        (backup / "libraries.json").write_text("[]")
    elif damage == "missing":
        (backup / "catalog.db").unlink()
    elif damage == "extra":
        (backup / "unknown").write_bytes(b"x")
    elif damage == "symlink":
        (backup / "libraries.json").unlink()
        (backup / "libraries.json").symlink_to(catalog.locator.libraries_file)
    else:
        path = backup / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["files"]["../outside"] = {"size": 0, "sha256": "x"}
        path.write_text(json.dumps(manifest))
    destination = tmp_path / "new" / "files-data"
    with pytest.raises(MaintenanceError):
        FilesMaintenance.restore(backup, destination)
    assert not destination.exists()


def test_backup_failed_publish_leaves_no_half_snapshot(fixture, monkeypatch):
    catalog, media, _ = fixture
    from src.features.files.transactions import ops

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(ops, "rename_no_replace", fail)
    with pytest.raises(OSError, match="disk full"):
        FilesMaintenance(catalog).backup()
    assert list(catalog.locator.backups_dir.iterdir()) == []
    assert media.exists() and catalog.asset(catalog.libraries()[0].library_id, media)


def test_restore_refuses_nonempty_and_linked_target(fixture, tmp_path):
    catalog, _, _ = fixture
    backup = FilesMaintenance(catalog).backup()
    target = tmp_path / "target" / "files-data"
    target.mkdir(parents=True)
    (target / "keep").write_text("keep")
    with pytest.raises(MaintenanceError):
        FilesMaintenance.restore(backup, target)
    link = tmp_path / "linked" / "files-data"
    link.parent.mkdir()
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(MaintenanceError):
        FilesMaintenance.restore(backup, link)
    assert (target / "keep").read_text() == "keep"


def test_development_reset_archives_and_reinitializes_without_touching_source(fixture):
    catalog, media, _ = fixture
    service = FilesMaintenance(catalog)
    assert service.reset() == {"scope": "files-data", "action": "archive_then_initialize"}
    assert catalog.libraries()
    report = service.reset(apply=True)
    assert not catalog.libraries()
    assert media.read_bytes() == b"source stays untouched"
    assert Path(report["archive"]).is_dir()
    assert FilesMaintenance.verify(Path(report["backup"]))


def test_cli_verify_and_restore_do_not_open_the_live_catalog(fixture, tmp_path, monkeypatch, capsys):
    catalog, _, _ = fixture
    backup = FilesMaintenance(catalog).backup()
    monkeypatch.setattr(FilesCatalog, "open", lambda self: (_ for _ in ()).throw(AssertionError("live catalog opened")))
    assert main(["--config-root", str(tmp_path), "verify", "--backup", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
