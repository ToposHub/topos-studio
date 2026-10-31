"""Shared renderer lifecycle and cancellation contracts."""

from __future__ import annotations

from ..core.contracts import AnimationFrame, RenderContext, RenderedFrame, SceneRenderer


class RenderCancelled(RuntimeError):
    """Raised before producing a frame when the caller cancelled the job."""


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


__all__ = ["AnimationFrame", "CancellationToken", "RenderCancelled", "RenderContext", "RenderedFrame", "SceneRenderer"]

