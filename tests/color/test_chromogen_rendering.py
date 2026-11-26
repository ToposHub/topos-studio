"""Exercise the native Chromogen render binding with real GL and Metal."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.color.core.base_grade_runtime import base_grade_graph_signal
from src.features.color.core.chromogen import (
    CHROMOGEN_KEY, STAGE_SPECS, apply_chromogen_linear, new_chromogen_stage,
)
from src.features.color.core.color_graph_migration import stack_primary_execution_graph
from src.features.color.core.layer import Layer
from src.shared.color.color_space_descriptor import (
    LINEAR_SCENE, OUTPUT_SPACE_KEY, TIMELINE_SPACE_KEY, descriptor_for_space_preset,
)
from src.shared.color.color_transform_provider import BuiltinColorProvider
from src.shared.color.project_video_signal import video_signal_spaces
from src.shared.project.color_graph import ColorGraphData
from src.shared.project.color_stack import ColorStackData


def _look() -> dict[str, Any]:
    stages = []
    for kind in STAGE_SPECS:
        stage = new_chromogen_stage(kind)
        for control in STAGE_SPECS[kind].controls:
            stage["params"][control.key] = control.default + (control.maximum - control.default) * 0.27
        stages.append(stage)
    return {"version": 1, "enabled": True, "amount": 0.65, "stages": stages}


def _case(working: str) -> tuple[ColorGraphData, np.ndarray, dict[str, Any]]:
    look = _look()
    stack = ColorStackData.create(asset_id=None, name="Chromogen").with_grading_params({CHROMOGEN_KEY: look})
    graph = stack_primary_execution_graph(stack)
    assert graph is not None
    space = LINEAR_SCENE if working == "linear" else descriptor_for_space_preset(working)
    linear = np.random.default_rng(9312).uniform(-0.04, 1.6, (16, 24, 4)).astype(np.float32)
    linear[..., 3] = np.linspace(0.1, 1.0, 24)
    if not np.any(np.abs(apply_chromogen_linear(linear[..., :3], look) - linear[..., :3]) > 1.0e-4):
        pytest.fail("The test look must visibly change the chart")
    colors = BuiltinColorProvider().transform_signal(linear, LINEAR_SCENE, space)
    return base_grade_graph_signal(graph, space), colors, look


def _expected(colors: np.ndarray, look: dict[str, Any], working: str) -> np.ndarray:
    provider = BuiltinColorProvider()
    space = LINEAR_SCENE if working == "linear" else descriptor_for_space_preset(working)
    rgb = provider.transform_signal(colors[..., :3], space, LINEAR_SCENE)
    result = colors.copy()
    result[..., :3] = provider.transform_signal(apply_chromogen_linear(rgb, look), LINEAR_SCENE, space)
    return result


def test_layer_round_trips_ordered_chromogen_stack() -> None:
    look = _look()
    layer = Layer("Chromogen")
    layer.grading_params[CHROMOGEN_KEY] = look
    assert layer.has_grading_effects()
    restored = Layer.from_dict(json.loads(json.dumps(layer.to_dict(None))))
    assert restored.grading_params[CHROMOGEN_KEY] == look
    restored.reset_params()
    assert restored.grading_params[CHROMOGEN_KEY] is None


def test_newer_chromogen_state_is_preserved_and_refused_for_lut_export() -> None:
    from src.features.color.core.graph_lut_export import bake_graph_lut

    look = {**_look(), "version": 99}
    stack = ColorStackData.create(asset_id=None, name="Chromogen").with_grading_params({CHROMOGEN_KEY: look})
    graph = stack_primary_execution_graph(stack)
    assert graph is not None
    with pytest.raises(ValueError, match="Unsupported Look Dev version"):
        bake_graph_lut(graph, 5)
    assert stack.grading_params[CHROMOGEN_KEY] == look


def test_graph_and_flat_lut_match_encoded_chromogen_at_cube_vertices() -> None:
    from src.features.color.core.graph_lut_export import bake_graph_lut
    from src.features.color.core.lut_exporter import LutExportWorker
    from src.shared.color.lut_loader import parse_cube, sample_lut_trilinear

    graph, _, look = _case("rec709-g24")
    axis = np.linspace(0.0, 1.0, 9, dtype=np.float32)
    points = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1).reshape(-1, 3)
    expected = np.clip(_expected(points, look, "rec709-g24"), 0.0, 1.0)
    cube = bake_graph_lut(graph, 9, rec709_calibration_encoded=True)
    np.testing.assert_allclose(sample_lut_trilinear(parse_cube(cube), points), expected, atol=4.0e-6)
    flat = LutExportWorker()._apply_grading_cpu(points, {
        "grading_params": {CHROMOGEN_KEY: look}, "base_grade_encoded_input": True,
    })
    np.testing.assert_allclose(np.clip(flat, 0.0, 1.0), expected, atol=4.0e-6)


@pytest.mark.parametrize("slot", ["color_stack_id", "color_trim_stack_id"])
def test_edit_and_deliver_evaluate_saved_timeline_look(slot: str) -> None:
    from src.features.edit.core.timeline_export import TimelineExporter
    from src.features.edit.core.timeline_preview_renderer import AssetResolver, PreviewRenderWorker

    _, colors, look = _case("rec709-g24")
    stack = ColorStackData.create(asset_id=None, name="Chromogen").with_grading_params({CHROMOGEN_KEY: look})
    settings = {TIMELINE_SPACE_KEY: "rec709-g24", OUTPUT_SPACE_KEY: "rec709-g24"}
    project = SimpleNamespace(color_stacks={stack.stack_id: stack}, color_management=settings)
    clip = SimpleNamespace(clip_id="chromogen-clip", **{slot: stack.stack_id})
    layer = SimpleNamespace(clip=clip, clip_id=clip.clip_id, source_frame=0)
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = project
    exporter._export_video_signal_spaces = video_signal_spaces(settings)
    preview = PreviewRenderWorker(
        frame_provider=MagicMock(), asset_resolver=AssetResolver(project), video_renderer=None)
    bgr = colors[..., [2, 1, 0, 3]]
    try:
        expected = _expected(colors, look, "rec709-g24")[..., [2, 1, 0, 3]]
        previewed = preview._apply_preview_color_grade_unstabilized(bgr, layer)
        delivered = exporter._apply_color_grade_unstabilized(bgr, layer)
        np.testing.assert_allclose(previewed, expected, atol=3.0e-6)
        np.testing.assert_array_equal(delivered, previewed)
    finally:
        preview._decode_executor.shutdown(wait=True)


@pytest.mark.parametrize("working", ["linear", "rec709-g24", "acescct"])
def test_chromogen_graph_cpu_and_gl_use_same_ordered_grade(working: str) -> None:
    from src.features.color.core.color_graph_cpu import run_graph_frame_cpu
    from src.features.color.core.color_graph_texture import ColorGraphTextureRenderer

    graph, colors, look = _case(working)
    expected = _expected(colors, look, working)
    encoded = working == "rec709-g24"
    bgr = colors[..., [2, 1, 0, 3]]
    cpu = run_graph_frame_cpu(graph, bgr, 0, rec709_calibration_encoded=encoded)
    np.testing.assert_allclose(cpu[..., [2, 1, 0, 3]], expected, atol=3.0e-6)

    moderngl = pytest.importorskip("moderngl")
    try:
        ctx = moderngl.create_standalone_context()
    except (RuntimeError, ValueError) as exc:
        pytest.skip(f"no standalone GL context: {exc}")
    source = ctx.texture((colors.shape[1], colors.shape[0]), 4, colors.tobytes(), dtype="f4")
    renderer = ColorGraphTextureRenderer(ctx)
    try:
        result = renderer.render(source, graph, 0, rec709_calibration_encoded=encoded)
        dtype = np.float16 if result.dtype == "f2" else np.float32
        rendered = np.frombuffer(result.read(), dtype=dtype).reshape(colors.shape)
        np.testing.assert_allclose(rendered, expected, atol=1.2e-4, rtol=8.0e-4)
    finally:
        renderer.release()
        source.release()
        ctx.release()


@pytest.mark.parametrize("working", ["linear", "rec709-g24", "acescct"])
def test_chromogen_metal_compute_matches_cpu(working: str) -> None:
    from src.features.color.core.color_graph_compiler import compile_graph
    from src.features.color.core.color_graph_metal import ColorGraphMetalExecutor
    from src.features.color.core.color_graph_param_source import graph_node_layer_provider
    from src.features.color.core.color_graph_snapshot import freeze_graph
    from src.features.color.core.color_graph_texture_metal import ColorGraphMetalTextureExecutor
    from src.features.color.core.composition_uniforms import build_composition_uniform_manifest
    from src.features.color.core.msl_composition import emit_composition_msl
    from src.shared.gpu.metal.context import MetalComputeContext

    supported, reason = MetalComputeContext.availability_reason()
    if not supported:
        pytest.skip(reason)
    import Metal

    graph, colors, look = _case(working)
    encoded = working == "rec709-g24"
    size = (colors.shape[1], colors.shape[0])
    plan = compile_graph(freeze_graph(graph, session_id="chromogen"), size=size)
    context = MetalComputeContext()
    manifest = build_composition_uniform_manifest(has_chromogen=True, has_base_grade=True)
    library, error = context.device.newLibraryWithSource_options_error_(
        emit_composition_msl(manifest, ("grading", "blend_modes")), None, None)
    assert library is not None, error
    executor = ColorGraphMetalExecutor(context)
    source = colors.astype(np.float16)
    provider = graph_node_layer_provider(graph, rec709_calibration_encoded=encoded)
    result = executor.execute_plan(plan, source, layer_param_provider=provider)
    assert result.published, result.diagnostic_message
    try:
        expected = _expected(source.astype(np.float32), look, working)
        np.testing.assert_allclose(result.output, expected, atol=1.2e-4, rtol=8.0e-4)
    finally:
        executor.release_revision(result.request_id)

    texture_executor = ColorGraphMetalTextureExecutor(context)
    texture = context.create_texture_from_numpy(source, Metal.MTLPixelFormatRGBA16Float, *size)
    textured = texture_executor.execute_plan(plan, texture, layer_param_provider=provider)
    assert textured.published, textured.diagnostic_message
    try:
        pixels = texture_executor.read_output_rgba16f(textured)
        # Some Metal devices round a texture write down by one FP16 unit.
        np.testing.assert_allclose(pixels, expected, atol=1.2e-4, rtol=1.1e-3)
    finally:
        texture_executor.release_revision(textured.request_id)


def test_graph_grade_patch_persists_stages_and_rejects_bad_version() -> None:
    from src.features.color.core.color_graph_migration import migrate_legacy_stack, project_legacy_layers
    from src.features.color.core.color_graph_service import ColorGraphService

    layer = Layer("Corrector")
    stack = ColorStackData(stack_id="look", layers=(layer.to_dict(None),))
    graph = migrate_legacy_stack(stack)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    service = ColorGraphService()
    look = _look()
    result = service.op_set_node_option(graph, node.node_id, "grade_patch", (CHROMOGEN_KEY, look))
    assert result.ok
    restored = ColorGraphData.from_dict(json.loads(json.dumps(result.graph.to_dict())))
    assert project_legacy_layers(restored)[0]["grading_params"][CHROMOGEN_KEY] == look
    invalid = service.op_set_node_option(restored, node.node_id, "grade_patch", (
        CHROMOGEN_KEY, {**look, "version": 999},
    ))
    assert not invalid.ok
    assert project_legacy_layers(restored)[0]["grading_params"][CHROMOGEN_KEY] == look


def test_picker_samples_node_color_override_before_chromogen() -> None:
    from src.features.color.core.async_renderer import AsyncRenderer
    from src.features.color.core.color_node_color_space import apply_node_color_space_cpu, node_color_transform_spec

    history = []
    renderer = SimpleNamespace(warper_input_ready=SimpleNamespace(emit=history.append))
    rgb = np.array([[[0.4, 0.6, 0.2]]], np.float32)
    rect = (0, 0, 500, 500)
    request = ("node", ("source",), (1,), "chromogen")
    context = {
        "warper_sample": request,
        "scene_visible_rect": rect,
        "rec709_calibration_encoded": True,
    }
    layer = {"grading_params": {CHROMOGEN_KEY: _look()},
             "_node_input_color": ("P3-D65", "Gamma 2.2")}
    AsyncRenderer._publish_warper_sample(renderer, context, rgb, layer)
    assert history[0][:3] == (request, rect, False)
    expected = apply_node_color_space_cpu(rgb, node_color_transform_spec("P3-D65", "Gamma 2.2"))
    np.testing.assert_allclose(history[0][3], expected, atol=1e-6)


def test_extreme_repeated_sector_stages_keep_finite_extended_values() -> None:
    from src.features.color.core.chromogen import CHROMOGEN_MAX_STAGES

    stages = [new_chromogen_stage("sector_brightness") for _ in range(CHROMOGEN_MAX_STAGES)]
    for stage in stages:
        stage["params"].update(hue=30.0, brightness=4.0, falloff=0.0)
    rgb = np.array([[16.0, 3.0, -0.04], [0.0, 0.0, 0.0], [-0.09, 0.1, 2.5]], np.float32)
    look = {"version": 1, "enabled": True, "amount": 1.0, "stages": stages}
    result = apply_chromogen_linear(rgb, look)
    assert np.isfinite(result).all()
    assert result[0, 0] > rgb[0, 0]
    np.testing.assert_allclose(result[1], rgb[1], atol=1e-7)


def test_encoded_chromogen_and_warper_share_one_graph_lut() -> None:
    from src.features.color.core.graph_lut_export import bake_graph_lut
    from src.features.color.core.warper_color import (
        RESOLVE_HS709_SPACE, apply_resolve_hs709_texture, prepare_resolve_hs709_texture,
    )
    from src.shared.color.lut_loader import parse_cube

    stage = new_chromogen_stage("colour_saturation")
    stage["params"].update(rg=1.14, yb=0.86)
    look = {"version": 1, "enabled": True, "amount": 1.0, "stages": [stage]}
    layer = Layer("Chromogen and Warper")
    layer.grading_params[CHROMOGEN_KEY] = look
    layer.warper_params = np.full((6, 8, 3), 0.025, np.float32)
    layer.warper_space = RESOLVE_HS709_SPACE
    stack = ColorStackData(stack_id="combined-look", layers=(layer.to_dict(None),))
    graph = stack_primary_execution_graph(stack)
    assert graph is not None
    chart = np.array([[0.625, 0.375, 0.25]], dtype=np.float32)
    graded = _expected(chart, look, "rec709-g24")
    response = prepare_resolve_hs709_texture(layer.warper_params, layer.warper_space)
    expected = apply_resolve_hs709_texture(graded, response, layer.warper_space, encoded_input=True)
    lut = parse_cube(bake_graph_lut(graph, 9, rec709_calibration_encoded=True)).table
    np.testing.assert_allclose(lut[2, 3, 5], expected[0], atol=5e-6)
