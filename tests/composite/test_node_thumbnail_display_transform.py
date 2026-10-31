"""Tests for scene-linear node thumbnail display conversion."""

import numpy as np

from src.features.composite.ui.node_thumbnail import linear_rgb_to_display
from src.shared.color.color_conversion import srgb_to_linear


def test_srgb_thumbnail_matches_viewer_display_values() -> None:
    """A color chosen in the sRGB picker should render identically in a node."""
    picker_rgb = np.array([245.0, 136.0, 136.0], dtype=np.float32) / 255.0
    linear_rgb = np.array(
        [srgb_to_linear(channel) for channel in picker_rgb],
        dtype=np.float32,
    ).reshape(1, 1, 3)

    encoded = linear_rgb_to_display(linear_rgb)

    np.testing.assert_allclose(encoded[0, 0], picker_rgb, atol=1e-5)


def test_thumbnail_does_not_display_raw_linear_values() -> None:
    """The ODT must lift midtones instead of treating linear data as sRGB."""
    linear_rgb = np.array([[[0.246202, 0.246202, 0.246202]]], dtype=np.float32)

    encoded = linear_rgb_to_display(linear_rgb)

    np.testing.assert_allclose(encoded[0, 0, 0], 136.0 / 255.0, atol=1e-5)
    assert encoded[0, 0, 0] > linear_rgb[0, 0, 0]


def test_rec709_thumbnail_uses_the_active_video_transfer() -> None:
    """Video compositions use the same Rec.709 transfer as the viewer."""
    linear_rgb = np.array([[[0.246202, 0.246202, 0.246202]]], dtype=np.float32)

    encoded = linear_rgb_to_display(linear_rgb, transfer=2)

    expected = 1.099 * (0.246202 ** 0.45) - 0.099
    np.testing.assert_allclose(encoded[0, 0, 0], expected, atol=1e-6)
