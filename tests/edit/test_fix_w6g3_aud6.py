"""w6-g3 回归测试：BUG-C3-01/02、BUG-C2-04、AUD-R2-005 修复后的正确行为。

对应的锁定用例翻转与根因修复记录见
``docs/plans/2026-09-28-multi-machine-program/fixes/reports/w6-g3.md``：

* BUG-C3-01：Composite 时间线传输条 13 个槽闭包在重进页面时先拆卸
  （含 enabled=False 的禁用路径），不再累积。
* BUG-C3-02：``comp_clip_total_frames()`` 返回 None/0 时回退
  ``controller.timeline_duration``，不再 TypeError 打断装配。
* BUG-C2-04：首帧 Level-3 收敛为 PLACEHOLDER 前把进入原因保留到
  ``FirstFrameResult.degraded_from``，中间状态可观测。
* AUD-R2-005：OpenBLAS 单线程环境隔离在位（tests/conftest.py 兜底）。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.app.test_page_controller_coverage import (
    _BAR_SIGNAL_NAMES,
    _RecordingPlaybackController,
    _timeline_controls_host,
)
from tests.video.test_video_playback_coverage import (
    _global_playback_engines,
    _global_playback_state_lock,
    _session_for_first_frame,
    make_coordinator,
    make_engine,
    make_packed_frame,
)

from src.shared.video.video_playback import (
    EnhancedVideoPlaybackEngine,
    FirstFrameStatus,
)


# =============================================================================
# BUG-C3-02：comp_clip_total_frames() 返回 None/0 的兜底
# =============================================================================


@pytest.mark.parametrize("returned", [None, 0])
def test_timeline_controls_invalid_total_frames_falls_back_to_timeline_duration(
        returned: Any) -> None:
    """callable 分支返回 None/0 时 clip_total 回退 controller.timeline_duration
    （BUG-C3-02：此前 ``int(None)`` 直接 TypeError，传输条不装配）。"""
    controller = _RecordingPlaybackController(timeline_duration=37)
    host, bar, page = _timeline_controls_host(controller)
    page.comp_clip_total_frames = lambda: returned

    host._configure_composite_timeline_playback_controls(True)

    assert bar.total_frames == 37


def test_timeline_controls_valid_total_frames_still_wins() -> None:
    """正常返回值不受兜底影响。"""
    controller = _RecordingPlaybackController(timeline_duration=37)
    host, bar, _page = _timeline_controls_host(controller)
    host.composite_page.comp_clip_total_frames.return_value = 48

    host._configure_composite_timeline_playback_controls(True)

    assert bar.total_frames == 48


# =============================================================================
# BUG-C3-01：传输条槽闭包的拆卸（禁用路径 + 重进路径）
# =============================================================================


def test_timeline_controls_disable_tears_down_bar_slots() -> None:
    """enabled=False 的重配必须拆掉上一次连接的 13 个传输条槽闭包。"""
    controller = _RecordingPlaybackController()
    host, bar, _page = _timeline_controls_host(controller)

    host._configure_composite_timeline_playback_controls(True)
    assert all(len(getattr(bar, name).slots) == 1 for name in _BAR_SIGNAL_NAMES)

    host._configure_composite_timeline_playback_controls(False)
    assert all(len(getattr(bar, name).slots) == 0 for name in _BAR_SIGNAL_NAMES), (
        "BUG-C3-01: 禁用路径不得遗留旧槽闭包")


def test_timeline_controls_rewire_keeps_single_slot_per_bar_signal() -> None:
    """重复进入 Composite 页（enabled=True 重配）每个 bar 信号只保留一个槽。"""
    controller = _RecordingPlaybackController()
    host, bar, _page = _timeline_controls_host(controller)

    for _ in range(3):
        host._configure_composite_timeline_playback_controls(True)

    assert all(len(getattr(bar, name).slots) == 1 for name in _BAR_SIGNAL_NAMES)


# =============================================================================
# BUG-C2-04：Level-3 保留进入原因（degraded_from）
# =============================================================================


@pytest.fixture
def clean_playback_registry():
    """测试后清理模块级播放引擎注册表（对齐 video 覆盖套件的 autouse）。"""
    yield
    with _global_playback_state_lock:
        _global_playback_engines.clear()


def _make_first_frame_controller(qapp) -> EnhancedVideoPlaybackEngine:
    """构造挂在 mock coordinator 上的 EnhancedVideoPlaybackEngine。"""
    engine = make_engine()
    coordinator = make_coordinator()
    controller = EnhancedVideoPlaybackEngine(engine)
    controller._use_dual_path = True
    controller._coordinator = coordinator
    controller.current_path = "/tmp/w6g3-aud6-first-frame.mp4"
    return controller


def test_first_frame_placeholder_preserves_timeout_reason(
        qapp, clean_playback_registry) -> None:
    """master/proxy 都超时：终态 PLACEHOLDER，但进入原因 TIMEOUT_PROXY 可观测。"""
    controller = _make_first_frame_controller(qapp)
    controller._coordinator.session = _session_for_first_frame(None, None)
    try:
        result = controller._show_first_frame_via_coordinator()
    finally:
        controller.stop_playback()

    assert result.status is FirstFrameStatus.PLACEHOLDER
    assert result.degraded_from is FirstFrameStatus.TIMEOUT_PROXY
    assert result.needs_placeholder is True
    assert result.is_displayable is False
    assert result.should_fallback_v2 is False


def test_first_frame_placeholder_preserves_frame_path_degraded_reason(
        qapp, clean_playback_registry) -> None:
    """master/proxy 帧都拿到但处理失败（高位深转换失败等）：
    进入原因为 FRAME_PATH_DEGRADED，不再被无条件覆盖丢失。"""
    controller = _make_first_frame_controller(qapp)
    controller._coordinator.session = _session_for_first_frame(
        make_packed_frame(0), make_packed_frame(0, color=(1, 2, 3)))
    controller._process_coordinator_frame = lambda frame: None
    try:
        result = controller._show_first_frame_via_coordinator()
    finally:
        controller.stop_playback()

    assert result.status is FirstFrameStatus.PLACEHOLDER
    assert result.degraded_from is FirstFrameStatus.FRAME_PATH_DEGRADED


def test_first_frame_master_ok_has_no_degraded_from(
        qapp, clean_playback_registry) -> None:
    """成功路径不携带 degraded_from（字段默认 None）。"""
    controller = _make_first_frame_controller(qapp)
    controller._coordinator.session = _session_for_first_frame(
        make_packed_frame(0), None)
    try:
        result = controller._show_first_frame_via_coordinator()
    finally:
        controller.stop_playback()

    assert result.status is FirstFrameStatus.MASTER_OK
    assert result.degraded_from is None


# =============================================================================
# AUD-R2-005：OpenBLAS 单线程隔离在位
# =============================================================================


def test_openblas_thread_cap_is_set_before_numpy_work() -> None:
    """tests/conftest.py 在 numpy 导入前 setdefault OPENBLAS_NUM_THREADS=1，
    防止本机 OpenBLAS 多线程 dgetrf 在 QThread 工作线程中并行崩溃
    （exit 132 / SIGILL）。已存在的用户显式设置不被覆盖。"""
    assert os.environ.get("OPENBLAS_NUM_THREADS") is not None
