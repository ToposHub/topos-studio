#!/usr/bin/env python3
"""W5-03 前置 probe：本机显示器 HDR / 高级色能力探测（ctypes DXGI 走查）。

链路：CreateDXGIFactory1 → EnumAdapters → EnumOutputs →
IUnknown::QueryInterface(IID_IDXGIOutput6) → GetDesc1（DXGI_OUTPUT_DESC1）。

vtable 依据（本机 SDK 10.0.26100 dxgi.h/dxgi1_6.h 提取，方法为 ABI 定序）：
- IUnknown 0-2；IDXGIObject 3-6（GetParent=6）
- IDXGIOutput 自有 12 方法（7-18：GetDesc=7 … GetFrameStatistics=18；
  **本 SDK 的 Gamma 三方法在 IDXGIOutput 上**，GetGammaControlCapabilities=13、
  SetGammaControl=14、GetGammaControl=15）
- IDXGIOutput1 +4（19-22）、IDXGIOutput2 +1（23）、IDXGIOutput3 +1（24）、
  IDXGIOutput4 +1（25）、IDXGIOutput5 +1（26）
- **IDXGIOutput6::GetDesc1 = vtable[27]**、CheckHardwareCompositionSupport = 28

DXGI_OUTPUT_DESC1（152 字节，8 字节对齐）：
DeviceName[32](0-63) DesktopCoordinates(64-79) AttachedToDesktop(80-83)
Rotation(84-87) Monitor(88-95) BitsPerColor(96-99) ColorSpace(100-103)
RedPrimary[2](104) GreenPrimary[2](112) BluePrimary[2](120) WhitePoint[2](128)
MinLuminance(136) MaxLuminance(140) MaxFullFrameLuminance(144)

诚实性边界：
- ColorSpace 反映的是**当前**高级色状态（Windows「使用 HDR」开关）：
  RGB_FULL_G2084_NONE_P2020(12)=HDR 生效；RGB_FULL_G22_NONE_P709(0)=SDR
  模式——SDR 模式不等于显示器无 HDR 能力，只代表此刻未启用。
- MaxLuminance / MaxFullFrameLuminance 仅在 HDR 元数据可达时非零。
- IID 与 vtable 槽位若与运行时 ABI 漂移，用 GetDesc 的 DeviceName 与
  GetDesc1 的 DeviceName 交叉验证兜底——不一致即报 drift，不猜。

用法：.venv/Scripts/python.exe scripts/probe_hdr_display_windows.py
"""

from __future__ import annotations

import ctypes
import json
import platform
import sys
from datetime import datetime

COLOR_SPACE_NAMES = {
    0: "RGB_FULL_G22_NONE_P709 (SDR sRGB)",
    1: "RGB_FULL_G10_NONE_P709",
    2: "RGB_LINEAR_G10_NONE_P709",
    3: "RGB_FULL_G22_NONE_P2020",
    4: "RGB_FULL_G10_NONE_P2020",
    5: "RGB_LINEAR_G22_NONE_P2020",
    6: "RGB_LINEAR_G10_NONE_P2020",
    8: "YCBCR_FULL_G22_NONE_P709_X601",
    11: "YCBCR_STUDIO_G2084_LEFT_P2020",
    12: "RGB_FULL_G2084_NONE_P2020 (HDR10)",
}

IID_IDXGIFACTORY1 = (ctypes.c_ubyte * 16)(
    0x78, 0xAE, 0x0A, 0x77, 0x6F, 0xF2, 0xBA, 0x4D,
    0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)
# {068346E8-AAEC-4B84-ADD7-137F513F77A1}（SDK dxgi1_6.h DEFINE_GUID）
IID_IDXGIOUTPUT6 = (ctypes.c_ubyte * 16)(
    0xE8, 0x46, 0x83, 0x06, 0xEC, 0xAA, 0x84, 0x4B,
    0xAD, 0xD7, 0x13, 0x7F, 0x51, 0x3F, 0x77, 0xA1)

OUTPUT_DESC_SIZE = 96     # DXGI_OUTPUT_DESC
OUTPUT_DESC1_SIZE = 152   # DXGI_OUTPUT_DESC1
VTBL_GETDESC = 7          # IDXGIOutput::GetDesc
VTBL_GETDESC1 = 27        # IDXGIOutput6::GetDesc1


def _vtbl_fn(ptr: int, slot: int, proto):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn = ctypes.cast(
        vtbl + slot * ctypes.sizeof(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p)).contents.value
    return ctypes.cast(fn, proto)


def _com_release(ptr: int) -> None:
    if ptr:
        _vtbl_fn(ptr, 2, ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p))(ptr)


def _parse_desc_common(raw: bytes) -> dict:
    """DXGI_OUTPUT_DESC / DESC1 的公共前 96 字节。"""
    name = bytes(raw[0:64]).decode("utf-16-le", errors="replace").split("\0", 1)[0]
    monitor = int.from_bytes(raw[88:96], "little")
    return {"device_name": name, "monitor": f"0x{monitor:X}"}


def _parse_desc1(raw: bytes) -> dict:
    import struct
    out = _parse_desc_common(raw)
    f = lambda off: struct.unpack_from("<f", raw, off)[0]
    cs = int.from_bytes(raw[100:104], "little")
    out.update({
        "bits_per_color": int.from_bytes(raw[96:100], "little"),
        "color_space": cs,
        "color_space_name": COLOR_SPACE_NAMES.get(cs, f"unknown({cs})"),
        "red_primary": [f(104), f(108)],
        "green_primary": [f(112), f(116)],
        "blue_primary": [f(120), f(124)],
        "white_point": [f(128), f(132)],
        "min_luminance_nits": round(f(136), 4),
        "max_luminance_nits": round(f(140), 1),
        "max_full_frame_luminance_nits": round(f(144), 1),
        "hdr_active": cs == 12,
    })
    return out


