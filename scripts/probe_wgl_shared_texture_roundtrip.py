#!/usr/bin/env python3
"""W3-01/02 原型验证：D3D11 共享纹理 → GL 注册/锁定/采样 roundtrip（真机）。

在 W1-02（interop_open_ok）之上再进一步，逐级验证 WGL_NV_DX_interop 的
完整链路（计划 W3-01/02 的可行性证据，全部真机实测、逐级落 reason）：

1. D3D11 设备 + BGRA8 纹理（CPU 写入已知图案，RENDER_TARGET|SHADER_RESOURCE）
2. wglDXOpenDeviceNV(interop device)
3. wglDXRegisterObjectNV(GL texture ← D3D texture)
4. wglDXLockObjectsNV
5. FBO attach + glReadPixels 读回 → 与 D3D 侧图案逐像素比对
6. Unlock / Unregister / CloseDevice

ID3D11Device vtable 序按 SDK d3d11.h 钉死：CreateTexture2D=[5]、
GetFeatureLevel=[35]（先做健全性检查，不匹配立即中止，防 vtable 错位崩溃）。

用法：
    .venv/Scripts/python.exe scripts/probe_wgl_shared_texture_roundtrip.py
    .venv/Scripts/python.exe scripts/probe_wgl_shared_texture_roundtrip.py --json out.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SIZE = 64
DXGI_FORMAT_B8G8R8A8_UNORM = 87
GL_TEXTURE_2D = 0x0DE1
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
WGL_ACCESS_READ_ONLY_NV = 0x0000


def _vtbl_fn(ptr: int, slot: int, proto):
    """取 COM 对象 vtable 第 slot 槽并按 proto 转型。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn = ctypes.cast(vtbl + slot * ctypes.sizeof(ctypes.c_void_p),
                     ctypes.POINTER(ctypes.c_void_p)).contents.value
    return ctypes.cast(fn, proto)


def make_pattern() -> np.ndarray:
    """BGRA8 图案：B=0x80, G=y*4, R=x*4, A=0xFF（64×64）。"""
    xs, ys = np.meshgrid(np.arange(SIZE), np.arange(SIZE))
    px = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
    px[..., 0] = 0x80          # B
    px[..., 1] = (ys * 4).astype(np.uint8)  # G
    px[..., 2] = (xs * 4).astype(np.uint8)  # R
    px[..., 3] = 0xFF          # A
    return np.ascontiguousarray(px)


def create_d3d11_texture(device: int) -> tuple[int, np.ndarray]:
    """CreateTexture2D（vtable[5]）+ 初始图案；返回 (texture, pattern)。"""
    pattern = make_pattern()
    # D3D11_TEXTURE2D_DESC：Width,Height,MipLevels,ArraySize,Format,
    # SampleDesc{Count,Quality},Usage,BindFlags,CPUAccessFlags,MiscFlags
    desc = (ctypes.c_uint * 11)(
        SIZE, SIZE, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM,
        1, 0,           # SampleDesc
        0,              # D3D11_USAGE_DEFAULT
        0x28,           # BIND_RENDER_TARGET | BIND_SHADER_RESOURCE
        0, 0,
    )
    subres = D3D11_SUBRESOURCE_DATA(pattern.ctypes.data, SIZE * 4, 0)
    texture = ctypes.c_void_p(0)
    proto = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    hr = _vtbl_fn(device, 5, proto)(
        device, ctypes.byref(desc), ctypes.byref(subres), ctypes.byref(texture))
    if hr != 0 or not texture.value:
        raise RuntimeError(f"CreateTexture2D hr=0x{hr & 0xFFFFFFFF:08X}")
    return texture.value, pattern


