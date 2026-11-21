"""Files 契约（B01）：transport 无关的 DTO、单位与错误语义。

设计约束
--------
* 迁移期数据权威仍是既有 Files 服务；本模块 **只做约定、解析与校验**，
  不读磁盘、不碰 SQL、不发请求。
* 原生 UI / 导入协调 / 预览会话共用一个解析入口，界面层不再自行猜字段
  或猜单位（例如 ``duration`` 秒数 vs ``mm:ss`` 字符串、``cached_info``
  自由文本 vs 结构化分辨率）。
* 旧字段一律保持兼容：未知字段忽略，缺失字段为 ``None``/``unknown``，
  绝不伪造"看起来成功"的值（例如假时长、假分辨率）。

字段与单位（与计划 §6.1 对应）
------------------------------
==================  ==================================================
对象                 必要字段
==================  ==================================================
Location            kind / key / display_name / path / writable / online / locked
Entry               identity / path / name / kind / is_dir / size_bytes /
                    mtime_ts / availability / thumb_version / technical / user
TechnicalMetadata   duration_seconds（秒，float）/ fps（可分数）/ width/height /
                    codec / probe_status
UserMetadata        rating(0-5) / tags / comments / color(0-5) / flag(0-2) / revision
QueryResult         request_id / generation / path / entries / total /
                    truncated / revision
ThumbnailRef        identity / source_version / size / status
Capability          key / available / reason / version
==================  ==================================================
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

class FilesErrorCode:
    """稳定错误码：UI 据此决定禁用操作、提示文案与是否可重试。"""

    LOCKED = "locked"                    # 目录软锁（既有策略，非加密）
    OFFLINE = "offline"                  # 设备/卷离线
    NOT_FOUND = "not_found"              # 路径或条目不存在
    PERMISSION = "permission"            # 无权限
    UNSUPPORTED = "unsupported"          # 格式/能力不支持
    CONFLICT = "conflict"                # 目标已存在等冲突
    CANCELLED = "cancelled"              # 用户取消 / 过期作废
    INVALID_PAYLOAD = "invalid_payload"  # 服务返回结构无效
    SERVICE_UNAVAILABLE = "service_unavailable"
    UNKNOWN = "unknown"

    ALL = (LOCKED, OFFLINE, NOT_FOUND, PERMISSION, UNSUPPORTED, CONFLICT,
           CANCELLED, INVALID_PAYLOAD, SERVICE_UNAVAILABLE, UNKNOWN)


class LocationKind:
    """真实目录与虚拟位置分开；虚拟位置不能作为 mkdir / paste 目标。"""

    DIRECTORY = "directory"
    SEARCH = "search"
    COLLECTION = "collection"
    TRASH = "trash"
    DRIVE = "drive"

    VIRTUAL = (SEARCH, COLLECTION, TRASH)

    ALL = (DIRECTORY, SEARCH, COLLECTION, TRASH, DRIVE,)


class ProbeStatus:
    """技术元数据可信度；``PENDING`` 表示服务正在探测，不得当作已知时长。"""

    KNOWN = "known"
    PENDING = "pending"
    UNKNOWN = "unknown"
    FAILED = "failed"

    ALL = (KNOWN, PENDING, UNKNOWN, FAILED)


class Availability:
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"

    ALL = (ONLINE, OFFLINE, UNKNOWN)


ENTRY_KINDS = ("dir", "image", "video", "audio", "doc", "other")

DEFAULT_TAG_SEPARATOR = ","


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FilesError(Exception):
    """一个可诊断、可分类的错误；不吞掉细节，也不冒充成功。"""

    code: str
    message: str
    retriable: bool = False
    target: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retriable": self.retriable,
            "target": self.target,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FilesError":
        code = str(payload.get("code") or FilesErrorCode.UNKNOWN)
        if code not in FilesErrorCode.ALL:
            code = FilesErrorCode.UNKNOWN
        return cls(
            code=code,
            message=str(payload.get("message") or ""),
            retriable=bool(payload.get("retriable", False)),
            target=payload.get("target"),
            details=dict(payload.get("details") or {}),
        )


def locked_error(path: str, hint: str = "") -> FilesError:
    return FilesError(FilesErrorCode.LOCKED, hint or "目录已锁定", False, path)


def offline_error(target: str, message: str = "设备或卷当前不可用") -> FilesError:
    return FilesError(FilesErrorCode.OFFLINE, message, True, target)


def not_found_error(target: str, message: str = "路径不存在") -> FilesError:
    return FilesError(FilesErrorCode.NOT_FOUND, message, False, target)


def permission_error(target: str, message: str = "没有访问权限") -> FilesError:
    return FilesError(FilesErrorCode.PERMISSION, message, False, target)


def unsupported_error(target: str, message: str = "不支持的格式或能力") -> FilesError:
    return FilesError(FilesErrorCode.UNSUPPORTED, message, False, target)


def conflict_error(target: str, message: str = "目标已存在") -> FilesError:
    return FilesError(FilesErrorCode.CONFLICT, message, False, target)


def cancelled_error(target: str | None = None, message: str = "操作已取消") -> FilesError:
    return FilesError(FilesErrorCode.CANCELLED, message, True, target)


def invalid_payload_error(message: str, target: str | None = None) -> FilesError:
    return FilesError(FilesErrorCode.INVALID_PAYLOAD, message, False, target)


def files_error_for_probe(target: str, exc: BaseException) -> FilesError:
    """探测失败：保留原始异常文本，标为可重试（磁盘/解码可能只是暂时问题）。"""
    message = str(exc) or exc.__class__.__name__
    return FilesError(FilesErrorCode.UNSUPPORTED, f"无法读取媒体信息：{message}", True, target)


def service_unavailable_error(message: str, target: str | None = None) -> FilesError:
    return FilesError(FilesErrorCode.SERVICE_UNAVAILABLE, message, True, target)


# ---------------------------------------------------------------------------
# 路径身份
# ---------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """NFC + 正斜杠 + 去尾斜杠；不解析符号链接、不改大小写。"""
    if not path:
        return ""
    text = unicodedata.normalize("NFC", str(path).replace("\\", "/"))
    while len(text) > 1 and text.endswith("/"):
        if len(text) == 3 and text[0].isascii() and text[0].isalpha() and text[1:] == ":/":
            break  # A Windows drive root must remain absolute.
        text = text[:-1]
    return text


def identity_key(path: str) -> str:
    """稳定比较键：用于去重、选择保持与缓存命中。

    macOS/Windows 默认大小写不敏感，因此比较键 casefold；**展示与传输**
    一律使用原始 ``path``，避免用户看到被改写的路径。
    """
    return normalize_path(path).casefold()


def display_name_for(path: str) -> str:
    text = normalize_path(path)
    if not text:
        return ""
    return text.rsplit("/", 1)[-1] or text


# ---------------------------------------------------------------------------
# 数值 / 文本解析（宽容输入，明确输出）
# ---------------------------------------------------------------------------

def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    try:
        return int(number)
    except (ValueError, OverflowError):
        return None


def parse_fps(value: Any) -> float | None:
    """接受 ``25``、``"25"``、``"30000/1001"``、``"29.97"``；无法解析返回 None。"""
    if isinstance(value, str) and "/" in value:
        numerator, _, denominator = value.partition("/")
        left, right = _as_float(numerator), _as_float(denominator)
        if left is None or right is None or right == 0:
            return None
        result = left / right
        return result if result > 0 else None
    number = _as_float(value)
    return number if number and number > 0 else None


def format_duration(seconds: float | None) -> str:
    """秒 → ``m:ss`` / ``h:mm:ss``；未知返回 ``—``，绝不补 0 假时长。"""
    if seconds is None or seconds <= 0:
        return "—"
    total = int(seconds)  # 时长按整秒截断：不向上取整制造"多一秒"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_bytes(size: int | None) -> str:
    if size is None or size < 0:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def format_resolution(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    return f"{width}×{height}"


# ---------------------------------------------------------------------------
# 技术 / 用户元数据
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TechnicalMetadata:
    """``probe_status != KNOWN`` 时字段可为 None；UI 必须显示"未知"。"""

    duration_seconds: float | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    probe_status: str = ProbeStatus.UNKNOWN

    @property
    def resolution(self) -> str | None:
        return format_resolution(self.width, self.height)

    @property
    def duration_label(self) -> str:
        return format_duration(self.duration_seconds)

    @property
    def fps_label(self) -> str:
        if self.fps is None:
            return "—"
        return f"{self.fps:.3g} fps"

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": self.duration_seconds,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "codec": self.codec,
            "probe_status": self.probe_status,
        }

    @classmethod
    def from_legacy(cls, payload: dict[str, Any]) -> "TechnicalMetadata":
        cached = str(payload.get("res") or payload.get("cached_info") or "")
        width = _as_int(payload.get("width"))
        height = _as_int(payload.get("height"))
        fps = parse_fps(payload.get("fps"))
        codec = payload.get("codec") or None
        if (width is None or height is None or fps is None or codec is None) and cached:
            cached_width, cached_height, cached_fps, cached_codec = _parse_cached_info(cached)
            width = width or cached_width
            height = height or cached_height
            fps = fps or cached_fps
            codec = codec or cached_codec
        duration = _as_float(payload.get("duration"))
        known = any(value is not None for value in (duration, width, height, codec, fps))
        status = ProbeStatus.KNOWN if known else ProbeStatus.UNKNOWN
        declared = payload.get("probe_status")
        if declared in ProbeStatus.ALL:
            status = str(declared)
        return cls(duration_seconds=duration, fps=fps, width=width, height=height,
                   codec=str(codec) if codec else None, probe_status=status)


_CACHED_RESOLUTION = "resolution"
_CACHED_FPS = "fps"


def _parse_cached_info(text: str) -> tuple[int | None, int | None, float | None, str | None]:
    """解析旧 ``cached_info``：``1920x1080 · ProRes 422 · 25.00fps · 120 Mbps``。

    这是兼容路径：新字段缺失时才回退，不把自由文本当作权威结构。
    """
    width = height = None
    fps: float | None = None
    codec: str | None = None
    for index, part in enumerate(text.replace("·", "|").split("|")):
        piece = part.strip()
        if not piece:
            continue
        lowered = piece.casefold()
        if "x" in lowered and index == 0:
            left, _, right = lowered.partition("x")
            width, height = _as_int(left.strip()), _as_int(right.strip())
        elif lowered.endswith("fps"):
            fps = parse_fps(piece[:-3].strip())
        elif codec is None and index >= 1 and not lowered.endswith("mbps"):
            codec = piece
    return width, height, fps, codec


@dataclass(frozen=True)
class UserMetadata:
    rating: int = 0
    tags: tuple[str, ...] = ()
    comments: str = ""
    color: int = 0
    flag: int = 0
    revision: int = 0

    @property
    def tag_text(self) -> str:
        return DEFAULT_TAG_SEPARATOR.join(self.tags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rating": self.rating,
            "tags": list(self.tags),
            "comments": self.comments,
            "color": self.color,
            "flag": self.flag,
            "revision": self.revision,
        }

    @classmethod
    def from_legacy(cls, payload: dict[str, Any]) -> "UserMetadata":
        return cls(
            rating=_clamp(_as_int(payload.get("rating")) or 0, 0, 5),
            tags=split_tags(payload.get("tags")),
            comments=str(payload.get("comments") or ""),
            color=_clamp(_as_int(payload.get("color")) or 0, 0, 5),
            flag=_clamp(_as_int(payload.get("flag")) or 0, 0, 2),
            revision=_as_int(payload.get("meta_revision")) or 0,
        )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def split_tags(value: Any) -> tuple[str, ...]:
    """标签以逗号分隔；去空、去重（保序），不静默吞掉用户可见文本。"""
    if not value:
        return ()
    if isinstance(value, (list, tuple, set)):
        raw = [str(item) for item in value]
    else:
        raw = str(value).split(DEFAULT_TAG_SEPARATOR)
    seen: dict[str, str] = {}
    for item in raw:
        text = item.strip()
        if text and text.casefold() not in seen:
            seen[text.casefold()] = text
    return tuple(seen.values())


# ---------------------------------------------------------------------------
# 位置
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Location:
    kind: str
    key: str
    display_name: str
    path: str | None = None
    writable: bool = True
    online: bool = True
    locked: bool = False
    hint: str = ""

    @property
    def is_virtual(self) -> bool:
        return self.kind in LocationKind.VIRTUAL

    def can_accept_files(self) -> bool:
        """虚拟位置、离线位置与软锁目录不能作为写入 / 粘贴目标。"""
        return not self.is_virtual and self.writable and self.online and not self.locked

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "display_name": self.display_name,
            "path": self.path,
            "writable": self.writable,
            "online": self.online,
            "locked": self.locked,
            "hint": self.hint,
        }

    @classmethod
    def directory(cls, path: str, *, display_name: str | None = None,
                  writable: bool = True, online: bool = True,
                  locked: bool = False) -> "Location":
        normalized = normalize_path(path)
        return cls(LocationKind.DIRECTORY, identity_key(normalized),
                   display_name or display_name_for(normalized), normalized,
                   writable, online, locked)


def parse_location(payload: dict[str, Any], *, kind: str = LocationKind.DIRECTORY) -> Location:
    path = payload.get("path")
    normalized = normalize_path(str(path)) if path else None
    return Location(
        kind=str(payload.get("kind") or kind),
        key=str(payload.get("key") or (identity_key(normalized) if normalized else "")),
        display_name=str(payload.get("name") or payload.get("display_name")
                         or (display_name_for(normalized) if normalized else "")),
        path=normalized,
        writable=bool(payload.get("writable", True)),
        online=bool(payload.get("online", True)),
        locked=bool(payload.get("locked", False)),
        hint=str(payload.get("hint") or ""),
    )


# ---------------------------------------------------------------------------
# 条目
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    """浏览快照；**不是**工程 asset ID，也不替代磁盘真实状态。"""

    path: str
    name: str
    kind: str
    is_dir: bool = False
    size_bytes: int | None = None
    mtime_ts: float | None = None
    mtime_label: str = ""
    availability: str = Availability.UNKNOWN
    thumb_version: int = 0
    subdir: str = ""
    technical: TechnicalMetadata = field(default_factory=TechnicalMetadata)
    user: UserMetadata = field(default_factory=UserMetadata)

    @property
    def identity(self) -> str:
        return identity_key(self.path)

    @property
    def size_label(self) -> str:
        return "—" if self.is_dir else format_bytes(self.size_bytes)

    @property
    def has_known_media(self) -> bool:
        return self.kind in ("image", "video", "audio") and not self.is_dir

    def with_user(self, user: UserMetadata) -> "Entry":
        return replace(self, user=user)

    def to_legacy_dict(self) -> dict[str, Any]:
        """回写旧字段形状，供仍在使用 dict 的调用方（测试 / 兼容宿主）读取。"""
        return {
            "name": self.name,
            "path": self.path,
            "type": self.kind,
            "size": self.size_label,
            "mtime": self.mtime_label,
            "size_bytes": self.size_bytes,
            "mtime_ts": self.mtime_ts,
            "t_ver": self.thumb_version,
            "res": self.technical.resolution,
            "duration": self.technical.duration_seconds,
            "width": self.technical.width,
            "height": self.technical.height,
            "codec": self.technical.codec,
            "fps": self.technical.fps,
            "tags": self.user.tag_text,
            "comments": self.user.comments,
            "rating": self.user.rating,
            "color": self.user.color,
            "flag": self.user.flag,
            "subdir": self.subdir,
            "availability": self.availability,
        }


def parse_entry(payload: Any) -> Entry:
    """把服务条目（或已构造的 Entry）规范化为 DTO。

    Raises:
        FilesError: 缺少 ``path`` / ``name`` / ``type`` 时 ``invalid_payload``。
    """
    if isinstance(payload, Entry):
        return payload
    if not isinstance(payload, dict):
        raise invalid_payload_error("文件条目不是对象")
    raw_path = payload.get("path")
    raw_name = payload.get("name")
    if not isinstance(raw_path, str) or not raw_path:
        raise invalid_payload_error("文件条目缺少 path", target=str(raw_path or ""))
    if not isinstance(raw_name, str) or not raw_name:
        raise invalid_payload_error(f"文件条目缺少 name: {raw_path}", target=raw_path)
    kind = payload.get("type")
    if not isinstance(kind, str) or not kind:
        raise invalid_payload_error(f"文件条目缺少 type: {raw_path}", target=raw_path)
    if kind not in ENTRY_KINDS:
        kind = "other"
    normalized = normalize_path(raw_path)
    availability = str(payload.get("availability") or Availability.UNKNOWN)
    if payload.get("missing") in (1, True, "1"):
        availability = Availability.OFFLINE
    elif availability not in Availability.ALL:
        availability = Availability.UNKNOWN
    mtime_ts = _as_float(payload.get("mtime_ts"))
    mtime_label = str(payload.get("mtime") or "")
    if not mtime_label and mtime_ts:
        import datetime as _dt
        mtime_label = _dt.datetime.fromtimestamp(mtime_ts).strftime("%Y-%m-%d")
    return Entry(
        path=normalized,
        name=unicodedata.normalize("NFC", raw_name),
        kind=kind,
        is_dir=(kind == "dir"),
        size_bytes=_as_int(payload.get("size_bytes")),
        mtime_ts=mtime_ts,
        mtime_label=mtime_label,
        availability=availability,
        thumb_version=_as_int(payload.get("t_ver")) or 0,
        subdir=str(payload.get("subdir") or ""),
        technical=TechnicalMetadata.from_legacy(payload),
        user=UserMetadata.from_legacy(payload),
    )


def parse_entries(payloads: Iterable[Any]) -> list[Entry]:
    return [parse_entry(payload) for payload in payloads]


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueryRequest:
    """一次查询身份；晚到响应据此丢弃，回填不触发二次请求。"""

    request_id: str
    path: str
    generation: int = 0
    recursive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_path(self.path))


@dataclass(frozen=True)
class QueryResult:
    request_id: str
    path: str
    entries: tuple[Entry, ...]
    generation: int = 0
    total: int = 0
    truncated: bool = False
    revision: int = 0
    locked: bool = False

    @property
    def count(self) -> int:
        return len(self.entries)

    def first_page_paths(self) -> list[str]:
        return [entry.path for entry in self.entries]


def parse_query_result(
    payload: Any, request: QueryRequest, *, revision: int = 0,
) -> QueryResult:
    """解析既有 ``/api/list`` 响应；错误与软锁转成 ``FilesError``。

    Raises:
        FilesError: 锁目录 / 路径不存在 / 结构无效。
    """
    if not isinstance(payload, dict):
        raise invalid_payload_error("素材服务返回的目录格式无效", request.path)
    if payload.get("locked"):
        raise locked_error(request.path, str(payload.get("hint") or ""))
    if payload.get("error"):
        message = str(payload.get("error") or "")
        lowered = message.casefold()
        if "not found" in lowered or "不存在" in message:
            raise not_found_error(request.path, message)
        raise FilesError(FilesErrorCode.UNKNOWN, message, True, request.path)
    raw_entries = payload.get("files", payload.get("entries"))
    if not isinstance(raw_entries, list):
        raise invalid_payload_error("素材服务返回的条目列表无效", request.path)
    entries = tuple(parse_entries(raw_entries))
    total = _as_int(payload.get("total"))
    declared_revision = _as_int(payload.get("revision"))
    return QueryResult(
        request_id=request.request_id,
        path=normalize_path(request.path),
        entries=entries,
        generation=request.generation,
        total=total if total is not None else len(entries),
        truncated=bool(payload.get("truncated", False)),
        revision=revision if declared_revision is None else declared_revision,
        locked=False,
    )


# ---------------------------------------------------------------------------
# 缩略图 / 能力 / 任务结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThumbnailRef:
    identity: str
    source_version: int
    size: str = "small"
    status: str = "ok"          # ok / pending / missing / failed
    cache_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "source_version": self.source_version,
            "size": self.size,
            "status": self.status,
            "cache_ref": self.cache_ref,
        }


@dataclass(frozen=True)
class Capability:
    key: str
    available: bool
    reason: str = ""
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "available": self.available,
                "reason": self.reason, "version": self.version}


@dataclass(frozen=True)
class JobProgress:
    job_id: str
    stage: str
    completed: int = 0
    total: int = 0
    cancelled: bool = False

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.completed / self.total)


@dataclass(frozen=True)
class JobFailure:
    target: str
    error: FilesError

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target, "error": self.error.to_dict()}


@dataclass(frozen=True)
class ImportFailureCandidate:
    """导入阶段（探测之后）才失败的条目：统一成可读诊断，不混入"没有文件"。"""

    path: str
    exception: BaseException

    @property
    def error(self) -> FilesError:
        return files_error_for_probe(self.path, self.exception)


@dataclass(frozen=True)
class JobResult:
    """部分成功单独表达：成功项、失败项、取消原因互不覆盖。"""

    job_id: str
    successes: tuple[str, ...] = ()
    failures: tuple[JobFailure, ...] = ()
    cancelled: bool = False
    changed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures and not self.cancelled

    @property
    def completed(self) -> int:
        return len(self.successes)

    def summary(self) -> str:
        if self.cancelled and not self.successes:
            return "已取消"
        parts = [f"成功 {len(self.successes)}"]
        if self.failures:
            parts.append(f"失败 {len(self.failures)}")
        if self.cancelled:
            parts.append("已取消")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "successes": list(self.successes),
            "failures": [failure.to_dict() for failure in self.failures],
            "cancelled": self.cancelled,
            "changed": list(self.changed),
        }
