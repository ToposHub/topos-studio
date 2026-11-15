"""一级校色扩展参数（Offset 轮 / pivot / Log 四轮 / Lum Mix）的后端契约。

覆盖：
- params_to_offset_rgb 加性域转换（中性/方向/主亮度/往返）
- composition_uniform_values 提取与值域收窄
- CPU 镜像（color_graph_cpu）与 GLSL 同序：Offset → Log 区间 → Lum Mix
- layer 默认值 / reset / has_grading_effects / grade_image 扩展键保留
- engine.update_grading_params 补丁语义（白名单外键拒绝）
- 关键帧白名单登记
- GLSL 源与调用点的参数顺序守卫（防 GLSL/调用点/MSL 三方漂移）
"""
from __future__ import annotations

import numpy as np
import pytest
from unittest.mock import MagicMock

from src.app.controllers.layer_controller import LayerOpsMixin
from src.features.color.core.color_graph_cpu import (
    apply_grading_uniforms_cpu,
)
from src.features.color.core.composition_uniforms import (
    build_composition_uniform_manifest,
    composition_uniform_values,
    offset_chroma_scale,
    offset_master_scale,
    params_to_offset_rgb,
)
from src.features.color.core.image_engine import ImageEngine
from src.features.color.core.keyframe_tracks import GRADE_KEYFRAME_PARAMS
from src.features.color.core.layer import Layer


# ---------------------------------------------------------------------------
# params_to_offset_rgb
# ---------------------------------------------------------------------------

def test_params_to_offset_rgb_neutral_is_zero() -> None:
    assert params_to_offset_rgb(0.0, 0.0, 0.0) == [0.0, 0.0, 0.0]


def test_params_to_offset_rgb_chroma_direction_and_master() -> None:
    # 纯红满偏（b=0）：色度方向 (2,-1,-1) × CHROMA_SCALE
    red = params_to_offset_rgb(0.0, 1.0, 0.0)
    assert red == pytest.approx([2.0 * offset_chroma_scale(),
                                 -1.0 * offset_chroma_scale(),
                                 -1.0 * offset_chroma_scale()])
    # 主亮度线性平移全部通道
    lifted = params_to_offset_rgb(0.0, 0.0, 0.5)
    assert lifted == pytest.approx([0.5, 0.5, 0.5])
    # b 超界被收窄到 [-1, 1]
    assert params_to_offset_rgb(0.0, 0.0, 5.0) == pytest.approx([1.0, 1.0, 1.0])
    assert params_to_offset_rgb(0.0, 0.0, -5.0) == pytest.approx([-1.0, -1.0, -1.0])


def test_offset_display_roundtrip_via_hsb() -> None:
    """UI 显示单位（×100）↔ (h, s, b) 精确往返（正向模型可达的组合）。"""
    from src.features.color.ui.color_wheel import (
        offset_display_to_hsb, offset_hsb_to_display,
    )
    for display in [(40.0, 0.0, 0.0), (0.0, -25.0, 25.0),
                    (-100.0, -100.0, -100.0), (50.0, -25.0, -25.0)]:
        hue, sat, bright = offset_display_to_hsb(*display)
        rx, gy, bz = offset_hsb_to_display(hue, sat, bright)
        assert rx == pytest.approx(display[0], abs=1e-2)
        assert gy == pytest.approx(display[1], abs=1e-2)
        assert bz == pytest.approx(display[2], abs=1e-2)


# ---------------------------------------------------------------------------
# composition_uniform_values 提取
# ---------------------------------------------------------------------------

def test_uniform_values_defaults_for_extended_params() -> None:
    layer = Layer("t")
    values = composition_uniform_values(layer, width=8, height=8)
    assert values["u_offset"] == (0.0, 0.0, 0.0)
    assert values["u_pivot"] == pytest.approx(0.18)
    assert values["u_log_shadow"] == (0.0, 0.0, 0.0)
    assert values["u_log_midtone"] == (0.0, 0.0, 0.0)
    assert values["u_log_highlight"] == (0.0, 0.0, 0.0)
    assert values["u_log_offset"] == (0.0, 0.0, 0.0)
    assert values["u_log_low_range"] == pytest.approx(0.333)
    assert values["u_log_high_range"] == pytest.approx(0.550)
    assert values["u_lum_mix"] == pytest.approx(100.0)


