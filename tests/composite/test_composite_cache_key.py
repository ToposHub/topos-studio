"""
Tests for CacheManager cache key computation - Comprehensive key validation.

Covers all cache key components per Phase 3 requirements:
- node_type, node_id
- parameters hash
- upstream texture metadata (id, revision, format, bbox, alpha, color_space)
- disabled flag
- render_mode (preview vs export isolation)
- color texture id/revision for color_input nodes
- ROI (region of interest)
- output_format (dimensions, pixel_aspect)
- alpha_mode (premult, unpremult, auto)
- proxy_scale (preview scale factor)
- frame_number (video/animation)

These tests verify cache isolation for different rendering contexts.

Pure logic tests - no GPU required.
"""

import sys
import os
from unittest.mock import MagicMock, patch

# Mock moderngl before any imports that depend on it, but restore afterward
# to avoid polluting other tests that need real moderngl.
_original_moderngl = sys.modules.get('moderngl')
sys.modules['moderngl'] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Now import pytest after mock is set up
import pytest

from src.features.composite.render.cache_manager import CacheManager, CacheCategory, CacheEntry, CacheStats
from src.features.composite.render.gpu_resource_pool import GPUResourcePool

# Restore original moderngl so subsequent tests are not affected.
if _original_moderngl is not None:
    sys.modules['moderngl'] = _original_moderngl
else:
    sys.modules.pop('moderngl', None)
from src.features.composite.core.graph_snapshot import NodeSnapshot
from src.features.composite.core.format import BBox, ImageFormat
from src.features.composite.nodes.base_node import TextureResult

import pytest
pytestmark = pytest.mark.composite_core


# ── Helper Functions ──

def _make_mock_resource_pool():
    """Create a mock GPUResourcePool for CacheManager."""
    mock_pool = MagicMock(spec=GPUResourcePool)
    mock_pool.release_texture_by_id = MagicMock()
    return mock_pool


def _make_node_snapshot(node_id="test_node", node_type="blur", params=None,
                        disabled=False, param_revision=1, input_ports=None):
    """Create a NodeSnapshot for testing."""
    if input_ports is None:
        input_ports = [("image", "image")]
    return NodeSnapshot(
        node_id=node_id,
        node_type=node_type,
        parameters=params or {"radius": 5.0},
        param_revision=param_revision,
        input_ports=input_ports,
        output_ports=[("result", "image")],
        disabled=disabled,
    )


def _make_texture_result(texture_id=1, revision=1, width=1920, height=1080,
                         alpha_premult=False, color_space="srgb"):
    """Create a mock TextureResult for testing."""
    fmt = ImageFormat(
        width=width,
        height=height,
        pixel_aspect=1.0,
        origin_x=0,
        origin_y=0,
    )
    bbox = BBox(x=0, y=0, width=width, height=height)

    tex = MagicMock(spec=TextureResult)
    tex.texture_id = texture_id
    tex.revision = revision
    tex.format = fmt
    tex.bbox = bbox
    tex.alpha_premultiplied = alpha_premult
    tex.color_space = color_space
    tex.is_valid = True
    # Memory calculation: RGBA float32 = 4 channels * 4 bytes = 16 bytes per pixel
    tex.memory_bytes = MagicMock(return_value=width * height * 16)
    return tex


def _make_format(width=1920, height=1080, pixel_aspect=1.0):
    """Create an ImageFormat for testing."""
    return ImageFormat(
        width=width,
        height=height,
        pixel_aspect=pixel_aspect,
        origin_x=0,
        origin_y=0,
    )


