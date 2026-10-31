# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Viewport3D Toolbar — C4D 风格 3D 工具栏（Phase 4）。

仅 3D 模式可见的工具栏：Select / Move / Rotate / Scale 按钮（可选中互斥），
写入 Viewer3DToolMode。对应 audit plan "Toolbar Implementation Plan"。

设计：
- 水平排列的 QToolButton 组，选中态高亮（C4D 橙）。
- 点击发 tool_mode_requested(Viewer3DToolMode) 信号；CompositeViewer/Controller
  据此更新 state + 显隐对应 gizmo（Phase 5 完整 gizmo）。
- 由 CompositeViewer 在 3D 模式时显示，2D 时隐藏。
- 组件模式（Point/Edge/Polygon/Model）分段 Phase 6 接线（此处先建 Model 默认）。

Phase 4 范围：工具模式按钮 + 选中态。Rotate/Scale 的实际 gizmo 在 Phase 5。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QToolButton, QWidget)

from src.features.composite.ui.viewport3d_state import (
    Viewer3DToolMode, ComponentMode, TOOL3D_MODE_LABELS, TOOL3D_MODE_SHORTCUTS)

# 选中态高亮（C4D 橙）
_TOOL_BTN_STYLE = """
    QToolButton {
        background-color: transparent;
        border: 1px solid #555;
        border-radius: 3px;
        padding: 3px 8px;
        color: #ccc;
        font-size: 11px;
    }
    QToolButton:hover {
        background-color: rgba(255, 255, 255, 30);
        color: white;
    }
    QToolButton:checked {
        background-color: rgba(255, 170, 40, 80);
        border-color: #ffaa28;
        color: #ffd084;
    }
    QToolButton:disabled {
        color: #555;
        border-color: #333;
    }
"""

# 组件模式标签
_COMPONENT_LABELS = {
    ComponentMode.OBJECT: "Model",
    ComponentMode.POINT: "Point",
    ComponentMode.EDGE: "Edge",
    ComponentMode.POLYGON: "Polygon",
}

# 组件模式启用状态（Point/Edge/Polygon 需 mesh picking，Phase 6 标记 disabled）
_COMPONENT_ENABLED = {
    ComponentMode.OBJECT: True,
    ComponentMode.POINT: True,     # P1-22：BVH face hit → barycentric vertex
    ComponentMode.EDGE: True,      # P1-22：BVH face hit → stable canonical edge
    ComponentMode.POLYGON: True,   # P7-5/P1-22：shared BVH face hit
}


class Viewport3DToolbar(QFrame):
    """3D 视口工具栏（Select/Move/Rotate/Scale，可选中互斥）。

    Signals:
        tool_mode_requested(Viewer3DToolMode): 用户点了某工具按钮。
    """

    tool_mode_requested = pyqtSignal(object)  # Viewer3DToolMode
    # Phase 6: 组件模式切换（Object/Point/Edge/Polygon）
    component_mode_requested = pyqtSignal(object)  # ComponentMode

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("viewport3dToolbar")
        self.setFrameShape(QFrame.Shape.NoFrame)
        # 工具栏按工具顺序排列
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(3)

        # 互斥按钮组（选中一个自动取消其它）
        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)
        self._buttons: dict = {}  # Viewer3DToolMode -> QToolButton

        for mode in (Viewer3DToolMode.SELECT, Viewer3DToolMode.MOVE,
                     Viewer3DToolMode.ROTATE, Viewer3DToolMode.SCALE):
            btn = QToolButton()
            btn.setText(TOOL3D_MODE_LABELS[mode])
            btn.setCheckable(True)
            btn.setStyleSheet(_TOOL_BTN_STYLE)
            btn.setFixedHeight(22)
            shortcut = TOOL3D_MODE_SHORTCUTS[mode]
            btn.setToolTip(f"{TOOL3D_MODE_LABELS[mode]} ({shortcut})")
            btn.clicked.connect(lambda _checked=False, m=mode:
                                self.tool_mode_requested.emit(m))
            self._btn_group.addButton(btn)
            self._buttons[mode] = btn
            layout.addWidget(btn)

        # ── Phase 6: 组件模式分段（Object/Point/Edge/Polygon）──
        # 用竖线分隔工具模式与组件模式（视觉分组）
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedWidth(2)
        sep.setStyleSheet("color: #444;")
        layout.addWidget(sep)

        self._comp_btn_group = QButtonGroup(self)
        self._comp_btn_group.setExclusive(True)
        self._comp_buttons: dict = {}  # ComponentMode -> QToolButton
        for cm in (ComponentMode.OBJECT, ComponentMode.POINT,
                   ComponentMode.EDGE, ComponentMode.POLYGON):
            btn = QToolButton()
            btn.setText(_COMPONENT_LABELS[cm])
            btn.setCheckable(True)
            btn.setStyleSheet(_TOOL_BTN_STYLE)
            btn.setFixedHeight(22)
            # Point/Edge/Polygon 需 mesh picking，Phase 6 暂禁用
            enabled = _COMPONENT_ENABLED[cm]
            btn.setEnabled(enabled)
            tip = _COMPONENT_LABELS[cm]
            if not enabled:
                tip += "（待 mesh picking 实现）"
            btn.setToolTip(tip)
            if enabled:
                btn.clicked.connect(lambda _checked=False, c=cm:
                                    self.component_mode_requested.emit(c))
            self._comp_btn_group.addButton(btn)
            self._comp_buttons[cm] = btn
            layout.addWidget(btn)

        # 默认选中 SELECT + OBJECT
        self._buttons[Viewer3DToolMode.SELECT].setChecked(True)
        self._comp_buttons[ComponentMode.OBJECT].setChecked(True)
        # 默认隐藏（仅 3D 模式显示）
        self.hide()

    # ── 状态同步 ──

    def set_tool_mode(self, mode: Viewer3DToolMode) -> None:
        """同步工具栏选中态（不触发 clicked 信号，避免递归）。"""
        btn = self._buttons.get(mode)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    def set_component_mode(self, mode: ComponentMode) -> None:
        """同步组件模式选中态（Phase 6）。"""
        btn = self._comp_buttons.get(mode)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    def show_for_3d(self) -> None:
        """3D 模式时显示工具栏。"""
        self.show()

    def hide_for_2d(self) -> None:
        """2D 模式时隐藏工具栏（并重置到 SELECT）。"""
        self.hide()
        self.set_tool_mode(Viewer3DToolMode.SELECT)
