"""Phase 4: C4D 风格视口壳测试。

覆盖 docs/composite_3d_module_audit_optimization_plan_2026-06-29.md 的 Phase 4：
1. 四视图叠加层几何（quad_view_rects 2×2 布局）
2. overlay 仅 QUAD 布局绘制（SINGLE no-op）
3. 3D 工具栏（按钮互斥、选中态、tool_mode_requested 信号、show/hide）
4. viewer 接线（MMB toggle 四视图、paintGL 调 overlay、controller 集成）

注：overlay 用 QRectF 纯几何验证；toolbar 用 QToolButton 真实实例（需 QApplication）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest

from PyQt6.QtCore import QRectF

from src.features.composite.render3d.viewport_overlays import (
    draw_quad_view_overlay, quad_view_rects, render_region_rect,
    _ACTIVE_BORDER_COLOR)
from src.features.composite.ui.viewport3d_state import (
    Viewport3DState, ViewportLayoutMode, ViewportViewKind, Viewer3DToolMode,
    QUAD_VIEW_ORDER, VIEW_KIND_LABELS)
from src.features.composite.ui.viewport3d_controller import Viewport3DController


# ──────────────────────────────────────────────────────────────────────────
# Part 1: 四视图叠加层几何
# ──────────────────────────────────────────────────────────────────────────

class TestQuadViewGeometry:
    """quad_view_rects 计算 2×2 布局的 4 个子矩形。"""

    def test_returns_four_rects(self):
        rects = quad_view_rects(QRectF(0, 0, 800, 600))
        assert len(rects) == 4
        assert set(rects.keys()) == set(QUAD_VIEW_ORDER)

    def test_rects_tile_full_viewport(self):
        """4 个子矩形应铺满整个视口（无重叠无间隙）。"""
        rects = quad_view_rects(QRectF(0, 0, 800, 600))
        # 每个子矩形是 400×300
        for r in rects.values():
            assert r.width() == 400
            assert r.height() == 300
        # 总面积 = 视口面积
        total = sum(r.width() * r.height() for r in rects.values())
        assert total == 800 * 600

    def test_perspective_is_top_left(self):
        """透视视图在左上（C4D 惯例）。"""
        rects = quad_view_rects(QRectF(0, 0, 800, 600))
        persp = rects[ViewportViewKind.PERSPECTIVE]
        assert persp.x() == 0 and persp.y() == 0

    def test_quadrant_positions(self):
        """四个象限位置正确：左上/右上/左下/右下。"""
        rects = quad_view_rects(QRectF(0, 0, 800, 600))
        assert rects[ViewportViewKind.PERSPECTIVE].x() == 0      # 左上
        assert rects[ViewportViewKind.TOP].x() == 400            # 右上
        assert rects[ViewportViewKind.RIGHT].y() == 300          # 左下
        assert rects[ViewportViewKind.FRONT].x() == 400          # 右下
        assert rects[ViewportViewKind.FRONT].y() == 300

    def test_view_kind_labels_complete(self):
        """每个视图类型有显示标签。"""
        for vk in ViewportViewKind:
            assert vk in VIEW_KIND_LABELS
            assert len(VIEW_KIND_LABELS[vk]) > 0

    def test_render_region_rect_fits_project_aspect(self):
        rect = render_region_rect(QRectF(0, 28, 1000, 498), 16.0 / 9.0)

        assert rect.width() == pytest.approx(885.333333, rel=1e-6)
        assert rect.height() == pytest.approx(498.0)
        assert rect.x() == pytest.approx(57.333333, rel=1e-6)
        assert rect.y() == pytest.approx(28.0)


# ──────────────────────────────────────────────────────────────────────────
# Part 2: overlay 绘制门控（仅 QUAD 绘制）
# ──────────────────────────────────────────────────────────────────────────

class TestOverlayGating:
    """draw_quad_view_overlay 仅在 QUAD 布局时绘制。"""

    def test_single_layout_is_noop(self):
        """SINGLE 布局时 draw_quad_view_overlay 不绘制（no-op）。"""
        state = Viewport3DState(layout_mode=ViewportLayoutMode.SINGLE)
        # 用一个 mock painter：SINGLE 时不应调用任何绘制方法
        from unittest.mock import MagicMock
        painter = MagicMock()
        draw_quad_view_overlay(painter, state, QRectF(0, 0, 800, 600))
        # save 总会调，但 drawLine/drawRect/drawText 在 SINGLE 时不应调
        painter.drawLine.assert_not_called()
        painter.drawRect.assert_not_called()

    def test_quad_layout_draws_dividers(self):
        """QUAD 布局时绘制分割线（drawLine 被调用）。"""
        state = Viewport3DState(layout_mode=ViewportLayoutMode.QUAD)
        from unittest.mock import MagicMock
        painter = MagicMock()
        # fontMetrics: horizontalAdvance + height 都是方法
        fm = MagicMock()
        fm.horizontalAdvance.return_value = 50
        fm.height.return_value = 14
        painter.fontMetrics.return_value = fm
        draw_quad_view_overlay(painter, state, QRectF(0, 0, 800, 600))
        # 至少 2 条分割线（十字）
        assert painter.drawLine.call_count >= 2

    def test_quad_layout_draws_active_border(self):
        """QUAD 布局时绘制活动视口边框（drawRect 被调用）。"""
        state = Viewport3DState(layout_mode=ViewportLayoutMode.QUAD,
                                active_view=ViewportViewKind.TOP)
        from unittest.mock import MagicMock
        painter = MagicMock()
        fm = MagicMock()
        fm.horizontalAdvance.return_value = 50
        fm.height.return_value = 14
        painter.fontMetrics.return_value = fm
        draw_quad_view_overlay(painter, state, QRectF(0, 0, 800, 600))
        assert painter.drawRect.call_count >= 1  # 活动边框（+标签背景）

    def test_overlay_does_not_affect_single_mode_viewer(self):
        """viewer 在 SINGLE 模式下不调 overlay 绘制（_draw_3d_viewport_overlay 门控）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_3d_viewport_overlay)
        assert 'is_quad()' in src or 'view_mode != "3d"' in src


