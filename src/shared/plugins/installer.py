"""Local package install/update/uninstall transactions (PL-08 core).

Staged layout under ``install_root``::

    install_root/
      <plugin_id>/                       # e.g. files.my_tool
        versions/<version>/              # immutable per-version payload
        versions/<version>.install.json  # installer-owned provenance + digest
        current -> versions/<version>    # atomic pointer (symlink swap)

Install pipeline: stage → validate → move version in → swap pointer. Every
failure path leaves the previously installed version running exactly as it
was, or refuses loudly — a half package can never appear as "installed".

Ownership rules (PR-01): the install root, each plugin root, ``versions``,
``.staging`` and every version directory are real directories created by the
installer. A symlink at any of these layers is refused before anything is
written, and ``current`` is only trusted when it resolves to a direct child
of that plugin's own ``versions`` directory. Mutations run inside one
transaction (thread lock + OS file lock); leftovers of crashed transactions
are swept at the start of the next one and their cleanup failures stay
visible instead of being pretended away.

Version identity (PR-01): ordering is full SemVer (a prerelease sorts below
its release), and reinstalling the *same* version with *different* content
is a conflict — "unchanged" is never concluded from the version string
alone. Provenance is recorded by the installer (``local_package``); a
package declaring ``source: builtin`` gains no trust from its own claim.

Security rules for packages (zip or folder):
- no absolute paths, no ``..`` traversal, no symlink members
- entry count / total size / per-file size bounded identically for zip and
  folder sources; folder checks run before the copy and again on the staged
  tree (files changed mid-copy cannot smuggle past the budget)
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
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.shared.plugins.manifest import (
    PluginManifest,
    parse_manifest_file,
    validate_manifest_set,
)

try:  # POSIX 跨进程事务锁；无 fcntl 的宿主退化为仅线程锁
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 20_000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
_PLUGIN_ID_RE = re.compile(r"[a-z][a-z0-9_]*\.[a-z0-9][a-z0-9._-]*")
_PROVENANCE_SUFFIX = ".install.json"
_LOCK_NAME = ".transactions.lock"
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


class InstallerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InstallOutcome:
    plugin_id: str
    version: str
    outcome: str          # installed / updated / unchanged / rejected / uninstalled
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


def _measure_source(folder: Path) -> None:
    """Folder packages live under the same entry/size budget as zips."""
    entries = 0
    total = 0
    for path in folder.rglob("*"):
        entries += 1
        if entries > MAX_PACKAGE_ENTRIES:
            raise InstallerError("PACKAGE_TOO_MANY_ENTRIES",
                                 f"目录条目超过 {MAX_PACKAGE_ENTRIES}")
        if path.is_file():
            size = path.stat().st_size
            total += size
            if size > MAX_MEMBER_BYTES:
                raise InstallerError("PACKAGE_MEMBER_TOO_LARGE",
                                     f"单文件过大：{path.relative_to(folder)}")
            if total > MAX_PACKAGE_BYTES:
                raise InstallerError("PACKAGE_TOO_LARGE",
                                     f"目录总大小超过 {MAX_PACKAGE_BYTES} 字节")


def _payload_digest(folder: Path) -> str:
    """Deterministic digest over payload files (path + exec bit + bytes)."""
    hasher = hashlib.sha256()
    names = sorted(path.relative_to(folder).as_posix()
                   for path in folder.rglob("*") if path.is_file())
    for name in names:
        path = folder / name
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(b"x" if path.stat().st_mode & 0o111 else b"-")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        hasher.update(b"\0")
    return hasher.hexdigest()


def _semver_key(version: str) -> tuple[tuple[int, int, int], int, tuple[tuple[int, Any], ...]]:
    """Full SemVer ordering: a prerelease sorts below its own release."""
    match = _SEMVER_RE.match(version)
    if match is None:
        raise InstallerError("VERSION_INVALID", f"版本不是语义化版本：{version}")
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    prerelease = match.group(4)
    if prerelease is None:
        return core, 1, ()
    identifiers = tuple(
        (0, int(chunk)) if chunk.isdigit() else (1, chunk)
        for chunk in prerelease.split("."))
    return core, 0, identifiers


class _Transaction:
    """One install/uninstall mutation at a time, per process and per host."""

    def __init__(self, thread_lock: threading.RLock, lock_path: Path) -> None:
        self._thread_lock = thread_lock
        self._lock_path = lock_path
        self._handle = None

    def __enter__(self) -> "_Transaction":
        self._thread_lock.acquire()
        parent = self._lock_path.parent
        if fcntl is not None and not parent.is_symlink():
            # 安装根是符号链接时不落锁文件（不能穿过链接写）；
            # 所有权核验随后会响亮拒绝整个事务。
            try:
                parent.mkdir(parents=True, exist_ok=True)
                self._handle = open(self._lock_path, "a+b")
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                if self._handle is not None:
                    self._handle.close()
                self._handle = None   # 退化为仅线程锁，不阻断安装
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                self._handle.close()
                self._handle = None
        self._thread_lock.release()


class LocalPackageInstaller:
    """Versioned, rollback-safe local package installs (PL-08)."""

    def __init__(self, install_root: Path, *, host_facts: dict[str, Any],
                 validator: Callable[[PluginManifest], list[str]] | None = None) -> None:
        self.install_root = Path(install_root)
        self._host_facts = dict(host_facts)
        self._validator = validator
        self._lock = threading.RLock()
        # 崩溃事务残留的清理诊断：保留可见，不假装从未发生
        self.last_cleanup_notes: tuple[str, ...] = ()

    # -- layout & ownership ----------------------------------------------
    def _plugin_root(self, plugin_id: str) -> Path:
        if not isinstance(plugin_id, str) or _PLUGIN_ID_RE.fullmatch(plugin_id) is None:
            raise InstallerError("PLUGIN_ID_INVALID", f"非法插件 id：{plugin_id}")
        return self.install_root / plugin_id

    def _versions_dir(self, plugin_id: str) -> Path:
        return self._plugin_root(plugin_id) / "versions"

    def _install_root_dir(self) -> Path:
        root = self.install_root
        if root.is_symlink():
            raise InstallerError("PATH_NOT_OWNED", f"安装根是符号链接，拒绝使用：{root}")
        if root.exists():
            if not root.is_dir():
                raise InstallerError("PATH_NOT_OWNED", f"安装根不是目录：{root}")
            return root
        try:
            root.mkdir(parents=True)
        except FileExistsError:
            pass
        except OSError as exc:
            raise InstallerError("INSTALL_IO_ERROR", f"无法创建安装根：{exc}") from exc
        if root.is_symlink() or not root.is_dir():
            raise InstallerError("PATH_NOT_OWNED", f"安装根创建后被替换，拒绝使用：{root}")
        return root

    def _ensure_real_dir(self, path: Path, *, what: str) -> Path:
        """受管层的每一层必须是本安装器创建的真实目录（PR-01）。"""
        if path.is_symlink():
            raise InstallerError("PATH_NOT_OWNED", f"{what} 是符号链接，拒绝写入：{path}")
        if path.exists():
            if not path.is_dir():
                raise InstallerError("PATH_NOT_OWNED", f"{what} 不是目录：{path}")
            return path
        try:
            path.mkdir()
        except OSError as exc:
            raise InstallerError("INSTALL_IO_ERROR", f"无法创建{what}：{exc}") from exc
        # 复核：检查与使用之间不得被替换
        if path.is_symlink() or not path.is_dir():
            raise InstallerError("PATH_NOT_OWNED", f"{what} 创建后被替换，拒绝使用：{path}")
        return path

    def _read_pointer(self, plugin_id: str, *, strict: bool = True) -> str:
        """``current`` 指向的版本名；strict 模式拒绝越界/异常指针。"""
        plugin_root = self._plugin_root(plugin_id)
        pointer = plugin_root / "current"

        def refuse(reason: str) -> str:
            if strict:
                raise InstallerError("POINTER_INVALID",
                                     f"current 指针异常（{plugin_id}）：{reason}")
            return ""

        if not pointer.exists() and not pointer.is_symlink():
            return ""
        if not pointer.is_symlink():
            return refuse("不是符号链接")
        try:
            target = Path(os.readlink(pointer))
        except OSError as exc:
            return refuse(f"无法读取链接：{exc}")
        if target.is_absolute():
            return refuse(f"指向绝对路径 {target}")
        versions = plugin_root / "versions"
        normalized = Path(os.path.normpath(plugin_root / target))
        if normalized.parent != versions:
            return refuse(f"越出本插件 versions 目录：{target}")
        if versions.is_symlink() or not versions.is_dir() or not normalized.is_dir():
            return refuse("目标版本目录不存在")
        if normalized.is_symlink():
            return refuse("目标版本目录是符号链接")
        try:
            if normalized.resolve().parent != versions.resolve():
                return refuse("解析后越出本插件 versions 目录")
        except OSError:
            return refuse("无法解析目标")
        return normalized.name

    # -- provenance -------------------------------------------------------
    def _provenance_path(self, version_dir: Path) -> Path:
        return version_dir.with_name(version_dir.name + _PROVENANCE_SUFFIX)

    def installed_provenance(self, plugin_id: str) -> dict[str, Any] | None:
        """安装器自记的来源记录；包声明的 ``source`` 不参与信任。"""
        with self._lock:
            version = self._read_pointer(plugin_id, strict=True)
            if not version:
                return None
            sidecar = self._provenance_path(self._versions_dir(plugin_id) / version)
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return data if isinstance(data, dict) else None

    def _stored_digest(self, version_dir: Path) -> str:
        sidecar = self._provenance_path(version_dir)
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("content_digest"), str):
                return data["content_digest"]
        except (OSError, ValueError):
            pass
        return _payload_digest(version_dir)   # 无 sidecar 的旧安装：现算

    def _sweep_transactions(self) -> None:
        """锁内清理崩溃事务的残留（.staging 目录与临时指针），失败保留诊断。"""
        notes: list[str] = []
        staging_parent = self.install_root / ".staging"
        if not staging_parent.is_symlink() and staging_parent.is_dir():
            for leftover in staging_parent.iterdir():
                try:
                    if leftover.is_dir() and not leftover.is_symlink():
                        shutil.rmtree(leftover, ignore_errors=True)
                    else:
                        leftover.unlink()
                except OSError as exc:
                    notes.append(f"清理残留暂存失败 {leftover.name}：{exc}")
        try:
            plugins = [path for path in self.install_root.iterdir()
                       if path.is_dir() and not path.is_symlink()]
        except OSError:
            plugins = []
        for plugin_root in plugins:
            for temp_pointer in plugin_root.glob(".current.*.tmp"):
                try:
                    temp_pointer.unlink()
                except OSError as exc:
                    notes.append(f"清理残留指针失败 {temp_pointer}：{exc}")
        self.last_cleanup_notes = tuple(notes)

    def _transaction(self) -> _Transaction:
        return _Transaction(self._lock, self.install_root / _LOCK_NAME)

    # -- introspection ----------------------------------------------------
    def current_version(self, plugin_id: str) -> str:
        with self._lock:
            return self._read_pointer(plugin_id, strict=True)

    def installed_manifest(self, plugin_id: str) -> PluginManifest | None:
        with self._lock:
            version = self._read_pointer(plugin_id, strict=True)
            if not version:
                return None
            manifest, _ = parse_manifest_file(
                self._versions_dir(plugin_id) / version / "topos.plugin.json")
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
        with self._transaction():
            return self._install_locked(package, expected_plugin_id, allow_downgrade)

    def _install_locked(self, package: Path, expected_plugin_id: str | None,
                        allow_downgrade: bool) -> InstallOutcome:
        self._install_root_dir()
        self._sweep_transactions()
        token = f"{os.getpid()}-{os.urandom(4).hex()}"
        staging_parent = self._ensure_real_dir(self.install_root / ".staging", what="暂存目录")
        staging = self._ensure_real_dir(staging_parent / token, what="暂存目录")
        version_dir: Path | None = None
        provenance_path: Path | None = None
        moved = False
        try:
            # 1) stage（解包/复制只进隔离目录，任何失败都不触碰已安装版本）
            if package.is_file():
                _safe_extract_zip(package, staging)
            elif package.is_dir():
                _reject_source_symlinks(package)      # 先查链接，再读内容
                _measure_source(package)              # 与 zip 同一预算（复制前）
                shutil.copytree(package, staging, dirs_exist_ok=True, symlinks=True)
                _reject_source_symlinks(staging)      # 复制后复查（防中途偷换）
                _measure_source(staging)              # 复制后复核预算
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

            # 3) 逐层所有权核验后的版本簿记
            plugin_root = self._ensure_real_dir(self._plugin_root(plugin_id), what="插件目录")
            versions = self._ensure_real_dir(plugin_root / "versions", what="版本目录")
            previous_version = self._read_pointer(plugin_id, strict=True)
            digest = _payload_digest(staging)
            if previous_version == manifest.version:
                if self._stored_digest(versions / manifest.version) == digest:
                    return InstallOutcome(plugin_id, manifest.version, "unchanged",
                                          previous_version)
                raise InstallerError("VERSION_CONTENT_CONFLICT",
                                     f"版本 {manifest.version} 与已安装内容不同："
                                     "请提升版本号，或先卸载再安装")
            if previous_version and not allow_downgrade:
                if _semver_key(manifest.version) <= _semver_key(previous_version):
                    raise InstallerError(
                        "VERSION_NOT_NEWER",
                        f"已安装 {previous_version}，拒绝平级/降级到 {manifest.version}（可显式允许）")

            # 4) 版本目录就位（临时指针名带事务令牌，不跨事务冲突）后原子切换
            version_dir = versions / manifest.version
            if version_dir.exists() or version_dir.is_symlink():
                raise InstallerError("VERSION_DIR_EXISTS",
                                     f"版本目录已存在：{version_dir}")
            os.rename(staging, version_dir)
            moved = True
            if version_dir.is_symlink() or not version_dir.is_dir():
                raise InstallerError("PATH_NOT_OWNED", "版本目录就位后被替换，拒绝使用")
            provenance_path = self._provenance_path(version_dir)
            try:
                provenance_path.write_text(json.dumps({
                    "plugin_id": plugin_id, "version": manifest.version,
                    # 来源由安装器记录；包自己声明的 source 不产生信任
                    "source": "local_package",
                    "content_digest": digest,
                }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                temp_pointer = plugin_root / f".current.{token}.tmp"
                if temp_pointer.is_symlink() or temp_pointer.exists():
                    temp_pointer.unlink()
                os.symlink(Path("versions") / manifest.version, temp_pointer)
                try:
                    os.replace(temp_pointer, plugin_root / "current")   # 原子切换
                except OSError:
                    if temp_pointer.is_symlink() or temp_pointer.exists():
                        temp_pointer.unlink()
                    raise
            except OSError as exc:
                raise InstallerError("INSTALL_IO_ERROR", f"安装 I/O 失败（已回滚）：{exc}") from exc
            if self._read_pointer(plugin_id, strict=True) != manifest.version:
                raise InstallerError("POINTER_INVALID", "指针切换后校验失败")
            outcome = "updated" if previous_version else "installed"
            return InstallOutcome(plugin_id, manifest.version, outcome, previous_version,
                                  rollback_path=str(versions / previous_version)
                                  if previous_version else "")
        except OSError as exc:
            # 指针切换前的任何失败：清掉新版本目录与暂存，旧版本原样可用
            if moved and version_dir is not None:
                shutil.rmtree(version_dir, ignore_errors=True)
                if provenance_path is not None:
                    provenance_path.unlink(missing_ok=True)
            raise InstallerError("INSTALL_IO_ERROR", f"安装 I/O 失败（已回滚）：{exc}") from exc
        except InstallerError:
            if moved and version_dir is not None:
                shutil.rmtree(version_dir, ignore_errors=True)
                if provenance_path is not None:
                    provenance_path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # -- uninstall -------------------------------------------------------
    def uninstall(self, plugin_id: str, *, is_active: Callable[[str], bool],
                  remove_data: bool = False,
                  delete_data: Callable[[str], None] | None = None) -> InstallOutcome:
        """Remove versions; "remove plugin" and "delete data" stay separate.

        失败协议：``UNINSTALL_IO_ERROR`` 后插件目录仍在，重试卸载安全且幂等；
        ``DATA_DELETE_FAILED`` 表示插件目录已移除，仅数据删除需宿主单独重试。
        """
        if remove_data and delete_data is None:
            raise InstallerError("DATA_POLICY_MISSING",
                                 "要求删除数据但宿主未提供数据删除入口")
        plugin_root = self._plugin_root(plugin_id)
        with self._transaction():
            if is_active(plugin_id):
                raise InstallerError("PLUGIN_ACTIVE", "插件仍在运行/加载，先停用再卸载")
            self._install_root_dir()
            if plugin_root.is_symlink():
                raise InstallerError("PATH_NOT_OWNED", "插件目录是符号链接，拒绝删除")
            if not plugin_root.exists():
                raise InstallerError("PLUGIN_NOT_INSTALLED", f"未安装：{plugin_id}")
            previous = self._read_pointer(plugin_id, strict=False)
            try:
                shutil.rmtree(plugin_root)
            except OSError as exc:
                raise InstallerError("UNINSTALL_IO_ERROR",
                                     f"插件目录移除失败，可直接重试卸载：{exc}") from exc
            if remove_data:
                try:
                    delete_data(plugin_id)            # 用户数据绝不属于插件目录
                except Exception as exc:              # 宿主回调边界：转成明确结果
                    raise InstallerError("DATA_DELETE_FAILED",
                                         f"插件已移除，但用户数据删除失败（可单独重试数据删除）：{exc}") from exc
            self._sweep_transactions()
            return InstallOutcome(plugin_id, "", "uninstalled", previous)


__all__ = ["InstallerError", "InstallOutcome", "LocalPackageInstaller",
           "MAX_PACKAGE_BYTES", "MAX_PACKAGE_ENTRIES"]
