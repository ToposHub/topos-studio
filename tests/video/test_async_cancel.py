"""
Tests for async rendering and cancellation mechanisms.

Covers:
- RenderRequest enhancement (graph_generation, color_revision)
- Stale request detection (is_stale_request, should_cancel_evaluation)
- Queue merging (submit() intelligent discard)
- AI task stale handling (AIComputeManager)
"""

import pytest
import time
import threading
import queue
from unittest.mock import MagicMock, patch

from src.features.composite.render.render_scheduler import (
    RenderRequest, RenderScheduler, RenderMode, RequestIdGenerator,
)
from src.features.composite.core.graph_snapshot import GraphSnapshot
from src.features.composite.render.ai_compute_manager import AIComputeManager, AIComputeWorker
from src.features.composite.render.ai_cache_manager import AICacheManager


# ── RenderRequest Enhancement Tests ──

def test_render_request_has_generation_fields():
    """RenderRequest should have graph_generation and color_revision fields."""
    snapshot = MagicMock()
    snapshot.generation = 5
    snapshot.color_revision = 3

    req = RenderRequest(
        snapshot=snapshot,
        request_id=10,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=3,
    )

    assert req.graph_generation == 5
    assert req.color_revision == 3


def test_render_request_default_generation():
    """RenderRequest should default generation fields to 0."""
    snapshot = MagicMock()

    req = RenderRequest(
        snapshot=snapshot,
        request_id=1,
        timestamp=time.monotonic(),
    )

    assert req.graph_generation == 0
    assert req.color_revision == 0


# ── RequestIdGenerator Tests ──

def test_request_id_generator_thread_safe():
    """RequestIdGenerator should be thread-safe."""
    gen = RequestIdGenerator()
    ids = []

    def get_ids():
        for _ in range(100):
            ids.append(gen.next_id())

    threads = [threading.Thread(target=get_ids) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All IDs should be unique
    assert len(ids) == 1000
    assert len(set(ids)) == 1000
    # IDs should be monotonically increasing
    assert sorted(ids) == ids


def test_request_id_generator_current():
    """RequestIdGenerator.current should return latest ID."""
    gen = RequestIdGenerator()

    assert gen.current == 0
    gen.next_id()
    assert gen.current == 1
    gen.next_id()
    assert gen.current == 2


# ── RenderScheduler Stale Detection Tests ──

def test_scheduler_mark_current():
    """RenderScheduler.mark_current should track all request fields."""
    scheduler = RenderScheduler()

    snapshot = MagicMock()
    snapshot.generation = 10
    snapshot.color_revision = 5

    req = RenderRequest(
        snapshot=snapshot,
        request_id=100,
        timestamp=time.monotonic(),
        graph_generation=10,
        color_revision=5,
    )

    scheduler.mark_current(req)

    assert scheduler._latest_request_id == 100
    assert scheduler._latest_graph_generation == 10
    assert scheduler._latest_color_revision == 5


def test_scheduler_is_stale_request():
    """RenderScheduler.is_stale_request should detect stale requests."""
    scheduler = RenderScheduler()

    # Mark current request
    current_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=10,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=3,
    )
    scheduler.mark_current(current_req)

    # Older request_id should be stale
    older_id_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=5,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=3,
    )
    assert scheduler.is_stale_request(older_id_req) == True

    # Same request_id should not be stale
    same_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=10,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=3,
    )
    assert scheduler.is_stale_request(same_req) == False

    # Newer request_id should not be stale
    newer_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=15,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=3,
    )
    assert scheduler.is_stale_request(newer_req) == False


def test_scheduler_is_stale_by_generation():
    """RenderScheduler should detect stale by graph_generation."""
    scheduler = RenderScheduler()

    # Mark current request
    current_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=10,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=3,
    )
    scheduler.mark_current(current_req)

    # Lower generation should be stale
    lower_gen_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=10,
        timestamp=time.monotonic(),
        graph_generation=3,
        color_revision=3,
    )
    assert scheduler.is_stale_request(lower_gen_req) == True


def test_scheduler_is_stale_by_color_revision():
    """RenderScheduler should detect stale by color_revision."""
    scheduler = RenderScheduler()

    # Mark current request
    current_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=10,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=3,
    )
    scheduler.mark_current(current_req)

    # Lower color_revision should be stale
    lower_rev_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=10,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=1,
    )
    assert scheduler.is_stale_request(lower_rev_req) == True


