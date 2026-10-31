"""
Render Module
Cross-feature render output protocol abstractions.

Public surface: :class:`RenderSource`.  This package is a leaf shared
module -- it defines protocols only and holds no runtime state, so it is
safe to import from any thread without registration or locking concerns.
"""

from .source import RenderSource

__all__ = [
    "RenderSource",
]
