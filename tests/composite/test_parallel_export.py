"""A3: frame-parallel export — collector, worker resolution, parity.

The full GL pipeline can't run under pytest; parity (N=1 vs N=4 produce
identical output) is proven against the parallel driver with fake
renderers, which is where the ordering/cancel logic lives.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.render.parallel_frame_render import (
    _OrderedFrameCollector, render_frames_parallel, resolve_parallel_workers)

pytestmark = pytest.mark.composite_core


class TestWorkerResolution:

    def test_plan_formula(self):
        # N = min(preferred, cpu/2, 4), ≥ 1
        assert resolve_parallel_workers(2, 16) == 2
        assert resolve_parallel_workers(4, 16) == 4
        assert resolve_parallel_workers(8, 16) == 4   # capped
        assert resolve_parallel_workers(4, 4) == 2    # cpu/2
        assert resolve_parallel_workers(4, 1) == 1
        assert resolve_parallel_workers(0, 16) == 1
        assert resolve_parallel_workers("3", 16) == 3  # tolerant cast


class TestOrderedCollector:

    def test_out_of_order_arrival_in_order_delivery(self):
        c = _OrderedFrameCollector(window=4)
        c.put(2, "b")
        c.put(0, "a")
        c.put(3, "d")
        c.put(1, "c")
        # frames arrive out of order; delivery is by FRAME ORDER
        assert [c.next(timeout=1.0) for _ in range(4)] == ["a", "c", "b", "d"]

    def test_next_blocks_until_expected(self):
        import threading
        c = _OrderedFrameCollector(window=2)
        c.put(1, "later")
        got = []

        def consume():
            got.append(c.next(timeout=0.5))
        t = threading.Thread(target=consume)
        t.start()
        import time
        time.sleep(0.05)
        assert got == []  # still waiting for frame 0
        c.put(0, "first")
        t.join(timeout=1.0)
        assert got == ["first"]

    def test_drained_after_finish(self):
        c = _OrderedFrameCollector(window=2)
        c.put(0, "x")
        c.finish()
        assert c.next(timeout=0.1) == "x"
        assert c.next(timeout=0.1) is None
        assert c.drained()


class TestParallelParity:

    def _render_all(self, n: int, frames, render_fn_factory):
        out = []
        errors = []

        def sink(frame_no, payload):
            out.append((frame_no, payload))

        ok = render_frames_parallel(
            frames, render_fn_factory, sink, n,
            on_worker_error=errors.append)
        return ok, out, errors

    def test_parity_n1_vs_n4(self):
        frames = list(range(20))

        def factory(seed_box):
            # each "renderer" produces deterministic per-frame payloads
            def render(frame_no):
                return {"frame": frame_no, "value": frame_no * frame_no + 7}
            return render

        ok2, out2, err2 = self._render_all(2, frames, lambda i: factory(i))
        ok4, out4, err4 = self._render_all(4, frames, lambda i: factory(i))
        assert ok2 and ok4
        assert not err2 and not err4
        assert out2 == out4  # identical AND in order
        # N=1 declines by design (caller keeps its serial path)
        ok1, _, _ = self._render_all(1, frames, lambda i: factory(i))
        assert not ok1

    def test_out_of_order_completion_still_ordered(self):
        # later frames render faster — sink order must still be sequential
        frames = list(range(12))

        def factory(_i):
            def render(frame_no):
                import time
                time.sleep(0.001 * (12 - frame_no))  # earlier = slower
                return frame_no
            return render

        ok, out, _ = self._render_all(4, frames, factory)
        assert ok
        assert [f for f, _ in out] == frames

    def test_worker_error_aborts_and_reports(self):
        frames = list(range(50))

        def factory(_i):
            def render(frame_no):
                if frame_no == 5:
                    raise RuntimeError("GL explosion")
                return frame_no
            return render

        ok, out, errors = self._render_all(3, frames, factory)
        assert not ok
        assert any("GL explosion" in e for e in errors)
        # sink only ever saw in-order prefix, never a frame past the error
        assert [f for f, _ in out] == list(range(len(out)))

    def test_cancel_stops_processing(self):
        frames = list(range(200))
        cancel_flag = {"v": False}

        def factory(_i):
            def render(frame_no):
                return frame_no
            return render

        def sink(frame_no, payload):
            if frame_no >= 10:
                cancel_flag["v"] = True  # user cancels mid-stream
        # cancel checked via is_cancelled → pass the flag
        ok = render_frames_parallel(
            frames, lambda _i: (lambda f: f), sink, 3,
            is_cancelled=lambda: cancel_flag["v"])
        assert not ok

    def test_all_factories_failing_reports_false_promptly(self):
        frames = list(range(8))

        def factory(_i):
            raise RuntimeError("no GL context available")
        ok, out, _ = self._render_all(2, frames, factory)
        assert not ok
        assert out == []


class TestNonZeroStartFrame:

    def test_collector_starts_at_first_requested_frame(self):
        """start_frame > 0: real frame numbers, delivery starts at first."""
        c = _OrderedFrameCollector(window=4, first=10)
        c.put(12, "c")
        c.put(11, "b")
        c.put(10, "a")
        assert [c.next(timeout=1.0) for _ in range(3)] == ["a", "b", "c"]

    def test_real_frame_numbers_survive_end_to_end(self):
        """Regression: frames keyed by real timeline numbers (start 100)."""
        frames = list(range(100, 104))
        seen = []

        def factory(index):
            return lambda f: f * 2

        ok = render_frames_parallel(
            frames, factory,
            lambda f, payload: seen.append((f, payload)),
            worker_count=3)
        assert ok is True
        assert [f for f, _ in seen] == frames
        assert [p for _, p in seen] == [f * 2 for f in frames]

    def test_teardown_runs_once_per_worker(self):
        """GL teardown happens on the worker thread, once per worker."""
        frames = list(range(6))
        torn = []

        def factory(index):
            return lambda f: f

        render_frames_parallel(
            frames, factory, lambda f, p: None, worker_count=2,
            teardown_fn=lambda i: torn.append(i))
        assert sorted(torn) == [0, 1]
