"""Frame-level parallel export (A3 deliverable 1).

The interactive pipeline is pull-based, but export walks a linear frame
range — the ideal case for a producer pipeline:

    ┌────────────────────────────────────────────────────────────┐
    │ N render threads (GL context each, created ON that thread) │
    │   claim frame numbers → evaluate the graph → numpy frame   │
    └──────────────┬─────────────────────────────────────────────┘
                   ▼ results (out of order)
    ┌────────────────────────────────────────────────────────────┐
    │ _OrderedFrameCollector — bounded reorder buffer            │
    └──────────────┬─────────────────────────────────────────────┘
                   ▼ in-order payloads
       caller thread: encode (video) or write (sequence)

Determinism: each frame is evaluated independently from the same graph
payload, so N=1 and N=4 produce identical frames — parity is tested.

Failure/cancel: the first renderer exception or a cancel check aborts;
workers drain, already-written artifacts are cleaned by the caller as
before. Renderer factory failures downgrade the worker count (VRAM
pressure) and the caller falls back to the serial path at N=1.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_PARALLEL_WORKERS = 4


def resolve_parallel_workers(preferred: int, cpu_count: int,
                             cap: int = MAX_PARALLEL_WORKERS) -> int:
    """N = min(preferred, cpu_count // 2, cap), at least 1 (plan §A3)."""
    try:
        preferred = int(preferred)
    except (TypeError, ValueError):
        preferred = 1
    if cpu_count and cpu_count > 0:
        preferred = min(preferred, max(1, cpu_count // 2))
    return max(1, min(preferred, max(1, cap)))


class _OrderedFrameCollector:
    """Bounded reorder buffer turning out-of-order renders into an
    in-order stream.  Notifies the consumer as the next expected frame
    arrives; producers block (semaphore) when `window` frames are
    in flight, bounding memory to ~window × frame size."""

    def __init__(self, window: int, first: int = 0) -> None:
        self._window = max(1, int(window))
        self._slots = threading.Semaphore(self._window)
        self._cond = threading.Condition()
        self._results: Dict[int, Any] = {}
        # Frame numbers are the real timeline numbers (start_frame may be
        # > 0) — the consumer starts expecting the FIRST requested frame.
        self._next_expected = int(first)
        self._finished = False

    def put(self, frame_no: int, payload: Any) -> None:
        with self._cond:
            self._results[frame_no] = payload
            if frame_no == self._next_expected:
                self._cond.notify_all()

    def acquire_slot(self, timeout: Optional[float] = None) -> bool:
        """Producers call before rendering; False when cancelled/timed out."""
        return self._slots.acquire(timeout=timeout if timeout is not None else -1)

    def release_slot(self) -> None:
        self._slots.release()

    def next(self, timeout: float = 1.0) -> Optional[Any]:
        """Next payload in order, or None when nothing ready (timeout,
        drained or cancelled) — the counter never skips a missing frame."""
        with self._cond:
            self._cond.wait_for(
                lambda: (self._next_expected in self._results
                         or self._finished),
                timeout=timeout)
            payload = self._results.pop(self._next_expected, None)
            if payload is None:
                return None
            self._next_expected += 1
            self._slots.release()
            self._cond.notify_all()
            return payload

    def finish(self) -> None:
        """Unblock the consumer when producers stop early (cancel/error)."""
        with self._cond:
            self._finished = True
            self._cond.notify_all()

    def drained(self) -> bool:
        """True when producers are done and nothing more can arrive."""
        with self._cond:
            return (self._finished
                    and self._next_expected not in self._results)


def render_frames_parallel(
    frame_numbers: List[int],
    make_render_fn: Callable[[int], Callable[[int], Any]],
    sink_fn: Callable[[int, Any], None],
    worker_count: int,
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    on_worker_error: Optional[Callable[[str], None]] = None,
    factory_fallback: Optional[Callable[[Exception], None]] = None,
    teardown_fn: Optional[Callable[[int], None]] = None,
) -> bool:
    """Render frames on N dedicated threads, consume them in order.

    Args:
        frame_numbers: ordered frames to render.
        make_render_fn: called once PER WORKER THREAD (thread-local GL
            context); returns a render function frame_no → payload.
            Raise here to downgrade the worker count (factory_fallback
            is notified, the worker is simply not started).
        sink_fn: consumer, called on the CALLER thread in frame order
            (encode_frame / imwrite …).
        worker_count: requested parallelism (≥2 engages threads).
        is_cancelled: cooperative cancel, checked per frame.
        progress_cb: (completed, total) after each consumed frame.
        on_worker_error: first renderer exception message (called once).
        factory_fallback: notified when a worker context fails to build.
        teardown_fn: called ONCE PER WORKER, on that worker's own thread,
            after its render loop ends — GL resources must be destroyed on
            the thread that owns their context.

    Returns:
        True when all frames were consumed, False on cancel/error.
    """
    total = len(frame_numbers)
    if total == 0:
        return True
    if worker_count < 2:
        # Caller keeps the serial path for N=1; nothing to do here.
        return False

    tasks: "queue.Queue[int]" = queue.Queue()
    for frame_no in frame_numbers:
        tasks.put(frame_no)

    collector = _OrderedFrameCollector(window=worker_count * 2,
                                       first=frame_numbers[0])
    errors: List[str] = []
    errors_lock = threading.Lock()
    stop_event = threading.Event()
    active_lock = threading.Lock()
    active_workers = [0]

    def _worker(index: int) -> None:
        try:
            render_fn = make_render_fn(index)
        except Exception as exc:  # context creation failed (VRAM …)
            if factory_fallback is not None:
                try:
                    factory_fallback(exc)
                except Exception:
                    logger.debug("factory_fallback raised", exc_info=True)
            with active_lock:
                active_workers[0] -= 1
                if active_workers[0] <= 0:
                    collector.finish()
            return
        try:
            while not stop_event.is_set():
                if is_cancelled is not None and is_cancelled():
                    return
                # Acquire BEFORE pulling the task — a timeout must never
                # drop a frame number already taken from the queue.
                if not collector.acquire_slot(timeout=0.5):
                    if stop_event.is_set() or (
                            is_cancelled is not None and is_cancelled()):
                        return
                    continue
                try:
                    frame_no = tasks.get_nowait()
                except queue.Empty:
                    collector.release_slot()
                    return
                try:
                    if stop_event.is_set() or (
                            is_cancelled is not None and is_cancelled()):
                        collector.release_slot()
                        return
                    payload = render_fn(frame_no)
                    collector.put(frame_no, ("ok", payload))
                except Exception as exc:
                    collector.put(frame_no, ("error", exc))
                    with errors_lock:
                        if not errors:
                            errors.append(str(exc))
                            stop_event.set()
                            collector.finish()
                            if on_worker_error is not None:
                                on_worker_error(str(exc))
                    return
        finally:
            # Each worker tears down ITS OWN GL renderer on its own thread —
            # GL context/resource destruction is thread-affine, so the
            # caller thread must not do it.
            if teardown_fn is not None:
                try:
                    teardown_fn(index)
                except Exception:
                    logger.debug("worker teardown failed", exc_info=True)
            # this worker is done; if none remain and the consumer still
            # waits (e.g. every factory failed), unblock it.
            with active_lock:
                active_workers[0] -= 1
                if active_workers[0] <= 0:
                    collector.finish()

    threads: List[threading.Thread] = []
    active_workers[0] = worker_count
    for i in range(worker_count):
        t = threading.Thread(target=_worker, args=(i,), daemon=True,
                             name=f"comp-export-render-{i}")
        t.start()
        threads.append(t)

    consumed = 0
    try:
        while consumed < total:
            if is_cancelled is not None and is_cancelled():
                stop_event.set()
                collector.finish()
                return False
            item = collector.next(timeout=0.25)
            if item is None:
                if collector.drained():
                    with errors_lock:
                        if errors and on_worker_error is not None:
                            on_worker_error(errors[0])
                    return False
                continue
            status, payload = item
            if status != "ok":
                stop_event.set()
                collector.finish()
                if on_worker_error is not None:
                    on_worker_error(str(payload))
                return False
            sink_fn(frame_numbers[consumed], payload)
            consumed += 1
            if progress_cb is not None:
                progress_cb(consumed, total)
        return True
    finally:
        stop_event.set()
        collector.finish()
        for t in threads:
            t.join(timeout=5.0)
