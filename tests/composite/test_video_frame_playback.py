"""VideoFrameNode playback tests with real media.

Covers the three fixes for video sources inside a composition:
- GPU upload orientation (top-down CPU frame -> bottom-left GL texture)
- timeline-driven frame selection (playhead advances the decoded frame)
- source_in/source_out playback range on the fallback decode path
- Keyframes-panel trim/slide edits linking to the video source params

Real footage lives in tests/test_videos; the GL tests need a standalone
ModernGL context (same pattern as tests/composite/test_nodes).
"""

import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

moderngl = pytest.importorskip("moderngl")

from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_model import Layer
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.video_frame_node import (
    VideoFrameNode, VideoLoadState)
from src.features.composite.ui.timeline_commands import apply_video_range_edit

pytestmark = pytest.mark.composite_core

VIDEO_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'test_videos', '2K_h264.mp4')

requires_video = pytest.mark.skipif(
    not os.path.isfile(VIDEO_PATH), reason="real test video missing")


@pytest.fixture
def gl_ctx():
    ctx = moderngl.create_standalone_context()
    yield ctx
    ctx.release()


def _stub_decoded_frame(value: float = 0.5) -> SimpleNamespace:
    data = np.full((4, 4, 4), value, dtype=np.float32)
    data[:, :, 3] = 1.0
    return SimpleNamespace(
        is_packed=True, is_planar=False, planes=None, data=data,
        has_alpha=True, pixel_format="rgba", bit_depth=8)


class _StubVideoContext:
    """Records request_frame_for_node kwargs, returns tiny frames."""

    def __init__(self):
        self.calls = []

    def open_clip(self, path, node_id):
        return "stub-clip"

    def get_clip_info(self, clip_id):
        return SimpleNamespace(
            fps=25.0, total_frames=100, width=4, height=4, bit_depth=8,
            is_hdr=False, codec_name="stub", color_primaries=None,
            color_transfer=None, color_matrix=None)

    def request_frame_for_node(self, **kwargs):
        self.calls.append(kwargs)
        return _stub_decoded_frame()


def _node_ready_for_stub_service(stub) -> VideoFrameNode:
    """A VideoFrameNode wired to the stub service, no file I/O."""
    node = VideoFrameNode()
    node._load_state = VideoLoadState.LOADING
    node._clip_id = "stub-clip"
    node._video_context = stub
    # Keep the test deterministic: no background prefetch thread.
    node._preload_frames = lambda *a, **k: None
    return node


# ── orientation ──────────────────────────────────────────────────────────

@requires_video
def test_gpu_upload_flips_top_down_cpu_frame(gl_ctx):
    """Texture row 0 must be the image bottom (pipeline-wide contract).

    Decoders emit top-down CPU arrays; the composite GL pipeline samples
    bottom-left origin, so the upload boundary must flip once.
    """
    node = VideoFrameNode()
    node.set_parameter("file_path", VIDEO_PATH)
    assert node._open_video(VIDEO_PATH)
    # Export mode decodes synchronously on the calling thread; suppress the
    # background prefetch worker so cleanup can't race an in-flight decode.
    node._preload_frames = lambda *a, **k: None
    cpu = node._decode_frame(0, render_mode='export')
    assert cpu is not None
    with node._cache_lock:
        node._frame_cache[0] = cpu

    outputs = node.evaluate({}, gl_ctx, None, None, render_mode='export')
    assert outputs, "evaluate produced no output"
    tex = node._gpu_texture
    assert tex is not None
    h, w = cpu.shape[:2]
    readback = np.frombuffer(
        tex.read(), dtype=np.float32).reshape(h, w, cpu.shape[2])

    assert np.allclose(np.flipud(readback), cpu, atol=2e-3), \
        "texture rows must be vertically flipped versus the CPU frame"
    # Guard against a double flip sneaking back in.
    assert not np.allclose(readback, cpu, atol=2e-3)
    node.cleanup()


# ── timeline-driven playback ─────────────────────────────────────────────

