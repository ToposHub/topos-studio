"""Real GL pixels for the shared Edit/Deliver graph execution path."""
import numpy as np
import pytest

from src.features.color.core.color_graph_cpu import run_graph_frame_cpu
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_texture import ColorGraphTextureRenderer
from src.features.color.core.grade_keyframes import GradeKeyframe, GradeKeyframeTrack
from tests.color.fixtures.color_graph import _neutral_layer
from tests.color.test_color_graph_migration import _stack_with_layers


def animated_graph(*, matte=False):
    a, b = _neutral_layer("Exposure"), _neutral_layer("Contrast")
    a.grade_keyframes["exposure"] = GradeKeyframeTrack("exposure", [GradeKeyframe(0, 0.0), GradeKeyframe(20, 30.0)])
    b.grade_keyframes["contrast"] = GradeKeyframeTrack("contrast", [GradeKeyframe(0, 5.0), GradeKeyframe(20, 40.0)])
    if matte:
        from src.features.color.core.layer import MaskShape
        shape = MaskShape(MaskShape.CIRCLE)
        shape.feather = 0.1
        a.mask_shapes = [shape]
        a.qualifier_params.update({"enabled": True, "lum_low": 0.15, "lum_high": 0.8})
        a.grade_keyframes["qualifier.lum_low"] = GradeKeyframeTrack("qualifier.lum_low", [GradeKeyframe(0, 0.1), GradeKeyframe(20, 0.4)])
    return migrate_legacy_stack(_stack_with_layers([a, b]))


@pytest.fixture
def ctx():
    import moderngl
    context = moderngl.create_standalone_context(require=330)
    yield context
    context.release()


@pytest.mark.parametrize("matte", [False, True])
def test_shared_graph_gl_matches_cpu_for_both_animated_nodes(ctx, matte):
    graph = animated_graph(matte=matte)
    rgb = np.linspace(0.05, 0.8, 16 * 8 * 3, dtype="f4").reshape(8, 16, 3)
    source = ctx.texture((16, 8), 3, rgb.tobytes(), dtype="f4")
    renderer = ColorGraphTextureRenderer(ctx)
    try:
        results = []
        first = None
        for i, frame in enumerate((0, 10, 20)):
            tex = renderer.render(source, graph, frame, output_key=str(i % 2))
            gl = np.frombuffer(tex.read(), dtype="f4").reshape(8, 16, 4)[..., :3].copy()
            cpu = run_graph_frame_cpu(graph, rgb[..., ::-1], frame)[..., ::-1]
            np.testing.assert_allclose(gl, cpu, atol=1 / 255, rtol=0.002)
            if i == 0:
                first = tex
            elif i == 1:
                np.testing.assert_array_equal(np.frombuffer(first.read(), dtype="f4").reshape(8, 16, 4)[..., :3], results[0])
            results.append(gl)
        assert not np.allclose(results[0], results[-1])
        assert len(renderer.plan_cache) == 1
    finally:
        renderer.release()
        source.release()


def test_edit_preview_packet_and_real_graph_pass(ctx):
    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
    from src.features.edit.core.project import ColorStackData
    from tests.edit.test_color_stack_preview_grade import _worker_for_stack, _layer
    graph = animated_graph()
    stack = ColorStackData(stack_id="s", color_graph=graph)
    worker = _worker_for_stack(stack)
    source = ctx.texture((4, 4), 4, np.full((4, 4, 4), 0.25, "f4").tobytes(), dtype="f4")
    target = ctx.texture((4, 4), 4, dtype="f1")
    fbo = ctx.framebuffer([target])
    renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
    from PyQt6.QtCore import QThread
    QThread.__init__(renderer)
    renderer.ctx = ctx
    renderer._grade_program = object()
    renderer._grade_vao = object()
    renderer._ensure_graded_track_fbo = lambda *_: (fbo, target)
    try:
        layer = _layer("s")
        layer.source_frame = 10
        packet = worker._get_preview_color_grade(layer)
        assert packet["color_graph"] is graph
        assert not worker._is_identity_layer(layer)
        result = renderer._apply_preview_grade(source, 0, 4, 4, packet)
        actual = np.frombuffer(result.read(), dtype=np.uint8).reshape(4, 4, 4)[..., :3]
        cpu = run_graph_frame_cpu(graph, np.full((4, 4, 3), 0.25, "f4"), 10)
        np.testing.assert_allclose(actual / 255, cpu[..., ::-1], atol=2 / 255)
    finally:
        graph_renderer = getattr(renderer, "_color_graph_texture_renderer", None)
        if graph_renderer is not None:
            graph_renderer.release()
        worker._decode_executor.shutdown(wait=True)
        fbo.release()
        target.release()
        source.release()


