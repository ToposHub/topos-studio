"""
Loudness Meter — EBU R128 LUFS 测量（W10）。

实现 ITU-R BS.1770-4 / EBU R128 响度测量：
  - K-weighting 滤波（stage 1: high-shelf pre-filter, stage 2: high-pass RLB）
  - 块均方（400ms 块，75% overlap → 100ms 步进）→ Momentary LUFS (M)
  - 短时窗（3s）→ Short-term LUFS (S)
  - 累积 Integrated LUFS（gated，符合 EBU R128）

校准点：1 kHz 正弦波 @ -23 dBFS → -23 LUFS ±0.1（ITU-R BS.1770-4 标准）。

性能：节流 ~10Hz emit（LUFS 不需要高频刷新）。
线程安全：实例不共享（每条 strip/总线一个）。
"""

from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np

from src.features.edit.core.audio_capabilities import is_scipy_available_for_dsp

try:
    from scipy.signal import lfilter, lfilter_zi
except ImportError:  # pragma: no cover
    lfilter = None  # type: ignore[assignment]
    lfilter_zi = None  # type: ignore[assignment]

# AM-01: scipy 可用性收口到 audio_capabilities 单一真相源。
# 运行时查询（同 audio_dsp），避免 reload 污染模块身份。
def _scipy_available() -> bool:
    return is_scipy_available_for_dsp()


# ─────────────────────────────────────────────────────────────────────────────
# K-weighting 滤波器系数（ITU-R BS.1770-4 固定系数）
# ─────────────────────────────────────────────────────────────────────────────

# Stage 1: pre-filter（high shelf，+4 dB boost 高频）
# 48 kHz 系数（来自 ITU-R BS.1770-4 表）
_K_PRE_B_48 = np.array([1.53512485958647, -2.69169618940638, 1.19839281085285], dtype=np.float64)
_K_PRE_A_48 = np.array([1.0, -1.69065929318241, 0.73248077421585], dtype=np.float64)

# Stage 2: RLB（high-pass，去低频）
_K_RLB_B_48 = np.array([1.0, -2.0, 1.0], dtype=np.float64)
_K_RLB_A_48 = np.array([1.0, -1.99004745483398, 0.99007225036621], dtype=np.float64)

# AM-10: 44.1 kHz 标准系数（ITU-R BS.1770-4 表）
_K_PRE_B_441 = np.array([1.53090959966709, -2.65095899568362, 1.16905117859210], dtype=np.float64)
_K_PRE_A_441 = np.array([1.0, -1.66363794583426, 0.71365412077482], dtype=np.float64)
_K_RLB_B_441 = np.array([1.0, -2.0, 1.0], dtype=np.float64)
_K_RLB_A_441 = np.array([1.0, -1.98917562089485, 0.98922186664907], dtype=np.float64)


def _get_k_weighting_coeffs(sample_rate: float):
    """返回 K-weighting 两段的 (b, a) 系数（AM-10: 支持多采样率）。

    ITU-R BS.1770-4 给出 48 kHz 和 44.1 kHz 的标准系数。
    其他采样率用最近的标准系数近似（精度可接受）。
    """
    if abs(sample_rate - 48000.0) < 1.0:
        return (_K_PRE_B_48, _K_PRE_A_48), (_K_RLB_B_48, _K_RLB_A_48)
    if abs(sample_rate - 44100.0) < 1.0:
        return (_K_PRE_B_441, _K_PRE_A_441), (_K_RLB_B_441, _K_RLB_A_441)
    # 其他采样率：用最近的标准系数（48k 或 44.1k）近似
    if sample_rate > 46050.0:  # 44.1k+48k 中点
        return (_K_PRE_B_48, _K_PRE_A_48), (_K_RLB_B_48, _K_RLB_A_48)
    return (_K_PRE_B_441, _K_PRE_A_441), (_K_RLB_B_441, _K_RLB_A_441)


# ─────────────────────────────────────────────────────────────────────────────
# LoudnessMeter（累积式，跨样本块连续）
# ─────────────────────────────────────────────────────────────────────────────

# EBU R128 参数
_MOMENTARY_BLOCK_MS = 400    # Momentary LUFS 窗口 400ms
_MOMENTARY_HOP_MS = 100      # 75% overlap → 100ms 步进
_SHORT_TERM_BLOCK_MS = 3000  # Short-term LUFS 窗口 3s
_GATE_ABSOLUTE_LUFS = -70.0  # 绝对门限（gating）

