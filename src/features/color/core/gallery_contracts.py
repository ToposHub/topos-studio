"""G01：Gallery 领域合同——查询／能力／请求结果的纯数据单一事实源。

计划 ``docs/plans/2026-11-09-color-gallery-backend-plan.md`` §4：Gallery
的读取散在 ``PresetsPanel.scan_presets``，能力位没有注册表，延迟任务没有
上下文校验口径。本模块定义查询 DTO、能力评估与请求/结果合同，供本地
repository（读侧）与 app 层 adapter／宿主消费。

契约边界：

- 本模块不 import PyQt／ModernGL／cv2／线程对象，不携带机器绝对路径；
  路径解析由 ``GalleryFolderRepository`` 与 app 层 adapter 负责，DTO 里
  只放 album 相对引用（``GalleryBundleRef.topos_name``）。
- **身份**：``still_id`` 优先取 .meta.json 里的稳定身份（G02 schema 2：
  抓帧时随机分配、跨 rename/move 不变）；无 meta 的旧条目退化为文件名
  派生临时身份（album_id ＋ 文件主名的 sha256 前 16 位），首次写入
  （rename／改 label）时由 ``ensure_still_identity`` 原子补齐。
- 未实现服务（import／独立 label／split presenter／集合 scope）能力位
  如实 False ＋ 稳定 reason，不冒充已接线。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:  # 仅类型标注；运行时不引入 cv2 依赖链
    from src.features.color.core.preset_meta import PresetMeta

# ── Album scope / kind（G04 集合服务落地前只有 legacy-folder） ──────────
SCOPE_PROJECT = "project"
SCOPE_GLOBAL = "global"
SCOPE_LEGACY_FOLDER = "legacy-folder"

ALBUM_KIND_STILLS = "stills"
ALBUM_KIND_POWERGRADE = "powergrade"
ALBUM_KIND_TIMELINE = "timeline"

_ALBUM_SCOPES = (SCOPE_PROJECT, SCOPE_GLOBAL, SCOPE_LEGACY_FOLDER)
_ALBUM_KINDS = (ALBUM_KIND_STILLS, ALBUM_KIND_POWERGRADE, ALBUM_KIND_TIMELINE)

# ── Bundle 文件角色（含既有 _SIDECAR_SUFFIXES 遗漏的 .still.exr.json） ──
BUNDLE_ROLE_GRADE = "grade"                  # .topos 工程包（唯一必需角色）
BUNDLE_ROLE_THUMBNAIL = "thumbnail"          # .thumb.jpg（512 长边）
BUNDLE_ROLE_STILL_JPEG = "still_jpeg"        # .still.jpg（1920 长边参考）
BUNDLE_ROLE_META = "meta"                    # .meta.json（来源/精度声明）
BUNDLE_ROLE_STILL_EXR = "still_exr"          # .still.exr（float32 高精参考）
BUNDLE_ROLE_EXR_DECLARATION = "exr_declaration"  # .still.exr.json（alpha 声明）

BUNDLE_SUFFIX_BY_ROLE: Dict[str, str] = {
    BUNDLE_ROLE_GRADE: ".topos",
    BUNDLE_ROLE_THUMBNAIL: ".thumb.jpg",
    BUNDLE_ROLE_STILL_JPEG: ".still.jpg",
    BUNDLE_ROLE_META: ".meta.json",
    BUNDLE_ROLE_STILL_EXR: ".still.exr",
    BUNDLE_ROLE_EXR_DECLARATION: ".still.exr.json",
}
BUNDLE_ROLE_BY_SUFFIX: Dict[str, str] = {
    v: k for k, v in BUNDLE_SUFFIX_BY_ROLE.items()}

# 必需角色：只有 grade 包缺失才算 bundle 不完整；sidecar 各自可选并在
# manifest 里如实记录 exists（成功抓帧的 meta 写失败会 drop meta，属合法）。
REQUIRED_BUNDLE_ROLES: Tuple[str, ...] = (BUNDLE_ROLE_GRADE,)

# ── 查询排序 ───────────────────────────────────────────────────────────
SORT_KEY_NAME = "name"
SORT_KEY_LABEL = "label"
SORT_KEY_CREATED = "created_at"
_SORT_KEYS = (SORT_KEY_NAME, SORT_KEY_LABEL, SORT_KEY_CREATED)

# ── 操作结果状态 ───────────────────────────────────────────────────────
OP_SUCCESS = "success"
OP_REJECTED = "rejected"
OP_CANCELLED = "cancelled"
OP_PARTIAL = "partial"
_OP_STATUSES = (OP_SUCCESS, OP_REJECTED, OP_CANCELLED, OP_PARTIAL)

# ── 参考精度词汇（与 preset_meta.load_still_best 返回一致） ──────────────
PRECISION_EXR_FLOAT32 = "exr_float32"
PRECISION_JPEG8 = "jpeg8"
PRECISION_NONE = "none"


def make_gallery_request_id() -> str:
    """分配新的 Gallery 请求 ID（一动作一请求一回填）。"""
    return uuid.uuid4().hex


def is_safe_bundle_stem(name: str) -> bool:
    """校验 bundle 主名（文件操作的安全名；G03 单一来源）。

    拒绝空／纯空白、``.``/``..``、路径分隔符（含 Windows 反斜杠）与
    控制字符——防止 ``../x`` 之类把文件写出集合目录。与 UI 旧
    ``_is_safe_preset_name`` 语义一致（中缀点合法）。
    """
    import os as _os
    if not name or not name.strip():
        return False
    name = name.strip()
    if name in {".", ".."}:
        return False
    seps = {"/", "\\", _os.sep}
    if _os.altsep:
        seps.add(_os.altsep)
    if any(sep in name for sep in seps):
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return False
    return True


@dataclass(frozen=True)
class GalleryAlbum:
    """Gallery 集合：G01 只描述既有 User/System 平铺目录（legacy-folder）。

    ``project``／``global`` scope 与 powergrade/timeline kind 由 G04 集合
    服务落地后才会出现；``revision`` 为目录时间戳（repository 口径）。
    """

    album_id: str
    scope: str
    kind: str
    label: str
    parent_id: Optional[str] = None
    read_only: bool = False
    project_id: Optional[str] = None
    timeline_id: Optional[str] = None
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.album_id:
            raise ValueError("album_id must not be empty")
        if self.scope not in _ALBUM_SCOPES:
            raise ValueError(f"unknown album scope: {self.scope!r}")
        if self.kind not in _ALBUM_KINDS:
            raise ValueError(f"unknown album kind: {self.kind!r}")

    @property
    def writable(self) -> bool:
        return not self.read_only

    def to_dict(self) -> Dict[str, Any]:
        return {
            "album_id": self.album_id,
            "scope": self.scope,
            "kind": self.kind,
            "label": self.label,
            "parent_id": self.parent_id,
            "read_only": self.read_only,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "revision": self.revision,
        }


def _contains_path_separator(name: str) -> bool:
    """相对引用必须是纯文件名——拒绝任何路径分隔符（含 Windows 反斜杠）。"""
    import os as _os
    seps = {"/", "\\", _os.sep}
    if _os.altsep:
        seps.add(_os.altsep)
    return any(sep in name for sep in seps)


@dataclass(frozen=True)
class GalleryBundleRef:
    """bundle 在 album 内的相对引用（不含机器路径）。

    ``topos_name`` 是含 ``.topos`` 扩展的文件名；sidecar 名按
    ``BUNDLE_SUFFIX_BY_ROLE`` 由主名派生。
    """

    topos_name: str

    def __post_init__(self) -> None:
        if not self.topos_name or _contains_path_separator(self.topos_name):
            raise ValueError(
                f"topos_name must be a bare file name: {self.topos_name!r}")

    def stem(self) -> str:
        name = self.topos_name
        return name[:-len(".topos")] if name.endswith(".topos") else name

    def sidecar_name(self, role: str) -> str:
        return f"{self.stem()}{BUNDLE_SUFFIX_BY_ROLE[role]}"

    def to_dict(self) -> Dict[str, Any]:
        return {"topos_name": self.topos_name}


@dataclass(frozen=True)
class GalleryStillProvenance:
    """来源快照（.meta.json 的 provenance 子集；旧条目可为空）。"""

    clip_id: Optional[str] = None
    asset_id: Optional[str] = None
    source_display_name: Optional[str] = None
    timeline_id: Optional[str] = None
    timeline_name: Optional[str] = None
    timeline_frame: Optional[int] = None
    source_frame: Optional[int] = None
    source_transfer: Optional[str] = None
    source_primaries: Optional[str] = None
    source_range: Optional[str] = None

    @classmethod
    def from_preset_meta(cls, meta: Any) -> "GalleryStillProvenance":
        """从 ``PresetMeta`` 提取来源字段（缺 meta 时全空）。"""
        def _opt(key: str) -> Optional[Any]:
            return getattr(meta, key, None) if meta is not None else None
        return cls(
            clip_id=_opt("clip_id"),
            asset_id=_opt("asset_id"),
            source_display_name=_opt("display_name"),
            timeline_id=_opt("timeline_id"),
            timeline_name=_opt("timeline_name"),
            timeline_frame=_opt("timeline_frame"),
            source_frame=_opt("source_frame"),
            source_transfer=_opt("source_transfer"),
            source_primaries=_opt("source_primaries"),
            source_range=_opt("source_range"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "asset_id": self.asset_id,
            "source_display_name": self.source_display_name,
            "timeline_id": self.timeline_id,
            "timeline_name": self.timeline_name,
            "timeline_frame": self.timeline_frame,
            "source_frame": self.source_frame,
            "source_transfer": self.source_transfer,
            "source_primaries": self.source_primaries,
            "source_range": self.source_range,
        }


@dataclass(frozen=True)
class GalleryStill:
    """单条 Gallery 静帧条目（查询结果，非持久化写模型）。"""

    still_id: Optional[str]
    album_id: str
    bundle: GalleryBundleRef
    display_name: str                 # 文件主名（现有 UI 显示口径）
    label: Optional[str] = None       # 独立 label（meta schema 2 起可携带）
    provenance: GalleryStillProvenance = field(
        default_factory=GalleryStillProvenance)
    created_at: Optional[str] = None
    content_revision: int = 0         # .topos mtime_ns（G05 索引失效口径）
    still_precision: Optional[str] = None
    working_space: Optional[str] = None
    display_transform: Optional[str] = None
    has_exr: bool = False
    has_still_jpeg: bool = False
    has_thumbnail: bool = False
    has_meta: bool = False
    grade_loadable: Optional[bool] = None   # None = 未探测（查询不默认探测）
    meta: Optional["PresetMeta"] = None     # 扫描时读取的只读 sidecar 快照

    @property
    def has_any_still_image(self) -> bool:
        """是否有可作为参考的图像（EXR／JPEG／缩略图）。"""
        return self.has_exr or self.has_still_jpeg or self.has_thumbnail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "still_id": self.still_id,
            "album_id": self.album_id,
            "bundle": self.bundle.to_dict(),
            "display_name": self.display_name,
            "label": self.label,
            "provenance": self.provenance.to_dict(),
            "created_at": self.created_at,
            "content_revision": self.content_revision,
            "still_precision": self.still_precision,
            "working_space": self.working_space,
            "display_transform": self.display_transform,
            "has_exr": self.has_exr,
            "has_still_jpeg": self.has_still_jpeg,
            "has_thumbnail": self.has_thumbnail,
            "has_meta": self.has_meta,
            "grade_loadable": self.grade_loadable,
        }


@dataclass(frozen=True)
class GalleryQuery:
    """查询请求：稳定次排序用 still_id，避免重排跳项。"""

    album_id: str
    search_text: str = ""
    sort_key: str = SORT_KEY_NAME
    sort_desc: bool = False
    request_revision: int = 0
    limit: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.album_id:
            raise ValueError("album_id must not be empty")
        if self.sort_key not in _SORT_KEYS:
            raise ValueError(f"unknown sort key: {self.sort_key!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "album_id": self.album_id,
            "search_text": self.search_text,
            "sort_key": self.sort_key,
            "sort_desc": self.sort_desc,
            "request_revision": self.request_revision,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class GalleryQueryResult:
    """查询结果：``scan_error`` 携带稳定失败原因（不抛异常冒充空库）。"""

    album: Optional[GalleryAlbum]
    stills: Tuple[GalleryStill, ...]
    request_revision: int
    total_matched: int = 0
    degraded_count: int = 0     # provenance 缺失（无 .meta.json）的条目数
    scan_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "album": self.album.to_dict() if self.album else None,
            "stills": [s.to_dict() for s in self.stills],
            "request_revision": self.request_revision,
            "total_matched": self.total_matched,
            "degraded_count": self.degraded_count,
            "scan_error": self.scan_error,
        }


@dataclass(frozen=True)
class GalleryCaptureRequest:
    """抓帧请求（G06 落地执行端；G01 先冻结口径）。

    ``expected_target_revision`` 是发起时读到的目标 album revision；
    ``cancel_token`` 匹配即视为取消（沿用 color_bake_protocol 口径）。
    """

    request_id: str
    album_id: str
    project_id: Optional[str] = None
    clip_id: Optional[str] = None
    asset_id: Optional[str] = None
    timeline_id: Optional[str] = None
    source_frame: Optional[int] = None
    timeline_frame: Optional[int] = None
    grade_revision: int = 0
    color_context: str = ""
    expected_target_revision: int = 0
    cancel_token: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must not be empty")
        if not self.album_id:
            raise ValueError("album_id must not be empty")

    def cancelled(self, cancel_token: Optional[str]) -> bool:
        return cancel_token is not None and cancel_token == self.cancel_token

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "album_id": self.album_id,
            "project_id": self.project_id,
            "clip_id": self.clip_id,
            "asset_id": self.asset_id,
            "timeline_id": self.timeline_id,
            "source_frame": self.source_frame,
            "timeline_frame": self.timeline_frame,
            "grade_revision": self.grade_revision,
            "color_context": self.color_context,
            "expected_target_revision": self.expected_target_revision,
        }


@dataclass(frozen=True)
class GalleryOperationResult:
    """动作回填：一动作一请求一回填；失败带稳定 reason。"""

    request_id: str
    status: str
    affected_still_ids: Tuple[str, ...] = ()
    album_revision: int = 0
    warnings: Tuple[str, ...] = ()
    reason: Optional[str] = None
    recoverable: bool = False

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must not be empty")
        if self.status not in _OP_STATUSES:
            raise ValueError(f"unknown operation status: {self.status!r}")

    @property
    def succeeded(self) -> bool:
        return self.status == OP_SUCCESS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "affected_still_ids": list(self.affected_still_ids),
            "album_revision": self.album_revision,
            "warnings": list(self.warnings),
            "reason": self.reason,
            "recoverable": self.recoverable,
        }


@dataclass(frozen=True)
class GalleryBundleFile:
    """bundle 内单个文件的角色／存在性／大小（hash 按需计算）。"""

    role: str
    relative_path: str
    required: bool
    exists: bool
    size_bytes: Optional[int] = None
    mtime_ns: Optional[int] = None
    sha256: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "relative_path": self.relative_path,
            "required": self.required,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class GalleryBundleManifest:
    """bundle 清单：角色覆盖全部六个文件（含 .still.exr.json）。"""

    schema_version: int
    still_id: Optional[str]
    files: Tuple[GalleryBundleFile, ...]
    still_precision: Optional[str] = None
    alpha_semantics: Optional[str] = None

    @property
    def complete(self) -> bool:
        return not self.missing_required

    @property
    def missing_required(self) -> Tuple[str, ...]:
        return tuple(
            f.role for f in self.files
            if f.required and not f.exists)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "still_id": self.still_id,
            "files": [f.to_dict() for f in self.files],
            "still_precision": self.still_precision,
            "alpha_semantics": self.alpha_semantics,
            "complete": self.complete,
        }


# ── 能力注册表（G01 门槛：无素材 Capture 禁用；仅 JPEG 且 grade 不可
#    加载时可 reference 不可 Apply；未实现服务如实 False） ────────────────

@dataclass(frozen=True)
class GalleryCapability:
    """单个 Gallery 动作能力：unsupported 必带稳定 reason。"""

    action: str
    supported: bool
    reason: Optional[str] = None
    checked: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "supported": self.supported,
            "reason": self.reason,
            "checked": self.checked,
        }


@dataclass(frozen=True)
class GalleryCapabilityContext:
    """评估输入：只含可序列化快照 ＋ 已落地服务位（如实 False）。"""

    has_media: bool = False
    media_is_video: bool = False
    engine_available: bool = False
    album: Optional[GalleryAlbum] = None
    selection: Optional[GalleryStill] = None
    selection_count: int = 0
    # grade 可加载性由宿主对**当前选中项**单独探测后传入（查询路径不
    # 默认探测大库）；None 视为未证实 → 按不可加载处理。
    selection_grade_loadable: Optional[bool] = None
    timeline_target_available: bool = False   # G07 权威目标接入后使用
    import_service_available: bool = False    # G08
    label_service_available: bool = False     # G02
    split_service_available: bool = False     # G13
    collection_service_available: bool = False  # G04


_REASONS = {
    "no_media": "No media is open to capture from or apply onto.",
    "no_engine": "The rendering engine is not available yet.",
    "album_missing": "The current album could not be resolved.",
    "album_readonly": "The current album is read-only.",
    "no_selection": "Select a still first.",
    "grade_unloadable": "The grade package (.topos) is missing or not a "
                        "loadable project bundle.",
    "no_still_image": "This still has no reference image (EXR/JPEG).",
    "no_thumbnail": "This still has no thumbnail to export.",
    "import_service_missing": "Bundle import is not implemented yet.",
    "label_service_missing": "Independent labels are not implemented yet.",
    "split_service_missing": "The split-screen compare presenter is not "
                             "wired yet; wipe stays available.",
    "collection_service_missing": "PowerGrade/Timelines collections are "
                                  "not backed by a storage service yet.",
    "memories_unavailable": "Memories has no model or storage in this "
                            "version.",
}


def _cap(action: str, supported: bool, reason_key: Optional[str] = None,
         checked: Optional[bool] = None) -> GalleryCapability:
    return GalleryCapability(
        action=action, supported=supported,
        reason=None if supported else _REASONS.get(reason_key, reason_key),
        checked=checked)


def evaluate_gallery_capabilities(
        context: GalleryCapabilityContext
) -> Tuple[GalleryCapability, ...]:
    """单一事实源：Gallery 面板动作的 enabled／reason。"""
    media_ready = context.has_media and context.engine_available
    album = context.album
    album_writable = album is not None and album.writable
    album_reason = ("album_missing" if album is None
                    else "album_readonly" if album.read_only else None)

    selection = context.selection
    has_selection = selection is not None
    selection_image = has_selection and selection.has_any_still_image
    grade_ok = context.selection_grade_loadable is True

    caps: list[GalleryCapability] = []

    # ── 抓帧／建目录（需要媒体＋引擎＋可写 album） ──────────────────────
    caps.append(_cap("capture.still",
                     media_ready and album_writable,
                     "no_media" if not context.has_media
                     else "no_engine" if not context.engine_available
                     else album_reason))
    caps.append(_cap("album.create", album_writable, album_reason))

    # ── 应用调色（需要目标媒体＋选中项＋ grade 可加载） ──────────────────
    apply_reason = ("no_selection" if not has_selection
                    else "no_media" if not context.has_media
                    else "grade_unloadable")
    caps.append(_cap("apply.grade",
                     has_selection and context.has_media and grade_ok,
                     apply_reason))
    caps.append(_cap("apply.categories",
                     has_selection and context.has_media and grade_ok,
                     apply_reason))

    # ── 参考／Match（不需要 grade 可加载，但需要参考图像） ───────────────
    reference_reason = ("no_selection" if not has_selection
                        else "no_media" if not context.has_media
                        else "no_still_image")
    reference_ok = has_selection and context.has_media and selection_image
    caps.append(_cap("reference.load", reference_ok, reference_reason))
    caps.append(_cap("compare.wipe", reference_ok, reference_reason))
    caps.append(_cap("match.color", reference_ok, reference_reason))

    # ── 条目文件操作（选中＋可写 album） ────────────────────────────────
    bundle_reason = "no_selection" if not has_selection else album_reason
    caps.append(_cap("rename.bundle",
                     has_selection and album_writable, bundle_reason))
    caps.append(_cap("delete.bundle",
                     has_selection and album_writable, bundle_reason))
    caps.append(_cap("export.thumbnail",
                     has_selection and bool(selection and selection.has_thumbnail),
                     "no_selection" if not has_selection else "no_thumbnail"))

    # ── 未实现服务（如实 False ＋ 稳定原因） ─────────────────────────────
    caps.append(_cap("import.bundle", context.import_service_available,
                     "import_service_missing"))
    caps.append(_cap("label.rename", context.label_service_available,
                     "label_service_missing"))
    caps.append(_cap("compare.split", context.split_service_available,
                     "split_service_missing"))
    caps.append(_cap("album.powergrade", context.collection_service_available,
                     "collection_service_missing"))
    caps.append(_cap("album.timelines", context.collection_service_available,
                     "collection_service_missing"))
    caps.append(_cap("album.memories", False, "memories_unavailable"))

    return tuple(caps)


def gallery_capability_by_action(
        caps: Tuple[GalleryCapability, ...]
) -> Dict[str, GalleryCapability]:
    return {cap.action: cap for cap in caps}
