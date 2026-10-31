"""Windows GPU / 驱动能力探针（计划 Phase 8 P8-01）。

替换 ``capability.py`` 的 stub：通过 ctypes DXGI（CreateDXGIFactory1 →
EnumAdapters → GetDesc）枚举适配器，记录 vendor / device / 显存；
NVIDIA 机器上可选经 nvml.dll 读驱动版本。产出供零拷贝门禁与诊断记录
（``WindowsZeroCopyCapability.last_probe_json``）消费。

诚实性边界（COM 走查层仅真实 Windows 可验证；W1-01 起已在 AMD RX 6800 /
Win11 26200 真机收口）：

- **COM 走查层**（``_create_dxgi_factory`` / ``_enumerate_adapters`` /
  ``_get_adapter_desc``）是纯 ctypes 实现，vtable 索引按 SDK dxgi.h 方法
  序钉死（IUnknown 占 0-2，IDXGIObject 占 3-6，IDXGIFactory1::EnumAdapters=7，
  IDXGIAdapter::GetDesc=8——W1-01 真机修正值，详见各函数 docstring），
  换 SDK 头文件布局时须同步复核。
- **数据整形层**（vendor 映射、主适配器选择、capability 组装）与平台
  无关，测试用假数据注入走查层覆盖（本文件配套测试即如此）。
- ``is_zero_copy_available()`` 由此不再是常量 False：available 由
  「找到适配器 + interop 扩展存在」的探测结果决定（本机/macOS 仍诚实
  返回 not_windows）。

参考：Microsoft Learn — DXGI (dxgi.h)、IDXGIFactory1、IDXGIAdapter::GetDesc。
"""

from __future__ import annotations

import ctypes
import json
import logging
import platform
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# DXGK 验厂 ID（PCI SIG 分配）
VENDOR_NAMES = {
    0x10DE: "nvidia",
    0x1002: "amd",
    0x1022: "amd",
    0x8086: "intel",
}

# DXGI_ADAPTER_DESC 固定布局（win 64-bit，按 SDK dxgi.h 实序）：
# WCHAR Description[128] (256B) + UINT×4 (16B) + SIZE_T×3 (24B) + LUID (8B)
# —— W1-01 真机修正：LUID 是**最后一个**字段（296..303），不在 Description
# 之后；旧注释/偏移把 LUID 放第二位，导致 vendor_id 读到 SubSysId、
# dedicated_video_memory 恒 0。总大小 304 字节。
_DXGI_ADAPTER_DESC_SIZE = 256 + 16 + 24 + 8


@dataclass
class AdapterInfo:
    """单个 DXGI 适配器的探测产物。"""

    description: str = ""
    vendor_id: int = 0
    device_id: int = 0
    dedicated_video_memory: int = 0
    vendor_name: str = "unknown"
    adapter_luid: str = ""  # "LowPart-HighPart" 十六进制（W1-03 adapter 匹配用）

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "vendor_id": f"0x{self.vendor_id:04X}",
            "device_id": f"0x{self.device_id:04X}",
            "dedicated_video_memory_mb": round(self.dedicated_video_memory / (1024 * 1024)),
            "vendor_name": self.vendor_name,
            "adapter_luid": self.adapter_luid,
        }


