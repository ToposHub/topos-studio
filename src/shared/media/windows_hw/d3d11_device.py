"""原生 D3D11 device 生命周期（计划 2026-09-15 §6 步骤 1）。

职责：以 ctypes 走查 d3d11.dll / dxgi.dll，创建硬件 D3D11 device 并登记
adapter LUID——这是解码、渲染、编码必须共享的 GPU 身份（计划 §3.3：
"设备以 DXGI LUID 为唯一身份。解码、渲染、编码不在同一 adapter 时，
默认回退当前已验证的下载路径并记录 adapter_mismatch"）。

vtable 槽位按 SDK d3d11.h 钉死，并在每次创建后做健全性检查
（GetFeatureLevel 读回值必须与 D3D11CreateDevice 返回值一致），
不匹配立即 fail-closed，防 vtable 错位崩溃。槽位由真机探针验证
（scripts/probe_wgl_shared_texture_roundtrip.py，AMD RX 6800 实测
CreateTexture2D=[5]、GetFeatureLevel=[37]）。

线程约束：本模块只负责 device 创建与释放；ModernGL/Qt 上下文互操作
不属于本层。release 只能由创建线程或明确持有所有权的线程调用。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from .gpu_capability import check_adapter_match, parse_adapter_desc
from .reasons import FallbackReason, param

logger = logging.getLogger(__name__)

__all__ = [
    "DXGI_FORMAT_NV12",
    "DXGI_FORMAT_P010",
    "DXGI_FORMAT_B8G8R8A8_UNORM",
    "VTABLE_SLOT_QUERY_INTERFACE",
    "VTABLE_SLOT_RELEASE",
    "VTABLE_SLOT_CREATE_TEXTURE2D",
    "VTABLE_SLOT_GET_IMMEDIATE_CONTEXT",
    "VTABLE_SLOT_GET_FEATURE_LEVEL",
    "D3D11DeviceIdentity",
    "D3D11Device",
    "create_d3d11_device",
    "check_device_adapter_match",
    "release_com_object",
    "vtable_fn",
]

# --- DXGI 格式常量（SDK dxgiformat.h 钉死码值） --------------------------
# AUD-36203/36206 根因修正：历史错值 65/100 实为 A8_UNORM/AYUV；
# 视频格式段真值 NV12=103、P010=104（Microsoft Learn DXGI_FORMAT）。
DXGI_FORMAT_NV12 = 103
DXGI_FORMAT_P010 = 104
DXGI_FORMAT_B8G8R8A8_UNORM = 87

# --- ID3D11Device vtable 槽位（IUnknown 3 槽 + SDK d3d11.h 实序） --------
# 真机探针（scripts/probe_wgl_shared_texture_roundtrip.py，AMD RX 6800）实测
# 覆盖 CreateTexture2D=[5] 与 GetFeatureLevel=[37]；其余槽位按 SDK 头文件
# 方法声明序钉死（见下方逐槽注释）。
VTABLE_SLOT_QUERY_INTERFACE = 0
VTABLE_SLOT_ADD_REF = 1
VTABLE_SLOT_RELEASE = 2
VTABLE_SLOT_CREATE_TEXTURE2D = 5
# AUD-36000：GetImmediateContext 实序为 40（SDK d3d11.h：27 是
# CreateDeferredContext——CheckFormatSupport=[29] 与 GetFeatureLevel=[37]
# 的真机实测值同时印证该序）。旧值 27 会调到 CreateDeferredContext，
# 注释声称的"真机同源验证"并未覆盖此槽。
VTABLE_SLOT_GET_IMMEDIATE_CONTEXT = 40
VTABLE_SLOT_GET_FEATURE_LEVEL = 37

# IDXGIDevice::GetAdapter = IUnknown 3 + SetPrivateData/SetPrivateDataInterface
# /GetPrivateData/GetParent 后第 5 个方法。
VTABLE_SLOT_IDXGIDEVICE_GET_ADAPTER = 7
# IDXGIAdapter::GetDesc（W1-01 真机验证槽位，gpu_capability 同源）。
VTABLE_SLOT_IDXGIADAPTER_GET_DESC = 8
# ID3D11Device::CheckFormatSupport。SDK d3d11.h 实序（括号标注真机验证）：
# CreateDeferredContext=[27]、OpenSharedResource=[28]、
# CheckFormatSupport=[29]（真机验证）、GetFeatureLevel=[37]（真机验证）、
# GetImmediateContext=[40]（AUD-36000：SDK 序钉死，真机探针未覆盖）。
VTABLE_SLOT_CHECK_FORMAT_SUPPORT = 29

# D3D_FEATURE_LEVEL 阶梯（SDK d3d11.h）。pFeatureLevels=NULL 时运行时最高只给
# 11.0（RX 6800 实测 fl=0xB000，P010 能力被锁）；必须显式请求 11.1+/12.x。
_D3D_FEATURE_LEVEL_LADDER = (0xC100, 0xC000, 0xB100, 0xB000)

_IID_IDXGIDevice_GUID = uuid.UUID("54ec77fa-1377-44e6-8c32-88fd5f44c84c")


class _GUID(ctypes.Structure):
    """Windows GUID 的内存布局（DWORD + WORD + WORD + BYTE[8]）。

    注意：必须以 Structure + byref 传给 QueryInterface；bytes/c_void_p
    形式在真机上返回 E_POINTER（RX 6800 实测）。
    """

    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


_IID_IDXGIDevice = _GUID(
    _IID_IDXGIDevice_GUID.time_low,
    _IID_IDXGIDevice_GUID.time_mid,
    _IID_IDXGIDevice_GUID.time_hi_version,
    (ctypes.c_ubyte * 8)(*_IID_IDXGIDevice_GUID.bytes[8:]),
)

_D3D_DRIVER_TYPE_HARDWARE = 1
_D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
_D3D11_SDK_VERSION = 7

# DXGI_ADAPTER_DESC 全长（UTF-16 描述 128 + 数据 + LUID 尾 8 字节）。
_DXGI_ADAPTER_DESC_SIZE = 304

# ``WINFUNCTYPE`` is only exposed by ctypes on Windows.  Keep this module
# importable on macOS/Linux so the platform capability gate can fail closed at
# call time instead of breaking unrelated Metal/OpenGL imports during startup.
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_RELEASE_PROTO = _WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


def vtable_fn(ptr: int, slot: int, proto: Any) -> Any:
    """取 COM 对象 vtable 第 slot 槽并按 proto 转型（真机验证走查层）。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    if not vtbl:
        raise ValueError("COM 对象指针无效")
    fn = ctypes.cast(
        vtbl + slot * ctypes.sizeof(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ).contents.value
    if not fn:
        raise ValueError(f"vtable slot {slot} 为空")
    return ctypes.cast(fn, proto)


def release_com_object(ptr: int) -> None:
    """IUnknown::Release（引用计数；不置空外部指针，调用方自行失忆）。"""
    vtable_fn(ptr, VTABLE_SLOT_RELEASE, _RELEASE_PROTO)(ptr)


@dataclass(frozen=True)
class D3D11DeviceIdentity:
    """一次成功创建的 D3D11 device 诊断快照（不含指针）。"""

    feature_level: int
    adapter_luid: str
    adapter_description: str
    vendor_name: str
    bgra_support: bool

    def to_dict(self) -> dict:
        return {
            "feature_level": hex(self.feature_level),
            "adapter_luid": self.adapter_luid,
            "adapter_description": self.adapter_description,
            "vendor_name": self.vendor_name,
            "bgra_support": self.bgra_support,
        }


class D3D11Device:
    """持有 D3D11 device / immediate context 引用计数的生命周期对象。

    硬规则：
    - release 恰好一次；重复 release 抛 ValueError；
    - release 后所有指针访问抛 RuntimeError（fail-closed）；
    - release 先释放 immediate context 再释放 device（context 持有
      device 引用，顺序颠倒会泄漏 device）；
    - D3D11 immediate context 的线程亲和语义由调用方遵守；本层只保证
      生命周期（引用计数持有 context，随 device 一起释放）。
    """

    def __init__(
        self,
        device_ptr: int,
        context_ptr: int,
        identity: D3D11DeviceIdentity,
    ) -> None:
        if not device_ptr or not context_ptr:
            raise ValueError("D3D11Device 不接受空指针（fail-closed）")
        self._device_ptr = device_ptr
        self._context_ptr = context_ptr
        self._identity = identity
        self._lock = threading.Lock()
        self._released = False

    @property
    def device_ptr(self) -> int:
        self._check_alive()
        return self._device_ptr

    @property
    def context_ptr(self) -> int:
        self._check_alive()
        return self._context_ptr

    @property
    def identity(self) -> D3D11DeviceIdentity:
        return self._identity

    @property
    def adapter_luid(self) -> str:
        return self._identity.adapter_luid

    @property
    def is_released(self) -> bool:
        return self._released

    def _check_alive(self) -> None:
        if self._released:
            raise RuntimeError("D3D11Device 已释放")

    def release(self) -> None:
        with self._lock:
            if self._released:
                raise ValueError("D3D11Device 重复 release")
            self._released = True
            device = self._device_ptr
            context = self._context_ptr
            self._device_ptr = 0
            self._context_ptr = 0
        # immediate context 持有 device 的引用：必须先 Release context，
        # 否则 context 引用泄漏且 device 因引用计数不为零而无法销毁。
        if context:
            release_com_object(context)
        release_com_object(device)

    def __enter__(self) -> "D3D11Device":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if not self._released:
            self.release()


def create_d3d11_device(
    require_bgra: bool = True,
) -> Tuple[Optional[D3D11Device], str]:
    """创建默认硬件适配器上的 D3D11 device。

    返回 ``(device, "")`` 或 ``(None, reason)``；reason 来自
    :mod:`.reasons` 词表（fail-closed，绝不返回半初始化对象）。
    """
    if sys.platform != "win32":
        return None, FallbackReason.NOT_WINDOWS.value
    try:
        d3d11 = ctypes.CDLL("d3d11.dll")
    except OSError as exc:
        return None, param(FallbackReason.GPU_PROBE_FAILED, f"d3d11_dll:{exc}")

    device = ctypes.c_void_p(0)
    context = ctypes.c_void_p(0)
    feature_level = ctypes.c_int(0)
    flags = _D3D11_CREATE_DEVICE_BGRA_SUPPORT if require_bgra else 0
    levels = (ctypes.c_int * len(_D3D_FEATURE_LEVEL_LADDER))(
        *_D3D_FEATURE_LEVEL_LADDER,
    )
    try:
        hr = d3d11.D3D11CreateDevice(
            None, _D3D_DRIVER_TYPE_HARDWARE, None, flags, levels,
            len(_D3D_FEATURE_LEVEL_LADDER), _D3D11_SDK_VERSION,
            ctypes.byref(device), ctypes.byref(feature_level),
            ctypes.byref(context),
        )
    except Exception as exc:
        return None, param(FallbackReason.GPU_PROBE_FAILED, f"exception:{exc}")
    if hr != 0 or not device.value:
        return None, param(
            FallbackReason.GPU_PROBE_FAILED,
            f"D3D11CreateDevice hr=0x{hr & 0xFFFFFFFF:08X}",
        )
    if not context.value:
        release_com_object(device.value)
        return None, param(FallbackReason.GPU_PROBE_FAILED, "no_immediate_context")

    # vtable 健全性：GetFeatureLevel 读回必须等于创建返回值（防错位）。
    proto_u = _WINFUNCTYPE(ctypes.c_uint, ctypes.c_void_p)
    got_fl = vtable_fn(
        device.value, VTABLE_SLOT_GET_FEATURE_LEVEL, proto_u,
    )(device.value)
    if got_fl != feature_level.value:
        release_com_object(context.value)
        release_com_object(device.value)
        return None, param(
            FallbackReason.GPU_PROBE_FAILED,
            f"vtable_sanity:slot{VTABLE_SLOT_GET_FEATURE_LEVEL}="
            f"0x{got_fl:X}!=0x{feature_level.value:X}",
        )

    luid, description, vendor = _device_adapter_identity(device.value)
    identity = D3D11DeviceIdentity(
        feature_level=feature_level.value,
        adapter_luid=luid,
        adapter_description=description,
        vendor_name=vendor,
        bgra_support=require_bgra,
    )
    logger.info(
        "D3D11 device 创建成功: %s LUID=%s fl=0x%X",
        description or "?", luid or "?", feature_level.value,
    )
    return D3D11Device(device.value, context.value, identity), ""


def check_device_adapter_match(
    render_adapter_luid: str,
    device: D3D11Device,
) -> Tuple[bool, str]:
    """渲染侧 LUID 与 device 实际 adapter 的同卡校验（计划 §3.3）。"""
    return check_adapter_match(render_adapter_luid, device.adapter_luid)


def check_format_support(
    device: D3D11Device,
    dxgi_format: int,
) -> Tuple[bool, int, str]:
    """CheckFormatSupport 预检：格式在真机上的能力位。

    返回 ``(ok, support_bits, reason)``。P010 在部分 AMD 驱动上直接
    E_FAIL（RX 6800 实测）——这类机器上手动创建 P010 纹理的池必须
    fail-closed，P010 surface 只能来自解码器/编码器自有分配。
    """
    support = ctypes.c_uint(0)
    proto = _WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
    )
    try:
        hr = vtable_fn(
            device.device_ptr, VTABLE_SLOT_CHECK_FORMAT_SUPPORT, proto,
        )(device.device_ptr, dxgi_format, ctypes.byref(support))
    except Exception as exc:
        return False, 0, param(
            FallbackReason.GPU_PROBE_FAILED, f"check_format_support:{exc}",
        )
    if hr != 0:
        return False, 0, param(
            FallbackReason.GPU_PROBE_FAILED,
            f"check_format_support fmt={dxgi_format} hr=0x{hr & 0xFFFFFFFF:08X}",
        )
    return True, support.value, ""


