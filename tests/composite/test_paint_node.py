"""C2 Paint node tests — stroke serialization, clone/erase/solid CPU
rasterization through a real standalone GL context, and the mask output
matte contract (RGB, for ai_remove/keyer consumers).
"""

import json
import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.paint_node import PaintNode
from src.features.composite.core.node_engine import NODE_REGISTRY, NODE_CATEGORIES
from src.features.composite.core.format import BBox
from src.features.composite.nodes.base_node import TextureResult

pytestmark = pytest.mark.composite_core


# ── Node registration + stroke serialization ────────────────────────────────

class TestPaintNodeRegistration:
    def test_registered_with_own_category(self):
        assert NODE_REGISTRY.get("paint") is PaintNode
        assert any(t == "paint" for t, _ in NODE_CATEGORIES.get("Paint", []))

    def test_ports_and_aliases(self):
        node = PaintNode(node_id="p")
        assert [port.name for port in node.inputs] == ["image", "mask"]  # CP-044 通用 mask 端口
        assert [port.name for port in node.outputs] == ["result", "mask"]
        assert "克隆" in PaintNode.SEARCH_ALIASES
        assert "仿制图章" in PaintNode.SEARCH_ALIASES

    def test_parse_strokes_coerces_and_drops_garbage(self):
        raw = json.dumps([
            {"id": "a", "type": "clone", "points": [[0.1, 0.1], [5.0, -2.0]],
             "size": 0.03, "src_offset": [0.2, 0.0]},
            {"type": "bogus", "points": [[0.1, 0.1]]},
            {"type": "erase", "points": "not-a-list"},
            {"type": "solid", "points": [[0.5, 0.5]], "color": [1, 2, 3, 4]},
        ])
        strokes = PaintNode.parse_strokes(raw)
        assert [s["type"] for s in strokes] == ["clone", "solid"]
        # Out-of-range coordinates clamp into [0, 1].
        assert strokes[0]["points"][1] == [1.0, 0.0]
        # HDR solid color survives (rgb unclamped), alpha clamps to 1.
        assert strokes[1]["color"] == [1.0, 2.0, 3.0, 1.0]

    def test_strokes_roundtrip_via_serialization(self):
        node = PaintNode(node_id="p")
        stroke = PaintNode.make_stroke(
            "clone", [[0.1, 0.1], [0.4, 0.4]], size=0.02,
            src_offset=[0.1, 0.0])
        node.set_strokes([stroke])
        clone = PaintNode.from_dict(dict(node.to_dict()))
        assert clone.get_strokes() == node.get_strokes()

    def test_legacy_graph_without_strokes_key(self):
        clone = PaintNode.from_dict({"id": "p", "type": "paint",
                                     "parameters": {}})
        assert clone.get_strokes() == []
        assert clone.parameters["output_width"] == 1920


# ── Evaluation through a real standalone GL context ─────────────────────────

class _Pool:
    def __init__(self, ctx):
        self.ctx = ctx
        # _get_texture_by_id scans _pooled_textures values (lists of tex).
        self._pooled_textures = {}

    def acquire_texture(self, w, h, components=4, dtype="f4"):
        tex = self.ctx.texture((w, h), components, dtype=dtype)
        self._pooled_textures.setdefault(dtype, []).append(tex)
        return tex


class _PaintHarness:
    """Minimal evaluator facade exposing what _apply_paint touches."""

    def __init__(self, ctx):
        import moderngl as _m
        from src.shared.gpu.gl_backend import FramebufferManager
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        self.ctx = ctx
        self.fb_manager = FramebufferManager(ctx)
        self._resource_pool = _Pool(ctx)
        self._mixin = FilterEvaluatorMixin()

        def _get_texture_by_id(texture_id):
            for tex in self.fb_manager._active_textures:
                if tex.glo == texture_id:
                    return tex
            for tex_list in self._resource_pool._pooled_textures.values():
                for tex in tex_list:
                    if tex.glo == texture_id:
                        return tex
            return None

        self._apply_paint = self._mixin._apply_paint.__get__(self)
        self._rasterize_stroke_mask = \
            self._mixin._rasterize_stroke_mask.__get__(self)
        self._paint_passthrough = \
            self._mixin._paint_passthrough.__get__(self)
        # staticmethod — access via the class, no instance binding.
        self._sample_clone_source = \
            FilterEvaluatorMixin._sample_clone_source
        self._get_texture_by_id = _get_texture_by_id


