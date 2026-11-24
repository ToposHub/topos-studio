"""RGB TIFF delivery profiles for validated wide-gamut SDR outputs."""

from functools import lru_cache

import numpy as np
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColorSpace


SDR_VIDEO_OUTPUTS = ("rec709-g24", "rec709-bt709", "srgb")
WIDE_GAMUT_TIFF_OUTPUTS = (
    "display-p3", "p3-d65-g24", "dci-p3-g26", "rec2020-g24", "rec2020-bt2020",
)


def video_tiff_output_profile(preset: str) -> bytes:
    """Build the ICC profile matching a wide-gamut TIFF output."""
    if preset not in WIDE_GAMUT_TIFF_OUTPUTS:
        raise ValueError(f"No TIFF delivery profile for {preset!r}")
    return sdr_video_icc_profile(preset)


@lru_cache(maxsize=len(SDR_VIDEO_OUTPUTS) + len(WIDE_GAMUT_TIFF_OUTPUTS))
def sdr_video_icc_profile(preset: str) -> bytes:
    """Source ICC for a full-range SDR RGB signal."""
    match preset:
        case "srgb":
            space = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
        case "rec709-g24":
            space = QColorSpace(QColorSpace.Primaries.SRgb, QColorSpace.TransferFunction.Gamma, 2.4)
        case "display-p3":
            space = QColorSpace(QColorSpace.NamedColorSpace.DisplayP3)
        case "p3-d65-g24":
            space = QColorSpace(QColorSpace.Primaries.DciP3D65,
                                QColorSpace.TransferFunction.Gamma, 2.4)
        case "dci-p3-g26":
            space = QColorSpace(QPointF(0.314, 0.351), QPointF(0.68, 0.32),
                                QPointF(0.265, 0.69), QPointF(0.15, 0.06),
                                QColorSpace.TransferFunction.Gamma, 2.6)
        case "rec2020-g24":
            space = QColorSpace(QColorSpace.Primaries.Bt2020,
                                QColorSpace.TransferFunction.Gamma, 2.4)
        case "rec709-bt709" | "rec2020-bt2020":
            from src.shared.color.color_space_descriptor import descriptor_for_space_preset
            from src.shared.color.color_transform_provider import BuiltinColorProvider
            from src.shared.color.types import ColorTransfer

            encoded = np.linspace(0.0, 1.0, 8193)
            signal = np.repeat(encoded[:, None], 3, axis=1)
            source = descriptor_for_space_preset(preset)
            linear = BuiltinColorProvider().transform_signal(
                signal, source, source.with_transfer(ColorTransfer.LINEAR),
            )
            # ICC tone response tables require a monotonic inverse. Flatten
            # the short overlap from the rounded 10-bit standard constants.
            curve = np.rint(np.maximum.accumulate(linear[:, 0]) * 65535).astype(int)
            primaries = (QColorSpace.Primaries.SRgb if preset == "rec709-bt709"
                         else QColorSpace.Primaries.Bt2020)
            space = QColorSpace(primaries, curve.tolist())
        case _:
            raise ValueError(f"No SDR signal ICC profile for {preset!r}")
    if preset in SDR_VIDEO_OUTPUTS and preset != "srgb":
        space.setDescription("Rec.709 Gamma 2.4" if preset == "rec709-g24" else "Rec.709 (Scene)")
    profile = bytes(space.iccProfile())
    if not profile:
        raise RuntimeError(f"Could not build SDR signal ICC profile for {preset!r}")
    return profile