# ──────────────────────────────────────────────────────────────────────────
# Part 3: 3D 工具栏
# ──────────────────────────────────────────────────────────────────────────

class TestViewport3DToolbar:
    """3D 工具栏：按钮互斥、选中态、信号、show/hide。"""

    def test_has_four_tool_buttons(self):
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        assert len(tb._buttons) == 4
        for mode in Viewer3DToolMode:
            assert mode in tb._buttons

    def test_default_select_checked(self):
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        assert tb._buttons[Viewer3DToolMode.SELECT].isChecked()

    def test_buttons_mutually_exclusive(self):
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        # 点 Move → 只有 Move 选中
        tb._buttons[Viewer3DToolMode.MOVE].click()
        assert tb._buttons[Viewer3DToolMode.MOVE].isChecked()
        assert not tb._buttons[Viewer3DToolMode.SELECT].isChecked()

    def test_click_emits_tool_mode(self):
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        received = []
        tb.tool_mode_requested.connect(lambda m: received.append(m))
        tb._buttons[Viewer3DToolMode.ROTATE].click()
        assert received == [Viewer3DToolMode.ROTATE]

    def test_set_tool_mode_syncs_without_emit(self):
        """set_tool_mode 同步选中态但不发 clicked（避免递归）。"""
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        received = []
        tb.tool_mode_requested.connect(lambda m: received.append(m))
        tb.set_tool_mode(Viewer3DToolMode.SCALE)
        assert tb._buttons[Viewer3DToolMode.SCALE].isChecked()
        assert received == []  # 程序同步不发信号

    def test_default_hidden(self):
        """工具栏默认隐藏（仅 3D 模式显示）。"""
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        assert not tb.isVisible()

    def test_show_for_3d_shows_toolbar(self):
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        tb.show_for_3d()
        assert tb.isVisible()

    def test_hide_for_2d_resets_to_select(self):
        from src.features.composite.ui.viewport3d_toolbar import Viewport3DToolbar
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tb = Viewport3DToolbar()
        tb.set_tool_mode(Viewer3DToolMode.MOVE)
        tb.hide_for_2d()
        assert not tb.isVisible()
        assert tb._buttons[Viewer3DToolMode.SELECT].isChecked()


# ──────────────────────────────────────────────────────────────────────────
# Part 4: viewer 集成（源码级断言）
# ──────────────────────────────────────────────────────────────────────────

class TestViewerQuadIntegration:
    """CompositeViewer 集成四视图切换 + overlay 绘制。"""

    def test_release_handler_calls_mmb_release(self):
        """_handle_3d_release 在 MMB 释放时调 controller.on_mmb_release（toggle 四视图）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_3d_release)
        assert "on_mmb_release" in src
        assert "MiddleButton" in src

    def test_paintgl_calls_viewport_overlay(self):
        """paintGL 调用 _draw_3d_viewport_overlay（四视图叠加层）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.paintGL)
        assert "_draw_3d_viewport_overlay" in src

    def test_draw_overlay_uses_overlays_module(self):
        """_draw_3d_viewport_overlay 调用 draw_quad_view_overlay（render3d 模块）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_3d_viewport_overlay)
        assert "draw_quad_view_overlay" in src

    def test_mmb_press_records_controller_state(self):
        """3D MMB press 调 controller.on_mmb_press（记录起点区分 click/drag）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.mousePressEvent)
        assert "on_mmb_press" in src