@pytest.mark.parametrize("matte", [False, True])
def test_repeated_graph_frames_after_caller_releases_bound_fbo_have_no_gl_errors(ctx, matte):
    from OpenGL.GL import GL_FRAMEBUFFER_BINDING, glGetIntegerv
    graph = animated_graph(matte=matte)
    source = ctx.texture((16, 8), 3, np.full((8, 16, 3), 0.25, "f4").tobytes(), dtype="f4")
    scratch = ctx.texture((16, 8), 4, dtype="f4")
    renderer = ColorGraphTextureRenderer(ctx)
    try:
        for frame in (0, 10, 20, 10, 0, 20, 10, 0):
            caller = ctx.framebuffer([scratch])
            caller.use()
            caller.release()
            assert ctx.error == "GL_NO_ERROR"
            result = renderer.render(source, graph, frame)
            assert ctx.error == "GL_NO_ERROR", f"graph frame {frame} leaked a GL error"
            # This is exactly the PyOpenGL query that previously reported 1282.
            assert int(glGetIntegerv(GL_FRAMEBUFFER_BINDING)) == ctx.fbo.glo
            output = np.frombuffer(result.read(), dtype="f4").reshape(8, 16, 4)[..., :3]
            expected = run_graph_frame_cpu(graph, np.full((8, 16, 3), 0.25, "f4"), frame)
            np.testing.assert_allclose(output, expected, atol=1 / 255)
    finally:
        renderer.release()
        scratch.release()
        source.release()


def test_snapshot_restores_target_viewport_and_scissor_enable_state(ctx):
    from OpenGL.GL import glIsEnabled, GL_SCISSOR_TEST
    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
    renderer = EditGpuPreviewRenderer(main_context=None)
    renderer.ctx = ctx
    texture = ctx.texture((32, 16), 4, np.full((16, 32, 4), 0.25, "f4").tobytes(), dtype="f4")
    target = ctx.texture((32, 16), 4, dtype="f4")
    caller = ctx.framebuffer([target])
    captures = []
    renderer.snapshot_ready.connect(lambda frame, generation, image: captures.append(image))
    try:
        for scissor in (None, (1, 2, 7, 4)):
            caller.use()
            ctx.viewport = (2, 1, 20, 10)
            ctx.scissor = scissor
            renderer._capture_frame_snapshot(texture, 32, 16, 20, 1)
            assert len(captures) > 0
            assert captures[-1].pixelColor(100, 100).red() == 64
            assert ctx.fbo is caller and ctx.viewport == (2, 1, 20, 10)
            assert bool(glIsEnabled(GL_SCISSOR_TEST)) == (scissor is not None)
            if scissor is not None:
                assert ctx.scissor == scissor
            assert ctx.error == "GL_NO_ERROR"
    finally:
        ctx.scissor = None
        ctx.detect_framebuffer(0).use()
        for resource in (renderer._snapshot_vao, renderer._snapshot_program, renderer._snapshot_fbo, renderer._snapshot_tex,
                         caller, target, texture):
            if resource is not None:
                resource.release()


def test_optional_snapshot_allocation_failure_does_not_raise_or_emit_error(ctx, monkeypatch):
    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
    renderer = EditGpuPreviewRenderer(main_context=None)
    renderer.ctx = ctx
    errors = []
    renderer.render_error.connect(lambda frame, message: errors.append(message))
    texture = ctx.texture((4, 4), 4, dtype="f4")
    try:
        monkeypatch.setattr(ctx, "texture", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("snapshot allocation failed")))
        renderer._capture_frame_snapshot(texture, 4, 4, 112, 1)
        assert not errors
        assert ctx.error == "GL_NO_ERROR"
    finally:
        texture.release()


