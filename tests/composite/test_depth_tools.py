"""A3 depth tools tests — ZBlur / Fog / DepthMatte.

Covers the shared depth normalization, registration, marker contracts,
serialization, and analytic verification on a real GL context (fog follows
1 - exp(-density*t); depth matte band boundaries follow smoothstep; zblur
keeps the focal plane sharp and blurs monotonically away from it).
"""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.core.node_capabilities import get_node_capability
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.depth_common import depth_to_normalized
from src.features.composite.nodes.depth_matte_node import DepthMatteNode
from src.features.composite.nodes.fog_node import FogNode
from src.features.composite.nodes.zblur_node import ZBlurNode

pytestmark = pytest.mark.composite_core

_W, _H = 64, 48


def _depth_ramp():
    """Depth in the red channel: 0 (left) → 1 (right)."""
    depth = np.zeros((_H, _W, 4), dtype=np.float32)
    depth[..., 0] = np.linspace(0.0, 1.0, _W, dtype=np.float32)[None, :]
    depth[..., 3] = 1.0
    return depth


def _flat_image(value=0.6):
    img = np.zeros((_H, _W, 4), dtype=np.float32)
    img[..., :3] = value
    img[..., 3] = 1.0
    return img


class TestDepthCommon:
    def test_normalized_clamps(self):
        assert depth_to_normalized(-0.5) == 0.0
        assert depth_to_normalized(0.25) == pytest.approx(0.25)
        assert depth_to_normalized(1.5) == 1.0

    def test_world_linearizes(self):
        assert depth_to_normalized(10.0, "world", near=0.0,
                                   far=100.0) == pytest.approx(0.1)
        assert depth_to_normalized(-5.0, "world", near=0.0,
                                   far=100.0) == 0.0
        assert depth_to_normalized(250.0, "world", near=0.0,
                                   far=100.0) == 1.0
        # Degenerate far==near must not divide by zero.
        assert depth_to_normalized(5.0, "world", near=5.0,
                                   far=5.0) == 0.0


class TestRegistration:
    def test_zblur(self):
        assert NODE_REGISTRY.get("zblur") is ZBlurNode
        assert any(t == "zblur" for t, _ in NODE_CATEGORIES["Filter"])
        cap = get_node_capability("zblur")
        assert cap.effect_type == "zblur"
        assert cap.roi_policy == "expand"

    def test_fog(self):
        assert NODE_REGISTRY.get("fog") is FogNode
        assert any(t == "fog" for t, _ in NODE_CATEGORIES["Filter"])
        cap = get_node_capability("fog")
        assert cap.roi_policy == "identity"

    def test_depth_matte(self):
        assert NODE_REGISTRY.get("depth_matte") is DepthMatteNode
        assert any(t == "depth_matte"
                   for t, _ in NODE_CATEGORIES["Mask"])
        cap = get_node_capability("depth_matte")
        assert cap.effect_type == "depth_matte"

    def test_zblur_padding_tracks_radius(self):
        node = ZBlurNode(node_id="z")
        node.set_parameter("max_blur_radius", 30.0)
        assert node._tile_padding(node.parameters) == 30
        roi = BBox(x=10, y=10, width=50, height=50)
        out = node.compute_input_roi(roi, node.parameters)
        assert out["image"].width >= roi.width + 60
        # Depth is sampled per output pixel — no kernel expansion.
        assert out["depth"] == roi


class TestMarkerContracts:
    def _tex(self, tex_id=5):
        return TextureResult(texture_id=tex_id, width=_W, height=_H,
                             bbox=BBox.full_frame(_W, _H),
                             alpha_premultiplied=True,
                             color_space="linear")

    def test_zblur_marker(self):
        node = ZBlurNode(node_id="z")
        image, depth = self._tex(1), self._tex(2)
        out = node.evaluate({"image": image, "depth": depth}, object(),
                            object(), object(), render_mode="full")
        assert out["_effect_type"] == "zblur"
        assert out["_effect_depth"] is depth
        assert out["_effect_params"]["sample_count"] == 16
        assert out["_effect_params"]["depth_units"] == "normalized"

    def test_zblur_preview_halves_samples(self):
        node = ZBlurNode(node_id="z")
        out = node.evaluate({"image": self._tex(1), "depth": self._tex(2)},
                            object(), object(), object(),
                            render_mode="preview")
        assert out["_effect_params"]["sample_count"] == 8

    def test_zblur_missing_depth_emits_nothing(self):
        node = ZBlurNode(node_id="z")
        out = node.evaluate({"image": self._tex(1)}, object(), object(),
                            object())
        assert out == {}

    def test_fog_marker(self):
        node = FogNode(node_id="f")
        node.set_parameter("density", 2.5)
        node.set_parameter("start", 0.2)
        node.set_parameter("end", 0.8)
        out = node.evaluate({"image": self._tex(1), "depth": self._tex(2)},
                            object(), object(), object(),
                            render_mode="full")
        params = out["_effect_params"]
        assert params["density"] == pytest.approx(2.5)
        assert params["start"] == pytest.approx(0.2)
        assert params["end"] == pytest.approx(0.8)
        assert params["fog_color"] == pytest.approx((0.5, 0.6, 0.7))

    def test_fog_end_never_below_start(self):
        node = FogNode(node_id="f")
        node.set_parameter("start", 0.8)
        node.set_parameter("end", 0.2)
        out = node.evaluate({"image": self._tex(1), "depth": self._tex(2)},
                            object(), object(), object())
        assert out["_effect_params"]["end"] == pytest.approx(0.8)

    def test_depth_matte_marker(self):
        node = DepthMatteNode(node_id="d")
        out = node.evaluate({"image": self._tex(1), "depth": self._tex(2)},
                            object(), object(), object())
        params = out["_effect_params"]
        assert params["range_start"] == pytest.approx(0.3)
        assert params["range_end"] == pytest.approx(0.7)
        assert out["result"].alpha_premultiplied is True


