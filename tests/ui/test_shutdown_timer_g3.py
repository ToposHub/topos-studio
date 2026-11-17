"""G3：shutdown_timer 接管——G 节行验收。

**启动只开窗口；系统动作有明确意图，取消/退出无遗留倒计时**；
single instance/owner（F5 WindowRegistry）；零 HTTP（原生接管）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.files.shutdown_timer_service import ShutdownTimerService


def _fake_actions(ran: list[str]) -> dict:
    return {"sleep": lambda: ran.append("sleep"),
            "shutdown": lambda: ran.append("shutdown")}


# ---------------------------------------------------------------------------
# 状态机：arm → tick → 待确认（不自动执行）
# ---------------------------------------------------------------------------

def test_arm_validates_action_and_minutes():
    ran: list[str] = []
    service = ShutdownTimerService(_fake_actions(ran))
    with pytest.raises(ValueError):
        service.arm("halt", 5)            # 未知动作
    with pytest.raises(ValueError):
        service.arm("sleep", 0)           # 非正时长
    service.arm("sleep", 2)
    assert service.state == "counting" and service.remaining == 120


def test_countdown_reaches_awaiting_confirmation_not_execution():
    ran: list[str] = []
    service = ShutdownTimerService(_fake_actions(ran))
    service.arm("shutdown", 1)
    for _ in range(60):
        service.tick()
    assert service.state == "awaiting_confirmation"
    assert ran == []                      # 到零不自动执行——明确意图


def test_confirm_executes_once_and_returns_idle():
    ran: list[str] = []
    service = ShutdownTimerService(_fake_actions(ran))
    service.arm("sleep", 1)
    for _ in range(60):
        service.tick()
    service.confirm()
    assert ran == ["sleep"]               # 明确确认后执行
    assert service.state == "idle"
    with pytest.raises(RuntimeError):
        service.confirm()                 # 无待确认再点=如实拒绝


def test_cancel_any_time_leaves_no_residue():
    ran: list[str] = []
    service = ShutdownTimerService(_fake_actions(ran))
    service.arm("sleep", 10)
    service.tick()
    service.cancel()
    assert service.state == "idle" and service.remaining == 0
    assert service.armed_action == ""     # 无遗留倒计时/动作
    # 到零后取消同样干净
    service.arm("shutdown", 1)
    for _ in range(60):
        service.tick()
    service.cancel()
    assert service.state == "idle"
    with pytest.raises(RuntimeError):
        service.confirm()
    assert ran == []


def test_tick_is_noop_when_idle():
    service = ShutdownTimerService(_fake_actions([]))
    service.tick()
    assert service.state == "idle" and service.remaining == 0


# ---------------------------------------------------------------------------
# 窗口：启动只开窗口；到零出确认按钮；关闭即取消
# ---------------------------------------------------------------------------

def test_window_open_starts_nothing(qtbot):
    from src.app.ui.shutdown_timer_window import ShutdownTimerWindow

    ran: list[str] = []
    window = ShutdownTimerWindow(
        service=ShutdownTimerService(_fake_actions(ran)))
    qtbot.addWidget(window)
    window.show()
    assert window._service.state == "idle"      # 启动只开窗口
    assert not window._ticker.isActive()        # 定时器未启动
    assert ran == []


def test_window_countdown_then_explicit_confirm(qtbot):
    from src.app.ui.shutdown_timer_window import ShutdownTimerWindow

    ran: list[str] = []
    window = ShutdownTimerWindow(
        service=ShutdownTimerService(_fake_actions(ran)))
    qtbot.addWidget(window)
    sleep_index = window.action.findData("sleep")
    window.action.setCurrentIndex(sleep_index)
    window.minutes.setValue(1)                  # 60s
    window._arm()
    assert window._ticker.isActive()
    for _ in range(60):
        window._tick()
    assert window._service.state == "awaiting_confirmation"
    assert not window._ticker.isActive()        # 到零停表
    assert window.execute_now.isVisibleTo(window)
    assert ran == []
    window._confirm()
    assert ran == ["sleep"]


def test_window_close_cancels_no_residue(qtbot):
    from src.app.ui.shutdown_timer_window import ShutdownTimerWindow

    ran: list[str] = []
    window = ShutdownTimerWindow(
        service=ShutdownTimerService(_fake_actions(ran)))
    qtbot.addWidget(window)
    window.minutes.setValue(5)
    window._arm()
    assert window._ticker.isActive()
    window.close()
    assert not window._ticker.isActive()        # 无遗留定时器
    assert window._service.state == "idle"      # 无遗留倒计时
    assert ran == []


# ---------------------------------------------------------------------------
# 宿主接线：single instance + 原生命令（零 HTTP）
# ---------------------------------------------------------------------------
