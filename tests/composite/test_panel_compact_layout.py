"""参数面板紧凑化布局测试。

覆盖：
1. group_xy_pairs 扩展：triple（xyz 三合一）+ rgb（颜色三合一）
2. bool 参数单行化（_create_bool_row）
3. _create_xyz_param_row / _create_rgb_param_row 方法存在性
4. _create_color_picker 用项目 get_color（替换裸 QColorDialog）

CI 无 GPU/无完整 Qt 事件循环，故采用纯函数断言（group_xy_pairs）+ 源码级
断言（inspect.getsource）+ 方法存在性检查。
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
from types import SimpleNamespace

import pytest

from src.features.composite.ui.node_properties_panel import group_xy_pairs


def _pd(ptype="float", group=None):
    return SimpleNamespace(ptype=ptype, group=group)


# ──────────────────────────────────────────────────────────────────────────
# #1 group_xy_pairs: triple（X/Y/Z 三合一）
# ──────────────────────────────────────────────────────────────────────────

class TestGroupTriple:
    """三个连续 _x/_y/_z 合并成 triple 指令。"""

    def test_xyz_consecutive_becomes_triple(self):
        keys = ["pos_x", "pos_y", "pos_z"]
        out = group_xy_pairs(keys)
        assert out == [("triple", "pos", "pos_x", "pos_y", "pos_z")]

    def test_xyz_with_group_all_same(self):
        keys = ["pos_x", "pos_y", "pos_z"]
        defs = {"pos_x": _pd(group="T"), "pos_y": _pd(group="T"),
                "pos_z": _pd(group="T")}
        out = group_xy_pairs(keys, defs)
        assert out[0][0] == "triple"

    def test_xyz_different_group_not_triple(self):
        """_z 在不同 group → triple 不成立，但 _x/_y 同 group 仍 pair，_z single。"""
        keys = ["pos_x", "pos_y", "pos_z"]
        defs = {"pos_x": _pd(group="T"), "pos_y": _pd(group="T"),
                "pos_z": _pd(group="Other")}
        out = group_xy_pairs(keys, defs)
        # _x/_y 同 group → pair；_z 不同 group → single
        assert ("pair", "pos", "pos_x", "pos_y") in out
        assert ("single", "pos_z") in out

    def test_xyz_non_float_not_triple(self):
        keys = ["pos_x", "pos_y", "pos_z"]
        defs = {"pos_x": _pd("int"), "pos_y": _pd("int"), "pos_z": _pd("int")}
        out = group_xy_pairs(keys, defs)
        # int 也算 numeric（isinstance 检查 ptype=="float"）；这里 ptype=int 不匹配
        assert out[0][0] != "triple"

    def test_triple_followed_by_single(self):
        keys = ["pos_x", "pos_y", "pos_z", "rotation"]
        out = group_xy_pairs(keys)
        assert out == [
            ("triple", "pos", "pos_x", "pos_y", "pos_z"),
            ("single", "rotation"),
        ]

    def test_xy_without_z_still_pair(self):
        """只有 _x/_y（无 _z）仍走 pair（向后兼容）。"""
        keys = ["scale_x", "scale_y", "rotation"]
        out = group_xy_pairs(keys)
        assert out == [
            ("pair", "scale", "scale_x", "scale_y"),
            ("single", "rotation"),
        ]


# ──────────────────────────────────────────────────────────────────────────
# #2 group_xy_pairs: rgb（R/G/B 颜色三合一）
# ──────────────────────────────────────────────────────────────────────────

class TestGroupRGB:
    """三个连续 _r/_g/_b 合并成 rgb 指令。"""

    def test_rgb_consecutive_becomes_rgb(self):
        keys = ["color_r", "color_g", "color_b"]
        out = group_xy_pairs(keys)
        assert out == [("rgb", "color", "color_r", "color_g", "color_b")]

    def test_rgb_with_group(self):
        keys = ["light_color_r", "light_color_g", "light_color_b"]
        defs = {k: _pd(group="G") for k in keys}
        out = group_xy_pairs(keys, defs)
        assert out[0][0] == "rgb"
        assert out[0][1] == "light_color"

    def test_rgb_different_group_not_merged(self):
        keys = ["color_r", "color_g", "color_b"]
        defs = {"color_r": _pd(group="A"), "color_g": _pd(group="A"),
                "color_b": _pd(group="B")}
        out = group_xy_pairs(keys, defs)
        assert out[0][0] != "rgb"

    def test_rgb_in_full_param_list(self):
        """真实节点参数序列里 rgb 正确识别。"""
        keys = ["metallic", "roughness", "color_r", "color_g", "color_b", "enabled"]
        out = group_xy_pairs(keys)
        assert out == [
            ("single", "metallic"),
            ("single", "roughness"),
            ("rgb", "color", "color_r", "color_g", "color_b"),
            ("single", "enabled"),
        ]


# ──────────────────────────────────────────────────────────────────────────
# #3 bool 参数单行化
# ──────────────────────────────────────────────────────────────────────────

class TestBoolRowCompact:
    """bool 参数走 _create_bool_row 单行布局。"""

    def test_create_bool_row_method_exists(self):
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        assert hasattr(NodePropertiesPanel, "_create_bool_row")

    def test_bool_row_uses_horizontal_layout(self):
        """_create_bool_row 用 QHBoxLayout（单行），非 QVBoxLayout（两行）。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_bool_row)
        assert "QHBoxLayout" in src
        assert "QVBoxLayout" not in src

    def test_bool_checkbox_no_text(self):
        """checkbox 不带文字（避免与左侧标签重复）。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_bool_row)
        assert 'setText("")' in src

    def test_create_param_row_bool_early_return(self):
        """_create_param_row 中 bool 在 fallback 前提前 return。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_param_row)
        # bool 判断在 combo 判断之前
        bool_pos = src.find("_create_bool_row")
        combo_pos = src.find("_create_combo_row")
        assert 0 < bool_pos < combo_pos


