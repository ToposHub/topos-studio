"""Second review: malformed external data and staged identity metadata."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.gallery_transfer_service import (
    BUNDLE_MANIFEST_NAME, GalleryTransferService, IMPORT_JOURNAL_NAME,
    IMPORT_TXN_PREFIX,
)
from src.features.color.core.gallery_txn_core import JournalError, read_journal
from src.features.color.core.preset_meta import (
    PresetMeta, load_preset_meta, save_preset_meta,
)


def _package(path: Path, **overrides: object) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as grade:
        grade.writestr("project.json", json.dumps({"layers": [{"name": "new"}]}))
    contents = data.getvalue()
    manifest = {
        "schema": 2, "kind": "topos-bundle",
        "still": {"display_name": "Incoming", "still_id": "S-conflict"},
        "files": [{"name": "Incoming.topos", "role": "grade",
                   "sha256": hashlib.sha256(contents).hexdigest()}],
        "dependencies": [],
    }
    manifest.update(overrides)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Incoming.topos", contents)
        archive.writestr(BUNDLE_MANIFEST_NAME, json.dumps(manifest))


@pytest.mark.parametrize("dependencies", [1, True, "bad", {}, {"bundled": True}, None])
def test_malformed_dependency_container_is_rejected(tmp_path: Path, dependencies: object) -> None:
    path = tmp_path / "bad.toposbundle"
    _package(path, dependencies=dependencies)
    service = GalleryTransferService(GalleryFolderRepository(str(tmp_path)))
    assert not service.inspect_bundle(str(path)).ok


@pytest.mark.parametrize("schema", [0, -1, True, 2])
def test_unsupported_journal_schema_is_quarantined(tmp_path: Path, schema: object) -> None:
    path = tmp_path / "journal.json"
    path.write_text(json.dumps({"schema": schema}))
    with pytest.raises(JournalError):
        read_journal(str(path))


@pytest.mark.parametrize("field,value", [
    ("entries", None), ("entries", {}), ("entries", [None]),
    ("finals", None), ("finals", "bad"), ("finals", [None]),
])
def test_invalid_journal_member_lists_are_quarantined(
        tmp_path: Path, field: str, value: object) -> None:
    path = tmp_path / "journal.json"
    path.write_text(json.dumps({"schema": 1, field: value}))
    with pytest.raises(JournalError):
        read_journal(str(path))


def test_reallocated_identity_without_original_meta_is_installed(tmp_path: Path) -> None:
    root = tmp_path / "Presets"
    folder = root / "User"
    folder.mkdir(parents=True)
    old = folder / "Existing.topos"
    with zipfile.ZipFile(old, "w") as grade:
        grade.writestr("project.json", json.dumps({"layers": [{"name": "old"}]}))
    assert save_preset_meta(str(old), PresetMeta(still_id="S-conflict"))
    path = tmp_path / "new.toposbundle"
    _package(path)
    service = GalleryTransferService(GalleryFolderRepository(str(root)))
    result = service.import_bundle(str(path), str(folder))
    assert result.succeeded, result.warnings
    meta = load_preset_meta(str(folder / "Incoming.topos"))
    assert meta is not None
    assert meta.still_id and meta.still_id != "S-conflict"
    assert meta.extra_fields["origin_still_id"] == "S-conflict"


@pytest.mark.parametrize("outside", [True, False])
def test_import_recovery_quarantines_outside_paths_or_unknown_states(
        tmp_path: Path, outside: bool) -> None:
    root = tmp_path / "Presets"
    folder = root / "User"
    folder.mkdir(parents=True)
    victim = (tmp_path if outside else folder) / "Keep.topos"
    victim.write_bytes(b"KEEP")
    txn = folder / (IMPORT_TXN_PREFIX + "bad")
    txn.mkdir()
    (txn / IMPORT_JOURNAL_NAME).write_text(json.dumps({
        "schema": 1, "state": "installing" if outside else "unknown",
        "finals": [{"path": str(victim), "size": 4}]}))
    service = GalleryTransferService(GalleryFolderRepository(str(root)))
    service._recover_import_txns(str(folder))
    assert victim.read_bytes() == b"KEEP"
    assert txn.is_dir()


def _dependency_package(path: Path, name: object = "look.cube") -> None:
    contents = b"LUT CONTENTS"
    _package(path, dependencies=[{
        "name": name, "bundled": True, "source": "/old/look.cube",
        "archive": "assets/luts/look.cube",
        "sha256": hashlib.sha256(contents).hexdigest(),
    }])
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("assets/luts/look.cube", contents)


@pytest.mark.parametrize("name", ["../escape.cube", ["bad"], "dir/look.cube"])
def test_dependency_install_name_is_validated(tmp_path: Path, name: object) -> None:
    path = tmp_path / "bad.toposbundle"
    _dependency_package(path, name)
    service = GalleryTransferService(GalleryFolderRepository(str(tmp_path)))
    assert not service.inspect_bundle(str(path)).ok


def test_dependency_directory_cannot_be_symlink_outside_library(tmp_path: Path) -> None:
    root = tmp_path / "Presets"
    folder = root / "User"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "LUTs").symlink_to(outside, target_is_directory=True)
    archive = tmp_path / "look.toposbundle"
    _dependency_package(archive)
    service = GalleryTransferService(GalleryFolderRepository(str(root)))
    result = service.import_bundle(str(archive), str(folder))
    assert not result.succeeded
    assert result.reason == "dependency_path_escape"
    assert list(outside.iterdir()) == []
    assert list(folder.iterdir()) == []
