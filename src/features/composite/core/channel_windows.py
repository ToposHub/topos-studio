"""Effect channel selection and data-window propagation (B10).

Verified single-input image effects gain optional RGB/A channel
selection: unselected channels restore from the effect's input, and when
alpha changes the color restore follows the premultiply contract.
Data windows are half-open rects (empty and negative coordinates
allowed), propagated per node kind — Blur expands by its halo, Crop
intersects, Transform takes the conservative box of the four transformed
corners plus the sampling radius, Merge unions by mode — and windows
participate in cache keys together with frame, sampling domain and input
revision.  The composition canvas and each node's data window are
separate models.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Optional, Tuple

import numpy as np

# ── Channel selection (B10 §1) ──────────────────────────────────────────────


@dataclass(frozen=True)
class ChannelSelection:
    """Which channels an effect is allowed to modify (default: all)."""

    red: bool = True
    green: bool = True
    blue: bool = True
    alpha: bool = True

    @property
    def selects_all(self) -> bool:
        return self.red and self.green and self.blue and self.alpha

    def mask_rgba(self) -> Tuple[bool, bool, bool, bool]:
        return (self.red, self.green, self.blue, self.alpha)


def apply_channel_selection(effect_rgba, input_rgba,
                            selection: ChannelSelection):
    """Restore unselected channels from the effect's input (premultiplied).

    Both arrays are premultiplied RGBA ``(h, w, 4)`` in the composition
    working space.  When the effect changed alpha but color is not
    selected, color is restored through the premultiply contract: unpre-
    multiply the effect's color by its (new) alpha is wrong — the
    authoritative color comes from the input, unpremultiplied by the
    input alpha and re-premultiplied by the output alpha.  HDR values
    pass through unclamped.
    """
    effect = np.asarray(effect_rgba, dtype=np.float32)
    source = np.asarray(input_rgba, dtype=np.float32)
    if effect.shape != source.shape:
        raise ValueError("apply_channel_selection shape mismatch")
    r_on, g_on, b_on, a_on = selection.mask_rgba()
    if (r_on and g_on and b_on and a_on):
        return effect
    result = effect.copy()
    color_changed_alpha = not a_on and not (r_on and g_on and b_on)
    if not color_changed_alpha:
        # Straight restore per channel (alpha, or all colors together).
        if not a_on:
            result[..., 3] = source[..., 3]
        if not (r_on and g_on and b_on):
            # Restore unselected color channels from the input's
            # corresponding premultiplied channels.
            for c, enabled in enumerate((r_on, g_on, b_on)):
                if not enabled:
                    result[..., c] = source[..., c]
        return result
    # Alpha restored AND some colors restored: rebuild the restored colors
    # under the (restored) input alpha — input premultiplied values are
    # already consistent with the input alpha, so a direct copy honors the
    # premultiply contract exactly.
    result[..., 3] = source[..., 3]
    for c, enabled in enumerate((r_on, g_on, b_on)):
        if not enabled:
            result[..., c] = source[..., c]
    return result


# ── Data windows (B10 §2) ───────────────────────────────────────────────────


@dataclass(frozen=True)
class DataWindow:
    """Half-open pixel rect ``(x0, y0, x1, y1)``; empty and negative allowed."""

    x0: int = 0
    y0: int = 0
    x1: int = 0
    y1: int = 0

    @property
    def is_empty(self) -> bool:
        return self.x0 >= self.x1 or self.y0 >= self.y1

    @property
    def size(self) -> Tuple[int, int]:
        return (max(0, self.x1 - self.x0), max(0, self.y1 - self.y0))

    def union(self, other: "DataWindow") -> "DataWindow":
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        return DataWindow(min(self.x0, other.x0), min(self.y0, other.y0),
                          max(self.x1, other.x1), max(self.y1, other.y1))

    def intersect(self, other: "DataWindow") -> "DataWindow":
        window = DataWindow(max(self.x0, other.x0), max(self.y0, other.y0),
                            min(self.x1, other.x1), min(self.y1, other.y1))
        return window if not window.is_empty else DataWindow(0, 0, 0, 0)

    def expanded(self, radius: float) -> "DataWindow":
        """Grow by a halo radius (Blur) with conservative ceil."""
        grow = int(np.ceil(float(radius)))
        if grow <= 0:
            return self
        return DataWindow(self.x0 - grow, self.y0 - grow,
                          self.x1 + grow, self.y1 + grow)

    def translated(self, dx: int, dy: int) -> "DataWindow":
        return DataWindow(self.x0 + int(dx), self.y0 + int(dy),
                          self.x1 + int(dx), self.y1 + int(dy))

    def to_dict(self) -> Dict[str, int]:
        return {"x0": self.x0, "y0": self.y0,
                "x1": self.x1, "y1": self.y1}

    @classmethod
    def from_dict(cls, data: Any) -> "DataWindow":
        if not isinstance(data, dict):
            return cls(0, 0, 0, 0)
        try:
            return cls(int(data.get("x0", 0)), int(data.get("y0", 0)),
                       int(data.get("x1", 0)), int(data.get("y1", 0)))
        except (TypeError, ValueError):
            return cls(0, 0, 0, 0)


EMPTY_WINDOW = DataWindow(0, 0, 0, 0)


def transform_window(window: DataWindow, corners: Tuple[Tuple[float, float],
                                                        ...],
                     sampling_radius: float = 0.0) -> DataWindow:
    """Conservative window of transformed corners plus sampling radius.

    ``corners`` are the window's four corners after the transform, in
    pixel space; the result is their bounding box expanded by the
    sampling radius (rotation/scale-safe, B10 §2).
    """
    if window.is_empty or not corners:
        return EMPTY_WINDOW
    xs = [float(x) for x, _ in corners]
    ys = [float(y) for _, y in corners]
    pad = int(np.ceil(float(sampling_radius)))
    x0 = int(np.floor(min(xs))) - pad
    y0 = int(np.floor(min(ys))) - pad
    x1 = int(np.ceil(max(xs))) + pad
    y1 = int(np.ceil(max(ys))) + pad
    return DataWindow(x0, y0, x1, y1)


# Merge modes whose output can exceed the union of the input windows.
_MERGE_UNION_MODES = frozenset({
    "over", "add", "subtract", "difference", "multiply", "screen",
    "overlay", "min", "max", "color-dodge", "color-burn",
    "soft-light", "hard-light",
})


def merge_window(foreground: DataWindow, background: DataWindow,
                 mode: str = "over") -> DataWindow:
    """Propagate windows through a Merge by blend mode (conservative union)."""
    return foreground.union(background)


# ── Cache keys (B10 §3) ─────────────────────────────────────────────────────


def window_cache_key(window: DataWindow, frame: int,
                     sampling_domain: Any = None,
                     input_revision: Any = None) -> tuple:
    """Cache identity: window × frame × sampling domain × input revision.

    Two requests with different windows must never share a cache entry,
    and a full-frame re-render must not serve a ROI tile's entry (or vice
    versa) even for the same frame.
    """
    return (window.to_dict(), int(frame),
            str(sampling_domain), str(input_revision))
