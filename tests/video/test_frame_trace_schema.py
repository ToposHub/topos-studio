"""P0-01/P0-03/P0-04: 统一帧 trace schema、像素拷贝计数器单元测试。"""

import json

import pytest

from src.shared.video.frame_trace_schema import (
    FRAME_TRACE_SCHEMA_VERSION,
    FrameTraceContext,
    GPU_TIME_UNAVAILABLE,
    next_frame_trace_id,
)
from src.shared.video.playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
)
from src.shared.video.pixel_copy_counters import (
    CopyKind,
    add_pixel_copy,
    reset_pixel_copies,
    snapshot_pixel_copies,
)


class TestFrameTraceId:
    """trace id 分配器（P0-01/P0-02）。"""

    def test_ids_are_monotonic_and_unique(self):
        ids = [next_frame_trace_id() for _ in range(1000)]
        assert len(set(ids)) == 1000
        assert ids == sorted(ids)

    def test_ids_start_above_zero(self):
        # 0 保留给"非 engine 派发"的帧
        assert next_frame_trace_id() >= 1

    def test_thread_safety(self):
        import threading

        results: list = []
        lock = threading.Lock()

        def worker():
            local = [next_frame_trace_id() for _ in range(200)]
            with lock:
                results.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(results)) == 8 * 200


class TestFrameTraceContext:
    def test_to_dict_shape(self):
        ctx = FrameTraceContext(
            trace_id=7, timeline_frame=100, generation_id=3,
            deadline_ns=123, mode="realtime", priority="playhead",
            backend="opengl",
        )
        d = ctx.to_dict()
        assert d["trace_id"] == 7
        assert d["timeline_frame"] == 100
        assert d["generation_id"] == 3
        assert d["deadline_ns"] == 123
        assert d["mode"] == "realtime"
        assert d["priority"] == "playhead"
        assert d["backend"] == "opengl"

    def test_frozen(self):
        ctx = FrameTraceContext(trace_id=1, timeline_frame=1, generation_id=1)
        with pytest.raises(Exception):
            ctx.trace_id = 2


class TestFrameTraceSchema:
    """EditPlaybackFrameTrace 新字段与导出（P0-01/P0-03/P0-04）。"""

    def test_schema_version_default(self):
        trace = EditPlaybackFrameTrace()
        assert trace.schema_version == FRAME_TRACE_SCHEMA_VERSION

    def test_trace_id_default_zero_means_bypass(self):
        trace = EditPlaybackFrameTrace()
        assert trace.trace_id == 0

    def test_gpu_time_unavailable_by_default(self):
        trace = EditPlaybackFrameTrace()
        assert trace.gpu_time_unavailable is True
        assert trace.gpu_submit_ms == 0.0

    def test_compact_dict_includes_trace_id(self):
        trace = EditPlaybackFrameTrace(frame_index=42, trace_id=99)
        d = trace.to_compact_dict()
        assert d["trace_id"] == 99
        assert d["frame_index"] == 42

    def test_compact_dict_omits_default_copy_bytes(self):
        trace = EditPlaybackFrameTrace(frame_index=1)
        d = trace.to_compact_dict()
        assert "cpu_upload_bytes" not in d
        assert "gpu_submit_ms" not in d

    def test_export_json_roundtrip_has_new_keys(self):
        collector = FrameTraceCollector()
        collector.start_session(page="test")
        collector.record(EditPlaybackFrameTrace(
            frame_index=1, trace_id=5,
            gpu_submit_ms=2.5, gpu_time_unavailable=False,
            cpu_upload_bytes=1024,
        ))
        collector.record(EditPlaybackFrameTrace(frame_index=2, trace_id=6))
        data = json.loads(collector.export_json())
        summary = data["summary"]
        assert summary["gpu_time"]["measured_frames"] == 1
        assert summary["gpu_time"]["unavailable_frames"] == 1
        assert summary["pixel_copy_bytes"]["cpu_upload"] == 1024
        assert summary["pixel_copy_bytes"]["texture_write"] == 0
        traces = data["traces"]
        assert traces[0]["trace_id"] == 5
        assert traces[1]["trace_id"] == 6

    def test_empty_summary_key_shape(self):
        collector = FrameTraceCollector()
        summary = collector.get_summary()
        assert summary["gpu_time"] == {
            "measured_frames": 0, "unavailable_frames": 0,
        }
        assert set(summary["pixel_copy_bytes"].keys()) == {
            "cpu_upload", "cpu_download", "surface_map",
            "texture_write", "cross_device",
        }

    def test_gpu_time_summary_percentiles(self):
        collector = FrameTraceCollector()
        for i in range(10):
            collector.record(EditPlaybackFrameTrace(
                frame_index=i, trace_id=i + 1,
                gpu_submit_ms=1.0 + i, gpu_time_unavailable=False,
            ))
        gpu_time = collector.get_summary()["gpu_time"]
        assert gpu_time["measured_frames"] == 10
        assert gpu_time["gpu_submit_ms_p50"] == 6.0
        assert gpu_time["gpu_submit_ms_max"] == 10.0