def _upload_source(harness, rgba_premult):
    h, w = rgba_premult.shape[:2]
    tex = harness.fb_manager.get_temp_texture(w, h, components=4, dtype="f4")
    tex.write(np.ascontiguousarray(np.flipud(rgba_premult)).tobytes())
    return TextureResult(
        texture_id=tex.glo, width=w, height=h,
        bbox=BBox.full_frame(w, h),
        alpha_premultiplied=True, color_space="linear")


def _read_result(harness, texture_result):
    from src.shared.gpu.gl_backend import FramebufferManager  # noqa: F401
    tex = harness._get_texture_by_id(texture_result.texture_id)
    fbo = harness.fb_manager.get_fbo(tex)
    fbo.use()
    harness.ctx.viewport = (0, 0, texture_result.width, texture_result.height)
    raw = fbo.read(components=4, dtype="f4")
    arr = np.frombuffer(raw, dtype=np.float32).reshape(
        texture_result.height, texture_result.width, 4)
    return np.flipud(arr).copy()


def _gradient_image(w=64, h=48):
    """Premultiplied-alpha unique-gradient image (alpha = 0.8)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    rgb = np.stack([xx / w, yy / h, np.full_like(xx, 0.5)], axis=-1)
    alpha = np.full((h, w, 1), 0.8, dtype=np.float32)
    rgb = rgb * alpha  # premultiply: only RGB scales
    return np.concatenate([rgb, alpha], axis=-1)


@pytest.mark.usefixtures("qapp")
class TestPaintEvaluation:
    @pytest.fixture()
    def harness(self):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover - CI GPU availability
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        yield _PaintHarness(ctx)
        ctx.release()

    def test_empty_strokes_pass_input_through(self, harness):
        src = _upload_source(harness, _gradient_image())
        out = harness._apply_paint(src, {"strokes": [], "output_width": 64,
                                         "output_height": 48})
        assert out["result"] is src

    def test_clone_copies_shifted_source_region(self, harness):
        img = _gradient_image()
        src = _upload_source(harness, img)
        # Horizontal clone stroke through the CENTRE of pixel row 24
        # (y = 24.5/48). Sampling is src = dst - offset, so a -0.25 x
        # offset copies the region 0.25w to the RIGHT of the stroke.
        strokes = [PaintNode.make_stroke(
            "clone", [[0.2, 24.5 / 48], [0.3, 24.5 / 48]], size=0.03,
            feather=0.0, opacity=1.0, src_offset=[-0.25, 0.0])]
        out = harness._apply_paint(src, {"strokes": strokes,
                                         "output_width": 64,
                                         "output_height": 48})
        assert "result" in out
        got = _read_result(harness, out["result"])

        w, h = 64, 48
        # A fully-covered stroke pixel: (0.25w, row 24); the source sits
        # 0.25w to the right (offset subtracted), same row.
        py, px = 24, int(0.25 * w)
        src_px = min(px + int(0.25 * w), w - 1)
        np.testing.assert_allclose(
            got[py, px, :3], img[py, src_px, :3], atol=2e-3)
        # Unpainted corner keeps the original (premultiplied) pixels.
        np.testing.assert_allclose(got[2, 2], img[2, 2], atol=2e-3)

    def test_erase_reduces_premultiplied_alpha(self, harness):
        img = _gradient_image()
        src = _upload_source(harness, img)
        # Dot centred on the middle pixel's centre (32.5, 24.5) so the
        # sample pixel is fully covered even with antialiasing.
        cx, cy = 32.5 / 64, 24.5 / 48
        strokes = [PaintNode.make_stroke(
            "erase", [[cx, cy]], size=0.05, feather=0.0, opacity=0.5)]
        out = harness._apply_paint(src, {"strokes": strokes,
                                         "output_width": 64,
                                         "output_height": 48})
        got = _read_result(harness, out["result"])
        py, px = 24, 32
        # Half-opacity erase over fully-covered pixel → alpha × 0.5 in the
        # premultiplied domain (rgb scaled identically).
        np.testing.assert_allclose(got[py, px, 3], 0.8 * 0.5, atol=2e-3)
        np.testing.assert_allclose(got[py, px, 0], img[py, px, 0] * 0.5,
                                   atol=2e-3)
        # Outside the stroke nothing changed.
        np.testing.assert_allclose(got[2, 2, 3], 0.8, atol=2e-3)

    def test_mask_output_is_rgb_matte_of_stroke_area(self, harness):
        img = _gradient_image()
        src = _upload_source(harness, img)
        strokes = [PaintNode.make_stroke(
            "solid", [[0.3, 0.3], [0.6, 0.3]], size=0.015,
            feather=0.0, opacity=0.5, color=[1.0, 0.0, 0.0, 1.0])]
        out = harness._apply_paint(src, {"strokes": strokes,
                                         "output_width": 64,
                                         "output_height": 48})
        assert "mask" in out
        mask = _read_result(harness, out["mask"])
        py, px = int(0.3 * 48), int(0.45 * 64)
        # Inside the stroke: white matte in RGB (ai_remove reads channel 0).
        assert mask[py, px, 0] == pytest.approx(1.0, abs=0.05)
        assert mask[py, px, 2] == pytest.approx(1.0, abs=0.05)
        # Outside: zero.
        assert mask[2, 2, 0] == pytest.approx(0.0, abs=0.02)

    def test_solid_on_transparent_base_without_input(self, harness):
        strokes = [PaintNode.make_stroke(
            "solid", [[0.5, 0.5]], size=0.03, feather=0.0,
            color=[0.0, 1.0, 0.0, 1.0])]
        out = harness._apply_paint(None, {"strokes": strokes,
                                          "output_width": 64,
                                          "output_height": 48})
        got = _read_result(harness, out["result"])
        py, px = int(0.5 * 48), int(0.5 * 64)
        # Premultiplied transparent base: solid green over it is premult
        # green (rgb == alpha inside the stroke).
        assert got[py, px, 1] == pytest.approx(1.0, abs=0.05)
        assert got[py, px, 3] == pytest.approx(1.0, abs=0.05)
        assert got[1, 1, 3] == pytest.approx(0.0, abs=0.02)

    def test_feather_softens_edges(self, harness):
        stroke = PaintNode.make_stroke(
            "erase", [[0.5, 0.5]], size=0.05, feather=1.0, opacity=1.0)
        mask_soft = harness._rasterize_stroke_mask(stroke, 64, 48)
        stroke_hard = dict(stroke, feather=0.0)
        mask_hard = harness._rasterize_stroke_mask(stroke_hard, 64, 48)
        # Feathering must produce MANY more partial-coverage pixels than
        # the hard edge (which only has the antialiased rim).
        mid_soft = int(((mask_soft > 0.05) & (mask_soft < 0.95)).sum())
        mid_hard = int(((mask_hard > 0.05) & (mask_hard < 0.95)).sum())
        assert mid_soft > 2 * mid_hard
        # The hard edge still reaches full saturation somewhere.
        assert mask_hard.max() == pytest.approx(1.0, abs=0.01)


# ── P1-5: empty mask must zero cleanly through the production resource pool ──

class TestPaintEmptyMaskProductionPool:
    """Reproduce the production texture pool (GPUResourcePool defaults to
    pooled 'f2' textures). The old np.float32 zeros upload raised
    "data size mismatch" on every empty-stroke frame at 4K.
    """

    def test_passthrough_mask_zero_via_pooled_f2_texture(self):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover - CI GPU availability
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        try:
            from src.shared.gpu.gl_backend import FramebufferManager
            from src.features.composite.render.gpu_resource_pool import (
                GPUResourcePool,
            )
            from src.features.composite.evaluators.filter_evaluator import (
                FilterEvaluatorMixin,
            )

            fb_manager = FramebufferManager(ctx)
            pool = GPUResourcePool(fb_manager, max_memory_mb=256)

            class _Self:
                pass

            slf = _Self()
            slf.ctx = ctx
            slf.fb_manager = fb_manager
            slf._resource_pool = pool
            bound = FilterEvaluatorMixin._paint_passthrough.__get__(slf)

            out = bound(None, 64, 48)
            assert "mask" in out
            mask_res = out["mask"]
            tex = next((t for t in fb_manager._active_textures
                        if t.glo == mask_res.texture_id), None)
            assert tex is not None, "mask texture not tracked by fb_manager"
            assert tex.dtype == "f2"  # production default — repro condition
            fbo = fb_manager.get_fbo(tex)
            fbo.use()
            ctx.viewport = (0, 0, 64, 48)
            raw = fbo.read(components=4, dtype="f4")
            arr = np.frombuffer(raw, dtype=np.float32).reshape(48, 64, 4)
            assert float(np.abs(arr).max()) == pytest.approx(0.0, abs=1e-6)
        finally:
            ctx.release()


# ── CP-003: stroke frame_range gating ───────────────────────────────────────

class TestPaintStrokeFrameGating:
    """frame_range 语义：null=永续；[start,end] 闭区间内才绘制。"""

    def _stroke(self, frame_range):
        return {"id": "s", "type": "solid", "points": [[0.5, 0.5]],
                "size": 0.02, "feather": 0.0, "opacity": 1.0,
                "src_offset": [0.0, 0.0], "color": [1.0, 1.0, 1.0, 1.0],
                "frame_range": frame_range}

    def test_null_range_always_active(self):
        strokes = [self._stroke(None)]
        for frame in (0, 5, 1000):
            assert len(PaintNode.strokes_active_at(strokes, frame)) == 1

    def test_range_is_inclusive(self):
        strokes = [self._stroke([10, 20])]
        assert PaintNode.strokes_active_at(strokes, 9) == []
        assert len(PaintNode.strokes_active_at(strokes, 10)) == 1
        assert len(PaintNode.strokes_active_at(strokes, 15)) == 1
        assert len(PaintNode.strokes_active_at(strokes, 20)) == 1
        assert PaintNode.strokes_active_at(strokes, 21) == []

    def test_inverted_range_never_activates(self):
        # start > end：宁可无绘制，不可意外绘制
        strokes = [self._stroke([20, 10])]
        for frame in (10, 15, 20):
            assert PaintNode.strokes_active_at(strokes, frame) == []

    def test_evaluate_filters_by_ctx_frame_number(self):
        node = PaintNode(node_id="p-gate")
        node.set_strokes([
            self._stroke(None),
            self._stroke([5, 10]),
        ])
        ctx = type("Ctx", (), {"frame_number": 15})()  # 简单 stub ctx
        out = node._do_evaluate({}, ctx, None)
        assert len(out["_effect_params"]["strokes"]) == 1

        ctx.frame_number = 7
        out = node._do_evaluate({}, ctx, None)
        assert len(out["_effect_params"]["strokes"]) == 2

    def test_params_require_frame_cache_only_when_gated(self):
        ungated = json.dumps([self._stroke(None)])
        gated = json.dumps([self._stroke(None), self._stroke([1, 2])])
        assert PaintNode.params_require_frame_cache(
            {"strokes": ungated}) is False
        assert PaintNode.params_require_frame_cache(
            {"strokes": gated}) is True
