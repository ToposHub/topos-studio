"""Real-footage full-node sweep regression tests (2026-09-09 整体复查).

The sweep drove every registered composite node type through the export
renderer with real footage from tests/test_videos (stage 1: defaults at
2K, stage 2: every enum option at 720p, stage 3: an 11-codec matrix). The
tests below pin the defects it uncovered:

- dispatch gate: generator / multi-input effect markers never reached
  their handlers (noise, ramp, keyer, difference_keyer, channel_merge);
- port-name normalisation: nodes whose declared output port differs from
  the dispatcher's ``"result"`` key silently lost their downstream edge
  (sharpen, erode_dilate, AI effects, denoise passthrough);
- ramp shader: evaluator sets ``u_resolution`` but the shader never
  declared it (KeyError → dead node);
- edge detection: the shader had no canny branch (gradient stayed 0 →
  always black) and direction mode hit atan(0,0) = NaN on flat regions;
- volume3d: Beer-Lambert colouring broadcast (3,) against (h, w) → crash;
- headless GL: raw-name texture binding went through
  QOpenGLFunctions_4_1_Core, which segfaults inside
  initializeOpenGLFunctions() without a Qt GL context — now routed via
  core/gl_bind (PyOpenGL) and the CLI renders headless;
- video fallback safety: the background preload worker must never decode
  through a direct (non-service) FFmpegSource concurrently with the
  render thread (libav native abort).
"""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.nodes.base_node import TextureResult

pytestmark = pytest.mark.composite_core

_W, _H = 64, 48

FIXTURE_DIR = os.path.join(
    os.path.dirname(__file__), '..', 'test_videos')


def _make_ctx():
    try:
        import moderngl
        return moderngl.create_standalone_context()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no GL: {exc}")


def _upload_rgba(harness, rgba):
    """Upload a top-down RGBA array through the harness pool."""
    h, w = rgba.shape[:2]
    tex = harness._resource_pool.acquire_texture(w, h)
    tex.write(np.ascontiguousarray(np.flipud(rgba)).tobytes())
    return TextureResult(
        texture_id=tex.glo, width=w, height=h,
        bbox=BBox.full_frame(w, h),
        alpha_premultiplied=False, color_space="linear")


def _make_harness(ctx, extra_mixins=()):
    from src.features.composite.evaluators.base_evaluator import (
        BaseEvaluatorMixin,
    )
    from src.features.composite.evaluators.channel_evaluator import (
        ChannelEvaluatorMixin,
    )
    from src.features.composite.evaluators.color_evaluator import (
        ColorEvaluatorMixin,
    )
    from src.features.composite.evaluators.filter_evaluator import (
        FilterEvaluatorMixin,
    )
    from src.features.composite.evaluators.keyer_evaluator import (
        KeyerEvaluatorMixin,
    )
    from src.features.composite.evaluators.merge_evaluator import (
        MergeEvaluatorMixin,
    )
    from src.shared.gpu.gl_backend import FramebufferManager

    class _Pool:
        def __init__(self, ctx):
            self.ctx = ctx
            self._pooled_textures = {}

        def acquire_texture(self, w, h, components=4, dtype="f4"):
            tex = self.ctx.texture((w, h), components, dtype=dtype)
            self._pooled_textures.setdefault(dtype, []).append(tex)
            return tex

    class _Harness(BaseEvaluatorMixin, ColorEvaluatorMixin,
                   FilterEvaluatorMixin, KeyerEvaluatorMixin,
                   MergeEvaluatorMixin, ChannelEvaluatorMixin,
                   *extra_mixins):
        def __init__(self, ctx):
            self.ctx = ctx
            self.utility_shaders = {}
            self._resource_pool = _Pool(ctx)
            self.fb_manager = FramebufferManager(ctx)
            self._load_shaders()

        def _bind_texture_unit(self, texture_id, unit):
            # Same contract as the production implementation (core/gl_bind):
            # bind by raw name against the current context.
            from src.features.composite.core.gl_bind import bind_texture_unit
            bind_texture_unit(texture_id, unit)

    return _Harness(ctx)


