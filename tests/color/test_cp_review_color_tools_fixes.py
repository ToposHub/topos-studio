"""CP-复查（调色页全量工具审查）修复的回归测试。

覆盖 2026-10-14 批次确认的缺陷：
- 限定器吸管 HSL/HSV 语义统一（canvas 取色改输出 HSV）
- 裁切 Custom 宽高比解析（此前 "Custom" 字符串解析必抛错退化为 Free）
- 模糊/锐化禁用分支保留滑条值（此前禁用即清零 + 整表覆盖 → 重载丢失）
- v1 调色栈回退先整体重置（此前跨 clip 泄漏蒙版/限定器/插件状态）
- ColorStackData.with_* 快照冻结（deepcopy 脱钩活对象，保撤销基线）
- Layer.reset_params 清空插件实例
- MixerPanel reset 深拷贝发射 + interaction 对（可撤销）
- HSL 曲线控件空白点击不再发 interaction_started（撤销污染/idle 停摆）
- lut_loader 非单位 DOMAIN 重采样到 [0,1]
"""
import sys
import os
import types
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytestmark = pytest.mark.color_core


# ---------------------------------------------------------------------------
# 限定器吸管：HSV 语义
# ---------------------------------------------------------------------------

@pytest.fixture
def qualifier_panel(qapp):
    from src.features.color.ui.qualifier_panel import QualifierPanel
    p = QualifierPanel()
    yield p
    p.deleteLater()


def _hsv_of(rgb):
    """与 shader rgb_to_qualifier_hsv 完全一致的换算（回归基准）。"""
    c = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, None)
    c_max, c_min = float(c.max()), float(c.min())
    delta = c_max - c_min
    s = delta / c_max if c_max > 1e-5 else 0.0
    if delta > 1e-5:
        if c[0] == c_max:
            h = (c[1] - c[2]) / delta + (6.0 if c[1] < c[2] else 0.0)
        elif c[1] == c_max:
            h = (c[2] - c[0]) / delta + 2.0
        else:
            h = (c[0] - c[1]) / delta + 4.0
        h /= 6.0
    else:
        h = 0.0
    return h % 1.0, min(s, 1.0), min(c_max, 1.0)


def test_eyedropper_normal_reset_uses_hsv_value(qualifier_panel):
    """高饱和色拾取后 Value 必须落在 lum 区间内（HSL 时代 v≈2l 会落在区间外）。"""
    rgb = (1.0, 0.5, 0.25)  # c_max=1, c_min=0.25, delta=0.75
    h, s, v = _hsv_of(rgb)
    # HSL 亮度 l=0.625，旧实现的 lum_high=0.825 < v=1.0 → 拾取色选不中
    assert v == 1.0

    qualifier_panel.current_picker_mode = 'normal'
    qualifier_panel.process_picked_color(h, s, v, reset_ranges=True)

    p = qualifier_panel.params
    assert p["lum_low"] <= v <= p["lum_high"]
    assert p["sat_low"] <= s <= p["sat_high"]
    assert abs(p["sat_high"] - min(1.0, s + 0.2)) < 1e-9
    assert abs(p["lum_high"] - min(1.0, v + 0.2)) < 1e-9


def test_eyedropper_add_mode_expands_to_hsv_sat(qualifier_panel):
    rgb = (0.9, 0.1, 0.1)
    h, s, v = _hsv_of(rgb)
    qualifier_panel.current_picker_mode = 'add'
    qualifier_panel.process_picked_color(h, s, v)
    p = qualifier_panel.params
    assert p["sat_low"] <= s <= p["sat_high"]
    assert p["lum_low"] <= v <= p["lum_high"]


# ---------------------------------------------------------------------------
# 裁切 Custom 宽高比
# ---------------------------------------------------------------------------

class _FakeCropItem:
    def __init__(self):
        self.aspect_ratio = None
        self.rotation_val = 0.0
        self._handle = types.SimpleNamespace(pos=lambda: (0.0, 0.0))
        self.handles = {'br': self._handle}

    def _apply_crop_constraints(self):
        pass

    def on_handle_moved(self, *args):
        pass


