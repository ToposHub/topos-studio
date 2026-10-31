"""Windows CUDA 驻留帧契约与 CUDA→GL interop 会话（计划表 B · B2.1）。

B1.2 产物落地（依据 ``docs/poc_interop_b12_windows_2026-10.md``，L2 真机）：

- **路线**：PyAV ``HWAccel(cuda, is_hw_owned=True)`` 保留设备帧 → 解码线程
  解析 DLPack 设备指针产出 :class:`CudaResidentFrame`（不触 CUDA API）→
  渲染 GL 线程 :class:`CudaGlInteropSession` 以私有 CUDA 上下文
  ``cuGraphicsGLRegisterImage`` + 每平面 map→DtoA→unmap 拷入常驻 GL 纹理
  → GpuDecodedFrame(api=opengl) 走既有零拷贝消费通路。
- **-129 毒化规则**：interop 一律私有上下文（cuCtxCreate），主上下文
  零触碰——PyAV ``is_hw_owned=True`` 会话在主上下文被本进程初始化过后
  永久失败（unbind 无效，reset 破坏活跃会话）。
- **绑定层**：cuda-bindings（cuda-python 13.x）为唯一受支持绑定
  （手写 ctypes 的 ``cuMemcpy2D_v2`` 结构体在本机驱动被拒，见 B1.2 §3-1）。
- **句柄纪律**：驱动不校验 interop 句柄（垃圾值直接访问违例），资源
  注册/注销只在本模块内完成。
- **同步**：map/unmap 提供双向同步（CUDA 写序 + 复用前等待在途回读），
  拷贝完成后 GL 纹理与源 av 帧独立，源帧可立即释放。
- **纹理语义**：与既有外部平面零拷贝 shader（归一化 sampler2D）兼容——
  NV12 → GL_R8/GL_RG8（moderngl f1）；P010/P016 → GL_R16/GL_RG16 UNORM
  （moderngl 无此 dtype，经 raw OpenGL 创建；CUDA array 格式与 R16UI 同为
  UNSIGNED_INT16，拷贝路径 B1.2 已验证，MSB 对齐码值经 /65535 归一化与
  hw_planar_source 的 p016le 契约同语义）。

opt-in：``TOPOS_WIN_HW_RESIDENT_DECODE`` ∈ {1,true,yes}，默认关闭
（计划 B2.1：先 opt-in，默认启用另需 L3 证据）。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Sequence

from src.shared.media.gpu_frame import GpuFramePlane, GpuFrameRelease
from src.shared.media.hwaccel import HardwareDecoder
from src.shared.media.source import MediaDecodeError, MediaOpenError

from .hw_planar_source import WindowsHwPlanarSource
from .reasons import codec_unsupported_reason

logger = logging.getLogger(__name__)

OPT_IN_ENV = "TOPOS_WIN_HW_RESIDENT_DECODE"

_TRUTHY = ("1", "true", "yes")


def resident_decode_enabled() -> bool:
    """驻留解码 opt-in 开关（默认关闭；计划 B2.1 门禁）。"""
    return os.environ.get(OPT_IN_ENV, "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# 运行中降级门闩（B2.1）：GL 导入连续失败 → 新源停止驻留路由。
#
# 失败模式分析（B1.2 失效注入）：运行中导入失败的现实原因是全局性的
# （CUDA 上下文/设备级、绑定层缺席），而非单源属性——故 v1 为**全局
# 连续失败门闩**：连续 N 次导入失败后 tripped，`resident_route_available`
# 对新源恒 False（既有源按既有逐帧回退链：track_skip→render_error→
# worker 回退），任何一次成功导入即复位连续计数。门闩进程内不自动
# 解除（设备级故障不会自愈）；诊断/测试用 reset 接口。
# ---------------------------------------------------------------------------

_IMPORT_FAILURE_LATCH_THRESHOLD = 3
_import_failure_lock = threading.Lock()
_consecutive_import_failures = 0
_import_failure_latch_tripped = False


def register_import_success() -> None:
    """渲染线程导入成功：复位连续失败计数（门闩状态不变）。"""
    global _consecutive_import_failures
    with _import_failure_lock:
        _consecutive_import_failures = 0


def register_import_failure() -> None:
    """渲染线程导入失败：累计计数，达阈值触发门闩。"""
    global _consecutive_import_failures, _import_failure_latch_tripped
    with _import_failure_lock:
        _consecutive_import_failures += 1
        if _consecutive_import_failures >= _IMPORT_FAILURE_LATCH_THRESHOLD:
            _import_failure_latch_tripped = True


def import_failure_latch_tripped() -> bool:
    with _import_failure_lock:
        return _import_failure_latch_tripped


def reset_import_failure_latch() -> None:
    """诊断/测试：解除门闩并复位计数。"""
    global _consecutive_import_failures, _import_failure_latch_tripped
    with _import_failure_lock:
        _consecutive_import_failures = 0
        _import_failure_latch_tripped = False


def resident_route_available() -> bool:
    """驻留路由是否可用（新源创建门）：opt-in 开启且门闩未触发。"""
    return resident_decode_enabled() and not import_failure_latch_tripped()


# ---------------------------------------------------------------------------
# CUcontext 比较助手（W02）：cuda.bindings 的 CUcontext 为值对象（== 按
# 句柄值比较）；替身测试可能返回任意对象，比较失败按"不同"处理（保守
# 切换 + 恢复，不误跳过守卫）。
# ---------------------------------------------------------------------------

def _cu_ctx_ptr(ctx: Any) -> Optional[int]:
    """CUcontext 原始句柄值；替身对象不可解析时返回 None。

    真绑定的句柄值经 ``int(CUcontext)`` 取得（``getPtr()`` 返回的是
    Python 包装对象地址，同值句柄的两个包装也会不同，不能用于比较）。
    """
    if ctx is None:
        return 0
    try:
        return int(ctx)  # type: ignore[call-overload]
    except Exception:  # noqa: BLE001
        pass
    getter = getattr(ctx, "getPtr", None)
    if callable(getter):
        try:
            return int(getter())
        except Exception:  # noqa: BLE001
            pass
    return None


def _cu_ctx_same(a: Any, b: Any) -> bool:
    pa, pb = _cu_ctx_ptr(a), _cu_ctx_ptr(b)
    if pa is not None and pb is not None:
        return pa == pb
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001
        return False


def _cu_ctx_is_null(ctx: Any) -> bool:
    p = _cu_ctx_ptr(ctx)
    return p == 0


def _null_ctx(cu: Any) -> Any:
    """空 CUcontext（弹出 current 用）：真绑定为 CUcontext()；替身可缺省。"""
    ctor = getattr(cu, "CUcontext", None)
    if ctor is None:
        return None
    try:
        return ctor()
    except Exception:  # noqa: BLE001
        return None


def probe_cuda_resident_capability() -> tuple[bool, str]:
    """cuda 驻留路线能力门（每次调用实测，不缓存——环境可变）。

    Returns:
        (available, fallback_reason)：available=False 时 reason 非空。
    """
    if sys.platform != "win32":
        return False, "not_windows"
    try:
        ctypes.WinDLL("nvcuda.dll")
    except OSError:
        return False, "nvcuda_missing"
    try:
        from cuda.bindings import driver  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"cuda_bindings_unavailable:{type(exc).__name__}"
    return True, ""


def resident_capability_report() -> dict:
    """W06 能力诊断：cuda 驻留路线的完整探测链（打包/安装记录用）。

    逐层报告（JSON 可序列化）：平台门 → nvcuda.dll → cuda-bindings
    （含版本）→ opt-in 开关 → 全局门闩 → 设备身份（ordinal/名称/驱动，
    经 cuInit 后轻量枚举，不建上下文）。每层带 ok/reason，缺席层如实
    报缺——安装包据此输出"实际后端/缺依赖原因"，不冒充。
    """
    report: dict = {
        "probe": "windows_cuda_resident",
        "platform_ok": sys.platform == "win32",
        "platform": sys.platform,
        "nvcuda_ok": False,
        "cuda_bindings_ok": False,
        "cuda_bindings_version": None,
        "opt_in_env": bool(os.environ.get("TOPOS_WIN_HW_RESIDENT_DECODE")),
        "hw_kill_switch": bool(
            os.environ.get("TOPOS_DISABLE_WIN_HW_DECODE")),
        "import_latch_tripped": import_failure_latch_tripped(),
        "route_available": False,
        "reason": "not_windows",
        "device": None,
    }
    if not report["platform_ok"]:
        return report
    try:
        ctypes.WinDLL("nvcuda.dll")
        report["nvcuda_ok"] = True
    except OSError as exc:
        report["reason"] = f"nvcuda_missing:{exc}"
        return report
    try:
        import cuda.bindings as _cb

        report["cuda_bindings_ok"] = True
        report["cuda_bindings_version"] = getattr(
            _cb, "__version__", None)
    except Exception as exc:  # noqa: BLE001
        report["reason"] = (
            f"cuda_bindings_unavailable:{type(exc).__name__}:{exc}")
        return report
    available, reason = probe_cuda_resident_capability()
    report["route_available"] = available
    report["reason"] = reason or "ok"
    # 设备身份（轻量：cuInit + 枚举，不创建上下文/不动 GL）
    try:
        from cuda.bindings import driver as cu

        err, = cu.cuInit(0)
        if int(err) == 0:
            err, count = cu.cuDeviceGetCount()
            if int(err) == 0:
                report["device"] = {"count": int(count), "items": []}
                for ordinal in range(min(int(count), 8)):
                    err2, name_bytes = cu.cuDeviceGetName(
                        128, cu.CUdevice(ordinal))
                    name = (name_bytes.split(b"\x00")[0].decode(
                        "ascii", "replace") if int(err2) == 0 else "")
                    report["device"]["items"].append(
                        {"ordinal": ordinal, "name": name})
                err3, drv = cu.cuDriverGetVersion()
                if int(err3) == 0:
                    report["device"]["driver_version"] = int(drv)
    except Exception as exc:  # noqa: BLE001 —— 诊断层失败不掩蔽主结果
        report["device_probe_error"] = f"{type(exc).__name__}:{exc}"
    return report


# ---------------------------------------------------------------------------
# DLPack 胶囊解析（B1.1/B1.2 实证 ABI，PyAV 18.1.0；正确性以同解码器
# 下载参考 md5 逐帧对拍验证——纯 Python 内存读，不触 CUDA API）
# ---------------------------------------------------------------------------

def parse_dlpack_capsule(capsule: Any) -> dict:
    pycapsule = ctypes.pythonapi
    pycapsule.PyCapsule_GetName.restype = ctypes.c_char_p
    pycapsule.PyCapsule_GetName.argtypes = [ctypes.py_object]
    pycapsule.PyCapsule_GetPointer.restype = ctypes.c_void_p
    pycapsule.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    name = pycapsule.PyCapsule_GetName(capsule)
    base = pycapsule.PyCapsule_GetPointer(capsule, name)
    itemsize = max(1, ctypes.c_uint8.from_address(base + 21).value // 8)
    ndim = ctypes.c_int32.from_address(base + 16).value
    shape_ptr = ctypes.c_void_p.from_address(base + 24).value or 0
    strides_ptr = ctypes.c_void_p.from_address(base + 32).value
    byte_offset = ctypes.c_uint64.from_address(base + 40).value
    data_ptr = ctypes.c_void_p.from_address(base + 0).value
    shape = [ctypes.c_int64.from_address(shape_ptr + 8 * i).value
             for i in range(ndim)]
    strides = ([ctypes.c_int64.from_address(strides_ptr + 8 * i).value
                for i in range(ndim)] if strides_ptr else None)
    return {
        "itemsize": itemsize, "shape": shape, "strides": strides,
        "data_ptr": data_ptr, "byte_offset": byte_offset,
    }


def plane_pitch_bytes(info: dict, row_bytes: int) -> int:
    """strides 为元素单位（B1.1 实证）；无 strides 视为紧凑行距。"""
    if info["strides"]:
        return info["strides"][0] * info["itemsize"]
    return row_bytes


def validate_resident_plane_geometry(
    width: int,
    height: int,
    contract: str,
    y_info: dict,
    uv_info: dict,
) -> tuple[CudaResidentPlane, CudaResidentPlane]:
    """W04 平面格式能力门：校验 DLPack 几何并构造拷贝平面（fail-closed）。

    只接受 4:2:0 半平面（NV12/P016 家族，双平面交错 UV）。拷贝不变量：

    - 平面数恰为 2（4:2:2/4:4:4 交错格式显式拒绝，不错读 UV）；
    - ndim == 2，shape 覆盖可见区域（Y≥(h,w)，UV≥(h/2,w/2)，元素单位，
      允许解码器对齐 padding）；
    - itemsize 与契约一致（nv12=1，p016le=2）且两平面一致；
    - pitch ≥ row_bytes（拷贝按行推进，pitch 不足会越界读）；
    - 设备指针非空。

    Raises:
        ValueError: 首个失败的不变量（原因可诊断）。
    """
    itemsize = 1 if contract == "nv12" else 2
    if int(y_info["itemsize"]) != itemsize or int(uv_info["itemsize"]) != itemsize:
        raise ValueError(
            f"plane_itemsize_mismatch:y={y_info['itemsize']},"
            f"uv={uv_info['itemsize']},contract={contract}")
    y_shape = list(y_info["shape"])
    uv_shape = list(uv_info["shape"])
    # PyAV cuda 平面实测 ABI（4090/av18.1）：Y ndim=2 [h, w]；UV ndim=3
    # [h/2, w/2, 2]（交错分量对）。紧凑 2-D UV 也合法（元素=样本对）。
    if len(y_shape) != 2 or len(uv_shape) not in (2, 3):
        raise ValueError(
            f"plane_ndim_invalid:y={y_shape},uv={uv_shape}")
    if len(uv_shape) == 3 and uv_shape[2] != 2:
        raise ValueError(
            f"uv_interleave_depth_invalid:{uv_shape[2]} (expected 2)")
    if y_shape[0] < height or y_shape[1] < width:
        raise ValueError(
            f"y_plane_too_small:shape={y_shape},visible=({width},{height})")
    if uv_shape[0] < height // 2 or uv_shape[1] < width // 2:
        raise ValueError(
            f"uv_plane_too_small:shape={uv_shape},"
            f"visible=({width // 2},{height // 2})")

    y_row_bytes = width * itemsize
    uv_row_bytes = width * itemsize  # 交错半平面：每行 w 个样本对
    y_ptr = int(y_info["data_ptr"]) + int(y_info["byte_offset"])
    uv_ptr = int(uv_info["data_ptr"]) + int(uv_info["byte_offset"])
    if y_ptr == 0 or uv_ptr == 0:
        raise ValueError("plane_device_ptr_null")

    y_plane = CudaResidentPlane(
        component="Y", device_ptr=y_ptr,
        pitch=plane_pitch_bytes(y_info, y_row_bytes),
        row_bytes=y_row_bytes, rows=height,
    )
    uv_plane = CudaResidentPlane(
        component="UV", device_ptr=uv_ptr,
        pitch=plane_pitch_bytes(uv_info, uv_row_bytes),
        row_bytes=uv_row_bytes, rows=height // 2,
    )
    if y_plane.pitch < y_plane.row_bytes or uv_plane.pitch < uv_plane.row_bytes:
        raise ValueError(
            f"plane_pitch_below_row_bytes:y={y_plane.pitch}/"
            f"{y_plane.row_bytes},uv={uv_plane.pitch}/{uv_plane.row_bytes}")
    return y_plane, uv_plane


# ---------------------------------------------------------------------------
# 驻留帧契约
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CudaResidentPlane:
    """单个驻留平面的拷贝几何（CUDA 设备内存，行距可含对齐 padding）。"""
    component: str          # 'Y' | 'UV'
    device_ptr: int         # CUdeviceptr（含 byte_offset）
    pitch: int              # bytes
    row_bytes: int          # 可见行字节
    rows: int


class CudaResidentFrame:
    """解码线程产出的 cuda 驻留帧（GL 导入延迟到渲染线程）。

    契约字段：内存驻留（cuda device，planes[].device_ptr）、像素格式
    （'nv12'|'p016le' 家族，与 planar 契约同词表）、PTS/色彩标记、帧号、
    逐平面几何。持有 av.Frame 引用保活设备缓冲；release() 幂等。

    decode 线程构造；渲染线程只读 + release——两侧均不触 CUDA API
    （拷贝由 CudaGlInteropSession 在 GL 线程执行）。
    """

    __slots__ = (
        "frame_index", "pts", "time_seconds", "width", "height",
        "pixel_format", "bit_depth", "msb_aligned_p010",
        "color_range", "color_matrix", "color_primaries", "color_transfer",
        "planes", "_av_frame", "_released",
    )

    def __init__(
        self,
        frame_index: int,
        pts: Optional[int],
        time_seconds: float,
        width: int,
        height: int,
        pixel_format: str,
        bit_depth: int,
        planes: Sequence[CudaResidentPlane],
        av_frame: Any,
        *,
        msb_aligned_p010: bool = False,
        color_range: str = "limited",
        color_matrix: str = "bt709",
        color_primaries: Optional[str] = None,
        color_transfer: Optional[str] = None,
    ) -> None:
        self.frame_index = int(frame_index)
        self.pts = pts
        self.time_seconds = float(time_seconds)
        self.width = int(width)
        self.height = int(height)
        self.pixel_format = str(pixel_format)
        self.bit_depth = int(bit_depth)
        self.msb_aligned_p010 = bool(msb_aligned_p010)
        self.color_range = str(color_range)
        self.color_matrix = str(color_matrix)
        self.color_primaries = color_primaries
        self.color_transfer = color_transfer
        self.planes = tuple(planes)
        self._av_frame = av_frame
        self._released = False

    def release(self) -> None:
        """幂等释放：置空 av 帧引用（设备缓冲归还解码器池）。"""
        if self._released:
            return
        self._released = True
        self._av_frame = None

    @property
    def released(self) -> bool:
        return self._released


def is_cuda_resident_frame(obj: Any) -> bool:
    return isinstance(obj, CudaResidentFrame)


# ---------------------------------------------------------------------------
# 驻留解码源（WindowsHwPlanarSource 的 cuda 保留帧变体）
# ---------------------------------------------------------------------------

class WindowsCudaResidentSource(WindowsHwPlanarSource):
    """cuda ``is_hw_owned=True`` 驻留解码源（H264/H265 4:2:0 8/10bit）。

    与父类共享全部会话/seek 机器；差异仅三点：

    1. hwaccel 会话固定 ``cuda + is_hw_owned=True``（保留设备帧）；
    2. open() 验证首帧为 ``cuda`` 格式（父类的 nv12/p010le 软件格式门
       对驻留帧必然失败，故覆写），契约格式由 DLPack itemsize 推导；
    3. ``_convert_frame_planar`` 产出 :class:`CudaResidentFrame`（0 CPU
       字节）；非 cuda 格式帧 = 硬解中途失效，显式抛错 fail-closed，
       不做 CPU 兜底伪装（回退由解码管理器重建源完成）。

    SLICE seek 会话（HEVC 的 _seek_gop_session）是独立软解容器，产出
    软件 nv12 帧与驻留契约冲突——本类显式禁用，统一走主会话
    keyframe seek + decode forward（保持帧格式一致，seek 略慢，如实
    登记）。
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self._resident_contract_format: Optional[str] = None
        self._resident_bit_depth: int = 8

    # -- 会话 ---------------------------------------------------------------

    def _reopen_with_hwaccel(self, decoder: HardwareDecoder) -> None:
        """固定 cuda + is_hw_owned=True 的 hwaccel 会话。"""
        import av
        from av.codec.hwaccel import HWAccel, HWDeviceType

        if decoder is None or decoder.value not in ("nvdec", "cuda"):
            raise MediaOpenError(
                f"cuda resident source requires nvdec, got "
                f"{getattr(decoder, 'value', decoder)}",
                source_path=self._path,
            )

        accel = HWAccel(HWDeviceType.cuda, is_hw_owned=True)
        self._frame_iterator = None
        self._decoder_buffer = []
        self._pending_av_frame = None
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                pass
            self._container = None
            self._video_stream = None

        try:
            self._container = av.open(self._path, hwaccel=accel)
        except Exception as e:
            raise MediaOpenError(
                f"cuda resident open failed: {e}",
                source_path=self._path,
            ) from e

        video_streams = self._container.streams.video
        if not video_streams:
            raise MediaOpenError(
                "No video stream on cuda resident reopen",
                source_path=self._path,
            )
        self._video_stream = video_streams[0]
        # 帧线程与 hwaccel 互斥（父类同注），不设 thread_type
        self._is_open = True
        self._hw_decoder = decoder
        self._hw_active = True

    def _verify_hw_output(self) -> None:
        """驻留验证：首帧格式必须为 ``cuda``；契约格式由 itemsize 推导。"""
        assert self._container is not None and self._video_stream is not None
        try:
            first = next(self._container.decode(self._video_stream))
        except StopIteration as e:
            raise MediaOpenError(
                "No decodable frames", source_path=self._path) from e
        except Exception as e:
            raise MediaOpenError(
                f"cuda resident decode probe failed: {e}",
                source_path=self._path,
            ) from e

        fmt = first.format.name if first.format else "unknown"
        if fmt != "cuda":
            metadata = getattr(self, "_metadata", None)
            codec_name = (metadata.codec_name if metadata else "") or ""
            raise MediaOpenError(
                f"{codec_unsupported_reason(codec_name, fmt)}: "
                f"cuda resident session produced {fmt} (expected 'cuda')",
                source_path=self._path,
            )
        # W04 平面格式能力门：只接受双平面 4:2:0 半平面契约（4:2:2/4:4:4
        # 交错格式显式拒绝——错读 UV 比拒绝更糟）。
        if len(first.planes) != 2:
            metadata = getattr(self, "_metadata", None)
            codec_name = (metadata.codec_name if metadata else "") or ""
            raise MediaOpenError(
                f"{codec_unsupported_reason(codec_name, fmt)}: "
                f"cuda resident session produced {len(first.planes)} "
                f"planes (expected 2 for nv12/p016 semi-planar)",
                source_path=self._path,
            )

        probe = parse_dlpack_capsule(first.planes[0].__dlpack__())
        if probe["itemsize"] == 1:
            self._resident_contract_format = "nv12"
            self._resident_bit_depth = 8
        else:
            # P010/P016 家族：MSB 对齐 uint16，按 p016le/16bit 契约
            # （与父类 p010le→p016le 映射同语义，GPU /65535 归一化）
            self._resident_contract_format = "p016le"
            self._resident_bit_depth = 16
        # W04：首帧几何先过拷贝不变量门（itemsize/shape/pitch 不足在此
        # 显式失败，而不是拷贝阶段越界读）。
        try:
            validate_resident_plane_geometry(
                first.width, first.height,
                self._resident_contract_format,
                probe,
                parse_dlpack_capsule(first.planes[1].__dlpack__()),
            )
        except ValueError as e:
            raise MediaOpenError(
                f"cuda resident plane geometry rejected: {e}",
                source_path=self._path,
            ) from e
        self._hw_output_format = fmt
        self._pending_av_frame = first
        self._current_frame = 0

    # -- 读路径 ---------------------------------------------------------------

    @property
    def resident_contract_format(self) -> Optional[str]:
        """驻留会话验证后的契约格式（'nv12' | 'p016le'；None=未验证）。"""
        return self._resident_contract_format

    def read_frame_resident(self, frame_index: int) -> CudaResidentFrame:
        """按帧号读取驻留帧（seek 语义与父类 planar 读完全一致）。"""
        decoded = self.read_frame_planar(frame_index)
        if not is_cuda_resident_frame(decoded):
            # read_frame_planar 的通用路径意外产出软件帧（不该发生——
            # _convert_frame_planar 对非 cuda 帧显式抛错）
            raise RuntimeError(
                f"cuda resident source produced {type(decoded).__name__} "
                f"at frame {frame_index}")
        return decoded

    def _convert_frame_planar(self, frame: Any) -> CudaResidentFrame:
        """驻留转换点：cuda 帧 → CudaResidentFrame（0 CPU 字节）。

        非帧线程 seek 会话（HEVC SLICE）在子类被禁用，任何到达此处的
        软件 nv12/p010le 帧都意味着会话被外部状态污染——fail-closed。
        """
        fmt = frame.format.name if getattr(frame, "format", None) else "unknown"
        if fmt != "cuda":
            raise MediaDecodeError(
                f"cuda resident session produced {fmt} (hw decode lost?)",
                source_path=self._path,
            )

        contract = self._resident_contract_format or "nv12"
        width, height = frame.width, frame.height

        if len(frame.planes) != 2:
            raise MediaDecodeError(
                f"cuda resident frame has {len(frame.planes)} planes "
                f"(expected 2, 420 semi-planar only)",
                source_path=self._path,
            )
        try:
            y_plane, uv_plane = validate_resident_plane_geometry(
                width, height, contract,
                parse_dlpack_capsule(frame.planes[0].__dlpack__()),
                parse_dlpack_capsule(frame.planes[1].__dlpack__()),
            )
        except ValueError as e:
            raise MediaDecodeError(
                f"cuda resident plane geometry rejected: {e}",
                source_path=self._path,
            ) from e
        planes = (y_plane, uv_plane)

        pts = frame.pts
        time_seconds = 0.0
        if pts is not None and self._video_stream is not None:
            if self._video_stream.time_base is not None:
                time_seconds = float(pts * self._video_stream.time_base)

        return CudaResidentFrame(
            frame_index=self._current_frame,
            pts=pts,
            time_seconds=time_seconds,
            width=width,
            height=height,
            pixel_format=contract,
            bit_depth=self._resident_bit_depth,
            planes=planes,
            av_frame=frame,
            msb_aligned_p010=contract == "p016le",
            color_range=self._frame_color_tag(frame, "color_range"),
            color_matrix=self._frame_color_tag(frame, "color_matrix"),
            color_primaries=self._frame_color_tag(frame, "color_primaries")
            or None,
            color_transfer=self._frame_color_tag(frame, "color_transfer")
            or None,
        )

    # -- SLICE seek 会话禁用（见类 docstring）---------------------------------

    def _seek_gop_session(self, frame_index: int, index: Any) -> None:
        raise RuntimeError(
            "cuda_resident_source_disables_slice_seek_session "
            "(software frames would break the resident contract)"
        )


