"""A1 Relight node tests — registration, marker contract, serialization, and
analytic Lambert verification on a real GL context.

The GL tests synthesize a plane at z=0 with known world POSITION and NORMAL
AOVs and compare the GPU output against the closed-form diffuse solution
    out = image * (max(dot(N, L), 0) * intensity * color + ambient)
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
from src.features.composite.nodes.relight_node import RelightNode

pytestmark = pytest.mark.composite_core

_W, _H = 64, 48


def _make_node(**overrides):
    node = RelightNode(node_id="r1")
    for k, v in overrides.items():
        node.set_parameter(k, v)
    return node


class TestRegistration:
    def test_registered_in_filter_category(self):
        assert NODE_REGISTRY.get("relight") is RelightNode
        assert any(t == "relight"
                   for t, _ in NODE_CATEGORIES.get("Filter", []))

    def test_aliases(self):
        assert "重光照" in RelightNode.SEARCH_ALIASES
        assert "补光" in RelightNode.SEARCH_ALIASES

    def test_capability_is_real_per_pixel_effect(self):
        cap = get_node_capability("relight")
        assert cap.effect_type == "relight"
        assert cap.action_type == "effect"
        assert cap.roi_policy == "identity"
        assert cap.tile_padding == 0
        assert cap.is_placeholder is False

    def test_per_pixel_roi(self):
        node = _make_node()
        roi = BBox(x=8, y=8, width=32, height=16)
        out = node.compute_input_roi(roi, dict(node.parameters))
        assert out["image"] == roi
        assert out["normal"] == roi
        assert out["position"] == roi
        assert node.requires_roi_expansion(dict(node.parameters)) == 0


class TestMarker:
    def _tex(self, tex_id=7):
        return TextureResult(texture_id=tex_id, width=_W, height=_H,
                             bbox=BBox.full_frame(_W, _H),
                             alpha_premultiplied=True, color_space="linear")

    def _evaluate(self, node, inputs):
        return node.evaluate(
            inputs, object(), object(), object(), render_mode="full")

    def test_marker_contract_with_position(self):
        node = _make_node(light_type="point")
        image, normal, position = self._tex(1), self._tex(2), self._tex(3)
        out = self._evaluate(node, {"image": image, "normal": normal,
                                    "position": position})
        assert out["_effect_type"] == "relight"
        assert out["_effect_input"] is image
        assert out["_effect_normal"] is normal
        assert out["_effect_position"] is position
        assert out["_effect_params"]["light_type"] == "point"
        assert out["result"].width == _W and out["result"].height == _H

    def test_position_is_optional(self):
        node = _make_node()
        out = self._evaluate(node, {"image": self._tex(1),
                                    "normal": self._tex(2)})
        assert "_effect_position" not in out
        assert out["_effect_params"]["light_type"] == "directional"

    def test_missing_normal_emits_nothing(self):
        node = _make_node()
        out = self._evaluate(node, {"image": self._tex(1)})
        assert out == {}

    def test_missing_image_emits_nothing(self):
        node = _make_node()
        out = self._evaluate(node, {"normal": self._tex(2)})
        assert out == {}

    def test_invalid_light_type_falls_back(self):
        node = _make_node(light_type="spot")
        out = self._evaluate(node, {"image": self._tex(1),
                                    "normal": self._tex(2)})
        assert out["_effect_params"]["light_type"] == "directional"


class TestSerialization:
    def test_roundtrip_preserves_light_params(self, qapp):
        node = _make_node(light_type="point", intensity=2.5, ambient=0.25,
                          light_position_x=-3.0, normal_flip_y=True)
        clone = RelightNode.from_dict(node.to_dict())
        assert clone.parameters["light_type"] == "point"
        assert clone.parameters["intensity"] == pytest.approx(2.5)
        assert clone.parameters["ambient"] == pytest.approx(0.25)
        assert clone.parameters["light_position_x"] == pytest.approx(-3.0)
        assert clone.parameters["normal_flip_y"] is True

    def test_new_params_merge_defaults_for_old_graphs(self, qapp):
        clone = RelightNode.from_dict({
            "id": "r", "type": "relight",
            "parameters": {"intensity": 3.0},
        })
        assert clone.parameters["intensity"] == pytest.approx(3.0)
        assert clone.parameters["ambient"] == pytest.approx(0.0)
        assert clone.parameters["normal_decode"] == "encoded"


# ── Real GL: analytic Lambert verification ─────────────────────────────────

def _plane_fixture():
    """Image gradient, N=(0,0,1) plane at z=0, world xy in [0,10]×[0,7.5].

    Returns the raw texture values: image RGBA, world POSITION (RGBA with
    w=1) and the NORMAL AOV already encoded to [0,1].
    """
    yy, xx = np.mgrid[0:_H, 0:_W].astype(np.float32)
    img = np.zeros((_H, _W, 4), dtype=np.float32)
    img[..., 0] = 0.2 + 0.6 * xx / (_W - 1)
    img[..., 1] = 0.3 + 0.4 * yy / (_H - 1)
    img[..., 2] = 0.5
    img[..., 3] = 1.0
    pos = np.zeros((_H, _W, 4), dtype=np.float32)
    pos[..., 0] = 10.0 * xx / (_W - 1)
    pos[..., 1] = 7.5 * yy / (_H - 1)
    pos[..., 2] = 0.0
    pos[..., 3] = 1.0
    n_encoded = np.zeros((_H, _W, 4), dtype=np.float32)
    n_encoded[..., 0] = 0.5
    n_encoded[..., 1] = 0.5
    n_encoded[..., 2] = 1.0
    n_encoded[..., 3] = 1.0
    return img, pos, n_encoded


def _tilted_normal_encoded():
    """N = normalize(1,1,1), encoded to [0,1]."""
    s = 0.5 + 1.0 / (3.0 ** 0.5) / 2.0
    n = np.zeros((_H, _W, 4), dtype=np.float32)
    n[..., 0] = s
    n[..., 1] = s
    n[..., 2] = s
    n[..., 3] = 1.0
    return n


def _analytic_lambert(img, normal_texel, position, params,
                      has_position=True):
    """Closed-form expectation of the relight shader.

    ``normal_texel`` carries the RAW texture values (either [0,1]-encoded
    or signed floats); decoding follows ``params["normal_decode"]``.
    """
    light_type = params.get("light_type", "directional")
    intensity = float(params.get("intensity", 1.0))
    ambient = float(params.get("ambient", 0.0))
    color = np.asarray(params.get("color", (1.0, 1.0, 1.0)),
                       dtype=np.float32)
    decode = params.get("normal_decode", "encoded")
    flip_y = bool(params.get("normal_flip_y", False))

    n = normal_texel.astype(np.float32).copy()
    if decode == "encoded":
        n = n * 2.0 - 1.0
    if flip_y:
        n[..., 1] = -n[..., 1]
    n_len = np.maximum(np.linalg.norm(n[..., :3], axis=-1,
                                      keepdims=True), 1e-6)
    n3 = n[..., :3] / n_len

    light_position = np.asarray(params.get("light_position", (1, 1, 2)),
                                dtype=np.float32)
    light_direction = np.asarray(params.get("light_direction", (0, 0, -1)),
                                 dtype=np.float32)
    if light_type == "point":
        if has_position:
            L = light_position - position[..., :3]
        else:
            L = np.broadcast_to(light_position, position[..., :3].shape
                                ).copy()
    else:
        L = np.broadcast_to(-light_direction, position[..., :3].shape
                            ).copy()
    l_len = np.maximum(np.linalg.norm(L, axis=-1, keepdims=True), 1e-6)
    L = L / l_len

    diffuse = np.maximum(np.sum(n3 * L, axis=-1), 0.0)[..., None]
    result = img.copy()
    result[..., :3] = img[..., :3] * (diffuse * intensity * color
                                      + ambient)
    return result


class TestGLRelight:
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
            """Mimics the shape _get_texture_by_id() expects."""

            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}

            def acquire_texture(self, w, h, components=4, dtype="f4"):
                tex = self.ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                return tex

            def release_texture(self, tex, force_evict=False):
                for lst in self._pooled_textures.values():
                    if tex in lst:
                        lst.remove(tex)
                tex.release()

        class _H(BaseEvaluatorMixin, ColorEvaluatorMixin,
                 FilterEvaluatorMixin):
            def __init__(self, ctx):
                self.ctx = ctx
                self.utility_shaders = {}
                self._resource_pool = _Pool(ctx)

                from src.shared.gpu.gl_backend import FramebufferManager
                self.fb_manager = FramebufferManager(ctx)
                self._load_shaders()

            # Standalone moderngl context: bind via moderngl instead of the
            # production QOpenGLFunctions path (no Qt GL context here).
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

    def _run(self, harness, params, normal, position, img):
        src = self._upload(harness, img)
        nrm = self._upload(harness, normal)
        pos = (self._upload(harness, position)
               if position is not None else None)
        out = harness._apply_relight(src, nrm, pos, dict(params), _W, _H)
        return self._read(harness, out["result"])

    def test_directional_head_on_is_identity(self, harness, qapp):
        harness
        img, pos, nrm = _plane_fixture()
        params = {"light_type": "directional",
                  "light_direction": (0.0, 0.0, -1.0),
                  "intensity": 1.0, "ambient": 0.0, "color": (1, 1, 1),
                  "normal_decode": "encoded", "normal_flip_y": False}
        got = self._run(harness, params, nrm, pos, img)
        np.testing.assert_allclose(got[..., :3], img[..., :3], atol=2e-5)
        np.testing.assert_allclose(got[..., 3], img[..., 3], atol=1e-6)

    def test_point_light_matches_analytic_lambert(self, harness, qapp):
        harness
        img, pos, nrm = _plane_fixture()
        params = {"light_type": "point",
                  "light_position": (10.0, 0.0, 5.0),
                  "intensity": 1.2, "ambient": 0.05,
                  "color": (1.0, 0.9, 0.8),
                  "normal_decode": "encoded", "normal_flip_y": False}
        got = self._run(harness, params, nrm, pos, img)
        expected = _analytic_lambert(img, nrm[..., :3], pos, params)
        np.testing.assert_allclose(got[..., :3], expected[..., :3],
                                   atol=2e-4)

    def test_directional_tilted_matches_analytic(self, harness, qapp):
        harness
        img, pos, _ = _plane_fixture()
        nrm = _tilted_normal_encoded()
        params = {"light_type": "directional",
                  "light_direction": (0.3, -0.4, -1.0),
                  "intensity": 1.5, "ambient": 0.1, "color": (1, 1, 1),
                  "normal_decode": "encoded", "normal_flip_y": False}
        got = self._run(harness, params, nrm, pos, img)
        expected = _analytic_lambert(img, nrm[..., :3], pos, params)
        np.testing.assert_allclose(got[..., :3], expected[..., :3],
                                   atol=2e-4)

    def test_flip_y_changes_result_per_analytic(self, harness, qapp):
        harness
        img, pos, _ = _plane_fixture()
        nrm = _tilted_normal_encoded()
        base = {"light_type": "directional",
                "light_direction": (0.0, -0.6, -1.0),
                "intensity": 1.0, "ambient": 0.0, "color": (1, 1, 1),
                "normal_decode": "encoded"}
        got_flip = self._run(harness, dict(base, normal_flip_y=True),
                             nrm, pos, img)
        expected_flip = _analytic_lambert(
            img, nrm[..., :3], pos, dict(base, normal_flip_y=True))
        np.testing.assert_allclose(got_flip[..., :3],
                                   expected_flip[..., :3], atol=2e-4)
        # And flipping genuinely differs from not flipping for this field.
        got_noflip = self._run(harness, dict(base, normal_flip_y=False),
                               nrm, pos, img)
        assert not np.allclose(got_flip, got_noflip, atol=1e-4)

    def test_signed_decode_consumes_raw_values(self, harness, qapp):
        harness
        img, pos, _ = _plane_fixture()
        # Signed texture carrying N = normalize(1,1,1) directly.
        s = 1.0 / (3.0 ** 0.5)
        nrm = np.zeros((_H, _W, 4), dtype=np.float32)
        nrm[..., 0] = s
        nrm[..., 1] = s
        nrm[..., 2] = s
        nrm[..., 3] = 1.0
        params = {"light_type": "directional",
                  "light_direction": (0.0, 0.0, -1.0),
                  "intensity": 2.0, "ambient": 0.0, "color": (1, 1, 1),
                  "normal_decode": "signed", "normal_flip_y": False}
        got = self._run(harness, params, nrm, pos, img)
        expected = _analytic_lambert(img, nrm[..., :3], pos, params)
        np.testing.assert_allclose(got[..., :3], expected[..., :3],
                                   atol=2e-4)

    def test_point_without_position_uses_direction_fallback(self,
                                                            harness, qapp):
        harness
        img, pos, nrm = _plane_fixture()
        params = {"light_type": "point",
                  "light_position": (0.0, 0.0, 1.0),
                  "intensity": 1.0, "ambient": 0.0, "color": (1, 1, 1),
                  "normal_decode": "encoded", "normal_flip_y": False}
        got = self._run(harness, params, nrm, None, img)
        # L = normalize((0,0,1)) → diffuse = 1 everywhere → identity.
        np.testing.assert_allclose(got[..., :3], img[..., :3], atol=2e-5)
