"""Metal conversion for SDR timeline previews presented through the GL slot pool.

Two kinds of frame leave this class, both into an IOSurface the GL viewer
blits (:meth:`render`, :meth:`render_composite`) or straight into a native
present slot (:meth:`encode_native`, :meth:`encode_composite_native`):

* one untouched VideoToolbox clip, optionally with a single LUT — the fused
  one-pass kernels below;
* a composite of several layers (plan task 2.6) — :class:`MetalEditCompositor`
  reproduces the GL per-track passes; see ``metal_edit_compositor.py`` for
  which frames qualify.
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np

from src.features.color.core.cache_manager import CacheTier, LRUCache
from src.features.color.core.shader_nodes_msl import MSL_LUT3D
from src.features.edit.core.metal_edit_compositor import (
    MetalCompositeError,
    MetalCompositePlan,
    MetalEditCompositor,
)
from src.shared.media.gpu_frame import GpuFrameRelease


# Match the Edit preview's limited-range BT.709 conversion and normalized
# bilinear sampling. Use the destination dimensions so proxy-sized canvases
# follow the same pixel-center convention as the GL YUV pass.
_NV12_PREVIEW_MSL = """
#include <metal_stdlib>
using namespace metal;
""" + MSL_LUT3D + """

constexpr sampler preview_sampler(coord::normalized, filter::linear, address::clamp_to_edge);

float3 preview_rgb(texture2d<float> y_plane, texture2d<float> uv_plane, uint2 p, uint2 size) {
    float2 pos = (float2(p) + 0.5) / float2(size);
    float y = max((y_plane.sample(preview_sampler, pos).r - 16.0 / 255.0) * (255.0 / 219.0), 0.0);
    float2 uv = (uv_plane.sample(preview_sampler, pos).rg - 128.0 / 255.0) * (255.0 / 224.0);
    float3 color = float3(y + 1.5748 * uv.y,
                          y - 0.1873 * uv.x - 0.4681 * uv.y,
                          y + 1.8556 * uv.x);
    return clamp(color, 0.0, 1.0);
}

kernel void edit_preview_nv12_sdr(
    texture2d<float> y_plane [[texture(0)]],
    texture2d<float> uv_plane [[texture(1)]],
    texture2d<float, access::write> rgb [[texture(2)]],
    uint2 p [[thread_position_in_grid]]) {
    uint2 size = uint2(rgb.get_width(), rgb.get_height());
    if (any(p >= size)) return;
    rgb.write(float4(preview_rgb(y_plane, uv_plane, p, size), 1.0), p);
}

