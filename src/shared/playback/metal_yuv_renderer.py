"""Draws playback ``YuvFrame`` planes into Metal present-slot textures (plan 7.1).

The GL playback worker uploads Y/U/V with ``YUVGPUUploader`` and converts
them in ``render_yuv_to_rgb``.  This module is the Metal counterpart of those
two steps and keeps to the GL semantics so both paths show the same picture:

- **Planes.**  8-bit planes become ``R8Unorm`` textures and are read through a
  normalised, linearly filtered sampler with clamped edges — what the GL ``R8``
  pool gives its ``sampler2D``.  10/12/16-bit planes become ``R16Uint``
  textures read by index, i.e. nearest — what the GL ``R16UI`` pool gives its
  ``usampler2D``.  The kernels differ only in that read.
- **Coordinates.**  A pixel centre of the picture rectangle maps to the same
  normalised texture coordinate the GL viewport gives its fragment; the
  chroma coordinate is ``uv * scale + offset`` with the values of
  ``topos_chroma_sampling_params``.
- **Colour.**  The same matrix coefficients and the same limited / full range
  arithmetic in float32, GBR planes passed through as full-range G, B, R.
- **Picture.**  The letterbox rectangle of the GL path is written into a
  drawable-sized ``BGRA8Unorm`` slot texture, black around it, so the present
  pass shows it one to one.

Everything here belongs to the render thread that created it.  Upload
textures are handed out under a ``busy`` flag and only become free again when
the frame transaction that read them completed on the GPU; present slots go
through the shared ``OutputSlotPool`` lease protocol.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from src.shared.gpu.metal.present_port import ORIGIN_TOP_LEFT
from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    NativePresentDescriptor,
    OutputSlotPool,
    PresentColorInfo,
    TextureLease,
)
from src.shared.media.gpu_frame import GpuPlaneResource

from .metal_playback import (
    KERNEL_U8,
    KERNEL_U16,
    build_yuv_params,
    device_max_texture,
    letterbox_rect,
    params_key,
)
from .yuv_frame import YuvFrame

logger = logging.getLogger(__name__)

#: ``MTLPixelFormatR8Unorm``: 8-bit planes, sampled as ``code / 255``.
PIXEL_FORMAT_R8_UNORM = 10
#: ``MTLPixelFormatR16Uint``: 10/12/16-bit planes, read as raw code values.
#: (``32`` is ``RG8Uint``; the value is cross-checked against the Metal
#: module at first upload.)
PIXEL_FORMAT_R16_UINT = 23
#: ``MTLPixelFormatBGRA8Unorm``: the format of the native present pass.
PIXEL_FORMAT_BGRA8_UNORM = 80
RESOURCE_FORMAT = "bgra8unorm"

# A slot pool whose frames did not all come back is dropped after this long.
_RETIRED_POOL_TIMEOUT_S = 2.0
_PARAMS_CACHE_LIMIT = 32

PLAYBACK_YUV_MSL = """
#include <metal_stdlib>
using namespace metal;

// Same numbers as the GLSL of YUVGPUUploader.  Rectangles are in pixels of
// the output texture, row 0 at the top.
struct PlaybackYuvParams {
    float4 rect;               // left, top, width, height of the picture
    float4 coeffs;             // cr_to_r, cb_to_g, cr_to_g, cb_to_b
    float2 luma_texel_scale;   // luma texels per output pixel
    float2 chroma_texel_scale; // chroma texels per output pixel
    float2 chroma_offset_texels;
    float2 chroma_scale;       // as the GL uniform
    float2 chroma_offset;      // as the GL uniform
    float2 pad;
    float  max_val;
    float  y_black;
    float  y_range_inv;
    float  c_neutral;
    float  c_range_inv;
    int    color_range;        // 0 = limited, 1 = full
    int    gbr_mode;           // 1 = planes are full-range G, B, R
    int    pad_i;
};

// What the GL path gets from its R8 pool: normalised samples, bilinear
// filtering, clamped at the edges.
constexpr sampler playback_sampler(address::clamp_to_edge,
                                   filter::linear,
                                   coord::normalized);

