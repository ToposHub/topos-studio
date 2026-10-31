"""第六轮审计 U06 回归（关窗部分）：GPU renderer 停止判定以线程真实状态为准。

审计实测：正常关窗进程退出码 0（线程已退出），但日志误报"GPU 渲染线程
未在 2000ms 内退出"。根因是 ``EditGpuPreviewRenderer.stop()`` 只置停止
标志并返回 None，而 ``_stop_gpu_renderer_deferred`` 把 falsy 当失败。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.app.controllers.file_controller import (
    _LEAKED_GPU_OBJECTS,
    _stop_gpu_renderer_deferred,
)


class _OldContractRenderer:
    """旧契约 stop()：置标志、返回 None（EditGpuPreviewRenderer 同型）。

    内部线程在 stop 后 ≤200ms 自行退出（模拟真实 GL 线程收尾）。
    """

    def __init__(self, exit_delay: float = 0.05):
        self._running = True
        self._exit_delay = exit_delay
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        deadline = time.monotonic() + 5.0
        while self._running and time.monotonic() < deadline:
            time.sleep(0.01)

    def stop(self) -> None:
        self._running = False

    def wait(self, timeout_ms: int) -> bool:
        self.thread.join(timeout=timeout_ms / 1000.0)
        return not self.thread.is_alive()


@pytest.fixture(autouse=True)
def _clean_leak_list():
    before = list(_LEAKED_GPU_OBJECTS)
    yield
    _LEAKED_GPU_OBJECTS[:] = before


def test_old_contract_stop_not_reported_as_leak():
    renderer = _OldContractRenderer(exit_delay=0.05)
    _stop_gpu_renderer_deferred(renderer, 2000)
    assert renderer not in _LEAKED_GPU_OBJECTS, \
        "stop() 返回 None 且线程实际退出时不得误报泄漏"


def test_genuinely_stuck_thread_still_reported():
    class _StuckRenderer(_OldContractRenderer):
        def stop(self) -> None:
            self._running = True  # 卡死：忽略停止请求

    renderer = _StuckRenderer(exit_delay=0.0)
    try:
        _stop_gpu_renderer_deferred(renderer, 200)
        assert renderer in _LEAKED_GPU_OBJECTS, "真实未退出的线程仍须上报推迟销毁"
    finally:
        renderer._running = False
        renderer.thread.join(timeout=2.0)


def test_bool_contract_unchanged():
    class _BoolRenderer:
        def stop(self, timeout_ms: int) -> bool:
            return True

    _stop_gpu_renderer_deferred(_BoolRenderer(), 1000)
    assert not _LEAKED_GPU_OBJECTS
