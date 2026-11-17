"""G3：shutdown_timer 接管——独立 utility window 与明确意图的系统动作。

G 节行验收：**启动只开窗口；系统动作有明确意图，取消/退出无遗留倒计时**。

* 启动只开窗口——command dispatch 仅显示工具窗口，**不**自动开始倒计时
  也不执行任何系统动作；
* 系统动作有明确意图——倒计时到零进入 ``awaiting_confirmation``，必须
  用户点击「立即执行」才触发动作（取消即回到空闲）；动作本体是宿主
  注入的显式命令（无 shell 拼接）；
* 取消/退出无遗留倒计时——窗口关闭/控制器关停即取消并停表：服务状态
  回 ``idle``，再次打开不残留旧倒计时（single instance/owner 经 F5
  WindowRegistry，二次打开聚焦现有窗口）。
"""
from __future__ import annotations

import threading
from typing import Callable

STATES = ("idle", "counting", "awaiting_confirmation")


class ShutdownTimerService:
    """倒计时状态机（纯域层；tick 由宿主定时器驱动）。"""

    def __init__(self, actions: dict[str, Callable[[], object]] | None = None) -> None:
        self._lock = threading.RLock()
        self._actions = dict(actions or {})
        self._state = "idle"
        self._remaining = 0
        self._action_id = ""

    # ------------------------------------------------------------ 状态查询
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._remaining

    @property
    def armed_action(self) -> str:
        with self._lock:
            return self._action_id

    def available_actions(self) -> tuple[str, ...]:
        return tuple(sorted(self._actions))

    # ------------------------------------------------------------ 状态机
    def arm(self, action_id: str, minutes: int) -> None:
        """明确意图之一：用户选择动作与时长并启动倒计时。"""
        if action_id not in self._actions:
            raise ValueError(f"未知系统动作：{action_id}")
        minutes = int(minutes)
        if minutes <= 0:
            raise ValueError("倒计时长必须是正整数分钟")
        with self._lock:
            self._action_id = action_id
            self._remaining = minutes * 60
            self._state = "counting"

    def tick(self) -> None:
        """每秒推进；到零进入待确认（不自动执行——明确意图之二）。"""
        with self._lock:
            if self._state != "counting":
                return
            self._remaining -= 1
            if self._remaining > 0:
                return
            self._remaining = 0
            self._state = "awaiting_confirmation"

    def confirm(self) -> object:
        """明确意图之三：用户在待确认状态点击执行——动作在此触发。"""
        with self._lock:
            if self._state != "awaiting_confirmation":
                raise RuntimeError("没有待确认的系统动作")
            action = self._actions.get(self._action_id)
            if action is None:
                raise ValueError(f"未知系统动作：{self._action_id}")
            self._state = "idle"
            self._remaining = 0
            self._action_id = ""
        return action()

    def cancel(self) -> None:
        """取消/退出：回空闲、清倒计时——无遗留。"""
        with self._lock:
            self._state = "idle"
            self._remaining = 0
            self._action_id = ""


def system_actions_darwin() -> dict[str, Callable[[], object]]:
    """macOS 系统动作（显式 osascript 列表参数；无 shell 拼接）。

    只在用户明确确认后才会被调用；测试环境请注入假动作，绝不真执行。
    """
    import subprocess

    def _osascript(script: str) -> Callable[[], object]:
        def run() -> object:
            return subprocess.run(
                ["osascript", "-e", script], capture_output=True, timeout=15)
        return run

    return {
        "sleep": _osascript('tell application "System Events" to sleep'),
        "shutdown": _osascript('tell application "System Events" to shut down'),
        "restart": _osascript('tell application "System Events" to restart'),
        "lock_screen": _osascript(
            'tell application "System Events" to keystroke "q" '
            'using {command down, control down}'),
    }


__all__ = ["ShutdownTimerService", "system_actions_darwin", "STATES"]
