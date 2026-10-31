"""DESK-01B/01C · 工程内嵌资源解析：资源索引 + 逻辑 URI + 内容身份校验。

问题
----
手机生成的自包含 ``.topos`` 把原片/字体/LUT/贴图塞进包内 ``resources/``
目录。桌面加载包后解包到 ``.topos_cache/<包名>/``，但素材表里的路径仍
指向手机上的外部路径——换机即离线，解码器不知道去包里找。

设计（契约冻结于 docs/adr/0005 §3）
-----------------------------------
1. **资源索引** ``resources/resources.json``（schema v1）：每个资源按
   SHA-256 内容寻址登记（resource_id / kind / archive_path / size_bytes /
   sha256 / roles / 可选 asset_id）。
2. **逻辑 URI** ``topos-res://<resource_id>`` / ``topos-res://sha256-<hex>``：
   引用方（素材表、字幕样式、调色栈 LUT 字段）持 URI，不持本机路径。
3. **解析** ``ProjectResourceResolver``：URI → 解包目录内 seekable 本地
   文件（路径穿越拒绝），首次解析时流式 SHA-256 校验（**按内容身份**，
   不按文件名——同名不同内容绝不串包；同内容天然去重）。校验失败按
   ``ResourceResolutionError`` 受控失败，不把错内容喂给解码器。
4. **缓存非权威**：resolver 返回的本地路径是加载机器的实现细节，
   保存时不得写回包（DESK-03C；``topos_project_io`` 的 relativize 侧
   有配套守卫）。

大包纪律（01C）：校验/入库全部流式（1 MiB 块），10 GB 包不整体进
内存；解包本身走既有 ``_extract_archive_to_cache`` 的流式拷贝与上限
防护。
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator

from src.shared.project.package_spec import (
    RESOURCE_INDEX_SCHEMA_VERSION,
    is_valid_resource_uri,
)

__all__ = [
    "ResourceEntry",
    "ResourceIndex",
    "ResourceResolutionError",
    "ResolvedResource",
    "ProjectResourceResolver",
    "make_resource_entry",
    "sha256_file",
    "RESOURCE_URI_SCHEME",
]

#: 索引文件在包内的归档路径（常量真值在 payload_cache，避免双处漂移）。
from .payload_cache import RESOURCE_INDEX_ARCHIVE_PATH  # noqa: E402,F401 — 转口 re-export（topos_project_io 经本模块导入）

#: 资源二进制存放的包内前缀（archive_path 必须位于其下）。
RESOURCE_FILES_PREFIX = "resources/"

#: 流式哈希/拷贝块大小。
_CHUNK_BYTES = 1024 * 1024

RESOURCE_URI_SCHEME = "topos-res"


class ResourceResolutionError(Exception):
    """资源解析失败（URI 未知/文件缺失/哈希不符/路径越界）。"""


def sha256_file(path: str) -> str:
    """流式计算文件 SHA-256（hex，小写）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_package_cache_path(path: str) -> bool:
    """DESK-03C：路径是否位于 ``.topos_cache`` 解包缓存内。

    缓存路径是加载机器的实现细节——绝不能作为权威路径写回工程包
    （换机即断链）。判定按路径段精确匹配（分隔符归一）。
    """
    if not path:
        return False
    segments = path.replace("\\", "/").rstrip("/").split("/")
    return ".topos_cache" in segments


# ================================================================
# 索引数据模型
# ================================================================

