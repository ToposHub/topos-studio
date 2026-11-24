"""Asynchronous OpenGL readback for single-LUT Rec.709 timeline exports."""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
import weakref
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.features.color.core.shader_nodes import LUT3D_SNIPPET
from src.shared.media.frame import DecodedFrame


logger = logging.getLogger(__name__)
READBACK_DEPTH = 3
_HOST_CACHE_SIZE = 8

_FRAGMENT = """
out uvec4 output_codes;
uniform usampler2D y_plane;
uniform usampler2D cb_plane;
uniform usampler2D cr_plane;
uniform sampler3D lut_tex;
uniform float strength;
""" + LUT3D_SNIPPET + """
const vec3 weights = vec3(0.2126, 0.7152, 0.0722);
#ifdef OUTPUT_YUV420P
const int chroma_y_div = 2;
const vec2 luma_scale_bias = vec2(219.0, 16.0);
const vec4 chroma_scale_bias_min_max = vec4(224.0, 128.0, 16.0, 240.0);
#else
const int chroma_y_div = 1;
const vec2 luma_scale_bias = vec2(876.0, 64.0);
const vec4 chroma_scale_bias_min_max = vec4(896.0, 512.0, 64.0, 960.0);
#endif

vec2 chroma_at(ivec2 p) {
    return (vec2(texelFetch(cb_plane, p, 0).r,
                 texelFetch(cr_plane, p, 0).r) - 128.0) / 224.0;
}

vec3 grade(ivec2 p, vec2 chroma) {
    float y = (float(texelFetch(y_plane, p, 0).r) - 16.0) / 219.0;
    vec3 rgb = clamp(vec3(y + 1.5748 * chroma.y,
                         y - 0.187324 * chroma.x - 0.468124 * chroma.y,
                         y + 1.8556 * chroma.x), 0.0, 1.0);
    // Use the same 2x2 chroma replication and RGB8 domain as the packed
    // export decoder. Its integer conversion has slightly different rounding.
    rgb = roundEven(rgb * 255.0) / 255.0;
    rgb = clamp(apply_3dlut(rgb, lut_tex, strength), 0.0, 1.0);
#ifdef OUTPUT_YUV420P
    // The packed 8-bit delivery compositor quantizes after the LUT too.
    rgb = roundEven(rgb * 255.0) / 255.0;
#endif
    return rgb;
}

void main() {
    ivec2 group = ivec2(gl_FragCoord.xy);
    ivec2 size = textureSize(y_plane, 0);
    // Four consecutive codes per texel. The atlas starts with luma.
    if (group.y < size.y) {
        ivec2 p = ivec2(group.x * 4, group.y);
        vec2 c0 = chroma_at(p / 2);
        vec2 c1 = chroma_at(p / 2 + ivec2(1, 0));
        vec4 luma = vec4(dot(grade(p, c0), weights),
                         dot(grade(p + ivec2(1, 0), c0), weights),
                         dot(grade(p + ivec2(2, 0), c1), weights),
                         dot(grade(p + ivec2(3, 0), c1), weights));
        output_codes = uvec4(roundEven(luma_scale_bias.y + luma_scale_bias.x * luma));
        return;
    }
    // Each atlas row holds two rows of one half-width chroma plane.
    int chroma_height = size.y / chroma_y_div;
    bool is_cr = group.y >= size.y + chroma_height / 2;
    int row = 2 * (group.y - size.y - (is_cr ? chroma_height / 2 : 0));
    int columns = size.x / 8;
    if (group.x >= columns) row += 1;
    row *= chroma_y_div;
    int x = 8 * (group.x % columns);
    for (int lane = 0; lane < 4; ++lane) {
        ivec2 p = ivec2(x + 2 * lane, row);
        vec2 c = chroma_at(p / 2);
        vec3 rgb = grade(p, c) + grade(p + ivec2(1, 0), c);
#ifdef OUTPUT_YUV420P
        rgb = 0.25 * (rgb + grade(p + ivec2(0, 1), c) + grade(p + ivec2(1, 1), c));
#else
        rgb *= 0.5;
#endif
        float luma = dot(rgb, weights);
        float value = is_cr ? (rgb.r - luma) / 1.5748 : (rgb.b - luma) / 1.8556;
        vec4 limits = chroma_scale_bias_min_max;
        output_codes[lane] = uint(roundEven(clamp(limits.y + limits.x * value, limits.z, limits.w)));
    }
}
"""


