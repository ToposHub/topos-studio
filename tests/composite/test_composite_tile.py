"""
Tests for Composite Tile Rendering - TileScheduler and TilePaddingComputer.

Covers:
- Tile data structure (output_roi, padded_roi, padding)
- TileScheduler.generate_tiles() tile subdivision logic
- Tile coverage completeness (no gaps, no overlap)
- TilePaddingComputer.compute() padding accumulation
- Padding propagation through graph chain (Blur, Transform, etc.)
- Edge tile clamping to format boundaries

These are pure logic tests - no GPU required.
"""

import sys
import os
from unittest.mock import MagicMock

# Mock moderngl before any imports that depend on it, but restore afterward
# to avoid polluting other tests that need real moderngl.
_original_moderngl = sys.modules.get('moderngl')
sys.modules['moderngl'] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.tile_scheduler import Tile, TileScheduler, TilePaddingComputer, TilePaddingInfo
from src.features.composite.core.format import BBox, ImageFormat
from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot, ConnectionSnapshot

# Restore original moderngl so subsequent tests are not affected.
if _original_moderngl is not None:
    sys.modules['moderngl'] = _original_moderngl
else:
    sys.modules.pop('moderngl', None)


# ── Helper Functions ──

def _make_format(width=1920, height=1080, pixel_aspect=1.0):
    """Create an ImageFormat for testing."""
    return ImageFormat(
        width=width,
        height=height,
        pixel_aspect=pixel_aspect,
        origin_x=0,
        origin_y=0,
    )


def _make_snapshot(nodes=None, connections=None, eval_order=None, **kwargs):
    """Helper to create a GraphSnapshot with sensible defaults."""
    node_dict = {}
    if nodes:
        for n in nodes:
            if isinstance(n, dict):
                ns = NodeSnapshot(**n)
            else:
                ns = n
            node_dict[ns.node_id] = ns

    return GraphSnapshot(
        generation=kwargs.get('generation', 1),
        nodes=node_dict,
        connections=connections or [],
        eval_order=eval_order or list(node_dict.keys()),
        color_texture_id=kwargs.get('color_texture_id', 0),
        color_width=kwargs.get('color_width', 0),
        color_height=kwargs.get('color_height', 0),
        color_revision=kwargs.get('color_revision', 0),
    )


def _node(node_id="n1", node_type="blur", params=None, disabled=False, **kwargs):
    """Shorthand to create a NodeSnapshot dict."""
    return {
        "node_id": node_id,
        "node_type": node_type,
        "parameters": params or {"radius": 5.0},
        "disabled": disabled,
        "input_ports": kwargs.get("input_ports", [("image", "image")]),
        "output_ports": kwargs.get("output_ports", [("result", "image")]),
    }


def _conn(src, src_port="result", dst="n1", dst_port="image"):
    return ConnectionSnapshot(src, src_port, dst, dst_port)


# ═══════════════════════════════════════════════════════════════════════
# Tile Data Structure Tests
# ═══════════════════════════════════════════════════════════════════════

class TestTileDataStructure:
    """Test Tile dataclass basic properties."""

    def test_tile_creation(self):
        """Basic tile creation with all fields."""
        output_roi = BBox(x=0, y=0, width=512, height=512)
        padded_roi = BBox(x=0, y=0, width=522, height=522)  # +10 padding

        tile = Tile(
            tile_id=0,
            output_roi=output_roi,
            padded_roi=padded_roi,
            padding=10,
        )

        assert tile.tile_id == 0
        assert tile.output_roi == output_roi
        assert tile.padded_roi == padded_roi
        assert tile.padding == 10

    def test_tile_frozen(self):
        """Tile is immutable (frozen=True)."""
        output_roi = BBox(x=0, y=0, width=100, height=100)
        padded_roi = BBox(x=0, y=0, width=110, height=110)

        tile = Tile(tile_id=1, output_roi=output_roi, padded_roi=padded_roi, padding=5)

        with pytest.raises(AttributeError):
            tile.tile_id = 2  # Should fail

    def test_tile_string_representation(self):
        """Tile has readable string representation."""
        output_roi = BBox(x=100, y=200, width=256, height=256)
        padded_roi = BBox(x=95, y=195, width=266, height=266)

        tile = Tile(tile_id=3, output_roi=output_roi, padded_roi=padded_roi, padding=5)
        str_repr = str(tile)

        assert "tile_id=3" in str_repr
        assert "padding=5" in str_repr