@dataclass
class WindowsGpuCapability:
    """GPU/驱动探测汇总（P8-01 交付物）。"""

    probe_ok: bool = False
    reason: str = "not_probed"
    adapters: list = field(default_factory=list)
    driver_version: str = ""  # nvml.dll 存在时（NVIDIA）；否则留空
    primary_vendor: str = "unknown"  # 第一个适配器的 vendor

    def to_json(self) -> str:
        return json.dumps(
            {
                "probe_ok": self.probe_ok,
                "reason": self.reason,
                "driver_version": self.driver_version,
                "primary_vendor": self.primary_vendor,
                "adapters": [a.to_dict() for a in self.adapters],
            },
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# COM 走查层（仅 Windows 可验证；vtable 索引按文档钉死）
# ---------------------------------------------------------------------------


def _create_dxgi_factory(dxgi: ctypes.CDLL) -> int:
    """CreateDXGIFactory1 → IDXGIFactory1*（失败返回 0）。

    IID_IDXGIFactory1 = {770AAE78-F26F-4DBA-A829-253C83D1B387}。
    W1-01 真机（AMD RX 6800 / Win11 26200 / SDK 10.0.26100 dxgi.h）验证：
    此前钉死的 {770A5F70-7808-4E6C-9FD0-F534CF3EEAE3} 是错的，
    CreateDXGIFactory1 一律返回 E_NOINTERFACE（0x80004002）。
    """
    iid_ixgifactory1 = (ctypes.c_ubyte * 16)(
        0x78, 0xAE, 0x0A, 0x77, 0x6F, 0xF2, 0xBA, 0x4D,
        0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87,
    )
    factory = ctypes.c_void_p(0)
    rc = dxgi.CreateDXGIFactory1(ctypes.cast(iid_ixgifactory1, ctypes.c_void_p), ctypes.byref(factory))
    if rc != 0:
        raise OSError(f"CreateDXGIFactory1 hr=0x{rc & 0xFFFFFFFF:08X}")
    return factory.value or 0


def _enum_adapters(factory: int, index: int) -> int:
    """IDXGIFactory1::EnumAdapters（vtable[7]）→ adapter*（0 = 无更多）。"""
    vtbl = ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn = ctypes.cast(vtbl + 7 * ctypes.sizeof(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)).contents.value
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
    adapter = ctypes.c_void_p(0)
    hr = ctypes.cast(fn, proto)(factory, index, ctypes.byref(adapter))
    if hr < 0:  # DXGI_ERROR_NOT_FOUND 等
        return 0
    return adapter.value or 0


def _get_adapter_desc(adapter: int) -> bytes:
    """IDXGIAdapter::GetDesc（vtable[8]）→ DXGI_ADAPTER_DESC 原始字节。

    W1-01 真机修正：IDXGIObject 占 3-6（GetParent=6），IDXGIAdapter 自有
    方法从 7 起（EnumOutputs=7、GetDesc=8、CheckInterfaceSupport=9，顺序
    按 SDK dxgi.h 声明）；旧索引 6 实调的是 GetParent。
    """
    vtbl = ctypes.cast(adapter, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn = ctypes.cast(vtbl + 8 * ctypes.sizeof(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)).contents.value
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
    desc = (ctypes.c_ubyte * _DXGI_ADAPTER_DESC_SIZE)()
    hr = ctypes.cast(fn, proto)(adapter, desc)
    if hr < 0:
        raise OSError(f"GetDesc hr=0x{hr & 0xFFFFFFFF:08X}")
    return bytes(desc)


def _com_release(ptr: int) -> None:
    if ptr:
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
        fn = ctypes.cast(vtbl + 2 * ctypes.sizeof(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)).contents.value
        proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        ctypes.cast(fn, proto)(ptr)


# ---------------------------------------------------------------------------
# 数据整形层（平台无关，测试覆盖处）
# ---------------------------------------------------------------------------


def parse_adapter_desc(raw: bytes) -> AdapterInfo:
    """DXGI_ADAPTER_DESC 原始字节 → AdapterInfo。

    偏移按 SDK dxgi.h 实序（LUID 在末尾）：VendorId=256、DeviceId=260、
    DedicatedVideoMemory=272、AdapterLuid=296。
    """
    raw = raw[:_DXGI_ADAPTER_DESC_SIZE].ljust(_DXGI_ADAPTER_DESC_SIZE, b"\0")
    description = bytes(raw[0:256]).decode("utf-16-le", errors="replace").split("\0", 1)[0]
    vendor_id, device_id = int.from_bytes(raw[256:260], "little"), int.from_bytes(
        raw[260:264], "little"
    )
    dedicated = int.from_bytes(raw[272:280], "little")
    luid_low = int.from_bytes(raw[296:300], "little", signed=True)
    luid_high = int.from_bytes(raw[300:304], "little", signed=True)
    return AdapterInfo(
        description=description,
        vendor_id=vendor_id,
        device_id=device_id,
        dedicated_video_memory=dedicated,
        vendor_name=VENDOR_NAMES.get(vendor_id, "unknown"),
        adapter_luid=f"{luid_low & 0xFFFFFFFF:08X}-{luid_high & 0xFFFFFFFF:08X}",
    )


def _probe_driver_version_nvml() -> str:
    """nvml.dll（NVIDIA 驱动自带）读驱动版本；不可用返回空串。"""
    try:
        nvml = ctypes.CDLL("nvml.dll")
        nvml.nvmlInit_v2()
        version = ctypes.create_string_buffer(80)
        nvml.nvmlSystemGetDriverVersion(version, len(version))
        nvml.nvmlShutdown()
        return version.value.decode("utf-8", errors="replace")
    except Exception:
        return ""


def probe_gpu_capability() -> WindowsGpuCapability:
    """枚举 DXGI 适配器 + （可选）NVML 驱动版本。非 Windows 诚实拒绝。"""
    if platform.system() != "Windows":
        return WindowsGpuCapability(probe_ok=False, reason="not_windows")
    try:
        dxgi = ctypes.CDLL("dxgi.dll")
    except OSError as e:
        return WindowsGpuCapability(probe_ok=False, reason=f"dxgi_load_failed:{e}")
    try:
        factory = _create_dxgi_factory(dxgi)
    except Exception as e:
        return WindowsGpuCapability(probe_ok=False, reason=f"factory_failed:{e}")

    adapters: list = []
    try:
        for i in range(8):
            ptr = _enum_adapters(factory, i)
            if not ptr:
                break
            try:
                adapters.append(parse_adapter_desc(_get_adapter_desc(ptr)))
            finally:
                _com_release(ptr)
    except Exception as e:
        logger.warning(f"DXGI adapter enumeration failed: {e}")
    finally:
        _com_release(factory)

    cap = WindowsGpuCapability(
        probe_ok=True,
        reason="ok" if adapters else "no_adapter",
        adapters=adapters,
        driver_version=_probe_driver_version_nvml(),
        primary_vendor=adapters[0].vendor_name if adapters else "unknown",
    )
    logger.info(f"WindowsGpuCapability: {cap.to_json()}")
    return cap


# ---------------------------------------------------------------------------
# W1-03：adapter 匹配（decode GPU vs render GPU）
# ---------------------------------------------------------------------------

_HARDWARE_VENDORS = frozenset({"amd", "nvidia", "intel"})


def pick_primary_hardware_adapter(adapters: list) -> Optional[AdapterInfo]:
    """从 DXGI 适配器列表选主硬件适配器（跳过 Basic Render Driver 等）。

    DXGI 枚举顺序即「主输出优先」；Optimus/iGPU+dGPU 笔记本上第一个
    通常承担 present。选择规则保持诚实：找不到硬件适配器返回 None，
    绝不猜。
    """
    for adapter in adapters:
        if adapter.vendor_name in _HARDWARE_VENDORS:
            return adapter
    return None


def check_adapter_match(render_adapter_luid: str,
                        decode_adapter_luid: str) -> tuple[bool, str]:
    """render GPU 与 decode GPU 的 LUID 匹配裁决。

    返回 (匹配, reason)。不匹配时 reason 为
    ``adapter_mismatch:<render_luid>|<decode_luid>``（词表 reasons.py，
    W3 interop 的硬前置：跨适配器共享句柄不可用，必须锁定同 adapter
    或回退 download）。
    """
    from .reasons import FallbackReason, param

    if render_adapter_luid == decode_adapter_luid:
        return True, ""
    return False, param(
        FallbackReason.ADAPTER_MISMATCH,
        f"{render_adapter_luid}|{decode_adapter_luid}",
    )


def decode_render_adapter_report(
    render_adapter_luid: str = "",
) -> dict:
    """W1-03 诊断快照：本机 decode 侧适配器与 render 侧 LUID 的匹配状态。

    ``render_adapter_luid`` 来自 GL 侧（运行时经 interop/WGL 查询传入）；
    不传时如实记 ``render_adapter_unprobed``（不猜）。
    """
    gpu = probe_gpu_capability()
    decode_adapter = pick_primary_hardware_adapter(gpu.adapters)
    decode_luid = decode_adapter.adapter_luid if decode_adapter else ""
    if not render_adapter_luid or not decode_luid:
        match, reason = False, (
            "render_adapter_unprobed" if not render_adapter_luid
            else "decode_adapter_absent")
    else:
        match, reason = check_adapter_match(render_adapter_luid, decode_luid)
    return {
        "probe_ok": gpu.probe_ok,
        "decode_adapter": decode_adapter.to_dict() if decode_adapter else None,
        "decode_adapter_luid": decode_luid,
        "render_adapter_luid": render_adapter_luid,
        "match": match,
        "reason": reason,
    }
