"""ADR 0004 门禁 G1：composition shader golden corpus（位级回归）。

对 ``ShaderPipeline.build_composition_shader`` 编译出的**生产渲染 shader**
（async_renderer:2217/5880、image_engine:2914/3139 实际使用的是
composition 路径而非 build_shader 融合路径），在 ModernGL standalone
context 上以固定输入图案渲染固定效果旗标组合，输出 RGBA8 **位级哈希**
与已归档基线比对。

- 门禁定义见 ``docs/adr/0004-dual-emitter-refactor-and-coverage-gates.md``：
  任何触碰 shader_compiler / effect_spec / shader_nodes 的 PR 必须全绿
  ——重构期间 GL 输出像素不得变化（P7-01 验收原文）。
- 基线按 GPU 指纹绑定：``tests/color/golden/shader_golden_corpus.json``。
  换机器/驱动后用 ``TOPOS_UPDATE_SHADER_GOLDEN=1`` 重新归档（人工确认
  渲染无变化后提交）。基线缺失时测试直接失败并给出归档指引。
- v1 刻意不覆盖（随各自迁移 PR 加入）：curves/hsl_curve/warper（需曲线/
  网格纹理 fixture）、lut3d（需 sampler3D + .cube fixture）。
- v2（2026-09-11，P7-01 前置）：加入**真实 .tglsl 插件栈用例**
  （HighPass_Retouch 单插件 + HighPass×bilateral 双插件，PluginManager 扫
  仓库 plugins/ 目录）。在编译入口权威化重构**之前**归档插件基线——重构后
  G1 才能证明插件路径（uniform 声明、正则命名空间、结构化缓存键）也位级不变。
- 位级（而非容差）的理由：容差会把重构期的语义漂移稀释进噪声——这正是
  ADR 0004 否决「放宽 G1 为容差一致」的原因。
"""

from __future__ import annotations

import hashlib
import json
import os

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用，跳过 G1 golden corpus")

from src.features.color.core.shader_compiler import ShaderPipeline  # noqa: E402

GOLDEN_PATH = os.path.join(
    os.path.dirname(__file__), "golden", "shader_golden_corpus.json"
)
W, H = 64, 48
UPDATE_ENV = "TOPOS_UPDATE_SHADER_GOLDEN"

# Layer.BLEND_MODES 索引（shader_compiler 按此枚举选择混合公式）
BLEND_SOFT_LIGHT = 3

# ── 固定输入图案 ────────────────────────────────────────────────────────


def _pattern_hdr_ramp() -> np.ndarray:
    """水平亮度斜坡 + HDR>1 条带 + 负值条带 + 灰阶（RGBA16F 载体）。"""
    img = np.zeros((H, W, 4), dtype=np.float32)
    img[..., :3] = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :, None]
    img[:, :8, :3] = np.linspace(-0.2, 0.0, 8, dtype=np.float32)[None, :, None]
    img[:, -8:, :3] = np.linspace(1.0, 1.5, 8, dtype=np.float32)[None, :, None]
    for row, g in zip(range(8, 32, 3), (0.0, 0.018, 0.081, 0.18, 0.5, 0.9, 1.0, 1.2)):
        img[row:row + 3, 16:48, :3] = g
    img[..., 3] = 1.0
    return np.ascontiguousarray(img)


def _pattern_checker() -> np.ndarray:
    """肤色 + 高饱和色块（8×6 棋盘）。"""
    palette = [
        (0.859, 0.729, 0.604), (0.769, 0.584, 0.486), (0.180, 0.412, 0.667),
        (0.639, 0.200, 0.184), (0.204, 0.529, 0.290), (0.878, 0.804, 0.067),
        (0.055, 0.055, 0.055), (0.945, 0.945, 0.945),
    ]
    img = np.zeros((H, W, 4), dtype=np.float32)
    for r in range(6):
        for c in range(8):
            img[r * 8:(r + 1) * 8, c * 8:(c + 1) * 8, :3] = palette[(r * 8 + c) % 8]
    img[..., 3] = 1.0
    return np.ascontiguousarray(img)


def _matte_white() -> np.ndarray:
    m = np.ones((H, W, 4), dtype=np.float32)
    return np.ascontiguousarray(m)


def _matte_gradient() -> np.ndarray:
    m = np.zeros((H, W, 4), dtype=np.float32)
    m[..., 0] = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :]
    m[..., 3] = 1.0
    return np.ascontiguousarray(m)


# ── 用例：composition 旗标组合 + u_* uniform 值 ─────────────────────────

