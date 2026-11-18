"""Explicit new-catalog backup/restore and reversible development reset.

Source media are references only. Operations reject links and publish complete
snapshots by rename; reset archives the managed root instead of deleting it.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Sequence
import uuid

from src.features.files.repository.catalog import CATALOG_SCHEMA_VERSION, FilesCatalog
from src.features.files.repository.locator import FilesDataLocator
from src.features.files.transactions.ops import OpError


class MaintenanceError(ValueError):
    """Unsafe, incomplete or damaged maintenance request."""


def _owned_path(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for parent in (*reversed(absolute.parents), absolute):
        if parent.is_symlink():
            raise MaintenanceError("维护路径不能穿过符号链接")


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _files(root: Path) -> list[Path]:
    _owned_path(root)
    result: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise MaintenanceError("备份不接受链接或非常规文件")
        if path.is_file():
            result.append(path)
    return result


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    return {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for (name,) in tables}


def _sync(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


class FilesMaintenance:
    def __init__(self, catalog: FilesCatalog) -> None:
        self.catalog = catalog
        self.locator = catalog.locator

    def backup(self, destination: Path | None = None) -> Path:
        """Snapshot the new system under its domain lock, never copy sources."""
        root = self.locator.root
        _owned_path(root)
        if root.name != "files-data":
            raise MaintenanceError("仅允许备份明确的 files-data 受管根")
        target = destination or self.locator.backups_dir / (
            time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12])
        target = Path(os.path.abspath(target))
        _owned_path(target)
        if target.exists():
            raise MaintenanceError("备份目标已存在，不覆盖可用备份")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".backup-", dir=target.parent))
        try:
            with self.catalog._lock:
                for library in self.catalog.libraries():
                    if Path(library.root).resolve().is_relative_to(root.resolve()):
                        raise MaintenanceError("源素材库不能位于受管数据目录中")
                with closing(sqlite3.connect(staging / "catalog.db")) as copied:
                    self.catalog._connection().backup(copied)
                    copied.execute("PRAGMA journal_mode=DELETE")
                    counts = _counts(copied)
                    if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise MaintenanceError("数据库快照未通过完整性检查")
                for relative in ("libraries.json", "schema_version", "plugins/state.json",
                                 "plugin_desired.json", "plugin_config.json", "plugin_dev_sources.json"):
                    source = root / relative
                    _owned_path(source)
                    if source.exists():
                        destination_file = staging / relative
                        destination_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, destination_file)
                trash = root / "trash"
                if trash.exists():
                    _files(trash)  # Verify the whole tree before copying any payload.
                    for source in sorted(trash.rglob("*")):
                        destination_file = staging / source.relative_to(root)
                        if source.is_dir():
                            destination_file.mkdir(parents=True, exist_ok=True)
                        else:
                            destination_file.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(source, destination_file)
                # Package code is reinstallable, not user data. Retain a bounded
                # declaration of installed versions, without running any plugin.
                package_index: list[dict[str, str]] = []
                install = self.locator.plugins_install_root
                if install.exists():
                    for source in _files_without_current_links(install):
                        if source.name.endswith(".install.json"):
                            package_index.append({"record": str(source.relative_to(install)),
                                                  "sha256": _digest(source)})
                (staging / "packages.json").write_text(json.dumps(package_index), encoding="utf-8")
                entries = {path.relative_to(staging).as_posix(): {
                    "sha256": _digest(path), "size": path.stat().st_size,
                } for path in _files(staging)}
                directories = [path.relative_to(staging).as_posix()
                               for path in sorted(staging.rglob("*")) if path.is_dir()]
                manifest = {"format": 1, "schema": CATALOG_SCHEMA_VERSION,
                            "created_at": time.time(), "counts": counts,
                            "files": entries, "directories": directories}
                (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
                for path in _files(staging):
                    _sync(path)
                self.verify(staging)
                # No-replace publishing; a competing existing target is refused.
                from src.features.files.transactions.ops import rename_no_replace
                rename_no_replace(staging, target)
            return target
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    @staticmethod
    def verify(backup: Path) -> dict[str, Any]:
        backup = Path(backup)
        physical = {path.relative_to(backup).as_posix() for path in _files(backup)}
        try:
            manifest_path = backup / "manifest.json"
            if manifest_path.stat().st_size > 32 * 1024 * 1024:
                raise MaintenanceError("备份清单超过读取预算")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MaintenanceError("备份清单缺失或损坏") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != 1 or manifest.get("schema") != CATALOG_SCHEMA_VERSION:
            raise MaintenanceError("不支持的备份版本")
        entries = manifest.get("files")
        if not isinstance(entries, dict) or "catalog.db" not in entries or len(entries) > 100000:
            raise MaintenanceError("备份条目无效")
        directories = manifest.get("directories", [])
        if not isinstance(directories, list) or any(not isinstance(name, str) for name in directories):
            raise MaintenanceError("备份目录记录无效")
        physical_dirs = {path.relative_to(backup).as_posix()
                         for path in backup.rglob("*") if path.is_dir()}
        if set(directories) != physical_dirs:
            raise MaintenanceError("备份目录缺失或含未登记内容")
        for name, info in entries.items():
            relative = PurePosixPath(name)
            if not name or relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise MaintenanceError("备份含非法路径")
            if not isinstance(info, dict):
                raise MaintenanceError("备份摘要记录无效")
            path = backup / name
            if not path.is_file() or path.stat().st_size != info.get("size") or _digest(path) != info.get("sha256"):
                raise MaintenanceError("备份文件缺失或摘要不符")
        if physical != set(entries) | {"manifest.json"}:
            raise MaintenanceError("备份含未登记内容")
        with closing(sqlite3.connect((backup / "catalog.db").resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            if (conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or conn.execute("PRAGMA user_version").fetchone()[0] != CATALOG_SCHEMA_VERSION
                    or _counts(conn) != manifest.get("counts")):
                raise MaintenanceError("备份数据库结构或计数不符")
        return manifest

    @classmethod
    def restore(cls, backup: Path, destination: Path) -> Path:
        """Restore into an empty managed root, leaving the backup unchanged."""
        backup, destination = Path(backup), Path(os.path.abspath(destination))
        _owned_path(destination)
        if destination.name != "files-data" or destination == destination.parent:
            raise MaintenanceError("恢复目标须为独立 files-data 目录")
        if destination.resolve().is_relative_to(backup.resolve()) or backup.resolve().is_relative_to(destination.resolve()):
            raise MaintenanceError("恢复目标与备份不能重叠")
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise MaintenanceError("恢复目标必须为空，不支持原地合并")
        manifest = cls.verify(backup)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
        try:
            for name in manifest.get("directories", []):
                (staging / name).mkdir(parents=True, exist_ok=True)
            for name in manifest["files"]:
                source, target = backup / name, staging / name
                _owned_path(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                _sync(target)
            (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            cls.verify(staging)
            # Backup manifest is audit evidence, not catalog runtime state.
            (staging / "manifest.json").unlink()
            if destination.exists():
                destination.rmdir()  # A concurrent writer makes this fail safely.
            from src.features.files.transactions.ops import rename_no_replace
            rename_no_replace(staging, destination)
            return destination
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def reset(self, *, apply: bool = False) -> dict[str, str]:
        """Explicit development reset: archive the complete old root, never rm it."""
        root = self.locator.root
        _owned_path(root)
        if root.name != "files-data":
            raise MaintenanceError("仅可重置 files-data 受管根")
        report = {"scope": "files-data", "action": "archive_then_initialize"}
        if not apply:
            return report
        with self.catalog._lock:
            backup = self.backup()
            archive = root.with_name("files-data.before-reset-" + uuid.uuid4().hex)
            self.catalog.close()
            from src.features.files.transactions.ops import rename_no_replace
            rename_no_replace(root, archive)
            try:
                self.catalog.open()
            except BaseException:
                if root.exists():
                    failed = root.with_name("files-data.failed-init-" + uuid.uuid4().hex)
                    rename_no_replace(root, failed)
                rename_no_replace(archive, root)
                self.catalog.open()
                raise
        return {**report, "archive": str(archive), "backup": str(archive / backup.relative_to(root))}


def _files_without_current_links(root: Path) -> list[Path]:
    result: list[Path] = []
    _owned_path(root)
    for path in root.rglob("*"):
        if path.is_symlink():
            if path.name != "current":
                raise MaintenanceError("安装目录含未识别链接")
            continue
        if path.is_file():
            result.append(path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="New Files catalog maintenance; no source media writes")
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("operation", choices=("backup", "list", "verify", "restore", "dev-reset"))
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--into", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    locator = FilesDataLocator.for_config_root(args.config_root)
    catalog: FilesCatalog | None = None
    try:
        if args.operation == "verify":
            if args.backup is None:
                parser.error("verify requires --backup")
            manifest = FilesMaintenance.verify(args.backup)
            result = {"ok": True, "files": len(manifest["files"])}
        elif args.operation == "restore":
            if args.backup is None or args.into is None:
                parser.error("restore requires --backup and --into")
            result = {"restored": str(FilesMaintenance.restore(args.backup, args.into))}
        elif args.operation == "list":
            _owned_path(locator.backups_dir)
            result = {"backups": sorted(path.name for path in locator.backups_dir.glob("*")
                                        if not path.name.startswith(".") and not path.is_symlink()
                                        and path.is_dir() and (path / "manifest.json").is_file())}
        elif args.operation == "dev-reset" and not args.apply:
            _owned_path(locator.root)
            result = {"scope": str(locator.root), "exists": locator.root.exists(),
                      "action": "archive_then_initialize"}
        else:
            catalog = FilesCatalog(locator)
            service = FilesMaintenance(catalog)
            result = ({"backup": str(service.backup())} if args.operation == "backup"
                      else service.reset(apply=args.apply))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, sqlite3.Error, OpError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if catalog is not None:
            catalog.close()


if __name__ == "__main__":
    raise SystemExit(main())