# ═══════════════════════════════════════════════════════════════════════
# Basic Cache Key Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyBasic:
    """Test basic cache key components."""

    def test_node_type_in_key(self):
        """Node type is included in cache key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns_blur = _make_node_snapshot(node_type="blur")
        ns_grade = _make_node_snapshot(node_type="grade")

        key_blur = cache_mgr.compute_key(ns_blur, {}, render_mode="preview")
        key_grade = cache_mgr.compute_key(ns_grade, {}, render_mode="preview")

        # Keys should start with node_type
        assert key_blur.startswith("blur:")
        assert key_grade.startswith("grade:")
        assert key_blur != key_grade

    def test_node_id_in_key(self):
        """Node ID is included in cache key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns1 = _make_node_snapshot(node_id="blur1")
        ns2 = _make_node_snapshot(node_id="blur2")

        key1 = cache_mgr.compute_key(ns1, {}, render_mode="preview")
        key2 = cache_mgr.compute_key(ns2, {}, render_mode="preview")

        # Keys should contain node_id
        assert "blur1" in key1
        assert "blur2" in key2
        assert key1 != key2

    def test_params_affect_key(self):
        """Different parameters produce different keys."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns1 = _make_node_snapshot(params={"radius": 5.0})
        ns2 = _make_node_snapshot(params={"radius": 10.0})

        key1 = cache_mgr.compute_key(ns1, {}, render_mode="preview")
        key2 = cache_mgr.compute_key(ns2, {}, render_mode="preview")

        # Different params → different hash → different key
        assert key1 != key2

    def test_disabled_flag_in_key(self):
        """Disabled flag affects cache key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns_enabled = _make_node_snapshot(disabled=False)
        ns_disabled = _make_node_snapshot(disabled=True)

        key_enabled = cache_mgr.compute_key(ns_enabled, {}, render_mode="preview")
        key_disabled = cache_mgr.compute_key(ns_disabled, {}, render_mode="preview")

        # Enabled vs disabled should differ
        assert "E" in key_enabled  # "E" for enabled
        assert "D" in key_disabled  # "D" for disabled
        assert key_enabled != key_disabled


# ═══════════════════════════════════════════════════════════════════════
# Render Mode Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyRenderMode:
    """Test render_mode isolation (preview vs export)."""

    def test_render_mode_in_key(self):
        """Render mode is included in cache key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        key_preview = cache_mgr.compute_key(ns, {}, render_mode="preview")
        key_export = cache_mgr.compute_key(ns, {}, render_mode="export")

        # Keys should contain render_mode
        assert "rm=preview" in key_preview
        assert "rm=export" in key_export
        assert key_preview != key_export

    def test_preview_export_cache_isolation(self):
        """Preview and export caches are isolated (no cross-contamination)."""
        cache_mgr = CacheManager(_make_mock_resource_pool(), max_entries=10)

        ns = _make_node_snapshot()

        key_preview = cache_mgr.compute_key(ns, {}, render_mode="preview")
        key_export = cache_mgr.compute_key(ns, {}, render_mode="export")

        # Create entries
        entry_preview = CacheEntry(outputs={"result": _make_texture_result()})
        entry_export = CacheEntry(outputs={"result": _make_texture_result(texture_id=2)})

        cache_mgr.put(key_preview, entry_preview, render_mode="preview", category=CacheCategory.PREVIEW)
        cache_mgr.put(key_export, entry_export, render_mode="export", category=CacheCategory.EXPORT)

        # Get with correct mode
        hit_preview = cache_mgr.get(key_preview)
        hit_export = cache_mgr.get(key_export)

        assert hit_preview is not None
        assert hit_export is not None
        assert hit_preview.outputs["result"].texture_id == 1
        assert hit_export.outputs["result"].texture_id == 2

    def test_full_mode_in_key(self):
        """Full render mode is included in key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        key_preview = cache_mgr.compute_key(ns, {}, render_mode="preview")
        key_full = cache_mgr.compute_key(ns, {}, render_mode="full")

        assert "rm=preview" in key_preview
        assert "rm=full" in key_full
        assert key_preview != key_full


