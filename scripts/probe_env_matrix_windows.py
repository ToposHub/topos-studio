"""B0.1 环境兼容矩阵实录（计划表 B · Windows 线，docs/perf_plan_B_windows_2026-10-22.md）。

一次性采集本机可复现环境记录，供跨机/跨版本分层归档：

- host / python / 关键包版本（av 含 FFmpeg 库位版本、moderngl、Qt、numpy）
- git manifest（commit/branch/dirty，产物与代码对齐）
- GPU 适配器（LUID/驱动/显存，经生产 gpu_capability 探测）+ nvidia-smi（可选）
- 设备 API：D3D11 device 创建 + NV12/P010/BGRA CheckFormatSupport、
  ModernGL 独立上下文、PyAV HWDeviceType 枚举
- 原生扩展：load_windows_native_bridge() 实查（不靠测试替身推断）
- 实际加载库：进程内 DLL 清单（avcodec/Qt/OpenGL/d3d11 等，路径+大小）
- 依赖检查：pip freeze 与 constraints.txt 逐项对拍

只采集、不切换环境、不修改任何系统状态。采集失败计入 issues 并以
退出码 1 结束（结构化 fail-closed，不静默缺项）。

用法：
    .venv/Scripts/python.exe scripts/probe_env_matrix_windows.py --tag B_20261024_run01
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import av  # noqa: E402

RESULT: dict = {"schema": "perf_env_matrix_windows/1", "date": datetime.now().isoformat(timespec="seconds")}
ISSUES: list[str] = []


def _add(key: str, value) -> None:
    RESULT[key] = value


def _fail(context: str, exc: Exception) -> None:
    ISSUES.append(f"{context}: {type(exc).__name__}: {exc}")
    RESULT.setdefault("errors", {})[context] = f"{type(exc).__name__}: {exc}"


def _git(args: list[str]) -> str:
    out = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def collect_host() -> None:
    cpu = "unknown"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            cpu = str(winreg.QueryValueEx(key, "ProcessorNameString")[0])
    except Exception:
        cpu = platform.processor() or "unknown"
    _add("host", {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()} build {platform.version().split('.')[-1]}",
        "cpu": cpu,
        "logical_cores": os.cpu_count(),
    })


def collect_python() -> None:
    _add("python", {
        "version": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "venv": str(Path(sys.executable).parent.parent) if hasattr(sys, "prefix") else None,
    })


def collect_packages() -> None:
    import importlib.metadata as md

    def pkg(name: str) -> str:
        try:
            return md.version(name)
        except Exception:
            return "not-installed"

    libs = {k: ".".join(str(x) for x in v) for k, v in av.library_versions.items()}
    import moderngl  # noqa: F401  ← 加载时刻即被计入 loaded_libraries
    import PyQt6.QtCore as qc

    _add("packages", {
        "av": av.__version__,
        "ffmpeg_libraries": libs,
        "moderngl": moderngl.__version__,
        "PyQt6": pkg("PyQt6"),
        "PyQt6_Qt6": pkg("PyQt6-Qt6"),
        "qt_runtime": qc.QT_VERSION_STR,
        "numpy": pkg("numpy"),
        "opencv-python": pkg("opencv-python"),
        "opencolorio": pkg("opencolorio"),
    })


def collect_git_manifest() -> None:
    dirty = _git(["status", "--porcelain"])
    _add("git_manifest", {
        "commit": _git(["rev-parse", "HEAD"]),
        "branch": _git(["branch", "--show-current"]),
        "dirty_files": len(dirty.splitlines()) if dirty else 0,
    })


def collect_gpu() -> None:
    try:
        from src.shared.media.windows_hw.gpu_capability import probe_gpu_capability

        g = probe_gpu_capability()
        _add("gpu", {
            "probe_ok": g.probe_ok,
            "reason": g.reason,
            "driver_version": g.driver_version,
            "primary_vendor": g.primary_vendor,
            "adapters": [
                {
                    "description": a.description,
                    "vendor": a.vendor_name,
                    "vendor_id": a.vendor_id,
                    "device_id": a.device_id,
                    "luid": a.adapter_luid,
                    "dedicated_video_memory_bytes": a.dedicated_video_memory,
                }
                for a in g.adapters
            ],
        })
    except Exception as exc:
        _fail("gpu_capability", exc)

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            _add("nvidia_smi", out.stdout.strip())
    except Exception:
        pass  # 无 NVIDIA 卡/无 nvidia-smi 的机器属正常缺项，不算 issue


def collect_device_api() -> None:
    d3d = {}
    try:
        from src.shared.media.windows_hw.d3d11_device import (
            DXGI_FORMAT_B8G8R8A8_UNORM,
            DXGI_FORMAT_NV12,
            DXGI_FORMAT_P010,
            check_format_support,
            create_d3d11_device,
        )

        device, reason = create_d3d11_device()
        if device is None:
            d3d["create"] = {"ok": False, "reason": reason}
            ISSUES.append(f"d3d11_create: {reason}")
        else:
            ident = device.identity
            fmts = {}
            for label, fmt in (("NV12", DXGI_FORMAT_NV12),
                               ("P010", DXGI_FORMAT_P010),
                               ("BGRA", DXGI_FORMAT_B8G8R8A8_UNORM)):
                ok, bits, why = check_format_support(device, fmt)
                fmts[label] = {"ok": ok, "support_bits": bits, "reason": why}
                if not ok:
                    ISSUES.append(f"d3d11_format_{label}: {why}")
            d3d["create"] = {
                "ok": True,
                "feature_level": f"0x{ident.feature_level:X}",
                "adapter_luid": ident.adapter_luid,
                "adapter_description": ident.adapter_description,
                "vendor": ident.vendor_name,
            }
            d3d["format_support"] = fmts
            device.release()
    except Exception as exc:
        _fail("d3d11_device", exc)
    _add("d3d11", d3d)

    try:
        import moderngl

        ctx = moderngl.create_standalone_context()
        info = ctx.info
        _add("opengl", {
            "version": info.get("GL_VERSION"),
            "renderer": info.get("GL_RENDERER"),
            "vendor": info.get("GL_VENDOR"),
        })
        ctx.release()
    except Exception as exc:
        _fail("moderngl_context", exc)

    try:
        from av.codec.hwaccel import HWDeviceType

        _add("pyav_hw_device_types", [m.name for m in HWDeviceType])
    except Exception as exc:
        _fail("pyav_hw_device_types", exc)


def collect_native_bridge() -> None:
    try:
        from src.shared.media.windows_hw.native_bridge import (
            load_windows_native_bridge,
        )

        r = load_windows_native_bridge()
        entry = {
            "available": bool(r.available),
            "reason": r.reason,
            "capabilities": None,
        }
        if r.capabilities is not None:
            caps = r.capabilities
            entry["capabilities"] = {
                k: getattr(caps, k)
                for k in dir(caps)
                if not k.startswith("_") and isinstance(getattr(caps, k), (bool, int, str))
            }
        # 扩展包残留检测：site-packages 里是否有 topos_hwdecode_windows*
        import site

        found: list[str] = []
        for sp in site.getsitepackages() + [site.getusersitepackages()]:
            p = Path(sp)
            if p.is_dir():
                found += [x.name for x in p.glob("topos_hwdecode_windows*")]
        entry["site_packages_hits"] = sorted(set(found))
        _add("native_bridge", entry)
        if not r.available:
            # 扩展未安装是当前机器的既定事实（记录，不算采集失败），
            # 但必须出现在台账里供 B1.1 路线决策使用。
            RESULT.setdefault("notes", []).append(
                f"native bridge unavailable on this host: {r.reason}"
            )
    except Exception as exc:
        _fail("native_bridge", exc)


def collect_loaded_libraries() -> None:
    """进程内实际加载的媒体/GPU 相关 DLL（计划 §B0.1「实际加载库」）。"""
    try:
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class DWORD(ctypes.c_ulong):
            pass

        psapi.EnumProcessModulesEx.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), DWORD,
            ctypes.POINTER(DWORD), DWORD,
        ]
        psapi.GetModuleFileNameExW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, DWORD,
        ]
        LIST_MODULES_ALL = 0x03
        h = kernel32.GetCurrentProcess()
        needed = DWORD(0)
        psapi.EnumProcessModulesEx(h, None, 0, ctypes.byref(needed), LIST_MODULES_ALL)
        count = needed.value // ctypes.sizeof(ctypes.c_void_p)
        mods = (ctypes.c_void_p * count)()
        if not psapi.EnumProcessModulesEx(
            h, mods, ctypes.sizeof(mods), ctypes.byref(needed), LIST_MODULES_ALL,
        ):
            raise OSError(f"EnumProcessModulesEx failed err={ctypes.get_last_error()}")
        buf = ctypes.create_unicode_buffer(1024)
        keys = ("avcodec", "avformat", "avutil", "swscale", "swresample",
                "Qt6", "opengl", "nvogl", "d3d11", "dxgi", "topos")
        hits = []
        for m in mods:
            if not m:
                continue
            if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(m), buf, 1024):
                name = buf.value
                low = name.lower()
                if any(k.lower() in low for k in keys):
                    try:
                        size = Path(name).stat().st_size
                    except OSError:
                        size = None
                    hits.append({"path": name, "size_bytes": size})
        _add("loaded_libraries", sorted(hits, key=lambda x: x["path"].lower()))
    except Exception as exc:
        _fail("loaded_libraries", exc)


def collect_deps_check() -> None:
    """pip freeze 与 constraints.txt 对拍（B0.1 依赖检查）。"""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "freeze", "--exclude-editable"],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        installed: dict[str, str] = {}
        for line in out.stdout.splitlines():
            line = line.strip()
            if "==" in line:
                name, _, ver = line.partition("==")
                installed[name.lower().replace("_", "-")] = ver
        constraints_path = PROJECT_ROOT / "constraints.txt"
        pins: dict[str, str] = {}
        if constraints_path.exists():
            for line in constraints_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "==" in line:
                    name, _, ver = line.partition("==")
                    pins[name.lower().replace("_", "-")] = ver
        mismatches, unpinned = [], []
        for name, want in sorted(pins.items()):
            got = installed.get(name)
            if got is None:
                mismatches.append({"package": name, "constraints": want, "installed": None})
            elif got != want:
                mismatches.append({"package": name, "constraints": want, "installed": got})
        for name in sorted(set(installed) - set(pins)):
            unpinned.append({"package": name, "installed": installed[name]})
        _add("deps_check", {
            "constraints_pins": len(pins),
            "mismatches": mismatches,
            "extra_installed_not_in_constraints": unpinned,
        })
        if mismatches:
            ISSUES.append(f"deps_check: {len(mismatches)} pin mismatch(es)")
    except Exception as exc:
        _fail("deps_check", exc)


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="", help="产物分册后缀（如 B_20261024_run01）")
    args = parser.parse_args()

    collect_host()
    collect_python()
    collect_packages()
    collect_git_manifest()
    collect_gpu()
    collect_device_api()
    collect_native_bridge()
    collect_loaded_libraries()
    collect_deps_check()

    RESULT["issues"] = ISSUES
    RESULT["ok"] = not ISSUES

    out = PROJECT_ROOT / "scripts" / (
        f"env_matrix_windows_{args.tag}.json" if args.tag
        else "env_matrix_windows.json"
    )
    out.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(RESULT, ensure_ascii=False, indent=2))
    print(f"\n结果已保存: {out}")
    print(f"issues={len(ISSUES)}")
    return 0 if not ISSUES else 1


if __name__ == "__main__":
    raise SystemExit(main())
