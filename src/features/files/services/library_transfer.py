"""Portable exchange of a single registered Files library."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import time
from typing import Any
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from src.features.files.contracts import is_valid_folder_name
from src.features.files.repository.catalog import FilesCatalog, LibraryRecord, nfc
from src.features.files.services.domain import FilesMetadataService
from src.features.files.transactions.ops import rename_no_replace

LIBRARY_PACKAGE_SUFFIX = ".toposlibrary"
_PACKAGE_FORMAT = "topos.asset-library"
_PACKAGE_VERSION = 1
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


def _valid_component(name: str) -> bool:
    return (is_valid_folder_name(name) and not name.startswith(".")
            and not name.endswith(" "))


def _member_path(name: str) -> PurePosixPath:
    parts = name.split("/")
    if not all(_valid_component(part) for part in parts):
        raise ValueError(f"素材库包包含不可移植的名称：{name}")
    return PurePosixPath(*parts)


def _raise_walk_error(error: OSError) -> None:
    raise error


def _source_info(path: Path) -> os.stat_result:
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f"素材库包含链接或非常规文件：{path}")
    return info


def _library_members(root: Path) -> dict[str, Path]:
    members: dict[str, Path] = {}
    spellings: set[str] = set()
    for folder, dirs, files in os.walk(root, onerror=_raise_walk_error):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        for name in [*dirs, *sorted(name for name in files if not name.startswith("."))]:
            path = Path(folder) / name
            _source_info(path)
            relative = path.relative_to(root).as_posix()
            key = nfc(_member_path(relative).as_posix()).casefold()
            if key in spellings:
                raise ValueError(f"素材库包含重名路径：{relative}")
            spellings.add(key)
            members[relative] = path
    return members


def _manifest(archive: ZipFile) -> dict[str, Any]:
    info = archive.getinfo("manifest.json")
    if info.file_size > _MAX_MANIFEST_BYTES:
        raise ValueError("素材库清单过大")
    data = json.loads(archive.read(info))
    if (not isinstance(data, dict) or data.get("format") != _PACKAGE_FORMAT
            or type(data.get("version")) is not int or data["version"] != _PACKAGE_VERSION):
        raise ValueError("无法识别此素材库包的格式或版本")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("素材库包缺少库名称")
    return data


def _archive_members(archive: ZipFile) -> dict[str, ZipInfo]:
    entries: dict[str, ZipInfo] = {}
    spellings: dict[str, str] = {}
    for info in archive.infolist():
        if info.orig_filename == "manifest.json":
            continue
        if info.orig_filename != info.filename or not info.filename.startswith("files/"):
            raise ValueError(f"素材库包包含无效路径：{info.orig_filename}")
        relative = _member_path(info.filename[len("files/"):].removesuffix("/"))
        declared_type = stat.S_IFMT(info.external_attr >> 16)
        expected_type = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
        if declared_type not in (0, expected_type) or info.flag_bits & 1:
            raise ValueError(f"素材库包包含链接、加密或非常规文件：{relative}")
        name = relative.as_posix()
        if name in entries:
            raise ValueError(f"素材库包包含重复路径：{name}")
        entries[name] = info
        for path in (relative, *relative.parents[:-1]):
            spelling = path.as_posix()
            key = nfc(spelling).casefold()
            if spellings.setdefault(key, spelling) != spelling:
                raise ValueError(f"素材库包包含重名路径：{spelling}")

    if sum(info.filename == "manifest.json" for info in archive.infolist()) != 1:
        raise ValueError("素材库包必须包含一份清单")
    for name in entries:
        for parent in PurePosixPath(name).parents:
            info = entries.get(parent.as_posix())
            if info is not None and not info.is_dir():
                raise ValueError(f"素材库包的文件与文件夹重名：{parent}")
    return entries


def _extract_members(archive: ZipFile, entries: dict[str, ZipInfo], root: Path) -> None:
    if sum(entry.file_size for entry in entries.values()) > shutil.disk_usage(root).free:
        raise OSError("目标磁盘空间不足")
    for relative, info in entries.items():
        destination = root / relative
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            modified = _member_mtime(info)
            os.utime(destination, (modified, modified))


def _member_mtime(info: ZipInfo) -> float:
    return time.mktime((*info.date_time, 0, 0, -1))


def _publish_import(source: Path, parent: Path, name: str, registered: set[str]) -> Path:
    base = name if _valid_component(name) else "Imported Library"
    for index in range(1, 10_001):
        target = parent / (base if index == 1 else f"{base} ({index})")
        if nfc(target).casefold() in registered:
            continue
        try:
            rename_no_replace(source, target)
        except FileExistsError:
            continue
        return target
    raise FileExistsError(f"无法为导入素材库创建新文件夹：{base}")


class FilesLibraryTransfer:
    def __init__(self, catalog: FilesCatalog, metadata: FilesMetadataService) -> None:
        self._catalog = catalog
        self._metadata = metadata

    def export_library(self, library_id: str, destination: str) -> Path:
        library = next((row for row in self._catalog.libraries()
                        if row.library_id == library_id), None)
        if library is None:
            raise ValueError("找不到要导出的素材库")
        root = Path(library.root).resolve(strict=True)
        output = Path(os.path.abspath(destination))
        parent = output.parent.resolve(strict=True)
        if parent.is_relative_to(root):
            raise ValueError("请将素材库包保存到源素材库之外")
        managed = self._catalog.locator.root.resolve()
        if managed.is_relative_to(root) or root.is_relative_to(managed):
            raise ValueError("素材库包含 Topos 的本地数据目录")
        target = parent / output.name
        if os.path.lexists(target):
            raise FileExistsError(f"素材库包已存在：{target}")

        members = _library_members(root)
        paths = {nfc(path): relative for relative, path in members.items()}
        manifest = {
            "format": _PACKAGE_FORMAT, "version": _PACKAGE_VERSION, "name": library.name,
            **self._catalog.library_share_metadata(library_id, paths),
        }
        data = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > _MAX_MANIFEST_BYTES:
            raise ValueError("素材库清单过大")
        with tempfile.TemporaryDirectory(prefix=".topos-library-", dir=parent) as temp:
            staging = Path(temp) / "package"
            with ZipFile(staging, "w", compression=ZIP_STORED, strict_timestamps=False) as archive:
                archive.writestr("manifest.json", data)
                for relative, path in members.items():
                    before = _source_info(path)
                    archive.write(path, f"files/{relative}")
                    after = path.lstat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                            after.st_ino, after.st_size, after.st_mtime_ns):
                        raise OSError(f"导出期间文件已发生变化，请重试：{path}")
            rename_no_replace(staging, target)
        return target

    def import_library(self, package: str, destination_parent: str) -> LibraryRecord:
        parent = Path(destination_parent).resolve(strict=True)
        if parent.is_relative_to(self._catalog.locator.root.resolve()):
            raise ValueError("请选择 Topos 本地数据目录之外的文件夹")
        with tempfile.TemporaryDirectory(prefix=".topos-library-", dir=parent) as temp:
            staging = Path(temp) / "files"
            staging.mkdir()
            with ZipFile(package) as archive:
                entries = _archive_members(archive)
                manifest = _manifest(archive)
                _extract_members(archive, entries, staging)
            staged_info = staging.lstat()
            with self._catalog._lock:
                registered = {lib.root.casefold() for lib in self._catalog.libraries()}
                root = _publish_import(staging, parent, manifest["name"], registered)
                try:
                    return self._catalog.register_imported_library(
                        root, manifest["name"],
                        lambda record: self._restore_metadata(record.library_id, root, manifest, entries),
                    )
                except BaseException:
                    try:
                        current = root.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        if os.path.samestat(staged_info, current):
                            rename_no_replace(root, staging)
                    raise

    def _restore_metadata(
        self, library_id: str, root: Path, manifest: dict[str, Any], entries: dict[str, ZipInfo]
    ) -> None:
        assets, tags, collections = (manifest.get(key) for key in ("assets", "tags", "collections"))
        if not isinstance(assets, dict) or not isinstance(tags, list) or not isinstance(collections, list):
            raise ValueError("素材库清单的元数据无效")
        tag_ids: dict[str, int] = {}
        for tag in tags:
            if (not isinstance(tag, dict) or not isinstance(tag.get("name"), str)
                    or type(tag.get("color")) is not int):
                raise ValueError("素材库包的标签无效")
            name = tag["name"]
            parent = tag.get("parent")
            if name in tag_ids or parent is not None and (not isinstance(parent, str) or parent not in tag_ids):
                raise ValueError("素材库包的标签层级无效")
            tag_ids[name] = self._catalog.create_tag(library_id, name, tag["color"], tag_ids.get(parent))
        for relative, fields in assets.items():
            if relative not in entries or not isinstance(fields, dict):
                raise ValueError(f"素材库包的资产元数据无效：{relative}")
            asset_tags = fields.get("tags")
            if not isinstance(asset_tags, list) or not all(
                    isinstance(tag, str) and tag in tag_ids for tag in asset_tags):
                raise ValueError(f"素材库包的资产标签无效：{relative}")
            path = root / relative
            info = entries[relative]
            self._catalog.insert_asset_full(library_id, {
                "path": path, "is_dir": info.is_dir(), "size": info.file_size,
                "mtime": _member_mtime(info),
            })
            patch = {key: value for key, value in fields.items() if key != "tags"}
            result = self._metadata.patch_metadata([str(path)], patch, library_id=library_id)
            if not result.ok:
                raise ValueError(result.message or f"无法恢复资产信息：{relative}")
            for tag in asset_tags:
                self._catalog.tag_asset(library_id, path, tag_ids[tag])
        for collection in collections:
            if (not isinstance(collection, dict) or not isinstance(collection.get("name"), str)
                    or not collection["name"].strip() or type(collection.get("color")) is not int
                    or not isinstance(collection.get("members"), list)
                    or not all(isinstance(path, str) and path in entries for path in collection["members"])):
                raise ValueError("素材库包的集合无效")
            self._catalog.create_collection(
                library_id, collection["name"], collection["color"],
                paths=(str(root / path) for path in collection["members"]),
            )