def _marker_input(harness, value=0.5):
    rgba = np.full((_H, _W, 4), value, dtype=np.float32)
    rgba[:, :, 3] = 1.0
    return _upload_rgba(harness, rgba)


# ── dispatch gate: generators + multi-input effects ─────────────────────────


class TestGeneratorDispatch:
    def test_ramp_marker_dispatches_without_input(self):
        ctx = _make_ctx()
        h = _make_harness(ctx)
        marker = {
            "result": TextureResult(texture_id=-1, width=_W, height=_H),
            "_effect_type": "ramp",
            "_effect_params": {
                "ramp_type": "linear", "width": _W, "height": _H,
                "start_x": 0.0, "start_y": 0.0,
                "end_x": 1.0, "end_y": 1.0,
            },
        }
        out = h._dispatch_effect("ramp", marker)
        assert "result" in out and out["result"].texture_id > 0

    def test_noise_marker_dispatches_without_input(self):
        ctx = _make_ctx()
        h = _make_harness(ctx)
        marker = {
            "result": TextureResult(texture_id=-1, width=_W, height=_H),
            "_effect_type": "noise",
            "_effect_params": {
                "noise_type": "perlin", "width": _W, "height": _H,
                "scale": 10.0, "output_channels": "4",
            },
        }
        out = h._dispatch_effect("noise", marker)
        assert "result" in out and out["result"].texture_id > 0


class TestMultiInputDispatch:
    def test_keyer_marker_without_effect_input(self):
        """Unified keyer carries operands as _effect_input_a/_b only."""
        ctx = _make_ctx()
        h = _make_harness(ctx)
        a = _marker_input(h, 0.2)
        b = _marker_input(h, 0.8)
        marker = {
            "result": TextureResult(texture_id=-1, width=_W, height=_H),
            "_effect_type": "keyer",
            "_effect_input_a": a,
            "_effect_input_b": b,
            "_effect_params": {
                "keyer_mode": "difference", "clip_black": 0.0,
                "clip_white": 1.0, "gain": 1.0, "softness": 0.0,
            },
        }
        out = h._dispatch_effect("keyer", marker)
        assert out and any(v.is_valid for v in out.values()
                           if isinstance(v, TextureResult))

    def test_channel_merge_marker_reads_placeholder_geometry(self):
        ctx = _make_ctx()
        h = _make_harness(ctx)
        r = _marker_input(h, 1.0)
        g = _marker_input(h, 0.0)
        marker = {
            "result": TextureResult(texture_id=-1, width=_W, height=_H),
            "_effect_type": "channel_merge",
            "_effect_params": {
                "r_source": 0, "g_source": 1, "b_source": 2, "a_source": 3,
                "r_channel": 0, "g_channel": 1, "b_channel": 2, "a_channel": 3,
                "input_r": r, "input_g": g, "input_b": g, "input_a": r,
            },
        }
        out = h._dispatch_effect("channel_merge", marker)
        assert "result" in out and out["result"].texture_id > 0


# ── port-name normalisation ────────────────────────────────────────────────


class TestNormalizeOutputPorts:
    def _ns(self, ports):
        return SimpleNamespace(output_ports=[(p, "image") for p in ports])

    def test_result_aliased_onto_declared_image_port(self):
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        tex = TextureResult(texture_id=7, width=_W, height=_H)
        out = BaseEvaluatorMixin._normalize_output_ports(
            None, self._ns(["image"]), {"result": tex})
        assert out["image"] is tex and out["result"] is tex

    def test_declared_port_present_unchanged(self):
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        tex = TextureResult(texture_id=7, width=_W, height=_H)
        out = BaseEvaluatorMixin._normalize_output_ports(
            None, self._ns(["result"]), {"result": tex})
        assert set(out) == {"result"}

    def test_multi_output_dicts_untouched(self):
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        a = TextureResult(texture_id=1, width=_W, height=_H)
        b = TextureResult(texture_id=2, width=_W, height=_H)
        out = BaseEvaluatorMixin._normalize_output_ports(
            None, self._ns(["beauty"]), {"a": a, "b": b})
        assert set(out) == {"a", "b"}


