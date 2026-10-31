"""
Proxy Manifest
资产级磁盘代理清单（规格书 §7.1 / Stage D）。

职责：
- 以资产/源文件为单位持久化代理状态到磁盘（JSON），避免每次启动重新探测。
- 提供 <1ms 级别查询（内存缓存 + 懒加载），不阻塞播放热路径。
- 记录源文件 mtime/size，用于失效判断。
- 与 DiskProxyService 共享 ProxyStatus 语义。

存储布局（规格书 §7.1）：
    .cache/video_proxy/manifest.json
    .cache/video_proxy/<hash>/<proxy_file>.mov

设计原则：
- 复用现有 proxy.py 的 ProxyStatus / generate_proxy_key，不重复造状态枚举。
- 与 DiskProxyService 互补：DiskProxyService 负责生成，manifest 负责持久化与快速查询。
- 不在播放热路径里做磁盘 IO；查询走内存，持久化是异步/批量的。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Dict, Optional

from .proxy import DiskProxyConfig, ProxyStatus

logger = logging.getLogger(__name__)


# =============================================================================
# Manifest 数据结构（规格书 §7.1）
# =============================================================================


@dataclass(frozen=True)
class ProxyManifestKey:
    """
    代理清单 key（不可变）。

    包含源文件特征 + 代理策略，用于判断代理是否仍然有效。
    源文件 mtime/size 变化或策略变化时，代理自动失效。

    Attributes:
        source_path: 源文件绝对路径
        source_mtime_ns: 源文件 mtime（纳秒，更精确）
        source_size: 源文件大小（字节）
        codec_policy: 代理编码策略名（如 "prores_proxy"）
        resolution_policy: 代理分辨率策略名（如 "half"）
        app_version: 应用版本（用于版本升级时强制重建代理）
    """

    source_path: str
    source_mtime_ns: int
    source_size: int
    codec_policy: str
    resolution_policy: str
    app_version: str

    def to_hash(self) -> str:
        """生成稳定 hash（用于磁盘目录名 + 快速比较）。"""
        raw = "|".join([
            self.source_path,
            str(self.source_mtime_ns),
            str(self.source_size),
            self.codec_policy,
            self.resolution_policy,
            self.app_version,
        ])
        return hashlib.md5(raw.encode("utf-8")).hexdigest()


@dataclass
class ProxyManifestEntry:
    """
    代理清单条目。

    Attributes:
        key: 清单 key
        status: 当前状态（MISSING/GENERATING/READY/STALE/FAILED）
        proxy_path: 代理文件路径（ready 时有效）
        width / height: 代理分辨率
        codec: 代理编码（如 "prores_ks" / "dnxhd"）
        pixel_format: 代理像素格式
        created_at / updated_at: 时间戳
        progress: 生成进度 0.0-1.0
        error: 失败原因（可空）
    """

    key: ProxyManifestKey
    status: ProxyStatus
    proxy_path: Optional[str]
    width: int
    height: int
    codec: str
    pixel_format: str
    created_at: float
    updated_at: float
    progress: float = 0.0
    error: Optional[str] = None

    @property
    def is_ready(self) -> bool:
        return self.status is ProxyStatus.READY and self.proxy_path is not None

    @property
    def is_generating(self) -> bool:
        return self.status is ProxyStatus.GENERATING

    @property
    def is_failed(self) -> bool:
        return self.status is ProxyStatus.FAILED

    def to_dict(self) -> Dict[str, object]:
        """序列化为 dict（JSON 持久化用）。"""
        d = asdict(self)
        # ProxyStatus -> str
        d["status"] = self.status.value
        d["key"] = asdict(self.key)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "ProxyManifestEntry":
        """从 dict 反序列化。"""
        key_d = d["key"]  # type: ignore[index]
        key = ProxyManifestKey(
            source_path=str(key_d["source_path"]),  # type: ignore[index]
            source_mtime_ns=int(key_d["source_mtime_ns"]),  # type: ignore[index]
            source_size=int(key_d["source_size"]),  # type: ignore[index]
            codec_policy=str(key_d["codec_policy"]),  # type: ignore[index]
            resolution_policy=str(key_d["resolution_policy"]),  # type: ignore[index]
            app_version=str(key_d["app_version"]),  # type: ignore[index]
        )
        status_str = str(d.get("status", "missing"))
        try:
            status = ProxyStatus(status_str)
        except ValueError:
            status = ProxyStatus.MISSING
        return cls(
            key=key,
            status=status,
            proxy_path=d.get("proxy_path"),  # type: ignore[arg-type]
            width=int(d.get("width", 0)),  # type: ignore[arg-type]
            height=int(d.get("height", 0)),  # type: ignore[arg-type]
            codec=str(d.get("codec", "")),  # type: ignore[arg-type]
            pixel_format=str(d.get("pixel_format", "")),  # type: ignore[arg-type]
            created_at=float(d.get("created_at", 0.0)),  # type: ignore[arg-type]
            updated_at=float(d.get("updated_at", 0.0)),  # type: ignore[arg-type]
            progress=float(d.get("progress", 0.0)),  # type: ignore[arg-type]
            error=d.get("error"),  # type: ignore[arg-type]
        )


# =============================================================================
# ProxyManifest
# =============================================================================


class ProxyManifest:
    """
    代理清单持久化存储。

    用法：
        manifest = ProxyManifest(cache_dir=Path(".cache/video_proxy"))
        manifest.load()
        entry = manifest.get_entry(source_path, policy_key)
        manifest.upsert_entry(entry)
        manifest.save()

    线程安全：所有查询/修改加锁。
    """

    MANIFEST_FILENAME = "manifest.json"
    SCHEMA_VERSION = 1

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._manifest_path = self._cache_dir / self.MANIFEST_FILENAME
        self._lock = threading.RLock()
        # key_hash -> ProxyManifestEntry
        self._entries: Dict[str, ProxyManifestEntry] = {}
        # source_path -> {key_hash}（快速反查，用于 invalidate_by_source）。
        # AUD-35616：key 含 codec_policy/resolution_policy/app_version，
        # 同一 source 可产生多个 entry——索引必须是 hash 集合。此前每源
        # 只保留最后一次 upsert 的 key_hash，多策略/版本条目被挤出，
        # invalidate/remove 漏处理孤儿条目，重载后陈旧条目重新可见。
        self._source_index: Dict[str, set] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # load / save
    # ------------------------------------------------------------------

    def load(self) -> None:
        """从磁盘加载 manifest（不存在则空 manifest）。"""
        with self._lock:
            self._entries.clear()
            self._source_index.clear()
            if self._manifest_path.exists():
                try:
                    data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
                    entries = data.get("entries", [])
                    for ed in entries:
                        try:
                            entry = ProxyManifestEntry.from_dict(ed)
                            key_hash = entry.key.to_hash()
                            self._entries[key_hash] = entry
                            self._source_index.setdefault(
                                entry.key.source_path, set()).add(key_hash)
                        except Exception as e:
                            logger.debug(f"Skip malformed manifest entry: {e}")
                except Exception as e:
                    logger.warning(f"Failed to load proxy manifest {self._manifest_path}: {e}")
            self._loaded = True
            logger.debug(f"ProxyManifest loaded: {len(self._entries)} entries")

    def save(self) -> None:
        """持久化 manifest 到磁盘。"""
        with self._lock:
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                data = {
                    "schema_version": self.SCHEMA_VERSION,
                    "saved_at": time.time(),
                    "entries": [e.to_dict() for e in self._entries.values()],
                }
                tmp_path = self._manifest_path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp_path.replace(self._manifest_path)
            except Exception as e:
                logger.warning(f"Failed to save proxy manifest: {e}")

    def ensure_loaded(self) -> None:
        """懒加载（首次访问时自动 load）。

        AUD-35618：double-checked locking——原实现 `_loaded` 在锁外读取，
        两线程首次并发查询会各自执行一次全量 load（含磁盘读与清空重建），
        后完成者覆盖前者，中间态下另一线程刚 upsert 的 entry 可能被清掉。
        """
        if self._loaded:
            return
        with self._lock:
            if not self._loaded:
                self.load()

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------

    def get_entry(self, source_path: str) -> Optional[ProxyManifestEntry]:
        """按 source_path 查询 entry（<1ms，纯内存）。

        同源存在多条策略/版本 entry 时（AUD-35616：索引为多条目），
        返回 updated_at 最新的那条。

        AUD-35617：返回 entry 的拷贝而非内部可变对象——ProxyManifestEntry
        为可变 dataclass，返回内部引用会让调用方绕过锁直接改写
        status/progress，save() 序列化时可能读到字段间不一致的状态。
        """
        self.ensure_loaded()
        with self._lock:
            key_hashes = self._source_index.get(source_path)
            if not key_hashes:
                return None
            candidates = [
                self._entries[h] for h in key_hashes if h in self._entries
            ]
            if not candidates:
                return None
            return replace(max(candidates, key=lambda e: e.updated_at))

    def has_ready_proxy(self, source_path: str) -> bool:
        """快速判断是否有 ready 代理（policy 查询热路径）。"""
        entry = self.get_entry(source_path)
        return entry is not None and entry.is_ready

    # ------------------------------------------------------------------
    # mutate
    # ------------------------------------------------------------------

    def upsert_entry(self, entry: ProxyManifestEntry) -> None:
        """插入或更新 entry。

        AUD-35617：存储 entry 的拷贝——调用方随后原地改写传入对象
        不应穿透到 manifest 内部（与 get_entry 返回拷贝对称）。
        """
        with self._lock:
            key_hash = entry.key.to_hash()
            self._entries[key_hash] = replace(entry)
            self._source_index.setdefault(
                entry.key.source_path, set()).add(key_hash)

    def invalidate_by_source(self, source_path: str, reason: str = "") -> None:
        """按 source 失效所有相关 entry（源文件被修改时调用）。

        AUD-35616：遍历该源的全部 key_hash（多策略/版本条目），
        而非索引指向的单条。
        """
        with self._lock:
            key_hashes = self._source_index.get(source_path)
            if not key_hashes:
                return
            for key_hash in key_hashes:
                entry = self._entries.get(key_hash)
                if entry is not None:
                    entry.status = ProxyStatus.STALE
                    entry.updated_at = time.time()
            logger.debug(
                f"Proxy invalidated: source={source_path}, "
                f"entries={len(key_hashes)}, reason={reason}"
            )

    def remove_entry(self, source_path: str) -> None:
        """删除 entry（代理被删除时）。

        AUD-35616：删除该源的全部 key_hash 对应条目。
        """
        with self._lock:
            key_hashes = self._source_index.pop(source_path, None)
            if not key_hashes:
                return
            for key_hash in key_hashes:
                self._entries.pop(key_hash, None)

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for e in self._entries.values():
                k = e.status.value
                counts[k] = counts.get(k, 0) + 1
            counts["total"] = len(self._entries)
            return counts


# =============================================================================
# 辅助：从 DiskProxyConfig + 源文件信息构造 key
# =============================================================================


def build_manifest_key(
    source_path: str,
    config: DiskProxyConfig,
    app_version: str = "1",
) -> Optional[ProxyManifestKey]:
    """
    从源文件 + 代理配置构造 manifest key。

    Returns:
        ProxyManifestKey 或 None（源文件不存在时）。
    """
    try:
        stat = os.stat(source_path)
    except OSError:
        return None
    return ProxyManifestKey(
        source_path=source_path,
        source_mtime_ns=stat.st_mtime_ns,
        source_size=stat.st_size,
        codec_policy=config.format.name,
        resolution_policy=config.resolution.name,
        app_version=app_version,
    )