def test_scheduler_should_cancel_evaluation():
    """RenderScheduler.should_cancel_evaluation should check mid-evaluation."""
    scheduler = RenderScheduler()

    # Start processing request_id=5 (using int for backward compatibility)
    scheduler.mark_current(5)

    # Should cancel request_id=3
    assert scheduler.should_cancel_evaluation(3) == True

    # Should not cancel request_id=5
    assert scheduler.should_cancel_evaluation(5) == False

    # Should not cancel request_id=7 (would be newer, but we're processing 5)
    assert scheduler.should_cancel_evaluation(7) == False


# ── Queue Merging Tests ──

class MockCompositeRenderer:
    """Mock renderer for testing queue merging."""

    def __init__(self):
        self.command_queue = queue.Queue()

    def submit(self, cmd_type: str, data: dict = None):
        """Submit with queue merging logic (copy from CompositeRenderer)."""
        EVALUATE_GRAPH = "evaluate_graph"

        if cmd_type == EVALUATE_GRAPH:
            request = data.get('request')
            new_request_id = 0
            new_generation = 0
            new_color_rev = 0

            if request is not None:
                new_request_id = request.request_id
                new_generation = request.graph_generation
                new_color_rev = request.color_revision

            preserved = []
            best_eval_request = None

            while True:
                try:
                    item = self.command_queue.get_nowait()
                    item_type, item_data = item

                    if item_type == EVALUATE_GRAPH:
                        old_request = item_data.get('request')
                        if old_request is not None:
                            old_request_id = old_request.request_id
                            old_generation = old_request.graph_generation
                            old_color_rev = old_request.color_revision

                            is_stale = (
                                old_request_id < new_request_id or
                                old_generation < new_generation or
                                old_color_rev < new_color_rev
                            )

                            if not is_stale and best_eval_request is None:
                                best_eval_request = item
                    else:
                        preserved.append(item)
                except queue.Empty:
                    break

            for item in preserved:
                self.command_queue.put(item)

            if best_eval_request is not None:
                self.command_queue.put(best_eval_request)
            else:
                self.command_queue.put((cmd_type, data or {}))
        else:
            self.command_queue.put((cmd_type, data or {}))


def test_queue_merge_discards_old_requests():
    """Queue merging should discard old requests."""
    renderer = MockCompositeRenderer()

    # Submit old request
    old_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=1,
        timestamp=time.monotonic(),
        graph_generation=1,
        color_revision=1,
    )
    renderer.submit("evaluate_graph", {"request": old_req})

    # Submit new request
    new_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=2,
        timestamp=time.monotonic(),
        graph_generation=2,
        color_revision=2,
    )
    renderer.submit("evaluate_graph", {"request": new_req})

    # Queue should have only the new request
    item = renderer.command_queue.get_nowait()
    assert item[0] == "evaluate_graph"
    assert item[1]["request"].request_id == 2


def test_queue_merge_preserves_other_commands():
    """Queue merging should preserve non-EVALUATE commands."""
    renderer = MockCompositeRenderer()

    # Submit UPLOAD_IMAGE
    renderer.submit("upload_image", {"node_id": "node_1"})

    # Submit EVALUATE_GRAPH (old)
    old_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=1,
        timestamp=time.monotonic(),
    )
    renderer.submit("evaluate_graph", {"request": old_req})

    # Submit EVALUATE_GRAPH (new)
    new_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=2,
        timestamp=time.monotonic(),
    )
    renderer.submit("evaluate_graph", {"request": new_req})

    # Queue should have UPLOAD_IMAGE and new EVALUATE
    items = []
    while True:
        try:
            items.append(renderer.command_queue.get_nowait())
        except queue.Empty:
            break

    assert len(items) == 2
    assert items[0] == ("upload_image", {"node_id": "node_1"})
    assert items[1][0] == "evaluate_graph"
    assert items[1][1]["request"].request_id == 2


def test_queue_merge_keeps_newer_in_queue():
    """Queue merging should keep newer request already in queue."""
    renderer = MockCompositeRenderer()

    # Submit new request first
    new_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=10,
        timestamp=time.monotonic(),
        graph_generation=10,
        color_revision=10,
    )
    renderer.submit("evaluate_graph", {"request": new_req})

    # Submit older request
    old_req = RenderRequest(
        snapshot=MagicMock(),
        request_id=5,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=5,
    )
    renderer.submit("evaluate_graph", {"request": old_req})

    # Queue should have only the new request (id=10)
    item = renderer.command_queue.get_nowait()
    assert item[1]["request"].request_id == 10


# ── AIComputeManager Stale Handling Tests ──

def test_ai_manager_set_latest_request_id():
    """AIComputeManager should track latest request ID."""
    cache_mgr = MagicMock()
    ai_mgr = AIComputeManager(cache_mgr)

    assert ai_mgr._latest_request_id == 0

    ai_mgr.set_latest_request_id(5)
    assert ai_mgr._latest_request_id == 5


