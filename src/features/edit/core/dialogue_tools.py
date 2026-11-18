"""P1 · 影视对白剪辑工具的核心算法（2026-11-19 计划第二阶段）。

四个纯函数（无 UI/无副作用，供 Clip Editor/时间线命令消费）：

- ``fill_room_tone``：环境声（Room Tone）填补——选区内容以噪声底
  取样段循环铺满，边缘短交叉淡化防爆音；
- ``plan_jl_cut``：J/L cut——音频切点相对画面切点前移（J）/后移
  （L）的音频分割计划；
- ``plan_batch_crossfades``：批量短交叉淡化——轨上相邻片段边界在
  容差内逐一配对；
- ``estimate_alignment_offset``：对白/ADR 匹配——归一化互相关估计
  take 相对参考的偏移，抛物线插值给出子帧建议（确认后写子帧偏移）。

take 轮换/audition lanes 与场次镜次元数据复用既有
``take_sheet``/``audition`` 模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

#: Room Tone 填补与批量交叉淡化的默认边缘过渡（ms；防爆音合同 2–5ms）。
DEFAULT_CROSSFADE_MS = 4.0


def fill_room_tone(
    pcm: np.ndarray,
    sample_rate: int,
    target_start: int,
    target_end: int,
    *,
    source_start: int,
    source_end: int,
    crossfade_ms: float = DEFAULT_CROSSFADE_MS,
    rng_seed: int = 20261119,
) -> np.ndarray:
    """把 ``[target_start, target_end)`` 替换为环境声（不修改输入）。

    - 取样段 ``[source_start, source_end)``（纯 Room Tone 采集区）循环
      铺满目标区，段边界加 2 ms 内接淡化避免循环接缝阶跃；
    - 填补区首尾与原内容做 ``crossfade_ms`` 交叉淡化（等功率）；
    - 目标区之外逐样本不变。
    """
    if pcm is None or pcm.ndim != 2:
        raise ValueError("pcm must be planar (channels, samples)")
    total = pcm.shape[1]
    target_start = max(0, int(target_start))
    target_end = min(total, int(target_end))
    source_start = max(0, int(source_start))
    source_end = min(total, int(source_end))
    if target_end <= target_start:
        return pcm
    if source_end - source_start < int(0.05 * sample_rate):
        raise ValueError("room tone source must be at least 50 ms")

    out = np.array(pcm, dtype=np.float32, copy=True)
    length = target_end - target_start
    # 循环铺满：以取样段为周期，段边界 2 ms 淡化消除接缝。
    seg = pcm[:, source_start:source_end]
    seg_len = seg.shape[1]
    seam = max(1, int(0.002 * sample_rate))
    loop = np.concatenate([seg, seg], axis=1)
    fade = np.ones(2 * seg_len, dtype=np.float32)
    ramp_len = min(seam, seg_len // 4)
    if ramp_len > 0:
        fade[:ramp_len] = np.linspace(0.0, 1.0, ramp_len, dtype=np.float32)
        fade[-ramp_len:] = np.linspace(1.0, 0.0, ramp_len, dtype=np.float32)
    # 归一化循环能量（fade 后幅度略降，补偿到取样段 RMS）。
    loop = loop * (fade / np.sqrt(np.mean(fade ** 2)))[np.newaxis, :]
    rng = np.random.default_rng(rng_seed)
    phase = int(rng.integers(0, seg_len))
    filled = np.empty((pcm.shape[0], length), dtype=np.float32)
    pos = 0
    while pos < length:
        take = min(2 * seg_len - phase, length - pos)
        filled[:, pos:pos + take] = loop[:, phase:phase + take]
        pos += take
        phase = (phase + take) % (2 * seg_len)

    out[:, target_start:target_end] = filled
    # 首尾交叉淡化（等功率，与原内容拼接）。
    xf = min(int(crossfade_ms * sample_rate / 1000.0),
             length // 2, target_start, total - target_end)
    if xf > 0:
        weight = np.sin(
            np.linspace(0.0, np.pi / 2.0, xf, dtype=np.float32)) ** 2
        out[:, target_start:target_start + xf] = (
            pcm[:, target_start:target_start + xf] * (1.0 - weight)
            + filled[:, :xf] * weight)
        out[:, target_end - xf:target_end] = (
            pcm[:, target_end - xf:target_end] * (weight)
            + filled[:, -xf:] * (1.0 - weight))
    return out


@dataclass(frozen=True)
class JLCutPlan:
    """一次 J/L cut 的音频分割计划（纯数据；offset<0=J 前移，>0=L 后移）。"""

    cut_frame: int
    audio_split_frame: int
    offset_frames: int
    kind: str                     # "J" | "L"
    valid: bool = True
    reason: str = ""


def plan_jl_cut(
    audio_clip: Any,
    video_cut_frame: int,
    offset_frames: int,
) -> JLCutPlan:
    """J/L cut：音频在画面切点 ± offset 处分割（切空不动同步）。

    ``offset_frames < 0`` = J cut（音频先入，提前切）；``> 0`` = L cut
    （音频后入）。分割点钳制在音频片段开区间内，越界给出不执行的原因。
    """
    offset = int(offset_frames)
    split = int(video_cut_frame) + offset
    kind = "J" if offset < 0 else "L"
    start = int(getattr(audio_clip, "timeline_start", 0) or 0)
    duration = int(getattr(audio_clip, "timeline_duration", 0) or 0)
    end = start + duration
    if not start < split < end:
        return JLCutPlan(
            cut_frame=int(video_cut_frame), audio_split_frame=split,
            offset_frames=offset, kind=kind, valid=False,
            reason=f"split frame {split} outside clip ({start}, {end})")
    return JLCutPlan(
        cut_frame=int(video_cut_frame), audio_split_frame=split,
        offset_frames=offset, kind=kind, valid=True)


@dataclass(frozen=True)
class BatchCrossfadePlan:
    """批量短交叉淡化计划：容差内的相邻边界逐一配对。"""

    pairs: tuple = field(default=())      # ((left_clip_id, right_clip_id, boundary_frame), ...)
    skipped: tuple = field(default=())    # 间隔超过容差的边界（gap 原样）


def plan_batch_crossfades(
    track: Any,
    *,
    max_gap_frames: int = 2,
) -> BatchCrossfadePlan:
    """轨上相邻片段边界配对：间隔 ≤ 容差的边界做短交叉淡化。

    只做**计划**（命令层据此提交淡化参数）；超过容差的间隔保持 gap
    （切空不动同步的保守语义）。
    """
    clips = sorted(
        (clip for clip in (getattr(track, "clips", ()) or ())),
        key=lambda clip: int(getattr(clip, "timeline_start", 0) or 0))
    pairs: list[tuple] = []
    skipped: list[tuple] = []
    for left, right in zip(clips, clips[1:]):
        left_end = (int(getattr(left, "timeline_start", 0) or 0)
                    + int(getattr(left, "timeline_duration", 0) or 0))
        right_start = int(getattr(right, "timeline_start", 0) or 0)
        gap = right_start - left_end
        if gap < 0:
            continue  # 重叠边界由既有转场语义处理
        if gap <= max_gap_frames:
            pairs.append((left.clip_id, right.clip_id, left_end))
        else:
            skipped.append((left.clip_id, right.clip_id, gap))
    return BatchCrossfadePlan(pairs=tuple(pairs), skipped=tuple(skipped))


@dataclass(frozen=True)
class AlignmentEstimate:
    """ADR take 相对参考对白的对齐估计。"""

    offset_samples: int          # take 应整体移动的整样本数（+ = 右移）
    fractional_samples: float    # 子帧残差（|·|<1，抛物线插值）
    confidence: float            # 峰值相关（归一化，0..1）


def estimate_alignment_offset(
    reference: np.ndarray,
    take: np.ndarray,
    sample_rate: int,
    *,
    max_offset_ms: float = 500.0,
) -> Optional[AlignmentEstimate]:
    """归一化互相关对齐（离线；建议值，确认后写子帧偏移）。

    take 较短（ADR cue）在参考（同期对白轨）内滑动搜索
    ``±max_offset_ms``；FFT 互相关 + 峰值抛物线插值给子帧建议。
    返回 None 表示无可信峰（能量过低/全静音）。
    """
    ref = np.asarray(reference, dtype=np.float64)
    tk = np.asarray(take, dtype=np.float64)
    if ref.ndim > 1:
        ref = ref.mean(axis=0)
    if tk.ndim > 1:
        tk = tk.mean(axis=0)
    n_ref, n_tk = ref.shape[0], tk.shape[0]
    if n_tk < int(0.05 * sample_rate) or n_ref <= n_tk:
        return None
    if float(np.max(np.abs(tk))) < 1e-5 or float(np.max(np.abs(ref))) < 1e-5:
        return None

    # 去均值（DC 不参与相关）。
    ref = ref - ref.mean()
    tk = tk - tk.mean()
    n = 1 << int(np.ceil(np.log2(n_ref + n_tk)))
    spec_ref = np.fft.rfft(ref, n)
    spec_tk = np.fft.rfft(tk, n)
    corr_full = np.fft.irfft(spec_ref * np.conj(spec_tk), n)
    # 峰值只取 |lag| <= max_offset 的窗（避免端部自相关伪峰）。
    max_lag = min(int(max_offset_ms * sample_rate / 1000.0),
                  n_tk // 2, n_ref - n_tk)
    if max_lag < 1:
        return None
    lags = np.concatenate([corr_full[:max_lag + 1],
                           corr_full[-max_lag:]])
    peak = int(np.argmax(lags))
    lag = peak if peak <= max_lag else peak - len(lags)
    # 归一化置信度：峰值 / sqrt(能量积)。
    denom = float(np.sqrt(np.sum(ref ** 2) * np.sum(tk ** 2))) or 1.0
    confidence = float(lags[peak] / denom)
    # 抛物线插值子帧残差。
    fractional = 0.0
    if 0 < peak < len(lags) - 1:
        y0, y1, y2 = float(lags[peak - 1]), float(lags[peak]), float(lags[peak + 1])
        denom2 = (y0 - 2.0 * y1 + y2)
        if abs(denom2) > 1e-12:
            fractional = float(0.5 * (y0 - y2) / denom2)
    # 互相关 lag 语义：ref[n] ≈ take[n+lag]——take 整体移动 lag 即对齐。
    return AlignmentEstimate(
        offset_samples=int(lag), fractional_samples=float(fractional),
        confidence=min(1.0, max(0.0, confidence)))
