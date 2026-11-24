"""Video project signal transforms at the timeline and delivery boundaries.

The SDR video graph works on normalized RGB signal values. Its default
input interpretation follows the timeline space, as in a manual grading
workflow. Scene-linear sources, such as a composited image, are encoded
into that space before joining the timeline. Level packing belongs to the
video encoder so RGB buffers always use full normalized values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from src.shared.color.color_space_descriptor import (
    ACESCG_SCENE,
    LEGACY_SPACE,
    LINEAR_SCENE,
    OUTPUT_SPACE_KEY,
    TIMELINE_SPACE_KEY,
    ColorSpaceDescriptor,
    descriptor_for_space_preset,
    project_output_space,
    project_timeline_space,
)
from src.shared.color.color_transform_provider import (
    BuiltinColorProvider, VideoSignalLuminance, apply_aces_output_transform,
    apply_aces_reference_gamut_compression,
)


_SIGNAL_PROVIDER = BuiltinColorProvider()

ACES_VERSION_KEY = "color.aces.version"
ACES_INPUT_KEY = "color.aces.input"
ACES_RGC_KEY = "color.aces.reference_gamut_compression"
ACES_LINEAR_SPACES = ("aces2065-1", "acescg")
ACES_WORKING_SPACES = ("acescct", "acescc", *ACES_LINEAR_SPACES)
ACES_DISPLAY_TARGETS = ("rec709-g24",)
ACES_INPUT_SPACES = ("auto", *ACES_WORKING_SPACES, "rec709-g24", "rec709-bt709",
                     "srgb", "linear-rec709")
ACES_EXR_CHROMATICITIES = {
    "aces2065-1": (0.7347, 0.2653, 0.0000, 1.0000, 0.0001, -0.0770, 0.32168, 0.33767),
    "acescg": (0.7130, 0.2930, 0.1650, 0.8300, 0.1280, 0.0440, 0.32168, 0.33767),
}


@dataclass(frozen=True)
class ProjectAcesSettings:
    version: str
    target: str
    input: str
    reference_gamut_compression: bool


def project_aces_settings(settings: Mapping[str, Any] | None) -> ProjectAcesSettings | None:
    if not settings or project_timeline_space(settings) not in ACES_WORKING_SPACES:
        return None
    version = settings.get(ACES_VERSION_KEY, "1.3")
    input_space = settings.get(ACES_INPUT_KEY, "auto")
    if version not in ("1.3", "2.0") or input_space not in ACES_INPUT_SPACES:
        raise ValueError("Invalid ACES project version or input color space")
    return ProjectAcesSettings(
        version, "rec709-g24", input_space,
        version == "1.3" and settings.get(ACES_RGC_KEY) in (True, "on"),
    )


def aces_input_descriptor(
    aces: ProjectAcesSettings, path: str = "", *, media_type: str = "",
    exr_header: Mapping[str, Any] | None = None,
) -> ColorSpaceDescriptor:
    """Use explicit input interpretation, or EXR chromaticities / SDR defaults."""
    selection = aces.input
    if selection == "auto":
        if Path(path).suffix.lower() == ".exr":
            if exr_header is None:
                from src.shared.io.openexr_io import read_exr_header

                exr_header = read_exr_header(path)
            chromaticities = exr_header.get("chromaticities")
            if chromaticities is None:
                selection = "aces2065-1"
            else:
                values = np.asarray(chromaticities, dtype=np.float64).reshape(8)
                spaces = {**ACES_EXR_CHROMATICITIES,
                          "linear-rec709": (0.64, 0.33, 0.30, 0.60, 0.15, 0.06,
                                            0.3127, 0.3290)}
                selection = next((name for name, xy in spaces.items()
                                  if np.allclose(values, xy, atol=0.00005, rtol=0.0)), "")
                if not selection:
                    raise ValueError("Unrecognized OpenEXR chromaticities. Select the ACES input color space.")
        else:
            selection = "srgb" if media_type == "image" else "rec709-g24"
    return LINEAR_SCENE if selection == "linear-rec709" else descriptor_for_space_preset(selection)


def read_aces_exr(path: str, aces: ProjectAcesSettings) -> tuple[np.ndarray, ColorSpaceDescriptor]:
    """Read an RGB(A) EXR into straight float BGR(A), including signed values."""
    from src.shared.io.openexr_io import read_exr_header

    header = read_exr_header(path)
    source = aces_input_descriptor(aces, path, media_type="image", exr_header=header)
    return _read_straight_exr_bgr(path, header), source


def _read_straight_exr_bgr(path: str, header: Mapping[str, Any]) -> np.ndarray:
    from src.shared.io.openexr_io import read_exr_bgr

    pixels = read_exr_bgr(path, header)
    if pixels.shape[-1] == 4:
        alpha = pixels[..., 3:4]
        rgb = pixels[..., :3]
        np.divide(rgb, alpha, out=rgb, where=alpha != 0.0)
        rgb[alpha[..., 0] == 0.0] = 0.0
    return pixels


def read_aces_still(path: str, aces: ProjectAcesSettings) -> tuple[np.ndarray, ColorSpaceDescriptor]:
    if Path(path).suffix.lower() == ".exr":
        return read_aces_exr(path, aces)
    return read_video_still_bgr(path), aces_input_descriptor(aces, path, media_type="image")


def read_video_still_bgr(path: str) -> np.ndarray:
    """Read a still at native depth for a managed video timeline."""
    if Path(path).suffix.lower() == ".exr":
        from src.shared.io.openexr_io import read_exr_header

        return _read_straight_exr_bgr(path, read_exr_header(path))
    import cv2

    pixels = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if pixels is None:
        raise OSError(f"Unable to read video input image {path}")
    if pixels.ndim == 2:
        pixels = cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGR)
    return pixels


def normalized_video_bgr(frame: np.ndarray) -> np.ndarray:
    if np.issubdtype(frame.dtype, np.integer):
        return frame.astype(np.float32) / float(np.iinfo(frame.dtype).max)
    return frame.astype(np.float32, copy=False)


def aces_monitor_space() -> ColorSpaceDescriptor:
    return descriptor_for_space_preset("rec709-g24")


def aces_timeline_black(space: ColorSpaceDescriptor) -> float:
    """Scene black encoded in the selected ACES working space."""
    if space.transfer.value == "acescct":
        return 0.0729055341958355
    if space.transfer.value == "acescc":
        return (-16.0 + 9.72) / 17.52
    return 0.0


def aces_input_bgr(
    frame: np.ndarray, source: ColorSpaceDescriptor, destination: ColorSpaceDescriptor,
    *, reference_gamut_compression: bool = False,
) -> np.ndarray:
    """Decode and convert an ACES input before any timeline grading."""
    frame = normalized_video_bgr(frame)
    if not reference_gamut_compression:
        return convert_video_signal_bgr(frame, source, destination)
    ap1 = convert_video_signal_bgr(frame, source, ACESCG_SCENE).copy()
    rgb = np.ascontiguousarray(ap1[..., 2::-1])
    apply_aces_reference_gamut_compression(rgb)
    ap1[..., :3] = rgb[..., ::-1]
    return convert_video_signal_bgr(ap1, ACESCG_SCENE, destination)


def render_aces_bgr(
    frame: np.ndarray, source: ColorSpaceDescriptor, aces: ProjectAcesSettings,
) -> np.ndarray:
    """Apply the same display transform used by the project viewers."""
    result = normalized_video_bgr(frame).copy()
    scene_rgb = _SIGNAL_PROVIDER.transform_signal(result[..., 2::-1], source, ACESCG_SCENE)
    rgb = apply_aces_output_transform(
        scene_rgb, ACESCG_SCENE, aces_version=aces.version, target=aces.target,
    )
    result[..., :3] = rgb[..., ::-1]
    return result


def video_signal_spaces(
    settings: Mapping[str, Any] | None,
) -> tuple[ColorSpaceDescriptor, ColorSpaceDescriptor] | None:
    """Resolve the project snapshot, keeping pre-preset projects on their path."""
    if not settings or not ({TIMELINE_SPACE_KEY, OUTPUT_SPACE_KEY} & settings.keys()):
        return None
    timeline = project_timeline_space(settings)
    output = project_output_space(settings)
    if timeline == output == LEGACY_SPACE:
        return None
    return (_video_signal_space(settings, timeline, "preview"),
            _video_signal_space(settings, output, "output"))


def video_signal_uses_legacy_space(
    settings: Mapping[str, Any] | None, *, timeline_only: bool = False,
) -> bool:
    """Whether a signal conversion still depends on an older space setting."""
    timeline_legacy = project_timeline_space(settings) == LEGACY_SPACE
    if timeline_only:
        return timeline_legacy
    output = project_output_space(settings)
    # Preserve existing mixed projects. New SDR output spaces require their
    # exact matrix and curve even when a saved timeline uses legacy keys.
    return output == LEGACY_SPACE or (
        timeline_legacy and output in ("rec709-g24", "rec709-bt709", "srgb", "display-p3")
    )


def _video_signal_space(
    settings: Mapping[str, Any], choice: str, scope: str,
) -> ColorSpaceDescriptor:
    if choice != LEGACY_SPACE:
        return descriptor_for_space_preset(choice)
    # Leave stored full/limited packing to the decoder and encoder.
    return ColorSpaceDescriptor.from_dict({
        "primaries": settings.get(f"color.{scope}.timeline_primaries") or "bt709",
        "transfer": settings.get(f"color.{scope}.timeline_transfer") or "bt709",
    })


def encode_timeline_linear_bgr(
    frame: np.ndarray, destination: ColorSpaceDescriptor,
    *, luminance: VideoSignalLuminance | None = None, legacy: bool = False,
) -> np.ndarray:
    """Encode scene-linear Rec.709 BGR(A) into the selected timeline signal."""
    return convert_video_signal_bgr(frame, LINEAR_SCENE, destination,
                                    luminance=luminance, legacy=legacy)


def convert_video_signal_bgr(
    frame: np.ndarray, source: ColorSpaceDescriptor, destination: ColorSpaceDescriptor,
    *, luminance: VideoSignalLuminance | None = None, legacy: bool = False,
) -> np.ndarray:
    """Convert full-range BGR(A), preserving alpha, precision and frame dtype."""
    if source == destination:
        return frame
    integer = np.issubdtype(frame.dtype, np.integer)
    scale = float(np.iinfo(frame.dtype).max) if integer else 1.0
    working_dtype = np.float32 if integer else np.result_type(frame.dtype, np.float32)
    rgb = frame[..., :3][..., ::-1].astype(working_dtype, copy=False)
    if integer:
        rgb /= scale
    if legacy:
        encoded = _SIGNAL_PROVIDER.transform(rgb, source, destination)
    else:
        encoded = _SIGNAL_PROVIDER.transform_signal(rgb, source, destination, luminance=luminance)
    result = frame.copy()
    bgr = encoded[..., ::-1]
    if integer:
        bgr = np.rint(np.clip(bgr, 0.0, 1.0) * scale)
    result[..., :3] = bgr.astype(frame.dtype, copy=False)
    return result