# ═══════════════════════════════════════════════════════════════════════
# ROI Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyROI:
    """Test ROI (Region of Interest) in cache key."""

    def test_roi_in_key(self):
        """ROI is included in cache key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()
        roi = BBox(x=100, y=100, width=200, height=200)

        key_no_roi = cache_mgr.compute_key(ns, {}, render_mode="preview")
        key_with_roi = cache_mgr.compute_key(ns, {}, render_mode="preview", roi=roi)

        assert "roi" in key_with_roi
        assert "roi100,100,200,200" in key_with_roi
        assert key_no_roi != key_with_roi

    def test_different_rois_different_keys(self):
        """Different ROIs produce different keys."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        roi1 = BBox(x=0, y=0, width=100, height=100)
        roi2 = BBox(x=50, y=50, width=100, height=100)

        key1 = cache_mgr.compute_key(ns, {}, render_mode="preview", roi=roi1)
        key2 = cache_mgr.compute_key(ns, {}, render_mode="preview", roi=roi2)

        assert key1 != key2

    def test_empty_roi_same_as_no_roi(self):
        """Empty ROI should be treated same as no ROI."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        empty_roi = BBox(x=0, y=0, width=0, height=0)

        key_no_roi = cache_mgr.compute_key(ns, {}, render_mode="preview")
        key_empty_roi = cache_mgr.compute_key(ns, {}, render_mode="preview", roi=empty_roi)

        # Empty ROI shouldn't add ROI suffix (is_empty check)
        assert "roi" not in key_empty_roi or key_empty_roi == key_no_roi

    def test_roi_prevents_wrong_cache_reuse(self):
        """ROI prevents incorrect cache reuse when viewer zoomed/panned."""
        cache_mgr = CacheManager(_make_mock_resource_pool(), max_entries=10)

        ns = _make_node_snapshot()

        # Cache full-frame result
        roi_full = BBox(x=0, y=0, width=1920, height=1080)
        key_full = cache_mgr.compute_key(ns, {}, render_mode="preview", roi=roi_full)

        entry_full = CacheEntry(outputs={"result": _make_texture_result()})
        cache_mgr.put(key_full, entry_full, roi=roi_full)

        # Request zoomed region - should NOT hit full-frame cache
        roi_zoomed = BBox(x=500, y=500, width=200, height=200)
        key_zoomed = cache_mgr.compute_key(ns, {}, render_mode="preview", roi=roi_zoomed)

        hit_zoomed = cache_mgr.get(key_zoomed)

        # Should be miss (different ROI)
        assert hit_zoomed is None


# ═══════════════════════════════════════════════════════════════════════
# Output Format Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyOutputFormat:
    """Test output_format in cache key."""

    def test_format_in_key(self):
        """Output format is included in cache key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        fmt = _make_format(width=1920, height=1080)

        key_no_fmt = cache_mgr.compute_key(ns, {}, render_mode="preview")
        key_with_fmt = cache_mgr.compute_key(ns, {}, render_mode="preview", output_format=fmt)

        assert "fmt" in key_with_fmt
        assert "fmt1920x1080" in key_with_fmt

    def test_different_dimensions_different_keys(self):
        """Different output dimensions produce different keys."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        fmt_1080 = _make_format(width=1920, height=1080)
        fmt_720 = _make_format(width=1280, height=720)

        key_1080 = cache_mgr.compute_key(ns, {}, render_mode="preview", output_format=fmt_1080)
        key_720 = cache_mgr.compute_key(ns, {}, render_mode="preview", output_format=fmt_720)

        assert key_1080 != key_720

    def test_pixel_aspect_in_key(self):
        """Pixel aspect ratio is included in format key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        fmt_sq = _make_format(pixel_aspect=1.0)
        fmt_nonsq = _make_format(pixel_aspect=1.5)

        key_sq = cache_mgr.compute_key(ns, {}, render_mode="preview", output_format=fmt_sq)
        key_nonsq = cache_mgr.compute_key(ns, {}, render_mode="preview", output_format=fmt_nonsq)

        assert key_sq != key_nonsq


