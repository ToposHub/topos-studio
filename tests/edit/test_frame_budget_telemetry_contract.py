"""
A0.1 帧预算遥测契约测试（schema v3）。

覆盖：
1. TelemetryAggregator 段有效性（stage_status）/ 覆盖率 / enrich 失败计数 /
   note_present_outcome。
2. FrameTraceCollector.annotate 白名单收紧、字节字段累加合并、呈现状态
   生命周期（submitted → displayed 自动计算 present_ms）、回读分类词表。
3. 实测 0.0（measured）与未测量（unset）在 summary 中可区分。
"""
from __future__ import annotations

import pytest

from src.features.edit.core.realtime_playback.telemetry import (
    PlaybackFrameTelemetry,
    TelemetryAggregator,
)
from src.shared.video.frame_trace_schema import (
    PRESENT_STATUSES,
    READBACK_KINDS,
    STAGE_VALIDITY_STATES,
    FRAME_TRACE_SCHEMA_VERSION,
)
from src.shared.video.playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
)


# =============================================================================
# 1. schema v3 词表
# =============================================================================


def test_schema_v3_vocabularies_locked():
    assert FRAME_TRACE_SCHEMA_VERSION == 3
    assert STAGE_VALIDITY_STATES == (
        "unset", "measured", "not_applicable", "unavailable")
    assert "superseded" in PRESENT_STATUSES
    assert "slot_released" in PRESENT_STATUSES
    assert "monitor" in READBACK_KINDS
    assert "cpu_fallback" in READBACK_KINDS


# =============================================================================
# 2. TelemetryAggregator 段有效性与覆盖率
# =============================================================================


def test_stage_status_vocab_enforced_on_record():
    agg = TelemetryAggregator()
    bad = PlaybackFrameTelemetry(timeline_frame=1)
    bad.stage_status["nonexistent_stage"] = "measured"
    with pytest.raises(ValueError):
        agg.record(bad)
    bad2 = PlaybackFrameTelemetry(timeline_frame=1)
    bad2.stage_status["decode"] = "kinda_fast"
    with pytest.raises(ValueError):
        agg.record(bad2)


def test_measured_zero_counts_as_sample_unset_does_not():
    agg = TelemetryAggregator()
    measured_zero = PlaybackFrameTelemetry(timeline_frame=1, trace_id=1)
    measured_zero.mark_stage("decode", "measured")  # decode_ms=0.0 实测 0
    agg.record(measured_zero)
    agg.record(PlaybackFrameTelemetry(timeline_frame=2, trace_id=2))  # unset
    summary = agg.summary()
    assert summary["coverage"]["decode"]["samples"] == 1
    assert summary["coverage"]["decode"]["coverage"] == 0.5
    # 实测 0 进样本后 mean=0.0，但不能因此把它当"没测"丢掉
    assert summary["decode_ms_mean"] == 0.0


def test_not_applicable_excluded_from_samples_but_counted():
    agg = TelemetryAggregator()
    zero_copy = PlaybackFrameTelemetry(timeline_frame=1, trace_id=1)
    zero_copy.mark_stage("import_upload", "not_applicable")
    agg.record(zero_copy)
    planar = PlaybackFrameTelemetry(timeline_frame=2, trace_id=2)
    planar.upload_ms = 1.5
    planar.mark_stage("import_upload", "measured")
    agg.record(planar)
    summary = agg.summary()
    assert summary["coverage"]["import_upload"]["samples"] == 1
    assert summary["upload_ms_mean"] == 1.5


def test_enrich_last_upload_and_present_segments():
    agg = TelemetryAggregator()
    agg.record(PlaybackFrameTelemetry(timeline_frame=10, trace_id=77))
    ok = agg.enrich_last(
        10, decode_ms=5.0, total_ms=20.0, composite_ms=8.0, trace_id=77,
        upload_ms=1.2, present_ms=3.3,
    )
    assert ok is True
    record = agg._records[-1]
    assert record.upload_ms == 1.2
    assert record.present_ms == 3.3
    assert record.stage_status["import_upload"] == "measured"
    assert record.stage_status["present"] == "measured"
    summary = agg.summary()
    assert summary["upload_ms_p50"] == 1.2
    assert summary["present_ms_p50"] == 3.3


def test_enrich_last_upload_only_backfill_and_status_only():
    agg = TelemetryAggregator()
    agg.record(PlaybackFrameTelemetry(timeline_frame=5, trace_id=9))
    # 只回填 upload 段（GL 完成回调场景）
    assert agg.enrich_last(5, trace_id=9, upload_ms=0.8) is True
    rec = agg._records[-1]
    assert rec.upload_ms == 0.8
    assert rec.decode_ms == 0.0
    assert "decode" not in rec.stage_status
    # 零拷贝帧：无 upload 数值，仅标记 not_applicable
    assert agg.enrich_last(5, trace_id=9, upload_status="not_applicable") is True
    assert rec.stage_status["import_upload"] == "not_applicable"
    with pytest.raises(ValueError):
        agg.enrich_last(5, trace_id=9, upload_status="zero_copy_dude")


def test_enrich_miss_counted_not_swallowed():
    agg = TelemetryAggregator()
    agg.record(PlaybackFrameTelemetry(timeline_frame=1, trace_id=1))
    # trace_id 匹配不到且帧号也匹配不到（滚出窗口/旁路帧）
    assert agg.enrich_last(999, decode_ms=1.0, total_ms=2.0, trace_id=424242) is False
    assert agg.summary()["enrich_misses"] == 1


