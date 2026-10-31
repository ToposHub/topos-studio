"""Metal export read-ahead stays bounded and is joined before frame use."""

from types import SimpleNamespace
from threading import Barrier, BrokenBarrierError

from src.features.edit.core.timeline_export import TimelineExporter


def _exporter(provider, *, enabled=True):
    exporter = object.__new__(TimelineExporter)
    exporter._frame_provider = provider
    exporter._metal_prefetch_enabled = enabled
    exporter._metal_prefetch_executor = None
    exporter._metal_prefetch_futures = {}
    return exporter


def test_metal_prefetch_is_joined_by_the_next_read_for_that_clip():
    calls = []
    exporter = _exporter(SimpleNamespace(
        prefetch_vt_sequential=lambda clip, frame: calls.append((clip, frame))
        or True,
    ))

    exporter._schedule_metal_prefetch("clip-a", 18)
    exporter._wait_for_metal_prefetch("clip-a")

    assert calls == [("clip-a", 18)]
    assert exporter._metal_prefetch_futures == {}
    exporter._metal_prefetch_executor.shutdown(wait=True)


def test_metal_prefetch_can_be_disabled_for_ab_measurement():
    exporter = _exporter(SimpleNamespace(
        prefetch_vt_sequential=lambda *_: True,
    ), enabled=False)

    exporter._schedule_metal_prefetch("clip-a", 18)

    assert exporter._metal_prefetch_executor is None
    assert exporter._metal_prefetch_futures == {}


def test_distinct_metal_sources_prefetch_concurrently():
    rendezvous = Barrier(2)
    outcomes = []

    def prefetch(_clip, _frame):
        try:
            rendezvous.wait(timeout=1.0)
        except BrokenBarrierError:
            result = False
        else:
            result = True
        outcomes.append(result)
        return result

    exporter = _exporter(SimpleNamespace(prefetch_vt_sequential=prefetch))
    try:
        exporter._schedule_metal_prefetch("clip-a", 18)
        exporter._schedule_metal_prefetch("clip-b", 18)
        exporter._wait_for_metal_prefetch("clip-a")
        exporter._wait_for_metal_prefetch("clip-b")
        assert outcomes == [True, True]
    finally:
        if exporter._metal_prefetch_executor is not None:
            exporter._metal_prefetch_executor.shutdown(wait=True)