def _return_pixels(cache: queue.LifoQueue[np.ndarray], pixels: np.ndarray) -> None:
    try:
        cache.put_nowait(pixels)
    except queue.Full:
        pass


class _PixelOwner:
    """Keep a cache entry leased through NumPy views and FFmpeg's AVBufferRefs."""

    def __init__(self, pixels: np.ndarray, cache: queue.LifoQueue[np.ndarray]) -> None:
        self.__array_interface__ = pixels.__array_interface__
        weakref.finalize(self, _return_pixels, cache, pixels)


@dataclass
class _Slot:
    buffer: int
    textures: list[Any]
    address: int = 0
    fence: Any = None
    borrowed: bool = False


class _ReadbackLease:
    def __init__(self, slot: _Slot, returned: threading.Condition) -> None:
        self._slot: _Slot | None = slot
        self._returned = returned

    def release(self) -> None:
        with self._returned:
            if self._slot is not None:
                self._slot.borrowed = False
                self._slot = None
                self._returned.notify()


@dataclass(frozen=True)
class PendingPlanarLutFrame:
    readback: PlanarLutReadback
    slot: _Slot
    frame_index: int
    time_seconds: float

    def finish(self) -> DecodedFrame:
        return self.readback.finish(self.slot, self.frame_index, self.time_seconds)


