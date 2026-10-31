"""
Frame Path Module
统一帧处理路径和解码路径的枚举定义。

## 职责

- DecodePath: 解码路径枚举（CPU解码、硬解下载、零拷贝等）
- RenderFramePath: 渲染路径枚举（CPU上传、GPU external texture等）

## 设计原则

- 所有路径必须明确命名，不允许模糊字符串
- 零拷贝路径必须标记为 zero_copy=True
- fallback 路径不得伪装成零拷贝

## 验收标准

- 每帧的 (decode_path, render_frame_path) 组合唯一且明确
- HUD/日志可以准确展示当前路径
- PerformanceMonitor 通过枚举值准确统计路径分布
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import threading
from typing import Any, Callable, Optional


class DecodeSurfaceState(str, Enum):
    """生命周期阶段 of a caller-owned decode surface.

    The codec only receives a mapped view during ``DECODING``.  It never owns
    the backing resource and never retains the pointer after the call returns.
    """

    UNACQUIRED = "unacquired"
    ACQUIRED = "acquired"
    MAPPED = "mapped"
    DECODING = "decoding"
    UNMAPPED = "unmapped"
    FENCED = "fenced"
    RELEASED = "released"


class DecodeSurfaceProtocolError(RuntimeError):
    """Raised when a platform surface violates the ownership protocol."""


@dataclass
class DecodeSurfaceLease:
    """Platform-neutral ownership contract for direct Topos surface decode.

    A platform adapter supplies callbacks for resource acquisition, mapping,
    unmapping, GPU fence insertion, and release.  ``map_callback`` returns the
    binding-layer plane-view array consumed synchronously by the codec.  The
    required call sequence is::

        acquire -> map -> decode -> unmap -> fence -> release

    ``zero_copy`` is descriptive only.  Adapters must set it to ``True`` only
    when the mapped address is the actual GPU/shared resource; a staging/PBO
    path must remain ``False`` and expose its copy in diagnostics.
    """

    backend: str
    memory_type: int
    resource: Any = None
    acquire_callback: Optional[Callable[[], None]] = None
    map_callback: Optional[Callable[[], Any]] = None
    unmap_callback: Optional[Callable[[], None]] = None
    fence_callback: Optional[Callable[[], None]] = None
    release_callback: Optional[Callable[[], None]] = None
    zero_copy: bool = False
    _state: DecodeSurfaceState = field(
        default=DecodeSurfaceState.UNACQUIRED, init=False, repr=False
    )
    _views: Any = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not str(self.backend).strip():
            raise ValueError("surface backend must be non-empty")
        if int(self.memory_type) not in (0, 1):
            raise ValueError(f"unsupported surface memory_type: {self.memory_type}")

    @property
    def state(self) -> DecodeSurfaceState:
        with self._lock:
            return self._state

    @property
    def views(self) -> Any:
        """Return mapped plane views; inaccessible after unmap."""
        with self._lock:
            if self._state not in (
                DecodeSurfaceState.MAPPED, DecodeSurfaceState.DECODING
            ):
                raise DecodeSurfaceProtocolError(
                    f"surface views unavailable in state {self._state.value}"
                )
            return self._views

    def _require(self, expected: DecodeSurfaceState) -> None:
        if self._state is not expected:
            raise DecodeSurfaceProtocolError(
                f"surface {self.backend} expected {expected.value}, "
                f"got {self._state.value}"
            )

    def acquire(self) -> None:
        with self._lock:
            self._require(DecodeSurfaceState.UNACQUIRED)
            if self.acquire_callback is not None:
                self.acquire_callback()
            self._state = DecodeSurfaceState.ACQUIRED

    def map(self) -> Any:
        with self._lock:
            self._require(DecodeSurfaceState.ACQUIRED)
            if self.map_callback is None:
                raise DecodeSurfaceProtocolError(
                    f"surface {self.backend} has no map callback"
                )
            views = self.map_callback()
            if views is None:
                raise DecodeSurfaceProtocolError(
                    f"surface {self.backend} map returned no views"
                )
            self._views = views
            self._state = DecodeSurfaceState.MAPPED
            return views

    def begin_decode(self) -> Any:
        with self._lock:
            self._require(DecodeSurfaceState.MAPPED)
            self._state = DecodeSurfaceState.DECODING
            return self._views

    def end_decode(self) -> None:
        with self._lock:
            self._require(DecodeSurfaceState.DECODING)
            self._state = DecodeSurfaceState.MAPPED

    def unmap(self) -> None:
        with self._lock:
            self._require(DecodeSurfaceState.MAPPED)
            if self.unmap_callback is not None:
                self.unmap_callback()
            self._views = None
            self._state = DecodeSurfaceState.UNMAPPED

    def fence(self) -> None:
        with self._lock:
            self._require(DecodeSurfaceState.UNMAPPED)
            if self.memory_type == 1 and self.fence_callback is None:
                raise DecodeSurfaceProtocolError(
                    f"GPU surface {self.backend} has no fence callback"
                )
            if self.fence_callback is not None:
                self.fence_callback()
            self._state = DecodeSurfaceState.FENCED

    def release(self) -> None:
        """Release once; early release is rejected to prevent use-after-free."""
        with self._lock:
            if self._state is DecodeSurfaceState.RELEASED:
                return
            if self._state not in (
                DecodeSurfaceState.FENCED, DecodeSurfaceState.UNMAPPED
            ):
                raise DecodeSurfaceProtocolError(
                    f"surface {self.backend} cannot release in state "
                    f"{self._state.value}"
                )
            if self.memory_type == 1 and self._state is not DecodeSurfaceState.FENCED:
                raise DecodeSurfaceProtocolError(
                    f"GPU surface {self.backend} requires a fence before release"
                )
            # AUD-34808：与 abort() 相同的 try/finally 语义——回调抛异常也
            # 必须让状态落位到 RELEASED，否则重试 release 会再次调用
            # release_callback（双释放风险）。
            try:
                if self.release_callback is not None:
                    self.release_callback()
            finally:
                self._views = None
                self._state = DecodeSurfaceState.RELEASED

    def abort(self) -> None:
        """Cancel an acquired-but-not-mapped lease after an adapter failure.

        Mapping can fail before a GPU surface has any consumer-visible work.
        In that narrow state the adapter may return its ring slot without a
        GPU fence; all normal decode paths must continue using ``release``
        after ``unmap``/``fence``.  Keeping this escape hatch explicit avoids
        leaking a slot when ``map_callback`` raises.
        """
        with self._lock:
            if self._state is DecodeSurfaceState.RELEASED:
                return
            self._require(DecodeSurfaceState.ACQUIRED)
            try:
                if self.release_callback is not None:
                    self.release_callback()
            finally:
                self._views = None
                self._state = DecodeSurfaceState.RELEASED

    def describe(self) -> dict[str, Any]:
        """Return diagnostics without exposing the mapped pointer."""
        with self._lock:
            return {
                "backend": self.backend,
                "memory_type": int(self.memory_type),
                "state": self._state.value,
                "zero_copy": bool(self.zero_copy),
                "resource_present": self.resource is not None,
                "mapped": self._state in (
                    DecodeSurfaceState.MAPPED, DecodeSurfaceState.DECODING
                ),
            }


class DecodePath(str, Enum):
    """
    解码路径枚举。

    标识视频帧从压缩码流到解码结果的完整路径。

    Values:
        CPU_PLANAR_YUV: CPU 解码，输出 planar YUV (如 yuv420p, yuv422p10le)
        CPU_PACKED_RGB: CPU 解码，输出 packed RGB (如 rgba, bgra)
        HWACCEL_DOWNLOAD_PACKED_RGB: 硬件解码后下载为 packed RGB 到 CPU
        HWACCEL_DOWNLOAD_PLANAR_YUV: 硬件解码后下载为 planar YUV 到 CPU
        MACOS_VT_IOSURFACE_ZERO_COPY: macOS VideoToolbox 零拷贝（CVPixelBuffer/IOSurface 保持 GPU resident）
        MACOS_TOPOS_IOSURFACE_ZERO_COPY: macOS Topos native decode 直写 IOSurface，供 Metal 导入
        MACOS_VT_CVMETAL_ZERO_COPY: macOS VideoToolbox 零拷贝（CVMetalTextureCache 直出 MTLTexture，Phase 5）
        WINDOWS_D3D11_GL_ZERO_COPY: Windows D3D11VA 零拷贝（D3D11 texture 通过 interop 直传 OpenGL）
        WINDOWS_CUDA_GL_ZERO_COPY: Windows cuda 驻留帧零拷贝（私有上下文 interop 拷入常驻 GL 纹理，B2.1）
    """

    CPU_PLANAR_YUV = "cpu_planar_yuv"
    CPU_PACKED_RGB = "cpu_packed_rgb"
    HWACCEL_DOWNLOAD_PACKED_RGB = "hwaccel_download_packed_rgb"
    HWACCEL_DOWNLOAD_PLANAR_YUV = "hwaccel_download_planar_yuv"
    MACOS_VT_IOSURFACE_ZERO_COPY = "macos_vt_iosurface_zero_copy"
    MACOS_TOPOS_IOSURFACE_ZERO_COPY = "macos_topos_iosurface_zero_copy"
    # Phase 5（P5-01/02）：同一 VT 解码，interop 改走 CVMetalTextureCache——
    # 产物是 MTLTexture plane（供 Metal 渲染/转换消费），不经过 GL。
    MACOS_VT_CVMETAL_ZERO_COPY = "macos_vt_cvmetal_zero_copy"
    WINDOWS_D3D11_GL_ZERO_COPY = "windows_d3d11_gl_zero_copy"
    # B2.1（计划表 B）：cuda 驻留帧经私有上下文 interop 拷入常驻 GL 纹理
    # （docs/poc_interop_b12_windows_2026-10.md）。含一次设备内 DtoA 拷贝，
    # 不经 CPU——与 D3D11 成员同口径计入零拷贝家族。
    WINDOWS_CUDA_GL_ZERO_COPY = "windows_cuda_gl_zero_copy"

    @property
    def is_zero_copy(self) -> bool:
        """是否为零拷贝路径"""
        return self in (
            DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY,
            DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY,
            DecodePath.MACOS_VT_CVMETAL_ZERO_COPY,
            DecodePath.WINDOWS_D3D11_GL_ZERO_COPY,
            DecodePath.WINDOWS_CUDA_GL_ZERO_COPY,
        )

    @property
    def is_cpu(self) -> bool:
        """是否为纯 CPU 路径"""
        return self in (DecodePath.CPU_PLANAR_YUV, DecodePath.CPU_PACKED_RGB)

    @property
    def is_hwaccel_download(self) -> bool:
        """是否为硬件解码后下载到 CPU 的路径"""
        return self in (
            DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB,
            DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV,
        )

    @property
    def needs_cpu_download(self) -> bool:
        """是否需要 CPU 下载（frame.to_ndarray() / frame.transfer()）"""
        return self.is_cpu or self.is_hwaccel_download


# P3-04：旧序列化值 → 限定值的透明别名（旧值隐含 opengl）。
# 注意：必须放模块级——Enum 类体内的 dict 属性会变成枚举成员。
_LEGACY_RENDER_PATH_ALIASES = {
    "gpu_decoded_external_yuv": "opengl_external_yuv",
    "gpu_decoded_external_rgb": "opengl_external_rgb",
}


class RenderFramePath(str, Enum):
    """
    渲染路径枚举。

    标识解码后帧数据如何进入 GPU 渲染管线。

    Values:
        PACKED_RGB_CPU_UPLOAD: packed RGB 通过 Texture.write() 上传到 GPU
        PLANAR_YUV_R8_UPLOAD: planar YUV 8-bit 通过 R8 texture 上传
        PLANAR_YUV_R16UI_UPLOAD: planar YUV 10/12/16-bit 通过 R16UI texture 上传
        NV12_R8_RG8_UPLOAD: NV12 格式 Y=R8, UV=RG8 上传
        P010_R16UI_UPLOAD: P010 格式 Y=R16UI, UV=RG16UI 上传
        GPU_DECODED_EXTERNAL_YUV: GPU 解码帧直接绑定 external YUV planes
        GPU_DECODED_EXTERNAL_RGB: GPU 解码帧直接绑定 external RGB texture
    """

    PACKED_RGB_CPU_UPLOAD = "packed_rgb_cpu_upload"
    PLANAR_YUV_R8_UPLOAD = "planar_yuv_r8_upload"
    PLANAR_YUV_R16UI_UPLOAD = "planar_yuv_r16ui_upload"
    NV12_R8_RG8_UPLOAD = "nv12_r8_rg8_upload"
    P010_R16UI_UPLOAD = "p010_r16ui_upload"
    GPU_DECODED_EXTERNAL_YUV = "gpu_decoded_external_yuv"
    GPU_DECODED_EXTERNAL_RGB = "gpu_decoded_external_rgb"
    # P3-04：graphics backend 限定的 external 路径（plan §5 Phase 3）。
    # 旧值 GPU_DECODED_EXTERNAL_* 隐含 opengl，保留为已序列化归档的
    # 兼容别名（from_serialized 透明映射），新产物一律写限定值。
    OPENGL_EXTERNAL_YUV = "opengl_external_yuv"
    OPENGL_EXTERNAL_RGB = "opengl_external_rgb"
    METAL_EXTERNAL_YUV = "metal_external_yuv"
    METAL_EXTERNAL_RGB = "metal_external_rgb"

    @classmethod
    def from_serialized(cls, value: str) -> "RenderFramePath":
        """反序列化：旧归档的隐含 opengl 值透明映射到限定值。

        未知值显式 ValueError——诊断而非静默（.clinerules:11）。
        """
        text = str(value).strip().lower()
        canonical = _LEGACY_RENDER_PATH_ALIASES.get(text, text)
        try:
            return cls(canonical)
        except ValueError:
            valid = ", ".join(m.value for m in cls)
            raise ValueError(
                f"unknown render frame path {value!r}; expected one of: {valid}"
            ) from None

    @classmethod
    def try_from_serialized(cls, value: str) -> Optional["RenderFramePath"]:
        """遥测用的安全解析：未知/自由格式诊断串返回 None 而非抛错。

        ``_debug_frame_path`` 是自由格式诊断串（'unknown'、
        'planar_yuv_r8_upload_gpu_failed' 等），性能统计不得因解析失败
        被跳过——严格解析留给归档/契约边界，遥测一律走本方法。
        """
        try:
            return cls.from_serialized(value)
        except ValueError:
            return None

    @classmethod
    def qualify_external(cls, graphics_api: str, is_rgb: bool) -> "RenderFramePath":
        """生产者侧：按实际 graphics backend 生成限定 external 路径。"""
        api = (graphics_api or "opengl").strip().lower()
        if api == "opengl":
            return (cls.OPENGL_EXTERNAL_RGB if is_rgb else cls.OPENGL_EXTERNAL_YUV)
        if api == "metal":
            return (cls.METAL_EXTERNAL_RGB if is_rgb else cls.METAL_EXTERNAL_YUV)
        raise ValueError(f"unsupported graphics backend for external path: {graphics_api!r}")

    @property
    def is_zero_copy(self) -> bool:
        """是否为零拷贝路径（无 CPU upload/download）"""
        return self in (
            RenderFramePath.GPU_DECODED_EXTERNAL_YUV,
            RenderFramePath.GPU_DECODED_EXTERNAL_RGB,
            RenderFramePath.OPENGL_EXTERNAL_YUV,
            RenderFramePath.OPENGL_EXTERNAL_RGB,
            RenderFramePath.METAL_EXTERNAL_YUV,
            RenderFramePath.METAL_EXTERNAL_RGB,
        )

    @property
    def needs_cpu_upload(self) -> bool:
        """是否需要 CPU upload（Texture.write()）"""
        return not self.is_zero_copy

    @property
    def needs_texture_write(self) -> bool:
        """是否需要 Texture.write() 写入视频帧"""
        return self.needs_cpu_upload


def classify_decode_path(
    hwaccel_enabled: bool = False,
    decoder_type: str | None = None,
    pixel_format: str = "",
    is_planar: bool = False,
    zero_copy_available: bool = False,
) -> DecodePath:
    """
    根据上下文推断解码路径。

    Args:
        hwaccel_enabled: 是否启用了硬件解码
        decoder_type: 解码器类型（'videotoolbox', 'topos', 'nvdec', 'qsv', 'd3d11va', 'cpu'）
        pixel_format: 像素格式名称
        is_planar: 是否为 planar 格式
        zero_copy_available: 零拷贝是否可用

    Returns:
        对应的 DecodePath 枚举值
    """
    if zero_copy_available:
        if decoder_type == 'videotoolbox':
            return DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY
        elif decoder_type == 'topos':
            return DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY
        elif decoder_type in ('d3d11va', 'nvdec', 'qsv'):
            return DecodePath.WINDOWS_D3D11_GL_ZERO_COPY

    if hwaccel_enabled and decoder_type not in (None, 'cpu'):
        if is_planar or 'yuv' in pixel_format.lower():
            return DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV
        else:
            return DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB

    if is_planar or 'yuv' in pixel_format.lower():
        return DecodePath.CPU_PLANAR_YUV

    return DecodePath.CPU_PACKED_RGB


def classify_render_frame_path(
    decode_path: DecodePath,
    pixel_format: str = "",
    bit_depth: int = 8,
    is_planar: bool = False,
    is_semi_planar: bool = False,
    graphics_api: str = "opengl",
) -> RenderFramePath:
    """
    根据解码路径和像素格式推断渲染帧路径。

    Args:
        decode_path: 解码路径
        pixel_format: 像素格式名称
        bit_depth: 位深度
        is_planar: 是否为 planar 格式
        is_semi_planar: 是否为 semi-planar（NV12/P010）
        graphics_api: 实际 graphics backend（P3-04：external 路径按其限定）

    Returns:
        对应的 RenderFramePath 枚举值
    """
    if decode_path.is_zero_copy:
        is_rgb = 'rgb' in pixel_format.lower() or 'rgba' in pixel_format.lower()
        try:
            return RenderFramePath.qualify_external(graphics_api, is_rgb)
        except ValueError:
            return (RenderFramePath.GPU_DECODED_EXTERNAL_RGB if is_rgb
                    else RenderFramePath.GPU_DECODED_EXTERNAL_YUV)

    if is_semi_planar:
        if bit_depth > 8:
            return RenderFramePath.P010_R16UI_UPLOAD
        return RenderFramePath.NV12_R8_RG8_UPLOAD

    if is_planar or 'yuv' in pixel_format.lower():
        if bit_depth > 8:
            return RenderFramePath.PLANAR_YUV_R16UI_UPLOAD
        return RenderFramePath.PLANAR_YUV_R8_UPLOAD

    return RenderFramePath.PACKED_RGB_CPU_UPLOAD


def is_zero_copy_combination(decode_path: DecodePath, render_path: RenderFramePath) -> bool:
    """
    判断 (decode_path, render_path) 组合是否为零拷贝。

    零拷贝必须同时满足：
    - 解码路径不经过 CPU
    - 渲染路径不经过 CPU upload

    Args:
        decode_path: 解码路径
        render_path: 渲染帧路径

    Returns:
        True 表示是零拷贝组合
    """
    return decode_path.is_zero_copy and render_path.is_zero_copy


def fallback_reason_for_decode_path(decode_path: DecodePath) -> str:
    """
    返回解码路径的 fallback 说明。

    用于 HUD 和日志中明确标记非零拷贝路径的原因。
    """
    if decode_path.is_zero_copy:
        return ""

    reasons = {
        DecodePath.CPU_PLANAR_YUV: "cpu_decode_no_hwaccel",
        DecodePath.CPU_PACKED_RGB: "cpu_decode_no_hwaccel",
        DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB: "hwaccel_cpu_download_packed_rgb",
        DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV: "hwaccel_cpu_download_planar_yuv",
    }
    return reasons.get(decode_path, "unknown")


def fallback_reason_for_render_path(render_path: RenderFramePath) -> str:
    """
    返回渲染路径的 fallback 说明。

    用于 HUD 和日志中明确标记 CPU upload 路径。
    """
    if render_path.is_zero_copy:
        return ""

    reasons = {
        RenderFramePath.PACKED_RGB_CPU_UPLOAD: "cpu_upload_packed_rgb",
        RenderFramePath.PLANAR_YUV_R8_UPLOAD: "cpu_upload_planar_yuv_r8",
        RenderFramePath.PLANAR_YUV_R16UI_UPLOAD: "cpu_upload_planar_yuv_r16ui",
        RenderFramePath.NV12_R8_RG8_UPLOAD: "cpu_upload_nv12_r8_rg8",
        RenderFramePath.P010_R16UI_UPLOAD: "cpu_upload_p010_r16ui",
    }
    return reasons.get(render_path, "cpu_upload_unknown")