@dataclass(frozen=True)
class ResourceEntry:
    """一个内嵌资源的索引条目（契约形状见 package_spec 资源 schema）。"""

    resource_id: str
    kind: str                     # video/audio/image/font/lut/texture/mask/generated/other
    archive_path: str             # 包内路径，必须位于 resources/ 前缀下
    size_bytes: int
    sha256: str                   # 64 位小写 hex
    original_filename: str = ""
    mime: str = ""
    roles: tuple[str, ...] = ()
    asset_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def uri(self) -> str:
        return f"{RESOURCE_URI_SCHEME}://{self.resource_id}"

    @property
    def content_uri(self) -> str:
        return f"{RESOURCE_URI_SCHEME}://sha256-{self.sha256}"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "archive_path": self.archive_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }
        if self.original_filename:
            data["original_filename"] = self.original_filename
        if self.mime:
            data["mime"] = self.mime
        if self.roles:
            data["roles"] = list(self.roles)
        if self.asset_id is not None:
            data["asset_id"] = self.asset_id
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ResourceEntry:
        return ResourceEntry(
            resource_id=str(data.get("resource_id", "")),
            kind=str(data.get("kind", "other")),
            archive_path=str(data.get("archive_path", "")),
            size_bytes=int(data.get("size_bytes", 0)),
            sha256=str(data.get("sha256", "")).lower(),
            original_filename=str(data.get("original_filename", "")),
            mime=str(data.get("mime", "")),
            roles=tuple(data.get("roles", ()) or ()),
            asset_id=data.get("asset_id"),
            metadata=dict(data.get("metadata", {}) or {}),
        )


@dataclass(frozen=True)
class ResourceIndex:
    """包内资源索引（resources/resources.json 的内存形态）。"""

    schema_version: int = RESOURCE_INDEX_SCHEMA_VERSION
    entries: dict[str, ResourceEntry] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "resources": [self.entries[rid].to_dict()
                          for rid in sorted(self.entries)],
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> ResourceIndex:
        """解析索引；容错策略：

        - 无索引 / 非 dict → 空索引（旧包合法形态）；
        - 单条损坏（缺键/畸形）→ 跳过并留给调用方日志，不废整包；
        - 未来 schema 版本 → 按已知字段尽力读取（future 分级语义）。
        """
        if not isinstance(data, dict):
            return ResourceIndex()
        version = data.get("schema_version", RESOURCE_INDEX_SCHEMA_VERSION)
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = RESOURCE_INDEX_SCHEMA_VERSION
        index = ResourceIndex(schema_version=version)
        raw_entries = data.get("resources", [])
        if isinstance(raw_entries, list):
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                if not raw.get("resource_id") or not raw.get("archive_path"):
                    continue
                try:
                    entry = ResourceEntry.from_dict(raw)
                except (TypeError, ValueError):
                    continue
                index.entries[entry.resource_id] = entry
        return index

    def __len__(self) -> int:
        return len(self.entries)

    def by_uri(self, uri: str) -> ResourceEntry | None:
        """按逻辑 URI 查找（resource_id 或内容别名两种形式）。"""
        if not isinstance(uri, str) or not uri.startswith(
                f"{RESOURCE_URI_SCHEME}://"):
            return None
        body = uri[len(RESOURCE_URI_SCHEME) + 3:]
        if body.startswith("sha256-"):
            want = body[len("sha256-"):].lower()
            for entry in self.entries.values():
                if entry.sha256 == want:
                    return entry
            return None
        return self.entries.get(body)

    def by_sha256(self, sha256_hex: str) -> ResourceEntry | None:
        want = sha256_hex.lower()
        for entry in self.entries.values():
            if entry.sha256 == want:
                return entry
        return None

    def for_asset(self, asset_id: str, role: str | None = None) -> ResourceEntry | None:
        """按素材 ID（可选角色过滤）查找其内嵌源资源。"""
        candidates = [e for e in self.entries.values() if e.asset_id == asset_id]
        if role is not None:
            candidates = [e for e in candidates if role in e.roles]
        if not candidates:
            return None
        # 多条命中时优先显式 source_media 角色，再按 resource_id 稳定排序
        candidates.sort(key=lambda e: (
            0 if "source_media" in e.roles else 1, e.resource_id))
        return candidates[0]

    def entries_for_role(self, role: str) -> list[ResourceEntry]:
        return sorted(
            (e for e in self.entries.values() if role in e.roles),
            key=lambda e: e.resource_id)


