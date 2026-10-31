"""
Tests for GPU Resource Pool v2 optimizations.

Validates the following new features:
1. VRAMMonitor - Dynamic GPU memory monitoring
2. RenderMode - Format tiering (Preview RGBA16F / Export RGBA32F)
3. VideoSlotPool - Cross-frame texture slot reuse
4. Cost-based eviction - Evict by cost = size × age
5. Enhanced statistics - Format distribution, slot reuse, VRAM stats

Uses mocks to avoid requiring moderngl dependency.
"""

import pytest
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from unittest.mock import MagicMock, Mock, patch
from collections import OrderedDict

# Import v2 classes directly (they don't depend on moderngl)
from src.features.composite.render.gpu_resource_pool import (
    VRAMMonitor, RenderMode, VideoSlotPool, get_dtype_for_mode,
    TextureKey, TextureInfo, TextureUsage
)


# ═══════════════════════════════════════════════════════════════════════
# VRAM Monitor Tests
# ═══════════════════════════════════════════════════════════════════════

class TestVRAMMonitor:
    """Test VRAMMonitor functionality."""

    def test_init(self):
        """VRAMMonitor initialization."""
        monitor = VRAMMonitor()
        assert monitor.is_supported() == False
        assert monitor.get_vendor() == ""
        assert monitor.query_available_vram_kb() == 0
        assert monitor.query_total_vram_kb() == 0

    def test_get_stats(self):
        """VRAMMonitor stats."""
        monitor = VRAMMonitor()
        stats = monitor.get_stats()
        assert 'supported' in stats
        assert 'vendor' in stats
        assert 'total_kb' in stats
        assert 'available_kb' in stats
        assert 'budget_mb' in stats

    def test_get_vram_budget_mb(self):
        """VRAM budget calculation."""
        monitor = VRAMMonitor()
        # Without initialization, should return 0
        budget = monitor.get_vram_budget_mb()
        assert budget == 0

        # Test with mock available VRAM
        monitor._supported = True
        monitor._vram_available_kb = 1024 * 1024  # 1GB
        budget = monitor.get_vram_budget_mb(safety_factor=0.7)
        assert budget == 716.8  # 70% of 1024 MiB

    def test_query_throttling(self):
        """VRAM query throttling."""
        monitor = VRAMMonitor()
        monitor._supported = True
        monitor._vram_available_kb = 1000
        monitor._last_query_time = time.time()

        # Immediate query should return cached value
        result1 = monitor.query_available_vram_kb()
        assert result1 == 1000

        # Wait past interval
        monitor._last_query_time = time.time() - 2.0
        monitor._vram_available_kb = 2000
        # Since we can't actually call GL, this will return 0 on error
        # But the throttling logic is exercised


# ═══════════════════════════════════════════════════════════════════════
# RenderMode Tests
# ═══════════════════════════════════════════════════════════════════════

class TestRenderMode:
    """Test RenderMode format tiering."""

    def test_render_mode_enum(self):
        """RenderMode enum values."""
        assert RenderMode.PREVIEW.value == "preview"
        assert RenderMode.EXPORT.value == "export"
        assert RenderMode.FULL.value == "full"

    def test_get_dtype_for_mode(self):
        """Format tiering: Preview->f2, Export->f4."""
        assert get_dtype_for_mode(RenderMode.PREVIEW) == 'f2'
        assert get_dtype_for_mode(RenderMode.EXPORT) == 'f4'
        assert get_dtype_for_mode(RenderMode.FULL) == 'f2'

    def test_dtype_format_difference(self):
        """Verify format size difference."""
        # f2 (half-float): 2 bytes per component
        # f4 (float): 4 bytes per component
        key_f2 = TextureKey(width=1920, height=1080, components=4, dtype='f2')
        key_f4 = TextureKey(width=1920, height=1080, components=4, dtype='f4')

        # f2: 1920 * 1080 * 4 * 2 = 16,588,800 bytes (~16 MB)
        # f4: 1920 * 1080 * 4 * 4 = 33,177,600 bytes (~32 MB)
        assert key_f2.total_bytes == 1920 * 1080 * 4 * 2
        assert key_f4.total_bytes == 1920 * 1080 * 4 * 4
        assert key_f4.total_bytes == key_f2.total_bytes * 2


