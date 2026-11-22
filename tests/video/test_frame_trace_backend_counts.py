"""Actual render backend in the playback frame trace.

The value written when a frame is enqueued only says whether a GPU renderer
exists ("opengl"/"cpu").  Metal preview frames choose their backend per
frame, so the present descriptor's declared backend is merged into the trace
late and counted in ``summary()['backend_counts']`` — the only place that
proves which backend rendered a frame.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.shared.video.playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
    get_trace_collector,
)


def _trace(trace_id: int, frame: int = 1) -> EditPlaybackFrameTrace:
    trace = EditPlaybackFrameTrace(frame_index=frame, trace_id=trace_id)
    trace.render_path = "gpu_composite"
    # Enqueue-time placeholder: a GPU renderer exists, backend unknown yet.
    trace.backend = "opengl"
    return trace


class TestBackendAnnotate:
    def test_annotate_backend_overrides_enqueue_placeholder(self) -> None:
        collector = FrameTraceCollector()
        collector.record(_trace(trace_id=7))
        assert collector.annotate(7, backend="metal") is True
        trace = collector.get_traces()[0]
        assert trace.backend == "metal"

    def test_summary_counts_each_backend(self) -> None:
        collector = FrameTraceCollector()
        for index in range(3):
            collector.record(_trace(trace_id=index + 1, frame=index))
        collector.annotate(1, backend="metal")
        collector.annotate(2, backend="metal")
        collector.annotate(3, backend="opengl")
        assert collector.get_summary()["backend_counts"] == {"metal": 2, "opengl": 1}

    def test_backend_outside_vocabulary_is_rejected(self) -> None:
        collector = FrameTraceCollector()
        collector.record(_trace(trace_id=1))
        with pytest.raises(ValueError):
            collector.annotate(1, backend="vulkan")
        assert collector.get_traces()[0].backend == "opengl"

    def test_empty_session_keeps_backend_counts_shape(self) -> None:
        assert FrameTraceCollector().get_summary()["backend_counts"] == {}

    def test_global_collector_round_trip(self) -> None:
        collector = get_trace_collector()
        collector.start_session(page="edit")
        collector.record(_trace(trace_id=99))
        assert collector.annotate(99, backend="metal") is True
        assert collector.get_summary()["backend_counts"] == {"metal": 1}


class TestPresentRenderBackend:
    def test_reads_gl_slot_extra(self) -> None:
        present = SimpleNamespace(extra={"render_backend": "opengl"}, slot_id=0)
        assert TimelinePreviewRenderer._present_render_backend(present) == "opengl"

    def test_native_api_wins_over_extra(self) -> None:
        present = SimpleNamespace(
            extra={"render_backend": "opengl"},
            native=SimpleNamespace(api="Metal"),
        )
        assert TimelinePreviewRenderer._present_render_backend(present) == "metal"

    def test_missing_or_unknown_declares_none(self) -> None:
        assert TimelinePreviewRenderer._present_render_backend(object()) is None
        present = SimpleNamespace(extra={"render_backend": "vulkan"})
        assert TimelinePreviewRenderer._present_render_backend(present) is None
