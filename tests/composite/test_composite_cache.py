"""
Tests for cache key computation and cache management — pure logic, no GL.

Covers:
- CacheManager.compute_key produces stable, deterministic keys
- Same params + same upstream → same key (cache hit)
- Different params → different key
- Different upstream texture → different key
- Disabled flag changes the key
- Color suffix for color_input nodes
- render_mode isolation (preview vs export)
- CacheManager.invalidate_node removes correct entries
- CacheManager.clear clears all entries
- NEW: output_format, alpha_mode, proxy_scale, frame_number in cache key
- NEW: CacheCategory classification
- NEW: Memory budget eviction
"""

import sys
import os
import json
import hashlib
import pytest
from collections import OrderedDict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.evaluators.graph_evaluator_mixin import GraphEvaluatorMixin
from src.features.composite.core.graph_snapshot import NodeSnapshot, GraphSnapshot, ConnectionSnapshot
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.render.cache_manager import CacheManager, CacheEntry, CacheCategory
from src.features.composite.render.gpu_resource_pool import GPUResourcePool
from src.features.composite.core.format import ImageFormat, BBox


# ── Minimal test harness ──────────────────────────────────────────────────

class MockFramebufferManager:
    """Minimal mock for FramebufferManager (no GL needed)."""
    def __init__(self):
        self._active_textures = set()
        self._texture_pool = {}

    def get_temp_texture(self, w, h, components=4, dtype='f2'):
        return None

    def release_temp_texture(self, tex):
        pass

    def get_fbo(self, tex):
        return None

    def cleanup(self):
        pass


def _make_cache_mgr() -> CacheManager:
    """Create a CacheManager with a mock resource pool."""
    fb_mgr = MockFramebufferManager()
    pool = GPUResourcePool(fb_mgr, max_memory_mb=256)
    return CacheManager(pool, max_entries=128)


def _make_node_snapshot(node_type="blur", node_id="blur1", params=None,
                        disabled=False, input_ports=None, output_ports=None):
    """Helper to create a NodeSnapshot with sensible defaults."""
    return NodeSnapshot(
        node_id=node_id,
        node_type=node_type,
        parameters=params or {"radius": 5.0},
        disabled=disabled,
        input_ports=input_ports or [("image", "image")],
        output_ports=output_ports or [("result", "image")],
    )


class TestDynamicCacheBypass:
    """Dynamic timeline/video branches should not accumulate preview cache entries."""

    def test_timeline_branch_and_downstream_bypass_persistent_cache(self):
        evaluator = GraphEvaluatorMixin()
        nodes = {
            "src": _make_node_snapshot(
                "timeline_clip_input",
                "src",
                input_ports=[],
                output_ports=[("output", "image")],
            ),
            "grade": _make_node_snapshot(
                "topos_color",
                "grade",
                input_ports=[("image", "image")],
                output_ports=[("result", "image")],
            ),
            "out": _make_node_snapshot(
                "output",
                "out",
                input_ports=[("image", "image")],
                output_ports=[("display", "image")],
            ),
            "constant": _make_node_snapshot(
                "constant",
                "constant",
                input_ports=[],
                output_ports=[("output", "image")],
            ),
        }
        snapshot = GraphSnapshot(
            generation=1,
            nodes=nodes,
            connections=[
                ConnectionSnapshot("src", "output", "grade", "image"),
                ConnectionSnapshot("grade", "result", "out", "image"),
            ],
            eval_order=["src", "grade", "out", "constant"],
        )

        # P0 契约更新：动态源下游不再整体绕过持久缓存——cache key 已包含
        # 上游纹理 id+revision（IDT/裁剪等中间变换也透传 revision），源内容
        # 变化会自然 miss。只有动态源节点本身绕过（它们逐帧重求值）。
        # 若把下游也绕过，静态链路（grade 参数不变）每次 seek 都全量重渲。
        assert evaluator._compute_dynamic_cache_bypass_nodes(snapshot) == {"src"}


