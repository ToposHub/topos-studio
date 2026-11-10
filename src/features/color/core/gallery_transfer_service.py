"""G08：Gallery bundle 导入／导出——可移植包、限制与身份冲突政策。

分清五种动作（计划 §5 G08）：导入图像为参考（既有 reference 链，非本
模块）、**导入 grade bundle**、导出参考图（既有 thumbnail/still 复制，
非本模块）、**导出完整 grade bundle**（本模块）、导出 thumbnail（既有）。

- **导出**：bundle 全成员（六角色，按需含 EXR 对）＋ ``bundle_manifest``
  （schema/still 身份/每文件 sha256/精度/alpha 声明）打包为单个
  ``.toposbundle`` zip——staging 写入后一次 ``os.replace``，失败/取消不
  留半目标；**源 bundle 一律不动**。SR03 起导出后**验证最终归档**
  （CRC＋逐成员 hash==manifest），并发修改源文件时要么完整成功要么
  明确拒绝；导出与导入共享数量／单文件／总量限额——不生成自己无法
  导入的成功包。
- **导入**：zip 安全限制（文件数／解压总量／单文件上限／纯文件名归属／
  symlink 条目拒绝／不执行任何内含内容）；manifest schema 已知才继续；
  **同一归档句柄**贯穿检查与解压（两次打开之间包被替换的结构差异不再
  放行）；逐文件分块解压（取消／进度／字节数核对）＋sha256 校验后经
  dest 目录内 staging 一次安装。
- **依赖统一事务**（SR03）：随包 LUT 先在事务 staging 内解压校验，与
  bundle finals 写进**同一份意图 journal**（``resources`` 段）后才安装
  到受管 ``LUTs/``——取消／失败／崩溃的回滚把资源与 bundle 一起撤掉，
  不留无记录的孤儿资源；安装名内容寻址（sha 前缀），碰撞（同前缀不同
  内容）顺延更长前缀，全 hash 同名异内容明确拒绝，**不覆盖外来文件**。
- **受管引用**（SR04）：导入重写 grade 的 ``active_lut_path`` 为库相对
  受管引用（``topos-lut://LUTs/<安装名>``，见 gallery_resource_refs）；
  库目录整体迁移后引用仍解析到当前库根。已导入的绝对引用经
  :meth:`migrate_album_lut_references` 按内容证据升级。
- **身份冲突**（G02 政策）：导入的 still_id 与目标库既有条目冲突时——
  同内容＝重复拒绝；不同内容＝保留外部 origin ID 并分配本库新 ID。
  重名文件自动后缀分配，结果里给出**实际采用名**；不做静默覆盖。
- **回滚内容归属**（SR02）：journal finals/resources 带 sha256——同大小
  不同内容、或旧格式无内容证据的日志一律不删（保全待人工），删除授权
  只来自内容证据。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.features.color.core.gallery_bundle_service import (
    BUNDLE_SUFFIX_BY_ROLE,
    GalleryBundleService,
    REASON_ALBUM_BUSY,
    REASON_IO_ERROR,
    REASON_OUTSIDE_ROOT,
    REASON_SOURCE_MISSING,
    REASON_UNSAFE_NAME,
)
from src.features.color.core.gallery_contracts import (
    BUNDLE_ROLE_GRADE,
    BUNDLE_ROLE_STILL_EXR,
    GalleryOperationResult,
    OP_CANCELLED,
    OP_SUCCESS,
    is_safe_bundle_stem,
    make_gallery_request_id,
)
from src.features.color.core.gallery_resource_refs import (
    DEPENDENCY_DIR_NAME,
    managed_lut_reference,
    parse_managed_lut_reference,
    resolve_lut_reference,
)
from src.features.color.core.gallery_txn_core import (
    ACTIVE_TXNS,
    DirectoryBusyError,
    JournalError,
    acquire_directories,
    make_owner_block,
    other_process_alive,
    path_within,
    read_journal,
    release_directories,
    try_recover_lock,
    write_journal,
)
from src.features.color.core.preset_meta import (
    PresetMeta,
    allocate_still_id,
    load_preset_meta,
    meta_path_for,
    save_preset_meta,
    still_alpha_declaration,
)

logger = logging.getLogger(__name__)

#: 可移植包信封名（zip 内的清单）
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"
#: 导入限制（zip bomb 防线；依赖计入同一限额）
IMPORT_MAX_FILES = 16
IMPORT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
IMPORT_MAX_FILE_BYTES = 128 * 1024 * 1024
IMPORT_TXN_PREFIX = ".gallery-import-"
IMPORT_JOURNAL_NAME = "__journal.json"
#: 自动重名后缀上限（防病态目录把导入卡成死循环）
IMPORT_MAX_STEM_SUFFIX = 10000
#: 归档内依赖条目前缀
DEPENDENCY_ARCHIVE_PREFIX = "assets/luts/"
#: 分块解压粒度（取消/进度在块间生效，不等整个大文件读完）
_EXTRACT_CHUNK = 1 << 20

#: schema 2（BR07）：新增 ``dependencies``。schema 1 包仍可导入（无依赖
#: 信息＝引用型包，如实告警）。
EXPORT_SCHEMA = 2

# 稳定拒绝原因
REASON_NOT_A_BUNDLE = "not_a_bundle"
REASON_FUTURE_SCHEMA = "future_schema"
REASON_MISSING_GRADE = "missing_grade"
REASON_PATH_ESCAPE = "path_escape"
REASON_SIZE_LIMIT = "size_limit"
REASON_FILE_COUNT_LIMIT = "file_count_limit"
REASON_HASH_MISMATCH = "hash_mismatch"
REASON_DUPLICATE_IDENTITY = "duplicate_identity"
REASON_IDENTITY_SAVE_FAILED = "identity_save_failed"
REASON_DEPENDENCY_ESCAPED = "dependency_path_escape"
REASON_DEPENDENCY_LIMIT = "dependency_limit"
REASON_DEPENDENCY_COLLISION = "dependency_collision"

_ROLE_BY_SUFFIX = {suffix: role for role, suffix
                   in BUNDLE_SUFFIX_BY_ROLE.items()}

_INSTALL_NAME_HASH_RE = re.compile(r"^([0-9a-f]{8,64})_(.+)$")


def _sha256_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_EXTRACT_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_safe_member_name(name: str) -> bool:
    """zip 成员必须是纯文件名（拒绝绝对路径/../分隔符）。"""
    if not is_safe_bundle_stem(name) or name.startswith(("/", "\\")):
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
                      include_exr: bool = True,
                      include_dependencies: bool = True
                      ) -> GalleryOperationResult:
        """把 bundle 打包为单个 ``.toposbundle`` zip（源不动）。

        BR07：grade 引用的外部 LUT（含受管引用，解析到当前库）默认**随
        包携带**；缺失的依赖如实记录 ``missing`` 并进 warnings——引用型
        包不冒充完整便携。SR03：导出后验证最终归档（CRC＋成员 hash），
        并与导入共享数量／单文件／总量限额。
        """
        request_id = make_gallery_request_id()
        if not is_safe_bundle_stem(stem):
            return self._rejected(request_id, REASON_UNSAFE_NAME, repr(stem))
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

        topos_path = os.path.join(real, f"{stem}.topos")
        dependencies, dep_warnings = self._collect_grade_dependencies(
            topos_path, self._repository.root)
        if not include_dependencies:
            dependencies = [dict(dep, bundled=False, missing=True,
                                 reason="dependencies not included")
                            for dep in dependencies]
            dep_warnings = [
                f"dependency not bundled: {dep['name']} "
                f"({dep.get('reason') or 'excluded'})"
                for dep in dependencies]
        bundled = {dep["archive"]: dep["resolved"]
                   for dep in dependencies if dep.get("bundled")}

        # 与导入同一限额：超限的包本服务自己就无法导入，明确拒绝
        total_members = len(chosen) + len(bundled) + 1
        if total_members > IMPORT_MAX_FILES:
            return self._rejected(
                request_id, REASON_FILE_COUNT_LIMIT,
                f"{total_members} members (limit {IMPORT_MAX_FILES})")
        total_bytes = 0
        for path in chosen:
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                return self._rejected(request_id, REASON_IO_ERROR, str(exc))
            if size > IMPORT_MAX_FILE_BYTES:
                return self._rejected(
                    request_id, REASON_SIZE_LIMIT,
                    f"{os.path.basename(path)}: {size} bytes")
            total_bytes += size
        for dep in dependencies:
            if dep.get("bundled"):
                total_bytes += dep.get("size", 0)
                if dep.get("size", 0) > IMPORT_MAX_FILE_BYTES:
                    return self._rejected(
                        request_id, REASON_DEPENDENCY_LIMIT,
                        f"{dep['name']}: {dep.get('size')} bytes")
        if total_bytes > IMPORT_MAX_TOTAL_BYTES:
            return self._rejected(
                request_id, REASON_SIZE_LIMIT, f"total {total_bytes} bytes")

        meta = load_preset_meta(topos_path)
        manifest = self._build_export_manifest(real, stem, chosen, meta,
                                               include_exr, dependencies)
        tmp_path = f"{dest_zip_path}.tmp-{os.getpid()}"
        try:
            with zipfile.ZipFile(tmp_path, "w",
                                 zipfile.ZIP_DEFLATED) as zf:
                for path in chosen:
                    zf.write(path, arcname=os.path.basename(path))
                for archive_name in sorted(bundled):
                    zf.write(bundled[archive_name], arcname=archive_name)
                zf.writestr(
                    BUNDLE_MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=1))
            os.replace(tmp_path, dest_zip_path)
            # SR03：验证最终归档——并发修改源文件时要么完整成功要么拒绝
            self._verify_export_archive(dest_zip_path, manifest)
        except _ExportVerifiedError as exc:
            _silent_remove(tmp_path)
            _silent_remove(dest_zip_path)
            return self._rejected(request_id, exc.reason, exc.detail,
                                  recoverable=False)
        except OSError as exc:
            _silent_remove(tmp_path)
            return self._rejected(request_id, REASON_IO_ERROR, str(exc),
                                  recoverable=False)
        warnings = [f"exported {len(chosen)} files -> {dest_zip_path}",
                    *dep_warnings]
        if dependencies:
            warnings.append(
                "portable: full" if manifest["portable"] else
                "portable: partial (missing dependencies listed above)")
        return GalleryOperationResult(
            request_id=request_id, status=OP_SUCCESS,
            affected_still_ids=(stem,), warnings=tuple(warnings))

    @staticmethod
    def _verify_export_archive(path: str, manifest: Dict[str, Any]) -> None:
        """最终归档验证：CRC＋逐成员内容 hash 与 manifest 一致。"""
        def _member_digest(zf: zipfile.ZipFile, name: str) -> str:
            hasher = hashlib.sha256()
            with zf.open(name) as fh:
                for chunk in iter(lambda: fh.read(_EXTRACT_CHUNK), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()

        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise _ExportVerifiedError(
                    REASON_HASH_MISMATCH, f"CRC failed: {bad}")
            for entry in manifest.get("files", []):
                if _member_digest(zf, entry["name"]) != entry["sha256"]:
                    raise _ExportVerifiedError(
                        REASON_HASH_MISMATCH,
                        f"archive differs from manifest: {entry['name']}")
            for dep in manifest.get("dependencies") or []:
                if dep.get("bundled") and _member_digest(
                        zf, dep["archive"]) != dep["sha256"]:
                    raise _ExportVerifiedError(
                        REASON_HASH_MISMATCH,
                        f"dependency differs from manifest: "
                        f"{dep['archive']}")

    def _collect_grade_dependencies(
            self, topos_path: str,
            library_root: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """收集 grade 的外部依赖（``active_lut_path`` LUT）。

        受管引用（``topos-lut://``）解析到**当前库根**（SR04——迁移后的
        库仍可打包）；本机存在的文件 → 打包候选（sha256/大小/归档名）；
        缺失 → 记 ``missing`` + 原因并告警——引用型包不冒充完整便携。
        ``source`` 字段＝grade 内的**引用字符串**（导入重写的匹配键）。
        """
        references: List[str] = []
        try:
            with zipfile.ZipFile(topos_path, "r") as zf:
                if "project.json" in zf.namelist():
                    project = json.loads(zf.read("project.json"))
                    for layer in project.get("layers") or []:
                        if isinstance(layer, dict):
                            ref = layer.get("active_lut_path")
                            if isinstance(ref, str) and ref:
                                references.append(ref)
        except (OSError, ValueError, zipfile.BadZipFile):
            return [], []
        dependencies: List[Dict[str, Any]] = []
        warnings: List[str] = []
        seen = set()
        for reference in references:
            if reference in seen:
                continue
            seen.add(reference)
            managed = parse_managed_lut_reference(reference)
            if managed is not None:
                name = managed
                source: Optional[str] = resolve_lut_reference(
                    reference, library_root or
                    os.path.dirname(os.path.dirname(topos_path)))
                missing_reason = "managed resource missing in this library"
            else:
                name = os.path.basename(reference.replace("\\", "/"))
                source = reference
                missing_reason = "source file not found"
            if not name:
                continue
            if not source or not os.path.isfile(source):
                dependencies.append({
                    "kind": "lut", "name": name, "source": reference,
                    "resolved": None,
                    "missing": True,
                    "reason": missing_reason if source else
                    "library root unknown", "bundled": False})
                warnings.append(
                    f"dependency not bundled: {name} "
                    f"({missing_reason}: {reference})")
                continue
            digest = _sha256_file(source)
            dependencies.append({
                "kind": "lut", "name": name, "source": reference,
                "resolved": source,
                "sha256": digest, "size": os.path.getsize(source),
                "archive": f"{DEPENDENCY_ARCHIVE_PREFIX}"
                           f"{digest[:8]}_{name}",
                "missing": False, "reason": "", "bundled": True})
        return dependencies, warnings

    def _build_export_manifest(self, folder: str, stem: str,
                               members: List[str], meta: Optional[PresetMeta],
                               include_exr: bool,
                               dependencies: Optional[List[Dict[str, Any]]]
                               = None) -> Dict[str, Any]:
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
            # BR07（schema 2）：依赖清单 + 便携性如实标注（不携带本机
            # 绝对路径——resolved 只在导出进程内使用）
            "dependencies": [
                {k: v for k, v in dep.items() if k != "resolved"}
                for dep in (dependencies or [])],
            "portable": all(not dep.get("missing")
                            for dep in (dependencies or [])),
        }

    # ── 检查 ───────────────────────────────────────────────────────────

    def inspect_bundle(self, zip_path: str) -> BundleInspection:
        """zip 安全线检查＋manifest 校验（不解压到目标目录）。"""
        if not os.path.isfile(zip_path) or not zipfile.is_zipfile(zip_path):
            inspection = BundleInspection()
            inspection.reject(REASON_NOT_A_BUNDLE, zip_path)
            return inspection
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                return self._inspect_bundle_handle(zf, zip_path)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            inspection = BundleInspection()
            inspection.reject(REASON_NOT_A_BUNDLE, str(exc))
            return inspection

    def _inspect_bundle_handle(self, zf: zipfile.ZipFile,
                               zip_path: str) -> BundleInspection:
        """在已打开的归档句柄上做安全线检查（SR03：单一稳定句柄）。"""
        inspection = BundleInspection()
        infos = zf.infolist()
        if len(infos) > IMPORT_MAX_FILES:
            inspection.reject(
                REASON_FILE_COUNT_LIMIT, f"{len(infos)} entries")
        total = 0
        for info in infos:
            if info.filename in inspection.members:
                inspection.reject(REASON_NOT_A_BUNDLE,
                                  f"duplicate member: {info.filename}")
                continue
            # BR07：依赖资源条目允许 assets/luts/ 子目录（不落入
            # 包顶层），结构在依赖段单独校验（越界拒绝）。
            if info.filename.startswith(DEPENDENCY_ARCHIVE_PREFIX):
                if ".." in info.filename.split("/"):
                    inspection.reject(REASON_DEPENDENCY_ESCAPED,
                                      info.filename)
                    continue
            elif not _is_safe_member_name(info.filename):
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
        if not inspection.ok:
            return inspection  # Do not decompress a rejected archive.
        if BUNDLE_MANIFEST_NAME not in inspection.members:
            inspection.reject(REASON_NOT_A_BUNDLE,
                              "manifest missing")
            return inspection
        try:
            manifest = json.loads(zf.read(BUNDLE_MANIFEST_NAME))
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            inspection.reject(REASON_NOT_A_BUNDLE, str(exc))
            return inspection
        if not isinstance(manifest, dict) \
                or manifest.get("kind") != "topos-bundle":
            inspection.reject(REASON_NOT_A_BUNDLE, "bad manifest")
            return inspection
        schema = manifest.get("schema")
        if type(schema) is not int or not 1 <= schema <= EXPORT_SCHEMA:
            inspection.reject(REASON_FUTURE_SCHEMA, f"schema {schema}")
            return inspection
        inspection.manifest = manifest
        # BR07：依赖条目校验（归档名必须位于 assets/luts/ 纯文件名空间、
        # sha256 格式合法、条目真实存在于包内——越界资源条目直接拒绝）
        dependencies = manifest.get("dependencies", [])
        if not isinstance(dependencies, list):
            inspection.reject(REASON_NOT_A_BUNDLE, "invalid dependencies list")
            return inspection
        for dep in dependencies:
            if not isinstance(dep, dict):
                inspection.reject(REASON_NOT_A_BUNDLE, "bad dependency")
                return inspection
            if dep.get("bundled"):
                archive = dep.get("archive")
                digest = dep.get("sha256")
                if not is_safe_bundle_stem(dep.get("name")):
                    inspection.reject(REASON_DEPENDENCY_ESCAPED,
                                      "invalid dependency install name")
                    return inspection
                if not isinstance(archive, str) \
                        or not archive.startswith(DEPENDENCY_ARCHIVE_PREFIX) \
                        or not _is_safe_member_name(
                            os.path.basename(archive)) \
                        or "/" in archive[len(DEPENDENCY_ARCHIVE_PREFIX):]:
                    inspection.reject(REASON_DEPENDENCY_ESCAPED,
                                      repr(archive))
                    return inspection
                if archive not in inspection.members:
                    inspection.reject(REASON_NOT_A_BUNDLE,
                                      f"dependency member missing: {archive}")
                    return inspection
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)):
                    inspection.reject(REASON_NOT_A_BUNDLE,
                                      f"bad dependency hash: {archive}")
                    return inspection
        files = manifest.get("files")
        still = manifest.get("still", {})
        if (not isinstance(files, list) or not isinstance(still, dict)
                or any(not isinstance(f, dict)
                       or not isinstance(f.get("name"), str)
                       or not _is_safe_member_name(f["name"])
                       for f in files)):
            inspection.reject(REASON_NOT_A_BUNDLE, "invalid manifest fields")
            return inspection
        listed = {}
        roles = set()
        for entry in files:
            name, role = entry["name"], entry.get("role")
            digest = entry.get("sha256")
            if (name not in inspection.members or name in listed
                    or not isinstance(role, str)
                    or role not in BUNDLE_SUFFIX_BY_ROLE or role in roles
                    or _suffix_of_name(name) != BUNDLE_SUFFIX_BY_ROLE[role]
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                inspection.reject(REASON_NOT_A_BUNDLE,
                                  f"invalid manifest member: {name}")
                return inspection
            listed[name] = entry
            roles.add(role)
        grade_listed = any(f.get("role") == BUNDLE_ROLE_GRADE
                           for f in listed.values())
        has_grade_member = any(name.endswith(".topos")
                               for name in inspection.members)
        if not (grade_listed and has_grade_member):
            inspection.reject(REASON_MISSING_GRADE, "grade package absent")
        # 导入名优先取 manifest 的来源名（保留大小写），回退 zip 文件名
        display = still.get("display_name")
        if isinstance(display, str) and display.strip():
            inspection.stem = display.strip()
        else:
            inspection.stem = os.path.splitext(
                os.path.basename(zip_path))[0].replace(".toposbundle", "")
        return inspection

    # ── 导入 ───────────────────────────────────────────────────────────

    def import_bundle(self, zip_path: str, dest_folder: str, *,
                      new_name: Optional[str] = None,
                      progress_callback: Optional[Callable[[int, int],
                                                           None]] = None,
                      cancel_check: Optional[Callable[[], bool]] = None
                      ) -> GalleryOperationResult:
        """检查→staging→身份提交→一次安装；拒绝不落任何散文件。

        BR02 原子性合同（全成员占用／意图先行／身份在 staging 提交／
        取消零落地／目录互斥）；SR03 起随包依赖与 bundle finals 同一
        journal 事务（``resources`` 段），资源目录与目标相册一起加锁；
        SR03.6 检查与解压共用同一归档句柄。
        """
        request_id = make_gallery_request_id()
        real = self._bundles._resolve_in_root(dest_folder)
        if real is None:
            return self._rejected(request_id, REASON_OUTSIDE_ROOT,
                                  dest_folder)
        if not os.access(real, os.W_OK):
            return self._rejected(request_id, "readonly_album", real)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                inspection = self._inspect_bundle_handle(zf, zip_path)
                if not inspection.ok:
                    return GalleryOperationResult(
                        request_id=request_id, status="rejected",
                        reason=inspection.reasons[0][0],
                        warnings=tuple(f"{r}: {d}"
                                       for r, d in inspection.reasons))
                manifest = inspection.manifest or {}
                lock_dirs = [real]
                if any(dep.get("bundled")
                       for dep in manifest.get("dependencies") or []
                       if isinstance(dep, dict)):
                    lock_dirs.append(self._managed_lut_root())
                try:
                    lock_keys = acquire_directories(*lock_dirs)
                except DirectoryBusyError as exc:
                    return self._rejected(request_id, REASON_ALBUM_BUSY,
                                          str(exc))
                try:
                    return self._import_bundle_locked(
                        zf, real, inspection, new_name, request_id,
                        progress_callback, cancel_check)
                finally:
                    release_directories(lock_keys)
        except zipfile.BadZipFile as exc:
            return self._rejected(request_id, REASON_NOT_A_BUNDLE, str(exc))
        except OSError as exc:
            return self._rejected(request_id, REASON_IO_ERROR, str(exc),
                                  recoverable=True)

    def _managed_lut_root(self) -> str:
        return os.path.join(self._repository.root, DEPENDENCY_DIR_NAME)

    def _import_bundle_locked(self, zf: zipfile.ZipFile, real: str,
                              inspection: "BundleInspection",
                              new_name: Optional[str], request_id: str,
                              progress_callback, cancel_check
                              ) -> GalleryOperationResult:
        manifest = inspection.manifest or {}
        explicit = isinstance(new_name, str) and bool(new_name.strip())
        base_stem = (new_name or inspection.stem or "").strip()
        if not is_safe_bundle_stem(base_stem):
            return self._rejected(request_id, REASON_UNSAFE_NAME,
                                  repr(base_stem))
        stem, stem_warnings = self._resolve_import_stem(real, base_stem,
                                                        explicit)
        if stem is None:
            reason, detail = stem_warnings  # type: ignore[misc]
            return self._rejected(request_id, reason, detail)
        warnings: List[str] = list(stem_warnings)

        # 身份冲突政策（G02/G08；作用域＝目标相册——同一 still 复制进另
        # 一相册是合法操作，不按全库去重，BR02 明确该合同）：重复拒绝；
        # 冲突换发本库新 ID（origin 保留）。
        still_block = manifest.get("still") or {}
        origin_id = still_block.get("still_id")
        identity_action = self._identity_conflict_action(real, manifest,
                                                         origin_id)
        if identity_action == "duplicate":
            return self._rejected(
                request_id, REASON_DUPLICATE_IDENTITY,
                f"still_id {origin_id} already present with identical "
                f"bundle content")

        self._recover_import_txns(real)
        txn_dir = tempfile.mkdtemp(prefix=IMPORT_TXN_PREFIX, dir=real)
        active_token = ACTIVE_TXNS.register(txn_dir)
        try:
            return self._run_import_txn(
                zf, real, inspection, request_id, stem, identity_action,
                origin_id, txn_dir, warnings, progress_callback,
                cancel_check)
        finally:
            # SR01：登记与事务生命周期平衡（任何出口都注销）
            ACTIVE_TXNS.unregister(active_token)

    def _run_import_txn(self, zf: zipfile.ZipFile, real: str,
                        inspection: "BundleInspection", request_id: str,
                        stem: str, identity_action: Optional[str],
                        origin_id, txn_dir: str, warnings: List[str],
                        progress_callback, cancel_check
                        ) -> GalleryOperationResult:
        manifest = inspection.manifest or {}
        journal_path = os.path.join(txn_dir, IMPORT_JOURNAL_NAME)
        journal: Dict[str, Any] = {"schema": 1, "kind": "gallery-import",
                                   "state": "staging", "stem": stem,
                                   "finals": [], "resources": [],
                                   **make_owner_block()}
        listed_names = {f.get("name") for f in manifest.get("files", [])}
        try:
            write_journal(journal_path, dict(journal, txn_id=request_id))
            staged: List[Tuple[str, str]] = []   # (成员名, staging 路径)
            for index, (name, info) in enumerate(
                    sorted(inspection.members.items()), start=1):
                if cancel_check is not None and cancel_check():
                    raise _ImportCancelled()
                if name == BUNDLE_MANIFEST_NAME:
                    continue
                if name not in listed_names:
                    warnings.append(f"skipped unlisted file: {name}")
                    continue
                target = os.path.join(txn_dir, name)
                _extract_member_chunked(
                    zf, name, target, expected_size=info.file_size,
                    cancel_check=cancel_check)
                listed = next(
                    f for f in manifest.get("files", [])
                    if f.get("name") == name)
                if listed.get("sha256") and \
                        _sha256_file(target) != listed["sha256"]:
                    raise _ImportVerifiedError(
                        REASON_HASH_MISMATCH, name)
                staged.append((name, target))
                if progress_callback is not None:
                    progress_callback(index, len(inspection.members))

            # SR03：依赖在事务 staging 内解压校验，安装意图与 bundle
            # finals 同一 journal；引用重写为受管形式（SR04）。
            dep_records, dep_warnings_extra = self._stage_dependencies(
                zf, txn_dir, manifest, cancel_check)
            warnings.extend(dep_warnings_extra)

            # 身份换发在 staging 完成（BR02）：文件提交之前；失败整笔拒绝
            if identity_action == "reallocate" and origin_id:
                grade_name = next(
                    (f["name"] for f in manifest.get("files", [])
                     if f.get("role") == BUNDLE_ROLE_GRADE), None)
                if grade_name:
                    staged_topos = os.path.join(txn_dir, grade_name)
                    if not self._reallocate_imported_identity(
                            staged_topos, origin_id, warnings):
                        raise _ImportVerifiedError(
                            REASON_IDENTITY_SAVE_FAILED, str(origin_id))
                    # A manifest may carry the origin identity without a
                    # meta member. Include the newly created sidecar in the
                    # same install journal rather than discarding it with
                    # staging.
                    staged_meta = meta_path_for(staged_topos)
                    if staged_meta not in {path for _, path in staged}:
                        staged.append((os.path.basename(staged_meta),
                                       staged_meta))

            # 依赖安装目标解析（碰撞顺延/复用）＋受管引用重写
            resources, install_warnings = self._resolve_dependency_installs(
                dep_records, staged, manifest)
            warnings.extend(install_warnings)

            # 安装意图先行：全部 finals（含内容 hash）与 resources 落盘
            # 后再动第一个文件（SR02：删除授权来自内容证据）
            finals = [{"path": os.path.join(
                           real, _rebased(name, inspection.stem, stem)),
                       "size": os.path.getsize(path),
                       "sha256": _sha256_file(path)}
                      for name, path in staged]
            journal["state"] = "installing"
            journal["finals"] = finals
            journal["resources"] = resources
            write_journal(journal_path, dict(journal, txn_id=request_id))

            # 资源安装（受管目录；不覆盖外来内容）
            os.makedirs(self._managed_lut_root(), exist_ok=True)
            for record in dep_records:
                if record.get("staged_path") and record.get("target_path"):
                    if cancel_check is not None and cancel_check():
                        raise _ImportCancelled()
                    os.replace(record["staged_path"], record["target_path"])

            for index, (_name, staged_path) in enumerate(staged):
                if cancel_check is not None and cancel_check():
                    raise _ImportCancelled()
                os.replace(staged_path, finals[index]["path"])
                if progress_callback is not None:
                    progress_callback(index + 1, len(staged))
            journal["state"] = "committed"
            write_journal(journal_path, dict(journal, txn_id=request_id))
        except _ImportCancelled:
            rollback_errors = self._rollback_import(journal)
            if rollback_errors:
                # SR02：取消回滚失败保留日志，结果标记可恢复
                return GalleryOperationResult(
                    request_id=request_id, status=OP_CANCELLED,
                    reason="cancelled", recoverable=True,
                    warnings=tuple(f"rollback incomplete: {e}"
                                   for e in rollback_errors),
                    affected_still_ids=(stem,))
            shutil.rmtree(txn_dir, ignore_errors=True)
            return GalleryOperationResult(
                request_id=request_id, status=OP_CANCELLED,
                reason="cancelled", affected_still_ids=(stem,))
        except _ImportVerifiedError as exc:
            rollback_errors = self._rollback_import(journal)
            if rollback_errors:
                # SR02：回滚不完整＝保留日志与现场（可重试/人工）
                return GalleryOperationResult(
                    request_id=request_id, status="rejected",
                    reason=exc.reason, recoverable=True,
                    warnings=(exc.detail,
                              *(f"rollback incomplete: {e}"
                                for e in rollback_errors)),
                    affected_still_ids=(stem,))
            shutil.rmtree(txn_dir, ignore_errors=True)
            return self._rejected(request_id, exc.reason, exc.detail)
        except OSError as exc:
            # 回滚已安装部分（finals＋resources）；失败保留 journal 供
            # 下次恢复
            rollback_errors = self._rollback_import(journal)
            if rollback_errors:
                return GalleryOperationResult(
                    request_id=request_id, status="rejected",
                    reason=REASON_IO_ERROR, recoverable=True,
                    warnings=tuple(f"rollback incomplete: {e}"
                                   for e in rollback_errors),
                    affected_still_ids=(stem,))
            shutil.rmtree(txn_dir, ignore_errors=True)
            return self._rejected(request_id, REASON_IO_ERROR, str(exc),
                                  recoverable=True, affected_still_ids=(stem,))
        final_meta = load_preset_meta(os.path.join(real, f"{stem}.topos"))
        if final_meta is not None and final_meta.still_id:
            warnings.append(f"still_id: {final_meta.still_id}")
        shutil.rmtree(txn_dir, ignore_errors=True)
        return GalleryOperationResult(
            request_id=request_id, status=OP_SUCCESS,
            affected_still_ids=(stem,), warnings=tuple(warnings))

    # ── 内部：依赖 staging／目标解析 ──────────────────────────────────

    def _stage_dependencies(self, zf: zipfile.ZipFile, txn_dir: str,
                            manifest: Dict[str, Any], cancel_check
                            ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """随包依赖在事务 staging 内解压＋sha 校验（SR03：先全验证）。

        返回 ``(dep_records, warnings)``；record 携带引用串、内容 hash、
        staging 路径——安装目标在全部 bundle 成员校验后统一解析。
        missing／schema 1 引用型包：不装不改，warnings 如实说明。
        """
        deps = manifest.get("dependencies") or []
        if not deps:
            return [], []
        lut_root = self._managed_lut_root()
        if not path_within(lut_root, self._repository.root):
            raise _ImportVerifiedError(REASON_DEPENDENCY_ESCAPED,
                                       "LUTs directory escapes library root")
        warnings: List[str] = []
        records: List[Dict[str, Any]] = []
        resource_dir = os.path.join(txn_dir, "resources")
        for dep in deps:
            name = dep.get("name")
            if not isinstance(name, str) or not name:
                continue
            if not dep.get("bundled"):
                warnings.append(
                    f"lut not in package: {name} "
                    f"({dep.get('reason') or 'reference-only package'}) — "
                    f"grade keeps original reference")
                continue
            archive = dep.get("archive")
            expected = dep.get("sha256")
            os.makedirs(resource_dir, exist_ok=True)
            staged_path = os.path.join(
                resource_dir, "dep-" + os.path.basename(str(archive)))
            _extract_member_chunked(
                zf, str(archive), staged_path, cancel_check=cancel_check)
            digest = _sha256_file(staged_path)
            if digest != expected:
                raise _ImportVerifiedError(REASON_HASH_MISMATCH,
                                           str(archive))
            records.append({
                "source_ref": str(dep.get("source")),
                "name": name, "sha256": digest,
                "size": os.path.getsize(staged_path),
                "staged_path": staged_path})
        return records, warnings

    def _resolve_dependency_installs(
            self, dep_records: List[Dict[str, Any]],
            staged: List[Tuple[str, str]], manifest: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """解析依赖安装目标（复用/顺延前缀/拒绝碰撞）＋重写 grade 引用。

        返回 ``(resources, warnings)``——resources 是写入 journal 的段
        （只含本次要安装的条目）。碰撞策略（SR03）：目标已存在且同内容
        → 复用（不重复安装）；同前缀不同内容 → 顺延更长 hash 前缀；
        全 hash 同名异内容 → 拒绝（不覆盖外来文件）。
        """
        lut_root = self._managed_lut_root()
        resources: List[Dict[str, Any]] = []
        warnings: List[str] = []
        by_source: Dict[str, str] = {}
        for record in dep_records:
            digest = record["sha256"]
            name = record["name"]
            target, reuse = self._dependency_target(lut_root, digest, name)
            install_name = os.path.basename(target)
            record["install_name"] = install_name
            record["target_path"] = None if reuse else target
            by_source[record["source_ref"]] = install_name
            if reuse:
                warnings.append(f"lut reused: {target}")
            else:
                resources.append({"path": target,
                                  "size": record["size"],
                                  "sha256": digest})
        if by_source:
            grade_name = next(
                (f["name"] for f in manifest.get("files", [])
                 if f.get("role") == BUNDLE_ROLE_GRADE), None)
            staged_by_name = dict(staged)
            if grade_name and grade_name in staged_by_name:
                self._rewrite_staged_grade_references(
                    staged_by_name[grade_name],
                    {source: managed_lut_reference(install)
                     for source, install in by_source.items()})
                warnings.extend(
                    f"lut installed: {os.path.join(lut_root, install)}"
                    for install in sorted(set(by_source.values())))
        return resources, warnings

    def _dependency_target(self, lut_root: str, digest: str,
                           name: str) -> Tuple[str, bool]:
        """内容寻址安装目标：``<shaN>_<name>``（N 顺延 8→64）。

        returns: ``(target, reuse)``——reuse=True 表示已有同内容文件。
        raises: ``_ImportVerifiedError``——全 hash 文件名被不同内容占用。
        """
        if not path_within(os.path.join(lut_root, f"{digest[:8]}_{name}"),
                           lut_root):
            raise _ImportVerifiedError(REASON_DEPENDENCY_ESCAPED,
                                       "LUT target escapes managed directory")
        for prefix_len in (8, 12, 16, 20, 32, 64):
            candidate = os.path.join(
                lut_root, f"{digest[:prefix_len]}_{name}")
            if not os.path.lexists(candidate):
                return candidate, False
            try:
                if _sha256_file(candidate) == digest:
                    return candidate, True    # 已有同内容：幂等复用
            except OSError:
                continue
        raise _ImportVerifiedError(
            REASON_DEPENDENCY_COLLISION,
            f"{digest[:8]}_{name}: managed name occupied by foreign content")

    @staticmethod
    def _rewrite_staged_grade_references(staged_topos: str,
                                         rewrites: Dict[str, str]) -> None:
        """把 staged grade 内的依赖引用重写（staging 内，原子替换）。"""
        try:
            with zipfile.ZipFile(staged_topos, "r") as zf:
                names = zf.namelist()
                project_raw = zf.read("project.json")
                payload = {name: zf.read(name)
                           for name in names if name != "project.json"}
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise _ImportVerifiedError(REASON_NOT_A_BUNDLE,
                                       f"grade rewrite failed: {exc}")
        try:
            project = json.loads(project_raw)
        except ValueError as exc:
            raise _ImportVerifiedError(REASON_NOT_A_BUNDLE,
                                       f"grade json broken: {exc}")
        changed = False
        for layer in project.get("layers") or []:
            if isinstance(layer, dict):
                ref = layer.get("active_lut_path")
                if isinstance(ref, str) and ref in rewrites:
                    layer["active_lut_path"] = rewrites[ref]
                    changed = True
        if not changed:
            return
        tmp = staged_topos + ".rewritten"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in payload.items():
                zf.writestr(name, data)
            zf.writestr("project.json",
                        json.dumps(project, ensure_ascii=False, indent=1))
        os.replace(tmp, staged_topos)

    # ── 内部：受管引用迁移（SR04） ─────────────────────────────────────

    def migrate_album_lut_references(self, folder: str) -> List[str]:
        """把相册内**可证明归属**的绝对 LUT 引用升级为受管引用。

        只重写 realpath 位于 ``<root>/LUTs/`` 且文件内容 sha256 与文件
        名 hash 前缀一致的 ``active_lut_path``（内容证据确凿才迁移，
        SR04.3）；外部用户引用不猜。幂等：受管引用跳过，可反复执行。
        返回 warnings（迁移明细／跳过原因），供日志与状态栏呈现。
        """
        real = self._bundles._resolve_in_root(folder)
        if real is None:
            return [f"migration skipped (outside root): {folder}"]
        lut_root = os.path.realpath(self._managed_lut_root())
        try:
            names = os.listdir(real)
        except OSError as exc:
            return [f"migration skipped: {exc}"]
        try:
            lock_keys = acquire_directories(real)
        except DirectoryBusyError:
            return ["migration skipped: album busy"]
        warnings: List[str] = []
        try:
            for name in names:
                if not name.endswith(".topos"):
                    continue
                path = os.path.join(real, name)
                if not os.path.isfile(path) or os.path.islink(path):
                    continue
                self._migrate_grade_file(path, lut_root, warnings)
        finally:
            release_directories(lock_keys)
        return warnings

    def _migrate_grade_file(self, topos_path: str, lut_root: str,
                            warnings: List[str]) -> bool:
        try:
            with zipfile.ZipFile(topos_path, "r") as zf:
                if "project.json" not in zf.namelist():
                    return False
                project = json.loads(zf.read("project.json"))
                payload = {name: zf.read(name) for name in zf.namelist()
                           if name != "project.json"}
        except (OSError, ValueError, zipfile.BadZipFile):
            return False
        changed = False
        for layer in project.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            ref = layer.get("active_lut_path")
            if not isinstance(ref, str) or \
                    parse_managed_lut_reference(ref) is not None:
                continue                      # 受管引用无需迁移
            if not os.path.isabs(ref):
                continue                      # 相对引用归属不明，不猜
            candidate = os.path.realpath(ref)
            if not (candidate == lut_root
                    or candidate.startswith(lut_root + os.sep)):
                continue                      # 库外引用：用户自己的文件
            match = _INSTALL_NAME_HASH_RE.match(os.path.basename(candidate))
            if not match or not os.path.isfile(candidate):
                continue
            try:
                digest = _sha256_file(candidate)
            except OSError:
                continue
            if not digest.startswith(match.group(1)):
                continue                      # 内容与文件名前缀不符：证据不足
            layer["active_lut_path"] = managed_lut_reference(
                os.path.basename(candidate))
            changed = True
            warnings.append(
                f"migrated lut reference: {os.path.basename(topos_path)} "
                f"-> {layer['active_lut_path']}")
        if not changed:
            return False
        tmp = topos_path + f".migrate-{os.getpid()}.tmp"
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for member, data in payload.items():
                    zf.writestr(member, data)
                zf.writestr("project.json",
                            json.dumps(project, ensure_ascii=False,
                                       indent=1))
            os.replace(tmp, topos_path)
        except OSError as exc:
            _silent_remove(tmp)
            warnings.append(f"migration failed for "
                            f"{os.path.basename(topos_path)}: {exc}")
            return False
        return True

    # ── 内部：stem／身份 ───────────────────────────────────────────────

    def _resolve_import_stem(self, folder: str, base_stem: str,
                             explicit: bool
                             ) -> Tuple[Optional[str], List[str]]:
        """目标名解析：占用语义＝任意 bundle 成员名（非仅 ``.topos``）。

        returns: ``(stem, warnings)``；显式名冲突时 ``(None, (reason,
        detail))``——显式名绝不静默顺延（用户指定的名字被占必须可见）。
        """
        problem = self._bundles._validate_target(folder, base_stem)
        if problem is None:
            return base_stem, []
        reason, detail = problem
        if explicit or reason == REASON_IO_ERROR:
            return None, [reason, detail]
        for counter in range(1, IMPORT_MAX_STEM_SUFFIX):
            candidate = f"{base_stem}_{counter}"
            if self._bundles._validate_target(folder, candidate) is None:
                return candidate, [f"imported as: {candidate}"]
        return None, [reason, detail]

    def _unoccupied_stem(self, folder: str, stem: str) -> str:
        """重名自动后缀分配（全成员占用口径；结果可审阅）。"""
        resolved, _ = self._resolve_import_stem(folder, stem, explicit=False)
        if resolved is None:
            raise RuntimeError(f"no unoccupied stem for {stem!r}")
        return resolved

    def _identity_conflict_action(self, folder: str, manifest: Dict[str, Any],
                                  origin_id: Optional[str]) -> Optional[str]:
        """同 ID 冲突判定：None=无冲突 / "duplicate" / "reallocate"。

        BR02：指纹从"仅 grade hash"扩为**完整 bundle 内容**——角色集合
        （grade/参考图/EXR 对/声明/meta）一致且逐角色 sha256 相同才算
        重复；任何差异（含精度/参考图不同）→ 换发本库新 ID。
        作用域＝目标相册（同一 still 复制进另一相册是合法操作）。
        """
        if not origin_id:
            return None
        try:
            names = os.listdir(folder)
        except OSError:
            return None
        role_files = {f.get("role"): f for f in manifest.get("files", [])
                      if isinstance(f, dict)}
        for name in names:
            if not name.endswith(".topos"):
                continue
            meta = load_preset_meta(os.path.join(folder, name))
            if meta is None or meta.still_id != origin_id:
                continue
            if self._bundle_fingerprint_matches(os.path.join(folder, name),
                                                role_files):
                return "duplicate"       # 同 ID 同内容：重复导入拒绝
            return "reallocate"          # 同 ID 不同内容：换发本库新 ID
        return None

    def _reallocate_imported_identity(self, staged_topos: str,
                                      origin_id: str,
                                      warnings: List[str]) -> bool:
        """origin ID 保留进 extra_fields，分配并落盘本库新 ID。

        BR02：在 **staging** 内完成（文件进入目标目录之前）；保存失败
        返回 False → 整笔导入拒绝，绝不留下未解决的身份冲突。
        """
        meta = load_preset_meta(staged_topos)
        if meta is None:
            meta = PresetMeta()
        meta.extra_fields["origin_still_id"] = origin_id
        meta.still_id = allocate_still_id()
        if not save_preset_meta(staged_topos, meta):
            return False
        warnings.append(
            f"still_id {origin_id} conflicted; allocated "
            f"{meta.still_id} (origin kept)")
        return True

    def _bundle_fingerprint_matches(self, topos_path: str,
                                    role_files: Dict[str, Dict[str, Any]]
                                    ) -> bool:
        """导入 manifest 与本地 bundle 的完整内容指纹比对。"""
        folder = os.path.dirname(topos_path)
        stem = os.path.basename(topos_path)[:-len(".topos")]
        local_paths: Dict[str, str] = {}
        for role, suffix in BUNDLE_SUFFIX_BY_ROLE.items():
            path = os.path.join(folder, f"{stem}{suffix}")
            if os.path.isfile(path):
                local_paths[role] = path
        if set(local_paths) != set(role_files):
            return False                 # 角色集合不同：精度/参考不同
        for role, path in local_paths.items():
            listed = role_files.get(role) or {}
            digest = listed.get("sha256")
            if not isinstance(digest, str):
                return False
            try:
                if _sha256_file(path) != digest:
                    return False
            except OSError:
                return False
        return True

    # ── 崩溃恢复（SR02：内容归属＋登记表＋保全优先） ────────────────────

    def _recover_import_txns(self, folder: str) -> None:
        """收敛遗留导入事务（installing → 按意图撤掉已装部分）。

        BR01 纪律：坏 journal／其他进程在途 → 保全不动；撤除失败保留
        事务目录供重试/人工处理。SR01：进程内登记的在场事务跳过；
        SR03：恢复同时锁住受管资源目录（resources 撤除）。
        """
        if not os.path.isdir(folder):
            return
        with try_recover_lock(folder, self._managed_lut_root()) as lock:
            if not lock.acquired:
                return
            for name in sorted(os.listdir(folder)):
                if not name.startswith(IMPORT_TXN_PREFIX):
                    continue
                txn_dir = os.path.join(folder, name)
                if os.path.islink(txn_dir):
                    logger.warning("gallery import: quarantine %s "
                                   "(symlink txn dir)", txn_dir)
                    continue
                if ACTIVE_TXNS.is_active(txn_dir):
                    continue             # SR01：在场事务不扫
                try:
                    journal = read_journal(
                        os.path.join(txn_dir, IMPORT_JOURNAL_NAME))
                except JournalError as exc:
                    logger.warning("gallery import: quarantine %s (%s)",
                                   txn_dir, exc)
                    continue             # 坏日志：保全，不据此动文件
                if other_process_alive(journal.get("pid")):
                    continue
                if not self._import_journal_actionable(journal, folder,
                                                       txn_dir):
                    logger.warning("gallery import: quarantine %s "
                                   "(unknown state/paths or duplicates)",
                                   txn_dir)
                    continue
                if journal.get("state") != "committed":
                    if self._rollback_import(journal):
                        logger.warning(
                            "gallery import: rollback incomplete for %s, "
                            "kept for retry", txn_dir)
                        continue
                shutil.rmtree(txn_dir, ignore_errors=True)

    def _import_journal_actionable(self, journal: Dict[str, Any],
                                   folder: str, txn_dir: str) -> bool:
        """恢复前置校验：状态已知、finals 属目标相册、resources 属受管
        目录、无重复路径（SR02.4——日志是意图，不是删除授权的全部证据）。
        """
        state = journal.get("state")
        if state not in ("staging", "installing", "committed"):
            return False
        finals = journal.get("finals", [])
        paths = [entry.get("path") if isinstance(entry, dict) else entry
                 for entry in finals]
        if any(not isinstance(path, str)
               or not path_within(path, folder)
               or os.path.dirname(os.path.abspath(path))
               != os.path.abspath(folder) for path in paths):
            return False
        if len(set(paths)) != len(paths):
            return False
        lut_root = os.path.realpath(self._managed_lut_root())
        resources = journal.get("resources", [])
        if not isinstance(resources, list):
            return False
        resource_paths = [entry.get("path")
                          for entry in resources
                          if isinstance(entry, dict)]
        if any(not isinstance(path, str)
               or not path_within(path, lut_root)
               for path in resource_paths):
            return False
        if len(set(resource_paths)) != len(resource_paths):
            return False
        return True

    @staticmethod
    def _rollback_import(journal: Dict[str, Any]) -> List[str]:
        """按 journal 意图撤掉已安装成员＋资源；返回撤除失败的条目。

        SR02：删除授权来自**内容证据**——finals/resources 带 sha256 且
        当前内容一致才撤；无 hash 的旧格式日志不猜归属（保全）。
        """
        errors: List[str] = []

        def _owned(path: str, entry: Dict[str, Any]) -> Optional[bool]:
            """True=证据齐全且内容一致；False=内容不符；None=无证据。"""
            digest = entry.get("sha256")
            if not isinstance(digest, str):
                return None
            size = entry.get("size")
            if type(size) is int and (
                    not os.path.isfile(path)
                    or os.path.getsize(path) != size):
                return False
            try:
                return _sha256_file(path) == digest
            except OSError:
                return False

        for final in reversed(journal.get("finals", [])):
            if isinstance(final, dict):
                path = final.get("path")
            else:
                path, final = final, {}
            if not isinstance(path, str) or not os.path.lexists(path):
                continue
            try:
                verdict = _owned(path, final)
                if verdict is None:
                    # 旧格式日志：没有内容证据就没有删除授权（保全）
                    errors.append(
                        f"{os.path.basename(path)}: no content evidence")
                    continue
                if verdict is False:
                    # 日志声称装过此文件而磁盘内容不符：矛盾现场保全
                    errors.append(
                        f"{os.path.basename(path)}: content mismatch")
                    continue
                os.remove(path)
            except OSError as exc:
                errors.append(f"{os.path.basename(path)}: {exc}")
        for resource in reversed(journal.get("resources", []) or []):
            if not isinstance(resource, dict):
                continue
            path = resource.get("path")
            if not isinstance(path, str) or not os.path.lexists(path):
                continue
            try:
                if not _owned(path, resource):
                    continue
                os.remove(path)
            except OSError as exc:
                errors.append(f"{os.path.basename(path)}: {exc}")
        return errors

    @staticmethod
    def _rejected(request_id: str, reason: str, detail: str, *,
                  recoverable: bool = False,
                  affected_still_ids: Tuple[str, ...] = ()
                  ) -> GalleryOperationResult:
        return GalleryOperationResult(
            request_id=request_id, status="rejected", reason=reason,
            warnings=(detail,) if detail else (), recoverable=recoverable,
            affected_still_ids=affected_still_ids)


class _ExportVerifiedError(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class _ImportVerifiedError(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class _ImportCancelled(RuntimeError):
    """协作取消（commit 前零落地；状态如实返回 cancelled）。"""


def _extract_member_chunked(zf: zipfile.ZipFile, name: str, target: str, *,
                            expected_size: Optional[int] = None,
                            cancel_check: Optional[Callable[[], bool]] = None
                            ) -> int:
    """分块解压单个成员：取消在块间生效，字节数与 ZipInfo 核对。"""
    written = 0
    try:
        with zf.open(name) as src, open(target, "wb") as dst:
            while True:
                if cancel_check is not None and cancel_check():
                    raise _ImportCancelled()
                chunk = src.read(_EXTRACT_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                written += len(chunk)
    except zipfile.BadZipFile as exc:
        raise _ImportVerifiedError(REASON_NOT_A_BUNDLE,
                                   f"{name}: {exc}") from exc
    if expected_size is not None and written != expected_size:
        raise _ImportVerifiedError(
            REASON_SIZE_LIMIT, f"{name}: byte count {written} != "
                               f"{expected_size}")
    return written


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
