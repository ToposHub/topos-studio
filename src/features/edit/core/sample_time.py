"""F3.3 · 采样级时间语义——有理数子帧位置/淡化与共享块跳过门。

计划要求"引入有采样率/有理数语义的子帧位置与 fade，不用浮点帧叠加；
视频保持帧对齐"。本模块是唯一的换算层：

- ``TimelineSampleClock``：帧↔样本全部走 ``Fraction``（非整数 fps 如
  30000/1001 下帧长 = 48000×1001/30000 = 8008/5 样本，精确有理数），
  长序列累积位置不做浮点叠加——任意帧号样本边界一次求值，无漂移。
- ``SampleSkipGate``：子帧偏移在帧量化渲染循环里的消费件——clip 首块
  跳过 N 样本（时间线侧延迟入点/源侧子帧修剪共用），实时与导出两条
  路径同一实现（预听/成片一致）。

声明：子帧偏移以工程音频参考率 ``AUDIO_REFERENCE_SAMPLE_RATE``（48000）
计量；块率不同（如 44100 工程）时按有理数精确换算后取整执行，误差 <1
样本并在 gate 内记账（不静默丢）。
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Optional

import numpy as np

#: 子帧偏移的参考采样率（与默认输出/导出采样率一致）
AUDIO_REFERENCE_SAMPLE_RATE = 48000


class TimelineSampleClock:
    """帧↔样本的精确有理数时钟（无浮点累积漂移）。"""

    def __init__(self, fps, sample_rate: int = AUDIO_REFERENCE_SAMPLE_RATE) -> None:
        self.fps = Fraction(str(fps)) if not isinstance(fps, Fraction) else fps
        if self.fps <= 0:
            raise ValueError(f"fps must be positive: {fps}")
        self.sample_rate = int(sample_rate)
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive: {sample_rate}")
        # 每帧样本数 = sr / fps（精确有理数；29.97@48k = 8008/5 = 1601.6）
        self.samples_per_frame: Fraction = Fraction(self.sample_rate) / self.fps

    # ── 位置换算（全部精确） ─────────────────────────────────────────

    def frame_start_sample(self, frame: int) -> Fraction:
        """帧起点的绝对样本位置（一次求值，无逐步累积）。"""
        return self.samples_per_frame * int(frame)

    def block_bounds(self, start_frame: int, frame_count: int) -> list[int]:
        """[start_frame, start_frame+frame_count] 每帧相邻样本边界（int）。

        相邻边界差之和 == floor(总样本数) 精确闭合——非整数帧率下逐帧
        floor 各自独立会造成孔洞/重叠，这里用整段一次求值再作差。
        """
        first = self.frame_start_sample(start_frame)
        total = self.frame_start_sample(start_frame + frame_count)
        n0 = int(first)  # 与渲染循环的全局零点对齐
        n1 = int(total)
        if n1 - n0 < frame_count:
            # 理论上 total-first ≥ frame_count-? 恒成立（spf>1）；防御回退
            n1 = n0 + frame_count
        # 均分余数：前 r 帧多 1 样本（与 D06 的 frame_sample_bounds 同型）
        base = (n1 - n0) // frame_count
        r = (n1 - n0) - base * frame_count
        bounds = [0] * (frame_count + 1)
        pos = n0
        for i in range(frame_count):
            bounds[i] = pos - n0
            pos += base + (1 if i < r else 0)
        bounds[frame_count] = n1 - n0
        return bounds

    def position_samples(
        self, frame: int, sample_offset: int = 0,
    ) -> Fraction:
        """clip 绝对样本位置 = 帧起点 + 子帧偏移（全有理数）。

        偏移是相对该 clip 帧起点的整数样本（r ≥ 0 = 延迟入点）。
        非整数帧长下不做帧进位归一化——任何进位都会引入 ≤0.6 样本的
        有理数残差错位；位置始终由 (帧, 整数偏移) 二元组精确表示。
        """
        return self.frame_start_sample(frame) + int(sample_offset)


class SampleSkipGate:
    """子帧偏移块消费门：clip 首个（或首个若干）块跳过前 N 样本。

    实时（AudioDecoder 逐块拉流）与导出（timeline_export 逐帧块循环）
    共用：两侧对同一 clip 语义一致（预听/成片一致）。
    """

    def __init__(self, skip_samples: int = 0) -> None:
        self._pending = max(0, int(skip_samples))

    @property
    def pending(self) -> int:
        return self._pending

    def feed(self, block: np.ndarray) -> Optional[np.ndarray]:
        """消费一块：跳过前 N 样本；全块被跳过返回长度 0 数组。"""
        if self._pending <= 0 or block is None or block.shape[-1] == 0:
            return block
        skip = min(self._pending, int(block.shape[-1]))
        self._pending -= skip
        return block[..., skip:]

    def reset(self, skip_samples: int = 0) -> None:
        self._pending = max(0, int(skip_samples))


def split_sample_partition(
    clock: TimelineSampleClock,
    clip_timeline_start_frame: int,
    clip_timeline_duration_frames: int,
    split_timeline_frame: int,
    *,
    source_sample_offset: int = 0,
) -> "tuple[tuple[int, int], tuple[int, int]]":
    """M14.1 · split 采样分区契约（冻结语义）。

    在时间线帧 ``split_timeline_frame`` 处切分时长为
    ``clip_timeline_duration_frames`` 帧的 clip，返回左右片在**源样本
    域**的 (offset, count)：

    - **取整**：切点样本位置（精确 Fraction）floor 为左片长度——
      非整数帧率（NTSC 30000/1001：帧长 8008/5=1601.6 样本）下切点
      落在样本内时，边界样本**归属左片**（左闭右开半开区间），右片
      从下一样本起；
    - **连续性**：``left.count + right.count == 原窗总样本数``——
      不重读、不漏样本；
    - **归属**：clip 既有 ``source_sample_offset``（源侧子帧修剪）
      归左片起点不变；右片 offset = 左片窗末 + 1（样本域连续推进，
      与帧域 source_start 分开表示）。

    帧域（source_start/source_duration）仍按帧对齐切分（视频不受
    影响）；本函数给出音频侧的样本域补充分区供采样级路径消费。
    """
    if not (clip_timeline_start_frame < split_timeline_frame
            < clip_timeline_start_frame + clip_timeline_duration_frames):
        raise ValueError(
            f"split frame {split_timeline_frame} 必须严格位于 clip "
            f"({clip_timeline_start_frame}, "
            f"{clip_timeline_start_frame + clip_timeline_duration_frames}) 内")

    clip_start_pos = clock.frame_start_sample(clip_timeline_start_frame)
    split_pos = clock.frame_start_sample(split_timeline_frame)
    clip_end_pos = clock.frame_start_sample(
        clip_timeline_start_frame + clip_timeline_duration_frames)

    left_len = int(split_pos - clip_start_pos)          # floor：边界归左
    total_len = int(clip_end_pos - clip_start_pos)

    # 源侧子帧修剪 offset 从左片消费窗扣减（跳过的样本两片都不消费）；
    # 右片消费从左片消费窗末连续推进——消费窗无缝、无重叠
    left_offset = int(source_sample_offset)
    left_count = max(0, left_len - left_offset)
    right_offset = left_offset + left_count
    right_count = max(0, total_len - right_offset)
    return (left_offset, left_count), (right_offset, right_count)


# ── M24.2 · 子帧 offset 有理时基映射（交换细则） ─────────────────────
#
# 外部时间线（FCPXML 等）声明的有理秒时间点映射为内部 (整帧, 子帧样本
# 偏移@48000 参考率) 二元组；导出逆向合成精确有理秒。声明采样率在
# "秒→样本"换算中显式参与（44.1k 工程的偏移按其声明时基转换，不被
# 48000 默认值掩盖）；量化残差 <1 参考样本返回调用方记账，不静默丢。


def subframe_decompose(
    clock: TimelineSampleClock, seconds: Fraction,
) -> "tuple[int, Fraction]":
    """M24.2 · 有理秒 → (整帧, 子帧样本偏移@声明率)——导入映射。

    整帧 = floor(秒 × fps)（与 adapter ``_time_to_frames`` 同语义）；
    残差秒 × 声明采样率 = 子帧样本（Fraction，不取整不进位——与
    ``position_samples`` 同一表示哲学：任何进位都引入有理数残差错位）。
    """
    sec = Fraction(seconds)
    frame = (sec * clock.fps).__floor__()
    residue_sec = sec - Fraction(frame) / clock.fps
    return frame, residue_sec * clock.sample_rate


def compose_timeline_seconds(
    clock: TimelineSampleClock, frame: int, offset_samples: Fraction,
) -> Fraction:
    """M24.2 · (帧, 子帧偏移@声明率) → 精确有理秒——导出逆映射。

    与 ``subframe_decompose`` 严格互逆（纯有理数运算）：
    ``compose(decompose(x)) == x`` 恒成立。
    """
    return (Fraction(int(frame)) / clock.fps
            + Fraction(offset_samples) / clock.sample_rate)


def offset_to_reference_rate(
    offset_samples: Fraction, declared_rate: int,
) -> "tuple[int, Fraction]":
    """M24.2 · 声明率样本偏移 → (@48000 参考率执行偏移, 残差)。

    内部统一以 ``AUDIO_REFERENCE_SAMPLE_RATE``（48000）计量（见
    timeline_v2 ClipInstance.audio_sample_offset 契约）；外部声明率
    不同（44100 工程等）时按有理数精确换算后 floor 执行——残差
    <1 参考样本返回调用方记账（诊断条目/对拍），不静默丢。
    """
    converted = (Fraction(offset_samples) * AUDIO_REFERENCE_SAMPLE_RATE
                 / int(declared_rate))
    whole = math.floor(converted)
    return whole, converted - whole


__all__ = [
    "AUDIO_REFERENCE_SAMPLE_RATE",
    "TimelineSampleClock",
    "SampleSkipGate",
    "split_sample_partition",
    "subframe_decompose",
    "compose_timeline_seconds",
    "offset_to_reference_rate",
]
