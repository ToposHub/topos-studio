"""阶段 3 Qt 测试共享辅助（假 worker、服务事件收集、事件循环驱动）。"""

import time

import numpy as np

from PyQt6.QtCore import QObject, pyqtSignal

from src.features.color.tracking.tracker_2d import TrackResult
from src.features.color.tracking.tracking_service import TrackingService
from src.features.color.tracking.types import (
    TrackStatus,
    TrackTransform,
    TrackingMode,
)

MEDIA = "/tmp/fake_media.mp4"
PATTERN = (0.375, 0.347, 0.25, 0.306)
SEARCH = (0.225, 0.197, 0.55, 0.606)


class FakeWorker(QObject):
    """可控的假 worker：不实际运行，测试手动触发信号。"""

    progress = pyqtSignal(int, int, int)
    finished_with_result = pyqtSignal(int, object)
    failed_with_error = pyqtSignal(int, str)

    def __init__(self, request) -> None:
        super().__init__()
        self.request = request
        self.started = False
        self.cancel_called = False

    def start(self) -> None:
        self.started = True

    def request_cancel(self) -> None:
        self.cancel_called = True

    def wait(self, ms: int = 0) -> bool:
        return True

    def isRunning(self) -> bool:
        return self.started and not self.cancel_called


def make_factory():
    workers = []

    def factory(request):
        w = FakeWorker(request)
        workers.append(w)
        return w

    return factory, workers


def make_result(generation: int, frames=(63, 64, 65), cancelled: bool = False):
    transforms = {
        f: TrackTransform.from_matrix(f, np.eye(3), 0.9, TrackStatus.VALID)
        for f in frames
    }
    return TrackResult(
        generation=generation,
        cancelled=cancelled,
        transforms=transforms,
        observations={},
        diagnostics={},
    )


def make_service():
    factory, workers = make_factory()
    service = TrackingService(worker_factory=factory)
    events = {"started": [], "finished": [], "failed": [], "cancelled": [],
              "progress": [], "stale": []}
    service.task_started.connect(lambda k, g: events["started"].append((k, g)))
    service.task_finished.connect(lambda k, g, r: events["finished"].append((k, g, r)))
    service.task_failed.connect(lambda k, g, m: events["failed"].append((k, g, m)))
    service.task_cancelled.connect(lambda k, g: events["cancelled"].append((k, g)))
    service.task_progress.connect(
        lambda k, g, d, t: events["progress"].append((k, g, d, t))
    )
    service.stale_result_discarded.connect(
        lambda k, g: events["stale"].append((k, g))
    )
    return service, workers, events


def start_default(service, **overrides):
    kwargs = dict(
        clip_id="clip1", shape_id="shape1", media_path=MEDIA,
        pattern_region=PATTERN, search_region=SEARCH,
        reference_frame=62, frame_start=0, frame_end=124,
        mode=TrackingMode.TRANSLATION_ROTATION_SCALE,
    )
    kwargs.update(overrides)
    return service.start(**kwargs)


def pump_until(predicate, qapp, timeout_s: float = 30.0) -> bool:
    """驱动事件循环直到条件成立或超时。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False
