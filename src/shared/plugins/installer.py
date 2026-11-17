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
import stat
import threading
import time
import zipfile
from dataclasses import dataclass

from src.shared.plugins import install_lock
from pathlib import Path
from typing import Any, Callable

from src.shared.plugins.manifest import (
    PluginManifest,
    parse_manifest_file,
    validate_manifest_set,
)

try:  # POSIX 跨进程事务锁；其他宿主明确拒绝，不能假装获得事务锁
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 20_000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
_PLUGIN_ID_RE = re.compile(r"[a-z][a-z0-9_]*\.[a-z0-9][a-z0-9._-]*")
_PROVENANCE_SUFFIX = ".install.json"
_LOCK_NAME = ".transactions.lock"
_SEMVER_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")


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
                if name.startswith("/") or ".." in Path(name).parts or "\\" in name or ":" in name:
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
                # Match author-side deterministic packaging and directory
                # installs; never restore setuid or platform-specific bits.
                executable = info.create_system == 3 and bool((info.external_attr >> 16) & 0o111)
                target.chmod(0o755 if executable else 0o644)
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
        if not (entry.is_dir() or entry.is_file()):
            raise InstallerError("PACKAGE_SPECIAL_MEMBER",
                                 f"包内含非常规文件：{entry.relative_to(folder)}")


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
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        raise InstallerError("VERSION_INVALID", f"版本不是语义化版本：{version}")
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    prerelease = match.group(4)
    if prerelease is None:
        return core, 1, ()
    if any(chunk.isdigit() and len(chunk) > 1 and chunk.startswith("0")
           for chunk in prerelease.split(".")):
        raise InstallerError("VERSION_INVALID", f"预发布数字标识不能有前导零：{version}")
    identifiers = tuple(
        (0, int(chunk)) if chunk.isdigit() else (1, chunk)
        for chunk in prerelease.split("."))
    return core, 0, identifiers


