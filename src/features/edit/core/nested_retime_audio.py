"""M14.2 · 嵌套 retime 子片段音频映射（模型层）。

父 retime 下嵌套序列的子音频窗换算：父窗时间→子序列源时间的映射是
纯函数（位置映射与速度无关——见 M03 的 source_offset_seconds 同一
论证），子片段在**子序列源帧域**内按父窗裁剪。子片段自身再 retime
（嵌套两层速度）时显式 unsupported——组合矩阵的拒绝点，不静默。
"""

from __future__ import annotations

from typing import Any, Optional


def nested_child_audio_window(
    parent_clip: Any,
    child_clip: Any,
) -> "tuple[Optional[tuple[int, int]], str]":
    """M14.2：父 retime 下嵌套子片段的音频消费窗（子序列帧域）。

    Args:
        parent_clip: 嵌套片段（引用子序列；带父层 speed/reverse/
            speed_points）。
        child_clip: 子序列内的片段。

    Returns:
        ((start, end_exclusive), "")：子片段与父源窗的交叠（子序列
        帧域）；或 (None, reason) 不支持/无交叠——调用方显式诊断，
        不静默跳过音频。
    """
    parent_src_lo = int(getattr(parent_clip, "source_start", 0) or 0)
    parent_src_hi = parent_src_lo + int(
        getattr(parent_clip, "source_duration", 0) or 0)
    child_lo = int(getattr(child_clip, "timeline_start", 0) or 0)
    child_hi = child_lo + int(
        getattr(child_clip, "timeline_duration", 0) or 0)
    # 子片段的时间线位置即父源窗坐标（嵌套引用语义：子序列帧域 ==
    # 父片段源域）
    lo = max(parent_src_lo, child_lo)
    hi = min(parent_src_hi, child_hi)
    if hi <= lo:
        return None, "no_overlap: 子片段不在父源窗内"
    # 子片段自身 retime：嵌套两层速度的样本域组合未定义——显式拒绝
    from src.features.edit.core.audio_retime import needs_retime
    if needs_retime(child_clip):
        return None, "child_retime_unsupported: 嵌套子片段自身变速/反放" \
                     "的音频样本域组合未支持（显式拒绝，不静默）"
    if getattr(child_clip, "freeze_source_frame", None) is not None:
        return None, "child_frozen: 冻结子片段无音频进度"
    return (lo, hi), ""


def nested_retime_diagnostics(
    parent_clip: Any,
    child_clips: "tuple[Any, ...]" = (),
) -> "tuple[dict[str, str], ...]":
    """批量诊断：每个子片段的映射结果（供交付预检/UI 呈现）。"""
    out = []
    for child in child_clips:
        window, reason = nested_child_audio_window(parent_clip, child)
        out.append({
            "parent_clip_id": str(getattr(parent_clip, "clip_id", "")),
            "child_clip_id": str(getattr(child, "clip_id", "")),
            "window": list(window) if window else None,
            "status": "ok" if window else reason.split(":")[0],
            "reason": reason,
        })
    return tuple(out)


__all__ = ["nested_child_audio_window", "nested_retime_diagnostics"]
