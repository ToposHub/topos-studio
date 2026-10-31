"""
Realtime Playback Policy
专业 NLE 实时播放源选择策略（规格书 §6 / Stage C）。

职责：
- 根据播放模式、proxy 状态、render cache 状态，决定每个 layer 应从哪里取帧。
- 明确禁止 realtime 模式 master fallback。
- 输出可 trace 的 SourceDecision。

实时优先级（规格书 §5.2）：
    render_cache > disk_proxy > ready_gpu_cache(memory_proxy 过渡) > placeholder/hold

非实时优先级（paused/step/export）：
    master > render_cache > disk_proxy

注意：policy 输出的 SourceDecision 目前主要用于 telemetry 和后台 proxy 触发，
不直接阻塞 realtime 解码路径（见 engine.py 模块文档"实际行为说明"）。
真正的 master 阻止需要未来在 SourceResolver 层加格式感知策略。

本模块为纯决策逻辑，不依赖 Qt / 不调用解码。
所有外部状态通过 ProxyStateSnapshot / RenderCacheStateSnapshot 传入，
方便单测与 benchmark 复用。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional, Protocol

from .types import (
    DecodeMiss,
    MissReason,
    PlaybackMode,
    SourceDecision,
    SourceKind,
    classify_miss_reason,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 状态快照 Protocols（解耦 policy 与具体 proxy/cache 实现）
# =============================================================================


class ProxyStateSnapshot(Protocol):
    """代理状态查询接口（policy 只读，不阻塞）。"""

    @property
    def has_disk_proxy_ready(self) -> bool: ...
    @property
    def is_disk_proxy_generating(self) -> bool: ...
    @property
    def has_disk_proxy_failed(self) -> bool: ...
    @property
    def has_memory_proxy(self) -> bool: ...
    @property
    def disk_proxy_path(self) -> Optional[str]: ...


class RenderCacheStateSnapshot(Protocol):
    """渲染缓存状态查询接口（policy 只读）。"""

    @property
    def has_render_cache_ready(self) -> bool: ...
    @property
    def render_cache_path(self) -> Optional[str]: ...


@dataclass(frozen=True)
class EmptyProxyState:
    """无代理可用（用于测试 / 初始状态）。"""

    has_disk_proxy_ready: bool = False
    is_disk_proxy_generating: bool = False
    has_disk_proxy_failed: bool = False
    has_memory_proxy: bool = False
    disk_proxy_path: Optional[str] = None


@dataclass(frozen=True)
class EmptyRenderCacheState:
    """无渲染缓存。"""

    has_render_cache_ready: bool = False
    render_cache_path: Optional[str] = None


# =============================================================================
# RealtimePlaybackPolicy
# =============================================================================


class RealtimePlaybackPolicy:
    """
    实时播放源选择策略。

    用法：
        policy = RealtimePlaybackPolicy()
        decision = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="asset-1",
            source_path="/a/b.mov",
            timeline_frame=100,
            source_frame=50,
            clip_id="clip-A",
            requires_effect_cache=False,
            proxy_state=session,           # 实现 ProxyStateSnapshot
            render_cache_state=cache_mgr,  # 实现 RenderCacheStateSnapshot
        )
        # decision.allow_master_fallback 在 REALTIME 模式下必然为 False

    关键不变量（规格书 §25）：
        - REALTIME 模式 allow_master_fallback 恒为 False。
        - REALTIME 模式不解 master。
    """

    def decide_layer_source(
        self,
        *,
        mode: PlaybackMode,
        asset_id: str,
        source_path: str,
        timeline_frame: int,
        source_frame: int,
        clip_id: str,
        requires_effect_cache: bool,
        proxy_state: Optional[ProxyStateSnapshot] = None,
        render_cache_state: Optional[RenderCacheStateSnapshot] = None,
    ) -> SourceDecision:
        """
        决定单个 layer 的取帧来源。

        Returns:
            SourceDecision，所有字段可 trace。
        """
        # ---------- 非实时模式：允许 master ----------
        if mode.allows_master:
            return self._decide_non_realtime(
                mode=mode,
                source_path=source_path,
                requires_effect_cache=requires_effect_cache,
                proxy_state=proxy_state,
                render_cache_state=render_cache_state,
            )

        # ---------- 实时模式：严格禁止 master ----------
        return self._decide_realtime(
            source_path=source_path,
            requires_effect_cache=requires_effect_cache,
            proxy_state=proxy_state,
            render_cache_state=render_cache_state,
        )

    # ------------------------------------------------------------------
    # realtime 决策
    # ------------------------------------------------------------------

    def _decide_realtime(
        self,
        *,
        source_path: str,
        requires_effect_cache: bool,
        proxy_state: Optional[ProxyStateSnapshot],
        render_cache_state: Optional[RenderCacheStateSnapshot],
    ) -> SourceDecision:
        """实时模式决策：禁止 master fallback。"""

        # 1. render cache ready（复杂效果优先走缓存）
        if render_cache_state is not None and render_cache_state.has_render_cache_ready:
            return SourceDecision(
                source_kind=SourceKind.RENDER_CACHE,
                source_path=render_cache_state.render_cache_path,
                proxy_info=None,
                render_cache_info=render_cache_state,
                allow_blocking_decode=False,
                allow_master_fallback=False,  # 实时永不 master
                max_wait_ms=0,
                reason="realtime: render_cache ready (highest priority)",
            )

        # 2. disk proxy ready（专业 NLE 主路径）
        if proxy_state is not None and proxy_state.has_disk_proxy_ready:
            return SourceDecision(
                source_kind=SourceKind.DISK_PROXY,
                source_path=proxy_state.disk_proxy_path or source_path,
                proxy_info=proxy_state,
                render_cache_info=None,
                allow_blocking_decode=False,
                allow_master_fallback=False,
                max_wait_ms=0,
                reason="realtime: disk_proxy ready",
            )

        # 3. memory proxy 仅作为过渡（trace 必须标红）
        if proxy_state is not None and proxy_state.has_memory_proxy:
            logger.debug(
                "realtime: using memory_proxy as transient fallback "
                "(source=%s) — not a professional proxy path",
                source_path,
            )
            return SourceDecision(
                source_kind=SourceKind.MEMORY_PROXY,
                source_path=source_path,
                proxy_info=proxy_state,
                render_cache_info=None,
                allow_blocking_decode=False,
                allow_master_fallback=False,
                max_wait_ms=0,
                reason="realtime: memory_proxy transient (disk_proxy pending) — TRACE WARN",
            )

        # 4. disk proxy generating：显示 placeholder / rebuffer
        if proxy_state is not None and proxy_state.is_disk_proxy_generating:
            return SourceDecision(
                source_kind=SourceKind.PLACEHOLDER,
                source_path=None,
                proxy_info=proxy_state,
                render_cache_info=None,
                allow_blocking_decode=False,
                allow_master_fallback=False,
                max_wait_ms=0,
                reason="realtime: disk_proxy generating — hold/placeholder",
            )

        # 5. disk proxy failed 或 missing：placeholder + proxy required
        return SourceDecision(
            source_kind=SourceKind.PLACEHOLDER,
            source_path=None,
            proxy_info=proxy_state,
            render_cache_info=None,
            allow_blocking_decode=False,
            allow_master_fallback=False,
            max_wait_ms=0,
            reason=(
                "realtime: proxy_missing/failed — placeholder + proxy required "
                "(NOT master fallback)"
            ),
        )

    # ------------------------------------------------------------------
    # non-realtime 决策
    # ------------------------------------------------------------------

    def _decide_non_realtime(
        self,
        *,
        mode: PlaybackMode,
        source_path: str,
        requires_effect_cache: bool,
        proxy_state: Optional[ProxyStateSnapshot],
        render_cache_state: Optional[RenderCacheStateSnapshot],
    ) -> SourceDecision:
        """非实时模式决策：允许 master exact。"""

        # paused/step/export 允许阻塞等待 + master fallback
        blocking = mode.allows_blocking

        # render cache 优先（如果有复杂效果缓存）
        if (
            requires_effect_cache
            and render_cache_state is not None
            and render_cache_state.has_render_cache_ready
        ):
            return SourceDecision(
                source_kind=SourceKind.RENDER_CACHE,
                source_path=render_cache_state.render_cache_path,
                proxy_info=None,
                render_cache_info=render_cache_state,
                allow_blocking_decode=blocking,
                allow_master_fallback=True,
                max_wait_ms=2000 if blocking else 0,
                reason=f"{mode.value}: render_cache ready",
            )

        # master exact（paused/step/export 主路径）
        return SourceDecision(
            source_kind=SourceKind.MASTER,
            source_path=source_path,
            proxy_info=proxy_state,
            render_cache_info=render_cache_state,
            allow_blocking_decode=blocking,
            allow_master_fallback=True,
            max_wait_ms=2000 if blocking else 500,
            reason=f"{mode.value}: master exact (frame-accurate allowed)",
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def may_use_master(self, mode: PlaybackMode) -> bool:
        """该模式是否允许 master（规格书 §6 API）。"""
        return mode.allows_master

    def should_rebuffer(
        self,
        miss: DecodeMiss,
        *,
        consecutive_miss_count: int = 0,
        threshold: int = 3,
    ) -> bool:
        """
        是否应该进入 rebuffer 状态（P1-05：按 MissReason 类别决策）。

        - UNSUPPORTED_EFFECT：rebuffer 不会让无实时路径的效果变好 →
          永不 rebuffer，由 engine 发 playback_warning 并 hold。
        - PROXY_PENDING（代理生成中）：等待没有意义 → 立即 rebuffer。
        - 其余瞬态类别（decode/gpu busy/cache warming/back pressure/
          delegate miss）：hold 优先，连续 miss 达阈值才 rebuffer。

        注意：source_kind=PLACEHOLDER 本身不再无条件立即 rebuffer——
        PLACEHOLDER 只是"当前帧没有可用源"的呈现事实，归因必须看 reason
        （P1-05 修复：engine 伪造的 delegate miss 曾借 PLACEHOLDER
        在首次 miss 就触发 rebuffer，hold 分支实际不可达）。
        """
        reason = classify_miss_reason(miss.reason)
        if reason is MissReason.UNSUPPORTED_EFFECT:
            return False
        if consecutive_miss_count >= max(1, threshold):
            return True
        if reason is MissReason.PROXY_PENDING:
            return True
        return False


# =============================================================================
# 单例
# =============================================================================

_default_policy: Optional[RealtimePlaybackPolicy] = None
# AUD-17408：单例 check-then-act 持锁完成。"线程安全由 GIL 保证"的说法
# 不成立——GIL 只保证单条字节码原子，不保证 check-then-act 序列原子，
# 并发首调可构造多个 policy 实例。
_default_policy_lock = threading.Lock()


def get_default_policy() -> RealtimePlaybackPolicy:
    """获取默认 policy 单例（双检锁惰性初始化，线程安全）。"""
    global _default_policy
    if _default_policy is None:
        with _default_policy_lock:
            if _default_policy is None:
                _default_policy = RealtimePlaybackPolicy()
    return _default_policy
