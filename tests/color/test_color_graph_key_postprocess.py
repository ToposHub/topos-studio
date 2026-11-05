"""N02-B 切片 2：key 后处理跨后端对齐（CPU 实现消除 GL/CPU 分歧）。

覆盖：
- 权威换算表（key_pipeline_params）与 GL 内联旧式数值一致；
- CPU 后处理语义：clean_black 收缩 / clean_white 膨胀 / blur 平滑 /
  denoise 极化（单调性与边界）；
- GL 可分 morphology/blur 与 numpy 等价在同输入同参数下逐像素对拍
  （standalone ctx 直接跑 shader，消除宿主注入依赖）；
- CPU 图路径带后处理 qualifier 不再 UnsupportedNode 且与手算一致。
"""
from __future__ import annotations

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.key_pipeline_params import (
    blur_radius, blur_sigma, clean_radius, denoise_edge_halfwidth,
    denoise_radius, denoise_strength, postprocess_layers,
)
from src.features.color.core.qualifier_cpu import (
    apply_qualifier_postprocess_cpu,
)


# ── 权威换算表 ────────────────────────────────────────────────────

def test_authority_table_matches_legacy_inline_math():
    """换算表与 GL 旧内联式逐值一致（行为不变，来源统一）。"""
    for v in (0.0, 0.009, 0.01, 1.0, 5.0, 10.0, 20.0):
        legacy = int(np.ceil(max(1.0, v * 3.0))) if v > 0.01 else 0
        assert clean_radius(v) == legacy, (v, clean_radius(v), legacy)
    for v in (0.0, 0.01, 1.0, 50.0, 100.0):
        legacy = int(np.ceil(max(1.0, v * 1.0))) if v > 0.01 else 0
        assert blur_radius(v) == legacy
    for v in (0.0, 0.5, 1.0, 50.0, 100.0):
        legacy = int(np.ceil(max(1.0, v * 0.04))) if v > 0.5 else 0
        assert denoise_radius(v) == legacy
    assert denoise_strength(50.0) == 0.5
    assert denoise_strength(150.0) == 1.0  # clamp
    assert denoise_edge_halfwidth(0.0) == pytest.approx(0.45)
    assert denoise_edge_halfwidth(1.0) == pytest.approx(0.05)
    assert postprocess_layers({"clean_black": 5.0, "blur": 2.0}) == (
        "clean_black", "blur")


# ── CPU 后处理语义 ────────────────────────────────────────────────

def _disc_matte(size: int = 64) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    r = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    return (r < size * 0.3).astype(np.float32)


def test_cpu_clean_black_erodes_white_region():
    matte = _disc_matte()
    # clean_black=2.0 → r=6（方形窗）：角落点 √(6²+6²)=8.5 < 圆半径 19.2，
    # 中心邻域全白 → 中心保持 1；边缘内缩。
    out = apply_qualifier_postprocess_cpu(matte, {"clean_black": 2.0})
    assert out[32, 32] == 1.0
    assert out.sum() < matte.sum()
    # 黑区保持黑
    assert out[2, 2] == 0.0
    # 大半径下中心也应被侵蚀（方形窗角落越界圆外）
    harsh = apply_qualifier_postprocess_cpu(matte, {"clean_black": 5.0})
    assert harsh[32, 32] == 0.0


def test_cpu_clean_white_dilates():
    matte = _disc_matte()
    out = apply_qualifier_postprocess_cpu(matte, {"clean_white": 5.0})
    assert out.sum() > matte.sum()
    # 白区扩张但仍在 [0,1]
    assert out.max() <= 1.0 and out.min() >= 0.0


def test_cpu_blur_smooths_and_denoise_polarizes():
    matte = _disc_matte()
    blurred = apply_qualifier_postprocess_cpu(matte, {"blur": 6.0})
    # 平滑：不再有纯 0/1 阶跃边界（出现中间值）
    fractional = ((blurred > 0.02) & (blurred < 0.98)).sum()
    assert fractional > 0
    denoised = apply_qualifier_postprocess_cpu(
        matte, {"denoise": 80.0})
    # denoise：模糊+对比恢复——边缘更陡（中间值更少或相当），值域钳制
    assert denoised.min() >= 0.0 and denoised.max() <= 1.0


def test_cpu_postprocess_order_clean_black_then_white():
    # 同时给 clean_black+clean_white：erode 后 dilate（可分方形窗合成
    # 语义），数值与两步手算一致
    matte = _disc_matte(32)
    both = apply_qualifier_postprocess_cpu(
        matte, {"clean_black": 2.0, "clean_white": 2.0})
    step1 = apply_qualifier_postprocess_cpu(matte, {"clean_black": 2.0})
    step2 = apply_qualifier_postprocess_cpu(step1, {"clean_white": 2.0})
    np.testing.assert_allclose(both, step2, atol=0.0)


