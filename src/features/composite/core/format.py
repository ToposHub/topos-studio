"""
ImageFormat and BBox — core spatial data types for the composite engine.

- ImageFormat: the canvas / frame size (NUKE's "Format").
- BBox:        the data window / region of interest (NUKE's "Bounding Box").

TextureResult carries both so that downstream nodes know the full frame
dimensions *and* which region actually contains useful pixel data.
"""

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# ── Bounding Box ──

@dataclass(frozen=True)
class BBox:
    """Immutable axis-aligned bounding box in pixel coordinates.

    Represents the "data window" — the sub-region of the format that
    actually contains meaningful pixel data.  BBox is independent of
    the canvas format; the same bbox can be valid across different
    formats (e.g. after a Reformat node).

    Attributes:
        x: Left edge in pixels.
        y: Bottom edge in pixels (OpenGL convention: 0 = bottom).
        width: Width in pixels (must be >= 0).
        height: Height in pixels (must be >= 0).
    """
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def top(self) -> int:
        return self.y + self.height

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    @property
    def area(self) -> int:
        return max(0, self.width * self.height)

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.right and self.y <= py < self.top

    def intersects(self, other: "BBox") -> bool:
        return (self.x < other.right and self.right > other.x and
                self.y < other.top and self.top > other.y)

    def union(self, other: "BBox") -> "BBox":
        """Return the smallest bbox that contains both."""
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        x = min(self.x, other.x)
        y = min(self.y, other.y)
        r = max(self.right, other.right)
        t = max(self.top, other.top)
        return BBox(x=x, y=y, width=r - x, height=t - y)

    def intersection(self, other: "BBox") -> "BBox":
        """Return the overlap region, or an empty bbox if no overlap."""
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        r = min(self.right, other.right)
        t = min(self.top, other.top)
        if r <= x or t <= y:
            return BBox(x=0, y=0, width=0, height=0)
        return BBox(x=x, y=y, width=r - x, height=t - y)

    def expanded(self, margin: int) -> "BBox":
        """Return a bbox grown by `margin` pixels on all sides."""
        return BBox(
            x=self.x - margin,
            y=self.y - margin,
            width=self.width + 2 * margin,
            height=self.height + 2 * margin,
        )

    def clamped(self, fmt: "ImageFormat") -> "BBox":
        """Clamp to the format's canvas boundaries."""
        x = max(0, self.x)
        y = max(0, self.y)
        r = min(fmt.width, self.right)
        t = min(fmt.height, self.top)
        if r <= x or t <= y:
            return BBox(x=0, y=0, width=0, height=0)
        return BBox(x=x, y=y, width=r - x, height=t - y)

    def transformed_corners(self, matrix: Tuple[float, ...]) -> List[Tuple[float, float]]:
        """Transform the four corners through an affine matrix.

        matrix is (m00, m10, m01, m11, m02, m12) — same layout as
        TransformNode / _apply_transform uses.

        Returns list of 4 (x, y) float tuples (bottom-left, bottom-right,
        top-left, top-right, pre-transform).
        """
        m00, m10, m01, m11, m02, m12 = matrix
        corners = [
            (self.x, self.y),
            (self.right, self.y),
            (self.x, self.top),
            (self.right, self.top),
        ]
        result = []
        for cx, cy in corners:
            tx = m00 * cx + m01 * cy + m02
            ty = m10 * cx + m11 * cy + m12
            result.append((tx, ty))
        return result

    @classmethod
    def from_corners(cls, corners: List[Tuple[float, float]]) -> "BBox":
        """Create an integer bbox that encloses all float corner points.

        Uses floor for min edges and ceil for max edges to ensure the
        returned bbox fully contains all sub-pixel corner positions.
        """
        if not corners:
            return cls(x=0, y=0, width=0, height=0)
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        x = int(math.floor(min(xs)))
        y = int(math.floor(min(ys)))
        r = int(math.ceil(max(xs)))
        t = int(math.ceil(max(ys)))
        if r <= x or t <= y:
            return cls(x=0, y=0, width=0, height=0)
        return cls(x=x, y=y, width=r - x, height=t - y)

    @classmethod
    def full_frame(cls, width: int, height: int) -> "BBox":
        """Convenience: a bbox covering the entire frame."""
        return cls(x=0, y=0, width=width, height=height)

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: dict) -> "BBox":
        return cls(
            x=int(data.get("x", 0)),
            y=int(data.get("y", 0)),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
        )


# ── Merge Geometry (S2-C) ──