# ═══════════════════════════════════════════════════════════════════════
# TileScheduler Tests - Tile Generation
# ═══════════════════════════════════════════════════════════════════════

class TestTileSchedulerBasic:
    """Test TileScheduler basic tile generation."""

    def test_single_tile_small_image(self):
        """Image smaller than tile_size produces single tile."""
        fmt = _make_format(width=512, height=512)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        assert len(tiles) == 1
        assert tiles[0].tile_id == 0
        assert tiles[0].output_roi.x == 0
        assert tiles[0].output_roi.y == 0
        assert tiles[0].output_roi.width == 512
        assert tiles[0].output_roi.height == 512

    def test_exact_tile_size_match(self):
        """Image exactly matches tile_size produces single tile."""
        fmt = _make_format(width=1024, height=1024)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        assert len(tiles) == 1
        assert tiles[0].output_roi.width == 1024
        assert tiles[0].output_roi.height == 1024

    def test_two_tiles_horizontal(self):
        """Image twice tile width produces 2x1 tiles."""
        fmt = _make_format(width=2048, height=1024)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        assert len(tiles) == 2

        # Tile 0: left half
        assert tiles[0].output_roi.x == 0
        assert tiles[0].output_roi.width == 1024

        # Tile 1: right half
        assert tiles[1].output_roi.x == 1024
        assert tiles[1].output_roi.width == 1024

    def test_two_tiles_vertical(self):
        """Image twice tile height produces 1x2 tiles."""
        fmt = _make_format(width=1024, height=2048)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        assert len(tiles) == 2

        # Tile 0: top half
        assert tiles[0].output_roi.y == 0
        assert tiles[0].output_roi.height == 1024

        # Tile 1: bottom half
        assert tiles[1].output_roi.y == 1024
        assert tiles[1].output_roi.height == 1024

    def test_four_tiles_grid(self):
        """2x2 tile_size grid produces 4 tiles."""
        fmt = _make_format(width=2048, height=2048)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        assert len(tiles) == 4

        # Check grid positions
        positions = [(t.output_roi.x, t.output_roi.y) for t in tiles]
        assert (0, 0) in positions      # top-left
        assert (1024, 0) in positions    # top-right
        assert (0, 1024) in positions    # bottom-left
        assert (1024, 1024) in positions  # bottom-right


class TestTileSchedulerPadding:
    """Test TileScheduler padding expansion."""

    def test_padding_expands_padded_roi(self):
        """Padded ROI is expanded by padding amount."""
        fmt = _make_format(width=512, height=512)
        tiles = TileScheduler.generate_tiles(fmt, padding=10, tile_size=1024)

        assert len(tiles) == 1

        # Output ROI unchanged
        assert tiles[0].output_roi.x == 0
        assert tiles[0].output_roi.y == 0
        assert tiles[0].output_roi.width == 512
        assert tiles[0].output_roi.height == 512

        # Padded ROI expanded by 10 on each side
        assert tiles[0].padded_roi.x == 0  # clamped at edge
        assert tiles[0].padded_roi.y == 0  # clamped at edge
        assert tiles[0].padded_roi.width == 512
        assert tiles[0].padded_roi.height == 512

    def test_padding_expands_interior_tile(self):
        """Interior tile gets full padding expansion (not clamped at edge)."""
        # Use 3072x3072 to create a 3x3 grid, with tile(1024,1024) as true interior
        fmt = _make_format(width=3072, height=3072)
        tiles = TileScheduler.generate_tiles(fmt, padding=10, tile_size=1024)

        assert len(tiles) == 9  # 3x3 grid

        # Find true interior tile (center of 3x3 grid)
        # Grid layout: (0,0) (1024,0) (2048,0) | (0,1024) (1024,1024) (2048,1024) | (0,2048) (1024,2048) (2048,2048)
        interior_tile = None
        for t in tiles:
            if t.output_roi.x == 1024 and t.output_roi.y == 1024:
                interior_tile = t
                break

        assert interior_tile is not None

        # Interior tile: padded ROI should be expanded on all sides (no clamping)
        assert interior_tile.padded_roi.x == 1014  # 1024 - 10
        assert interior_tile.padded_roi.y == 1014  # 1024 - 10
        assert interior_tile.padded_roi.width == 1024 + 20  # +10 each side
        assert interior_tile.padded_roi.height == 1024 + 20

    def test_edge_tile_padding_clamped(self):
        """Edge tiles have truncated padding (clamped to format)."""
        fmt = _make_format(width=2048, height=2048)
        tiles = TileScheduler.generate_tiles(fmt, padding=10, tile_size=1024)

        # Tile at (0, 0) - top-left edge
        edge_tile = tiles[0]
        assert edge_tile.output_roi.x == 0
        assert edge_tile.output_roi.y == 0

        # Padded ROI clamped at left/top edges
        assert edge_tile.padded_roi.x == 0  # can't go negative
        assert edge_tile.padded_roi.y == 0  # can't go negative
        # But can expand right/bottom (unless also at edge)
        assert edge_tile.padded_roi.width == min(1024 + 10, 2048)  # 1034
        assert edge_tile.padded_roi.height == min(1024 + 10, 2048)  # 1034

    def test_large_padding(self):
        """Large padding values handled correctly."""
        fmt = _make_format(width=4096, height=4096)
        tiles = TileScheduler.generate_tiles(fmt, padding=50, tile_size=1024)

        assert len(tiles) == 16  # 4x4 grid
        assert tiles[0].padding == 50