# 与 update_uniforms/_update_composition_uniforms 的中性缺省一致
_BASE_UNIFORMS = {
    "u_lift": (0.0, 0.0, 0.0), "u_gamma": (1.0, 1.0, 1.0),
    "u_gain": (1.0, 1.0, 1.0),
    "u_exposure": 0.0, "u_temp": 0.0, "u_tint": 0.0,
    "u_contrast": 0.0, "u_saturation": 1.0, "u_vibrance": 0.0,
    "u_shadow": 0.0, "u_highlights": 0.0, "u_mid_detail": 0.0,
    "u_sharpness": 0.0, "u_hue_shift": 0.0,
    "u_fx_mode": 0, "u_fx_sharpen": 0.0, "u_fx_texture": 0.0,
    "u_fx_halo": 0.0, "u_fx_radius": 1.0, "u_fx_iterations": 1,
    "u_fx_enabled": False, "u_blur_enabled": False,
    "u_blur_mode": 0, "u_blur_intensity": 0.0, "u_blur_detail": 0.0,
    "u_blur_radius": 0.0, "u_blur_bokeh": 0.0, "u_blur_angle": 0.0,
    "u_opacity": 1.0, "u_blend_mode": 0,
}

_MIXER_NEUTRAL = {f"u_{c}_p": (0.0, 0.0, 0.0)
                  for c in ("red", "orange", "yellow", "green",
                            "aqua", "blue", "purple", "magenta")}


def _cases():
    grading = {
        "u_lift": (0.0, 0.0, 0.02), "u_gamma": (0.95, 1.0, 1.02),
        "u_gain": (1.05, 1.0, 0.96), "u_exposure": 0.3, "u_temp": 0.04,
        "u_tint": -0.02, "u_contrast": 0.15, "u_saturation": 1.2,
        "u_vibrance": 0.1, "u_shadow": 0.05, "u_highlights": -0.05,
    }
    grading_extreme = {
        "u_lift": (0.1, -0.05, 0.08), "u_gamma": (1.25, 0.75, 1.1),
        "u_gain": (1.4, 0.7, 1.2), "u_exposure": 2.0, "u_contrast": 0.8,
        "u_saturation": 0.2, "u_vibrance": -0.4, "u_shadow": 0.4,
        "u_highlights": 0.5, "u_mid_detail": 0.2, "u_sharpness": 0.5,
        "u_hue_shift": 0.1, "u_temp": -0.3, "u_tint": 0.25,
    }
    sc = {
        "u_sc_enabled": True, "u_sc_high": 0.9, "u_sc_white_point": 1.3,
        "u_sc_shoulder_strength": 0.7, "u_sc_low": 0.02,
        "u_sc_black_point": -0.05, "u_sc_toe_strength": 0.8,
        "u_sc_exposure": 0.4, "u_sc_contrast": 0.2,
    }
    mixer = {
        "u_red_p": (1.1, -0.05, 0.0), "u_green_p": (0.0, 1.05, -0.1),
        "u_blue_p": (-0.08, 0.0, 1.15), "u_yellow_p": (0.05, 0.05, 0.0),
    }
    grain = {
        "u_grain_intensity": 0.3, "u_grain_size": 2.0,
        "u_grain_monochrome": False, "u_grain_shadows": 0.8,
        "u_grain_midtones": 1.0, "u_grain_highlights": 0.5,
        "u_grain_seed": 0.42, "u_grain_aspect_ratio": W / H,
    }
    grain_mono = dict(grain, u_grain_monochrome=True, u_grain_intensity=0.18,
                      u_grain_size=1.6, u_grain_seed=0.42)
    fx_sharpen = {
        "u_fx_enabled": True, "u_fx_mode": 1, "u_fx_sharpen": 0.5,
        "u_fx_texture": 0.3, "u_fx_halo": 0.1, "u_fx_radius": 1.0,
        "u_fx_iterations": 2,
    }
    fx_blur = {
        "u_fx_enabled": True, "u_fx_mode": 0, "u_blur_enabled": True,
        "u_blur_mode": 1, "u_blur_intensity": 0.6, "u_blur_detail": 0.3,
        "u_blur_radius": 2.0, "u_blur_bokeh": 0.0, "u_blur_angle": 0.0,
    }
    # 真实 .tglsl 插件实例（plugins/ 目录，filename stem 即 plugin_id）。
    # params 显式全给值：u_P{n}_* uniform 由下方 uniforms 覆盖上传，
    # 值与实例 params 保持一致（与 _update_composition_uniforms 行为对齐）。
    highpass = {
        "plugin_id": "HighPass_Retouch", "enabled": True,
        "params": {
            "smooth_radius": 0.5, "detail_preserve": 0.5,
            "mix_amount": 0.85,
        },
    }
    bilateral = {
        "plugin_id": "bilateral_skin_smooth", "enabled": True,
        "params": {
            "strength": 0.7, "radius": 0.4,
            "threshold": 0.2, "recover": 0.5,
        },
    }
    highpass_uniforms = {
        "u_P0_smooth_radius": 0.5, "u_P0_detail_preserve": 0.5,
        "u_P0_mix_amount": 0.85,
    }
    bilateral_uniforms = {
        "u_P1_strength": 0.7, "u_P1_radius": 0.4,
        "u_P1_threshold": 0.2, "u_P1_recover": 0.5,
    }

    return [
        # (名称, build_composition_shader 旗标, uniform 覆盖, matte)
        ("passthrough_neutral", {}, {}, "white"),
        ("grading_basic", {}, grading, "white"),
        ("grading_extreme", {}, grading_extreme, "white"),
        ("soft_clip_hdr",
         {"has_soft_clip": True}, sc, "white"),
        ("mixer_channels",
         {"has_mixer": True}, mixer, "white"),
        ("film_grain_seed042",
         {"grain_enabled": True}, grain, "white"),
        ("blur_sharpen_fx",
         {}, fx_sharpen, "white"),
        ("blur_gaussian_fx",
         {}, fx_blur, "white"),
        ("matte_gradient_blend",
         {}, {"u_opacity": 0.7, "u_blend_mode": BLEND_SOFT_LIGHT},
         "gradient"),
        ("stack_all_flags",
         {"has_soft_clip": True, "has_mixer": True, "grain_enabled": True},
         {**grading, **sc, **mixer, **grain_mono,
          "u_opacity": 0.85},
         "gradient"),
        # 插件栈（P7-01 重构的位级锚点）：单插件 + 双插件（P0/P1 命名空间）
        ("plugin_highpass_single",
         {"plugins": [highpass]}, highpass_uniforms, "white"),
        ("plugin_stack_two",
         {"plugins": [highpass, bilateral]},
         {**highpass_uniforms, **bilateral_uniforms}, "checker"),
    ]


