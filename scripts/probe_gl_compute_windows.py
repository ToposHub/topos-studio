#!/usr/bin/env python3
"""W4-01：GL compute 现状确认（应用形态 Qt 上下文，真机实测）。

计划 W4-01 验收标准：blur/erode_dilate/histogram 在 Windows GL 4.6 上
不触发 fragment 回退。compute_shader_manager 的门控是 GL >= 4.3，而
W1-02 的 standalone 上下文只拿到 3.3 Core——本脚本用 **应用同形态的
Qt QSurfaceFormat(4.5 Core) + QOffscreenSurface** 实测：

1. Qt 请求 4.5 Core 后实际拿到的 GL 版本（开放问题 3 的应用侧答案）
2. GL_COMPUTE_SHADER 编译 + program link（含 SSBO 声明）
3. glDispatchCompute 函数指针存在性

任何一级失败都如实落 reason；compute 不可用则 W4-01 结论为
「fragment 回退路径在本机成立」（即 W4-02/W4-03 的对照基线）。

用法：
    .venv/Scripts/python.exe scripts/probe_gl_compute_windows.py
    .venv/Scripts/python.exe scripts/probe_gl_compute_windows.py --json out.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GL_COMPUTE_SHADER = 0x91D9
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82
GL_VERSION = 0x1F02
GL_RENDERER = 0x1F01

# #version 必须位于首行（不留前导空行；早期探针曾以 \n 开头）
COMPUTE_SRC = (
    b"#version 430\n"
    b"layout(local_size_x = 8, local_size_y = 8) in;\n"
    b"layout(std430, binding = 0) buffer Data { float values[]; };\n"
    b"void main() {\n"
    b"    uint idx = gl_GlobalInvocationID.y * 64u + gl_GlobalInvocationID.x;\n"
    b"    values[idx] = values[idx] * 2.0;\n"
    b"}\n"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_out", default="", help="可选 JSON 输出路径")
    args = parser.parse_args()

    report: dict = {"date": datetime.now().isoformat(timespec="seconds"),
                    "purpose": "W4-01 GL compute probe (Qt offscreen 4.5 Core)"}
    steps: list[dict] = []

    def step(name: str, ok: bool, detail: str = "") -> bool:
        steps.append({"step": name, "ok": ok, "detail": detail})
        print(f"[{'OK' if ok else 'FAIL'}] {name} {detail}")
        return ok

    # --- Qt offscreen context（应用同形态） --------------------------------
    try:
        from PyQt6.QtGui import QGuiApplication, QOpenGLContext, QSurfaceFormat
        from PyQt6.QtGui import QOffscreenSurface

        app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])

        fmt = QSurfaceFormat()
        fmt.setVersion(4, 5)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setDepthBufferSize(24)
        QSurfaceFormat.setDefaultFormat(fmt)

        surface = QOffscreenSurface()
        surface.setFormat(fmt)
        surface.create()

        context = QOpenGLContext()
        context.setFormat(fmt)
        context.create()
        current = context.makeCurrent(surface)
        got = context.format()
        version_str = f"{got.majorVersion()}.{got.minorVersion()}"
        step("qt_context_current", current,
             f"requested=4.5 Core got={version_str} "
             f"profile={'Core' if got.profile() == QSurfaceFormat.OpenGLContextProfile.CoreProfile else got.profile()}")
        report["gl_context"] = {
            "requested": "4.5 Core",
            "got": version_str,
            "surface": "QOffscreenSurface",
        }
    except Exception as exc:
        step("qt_context_current", False, f"{type(exc).__name__}: {exc}")
        report["steps"] = steps
        report["verdict"] = "inconclusive_no_qt_context"
        _emit(report, args.json_out)
        return 1

    # --- 原始 GL 探测（上下文已在当前线程） --------------------------------
    # 函数指针优先经 QOpenGLContext.getProcAddress 解析（与上下文实际所属
    # 的 GL 模块一致——offscreen/软件 GL 场景下系统 opengl32 不可见）；
    # 系统 opengl32 仅作 GL 1.x 直呼与回退。
    try:
        context.getProcAddress  # noqa: B018 — 存在性探测
        _ctx_get_proc = context.getProcAddress
    except AttributeError:
        _ctx_get_proc = None

    def get_proc(name: bytes) -> int:
        if _ctx_get_proc is not None:
            try:
                ptr = _ctx_get_proc(name)
                addr = int(ptr) if ptr is not None else 0
                if addr:
                    return addr
            except Exception:
                pass
        return opengl32.wglGetProcAddress(name) or 0

    opengl32 = ctypes.CDLL("opengl32.dll")

    def _gl_string(enum: int) -> str:
        # 先经上下文模块，再退回系统 opengl32
        for proc in (get_proc(b"glGetString"),
                     getattr(opengl32, "glGetString", None)):
            if not proc:
                continue
            try:
                proto = ctypes.WINFUNCTYPE(ctypes.c_char_p, ctypes.c_uint)
                raw = ctypes.cast(proc, proto)(enum) if callable(proc) else proc(enum)
                if raw:
                    return raw.decode(errors="replace")
            except Exception:
                continue
        return "?"

    gl_version = _gl_string(GL_VERSION)
    gl_renderer = _gl_string(GL_RENDERER)
    report["gl_strings"] = {"GL_VERSION": gl_version, "GL_RENDERER": gl_renderer}
    step("gl_strings", gl_version != "?", f"{gl_renderer} | {gl_version}")

    # compute 入口函数（4.3+；IUnknown 式经 GetProcAddress 获取）
    proc_names = [b"glCreateShader", b"glShaderSource", b"glCompileShader",
                  b"glGetShaderiv", b"glCreateProgram", b"glAttachShader",
                  b"glLinkProgram", b"glGetProgramiv", b"glDispatchCompute",
                  b"glGenBuffers"]
    procs = {n.decode(): get_proc(n) for n in proc_names}
    missing = [k for k, v in procs.items() if not v]
    step("compute_fn_ptrs", not missing, f"missing={missing}" if missing else "all present")
    if missing:
        report["steps"] = steps
        report["verdict"] = "compute_unavailable_fn_ptrs_absent"
        _emit(report, args.json_out)
        return 1

    # --- compute shader 编译 + link ---------------------------------------
    proto_create = ctypes.WINFUNCTYPE(ctypes.c_uint, ctypes.c_uint)
    proto_src = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_char_p),
                                   ctypes.POINTER(ctypes.c_int))
    proto_compile = ctypes.WINFUNCTYPE(None, ctypes.c_uint)
    proto_getiv = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint,
                                     ctypes.POINTER(ctypes.c_int))

    glCreateShader = ctypes.cast(procs["glCreateShader"], proto_create)
    glShaderSource = ctypes.cast(procs["glShaderSource"], proto_src)
    glCompileShader = ctypes.cast(procs["glCompileShader"], proto_compile)
    glGetShaderiv = ctypes.cast(procs["glGetShaderiv"], proto_getiv)
    glCreateProgram = ctypes.cast(procs["glCreateProgram"], proto_create)
    proto_attach = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint)
    glAttachShader = ctypes.cast(procs["glAttachShader"], proto_attach)
    glLinkProgram = ctypes.cast(procs["glLinkProgram"], proto_compile)
    glGetProgramiv = ctypes.cast(procs["glGetProgramiv"], proto_getiv)

    # 区分 create 失败与 compile 失败：glCreateShader 失败返回 0 并置
    # GL_INVALID_ENUM——不检查会把 create 失败误报成 "compile failed
    # (empty log)"（W4-01 NVIDIA 复测修正）。
    glGetError = ctypes.cast(
        get_proc(b"glGetError"), ctypes.WINFUNCTYPE(ctypes.c_uint))
    gl_error_before = glGetError()
    shader = glCreateShader(GL_COMPUTE_SHADER)
    gl_error_after = glGetError()
    err_name = {0x0500: "GL_INVALID_ENUM", 0x0502: "GL_INVALID_OPERATION",
                }.get(gl_error_after, f"0x{gl_error_after:04X}")
    report["create_shader"] = {
        "shader_handle": int(shader),
        "gl_error_before": f"0x{gl_error_before:04X}",
        "gl_error_after": f"0x{gl_error_after:04X}",
    }
    if not step("create_shader_handle", bool(shader),
                f"handle={shader} err_after={err_name}"):
        # Qt 形态 create 失败 ≠ 驱动不支持——用原生 WGL_ARB_create_context
        # 多版本 Core 上下文交叉验证（排除 Qt/ICD 分派形态因素，W4-01
        # NVIDIA 复测修正：先前的 create 失败可能被误报成 compile 失败）
        report["native_wgl_cross_check"] = _native_wgl_cross_check()
        all_native_fail = bool(report["native_wgl_cross_check"]) and all(
            not r.get("compute_handle")
            for r in report["native_wgl_cross_check"])
        report["steps"] = steps
        report["verdict"] = (
            "compute_unavailable_native_wgl_confirmed" if all_native_fail
            else "compute_unavailable_create_shader_failed")
        _emit(report, args.json_out)
        return 1
    src_buf = ctypes.c_char_p(COMPUTE_SRC)
    src_len = ctypes.c_int(len(COMPUTE_SRC))
    glShaderSource(shader, 1, ctypes.byref(src_buf), ctypes.byref(src_len))
    glCompileShader(shader)
    status = ctypes.c_int(0)
    glGetShaderiv(shader, GL_COMPILE_STATUS, ctypes.byref(status))

    log_text = ""
    try:
        glGetShaderInfoLog = ctypes.cast(get_proc(b"glGetShaderInfoLog"),
            ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_int,
                               ctypes.POINTER(ctypes.c_int), ctypes.c_char_p))
        log_buf = ctypes.create_string_buffer(4096)
        log_len = ctypes.c_int(0)
        glGetShaderInfoLog(shader, 4096, ctypes.byref(log_len), log_buf)
        log_text = log_buf.value.decode(errors="replace")
    except Exception as exc:
        log_text = f"(info log unavailable: {exc})"
    if not step("compute_shader_compile", bool(status.value),
                log_text[:300] or "(empty log)"):
        report["steps"] = steps
        report["verdict"] = "compute_unavailable_shader_compile_failed"
        _emit(report, args.json_out)
        return 1

    program = glCreateProgram()
    glAttachShader(program, shader)
    glLinkProgram(program)
    link_status = ctypes.c_int(0)
    glGetProgramiv(program, GL_LINK_STATUS, ctypes.byref(link_status))
    step("compute_program_link", bool(link_status.value))

    verdict = ("gl_compute_available"
               if status.value and link_status.value
               else "compute_unavailable")
    report["verdict"] = verdict
    if verdict == "gl_compute_available":
        report["implication"] = (
            "compute_shader_manager 的 GL>=4.3 门在本机应用形态上下文成立；"
            "blur/erode_dilate/histogram 的 GL compute 路径不触发 fragment 回退"
            "（W4-01 验收达成，AMD 行）。CUDA/DirectML 仅在 A/B >=15% 收益或"
            "承载 ONNX 模型时立项（K-W4）。")
    _emit(report, args.json_out)
    context.doneCurrent()
    return 0 if verdict == "gl_compute_available" else 1


def _native_wgl_cross_check() -> list[dict]:
    """原生 WGL_ARB_create_context 多版本 Core 上下文的 compute 交叉验证。

    与 Qt 形态独立（直接 D3D 窗口 + 硬件 pixel format + ICD 上下文），
    用于区分「Qt/上下文形态问题」vs「驱动/会话环境裁剪 compute」。
    64 位句柄必须显式 c_void_p（默认 c_int 截断高 32 位会造成
    wglMakeCurrent 对错句柄「成功」的非确定性退化）。
    """
    results: list[dict] = []
    try:
        import ctypes.wintypes as wt

        user32, gdi32, opengl32 = (ctypes.windll.user32,
                                   ctypes.windll.gdi32,
                                   ctypes.windll.opengl32)
        kernel32 = ctypes.windll.kernel32

        class _WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wt.UINT),
                        ("lpfnWndProc", ctypes.c_void_p),
                        ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int),
                        ("hInstance", ctypes.c_void_p),
                        ("hIcon", ctypes.c_void_p),
                        ("hCursor", ctypes.c_void_p),
                        ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", ctypes.c_wchar_p),
                        ("lpszClassName", ctypes.c_wchar_p)]

        class _PFD(ctypes.Structure):
            _fields_ = [("nSize", wt.WORD), ("nVersion", wt.WORD),
                        ("dwFlags", wt.DWORD), ("iPixelType", ctypes.c_ubyte),
                        ("cColorBits", ctypes.c_ubyte),
                        ("cRedBits", ctypes.c_ubyte),
                        ("cRedShift", ctypes.c_ubyte),
                        ("cGreenBits", ctypes.c_ubyte),
                        ("cGreenShift", ctypes.c_ubyte),
                        ("cBlueBits", ctypes.c_ubyte),
                        ("cBlueShift", ctypes.c_ubyte),
                        ("cAlphaBits", ctypes.c_ubyte),
                        ("cAlphaShift", ctypes.c_ubyte),
                        ("cAccumBits", ctypes.c_ubyte),
                        ("cAccumRedBits", ctypes.c_ubyte),
                        ("cAccumGreenBits", ctypes.c_ubyte),
                        ("cAccumBlueBits", ctypes.c_ubyte),
                        ("cAccumAlphaBits", ctypes.c_ubyte),
                        ("cDepthBits", ctypes.c_ubyte),
                        ("cStencilBits", ctypes.c_ubyte),
                        ("cAuxBuffers", ctypes.c_ubyte),
                        ("iLayerType", ctypes.c_ubyte),
                        ("bReserved", ctypes.c_ubyte),
                        ("dwLayerMask", wt.DWORD),
                        ("dwVisibleMask", wt.DWORD),
                        ("dwDamageMask", wt.DWORD)]

        _CWEX = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                 ctypes.c_void_p]
        for api, attr, rt, at in (
                (user32, "GetDC", ctypes.c_void_p, [ctypes.c_void_p]),
                (user32, "CreateWindowExW", ctypes.c_void_p, _CWEX),
                (opengl32, "wglCreateContext", ctypes.c_void_p,
                 [ctypes.c_void_p]),
                (opengl32, "wglMakeCurrent", ctypes.c_int,
                 [ctypes.c_void_p, ctypes.c_void_p]),
                (opengl32, "wglDeleteContext", None, [ctypes.c_void_p]),
                (gdi32, "SetPixelFormat", ctypes.c_int,
                 [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]),
                (gdi32, "ChoosePixelFormat", ctypes.c_int,
                 [ctypes.c_void_p, ctypes.c_void_p]),
                (gdi32, "DescribePixelFormat", ctypes.c_int,
                 [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                  ctypes.c_void_p]),
                (kernel32, "GetModuleHandleW", ctypes.c_void_p,
                 [ctypes.c_wchar_p]),
                (user32, "UnregisterClassW", None,
                 [ctypes.c_wchar_p, ctypes.c_void_p])):
            fn = getattr(api, attr)
            fn.restype = rt
            if at:
                fn.argtypes = at

        wnd_proc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, wt.UINT, wt.UINT, ctypes.c_int)(
            lambda *a: 1)
        hinst = kernel32.GetModuleHandleW(None)
        cls = f"glcompute_xcheck_{os.getpid()}"
        wc = _WNDCLASS(0, ctypes.cast(wnd_proc, ctypes.c_void_p).value,
                       0, 0, hinst, None, None, None, None, cls)
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            return [{"error": "RegisterClassW failed"}]
        hwnd = user32.CreateWindowExW(0, atom, "p", 0xCF0000, 0, 0, 64, 64,
                                      None, None, hinst, None)
        if not hwnd:
            return [{"error": "CreateWindowExW failed"}]
        hdc = user32.GetDC(hwnd)

        pfd = _PFD()
        pfd.nSize, pfd.nVersion = 40, 1
        pfd.dwFlags = 0x2028 | 0x20 | 0x8  # SUPPORT_OPENGL|DOUBLEBUFFER|DRAW_TO_WINDOW
        pfd.iPixelType, pfd.cColorBits, pfd.cDepthBits, pfd.iLayerType = 1, 32, 24, 0
        # ChoosePixelFormat 可能返回软件/GDI 格式——枚举找非 GENERIC 硬件格式
        pf = gdi32.ChoosePixelFormat(hdc, ctypes.byref(pfd))
        total = gdi32.DescribePixelFormat(hdc, 1, 0, None)
        for i in range(1, (total or 0) + 1):
            d = _PFD()
            if not gdi32.DescribePixelFormat(hdc, i, ctypes.sizeof(d),
                                             ctypes.byref(d)):
                continue
            if (d.dwFlags & 0x1) or not (d.dwFlags & 0x4):
                continue  # PFD_GENERIC_FORMAT / 非 SUPPORT_OPENGL
            if d.cColorBits >= 24 and d.cDepthBits >= 24:
                pf = i
                break
        gdi32.SetPixelFormat(hdc, pf, ctypes.byref(pfd))

        opengl32.wglGetProcAddress.restype = ctypes.c_void_p
        opengl32.wglGetProcAddress.argtypes = [ctypes.c_char_p]
        proto_create = ctypes.WINFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int))
        wgl_create_attribs = ctypes.cast(
            opengl32.wglGetProcAddress(b"wglCreateContextAttribsARB"),
            proto_create)
        _gs = ctypes.cast(
            opengl32.glGetString,
            ctypes.WINFUNCTYPE(ctypes.c_char_p, ctypes.c_uint))
        _gge = ctypes.cast(
            opengl32.glGetError, ctypes.WINFUNCTYPE(ctypes.c_uint))
        _create_shader = ctypes.cast(
            opengl32.wglGetProcAddress(b"glCreateShader"),
            ctypes.WINFUNCTYPE(ctypes.c_uint, ctypes.c_uint))

        legacy = opengl32.wglCreateContext(hdc)
        if legacy and opengl32.wglMakeCurrent(hdc, legacy):
            v = _gs(0x1F02)
            results.append({"ctx": "legacy", "version": v.decode() if v else "?"})
            opengl32.wglMakeCurrent(None, None)
        opengl32.wglDeleteContext(legacy)

        for major, minor in ((4, 6), (4, 5), (4, 3)):
            attribs = (ctypes.c_int * 7)(
                0x2091, major, 0x2092, minor, 0x9126, 1, 0)
            ctx = wgl_create_attribs(hdc, None, attribs)
            if not ctx:
                results.append({"ctx": f"{major}.{minor} Core",
                                "create_failed": True})
                continue
            if not opengl32.wglMakeCurrent(hdc, ctx):
                results.append({"ctx": f"{major}.{minor} Core",
                                "make_current_failed": True})
                opengl32.wglDeleteContext(ctx)
                continue
            v = _gs(0x1F02)
            _gge()
            handle = _create_shader(0x91D9)
            err = _gge()
            results.append({
                "ctx": f"{major}.{minor} Core",
                "version": v.decode() if v else "?",
                "compute_handle": int(handle),
                "gl_error": f"0x{err:04X}" +
                            (" (GL_INVALID_ENUM)" if err == 0x0500 else ""),
            })
            opengl32.wglMakeCurrent(None, None)
            opengl32.wglDeleteContext(ctx)

        user32.ReleaseDC(hwnd, hdc)
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(cls, hinst)
    except Exception as exc:  # noqa: BLE001
        results.append({"error": f"{type(exc).__name__}: {exc}"})
    return results


def _emit(report: dict, json_out: str) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if json_out:
        Path(json_out).write_text(text + "\n", encoding="utf-8")
        print(f"JSON written: {json_out}")
    print(f"\nverdict: {report['verdict']}")


if __name__ == "__main__":
    raise SystemExit(main())