def test_uniform_values_clamp_pivot_and_lum_mix() -> None:
    layer = Layer("t")
    layer.grading_params["pivot"] = 0.0   # 极端值：提取时收窄到 0.05
    layer.grading_params["lum_mix"] = 500
    values = composition_uniform_values(layer, width=8, height=8)
    assert values["u_pivot"] == pytest.approx(0.05)
    assert values["u_lum_mix"] == pytest.approx(100.0)


def test_uniform_values_zero_lum_mix_is_preserved() -> None:
    """lum_mix=0 是合法值（完全恢复原亮度），不能用 ``or`` 惯用法吞掉。"""
    layer = Layer("t")
    layer.grading_params["lum_mix"] = 0
    values = composition_uniform_values(layer, width=8, height=8)
    assert values["u_lum_mix"] == 0.0


def test_manifest_layout_appends_without_shifting_existing_uniforms() -> None:
    manifest = build_composition_uniform_manifest()
    # 扩展 uniform 追加在清单末尾：既有 uniform 的 MSL 偏移保持不变
    assert manifest.decl("u_gain_saturation").offset \
        < manifest.decl("u_offset").offset
    assert manifest.decl("u_offset").glsl_type == "vec3"
    assert manifest.decl("u_pivot").glsl_type == "float"
    assert manifest.decl("u_lum_mix").glsl_type == "float"
    msl_struct = manifest.to_msl_struct("CompositionParams")
    for field in ("u_offset_x", "u_pivot", "u_log_shadow_z", "u_lum_mix"):
        assert field in msl_struct


# ---------------------------------------------------------------------------
# CPU 镜像（与 GLSL apply_grading 同序）
# ---------------------------------------------------------------------------

def _img(value: float = 0.4) -> np.ndarray:
    return np.full((2, 2, 3), value, dtype=np.float32)


def test_cpu_offset_wheel_moves_channels_additively() -> None:
    layer = Layer("t")
    layer.grading_params["offset"] = (0.0, 1.0, 0.0)
    values = composition_uniform_values(layer, width=8, height=8)
    out = apply_grading_uniforms_cpu(_img(0.4), values)
    assert out[0, 0] == pytest.approx([0.9, 0.15, 0.15], abs=1e-5)


def test_cpu_log_wheels_are_zone_weighted() -> None:
    layer = Layer("t")
    layer.grading_params["log_shadow"] = (0.0, 1.0, 0.0)
    values = composition_uniform_values(layer, width=8, height=8)
    dark = apply_grading_uniforms_cpu(
        np.array([[[0.02, 0.02, 0.02]]], dtype=np.float32), values)
    bright = apply_grading_uniforms_cpu(
        np.array([[[0.9, 0.9, 0.9]]], dtype=np.float32), values)
    assert dark[0, 0, 0] > 0.02  # 暗部被推红
    assert bright[0, 0] == pytest.approx([0.9, 0.9, 0.9], abs=1e-5)  # 亮部不动


def test_cpu_log_offset_wheel_is_uniform() -> None:
    layer = Layer("t")
    layer.grading_params["log_offset"] = (0.0, 0.0, -1.0)  # 主亮度 -1 → 全通道 -1
    values = composition_uniform_values(layer, width=8, height=8)
    out = apply_grading_uniforms_cpu(_img(0.6), values)
    assert out[0, 0] == pytest.approx([-0.4, -0.4, -0.4], abs=1e-5)


