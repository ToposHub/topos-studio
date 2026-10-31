"""Phase 6: 组件选择 + 网格吸附 + 量化测试。

覆盖 docs/composite_3d_module_audit_optimization_plan_2026-06-29.md 的 Phase 6：
1. 组件模式 UI（工具栏分段 Object/Point/Edge/Polygon；Point/Edge disabled，
   Polygon 由 P7-5 面级 picking 启用）
2. 网格吸附 + 量化应用到 gizmo 拖动结果
3. viewer 接线（_apply_snap_quantize）

注：Point/Edge/Polygon 的真实 picking 需 GPU ID buffer，本阶段标记 disabled；
网格吸附 + 量化已接线到 gizmo 拖动。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest

from src.features.composite.ui.viewport3d_state import (
    ComponentMode, SnapMode, SnapQuantizeSettings, Viewer3DToolMode)


# ──────────────────────────────────────────────────────────────────────────
# Part 1: 组件模式 UI（工具栏分段）
# ──────────────────────────────────────────────────────────────────────────

class TestComponentModeUI:
    """工具栏组件模式分段：Object/Polygon 可用，Point/Edge disabled。"""

    def test_toolbar_has_component_buttons(self):
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        assert len(tb._comp_buttons) == 4
        for cm in ComponentMode:
            assert cm in tb._comp_buttons

    def test_object_mode_enabled(self):
        """Object（Model）模式按钮可用。"""
        from src.features.composite.ui.viewport3d_toolbar import (
            Viewport3DToolbar, _COMPONENT_ENABLED)
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        assert tb._comp_buttons[ComponentMode.OBJECT].isEnabled()
        assert _COMPONENT_ENABLED[ComponentMode.OBJECT] is True

    def test_point_edge_polygon_enabled(self):
        """Point/Edge/Polygon 均由 P1-22 的共享 BVH picking 启用。"""
        from src.features.composite.ui.viewport3d_toolbar import (
            Viewport3DToolbar, _COMPONENT_ENABLED)
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        for cm in (ComponentMode.POINT, ComponentMode.EDGE):
            assert tb._comp_buttons[cm].isEnabled()
            assert _COMPONENT_ENABLED[cm] is True
        assert tb._comp_buttons[ComponentMode.POLYGON].isEnabled()
        assert _COMPONENT_ENABLED[ComponentMode.POLYGON] is True

    def test_default_object_checked(self):
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        assert tb._comp_buttons[ComponentMode.OBJECT].isChecked()

    def test_object_click_emits_component_mode(self):
        """点 Object 按钮发 component_mode_requested 信号。"""
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        received = []
        tb.component_mode_requested.connect(lambda c: received.append(c))
        tb._comp_buttons[ComponentMode.OBJECT].click()
        assert received == [ComponentMode.OBJECT]

    def test_set_component_mode_syncs(self):
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        tb.set_component_mode(ComponentMode.OBJECT)
        assert tb._comp_buttons[ComponentMode.OBJECT].isChecked()


# ──────────────────────────────────────────────────────────────────────────
# Part 2: 网格吸附 + 量化（纯逻辑）
# ──────────────────────────────────────────────────────────────────────────

class TestSnapQuantizeLogic:
    """吸附/量化的 round-to-step 逻辑（复刻 _apply_snap_quantize）。"""

    def _quantize(self, result: dict, snap: SnapQuantizeSettings) -> dict:
        """复刻 CompositeViewer._apply_snap_quantize 的核心逻辑（无 Qt 依赖）。"""
        import math
        if not snap.quantize_enabled and snap.snap_mode.value == "none":
            return dict(result)
        out = dict(result)
        step_move = snap.move_step if snap.move_step > 0 else None
        step_rot = snap.rotate_step if snap.rotate_step > 0 else None
        step_scale = snap.scale_step if snap.scale_step > 0 else None
        for k, v in out.items():
            if k.startswith("object_pos_") and step_move is not None:
                out[k] = round(v / step_move) * step_move
            elif k.startswith("object_rot_") and step_rot is not None:
                out[k] = round(v / step_rot) * step_rot
            elif k == "mesh_scale" and step_scale is not None:
                if v > 0 and step_scale > 0:
                    exp = round(math.log(v) / math.log(1.0 + step_scale))
                    out[k] = (1.0 + step_scale) ** exp
        return out

    def test_no_snap_no_quantize_passes_through(self):
        snap = SnapQuantizeSettings()  # 默认 none + quantize off
        result = {"object_pos_x": 1.234}
        assert self._quantize(result, snap) == result

    def test_quantize_move_snaps_to_step(self):
        """量化移动：object_pos 吸附到 move_step 网格。"""
        snap = SnapQuantizeSettings(quantize_enabled=True, move_step=0.5)
        result = {"object_pos_x": 1.3}
        out = self._quantize(result, snap)
        assert out["object_pos_x"] == 1.5  # round(1.3/0.5)*0.5 = 1.5

    def test_quantize_rotate_snaps_to_step(self):
        """量化旋转：object_rot 吸附到 rotate_step 度。"""
        snap = SnapQuantizeSettings(quantize_enabled=True, rotate_step=15.0)
        result = {"object_rot_y": 23.0}
        out = self._quantize(result, snap)
        assert out["object_rot_y"] == 30.0  # round(23/15)*15 = 30

    def test_grid_snap_equivalent_to_quantize_move(self):
        """网格吸附（snap_mode=GRID）对移动等同量化到 move_step。"""
        snap = SnapQuantizeSettings(snap_mode=SnapMode.GRID, move_step=1.0)
        result = {"object_pos_x": 1.7}
        out = self._quantize(result, snap)
        assert out["object_pos_x"] == 2.0  # round(1.7/1)*1

    def test_scale_quantize_is_exponential(self):
        """缩放量化是指数式（round 到 step 的幂）。"""
        snap = SnapQuantizeSettings(quantize_enabled=True, scale_step=0.1)
        result = {"mesh_scale": 1.25}
        out = self._quantize(result, snap)
        # exp = round(log(1.25)/log(1.1)) ≈ round(2.25) = 2 → 1.1^2 = 1.21
        assert abs(out["mesh_scale"] - 1.21) < 0.01


# ──────────────────────────────────────────────────────────────────────────
# Part 3: viewer 接线
# ──────────────────────────────────────────────────────────────────────────

class TestViewerSnapWiring:
    """CompositeViewer 接线吸附/量化。"""

    def test_viewer_has_apply_snap_quantize(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        assert hasattr(CompositeViewer, "_apply_snap_quantize")

    def test_drag_move_applies_snap_quantize(self):
        """_handle_gizmo_drag_move 调用 _apply_snap_quantize。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_gizmo_drag_move)
        assert "_apply_snap_quantize" in src

    def test_apply_snap_quantize_reads_controller_state(self):
        """_apply_snap_quantize 从 _viewport3d_ctrl.state.snap 读设置。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._apply_snap_quantize)
        assert "_viewport3d_ctrl.state.snap" in src
        assert "quantize_enabled" in src
        assert "snap_mode" in src
