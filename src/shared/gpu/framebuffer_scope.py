"""Temporary ModernGL targets with valid binding ownership on every exit."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Sequence


def _live_framebuffer(framebuffer: Any) -> bool:
    from moderngl import InvalidObject
    return framebuffer is not None and not isinstance(framebuffer.mglo, InvalidObject)


@contextmanager
def preserve_framebuffer(ctx: Any) -> Iterator[None]:
    """Keep ModernGL's cached FBO and the driver binding in sync.

    A caller may have released its last bound temporary FBO. Normalize that
    stale cache *before* creating another FBO: ModernGL creation restores the
    cached binding, and restoring a deleted GL name generates INVALID_OPERATION.
    No GL errors are consumed or hidden here.
    """
    previous = getattr(ctx, "fbo", None)
    viewport = ctx.viewport
    scissor = ctx.scissor
    from OpenGL.GL import GL_SCISSOR_TEST, glIsEnabled
    # ModernGL's getter returns the rectangle even when scissoring is off.
    # Assigning that rectangle would enable a stale (often 1x1) crop.
    scissor_enabled = bool(glIsEnabled(GL_SCISSOR_TEST))
    if not _live_framebuffer(previous):
        previous = ctx.detect_framebuffer(0)
        previous.use()
        ctx.viewport = viewport
        ctx.scissor = scissor if scissor_enabled else None
    try:
        yield
    finally:
        if not _live_framebuffer(previous):
            previous = ctx.detect_framebuffer(0)
        previous.use()
        ctx.viewport = viewport
        ctx.scissor = scissor if scissor_enabled else None


@contextmanager
def temporary_framebuffer(ctx: Any, attachments: Sequence[Any]) -> Iterator[Any]:
    """Restore a live caller target before releasing a bound temporary target."""
    framebuffer = None
    try:
        with preserve_framebuffer(ctx):
            framebuffer = ctx.framebuffer(color_attachments=attachments)
            framebuffer.use()
            yield framebuffer
    finally:
        if framebuffer is not None:
            framebuffer.release()
