"""Base Grade reaches saved Correctors, preview and the delivery render routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.color.core.base_grade import BASE_GRADE_DEFAULTS, apply_base_grade_linear
from src.features.color.core.base_grade_runtime import base_grade_graph_signal
from src.features.color.core.color_graph_migration import stack_primary_execution_graph
from src.features.color.core.layer import Layer
from src.shared.color.color_space_descriptor import (
    LINEAR_SCENE, OUTPUT_SPACE_KEY, TIMELINE_SPACE_KEY, descriptor_for_space_preset,
)
from src.shared.color.color_transform_provider import BuiltinColorProvider, VideoSignalLuminance
from src.shared.color.project_video_signal import video_signal_spaces
from src.shared.project.color_graph import ColorGraphData
from src.shared.project.color_stack import ColorStackData


@pytest.fixture
def base_grade_case() -> tuple[ColorGraphData, np.ndarray, dict[str, float]]:
    params = {
        "base_grade_balance_exposure": 0.35,
        "base_grade_balance_a": 0.05,
        "base_grade_balance_b": -0.07,
        "base_grade_flare": -0.002,
        "base_grade_contrast": 1.18,
        "base_grade_contrast_pivot": 0.17,
        "base_grade_saturation": 1.25,
        "base_grade_dim_exposure": -0.4,
        "base_grade_dim_saturation": 0.85,
        "base_grade_dim_falloff": 0.24,
        "base_grade_light_exposure": 0.65,
        "base_grade_light_b": 0.04,
        "base_grade_dark_pivot": -1.4,
        "base_grade_dark_a": -0.02,
        "base_grade_bright_exposure": -0.25,
    }
    stack = ColorStackData.create(asset_id=None, name="Base Grade").with_grading_params(params)
    graph = stack_primary_execution_graph(stack)
    assert graph is not None
    colors = np.random.default_rng(8263).uniform(-0.04, 1.4, (12, 16, 4)).astype(np.float32)
    colors[0, :3, :3] = (0.0, 0.02, 0.18)
    return graph, colors, params


_WORKING_SPACES = ("linear-rec709", "rec709-g24", "acescct", "rec2100-pq", "rec2100-hlg")
_HDR_LUMINANCE = VideoSignalLuminance(203.0, 1200.0, 1.24)


def _working_case(base_grade_case: tuple, working: str) -> tuple[ColorGraphData, np.ndarray, dict[str, float], bool]:
    graph, colors, params = base_grade_case
    space = LINEAR_SCENE if working == "linear-rec709" else descriptor_for_space_preset(working)
    if space != LINEAR_SCENE:
        # Start from the same valid linear values for every signal.
        colors = BuiltinColorProvider().transform_signal(colors, LINEAR_SCENE, space, luminance=_HDR_LUMINANCE)
    return base_grade_graph_signal(graph, space, _HDR_LUMINANCE), colors, params, working == "rec709-g24"


def _expected(colors: np.ndarray, params: dict[str, float], working: str) -> np.ndarray:
    provider = BuiltinColorProvider()
    space = LINEAR_SCENE if working == "linear-rec709" else descriptor_for_space_preset(working)
    linear = provider.transform_signal(colors[..., :3], space, LINEAR_SCENE, luminance=_HDR_LUMINANCE)
    expected = colors.copy()
    graded = apply_base_grade_linear(linear, params)
    expected[..., :3] = provider.transform_signal(graded, LINEAR_SCENE, space, luminance=_HDR_LUMINANCE)
    return expected


def test_base_grade_layer_round_trip_and_reset(base_grade_case: tuple) -> None:
    _, _, params = base_grade_case
    layer = Layer("Base Grade")
    assert not layer.has_grading_effects()
    layer.grading_params.update(params)
    assert layer.has_grading_effects()
    restored = Layer.from_dict(layer.to_dict(None))
    assert all(restored.grading_params[key] == value for key, value in params.items())
    restored.reset_params()
    assert all(restored.grading_params[key] == value for key, value in BASE_GRADE_DEFAULTS.items())
    assert not restored.has_grading_effects()


@pytest.mark.parametrize("working", _WORKING_SPACES)
def test_graph_cpu_base_grade_matches_linear_operator(
    base_grade_case: tuple, working: str,
) -> None:
    from src.features.color.core.color_graph_cpu import run_graph_frame_cpu

    graph, colors, params, encoded = _working_case(base_grade_case, working)
    # Frame convenience entry point consumes BGR(A).
    bgr = colors[..., [2, 1, 0, 3]]
    result = run_graph_frame_cpu(graph, bgr, 0, rec709_calibration_encoded=encoded)
    np.testing.assert_allclose(result[..., [2, 1, 0, 3]], _expected(colors, params, working), atol=2e-6)


@pytest.mark.parametrize("working", _WORKING_SPACES)
def test_gl_graph_base_grade_matches_linear_operator(
    base_grade_case: tuple, working: str,
) -> None:
    moderngl = pytest.importorskip("moderngl")
    from src.features.color.core.color_graph_texture import ColorGraphTextureRenderer

    try:
        ctx = moderngl.create_context(standalone=True)
    except (RuntimeError, ValueError) as exc:
        pytest.skip(f"no standalone GL context: {exc}")
    graph, colors, params, encoded = _working_case(base_grade_case, working)
    source = ctx.texture((colors.shape[1], colors.shape[0]), 4, colors.tobytes(), dtype="f4")
    renderer = ColorGraphTextureRenderer(ctx)
    try:
        result = renderer.render(source, graph, 0, rec709_calibration_encoded=encoded)
        pixels = np.frombuffer(result.read(), dtype=np.float16 if result.dtype == "f2" else np.float32)
        np.testing.assert_allclose(pixels.reshape(colors.shape), _expected(colors, params, working), atol=8e-5, rtol=7e-4)
    finally:
        renderer.release()
        source.release()
        ctx.release()


@pytest.mark.parametrize("working", _WORKING_SPACES)
def test_metal_graph_base_grade_matches_linear_operator(
    base_grade_case: tuple, working: str,
) -> None:
    from src.features.color.core.color_graph_compiler import compile_graph
    from src.features.color.core.color_graph_metal import ColorGraphMetalExecutor
    from src.features.color.core.color_graph_param_source import graph_node_layer_provider
    from src.features.color.core.color_graph_snapshot import freeze_graph
    from src.features.color.core.composition_uniforms import build_composition_uniform_manifest
    from src.features.color.core.msl_composition import emit_composition_msl
    from src.shared.gpu.metal.context import MetalComputeContext

    supported, reason = MetalComputeContext.availability_reason()
    if not supported:
        pytest.skip(reason)
    graph, colors, params, encoded = _working_case(base_grade_case, working)
    size = (colors.shape[1], colors.shape[0])
    plan = compile_graph(freeze_graph(graph, session_id="base-grade", source_frame=0), size=size)
    context = MetalComputeContext()
    manifest = build_composition_uniform_manifest(has_base_grade=True)
    msl = emit_composition_msl(manifest, ("grading", "blend_modes"))
    library, error = context.device.newLibraryWithSource_options_error_(msl, None, None)
    assert library is not None, error
    executor = ColorGraphMetalExecutor(context)
    source = colors.astype(np.float16)
    provider = graph_node_layer_provider(graph, rec709_calibration_encoded=encoded)
    result = executor.execute_plan(plan, source, layer_param_provider=provider)
    assert result.published, result.diagnostic_message
    try:
        expected = _expected(source.astype(np.float32), params, working)
        np.testing.assert_allclose(result.output, expected, atol=8e-5, rtol=7e-4)
    finally:
        executor.release_revision(result.request_id)


@pytest.mark.parametrize("working", ["rec709-g24", "acescct"])
def test_edit_preview_and_delivery_read_the_same_saved_base_grade(
    base_grade_case: tuple, working: str,
) -> None:
    from src.features.edit.core.timeline_export import TimelineExporter
    from src.features.edit.core.timeline_preview_renderer import AssetResolver, PreviewRenderWorker

    _, colors, params, _ = _working_case(base_grade_case, working)
    stack = ColorStackData.create(asset_id=None, name="Base Grade").with_grading_params(params)
    settings = {TIMELINE_SPACE_KEY: working, OUTPUT_SPACE_KEY: "rec709-g24"}
    project = SimpleNamespace(color_stacks={stack.stack_id: stack}, color_management=settings)
    layer = SimpleNamespace(
        clip=SimpleNamespace(color_stack_id=stack.stack_id), clip_id="base-grade", source_frame=0)
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = project
    exporter._export_video_signal_spaces = video_signal_spaces(settings)
    preview = PreviewRenderWorker(
        frame_provider=MagicMock(), asset_resolver=AssetResolver(project), video_renderer=None)
    bgr = colors[..., [2, 1, 0, 3]]
    try:
        previewed = preview._apply_preview_color_grade_unstabilized(bgr, layer)
        delivered = exporter._apply_color_grade_unstabilized(bgr, layer)
        ref = _expected(colors, params, working)[..., [2, 1, 0, 3]]
        np.testing.assert_allclose(previewed, ref, atol=2e-6)
        np.testing.assert_array_equal(delivered, previewed)
    finally:
        preview._decode_executor.shutdown(wait=True)


@pytest.mark.parametrize(("kind", "path", "working"), [
    ("video", "test.mov", "rec709-bt709"),
    ("video", "test.mov", "rec709-g24"),
    ("image", "test.png", "srgb"),
    ("image", "test.exr", "linear-rec709"),
])
def test_legacy_timeline_grade_decodes_the_established_signal(
    base_grade_case: tuple, kind: str, path: str, working: str,
) -> None:
    from src.features.edit.core.timeline_export import TimelineExporter
    from src.features.edit.core.timeline_preview_renderer import AssetResolver, PreviewRenderWorker

    _, colors, params, _ = _working_case(base_grade_case, working)
    stack = ColorStackData.create(asset_id=None, name="Base Grade").with_grading_params(params)
    asset = SimpleNamespace(media_type=kind, path=path)
    settings = {"color.preview.timeline_transfer": "gamma24"} if working == "rec709-g24" else {}
    project = SimpleNamespace(color_stacks={stack.stack_id: stack}, color_management=settings,
                              get_asset_by_id=lambda _: asset)
    layer = SimpleNamespace(clip=SimpleNamespace(color_stack_id=stack.stack_id),
                            clip_id="base-grade", asset_id="asset", source_frame=0)
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = project
    preview = PreviewRenderWorker(
        frame_provider=MagicMock(), asset_resolver=AssetResolver(project), video_renderer=None)
    bgr = colors[..., [2, 1, 0, 3]]
    try:
        expected = _expected(colors, params, working)[..., [2, 1, 0, 3]]
        np.testing.assert_allclose(preview._apply_preview_color_grade_unstabilized(bgr, layer), expected, atol=2e-6)
        np.testing.assert_allclose(exporter._apply_color_grade_unstabilized(bgr, layer), expected, atol=2e-6)
    finally:
        preview._decode_executor.shutdown(wait=True)


@pytest.mark.parametrize(("path", "video", "transfer"), [
    ("test.png", False, "iec61966_2_1"),
    ("test.exr", False, None),
    ("test.mov", True, "bt709"),
])
def test_color_source_signal_tracks_legacy_import_format(path: str, video: bool, transfer: str | None) -> None:
    from src.features.color.core.image_engine import ImageEngine

    engine = ImageEngine.__new__(ImageEngine)
    engine.__dict__.update(current_file_path=path, _is_video_mode=video, is_current_image_srgb=True)
    signal = engine._base_grade_working_signal()
    assert (signal["space"]["transfer"] if signal else None) == transfer
