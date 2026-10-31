"""
Tile Scheduler - Tile subdivision for large image export.

Divides the output image into tiles for memory-bounded GPU rendering.
Computes the padding needed by spatial effects (blur, transform) to ensure
seamless tile boundaries, then generates the tile list.

Architecture:
- TilePaddingComputer (in tile_padding_computer.py): walks the graph backward,
  accumulating padding from each spatial node's _tile_padding() method.
- TileScheduler: divides output format into non-overlapping tiles, each
  expanded by the total padding and clamped to format boundaries.

Note: TilePaddingComputer is now in its own module for better organization
      and enhanced features like tile size recommendation and memory budgeting.
"""

from dataclasses import dataclass
from typing import List

from .format import BBox, ImageFormat


# ── Tile Definition ──

@dataclass(frozen=True)
class Tile:
    """A single tile of the output image.

    Attributes:
        tile_id: Sequential tile index (0-based).
        output_roi: The region of the final output image this tile covers.
                    No padding — this is what gets stitched into the result.
        padded_roi: The region to actually evaluate, including padding for
                    spatial effects. Clamped to the output format boundaries.
        padding: How many pixels of padding were added on each side
                 (before clamping at edges).
    """
    tile_id: int
    output_roi: BBox
    padded_roi: BBox
    padding: int


# ── Tile Generation ──

class TileScheduler:
    """Divides the output format into tiles for memory-bounded rendering.

    Tile sizing strategy:
    - Target tile size configurable (default 1024x1024).
    - Tiles are rectangular, non-overlapping in output space.
    - Each tile's evaluation region is expanded by total_padding and
      clamped to the output format boundaries.
    - Interior tiles have full padding on all sides.
    - Edge tiles have truncated padding (clamped to format).

    Tile size recommendations:
    - preview: 512 or 1024
    - export:  1024 or 2048

    For enhanced padding computation with memory budgeting,
    use TilePaddingComputer from tile_padding_computer.py.
    """

    @staticmethod
    def generate_tiles(
        output_format: ImageFormat,
        padding: int = 0,
        tile_size: int = 1024,
    ) -> List[Tile]:
        """Generate tiles covering the entire output format.

        Args:
            output_format: Output image dimensions.
            padding: Extra pixels on each side for spatial effects.
            tile_size: Target tile dimension in pixels.

        Returns:
            List of Tile objects covering the output format exactly.
        """
        if tile_size < 1:
            tile_size = 1024

        tiles: List[Tile] = []
        tile_id = 0

        for y in range(0, output_format.height, tile_size):
            for x in range(0, output_format.width, tile_size):
                # Output region (what gets stitched into final image)
                tw = min(tile_size, output_format.width - x)
                th = min(tile_size, output_format.height - y)
                output_roi = BBox(x=x, y=y, width=tw, height=th)

                # Padded region (what gets evaluated on GPU)
                padded_roi = output_roi.expanded(padding).clamped(output_format)

                tiles.append(Tile(
                    tile_id=tile_id,
                    output_roi=output_roi,
                    padded_roi=padded_roi,
                    padding=padding,
                ))
                tile_id += 1

        return tiles


# ── Backward Compatibility ──
# Import TilePaddingComputer from new location for existing code

from .tile_padding_computer import PaddingInfo
from .tile_padding_computer import TilePaddingComputer  # noqa: F401 — 转口 re-export（tests/composite 经本模块导入）

# Alias for backward compatibility
TilePaddingInfo = PaddingInfo