def test_snapshot_pair_allocation_failure_can_recover_on_next_frame(ctx, monkeypatch):
    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
    renderer = EditGpuPreviewRenderer(main_context=None)
    renderer.ctx = ctx
    source = ctx.texture((4, 4), 4, np.full((4, 4, 4), 0.25, "f4").tobytes(), dtype="f4")
    framebuffer = ctx.framebuffer
    captures = []
    renderer.snapshot_ready.connect(lambda frame, generation, image: captures.append(image))
    try:
        monkeypatch.setattr(ctx, "framebuffer", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("snapshot target failed")))
        renderer._capture_frame_snapshot(source, 4, 4, 112, 1)
        assert renderer._snapshot_tex is None and renderer._snapshot_fbo is None
        monkeypatch.setattr(ctx, "framebuffer", framebuffer)
        renderer._capture_frame_snapshot(source, 4, 4, 113, 1)
        assert len(captures) == 1 and captures[0].pixelColor(100, 100).red() == 64
        assert ctx.error == "GL_NO_ERROR"
    finally:
        ctx.detect_framebuffer(0).use()
        for resource in (renderer._snapshot_vao, renderer._snapshot_program, renderer._snapshot_fbo, renderer._snapshot_tex, source):
            if resource is not None:
                resource.release()