def _canvas_stub():
    return types.SimpleNamespace(
        crop_item=_FakeCropItem(),
        logical_size=None,
        engine=None,
        viewport=lambda: types.SimpleNamespace(update=lambda: None),
    )


def test_crop_custom_aspect_applies_ratio(qapp):
    from src.features.color.ui.canvas import Canvas
    stub = _canvas_stub()
    Canvas.set_crop_params(
        stub, {"aspect": "Custom", "custom_width": 1920, "custom_height": 1080})
    assert abs(stub.crop_item.aspect_ratio - (1920 / 1080)) < 1e-9


def test_crop_custom_missing_fields_falls_back_to_free(qapp):
    from src.features.color.ui.canvas import Canvas
    stub = _canvas_stub()
    # set_crop_aspect 的 legacy 路径只带 aspect 字符串
    Canvas.set_crop_params(stub, {"aspect": "Custom"})
    assert stub.crop_item.aspect_ratio is None


def test_crop_numeric_aspect_still_parses(qapp):
    from src.features.color.ui.canvas import Canvas
    stub = _canvas_stub()
    Canvas.set_crop_params(stub, {"aspect": "16:9"})
    assert abs(stub.crop_item.aspect_ratio - 16 / 9) < 1e-9


# ---------------------------------------------------------------------------
# 模糊/锐化：禁用不清零
# ---------------------------------------------------------------------------

@pytest.fixture
def blur_panel(qapp, monkeypatch):
    from src.features.color.ui.blur_sharpen_panel import BlurSharpenPanel
    # ConfigManager 取默认值路径打桩，避免读用户配置
    from src.shared.config.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "get", lambda self, k, d=None: d)
    p = BlurSharpenPanel()
    yield p
    p.deleteLater()


def test_blur_sharpen_disabled_keeps_slider_values(blur_panel):
    blur_panel.sharpen_slider.setValue(40.0)
    blur_panel.radius_slider.setValue(3.0)
    blur_panel.blur_intensity_slider.setValue(70.0)
    blur_panel.enable_cb.setChecked(False)
    blur_panel.blur_enable_cb.setChecked(False)

    d = blur_panel.get_params_dict()
    assert d["enabled"] is False
    assert d["blur_enabled"] is False
    # 数值保留：重新打开开关 / 保存重载后滑条不归零
    assert d["sharpen"] == 40.0
    assert d["radius"] == 3.0
    assert d["texture_soften_intensity"] == 70.0
    assert d["fast_blur_radius"] == blur_panel.blur_fast_radius_slider.value()


def test_blur_set_params_mode_aware_fallback(blur_panel):
    """旧工程通用键按 blur_mode 分派（Fast 模式半径不再灌给光学模糊）。"""
    blur_panel.set_params_dict({
        "blur_enabled": True, "blur_mode": 3,
        "blur_intensity": 55.0, "blur_radius": 8.0,
    }, emit=False)
    assert blur_panel.blur_fast_intensity_slider.value() == 55.0
    assert blur_panel.blur_fast_radius_slider.value() == 8.0
    assert blur_panel.blur_radius_slider.value() == 0.0


# ---------------------------------------------------------------------------
# v1 栈回退：整体重置
# ---------------------------------------------------------------------------

class _FakeV1Engine:
    def __init__(self, layers):
        self.layers = layers
        self.tracking_store = None


