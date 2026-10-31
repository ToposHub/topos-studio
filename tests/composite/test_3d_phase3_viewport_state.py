"""Phase 3: 3D 视口状态/控制器拆分测试。

覆盖 docs/composite_3d_module_audit_optimization_plan_2026-06-29.md 的 Phase 3：
1. viewport3d_state：数据层（布局/视图/工具/组件/吸附 enum + dataclass）
2. viewport3d_controller：MMB click/drag 区分、工具模式快捷键、信号
3. CompositeViewer 持有 controller 实例（非侵入式接入）

注：用 mock QMouseEvent/QKeyEvent 验证控制器逻辑（不需真实 GL/Qt 窗口）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock

import pytest

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QKeyEvent
from PyQt6.QtCore import QCoreApplication

from src.features.composite.ui.viewport3d_state import (
    Viewport3DState, ViewportLayoutMode, ViewportViewKind, Viewer3DToolMode,
    ComponentMode, SnapMode, SnapQuantizeSettings, QUAD_VIEW_ORDER,
    VIEW_KIND_LABELS, TOOL3D_MODE_SHORTCUTS, TOOL3D_MODE_LABELS)
from src.features.composite.ui.viewport3d_controller import (
    Viewport3DController, MMB_CLICK_THRESHOLD_PX)


# ──────────────────────────────────────────────────────────────────────────
# Part 1: viewport3d_state 数据层
# ──────────────────────────────────────────────────────────────────────────

class TestViewport3DState:
    """Viewport3DState 数据层：默认值 + toggle_layout。"""

    def test_defaults(self):
        s = Viewport3DState()
        assert s.layout_mode == ViewportLayoutMode.SINGLE
        assert s.active_view == ViewportViewKind.PERSPECTIVE
        assert s.tool_mode == Viewer3DToolMode.SELECT
        assert s.component_mode == ComponentMode.OBJECT
        assert s.snap.snap_mode == SnapMode.NONE
        assert s.axis_edit_enabled is False
        assert not s.is_quad()

    def test_toggle_layout_single_to_quad(self):
        s = Viewport3DState()
        result = s.toggle_layout()
        assert result == ViewportLayoutMode.QUAD
        assert s.is_quad()
        # 切到 QUAD 时活动视图重置为透视
        assert s.active_view == ViewportViewKind.PERSPECTIVE

    def test_toggle_layout_quad_to_single(self):
        s = Viewport3DState(layout_mode=ViewportLayoutMode.QUAD)
        result = s.toggle_layout()
        assert result == ViewportLayoutMode.SINGLE
        assert not s.is_quad()

    def test_reset_tool(self):
        s = Viewport3DState(tool_mode=Viewer3DToolMode.MOVE,
                            component_mode=ComponentMode.POINT)
        s.reset_tool()
        assert s.tool_mode == Viewer3DToolMode.SELECT
        assert s.component_mode == ComponentMode.OBJECT

    def test_snap_quantize_defaults(self):
        sq = SnapQuantizeSettings()
        assert sq.quantize_enabled is False
        assert sq.move_step > 0
        assert sq.rotate_step > 0
        assert sq.scale_step > 0


class TestViewportEnums:
    """3D 视口相关 enum 完整性。"""

    def test_layout_modes(self):
        assert {m.value for m in ViewportLayoutMode} == {"single", "quad"}

    def test_view_kinds(self):
        assert {v.value for v in ViewportViewKind} == {
            "perspective", "top", "right", "front"}

    def test_tool_modes(self):
        assert {m.value for m in Viewer3DToolMode} == {
            "select3d", "move3d", "rotate3d", "scale3d"}

    def test_component_modes(self):
        assert {m.value for m in ComponentMode} == {
            "object", "point", "edge", "polygon"}

    def test_snap_modes(self):
        assert {m.value for m in SnapMode} == {"none", "grid", "vertex", "object"}

    def test_quad_view_order_has_four(self):
        assert len(QUAD_VIEW_ORDER) == 4
        assert len(set(QUAD_VIEW_ORDER)) == 4  # 不重复

    def test_view_kind_labels_complete(self):
        for vk in ViewportViewKind:
            assert vk in VIEW_KIND_LABELS

    def test_tool_mode_shortcuts_complete(self):
        for tm in Viewer3DToolMode:
            assert tm in TOOL3D_MODE_SHORTCUTS
        # C4D 约定：W=Move, E=Rotate, R=Scale
        assert TOOL3D_MODE_SHORTCUTS[Viewer3DToolMode.MOVE] == "W"
        assert TOOL3D_MODE_SHORTCUTS[Viewer3DToolMode.ROTATE] == "E"
        assert TOOL3D_MODE_SHORTCUTS[Viewer3DToolMode.SCALE] == "R"

    def test_tool3d_labels_complete(self):
        for tm in Viewer3DToolMode:
            assert tm in TOOL3D_MODE_LABELS


# ──────────────────────────────────────────────────────────────────────────
# Part 2: viewport3d_controller MMB click/drag 区分
# ──────────────────────────────────────────────────────────────────────────

def _make_mouse_event(button, pos, modifiers=Qt.KeyboardModifier.NoModifier):
    """构造一个 mock QMouseEvent（避免真实窗口）。"""
    event = MagicMock(spec=QMouseEvent)
    event.button.return_value = button
    event.position.return_value = QPointF(pos)
    event.modifiers.return_value = modifiers
    return event


class TestMMBClickDragDisambiguation:
    """MMB click（toggle 四视图）vs drag（平移）区分。"""

    def test_mmb_click_toggles_quad(self):
        """MMB 按下后位移 < 阈值再释放 → toggle 四视图。"""
        ctrl = Viewport3DController()
        assert not ctrl.state.is_quad()
        press = _make_mouse_event(Qt.MouseButton.MiddleButton, QPointF(100, 100))
        release = _make_mouse_event(Qt.MouseButton.MiddleButton, QPointF(101, 101))
        ctrl.on_mmb_press(press)
        toggled = ctrl.on_mmb_release(release)
        assert toggled is True
        assert ctrl.state.is_quad()

    def test_mmb_drag_does_not_toggle(self):
        """MMB 按下后位移 > 阈值 → drag（平移），不 toggle 四视图。"""
        ctrl = Viewport3DController()
        press = _make_mouse_event(Qt.MouseButton.MiddleButton, QPointF(100, 100))
        move = _make_mouse_event(Qt.MouseButton.MiddleButton,
                                 QPointF(100 + MMB_CLICK_THRESHOLD_PX + 5, 100))
        release = _make_mouse_event(Qt.MouseButton.MiddleButton,
                                    QPointF(110, 100))
        ctrl.on_mmb_press(press)
        is_pan = ctrl.on_mmb_move(move)
        assert is_pan is True  # 超过阈值 → pan
        toggled = ctrl.on_mmb_release(release)
        assert toggled is False  # drag 不 toggle
        assert not ctrl.state.is_quad()  # 仍是 SINGLE

    def test_mmb_drag_emits_drag_started_once(self):
        """首次超过阈值时发 mmb_drag_started（仅一次）。"""
        ctrl = Viewport3DController()
        drag_started_count = [0]
        ctrl.mmb_drag_started.connect(lambda: drag_started_count.__setitem__(0, drag_started_count[0] + 1))
        press = _make_mouse_event(Qt.MouseButton.MiddleButton, QPointF(0, 0))
        ctrl.on_mmb_press(press)
        # 移动多次（都超过阈值）
        for dx in (10, 20, 30):
            move = _make_mouse_event(Qt.MouseButton.MiddleButton, QPointF(dx, 0))
            ctrl.on_mmb_move(move)
        assert drag_started_count[0] == 1  # 只发一次

    def test_alt_mmb_always_pan_no_toggle(self):
        """Alt+MMB 永远平移，即使位移小也不 toggle 四视图。"""
        ctrl = Viewport3DController()
        press = _make_mouse_event(Qt.MouseButton.MiddleButton, QPointF(100, 100),
                                  modifiers=Qt.KeyboardModifier.AltModifier)
        release = _make_mouse_event(Qt.MouseButton.MiddleButton, QPointF(101, 101))
        ctrl.on_mmb_press(press)
        toggled = ctrl.on_mmb_release(release)
        assert toggled is False  # Alt+MMB 不 toggle
        assert not ctrl.state.is_quad()

    def test_threshold_is_reasonable(self):
        """click 阈值是合理的小值（4px 左右，区分有意点击与微小抖动）。"""
        assert 2.0 <= MMB_CLICK_THRESHOLD_PX <= 8.0


# ──────────────────────────────────────────────────────────────────────────
# Part 3: 工具模式快捷键
# ──────────────────────────────────────────────────────────────────────────

def _make_key_event(text, modifiers=Qt.KeyboardModifier.NoModifier):
    """构造一个 mock QKeyEvent。"""
    event = MagicMock(spec=QKeyEvent)
    event.text.return_value = text
    event.modifiers.return_value = modifiers
    event.key.return_value = 0
    return event


class TestToolModeShortcuts:
    """3D 工具模式快捷键分发（Q/W/E/R）。"""

    def test_w_sets_move(self):
        ctrl = Viewport3DController()
        handled = ctrl.handle_key(_make_key_event("w"))
        assert handled is True
        assert ctrl.state.tool_mode == Viewer3DToolMode.MOVE

    def test_e_sets_rotate(self):
        ctrl = Viewport3DController()
        ctrl.handle_key(_make_key_event("e"))
        assert ctrl.state.tool_mode == Viewer3DToolMode.ROTATE

    def test_r_sets_scale(self):
        ctrl = Viewport3DController()
        ctrl.handle_key(_make_key_event("r"))
        assert ctrl.state.tool_mode == Viewer3DToolMode.SCALE

    def test_q_sets_select(self):
        ctrl = Viewport3DController()
        ctrl.set_tool_mode(Viewer3DToolMode.MOVE)
        ctrl.handle_key(_make_key_event("q"))
        assert ctrl.state.tool_mode == Viewer3DToolMode.SELECT

    def test_unrelated_key_not_consumed(self):
        """非工具键不消费（返回 False，viewer 继续处理）。"""
        ctrl = Viewport3DController()
        handled = ctrl.handle_key(_make_key_event("z"))
        assert handled is False

    def test_modifier_key_not_consumed(self):
        """带修饰键（如 Ctrl+W）不触发工具切换（避免冲突）。"""
        ctrl = Viewport3DController()
        handled = ctrl.handle_key(_make_key_event(
            "w", modifiers=Qt.KeyboardModifier.ControlModifier))
        assert handled is False
        assert ctrl.state.tool_mode == Viewer3DToolMode.SELECT  # 未变


# ──────────────────────────────────────────────────────────────────────────
# Part 4: 控制器信号 + 状态访问
# ──────────────────────────────────────────────────────────────────────────

class TestControllerSignals:
    """控制器发信号让 viewer 反应。"""

    def test_set_tool_mode_emits_signal(self):
        ctrl = Viewport3DController()
        received = []
        ctrl.tool_mode_changed.connect(lambda m: received.append(m))
        ctrl.set_tool_mode(Viewer3DToolMode.ROTATE)
        assert received == [Viewer3DToolMode.ROTATE]

    def test_set_same_tool_mode_no_signal(self):
        """设置相同模式不发信号（去抖）。"""
        ctrl = Viewport3DController()
        received = []
        ctrl.tool_mode_changed.connect(lambda m: received.append(m))
        ctrl.set_tool_mode(Viewer3DToolMode.SELECT)  # 已是 SELECT
        assert received == []

    def test_quad_toggle_emits_signal(self):
        ctrl = Viewport3DController()
        received = []
        ctrl.quad_toggled.connect(lambda q: received.append(q))
        press = _make_mouse_event(Qt.MouseButton.MiddleButton, QPointF(0, 0))
        release = _make_mouse_event(Qt.MouseButton.MiddleButton, QPointF(1, 1))
        ctrl.on_mmb_press(press)
        ctrl.on_mmb_release(release)
        assert received == [True]  # 切到 QUAD

    def test_reset_clears_state(self):
        ctrl = Viewport3DController()
        ctrl.set_tool_mode(Viewer3DToolMode.MOVE)
        ctrl.state.toggle_layout()  # QUAD
        ctrl.reset()
        assert ctrl.state.tool_mode == Viewer3DToolMode.SELECT
        assert not ctrl.state.is_quad()

    def test_state_property_returns_underlying(self):
        ctrl = Viewport3DController()
        assert isinstance(ctrl.state, Viewport3DState)


# ──────────────────────────────────────────────────────────────────────────
# Part 5: CompositeViewer 集成（持有 controller）
# ──────────────────────────────────────────────────────────────────────────

class TestViewerIntegration:
    """CompositeViewer 持有 Viewport3DController 实例（非侵入式接入）。"""

    def test_viewer_has_viewport3d_controller(self):
        """CompositeViewer 有 _viewport3d_ctrl 字段（Viewport3DController）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        from src.features.composite.ui.viewport3d_controller import Viewport3DController
        import inspect
        src = inspect.getsource(CompositeViewer.__init__)
        assert "_viewport3d_ctrl" in src
        assert "Viewport3DController" in src