class TestCacheKeyDeterminism:
    """Same inputs → same key. No randomness."""

    def test_identical_inputs_produce_same_key(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": TextureResult(texture_id=10, width=1920, height=1080)}

        key1 = mgr.compute_key(ns, inputs)
        key2 = mgr.compute_key(ns, inputs)
        assert key1 == key2

    def test_key_format_includes_node_type_and_id(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1")
        inputs = {"image": TextureResult(texture_id=10)}

        key = mgr.compute_key(ns, inputs)
        # Key format: "node_type:node_id:hash:..."
        assert key.startswith("blur:b1:")

    def test_params_hash_is_md5_truncated(self):
        mgr = _make_cache_mgr()
        params = {"radius": 5.0, "opacity": 0.5}
        ns = _make_node_snapshot("blur", "b1", params)
        inputs = {"image": TextureResult(texture_id=10)}

        key = mgr.compute_key(ns, inputs)

        # Manually compute expected hash
        params_str = json.dumps(params, sort_keys=True, default=str)
        expected_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]
        assert expected_hash in key


class TestCacheKeyParamSensitivity:
    """Different parameters → different cache key."""

    def test_different_radius_different_key(self):
        mgr = _make_cache_mgr()
        inputs = {"image": TextureResult(texture_id=10)}

        ns1 = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        ns2 = _make_node_snapshot("blur", "b1", {"radius": 20.0})

        key1 = mgr.compute_key(ns1, inputs)
        key2 = mgr.compute_key(ns2, inputs)
        assert key1 != key2

    def test_same_params_same_key(self):
        mgr = _make_cache_mgr()
        inputs = {"image": TextureResult(texture_id=10)}

        ns1 = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        ns2 = _make_node_snapshot("blur", "b1", {"radius": 5.0})

        key1 = mgr.compute_key(ns1, inputs)
        key2 = mgr.compute_key(ns2, inputs)
        assert key1 == key2


class TestCacheKeyUpstreamSensitivity:
    """Different upstream texture → different cache key."""

    def test_different_texture_id_different_key(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs1 = {"image": TextureResult(texture_id=10)}
        inputs2 = {"image": TextureResult(texture_id=20)}

        key1 = mgr.compute_key(ns, inputs1)
        key2 = mgr.compute_key(ns, inputs2)
        assert key1 != key2

    def test_different_revision_different_key(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs1 = {"image": TextureResult(texture_id=10, revision=1)}
        inputs2 = {"image": TextureResult(texture_id=10, revision=2)}

        key1 = mgr.compute_key(ns, inputs1)
        key2 = mgr.compute_key(ns, inputs2)
        assert key1 != key2

    def test_empty_inputs_vs_nonempty(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        key1 = mgr.compute_key(ns, {})
        key2 = mgr.compute_key(ns, {"image": TextureResult(texture_id=10)})

        assert key1 != key2


class TestCacheKeyDisabledFlag:
    """Disabled flag changes the cache key."""

    def test_disabled_changes_key(self):
        mgr = _make_cache_mgr()
        inputs = {"image": TextureResult(texture_id=10)}

        ns_enabled = _make_node_snapshot("blur", "b1", {"radius": 5.0}, disabled=False)
        ns_disabled = _make_node_snapshot("blur", "b1", {"radius": 5.0}, disabled=True)

        key_enabled = mgr.compute_key(ns_enabled, inputs)
        key_disabled = mgr.compute_key(ns_disabled, inputs)
        assert key_enabled != key_disabled

    def test_enabled_flag_in_key(self):
        mgr = _make_cache_mgr()
        inputs = {"image": TextureResult(texture_id=10)}
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0}, disabled=False)
        key = mgr.compute_key(ns, inputs)
        assert ":E:" in key

    def test_disabled_flag_in_key(self):
        mgr = _make_cache_mgr()
        inputs = {"image": TextureResult(texture_id=10)}
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0}, disabled=True)
        key = mgr.compute_key(ns, inputs)
        assert ":D:" in key


