"""R10：设备格式回退的 manager 广播（软件可测部分，计划 §5 R10）。

复审缺口：播放线程设备 fallback 只同步线程内 clock/mixer/decoder，未经
manager 的 ``sample_rate_changed`` 通知上层（EQ 曲线/频谱轴/解码时基仍
按 requested 速率重建——F02 只修了收到通知后的接收侧）。

本批修复：线程 emit ``device_format_changed(actual_rate, channels)`` →
manager handler 更新真源 + 广播 ``sample_rate_changed``。真机拔插/磁盘
故障/自然退出清理仍列"待硬件"（计划要求不得改文档为完成）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.audio_playback import AudioPlaybackManager


class TestDeviceFormatPropagation:
    def test_thread_signal_reaches_manager_broadcast(self):
        mgr = AudioPlaybackManager()
        rates: list = []
        mgr.sample_rate_changed.connect(lambda r: rates.append(r))
        # 模拟线程回报：requested 48000 → 设备实际 44100
        mgr._on_device_format_changed(44100.0, 2)
        assert rates == [44100.0]
        assert mgr._sample_rate == 44100
        # 重复回报同率：不重复广播（避免 UI 重建抖动）
        mgr._on_device_format_changed(44100.0, 2)
        assert rates == [44100.0]
        # 非法率被拒
        mgr._on_device_format_changed(-1.0, 2)
        assert rates == [44100.0]

    def test_thread_signal_wired(self):
        """AudioPlaybackThread 具备 device_format_changed 信号。"""
        from src.features.edit.core.audio_playback import AudioPlaybackThread
        assert hasattr(AudioPlaybackThread, "device_format_changed")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
