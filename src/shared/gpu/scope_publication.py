"""Lifetime and GPU ordering for shared scope texture allocations.

All methods run with the global GL mutex held and the caller's context
current. The producer retires a publication before reallocating outputs;
presenters copy into their own GPU cache, never retaining unprotected IDs.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)
# PyOpenGL on macOS can expose GL_TIMEOUT_IGNORED as -2**63 + 1.
# glWaitSync requires the actual GLuint64 all-ones value (otherwise 1281).
_TIMEOUT_IGNORED = 0xFFFFFFFFFFFFFFFF


class ScopePublication:
    """A revocable descriptor, with server-side producer/consumer fences."""

    def __init__(self, fence: Any = None) -> None:
        self.valid = True
        self._ready = True
        self._write_fence = fence
        self._read_fences: list[Any] = []

    @classmethod
    def publish(cls, finish: Callable[[], None]) -> ScopePublication:
        """Flush producer work without waiting on the render worker."""
        from OpenGL.GL import GL_SYNC_GPU_COMMANDS_COMPLETE, glFenceSync, glFlush

        fence = None
        try:
            fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            if fence:
                glFlush()
                return cls(fence)
        except Exception as exc:
            # PyOpenGL can raise for a pre-existing GL error even when the
            # operation returned a sync object. Retire that result as well.
            if fence is None:
                fence = getattr(exc, 'result', None)
            logger.debug('Scope producer fence unavailable', exc_info=True)
        # Correctness fallback for drivers lacking sync-object support.
        if fence:
            from OpenGL.GL import glDeleteSync
            glDeleteSync(fence)
        finish()
        return cls()

    def wait_ready(self) -> bool:
        """Order the UI's GPU copy after the producer; never CPU-wait."""
        if not self.valid or not self._ready:
            return False
        if self._write_fence:
            from OpenGL.GL import glWaitSync
            glWaitSync(self._write_fence, 0, _TIMEOUT_IGNORED)
        return True

    def prepare_write(self) -> None:
        """Order the next producer write after copies from every UI context."""
        # If a later draw fails, queued descriptors must not copy partially
        # updated output. Only a successful publish makes it readable again.
        self._ready = False
        from OpenGL.GL import glDeleteSync, glWaitSync

        for fence in self._read_fences:
            glWaitSync(fence, 0, _TIMEOUT_IGNORED)
            glDeleteSync(fence)
        self._read_fences.clear()
        if self._write_fence:
            glDeleteSync(self._write_fence)
            self._write_fence = None

    def refresh(self, finish: Callable[[], None]) -> None:
        """Publish the latest writes to the same still-live allocations."""
        self.prepare_write()
        published = self.publish(finish)
        self._write_fence = published._write_fence
        self._ready = True

    def copied(self) -> None:
        """Order future producer writes after this context's cache copy."""
        from OpenGL.GL import GL_SYNC_GPU_COMMANDS_COMPLETE, glFenceSync, glFlush, glFinish

        fence = None
        try:
            fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            if fence:
                glFlush()
                self._read_fences.append(fence)
                return
        except Exception as exc:
            if fence is None:
                fence = getattr(exc, 'result', None)
            logger.debug('Scope consumer fence unavailable', exc_info=True)
        if fence:
            from OpenGL.GL import glDeleteSync
            glDeleteSync(fence)
        glFinish()

    def retire(self) -> None:
        """Revoke IDs before reuse and order writes after all GPU copies."""
        if not self.valid:
            return
        self.valid = False
        self.prepare_write()
