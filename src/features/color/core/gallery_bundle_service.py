"""G03：Gallery bundle 的统一文件事务（rename／move／delete）。

缺口（计划 §5 G03）：rename 的事务只覆盖 4 个 sidecar（漏
``.still.exr.json``）且回滚靠逆序 rename 无崩溃恢复；delete 直接
``os.remove``、个别失败被吞（UI 假装全部成功）；目标冲突只查同名
``.topos``，不查大小写不敏感卷与孤立 sidecar；目录归属只靠
``startswith``。

本服务把 rename／move／delete 收敛为与 R01 抓帧事务同纪律的 journal
事务（staging → installing → committed，每步先落 manifest 再动文件）：

- **单一 manifest 枚举**：全部成员名来自 ``BUNDLE_SUFFIX_BY_ROLE``
  （含 ``.still.exr.json``）；本模块不维护第二份 suffix 清单。
- **前置验证**先于任何文件动作：安全名、目标 ``.topos`` 冲突（含
  ``os.path.normcase`` 大小写不敏感卷）、孤立目标 sidecar、只读目录、
  目录 symlink 越界（realpath 归属）、成员本身是 symlink。
- **rename/move**：同卷两段式（成员先搬进目标目录内隐藏 staging，再
  安装到新名）；跨卷 move 按 copy → fsync → 校验 → 提交 → 清源的可恢
  复事务（源文件在 committed 前不动）。
- **delete**：默认移入根下 ``.trash/<txn>/``（可恢复，明确**不是**工程
  Undo）；``permanent=True`` 仅限用户显式确认动作。逐文件失败进
  ``warnings``，不静默吞掉后假装成功。
- **崩溃恢复**：任一操作开始先 ``recover_gallery_transactions`` 收敛
  上次遗留（backing/installing → 原位放回；committed → 只清 staging）；
  回滚失败保留 journal 供重试／人工恢复，不删证据。

结果统一为 ``GalleryOperationResult``（G01 合同）：reason 是稳定键
（``name_conflict``／``orphan_sidecar_conflict``／``readonly_album``…），
不是给用户看的句子。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.features.color.core.gallery_contracts import (
    BUNDLE_SUFFIX_BY_ROLE,
    GalleryOperationResult,
    OP_PARTIAL,
    OP_REJECTED,
    OP_SUCCESS,
    is_safe_bundle_stem,
    make_gallery_request_id,
)

logger = logging.getLogger(__name__)

TXN_DIR_PREFIX = ".gallery-txn-"
MANIFEST_NAME = "manifest.json"
TRASH_DIR_NAME = ".trash"

STATE_PREPARED = "prepared"
STATE_INSTALLING = "installing"
STATE_COMMITTED = "committed"
STATE_ROLLED_BACK = "rolled_back"

# 稳定拒绝原因（GalleryOperationResult.reason）
REASON_UNSAFE_NAME = "unsafe_name"
REASON_NAME_CONFLICT = "name_conflict"
REASON_NAME_CONFLICT_CASE = "name_conflict_case"
REASON_ORPHAN_SIDECAR = "orphan_sidecar_conflict"
REASON_READONLY = "readonly_album"
REASON_OUTSIDE_ROOT = "outside_root"
REASON_SYMLINK_MEMBER = "symlink_member"
REASON_SOURCE_MISSING = "source_missing"
REASON_IO_ERROR = "io_error"
REASON_ROLLBACK_FAILED = "rollback_failed"


class GalleryBundleService:
    """对既有 User/System 平铺库的 bundle 文件事务服务（无 Qt）。"""

    def __init__(self, root: str) -> None:
        self._root = os.path.realpath(root)

    # ── 成员枚举（单一 manifest 来源） ──────────────────────────────────

    @staticmethod
    def bundle_paths(folder: str, stem: str) -> List[str]:
        """bundle 全部成员路径（.topos ＋ 全部 sidecar，含 exr 声明）。"""
        return [os.path.join(folder, f"{stem}{suffix}")
                for suffix in BUNDLE_SUFFIX_BY_ROLE.values()]

    @staticmethod
    def existing_members(folder: str, stem: str) -> List[str]:
        """bundle 中真实存在的成员（含 dotfile 之外的正常命名）。"""
        return [path for path in GalleryBundleService.bundle_paths(folder, stem)
                if os.path.lexists(path)]

    # ── 前置验证 ───────────────────────────────────────────────────────

    def _resolve_in_root(self, folder: str) -> Optional[str]:
        """目录归属：realpath 后必须位于根内（挡 symlink 越界）。"""
        real = os.path.realpath(os.path.abspath(folder))
        if real == self._root or real.startswith(self._root + os.sep):
            return real
        return None

    def _validate_target(self, folder: str, new_stem: str, *,
                         ignore_stem: Optional[str] = None
                         ) -> Optional[Tuple[str, str]]:
        """目标名校验；返回 (reason, detail) 或 None（通过）。

        - 不安全名（分隔符/../控制字符）→ ``unsafe_name``；
        - 同名/大小写变体 ``.topos`` 已存在 → ``name_conflict(_case)``；
        - 目标 stem 有 sidecar 但无 ``.topos``（孤立 sidecar）→
          ``orphan_sidecar_conflict``——不覆盖不属于本次事务的文件。
        """
        if not is_safe_bundle_stem(new_stem):
            return REASON_UNSAFE_NAME, new_stem
        try:
            entries = os.listdir(folder)
        except OSError as exc:
            return REASON_IO_ERROR, str(exc)
        # 忽略集按 normcase 比较：大小写不敏感卷上的纯大小写改名不算冲突
        ignore = {os.path.normcase(f"{ignore_stem}{suffix}")
                  for suffix in BUNDLE_SUFFIX_BY_ROLE.values()} \
            if ignore_stem else set()
        new_names = {f"{new_stem}{s}": s
                     for s in BUNDLE_SUFFIX_BY_ROLE.values()}
        has_case_variant = False
        has_orphan_sidecar = False
        for entry in entries:
            if os.path.normcase(entry) in ignore:
                continue
            norm = os.path.normcase(entry)
            target_topos = os.path.normcase(f"{new_stem}.topos")
            if norm == target_topos:
                has_case_variant = norm != entry  # 完全同名 or 大小写变体
                for suffix in BUNDLE_SUFFIX_BY_ROLE.values():
                    if norm == os.path.normcase(f"{new_stem}{suffix}"):
                        return (REASON_NAME_CONFLICT_CASE
                                if has_case_variant else REASON_NAME_CONFLICT,
                                entry)
            for suffix in BUNDLE_SUFFIX_BY_ROLE.values():
                if suffix == ".topos":
                    continue
                if norm == os.path.normcase(f"{new_stem}{suffix}"):
                    has_orphan_sidecar = True
        if has_orphan_sidecar:
            return REASON_ORPHAN_SIDECAR, new_stem
        return None

    # ── rename / move ──────────────────────────────────────────────────

    def rename_bundle(self, folder: str, old_stem: str, new_stem: str, *,
                      request_id: Optional[str] = None
                      ) -> GalleryOperationResult:
        return self.move_bundle(folder, old_stem, folder, new_stem,
                                request_id=request_id)

    def move_bundle(self, src_folder: str, stem: str, dst_folder: str,
                    new_stem: Optional[str] = None, *,
                    request_id: Optional[str] = None,
                    _prefer_copy: bool = False
                    ) -> GalleryOperationResult:
        """把 bundle 移动／重命名到目标目录与目标名。

        同卷：两段式 journal 事务（staging → install）；
        跨卷（或 ``_prefer_copy``）：copy → fsync → size 校验 → install →
        committed 后清源。
        """
        request_id = request_id or make_gallery_request_id()
        if not is_safe_bundle_stem(stem):
            return self._rejected(request_id, REASON_UNSAFE_NAME, repr(stem))
        # new_stem=None：move 保持原名；显式空名按不安全名拒绝（不得
        # 静默退化成"改回自己"）
        new_stem = (new_stem.strip() if isinstance(new_stem, str)
                    else stem)
        if not new_stem:
            return self._rejected(request_id, REASON_UNSAFE_NAME,
                                  repr(new_stem))
        src_real = self._resolve_in_root(src_folder)
        dst_real = self._resolve_in_root(dst_folder)
        if src_real is None or dst_real is None:
            return self._rejected(request_id, REASON_OUTSIDE_ROOT,
                                  f"{src_folder} -> {dst_folder}")
        problem = self._validate_target(dst_real, new_stem, ignore_stem=stem
                                        if src_real == dst_real else None)
        if problem is not None:
            return self._rejected(request_id, problem[0], problem[1])
        if not os.access(dst_real, os.W_OK) or not os.access(src_real, os.W_OK):
            return self._rejected(request_id, REASON_READONLY,
                                  f"{src_real} -> {dst_real}")

        # 收敛上次遗留事务（先于成员检查——无论本次结果如何都要恢复）
        self.recover_gallery_transactions(dst_real)
        if dst_real != src_real:
            self.recover_gallery_transactions(src_real)

        members = self.existing_members(src_real, stem)
        if not any(os.path.normcase(os.path.basename(p))
                   == os.path.normcase(f"{stem}.topos") for p in members):
            return self._rejected(request_id, REASON_SOURCE_MISSING,
                                  f"{stem}.topos")
        symlinked = [p for p in members if os.path.islink(p)]
        if symlinked:
            return self._rejected(request_id, REASON_SYMLINK_MEMBER,
                                  "; ".join(map(os.path.basename, symlinked)))

        same_volume = (os.stat(src_real).st_dev == os.stat(dst_real).st_dev)
        use_copy = _prefer_copy or not same_volume
        try:
            if use_copy:
                moved = self._copy_move(members, src_real, dst_real,
                                        new_stem, request_id)
            else:
                moved = self._rename_move(members, src_real, dst_real,
                                          new_stem, request_id)
        except OSError as exc:
            return self._rejected(
                request_id, REASON_IO_ERROR,
                f"{os.strerror(exc.errno) if exc.errno else exc}: {exc}",
                recoverable=True)
        except _RollbackFailed as exc:
            return GalleryOperationResult(
                request_id=request_id, status=OP_REJECTED,
                reason=REASON_ROLLBACK_FAILED, warnings=exc.warnings,
                recoverable=True)
        return GalleryOperationResult(
            request_id=request_id, status=OP_SUCCESS,
            affected_still_ids=(new_stem,),
            warnings=tuple(moved.get("warnings", ())))

    # ── delete（默认可恢复 trash） ─────────────────────────────────────

    def delete_bundle(self, folder: str, stem: str, *,
                      permanent: bool = False,
                      request_id: Optional[str] = None
                      ) -> GalleryOperationResult:
        """删除 bundle：默认移入 ``.trash``（可恢复，非工程 Undo）。

        ``permanent=True`` 是用户显式确认后的直删。逐文件 OSError 进
        ``warnings`` 并以 ``partial`` 如实返回，绝不静默吞掉后报成功。
        """
        request_id = request_id or make_gallery_request_id()
        if not is_safe_bundle_stem(stem):
            return self._rejected(request_id, REASON_UNSAFE_NAME, repr(stem))
        real = self._resolve_in_root(folder)
        if real is None:
            return self._rejected(request_id, REASON_OUTSIDE_ROOT, folder)
        if not os.access(real, os.W_OK):
            return self._rejected(request_id, REASON_READONLY, real)
        self.recover_gallery_transactions(real)
        members = self.existing_members(real, stem)
        if not members:
            return self._rejected(request_id, REASON_SOURCE_MISSING,
                                  f"{stem}.topos")
        symlinked = [p for p in members if os.path.islink(p)]
        if symlinked:
            return self._rejected(request_id, REASON_SYMLINK_MEMBER,
                                  "; ".join(map(os.path.basename, symlinked)))

        failures: List[str] = []
        if permanent:
            for path in members:
                try:
                    os.remove(path)
                except OSError as exc:
                    failures.append(f"{os.path.basename(path)}: {exc}")
            status = OP_SUCCESS if not failures else OP_PARTIAL
            return GalleryOperationResult(
                request_id=request_id, status=status,
                affected_still_ids=(stem,), warnings=tuple(failures))

        trash_root = os.path.join(self._root, TRASH_DIR_NAME)
        try:
            os.makedirs(trash_root, exist_ok=True)
            txn_dir = tempfile.mkdtemp(
                prefix=f"{os.path.basename(real)}__{stem}__", dir=trash_root)
        except OSError as exc:
            return self._rejected(request_id, REASON_IO_ERROR, str(exc),
                                  recoverable=True)
        record = {"kind": "gallery-delete", "request_id": request_id,
                  "source_folder": real, "stem": stem,
                  "members": [os.path.basename(p) for p in members]}
        moved: List[str] = []
        current = ""
        try:
            with open(os.path.join(txn_dir, MANIFEST_NAME), "w",
                      encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=1)
            for path in members:
                current = os.path.basename(path)
                target = os.path.join(txn_dir, current)
                os.replace(path, target)
                moved.append(path)
            _fsync_dir(txn_dir)
            _fsync_dir(real)
        except OSError as exc:
            failures.append(f"{current or stem}: {exc}")
            # 已搬进 trash 的留在 trash（可恢复），源目录残留如实报告
            for path in moved:
                if os.path.lexists(path):
                    failures.append(f"left at source: "
                                    f"{os.path.basename(path)}")
            return GalleryOperationResult(
                request_id=request_id, status=OP_PARTIAL,
                affected_still_ids=(stem,), warnings=tuple(failures),
                recoverable=True)
        return GalleryOperationResult(
            request_id=request_id, status=OP_SUCCESS,
            affected_still_ids=(stem,),
            warnings=(f"moved to trash: {txn_dir}",))

    def list_trash(self) -> List[Dict[str, Any]]:
        """列出可恢复的删除记录（.trash 内含 manifest 的目录）。"""
        trash_root = os.path.join(self._root, TRASH_DIR_NAME)
        records: List[Dict[str, Any]] = []
        if not os.path.isdir(trash_root):
            return records
        for name in sorted(os.listdir(trash_root)):
            manifest = os.path.join(trash_root, name, MANIFEST_NAME)
            if not os.path.isfile(manifest):
                continue
            try:
                with open(manifest, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
                record["trash_id"] = name
                records.append(record)
            except (OSError, ValueError):
                continue
        return records

    def restore_from_trash(self, trash_id: str) -> GalleryOperationResult:
        """把一条 trash 记录搬回原目录（同名冲突时拒绝，不覆盖）。"""
        request_id = make_gallery_request_id()
        if not is_safe_bundle_stem(trash_id):
            return self._rejected(request_id, REASON_UNSAFE_NAME, repr(trash_id))
        trash_root = os.path.join(self._root, TRASH_DIR_NAME)
        txn_dir = os.path.join(trash_root, trash_id)
        manifest = os.path.join(txn_dir, MANIFEST_NAME)
        if not os.path.isfile(manifest):
            return self._rejected(request_id, REASON_SOURCE_MISSING, trash_id)
        try:
            with open(manifest, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError) as exc:
            return self._rejected(request_id, REASON_IO_ERROR, str(exc))
        folder = record.get("source_folder", "")
        stem = record.get("stem", "")
        real = self._resolve_in_root(folder)
        if real is None or not stem:
            return self._rejected(request_id, REASON_OUTSIDE_ROOT, folder)
        problem = self._validate_target(real, stem)
        if problem is not None:
            return self._rejected(request_id, problem[0], problem[1])
        try:
            for member in os.listdir(txn_dir):
                if member == MANIFEST_NAME:
                    continue
                os.replace(os.path.join(txn_dir, member),
                           os.path.join(real, member))
            os.remove(manifest)
            os.rmdir(txn_dir)
        except OSError as exc:
            return self._rejected(request_id, REASON_IO_ERROR, str(exc),
                                  recoverable=True)
        return GalleryOperationResult(
            request_id=request_id, status=OP_SUCCESS,
            affected_still_ids=(stem,))

    # ── 崩溃恢复 ───────────────────────────────────────────────────────

    def recover_gallery_transactions(self, folder: str) -> List[str]:
        """收敛目录内遗留的 ``.gallery-txn-*``（下一操作前自动调用）。"""
        recovered: List[str] = []
        if not os.path.isdir(folder):
            return recovered
        for name in sorted(os.listdir(folder)):
            if not name.startswith(TXN_DIR_PREFIX):
                continue
            txn_dir = os.path.join(folder, name)
            if not os.path.isdir(txn_dir):
                continue
            try:
                with open(os.path.join(txn_dir, MANIFEST_NAME),
                          "r", encoding="utf-8") as fh:
                    manifest = json.load(fh)
            except (OSError, ValueError):
                shutil.rmtree(txn_dir, ignore_errors=True)
                continue
            recovered.append(str(manifest.get("txn_id", name)))
            if manifest.get("state") in (STATE_INSTALLING,):
                _restore_move_entries(manifest.get("entries", []))
            shutil.rmtree(txn_dir, ignore_errors=True)
        if recovered:
            logger.warning("gallery bundle: recovered %d leftover "
                           "transaction(s) in %s: %s",
                           len(recovered), folder, recovered)
        return recovered

    # ── 内部：两段式 rename / copy 事务 ────────────────────────────────

    def _rename_move(self, members: List[str], src_folder: str,
                     dst_folder: str, new_stem: str, request_id: str
                     ) -> Dict[str, Any]:
        txn = _MoveTransaction(src_folder, dst_folder, request_id)
        return txn.run(members, new_stem)

    def _copy_move(self, members: List[str], src_folder: str,
                   dst_folder: str, new_stem: str, request_id: str
                   ) -> Dict[str, Any]:
        txn = _CopyMoveTransaction(src_folder, dst_folder, request_id)
        return txn.run(members, new_stem)

    @staticmethod
    def _rejected(request_id: str, reason: str, detail: str, *,
                  recoverable: bool = False) -> GalleryOperationResult:
        return GalleryOperationResult(
            request_id=request_id, status=OP_REJECTED, reason=reason,
            warnings=(detail,) if detail else (), recoverable=recoverable)


class _RollbackFailed(RuntimeError):
    def __init__(self, warnings: List[str]) -> None:
        super().__init__("; ".join(warnings))
        self.warnings = warnings


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _restore_move_entries(entries: List[Dict[str, Any]]) -> None:
    """installing 中断：撤掉已安装的新名，把 staging 成员放回原位。"""
    for entry in reversed(entries):
        try:
            if entry.get("installed") and os.path.lexists(str(entry["dst"])):
                os.replace(str(entry["dst"]), str(entry["src"]))
            elif (entry.get("staged") and os.path.lexists(str(entry["staged"]))
                    and not os.path.lexists(str(entry["src"]))):
                os.replace(str(entry["staged"]), str(entry["src"]))
        except OSError:
            continue


class _MoveTransaction:
    """同卷 rename/move：成员先进 staging（源目录同卷隐藏目录），再安装。"""

    def __init__(self, src_folder: str, dst_folder: str, txn_id: str) -> None:
        self.src = os.path.abspath(src_folder)
        self.dst = os.path.abspath(dst_folder)
        self.txn_id = txn_id
        self.txn_dir = tempfile.mkdtemp(prefix=TXN_DIR_PREFIX, dir=self.dst)
        self.state = STATE_PREPARED
        self.entries: List[Dict[str, Any]] = []
        self.warnings: List[str] = []

    def run(self, members: List[str], new_stem: str) -> Dict[str, Any]:
        try:
            # 逐成员登记：src / staged / dst（staging 与目标同卷同目录）
            for path in members:
                suffix = _suffix_of(path)
                staged = os.path.join(self.txn_dir, os.path.basename(path))
                dst = os.path.join(self.dst, f"{new_stem}{suffix}")
                self.entries.append({
                    "src": os.path.abspath(path), "staged": staged,
                    "dst": dst, "installed": False})
            self._write()
            self.state = STATE_INSTALLING
            self._write()
            for entry in self.entries:
                os.replace(entry["src"], entry["staged"])
            for entry in self.entries:
                os.replace(entry["staged"], entry["dst"])
                entry["installed"] = True
                self._write()
            _fsync_dir(self.dst)
            if self.src != self.dst:
                _fsync_dir(self.src)
            self.state = STATE_COMMITTED
            self._write()
            shutil.rmtree(self.txn_dir, ignore_errors=True)
            return {"warnings": self.warnings}
        except OSError as exc:
            rollback_errors = self._rollback()
            if rollback_errors:
                # 保留 journal 供恢复；事务目录不清理
                raise _RollbackFailed([
                    *(f"rollback incomplete: {err}"
                      for err in rollback_errors),
                    f"journal kept at {self.txn_dir}",
                    f"original error: {exc}",
                ]) from exc
            shutil.rmtree(self.txn_dir, ignore_errors=True)
            raise

    def _rollback(self) -> List[str]:
        errors: List[str] = []
        for entry in reversed(self.entries):
            try:
                if entry["installed"] and os.path.lexists(entry["dst"]):
                    os.replace(entry["dst"], entry["staged"])
                    entry["installed"] = False
                if os.path.lexists(entry["staged"]) and \
                        not os.path.lexists(entry["src"]):
                    os.replace(entry["staged"], entry["src"])
            except OSError as exc:
                errors.append(f"{os.path.basename(entry['src'])}: {exc}")
        return errors

    def _write(self) -> None:
        _atomic_write_json(os.path.join(self.txn_dir, MANIFEST_NAME), {
            "schema": 1,
            "txn_id": self.txn_id,
            "kind": "gallery-move",
            "state": self.state,
            "entries": [dict(e) for e in self.entries],
        })


def _suffix_of(path: str) -> str:
    """从成员文件名里剥出 bundle 后缀（含 .still.exr.json 双段后缀）。"""
    name = os.path.basename(path)
    for suffix in sorted(BUNDLE_SUFFIX_BY_ROLE.values(), key=len,
                         reverse=True):
        if name.endswith(suffix):
            return suffix
    return os.path.splitext(name)[1]


class _CopyMoveTransaction(_MoveTransaction):
    """跨卷 move：copy → fsync → size 校验 → install → committed 后清源。"""

    def run(self, members: List[str], new_stem: str) -> Dict[str, Any]:
        try:
            for path in members:
                suffix = _suffix_of(path)
                staged = os.path.join(self.txn_dir,
                                      f"copy-{os.path.basename(path)}")
                dst = os.path.join(self.dst, f"{new_stem}{suffix}")
                self.entries.append({
                    "src": os.path.abspath(path), "staged": staged,
                    "dst": dst, "installed": False})
            self._write()
            self.state = STATE_INSTALLING
            self._write()
            for entry in self.entries:
                shutil.copy2(entry["src"], entry["staged"])
                with open(entry["staged"], "rb") as fh:
                    os.fsync(fh.fileno())
                if os.path.getsize(entry["staged"]) != \
                        os.path.getsize(entry["src"]):
                    raise OSError(errno.EIO,
                                  "size mismatch after copy",
                                  entry["src"])
            for entry in self.entries:
                os.replace(entry["staged"], entry["dst"])
                entry["installed"] = True
                self._write()
            _fsync_dir(self.dst)
            self.state = STATE_COMMITTED
            self._write()
            # committed 后才清源：中断时源完好，恢复=清掉目标侧副本
            for entry in self.entries:
                try:
                    os.remove(entry["src"])
                except OSError as exc:
                    self.warnings.append(
                        f"source left behind: "
                        f"{os.path.basename(entry['src'])}: {exc}")
            _fsync_dir(self.src)
            shutil.rmtree(self.txn_dir, ignore_errors=True)
            return {"warnings": self.warnings}
        except OSError as exc:
            rollback_errors = self._rollback_copy()
            if rollback_errors:
                raise _RollbackFailed([
                    *(f"rollback incomplete: {err}"
                      for err in rollback_errors),
                    f"journal kept at {self.txn_dir}",
                    f"original error: {exc}",
                ]) from exc
            shutil.rmtree(self.txn_dir, ignore_errors=True)
            raise

    def _rollback_copy(self) -> List[str]:
        errors: List[str] = []
        for entry in reversed(self.entries):
            try:
                if entry["installed"] and os.path.lexists(entry["dst"]):
                    os.remove(entry["dst"])
                    entry["installed"] = False
                elif os.path.lexists(entry["staged"]):
                    os.remove(entry["staged"])
            except OSError as exc:
                errors.append(f"{os.path.basename(entry['dst'])}: {exc}")
        return errors
