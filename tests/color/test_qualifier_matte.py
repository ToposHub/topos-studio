"""P1-COLOR-08: Qualifier 专业 matte 工作流测试。

覆盖：
- qualifier 参数模型（denoise 默认值 / 序列化往返）
- matte 缓存键契约（matte_cache_hash 对所有内容输入敏感）
- matte 预览三模式（面板按钮 / engine 状态 / controller 接线）
- qualifier 面板 denoise 滑条与 params 同步
"""

import os
from types import SimpleNamespace

import pytest

from src.features.color.core.layer import Layer


# ---------------------------------------------------------------------------
# 参数模型
# ---------------------------------------------------------------------------

def test_layer_default_qualifier_params_include_denoise():
    layer = Layer("l")
    assert layer.qualifier_params["denoise"] == 0.0
    assert layer.qualifier_params["clean_black"] == 0.0
    assert layer.qualifier_params["invert"] is False


def test_qualifier_denoise_survives_serialization_roundtrip():
    layer = Layer("l")
    layer.qualifier_params["denoise"] = 42.0
    layer.qualifier_params["enabled"] = True

    data = layer.to_dict()
    assert data["qualifier_params"]["denoise"] == 42.0

    restored = Layer.from_dict(dict(data))
    assert restored.qualifier_params["denoise"] == 42.0


def test_qualifier_denoise_copied_by_copy_params_from():
    src = Layer("src")
    src.qualifier_params["denoise"] = 77.0
    dst = Layer("dst")
    dst.copy_params_from(src)
    assert dst.qualifier_params["denoise"] == 77.0


# ---------------------------------------------------------------------------
# matte 缓存键契约
# ---------------------------------------------------------------------------

def _base_inputs():
    qp = {"enabled": True, "hue_center": 0.5, "denoise": 0.0}
    ms = [{"type": "circle", "id": "s1"}]
    return qp, ms


def test_matte_cache_hash_is_deterministic():
    from src.features.color.core.async_renderer import matte_cache_hash
    qp, ms = _base_inputs()
    h1 = matte_cache_hash(qp, ms, (1920, 1080), (0, 0, 1920, 1080), 0, 0)
    h2 = matte_cache_hash(qp, ms, (1920, 1080), (0, 0, 1920, 1080), 0, 0)
    assert h1 == h2


def test_matte_cache_hash_sensitive_to_each_input():
    from src.features.color.core.async_renderer import matte_cache_hash
    qp, ms = _base_inputs()
    base = matte_cache_hash(qp, ms, (1920, 1080), None, 0, 0)

    # qualifier 参数（含 denoise）
    qp2 = dict(qp, denoise=10.0)
    assert matte_cache_hash(qp2, ms, (1920, 1080), None, 0, 0) != base
    # mask shapes
    ms2 = [dict(ms[0], id="s2")]
    assert matte_cache_hash(qp, ms2, (1920, 1080), None, 0, 0) != base
    # 生成尺寸
    assert matte_cache_hash(qp, ms, (1280, 720), None, 0, 0) != base
    # 视频帧代际（播放/seek）
    assert matte_cache_hash(qp, ms, (1920, 1080), None, 3, 0) != base
    # mask 纹理上传代际（AI matte / bitmap 重绘不改变 qp/ms 序列化）
    assert matte_cache_hash(qp, ms, (1920, 1080), None, 0, 1) != base


def test_mask_preview_modes_mapping():
    from src.features.color.core.async_renderer import MASK_PREVIEW_MODES
    assert MASK_PREVIEW_MODES == {"highlight": 0, "black": 1, "matte": 2}


def test_matte_denoise_shader_file_has_required_uniforms():
    shader_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "src", "features", "color", "shaders")
    path = os.path.join(shader_dir, "matte_denoise.glsl")
    assert os.path.isfile(path)
    src = open(path, encoding="utf-8").read()
    assert "u_strength" in src
    assert "smoothstep" in src


def test_mask_preview_shader_has_mode_uniform():
    """async_renderer 的 mask_preview shader 源码含 u_mode 三模式分支。"""
    src = open(
        "src/features/color/core/async_renderer.py", encoding="utf-8").read()
    assert "uniform int u_mode;" in src
    assert "u_mode == 2" in src  # matte 灰度分支


# ---------------------------------------------------------------------------
# engine 预览状态（bare-mixin 模式，无 GL）
# ---------------------------------------------------------------------------

def _fake_engine():
    return SimpleNamespace(
        show_mask_mode=False,
        mask_preview_style="highlight",
        render_calls=[],
    )


def test_engine_set_mask_preview_mode_updates_style():
    from src.features.color.core.image_engine import ImageEngine
    eng = _fake_engine()
    eng.render_pipeline = lambda **kw: eng.render_calls.append(kw)

    ImageEngine.set_mask_preview_mode(eng, True, "matte")
    assert eng.show_mask_mode is True
    assert eng.mask_preview_style == "matte"

    ImageEngine.set_mask_preview_mode(eng, False)
    assert eng.show_mask_mode is False
    assert eng.mask_preview_style == "matte"  # 关闭不改模式

    ImageEngine.set_mask_preview_mode(eng, True, style="black")
    assert eng.mask_preview_style == "black"