def make_resource_entry(
    source_file: str,
    *,
    resource_id: str,
    kind: str,
    roles: tuple[str, ...] = (),
    asset_id: str | None = None,
    original_filename: str | None = None,
    mime: str = "",
    metadata: dict[str, Any] | None = None,
) -> ResourceEntry:
    """从本机文件构建索引条目（流式 SHA-256，内容寻址归档路径）。

    archive_path = ``resources/<kind>/<sha256><原扩展名>``——同名不同内容
    的文件散列不同、天然分路径；同内容跨工程去重。
    """
    sha = sha256_file(source_file)
    size = os.path.getsize(source_file)
    ext = os.path.splitext(original_filename or source_file)[1].lower()
    return ResourceEntry(
        resource_id=resource_id,
        kind=kind,
        archive_path=f"{RESOURCE_FILES_PREFIX}{kind}/{sha}{ext}",
        size_bytes=size,
        sha256=sha,
        original_filename=original_filename or os.path.basename(source_file),
        mime=mime,
        roles=roles,
        asset_id=asset_id,
        metadata=metadata or {},
    )


# ================================================================
# 解析器
# ================================================================

@dataclass(frozen=True)
class ResolvedResource:
    """一次成功的解析结果。

    local_path 是解包缓存里的 seekable 文件——仅供解码器/加载器使用，
    **不得**写回工程包或作为权威引用（DESK-03C）。
    """

    entry: ResourceEntry
    local_path: str
    verified: bool


class ProjectResourceResolver:
    """把逻辑资源 URI 解析为本机 seekable 文件，按内容身份校验。

    Args:
        extracted_root: 包解包目录（其下含 ``resources/``）。
        index: 资源索引。
        verify: 首次解析时流式校验 SHA-256 与 size（默认开）。关闭仅限
            内部受信场景（如刚写完立即读回），生产加载保持开启。
    """

    def __init__(
        self,
        extracted_root: str,
        index: ResourceIndex,
        *,
        verify: bool = True,
    ) -> None:
        self._root = os.path.abspath(extracted_root)
        self._index = index
        self._verify = verify
        # (sha256, size, path, mtime) -> ok：会话内 memo，避免重复哈希大文件
        self._verified: dict[tuple[str, int, str, float], bool] = {}
        self._failures: list[str] = []

    @property
    def index(self) -> ResourceIndex:
        return self._index

    @property
    def extracted_root(self) -> str:
        return self._root

    def diagnostics(self) -> tuple[str, ...]:
        """解析期累积的问题（缺失/哈希不符），供预检与 UI 诊断展示。"""
        return tuple(self._failures)

    def local_path_for(self, entry: ResourceEntry) -> str:
        """条目 → 解包目录内本地路径；路径越界/逃逸直接拒绝。"""
        if not entry.archive_path.startswith(RESOURCE_FILES_PREFIX):
            raise ResourceResolutionError(
                f"资源条目 {entry.resource_id} 的 archive_path 不在 "
                f"{RESOURCE_FILES_PREFIX} 前缀下: {entry.archive_path!r}")
        candidate = os.path.abspath(
            os.path.join(self._root, entry.archive_path))
        # 双重边界：必须在解包根内，且必须在 resources/ 子树内
        # （"resources/video/../../x" 会折回解包根——同属逃逸）。
        resources_root = os.path.abspath(
            os.path.join(self._root, RESOURCE_FILES_PREFIX))
        if os.path.commonpath((resources_root, candidate)) != resources_root:
            raise ResourceResolutionError(
                f"资源条目 {entry.resource_id} 的 archive_path 越界: "
                f"{entry.archive_path!r}")
        return candidate

    def _resolve_entry(self, entry: ResourceEntry) -> ResolvedResource:
        path = self.local_path_for(entry)
        if not os.path.isfile(path):
            self._failures.append(
                f"资源 {entry.resource_id} 缺失: {entry.archive_path}")
            raise ResourceResolutionError(
                f"资源文件缺失: {entry.archive_path} "
                f"(resource_id={entry.resource_id})")
        verified = False
        if self._verify:
            try:
                stat = os.stat(path)
                key = (entry.sha256, entry.size_bytes, path, stat.st_mtime)
                if self._verified.get(key):
                    verified = True
                else:
                    self._check_content(entry, path)
                    self._verified[key] = True
                    verified = True
            except ResourceResolutionError:
                raise
        return ResolvedResource(entry=entry, local_path=path, verified=verified)

    def _check_content(self, entry: ResourceEntry, path: str) -> None:
        actual_size = os.path.getsize(path)
        if actual_size != entry.size_bytes:
            self._failures.append(
                f"资源 {entry.resource_id} 大小不符: 索引 {entry.size_bytes}，"
                f"实际 {actual_size}")
            raise ResourceResolutionError(
                f"资源大小不符（索引 {entry.size_bytes}，实际 {actual_size}）: "
                f"{entry.archive_path}")
        actual_sha = sha256_file(path)
        if actual_sha != entry.sha256:
            self._failures.append(
                f"资源 {entry.resource_id} 哈希不符: 索引 {entry.sha256}，"
                f"实际 {actual_sha}")
            raise ResourceResolutionError(
                f"资源内容哈希不符（索引 {entry.sha256[:12]}…，"
                f"实际 {actual_sha[:12]}…）: {entry.archive_path}")

    # ------------------------------------------------------------------
    # 公共解析入口
    # ------------------------------------------------------------------

    def resolve_uri(self, uri: str) -> ResolvedResource:
        """按逻辑 URI 解析（``topos-res://<id>`` 或 ``topos-res://sha256-…``）。"""
        if not is_valid_resource_uri(uri):
            raise ResourceResolutionError(f"非法资源 URI: {uri!r}")
        entry = self._index.by_uri(uri)
        if entry is None:
            raise ResourceResolutionError(f"未知资源 URI: {uri}")
        return self._resolve_entry(entry)

    def resolve_for_asset(
        self, asset_id: str, role: str | None = None,
    ) -> ResolvedResource | None:
        """解析某素材的内嵌源资源；素材无内嵌资源时返回 None（合法：
        外部路径素材）。"""
        entry = self._index.for_asset(asset_id, role=role)
        if entry is None:
            return None
        return self._resolve_entry(entry)

    def iter_entries(self) -> Iterator[ResourceEntry]:
        yield from self._index.entries.values()