# ── ramp shader ────────────────────────────────────────────────────────────


class TestRampShader:
    def test_apply_ramp_sets_resolution_without_keyerror(self):
        ctx = _make_ctx()
        h = _make_harness(ctx)
        out = h._apply_ramp({
            "ramp_type": "linear", "width": _W, "height": _H,
            "start_x": 0.0, "start_y": 0.0, "end_x": 1.0, "end_y": 0.0,
        })
        assert out["result"].texture_id > 0

    def test_radial_ramp_aspect_corrected(self):
        """A radial ramp on a 2:1 canvas stays circular (aspect-corrected).

        With radius=0.5 (normalised against the canvas height) the gradient
        saturates to the end colour 0.5·height pixels from the centre along
        the middle row. An aspect-naive shader would measure the radius in
        normalised x, saturating 0.5·width pixels out — never inside the
        frame.
        """
        ctx = _make_ctx()
        h = _make_harness(ctx)
        w2, h2 = 64, 32
        out = h._apply_ramp({
            "ramp_type": "radial", "width": w2, "height": h2,
            "center_x": 0.5, "center_y": 0.5, "radius": 0.5,
        })
        tex = h._resource_pool._pooled_textures.get("f4")[-1]
        arr = np.frombuffer(
            tex.read(), dtype=np.float32).reshape(h2, w2, 4)
        mid_row = arr[h2 // 2, :, 0]
        cx = w2 // 2
        assert abs(float(mid_row[cx]) - 0.0) < 0.1, "centre is start colour"
        # Half-way to the radius: t ≈ 0.5 → ~0.5 in the gradient.
        assert abs(float(mid_row[cx + 8]) - 0.5) < 0.15
        # At the radius (16 px) the gradient has saturated to the end colour.
        assert float(mid_row[cx + 16]) > 0.9


# ── edge detection ─────────────────────────────────────────────────────────


class TestEdgeDetectCanny:
    def _edge_image(self):
        img = np.zeros((_H, _W, 4), dtype=np.float32)
        img[:, _W // 2:, :3] = 1.0
        img[:, :, 3] = 1.0
        return img

    def test_canny_detects_vertical_edge(self):
        ctx = _make_ctx()
        h = _make_harness(ctx)
        src = _upload_rgba(h, self._edge_image())
        out = h._apply_edge_detect(src, {"method": "canny"}, _W, _H)
        tex = h._resource_pool._pooled_textures.get("f4")[-1]
        arr = np.frombuffer(
            tex.read(), dtype=np.float32).reshape(_H, _W, 4)
        assert arr.max() > 0.5, "canny must find the step edge"

    def test_direction_mode_has_no_nan_on_flat_regions(self):
        ctx = _make_ctx()
        h = _make_harness(ctx)
        src = _marker_input(h, 0.5)  # completely flat
        out = h._apply_edge_detect(
            src, {"method": "sobel", "output_mode": "direction"}, _W, _H)
        tex = h._resource_pool._pooled_textures.get("f4")[-1]
        arr = np.frombuffer(
            tex.read(), dtype=np.float32).reshape(_H, _W, 4)
        assert not np.isnan(arr).any(), "atan(0,0) leaked NaN"


# ── volume3d ───────────────────────────────────────────────────────────────


class TestVolume3dBroadcast:
    def test_evaluate_small_volume_returns_result(self, tmp_path):
        from src.features.composite.nodes.volume3d_node import Volume3DNode
        n = 32
        z, y, x = np.mgrid[0:n, 0:n, 0:n].astype(np.float32)
        cx = (n - 1) / 2
        r = np.sqrt((x - cx) ** 2 + (y - cx) ** 2 + (z - cx) ** 2)
        density = np.clip(1.0 - r / (n / 2.5), 0, 1).astype(np.float16)
        path = tmp_path / "vol.npz"
        np.savez_compressed(path, density=density,
                            spacing=np.float32(0.1))

        node = Volume3DNode()
        node.set_parameter("file_path", str(path))
        node.set_parameter("output_width", 32)
        node.set_parameter("output_height", 32)
        fake_ctx = SimpleNamespace(
            gl=SimpleNamespace(
                texture=lambda size, comp, data=None, dtype="f4":
                    SimpleNamespace(glo=42)))
        out = node._do_evaluate({}, fake_ctx, None, None,
                                render_mode="export")
        assert "result" in out, "Beer-Lambert broadcast crashed"


# ── headless GL binding ────────────────────────────────────────────────────


class TestGlBind:
    def test_bind_texture_unit_on_standalone_context(self):
        ctx = _make_ctx()
        from OpenGL.GL import glGetError
        from src.features.composite.core.gl_bind import bind_texture_unit
        tex = ctx.texture((4, 4), 4, dtype='f2')
        bind_texture_unit(tex.glo, 1)
        bind_texture_unit(0, 1)
        assert glGetError() == 0


# ── video fallback preload suppression ─────────────────────────────────────


class TestVideoFallbackPreloadSuppression:
    def test_fallback_mode_never_spawns_preload_worker(self, qapp):
        from src.features.composite.nodes.video_frame_node import (
            VideoFrameNode,
        )
        node = VideoFrameNode()
        node._use_service = False
        node._source = SimpleNamespace(read_frame=lambda f: None)
        node._preload_frames(5)
        assert node._preload_thread is None, (
            "direct-source fallback must decode on the render thread only "
            "(FFmpegSource is not reentrant; the worker races it in libav)")

    def test_run_preload_returns_early_in_fallback_mode(self, qapp):
        from src.features.composite.nodes.video_frame_node import (
            VideoFrameNode,
        )
        node = VideoFrameNode()
        node._use_service = False
        node.parameters["total_frames"] = 100
        # Must return before touching any decode path.
        node._run_preload(5, "preview")


# ── CLI headless render (real footage, subprocess) ────────────────────────


class TestCliHeadlessRender:
    VIDEO = os.path.join(
        FIXTURE_DIR, 'tracking_fixtures', 'perspective.mp4')

    @pytest.mark.skipif(not os.path.isfile(VIDEO),
                        reason="real test video missing")
    def test_render_one_frame_headless(self, tmp_path):
        from src.features.composite.core.node_engine import (
            NodeGraph, NODE_REGISTRY,
        )
        g = NodeGraph()
        v = NODE_REGISTRY['video_frame']()
        v.set_parameter('file_path', self.VIDEO)
        v.set_parameter('frame_number', 10)
        g.add_node(v)
        o = NODE_REGISTRY['output']()
        g.add_node(o)
        g.connect(v.id, 'image', o.id, 'image')
        graph_path = tmp_path / "graph.json"
        graph_path.write_text(json.dumps(g.to_dict()))

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        proc = subprocess.run(
            [sys.executable, "scripts/topos_render.py", str(graph_path),
             "--frames", "1", "--out", str(tmp_path), "--format", "png"],
            capture_output=True, text=True, timeout=180, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..', '..'))
        out_png = tmp_path / "graph.0001.png"
        assert proc.returncode == 0, (
            f"CLI render failed: rc={proc.returncode} "
            f"stderr={proc.stderr[-400:]}")
        assert out_png.is_file(), "no output frame written"
        from PIL import Image
        img = np.asarray(
            Image.open(out_png).convert('RGB'), dtype=np.float32) / 255.0
        assert img.mean() > 0.01, "rendered frame is black"
