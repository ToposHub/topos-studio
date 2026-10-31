"""批 A（AUD-25420 / AUD-25421 / AUD-25600）回归测试。

覆盖：
- AUD-25420: LayerPanel._toggle_current_visibility 不再访问不存在的
  widget._is_visible，改读 visible_btn check 态。
- AUD-25421: 混合模式下拉显示本地化名，但 emit 给引擎的必须是规范名
  （Layer.BLEND_MODES 反查可命中的名字）；update_layers 同步用 data 定位。
- AUD-25600: 程序化 set_params/reset_all 时 invert 复选框 toggled 槽
  检查 _updating_flag 状态锁直接返回，不强制 enabled、不重复提交引擎。
"""

import pytest

from src.features.color.core.layer import Layer
from src.features.color.ui.layer_panel import (
    LayerPanel,
    _BLEND_MODE_KEY_TO_CANONICAL,
)


# ---------------------------------------------------------------------------
# AUD-25420: 切换当前图层可见性
# ---------------------------------------------------------------------------

@pytest.fixture
def layer_panel(qapp):
    p = LayerPanel()
    layers = [Layer("bottom"), Layer("top")]
    layers[0].visible = True
    layers[1].visible = False
    p.update_layers(layers, active_index=0)
    yield p
    p.deleteLater()


def test_toggle_current_visibility_does_not_crash(layer_panel):
    """修复前：访问不存在的 widget._is_visible 触发 AttributeError。"""
    layer_panel._toggle_current_visibility()  # 不应抛异常


def test_toggle_current_visibility_emits_inverted_state(layer_panel):
    """active_index=0（bottom，visible=True）→ 应 emit (0, False)。"""
    emitted = []
    layer_panel.layer_visibility_changed.connect(
        lambda idx, vis: emitted.append((idx, vis)))
    layer_panel._toggle_current_visibility()
    assert emitted == [(0, False)]


def test_toggle_current_visibility_roundtrip(layer_panel):
    """引擎回写不可见状态后，再次切换应翻转回 True。"""
    layers = [Layer("bottom"), Layer("top")]
    layers[0].visible = False  # 模拟引擎已应用上一次 emit 的 False
    layers[1].visible = False
    layer_panel.update_layers(layers, active_index=0)
    emitted = []
    layer_panel.layer_visibility_changed.connect(
        lambda idx, vis: emitted.append((idx, vis)))
    layer_panel._toggle_current_visibility()
    assert emitted == [(0, True)]


def test_toggle_visibility_without_selection_is_noop(qapp):
    p = LayerPanel()
    p.update_layers([], active_index=-1)
    p._toggle_current_visibility()  # 无选中行，安全 no-op
    p.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25421: 混合模式规范名契约
# ---------------------------------------------------------------------------

def test_blend_combo_data_are_canonical_names(layer_panel):
    """每个下拉项的 UserRole data 必须能在 Layer.BLEND_MODES 反查命中。"""
    combo = layer_panel.blend_combo
    assert combo.count() == len(_BLEND_MODE_KEY_TO_CANONICAL)
    for i in range(combo.count()):
        canonical = combo.itemData(i)
        assert canonical in Layer.BLEND_MODES, (
            f"item {i} data {canonical!r} 不在 BLEND_MODES 中，"
            "composition_uniforms 会静默回退 Normal")


def test_saturation_blend_maps_to_canonical(layer_panel):
    """saturation_blend 的规范名必须是 "Saturation"（标题化结果是
    "Saturation Blend"，不在 BLEND_MODES 中——修复前的实际回退点）。"""
    combo = layer_panel.blend_combo
    data_idx = combo.findData("Saturation")
    assert data_idx >= 0
    # en 兜底下显示名与规范名一致；本地化后显示名可不同但 data 不变
    assert combo.itemData(data_idx) == "Saturation"
    assert "Saturation Blend" not in (
        combo.itemData(i) for i in range(combo.count()))


def test_blend_mode_signal_emits_canonical_even_when_localized(qapp, monkeypatch):
    """即使 I18N 返回完全本地化的显示名，emit 的也必须是规范名。"""
    from src.shared.i18n.i18n import I18N

    def fake_tr(key, default=None, **kwargs):
        return f"LOC[{key}]"

    monkeypatch.setattr(I18N, "tr", staticmethod(fake_tr))

    p = LayerPanel()
    try:
        combo = p.blend_combo
        emitted = []
        p.layer_blend_mode_changed.connect(emitted.append)

        combo.setCurrentIndex(-1)  # 归零，保证后续每个索引都是真实切换
        for i in range(combo.count()):
            emitted.clear()
            combo.setCurrentIndex(i)
            assert len(emitted) == 1
            canonical = combo.itemData(i)
            assert emitted[0] == canonical
            assert emitted[0] in Layer.BLEND_MODES  # 引擎反查可命中
            assert not emitted[0].startswith("LOC[")  # 绝不是本地化显示名
    finally:
        p.deleteLater()