# ================================================================
# 当前工程资源上下文（DESK-02B）
# ================================================================
# 工程一次只打开一个；加载时把 resolver 设为当前上下文，关闭/换工程时
# 置 None。消费方（LUT/贴图/遮罩/生成资源加载器）经
# ``resolve_project_reference`` 把 ``topos-res://`` 引用解析成本地文件，
# 不必各自持有 resolver（与 color_management 的工程级覆盖层同一模式）。

_ACTIVE_RESOLVER_LOCK = threading.Lock()
_ACTIVE_RESOLVER: "ProjectResourceResolver | None" = None


def set_active_project_resolver(
    resolver: "ProjectResourceResolver | None",
) -> None:
    """设置/清除当前工程的资源解析器（load 时调用；None = 工程关闭）。"""
    global _ACTIVE_RESOLVER
    with _ACTIVE_RESOLVER_LOCK:
        _ACTIVE_RESOLVER = resolver


def get_active_project_resolver() -> "ProjectResourceResolver | None":
    """当前工程的资源解析器（无工程/旧包无资源索引 → None）。"""
    with _ACTIVE_RESOLVER_LOCK:
        return _ACTIVE_RESOLVER


def resolve_project_reference(reference: str) -> str | None:
    """把 ``topos-res://`` 逻辑引用解析为本地物化路径（DESK-02B）。

    - 非 URI / 无激活 resolver / 解析失败 → None（调用方回退外部路径
      或按缺失处理；失败细节在 resolver.diagnostics() 里记账）。
    - 成功 → 已通过内容校验的 seekable 本地路径（缓存路径，不落盘）。
    """
    if not isinstance(reference, str) or not reference.startswith(
            f"{RESOURCE_URI_SCHEME}://"):
        return None
    resolver = get_active_project_resolver()
    if resolver is None:
        return None
    try:
        return resolver.resolve_uri(reference).local_path
    except ResourceResolutionError:
        return None
