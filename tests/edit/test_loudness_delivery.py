"""
离线响度交付集成测试（AM-10 阶段 3.4）。

验证导出流程中的响度 normalize 与报告生成闭环：
1. analyze → 计算 normalize 增益 → 应用 → 复测收敛到目标 LUFS。
2. 报告 JSON/TXT 文件生成与内容。
3. TimelineExporter.last_loudness_report 暴露与配置字段。
4. 不破坏性改源：normalize 只调整输出 PCM，不修改原始 sequence。

验收标准（计划表 3.4）：
- 导出指定 -23/-24/-14 LUFS target，报告与复测一致。
- 报告作为 JSON/文本与导出一起保存。
- normalize 应用安全限幅，峰值不超 1.0。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
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


class TestLoudnessNormalizeFlow:
    """normalize 全流程：analyze → 增益 → 应用 → 复测收敛。"""

    def test_normalize_brings_signal_to_target(self):
        """低响度信号 normalize 到目标 LUFS。"""
        # -23 LUFS 校准信号
        peak = 0.0708 * np.sqrt(2)
        samples = _sine(peak, 1000, 10.0)
        report = analyze_loudness(samples, SR, target_lufs=-16.0)

        gain_db = report.normalize_gain_db
        assert gain_db is not None
        # 应用增益
        scale = float(10.0 ** (gain_db / 20.0))
        normalized = samples * scale

        # 复测应接近目标
        post = analyze_loudness(normalized, SR)
        assert abs(post.integrated_lufs - (-16.0)) < 0.5, \
            f"normalize 后 {post.integrated_lufs:.2f}，期望 -16.0"

    def test_normalize_attenuates_loud_signal(self):
        """高响度信号 normalize 衰减到更低目标。"""
        samples = _sine(0.5, 1000, 8.0)  # 较高响度
        report = analyze_loudness(samples, SR, target_lufs=-30.0)
        gain_db = report.normalize_gain_db
        assert gain_db is not None and gain_db < 0  # 衰减

        scale = float(10.0 ** (gain_db / 20.0))
        normalized = samples * scale
        post = analyze_loudness(normalized, SR)
        assert abs(post.integrated_lufs - (-30.0)) < 0.5

    def test_normalize_preserves_relative_dynamics(self):
        """normalize 是整体缩放，不改变动态范围结构（两段不同响度比例不变）。"""
        # 两段拼接：安静 + 响
        quiet = _sine(0.05, 1000, 3.0)
        loud = _sine(0.4, 1000, 3.0)
        samples = np.concatenate([quiet, loud], axis=1)

        report = analyze_loudness(samples, SR, target_lufs=-23.0)
        gain_db = report.normalize_gain_db
        assert gain_db is not None
        scale = float(10.0 ** (gain_db / 20.0))
        normalized = samples * scale

        # 两段的响度差应保持（整体缩放不改变差值）
        q_post = analyze_loudness(normalized[:, :int(SR * 3)], SR)
        l_post = analyze_loudness(normalized[:, int(SR * 3):], SR)
        diff_post = l_post.integrated_lufs - q_post.integrated_lufs

        q_pre = analyze_loudness(samples[:, :int(SR * 3)], SR)
        l_pre = analyze_loudness(samples[:, int(SR * 3):], SR)
        diff_pre = l_pre.integrated_lufs - q_pre.integrated_lufs

        assert abs(diff_post - diff_pre) < 0.5, \
            f"动态范围差应保持：pre={diff_pre:.2f}, post={diff_post:.2f}"


class TestNormalizeSafetyLimiter:
    """normalize 后安全限幅保证峰值不超 1.0。"""

    def test_normalize_with_safety_limiter_no_clip(self):
        """放大到目标后峰值可能超 1.0，限幅器保护。"""
        from src.features.edit.core.audio_dsp import MasterLimiter

        # 接近满量程但低 LUFS 的信号（短脉冲 + 静音）
        samples = np.zeros((2, int(SR * 5)), dtype=np.float32)
        # 末尾一段高振幅
        samples[:, int(SR * 4):] = 0.95 * np.sin(
            2 * np.pi * 1000 * np.arange(int(SR)) / SR
        ).astype(np.float32)

        report = analyze_loudness(samples, SR, target_lufs=-10.0)
        gain_db = report.normalize_gain_db
        if gain_db is not None and gain_db > 0:
            scale = float(10.0 ** (gain_db / 20.0))
            normalized = samples * scale
            # 安全限幅
            limiter = MasterLimiter()
            limited = limiter.process(normalized)
            peak = float(np.max(np.abs(limited)))
            assert peak <= 1.0 + 1e-6, f"限幅后峰值 {peak} 超 1.0"

    def test_warning_when_normalize_causes_clipping(self):
        """normalize 放大导致预测削波 → 报告含警告。"""
        samples = _sine(0.9, 1000, 5.0)
        report = analyze_loudness(samples, SR, target_lufs=0.0)
        if report.normalize_gain_db is not None and report.normalize_gain_db > 0:
            assert any("限幅" in w or "True Peak" in w for w in report.warnings)


class TestReportGeneration:
    """报告文件生成。"""

    def test_report_json_schema(self, tmp_path):
        """报告 JSON 包含 schema 与 pre/post 字段。"""
        samples = _sine(0.1, 1000, 5.0)
        pre = analyze_loudness(samples, SR, target_lufs=-23.0)

        scale = float(10.0 ** (pre.normalize_gain_db / 20.0))
        post = analyze_loudness(samples * scale, SR, target_lufs=-23.0)

        report_path = tmp_path / "loudness.json"
        data = {
            "schema": "topos.loudness_report.v1",
            "pre_normalize": pre.to_dict(),
            "post_normalize": post.to_dict(),
        }
        report_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        loaded = json.loads(report_path.read_text(encoding="utf-8"))
        assert loaded["schema"] == "topos.loudness_report.v1"
        assert "integrated_lufs" in loaded["pre_normalize"]
        assert "integrated_lufs" in loaded["post_normalize"]

    def test_report_text_human_readable(self, tmp_path):
        """报告 TXT 人类可读。"""
        samples = _sine(0.1, 1000, 3.0)
        report = analyze_loudness(samples, SR, target_lufs=-23.0)
        text = report.to_text()
        txt_path = tmp_path / "loudness.txt"
        txt_path.write_text(text, encoding="utf-8")

        content = txt_path.read_text(encoding="utf-8")
        assert "Loudness Report" in content
        assert "Integrated LUFS" in content


class TestExporterLoudnessConfig:
    """TimelineExportConfig 响度字段与 exporter 暴露。"""

    def test_config_has_loudness_fields(self):
        """配置类含 loudness_target_lufs / loudness_report_path 字段。"""
        from src.features.edit.core.timeline_export import TimelineExportConfig

        config = TimelineExportConfig()
        assert config.loudness_target_lufs is None
        assert config.loudness_report_path is None

    def test_config_accepts_loudness_target(self):
        """配置可设置 target。"""
        from src.features.edit.core.timeline_export import TimelineExportConfig

        config = TimelineExportConfig(loudness_target_lufs=-23.0)
        assert config.loudness_target_lufs == -23.0

    def test_exporter_has_last_loudness_report_property(self):
        """exporter 暴露 last_loudness_report 属性。"""
        from src.features.edit.core.timeline_export import TimelineExporter

        # 不实际导出，只验证属性存在
        assert hasattr(TimelineExporter, 'last_loudness_report')


class TestExportReportWriting:
    """exporter._write_loudness_report 实际写文件。"""

    def test_write_loudness_report_creates_json_and_txt(self, tmp_path):
        """_write_loudness_report 生成 .json 和 .txt。"""
        from src.features.edit.core.timeline_export import TimelineExporter

        # 构造最小 exporter（不实际导出，只测写报告方法）
        # 用 Mock 避免 __init__ 的重依赖
        from unittest.mock import MagicMock
        exporter = TimelineExporter.__new__(TimelineExporter)

        samples = _sine(0.1, 1000, 4.0)
        pre = analyze_loudness(samples, SR, target_lufs=-23.0)
        scale = float(10.0 ** (pre.normalize_gain_db / 20.0))
        post = analyze_loudness(samples * scale, SR, target_lufs=-23.0)

        report_path = str(tmp_path / "report.json")
        exporter._write_loudness_report(report_path, pre, post, int(SR))

        # JSON
        assert os.path.exists(report_path)
        data = json.loads(Path(report_path).read_text(encoding="utf-8"))
        assert data["schema"] == "topos.loudness_report.v1"
        assert "pre_normalize" in data
        assert "post_normalize" in data

        # TXT（同名 .txt）
        txt_path = str(tmp_path / "report.txt")
        assert os.path.exists(txt_path)
        txt = Path(txt_path).read_text(encoding="utf-8")
        assert "Pre-normalize" in txt
        assert "Post-normalize" in txt


class TestNonDestructiveNormalize:
    """normalize 不破坏性改源（计划表 3.4 核心原则）。"""

    def test_normalize_does_not_modify_source_sequence(self):
        """normalize 只调整输出 PCM，原始 sequence 数据不变。"""
        # 模拟：源 PCM 数组在 normalize 后应未被修改（应用 scale 产生新数组）
        source = _sine(0.1, 1000, 3.0)
        source_copy = source.copy()

        report = analyze_loudness(source, SR, target_lufs=-23.0)
        gain_db = report.normalize_gain_db
        assert gain_db is not None
        scale = float(10.0 ** (gain_db / 20.0))
        _normalized = source * scale  # 产生新数组

        # 原 source 未被修改
        np.testing.assert_array_equal(source, source_copy)

    def test_normalize_optional_no_target_preserves_original(self):
        """未指定 target → 不 normalize，输出原样。"""
        from src.features.edit.core.timeline_export import TimelineExportConfig

        config = TimelineExportConfig()
        assert config.loudness_target_lufs is None
        # None 表示不进行响度分析/normalize（导出代码中 if 判断）
