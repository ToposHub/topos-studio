import numpy as np

from src.features.color.core.color_manager import ColorMatrix, ColorRange
from src.shared.media.color_decode import ColorConverter


def test_yuv_to_rgb_handles_odd_chroma_dimensions() -> None:
    converter = ColorConverter()

    y = np.full((5, 7), 128, dtype=np.uint8)
    u = np.full((3, 4), 128, dtype=np.uint8)
    v = np.full((3, 4), 128, dtype=np.uint8)

    rgb = converter.yuv_to_rgb(
        y,
        u,
        v,
        matrix=ColorMatrix.BT709,
        range_=ColorRange.LIMITED,
        bit_depth=8,
    )

    assert rgb.shape == (5, 7, 3)
    assert np.isfinite(rgb).all()


def test_yuv_to_rgb_handles_mismatched_u_v_shapes() -> None:
    converter = ColorConverter()

    y = np.full((6, 9), 512, dtype=np.uint16)
    u = np.full((3, 5), 512, dtype=np.uint16)
    v = np.full((6, 5), 512, dtype=np.uint16)

    rgb = converter.yuv_to_rgb(
        y,
        u,
        v,
        matrix=ColorMatrix.BT2020_NC,
        range_=ColorRange.LIMITED,
        bit_depth=10,
    )

    assert rgb.shape == (6, 9, 3)
    assert np.isfinite(rgb).all()