class TransactionJournal:
    """[F14] 安装事务持久日志（append-only JSONL + 每条 fsync）。

    崩溃恢复口径：下次事务启动时扫描无 ``committed``/``aborted`` 收尾的
    ``begin``——按磁盘事实判定（不是按日志自称）：
    * current 严格校验通过且 payload/provenance 摘要一致 → 追加
      ``recovered:committed``（换轨已原子完成，前滚）；
    * current 校验失败/摘要不符 → 指针回指 ``previous_version``，坏
      payload 目录隔离（``.quarantined-<token>``），追加
      ``recovered:rolled_back``；
    * 无 current（交换前崩溃）→ 追加 ``recovered:aborted``（暂存清扫
      由既有 ``_sweep_transactions`` 负责）。
    """

    def __init__(self, install_root: Path) -> None:
        self._path = Path(install_root) / ".transactions.log"
        self._lock = threading.Lock()

    def _append(self, entry: dict[str, Any]) -> None:
        entry = {"ts": time.time(), **entry}
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)                      # 每条落盘：崩溃后日志可信
            finally:
                os.close(fd)

    def begin(self, token: str, plugin_id: str, previous_version: str) -> None:
        self._append({"event": "begin", "token": token,
                      "plugin_id": plugin_id,
                      "previous_version": previous_version})

    def committed(self, token: str) -> None:
        self._append({"event": "committed", "token": token})

    def aborted(self, token: str, reason: str) -> None:
        self._append({"event": "aborted", "token": token, "reason": reason[:200]})

    def recovered(self, token: str, outcome: str) -> None:
        self._append({"event": "recovered", "token": token, "outcome": outcome})

    def entries(self) -> list[dict[str, Any]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return []
        result = []
        for line in raw.splitlines():
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    result.append(item)
            except ValueError:
                continue   # 崩溃截断的尾行：忽略（下一条 begin 会重新记账）
        return result

    def pending_tokens(self) -> dict[str, dict[str, Any]]:
        """无收尾（committed/aborted/recovered）的 begin 记录。"""
        closed = {item.get("token") for item in self.entries()
                  if item.get("event") in ("committed", "aborted", "recovered")}
        pending: dict[str, dict[str, Any]] = {}
        for item in self.entries():
            if item.get("event") == "begin" and item.get("token") not in closed:
                pending[str(item.get("token"))] = item
        return pending


def fsync_directory(path: Path) -> None:
    """[F14] 目录同步（POSIX）：rename/symlink 落盘后才算事务完成。"""
    if os.name != "posix":
        return   # Windows 无目录 fsync 等价（CreateFile/FlushBuffers 语义
                 # 不同）：日志已逐条 fsync，恢复按磁盘事实判定
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


class _Transaction:
    """One install/uninstall mutation at a time, per process and per host.

    [F13] 跨进程锁按平台选择等价原语（POSIX flock / Windows msvcrt.locking
    ——见 ``install_lock``）；无任何原语的平台由 ``LockUnavailable`` 如实
    拒绝（拒绝≠支持）。线程锁仍包在外层（同进程串行）。
    """

    def __init__(self, thread_lock: threading.RLock, lock_path: Path) -> None:
        self._held = install_lock.HeldTransactionLock(thread_lock, lock_path)

    def __enter__(self) -> "_Transaction":
        try:
            self._held.__enter__()
        except install_lock.LockUnavailable as exc:
            raise InstallerError("LOCK_UNAVAILABLE", str(exc)) from exc
        except OSError as exc:
            raise InstallerError("LOCK_UNAVAILABLE", f"无法取得安装事务锁：{exc}") from exc
        except RuntimeError as exc:
            raise InstallerError("PATH_NOT_OWNED", str(exc)) from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._held.__exit__(*exc_info)


_HAS_DIRFD = bool(getattr(os, "supports_dir_fd", False) and os.supports_dir_fd)


def verify_trusted_root(root: Path, *, boundary: Path | None = None) -> list[Path]:
    """[F12] 证明可信 root 及祖先边界：从 boundary（默认 root 父目录）向下
    逐级 ``lstat``——到 root 为止的每一级都必须是真实目录（无符号链接、
    无中途挂载替身）。返回证明链（boundary→…→root）；任何一级不实即抛
    ``PATH_NOT_OWNED``。
    """
    root = Path(root)
    boundary = Path(boundary) if boundary is not None else root.parent
    try:
        # 词法路径（abspath 不解析符号链接）：链上的每一级都要被 lstat
        # 亲自核验——resolve() 会吞掉链接，证明就失效了
        rel = Path(os.path.abspath(root)).relative_to(os.path.abspath(boundary))
    except (ValueError, OSError) as exc:
        raise InstallerError("PATH_NOT_OWNED",
                             f"root 不在信任边界之内：{root} vs {boundary}") from exc
    proof: list[Path] = [boundary]
    current = boundary
    for part in rel.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise InstallerError("PATH_NOT_OWNED",
                                 f"祖先层级无法核验：{current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise InstallerError("PATH_NOT_OWNED",
                                 f"祖先层级含符号链接，拒绝：{current}")
        if not stat.S_ISDIR(info.st_mode):
            raise InstallerError("PATH_NOT_OWNED",
                                 f"祖先层级不是真实目录：{current}")
        proof.append(current)
    return proof


class _DirAnchor:
    """[F12] fd 锚定目录：子项操作相对 fd 解析（O_NOFOLLOW|O_DIRECTORY）。

    "检查后再使用"的窗口在 fd 打开那一刻关闭——之后的 rename/symlink/
    unlink/open 都不经路径分量解析，并发路径替换无法插入。POSIX 专属；
    平台不支持时调用方回退路径版并如实标注（不冒充已锚定）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        flags = os.O_RDONLY | os.O_DIRECTORY
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        self.fd = os.open(str(path), flags | nofollow)

    def lstat(self, name: str) -> os.stat_result:
        return os.lstat(name, dir_fd=self.fd)

    def exists(self, name: str) -> bool:
        try:
            self.lstat(name)
            return True
        except FileNotFoundError:
            return False

    def is_real_dir(self, name: str) -> bool:
        try:
            return stat.S_ISDIR(self.lstat(name).st_mode)
        except FileNotFoundError:
            return False

    def mkdir(self, name: str) -> None:
        os.mkdir(name, 0o755, dir_fd=self.fd)

    def rename_into(self, source: Path, name: str) -> None:
        # source 为绝对路径（暂存目录在受管 .staging 下）；目标相对锚 fd
        os.rename(str(source), name, dst_dir_fd=self.fd)

    def symlink(self, target: str, name: str) -> None:
        os.symlink(target, name, dir_fd=self.fd)

    def replace_within(self, source_name: str, target_name: str) -> None:
        try:
            os.replace(source_name, target_name,
                       src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except TypeError:
            # 替身 os.replace（测试注入/受限平台）不带 dir_fd 形参：
            # 回退路径版（同一锚目录内，仍原子）
            os.replace(self.path / source_name, self.path / target_name)

    def unlink(self, name: str) -> None:
        os.unlink(name, dir_fd=self.fd)

    def open_exclusive_nofollow(self, name: str) -> "int":
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        return os.open(name, flags | getattr(os, "O_NOFOLLOW", 0),
                       0o644, dir_fd=self.fd)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


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
        # [F14] 持久事务日志：begin/committed/aborted/recovered 全程记账
        self.journal = TransactionJournal(self.install_root)

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

        if self.install_root.is_symlink() or plugin_root.is_symlink():
            return refuse("安装根或插件目录是符号链接")
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
            if sidecar.is_symlink():
                raise InstallerError("PATH_NOT_OWNED", "来源记录不能是符号链接")
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return data if isinstance(data, dict) else None

    def _stored_digest(self, version_dir: Path) -> str:
        sidecar = self._provenance_path(version_dir)
        if sidecar.is_symlink():
            raise InstallerError("PATH_NOT_OWNED", "来源记录不能是符号链接")
        _reject_source_symlinks(version_dir)
        actual = _payload_digest(version_dir)
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("content_digest"), str):
                if data["content_digest"] != actual:
                    raise InstallerError("PROVENANCE_INVALID", "已安装内容与来源摘要不符")
                return actual
        except (OSError, ValueError):
            pass
        return actual   # 无 sidecar 的旧安装：现算

    def _sweep_transactions(self) -> None:
        """锁内清理崩溃事务的残留（.staging 目录与临时指针），失败保留诊断。"""
        notes: list[str] = []
        staging_parent = self.install_root / ".staging"
        if not staging_parent.is_symlink() and staging_parent.is_dir():
            for leftover in staging_parent.iterdir():
                try:
                    if leftover.is_dir() and not leftover.is_symlink():
                        shutil.rmtree(leftover)
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
        notes.extend(self._recover_pending_transactions())
        self.last_cleanup_notes = tuple(notes)

    def _recover_pending_transactions(self) -> list[str]:
        """[F14] 崩溃恢复：按磁盘事实收尾无 committed/aborted 的事务。

        中断后 current/payload/provenance 必须一致——指针可验证且摘要
        相符则前滚记账；否则回指 previous_version 并隔离坏 payload。
        """
        notes: list[str] = []
        for token, record in self.journal.pending_tokens().items():
            plugin_id = str(record.get("plugin_id", ""))
            previous = str(record.get("previous_version", ""))
            if not plugin_id:
                self.journal.recovered(token, "aborted:bad-record")
                continue
            plugin_root = self._plugin_root(plugin_id)
            try:
                current = self._read_pointer(plugin_id, strict=True)
            except InstallerError:
                current = ""
            if current:
                if current == previous:
                    # 交换从未发生（current 仍是 previous）：按磁盘事实，
                    # 这不是前滚——是交换前中止
                    self.journal.recovered(token, "aborted:before-swap")
                    notes.append(f"事务 {token} 崩溃恢复：交换前中止（current 未变）")
                    continue
                try:
                    versions = self._versions_dir(plugin_id)
                    # 恢复期校验比运行期严：provenance 必须在（安装器总在
                    # 交换前写 sidecar）且摘要一致——无 sidecar 的版本不可
                    # 能出自本安装器的完整事务
                    provenance = self.installed_provenance(plugin_id) or {}
                    actual = self._stored_digest(versions / current)
                    if provenance.get("content_digest") != actual:
                        raise InstallerError(
                            "PROVENANCE_INVALID",
                            "崩溃恢复：payload/来源记录不一致")
                    fsync_directory(plugin_root)
                    self.journal.recovered(token, f"committed:{current}")
                    notes.append(f"事务 {token} 崩溃恢复：前滚至 {current}")
                    continue
                except InstallerError as exc:
                    if not previous:
                        # 无可回退版本：隔离坏目录，指针摘除
                        bad = versions / current if current else None
                        if bad is not None and bad.is_dir() and not bad.is_symlink():
                            quarantine = plugin_root / f".quarantined-{token}"
                            try:
                                os.rename(bad, quarantine)
                                notes.append(
                                    f"事务 {token} 恢复：坏 payload 已隔离 {quarantine.name}")
                            except OSError:
                                pass
                        try:
                            (plugin_root / "current").unlink()
                        except OSError:
                            pass
                        self.journal.recovered(token, "rolled_back:no-previous")
                        notes.append(f"事务 {token} 崩溃恢复：无前版可回，指针已摘除")
                        continue
                    # 回指 previous_version（内容仍完整保留）
                    rollback_pointer = plugin_root / f".current.{token}.rollback"
                    try:
                        if rollback_pointer.is_symlink() or rollback_pointer.exists():
                            rollback_pointer.unlink()
                        os.symlink(Path("versions") / previous, rollback_pointer)
                        os.replace(rollback_pointer, plugin_root / "current")
                        fsync_directory(plugin_root)
                        self.journal.recovered(token, f"rolled_back:{previous}")
                        notes.append(
                            f"事务 {token} 崩溃恢复：回滚至 {previous}（{exc.message}）")
                        continue
                    except OSError as recovery_exc:
                        notes.append(
                            f"事务 {token} 回滚失败（可重试）：{recovery_exc}")
                        continue
            # 交换前崩溃：暂存已由上方清扫，无指针动作
            self.journal.recovered(token, "aborted:before-swap")
            notes.append(f"事务 {token} 崩溃恢复：交换前中止，记账收尾")
        return notes

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
        if package.is_dir() and self.install_root.resolve().is_relative_to(package.resolve()):
            raise InstallerError("PACKAGE_SOURCE_OVERLAP", "安装根不能位于包来源目录内")
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
        provenance_written = False
        moved = False
        pointer_swapped = False
        versions_anchor: _DirAnchor | None = None
        root_anchor: _DirAnchor | None = None
        previous_version = ""
        plugin_root: Path | None = None
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
            self.journal.begin(token, plugin_id, self._read_pointer(plugin_id, strict=False))
            _semver_key(manifest.version)  # Also validate first installs, not only upgrades.
            if expected_plugin_id is not None and plugin_id != expected_plugin_id:
                raise InstallerError("PLUGIN_ID_MISMATCH",
                                     f"包身份 {plugin_id} 与期望 {expected_plugin_id} 不符")

            # 3) 逐层所有权核验后的版本簿记
            plugin_root = self._ensure_real_dir(self._plugin_root(plugin_id), what="插件目录")
            # [F12] 插件目录核验后**立即**锚定（O_NOFOLLOW）：之后的一切
            # 子项操作（versions 创建/复核）都相对 fd——核验与使用之间
            # 没有可被替换的路径分量
            try:
                root_anchor = _DirAnchor(plugin_root) if _HAS_DIRFD else None
            except OSError as exc:
                raise InstallerError(
                    "PATH_NOT_OWNED",
                    f"插件目录核验后被并发替换（锚定打开失败），拒绝使用：{exc}") from exc
            if root_anchor is not None:
                if root_anchor.exists("versions"):
                    if not root_anchor.is_real_dir("versions"):
                        raise InstallerError("PATH_NOT_OWNED",
                                             "版本目录是符号链接，拒绝写入")
                else:
                    root_anchor.mkdir("versions")
                versions = plugin_root / "versions"
                try:
                    versions_anchor = _DirAnchor(versions)
                except OSError as exc:
                    raise InstallerError(
                        "PATH_NOT_OWNED",
                        f"版本目录锚定打开失败（可能被并发替换），拒绝使用：{exc}") from exc
            else:
                versions = self._ensure_real_dir(plugin_root / "versions", what="版本目录")
                versions_anchor = None
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
                if version_dir.is_symlink() or not version_dir.is_dir():
                    raise InstallerError("PATH_NOT_OWNED", f"版本目录异常：{version_dir}")
                _reject_source_symlinks(version_dir)
                if _payload_digest(version_dir) != digest:
                    raise InstallerError("VERSION_CONTENT_CONFLICT",
                                         f"保留版本 {manifest.version} 与待安装内容不同")
                # A prior transaction may have crashed after moving the complete
                # payload but before swapping current. Reuse only byte-identical
                # versions; do not delete this pre-existing payload on failure.
            else:
                if versions_anchor is not None:
                    versions_anchor.rename_into(staging, manifest.version)  # [F12]
                else:
                    os.rename(staging, version_dir)
                moved = True
            if versions_anchor is not None:
                # [F12] fd 级复核：就位的是真实目录（不经路径解析）
                if not versions_anchor.is_real_dir(manifest.version):
                    raise InstallerError("PATH_NOT_OWNED", "版本目录就位后被替换，拒绝使用")
            elif version_dir.is_symlink() or not version_dir.is_dir():
                raise InstallerError("PATH_NOT_OWNED", "版本目录就位后被替换，拒绝使用")
            provenance_path = self._provenance_path(version_dir)
            try:
                if provenance_path.is_symlink():
                    raise InstallerError("PATH_NOT_OWNED", "来源记录不能是符号链接")
                provenance = {
                    "plugin_id": plugin_id, "version": manifest.version,
                    "source": "local_package", "content_digest": digest,
                }
                if provenance_path.exists():
                    try:
                        existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as provenance_exc:
                        raise InstallerError("PROVENANCE_INVALID", "保留版本的来源记录不可解析") from provenance_exc
                    if existing_provenance != provenance:
                        raise InstallerError("PROVENANCE_INVALID", "保留版本的来源记录与验证内容不符")
                if not provenance_path.exists():
                    if versions_anchor is not None:
                        # [F12] 锚定开写（O_EXCL|O_NOFOLLOW，相对 fd）
                        sidecar_name = provenance_path.name
                        fd = versions_anchor.open_exclusive_nofollow(sidecar_name)
                        try:
                            os.write(fd, json.dumps(
                                provenance, ensure_ascii=False,
                                sort_keys=True).encode("utf-8"))
                        finally:
                            os.close(fd)
                        provenance_written = True
                    else:
                        with provenance_path.open("x", encoding="utf-8") as provenance_file:
                            provenance_written = True
                            provenance_file.write(json.dumps(provenance, ensure_ascii=False, sort_keys=True))
                temp_pointer = plugin_root / f".current.{token}.tmp"
                if root_anchor is not None:
                    # [F12] 指针创建与原子切换都在锚 fd 内完成
                    temp_name = temp_pointer.name
                    if root_anchor.exists(temp_name):
                        root_anchor.unlink(temp_name)
                    root_anchor.symlink(str(Path("versions") / manifest.version),
                                        temp_name)
                    try:
                        root_anchor.replace_within(temp_name, "current")
                        pointer_swapped = True
                    except OSError:
                        if root_anchor.exists(temp_name):
                            root_anchor.unlink(temp_name)
                        raise
                else:
                    if temp_pointer.is_symlink() or temp_pointer.exists():
                        temp_pointer.unlink()
                    os.symlink(Path("versions") / manifest.version, temp_pointer)
                    try:
                        os.replace(temp_pointer, plugin_root / "current")   # 原子切换
                        pointer_swapped = True
                    except OSError:
                        if temp_pointer.is_symlink() or temp_pointer.exists():
                            temp_pointer.unlink()
                        raise
            except OSError as exc:
                raise InstallerError("INSTALL_IO_ERROR", f"安装 I/O 失败（已回滚）：{exc}") from exc
            if self._read_pointer(plugin_id, strict=True) != manifest.version:
                raise InstallerError("POINTER_INVALID", "指针切换后校验失败")
            fsync_directory(plugin_root)      # [F14] 指针落盘后事务才算完成
            self.journal.committed(token)
            if versions_anchor is not None:
                versions_anchor.close()
                versions_anchor = None
            if root_anchor is not None:
                root_anchor.close()
                root_anchor = None
            outcome = "updated" if previous_version else "installed"
            return InstallOutcome(plugin_id, manifest.version, outcome, previous_version,
                                  rollback_path=str(versions / previous_version)
                                  if previous_version else "")
        except (OSError, InstallerError) as exc:
            if versions_anchor is not None:
                versions_anchor.close()
                versions_anchor = None
            if root_anchor is not None:
                root_anchor.close()
                root_anchor = None
            # [F14] 非崩溃失败即时记账收尾（崩溃路径由恢复按磁盘事实收尾）
            try:
                self.journal.aborted(token, getattr(exc, "code", type(exc).__name__))
            except Exception:
                pass
            # A post-swap validation failure must restore the previous pointer
            # before removing the new payload. If that fails, retain both
            # payloads so current never points at a directory we just deleted.
            if pointer_swapped and plugin_root is not None:
                rollback_pointer = plugin_root / f".current.{token}.rollback.tmp"
                try:
                    if previous_version:
                        os.symlink(Path("versions") / previous_version, rollback_pointer)
                        os.replace(rollback_pointer, plugin_root / "current")
                    else:
                        (plugin_root / "current").unlink()
                except OSError as rollback_exc:
                    raise InstallerError("INSTALL_ROLLBACK_FAILED",
                                         f"安装失败且指针恢复失败，已保留版本目录供恢复：{rollback_exc}") from exc
                finally:
                    rollback_pointer.unlink(missing_ok=True)
            if moved and version_dir is not None:
                shutil.rmtree(version_dir, ignore_errors=True)
            if provenance_written and provenance_path is not None:
                provenance_path.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise InstallerError("INSTALL_IO_ERROR", f"安装 I/O 失败（已回滚）：{exc}") from exc
            raise
        finally:
            if staging.exists():
                try:
                    shutil.rmtree(staging)
                except OSError as cleanup_exc:
                    self.last_cleanup_notes += (f"清理本次暂存失败 {staging.name}：{cleanup_exc}",)

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