kernel void edit_preview_nv12_lut(
    texture2d<float> y_plane [[texture(0)]],
    texture2d<float> uv_plane [[texture(1)]],
    texture2d<float, access::write> rgb [[texture(2)]],
    texture3d<float> lut [[texture(3)]],
    constant float &strength [[buffer(0)]],
    uint2 p [[thread_position_in_grid]]) {
    uint2 size = uint2(rgb.get_width(), rgb.get_height());
    if (any(p >= size)) return;
    // The GL preview grades the RGB8 track FBO, including its quantization.
    float3 base = rint(preview_rgb(y_plane, uv_plane, p, size) * 255.0) / 255.0;
    float3 grade = msl_apply_3dlut(base, lut, preview_sampler, strength);
    rgb.write(float4(clamp(grade, 0.0, 1.0), 1.0), p);
}
"""

_PRESENT_VERTEX_SHADER = """
#version 330
void main() {
    const vec2 corners[3] = vec2[3](vec2(-1.0, -1.0), vec2(3.0, -1.0), vec2(-1.0, 3.0));
    gl_Position = vec4(corners[gl_VertexID], 0.0, 1.0);
}
"""

_PRESENT_FRAGMENT_SHADER = """
#version 330
#extension GL_ARB_texture_rectangle : enable
uniform sampler2DRect u_surface;
out vec4 f_color;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    p.y = textureSize(u_surface).y - 1 - p.y;
    f_color = texelFetch(u_surface, p);
}
"""


def metal_preview_mode() -> Literal["off", "lut", "all"]:
    """Resolve which eligible frames an Edit preview worker sends to Metal."""
    if sys.platform != "darwin":
        return "off"
    override = os.getenv("TOPOS_EDIT_METAL_PREVIEW", "").strip().lower()
    if override in {"0", "false", "off", "no"}:
        return "off"
    graphics = os.getenv("TOPOS_BACKEND_GRAPHICS", "").strip().lower().removeprefix("force_")
    if not graphics:
        from src.shared.config.config_manager import ConfigManager

        graphics = ConfigManager().get_backend_selection().graphics.value
    if graphics == "metal" or (graphics == "auto" and override in {"1", "true", "on", "yes"}):
        return "all"
    return "lut" if graphics == "auto" else "off"


@dataclass
class _SharedPreviewSurface:
    size: tuple[int, int]
    pixel_buffer: Any
    metal_texture: Any
    gl_texture: int
    in_use: bool = False

    def release_lease(self) -> None:
        self.in_use = False


class MetalTimelinePreview:
    """Own shared scratch surfaces on the existing Edit render thread."""

    def __init__(self, gl_context: Any) -> None:
        from src.shared.gpu.metal.context import MetalComputeContext
        from src.shared.media.macos_vt.cv_metal_bridge import CVMetalVideoBridge

        self._gl = gl_context
        self._metal = MetalComputeContext()
        self._kernel = self._metal.compile_kernel("edit_preview_nv12_sdr", _NV12_PREVIEW_MSL)
        self._lut_kernel = None
        self._bridge = CVMetalVideoBridge(self._metal)
        self._slots: list[_SharedPreviewSurface] = []
        self._lut_textures: LRUCache[Any] = LRUCache(
            16, CacheTier.GPU_TEXTURE, max_bytes=32 * 1024 * 1024)
        self._lut_strength: tuple[float, Any] | None = None
        self._compositor = MetalEditCompositor(self._metal, self._prepare_lut)
        self._program = self._gl.program(vertex_shader=_PRESENT_VERTEX_SHADER,
                                         fragment_shader=_PRESENT_FRAGMENT_SHADER)
        try:
            self._vao = self._gl.vertex_array(self._program, [])
        except Exception:
            self._program.release()
            raise

    @property
    def last_gpu_time_ms(self) -> float | None:
        return self._metal.last_gpu_time_ms

    @property
    def metal_context(self) -> Any:
        """The MetalComputeContext every preview kernel is dispatched on."""
        return self._metal

    def _allocate(self, size: tuple[int, int]) -> _SharedPreviewSurface:
        import Metal
        import Quartz
        import objc
        from OpenGL import GL

        from src.shared.media.macos_vt.iosurface_gl_bridge import (
            _CGLTexImageIOSurface2D, _current_cgl_context,
        )

        width, height = size
        cgl_context = _current_cgl_context()
        if not cgl_context:
            raise RuntimeError("Metal preview needs a current GL presentation context")
        prior_error = self._gl.error
        if prior_error != "GL_NO_ERROR":
            raise RuntimeError(f"GL error before Metal preview surface allocation ({prior_error})")
        status, buffer = Quartz.CVPixelBufferCreate(
            None, width, height, Quartz.kCVPixelFormatType_32BGRA,
            {Quartz.kCVPixelBufferIOSurfacePropertiesKey: {},
             Quartz.kCVPixelBufferMetalCompatibilityKey: True,
             Quartz.kCVPixelBufferOpenGLCompatibilityKey: True}, None,
        )
        if status != 0 or buffer is None:
            raise RuntimeError(f"Metal preview BGRA pixel buffer allocation failed ({status})")
        surface = Quartz.CVPixelBufferGetIOSurface(buffer)
        if surface is None:
            raise RuntimeError("Metal preview BGRA pixel buffer has no IOSurface")
        desc = Metal.MTLTextureDescriptor.texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
            Metal.MTLPixelFormatBGRA8Unorm, width, height, False)
        desc.setUsage_(Metal.MTLTextureUsageShaderWrite)
        metal_texture = self._metal.device.newTextureWithDescriptor_iosurface_plane_(desc, surface, 0)
        if metal_texture is None:
            raise RuntimeError("Metal preview failed to import the BGRA IOSurface")

        gl_texture = int(GL.glGenTextures(1))
        try:
            target = GL.GL_TEXTURE_RECTANGLE
            GL.glBindTexture(target, gl_texture)
            GL.glTexParameteri(target, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
            GL.glTexParameteri(target, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
            GL.glTexParameteri(target, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
            GL.glTexParameteri(target, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
            result = _CGLTexImageIOSurface2D(
                cgl_context, target, GL.GL_RGBA8, width, height,
                GL.GL_BGRA, GL.GL_UNSIGNED_INT_8_8_8_8_REV,
                ctypes.c_void_p(objc.pyobjc_id(surface)), 0,
            )
            error = GL.glGetError()
            if result or error != GL.GL_NO_ERROR:
                raise RuntimeError(f"Metal preview BGRA surface import failed (CGL={result}, GL={error})")
        except Exception:
            GL.glDeleteTextures([gl_texture])
            raise
        finally:
            GL.glBindTexture(GL.GL_TEXTURE_RECTANGLE, 0)
        return _SharedPreviewSurface(size, buffer, metal_texture, gl_texture)

    def _acquire(self, size: tuple[int, int]) -> _SharedPreviewSurface | None:
        from OpenGL.GL import glDeleteTextures

        for slot in self._slots:
            if not slot.in_use and slot.size == size:
                return slot
        for index, slot in enumerate(self._slots):
            if not slot.in_use:
                replacement = self._allocate(size)
                glDeleteTextures([slot.gl_texture])
                self._slots[index] = replacement
                return replacement
        if len(self._slots) >= 3:
            return None
        slot = self._allocate(size)
        self._slots.append(slot)
        return slot

    def _prepare_lut(self, reference: str, strength: float) -> tuple[Any, Any]:
        from src.shared.color.lut_loader import load_lut, resample_to_unit_domain, resolve_lut_path

        path = Path(resolve_lut_path(reference)).absolute()
        info = path.stat()
        key = (path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        texture = self._lut_textures.get(key)
        if texture is None:
            lut = resample_to_unit_domain(load_lut(str(path)))
            texture = self._metal.create_rgb_lut_texture(lut.table)
            self._lut_textures.put(key, texture, estimated_bytes=lut.size ** 3 * 8)
        if self._lut_strength is None or self._lut_strength[0] != strength:
            self._lut_strength = (strength, self._metal.create_buffer_from_numpy(
                np.asarray([strength], dtype=np.float32)))
        return texture, self._lut_strength[1]

    def _import_source(self, vt_frame: Any) -> tuple[tuple[Any, Any], GpuFrameRelease] | None:
        """Import a VT frame's planes; the caller owns the returned lease."""
        if not self._bridge.initialize():
            reason = self._bridge.last_fallback_reason or "CVMetalTextureCache is unavailable"
            raise RuntimeError(f"Metal preview source import unavailable ({reason})")
        source = self._bridge.import_pixel_buffer(vt_frame.pixel_buffer, vt_frame.frame_index)
        if source is None:
            # One source could be ineligible for this bridge. Preserve the
            # caller's VT frame so the ordinary GL import can still consume it.
            return None
        return source

    def _encode(
        self, y_plane: Any, uv_plane: Any, output: Any, size: tuple[int, int],
        *, lut: tuple[str, float] | None,
    ) -> None:
        """Encode NV12 -> BGRA into ``output``; leaves submitting to the caller."""
        kernel = self._kernel
        buffers = None
        textures = {0: y_plane, 1: uv_plane, 2: output}
        if lut is not None:
            textures[3], strength = self._prepare_lut(*lut)
            buffers = {0: strength}
            if self._lut_kernel is None:
                self._lut_kernel = self._metal.compile_kernel("edit_preview_nv12_lut", _NV12_PREVIEW_MSL)
            kernel = self._lut_kernel
        self._metal.dispatch(kernel, textures=textures, buffers=buffers, threads=(*size, 1))

    def render(
        self, vt_frame: Any, size: tuple[int, int], *, lut: tuple[str, float] | None = None,
    ) -> tuple[_SharedPreviewSurface, GpuFrameRelease] | None:
        """Render into a free IOSurface, leaving ownership of the VT frame to the caller."""
        slot = self._acquire(size)
        if slot is None:
            return None
        source = self._import_source(vt_frame)
        if source is None:
            return None
        (y_plane, uv_plane), source_lease = source
        try:
            self._encode(y_plane.resolved_handle(), uv_plane.resolved_handle(),
                         slot.metal_texture, size, lut=lut)
            self._metal.finish()
        finally:
            source_lease.release()
        slot.in_use = True
        return slot, GpuFrameRelease(retain_token=slot, release_callback=slot.release_lease)

    def encode_native(
        self, vt_frame: Any, output: Any, size: tuple[int, int], *,
        lut: tuple[str, float] | None = None,
    ) -> Optional[GpuFrameRelease]:
        """Encode NV12 -> BGRA into ``output`` for the native viewport (plan 2.1).

        Nothing is submitted and nothing is waited for: the caller owns the
        command buffer (``MetalFrameTransactionPool``) and the returned lease
        of the VT frame, which must stay alive until the GPU finished reading
        the planes.  ``None`` when this frame cannot be imported; the caller's
        VT frame is untouched then.
        """
        source = self._import_source(vt_frame)
        if source is None:
            return None
        (y_plane, uv_plane), source_lease = source
        fresh = not self._metal.has_pending_work
        try:
            self._encode(y_plane.resolved_handle(), uv_plane.resolved_handle(),
                         output, size, lut=lut)
        except Exception:
            source_lease.release()
            if fresh:
                # Leave no half-encoded frame for the next submission: it
                # would read the planes that were just given back.
                self._metal.discard_pending()
            raise
        return source_lease

    def render_composite(
        self, plan: MetalCompositePlan, size: tuple[int, int],
        coordinate_size: tuple[int, int],
    ) -> tuple[_SharedPreviewSurface, GpuFrameRelease] | None:
        """Composite into a free IOSurface (plan 2.6); the GL viewer blits it.

        Every layer's VT frame stays with the caller.  ``None`` when no
        surface is free or a source cannot be imported; nothing of the frame
        is consumed then.
        """
        slot = self._acquire(size)
        if slot is None:
            return None
        try:
            leases = self._compositor.encode(
                plan, slot.metal_texture, size, coordinate_size,
                import_source=self._import_source)
        except MetalCompositeError:
            return None
        try:
            self._metal.finish()
        finally:
            for lease in leases:
                lease.release()
        slot.in_use = True
        return slot, GpuFrameRelease(retain_token=slot, release_callback=slot.release_lease)

    def encode_composite_native(
        self, plan: MetalCompositePlan, output: Any, size: tuple[int, int],
        coordinate_size: tuple[int, int],
    ) -> Optional[list[GpuFrameRelease]]:
        """Encode a composite into ``output`` for the native viewport.

        Like :meth:`encode_native`: nothing is submitted or waited for, and
        the caller owns the returned import leases together with the VT
        frames until the GPU finished.  ``None`` when a source cannot be
        imported; the layers' VT frames are then untouched.
        """
        try:
            return self._compositor.encode(
                plan, output, size, coordinate_size, import_source=self._import_source)
        except MetalCompositeError:
            return None

    def blit(self, slot: _SharedPreviewSurface, framebuffer: Any) -> None:
        """Flip the IOSurface once into the normal viewer presentation slot."""
        from OpenGL import GL
        import moderngl

        framebuffer.use()
        self._gl.viewport = (0, 0, *slot.size)
        GL.glUseProgram(self._program.glo)
        GL.glUniform1i(GL.glGetUniformLocation(self._program.glo, "u_surface"), 0)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_RECTANGLE, slot.gl_texture)
        self._vao.render(mode=moderngl.TRIANGLES, vertices=3)

    def release(self) -> None:
        """Tear down after the worker drains its source fences."""
        from OpenGL.GL import glDeleteTextures

        try:
            if self._slots:
                glDeleteTextures([slot.gl_texture for slot in self._slots])
        finally:
            self._slots.clear()
            self._lut_textures.clear()
            self._lut_strength = None
            self._bridge.destroy()
            self._compositor.release()
            self._vao.release()
            self._program.release()
