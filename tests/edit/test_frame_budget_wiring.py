"""
A0.1 接线层单测（无 GL / 无真实工程）：

1. GlPassTimer：begin_frame(tag) → collect() 返回带发起帧 tag 的结果；
   读回失败 fail-closed；unsupported 时 collect 返回空。
2. TPR：_take_in_flight_frame_ids 的历史映射（GPU query 迟到回填身份）、
   呈现状态 annotate 辅助（submitted 附带单调时钟；dropped 帧不被
   slot_released 覆盖）。
3. engine：scrub 记录携带 generation_id；背压 drop 在 trace 通道留下
   present_status='dropped' 显式记录。
"""
from __future__ import annotations

import types

import numpy as np
import pytest

from src.shared.gpu.gl_pass_timer import GlPassTimer


# =============================================================================
# 1. GlPassTimer tag 绑定
# =============================================================================


class _FakeQuery:
    """ModernGL time query 替身：elapsed ns 可配；release 幂等。"""

    def __init__(self, elapsed_ns: int | None = 1_500_000) -> None:
        self._elapsed = elapsed_ns
        self.released = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    @property
    def elapsed(self):
        if self._elapsed is None:
            raise RuntimeError("query not ready")
        return self._elapsed

    def release(self) -> None:
        self.released = True


class _FakeCtx:
    def __init__(self, elapsed_ns: int | None = 1_500_000,
                 fail: bool = False) -> None:
        self._elapsed = elapsed_ns
        self._fail = fail

    def query(self, time=True):
        if self._fail:
            raise RuntimeError("no timer query support")
        return _FakeQuery(self._elapsed)


def test_gl_pass_timer_collect_returns_origin_tag():
    timer = GlPassTimer(_FakeCtx(elapsed_ns=2_000_000))
    assert timer.unsupported is False

    timer.begin_frame(tag=101)          # 发起帧 101
    with timer.measure("composite"):
        pass
    timer.begin_frame(tag=102)
    timer.begin_frame(tag=103)          # 第 3 帧：RING_DEPTH-1=2 帧后到期
    results = timer.collect()

    assert len(results) == 1
    entry = results[0]
    assert entry["pass"] == "composite"
    assert entry["ms"] == pytest.approx(2.0)
    assert entry["tag"] == 101  # 结果归因到发起帧，不是当前帧 103


def test_gl_pass_timer_failed_readback_reports_none():
    timer = GlPassTimer(_FakeCtx(elapsed_ns=None))
    timer.begin_frame(tag=1)
    with timer.measure("composite"):
        pass
    timer.begin_frame(tag=2)
    timer.begin_frame(tag=3)
    results = timer.collect()
    assert len(results) == 1
    assert results[0]["ms"] is None  # fail-closed：不得当 0.0


def test_gl_pass_timer_unsupported_collects_nothing():
    timer = GlPassTimer(_FakeCtx(fail=True))
    assert timer.unsupported is True
    timer.begin_frame(tag=1)
    assert timer.collect() == []
    assert timer.last_ms("composite") is None


# =============================================================================
# 2. TPR 身份历史与呈现状态辅助
# =============================================================================


def _tpr_shell(**attrs):
    """用 SimpleNamespace 直接挂 TimelinePreviewRenderer 的纯逻辑方法。"""
    from src.features.edit.core.timeline_preview_renderer import (
        TimelinePreviewRenderer as TPR,
    )
    shell = types.SimpleNamespace(**attrs)
    for name in (
        "_take_in_flight_frame_ids", "_lookup_frame_trace_id",
        "_annotate_present_status", "_annotate_present_status_for_slot",
        "_record_frame_present_state", "_note_telemetry_present",
        "_annotate_present_drop",
    ):
        setattr(shell, name, getattr(TPR, name).__get__(shell))
    return shell


@pytest.fixture()
def fresh_collector():
    from src.shared.video.playback_frame_trace import (
        EditPlaybackFrameTrace,
        get_trace_collector,
        reset_trace_collector,
    )
    reset_trace_collector()
    collector = get_trace_collector()
    collector.start_session(page="wiring-test")
    collector.record(EditPlaybackFrameTrace(frame_index=10, trace_id=42))
    yield collector
    reset_trace_collector()


def test_in_flight_history_survives_pop(fresh_collector):
    from src.features.edit.core.timeline_preview_renderer import (
        TimelinePreviewRenderer as TPR,
    )
    shell = _tpr_shell(_in_flight_frame_ids={10: (42, 999)})
    entry = TPR._take_in_flight_frame_ids(shell, 10)
    assert entry == (42, 999)
    # pop 之后历史映射仍可查（GPU query 迟到回填场景）
    assert TPR._lookup_frame_trace_id(shell, 10) == 42
    assert TPR._lookup_frame_trace_id(shell, 11) == 0


