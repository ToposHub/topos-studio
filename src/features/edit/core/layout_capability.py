"""Audio stage layout capabilities for the explicit programme graph.

Track and Master layouts are opt-in. Legacy timelines keep their existing
routing defaults. The DSP stage advertises buffer support; individual effects
must pass the per-layout validation in native_mix_layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from src.features.edit.core.channel_layout import (
    ChannelLayout,
    downmix_matrix,
)

DSP_CORE_LAYOUT = ChannelLayout.STEREO  # Legacy playback default.
_NATIVE_LAYOUTS = (
    ChannelLayout.MONO, ChannelLayout.STEREO,
    ChannelLayout.SURROUND_5_1, ChannelLayout.SURROUND_7_1,
)

_SUPPORTED = {stage: _NATIVE_LAYOUTS for stage in (
    "source_decode", "clip_stage", "track_dsp", "bus_routing", "bus_master", "export",
)}

_REASON = {
    stage: "Supports mono, stereo, 5.1 and 7.1 only" for stage in _SUPPORTED
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


_ADAPTATION = {
    "source_decode": "Container layout and source patch determine clip input",
    "clip_stage": "Explicit speaker routing at the track input",
    "track_dsp": "Declared track width with layout-compatible effects",
    "bus_routing": "Speaker matrix at every output and send edge",
    "bus_master": "Bus and Master retain their own declared layouts",
    "export": "Convert the final programme to the requested delivery layout",
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
