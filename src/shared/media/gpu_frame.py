"""
GPU Decoded Frame Module
GPU 驻留帧的统一数据抽象，用于零拷贝播放链路。

## 职责

- GpuFramePlane: 单个 GPU texture plane 描述（Y/UV/R/G/B/A 等）
- GpuFrameRelease: GPU frame 生命周期管理（retain/release/fence）
- GpuDecodedFrame: GPU 驻留帧完整描述

## 设计原则

- GpuDecodedFrame.zero_copy 对真正 external texture 路径必须为 True
- CPU upload/download fallback 必须继续用 DecodedFrame，不伪造 GpuDecodedFrame
- release.release() 不由 UI tick 调用，必须由渲染线程在安全时机调用
- GpuDecodedFrame planes 在构造期归一为不可变 tuple，发布后不可被
  生产者线程原地修改（类本身非 frozen：async_renderer 的惰性 VT 绑定
  需要整体替换 planes/release 字段，见 AUD-34813 说明）
- release 可以是可变的（需要线程安全地管理 GPU 资源生命周期）

## 验收标准

- GpuDecodedFrame 只描述 GPU resident 数据，不持有任何 CPU 数组
- release.release() 只在渲染线程安全时机调用
- 队列丢弃旧 GpuDecodedFrame 时 release exactly once
- Renderer 接手后，队列不提前 release

## 禁止

- GpuDecodedFrame 路径中不能 Texture.write
- GpuDecodedFrame 路径中不能 to_ndarray / transfer
- 失败必须 fallback，不许黑屏
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Literal, Any, Mapping

logger = logging.getLogger(__name__)


# Color metadata normalization constants
# Ensures consistent string representation across GPU and CPU paths
VALID_COLOR_MATRICES = {'bt709', 'bt2020nc', 'bt2020', 'smpte170m', 'bt470bg', 'fcc', 'unknown'}
VALID_COLOR_RANGES = {'limited', 'full', 'unknown'}

# Common aliases that should be normalized
_COLOR_MATRIX_ALIASES = {
    'bt.709': 'bt709',
    'bt709': 'bt709',
    '1': 'bt709',
    '2': 'bt470bg',
    'bt.2020': 'bt2020',
    'bt2020nc': 'bt2020nc',
    '9': 'bt2020',
    'bt.601': 'smpte170m',
    'bt601': 'smpte170m',
    'smpte170m': 'smpte170m',
    '6': 'smpte170m',
}

_COLOR_RANGE_ALIASES = {
    'tv': 'limited',
    'limited': 'limited',
    '0': 'limited',
    'pc': 'full',
    'full': 'full',
    '1': 'full',
}


def normalize_color_matrix(value) -> str:
    """
    Normalize color matrix to consistent string representation.

    Args:
        value: Color matrix value (string, int, or None)

    Returns:
        Normalized color matrix string
    """
    if value is None:
        return 'bt709'

    if isinstance(value, int):
        int_map = {1: 'bt709', 2: 'bt470bg', 6: 'smpte170m', 9: 'bt2020'}
        return int_map.get(value, 'bt709')

    if isinstance(value, str):
        return _COLOR_MATRIX_ALIASES.get(value.lower().strip(), value.lower().strip())

    return 'bt709'


def normalize_color_range(value) -> str:
    """
    Normalize color range to consistent string representation.

    Args:
        value: Color range value (string, int, or None)

    Returns:
        'limited' or 'full'
    """
    if value is None:
        return 'limited'

    if isinstance(value, int):
        return 'full' if value == 1 else 'limited'

    if isinstance(value, str):
        return _COLOR_RANGE_ALIASES.get(value.lower().strip(), 'limited')

    return 'limited'


# GPU API 类型标识
GpuApi = Literal["opengl", "metal", "d3d11", "cuda", "unknown"]

# Texture target 类型
TextureTarget = Literal["2d", "rectangle", "external", "unknown"]

# Plane component 类型
PlaneComponent = Literal["Y", "UV", "U", "V", "R", "G", "B", "A", "RGBA"]

# Plane 资源所有权（P2-03：borrowed = 解码器拥有，fence 后归还；
# retained = 帧持有引用；renderer_owned = 渲染器池拥有，lease 回收）
PlaneOwnership = Literal["borrowed", "retained", "renderer_owned"]


@dataclass(frozen=True)
class GpuPlaneResource:
    """
    后端无关的 plane 资源描述（P2-03，plan §3.3 GPU 帧契约）。

    规则：
    - ``native_handle`` 只能由对应 backend 解读，上层只读描述、不得解释：
      opengl -> GL texture name (int)；metal -> MTLTexture 对象；
      d3d11 -> shared texture handle。
    - ``resource_format`` 用后端中立词汇（r8unorm / r16uint / rg8unorm /
      bgra8unorm / nv12 ...），不写 GL 魔数。
    - 资源释放由拥有它的 backend 在 fence 安全后执行（P2-04），
      present 路径复用 present_frame.TextureLease 状态机，不建第二套 lease。
    """
    api: GpuApi
    native_handle: int | object
    resource_format: str
    width: int
    height: int
    stride_bytes: int = 0
    target: TextureTarget = "2d"
    ownership: PlaneOwnership = "renderer_owned"
    device_identity: str = ""

    def as_descriptor(self) -> dict[str, object]:
        """诊断用描述（不含裸 handle 数值，避免上层误用）。"""
        return {
            "api": self.api,
            "resource_format": self.resource_format,
            "width": self.width,
            "height": self.height,
            "stride_bytes": self.stride_bytes,
            "target": self.target,
            "ownership": self.ownership,
            "device_identity": self.device_identity,
            "handle_kind": type(self.native_handle).__name__,
        }


@dataclass(frozen=True)
class GpuFramePlane:
    """
    GPU texture plane 描述。

    描述解码后的单个 GPU texture plane（如 Y plane, UV plane）。
    不拥有 texture 生命周期。

    Attributes:
        component: plane 的色彩分量（Y, UV, U, V 等）
        api: GPU API 类型（opengl, metal, d3d11, cuda）
        texture_id: OpenGL/Metal/D3D11 texture ID 或 handle
        target: texture target（2d, rectangle, external）
        width: texture 宽度
        height: texture 高度
        internal_format: OpenGL/Metal 内部格式（如 GL_R8, GL_RG8, GL_R16UI）
        pixel_format: 像素格式描述（如 NV12_Y, NV12_UV, P010_Y, P010_UV）
        pixel_type: 像素类型（如 GL_UNSIGNED_BYTE, GL_UNSIGNED_SHORT）
        texture_unit_hint: 建议绑定的 texture unit（默认 0）
    """
    component: PlaneComponent
    api: GpuApi
    texture_id: int
    target: TextureTarget
    width: int
    height: int
    internal_format: str = ""
    pixel_format: str = ""
    pixel_type: str = ""
    texture_unit_hint: int = 0
    resource: Optional[GpuPlaneResource] = None

    def __post_init__(self) -> None:
        # P2-03 契约：裸 texture_id 是 OpenGL 对象名语义；非 GL 后端必须
        # 携带 GpuPlaneResource（native_handle 由对应后端解释）。
        if self.resource is None and self.api != "opengl":
            raise ValueError(
                f"GpuFramePlane(api={self.api!r}) requires a GpuPlaneResource; "
                f"bare texture_id is an OpenGL object name"
            )

    def resolved_handle(self) -> int | object:
        """后端消费者取 handle：优先 resource.native_handle，GL 兼容回退 texture_id。"""
        if self.resource is not None:
            return self.resource.native_handle
        return self.texture_id


@dataclass
class GpuFrameRelease:
    """
    GPU frame 生命周期管理（P2-04：backend-owned fence/release contract）。

    管理 GPU texture / IOSurface / MTLTexture / D3D11 texture 的生命周期。
    release callback 在渲染线程安全时机调用；释放由拥有资源的 backend
    在 fence 安全后执行（plan §3.3 规则 3 / §7.2）。

    exactly-once 语义覆盖：丢帧（队列丢弃）、seek（generation 递增）、
    取消、窗口关闭——release() 幂等，重复调用无害。

    Attributes:
        retain_token: 保留令牌（如 CVPixelBuffer ref 或 D3D11 shared handle）
        release_callback: 释放回调（在渲染线程安全时调用）
        fence: GPU fence 对象（可选，用于确认渲染完成）
        destructor_policy: 析构策略 ("warn_only" | "thread_safe"；
                       "enqueue" 尚未实现，构造期拒绝——AUD-34814）
        owner_backend: 拥有资源的 backend（opengl/metal/d3d11/cuda），
                       决定哪个 backend 的渲染线程执行 release
    """
    retain_token: object | None = None
    release_callback: Optional[Callable[[], None]] = None
    fence: object | None = None
    destructor_policy: str = "warn_only"
    owner_backend: str = "opengl"
    _released: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        """校验析构策略（AUD-34814）。

        "enqueue"（__del__ 把 release 请求投递到全局 render-thread
        release queue）尚未实现——在此之前构造期直接拒绝，防止 GC 时
        静默降级为泄漏告警、资源回收承诺不成立。
        """
        if self.destructor_policy == "enqueue":
            raise ValueError(
                'GpuFrameRelease.destructor_policy="enqueue" is not '
                'implemented yet; use "warn_only" or "thread_safe"'
            )

    def release(self) -> None:
        """
        释放 GPU frame 资源。

        只调用一次 release_callback，防止重复释放。
        必须由渲染线程在安全时机调用，不能由 UI tick 调用。
        """
        with self._lock:
            if self._released:
                logger.debug("GpuFrameRelease: already released, skipping")
                return
            self._released = True
            callback = self.release_callback

        if callback is not None:
            try:
                callback()
            except Exception as e:
                logger.warning(f"GpuFrameRelease: release callback error: {e}")

    def release_when_fenced(self, wait_fn: Callable[[object], bool]) -> bool:
        """等 fence 安全后再释放（plan §7.2：fence complete → release）。

        ``wait_fn(fence) -> bool`` 由 backend 提供（GL: glClientWaitSync /
        Metal: waitUntilCompleted 的已缓存结果 / CPU 测试: lambda）。返回
        False 表示 fence 未确认——此时**不释放**资源（宁可延迟回收也不
        释放使用中的资源），调用方需在安全时机重试。

        exactly-once：与 release() 共享同一 _released 位，先到先释放。
        """
        with self._lock:
            if self._released:
                return True
            fence = self.fence
        if fence is None or wait_fn(fence):
            self.release()
            return True
        return False

    @property
    def is_released(self) -> bool:
        """是否已经释放"""
        with self._lock:
            return self._released


@dataclass
class GpuDecodedFrame:
    """
    GPU 驻留帧完整描述。

    描述从硬件解码器直接输出的 GPU resident frame。
    与 DecodedFrame（CPU ndarray）对应，GpuDecodedFrame 不持有任何 CPU 数据。

    重要约束：
    - zero_copy=True 只用于真正 external texture 路径
    - CPU upload/download fallback 继续用 DecodedFrame
    - release 不由 UI tick 调用

    Attributes:
        frame_index: 帧号
        pts: 展示时间戳（stream time_base 单位）
        time_seconds: 展示时间（秒）
        width: 视频宽度
        height: 视频高度
        visible_rect: 可见矩形区域（left, top, right, bottom）
        pixel_format: 像素格式描述（nv12, p010 等）
        bit_depth: 位深度（8, 10, 12, 16）
        planes: GPU texture plane 元组（构造期由任意序列归一为 tuple，
            发布后不可原地修改——AUD-34813）
        color_range: 色彩范围（limited, full）
        color_matrix: 色彩矩阵（bt709, bt2020 等）
        color_primaries: 色彩原色
        color_transfer: 色彩传输函数
        decode_path: 解码路径（与 DecodePath 枚举对应）
        zero_copy: 是否为零拷贝路径（真正 external texture 必须为 True）
        release: GPU frame 生命周期管理
    """
    frame_index: int
    pts: int | None
    time_seconds: float
    width: int
    height: int
    visible_rect: tuple[int, int, int, int] | None
    pixel_format: str
    bit_depth: int
    planes: Sequence[GpuFramePlane]
    color_range: str = "limited"
    color_matrix: str = "bt709"
    color_primaries: str | None = None
    color_transfer: str | None = None
    decode_path: str = ""
    zero_copy: bool = True
    release: GpuFrameRelease = field(default_factory=GpuFrameRelease)
    source_kind: str = "unknown"
    device_identity: str = ""
    duration_seconds: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """验证数据完整性"""
        # AUD-34813：planes 归一为不可变 tuple——生产者（解码线程）发布
        # 帧后，消费方（渲染线程）读到的是不可变序列，消除发布后被原地
        # 修改的竞争窗口。类保持非 frozen：惰性 VT 绑定路径需要整体替换
        # planes/release 字段（见模块设计原则）。
        if not isinstance(self.planes, tuple):
            self.planes = tuple(self.planes)
        if self.zero_copy and not self.planes:
            raise ValueError(
                "GpuDecodedFrame.zero_copy=True requires at least one plane"
            )
        if self.zero_copy and self.decode_path and 'cpu' in self.decode_path:
            raise ValueError(
                f"GpuDecodedFrame.zero_copy=True but decode_path={self.decode_path} "
                f"suggests CPU path; use DecodedFrame for CPU paths"
            )
        if self.width <= 0 or self.height <= 0:
            raise ValueError("GpuDecodedFrame dimensions must be positive")
        if self.duration_seconds is not None and self.duration_seconds < 0.0:
            raise ValueError("GpuDecodedFrame.duration_seconds cannot be negative")
        if self.device_identity:
            for plane in self.planes:
                if (plane.resource is not None
                        and plane.resource.device_identity
                        and plane.resource.device_identity != self.device_identity):
                    raise ValueError(
                        "GpuDecodedFrame plane device does not match frame device"
                    )

    def validate_for_device(
        self,
        *,
        api: str,
        device_identity: str = "",
    ) -> str:
        """Return a machine-readable contract failure, or ``""``.

        A producer may leave ``device_identity`` empty when the platform does
        not expose a stable string. If both sides do provide one, mismatch is
        fatal: copying to another device would violate the zero-copy contract.
        """
        expected_api = str(api or "").strip().lower()
        if not self.zero_copy:
            return "frame_not_zero_copy"
        if not self.planes:
            return "frame_planes_missing"
        if expected_api and any(plane.api != expected_api for plane in self.planes):
            return f"graphics_api_mismatch:{expected_api}"
        if (device_identity and self.device_identity
                and device_identity != self.device_identity):
            return (
                f"device_mismatch:frame={self.device_identity}"
                f"|consumer={device_identity}"
            )
        for plane in self.planes:
            resource = plane.resource
            if (resource is not None and device_identity
                    and resource.device_identity
                    and resource.device_identity != device_identity):
                return (
                    f"device_mismatch:plane={resource.device_identity}"
                    f"|consumer={device_identity}"
                )
        return ""

    def __del__(self) -> None:
        """
        析构时根据 destructor_policy 处理未释放的 GPU 资源。

        - "warn_only"（默认）：只记录警告日志，不调用 release()。
          原因：__del__ 可在任意 GC 线程执行，而 release_callback
          可能包含 GL/CVPixelBuffer 操作，需要从渲染线程安全调用。
        - "thread_safe"：release_callback 明确是线程安全的，__del__ 可调用 release()。
          只用于 tests、CPU-only fake、或 release callback 已经封送到安全线程。
        - "enqueue"：__del__ 把 release 请求投递到全局 release queue，由渲染线程 drain。
        """
        if not hasattr(self, 'release') or self.release.is_released:
            return
        policy = self.release.destructor_policy
        if policy == "thread_safe":
            self.release.release()
        elif policy == "enqueue":
            # Future: enqueue_gpu_release(self.release) when render-thread queue exists
            logger.warning(
                f"GpuDecodedFrame.__del__: frame={self.frame_index} "
                f"enqueue policy not yet implemented, falling back to warn."
            )
        else:
            logger.warning(
                f"GpuDecodedFrame.__del__: frame={self.frame_index} "
                f"leaked (not released before GC). "
                f"Call safe_release_gpu_frame() from the render thread."
            )


# ================================================================
# 类型别名和工具函数
# ================================================================

# VideoFrameLike: 可以是 CPU DecodedFrame 或 GPU GpuDecodedFrame
# DecodedFrame 来自 src.core.media.frame，这里只做类型注解不做导入
VideoFrameLike = Any  # DecodedFrame | GpuDecodedFrame


def is_gpu_decoded_frame(frame: Any) -> bool:
    """
    检查是否为 GpuDecodedFrame。

    Args:
        frame: 任意帧对象

    Returns:
        True 如果是 GpuDecodedFrame 实例
    """
    return isinstance(frame, GpuDecodedFrame)


def is_zero_copy_frame(frame: Any) -> bool:
    """
    检查帧是否为零拷贝路径。

    Args:
        frame: 任意帧对象

    Returns:
        True 如果是 GpuDecodedFrame 且 zero_copy=True
    """
    return isinstance(frame, GpuDecodedFrame) and frame.zero_copy


def get_frame_decode_path(frame: Any) -> str:
    """
    获取帧的解码路径标识。

    Args:
        frame: DecodedFrame 或 GpuDecodedFrame

    Returns:
        decode_path 字符串
    """
    if isinstance(frame, GpuDecodedFrame):
        return frame.decode_path or "gpu_decoded_unknown"
    return "cpu_unknown"


def get_frame_render_path(frame: Any) -> str:
    """
    获取帧的渲染路径标识。

    Args:
        frame: DecodedFrame 或 GpuDecodedFrame

    Returns:
        render_frame_path 字符串（与 RenderFramePath 枚举对应）
    """
    from src.shared.media.frame_path import RenderFramePath

    if isinstance(frame, GpuDecodedFrame):
        if frame.zero_copy and frame.planes:
            is_rgb = any(p.component == "RGBA" for p in frame.planes)
            api = frame.planes[0].api or "opengl"
            try:
                return RenderFramePath.qualify_external(api, is_rgb).value
            except ValueError:
                # 未知 api（未来后端）回退旧通用值，避免归档崩溃
                return (RenderFramePath.GPU_DECODED_EXTERNAL_RGB.value if is_rgb
                        else RenderFramePath.GPU_DECODED_EXTERNAL_YUV.value)
        return RenderFramePath.PACKED_RGB_CPU_UPLOAD.value  # fallback

    # DecodedFrame 路径推断
    if hasattr(frame, 'is_planar') and frame.is_planar:
        bit_depth = getattr(frame, 'bit_depth', 8)
        if bit_depth > 8:
            return RenderFramePath.PLANAR_YUV_R16UI_UPLOAD.value
        return RenderFramePath.PLANAR_YUV_R8_UPLOAD.value

    if hasattr(frame, 'data') and frame.data is not None:
        return RenderFramePath.PACKED_RGB_CPU_UPLOAD.value

    return "unknown"


def safe_release_gpu_frame(frame: Any) -> None:
    """
    安全释放 GPU frame 资源。

    只对 GpuDecodedFrame 类型有效，对其他类型不做任何操作。
    可以从任何线程调用，release 内部负责线程安全。

    Args:
        frame: 任意帧对象
    """
    if isinstance(frame, GpuDecodedFrame):
        frame.release.release()
