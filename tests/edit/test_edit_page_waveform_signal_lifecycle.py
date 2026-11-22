"""T-P1-71 回归：EditPage 析构不得在全局波形单例上留下悬空连接。

历史缺陷（全套件在 test_audio_waveform 处 SIGSEGV 的最小复现链）：

    EditPage.__init__ 曾连接
        WaveformExtractor.instance().status_changed.connect(
            self.waveform_status_message.emit)
    signal→.emit 的 C 函数槽不带接收者上下文——EditPage 被销毁后连接
    悬空，下一次单例 emit 直接调用已删除对象的 bound signal emit，
    原生层段错误（.ips 栈：pyqtBoundSignal_emit → PyQtSlotProxy → 悬空）。

修复：改为 signal→signal 连接（PyQt 注册接收者上下文，析构自动断开）。

本测试在单进程内断言：页面销毁后单例 emit 安全（连接已被 Qt 断开）。
若回归（改回 .emit 连接），本测试进程将以 SIGSEGV 方式失败——
这是该缺陷类唯一可靠的失败信号。
"""

from __future__ import annotations

import gc

import pytest

pytestmark = pytest.mark.edit_ui


def test_edit_page_destruction_disconnects_waveform_status(qapp) -> None:
    from PyQt6 import sip

    from src.features.edit.core.audio_waveform import WaveformExtractor
    from src.features.edit.ui.edit_page import EditPage

    singleton = WaveformExtractor.instance()
    assert not sip.isdeleted(singleton)

    page = EditPage()
    assert page.waveform_status_message is not None
    # 页面销毁（模拟测试套件中 page 局部变量被 GC 的真实时序）
    page_id = id(page)
    page.deleteLater()
    del page
    gc.collect()
    qapp.processEvents()

    # 单例仍然健康；emit 不得触达已删除页面的槽（悬空连接 = SIGSEGV）
    assert not sip.isdeleted(singleton)
    singleton.status_changed.emit("回归探测", 0)
    qapp.processEvents()
    assert page_id  # 仅保持 page_id 引用语义完整


def test_waveform_singleton_survives_emit_without_receivers(qapp) -> None:
    """无任何接收者时单例 emit 必须安全（基线健康检查）。"""
    from src.features.edit.core.audio_waveform import WaveformExtractor

    singleton = WaveformExtractor.instance()
    seen: list[tuple[str, int]] = []

    def _receive(message: str, timeout_ms: int) -> None:
        seen.append((message, timeout_ms))

    singleton.status_changed.connect(_receive)
    singleton.status_changed.emit("直接槽", 0)
    assert seen[-1] == ("直接槽", 0)
    singleton.status_changed.disconnect(_receive)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_page_shutdown_releases_scrub_priority_and_resumes_queued_waveforms(qapp, monkeypatch, cleanup_fails: bool) -> None:
    """Closing/leaving mid-scrub must not permanently pause global decoding."""
    from types import SimpleNamespace
    from src.features.edit.core.audio_waveform import ClipEffectWaveformRenderer, WaveformCache, WaveformExtractor
    from src.features.edit.ui.edit_page import EditPage

    extractor = WaveformExtractor()
    effects = ClipEffectWaveformRenderer()
    monkeypatch.setattr(WaveformExtractor, "_instance", extractor)
    monkeypatch.setattr(ClipEffectWaveformRenderer, "_instance", effects)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(extractor, "_start_worker", lambda asset, path: started.append((asset, path)))
    page = EditPage()
    page._scrub_active = True
    page._pending_scrub_request_frame = 20
    page._scrub_request_timer_pending = True
    token = page._background_media_resume_token
    page._set_thumbnail_playback(True)
    extractor.request_waveform("shutdown-queued", "/queued.wav")
    assert extractor._playback_active and effects._playback_active
    assert not started

    if cleanup_fails:
        def fail_cleanup() -> None:
            raise RuntimeError("device disconnected during cleanup")
        page._audio_manager = SimpleNamespace(cleanup=fail_cleanup)
    # The no-manager case is intentional: waveform priority is also set by
    # video-only scrubbing before a device/audio manager exists.
    page.shutdown_audio_playback()
    assert not page._scrub_active
    assert page._pending_scrub_request_frame is None
    assert not page._scrub_request_timer_pending
    assert page._background_media_resume_token > token
    assert not extractor._playback_active and not effects._playback_active
    assert started == [("shutdown-queued", "/queued.wav")]
    assert not extractor._queued_requests
    WaveformCache().clear_pending("shutdown-queued")
    page.deleteLater()


def test_terminal_page_shutdown_cancels_queue_without_starting_decoders(qapp, monkeypatch) -> None:
    from unittest.mock import Mock
    from src.features.edit.core.audio_waveform import ClipEffectWaveformRenderer, WaveformCache, WaveformExtractor
    from src.features.edit.ui.edit_page import EditPage

    extractor = WaveformExtractor()
    effects = ClipEffectWaveformRenderer()
    monkeypatch.setattr(WaveformExtractor, "_instance", extractor)
    monkeypatch.setattr(ClipEffectWaveformRenderer, "_instance", effects)
    start_source = Mock()
    monkeypatch.setattr(extractor, "_start_worker", start_source)
    extractor.set_playback_active(True)
    effects.set_playback_active(True)
    extractor.request_waveform("terminal-queued", "/queued.wav")
    effects.request_effect_waveform("fx-queued", "/queued.wav", 0.0, 1.0, [])
    page = EditPage()
    page.shutdown_audio_playback(resume_background=False)
    assert extractor._shutdown and effects._shutdown
    assert not extractor._playback_active and not effects._playback_active
    assert not extractor._queued_requests and not effects._queued
    assert not WaveformCache().is_pending("terminal-queued")
    start_source.assert_not_called()
    # Late requests to these closed owners cannot restart native workers.
    extractor.request_waveform("late-queued", "/late.wav")
    effects.request_effect_waveform("late-fx", "/late.wav", 0.0, 1.0, [])
    assert not extractor._queued_requests and not effects._queued
    start_source.assert_not_called()
    page.deleteLater()


def test_clip_effect_shutdown_retains_a_worker_that_misses_the_join_budget(qapp) -> None:
    from unittest.mock import Mock
    from src.features.edit.core.audio_waveform import (
        ClipEffectWaveformRenderer, WAVEFORM_CANCEL_WAIT_MS, _retired_waveform_owners,
    )

    renderer = ClipEffectWaveformRenderer()
    worker = Mock()
    worker.isRunning.return_value = True
    worker.wait.return_value = False
    renderer._worker = worker
    renderer.shutdown()
    worker.requestInterruption.assert_called_once()
    worker.wait.assert_called_once_with(WAVEFORM_CANCEL_WAIT_MS)
    assert renderer._worker is None
    assert renderer._detached_workers[id(worker)] is worker
    assert renderer in _retired_waveform_owners
    worker.deleteLater.assert_not_called()
    renderer._on_worker_finished("finished-fx", worker)
    assert not renderer._detached_workers
    assert renderer not in _retired_waveform_owners
    worker.deleteLater.assert_called_once()
