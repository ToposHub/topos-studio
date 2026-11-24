"""Read RGB OpenEXR files at their native precision."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np


def read_exr_header(path: str) -> dict[str, Any]:
    import OpenEXR

    with OpenEXR.File(path, header_only=True) as file:
        return dict(file.header())


def _read_zip_scanlines(path: str, header: Mapping[str, Any]) -> np.ndarray:
    import av

    # FFmpeg's EXR decoder uses zlib for ZIP scanlines. This avoids the
    # libdeflate SIMD dispatch in some OpenEXR builds on Intel Macs.
    with av.open(path) as file:
        frame = next(file.decode(video=0), None)
        if frame is None or frame.format.name not in (
            "gbrpf16le", "gbrapf16le", "gbrpf32le", "gbrapf32le",
        ):
            raise ValueError(f"Unsupported ZIP OpenEXR RGB pixel format: {path}")
        dtype = np.dtype("<f2" if "f16" in frame.format.name else "<f4")
        planes = [
            np.frombuffer(plane, dtype=dtype).reshape(
                plane.height, plane.line_size // dtype.itemsize)[:, :plane.width]
            for plane in frame.planes
        ]
        # AVFrame stores G, B, R, A. Keep the decoded floats and reorder only.
        order = (1, 0, 2, 3) if len(planes) == 4 else (1, 0, 2)
        bgr = np.stack([planes[index] for index in order], axis=-1).astype(np.float32, copy=False)
    lo, hi = header["dataWindow"]
    width, height = hi - lo + 1
    if bgr.shape[:2] != (height, width):
        display_lo, display_hi = header["displayWindow"]
        x, y = lo - display_lo
        display_width, display_height = display_hi - display_lo + 1
        if (bgr.shape[:2] != (display_height, display_width) or x < 0 or y < 0
                or x + width > display_width or y + height > display_height):
            raise ValueError(f"Unsupported ZIP OpenEXR data window: {path}")
        bgr = np.ascontiguousarray(bgr[y:y + height, x:x + width])
    return bgr


def read_exr_bgr(path: str, header: Mapping[str, Any] | None = None) -> np.ndarray:
    """Decode a standard EXR's BGR(A) channels, retaining associated alpha."""
    import OpenEXR

    header = read_exr_header(path) if header is None else header
    names = {channel.name for channel in header["channels"]}
    if ({"R", "G", "B"} <= names and header.get("type") == OpenEXR.scanlineimage
            and header.get("compression") in (OpenEXR.ZIP_COMPRESSION, OpenEXR.ZIPS_COMPRESSION)):
        return _read_zip_scanlines(path, header)
    with OpenEXR.File(path, separate_channels=False) as file:
        channels = file.channels()
        rgb = channels.get("RGBA") or channels.get("RGB")
        if rgb is None or rgb.pixels.ndim != 3:
            raise ValueError(f"OpenEXR input requires RGB or RGBA channels: {path}")
        pixels = rgb.pixels
        order = [2, 1, 0, 3] if pixels.shape[-1] == 4 else [2, 1, 0]
        return np.ascontiguousarray(pixels[..., order], dtype=np.float32)
