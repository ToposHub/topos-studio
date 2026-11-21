"""P0 · Clip Editor 预览 worker（2026-11-19 计划第一阶段）。

重开/切换片段的预览使用与播放同一份已处理数据：按
``(spec, steps, view, fft, generation)`` 作为请求键在后台线程计算
当前/原始预览与波形、频谱、计量（沿用 PCM/预渲染缓存），结果经
Qt 信号回到主线程——UI 只做差分投影。

- 可取消：新请求递增 generation，旧任务的发布被丢弃；
- 同 key 合并：与在途任务键相同直接跳过（避免重复烘焙）；
- 失败清理：job 抛错经 ``preview_failed`` 上报，编辑器保留上一份
  有效预览；
- 可结束：``finish()`` 协作停止线程（编辑器关闭/退出时调用）。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from PyQt6.QtCore import QMutex, QThread, pyqtSignal

import numpy as np

from src.features.mixer.spectrogram import Spectrogram

logger = logging.getLogger(__name__)
_active_workers: set[ClipPreviewWorker] = set()


@dataclass
class ClipPreviewResult:
    """一次预览计算的纯数据结果（主线程差分投影的输入）。"""

    key: tuple
    edited: np.ndarray
    sample_rate: int
    duration: float
    mins: np.ndarray
    maxs: np.ndarray
    rmss: np.ndarray
    spectrogram: Spectrogram | list | None
    report: dict = field(default_factory=dict)
    length_maps: tuple[tuple[int, int, int], ...] = ()


class ClipPreviewWorker(QThread):
    """后台计算 Clip Editor 预览（串行、最新请求优先、同 key 合并）。"""

    preview_ready = pyqtSignal(object)          # ClipPreviewResult
    preview_failed = pyqtSignal(tuple, str)     # key, error

    def __init__(self) -> None:
        super().__init__()
        self.finished.connect(self._release)
        self._mutex = QMutex()
        self._key: Optional[tuple] = None
        self._job: Callable[[Callable[[], bool]], object] | None = None
        self._inflight_key: Optional[tuple] = None
        self._generation = 0
        self._stopped = False
        self._wakeup = threading.Event()

    # ── 主线程接口 ────────────────────────────────────────────────
    def submit(self, key: tuple, job: Callable[[], ClipPreviewResult]) -> None:
        """提交一个预览任务；与在途任务同 key 时合并（不重复计算）。"""
        self.submit_cancellable(key, lambda _cancelled: job())

    def submit_cancellable(self, key: tuple, job: Callable[[Callable[[], bool]], object]) -> None:
        self._mutex.lock()
        try:
            if self._stopped:
                return
            if key == self._key or (self._key is None and key == self._inflight_key):
                return
            self._generation += 1
            self._key = key
            self._job = job
        finally:
            self._mutex.unlock()
        self._wakeup.set()
        if not self.isRunning():
            _active_workers.add(self)
            self.start()

    def cancel(self) -> None:
        """Discard the queued request and signal cancellation to running work."""
        self._mutex.lock()
        try:
            self._generation += 1
            self._key = None
            self._job = None
            self._inflight_key = None
        finally:
            self._mutex.unlock()

    def _release(self) -> None:
        _active_workers.discard(self)

    def finish(self) -> None:
        """协作停止（编辑器关闭；在途结果不再发布）。"""
        self._mutex.lock()
        try:
            self._stopped = True
            self._key = None
            self._job = None
        finally:
            self._mutex.unlock()
        self._wakeup.set()

    # ── 工作线程 ──────────────────────────────────────────────────
    def run(self) -> None:  # noqa: D102 - QThread 入口
        while True:
            self._wakeup.wait()
            self._wakeup.clear()
            while True:
                self._mutex.lock()
                try:
                    if self._stopped:
                        return
                    key, job = self._key, self._job
                    generation = self._generation
                    self._key = None
                    self._job = None
                    self._inflight_key = key
                finally:
                    self._mutex.unlock()
                if job is None:
                    break
                result = None
                error = None

                def cancelled() -> bool:
                    self._mutex.lock()
                    try:
                        return self._stopped or generation != self._generation
                    finally:
                        self._mutex.unlock()

                try:
                    if not cancelled():
                        result = job(cancelled)
                except Exception as exc:  # noqa: BLE001 - 失败上报并清理
                    if not cancelled():
                        logger.warning("clip audio job failed: %s", exc, exc_info=True)
                    error = str(exc)
                finally:
                    self._mutex.lock()
                    try:
                        self._inflight_key = None
                        publish = not self._stopped and generation == self._generation
                    finally:
                        self._mutex.unlock()
                if publish and error is not None:
                    self.preview_failed.emit(key, error)
                if publish and result is not None:
                    self.preview_ready.emit(result)
            if self._stopped:
                return