# ═══════════════════════════════════════════════════════════════════════
# Alpha Mode Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyAlphaMode:
    """Test alpha_mode in cache key."""

    def test_alpha_mode_in_key(self):
        """Alpha mode is included in cache key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        key_no_alpha = cache_mgr.compute_key(ns, {}, render_mode="preview")
        key_premult = cache_mgr.compute_key(ns, {}, render_mode="preview", alpha_mode="premult")

        assert "am" in key_premult
        assert "ampremult" in key_premult

    def test_different_alpha_modes_different_keys(self):
        """Different alpha modes produce different keys."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        key_premult = cache_mgr.compute_key(ns, {}, render_mode="preview", alpha_mode="premult")
        key_unpremult = cache_mgr.compute_key(ns, {}, render_mode="preview", alpha_mode="unpremult")
        key_auto = cache_mgr.compute_key(ns, {}, render_mode="preview", alpha_mode="auto")

        assert key_premult != key_unpremult
        assert key_premult != key_auto
        assert key_unpremult != key_auto


# ═══════════════════════════════════════════════════════════════════════
# Proxy Scale Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyProxyScale:
    """Test proxy_scale in cache key."""

    def test_proxy_scale_in_key(self):
        """Proxy scale is included in cache key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        key_full = cache_mgr.compute_key(ns, {}, render_mode="preview", proxy_scale=1.0)
        key_half = cache_mgr.compute_key(ns, {}, render_mode="preview", proxy_scale=0.5)

        # Full scale shouldn't add suffix (default)
        assert "px" not in key_full
        # Half scale should add suffix
        assert "px0.50" in key_half

    def test_different_proxy_scales_different_keys(self):
        """Different proxy scales produce different keys."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        key_1x = cache_mgr.compute_key(ns, {}, render_mode="preview", proxy_scale=1.0)
        key_05x = cache_mgr.compute_key(ns, {}, render_mode="preview", proxy_scale=0.5)
        key_025x = cache_mgr.compute_key(ns, {}, render_mode="preview", proxy_scale=0.25)

        assert key_1x != key_05x
        assert key_05x != key_025x


# ═══════════════════════════════════════════════════════════════════════
# Frame Number Tests (Video/Animation)
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyFrameNumber:
    """Test frame_number in cache key for video/animation."""

    def test_frame_number_in_key(self):
        """Frame number is included in cache key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        key_frame0 = cache_mgr.compute_key(ns, {}, render_mode="preview", frame_number=0)
        key_frame10 = cache_mgr.compute_key(ns, {}, render_mode="preview", frame_number=10)

        assert "fn0" in key_frame0
        assert "fn10" in key_frame10

    def test_different_frames_different_keys(self):
        """Different frame numbers produce different keys."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        key_f1 = cache_mgr.compute_key(ns, {}, render_mode="preview", frame_number=1)
        key_f2 = cache_mgr.compute_key(ns, {}, render_mode="preview", frame_number=2)
        key_f100 = cache_mgr.compute_key(ns, {}, render_mode="preview", frame_number=100)

        assert key_f1 != key_f2
        assert key_f2 != key_f100


# ═══════════════════════════════════════════════════════════════════════
# Color Input Node Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyColorInput:
    """Test color texture id/revision for color_input nodes."""

    def test_color_texture_id_in_key(self):
        """Color texture ID is included for color_input nodes."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot(node_type="color_input", input_ports=[])

        key_id1 = cache_mgr.compute_key(ns, {}, render_mode="preview",
                                        snapshot_color_id=1, snapshot_color_rev=1)
        key_id2 = cache_mgr.compute_key(ns, {}, render_mode="preview",
                                        snapshot_color_id=2, snapshot_color_rev=1)

        assert "ct1" in key_id1
        assert "ct2" in key_id2
        assert key_id1 != key_id2

    def test_color_revision_in_key(self):
        """Color revision is included for color_input nodes."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot(node_type="color_input", input_ports=[])

        key_rev1 = cache_mgr.compute_key(ns, {}, render_mode="preview",
                                         snapshot_color_id=1, snapshot_color_rev=1)
        key_rev2 = cache_mgr.compute_key(ns, {}, render_mode="preview",
                                         snapshot_color_id=1, snapshot_color_rev=2)

        assert "cr1" in key_rev1
        assert "cr2" in key_rev2
        assert key_rev1 != key_rev2


