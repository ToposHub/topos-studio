"""
导出 preflight 与 DSP 降级结构化错误测试（RMA-01 阶段 A1）。

验收门槛（计划表 A1）：
- 缺 SciPy 后端时，EQ/LUFS/normalize 不得静默继续（不产出未处理文件）。
- `analyze_loudness` 必须把"后端不可用"与"信号静音"区分开：
  backend_available=False、normalize_gain_db=None、warnings 明确标注后端缺失。
- `preflight_audio_export` 在 scipy 缺失 + 依赖 EQ/LUFS 时返回 ok=False 阻断。
- `TimelineExporter.export_timeline` 在 preflight 阻断时抛 ValueError 且不产出文件。

策略：用 monkeypatch 模拟 scipy 不可用（capabilities 探测返回 False），
验证上述每条语义，不依赖真实声卡/真实 scipy 缺失环境。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.loudness_analyzer import analyze_loudness


SR = 48000.0


def _sine(amp: float, freq: float, duration: float, sr: float = SR) -> np.ndarray:
    n = int(sr * duration)
    t = np.arange(n) / sr
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig, sig])


@pytest.fixture
def scipy_unavailable(monkeypatch):
    """模拟 scipy 不可用：让 capabilities 探测返回 False。

    DSP/分析层运行时查询 capabilities，monkeypatch 后立刻生效。
    """
    from src.features.edit.core import audio_capabilities as mod

    monkeypatch.setattr(mod, "_probe_scipy", lambda: mod.CapabilityResult(False, "simulated"))
    mod.reset_audio_capabilities_cache()
    yield
    mod.reset_audio_capabilities_cache()


# ─────────────────────────────────────────────────────────────────────────────
# analyze_loudness: 后端缺失的结构化报告
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeLoudnessBackendDegradation:
    """RMA-01: scipy 缺失时 analyze_loudness 必须如实标注，不伪装成静音。"""

    def test_report_marks_backend_unavailable(self, scipy_unavailable):
        """后端缺失 → backend_available=False。"""
        samples = _sine(0.1, 1000, 3.0)
        report = analyze_loudness(samples, SR, target_lufs=-23.0)
        assert report.backend_available is False

    def test_no_normalize_gain_when_backend_unavailable(self, scipy_unavailable):
        """后端缺失 → normalize_gain_db=None，不得基于 -70 floor 给出荒谬增益。"""
        samples = _sine(0.1, 1000, 3.0)
        report = analyze_loudness(samples, SR, target_lufs=-23.0)
        assert report.normalize_gain_db is None

    def test_warning_explicitly_states_backend_missing(self, scipy_unavailable):
        """warnings 必须明确写出"后端不可用"，区别于"信号静音"。"""
        samples = _sine(0.1, 1000, 3.0)
        report = analyze_loudness(samples, SR, target_lufs=-23.0)
        joined = " ".join(report.warnings)
        # 必须出现后端相关字眼（scipy / 后端 / 不可用 / 未测量）
        assert any(k in joined for k in ("scipy", "后端", "不可用", "未测量")), \
            f"warnings 未标注后端缺失: {report.warnings}"

    def test_true_peak_still_trustworthy_without_backend(self, scipy_unavailable):
        """true_peak 基于 np.abs，不依赖 scipy，后端缺失时仍可信。"""
        samples = _sine(0.5, 1000, 2.0)
        report = analyze_loudness(samples, SR)
        # 振幅 0.5 → 约 -6 dBTP
        assert report.true_peak_dbtp > -10.0
        assert np.isfinite(report.true_peak_dbtp)

    def test_report_dict_and_text_carry_backend_flag(self, scipy_unavailable):
        """to_dict / to_text 也携带后端不可用信息。"""
        samples = _sine(0.1, 1000, 2.0)
        report = analyze_loudness(samples, SR, target_lufs=-23.0)
        d = report.to_dict()
        assert d["backend_available"] is False
        txt = report.to_text()
        assert "UNAVAILABLE" in txt or "不可用" in txt


# ─────────────────────────────────────────────────────────────────────────────
# preflight_audio_export: 阻断判定
# ─────────────────────────────────────────────────────────────────────────────

class TestPreflightAudioExport:
    """RMA-01: preflight 在 scipy 缺失 + 依赖 EQ/LUFS 时阻断。"""

    def _make_sequence_with_eq(self):
        from src.features.edit.core.timeline_v2 import (
            EditTrack, StripEQParams, TimelineSequence,
        )
        eq = StripEQParams(
            enabled=True, mode="3-band",
            bands=(6.0, 0.0, -3.0), gain=0.0,
            low_cut_enable=False, low_cut=20.0,
            high_cut_enable=False, high_cut=20000.0,
        )
        track = EditTrack(
            track_id="t1", track_type="audio", index=0,
            strip_eq=eq,
        )
        return TimelineSequence(audio_tracks=(track,))

    def test_preflight_blocks_when_loudness_target_and_no_scipy(self, scipy_unavailable):
        """scipy 缺失 + loudness_target → ok=False。"""
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, preflight_audio_export,
        )
        config = TimelineExportConfig(loudness_target_lufs=-23.0)
        result = preflight_audio_export(config, sequence=None)
        assert result.ok is False
        assert len(result.blocking_messages) > 0
        assert any("scipy" in m.lower() or "SciPy" in m for m in result.blocking_messages)

    def test_preflight_blocks_when_eq_enabled_and_no_scipy(self, scipy_unavailable):
        """scipy 缺失 + 轨道启用 EQ → ok=False。"""
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, preflight_audio_export,
        )
        seq = self._make_sequence_with_eq()
        config = TimelineExportConfig()
        result = preflight_audio_export(config, sequence=seq)
        assert result.ok is False

    def test_preflight_passes_when_no_scipy_dependency(self, scipy_unavailable):
        """scipy 缺失但导出不依赖 EQ/LUFS → ok=True（纯音频导出允许）。"""
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, preflight_audio_export,
        )
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
        track = EditTrack(track_id="t1", track_type="audio", index=0)
        seq = TimelineSequence(audio_tracks=(track,))
        config = TimelineExportConfig()
        result = preflight_audio_export(config, sequence=seq)
        assert result.ok is True

    def test_preflight_passes_when_backend_available(self):
        """scipy 可用时 preflight 总是通过。"""
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, preflight_audio_export,
        )
        config = TimelineExportConfig(loudness_target_lufs=-23.0)
        result = preflight_audio_export(config, sequence=None)
        assert result.ok is True

    def test_config_requires_scipy_backend_detection(self):
        """TimelineExportConfig.requires_scipy_backend 正确识别依赖。"""
        from src.features.edit.core.timeline_export import TimelineExportConfig
        # loudness target
        assert TimelineExportConfig(loudness_target_lufs=-14.0).requires_scipy_backend() is True
        # 无 target 无 EQ
        assert TimelineExportConfig().requires_scipy_backend() is False


# ─────────────────────────────────────────────────────────────────────────────
# export_timeline: 阻断时不产出文件
# ─────────────────────────────────────────────────────────────────────────────

class TestExportTimelineBlockedByPreflight:
    """RMA-01: preflight 阻断时 export_timeline 抛 ValueError 且不产出文件。"""

    def test_export_raises_and_no_file_when_loudness_target_no_scipy(
        self, scipy_unavailable, tmp_path,
    ):
        """scipy 缺失 + loudness_target → export 抛 ValueError，output_path 不存在。"""
        from unittest.mock import MagicMock
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, TimelineExporter,
        )

        # 构造最小 sequence（无 clip，避免解码依赖）
        from src.features.edit.core.timeline_v2 import TimelineSequence
        seq = TimelineSequence(audio_tracks=())

        # project / frame_provider 用 mock（preflight 在解码前阻断，不会用到它们）
        exporter = TimelineExporter(MagicMock(), seq, MagicMock())
        out = tmp_path / "out.mov"
        config = TimelineExportConfig(
            timeline_id=seq.timeline_id,
            output_path=str(out),
            loudness_target_lufs=-23.0,
            include_audio=False,
        )
        with pytest.raises(ValueError, match="preflight"):
            exporter.export_timeline(config)
        # 阻断时不得产出文件
        assert not out.exists()
        # preflight 结果可追溯
        assert exporter.last_preflight_result is not None
        assert exporter.last_preflight_result.ok is False
