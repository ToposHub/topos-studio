"""宿主 GPU 事实探测（纯标准库，无 Qt/GL 依赖）。

T-P0-00（单一能力状态模型）与 X-TEST-00（素材/环境冻结）共用的事实源：
通过 ``system_profiler`` 读取显示设备与 Metal 支持信息，不做任何 GPU
context 创建。所有失败路径都返回稳定 ``reason_code``，不静默跳过。

返回结构（JSON-safe）::

    {
        "status": "AVAILABLE" | "UNAVAILABLE",
        "reason_code": None | str,
        "detail": None | str,
        "devices": [
            {"model": ..., "vendor": ..., "vram": ..., "cores": ...,
             "metal_support": ...},
        ],
    }

线程边界：无任何共享状态、无缓存，可任意线程调用；但每次调用都会
spawn ``system_profiler`` 子进程并阻塞至多 ``timeout_s`` 秒（实测通常
数秒），生产路径应只在启动探测或诊断时调用，不要进热循环。
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Dict, List

__all__ = ["detect_host_gpu_info", "metal_info_from_gpu"]


def detect_host_gpu_info(timeout_s: float = 30.0) -> Dict[str, Any]:
    """macOS 下经 system_profiler 采集显示设备信息；失败带 reason_code。"""
    if sys.platform != "darwin":
        return {
            "status": "UNAVAILABLE",
            "reason_code": "GPU_INFO_PLATFORM_NOT_MACOS",
            "detail": f"system_profiler 仅在 macOS 可用（platform={sys.platform}）",
            "devices": [],
        }
    try:
        out = subprocess.run(
            ["system_profiler", "-json", "SPDisplaysDataType"],
            capture_output=True, timeout=timeout_s, check=True,
        )
        data = json.loads(out.stdout.decode("utf-8", "replace"))
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        return {
            "status": "UNAVAILABLE",
            "reason_code": "GPU_INFO_SYSTEM_PROFILER_FAILED",
            "detail": str(exc),
            "devices": [],
        }
    devices: List[Dict[str, Any]] = []
    for gpu in data.get("SPDisplaysDataType", []) or []:
        # Apple Silicon 报 spdisplays_metal；AMD/Intel 报 spdisplays_mtlgpufamilysupport
        metal = (gpu.get("spdisplays_metal")
                 or gpu.get("spdisplays_mtlgpufamilysupport"))
        devices.append({
            "model": gpu.get("sppci_model"),
            "vendor": gpu.get("spdisplays_vendor"),
            "vram": gpu.get("spdisplays_vram_shared") or gpu.get("spdisplays_vram"),
            "cores": gpu.get("spdisplays_cores"),
            "metal_support": metal,
        })
    if not devices:
        return {
            "status": "UNAVAILABLE",
            "reason_code": "GPU_INFO_EMPTY",
            "detail": "system_profiler 未返回显示设备",
            "devices": [],
        }
    return {"status": "AVAILABLE", "reason_code": None, "detail": None,
            "devices": devices}


def metal_info_from_gpu(gpu: Dict[str, Any]) -> Dict[str, Any]:
    """从 GPU 设备信息推导 Metal 支持；GPU 信息缺失时带 reason_code。"""
    if gpu.get("status") != "AVAILABLE":
        return {
            "status": "UNAVAILABLE",
            "reason_code": "METAL_INFO_REQUIRES_GPU_INFO",
            "detail": None,
        }
    metals = sorted({str(d.get("metal_support")) for d in gpu["devices"]
                     if d.get("metal_support")})
    if not metals:
        return {"status": "UNAVAILABLE",
                "reason_code": "METAL_SUPPORT_NOT_REPORTED", "detail": None}
    return {"status": "AVAILABLE", "reason_code": None,
            "detail": ", ".join(metals)}
