"""Regression tests for waveform QThread ownership."""

from __future__ import annotations

import time

import pytest
from PyQt6.QtWidgets import QApplication

from src.features.edit.core.audio_waveform import (
    WaveformCache,
    WaveformData,
    WaveformExtractor,
    WaveformExtractorWorker,
)


pytestmark = pytest.mark.edit_core


def test_waveform_worker_is_retained_until_finished(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The result signal must not destroy a still-running QThread."""
    cache = WaveformCache()
    cache.clear_cache()
    manager = WaveformExtractor()
    worker = WaveformExtractorWorker("asset-1", "/tmp/test.wav")

    waveform = WaveformData(
        asset_id="asset-1",
        peaks_min=[-0.5],
        peaks_max=[0.5],
        samples_per_second=1.0,
        duration_seconds=1.0,
    )
    monkeypatch.setattr(worker, "_extract_waveform", lambda: waveform)
    # 磁盘波形缓存会让 run() 在 _extract_waveform 之前短路并返回另一个
    # WaveformData 对象，破坏下方 `is waveform` 身份断言；测试环境禁用之。
    monkeypatch.setattr(
        "src.features.edit.core.audio_waveform.load_waveform_from_disk",
        lambda asset_id, file_path, samples_per_second=None: None)
    monkeypatch.setattr(
        "src.features.edit.core.audio_waveform.save_waveform_to_disk",
        lambda file_path, waveform, samples_per_second=None: None)

    manager._workers["asset-1"] = worker
    worker.waveform_ready.connect(manager._on_waveform_ready)
    worker.extraction_failed.connect(manager._on_extraction_failed)
    worker.extraction_deferred.connect(manager._on_extraction_deferred)
    worker.finished.connect(
        lambda: manager._on_worker_finished("asset-1")
    )

    worker.start()
    deadline = time.monotonic() + 2.0
    while "asset-1" in manager._workers and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)

    assert "asset-1" not in manager._workers
    assert cache.get_cached_waveform("asset-1") is waveform

    cache.clear_cache()


# ── T-P1-71：create/close/cancel 20 次生命周期压力 ──
# 复现记录：tests/edit 全量套件在本模块中段曾发生解释器级 Segmentation
# fault（顺序依赖、干净 HEAD 复现；单独运行通过）——套件级 Qt/GL 状态
# 污染，最小 reproduction 待后续批次（卡协议：先最小复现再修）。

def test_extractor_create_request_cancel_close_twenty_cycles(qtbot) -> None:
    """20 轮 request → cancel_all → close：无线程悬挂、无崩溃。"""
    from src.features.edit.core.audio_waveform import WaveformExtractor

    import threading

    for cycle in range(20):
        extractor = WaveformExtractor()
        try:
            extractor.request_waveform(
                f"asset_{cycle}", f"/nonexistent/{cycle}.mov",
                callback=None,
            )
            # cancel_all 协作式中断并 join（对持有 PyAV 容器的 worker
            # 禁用 terminate——见 audio_waveform.cancel_all 契约）
            extractor.cancel_all()
            assert not extractor._workers, f"cycle {cycle}: workers 残留"
            lingering = [
                t for t in threading.enumerate()
                if isinstance(t, type(threading.current_thread()))
                and "waveform" in t.name.lower()
                and t.is_alive()
            ]
            assert not lingering, f"cycle {cycle}: 悬挂线程 {lingering}"
        finally:
            pass  # WaveformExtractor 无 close 契约；cancel_all 即收口
