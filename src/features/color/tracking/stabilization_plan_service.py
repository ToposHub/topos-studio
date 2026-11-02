"""Background camera-path smoothing; late jobs never overwrite newer edits."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from src.shared.tracking.camera_stabilization import compute_stabilization_plan
from src.shared.tracking.types import TrackTransform


class _Signals(QObject):
    ready = pyqtSignal(int, object, object)
    failed = pyqtSignal(int, str, object)


class _PlanJob(QRunnable):
    def __init__(self, signals: _Signals, generation: int,
                 transforms: Mapping[int, TrackTransform], size: tuple[int, int],
                 options: dict[str, Any], fps: float, metadata: dict[str, Any]) -> None:
        super().__init__()
        self.signals = signals
        self.generation = generation
        self.transforms = dict(transforms)
        self.size, self.options, self.fps, self.metadata = size, dict(options), fps, metadata

    def run(self) -> None:
        try:
            plan = compute_stabilization_plan(self.transforms, self.size, fps=self.fps, **self.options)
        except (ValueError, ArithmeticError) as exc:
            self.signals.failed.emit(self.generation, str(exc), self.metadata)
        else:
            self.signals.ready.emit(self.generation, plan, self.metadata)


class StabilizationPlanService(QObject):
    """Own CPU jobs separately from decoding and from the GL render queue."""

    ready = pyqtSignal(object, object)
    failed = pyqtSignal(str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._generation = 0
        self._pending = False
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _Signals(self)
        self._signals.ready.connect(self._on_ready)
        self._signals.failed.connect(self._on_failed)

    def submit(self, transforms: Mapping[int, TrackTransform], size: tuple[int, int],
               options: dict[str, Any], fps: float, metadata: dict[str, Any]) -> None:
        self.invalidate()
        self._pending = True
        self._pool.start(_PlanJob(self._signals, self._generation, transforms, size, options, fps, metadata))

    def invalidate(self) -> None:
        self._generation += 1
        self._pending = False
        self._pool.clear()

    def is_pending(self) -> bool:
        return self._pending

    def _on_ready(self, generation: int, plan: object, metadata: object) -> None:
        if generation == self._generation:
            self._pending = False
            self.ready.emit(plan, metadata)

    def _on_failed(self, generation: int, message: str, metadata: object) -> None:
        if generation == self._generation:
            self._pending = False
            self.failed.emit(message, metadata)

    def shutdown(self) -> None:
        self.invalidate()
        self._pool.waitForDone()
