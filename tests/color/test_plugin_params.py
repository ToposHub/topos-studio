"""插件参数模型 + capability manifest（P1-COLOR-14/15）测试。

覆盖：
- 解析器：@param color 默认三元组、capability 指令、@description
- format_capability_summary：描述/版本/能力行合成，空数据返回空串
- PluginPanel：color 参数生成 ColorPickerControl（不再是占位文本）、
  确认拾色写回 linear RGB 参数、params_changed/interaction 边界发射、
  分组 tooltip 携带 capability 摘要
"""
import textwrap
from unittest.mock import MagicMock

import pytest
from PyQt6.QtGui import QColor

from src.features.color.core.layer import Layer
from src.features.color.core.plugin_manager import (
    PluginManager, format_capability_summary,
)

_SAMPLE = textwrap.dedent("""\
    // ---
    // @name Highlight Tint
    // @version 1.2
    // @description 高光染色，模拟胶片 halation
    // @input_space scene-linear
    // @output_space scene-linear
    // @alpha premultiplied
    // @precision float16
    // @export_support none
    // @param slider amount "强度" 0.5 0.0 1.0
    // @param color tint_color "高光染色" 1.0 0.9 0.8
    // ---
    vec3 process(vec3 color, vec2 uv) {
        return color;
    }
""")


def _parse(tmp_path, content=_SAMPLE):
    path = tmp_path / "Highlight_Tint.tglsl"
    path.write_text(content, encoding="utf-8")
    manager = PluginManager()
    return manager._parse_plugin_file(str(path), "Highlight_Tint")


# ---------------------------------------------------------------------------
# 解析器（P1-COLOR-15）
# ---------------------------------------------------------------------------

def test_parse_color_param_default(tmp_path):
    plugin = _parse(tmp_path)
    by_var = {p["var_name"]: p for p in plugin["params"]}
    assert by_var["tint_color"]["type"] == "color"
    assert by_var["tint_color"]["default"] == [1.0, 0.9, 0.8]
    assert by_var["amount"]["type"] == "slider"


def test_parse_capabilities_and_description(tmp_path):
    plugin = _parse(tmp_path)
    assert plugin["description"] == "高光染色，模拟胶片 halation"
    caps = plugin["capabilities"]
    assert caps["input_space"] == "scene-linear"
    assert caps["output_space"] == "scene-linear"
    assert caps["alpha"] == "premultiplied"
    assert caps["precision"] == "float16"
    assert caps["export_support"] == "none"
    assert "threading" not in caps  # 未声明的键不出现


def test_parse_capabilities_absent(tmp_path):
    plugin = _parse(tmp_path, content=textwrap.dedent("""\
        // @name Plain
        // @param slider x "X" 0.0 0.0 1.0
        vec3 process(vec3 color, vec2 uv) { return color; }
    """))
    # Phase 7 P7-07：后端声明键缺省兜底（opengl-only / glsl_fragment），
    # 其余键未声明即不出现——旧插件行为不变。
    assert plugin["capabilities"] == {
        "graphics_backends": "opengl",
        "compute_backends": "glsl_fragment",
    }
    assert plugin["description"] == ""


def test_format_capability_summary():
    plugin = {
        "version": "1.2",
        "description": "描述",
        "capabilities": {"input_space": "scene-linear", "export_support": "none"},
    }
    text = format_capability_summary(plugin)
    assert "描述" in text and "Version 1.2" in text
    assert "输入色彩空间: scene-linear" in text
    assert "导出支持: none" in text
    assert format_capability_summary({}) == ""
    assert format_capability_summary({"capabilities": {}}) == ""


# ---------------------------------------------------------------------------
# PluginPanel color 参数（P1-COLOR-14）
# ---------------------------------------------------------------------------

def _make_panel(monkeypatch, params_spec):
    import src.features.color.ui.plugin_panel as plugin_panel_mod
    from src.features.color.ui.plugin_panel import PluginPanel

    plugin_meta = {
        "plugin_id": "fake_plugin",
        "name": "Fake Plugin",
        "type": "dctl",
        "params": params_spec,
        "version": "2.0",
        "description": "测试插件",
        "capabilities": {"input_space": "scene-linear"},
    }
    stub_manager = MagicMock()
    stub_manager.get_plugin.return_value = plugin_meta
    monkeypatch.setattr(plugin_panel_mod, "get_plugin_manager",
                        lambda: stub_manager)

    engine = MagicMock()
    engine.layers = []
    panel = PluginPanel(engine)
    return panel


def _layer_with_color_plugin(panel_engine_unused=None):
    layer = Layer("L")
    # 绕过 PluginManager 单例：直接构造实例结构
    layer.plugins = [{
        "instance_id": "inst-1",
        "plugin_id": "fake_plugin",
        "params": {"tint_color": [0.5, 0.5, 0.5]},
        "enabled": True,
        "type": "dctl",
    }]
    return layer


def _find_picker(panel):
    from src.shared.ui.widgets.color_picker_control import ColorPickerControl
    return panel.findChild(ColorPickerControl)


