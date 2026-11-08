"""VP-B05：Transport 适配——Stop 原位语义、Source Loop/Reverse、scrub 恢复。

缺口（计划 VP-B05）：

- Timeline 与 Source 的 ``stop()`` 都会归零（``EditPlaybackController.stop``
  seek(0)；``SourceViewerWidget.stop`` = pause+seek(0)）——Viewer 的 Stop
  要求"停止于当前位置"，回起点应由 Home 单独负责；
- Source 播放到素材末尾只会 pause，无素材/InOut 循环与边界音频重定位；
- Source 无反向播放；音频反向不支持时必须 capability 明示，不得假装
  正常反向音频；
- 拖动 scrub 开始时会暂停播放，结束后不按开始前状态恢复。

契约：

- Timeline ``stop_in_place()`` = pause + JKL 档位复位，播放头保留；
  ``stop()`` 的归零语义不变（legacy）；
- Source ``stop_in_place()`` 原位暂停；``set_loop(True)`` 后播放到
  Out（或末帧）回卷 In（或 0）并重定位音频；一帧循环范围不死循环；
- Source ``play_reverse()`` 倒放视频（1x，初版限速），不启动音频，
  ``source_audio_reverse_supported`` 如实为 False；倒放到头暂停；
- slider 按下时在播 → 释放后恢复播放；按下时已暂停 → 释放后仍暂停。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from src.features.edit.core.edit_playback_controller import (
    EditPlaybackController,
)
from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.source_viewer import SourceViewerWidget

pytestmark = pytest.mark.edit_ui


class _Provider:
    def __init__(self) -> None:
        self.requests: list[int] = []

    def __call__(self, path: str, frame: int) -> dict:
        self.requests.append(int(frame))
        return {"submitted": True}


def _video_asset(total: int = 240) -> MediaAsset:
    return MediaAsset(
        asset_id="asset-b05",
        path="/tmp/b05_source.mov",
        media_type="video",
        display_name="b05_source.mov",
        metadata={"fps": 24.0, "total_frames": total, "width": 640,
                  "height": 480},
    )


def _widget(qapp: Any, provider: _Provider | None = None,
            total: int = 240) -> tuple[SourceViewerWidget, list, list]:
    audio_starts: list[int] = []
    audio_seeks: list[int] = []
    widget = SourceViewerWidget()
    if provider is not None:
        widget.set_gpu_frame_provider(provider)
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    widget._start_source_audio = audio_starts.append  # type: ignore[method-assign]
    widget._seek_source_audio = audio_seeks.append  # type: ignore[method-assign]
    widget._pause_source_audio = lambda: None  # type: ignore[method-assign]
    widget._request_scrub_audio_preview = (  # type: ignore[method-assign]
        lambda *args, **kwargs: None)
    widget.open_asset(_video_asset(total))
    return widget, audio_starts, audio_seeks


def _anchor(widget: SourceViewerWidget, frame: int, elapsed: float) -> None:
    widget._playback_start_frame = frame
    widget._playback_start_time = time.perf_counter() - elapsed
    widget._gpu_pending_frame = -1


# ── Timeline：Stop 原位语义 ────────────────────────────────────────


def test_b05_timeline_stop_in_place_keeps_frame_and_resets_jkl(qapp):
    controller = EditPlaybackController()
    controller.set_fps(24.0)
    controller.set_timeline_duration(100)
    controller.seek(42)
    controller.play()
    assert controller.is_playing
    controller.jkl_forward_press()
    controller.jkl_forward_press()
    assert controller.get_jkl_speed_index() == 2

    controller.stop_in_place()

    assert controller.current_frame == 42        # 播放头保留
    assert not controller.is_playing
    assert controller.get_jkl_speed_index() == 0  # JKL 档位复位


def test_b05_timeline_legacy_stop_still_rewinds(qapp):
    """legacy stop() 的归零语义不变（回起点由 Home/stop 显式负责）。"""
    controller = EditPlaybackController()
    controller.set_fps(24.0)
    controller.set_timeline_duration(100)
    controller.seek(42)

    controller.stop()

    assert controller.current_frame == 0


# ── Source：Stop 原位语义 ──────────────────────────────────────────


def test_b05_source_stop_in_place_keeps_frame(qapp):
    widget, _, _ = _widget(qapp)
    widget.seek(33)
    widget._is_playing = True

    widget.stop_in_place()

    assert widget.current_frame == 33
    assert widget.is_playing is False


def test_b05_source_legacy_stop_still_rewinds(qapp):
    widget, _, _ = _widget(qapp)
    widget.seek(33)

    widget.stop()

    assert widget.current_frame == 0


# ── Source：Loop ───────────────────────────────────────────────────


def test_b05_source_loop_wraps_at_out_with_audio_relocation(qapp):
    provider = _Provider()
    widget, starts, seeks = _widget(qapp, provider)
    widget.set_in_point(20)
    widget.set_out_point(30)
    widget.set_loop(True)
    widget._current_frame = 29
    widget._is_playing = True
    _anchor(widget, 10, 1.0)  # elapsed 1s → target 34 ≥ Out 30 → 回卷
    provider.requests.clear()

    widget._on_playback_tick()

    assert widget._current_frame == 20            # 回卷到 In
    assert 20 in seeks                            # 边界音频重定位
    assert provider.requests == [20]              # 回卷帧照常提交
    assert widget.is_playing                      # 循环继续


def test_b05_source_loop_without_in_out_wraps_to_zero(qapp):
    provider = _Provider()
    widget, _, seeks = _widget(qapp, provider)
    widget.set_loop(True)
    widget._current_frame = 239
    widget._is_playing = True
    _anchor(widget, 200, 1.0)  # target 224… hmm 需要 ≥ 末帧
    widget._playback_start_frame = 235
    widget._playback_start_time = time.perf_counter() - 1.0  # target 259

    widget._on_playback_tick()

    assert widget._current_frame == 0
    assert 0 in seeks


def test_b05_source_loop_disabled_pauses_at_end(qapp):
    widget, _, _ = _widget(qapp)
    widget._current_frame = 239
    widget._is_playing = True
    _anchor(widget, 235, 1.0)

    widget._on_playback_tick()

    assert widget.is_playing is False             # 无循环：末尾暂停（不变）


def test_b05_source_one_frame_loop_range_terminates_each_tick(qapp):
    provider = _Provider()
    widget, _, seeks = _widget(qapp, provider)
    widget.set_in_point(5)
    widget.set_out_point(5)
    widget.set_loop(True)
    widget._is_playing = True
    provider.requests.clear()

    for _ in range(3):  # 每个 tick 恰好回卷一次，绝不死循环
        widget._current_frame = 5
        _anchor(widget, 5, 0.0)
        widget._on_playback_tick()

    assert widget._current_frame == 5
    assert len(seeks) == 3                        # 每 tick 一次（有界）


# ── Source：Reverse（capability 如实） ─────────────────────────────


def test_b05_source_reverse_video_only_and_audio_capability_honest(qapp):
    provider = _Provider()
    widget, starts, _ = _widget(qapp, provider)
    assert widget.source_audio_reverse_supported is False
    widget._current_frame = 10
    provider.requests.clear()

    widget.play_reverse()

    assert widget.is_playing
    assert starts == []                           # 不启动音频（capability 明示）
    # 倒放步进：elapsed 0.125s@24fps → target 10-3=7
    _anchor(widget, 10, 0.125)
    widget._on_playback_tick()
    assert widget._current_frame == 7
    assert 7 in provider.requests


def test_b05_source_reverse_pauses_at_head(qapp):
    provider = _Provider()
    widget, _, _ = _widget(qapp, provider)
    widget._current_frame = 3
    widget.play_reverse()

    _anchor(widget, 5, 1.0)  # target 5-24 <0 → 到头

    widget._on_playback_tick()

    assert widget._current_frame == 0
    assert widget.is_playing is False             # 倒放到头暂停


def test_b05_source_forward_play_clears_reverse_mode(qapp):
    provider = _Provider()
    widget, _, _ = _widget(qapp, provider)
    widget._current_frame = 10
    widget.play_reverse()
    widget.pause()

    widget.play()                                 # 正向恢复

    _anchor(widget, 10, 0.125)
    widget._on_playback_tick()
    assert widget._current_frame == 13            # 正向步进


# ── scrub 结束后的播放恢复 ─────────────────────────────────────────


def test_b05_source_scrub_release_resumes_if_was_playing(qapp):
    widget, _, _ = _widget(qapp)
    widget._current_frame = 10
    widget.play()
    assert widget.is_playing

    widget._on_slider_pressed()
    assert widget.is_playing is False             # 拖动开始暂停
    widget.slider.setValue(15)
    widget._on_slider_released()

    assert widget.is_playing is True              # 按开始前状态恢复


def test_b05_source_scrub_release_stays_paused_if_was_paused(qapp):
    widget, _, _ = _widget(qapp)
    widget.pause()

    widget._on_slider_pressed()
    widget.slider.setValue(15)
    widget._on_slider_released()

    assert widget.is_playing is False
    widget.pause()
