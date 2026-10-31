"""Plan-03 batch-2 (PLUG-004) plugin semantic acceptance tests (18 .tglsl).

Review-driven suite (fixes/reviews/f-plug004-review.md C1/C2/C3/I1/I2): the
compile gate proves the 18 plugins parse and compile, but three functional
defects slipped through exactly because no test asserted *behavior* —
C1/C2 sampled with pixel-unit offsets added to normalized UV (both blur
plugins collapsed onto the image border at any real resolution), and C3
declared ``edge_strength`` without ever using it (dead slider). This suite
renders every batch-2 plugin through the real composition pipeline on a
standalone GL 3.3 context and asserts semantics:

1. ``test_*_neutral_bypass_identity`` — rendering with the plugin's neutral
   (no-op) parameters equals rendering with no plugin at all. The shared
   grading path cancels, so this isolates the plugin's bypass contract.
2. ``test_blur_plugins_locality`` — blur-family plugins must not contaminate
   a locally-uniform interior with border texels; the px→UV unit bug (C1/C2)
   pulled every sample from the image border, darkening the whole frame.
   A companion "effect is active" assertion guards against a vacuous pass.
3. ``test_*_slider_responsiveness`` — every slider perturbed 25% of its
   range from an effect-active base must change the rendered output (C3
   family: declared-but-inert parameters).
4. ``test_defaults_render_finite`` — default parameters produce finite
   output (no NaN/Inf) on a feature-rich HDR-ish image.

Pixel_Blocks is exempt from (1): its color quantizer has no exact no-op
parameter pair (min ``color_levels`` is 2), documented per-test.

These tests are read-only wrt src/ and plugins/.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.color.core.plugin_manager import get_plugin_manager  # noqa: E402
from src.features.color.core.shader_compiler import (  # noqa: E402
    ShaderPipeline,
    plugin_binding_from_instance,
)

PLUGIN_DIR = REPO_ROOT / "plugins"

BATCH2_IDS = [
    "Anamorphic_Streak",
    "Bokeh_Defocus",
    "CRT_Scanlines",
    "Channel_Mixer",
    "Duotone_Toner",
    "Edge_Ink_Outline",
    "Glitch_Blocks",
    "Grad_Filter_Exposure",
    "Halftone_Print",
    "Kaleido_Mirror",
    "Lens_Distortion",
    "Light_Leak_Corner",
    "Motion_Blur_Linear",
    "Pixel_Blocks",
    "Tilt_Shift_Focus",
    "Unsharp_Sharpen",
    "Vibrance_Saturation",
    "White_Balance_Advanced",
]

# Neutral (no-op) parameter overrides per plugin. Plugins absent from this
# map have no exact neutral parameterization and are exempt from the bypass
# identity test (Pixel_Blocks: quantizer min level is 2).
NEUTRAL_PARAMS: Dict[str, Dict[str, Any]] = {
    "Anamorphic_Streak": {"intensity": 0.0},
    "Bokeh_Defocus": {"bokeh_radius": 0.0},
    "CRT_Scanlines": {"scan_strength": 0.0, "mask_strength": 0.0},
    "Channel_Mixer": {},  # shipped defaults are the identity matrix
    "Duotone_Toner": {"tone_amount": 0.0},
    "Edge_Ink_Outline": {"edge_strength": 0.0},
    "Glitch_Blocks": {"band_shift": 0.0, "rgb_split": 0.0, "corruption": 0.0},
    "Grad_Filter_Exposure": {"exposure": 0.0},
    "Halftone_Print": {"ink_amount": 0.0},
    "Kaleido_Mirror": {"mirror_mix": 0.0},
    "Lens_Distortion": {"distortion": 0.0},
    "Light_Leak_Corner": {"strength": 0.0},
    "Motion_Blur_Linear": {"motion_length": 0.0},
    "Tilt_Shift_Focus": {"tilt_blur": 0.0},
    "Unsharp_Sharpen": {"sharpen_amount": 0.0},
    "Vibrance_Saturation": {},  # shipped defaults: saturation/vibrance 0
    "White_Balance_Advanced": {},  # shipped defaults: temperature/tint 0
}

# Effect-active base overrides for the slider responsiveness scan. Sliders
# whose influence is gated by another parameter being non-neutral (e.g.
# White_Balance strength scales a zero offset at shipped defaults) need the
# gate opened first, otherwise "perturb slider → output changes" would be
# vacuously false for a healthy plugin.
ACTIVE_BASE: Dict[str, Dict[str, Any]] = {
    "Channel_Mixer": {"green_into_red": 0.3},
    "White_Balance_Advanced": {"temperature": 0.4, "tint": 0.25},
}

IMG_W, IMG_H = 96, 64
# 响应性扫描用高帧：CRT scan_count（120..1200 行）的周期下限钳 1px，
# 在 64px 高的图上整个滑杆区间天然失效（分辨率门控，非死参数）
TALL_W, TALL_H = 128, 900


def _plugin_data() -> Dict[str, Any]:
    pm = get_plugin_manager()
    data = pm.scan_plugins(str(PLUGIN_DIR))
    missing = [pid for pid in BATCH2_IDS if pid not in data]
    assert not missing, f"batch-2 插件未扫描到: {missing}"
    return data


_PLUGIN_DATA = _plugin_data()


def _default_params(plugin_id: str) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for p in _PLUGIN_DATA[plugin_id].get("params", []):
        if p["type"] == "separator":
            continue
        params[p["var_name"]] = p.get("default")
    return params


def _sliders(plugin_id: str) -> list[Tuple[str, float, float, float]]:
    out = []
    for p in _PLUGIN_DATA[plugin_id].get("params", []):
        if p["type"] != "slider":
            continue
        out.append((p["var_name"], float(p["default"]), float(p["min"]), float(p["max"])))
    return out


def _feature_image(w: int = IMG_W, h: int = IMG_H) -> np.ndarray:
    """Feature-rich HDR-ish frame: gradients, a hard edge, a bright spot."""
    x = np.linspace(0.0, 1.0, w, dtype=np.float32)
    y = np.linspace(0.0, 1.0, h, dtype=np.float32)
    xx, yy = np.meshgrid(x, y)
    img = np.zeros((h, w, 3), dtype=np.float32)
    img[..., 0] = 0.1 + 0.7 * xx
    img[..., 1] = 0.1 + 0.7 * yy
    img[..., 2] = 0.2 + 0.5 * np.abs(xx - yy)
    # Hard vertical edge (Sobel food) and a >1.0 HDR hotspot (streak food).
    img[:, w // 2:, 0] = np.float32(0.9)
    img[:, w // 2:, 1] = np.float32(0.15)
    img[:, w // 2:, 2] = np.float32(0.1)
    cx, cy = 24, 20
    r = np.sqrt((xx - x[cx]) ** 2 + (yy - y[cy]) ** 2)
    spot = r < 0.12
    img[spot, :] = np.float32(2.4)  # HDR 超白热点
    img[..., 0] += 0.02 * np.sin(6.0 * xx) * np.cos(5.0 * yy)  # 细节纹理
    return img


def _locality_image() -> np.ndarray:
    """Uniform HDR interior with a thin dark frame: any blur that respects
    pixel-space locality must leave the interior bit-identical-ish, while
    the px→UV unit bug (C1/C2) samples the border from every pixel."""
    img = np.full((IMG_H, IMG_W, 3), 1.2, dtype=np.float32)
    img[:4, :, :] = 0.0
    img[-4:, :, :] = 0.0
    img[:, :4, :] = 0.0
    img[:, -4:, :] = 0.0
    return img


class _Renderer:
    """Minimal composition-pipeline renderer (quad + f32 FBO readback)."""

    def __init__(self, ctx: "moderngl.Context", pipeline: ShaderPipeline) -> None:
        self.ctx = ctx
        self.pipeline = pipeline
        self._vaos: Dict[int, Any] = {}
        quad = np.array([
            -1.0, -1.0, 0.0, 0.0,
            1.0, -1.0, 1.0, 0.0,
            -1.0, 1.0, 0.0, 1.0,
            1.0, 1.0, 1.0, 1.0,
        ], dtype=np.float32)
        self._vbo = ctx.buffer(quad.tobytes())
        self._ibo = ctx.buffer(np.array([0, 1, 2, 2, 1, 3], dtype=np.int32).tobytes())
        self._tex: Optional[Any] = None
        self._fbo: Optional[Any] = None

    def _vao_for(self, prog: Any) -> Any:
        key = id(prog)
        if key not in self._vaos:
            self._vaos[key] = self.ctx.vertex_array(
                prog,
                [(self._vbo, "2f 2f", "in_vert", "in_texcoord")],
                self._ibo,
            )
        return self._vaos[key]

    def render(self, image: np.ndarray, plugin_id: Optional[str],
               params: Optional[Dict[str, Any]] = None) -> np.ndarray:
        h, w = image.shape[:2]
        rgba = np.dstack([image, np.ones((h, w, 1), dtype=np.float32)])
        if self._tex is None or self._tex.size != (w, h):
            if self._tex is not None:
                self._tex.release()
            self._tex = self.ctx.texture((w, h), 4, dtype="f4")
        if self._fbo is None or self._fbo.color_attachments[0].size != (w, h):
            if self._fbo is not None:
                self._fbo.release()
            self._fbo = self.ctx.simple_framebuffer((w, h), dtype="f4")
        self._tex.write(rgba.tobytes())

        if plugin_id is None:
            prog = self.pipeline.build_composition_shader()
        else:
            binding = plugin_binding_from_instance({
                "plugin_id": plugin_id,
                "enabled": True,
                "params": dict(params or {}),
            })
            assert binding is not None, f"{plugin_id} 绑定构造失败"
            prog = self.pipeline.build_composition_shader(plugins=[binding])
            assert prog is not None, f"{plugin_id} composition 编译失败"
            for ub in binding.uniform_manifest(prefix="u_P0_"):
                u = prog.get(ub.uniform_name, None)
                if u is None:
                    continue
                value = (params or {}).get(ub.param_name)
                if value is None:
                    continue
                if ub.glsl_type == "float":
                    u.value = float(value)
                elif ub.glsl_type == "int":
                    u.value = int(value)
                else:  # vec3
                    u.value = tuple(float(c) for c in value)

        tex_u = prog.get("u_input_tex", None)
        if tex_u is not None:
            tex_u.value = 0
        # composition 末段 mix(color, blended, mask * u_opacity)——未设置
        # 的 u_opacity 默认 0 会把整个插件链短路成直通，必须显式置 1
        opacity_u = prog.get("u_opacity", None)
        if opacity_u is not None:
            opacity_u.value = 1.0
        # apply_grading 的缺省 uniform（lift/gamma/gain=0）不是恒等变换
        # （gamma=0 → gm=100 的幂映射）——置恒等集，让插件语义成为唯一变量
        for name, value in (
            ("u_lift", (1.0, 1.0, 1.0)),
            ("u_gamma", (1.0, 1.0, 1.0)),
            ("u_gain", (1.0, 1.0, 1.0)),
        ):
            u = prog.get(name, None)
            if u is not None:
                u.value = value
        self._tex.use(0)
        self._fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        vao = self._vao_for(prog)
        vao.render()
        data = np.frombuffer(
            self._fbo.read(components=4, dtype="f4"), dtype=np.float32)
        return np.flipud(data.reshape(h, w, 4))[..., :3].copy()

    def release(self) -> None:
        for vao in self._vaos.values():
            vao.release()
        if self._tex is not None:
            self._tex.release()
        if self._fbo is not None:
            self._fbo.release()


@pytest.fixture(scope="module")
def renderer():
    ctx = moderngl.create_standalone_context(require=330)
    pipeline = ShaderPipeline(ctx)
    r = _Renderer(ctx, pipeline)
    yield r
    r.release()
    pipeline.release()
    ctx.release()


@pytest.fixture(scope="module")
def feature_img():
    return _feature_image()


@pytest.fixture(scope="module")
def locality_img():
    return _locality_image()


# ── 1. 中性旁路恒等 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("plugin_id", sorted(NEUTRAL_PARAMS))
def test_neutral_bypass_identity(renderer, feature_img, plugin_id):
    """中性（no-op）参数渲染 == 无插件渲染：插件旁路契约（评级路径对消）。"""
    base = _default_params(plugin_id)
    neutral = {**base, **NEUTRAL_PARAMS[plugin_id]}
    with_plugin = renderer.render(feature_img, plugin_id, neutral)
    without = renderer.render(feature_img, None)
    diff = float(np.abs(with_plugin - without).max())
    assert diff < 1e-5, (
        f"{plugin_id} 中性参数应严格旁路（覆盖 {NEUTRAL_PARAMS[plugin_id]}），"
        f"最大偏差 {diff:.3e}"
    )


# ── 2. 模糊类局域性（拦截 C1/C2 的 px→UV 量纲错配） ─────────────────────

@pytest.mark.parametrize("plugin_id,params", [
    ("Bokeh_Defocus", {"bokeh_radius": 1.0}),                      # 圆形光圈
    ("Bokeh_Defocus", {"bokeh_radius": 1.0, "iris_shape": 1}),     # 六边形光圈
    # 焦带压到底部（center=0/宽 0.05）：断言区域处于焦外激活态——若用默认
    # 居中焦带，区域落在焦内 <0.5px 早退旁路里，测试会空转
    ("Tilt_Shift_Focus", {"tilt_blur": 1.0, "focus_center": 0.0, "focus_width": 0.05}),
    ("Motion_Blur_Linear", {"motion_length": 0.05, "motion_angle": 0.0}),
])
def test_blur_plugins_locality(renderer, locality_img, plugin_id, params):
    """局域均匀区域内模糊插件必须保持该区域不变（不得污染边框纹素）。"""
    full = {**_default_params(plugin_id), **params}
    out = renderer.render(locality_img, plugin_id, full)
    ref = renderer.render(locality_img, None)
    region = (slice(16, 48), slice(20, 76))  # 距黑色边框 ≥ 8px 的内部区域
    diff = float(np.abs(out[region] - ref[region]).max())
    assert diff < 5e-3, (
        f"{plugin_id} 在局域均匀区域引入了 {diff:.3e} 偏差——疑似采样偏移"
        "未按纹理尺寸换算（px 直接加到归一化 UV，复查 C1/C2 家族缺陷）"
    )


@pytest.mark.parametrize("plugin_id,params", [
    ("Bokeh_Defocus", {"bokeh_radius": 1.0}),
    ("Tilt_Shift_Focus", {"tilt_blur": 1.0}),
])
def test_blur_effect_active_on_feature_image(renderer, feature_img, plugin_id, params):
    """伴生断言：局域性测试所用参数确实激活了效果（防止空转通过）。"""
    full = {**_default_params(plugin_id), **params}
    out = renderer.render(feature_img, plugin_id, full)
    ref = renderer.render(feature_img, None)
    assert float(np.abs(out - ref).max()) > 1e-3, (
        f"{plugin_id} 全强度参数应产生可见变化——局域性测试疑似空转"
    )


# ── 3. 滑杆响应性（拦截 C3 家族：声明但未接线的参数） ───────────────────

def _perturbed(default: float, lo: float, hi: float) -> float:
    span = hi - lo
    step = 0.25 * span
    value = default + step
    if value > hi:
        value = default - step
    assert lo <= value <= hi and value != default
    return value


@pytest.mark.parametrize("plugin_id", BATCH2_IDS)
def test_slider_responsiveness(renderer, plugin_id):
    tall_img = _feature_image(TALL_W, TALL_H)
    base = {**_default_params(plugin_id), **ACTIVE_BASE.get(plugin_id, {})}
    base_out = renderer.render(tall_img, plugin_id, base)
    sliders = _sliders(plugin_id)
    assert sliders, f"{plugin_id} 应至少声明一个 slider 参数"
    for name, default, lo, hi in sliders:
        value = _perturbed(default, lo, hi)
        perturbed = renderer.render(tall_img, plugin_id, {**base, name: value})
        diff = float(np.abs(perturbed - base_out).max())
        assert diff > 1e-4, (
            f"{plugin_id} 的 slider {name} 扰动到 {value} 输出无变化"
            f"（默认 {default}，区间 [{lo}, {hi}]）——参数声明后未参与运算"
            "（复查 C3 家族缺陷）"
        )


# ── 4. 默认参数有限性 ───────────────────────────────────────────────────

@pytest.mark.parametrize("plugin_id", BATCH2_IDS)
def test_defaults_render_finite(renderer, feature_img, plugin_id):
    out = renderer.render(feature_img, plugin_id, _default_params(plugin_id))
    # 场景线性管线不钳制输出（负值/超白合法），只断言无 NaN/Inf 与量级 sane
    assert np.isfinite(out).all(), f"{plugin_id} 默认参数渲染出现 NaN/Inf"
    assert float(np.abs(out).max()) < 1e4, f"{plugin_id} 输出量级异常 {np.abs(out).max()}"