# ──────────────────────────────────────────────────────────────────────────
# #4 XYZ 三合一行 + RGB 颜色行方法
# ──────────────────────────────────────────────────────────────────────────

class TestCompactRowMethods:
    """_create_xyz_param_row / _create_rgb_param_row 方法存在 + 布局正确。"""

    def test_create_xyz_param_row_exists(self):
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        assert hasattr(NodePropertiesPanel, "_create_xyz_param_row")
        assert hasattr(NodePropertiesPanel, "_create_xyz_param_row_for_section")

    def test_create_rgb_param_row_exists(self):
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        assert hasattr(NodePropertiesPanel, "_create_rgb_param_row")
        assert hasattr(NodePropertiesPanel, "_create_rgb_param_row_for_section")

    def test_xyz_row_has_three_axes(self):
        """xyz 行构建 3 个 axis（x/y/z marker + drag）。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_xyz_param_row)
        # 3 次 build_axis 调用
        assert src.count("build_axis(") >= 3

    def test_xyz_row_chain_for_scale(self):
        """scale/radius 的 xyz 行有 chain toggle（三轴等比）。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_xyz_param_row)
        assert "scale" in src
        assert "radius" in src
        assert "⛓" in src

    def test_rgb_row_has_color_button(self):
        """rgb 行有色块按钮 + get_color 弹窗。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_rgb_param_row)
        assert "get_color" in src
        assert "QPushButton" in src or "color_btn" in src

    def test_rgb_row_three_drag_values(self):
        """rgb 行有 3 个 DragValue（R/G/B）。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_rgb_param_row)
        assert src.count("build_channel(") >= 3

    def test_rgb_row_registers_three_keys(self):
        """rgb 行把 r/g/b 三个 key 都注册到 _param_widgets。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_rgb_param_row)
        assert "self._param_widgets[r_key]" in src
        assert "self._param_widgets[g_key]" in src
        assert "self._param_widgets[b_key]" in src

    def test_build_directives_handles_triple_and_rgb(self):
        """_build_directives_into 有 triple 和 rgb 分支。"""
        from src.features.composite.ui.node_properties_panel import NodeSectionWidget
        src = inspect.getsource(NodeSectionWidget._build_directives_into)
        assert "'triple'" in src or "directive[0] == 'triple'" in src
        assert "'rgb'" in src or "directive[0] == 'rgb'" in src


# ──────────────────────────────────────────────────────────────────────────
# #4b XYZ slider 空间不足时隐藏（独立阈值）
# ──────────────────────────────────────────────────────────────────────────

class TestTripleSliderVisibility:
    """XYZ 三合一行的 slider 在面板窄时隐藏，用独立阈值（比 XY 更高）。"""

    def test_triple_slider_threshold_constant_exists(self):
        from src.features.composite.ui import node_properties_panel as mod
        assert hasattr(mod, "_SLIDER_SHOW_WIDTH_TRIPLE")
        # triple 阈值必须高于 pair（XY），否则没意义
        assert mod._SLIDER_SHOW_WIDTH_TRIPLE > mod._SLIDER_SHOW_WIDTH

    def test_section_has_triple_sliders_holder(self):
        """NodeSectionWidget 有独立的 _triple_sliders 列表。"""
        from src.features.composite.ui.node_properties_panel import NodeSectionWidget
        src = inspect.getsource(NodeSectionWidget.__init__)
        assert "_triple_sliders" in src

    def test_apply_slider_visibility_handles_triple(self):
        """_apply_slider_visibility 有 triple slider 的独立宽度判定。"""
        from src.features.composite.ui.node_properties_panel import NodeSectionWidget
        src = inspect.getsource(NodeSectionWidget._apply_slider_visibility)
        assert "_triple_sliders" in src
        assert "_SLIDER_SHOW_WIDTH_TRIPLE" in src or "show_triple" in src

    def test_build_directives_uses_triple_holder(self):
        """triple 分支的 slider 进 _triple_sliders（非 _pair_sliders）。"""
        from src.features.composite.ui.node_properties_panel import NodeSectionWidget
        src = inspect.getsource(NodeSectionWidget._build_directives_into)
        # triple 分支里 slider_holder=self._triple_sliders
        assert "self._triple_sliders" in src

    def test_triple_slider_hidden_below_threshold(self, qapp):
        """面板宽度低于 triple 阈值时，XYZ slider 不可见。"""
        from src.features.composite.ui.node_properties_panel import (
            NodePropertiesPanel, NodeSectionWidget)
        from src.features.composite.nodes.camera3d_node import Camera3DNode

        panel = NodePropertiesPanel()
        node = Camera3DNode("test_cam")
        pdefs = {pd.name: pd for pd in node.PARAM_DEFS}
        params = {pd.name: pd.default for pd in node.PARAM_DEFS}
        section = NodeSectionWidget(
            node_id="test_cam", node_type="camera3d", node_title="Camera3D",
            parameters=params, param_options={}, param_defs=pdefs,
            enabled=True, node_color="#666", parent_panel=panel)
        section.resize(_SLIDER_W_NARROW, 400)
        section._apply_slider_visibility()
        # 窄于 triple 阈值 → triple slider 全部隐藏
        for slider in section._triple_sliders:
            assert not slider.isVisibleTo(section), \
                "XYZ slider 应在面板窄时隐藏"

    def test_triple_slider_visible_above_threshold(self, qapp):
        """面板宽度高于 triple 阈值时，XYZ slider 可见。"""
        from src.features.composite.ui.node_properties_panel import (
            NodePropertiesPanel, NodeSectionWidget, _SLIDER_SHOW_WIDTH_TRIPLE)
        from src.features.composite.nodes.camera3d_node import Camera3DNode

        panel = NodePropertiesPanel()
        node = Camera3DNode("test_cam2")
        pdefs = {pd.name: pd for pd in node.PARAM_DEFS}
        params = {pd.name: pd.default for pd in node.PARAM_DEFS}
        section = NodeSectionWidget(
            node_id="test_cam2", node_type="camera3d", node_title="Camera3D",
            parameters=params, param_options={}, param_defs=pdefs,
            enabled=True, node_color="#666", parent_panel=panel)
        section.resize(_SLIDER_SHOW_WIDTH_TRIPLE + 50, 400)
        section._apply_slider_visibility()
        # 宽于 triple 阈值 → triple slider 可见
        for slider in section._triple_sliders:
            assert slider.isVisibleTo(section), \
                "XYZ slider 应在面板足够宽时显示"


# 用于 slider 可见性测试的宽度常量（narrow < triple 阈值）
_SLIDER_W_NARROW = 420  # 高于 pair(380) 但低于 triple(480)：此时 XY slider 显示，XYZ 隐藏


# ──────────────────────────────────────────────────────────────────────────
# #5 色彩选择器替换（get_color）
# ──────────────────────────────────────────────────────────────────────────

class TestColorPickerUsesGetColor:
    """_create_color_picker 用项目 get_color 替换裸 QColorDialog。"""

    def test_create_color_picker_imports_get_color(self):
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_color_picker)
        assert "from src.shared.color import get_color" in src

    def test_create_color_picker_no_qcolordialog(self):
        """_create_color_picker 不再直接用 QColorDialog。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_color_picker)
        assert "QColorDialog(self)" not in src

    def test_create_color_picker_calls_get_color(self):
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._create_color_picker)
        assert "get_color(parent=self" in src
        assert "on_live=" in src