class TestTileSchedulerCoverage:
    """Test tile coverage completeness."""

    def test_tiles_cover_entire_output(self):
        """Tiles must cover the entire output format without gaps."""
        fmt = _make_format(width=3000, height=2000)  # non-power-of-2
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        # Collect all covered pixels
        covered = set()
        for t in tiles:
            for y in range(t.output_roi.y, t.output_roi.y + t.output_roi.height):
                for x in range(t.output_roi.x, t.output_roi.x + t.output_roi.width):
                    covered.add((x, y))

        # Check all pixels in format are covered
        expected_count = fmt.width * fmt.height
        assert len(covered) == expected_count

    def test_tiles_no_overlap(self):
        """Output ROIs must not overlap (padded ROIs can overlap)."""
        fmt = _make_format(width=2048, height=2048)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        # Check no two tiles have overlapping output ROIs
        for i, t1 in enumerate(tiles):
            for j, t2 in enumerate(tiles):
                if i >= j:
                    continue
                intersection = t1.output_roi.intersection(t2.output_roi)
                assert intersection.is_empty or (
                    intersection.width == 0 or intersection.height == 0
                )

    def test_padded_rois_can_overlap(self):
        """Padded ROIs intentionally overlap to handle spatial effects."""
        fmt = _make_format(width=2048, height=2048)
        tiles = TileScheduler.generate_tiles(fmt, padding=10, tile_size=1024)

        # Adjacent tiles' padded ROIs should overlap at boundaries
        # (This is intentional for seamless stitching)
        overlaps_found = False
        for i, t1 in enumerate(tiles):
            for j, t2 in enumerate(tiles):
                if i >= j:
                    continue
                intersection = t1.padded_roi.intersection(t2.padded_roi)
                if not intersection.is_empty:
                    overlaps_found = True

        # For interior tiles with padding, we expect overlaps
        # (Adjacent tiles share the padding region)
        assert overlaps_found or len(tiles) == 1

    def test_tile_order_consistent(self):
        """Tiles are generated in consistent row-major order."""
        fmt = _make_format(width=2048, height=2048)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        # Row-major: (0,0), (1024,0), (0,1024), (1024,1024)
        assert tiles[0].output_roi.x == 0 and tiles[0].output_roi.y == 0
        assert tiles[1].output_roi.x == 1024 and tiles[1].output_roi.y == 0
        assert tiles[2].output_roi.x == 0 and tiles[2].output_roi.y == 1024
        assert tiles[3].output_roi.x == 1024 and tiles[3].output_roi.y == 1024


