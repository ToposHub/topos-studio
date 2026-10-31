"""
Windows Zero-Copy Capability Gate
缓存 probe 结果，给主工程 gate 使用。

## 环境变量

- TOPOS_VIDEO_ZERO_COPY=auto|off|force
  （AUD-36006：Windows 上 ``force`` 仅按 ``auto`` 处理——是否零拷贝由
  真实探测决定，不接受环境变量强制成功，与 d3d11_export_capability 的
  no-force 哲学一致；未知取值记录 warning 后按 auto 处理）
- TOPOS_VIDEO_FORCE_CPU_UPLOAD=1
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
from dataclasses import dataclass, field

from .reasons import FallbackReason, param

logger = logging.getLogger(__name__)


@dataclass
class WindowsZeroCopyCapability:
    """
    Windows 零拷贝能力描述。

    Attributes:
        available: 零拷贝是否可用
        reason: 不可用原因
        supported_codecs: 支持零拷贝的编码器集合
        supported_pixel_formats: 支持零拷贝的像素格式集合
        interop_method: interop 方法（wgl_nv_dx_interop, cuda_opengl 等）
        d3d_adapter_matches_gl_gpu: D3D adapter 是否匹配 GL GPU
        last_probe_json: 最近一次 probe 结果
    """
    available: bool = False
    reason: str = FallbackReason.NOT_PROBED.value
    supported_codecs: set = field(default_factory=lambda: {'h264', 'hevc', 'h265', 'vc1', 'vp9'})
    supported_pixel_formats: set = field(default_factory=lambda: {'nv12', 'p010'})
    interop_method: str = ""
    d3d_adapter_matches_gl_gpu: bool = False
    last_probe_json: str | None = None


# AUD-36006：显式校验取值 ∈ {auto, off, force}——原实现未知值（如拼错
# 的 "of"）静默按 auto 处理，违反外部数据须校验的纪律。
_raw_zero_copy_env = os.environ.get('TOPOS_VIDEO_ZERO_COPY', 'auto').lower()
if _raw_zero_copy_env in ('auto', 'off', 'force'):
    _zero_copy_env_var = _raw_zero_copy_env
else:
    logger.warning(
        "TOPOS_VIDEO_ZERO_COPY=%r 不是合法取值（auto|off|force），按 auto 处理",
        _raw_zero_copy_env,
    )
    _zero_copy_env_var = 'auto'
# 注意：Windows gate 不支持用 force 强制零拷贝成功（探测不过即降级），
# 'force' 当前语义等同 auto，仅为兼容文档曾声明过的取值而接受。
_force_cpu_upload = os.environ.get('TOPOS_VIDEO_FORCE_CPU_UPLOAD', '').lower() in ('1', 'true', 'yes')

_cached_capability: WindowsZeroCopyCapability | None = None


def _detect_wgl_nv_dx_interop() -> bool:
    """WGL_NV_DX_interop 扩展探测（W1-02 真机修正版）。

    注意 ``wglGetExtensionsStringEXT`` **不是** opengl32.dll 的直接导出，
    必须经 ``wglGetProcAddress`` 向当前 ICD 获取（AMD RX 6800 实测：直接
    ``CDLL().wglGetExtensionsStringEXT`` 抛 AttributeError，导致所有机器
    误报扩展缺失——旧实现的 bug）。

    需要当前 GL 上下文；任何失败（无上下文/无扩展/非 Windows）一律
    False——诚实降级，绝不猜。调用方注意：无上下文时的 False 只反映
    「本次调用时序」，不得当作机器事实缓存（见 probe_zero_copy_capability
    对 wgl_nv_dx_interop_unavailable 分支的处理）。
    """
    if platform.system() != "Windows":
        return False
    try:
        gl = ctypes.CDLL("opengl32.dll")
        # 无当前上下文时 wglGetProcAddress 查不到 ICD 扩展——先显式
        # 确认上下文存在，避免把「调用时序」误读成「扩展缺失」。
        gl.wglGetCurrentContext.restype = ctypes.c_void_p
        if not gl.wglGetCurrentContext():
            return False
        gl.wglGetProcAddress.restype = ctypes.c_void_p
        gl.wglGetProcAddress.argtypes = [ctypes.c_char_p]
        fn_ptr = gl.wglGetProcAddress(b"wglGetExtensionsStringEXT")
        if not fn_ptr:
            return False
        proto = ctypes.WINFUNCTYPE(ctypes.c_char_p)
        get_ext = ctypes.cast(fn_ptr, proto)
        exts = (get_ext() or b"").decode("ascii", errors="replace")
        return "WGL_NV_DX_interop" in exts
    except Exception:
        return False


def probe_zero_copy_capability() -> WindowsZeroCopyCapability:
    """
    获取 Windows 零拷贝能力（带缓存）。

    启用条件：
    - platform.system() == "Windows"
    - DXGI 适配器探测成功（P8-01：vendor/driver 信息随 last_probe_json 记录）
    - D3D11VA / NVDEC 可用
    - WGL_NV_DX_interop 或 CUDA-OpenGL interop 可用
    - adapter 匹配

    Returns:
        WindowsZeroCopyCapability
    """
    global _cached_capability

    if _cached_capability is not None:
        return _cached_capability

    cap = WindowsZeroCopyCapability()

    if platform.system() != 'Windows':
        cap.reason = FallbackReason.NOT_WINDOWS.value
        cap.available = False
        _cached_capability = cap
        return cap

    if _force_cpu_upload:
        cap.reason = FallbackReason.ENV_FORCE_CPU_UPLOAD.value
        cap.available = False
        _cached_capability = cap
        return cap

    if _zero_copy_env_var == 'off':
        cap.reason = FallbackReason.ENV_ZERO_COPY_OFF.value
        cap.available = False
        _cached_capability = cap
        return cap

    try:
        # P8-01：真实 GPU/驱动探测（替换原 "interop_not_yet_implemented" stub）。
        # available 由探测数据决定，不再是硬编码 False。
        from .gpu_capability import probe_gpu_capability

        gpu = probe_gpu_capability()
        cap.last_probe_json = gpu.to_json()

        if not gpu.probe_ok or gpu.primary_vendor == "unknown":
            cap.reason = param(FallbackReason.GPU_PROBE_FAILED, gpu.reason)
        elif gpu.primary_vendor == "nvidia" and not gpu.driver_version:
            # NVIDIA 链路的驱动版本是 capability gate 的一部分（§8.2：
            # 驱动版本不满足时 gate 拒绝专属路径）；读不到 = 不放行
            cap.reason = FallbackReason.NVIDIA_DRIVER_VERSION_UNAVAILABLE.value
        elif _detect_wgl_nv_dx_interop():
            cap.reason = ""
            cap.available = True
            cap.interop_method = "wgl_nv_dx_interop"
        else:
            # WGL 扩展本次不可见：可能是真缺失，也可能只是**此刻无
            # GL 上下文**（诊断入口可能在渲染线程建上下文前询问）。
            # 该负结果依赖调用时序而非机器事实——不进永久缓存，待有
            # 上下文后的下一次探测重判（探测成本毫秒级，调用频率为
            # 每媒体源/每诊断一次，可接受）。
            cap.reason = FallbackReason.WGL_NV_DX_INTEROP_UNAVAILABLE.value
            cap.available = False
            return cap
    except Exception as e:
        cap.reason = param(FallbackReason.PROBE_ERROR, str(e))
        cap.available = False
        logger.error(f"Windows zero-copy probe error: {e}")

    _cached_capability = cap
    return cap


def is_zero_copy_available(codec: str = "", pixel_format: str = "") -> bool:
    """快速判断 Windows 零拷贝是否可用"""
    if _zero_copy_env_var == 'off' or _force_cpu_upload:
        return False

    cap = probe_zero_copy_capability()
    if not cap.available:
        return False

    if codec and cap.supported_codecs and codec.lower() not in cap.supported_codecs:
        return False

    if pixel_format and cap.supported_pixel_formats and pixel_format.lower() not in cap.supported_pixel_formats:
        return False

    return True