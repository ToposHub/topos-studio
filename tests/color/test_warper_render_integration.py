# -*- coding: utf-8 -*-
"""CW-B08:warper 多空间渲染集成测试(管线编译/manifest/值/持久化/投影)。

GPU 部分在真实 GL 上下文编译生产 ShaderPipeline(无 GL 环境 skip);
数值 parity 见 test_warper_color_parity.py。
"""
from __future__ import annotations

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
    WarperStateV2,
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