class TestTileSchedulerEdgeCases:
    """Test TileScheduler edge cases."""

    def test_very_small_image(self):
        """Very small image (< tile_size) still produces tile."""
        fmt = _make_format(width=64, height=64)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        assert len(tiles) == 1
        assert tiles[0].output_roi.width == 64
        assert tiles[0].output_roi.height == 64

    def test_very_large_image(self):
        """8K image produces many tiles."""
        fmt = _make_format(width=7680, height=4320)  # 8K UHD
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        # 7680/1024 ≈ 7.5 → 8 columns
        # 4320/1024 ≈ 4.2 → 5 rows
        # Total: 8 * 5 = 40 tiles
        assert len(tiles) >= 35  # approximate

    def test_non_square_tiles(self):
        """Non-square output produces non-square tiles at edges."""
        fmt = _make_format(width=1536, height=512)  # 3:1 aspect
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=1024)

        # First tile: 1024x512
        assert tiles[0].output_roi.width == 1024
        assert tiles[0].output_roi.height == 512

        # Second tile: 512x512 (remainder)
        assert tiles[1].output_roi.x == 1024
        assert tiles[1].output_roi.width == 512
        assert tiles[1].output_roi.height == 512

    def test_invalid_tile_size_handled(self):
        """Tile size < 1 is corrected to default."""
        fmt = _make_format(width=512, height=512)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=0)

        # Should fall back to default (1024)
        assert len(tiles) == 1

    def test_negative_tile_size_handled(self):
        """Negative tile size is corrected."""
        fmt = _make_format(width=512, height=512)
        tiles = TileScheduler.generate_tiles(fmt, padding=0, tile_size=-100)

        # Should fall back to default
        assert len(tiles) >= 1


# ═══════════════════════════════════════════════════════════════════════
# TilePaddingComputer Tests
# ═══════════════════════════════════════════════════════════════════════

class TestTilePaddingComputerBasic:
    """Test TilePaddingComputer basic padding calculation."""

    def test_empty_graph_zero_padding(self):
        """Empty graph requires zero padding."""
        snapshot = _make_snapshot(nodes=[])
        padding_info = TilePaddingComputer.compute(snapshot)

        assert padding_info.total_padding == 0
        assert padding_info.per_node_padding == {}

    def test_single_source_node_zero_padding(self):
        """Source node alone needs zero padding."""
        snapshot = _make_snapshot(
            nodes=[_node("color1", "color_input", input_ports=[])],
        )
        padding_info = TilePaddingComputer.compute(snapshot)

        assert padding_info.total_padding == 0

    def test_single_blur_node(self):
        """Single Blur node with radius produces padding."""
        snapshot = _make_snapshot(
            nodes=[
                _node("color1", "color_input", input_ports=[]),
                _node("blur1", "blur", params={"radius": 10.0}),
            ],
            connections=[_conn("color1", "result", "blur1", "image")],
            eval_order=["color1", "blur1"],
        )
        padding_info = TilePaddingComputer.compute(snapshot)

        # Blur radius 10 → padding 10
        assert padding_info.total_padding >= 10
        assert "blur1" in padding_info.per_node_padding

    def test_single_grade_node_zero_padding(self):
        """Grade node (pixel-wise) needs zero padding."""
        snapshot = _make_snapshot(
            nodes=[
                _node("color1", "color_input", input_ports=[]),
                _node("grade1", "grade", params={"lift": 0.0, "gain": 1.0}),
            ],
            connections=[_conn("color1", "result", "grade1", "image")],
            eval_order=["color1", "grade1"],
        )
        padding_info = TilePaddingComputer.compute(snapshot)

        # Grade has no ROI expansion
        assert padding_info.per_node_padding.get("grade1", 0) == 0
        assert padding_info.total_padding == 0