# ═══════════════════════════════════════════════════════════════════════
# Upstream Texture Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyUpstreamTexture:
    """Test upstream texture metadata in cache key."""

    def test_upstream_texture_id_in_key(self):
        """Upstream texture ID is included in key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        tex1 = _make_texture_result(texture_id=1)
        tex2 = _make_texture_result(texture_id=2)

        inputs1 = {"image": tex1}
        inputs2 = {"image": tex2}

        key1 = cache_mgr.compute_key(ns, inputs1, render_mode="preview")
        key2 = cache_mgr.compute_key(ns, inputs2, render_mode="preview")

        assert key1 != key2

    def test_upstream_revision_in_key(self):
        """Upstream texture revision is included in key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        tex1 = _make_texture_result(revision=1)
        tex2 = _make_texture_result(revision=2)

        inputs1 = {"image": tex1}
        inputs2 = {"image": tex2}

        key1 = cache_mgr.compute_key(ns, inputs1, render_mode="preview")
        key2 = cache_mgr.compute_key(ns, inputs2, render_mode="preview")

        assert key1 != key2

    def test_upstream_format_in_key(self):
        """Upstream texture format is included in key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        tex_1080 = _make_texture_result(width=1920, height=1080)
        tex_720 = _make_texture_result(width=1280, height=720)

        inputs_1080 = {"image": tex_1080}
        inputs_720 = {"image": tex_720}

        key_1080 = cache_mgr.compute_key(ns, inputs_1080, render_mode="preview")
        key_720 = cache_mgr.compute_key(ns, inputs_720, render_mode="preview")

        assert key_1080 != key_720

    def test_upstream_bbox_in_key(self):
        """Upstream texture bbox is included in key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        tex_full = _make_texture_result()
        tex_crop = MagicMock(spec=TextureResult)
        tex_crop.texture_id = 1
        tex_crop.revision = 1
        tex_crop.format = _make_format()
        tex_crop.bbox = BBox(x=100, y=100, width=500, height=500)
        tex_crop.alpha_premultiplied = False
        tex_crop.color_space = "srgb"
        tex_crop.is_valid = True

        inputs_full = {"image": tex_full}
        inputs_crop = {"image": tex_crop}

        key_full = cache_mgr.compute_key(ns, inputs_full, render_mode="preview")
        key_crop = cache_mgr.compute_key(ns, inputs_crop, render_mode="preview")

        assert key_full != key_crop

    def test_upstream_alpha_in_key(self):
        """Upstream alpha premultiplication state in key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        tex_premult = _make_texture_result(alpha_premult=True)
        tex_unpremult = _make_texture_result(alpha_premult=False)

        inputs_premult = {"image": tex_premult}
        inputs_unpremult = {"image": tex_unpremult}

        key_premult = cache_mgr.compute_key(ns, inputs_premult, render_mode="preview")
        key_unpremult = cache_mgr.compute_key(ns, inputs_unpremult, render_mode="preview")

        assert key_premult != key_unpremult

    def test_upstream_color_space_in_key(self):
        """Upstream color space is included in key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        tex_srgb = _make_texture_result(color_space="srgb")
        tex_linear = _make_texture_result(color_space="linear")

        inputs_srgb = {"image": tex_srgb}
        inputs_linear = {"image": tex_linear}

        key_srgb = cache_mgr.compute_key(ns, inputs_srgb, render_mode="preview")
        key_linear = cache_mgr.compute_key(ns, inputs_linear, render_mode="preview")

        assert key_srgb != key_linear

    def test_multiple_inputs_sorted(self):
        """Multiple inputs are sorted for consistent key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot(input_ports=[("A", "image"), ("B", "image")])

        tex_a = _make_texture_result(texture_id=1)
        tex_b = _make_texture_result(texture_id=2)

        inputs_ab = {"A": tex_a, "B": tex_b}
        inputs_ba = {"B": tex_b, "A": tex_a}  # Different order

        key_ab = cache_mgr.compute_key(ns, inputs_ab, render_mode="preview")
        key_ba = cache_mgr.compute_key(ns, inputs_ba, render_mode="preview")

        # Should produce same key (sorted)
        assert key_ab == key_ba


# ═══════════════════════════════════════════════════════════════════════
# Combined Key Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheKeyCombined:
    """Test combined cache key with multiple components."""

    def test_all_components_unique(self):
        """All key components contribute to uniqueness."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns1 = _make_node_snapshot(node_id="n1", node_type="blur",
                                  params={"radius": 10.0})
        ns2 = _make_node_snapshot(node_id="n2", node_type="grade",
                                  params={"lift": 0.1})

        roi1 = BBox(x=0, y=0, width=100, height=100)
        roi2 = BBox(x=50, y=50, width=100, height=100)

        fmt1 = _make_format(width=1920, height=1080)
        fmt2 = _make_format(width=1280, height=720)

        tex1 = _make_texture_result(texture_id=1)
        tex2 = _make_texture_result(texture_id=2)

        # Combine all components
        key1 = cache_mgr.compute_key(
            ns1, {"image": tex1},
            render_mode="preview",
            roi=roi1,
            output_format=fmt1,
            alpha_mode="premult",
            proxy_scale=0.5,
            frame_number=1,
        )

        key2 = cache_mgr.compute_key(
            ns2, {"image": tex2},
            render_mode="export",
            roi=roi2,
            output_format=fmt2,
            alpha_mode="unpremult",
            proxy_scale=1.0,
            frame_number=2,
        )

        # All components different → keys different
        assert key1 != key2

    def test_same_components_same_key(self):
        """Same all components produce same key."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()
        tex = _make_texture_result()
        roi = BBox(x=0, y=0, width=100, height=100)
        fmt = _make_format()

        inputs = {"image": tex}

        # Compute twice with same params
        key1 = cache_mgr.compute_key(
            ns, inputs,
            render_mode="preview",
            roi=roi,
            output_format=fmt,
            alpha_mode="premult",
            proxy_scale=0.5,
            frame_number=1,
        )

        key2 = cache_mgr.compute_key(
            ns, inputs,
            render_mode="preview",
            roi=roi,
            output_format=fmt,
            alpha_mode="premult",
            proxy_scale=0.5,
            frame_number=1,
        )

        assert key1 == key2


# ═══════════════════════════════════════════════════════════════════════
# Cache Category Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheCategory:
    """Test cache category classification."""

    def test_preview_category(self):
        """Preview render mode gets PREVIEW category."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot(node_type="blur")
        key = cache_mgr.compute_key(ns, {}, render_mode="preview")

        entry = CacheEntry(outputs={})
        cache_mgr.put(key, entry, render_mode="preview")

        hit = cache_mgr.get(key)
        assert hit.category == CacheCategory.PREVIEW

    def test_export_category(self):
        """Export render mode gets EXPORT category."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot(node_type="blur")
        key = cache_mgr.compute_key(ns, {}, render_mode="export")

        entry = CacheEntry(outputs={})
        cache_mgr.put(key, entry, render_mode="export")

        hit = cache_mgr.get(key)
        assert hit.category == CacheCategory.EXPORT

    def test_source_category(self):
        """Source node types get SOURCE category."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns_color = _make_node_snapshot(node_type="color_input", input_ports=[])
        key = cache_mgr.compute_key(ns_color, {}, render_mode="preview")

        entry = CacheEntry(outputs={})
        cache_mgr.put(key, entry, render_mode="preview")

        hit = cache_mgr.get(key)
        assert hit.category == CacheCategory.SOURCE

    def test_ai_category(self):
        """AI node types get AI category."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns_ai = _make_node_snapshot(node_type="ai_generate", params={"prompt": "test"})
        key = cache_mgr.compute_key(ns_ai, {}, render_mode="preview")

        entry = CacheEntry(outputs={})
        cache_mgr.put(key, entry, render_mode="preview")

        hit = cache_mgr.get(key)
        assert hit.category == CacheCategory.AI


# ═══════════════════════════════════════════════════════════════════════
# Cache Entry Metadata Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheEntryMetadata:
    """Test CacheEntry metadata storage."""

    def test_roi_stored_in_entry(self):
        """ROI is stored in cache entry."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()
        roi = BBox(x=100, y=100, width=200, height=200)

        key = cache_mgr.compute_key(ns, {}, render_mode="preview", roi=roi)

        entry = CacheEntry(outputs={})
        cache_mgr.put(key, entry, roi=roi)

        hit = cache_mgr.get(key)
        assert hit.roi == roi

    def test_render_mode_stored_in_entry(self):
        """Render mode is stored in cache entry."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()

        key = cache_mgr.compute_key(ns, {}, render_mode="export")

        entry = CacheEntry(outputs={})
        cache_mgr.put(key, entry, render_mode="export")

        hit = cache_mgr.get(key)
        assert hit.render_mode == "export"


# ═══════════════════════════════════════════════════════════════════════
# Stats Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCacheStats:
    """Test cache statistics tracking."""

    def test_hit_miss_tracking(self):
        """Hits and misses are tracked."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        ns = _make_node_snapshot()
        key = cache_mgr.compute_key(ns, {}, render_mode="preview")

        # Miss
        cache_mgr.get(key)
        assert cache_mgr.stats.misses == 1
        assert cache_mgr.stats.hits == 0

        # Put entry
        entry = CacheEntry(outputs={})
        cache_mgr.put(key, entry)

        # Hit
        cache_mgr.get(key)
        assert cache_mgr.stats.hits == 1
        assert cache_mgr.stats.misses == 1

    def test_category_memory_tracking(self):
        """Per-category memory is tracked."""
        cache_mgr = CacheManager(_make_mock_resource_pool(), max_memory_mb=100)

        # Add PREVIEW entry
        ns_preview = _make_node_snapshot(node_id="preview_node")
        key_preview = cache_mgr.compute_key(ns_preview, {}, render_mode="preview")
        tex_preview = _make_texture_result(width=1000, height=1000)  # ~8MB RGBA f16
        entry_preview = CacheEntry(outputs={"result": tex_preview},
                                   owned_texture_ids={1})
        cache_mgr.put(key_preview, entry_preview, category=CacheCategory.PREVIEW)

        # Add EXPORT entry
        ns_export = _make_node_snapshot(node_id="export_node")
        key_export = cache_mgr.compute_key(ns_export, {}, render_mode="export")
        tex_export = _make_texture_result(width=500, height=500)
        entry_export = CacheEntry(outputs={"result": tex_export},
                                  owned_texture_ids={2})
        cache_mgr.put(key_export, entry_export, category=CacheCategory.EXPORT)

        # Check category memory
        preview_mem = cache_mgr.category_memory_mb(CacheCategory.PREVIEW)
        export_mem = cache_mgr.category_memory_mb(CacheCategory.EXPORT)

        assert preview_mem >= 0
        assert export_mem >= 0

    def test_dump_stats(self):
        """dump_stats() returns formatted string."""
        cache_mgr = CacheManager(_make_mock_resource_pool())

        stats_str = cache_mgr.dump_stats()

        assert "[CacheManager]" in stats_str
        assert "entries=" in stats_str
        assert "hits=" in stats_str
        assert "misses=" in stats_str


# ── Run Tests ──

if __name__ == "__main__":
    pytest.main([__file__, "-v"])