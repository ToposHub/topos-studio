"""Extract PCM arrays from PyAV audio frames without reading unused planes."""
from __future__ import annotations

from typing import Any

import numpy as np

_PLANAR_DTYPES = {
    "u8p": np.uint8, "s16p": np.int16, "s32p": np.int32,
    "s64p": np.int64, "fltp": np.float32, "dblp": np.float64,
}


def audio_frame_array(frame: Any) -> np.ndarray:
    """Return PyAV's normal ndarray shape, bounded by declared speaker count."""
    layout = getattr(frame, "layout", None)
    channels = getattr(layout, "nb_channels", 0)
    fmt = getattr(frame, "format", None)
    if not isinstance(channels, int) or channels < 8 or not fmt.is_planar:
        return frame.to_ndarray()
    # Some PyAV builds enumerate beyond extended_data[0:nb_channels] on
    # planar 7.1. The extra entries may be invalid pointers. Never read them.
    dtype = _PLANAR_DTYPES.get(fmt.name)
    if dtype is None:
        raise ValueError(f"Unsupported planar PCM format {fmt.name!r}")
    planes = frame.planes[:channels]
    if len(planes) != channels:
        raise ValueError(f"Expected {channels} PCM planes, got {len(planes)}")
    return np.stack([np.frombuffer(plane, dtype=dtype, count=frame.samples) for plane in planes])
