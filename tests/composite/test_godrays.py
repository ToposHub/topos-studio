"""B3 God Rays tests — registration, marker contract, full-frame ROI
policy, serialization, and directional/decay verification on a real GL
context.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.core.node_capabilities import get_node_capability
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.godrays_node import GodRaysNode

pytestmark = pytest.mark.composite_core

_W, _H = 64, 48


def _make_node(**overrides):
    node = GodRaysNode(node_id="gr1")
    for k, v in overrides.items():
        node.set_parameter(k, v)
    return node


class TestRegistration:
    def test_registered_in_filter_category(self):
        assert NODE_REGISTRY.get("godrays") is GodRaysNode
        assert any(t == "godrays"
                   for t, _ in NODE_CATEGORIES.get("Filter", []))

    def test_aliases(self):
        assert "体积光" in GodRaysNode.SEARCH_ALIASES
        assert "丁达尔" in GodRaysNode.SEARCH_ALIASES

    def test_capability_is_full_frame(self):
        cap = get_node_capability("godrays")
        assert cap.effect_type == "godrays"
        # Radial sampling is global: the node opts out of ROI clipping.
        assert cap.roi_policy == "full_frame"
        assert cap.tile_padding == 0


class TestRoiPolicy:
    def test_input_roi_is_full_frame(self):
        node = _make_node()
        output_roi = BBox(x=16, y=16, width=8, height=8)
        input_full = BBox(x=0, y=0, width=_W, height=_H)
        out = node.compute_input_roi(output_roi, node.parameters,
                                     {"image": input_full,
                                      "mask": input_full})
        assert out["image"] == input_full
        assert out["mask"] == input_full

    def test_requires_no_roi_expansion(self):
        node = _make_node(samples=64)
        assert node.requires_roi_expansion(node.parameters) == 0


class TestMarker:
    def _tex(self, tex_id=4):
        return TextureResult(texture_id=tex_id, width=_W, height=_H,
                             bbox=BBox.full_frame(_W, _H),
                             alpha_premultiplied=True,
                             color_space="linear")

    def test_marker_alpha_occluder(self):
        node = _make_node(light_x=0.2, density=0.8, samples=48)
        out = node.evaluate({"image": self._tex(1)}, object(), object(),
                            object(), render_mode="full")
        params = out["_effect_params"]
        assert params["light_pos"] == (0.2, 0.5)
        assert params["density"] == 0.8
        assert params["sample_count"] == 48
        assert "_effect_mask" not in out

    def test_mask_input_attached(self):
        node = _make_node()
        out = node.evaluate({"image": self._tex(1), "mask": self._tex(2)},
                            object(), object(), object())
        assert out["_effect_mask"].texture_id == 2

    def test_preview_halves_samples(self):
        node = _make_node(samples=64)
        out = node.evaluate({"image": self._tex(1)}, object(), object(),
                            object(), render_mode="preview")
        assert out["_effect_params"]["sample_count"] == 32

    def test_samples_clamped(self):
        node = _make_node()
        node.parameters["samples"] = 999  # bypass UI clamping
        out = node.evaluate({"image": self._tex(1)}, object(), object(),
                            object(), render_mode="full")
        assert out["_effect_params"]["sample_count"] == 64

    def test_missing_input_emits_nothing(self):
        node = _make_node()
        assert node.evaluate({}, object(), object(), object()) == {}


class TestSerialization:
    def test_roundtrip(self, qapp):
        node = _make_node(light_x=0.25, light_y=0.75, density=1.2,
                          decay=0.9, samples=64)
        clone = GodRaysNode.from_dict(node.to_dict())
        assert clone.parameters["light_x"] == pytest.approx(0.25)
        assert clone.parameters["light_y"] == pytest.approx(0.75)
        assert clone.parameters["density"] == pytest.approx(1.2)
        assert clone.parameters["decay"] == pytest.approx(0.9)
        assert clone.parameters["samples"] == 64


# ── Real GL: directionality + decay ────────────────────────────────────────

class TestGLGodRays:
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

    def _occluder_scene(self):
        """Transparent frame with one opaque vertical stripe at x=0.5."""
        img = np.zeros((_H, _W, 4), dtype=np.float32)
        img[:, _W // 2 - 1:_W // 2 + 1, 3] = 1.0  # alpha occluder
        return img

    def test_shafts_appear_behind_occluder_and_decay(self, harness, qapp):
        harness
        img = self._occluder_scene()
        src = self._upload(harness, img)
        params = {"light_pos": (0.1, 0.5), "density": 1.0, "weight": 0.5,
                  "decay": 0.9, "sample_count": 64,
                  "light_color": (1.0, 1.0, 1.0)}
        out = harness._apply_godrays(src, params, _W, _H)
        got = self._read(harness, out["result"])
        base = img[..., 0]

        row = _H // 2  # the ray line through the light and the stripe
        behind = got[row, int(_W * 0.75), 0] - base[  # right of stripe
            row, int(_W * 0.75)]
        in_front = got[row, int(_W * 0.25), 0] - base[  # light side
            row, int(_W * 0.25)]
        # Light sits left of the stripe: shafts land on its right side.
        assert behind > 0.01
        assert in_front < 0.01
        # Energy decays away from the occluder (toward the right edge).
        near = got[row, int(_W * 0.55), 0] - base[row, int(_W * 0.55)]
        far = got[row, int(_W * 0.95), 0] - base[row, int(_W * 0.95)]
        assert near > far
        # Off the ray line there is little to no shaft.
        off = got[int(_H * 0.1), int(_W * 0.75), 0]
        assert off < 0.05
        # Alpha passthrough.
        np.testing.assert_allclose(got[..., 3], img[..., 3], atol=1e-5)

    def test_decay_one_removes_distance_falloff(self, harness, qapp):
        harness
        img = self._occluder_scene()
        src = self._upload(harness, img)
        params = {"light_pos": (0.1, 0.5), "density": 1.0, "weight": 0.5,
                  "decay": 1.0, "sample_count": 64,
                  "light_color": (1.0, 1.0, 1.0)}
        out = harness._apply_godrays(src, params, _W, _H)
        got = self._read(harness, out["result"])
        row = _H // 2
        # With decay=1 every sample weighs equally: uniform shaft strength
        # along the ray line right of the stripe.
        a = got[row, int(_W * 0.60), 0]
        b = got[row, int(_W * 0.90), 0]
        assert a == pytest.approx(b, abs=0.02)
        assert a > 0.01

    def test_mask_input_replaces_alpha(self, harness, qapp):
        harness
        # Input fully transparent; the mask carries the occluder.
        img = np.zeros((_H, _W, 4), dtype=np.float32)
        mask = self._occluder_scene()
        src = self._upload(harness, img)
        mask_tex = self._upload(harness, mask)
        params = {"light_pos": (0.1, 0.5), "density": 1.0, "weight": 0.5,
                  "decay": 0.9, "sample_count": 64,
                  "light_color": (1.0, 1.0, 1.0), "_mask_tex": mask_tex}
        out = harness._apply_godrays(src, params, _W, _H)
        got = self._read(harness, out["result"])
        row = _H // 2
        assert got[row, int(_W * 0.75), 0] > 0.01
