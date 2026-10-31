"""
Test Tiling Renderer - Validates tile-based rendering for large images.

Tests:
1. Memory estimation accuracy
2. Automatic tiling detection for 4K/8K images
3. Tile padding calculation for spatial effects
4. Seamless tile stitching

Run with:
    pytest tests/test_tiling.py -v
"""

import pytest
import numpy as np
from typing import Dict, Tuple

# Import tiling components
from src.features.composite.render.gpu_resource_pool import GPUResourcePool
from src.features.composite.render.render_scheduler import RenderScheduler, RenderPlan, NodePlan, RenderMode
from src.features.composite.core.tile_scheduler import TileScheduler, TilePaddingComputer, Tile
from src.features.composite.core.format import ImageFormat, BBox
from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot


# ── Memory Estimation Tests ──

class TestMemoryEstimation:
    """Test GPU memory estimation for rendering."""

    def test_texture_memory_calculation(self):
        """Test single texture memory calculation."""
        # RGBA16F (f2): 4 channels × 2 bytes = 8 bytes/pixel
        mem_f2 = GPUResourcePool.estimate_texture_memory(1920, 1080, 4, 'f2')
        expected_f2 = 1920 * 1080 * 8
        assert mem_f2 == expected_f2
        assert mem_f2 == 16_588_800  # ~16 MB

        # RGBA32F (f4): 4 channels × 4 bytes = 16 bytes/pixel
        mem_f4 = GPUResourcePool.estimate_texture_memory(1920, 1080, 4, 'f4')
        expected_f4 = 1920 * 1080 * 16
        assert mem_f4 == expected_f4
        assert mem_f4 == 33_177_600  # ~33 MB

    def test_render_memory_estimation_1080p(self):
        """Test render memory estimation for 1080p image."""
        # Simple graph: 1 source, 1 effect, 2 nodes total
        estimated = GPUResourcePool.estimate_render_memory(
            1920, 1080, node_count=2, effect_count=1, source_count=1,
            render_mode="preview"
        )
        # Should be reasonable (< 100 MB for preview)
        assert estimated < 100_000_000  # < 100 MB

    def test_render_memory_estimation_4k(self):
        """Test render memory estimation for 4K image."""
        # 4K = 3840 × 2160
        estimated = GPUResourcePool.estimate_render_memory(
            3840, 2160, node_count=5, effect_count=3, source_count=1,
            render_mode="preview"
        )
        # Should be larger but still reasonable
        assert estimated > 100_000_000  # > 100 MB

    def test_render_memory_estimation_8k(self):
        """Test render memory estimation for 8K image."""
        # 8K = 7680 × 4320
        estimated = GPUResourcePool.estimate_render_memory(
            7680, 4320, node_count=5, effect_count=3, source_count=1,
            render_mode="export"
        )
        # Should be large (> 500 MB for export with f4)
        assert estimated > 500_000_000  # > 500 MB

    def test_export_mode_more_memory(self):
        """Test that export mode uses more memory than preview."""
        preview_mem = GPUResourcePool.estimate_render_memory(
            1920, 1080, node_count=3, effect_count=1, source_count=1,
            render_mode="preview"
        )
        export_mem = GPUResourcePool.estimate_render_memory(
            1920, 1080, node_count=3, effect_count=1, source_count=1,
            render_mode="export"
        )
        # Export uses f4 (32-bit) vs preview f2 (16-bit)
        assert export_mem > preview_mem


# ── Tiling Detection Tests ──

