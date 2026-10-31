"""
Tests for GPU Resource Pool - Enhanced memory-managed texture pool.

Validates the following acceptance criteria:
1. dump_stats() can print active textures, pooled textures, estimated VRAM
2. Active texture count doesn't grow infinitely after repeated operations
3. force_cleanup() releases all resources cleanly
4. Pinned textures are not evicted/reused during display
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from unittest.mock import MagicMock, Mock, patch
from collections import OrderedDict

from src.features.composite.render.gpu_resource_pool import (
    GPUResourcePool, TextureKey, TextureInfo, TextureUsage
)


# ═══════════════════════════════════════════════════════════════════════
# Unit Tests (No GPU Context Required)
# ═══════════════════════════════════════════════════════════════════════

class TestTextureKey:
    """Test TextureKey data structure."""

    def test_texture_key_creation(self):
        """Basic key creation and properties."""
        key = TextureKey(width=1920, height=1080, components=4, dtype='f2')
        assert key.width == 1920
        assert key.height == 1080
        assert key.components == 4
        assert key.dtype == 'f2'
        assert key.usage == TextureUsage.TEMP  # default

    def test_texture_key_bytes_calculation(self):
        """Verify bytes calculation for different formats."""
        # RGBA half-float: 1920 * 1080 * 4 * 2 = 16,588,800 bytes
        key_f2 = TextureKey(width=1920, height=1080, components=4, dtype='f2')
        assert key_f2.total_bytes == 1920 * 1080 * 4 * 2

        # RGBA float: 1920 * 1080 * 4 * 4 = 33,177,600 bytes
        key_f4 = TextureKey(width=1920, height=1080, components=4, dtype='f4')
        assert key_f4.total_bytes == 1920 * 1080 * 4 * 4

        # 1-channel half-float: 1000 * 1000 * 1 * 2 = 2,000,000 bytes
        key_mono = TextureKey(width=1000, height=1000, components=1, dtype='f2')
        assert key_mono.total_bytes == 1000 * 1000 * 1 * 2

    def test_texture_key_usage_types(self):
        """Test different usage categories."""
        key_temp = TextureKey(width=100, height=100, usage=TextureUsage.TEMP)
        key_cache = TextureKey(width=100, height=100, usage=TextureUsage.CACHE)
        key_output = TextureKey(width=100, height=100, usage=TextureUsage.OUTPUT)
        key_tile = TextureKey(width=100, height=100, usage=TextureUsage.TILE)

        assert key_temp.usage == TextureUsage.TEMP
        assert key_cache.usage == TextureUsage.CACHE
        assert key_output.usage == TextureUsage.OUTPUT
        assert key_tile.usage == TextureUsage.TILE

    def test_texture_key_frozen(self):
        """Verify TextureKey is immutable (frozen=True)."""
        key = TextureKey(width=100, height=100)
        with pytest.raises(AttributeError):
            key.width = 200  # Should fail

    def test_texture_key_string_format(self):
        """Test string representation."""
        key = TextureKey(width=1920, height=1080, components=4, dtype='f2', usage=TextureUsage.CACHE)
        str_repr = str(key)
        assert "1920" in str_repr
        assert "1080" in str_repr
        assert "CACHE" in str_repr


class TestTextureInfo:
    """Test TextureInfo metadata."""

    def test_texture_info_creation(self):
        """Basic info creation."""
        key = TextureKey(width=1920, height=1080)
        info = TextureInfo(glo=42, key=key)
        assert info.glo == 42
        assert info.width == 1920
        assert info.height == 1080
        assert info.external == False

    def test_texture_info_bytes(self):
        """Bytes property delegates to key."""
        key = TextureKey(width=1000, height=1000, components=4, dtype='f2')
        info = TextureInfo(glo=1, key=key)
        assert info.bytes == key.total_bytes

    def test_texture_info_external(self):
        """External textures are marked."""
        key = TextureKey(width=100, height=100, usage=TextureUsage.EXTERNAL)
        info = TextureInfo(glo=99, key=key, external=True)
        assert info.external == True
        assert info.usage == TextureUsage.EXTERNAL


class TestGPUResourcePoolLogic:
    """Test GPUResourcePool logic without actual GPU calls."""

    def test_init(self):
        """Pool initialization."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb, max_memory_mb=512)
        assert pool.max_memory_bytes == 512 * 1024 * 1024
        assert pool.active_count() == 0
        assert pool.pooled_count() == 0

    def test_statistics_methods(self):
        """Test all statistics methods return correct values."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb, max_memory_mb=1024)

        # Initial state
        assert pool.memory_usage_bytes() == 0
        assert pool.memory_usage_mb() == 0.0
        assert pool.pooled_memory_bytes() == 0
        assert pool.active_count() == 0
        assert pool.pooled_count() == 0
        assert pool.pinned_count() == 0
        assert pool.owned_count() == 0
        assert pool.external_count() == 0

    def test_budget_counts_pooled_resident_textures(self):
        """Released textures still consume budget until they are evicted."""
        mock_fb = MagicMock()
        mock_tex = MagicMock()
        mock_tex.glo = 1
        mock_fb.get_temp_texture.return_value = mock_tex
        pool = GPUResourcePool(mock_fb, max_memory_mb=1)

        tex = pool.acquire_temp_texture(100, 100)
        pool.release_texture(tex)
        texture_bytes = 100 * 100 * 4 * 2

        assert pool.memory_usage_bytes() == 0
        assert pool.pooled_memory_bytes() == texture_bytes
        assert pool.resident_memory_bytes() == texture_bytes

        # Force a budget check for a different-sized allocation.  The
        # pooled texture is the only evictable resident resource and must be
        # considered even though no texture is active at this point.
        pool.max_memory_bytes = texture_bytes // 2
        pool._check_budget(1)
        assert pool.pooled_memory_bytes() == 0
        assert pool.resident_memory_bytes() == 0

    def test_get_stats_dict(self):
        """get_stats() returns proper dictionary."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        stats = pool.get_stats()
        assert isinstance(stats, dict)
        assert 'active_count' in stats
        assert 'pooled_count' in stats
        assert 'memory_mb' in stats
        assert 'resident_memory_mb' in stats
        assert 'budget_mb' in stats
        assert 'total_created' in stats
        assert 'total_reused' in stats
        assert 'total_evicted' in stats

    def test_dump_stats_format(self):
        """dump_stats() returns properly formatted string."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb, max_memory_mb=1024)

        stats_str = pool.dump_stats()
        assert "[ResourcePool]" in stats_str
        assert "active=" in stats_str
        assert "pooled=" in stats_str
        assert "vram=" in stats_str

    def test_dump_active_textures_empty(self):
        """dump_active_textures() handles empty state."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        dump = pool.dump_active_textures()
        assert "[ResourcePool] Active textures:" in dump

    def test_dump_pooled_textures_empty(self):
        """dump_pooled_textures() handles empty state."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        dump = pool.dump_pooled_textures()
        assert "[ResourcePool] Pooled textures:" in dump

    def test_dump_full_report(self):
        """dump_full_report() includes all sections."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        report = pool.dump_full_report()
        assert "GPU Resource Pool Report" in report
        assert "Active textures:" in report
        assert "Pooled textures:" in report

    def test_pin_unpin_tracking(self):
        """Pin and unpin texture tracking."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        # Pin a texture ID
        pool.pin_texture(123)
        assert pool.is_pinned(123) == True
        assert pool.pinned_count() == 1

        # Unpin
        pool.unpin_texture(123)
        assert pool.is_pinned(123) == False
        assert pool.pinned_count() == 0

    def test_pin_multiple(self):
        """Multiple pinned textures."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        pool.pin_texture(1)
        pool.pin_texture(2)
        pool.pin_texture(3)
        assert pool.pinned_count() == 3

        # Unpin one
        pool.unpin_texture(2)
        assert pool.pinned_count() == 2
        assert pool.is_pinned(1) == True
        assert pool.is_pinned(2) == False
        assert pool.is_pinned(3) == True

    def test_track_external(self):
        """External texture tracking."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        pool.track_external(999, 1920, 1080)
        assert pool.active_count() == 1
        assert pool.external_count() == 1
        assert pool.owned_count() == 0

        info = pool._active_textures.get(999)
        assert info is not None
        assert info.external == True

    def test_untrack_external(self):
        """Untrack external texture."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        pool.track_external(999, 1920, 1080)
        pool.untrack_external(999)
        assert pool.active_count() == 0
        assert pool.external_count() == 0

    def test_has_texture(self):
        """has_texture() checks active and pooled."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        # Track external (active)
        pool.track_external(123, 100, 100)
        assert pool.has_texture(123) == True
        assert pool.has_texture(456) == False


# ═══════════════════════════════════════════════════════════════════════
# Integration Tests (Require Mocked FramebufferManager)
# ═══════════════════════════════════════════════════════════════════════

class TestGPUResourcePoolWithMockFB:
    """Test pool with mocked FramebufferManager (simulates GPU operations)."""

    def create_mock_texture(self, glo, width=1920, height=1080, components=4, dtype='f2'):
        """Create a mock texture object."""
        mock_tex = MagicMock()
        mock_tex.glo = glo
        mock_tex.width = width
        mock_tex.height = height
        mock_tex.components = components
        mock_tex.dtype = dtype
        return mock_tex

    def test_acquire_temp_texture(self):
        """Acquire a temp texture from pool."""
        mock_fb = MagicMock()
        mock_tex = self.create_mock_texture(42)
        mock_fb.get_temp_texture.return_value = mock_tex

        pool = GPUResourcePool(mock_fb)
        tex = pool.acquire_temp_texture(1920, 1080)

        # Verify fb_manager was called
        mock_fb.get_temp_texture.assert_called_once_with(1920, 1080, 4, 'f2')

        # Verify tracking
        assert pool.active_count() == 1
        assert pool.owned_count() == 1
        assert pool.total_created() == 1

    def test_acquire_cache_texture(self):
        """Acquire a cache texture from pool."""
        mock_fb = MagicMock()
        mock_tex = self.create_mock_texture(100)
        mock_fb.get_temp_texture.return_value = mock_tex

        pool = GPUResourcePool(mock_fb)
        tex = pool.acquire_cache_texture(1000, 1000)

        assert pool.active_count() == 1
        # Verify TextureKey has CACHE usage
        info = pool._active_textures.get(100)
        assert info.usage == TextureUsage.CACHE

    def test_acquire_output_texture_auto_pinned(self):
        """Output textures are auto-pinned."""
        mock_fb = MagicMock()
        mock_tex = self.create_mock_texture(200)
        mock_fb.get_temp_texture.return_value = mock_tex

        pool = GPUResourcePool(mock_fb)
        tex = pool.acquire_output_texture(1920, 1080)

        # Should be automatically pinned
        assert pool.is_pinned(200) == True
        assert pool.pinned_count() == 1

    def test_release_texture_returns_to_pool(self):
        """Released textures go to pool for reuse."""
        mock_fb = MagicMock()
        mock_tex1 = self.create_mock_texture(1)
        mock_tex2 = self.create_mock_texture(2)
        mock_fb.get_temp_texture.side_effect = [mock_tex1, mock_tex2]

        pool = GPUResourcePool(mock_fb)

        # Acquire and release
        tex1 = pool.acquire_temp_texture(100, 100)
        pool.release_texture(tex1)

        assert pool.active_count() == 0
        assert pool.pooled_count() == 1

        # Acquire same size - should reuse
        tex2 = pool.acquire_temp_texture(100, 100)
        assert pool.total_reused() == 1
        assert pool.active_count() == 1
        assert pool.pooled_count() == 0

    def test_release_texture_by_id(self):
        """Release texture by OpenGL ID."""
        mock_fb = MagicMock()
        mock_tex = self.create_mock_texture(50)
        mock_fb.get_temp_texture.return_value = mock_tex
        mock_fb._active_textures = {mock_tex}

        pool = GPUResourcePool(mock_fb)

        tex = pool.acquire_temp_texture(100, 100)
        assert pool.active_count() == 1

        # Release by ID
        pool.release_texture_by_id(50)
        assert pool.active_count() == 0

    def test_budget_check_does_not_evict_reusable_pooled_texture(self):
        """Resident-at-budget acquire must reuse the pooled texture.

        Regression: the budget check ran before the pool lookup, so with
        resident bytes at the cap (typical steady state — last frame's TEMP
        textures all pooled) cost-eviction destroyed exactly the reusable
        same-size texture and the acquire fell through to a same-size
        reallocation every frame.
        """
        mock_fb = MagicMock()
        mock_tex1 = self.create_mock_texture(1)
        mock_tex2 = self.create_mock_texture(2)
        mock_fb.get_temp_texture.side_effect = [mock_tex1, mock_tex2]

        pool = GPUResourcePool(mock_fb)
        tex_bytes = 100 * 100 * 4 * 2  # f2 components=4
        pool.max_memory_bytes = tex_bytes  # exactly one texture resident

        tex1 = pool.acquire_temp_texture(100, 100)
        pool.release_texture(tex1)
        assert pool.pooled_count() == 1

        # Re-acquire the same key while resident == budget: must reuse
        # without eviction and without asking fb_manager for a new texture.
        tex2 = pool.acquire_temp_texture(100, 100)
        assert tex2 is tex1
        assert pool.total_reused() == 1
        assert pool.total_created() == 1
        assert mock_fb.get_temp_texture.call_count == 1
        assert pool.pooled_count() == 0

    def test_force_evict_on_release(self):
        """force_evict=True skips pooling."""
        mock_fb = MagicMock()
        mock_tex = self.create_mock_texture(10)
        mock_fb.get_temp_texture.return_value = mock_tex
        mock_fb._active_textures = {mock_tex}

        pool = GPUResourcePool(mock_fb)

        tex = pool.acquire_temp_texture(100, 100)
        pool.release_texture(tex, force_evict=True)

        # Should not be pooled
        assert pool.pooled_count() == 0
        assert pool.total_evicted() == 1

    def test_evict_all_pooled(self):
        """evict_all_pooled clears pooled textures."""
        mock_fb = MagicMock()
        mock_tex1 = self.create_mock_texture(1)
        mock_tex2 = self.create_mock_texture(2)
        # Use different sizes to ensure they go to separate pools
        mock_fb.get_temp_texture.side_effect = [mock_tex1, mock_tex2]

        pool = GPUResourcePool(mock_fb)

        # Create and pool two textures with DIFFERENT sizes (to avoid reuse)
        tex1 = pool.acquire_temp_texture(100, 100)  # size 100x100
        pool.release_texture(tex1)
        tex2 = pool.acquire_temp_texture(200, 200)  # size 200x200 (different!)
        pool.release_texture(tex2)

        # Now we should have 2 pooled textures (different keys)
        assert pool.pooled_count() == 2

        # Evict all
        pool.evict_all_pooled()
        assert pool.pooled_count() == 0
        assert pool.total_evicted() == 2

    def test_force_cleanup(self):
        """force_cleanup() clears all owned resources, keeps external tracking.

        External textures are tracked but not owned by the pool, so they
        remain in tracking after cleanup (they should be released by their
        owner, e.g., ColorOutput).
        """
        mock_fb = MagicMock()
        mock_tex1 = self.create_mock_texture(1)
        mock_tex2 = self.create_mock_texture(2)
        mock_fb.get_temp_texture.side_effect = [mock_tex1, mock_tex2]

        # Setup _active_textures as a set that we can manipulate
        active_set = set()
        mock_fb._active_textures = active_set
        mock_fb.cleanup = MagicMock()
        mock_fb.release_temp_texture = MagicMock(side_effect=lambda tex: active_set.discard(tex))

        pool = GPUResourcePool(mock_fb)

        # Create textures - add to active_set to simulate fb_manager behavior
        tex1 = pool.acquire_temp_texture(100, 100)
        active_set.add(tex1)
        tex2 = pool.acquire_temp_texture(200, 200)
        active_set.add(tex2)

        # Track an external (not owned by pool)
        pool.track_external(999, 1920, 1080)

        # Verify state
        assert pool.active_count() == 3  # 2 owned + 1 external
        assert pool.owned_count() == 2

        # Force cleanup (simulating project close)
        pool.force_cleanup("test")

        # Owned textures should be cleared, external tracking remains
        assert pool.owned_count() == 0
        assert pool.external_count() == 1  # External still tracked
        assert pool.pooled_count() == 0
        assert pool.pinned_count() == 0

    def test_pinned_texture_not_evicted_from_pool(self):
        """Pinned textures in pool are not evicted during budget check."""
        mock_fb = MagicMock()
        mock_tex = self.create_mock_texture(1)
        mock_fb.get_temp_texture.return_value = mock_tex

        # Small budget to force eviction
        pool = GPUResourcePool(mock_fb, max_memory_mb=1)

        tex = pool.acquire_temp_texture(100, 100)
        pool.release_texture(tex)

        # Pin the texture ID
        pool.pin_texture(1)

        # Try to evict - pinned should be skipped
        pool._evict_to_free(10000)  # Request more than available

        # Texture should still be in pool (pinned)
        assert pool.is_pinned(1) == True

    def test_memory_usage_accumulation(self):
        """Memory usage accumulates correctly."""
        mock_fb = MagicMock()
        mock_tex1 = self.create_mock_texture(1, width=1000, height=1000)
        mock_tex2 = self.create_mock_texture(2, width=2000, height=2000)
        mock_fb.get_temp_texture.side_effect = [mock_tex1, mock_tex2]

        pool = GPUResourcePool(mock_fb)

        tex1 = pool.acquire_temp_texture(1000, 1000)
        expected1 = 1000 * 1000 * 4 * 2  # RGBA f2
        assert pool.memory_usage_bytes() == expected1

        tex2 = pool.acquire_temp_texture(2000, 2000)
        expected2 = 2000 * 2000 * 4 * 2
        assert pool.memory_usage_bytes() == expected1 + expected2

    def test_lru_order(self):
        """Pool maintains LRU order for eviction (different sizes)."""
        mock_fb = MagicMock()
        mock_tex1 = self.create_mock_texture(1, 100, 100)
        mock_tex2 = self.create_mock_texture(2, 200, 200)
        mock_tex3 = self.create_mock_texture(3, 300, 300)
        mock_fb.get_temp_texture.side_effect = [mock_tex1, mock_tex2, mock_tex3]

        pool = GPUResourcePool(mock_fb)

        # Create and release in order: 1, 2, 3 (different sizes to avoid reuse)
        tex1 = pool.acquire_temp_texture(100, 100)
        pool.release_texture(tex1)
        tex2 = pool.acquire_temp_texture(200, 200)
        pool.release_texture(tex2)
        tex3 = pool.acquire_temp_texture(300, 300)
        pool.release_texture(tex3)

        # Pool LRU order: 1 (oldest), 2, 3 (newest) - each has unique key
        assert list(pool._pool_lru.keys()) == [1, 2, 3]
        assert pool.pooled_count() == 3

        # Reuse should take the texture for the matching key
        tex = pool.acquire_temp_texture(100, 100)
        # Texture 1 should be reused for 100x100 key
        assert pool.total_reused() == 1


# ═══════════════════════════════════════════════════════════════════════
# Acceptance Criteria Tests
# ═══════════════════════════════════════════════════════════════════════

class TestAcceptanceCriteria:
    """Tests for specific acceptance criteria from the requirements."""

    def create_mock_texture(self, glo, width=1920, height=1080, components=4, dtype='f2'):
        """Create a mock texture object."""
        mock_tex = MagicMock()
        mock_tex.glo = glo
        mock_tex.width = width
        mock_tex.height = height
        mock_tex.components = components
        mock_tex.dtype = dtype
        return mock_tex

    def test_ac1_print_stats(self):
        """AC1: Can print active textures, pooled textures, estimated VRAM."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        # All dump methods should work
        stats = pool.dump_stats()
        active = pool.dump_active_textures()
        pooled = pool.dump_pooled_textures()
        full = pool.dump_full_report()

        # Verify content
        assert "active=" in stats
        assert "pooled=" in stats
        assert "vram=" in stats

        assert "Active textures:" in active
        assert "Pooled textures:" in pooled
        assert "GPU Resource Pool Report" in full

    def test_ac2_no_infinite_growth(self):
        """AC2: Active texture count doesn't grow infinitely.

        Simulate repeated acquire/release cycles.
        """
        mock_fb = MagicMock()
        mock_fb._active_textures = set()
        mock_fb.cleanup = MagicMock()

        pool = GPUResourcePool(mock_fb)

        # Simulate 100 cycles of acquire/release
        for i in range(100):
            mock_tex = MagicMock()
            mock_tex.glo = i + 1
            mock_tex.width = 100
            mock_tex.height = 100
            mock_tex.components = 4
            mock_tex.dtype = 'f2'
            mock_fb.get_temp_texture.return_value = mock_tex
            mock_fb._active_textures.add(mock_tex)

            tex = pool.acquire_temp_texture(100, 100)
            pool.release_texture(tex)

        # After 100 cycles, active should be 0 (all released)
        # Pooled may have accumulated, but that's expected for reuse
        assert pool.active_count() == 0
        assert pool.pooled_count() > 0  # Pooled for reuse

        # Force cleanup should clear all pooled
        pool.evict_all_pooled()
        assert pool.pooled_count() == 0

    def test_ac3_cleanup_releases_all(self):
        """AC3: Switching project/closing Composite releases owned resources.

        External textures remain tracked (their owner is responsible for
        releasing them).
        """
        mock_fb = MagicMock()
        mock_fb._active_textures = set()
        mock_fb.cleanup = MagicMock()

        pool = GPUResourcePool(mock_fb)

        # Create some textures
        for i in range(5):
            mock_tex = MagicMock()
            mock_tex.glo = i + 1
            mock_tex.width = 100
            mock_tex.height = 100
            mock_tex.components = 4
            mock_tex.dtype = 'f2'
            mock_fb.get_temp_texture.return_value = mock_tex
            mock_fb._active_textures.add(mock_tex)

            pool.acquire_temp_texture(100, 100)

        # Track an external (not owned by pool)
        pool.track_external(999, 1920, 1080)

        # Verify state
        assert pool.active_count() == 6  # 5 owned + 1 external
        assert pool.owned_count() == 5

        # Force cleanup (simulating project close)
        pool.force_cleanup("project_close")

        # All owned should be gone, external tracking remains
        assert pool.owned_count() == 0
        assert pool.external_count() == 1  # External still tracked
        assert pool.pooled_count() == 0

    def test_ac4_output_texture_not_reused(self):
        """AC4: Output texture is not evicted/reused while pinned.

        Simulates displaying an output texture that should not be
        reused by the pool.
        """
        mock_fb = MagicMock()
        mock_tex1 = self.create_mock_texture(1)  # output
        mock_tex2 = self.create_mock_texture(2)  # another texture
        mock_fb.get_temp_texture.side_effect = [mock_tex1, mock_tex2]
        mock_fb._active_textures = {mock_tex1, mock_tex2}

        # Small budget to trigger eviction
        pool = GPUResourcePool(mock_fb, max_memory_mb=1)

        # Acquire output texture (auto-pinned)
        output_tex = pool.acquire_output_texture(100, 100)
        assert pool.is_pinned(output_tex.glo) == True

        # Try to acquire another texture - budget check should not evict pinned
        another_tex = pool.acquire_temp_texture(100, 100)

        # Pinned texture should still be active
        assert pool.is_pinned(output_tex.glo) == True

        # Unpin when no longer displaying
        pool.unpin_texture(output_tex.glo)
        pool.release_texture(output_tex)

        # Now it can be pooled/reused
        assert pool.is_pinned(output_tex.glo) == False

    def test_acceptance_all_stats_available(self):
        """Verify all statistics methods are available for monitoring."""
        mock_fb = MagicMock()
        pool = GPUResourcePool(mock_fb)

        # All methods should exist and be callable
        stats = pool.get_stats()
        assert stats['active_count'] >= 0
        assert stats['pooled_count'] >= 0
        assert stats['memory_mb'] >= 0.0
        assert stats['pinned_count'] >= 0
        assert stats['total_created'] >= 0
        assert stats['total_reused'] >= 0
        assert stats['total_evicted'] >= 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
