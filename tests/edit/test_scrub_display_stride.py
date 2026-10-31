"""Adaptive scrub display stride (1x/2x/4x/8x/16x frame sampling).

快速拖拽时解码管线追逐步长量化后的目标帧：解出的帧几乎全部可上屏，
而不是解码完成后因播放头已离开而被 stale 丢弃。慢拖步长为 1 逐帧；
松手/停顿修复始终精确落在指针下方的真实帧。全分辨率解码不受影响。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.video.scrub_stride import (
    DEFAULT_DISPLAY_RATE_CAP,
    display_stride,
    quantize_frame,
    update_velocity,
)


# ── 步长数学 ────────────────────────────────────────────────────────────

def test_stride_stays_one_for_slow_drags() -> None:
    assert display_stride(0.0, 48.0) == 1
    assert display_stride(30.0, 48.0) == 1
    # 1.25x hysteresis: up to cap*1.25 the stride stays 1.
    assert display_stride(48.0, 48.0) == 1
    assert display_stride(60.0, 48.0) == 1


def test_stride_grows_in_power_of_two_steps() -> None:
    assert display_stride(80.0, 48.0) == 2    # 80/2 = 40 <= 48
    assert display_stride(200.0, 48.0) == 8   # 200/4 = 50 > 48 → 200/8 = 25 <= 48
    assert display_stride(1000.0, 48.0) == 16  # clamped at 16
    # 慢解码管线（DNxHR 级）同样速度下自动取更大步长
    assert display_stride(100.0, 10.0) == 16   # 100/16 = 6.25 <= 10


def test_quantize_uses_floor_grid_never_ahead_of_playhead() -> None:
    assert quantize_frame(53, 8) == 48
    assert quantize_frame(56, 8) == 56
    assert quantize_frame(7, 8) == 0
    assert quantize_frame(-3, 4) == -4
    assert quantize_frame(99, 1) == 99


def test_velocity_ema_smooths_and_tracks_direction_independently() -> None:
    v = update_velocity(0.0, 5, 0.1)
    assert v == pytest.approx(50.0)
    v = update_velocity(v, -5, 0.1)
    assert v == pytest.approx(50.0)  # |Δ|/dt again 50
    v = update_velocity(v, 0, 0.1)
    assert v == pytest.approx(50.0 * 0.65)  # decays toward 0


# ── 渲染器接入 ─────────────────────────────────────────────────────────

@pytest.fixture
def renderer():
    """Real TimelinePreviewRenderer with a recording worker (no GL needed)."""
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.edit.core.clip_params import TransformParams, CropParams
    from src.features.edit.core.project import EditProject

    project = EditProject.create("stride-test")
    project = project.import_asset(
        path="/tmp/stride.mov",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0,
                  "total_frames": 300, "duration_seconds": 12.5},
    )
    asset = project.video_assets[0]
    clip = ClipInstance(
        clip_id="clip-1", asset_id=asset.asset_id, track_id="track-1",
        timeline_start=0, timeline_duration=300, source_start=0, source_duration=300,
        speed=1.0, transform=TransformParams(), crop=CropParams(),
    )
    track = EditTrack(track_id="track-1", name="V1", track_type="video", index=0, clips=(clip,))
    sequence = TimelineSequence(name="stride", fps=24.0, video_tracks=(track,))

    provider = Mock()
    provider.open_clip.return_value = "clip-test-001"
    frame = Mock()
    frame.data = Mock(); frame.data.shape = (1080, 1920, 3); frame.data.dtype = "uint8"
    frame.pixel_format = "bgr24"
    provider.request_frame.return_value = frame
    provider.close_clip.return_value = True

    renderer = TimelinePreviewRenderer(
        project=project, sequence=sequence, frame_provider=provider,
        viewport_size=(1920, 1080),
    )
    enqueued = []

    def _enqueue(frame, *args, **kwargs):
        enqueued.append(int(frame))
        return True

    renderer._worker.enqueue = _enqueue
    renderer._enqueued = enqueued
    # Fast-drag state as if the pointer were moving quickly.
    renderer._scrub_input_fast = True
    yield renderer
    renderer.shutdown()


def _drive(renderer, frames, velocity=None, cycle_s=None, fast=True):
    renderer.begin_scrub(frames[0])
    renderer._scrub_input_fast = fast
    if velocity is not None:
        renderer._scrub_input_velocity_fps = velocity
        renderer._scrub_velocity_samples = 3  # sustained drag, ramped past the gate
    if cycle_s is not None:
        renderer._scrub_cycle_period_ema_s = cycle_s
    for f in frames:
        display = renderer._quantize_scrub_display_frame(f)
        renderer._scrub_latest_frame = f
        renderer._scrub_latest_display_frame = display
        # 直接派发（跳过 QTimer 合并，单元测试不等事件循环）
        renderer._scrub_exact_timer_pending = False
        renderer._render_in_flight = False
        renderer._scrub_exact_in_flight_frame = None
        renderer._dispatch_scrub_exact_render(display)


def test_fast_drag_dispatches_quantized_frames(renderer) -> None:
    # 400 frames/sec 拖拽，解码周期 EMA 25ms（可持续 40fps）→ 步长 16
    _drive(renderer, [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
           velocity=400.0, cycle_s=0.025)

    assert renderer._enqueued, "no frames dispatched"
    # 所有派发帧都落在 16 帧网格上，且不超前于播放头
    assert all(f % 16 == 0 for f in renderer._enqueued)
    assert set(renderer._enqueued) == {96}


def test_slow_drag_dispatches_every_frame(renderer) -> None:
    renderer._scrub_input_fast = False
    _drive(renderer, [10, 11, 12, 13])
    assert renderer._enqueued == [10, 11, 12, 13]


def test_finish_scrub_lands_exact_frame(renderer) -> None:
    _drive(renderer, [100, 105], velocity=400.0, cycle_s=0.025)
    renderer.finish_scrub(107)
    # 松手后 worker 最终收到精确帧 107（非网格帧）
    assert 107 in renderer._enqueued
    assert renderer._scrub_latest_display_frame == 107
    assert renderer._scrub_input_velocity_fps == 0.0


def test_stride_adapts_to_slow_codecs(renderer) -> None:
    # DNxHR 级解码周期 ~110ms（可持续 ~9fps）→ 中速拖拽也取大步长
    renderer._scrub_input_velocity_fps = 100.0
    renderer._scrub_velocity_samples = 3
    renderer._scrub_cycle_period_ema_s = 0.11
    assert renderer._scrub_display_stride() == 16


def test_single_fast_input_does_not_stride(renderer) -> None:
    """一次快速输入（单击 seek 式跳转）不应触发隔行取帧。"""
    renderer._scrub_input_fast = True
    renderer._scrub_input_velocity_fps = 1200.0
    renderer._scrub_velocity_samples = 1
    assert renderer._scrub_display_stride() == 1


# ── 看门狗自愈 ─────────────────────────────────────────────────────────

def test_lost_exact_timer_callback_recovers_via_watchdog(renderer) -> None:
    """singleShot 回调丢失后，闩锁必须在看门狗窗口内自愈。

    真实 App 复现：exact 派发一次性回调永不执行 → _scrub_exact_timer_pending
    永久卡 True → 之后所有输入静默 return，拖拽画面完全不动。
    """
    import time as _time

    renderer.begin_scrub(10)
    renderer._scrub_input_fast = False
    # 模拟回调丢失：标志卡 True，且调度时间戳已超过看门狗窗口。
    renderer._scrub_exact_timer_pending = True
    renderer._scrub_exact_scheduled_at = _time.monotonic() - 1.0
    renderer._scrub_exact_pending_frame = 42

    # 直接触发调度路径（绕过 QTimer）：应看门狗复位并重新调度。
    from unittest.mock import patch
    with patch.object(renderer, "_submit_scrub_exact_render"):
        # patch 掉 submit 以避免真实 QTimer 依赖；schedule 内部会安排 singleShot，
        # 但我们只验证闩锁被复位。
        renderer._scrub_exact_timer_pending = True
        renderer._scrub_exact_scheduled_at = _time.monotonic() - 1.0
        renderer._schedule_scrub_exact_render(42)
        assert renderer._scrub_exact_timer_pending is True  # 重新调度置位
        assert renderer._scrub_exact_scheduled_at > 0
        # 新的调度时间戳已更新（不再是过去）
        assert renderer._scrub_exact_scheduled_at >= _time.monotonic() - 0.5


def test_lost_release_recovers_via_watchdog(renderer) -> None:
    """单飞释放链丢失后，in_flight 闩锁必须在看门狗窗口内自愈。"""
    import time as _time

    renderer.begin_scrub(10)
    renderer._scrub_input_fast = False
    renderer._render_in_flight = True
    renderer._scrub_last_dispatch_at = _time.monotonic() - 5.0  # 5s 前派发，无释放

    from unittest.mock import patch
    with patch("src.features.edit.core.timeline_preview_renderer.QTimer") as mock_timer:
        mock_timer.singleShot = lambda *a, **k: None
        renderer._schedule_scrub_exact_render(50)
        assert renderer._render_in_flight is False, "watchdog should have reset in_flight"


class TestScrubFastCadenceDetection:
    """快拖判定：间隔死区（24-40ms）内的大步幅输入也必须识别为快拖。

    真实 App 复现（调色页往返后编辑页快拖冻结）：主线程拥堵把输入间隔
    拉到 24-40ms 死区，旧判定永不开启快模式 → 步长失效 + 每请求派发
    预置位解码与显示解码争锁（870 请求 680 次解码失败，~4ups）。
    """

    def test_large_delta_engages_fast_despite_slow_interval(self, renderer) -> None:
        renderer._scrub_input_fast = False
        renderer._scrub_last_input_at = None
        # 第一拍建立 last；第二拍间隔 30ms（死区）但跨 20 帧 → 快拖
        renderer._update_scrub_input_cadence(100, 100)
        import time as _t
        renderer._scrub_last_input_at = _t.perf_counter() - 0.030
        renderer._update_scrub_input_cadence(120, 100)
        assert renderer._scrub_input_fast is True

    def test_small_interval_engages_fast(self, renderer) -> None:
        renderer._scrub_input_fast = False
        renderer._update_scrub_input_cadence(10, 10)
        import time as _t
        renderer._scrub_last_input_at = _t.perf_counter() - 0.010
        renderer._update_scrub_input_cadence(12, 10)
        assert renderer._scrub_input_fast is True

    def test_exit_requires_both_slow_interval_and_small_delta(self, renderer) -> None:
        renderer._scrub_input_fast = True
        renderer._update_scrub_input_cadence(500, 500)
        import time as _t
        # 间隔 >40ms 但步幅大（快拖中）：保持快模式
        renderer._scrub_last_input_at = _t.perf_counter() - 0.050
        renderer._update_scrub_input_cadence(560, 500)
        assert renderer._scrub_input_fast is True
        # 间隔 >40ms 且步幅 ≤2：退出快模式
        renderer._scrub_last_input_at = _t.perf_counter() - 0.050
        renderer._update_scrub_input_cadence(562, 560)
        assert renderer._scrub_input_fast is False

    def test_dead_zone_small_delta_keeps_previous_state(self, renderer) -> None:
        # 间隔 30ms + 步幅 2：迟滞区，不主动开启
        renderer._scrub_input_fast = False
        renderer._update_scrub_input_cadence(10, 10)
        import time as _t
        renderer._scrub_last_input_at = _t.perf_counter() - 0.030
        renderer._update_scrub_input_cadence(12, 10)
        assert renderer._scrub_input_fast is False
