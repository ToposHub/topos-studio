"""VP-B02：隐藏视图调度——可见性 revision 与媒体 generation 分离。

缺口（计划 VP-B02 第 1/2/5 条）：SourceViewerWidget 没有
show/hide 处理——单视图切走后（EditViewport 隐藏 source_pane），
播放 tick 照常向 decode worker 提交请求、watchdog 照常可能拉起 CPU
回退；也没有 submit/discard/ack 计数可诊断饥饿与泄漏。

契约：

- 隐藏：停止周期性解码提交（播放时钟与播放头继续走，音频不动），
  停 watchdog（隐藏不被拉起新解码）；**不**推进媒体 generation；
- 再次可见：恰好一次当前帧刷新请求；
- 可见性 revision（``view_visibility_revision``）独立于
  ``gpu_generation``——反复 show/hide 不会把当前素材判为过期；
- 200 次模式切换：媒体 generation 不变，slot 收支平衡（ack 数 ==
  slot 数），无持续增长的占用池；
- widget 销毁路径归还仍在显示的 slot（destroyed 回调不触碰 widget）。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.source_viewer import (
    SourceViewerWidget,
    _release_displayed_slot_on_destroy,
)

pytestmark = pytest.mark.edit_ui


class _Provider:
    """假 GPU provider：只记录请求，全部应答 submitted。"""

    def __init__(self) -> None:
        self.requests: list[int] = []

    def __call__(self, path: str, frame: int) -> dict:
        self.requests.append(int(frame))
        return {"submitted": True}


def _video_asset() -> MediaAsset:
    return MediaAsset(
        asset_id="asset-b02",
        path="/tmp/b02_source.mov",
        media_type="video",
        display_name="b02_source.mov",
        metadata={"fps": 24.0, "total_frames": 240, "width": 640,
                  "height": 480},
    )


def _widget(qapp: Any, provider: _Provider) -> SourceViewerWidget:
    widget = SourceViewerWidget()
    widget.show()  # hideEvent 只在 可见→隐藏 转换时发出，先进入可见态
    widget.set_gpu_frame_provider(provider)
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    widget._start_source_audio = lambda frame: None  # type: ignore[method-assign]
    widget._pause_source_audio = lambda: None  # type: ignore[method-assign]
    widget.open_asset(_video_asset())
    return widget


# ── 隐藏策略 ────────────────────────────────────────────────────────


def test_b02_hidden_tick_skips_decode_keeps_clock(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    provider.requests.clear()
    widget._current_frame = 0
    widget._playback_start_time = time.perf_counter() - 0.5  # → target ≈ 12
    widget._playback_start_frame = 0
    widget._is_playing = True
    widget._gpu_pending_frame = -1  # 无在途解码，避免 tick 走背压分支

    widget.hide()
    widget._on_playback_tick()

    assert provider.requests == []          # 隐藏态零周期解码提交
    assert widget._current_frame == 12      # 时钟照走、播放头照算
    assert widget.slider.value() == 12

    widget.show()
    assert provider.requests == [12]        # 恢复可见恰好一次刷新
    widget.pause()


def test_b02_visible_tick_still_submits(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    provider.requests.clear()
    widget._current_frame = 0
    widget._playback_start_time = time.perf_counter() - 0.5
    widget._playback_start_frame = 0
    widget._is_playing = True
    widget._gpu_pending_frame = -1

    widget._on_playback_tick()              # 可见态不受新策略影响

    assert provider.requests == [12]
    widget.pause()


def test_b02_hide_stops_watchdog(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    widget._gpu_pending_frame = 5
    widget._gpu_watchdog_timer.start(100)
    assert widget._gpu_watchdog_timer.isActive()

    widget.hide()

    assert not widget._gpu_watchdog_timer.isActive()
    widget.show()
    widget.pause()


def test_b02_visibility_revision_separate_from_media_generation(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    generation_after_open = widget._gpu_generation
    revision0 = widget.view_visibility_revision

    for _ in range(200):
        widget.hide()
        widget.show()

    assert widget._gpu_generation == generation_after_open  # 媒体代数不动
    assert widget.view_visibility_revision >= revision0 + 400
    widget.pause()


def test_b02_show_requests_current_frame_once(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    widget.seek(33)
    provider.requests.clear()

    widget.hide()
    assert provider.requests == []
    widget.show()

    assert provider.requests == [33]  # 恰好一次当前帧
    widget.pause()


def test_b02_hidden_pending_decode_does_not_hold_logic_clock(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    widget._current_frame = 0
    widget._last_good_frame = 0
    widget._gpu_pending_frame = 0
    widget._playback_start_time = time.perf_counter() - 0.5
    widget._playback_start_frame = 0
    widget._is_playing = True
    provider.requests.clear()
    widget.hide()
    widget._on_playback_tick()
    assert widget.current_frame == 12
    assert provider.requests == []
    widget.show()
    assert provider.requests == [12]
    widget.pause()


def test_b02_hidden_late_failure_never_submits_cpu_decode(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    submitted: list[tuple] = []
    widget._cpu_worker.request = lambda *args, **kwargs: submitted.append((args, kwargs))
    widget.seek(20)
    widget._gpu_expected_frame = 20
    widget._gpu_pending_frame = 20
    provider.requests.clear()
    widget.hide()
    widget.request_cpu_fallback(20)
    widget._on_gpu_timeout()
    assert submitted == []
    assert provider.requests == []
    widget.show()
    assert provider.requests == [20]
    widget.pause()


# ── slot 收支闭环 ───────────────────────────────────────────────────


def test_b02_slot_accounting_balanced_over_200_cycles(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    acked: list[int] = []
    widget.set_gpu_slot_ack_callback(acked.append)
    gen = widget._gpu_generation

    slots = 0
    for i in range(200):
        widget.hide()
        widget._gpu_expected_frame = i
        accepted = widget.set_gpu_frame(
            1000 + i, 320, 180, frame=i, generation=gen, slot_id=i)
        assert accepted
        slots += 1
        widget.show()

    # 200 次接受 → 199 个被替换 slot 已归还；显示中的最后一个由 close 归还
    widget.close_source()
    assert len(acked) == slots
    assert sorted(acked) == list(range(slots))   # 每个 slot 恰好归还一次
    assert widget._gpu_displayed_slot_id == -1


def test_b02_stats_counters_track_lifecycle(qapp):
    provider = _Provider()
    widget = _widget(qapp, provider)
    acked: list[int] = []
    widget.set_gpu_slot_ack_callback(acked.append)
    gen = widget._gpu_generation
    provider.requests.clear()

    widget._gpu_expected_frame = 10
    widget.set_gpu_frame(1, 320, 180, frame=10, generation=gen, slot_id=0)
    widget.set_gpu_frame(2, 320, 180, frame=10, generation=gen + 1,
                         slot_id=1)  # 旧 generation → 丢弃并 ack 自己

    stats = widget.gpu_slot_stats
    assert stats["decode_submitted"] >= 1
    assert stats["display_accepted"] == 1
    assert stats["results_discarded"] == 1
    assert stats["slots_acked"] == 1  # 丢弃帧自身的 slot
    widget.close_source()


def test_b02_destroyed_releases_displayed_slot_without_touching_widget():
    acked: list[int] = []
    state = {"slot_id": 41, "ack": acked.append}

    _release_displayed_slot_on_destroy(state)

    assert acked == [41]
    # 无效状态安全：无回调 / 无 slot 都不炸
    _release_displayed_slot_on_destroy({"slot_id": -1, "ack": acked.append})
    _release_displayed_slot_on_destroy({"slot_id": 5, "ack": None})
    _release_displayed_slot_on_destroy({"slot_id": 5,
                                        "ack": lambda s: 1 / 0})
    assert acked == [41]