class TestPixelCopyCounters:
    """像素拷贝字节审计（P0-04）。"""

    def setup_method(self):
        reset_pixel_copies()

    def teardown_method(self):
        reset_pixel_copies()

    def test_add_and_snapshot(self):
        add_pixel_copy(CopyKind.CPU_UPLOAD, 1000)
        add_pixel_copy(CopyKind.CPU_UPLOAD, 500)
        add_pixel_copy(CopyKind.CPU_DOWNLOAD, 256)
        snap = snapshot_pixel_copies()
        assert snap["cpu_upload"] == {"bytes": 1500, "events": 2}
        assert snap["cpu_download"] == {"bytes": 256, "events": 1}
        assert snap["cross_device"] == {"bytes": 0, "events": 0}

    def test_zero_bytes_skipped_negative_rejected(self):
        # AUD-39304: 负数字节（调用点 bug，如 signed 差值下溢）按
        # fail-closed 语义抛 ValueError 拒绝，不再静默忽略；0 仍静默跳过。
        add_pixel_copy(CopyKind.CPU_UPLOAD, 0)
        with pytest.raises(ValueError, match="negative"):
            add_pixel_copy(CopyKind.CPU_UPLOAD, -5)
        snap = snapshot_pixel_copies()
        assert snap["cpu_upload"] == {"bytes": 0, "events": 0}

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            add_pixel_copy("not_a_kind", 100)

    def test_reset(self):
        add_pixel_copy(CopyKind.TEXTURE_WRITE, 4096)
        reset_pixel_copies()
        snap = snapshot_pixel_copies()
        assert all(v["bytes"] == 0 for v in snap.values())


class TestSegmentBudgets:
    """P1-06 分段 deadline 预算表。"""

    def test_ratios_sum_to_one(self):
        from src.shared.video.frame_trace_schema import SEGMENT_BUDGET_RATIOS

        assert abs(sum(r for _, r in SEGMENT_BUDGET_RATIOS) - 1.0) < 1e-9

    def test_budgets_partition_deadline(self):
        from src.shared.video.frame_trace_schema import segment_budgets_ms

        budgets = segment_budgets_ms(int(33.3 * 1e6))
        assert set(budgets) == {
            "decode", "import_upload", "effects", "transition",
            "composite", "present",
        }
        # 分配之和不超过总预算（round-down 语义，防段总和越界）
        assert sum(budgets.values()) <= 33.3 + 1e-6
        assert budgets["decode"] > budgets["present"]

    def test_no_deadline_means_no_budgets(self):
        from src.shared.video.frame_trace_schema import segment_budgets_ms

        assert segment_budgets_ms(0) == {}
        assert segment_budgets_ms(-5) == {}