# ──────────────────────────────────────────────────────────────────────────
# 集成：真实节点参数序列的配对结果
# ──────────────────────────────────────────────────────────────────────────

class TestRealNodePairing:
    """真实 3D 节点的参数序列正确识别 triple/rgb。"""

    def test_geo3d_object_color_rgb(self):
        """Geo3D 的 object_color_r/g/b 合并成 rgb。"""
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        defs = {p.name: p for p in Geo3DNode.PARAM_DEFS}
        # 取材质分组的 color 参数
        keys = ["metallic", "roughness",
                "object_color_r", "object_color_g", "object_color_b"]
        out = group_xy_pairs(keys, defs)
        rgb_directives = [d for d in out if d[0] == "rgb"]
        assert len(rgb_directives) == 1
        assert rgb_directives[0][1] == "object_color"

    def test_camera3d_target_xyz(self):
        """Camera3D 的 cam_target_x/y/z 合并成 triple。"""
        from src.features.composite.nodes.camera3d_node import Camera3DNode
        defs = {p.name: p for p in Camera3DNode.PARAM_DEFS}
        keys = ["cam_target_x", "cam_target_y", "cam_target_z"]
        out = group_xy_pairs(keys, defs)
        assert out == [("triple", "cam_target", "cam_target_x",
                        "cam_target_y", "cam_target_z")]

    def test_camera3d_eye_xyz(self):
        """Camera3D 的 cam_eye_x/y/z 合并成 triple。"""
        from src.features.composite.nodes.camera3d_node import Camera3DNode
        defs = {p.name: p for p in Camera3DNode.PARAM_DEFS}
        keys = ["cam_eye_x", "cam_eye_y", "cam_eye_z"]
        out = group_xy_pairs(keys, defs)
        assert out == [("triple", "cam_eye", "cam_eye_x",
                        "cam_eye_y", "cam_eye_z")]

    def test_geo3d_transform_xyz(self):
        """Geo3D 的 object_pos_x/y/z 和 object_rot_x/y/z 各合并成 triple。"""
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        defs = {p.name: p for p in Geo3DNode.PARAM_DEFS}
        keys = ["object_pos_x", "object_pos_y", "object_pos_z",
                "object_rot_x", "object_rot_y", "object_rot_z"]
        out = group_xy_pairs(keys, defs)
        triples = [d for d in out if d[0] == "triple"]
        assert len(triples) == 2
        bases = {t[1] for t in triples}
        assert bases == {"object_pos", "object_rot"}