def test_cpu_lum_mix_zero_restores_original_luma() -> None:
    layer = Layer("t")
    layer.grading_params["saturation"] = 60
    layer.grading_params["lum_mix"] = 0
    values = composition_uniform_values(layer, width=8, height=8)
    source = np.array([[[0.3, 0.5, 0.7]]], dtype=np.float32)
    out = apply_grading_uniforms_cpu(source, values)
    luma_weights = np.array([0.299, 0.587, 0.114], np.float32)
    original_luma = float((source @ luma_weights)[0, 0])
    graded_luma = float((out @ luma_weights)[0, 0])
    assert graded_luma == pytest.approx(original_luma, abs=1e-5)


def test_cpu_pivot_matches_legacy_hardcoded_018() -> None:
    """pivot 默认 0.18 时与旧硬编码渲染逐位一致（既有工程不变）。"""
    layer = Layer("t")
    layer.grading_params["contrast"] = 40
    values = composition_uniform_values(layer, width=8, height=8)
    source = np.array([[[0.05, 0.3, 1.4]]], dtype=np.float32)
    out = apply_grading_uniforms_cpu(source, values)
    expected = (np.sign(source)
                * np.power(np.abs(source) / 0.18, 1.4) * 0.18).astype(np.float32)
    np.testing.assert_allclose(out, expected, rtol=1e-6)


def test_cpu_pivot_parameter_changes_contrast_center() -> None:
    layer = Layer("t")
    layer.grading_params["contrast"] = 40
    layer.grading_params["pivot"] = 0.4
    values = composition_uniform_values(layer, width=8, height=8)
    source = np.array([[[0.05, 0.3, 1.4]]], dtype=np.float32)
    out = apply_grading_uniforms_cpu(source, values)
    expected = (np.sign(source)
                * np.power(np.abs(source) / 0.4, 1.4) * 0.4).astype(np.float32)
    np.testing.assert_allclose(out, expected, rtol=1e-6)


def test_cpu_default_layer_is_identity() -> None:
    values = composition_uniform_values(Layer("t"), width=8, height=8)
    source = np.array([[[0.1, 0.5, 0.9], [1.2, -0.1, 0.4]]], dtype=np.float32)
    out = apply_grading_uniforms_cpu(source, values)
    np.testing.assert_allclose(out, source, rtol=1e-6, atol=1e-6)


# ---------------------------------------------------------------------------
# layer / engine 契约
# ---------------------------------------------------------------------------

def test_layer_defaults_and_reset_include_extended_params() -> None:
    layer = Layer("t")
    layer.grading_params["offset"] = (10, 20, 30)
    layer.grading_params["lum_mix"] = 40
    layer.reset_params()
    assert layer.grading_params["offset"] == (0, 0, 0)
    assert layer.grading_params["pivot"] == pytest.approx(0.18)
    assert layer.grading_params["lum_mix"] == 100
    assert layer.grading_params["log_low_range"] == pytest.approx(0.333)


def test_has_grading_effects_detects_extended_params() -> None:
    layer = Layer("t")
    assert not layer.has_grading_effects()
    layer.grading_params["offset"] = (0, 1, 0)
    assert layer.has_grading_effects()
    layer.reset_params()
    layer.grading_params["lum_mix"] = 50
    assert layer.has_grading_effects()


def test_grade_image_preserves_extended_keys(image_engine) -> None:
    layer = image_engine.get_active_layer()
    layer.grading_params["offset"] = (0.0, 0.5, 0.0)
    layer.grading_params["pivot"] = 0.3
    image_engine.grade_image(*(((0, 0, 0),) * 3 + (0.0,) * 9))
    assert layer.grading_params["offset"] == (0.0, 0.5, 0.0)
    assert layer.grading_params["pivot"] == pytest.approx(0.3)


def test_update_grading_params_merges_whitelisted_keys_only(image_engine) -> None:
    engine: ImageEngine = image_engine
    layer = engine.get_active_layer()
    engine.update_grading_params({"offset": (0.0, 0.8, 0.1), "pivot": 0.25,
                                  "lum_mix": 50})
    assert layer.grading_params["offset"] == (0.0, 0.8, 0.1)
    assert layer.grading_params["pivot"] == pytest.approx(0.25)
    assert layer.grading_params["lum_mix"] == 50
    # 白名单外键一律拒绝（全量键必须走 grade_image）
    before = layer.grading_params["contrast"]
    engine.update_grading_params({"contrast": 99})
    assert layer.grading_params["contrast"] == before
    # 未知键静默忽略
    engine.update_grading_params({"not_a_key": 1})
    assert "not_a_key" not in layer.grading_params


