"""SDR viewer ICC and presentation checks independent of the workstation profile."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PyQt6.QtGui import QColor, QImage

from src.features.color.shaders.shaders import (
    DESKTOP_PASSTHROUGH_FRAGMENT,
    PASSTHROUGH_VERTEX,
    ROTATION_PREVIEW_FRAGMENT,
    ROTATION_PREVIEW_VERTEX,
    SPLIT_VIEW_FRAGMENT,
)
from src.shared.color.color_space_descriptor import descriptor_for_space_preset
from src.shared.color.desktop_video_color import (
    LUT_SIZE,
    build_calibrated_display_lut,
    build_display_lut,
    convert_calibrated_display_image,
    convert_display_image,
    metal_display_color_space,
    sdr_display_preset,
)
from src.shared.color.monitor_calibration import monitor_calibration_revision
from src.shared.color.video_delivery_profiles import (
    SDR_VIDEO_OUTPUTS,
    WIDE_GAMUT_TIFF_OUTPUTS,
    sdr_video_icc_profile,
    video_tiff_output_profile,
)


@pytest.mark.parametrize("preset", (*SDR_VIDEO_OUTPUTS, *WIDE_GAMUT_TIFF_OUTPUTS))
def test_viewer_uses_project_signal_profile(preset: str) -> None:
    assert sdr_display_preset(descriptor_for_space_preset(preset)) == preset
    profile = sdr_video_icc_profile(preset)
    assert profile[36:40] == b"acsp"
    if preset in WIDE_GAMUT_TIFF_OUTPUTS:
        assert profile == video_tiff_output_profile(preset)


def test_hdr_is_outside_sdr_display_adaptation() -> None:
    assert sdr_display_preset(None) is None
    assert sdr_display_preset(descriptor_for_space_preset("rec2100-pq")) is None
    assert sdr_display_preset(descriptor_for_space_preset("rec2100-hlg")) is None


@pytest.fixture
def red_inversion_lut(tmp_path: Path) -> Path:
    pytest.importorskip("PyOpenColorIO")
    path = tmp_path / "calibration.cube"
    values = (f"{1 - r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1))
    path.write_text("LUT_3D_SIZE 2\n" + "".join(values), encoding="ascii")
    return path


def test_calibrated_viewer_image_preserves_alpha_and_original(red_inversion_lut: Path) -> None:
    original = QImage(2, 1, QImage.Format.Format_RGBA8888)
    original.setPixelColor(0, 0, QColor(64, 128, 200, 191))
    original.setPixelColor(1, 0, QColor(17, 38, 119, 255))

    actual = convert_calibrated_display_image(original, None, b"", str(red_inversion_lut))

    assert actual.pixelColor(0, 0).getRgb() == (191, 128, 200, 191)
    assert actual.pixelColor(1, 0).getRgb() == (238, 38, 119, 255)
    assert original.pixelColor(0, 0).getRgb() == (64, 128, 200, 191)


def test_calibration_lattice_follows_draw_pass_channel_order(red_inversion_lut: Path) -> None:
    path = str(red_inversion_lut)
    result = build_calibrated_display_lut(None, b"", path, monitor_calibration_revision(path))

    np.testing.assert_array_equal(result[0, 0, 0], [1.0, 0.0, 0.0])
    np.testing.assert_array_equal(result[-1, 0, -1], [0.0, 0.0, 1.0])
    np.testing.assert_allclose(result[32, 96, 64], [1.0 - 0.5**1.5, 0.75**1.5, 0.25**1.5], atol=4e-4)


def _native_match(pixels: np.ndarray, preset: str, destination: bytes) -> np.ndarray:
    quartz = pytest.importorskip("Quartz")
    source = quartz.CGColorSpaceCreateWithICCData(sdr_video_icc_profile(preset))
    display = quartz.CGColorSpaceCreateWithICCData(destination)
    result = []
    for rgb in pixels:
        color = quartz.CGColorCreate(source, [*map(float, rgb), 1.0])
        matched = quartz.CGColorCreateCopyByMatchingToColorSpace(
            display, quartz.kCGRenderingIntentDefault, color, None,
        )
        result.append(list(quartz.CGColorGetComponents(matched)[:3]))
    return np.clip(np.array(result), 0.0, 1.0)


@pytest.mark.skipif(sys.platform != "darwin", reason="ColorSync and CAMetalLayer")
def test_color_timeline_image_and_metal_use_the_same_source_icc(red_inversion_lut: Path) -> None:
    quartz = pytest.importorskip("Quartz")
    assert metal_display_color_space("rec709-g24", False) is None
    managed = metal_display_color_space("rec709-g24", True)
    assert bytes(quartz.CGColorSpaceCopyICCData(managed)) == sdr_video_icc_profile("rec709-g24")
    assert metal_display_color_space(None, False) is not None

    destination = sdr_video_icc_profile("srgb")
    pixels = np.array([[26, 64, 128], [128, 80, 64], [255, 0, 32]])
    image = QImage(1, len(pixels), QImage.Format.Format_RGB888)
    for row, rgb in enumerate(pixels):
        image.setPixelColor(0, row, QColor(*map(int, rgb)))
    result = convert_display_image(image, "rec709-g24", destination)
    expected = _native_match(pixels / 255.0, "rec709-g24", destination)
    actual = np.array([result.pixelColor(0, row).getRgb()[:3] for row in range(len(pixels))])
    np.testing.assert_allclose(actual, np.rint(expected * 255), atol=1)
    assert image.pixelColor(0, 0).getRgb() == (26, 64, 128, 255)
    calibrated = convert_calibrated_display_image(image, "rec709-g24", destination, str(red_inversion_lut))
    calibrated_rgb = np.array([calibrated.pixelColor(0, row).getRgb()[:3] for row in range(len(pixels))])
    actual[:, 0] = 255 - actual[:, 0]
    np.testing.assert_array_equal(calibrated_rgb, actual)


@pytest.mark.gpu_renderer
def test_color_viewer_shader_variants_compile(gl_context: Any) -> None:
    for vertex, fragment in (
        (PASSTHROUGH_VERTEX, DESKTOP_PASSTHROUGH_FRAGMENT),
        (PASSTHROUGH_VERTEX, SPLIT_VIEW_FRAGMENT),
        (ROTATION_PREVIEW_VERTEX, ROTATION_PREVIEW_FRAGMENT),
    ):
        program = gl_context.program(vertex_shader=vertex, fragment_shader=fragment)
        assert "topos_monitor_icc_lut" in program
        program.release()


@pytest.mark.gpu_renderer
@pytest.mark.skipif(sys.platform != "darwin", reason="ColorSync")
def test_viewer_color_shader_matches_native_conversion(gl_context: Any, red_inversion_lut: Path) -> None:
    ctx = gl_context
    source = "rec2020-g24"
    destination = sdr_video_icc_profile("srgb")
    samples = np.array([
        [0.03, 0.03, 0.03], [0.17, 0.17, 0.17], [0.5, 0.5, 0.5],
        [0.43, 0.29, 0.22], [0.3, 0.56, 0.76], [1.0, 0.02, 0.15],
        [0.1, 0.94, 0.2], [0.15, 0.2, 1.0],
    ], dtype=np.float32)
    lut = build_display_lut(source, destination)
    program = ctx.program(vertex_shader=PASSTHROUGH_VERTEX, fragment_shader=DESKTOP_PASSTHROUGH_FRAGMENT)
    quad = ctx.buffer(np.array([
        -1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1,
    ], dtype=np.float32))
    vao = ctx.vertex_array(program, [(quad, "2f 2f", "in_vert", "in_texcoord")])
    image = ctx.texture((len(samples), 1), 3, data=samples.tobytes(), dtype="f4")
    texture = ctx.texture3d((LUT_SIZE,) * 3, 3, data=lut.tobytes(), dtype="f2")
    target = ctx.simple_framebuffer((len(samples), 1), components=4, dtype="f4")

    try:
        image.use(0)
        texture.use(1)
        program["u_texture"].value = 0
        program["topos_monitor_icc_lut"].value = 1
        target.use()
        program["topos_monitor_icc_enabled"].value = False
        vao.render(mode=ctx.TRIANGLE_STRIP)
        signal = np.frombuffer(target.read(components=3, dtype="f4"), dtype=np.float32).reshape(-1, 3)
        np.testing.assert_allclose(signal, samples, atol=1e-6)

        program["topos_monitor_icc_enabled"].value = True
        vao.render(mode=ctx.TRIANGLE_STRIP)
        actual = np.frombuffer(target.read(components=3, dtype="f4"), dtype=np.float32).reshape(-1, 3)
        expected = _native_match(samples, source, destination)
        np.testing.assert_allclose(actual, expected, atol=2 / 255)

        path = str(red_inversion_lut)
        corrected_lut = build_calibrated_display_lut(source, destination, path, monitor_calibration_revision(path))
        texture.write(corrected_lut.tobytes())
        vao.render(mode=ctx.TRIANGLE_STRIP)
        actual = np.frombuffer(target.read(components=3, dtype="f4"), dtype=np.float32).reshape(-1, 3)
        expected[:, 0] = 1.0 - expected[:, 0]
        np.testing.assert_allclose(actual, expected, atol=2 / 255)
    finally:
        for resource in (target, texture, image, vao, quad, program):
            resource.release()