# ---------------------------------------------------------------------------
# CUDA→GL interop 会话（渲染 GL 线程专属）
# ---------------------------------------------------------------------------

class _RawGLTexture:
    """raw OpenGL 纹理的最小持有者（GL_R16/GL_RG16 UNORM 等 moderngl 不
    直接支持的内部格式用）。glo=纹理名；release 幂等。"""

    __slots__ = ("glo", "_released", "_delete")

    def __init__(self, glo: int, delete_fn) -> None:
        self.glo = int(glo)
        self._released = False
        self._delete = delete_fn

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._delete(self.glo)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"raw GL texture delete failed: {exc}")


class _InteropSlot:
    """一对常驻 GL 纹理 + 已注册 CUDA 资源（按 (w,h,fmt) 复用）。

    W02：``last_used`` 驱动空闲 slot 的 LRU 回收；``byte_cost`` 为两平面
    纹理的近似显存占用（Y=w*h*itemsize，UV=(w/2)*(h/2)*2*itemsize，合计
    1.5*w*h*itemsize——GL 内部可能有对齐 padding，此为下限预算）。
    """

    __slots__ = ("key", "textures", "resources", "in_use", "last_used",
                 "byte_cost")

    def __init__(self, key, textures, resources, byte_cost: int) -> None:
        self.key = key
        self.textures = textures
        self.resources = resources
        self.in_use = False
        self.last_used = 0.0
        self.byte_cost = int(byte_cost)


