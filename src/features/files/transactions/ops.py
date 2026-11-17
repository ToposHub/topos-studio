"""安全文件事务核心（B 阶段，总计划 B 表第 1~3 行）。

原则：
- **可证明 no-replace**（文件）：目标存在性判断交给内核——同卷用
  ``os.link``（EEXIST 即冲突）+ ``os.unlink``，跨卷用目标目录内 staging
  临时文件 + ``link`` 发布。绝不用"先占位再无条件 rename"。
- **目录**没有内核级 no-replace rename（POSIX rename 对空目录会替换）：
  采用 检查→rename→事后核验，失败/中断由 journal（B3）补偿；本模块
  如实区分两种口径，不冒充目录也内核原子。
- **软链接政策**：源末级是符号链接一律拒绝（绝不静默解引用）；目标
  末级已存在符号链接按"已存在"处理，绝不穿过它写入。
- **策略**：error（冲突即失败）/ rename（自动唯一名 ``name 2.ext``）/
  overwrite（仅文件；目录覆盖一律拒绝）。
- **取消**：协作式——条目间与拷贝块边界检查 CancelToken。
- **错误分类**：OSError errno → 结构化 code + retriable，权限/磁盘满
  不得伪装成功。

单一领域入口：rename/move/copy/mkdir；批改名、任务 move、回收站恢复
（B4/B5/B6）都走这里，不自建第二条 FS 写路径。
"""
from __future__ import annotations

import errno
import os
import stat
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

CHUNK_SIZE = 1024 * 1024
TMP_SUFFIX = ".topos-tmp"


