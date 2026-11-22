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

from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np
from numpy.typing import ArrayLike, NDArray

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


def apply_channel_selection(effect_rgba: ArrayLike, input_rgba: ArrayLike,
                            selection: ChannelSelection) -> NDArray[np.float32]:
    """Restore unselected channels from the effect's input (premultiplied).

    Both arrays contain premultiplied RGBA in the composition working
    space. Each selected RGB component comes from the effect, and each
    unselected component comes from the input. If its alpha differs from
    the output alpha, restore its straight value with its original alpha
    and premultiply by the output alpha. Zero alpha contributes zero color.
    """
    effect = np.asarray(effect_rgba, dtype=np.float32)
    source = np.asarray(input_rgba, dtype=np.float32)
    if effect.shape != source.shape or effect.ndim == 0 or effect.shape[-1] != 4:
        raise ValueError("Channel selection requires matching RGBA arrays")
    if selection.selects_all:
        return effect

    selected_rgb = (selection.red, selection.green, selection.blue)
    effect_alpha = effect[..., 3:4]
    source_alpha = source[..., 3:4]
    output_alpha = effect_alpha if selection.alpha else source_alpha
    original_alpha = np.where(selected_rgb, effect_alpha, source_alpha)
    original_rgb = np.where(selected_rgb, effect[..., :3], source[..., :3])
    rescale = original_alpha != output_alpha
    straight = np.divide(original_rgb, original_alpha,
                         out=np.zeros_like(original_rgb), where=rescale & (original_alpha != 0.0))
    result = np.empty_like(effect)
    result[..., :3] = np.where(rescale, straight * output_alpha, original_rgb)
    result[..., 3:4] = output_alpha
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
        if self.is_empty:
            return self
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
        except (TypeError, ValueError, OverflowError):
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
    pad = max(0, int(np.ceil(float(sampling_radius))))
    x0 = int(np.floor(min(xs))) - pad
    y0 = int(np.floor(min(ys))) - pad
    x1 = int(np.ceil(max(xs))) + pad
    y1 = int(np.ceil(max(ys))) + pad
    return DataWindow(x0, y0, x1, y1)


def merge_window(foreground: DataWindow, background: DataWindow,
                 mode: str = "over") -> DataWindow:
    """Return a conservative bound for any Merge blend mode and Mix value."""
    return foreground.union(background)


# ── Cache keys (B10 §3) ─────────────────────────────────────────────────────


def window_cache_key(window: DataWindow, frame: int,
                     sampling_domain: Optional[Hashable] = None,
                     input_revision: Optional[Hashable] = None,
                     ) -> tuple[DataWindow, int, Optional[Hashable], Optional[Hashable]]:
    """Cache identity: window × frame × sampling domain × input revision.

    Two requests with different windows must never share a cache entry,
    and a full-frame re-render must not serve a ROI tile's entry (or vice
    versa) even for the same frame.
    """
    return (window, int(frame), sampling_domain, input_revision)
