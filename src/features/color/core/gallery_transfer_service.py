"""G08：Gallery bundle 导入／导出——可移植包、限制与身份冲突政策。

分清五种动作（计划 §5 G08）：导入图像为参考（既有 reference 链，非本
模块）、**导入 grade bundle**、导出参考图（既有 thumbnail/still 复制，
非本模块）、**导出完整 grade bundle**（本模块）、导出 thumbnail（既有）。

- **导出**：bundle 全成员（六角色，按需含 EXR 对）＋ ``bundle_manifest``
  （schema/still 身份/每文件 sha256/精度/alpha 声明）打包为单个
  ``.toposbundle`` zip——staging 写入后一次 ``os.replace``，失败/取消不
  留半目标；**源 bundle 一律不动**。成员只存相对文件名，不携带机器
  绝对路径/secret；grade 内 LUT 引用按原样保留（应用时经
  ``_prepare_preset_layers`` 既有可移植过滤），manifest 汇总 LUT 引用数。
- **导入**：zip 安全限制（文件数／解压总量／单文件上限／纯文件名归属／
  symlink 条目拒绝／不执行任何内含内容）；manifest schema 已知才继续
  （未来 schema 拒绝）；逐文件 sha256 校验后经 dest 目录内 staging 一次
  安装——journal 可恢复，拒绝时不落任何散文件。
- **身份冲突**（G02 政策）：导入的 still_id 与目标库既有条目冲突时——
  同内容＝重复拒绝；不同内容＝保留外部 origin ID（meta ``origin_still_id``）
  并分配本库新 ID。重名文件自动后缀分配，结果里给出**实际采用名**供
  审阅；不做静默覆盖。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from src.features.color.core.gallery_bundle_service import (
    BUNDLE_SUFFIX_BY_ROLE,
    GalleryBundleService,
    REASON_IO_ERROR,
    REASON_OUTSIDE_ROOT,
    REASON_SOURCE_MISSING,
    REASON_UNSAFE_NAME,
)
from src.features.color.core.gallery_contracts import (
    BUNDLE_ROLE_GRADE,
    BUNDLE_ROLE_STILL_EXR,
    GalleryOperationResult,
    OP_SUCCESS,
    is_safe_bundle_stem,
    make_gallery_request_id,
)
from src.features.color.core.preset_meta import (
    PresetMeta,
    allocate_still_id,
    load_preset_meta,
    save_preset_meta,
    still_alpha_declaration,
)

logger = logging.getLogger(__name__)

#: 可移植包信封名（zip 内的清单）
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"
#: 导入限制（zip bomb 防线）
IMPORT_MAX_FILES = 16
IMPORT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
IMPORT_MAX_FILE_BYTES = 128 * 1024 * 1024
IMPORT_TXN_PREFIX = ".gallery-import-"

EXPORT_SCHEMA = 1

# 稳定拒绝原因
REASON_NOT_A_BUNDLE = "not_a_bundle"
REASON_FUTURE_SCHEMA = "future_schema"
REASON_MISSING_GRADE = "missing_grade"
REASON_PATH_ESCAPE = "path_escape"
REASON_SIZE_LIMIT = "size_limit"
REASON_FILE_COUNT_LIMIT = "file_count_limit"
REASON_HASH_MISMATCH = "hash_mismatch"
REASON_DUPLICATE_IDENTITY = "duplicate_identity"

_ROLE_BY_SUFFIX = {suffix: role for role, suffix
                   in BUNDLE_SUFFIX_BY_ROLE.items()}


def _sha256_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_safe_member_name(name: str) -> bool:
    """zip 成员必须是纯文件名（拒绝绝对路径/../分隔符）。"""
    if not name or name.startswith(("/", "\\")):
        return False
    if "/" in name or "\\" in name or ".." in name.split("/") \
            or ".." in name.split("\\"):
        return False
    if name in {".", ".."} or name.startswith("."):
        return False
    return True


class BundleInspection:
    """导入包检查结果（拒绝时 reasons 非空；不读任何文件内容）。"""

    def __init__(self) -> None:
        self.reasons: List[Tuple[str, str]] = []   # (reason, detail)
        self.manifest: Optional[Dict[str, Any]] = None
        self.members: Dict[str, zipfile.ZipInfo] = {}
        self.stem: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.reasons

    def reject(self, reason: str, detail: str = "") -> None:
        self.reasons.append((reason, detail))


class GalleryTransferService:
    """bundle 可移植包的导出／检查／导入（core，无 Qt）。"""

    def __init__(self, repository,
                 bundle_service: Optional[GalleryBundleService] = None
                 ) -> None:
        self._repository = repository
        self._bundles = bundle_service or GalleryBundleService(
            repository.root)

    # ── 导出 ───────────────────────────────────────────────────────────

    def export_bundle(self, folder: str, stem: str, dest_zip_path: str, *,
                      include_exr: bool = True
                      ) -> GalleryOperationResult:
        """把 bundle 打包为单个 ``.toposbundle`` zip（源不动）。"""
        request_id = make_gallery_request_id()
        real = self._bundles._resolve_in_root(folder)
        if real is None:
            return self._rejected(request_id, REASON_OUTSIDE_ROOT, folder)
        members = self._bundles.existing_members(real, stem)
        if not any(n.endswith(".topos") for n in members):
            return self._rejected(request_id, REASON_SOURCE_MISSING,
                                  f"{stem}.topos")
        chosen: List[str] = []
        for path in members:
            name = os.path.basename(path)
            if not include_exr and (
                    name.endswith(".still.exr")
                    or name.endswith(".still.exr.json")):
                continue
            chosen.append(path)
        dest_dir = os.path.dirname(os.path.abspath(dest_zip_path))
        if not os.path.isdir(dest_dir) or not os.access(dest_dir, os.W_OK):
            return self._rejected(request_id, REASON_IO_ERROR,
                                  f"dest not writable: {dest_dir}")

        meta = load_preset_meta(os.path.join(real, f"{stem}.topos"))
        manifest = self._build_export_manifest(real, stem, chosen, meta,
                                               include_exr)
        tmp_path = f"{dest_zip_path}.tmp-{os.getpid()}"
        try:
            with zipfile.ZipFile(tmp_path, "w",
                                 zipfile.ZIP_DEFLATED) as zf:
                for path in chosen:
                    zf.write(path, arcname=os.path.basename(path))
                zf.writestr(
                    BUNDLE_MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=1))
            os.replace(tmp_path, dest_zip_path)
        except OSError as exc:
            _silent_remove(tmp_path)
            return self._rejected(request_id, REASON_IO_ERROR, str(exc),
                                  recoverable=False)
        return GalleryOperationResult(
            request_id=request_id, status=OP_SUCCESS,
            affected_still_ids=(stem,),
            warnings=(f"exported {len(chosen)} files -> {dest_zip_path}",))

    def _build_export_manifest(self, folder: str, stem: str,
                               members: List[str], meta: Optional[PresetMeta],
                               include_exr: bool) -> Dict[str, Any]:
        files = []
        lut_refs = 0
        for path in members:
            name = os.path.basename(path)
            role = _ROLE_BY_SUFFIX.get(_suffix_of_name(name), "unknown")
            files.append({"role": role, "name": name,
                          "sha256": _sha256_file(path),
                          "size": os.path.getsize(path)})
        # grade 内 LUT 引用计数（可移植性如实汇总；应用时按引用解析）
        topos_path = os.path.join(folder, f"{stem}.topos")
        try:
            with zipfile.ZipFile(topos_path, "r") as zf:
                if "project.json" in zf.namelist():
                    project = json.loads(zf.read("project.json"))
                    layers = project.get("layers") or []
                    lut_refs = sum(
                        1 for layer in layers
                        if isinstance(layer, dict) and layer.get(
                            "active_lut_path"))
        except (OSError, ValueError):
            lut_refs = 0
        precision = ("exr_float32"
                     if any(f["role"] == BUNDLE_ROLE_STILL_EXR for f in files)
                     else "jpeg8" if any(f["role"] in ("thumbnail",
                                                       "still_jpeg")
                                         for f in files) else "none")
        alpha = (still_alpha_declaration(topos_path)
                 if precision == "exr_float32" else None)
        still_block: Dict[str, Any] = {"display_name": stem}
        if meta is not None:
            for key in ("still_id", "label", "clip_id", "asset_id",
                        "project_id", "timeline_id", "timeline_name",
                        "created_at"):
                value = getattr(meta, key, None)
                if value is not None:
                    still_block[key] = value
        return {
            "schema": EXPORT_SCHEMA,
            "kind": "topos-bundle",
            "still": still_block,
            "files": files,
            "precision": precision,
            "alpha_semantics": alpha,
            "exr_included": include_exr and precision == "exr_float32",
            "grade_lut_reference_count": lut_refs,
        }

    # ── 检查 ───────────────────────────────────────────────────────────

    def inspect_bundle(self, zip_path: str) -> BundleInspection:
        """zip 安全线检查＋manifest 校验（不解压到目标目录）。"""
        inspection = BundleInspection()
        if not os.path.isfile(zip_path) or not zipfile.is_zipfile(zip_path):
            inspection.reject(REASON_NOT_A_BUNDLE, zip_path)
            return inspection
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                infos = zf.infolist()
                if len(infos) > IMPORT_MAX_FILES:
                    inspection.reject(
                        REASON_FILE_COUNT_LIMIT, f"{len(infos)} entries")
                total = 0
                for info in infos:
                    if not _is_safe_member_name(info.filename):
                        inspection.reject(REASON_PATH_ESCAPE,
                                          info.filename)
                        continue
                    # symlink 条目（external_attr 高位 S_IFLNK）拒绝
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        inspection.reject(REASON_PATH_ESCAPE,
                                          f"symlink: {info.filename}")
                        continue
                    if info.file_size > IMPORT_MAX_FILE_BYTES:
                        inspection.reject(REASON_SIZE_LIMIT, info.filename)
                    total += info.file_size
                    inspection.members[info.filename] = info
                if total > IMPORT_MAX_TOTAL_BYTES:
                    inspection.reject(REASON_SIZE_LIMIT,
                                      f"total {total} bytes")
                if BUNDLE_MANIFEST_NAME not in inspection.members:
                    inspection.reject(REASON_NOT_A_BUNDLE,
                                      "manifest missing")
                    return inspection
                raw = zf.read(BUNDLE_MANIFEST_NAME)
                manifest = json.loads(raw)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            inspection.reject(REASON_NOT_A_BUNDLE, str(exc))
            return inspection
        if not isinstance(manifest, dict) \
                or manifest.get("kind") != "topos-bundle":
            inspection.reject(REASON_NOT_A_BUNDLE, "bad manifest")
            return inspection
        schema = manifest.get("schema")
        if not isinstance(schema, int) or schema > EXPORT_SCHEMA:
            inspection.reject(REASON_FUTURE_SCHEMA, f"schema {schema}")
            return inspection
        inspection.manifest = manifest
        listed = {f.get("name"): f for f in manifest.get("files", [])
                  if isinstance(f, dict)}
        grade_listed = any(f.get("role") == BUNDLE_ROLE_GRADE
                           for f in listed.values())
        has_grade_member = any(name.endswith(".topos")
                               for name in inspection.members)
        if not (grade_listed and has_grade_member):
            inspection.reject(REASON_MISSING_GRADE, "grade package absent")
        # 导入名优先取 manifest 的来源名（保留大小写），回退 zip 文件名
        display = (manifest.get("still") or {}).get("display_name")
        if isinstance(display, str) and display.strip():
            inspection.stem = display.strip()
        else:
            inspection.stem = os.path.splitext(
                os.path.basename(zip_path))[0].replace(".toposbundle", "")
        return inspection

    # ── 导入 ───────────────────────────────────────────────────────────

    def import_bundle(self, zip_path: str, dest_folder: str, *,
                      new_name: Optional[str] = None
                      ) -> GalleryOperationResult:
        """检查→staging→一次安装；拒绝不落任何散文件。"""
        request_id = make_gallery_request_id()
        real = self._bundles._resolve_in_root(dest_folder)
        if real is None:
            return self._rejected(request_id, REASON_OUTSIDE_ROOT,
                                  dest_folder)
        if not os.access(real, os.W_OK):
            return self._rejected(request_id, "readonly_album", real)
        inspection = self.inspect_bundle(zip_path)
        if not inspection.ok:
            return GalleryOperationResult(
                request_id=request_id, status="rejected",
                reason=inspection.reasons[0][0],
                warnings=tuple(f"{r}: {d}" for r, d in inspection.reasons))
        manifest = inspection.manifest or {}
        stem = (new_name or inspection.stem or "").strip()
        if not is_safe_bundle_stem(stem):
            return self._rejected(request_id, REASON_UNSAFE_NAME,
                                  repr(stem))
        stem = self._unoccupied_stem(real, stem)
        warnings: List[str] = [f"imported as: {stem}"] \
            if stem != (new_name or inspection.stem) else []

        # 身份冲突政策（G02/G08）：重复拒绝；冲突换发本库新 ID（记 origin）
        still_block = manifest.get("still") or {}
        origin_id = still_block.get("still_id")
        identity_action = self._identity_conflict_action(real, manifest,
                                                         origin_id)
        if identity_action == "duplicate":
            return self._rejected(
                request_id, REASON_DUPLICATE_IDENTITY,
                f"still_id {origin_id} already present with same grade")

        self._recover_import_txns(real)
        txn_dir = tempfile.mkdtemp(prefix=IMPORT_TXN_PREFIX, dir=real)
        journal: Dict[str, Any] = {"state": "installing", "stem": stem,
                                   "finals": []}
        try:
            with open(os.path.join(txn_dir, "__journal.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(journal, fh)
            with zipfile.ZipFile(zip_path, "r") as zf:
                for name, info in inspection.members.items():
                    if name == BUNDLE_MANIFEST_NAME:
                        continue
                    if name not in {f.get("name")
                                    for f in manifest.get("files", [])}:
                        warnings.append(f"skipped unlisted file: {name}")
                        continue
                    target = os.path.join(txn_dir, name)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    listed = next(
                        f for f in manifest.get("files", [])
                        if f.get("name") == name)
                    if listed.get("sha256") and \
                            _sha256_file(target) != listed["sha256"]:
                        raise _ImportVerifiedError(
                            REASON_HASH_MISMATCH, name)
            # 通过检查：一次安装（grade→sidecars），journal 记录 finals
            finals: List[str] = []
            for name in sorted(inspection.members):
                if name == BUNDLE_MANIFEST_NAME:
                    continue
                staged = os.path.join(txn_dir, name)
                if not os.path.isfile(staged):
                    continue
                final = os.path.join(
                    real, _rebased(name, inspection.stem, stem))
                os.replace(staged, final)
                finals.append(final)
            journal["state"] = "committed"
            journal["finals"] = finals
            with open(os.path.join(txn_dir, "__journal.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(journal, fh)
        except _ImportVerifiedError as exc:
            shutil.rmtree(txn_dir, ignore_errors=True)
            return self._rejected(request_id, exc.reason, exc.detail)
        except OSError as exc:
            # 回滚已安装部分；journal 保持 installing 供下次恢复
            self._rollback_import(journal)
            shutil.rmtree(txn_dir, ignore_errors=True)
            return self._rejected(request_id, REASON_IO_ERROR, str(exc),
                                  recoverable=True)
        # 身份换发（origin 保留进 extra_fields；本库新 ID 落盘）
        if identity_action == "reallocate" and origin_id:
            self._reallocate_imported_identity(
                os.path.join(real, f"{stem}.topos"), origin_id, warnings)
        shutil.rmtree(txn_dir, ignore_errors=True)
        return GalleryOperationResult(
            request_id=request_id, status=OP_SUCCESS,
            affected_still_ids=(stem,), warnings=tuple(warnings))

    # ── 内部 ───────────────────────────────────────────────────────────

    def _unoccupied_stem(self, folder: str, stem: str) -> str:
        """重名自动后缀分配（结果可审阅：返回实际采用名）。"""
        candidate = stem
        counter = 1
        while os.path.exists(os.path.join(folder, f"{candidate}.topos")):
            candidate = f"{stem}_{counter}"
            counter += 1
        return candidate

    def _identity_conflict_action(self, folder: str, manifest: Dict[str, Any],
                                  origin_id: Optional[str]) -> Optional[str]:
        """同 ID 冲突判定：None=无冲突 / "duplicate" / "reallocate"。"""
        if not origin_id:
            return None
        try:
            names = os.listdir(folder)
        except OSError:
            return None
        for name in names:
            if not name.endswith(".topos"):
                continue
            meta = load_preset_meta(os.path.join(folder, name))
            if meta is None or meta.still_id != origin_id:
                continue
            if self._grade_hash_matches(folder, name, manifest):
                return "duplicate"       # 同 ID 同内容：重复导入拒绝
            return "reallocate"          # 同 ID 不同内容：换发本库新 ID
        return None

    def _reallocate_imported_identity(self, topos_path: str, origin_id: str,
                                      warnings: List[str]) -> None:
        """origin ID 保留进 extra_fields，分配并落盘本库新 ID。"""
        meta = load_preset_meta(topos_path)
        if meta is None:
            meta = PresetMeta()
        meta.extra_fields["origin_still_id"] = origin_id
        meta.still_id = allocate_still_id()
        if save_preset_meta(topos_path, meta):
            warnings.append(
                f"still_id {origin_id} conflicted; allocated "
                f"{meta.still_id} (origin kept)")
        else:
            warnings.append(
                f"WARNING: identity reallocation save failed; origin "
                f"{origin_id} retained in imported meta")

    def _grade_hash_matches(self, folder: str, topos_name: str,
                            manifest: Dict[str, Any]) -> bool:
        listed = next(
            (f for f in manifest.get("files", [])
             if f.get("role") == BUNDLE_ROLE_GRADE), None)
        if not listed or not listed.get("sha256"):
            return False
        local = os.path.join(folder, topos_name)
        try:
            return _sha256_file(local) == listed["sha256"]
        except OSError:
            return False

    def _recover_import_txns(self, folder: str) -> None:
        """收敛遗留导入事务（installing → 移除已装部分）。"""
        if not os.path.isdir(folder):
            return
        for name in sorted(os.listdir(folder)):
            if not name.startswith(IMPORT_TXN_PREFIX):
                continue
            txn_dir = os.path.join(folder, name)
            journal_path = os.path.join(txn_dir, "__journal.json")
            try:
                with open(journal_path, "r", encoding="utf-8") as fh:
                    journal = json.load(fh)
            except (OSError, ValueError):
                shutil.rmtree(txn_dir, ignore_errors=True)
                continue
            if journal.get("state") != "committed":
                self._rollback_import(journal)
            shutil.rmtree(txn_dir, ignore_errors=True)

    @staticmethod
    def _rollback_import(journal: Dict[str, Any]) -> None:
        for final in reversed(journal.get("finals", [])):
            _silent_remove(final)

    @staticmethod
    def _rejected(request_id: str, reason: str, detail: str, *,
                  recoverable: bool = False) -> GalleryOperationResult:
        return GalleryOperationResult(
            request_id=request_id, status="rejected", reason=reason,
            warnings=(detail,) if detail else (), recoverable=recoverable)


class _ImportVerifiedError(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _rebased(member_name: str, inspection_stem: Optional[str],
             target_stem: str) -> str:
    """把包内成员名换到目标 stem 下（成员名以包 stem 或固定角色结尾）。"""
    suffix = _suffix_of_name(member_name)
    return f"{target_stem}{suffix}"


def _suffix_of_name(name: str) -> str:
    for suffix in sorted(BUNDLE_SUFFIX_BY_ROLE.values(), key=len,
                         reverse=True):
        if name.endswith(suffix):
            return suffix
    return os.path.splitext(name)[1]


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