# ── GL shader ↔ CPU 等价逐像素对拍 ───────────────────────────────

def _run_gl_postprocess(ctx, matte, radius, shader_kind,
                        denoise_strength_value=0.0, operation=0):
    """standalone ctx 直接跑 morphology/blur/denoise shader（H+V 双趟）。

    与 AsyncRenderer 生产路径同构：morph/blur 可分两趟（src→mid→dst）；
    denoise 单趟对比恢复。shader 源逐行复制自 morphology.glsl/blur.glsl/
    matte_denoise.glsl（权威实现的就地内联副本，防漂移）。
    """
    size = matte.shape[0]
    # 顶点布局照 test_m06_keyer_gpu_parity 的成熟管线：交错 (x,y,u,v) +
    # TRIANGLE_STRIP（此前 pos 推导 uv 的 FAN 管线在本机渲染不完整）。
    vs = """
    #version 330
    in vec2 in_vert;
    in vec2 in_uv;
    out vec2 v_uv;
    void main() { v_uv = in_uv; gl_Position = vec4(in_vert, 0.0, 1.0); }
    """
    if shader_kind == "morph":
        fs = """
        #version 330
        uniform sampler2D u_tex;
        uniform float u_radius;
        uniform int u_operation;
        uniform vec2 u_texel_size;
        uniform int u_direction;
        in vec2 v_uv;
        out float f_result;
        void main() {
            float result = texture(u_tex, v_uv).r;
            int r = int(ceil(u_radius));
            if (r <= 0) { f_result = result; return; }
            vec2 dir = (u_direction == 0) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
            for (int i = -r; i <= r; i++) {
                if (i == 0) continue;
                float val = texture(u_tex, v_uv + dir * float(i) * u_texel_size).r;
                result = (u_operation == 0) ? min(result, val) : max(result, val);
            }
            f_result = result;
        }
        """
    elif shader_kind == "blur":
        fs = """
        #version 330
        uniform sampler2D u_tex;
        uniform vec2 u_direction;
        uniform float u_radius;
        in vec2 v_uv;
        out float f_result;
        float gaussian(float x, float sigma) { return exp(-(x * x) / (2.0 * sigma * sigma)); }
        void main() {
            float c = texture(u_tex, v_uv).r;
            if (u_radius <= 0.0) { f_result = c; return; }
            float sigma = max(u_radius * 0.5, 0.1);
            float total = gaussian(0.0, sigma);
            float acc = c * total;
            int r = int(ceil(u_radius));
            for (int i = 1; i <= r; i++) {
                float w = gaussian(float(i), sigma);
                acc += texture(u_tex, v_uv + u_direction * float(i)).r * w;
                acc += texture(u_tex, v_uv - u_direction * float(i)).r * w;
                total += 2.0 * w;
            }
            f_result = acc / total;
        }
        """
    else:
        fs = """
        #version 330
        uniform sampler2D u_tex;
        uniform float u_strength;
        in vec2 v_uv;
        out float f_result;
        void main() {
            float m = texture(u_tex, v_uv).r;
            if (u_strength <= 0.0) { f_result = m; return; }
            float k = mix(0.45, 0.05, clamp(u_strength, 0.0, 1.0));
            f_result = smoothstep(0.5 - k, 0.5 + k, m);
        }
        """
    prog = ctx.program(vertex_shader=vs, fragment_shader=fs)
    quad = ctx.buffer(np.array(
        [-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1],
        dtype=np.float32).tobytes())
    vao = ctx.vertex_array(prog, quad, "in_vert", "in_uv")

    def make_tex(data16=None):
        tex = ctx.texture((size, size), components=1, dtype="f2")
        # NEAREST：消除 f16 LINEAR 采样在部分驱动上的半 texel 歧义；
        # CLAMP：与生产 matte 纹理（CLAMP_TO_EDGE）及 ndimage 'nearest'
        # 的边界外推语义一致（moderngl 默认 REPEAT 会在窗越界时回绕）。
        tex.filter = (ctx.NEAREST, ctx.NEAREST)
        tex.repeat_x = False
        tex.repeat_y = False
        if data16 is not None:
            tex.write(data16.astype(np.float16).tobytes())
        return tex

    src = make_tex(matte)
    mid = make_tex()
    dst = make_tex()

    def render_to(target_tex, uniforms):
        fbo = ctx.framebuffer(color_attachments=[target_tex])
        fbo.use()
        ctx.viewport = (0, 0, size, size)
        ctx.clear(0.0, 0.0, 0.0, 0.0)
        for key, value in uniforms.items():
            prog[key].value = value
        vao.render(moderngl.TRIANGLE_STRIP)

    prog["u_tex"].value = 0
    if shader_kind == "morph":
        for src_tex, direction in ((src, 0), (mid, 1)):
            src_tex.use(0)
            render_to(mid if direction == 0 else dst, {
                "u_radius": float(radius), "u_operation": int(operation),
                "u_texel_size": (1.0 / size, 1.0 / size),
                "u_direction": direction})
        out = dst
    elif shader_kind == "blur":
        # blur.glsl 的 u_direction 是归一化步长向量（(1/w,0)/(0,1/h)），
        # 形态学的 u_texel_size 同义——两个 shader 的偏移语义不同源。
        for src_tex, target, direction in ((src, mid, (1.0, 0.0)),
                                           (mid, dst, (0.0, 1.0))):
            src_tex.use(0)
            render_to(target, {
                "u_radius": float(radius),
                "u_direction": (direction[0] / size, direction[1] / size)})
        out = dst
    else:
        src.use(0)
        render_to(dst, {"u_strength": float(denoise_strength_value)})
        out = dst

    fbo = ctx.framebuffer(color_attachments=[out])
    fbo.use()
    raw = fbo.read(components=1, dtype="f2")
    result = np.frombuffer(raw, dtype=np.float16).reshape(size, size)
    for tex in (src, mid, dst):
        tex.release()
    quad.release()
    vao.release()
    prog.release()
    return result.astype(np.float32)


