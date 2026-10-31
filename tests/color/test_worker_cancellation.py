"""Cooperative cancellation contracts for non-GL ImageEngine workers."""

from unittest.mock import MagicMock

from src.features.color.core.image_engine import RawUpdateWorker
from src.shared.ai_models.ai_mask_engine import AIMaskWorker


def test_raw_worker_suppresses_result_after_cancel(monkeypatch):
    worker = RawUpdateWorker()
    finished = []
    worker.finished.connect(lambda *args: finished.append(args))

    monkeypatch.setattr(
        "src.shared.io.file_io.FileIO.update_raw_decoding",
        lambda *args, **kwargs: ("full", "high", "low", {"wb_type": "camera"}),
    )
    worker.decode("sample.raw", {})
    assert len(finished) == 1

    worker.request_cancel()
    worker.decode("sample.raw", {})
    assert len(finished) == 1


def test_ai_worker_cancellation_uses_generation_token():
    worker = AIMaskWorker()

    generation = worker._begin_operation()
    assert worker._is_cancelled(generation) is False

    worker.request_cancel()
    assert worker._is_cancelled(generation) is True

    next_generation = worker._begin_operation()
    assert worker._is_cancelled(next_generation) is False


def test_image_engine_shutdown_cancels_workers_before_quit():
    from src.features.color.core.image_engine import ImageEngine

    class _Thread:
        def __init__(self, name):
            self._name = name
            self.quit_called = False

        def isRunning(self):
            return True

        def objectName(self):
            return self._name

        def quit(self):
            self.quit_called = True

        def wait(self, _timeout):
            return True

    engine = ImageEngine.__new__(ImageEngine)
    engine.raw_worker_thread = _Thread("raw")
    engine.ai_worker_thread = _Thread("ai")
    engine.raw_worker = MagicMock()
    engine.ai_worker = MagicMock()

    ImageEngine.shutdown_worker_threads(engine, timeout_ms=1)

    engine.raw_worker.request_cancel.assert_called_once_with()
    engine.ai_worker.request_cancel.assert_called_once_with()
    assert engine.raw_worker_thread.quit_called is True
    assert engine.ai_worker_thread.quit_called is True
