"""
Offline Loudness Analyzer（AM-10 阶段 3.4，离线响度交付）。

基于已完成的 BS.1770-4 LoudnessMeter（阶段 3.3）和共享 renderer（阶段 2.1），
提供离线响度分析、normalize 增益计算和交付报告。

功能（计划表 3.4）：
- analyze：离线扫描整段 PCM，计算 integrated/true-peak/loudness-range
- normalize_gain：计算到达目标 LUFS 所需的线性增益（不破坏性改源，供调用方应用）
- report：生成可审计的 JSON/文本响度报告

设计原则（计划表 3.4）：
- 不在未经用户确认时破坏性改源（normalize 只计算增益，由调用方决定是否应用）
- 报告与导出一起保存（integrated/range/true peak/warnings）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from src.features.edit.core.audio_capabilities import is_scipy_available_for_dsp
from src.features.edit.core.loudness_meter import LoudnessMeter


def _interpolation_taps_4x() -> "np.ndarray":
    """4x 插值低通核（windowed-sinc，m = -24..24，截止 fs/4）。

    DC 处对零填充信号增益为 1（Σ taps == 1），用于 intersample 峰值
    估计（FE-B15：样本峰值不能代表样本间峰值，不得再标 dBTP）。
    """
    m = np.arange(-24, 25, dtype=np.float64)
    taps = np.sinc(m / 4.0)
    taps *= np.blackman(len(m))
    # 零填充路径的相位 0 多相和必须为 1（y[4n] ≈ x[n]），全体系和 ≈ 4
    phase0_sum = float(np.sum(taps[::4]))
    return taps / phase0_sum


class _TruePeakEstimator:
    """跨块保持滤波状态的 4x 插值 true-peak 估计器。

    零填充上采样 + 低通插值后在 4x 网格上取 |max|，捕获样本间过冲
    （intersample overs）。输出为线性峰值（0..1）；phase 0 即原始样本。

    R27/F-R08：流式边界契约——**同一有限信号任意分块结果一致**：

    - 每个内容样本的 4x 插值窗口需要左右各 24（滤波器半宽）个样本
      上下文。左上下文来自已扫内容的尾部（流首由「头 24 样本镜像」
      pre-roll 提供）；右上下文由 24 样本的扫描延迟保证——只有攒够
      右侧上下文的区段才扫描，尾部留到 :meth:`finalize。
    - :meth:`finalize` 用「流尾 24 样本镜像」补右侧上下文，把最后
      一段扫完（文件尾样本间的 intersample 峰不漏报；镜像延拓使直流
      /平稳信号首尾不产生补零 Gibbs 假峰 1.119）。幂等，此后 process
      为 no-op。
    """

    OVERSAMPLE = 4
    CONTEXT = 24  # 滤波器半宽（m = -24..24）

    def __init__(self, channels: int) -> None:
        self._taps = _interpolation_taps_4x()
        self._channels = channels
        self.peak = 0.0
        self._pending: Optional["np.ndarray"] = None  # 尚未扫描的内容
        self._state = np.zeros((channels, self.CONTEXT), dtype=np.float64)
        self._head_initialized = False
        self._finalized = False

    @staticmethod
    def _mirror_to_context(sig: "np.ndarray", context: int) -> "np.ndarray":
        """把 sig 样本镜像并重复到 context 长度。"""
        n = sig.shape[1]
        if n == 0:
            return np.zeros((sig.shape[0], context), dtype=np.float64)
        mirror = sig[:, ::-1]
        reps = int(np.ceil(context / max(1, n)))
        tiled = np.concatenate([mirror] * reps, axis=1)
        return tiled[:, -context:].copy()

    def _scan_content(self, context_left: "np.ndarray",
                      content: "np.ndarray",
                      context_right: "np.ndarray") -> None:
        """扫描 content 样本区段的 4x 插值网格（两侧上下文 ≥ 半宽）。"""
        n = content.shape[1]
        if n == 0:
            return
        padded = np.concatenate(
            [context_left, content.astype(np.float64, copy=False),
             context_right],
            axis=1,
        )
        c0 = context_left.shape[1]
        up_len = padded.shape[1] * self.OVERSAMPLE
        upsampled = np.zeros((self._channels, up_len), dtype=np.float64)
        upsampled[:, ::self.OVERSAMPLE] = padded
        half = len(self._taps) // 2
        for ch in range(self._channels):
            y = np.convolve(upsampled[ch], self._taps, mode="full")
            lo = half + c0 * self.OVERSAMPLE
            hi = half + (c0 + n) * self.OVERSAMPLE
            if hi > lo:
                self.peak = max(self.peak, float(np.max(np.abs(y[lo:hi]))))

    def process(self, block: "np.ndarray") -> None:
        if block.size == 0 or self._finalized:
            return
        block = block.astype(np.float64, copy=False)
        if self._pending is None:
            self._pending = block.copy()
        else:
            self._pending = np.concatenate([self._pending, block], axis=1)

        if not self._head_initialized:
            if self._pending.shape[1] < self.CONTEXT:
                return  # 攒流头（镜像 pre-roll 的延拓源）
            self._state = self._mirror_to_context(
                self._pending[:, : self.CONTEXT], self.CONTEXT
            )
            self._head_initialized = True

        pending_n = self._pending.shape[1]
        scannable = pending_n - self.CONTEXT
        if scannable > 0:
            self._scan_content(
                self._state,
                self._pending[:, :scannable],
                self._pending[:, scannable: scannable + self.CONTEXT],
            )
            self._state = np.concatenate(
                [self._state, self._pending[:, :scannable]], axis=1
            )[:, -self.CONTEXT:].copy()
            self._pending = self._pending[:, scannable:].copy()

    def finalize(self) -> None:
        """R27：流结束收尾（幂等）——镜像右上下文扫完尾部。"""
        if self._finalized:
            return
        self._finalized = True
        pending = self._pending
        if pending is None or pending.shape[1] == 0:
            return
        if not self._head_initialized:
            # 短流：从未攒满 24 个样本——两侧都用镜像
            self._state = self._mirror_to_context(
                pending[:, : self.CONTEXT], self.CONTEXT
            )
        right = self._mirror_to_context(
            pending[:, -self.CONTEXT:], self.CONTEXT
        )
        self._scan_content(self._state, pending, right)
        self._pending = None


@dataclass(frozen=True)
class LoudnessReport:
    """离线响度分析报告（不可变）。

    Attributes:
        integrated_lufs: Integrated LUFS（gated 累积，整段）。仅当 backend_available=True 时可信。
        true_peak_dbtp: True Peak（dBTP，样本峰值近似）。基于 np.abs 计算，不依赖 scipy，始终可信。
        loudness_range_lu: Loudness Range（LU，short-term 分布的 [10%, 95%] 分位差）
        sample_rate: 分析所用采样率
        duration_seconds: 分析时长
        target_lufs: 目标 LUFS（normalize 用，None=未指定）
        normalize_gain_db: 达到 target 所需增益（dB），None=未计算
        warnings: 超标/异常警告列表
        backend_available: BS.1770 后端（scipy）是否可用。False 时 integrated_lufs/loudness_range_lu
            为不可信的 floor 值，调用方不得据此 normalize 或判定响度达标（RMA-01）。
    """
    integrated_lufs: float = -70.0
    true_peak_dbtp: float = -np.inf  # 4x 过采样 intersample 峰值（FE-B15）
    loudness_range_lu: float = 0.0
    sample_rate: float = 48000.0
    duration_seconds: float = 0.0
    target_lufs: Optional[float] = None
    normalize_gain_db: Optional[float] = None
    warnings: tuple = field(default_factory=tuple)
    backend_available: bool = True

    def to_dict(self) -> dict:
        return {
            "integrated_lufs": round(self.integrated_lufs, 2),
            "true_peak_dbtp": round(self.true_peak_dbtp, 2) if np.isfinite(self.true_peak_dbtp) else None,
            "loudness_range_lu": round(self.loudness_range_lu, 2),
            "sample_rate": int(self.sample_rate),
            "duration_seconds": round(self.duration_seconds, 3),
            "target_lufs": round(self.target_lufs, 2) if self.target_lufs is not None else None,
            "normalize_gain_db": round(self.normalize_gain_db, 2) if self.normalize_gain_db is not None else None,
            "warnings": list(self.warnings),
            "backend_available": self.backend_available,
        }

    def to_text(self) -> str:
        """人类可读的响度报告文本。"""
        lines = [
            "=== Loudness Report (BS.1770-4) ===",
            f"Integrated LUFS : {self.integrated_lufs:.1f}",
            f"True Peak (dBTP): {self.true_peak_dbtp:.1f}" if np.isfinite(self.true_peak_dbtp) else "True Peak (dBTP): -inf",
            f"Loudness Range  : {self.loudness_range_lu:.1f} LU",
            f"Duration        : {self.duration_seconds:.2f} s",
            f"Sample Rate     : {int(self.sample_rate)} Hz",
        ]
        if self.target_lufs is not None:
            lines.append(f"Target LUFS     : {self.target_lufs:.1f}")
            if self.normalize_gain_db is not None:
                lines.append(f"Normalize Gain  : {self.normalize_gain_db:+.1f} dB")
        if not self.backend_available:
            lines.append("Backend         : BS.1770 backend (scipy) UNAVAILABLE — "
                         "integrated LUFS / range are NOT measured and must not be trusted")
        if self.warnings:
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        return "\n".join(lines)


class LoudnessAnalysisCancelled(RuntimeError):
    """R27：响度分析被取消（progress 回调返回 False）。"""


def analyze_loudness(samples: np.ndarray, sample_rate: float = 48000.0,
                     target_lufs: Optional[float] = None,
                     block_size: int = 4800,
                     progress: Optional[Callable[[float], bool]] = None) -> LoudnessReport:
    """离线分析整段 PCM 的响度（AM-10 阶段 3.4）。

    Args:
        samples: (channels, total_samples) PCM，float32/64
        sample_rate: 采样率
        target_lufs: 目标 LUFS（提供则计算 normalize 增益），None=不计算
        block_size: 分块大小（样本数，避免一次性内存峰值）

    Returns:
        LoudnessReport：含 integrated/true-peak/range/normalize 增益/警告

    Note:
        当 BS.1770 后端（scipy）不可用时，返回的 report.backend_available=False，
        此时 integrated_lufs/loudness_range_lu 为不可信 floor 值，调用方不得据此
        normalize 或判定响度达标（RMA-01：不得把后端缺失伪装成"信号静音"）。
        true_peak_dbtp 基于 4x 过采样插值估算（FE-B15：windowed-sinc
        插值捕获样本间过冲，跨块滤波状态），不依赖 scipy，始终可信。
    """
    backend_available = is_scipy_available_for_dsp()
    meter = LoudnessMeter(sample_rate, channels=int(samples.shape[0]))
    # FE-B15：有状态 4x 过采样 true-peak 估计器（跨块滤波状态，能捕获
    # 样本间过冲）；样本峰值保留为 fallback 与对照。
    tp_estimator = _TruePeakEstimator(channels=int(samples.shape[0]))
    sample_peak = 0.0
    # 收集 short-term LUFS 用于 loudness range
    st_lufs_values: list = []

    total = samples.shape[-1]
    cancelled = False
    for i in range(0, total, block_size):
        block = samples[:, i:i + block_size]
        meter.process(block)
        tp_estimator.process(block.astype(np.float64, copy=False))
        sample_peak = max(
            sample_peak,
            float(np.max(np.abs(block))) if block.size > 0 else 0.0,
        )
        st = meter.short_term_lufs
        if st > -70.0:
            st_lufs_values.append(st)
        # R27：progress 回调返回 False 即取消——立即停止，不发布结果
        if progress is not None:
            try:
                if progress(i / max(1, total)) is False:
                    cancelled = True
                    break
            except Exception:
                pass
    if cancelled:
        raise LoudnessAnalysisCancelled()

    # R27：显式冲刷流尾——文件尾的 intersample 峰不再漏报
    tp_estimator.finalize()

    true_peak = max(tp_estimator.peak, sample_peak)

    integrated = meter.integrated_lufs

    # Loudness Range（简化：short-term 分布的 10%-95% 分位差）
    if len(st_lufs_values) >= 10:
        st_arr = np.array(st_lufs_values)
        lra = float(np.percentile(st_arr, 95) - np.percentile(st_arr, 10))
    else:
        lra = 0.0

    # true peak → dBTP
    true_peak_dbtp = 20.0 * np.log10(true_peak) if true_peak > 1e-12 else -np.inf

    # normalize 增益（达目标所需 dB）。后端不可用时强制不计算——
    # 避免基于不可信 floor 值 (-70) 给出 +47 dB 的荒谬增益（RMA-01）。
    normalize_gain_db = None
    if backend_available and target_lufs is not None and integrated > -70.0:
        normalize_gain_db = target_lufs - integrated

    # 警告
    warnings = []
    if not backend_available:
        warnings.append(
            "BS.1770 后端 (scipy) 不可用：integrated LUFS / loudness range 未测量，"
            "本报告不可用于响度 normalize 或交付达标判定"
        )
    if integrated <= -70.0:
        warnings.append("信号过低或为静音，integrated LUFS 为 floor 值")
    if np.isfinite(true_peak_dbtp) and true_peak_dbtp > -1.0:
        warnings.append(f"True Peak {true_peak_dbtp:.1f} dBTP 接近或超过 -1 dBTP，可能削波")
    if target_lufs is not None and normalize_gain_db is not None:
        if normalize_gain_db > 0 and np.isfinite(true_peak_dbtp):
            predicted_peak = true_peak_dbtp + normalize_gain_db
            if predicted_peak > 0.0:
                warnings.append(
                    f"Normalize +{normalize_gain_db:.1f} dB 会导致 True Peak 达 {predicted_peak:.1f} dBTP（超 0），需限幅"
                )

    duration = total / sample_rate if sample_rate > 0 else 0.0

    return LoudnessReport(
        integrated_lufs=integrated,
        true_peak_dbtp=true_peak_dbtp,
        loudness_range_lu=max(0.0, lra),
        sample_rate=float(sample_rate),
        duration_seconds=float(duration),
        target_lufs=target_lufs,
        normalize_gain_db=normalize_gain_db,
        warnings=tuple(warnings),
        backend_available=backend_available,
    )


def compute_normalize_gain_db(integrated_lufs: float, target_lufs: float) -> float:
    """计算到达目标 LUFS 所需的线性增益（dB）。

    normalize 增益 = target - integrated（LU 差即 dB 差）。
    负值=衰减，正值=放大。
    """
    return float(target_lufs) - float(integrated_lufs)
