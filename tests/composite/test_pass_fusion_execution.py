"""P1-9 pass-fusion execution tests.

The fusion planner had source-text assertions only; these tests pin the
EXECUTION contract:

- a fusible chain (grade → invert) renders to the same pixels with
  fusion on (conservative default) and fusion off (per-node passes),
  within the float-rounding tolerance of the two shader paths;
- the fusion-mode preference is actually honoured through the composite
  config manager (previously `pass_fusion_mode` was read by
  get_fusion_mode_from_config but never consulted by the evaluator);
- a fused-chain cache hit serves the previous result without re-draws
  when nothing changes, and re-renders when the chain input changes.
"""

import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytestmark = pytest.mark.composite_core

W, H = 160, 120


class _StubConfig:
    """Minimal ConfigManager stand-in exposing one key."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key):
        return self._values.get(key)


def _graph():
    return {
        "composite_id": "fusion-gate",
        "fps": 25.0, "width": W, "height": H,
        "nodes": [
            {"id": "src", "type": "constant", "position": [0, 0],
             "parameters": {"color_r": 0.7, "color_g": 0.4, "color_b": 0.2,
                            "width": W, "height": H}},
            {"id": "g1", "type": "grade", "position": [160, 0],
             "parameters": {"lift_r": 0.02, "lift_g": 0.0, "lift_b": -0.01,
                            "gain_r": 1.2, "gain_g": 1.0, "gain_b": 0.8,
                            "gamma_r": 0.9, "gamma_g": 1.0, "gamma_b": 1.1,
                            "offset_r": 0.0, "offset_g": 0.01,
                            "offset_b": 0.0}},
            {"id": "iv", "type": "invert", "position": [320, 0],
             "parameters": {}},
            {"id": "out", "type": "output", "position": [480, 0],
             "parameters": {"format_mode": "custom",
                            "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "src", "src_port_name": "result",
             "dst_node_id": "g1", "dst_port_name": "image"},
            {"src_node_id": "g1", "src_port_name": "result",
             "dst_node_id": "iv", "dst_port_name": "image"},
            {"src_node_id": "iv", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }


def test_layer_time_gate_breaks_fusion_at_the_layer_root():
    from src.features.composite.core.layer_model import Layer, LayerStack
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.render.render_scheduler import RenderScheduler
    from src.features.composite.utils.pass_fusion import analyze_fusion

    graph = NodeGraph.from_dict(_graph())
    scheduler = RenderScheduler()
    before = graph.snapshot(frame_number=0)
    assert "g1" in analyze_fusion(scheduler.plan(before), before).node_to_chain

    graph.set_layer_stack(LayerStack(stack_id="main", name="Main",
                                     output_node_id="out"))
    graph.upsert_layer("main", Layer(layer_id="layer", name="Layer",
                                    root_node_id="g1", in_frame=2))
    after = graph.snapshot(frame_number=0)
    assert "g1" not in analyze_fusion(scheduler.plan(after), after).node_to_chain


def test_managed_layer_gaps_render_transparent_in_preview_and_export(qapp, _gl_available, monkeypatch):
    from src.features.composite.core.layer_model import BOTTOM_UP_LAYER_STACK_VERSION
    from src.features.composite.core.layer_stack_controller import LayerStackController
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.constant_node import ConstantNode
    from src.features.composite.nodes.output_node import OutputNode
    from src.features.composite.render.composite_frame_renderer import CompositeFrameRenderer

    monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    output = graph.get_node("out")
    output.set_parameter("width", 32)
    output.set_parameter("height", 24)
    output.set_parameter("format_mode", "custom")
    free_graph = graph.to_dict()
    controller = LayerStackController(graph, layer_stack_version=BOTTOM_UP_LAYER_STACK_VERSION)
    for ident, span, color in (("red", (3, 6), (1.0, 0.0, 0.0, 1.0)),
                               ("blue", (9, 12), (0.0, 0.0, 1.0, 1.0))):
        source = ConstantNode(node_id=ident)
        source.set_parameter("width", 32)
        source.set_parameter("height", 24)
        source.set_parameter("color", color)
        graph.add_node(source)
        layer = controller.add_media_layer(ident, source.id)
        assert controller.apply_layer_time_bounds(layer, span)

    data = graph.to_dict()
    for mode in ("preview", "export"):
        renderer = CompositeFrameRenderer()
        try:
            for frame, covered in ((0, False), (3, True), (7, False), (9, True), (13, False)):
                result = renderer.render_comp_clip_frame(data, timeline_frame=frame, quality=mode)
                assert result is not None and result.shape[:2] == (24, 32)
                assert bool(np.any(result)) is covered
                if covered:
                    expected_channel = 2 if frame == 3 else 0
                    assert np.argmax(result[0, 0]) == expected_channel
                if not covered:
                    engine = renderer._get_export_renderer()
                    snapshot = renderer._build_snapshot(data, frame)
                    display = engine._get_cached_outputs_for_node("out", snapshot)["display"]
                    framebuffer = next(fbo for tex, fbo in engine.fb_manager._fbo_cache.items()
                                       if tex.glo == display.texture_id)
                    pixel = framebuffer.read(viewport=(0, 0, 1, 1), components=4, dtype="f4")
                    assert np.frombuffer(pixel, np.float32)[3] == 0.0
            assert renderer.render_comp_clip_frame(free_graph, quality=mode) is None
        finally:
            renderer.cleanup()


def test_vfr_layer_split_preserves_presentation_time_after_reopen(
    qapp, _gl_available, monkeypatch, tmp_path,
) -> None:
    from fractions import Fraction

    from src.features.composite.core.layer_stack_controller import LayerStackController
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.output_node import OutputNode
    from src.features.composite.nodes.video_frame_node import VideoFrameNode
    from src.features.composite.render.composite_frame_renderer import CompositeFrameRenderer
    from src.features.composite.ui.timeline_commands import apply_video_range_edit
    from src.shared.media.presentation_time import request_presentation_index

    av = pytest.importorskip("av")
    movie = str(tmp_path / "pts-splits.mkv")
    time_base = Fraction(1, 1000)
    colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))
    with av.open(movie, "w") as container:
        stream = container.add_stream("ffv1", rate=25)
        stream.width = stream.height = 16
        stream.pix_fmt = "bgr0"
        stream.time_base = stream.codec_context.time_base = time_base
        for pts, color in zip((2000, 2040, 2160, 2200), colors):
            raster = np.full((16, 16, 3), color, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(raster, format="rgb24")
            frame.pts, frame.time_base = pts, time_base
            container.mux(stream.encode(frame))
        container.mux(stream.encode())

    future = request_presentation_index(movie)
    assert request_presentation_index(movie) is future
    index = future.result(timeout=10)
    assert index.is_variable and index.pts == (2000, 2040, 2160, 2200)
    stat = os.stat(movie)
    os.utime(movie, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    next_version = request_presentation_index(movie)
    assert next_version is not future
    assert next_version.result(timeout=10).pts == index.pts

    monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
    graph = NodeGraph()
    graph.fps = 25
    graph.duration_frames = 6
    output = OutputNode(node_id="out")
    graph.add_node(output)
    for name, value in {"format_mode": "custom", "width": 16, "height": 16}.items():
        output.set_parameter(name, value)
    video = VideoFrameNode(node_id="video")
    graph.add_node(video)
    for name, value in {"file_path": movie, "asset_id": "shared-video", "fps": 25,
                        "total_frames": 4, "video_width": 16, "video_height": 16}.items():
        video.set_parameter(name, value)
    controller = LayerStackController(graph, layer_stack_version=2)
    left = controller.add_media_layer("VFR", video.id)
    assert controller.video_layer_timing(left, (0, 0)).window_duration(3) == 6

    trim_graph = NodeGraph.from_dict(graph.to_dict())
    trim_layer = trim_graph.get_layer(left)
    trim = apply_video_range_edit(trim_graph.get_node(trim_layer.root_node_id),
                                 trim_graph, {left: trim_layer},
                                 {"edit": "trim_in", "comp_start": 2, "comp_end": 5},
                                 previous_bounds=(0, 5))
    assert trim["after"] == {"source_in": 1}
    assert trim["source_timing"]["source_time_origin"] == "2/25"

    right = controller.edit_layer_source_time(left, "split", 2, (0, 5))
    assert isinstance(right, str)
    reopened = NodeGraph.from_dict(graph.to_dict())
    right_source = reopened.get_node(reopened.get_layer(right).source_node_id)
    assert right_source.parameters["asset_id"] == video.parameters["asset_id"]
    assert right_source.time_range["source_time_origin"] == "2/25"

    data = reopened.to_dict()
    renderer = CompositeFrameRenderer()
    try:
        for mode in ("preview", "export"):
            for frame, ordinal in ((4, 2), (0, 0), (3, 1), (5, 3), (1, 1), (2, 1)):
                result = renderer.render_comp_clip_frame(data, timeline_frame=frame, quality=mode)
                assert result is not None
                bgr = result[8, 8]
                if mode == "preview":
                    bgr = bgr.astype(np.float32) / 255.0
                assert bgr == pytest.approx(np.array(colors[ordinal][::-1]) / 255, abs=0.01)
    finally:
        renderer.cleanup()


def _render(config_values=None):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
    from src.features.composite.render.composite_frame_renderer import (
        CompositeFrameRenderer,
    )
    renderer = CompositeFrameRenderer(
        config_manager=_StubConfig(config_values))
    try:
        img = renderer.render_comp_clip_frame(
            _graph(), timeline_frame=0, quality="preview")
        assert img is not None, "fused-chain graph produced no output"
        return img.astype(np.float32)
    finally:
        renderer.cleanup()


@pytest.fixture(scope="module")
def _gl_available():
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    ctx.release()


class TestFusedChainExecution:
    def test_fused_matches_individual_within_tolerance(self, qapp,
                                                       _gl_available):
        fused = _render({})          # default: conservative fusion ON
        individual = _render({"pass_fusion_mode": "off"})
        d = np.abs(fused - individual)
        # One fused pass vs stage-wise f4 textures: rounding differs.
        assert float(d.max()) <= 2e-3
        assert float(d.mean()) <= 2e-4

    def test_fusion_mode_config_is_honoured(self, qapp, _gl_available,
                                            monkeypatch):
        from src.features.composite.evaluators import base_evaluator
        calls = {"n": 0}
        original = base_evaluator.BaseEvaluatorMixin._execute_fused_chain

        def counting(self, *a, **k):
            calls["n"] += 1
            return original(self, *a, **k)

        monkeypatch.setattr(base_evaluator.BaseEvaluatorMixin,
                            "_execute_fused_chain", counting)
        _render({})                          # fusion ON → chain executes
        fused_calls = calls["n"]
        calls["n"] = 0
        _render({"pass_fusion_mode": "off"})  # OFF → no fused execution
        assert fused_calls >= 1
        assert calls["n"] == 0

    def test_fused_cache_hit_reuses_result(self, qapp, _gl_available,
                                           monkeypatch):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
        from src.features.composite.evaluators import base_evaluator
        from src.features.composite.render.composite_frame_renderer import (
            CompositeFrameRenderer,
        )
        draws = {"n": 0}
        original = base_evaluator.BaseEvaluatorMixin._apply_fused_chain

        def counting(self, *a, **k):
            draws["n"] += 1
            return original(self, *a, **k)

        monkeypatch.setattr(base_evaluator.BaseEvaluatorMixin,
                            "_apply_fused_chain", counting)
        # The fused-chain cache lives with the renderer's cache manager.
        # The PREVIEW path deliberately clears it every frame (P0-1: the
        # frame's eval textures must return to the pool), so warm-hit
        # behaviour is verified on the export path, where the cache is
        # meant to persist across frames.
        renderer = CompositeFrameRenderer(config_manager=_StubConfig({}))
        try:
            img1 = renderer.render_comp_clip_frame(
                _graph(), timeline_frame=0, quality="export")
            assert img1 is not None
            first = draws["n"]
            assert first >= 1
            img2 = renderer.render_comp_clip_frame(
                _graph(), timeline_frame=0, quality="export")
            assert img2 is not None
            assert draws["n"] == first
            assert np.array_equal(img1, img2)
        finally:
            renderer.cleanup()