def test_color_param_renders_picker_not_placeholder(qapp, monkeypatch):
    params_spec = [
        {"var_name": "tint_color", "type": "color", "label": "高光染色",
         "default": [1.0, 0.9, 0.8]},
    ]
    panel = _make_panel(monkeypatch, params_spec)
    layer = _layer_with_color_plugin()
    panel.set_layer(layer)

    picker = _find_picker(panel)
    assert picker is not None, "color 参数应生成 ColorPickerControl"
    # 无占位文本残留
    from PyQt6.QtWidgets import QLabel
    labels = [lbl.text() for lbl in panel.findChildren(QLabel)]
    assert not any("coming soon" in text.lower() for text in labels)
    # 初始值进入控件（QColor 为 8-bit，linear 往返存在量化误差）
    r, g, b, _a = picker.rgba()
    assert r == pytest.approx(0.5, abs=0.01)
    assert g == pytest.approx(0.5, abs=0.01)


def test_color_confirm_writes_linear_params_and_emits(qapp, monkeypatch):
    params_spec = [
        {"var_name": "tint_color", "type": "color", "label": "高光染色",
         "default": [1.0, 0.9, 0.8]},
    ]
    panel = _make_panel(monkeypatch, params_spec)
    layer = _layer_with_color_plugin()
    panel.set_layer(layer)

    events = []
    panel.params_changed.connect(lambda: events.append("params"))
    panel.interaction_started.connect(lambda: events.append("started"))
    panel.interaction_ended.connect(lambda: events.append("ended"))

    picker = _find_picker(panel)
    layer.is_dirty = False
    # 用户在拾色器确认一个 sRGB 灰色（0.5,0.5,0.5）→ linear ≈ 0.214
    picker._commit_color(QColor(127, 127, 127))

    value = layer.plugins[0]["params"]["tint_color"]
    assert isinstance(value, list) and len(value) == 3
    expected = 127 / 255
    expected_linear = ((expected + 0.055) / 1.055) ** 2.4
    assert value[0] == pytest.approx(expected_linear, abs=1e-3)
    assert value[1] == pytest.approx(expected_linear, abs=1e-3)
    assert value[2] == pytest.approx(expected_linear, abs=1e-3)
    assert layer.is_dirty is True
    # 一次提交 = started → params → ended（恰好一次 undo 边界）
    assert events == ["started", "params", "ended"]


def test_color_fallback_to_default_when_missing(qapp, monkeypatch):
    params_spec = [
        {"var_name": "tint_color", "type": "color", "label": "高光染色",
         "default": [0.9, 0.8, 0.7]},
    ]
    panel = _make_panel(monkeypatch, params_spec)
    layer = _layer_with_color_plugin()
    layer.plugins[0]["params"] = {}  # 无当前值 → 回退默认
    panel.set_layer(layer)
    r, g, b, _a = _find_picker(panel).rgba()
    assert r == pytest.approx(0.9, abs=0.01)
    assert g == pytest.approx(0.8, abs=0.01)


def test_group_tooltip_has_capability_summary(qapp, monkeypatch):
    from PyQt6.QtWidgets import QGroupBox

    params_spec = [
        {"var_name": "amount", "type": "slider", "label": "强度",
         "default": 0.5, "min": 0.0, "max": 1.0},
    ]
    panel = _make_panel(monkeypatch, params_spec)
    layer = _layer_with_color_plugin()
    panel.set_layer(layer)

    groups = panel.findChildren(QGroupBox)
    assert groups, "应创建插件分组"
    tips = [g.toolTip() for g in groups]
    assert any("输入色彩空间: scene-linear" in tip and "Version 2.0" in tip
               for tip in tips)


# ---------------------------------------------------------------------------
# P1-COLOR-14 复查：select 回显实例当前值 + 未知类型不静默
# ---------------------------------------------------------------------------

def test_select_echoes_saved_value_not_declared_default(qapp, monkeypatch):
    params_spec = [
        {"var_name": "mode", "type": "select", "label": "模式",
         "options": ["低", "中", "高"], "default": 0},
    ]
    panel = _make_panel(monkeypatch, params_spec)
    layer = Layer("L")
    layer.plugins = [{
        "instance_id": "inst-1", "plugin_id": "fake_plugin",
        "params": {"mode": 2}, "enabled": True, "type": "dctl",
    }]
    panel.set_layer(layer)

    from PyQt6.QtWidgets import QComboBox
    combo = panel.findChild(QComboBox)
    assert combo is not None
    assert combo.currentIndex() == 2  # 回显保存值，而非声明 default=0


def test_unknown_param_type_shows_visible_placeholder(qapp, monkeypatch):
    params_spec = [
        {"var_name": "curve1", "type": "curve", "label": "曲线", "default": 0},
    ]
    panel = _make_panel(monkeypatch, params_spec)
    layer = Layer("L")
    layer.plugins = [{
        "instance_id": "inst-1", "plugin_id": "fake_plugin",
        "params": {"curve1": 0}, "enabled": True, "type": "dctl",
    }]
    panel.set_layer(layer)

    from PyQt6.QtWidgets import QLabel
    labels = [lbl.text() for lbl in panel.findChildren(QLabel)]
    assert any("curve" in text and "unsupported" in text for text in labels)