@pytest.mark.parametrize("kind", ["curve", "brush"])
def test_production_window_shapes_animate_in_shared_gl_and_cpu(ctx, kind):
    from src.features.color.core.layer import MaskShape
    layer = _neutral_layer("Window")
    layer.grading_params["exposure"] = 30.0
    shape = MaskShape(kind)
    shape.feather = 0.0
    if kind == "curve":
        shape.points = [{"pos": p, "c1": p, "c2": p} for p in
                        ((0.1, 0.1), (0.8, 0.1), (0.8, 0.8), (0.1, 0.8))]
    else:
        shape.brush_strokes = [{"points": [(0.1, 0.2), (0.7, 0.8)], "size": 3}]
    layer.mask_shapes = [shape]
    name = f"mask.{shape.id}.opacity"
    layer.grade_keyframes[name] = GradeKeyframeTrack(name, [GradeKeyframe(0, 0.0), GradeKeyframe(20, 1.0)])
    graph = migrate_legacy_stack(_stack_with_layers([layer]))
    rgb = np.full((24, 32, 3), 0.25, "f4")
    source = ctx.texture((32, 24), 3, rgb.tobytes(), dtype="f4")
    renderer = ColorGraphTextureRenderer(ctx)
    try:
        outputs = []
        for frame in (0, 10, 20):
            texture = renderer.render(source, graph, frame)
            actual = np.frombuffer(texture.read(), dtype="f4").reshape(24, 32, 4)[..., :3]
            expected = run_graph_frame_cpu(graph, rgb, frame)
            np.testing.assert_allclose(actual, expected, atol=1 / 255)
            outputs.append(actual.copy())
        np.testing.assert_allclose(outputs[0], rgb, atol=1e-5)
        assert np.max(outputs[-1] - outputs[0]) > 0.05
    finally:
        renderer.release()
        source.release()


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_deliver_runs_the_same_graph_instead_of_flattened_grade(dtype):
    from src.features.edit.core.timeline_gpu_export_renderer import TimelineGpuExportRenderer
    graph = animated_graph()
    renderer = TimelineGpuExportRenderer()
    frame = np.full((8, 16, 3), np.iinfo(dtype).max // 4, dtype=dtype)
    try:
        output = renderer.render_packed_bgr(frame, {"color_graph": graph, "source_frame": 10}, 16, 8, 1.0, output_u16=dtype == np.uint16)
        assert output is not None
        cpu = run_graph_frame_cpu(graph, frame, 10)
        np.testing.assert_allclose(output.astype(float) / np.iinfo(dtype).max,
                                   cpu.astype(float) / np.iinfo(dtype).max, atol=2 / 255)
    finally:
        renderer.release()


def test_graph_failure_does_not_publish_and_renderer_can_recover(ctx):
    from dataclasses import replace
    from src.shared.project.color_graph import freeze_params
    graph = animated_graph(matte=True)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    bad = replace(node, params=freeze_params({**dict(node.params), "qualifier": {"enabled": True, "blur": 1.0}}))
    bad_graph = replace(graph, nodes=tuple(bad if n.node_id == node.node_id else n for n in graph.nodes))
    source = ctx.texture((4, 4), 3, np.full((4, 4, 3), 0.2, "f4").tobytes(), dtype="f4")
    renderer = ColorGraphTextureRenderer(ctx)
    try:
        with pytest.raises(RuntimeError, match="qualifier_postprocess"):
            renderer.render(source, bad_graph, 10)
        assert not renderer.executor._published
        assert renderer.render(source, graph, 10) is not None
    finally:
        renderer.release()
        source.release()


@pytest.mark.parametrize("fail", [False, True])
def test_color_graph_matte_uses_evaluated_windows_and_restores_legacy_texture(ctx, fail):
    from PyQt6.QtCore import QThread
    from src.features.color.core.async_renderer import AsyncRenderer
    from src.features.color.core.layer import MaskShape
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    QThread.__init__(renderer)
    renderer.ctx = ctx
    renderer.pyramid_textures = []
    renderer.video_source_size = (32, 24)
    original = {"mask": object(), "curves": object()}
    renderer.layer_textures = {"member": original}
    shape = MaskShape("rect")
    shape.feather = 0.0
    shape.opacity = 0.25  # Evaluated graph payload, unrelated to old uploaded mask.
    seen = []

    def consume(layer, *_args):
        texture = renderer.layer_textures["member"]["mask"]
        assert texture is not original["mask"]
        array = np.frombuffer(texture.read(), dtype="f4").reshape(24, 32)
        assert array.max() == pytest.approx(0.25)
        seen.append(texture)
        if fail:
            raise RuntimeError("matte shader failed")
        return "combined-matte"

    renderer._generate_matte = consume
    payload = {"id": "member", "mask_shapes": [shape.to_dict()]}
    if fail:
        with pytest.raises(RuntimeError, match="matte shader failed"):
            renderer._generate_graph_matte(payload, None, 32, 24, (0, 0, 32, 24))
    else:
        assert renderer._generate_graph_matte(payload, None, 32, 24, (0, 0, 32, 24)) == "combined-matte"
    assert renderer.layer_textures["member"] is original
    assert len(seen) == 1
    assert type(seen[0].mglo).__name__ == "InvalidObject"


def test_cpu_graph_rejects_advanced_chain_instead_of_dropping_it():
    from src.features.color.core.color_graph_cpu import UnsupportedNode
    with pytest.raises(UnsupportedNode, match="Advanced Color chain"):
        run_graph_frame_cpu(animated_graph(), np.full((4, 4, 3), 0.25, "f4"), 10,
                            post_chain=[{"_unsupported_advanced": True, "_chain_segment": "timeline"}])


def test_deliver_releases_uploaded_source_when_graph_renderer_init_fails(monkeypatch):
    from src.features.color.core import color_graph_texture
    from src.features.edit.core.timeline_gpu_export_renderer import TimelineGpuExportRenderer
    renderer = TimelineGpuExportRenderer()
    released = []
    try:
        assert renderer._ensure_ready()
        original_texture = renderer._ctx.texture

        def tracked_texture(*args, **kwargs):
            texture = original_texture(*args, **kwargs)
            release = texture.release
            state = {"released": False}
            released.append(state)

            def tracked_release():
                state["released"] = True
                release()

            texture.release = tracked_release
            return texture

        def fail_init(*_args):
            raise RuntimeError("graph context initialization failed")

        monkeypatch.setattr(renderer._ctx, "texture", tracked_texture)
        monkeypatch.setattr(color_graph_texture, "ColorGraphTextureRenderer", fail_init)
        with pytest.raises(RuntimeError, match="initialization failed"):
            renderer._prepare_layer_texture(np.full((4, 4, 3), 64, np.uint8),
                {"color_graph": animated_graph(), "source_frame": 10}, False)
        assert released and all(item["released"] for item in released)
    finally:
        renderer.release()
