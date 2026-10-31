"""
Stage C1 unit tests: RealtimePlaybackPolicy.
对应规格书 §6 / §20.1 test_realtime_playback_policy.py。

核心验收：REALTIME 模式下 allow_master_fallback 恒为 False。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    DecodeMiss,
    EmptyProxyState,
    EmptyRenderCacheState,
    MissReason,
    RealtimePlaybackPolicy,
    classify_miss_reason,
)
from src.features.edit.core.realtime_playback.types import PlaybackMode, SourceKind


# =============================================================================
# Snapshot helpers
# =============================================================================


@pytest.fixture
def policy() -> RealtimePlaybackPolicy:
    return RealtimePlaybackPolicy()


class _ProxyReady:
    """disk proxy ready 状态。"""
    has_disk_proxy_ready = True
    is_disk_proxy_generating = False
    has_disk_proxy_failed = False
    has_memory_proxy = False
    disk_proxy_path = "/cache/proxy/b.mov"


class _ProxyGenerating:
    has_disk_proxy_ready = False
    is_disk_proxy_generating = True
    has_disk_proxy_failed = False
    has_memory_proxy = False  # 生成中且无 memory proxy → placeholder
    disk_proxy_path = None


class _ProxyMissing:
    has_disk_proxy_ready = False
    is_disk_proxy_generating = False
    has_disk_proxy_failed = False
    has_memory_proxy = False
    disk_proxy_path = None


class _ProxyFailed:
    has_disk_proxy_ready = False
    is_disk_proxy_generating = False
    has_disk_proxy_failed = True
    has_memory_proxy = False
    disk_proxy_path = None


class _MemoryOnly:
    has_disk_proxy_ready = False
    is_disk_proxy_generating = False
    has_disk_proxy_failed = False
    has_memory_proxy = True
    disk_proxy_path = None


class _CacheReady:
    has_render_cache_ready = True
    render_cache_path = "/cache/render/c1.mov"


# =============================================================================
# REALTIME 模式：禁止 master fallback
# =============================================================================


class TestRealtimeModeMasterForbidden:
    """规格书 §6：播放状态下 allow_master_fallback=False，master_requests=0。"""

    def test_no_source_still_no_master(self, policy: RealtimePlaybackPolicy) -> None:
        """没有任何代理/缓存时，realtime 也不解 master。"""
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
        )
        assert d.allow_master_fallback is False
        assert d.source_kind is SourceKind.PLACEHOLDER
        assert d.source_kind is not SourceKind.MASTER

    def test_disk_proxy_ready_no_master(self, policy: RealtimePlaybackPolicy) -> None:
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
            proxy_state=_ProxyReady(),
        )
        assert d.allow_master_fallback is False
        assert d.source_kind is SourceKind.DISK_PROXY
        assert d.source_path == "/cache/proxy/b.mov"

    def test_memory_proxy_no_master(self, policy: RealtimePlaybackPolicy) -> None:
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
            proxy_state=_MemoryOnly(),
        )
        assert d.allow_master_fallback is False
        assert d.source_kind is SourceKind.MEMORY_PROXY
        # trace 必须标红（warning）
        assert "WARN" in d.reason or "transient" in d.reason

    def test_proxy_generating_placeholder_no_master(self, policy: RealtimePlaybackPolicy) -> None:
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
            proxy_state=_ProxyGenerating(),
        )
        assert d.allow_master_fallback is False
        assert d.source_kind is SourceKind.PLACEHOLDER

    def test_proxy_failed_placeholder_no_master(self, policy: RealtimePlaybackPolicy) -> None:
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
            proxy_state=_ProxyFailed(),
        )
        assert d.allow_master_fallback is False
        assert d.source_kind is SourceKind.PLACEHOLDER

    @pytest.mark.parametrize("proxy_state", [
        _ProxyReady(), _ProxyGenerating(), _ProxyFailed(),
        _MemoryOnly(), _ProxyMissing(), None,
    ])
    def test_master_never_allowed_in_realtime(
        self, policy: RealtimePlaybackPolicy, proxy_state
    ) -> None:
        """任何 proxy 状态下，realtime 模式都禁止 master。"""
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
            proxy_state=proxy_state,
        )
        assert d.source_kind is not SourceKind.MASTER
        assert d.allow_master_fallback is False


# =============================================================================
# 优先级：render_cache > disk_proxy > memory_proxy > placeholder
# =============================================================================


class TestRealtimePriority:
    def test_render_cache_beats_disk_proxy(self, policy: RealtimePlaybackPolicy) -> None:
        """render cache 优先级最高，即使 disk proxy ready。"""
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=True,
            proxy_state=_ProxyReady(),
            render_cache_state=_CacheReady(),
        )
        assert d.source_kind is SourceKind.RENDER_CACHE

    def test_disk_proxy_beats_memory_proxy(self, policy: RealtimePlaybackPolicy) -> None:
        d_ready = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
            proxy_state=_ProxyReady(),
        )
        assert d_ready.source_kind is SourceKind.DISK_PROXY

    def test_memory_proxy_when_no_disk(self, policy: RealtimePlaybackPolicy) -> None:
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
            proxy_state=_MemoryOnly(),
        )
        assert d.source_kind is SourceKind.MEMORY_PROXY


# =============================================================================
# 非实时模式：允许 master
# =============================================================================


class TestNonRealtimeModeAllowsMaster:
    @pytest.mark.parametrize("mode", [
        PlaybackMode.PAUSED, PlaybackMode.STEP, PlaybackMode.EXPORT,
    ])
    def test_non_realtime_allows_master(
        self, policy: RealtimePlaybackPolicy, mode: PlaybackMode
    ) -> None:
        d = policy.decide_layer_source(
            mode=mode,
            asset_id="a", source_path="/master.mov", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
        )
        assert d.allow_master_fallback is True
        assert d.source_kind is SourceKind.MASTER
        assert d.source_path == "/master.mov"

    def test_paused_blocking_allowed(self, policy: RealtimePlaybackPolicy) -> None:
        d = policy.decide_layer_source(
            mode=PlaybackMode.PAUSED,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
        )
        assert d.allow_blocking_decode is True
        assert d.max_wait_ms > 0

    def test_scrub_allows_master_short_timeout(self, policy: RealtimePlaybackPolicy) -> None:
        d = policy.decide_layer_source(
            mode=PlaybackMode.SCRUB,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=False,
        )
        assert d.allow_master_fallback is True
        # scrub 允许 master 但不阻塞（短 timeout）
        assert d.allow_blocking_decode is False

    def test_non_realtime_render_cache_priority(
        self, policy: RealtimePlaybackPolicy
    ) -> None:
        """非实时模式 + 复杂效果 + cache ready → render_cache（仍允许 master fallback）。"""
        d = policy.decide_layer_source(
            mode=PlaybackMode.PAUSED,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=True,
            render_cache_state=_CacheReady(),
        )
        assert d.source_kind is SourceKind.RENDER_CACHE
        assert d.allow_master_fallback is True


# =============================================================================
# may_use_master / should_rebuffer
# =============================================================================


class TestPolicyHelpers:
    def test_may_use_master(self, policy: RealtimePlaybackPolicy) -> None:
        assert policy.may_use_master(PlaybackMode.REALTIME) is False
        assert policy.may_use_master(PlaybackMode.PAUSED) is True
        assert policy.may_use_master(PlaybackMode.EXPORT) is True

    def test_should_rebuffer_on_threshold(self, policy: RealtimePlaybackPolicy) -> None:
        # 用 non-placeholder miss 测阈值（placeholder miss 会立即 rebuffer）
        miss = DecodeMiss(
            timeline_frame=5, clip_id="c", asset_id="a", source_frame=5,
            source_kind=SourceKind.MEMORY_PROXY, reason="not ready",
        )
        assert policy.should_rebuffer(miss, consecutive_miss_count=2, threshold=3) is False
        assert policy.should_rebuffer(miss, consecutive_miss_count=3, threshold=3) is True

    def test_should_rebuffer_on_placeholder(self, policy: RealtimePlaybackPolicy) -> None:
        miss = DecodeMiss(
            timeline_frame=5, clip_id="c", asset_id="a", source_frame=5,
            source_kind=SourceKind.PLACEHOLDER, reason="proxy generating",
        )
        # placeholder miss 立即 rebuffer
        assert policy.should_rebuffer(miss, consecutive_miss_count=0) is True


# =============================================================================
# P1-05: MissReason 词表 / 归一化 / 按类别决策
# =============================================================================


class TestClassifyMissReason:
    """classify_miss_reason：已知生产方字符串 → 归一类别。"""

    @pytest.mark.parametrize("reason,expected", [
        ("delegate_miss", MissReason.DELEGATE_MISS),
        ("", MissReason.DELEGATE_MISS),
        (None, MissReason.DELEGATE_MISS),
        ("proxy generating", MissReason.PROXY_PENDING),
        ("proxy_pending", MissReason.PROXY_PENDING),
        ("clip-A: stream not ready", MissReason.CACHE_WARMING),
        ("clip-B: source_frame 42 not ready", MissReason.CACHE_WARMING),
        ("no layers configured", MissReason.CACHE_WARMING),
        ("gpu lane saturated", MissReason.GPU_BUSY),
        ("gpu_result_drop_playback_stale", MissReason.GPU_BUSY),
        ("gpu_result_drop_realtime_no_present", MissReason.GPU_BUSY),
        ("unsupported_effect", MissReason.UNSUPPORTED_EFFECT),
        ("effect too heavy for realtime", MissReason.UNSUPPORTED_EFFECT),
        ("back_pressure", MissReason.BACK_PRESSURE),
        ("budget drop", MissReason.BACK_PRESSURE),
        ("decode failed", MissReason.DECODE_MISS),
        ("vt open failed", MissReason.DECODE_MISS),
        ("deadline expired", MissReason.DECODE_MISS),
    ])
    def test_known_producer_strings(self, reason, expected) -> None:
        assert classify_miss_reason(reason) is expected

    def test_unknown_string_holds_not_rebuffers(self) -> None:
        """未知归因必须落回瞬态（hold-first），绝不默认立即 rebuffer。"""
        assert classify_miss_reason("something new") is MissReason.DELEGATE_MISS


class TestMissReasonSemantics:
    """P1-05: should_rebuffer 按类别决策（hold / 立即 rebuffer / 永不 rebuffer）。"""

    def _miss(self, reason: str, source_kind: SourceKind = SourceKind.NONE) -> DecodeMiss:
        return DecodeMiss(
            timeline_frame=1, clip_id="c", asset_id="a", source_frame=1,
            source_kind=source_kind, reason=reason,
        )

    @pytest.mark.parametrize("reason", [
        "delegate_miss", "decode failed", "gpu busy", "cache warming",
        "back_pressure",
    ])
    def test_transient_reasons_hold_until_threshold(
        self, policy: RealtimePlaybackPolicy, reason: str,
    ) -> None:
        miss = self._miss(reason)
        assert policy.should_rebuffer(miss, consecutive_miss_count=1, threshold=3) is False
        assert policy.should_rebuffer(miss, consecutive_miss_count=2, threshold=3) is False
        assert policy.should_rebuffer(miss, consecutive_miss_count=3, threshold=3) is True

    def test_proxy_pending_rebuffers_immediately(
        self, policy: RealtimePlaybackPolicy,
    ) -> None:
        miss = self._miss("proxy generating")
        assert policy.should_rebuffer(miss, consecutive_miss_count=0, threshold=3) is True

    @pytest.mark.parametrize("count", [1, 5, 50])
    def test_unsupported_effect_never_rebuffers(
        self, policy: RealtimePlaybackPolicy, count: int,
    ) -> None:
        miss = self._miss("unsupported_effect")
        assert policy.should_rebuffer(miss, consecutive_miss_count=count) is False

    def test_placeholder_without_proxy_reason_holds_first(
        self, policy: RealtimePlaybackPolicy,
    ) -> None:
        """P1-05 修复契约：PLACEHOLDER 本身不再无条件立即 rebuffer。

        PLACEHOLDER 只是"当前帧没有可用源"的呈现事实；归因必须看 reason。
        引擎伪造的 delegate miss（P1-05 之前 source_kind=PLACEHOLDER）曾
        在首次 miss 就触发 rebuffer，hold 分支不可达。
        """
        miss = self._miss("delegate_miss", source_kind=SourceKind.PLACEHOLDER)
        assert policy.should_rebuffer(miss, consecutive_miss_count=1, threshold=3) is False
        assert policy.should_rebuffer(miss, consecutive_miss_count=3, threshold=3) is True


# =============================================================================
# 决策可 trace
# =============================================================================


class TestDecisionTraceability:
    def test_every_decision_has_reason(self, policy: RealtimePlaybackPolicy) -> None:
        for mode in PlaybackMode:
            for proxy in [_ProxyReady(), _ProxyGenerating(), _MemoryOnly(), None]:
                d = policy.decide_layer_source(
                    mode=mode,
                    asset_id="a", source_path="/x", timeline_frame=0,
                    source_frame=0, clip_id="c", requires_effect_cache=False,
                    proxy_state=proxy,
                )
                assert isinstance(d.reason, str) and len(d.reason) > 0, (
                    f"mode={mode} proxy={proxy}: reason empty"
                )
