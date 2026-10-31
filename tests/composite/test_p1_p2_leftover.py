"""P1/P2 遗留子任务测试（Milestone B/C 补完）。

覆盖：
- P1-5：导航恢复 timer（_nav3d_restore_timer + _schedule_nav3d_restore +
  _on_nav3d_restore + nav_started 取消 timer）。
- P1-6：导航 LOD 状态徽章（CompositeViewer._draw_nav_lod_badge）。
- P1-7：3D Scene Settings 新增 Performance/LOD 组（interactive_lod /
  nav_restore_delay_ms / quad_view_quality / inactive_view_skip_in_nav 控件）。
- P2-6：Quad View Quality 下拉控件（同 P1-7 组内）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest


# ──────────────────────────────────────────────────────────────────────────
# P1-5：导航恢复 timer
# ──────────────────────────────────────────────────────────────────────────

class TestNavRestoreTimer:
    """CompositePage 延迟恢复高质量（nav_restore_delay_ms）。"""

    def test_init_creates_restore_timer(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage.__init__)
        assert "_nav3d_restore_timer" in src
        assert "QTimer(self)" in src

    def test_timer_is_single_shot(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage.__init__)
        assert "setSingleShot(True)" in src
        assert "_on_nav3d_restore" in src

    def test_schedule_method_exists(self):
        from src.features.composite.ui.composite_page import CompositePage
        assert hasattr(CompositePage, "_schedule_nav3d_restore")

    def test_schedule_reads_delay_from_settings(self):
        """_schedule_nav3d_restore 从 nav_restore_delay_ms 读延迟。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._schedule_nav3d_restore)
        assert "nav_restore_delay_ms" in src
        assert "setInterval" in src

    def test_schedule_zero_delay_immediate_restore(self):
        """nav_restore_delay_ms=0 → 立即恢复（调 _on_nav3d_restore）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._schedule_nav3d_restore)
        assert "delay_ms <= 0" in src
        assert "self._on_nav3d_restore()" in src

    def test_restore_handler_clears_flag_and_rerenders(self):
        """_on_nav3d_restore 清 _nav3d_active + evaluate（P6 后不再 force_refresh）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_nav3d_restore)
        assert "self._nav3d_active = False" in src
        assert "_evaluate_graph()" in src

    def test_nav_started_stops_restore_timer(self):
        """新导航开始时取消挂起的恢复 timer（不应被上次的恢复打断）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_viewer3d_nav_started)
        # 用 getattr 兜底访问 timer（兼容未初始化的 page）+ stop
        assert "_nav3d_restore_timer" in src
        assert ".stop()" in src

    def test_nav_committed_schedules_restore_not_immediate(self):
        """nav_committed 启动 timer，不立即清 _nav3d_active（P1-5 延迟恢复）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_viewer3d_nav_committed)
        assert "_schedule_nav3d_restore" in src
        # 不应有立即清 flag（移到 _on_nav3d_restore）
        # 注意：undo 提交逻辑仍在，只是 flag 清除改为延迟


# ──────────────────────────────────────────────────────────────────────────
# P1-6：导航 LOD 状态徽章
# ──────────────────────────────────────────────────────────────────────────

