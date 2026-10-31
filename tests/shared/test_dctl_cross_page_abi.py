"""L02 跨页 GLSL ABI 验证（升级计划 L，Mac 集成者角色）。

背景：dctl_wrapper 在 L02 前无参数名/引擎 uniform 别名层——按 Color 合成
约定书写的真实插件（裸参数名 + u_input_tex/u_tex_size/u_aspect_ratio）在
Edit runner 下必然 undeclared identifier 编译失败并静默 no-op（L00 台账
§5.1）。本文件证明 L02 修复后的真实执行链：

1. 全部生产 .tglsl（35 个）经 wrap_dctl_process 在 standalone GL 3.3 上下文
   **真实编译**（不是 regex 查头、不是伪造 uniform）；
2. 试点 F16（点处理类）与 F19（纹理邻域类）经 runner 约定的顶点着色器/
   全屏 quad **真实渲染**，像素级对拍独立 CPU oracle（L02 两类子门）；
3. 参数打包器产出的 u_<name> uniform 在编译产物中真实存在；
4. 保留字参数名（如 GLSL 内建 mix）被显式拒绝（对齐 shader_compiler AUD-0832）；
5. 已发布的 LIB1_* 插件经真实 EffectCatalog 注册成 plugin:<id> spec。

无 GL 环境按 gpu_renderer 惯例 skip 带原因。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")

from src.features.color.core.plugin_manager import PluginManager
from src.features.shared.effects.dctl_wrapper import wrap_dctl_process
from src.features.shared.effects.effect_pass_runner import (
    EffectPassRunner, _PASSTHROUGH_VERTEX, _QUAD_VERTICES,
)

PLUGINS_DIR = REPO_ROOT / "plugins"
STAGING_FILTERS = REPO_ROOT / "content_staging" / "library_v1" / "filters"


@pytest.fixture(scope="module")
def gl_ctx():
    try:
        ctx = moderngl.create_standalone_context(require=330)
    except Exception as exc:  # pragma: no cover - 无 GPU 环境
        pytest.skip(f"standalone GL 3.3 context 不可用: {exc}")
    yield ctx
    ctx.release()


def _parse_plugin(path: Path) -> dict:
    data = PluginManager()._parse_plugin_file(str(path), path.stem)
    assert data is not None, f"真实 parser 解析失败: {path.name}"
    return data


def _set_uniforms(program, params: dict, parsed_params: list[dict]) -> None:
    """按打包器同口径设置 uniform（select→索引、color→vec3、checkbox→int）。"""
    for p in parsed_params:
        name = p["var_name"]
        key = f"u_{name}"  # wrapper 声明的 uniform 名（select 索引口径与打包器一致）
        if name not in params or key not in program:
            continue
        value = params[name]
        ptype = p["type"]
        if ptype == "slider":
            program[key].value = float(value)
        elif ptype in {"checkbox", "select"}:
            if isinstance(value, str):  # select label → 索引
                value = p["options"].index(value)
            program[key].value = int(value)
        elif ptype == "color":
            program[key].value = tuple(float(v) for v in value)


def _default_params(parsed_params: list[dict]) -> dict:
    """解析产物默认值（select 为 int 索引、color 为 3 元组）。"""
    out = {}
    for p in parsed_params:
        if p.get("type") in {"slider", "checkbox", "select"}:
            out[p["var_name"]] = p["default"]
        elif p.get("type") == "color":
            out[p["var_name"]] = tuple(float(v) for v in p["default"])
    return out


def _render(ctx, fragment_src: str, image: np.ndarray,
            params: dict, parsed_params: list[dict]) -> np.ndarray:
    """按 EffectPassRunner 的几何/命名约定渲染一帧并读回 float32 RGBA。"""
    h, w = image.shape[0], image.shape[1]
    program = ctx.program(vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=fragment_src)
    rgba = np.zeros((h, w, 4), dtype=np.float32)
    rgba[..., :3] = image
    rgba[..., 3] = 1.0
    src_tex = ctx.texture((w, h), 4, rgba.tobytes(), dtype="f4")
    src_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    src_tex.use(0)
    program["u_texture"].value = 0
    if "u_resolution" in program:  # 未使用时被链接器优化掉
        program["u_resolution"].value = (w, h)
    merged = _default_params(parsed_params)
    merged.update(params)
    _set_uniforms(program, merged, parsed_params)

    target = ctx.texture((w, h), 4, dtype="f4")
    fbo = ctx.framebuffer(color_attachments=[target])
    vbo = ctx.buffer(_QUAD_VERTICES.tobytes())
    vao = ctx.vertex_array(program, vbo, "in_vert", "in_uv")
    fbo.use()
    vao.render(moderngl.TRIANGLE_STRIP)
    raw = fbo.read(components=4, dtype="f4")
    vao.release()
    vbo.release()
    target.release()
    src_tex.release()
    fbo.release()
    program.release()
    return np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)


# ──────────────────────────────────────────────────────────────────────
# 1. 全库真实编译（L02 主证据：修共享缝后才能声明跨页）
# ──────────────────────────────────────────────────────────────────────

def test_all_production_tglsl_compile_cross_page(gl_ctx) -> None:
    files = sorted(PLUGINS_DIR.glob("*.tglsl"))
    assert len(files) >= 33, "生产插件数量异常"
    failures = []
    for path in files:
        data = _parse_plugin(path)
        fragment = wrap_dctl_process(data["shader_code"], data["params"])
        try:
            program = gl_ctx.program(
                vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=fragment)
            program.release()
        except Exception as exc:
            failures.append(f"{path.stem}: {exc}")
    assert not failures, "跨页编译失败:\n" + "\n".join(failures)


def test_staging_pilots_compile_cross_page(gl_ctx) -> None:
    for stem in ("gradient_map", "median_rank"):
        data = _parse_plugin(STAGING_FILTERS / stem / f"{stem}.tglsl")
        fragment = wrap_dctl_process(data["shader_code"], data["params"])
        program = gl_ctx.program(
            vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=fragment)
        program.release()


# ──────────────────────────────────────────────────────────────────────
# 2. 试点像素级对拍（点处理子门 F16 / 纹理邻域子门 F19）
# ──────────────────────────────────────────────────────────────────────

def test_f16_pilot_pixel_parity(gl_ctx) -> None:
    from content_staging.library_v1.fixtures.generate_fixtures import gray_ramp_linear
    from content_staging.library_v1.references import gradient_map_ref

    data = _parse_plugin(STAGING_FILTERS / "gradient_map" / "gradient_map.tglsl")
    fragment = wrap_dctl_process(data["shader_code"], data["params"])
    image = gray_ramp_linear()
    cases = [
        {},                                     # 默认参数
        {"amount": 0.5},                        # 混合
        {"interp_mode": 1},                     # Smooth（GLSL 侧是索引）
        {"pos_1": 0.2, "pos_2": 0.5},
    ]
    for params in cases:
        rendered = _render(gl_ctx, fragment, image, params, data["params"])[..., :3]
        oracle_params = dict(params)
        if "interp_mode" in oracle_params and isinstance(oracle_params["interp_mode"], int):
            select_p = next(p for p in data["params"] if p["var_name"] == "interp_mode")
            oracle_params["interp_mode"] = select_p["options"][oracle_params["interp_mode"]]
        expected = gradient_map_ref.evaluate(image, oracle_params)
        # float32 纹理往返 + GPU 插值误差容差（oracle 为 float64 语义）
        np.testing.assert_allclose(
            rendered, expected, atol=6e-4,
            err_msg=f"F16 参数 {params} 像素对拍失败")


def test_f19_pilot_pixel_parity(gl_ctx) -> None:
    from content_staging.library_v1.references import median_rank_ref

    data = _parse_plugin(STAGING_FILTERS / "median_rank" / "median_rank.tglsl")
    fragment = wrap_dctl_process(data["shader_code"], data["params"])
    rng = np.random.default_rng(7)  # 测试内构造用（夹具 hash 纪律不适用于本用例）
    img = rng.uniform(0.0, 1.0, (12, 12, 3)).astype(np.float32)
    img[5, 5] = 8.0  # 盐椒脉冲
    cases = [
        {"kernel_size": 0, "percentile": 0.5, "channel_mode": 0, "amount": 1.0},
        {"kernel_size": 1, "percentile": 0.25, "channel_mode": 0, "amount": 0.7},
        {"kernel_size": 0, "percentile": 1.0, "channel_mode": 1, "amount": 1.0},
    ]
    for params in cases:
        rendered = _render(gl_ctx, fragment, img, params, data["params"])[..., :3]
        oracle_params = {
            "kernel_size": "3x3" if params["kernel_size"] == 0 else "5x5",
            "percentile": params["percentile"],
            "channel_mode": "RGB" if params["channel_mode"] == 0 else "Luma",
            "amount": params["amount"],
        }
        expected = median_rank_ref.evaluate(img, oracle_params)
        np.testing.assert_allclose(
            rendered, expected, atol=6e-4,
            err_msg=f"F19 参数 {params} 像素对拍失败")


# ──────────────────────────────────────────────────────────────────────
# 3. 打包器/uniform 存在性 + 保留字拒绝 + 别名改写（纯逻辑）
# ──────────────────────────────────────────────────────────────────────

def test_packer_uniforms_exist_in_program(gl_ctx) -> None:
    """真实 catalog spec → 打包器 → 编译产物：u_<name> 全部存在且类型可设置。"""
    from src.features.shared.effects.effect_catalog import get_effect_catalog

    spec = get_effect_catalog().get_spec("plugin:LIB1_Gradient_Map")
    assert spec is not None, "LIB1_Gradient_Map 未发布或未注册"
    data = _parse_plugin(STAGING_FILTERS / "gradient_map" / "gradient_map.tglsl")
    fragment = wrap_dctl_process(data["shader_code"], data["params"])
    program = gl_ctx.program(vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=fragment)
    overrides = {"interp_mode": "Smooth", "amount": 0.5}
    packed = EffectPassRunner._pack_plugin_params(spec, overrides)
    for key, value in packed.items():
        assert key in program, f"打包 uniform {key} 未出现在编译产物（被优化或命名不一致）"
    assert packed["u_interp_mode"] == 1  # select label → 索引
    assert packed["u_amount"] == 0.5
    program.release()


def test_reserved_param_name_rejected() -> None:
    src = "// --- Topos Plugin Metadata ---\n// ------\nvec3 process(vec3 c, vec2 uv){ return c; }\n"
    with pytest.raises(ValueError, match="mix"):
        wrap_dctl_process(src, [{"type": "slider", "var_name": "mix", "default": 1.0}])


def test_engine_uniform_alias_rewrite() -> None:
    src = (
        "// --- Topos Plugin Metadata ---\n"
        "// @param slider strength \"S\" 1.0 0.0 2.0\n"
        "// ------\n"
        "vec3 process(vec3 color, vec2 uv) {\n"
        "    vec2 ts = textureSize(u_input_tex, 0);\n"
        "    float aspect = u_aspect_ratio;\n"
        "    vec2 sz = u_tex_size;\n"
        "    return color * strength + ts.x * 0.0 + aspect * 0.0 + sz.y * 0.0;\n"
        "}\n"
    )
    out = wrap_dctl_process(src, [{"type": "slider", "var_name": "strength", "default": 1.0}])
    assert "u_input_tex" not in out and "u_tex_size" not in out and "u_aspect_ratio" not in out
    assert "textureSize(u_texture, 0)" in out
    assert "(u_resolution.x / u_resolution.y)" in out
    assert "u_resolution" in out
    assert "color * u_strength" in out
    assert "uniform float u_strength;" in out


def test_process_signature_param_collision_rejected() -> None:
    src = "// --- Topos Plugin Metadata ---\n// ------\nvec3 process(vec3 color, vec2 uv){ return color; }\n"
    with pytest.raises(ValueError, match="color"):
        wrap_dctl_process(src, [{"type": "slider", "var_name": "color", "default": 1.0}])


# ──────────────────────────────────────────────────────────────────────
# 4. 已发布 LIB1 插件的真实 catalog 注册
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("plugin_id,cost", [
    ("LIB1_Gradient_Map", "low"),
    ("LIB1_Median_Rank", "medium"),
])
def test_published_lib1_plugins_in_catalog(plugin_id: str, cost: str) -> None:
    from src.features.shared.effects.effect_catalog import get_effect_catalog

    spec = get_effect_catalog().get_spec(f"plugin:{plugin_id}")
    assert spec is not None, f"{plugin_id} 未被 EffectCatalog 注册"
    assert spec.param_defs, "参数表为空"
    names = {pd.name for pd in spec.param_defs}
    if plugin_id == "LIB1_Gradient_Map":
        assert {"stop_0", "stop_1", "stop_2", "stop_3", "pos_1", "pos_2",
                "interp_mode", "amount"} <= names
    else:
        assert {"kernel_size", "percentile", "channel_mode", "amount"} <= names
    assert spec.preview_cost == cost
    # @export_support 声明白名单 → gpu_preview + export_opengl
    assert "gpu_preview" in spec.plugin_capabilities
    assert "export_opengl" in spec.plugin_capabilities
    # select 参数进结构键（程序缓存重编译）
    assert "interp_mode" in spec.structural_param_names or "kernel_size" in \
        spec.structural_param_names


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ──────────────────────────────────────────────────────────────────────
# 5. F18 posterize_shared：共享核跨页入口（计划 L / L10）
# ──────────────────────────────────────────────────────────────────────

def test_lib1_posterize_spec_registered_and_packed(gl_ctx) -> None:
    from src.features.shared.effects.effect_catalog import get_effect_catalog

    spec = get_effect_catalog().get_spec("lib1_posterize")
    assert spec is not None, "lib1_posterize 未注册为内置 spec"
    assert spec.preview_cost == "low"
    # 参数 schema 与 Composite PosterizeNode 逐字对齐（漂移钉子）
    import importlib.util
    mod_path = REPO_ROOT / "plugins" / "node_plugins" / "posterize_plugin.py"
    loader = importlib.util.spec_from_file_location("_posterize_plugin", mod_path)
    mod = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(mod)
    node_defs = mod.PosterizeNode.PARAM_DEFS
    assert [(pd.name, pd.ptype, pd.default) for pd in spec.param_defs] ==         [(pd.name, pd.ptype, pd.default) for pd in node_defs]
    # packer 产出与编译产物对齐 + 真实渲染量化语义
    from src.features.shared.effects.effect_pass_runner import (
        _pack_lib1_posterize_params,
    )
    packed = _pack_lib1_posterize_params(
        {"levels": 4, "channel_mode": "rgb", "dither": False})
    assert packed == {"u_levels": 4.0, "u_channel_mode": 0, "u_dither": 0.0}
    program = gl_ctx.program(
        vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=spec.shader_sources[0])
    for key in packed:
        assert key in program
    program.release()


def test_lib1_posterize_pixel_quantization(gl_ctx) -> None:
    """真实渲染：levels=4 把 [0,1] 阶梯量化到 4 档（无抖动）。"""
    from src.features.shared.effects.effect_catalog import get_effect_catalog

    spec = get_effect_catalog().get_spec("lib1_posterize")
    image = np.tile(np.linspace(0.0, 1.0, 16, dtype=np.float32), (4, 1))
    image = np.repeat(image[:, :, None], 3, axis=2)
    rendered = _render(gl_ctx, spec.shader_sources[0], image, {}, [
        {"type": "slider", "var_name": "levels", "default": 4},
        {"type": "select", "var_name": "channel_mode", "default": 0,
         "options": ["rgb", "rgba", "luminance"]},
        {"type": "checkbox", "var_name": "dither", "default": 0},
    ])[..., :3]
    # 量化阶梯：k = floor(c*4+0.5)/4
    expected = np.floor(image * 4.0 + 0.5) / 4.0
    np.testing.assert_allclose(rendered, expected, atol=6e-4)
