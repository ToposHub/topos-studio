"""
音频健康报告测试（RMA-12 阶段 D2）。

验收门槛（计划表 D2）：
- 五类失败状态（无 SciPy、无输入设备、非法 route、磁盘不足、缓存待清理）均能解释、
  阻止或安全降级；报告可导出。
- Purge Cache 不可用时入口禁用或给出原因（不假装可用）。

本测试覆盖 AudioHealthReport 聚合（DSP/device/graph/disk/cache）、磁盘/缓存探测、
Purge Cache flag 行为。不依赖真实声卡。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_health import (
    AudioHealthReport, DiskStatus, CacheStatus,
    build_audio_health_report,
    probe_disk_free_gb, probe_dir_size_mb,
    DISK_WARNING_THRESHOLD_GB, DISK_BLOCKING_THRESHOLD_GB,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 磁盘 / 缓存探测
# ─────────────────────────────────────────────────────────────────────────────

class TestDiskAndCacheProbes:
    """RMA-12: 磁盘空间与缓存目录探测。"""

    def test_probe_disk_free_gb_returns_positive(self, tmp_path):
        """存在的路径 → 返回正的剩余空间。"""
        free = probe_disk_free_gb(str(tmp_path))
        assert free is not None
        assert free > 0.0

    def test_probe_disk_free_gb_nonexistent_falls_back(self):
        """不存在的路径 → 退化到父级/cwd，不抛错。"""
        free = probe_disk_free_gb("/nonexistent/path/that/does/not/exist")
        # 应退化到 cwd 或父级，返回值或 None（不抛异常）
        assert free is None or free > 0.0

    def test_probe_dir_size_mb_measures_files(self, tmp_path):
        """目录大小探测累加文件。"""
        (tmp_path / "a.bin").write_bytes(b"\x00" * 2048)
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.bin").write_bytes(b"\x00" * 1024)
        size = probe_dir_size_mb(str(tmp_path))
        assert size is not None
        assert size > 0.0  # 3072 bytes > 0

    def test_probe_dir_size_mb_nonexistent_returns_none(self):
        """不存在的目录 → None。"""
        assert probe_dir_size_mb("/nonexistent/cache/dir") is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. AudioHealthReport 聚合
# ─────────────────────────────────────────────────────────────────────────────

class TestAudioHealthReportAggregation:
    """RMA-12: build_audio_health_report 聚合各维度。"""

    def test_healthy_report(self, tmp_path):
        """健康状态：无阻断、无告警（默认环境 scipy 可用）。"""
        report = build_audio_health_report(output_path=str(tmp_path))
        assert report.dsp_backend_available is True
        assert report.eq_lufs_available is True
        assert report.has_blocking is False

    def test_graph_invalid_makes_blocking(self):
        """非法路由图 → has_blocking True。"""
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
        t = EditTrack(track_id="t1", track_type="audio", index=0,
                      output_bus_id="ghost_bus")
        seq = TimelineSequence(audio_tracks=(t,))
        report = build_audio_health_report(sequence=seq)
        assert report.graph_valid is False
        assert len(report.graph_errors) > 0
        assert report.has_blocking is True

    def test_graph_valid_passes(self):
        """有效路由图 → graph_valid True，不阻断。"""
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
        t = EditTrack(track_id="t1", track_type="audio", index=0)
        seq = TimelineSequence(audio_tracks=(t,))
        report = build_audio_health_report(sequence=seq)
        assert report.graph_valid is True
        assert report.has_blocking is False

    def test_disk_warning_threshold(self, tmp_path, monkeypatch):
        """磁盘剩余 < WARNING 阈值 → warning True。"""
        monkeypatch.setattr("src.features.edit.core.audio_health.probe_disk_free_gb",
                            lambda path: DISK_WARNING_THRESHOLD_GB - 0.5)
        report = build_audio_health_report(output_path=str(tmp_path))
        assert report.disk.warning is True
        assert report.disk.blocking is False
        assert report.has_warnings is True

    def test_disk_blocking_threshold(self, tmp_path, monkeypatch):
        """磁盘剩余 < BLOCKING 阈值 → blocking True，has_blocking True。"""
        monkeypatch.setattr("src.features.edit.core.audio_health.probe_disk_free_gb",
                            lambda path: DISK_BLOCKING_THRESHOLD_GB - 0.5)
        report = build_audio_health_report(output_path=str(tmp_path))
        assert report.disk.blocking is True
        assert report.has_blocking is True

    def test_cache_status_reported(self, tmp_path):
        """缓存目录大小被报告。"""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "a.bin").write_bytes(b"\x00" * 4096)
        report = build_audio_health_report(cache_dirs=(str(cache_dir),))
        assert len(report.caches) == 1
        assert report.caches[0].size_mb is not None
        assert report.caches[0].size_mb > 0.0
        assert report.caches[0].purgable is True

    def test_underrun_warning(self, tmp_path):
        """underrun > 0 → has_warnings True。"""
        report = build_audio_health_report(output_path=str(tmp_path), underruns=5)
        assert report.underruns == 5
        assert report.has_warnings is True

    def test_scipy_unavailable_warning(self, tmp_path, monkeypatch):
        """scipy 不可用 → eq_lufs_available False，has_warnings True。"""
        from src.features.edit.core import audio_capabilities as mod
        monkeypatch.setattr(mod, "_probe_scipy",
                            lambda: mod.CapabilityResult(False, "simulated"))
        mod.reset_audio_capabilities_cache()
        try:
            report = build_audio_health_report(output_path=str(tmp_path))
            assert report.eq_lufs_available is False
            assert report.has_warnings is True
        finally:
            mod.reset_audio_capabilities_cache()

    def test_report_serializes_to_dict_and_text(self, tmp_path):
        """报告可序列化为 dict 与人类可读文本（供 issue 导出）。"""
        report = build_audio_health_report(
            output_path=str(tmp_path), sample_rate=48000, output_channels=2,
        )
        d = report.to_dict()
        assert "dsp_backend_available" in d
        assert "has_blocking" in d
        txt = report.to_text()
        assert "Audio Health Report" in txt
        assert "Sample rate" in txt


# ─────────────────────────────────────────────────────────────────────────────
# 3. Purge Cache 死路径清理（E17）
# ─────────────────────────────────────────────────────────────────────────────

class TestPurgeCacheCleanup:
    """E17: purge cache no-op 路径已删除——不再保留"有声明无入口"的死信号。"""

    def test_purge_cache_signal_removed(self):
        """AudioMixerPanel 不再声明 purge_cache 信号。"""
        from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel
        assert not hasattr(AudioMixerPanel, "purge_cache")

    def test_purge_cache_flag_removed(self):
        """mixer_feature_flags 不再导出 PURGE_CACHE_IMPLEMENTED。"""
        import src.shared.ui.audio_mixer.mixer_feature_flags as flags
        assert not hasattr(flags, "PURGE_CACHE_IMPLEMENTED")

    def test_controller_slot_removed(self):
        """EditControllerMixin 不再有 no-op 的 _on_mixer_purge_cache。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        assert not hasattr(EditControllerMixin, "_on_mixer_purge_cache")
