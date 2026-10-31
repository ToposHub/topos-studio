"""
Batch 5 tests: SourceResolver proxy/cache source 解析。
对应缺陷计划 RP-P0-07/09 / §3.3。

核心验收：
- realtime 模式 proxy ready → DISK_PROXY，不解 master。
- realtime 模式 proxy missing → PLACEHOLDER + 触发后台生成。
- realtime 模式 render cache ready → RENDER_CACHE（优先）。
- 拖入新素材 ensure_proxy 只触发一次（去重）。
- paused 模式允许 master。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    LayerSourceQuery,
    PlaybackMode,
    RenderCacheAdapter,
    RenderCacheEntry,
    RenderCacheKey,
    SourceDecision,
    SourceKind,
    SourceResolver,
)


# =============================================================================
# Helpers
# =============================================================================


class _MockProxyEntry:
    """模拟 ProxyManifestEntry。"""
    def __init__(self, ready=False, generating=False, failed=False, path=None):
        self.is_ready = ready
        self.is_generating = generating
        self.is_failed = failed
        self.proxy_path = path


class _MockProxyManager:
    """模拟 AssetProxyManager。"""
    def __init__(self, entries=None):
        self._entries = entries or {}
        self.ensure_calls = []
    def get_proxy_entry(self, source_path):
        return self._entries.get(source_path)
    def ensure_proxy_async(self, *, source_path, asset_id, config=None):
        self.ensure_calls.append((source_path, asset_id))


def _make_query(clip_id="c1", source_path="/x.mov", requires_effect_cache=False) -> LayerSourceQuery:
    return LayerSourceQuery(
        clip_id=clip_id, asset_id="a", source_path=source_path,
        source_frame=10, timeline_frame=10,
        requires_effect_cache=requires_effect_cache,
    )


# =============================================================================
# Realtime source 优先级
# =============================================================================


class TestRealtimeSourcePriority:
    """realtime 优先级 RENDER_CACHE > DISK_PROXY > PLACEHOLDER。"""

    def test_disk_proxy_ready(self) -> None:
        pm = _MockProxyManager({"/x.mov": _MockProxyEntry(ready=True, path="/proxy/x.mov")})
        r = SourceResolver(proxy_manager=pm)
        d = r.resolve_single(mode=PlaybackMode.REALTIME, query=_make_query())
        assert d.source_kind is SourceKind.DISK_PROXY
        assert d.allow_master_fallback is False

    def test_render_cache_beats_disk_proxy(self) -> None:
        pm = _MockProxyManager({"/x.mov": _MockProxyEntry(ready=True, path="/proxy/x.mov")})
        cache = RenderCacheAdapter()
        cache.upsert_entry(RenderCacheEntry(
            key=RenderCacheKey(timeline_id="t", clip_id="c1", asset_id="a",
                               source_path="/x.mov", frame_rate_num=25, frame_rate_den=1,
                               width=1920, height=1080, render_hash="rh"),
            status="valid", cache_path="/cache/c.mov",
            width=1920, height=1080, start_frame=0, duration_frames=100,
        ))
        r = SourceResolver(proxy_manager=pm, render_cache=cache)
        d = r.resolve_single(mode=PlaybackMode.REALTIME,
                             query=_make_query(requires_effect_cache=True))
        assert d.source_kind is SourceKind.RENDER_CACHE

    def test_proxy_missing_placeholder(self) -> None:
        pm = _MockProxyManager({})
        r = SourceResolver(proxy_manager=pm)
        d = r.resolve_single(mode=PlaybackMode.REALTIME, query=_make_query())
        assert d.source_kind is SourceKind.PLACEHOLDER
        assert d.allow_master_fallback is False

    def test_proxy_generating_placeholder(self) -> None:
        pm = _MockProxyManager({"/x.mov": _MockProxyEntry(generating=True)})
        r = SourceResolver(proxy_manager=pm)
        d = r.resolve_single(mode=PlaybackMode.REALTIME, query=_make_query())
        assert d.source_kind is SourceKind.PLACEHOLDER

    def test_realtime_never_master(self) -> None:
        """realtime 模式任何状态都不解 master。"""
        states = [
            {},  # no proxy
            {"/x.mov": _MockProxyEntry(ready=True, path="/p")},
            {"/x.mov": _MockProxyEntry(generating=True)},
            {"/x.mov": _MockProxyEntry(failed=True)},
        ]
        for entries in states:
            pm = _MockProxyManager(entries)
            r = SourceResolver(proxy_manager=pm)
            d = r.resolve_single(mode=PlaybackMode.REALTIME, query=_make_query())
            assert d.source_kind is not SourceKind.MASTER
            assert d.allow_master_fallback is False


# =============================================================================
# Paused 模式允许 master
# =============================================================================


class TestPausedAllowsMaster:
    def test_paused_allows_master(self) -> None:
        pm = _MockProxyManager({})
        r = SourceResolver(proxy_manager=pm)
        d = r.resolve_single(mode=PlaybackMode.PAUSED, query=_make_query())
        assert d.allow_master_fallback is True

    def test_export_allows_master(self) -> None:
        pm = _MockProxyManager({})
        r = SourceResolver(proxy_manager=pm)
        d = r.resolve_single(mode=PlaybackMode.EXPORT, query=_make_query())
        assert d.allow_master_fallback is True


# =============================================================================
# ensure_proxy 去重
# =============================================================================


class TestEnsureProxyDedup:
    """缺陷计划 RP-P0-09：拖入新素材后立即创建 proxy job，去重。"""

    def test_ensure_triggers_once_per_source(self) -> None:
        pm = _MockProxyManager({})
        r = SourceResolver(proxy_manager=pm)
        q = _make_query(source_path="/x.mov")
        r.ensure_proxy(q)
        r.ensure_proxy(q)
        r.ensure_proxy(q)
        assert len(pm.ensure_calls) == 1

    def test_ensure_skipped_when_ready(self) -> None:
        pm = _MockProxyManager({"/x.mov": _MockProxyEntry(ready=True, path="/p")})
        r = SourceResolver(proxy_manager=pm)
        r.ensure_proxy(_make_query(source_path="/x.mov"))
        assert len(pm.ensure_calls) == 0

    def test_ensure_skipped_when_generating(self) -> None:
        pm = _MockProxyManager({"/x.mov": _MockProxyEntry(generating=True)})
        r = SourceResolver(proxy_manager=pm)
        r.ensure_proxy(_make_query(source_path="/x.mov"))
        assert len(pm.ensure_calls) == 0

    def test_different_sources_each_ensure(self) -> None:
        pm = _MockProxyManager({})
        r = SourceResolver(proxy_manager=pm)
        r.ensure_proxy(_make_query(source_path="/a.mov"))
        r.ensure_proxy(_make_query(source_path="/b.mov"))
        assert len(pm.ensure_calls) == 2

    def test_invalidate_proxy_cache_re_allows_ensure(self) -> None:
        pm = _MockProxyManager({})
        r = SourceResolver(proxy_manager=pm)
        r.ensure_proxy(_make_query(source_path="/x.mov"))
        r.invalidate_proxy_cache("/x.mov")
        r.ensure_proxy(_make_query(source_path="/x.mov"))
        assert len(pm.ensure_calls) == 2


# =============================================================================
# 多 layer resolve
# =============================================================================


class TestResolveLayers:
    def test_multi_layer(self) -> None:
        pm = _MockProxyManager({
            "/a.mov": _MockProxyEntry(ready=True, path="/pa"),
        })
        r = SourceResolver(proxy_manager=pm)
        queries = [
            _make_query(clip_id="c1", source_path="/a.mov"),
            _make_query(clip_id="c2", source_path="/b.mov"),  # 无 proxy
        ]
        decisions = r.resolve_layers(mode=PlaybackMode.REALTIME, queries=queries)
        assert len(decisions) == 2
        assert decisions[0].source_kind is SourceKind.DISK_PROXY
        assert decisions[1].source_kind is SourceKind.PLACEHOLDER

    def test_empty_queries(self) -> None:
        r = SourceResolver()
        decisions = r.resolve_layers(mode=PlaybackMode.REALTIME, queries=[])
        assert decisions == []
