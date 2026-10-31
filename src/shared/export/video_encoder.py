"""
PyAV Video Encoder Module
FFmpeg/PyAV 视频编码器实现。

## 职责

- FFmpegEncoderConfig: PyAV 编码器配置（与 encoder.py 的 VideoEncoderConfig 互补）
- ColorMetadataConfig: 色彩元数据配置（定义在 src/shared/codec/color_metadata_config.py，此处重导出）
- FFmpegVideoEncoder: PyAV 编码器封装
- 支持 ProRes、DNxHR、H.264、HEVC 编码
- 支持高位深输出（10-bit/12-bit）

## 与 encoder.py 的关系

- encoder.py: VideoEncoderConfig（通用配置类，生成 FFmpeg 命令行参数）
- video_encoder.py: FFmpegEncoderConfig（PyAV 编码器专用配置，用于实际编码）

两者可以共存，encoder.py 用于配置生成，video_encoder.py 用于实际编码执行。

## 设计原则

- 完整类型注解
- 支持色彩元数据写入
- 资源安全管理
- 与 DecodedFrame 兼容
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from ..media.frame import DecodedFrame
    from ..media.metadata import Rational, VideoMetadata


logger = logging.getLogger(__name__)


# =============================================================================
# 编码器配置
# =============================================================================


class CodecType(Enum):
    """编码器类型"""

    H264 = "h264"
    HEVC = "hevc"
    PRORES = "prores"
    DNXHR = "dnxhr"
    VP9 = "vp9"
    AV1 = "av1"
    FFV1 = "ffv1"
    RAWVIDEO = "rawvideo"
    GIF = "gif"


class ProResProfile(Enum):
    """ProRes 编码器 profile"""

    PROXY = "proxy"      # ProRes Proxy (~45 Mbps)
    LT = "lt"            # ProRes LT (~102 Mbps)
    STANDARD = "422"     # ProRes 422 (~147 Mbps)
    HQ = "hq"            # ProRes 422 HQ (~220 Mbps)
    QUALITY = "4444"     # ProRes 4444 (~330 Mbps, 10-bit + alpha)
    XQ = "4444xq"        # ProRes 4444 XQ (~440 Mbps, 12-bit + alpha)


class DNxHRProfile(Enum):
    """DNxHR 编码器 profile"""

    LB = "lb"            # Low Bandwidth (~78 Mbps @ 4K)
    SQ = "sq"            # Standard Quality (~156 Mbps @ 4K)
    HQ = "hq"            # High Quality (~234 Mbps @ 4K)
    HQX = "hqx"          # High Quality Extended (10-bit, ~468 Mbps @ 4K)
    QUALITY = "444"      # DNxHR 444 (10-bit + alpha, ~702 Mbps @ 4K)


# 像素格式映射表
PIXEL_FORMAT_MAP: dict[str, dict[str, str]] = {
    # H.264/HEVC 可用格式
    "h264": {
        "8bit": "yuv420p",
        "8bit_422": "yuv422p",
        "8bit_444": "yuv444p",
        "10bit": "yuv420p10le",
        "10bit_422": "yuv422p10le",
        "10bit_444": "yuv444p10le",
    },
    "hevc": {
        "8bit": "yuv420p",
        "8bit_422": "yuv422p",
        "8bit_444": "yuv444p",
        "10bit": "yuv420p10le",
        "10bit_422": "yuv422p10le",
        "10bit_444": "yuv444p10le",
        "12bit": "yuv420p12le",
        "12bit_422": "yuv422p12le",
        "12bit_444": "yuv444p12le",
    },
    "prores": {
        "proxy": "prores_422proxy",
        "lt": "prores_422lt",
        "standard": "prores_422",
        "hq": "prores_422hq",
        "4444": "prores_4444",
        "4444xq": "prores_4444xq",
    },
    "dnxhr": {
        "lb": "dnxhr_lb",
        "sq": "dnxhr_sq",
        "hq": "dnxhr_hq",
        "hqx": "dnxhr_hqx",
        "444": "dnxhr_444",
    },
    "vp9": {
        "8bit": "yuv420p",
        "10bit": "yuv420p10le",
        "12bit": "yuv420p12le",
    },
    "av1": {
        "8bit": "yuv420p",
        "10bit": "yuv420p10le",
        "10bit_422": "yuv422p10le",
        "10bit_444": "yuv444p10le",
        "12bit": "yuv420p12le",
        "12bit_422": "yuv422p12le",
        "12bit_444": "yuv444p12le",
    },
    "ffv1": {
        "8bit": "yuv420p",
        "8bit_444": "yuv444p",
        "10bit": "yuv420p10le",
        "10bit_444": "yuv444p10le",
        "16bit": "yuv420p16le",
        "16bit_444": "yuv444p16le",
        "rgb48": "rgb48le",
    },
}

# FFmpeg 编码器名称映射
ENCODER_NAME_MAP: dict[str, str] = {
    "h264": "libx264",
    "hevc": "libx265",
    "prores": "prores_ks",
    # FFmpeg 里 DNxHR/DNxHD 的编码器名是 dnxhd（不存在名为 'dnxhr' 的
    # 编码器）；DNxHR profile 通过 profile 选项 dnxhr_<lb|sq|hq|hqx|444> 指定
    "dnxhr": "dnxhd",
    "vp9": "libvpx-vp9",
    "av1": "libaom-av1",  # 静态缺省；实际经 cpu_av1_encoder_name() 解析
    "ffv1": "ffv1",
}

# AUD-33504：x264 风格 preset → libvpx-vp9 速度档 'cpu-used' 的映射。
# libvpx 没有 'preset' AVOption（直接下发会在 avcodec_open2 报
# Option not found）；cpu-used 0=最慢/质量最好 .. 8=最快。
VP9_PRESET_CPU_USED: dict[str, str] = {
    "ultrafast": "8",
    "superfast": "7",
    "veryfast": "6",
    "faster": "5",
    "fast": "5",
    "medium": "4",
    "slow": "2",
    "slower": "1",
    "veryslow": "0",
    "placebo": "0",
}


def cpu_av1_encoder_name() -> str:
    """本 wheel 实际可用的 av1 CPU 编码器名（注册探测为准）。

    PyAV 18.1.0 wheel 不带 libaom-av1（codecs_available 为 False），只带
    libsvtav1；静态映射 libaom-av1 会让 av1 CPU 路径（kill switch、无
    N/A 卡机器、硬编探针回退）在 add_stream 即失败。libsvtav1 注册时
    优先，否则维持 libaom-av1（老 wheel 兼容）。
    """
    from .encoder import probe_hwaccel_encoder_available

    if probe_hwaccel_encoder_available("libsvtav1"):
        return "libsvtav1"
    return "libaom-av1"

# 硬件编码开关按平台分开，保留 Windows 变量的既有语义；硬编码流与
# x264/x265 结果不同，需要可重复的 CPU 输出时可关闭对应平台的硬编。
WIN_HW_ENCODE_DISABLE_ENV = "TOPOS_DISABLE_WIN_HW_ENCODE"
MACOS_HW_ENCODE_DISABLE_ENV = "TOPOS_DISABLE_MACOS_HW_ENCODE"

def amf_encoder_available(codec_lower: str) -> bool:
    """检查 PyAV 是否带 AMD AMF 硬件编码器（h264_amf/hevc_amf）。

    W6-02 接缝统一：编码器名与注册探测统一消费 encoder.py 的
    (hwaccel, codec) 映射与 probe 缓存，本模块不再维护平行实现。
    注意：这只证明编码器注册存在；驱动级失败在 open() 时捕获并回退
    CPU，回退经 hw_fallback_reason_with_detail 产出结构化诊断。
    """
    from .encoder import hwaccel_encoder_name, probe_hwaccel_encoder_available

    name = hwaccel_encoder_name("amf", codec_lower)
    return bool(name) and probe_hwaccel_encoder_available(name)


def nvenc_encoder_available(codec_lower: str) -> bool:
    """检查 PyAV 是否带 NVIDIA NVENC 硬件编码器（h264/hevc/av1_nvenc）。

    与 :func:`amf_encoder_available` 同语义：只查注册存在，驱动级
    可用性裁决在 open() 单帧探针（W6-01，同 AMF 先例）。
    """
    from .encoder import hwaccel_encoder_name, probe_hwaccel_encoder_available

    name = hwaccel_encoder_name("nvenc", codec_lower)
    return bool(name) and probe_hwaccel_encoder_available(name)


def videotoolbox_encoder_available(codec_lower: str) -> bool:
    """检查 PyAV/FFmpeg 是否注册了对应的 VideoToolbox 编码器。"""
    from .encoder import hwaccel_encoder_name, probe_hwaccel_encoder_available

    name = hwaccel_encoder_name("videotoolbox", codec_lower)
    return bool(name) and probe_hwaccel_encoder_available(name)


_hw_vendor_cache: Optional[str] = None


def detect_hw_vendor() -> str:
    """Windows 硬件编码 vendor 路由探测（'nvidia'/'amd'/''，带缓存）。

    auto 模式据此选择硬编通道（nvidia→NVENC、amd→AMF）；非 Windows
    或探测不到硬件 vendor 返回 ''。判定走 DXGI 适配器枚举
    （windows_hw/gpu_capability），与解码侧 decode_policy 同一真相源。
    边界：取 primary_vendor（=adapters[0]，「主输出优先」枚举序）——
    Intel iGPU 主显 + N/A 卡副显的 Optimus 布局下 auto 会保守回退
    CPU（编码/解码/interop 同源同判，跨适配器统一裁决留给
    W1-03 真 Optimus 复核，不在此单边改路由）。
    """
    global _hw_vendor_cache
    if _hw_vendor_cache is not None:
        return _hw_vendor_cache
    vendor = ""
    if sys.platform == "win32":
        try:
            from src.shared.media.windows_hw.gpu_capability import (
                probe_gpu_capability,
            )

            gpu = probe_gpu_capability()
            if gpu.probe_ok and gpu.primary_vendor in ("nvidia", "amd"):
                vendor = gpu.primary_vendor
        except Exception:  # noqa: BLE001 — 探测失败按无硬件路由，不猜
            vendor = ""
    _hw_vendor_cache = vendor
    return vendor


def vendor_hwaccel_name(vendor: str) -> str:
    """vendor → hwaccel 通道名（auto 路由用；未知 vendor → ''）。"""
    return {"nvidia": "nvenc", "amd": "amf"}.get(vendor, "")


def hw_fallback_reason_with_detail(hwaccel: str, detail: str = "") -> str:
    """构造 ``encoder_hw_unavailable:<hwaccel>[:<detail>]`` 结构化诊断。

    reasons.py 词表规范形态：归类只看第一段前缀；hwaccel 与探测异常
    摘要作为 detail 供 backend_selector / 导出 outcome 诊断链消费。
    """
    from src.shared.media.windows_hw.reasons import FallbackReason, param

    return param(
        FallbackReason.ENCODER_HW_UNAVAILABLE,
        f"{hwaccel}:{detail}" if detail else hwaccel,
    )

# DNxHR bitrate 查找表 (bps)，按 profile × 分辨率层级
# 来源: Avid DNxHR 编码器规范
# resolution_tier: "hd" (<=1080p), "uhd" (>1080p)
DNXHR_BITRATE_TABLE: dict[str, int] = {
    # key: f"{profile}_{resolution_tier}"
    "lb_hd": 78_000_000,
    "lb_uhd": 156_000_000,
    "sq_hd": 156_000_000,
    "sq_uhd": 312_000_000,
    "hq_hd": 234_000_000,
    "hq_uhd": 468_000_000,
    "hqx_hd": 468_000_000,
    "hqx_uhd": 936_000_000,
    "444_hd": 702_000_000,
    "444_uhd": 1_404_000_000,
}


# ColorMetadataConfig 定义已移至中立层 src/shared/codec/color_metadata_config.py
# （ToposVideoEncoder 与本模块共同消费；此处重导出保持既有 import 兼容）
from src.shared.codec.color_metadata_config import ColorMetadataConfig  # noqa: F401


@dataclass
class FFmpegEncoderConfig:
    """
    PyAV 编码器配置。

    包含编码器的所有参数设置，用于 FFmpegVideoEncoder。

    与 encoder.py 中的 VideoEncoderConfig 互补：
    - VideoEncoderConfig: FFmpeg 命令行参数配置
    - FFmpegEncoderConfig: PyAV 编码器运行时配置

    Attributes:
        codec: 编码器名称（'h264', 'hevc', 'prores', 'dnxhr'）
        profile: 编码器 profile（ProRes/DNxHR）
        width: 输出宽度
        height: 输出高度
        fps: 帧率（Rational）
        pix_fmt: 像素格式
        color_metadata: 色彩元数据配置
        bitrate: 目标比特率（仅 GOP codec，单位 bps）
        min_bitrate: CBR 最小比特率（单位 bps）
        max_bitrate: CBR 最大比特率（单位 bps）
        buffer_size: CBR VBV 缓冲区大小（单位 bit）
        crf: CRF 质量参数（仅 H.264/HEVC）
        preset: 编码预设（仅 H.264/HEVC）
        gop_size: GOP 大小（仅 GOP codec）
        has_alpha: 是否包含 alpha 通道
        hwaccel: 硬件编码策略（'auto' 默认——Windows 按 NVIDIA/AMD 路由，
            macOS 对 4:2:0 SDR H.264/HEVC 路由 VideoToolbox；驱动会话探针
            失败自动回退 CPU 并记录 encoder_hw_unavailable:<hwaccel>:<detail>；
            显式值支持 amf/nvenc/videotoolbox；None 强制 CPU）
        alpha_premultiplied: Alpha 预乘语义（整段固定；仅 Topos 路径消费——
            应用内合成链约定 straight=False，planar 直通 premultiplied 源时
            必须显式置 True，与帧标志不一致将显式报错）
        alpha_budget_policy: Topos Alpha 预算超硬上限策略（record/error/continue；
            仅 Topos 路径消费，默认 record）
    """

    codec: str
    width: int
    height: int
    fps: "Rational"
    pix_fmt: str = "yuv420p"
    profile: Optional[str] = None
    color_metadata: Optional[ColorMetadataConfig] = None
    bitrate: Optional[int] = None  # bps
    min_bitrate: Optional[int] = None  # bps, CBR only
    max_bitrate: Optional[int] = None  # bps, CBR only
    buffer_size: Optional[int] = None  # bits, CBR VBV buffer
    crf: Optional[int] = None  # 0-51 for H.264/HEVC
    preset: Optional[str] = None  # ultrafast, fast, medium, slow, veryslow
    gop_size: Optional[int] = None  # GOP size
    gop: str = 'intra'  # Topos: 'intra'（全 I）| 'ip2'（帧间微 GOP，ADR-C047/C048）
    gop_workers: int = 2  # Topos gop='ip2' 分段并行 worker 数（ADR-C050）
    has_alpha: bool = False
    # GIF palette entries (2..256); ignored by non-GIF encoders.
    gif_color_count: int = 256
    threads: int = 0  # 0 = FFmpeg 自动（全核）。编码/导出是批处理负载，
    #                    x264/x265/dnxhd 线程扩展显著；需要限制核数的
    #                    调用方（如播放期渲染缓存）显式传小值
    hwaccel: Optional[str] = "auto"  # 'auto' 按 macOS/Windows 能力路由；None=CPU
    pipelined_conversion: bool = False  # 后台线程做帧格式转换，与编码重叠。
    #   swscale/numpy 释放 GIL，编码期间转换下一帧，帧耗时从
    #   convert+encode 之和降到 max(convert, encode)（4K DNxHR 实测
    #   12→20fps）。默认关闭以保持同步语义（encode_frame 返回即编码
    #   完成）；时间线导出等批处理路径显式开启。close() 时 drain 保序，
    #   转换错误延迟至 drain 抛出。
    convert_workers: int = 1  # 并行转换线程数（须 pipelined_conversion=True）。
    #   1 = 单转换线程（历史语义，FIFO 保序）。>1 = 多转换线程帧级并行，
    #   主线程按 pts 序重排后编码——4K 10bit 单帧转换 ~45ms 远超 AMF
    #   编码 13ms（2026-09-12 分环节实测），单线程转换是端到端地板；
    #   帧级并行（每线程独立 swscale 上下文）优于帧内分带（每带重开
    #   上下文反而更慢，且色度跨带重采样破坏 bit 一致性——实测证伪）。
    #   in-flight 内存上界（AUD-33508 输出侧背压后成立）：
    #   输入 ≤ (workers+1) 帧（队列容量）；输出 ≤ map 容量 workers
    #   + 各 worker 手中待插入 1 帧（+ 编码器正等待的 next 帧旁路 1 帧）。
    alpha_premultiplied: bool = False
    # Topos 熵模式——'rans2'=V7-R2 上下文 rANS（默认，ADR-C038 转正：
    # ADR-C036 实测真实素材同画质较 rans 再省 1.2~2.8% payload（qp20-84
    # 全段）、解码 ~1×、差分逐位一致）；'v2'=canonical VLC（RDO 依赖）；
    # 'v1'=Rice（历史位流逐字节一致）。仅 Topos 路径消费。
    # V 代际收纳（2026-09-13）：合法域收缩为 {rans2, v2, v1}；退役值
    # （rans/intra/intra-range/acpair/acpair_table）构造期显式 ValueError。
    entropy_mode: str = "rans2"
    # V2.x：Topos 逐带自适应量化（reserved[1]）——'off'（默认）/ 'on'
    # （选用）。2026-09-03 色度专属实测天花板 = 422 家族同码率 luma
    # +0.19~0.23 dB；2026-09-21（P3）加入亮度空间 AQ（块均值活动度
    # 1/8 倍频程 log、纹理优先方向 ±4）后真实语料 A/B 同码率
    # ΔPSNR_Y −0.05~−0.27 dB（p3_spatial_aq_ab.json）——两方向均负收益，
    # 默认关维持；块级 QP 信令归 P5。仅 Topos 消费。
    aq_mode: str = "off"
    # V2.x：Topos 逐系数 level RDO 精修（reserved[2]，须 V2 VLC 熵）——
    # 'off'（默认）/ 'on'（选用）。同码率实测（2026-09-04）：422 家族
    # luma +0.15 dB；444 家族 +0.4~0.6 dB（色度 −1~3 dB 的预算交换，
    # 内建 μ_chroma=50% 中档）。纯编码端决策，码流零格式变更；编码
    # 端开销 ~10-20%（解码不变）。默认关：luma↔chroma 交换比属产品
    # 决策（默认开 + 档位第三轮重定标待拍板）。
    rdo_mode: str = "off"
    # R3：Topos Alpha 预算超硬上限交付策略——"record"=记录+警告交付（默认）/
    # "error"=结构化报错 / "continue"=用户授权交付（记 authorized 标志）。
    # 仅 Topos 路径消费；mode1（a16 无损）任何策略都不自动降质。
    alpha_budget_policy: str = "record"
    # P1 预算接口（2026-09-21，画质对齐计划 §5 P1；仅 Topos 路径消费）：
    # 三级预算优先级 max_file_bytes > max_video_bytes > bitrate > 档位 bpp。
    # max_video_bytes：视频流总字节硬上限——码控逐帧目标按
    #   remaining_budget/remaining_frames 自适应；close() 超限即拒绝定稿。
    # max_file_bytes：最终文件总字节硬上限——编码器自动扣除音轨
    #   （已声明 PCM/AAC 按预算帧数换算）与容器开销预留（4KB + 64B/帧）
    #   后把剩余给视频。两者都要求 budget_frames（总帧数估计，调用方
    #   如时间线导出器天然可知）。
    max_video_bytes: Optional[int] = None
    max_file_bytes: Optional[int] = None
    # 预算换算帧数：max_video_bytes/max_file_bytes/bitrate 换算逐帧目标所需
    # 的总帧数（≥1）。缺省 None = 不启用任何预算覆盖。
    budget_frames: Optional[int] = None
    # M3-H2：源 ICC profile 原始字节（仅 Topos 图片序列导出消费——
    # 随流写入 ICCP chunk + iccp_ref=1 权威位；None = 不携带）
    icc_profile: Optional[bytes] = None

    def __post_init__(self) -> None:
        """验证配置并自动推断参数"""
        # 验证分辨率
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid resolution: {self.width}x{self.height}")

        # P1 预算字段（Topos 消费，但校验通用）：正整数；预算须伴随帧数
        for name in ("max_video_bytes", "max_file_bytes", "budget_frames"):
            v = getattr(self, name)
            if v is not None and int(v) <= 0:
                raise ValueError(f"{name} 必须为正整数，得到 {v}")
        has_budget = (self.max_video_bytes is not None
                      or self.max_file_bytes is not None)
        if has_budget and self.budget_frames is None:
            raise ValueError(
                "max_video_bytes/max_file_bytes 须同时给出 budget_frames"
                "（预算换算总帧数）")

        if self.codec.lower() == "gif" and not 2 <= int(self.gif_color_count) <= 256:
            raise ValueError("GIF color count must be between 2 and 256")

        # 自动推断像素格式
        if self.pix_fmt == "yuv420p" and self.has_alpha:
            logger.warning("Alpha requested but pixel format is yuv420p, no alpha support")
            self.has_alpha = False

        # 自动设置 profile
        if self.profile is None:
            self.profile = self._infer_profile()

        # 自动设置 GOP size
        if self.gop_size is None and self._is_gop_codec():
            # 默认 GOP size = fps * 2（约 2 秒一个关键帧）
            self.gop_size = int(self.fps.to_fps() * 2)

        # 默认色彩元数据
        if self.color_metadata is None:
            self.color_metadata = ColorMetadataConfig.sdr()

        logger.debug(
            f"VideoEncoderConfig: codec={self.codec}, "
            f"resolution={self.width}x{self.height}, "
            f"fps={self.fps.to_fps():.3f}, "
            f"profile={self.profile}, pix_fmt={self.pix_fmt}"
        )

    def _infer_profile(self) -> str:
        """根据编码器推断默认 profile"""
        codec_lower = self.codec.lower()

        if codec_lower == "prores":
            return "hq"  # ProRes 422 HQ
        elif codec_lower == "dnxhr":
            if self.has_alpha:
                return "444"  # DNxHR 444
            elif self.pix_fmt.endswith("10le") or self.pix_fmt.endswith("10"):
                return "hqx"  # DNxHR HQX (10-bit)
            else:
                return "hq"  # DNxHR HQ
        elif codec_lower in ("h264", "hevc"):
            if self.pix_fmt.endswith("10le"):
                return "main10" if codec_lower == "hevc" else "high10"
            # HEVC 8-bit 档是 main（"high" 是 H.264 专属 profile 名，
            # libx265/hevc_amf/hevc_nvenc 均不识别）
            return "main" if codec_lower == "hevc" else "high"

        return ""

    def _is_gop_codec(self) -> bool:
        """判断是否为 GOP 编解码器"""
        return self.codec.lower() in ("h264", "hevc", "vp9", "av1")

    def amf_input_pix_fmt(self) -> Optional[str]:
        """AMF 会话可接受的输入像素格式；None = 该格式不走 AMF。

        真机实测（2026-09-12，RX 6800 / PyAV 18.1.0）：
        - yuv420p（8bit 4:2:0）：AMF 直收；
        - yuv420p10le 直输 avcodec_open2 EINVAL（两次独立实测）——AMF 的
          10bit 半平面输入是 p010le，需先 swscale 转换（yuv420p10le→
          p010le replan 实测可行；编码侧由 _encode_pix_fmt 统一承载）；
        - h264 无 10bit 档（High10 非消费场景）；4:2:2/4:4:4/12bit 不支持。
        """
        pix = self.pix_fmt.lower()
        codec_lower = self.codec.lower()
        if pix == "yuv420p" and codec_lower in ("h264", "hevc"):
            return "yuv420p"
        if pix == "yuv420p10le" and codec_lower == "hevc":
            return "p010le"
        return None

    def nvenc_input_pix_fmt(self) -> Optional[str]:
        """NVENC 会话可接受的输入像素格式；None = 该格式不走 NVENC。

        FFmpeg nvenc 家族（h264_nvenc/hevc_nvenc/av1_nvenc）支持 yuv420p
        与 p010le 半平面输入（W6-01 真机接线验证）：
        - yuv420p（8bit 4:2:0）：h264/hevc/av1 直收；
        - yuv420p10le → p010le：hevc/av1（10bit）；h264 High10 非消费
          场景不支持；4:2:2/4:4:4/12bit 不支持。
        """
        pix = self.pix_fmt.lower()
        codec_lower = self.codec.lower()
        if pix == "yuv420p" and codec_lower in ("h264", "hevc", "av1"):
            return "yuv420p"
        if pix == "yuv420p10le" and codec_lower in ("hevc", "av1"):
            return "p010le"
        return None

    def hw_vendor(self) -> str:
        """本配置将路由的硬编通道（'amf'/'nvenc'/'videotoolbox'/''）。

        显式硬编模式原样返回；'auto' 在 macOS 选择 VideoToolbox，
        Windows 按 detect_hw_vendor() 路由。
        """
        mode = (self.hwaccel or "").lower()
        if mode in ("amf", "nvenc", "videotoolbox"):
            return mode
        if mode == "auto":
            if sys.platform == "darwin":
                return "videotoolbox"
            return vendor_hwaccel_name(detect_hw_vendor())
        return ""

    def videotoolbox_input_pix_fmt(self) -> Optional[str]:
        """VideoToolbox H.264/HEVC 8-bit 4:2:0 input formats.

        The current export pipeline converts to CPU ``yuv420p`` before handing
        a frame to PyAV. VideoToolbox accepts that format directly, avoiding
        another swscale conversion. 10-bit and native CVPixelBuffer input are
        intentionally left to a later zero-copy writer path.
        """
        if (self.codec.lower() in ("h264", "hevc")
                and self.pix_fmt.lower() == "yuv420p"):
            return "yuv420p"
        return None

    def hw_input_pix_fmt(self) -> Optional[str]:
        """当前 vendor 通道可接受的输入像素格式（None = 不走硬编）。"""
        vendor = self.hw_vendor()
        if vendor == "amf":
            return self.amf_input_pix_fmt()
        if vendor == "nvenc":
            return self.nvenc_input_pix_fmt()
        if vendor == "videotoolbox":
            return self.videotoolbox_input_pix_fmt()
        return None

    def get_encoder_name(self, force_cpu: bool = False) -> str:
        """获取 FFmpeg 编码器名称（hwaccel 启用时按 encoder.py seam 映射）。"""
        if not force_cpu and self.hwaccel_engaged():
            from .encoder import hwaccel_encoder_name

            seam_name = hwaccel_encoder_name(self.hw_vendor(), self.codec.lower())
            if seam_name:
                return seam_name
        name = ENCODER_NAME_MAP.get(self.codec.lower(), self.codec.lower())
        if name == "libaom-av1":
            name = cpu_av1_encoder_name()
        return name

    def hwaccel_engaged(self, force_cpu: bool = False) -> bool:
        """硬件编码是否实际启用（平台/格式/可用性门控后）。

        'auto' 按平台启用：macOS 使用 VideoToolbox 的 8-bit 4:2:0
        H.264/HEVC；Windows 使用已探测到的 AMF/NVENC。两条 auto 路径
        都限 SDR 并要求编码器已注册，open() 还会使用实际输出分辨率
        创建硬件会话探针；失败时回退 CPU。

        显式 hwaccel='amf'/'nvenc'/'videotoolbox' 同样受平台和格式门控
        （格式不支持时硬件
        编码必然失败），但不做 vendor 预检（点错通道由 open() 探针
        回退并记录诊断），也不受 SDR 限制（HDR 元数据缺失在 open()
        时告警）；可用性探测失败时告警并回退 CPU。
        """
        if force_cpu:
            return False
        mode = (self.hwaccel or "").lower()
        if mode not in ("auto", "amf", "nvenc", "videotoolbox"):
            return False
        if (sys.platform == "win32"
                and os.environ.get(WIN_HW_ENCODE_DISABLE_ENV)):
            return False
        if (sys.platform == "darwin"
                and os.environ.get(MACOS_HW_ENCODE_DISABLE_ENV)):
            return False
        codec_lower = self.codec.lower()
        if codec_lower not in ("h264", "hevc", "av1"):
            return False
        if self.hw_input_pix_fmt() is None:
            return False
        if sys.platform == "darwin":
            if self.hw_vendor() != "videotoolbox":
                return False
            if codec_lower not in ("h264", "hevc"):
                return False
            available = videotoolbox_encoder_available(codec_lower)
        elif sys.platform == "win32":
            if self.hw_vendor() not in ("amf", "nvenc"):
                return False
            available = {
                "amf": amf_encoder_available,
                "nvenc": nvenc_encoder_available,
            }[self.hw_vendor()](codec_lower)
        else:
            return False
        if mode == "auto":
            transfer = (
                getattr(self.color_metadata, "transfer", None)
                if self.color_metadata is not None
                else None
            )
            if transfer not in (None, "", "bt709", "unspecified"):
                return False
        if not available:
            if mode != "auto":
                logger.warning(
                    "hwaccel=%r 显式请求但 %s 编码器不可用，回退 CPU 编码",
                    mode, self.hw_vendor(),
                )
            return False
        return True

    @classmethod
    def from_metadata(
        cls,
        metadata: "VideoMetadata",
        codec: str = "prores",
        profile: Optional[str] = None,
    ) -> "FFmpegEncoderConfig":
        """
        从 VideoMetadata 创建配置。

        Args:
            metadata: 视频元数据
            codec: 目标编码器
            profile: 编码器 profile

        Returns:
            FFmpegEncoderConfig 实例
        """
        # 确定像素格式
        bit_depth = metadata.bit_depth
        has_alpha = metadata.has_alpha

        # 根据编码器选择像素格式
        pix_fmt = cls._select_pix_fmt(codec, bit_depth, has_alpha, profile)

        # 色彩元数据
        color_metadata = ColorMetadataConfig.from_metadata(metadata)

        return cls(
            codec=codec,
            width=metadata.width,
            height=metadata.height,
            fps=metadata.fps,
            pix_fmt=pix_fmt,
            profile=profile,
            color_metadata=color_metadata,
            has_alpha=has_alpha,
        )

    @staticmethod
    def _select_pix_fmt(
        codec: str,
        bit_depth: int,
        has_alpha: bool,
        profile: Optional[str],
    ) -> str:
        """选择合适的像素格式"""
        codec_lower = codec.lower()
        profile_lower = profile.lower() if profile else ""

        # ProRes/DNxHR 有固定的像素格式
        if codec_lower == "prores":
            if profile_lower in ("4444", "4444xq"):
                return "yuva444p10le" if profile_lower == "4444" else "yuva444p12le"
            else:
                return "yuv422p10le"  # ProRes 422 系列

        if codec_lower == "dnxhr":
            if profile_lower == "444":
                return "yuva444p10le"
            elif profile_lower == "hqx":
                return "yuv422p10le"
            else:
                return "yuv422p"

        # GOP codec
        if codec_lower in ("h264", "hevc", "vp9", "av1"):
            if bit_depth > 8:
                if has_alpha:
                    return "yuva444p10le" if codec_lower != "vp9" else "yuva420p10le"
                else:
                    return "yuv420p10le"
            else:
                return "yuv420p"

        # FFV1
        if codec_lower == "ffv1":
            if bit_depth > 8:
                return "yuv420p10le"
            else:
                return "yuv420p"

        return "yuv420p"


class FFmpegVideoEncoder:
    """
    FFmpeg/PyAV 视频编码器。

    支持 ProRes、DNxHR、H.264、HEVC 等编码格式。
    支持高位深输出和色彩元数据写入。

    Attributes:
        output_path: 输出文件路径
        config: 编码器配置
        frame_count: 已编码帧数

    Examples:
        >>> config = FFmpegEncoderConfig(
        ...     codec='prores',
        ...     width=1920,
        ...     height=1080,
        ...     fps=Rational(24, 1),
        ...     profile='hq'
        ... )
        >>> encoder = FFmpegVideoEncoder('/path/to/output.mov', config)
        >>> encoder.open()
        >>> for frame in frame_provider:
        ...     encoder.encode_frame(frame)
        >>> encoder.close()
    """

    #: P1-02：容器能力显式契约（gif_encoder.supports_audio=False 的对偶，
    #: Topos v1.1 起 mov 容器档同为 True）——导出管线据此决定是否执行
    #: 音频渲染。
    supports_audio = True

    def __init__(
        self,
        output_path: str,
        config: FFmpegEncoderConfig,
    ) -> None:
        """
        初始化编码器。

        Args:
            output_path: 输出文件路径
            config: 编码器配置
        """
        self._output_path: str = output_path
        # M21.1（R04）：发布事务——编码写入临时文件，close 成功（flush/
        # trailer/容器关闭全部完成）后 os.replace 原子发布到最终路径。
        # 此前直接以 'w' 打开最终路径：open 即截断用户旧成片，失败路径
        # 的 os.remove 还会删掉旧成功输出。
        self._write_path: str = output_path + ".topos-partial"
        self._config: FFmpegEncoderConfig = config
        self._container: Optional[Any] = None  # av.container.OutputContainer
        self._stream: Optional[Any] = None  # av.VideoStream
        self._audio_stream: Optional[Any] = None  # av.AudioStream
        # AUD-33509：add_audio_stream 推导出的声道布局名单一真相，
        # write_audio_samples 帧布局复用（流/帧同源，不二度推导）
        self._audio_layout_name: Optional[str] = None
        self._frame_count: int = 0
        self._is_open: bool = False
        self._last_pts: int = 0
        self._using_amf: bool = False  # 实际启用了 AMF 硬件编码（探针通过）
        # W6-01 vendor 泛化：实际启用的硬编通道与编码器名（"" = CPU）
        self._hw_vendor: str = ""       # 'amf' / 'nvenc'
        self._hw_encoder_name: str = ""  # 如 'h264_nvenc' / 'hevc_amf'
        # 实际编码像素格式：通常等于 config.pix_fmt；硬编 10bit 时为
        # p010le（hw_input_pix_fmt()），转换层据此 reformat
        self._encode_pix_fmt: str = config.pix_fmt
        # W6-02 结构化诊断：探针失败回退 CPU 时的显式 reason
        # （encoder_hw_unavailable:<vendor>:<detail>，空串 = 未发生回退）
        self._hw_fallback_reason: str = ""
        self._last_probe_error: str = ""
        # 选项级 AMF 失败后的 CPU 重建标志（见 open() 的显式 codec 打开）
        self._cpu_rebuild = False
        # 转换/编码流水线：converter 线程把 DecodedFrame → PyAV frame，
        # 主线程只做 encode+mux。maxsize=2 提供背压，防止转换快于编码时
        # 无界堆积。
        self._pipelined = bool(getattr(config, "pipelined_conversion", False))
        self._convert_workers = max(
            1, int(getattr(config, "convert_workers", 1) or 1)
        )
        self._convert_queue: Optional["queue.Queue"] = None
        self._converted_queue: Optional["queue.Queue"] = None
        self._convert_threads: list = []
        self._converted_map: Optional[dict] = None
        self._convert_cond: Optional["threading.Condition"] = None
        self._converted_map_capacity: int = 0
        # AUD-33508：会话废弃标志——encode/close 失败时置位并唤醒因
        # 输出侧背压/空队列阻塞的转换线程，保证其退出（daemon 兜底
        # 之外显式清理）。
        self._convert_aborted: bool = False
        self._next_encode_pts = 0
        if self._pipelined:
            # 背压：in-flight DecodedFrame 上限（每帧 4K ~50MB）
            self._convert_queue = queue.Queue(
                maxsize=self._convert_workers + 1
            )
            if self._convert_workers > 1:
                self._converted_map = {}
                self._convert_cond = threading.Condition()
                # AUD-33508：输出侧背压——map 容量 = workers。此前 map
                # 无界：输入队列腾位后已转换帧全部积压在 map 中等待按
                # pts 序编码，快慢转换倾斜时可积压远超 workers 帧（4K
                # 每帧 ~50MB）。容量语义见 _convert_worker_multi。
                self._converted_map_capacity = self._convert_workers
            else:
                self._converted_queue = queue.Queue()

        logger.debug(
            f"FFmpegVideoEncoder created: output={output_path}, "
            f"codec={config.codec}, resolution={config.width}x{config.height}"
        )

    @property
    def output_path(self) -> str:
        """输出文件路径"""
        return self._output_path

    @property
    def config(self) -> FFmpegEncoderConfig:
        """编码器配置"""
        return self._config

    @property
    def frame_count(self) -> int:
        """已编码帧数"""
        return self._frame_count

    @property
    def is_open(self) -> bool:
        """是否已打开"""
        return self._is_open

    @property
    def hw_fallback_reason(self) -> str:
        """本次导出 硬编→CPU 回退的结构化诊断（空串 = 未发生回退）。

        形态 ``encoder_hw_unavailable:<vendor>:<detail>``（reasons.py 词表）；
        经 ExportOutcome.hw_fallback_reason 上行到导出 outcome。
        """
        return self._hw_fallback_reason

    @property
    def hw_encoder_name(self) -> str:
        """实际启用的硬件编码器名（如 'h264_nvenc'；空串 = CPU 路径）。"""
        return self._hw_encoder_name

    def open(self) -> None:
        """
        打开编码器。

        创建输出容器、配置视频流、设置编码参数。

        Raises:
            RuntimeError: PyAV 不可用
            IOError: 创建文件失败
        """
        if self._is_open:
            return

        try:
            import av
        except ImportError:
            raise RuntimeError("PyAV is not installed. Install with: pip install av")

        try:
            # 根据文件扩展名选择容器格式
            container_format = self._infer_container_format()

            # 创建输出容器
            self._container = av.open(
                self._write_path,
                mode='w',
                format=container_format if container_format else None,
            )

            # 创建视频流
            encoder_name = self._config.get_encoder_name(
                force_cpu=self._cpu_rebuild
            )
            hw_vendor = (
                self._config.hw_vendor()
                if (not self._cpu_rebuild
                    and self._config.hwaccel_engaged()) else ""
            )
            hw_input = self._config.hw_input_pix_fmt() if hw_vendor else None
            self._encode_pix_fmt = self._config.pix_fmt
            self._hw_vendor = ""
            self._hw_encoder_name = ""
            if hw_input is not None and hw_vendor:
                # 10bit：硬编通道只收 p010le，编码像素格式与配置格式解耦
                # （转换层按 _encode_pix_fmt reformat，如 yuv420p10le→p010le）
                self._encode_pix_fmt = hw_input
                vendor_label = {
                    "amf": "AMD AMF",
                    "nvenc": "NVIDIA NVENC",
                    "videotoolbox": "Apple VideoToolbox",
                }.get(hw_vendor, hw_vendor)
                if self._probe_encoder_open(encoder_name):
                    logger.info(
                        "使用 %s 硬件编码: %s（输入 %s；速度优先）",
                        vendor_label, encoder_name, hw_input,
                    )
                    transfer = (
                        getattr(self._config.color_metadata, "transfer", None)
                        if self._config.color_metadata is not None else None
                    )
                    if transfer not in (None, "", "bt709", "unspecified"):
                        logger.warning(
                            "%s 不承载 HDR 静态元数据（ST2086/MaxCLL 走 "
                            "x265-params 仅 CPU 路径支持）：显式 %s 模式下"
                            "输出流将缺 HDR metadata，请确认可接受",
                            vendor_label, hw_vendor,
                        )
                else:
                    self._hw_fallback_reason = hw_fallback_reason_with_detail(
                        hw_vendor, self._last_probe_error
                    )
                    failed_name = encoder_name
                    encoder_name = self._config.get_encoder_name(force_cpu=True)
                    self._encode_pix_fmt = self._config.pix_fmt
                    logger.warning(
                        "%s 编码器 %s 初始化失败，本次导出回退 CPU 编码器 %s"
                        "（%s）",
                        vendor_label, failed_name, encoder_name,
                        self._hw_fallback_reason,
                    )
            self._using_amf = encoder_name.endswith("_amf")
            if (self._using_amf or encoder_name.endswith("_nvenc")
                    or encoder_name.endswith("_videotoolbox")):
                self._hw_vendor = hw_vendor or (
                    "amf" if self._using_amf else
                    "videotoolbox" if encoder_name.endswith("_videotoolbox")
                    else "nvenc")
                self._hw_encoder_name = encoder_name
            # PyAV 的 add_stream(rate=...) 需要有理数（Fraction 或类似 numerator/denominator），
            # 不能传 float（旧版可接受，PyAV 17.x 会抛 AttributeError）。
            # 用 Fraction 构造，保留 fps 精度（如 24000/1001 → 23.976）。
            from fractions import Fraction
            fps_rational = self._config.fps
            if hasattr(fps_rational, 'num') and hasattr(fps_rational, 'den'):
                # 我们的 Rational 类型（num/den 字段）
                fps_fraction = Fraction(int(fps_rational.num), int(fps_rational.den))
            elif hasattr(fps_rational, 'numerator') and hasattr(fps_rational, 'denominator'):
                # 标准 Fraction 或类似
                fps_fraction = Fraction(int(fps_rational.numerator), int(fps_rational.denominator))
            else:
                # 兜底：从 float 构造（限制分母避免精度爆炸）
                fps_fraction = Fraction(float(fps_rational)).limit_denominator(100000)
            self._stream = self._container.add_stream(
                encoder_name,
                rate=fps_fraction,
            )

            # N06/C19 防线：软件 libx265 10-bit 在本构建（PyAV 17.0.1/
            # ffmpeg 8.0.1 / libx265 4.1 十比特）有确定性段错误——进程内
            # 有过任何 PyAV 媒体探测/解码后，flush 必崩（x265 十比特运动
            # 估计读污染参考帧；pools=none/frame-threads=1/bframes=0/
            # ultrafast 均不能绕过；x264 10-bit、hevc 8-bit、硬件 VT/AMF/
            # NVENC 不受影响——上游缺陷）。段错误击穿整个进程且不可恢复，
            # 显式失败优于崩溃：默认拒绝并清理半成品；设
            # TOPOS_ALLOW_KNOWN_X265_10BIT_CRASH=1 可强行尝试（上游修复
            # 验证口径）。上游升级后移除本防线。
            if (
                self._config.codec.lower() == "hevc"
                and not encoder_name.endswith(("_videotoolbox", "_amf", "_nvenc"))
                and (
                    self._encode_pix_fmt.endswith("10le")
                    or self._encode_pix_fmt.startswith("p010")
                )
                and os.environ.get("TOPOS_ALLOW_KNOWN_X265_10BIT_CRASH") != "1"
            ):
                reason = (
                    "FFmpegVideoEncoder: HEVC 10-bit 软件编码（libx265）在"
                    "当前 PyAV/ffmpeg 构建下有确定性崩溃（flush 段错误，"
                    "上游缺陷）——已拒绝导出以免整个进程崩溃丢失工作。"
                    "请改用 HEVC 8-bit、H.264，或硬件 VideoToolbox 路径；"
                    "升级 PyAV/ffmpeg 后可移除此限制。"
                )
                logger.error(reason)
                try:
                    self._stream = None
                    self._container.close()
                except Exception:  # noqa: BLE001 — 清理失败不掩盖拒绝原因
                    pass
                self._container = None
                try:
                    if os.path.exists(self._write_path):
                        os.remove(self._write_path)
                except OSError:
                    pass
                raise RuntimeError(reason)

            # 配置基本参数
            self._stream.width = self._config.width
            self._stream.height = self._config.height
            self._stream.pix_fmt = self._encode_pix_fmt

            # 配置编码参数
            self._configure_encoder()

            # 设置色彩元数据
            self._set_color_metadata()

            # 选项级硬件编码失败（如 crf=0 → cq/qp=0 超出驱动接受域、
            # 非常规分辨率约束）只在 avcodec_open2 暴露——PyAV 懒打开会把
            # 这类失败推迟到第一帧 encode_frame（届时探针回退已不可达）。
            # 显式在此打开；此刻尚未 mux 任何包，整体重建为 CPU 编码器安全。
            if self._hw_vendor:
                try:
                    self._stream.codec_context.open()
                except Exception as exc:  # noqa: BLE001
                    self._hw_fallback_reason = hw_fallback_reason_with_detail(
                        self._hw_vendor, f"{type(exc).__name__}: {exc}"
                    )
                    logger.warning(
                        "硬件编码器选项级初始化失败，重建为 CPU 编码器（%s）",
                        self._hw_fallback_reason,
                    )
                    self._cpu_rebuild = True
                    broken_container = self._container
                    self._container = None
                    self._stream = None
                    try:
                        broken_container.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.open()
                    return

            self._is_open = True
            self._frame_count = 0
            self._last_pts = 0

            logger.info(
                f"Encoder opened: {self._output_path}, "
                f"codec={encoder_name}, "
                f"pix_fmt={self._encode_pix_fmt}"
            )

        except Exception as e:
            # N06 防线等主动 RuntimeError 原样上抛（调用方按导出失败处理，
            # 不与 IO 创建失败混淆）
            if isinstance(e, RuntimeError):
                raise
            # 清理已创建的资源，防止文件句柄泄漏
            if self._container is not None:
                try:
                    self._container.close()
                except Exception:
                    pass
            self._container = None
            self._stream = None
            logger.exception(f"Failed to open encoder: {e}")
            raise IOError(f"Failed to create output file: {e}") from e

    def _infer_container_format(self) -> Optional[str]:
        """
        根据文件扩展名推断容器格式。

        Returns:
            容器格式名称或 None
        """
        path_lower = self._output_path.lower()

        if path_lower.endswith('.mov') or path_lower.endswith('.qt'):
            return 'mov'
        elif path_lower.endswith('.mp4') or path_lower.endswith('.m4v'):
            return 'mp4'
        elif path_lower.endswith('.mkv'):
            return 'matroska'
        elif path_lower.endswith('.avi'):
            return 'avi'
        elif path_lower.endswith('.webm'):
            return 'webm'
        elif path_lower.endswith('.ts') or path_lower.endswith('.m2ts'):
            return 'mpegts'

        # 根据编码器推荐
        codec_lower = self._config.codec.lower()
        if codec_lower in ('prores', 'dnxhr'):
            return 'mov'  # ProRes/DNxHR 通常在 MOV 中
        elif codec_lower == 'vp9':
            return 'webm'
        elif codec_lower == 'av1':
            return 'mp4'  # AV1 通常在 MP4 或 MKV

        return None

    def _get_dnxhr_bitrate(self, profile: str, width: int, height: int) -> int:
        """
        获取 DNxHR 精确 bitrate。

        根据编码器规范，不同分辨率层级有不同的基准 bitrate 值。

        Args:
            profile: DNxHR profile (lb/sq/hq/hqx/444)
            width: 视频宽度
            height: 视频高度

        Returns:
            计算后的 bitrate (bps)
        """
        # 确定分辨率层级: hd (<=1080p), uhd (>1080p)
        tier = "uhd" if width > 1920 or height > 1080 else "hd"
        key = f"{profile.lower()}_{tier}"
        # 默认使用 HQ @ HD 的值作为 fallback
        bitrate = DNXHR_BITRATE_TABLE.get(key, 234_000_000)
        # 按帧率缩放（基于 30fps 基准）
        fps = self._config.fps.to_fps()
        if fps > 0:
            bitrate = int(bitrate * fps / 30)
        return bitrate

    def _probe_encoder_open(self, encoder_name: str) -> bool:
        """用实际输出分辨率对硬件编码器做一次单帧打开验证。

        VideoToolbox 在这个设备上拒绝 256x256 探针（-12903），但能正常
        编码项目使用的 1920x1080；用实际尺寸可以避免误回退 CPU。硬件
        驱动/会话失败会在 avcodec_open2 阶段暴露。
        失败摘要记入 _last_probe_error，供 hw_fallback_reason 携带。
        """
        try:
            from fractions import Fraction

            import av

            self._last_probe_error = ""
            pix_fmt = self._encode_pix_fmt
            ctx = av.codec.CodecContext.create(encoder_name, "w")
            ctx.width = self._config.width
            ctx.height = self._config.height
            ctx.pix_fmt = pix_fmt
            fps = self._config.fps.to_fps()
            ctx.time_base = Fraction(1, 1) / Fraction(fps).limit_denominator(100000)
            if encoder_name.endswith("_videotoolbox"):
                ctx.options = {
                    "allow_sw": "0",
                    "realtime": "1",
                    "prio_speed": "1",
                }
                ctx.bit_rate = int(self._videotoolbox_target_bitrate())
            ctx.open()
            list(ctx.encode(av.VideoFrame(self._config.width,
                                          self._config.height, pix_fmt)))
            return True
        except Exception as exc:  # noqa: BLE001
            self._last_probe_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Hardware encoder probe failed for %s: %s", encoder_name, exc)
            return False

    def _videotoolbox_target_bitrate(self) -> int:
        """Return VideoToolbox's target bitrate, translating CRF if needed.

        VideoToolbox has no x264-style CRF. 8 Mbps at 1080p/25 is the measured
        quality anchor (close to this project's CPU CRF 20 export); scale it
        for resolution, frame rate, and requested CRF. Explicit bitrate modes
        remain exact.
        """
        if self._config.bitrate is not None:
            return max(250_000, int(self._config.bitrate))
        fps = max(1.0, float(self._config.fps.to_fps()))
        crf = 20 if self._config.crf is None else max(0, min(51, int(self._config.crf)))
        pixels_per_second = self._config.width * self._config.height * fps
        scale = pixels_per_second / (1920 * 1080 * 25)
        quality_scale = 2.0 ** ((20 - crf) / 6.0)
        return max(250_000, min(240_000_000,
                                int(8_000_000 * scale * quality_scale)))

    def _build_videotoolbox_options(self) -> dict[str, str]:
        """Map project bitrate/CRF intent to VideoToolbox's supported options."""
        bitrate = self._videotoolbox_target_bitrate()
        options = {
            "allow_sw": "0",
            "realtime": "1",
            "prio_speed": "1",
            "b": f"{max(1, round(bitrate / 1000))}k",
        }
        if self._config.max_bitrate is not None:
            options["maxrate"] = f"{max(1, round(self._config.max_bitrate / 1000))}k"
        if self._config.buffer_size is not None:
            options["bufsize"] = f"{max(1, round(self._config.buffer_size / 1000))}k"
        return options

    def _build_amf_options(self, codec_lower: str) -> dict[str, str]:
        """AMD AMF (h264_amf/hevc_amf) 编码选项映射。

        - 码率模式 → rc=vbr_peak（CBR 时 rc=cbr）+ b/maxrate/bufsize，
          与 x264/x265 路径一样走选项字符串（bit_rate 属性是 32 位，
          承载不了高码率）。键名用 ``b``（AVOption 正名）——``b:v`` 是
          ffmpeg CLI 流指定符语法，avcodec_open2 静默忽略（W6-02 引入、
          2026-09-12 复查在 RTX 4090 上实证修正；AMF 侧同修，机制与
          vendor 无关，无 AMD 真机复测）
        - CRF 质量模式 → rc=cqp，crf(0-51) 直接作 QP（qp_i/qp_p/qp_b）
        - x264/x265 风格 preset → AMF quality: speed/balanced/quality
        """
        opts: dict[str, str] = {}
        preset = (self._config.preset or "slow").lower()
        if preset in ("ultrafast", "veryfast", "faster", "fast"):
            opts["quality"] = "speed"
        elif preset == "medium":
            opts["quality"] = "balanced"
        else:
            opts["quality"] = "quality"

        cfg = self._config
        if cfg.bitrate is not None:

            def kbps(value: int) -> str:
                return str(max(1, int(round(value / 1000.0))))

            cbr = (
                cfg.min_bitrate is not None
                and cfg.max_bitrate is not None
                and cfg.min_bitrate == cfg.max_bitrate
            )
            opts["rc"] = "cbr" if cbr else "vbr_peak"
            opts["b"] = f"{kbps(cfg.bitrate)}k"
            if cfg.max_bitrate is not None:
                opts["maxrate"] = f"{kbps(cfg.max_bitrate)}k"
            if cfg.buffer_size is not None:
                opts["bufsize"] = f"{kbps(cfg.buffer_size)}k"
        elif cfg.crf is not None:
            qp = str(max(0, min(51, int(cfg.crf))))
            opts["rc"] = "cqp"
            opts["qp_i"] = qp
            opts["qp_p"] = qp
            if codec_lower == "h264":
                opts["qp_b"] = qp
        return opts

    def _build_nvenc_options(self, codec_lower: str) -> dict[str, str]:
        """NVIDIA NVENC (h264_nvenc/hevc_nvenc/av1_nvenc) 选项映射（W6-01）。

        - x264/x265 风格 preset → NVENC p1..p7（fast→p4 / medium→p5 /
          slow→p6；NVENC 各档编解码时间差异小，取中高档保质量）
        - CRF 质量模式 → rc=vbr + cq（NVENC 无 CRF；cq 0-51 与 x264 CRF
          同量纲，作恒质量目标）
        - 目标码率 → rc=vbr（CBR 时 rc=cbr）+ b/maxrate/bufsize 选项
          （bit_rate 属性是 32 位，高码率溢出——同 AMF 路径教训）。
          注意键名必须是 ``b``（AVCodecContext AVOption 正名）：
          ``b:v`` 是 ffmpeg CLI 的流指定符语法，经 PyAV 的 stream.options
          → avcodec_open2 会被**静默忽略**（2026-09-12 RTX 4090 真机
          实证：请求 20Mbps 输出与不设码率的对照组逐字节一致）。
        """
        opts: dict[str, str] = {}
        preset = (self._config.preset or "medium").lower()
        if preset in ("ultrafast", "superfast", "veryfast", "faster", "fast"):
            opts["preset"] = "p4"
        elif preset == "medium":
            opts["preset"] = "p5"
        else:
            opts["preset"] = "p6"

        cfg = self._config
        if cfg.bitrate is not None:

            def kbps(value: int) -> str:
                return str(max(1, int(round(value / 1000.0))))

            cbr = (
                cfg.min_bitrate is not None
                and cfg.max_bitrate is not None
                and cfg.min_bitrate == cfg.max_bitrate
            )
            opts["rc"] = "cbr" if cbr else "vbr"
            opts["b"] = f"{kbps(cfg.bitrate)}k"
            if cfg.max_bitrate is not None:
                opts["maxrate"] = f"{kbps(cfg.max_bitrate)}k"
            if cfg.buffer_size is not None:
                opts["bufsize"] = f"{kbps(cfg.buffer_size)}k"
        elif cfg.crf is not None:
            opts["rc"] = "vbr"
            opts["cq"] = str(max(0, min(51, int(cfg.crf))))
        return opts

    def _configure_encoder(self) -> None:
        """配置编码器参数"""
        if self._stream is None:
            return

        codec_lower = self._config.codec.lower()

        # Profile 设置（DNxHR 例外：'hqx' 这类短名不是 FFmpeg 的合法
        # profile 名，在其专属分支里用 dnxhr_<name> 选项处理）
        if self._config.profile and codec_lower != "dnxhr":
            try:
                self._stream.profile = self._config.profile
            except Exception as e:
                logger.warning(f"Could not set profile: {e}")

        # GOP codec 特定参数
        # （AUD-33504：vp9 此前不在列表内，crf/bitrate/preset/gop_size
        # 一个都不下发，libvpx-vp9 落到 FFmpeg 默认 256 kb/s；vp9 无硬编
        # 通道——hwaccel_engaged 仅放行 h264/hevc/av1——故只走 CPU 路径）
        if codec_lower in ("h264", "hevc", "vp9", "av1"):
            if self._hw_vendor:
                # 硬编通道不认识 crf/preset/x264-params 等软件编码器选项，
                # 走各自的 rate-control 映射
                if self._hw_vendor == "amf":
                    self._merge_stream_options(
                        self._build_amf_options(codec_lower))
                elif self._hw_vendor == "nvenc":
                    self._merge_stream_options(
                        self._build_nvenc_options(codec_lower))
                elif self._hw_vendor == "videotoolbox":
                    self._merge_stream_options(
                        self._build_videotoolbox_options())
            else:
                # CRF 或目标码率。码率通过 x264/x265 的参数字符串传递，而
                # 不是直接写 VideoCodecContext.bit_rate：PyAV 的 bit_rate
                # 属性在当前版本使用 32 位整数，无法承载 12000 Mbps。
                if self._config.crf is not None:
                    if codec_lower == "vp9":
                        # libvpx-vp9 的 crf 域是 0-63（x264/x265 是 0-51）
                        self._merge_stream_options(
                            {'crf': str(max(0, min(63, int(self._config.crf))))})
                    else:
                        self._merge_stream_options({'crf': str(self._config.crf)})
                elif self._config.bitrate is not None:
                    self._merge_stream_options(self._build_rate_control_options(codec_lower))

                # Preset（libaom-av1 不接受 x264 风格 preset 名，跳过）
                if self._config.preset and codec_lower in ("h264", "hevc"):
                    self._merge_stream_options({'preset': self._config.preset})
                elif self._config.preset and codec_lower == "vp9":
                    # AUD-33504：libvpx 用速度档 cpu-used 承载 preset 语义
                    #（未知 preset 名回退 medium 档）
                    self._merge_stream_options(
                        {'cpu-used': VP9_PRESET_CPU_USED.get(
                            self._config.preset.lower(), "4")})

            # GOP size（AMF/NVENC 同样读取标准 gop_size 作为关键帧间隔）
            if self._config.gop_size:
                self._stream.gop_size = self._config.gop_size

        elif codec_lower == "prores":
            # ProRes profile 通过 pix_fmt 控制
            # ProRes_ks 编码器根据 pix_fmt 自动选择正确的 profile:
            # - yuv422p10le -> ProRes 422 (standard/hq)
            # - prores_422proxy -> ProRes Proxy
            # - prores_422lt -> ProRes LT
            # - prores_422hq -> ProRes 422 HQ
            # - yuva444p10le -> ProRes 4444
            # - yuva444p12le -> ProRes 4444 XQ
            #
            # 注意：pix_fmt 已在 FFmpegEncoderConfig 中根据 profile 设置
            # 这里仅记录配置信息
            logger.debug(
                f"ProRes configured: pix_fmt={self._config.pix_fmt}, "
                f"profile={self._config.profile}"
            )

        elif codec_lower == "dnxhr":
            # FFmpeg 的 DNxHR 编码器名为 dnxhd，profile 用私有选项
            # 'profile' 以 FFmpeg 命名（dnxhr_lb/sq/hq/hqx/444）显式指定；
            # 编码器按 profile 自动选择符合 Avid 规范的码率（含帧率缩放）。
            # 兼容传入全名（'dnxhr_hqx'）或短名（'hqx'）。
            if self._config.profile:
                profile_str = self._config.profile.lower()
                if not profile_str.startswith("dnxhr"):
                    profile_str = f"dnxhr_{profile_str}"
                self._merge_stream_options({"profile": profile_str})
                logger.debug(
                    f"DNxHR configured: profile={profile_str}, "
                    f"pix_fmt={self._config.pix_fmt}"
                )
            else:
                # 无 profile 时按规范表回退 HQ 码率。走 b 选项而非
                # bit_rate 属性——后者是 32 位整数，4K 444 高帧率下
                # (1.4Gbps × fps/30) 会溢出（键名必须是 b，b:v 是
                # ffmpeg CLI 语法，avcodec_open2 静默忽略）。
                bitrate = self._get_dnxhr_bitrate(
                    "hq", self._config.width, self._config.height
                )
                self._merge_stream_options(
                    {"b": f"{max(1, round(bitrate / 1000))}k"}
                )
                logger.warning(
                    "DNxHR profile not specified, using HQ default bitrate"
                )

        # 线程数
        try:
            self._stream.thread_count = self._config.threads
        except Exception:
            pass  #某些编码器不支持多线程设置

    def _merge_stream_options(self, options: dict[str, str]) -> None:
        """Merge codec options without discarding color/quality settings."""
        if self._stream is None or not options:
            return
        try:
            current = self._stream.options or {}
            merged = dict(current)
        except (TypeError, ValueError):
            merged = {}
        merged.update(options)
        self._stream.options = merged

    def _build_rate_control_options(self, codec_lower: str) -> dict[str, str]:
        """Build x264/x265 rate-control options from bps values.

        libx264 and libx265 accept their ``bitrate``/VBV values in kbps. Using
        these codec-specific options also keeps large but valid UI values from
        overflowing PyAV's ``bit_rate`` integer property.
        """
        if self._config.bitrate is None:
            return {}

        def kbps(value: int) -> str:
            return str(max(1, int(round(value / 1000.0))))

        if codec_lower in ("av1", "vp9"):
            # libsvtav1 / libvpx-vp9：走标准 AVOption（b/maxrate/bufsize）——
            # x264/x265 的私有参数串对它们无效（2026-09-12 真机实证：
            # x265-params 被忽略、BRC 停留默认 CRF；b 生效，SVT 配置打印
            # VBR/<目标>）。AUD-33504：vp9 原先根本不会走到这里。
            opts = {"b": f"{kbps(self._config.bitrate)}k"}
            if self._config.max_bitrate is not None:
                opts["maxrate"] = f"{kbps(self._config.max_bitrate)}k"
            if self._config.buffer_size is not None:
                opts["bufsize"] = f"{kbps(self._config.buffer_size)}k"
            return opts

        params = [f"bitrate={kbps(self._config.bitrate)}"]
        if self._config.max_bitrate is not None:
            max_rate = kbps(self._config.max_bitrate)
            params.append(
                f"{'maxrate' if codec_lower == 'h264' else 'vbv-maxrate'}={max_rate}"
            )
        if self._config.min_bitrate is not None and codec_lower == "h264":
            params.append(f"minrate={kbps(self._config.min_bitrate)}")
        if self._config.buffer_size is not None:
            params.append(f"vbv-bufsize={kbps(self._config.buffer_size)}")

        option_name = "x264-params" if codec_lower == "h264" else "x265-params"
        return {option_name: ":".join(params)}

    def _set_color_metadata(self) -> None:
        """设置色彩元数据"""
        if self._stream is None or self._config.color_metadata is None:
            return

        color = self._config.color_metadata

        # FFmpeg 色彩参数映射
        color_primaries_map = {
            "bt709": 1,
            "bt470m": 4,
            "bt470bg": 5,
            "smpte170m": 6,
            "smpte240m": 7,
            "film": 8,
            "bt2020": 9,
            "smpte428": 10,
            "smpte431": 11,
            "smpte432": 12,
        }

        color_trc_map = {
            "bt709": 1,
            "gamma22": 4,
            "gamma28": 5,
            "smpte170m": 6,
            "smpte240m": 7,
            "linear": 8,
            "iec61966_2_1": 13,
            "bt2020_10": 14,
            "bt2020_12": 15,
            "smpte2084": 16,
            "arib-std-b67": 18,
        }

        color_space_map = {
            "gbr": 0,
            "bt709": 1,
            "fcc": 4,
            "bt470bg": 5,
            "smpte170m": 6,
            "smpte240m": 7,
            "ycgco": 8,
            "bt2020nc": 9,
            "bt2020c": 10,
        }

        color_range_map = {
            "limited": 1,
            "full": 2,
            "unknown": 1,  # unknown 默认使用 limited
        }

        try:
            # 设置 color primaries
            if color.primaries in color_primaries_map:
                self._stream.color_primaries = color_primaries_map[color.primaries]

            # 设置 color transfer (trc)
            if color.transfer in color_trc_map:
                self._stream.color_trc = color_trc_map[color.transfer]

            # 设置 color space (matrix)
            if color.matrix in color_space_map:
                self._stream.colorspace = color_space_map[color.matrix]

            # 设置 color range（unknown 默认 limited）
            range_value = color_range_map.get(color.range, 1)  # 默认 limited
            self._stream.color_range = range_value

            # Stage-2: HDR static metadata (ST 2086 mastering display + CTA-861.3
            # MaxCLL/MaxFALL). PyAV/FFmpeg expose these via encoder options
            # (most reliably as x265 params for HEVC); we set them as codec
            # options which PyAV forwards. Each is best-effort — if the codec
            # ignores them, the stream still carries correct primaries/transfer.
            # 硬编通道不承载 x265-params（软编专属；HDR 不进硬编的语义
            # 已在 open() 告警）——跳过注入，避免向 NVENC/AMF 传非法选项。
            if not self._hw_vendor:
                self._inject_hdr_static_metadata(color)

            logger.debug(
                f"Color metadata set: primaries={color.primaries}, "
                f"transfer={color.transfer}, matrix={color.matrix}, range={color.range}"
            )

        except Exception as e:
            logger.warning(f"Could not set color metadata: {e}")

    def _inject_hdr_static_metadata(self, color: "ColorMetadataConfig") -> None:
        """Inject ST 2086 mastering display + MaxCLL/MaxFALL as encoder options.

        PyAV's stream.side_data API varies across versions and codec support
        is uneven, so we set these as codec options (x265-style params) which
        the underlying encoder honours for HEVC HDR10. All fields are
        best-effort: missing/None fields are omitted, and codecs that don't
        understand a key simply ignore it.
        """
        try:
            codec = (self._config.codec or "").lower()
            opts: dict = {}

            # ST 2086 mastering display: "G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)"
            # format expected by x265. max/min luminance in 0.0001 cd/m^2.
            if color.mastering_display is not None and len(color.mastering_display) == 8:
                gx, gy, bx, by, rx, ry, wpx, wpy = color.mastering_display
                # 1000-nit peak, 0.05-nit black (typical HDR10 LCD master).
                md = (f"G({gx*50000:.0f},{gy*50000:.0f})"
                      f"B({bx*50000:.0f},{by*50000:.0f})"
                      f"R({rx*50000:.0f},{ry*50000:.0f})"
                      f"WP({wpx*50000:.0f},{wpy*50000:.0f})"
                      f"L(10000000,50)")
                opts["master-display"] = md

            if color.max_cll is not None:
                opts["max-cll"] = f"{int(color.max_cll)},{int(color.max_fall or 0)}"

            if codec == "hevc":
                # x265 carries ST 2086 / MaxCLL signaling most reliably via the
                # 'x265-params' string (":key=value:key=value" joined). Setting
                # them as independent codec options is NOT honoured by libx265
                # (it only reads its own -x265-params list), so they must be
                # appended to that string. Preserve any existing x265-params
                # (e.g. the HDR10 preset's 'hdr10=1:colorprim=bt2020:...').
                cc = self._stream.codec_context
                try:
                    existing = ""
                    try:
                        existing = cc.options.get("x265-params", "") or ""
                    except Exception:
                        existing = ""
                    if existing and not existing.endswith(":") and existing[-1] not in (":",):
                        existing = existing + ":"
                    parts = [f"{k}={v}" for k, v in opts.items()]
                    joined = ":".join(parts)
                    merged = (existing + joined) if existing else joined
                    if not merged:
                        # SDR 无元数据可注入——不写空 x265-params
                        # （对 x265 是无害噪声，对非软编通道是非法选项）
                        return
                    # options["x265-params"] replaces any prior value.
                    new_opts = {}
                    try:
                        # Copy existing options so we don't drop unrelated keys.
                        new_opts = dict(cc.options or {})
                    except Exception:
                        new_opts = {}
                    new_opts["x265-params"] = merged
                    cc.options = new_opts
                    logger.debug(
                        f"Injected HDR static metadata via x265-params: {merged}"
                    )
                except Exception as inner:
                    logger.debug(
                        f"x265-params merge failed, metadata may be missing: {inner}"
                    )
            elif opts:
                logger.debug(
                    f"HDR static metadata present but codec '{codec}' may not "
                    f"support it; options: {opts}"
                )
        except Exception as e:
            logger.debug(f"HDR static metadata injection skipped: {e}")

    def encode_frame(self, frame: "DecodedFrame") -> None:
        """
        编码单帧。

        pipelined_conversion=True 时把帧交给后台转换线程后立即返回，
        已转换完成的帧在本次调用中编码入容器（保序）；关闭时 drain 余帧。
        False 时同步转换 + 编码。

        Args:
            frame: 解码后的帧

        Raises:
            RuntimeError: 编码器未打开或编码/转换失败
        """
        if not self._is_open:
            raise RuntimeError("Encoder not open. Call open() first.")

        if self._container is None or self._stream is None:
            raise RuntimeError("Container or stream not initialized")

        try:
            import av  # noqa: F401 — PyAV 可用性探测，缺失时抛安装提示
        except ImportError:
            raise RuntimeError("PyAV is not installed")

        if self._pipelined and not self._convert_threads:
            target = (
                self._convert_worker
                if self._convert_workers == 1
                else self._convert_worker_multi
            )
            self._convert_threads = [
                threading.Thread(
                    target=target,
                    name=f"FFmpegVideoEncoder-Convert-{i}",
                    daemon=True,
                )
                for i in range(self._convert_workers)
            ]
            for t in self._convert_threads:
                t.start()

        try:
            pts = self._last_pts
            self._last_pts += 1

            # 零转换快路（2026-09-15 导出提速）：平面帧与编码像素格式一致
            # 时，转换层本就是逐平面直填（无 swscale、无色彩转换），走转换
            # 队列反而引入两次线程交接 + pts 重排开销。转换线程是惰性启动
            # 的——只要此前所有帧都走了内联快路，线程就从未启动，保序性
            # 天然成立；首个需要转换的帧照常启动线程进入队列路径（其后的
            # 帧不再内联，队列内重排只涉及队列中的帧，全局保序不破坏）。
            if not self._convert_threads and self._is_zero_conversion(frame):
                av_frame = self._create_av_frame(frame)
                av_frame.pts = pts
                self._encode_av_frame(av_frame)
                return

            if self._convert_threads:
                # put 满时阻塞 = 背压（in-flight 转换不超过 workers+1 帧）。
                # AUD-33508：多 worker 输出侧背压下，worker 可能因 map 满
                # 停在入表等待、不再消费输入队列，无脑阻塞 put 会与
                # "put 成功后才弹 map" 的消费者位置互等（实测死锁）。
                # 改为交替"尝试入队 / 按序弹出已转换帧"：弹出释放 map
                # 空位 → worker 入表并继续消费队列 → 队列腾位 → put
                # 成功。保活性论证：队列按 pts 严格 FIFO，全部 worker
                # 阻塞入表时 next 帧必然已在 map（可弹）或已旁路入表。
                put_deadline = time.monotonic() + 60.0
                while True:
                    try:
                        self._convert_queue.put((frame, pts), timeout=0.2)
                        break
                    except queue.Full:
                        if self._convert_workers > 1:
                            self._encode_next_converted(block=False)
                        if time.monotonic() > put_deadline:
                            raise RuntimeError(
                                f"Pipeline stalled: frame pts {pts} "
                                f"60s 内无法入转换队列（输出背压未释放）"
                            )
                if self._convert_workers == 1:
                    self._drain_converted(block=False)
                else:
                    self._encode_next_converted(block=False)
                return

            # 创建 PyAV VideoFrame
            av_frame = self._create_av_frame(frame)

            # 设置 PTS
            av_frame.pts = pts

            self._encode_av_frame(av_frame)

        except Exception as e:
            logger.error(f"Encode error at frame {self._frame_count}: {e}")
            # AUD-33508：编码失败即废弃会话——唤醒因输出侧背压/空队列
            # 阻塞的转换线程，保证其退出（不依赖 daemon 兜底）。
            self._convert_aborted = True
            if self._convert_cond is not None:
                with self._convert_cond:
                    self._convert_cond.notify_all()
            raise RuntimeError(f"Encode error: {e}") from e

    def _encode_av_frame(self, av_frame: Any) -> None:
        """编码单个 PyAV 帧（只允许主调用线程执行，避免并发 mux）。"""
        for packet in self._stream.encode(av_frame):
            self._container.mux(packet)

        self._frame_count += 1

        # 进度日志（每 100 帧）
        if self._frame_count % 100 == 0:
            logger.debug(f"Encoded {self._frame_count} frames")

    def _convert_worker(self) -> None:
        """后台转换线程：DecodedFrame → PyAV VideoFrame（单线程保序）。

        swscale/from_ndarray 释放 GIL，可与主线程的 encode 并行。
        """
        while True:
            item = self._convert_queue.get()
            if item is None:
                return
            frame, pts = item
            try:
                av_frame = self._create_av_frame(frame)
                av_frame.pts = pts
                self._converted_queue.put((av_frame, None))
            except BaseException as e:  # noqa: BLE001  转换失败必须传递给主线程
                self._converted_queue.put((None, e))

    def _convert_worker_multi(self) -> None:
        """并行转换线程：DecodedFrame → PyAV VideoFrame（帧级并行）。

        每次 reformat 独立建 swscale 上下文且释放 GIL，整帧并行安全；
        完成序与 pts 无关，主线程按 pts 重排后编码（_encode_next_converted）。
        """
        while True:
            try:
                item = self._convert_queue.get(timeout=0.5)
            except queue.Empty:
                # 空队列轮询：会话废弃（encode/close 失败）时无哨兵可
                # 收，靠此标志退出（AUD-33508 清理保证）
                if self._convert_aborted:
                    return
                continue
            if item is None:
                return
            frame, pts = item
            try:
                av_frame = self._create_av_frame(frame)
                av_frame.pts = pts
                entry = (av_frame, None)
            except BaseException as e:  # noqa: BLE001  转换失败必须传递给主线程
                entry = (None, e)
            # AUD-33508：输出侧背压——map 满时等待空位再入表，防止快
            # 转换帧在 map 中无界积压（内存上界契约见 config 注释）。
            # 例外：编码器正等待的下一 pts（_next_encode_pts）不受容量
            # 限制——输入队列按 pts 严格 FIFO 派发，"map 满且全是非
            # next 帧"时若 next 帧仍在 worker 手中，硬容量会死锁（消费
            # 者等它入表、它等空位）；放行 next 帧保证消费者总能推进、
            # 空位总能释放。
            with self._convert_cond:
                while (
                    len(self._converted_map) >= self._converted_map_capacity
                    and pts != self._next_encode_pts
                    and not self._convert_aborted
                ):
                    self._convert_cond.wait(0.5)
                if self._convert_aborted:
                    return
                self._converted_map[pts] = entry
                self._convert_cond.notify_all()

    def _encode_next_converted(self, block: bool) -> None:
        """按 pts 序编码下一张已转换完成的帧（多转换线程重排语义）。

        block=False：next pts 未就绪即返回（播放式逐帧推进）；
        block=True：等待至就绪（close 排空路径），60s 超时显式报错。
        """
        with self._convert_cond:
            ready = self._convert_cond.wait_for(
                lambda: self._next_encode_pts in self._converted_map,
                timeout=60.0 if block else 0.0,
            )
            if not ready:
                if block:
                    raise RuntimeError(
                        f"Frame conversion timeout: pts {self._next_encode_pts}"
                    )
                return
            entry = self._converted_map.pop(self._next_encode_pts)
            # AUD-33508：唤醒等待 map 空位的转换线程（输出侧背压）
            self._convert_cond.notify_all()
        av_frame, error = entry
        if error is not None:
            logger.error(f"Frame conversion failed: {error}")
            raise RuntimeError(f"Frame conversion error: {error}") from error
        self._encode_av_frame(av_frame)
        self._next_encode_pts += 1

    def _drain_converted(self, block: bool) -> None:
        """把转换完成的帧编码入容器；转换错误在此抛出。"""
        while True:
            try:
                if block:
                    av_frame, error = self._converted_queue.get(timeout=60)
                else:
                    av_frame, error = self._converted_queue.get_nowait()
            except queue.Empty:
                return
            if error is not None:
                logger.error(f"Frame conversion failed: {error}")
                raise RuntimeError(f"Frame conversion error: {error}") from error
            self._encode_av_frame(av_frame)

    def _is_zero_conversion(self, frame: "DecodedFrame") -> bool:
        """判定帧是否命中转换层的同格式直填路径（`_convert_planar_frame`）。

        必须与该函数的实际直填条件保持同构：planar 平面帧、无 packed
        data、像素格式与编码目标一致且同为 yuv 系。任何不确定（字段
        缺失、格式为空）都返回 False——快路只优化可证明为零转换的帧。
        """
        if frame is None or getattr(frame, "data", None) is not None:
            return False
        planes = getattr(frame, "planes", None)
        if not planes:
            return False
        source_format = str(getattr(frame, "pixel_format", "") or "").lower()
        target_format = str(self._encode_pix_fmt or "").lower()
        return bool(
            source_format
            and source_format == target_format
            and source_format.startswith("yuv")
        )

    def _create_av_frame(self, frame: "DecodedFrame") -> Any:
        """
        从 DecodedFrame 创建 PyAV VideoFrame。

        Args:
            frame: DecodedFrame 实例

        Returns:
            PyAV VideoFrame
        """

        # 确定目标格式（AMF 10bit 时是 p010le，与 config.pix_fmt 解耦）
        target_format = self._encode_pix_fmt

        # 根据源帧类型转换
        if frame.is_packed and frame.data is not None:
            # 打包格式（RGB/RGBA）
            av_frame = self._convert_packed_frame(frame, target_format)
        elif frame.is_planar and frame.planes:
            # 平面格式（YUV/YUVA）
            av_frame = self._convert_planar_frame(frame, target_format)
        else:
            # 无数据
            raise RuntimeError(f"Frame has no data: {frame}")

        # 设置时间属性
        # PyAV 17.x 中 self._stream.time_base 可能是 None（未自动设置），
        # 直接赋值 None 会触发 to_avrational 内部 AttributeError。
        # 显式构造 time_base = 1/fps（标准视频 time_base）。
        from fractions import Fraction
        fps_rational = self._config.fps
        if hasattr(fps_rational, 'num') and hasattr(fps_rational, 'den'):
            # 我们的 Rational（num/den 字段）：time_base = den/num
            if fps_rational.num > 0:
                av_frame.time_base = Fraction(int(fps_rational.den), int(fps_rational.num))
            else:
                av_frame.time_base = Fraction(1, 24)
        elif hasattr(fps_rational, 'numerator') and hasattr(fps_rational, 'denominator'):
            if fps_rational.numerator > 0:
                av_frame.time_base = Fraction(int(fps_rational.denominator), int(fps_rational.numerator))
            else:
                av_frame.time_base = Fraction(1, 24)
        else:
            # float 兜底
            fps_float = float(fps_rational)
            av_frame.time_base = Fraction(1, int(round(fps_float))) if fps_float > 0 else Fraction(1, 24)

        return av_frame

    def _convert_packed_frame(self, frame: "DecodedFrame", target_format: str) -> Any:
        """
        转换打包格式帧。

        Args:
            frame: DecodedFrame（打包格式）
            target_format: 目标像素格式

        Returns:
            PyAV VideoFrame
        """
        import av
        import numpy as np

        data = frame.data

        # 创建 PyAV frame
        # 根据源格式和目标格式转换
        source_format = frame.pixel_format.lower()
        target_lower = target_format.lower()

        if 'rgb' in source_format or 'bgr' in source_format:
            # RGB/BGR 源
            # p0 前缀 = 10/16bit 半平面（p010le/p016le，AMF 10bit 输入格式），
            # 同属 YUV 转换家族——swscale 直达，不能掉进 from_ndarray 兜底
            # （PyAV 不支持 p010le 的 from_ndarray）
            if (
                target_lower.startswith('yuv')
                or target_lower.startswith('yuva')
                or target_lower.startswith('p0')
            ):
                # RGB -> YUV 转换
                # PyAV 的 reformat 可以处理这个转换
                # 创建临时 RGB frame
                # Pick the RGB temp format by bit depth: 8-bit -> rgb24,
                # 10/16-bit (HDR / high depth) -> rgb48le so uint16 data
                # survives the RGB->YUV conversion without truncation.
                nch = int(data.shape[-1]) if data.ndim == 3 else 3
                is_high_depth = (
                    (data.dtype == np.uint16)
                    or '48' in source_format or '64' in source_format
                )
                temp_frame = None
                if source_format.startswith('bgr') and data.ndim == 3:
                    channels = nch
                    # 直填格式：8bit → bgr24/bgra；16bit 3 通道 → bgr48le
                    # （16bit 4 通道无直填格式，走下方传统 RGBA 重排路径）
                    if is_high_depth:
                        temp_format = 'bgr48le' if channels == 3 else None
                    else:
                        temp_format = 'bgra' if channels == 4 else 'bgr24'
                    if temp_format is not None and data.flags['C_CONTIGUOUS']:
                        # BGR 直填快路径：绕过 from_ndarray 的逐行跨距拷贝。
                        # 4K 实测：uint8 19→10ms、uint16 85→35ms（含
                        # reformat）。仅在数据连续、行宽与 PyAV 分配的
                        # line_size、总长与 buffer_size 严格一致时启用
                        # （常规分辨率满足，奇数宽自动回退）。输出与传统
                        # flip+rgb48le/rgb24 路径 bit 级一致（实测 bgr24/
                        # bgr48le → yuv420p / yuv422p10le 全平面 diff=0）。
                        try:
                            row_bytes = (
                                data.shape[1] * channels * data.dtype.itemsize
                            )
                            cand = av.VideoFrame(
                                data.shape[1], data.shape[0], temp_format
                            )
                            plane = cand.planes[0]
                            view = memoryview(data)
                            if (
                                plane.line_size == row_bytes
                                and view.nbytes == plane.buffer_size
                            ):
                                plane.update(view)
                                temp_frame = cand
                        except Exception:  # noqa: BLE001  回退传统路径
                            temp_frame = None
                    if temp_frame is None:
                        # 传统路径：以 rgb 系格式喂 PyAV。R2：四通道显式
                        # 重排——整轴 flip 会把 BGRA 变 ARGB；高位深 4ch
                        # 用 rgba64le 保 alpha。3 通道 BGR 8bit 直接以
                        # bgr24 原生格式喂入（无需换序）。
                        if channels == 4:
                            temp_frame = av.VideoFrame.from_ndarray(
                                data[..., [2, 1, 0, 3]],
                                format='rgba64le' if is_high_depth else 'rgba'
                            )
                        elif is_high_depth:
                            temp_frame = av.VideoFrame.from_ndarray(
                                np.flip(data, axis=-1), format='rgb48le'
                            )
                        else:
                            temp_frame = av.VideoFrame.from_ndarray(
                                data, format='bgr24'
                            )
                else:
                    # AUD-33507：RGB 序高位深 4 通道须用 rgba64le 承载
                    # uint16——此前落入 uint8 'rgba'（或 4ch 被当成 rgb48le
                    # 的 3ch 形状），from_ndarray 直接抛 ValueError（与
                    # BGR 分支的正确处理对齐）。
                    if is_high_depth:
                        temp_format = (
                            'rgb48le' if data.shape[-1] == 3 else 'rgba64le'
                        )
                    else:
                        temp_format = (
                            'rgb24' if data.shape[-1] == 3 else 'rgba'
                        )
                    temp_frame = av.VideoFrame.from_ndarray(data, format=temp_format)
                # P0-COLOR-06: pass the stream's colour metadata to libswscale.
                # Without dst_colorspace/dst_color_range swscale picks the YUV
                # matrix by its own defaults (typically BT.601 for RGB input),
                # while the stream is tagged (e.g.) BT.709 in
                # _set_color_metadata — the classic "tagged 709, pixels
                # converted as 601" hue/saturation shift on export.
                # Our RGB data is full-range by construction (0..255 maps
                # 0..1 after the provider encode), and the pixel transfer /
                # primaries were already applied upstream by the provider —
                # the trc/primaries here only tag the output frame.
                # NOTE PyAV enum names differ from FFmpeg strings: swscale
                # colorspace uses the legacy SWS_CS_* enums (ITU709/ITU601/
                # SMPTE240M; BT.2020 = int 9, not exposed as a member), range
                # uses MPEG(limited)/JPEG(full), trc/primaries are CamelCase.
                color = self._config.color_metadata
                reformat_kwargs = {}
                if color is not None:
                    matrix_to_sws = {
                        'bt709': 'ITU709',
                        'smpte170m': 'ITU601',
                        'bt470bg': 'ITU601',
                        'smpte240m': 'SMPTE240M',
                        'bt2020nc': 9,   # SWS_CS_BT2020 (== AVCOL_SPC_BT2020NC)
                        'bt2020c': 9,
                    }
                    if color.matrix in ('bt2020nc', 'bt2020c'):
                        # PyAV/swscale 的 dst_colorspace 枚举（ITU709/ITU601/
                        # SMPTE240M）不支持 BT.2020——传 int 9 会被静默忽略
                        # 并回退 BT.601（实测红块 Y=326≈601 权重）。BT.2020
                        # 用 numpy 精确转换（与解码端 ColorConverter 同系数）。
                        if data.ndim == 3 and data.shape[-1] == 4:
                            # R2：BT.2020 精确转换按 3 通道设计——4ch 输入的
                            # alpha 无承载路径，显式拒绝而非静默丢弃
                            # （整改记录见 ADR-C013 遗留项）。
                            raise RuntimeError(
                                "FFmpegVideoEncoder: BT.2020 矩阵 + Alpha 输入"
                                "的精确转换尚未实现（swscale 无 BT.2020 枚举，"
                                "numpy 路径无 alpha 承载）——请选择非 BT.2020 "
                                "矩阵或 Topos 编码器"
                            )
                        # R-04：BT.2020 numpy 转换按 RGB 语义读取前三通道。
                        # 合成 HDR10/HLG 提交 bgr48le/bgr24——不换序会把
                        # 通道 0 当 R（实为 B），红/蓝互换（灰阶测试不暴露：
                        # 红 BGR 实测 Y=116，正确 294）。
                        bt2020_input = data
                        if source_format.startswith('bgr') and data.ndim == 3:
                            bt2020_input = np.ascontiguousarray(data[..., ::-1])
                        return self._convert_rgb_to_yuv_bt2020(
                            bt2020_input, color, target_format
                        )
                    trc_to_av = {
                        'bt709': 'BT709', 'gamma22': 'GAMMA22', 'gamma28': 'GAMMA28',
                        'smpte170m': 'SMPTE170M', 'smpte240m': 'SMPTE240M',
                        'linear': 'LINEAR', 'iec61966_2_1': 'IEC61966_2_1',
                        'bt2020_10': 'BT2020_10', 'bt2020_12': 'BT2020_12',
                        'smpte2084': 'SMPTE2084', 'arib-std-b67': 'ARIB_STD_B67',
                    }
                    prim_to_av = {
                        'bt709': 'BT709', 'bt470m': 'BT470M', 'bt470bg': 'BT470BG',
                        'smpte170m': 'SMPTE170M', 'smpte240m': 'SMPTE240M',
                        'film': 'FILM', 'bt2020': 'BT2020', 'smpte428': 'SMPTE428',
                        'smpte431': 'SMPTE431', 'smpte432': 'SMPTE432',
                    }
                    if color.matrix in matrix_to_sws:
                        reformat_kwargs['dst_colorspace'] = matrix_to_sws[color.matrix]
                    reformat_kwargs['src_color_range'] = 'JPEG'  # our RGB is full-range
                    reformat_kwargs['dst_color_range'] = (
                        'JPEG' if color.range == 'full' else 'MPEG'
                    )
                    if color.transfer in trc_to_av:
                        reformat_kwargs['dst_color_trc'] = trc_to_av[color.transfer]
                    if color.primaries in prim_to_av:
                        reformat_kwargs['dst_color_primaries'] = prim_to_av[color.primaries]
                av_frame = temp_frame.reformat(format=target_format, **reformat_kwargs)
            else:
                # RGB -> RGB（直接使用）
                # 注意：大多数编码器不支持 RGB 输入
                av_frame = av.VideoFrame.from_ndarray(data, format=target_format)

        else:
            # 其他格式，直接尝试 ndarray -> VideoFrame
            av_frame = av.VideoFrame.from_ndarray(data, format=target_format)

        return av_frame

    def _convert_rgb_to_yuv_bt2020(self, data: Any, color: "ColorMetadataConfig",
                                   target_format: str) -> Any:
        """RGB → BT.2020nc YUV 的 numpy 精确转换（P0-COLOR-06）。

        PyAV/swscale 的 dst_colorspace 枚举不含 BT.2020（int 9 被静默忽略、
        回退 BT.601），而 HDR10/HLG 导出要求 BT.2020nc 矩阵与流 tag 一致。
        本方法用与解码端 ColorConverter.BT2020_NCL_MATRIX 相同的系数做
        正向转换，支持 limited/full range 与 4:2:0/4:2:2/4:4:4 子采样。

        Args:
            data: RGB 数组（H, W, 3），uint8 或 uint16（full-range）
            color: 流色彩元数据（range 决定压缩）
            target_format: 目标像素格式（yuv420p10le 等）

        Returns:
            PyAV VideoFrame
        """
        import av
        import numpy as np
        import re

        h, w = data.shape[:2]
        # 输入归一化（uint8/uint16 full-range RGB）
        rgb = data.astype(np.float32)
        rgb /= 255.0 if data.dtype == np.uint8 else 65535.0
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]

        # BT.2020 NCL 正向（BT.2100 表9）：Y = 0.2627R + 0.6780G + 0.0593B
        y = 0.2627 * r + 0.6780 * g + 0.0593 * b
        u = (b - y) / 1.8814   # 2(1-0.0593)
        v = (r - y) / 1.4746   # 2(1-0.2627)

        # 输出位深由目标格式决定（yuv420p10le → 10-bit 值域 0..1023），
        # 与输入容器位深（rgb48le 是 16-bit 容器）无关。
        m = re.search(r'p(\d+)(?:le|be)?\Z', target_format.lower())
        out_depth = int(m.group(1)) if m else 8
        scale = float(1 << out_depth) - 1.0
        # Limited 跨度按 2^(n-8) 精确位移（不是 scale/255≈4.012）：
        # 8-bit [16,235]/[16,240] → 10-bit [64,940]/[64,960] 是 4 倍而非
        # 1023/255 倍——色度中心固定在半量程（512），幅度 ±(112<<s)。
        depth_shift = max(0, out_depth - 8)
        if color.range == 'full':
            y_q = y * scale
            u_q = (u + 0.5) * scale
            v_q = (v + 0.5) * scale
        else:
            y_q = y * (219 << depth_shift) + float(16 << depth_shift)
            u_q = (u + 0.5) * (224 << depth_shift) + float(16 << depth_shift)
            v_q = (v + 0.5) * (224 << depth_shift) + float(16 << depth_shift)

        dtype = np.uint16 if out_depth > 8 else np.uint8
        y_p = np.clip(np.rint(y_q), 0, scale).astype(dtype)
        u_p = np.clip(np.rint(u_q), 0, scale).astype(dtype)
        v_p = np.clip(np.rint(v_q), 0, scale).astype(dtype)

        # 色度子采样（box 2x2 平均，与 swscale 的 area 行为一致）
        fmt = target_format.lower()
        # p010/p016 是 4:2:0 半平面家族（名称不含 '420'，需前缀识别）
        if '420' in fmt or fmt.startswith('p0'):
            div = (2, 2)
        elif '422' in fmt:
            div = (2, 1)
        else:
            div = (1, 1)

        def _subsample(plane: np.ndarray, dv: tuple) -> np.ndarray:
            if dv == (1, 1):
                return plane
            ph, pw = plane.shape
            ch, cw = ph // dv[0], pw // dv[1]
            cropped = plane[:ch * dv[0], :cw * dv[1]]
            return np.rint(
                cropped.reshape(ch, dv[0], cw, dv[1]).mean(axis=(1, 3))
            ).astype(plane.dtype)

        u_s = _subsample(u_p, div)
        v_s = _subsample(v_p, div)

        if fmt.startswith('p0'):
            # p010/p016 半平面族（AMF 10bit 输入）：2 平面（Y + UV 交错）
            # 且 16bit 容器 MSB 对齐——实测 swscale yuv420p10le 500 →
            # p010le 原始 32000 = 500<<6。直写 out_depth 位值会把亮度
            # 缩小 2^(16-depth) 倍；按 3 平面 zip 填充则 V 整个丢失。
            shift = np.uint16(16 - out_depth)
            uv = np.empty((u_s.shape[0], u_s.shape[1] * 2), dtype=dtype)
            uv[:, 0::2] = u_s
            uv[:, 1::2] = v_s
            planes = (y_p << shift, uv << shift)
        else:
            planes = (y_p, u_s, v_s)

        # PyAV 17 无 from_planar：创建空 frame 后逐 plane 填充，
        # 按 line_size 处理 stride 对齐（宽度非 16/32 倍数时 plane 有 padding）。
        frame = av.VideoFrame(w, h, target_format)
        for av_plane, arr in zip(frame.planes, planes):
            itemsize = arr.dtype.itemsize
            row_bytes = arr.shape[1] * itemsize
            ls = int(av_plane.line_size or row_bytes)
            flat = np.ascontiguousarray(arr).view(np.uint8).reshape(
                arr.shape[0], row_bytes)
            if ls == row_bytes:
                payload = flat.tobytes()
            else:
                buf = np.zeros((arr.shape[0], ls), dtype=np.uint8)
                buf[:, :row_bytes] = flat
                payload = buf.tobytes()
            av_plane.update(payload)
        # tag 输出帧色彩（流级 tag 在 _set_color_metadata 已设置）
        try:
            from av.video.reformatter import ColorPrimaries, ColorRange, ColorTrc
            frame.color_primaries = ColorPrimaries.BT2020
            frame.color_trc = {
                'smpte2084': ColorTrc.SMPTE2084,
                'arib-std-b67': ColorTrc.ARIB_STD_B67,
            }.get(color.transfer, ColorTrc.UNSPECIFIED)
            frame.color_range = (
                ColorRange.JPEG if color.range == 'full' else ColorRange.MPEG
            )
        except Exception:
            pass
        return frame

    def _convert_planar_frame(self, frame: "DecodedFrame", target_format: str) -> Any:
        """
        转换平面格式帧。

        Args:
            frame: DecodedFrame（平面格式）
            target_format: 目标像素格式

        Returns:
            PyAV VideoFrame
        """
        source_format = frame.pixel_format.lower()
        target_lower = target_format.lower()

        # YUV/YUVA 格式
        is_source_yuv = source_format.startswith('yuv') or source_format.startswith('yuva')
        is_target_yuv = target_lower.startswith('yuv') or target_lower.startswith('yuva')

        if is_source_yuv and is_target_yuv and source_format == target_lower:
            # 完全同格式：逐平面字节级直接填充（无重采样、无色彩转换）
            return self._create_planar_av_frame(frame, source_format)

        # 其余情况统一走 swscale reformat。注意同子采样跨格式（如
        # yuv420p10le→p010le 半平面 2 平面）也必须 reformat：按平面序
        # 拷贝会因平面数/布局不同静默丢平面（W6-02 10bit 接线时修复）
        source_av_frame = self._create_planar_av_frame(frame, source_format)
        return source_av_frame.reformat(format=target_format)

    def _create_planar_av_frame(self, frame: "DecodedFrame", format_name: str) -> Any:
        """
        从 DecodedFrame 的 planes 创建 PyAV VideoFrame（字节级平面填充）。

        注意：PyAV 的 VideoPlane 只暴露扁平 uint8 缓冲（np.array(plane)
        形状是 (buffer_size,) 而非 (H, W)）——按形状匹配的「直接填充」
        从未生效过（历史遗留：所有平面被静默跳过，输出黑帧）。本实现
        按 dtype itemsize × 行宽显式换算字节，并处理 line_size 对齐。

        Args:
            frame: DecodedFrame（平面格式）
            format_name: 像素格式名称

        Returns:
            PyAV VideoFrame
        """
        import av
        import numpy as np

        if frame.is_packed and frame.data is not None:
            # 有打包数据，直接使用
            temp_frame = av.VideoFrame.from_ndarray(frame.data, format='rgb24')
            return temp_frame.reformat(format=format_name)

        av_frame = av.VideoFrame(frame.width, frame.height, format_name)

        for i, plane_data in enumerate(frame.planes):
            if i >= len(av_frame.planes):
                break
            av_plane = av_frame.planes[i]
            itemsize = plane_data.dtype.itemsize
            rows = plane_data.shape[0]
            row_bytes = plane_data.shape[1] * itemsize
            ls = int(av_plane.line_size or row_bytes)
            # 平面通常已 C 连续：ascontiguousarray 是零拷贝视图；再经
            # buffer 协议直传给 plane.update，避免 tobytes() 的整帧
            # 中间拷贝（2026-09-15 导出提速：每帧省 ~3MB 拷贝）。
            flat = np.ascontiguousarray(plane_data).view(np.uint8).ravel()
            if ls == row_bytes:
                av_plane.update(flat)
            else:
                # line_size 对齐（宽度非对齐边界时 plane 带行填充）
                buf = np.zeros((rows, ls), dtype=np.uint8)
                buf[:, :row_bytes] = flat.reshape(rows, row_bytes)
                av_plane.update(buf.reshape(-1))

        return av_frame

    def add_audio_stream(
        self,
        codec: str = "aac",
        sample_rate: int = 48000,
        channels: int = 2,
        bitrate: Optional[int] = None,
    ) -> None:
        """
        添加音频流到输出容器。

        必须在 open() 之后、close() 之前调用。

        Args:
            codec: 音频编码器名称 (aac, pcm_s16le, pcm_s24le, flac, libmp3lame)
            sample_rate: 采样率
            channels: 声道数
            bitrate: 比特率 (kbps)，仅用于压缩编码器
        """
        if not self._is_open or self._container is None:
            raise RuntimeError("Encoder not open. Call open() first.")

        try:
            import av  # noqa: F401 — PyAV 可用性探测，缺失时抛安装提示
        except ImportError:
            raise RuntimeError("PyAV is not installed")

        self._audio_stream = self._container.add_stream(
            codec,
            rate=sample_rate,
        )
        # AM-08 阶段 4.1：用标准 ChannelLayout 推导 layout。
        # 此前 channels>2 被错误设为 'mono'，导致 5.1 导出声道映射错误。
        # AUD-33509：推导结果存入 self._audio_layout_name 作为单一真相，
        # write_audio_samples 的帧布局复用同一名字，消除流侧/帧侧两套
        # 推导（'5.1' vs '6c'）的失配。
        try:
            from src.features.edit.core.channel_layout import ChannelLayout
            layout_name = ChannelLayout.from_channel_count(channels).av_layout_name
        except ImportError:
            # ChannelLayout 不可用（shared 层脱离 edit 特性栈独立使用）：
            # 回退 PyAV 朴素命名——与帧侧同源，绝不再回退 'mono'（把
            # 多声道流标成单声道只会制造必然的流/帧失配）。
            if channels == 1:
                layout_name = 'mono'
            elif channels == 2:
                layout_name = 'stereo'
            else:
                layout_name = f'{channels}c'
            logger.debug(
                "ChannelLayout unavailable; using PyAV layout %r for %d ch",
                layout_name, channels,
            )
        # 非标准声道数（3/4/5/7…）：ChannelLayout.from_channel_count 抛出的
        # ValueError 显式上抛（AUD-33509：显式报错优于静默降 mono）。
        # 已知调用方（timeline_export 两处）均以 try/except 包裹并降级为
        # 无音频导出，不会中断导出本身。
        self._audio_stream.layout = layout_name
        self._audio_layout_name = layout_name

        if bitrate:
            # AUD-33505：键名必须是 'b'（AVCodecContext AVOption 正名）。
            # 'b:a' 是 ffmpeg CLI 的流指定符语法，经 stream.options →
            # avcodec_open2 会被静默忽略（视频侧同根因缺陷的实证见
            # _configure_video_bitrate 的注释）。
            self._audio_stream.options = {'b': f'{bitrate}k'}

        logger.info(
            f"Audio stream added: codec={codec}, "
            f"rate={sample_rate}, channels={channels}"
        )

    def write_audio_samples(self, samples: "np.ndarray", sample_rate: int = 48000) -> None:
        """
        写入音频采样数据。

        Args:
            samples: 音频数据数组，shape=(channels, samples) 或 (samples,)
                     dtype 必须为 float32，值范围 -1.0 ~ 1.0
            sample_rate: 采样率

        Raises:
            RuntimeError: 编码器未打开或无音频流
            ValueError: samples.dtype 不满足 float32 契约（AUD-33509）
        """
        if not self._is_open:
            raise RuntimeError("Encoder not open.")
        if self._audio_stream is None:
            raise RuntimeError("No audio stream. Call add_audio_stream() first.")
        if self._container is None:
            raise RuntimeError("Container not initialized.")

        try:
            import av
            import numpy as np
        except ImportError:
            raise RuntimeError("PyAV/numpy is not installed")

        if samples.ndim == 1:
            samples = samples.reshape(1, -1)

        # AUD-33509：帧声道布局与流布局同源（add_audio_stream 的推导
        # 结果单一真相），消除流侧 '5.1' / 帧侧 '6c' 两套推导的失配。
        layout = self._audio_layout_name
        if layout is None:
            layout = self._audio_stream.layout.name
        channels = samples.shape[0]
        stream_channels = int(self._audio_stream.layout.nb_channels)
        if channels != stream_channels:
            raise RuntimeError(
                f"Audio channel mismatch: stream layout {layout!r} has "
                f"{stream_channels} channels but samples has {channels}; "
                f"adapt PCM to the stream layout before writing "
                f"(add_audio_stream(channels=...) 与混音输出须同源)"
            )
        # AUD-33509：docstring 契约显式校验——此前 float64 输入在
        # from_ndarray 处抛裸 ValueError，错误信息不含修复指引。
        if samples.dtype != np.float32:
            raise ValueError(
                f"write_audio_samples 契约为 float32 PCM（-1.0~1.0），"
                f"收到 {samples.dtype}；请在调用侧 astype(np.float32)"
            )

        # 分块写入以避免内存问题
        block_size = 1024
        total_samples = samples.shape[1]
        offset = 0

        while offset < total_samples:
            end = min(offset + block_size, total_samples)
            block = samples[:, offset:end]

            av_frame = av.AudioFrame.from_ndarray(block, format='fltp', layout=layout)
            av_frame.sample_rate = sample_rate

            for packet in self._audio_stream.encode(av_frame):
                self._container.mux(packet)

            offset = end

    def close(self) -> None:
        """
        关闭编码器并写入文件。

        编码结束帧并关闭容器，确保文件完整写入。

        Raises:
            Exception: flush/drain/容器关闭中途失败时上抛（AUD-33506）。
                异常路径会 best-effort 关闭容器并删除半成品输出文件
                （与 open() 失败路径对齐），编码器回到未打开状态，
                资源不依赖 PyAV dealloc 兜底。
        """
        if not self._is_open:
            return

        container_closed: bool = False
        try:
            # 停转换线程并编码其余已入队帧（保序：必须先于 flush）
            if self._convert_threads:
                if self._convert_workers == 1:
                    for _ in self._convert_threads:
                        self._convert_queue.put(None)
                    for t in self._convert_threads:
                        t.join(timeout=60)
                        if t.is_alive():
                            logger.warning(
                                "转换线程未在 60s 内退出，部分尾部帧可能缺失"
                            )
                    self._convert_threads = []
                    self._drain_converted(block=False)
                else:
                    # AUD-33508：多 worker 下必须先排空再投递哨兵——
                    # worker 可能因输出侧背压阻塞在入表等待而不再消费
                    # 输入队列，先 put(None) 会在满队列上永久阻塞（死
                    # 锁）。排空条件：全部已入队帧（_last_pts）均按序
                    # 编码完成（_next_encode_pts 追平）。此时 worker 手
                    # 中无待处理项、队列必空，哨兵投递与 join 立即完成。
                    while self._next_encode_pts < self._last_pts:
                        self._encode_next_converted(block=True)
                    for _ in self._convert_threads:
                        self._convert_queue.put(None)
                    for t in self._convert_threads:
                        t.join(timeout=60)
                        if t.is_alive():
                            logger.warning(
                                "转换线程未在 60s 内退出，部分尾部帧可能缺失"
                            )
                    self._convert_threads = []
                    # 哨兵退出后 map 必空；防御性校验（判空须持
                    # _convert_cond——join 超时线程仍存活时无锁读 dict
                    # 真值有竞态）。
                    while True:
                        with self._convert_cond:
                            if not self._converted_map:
                                break
                        self._encode_next_converted(block=True)

            # 编码结束帧（flush video）
            if self._stream is not None:
                for packet in self._stream.encode():
                    if self._container is not None:
                        self._container.mux(packet)

            # 编码结束帧（flush audio）
            if self._audio_stream is not None:
                for packet in self._audio_stream.encode():
                    if self._container is not None:
                        self._container.mux(packet)

            # 关闭容器
            if self._container is not None:
                self._container.close()
            container_closed = True

            # M21.1（R04）：发布事务——flush/trailer/容器关闭全部完成
            # 后才原子替换到最终路径。存在同名旧成片时，编码中途任何
            # 失败/异常都只清临时文件，旧成功输出保持不动。
            if self._write_path != self._output_path \
                    and os.path.exists(self._write_path):
                os.replace(self._write_path, self._output_path)
            elif self._write_path != self._output_path:
                # 生产路径 av.open(mode='w') 在 open 即创建临时文件，
                # 此分支只可能是替身/异常环境——不发布也不报错（无
                # 半成品可替换），最终路径保持原状。
                logger.debug(
                    "Publish skipped: no temp output at %s",
                    self._write_path)

            logger.info(
                f"Encoder closed: {self._output_path}, "
                f"total_frames={self._frame_count}"
            )

        except Exception as e:
            logger.exception(f"Error closing encoder: {e}")
            # AUD-33508：会话废弃——唤醒因输出侧背压/空队列阻塞的转换
            # 线程，保证其退出（不依赖 daemon 兜底）。
            self._convert_aborted = True
            if self._convert_cond is not None:
                with self._convert_cond:
                    self._convert_cond.notify_all()
            # AUD-33506：异常路径资源/半成品清理，与 open() 失败路径
            # （N06 防线的 stream/container close + os.remove）对齐：
            # (1) 容器 best-effort 显式关闭——trailer/句柄释放不依赖
            #     PyAV dealloc 兜底；
            # (2) 删除半成品输出并留警告——静默留下损坏文件比显式失败
            #     更危险，导出 outcome 以本异常统一裁决。
            if self._container is not None and not container_closed:
                try:
                    self._container.close()
                except Exception as close_exc:  # noqa: BLE001 — 不掩盖原始异常
                    logger.warning(
                        f"Best-effort container close failed: {close_exc}"
                    )
            try:
                if os.path.exists(self._write_path):
                    os.remove(self._write_path)
                    logger.warning(
                        f"Removed partial output after close failure: "
                        f"{self._write_path}"
                    )
            except OSError as rm_exc:
                logger.warning(
                    f"Failed to remove partial output "
                    f"{self._write_path}: {rm_exc}"
                )
            raise

        finally:
            self._container = None
            self._stream = None
            self._audio_stream = None
            self._audio_layout_name = None
            self._is_open = False

    def get_progress(self) -> float:
        """
        返回编码进度。

        Note: 需要外部提供总帧数才能计算进度。

        Returns:
            已编码帧数（进度需要外部计算）
        """
        # 返回帧数，进度百分比需要外部根据总帧数计算
        return float(self._frame_count)

    def get_progress_percentage(self, total_frames: int) -> float:
        """
        返回编码进度百分比。

        Args:
            total_frames: 总帧数

        Returns:
            进度百分比（0.0 - 1.0）
        """
        if total_frames <= 0:
            return 0.0
        return self._frame_count / total_frames


