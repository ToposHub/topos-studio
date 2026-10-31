"""
Stage G unit tests: RenderCacheAdapter + RenderCacheKey.
对应规格书 §8 / §20.1。

核心验收：
- RenderCacheKey 参数变化 → hash 变化 → 旧 cache 失效。
- get_cache_entry hash 不匹配时返回 None。
- RealtimePlaybackPolicy 优先选 RENDER_CACHE。
- invalidate_by_clip 正确失效。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    RealtimePlaybackPolicy,
    RenderCacheAdapter,
    RenderCacheEntry,
    RenderCacheKey,
    build_render_hash,
    build_effects_signature,
)
from src.features.edit.core.realtime_playback.types import PlaybackMode, SourceKind
from src.features.edit.core.effect_instance import EffectInstance


# =============================================================================
# build_render_hash
# =============================================================================


class TestBuildRenderHash:
    def test_stable(self) -> None:
        h1 = build_render_hash(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
        )
        h2 = build_render_hash(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
        )
        assert h1 == h2

    def test_speed_change_changes_hash(self) -> None:
        h1 = build_render_hash(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
        )
        h2 = build_render_hash(
            source_start=0, source_duration=100, speed=2.0,
            transform=None, crop=None, opacity=1.0,
        )
        assert h1 != h2

    def test_effect_change_changes_hash(self) -> None:
        h1 = build_render_hash(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
            effects_signature="blur:v1",
        )
        h2 = build_render_hash(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
            effects_signature="blur:v2",
        )
        assert h1 != h2

    def test_color_stack_version_change(self) -> None:
        h1 = build_render_hash(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
            color_stack_id="cs1", color_stack_version=1,
        )
        h2 = build_render_hash(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
            color_stack_id="cs1", color_stack_version=2,
        )
        assert h1 != h2

    def test_composite_graph_version_change(self) -> None:
        h1 = build_render_hash(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
            composite_graph_id="g1", composite_graph_version=1,
        )
        h2 = build_render_hash(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
            composite_graph_id="g1", composite_graph_version=2,
        )
        assert h1 != h2


# =============================================================================
# build_effects_signature
# =============================================================================


class TestBuildEffectsSignature:
    def test_empty_returns_empty_string(self) -> None:
        assert build_effects_signature(None) == ""
        assert build_effects_signature(()) == ""
        assert build_effects_signature([]) == ""

    def test_stable_for_same_effects(self) -> None:
        e = EffectInstance(effect_type="blur", params={"radius": 5.0})
        assert build_effects_signature((e,)) == build_effects_signature((e,))

    def test_different_effect_id_same_config_same_signature(self) -> None:
        """effect_id is volatile (uuid4); identical settings must hash
        equally so a freshly-recreated effect hits the same cache."""
        e1 = EffectInstance(effect_type="blur", params={"radius": 5.0})
        e2 = EffectInstance(effect_type="blur", params={"radius": 5.0})
        assert e1.effect_id != e2.effect_id
        assert build_effects_signature((e1,)) == build_effects_signature((e2,))

    def test_param_change_changes_signature(self) -> None:
        e1 = EffectInstance(effect_type="blur", params={"radius": 5.0})
        e2 = EffectInstance(effect_type="blur", params={"radius": 8.0})
        assert build_effects_signature((e1,)) != build_effects_signature((e2,))

    def test_effect_type_change_changes_signature(self) -> None:
        e1 = EffectInstance(effect_type="blur", params={"radius": 5.0})
        e2 = EffectInstance(effect_type="sharpen", params={"amount": 1.0})
        assert build_effects_signature((e1,)) != build_effects_signature((e2,))

    def test_enabled_toggle_changes_signature(self) -> None:
        e_on = EffectInstance(effect_type="blur", is_enabled=True)
        e_off = EffectInstance(effect_type="blur", is_enabled=False)
        assert build_effects_signature((e_on,)) != build_effects_signature((e_off,))

    def test_order_changes_signature(self) -> None:
        """Reordering effects changes render output, so signature must
        change — even with identical effect sets."""
        blur = EffectInstance(effect_type="blur", order=0)
        sharp = EffectInstance(effect_type="sharpen", order=1)
        sig_ab = build_effects_signature((blur, sharp))
        # Swap which runs first
        blur2 = EffectInstance(effect_type="blur", order=1)
        sharp2 = EffectInstance(effect_type="sharpen", order=0)
        sig_ba = build_effects_signature((sharp2, blur2))
        assert sig_ab != sig_ba

    def test_same_order_same_type_deterministic(self) -> None:
        """Two effects with identical order AND effect_type must produce a
        deterministic signature regardless of input tuple order — the
        sorter uses effect_id as a stable tiebreaker so the cache doesn't
        thrash on iteration-order noise."""
        a = EffectInstance(effect_type="blur", order=0, params={"radius": 5.0})
        b = EffectInstance(effect_type="blur", order=0, params={"radius": 3.0})
        # Distinct ids so they're distinguishable for sorting.
        a = a.with_effect_id("effect-a")
        b = b.with_effect_id("effect-b")
        sig_ab = build_effects_signature((a, b))
        sig_ba = build_effects_signature((b, a))  # reversed input order
        assert sig_ab == sig_ba

    def test_adding_effect_changes_signature(self) -> None:
        one = (EffectInstance(effect_type="blur", params={"radius": 5.0}),)
        two = (
            EffectInstance(effect_type="blur", params={"radius": 5.0}),
            EffectInstance(effect_type="sharpen", params={"amount": 2.0}),
        )
        assert build_effects_signature(one) != build_effects_signature(two)

    def test_keyframe_change_changes_signature(self) -> None:
        """Animated params (keyframe tracks) must be part of the
        signature — dragging a keyframe should invalidate the cache."""
        from src.shared.keyframe import InterpolationType
        e_static = EffectInstance(effect_type="blur", params={"radius": 5.0})
        e_animated = e_static.with_keyframe_added(
            "radius", frame=0, value=5.0,
            interpolation=InterpolationType.LINEAR,
            min_value=0.0, max_value=100.0,
        )
        sig_static = build_effects_signature((e_static,))
        sig_animated = build_effects_signature((e_animated,))
        assert sig_static != sig_animated

        # And moving the keyframe value changes it again
        e_moved = e_animated.with_keyframe_added(
            "radius", frame=0, value=10.0,
            interpolation=InterpolationType.LINEAR,
            min_value=0.0, max_value=100.0,
        )
        assert sig_animated != build_effects_signature((e_moved,))

    def test_single_instance_accepted(self) -> None:
        """A bare EffectInstance (not wrapped in a tuple) is accepted."""
        e = EffectInstance(effect_type="blur")
        assert build_effects_signature(e) == build_effects_signature((e,))

    def test_feeds_into_build_render_hash(self) -> None:
        """The signature is meant to be passed to build_render_hash; a
        change in it must change the overall render hash (the property
        the render cache actually invalidates on)."""
        base = dict(
            source_start=0, source_duration=100, speed=1.0,
            transform=None, crop=None, opacity=1.0,
        )
        sig1 = build_effects_signature(
            (EffectInstance(effect_type="blur", params={"radius": 5.0}),))
        sig2 = build_effects_signature(
            (EffectInstance(effect_type="blur", params={"radius": 8.0}),))
        h1 = build_render_hash(effects_signature=sig1, **base)
        h2 = build_render_hash(effects_signature=sig2, **base)
        assert h1 != h2


# =============================================================================
# RenderCacheKey
# =============================================================================


class TestRenderCacheKey:
    def test_to_hash_stable(self) -> None:
        k = RenderCacheKey(
            timeline_id="t", clip_id="c", asset_id="a", source_path="/x",
            frame_rate_num=25, frame_rate_den=1, width=1920, height=1080,
            render_hash="abc123",
        )
        assert k.to_hash() == k.to_hash()
        assert len(k.to_hash()) == 32

    def test_different_render_hash_different_key_hash(self) -> None:
        k1 = RenderCacheKey(
            timeline_id="t", clip_id="c", asset_id="a", source_path="/x",
            frame_rate_num=25, frame_rate_den=1, width=1920, height=1080,
            render_hash="abc",
        )
        k2 = RenderCacheKey(
            timeline_id="t", clip_id="c", asset_id="a", source_path="/x",
            frame_rate_num=25, frame_rate_den=1, width=1920, height=1080,
            render_hash="xyz",
        )
        assert k1.to_hash() != k2.to_hash()


# =============================================================================
# RenderCacheAdapter
# =============================================================================


@pytest.fixture
def adapter() -> RenderCacheAdapter:
    return RenderCacheAdapter()


def _make_key(
    clip_id: str = "c1",
    render_hash: str = "h1",
    width: int = 1920,
    height: int = 1080,
) -> RenderCacheKey:
    return RenderCacheKey(
        timeline_id="t", clip_id=clip_id, asset_id="a", source_path="/x",
        frame_rate_num=25, frame_rate_den=1, width=width, height=height,
        render_hash=render_hash,
    )


def _make_entry(
    clip_id: str = "c1",
    status: str = "valid",
    render_hash: str = "h1",
    cache_path: str | None = "/cache/c.mov",
) -> RenderCacheEntry:
    return RenderCacheEntry(
        key=_make_key(clip_id, render_hash),
        status=status,
        cache_path=cache_path,
        width=1920, height=1080,
        start_frame=0, duration_frames=100,
    )


class TestRenderCacheAdapter:
    def test_empty_initially(self, adapter: RenderCacheAdapter) -> None:
        assert adapter.has_render_cache_ready is False
        assert adapter.render_cache_path is None
        assert adapter.get_cache_entry(_make_key()) is None

    def test_upsert_and_query_ready(self, adapter: RenderCacheAdapter) -> None:
        adapter.upsert_entry(_make_entry(status="valid"))
        assert adapter.has_render_cache_ready is True
        assert adapter.render_cache_path == "/cache/c.mov"
        entry = adapter.get_cache_entry(_make_key())
        assert entry is not None
        assert entry.is_ready

    def test_hash_mismatch_returns_none(self, adapter: RenderCacheAdapter) -> None:
        """规格书 §8：参数变化（render_hash 变）→ 旧 cache 失效。"""
        adapter.upsert_entry(_make_entry(render_hash="h1", status="valid"))
        # 用不同 hash 查询 → None
        entry = adapter.get_cache_entry(_make_key(render_hash="h2"))
        assert entry is None

    def test_resolution_mismatch_returns_none(self, adapter: RenderCacheAdapter) -> None:
        adapter.upsert_entry(_make_entry(status="valid"))
        entry = adapter.get_cache_entry(_make_key(width=3840, height=2160))
        assert entry is None

    def test_stale_not_ready(self, adapter: RenderCacheAdapter) -> None:
        adapter.upsert_entry(_make_entry(status="stale"))
        assert adapter.has_render_cache_ready is False

    def test_rendering_not_ready(self, adapter: RenderCacheAdapter) -> None:
        adapter.upsert_entry(_make_entry(status="rendering", cache_path=None))
        assert adapter.has_render_cache_ready is False

    def test_invalidate_by_clip(self, adapter: RenderCacheAdapter) -> None:
        adapter.upsert_entry(_make_entry(status="valid"))
        adapter.invalidate_by_clip("c1", "params changed")
        entry = adapter.get_cache_entry_for_clip("c1")
        assert entry is not None
        assert entry.status == "stale"

    def test_remove_by_clip(self, adapter: RenderCacheAdapter) -> None:
        adapter.upsert_entry(_make_entry())
        adapter.remove_by_clip("c1")
        assert adapter.get_cache_entry_for_clip("c1") is None

    def test_is_cache_ready(self, adapter: RenderCacheAdapter) -> None:
        adapter.upsert_entry(_make_entry(status="valid"))
        assert adapter.is_cache_ready("c1") is True
        assert adapter.is_cache_ready("c1", _make_key(render_hash="wrong")) is False

    def test_register_from_manifest(self, adapter: RenderCacheAdapter) -> None:
        """适配现有 RenderCacheManifest。

        AUD-17410（锁定旧行为协议更新）：register_manifest 不再静默回退
        硬编码 NTSC 帧率/1080p 默认值，manifest 必须携带真实帧率与尺寸。
        """
        manifest = MagicMock()
        manifest.timeline_id = "t1"
        manifest.frame_rate_num = 25
        manifest.frame_rate_den = 1
        manifest.width = 1920
        manifest.height = 1080
        manifest.path = "/cache/m.mov"
        manifest.status = "valid"
        manifest.start_frame = 0
        manifest.duration_frames = 50
        entry = adapter.register_manifest("c1", manifest, render_hash="rh1")
        assert adapter.has_render_cache_ready is True
        # key 透传真实 fps/尺寸（旧行为恒为 24000/1001 + 1920x1080 默认）
        assert entry.key.frame_rate_num == 25
        assert entry.key.frame_rate_den == 1
        assert (entry.key.width, entry.key.height) == (1920, 1080)

    def test_register_manifest_without_frame_rate_rejected(
        self, adapter: RenderCacheAdapter,
    ) -> None:
        """AUD-17410：manifest 缺帧率 → 显式报错而非静默默认。"""
        manifest = MagicMock()
        manifest.timeline_id = "t1"
        manifest.width = 1920
        manifest.height = 1080
        with pytest.raises(ValueError):
            adapter.register_manifest("c1", manifest, render_hash="rh1")

    def test_stats(self, adapter: RenderCacheAdapter) -> None:
        adapter.upsert_entry(_make_entry("c1", status="valid"))
        adapter.upsert_entry(_make_entry("c2", status="rendering", cache_path=None))
        s = adapter.stats()
        assert s["total"] == 2
        assert s.get("valid", 0) == 1
        assert s.get("rendering", 0) == 1


# =============================================================================
# Policy 集成：render_cache 优先
# =============================================================================


class _CacheReady:
    has_render_cache_ready = True
    render_cache_path = "/cache/c.mov"


class _ProxyReady:
    has_disk_proxy_ready = True
    is_disk_proxy_generating = False
    has_disk_proxy_failed = False
    has_memory_proxy = False
    disk_proxy_path = "/proxy/p.mov"


class TestPolicyRenderCachePriority:
    """规格书 §8.3：cache ready 后 RealtimePlaybackPolicy 优先选 RENDER_CACHE。"""

    def test_cache_beats_proxy_in_realtime(self) -> None:
        policy = RealtimePlaybackPolicy()
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=True,
            proxy_state=_ProxyReady(),
            render_cache_state=_CacheReady(),
        )
        assert d.source_kind is SourceKind.RENDER_CACHE
        assert d.allow_master_fallback is False

    def test_cache_no_master_in_realtime(self) -> None:
        policy = RealtimePlaybackPolicy()
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=True,
            render_cache_state=_CacheReady(),
        )
        assert d.source_kind is SourceKind.RENDER_CACHE
        assert d.allow_master_fallback is False