def probe_outputs() -> dict:
    if platform.system() != "Windows":
        return {"probe_ok": False, "reason": "not_windows"}

    dxgi = ctypes.CDLL("dxgi.dll")
    factory = ctypes.c_void_p(0)
    iid_f = ctypes.cast(IID_IDXGIFACTORY1, ctypes.c_void_p)
    hr = dxgi.CreateDXGIFactory1(iid_f, ctypes.byref(factory))
    if hr != 0 or not factory.value:
        return {"probe_ok": False,
                "reason": f"CreateDXGIFactory1 hr=0x{hr & 0xFFFFFFFF:08X}"}

    enum_adapters = _vtbl_fn(factory.value, 7, ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p)))

    outputs: list[dict] = []
    try:
        for ai in range(8):
            adapter = ctypes.c_void_p(0)
            if enum_adapters(factory, ai, ctypes.byref(adapter)) < 0 \
                    or not adapter.value:
                break
            try:
                # IDXGIAdapter::GetDesc = vtable[8]（W1-01 已验证）
                get_desc = _vtbl_fn(adapter.value, 8, ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p))
                adesc = (ctypes.c_ubyte * 304)()
                if get_desc(adapter, adesc) == 0:
                    a_name = bytes(adesc[0:256]).decode(
                        "utf-16-le", errors="replace").split("\0", 1)[0]
                else:
                    a_name = "<GetDesc failed>"
                a_vid = int.from_bytes(bytes(adesc[256:260]), "little")

                # IDXGIAdapter::EnumOutputs = vtable[7]
                enum_outputs = _vtbl_fn(adapter.value, 7, ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_uint,
                    ctypes.POINTER(ctypes.c_void_p)))
                for oi in range(16):
                    output = ctypes.c_void_p(0)
                    if enum_outputs(adapter, oi, ctypes.byref(output)) < 0 \
                            or not output.value:
                        break
                    try:
                        entry = {"adapter": a_name,
                                 "adapter_vendor": f"0x{a_vid:04X}"}
                        # 基础 GetDesc（前 96 字节，含 DeviceName 交叉验证基准）
                        out_getdesc = _vtbl_fn(output.value, VTBL_GETDESC,
                                               ctypes.WINFUNCTYPE(
                                                   ctypes.c_long,
                                                   ctypes.c_void_p,
                                                   ctypes.c_void_p))
                        base = (ctypes.c_ubyte * OUTPUT_DESC_SIZE)()
                        hr0 = out_getdesc(output, base)
                        base_name = (
                            bytes(base[0:64]).decode("utf-16-le", errors="replace")
                            .split("\0", 1)[0] if hr0 == 0 else "")

                        qi = _vtbl_fn(output.value, 0, ctypes.WINFUNCTYPE(
                            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p)))
                        out6 = ctypes.c_void_p(0)
                        hr_qi = qi(output, ctypes.cast(IID_IDXGIOUTPUT6,
                                                       ctypes.c_void_p),
                                   ctypes.byref(out6))
                        if hr_qi != 0 or not out6.value:
                            entry.update({
                                "idxgioutput6": False,
                                "reason": f"QueryInterface hr=0x{hr_qi & 0xFFFFFFFF:08X}",
                                "getdesc_hr": f"0x{hr0 & 0xFFFFFFFF:08X}",
                            })
                            outputs.append(entry)
                            continue
                        try:
                            get_desc1 = _vtbl_fn(out6.value, VTBL_GETDESC1,
                                                 ctypes.WINFUNCTYPE(
                                                     ctypes.c_long,
                                                     ctypes.c_void_p,
                                                     ctypes.c_void_p))
                            d1 = (ctypes.c_ubyte * OUTPUT_DESC1_SIZE)()
                            hr1 = get_desc1(out6, d1)
                            if hr1 != 0:
                                entry.update({
                                    "idxgioutput6": True,
                                    "getdesc1_hr": f"0x{hr1 & 0xFFFFFFFF:08X}",
                                })
                            else:
                                info = _parse_desc1(bytes(d1))
                                info["idxgioutput6"] = True
                                # vtable 漂移兜底：两条路径的 DeviceName 必须一致
                                info["vtable_sanity"] = (
                                    "ok" if (not base_name or
                                             base_name == info["device_name"])
                                    else f"DRIFT({base_name!r} vs "
                                         f"{info['device_name']!r})")
                                entry.update(info)
                            entry["getdesc_hr"] = f"0x{hr0 & 0xFFFFFFFF:08X}"
                        finally:
                            _com_release(out6.value)
                        outputs.append(entry)
                    finally:
                        _com_release(output.value)
            finally:
                _com_release(adapter.value)
    finally:
        _com_release(factory.value)

    hdr_any = any(o.get("hdr_active") for o in outputs)
    return {
        "probe_ok": True,
        "hdr_active_any_output": hdr_any,
        "output_count": len(outputs),
        "outputs": outputs,
        "note": ("ColorSpace 反映当前高级色状态：SDR(0) 不代表显示器无 HDR "
                 "能力，只代表 Windows「使用 HDR」未启用；W5-03 裁决前需"
                 "在该显示器上开启 HDR 复测。"),
    }


def main() -> int:
    report = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "plan_item": "W5-03 前置 probe：HDR/高级色显示能力（本机）",
        **probe_outputs(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("probe_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