def test_update_layers_syncs_combo_from_canonical(layer_panel):
    """update_layers 用 data 定位下拉项（旧代码 setCurrentText(规范名)
    在本地化显示名下永远匹配失败）。"""
    layers = [Layer("bottom"), Layer("top")]
    layers[1].blend_mode = "Soft Light"
    layer_panel.update_layers(layers, active_index=1)
    assert layer_panel.blend_combo.currentData() == "Soft Light"

    # 面板未收录的模式（Addition）回退 Normal，与引擎 [R3-1] 一致
    layers[1].blend_mode = "Addition"
    layer_panel.update_layers(layers, active_index=1)
    assert layer_panel.blend_combo.currentData() == "Normal"


def test_update_layers_does_not_emit_blend_signal(layer_panel):
    """程序化同步期间（blockSignals 状态锁）不得 emit 混合模式信号。"""
    emitted = []
    layer_panel.layer_blend_mode_changed.connect(emitted.append)
    layers = [Layer("bottom"), Layer("top")]
    layers[0].blend_mode = "Multiply"
    layer_panel.update_layers(layers, active_index=0)
    assert emitted == []


# ---------------------------------------------------------------------------
# AUD-25600: invert 复选框状态锁
# ---------------------------------------------------------------------------

@pytest.fixture
def qualifier_panel(qapp):
    from src.features.color.ui.qualifier_panel import QualifierPanel
    p = QualifierPanel()
    yield p
    p.deleteLater()


def test_set_params_invert_does_not_force_enable(qualifier_panel):
    """程序化 set_params（invert False→True）不得把 enabled 强制为 True。

    修复前：invert_check.setChecked(True) 触发 on_param_changed，
    绕过状态锁 → params["enabled"] 被置 True。
    """
    qualifier_panel.set_params({"invert": True, "enabled": False})
    assert qualifier_panel.params["invert"] is True
    assert qualifier_panel.params["enabled"] is False
    assert qualifier_panel.invert_check.isChecked() is True


def test_set_params_invert_emits_params_changed_once(qualifier_panel):
    """程序化路径只允许 set_params 末尾的一次 emit（不得叠加 toggled 槽的
    重复提交）。"""
    emitted = []
    qualifier_panel.params_changed.connect(lambda p: emitted.append(dict(p)))
    qualifier_panel.set_params({"invert": True, "enabled": False})
    assert len(emitted) == 1
    assert emitted[0]["invert"] is True
    assert emitted[0]["enabled"] is False


def test_reset_all_does_not_force_enable_via_invert(qualifier_panel):
    """reset 路径：先让 invert=True，reset_all 内部 setChecked(False)
    不得经 toggled 槽把 enabled 强制为 True。"""
    qualifier_panel.set_params({"invert": True, "enabled": True})
    qualifier_panel.reset_all()
    assert qualifier_panel.params["invert"] is False
    assert qualifier_panel.params["enabled"] is False
    assert qualifier_panel.invert_check.isChecked() is False


def test_reset_all_emits_params_changed_once(qualifier_panel):
    emitted = []
    qualifier_panel.params_changed.connect(lambda p: emitted.append(dict(p)))
    qualifier_panel.set_params({"invert": True, "enabled": True})
    emitted.clear()
    qualifier_panel.reset_all()
    assert len(emitted) == 1
    assert emitted[0]["enabled"] is False


def test_user_invert_toggle_still_auto_activates(qualifier_panel):
    """用户交互路径不受影响：手动勾选 invert 仍自动激活并提交一次。"""
    emitted = []
    qualifier_panel.params_changed.connect(lambda p: emitted.append(dict(p)))
    qualifier_panel.invert_check.setChecked(True)  # 模拟用户点击
    assert qualifier_panel.params["invert"] is True
    assert qualifier_panel.params["enabled"] is True
    assert len(emitted) == 1


def test_updating_flag_cleared_after_programmatic_paths(qualifier_panel):
    """程序化路径结束后状态锁必须释放，用户交互恢复生效。"""
    qualifier_panel.set_params({"invert": False, "enabled": False})
    qualifier_panel.reset_all()
    assert qualifier_panel._updating_flag is False
    qualifier_panel.invert_check.setChecked(True)
    assert qualifier_panel.params["enabled"] is True
