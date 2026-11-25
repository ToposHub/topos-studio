# -*- coding: utf-8 -*-
"""CW-B08:warper 多空间渲染集成测试(管线编译/manifest/值/持久化/投影)。

GPU 部分在真实 GL 上下文编译生产 ShaderPipeline(无 GL 环境 skip);
数值 parity 见 test_warper_color_parity.py。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pytest

from src.features.color.core.composition_uniforms import (
    build_composition_uniform_manifest,
    composition_uniform_values,
)
from src.features.color.core.layer import Layer
from src.features.color.core.warper_state import (
    WARPER_SCHEMA_VERSION,
    CURRENT_ALGORITHM,
    WarperGrid,
    WarperPinPoint,
    WarperStateV2,
    WarperStroke,
    XGradeState,
    describe_warper_capabilities,
    legacy_from_ndarray,
    renderable_projection,
)


def _hsp_state() -> WarperStateV2:
    grid = WarperGrid.identity("hs", 6, 8, ("dh", "ds"))
    return WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="hue_saturation", color_space="hsp", axis_angle=0.0, grids={"hs": grid})


# ---- 投影与能力 ----

def test_renderable_projection_spaces_and_limits() -> None:
    offsets, space = renderable_projection(_hsp_state())
    assert offsets is not None and offsets.shape == (6, 8, 2)
    assert space == 2
    _, hsl_space = renderable_projection(
        WarperStateV2(schema_version=WARPER_SCHEMA_VERSION,
                      algorithm_version=CURRENT_ALGORITHM, mode="hue_saturation",
                      color_space="hsl", axis_angle=0.0,
                      grids={"hs": WarperGrid.identity("hs", 6, 6, ("dh", "ds"))}))
    assert hsl_space == 1
    # 三分量 / CL / CW / None:无 GPU 后端 → 空
    luma_state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="hue_saturation", color_space="hsv", axis_angle=0.0,
        grids={"hs": WarperGrid.identity("hs", 6, 6, ("dh", "ds", "dl"))})
    assert renderable_projection(luma_state) == (None, 0)
    assert renderable_projection(None) == (None, 0)
    caps = describe_warper_capabilities(_hsp_state())
    assert caps.enabled and caps.backend == "warper_v2_glsl"


# ---- manifest / values ----

def test_manifest_declares_space_uniform_only_for_v2() -> None:
    def _lines(manifest) -> list[str]:
        return manifest.declarations("header") + manifest.declarations("body")

    legacy = build_composition_uniform_manifest(has_warper=True)
    assert not any("u_warper_space" in line for line in _lines(legacy))
    v2 = build_composition_uniform_manifest(has_warper=True, warper_space=2)
    space_lines = [line for line in _lines(v2) if "u_warper_space" in line]
    assert len(space_lines) == 1 and "int" in space_lines[0]
    # 关闭 warper 时不声明
    off = build_composition_uniform_manifest(has_warper=False, warper_space=2)
    assert not any("u_warper_space" in line for line in _lines(off))


def test_uniform_values_carry_space_from_dto_or_layer() -> None:
    dto = {"warper_params": np.zeros((6, 6, 2), np.float32), "warper_space": 2,
           "grading_params": {}, "opacity": 1.0, "blend_mode": "Normal"}
    values = composition_uniform_values(dto, width=64, height=64)
    assert values["u_warper_space"] == 2
    legacy_dto = dict(dto, warper_space=0)
    assert composition_uniform_values(legacy_dto, width=64, height=64)["u_warper_space"] == 0
    # 缺省键 → legacy 0
    del legacy_dto["warper_space"]
    assert composition_uniform_values(legacy_dto, width=64, height=64)["u_warper_space"] == 0


# ---- 层持久化 ----

def test_layer_round_trips_warper_space() -> None:
    layer = Layer("s")
    layer.warper_params = np.zeros((6, 6, 2), np.float32)
    layer.warper_space = 2
    data = layer.to_dict(None)
    assert data.get("warper_space") == 2
    restored = Layer.from_dict(data)
    assert restored.warper_space == 2
    # legacy(0)不写键:旧工程字节不变
    plain = Layer("p")
    plain.warper_params = np.zeros((6, 6, 2), np.float32)
    assert "warper_space" not in plain.to_dict(None)
    # 复制与粘贴
    target = Layer("t")
    target.copy_params_from(layer)
    assert target.warper_space == 2
    target2 = Layer("t2")
    target2.apply_grade_patch({"warper_params": plain.to_dict(None)["warper_params"],
                               "warper_space": 1}, categories=("warper",))
    assert target2.warper_space == 1


# ---- 真实管线编译(GL) ----

def _pipeline():
    moderngl = pytest.importorskip("moderngl")
    from src.features.color.core.shader_compiler import ShaderPipeline
    try:
        ctx = moderngl.create_context(standalone=True)
    except Exception as exc:
        pytest.skip(f"no standalone GL context: {exc}")
    return ShaderPipeline(ctx)


def test_composition_shader_compiles_v2_variant_and_legacy_unchanged() -> None:
    pipeline = _pipeline()
    # v2 变体:编译成功且源码走 apply_warper_v2
    program = pipeline.build_composition_shader(has_warper=True, warper_space=2)
    assert program is not None
    assert "u_warper_space" in program
    # legacy 变体:源码不变(无 v2 调用),缓存互不污染
    legacy = pipeline.build_composition_shader(has_warper=True)
    assert legacy is not None and "u_warper_space" not in legacy
    again = pipeline.build_composition_shader(has_warper=True, warper_space=1)
    assert again is not None and "u_warper_space" in again


def test_v2_variant_renders_fixture_grid_consistently_with_cpu() -> None:
    """端到端:生产管线(space=2)+ RG32F 纹理,与 CPU 参考逐点比对。"""
    from src.features.color.core.warper_color import apply_warper_in_space

    pipeline = _pipeline()
    moderngl = pytest.importorskip("moderngl")
    ctx = pipeline.ctx
    program = pipeline.build_composition_shader(has_warper=True, warper_space=2)
    assert program is not None

    grid = np.random.default_rng(20261112).normal(0, 0.15, (8, 8, 2)).astype(np.float32)
    grid[..., 0] = np.clip(grid[..., 0], -0.3, 0.3)
    grid[..., 1] = np.clip(grid[..., 1], -0.4, 0.4)
    colors = np.array([(1.0, 0.0, 0.0), (0.2, 0.4, 0.1), (0.5, 0.5, 0.5),
                       (0.0, 1.0, 1.0), (2.0, 1.5, 0.5), (1.0, 1.0, 0.0)], np.float32)

    warper_tex = ctx.texture((8, 8), 2, np.ascontiguousarray(grid).tobytes(), dtype="f4")
    warper_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    warper_tex.repeat_x = warper_tex.repeat_y = False
    input_tex = ctx.texture((len(colors), 1), 4,
                            np.ascontiguousarray(np.concatenate(
                                [colors, np.ones((len(colors), 1))], axis=1,
                                dtype=np.float32)).tobytes(),
                            dtype="f4")
    input_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
    # 按生产方式上传全套中性 uniform(grading/curves/... 缺省会以 0 值
    # 改坏颜色,必须显式给中性默认)
    from src.features.color.core.composition_uniforms import (
        composition_uniform_values as _values,
        upload_composition_uniforms as _upload,
    )
    neutral_dto = {"warper_params": np.zeros((8, 8, 2), np.float32),
                   "warper_space": 2, "grading_params": {}, "opacity": 1.0,
                   "blend_mode": "Normal", "effects_params": {"enabled": False}}
    _upload(program, _values(neutral_dto, width=len(colors), height=1))

    fbo = ctx.simple_framebuffer((len(colors), 1), components=4, dtype="f4")
    fbo.use()
    # 组合管线顶点布局:in_vert + in_texcoord(全屏条带,texcoord 未被
    # warper 路径使用,但属性必须存在)
    strip = np.array([-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1], np.float32)
    quad = ctx.buffer(strip.tobytes())
    vao = ctx.vertex_array(program, [(quad, "2f 2f", "in_vert", "in_texcoord")])
    # 白色 matte:composition main 无条件采样 u_matte_tex 做遮罩混合,
    # 不绑定会回落到 unit 0(input)导致 mask=input.r 把结果混回原色
    matte_tex = ctx.texture((1, 1), 1, np.array([1.0], np.float32).tobytes(), dtype="f4")
    matte_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
    matte_tex.repeat_x = matte_tex.repeat_y = False
    input_tex.use(0)
    program["u_input_tex"].value = 0
    warper_tex.use(1)
    program["u_warper_tex"].value = 1
    matte_tex.use(2)
    program["u_matte_tex"].value = 2
    program["u_warper_space"].value = 2
    vao.render(moderngl.TRIANGLE_STRIP)
    result = np.frombuffer(fbo.read(components=4, dtype="f4"), np.float32).reshape(1, -1, 4)

    for index, color in enumerate(colors):
        cpu = apply_warper_in_space(color, grid, "hsp")
        np.testing.assert_allclose(result[0, index, :3], cpu, rtol=1e-2, atol=3e-3,
                                   err_msg=f"pipeline hsp parity {color}")
    warper_tex.release(); input_tex.release(); matte_tex.release(); fbo.release()
    quad.release(); vao.release()


@pytest.fixture(params=[3, 4, 5, "chroma_luma", "chroma_warp", "x_grade", "combined_x_grade"],
                ids=["709-v1", "709-v2", "709-v3", "chroma-luma", "chroma-warp",
                     "x-grade", "combined-x-grade"])
def calibrated_graph_case(request: pytest.FixtureRequest) -> tuple:
    from src.shared.project.color_graph import ColorEdgeData, ColorGraphData, ColorNodeData, NodeType

    colors = np.array([[
        (0.75, 0.18, 0.14, 1.0), (0.75, 0.14, 0.18, 0.5), (0.11, 0.67, 0.62, 1.0),
        (0.23, 0.24, 0.80, 0.8), (0.3, 0.3, 0.3, 0.6), (-0.1, 1.5, 0.45, 1.0),
    ]], np.float32)
    offsets = np.zeros((6, 8, 3), np.float32)
    offsets[1:5, (0, 4, 7), :] = (0.06, -0.08, 0.09)
    space = request.param
    if space in ("x_grade", "combined_x_grade"):
        from src.features.color.core.warper_state import RESOLVE_HS_ALGORITHM_V3

        source = tuple(colors[0, 0, :3] ** (1.0 / 2.4))
        target = tuple(np.array((0.72, 0.26, 0.13)) ** (1.0 / 2.4))
        stroke = WarperStroke("red", "normal", source, target,
                              {"chroma_range": 0.06, "exposure": 0.8, "x_zone": "light"})
        base = _hsp_state()
        if space == "combined_x_grade":
            grid = WarperGrid.identity("hs", 6, 8, ("dh", "ds", "dl")).with_offsets(offsets)
            base = replace(base, algorithm_version=RESOLVE_HS_ALGORITHM_V3, grids={"hs": grid})
        state = replace(base, x_grade=XGradeState(strokes=(stroke,)))
        offsets, space = renderable_projection(state)
    elif isinstance(space, str):
        from src.features.color.core.warper_state import CHROMA709_ALGORITHM

        if space == "chroma_luma":
            grid = WarperGrid.identity("grid1", 7, 7, ("dc", "dl"))
            change = grid.mutable_offsets()
            change[2, 3] = (0.10, -0.1)
            grids = {"grid1": grid.with_offsets(change), "grid2": WarperGrid.identity("grid2", 7, 7, ("dc", "dl"))}
            strokes = ()
        else:
            grids = {}
            strokes = (WarperStroke("test", "normal", (1.0, 0.0, 0.0), (0.9, 0.1, 0.2),
                                    {"chroma_range": 0.18}, ((0.29, 0.18), (0.35, 0.27))),)
        state = WarperStateV2(WARPER_SCHEMA_VERSION, CHROMA709_ALGORITHM, space, "hsp", 0.0, grids, strokes)
        offsets, space = renderable_projection(state)
    payload = {
        "grading_params": {}, "warper_params": offsets.tolist(),
        "warper_space": space, "opacity": 0.7,
    }
    nodes = (
        ColorNodeData(node_id="source", node_type=NodeType.SOURCE.value),
        ColorNodeData(node_id="grade", node_type=NodeType.CORRECTOR.value, legacy_payload=payload),
        ColorNodeData(node_id="output", node_type=NodeType.OUTPUT.value),
    )
    edges = (
        ColorEdgeData(edge_id="e1", src_node="source", src_port="image",
                      dst_node="grade", dst_port="image"),
        ColorEdgeData(edge_id="e2", src_node="grade", src_port="image",
                      dst_node="output", dst_port="image"),
    )
    graph = ColorGraphData(nodes=nodes, edges=edges, source_node_id="source", output_node_id="output")
    return graph, payload, colors


def test_calibrated_warper_graph_texture_matches_cpu(calibrated_graph_case: tuple) -> None:
    from src.features.color.core.color_graph_cpu import corrector_production_oracle
    from src.features.color.core.color_graph_texture import ColorGraphTextureRenderer

    graph, payload, colors = calibrated_graph_case
    ctx = _pipeline().ctx
    source = ctx.texture((colors.shape[1], 1), 4, colors.tobytes(), dtype="f4")
    renderer = ColorGraphTextureRenderer(ctx)
    try:
        output = renderer.render(source, graph, 0)
        got = np.frombuffer(output.read(), dtype=np.float32).reshape(colors.shape)
        expected = corrector_production_oracle(colors, payload)
        assert np.max(np.abs(expected[..., :3] - colors[..., :3])) > 0.01
        np.testing.assert_allclose(got, expected, atol=2e-3, rtol=1e-2)
    finally:
        renderer.release()
        source.release()


def test_same_size_warper_texture_reallocates_only_when_channels_change() -> None:
    from types import SimpleNamespace

    from src.features.color.core.async_renderer import AsyncRenderer
    from src.features.color.core.warper_color import (
        RESOLVE_HS709_SPACE, prepare_resolve_hs709_texture,
    )

    ctx = _pipeline().ctx
    renderer = SimpleNamespace(ctx=ctx, layer_textures={})
    payload = {"layer_id": "layer", "size": (8, 6), "type": "warper"}
    legacy = np.zeros((6, 8, 2), np.float32)
    calibrated = np.full((6, 8, 3), 0.125, np.float32)

    def update(grid: np.ndarray, space: int) -> Any:
        AsyncRenderer._do_update_aux_tex(renderer, {**payload, "data": grid.tobytes(), "space": space})
        return renderer.layer_textures["layer"]["warper"]

    legacy_tex = update(legacy, 0)
    calibrated_tex = update(calibrated, RESOLVE_HS709_SPACE)
    try:
        assert calibrated_tex is not legacy_tex
        assert calibrated_tex.components == 3
        assert update(calibrated, RESOLVE_HS709_SPACE) is calibrated_tex
        assert update(calibrated * 2, RESOLVE_HS709_SPACE) is calibrated_tex
        assert calibrated_tex.read() == prepare_resolve_hs709_texture(calibrated * 2).tobytes()
    finally:
        calibrated_tex.release()


def test_calibrated_warper_has_no_grid_boundary_steps() -> None:
    from src.features.color.core.warper_color import apply_resolve_hs709, hsv_to_rgb

    offsets = np.zeros((6, 6, 3), np.float32)
    offsets[2, 1] = (0.10, 0.0, 0.0)
    offsets[-1, 0] = (0.0, 0.08, 0.0)
    hue_seam = np.stack([hsv_to_rgb((h, 0.55, 0.7)) for h in (1.0 - 1e-5, 1e-5)])
    # The calibrated radial domain crosses a ring near radius 0.5.
    ring_saturation = 0.5 * 0.7 ** 0.074 / 0.876
    ring_boundary = np.stack([
        hsv_to_rgb((0.0, ring_saturation + d, 0.7)) for d in (-1e-5, 1e-5)
    ])
    work = np.stack((hue_seam, ring_boundary))
    warped = apply_resolve_hs709(work ** (2.4 / 1.605), offsets)
    np.testing.assert_allclose(warped[:, 0], warped[:, 1], atol=2e-4, rtol=0)


def test_calibrated_warper_metal_compute_matches_cpu(calibrated_graph_case: tuple) -> None:
    from src.features.color.core.color_graph_compiler import compile_graph
    from src.features.color.core.color_graph_cpu import corrector_production_oracle
    from src.features.color.core.color_graph_metal import ColorGraphMetalExecutor
    from src.features.color.core.color_graph_param_source import graph_node_layer_provider
    from src.features.color.core.color_graph_snapshot import freeze_graph
    from src.features.color.core.msl_composition import emit_composition_msl
    from src.shared.gpu.metal.context import MetalComputeContext

    supported, reason = MetalComputeContext.availability_reason()
    if not supported:
        pytest.skip(reason)
    graph, payload, colors = calibrated_graph_case
    size = (colors.shape[1], 1)
    plan = compile_graph(freeze_graph(graph, session_id="warper-test", source_frame=0), size=size)
    ctx = MetalComputeContext()
    manifest = build_composition_uniform_manifest(
        has_warper=True, warper_space=payload["warper_space"],
    )
    msl = emit_composition_msl(manifest, ("grading", "warper", "blend_modes"))
    library, error = ctx.device.newLibraryWithSource_options_error_(msl, None, None)
    assert library is not None, error
    executor = ColorGraphMetalExecutor(ctx)
    source = colors.astype(np.float16)
    result = executor.execute_plan(plan, source, layer_param_provider=graph_node_layer_provider(graph))
    assert result.published, result.diagnostic_message
    try:
        expected = corrector_production_oracle(source.astype(np.float32), payload)
        np.testing.assert_allclose(result.output, expected, atol=2e-3, rtol=2e-3)
    finally:
        executor.release_revision(result.request_id)


def test_x_grade_controls_round_trip_and_protect_neutral_and_pins() -> None:
    from src.features.color.core.warper_chroma709 import chroma_warp_position
    from src.features.color.core.warper_color import apply_resolve_hs709

    color = np.array((0.70, 0.18, 0.10), np.float32)
    red = tuple(chroma_warp_position(color ** (1.0 / 2.4)))
    stroke = WarperStroke("red", "normal", tuple(color), tuple(color),
                          {"exposure": 1.0, "chroma_range": 0.10}, (red, red))
    base = _hsp_state()
    authored = replace(base, x_grade=XGradeState(strokes=(stroke,)))
    layer = Layer("x-grade")
    layer.warper_state = authored
    restored = Layer.from_dict(layer.to_dict(None))
    assert restored.warper_state.x_grade == authored.copy().x_grade
    assert restored.warper_params is not None
    colors = np.stack((color, color * 8.0, (0.40, 0.40, 0.40), (0.08, 0.18, 0.72))).astype(np.float32)
    result = apply_resolve_hs709(colors, restored.warper_params, restored.warper_space)
    np.testing.assert_allclose(result[:2], colors[:2] * 2.0, rtol=0.05)
    assert result[2, 0] > colors[2, 0]
    np.testing.assert_array_equal(result[2], np.full(3, result[2, 0], dtype=np.float32))

    pinned = XGradeState(strokes=(stroke,), pin_points=(WarperPinPoint("skin", red),))
    records, space = renderable_projection(replace(base, x_grade=pinned))
    np.testing.assert_array_equal(apply_resolve_hs709(colors, records, space)[:2], colors[:2])


def test_x_grade_small_exposure_stays_closer_to_the_selected_color() -> None:
    from src.features.color.core.warper_chroma709 import chroma_warp_position
    from src.features.color.core.warper_color import apply_resolve_hs709

    green = (0.22, 0.55, 0.18)
    point = tuple(chroma_warp_position(np.asarray(green)))
    rgb = np.asarray((green, (0.30, 0.55, 0.24)), dtype=np.float32) ** 2.4

    def response(stops: float) -> np.ndarray:
        stroke = WarperStroke("green", "normal", green, green, {"chroma_range": 0.04, "exposure": stops},
                              (point, point))
        state = replace(_hsp_state(), x_grade=XGradeState(strokes=(stroke,)))
        offsets, space = renderable_projection(state)
        return np.log2(apply_resolve_hs709(rgb, offsets, space) / rgb) / stops

    small, full = response(0.65), response(1.0)
    np.testing.assert_allclose(small[0], full[0], atol=0.01)
    assert np.all(small[1] < full[1])


def test_x_grade_outward_stroke_rolls_off_near_the_gamut_boundary() -> None:
    from src.features.color.core.warper_chroma709 import chroma_warp_color, chroma_warp_position
    from src.features.color.core.warper_color import apply_resolve_hs709

    green = np.array((0.22, 0.55, 0.18))
    start = chroma_warp_position(green)
    end = start + (-0.007, 0.045)
    neutral = chroma_warp_position(np.ones(3))
    neighbor = chroma_warp_color((start + neutral) * 0.5) * 0.5
    encoded = np.stack((green, neighbor, (0.55, 0.55, 0.55))).astype(np.float32)
    stroke = WarperStroke("green", "normal", tuple(green), tuple(green), {"chroma_range": 0.07},
                          (tuple(start), tuple(end)))
    state = replace(_hsp_state(), x_grade=XGradeState(strokes=(stroke,)))
    records, space = renderable_projection(state)
    result = apply_resolve_hs709(encoded ** 2.4, records, space) ** (1.0 / 2.4)
    error = np.max(np.abs(result - encoded), axis=1)
    assert 0.0 < error[0] < error[1]
    np.testing.assert_allclose(result[2], encoded[2], atol=1e-6)


def test_x_grade_long_outward_drag_keeps_moving_on_the_gamut_edge() -> None:
    from src.features.color.core.warper_chroma709 import chroma_warp_position
    from src.features.color.core.warper_color import apply_resolve_hs709_texture, prepare_resolve_hs709_texture
    from src.features.color.core.x_grade_domain import X_GRADE_SCOPE_AXES

    green = (0.22, 0.55, 0.18)
    start = chroma_warp_position(np.asarray(green))
    encoded = np.array((green, (0.42, 0.42, 0.42), (0.62, 0.28, 0.20)), dtype=np.float32)
    inverse = np.linalg.inv(X_GRADE_SCOPE_AXES)

    def render(distance: float, *, pinned: bool = False) -> np.ndarray:
        end = start + np.array((0.0, -distance / 900.0)) @ inverse
        stroke = WarperStroke("green", "normal", green, green, {"chroma_range": 0.04},
                              (tuple(start), tuple(end)))
        pins = (WarperPinPoint("green-pin", tuple(start)),) if pinned else ()
        grade = XGradeState(strokes=(stroke,), pin_points=pins)
        records, space = renderable_projection(replace(_hsp_state(), x_grade=grade))
        atlas = prepare_resolve_hs709_texture(records, space)
        return apply_resolve_hs709_texture(encoded, atlas, space, encoded_input=True)

    before = render(65.0)
    long = render(130.0)
    longer = render(131.0)
    assert 0.0 <= long[0, 0] < 0.01
    assert long[0, 2] > before[0, 2] + 0.04
    assert longer[0, 2] > long[0, 2]
    np.testing.assert_allclose(longer, long, atol=0.003)
    np.testing.assert_array_equal(long[1:], encoded[1:])
    np.testing.assert_array_equal(render(130.0, pinned=True), encoded)


def test_x_grade_picker_uses_primary_and_warper_without_applying_x_grade() -> None:
    from src.features.color.core.color_graph_cpu import x_grade_input_rgb
    from src.features.color.core.warper_color import WARPER_X_GRADE_SPACE
    from src.features.color.core.x_grade_render import pack_x_grade

    rgb = np.array([[[0.62, 0.29, 0.11]]], dtype=np.float32)
    offsets = np.zeros((6, 6, 2), dtype=np.float32)
    offsets[..., 0] = 0.25
    stroke = WarperStroke("warm", "normal", (0.62, 0.29, 0.11), (0.62, 0.29, 0.11),
                          {"exposure": 3.0}, ((0.30, 0.40), (0.30, 0.40)))
    packed = pack_x_grade(XGradeState(strokes=(stroke,)), offsets, 0)
    primary = {"grading_params": {"hue_shift": 45.0}}
    base = {"warper_params": offsets, "warper_space": 0}
    visible = x_grade_input_rgb(rgb, {**primary, **base}, encoded_input=True)
    picked = x_grade_input_rgb(rgb, {**primary, "warper_params": packed,
                                    "warper_space": WARPER_X_GRADE_SPACE}, encoded_input=True)
    np.testing.assert_array_equal(picked, visible)
    assert np.max(np.abs(visible - x_grade_input_rgb(rgb, primary, encoded_input=True))) > 0.10
    assert np.max(np.abs(visible - x_grade_input_rgb(rgb, base, encoded_input=True))) > 0.01


@pytest.mark.parametrize("zone", ["dim", "light"])
def test_x_grade_exposure_zones_fade_out_on_the_opposite_side(zone: str) -> None:
    from src.features.color.core.warper_chroma709 import chroma_warp_position
    from src.features.color.core.warper_color import apply_resolve_hs709

    color = np.array((0.70, 0.18, 0.10), np.float32)
    red = tuple(chroma_warp_position(color ** (1.0 / 2.4)))
    stroke = WarperStroke("red", "normal", tuple(color), tuple(color),
                          {"exposure": 1.0, "chroma_range": 0.10, "x_zone": zone,
                           "pivot": 0.54, "x_falloff": 0.3}, (red, red))
    state = replace(_hsp_state(), x_grade=XGradeState(strokes=(stroke,)))
    records, space = renderable_projection(state)
    colors = np.stack((color * 0.03, color * 8.0))
    processed = apply_resolve_hs709(colors, records, space)
    active, inactive = (0, 1) if zone == "dim" else (1, 0)
    np.testing.assert_array_equal(processed[inactive], colors[inactive])
    np.testing.assert_allclose(processed[active], colors[active] * 2.0, rtol=0.04)


def test_x_grade_only_pins_have_pointwise_identity() -> None:
    from src.features.color.core.graph_lut_export import _pointwise_warper
    from src.features.color.core.warper_state import CHROMA709_ALGORITHM

    state = WarperStateV2(WARPER_SCHEMA_VERSION, CHROMA709_ALGORITHM, "chroma_luma", "hsp", 0.0,
                          {name: WarperGrid.identity(name, 6, 6, ("dc", "dl")) for name in ("grid1", "grid2")},
                          x_grade=XGradeState(pin_points=(WarperPinPoint("a", (0.2, 0.2)),)))
    assert renderable_projection(state) == (None, 0)
    offsets, space = _pointwise_warper({"warper_state": state.to_dict()})
    assert space == 0 and not offsets.any()


@pytest.mark.parametrize("raw", [{"schema_version": 999, "future": [1, 2]}, {"schema_version": 1, "pins": "invalid"}])
def test_unreadable_x_grade_preserves_base_warper_and_raw_payload(raw: dict) -> None:
    from src.features.color.core.graph_lut_export import _pointwise_warper
    from src.features.color.core.warper_color import WARPER_X_GRADE_SPACE

    base = _hsp_state()
    grid = base.grids["hs"]
    active = replace(base, grids={"hs": grid.with_offsets(np.full_like(grid.offsets, 0.12))})
    payload = active.to_dict()
    payload["x_grade"] = raw
    layer = Layer.from_dict({"name": "future", "warper_state": payload,
                             "warper_space": WARPER_X_GRADE_SPACE,
                             "warper_params": np.ones((5, 1, 3), dtype=np.float32).tolist()})
    assert layer.warper_state is not None and layer.warper_state.x_grade is None
    assert layer.warper_state_unparsed is None
    np.testing.assert_array_equal(layer.warper_params, active.grids["hs"].offsets)
    assert layer.warper_space == 2
    assert layer.to_dict(None)["warper_state"]["x_grade"] == raw
    assert layer.warper_state.replacing_mode(base).to_dict()["x_grade"] == raw
    assert _pointwise_warper(layer.to_dict(None)) is None
