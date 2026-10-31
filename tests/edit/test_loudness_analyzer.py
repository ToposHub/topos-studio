"""
Offline Loudness Analyzer 测试（AM-10 阶段 3.4）。

验证离线响度分析、normalize 增益计算、报告生成。

验收标准（计划表 3.4）：
- 导出指定 target，报告与复测一致
- 报告作为 JSON/文本可保存
- 不在未经确认时破坏性改源（normalize 只计算增益）
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.loudness_analyzer import (
    LoudnessReport,
    analyze_loudness,
    compute_normalize_gain_db,
)


SR = 48000.0


def _sine(amp: float, freq: float, duration: float, sr: float = SR) -> np.ndarray:
    """生成立体声正弦 PCM (2, n)。"""
    n = int(sr * duration)
    t = np.arange(n) / sr
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig, sig])


class TestAnalyzeLoudness:
    """离线响度分析。"""

    def test_calibrated_signal_minus_23_lufs(self):
        """BS.1770-4 校准信号 → integrated ≈ -23 LUFS。

        F3.4 公式修复后：dual-mono 每声道峰值 0.0708（声道求和语义；
        旧值 0.1001 是在为归一化缺陷补偿）。"""
        peak = 0.0708
        samples = _sine(peak, 1000, 10.0)
        report = analyze_loudness(samples, SR)
        assert abs(report.integrated_lufs - (-23.0)) < 0.5, \
            f"期望 -23 LUFS，实际 {report.integrated_lufs:.2f}"

    def test_silence_floor(self):
        """静音 → -70 floor + 警告。"""
        samples = np.zeros((2, int(SR * 2)), dtype=np.float32)
        report = analyze_loudness(samples, SR)
        assert report.integrated_lufs == -70.0
        assert any("静音" in w or "过低" in w for w in report.warnings)

    def test_true_peak_reported(self):
        """True Peak 被报告（dBTP）。"""
        samples = _sine(0.5, 1000, 2.0)
        report = analyze_loudness(samples, SR)
        # 0.5 峰值 → 约 -6 dBTP
        assert report.true_peak_dbtp < 0.0
        assert report.true_peak_dbtp > -10.0

    def test_high_peak_warning(self):
        """接近 0 dBTP 的信号触发警告。"""
        samples = _sine(0.99, 1000, 2.0)
        report = analyze_loudness(samples, SR)
        assert any("削波" in w or "True Peak" in w for w in report.warnings)

    def test_duration_reported(self):
        """报告含时长。"""
        samples = _sine(0.05 * np.sqrt(2), 1000, 3.0)
        report = analyze_loudness(samples, SR)
        assert abs(report.duration_seconds - 3.0) < 0.1


class TestNormalizeGain:
    """normalize 增益计算。"""

    def test_normalize_gain_to_target(self):
        """提供 target → 计算 normalize 增益。"""
        peak = 0.0708
        samples = _sine(peak, 1000, 10.0)
        # 当前 -23 LUFS，目标 -16 → 增益 +7 dB
        report = analyze_loudness(samples, SR, target_lufs=-16.0)
        assert report.normalize_gain_db is not None
        assert abs(report.normalize_gain_db - 7.0) < 0.5, \
            f"期望 +7 dB，实际 {report.normalize_gain_db:.2f}"

    def test_normalize_gain_negative_for_louder_target_lower(self):
        """目标更低 → 负增益（衰减）。"""
        peak = 0.0708
        samples = _sine(peak, 1000, 10.0)  # -23 LUFS
        report = analyze_loudness(samples, SR, target_lufs=-24.0)
        assert report.normalize_gain_db is not None
        assert report.normalize_gain_db < 0  # 衰减

    def test_compute_normalize_gain_db_function(self):
        """独立函数计算增益。"""
        assert compute_normalize_gain_db(-23.0, -16.0) == pytest.approx(7.0)
        assert compute_normalize_gain_db(-20.0, -24.0) == pytest.approx(-4.0)

    def test_normalize_clipping_warning(self):
        """normalize 放大导致超 0 dBTP → 警告。"""
        # 接近满量程的安静信号（low LUFS but high peak），放大到目标会削波
        peak = 0.9
        samples = _sine(peak, 1000, 5.0)
        report = analyze_loudness(samples, SR, target_lufs=0.0)  # 目标 0 LUFS（很大）
        if report.normalize_gain_db is not None and report.normalize_gain_db > 0:
            assert any("限幅" in w or "True Peak" in w for w in report.warnings)


class TestReport:
    """报告序列化。"""

    def test_to_dict(self):
        """报告转 dict（供 JSON 保存）。"""
        peak = 0.0708 * np.sqrt(2)
        samples = _sine(peak, 1000, 5.0)
        report = analyze_loudness(samples, SR, target_lufs=-23.0)
        d = report.to_dict()
        assert "integrated_lufs" in d
        assert "true_peak_dbtp" in d
        assert "target_lufs" in d
        assert "normalize_gain_db" in d
        assert "warnings" in d

    def test_to_text_human_readable(self):
        """报告转文本（人类可读）。"""
        samples = _sine(0.05 * np.sqrt(2), 1000, 2.0)
        report = analyze_loudness(samples, SR, target_lufs=-23.0)
        text = report.to_text()
        assert "Loudness Report" in text
        assert "Integrated LUFS" in text
        assert "True Peak" in text

    def test_report_immutable(self):
        """报告不可变（frozen dataclass）。"""
        report = LoudnessReport(integrated_lufs=-20.0)
        with pytest.raises((AttributeError, Exception)):
            report.integrated_lufs = -10.0  # type: ignore