@requires_video
def test_playhead_advances_frames_during_playback(gl_ctx):
    """Evaluating at increasing comp frames must decode different frames."""
    node = VideoFrameNode()
    node.set_parameter("file_path", VIDEO_PATH)
    assert node._open_video(VIDEO_PATH)
    # Export mode decodes synchronously on the calling thread; suppress the
    # background prefetch worker so cleanup can't race an in-flight decode.
    node._preload_frames = lambda *a, **k: None

    readbacks = []
    for comp_frame in (0, 20, 40):
        node.evaluate(
            {}, CompositeEvalContext(gl=gl_ctx, frame_number=comp_frame),
            None, None, render_mode='export')
        assert node._current_frame == comp_frame
        readback = np.frombuffer(
            node._gpu_texture.read(), dtype=np.float32).reshape(1080, 1920, 4)
        readbacks.append(readback)

    assert not np.array_equal(readbacks[0], readbacks[1]), \
        "frame 20 must differ from frame 0"
    assert not np.array_equal(readbacks[1], readbacks[2]), \
        "frame 40 must differ from frame 20"
    node.cleanup()


@requires_video
def test_playhead_is_offset_by_time_range_comp_start(gl_ctx):
    """A bar starting at comp_start shows source frame (playhead - start)."""
    node = VideoFrameNode()
    node.set_parameter("file_path", VIDEO_PATH)
    assert node._open_video(VIDEO_PATH)
    # Export mode decodes synchronously on the calling thread; suppress the
    # background prefetch worker so cleanup can't race an in-flight decode.
    node._preload_frames = lambda *a, **k: None
    node.time_range = {"comp_start": 100, "comp_end": 200, "enabled": True}

    node.evaluate(
        {}, CompositeEvalContext(gl=gl_ctx, frame_number=105),
        None, None, render_mode='export')
    assert node._current_frame == 5

    node.evaluate(
        {}, CompositeEvalContext(gl=gl_ctx, frame_number=99),
        None, None, render_mode='export')
    # Playhead before the bar clamps to the clip head (local frame 0).
    assert node._current_frame == 0
    node.cleanup()


@requires_video
def test_static_fallback_without_eval_context(gl_ctx):
    """Plain moderngl ctx keeps the legacy static frame_number behaviour."""
    node = VideoFrameNode()
    node.set_parameter("file_path", VIDEO_PATH)
    node.set_parameter("frame_number", 7)
    assert node._open_video(VIDEO_PATH)
    node._preload_frames = lambda *a, **k: None

    node.evaluate({}, gl_ctx, None, None, render_mode='export')
    assert node._current_frame == 7
    node.cleanup()


def test_service_receives_playhead_and_source_in(gl_ctx):
    """The stub service must get the timeline-local frame + source_in."""
    stub = _StubVideoContext()
    node = _node_ready_for_stub_service(stub)
    node.set_parameter("source_in", 5)

    node.evaluate(
        {}, CompositeEvalContext(gl=gl_ctx, frame_number=3),
        None, None, render_mode='export')
    assert node._current_frame == 3
    assert stub.calls, "service was never asked for a frame"
    last = stub.calls[-1]
    assert last["timeline_frame"] == 3
    assert last["source_in"] == 5

    # With a trimmed bar the local frame is playhead - comp_start.
    node.time_range = {"comp_start": 10, "comp_end": 50, "enabled": True}
    node.evaluate(
        {}, CompositeEvalContext(gl=gl_ctx, frame_number=12),
        None, None, render_mode='export')
    assert node._current_frame == 2
    assert stub.calls[-1]["timeline_frame"] == 2
    assert stub.calls[-1]["source_in"] == 5


# ── fallback decode path honours the source range ────────────────────────

class _RecordingSource:
    def __init__(self):
        self.reads = []

    def read_frame(self, index):
        self.reads.append(index)
        return _stub_decoded_frame()


def _fallback_node(source_in=0, source_out=-1, hold=False, loop=False):
    node = VideoFrameNode()
    node._use_service = False
    node._source = _RecordingSource()
    node.set_parameter("total_frames", 100)
    node.set_parameter("source_in", source_in)
    node.set_parameter("source_out", source_out)
    node.set_parameter("hold", hold)
    node.set_parameter("loop", loop)
    return node


def test_fallback_decode_applies_source_in_offset():
    node = _fallback_node(source_in=5)
    frame = node._decode_frame(3)
    assert frame is not None
    assert node._source.reads == [8]  # 3 + source_in


def test_fallback_decode_returns_none_beyond_source_out():
    node = _fallback_node(source_in=5, source_out=10)
    assert node._decode_frame(20) is None
    assert node._source.reads == []


def test_fallback_decode_holds_last_frame():
    node = _fallback_node(source_in=5, source_out=10, hold=True)
    assert node._decode_frame(20) is not None
    assert node._source.reads == [10]