@dataclass(frozen=True)
class MergeGeometry:
    """Immutable geometry configuration for merge operations (S2-C).

    Contains all spatial information needed for correct shader sampling
    when compositing inputs with different bboxes and formats.

    Attributes:
        output_width: Output canvas width in pixels.
        output_height: Output canvas height in pixels.
        output_bbox: Output data window (region containing meaningful data).
        bbox_a: BBox of input A in output coordinate space.
        bbox_b: BBox of input B in output coordinate space.
        bbox_mask: BBox of mask input in output coordinate space (optional).
        resolution_a: Actual resolution of input A texture (width, height).
        resolution_b: Actual resolution of input B texture (width, height).
        resolution_mask: Actual resolution of mask texture (width, height) (optional).
        output_format: Output ImageFormat (optional, for metadata propagation).
    """
    output_width: int
    output_height: int
    output_bbox: BBox
    bbox_a: BBox
    bbox_b: BBox
    bbox_mask: Optional[BBox] = None
    resolution_a: Tuple[int, int] = (0, 0)
    resolution_b: Tuple[int, int] = (0, 0)
    resolution_mask: Optional[Tuple[int, int]] = None
    output_format: Optional["ImageFormat"] = None

    @property
    def is_valid(self) -> bool:
        """Check if geometry has valid output dimensions."""
        return self.output_width > 0 and self.output_height > 0

    def to_uniform_dict(self) -> Dict[str, Tuple[float, ...]]:
        """Convert to shader uniform values (float tuples).

        Returns dict with uniform names mapped to float tuples:
            - u_output_resolution: (output_width, output_height)
            - u_output_bbox: (x, y, width, height) as floats
            - u_bbox_a: (x, y, width, height) as floats
            - u_bbox_b: (x, y, width, height) as floats
            - u_bbox_mask: (x, y, width, height) as floats (or (0,0,0,0) if None)
            - u_resolution_a: (width, height) as floats
            - u_resolution_b: (width, height) as floats
            - u_resolution_mask: (width, height) as floats (or (0,0) if None)
        """
        result: Dict[str, Tuple[float, ...]] = {
            "u_output_resolution": (float(self.output_width), float(self.output_height)),
            "u_output_bbox": (
                float(self.output_bbox.x),
                float(self.output_bbox.y),
                float(self.output_bbox.width),
                float(self.output_bbox.height),
            ),
            "u_bbox_a": (
                float(self.bbox_a.x),
                float(self.bbox_a.y),
                float(self.bbox_a.width),
                float(self.bbox_a.height),
            ),
            "u_bbox_b": (
                float(self.bbox_b.x),
                float(self.bbox_b.y),
                float(self.bbox_b.width),
                float(self.bbox_b.height),
            ),
            "u_resolution_a": (float(self.resolution_a[0]), float(self.resolution_a[1])),
            "u_resolution_b": (float(self.resolution_b[0]), float(self.resolution_b[1])),
        }

        if self.bbox_mask is not None:
            result["u_bbox_mask"] = (
                float(self.bbox_mask.x),
                float(self.bbox_mask.y),
                float(self.bbox_mask.width),
                float(self.bbox_mask.height),
            )
        else:
            result["u_bbox_mask"] = (0.0, 0.0, 0.0, 0.0)

        if self.resolution_mask is not None:
            result["u_resolution_mask"] = (
                float(self.resolution_mask[0]),
                float(self.resolution_mask[1]),
            )
        else:
            result["u_resolution_mask"] = (0.0, 0.0)

        return result


# ── Image Format ──

@dataclass(frozen=True)
class ImageFormat:
    """Immutable description of an image canvas format.

    Attributes:
        width: Frame width in pixels.
        height: Frame height in pixels.
        pixel_aspect: Pixel aspect ratio (1.0 = square pixels).
        name: Human-readable format name (e.g. "HD 1080").
        origin_x: X origin of the frame in pixel coordinates.
        origin_y: Y origin of the frame in pixel coordinates.
    """
    width: int
    height: int
    pixel_aspect: float = 1.0
    name: str = ""
    origin_x: int = 0
    origin_y: int = 0

    @property
    def full_bbox(self) -> BBox:
        """A bbox covering the entire format."""
        return BBox.full_frame(self.width, self.height)

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "pixel_aspect": self.pixel_aspect,
            "name": self.name,
            "origin_x": self.origin_x,
            "origin_y": self.origin_y,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageFormat":
        return cls(
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            pixel_aspect=float(data.get("pixel_aspect", 1.0)),
            name=str(data.get("name", "")),
            origin_x=int(data.get("origin_x", 0)),
            origin_y=int(data.get("origin_y", 0)),
        )

    def resized(self, width: int, height: int) -> "ImageFormat":
        """Return a new format with different dimensions, keeping other fields."""
        return ImageFormat(
            width=width,
            height=height,
            pixel_aspect=self.pixel_aspect,
            name="",
            origin_x=self.origin_x,
            origin_y=self.origin_y,
        )


# ── Common Format Presets ──

FORMAT_PRESETS: Dict[str, ImageFormat] = {
    "SD NTSC":       ImageFormat(720,  486,  pixel_aspect=0.91, name="SD NTSC"),
    "SD PAL":        ImageFormat(720,  576,  pixel_aspect=1.09, name="SD PAL"),
    "HD 720":        ImageFormat(1280, 720,  name="HD 720"),
    "HD 1080":       ImageFormat(1920, 1080, name="HD 1080"),
    "HD 1080 Anamorphic": ImageFormat(1920, 1080, pixel_aspect=2.0, name="HD 1080 Anamorphic"),
    "UHD 4K":        ImageFormat(3840, 2160, name="UHD 4K"),
    "DCI 4K":        ImageFormat(4096, 2160, name="DCI 4K"),
    "UHD 8K":        ImageFormat(7680, 4320, name="UHD 8K"),
    "Square 1024":   ImageFormat(1024, 1024, name="Square 1024"),
    "Square 2048":   ImageFormat(2048, 2048, name="Square 2048"),
    "Square 4096":   ImageFormat(4096, 4096, name="Square 4096"),
}
