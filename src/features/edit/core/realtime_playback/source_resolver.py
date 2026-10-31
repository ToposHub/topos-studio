"""
Realtime Source Resolver
把 AssetProxyManager + RenderCacheAdapter 接入 RealtimePlaybackEngine
的 source 决策路径（缺陷计划 RP-P0-07/09 / §3.3）。

## 目标

拖入时间线后不蓝屏、不解 master。realtime source 优先级：
    RENDER_CACHE -> DISK_PROXY -> PLACEHOLDER/HOLD

## 职责

- 给 RealtimePlaybackEngine 提供 per-layer source decision（用 policy）。
- 查询 AssetProxyManager 的 proxy ready 状态。
- 查询 RenderCacheAdapter 的 cache ready 状态。
- proxy/cache 未 ready 时返回 PLACEHOLDER，触发 hold/rebuffer，不解 master。
- 拖入新素材时触发后台 proxy 生成（ensure_proxy_async）。

## 集成

engine.request_present -> SourceResolver.resolve_layers -> policy.decide_layer_source
返回 SourceDecision（source_kind / allow_master_fallback / reason）。
engine 根据 decision 决定渲染策略。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional

from .policy import RealtimePlaybackPolicy, get_default_policy
from .render_cache_adapter import RenderCacheAdapter
from .types import PlaybackMode, SourceDecision

logger = logging.getLogger(__name__)


# =============================================================================
# LayerSourceQuery：单个 layer 的查询输入
# =============================================================================


@dataclass
class LayerSourceQuery:
    """
    单个 layer 的 source 查询输入。

    Attributes:
        clip_id: timeline clip id
        asset_id: 素材 id
        source_path: 源文件路径
        source_frame: 源帧号
        timeline_frame: 时间线帧号
        requires_effect_cache: 是否有复杂效果需要 render cache
    """
    clip_id: str
    asset_id: str
    source_path: str
    source_frame: int
    timeline_frame: int
    requires_effect_cache: bool = False


# =============================================================================
# ProxyStateAdapter：把 AssetProxyManager 的 entry 适配为 policy 的 snapshot
# =============================================================================


class _ProxyStateFromEntry:
    """把 ProxyManifestEntry 适配为 ProxyStateSnapshot。"""

    def __init__(self, entry: Any) -> None:
        self._entry = entry

    @property
    def has_disk_proxy_ready(self) -> bool:
        return getattr(self._entry, "is_ready", False)

    @property
    def is_disk_proxy_generating(self) -> bool:
        return getattr(self._entry, "is_generating", False)

    @property
    def has_disk_proxy_failed(self) -> bool:
        return getattr(self._entry, "is_failed", False)

    @property
    def has_memory_proxy(self) -> bool:
        # memory proxy 是过渡，不作为专业路径；这里保守返回 False，
        # 让 policy 走 PLACEHOLDER 而非 memory_proxy。
        return False

    @property
    def disk_proxy_path(self) -> Optional[str]:
        return getattr(self._entry, "proxy_path", None)


# =============================================================================
# DiskProxyServiceAdapter：把全局 DiskProxyService 适配为 AssetProxyManager 接口
# Final-1 (FINAL-P0-03): SourceResolver 通过这个适配器查询/触发磁盘代理，
#                          不需要修改既有 AssetProxyManager 代码。
# =============================================================================


class _DiskProxyManifestEntry:
    """
    把 DiskProxyService 返回的 DiskProxyInfo 适配为 ProxyManifestEntry 接口。
    SourceResolver._ProxyStateFromEntry 会消费 is_ready/is_generating/is_failed/proxy_path。
    """

    def __init__(self, info: Any) -> None:
        self._info = info

    @property
    def is_ready(self) -> bool:
        return bool(getattr(self._info, "is_ready", False))

    @property
    def is_generating(self) -> bool:
        status = getattr(self._info, "status", None)
        name = getattr(status, "value", str(status))
        return name == "generating"

    @property
    def is_failed(self) -> bool:
        status = getattr(self._info, "status", None)
        name = getattr(status, "value", str(status))
        return name == "failed"

    @property
    def proxy_path(self) -> Optional[str]:
        return getattr(self._info, "proxy_path", None)


class DiskProxyServiceAdapter:
    """
    把全局 DiskProxyService 适配为 SourceResolver 期望的 proxy_manager 接口。

    SourceResolver 需要：
        - get_proxy_entry(source_path) -> entry (is_ready/is_generating/proxy_path)
        - ensure_proxy_async(source_path, asset_id=...) -> None

    DiskProxyService 实际提供：
        - get_disk_proxy_info(source_path) -> DiskProxyInfo
        - generate_proxy(source_path, asset_id, ...) -> Optional[task_id]

    这个适配器做两者之间的转换，让 SourceResolver 可以直接消费既有的
    DiskProxyService（不依赖 AssetProxyManager）。
    """

    def __init__(self, service: Any = None) -> None:
        self._service = service
        # AUD-17414：懒解析的赋值在同一把锁内完成（双检锁）。
        self._service_lock = threading.Lock()

    def _resolve_service(self) -> Any:
        service = self._service
        if service is not None:
            return service
        # AUD-17414：懒加载全局 DiskProxyService（避免 import 时副作用）。
        # 失败不再把 self._service 固化为 None——只把 None 作为本次返回
        # 值，服务恢复后下次调用会重新尝试解析（旧实现在 import 失败/
        # 服务未就绪时永久放弃，proxy 永远不会再被解析）。
        with self._service_lock:
            if self._service is not None:
                return self._service
            try:
                from src.shared.media.disk_proxy_service import get_disk_proxy_service
                service = get_disk_proxy_service()
            except Exception as e:
                logger.debug(f"DiskProxyServiceAdapter: get_disk_proxy_service failed: {e}")
                return None
            self._service = service
            return service

    def get_proxy_entry(self, source_path: str) -> Optional[_DiskProxyManifestEntry]:
        service = self._resolve_service()
        if service is None:
            return None
        try:
            info = service.get_disk_proxy_info(source_path)
        except Exception as e:
            logger.debug(f"get_proxy_entry failed for {source_path}: {e}")
            return None
        if info is None:
            return None
        return _DiskProxyManifestEntry(info)

    def ensure_proxy_async(self, source_path: str, asset_id: str = "") -> None:
        """触发后台 proxy 生成（不阻塞调用方）。"""
        service = self._resolve_service()
        if service is None:
            return
        try:
            service.generate_proxy(source_path=source_path, asset_id=asset_id)
        except Exception as e:
            logger.debug(f"ensure_proxy_async failed for {source_path}: {e}")


# =============================================================================
# Per-clip render-cache snapshot（AUD-17409）
# =============================================================================


class _PerClipRenderCacheState:
    """把 RenderCacheAdapter 的全局索引投影为单 clip 的快照视图。

    policy 的 RenderCacheStateSnapshot 协议是逐 layer 查询的；旧实现把
    同一个全局 adapter 传给每个 layer 的 decision，导致"任意 clip ready"
    被当成"本 clip ready"——多 clip 时间线下无缓存 layer 会拿到第一个
    ready 条目（其他 clip）的缓存路径 → 错误解码源/渲染错帧。本视图只
    暴露绑定 clip_id 的条目，policy 无需感知 clip 维度。
    """

    __slots__ = ("_adapter", "_clip_id")

    def __init__(self, adapter: RenderCacheAdapter, clip_id: str) -> None:
        self._adapter = adapter
        self._clip_id = clip_id

    @property
    def has_render_cache_ready(self) -> bool:
        ready_fn = getattr(self._adapter, "has_render_cache_ready_for", None)
        return bool(ready_fn(self._clip_id)) if callable(ready_fn) else False

    @property
    def render_cache_path(self) -> Optional[str]:
        path_fn = getattr(self._adapter, "render_cache_path_for", None)
        return path_fn(self._clip_id) if callable(path_fn) else None


# =============================================================================
# SourceResolver
# =============================================================================


class SourceResolver:
    """
    实时播放 source 解析器。

    用法：
        resolver = SourceResolver(proxy_manager=..., render_cache=...)
        resolver.bind_engine(engine)
        # 每帧
        decisions = resolver.resolve_layers(mode=REALTIME, queries=[...])
        for q, d in zip(queries, decisions):
            if d.source_kind is PLACEHOLDER and d.reason contains 'proxy_missing':
                resolver.ensure_proxy(q)  # 后台触发 proxy 生成
    """

    def __init__(
        self,
        *,
        proxy_manager: Any = None,  # AssetProxyManager
        render_cache: Optional[RenderCacheAdapter] = None,
        policy: Optional[RealtimePlaybackPolicy] = None,
    ) -> None:
        self._proxy_manager = proxy_manager
        self._render_cache = render_cache or RenderCacheAdapter()
        self._policy = policy or get_default_policy()
        # 已 ensure 过的 source_path（避免重复触发后台生成）
        self._ensured_proxies: set[str] = set()
        # AUD-17413：集合的 check-then-add 与后台触发在同一把锁内原子
        # 完成——多轨/多线程并发调用 ensure_proxy 时旧实现可对同一路径
        # 重复触发后台代理生成。
        self._ensure_proxy_lock = threading.Lock()

    # ------------------------------------------------------------------
    # bind
    # ------------------------------------------------------------------

    def bind_engine(self, engine: Any) -> None:
        """把 resolver 注入 engine（engine 调用 resolve_layers）。"""
        # engine 持有 resolver 引用即可，这里做 type-erased 绑定
        engine._source_resolver = self  # type: ignore[attr-defined]

    def configure(
        self,
        *,
        proxy_manager: Any = None,
        render_cache: Optional[RenderCacheAdapter] = None,
    ) -> None:
        if proxy_manager is not None:
            self._proxy_manager = proxy_manager
        if render_cache is not None:
            self._render_cache = render_cache

    # ------------------------------------------------------------------
    # resolve（engine 热路径）
    # ------------------------------------------------------------------

    def resolve_layers(
        self,
        *,
        mode: PlaybackMode,
        queries: list[LayerSourceQuery],
    ) -> list[SourceDecision]:
        """
        为多个 layer 解析 source decision。

        Returns:
            与 queries 等长的 SourceDecision 列表。
        """
        decisions: list[SourceDecision] = []
        for q in queries:
            proxy_state = self._query_proxy_state(q.source_path)
            cache_state = self._per_clip_cache_state(q.clip_id)
            decision = self._policy.decide_layer_source(
                mode=mode,
                asset_id=q.asset_id,
                source_path=q.source_path,
                timeline_frame=q.timeline_frame,
                source_frame=q.source_frame,
                clip_id=q.clip_id,
                requires_effect_cache=q.requires_effect_cache,
                proxy_state=proxy_state,
                render_cache_state=cache_state,
            )
            decisions.append(decision)
        return decisions

    def resolve_single(
        self,
        *,
        mode: PlaybackMode,
        query: LayerSourceQuery,
    ) -> SourceDecision:
        """单个 layer 解析。"""
        return self.resolve_layers(mode=mode, queries=[query])[0]

    # ------------------------------------------------------------------
    # proxy ensure（后台生成）
    # ------------------------------------------------------------------

    def ensure_proxy(self, query: LayerSourceQuery) -> None:
        """
        后台触发 proxy 生成（缺陷计划 RP-P0-09：拖入素材后立即创建 proxy job）。

        只对每个 source_path 触发一次（去重），避免重复生成。
        不阻塞调用方（AssetProxyManager 内部异步）。
        """
        if self._proxy_manager is None:
            return
        # AUD-17413：检查 + 登记 + 后台触发在锁内原子完成。锁内调用的是
        # proxy_manager 的非阻塞接口（get_proxy_entry / ensure_proxy_async
        # 内部异步），持锁窗口有界。
        with self._ensure_proxy_lock:
            if query.source_path in self._ensured_proxies:
                return
            # 检查是否已有 ready proxy
            entry = self._proxy_manager.get_proxy_entry(query.source_path)
            if entry is not None and getattr(entry, "is_ready", False):
                self._ensured_proxies.add(query.source_path)
                return
            if entry is not None and getattr(entry, "is_generating", False):
                self._ensured_proxies.add(query.source_path)
                return
            try:
                self._proxy_manager.ensure_proxy_async(
                    source_path=query.source_path,
                    asset_id=query.asset_id,
                )
                self._ensured_proxies.add(query.source_path)
                logger.debug(f"SourceResolver: triggered proxy generation for {query.source_path}")
            except Exception as e:
                logger.debug(f"SourceResolver ensure_proxy failed: {e}")

    def invalidate_proxy_cache(self, source_path: str) -> None:
        """源文件变化时清空 ensure 去重缓存。"""
        with self._ensure_proxy_lock:
            self._ensured_proxies.discard(source_path)

    # ------------------------------------------------------------------
    # query helpers
    # ------------------------------------------------------------------

    def _query_proxy_state(self, source_path: str) -> Optional[_ProxyStateFromEntry]:
        """查询 proxy 状态，返回 policy 兼容的 snapshot。"""
        if self._proxy_manager is None:
            return None
        try:
            entry = self._proxy_manager.get_proxy_entry(source_path)
            if entry is None:
                return None
            return _ProxyStateFromEntry(entry)
        except Exception as e:
            logger.debug(f"SourceResolver proxy query failed: {e}")
            return None

    def _per_clip_cache_state(self, clip_id: str) -> Any:
        """AUD-17409：为 policy 构造按 clip 限定的 render-cache 快照。

        RenderCacheAdapter（本仓默认实现）经 _PerClipRenderCacheState 视图
        限定到当前 layer 的 clip_id；外部注入的自定义快照实现若不提供
        per-clip 方法，视为调用方已自行按 layer 限定，原样透传。
        """
        adapter = self._render_cache
        if adapter is None:
            return None
        if callable(getattr(adapter, "has_render_cache_ready_for", None)):
            return _PerClipRenderCacheState(adapter, clip_id)
        return adapter

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        with self._ensure_proxy_lock:
            ensured_count = len(self._ensured_proxies)
        return {
            "ensured_proxies": ensured_count,
            "has_proxy_manager": self._proxy_manager is not None,
            "render_cache_ready": self._render_cache.has_render_cache_ready,
        }
