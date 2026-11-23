from __future__ import annotations

import json
from pathlib import Path
import stat
from zipfile import ZipFile, ZipInfo

import pytest

from src.features.files.repository.catalog import FilesCatalog
from src.features.files.repository.locator import FilesDataLocator
from src.features.files.services.domain import FilesMetadataService
from src.features.files.services.library_transfer import FilesLibraryTransfer
from src.shared.project.image_sidecar import resolve_image_sidecar


@pytest.fixture
def transfer(tmp_path: Path):
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    yield catalog, FilesLibraryTransfer(catalog, FilesMetadataService(catalog))
    catalog.close()


def test_whole_library_round_trip(transfer, tmp_path: Path) -> None:
    catalog, exchange = transfer
    root = tmp_path / "source"
    (root / "nested" / "empty").mkdir(parents=True)
    image = root / "nested" / "照片.jpg"
    image.write_bytes(b"original media")
    loose = root / "notes.txt"
    loose.write_text("notes")
    catalog.upsert_asset("", loose)
    catalog.patch_user_fields("", loose, comment="before registering")
    (root / ".DS_Store").write_text("machine metadata")
    sidecar = image.with_suffix(".topos")
    with ZipFile(sidecar, "w") as project:
        project.writestr("manifest.json", json.dumps({"project_mode": "standalone_image"}))
        project.writestr("project.json", json.dumps({"assets": {"source": {
            "path": str(image), "path_relative": image.name,
        }}}))
    record = catalog.register_library(root, "素材库")
    catalog.upsert_asset(record.library_id, image)
    catalog.patch_user_fields(record.library_id, image, rating=4, color=2, flag=1, comment="共享备注")
    parent_tag = catalog.create_tag(record.library_id, "旅行", 4)
    child_tag = catalog.create_tag(record.library_id, "冬日", 5, parent_tag)
    catalog.tag_asset(record.library_id, image, child_tag)
    catalog.create_collection(record.library_id, "精选", 3, paths=[str(image), str(loose)])
    catalog.create_collection("topos:color-grade-sets", "其他调色集", paths=[str(image)])

    package = exchange.export_library(record.library_id, str(tmp_path / "shared.toposlibrary"))
    with ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert str(root) not in json.dumps(manifest, ensure_ascii=False)
        assert "files/.DS_Store" not in archive.namelist()
    with pytest.raises(FileExistsError):
        exchange.export_library(record.library_id, str(package))
    with pytest.raises(ValueError, match="源素材库之外"):
        exchange.export_library(record.library_id, str(root / "recursive.toposlibrary"))

    receiver = tmp_path / "receiver"
    receiver.mkdir()
    existing = receiver / "素材库"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep")
    imported = exchange.import_library(str(package), str(receiver))
    assert imported.name == record.name
    assert imported.library_id != record.library_id
    destination = Path(imported.root)
    assert destination == receiver / "素材库 (2)"
    assert (existing / "keep.txt").read_text() == "keep"
    imported_image = destination / "nested" / image.name
    imported_sidecar = imported_image.with_suffix(".topos")
    assert imported_image.read_bytes() == image.read_bytes()
    assert imported_sidecar.read_bytes() == sidecar.read_bytes()
    assert resolve_image_sidecar(str(imported_image)) == (str(imported_sidecar), True)
    assert (destination / "nested" / "empty").is_dir()
    fields = catalog.asset(imported.library_id, imported_image)
    assert fields is not None
    assert (fields["rating"], fields["color"], fields["flag"], fields["comment"]) == (4, 2, 1, "共享备注")
    assert catalog.tags_of(imported.library_id, imported_image) == ["冬日"]
    assert catalog.asset(imported.library_id, destination / loose.name)["comment"] == "before registering"
    tag, = catalog.snapshot_relations(imported.library_id, imported_image)["tags"]
    assert tag["color"] == 5 and tag["parent_id"] is not None
    exported_collection = catalog.collection_id_for_name(imported.library_id, "精选")
    assert catalog.collection_member_paths(exported_collection) == [str(imported_image), str(destination / loose.name)]
    assert catalog.collection_id_for_name(imported.library_id, "其他调色集") is None


def _make_package(path: Path, members: list[str | ZipInfo], **fields: object) -> None:
    with ZipFile(path, "w") as archive:
        manifest = {"format": "topos.asset-library", "version": 1, "name": "Example",
                    "assets": {}, "tags": [], "collections": []}
        archive.writestr("manifest.json", json.dumps({**manifest, **fields}))
        for entry in members:
            archive.writestr(entry, b"data")


@pytest.mark.parametrize("paths", [
    ["files/../outside.jpg"],
    ["files/a\\escape.jpg"],
    ["files/Folder/photo.jpg", "files/folder/note.txt"],
    ["files/photo.jpg", "files/photo.jpg/incompatible.txt"],
])
def test_unsafe_archive_leaves_receiver_empty(transfer, tmp_path: Path, paths: list[str]) -> None:
    catalog, exchange = transfer
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    package = tmp_path / "bad.toposlibrary"
    _make_package(package, paths)
    with pytest.raises(ValueError):
        exchange.import_library(str(package), str(receiver))
    assert list(receiver.iterdir()) == []
    assert catalog.libraries() == ()


def test_invalid_metadata_rolls_back_new_library(transfer, tmp_path: Path) -> None:
    catalog, exchange = transfer
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    package = tmp_path / "bad.toposlibrary"
    _make_package(package, ["files/photo.jpg"], assets={"photo.jpg": {"tags": [], "rating": 8}})
    with pytest.raises(ValueError, match="rating"):
        exchange.import_library(str(package), str(receiver))
    assert list(receiver.iterdir()) == []
    assert catalog.libraries() == ()
    assert list((catalog.locator.root / "trash").iterdir()) == []


def test_registry_failure_rolls_back_disk_and_catalog(transfer, tmp_path: Path, monkeypatch) -> None:
    catalog, exchange = transfer
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    package = tmp_path / "example.toposlibrary"
    _make_package(package, ["files/photo.jpg"], assets={"photo.jpg": {"tags": [], "rating": 4}})

    def fail_registration(records: tuple) -> None:
        with catalog._read() as cursor:
            assert cursor.execute("SELECT rating FROM assets").fetchone()[0] == 4
        assert catalog.libraries() == ()
        raise OSError("could not publish registry")

    monkeypatch.setattr(catalog, "_save_libraries", fail_registration)
    with pytest.raises(OSError, match="publish registry"):
        exchange.import_library(str(package), str(receiver))
    with catalog._read() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert list(receiver.iterdir()) == []
    assert list((catalog.locator.root / "trash").iterdir()) == []


def test_links_are_not_shared_or_extracted(transfer, tmp_path: Path) -> None:
    catalog, exchange = transfer
    root = tmp_path / "source"
    root.mkdir()
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    package = tmp_path / "bad.toposlibrary"
    link = ZipInfo("files/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    _make_package(package, [link])
    with pytest.raises(ValueError, match="链接"):
        exchange.import_library(str(package), str(receiver))
    assert not list(receiver.iterdir())
    try:
        (root / "link").symlink_to(package)
    except OSError:
        pytest.skip("symlinks unavailable")
    record = catalog.register_library(root)
    with pytest.raises(ValueError, match="链接"):
        exchange.export_library(record.library_id, str(receiver / "new.toposlibrary"))
    assert not list(receiver.iterdir())
