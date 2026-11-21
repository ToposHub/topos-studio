"""B10: channel selection and data-window propagation.

Premultiplied channel restore (alpha=0, negative and HDR values), half-
open windows with empty/negative support, per-node propagation (blur
halo, crop intersection, transform corners + sampling radius, merge
union), and window-aware cache keys separating ROI from full frame.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import pytest

from src.features.composite.core.channel_windows import (
    EMPTY_WINDOW, ChannelSelection, DataWindow, apply_channel_selection,
    merge_window, transform_window, window_cache_key,
)

pytestmark = pytest.mark.composite_core


def _rgba(*pixels):
    return np.array(pixels, dtype=np.float32).reshape(-1, 1, 4)


# ── Channel selection ───────────────────────────────────────────────────────


def test_alpha_only_selection_restores_colors_from_input():
    source = _rgba((0.2, 0.4, 0.6, 0.5))
    effect = _rgba((9.0, 9.0, 9.0, 0.8))  # effect broke colors; kept alpha
    result = apply_channel_selection(
        effect, source, ChannelSelection(red=False, green=False, blue=False))
    # Colors restored from input, alpha kept from the effect.
    assert result[0, 0].tolist() == pytest.approx([0.2, 0.4, 0.6, 0.8])


def test_color_only_selection_restores_alpha_from_input():
    source = _rgba((0.2, 0.4, 0.6, 0.5))
    effect = _rgba((1.0, 0.0, 0.0, 0.9))
    result = apply_channel_selection(
        effect, source,
        ChannelSelection(red=True, green=True, blue=True, alpha=False))
    assert result[0, 0].tolist() == pytest.approx([1.0, 0.0, 0.0, 0.5])


def test_partial_color_selection_restores_only_unselected():
    source = _rgba((0.1, 0.2, 0.3, 1.0))
    effect = _rgba((1.0, 1.0, 1.0, 1.0))
    result = apply_channel_selection(
        effect, source, ChannelSelection(red=True, green=False, blue=False))
    assert result[0, 0].tolist() == pytest.approx([1.0, 0.2, 0.3, 1.0])


def test_selection_preserves_alpha_zero_and_negative_and_hdr():
    source = _rgba((0.0, 0.0, 0.0, 0.0),
                   (-2.0, -4.0, 8.0, 0.5),
                   (16.0, 0.0, 0.0, 1.0))
    effect = _rgba((1.0, 1.0, 1.0, 1.0),
                   (1.0, 1.0, 1.0, 1.0),
                   (1.0, 1.0, 1.0, 1.0))
    result = apply_channel_selection(
        effect, source,
        ChannelSelection(red=False, green=False, blue=False, alpha=False))
    np.testing.assert_allclose(result.reshape(3, 4), source.reshape(3, 4))


def test_select_all_is_passthrough():
    source = _rgba((0.3, 0.3, 0.3, 1.0))
    out = apply_channel_selection(source * 2, source, ChannelSelection())
    assert out[0, 0, 0] == pytest.approx(0.6)  # untouched effect output


def test_shape_mismatch_raises():
    with pytest.raises(ValueError):
        apply_channel_selection(np.zeros((2, 1, 4), np.float32),
                                np.zeros((1, 1, 4), np.float32),
                                ChannelSelection())


# ── Data windows ────────────────────────────────────────────────────────────


def test_windows_support_empty_and_negative_coordinates():
    empty = DataWindow(5, 5, 5, 9)  # zero width though non-zero height
    assert empty.is_empty and empty.size[0] == 0
    negative = DataWindow(-100, -50, -40, -10)
    assert not negative.is_empty
    assert negative.size == (60, 40)
    assert DataWindow(0, 0, 0, 0).is_empty


def test_window_union_and_intersection():
    a = DataWindow(0, 0, 10, 10)
    b = DataWindow(5, 5, 15, 15)
    assert a.union(b) == DataWindow(0, 0, 15, 15)
    assert a.intersect(b) == DataWindow(5, 5, 10, 10)
    disjoint = a.intersect(DataWindow(20, 20, 30, 30))
    assert disjoint.is_empty
    # Union with empty keeps the other operand.
    assert EMPTY_WINDOW.union(a) == a
    assert a.union(EMPTY_WINDOW) == a


def test_blur_window_expands_by_halo():
    window = DataWindow(10, 10, 50, 40)
    assert window.expanded(0) == window
    assert window.expanded(2.5) == DataWindow(7, 7, 53, 43)  # ceil(2.5)=3


def test_crop_window_intersects():
    canvas = DataWindow(0, 0, 1920, 1080)
    layer = DataWindow(-64, -32, 512, 512)
    assert canvas.intersect(layer) == DataWindow(0, 0, 512, 512)


def test_transform_window_bounding_box_with_sampling_radius():
    window = DataWindow(0, 0, 100, 100)
    # 90° rotation of the four corners.
    corners = ((0, 100), (100, 100), (100, 0), (0, 0))
    rotated = transform_window(window, corners, sampling_radius=1.5)
    assert rotated == DataWindow(-2, -2, 102, 102)  # pad = ceil(1.5) = 2
    # Translation only.
    moved = transform_window(window, ((110, 210), (210, 210),
                                      (210, 110), (110, 110)))
    assert moved == DataWindow(110, 110, 210, 210)
    # Empty input stays empty.
    assert transform_window(EMPTY_WINDOW, ()) == EMPTY_WINDOW


def test_merge_window_unions_by_mode():
    fg = DataWindow(10, 10, 40, 40)
    bg = DataWindow(0, 0, 30, 60)
    for mode in ("over", "add", "multiply", "screen"):
        assert merge_window(fg, bg, mode) == DataWindow(0, 0, 40, 60)


def test_window_cache_key_separates_roi_from_full_frame():
    full = DataWindow(0, 0, 3840, 2160)
    roi = DataWindow(512, 512, 1024, 1024)
    key_full = window_cache_key(full, 7, "preview", "rev1")
    key_roi = window_cache_key(roi, 7, "preview", "rev1")
    assert key_full != key_roi
    assert key_full != window_cache_key(full, 8, "preview", "rev1")
    assert key_full != window_cache_key(full, 7, "export", "rev1")
    assert key_full != window_cache_key(full, 7, "preview", "rev2")
    assert key_full == window_cache_key(full, 7, "preview", "rev1")


def test_window_round_trips_through_dict():
    window = DataWindow(-10, 4, 128, 256)
    assert DataWindow.from_dict(window.to_dict()) == window
    assert DataWindow.from_dict("junk") == EMPTY_WINDOW
