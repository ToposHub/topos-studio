"""M13.5/13.6 页面入口接线回归（策略徽标 + DPI 恢复路径）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core


class TestMulticamPolicyBadge:
    def _panel(self, qtbot, n_tiles):
        from src.features.edit.ui.multicam_panel import MulticamPanel
        panel = MulticamPanel.__new__(MulticamPanel)
        MulticamPanel.__init__(panel) if False else None
        # 只装配 _tile_target_height 依赖的属性
        panel._tiles = {f"a{i}": NS() for i in range(n_tiles)}
        return panel

    def test_four_angles_full_res_badge_hidden(self, qtbot):
        from PyQt6.QtWidgets import QApplication, QWidget
        app = QApplication.instance() or QApplication([])
        from src.features.edit.ui.multicam_panel import MulticamPanel
        host = QWidget()
        qtbot.addWidget(host)
        panel = MulticamPanel.__new__(MulticamPanel)
        # 走真实 __init__ 以获得徽标（父窗口提供 Qt 环境）
        panel.__init__(host)
        panel._tiles = {f"a{i}": NS() for i in range(4)}
        height = panel._tile_target_height()
        assert height == 96                      # 全分辨率
        assert panel._quality_badge.isHidden()   # 无降档无徽标

    def test_nine_angles_half_res_badge_shown(self, qtbot):
        from PyQt6.QtWidgets import QApplication, QWidget
        app = QApplication.instance() or QApplication([])
        from src.features.edit.ui.multicam_panel import MulticamPanel
        host = QWidget()
        qtbot.addWidget(host)
        panel = MulticamPanel.__new__(MulticamPanel)
        panel.__init__(host)
        panel._tiles = {f"a{i}": NS() for i in range(9)}
        height = panel._tile_target_height()
        assert height == 48                       # 96 × 0.5
        assert not panel._quality_badge.isHidden()
        assert "1/2" in panel._quality_badge.text()


class TestDpiRestoreWiring:
    def test_dead_screen_geometry_restored(self):
        """模型直测经恢复路径同式：窗口全在失效屏（可视比 0）→ 回退
        首个存活屏并钳制。"""
        from src.features.edit.core.multicam_adaptive import (
            dpi_restore_geometry,
        )
        # 失效屏 [0,0,1920,1080]；存活屏 [1920,0,1920,1080]
        restored = dpi_restore_geometry(
            (100, 100, 800, 600),
            [(1920, 0, 1920, 1080)])
        # y 钳制保留（100 在屏内合法——模型只钳越界轴）
        assert restored[0] == 1920 and restored[2:] == (800, 600)

    def test_partially_visible_falls_back(self):
        from src.features.edit.core.multicam_adaptive import (
            dpi_restore_geometry,
        )
        # 窗口仅 20% 在存活屏上（<25% 阈值）→ 视为失效回退
        # 屏 [0,0,1000,1000]，窗口 (990,0,100,1000) → 可视 1%
        restored = dpi_restore_geometry(
            (990, 0, 100, 1000), [(0, 0, 1000, 1000)])
        # 唯一屏即回退屏：钳制到屏内（x 右缘钳制）
        assert restored[0] + restored[2] <= 1000
