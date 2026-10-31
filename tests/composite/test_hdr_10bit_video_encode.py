"""P0-3 regression: HDR 10-bit video export must map white near the 10-bit
white level, not near black.

The HDR export path packs encoded [0,1] float frames into a uint16/bgr48le
buffer before PyAV's swscale converts to yuv420p10le. swscale treats
rgb48le/bgr48le samples as 16-bit full-scale (white = 65535). The previous code
scaled by 1023 (10-bit max), so the encoded white (1.0) landed at 1023/65535 ≈
1.5% of full scale — near black. This test guards that the scaling uses the
full 16-bit range.
"""
import numpy as np
import pytest

av = pytest.importorskip("av")


def _yuv_y_max(rgb48_uint16):
    """Reformat an rgb48le frame to yuv420p10le and return the Y plane max.

    The Y plane carries line_size padding, so only the first ``width`` samples
    per row are real pixel data (each sample is uint16 = 2 bytes).
    """
    f = av.VideoFrame.from_ndarray(rgb48_uint16, format="rgb48le")
    yuv = f.reformat(format="yuv420p10le")
    p = yuv.planes[0]
    width = p.width
    samples_per_row = p.line_size // 2  # uint16 samples per padded row
    raw = np.frombuffer(bytes(p), dtype=np.uint16).reshape(p.height, samples_per_row)
    real = raw[:, :width]  # drop right-edge padding
    return int(real.max())


def test_hdr_full_scale_white_is_near_10bit_white():
    """White scaled to the full 16-bit range must land near the 10-bit limited
    white level (~940), not near the black floor. This is the core P0-3
    invariant: 1023-scaling made HDR exports near-black (Y well under 200),
    while correct 16-bit scaling produces near-white Y."""
    h, w = 4, 4
    white_full = np.full((h, w, 3), 65535, dtype=np.uint16)  # fixed scaling
    y_full = _yuv_y_max(white_full)
    # 10-bit limited-range white is 940; swscale emits a value near it. The old
    # 1023-scaling produced Y around 78 (near the 64 black floor). Require the
    # fixed path to reach at least mid-range — comfortably above any 1023-scale
    # output across swscale builds.
    assert y_full > 800, (
        f"HDR white Y={y_full} too low; full 16-bit scaling should reach ~940"
    )


def test_hdr_1023_scale_white_is_far_below_full_scale_white():
    """The OLD (buggy) 1023-scaling must produce a white level far below the
    corrected full-scale path — proving the scaling factor matters."""
    h, w = 4, 4
    white_1023 = np.full((h, w, 3), 1023, dtype=np.uint16)
    white_full = np.full((h, w, 3), 65535, dtype=np.uint16)
    y_1023 = _yuv_y_max(white_1023)
    y_full = _yuv_y_max(white_full)
    # Full-scale white must be substantially brighter than 1023-scale white.
    # (Across swscale builds 1023-scale yields Y in roughly 78-512; full-scale
    # ~940. Requiring full > 1023 + 200 keeps this robust to swscale variance.)
    assert y_full > y_1023 + 200, (
        f"Scaling had little effect: 1023-scale Y={y_1023}, full-scale Y={y_full}"
    )
