"""
Tests for Node-Level Cache Strategy Upgrade — pure logic, no GL.

Covers acceptance criteria:
- 调整 viewer ROI 不会错误复用全局不匹配 cache
- preview cache 不会污染 full-res export
- 切换 color space/alpha mode 会正确失效
- cache manager 能按内存预算驱逐
- CacheCategory classification
- output_format, alpha_mode, proxy_scale, frame_number in cache key
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.render.cache_manager import CacheManager, CacheEntry, CacheCategory
from src.features.composite.render.gpu_resource_pool import GPUResourcePool
from src.features.composite.core.graph_snapshot import NodeSnapshot
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.core.format import ImageFormat, BBox

import pytest
pytestmark = pytest.mark.composite_core


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


def _make_cache_mgr(max_entries=128, max_memory_mb=512) -> CacheManager:
    """Create a CacheManager with a mock resource pool."""
    fb_mgr = MockFramebufferManager()
    pool = GPUResourcePool(fb_mgr, max_memory_mb=max_memory_mb * 2)
    return CacheManager(pool, max_entries=max_entries, max_memory_mb=max_memory_mb)


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


def _make_texture(texture_id=1, width=1920, height=1080, revision=1,
                   alpha_premultiplied=True, color_space="linear"):
    """Helper to create a TextureResult."""
    return TextureResult(
        texture_id=texture_id,
        width=width,
        height=height,
        revision=revision,
        alpha_premultiplied=alpha_premultiplied,
        color_space=color_space,
    )


# ── Acceptance Criteria Tests ─────────────────────────────────────────────

class TestAcceptanceCriteria:
    """Tests for explicit acceptance criteria from the upgrade spec."""

    def test_roi_adjustment_no_incorrect_reuse(self):
        """验收标准：调整 viewer ROI 不会错误复用全局不匹配 cache"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": _make_texture()}

        # Create cache with ROI (0, 0, 100, 100)
        roi1 = BBox(x=0, y=0, width=100, height=100)
        key1 = mgr.compute_key(ns, inputs, render_mode="preview", roi=roi1)

        # Same params but different ROI (100, 100, 100, 100) → different key
        roi2 = BBox(x=100, y=100, width=100, height=100)
        key2 = mgr.compute_key(ns, inputs, render_mode="preview", roi=roi2)

        assert key1 != key2, "Different ROI should produce different cache key"

    def test_preview_export_isolation(self):
        """验收标准：preview cache 不会污染 full-res export"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": _make_texture()}

        key_preview = mgr.compute_key(ns, inputs, render_mode="preview")
        key_export = mgr.compute_key(ns, inputs, render_mode="export")

        assert key_preview != key_export, "Preview and export should have different cache keys"

        # Put preview entry
        entry = CacheEntry(outputs={"result": _make_texture()})
        mgr.put(key_preview, entry, render_mode="preview")

        # Export should NOT hit preview cache
        assert mgr.get(key_export) is None, "Export request should not hit preview cache"
        assert mgr.get(key_preview) is not None, "Preview cache should exist"

    def test_color_space_change_invalidates(self):
        """验收标准：切换 color space 会正确失效"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})

        # Input with linear color space
        inputs_linear = {"image": _make_texture(color_space="linear")}
        key_linear = mgr.compute_key(ns, inputs_linear, render_mode="preview")

        # Input with sRGB color space
        inputs_srgb = {"image": _make_texture(color_space="srgb")}
        key_srgb = mgr.compute_key(ns, inputs_srgb, render_mode="preview")

        assert key_linear != key_srgb, "Different color space should invalidate cache"

    def test_alpha_mode_change_invalidates(self):
        """验收标准：切换 alpha mode 会正确失效"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": _make_texture()}

        # Request with premult alpha mode
        key_premult = mgr.compute_key(ns, inputs, render_mode="preview", alpha_mode="premult")

        # Request with unpremult alpha mode
        key_unpremult = mgr.compute_key(ns, inputs, render_mode="preview", alpha_mode="unpremult")

        assert key_premult != key_unpremult, "Different alpha mode should invalidate cache"

    def test_memory_budget_eviction(self):
        """验收标准：cache manager 能按内存预算驱逐"""
        # Small memory budget (1 MB)
        mgr = _make_cache_mgr(max_entries=100, max_memory_mb=1.0)

        # Add entries that exceed memory budget
        # 1920x1080 RGBA half-float = ~4 MB each
        for i in range(5):
            ns = _make_node_snapshot("blur", f"b{i}", {"radius": 5.0})
            tex = _make_texture(texture_id=i+1)
            inputs = {"image": tex}
            key = mgr.compute_key(ns, inputs, render_mode="preview")
            entry = CacheEntry(
                outputs={"result": tex},
                owned_texture_ids={i+1},
            )
            mgr.put(key, entry, render_mode="preview")

        # Should have evicted entries to meet budget
        # With 1 MB budget and ~4 MB per entry, should have ~0 entries left
        assert mgr.estimated_memory_mb() <= mgr.max_memory_mb, \
            f"Memory should be within budget: {mgr.estimated_memory_mb():.2f}MB > {mgr.max_memory_mb}MB"

    def test_output_format_change_invalidates(self):
        """验收标准：请求不同输出格式会正确失效"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("reformat", "r1", {"target_width": 1920})
        inputs = {"image": _make_texture()}

        # Request for 1920x1080 output
        fmt1 = ImageFormat(width=1920, height=1080)
        key1 = mgr.compute_key(ns, inputs, render_mode="preview", output_format=fmt1)

        # Request for 3840x2160 output
        fmt2 = ImageFormat(width=3840, height=2160)
        key2 = mgr.compute_key(ns, inputs, render_mode="preview", output_format=fmt2)

        assert key1 != key2, "Different output format should invalidate cache"

    def test_proxy_scale_change_invalidates(self):
        """验收标准：改变 proxy scale 会正确失效"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": _make_texture()}

        # Full resolution (proxy_scale=1.0)
        key_full = mgr.compute_key(ns, inputs, render_mode="preview", proxy_scale=1.0)

        # Half resolution (proxy_scale=0.5)
        key_half = mgr.compute_key(ns, inputs, render_mode="preview", proxy_scale=0.5)

        assert key_full != key_half, "Different proxy scale should invalidate cache"

    def test_frame_number_change_invalidates(self):
        """验收标准：改变 frame number 会正确失效（视频帧）"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": _make_texture()}

        # Frame 0
        key_frame0 = mgr.compute_key(ns, inputs, render_mode="preview", frame_number=0)

        # Frame 100
        key_frame100 = mgr.compute_key(ns, inputs, render_mode="preview", frame_number=100)

        assert key_frame0 != key_frame100, "Different frame number should invalidate cache"

    def test_cache_key_includes_roi(self):
        """验收标准：缓存 key 包含 ROI 信息（x, y, width, height 都编码）"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": _make_texture()}

        # 创建多个不同 ROI 的缓存请求，验证所有 ROI 值都被编码
        roi1 = BBox(x=100, y=200, width=300, height=400)
        key1 = mgr.compute_key(ns, inputs, render_mode="preview", roi=roi1)

        # 验证 ROI 信息编码格式：:roi100,200,300,400
        assert ":roi" in key1, "Key should contain ROI suffix"
        assert "roi100,200,300,400" in key1, "All ROI values (x,y,width,height) should be encoded"

        # 相同参数但不同 ROI 的请求
        roi2 = BBox(x=0, y=0, width=100, height=100)
        key2 = mgr.compute_key(ns, inputs, render_mode="preview", roi=roi2)

        # 验证两个 key 不同
        assert key1 != key2, "Different ROI should produce different cache key"

        # 验证第二个 key 编码正确的 ROI 信息
        assert "roi0,0,100,100" in key2, "ROI should encode correct values"

    def test_different_rois_different_keys(self):
        """验收标准：不同 ROI 生成唯一缓存 key，测试边界情况"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1", {"radius": 5.0})
        inputs = {"image": _make_texture()}

        # 创建多个不同 ROI 的缓存请求
        rois = [
            BBox(x=0, y=0, width=100, height=100),      # 基准 ROI
            BBox(x=50, y=50, width=100, height=100),    # 相邻 ROI（有重叠）
            BBox(x=100, y=100, width=100, height=100),  # 不重叠 ROI
            BBox(x=0, y=0, width=50, height=50),        # 同起点但更小
            BBox(x=0, y=0, width=100, height=100),      # 与第一个相同（应生成相同 key）
        ]

        keys = [mgr.compute_key(ns, inputs, render_mode="preview", roi=roi) for roi in rois]

        # 验证每个不同 ROI 生成唯一的 key
        unique_keys = set(keys[:-1])  # 排除最后一个（与第一个相同）
        assert len(unique_keys) == 4, "4 different ROIs should produce 4 unique keys"

        # 验证相邻 ROI（有重叠）生成不同 key
        assert keys[0] != keys[1], "Adjacent ROI should produce different key"

        # 验证不重叠 ROI 生成不同 key
        assert keys[0] != keys[2], "Non-overlapping ROI should produce different key"

        # 验证同起点但不同尺寸生成不同 key
        assert keys[0] != keys[3], "Same origin but different size should produce different key"

        # 验证相同 ROI 生成相同 key
        assert keys[0] == keys[4], "Same ROI should produce same key"


