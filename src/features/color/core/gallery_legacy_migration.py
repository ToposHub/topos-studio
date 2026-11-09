"""G10：旧库与脚本 capture 格式的统一迁移（可恢复、不删源）。

旧格式盘点（计划 §5 G10）：

- **平铺 ``.topos`` 族**（含 AppData User/System 与旧 cwd 复制来源）：
  已是 Gallery 原生格式 → 复制迁移（bundle 全成员）。
- **脚本子目录格式** ``<name>/grade.json＋thumbnail.jpg``（旧
  ``capture_still`` 产物，Gallery 扫描不可见）：转换迁移——grade.json
  的 layers **原样**进 ``project.json``（未知字段/插件数据保留），
  thumbnail.jpg 解码后按现行口径写 thumb／still sidecar。

原则：**复制不删源**；迁移经目标目录 staging 一次安装（journal 可恢复
——断电/取消可重跑）；重跑不重复（持久 mapping 记录 source→dest＋
源 mtime，已迁移且未变化的来源跳过）；只读来源只查询（迁移只读源、
写目标 album）；冲突预览在 ``scan``（不写任何东西），确认走 ``migrate``。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from src.features.color.core.gallery_bundle_service import (
    GalleryBundleService,
    REASON_IO_ERROR,
    _atomic_write_json,
    _fsync_dir,
)
from src.features.color.core.gallery_contracts import (
    GalleryOperationResult,
    OP_PARTIAL,
    OP_SUCCESS,
    make_gallery_request_id,
)
from src.features.color.core.preset_meta import PresetMeta, save_preset_meta

logger = logging.getLogger(__name__)

MIGRATION_LOG_NAME = ".gallery-migration-log.json"
MIGRATION_TXN_PREFIX = ".gallery-migrate-"

KIND_COPY = "copy-topos"
KIND_CONVERT = "convert-script"

# 稳定拒绝原因
REASON_SOURCE_ROOT_MISSING = "source_root_missing"
REASON_TARGET_INVALID = "target_invalid"


class LegacyScanItem:
    """盘点条目（name/kind/成员/冲突）。"""

    def __init__(self, name: str, kind: str, source: str,
                 members: List[str], conflict: Optional[str],
                 base_dir: Optional[str] = None,
                 stem: Optional[str] = None) -> None:
        self.name = name
        self.kind = kind          # KIND_COPY / KIND_CONVERT
        self.source = source      # 迁移映射用的源标识（.topos 路径或目录）
        self.members = members
        self.conflict = conflict  # 目标同名冲突（预览用；"migrated"=已迁）
        self.base_dir = base_dir  # copy 项：bundle 所在目录
        self.stem = stem          # copy 项：bundle 主名

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind,
                "source": self.source, "members": len(self.members),
                "conflict": self.conflict}


class GalleryLegacyMigration:
    """旧库/脚本格式 → 现行 bundle 的可恢复迁移（core，无 Qt）。"""

    def __init__(self, repository,
                 bundle_service: Optional[GalleryBundleService] = None
                 ) -> None:
        self._repository = repository
        self._bundles = bundle_service or GalleryBundleService(
            repository.root)

    # ── 盘点（只读；冲突预览） ─────────────────────────────────────────

    def scan(self, legacy_root: str,
             target_album_id: str = "legacy:User") -> List[LegacyScanItem]:
        target = self._repository.album_folder(target_album_id) or ""
        items: List[LegacyScanItem] = []
        if not os.path.isdir(legacy_root):
            return items
        existing = set(os.listdir(target)) if os.path.isdir(target) else set()
        migrated = self._migration_log(target)
        for name in sorted(os.listdir(legacy_root)):
            if name.startswith("."):
                continue
            source = os.path.join(legacy_root, name)
            if os.path.isdir(source):
                if os.path.isfile(os.path.join(source, "grade.json")):
                    if self._already_migrated(migrated, source):
                        continue     # 已迁移且源未变：不重复（含目标同名
                                     # ——那是本次迁移自己的产物，非冲突）
                    conflict = name if f"{name}.topos" in existing else None
                    items.append(LegacyScanItem(
                        name, KIND_CONVERT, source,
                        self._legacy_members(source), conflict))
                elif os.path.isfile(os.path.join(source, f"{name}.topos")):
                    items.append(self._copy_item(
                        source, name, existing, migrated,
                        source_marker=source))
            elif name.endswith(".topos"):
                stem = name[:-len(".topos")]
                items.append(self._copy_item(
                    legacy_root, stem, existing, migrated,
                    source_marker=source))
        return items

    def _copy_item(self, folder: str, stem: str, existing: set,
                   migrated: Dict[str, Any], *,
                   source_marker: str) -> LegacyScanItem:
        if self._already_migrated(migrated, source_marker):
            return LegacyScanItem(stem, KIND_COPY, source_marker, [],
                                  "migrated", base_dir=folder, stem=stem)
        members = self._bundles.existing_members(folder, stem)
        conflict = stem if f"{stem}.topos" in existing else None
        return LegacyScanItem(stem, KIND_COPY, source_marker, members,
                              conflict, base_dir=folder, stem=stem)

    @staticmethod
    def _legacy_members(legacy_dir: str) -> List[str]:
        members = []
        for name in ("grade.json", "thumbnail.jpg"):
            if os.path.isfile(os.path.join(legacy_dir, name)):
                members.append(name)
        return members

    # ── 迁移（复制/转换 → staging → 一次安装 → mapping 落盘） ─────────

    def migrate(self, legacy_root: str, target_album_id: str = "legacy:User",
                *, conflict_policy: str = "suffix"
                ) -> GalleryOperationResult:
        """把 ``scan`` 盘点的条目迁入目标 album（不删源；可重跑）。"""
        request_id = make_gallery_request_id()
        if not os.path.isdir(legacy_root):
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=REASON_SOURCE_ROOT_MISSING, warnings=(legacy_root,))
        target = self._repository.album_folder(target_album_id)
        if target is None or not os.path.isdir(target) \
                or not os.access(target, os.W_OK):
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=REASON_TARGET_INVALID, warnings=(target_album_id,))

        items = [item for item in self.scan(legacy_root, target_album_id)
                 if item.conflict != "migrated"]
        # 冲突按策略处理：suffix 自动后缀（结果可审阅）；skip 跳过
        planned: List[Tuple[LegacyScanItem, str]] = []
        skipped: List[str] = []
        for item in items:
            if item.conflict and conflict_policy == "skip":
                skipped.append(item.name)
                continue
            planned.append((item, self._unoccupied(target, item.name)))
        self._recover_migration_txns(target)

        txn_dir = tempfile.mkdtemp(prefix=MIGRATION_TXN_PREFIX, dir=target)
        journal_path = os.path.join(txn_dir, "journal.json")
        journal: Dict[str, Any] = {"state": "installing", "finals": [],
                                   "entries": []}
        warnings: List[str] = []
        failures: List[str] = []
        try:
            _atomic_write_json(journal_path, journal)
            mapping_entries: List[Dict[str, Any]] = []
            for item, dest_stem in planned:
                try:
                    self._materialize(txn_dir, item, dest_stem)
                    # journal-first：每个 final 先登记再安装（恢复无孤儿）
                    finals = self._install(target, txn_dir, dest_stem,
                                           journal, journal_path)
                    mapping_entries.append({
                        "source": os.path.abspath(item.source),
                        "source_mtime_ns": self._mtime(item.source),
                        "dest": dest_stem,
                        "kind": item.kind})
                except Exception as exc:   # 单条失败不拖垮整批
                    failures.append(f"{item.name}: {exc}")
            journal["state"] = "committed"
            _atomic_write_json(journal_path, journal)
            self._append_migration_log(target, mapping_entries)
        except OSError as exc:
            for final in reversed(journal.get("finals", [])):
                try:
                    os.remove(final)
                except OSError:
                    pass
            shutil.rmtree(txn_dir, ignore_errors=True)
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=REASON_IO_ERROR, warnings=(str(exc),),
                recoverable=True)
        _fsync_dir(target)
        shutil.rmtree(txn_dir, ignore_errors=True)
        if skipped:
            warnings.append(f"skipped conflicts: {', '.join(skipped)}")
        warnings.extend(failures)
        status = OP_SUCCESS if not failures else OP_PARTIAL
        return GalleryOperationResult(
            request_id=request_id, status=status,
            affected_still_ids=tuple(stem for _, stem in planned),
            warnings=tuple(warnings))

    # ── 内部 ───────────────────────────────────────────────────────────

    def _materialize(self, txn_dir: str, item: LegacyScanItem,
                     dest_stem: str) -> str:
        """把条目写进 staging（copy：全成员；convert：grade→zip＋缩略图）。"""
        staged_topos = os.path.join(txn_dir, f"{dest_stem}.topos")
        if item.kind == KIND_COPY:
            for member in self._bundles.existing_members(
                    item.base_dir or "", item.stem or item.name):
                suffix = os.path.basename(member)[len(item.stem or
                                                      item.name):]
                shutil.copy2(member, os.path.join(
                    txn_dir, f"{dest_stem}{suffix}"))
            return staged_topos
        # convert：脚本子目录格式
        with open(os.path.join(item.source, "grade.json"),
                  "r", encoding="utf-8") as fh:
            grade = json.load(fh)
        project = {
            "layers": grade.get("layers") or [],      # 原样（未知字段保留）
            "active_layer_index": grade.get("active_layer_index", 0),
        }
        with zipfile.ZipFile(staged_topos, "w") as zf:
            zf.writestr("project.json",
                        json.dumps(project, ensure_ascii=False, indent=1))
        thumbnail = os.path.join(item.source, "thumbnail.jpg")
        if os.path.isfile(thumbnail):
            image = self._decode_jpeg(thumbnail)
            if image is not None:
                from src.features.color.core.preset_meta import (
                    write_still_images,
                )
                write_still_images(staged_topos, image)
        save_preset_meta(staged_topos, PresetMeta(label=item.name))
        return staged_topos

    def _install(self, target: str, txn_dir: str, dest_stem: str,
                 journal: Dict[str, Any], journal_path: str) -> List[str]:
        """journal-first 安装：每个 final 先登记再替换（恢复无孤儿）。"""
        finals: List[str] = []
        for name in sorted(os.listdir(txn_dir)):
            if name == "journal.json" or not name.startswith(dest_stem):
                continue
            final = os.path.join(target, name)
            journal["finals"].append(final)
            _atomic_write_json(journal_path, journal)
            os.replace(os.path.join(txn_dir, name), final)
            finals.append(final)
        return finals

    def _unoccupied(self, folder: str, stem: str) -> str:
        candidate, counter = stem, 1
        while os.path.exists(os.path.join(folder, f"{candidate}.topos")):
            candidate = f"{stem}_{counter}"
            counter += 1
        return candidate

    @staticmethod
    def _decode_jpeg(path: str):
        import cv2
        image = cv2.imread(path, cv2.IMREAD_COLOR)
        return image if image is not None else None

    @staticmethod
    def _mtime(path: str) -> int:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return 0

    # ── 持久 mapping（重跑不重复） ─────────────────────────────────────

    @staticmethod
    def _migration_log_path(target: str) -> str:
        return os.path.join(target, MIGRATION_LOG_NAME)

    def _migration_log(self, target: str) -> Dict[str, Any]:
        path = self._migration_log_path(target)
        if not os.path.isfile(path):
            return {"entries": []}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {"entries": []}
        except (OSError, ValueError):
            return {"entries": []}

    def _already_migrated(self, log: Dict[str, Any], source: str) -> bool:
        source = os.path.abspath(source)
        for entry in log.get("entries", []):
            if entry.get("source") == source and \
                    entry.get("source_mtime_ns") == self._mtime(source):
                return True
        return False

    def _append_migration_log(self, target: str,
                              entries: List[Dict[str, Any]]) -> None:
        if not entries:
            return
        log = self._migration_log(target)
        log.setdefault("entries", []).extend(entries)
        _atomic_write_json(self._migration_log_path(target), log)

    def _recover_migration_txns(self, folder: str) -> None:
        if not os.path.isdir(folder):
            return
        for name in sorted(os.listdir(folder)):
            if not name.startswith(MIGRATION_TXN_PREFIX):
                continue
            txn_dir = os.path.join(folder, name)
            try:
                with open(os.path.join(txn_dir, "journal.json"),
                          "r", encoding="utf-8") as fh:
                    journal = json.load(fh)
            except (OSError, ValueError):
                shutil.rmtree(txn_dir, ignore_errors=True)
                continue
            if journal.get("state") != "committed":
                for final in reversed(journal.get("finals", [])):
                    try:
                        os.remove(final)
                    except OSError:
                        pass
            shutil.rmtree(txn_dir, ignore_errors=True)