class D3D11_SUBRESOURCE_DATA(ctypes.Structure):
    _fields_ = [("pSysMem", ctypes.c_void_p),
                ("SysMemPitch", ctypes.c_uint),
                ("SysMemSlicePitch", ctypes.c_uint)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_out", default="", help="可选 JSON 输出路径")
    args = parser.parse_args()

    report: dict = {"date": datetime.now().isoformat(timespec="seconds"),
                    "purpose": "W3-01/02 shared texture roundtrip (AMD real machine)"}
    steps: list[dict] = []

    def step(name: str, ok: bool, detail: str = "") -> bool:
        steps.append({"step": name, "ok": ok, "detail": detail})
        print(f"[{'OK' if ok else 'FAIL'}] {name} {detail}")
        return ok

    # --- GL context（moderngl standalone，同 W1-02） ----------------------
    ctx = None
    for version in ((4, 6), (4, 5), (4, 3), None):
        try:
            import moderngl

            ctx = (moderngl.create_standalone_context(version=version, profile="core")
                   if version else moderngl.create_standalone_context())
            break
        except Exception:
            ctx = None
    if not step("gl_context", ctx is not None,
                str(ctx.info.get("GL_VERSION", "")) if ctx else "no context"):
        return _emit(report, steps, args.json_out, ctx)

    opengl32 = ctypes.CDLL("opengl32.dll")
    opengl32.wglGetProcAddress.restype = ctypes.c_void_p
    opengl32.wglGetProcAddress.argtypes = [ctypes.c_char_p]

    def gl_proc(name: str):
        ptr = opengl32.wglGetProcAddress(name.encode())
        return ptr

    # --- D3D11 device + texture -------------------------------------------
    d3d11 = ctypes.CDLL("d3d11.dll")
    device = ctypes.c_void_p(0)
    context = ctypes.c_void_p(0)
    fl = ctypes.c_int(0)
    hr = d3d11.D3D11CreateDevice(None, 1, None, 0x20, None, 0, 7,
                                 ctypes.byref(device), ctypes.byref(fl),
                                 ctypes.byref(context))
    if not step("d3d11_create_device", hr == 0 and bool(device.value),
                f"hr=0x{hr & 0xFFFFFFFF:08X} fl=0x{fl.value:X}"):
        return _emit(report, steps, args.json_out, ctx)

    # 槽位健全性检查：GetFeatureLevel=[37]（SDK d3d11.h 实序第 35 个接口
    # 方法 + IUnknown 3 槽；CreateTexture2D=第 3 个 → [5]）
    proto_u = ctypes.WINFUNCTYPE(ctypes.c_uint, ctypes.c_void_p)
    got_fl = _vtbl_fn(device.value, 37, proto_u)(device.value)
    if not step("vtable_sanity_get_feature_level", got_fl == fl.value,
                f"slot37=0x{got_fl:X} expect 0x{fl.value:X}"):
        return _emit(report, steps, args.json_out, ctx)

    try:
        texture, pattern = create_d3d11_texture(device.value)
    except RuntimeError as exc:
        step("create_texture2d", False, str(exc))
        return _emit(report, steps, args.json_out, ctx)
    step("create_texture2d", True, f"BGRA8 {SIZE}x{SIZE}")

    # --- interop：Open / Register / Lock ----------------------------------
    proto_open = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)
    open_fn_ptr = gl_proc("wglDXOpenDeviceNV")
    if not step("get_proc_wglDXOpenDeviceNV", bool(open_fn_ptr)):
        return _emit(report, steps, args.json_out, ctx)
    interop = ctypes.cast(open_fn_ptr, proto_open)(device.value)
    if not step("wglDXOpenDeviceNV", bool(interop)):
        return _emit(report, steps, args.json_out, ctx)

    name = ctypes.c_uint(0)
    opengl32.glGenTextures(1, ctypes.byref(name))
    opengl32.glBindTexture(GL_TEXTURE_2D, name)
    step("gl_gen_bind_texture", True, f"name={name.value}")

    reg_fn_ptr = gl_proc("wglDXRegisterObjectNV")
    proto_reg = ctypes.WINFUNCTYPE(
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint)
    obj = None
    if not step("get_proc_wglDXRegisterObjectNV", bool(reg_fn_ptr)):
        return _emit(report, steps, args.json_out, ctx)
    obj = ctypes.cast(reg_fn_ptr, proto_reg)(
        interop, texture, name, GL_TEXTURE_2D, WGL_ACCESS_READ_ONLY_NV)
    if not step("wglDXRegisterObjectNV", bool(obj),
                "" if obj else "(driver refused registration)"):
        return _emit(report, steps, args.json_out, ctx)

    proto_lock = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                    ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
    lock_fn_ptr = gl_proc("wglDXLockObjectsNV")
    obj_ptr = ctypes.c_void_p(obj)
    locked = bool(lock_fn_ptr) and bool(ctypes.cast(lock_fn_ptr, proto_lock)(
        interop, 1, ctypes.byref(obj_ptr)))
    if not step("wglDXLockObjectsNV", locked):
        return _emit(report, steps, args.json_out, ctx)

    # --- FBO + readback roundtrip -----------------------------------------
    glGenFramebuffers = gl_proc("glGenFramebuffers")
    glBindFramebuffer = gl_proc("glBindFramebuffer")
    glFramebufferTexture2D = gl_proc("glFramebufferTexture2D")
    glCheckFramebufferStatus = gl_proc("glCheckFramebufferStatus")
    needed = {"glGenFramebuffers": glGenFramebuffers,
              "glBindFramebuffer": glBindFramebuffer,
              "glFramebufferTexture2D": glFramebufferTexture2D,
              "glCheckFramebufferStatus": glCheckFramebufferStatus}
    missing = [k for k, v in needed.items() if not v]
    if not step("gl_fbo_procs", not missing, f"missing={missing}"):
        return _emit(report, steps, args.json_out, ctx)

    fbo = ctypes.c_uint(0)
    proto_gen = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
    ctypes.cast(glGenFramebuffers, proto_gen)(1, ctypes.byref(fbo))
    proto_bind = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint)
    ctypes.cast(glBindFramebuffer, proto_bind)(GL_FRAMEBUFFER, fbo)
    proto_att = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint,
                                   ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
    ctypes.cast(glFramebufferTexture2D, proto_att)(
        GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, name, 0)
    proto_chk = ctypes.WINFUNCTYPE(ctypes.c_uint, ctypes.c_uint)
    status = ctypes.cast(glCheckFramebufferStatus, proto_chk)(GL_FRAMEBUFFER)
    if not step("fbo_attach_complete", status == GL_FRAMEBUFFER_COMPLETE,
                f"status=0x{status:X}"):
        return _emit(report, steps, args.json_out, ctx)

    buf = (ctypes.c_ubyte * (SIZE * SIZE * 4))()
    opengl32.glReadPixels(0, 0, SIZE, SIZE, 0x1908, 0x1401, buf)  # RGBA, UNSIGNED_BYTE
    read = np.frombuffer(buf, dtype=np.uint8).reshape(SIZE, SIZE, 4).copy()

    # 与 D3D 侧图案比对（GL_BGRA 源 → RGBA 读回通道序按实现；两种解释任一
    # 严格一致即 roundtrip 成立，另记录像素[0,0]供诊断）
    as_rgba = read                     # (R,G,B,A)
    d3d_bgra = pattern                 # (B,G,R,A)
    swapped = read[..., [2, 1, 0, 3]]  # 读回按 (B,G,R,A) 解释
    match_rgba = np.array_equal(as_rgba, d3d_bgra)
    match_swapped = np.array_equal(swapped, d3d_bgra)
    px00 = [int(v) for v in read[0, 0]]
    d3d00 = [int(v) for v in pattern[0, 0]]
    step("pixel_roundtrip", match_rgba or match_swapped,
         f"rgba_match={match_rgba} swapped_match={match_swapped} "
         f"gl[0,0]={px00} d3d[0,0]={d3d00}")

    # --- 反向解绑 + 清理（泄漏面收口） ------------------------------------
    proto_unlock = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                      ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
    unlock_fn = gl_proc("wglDXUnlockObjectsNV")
    unlocked = bool(unlock_fn) and bool(ctypes.cast(unlock_fn, proto_unlock)(
        interop, 1, ctypes.byref(obj_ptr)))
    step("wglDXUnlockObjectsNV", unlocked)

    proto_unreg = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
    unreg_fn = gl_proc("wglDXUnregisterObjectNV")
    unregistered = bool(unreg_fn) and bool(ctypes.cast(unreg_fn, proto_unreg)(
        interop, obj))
    step("wglDXUnregisterObjectNV", unregistered)

    proto_close = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    close_fn = gl_proc("wglDXCloseDeviceNV")
    closed = bool(close_fn) and bool(ctypes.cast(close_fn, proto_close)(interop))
    step("wglDXCloseDeviceNV", closed)

    proto_rel = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    _vtbl_fn(texture, 2, proto_rel)(texture)
    _vtbl_fn(context.value, 2, proto_rel)(context.value)
    _vtbl_fn(device.value, 2, proto_rel)(device.value)
    ctypes.cast(glBindFramebuffer, proto_bind)(GL_FRAMEBUFFER, 0)

    all_ok = all(s["ok"] for s in steps)
    return _emit(report, steps, args.json_out, ctx)


def _emit(report: dict, steps: list, json_out: str, ctx) -> int:
    report["steps"] = steps
    report["verdict"] = ("shared_texture_roundtrip_ok"
                         if all(s["ok"] for s in steps) else "incomplete")
    report["failed_at"] = next((s["step"] for s in steps if not s["ok"]), None)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if json_out:
        Path(json_out).write_text(text + "\n", encoding="utf-8")
        print(f"JSON written: {json_out}")
    print(f"\nverdict: {report['verdict']}"
          + (f" (failed at {report['failed_at']})" if report["failed_at"] else ""))
    if ctx is not None:
        try:
            ctx.release()
        except Exception:
            pass
    return 0 if report["verdict"].endswith("_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
