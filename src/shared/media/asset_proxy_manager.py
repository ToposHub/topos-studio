# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Asset Proxy Manager
资产级磁盘代理统一管理器（规格书 §7.2 / Stage D）。

职责：
- 以资产/源文件为单位管理磁盘代理（不是 per timeline clip）。
- 复用 DiskProxyService 的实际转码能力。
- 维护 ProxyManifest，避免每个 timeline clip 重复生成。
- 给 RealtimePlaybackPolicy 提供快速查询（<1ms），不阻塞播放热路径。

设计原则（规格书 §2 / §7.3）：
- 包装/演进 DiskProxyService + ProxyManager，不是复制一套代理系统。
- 同一 source path 的多次 ensure_proxy_async 只生成一个 proxy entry。
- 代理生成在后台低优先级队列，不在播放热路径 / render worker 触发。
- 不把生成中的 proxy 当 ready。

验收（规格书 §7）：
- 同源素材拖入两轨只生成一个 proxy entry。
- 查询 <1ms。
- 生成期间播放不阻塞。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import replace as _dataclass_replace
from pathlib import Path
from typing import Any, Dict, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .proxy import (
    DEFAULT_DISK_PROXY_CONFIG,
    DiskProxyConfig,
    DiskProxyInfo,
    ProxyStatus,
)
from .proxy_manifest import (
    ProxyManifest,
    ProxyManifestEntry,
    ProxyManifestKey,
    build_manifest_key,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ProxyBuildPolicy（规格书 §7.3）
# =============================================================================


_PROXY_BUILD_POLICY_DEFAULTS = {
    "max_width": 1920,
    "max_height": 1080,
    "prefer_intraframe": True,
    "codec_order": ("prores_proxy", "dnx_lb", "h264_intra"),
    "allow_long_gop": False,
    "priority": 0,
}


def _default_codec_policy(config: DiskProxyConfig) -> str:
    return config.format.name


def _default_resolution_policy(config: DiskProxyConfig) -> str:
    return config.resolution.name


# AUD-34310：GENERATING 滞留自愈阈值。entry/pending 停留在 GENERATING
# 超过该时长且服务侧确认无生成任务时，视为孤儿状态重建（生成期间服务
# 缓存会保持 GENERATING，长转码不受影响）。
_GENERATING_STALE_TIMEOUT_S: float = 120.0


# =============================================================================
# AssetProxyManager
# =============================================================================


class AssetProxyManager(QObject):
    """
    资产级代理管理器。

    信号：
        proxy_state_changed(source_path, entry): 代理状态变化（ready/generating/failed）。
        proxy_required(source_path, reason): 需要 proxy（用于 UI 提示生成中）。

    用法：
        mgr = AssetProxyManager()
        mgr.configure(cache_dir=Path(".cache/video_proxy"))
        entry = mgr.get_proxy_entry(source_path)
        if entry is None or not entry.is_ready:
            mgr.ensure_proxy_async(source_path=..., asset_id=..., config=...)
        # 解码时
        source = mgr.open_proxy_source(entry)  # ready 时返回代理源
    """

    proxy_state_changed = pyqtSignal(str, object)  # source_path, ProxyManifestEntry
    proxy_required = pyqtSignal(str, str)          # source_path, reason

    def __init__(
        self,
        disk_proxy_service: Any | None = None,
        config: Optional[DiskProxyConfig] = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config or DEFAULT_DISK_PROXY_CONFIG
        self._disk_proxy_service = disk_proxy_service  # DiskProxyService（懒注入）
        self._manifest: Optional[ProxyManifest] = None
        self._cache_dir: Optional[Path] = None
        self._app_version = "1"
        self._lock = threading.RLock()
        # source_path -> 生成去重 guard（同源只生成一次；值为派发时间戳，
        # AUD-34310：用于滞留 pending 的超时自愈）
        self._pending_generates: Dict[str, float] = {}
        # 注册到 DiskProxyService 的回调（用于解绑）
        self._registered_callback = False

    # ------------------------------------------------------------------
    # configure
    # ------------------------------------------------------------------

    def configure(
        self,
        *,
        cache_dir: Path,
        app_version: str = "1",
        disk_proxy_service: Any | None = None,
    ) -> None:
        """
        配置代理管理器。

        Args:
            cache_dir: 代理缓存根目录（manifest + proxy 文件）。
            app_version: 应用版本（用于 key，版本升级强制重建）。
            disk_proxy_service: DiskProxyService 实例（若 None 则惰性获取）。
        """
        self._cache_dir = Path(cache_dir)
        self._app_version = app_version
        self._manifest = ProxyManifest(self._cache_dir)
        self._manifest.load()
        if disk_proxy_service is not None:
            self._disk_proxy_service = disk_proxy_service
            self._register_disk_service_callback()
        logger.info(
            f"AssetProxyManager configured: cache_dir={self._cache_dir}, "
            f"app_version={app_version}, entries={self._manifest.entry_count}"
        )

    def _register_disk_service_callback(self) -> None:
        """注册状态回调，把 DiskProxyService 的状态变化同步到 manifest。"""
        if self._registered_callback or self._disk_proxy_service is None:
            return
        try:
            self._disk_proxy_service.register_status_callback(self._on_disk_proxy_status)
            self._registered_callback = True
        except Exception as e:
            logger.debug(f"register_status_callback failed: {e}")

    # ------------------------------------------------------------------
    # query（播放热路径，<1ms）
    # ------------------------------------------------------------------

    def get_proxy_entry(self, source_path: str) -> Optional[ProxyManifestEntry]:
        """
        查询资产代理状态（<1ms，纯内存 manifest 查询）。

        这是 RealtimePlaybackPolicy 的主要查询入口。不在热路径做磁盘 IO。
        """
        if self._manifest is None:
            return None
        return self._manifest.get_entry(source_path)

    def is_proxy_ready(self, source_path: str) -> bool:
        """快速判断代理是否 ready（policy 查询热路径）。"""
        if self._manifest is None:
            return False
        return self._manifest.has_ready_proxy(source_path)

    # ------------------------------------------------------------------
    # ensure（后台生成）
    # ------------------------------------------------------------------

    def ensure_proxy_async(
        self,
        *,
        source_path: str,
        asset_id: str,
        config: Optional[DiskProxyConfig] = None,
    ) -> ProxyManifestEntry:
        """
        确保资产有代理（如缺失则后台生成）。

        关键不变量（规格书 §7.3）：
        - 同一 source_path 的多次调用只生成一次（去重 guard）。
        - 不在 render worker / 播放热路径触发重转码。
        - 生成中返回 GENERATING 状态，不当 ready。

        Returns:
            当前的 ProxyManifestEntry（可能是 MISSING/GENERATING/READY）。
        """
        if self._manifest is None:
            # 未配置时返回占位 entry（不应该发生，调用方应先 configure）。
            # AUD-34313：无条件返回占位 entry，不得落入下方 assert
            # （常规运行 AssertionError / -O 下 AttributeError）。
            key = build_manifest_key(source_path, config or self._config, self._app_version)
            if key is None:
                return ProxyManifestEntry(
                    key=ProxyManifestKey(source_path, 0, 0, "?", "?", self._app_version),
                    status=ProxyStatus.MISSING,
                    proxy_path=None, width=0, height=0, codec="", pixel_format="",
                    created_at=time.time(), updated_at=time.time(),
                    error="AssetProxyManager not configured",
                )
            return ProxyManifestEntry(
                key=key,
                status=ProxyStatus.MISSING,
                proxy_path=None, width=0, height=0,
                codec=_default_codec_policy(config or self._config),
                pixel_format="",
                created_at=time.time(), updated_at=time.time(),
                error="AssetProxyManager not configured",
            )

        effective_config = config or self._config
        key = build_manifest_key(source_path, effective_config, self._app_version)
        if key is None:
            # 源文件不存在
            placeholder_key = ProxyManifestKey(
                source_path, 0, 0,
                _default_codec_policy(effective_config),
                _default_resolution_policy(effective_config),
                self._app_version,
            )
            return ProxyManifestEntry(
                key=placeholder_key,
                status=ProxyStatus.FAILED,
                proxy_path=None, width=0, height=0, codec="", pixel_format="",
                created_at=time.time(), updated_at=time.time(),
                error="source file not found",
            )

        assert self._manifest is not None
        # 1. 已有 entry：检查 key 是否匹配（源文件变化则失效）
        existing = self._manifest.get_entry(source_path)
        if existing is not None:
            # key 一致且 ready → 直接返回
            if existing.key == key and existing.is_ready:
                return existing
            # key 一致且 generating → 返回 generating（去重）；
            # AUD-34310：滞留 GENERATING（回调被吞/取消未广播导致的
            # 孤儿状态）自愈——重建 entry 并重新派发。
            if existing.key == key and existing.is_generating:
                if self._is_generation_alive(source_path, existing):
                    return existing
                logger.warning(
                    f"滞留 GENERATING entry 自愈（重建）: {source_path}"
                )
                with self._lock:
                    self._pending_generates.pop(source_path, None)
                existing = None

        # 2. 去重 guard：同源正在 ensure 中，不重复触发
        with self._lock:
            pending_since = self._pending_generates.get(source_path)
            if pending_since is not None:
                # AUD-34310：pending 滞留超时（完成回调永不到达的兜底）
                if time.time() - pending_since > _GENERATING_STALE_TIMEOUT_S:
                    logger.warning(
                        f"滞留 pending guard 自愈（重新派发）: {source_path}"
                    )
                    del self._pending_generates[source_path]
                else:
                    # AUD-34309：guard 命中时无论内存 entry 状态如何都
                    # 返回——竞态窗口（A 已置 pending、尚未 upsert entry）
                    # 或 entry 被清空时不得重复派发。
                    if existing is not None:
                        return existing
                    # 锁内重读：线程 A 可能已写入 GENERATING entry
                    reread = self._manifest.get_entry(source_path)
                    if reread is not None:
                        return reread
                    return ProxyManifestEntry(
                        key=key,
                        status=ProxyStatus.GENERATING,
                        proxy_path=None,
                        width=0,
                        height=0,
                        codec=_default_codec_policy(effective_config),
                        pixel_format="",
                        created_at=time.time(),
                        updated_at=time.time(),
                    )
            self._pending_generates[source_path] = time.time()

        # 3. 写入 GENERATING 占位 entry（key 变化时重建）
        entry = ProxyManifestEntry(
            key=key,
            status=ProxyStatus.GENERATING,
            proxy_path=None,
            width=effective_config.target_width if hasattr(effective_config, "target_width") else 0,
            height=effective_config.target_height if hasattr(effective_config, "target_height") else 0,
            codec=_default_codec_policy(effective_config),
            pixel_format="",
            created_at=time.time(),
            updated_at=time.time(),
            progress=0.0,
        )
        self._manifest.upsert_entry(entry)
        # AUD-34314：emit 传递快照拷贝——信号跨线程 queued 投递期间，
        # manifest 内 entry 可能被后续状态迁移改写，接收方不得看到已变更对象
        self.proxy_state_changed.emit(source_path, _dataclass_replace(entry))
        self.proxy_required.emit(source_path, "disk_proxy_generating")

        # 4. 委托 DiskProxyService 后台生成（低优先级队列）
        self._dispatch_generation(source_path, asset_id, effective_config)

        return entry

    def _is_generation_alive(self, source_path: str, entry: ProxyManifestEntry) -> bool:
        """
        判断 GENERATING entry 是否对应仍在进行的生成（AUD-34310）。

        以 DiskProxyService 缓存为权威：服务侧仍报 GENERATING → 存活；
        服务侧无生成任务（MISSING/READY/FAILED/STALE）且 entry 超时 →
        孤儿。查询失败（服务不可用/异常）时保守返回 True，避免误杀。

        Args:
            source_path: 源文件路径
            entry: 当前 GENERATING entry

        Returns:
            True 表示生成仍在进行（entry 应保留）
        """
        if time.time() - entry.updated_at <= _GENERATING_STALE_TIMEOUT_S:
            # 未超时：信任在途状态（转码可能尚未推进服务侧进度）
            return True
        service = self._disk_proxy_service
        if service is None:
            # 无服务可核实：保守视为存活
            return True
        try:
            info = service.get_disk_proxy_info(source_path)
        except Exception as e:  # noqa: BLE001 — 自愈探测失败不阻断 ensure
            logger.debug(f"GENERATING 存活探测失败（保守视为存活）: {e}")
            return True
        return info is not None and info.status == ProxyStatus.GENERATING

    def _dispatch_generation(
        self,
        source_path: str,
        asset_id: str,
        config: DiskProxyConfig,
    ) -> None:
        """委托 DiskProxyService 执行实际转码。"""
        service = self._disk_proxy_service
        if service is None:
            # 惰性获取单例
            try:
                from .disk_proxy_service import get_disk_proxy_service
                service = get_disk_proxy_service()
                self._disk_proxy_service = service
                self._register_disk_service_callback()
            except Exception as e:
                logger.warning(f"DiskProxyService unavailable, proxy generation skipped: {e}")
                self._mark_failed(source_path, f"disk service unavailable: {e}")
                return

        try:
            service.generate_proxy(
                source_path=source_path,
                asset_id=asset_id,
                on_complete=lambda sp, info: self._on_generation_complete(sp, info, config),
                on_error=lambda sp, err: self._mark_failed(sp, err),
            )
        except Exception as e:
            logger.error(f"generate_proxy failed for {source_path}: {e}")
            self._mark_failed(source_path, str(e))

    # ------------------------------------------------------------------
    # open proxy source（解码侧）
    # ------------------------------------------------------------------

    def open_proxy_source(self, entry: ProxyManifestEntry) -> Any:
        """
        打开代理源用于解码。

        仅当 entry.is_ready 时返回 MediaSource；否则返回 None。
        调用方应在 GL/render 线程之外打开，或使用 clip_decode_stream 的 open_async。
        """
        if not entry.is_ready or entry.proxy_path is None:
            return None
        try:
            from .factory import MediaSourceFactory
            # AUD-34311：尺寸未知时不传 target_size（保持代理原生几何），
            # 不再伪造 1920x1080——对竖屏/非 16:9 代理会造成拉伸变形。
            if entry.width > 0 and entry.height > 0:
                target_size: Optional[tuple[int, int]] = (entry.width, entry.height)
            else:
                target_size = None
            return MediaSourceFactory.create(
                path=entry.proxy_path,
                target_size=target_size,
            )
        except Exception as e:
            logger.warning(f"open_proxy_source failed for {entry.proxy_path}: {e}")
            return None

    # ------------------------------------------------------------------
    # invalidate
    # ------------------------------------------------------------------

    def invalidate_proxy(self, source_path: str, reason: str = "") -> None:
        """失效代理（源文件被修改时）。"""
        if self._manifest is None:
            return
        self._manifest.invalidate_by_source(source_path, reason)
        entry = self._manifest.get_entry(source_path)
        if entry is not None:
            # AUD-34314：emit 快照拷贝
            self.proxy_state_changed.emit(source_path, _dataclass_replace(entry))

    # ------------------------------------------------------------------
    # callbacks（DiskProxyService → manifest 同步）
    # ------------------------------------------------------------------

    def _on_disk_proxy_status(self, source_path: str, info: DiskProxyInfo) -> None:
        """DiskProxyService 状态变化回调：同步到 manifest。"""
        self._sync_from_disk_proxy_info(source_path, info)

    def _on_generation_complete(
        self,
        source_path: str,
        info: DiskProxyInfo,
        config: DiskProxyConfig,
    ) -> None:
        """生成完成回调。"""
        self._sync_from_disk_proxy_info(source_path, info)
        with self._lock:
            self._pending_generates.pop(source_path, None)

    def _sync_from_disk_proxy_info(
        self,
        source_path: str,
        info: DiskProxyInfo,
    ) -> None:
        """把 DiskProxyInfo 同步为 ProxyManifestEntry。"""
        if self._manifest is None:
            return
        key = build_manifest_key(source_path, self._config, self._app_version)
        if key is None:
            return
        # AUD-34311：DiskProxyInfo 无 width/height 字段——此前
        # hasattr(info, "width") 恒 False，READY entry 尺寸恒 0x0，
        # proxy_resolution 信息被丢弃。尺寸真实来源是 proxy_resolution。
        resolution = getattr(info, "proxy_resolution", None)
        width, height = (resolution[0], resolution[1]) if resolution else (0, 0)
        entry = ProxyManifestEntry(
            key=key,
            status=info.status,
            proxy_path=info.proxy_path,
            width=width,
            height=height,
            codec=_default_codec_policy(self._config),
            pixel_format=getattr(info, "pixel_format", "") or "",
            created_at=time.time(),
            updated_at=time.time(),
            progress=getattr(info, "generation_progress", 0.0) / 100.0
            if hasattr(info, "generation_progress") else 0.0,
        )
        self._manifest.upsert_entry(entry)
        self._manifest.save()
        # AUD-34314：emit 快照拷贝
        self.proxy_state_changed.emit(source_path, _dataclass_replace(entry))
        # AUD-34310：终态（非 GENERATING）同步时清理 pending——取消/
        # 回调被吞的路径经服务侧 status 广播到此，避免 pending 永久滞留
        # 导致该源无法重新生成。
        if info.status != ProxyStatus.GENERATING:
            with self._lock:
                self._pending_generates.pop(source_path, None)

    def _mark_failed(self, source_path: str, error: str) -> None:
        # AUD-34310：先清 pending 再查 entry——entry 缺失（刚被
        # invalidate/remove）时不得把 pending 留成永久滞留。
        with self._lock:
            self._pending_generates.pop(source_path, None)
        if self._manifest is None:
            return
        entry = self._manifest.get_entry(source_path)
        if entry is None:
            return
        # AUD-34314：以新对象表示状态迁移，不原地改 entry——emit 过的
        # 旧引用（跨线程槽/调用方持有的快照）不受后续变更影响
        entry = _dataclass_replace(
            entry,
            status=ProxyStatus.FAILED,
            error=error,
            updated_at=time.time(),
        )
        self._manifest.upsert_entry(entry)
        self._manifest.save()
        # AUD-34314：emit 快照拷贝
        self.proxy_state_changed.emit(source_path, _dataclass_replace(entry))

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    # ── F6.6 · 安全清理（受保护的代理回收） ────────────────────────

    def purge_proxies(
        self,
        *,
        source_paths: Optional[list] = None,
        statuses: tuple = ("READY", "STALE", "FAILED"),
        only_if_source_missing: bool = False,
    ) -> "dict[str, int]":
        """F6.6：按条件清理代理文件（绝不触碰原片/归档/其他工程文件）。

        安全规则：
        - 只删 **manifest 中登记的 proxy_path**（外部文件不可能误删）；
        - proxy_path 必须位于当前代理目录内（路径逃逸拒绝）；
        - GENERATING 条目跳过（活动任务受保护，播放中清理不炸任务）；
        - 只删文件 + 清 manifest 条目——原片路径永远只读；
        - only_if_source_missing=True 时仅清源已不存在的孤儿代理
          （跨项目引用保护：源在即引用可能在，不动）。

        Returns:
            {"removed": n, "skipped_active": n, "skipped_not_found": n,
             "skipped_outside_dir": n}
        """
        removed = skipped_active = not_found = outside = 0
        if self._manifest is None:
            return {"removed": 0, "skipped_active": 0,
                    "skipped_not_found": 0, "skipped_outside_dir": 0}
        raw_root = str(getattr(self._manifest, "cache_dir", "") or
                        getattr(self._manifest, "_cache_dir", "") or "")
        proxy_root = os.path.abspath(raw_root) if raw_root else ""
        # manifest 迭代：entries 经内部字典（load 后可迭代）
        entries = getattr(self._manifest, "_entries", None) or {}
        for key_text, entry in list(entries.items()):
            source = (entry.key.source_path
                      if getattr(entry, "key", None) is not None else "")
            if source_paths is not None and source not in set(source_paths):
                continue
            # ProxyStatus 枚举值为小写（'ready'）——大小写归一比较
            status_norm = str(getattr(
                getattr(entry, "status", ""), "value",
                getattr(entry, "status", ""))).lower()
            if status_norm not in {s.lower() for s in statuses}:
                continue
            if entry.is_generating:  # 属性而非方法
                skipped_active += 1
                continue
            if only_if_source_missing and os.path.exists(source):
                continue  # 源在 = 引用可能跨项目存在，不动
            proxy_path = getattr(entry, "proxy_path", None)
            if not proxy_path or not os.path.exists(proxy_path):
                self._manifest.remove_entry(source)
                not_found += 1
                continue
            if proxy_root and not os.path.abspath(
                    proxy_path).startswith(proxy_root + os.sep):
                outside += 1  # 代理目录外的一律不动（归档/他工程）
                continue
            try:
                os.remove(proxy_path)
                removed += 1
            except OSError:
                not_found += 1
                continue
            self._manifest.remove_entry(source)
        self.flush()
        return {"removed": removed, "skipped_active": skipped_active,
                "skipped_not_found": not_found, "skipped_outside_dir": outside}

    def proxy_usage_report(self) -> "dict[str, object]":
        """F6.6：当前 manifest 的使用状态（总数/状态分布/占用字节）。"""
        if self._manifest is None:
            return {"total": 0, "statuses": {}, "bytes": 0}
        entries = getattr(self._manifest, "_entries", None) or {}
        statuses: "dict[str, int]" = {}
        total_bytes = 0
        for entry in entries.values():
            status = str(getattr(getattr(entry, "status", ""), "value",
                                 getattr(entry, "status", ""))).lower()
            statuses[status] = statuses.get(status, 0) + 1
            proxy_path = getattr(entry, "proxy_path", None)
            if proxy_path and os.path.exists(proxy_path):
                try:
                    total_bytes += os.path.getsize(proxy_path)
                except OSError:
                    pass
        return {"total": len(entries), "statuses": statuses,
                "bytes": total_bytes}

    def flush(self) -> None:
        """显式持久化 manifest（可定期调用）。"""
        if self._manifest is not None:
            self._manifest.save()

    def stats(self) -> Dict[str, int]:
        if self._manifest is None:
            return {"total": 0}
        return self._manifest.stats()
