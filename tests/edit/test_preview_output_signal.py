"""Video project output color at the Edit presentation boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest

from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer, EditRenderTask
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_preview_renderer import AssetResolver, PreviewRenderWorker
from src.shared.color.color_space_descriptor import (
    LINEAR_SCENE, OUTPUT_SPACE_KEY, TIMELINE_SPACE_KEY, descriptor_for_space_preset,
)
from src.shared.color.project_video_signal import convert_video_signal_bgr


@pytest.mark.gpu_renderer
@pytest.mark.parametrize(("version", "compress"), [("1.3", True), ("2.0", False)])
def test_aces_gl_input_and_output_keep_signed_values(
    gl_context: Any, version: str, compress: bool,
) -> None:
    from src.shared.color.aces_video_gl import AcesVideoGLPass
    from src.shared.color.project_video_signal import (
        ProjectAcesSettings, aces_input_bgr, render_aces_bgr,
    )

    source = descriptor_for_space_preset("aces2065-1")
    working = descriptor_for_space_preset("acescct")
    settings = ProjectAcesSettings(version, "rec709-g24", "auto", compress)
    rgb = np.array([[[0.5645, 0.4265, 3.799, 0.8], [0.18, 0.18, 0.18, 1.0],
                     [0.02, -0.003, 0.12, 0.25], [18.0, 12.0, 5.0, 0.5]]], dtype=np.float32)
    swap = [2, 1, 0, 3]
    expected_work = aces_input_bgr(
        rgb[..., swap], source, working, reference_gamut_compression=compress)
    expected_output = render_aces_bgr(expected_work, working, settings)[..., swap]
    renderer = EditGpuPreviewRenderer(None)
    renderer.ctx = gl_context
    size = (rgb.shape[1], rgb.shape[0])
    source_tex = gl_context.texture(size, 4, rgb.tobytes(), dtype="f4")
    working_tex = gl_context.texture(size, 4, dtype="f4")
    output_tex = gl_context.texture(size, 4, dtype="f4")
    working_fbo = gl_context.framebuffer([working_tex])
    output_fbo = gl_context.framebuffer([output_tex])
    view = AcesVideoGLPass(gl_context, settings)
    try:
        renderer._render_video_signal(
            source_tex, working_fbo, (source, working), reference_gamut_compression=compress)
        actual_work = np.frombuffer(working_tex.read(), np.float32).reshape(rgb.shape)
        np.testing.assert_allclose(actual_work, expected_work[..., swap], atol=5e-6, rtol=0)
        output_fbo.use()
        view.draw(working_tex, working)
        actual = np.frombuffer(output_tex.read(), np.float32).reshape(rgb.shape)
        np.testing.assert_allclose(actual, expected_output, atol=6e-5, rtol=0)
        np.testing.assert_array_equal(actual[..., 3], rgb[..., 3])
    finally:
        if renderer._signal_vao is not None:
            renderer._signal_vao.release()
            renderer._signal_program.release()
        for resource in (view, working_fbo, output_fbo, source_tex, working_tex, output_tex):
            resource.release()


@pytest.mark.gpu_renderer
@pytest.mark.parametrize("input_id", ["arri-logc3-ei800", "arri-logc4", "sony-slog3-sgamut3cine"])
def test_camera_input_shader_matches_cpu(gl_context: Any, input_id: str) -> None:
    from src.shared.color.project_video_signal import ProjectAcesSettings, aces_input_descriptor

    aces = ProjectAcesSettings("2.0", "rec709-g24", "auto", False)
    source = aces_input_descriptor(aces, input_override=input_id)
    working = descriptor_for_space_preset("acescct")
    rgb = np.array([[[-0.01, 0.02, 0.03, 0.8], [0.4, 0.5, 0.6, 1.0],
                     [0.9, 0.6, 0.72, 0.25]]], dtype=np.float32)
    renderer = EditGpuPreviewRenderer(None)
    renderer.ctx = gl_context
    source_tex = gl_context.texture((3, 1), 4, rgb.tobytes(), dtype="f4")
    output_tex = gl_context.texture((3, 1), 4, dtype="f4")
    output_fbo = gl_context.framebuffer([output_tex])
    try:
        renderer._render_video_signal(source_tex, output_fbo, (source, working))
        actual = np.frombuffer(output_tex.read(), np.float32).reshape(rgb.shape)
        expected = convert_video_signal_bgr(rgb[..., [2, 1, 0, 3]], source, working)[..., [2, 1, 0, 3]]
        np.testing.assert_allclose(actual, expected, atol=5e-6, rtol=5e-7)
    finally:
        if renderer._signal_vao is not None:
            renderer._signal_vao.release()
            renderer._signal_program.release()
        for resource in (output_fbo, source_tex, output_tex):
            resource.release()


def test_edit_qimage_uses_project_output_and_preserves_input() -> None:
    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    project = EditProject.create("Edit output color")
    source = np.array([[[18, 63, 40], [112, 42, 189]]], dtype=np.uint8)
    worker._asset_resolver = AssetResolver(project)
    assert worker._numpy_to_qimage(source).pixelColor(0, 0).getRgb() == (40, 63, 18, 255)

    project = project.with_color_management({
        TIMELINE_SPACE_KEY: "rec709-g24", OUTPUT_SPACE_KEY: "srgb",
    })
    worker._asset_resolver = SimpleNamespace(
        _project=project, get_asset_type=lambda _asset: "video", get_asset_path=lambda _asset: "clip.mov",
    )
    worker._is_identity_layer = Mock(return_value=True)
    worker._get_direct_preview_frame = Mock(return_value=source)
    worker._render_frame_qimage = Mock()
    layer = SimpleNamespace(asset_id="asset", decoder_stream_key="clip", source_frame=0)
    direct = worker._try_render_single_direct_preview_qimage((layer,), (), 0)
    worker._render_frame(0, source, 1)
    result = worker._render_frame_qimage.call_args.args[1]
    expected = convert_video_signal_bgr(
        source, descriptor_for_space_preset("rec709-g24"), descriptor_for_space_preset("srgb"),
    )
    assert result.pixelColor(0, 0).getRgb() == (*expected[0, 0, ::-1], 255)
    assert result.pixelColor(0, 0).getRgb() != (40, 63, 18, 255)
    assert direct == result
    np.testing.assert_array_equal(source[0, 0], [18, 63, 40])


@pytest.mark.gpu_renderer
@pytest.mark.parametrize(("source_id", "output_id"), [
    ("rec709-g24", "display-p3"),
    ("display-p3", "rec2020-bt2020"),
    ("rec709-bt709", "dci-p3-g26"),
    ("dci-p3-g26", "rec709-g24"),
])
def test_edit_gl_signal_matches_cpu_before_quantization(
    gl_context: Any, source_id: str, output_id: str,
) -> None:
    renderer = EditGpuPreviewRenderer(None)
    renderer.ctx = gl_context
    rgb = np.array([[
        [0.015, 0.1, 0.77, 0.75], [0.8, 0.0, 0.4, 0.35],
        [1.12, -0.04, 0.013, 0.84], [0.18, 0.18, 0.18, 1.0],
    ]], dtype=np.float32)
    spaces = descriptor_for_space_preset(source_id), descriptor_for_space_preset(output_id)
    bgr_to_rgb = [2, 1, 0, 3]
    expected = convert_video_signal_bgr(rgb[..., bgr_to_rgb], *spaces)[..., bgr_to_rgb]
    source = gl_context.texture((4, 1), 4, rgb.tobytes(), dtype="f4")
    destination = gl_context.texture((4, 1), 4, dtype="f4")
    fbo = gl_context.framebuffer(color_attachments=[destination])
    try:
        renderer._render_video_signal(source, fbo, spaces)
        actual = np.frombuffer(destination.read(), dtype=np.float32).reshape(rgb.shape)
        np.testing.assert_allclose(actual, expected, atol=3e-6, rtol=0)
        np.testing.assert_array_equal(actual[..., 3], rgb[..., 3])
    finally:
        if renderer._signal_vao is not None:
            renderer._signal_vao.release()
            renderer._signal_program.release()
        fbo.release()
        destination.release()
        source.release()


@pytest.mark.gpu_renderer
def test_premultiplied_composite_encodes_straight_timeline_signal(gl_context: Any) -> None:
    renderer = EditGpuPreviewRenderer(None)
    renderer.ctx = gl_context
    rgb = np.array([[[0.05, 0.10, 0.20, 0.25], [0.0, 0.0, 0.0, 0.0]]], dtype=np.float32)
    source = gl_context.texture((2, 1), 4, rgb.tobytes(), dtype="f4")
    target = gl_context.texture((2, 1), 4, dtype="f4")
    fbo = gl_context.framebuffer([target])
    try:
        renderer._render_video_signal(
            source, fbo, (LINEAR_SCENE, descriptor_for_space_preset("rec709-g24")),
            premultiplied=True,
        )
        actual = np.frombuffer(target.read(), dtype=np.float32).reshape(rgb.shape)
        expected = np.array([[[0.2 ** (1.0 / 2.4), 0.4 ** (1.0 / 2.4),
                               0.8 ** (1.0 / 2.4), 0.25], [0.0, 0.0, 0.0, 0.0]]])
        np.testing.assert_allclose(actual, expected, atol=1e-6, rtol=0)
    finally:
        if renderer._signal_vao is not None:
            renderer._signal_vao.release()
            renderer._signal_program.release()
        fbo.release()
        target.release()
        source.release()


@pytest.mark.gpu_renderer
def test_edit_multitrack_preserves_signal_before_output_quantization(gl_context: Any) -> None:
    renderer = EditGpuPreviewRenderer(None)
    renderer.ctx = gl_context
    width, height = 16, 12
    bottom = (1.125, 0.268, -0.062)
    top = (0.4, 0.75, 0.19)
    layers = [
        {"frame_type": "solid", "track_info": {
            "is_solid": True, "solid_color": bottom, "track_index": 0,
            "transform": (1.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
        }},
        {"frame_type": "solid", "track_info": {
            "is_solid": True, "solid_color": top, "track_index": 1,
            "opacity": 0.25, "preview_grade": {"grading_params": {}},
        }},
    ]
    spaces = descriptor_for_space_preset("rec709-g24"), descriptor_for_space_preset("display-p3")
    try:
        assert renderer._init_shaders()
        renderer._init_quad()
        size = (width, height)
        task = EditRenderTask(0, 0, layers, output_fbo_size=size, output_transform=spaces)
        renderer._process_task(task)
        assert renderer._track_texes[0].dtype == "f2"
        assert renderer._transform_track_texes[0].dtype == "f2"
        assert renderer._graded_track_texes[1].dtype == "f2"
        assert renderer._composite_scratch_a.dtype == "f2"
        assert renderer._composite_tex.dtype == "f1"

        working = 0.75 * np.float32(bottom) + 0.25 * np.float32(top)
        expected = convert_video_signal_bgr(working[::-1], *spaces)[::-1]
        wanted = np.rint(np.clip(expected, 0.0, 1.0) * 255)
        output = np.frombuffer(renderer._composite_tex.read(), np.uint8).reshape((height, width, 4))
        np.testing.assert_allclose(output[height // 2, width // 2, :3], wanted, atol=1, rtol=0)

        renderer._process_task(EditRenderTask(1, 0, layers, output_fbo_size=size))
        assert renderer._track_texes[0].dtype == "f1"
        assert renderer._transform_track_texes[0].dtype == "f1"
        assert renderer._graded_track_texes[1].dtype == "f1"
        assert renderer._composite_scratch_a.dtype == "f1"
    finally:
        renderer.ctx = None
        renderer._cleanup()


def test_failed_precision_change_keeps_active_track_target() -> None:
    renderer = EditGpuPreviewRenderer(None)
    old_tex = Mock(size=(4, 4), dtype="f1")
    old_fbo = Mock()
    failed_tex = Mock()
    new_tex = Mock()
    new_fbo = Mock()
    renderer.ctx = Mock()
    renderer.ctx.texture.side_effect = [failed_tex, new_tex]
    renderer.ctx.framebuffer.side_effect = [RuntimeError("framebuffer allocation failed"), new_fbo]
    renderer._track_texes, renderer._track_fbos = [old_tex], [old_fbo]
    renderer._working_dtype = "f2"

    with pytest.raises(RuntimeError, match="framebuffer allocation failed"):
        renderer._ensure_track_fbo(0, 4, 4)

    failed_tex.release.assert_called_once()
    old_fbo.release.assert_not_called()
    old_tex.release.assert_not_called()
    assert renderer._track_texes == [old_tex]
    assert renderer._track_fbos == [old_fbo]

    assert renderer._ensure_track_fbo(0, 4, 4) == (new_fbo, new_tex)
    old_fbo.release.assert_called_once()
    old_tex.release.assert_called_once()


@pytest.mark.gpu_renderer
@pytest.mark.parametrize(("src", "dst"), [
    ("rec2100-pq", "davinci-wide-gamut-intermediate"),
    ("davinci-wide-gamut-intermediate", "rec2100-pq"),
    ("rec2100-pq", "rec2100-hlg"),
    ("rec2100-hlg", "rec2100-pq"),
    ("rec2100-hlg", "rec709-g24"),
])
def test_hdr_gl_signal_matches_float_cpu(gl_context: Any, src: str, dst: str) -> None:
    from src.shared.color.hdr_video_signal import project_hdr_settings

    renderer = EditGpuPreviewRenderer(None)
    renderer.ctx = gl_context
    rgb = np.array([[[0.07, 0.3, 0.942, .9], [.4, .55, .7, .2], [.8, .76, 1.02, .5],
                     [0, 0, 0, 0], [.58068888, .58068888, .58068888, 1]]], dtype=np.float32)
    spaces = descriptor_for_space_preset(src), descriptor_for_space_preset(dst)
    hdr = project_hdr_settings({"color.timeline_space": src, "color.output_space": dst,
                                "color.hdr.reference_white": "203", "color.hdr.hlg_peak": "600"})
    channels = [2, 1, 0, 3]
    expected = convert_video_signal_bgr(rgb[..., channels], *spaces, luminance=hdr.luminance)[..., channels]
    shape = (rgb.shape[1], rgb.shape[0])
    src_tex = gl_context.texture(shape, 4, rgb.tobytes(), dtype="f4")
    dst_tex = gl_context.texture(shape, 4, dtype="f4")
    fbo = gl_context.framebuffer([dst_tex])
    try:
        renderer._render_video_signal(src_tex, fbo, spaces, luminance=hdr.luminance)
        actual = np.frombuffer(dst_tex.read(), np.float32).reshape(rgb.shape)
        np.testing.assert_allclose(actual, expected, atol=5e-5, rtol=2e-5)
        np.testing.assert_array_equal(actual[..., 3], rgb[..., 3])
    finally:
        if renderer._signal_vao is not None:
            renderer._signal_vao.release()
            renderer._signal_program.release()
        for resource in (fbo, dst_tex, src_tex):
            resource.release()