# =============================================================================
# 辅助函数
# =============================================================================


def create_encoder_config(
    metadata: "VideoMetadata",
    codec: str = "prores",
    profile: Optional[str] = None,
) -> FFmpegEncoderConfig:
    """
    创建编码器配置的便捷函数。

    Args:
        metadata: 视频元数据
        codec: 编码器名称
        profile: 编码器 profile

    Returns:
        FFmpegEncoderConfig 实例
    """
    return FFmpegEncoderConfig.from_metadata(metadata, codec, profile)


def get_recommended_codec(
    metadata: "VideoMetadata",
    output_format: str = "mov",
) -> str:
    """
    根据元数据和输出格式推荐编码器。

    Args:
        metadata: 视频元数据
        output_format: 输出容器格式

    Returns:
        推荐的编码器名称
    """
    format_lower = output_format.lower()

    # ProRes/DNxHR 推荐（专业编辑格式）
    if format_lower in ("mov", "qt"):
        if metadata.has_alpha:
            return "prores"  # ProRes 4444
        else:
            return "prores"  # ProRes 422 HQ

    # H.264/HEVC 推荐（通用格式）
    if format_lower in ("mp4", "m4v"):
        if metadata.is_hdr:
            return "hevc"  # HDR 使用 HEVC
        else:
            return "h264"

    # VP9/AV1 推荐（Web 格式）
    if format_lower == "webm":
        return "vp9"

    # FFV1 推荐（无损存档）
    if format_lower in ("mkv", "avi"):
        return "ffv1"

    # 默认
    return "prores"