# ---------------------------------------------------------------------------
# 关键帧白名单
# ---------------------------------------------------------------------------

def test_extended_params_registered_as_animatable() -> None:
    assert GRADE_KEYFRAME_PARAMS["offset"] == "vec3"
    assert GRADE_KEYFRAME_PARAMS["log_shadow"] == "vec3"
    assert GRADE_KEYFRAME_PARAMS["log_midtone"] == "vec3"
    assert GRADE_KEYFRAME_PARAMS["log_highlight"] == "vec3"
    assert GRADE_KEYFRAME_PARAMS["log_offset"] == "vec3"
    assert GRADE_KEYFRAME_PARAMS["pivot"] == "scalar"
    assert GRADE_KEYFRAME_PARAMS["log_low_range"] == "scalar"
    assert GRADE_KEYFRAME_PARAMS["log_high_range"] == "scalar"
    assert GRADE_KEYFRAME_PARAMS["lum_mix"] == "scalar"


# ---------------------------------------------------------------------------
# GLSL / 调用点 / MSL 顺序守卫
# ---------------------------------------------------------------------------

def test_glsl_snippet_signature_and_call_site_stay_in_sync() -> None:
    from src.features.color.core.shader_nodes import GRADING_SNIPPET

    call = ("apply_grading(graded, u_lift, u_gamma, u_gain, u_exposure, "
            "u_temp, u_tint, u_contrast, u_saturation, u_vibrance, u_shadow, "
            "u_highlights, u_mid_detail, u_sharpness, u_hue_shift, "
            "u_lift_saturation, u_gamma_saturation, u_gain_saturation, "
            "u_pivot, u_offset, u_log_shadow, u_log_midtone, u_log_highlight, "
            "u_log_offset, u_log_low_range, u_log_high_range, u_lum_mix)")
    # 调用点字符串逐字包含（shader_compiler 拼接文本）
    import inspect
    from src.features.color.core import shader_compiler
    source = inspect.getsource(shader_compiler)
    assert call in source
    # GLSL 签名（跳过首参 color）与调用点实参（跳过 graded、去掉 u_ 前缀）一一对应
    signature = GRADING_SNIPPET.split("apply_grading(", 1)[1].split(")", 1)[0]
    glsl_names = [p.strip().split()[-1] for p in signature.split(",")][1:]
    call_args = [a.strip() for a in
                 call.split("(", 1)[1].rstrip(");").split(",")][1:]
    assert glsl_names == [name[2:] for name in call_args]
    # 新数学位次：Offset/Log 在 Shadows 之前，Lum Mix 在 Saturation 之后
    body = GRADING_SNIPPET
    assert body.index("color += offset;") < body.index("// 5. Shadows")
    assert body.index("// 4.6 Log") < body.index("// 5. Shadows")
    assert body.index("// 11. Lum Mix") > body.index("// 10. Saturation")


def test_msl_grading_mirrors_glsl_order() -> None:
    from src.features.color.core.shader_nodes_msl import MSL_GRADING
    assert MSL_GRADING.index("color += offset;") < MSL_GRADING.index("// 5. Shadows")
    assert MSL_GRADING.index("// 11. Lum Mix") > MSL_GRADING.index("// 10. Saturation")
    assert "p.u_pivot" in MSL_GRADING
    assert "p.u_lum_mix" in MSL_GRADING


# ---------------------------------------------------------------------------
# 端到端链路：面板 → LayerOpsMixin → 引擎 / 关键帧 / ColorStack 回填
# ---------------------------------------------------------------------------