static inline float3 playback_yuv_to_rgb(float y,
                                         float u,
                                         float v,
                                         constant const PlaybackYuvParams &p) {
    if (p.gbr_mode != 0) {
        // Plane order G, B, R; no range expansion and no matrix.
        return float3(v, y, u);
    }
    if (p.color_range == 0) {
        y = (y - p.y_black) * p.y_range_inv;
        u = (u - p.c_neutral) * p.c_range_inv;
        v = (v - p.c_neutral) * p.c_range_inv;
    } else {
        u = u - 0.5f;
        v = v - 0.5f;
    }
    return float3(y + p.coeffs.x * v,
                  y + p.coeffs.y * u + p.coeffs.z * v,
                  y + p.coeffs.w * u);
}

// Normalised texture coordinate of an output pixel inside the picture, or a
// point outside [0, 1] when the pixel lies in the letterbox.
static inline float2 playback_uv(uint2 gid,
                                 constant const PlaybackYuvParams &p) {
    return (float2(gid) + 0.5f - p.rect.xy) / p.rect.zw;
}

// GL NEAREST with clamp-to-edge on an integer texture: floor(texel).
static inline uint2 playback_nearest(float2 texel,
                                     uint2 size) {
    const int4 limit = int4(0, 0, size.x - 1, size.y - 1);
    const int2 pos = int2(floor(texel));
    return uint2(clamp(pos, limit.xy, limit.zw));
}

// 8-bit planes: R8Unorm, bilinear like the GL R8 pool.
kernel void playback_yuv_u8(
    texture2d<float> y_plane [[texture(0)]],
    texture2d<float> u_plane [[texture(1)]],
    texture2d<float> v_plane [[texture(2)]],
    texture2d<float, access::write> picture [[texture(3)]],
    constant const PlaybackYuvParams &params [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= picture.get_width() || gid.y >= picture.get_height()) {
        return;
    }
    const float2 uv = playback_uv(gid, params);
    if (any(uv < 0.0f) || any(uv > 1.0f)) {
        picture.write(float4(0.0f, 0.0f, 0.0f, 1.0f), gid);
        return;
    }
    const float2 chroma = uv * params.chroma_scale + params.chroma_offset;
    const float y = y_plane.sample(playback_sampler, uv).r;
    const float u = u_plane.sample(playback_sampler, chroma).r;
    const float v = v_plane.sample(playback_sampler, chroma).r;
    const float3 rgb = playback_yuv_to_rgb(y, u, v, params);
    picture.write(float4(rgb.r, rgb.g, rgb.b, 1.0f), gid);
}