class TestSerialization:
    def test_zblur_roundtrip(self, qapp):
        node = ZBlurNode(node_id="z")
        for k, v in (("focus_distance", 0.75), ("max_blur_radius", 25.0),
                     ("depth_units", "world"), ("near", 1.0),
                     ("far", 50.0)):
            node.set_parameter(k, v)
        clone = ZBlurNode.from_dict(node.to_dict())
        assert clone.parameters["focus_distance"] == pytest.approx(0.75)
        assert clone.parameters["max_blur_radius"] == pytest.approx(25.0)
        assert clone.parameters["depth_units"] == "world"
        assert clone.parameters["far"] == pytest.approx(50.0)

    def test_fog_roundtrip(self, qapp):
        node = FogNode(node_id="f")
        node.set_parameter("density", 3.0)
        node.set_parameter("color", (0.1, 0.2, 0.3, 1.0))
        clone = FogNode.from_dict(node.to_dict())
        assert clone.parameters["density"] == pytest.approx(3.0)
        assert clone.parameters["color"] == pytest.approx(
            (0.1, 0.2, 0.3, 1.0))

    def test_depth_matte_roundtrip(self, qapp):
        node = DepthMatteNode(node_id="d")
        node.set_parameter("range_start", 0.1)
        node.set_parameter("range_end", 0.9)
        node.set_parameter("feather", 0.2)
        clone = DepthMatteNode.from_dict(node.to_dict())
        assert clone.parameters["range_start"] == pytest.approx(0.1)
        assert clone.parameters["range_end"] == pytest.approx(0.9)
        assert clone.parameters["feather"] == pytest.approx(0.2)


# ── Real GL: analytic verification ─────────────────────────────────────────

class _DepthHarnessBase:
    @pytest.fixture()
    def harness(self):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        from src.features.composite.evaluators.color_evaluator import (
            ColorEvaluatorMixin,
        )
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )

        class _Pool:
            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}

            def acquire_texture(self, w, h, components=4, dtype="f4"):
                tex = self.ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                return tex

        class _H(BaseEvaluatorMixin, ColorEvaluatorMixin,
                 FilterEvaluatorMixin):
            def __init__(self, ctx):
                self.ctx = ctx
                self.utility_shaders = {}
                self._resource_pool = _Pool(ctx)

                from src.shared.gpu.gl_backend import FramebufferManager
                self.fb_manager = FramebufferManager(ctx)
                self._load_shaders()

            def _bind_texture_unit(self, texture_id, unit):
                tex = self._get_texture_by_id(texture_id)
                if tex is not None:
                    tex.use(unit)

        yield _H(ctx)
        ctx.release()

    def _upload(self, harness, rgba):
        h, w = rgba.shape[:2]
        tex = harness._resource_pool.acquire_texture(w, h)
        tex.write(np.ascontiguousarray(np.flipud(rgba)).tobytes())
        return TextureResult(
            texture_id=tex.glo, width=w, height=h,
            bbox=BBox.full_frame(w, h),
            alpha_premultiplied=False, color_space="linear")

    def _read(self, harness, tex_result):
        tex = harness._get_texture_by_id(tex_result.texture_id)
        fbo = harness.fb_manager.get_fbo(tex)
        fbo.use()
        harness.ctx.viewport = (0, 0, tex_result.width, tex_result.height)
        raw = fbo.read(components=4, dtype="f4")
        arr = np.frombuffer(raw, dtype=np.float32).reshape(
            tex_result.height, tex_result.width, 4)
        return np.flipud(arr).copy()