# LUFS = -0.691 + 10*log10(mean(mean square over channels))
_LUFS_OFFSET = -0.691


class LoudnessMeter:
    """EBU R128 响度计（W10）。

    用法：
      meter = LoudnessMeter(sample_rate=48000)
      meter.process(samples_block)  # 每次喂一块样本（channels, samples）
      lufs_m = meter.momentary_lufs   # Momentary LUFS（400ms 窗）
      lufs_s = meter.short_term_lufs  # Short-term LUFS（3s 窗）
      lufs_i = meter.integrated_lufs  # Integrated LUFS（gated 累积）

    校准：1 kHz 正弦 @ -23 dBFS（振幅 0.0708）→ -23 LUFS ±0.1
    """

    def __init__(self, sample_rate: float = 48000.0, channels: int = 2) -> None:
        self._sample_rate = float(sample_rate)
        self._channels = int(channels)

        if not _scipy_available():
            self._k_pre = None
            self._k_rlb = None
        else:
            (pre_b, pre_a), (rlb_b, rlb_a) = _get_k_weighting_coeffs(self._sample_rate)
            self._k_pre = (pre_b, pre_a)
            self._k_rlb = (rlb_b, rlb_a)

        # IIR 滤波器状态（每声道）
        self._zi_pre: Optional[np.ndarray] = None
        self._zi_rlb: Optional[np.ndarray] = None

        # 样本环形缓冲（用于 400ms 和 3s 窗口计算）
        # 存储 K-weighted 后的 mean-square 逐样本值（标量，已跨声道平均）
        samples_per_ms = self._sample_rate / 1000.0
        self._ms_block_size = int(_MOMENTARY_BLOCK_MS * samples_per_ms)
        self._ms_hop_size = int(_MOMENTARY_HOP_MS * samples_per_ms)
        self._st_block_size = int(_SHORT_TERM_BLOCK_MS * samples_per_ms)

        # K-weighted 样本的平方值环形缓冲（numpy，向量化读写）。
        # 此前用 deque 存逐样本值、每 chunk `list(...)[-N:]` + Python sum
        # 重算 3s 窗口：8 轨实测每 chunk 37ms（LUFS 占 83%），纯 Python
        # 逐样本循环是实时混音的单点瓶颈。numpy 环形缓冲 + 向量化窗口和
        # 将该成本降到 ~0.04ms/轨/chunk，数值语义（最近 N 样本均值）不变。
        self._sq_buf: np.ndarray = np.zeros(self._st_block_size, dtype=np.float64)
        self._sq_pos: int = 0   # 下一个写入位置
        self._sq_count: int = 0  # 已积累样本数（≤ st_block_size）
        # Momentary hop 计数（替代 _block_ms_buffer 的逐元素进出）
        self._ms_acc: int = 0

        # Integrated LUFS 累积（gating）
        # AM-10: 存储**第一阶段绝对门限（-70 LUFS）上方块**的 mean-z 值，
        # 供第二阶段相对门限（-10 LU）重新筛选。此前实现是简化版，未正确实现
        # BS.1770-4 的两阶段 gating。
        self._gated_blocks: deque = deque()  # 第一阶段通过块的 mean-z 值列表
        self._gated_sum: float = 0.0         # 第一阶段通过块 mean-z 的运行和

        self._momentary_lufs: float = -70.0
        self._short_term_lufs: float = -70.0
        self._integrated_lufs: float = -70.0
        # AUD-16905：第二阶段（相对门限）筛选移到 integrated_lufs 读取时
        # 惰性计算并缓存——实时 mix 循环每 100ms hop 只做 O(1) 追加，
        # O(N) 全量重扫仅发生在真正读取 integrated 的时刻。
        self._integrated_dirty: bool = False

    def reset(self) -> None:
        """重置所有状态（seek/重新开始时调用）。"""
        self._zi_pre = None
        self._zi_rlb = None
        self._sq_pos = 0
        self._sq_count = 0
        self._ms_acc = 0
        self._gated_blocks.clear()
        self._gated_sum = 0.0
        self._momentary_lufs = -70.0
        self._short_term_lufs = -70.0
        self._integrated_lufs = -70.0
        self._integrated_dirty = False

    # ── 环形缓冲原语 ──────────────────────────────────────────

    def _push_sq(self, sq: np.ndarray) -> None:
        """向 3s 环形缓冲写入一块逐样本 mean-square 值。"""
        buf = self._sq_buf
        n = int(sq.size)
        if n <= 0:
            return
        if n >= buf.size:
            # 超长块：只保留尾部，游标重置
            buf[:] = sq[-buf.size:]
            self._sq_pos = 0
            self._sq_count = buf.size
            return
        end = self._sq_pos + n
        if end <= buf.size:
            buf[self._sq_pos:end] = sq
        else:
            first = buf.size - self._sq_pos
            buf[self._sq_pos:] = sq[:first]
            buf[:end - buf.size] = sq[first:]
        self._sq_pos = end % buf.size
        self._sq_count = min(self._sq_count + n, buf.size)

    def _window_sum(self, size: int) -> tuple[float, int]:
        """最近 n=min(size, count) 个样本的平方和及样本数。"""
        n = min(int(size), self._sq_count)
        if n <= 0:
            return 0.0, 0
        start = (self._sq_pos - n) % self._sq_buf.size
        if start + n <= self._sq_buf.size:
            return float(self._sq_buf[start:start + n].sum()), n
        first = self._sq_buf.size - start
        return float(self._sq_buf[start:].sum() + self._sq_buf[:n - first].sum()), n

    def process(self, samples: np.ndarray) -> None:
        """处理一块样本，更新所有 LUFS 测量值。

        Args:
            samples: shape (channels, num_samples)，float32/float64
        """
        if not _scipy_available() or self._k_pre is None:
            return
        if samples.ndim != 2 or samples.shape[0] < 1:
            return

        channels = samples.shape[0]
        # 初始化/校准 IIR 状态
        if self._zi_pre is None or self._zi_pre.shape[0] != channels:
            self._zi_pre = np.zeros((channels, 2), dtype=np.float64)
            self._zi_rlb = np.zeros((channels, 2), dtype=np.float64)

        # K-weighting：每声道两段串联滤波
        weighted = samples.astype(np.float64, copy=True)
        pre_b, pre_a = self._k_pre
        rlb_b, rlb_a = self._k_rlb
        for ch in range(channels):
            y, self._zi_pre[ch] = lfilter(pre_b, pre_a, weighted[ch], zi=self._zi_pre[ch])
            y, self._zi_rlb[ch] = lfilter(rlb_b, rlb_a, y, zi=self._zi_rlb[ch])
            weighted[ch] = y

        # 计算逐样本的跨声道加权能量（BS.1770-4 声道权重求和）。
        # AM-08 阶段 4.1：各声道按 BS.1770-4 权重加权（L/R/C=1.0,
        # Ls/Rs=0.59/0.41→1.41 增益, LFE=0）。
        # F3.4 缺陷修复：BS.1770-4 规定 LKFS = -0.691 + 10lg(Σ G_i·z_i)
        # ——**对声道求和、不归一化**。旧实现 `/ sum(weights)` 是加权平均，
        # 使 dual-mono 立体声与同电平 mono 读数相同（标准应 +3.01 LU）、
        # 5.1 整体低 10lg(ΣG)=7.66 LU——立体声/环绕交付按 -23 目标归一后
        # 在外部合规表上系统性偏响。
        try:
            from src.features.edit.core.channel_layout import (
                ChannelLayout, bs1770_channel_weights,
            )
            layout = ChannelLayout.from_channel_count(channels)
            weights = bs1770_channel_weights(layout)
            # 加权能量和 = sum(w_i * x_i^2)（BS.1770-4 §3；mono 自然退化为 z）
            sq = np.sum(weights.reshape(-1, 1) * (weighted ** 2), axis=0)
        except (ValueError, ImportError):
            # 非标准声道数（如 4）：无标准 G_i，保留等权 mean（声明非
            # BS.1770 语义，不冒充合规读数）
            sq = np.mean(weighted ** 2, axis=0)  # shape (num_samples,)

        # 推入 3s 环形缓冲（向量化）
        self._push_sq(sq.astype(np.float64, copy=False))

        # ── Momentary LUFS（400ms 窗，100ms hop）──
        # 每积累满一个 hop 计算一次最近 400ms 窗口（滑动窗语义与原实现一致）
        self._ms_acc += int(sq.size)
        while self._ms_acc >= self._ms_hop_size:
            window_sum, n = self._window_sum(self._ms_block_size)
            if n > 0:
                mean_z = window_sum / n
                self._momentary_lufs = self._z_to_lufs(mean_z)
                self._update_integrated(mean_z)
            self._ms_acc -= self._ms_hop_size

        # ── Short-term LUFS（3s 窗）──
        if self._sq_count > 0:
            window_sum, n = self._window_sum(self._st_block_size)
            mean_z = window_sum / n
            self._short_term_lufs = self._z_to_lufs(mean_z)

    def _update_integrated(self, block_mean_z: float) -> None:
        """更新 Integrated LUFS 第一阶段（BS.1770-4 两阶段 gating，AM-10 修正）。

        标准 ITU-R BS.1770-4 gating 流程：
        1. 第一阶段（绝对门限 -70 LUFS）：仅保留 mean-z 高于 -70 LUFS 对应值的块。
        2. 计算第一阶段通过块的均值（mean-z），相对门限 = 该均值 - 10 LU。
        3. 第二阶段：从第一阶段通过块中，再筛出高于相对门限的块。
        4. Integrated LUFS = 第二阶段通过块 mean-z 的均值 → LUFS。

        此前实现是简化版（第一阶段计入后不回退），不符合 BS.1770-4。

        AUD-16905：本方法只做第一阶段的 O(1) 追加与运行和。旧实现每个
        hop 对 _gated_blocks 做一次 O(N) 全量重扫（第二阶段筛选），实时
        音频路径上成本随会话时长无界线性增长（1 小时会话每轨每 hop 约
        3.6 万次比较）；第二阶段现移到 ``integrated_lufs`` 属性读取时惰性
        计算并缓存（mix 循环只读 momentary，integrated 消费者 ~10Hz 或
        分析结束读取一次）。
        """
        lufs = self._z_to_lufs(block_mean_z)
        # 第一阶段：绝对门限 -70 LUFS
        if lufs < _GATE_ABSOLUTE_LUFS:
            return
        self._gated_blocks.append(block_mean_z)
        self._gated_sum += block_mean_z
        self._integrated_dirty = True

    def _compute_integrated(self) -> float:
        """BS.1770-4 第二阶段 gating（O(N)，仅读取 integrated 时执行）。"""
        if not self._gated_blocks:
            return -70.0
        # 第一阶段通过块的均值（运行和，避免全表重扫求和）
        mean_z_first = self._gated_sum / len(self._gated_blocks)
        relative_gate_z = self._lufs_to_z(self._z_to_lufs(mean_z_first) - 10.0)
        # 第二阶段：筛出高于相对门限的块
        second_stage = [z for z in self._gated_blocks if z > relative_gate_z]
        if not second_stage:
            return -70.0
        return self._z_to_lufs(sum(second_stage) / len(second_stage))

    @staticmethod
    def _lufs_to_z(lufs: float) -> float:
        """LUFS → mean-square z 值（_z_to_lufs 的逆）。"""
        if lufs <= -70.0:
            return 0.0
        return 10.0 ** ((lufs - _LUFS_OFFSET) / 10.0)

    @staticmethod
    def _z_to_lufs(mean_z: float) -> float:
        """mean-square z 值 → LUFS。"""
        if mean_z < 1e-12:
            return -70.0
        return _LUFS_OFFSET + 10.0 * np.log10(mean_z)

    @property
    def momentary_lufs(self) -> float:
        """Momentary LUFS（400ms 窗，最新值）。无信号时 -70.0。"""
        return self._momentary_lufs

    @property
    def is_backend_available(self) -> bool:
        """LUFS 后端（scipy）是否可用。

        AM-01: 供 meter UI 区分"信号静音（合法 -70）"与"后端不可用（伪造的 -70）"。
        当本属性为 False 时，所有 lufs 读数均不可信，UI 应显示降级状态而非数值。
        """
        return _scipy_available()

    @property
    def short_term_lufs(self) -> float:
        """Short-term LUFS（3s 窗，最新值）。无信号时 -70.0。"""
        return self._short_term_lufs

    @property
    def integrated_lufs(self) -> float:
        """Integrated LUFS（gated 累积，整个播放段）。无信号时 -70.0。

        AUD-16905：第二阶段（相对门限）筛选在此惰性计算并缓存——
        mix 循环每 100ms hop 只做 O(1) 累积，读取方（UI ~10Hz /
        分析结束一次性）才对第一阶段通过块做一次 O(N) 扫描。
        """
        if self._integrated_dirty:
            self._integrated_lufs = self._compute_integrated()
            self._integrated_dirty = False
        return self._integrated_lufs