def _device_adapter_identity(device_ptr: int) -> Tuple[str, str, str]:
    """device → IDXGIDevice → IDXGIAdapter::GetDesc → (LUID, 描述, vendor)。

    走查失败不视为 device 失败：LUID 缺失时返回空串。没有 LUID 的
    device 不允许参与全 GPU 导出（调用方以空 LUID 显式拒绝），但可以
    继续做与 LUID 无关的纹理池验证。
    """
    try:
        dxgi_device = ctypes.c_void_p(0)
        proto_qi = _WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        )
        hr = vtable_fn(device_ptr, VTABLE_SLOT_QUERY_INTERFACE, proto_qi)(
            device_ptr,
            ctypes.byref(_IID_IDXGIDevice),
            ctypes.byref(dxgi_device),
        )
        if hr != 0 or not dxgi_device.value:
            return "", "", ""
        try:
            adapter = ctypes.c_void_p(0)
            proto_ga = _WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )
            hr = vtable_fn(
                dxgi_device.value, VTABLE_SLOT_IDXGIDEVICE_GET_ADAPTER, proto_ga,
            )(dxgi_device.value, ctypes.byref(adapter))
            if hr != 0 or not adapter.value:
                return "", "", ""
            try:
                raw = _adapter_desc_raw(adapter_ptr=adapter.value)
                if not raw:
                    return "", "", ""
                info = parse_adapter_desc(raw)
                return (info.adapter_luid, info.description,
                        info.vendor_name or "")
            finally:
                release_com_object(adapter.value)
        finally:
            release_com_object(dxgi_device.value)
    except Exception as exc:  # 诊断走查绝不拖垮 device 创建本身
        logger.warning("adapter LUID 走查失败: %s", exc)
        return "", "", ""


def _adapter_desc_raw(adapter_ptr: int) -> bytes:
    """IDXGIAdapter::GetDesc → DXGI_ADAPTER_DESC 原始字节（304 字节）。"""
    buffer = ctypes.create_string_buffer(_DXGI_ADAPTER_DESC_SIZE)
    proto = _WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
    )
    hr = vtable_fn(
        adapter_ptr, VTABLE_SLOT_IDXGIADAPTER_GET_DESC, proto,
    )(adapter_ptr, buffer)
    if hr != 0:
        return b""
    return buffer.raw[:_DXGI_ADAPTER_DESC_SIZE]