def test_annotate_present_status_submitted_carries_monotonic(fresh_collector):
    from src.features.edit.core.timeline_preview_renderer import (
        TimelinePreviewRenderer as TPR,
    )
    shell = _tpr_shell(_frame_trace_id_history={10: 42})
    TPR._annotate_present_status(shell, 10, "submitted", slot_id=3)
    trace = fresh_collector.get_traces()[-1]
    assert trace.present_status == "submitted"
    assert trace.present_slot_id == 3
    assert trace.present_submitted_mono > 0.0


def test_slot_released_not_annotated_over_dropped(fresh_collector):
    from src.features.edit.core.timeline_preview_renderer import (
        TimelinePreviewRenderer as TPR,
    )
    shell = _tpr_shell(
        _frame_trace_id_history={10: 42},
        _present_slot_frames={3: 10},
        _frame_present_state={10: "dropped"},
    )
    TPR._annotate_present_status_for_slot(shell, 3, "slot_released")
    trace = fresh_collector.get_traces()[-1]
    # dropped 终态保留，不被 slot_released 覆盖
    assert trace.present_status == ""

    # submitted 状态的帧：slot 回收正常 annotate
    shell2 = _tpr_shell(
        _frame_trace_id_history={10: 42},
        _present_slot_frames={4: 10},
        _frame_present_state={10: "submitted"},
    )
    TPR._annotate_present_status_for_slot(shell2, 4, "slot_released")
    trace = fresh_collector.get_traces()[-1]
    assert trace.present_status == "slot_released"


# =============================================================================
# 3. engine 补洞
# =============================================================================


def test_scrub_record_carries_generation(qapp):
    from src.features.edit.core.realtime_playback.engine import RealtimePlaybackEngine

    engine = RealtimePlaybackEngine()
    engine.sync_generation_id(7)
    engine.request_scrub_present(10, None)
    record = engine.telemetry._records[-1]
    assert record.generation_id == 7
    assert record.mode == "scrub"
    engine.shutdown()


# =============================================================================
# 4. A1.2：监看回读 QImage 转换（Qt 快路径 vs 旧 NumPy 路径逐字节对拍）
# =============================================================================


def _legacy_pbo_to_qimage(raw, width, height, is_gl_oriented):
    """旧实现（A1.2 之前的 NumPy 路径）——对拍基准。"""
    from PyQt6.QtGui import QImage
    data = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 4))
    if is_gl_oriented:
        data = np.flipud(data)
    rgb = np.ascontiguousarray(data[:, :, :3])
    image = QImage(rgb.data, int(width), int(height), int(width) * 3,
                   QImage.Format.Format_RGB888)
    return image.copy()


@pytest.mark.parametrize("w,h,gl_oriented", [
    (64, 32, True), (64, 32, False), (13, 7, True),  # 奇数宽
])
def test_pbo_rgba_to_qimage_matches_legacy(qapp, w, h, gl_oriented):
    from PyQt6.QtGui import QImage
    from src.features.edit.ui.program_viewer import ProgramViewer

    def _rgb_bytes(img: QImage) -> "np.ndarray":
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape(
            img.height(), img.bytesPerLine())
        return arr[:, : img.width() * 3]

    rng = np.random.default_rng(11)
    raw = rng.integers(0, 256, (h, w, 4), dtype=np.uint8).tobytes()
    new_image = ProgramViewer._pbo_rgba_to_qimage(raw, w, h, gl_oriented)
    legacy_image = _legacy_pbo_to_qimage(raw, w, h, gl_oriented)
    assert new_image is not None and legacy_image is not None
    assert new_image.format() == QImage.Format.Format_RGB888
    assert (new_image.width(), new_image.height()) == (w, h)
    # 逐字节对拍（含跨距正确展开）：两实现输出必须完全一致
    np.testing.assert_array_equal(
        _rgb_bytes(new_image), _rgb_bytes(legacy_image))


def test_back_pressure_drop_recorded_in_trace_channel(qapp, fresh_collector):
    from src.features.edit.core.realtime_playback.engine import RealtimePlaybackEngine
    from src.features.edit.core.realtime_playback.types import PlaybackMode

    engine = RealtimePlaybackEngine()
    engine.set_mode(PlaybackMode.REALTIME)
    engine._budget.before_render = lambda frame: False  # 强制背压
    presented = engine.request_present(5)
    assert presented is False

    record = engine.telemetry._records[-1]
    assert record.miss_reason == "back_pressure"
    dropped = [t for t in fresh_collector.get_traces()
               if t.trace_id == record.trace_id]
    assert len(dropped) == 1
    assert dropped[0].present_status == "dropped"
    assert dropped[0].fallback_reason == "engine_back_pressure_drop"
    assert dropped[0].dispatch_channel == "engine"
    engine.shutdown()
