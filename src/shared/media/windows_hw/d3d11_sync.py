"""Small synchronization contracts used by D3D11 surface leases."""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

__all__ = ["D3D11Fence", "ImmediateD3D11Fence", "EventD3D11Fence"]


@runtime_checkable
class D3D11Fence(Protocol):
    """A non-blocking-to-the-render-thread completion fence."""

    def wait(self, timeout_s: float | None = None) -> bool:
        ...

    @property
    def signalled(self) -> bool:
        ...


class ImmediateD3D11Fence:
    """Test/native adapter for a submission already known complete."""

    @property
    def signalled(self) -> bool:
        return True

    def wait(self, timeout_s: float | None = None) -> bool:
        return True


class EventD3D11Fence:
    """Thread-safe test fence mirroring a native event/query completion."""

    def __init__(self, signalled: bool = False) -> None:
        self._event = threading.Event()
        if signalled:
            self._event.set()

    @property
    def signalled(self) -> bool:
        return self._event.is_set()

    def signal(self) -> None:
        self._event.set()

    def wait(self, timeout_s: float | None = None) -> bool:
        return self._event.wait(timeout_s)