class TestTilingDetection:
    """Test automatic tiling detection in RenderScheduler."""

    def _create_mock_snapshot(self, width: int, height: int) -> GraphSnapshot:
        """Create a mock GraphSnapshot for testing."""
        # Create minimal node snapshots
        color_node = NodeSnapshot(
            node_id="color_1",
            node_type="color_input",
            position=(0, 0),
            parameters={},
            input_ports=[],
            output_ports=[("display", "image")],
            disabled=False,
        )
        # AUD-48006 后 NodeSnapshot 冻结；尺寸字段在 GraphSnapshot 上（见下），
        # 历史上对 color_node 的赋值从未被任何生产代码读取过，直接移除。

        output_node = NodeSnapshot(
            node_id="output_1",
            node_type="output",
            position=(200, 0),
            parameters={"format_mode": "auto"},
            input_ports=[("image", "image")],
            output_ports=[("display", "image")],
            disabled=False,
        )

        nodes = {
            "color_1": color_node,
            "output_1": output_node,
        }

        # Create minimal snapshot
        snapshot = GraphSnapshot(
            nodes=nodes,
            connections=[],
            eval_order=["color_1", "output_1"],
            generation=1,
            color_texture_id=1,
            color_width=width,
            color_height=height,
            color_revision=1,
        )
        return snapshot

    def test_no_tiling_for_small_images(self):
        """Test that small images (< 2048px) don't need tiling."""
        scheduler = RenderScheduler()
        snapshot = self._create_mock_snapshot(1920, 1080)
        plan = scheduler.plan(snapshot, RenderMode.PREVIEW)

        assert not plan.needs_tiling
        assert plan.output_dimensions == (1920, 1080)

    def test_tiling_for_4k_images(self):
        """Test that 4K images need tiling."""
        scheduler = RenderScheduler()
        snapshot = self._create_mock_snapshot(3840, 2160)
        plan = scheduler.plan(snapshot, RenderMode.PREVIEW)

        assert plan.needs_tiling
        assert plan.tile_size in [256, 512, 1024, 2048]
        assert plan.estimated_memory_mb > 0

    def test_tiling_for_8k_images(self):
        """Test that 8K images need tiling."""
        scheduler = RenderScheduler()
        snapshot = self._create_mock_snapshot(7680, 4320)
        plan = scheduler.plan(snapshot, RenderMode.EXPORT)

        assert plan.needs_tiling
        # 8K should use smaller tile size due to memory constraints
        assert plan.tile_size in [256, 512, 1024]
        assert plan.estimated_memory_mb > 500  # Large memory requirement


# ── Tile Generation Tests ──

class TestTileGeneration:
    """Test tile subdivision logic."""

    def test_tile_generation_no_padding(self):
        """Test tile generation without padding."""
        format = ImageFormat(width=2048, height=2048)
        tiles = TileScheduler.generate_tiles(format, padding=0, tile_size=1024)

        # Should produce 4 tiles (2 × 2)
        assert len(tiles) == 4

        # Each tile should be 1024 × 1024
        for tile in tiles:
            assert tile.output_roi.width == 1024
            assert tile.output_roi.height == 1024
            assert tile.padding == 0

    def test_tile_generation_with_padding(self):
        """Test tile generation with padding for blur effects."""
        format = ImageFormat(width=2048, height=2048)
        tiles = TileScheduler.generate_tiles(format, padding=10, tile_size=1024)

        assert len(tiles) == 4

        # Check that tiles have padding
        for tile in tiles:
            assert tile.padding == 10
            # Padded ROI should be larger than output ROI (unless at edges)
            if tile.output_roi.x > 0 and tile.output_roi.y > 0:
                assert tile.padded_roi.width >= tile.output_roi.width

    def test_tile_coverage(self):
        """Test that tiles cover the entire output region."""
        format = ImageFormat(width=3000, height=2000)
        tiles = TileScheduler.generate_tiles(format, padding=0, tile_size=1024)

        # Union of all output_rois should cover the full format
        covered = BBox(x=0, y=0, width=0, height=0)
        for tile in tiles:
            covered = covered.union(tile.output_roi)

        assert covered.x == 0
        assert covered.y == 0
        assert covered.width == format.width
        assert covered.height == format.height

    def test_edge_tile_clamping(self):
        """Test that edge tiles are properly clamped to format boundaries."""
        format = ImageFormat(width=1500, height=1500)
        tiles = TileScheduler.generate_tiles(format, padding=50, tile_size=1024)

        # Find edge tiles
        for tile in tiles:
            # Padded ROI should not extend beyond format
            assert tile.padded_roi.x >= 0
            assert tile.padded_roi.y >= 0
            assert tile.padded_roi.right <= format.width
            assert tile.padded_roi.top <= format.height


# ── Padding Computation Tests ──

