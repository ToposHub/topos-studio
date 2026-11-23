"""An RGBA16F surface shared by the Color worker's GL and Metal contexts."""

from __future__ import annotations

import ctypes
from typing import Any


class MetalColorSharedSurface:
    def __init__(self, metal_device: Any, size: tuple[int, int]) -> None:
        import Metal
        import Quartz
        import objc
        from OpenGL import GL

        from src.shared.media.macos_vt.iosurface_gl_bridge import (
            _CGLTexImageIOSurface2D,
            _current_cgl_context,
        )

        cgl = _current_cgl_context()
        if not cgl:
            raise RuntimeError("Color Metal bridge requires a current CGL context")

        self.size = size
        self._texture_id = 0
        self._framebuffer_id = 0
        width, height = size
        status, buffer = Quartz.CVPixelBufferCreate(
            None, width, height, Quartz.kCVPixelFormatType_64RGBAHalf,
            {Quartz.kCVPixelBufferIOSurfacePropertiesKey: {},
             Quartz.kCVPixelBufferMetalCompatibilityKey: True,
             Quartz.kCVPixelBufferOpenGLCompatibilityKey: True},
            None,
        )
        if status or buffer is None:
            raise RuntimeError(f"RGBA16F pixel buffer allocation failed ({status})")
        io_surface = Quartz.CVPixelBufferGetIOSurface(buffer)
        if io_surface is None:
            raise RuntimeError("RGBA16F pixel buffer has no IOSurface")
        descriptor = Metal.MTLTextureDescriptor.texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
            Metal.MTLPixelFormatRGBA16Float, width, height, False)
        descriptor.setUsage_(Metal.MTLTextureUsageShaderRead | Metal.MTLTextureUsageShaderWrite)
        self.texture = metal_device.newTextureWithDescriptor_iosurface_plane_(
            descriptor, io_surface, 0)
        if self.texture is None:
            raise RuntimeError("Metal could not import the RGBA16F IOSurface")
        self._pixel_buffer = buffer

        rectangle = GL.GL_TEXTURE_RECTANGLE
        previous_texture = int(GL.glGetIntegerv(GL.GL_TEXTURE_BINDING_RECTANGLE))
        previous_draw = int(GL.glGetIntegerv(GL.GL_DRAW_FRAMEBUFFER_BINDING))
        try:
            self._texture_id = int(GL.glGenTextures(1))
            GL.glBindTexture(rectangle, self._texture_id)
            result = _CGLTexImageIOSurface2D(
                cgl, rectangle, GL.GL_RGBA16F, width, height,
                GL.GL_RGBA, GL.GL_HALF_FLOAT,
                ctypes.c_void_p(objc.pyobjc_id(io_surface)), 0,
            )
            if result:
                raise RuntimeError(f"GL could not import the RGBA16F IOSurface ({result})")
            self._framebuffer_id = int(GL.glGenFramebuffers(1))
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, self._framebuffer_id)
            GL.glFramebufferTexture2D(
                GL.GL_DRAW_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                rectangle, self._texture_id, 0)
            status = GL.glCheckFramebufferStatus(GL.GL_DRAW_FRAMEBUFFER)
            if status != GL.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError(f"RGBA16F IOSurface framebuffer is incomplete ({status:#x})")
        except Exception:
            self.release()
            raise
        finally:
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, previous_draw)
            GL.glBindTexture(rectangle, previous_texture)

    def copy_from_gl(self, framebuffer: Any) -> Any:
        from OpenGL import GL

        self._blit(framebuffer.glo, self._framebuffer_id)
        GL.glFinish()
        return self.texture

    def copy_to_gl(self, framebuffer: Any) -> None:
        self._blit(self._framebuffer_id, framebuffer.glo)

    def _blit(self, source_id: int, target_id: int) -> None:
        from OpenGL import GL

        previous_read = int(GL.glGetIntegerv(GL.GL_READ_FRAMEBUFFER_BINDING))
        previous_draw = int(GL.glGetIntegerv(GL.GL_DRAW_FRAMEBUFFER_BINDING))
        scissor_enabled = bool(GL.glIsEnabled(GL.GL_SCISSOR_TEST))
        width, height = self.size
        try:
            if scissor_enabled:
                GL.glDisable(GL.GL_SCISSOR_TEST)
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, source_id)
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, target_id)
            # OpenGL stores a bottom-up image. Metal color operators and
            # CAMetalLayer use top-down image rows, including the shared one.
            GL.glBlitFramebuffer(
                0, 0, width, height, 0, height, width, 0,
                GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
        finally:
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, previous_read)
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, previous_draw)
            if scissor_enabled:
                GL.glEnable(GL.GL_SCISSOR_TEST)

    def release(self) -> None:
        from OpenGL import GL

        if self._framebuffer_id:
            # A queued GL compatibility blit may still reference the IOSurface.
            GL.glFinish()
            GL.glDeleteFramebuffers(1, [self._framebuffer_id])
            self._framebuffer_id = 0
        if self._texture_id:
            GL.glDeleteTextures([self._texture_id])
            self._texture_id = 0
        self.texture = None
        self._pixel_buffer = None
