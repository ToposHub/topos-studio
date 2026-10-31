"""D3D11 GPU 驻留帧契约（计划 2026-09-15 §3.3 `GpuDecodedFrame`）。

计划原文：

    | GpuDecodedFrame | ID3D11Texture2D、DXGI 格式、颜色元数据、时间戳、
    | adapter LUID、fence / release lease | 不暴露 CPU planes；跨 adapter
    | 立即失败并给出 reason。 |

与 src/shared/media/gpu_frame.py 的 Metal/GL 语义版并存：那个 `GpuDecodedFrame`
服务零拷贝播放链路，本模块是 Windows 导出链路的 D3D11 语义版本——texture
是 ID3D11Texture2D 指针、身份是 adapter LUID、生命周期归纹理池 lease。

硬规则：
- 不持有任何 CPU 数组（没有 planes / data 字段）；
- adapter_luid 必填：空 LUID 的帧不允许进入全 GPU 导出判定；
- release 委托 lease（恰一次），帧对象本身 frozen；
- 10-bit P010 保持 MSB 对齐原样（与 hw_planar_source 的 msb_aligned_p010
  约定一致），不做 CPU 移位。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .d3d11_device import DXGI_FORMAT_NV12, DXGI_FORMAT_P010
from .d3d11_texture_pool import SEMI_PLANAR_SUBRESOURCE_COUNT, TextureLease

__all__ = [
    "D3D11_SEMI_PLANAR_FORMATS",
    "D3D11GpuFrame",
    "is_d3d11_gpu_frame",
]

D3D11_SEMI_PLANAR_FORMATS = (DXGI_FORMAT_NV12, DXGI_FORMAT_P010)


@dataclass(frozen=True)
class D3D11GpuFrame:
    """D3D11 GPU 驻留解码帧（导出链路 T0 链路的载荷类型）。"""

    texture_ptr: int
    dxgi_format: int
    width: int
    height: int
    #: 归还所有权的唯一途径；帧消费方必须在 fence 后调用。
    lease: TextureLease
    adapter_luid: str
    frame_index: int = -1
    pts_us: Optional[int] = None
    #: H.273 语义的色彩元数据（bt709 / bt2020nc / smpte2084 / limited...）。
    color_matrix: str = "bt709"
    color_transfer: str = "bt709"
    color_primaries: str = "bt709"
    video_range: str = "limited"
    #: 半平面 NV12/P010 固定 2 个子资源（Y=0 / UV=1）。
    subresource_count: int = SEMI_PLANAR_SUBRESOURCE_COUNT
    bit_depth: int = 8
    gpu_decode_ms: Optional[float] = None
    gpu_render_ms: Optional[float] = None
    gpu_yuv_ms: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.texture_ptr:
            raise ValueError("D3D11GpuFrame 不接受空纹理指针")
        if self.dxgi_format not in D3D11_SEMI_PLANAR_FORMATS:
            raise ValueError(f"不支持的 DXGI 半平面格式: {self.dxgi_format}")
        if self.width <= 0 or self.height <= 0 or self.height % 2 != 0:
            raise ValueError("宽高必须为正且高为偶数（4:2:0）")
        if not self.adapter_luid:
            raise ValueError(
                "adapter_luid 必填：无 LUID 的帧不允许进入全 GPU 导出判定"
            )
        if self.subresource_count != SEMI_PLANAR_SUBRESOURCE_COUNT:
            raise ValueError("半平面帧子资源数必须为 2")
        expected_depth = 8 if self.dxgi_format == DXGI_FORMAT_NV12 else 10
        if self.bit_depth != expected_depth:
            raise ValueError(
                f"dxgi_format={self.dxgi_format} 的 bit_depth 必须是"
                f" {expected_depth}"
            )
        for name in ("gpu_decode_ms", "gpu_render_ms", "gpu_yuv_ms"):
            value = getattr(self, name)
            if value is not None and value < 0.0:
                raise ValueError(f"{name} 不可为负")

    @property
    def is_10bit(self) -> bool:
        return self.dxgi_format == DXGI_FORMAT_P010

    def release(self) -> None:
        """归还纹理到池（恰一次；重复调用由 lease 抛错）。"""
        self.lease.release()

    def to_trail_dict(self) -> dict:
        """进入 ExportGpuTrail/报告的精简视图（绝不含指针数值泄漏风险）。"""
        return {
            "kind": "d3d11_gpu_frame",
            "dxgi_format": "p010" if self.is_10bit else "nv12",
            "width": self.width,
            "height": self.height,
            "frame_index": self.frame_index,
            "pts_us": self.pts_us,
            "adapter_luid": self.adapter_luid,
            "color_matrix": self.color_matrix,
            "color_transfer": self.color_transfer,
            "color_primaries": self.color_primaries,
            "video_range": self.video_range,
            "bit_depth": self.bit_depth,
        }


def is_d3d11_gpu_frame(obj: object) -> bool:
    """判别函数：调用方在接缝处显式分支，不做鸭子类型猜测。"""
    return isinstance(obj, D3D11GpuFrame)