PATTERNS = ("hdr_ramp", "checker")


# ── 渲染与哈希 ──────────────────────────────────────────────────────────


def _upload_f2(ctx, arr: np.ndarray):
    tex = ctx.texture((W, H), 4, dtype="f2")  # RGBA16F，与生产输入精度一致
    tex.write(arr.astype(np.float16).tobytes())
    return tex


def _render_case(ctx, pipeline, flags, uniforms, matte_name, pattern) -> bytes:
    flags = dict(flags)
    plugin_instances = flags.pop("plugins", None)
    # P7-01：编译入口只接受 PluginBinding——用生产适配器从实例 dict 解析
    from src.features.color.core.shader_compiler import (
        plugin_bindings_from_instances,
    )
    plugins = (plugin_bindings_from_instances(plugin_instances)
               if plugin_instances is not None else None)
    prog = pipeline.build_composition_shader(**flags, plugins=plugins)
    assert prog is not None, f"composition shader 编译失败：flags={flags}"

    src_tex = _upload_f2(ctx, pattern)
    matte = _upload_f2(
        ctx, _matte_white() if matte_name == "white" else _matte_gradient())
    fbo = ctx.framebuffer(ctx.texture((W, H), 4, dtype="f1"))
    quad = ctx.buffer(np.array([
        -1.0, -1.0, 0.0, 0.0,
         1.0, -1.0, 1.0, 0.0,
        -1.0,  1.0, 0.0, 1.0,
         1.0,  1.0, 1.0, 1.0,
    ], dtype="f4").tobytes())
    vao = ctx.vertex_array(prog, [(quad, "2f 2f", "in_vert", "in_texcoord")])

    values = {**_BASE_UNIFORMS, **_MIXER_NEUTRAL, **uniforms}
    for name, val in values.items():
        if name in prog:  # 未编译进 shader 的旗标对应 uniform 直接跳过
            prog[name].value = val
    if "u_tex_size" in prog:
        prog["u_tex_size"].value = (float(W), float(H))

    fbo.use()
    ctx.viewport = (0, 0, W, H)
    src_tex.use(0)
    matte.use(1)
    prog["u_input_tex"].value = 0
    prog["u_matte_tex"].value = 1
    vao.render(moderngl.TRIANGLE_STRIP)
    raw = fbo.read(components=4)
    for res in (src_tex, matte, fbo, quad, vao):
        try:
            res.release()
        except Exception:
            pass
    return raw