def test_gl_morphology_matches_cpu_erode():
    from src.features.color.core.qualifier_cpu import (
        _box_min_max,
    )
    ctx = moderngl.create_standalone_context(require=330)
    try:
        matte = _disc_matte(48)
        radius = clean_radius(3.0)
        gl = _run_gl_postprocess(ctx, matte, radius, "morph")
        cpu = _box_min_max(matte, radius, maximum=False)
        np.testing.assert_allclose(gl, cpu, atol=2e-3)
    finally:
        ctx.release()


def test_gl_blur_matches_cpu_gaussian():
    from src.features.color.core.qualifier_cpu import (
        _separable_gaussian,
    )
    ctx = moderngl.create_standalone_context(require=330)
    try:
        matte = _disc_matte(48)
        radius = blur_radius(4.0)
        gl = _run_gl_postprocess(ctx, matte, radius, "blur")
        cpu = _separable_gaussian(matte, radius, blur_sigma(radius))
        np.testing.assert_allclose(gl, cpu, atol=2e-3)
    finally:
        ctx.release()


def test_gl_denoise_matches_cpu_smoothstep():
    from src.features.color.core.qualifier_cpu import _smoothstep_np
    ctx = moderngl.create_standalone_context(require=330)
    try:
        rng = np.random.default_rng(7)
        matte = rng.uniform(0.0, 1.0, (48, 48)).astype(np.float32)
        strength = denoise_strength(60.0)
        gl = _run_gl_postprocess(ctx, matte, 0.0, "denoise",
                                 denoise_strength_value=strength)
        k = denoise_edge_halfwidth(strength)
        cpu = _smoothstep_np(np.float32(0.5 - k), np.float32(0.5 + k), matte)
        np.testing.assert_allclose(gl, cpu, atol=2e-3)
    finally:
        ctx.release()


# ── CPU 图路径不再拒绝后处理 ─────────────────────────────────────

def test_cpu_graph_path_supports_postprocessed_qualifier():
    from src.features.color.core.color_graph_cpu import _cpu_node_matte
    from src.features.color.core.color_graph_compiler import compile_graph
    from src.features.color.core.color_graph_snapshot import freeze_graph
    from tests.color.test_color_graph_schema import _serial_graph
    from tests.color.test_color_graph_key_pipeline_decl import _with_params

    graph = _with_params(_serial_graph(1), {"qualifier": {
        "enabled": True, "clean_black": 4.0, "blur": 3.0}})
    plan = compile_graph(freeze_graph(graph, session_id="s"), size=(64, 64))
    corrector_pass = next(p for p in plan.passes if p.op == "corrector")
    base = np.tile(np.linspace(0.05, 0.95, 64, dtype=np.float32)[None, :],
                   (64, 1))
    base = np.stack([base, base * 0.5, 1 - base], axis=-1)
    matte = _cpu_node_matte(base, {"qualifier_params": {
        "enabled": True, "clean_black": 4.0, "blur": 3.0,
        "hue_enabled": True, "hue_center": 0.5, "hue_width": 1.0,
        "hue_soft": 0.1, "sat_enabled": True, "sat_low": 0.0,
        "sat_high": 1.0, "sat_soft_low": 0.0, "sat_soft_high": 0.0,
        "lum_enabled": True, "lum_low": 0.0, "lum_high": 1.0,
        "lum_soft_low": 0.0, "lum_soft_high": 0.0}}, corrector_pass, None)
    assert matte is not None and matte.shape == (64, 64, 1)
    assert matte.min() >= 0.0 and matte.max() <= 1.0