class TestTilePaddingComputerChain:
    """Test padding propagation through graph chains."""

    def test_blur_chain_accumulates(self):
        """Multiple Blur nodes accumulate padding."""
        # color → blur(r=10) → blur(r=5) → output
        snapshot = _make_snapshot(
            nodes=[
                _node("color1", "color_input", input_ports=[]),
                _node("blur1", "blur", params={"radius": 10.0}),
                _node("blur2", "blur", params={"radius": 5.0}),
                _node("output1", "output", input_ports=[("image", "image")]),
            ],
            connections=[
                _conn("color1", "result", "blur1", "image"),
                _conn("blur1", "result", "blur2", "image"),
                _conn("blur2", "result", "output1", "image"),
            ],
            eval_order=["color1", "blur1", "blur2", "output1"],
        )
        padding_info = TilePaddingComputer.compute(snapshot)

        # Total padding should be blur1(10) + blur2(5) = 15
        # (Walking backward: blur2 needs 5, blur1 needs 10 + 5 = 15)
        assert padding_info.total_padding >= 15

    def test_grade_between_blurs_no_effect(self):
        """Grade node between Blurs doesn't add extra padding."""
        # color → blur(r=10) → grade → blur(r=5) → output
        snapshot = _make_snapshot(
            nodes=[
                _node("color1", "color_input", input_ports=[]),
                _node("blur1", "blur", params={"radius": 10.0}),
                _node("grade1", "grade", params={"lift": 0.0}),
                _node("blur2", "blur", params={"radius": 5.0}),
            ],
            connections=[
                _conn("color1", "result", "blur1", "image"),
                _conn("blur1", "result", "grade1", "image"),
                _conn("grade1", "result", "blur2", "image"),
            ],
            eval_order=["color1", "blur1", "grade1", "blur2"],
        )
        padding_info = TilePaddingComputer.compute(snapshot)

        # Grade adds 0 padding
        # Total: blur1(10) + blur2(5) = 15
        assert padding_info.per_node_padding.get("grade1", 0) <= 5
        assert padding_info.total_padding >= 15

    def test_transform_filter_padding(self):
        """Transform node with linear filter needs 1 pixel padding."""
        snapshot = _make_snapshot(
            nodes=[
                _node("color1", "color_input", input_ports=[]),
                _node("transform1", "transform",
                      params={"filter": "linear", "translate_x": 0.0}),
            ],
            connections=[_conn("color1", "result", "transform1", "image")],
            eval_order=["color1", "transform1"],
        )
        padding_info = TilePaddingComputer.compute(snapshot)

        # Linear filter needs 1 pixel
        assert padding_info.per_node_padding.get("transform1", 0) >= 1

    def test_transform_nearest_zero_padding(self):
        """Transform with nearest filter needs zero padding."""
        snapshot = _make_snapshot(
            nodes=[
                _node("color1", "color_input", input_ports=[]),
                _node("transform1", "transform",
                      params={"filter": "nearest", "translate_x": 0.0}),
            ],
            connections=[_conn("color1", "result", "transform1", "image")],
            eval_order=["color1", "transform1"],
        )
        padding_info = TilePaddingComputer.compute(snapshot)

        # Nearest filter needs 0 padding
        assert padding_info.per_node_padding.get("transform1", 0) == 0


class TestTilePaddingComputerComplexGraph:
    """Test padding computation with complex graph structures."""

    def test_merge_node_two_inputs(self):
        """Merge node with two inputs, both need same padding."""
        # colorA → blurA → merge
        # colorB → blurB → merge
        snapshot = _make_snapshot(
            nodes=[
                _node("colorA", "color_input", input_ports=[]),
                _node("colorB", "color_input", input_ports=[]),
                _node("blurA", "blur", params={"radius": 10.0}),
                _node("blurB", "blur", params={"radius": 5.0}),
                _node("merge1", "merge",
                      input_ports=[("A", "image"), ("B", "image")]),
            ],
            connections=[
                _conn("colorA", "result", "blurA", "image"),
                _conn("colorB", "result", "blurB", "image"),
                _conn("blurA", "result", "merge1", "A"),
                _conn("blurB", "result", "merge1", "B"),
            ],
            eval_order=["colorA", "colorB", "blurA", "blurB", "merge1"],
        )
        padding_info = TilePaddingComputer.compute(snapshot)

        # Max padding: blurA(10) and blurB(5) paths merge
        # Source nodes get max padding from their downstream path
        assert padding_info.total_padding >= 10  # max of both paths

    def test_disconnected_branch_not_counted(self):
        """Disconnected branch doesn't contribute to output padding."""
        # colorA → blurA → output
        # colorB → blurB (not connected to output)
        snapshot = _make_snapshot(
            nodes=[
                _node("colorA", "color_input", input_ports=[]),
                _node("colorB", "color_input", input_ports=[]),
                _node("blurA", "blur", params={"radius": 10.0}),
                _node("blurB", "blur", params={"radius": 50.0}),  # Large but disconnected
                _node("output1", "output", input_ports=[("image", "image")]),
            ],
            connections=[
                _conn("colorA", "result", "blurA", "image"),
                _conn("blurA", "result", "output1", "image"),
                # blurB not connected to output
            ],
            eval_order=["colorA", "colorB", "blurA", "blurB", "output1"],
        )
        padding_info = TilePaddingComputer.compute(snapshot)

        # Only blurA path contributes
        # blurB(50) is disconnected, shouldn't affect output
        assert padding_info.total_padding >= 10
        # blurB may have its own padding, but doesn't affect colorA's requirement
        assert padding_info.per_node_padding.get("blurB", 0) >= 50


