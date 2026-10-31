"""RD5-01: caller-owned decode surface lifecycle contract."""
from __future__ import annotations

import pytest

from src.shared.media.frame_path import (
    DecodeSurfaceLease,
    DecodeSurfaceProtocolError,
    DecodeSurfaceState,
)


def test_surface_lifecycle_and_callback_order() -> None:
    events: list[str] = []
    views = object()
    lease = DecodeSurfaceLease(
        backend="fake-host-visible",
        memory_type=1,
        acquire_callback=lambda: events.append("acquire"),
        map_callback=lambda: (events.append("map") or views),
        unmap_callback=lambda: events.append("unmap"),
        fence_callback=lambda: events.append("fence"),
        release_callback=lambda: events.append("release"),
        zero_copy=True,
    )

    assert lease.state is DecodeSurfaceState.UNACQUIRED
    lease.acquire()
    assert lease.map() is views
    assert lease.state is DecodeSurfaceState.MAPPED
    assert lease.begin_decode() is views
    lease.end_decode()
    lease.unmap()
    lease.fence()
    lease.release()
    lease.release()

    assert events == ["acquire", "map", "unmap", "fence", "release"]
    assert lease.state is DecodeSurfaceState.RELEASED
    assert lease.describe() == {
        "backend": "fake-host-visible",
        "memory_type": 1,
        "state": "released",
        "zero_copy": True,
        "resource_present": False,
        "mapped": False,
    }


def test_surface_rejects_early_release_and_use_after_unmap() -> None:
    lease = DecodeSurfaceLease(
        backend="fake-cpu",
        memory_type=0,
        map_callback=lambda: object(),
    )
    with pytest.raises(DecodeSurfaceProtocolError):
        lease.release()

    lease.acquire()
    lease.map()
    lease.unmap()
    with pytest.raises(DecodeSurfaceProtocolError):
        _ = lease.views
    with pytest.raises(DecodeSurfaceProtocolError):
        lease.begin_decode()
    lease.release()


def test_surface_requires_fence_before_gpu_release() -> None:
    lease = DecodeSurfaceLease(
        backend="fake-gpu",
        memory_type=1,
        map_callback=lambda: object(),
    )
    lease.acquire()
    lease.map()
    lease.unmap()
    with pytest.raises(DecodeSurfaceProtocolError):
        lease.release()
    with pytest.raises(DecodeSurfaceProtocolError):
        lease.fence()
    lease.fence_callback = lambda: None
    lease.fence()
    lease.release()
    assert lease.state is DecodeSurfaceState.RELEASED


def test_surface_abort_returns_acquired_slot_without_gpu_fence() -> None:
    events: list[str] = []
    lease = DecodeSurfaceLease(
        backend="fake-gpu",
        memory_type=1,
        release_callback=lambda: events.append("release"),
    )
    lease.acquire()
    lease.abort()
    lease.abort()
    assert events == ["release"]
    assert lease.state is DecodeSurfaceState.RELEASED
