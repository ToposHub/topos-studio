"""HDR project interpretation, working space, and render-worker precision."""

from types import SimpleNamespace

import numpy as np
import pytest

from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_preview_renderer import AssetResolver, PreviewRenderWorker
from src.shared.color.color_space_descriptor import LINEAR_SCENE, descriptor_for_space_preset
from src.shared.color.hdr_video_signal import hdr_input_descriptor, hdr_monitor_space, project_hdr_settings
from src.shared.color.project_video_signal import convert_video_signal_bgr


def test_davinci_intermediate_reference_values() -> None:
    working = descriptor_for_space_preset("davinci-wide-gamut-intermediate")
    linear = working.with_transfer(LINEAR_SCENE.transfer)
    # Blackmagic v1.1 relative light / encoded mapping including signed blacks.
    samples = np.repeat(np.array([-.01, 0.0, .18, 1.0, 10.0, 40.0, 100.0])[:, None], 3, axis=1)
    encoded = convert_video_signal_bgr(samples, linear, working)
    np.testing.assert_allclose(encoded[:, 0], [-.104443, 0, .336043, .513837, .756599, .903125, 1], atol=1e-6)
    np.testing.assert_allclose(convert_video_signal_bgr(encoded, working, linear), samples, atol=1e-12)


def test_hdr_project_input_tags_and_monitor_are_independent() -> None:
    settings = {"color.timeline_space": "davinci-wide-gamut-intermediate", "color.output_space": "rec2100-pq",
                "color.hdr.reference_white": "203", "color.hdr.hlg_peak": "600", "color.hdr.monitor": "rec709"}
    hdr = project_hdr_settings(settings)
    assert hdr is not None
    assert hdr.luminance.reference_white_nits == 203.0
    assert hdr.luminance.hlg_system_gamma == pytest.approx(1.1068235)
    working = descriptor_for_space_preset(settings["color.timeline_space"])
    pq = descriptor_for_space_preset(settings["color.output_space"])
    assert hdr_input_descriptor(hdr, working, media_type="video", metadata={
        "color_primaries": "bt2020", "color_transfer": "smpte2084", "color_range": "limited",
    }) == pq
    assert hdr_input_descriptor(hdr, working, media_type="image") == descriptor_for_space_preset("srgb")
    assert hdr_monitor_space(pq, hdr) == descriptor_for_space_preset("rec709-g24")
    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._asset_resolver = AssetResolver(EditProject.create("HDR").with_color_management(settings))
    white = np.full((1, 1, 3), 0.58068888, np.float32)
    encoded = convert_video_signal_bgr(white, pq, working, luminance=hdr.luminance)
    assert worker._numpy_to_qimage(encoded).pixelColor(0, 0).getRgb() == (255, 255, 255, 255)


def test_edit_planar_hdr_decode_retains_limited_10bit_headroom() -> None:
    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._asset_resolver = AssetResolver(EditProject.create("HDR").with_color_management({
        "color.timeline_space": "rec2100-hlg", "color.output_space": "rec2100-hlg",
    }))
    y = np.array([[64, 65, 940, 950]], dtype=np.uint16)
    chroma = np.full_like(y, 512)
    actual = worker._convert_planar_to_bgr(SimpleNamespace(
        pixel_format="yuv444p10le", bit_depth=10, planes=(y, chroma, chroma),
        metadata=SimpleNamespace(color_range="limited", color_matrix="bt2020nc"),
    ))
    assert actual.dtype == np.float32
    np.testing.assert_allclose(actual[..., 0], [[0, 1.0 / 876, 1.0, 886.0 / 876]], atol=1e-7)


def test_composite_direct_hdr_source_gets_project_luminance() -> None:
    from unittest.mock import Mock

    from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
    from src.features.composite.nodes.base_node import TextureResult

    hdr = project_hdr_settings({"color.timeline_space": "rec2100-pq", "color.hdr.reference_white": "203"})
    host = BaseEvaluatorMixin()
    host._eval_ctx = SimpleNamespace(timeline_clip_gpu_provider=SimpleNamespace(project_hdr_recipe=lambda: hdr))
    host._convert_to_linear = Mock(return_value="converted")
    texture = TextureResult(
        texture_id=1, width=4, height=4, color_space="pq",
        frame_metadata={"color_primaries": "bt2020"},
    )
    assert host._apply_idt_to_source_outputs({"output": texture}) == {"output": "converted"}
    host._convert_to_linear.assert_called_once_with(
        texture, project_source=descriptor_for_space_preset("rec2100-pq"),
        reference_gamut_compression=False, luminance=hdr.luminance,
    )