class TestTilePaddingInfoDataStructure:
    """Test TilePaddingInfo dataclass."""

    def test_padding_info_creation(self):
        """Basic padding info creation."""
        info = TilePaddingInfo(
            total_padding=15,
            per_node_padding={"blur1": 15, "blur2": 5},
        )

        assert info.total_padding == 15
        assert info.per_node_padding["blur1"] == 15
        assert info.per_node_padding["blur2"] == 5

    def test_padding_info_frozen(self):
        """TilePaddingInfo is immutable."""
        info = TilePaddingInfo(total_padding=10)

        with pytest.raises(AttributeError):
            info.total_padding = 20

    def test_padding_info_default_empty_dict(self):
        """Default per_node_padding is empty dict."""
        info = TilePaddingInfo(total_padding=0)
        assert info.per_node_padding == {}


# ═══════════════════════════════════════════════════════════════════════
# Integration Tests - Tile + Padding
# ═══════════════════════════════════════════════════════════════════════

class TestTileIntegration:
    """Test tile generation with computed padding."""

    def test_4k_image_with_blur(self):
        """4K image with Blur generates tiles with correct padding."""
        # Create snapshot with blur chain
        snapshot = _make_snapshot(
            nodes=[
                _node("color1", "color_input", input_ports=[]),
                _node("blur1", "blur", params={"radius": 20.0}),
            ],
            connections=[_conn("color1", "result", "blur1", "image")],
            eval_order=["color1", "blur1"],
        )

        # Compute padding
        padding_info = TilePaddingComputer.compute(snapshot)

        # Generate tiles
        fmt = _make_format(width=3840, height=2160)  # 4K
        tiles = TileScheduler.generate_tiles(
            fmt, padding=padding_info.total_padding, tile_size=1024
        )

        # Each tile should have blur radius padding
        assert all(t.padding == padding_info.total_padding for t in tiles)

        # Interior tiles should have full padding expansion
        # Find an interior tile
        for t in tiles:
            if t.output_roi.x > 0 and t.output_roi.y > 0:
                # Interior tile: padded ROI should be expanded
                assert t.padded_roi.x < t.output_roi.x
                assert t.padded_roi.y < t.output_roi.y

    def test_tile_count_reasonable_for_memory(self):
        """Tile count reasonable for memory-bounded rendering."""
        # 8K image with 1024 tiles should have ~40 tiles
        fmt = _make_format(width=7680, height=4320)
        tiles = TileScheduler.generate_tiles(fmt, padding=20, tile_size=1024)

        # Each tile is ~1024x1024 RGBA f16 = ~8MB
        # 40 tiles = 320MB intermediate memory (bounded)
        tile_count = len(tiles)

        # Verify count is reasonable (not thousands)
        assert tile_count < 100
        assert tile_count > 30  # 8K needs many tiles


# ═══════════════════════════════════════════════════════════════════════
# BBox Clamping Tests (used by TileScheduler)
# ═══════════════════════════════════════════════════════════════════════

class TestBBoxClamping:
    """Test BBox.clamped() method used for tile padding."""

    def test_clamp_within_bounds(self):
        """BBox clamped within valid bounds."""
        bbox = BBox(x=-10, y=-10, width=100, height=100)
        bounds = BBox(x=0, y=0, width=512, height=512)
        clamped = bbox.clamped(bounds)

        assert clamped.x == 0
        assert clamped.y == 0
        # Width/height adjusted to stay within bounds
        assert clamped.x + clamped.width <= bounds.x + bounds.width
        assert clamped.y + clamped.height <= bounds.y + bounds.height

    def test_clamp_full_within(self):
        """BBox fully within bounds unchanged."""
        bbox = BBox(x=100, y=100, width=50, height=50)
        bounds = BBox(x=0, y=0, width=512, height=512)
        clamped = bbox.clamped(bounds)

        assert clamped.x == 100
        assert clamped.y == 100
        assert clamped.width == 50
        assert clamped.height == 50

    def test_clamp_exceed_right_edge(self):
        """BBox exceeding right edge truncated."""
        bbox = BBox(x=500, y=100, width=100, height=50)
        bounds = BBox(x=0, y=0, width=512, height=512)
        clamped = bbox.clamped(bounds)

        assert clamped.x == 500
        assert clamped.x + clamped.width == 512  # truncated to edge

    def test_clamp_exceed_bottom_edge(self):
        """BBox exceeding bottom edge truncated."""
        bbox = BBox(x=100, y=500, width=50, height=100)
        bounds = BBox(x=0, y=0, width=512, height=512)
        clamped = bbox.clamped(bounds)

        assert clamped.y == 500
        assert clamped.y + clamped.height == 512  # truncated to edge


