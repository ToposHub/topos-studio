"""E04: 时间线错误反馈到状态栏。

审计实测：``TimelineView.status_message_requested`` 有 18 个发射点但全仓
0 个接收者——锁轨、撤销事务拒绝、Slip/Slide 拒绝等错误只进日志。

回归锁定：
- MainWindow 源码连接该信号 → _on_timeline_status_message；
- 处理器把消息写状态栏（5s 超时语义）+ 日志。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.ui.timeline_view import TimelineView

_MAIN_WINDOW = (
    Path(__file__).resolve().parents[2] / "src" / "app" / "main_window.py"
)


class TestHostWiring:
    def test_main_window_connects_status_message(self) -> None:
        text = _MAIN_WINDOW.read_text(encoding="utf-8")
        assert "status_message_requested.connect(" in text
        assert "_on_timeline_status_message" in text

    def test_signal_still_declared(self) -> None:
        assert "status_message_requested" in vars(TimelineView)


class TestHandlerBehavior:
    def test_message_reaches_status_bar(self) -> None:
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        shown: list[tuple[str, int]] = []
        ctrl.status_bar = SimpleNamespace(
            showMessage=lambda msg, timeout=5000: shown.append((msg, timeout)))
        ctrl._on_timeline_status_message("Track is locked; trim rejected")
        assert shown == [("Track is locked; trim rejected", 5000)]

    def test_status_bar_missing_does_not_raise(self) -> None:
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        # 无 status_bar/statusBar 属性 → 静默降级，不抛
        ctrl._on_timeline_status_message("whatever")
