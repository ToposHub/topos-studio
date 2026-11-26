"""Render an independent external ACES view on a GL worker."""

from __future__ import annotations

from typing import Any

from src.shared.color.aces_video_gl import AcesVideoGLPass, aces_shader_key
from src.shared.color.color_space_descriptor import ColorSpaceDescriptor
from src.shared.color.project_video_signal import ProjectAcesSettings
from src.shared.video.monitor_output import MonitorRGBAFrame


class AcesMonitorReadback:
    """One bounded GPU transfer. A slow output can hold its last good frame."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._pass: AcesVideoGLPass | None = None
        self._key: tuple[Any, ...] | None = None
        self._target: tuple[Any, Any, Any] | None = None
        self._pending: tuple[Any, int, int] | None = None

    @property
    def transfer_pending(self) -> bool:
        return self._pending is not None

    def submit(
        self, texture: Any, source: ColorSpaceDescriptor, aces: ProjectAcesSettings,
        frame: int, generation: int, *, supersede: bool = False,
    ) -> None:
        from OpenGL.GL import GL_SYNC_GPU_COMMANDS_COMPLETE, glFenceSync, glFlush

        if self._pending is not None:
            if self._pending[2] == generation and not supersede:
                return
            self._discard_pending()
            # Deleting a GL buffer with a pending transfer preserves the
            # object's storage until the driver retires the old command.
            self._release_target()
        key = aces_shader_key(aces)
        if key != self._key:
            output_pass = AcesVideoGLPass(self._ctx, aces)
            if self._pass is not None:
                self._pass.release()
            self._pass, self._key = output_pass, key

        width, height = texture.size
        if self._target is None or self._target[1].size != (width, height):
            self._release_target()
            color = self._ctx.texture((width, height), 4, dtype="f1")
            fbo = None
            try:
                fbo = self._ctx.framebuffer(color_attachments=[color])
                buffer = self._ctx.buffer(reserve=width * height * 4)
            except BaseException:
                if fbo is not None:
                    fbo.release()
                color.release()
                raise
            self._target = color, fbo, buffer

        _, fbo, buffer = self._target
        fbo.use()
        self._ctx.viewport = (0, 0, width, height)
        self._pass.draw(texture, source)
        fbo.read_into(buffer, components=4, alignment=1)
        fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
        if fence is None or fence == 0:
            raise RuntimeError("Could not fence the independent ACES monitor readback")
        self._pending = (fence, frame, generation)
        glFlush()

    def take(self, current_generation: int) -> tuple[int, int, MonitorRGBAFrame] | None:
        if self._pending is None:
            return None

        from OpenGL.GL import (
            GL_ALREADY_SIGNALED, GL_CONDITION_SATISFIED, GL_WAIT_FAILED, glClientWaitSync, glDeleteSync,
        )

        fence, frame, generation = self._pending
        status = glClientWaitSync(fence, 0, 0)
        if status == GL_WAIT_FAILED:
            raise RuntimeError("Could not poll the independent ACES monitor readback")
        if status not in (GL_ALREADY_SIGNALED, GL_CONDITION_SATISFIED):
            return None
        glDeleteSync(fence)
        self._pending = None
        if generation != current_generation:
            return None
        _, fbo, buffer = self._target
        return frame, generation, MonitorRGBAFrame(buffer.read(), *fbo.size)

    def _release_target(self) -> None:
        if self._target is not None:
            color, fbo, buffer = self._target
            self._target = None
            fbo.release()
            color.release()
            buffer.release()

    def _discard_pending(self) -> None:
        if self._pending is not None:
            from OpenGL.GL import glDeleteSync

            fence, _, _ = self._pending
            self._pending = None
            glDeleteSync(fence)

    def release(self) -> None:
        self._discard_pending()
        self._release_target()
        if self._pass is not None:
            self._pass.release()
            self._pass = None
            self._key = None
