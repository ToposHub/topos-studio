"""Float RGB ICC transforms used when Pillow would quantize RGB to eight bits."""

from __future__ import annotations

import ctypes
from ctypes.util import find_library
from functools import lru_cache
from pathlib import Path

import numpy as np

# lcms2.h TYPE_RGB_FLT and INTENT_RELATIVE_COLORIMETRIC.
_RGB_FLOAT = (1 << 22) | (4 << 16) | (3 << 3) | 4
_RELATIVE_COLORIMETRIC = 1


@lru_cache(maxsize=1)
def _lcms() -> ctypes.CDLL:
    from PIL import _imagingcms

    pillow_extension = Path(_imagingcms.__file__)
    candidates = [find_library("lcms2"), str(pillow_extension)]
    for directory in (pillow_extension.parent / ".dylibs", pillow_extension.parent / ".libs",
                      pillow_extension.parent.parent / "pillow.libs"):
        candidates.extend(str(path) for path in directory.glob("*lcms*"))
    signatures = {
        "cmsOpenProfileFromMem": (ctypes.c_void_p, (ctypes.c_void_p, ctypes.c_uint32)),
        "cmsCreate_sRGBProfile": (ctypes.c_void_p, ()),
        "cmsCreateTransform": (ctypes.c_void_p, (ctypes.c_void_p, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)),
        "cmsDoTransform": (None, (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)),
        "cmsDeleteTransform": (None, (ctypes.c_void_p,)),
        "cmsCloseProfile": (ctypes.c_int, (ctypes.c_void_p,)),
    }
    for candidate in filter(None, candidates):
        try:
            library = ctypes.CDLL(candidate)
            for name, (result, args) in signatures.items():
                function = getattr(library, name)
                function.restype = result
                function.argtypes = args
            return library
        except (OSError, AttributeError):
            continue
    raise RuntimeError("A high-precision RGB ICC transform requires LittleCMS 2")


def icc_rgb_to_srgb(rgb: np.ndarray, profile: bytes) -> np.ndarray:
    """Convert RGB to sRGB using a float buffer and preserve sample precision."""
    source = np.ascontiguousarray(rgb, dtype=np.float32)
    if source.ndim != 3 or source.shape[2] != 3:
        raise ValueError("An RGB ICC transform requires a three-channel image")
    library = _lcms()
    input_profile = library.cmsOpenProfileFromMem(profile, len(profile))
    if not input_profile:
        raise ValueError("Cannot open the embedded RGB ICC profile")
    output_profile = library.cmsCreate_sRGBProfile()
    try:
        if not output_profile:
            raise RuntimeError("Cannot create the target sRGB ICC profile")
        transform = library.cmsCreateTransform(input_profile, _RGB_FLOAT, output_profile,
                                               _RGB_FLOAT, _RELATIVE_COLORIMETRIC, 0)
        if not transform:
            raise ValueError("The embedded ICC profile does not support an RGB transform")
        try:
            result = np.empty_like(source)
            library.cmsDoTransform(transform, source.ctypes.data, result.ctypes.data, source.size // 3)
            return result
        finally:
            library.cmsDeleteTransform(transform)
    finally:
        if output_profile:
            library.cmsCloseProfile(output_profile)
        library.cmsCloseProfile(input_profile)