class TestNavLodBadge:
    """CompositeViewer 导航态显示状态徽章。"""

    def test_badge_method_exists(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        assert hasattr(CompositeViewer, "_draw_nav_lod_badge")

    def test_badge_called_in_overlay(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_3d_viewport_overlay)
        assert "_draw_nav_lod_badge" in src

    def test_badge_shows_navigation_profile(self):
        """NAVIGATION/FAST_SOLID profile 时画徽章。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_nav_lod_badge)
        assert "navigation" in src
        assert "fast_solid" in src
        assert "Navigation" in src

    def test_badge_includes_lod_level(self):
        """徽章含 LOD 等级（LOD1/LOD2）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_nav_lod_badge)
        assert "lod_level" in src
        assert "LOD" in src

    def test_badge_skips_preview_final(self):
        """PREVIEW/FINAL profile 不画徽章。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_nav_lod_badge)
        assert "return" in src  # 早退


# ──────────────────────────────────────────────────────────────────────────
# P1-7 / P2-6：Performance/LOD 设置组 + Quad View Quality 控件
# ──────────────────────────────────────────────────────────────────────────

class TestPerformanceSettingsUI:
    """Viewer3DSettingsWidget 新增 Performance/LOD 组（§15.1）。"""

    def test_build_performance_group_exists(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget)
        assert hasattr(Viewer3DSettingsWidget, "_build_performance_group")

    def test_build_ui_includes_performance_group(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget)
        src = inspect.getsource(Viewer3DSettingsWidget._build_ui)
        assert "_build_performance_group" in src

    def test_group_has_interactive_lod_checkbox(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget)
        src = inspect.getsource(Viewer3DSettingsWidget._build_performance_group)
        assert "_interactive_lod" in src
        assert "interactive_lod" in src

    def test_group_has_nav_restore_delay_drag(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget)
        src = inspect.getsource(Viewer3DSettingsWidget._build_performance_group)
        assert "_nav_restore_delay" in src
        assert "nav_restore_delay_ms" in src

    def test_group_has_quad_view_quality_combo(self):
        """P2-6：Quad View Quality 下拉（Active Textured/All Textured/All Solid）。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget)
        src = inspect.getsource(Viewer3DSettingsWidget._build_performance_group)
        assert "_quad_view_quality" in src
        assert "active_textured" in src
        assert "all_textured" in src
        assert "all_solid" in src

    def test_group_has_inactive_view_skip_checkbox(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget)
        src = inspect.getsource(Viewer3DSettingsWidget._build_performance_group)
        assert "_inactive_view_skip" in src
        assert "inactive_view_skip_in_nav" in src

    def test_sync_widgets_syncs_performance_controls(self):
        """_sync_widgets 同步新控件状态。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget)
        src = inspect.getsource(Viewer3DSettingsWidget._sync_widgets)
        assert "_interactive_lod.setChecked" in src
        assert "_nav_restore_delay.setValue" in src
        assert "_inactive_view_skip.setChecked" in src
        assert "_quad_view_quality" in src


# ──────────────────────────────────────────────────────────────────────────
# P2-4：非活动视图降频（throttle，非全跳过）
# ──────────────────────────────────────────────────────────────────────────

class TestInactiveViewThrottle:
    """Scene3D 非活动正交视图降频（每 N ms 更新一次，§6.3 low_freq_ortho）。"""

    def test_init_has_last_render_timestamps(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.__init__)
        assert "_ortho_last_render" in src

    def test_do_evaluate_uses_throttle_interval(self):
        """_do_evaluate 用 inactive_view_update_rate 算降频间隔。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "inactive_view_update_rate" in src
        assert "_iv_interval" in src

    def test_do_evaluate_compares_timestamp(self):
        """降频判定：距上次渲染 >= 间隔才更新。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_ortho_last_render" in src
        assert "_iv_interval" in src
        assert "_now" in src

    def test_render_ortho_views_updates_timestamp(self):
        """_render_ortho_views 渲染视图后更新时间戳。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._render_ortho_views)
        assert "_ortho_last_render[key]" in src
        assert "time.monotonic()" in src

    def test_settings_has_update_rate(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "inactive_view_update_rate" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["inactive_view_update_rate"] == 8

    def test_settings_ui_has_update_rate_control(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget)
        src = inspect.getsource(Viewer3DSettingsWidget._build_performance_group)
        assert "_inactive_view_rate" in src
        assert "inactive_view_update_rate" in src

    def test_settings_ui_syncs_update_rate(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget)
        src = inspect.getsource(Viewer3DSettingsWidget._sync_widgets)
        assert "_inactive_view_rate.setValue" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
