"""WorkflowRunController 取消窗口与在跑防护回归测试（AUD-23404 / AUD-23405）。

> 审计发现（batch-234）：

- AUD-23404：``_current_run_id`` 从不清空，第二次 run 起 pending-cancel 机制
  失效——窗口期（panel 已进入 running、新 run_id 尚未由 run.created 回填）用户
  点 Cancel 会把取消发往**已终态的旧 run**，新 run 的取消被静默吞掉。
- AUD-23405：``run()``/``_start_worker`` 无在跑防护，覆盖仍运行中的旧 worker
  引用会析构运行中的 QThread C++ 对象（"QThread: Destroyed while thread is
  still running" 崩溃）。

本文件用注入 fake（runtime / panel）直接驱动控制器内部路径，不起真实线程。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

qtcore = pytest.importorskip("PyQt6.QtCore")

from src.features.ai_harness.invocation_models import RunStatus  # noqa: E402
from src.features.ai_workflow.runtime.workflow_run_controller import (  # noqa: E402
    WorkflowRunController,
)

# QObject/pyqtSignal 不强制需要 QApplication，但保险起见确保实例存在
# （跨线程 signal 派发在无事件循环时退化为直连，不影响本文件的同步断言）。
if qtcore.QCoreApplication.instance() is None:
    _APP = qtcore.QCoreApplication(["test-workflow-run-controller"])
else:  # pragma: no cover - 复用既有实例
    _APP = qtcore.QCoreApplication.instance()


class _FakeRuntime:
    """记录 cancel 调用 + 可注入 run 记录的最小 runtime。"""

    def __init__(self) -> None:
        self.runs: dict[str, Any] = {}
        self.cancel_calls: list[tuple[str, str]] = []

    def add_subscriber(self, callback: Any) -> Any:
        return lambda: None

    def get_run(self, run_id: str) -> Any:
        return self.runs.get(run_id)

    def cancel(self, run_id: str, reason: str = "") -> bool:
        self.cancel_calls.append((run_id, reason))
        return True


class _FakePanel:
    def __init__(self) -> None:
        self.running_states: list[bool] = []
        self.run_drawer_states: list[Any] = []

    def set_running(self, running: bool) -> None:
        self.running_states.append(running)

    def set_run_drawer(self, state: Any) -> None:
        self.run_drawer_states.append(state)


class _FakeWorker:
    """只实现 isRunning() 的最小 worker 桩。"""

    def __init__(self, running: bool) -> None:
        self._running = running

    def isRunning(self) -> bool:  # noqa: N802 — 对齐 QThread API
        return self._running


def _make_controller() -> tuple[WorkflowRunController, _FakeRuntime, _FakePanel]:
    runtime = _FakeRuntime()
    panel = _FakePanel()
    controller = WorkflowRunController(
        runtime=runtime,
        compiler=SimpleNamespace(),   # 本文件不触发 compile 路径
        adapter=SimpleNamespace(),
        panel=panel,
        fake_service=None,            # 让 _start_worker 在装配后安全早退
    )
    return controller, runtime, panel


# ---------------------------------------------------------------------- #
# AUD-23404：pending-cancel 窗口
# ---------------------------------------------------------------------- #


class TestCancelPendingWindow:
    def test_cancel_with_active_run_forwards_to_runtime(self) -> None:
        controller, runtime, _panel = _make_controller()
        controller._current_run_id = "run-1"
        runtime.runs["run-1"] = SimpleNamespace(status=RunStatus.RUNNING)
        controller.cancel()
        assert runtime.cancel_calls == [("run-1", "user_requested")]
        assert controller._cancel_pending is False

    def test_cancel_with_stale_terminal_run_becomes_pending(self) -> None:
        """AUD-23404：指向已终态旧 run 的取消不得发往死 run，必须转 pending。"""
        controller, runtime, _panel = _make_controller()
        controller._current_run_id = "old-run"
        runtime.runs["old-run"] = SimpleNamespace(status=RunStatus.SUCCEEDED)
        controller.cancel()
        assert runtime.cancel_calls == [], "取消不得发往已终态的旧 run"
        assert controller._cancel_pending is True

    def test_cancel_with_unknown_run_id_becomes_pending(self) -> None:
        controller, runtime, _panel = _make_controller()
        controller._current_run_id = "ghost"
        # runtime.get_run 返回 None（run 不存在/已清理）
        controller.cancel()
        assert runtime.cancel_calls == []
        assert controller._cancel_pending is True

    def test_cancel_before_run_id_assigned_is_pending(self) -> None:
        controller, runtime, _panel = _make_controller()
        controller.cancel()
        assert controller._cancel_pending is True
        assert runtime.cancel_calls == []

    def test_start_worker_clears_stale_run_id(self) -> None:
        """AUD-23404：新 run 启动前清空旧 run_id，窗口期取消走 pending 通道。"""
        controller, _runtime, panel = _make_controller()
        controller._current_run_id = "old-run"
        # fake_service=None → _start_worker 在装配检查处安全早退（不起线程）
        controller._start_worker(SimpleNamespace(), SimpleNamespace())
        assert controller._current_run_id == ""
        # panel 先进入 running 再被安全早退复位
        assert panel.running_states == [True, False]


# ---------------------------------------------------------------------- #
# AUD-23405：在跑防护
# ---------------------------------------------------------------------- #


class TestStartWorkerGuard:
    def test_start_worker_refuses_while_previous_running(self) -> None:
        """在跑防护：不得覆盖运行中的 worker 引用（QThread 析构崩溃根因）。"""
        controller, runtime, panel = _make_controller()
        running_worker = _FakeWorker(running=True)
        controller._worker = running_worker
        controller._current_run_id = "run-1"
        runtime.runs["run-1"] = SimpleNamespace(status=RunStatus.RUNNING)
        controller._start_worker(SimpleNamespace(), SimpleNamespace())
        # worker 引用未被替换；panel 未进入 running
        assert controller._worker is running_worker
        assert panel.running_states == []
        # 旧 run 被协作取消
        assert ("run-1", "superseded_by_new_run") in runtime.cancel_calls

    def test_start_worker_proceeds_when_previous_finished(self) -> None:
        controller, _runtime, panel = _make_controller()
        controller._worker = _FakeWorker(running=False)
        controller._start_worker(SimpleNamespace(), SimpleNamespace())
        # 旧 worker 已结束 → 允许新 run（此处因 fake_service=None 安全早退）
        assert True in panel.running_states
        assert False in panel.running_states

    def test_refused_start_pushes_diagnostic(self) -> None:
        controller, runtime, panel = _make_controller()
        controller._worker = _FakeWorker(running=True)
        controller._start_worker(SimpleNamespace(), SimpleNamespace())
        # 诊断进 Run Drawer（RUN_ALREADY_ACTIVE）；若 UI 模块不可导入则跳过断言
        from src.features.edit.ui.ai_assistant_composer import RunDrawerState
        drawer = next(
            (s for s in panel.run_drawer_states if isinstance(s, RunDrawerState)),
            None,
        )
        assert drawer is not None, "拒绝并发启动必须给出可定位诊断"
        assert any(
            item.error_code == "RUN_ALREADY_ACTIVE" for item in drawer.items
        )