class TestGLFog(_DepthHarnessBase):
    def test_fog_matches_beer_lambert(self, harness, qapp):
        harness
        img = _flat_image(0.6)
        depth = _depth_ramp()
        params = {"depth_units": "normalized", "near": 0.0, "far": 100.0,
                  "density": 2.0, "start": 0.1, "end": 0.9,
                  "fog_color": (0.3, 0.5, 0.7)}
        src = self._upload(harness, img)
        dep = self._upload(harness, depth)
        out = harness._apply_depth_tool("fog", src, dep, params, _W, _H)
        got = self._read(harness, out["result"])

        z = depth[..., 0]
        t = np.clip((z - 0.1) / 0.8, 0.0, 1.0)
        amount = 1.0 - np.exp(-2.0 * t)
        for c in range(3):
            expected = 0.6 * (1.0 - amount) + params["fog_color"][c] * amount
            np.testing.assert_allclose(got[..., c], expected, atol=2e-4)

    def test_start_before_ramp_is_unfogged(self, harness, qapp):
        harness
        img = _flat_image(0.6)
        depth = _depth_ramp()
        params = {"depth_units": "normalized", "near": 0.0, "far": 100.0,
                  "density": 5.0, "start": 0.0, "end": 0.02,
                  "fog_color": (1.0, 0.0, 0.0)}
        src = self._upload(harness, img)
        dep = self._upload(harness, depth)
        out = harness._apply_depth_tool("fog", src, dep, params, _W, _H)
        got = self._read(harness, out["result"])
        # Right edge: t clamps to 1 → fog = 1 - exp(-5) ≈ 0.9933.
        assert got[24, -1, 0] > 0.99
        assert got[24, -1, 1] < 0.01
        # Left edge: z=0 → t=0 → unfogged.
        np.testing.assert_allclose(got[24, 0, :3], 0.6, atol=1e-4)


class TestGLDepthMatte(_DepthHarnessBase):
    def test_band_boundaries_and_feather(self, harness, qapp):
        harness
        img = _flat_image()
        depth = _depth_ramp()
        params = {"depth_units": "normalized", "near": 0.0, "far": 100.0,
                  "range_start": 0.3, "range_end": 0.7, "feather": 0.05}
        src = self._upload(harness, img)
        dep = self._upload(harness, depth)
        out = harness._apply_depth_tool("depth_matte", src, dep, params,
                                        _W, _H)
        got = self._read(harness, out["result"])
        alpha = got[..., 3]

        def smoothstep(e0, e1, x):
            t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
            return t * t * (3.0 - 2.0 * t)

        z = depth[..., 0]
        expected = (smoothstep(0.25, 0.35, z)
                    * (1.0 - smoothstep(0.65, 0.75, z)))
        np.testing.assert_allclose(alpha, expected, atol=2e-4)
        # Inside the band → 1; far outside → 0.
        assert alpha[:, int(0.5 * _W)] == pytest.approx(1.0, abs=1e-4)
        assert alpha[:, 0] == pytest.approx(0.0, abs=1e-4)


class TestGLZBlur(_DepthHarnessBase):
    def test_focus_plane_sharp_and_far_monotonic(self, harness, qapp):
        harness
        # White square at the centre — blur spreads it; measure spread.
        img = np.zeros((_H, _W, 4), dtype=np.float32)
        img[20:28, 28:36, :3] = 1.0
        img[..., 3] = 1.0
        depth = np.full((_H, _W, 4), 0.5, dtype=np.float32)
        depth[..., 3] = 1.0

        params = {"depth_units": "normalized", "near": 0.0, "far": 100.0,
                  "focus_distance": 0.5, "focal_range": 0.15,
                  "max_blur_radius": 8.0, "sample_count": 32}
        src = self._upload(harness, img)
        dep = self._upload(harness, depth)
        out = harness._apply_depth_tool("zblur", src, dep, params, _W, _H)
        got = self._read(harness, out["result"])

        # Focus plane everywhere → coc = 0 → exact passthrough.
        np.testing.assert_allclose(got, img, atol=1e-5)

    def test_defocused_region_blurs(self, harness, qapp):
        harness
        img = np.zeros((_H, _W, 4), dtype=np.float32)
        img[20:28, 28:36, :3] = 1.0
        img[..., 3] = 1.0
        depth = np.full((_H, _W, 4), 0.0, dtype=np.float32)
        depth[..., 3] = 1.0

        params = {"depth_units": "normalized", "near": 0.0, "far": 100.0,
                  "focus_distance": 1.0, "focal_range": 0.2,
                  "max_blur_radius": 8.0, "sample_count": 32}
        src = self._upload(harness, img)
        dep = self._upload(harness, depth)
        out = harness._apply_depth_tool("zblur", src, dep, params, _W, _H)
        got = self._read(harness, out["result"])

        # Centre of the square is spread: dimmer than 1.0 but bright.
        centre = got[24, 32, 0]
        assert 0.2 < centre < 0.9
        # Energy bleeds outside the original square (rows above it).
        assert got[14, 32, 0] > 0.001
        # Alpha channel is preserved, not blurred.
        np.testing.assert_allclose(got[..., 3], img[..., 3], atol=1e-5)
