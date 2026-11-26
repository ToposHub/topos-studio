"""SDR video display adaptation. The export and scope signals never use this module."""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from typing import Any

import numpy as np
from PyQt6.QtGui import QImage

from src.shared.color.color_space_descriptor import ColorSpaceDescriptor, descriptor_for_space_preset
from src.shared.color.monitor_calibration import CalibrationRevision, load_monitor_calibration
from src.shared.color.video_delivery_profiles import (
    SDR_VIDEO_OUTPUTS,
    WIDE_GAMUT_TIFF_OUTPUTS,
    sdr_video_icc_profile,
)

USE_DISPLAY_ICC = "color.preview.use_macos_display_profile"
LUT_SIZE = 129
DISPLAY_COLOR_TEXTURE_UNIT = 5

DISPLAY_COLOR_GLSL = """
uniform bool topos_monitor_icc_enabled;
uniform sampler3D topos_monitor_icc_lut;

vec3 topos_monitor_color(vec3 rgb) {
    if (!topos_monitor_icc_enabled) {
        return rgb;
    }
    float n = float(textureSize(topos_monitor_icc_lut, 0).x);
    // This bounded LUT is the final SDR device boundary. The render graph
    // and the project output signal retain their extended range.
    vec3 shaped = pow(clamp(rgb, 0.0, 1.0), vec3(2.0 / 3.0));
    vec3 uv = (shaped * (n - 1.0) + 0.5) / n;
    return clamp(texture(topos_monitor_icc_lut, uv).rgb, 0.0, 1.0);
}
"""

_LOG = logging.getLogger(__name__)

_SDR_SIGNALS = {
    (descriptor.primaries, descriptor.transfer): preset
    for preset in (*SDR_VIDEO_OUTPUTS, *WIDE_GAMUT_TIFF_OUTPUTS)
    for descriptor in (descriptor_for_space_preset(preset),)
}


def sdr_display_preset(output: ColorSpaceDescriptor | None) -> str | None:
    """Identify a supported SDR signal by its primaries and transfer."""
    if output is None:
        return None
    return _SDR_SIGNALS.get((output.primaries, output.transfer))

@lru_cache(maxsize=len(_SDR_SIGNALS) + 1)
def metal_display_color_space(preset: str | None, managed: bool) -> Any:
    """Return the layer's source tag, or None for untagged video passthrough."""
    import Quartz

    if preset is None:
        return Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    if not managed:
        return None
    color_space = Quartz.CGColorSpaceCreateWithICCData(sdr_video_icc_profile(preset))
    if color_space is None:
        raise ValueError(f"ColorSync rejected the {preset} viewer profile")
    return color_space


def set_metal_video_color(layer: Any, preset: str | None, managed: bool) -> bool:
    """Tag the existing SDR layer on its owning GUI thread."""
    if layer is None or sys.platform != "darwin":
        return False
    try:
        layer.setColorspace_(metal_display_color_space(preset, managed))
    except (ImportError, ValueError, AttributeError):
        _LOG.warning("Could not configure the native SDR viewer color space", exc_info=True)
        return False
    return True


def _draw_color_managed_bitmap(
    source: bytes, destination: bytes, pixels: bytes, width: int, height: int,
    *, depth: int, layout: int, stride: int, extended: bool = False,
) -> bytearray:
    import objc
    import Quartz

    with objc.autorelease_pool():
        source_space = Quartz.CGColorSpaceCreateWithICCData(source)
        destination_space = Quartz.CGColorSpaceCreateWithICCData(destination)
        if source_space is None or destination_space is None:
            raise ValueError("ColorSync rejected a viewer ICC profile")
        if extended:
            # Keep the transform smooth where an out-of-gamut channel
            # crosses the monitor boundary. The shader clips after interpolation.
            make_extended = getattr(Quartz, "CGColorSpaceCreateExtended", None)
            if callable(make_extended):
                destination_space = make_extended(destination_space) or destination_space
        provider = Quartz.CGDataProviderCreateWithCFData(pixels)
        image = Quartz.CGImageCreate(
            width, height, depth, depth * 4, stride, source_space, layout, provider,
            None, False, Quartz.kCGRenderingIntentDefault,
        )
        output = bytearray(stride * height)
        context = Quartz.CGBitmapContextCreate(
            output, width, height, depth, stride, destination_space, layout,
        )
        if image is None or context is None:
            raise ValueError("ColorSync could not create the viewer bitmap transform")
        Quartz.CGContextSetBlendMode(context, Quartz.kCGBlendModeCopy)
        Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
        return output


