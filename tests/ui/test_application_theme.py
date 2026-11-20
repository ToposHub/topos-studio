"""Verify the application theme in an isolated Qt process."""

import os
from pathlib import Path
import subprocess
import sys
from textwrap import dedent


def test_application_theme_replaces_external_style_and_paints_matching_tabs() -> None:
    script = dedent(
        """
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QFontInfo, QPalette, QTextLayout
        from PyQt6.QtWidgets import (
            QApplication, QDockWidget, QLabel, QMainWindow,
            QTabBar, QTabWidget, QToolButton, QWidget,
        )

        from src.shared.ui.dock_tab_style import apply_dock_tab_bar_style
        from src.shared.ui.style_sheet import C, UI_FONT_FAMILIES, apply_application_theme

        def font_families(widget: QWidget) -> set[str]:
            widget.ensurePolished()
            layout = QTextLayout("Topos 剪辑 调色 属性 示波器 节点 监听 渲染 交付 素材", widget.font())
            layout.beginLayout()
            layout.createLine().setLineWidth(1000)
            layout.endLayout()
            return {run.rawFont().familyName() for run in layout.glyphRuns()}

        app = QApplication([])
        apply_application_theme(app)
        stylesheet = app.styleSheet()
        app.setStyleSheet("")
        assert app.style().name() == "fusion"
        assert app.palette().color(QPalette.ColorRole.Highlight).name() == C.ACCENT_SURFACE
        assert QFontInfo(app.font()).family() == UI_FONT_FAMILIES[0]
        app.setStyleSheet(stylesheet)

        label = QLabel("示波器")
        panel_button = QToolButton()
        panel_button.setText("属性")
        panel_button.setStyleSheet("font-weight: bold;")
        assert font_families(label) == set(UI_FONT_FAMILIES)
        assert font_families(panel_button) == set(UI_FONT_FAMILIES)

        sizes = []
        for dock_tab in (False, True):
            bar = QTabBar()
            bar.addTab("Topos 素材")
            bar.addTab("剪辑")
            bar.setExpanding(False)
            if dock_tab:
                apply_dock_tab_bar_style(bar)
            bar.resize(240, 34)
            bar.show()
            app.processEvents()
            assert font_families(bar) == set(UI_FONT_FAMILIES)
            image = bar.grab().toImage()
            rect = bar.tabRect(0)
            group = rect.united(bar.tabRect(1))
            assert abs(2 * group.left() + group.width() - bar.width()) <= 1
            scale = image.devicePixelRatio()
            x = round((rect.left() + 8) * scale)
            y = round((rect.top() + rect.height() // 2) * scale)
            assert image.pixelColor(x, y).name() == C.ACCENT_SURFACE
            sizes.append(rect.size())
            bar.close()
        assert sizes[0] == sizes[1]

        window = QMainWindow()
        dock = QDockWidget("Tools", window)
        tabs = QTabWidget(dock)
        tabs.addTab(QWidget(), "曲线")
        tabs.addTab(QWidget(), "蒙版")
        corner = QToolButton(tabs)
        corner.setText("选项")
        tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        dock.setWidget(tabs)
        window.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, dock)
        window.resize(600, 180)
        window.show()
        app.processEvents()
        group = tabs.tabBar().geometry()
        assert abs(2 * group.left() + group.width() - corner.x()) <= 2
        assert group.right() < corner.x()
        assert corner.geometry().right() == tabs.width() - 1
        window.close()
        """
    )
    child = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_STYLE_OVERRIDE": "Windows"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 0, child.stderr