class CancelToken:
    """协作式取消：请求后各检查点尽快停下，不强行终止系统调用。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()


class IdentityJournal(Protocol):
    """B3 持久 journal 的接口（B2 只依赖该协议；None 时纯内存运行）。"""

    def record(self, op: str, state: str, detail: dict) -> None:  # pragma: no cover
        ...


class OpError(Exception):
    """结构化文件操作错误：code 稳定、retriable 明确、不吞 errno。"""

    RETRIABLE = {"PERMISSION_DENIED", "DISK_FULL", "IO_ERROR", "DEST_BUSY"}

    def __init__(self, code: str, message: str, *, errno_code: int | None = None,
                 source: str = "", destination: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.errno_code = errno_code
        self.retriable = code in self.RETRIABLE
        self.source = source
        self.destination = destination


def _map_oserror(exc: OSError, source: str = "", destination: str = "") -> OpError:
    if exc.errno in (errno.EACCES, errno.EPERM):
        code = "PERMISSION_DENIED"
    elif exc.errno == errno.ENOSPC:
        code = "DISK_FULL"
    elif exc.errno == errno.ENOENT:
        code = "SOURCE_MISSING"
    elif exc.errno == errno.EEXIST:
        code = "DEST_EXISTS"
    elif exc.errno == errno.ENOTEMPTY:
        code = "DEST_NOT_EMPTY"
    else:
        code = "IO_ERROR"
    return OpError(code, str(exc) or exc.__class__.__name__,
                   errno_code=exc.errno, source=source, destination=destination)


@dataclass
class OpResult:
    source: str
    destination: str = ""
    status: str = "done"  # done | conflict | failed | cancelled
    code: str = ""
    message: str = ""
    staged_tmp: str = ""  # 中断时保留的 staging 路径（可恢复证据）

    @property
    def ok(self) -> bool:
        return self.status == "done"


def _nfc(path: str | Path) -> str:
    return unicodedata.normalize("NFC", str(path))


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _map_oserror(exc, str(path)) from exc


def unique_suffixed(path: Path, occupied: set[str] | None = None) -> Path:
    """``name.ext`` → ``name 2.ext``、``name 3.ext`` …（占用集合可注入）。"""
    occupied = occupied or set()
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    candidate = path
    index = 2
    while candidate.exists() or candidate.is_symlink() or _nfc(candidate) in occupied:
        candidate = parent / f"{stem} {index}{suffix}"
        index += 1
    return candidate


def same_volume(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b.parent).st_dev
    except OSError:
        return False


class FileTransactionService:
    """统一安全文件操作入口；线程内串行由目标 claim 锁保证。"""

    def __init__(self, *, journal: IdentityJournal | None = None) -> None:
        self._journal = journal
        self._claim_lock = threading.Lock()
        self._claims: set[str] = set()

    # ------------------------------------------------------------ claim

    def _claim(self, key: str) -> None:
        with self._claim_lock:
            if key in self._claims:
                raise OpError("DEST_BUSY", "目标正被另一操作处理", destination=key)
            self._claims.add(key)

    def _release(self, key: str) -> None:
        with self._claim_lock:
            self._claims.discard(key)

    def _record(self, op: str, state: str, detail: dict) -> None:
        if self._journal is not None:
            self._journal.record(op, state, detail)

    # ------------------------------------------------------------ 入口

    def rename(self, source: str | Path, new_name: str, *,
               policy: str = "error", cancel: CancelToken | None = None,
               on_identity_moved: Callable[[str, str], None] | None = None) -> OpResult:
        """同目录改名。new_name 只允许末级名字（不含路径分隔符）。"""
        src = Path(source)
        if os.sep in new_name or (os.altsep and os.altsep in new_name):
            return OpResult(str(src), status="failed", code="BAD_NAME",
                            message="new_name 必须是末级名字")
        destination = src.parent / new_name
        return self._transfer(src, destination, policy=policy, remove_source=True,
                              cancel=cancel, on_identity_moved=on_identity_moved)

    def move(self, source: str | Path, destination_dir: str | Path, *,
             policy: str = "error", cancel: CancelToken | None = None,
             on_identity_moved: Callable[[str, str], None] | None = None) -> OpResult:
        src = Path(source)
        dst_dir = Path(destination_dir)
        destination = dst_dir / src.name
        return self._transfer(src, destination, policy=policy, remove_source=True,
                              cancel=cancel, on_identity_moved=on_identity_moved)

    def copy(self, source: str | Path, destination_dir: str | Path, *,
             policy: str = "error", cancel: CancelToken | None = None) -> OpResult:
        src = Path(source)
        dst_dir = Path(destination_dir)
        destination = dst_dir / src.name
        return self._transfer(src, destination, policy=policy, remove_source=False,
                              cancel=cancel, on_identity_moved=None)

    def mkdir(self, path: str | Path) -> OpResult:
        target = Path(path)
        existing = _lstat(target)
        if existing is not None:
            if stat.S_ISDIR(existing.st_mode) and not target.is_symlink():
                return OpResult(str(target), str(target), status="conflict",
                                code="DEST_EXISTS", message="目录已存在")
            return OpResult(str(target), status="conflict", code="DEST_EXISTS",
                            message="同名非目录项已存在")
        try:
            os.makedirs(target, exist_ok=False)
        except OSError as exc:
            raise _map_oserror(exc, str(target)) from exc
        return OpResult(str(target), str(target))

    # ------------------------------------------------------------ 核心

    def _transfer(self, src: Path, destination: Path, *, policy: str, remove_source: bool,
                  cancel: CancelToken | None,
                  on_identity_moved: Callable[[str, str], None] | None) -> OpResult:
        if policy not in ("error", "rename", "overwrite"):
            raise OpError("BAD_POLICY", f"未知策略 {policy}")
        if cancel is not None and cancel.requested:
            return OpResult(str(src), status="cancelled", code="CANCELLED")

        source_stat = _lstat(src)
        if source_stat is None:
            return OpResult(str(src), status="failed", code="SOURCE_MISSING",
                            message="源不存在")
        if stat.S_ISLNK(source_stat.st_mode):
            return OpResult(str(src), status="failed", code="SOURCE_SYMLINK",
                            message="拒绝操作符号链接源（不解引用）")
        is_dir = stat.S_ISDIR(source_stat.st_mode)

        dst_parent = destination.parent
        parent_stat = _lstat(dst_parent)
        if parent_stat is None or not stat.S_ISDIR(parent_stat.st_mode):
            return OpResult(str(src), str(destination), status="failed",
                            code="DEST_PARENT_MISSING", message="目标父目录不存在")
        if destination.is_symlink():
            return OpResult(str(src), str(destination), status="conflict",
                            code="DEST_SYMLINK", message="目标已是符号链接，拒绝穿过写入")

        claim_key = _nfc(destination)
        try:
            self._claim(claim_key)
        except OpError as exc:
            return OpResult(str(src), str(destination), status="failed",
                            code=exc.code, message=str(exc))
        try:
            return self._execute(src, destination, source_stat, is_dir=is_dir,
                                 policy=policy, remove_source=remove_source,
                                 cancel=cancel, on_identity_moved=on_identity_moved)
        finally:
            self._release(claim_key)

    def _execute(self, src: Path, destination: Path, source_stat: os.stat_result, *,
                 is_dir: bool, policy: str, remove_source: bool,
                 cancel: CancelToken | None,
                 on_identity_moved: Callable[[str, str], None] | None) -> OpResult:
        exists_now = _lstat(destination) is not None
        if exists_now and policy == "error":
            return OpResult(str(src), str(destination), status="conflict",
                            code="DEST_EXISTS", message="目标已存在")
        if is_dir and policy == "overwrite":
            return OpResult(str(src), str(destination), status="failed",
                            code="POLICY_FORBIDDEN", message="目录不允许 overwrite")
        final = unique_suffixed(destination) if (exists_now and policy == "rename") else destination

        try:
            if is_dir:
                self._transfer_dir(src, final, remove_source=remove_source, cancel=cancel)
            elif same_volume(src, final):
                self._transfer_file_same_volume(
                    src, final, source_stat, remove_source=remove_source,
                    overwrite=(policy == "overwrite"))
            else:
                self._transfer_file_cross_volume(
                    src, final, remove_source=remove_source, cancel=cancel,
                    overwrite=(policy == "overwrite"))
        except OpError as exc:
            if exc.code == "CANCELLED":
                return OpResult(str(src), str(final), status="cancelled",
                                code="CANCELLED", message="已取消；无半成品目标")
            return OpResult(str(src), str(final), status="failed", code=exc.code,
                            message=str(exc))
        if remove_source and on_identity_moved is not None:
            on_identity_moved(_nfc(src), _nfc(final))
        return OpResult(str(src), str(final))

    # ------------------------------------------------------------ 文件：可证明 no-replace

    def _transfer_file_same_volume(self, src: Path, dst: Path,
                                   source_stat: os.stat_result, *,
                                   remove_source: bool, overwrite: bool) -> None:
        """同卷：默认 link（内核保证目标不存在）→（move 时）unlink 源。

        link 失败 EEXIST 即冲突——存在性判断在内核，不在用户态检查里。
        overwrite 策略才允许 os.replace（原子替换，仅文件）。
        """
        try:
            if overwrite:
                # replace 自身原子且已消费源；无法回退，发布前不再做
                # 可逆校验（旧目标内容按策略让位）
                os.replace(src, dst)
                self._record("move" if remove_source else "copy", "done",
                             {"source": str(src), "destination": str(dst), "policy": "overwrite"})
                return
            os.link(src, dst)
        except FileExistsError as exc:
            raise OpError("DEST_EXISTS", "目标已存在",
                          source=str(src), destination=str(dst)) from exc
        except OSError as exc:
            raise _map_oserror(exc, str(src), str(dst)) from exc
        # link 后核验 inode 一致，防御 link 与 unlink 之间源被替换
        if os.stat(dst).st_ino != source_stat.st_ino:  # pragma: no cover
            os.unlink(dst)
            raise OpError("IO_ERROR", "发布后 inode 不一致，已回退",
                          source=str(src), destination=str(dst))
        if remove_source:
            try:
                os.unlink(src)
            except OSError as exc:  # 极小窗口：unlink 失败则回滚发布的链接
                try:
                    os.unlink(dst)
                except OSError:
                    pass
                raise _map_oserror(exc, str(src), str(dst)) from exc
            self._record("move", "done", {"source": str(src), "destination": str(dst)})
        else:
            self._record("copy", "done", {"source": str(src), "destination": str(dst)})

    def _transfer_file_cross_volume(self, src: Path, dst: Path, *,
                                    remove_source: bool, cancel: CancelToken | None,
                                    overwrite: bool) -> None:
        """跨卷：目标目录内 staging 拷贝 → fsync → link/replace 发布。

        中断/失败清理 staging；取消发生在块边界，不留半成品目标。
        """
        token = f"{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns():x}"
        tmp = dst.parent / f".{dst.name}.{token}{TMP_SUFFIX}"
        self._record("copy", "staged", {"source": str(src), "tmp": str(tmp)})
        try:
            try:
                self._copy_payload(src, tmp, cancel=cancel)
                os.chmod(tmp, stat.S_IMODE(os.stat(src).st_mode))
                if overwrite:
                    os.replace(tmp, dst)
                else:
                    os.link(tmp, dst)  # 同目录：link 可用且内核级 no-replace
            except FileExistsError as exc:
                raise OpError("DEST_EXISTS", "目标已存在",
                              source=str(src), destination=str(dst)) from exc
            except OpError:
                raise
            except OSError as exc:
                raise _map_oserror(exc, str(src), str(tmp)) from exc
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError:
                pass  # 清理失败不掩盖主错误
        if remove_source:
            try:
                os.unlink(src)
            except OSError as exc:
                raise _map_oserror(exc, str(src), str(dst)) from exc
            self._record("move", "done", {"source": str(src), "destination": str(dst)})

    def _copy_payload(self, src: Path, tmp: Path, *,
                      cancel: CancelToken | None) -> None:
        source_stat = os.lstat(src)
        if stat.S_ISLNK(source_stat.st_mode):  # 拷贝期间源被换成链接：拒绝
            raise OpError("SOURCE_SYMLINK", "拷贝期间源被替换为符号链接",
                          source=str(src))
        with open(src, "rb") as fin, open(tmp, "xb") as fout:
            opened_stat = os.fstat(fin.fileno())
            if opened_stat.st_ino != source_stat.st_ino:  # 检查与使用之间被换
                raise OpError("IO_ERROR", "源在打开时被替换",
                              source=str(src), destination=str(tmp))
            while True:
                if cancel is not None and cancel.requested:
                    raise OpError("CANCELLED", "取消请求", source=str(src),
                                  destination=str(tmp))
                chunk = fin.read(CHUNK_SIZE)
                if not chunk:
                    break
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        os.utime(tmp, (source_stat.st_atime, source_stat.st_mtime))

    # ------------------------------------------------------------ 目录：检查+rename+核验

    def _transfer_dir(self, src: Path, dst: Path, *, remove_source: bool,
                      cancel: CancelToken | None) -> None:
        """目录无内核级 no-replace rename（POSIX 对空目标目录会静默替换）。

        口径：检查 → rename → 事后核验；非内核原子，中断窗口由 journal
        记录（B3 恢复）。此处不冒充原子。
        """
        existing = _lstat(dst)
        if existing is not None:
            raise OpError("DEST_EXISTS", "目标目录已存在",
                          source=str(src), destination=str(dst))
        if cancel is not None and cancel.requested:
            raise OpError("CANCELLED", "取消请求", source=str(src))
        if not remove_source:  # 目录 copy
            self._record("copy_dir", "staged", {"source": str(src), "tmp": str(dst)})
            self._copy_dir_payload(src, dst, cancel=cancel)
            return
        self._record("move_dir", "renaming", {"source": str(src), "destination": str(dst)})
        try:
            os.rename(src, dst)
        except OSError as exc:
            raise _map_oserror(exc, str(src), str(dst)) from exc
        if _lstat(src) is not None or _lstat(dst) is None:  # pragma: no cover
            raise OpError("IO_ERROR", "目录 rename 后核验失败",
                          source=str(src), destination=str(dst))

    def _copy_dir_payload(self, src: Path, dst: Path, *,
                          cancel: CancelToken | None) -> None:
        os.mkdir(dst)
        try:
            with os.scandir(src) as it:
                for entry in it:
                    if cancel is not None and cancel.requested:
                        raise OpError("CANCELLED", "取消请求", source=str(src))
                    child_src = src / entry.name
                    child_dst = dst / entry.name
                    if entry.is_symlink():
                        continue  # 不复制链接（与源链接拒绝政策一致）
                    if entry.is_dir(follow_symlinks=False):
                        self._copy_dir_payload(child_src, child_dst, cancel=cancel)
                    else:
                        token = f"{os.getpid()}-{time.monotonic_ns():x}"
                        tmp = child_dst.parent / f".{child_dst.name}.{token}{TMP_SUFFIX}"
                        try:
                            self._copy_payload(child_src, tmp, cancel=cancel)
                            os.chmod(tmp, stat.S_IMODE(os.lstat(child_src).st_mode))
                            os.link(tmp, child_dst)
                        finally:
                            try:
                                os.unlink(tmp)
                            except OSError:
                                pass
        except BaseException:
            _remove_tree_best_effort(dst)
            raise
        os.chmod(dst, stat.S_IMODE(os.lstat(src).st_mode))


def _remove_tree_best_effort(path: Path) -> None:
    try:
        import shutil

        shutil.rmtree(path)
    except OSError:
        pass