def test_v1_stack_fallback_resets_layer_leftovers():
    from src.features.color.adapters.color_stack_hydrator import (
        ColorStackHydrator,
    )
    from src.features.color.core.layer import Layer
    from src.shared.project.color_stack import ColorStackData

    layer = Layer("Leaked")
    layer.mask_shapes.append(layer.add_shape("circle"))
    layer.qualifier_params["hue_center"] = 0.33
    layer.plugins.append({"plugin_id": "x", "enabled": True})
    layer.grade_keyframes["saturation"] = {"0": 0.5}
    layer.warper_params = np.ones((6, 6, 2), dtype=np.float32)

    stack = ColorStackData.create(asset_id="a").with_grading_params(
        {"exposure": 1.0})
    ColorStackHydrator().apply_stack_to_engine(stack, _FakeV1Engine([layer]))

    # 平铺键写入
    assert layer.grading_params["exposure"] == 1.0
    # 上一剪辑残留整体清除
    assert layer.mask_shapes == []
    assert layer.qualifier_params["hue_center"] == 0.5  # 默认值
    assert layer.plugins == []
    assert layer.grade_keyframes == {}
    assert layer.warper_params is None


def test_layer_reset_params_clears_plugins():
    from src.features.color.core.layer import Layer

    layer = Layer("L")
    layer.plugins.append({"plugin_id": "x", "enabled": True})
    layer.reset_params()
    assert layer.plugins == []


# ---------------------------------------------------------------------------
# 快照冻结（with_* deepcopy）
# ---------------------------------------------------------------------------

def test_with_layers_freezes_snapshot():
    from src.shared.project.color_stack import ColorStackData

    stack = ColorStackData.create(asset_id="a")
    grading = {"exposure": 0.0}
    layer_snap = {"grading_params": grading, "mask_shapes": [{"id": 1}]}

    frozen = stack.with_layers([layer_snap])
    grading["exposure"] = 9.0
    layer_snap["mask_shapes"].append({"id": 2})

    assert frozen.layers[0]["grading_params"]["exposure"] == 0.0
    assert len(frozen.layers[0]["mask_shapes"]) == 1


def test_with_qualifier_style_params_freeze():
    from src.shared.project.color_stack import ColorStackData

    stack = ColorStackData.create(asset_id="a")
    soft = {"enabled": False, "high": 1.0}
    frozen = stack.with_soft_clip_params(soft)
    soft["enabled"] = True
    assert frozen.soft_clip_params["enabled"] is False


# ---------------------------------------------------------------------------
# MixerPanel reset：深拷贝 + interaction 对
# ---------------------------------------------------------------------------

@pytest.fixture
def mixer_panel(qapp):
    from src.features.color.ui.mixer_panel import MixerPanel
    p = MixerPanel()
    yield p
    p.deleteLater()


def test_mixer_reset_all_emits_copy_and_interaction_pair(mixer_panel):
    started, ended, emitted = [], [], []
    mixer_panel.interaction_started.connect(lambda: started.append(1))
    mixer_panel.interaction_ended.connect(lambda: ended.append(1))
    mixer_panel.mixer_changed.connect(lambda params: emitted.append(params))

    mixer_panel.params["red"][0] = 50
    mixer_panel.reset_all()

    assert len(started) == 1 and len(ended) == 1
    assert len(emitted) == 1
    # 深拷贝：面板后续拖动不得穿透到已发射的 dict
    assert emitted[0] is not mixer_panel.params
    assert emitted[0]["red"] == [0, 0, 0]


def test_mixer_reset_all_noop_when_already_default(mixer_panel):
    started = []
    mixer_panel.interaction_started.connect(lambda: started.append(1))
    mixer_panel.reset_all()  # 已是默认：不构成可撤销交互
    assert started == []


def test_mixer_interaction_ended_flushes_before_emit(mixer_panel):
    """连接顺序契约：ended 触发时尾值必须已先到达（changed → ended）。"""
    order = []
    mixer_panel.mixer_changed.connect(lambda p: order.append("changed"))
    mixer_panel.interaction_ended.connect(lambda: order.append("ended"))

    from src.shared.ui.widgets.param_slider_row import ParamSliderRow
    row = None
    for tab in (mixer_panel.hue_tab, mixer_panel.sat_tab, mixer_panel.lum_tab):
        rows = tab.findChildren(ParamSliderRow)
        if rows:
            row = rows[0]
            break
    assert row is not None

    # 拖动尾值滞留在 16ms 节流里，release 只触发 row 的 interactionEnded
    mixer_panel.update_param("red", 0, 33)
    row.interactionEnded.emit()

    assert order == ["changed", "ended"]
    mixer_panel._flush_pending_params()  # 清场（若有残留 pending）