def _case_hash(raw: bytes, case_name: str, pattern_name: str,
               flags, uniforms, matte_name) -> str:
    payload = {
        "case": case_name, "pattern": pattern_name, "matte": matte_name,
        "flags": json.dumps(flags, sort_keys=True, default=str),
        "uniforms": json.dumps(uniforms, sort_keys=True, default=str),
        "pixels": hashlib.sha256(raw).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()


@pytest.fixture(scope="module")
def gl():
    ctx = moderngl.create_standalone_context(require=330)
    yield ctx
    ctx.release()


@pytest.fixture(scope="module")
def pipeline(gl):
    # 插件用例前置：把仓库 plugins/ 目录扫进 PluginManager（绝对路径，
    # 与 CWD 无关）。扫描失败仅影响插件用例——非插件用例照常执行。
    try:
        from src.features.color.core.plugin_manager import get_plugin_manager
        _repo = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        get_plugin_manager().scan_plugins(os.path.join(_repo, "plugins"))
    except Exception as exc:  # noqa: BLE001
        print(f"PluginManager 扫描失败（插件用例将失败）: {exc}")
    p = ShaderPipeline(gl)
    yield p
    p.release()


@pytest.mark.filterwarnings("ignore")
class TestShaderGoldenCorpus:
    def test_bit_identical_to_baseline(self, gl, pipeline, request):
        gpu = gl.info.get("GL_RENDERER", "unknown")
        gl_version = gl.info.get("GL_VERSION", "unknown")
        baseline = None
        if os.path.exists(GOLDEN_PATH):
            with open(GOLDEN_PATH) as f:
                baseline = json.load(f)
        update_mode = os.environ.get(UPDATE_ENV, "").strip() not in ("", "0")
        if baseline is None and not update_mode:
            pytest.fail(
                "G1 基线缺失：先以人工审查确认当前渲染为预期，然后 "
                f"{UPDATE_ENV}=1 pytest {request.node.fspath} 归档并提交基线"
            )
        if baseline is not None and not update_mode \
                and baseline.get("_meta", {}).get("gpu") != gpu:
            pytest.skip(
                f"基线绑定其他 GPU（{baseline.get('_meta', {}).get('gpu')}，"
                f"本机 {gpu}）：{UPDATE_ENV}=1 重新归档"
            )

        hashes = {}
        for case_name, flags, uniforms, matte in _cases():
            for pattern_name in PATTERNS:
                pattern = (_pattern_hdr_ramp() if pattern_name == "hdr_ramp"
                           else _pattern_checker())
                raw = _render_case(gl, pipeline, flags, uniforms,
                                   matte, pattern)
                key = f"{case_name}/{pattern_name}"
                hashes[key] = _case_hash(raw, case_name, pattern_name,
                                         flags, uniforms, matte)

        if update_mode:
            os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
            with open(GOLDEN_PATH, "w") as f:
                json.dump({
                    "_meta": {"gpu": gpu, "gl_version": gl_version,
                              "count": len(hashes)},
                    **hashes,
                }, f, indent=2, sort_keys=True)
            pytest.skip(f"已归档 {len(hashes)} 条基线 → {GOLDEN_PATH}（记得提交）")

        mismatches = {k: (baseline.get(k), v) for k, v in hashes.items()
                      if baseline.get(k) != v}
        assert not mismatches, (
            "G1 位级回归失败（ADR 0004）：以下用例输出与基线不一致：\n"
            + "\n".join(f"  {k}" for k in mismatches)
            + "\n若变更已过人工审查且确为预期，"
            + f"以 {UPDATE_ENV}=1 重归档并写入台账 0004a"
        )

    def test_corpus_covers_baseline_exactly(self):
        """防基线腐化：归档文件里的用例键与代码中的 corpus 完全一致。"""
        if not os.path.exists(GOLDEN_PATH):
            pytest.skip("基线未归档")
        with open(GOLDEN_PATH) as f:
            baseline = json.load(f)
        expected = {f"{name}/{pat}"
                    for name, _, _, _ in _cases() for pat in PATTERNS}
        actual = {k for k in baseline if not k.startswith("_")}
        assert actual == expected, (
            f"基线与 corpus 用例集不一致：多出 {actual - expected}，"
            f"缺失 {expected - actual}"
        )