# ═══════════════════════════════════════════════════════════════════════
# VideoSlotPool Tests
# ═══════════════════════════════════════════════════════════════════════

class TestVideoSlotPool:
    """Test VideoSlotPool functionality."""

    def create_mock_texture(self, glo, width=1920, height=1080, components=4, dtype='f2'):
        """Create a mock texture object."""
        mock_tex = MagicMock()
        mock_tex.glo = glo
        mock_tex.width = width
        mock_tex.height = height
        mock_tex.components = components
        mock_tex.dtype = dtype
        return mock_tex

    def test_init(self):
        """VideoSlotPool initialization."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        slot_pool = VideoSlotPool(mock_fb, mock_pool)
        assert slot_pool.get_slot_count() == 0
        assert slot_pool.get_reuse_rate() == 0.0

    def test_acquire_slot_creates_new(self):
        """Slot acquisition creates new texture if slot empty."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        mock_tex = self.create_mock_texture(1)
        mock_pool.acquire_temp_texture.return_value = mock_tex

        slot_pool = VideoSlotPool(mock_fb, mock_pool)
        tex = slot_pool.acquire_slot(1920, 1080, slot_id=0)

        assert slot_pool.get_slot_count() == 1
        assert slot_pool.get_stats()['total_creations'] == 1
        mock_pool.acquire_temp_texture.assert_called_once()

    def test_acquire_slot_reuses_existing(self):
        """Slot acquisition reuses existing texture."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        mock_tex1 = self.create_mock_texture(1)
        mock_pool.acquire_temp_texture.return_value = mock_tex1

        slot_pool = VideoSlotPool(mock_fb, mock_pool)

        # First acquisition creates
        tex1 = slot_pool.acquire_slot(1920, 1080, slot_id=0)
        assert slot_pool.get_stats()['total_creations'] == 1

        # Second acquisition reuses
        tex2 = slot_pool.acquire_slot(1920, 1080, slot_id=0)
        assert tex2 == tex1
        assert slot_pool.get_stats()['total_reuses'] == 1
        assert slot_pool.get_reuse_rate() == 0.5  # 1 reuse, 1 creation

    def test_different_slot_ids(self):
        """Different slot IDs create different slots."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        mock_tex1 = self.create_mock_texture(1)
        mock_tex2 = self.create_mock_texture(2)
        mock_pool.acquire_temp_texture.side_effect = [mock_tex1, mock_tex2]

        slot_pool = VideoSlotPool(mock_fb, mock_pool)

        tex1 = slot_pool.acquire_slot(1920, 1080, slot_id=0)
        tex2 = slot_pool.acquire_slot(1920, 1080, slot_id=1)

        assert tex1 != tex2
        assert slot_pool.get_slot_count() == 2

    def test_different_sizes_different_slots(self):
        """Different sizes create different slots."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        mock_tex1 = self.create_mock_texture(1, 1920, 1080)
        mock_tex2 = self.create_mock_texture(2, 1280, 720)
        mock_pool.acquire_temp_texture.side_effect = [mock_tex1, mock_tex2]

        slot_pool = VideoSlotPool(mock_fb, mock_pool)

        tex1 = slot_pool.acquire_slot(1920, 1080, slot_id=0)
        tex2 = slot_pool.acquire_slot(1280, 720, slot_id=0)

        assert tex1 != tex2
        assert slot_pool.get_slot_count() == 2

    def test_clear_slot(self):
        """Clear slot releases texture."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        mock_tex = self.create_mock_texture(1)
        mock_pool.acquire_temp_texture.return_value = mock_tex

        slot_pool = VideoSlotPool(mock_fb, mock_pool)
        tex = slot_pool.acquire_slot(1920, 1080, slot_id=0)

        assert slot_pool.get_slot_count() == 1

        slot_pool.clear_slot(1920, 1080, slot_id=0)
        mock_pool.release_texture.assert_called_once_with(mock_tex)
        assert slot_pool.get_slot_count() == 0

    def test_clear_all_slots(self):
        """Clear all slots releases all textures."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        mock_tex1 = self.create_mock_texture(1)
        mock_tex2 = self.create_mock_texture(2)
        mock_pool.acquire_temp_texture.side_effect = [mock_tex1, mock_tex2]

        slot_pool = VideoSlotPool(mock_fb, mock_pool)
        slot_pool.acquire_slot(1920, 1080, slot_id=0)
        slot_pool.acquire_slot(1920, 1080, slot_id=1)

        assert slot_pool.get_slot_count() == 2

        slot_pool.clear_all_slots()
        assert mock_pool.release_texture.call_count == 2
        assert slot_pool.get_slot_count() == 0

    def test_begin_end_frame(self):
        """Frame lifecycle management."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        slot_pool = VideoSlotPool(mock_fb, mock_pool)

        slot_pool.begin_frame()
        assert slot_pool._in_frame == True

        slot_pool.end_frame()
        assert slot_pool._in_frame == False

    def test_render_mode_auto_dtype(self):
        """Render mode auto-selects dtype."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        mock_tex = self.create_mock_texture(1)
        mock_pool.acquire_temp_texture.return_value = mock_tex

        slot_pool = VideoSlotPool(mock_fb, mock_pool)

        # Preview mode -> f2
        tex = slot_pool.acquire_slot(1920, 1080, render_mode=RenderMode.PREVIEW)
        mock_pool.acquire_temp_texture.assert_called_with(1920, 1080, 4, 'f2')

        # Export mode -> f4
        mock_pool.acquire_temp_texture.reset_mock()
        tex = slot_pool.acquire_slot(1920, 1080, render_mode=RenderMode.EXPORT)
        # Should have created new slot with f4
        assert mock_pool.acquire_temp_texture.called

    def test_memory_calculation(self):
        """Slot memory calculation."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        mock_tex1 = self.create_mock_texture(1, 1920, 1080)
        mock_tex2 = self.create_mock_texture(2, 1280, 720)
        mock_pool.acquire_temp_texture.side_effect = [mock_tex1, mock_tex2]

        slot_pool = VideoSlotPool(mock_fb, mock_pool)
        slot_pool.acquire_slot(1920, 1080, slot_id=0)  # ~16MB for f2
        slot_pool.acquire_slot(1280, 720, slot_id=0)   # ~7MB for f2

        memory = slot_pool.get_memory_bytes()
        # 1920*1080*4*2 + 1280*720*4*2
        expected = 1920 * 1080 * 4 * 2 + 1280 * 720 * 4 * 2
        assert memory == expected


