"""
Color Space definitions for the composite engine.

Provides a lightweight, string-based color space tagging system for
TextureResult metadata.  Designed so that future OCIO integration can
extend this without breaking existing graphs.

Working space (stage 1 of composite color management):
  The composite graph runs in a **scene-linear** working space. Source
  nodes tag their output with the space the pixels are actually in
  (usually SRGB for JPEG/PNG, LINEAR for EXR / generated content).
  ``BaseEvaluatorMixin._apply_idt_to_source_outputs`` is the central IDT
  hook: it decodes any SRGB source to LINEAR as the data enters the graph,
  so every downstream effect and merge runs in linear light.

  The viewer (composite_viewport.py) applies the matching ODT — a
  linear→sRGB encode — so scene-linear data displays correctly on an sRGB
  monitor. The IDT + ODT pair cancel out for an unprocessed sRGB image
  (it looks identical before/after), but linear math now works correctly
  for merges/blurs/etc.

Short-term: only LINEAR and SRGB are meaningful inside the graph.
  - Source nodes tag their output with the *actual* space of the pixel
    data they produce (before the IDT runs).
  - The IDT hook converts SRGB → LINEAR at the graph boundary.
  - Effect nodes (Blur, Grade, Merge, etc.) propagate the input's
    color_space unchanged — they operate on whatever space the data is in
    (which is LINEAR once the IDT has run).
  - ColorSpaceTransformNode handles explicit sRGB ↔ Linear conversion.
  - The viewer's ODT converts LINEAR → SRGB for display.

Stage 2 (future): add a working-space config (linear / ACEScg), an ODT
config (sRGB / Rec.709 / PQ), project-level persistence, and integrate
the export path with ColorManager/ICC.
"""

import math


class ColorSpace:
    """String constants for color space tagging.

    Stored as plain strings so serialization is trivial and extensible.
    """
    LINEAR = "linear"      # Scene-linear (no transfer function applied)
    SRGB = "srgb"          # sRGB EOTF applied (display-referred)
    DISPLAY = "display"    # Current display transform (viewer-only, never baked)
    UNKNOWN = "unknown"    # Unspecified — treated as-is

    # All valid values (for validation / UI options)
    ALL = (LINEAR, SRGB, DISPLAY, UNKNOWN)


def is_linear(color_space: str) -> bool:
    """Check whether a color space tag represents scene-linear data."""
    return color_space == ColorSpace.LINEAR


def srgb_to_linear_value(c: float) -> float:
    """Convert a single sRGB-encoded value to linear (CPU-side utility)."""
    if c <= 0.04045:
        return c / 12.92
    return math.pow((c + 0.055) / 1.055, 2.4)


def linear_to_srgb_value(c: float) -> float:
    """Convert a single linear value to sRGB-encoded (CPU-side utility)."""
    if c <= 0.0031308:
        return c * 12.92
    return 1.055 * math.pow(max(c, 0.0), 1.0 / 2.4) - 0.055
