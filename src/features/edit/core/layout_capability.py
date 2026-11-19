"""MX-15：布局能力矩阵（声道格式、输入输出 Patch 与降混）。

把 source→clip→track→bus→master/export 各段实际支持的声道布局声明为
单一真源（计划 §5 MX-15："建立 source→clip→track→bus→device/export 的
布局能力矩阵"）。UI/导出/诊断读此声明，不再以菜单存在证明能力。

矩阵如实反映当前实现（2026-11）：

- source_decode  素材解码支持 mono/stereo/5.1/7.1；未知布局回退
                 截断/复制并逐次告警（adapt_samples_for_delivery）。
- clip_stage     源布局 → 轨域声道数的标准 ITU downmix/复制
                 （RMA-08：替代 naive 截断；Center/LFE 不丢弃）。
- track_dsp      轨链（volume/pan/EQ/FX/Dynamics）在立体声域处理；
                 SpatialPanner 为 2×2 矩阵（PAN-02 VBAP 后扩展）。
- bus_master     Bus/Master 求和与处理为立体声域。
- export         交付容器支持 stereo/5.1/7.1（resolve_export_channels
                 为单一真相）；立体声混音上混到 5.1/7.1 走显式回退
                 （复制首声道 + 告警），不做假环绕。
- height         7.1.4/Atmos/高度声道不支持——能力查询返回 False 并
                 给出原因（不静默、不截断冒充）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from src.features.edit.core.channel_layout import (
    ChannelLayout,
    downmix_matrix,
)

# 处理域：轨链/Bus/Master 的 DSP 核心布局
DSP_CORE_LAYOUT = ChannelLayout.STEREO

# 各段支持的布局（声明即验收口径；变更须同步测试）
_SUPPORTED: dict = {
    "source_decode": (
        ChannelLayout.MONO, ChannelLayout.STEREO,
        ChannelLayout.SURROUND_5_1, ChannelLayout.SURROUND_7_1,
    ),
    "clip_stage": (
        ChannelLayout.MONO, ChannelLayout.STEREO,
        ChannelLayout.SURROUND_5_1, ChannelLayout.SURROUND_7_1,
    ),
    # 轨链在立体声域：多声道素材在 clip 阶段已标准降混
    "track_dsp": (ChannelLayout.STEREO,),
    # P2 环绕 Bus 路由核心：Bus 布局可声明 5.1/7.1（轨→Bus 上混、
    # Bus→Master 降混走 routing_matrix）；Master 求和仍立体声域。
    "bus_routing": (
        ChannelLayout.STEREO, ChannelLayout.SURROUND_5_1,
        ChannelLayout.SURROUND_7_1,
    ),
    "bus_master": (ChannelLayout.STEREO,),
    "export": (
        ChannelLayout.STEREO, ChannelLayout.SURROUND_5_1,
        ChannelLayout.SURROUND_7_1,
    ),
}

# 各段不支持时的原因（诊断用；计划 §2.3 约束 3："不支持组合明确诊断"）
_REASON: dict = {
    "source_decode": "素材布局未知——回退截断/复制并告警（adapt_samples_for_delivery）",
    "clip_stage": "源布局→轨域使用标准 ITU downmix；未知布局回退并告警",
    "track_dsp": "轨链 DSP 为立体声域；多声道在 clip 阶段已降混（Panner 2×2，"
                 "VBAP 为 PAN-02 待办）",
    "bus_routing": "Bus 布局支持 stereo/5.1/7.1；高度声道（x.y.z）不支持",
    "bus_master": "Bus/Master 求和为立体声域",
    "export": "交付容器仅支持 stereo/5.1/7.1；高度声道（7.1.4/Atmos）不支持",
}


@dataclass(frozen=True)
class StageCapability:
    """单段布局能力声明。"""
    stage: str
    layouts: Tuple[ChannelLayout, ...]
    unsupported_reason: str
    adaptation: str

    def supports(self, layout: ChannelLayout) -> bool:
        return layout in self.layouts


_ADAPTATION: dict = {
    "source_decode": "解码器读容器布局；未知布局逐次告警后安全回退",
    "clip_stage": "标准 ITU BS.775 downmix 矩阵（Lo/Ro；LFE 默认排除可显式包含）",
    "track_dsp": "多声道素材在 clip 阶段降混进立体声轨域",
    "bus_routing": "轨→Bus 上混与 Bus→Master 降混走 routing_matrix"
                   "（upmix：L/R 直通占位、mono 进 C；downmix：BS.775，LFE 排除）",
    "bus_master": "轨/Bus 输出在立体声域求和",
    "export": "resolve_export_channels 单一真相；混音域→交付布局经"
              " adapt_samples_for_delivery（不支持组合回退 + 告警）",
}


def stage_capability(stage: str) -> Optional[StageCapability]:
    """查询某段的能力声明；未知段返回 None。"""
    if stage not in _SUPPORTED:
        return None
    return StageCapability(
        stage=stage,
        layouts=_SUPPORTED[stage],
        unsupported_reason=_REASON[stage],
        adaptation=_ADAPTATION[stage],
    )


def all_stages() -> Tuple[str, ...]:
    """全部已声明段名。"""
    return tuple(_SUPPORTED.keys())


def layout_supported(stage: str, layout: ChannelLayout) -> bool:
    """某段是否支持某布局。"""
    cap = stage_capability(stage)
    return bool(cap and cap.supports(layout))


def unsupported_reason(stage: str, layout: ChannelLayout) -> Optional[str]:
    """某段不支持某布局时的诊断原因（支持时 None）。"""
    cap = stage_capability(stage)
    if cap is None:
        return f"未知能力段：{stage!r}（已声明：{all_stages()}）"
    if cap.supports(layout):
        return None
    return (f"{stage} 段不支持 {layout.av_layout_name}："
            f"{cap.unsupported_reason}")


def downmix_supported(src: ChannelLayout, dst: ChannelLayout) -> bool:
    """src→dst 是否有标准 downmix 矩阵（不实际构建，捕获 ValueError）。"""
    try:
        downmix_matrix(src, dst)
        return True
    except ValueError:
        return False


def supported_downmix_pairs() -> Tuple[Tuple[ChannelLayout, ChannelLayout], ...]:
    """全部支持的标准 downmix 组合（含同布局直通；不含 upmix）。"""
    layouts = (
        ChannelLayout.MONO, ChannelLayout.STEREO,
        ChannelLayout.SURROUND_5_1, ChannelLayout.SURROUND_7_1,
    )
    return tuple(
        (s, d) for s in layouts for d in layouts if downmix_supported(s, d)
    )
