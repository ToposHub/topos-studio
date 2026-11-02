"""CPU derivation stays off the UI thread and superseded jobs cannot commit."""
from __future__ import annotations

import threading

import numpy as np
import pytest

from src.features.color.tracking.stabilization_plan_service import StabilizationPlanService
from src.shared.tracking.camera_stabilization import StabilizationPlan
from src.shared.tracking.types import TrackTransform, TrackStatus

pytestmark = pytest.mark.usefixtures('qapp')


def test_background_job_and_queued_generation_filter(qtbot, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    worker_threads: list[int] = []

    def compute(transforms, size, *, fps, **options):
        worker_threads.append(threading.get_ident())
        if len(worker_threads) == 1:
            entered.set()
            assert release.wait(3.)
        return StabilizationPlan({}, 1., 1., 2, ())

    monkeypatch.setattr('src.features.color.tracking.stabilization_plan_service.compute_stabilization_plan', compute)
    service = StabilizationPlanService()
    ready: list[dict] = []
    service.ready.connect(lambda plan, metadata: ready.append(metadata))
    try:
        service.submit({}, (64, 48), {}, 25., {'edit': 'old'})
        qtbot.waitUntil(entered.is_set)
        # UI callbacks can execute while the first CPU job is deliberately held.
        service.submit({}, (64, 48), {}, 25., {'edit': 'new'})
        release.set()
        qtbot.waitUntil(lambda: bool(ready))
        assert ready == [{'edit': 'new'}]
        assert all(thread != main_thread for thread in worker_threads)
        assert not service.is_pending()
    finally:
        release.set()
        service.shutdown()


def test_invalidate_and_failure_do_not_publish_a_partial_plan(qtbot):
    service = StabilizationPlanService()
    failures, ready = [], []
    service.failed.connect(lambda message, metadata: failures.append((message, metadata)))
    service.ready.connect(lambda plan, metadata: ready.append(plan))
    try:
        transforms = {0: TrackTransform.from_matrix(0, np.eye(3), 1., TrackStatus.VALID)}
        service.submit(transforms, (0, 48), {}, 25., {'edit': 'invalid'})
        qtbot.waitUntil(lambda: bool(failures))
        assert 'positive' in failures[0][0]
        assert failures[0][1] == {'edit': 'invalid'}
        service.submit(transforms, (64, 48), {}, 25., {'edit': 'cancelled'})
        service.invalidate()
        service.shutdown()
        qtbot.wait(10)
        assert not ready
    finally:
        service.shutdown()
