"""
LUFS 响度计测试（W10）。

覆盖：
1. 校准：已知幅度正弦波的 LUFS 读数
2. 单调性：更大信号 → 更高 LUFS
3. 动态范围：振幅比 → LUFS 差 ≈ 20·log10(amp 比)
4. 无信号 → -70 LUFS floor
5. reset() 重置状态
6. 跨块连续性（分块 vs 整块结果一致）
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.loudness_meter import LoudnessMeter


def _sine_block(amp: float, freq: float, sr: float, duration_s: float, channels: int = 2) -> np.ndarray:
    """生成正弦波 stereo block。"""
    n = int(sr * duration_s)
    t = np.arange(n) / sr
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig] * channels)


class TestLoudnessMeterCalibration:
    """LUFS 校准与基本测量。"""

    def test_no_signal_returns_floor(self):
        """无信号时所有 LUFS = -70（floor）。"""
        m = LoudnessMeter(48000.0)
        assert m.momentary_lufs == -70.0
        assert m.short_term_lufs == -70.0
        assert m.integrated_lufs == -70.0

    def test_calibration_23_lufs(self):
        """BS.1770-4 校准：dual-mono 每声道峰值 0.0708 的 1kHz 正弦
        → 约 -23 LUFS（F3.4 公式修复后：声道求和不归一化，dual-mono
        比 mono 同电平高 3.01 LU——0.1084 为 mono 校准幅度）。"""
        sr = 48000.0
        amp = 0.1084 / np.sqrt(2)  # dual-mono 等效 -23 LUFS
        samples = _sine_block(amp, 1000.0, sr, 10.0)
        m = LoudnessMeter(sr, channels=2)
        # 分块喂入
        for i in range(0, samples.shape[1], 4800):
            m.process(samples[:, i:i+4800])
        # 容差 ±1.5（K-weighting 在 1kHz +0.7dB，加上实现细节）
        assert abs(m.integrated_lufs - (-23.0)) < 1.5, \
            f"integrated {m.integrated_lufs:.2f} not within 1.5 of -23"

    def test_momentary_responds_to_signal(self):
        """有信号时 Momentary LUFS 高于 floor。"""
        sr = 48000.0
        samples = _sine_block(0.5, 1000.0, sr, 2.0)
        m = LoudnessMeter(sr)
        for i in range(0, samples.shape[1], 4800):
            m.process(samples[:, i:i+4800])
        assert m.momentary_lufs > -10.0  # 0.5 振幅应该约 -9 LUFS


class TestLoudnessMonotonicity:
    """LUFS 单调性：更大信号 → 更高 LUFS。"""

    def test_louder_signal_higher_lufs(self):
        sr = 48000.0
        quiet = _sine_block(0.05, 1000.0, sr, 2.0)
        loud = _sine_block(0.5, 1000.0, sr, 2.0)

        m_quiet = LoudnessMeter(sr)
        m_quiet.process(quiet)
        m_loud = LoudnessMeter(sr)
        m_loud.process(loud)

        assert m_loud.integrated_lufs > m_quiet.integrated_lufs

    def test_dynamic_range_20db(self):
        """振幅比 10x → LUFS 差约 20 dB。"""
        sr = 48000.0
        m1 = LoudnessMeter(sr)
        m1.process(_sine_block(0.05, 1000.0, sr, 2.0))
        m2 = LoudnessMeter(sr)
        m2.process(_sine_block(0.5, 1000.0, sr, 2.0))

        diff = m2.integrated_lufs - m1.integrated_lufs
        assert 18 < diff < 22, f"LUFS diff {diff:.2f} not ~20 dB"


class TestLoudnessReset:
    """reset() 重置状态。"""

    def test_reset_clears_state(self):
        sr = 48000.0
        m = LoudnessMeter(sr)
        m.process(_sine_block(0.5, 1000.0, sr, 2.0))
        assert m.integrated_lufs > -10.0

        m.reset()
        assert m.momentary_lufs == -70.0
        assert m.short_term_lufs == -70.0
        assert m.integrated_lufs == -70.0


class TestLoudnessContinuity:
    """跨块连续性：分块 vs 整块结果应一致。"""

    def test_chunked_equals_whole(self):
        sr = 48000.0
        samples = _sine_block(0.3, 1000.0, sr, 3.0)

        # 整块
        m_whole = LoudnessMeter(sr)
        m_whole.process(samples)

        # 分块（100ms）
        m_chunked = LoudnessMeter(sr)
        for i in range(0, samples.shape[1], 4800):
            m_chunked.process(samples[:, i:i+4800])

        # short-term 应接近（分块略因边界不同，容差 0.5 LUFS）
        assert abs(m_chunked.short_term_lufs - m_whole.short_term_lufs) < 0.5


# =============================================================================
# AM-10: BS.1770-4 校准与两阶段 gating
# =============================================================================

class TestBS1770Calibration:
    """BS.1770-4 校准点验证（AM-10 验收：与参考误差 ≤0.5 LU）。"""

    def test_calibrated_1khz_minus_23_dbfs(self):
        """BS.1770-4 标准校准点：dual-mono 每声道峰值 0.0708 的 1kHz
        正弦 → -23.0 ±0.5 LUFS。

        F3.4 公式修复后 ΣG·z 求和语义：每声道 mean-square z=2.51e-3，
        Σ=5.02e-3 → -0.691+10lg(Σ)≈-23.7，K-weight 在 1kHz +0.69dB
        抵消 -0.691 偏置 → -23.0。（旧测试峰值 0.1001 是在为归一化
        缺陷补偿，见 loudness_meter.py F3.4 修复注释。）
        """
        sr = 48000.0
        duration = 10.0  # 足够长让 integrated gating 稳定
        n = int(sr * duration)
        t = np.arange(n) / sr
        peak = 0.0708  # dual-mono 每声道峰值（标准校准参考）
        tone = peak * np.sin(2 * np.pi * 1000 * t)
        stereo = np.stack([tone, tone], axis=0).astype(np.float32)

        m = LoudnessMeter(sr, 2)
        for i in range(0, n, 4800):
            m.process(stereo[:, i:i + 4800])

        assert abs(m.integrated_lufs - (-23.0)) < 0.5, \
            f"BS.1770-4 校准: 期望 -23.0 LUFS，实际 {m.integrated_lufs:.2f}"

    def test_silence_minus_70_floor(self):
        """静音信号 → -70 LUFS floor。"""
        sr = 48000.0
        m = LoudnessMeter(sr, 2)
        silence = np.zeros((2, 48000), dtype=np.float32)
        m.process(silence)
        assert m.integrated_lufs == -70.0

    def test_louder_signal_higher_lufs(self):
        """更大信号 → 更高 LUFS（单调性）。"""
        sr = 48000.0
        n = int(sr * 5)
        t = np.arange(n) / sr
        peak_low = 0.05 * np.sqrt(2)
        peak_high = 0.2 * np.sqrt(2)
        low = np.stack([peak_low * np.sin(2*np.pi*1000*t)] * 2, axis=0).astype(np.float32)
        high = np.stack([peak_high * np.sin(2*np.pi*1000*t)] * 2, axis=0).astype(np.float32)

        m_low = LoudnessMeter(sr, 2)
        m_high = LoudnessMeter(sr, 2)
        for i in range(0, n, 4800):
            m_low.process(low[:, i:i+4800])
            m_high.process(high[:, i:i+4800])
        assert m_high.integrated_lufs > m_low.integrated_lufs


class TestTwoStageGating:
    """BS.1770-4 两阶段 gating（AM-10 修正）。"""

    def test_absolute_gate_excludes_silent_blocks(self):
        """低于 -70 LUFS 的块被绝对门限排除。"""
        sr = 48000.0
        m = LoudnessMeter(sr, 2)
        # 极低信号（远低于 -70 LUFS）
        quiet = np.full((2, 48000), 1e-6, dtype=np.float32)
        m.process(quiet)
        # 全部被绝对门限排除 → integrated 保持 -70
        assert m.integrated_lufs == -70.0

    def test_relative_gate_recomputes(self):
        """相对门限（-10 LU）重新筛选块，高于门限的块计入 integrated。"""
        sr = 48000.0
        m = LoudnessMeter(sr, 2)
        n = int(sr * 5)
        t = np.arange(n) / sr
        # 持续中等多度信号（高于 -70 绝对门限，应通过两阶段 gating）
        peak = 0.05 * np.sqrt(2)
        sig = np.stack([peak * np.sin(2*np.pi*1000*t)] * 2, axis=0).astype(np.float32)
        for i in range(0, n, 4800):
            m.process(sig[:, i:i+4800])
        # 应有有效 integrated（非 -70 floor）
        assert m.integrated_lufs > -70.0


class TestMultiSampleRateKWeighting:
    """多采样率 K-weighting（AM-10）。"""

    @pytest.mark.parametrize("sr", [44100.0, 48000.0])
    def test_k_weighting_available_for_common_rates(self, sr):
        """44.1k / 48k 都有标准 K-weighting 系数。"""
        from src.features.edit.core.loudness_meter import _get_k_weighting_coeffs
        (pre_b, pre_a), (rlb_b, rlb_a) = _get_k_weighting_coeffs(sr)
        # 系数非空且长度正确
        assert len(pre_b) == 3 and len(pre_a) == 3
        assert len(rlb_b) == 3 and len(rlb_a) == 3

    def test_44100_close_to_48000_calibration(self):
        """44.1kHz 下校准点也应接近 -23 LUFS（标准系数）。"""
        sr = 44100.0
        n = int(sr * 8)
        t = np.arange(n) / sr
        peak = 0.0708  # dual-mono 每声道峰值（与 48k 同校准点）
        tone = peak * np.sin(2 * np.pi * 1000 * t)
        stereo = np.stack([tone, tone], axis=0).astype(np.float32)

        m = LoudnessMeter(sr, 2)
        for i in range(0, n, 4410):
            m.process(stereo[:, i:i+4410])
        # 44.1k 标准系数，误差应 ≤1.0 LU（略宽于 48k）
        assert abs(m.integrated_lufs - (-23.0)) < 1.0, \
            f"44.1k 校准: 期望≈-23.0，实际 {m.integrated_lufs:.2f}"



# =====================================================================
# F3.4 · 响度声道语义向量（BS.1770-4 求和公式验收）
# =====================================================================

class TestChannelSemanticsVectors:
    """mono/dual-mono/反相/不相关向量：声道求和（非归一化）语义。"""

    @staticmethod
    def _lufs(samples: np.ndarray, sr: float = 48000.0) -> float:
        m = LoudnessMeter(sr, samples.shape[0])
        for i in range(0, samples.shape[1], 4800):
            m.process(samples[:, i:i + 4800])
        return m.integrated_lufs

    def test_dual_mono_plus_3db_over_mono(self) -> None:
        """同电平 dual-mono 立体声比 mono 响 3.01 LU（BS.1770 求和）。"""
        sr = 48000.0
        t = np.arange(int(sr * 6)) / sr
        tone = (0.2 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        mono = self._lufs(tone[np.newaxis, :])
        dual = self._lufs(np.stack([tone, tone]))
        assert dual - mono == pytest.approx(3.01, abs=0.1)

    def test_anticorrelated_channels_sum_like_dual_mono(self) -> None:
        """反相双声道（L=-R）：LUFS 只看各声道能量（求和），仍 +3.01 LU
        ——BS.1770 不做声道间相消（这正是与峰值表的关键差异）。"""
        sr = 48000.0
        t = np.arange(int(sr * 6)) / sr
        tone = (0.2 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        mono = self._lufs(tone[np.newaxis, :])
        anti = self._lufs(np.stack([tone, -tone]))
        assert anti - mono == pytest.approx(3.01, abs=0.1)

    def test_uncorrelated_stereo_plus_3db(self) -> None:
        """不相关双声道（能量同电平）：与 dual-mono 同读数（+3.01 LU）。"""
        sr = 48000.0
        rng = np.random.default_rng(31)
        noise = (0.2 * rng.standard_normal(int(sr * 6))).astype(np.float32)
        ref = (0.2 * rng.standard_normal(int(sr * 6))).astype(np.float32)
        mono = self._lufs(ref[np.newaxis, :])
        uncorr = self._lufs(np.stack([noise, ref]))
        assert uncorr - mono == pytest.approx(3.01, abs=0.15)

    def test_silent_channel_excluded(self) -> None:
        """一声道静音：读数退化为 mono（+0 LU，无 -inf 传播）。"""
        sr = 48000.0
        t = np.arange(int(sr * 6)) / sr
        tone = (0.2 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        mono = self._lufs(tone[np.newaxis, :])
        half = self._lufs(np.stack([tone, np.zeros_like(tone)]))
        assert half - mono == pytest.approx(0.0, abs=0.1)