class _Host(LayerOpsMixin):
    """LayerOpsMixin 最小宿主（on_grading_extended_changed 路由测试）。"""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.request_render = MagicMock()
        self._invalidate_video_render_params_if_playing = MagicMock()
        self._refresh_grade_keyframe_ui = MagicMock()

    def _is_video_playing(self) -> bool:
        return False


def test_extended_signal_reaches_engine_via_controller(qapp, image_engine) -> None:
    from src.features.color.ui.color_wheel import GradingPanel

    panel = GradingPanel()
    host = _Host(image_engine)
    panel.grading_extended_changed.connect(host.on_grading_extended_changed)
    try:
        image_engine.img_full_original = np.full((2, 2, 3), 0.5, np.float32)
        panel.show()
        qapp.processEvents()
        panel.offset_wheel._r_drag.setValue(50.0)
        panel.offset_wheel.flush_pending_changes()
        qapp.processEvents()
        layer = image_engine.get_active_layer()
        stored = layer.grading_params["offset"]
        assert stored[1] > 0.0  # R 框 +50 → 色度方向（sat > 0）
        assert stored[2] > 0.0  # 主亮度分量同步抬升
        host.request_render.assert_called_once_with()
    finally:
        panel.close()
        panel.deleteLater()


def test_extended_params_recorded_as_grade_keyframes(qapp) -> None:
    from types import SimpleNamespace

    from src.features.color.ui.color_wheel import GradingPanel

    layer = SimpleNamespace(grade_keyframes={})
    engine = SimpleNamespace(
        current_grade_frame=12,
        get_current_image=lambda: np.zeros((2, 2, 3), np.float32),
        get_active_layer=lambda: layer,
        record_grade_keyframe=MagicMock(return_value=True),
        update_grading_params=MagicMock(),
    )
    host = _Host(engine)
    host.grading_panel = SimpleNamespace(keyframe_mode=True)
    host.on_grading_extended_changed({
        "offset": (0.0, 0.5, 0.0), "pivot": 0.25, "lum_mix": 80,
    })
    recorded = {call.args[1]: call.args[2]
                for call in engine.record_grade_keyframe.call_args_list}
    assert recorded["offset"] == (0.0, 0.5, 0.0)
    assert recorded["pivot"] == pytest.approx(0.25)
    assert recorded["lum_mix"] == pytest.approx(80)


def test_extended_params_survive_stack_roundtrip(qapp, image_engine) -> None:
    """engine → ColorStack → hydrator 回填：扩展参数无损往返到新面板。"""
    from src.features.color.adapters.color_stack_hydrator import ColorStackHydrator
    from src.features.color.ui.color_wheel import GradingPanel
    from src.shared.project.color_stack import ColorStackData

    source = GradingPanel()
    target = GradingPanel()
    panels = {"grading_panel": source}
    try:
        source.show()
        qapp.processEvents()
        source.offset_wheel._r_drag.setValue(40.0)
        source.offset_wheel.flush_pending_changes()
        source.pivot_slider.setValue(0.3)
        source.lum_mix_slider.setValue(60)
        source.log_midtone_wheel._g_drag.setValue(25.0)
        source.log_midtone_wheel.flush_pending_changes()

        layer = image_engine.get_active_layer()
        layer.grading_params.update(source.get_current_params())
        hydrator = ColorStackHydrator()
        stack = hydrator.collect_stack_from_engine_and_panels(
            "clip-a", image_engine, panels)
        restored = ColorStackData.from_dict(stack.to_dict())

        target.show()
        qapp.processEvents()
        hydrator.apply_stack_to_panels(restored, {"grading_panel": target})
        assert target.get_current_params()["offset"] == \
            source.get_current_params()["offset"]
        assert target.pivot_slider.value() == pytest.approx(0.3)
        assert target.lum_mix_slider.value() == pytest.approx(60)
        assert target.get_current_params()["log_midtone"][1] == \
            source.get_current_params()["log_midtone"][1]
        assert target.color_bars_panel.offset._nums["R"].value() == \
            source.color_bars_panel.offset._nums["R"].value()
    finally:
        source.close()
        source.deleteLater()
        target.close()
        target.deleteLater()