# ---------------------------------------------------------------------------
# HSL 曲线控件：手势对称
# ---------------------------------------------------------------------------

@pytest.fixture
def hsl_widget(qapp):
    from src.shared.ui.widgets.hsl_curves_widget import HSLCurvesWidget
    w = HSLCurvesWidget()
    yield w
    w.deleteLater()


def _left_press(widget, x, y):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    widget.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _left_release(widget, x, y):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    widget.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def test_hsl_empty_click_does_not_start_interaction(hsl_widget):
    started, ended = [], []
    hsl_widget.interaction_started.connect(lambda: started.append(1))
    hsl_widget.interaction_ended.connect(lambda: ended.append(1))

    # 画布左上角远端的空白（padding 区域外）
    _left_press(hsl_widget, 1.0, 1.0)
    _left_release(hsl_widget, 1.0, 1.0)
    assert started == [] and ended == []

    # draw rect 内的空点 → 插入控制点 = 一次完整交互
    rect = hsl_widget.get_draw_rect()
    inside_x = rect.center().x()
    inside_y = rect.center().y() - 30  # 避开默认水平线（y=127.5 映射处）
    _left_press(hsl_widget, inside_x, inside_y)
    _left_release(hsl_widget, inside_x, inside_y)
    assert len(started) == 1 and len(ended) == 1


def test_hsl_reset_all_curves_emits_interaction_pair(hsl_widget):
    from PyQt6.QtCore import QPointF
    started, ended = [], []
    hsl_widget.interaction_started.connect(lambda: started.append(1))
    hsl_widget.interaction_ended.connect(lambda: ended.append(1))

    hsl_widget.curves["LumSat"] = [QPointF(0, 30), QPointF(255, 30)]
    hsl_widget.reset_all_curves(emit_changed=True)
    assert len(started) == 1 and len(ended) == 1

    # 已是默认时再重置：不产生交互对
    hsl_widget.reset_all_curves(emit_changed=True)
    assert len(started) == 1 and len(ended) == 1


# ---------------------------------------------------------------------------
# lut_loader：非单位 DOMAIN 重采样
# ---------------------------------------------------------------------------

def _domain_cube_text(dmin, dmax):
    """线性放大 2 倍的 2³ cube，DOMAIN=[dmin, dmax]。"""
    lines = [f"LUT_3D_SIZE 2",
             f"DOMAIN_MIN {dmin} {dmin} {dmin}",
             f"DOMAIN_MAX {dmax} {dmax} {dmax}"]
    for b in (0.0, 1.0):
        for g in (0.0, 1.0):
            for r in (0.0, 1.0):
                lines.append(f"{r} {g} {b}")
    return "\n".join(lines)


def test_resample_to_unit_domain_identity_when_unit():
    from src.shared.color.lut_loader import parse_cube, resample_to_unit_domain
    lut = parse_cube(_domain_cube_text(0.0, 1.0))
    assert resample_to_unit_domain(lut) is lut


def test_resample_to_unit_domain_matches_cpu_sampler():
    from src.shared.color.lut_loader import (
        parse_cube, resample_to_unit_domain, sample_lut_trilinear,
    )
    lut = parse_cube(_domain_cube_text(0.0, 2.0))
    rs = resample_to_unit_domain(lut)
    assert rs.domain_max == (1.0, 1.0, 1.0)

    # GPU [0,1] 采样 u 处的值 == CPU 采样 domain 输入 (u*2) 处的值
    for u in (0.0, 0.25, 0.5, 0.75, 1.0):
        gpu_side = sample_lut_trilinear(rs, np.array([[u, u, u]]))[0]
        cpu_side = sample_lut_trilinear(lut, np.array([[u * 2.0] * 3]))[0]
        assert np.allclose(gpu_side, cpu_side, atol=1e-6), (u, gpu_side, cpu_side)
