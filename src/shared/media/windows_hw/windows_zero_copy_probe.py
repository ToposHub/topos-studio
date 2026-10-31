"""Windows 零拷贝分层探测（计划 2026-09-15 §6 步骤 2 接入点）。

2026-09-15 起从"整体 not_yet_implemented 骨架"升级为**分层真实探测**：

1. device 层（真实）：D3D11CreateDevice + adapter LUID 走查；
2. 纹理池层（真实）：NV12 定长池创建 + 短 lease 循环；
3. 解码 surface 层（fail-closed）：`D3D11Decoder.open()` 仍不可用——
   MF D3D11VA 原生桥落地前如实记录 `not_yet_implemented`。

因此结果的语义是"probe 已分层完成 + 哪一层是当前瓶颈"，而不是整体的
不可用。`zero_copy` 仍然只有在解码 surface 层真实可用后才可能为 True
（绝不伪报硬解或全 GPU，计划 §4.3 回退透明度门槛）。
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass, field
from typing import Dict

from .d3d11_device import (
    DXGI_FORMAT_NV12,
    create_d3d11_device,
    check_format_support,
)
from .d3d11_decoder import D3D11Decoder
from .d3d11_texture_pool import create_d3d11_texture_pool
from .reasons import FallbackReason, param

logger = logging.getLogger(__name__)

#: AUD-36203：dxgiformat.h 中 ``DXGI_FORMAT_NV12`` 的真值（103）。包内
#: ``d3d11_device`` 的同名常量历史错值 65 实为 ``A8_UNORM``（根因已
#: 修正为 103；本门禁初版曾误写 77，实为 ``BC3_UNORM``）。探测层把
#: 格式码传给 CheckFormatSupport / 纹理池，必须钉死真值做一致性门禁：
#: 包常量与 dxgiformat.h 不符时显式 fail-closed，绝不把 A8 的支持
#: 证据误报为 NV12 支持（分层证据失真 + 误放行）。
DXGI_FORMAT_NV12_ABI_TRUTH = 103


@dataclass
class WindowsZeroCopyProbeResult:
    """Windows 零拷贝 probe 结果"""
    probe_success: bool = False
    platform: str = ""
    d3d11_decoder_available: bool = False
    d3d_adapter_matches_gl_gpu: bool = False
    cpu_download_frames: int = 0
    cpu_upload_frames: int = 0
    texture_write_frames: int = 0
    decode_path: str = ""
    zero_copy: bool = False
    interop_method: str = ""
    fallback_reason: str | None = None
    frames_tested: int = 0
    #: 分层证据（2026-09-15 步骤 1/2 起）：device/纹理池层真实探测结果。
    layers: Dict[str, object] = field(default_factory=dict)


def run_probe(file_path: str = "", frames: int = 120) -> WindowsZeroCopyProbeResult:
    """运行 Windows 零拷贝分层 probe。

    - 非 Windows：``not_windows``（与既有语义一致）；
    - Windows：真实执行 device / 格式支持 / 纹理池层探测，解码 surface
      层按 D3D11Decoder 的真实 open 结果记录；第一层失败即在该层
      fail-closed（reason 词表来自 .reasons）。
    """
    result = WindowsZeroCopyProbeResult(platform=platform.system())

    if platform.system() != 'Windows':
        result.fallback_reason = FallbackReason.NOT_WINDOWS.value
        return result

    # --- 1. device 层 ------------------------------------------------------
    device, reason = create_d3d11_device()
    if device is None:
        result.fallback_reason = f"device_layer:{reason}"
        result.layers = {"device": {"ok": False, "reason": reason}}
        return result
    try:
        result.layers["device"] = {
            "ok": True,
            "adapter_luid": device.identity.adapter_luid,
            "feature_level": hex(device.identity.feature_level),
            "adapter_description": device.identity.adapter_description,
        }

        # --- 2. 格式支持层 -------------------------------------------------
        # AUD-36203：先校验包常量与 dxgiformat.h 真值一致，再做真实
        # CheckFormatSupport——错值（65=A8_UNORM）会把"检测到的其实是
        # A8 支持"记入 NV12 层证据并可能误放行。
        if DXGI_FORMAT_NV12 != DXGI_FORMAT_NV12_ABI_TRUTH:
            reason = param(
                FallbackReason.GPU_PROBE_FAILED,
                f"dxgi_constant_mismatch:nv12={DXGI_FORMAT_NV12}"
                f"!=dxgiformat.h:{DXGI_FORMAT_NV12_ABI_TRUTH}",
            )
            result.fallback_reason = f"format_support_layer:{reason}"
            result.layers["format_support_nv12"] = {
                "ok": False,
                "reason": reason,
                "dxgi_format": DXGI_FORMAT_NV12,
            }
            return result
        ok, bits, why = check_format_support(device, DXGI_FORMAT_NV12)
        result.layers["format_support_nv12"] = {
            "ok": ok, "support_bits": hex(bits), "reason": why,
        }
        if not ok:
            result.fallback_reason = f"format_support_layer:{why}"
            return result

        # --- 3. 纹理池层（短循环，120 帧探测协议） --------------------------
        pool, pool_reason = create_d3d11_texture_pool(
            device.device_ptr, depth=4, width=1920, height=1080,
            dxgi_format=DXGI_FORMAT_NV12, label="zero_copy_probe",
        )
        if pool is None:
            result.fallback_reason = f"texture_pool_layer:{pool_reason}"
            result.layers["texture_pool"] = {
                "ok": False, "reason": pool_reason,
            }
            return result
        try:
            serviced = 0
            for _ in range(max(1, min(frames, 120))):
                lease = pool.try_acquire()
                if lease is None:
                    break
                lease.release()
                serviced += 1
            stats = pool.stats()
            pool_ok = (
                stats.created == 4 and stats.in_use == 0
                and serviced > 0
            )
            result.layers["texture_pool"] = {
                "ok": pool_ok,
                "stats": stats.to_dict(),
                "frames_serviced": serviced,
            }
            if not pool_ok:
                result.fallback_reason = (
                    f"texture_pool_layer:{FallbackReason.GPU_PROBE_FAILED.value}"
                )
                return result
        finally:
            pool.close()

        # --- 4. 解码 surface 层（fail-closed 边界） -------------------------
        decoder = D3D11Decoder()
        opened = False
        if file_path:
            opened = decoder.open(file_path)
        else:
            # 无素材时也如实标注：解码 surface 层未实现（原生桥缺位）。
            result.fallback_reason = (
                f"decode_surface_layer:{FallbackReason.NOT_YET_IMPLEMENTED.value}"
            )
        result.d3d11_decoder_available = opened
        if opened:
            decoder.close()
            result.decode_path = "d3d11_decode_surface"
        elif not result.fallback_reason:
            result.fallback_reason = (
                f"decode_surface_layer:{decoder.last_fallback_reason}"
            )

        result.probe_success = True
        result.frames_tested = max(1, min(frames, 120))
        return result
    finally:
        device.release()
