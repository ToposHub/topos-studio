#!/usr/bin/env python3
"""W1-02：WGL_NV_DX_interop 真机探测（K-W2 门槛证据）。

探测层级（逐级深入，任何一级失败都如实记录 reason，不猜）：

1. DXGI 适配器枚举（W1-01 已验证的走查层）——记录 vendor/驱动。
2. ModernGL standalone context（glcontext WGL 后端）——回答开放问题 3
   「Windows 上 ModernGL 实际创建的 GL 上下文类型」。
3. wglGetExtensionsStringEXT —— 扩展字符串里是否有 WGL_NV_DX_interop(2)。
4. wglGetProcAddress("wglDXOpenDeviceNV") —— 函数指针存在才算数。
5. 创建 D3D11 设备（BGRA_SUPPORT）并真实调用 wglDXOpenDeviceNV——
   返回非 NULL 即 interop 可打开（W3 的可行性证据）。

计划依据：W1-02「不得按 vendor 猜测，只信探测」；K-W2「W1-02 显示
不可用 → W3 降级或搁置」。

用法：
    .venv/Scripts/python.exe scripts/probe_wgl_interop_windows.py
    .venv/Scripts/python.exe scripts/probe_wgl_interop_windows.py --json out.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.media.windows_hw.gpu_capability import probe_gpu_capability  # noqa: E402


def probe_d3d11_device() -> tuple[int, str]:
    """D3D11CreateDevice(BGRA_SUPPORT) → (device ptr, hr)。失败返回 (0, hr)。"""
    d3d11 = ctypes.CDLL("d3d11.dll")
    device = ctypes.c_void_p(0)
    context = ctypes.c_void_p(0)
    feature_level = ctypes.c_int(0)
    # D3D_DRIVER_TYPE_HARDWARE=1, BGRA_SUPPORT=0x20, SDK_VERSION=7
    try:
        hr = d3d11.D3D11CreateDevice(
            None, 1, None, 0x20, None, 0, 7,
            ctypes.byref(device), ctypes.byref(feature_level), ctypes.byref(context),
        )
    except Exception as exc:  # pragma: no cover
        return 0, f"exception:{exc}"
    if context.value:  # 立即释放 immediate context（引用计数即可）
        device._keep = context  # type: ignore[attr-defined]
    return (device.value or 0), f"0x{hr & 0xFFFFFFFF:08X}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_out", default="", help="可选 JSON 输出路径")
    args = parser.parse_args()

    report: dict = {"date": datetime.now().isoformat(timespec="seconds"),
                    "purpose": "W1-02 WGL_NV_DX_interop probe (K-W2 evidence)"}

    # --- 1. DXGI adapters -------------------------------------------------
    gpu = probe_gpu_capability()
    report["dxgi"] = {
        "probe_ok": gpu.probe_ok,
        "reason": gpu.reason,
        "primary_vendor": gpu.primary_vendor,
        "adapters": [a.to_dict() for a in gpu.adapters],
    }
    print(f"[1] DXGI: ok={gpu.probe_ok} vendor={gpu.primary_vendor} "
          f"adapters={[a.description for a in gpu.adapters]}")

    if not gpu.probe_ok:
        report["verdict"] = "inconclusive_no_dxgi"
        _emit(report, args.json_out)
        return 1

    # --- 2. ModernGL standalone context ------------------------------------
    # 显式请求 4.6 Core（W4-01 前置：compute 需要 GL 4.3+；AMD Windows
    # 驱动应支持 4.6，standalone 默认只给 3.3 就拿不到）。
    gl_info: dict = {}
    ctx = None
    for version in ((4, 6), (4, 5), (4, 3), None):
        try:
            import moderngl

            if version is None:
                ctx = moderngl.create_standalone_context()
            else:
                ctx = moderngl.create_standalone_context(
                    version=version, profile="core")
            break
        except Exception as exc:
            gl_info.setdefault("request_failures", []).append(
                {"version": version, "error": str(exc)})
    if ctx is not None:
        info = ctx.info
        gl_info.update({
            "gl_version": info.get("GL_VERSION", ""),
            "gl_renderer": info.get("GL_RENDERER", ""),
            "gl_vendor": info.get("GL_VENDOR", ""),
            "requested_version": version,
        })
        print(f"[2] ModernGL context: {gl_info['gl_renderer']} | "
              f"{gl_info['gl_version']} | requested={version}")
    else:
        gl_info["error"] = "no context creatable"
        print(f"[2] ModernGL context FAILED: {gl_info['request_failures'][-1]}")
    report["gl_context"] = gl_info

    wgl_exts = ""
    if ctx is not None:
        # --- 3. wglGetExtensionsStringEXT（经 wglGetProcAddress 向 ICD 获取；
        #        直接 CDLL 导出不存在——AMD 真机实测，capability.py 同此修复）--
        try:
            opengl32 = ctypes.CDLL("opengl32.dll")
            opengl32.wglGetProcAddress.restype = ctypes.c_void_p
            opengl32.wglGetProcAddress.argtypes = [ctypes.c_char_p]
            fn_ptr = opengl32.wglGetProcAddress(b"wglGetExtensionsStringEXT")
            if fn_ptr:
                proto_ext = ctypes.WINFUNCTYPE(ctypes.c_char_p)
                wgl_exts = (
                    ctypes.cast(fn_ptr, proto_ext)() or b""
                ).decode("ascii", errors="replace")
                has1 = "WGL_NV_DX_interop " in wgl_exts + " " or \
                    "WGL_NV_DX_interop2" in wgl_exts
                print(f"[3] WGL extensions: NV_DX_interop(any)={has1} "
                      f"(total {len(wgl_exts.split())} extensions)")
                report["wgl_extensions"] = {
                    "nv_dx_interop_any": has1,
                    "count": len(wgl_exts.split()),
                }
            else:
                report["wgl_extensions"] = {"error": "GetProcAddress returned NULL"}
                print("[3] wglGetExtensionsStringEXT fn ptr NULL")
        except Exception as exc:
            report["wgl_extensions"] = {"error": str(exc)}
            print(f"[3] wglGetExtensionsStringEXT FAILED: {exc}")

        # --- 4/5. wglGetProcAddress + wglDXOpenDeviceNV ------------------
        interop: dict = {}
        try:
            opengl32 = ctypes.CDLL("opengl32.dll")
            opengl32.wglGetProcAddress.restype = ctypes.c_void_p
            opengl32.wglGetProcAddress.argtypes = [ctypes.c_char_p]
            fn_ptr = opengl32.wglGetProcAddress(b"wglDXOpenDeviceNV")
            interop["open_device_fn_ptr"] = bool(fn_ptr)
            if fn_ptr:
                proto = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)
                dx_open = ctypes.cast(fn_ptr, proto)
                dev_handle, hr = probe_d3d11_device()
                interop["d3d11_create_device_hr"] = hr
                if dev_handle:
                    opened = dx_open(dev_handle)
                    interop["dx_open_device_nv"] = bool(opened)
                    verdict = ("interop_open_ok" if opened
                               else "interop_open_failed")
                else:
                    verdict = "inconclusive_no_d3d11_device"
                print(f"[5] wglDXOpenDeviceNV: {interop}")
            else:
                verdict = "fn_ptr_absent"
                print("[4] wglGetProcAddress(wglDXOpenDeviceNV) -> NULL")
        except Exception as exc:
            interop["error"] = str(exc)
            verdict = "probe_error"
            print(f"[4/5] interop probe error: {exc}")
        report["wgl_dx_interop"] = interop
    else:
        verdict = "inconclusive_no_gl_context"

    report["verdict"] = verdict
    print(f"\nverdict: {verdict}")
    _emit(report, args.json_out)
    if ctx is not None:
        ctx.release()
    return 0


def _emit(report: dict, json_out: str) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if json_out:
        Path(json_out).write_text(text + "\n", encoding="utf-8")
        print(f"JSON written: {json_out}")


if __name__ == "__main__":
    raise SystemExit(main())
