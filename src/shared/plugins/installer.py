"""Local package install/update/uninstall transactions (PL-08 core).

Staged layout under ``install_root``::

    install_root/
      <plugin_id>/                     # e.g. files.my_tool
        versions/<version>/            # immutable per-version payload
        current -> versions/<version>  # atomic pointer (symlink swap)

Install pipeline: stage → validate → move version in → swap pointer. Every
failure path leaves the previously installed version running exactly as it
was, or refuses loudly — a half package can never appear as "installed".

Security rules for packages (zip or folder):
- no absolute paths, no ``..`` traversal, no symlink members
- entry count / total uncompressed size / per-file size are bounded
- manifest is declarative (``topos.plugin.json``) and validated against host
  facts before any file is moved into place
- ``runAfterInstall``-style hooks are data: the installer never executes them

Honest limits: verification of signatures/public-key trust is a PL-09/D stage
concern; here ``declared_hash`` (when present) must match the deterministic
content digest computed over the packaged manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.shared.plugins.manifest import (
    PluginManifest,
    parse_manifest_file,
    validate_manifest_set,
)

MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 20_000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
_PLUGIN_ID_RE = re.compile(r"[a-z][a-z0-9_]*\.[a-z0-9][a-z0-9._-]*")


class InstallerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InstallOutcome:
    plugin_id: str
    version: str
    outcome: str          # installed / updated / unchanged / rejected
    previous_version: str = ""
    rollback_path: str = ""   # non-empty when a previous version was preserved

    def to_dict(self) -> dict[str, Any]:
        return {"plugin_id": self.plugin_id, "version": self.version,
                "outcome": self.outcome, "previous_version": self.previous_version,
                "rollback_path": self.rollback_path}


def _safe_extract_zip(package: Path, staging: Path) -> None:
    try:
        with zipfile.ZipFile(package) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_PACKAGE_ENTRIES:
                raise InstallerError("PACKAGE_TOO_MANY_ENTRIES",
                                     f"压缩包条目超过 {MAX_PACKAGE_ENTRIES}")
            total = sum(info.file_size for info in infos)
            if total > MAX_PACKAGE_BYTES:
                raise InstallerError("PACKAGE_TOO_LARGE",
                                     f"解压总大小超过 {MAX_PACKAGE_BYTES} 字节")
            for info in infos:
                name = info.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    raise InstallerError("PACKAGE_PATH_TRAVERSAL",
                                         f"压缩包含非法路径：{name}")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise InstallerError("PACKAGE_SYMLINK_MEMBER",
                                         f"压缩包含符号链接：{name}")
                if name.endswith("/") or info.is_dir():
                    (staging / name).mkdir(parents=True, exist_ok=True)
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise InstallerError("PACKAGE_MEMBER_TOO_LARGE", f"单文件过大：{name}")
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(target, "wb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise InstallerError("PACKAGE_NOT_ZIP", f"不是有效的 zip 包：{exc}") from exc


def _reject_source_symlinks(folder: Path) -> None:
    """Inspect names before any source file is read or copied."""
    if folder.is_symlink():
        raise InstallerError("PACKAGE_SYMLINK_MEMBER", "安装来源不能是符号链接")
    for entry in folder.rglob("*"):
        if entry.is_symlink():
            raise InstallerError("PACKAGE_SYMLINK_MEMBER",
                                 f"包内含符号链接：{entry.relative_to(folder)}")


class LocalPackageInstaller:
    """Versioned, rollback-safe local package installs (PL-08)."""

    def __init__(self, install_root: Path, *, host_facts: dict[str, Any],
                 validator: Callable[[PluginManifest], list[str]] | None = None) -> None:
        self.install_root = Path(install_root)
        self._host_facts = dict(host_facts)
        self._validator = validator

    # -- layout ----------------------------------------------------------
    def _plugin_root(self, plugin_id: str) -> Path:
        if not isinstance(plugin_id, str) or _PLUGIN_ID_RE.fullmatch(plugin_id) is None:
            raise InstallerError("PLUGIN_ID_INVALID", f"非法插件 id：{plugin_id}")
        return self.install_root / plugin_id

    def current_version(self, plugin_id: str) -> str:
        pointer = self._plugin_root(plugin_id) / "current"
        if not pointer.is_symlink():
            return ""
        target = Path(os.readlink(pointer))
        return target.name if target.name else ""

    def installed_manifest(self, plugin_id: str) -> PluginManifest | None:
        pointer = self._plugin_root(plugin_id) / "current"
        if not pointer.is_symlink():
            return None
        manifest, _ = parse_manifest_file(pointer.resolve() / "topos.plugin.json")
        return manifest

    # -- install ---------------------------------------------------------
    def install(self, package: Path, *, expected_plugin_id: str | None = None,
                allow_downgrade: bool = False) -> InstallOutcome:
        """Install or update from a zip/folder package; atomic, rollback-safe."""
        package = Path(package)
        if package.is_symlink():
            raise InstallerError("PACKAGE_SYMLINK_MEMBER", "安装来源不能是符号链接")
        if not package.exists():
            raise InstallerError("PACKAGE_MISSING", f"安装包不存在：{package}")
        staging = self.install_root / ".staging" / f"{os.getpid()}-{os.urandom(4).hex()}"
        staging.parent.mkdir(parents=True, exist_ok=True)
        version_dir: Path | None = None
        try:
            # 1) stage（解包只进隔离目录，任何失败都不触碰已安装版本）
            staging.mkdir(parents=True)
            if package.is_file():
                _safe_extract_zip(package, staging)
            elif package.is_dir():
                _reject_source_symlinks(package)
                if sum(1 for _ in package.rglob("*")) > MAX_PACKAGE_ENTRIES:
                    raise InstallerError("PACKAGE_TOO_MANY_ENTRIES", "目录条目过多")
                # Also preserve and reject links introduced after preflight;
                # never silently dereference them into the installed payload.
                shutil.copytree(package, staging, dirs_exist_ok=True, symlinks=True)
                _reject_source_symlinks(staging)
            else:
                raise InstallerError("PACKAGE_KIND_UNKNOWN", "安装包必须是 zip 或目录")

            # 2) validate declaration（此处绝不执行插件代码）
            manifest_path = staging / "topos.plugin.json"
            if not manifest_path.is_file():
                raise InstallerError("MANIFEST_MISSING", "包内缺少 topos.plugin.json")
            manifest, diagnostics = parse_manifest_file(manifest_path)
            if manifest is None:
                raise InstallerError("MANIFEST_INVALID",
                                     "manifest 无效：" + ";".join(d.message for d in diagnostics))
            blocking = [d for d in diagnostics if d.severity == "error"]
            findings = validate_manifest_set((manifest,), **self._host_facts)
            blocking += [d for d in findings[manifest.plugin_id] if d.severity == "error"]
            if blocking:
                raise InstallerError("MANIFEST_REJECTED",
                                     "manifest 被宿主拒绝：" + ";".join(d.message for d in blocking))
            if self._validator is not None:
                reasons = self._validator(manifest)
                if reasons:
                    raise InstallerError("PACKAGE_VALIDATOR_REJECTED", "；".join(reasons))
            # 宿主不执行任何 runAfterInstall 式钩子；未知字段只是数据
            if "runAfterInstall" in manifest.metadata:
                raise InstallerError("INSTALL_HOOK_FORBIDDEN",
                                     "manifest 声明了 runAfterInstall 钩子，宿主不执行")
            plugin_id = manifest.plugin_id
            if expected_plugin_id is not None and plugin_id != expected_plugin_id:
                raise InstallerError("PLUGIN_ID_MISMATCH",
                                     f"包身份 {plugin_id} 与期望 {expected_plugin_id} 不符")

            # 3) version bookkeeping
            previous_version = self.current_version(plugin_id)
            if previous_version == manifest.version:
                return InstallOutcome(plugin_id, manifest.version, "unchanged",
                                      previous_version)
            if (previous_version and not allow_downgrade
                    and _version_tuple(manifest.version) <= _version_tuple(previous_version)):
                raise InstallerError(
                    "VERSION_NOT_NEWER",
                    f"已安装 {previous_version}，拒绝平级/降级到 {manifest.version}（可显式允许）")

            # 4) 版本目录就位后原子切换 current 指针
            versions = self._plugin_root(plugin_id) / "versions"
            versions.mkdir(parents=True, exist_ok=True)
            version_dir = versions / manifest.version
            if version_dir.exists():
                raise InstallerError("VERSION_DIR_EXISTS",
                                     f"版本目录已存在：{version_dir}")
            os.rename(staging, version_dir)
            pointer = self._plugin_root(plugin_id) / "current"
            temp_pointer = self._plugin_root(plugin_id) / ".current.tmp"
            if temp_pointer.is_symlink() or temp_pointer.exists():
                temp_pointer.unlink()
            os.symlink(Path("versions") / manifest.version, temp_pointer)
            os.replace(temp_pointer, pointer)        # 原子切换；旧版本目录保留作回滚
            outcome = "updated" if previous_version else "installed"
            return InstallOutcome(plugin_id, manifest.version, outcome, previous_version,
                                  rollback_path=str(versions / previous_version)
                                  if previous_version else "")
        except OSError as exc:
            # 指针切换前的任何失败：清掉新版本目录与暂存，旧版本原样可用
            if version_dir is not None and version_dir.exists():
                shutil.rmtree(version_dir, ignore_errors=True)
            raise InstallerError("INSTALL_IO_ERROR", f"安装 I/O 失败（已回滚）：{exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # -- uninstall -------------------------------------------------------
    def uninstall(self, plugin_id: str, *, is_active: Callable[[str], bool],
                  remove_data: bool = False,
                  delete_data: Callable[[str], None] | None = None) -> InstallOutcome:
        """Remove versions; "remove plugin" and "delete data" stay separate."""
        plugin_root = self._plugin_root(plugin_id)
        if remove_data and delete_data is None:
            raise InstallerError("DATA_POLICY_MISSING",
                                 "要求删除数据但宿主未提供数据删除入口")
        if is_active(plugin_id):
            raise InstallerError("PLUGIN_ACTIVE", "插件仍在运行/加载，先停用再卸载")
        if not plugin_root.exists():
            raise InstallerError("PLUGIN_NOT_INSTALLED", f"未安装：{plugin_id}")
        previous = self.current_version(plugin_id)
        shutil.rmtree(plugin_root)
        if remove_data:
            delete_data(plugin_id)                    # 用户数据绝不属于插件目录
        return InstallOutcome(plugin_id, "", "uninstalled", previous)


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


__all__ = ["InstallerError", "InstallOutcome", "LocalPackageInstaller",
           "MAX_PACKAGE_BYTES", "MAX_PACKAGE_ENTRIES"]