class CudaGlInteropSession:
    """渲染 GL 线程的 CUDA→GL interop 会话（VT IOSurfaceGLBridge 对位）。

    生命周期：renderer GL 线程惰性创建（initialize），关停时 destroy()。
    线程亲和：initialize 记录 owner 线程，跨线程 bind 直接拒绝（驱动对
    上下文误用不总是给出可恢复错误）。

    资源模型：slot 池按 (w, h, pixel_format) 复用，纹理注册一次；帧拷贝
    每平面 map→DtoA→unmap（map/unmap 双向同步，含复用前等待在途回读）。
    slot 租约由返回的 GpuFrameRelease 归还（exactly-once）。

    W02（计划 upgrade_W §3）：

    - **销毁闭环**：destroy() 在 owner 线程以私有上下文注销全部资源、
      释放 GL 纹理后调用 ``cuCtxDestroy``（旧实现从不销毁上下文，长时
      多 renderer 生命周期会累积 CUDA 上下文）；
    - **分阶段创建 + 逆序回滚**：纹理创建/注册逐阶段推进，任一阶段失
      败时逆序撤销已成功阶段（第二平面注册失败必须撤销第一平面注册与
      两张纹理——旧实现泄漏全部）；
    - **池预算**：slot 池受 (max_ring_size 计数, max_pool_bytes 字节) 双
      上限约束；超限时按 LRU 回收**空闲** slot（活跃租约绝不回收），
      无可回收则诚实拒绝（reason 带当前占用）——旧实现 32 个空闲异 key
      slot 即耗尽；
    - **current-context 契约**：initialize/bind/unregister/destroy 各阶
      段核验实际 CUDA current-context：操作前按需切换到会话私有上下文，
      操作后恢复调用者原上下文（同线程不等于同 current-context——PyAV
      cuda 会话可能在线程上留下其它 current；R12/W-I03）。
    """

    #: 默认 slot 池字节预算（两平面纹理显存占用的下限和）。
    DEFAULT_MAX_POOL_BYTES = 1 << 30  # 1 GiB

    def __init__(self, ring_size: int = 8, max_ring_size: int = 32,
                 max_pool_bytes: Optional[int] = None) -> None:
        self._ring_size = max(2, int(ring_size))
        self._max_ring_size = max(self._ring_size, int(max_ring_size))
        self._max_pool_bytes = (int(max_pool_bytes)
                                if max_pool_bytes is not None
                                else self.DEFAULT_MAX_POOL_BYTES)
        self._pool_bytes = 0
        self._slots: list[_InteropSlot] = []
        self._cu = None            # cuda.bindings.driver
        self._ctx = None           # 私有 CUcontext
        self._device = None        # W04：会话对齐的 CUdevice（非隐含 0）
        self._device_ordinal: Optional[int] = None
        self._device_name: str = ""
        self._device_source: str = ""   # 'gl_context' | 'single_cuda_device'
        self._driver_version: Optional[int] = None
        self._initialized = False
        self._destroyed = False
        self._owner_thread_id: Optional[int] = None
        self._bind_count = 0
        self._fail_count = 0
        self._clock = 0.0
        self.last_fallback_reason: str = ""

    # -- 初始化 / 销毁 -------------------------------------------------------

    def initialize(self) -> bool:
        """创建私有 CUDA 上下文（须在 GL 上下文 current 的线程调用）。"""
        if self._initialized:
            return True
        if self._destroyed:
            # 诊断状态保留：销毁后的会话不隐式复活（调用方应重建会话）。
            self.last_fallback_reason = "session_destroyed"
            return False
        try:
            from cuda.bindings import driver as cu
        except Exception as exc:  # noqa: BLE001
            self.last_fallback_reason = f"cuda_bindings_unavailable:{exc}"
            return False
        try:
            err, = cu.cuInit(0)
            if int(err) != 0:
                self.last_fallback_reason = f"cuInit:{err}"
                return False
            # W04（计划 W-I04）：显式对齐当前 GL 上下文的适配器，拒绝
            # 固定 CUdevice(0) 的隐含假设。多 GPU（如 4090+核显显示）下
            # GL 落在非 CUDA 适配器时，interop 注册只会在 bind 期以难归因
            # 的错误失败——这里在会话创建期就查 cuGLGetDevices 对齐：
            # - 关联到 CUDA 设备 → 用该设备（'gl_context'）；
            # - 查询失败（无 current GL 上下文等）且全机恰一台 CUDA 设备
            #   → device 0 与之重合，如实记录后使用；
            # - 多台 CUDA 设备且无法对齐 → fail-closed（不盲选 0）。
            #
            # N22：调用者 current-context 全阶段保存与恢复——初始化在
            # 调用者线程上临时切换到私有上下文，成功/失败/异常所有出口
            # 都恢复调用者原上下文（bind/destroy 已各自有 prev 守卫，
            # initialize 不再留下"current 被会话私有上下文占用"的副作用）。
            caller_ctx: Any = None
            caller_had_ctx = False
            try:
                gerr, gctx = cu.cuCtxGetCurrent()
                if int(gerr) == 0 and gctx:
                    caller_ctx = gctx
                    caller_had_ctx = True
            except Exception:  # noqa: BLE001 —— 假驱动/绑定差异容错
                caller_had_ctx = False

            def _restore_caller_ctx() -> None:
                if caller_had_ctx:
                    try:
                        cu.cuCtxSetCurrent(caller_ctx)
                    except Exception:  # noqa: BLE001 —— 恢复尽力而为
                        pass

            device, device_reason = self._select_aligned_device(cu)
            if device is None:
                self.last_fallback_reason = device_reason
                _restore_caller_ctx()
                return False
            err, ctx = cu.cuCtxCreate(None, 0, device)
            if int(err) != 0:
                self.last_fallback_reason = f"cuCtxCreate:{err}"
                _restore_caller_ctx()
                return False
            err, = cu.cuCtxSetCurrent(ctx)
            if int(err) != 0:
                self.last_fallback_reason = f"cuCtxSetCurrent:{err}"
                # 创建成功但置 current 失败：显式销毁，不留悬空上下文
                try:
                    cu.cuCtxDestroy(ctx)
                except Exception:  # noqa: BLE001
                    pass
                _restore_caller_ctx()
                return False
            # W02：核验实际 current-context 即所建上下文（驱动契约以
            # GetCurrent 为准，不以 SetCurrent 返回码推断）
            err, current = cu.cuCtxGetCurrent()
            if int(err) != 0 or not _cu_ctx_same(current, ctx):
                self.last_fallback_reason = (
                    f"cuCtxGetCurrent_verify:{err}")
                try:
                    cu.cuCtxDestroy(ctx)
                except Exception:  # noqa: BLE001
                    pass
                _restore_caller_ctx()
                return False
        except Exception as exc:  # noqa: BLE001
            self.last_fallback_reason = f"cuda_init_error:{exc}"
            return False
        # W04：能力身份落档（设备/驱动随 stats 可见；能力缓存的失效
        # 条件即设备/驱动身份变化）
        self._device = device
        try:
            self._device_ordinal = int(device)
        except (TypeError, ValueError):
            self._device_ordinal = 0
        try:
            err, name_bytes = cu.cuDeviceGetName(128, device)
            if int(err) == 0 and name_bytes:
                self._device_name = name_bytes.split(b"\x00")[0].decode(
                    "ascii", "replace")
        except Exception:  # noqa: BLE001 —— 名字仅诊断用
            self._device_name = ""
        try:
            err, drv_ver = cu.cuDriverGetVersion()
            self._driver_version = int(drv_ver) if int(err) == 0 else None
        except Exception:  # noqa: BLE001
            self._driver_version = None
        self._cu = cu
        self._ctx = ctx
        self._owner_thread_id = threading.get_ident()
        self._initialized = True
        # N22：成功路径同样恢复调用者上下文（会话私有上下文由
        # bind/destroy 的 prev 守卫按需切换）。
        if caller_had_ctx:
            try:
                cu.cuCtxSetCurrent(caller_ctx)
            except Exception:  # noqa: BLE001 —— 恢复尽力而为
                pass
        return True

    def _select_aligned_device(self, cu) -> tuple[Optional[Any], str]:
        """选择与当前 GL 上下文对齐的 CUDA 设备（W04）。

        Returns:
            (CUdevice | None, reason)：None 时 reason 说明 fail-closed 原因
            （或 device_source 记录用原因）。
        """
        gl_err: Any = None
        gl_count = 0
        gl_devices: Any = None
        try:
            gl_err, gl_count, gl_devices = cu.cuGLGetDevices(
                8, cu.CUGLDeviceList.CU_GL_DEVICE_LIST_ALL)
        except Exception as exc:  # noqa: BLE001 —— 绑定层差异容错
            gl_err = exc
        # N22：API 返回码/异常先安全分类（旧代码 `int(gl_err)` 在异常
        # 对象上直接 TypeError 外泄，设备数兜底不可达）；"异常"不等于
        # "假支持"——分类后按实际设备证据走合法回退。
        try:
            gl_err_code = int(gl_err)
        except (TypeError, ValueError):
            gl_err_code = -1
        if gl_err_code == 0 and gl_count and gl_devices:
            self._device_source = 'gl_context'
            return gl_devices[0], ''
        # CUDA_ERROR_NO_DEVICE(100)：有 current GL 上下文但其适配器非
        # CUDA 设备（如 GL 落在核显）——interop 无从建立，fail-closed。
        if gl_err_code == 100:
            return None, f"no_cuda_device_for_gl_context:{gl_err}"
        err, count = cu.cuDeviceGetCount()
        if int(err) != 0:
            return None, f"cuDeviceGetCount:{err}"
        if count == 1:
            # 查询失败（典型为无 current GL 上下文）且全机唯一 CUDA 设备：
            # device 0 与之确定重合
            self._device_source = 'single_cuda_device'
            return cu.CUdevice(0), f'gl_query_failed:{gl_err}'
        return None, (
            f"cannot_align_gl_adapter:{gl_err}(cuda_devices={count})")

    @contextmanager
    def _private_context(self) -> Iterator[None]:
        """私有上下文守卫：进入前按需切换，退出时恢复调用者原上下文。

        仅 owner 线程内调用（bind/destroy 已有亲和门）。current-context
        与线程无必然绑定（PyAV cuda 会话可在同线程留下别的 current），
        故每次资源操作都核验-切换-恢复，不靠 thread id 推断。
        """
        cu, ctx = self._cu, self._ctx
        if cu is None or ctx is None:
            yield
            return
        prev = None
        had_prev = False
        switched = False
        try:
            err, prev = cu.cuCtxGetCurrent()
            if int(err) == 0:
                had_prev = not _cu_ctx_is_null(prev)
                if not had_prev or not _cu_ctx_same(prev, ctx):
                    err2, = cu.cuCtxSetCurrent(ctx)
                    if int(err2) != 0:
                        raise RuntimeError(f"cuCtxSetCurrent(guard):{err2}")
                    switched = True
            else:
                err2, = cu.cuCtxSetCurrent(ctx)
                if int(err2) != 0:
                    raise RuntimeError(f"cuCtxSetCurrent(guard):{err2}")
                switched = True
            yield
        finally:
            if switched:
                try:
                    if had_prev:
                        cu.cuCtxSetCurrent(prev)
                    else:
                        # 调用者原本无 current：弹出，不留我们的私有上下文
                        cu.cuCtxSetCurrent(_null_ctx(cu))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "cuda interop context restore failed: %s", exc)

    def destroy(self) -> None:
        """幂等销毁：注销资源、释放纹理、销毁私有上下文（GL 线程调用）。

        W02：补齐 cuCtxDestroy；注销/销毁在私有上下文守卫内执行，调用
        者 current-context 恢复。GL 纹理释放即使 CUDA 侧失败也尽力完成。
        """
        slots = self._slots
        ctx, cu = self._ctx, self._cu
        self._slots = []
        self._pool_bytes = 0
        self._initialized = False
        self._ctx = None
        self._cu = None
        self._owner_thread_id = None
        self._destroyed = True
        if cu is None or ctx is None:
            self._release_slot_textures_quiet(slots)
            return
        try:
            with self._private_context_with(cu, ctx):
                for slot in slots:
                    for res in slot.resources:
                        try:
                            err, = cu.cuGraphicsUnregisterResource(res)
                            if int(err) != 0:
                                logger.debug(
                                    "cuda interop unregister rc=%s", err)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                f"cuda interop unregister failed: {exc}")
                # 纹理释放与上下文销毁仍在守卫内（纹理不依赖 CUDA，但
                # 保持单出口路径）
                self._release_slot_textures_quiet(slots)
                try:
                    err, = cu.cuCtxDestroy(ctx)
                    if int(err) != 0:
                        logger.debug("cuda interop cuCtxDestroy rc=%s", err)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"cuda interop cuCtxDestroy failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"cuda interop destroy error: {exc}")
            self._release_slot_textures_quiet(slots)

    @staticmethod
    def _release_slot_textures_quiet(slots) -> None:
        for slot in slots:
            for tex in slot.textures:
                try:
                    tex.release()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"cuda interop texture release failed: {exc}")

    @contextmanager
    def _private_context_with(self, cu, ctx) -> Iterator[None]:
        """destroy 专用：self._cu/_ctx 已清空，用显式参数执行同款守卫。"""
        prev = None
        had_prev = False
        switched = False
        try:
            err, prev = cu.cuCtxGetCurrent()
            if int(err) == 0:
                had_prev = not _cu_ctx_is_null(prev)
                if not had_prev or not _cu_ctx_same(prev, ctx):
                    err2, = cu.cuCtxSetCurrent(ctx)
                    if int(err2) != 0:
                        raise RuntimeError(f"cuCtxSetCurrent(destroy):{err2}")
                    switched = True
            else:
                err2, = cu.cuCtxSetCurrent(ctx)
                if int(err2) != 0:
                    raise RuntimeError(f"cuCtxSetCurrent(destroy):{err2}")
                switched = True
            yield
        finally:
            if switched:
                try:
                    if had_prev:
                        cu.cuCtxSetCurrent(prev)
                    else:
                        cu.cuCtxSetCurrent(_null_ctx(cu))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "cuda interop context restore failed: %s", exc)

    # -- 纹理创建 -------------------------------------------------------------

    def _create_plane_textures(self, width: int, height: int,
                               pixel_format: str):
        """NV12 → (R8, RG8)（moderngl）；p016le → (R16, RG16) UNORM（raw GL，
        moderngl 无 normalized u16 dtype；CUDA array 格式与 R16UI 相同）。

        W02：分阶段创建——第二平面失败时释放已创建的第一平面（旧实现
        直接抛出，首个纹理泄漏）。
        """
        import moderngl

        ctx = moderngl.get_context()
        if pixel_format == "nv12":
            y = None
            try:
                y = ctx.texture((width, height), components=1, dtype="f1")
                uv = ctx.texture((width // 2, height // 2), components=2,
                                 dtype="f1")
            except Exception:
                if y is not None:
                    try:
                        y.release()
                    except Exception:  # noqa: BLE001
                        pass
                raise
            return y, uv

        from OpenGL.GL import (
            GL_TEXTURE_2D, GL_R16, GL_RG16, GL_RED, GL_RG,
            GL_UNSIGNED_SHORT, GL_NEAREST, GL_TEXTURE_MIN_FILTER,
            GL_TEXTURE_MAG_FILTER, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T,
            GL_CLAMP_TO_EDGE, glGenTextures, glBindTexture,
            glTexParameteri, glTexImage2D, glGetError, glDeleteTextures,
        )

        def _make(internal, fmt, w, h):
            tex_id = glGenTextures(1)
            glBindTexture(GL_TEXTURE_2D, tex_id)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            glTexImage2D(
                GL_TEXTURE_2D, 0, internal, w, h, 0, fmt,
                GL_UNSIGNED_SHORT, None)
            glBindTexture(GL_TEXTURE_2D, 0)
            if glGetError() != 0:
                glDeleteTextures(1, [tex_id])
                raise RuntimeError(
                    f"raw GL texture create failed (internal={internal})")
            return _RawGLTexture(
                tex_id, lambda name: glDeleteTextures(1, [name]))

        y = None
        try:
            y = _make(GL_R16, GL_RED, width, height)
            uv = _make(GL_RG16, GL_RG, width // 2, height // 2)
        except Exception:
            if y is not None:
                try:
                    y.release()
                except Exception:  # noqa: BLE001
                    pass
            raise
        return y, uv

    def _plane_meta(self, pixel_format: str) -> list[dict]:
        """GpuFramePlane 元数据（与外部平面消费 shader 的归一化语义对齐）。"""
        if pixel_format == "nv12":
            return [
                {"component": "Y", "internal_format": "GL_R8",
                 "pixel_format": "NV12_Y", "pixel_type": "GL_UNSIGNED_BYTE"},
                {"component": "UV", "internal_format": "GL_RG8",
                 "pixel_format": "NV12_UV", "pixel_type": "GL_UNSIGNED_BYTE"},
            ]
        return [
            {"component": "Y", "internal_format": "GL_R16",
             "pixel_format": "P016_Y", "pixel_type": "GL_UNSIGNED_SHORT"},
            {"component": "UV", "internal_format": "GL_RG16",
             "pixel_format": "P016_UV", "pixel_type": "GL_UNSIGNED_SHORT"},
        ]

    # -- 帧绑定 ---------------------------------------------------------------

    @staticmethod
    def _slot_byte_cost(key) -> int:
        """两平面纹理近似显存（下限预算）：Y=w*h*itemsize + UV=(w*h/2)*itemsize。"""
        w, h, fmt = key[0], key[1], key[2]
        itemsize = 2 if fmt != "nv12" else 1
        return int(1.5 * w * h * itemsize)

    def _evict_lru_idle_slots_for(self, needed_bytes: int) -> bool:
        """按 LRU 回收空闲 slot 以同时满足 (计数, 字节) 双预算。

        活跃租约绝不回收。Returns: True 表示预算已满足（或无可回收且
        当前本就满足）；False 表示无可回收空间。
        """
        while (len(self._slots) >= self._max_ring_size
               or self._pool_bytes + needed_bytes > self._max_pool_bytes):
            idle = [s for s in self._slots if not s.in_use]
            if not idle:
                return (len(self._slots) < self._max_ring_size
                        and self._pool_bytes + needed_bytes
                        <= self._max_pool_bytes)
            victim = min(idle, key=lambda s: s.last_used)
            try:
                with self._private_context():
                    for res in victim.resources:
                        try:
                            err, = self._cu.cuGraphicsUnregisterResource(res)
                            if int(err) != 0:
                                logger.debug(
                                    "cuda interop evict unregister rc=%s",
                                    err)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                f"cuda interop evict unregister: {exc}")
                self._release_slot_textures_quiet([victim])
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"cuda interop evict failed: {exc}")
            self._slots.remove(victim)
            self._pool_bytes = max(0, self._pool_bytes - victim.byte_cost)
        return True

    def _destroy_slot(self, slot: _InteropSlot) -> None:
        """注销并释放单个 slot 的全部资源（私有上下文守卫内）。"""
        with self._private_context():
            for res in slot.resources:
                try:
                    err, = self._cu.cuGraphicsUnregisterResource(res)
                    if int(err) != 0:
                        logger.debug(
                            "cuda interop unregister rc=%s", err)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"cuda interop unregister failed: {exc}")
        self._release_slot_textures_quiet([slot])
        if slot in self._slots:
            self._slots.remove(slot)
        self._pool_bytes = max(0, self._pool_bytes - slot.byte_cost)

    def _acquire_slot(self, key):
        cost = self._slot_byte_cost(key)
        for slot in self._slots:
            if slot.key == key and not slot.in_use:
                slot.in_use = True
                self._clock += 1
                slot.last_used = self._clock
                return slot
        # 未命中空闲：计数或字节预算超限时先按 LRU 回收空闲 slot
        if (len(self._slots) >= self._max_ring_size
                or self._pool_bytes + cost > self._max_pool_bytes):
            if not self._evict_lru_idle_slots_for(cost):
                self.last_fallback_reason = (
                    f"slot_pool_exhausted:count={len(self._slots)}"
                    f"/{self._max_ring_size},bytes={self._pool_bytes}"
                    f"/{self._max_pool_bytes}")
                return None
        # 分阶段创建 + 逆序回滚（W02）：纹理对 → 注册 Y → 注册 UV；
        # 任一阶段失败撤销已成功阶段，不留半初始化资源
        created_tex = None
        registered: list = []
        try:
            created_tex = self._create_plane_textures(*key[:2], key[2])
            registered.append(self._register_texture(created_tex[0]))
            registered.append(self._register_texture(created_tex[1]))
        except Exception as exc:  # noqa: BLE001
            self.last_fallback_reason = f"slot_create_failed:{exc}"
            with self._private_context():
                for res in registered:
                    try:
                        self._cu.cuGraphicsUnregisterResource(res)
                    except Exception:  # noqa: BLE001
                        pass
            if created_tex is not None:
                self._release_slot_textures_quiet(
                    [_InteropSlot(key, created_tex, (), 0)])
            return None
        slot = _InteropSlot(key, created_tex, tuple(registered), cost)
        slot.in_use = True
        self._clock += 1
        slot.last_used = self._clock
        self._slots.append(slot)
        self._pool_bytes += cost
        return slot

    def _register_texture(self, tex):
        GL_TEXTURE_2D = 0x0DE1
        err, res = self._cu.cuGraphicsGLRegisterImage(
            tex.glo, GL_TEXTURE_2D, 0)
        if int(err) != 0:
            raise RuntimeError(f"cuGraphicsGLRegisterImage:{err}")
        return res

    def _copy_plane(self, res, plane: CudaResidentPlane) -> None:
        cu = self._cu
        err, = cu.cuGraphicsMapResources(1, res, 0)
        if int(err) != 0:
            raise RuntimeError(f"cuGraphicsMapResources:{err}")
        try:
            err, arr = cu.cuGraphicsSubResourceGetMappedArray(res, 0, 0)
            if int(err) != 0:
                raise RuntimeError(f"cuGraphicsSubResourceGetMappedArray:{err}")
            m = cu.CUDA_MEMCPY2D()
            m.srcMemoryType = cu.CUmemorytype.CU_MEMORYTYPE_DEVICE
            m.srcDevice = plane.device_ptr
            m.srcPitch = plane.pitch
            m.dstMemoryType = cu.CUmemorytype.CU_MEMORYTYPE_ARRAY
            m.dstArray = arr
            m.WidthInBytes = plane.row_bytes
            m.Height = plane.rows
            err, = cu.cuMemcpy2D(m)
            if int(err) != 0:
                raise RuntimeError(f"cuMemcpy2D:{err}")
            # D2A 拷贝对主机异步，unmap 不保证等待在途拷贝（L3 实测：
            # 逐帧导入下偶发左缘未写入纹素被采样）。显式同步上下文后再
            # unmap，拷贝完成对后续 GL 采样可见。每帧一次，成本 ~0.1ms
            # 量级（会话统计 xfer 段内）。
            err, = cu.cuCtxSynchronize()
            if int(err) != 0:
                raise RuntimeError(f"cuCtxSynchronize:{err}")
        finally:
            try:
                cu.cuGraphicsUnmapResources(1, res, 0)
            except Exception:  # noqa: BLE001
                pass

    def bind_resident_frame(
        self,
        frame: CudaResidentFrame,
        decode_path: str = "",
    ) -> Optional[tuple[Sequence[GpuFramePlane], GpuFrameRelease]]:
        """驻留帧 → 常驻 GL 纹理对 + slot 租约（GL 线程调用）。

        Returns:
            (planes, release) 或 None（reason 见 last_fallback_reason）。
            成功后源 frame 的设备缓冲已拷贝完成，调用方应立即 release()
            源帧；GL 纹理的生命周期由返回的租约管理。
        """
        if not self._initialized:
            self.last_fallback_reason = (
                "session_destroyed" if self._destroyed
                else "session_not_initialized")
            self._fail_count += 1
            return None
        if self._owner_thread_id != threading.get_ident():
            self.last_fallback_reason = "session_thread_mismatch"
            self._fail_count += 1
            logger.warning(
                "CudaGlInteropSession: refusing bind from a thread other "
                "than the GL owner thread")
            return None
        if not is_cuda_resident_frame(frame) or frame.released:
            self.last_fallback_reason = "invalid_or_released_frame"
            self._fail_count += 1
            return None

        key = (frame.width, frame.height, frame.pixel_format)
        try:
            # W02：slot 获取（含纹理创建/注册/回收）与逐平面拷贝都在私有
            # 上下文守卫内，调用者 current-context 退出时恢复
            with self._private_context():
                # W04 设备身份门：拷贝前核验平面指针确为本会话设备上的
                # 设备内存（解码器契约漂移/跨设备指针在此显式拒绝，触发
                # W03 按素材降级，而不是越界读）
                pointer_reason = self._verify_plane_pointers(frame.planes)
                if pointer_reason:
                    self.last_fallback_reason = pointer_reason
                    self._fail_count += 1
                    return None
                slot = self._acquire_slot(key)
                if slot is None:
                    self._fail_count += 1
                    return None
                ok = True
                try:
                    for pi, plane in enumerate(frame.planes):
                        self._copy_plane(slot.resources[pi], plane)
                except Exception as exc:  # noqa: BLE001
                    self.last_fallback_reason = f"copy_failed:{exc}"
                    ok = False
                if not ok:
                    slot.in_use = False
                    self._fail_count += 1
                    return None

            meta = self._plane_meta(frame.pixel_format)
            planes = []
            for pi, tex in enumerate(slot.textures):
                w = frame.width if pi == 0 else frame.width // 2
                h = frame.height if pi == 0 else frame.height // 2
                planes.append(GpuFramePlane(
                    component=meta[pi]["component"],
                    api="opengl",
                    texture_id=tex.glo,
                    target="2d",
                    width=w,
                    height=h,
                    internal_format=meta[pi]["internal_format"],
                    pixel_format=meta[pi]["pixel_format"],
                    pixel_type=meta[pi]["pixel_type"],
                    texture_unit_hint=pi,
                ))

            def _release_slot(slot: _InteropSlot = slot) -> None:
                slot.in_use = False

            self._bind_count += 1
            return tuple(planes), GpuFrameRelease(
                release_callback=_release_slot,
                owner_backend="cuda",
                destructor_policy="warn_only",
            )
        except Exception as exc:  # noqa: BLE001
            self.last_fallback_reason = f"bind_error:{exc}"
            self._fail_count += 1
            logger.error(f"CudaGlInteropSession: bind failed: {exc}")
            return None

    def _verify_plane_pointers(self, planes) -> str:
        """W04：逐平面核验设备内存类型与设备序号（须在私有上下文内）。

        Returns:
            空串 = 通过；非空 = fail-closed 原因。
        """
        cu = self._cu
        if cu is None:
            return "session_not_initialized"
        try:
            for plane in planes:
                ptr = cu.CUdeviceptr(int(plane.device_ptr))
                err, mem_type = cu.cuPointerGetAttribute(
                    cu.CUpointer_attribute.CU_POINTER_ATTRIBUTE_MEMORY_TYPE,
                    ptr)
                if int(err) != 0 or int(mem_type) != int(
                        cu.CUmemorytype.CU_MEMORYTYPE_DEVICE):
                    return (f"plane_not_device_memory:"
                            f"{plane.component},err={err},type={mem_type}")
                err, ordinal = cu.cuPointerGetAttribute(
                    cu.CUpointer_attribute.CU_POINTER_ATTRIBUTE_DEVICE_ORDINAL,
                    ptr)
                if int(err) != 0 or int(ordinal) != int(self._device_ordinal):
                    return (f"plane_device_mismatch:"
                            f"{plane.component},frame_dev={ordinal},"
                            f"session_dev={self._device_ordinal}")
        except Exception as exc:  # noqa: BLE001 —— 指针属性不可查 = 拒绝
            return f"plane_pointer_probe_failed:{exc}"
        return ""

    # -- 状态 ---------------------------------------------------------------

    @property
    def stats(self) -> dict:
        ctx_ptr = _cu_ctx_ptr(self._ctx)
        return {
            "initialized": self._initialized,
            "destroyed": self._destroyed,
            "slots": len(self._slots),
            "slots_in_use": sum(1 for s in self._slots if s.in_use),
            "max_ring_size": self._max_ring_size,
            "pool_bytes": self._pool_bytes,
            "max_pool_bytes": self._max_pool_bytes,
            "ctx": (f"0x{ctx_ptr:X}" if ctx_ptr else ""),
            "bind_count": self._bind_count,
            "fail_count": self._fail_count,
            # W04 能力身份：设备/驱动随会话可见（缓存失效条件 = 身份变化）
            "device_ordinal": self._device_ordinal,
            "device_name": self._device_name,
            "device_source": self._device_source,
            "driver_version": self._driver_version,
        }