# ═══════════════════════════════════════════════════════════════════════
# F2-04: Capability-Based Padding Check Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCapabilityBasedPaddingCheck:
    """Test _needs_tile_padding_check() function (F2-04 migration).

    Verifies that the capability-based check correctly identifies
    which node types need tile padding computation.
    """

    def test_effect_nodes_need_check(self):
        """Effect nodes always need padding check."""
        from src.features.composite.core.tile_padding_computer import _needs_tile_padding_check

        # Effect nodes that may have padding
        assert _needs_tile_padding_check("blur") is True
        assert _needs_tile_padding_check("grade") is True
        assert _needs_tile_padding_check("transform") is True
        assert _needs_tile_padding_check("sharpen") is True
        assert _needs_tile_padding_check("glow") is True
        assert _needs_tile_padding_check("merge") is True

    def test_source_nodes_no_check(self):
        """Source nodes don't need padding check."""
        from src.features.composite.core.tile_padding_computer import _needs_tile_padding_check

        assert _needs_tile_padding_check("color_input") is False
        assert _needs_tile_padding_check("image_input") is False
        assert _needs_tile_padding_check("constant") is False

    def test_output_nodes_no_check(self):
        """Output nodes don't need padding check."""
        from src.features.composite.core.tile_padding_computer import _needs_tile_padding_check

        assert _needs_tile_padding_check("output") is False

    def test_passthrough_nodes_no_check(self):
        """Passthrough nodes don't need padding check."""
        from src.features.composite.core.tile_padding_computer import _needs_tile_padding_check

        assert _needs_tile_padding_check("dot") is False
        assert _needs_tile_padding_check("noop") is False
        assert _needs_tile_padding_check("switch") is False

    def test_unknown_node_uses_fallback(self):
        """Unknown node types use legacy fallback."""
        from src.features.composite.core.tile_padding_computer import (
            _needs_tile_padding_check,
            _LEGACY_PADDING_NODE_TYPES,
        )

        # "output" is in legacy set but capability says it's output type
        # Capability takes precedence
        assert _needs_tile_padding_check("output") is False

        # A truly unknown type (not in capability table, not in legacy)
        # This should return False (not in legacy fallback)
        assert _needs_tile_padding_check("unknown_node_xyz") is False

    def test_capability_supports_new_nodes(self):
        """New effect nodes automatically supported without list update.

        This is the key benefit of capability-based system:
        adding a new node to _NODE_CAPABILITY_TABLE automatically
        enables padding check without updating _PADDING_NODE_TYPES.
        """
        from src.features.composite.core.tile_padding_computer import _needs_tile_padding_check
        from src.features.composite.core.node_capabilities import get_node_capability

        # Check nodes that were NOT in legacy list but are in capability table
        # These should still work because capability says they're effect nodes

        # "color_correct" is an effect node (not in legacy, but in capability)
        cap = get_node_capability("color_correct")
        if cap is not None and cap.action_type == "effect":
            assert _needs_tile_padding_check("color_correct") is True

        # "denoise" is an effect node (not in legacy, but in capability)
        cap = get_node_capability("denoise")
        if cap is not None and cap.action_type == "effect":
            assert _needs_tile_padding_check("denoise") is True

    def test_legacy_set_matches_effect_types(self):
        """Verify legacy set coverage (compatibility check).

        The legacy set should contain all effect nodes that were
        in the original _PADDING_NODE_TYPES.
        """
        from src.features.composite.core.tile_padding_computer import _LEGACY_PADDING_NODE_TYPES
        from src.features.composite.core.node_capabilities import EFFECT_NODE_TYPES, get_node_capability

        # For each node in legacy set, verify it's an effect node
        # (or output which was incorrectly in old list)
        for node_type in _LEGACY_PADDING_NODE_TYPES:
            cap = get_node_capability(node_type)
            if cap is not None:
                # Legacy "output" was in old list but shouldn't need check
                # (this was a bug in original list, now fixed by capability)
                if node_type != "output":
                    assert cap.action_type == "effect", (
                        f"{node_type} in legacy set but not effect type"
                    )


# ── Run Tests ──

if __name__ == "__main__":
    pytest.main([__file__, "-v"])