class PlanarLutReadback:
    """Own the fixed-size transfer ring on the timeline render thread.

    Synchronous TOPOS encode can borrow a mapped slot until its call returns.
    Other encoders get host allocations that can outlive this GL context.
    """

    def __init__(
        self, ctx: Any, vertex: str, quad: Any, width: int, height: int, pixel_format: str,
        *, existing_422_atlas: bool = False, borrow_slots: int = 0,
    ) -> None:
        import av
        from OpenGL import GL, platform
        from OpenGL.raw.GL.VERSION.GL_1_0 import glReadPixels

        if pixel_format not in ("yuv420p", "yuv422p10le"):
            raise ValueError(f"Unsupported planar LUT format: {pixel_format}")
        output_420 = pixel_format == "yuv420p"
        if existing_422_atlas and output_420:
            raise ValueError("Existing atlas must have 10-bit 4:2:2 planes")
        if borrow_slots < 0 or (borrow_slots and not existing_422_atlas):
            raise ValueError("Only native 4:2:2 atlas readback can borrow mapped slots")
        if (width <= 0 or height <= 0 or width % 32 or height % (4 if output_420 else 2)
                or "NVIDIA" not in ctx.info.get("GL_VENDOR", "").upper()
                or (ctx.version_code < 440 and "GL_ARB_buffer_storage" not in ctx.extensions)):
            raise RuntimeError("OpenGL planar LUT asynchronous readback unavailable")
        if output_420:
            if not callable(getattr(av.VideoFrame, "from_numpy_buffer", None)):
                raise RuntimeError("PyAV buffer wrapping unavailable")
        elif not existing_422_atlas:
            # Older PyAV versions may expose from_dlpack only for NV12.
            if not callable(getattr(av.VideoFrame, "from_dlpack", None)):
                raise RuntimeError("PyAV planar DLPack is unavailable")
            av.VideoFrame.from_dlpack(tuple(np.zeros((2, w), dtype=np.uint16)
                                          for w in (32, 16, 16)), format=pixel_format)
        self.width, self.height = width, height
        self.pixel_format = pixel_format
        self._existing_422_atlas = existing_422_atlas
        self._chroma_height = height // 2 if output_420 else height
        self._atlas_height = height + self._chroma_height
        self._dtype = np.dtype(np.uint8 if output_420 else np.uint16)
        self._pixel_type = GL.GL_UNSIGNED_BYTE if output_420 else GL.GL_UNSIGNED_SHORT
        self._gl = GL
        self._read_pixels = glReadPixels
        self._get_current_context = platform.PLATFORM.GetCurrentContext
        self._context_id = self._get_current_context()
        if not self._context_id:
            raise RuntimeError("No current OpenGL context for planar LUT readback")
        self._slots: list[_Slot] = []
        self._borrow_slots = borrow_slots
        self._returned = threading.Condition()
        self._objects: list[Any] = []
        self._qualified_luts: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()
        self._host: queue.LifoQueue[np.ndarray] = queue.LifoQueue(maxsize=_HOST_CACHE_SIZE)
        try:
            if not existing_422_atlas:
                defines = "#define OUTPUT_YUV420P\n" if output_420 else ""
                self._program = ctx.program(
                    vertex_shader=vertex, fragment_shader="#version 330\n" + defines + _FRAGMENT)
                self._objects.append(self._program)
                self._vao = ctx.vertex_array(self._program, [(quad, "2f", "in_pos")])
                self._objects.append(self._vao)
                atlas = ctx.texture((width // 4, self._atlas_height), 4,
                                    dtype="u1" if output_420 else "u2")
                self._objects.append(atlas)
                self._fbo = ctx.framebuffer(atlas)
                self._objects.append(self._fbo)
            flags = GL.GL_MAP_READ_BIT | GL.GL_MAP_PERSISTENT_BIT | GL.GL_MAP_COHERENT_BIT
            size = width * self._atlas_height * self._dtype.itemsize
            for _ in range(READBACK_DEPTH + borrow_slots):
                slot = _Slot(int(GL.glGenBuffers(1)), [])
                self._slots.append(slot)
                GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, slot.buffer)
                GL.glBufferStorage(GL.GL_PIXEL_PACK_BUFFER, size, None, flags)
                slot.address = GL.glMapBufferRange(
                    GL.GL_PIXEL_PACK_BUFFER, 0, size, flags)
                if not slot.address:
                    raise RuntimeError("Could not map the HQX readback buffer")
                if not existing_422_atlas:
                    for w, h in ((width, height), (width // 2, height // 2), (width // 2, height // 2)):
                        texture = ctx.texture((w, h), 1, dtype="u1")
                        self._objects.append(texture)
                        slot.textures.append(texture)
            GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, 0)
        except BaseException:
            self.release()
            raise

    def is_current(self) -> bool:
        return self._get_current_context() == self._context_id

    def supports_lut(self, texture: Any) -> bool:
        """Avoid amplifying RGB8 rounding with steep LUT transitions."""
        if texture not in self._qualified_luts:
            size = texture.size[0]
            qualified = False
            if 2 <= size <= 65:
                # Read once per cached texture, after the normal LUT loader's
                # domain and shaper remap, to bound trilinear interpolation.
                values = np.frombuffer(texture.read(alignment=1), dtype=np.float32)
                values = values.reshape(size, size, size, 3)
                if np.isfinite(values).all():
                    slope = (size - 1) * sum(
                        np.max(np.abs(np.diff(values, axis=axis)), axis=(0, 1, 2))
                        for axis in range(3)
                    )
                    qualified = bool(np.all(slope <= 1.5))
            self._qualified_luts[texture] = qualified
        return self._qualified_luts[texture]

    def _available_slot(self) -> _Slot:
        if not self.is_current():
            raise RuntimeError("Planar LUT readback context is no longer current")
        with self._returned:
            slot = next((entry for entry in self._slots
                         if entry.fence is None and not entry.borrowed), None)
        if slot is None:
            raise RuntimeError("Planar LUT readback ring is full")
        return slot

    def submit(
        self, planes: tuple[np.ndarray, ...], lut: Any, strength: float,
        frame_index: int, time_seconds: float,
    ) -> PendingPlanarLutFrame:
        import moderngl

        slot = self._available_slot()
        for unit, (name, texture, plane) in enumerate(zip(
            ("y_plane", "cb_plane", "cr_plane"), slot.textures, planes, strict=True
        )):
            texture.write(np.ascontiguousarray(plane), alignment=1)
            texture.use(unit)
            self._program[name].value = unit
        lut.use(3)
        self._program["lut_tex"].value = 3
        self._program["strength"].value = strength
        self._fbo.use()
        self._vao.render(mode=moderngl.TRIANGLE_STRIP)
        return self._submit_readback(slot, frame_index, time_seconds)

    def capture_422_atlas(self, fbo: Any, frame_index: int, time_seconds: float) -> PendingPlanarLutFrame:
        if not self._existing_422_atlas or fbo.size != (self.width, self._atlas_height):
            raise ValueError("Unexpected framebuffer for planar 4:2:2 readback")
        slot = self._available_slot()
        fbo.use()
        return self._submit_readback(slot, frame_index, time_seconds)

    def _submit_readback(self, slot: _Slot, frame_index: int, time_seconds: float) -> PendingPlanarLutFrame:
        gl = self._gl
        read_width = self.width if self._existing_422_atlas else self.width // 4
        read_format = gl.GL_RED_INTEGER if self._existing_422_atlas else gl.GL_RGBA_INTEGER
        gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, slot.buffer)
        try:
            self._read_pixels(0, 0, read_width, self._atlas_height, read_format,
                              self._pixel_type, ctypes.c_void_p(0))
            slot.fence = gl.glFenceSync(gl.GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            if not slot.fence:
                raise RuntimeError("Could not fence the planar LUT readback")
        finally:
            gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, 0)
        return PendingPlanarLutFrame(self, slot, frame_index, time_seconds)

    def finish(self, slot: _Slot, frame_index: int, time_seconds: float) -> DecodedFrame:
        import av

        if not self.is_current():
            raise RuntimeError("Planar LUT readback context is no longer current")
        gl = self._gl
        fence = slot.fence
        status = gl.glClientWaitSync(fence, gl.GL_SYNC_FLUSH_COMMANDS_BIT, 1_000_000_000)
        if status not in (gl.GL_CONDITION_SATISFIED, gl.GL_ALREADY_SIGNALED):
            raise RuntimeError(f"Planar LUT readback fence did not complete ({status})")
        gl.glDeleteSync(fence)
        slot.fence = None
        width, height = self.width, self.height
        luma = width * height
        chroma = width // 2 * self._chroma_height
        if self._borrow_slots:
            memory = (ctypes.c_uint16 * (luma + 2 * chroma)).from_address(slot.address)
            pixels = np.ctypeslib.as_array(memory)
            pixels.flags.writeable = False
            frame = DecodedFrame(
                frame_index=frame_index, pts=frame_index, time_seconds=time_seconds,
                planes=(pixels[:luma].reshape(height, width),
                        pixels[luma:luma + chroma].reshape(height, width // 2),
                        pixels[luma + chroma:].reshape(height, width // 2)),
                pixel_format=self.pixel_format, width=width, height=height,
                extra={"opengl_output_buffer_lease": _ReadbackLease(slot, self._returned)},
            )
            with self._returned:
                slot.borrowed = True
            return frame
        try:
            allocation = self._host.get_nowait()
        except queue.Empty:
            # Align plane starts and keep padding after the last visible sample.
            size = luma + 2 * chroma
            raw = np.empty(size + 128 // self._dtype.itemsize, dtype=self._dtype)
            start = (-raw.ctypes.data % 64) // self._dtype.itemsize
            raw[start + size:].fill(0)
            allocation = raw[start:start + size]
        pixels = np.asarray(_PixelOwner(allocation, self._host))
        ctypes.memmove(pixels.ctypes.data, slot.address, pixels.nbytes)
        planes = (pixels[:luma].reshape(height, width),
                  pixels[luma:luma + chroma].reshape(self._chroma_height, width // 2),
                  pixels[luma + chroma:].reshape(self._chroma_height, width // 2))
        if self.pixel_format == "yuv420p":
            extra = {"yuv420p_buffer": pixels.reshape(self._atlas_height, width)}
        elif self._existing_422_atlas:
            extra = None
        else:
            extra = {"yuv422p10_frame": av.VideoFrame.from_dlpack(planes, format=self.pixel_format)}
        return DecodedFrame(
            frame_index=frame_index, pts=frame_index, time_seconds=time_seconds,
            planes=planes, pixel_format=self.pixel_format, width=width, height=height,
            extra=extra,
        )

    def wait_for_borrowed_frames(self) -> None:
        """Wait for the encoder to return every mapped plane, without GL calls."""
        with self._returned:
            self._returned.wait_for(lambda: not any(slot.borrowed for slot in self._slots))

    def release(self) -> None:
        if not self.is_current():
            raise RuntimeError("Planar LUT readback must be released on its current context")
        self.wait_for_borrowed_frames()
        gl = self._gl
        for slot in self._slots:
            if slot.fence is not None:
                try:
                    gl.glDeleteSync(slot.fence)
                except gl.error.Error:
                    logger.debug("Could not delete an HQX readback fence", exc_info=True)
                slot.fence = None
            try:
                gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, slot.buffer)
                if slot.address:
                    gl.glUnmapBuffer(gl.GL_PIXEL_PACK_BUFFER)
            except gl.error.Error:
                logger.debug("Could not unmap an HQX readback buffer", exc_info=True)
            finally:
                try:
                    gl.glDeleteBuffers(1, [slot.buffer])
                except gl.error.Error:
                    logger.debug("Could not delete an HQX readback buffer", exc_info=True)
        self._slots.clear()
        try:
            gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, 0)
        except gl.error.Error:
            logger.debug("Could not reset the HQX pack buffer binding", exc_info=True)
        for resource in reversed(self._objects):
            try:
                resource.release()
            except Exception:
                logger.debug("Could not release an HQX render resource", exc_info=True)
        self._objects.clear()
