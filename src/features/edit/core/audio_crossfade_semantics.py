"""M14.2b · 子帧 crossfade 与 Stem 的采样域语义（模型层）。

F3.3 三字段（audio_sample_offset/source_sample_offset/fade_*_samples）
在 crossfade 与 Stem 两条消费路径上的统一换算——预览/成片/Stem 三路
同语义（计划 §M14 验收句）。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


def crossfade_gain_envelope(
    total_samples: int,
    fade_in_samples: int = 0,
    fade_out_samples: int = 0,
    *,
    curve: str = "linear",
) -> Tuple["Any", "Any"]:
    """子帧 crossfade 的增益包络（等功率/线性）。

    返回 (fade_in_gains, fade_out_gains)——两个 1D 数组。等功率
    （constant-power）交叉点两增益各 √0.5，总能量保持；linear 交叉点
    两增益各 0.5（有声像下潜）。采样域整数样本粒度（子帧偏移经
    ClipInstance.audio_sample_offset 已折算进窗口起点）。
    """
    import numpy as np

    def _ramp(n: int) -> "Any":
        if n <= 0:
            return np.ones(0, dtype=np.float32)
        t = np.arange(n, dtype=np.float32) / max(n - 1, 1)
        if curve == "equal_power":
            return np.sin(t * np.pi / 2.0).astype(np.float32)
        return t.astype(np.float32)

    fi = min(max(0, int(fade_in_samples)), total_samples)
    fo = min(max(0, int(fade_out_samples)), total_samples)
    gains_in = np.ones(max(total_samples, 0), dtype=np.float32)
    gains_out = np.ones(max(total_samples, 0), dtype=np.float32)
    if total_samples <= 0:
        return gains_in[:0], gains_out[:0]
    if fi > 0:
        gains_in[:fi] = _ramp(fi)
    if fo > 0:
        gains_out[-fo:] = _ramp(fo)[::-1]
    return gains_in, gains_out


def stem_channel_semantics(
    clip: Any,
    *,
    track_id: str,
    is_bus: bool = False,
) -> Dict[str, Any]:
    """Stem 导出对 clip 的采样域消费声明（GROUP/TRACK bus 语义入口）。

    Returns:
        {"track_id", "is_bus", "fade_samples_in/out"（clip 采样域 fade
        字段优先，None=沿用帧域）, "sample_offsets"（三字段原样）,
        "bus_kind": "GROUP"|"TRACK"|None}
        ——Stem 渲染按此声明消费与成片同一换算（F3.3 三路同语义）。
    """
    return {
        "track_id": str(track_id),
        "is_bus": bool(is_bus),
        "bus_kind": ("GROUP" if is_bus else None),
        "fade_samples_in": getattr(clip, "fade_in_samples", None),
        "fade_samples_out": getattr(clip, "fade_out_samples", None),
        "sample_offsets": {
            "audio_sample_offset": int(
                getattr(clip, "audio_sample_offset", 0) or 0),
            "source_sample_offset": int(
                getattr(clip, "source_sample_offset", 0) or 0),
        },
    }


__all__ = ["crossfade_gain_envelope", "stem_channel_semantics"]
