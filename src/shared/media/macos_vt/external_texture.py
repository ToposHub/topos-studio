"""
External OpenGL texture descriptor for macOS VideoToolbox zero-copy probes.

The object is intentionally small: it mirrors the attributes that render-side
code expects from a ModernGL-like texture without owning the underlying GL
resource.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExternalGLTexture:
    """Non-owning descriptor for an externally created OpenGL texture."""

    texture_id: int
    target: int
    size: tuple[int, int]
    internal_format: str = ""
    pixel_format: str = ""
    pixel_type: str = ""

    @property
    def glo(self) -> int:
        """ModernGL-compatible texture object id."""
        return self.texture_id

    @property
    def width(self) -> int:
        return self.size[0]

    @property
    def height(self) -> int:
        return self.size[1]