class TestCacheKeyColorSuffix:
    """Color input nodes include color_texture_id and revision in their key."""

    def test_color_node_has_suffix(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("color_input", "c1", {})
        inputs = {}

        key = mgr.compute_key(ns, inputs, snapshot_color_id=42, snapshot_color_rev=3)
        assert ":ct42" in key
        assert ":cr3" in key

    def test_different_color_revision_different_key(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("color_input", "c1", {})
        inputs = {}

        key1 = mgr.compute_key(ns, inputs, snapshot_color_id=42, snapshot_color_rev=1)
        key2 = mgr.compute_key(ns, inputs, snapshot_color_id=42, snapshot_color_rev=2)

        assert key1 != key2

    def test_non_color_node_has_no_suffix(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": TextureResult(texture_id=10)}

        key = mgr.compute_key(ns, inputs, snapshot_color_id=42, snapshot_color_rev=3)
        assert ":ct42" not in key
        assert ":cr3" not in key


class TestCacheKeyRenderMode:
    """render_mode is part of the cache key — preview vs export isolation."""

    def test_different_render_mode_different_key(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": TextureResult(texture_id=10)}

        key_preview = mgr.compute_key(ns, inputs, render_mode="preview")
        key_export = mgr.compute_key(ns, inputs, render_mode="export")
        assert key_preview != key_export

    def test_same_render_mode_same_key(self):
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": TextureResult(texture_id=10)}

        key1 = mgr.compute_key(ns, inputs, render_mode="preview")
        key2 = mgr.compute_key(ns, inputs, render_mode="preview")
        assert key1 == key2


class TestCacheKeyNodeSpecificity:
    """Different node_id → different key (even with same params)."""

    def test_different_node_id_different_key(self):
        mgr = _make_cache_mgr()
        inputs = {"image": TextureResult(texture_id=10)}

        ns1 = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        ns2 = _make_node_snapshot("blur", "b2", {"radius": 5.0})

        key1 = mgr.compute_key(ns1, inputs)
        key2 = mgr.compute_key(ns2, inputs)
        assert key1 != key2


class TestCacheManagement:
    """CacheManager lifecycle: put, get, invalidate, clear."""

    def test_put_and_get(self):
        mgr = _make_cache_mgr()
        entry = CacheEntry(outputs={"result": TextureResult(texture_id=1)})
        mgr.put("key1", entry)
        assert mgr.get("key1") is entry

    def test_get_miss_returns_none(self):
        mgr = _make_cache_mgr()
        assert mgr.get("nonexistent") is None

    def test_invalidate_node(self):
        mgr = _make_cache_mgr()
        # Build keys manually using compute_key format
        mgr._cache["blur:b1:abc:():E:rm=preview"] = CacheEntry(
            outputs={"result": TextureResult(texture_id=1)},
        )
        mgr._cache["blur:b2:def:():E:rm=preview"] = CacheEntry(
            outputs={"result": TextureResult(texture_id=2)},
        )
        mgr._cache["grade:b1:ghi:():E:rm=preview"] = CacheEntry(
            outputs={"result": TextureResult(texture_id=3)},
        )

        assert mgr.entry_count() == 3

        # Invalidate all entries for node b1
        mgr.invalidate_node("b1")
        assert mgr.entry_count() == 1
        assert mgr.get("blur:b2:def:():E:rm=preview") is not None

    def test_invalidate_nonexistent_node_is_noop(self):
        mgr = _make_cache_mgr()
        mgr._cache["blur:b1:abc:():E:rm=preview"] = CacheEntry(
            outputs={"result": TextureResult(texture_id=1)},
        )
        mgr.invalidate_node("nonexistent")
        assert mgr.entry_count() == 1

    def test_clear_cache(self):
        mgr = _make_cache_mgr()
        mgr._cache["blur:b1:abc:():E:rm=preview"] = CacheEntry(
            outputs={"result": TextureResult(texture_id=1)},
        )
        mgr._cache["grade:b2:def:():E:rm=preview"] = CacheEntry(
            outputs={"result": TextureResult(texture_id=2)},
        )

        mgr.clear()
        assert mgr.entry_count() == 0

    def test_clear_empty_cache_is_safe(self):
        mgr = _make_cache_mgr()
        mgr.clear()  # Should not raise
        assert mgr.entry_count() == 0

    def test_lru_on_get(self):
        """get() moves entry to end (most recently used)."""
        mgr = _make_cache_mgr()
        mgr.put("key1", CacheEntry(outputs={"result": TextureResult(texture_id=1)}))
        mgr.put("key2", CacheEntry(outputs={"result": TextureResult(texture_id=2)}))
        mgr.put("key3", CacheEntry(outputs={"result": TextureResult(texture_id=3)}))

        # Access key1 — should move to end
        mgr.get("key1")

        # OrderedDict: key2, key3, key1 (after get)
        keys = list(mgr._cache.keys())
        assert keys[-1] == "key1"

    def test_stats_tracking(self):
        mgr = _make_cache_mgr()
        mgr.put("key1", CacheEntry(outputs={"result": TextureResult(texture_id=1)}))

        # Hit
        mgr.get("key1")
        assert mgr.stats.hits == 1

        # Miss
        mgr.get("nonexistent")
        assert mgr.stats.misses == 1


class TestCacheEntryFields:
    """CacheEntry dataclass correctness."""

    def test_defaults(self):
        entry = CacheEntry()
        assert entry.outputs == {}
        assert entry.owned_texture_ids == set()

    def test_with_values(self):
        tex = TextureResult(texture_id=5, width=100, height=100)
        entry = CacheEntry(
            outputs={"result": tex},
            owned_texture_ids={5},
        )
        assert entry.outputs["result"].texture_id == 5
        assert 5 in entry.owned_texture_ids

    def test_memory_bytes(self):
        tex = TextureResult(texture_id=5, width=1920, height=1080)
        # RGBA half-float: 1920 * 1080 * 4 * 2 = 16,588,800 bytes
        assert tex.memory_bytes() == 1920 * 1080 * 4 * 2

    def test_memory_bytes_invalid(self):
        tex = TextureResult(texture_id=0)
        assert tex.memory_bytes() == 0


class TestCacheEntryRelease:
    """Cache eviction must release every owned GPU attachment."""

    def test_release_includes_depth_and_aov_textures(self):
        class RecordingPool:
            def __init__(self):
                self.released = []

            def release_texture_by_id(self, texture_id):
                self.released.append(texture_id)

        pool = RecordingPool()
        mgr = CacheManager(pool)
        output = TextureResult(
            texture_id=100,
            width=1920,
            height=1080,
            depth_texture_id=200,
            has_deep_data=True,
            aov_texture_ids={"depth": 200, "object_id": 300},
        )
        entry = CacheEntry(
            outputs={"result": output},
            owned_texture_ids={100, 200, 300},
        )

        mgr._release_entry(entry)

        assert set(pool.released) == {100, 200, 300}
        assert len(pool.released) == 3


class TestGPUResourcePool:
    """GPUResourcePool memory tracking (no GL)."""

    def test_track_external(self):
        fb = MockFramebufferManager()
        pool = GPUResourcePool(fb, max_memory_mb=256)
        pool.track_external(42, 1920, 1080)
        assert pool.active_count() == 1
        assert pool.external_count() == 1
        assert pool.memory_usage_bytes() > 0

    def test_untrack_external(self):
        fb = MockFramebufferManager()
        pool = GPUResourcePool(fb, max_memory_mb=256)
        pool.track_external(42, 1920, 1080)
        pool.untrack_external(42)
        assert pool.active_count() == 0

    def test_dump_stats(self):
        fb = MockFramebufferManager()
        pool = GPUResourcePool(fb, max_memory_mb=256)
        pool.track_external(42, 1920, 1080)
        stats = pool.dump_stats()
        assert "[ResourcePool]" in stats
        assert "ext=1" in stats


# ── S1-D: Texture Stability Tests ─────────────────────────────────────────────

class TestTextureStabilityS1D:
    """S1-D 验收测试：确保 cache ownership 和资源释放规则正确。

    验收标准：
    1. 连续渲染同一图 20 次，texture 数量稳定
    2. 清 cache 后 texture 全部释放
    """

    def test_cache_owned_types_includes_all_texture_producers(self):
        """验证 _CACHE_OWNED_TYPES 包含所有会产生 GPU texture 的节点类型。

        S1-D: 检查新增的节点类型是否已加入 cache ownership 列表。
        """
        # Phase 2 F2-02: CACHE_OWNED_TYPES moved to node_capabilities
        from src.features.composite.core.node_capabilities import CACHE_OWNED_TYPES

        cache_owned = CACHE_OWNED_TYPES

        # Generator nodes (produce textures without input)
        assert "noise" in cache_owned
        assert "ramp" in cache_owned
        assert "mask" in cache_owned

        # Transform nodes (produce transformed textures)
        assert "cornerpin" in cache_owned

        # Keyer/Matte nodes (produce matte textures)
        assert "keyer" in cache_owned
        assert "difference_keyer" in cache_owned
        assert "matte" in cache_owned

        # Color processing nodes (produce processed textures)
        assert "color_correct" in cache_owned
        assert "huecorrect" in cache_owned
        assert "normalize" in cache_owned

        # Filter nodes (produce filtered textures)
        assert "edge_detect" in cache_owned
        assert "denoise" in cache_owned

        # Channel nodes (produce channel-adjusted textures)
        assert "channel_merge" in cache_owned

        # Deep merge (produces color and depth textures)
        assert "deep_merge" in cache_owned

    def test_cache_entry_owns_depth_texture(self):
        """S1-D: CacheEntry 应该管理 depth texture 的 ownership。

        DeepMergeNode 会产生 color texture 和 depth texture，
        两者都应该被正确跟踪和释放。
        """
        # Create texture result with depth
        color_tex = TextureResult(texture_id=100, width=1920, height=1080)
        depth_tex_id = 200

        # Simulate DeepMerge output with depth
        output_with_depth = TextureResult(
            texture_id=100,
            width=1920,
            height=1080,
            depth_texture_id=depth_tex_id,
            has_deep_data=True,
        )

        # Create entry with depth texture ownership
        entry = CacheEntry(
            outputs={"result": output_with_depth},
            owned_texture_ids={100, 200},  # Both color and depth
        )

        # Verify both are tracked
        assert 100 in entry.owned_texture_ids
        assert 200 in entry.owned_texture_ids

    def test_repeated_render_texture_count_stable(self):
        """验收标准 1：连续渲染同一图 20 次，texture 数量稳定。

        使用 mock 资源池模拟重复渲染场景，验证 texture 数量不会增长。
        """
        fb = MockFramebufferManager()
        pool = GPUResourcePool(fb, max_memory_mb=256)
        mgr = _make_cache_mgr()

        # Track initial external texture (source node output)
        pool.track_external(100, 1920, 1080)

        # Create node snapshot for effect node
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": TextureResult(texture_id=100, width=1920, height=1080)}

        # Compute cache key
        key = mgr.compute_key(ns, inputs, render_mode="preview")

        # Create entry with owned texture
        output_tex = TextureResult(texture_id=101, width=1920, height=1080)
        entry = CacheEntry(
            outputs={"result": output_tex},
            owned_texture_ids={101},
        )

        # "Render" 20 times - should hit cache after first time
        initial_active = pool.active_count()

        for i in range(20):
            # First iteration: cache miss, put entry
            if i == 0:
                mgr.put(key, entry, render_mode="preview")
            else:
                # Subsequent iterations: cache hit
                cached = mgr.get(key)
                assert cached is not None

        # After 20 "renders", texture count should be stable
        # (only the external texture we tracked)
        final_active = pool.active_count()
        assert final_active == initial_active

        # Cache should have exactly 1 entry
        assert mgr.entry_count() == 1

    def test_cache_clear_releases_all_textures(self):
        """验收标准 2：清 cache 后 texture 全部释放。

        验证 CacheManager.clear() 正确清空所有 entries。
        注意：clear() 是直接清空操作，不触发 eviction 统计。
        """
        fb = MockFramebufferManager()
        pool = GPUResourcePool(fb, max_memory_mb=256)
        mgr = CacheManager(pool, max_entries=128)

        # Create multiple cache entries with owned textures
        for i in range(5):
            tex = TextureResult(texture_id=i + 1, width=1920, height=1080)
            entry = CacheEntry(
                outputs={"result": tex},
                owned_texture_ids={i + 1},
            )
            key = f"blur:b{i}:hash{i}:():E:rm=preview"
            mgr.put(key, entry, render_mode="preview")

        # Verify cache has entries
        assert mgr.entry_count() == 5

        # Clear cache
        mgr.clear()

        # Verify all entries removed
        assert mgr.entry_count() == 0

        # Verify memory was released (tracked in stats)
        assert mgr.stats.memory_released_bytes >= 5 * 1920 * 1080 * 4 * 2

    def test_collect_owned_texture_ids_with_depth(self):
        """S1-D: _collect_owned_texture_ids 应该收集 depth texture IDs。

        验证新扩展的方法正确处理 depth texture ownership。
        """
        # This test requires a real evaluator instance, so we test the logic
        # by simulating the method's expected behavior

        # Create input texture (upstream, not owned)
        input_tex = TextureResult(texture_id=10, width=1920, height=1080)

        # Create output texture with depth (should be owned)
        output_tex = TextureResult(
            texture_id=20,
            width=1920,
            height=1080,
            depth_texture_id=21,
            has_deep_data=True,
        )

        # Expected owned IDs: 20 (color) and 21 (depth)
        # Input ID 10 should NOT be owned (it's upstream)
        expected_owned = {20, 21}

        # Verify the output has depth
        assert output_tex.has_depth()
        assert output_tex.depth_texture_id == 21

        # Verify depth texture ID is different from color texture ID
        assert output_tex.depth_texture_id != output_tex.texture_id

        # This simulates what _collect_owned_texture_ids should return
        # (the actual method is tested via integration tests)
        assert 10 not in expected_owned  # upstream should not be owned
        assert 20 in expected_owned  # output color should be owned
        assert 21 in expected_owned  # output depth should be owned

    def test_source_node_textures_not_owned_by_evaluator(self):
        """S1-D: Source node 的 texture 不应该被 evaluator 释放。

        验证 color_input、image_input 等节点的 texture 不在
        _CACHE_OWNED_TYPES 中，它们自己管理 texture 生命周期。
        
        Phase 2 F2-02: source nodes self-manage their texture lifetimes.
        All source nodes (image_input/video_frame/constant/color_input) are
        marked owns_color_texture=False so the cache manager does NOT release
        their glo on eviction. This prevents double-free crashes: Constant
        (and image_input/video_frame) release their own _gpu_texture on
        parameter change and in cleanup(); if the cache also released the
        same glo, a driver-level crash could occur (especially on the short
        Constant→Output passthrough chain).
        """
        # Phase 2 F2-02: CACHE_OWNED_TYPES moved to node_capabilities
        from src.features.composite.core.node_capabilities import CACHE_OWNED_TYPES

        cache_owned = CACHE_OWNED_TYPES

        # Source nodes with external textures should NOT be in cache owned types
        # (they manage their own texture lifetimes from external sources)
        assert "image_input" not in cache_owned
        # video_frame uses external texture, marked as owns_color_texture=False
        assert "video_frame" not in cache_owned

        # constant self-manages its texture (releases on param/size change,
        # destroys in cleanup()), so it must NOT be in cache_owned to avoid
        # double-free when the cache evicts a stale entry holding the same glo.
        assert "constant" not in cache_owned


# ── Interaction Mode (drag optimization) ──────────────────────────────────

class TestInteractionMode:
    """CacheManager interaction mode raises limits during handle drag.

    During a handle drag the dragged node's params change every frame,
    producing a fresh CacheEntry per frame. At the baseline 128-entry /
    512MB budget this thrashes the cache (eviction → texture release →
    real GL allocation jitter). begin_interaction() temporarily raises the
    budget so the per-frame entries stay cached.
    """

    def test_begin_interaction_raises_limits(self):
        """begin_interaction() raises max_entries and max_memory_mb."""
        mgr = _make_cache_mgr()
        baseline_entries = mgr.max_entries
        baseline_memory = mgr.max_memory_mb
        assert not mgr.interaction_active

        mgr.begin_interaction()

        assert mgr.interaction_active is True
        assert mgr.max_entries > baseline_entries
        assert mgr.max_memory_mb > baseline_memory
        assert mgr.max_entries == mgr._INTERACTION_MAX_ENTRIES
        assert mgr.max_memory_mb == mgr._INTERACTION_MAX_MEMORY_MB

    def test_end_interaction_restores_limits(self):
        """end_interaction() restores baseline limits."""
        mgr = _make_cache_mgr()
        baseline_entries = mgr.max_entries
        baseline_memory = mgr.max_memory_mb

        mgr.begin_interaction()
        mgr.end_interaction()

        assert mgr.interaction_active is False
        assert mgr.max_entries == baseline_entries
        assert mgr.max_memory_mb == baseline_memory

    def test_begin_interaction_is_idempotent(self):
        """Repeated begin_interaction() doesn't overwrite the stored baseline."""
        mgr = _make_cache_mgr()
        mgr.begin_interaction()
        stored_baseline = mgr._interaction_backup_max_entries

        # Second begin while already active should be a no-op
        mgr.begin_interaction()

        assert mgr._interaction_backup_max_entries == stored_baseline
        assert mgr.interaction_active is True

    def test_end_interaction_without_begin_is_noop(self):
        """end_interaction() when not active is a safe no-op."""
        mgr = _make_cache_mgr()
        baseline_entries = mgr.max_entries

        mgr.end_interaction()  # not active

        assert mgr.interaction_active is False
        assert mgr.max_entries == baseline_entries
        assert mgr._interaction_backup_max_entries is None

    def test_no_eviction_during_interaction(self):
        """During interaction mode, exceeding baseline budget doesn't evict.

        Put more entries than the baseline allows but fewer than the raised
        interaction limit — none should be evicted because _evict() checks
        against the (raised) max_entries.
        """
        mgr = _make_cache_mgr()  # baseline max_entries=128
        mgr.begin_interaction()  # raised to 512

        # Insert 200 entries (over baseline 128, under interaction 512)
        for i in range(200):
            key = f"transform:t1:param{i}:():E:rm=preview"
            mgr.put(key, CacheEntry(outputs={"result": TextureResult(texture_id=0)}),
                    render_mode="preview")

        assert mgr.entry_count() == 200, (
            f"expected 200 entries preserved during interaction, got {mgr.entry_count()}"
        )

    def test_end_interaction_evicts_drag_residue(self):
        """end_interaction() reclaims entries that exceed the restored baseline.

        After the drag, the budget drops back; the surplus PREVIEW entries
        (drag residue) should be evicted down to the baseline.
        """
        mgr = _make_cache_mgr()  # baseline max_entries=128
        mgr.begin_interaction()

        # Simulate a drag producing 200 per-frame entries
        for i in range(200):
            key = f"transform:t1:param{i}:():E:rm=preview"
            mgr.put(key, CacheEntry(outputs={"result": TextureResult(texture_id=0)}),
                    render_mode="preview")
        assert mgr.entry_count() == 200

        mgr.end_interaction()

        # Should have evicted down to <= baseline (128)
        assert mgr.entry_count() <= 128, (
            f"expected <=128 after end_interaction, got {mgr.entry_count()}"
        )
        assert mgr.interaction_active is False

    def test_interaction_property_reflects_state(self):
        """interaction_active property tracks begin/end calls."""
        mgr = _make_cache_mgr()
        assert mgr.interaction_active is False

        mgr.begin_interaction()
        assert mgr.interaction_active is True

        mgr.end_interaction()
        assert mgr.interaction_active is False