def test_fallback_decode_loops_within_range():
    # range [5, 9]; requested 12+5=17 -> offset (17-9-1) % 5 = 2 -> 5+2=7.
    node = _fallback_node(source_in=5, source_out=9, loop=True)
    assert node._decode_frame(12) is not None
    assert node._source.reads == [7]


# ── Keyframes trim/slide -> video source params ──────────────────────────

def _graph_with_video_layer():
    g = NodeGraph()
    video = VideoFrameNode(node_id="vid")
    video.set_parameter("total_frames", 500)
    video.set_parameter("source_in", 0)
    video.set_parameter("source_out", -1)
    root = TransformNode(node_id="root")
    g.add_node(video)
    g.add_node(root)
    layers_map = {"l1": Layer(
        layer_id="l1", name="Video", root_node_id="root",
        source_node_id="vid")}
    return g, video, root, layers_map


def test_trim_in_advances_source_in():
    g, video, root, layers = _graph_with_video_layer()
    result = apply_video_range_edit(root, g, layers, {
        "comp_start": 30, "comp_end": 499, "enabled": True, "edit": "trim_in"})
    assert result is not None
    assert video.parameters["source_in"] == 30
    assert result["before"] == {"source_in": 0}
    assert result["after"] == {"source_in": 30}


def test_trim_in_clamps_at_source_head():
    g, video, root, layers = _graph_with_video_layer()
    video.set_parameter("source_in", 2)
    root.time_range = {"comp_start": 10, "comp_end": 40, "enabled": True}
    result = apply_video_range_edit(root, g, layers, {
        "comp_start": 5, "comp_end": 40, "enabled": True, "edit": "trim_in"})
    assert result is not None
    assert video.parameters["source_in"] == 0  # clamped, not negative


def test_trim_out_derives_source_out_from_duration():
    g, video, root, layers = _graph_with_video_layer()
    video.set_parameter("source_in", 10)
    root.time_range = {"comp_start": 10, "comp_end": 40, "enabled": True}
    result = apply_video_range_edit(root, g, layers, {
        "comp_start": 10, "comp_end": 25, "enabled": True, "edit": "trim_out"})
    assert result is not None
    # duration 16 frames starting at source_in 10
    assert video.parameters["source_out"] == 25


def test_slide_keeps_source_params():
    g, video, root, layers = _graph_with_video_layer()
    video.set_parameter("source_in", 7)
    result = apply_video_range_edit(root, g, layers, {
        "comp_start": 50, "comp_end": 90, "enabled": True, "edit": "slide"})
    assert result is None
    assert video.parameters["source_in"] == 7


def test_range_edit_on_video_node_itself():
    """Node-mode rows pass the video_frame node directly (no layer map)."""
    g, video, root, layers = _graph_with_video_layer()
    result = apply_video_range_edit(video, g, None, {
        "comp_start": 12, "comp_end": 60, "enabled": True, "edit": "trim_in"})
    assert result is not None
    assert video.parameters["source_in"] == 12


def test_range_edit_ignores_non_video_layers():
    g, video, root, layers = _graph_with_video_layer()
    layers["l1"].source_node_id = root.id  # root is a Transform, not video
    result = apply_video_range_edit(root, g, layers, {
        "comp_start": 5, "comp_end": 9, "enabled": True, "edit": "trim_in"})
    assert result is None


def test_legacy_payload_without_edit_kind_keeps_params():
    """Payloads without an `edit` key (old callers) must not move source_in."""
    g, video, root, layers = _graph_with_video_layer()
    result = apply_video_range_edit(root, g, layers, {
        "comp_start": 30, "comp_end": 99, "enabled": True})
    assert result is None
    assert video.parameters["source_in"] == 0


# ── layer-mode bar carries the source media length ──────────────────────

def test_layer_bar_uses_video_source_length():
    from src.features.composite.ui.keyframes_model import KeyframesModel

    g, video, root, layers = _graph_with_video_layer()
    video.set_parameter("total_frames", 500)

    class _Stack:
        layer_ids = ["l1"]
        generated_merge_node_ids = []

    controller = SimpleNamespace(
        graph=g, layers_map=lambda: layers, main_stack=lambda: _Stack())

    m = KeyframesModel()
    m.set_comp_range(0, 99)
    m.rebuild_layers(controller)
    track = m.track_for("layer:l1")
    assert track is not None and track.range_bar is not None
    # The bar lives on the root node but spans the video source length.
    assert (track.range_bar.start_frame, track.range_bar.end_frame) == (0, 499)
    assert track.source_total == 500
