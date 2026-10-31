"""A1.1 GC 停顿观测器测试（纯逻辑，不触发真实 GC）。"""
from __future__ import annotations

import time

from src.shared.video.gc_pause_observer import GcPauseObserver
from src.shared.video.playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
)


def _fire(observer: GcPauseObserver, gen: int, duration_s: float) -> None:
    """直接调用 gc 回调协议（start → stop），duration 由时钟伪造不可控，
    这里用 monkey 时间：start 记录 now，stop 前 sleep——测试只要求 >0。"""
    observer._on_gc("start", {"generation": gen})
    if duration_s > 0:
        time.sleep(duration_s)
    observer._on_gc("stop", {"generation": gen})


def test_observer_records_and_aggregates():
    observer = GcPauseObserver()
    observer.start()
    try:
        _fire(observer, 0, 0.002)
        _fire(observer, 0, 0.001)
        _fire(observer, 2, 0.005)
    finally:
        summary = observer.stop()
    assert observer.running is False
    assert summary["collections"] == 3
    assert summary["pause_ms_total"] > 8.0  # ≥ 8ms 合计（各次 ≥ 时长）
    assert summary["pause_ms_max"] >= 4.9
    gens = summary["by_generation"]
    assert gens["0"]["collections"] == 2
    assert gens["2"]["collections"] == 1


def test_observer_overlap_seconds():
    observer = GcPauseObserver()
    now = time.monotonic()
    # 手工注入确定值（绕过时钟）：(stop_ts, dur, gen)
    observer._pauses.append((now - 1.0, 0.01, 0))
    observer._pauses.append((now - 100.0, 0.5, 2))
    assert 0.0049 < observer.overlap_seconds(now - 1.005, now) <= 0.0101
    assert observer.overlap_seconds(now - 50, now - 40) == 0.0


def test_collector_gc_correlation_with_deadline_miss():
    collector = FrameTraceCollector(max_frames=32)
    collector.start_session(page="gc-test")
    now = time.monotonic()
    # 一个停顿，与 miss 帧的渲染窗口重叠
    collector.note_gc_pause(now - 0.005, 0.004, 0)
    collector.record(EditPlaybackFrameTrace(
        frame_index=1, trace_id=1, deadline_ns=10_000_000,  # 10ms 预算
        total_ms=50.0, timestamp=now, decode_ms=60.0))  # 必 miss
    collector.record(EditPlaybackFrameTrace(
        frame_index=2, trace_id=2, deadline_ns=0, total_ms=5.0,
        timestamp=now))  # 无 deadline，不参与关联
    gc_section = collector.get_summary()["gc"]
    assert gc_section["pause_count"] == 1
    assert gc_section["pause_ms_total"] == 4.0
    assert gc_section["deadline_miss_frames_overlapping_gc"] == 1


def test_engine_enables_observer_via_env(qapp, monkeypatch):
    from src.features.edit.core.realtime_playback.engine import RealtimePlaybackEngine

    monkeypatch.setenv("TOPOS_GC_PAUSE_OBSERVER", "1")
    engine = RealtimePlaybackEngine()
    assert engine._gc_observer is not None
    assert engine._gc_observer.running is True
    engine.shutdown()
    assert engine._gc_observer is None

    monkeypatch.delenv("TOPOS_GC_PAUSE_OBSERVER", raising=False)
    engine2 = RealtimePlaybackEngine()
    assert engine2._gc_observer is None
    engine2.shutdown()
