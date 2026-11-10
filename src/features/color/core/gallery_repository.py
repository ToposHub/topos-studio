"""G01：Gallery 本地读侧 repository——User/System 平铺库的查询 adapter。

把 ``PresetsPanel.scan_presets`` 的磁盘读取提取为可独立测试的查询服务
（计划 §5 G01："提取当前 scan_presets 的读取结果为查询 adapter，先保持
User/System 平铺行为"）：

- ``list_albums``：User／System 及各自一层子目录 → legacy-folder album
  （``scope=legacy-folder``、``kind=stills``）；``read_only`` 来自
  ``os.access`` 实测，不靠结构推断。
- ``query``：平铺 ``.topos`` 扫描（跳过 dotfile／非文件，与旧行为一致），
  sidecar 存在性用**同一次 listdir 的文件名集合**判断（每条目不重复
  stat）；搜索过滤复用 ``preset_meta.matches_query`` 保持口径一致。
- grade 可加载性**不**在查询路径探测（大库逐条开 zip 的成本留给 G05
  异步索引）；宿主对当前选中项用 ``still_at(..., probe_grade=True)``
  单独探测，探测实现可注入（app 层可换成 SaveRouter 口径）。
- ``still_id``：meta 携带稳定身份（G02 schema 2，抓帧时分配、rename/move
  不变）时直接使用；无 meta 的旧条目退化为文件名派生临时身份（首次写入
  时 ``ensure_still_identity`` 原子补齐）。
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from typing import Callable, Dict, Optional, Tuple

from src.features.color.core.gallery_contracts import (
    ALBUM_KIND_STILLS,
    BUNDLE_ROLE_EXR_DECLARATION,
    BUNDLE_ROLE_GRADE,
    BUNDLE_ROLE_META,
    BUNDLE_ROLE_STILL_EXR,
    BUNDLE_ROLE_STILL_JPEG,
    BUNDLE_ROLE_THUMBNAIL,
    BUNDLE_SUFFIX_BY_ROLE,
    GalleryAlbum,
    GalleryBundleFile,
    GalleryBundleManifest,
    GalleryBundleRef,
    GalleryQuery,
    GalleryQueryResult,
    GalleryStill,
    GalleryStillProvenance,
    PRECISION_EXR_FLOAT32,
    PRECISION_JPEG8,
    PRECISION_NONE,
    REQUIRED_BUNDLE_ROLES,
    SCOPE_LEGACY_FOLDER,
    SORT_KEY_CREATED,
    SORT_KEY_LABEL,
    SORT_KEY_NAME,
)
from src.features.color.core.preset_meta import (
    PresetMeta,
    load_preset_meta,
    still_alpha_declaration,
)

# 稳定扫描失败原因（GalleryQueryResult.scan_error）
SCAN_ERROR_ALBUM_MISSING = "album_missing"
SCAN_ERROR_FOLDER_MISSING = "folder_missing"

GradeProbe = Callable[[str], bool]


def _default_grade_probe(topos_path: str) -> bool:
    """默认 grade 探测：.topos 是可读 zip 且含 project.json。

    这是"可加载"的结构性下限（完整加载语义属 SaveRouter，app 层可用
    ``grade_probe=`` 注入更严格口径）；任何 OSError 视为不可加载。
    """
    try:
        if not zipfile.is_zipfile(topos_path):
            return False
        with zipfile.ZipFile(topos_path, "r") as zf:
            return "project.json" in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _folder_revision(folder: str) -> int:
    """album revision：目录 mtime_ns（真实变化才变；缺失为 0）。"""
    try:
        return os.stat(folder).st_mtime_ns
    except OSError:
        return 0


class GalleryFolderRepository:
    """既有 User/System 平铺目录结构的只读查询 repository。"""

    def __init__(self, root: str, *,
                 grade_probe: Optional[GradeProbe] = None) -> None:
        self._root = os.path.abspath(root)
        self._grade_probe: GradeProbe = grade_probe or _default_grade_probe

    # ── album 枚举 ─────────────────────────────────────────────────────

    @property
    def root(self) -> str:
        return self._root

    def album_id_for_rel(self, rel_parts: Tuple[str, ...]) -> str:
        return "legacy:" + "/".join(rel_parts)

    def album_id_for_folder(self, folder: str) -> str:
        """目录路径 → album_id（越界目录退化为按 basename 的诚实 ID）。"""
        folder = os.path.abspath(folder)
        rel = os.path.relpath(folder, self._root)
        parts: Tuple[str, ...]
        if rel == "." or rel.startswith(".."):
            # 根目录或库外目录：legacy 模型没有对应集合，用稳定派生 ID，
            # 调用方（能力评估）会因 album 解析不到而得到 album_missing。
            parts = ("__unmapped__", os.path.basename(folder) or "root")
        else:
            parts = tuple(rel.replace("\\", "/").split("/"))
        return self.album_id_for_rel(parts)

    def list_albums(self) -> Tuple[GalleryAlbum, ...]:
        """User／System 及各自一层子目录（与 folder_combo 口径一致）。"""
        albums: list[GalleryAlbum] = []
        for section in ("User", "System"):
            section_dir = os.path.join(self._root, section)
            section_id = self.album_id_for_rel((section,))
            albums.append(GalleryAlbum(
                album_id=section_id,
                scope=SCOPE_LEGACY_FOLDER,
                kind=ALBUM_KIND_STILLS,
                label=section,
                read_only=not os.access(section_dir, os.W_OK),
                revision=_folder_revision(section_dir),
            ))
            if not os.path.isdir(section_dir):
                continue
            try:
                subdirs = sorted(
                    name for name in os.listdir(section_dir)
                    if not name.startswith(".")
                    and os.path.isdir(os.path.join(section_dir, name)))
            except OSError:
                continue
            for name in subdirs:
                sub_dir = os.path.join(section_dir, name)
                albums.append(GalleryAlbum(
                    album_id=self.album_id_for_rel((section, name)),
                    scope=SCOPE_LEGACY_FOLDER,
                    kind=ALBUM_KIND_STILLS,
                    label=f"{section}/{name}",
                    parent_id=section_id,
                    read_only=not os.access(sub_dir, os.W_OK),
                    revision=_folder_revision(sub_dir),
                ))
        return tuple(albums)

    def album_folder(self, album_id: str) -> Optional[str]:
        """album_id → 目录路径；解析不到（含越界段）返回 None。"""
        if not album_id.startswith("legacy:"):
            return None
        rel = album_id[len("legacy:"):]
        parts = rel.split("/")
        if not parts or any(
                part in ("", ".", "..") for part in parts):
            return None
        folder = os.path.join(self._root, *parts)
        # 二次确认解析结果仍在根内（symlink 越界交由 G03 事务统一收紧；
        # 这里至少不把 ../.. 直接拼进路径）。
        real_folder = os.path.abspath(folder)
        if not real_folder.startswith(self._root + os.sep) \
                and real_folder != self._root:
            return None
        resolved = os.path.realpath(real_folder)
        root = os.path.realpath(self._root)
        if resolved != root and not resolved.startswith(root + os.sep):
            return None
        return real_folder

    def album_for_folder(self, folder: str) -> Optional[GalleryAlbum]:
        """目录路径 → album DTO（与 list_albums 同一来源）。"""
        target = os.path.abspath(folder)
        for album in self.list_albums():
            album_dir = self.album_folder(album.album_id)
            if album_dir is not None and album_dir == target:
                return album
        return self._find_album_by_id(self.album_id_for_folder(target))

    # ── 查询 ───────────────────────────────────────────────────────────

    def query(self, query: GalleryQuery, *,
              probe_grades: bool = False) -> GalleryQueryResult:
        """平铺扫描 + 过滤 + 排序；失败原因进 ``scan_error`` 不抛异常。"""
        # 先按 ID 找 album（不认识的身份 → album_missing，不猜路径）；
        # 认识但目录缺失/不可读 → folder_missing。
        album = self._find_album_by_id(query.album_id)
        folder = self.album_folder(query.album_id)
        if album is None or folder is None:
            return GalleryQueryResult(
                album=album, stills=(), request_revision=query.request_revision,
                scan_error=SCAN_ERROR_ALBUM_MISSING)
        return self.query_folder(folder, query, album=album,
                                 probe_grades=probe_grades)

    def query_folder(self, folder: str, query: GalleryQuery, *,
                     album: Optional[GalleryAlbum] = None,
                     album_id: Optional[str] = None,
                     probe_grades: bool = False) -> GalleryQueryResult:
        """直接对目录平铺扫描（G04：集合 scope 服务供 PowerGrade 等非
        legacy 目录复用同一读取口径）。"""
        from src.features.color.core.preset_meta import matches_query

        if album is None:
            album = self.album_for_folder(folder)
        if not os.path.isdir(folder):
            return GalleryQueryResult(
                album=album, stills=(), request_revision=query.request_revision,
                scan_error=SCAN_ERROR_FOLDER_MISSING)

        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return GalleryQueryResult(
                album=album, stills=(), request_revision=query.request_revision,
                scan_error=SCAN_ERROR_FOLDER_MISSING)

        effective_album_id = album_id or query.album_id
        existing = {name for name in names
                    if os.path.isfile(os.path.join(folder, name))}
        stills: list[GalleryStill] = []
        for name in names:
            if name.startswith(".") or not name.endswith(".topos"):
                continue
            if name not in existing:
                continue
            still = self._still_from_name(
                folder, name, effective_album_id, existing,
                probe_grade=probe_grades)
            if not matches_query(still.display_name, still.meta,
                                 query.search_text):
                continue
            stills.append(still)

        self._sort_stills(stills, query)
        total_matched = len(stills)
        if query.limit is not None:
            stills = stills[:query.limit]
        degraded = sum(1 for s in stills if not s.has_meta)
        return GalleryQueryResult(
            album=album,
            stills=tuple(stills),
            request_revision=query.request_revision,
            total_matched=total_matched,
            degraded_count=degraded,
        )

    def still_at(self, folder: str, topos_path: str, *,
                 probe_grade: bool = False) -> Optional[GalleryStill]:
        """按 .topos 路径重建单条 still 记录（宿主取选中项用）。"""
        folder = os.path.abspath(folder)
        if not os.path.isfile(topos_path):
            return None
        name = os.path.basename(topos_path)
        try:
            existing = {entry for entry in os.listdir(folder)
                        if os.path.isfile(os.path.join(folder, entry))}
        except OSError:
            existing = set()
        album_id = self.album_id_for_folder(folder)
        return self._still_from_name(
            folder, name, album_id, existing, probe_grade=probe_grade)

    # ── bundle manifest ────────────────────────────────────────────────

    def bundle_manifest(self, still: GalleryStill,
                        folder: Optional[str] = None, *,
                        with_hashes: bool = False
                        ) -> GalleryBundleManifest:
        """枚举 bundle 全部六个角色的文件事实（G03 事务将复用同一清单）。"""
        if folder is None:
            resolved = self.album_folder(still.album_id)
            folder = resolved if resolved is not None else self._root
        files: list[GalleryBundleFile] = []
        for role, suffix in BUNDLE_SUFFIX_BY_ROLE.items():
            relative = still.bundle.sidecar_name(role) \
                if role != BUNDLE_ROLE_GRADE else still.bundle.topos_name
            path = os.path.join(folder, relative)
            exists = os.path.isfile(path)
            size: Optional[int] = None
            mtime: Optional[int] = None
            digest: Optional[str] = None
            if exists:
                try:
                    stat = os.stat(path)
                    size, mtime = stat.st_size, stat.st_mtime_ns
                except OSError:
                    exists = False
            if exists and with_hashes:
                digest = _sha256_file(path)
            files.append(GalleryBundleFile(
                role=role,
                relative_path=relative,
                required=role in REQUIRED_BUNDLE_ROLES,
                exists=exists,
                size_bytes=size,
                mtime_ns=mtime,
                sha256=digest,
            ))
        alpha: Optional[str] = None
        exr_file = next(
            (f for f in files if f.role == BUNDLE_ROLE_STILL_EXR), None)
        if exr_file is not None and exr_file.exists:
            # still_alpha_declaration 以 topos 路径推导 <stem>.still.exr.json
            alpha = still_alpha_declaration(
                os.path.join(folder, still.bundle.topos_name))
        return GalleryBundleManifest(
            schema_version=1,          # 磁盘事实：save_preset_meta 当前写 1
            still_id=still.still_id,
            files=tuple(files),
            still_precision=_manifest_precision(still, exr_file.exists
                                                if exr_file else False),
            alpha_semantics=alpha,
        )

    # ── 内部 ───────────────────────────────────────────────────────────

    def _find_album_by_id(self, album_id: str) -> Optional[GalleryAlbum]:
        for album in self.list_albums():
            if album.album_id == album_id:
                return album
        folder = self.album_folder(album_id)
        if folder is not None and os.path.isdir(folder):
            relative = os.path.relpath(folder, self._root)
            parts = tuple(relative.split(os.sep))
            if parts and parts[0] in ("User", "System"):
                return GalleryAlbum(
                    album_id=album_id, scope=SCOPE_LEGACY_FOLDER,
                    kind=ALBUM_KIND_STILLS, label="/".join(parts),
                    parent_id=self.album_id_for_rel(parts[:-1]) if len(parts) > 1 else None,
                    read_only=not os.access(folder, os.W_OK),
                    revision=_folder_revision(folder))
        return None

    def _still_from_name(self, folder: str, topos_name: str,
                         album_id: str, existing: set,
                         *, probe_grade: bool) -> GalleryStill:
        topos_path = os.path.join(folder, topos_name)
        stem = topos_name[:-len(".topos")]
        meta: Optional[PresetMeta] = load_preset_meta(topos_path)
        flags = {
            BUNDLE_ROLE_THUMBNAIL: f"{stem}.thumb.jpg" in existing,
            BUNDLE_ROLE_STILL_JPEG: f"{stem}.still.jpg" in existing,
            BUNDLE_ROLE_META: f"{stem}.meta.json" in existing,
            BUNDLE_ROLE_STILL_EXR: f"{stem}.still.exr" in existing,
        }
        try:
            content_revision = os.stat(topos_path).st_mtime_ns
        except OSError:
            content_revision = 0
        grade_loadable: Optional[bool] = None
        if probe_grade:
            grade_loadable = self._grade_probe(topos_path)
        return GalleryStill(
            # G02：meta 携带稳定身份/label 时优先（跨 rename 不变）；
            # 无 meta 的旧条目退化为文件名派生临时身份（首次写入时由
            # ensure_still_identity 原子补齐）。
            still_id=(meta.still_id if meta is not None and meta.still_id
                      else _provisional_still_id(album_id, stem)),
            album_id=album_id,
            bundle=GalleryBundleRef(topos_name=topos_name),
            display_name=stem,
            label=(meta.label if meta is not None else None),
            provenance=GalleryStillProvenance.from_preset_meta(meta),
            created_at=getattr(meta, "created_at", None),
            content_revision=content_revision,
            still_precision=getattr(meta, "still_precision", None),
            working_space=getattr(meta, "working_space", None),
            display_transform=getattr(meta, "display_transform", None),
            has_exr=flags[BUNDLE_ROLE_STILL_EXR],
            has_still_jpeg=flags[BUNDLE_ROLE_STILL_JPEG],
            has_thumbnail=flags[BUNDLE_ROLE_THUMBNAIL],
            has_meta=flags[BUNDLE_ROLE_META],
            grade_loadable=grade_loadable,
            meta=meta,
        )

    @staticmethod
    def _sort_stills(stills: list, query: GalleryQuery) -> None:
        """稳定排序：主键按方向，次键 still_id 恒升序（两趟稳定排序）。

        ``SORT_KEY_NAME`` 的主键是**含扩展名的完整文件名、区分大小写**——
        与旧 ``sorted(os.listdir(folder))`` 插入顺序逐字节一致（新排序键
        label/created_at 无旧序可保，用小写归一）。"""
        secondary = lambda s: (s.still_id or "",)  # noqa: E731
        stills.sort(key=secondary)
        if query.sort_key == SORT_KEY_NAME:
            primary = lambda s: s.bundle.topos_name   # noqa: E731
        elif query.sort_key == SORT_KEY_LABEL:
            primary = lambda s: (s.label or "").lower()  # noqa: E731
        else:
            primary = lambda s: (s.created_at or "")    # noqa: E731
        stills.sort(key=primary, reverse=query.sort_desc)


def _manifest_precision(still: GalleryStill, has_exr: bool) -> str:
    if has_exr:
        return PRECISION_EXR_FLOAT32
    declared = still.still_precision
    if declared in (PRECISION_EXR_FLOAT32, PRECISION_JPEG8):
        return declared
    if still.has_still_jpeg or still.has_thumbnail:
        return PRECISION_JPEG8
    return PRECISION_NONE


def _provisional_still_id(album_id: str, stem: str) -> str:
    """文件名派生临时身份（sha256 前 16 位，hex）；G02 替换为落盘身份。"""
    digest = hashlib.sha256(
        f"{album_id}\x00{stem}".encode("utf-8")).hexdigest()
    return digest[:16]


def _sha256_file(path: str) -> Optional[str]:
    import hashlib as _hashlib
    hasher = _hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                hasher.update(chunk)
    except OSError:
        return None
    return hasher.hexdigest()