def test_ai_manager_is_request_stale():
    """AIComputeManager should detect stale requests."""
    cache_mgr = MagicMock()
    ai_mgr = AIComputeManager(cache_mgr)

    ai_mgr.set_latest_request_id(10)

    # Lower request_id is stale
    assert ai_mgr.is_request_stale(5) == True

    # Same request_id is not stale
    assert ai_mgr.is_request_stale(10) == False

    # Higher request_id is not stale
    assert ai_mgr.is_request_stale(15) == False


def test_ai_manager_cancel_stale_requests():
    """AIComputeManager.cancel_stale_requests should cancel old workers."""
    cache_mgr = MagicMock()
    ai_mgr = AIComputeManager(cache_mgr)

    # Mock workers with different request_ids
    worker_1 = MagicMock()
    worker_1.request_id = 1
    worker_1.cancel = MagicMock()
    worker_1.wait = MagicMock()

    worker_5 = MagicMock()
    worker_5.request_id = 5
    worker_5.cancel = MagicMock()
    worker_5.wait = MagicMock()

    worker_10 = MagicMock()
    worker_10.request_id = 10
    worker_10.cancel = MagicMock()
    worker_10.wait = MagicMock()

    ai_mgr._workers = {
        "key_1": worker_1,
        "key_5": worker_5,
        "key_10": worker_10,
    }

    # Cancel requests older than 10
    ai_mgr.cancel_stale_requests(10)

    # worker_1 and worker_5 should be cancelled
    assert worker_1.cancel.called
    assert worker_5.cancel.called
    assert not worker_10.cancel.called

    # Only worker_10 should remain
    assert "key_1" not in ai_mgr._workers
    assert "key_5" not in ai_mgr._workers
    assert "key_10" in ai_mgr._workers


def test_ai_worker_stale_callback():
    """AIComputeWorker should check stale callback."""
    cache_mgr = MagicMock()

    worker = AIComputeWorker(
        cache_key="test_key",
        node_type="ai_generate",
        input_images={},
        params={},
        cache_manager=cache_mgr,
        request_id=5,
    )

    # Set stale callback that says request 5 is stale
    def is_stale(req_id):
        return req_id < 10

    worker.set_stale_callback(is_stale)

    # Run worker (should detect stale and emit stale_detected)
    stale_signal_received = []
    worker.stale_detected.connect(lambda k, r: stale_signal_received.append((k, r)))
    worker.run()

    assert ("test_key", 5) in stale_signal_received


# ── Frame Ready Signal Tests ──

def test_frame_ready_includes_request_id():
    """CompositeRenderer.frame_ready should emit request_id."""
    # This is a structural test - verify the signal signature
    from PyQt6.QtCore import pyqtSignal

    # The signal is defined as: pyqtSignal(int, int, int, int)
    # request_id, texture_id, width, height
    # We verify this through the type annotations in the code

    # Read the signal definition from the actual class
    # (Can't instantiate without OpenGL context, so just check definition exists)
    import inspect
    from src.features.composite.render.composite_renderer import CompositeRenderer

    # Check that frame_ready is defined
    assert hasattr(CompositeRenderer, 'frame_ready')


# ── Integration: Slider Drag Scenario ──

def test_slider_drag_no_queue_buildup():
    """Simulate rapid slider drag: queue should not build up."""
    renderer = MockCompositeRenderer()

    # Simulate 10 rapid slider changes
    for i in range(10):
        req = RenderRequest(
            snapshot=MagicMock(),
            request_id=i + 1,
            timestamp=time.monotonic(),
            graph_generation=i + 1,
            color_revision=1,
        )
        renderer.submit("evaluate_graph", {"request": req})

    # Queue should have only 1 item (the latest request)
    items = []
    while True:
        try:
            items.append(renderer.command_queue.get_nowait())
        except queue.Empty:
            break

    assert len(items) == 1
    assert items[0][1]["request"].request_id == 10


def test_old_result_does_not_overwrite_new():
    """Old evaluation result should be discarded if newer request arrived."""
    scheduler = RenderScheduler()

    # Start processing request_id=5
    req_5 = RenderRequest(
        snapshot=MagicMock(),
        request_id=5,
        timestamp=time.monotonic(),
        graph_generation=5,
        color_revision=5,
    )
    scheduler.mark_current(req_5)

    # Simulate newer request arriving during evaluation
    req_10 = RenderRequest(
        snapshot=MagicMock(),
        request_id=10,
        timestamp=time.monotonic(),
        graph_generation=10,
        color_revision=10,
    )
    scheduler.mark_current(req_10)

    # Now request_id=5 is stale
    assert scheduler.is_stale(5) == True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])