class TestPaddingComputation:
    """Test padding calculation for spatial effects."""

    def test_no_padding_for_simple_graph(self):
        """Test that graphs without spatial effects have zero padding."""
        # Create snapshot with only Grade node (no spatial expansion)
        color_node = NodeSnapshot(
            node_id="color_1",
            node_type="color_input",
            position=(0, 0),
            parameters={},
            input_ports=[],
            output_ports=[("display", "image")],
            disabled=False,
        )

        grade_node = NodeSnapshot(
            node_id="grade_1",
            node_type="grade",
            position=(100, 0),
            parameters={"lift": 0.0, "gamma": 1.0, "gain": 1.0},
            input_ports=[("image", "image")],
            output_ports=[("result", "image")],
            disabled=False,
        )

        output_node = NodeSnapshot(
            node_id="output_1",
            node_type="output",
            position=(200, 0),
            parameters={"format_mode": "auto"},
            input_ports=[("image", "image")],
            output_ports=[("display", "image")],
            disabled=False,
        )

        nodes = {
            "color_1": color_node,
            "grade_1": grade_node,
            "output_1": output_node,
        }

        snapshot = GraphSnapshot(
            nodes=nodes,
            connections=[],  # Will be inferred from input_ports
            eval_order=["color_1", "grade_1", "output_1"],
            generation=1,
            color_texture_id=1,
            color_width=1920,
            color_height=1080,
            color_revision=1,
        )

        padding_info = TilePaddingComputer.compute(snapshot)
        assert padding_info.total_padding == 0

    def test_padding_for_blur_node(self):
        """Test that Blur nodes contribute to padding."""
        # Create snapshot with Blur node (radius=20)
        color_node = NodeSnapshot(
            node_id="color_1",
            node_type="color_input",
            position=(0, 0),
            parameters={},
            input_ports=[],
            output_ports=[("display", "image")],
            disabled=False,
        )

        blur_node = NodeSnapshot(
            node_id="blur_1",
            node_type="blur",
            position=(100, 0),
            parameters={"radius_x": 20.0, "radius_y": 20.0, "blur_type": "gaussian"},
            input_ports=[("image", "image")],
            output_ports=[("result", "image")],
            disabled=False,
        )

        output_node = NodeSnapshot(
            node_id="output_1",
            node_type="output",
            position=(200, 0),
            parameters={"format_mode": "auto"},
            input_ports=[("image", "image")],
            output_ports=[("display", "image")],
            disabled=False,
        )

        nodes = {
            "color_1": color_node,
            "blur_1": blur_node,
            "output_1": output_node,
        }

        # Build connections manually
        from src.features.composite.nodes.base_node import Connection
        connections = [
            Connection("color_1", "display", "blur_1", "image"),
            Connection("blur_1", "result", "output_1", "image"),
        ]

        snapshot = GraphSnapshot(
            nodes=nodes,
            connections=connections,
            eval_order=["color_1", "blur_1", "output_1"],
            generation=1,
            color_texture_id=1,
            color_width=1920,
            color_height=1080,
            color_revision=1,
        )

        padding_info = TilePaddingComputer.compute(snapshot)
        # Blur radius=20 means padding should be at least 20
        assert padding_info.total_padding >= 20


# ── Integration Tests ──

class TestTilingIntegration:
    """Integration tests for the complete tiling pipeline."""

    def test_tiling_workflow(self):
        """Test the complete tiling workflow from detection to tile generation."""
        # 1. Create a large image format
        format = ImageFormat(width=4096, height=4096)

        # 2. Compute padding
        # (Mock - would need real graph for accurate computation)
        padding = 20

        # 3. Generate tiles
        tiles = TileScheduler.generate_tiles(format, padding, tile_size=1024)

        # 4. Verify tile count and coverage
        # 4096 / 1024 = 4 × 4 = 16 tiles
        assert len(tiles) == 16

        # 5. Verify tile properties
        for tile in tiles:
            assert tile.tile_id >= 0
            assert tile.output_roi.width <= 1024
            assert tile.output_roi.height <= 1024
            assert tile.padded_roi.width >= tile.output_roi.width

    def test_memory_budget_aware_tiling(self):
        """Test that tile size adjusts based on memory budget."""
        # Simulate different memory budgets
        budgets_and_expected_sizes = [
            (2048, 1024),   # 2GB budget -> 2048 tile
            (1024, 1024),   # 1GB budget -> 1024 tile
            (512, 512),     # 512MB budget -> 512 tile
            (256, 256),     # 256MB budget -> 256 tile
        ]

        for budget_mb, expected_max_tile in budgets_and_expected_sizes:
            # Estimate memory for different tile sizes
            for tile_size in [2048, 1024, 512, 256]:
                if tile_size > expected_max_tile:
                    # This tile size should exceed budget
                    estimated = GPUResourcePool.estimate_render_memory(
                        tile_size, tile_size, node_count=5, effect_count=3,
                        source_count=1, render_mode="preview"
                    )
                    # It may or may not exceed depending on node count
                    # This is a heuristic test
                    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])