class TestDeadlineMissAttribution:
    """P1-06 首段越界归因。"""

    def _trace(self, deadline_ms: float = 33.3, **kw):
        return EditPlaybackFrameTrace(
            deadline_ns=int(deadline_ms * 1e6), **kw
        )

    def test_no_deadline_no_miss(self):
        assert EditPlaybackFrameTrace().evaluate_deadline_miss() == ''

    def test_all_within_budget_no_miss(self):
        t = self._trace(decode_ms=10.0, gpu_submit_ms=3.0,
                        gpu_time_unavailable=False, total_ms=20.0)
        assert t.evaluate_deadline_miss() == ''
        assert t.deadline_missed_segment == ''

    def test_decode_over_attribution(self):
        t = self._trace(decode_ms=20.0)  # 40% of 33.3 = 13.3
        assert t.evaluate_deadline_miss() == "decode"

    def test_composite_over_attribution(self):
        t = self._trace(decode_ms=5.0, gpu_submit_ms=6.0,
                        gpu_time_unavailable=False)  # composite budget 5.0
        assert t.evaluate_deadline_miss() == "composite"

    def test_gpu_unavailable_skips_composite_check(self):
        # GPU 时间不可用（fail-closed）时不得拿 0.0 冒充"composite 达标"
        # 去掩盖 total 越界。
        t = self._trace(decode_ms=1.0, total_ms=40.0)  # gpu 默认 unavailable
        assert t.evaluate_deadline_miss() == "total"

    def test_total_over_when_segments_ok(self):
        t = self._trace(decode_ms=5.0, total_ms=40.0)
        assert t.evaluate_deadline_miss() == "total"

    def test_attribution_is_idempotent(self):
        t = self._trace(decode_ms=20.0)
        assert t.evaluate_deadline_miss() == "decode"
        assert t.evaluate_deadline_miss() == "decode"


class TestCollectorDeadlineSummary:
    """record() 统一归因 + summary deadline 块。"""

    def test_record_evaluates_deadline(self):
        collector = FrameTraceCollector()
        collector.start_session(page="test")
        collector.record(EditPlaybackFrameTrace(
            frame_index=1,
            deadline_ns=int(33.3 * 1e6),
            decode_ms=30.0,
        ))
        traces = collector.get_traces()
        assert traces[0].deadline_missed_segment == "decode"

    def test_summary_counts_miss_by_segment(self):
        collector = FrameTraceCollector()
        collector.start_session(page="test")
        collector.record(EditPlaybackFrameTrace(
            frame_index=1, deadline_ns=int(33.3 * 1e6), decode_ms=30.0))
        collector.record(EditPlaybackFrameTrace(
            frame_index=2, deadline_ns=int(33.3 * 1e6), total_ms=50.0))
        collector.record(EditPlaybackFrameTrace(frame_index=3))  # 无 deadline
        summary = collector.get_summary()
        block = summary["deadline"]
        assert block["frames_with_deadline"] == 2
        assert block["miss_frames"] == 2
        assert block["miss_by_segment"] == {"decode": 1, "total": 1}


class TestInFlightFrameIds:
    """在飞帧身份字典（P0-02 guard-drop 归因）。"""

    def _stub(self):
        from types import SimpleNamespace

        from src.features.edit.core.timeline_preview_renderer import (
            TimelinePreviewRenderer,
        )

        stub = SimpleNamespace(_in_flight_frame_ids={})
        return TimelinePreviewRenderer._take_in_flight_frame_ids, stub

    def test_missing_frame_returns_zeroes(self):
        take, stub = self._stub()
        assert take(stub, 42) == (0, 0)

    def test_take_removes_entry(self):
        take, stub = self._stub()
        stub._in_flight_frame_ids[7] = (123, 33_300_000)
        assert take(stub, 7) == (123, 33_300_000)
        assert 7 not in stub._in_flight_frame_ids
        assert take(stub, 7) == (0, 0)
