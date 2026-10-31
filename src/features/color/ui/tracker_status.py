"""Tracker 面板状态灯与面板状态枚举（阶段 5，计划 §5.2/§5.8）。"""

from __future__ import annotations

from enum import Enum

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QWidget
from src.shared.i18n.i18n import tr


class PanelState(Enum):
    """Tracker 面板四态（+部分失败）。"""

    EMPTY = "empty"            # 无跟踪数据：提示选择蒙版窗口
    RUNNING = "running"        # 跟踪进行中：参数禁用
    SUCCESS = "success"        # 成功：绿色灯 + 统计
    PARTIAL_FAILED = "partial" # 部分失败：黄色灯，曲线断开
    FAILED = "failed"          # 当前帧失败：红色灯 + 原因


# 状态灯颜色（计划 §5.2：灰/青绿/黄/红）
LIGHT_COLORS = {
    "none": QColor(0x66, 0x66, 0x66),      # 无跟踪数据
    "valid": QColor(0x00, 0xD9, 0xA6),    # 有效跟踪
    "weak": QColor(0xFF, 0xD4, 0x3B),     # 存在 weak 帧
    "failed": QColor(0xFF, 0x4D, 0x5E),   # 当前帧失败
}

# 面板状态 → 状态灯颜色映射
STATE_LIGHT = {
    PanelState.EMPTY: "none",
    PanelState.RUNNING: "valid",
    PanelState.SUCCESS: "valid",
    PanelState.PARTIAL_FAILED: "weak",
    PanelState.FAILED: "failed",
}


class TrackerStatusLight(QWidget):
    """8 px 圆形状态灯。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._color = LIGHT_COLORS["none"]
        self.setFixedSize(QSize(12, 12))
        self.setToolTip(tr("tracker.no_data", "无跟踪数据"))

    def set_status(self, status: str) -> None:
        color = LIGHT_COLORS.get(status, LIGHT_COLORS["none"])
        if color == self._color:
            return
        self._color = color
        self.update()

    def set_from_state(self, state: PanelState) -> None:
        self.set_status(STATE_LIGHT[state])

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        r = 4.0
        cx, cy = self.width() / 2.0, self.height() / 2.0
        painter.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))
