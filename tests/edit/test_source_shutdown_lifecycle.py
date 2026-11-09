"""VP-B11：Source 关闭/取消/超时/fallback 生命周期（widget 侧）。

缺口（计划 VP-B11-1/2）：主窗 closeEvent 只停 edit_page 音频、decode
worker 与 GPU renderer——**不**调 Source 的任何关闭入口：播放 timer、
watchdog、`_gpu_displayed_slot_id`（永不 ack）、`_scrub_audio_manager`
（parent 为 widget 的 QThread，随窗口析构有 ``QThread::~QThread`` abort
风险）全部遗留；也没有"关闭后拒绝新请求"的 token。

契约：

- ``prepare_for_shutdown()`` 幂等，一次完成：停播放/周期 tick/watchdog、
  关闭 token 置位（此后 play/tick 解码提交、GPU 超时回退、CPU 回退
  请求、CPU 回包一律拒绝——零新请求）、关闭 CPU 解码器、清缓存、
  撤销会话 generation（在飞结果作废）、归还显示中的 present slot、
  清理 scrub 音频线程；
- 每项副作用恰好一次（重复调用 no-op）；
- closeEvent/换工程必须**先**调它再停 decode worker（file_controller
  接线；本文件测 widget 侧语义）。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from PyQt6.QtGui import QImage

from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.source_viewer import SourceViewerWidget

pytestmark = pytest.mark.edit_ui


class _Provider:
    def __init__(self) -> None:
        self.requests: list[int] = []

    def __call__(self, path: str, frame: int) -> dict:
        self.requests.append(int(frame))
        return {"submitted": True}


def _video_asset() -> MediaAsset:
    return MediaAsset(
        asset_id="asset-b11",
        path="/tmp/b11_source.mov",
        media_type="video",
        display_name="b11_source.mov",
        metadata={"fps": 24.0, "total_frames": 240, "width": 640,
                  "height": 480},
    )


def _widget(qapp: Any, provider: _Provider) -> SourceViewerWidget:
    widget = SourceViewerWidget()
    widget.set_gpu_frame_provider(provider)
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    widget._start_source_audio = lambda frame: None  # type: ignore[method-assign]
    widget._pause_source_audio = lambda: None  # type: ignore[method-assign]
    widget.open_asset(_video_asset())
    return widget


def test_b11_shutdown_releases_everything_exactly_once(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    acked: list[int] = []
    widget.set_gpu_slot_ack_callback(acked.append)
    close_decoder_calls: list[int] = []
    widget._cpu_worker.close_decoder = (  # type: ignore[method-assign]
        lambda: close_decoder_calls.append(1))
    audio_cleanups: list[int] = []
    widget._cleanup_scrub_audio_manager = (  # type: ignore[method-assign]
        lambda: audio_cleanups.append(1))
    # 起播 + 显示 slot 占用 + watchdog 在途
    widget._playback_start_time = time.perf_counter()
    widget._playback_start_frame = 0
    widget.play()
    gen = widget._gpu_generation
    widget._gpu_expected_frame = 10
    assert widget.set_gpu_frame(7, 320, 180, frame=10, generation=gen,
                                slot_id=42)
    widget._gpu_pending_frame = 10
    widget._gpu_watchdog_timer.start(100)
    assert widget._gpu_displayed_slot_id == 42

    widget.prepare_for_shutdown()
    widget.prepare_for_shutdown()  # 幂等

    assert acked == [42]                      # 显示 slot 恰好归还一次
    assert close_decoder_calls == [1]         # CPU 解码器关闭一次
    assert audio_cleanups == [1]              # scrub 音频线程清理一次
    assert widget.is_playing is False
    assert not widget._playback_timer.isActive()
    assert not widget._gpu_watchdog_timer.isActive()
    assert widget._gpu_pending_frame == -1
    assert widget._gpu_generation == gen + 1  # 会话 generation 已撤销
    assert widget._gpu_displayed_slot_id == -1


def test_b11_after_shutdown_no_new_requests(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    cpu_submits: list[int] = []
    widget._submit_cpu_fallback = (  # type: ignore[method-assign]
        lambda frame: cpu_submits.append(frame))
    widget.prepare_for_shutdown()
    provider.requests.clear()

    widget.play()                              # 关闭后不起播
    assert widget.is_playing is False
    assert not widget._playback_timer.isActive()

    widget._current_frame = 5
    widget._playback_start_time = time.perf_counter() - 0.5
    widget._playback_start_frame = 0
    widget._is_playing = True
    widget._gpu_pending_frame = -1
    widget._on_playback_tick()                 # tick 不再提交解码
    assert provider.requests == []

    widget._gpu_pending_frame = 5
    widget._on_gpu_timeout()                   # 超时不再拉起 CPU 回退
    assert cpu_submits == []

    widget.request_cpu_fallback(5)             # 回退请求拒绝
    assert cpu_submits == []


def test_b11_cpu_result_after_shutdown_dropped(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    submitted: list[tuple] = []
    widget.viewer.submit_qimage = (  # type: ignore[method-assign]
        lambda *args, **kwargs: submitted.append(args))
    gen = widget._gpu_generation
    widget._gpu_expected_frame = 8
    widget.prepare_for_shutdown()
    widget._gpu_generation = gen  # 抵消 generation 撤销，单测关闭 token 本身

    image = QImage(4, 4, QImage.Format.Format_RGB888)
    widget._on_cpu_frame_ready(8, image, 1.0, generation=gen)

    assert submitted == []                     # 关闭 token：结果不上屏
    assert widget._last_good_frame == -1


# ── I05：工程关闭/切换的可复用释放边界 ─────────────────────────────


def test_i05_release_for_project_close_is_reusable_not_permanent(qapp):
    """释放后 widget 可复用：新素材照常打开/播放；永久 token 不置位。"""
    widget = _widget(qapp, _Provider())
    widget.open_asset(_video_asset())
    widget.toggle_playback()
    assert widget.is_playing is True
    acked: list[int] = []
    widget.set_gpu_slot_ack_callback(acked.append)
    widget._gpu_displayed_slot_id = 5

    states: list[bool] = []
    widget.playback_state_changed.connect(states.append)
    widget.release_for_project_close()

    assert widget.is_playing is False
    assert states[-1:] == [False]          # 播放状态信号如实翻转
    assert acked == [5]                    # 显示中的 slot 已归还
    assert widget.current_asset is None
    assert widget._in_point is None and widget._out_point is None
    assert widget._shutdown_prepared is False  # 非永久 token

    # 新工程复用：再次打开并播放正常
    widget.open_asset(_video_asset())
    assert widget.toggle_playback() is True
    assert widget.is_playing is True


def test_i05_release_clears_monitor_state_store(qapp):
    """旧工程的监看状态不带入新工程（同一素材重开从第 0 帧起）。"""
    widget = _widget(qapp, _Provider())
    asset = _video_asset()
    widget.open_asset(asset)
    widget.seek(77)
    assert widget._current_frame == 77

    widget.release_for_project_close()
    widget.open_asset(asset)

    assert widget._current_frame == 0     # 存储已清，不回填旧工程位置


def test_i05_close_source_keeps_monitor_state_within_project(qapp):
    """对照：工程内换源（close_source）保留监看状态——只有工程级释放才清。"""
    widget = _widget(qapp, _Provider())
    widget.open_asset(_video_asset())
    widget.seek(33)

    widget.close_source()                 # 换源路径（I02 语义）
    widget.open_asset(_video_asset())

    assert widget._current_frame == 33