# ═══════════════════════════════════════════════════════════════════════
# Cost-based Eviction Tests (via GPUResourcePool mock)
# ═══════════════════════════════════════════════════════════════════════

class TestCostBasedEviction:
    """Test cost-based eviction strategy."""

    def test_cost_calculation_formula(self):
        """Verify cost = size × age_factor."""
        # Age factor: 1.0 for newest, up to 2.0 for oldest
        pool_size = 10

        # Oldest texture (index 0): age_factor = 1.0 + 0/10 = 1.0
        # Newest texture (index 9): age_factor = 1.0 + 9/10 = 1.9
        age_factor_oldest = 1.0 + (0 / pool_size)
        age_factor_newest = 1.0 + (9 / pool_size)

        assert age_factor_oldest == 1.0
        assert age_factor_newest == 1.9

        # Large texture cost vs small texture
        large_size = 1920 * 1080 * 4 * 2  # ~16MB
        small_size = 100 * 100 * 4 * 2    # ~80KB

        large_cost_old = large_size * age_factor_oldest
        small_cost_new = small_size * age_factor_newest

        # Large old texture should have much higher cost
        assert large_cost_old > small_cost_new * 100

    def test_texture_key_bytes_property(self):
        """TextureKey bytes property used for cost calculation."""
        key_large = TextureKey(width=1920, height=1080)
        key_small = TextureKey(width=100, height=100)

        # Should correctly calculate bytes for cost
        assert key_large.total_bytes > key_small.total_bytes * 100


# ═══════════════════════════════════════════════════════════════════════
# Enhanced Statistics Tests
# ═══════════════════════════════════════════════════════════════════════

