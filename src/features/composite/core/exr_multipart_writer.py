"""Multi-part EXR writer (CP-068).

Writes one EXR file with multiple parts (beauty RGBA + auxiliary AOVs)
using the OpenEXR 3.x python bindings. The reader side
(``image_loader.probe_exr_header`` / multi-part decode) already speaks
this format; this module is the missing write path.

Binding quirks discovered on OpenEXR 3.4.x/3.5.x (documented so nobody
"fixes" them back):
- Parts must be passed to the ``OpenEXR.File(parts_list)`` CONSTRUCTOR.
  Constructing an empty File and appending parts writes a corrupted
  multipart file ("more scan lines than data window").
- Do NOT set dataWindow/displayWindow by hand — the binding infers them
  from the channel array shape ((height, width) float32).
- ``type`` must be ``OpenEXR.Storage.scanlineimage`` and each part header
  needs a unique ``name``.
"""

from __future__ import annotations

import logging
from typing import Dict, List

import numpy as np

logger = logging.getLogger(__name__)

# AOV pass name -> part channels. Single-channel data passes carry their
# semantic channel name; colour-ish passes get RGBA.
_AOV_CHANNEL_MAP = {
    "beauty": ["R", "G", "B", "A"],
    "depth": ["Z"],
    "normal": ["X", "Y", "Z"],
    "position": ["X", "Y", "Z"],
    "uv": ["U", "V"],
    "motion_vector": ["U", "V"],
    "object_id": ["ID"],
    "material_id": ["ID"],
}


def channels_for_pass(pass_name: str) -> List[str]:
    """Canonical channel names for a render pass part."""
    return list(_AOV_CHANNEL_MAP.get(pass_name, [pass_name.upper()]))


def write_exr_multipart(path: str,
                        beauty_rgba: np.ndarray,
                        aov_passes: Dict[str, np.ndarray],
                        compression: str = "PIZ") -> bool:
    """Write a multi-part EXR: part 0 = beauty RGBA, one part per AOV.

    Args:
        path: Output file path.
        beauty_rgba: (H, W, 4) float32 premultiplied-linear beauty.
        aov_passes: {pass_name: (H, W[, C]) float32 array}. C must match
            ``channels_for_pass(pass_name)`` length (1 or 3) — or the array
            may already be 2D for single-channel passes.
        compression: OpenEXR compression name ("PIZ"/"ZIP"/"ZIPS"/"DWAB"/
            "NONE"...); invalid names fall back to PIZ.

    Returns:
        True on success; False when bindings are unavailable or the write
        raised (caller falls back to the single-part cv2 writer).
    """
    try:
        import OpenEXR
    except ImportError:
        logger.info("OpenEXR bindings unavailable — multipart write skipped")
        return False

    h, w = beauty_rgba.shape[:2]
    if beauty_rgba.ndim != 3 or beauty_rgba.shape[2] < 3:
        logger.warning("write_exr_multipart: beauty must be (H, W, 4)")
        return False

    comp_attr = getattr(OpenEXR, f"{compression}_COMPRESSION", None)
    if comp_attr is None:
        comp_attr = OpenEXR.PIZ_COMPRESSION

    def _part(name: str, channels: Dict[str, np.ndarray]) -> "OpenEXR.Part":
        return OpenEXR.Part(
            {"compression": comp_attr,
             "type": OpenEXR.Storage.scanlineimage,
             "name": name},
            channels)

    rgba = np.asarray(beauty_rgba, dtype=np.float32)
    if rgba.ndim == 3 and rgba.shape[2] == 3:
        # RGB 无 alpha：补不透明 alpha，避免下方 [:, :, 3] 越界（此前
        # 越界被 except 吞掉后静默回落单层写出）。
        rgba = np.concatenate(
            [rgba, np.ones(rgba.shape[:2] + (1,), np.float32)], axis=2)
    rgba = np.ascontiguousarray(rgba[:, :, :4])
    parts = [_part("beauty", {
        "R": rgba[:, :, 0].copy(), "G": rgba[:, :, 1].copy(),
        "B": rgba[:, :, 2].copy(), "A": rgba[:, :, 3].copy(),
    })]

    for pass_name in sorted(aov_passes.keys()):
        arr = np.asarray(aov_passes[pass_name], dtype=np.float32)
        names = channels_for_pass(pass_name)
        if arr.ndim == 3:
            if arr.shape[2] != len(names):
                logger.warning(
                    "write_exr_multipart: pass '%s' has %d channels, "
                    "expected %d — skipped", pass_name, arr.shape[2],
                    len(names))
                continue
            chans = {n: np.ascontiguousarray(arr[:, :, i])
                     for i, n in enumerate(names)}
        elif arr.ndim == 2 and len(names) == 1:
            chans = {names[0]: np.ascontiguousarray(arr)}
        else:
            logger.warning("write_exr_multipart: pass '%s' shape %s not "
                           "compatible with channels %s — skipped",
                           pass_name, arr.shape, names)
            continue
        if arr.shape[0] != h or arr.shape[1] != w:
            logger.warning("write_exr_multipart: pass '%s' size %s != "
                           "beauty %s — skipped", pass_name,
                           (arr.shape[1], arr.shape[0]), (w, h))
            continue
        parts.append(_part(pass_name, chans))

    try:
        outfile = OpenEXR.File(parts)
        outfile.write(path)
        return True
    except Exception:
        logger.exception("write_exr_multipart: failed for %s", path)
        return False