@lru_cache(maxsize=6)
def build_display_lut(preset: str, destination: bytes) -> np.ndarray:
    """Sample native ColorSync with extra domain resolution near black."""
    import Quartz

    axis = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float32) ** 1.5
    rgba = np.ones((LUT_SIZE, LUT_SIZE, LUT_SIZE, 4), dtype=np.float32)
    rgba[..., 0] = axis[None, None, :]
    rgba[..., 1] = axis[None, :, None]
    rgba[..., 2] = axis[:, None, None]
    output = _draw_color_managed_bitmap(
        sdr_video_icc_profile(preset), destination, rgba.tobytes(), LUT_SIZE, LUT_SIZE**2,
        depth=32, stride=LUT_SIZE * 16, extended=True,
        layout=(Quartz.kCGBitmapByteOrder32Little | Quartz.kCGBitmapFloatComponents
                | Quartz.kCGImageAlphaPremultipliedLast),
    )
    rgb = np.ascontiguousarray(
        np.frombuffer(output, dtype=np.float32).reshape(rgba.shape)[..., :3], dtype=np.float16,
    )
    rgb.setflags(write=False)
    return rgb


def convert_display_image(image: QImage, preset: str, destination: bytes) -> QImage:
    """Apply ColorSync to a detached SDR QImage on the timeline preview worker."""
    import Quartz

    image = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    output = _draw_color_managed_bitmap(
        sdr_video_icc_profile(preset), destination, image.constBits().asstring(image.sizeInBytes()),
        image.width(), image.height(), depth=8, stride=image.bytesPerLine(),
        layout=Quartz.kCGBitmapByteOrder32Big | Quartz.kCGImageAlphaPremultipliedLast,
    )
    return QImage(
        bytes(output), image.width(), image.height(), image.bytesPerLine(), image.format(),
    ).copy()


@lru_cache(maxsize=3)
def build_calibrated_display_lut(
    preset: str | None, destination: bytes, calibration_path: str,
    calibration_version: CalibrationRevision,
) -> np.ndarray:
    """Compose calibration after any ICC conversion, keyed by file revision."""
    calibrated = load_monitor_calibration(calibration_path)
    if preset and destination:
        source = build_display_lut(preset, destination).astype(np.float32)
    else:
        axis = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float32) ** 1.5
        source = np.empty((LUT_SIZE, LUT_SIZE, LUT_SIZE, 3), dtype=np.float32)
        source[..., 0] = axis[None, None, :]
        source[..., 1] = axis[None, :, None]
        source[..., 2] = axis[:, None, None]
    corrected = calibrated.apply_rgb(source)
    np.clip(corrected, -65504.0, 65504.0, out=corrected)
    corrected = np.ascontiguousarray(corrected, dtype=np.float16)
    corrected.setflags(write=False)
    return corrected


def convert_calibrated_display_image(
    image: QImage, preset: str | None, destination: bytes, calibration_path: str,
) -> QImage:
    """Apply display-device transforms to a detached viewer image."""
    if preset and destination:
        image = convert_display_image(image, preset, destination)
    if not calibration_path or image.isNull():
        return image

    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    size = (image.height(), image.bytesPerLine())
    pixels = np.frombuffer(image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8)
    pixels = pixels.reshape(size).copy()
    rgba = pixels[:, :4 * image.width()].reshape(image.height(), image.width(), 4)
    calibration = load_monitor_calibration(calibration_path)
    corrected = calibration.apply_rgb(rgba[..., :3].astype(np.float32) / 255.0)
    rgba[..., :3] = np.rint(np.clip(corrected, 0.0, 1.0) * 255.0).astype(np.uint8)
    return QImage(pixels.data, image.width(), image.height(), image.bytesPerLine(), image.format()).copy()