def test_engine_rejects_unknown_style_falls_back_unchanged():
    from src.features.color.core.image_engine import ImageEngine
    eng = _fake_engine()
    eng.render_pipeline = lambda **kw: None
    # 非法模式字符串仍会存储，但渲染端会回退 highlight（MASK_PREVIEW_MODES.get 默认 0）
    ImageEngine.set_mask_preview_mode(eng, True, "nonsense")
    assert eng.mask_preview_style == "nonsense"
    from src.features.color.core.async_renderer import MASK_PREVIEW_MODES
    assert MASK_PREVIEW_MODES.get("nonsense", 0) == 0


# ---------------------------------------------------------------------------
# qualifier 面板
# ---------------------------------------------------------------------------

@pytest.fixture
def panel(qapp):
    from src.features.color.ui.qualifier_panel import QualifierPanel
    p = QualifierPanel()
    yield p
    p.deleteLater()


def test_panel_has_denoise_slider_in_params(panel):
    assert panel.params["denoise"] == 0.0
    assert panel.denoise_ctrl.value() == 0.0


def test_panel_denoise_slider_updates_params_and_emits(panel):
    emitted = []
    panel.params_changed.connect(lambda p: emitted.append(dict(p)))

    # ParamSliderRow.setValue 不 emit（防递归），用户路径 = setValue + 回调
    panel.denoise_ctrl.setValue(30)
    panel.on_slider_param_changed()
    assert panel.params["denoise"] == 30.0
    assert any(p.get("denoise") == 30.0 for p in emitted)
    # 参数调整自动激活限定器
    assert panel.params["enabled"] is True


def test_panel_update_param_denoise_syncs_slider(panel):
    panel.update_param("denoise", 55)
    assert panel.denoise_ctrl.value() == 55.0
    assert panel.params["denoise"] == 55.0


def test_panel_reset_all_clears_denoise(panel):
    panel.update_param("denoise", 60)
    panel.reset_all()
    assert panel.params["denoise"] == 0.0
    assert panel.denoise_ctrl.value() == 0.0


def test_panel_set_params_syncs_denoise(panel):
    panel.set_params({"denoise": 25, "enabled": True})
    assert panel.denoise_ctrl.value() == 25.0


def test_panel_preview_buttons_request_modes(panel):
    emitted = []
    panel.matte_preview_requested.connect(emitted.append)

    # 点击高亮 → 请求 highlight，按钮 checked
    panel._preview_buttons["highlight"].click()
    assert emitted == ["highlight"]
    assert panel._preview_buttons["highlight"].isChecked()
    assert not panel._preview_buttons["matte"].isChecked()

    # 切到灰度 → 旧按钮取消，新按钮 checked
    panel._preview_buttons["matte"].click()
    assert emitted == ["highlight", "matte"]
    assert panel._preview_buttons["matte"].isChecked()
    assert not panel._preview_buttons["highlight"].isChecked()

    # 再点当前激活按钮 → 关闭预览
    panel._preview_buttons["matte"].click()
    assert emitted == ["highlight", "matte", ""]
    assert not panel._preview_buttons["matte"].isChecked()


def test_panel_set_matte_preview_active_external_sync(panel):
    panel.set_matte_preview_active(True, "black")
    assert panel._preview_buttons["black"].isChecked()
    assert not panel._preview_buttons["highlight"].isChecked()
    assert not panel._preview_buttons["matte"].isChecked()

    panel.set_matte_preview_active(False)
    assert not any(b.isChecked() for b in panel._preview_buttons.values())


# ---------------------------------------------------------------------------
# 控制器接线（bare-mixin）
# ---------------------------------------------------------------------------

def test_controller_matte_preview_request_opens_with_style():
    from src.app.controllers.color_controller import ColorControllerMixin

    calls = []
    panel_calls = []
    self = SimpleNamespace(
        engine=SimpleNamespace(
            set_mask_preview_mode=lambda *a, **kw: calls.append((a, kw))),
        qualifier_panel=SimpleNamespace(
            set_matte_preview_active=lambda active, style: panel_calls.append(
                (active, style))),
    )
    ColorControllerMixin._on_matte_preview_requested(self, "matte")
    assert calls == [((True, "matte"), {})]
    assert panel_calls == [(True, "matte")]


def test_controller_matte_preview_request_empty_string_closes():
    from src.app.controllers.color_controller import ColorControllerMixin

    calls = []
    panel_calls = []
    self = SimpleNamespace(
        engine=SimpleNamespace(
            set_mask_preview_mode=lambda *a, **kw: calls.append((a, kw))),
        qualifier_panel=SimpleNamespace(
            set_matte_preview_active=lambda active, style: panel_calls.append(
                (active, style))),
    )
    ColorControllerMixin._on_matte_preview_requested(self, "")
    assert calls == [((False,), {})]
    assert panel_calls == [(False, "highlight")]


def test_canvas_mask_toggle_syncs_qualifier_panel():
    from src.features.color.ui.canvas import Canvas

    engine_calls = []
    panel_calls = []
    canvas = SimpleNamespace(
        engine=SimpleNamespace(
            mask_preview_style="black",
            set_mask_preview_mode=lambda on: engine_calls.append(on)),
    )
    qualifier_panel = SimpleNamespace(
        set_matte_preview_active=lambda active, style: panel_calls.append(
            (active, style)))
    canvas.window = lambda: SimpleNamespace(qualifier_panel=qualifier_panel)

    Canvas._on_mask_preview_toggled(canvas, True)
    assert engine_calls == [True]
    assert panel_calls == [(True, "black")]

    Canvas._on_mask_preview_toggled(canvas, False)
    assert engine_calls == [True, False]
    assert panel_calls == [(True, "black"), (False, "black")]
