"""G3：shutdown_timer 独立 utility window（single instance/owner）。

启动只开窗口（不自动倒计时/不执行系统动作）；倒计时到零需明确确认才
执行；关闭即取消（无遗留倒计时——QTimer 停止 + 服务回空闲）。
"""
from __future__ import annotations

import sys

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from src.features.files.shutdown_timer_service import ShutdownTimerService
from src.shared.ui.style_sheet import C


def default_actions() -> dict:
    if sys.platform == "darwin":
        from src.features.files.shutdown_timer_service import (
            system_actions_darwin,
        )
        return system_actions_darwin()
    return {"sleep": lambda: None}   # 非 macOS：显式空动作（如实降级）


class ShutdownTimerWindow(QDialog):
    """定时关机工具窗口：arm → tick → 明确确认执行 / 取消。"""

    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *,
                 service: ShutdownTimerService) -> None:
        super().__init__(parent)
        self._service = service
        self.setWindowTitle("定时关机")
        self.resize(380, 220)
        self.setStyleSheet(
            f"QDialog {{ background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; }}")
        root = QVBoxLayout(self)
        self.status = QLabel("选择动作与时长后启动倒计时。")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        row = QHBoxLayout()
        self.action = QComboBox()
        for action_id in service.available_actions():
            self.action.addItem({"sleep": "睡眠", "shutdown": "关机",
                                 "restart": "重启", "lock_screen": "锁屏"}.get(
                                     action_id, action_id), action_id)
        row.addWidget(self.action)
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 480)
        self.minutes.setValue(30)
        row.addWidget(self.minutes)
        root.addLayout(row)
        actions = QHBoxLayout()
        self.start = QPushButton("启动倒计时")
        self.start.setObjectName("pluginPrimary")
        self.start.clicked.connect(self._arm)
        self.cancel = QPushButton("取消倒计时")
        self.cancel.clicked.connect(self._cancel)
        self.cancel.setEnabled(False)
        self.execute_now = QPushButton("立即执行")
        self.execute_now.clicked.connect(self._confirm)
        self.execute_now.setVisible(False)
        actions.addWidget(self.start)
        actions.addWidget(self.cancel)
        actions.addWidget(self.execute_now)
        actions.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        actions.addWidget(close)
        root.addLayout(actions)
        # GUI 定时器：窗口关闭即停（无遗留倒计时）
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick)
        self._refresh()

    # ------------------------------------------------------------ 交互
    def _arm(self) -> None:
        try:
            self._service.arm(self.action.currentData() or "sleep",
                              self.minutes.value())
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self._ticker.start()
        self._refresh()

    def _tick(self) -> None:
        self._service.tick()
        if self._service.state == "awaiting_confirmation":
            self._ticker.stop()
        self._refresh()

    def _confirm(self) -> None:
        try:
            self._service.confirm()
            self.status.setText("系统动作已按明确确认执行。")
        except (RuntimeError, ValueError) as exc:
            self.status.setText(str(exc))
        self._refresh()

    def _cancel(self) -> None:
        self._service.cancel()
        self._ticker.stop()
        self._refresh()

    def _refresh(self) -> None:
        state = self._service.state
        counting = state == "counting"
        pending = state == "awaiting_confirmation"
        self.cancel.setEnabled(counting or pending)
        self.start.setEnabled(not counting and not pending)
        self.execute_now.setVisible(pending)
        if counting:
            minutes, seconds = divmod(self._service.remaining, 60)
            self.status.setText(
                f"倒计时中：{minutes:02d}:{seconds:02d} 后请求"
                f"「{self.action.currentText()}」（到点需再确认，不会自动执行）。")
        elif pending:
            self.status.setText(
                "倒计时结束。要执行系统动作请点「立即执行」；取消则回到空闲。")

    # ------------------------------------------------------------ 关闭路径
    def closeEvent(self, event) -> None:  # type: ignore[override]
        # G 节退出验收：取消/退出无遗留倒计时
        self._ticker.stop()
        self._service.cancel()
        self.closed.emit()
        super().closeEvent(event)