def test_note_present_outcome_distribution():
    agg = TelemetryAggregator()
    agg.record(PlaybackFrameTelemetry(timeline_frame=1, trace_id=1))
    agg.record(PlaybackFrameTelemetry(timeline_frame=2, trace_id=2))
    assert agg.note_present_outcome(1, 1, "submitted") is True
    assert agg.note_present_outcome(1, 1, "slot_released") is True  # 演进覆盖
    assert agg.note_present_outcome(2, 2, "superseded") is True
    counts = agg.summary()["present_outcome_counts"]
    assert counts.get("slot_released") == 1
    assert counts.get("superseded") == 1
    assert "submitted" not in counts  # 被演进覆盖后从分布移除
    with pytest.raises(ValueError):
        agg.note_present_outcome(1, 1, "kinda_displayed")


def test_legacy_records_without_stage_status_still_summarized():
    """旧调用方不标 stage_status：value>0 的记录仍进样本（兼容）。"""
    agg = TelemetryAggregator()
    legacy = PlaybackFrameTelemetry(timeline_frame=1, decode_ms=4.0, total_ms=16.0)
    agg.record(legacy)
    summary = agg.summary()
    assert summary["coverage"]["decode"]["samples"] == 1
    assert summary["decode_ms_mean"] == 4.0


# =============================================================================
# 3. FrameTraceCollector.annotate（schema v3）
# =============================================================================


def _collector_with_trace(trace_id: int, frame: int = 10) -> FrameTraceCollector:
    collector = FrameTraceCollector(max_frames=64)
    collector.start_session(page="test")
    collector.record(EditPlaybackFrameTrace(
        frame_index=frame, trace_id=trace_id, deadline_ns=0))
    return collector


def test_annotate_rejects_unknown_fields():
    collector = _collector_with_trace(5)
    with pytest.raises(ValueError, match="unknown field"):
        collector.annotate(5, totally_made_up=1.0)


def test_annotate_rejects_invalid_vocab_values():
    collector = _collector_with_trace(5)
    with pytest.raises(ValueError, match="present_status"):
        collector.annotate(5, present_status="kinda_shown")
    with pytest.raises(ValueError, match="readback_kind"):
        collector.annotate(5, readback_kind="oops")
    with pytest.raises(ValueError, match="import_upload_status"):
        collector.annotate(5, import_upload_status="skipped")


def test_annotate_byte_fields_accumulate():
    collector = _collector_with_trace(5)
    assert collector.annotate(5, cpu_upload_bytes=100, cpu_download_bytes=50) is True
    assert collector.annotate(5, cpu_upload_bytes=25, cpu_download_bytes=30,
                              readback_kind="monitor") is True
    trace = collector.get_traces()[-1]
    assert trace.cpu_upload_bytes == 125
    assert trace.cpu_download_bytes == 80
    assert trace.readback_kind == "monitor"
    summary = collector.get_summary()
    assert summary["pixel_copy_bytes"]["cpu_upload"] == 125
    assert summary["pixel_copy_bytes"]["cpu_download"] == 80
    assert summary["readback_kind_counts"] == {"monitor": 1}


def test_annotate_present_lifecycle_computes_present_ms():
    collector = _collector_with_trace(6)
    assert collector.annotate(
        6, present_status="submitted", present_slot_id=3,
        present_submitted_mono=__import__("time").monotonic() - 0.005,
    ) is True
    assert collector.annotate(6, present_status="viewer_copy") is True
    assert collector.annotate(6, present_status="displayed") is True
    trace = collector.get_traces()[-1]
    # submitted(-5ms) → displayed(now)：present_ms ≈ 5ms
    assert 0.0 < trace.present_ms < 1000.0
    assert trace.present_status == "displayed"
    summary = collector.get_summary()
    assert summary["present_status_counts"]["displayed"] == 1


def test_annotate_superseded_before_display_keeps_present_ms_zero():
    collector = _collector_with_trace(7)
    collector.annotate(7, present_status="submitted",
                       present_submitted_mono=__import__("time").monotonic())
    collector.annotate(7, present_status="superseded")
    trace = collector.get_traces()[-1]
    assert trace.present_status == "superseded"
    assert trace.present_ms == 0.0  # 从未 displayed：不得虚构 present_ms


def test_annotate_gpu_query_backfill_and_counters():
    collector = _collector_with_trace(8)
    assert collector.annotate(
        8, gpu_submit_ms=2.5, gpu_time_unavailable=False) is True
    trace = collector.get_traces()[-1]
    assert trace.gpu_submit_ms == 2.5
    assert trace.gpu_time_unavailable is False
    # 迟到证据：滚出 ring / trace_id=0 → 计数可见
    assert collector.annotate(0, cpu_upload_bytes=1) is False
    assert collector.annotate(999999, cpu_upload_bytes=1) is False
    summary = collector.get_summary()
    assert summary["annotate"] == {"merged": 1, "missed": 2}


def test_import_upload_status_coverage_summary():
    collector = FrameTraceCollector(max_frames=64)
    collector.start_session(page="test")
    for i, status in enumerate(["measured", "not_applicable", "", "unavailable"]):
        collector.record(EditPlaybackFrameTrace(frame_index=i, trace_id=i + 1))
        if status:
            assert collector.annotate(i + 1, import_upload_status=status) is True
    coverage = collector.get_summary()["import_upload_coverage"]
    assert coverage == {"measured_frames": 1, "not_applicable_frames": 1,
                        "unavailable_frames": 1, "unset_frames": 1}