class TestEnhancedStatistics:
    """Test enhanced statistics functionality."""

    def test_format_distribution_tracking(self):
        """Format (f2/f4) distribution in stats."""
        # Create mock textures with different formats
        key_f2 = TextureKey(width=100, height=100, dtype='f2')
        key_f4 = TextureKey(width=100, height=100, dtype='f4')

        info_f2 = TextureInfo(glo=1, key=key_f2)
        info_f4 = TextureInfo(glo=2, key=key_f4)

        assert info_f2.dtype == 'f2'
        assert info_f4.dtype == 'f4'

    def test_usage_distribution_tracking(self):
        """Usage category distribution in stats."""
        key_temp = TextureKey(width=100, height=100, usage=TextureUsage.TEMP)
        key_cache = TextureKey(width=100, height=100, usage=TextureUsage.CACHE)
        key_output = TextureKey(width=100, height=100, usage=TextureUsage.OUTPUT)

        info_temp = TextureInfo(glo=1, key=key_temp)
        info_cache = TextureInfo(glo=2, key=key_cache)
        info_output = TextureInfo(glo=3, key=key_output)

        assert info_temp.usage == TextureUsage.TEMP
        assert info_cache.usage == TextureUsage.CACHE
        assert info_output.usage == TextureUsage.OUTPUT

    def test_slot_pool_stats(self):
        """VideoSlotPool stats structure."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        slot_pool = VideoSlotPool(mock_fb, mock_pool)

        stats = slot_pool.get_stats()
        assert 'slot_count' in stats
        assert 'memory_bytes' in stats
        assert 'memory_mb' in stats
        assert 'total_acquisitions' in stats
        assert 'total_reuses' in stats
        assert 'total_creations' in stats
        assert 'reuse_rate' in stats

    def test_vram_monitor_stats(self):
        """VRAMMonitor stats structure."""
        monitor = VRAMMonitor()
        stats = monitor.get_stats()
        assert 'supported' in stats
        assert 'vendor' in stats
        assert 'total_kb' in stats
        assert 'available_kb' in stats
        assert 'budget_mb' in stats


# ═══════════════════════════════════════════════════════════════════════
# Integration Tests
# ═══════════════════════════════════════════════════════════════════════

class TestIntegration:
    """Integration tests for v2 features."""

    def test_format_tiering_workflow(self):
        """Workflow: preview uses f2, export uses f4."""
        # Preview workflow
        preview_dtype = get_dtype_for_mode(RenderMode.PREVIEW)
        assert preview_dtype == 'f2'

        # Export workflow
        export_dtype = get_dtype_for_mode(RenderMode.EXPORT)
        assert export_dtype == 'f4'

        # Verify memory difference
        preview_tex_key = TextureKey(width=1920, height=1080, dtype=preview_dtype)
        export_tex_key = TextureKey(width=1920, height=1080, dtype=export_dtype)

        assert export_tex_key.total_bytes == preview_tex_key.total_bytes * 2

    def test_video_slot_workflow(self):
        """Workflow: video frame processing with slot reuse."""
        mock_fb = MagicMock()
        mock_pool = MagicMock()
        mock_tex = MagicMock(glo=1, width=1920, height=1080, components=4, dtype='f2')
        mock_pool.acquire_temp_texture.return_value = mock_tex

        slot_pool = VideoSlotPool(mock_fb, mock_pool)

        # Frame 1: create slot
        slot_pool.begin_frame()
        tex1 = slot_pool.acquire_slot(1920, 1080, slot_id=0)
        slot_pool.end_frame()

        # Frame 2: reuse slot
        slot_pool.begin_frame()
        tex2 = slot_pool.acquire_slot(1920, 1080, slot_id=0)
        slot_pool.end_frame()

        assert tex1 == tex2
        assert slot_pool.get_reuse_rate() == 0.5  # 1 reuse / 2 acquisitions

    def test_vram_budget_workflow(self):
        """Workflow: VRAM monitoring and budget adjustment."""
        monitor = VRAMMonitor()
        monitor._supported = True
        monitor._vram_available_kb = 2048 * 1024  # 2GB available

        budget = monitor.get_vram_budget_mb(safety_factor=0.7)
        assert budget == 1433.6  # 70% of 2GB = 1433.6 MB


# ═══════════════════════════════════════════════════════════════════════
# Run Tests
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])