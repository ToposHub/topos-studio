"""P0-4: per-frame Composite trace contract.

Every frame must carry a stable trace_id (caller-supplied or generated),
record strategy/timings/transfer bytes with unknowns explicitly marked
(gpu_time_unavailable, cpu_*_bytes=None when unknown), and be delivered
through the sink hook as a JSON-serializable dict.
"""

import json

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.render.composite_export_renderer import (
    CompositeExportRenderer,
)
from src.features.composite.render.frame_trace import (
    CompositeFrameTrace,
    new_trace_id,
    set_trace_sink,
)
from src.shared.gpu.composite_gpu_frame_handle import (
    CompositeGpuFrameHandle,
)

pytestmark = pytest.mark.composite_core

W, H = 640, 400


def _solid_graph() -> NodeGraph:
    data = {
        "nodes": [
            {"id": "bg", "type": "constant", "parameters": {
                "width": W, "height": H, "mode": "solid",
                "color": [0.5, 0.25, 0.125, 1.0]}},
            {"id": "out", "type": "output", "parameters": {
                "format_mode": "custom", "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "bg", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }
    return NodeGraph.from_dict(data)


@pytest.fixture()
def headless_renderer(monkeypatch):
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
        ctx.release()
    except Exception as exc:  # pragma: no cover - CI GPU availability
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
    renderer = CompositeExportRenderer()
    renderer.set_persistent_mode(True)
    yield renderer
    renderer.cleanup()


class TestFrameTrace:
    def test_readback_trace_attached_and_correlatable(self, headless_renderer):
        tid = "edit-frame-42-lane-1"
        snapshot = _solid_graph().snapshot(
            color_texture_id=0, color_width=0, color_height=0,
            color_revision=0)
        result = headless_renderer.render_and_readback(
            snapshot, readback_output="srgb_u8", trace_id=tid)
        assert not result.error, result.error
        tr = result.frame_trace
        assert tr is not None
        # Correlation: the caller's id comes back verbatim.
        assert tr["trace_id"] == tid
        assert tr["producer"] == "readback"
        assert tr["readback_format"] == "srgb_u8"
        assert tr["target"] == [W, H]
        assert tr["strategy"] == "full_frame"
        assert tr["node_count"] == 2
        # Unknowns are marked, never faked.
        assert tr["gpu_time_unavailable"] is True
        assert tr["gpu_time_ms"] is None
        assert tr["cpu_download_bytes"] == result.image.nbytes
        assert isinstance(tr["total_ms"], float)
        # The dict must survive a JSON round-trip (unified event schema).
        json.dumps(tr)

    def test_sink_receives_trace_dict(self, headless_renderer):
        seen = []
        set_trace_sink(seen.append)
        try:
            snapshot = _solid_graph().snapshot(
                color_texture_id=0, color_width=0, color_height=0,
                color_revision=0)
            result = headless_renderer.render_and_readback(
                snapshot, readback_output="linear_f32")
            assert not result.error, result.error
        finally:
            set_trace_sink(None)
        assert len(seen) == 1
        assert seen[0]["producer"] == "readback"
        assert seen[0]["readback_format"] == "linear_f32"

    def test_gpu_handle_trace_proves_zero_transfer(self, headless_renderer):
        seen = []
        set_trace_sink(seen.append)
        try:
            snapshot = _solid_graph().snapshot(
                color_texture_id=0, color_width=0, color_height=0,
                color_revision=0)
            handle = headless_renderer.render_gpu_handle(
                snapshot, frame_number=9, generation=3,
                trace_id="tl-handle-9")
            assert handle.available
        finally:
            set_trace_sink(None)
        tr = seen[-1]
        assert tr["producer"] == "gpu_handle"
        assert tr["trace_id"] == "tl-handle-9"
        # GPU→GPU path: the zero-transfer claim is trace-provable.
        assert tr["cpu_download_bytes"] == 0
        assert tr["cpu_upload_bytes"] == 0
        # The handle echoes the correlation id (consumer-side join key).
        assert handle.trace_id == "tl-handle-9"
        assert handle.as_dict()["trace_id"] == "tl-handle-9"

    def test_failure_trace_records_fallback_reason(self, headless_renderer):
        empty = NodeGraph.from_dict({
            "nodes": [{"id": "out", "type": "output", "parameters": {
                "format_mode": "custom", "width": W, "height": H}}],
            "connections": [],
        })
        snapshot = empty.snapshot(color_texture_id=0, color_width=0,
                                  color_height=0, color_revision=0)
        handle = headless_renderer.render_gpu_handle(
            snapshot, trace_id="broken-1")
        assert not handle.available
        assert handle.trace_id == "broken-1"
        assert handle.fallback_reason  # diagnosable, not silent


class TestTraceId:
    def test_ids_are_unique(self):
        ids = {new_trace_id() for _ in range(100)}
        assert len(ids) == 100

    def test_default_trace_record_shape(self):
        tr = CompositeFrameTrace().to_dict()
        assert tr["trace_id"].startswith("comp-")
        assert tr["backend"] == "opengl"
        json.dumps(tr)