# ── CacheCategory Tests ───────────────────────────────────────────────────

class TestCacheCategory:
    """Tests for cache category classification and eviction priority."""

    def test_category_classification_preview(self):
        """PREVIEW category for preview render mode effect nodes"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1")
        inputs = {"image": _make_texture()}
        key = mgr.compute_key(ns, inputs, render_mode="preview")

        entry = CacheEntry(outputs={"result": _make_texture()})
        mgr.put(key, entry, render_mode="preview")

        retrieved = mgr.get(key)
        assert retrieved.category == CacheCategory.PREVIEW

    def test_category_classification_export(self):
        """EXPORT category for export render mode"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1")
        inputs = {"image": _make_texture()}
        key = mgr.compute_key(ns, inputs, render_mode="export")

        entry = CacheEntry(outputs={"result": _make_texture()})
        mgr.put(key, entry, render_mode="export")

        retrieved = mgr.get(key)
        assert retrieved.category == CacheCategory.EXPORT

    def test_category_classification_source(self):
        """SOURCE category for source nodes (color_input, constant, image_input)"""
        mgr = _make_cache_mgr()

        # Test color_input
        ns_color = _make_node_snapshot("color_input", "c1", {})
        key_color = mgr.compute_key(ns_color, {}, render_mode="preview")
        entry_color = CacheEntry(outputs={"image": _make_texture()})
        mgr.put(key_color, entry_color, render_mode="preview")
        assert mgr.get(key_color).category == CacheCategory.SOURCE

        # Test constant
        ns_const = _make_node_snapshot("constant", "c2", {"width": 1920})
        key_const = mgr.compute_key(ns_const, {}, render_mode="preview")
        entry_const = CacheEntry(outputs={"result": _make_texture()})
        mgr.put(key_const, entry_const, render_mode="preview")
        assert mgr.get(key_const).category == CacheCategory.SOURCE

    def test_category_classification_ai(self):
        """AI category for AI node types"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("ai_generate", "ai1", {"prompt": "test"})
        inputs = {"image": _make_texture()}
        key = mgr.compute_key(ns, inputs, render_mode="preview")

        entry = CacheEntry(outputs={"result": _make_texture()})
        mgr.put(key, entry, render_mode="preview")

        retrieved = mgr.get(key)
        assert retrieved.category == CacheCategory.AI

    def test_source_entries_protected_from_eviction(self):
        """SOURCE entries should not be evicted even under memory pressure"""
        mgr = _make_cache_mgr(max_entries=100, max_memory_mb=1.0)

        # Add SOURCE entry (color_input)
        ns_color = _make_node_snapshot("color_input", "c1", {})
        key_color = mgr.compute_key(ns_color, {}, render_mode="preview")
        tex_color = _make_texture(texture_id=100)
        entry_color = CacheEntry(
            outputs={"image": tex_color},
            owned_texture_ids={100},
        )
        mgr.put(key_color, entry_color, render_mode="preview")

        # Add PREVIEW entries that exceed budget
        for i in range(5):
            ns = _make_node_snapshot("blur", f"b{i}", {"radius": 5.0})
            tex = _make_texture(texture_id=i+1)
            inputs = {"image": tex}
            key = mgr.compute_key(ns, inputs, render_mode="preview")
            entry = CacheEntry(
                outputs={"result": tex},
                owned_texture_ids={i+1},
            )
            mgr.put(key, entry, render_mode="preview")

        # SOURCE entry should still exist
        assert mgr.get(key_color) is not None, "SOURCE entry should not be evicted"

    def test_preview_evicted_before_effect(self):
        """PREVIEW entries should be evicted before EFFECT entries"""
        mgr = _make_cache_mgr(max_entries=100, max_memory_mb=1.0)

        # Add one EFFECT entry (by using render_mode="full")
        ns_effect = _make_node_snapshot("blur", "e1")
        inputs_effect = {"image": _make_texture(texture_id=50)}
        key_effect = mgr.compute_key(ns_effect, inputs_effect, render_mode="full")
        tex_effect = _make_texture(texture_id=50)
        entry_effect = CacheEntry(
            outputs={"result": tex_effect},
            owned_texture_ids={50},
        )
        mgr.put(key_effect, entry_effect, render_mode="full")

        # Add PREVIEW entries that exceed budget
        preview_keys = []
        for i in range(5):
            ns = _make_node_snapshot("blur", f"p{i}")
            tex = _make_texture(texture_id=i+1)
            inputs = {"image": tex}
            key = mgr.compute_key(ns, inputs, render_mode="preview")
            entry = CacheEntry(
                outputs={"result": tex},
                owned_texture_ids={i+1},
            )
            mgr.put(key, entry, render_mode="preview")
            preview_keys.append(key)

        # Check that PREVIEW entries were evicted first
        preview_count = mgr.category_entry_count(CacheCategory.PREVIEW)
        effect_count = mgr.category_entry_count(CacheCategory.EFFECT)

        # PREVIEW should be fewer than EFFECT (or zero) after eviction
        assert preview_count <= effect_count, \
            f"PREVIEW ({preview_count}) should be evicted before EFFECT ({effect_count})"


# ── CacheEntry Enhanced Tests ─────────────────────────────────────────────

class TestCacheEntryEnhanced:
    """Tests for enhanced CacheEntry with metadata."""

    def test_memory_cost_computation(self):
        """CacheEntry should compute memory cost from outputs"""
        tex1 = _make_texture(texture_id=1, width=1920, height=1080)  # ~4 MB
        tex2 = _make_texture(texture_id=2, width=960, height=540)   # ~1 MB

        entry = CacheEntry(outputs={"result": tex1, "aux": tex2})
        cost = entry.compute_memory_cost()

        expected = tex1.memory_bytes() + tex2.memory_bytes()
        assert cost == expected, f"Memory cost should be sum of outputs: {cost} != {expected}"

    def test_last_used_frame_tracking(self):
        """CacheEntry should track last used frame number"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1")
        inputs = {"image": _make_texture()}
        key = mgr.compute_key(ns, inputs, render_mode="preview")

        entry = CacheEntry(outputs={"result": _make_texture()})
        mgr.put(key, entry, render_mode="preview", frame_number=0)

        # Set frame number and access
        mgr.set_frame_number(100)
        mgr.get(key)

        retrieved = mgr.get(key)
        assert retrieved.last_used_frame == 100, \
            f"last_used_frame should be updated to 100, got {retrieved.last_used_frame}"

    def test_roi_metadata_storage(self):
        """CacheEntry should store ROI metadata"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1")
        inputs = {"image": _make_texture()}
        roi = BBox(x=0, y=0, width=100, height=100)
        key = mgr.compute_key(ns, inputs, render_mode="preview", roi=roi)

        entry = CacheEntry(outputs={"result": _make_texture()})
        mgr.put(key, entry, render_mode="preview", roi=roi)

        retrieved = mgr.get(key)
        assert retrieved.roi == roi, "ROI should be stored in entry metadata"

    def test_render_mode_metadata_storage(self):
        """CacheEntry should store render_mode metadata"""
        mgr = _make_cache_mgr()
        ns = _make_node_snapshot("blur", "b1")
        inputs = {"image": _make_texture()}
        key = mgr.compute_key(ns, inputs, render_mode="export")

        entry = CacheEntry(outputs={"result": _make_texture()})
        mgr.put(key, entry, render_mode="export")

        retrieved = mgr.get(key)
        assert retrieved.render_mode == "export", "render_mode should be stored in entry metadata"


# ── Clear Methods Tests ───────────────────────────────────────────────────

class TestClearMethods:
    """Tests for category-specific clear methods."""

    def test_clear_export_cache_only(self):
        """clear_export_cache should only clear EXPORT entries"""
        mgr = _make_cache_mgr()

        # Add entries in different categories
        ns_preview = _make_node_snapshot("blur", "p1")
        key_preview = mgr.compute_key(ns_preview, {"image": _make_texture()}, render_mode="preview")
        mgr.put(key_preview, CacheEntry(outputs={"result": _make_texture()}), render_mode="preview")

        ns_export = _make_node_snapshot("blur", "e1")
        key_export = mgr.compute_key(ns_export, {"image": _make_texture()}, render_mode="export")
        mgr.put(key_export, CacheEntry(outputs={"result": _make_texture()}), render_mode="export")

        # Clear only export cache
        mgr.clear_export_cache()

        assert mgr.get(key_preview) is not None, "PREVIEW entry should remain"
        assert mgr.get(key_export) is None, "EXPORT entry should be cleared"

    def test_clear_preview_cache_only(self):
        """clear_preview_cache should only clear PREVIEW entries"""
        mgr = _make_cache_mgr()

        # Add entries in different categories
        ns_preview = _make_node_snapshot("blur", "p1")
        key_preview = mgr.compute_key(ns_preview, {"image": _make_texture()}, render_mode="preview")
        mgr.put(key_preview, CacheEntry(outputs={"result": _make_texture()}), render_mode="preview")

        ns_export = _make_node_snapshot("blur", "e1")
        key_export = mgr.compute_key(ns_export, {"image": _make_texture()}, render_mode="export")
        mgr.put(key_export, CacheEntry(outputs={"result": _make_texture()}), render_mode="export")

        # Clear only preview cache
        mgr.clear_preview_cache()

        assert mgr.get(key_preview) is None, "PREVIEW entry should be cleared"
        assert mgr.get(key_export) is not None, "EXPORT entry should remain"


# ── Stats and Debug Tests ─────────────────────────────────────────────────

class TestStatsAndDebug:
    """Tests for enhanced statistics and debug output."""

    def test_per_category_memory_tracking(self):
        """category_memory_mb should return memory per category"""
        mgr = _make_cache_mgr()

        # Add entries
        ns_preview = _make_node_snapshot("blur", "p1")
        tex_preview = _make_texture(texture_id=1, width=1920, height=1080)
        key_preview = mgr.compute_key(ns_preview, {"image": tex_preview}, render_mode="preview")
        entry_preview = CacheEntry(
            outputs={"result": tex_preview},
            owned_texture_ids={1},
        )
        mgr.put(key_preview, entry_preview, render_mode="preview")

        ns_export = _make_node_snapshot("blur", "e1")
        tex_export = _make_texture(texture_id=2, width=960, height=540)
        key_export = mgr.compute_key(ns_export, {"image": tex_export}, render_mode="export")
        entry_export = CacheEntry(
            outputs={"result": tex_export},
            owned_texture_ids={2},
        )
        mgr.put(key_export, entry_export, render_mode="export")

        # Check per-category memory
        preview_mem = mgr.category_memory_mb(CacheCategory.PREVIEW)
        export_mem = mgr.category_memory_mb(CacheCategory.EXPORT)

        assert preview_mem > 0, "PREVIEW category should have memory"
        assert export_mem > 0, "EXPORT category should have memory"
        assert preview_mem > export_mem, "PREVIEW should have more memory (larger texture)"

    def test_dump_stats_with_category_breakdown(self):
        """dump_stats should include per-category breakdown"""
        mgr = _make_cache_mgr()

        # Add entries
        ns = _make_node_snapshot("blur", "b1")
        inputs = {"image": _make_texture()}
        key = mgr.compute_key(ns, inputs, render_mode="preview")
        mgr.put(key, CacheEntry(outputs={"result": _make_texture()}), render_mode="preview")

        stats_str = mgr.dump_stats()

        assert "[CacheManager]" in stats_str
        assert "preview=1" in stats_str, "Should show PREVIEW category count"

    def test_dump_full_report(self):
        """dump_full_report should provide detailed debug info"""
        mgr = _make_cache_mgr()

        # Add entries
        ns = _make_node_snapshot("blur", "b1")
        inputs = {"image": _make_texture()}
        key = mgr.compute_key(ns, inputs, render_mode="preview")
        mgr.put(key, CacheEntry(outputs={"result": _make_texture()}), render_mode="preview")

        report = mgr.dump_full_report()

        assert "Per-category breakdown:" in report
        assert "Sample entries" in report
        assert "max_entries=" in report
        assert "max_memory_mb=" in report


if __name__ == "__main__":
    pytest.main([__file__, "-v"])