// 10/12/16-bit planes: R16Uint, nearest like the GL R16UI pool.
kernel void playback_yuv_u16(
    texture2d<uint> y_plane [[texture(0)]],
    texture2d<uint> u_plane [[texture(1)]],
    texture2d<uint> v_plane [[texture(2)]],
    texture2d<float, access::write> picture [[texture(3)]],
    constant const PlaybackYuvParams &params [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= picture.get_width() || gid.y >= picture.get_height()) {
        return;
    }
    const float2 uv = playback_uv(gid, params);
    if (any(uv < 0.0f) || any(uv > 1.0f)) {
        picture.write(float4(0.0f, 0.0f, 0.0f, 1.0f), gid);
        return;
    }
    // Texel positions are formed directly from the pixel position, not from
    // the normalised coordinate: with a scale of exactly 1, 1/2 or 2 a sample
    // point on a texel boundary stays exactly there, as it does in the GL
    // shader.  Going through the normalised value rounds it to either side.
    const float2 pixel = float2(gid) + 0.5f - params.rect.xy;
    const float2 y_texel = pixel * params.luma_texel_scale;
    const float2 c_texel = pixel * params.chroma_texel_scale + params.chroma_offset_texels;
    const uint2 y_pos = playback_nearest(y_texel, uint2(y_plane.get_width(), y_plane.get_height()));
    const uint2 c_size = uint2(u_plane.get_width(), u_plane.get_height());
    const float y = float(y_plane.read(y_pos).r) / params.max_val;
    const float u = float(u_plane.read(playback_nearest(c_texel, c_size)).r) / params.max_val;
    const float v = float(v_plane.read(playback_nearest(c_texel, c_size)).r) / params.max_val;
    const float3 rgb = playback_yuv_to_rgb(y, u, v, params);
    picture.write(float4(rgb.r, rgb.g, rgb.b, 1.0f), gid);
}
"""


class BackPressure(Exception):
    """No resource is free for this frame right now; nothing was encoded."""


@dataclass
class _UploadSlot:
    """One set of Y/U/V textures; busy from upload until the GPU read ended."""
    planes: Tuple[Any, Any, Any] = ()
    shape: Tuple[int, int, int, int, int] = ()     # (depth, y_w, y_h, c_w, c_h)
    busy: bool = False

    def release(self) -> None:
        self.busy = False


@dataclass
class _RetiredPool:
    pool: OutputSlotPool
    textures: Dict[int, Any]
    retired_at: float


class PlaybackYuvRenderer:
    """Upload textures, parameter buffers and present slots of one canvas.

    Owned by the render thread.  ``context`` is the ``MetalComputeContext``
    the frames are encoded on (or a test double with the same methods).
    """

    def __init__(
        self,
        context: Any,
        *,
        pool_size: int = 4,
        upload_slots: int = 3,
        metal: Any = None,
    ) -> None:
        self._context = context
        self._pool_size = max(2, int(pool_size))
        self._upload_count = max(1, int(upload_slots))
        self._metal = metal
        self._plane_formats: Tuple[int, int] = (PIXEL_FORMAT_R8_UNORM, PIXEL_FORMAT_R16_UINT)
        self._max_texture = device_max_texture(getattr(context, "device", None))
        self._kernels = {
            8: context.compile_kernel(KERNEL_U8, PLAYBACK_YUV_MSL),
            16: context.compile_kernel(KERNEL_U16, PLAYBACK_YUV_MSL),
        }
        self._uploads: List[_UploadSlot] = []
        self._params: "OrderedDict[Tuple[Any, ...], Any]" = OrderedDict()
        self._pool: Optional[OutputSlotPool] = None
        self._slot_textures: Dict[int, Any] = {}
        self._size: Optional[Tuple[int, int]] = None
        self._retired: List[_RetiredPool] = []
        self.stats: Dict[str, int] = {"encoded": 0, "uploaded_bytes": 0, "pool_full": 0}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def max_texture(self) -> int:
        return self._max_texture

    @property
    def size(self) -> Optional[Tuple[int, int]]:
        return self._size

    @property
    def pool(self) -> Optional[OutputSlotPool]:
        return self._pool

    @property
    def uploads_busy(self) -> int:
        return sum(1 for slot in self._uploads if slot.busy)

    def stats_snapshot(self) -> dict:
        """Diagnostics: counters, slot states and texture counts."""
        snapshot = dict(self.stats)
        snapshot["uploads_busy"] = self.uploads_busy
        snapshot["upload_slots"] = len(self._uploads)
        snapshot["params_cached"] = len(self._params)
        snapshot["retired_pools"] = len(self._retired)
        if self._pool is not None:
            snapshot["slots"] = self._pool.stats()
        return snapshot

    # ------------------------------------------------------------------
    # 呈现槽位
    # ------------------------------------------------------------------

    def ensure_size(self, size: Tuple[int, int]) -> None:
        """Create the slot pool for ``size``; retire the previous one."""
        size = (int(size[0]), int(size[1]))
        if size == self._size and self._pool is not None:
            return
        if self._pool is not None:
            self._retired.append(_RetiredPool(self._pool, self._slot_textures, time.monotonic()))
        self._size = size
        self._pool = OutputSlotPool(size=self._pool_size)
        self._slot_textures = {}
        identity = str(getattr(self._context, "device_identity", ""))
        for slot_id in range(self._pool_size):
            texture = self._context.create_texture_2d(
                PIXEL_FORMAT_BGRA8_UNORM, size[0], size[1], shader_write=True)
            self._slot_textures[slot_id] = texture
            self._pool.register_slot(
                slot_id, texture_glo=0, width=size[0], height=size[1],
                resource=GpuPlaneResource(
                    api="metal", native_handle=texture,
                    resource_format=RESOURCE_FORMAT, width=size[0], height=size[1],
                    ownership="renderer_owned", device_identity=identity,
                ),
            )

    def acquire_slot(self, *, generation_id: int, frame_index: int) -> Optional[TextureLease]:
        """A writable present slot, or ``None`` when the viewport still holds them all."""
        if self._pool is None:
            return None
        lease = self._pool.acquire_for_writing(generation_id=generation_id, frame_index=frame_index)
        if lease is None:
            self.stats["pool_full"] += 1
        return lease

    def slot_state(self, slot_id: int) -> Optional[str]:
        if self._pool is None:
            return None
        lease = self._pool.get_slot(slot_id)
        return None if lease is None else lease.state.name

    def make_present(self, lease: TextureLease, *, frame: int, generation_id: int,
                     frame_meta: YuvFrame) -> GpuPresentFrame:
        """A native present descriptor for a slot the GPU has finished writing."""
        descriptor = NativePresentDescriptor.from_resource(
            lease.resource, origin=ORIGIN_TOP_LEFT,
            color=PresentColorInfo(
                # The kernel already turned limited YUV into display RGB.
                color_range="full", color_matrix="bt709",
                color_primaries="bt709", color_transfer="bt709",
            ),
        )
        return lease.make_present_frame(
            frame=frame, generation_id=generation_id,
            extra={
                "render_backend": "metal",
                "native_viewport": True,
                "playback": True,
                "bit_depth": int(frame_meta.bit_depth),
                "subsampling": str(frame_meta.subsampling),
                "source_matrix": int(frame_meta.matrix),
                "source_range": "full" if int(frame_meta.color_range) else "limited",
            },
            native=descriptor,
        )

    def ack(self, present: Any, reason: str = "") -> None:
        """The viewport is done with a published frame: its slot may be reused."""
        key = getattr(present, "lease_key", None)
        slot_id = int(getattr(present, "slot_id", -1))
        if slot_id < 0:
            return
        pools: List[OutputSlotPool] = []
        if self._pool is not None:
            pools.append(self._pool)
        pools.extend(item.pool for item in self._retired)
        for pool in pools:
            if key is not None and key.pool_generation != pool.generation:
                continue
            if pool.ack(slot_id, key):
                break

    def drain_retired(self) -> None:
        """Drop retired slot textures once no frame of them is out."""
        if not self._retired:
            return
        now = time.monotonic()
        remaining: List[_RetiredPool] = []
        for item in self._retired:
            states = item.pool.stats().get("states", {})
            busy = states.get("READY", 0) + states.get("IN_VIEWER", 0) + states.get("WRITING", 0)
            if busy and now - item.retired_at < _RETIRED_POOL_TIMEOUT_S:
                remaining.append(item)
                continue
            if busy:
                # A frame never came back (the viewport is gone).  The port was
                # shut down first, so nothing on the GPU reads these textures.
                logger.warning(
                    "PlaybackYuvRenderer: retiring slot pool %s with %d slot(s) still out",
                    item.pool.generation, busy,
                )
            item.pool.release_all()
        self._retired = remaining

    # ------------------------------------------------------------------
    # 一帧
    # ------------------------------------------------------------------

    def encode(self, frame: YuvFrame, output: Any, out_size: Tuple[int, int]) -> Callable[[], None]:
        """Upload ``frame`` and encode its conversion into ``output``.

        Returns the function that frees the upload textures again; register it
        as an input of the frame transaction so it runs when the GPU has
        finished reading them.  Raises :class:`BackPressure` (nothing encoded)
        when every upload set is still being read by an earlier frame.
        """
        slot = self._acquire_upload(frame)
        try:
            self._upload(slot, frame)
            params = self._params_buffer(frame, out_size)
            kernel = self._kernels[16 if int(frame.bit_depth) > 8 else 8]
            self._context.dispatch(
                kernel,
                buffers={0: params},
                textures={0: slot.planes[0], 1: slot.planes[1], 2: slot.planes[2], 3: output},
                threads=(int(out_size[0]), int(out_size[1]), 1),
            )
        except Exception:
            slot.release()
            raise
        self.stats["encoded"] += 1
        return slot.release

    def _acquire_upload(self, frame: YuvFrame) -> _UploadSlot:
        y_h, y_w = (int(value) for value in frame.y.shape)
        c_h, c_w = (int(value) for value in frame.u.shape)
        depth = 16 if int(frame.bit_depth) > 8 else 8
        shape = (depth, y_w, y_h, c_w, c_h)
        pixel_format = self._plane_format(depth)
        for slot in self._uploads:
            if slot.busy:
                continue
            if slot.shape != shape:
                slot.planes = self._new_plane_textures(pixel_format, shape)
                slot.shape = shape
            slot.busy = True
            return slot
        if len(self._uploads) >= self._upload_count:
            raise BackPressure("upload textures are still being read by earlier frames")
        slot = _UploadSlot(planes=self._new_plane_textures(pixel_format, shape), shape=shape, busy=True)
        self._uploads.append(slot)
        return slot

    def _new_plane_textures(self, pixel_format: int, shape: Tuple[int, int, int, int, int]) -> Tuple[Any, Any, Any]:
        _depth, y_w, y_h, c_w, c_h = shape
        return tuple(
            self._context.create_texture_2d(pixel_format, width, height)
            for width, height in ((y_w, y_h), (c_w, c_h), (c_w, c_h))
        )

    def _ensure_metal(self) -> None:
        if self._metal is not None:
            return
        import Metal  # only on the render thread, only on macOS

        self._metal = Metal
        # The binding is the authority on the packed formats' numbers.
        self._plane_formats = (int(Metal.MTLPixelFormatR8Unorm), int(Metal.MTLPixelFormatR16Uint))

    def _plane_format(self, depth_key: int) -> int:
        self._ensure_metal()
        return self._plane_formats[1 if depth_key == 16 else 0]

    def _upload(self, slot: _UploadSlot, frame: YuvFrame) -> None:
        self._ensure_metal()
        region = self._metal.MTLRegionMake2D
        for texture, plane in zip(slot.planes, (frame.y, frame.u, frame.v)):
            height, width = (int(value) for value in plane.shape)
            # memoryview hands the array to Metal without a copy; the texture
            # keeps its own (managed) copy, so the frame may go afterwards.
            texture.replaceRegion_mipmapLevel_withBytes_bytesPerRow_(
                region(0, 0, width, height), 0, memoryview(plane), int(plane.strides[0]),
            )
            self.stats["uploaded_bytes"] += int(plane.nbytes)

    def _params_buffer(self, frame: YuvFrame, out_size: Tuple[int, int]) -> Any:
        rect = letterbox_rect(frame.width, frame.height, int(out_size[0]), int(out_size[1]))
        key = params_key(frame, rect)
        buffer = self._params.get(key)
        if buffer is None:
            buffer = self._context.create_buffer_from_numpy(build_yuv_params(frame, rect))
            if len(self._params) >= _PARAMS_CACHE_LIMIT:
                self._params.clear()
            self._params[key] = buffer
        else:
            self._params.move_to_end(key)
        return buffer

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def read_slot(self, slot_id: int) -> Optional[np.ndarray]:
        """Read a slot back as top-down RGB8 (verification only; blocks the GPU)."""
        texture = self._slot_textures.get(int(slot_id))
        if texture is None or self._size is None:
            return None
        width, height = self._size
        raw = self._context.read_texture(texture, width * 4, height, np.uint8, region_width=width)
        bgra = np.asarray(raw).reshape(height, width, 4)
        return np.ascontiguousarray(bgra[..., 2::-1])

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def release_all(self) -> None:
        """Let go of every texture.  Call once the GPU is confirmed idle."""
        for item in self._retired:
            item.pool.release_all()
        self._retired = []
        if self._pool is not None:
            self._pool.release_all()
        self._slot_textures = {}
        self._pool = None
        self._size = None
        for slot in self._uploads:
            slot.planes = ()
            slot.busy = False
        self._uploads = []
        self._params.clear()
