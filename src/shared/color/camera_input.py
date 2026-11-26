"""Scene-referred camera input curves and AP0 matrices from the ACES CSCs."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.shared.color.types import ColorPrimaries, ColorTransfer


@dataclass(frozen=True)
class CameraLogCurve:
    mode: int
    linear_slope: float
    linear_offset: float
    log_slope: float
    log_offset: float
    linear_break: float
    encoded_break: float
    toe_slope: float

    def convert(self, rgb: np.ndarray, *, to_linear: bool) -> np.ndarray:
        if to_linear:
            linear = (np.exp2((rgb - self.log_offset) / self.log_slope) - self.linear_offset) / self.linear_slope
            toe = (rgb - self.encoded_break) / self.toe_slope + self.linear_break
            return np.where(rgb < self.encoded_break, toe, linear)
        encoded = self.log_slope * np.log2(np.maximum(rgb * self.linear_slope + self.linear_offset, 1e-10))
        toe = (rgb - self.linear_break) * self.toe_slope + self.encoded_break
        return np.where(rgb < self.linear_break, toe, encoded + self.log_offset)


# OCIO Studio ACES reference CSCs expressed with base-2 logarithms. LogC3 is
# the ALEXA EI800 scene-exposure transform. Sony includes separate gamut CSCs.
CAMERA_LOG_CURVES = {
    ColorTransfer.ARRI_LOGC3_EI800: CameraLogCurve(
        13, 5.55555555555556, 0.0522722750251688, 0.0744114957512506, 0.385536998692443,
        0.0105909904954696, 0.149658137915835, 5.36765479527298),
    ColorTransfer.ARRI_LOGC4: CameraLogCurve(
        14, 2231.82630906769, 64.0, 0.0647954196341293, -0.295908392682586,
        -0.0180569961199113, 0.0, 8.80303321033176),
    ColorTransfer.SONY_S_LOG3: CameraLogCurve(
        15, 5.26315789473684, 0.0526315789473684, 0.076949505245485, 0.410557184750733,
        0.01125, 0.167360991879629, 6.62194371177582),
}

CAMERA_TO_AP0 = {
    ColorPrimaries.ARRI_WIDE_GAMUT3: np.array([
        [0.680205505106279, 0.236136601606481, 0.0836578932872398],
        [0.0854149797421404, 1.01747087860704, -0.102885858349182],
        [0.00205652166929683, -0.0625625003847921, 1.06050597871549],
    ]),
    ColorPrimaries.ARRI_WIDE_GAMUT4: np.array([
        [0.750957362824734, 0.144422786709757, 0.104619850465509],
        [0.000821837079380207, 1.007397584885, -0.00821942196438358],
        [-0.000499952143533471, -0.000854177231436971, 1.00135412937497],
    ]),
    ColorPrimaries.SONY_S_GAMUT3: np.array([
        [0.75298259539984, 0.143370216235557, 0.103647188364603],
        [0.0217076974414429, 1.01531883550528, -0.0370265329467195],
        [-0.00941605274963355, 0.00337041785882367, 1.00604563489081],
    ]),
    ColorPrimaries.SONY_S_GAMUT3_CINE: np.array([
        [0.638788667185978, 0.272351433711262, 0.0888598991027595],
        [-0.00391590602528224, 1.0880732308974, -0.0841573248721177],
        [-0.0299072021239151, -0.0264325799101947, 1.05633978203411],
    ]),
}

CAMERA_INPUTS = {
    "arri-logc3-ei800": ("ARRI LogC3 (EI800) / AWG3", ColorPrimaries.ARRI_WIDE_GAMUT3, ColorTransfer.ARRI_LOGC3_EI800),
    "arri-logc4": ("ARRI LogC4 / AWG4", ColorPrimaries.ARRI_WIDE_GAMUT4, ColorTransfer.ARRI_LOGC4),
    "sony-slog3-sgamut3": ("Sony S-Log3 / S-Gamut3", ColorPrimaries.SONY_S_GAMUT3, ColorTransfer.SONY_S_LOG3),
    "sony-slog3-sgamut3cine": ("Sony S-Log3 / S-Gamut3.Cine", ColorPrimaries.SONY_S_GAMUT3_CINE, ColorTransfer.SONY_S_LOG3),
}
