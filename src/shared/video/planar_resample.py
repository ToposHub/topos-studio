"""Spatial resampling of individual video planes in their native sample domain."""

import numpy as np


def downsample_plane(plane: np.ndarray, width: int, height: int) -> np.ndarray:
    """Average each destination pixel's source footprint, preserving the sample type."""
    if plane.shape == (height, width):
        return plane

    import cv2

    return cv2.resize(plane, (width, height), interpolation=cv2.INTER_AREA)
