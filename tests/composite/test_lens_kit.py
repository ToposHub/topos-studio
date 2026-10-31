"""B4 lens kit tests — chromatic aberration / vignette / lens flare.

Registration, marker contracts, ROI, serialization, the Python-mirror
vignette formula, and GL verification: CA channel displacement on a ramp,
vignette centre-vs-corner exposure, and flare ghost positions on the
centre<->light axis.
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
from src.features.composite.nodes.chromatic_aberration_node import (
    ChromaticAberrationNode,
)
from src.features.composite.nodes.lens_flare_node import LensFlareNode
from src.features.composite.nodes.vignette_node import (
    VignetteNode,
    vignette_factor,
)

pytestmark = pytest.mark.composite_core

_W, _H = 64, 48


class TestRegistration:
    @pytest.mark.parametrize("node_type,cls,category", [
        ("chromatic_aberration", ChromaticAberrationNode, "Filter"),
        ("vignette", VignetteNode, "Filter"),
        ("lens_flare", LensFlareNode, "Filter"),
    ])
    def test_registered(self, node_type, cls, category):
        assert NODE_REGISTRY.get(node_type) is cls
        assert any(t == node_type
                   for t, _ in NODE_CATEGORIES[category])
        cap = get_node_capability(node_type)
        assert cap.effect_type == node_type
        assert cap.action_type == "effect"

    def test_ca_aliases(self):
        assert "色差" in ChromaticAberrationNode.SEARCH_ALIASES

    def test_vignette_aliases(self):
        assert "暗角" in VignetteNode.SEARCH_ALIASES

    def test_flare_aliases(self):
        assert "光晕" in LensFlareNode.SEARCH_ALIASES
        assert "镜头光斑" in LensFlareNode.SEARCH_ALIASES


class TestMarkerContracts:
    def _tex(self, tex_id=6):
        return TextureResult(texture_id=tex_id, width=_W, height=_H,
                             bbox=BBox.full_frame(_W, _H),
                             alpha_premultiplied=True,
                             color_space="linear")

    def test_ca_marker(self):
        node = ChromaticAberrationNode(node_id="ca")
        out = node.evaluate({"image": self._tex(1)}, object(), object(),
                            object(), render_mode="full")
        params = out["_effect_params"]
        assert params["offset_r"] == 2.0
        assert params["offset_b"] == -2.0
        assert params["center"] == (0.5, 0.5)

    def test_ca_preview_halves_offsets(self):
        node = ChromaticAberrationNode(node_id="ca", )
        node.set_parameter("offset_r", 8.0)
        out = node.evaluate({"image": self._tex(1)}, object(), object(),
                            object(), render_mode="preview")
        assert out["_effect_params"]["offset_r"] == 4.0

    def test_ca_padding_tracks_offsets(self):
        node = ChromaticAberrationNode(node_id="ca")
        node.parameters["offset_r"] = 10.0
        node.parameters["offset_b"] = -6.0
        assert node._tile_padding(node.parameters) == 10

    def test_vignette_marker(self):
        node = VignetteNode(node_id="v", )
        node.set_parameter("amount", 0.8)
        out = node.evaluate({"image": self._tex(1)}, object(), object(),
                            object())
        assert out["_effect_params"]["amount"] == 0.8
        assert out["_effect_params"]["radius"] == 0.7

    def test_flare_marker(self):
        node = LensFlareNode(node_id="f")
        out = node.evaluate({"image": self._tex(1)}, object(), object(),
                            object(), render_mode="full")
        params = out["_effect_params"]
        assert params["light_pos"] == (0.3, 0.4)
        assert params["ghost_count"] == 4
        assert params["blend_mode"] == 0  # screen

    def test_flare_preview_halves_ghosts(self):
        node = LensFlareNode(node_id="f")
        out = node.evaluate({"image": self._tex(1)}, object(), object(),
                            object(), render_mode="preview")
        assert out["_effect_params"]["ghost_count"] == 2

    def test_flare_full_frame_roi(self):
        node = LensFlareNode(node_id="f")
        input_full = BBox(x=0, y=0, width=_W, height=_H)
        out = node.compute_input_roi(BBox(x=8, y=8, width=4, height=4),
                                     node.parameters,
                                     {"image": input_full})
        assert out["image"] == input_full

    @pytest.mark.parametrize("node_cls", [ChromaticAberrationNode,
                                          VignetteNode, LensFlareNode])
    def test_missing_input_emits_nothing(self, node_cls):
        node = node_cls(node_id="x")
        assert node.evaluate({}, object(), object(), object()) == {}


class TestVignetteMath:
    def test_python_mirror_centre_is_untouched(self):
        assert vignette_factor(0.0, 0.0, 0.7, 0.4, 0.5, 0.0) == 1.0

    def test_amount_zero_is_identity(self):
        assert vignette_factor(0.4, 0.4, 0.7, 0.4, 0.0, 0.0) == 1.0

    def test_corner_darkens_with_amount(self):
        f1 = vignette_factor(0.45, 0.45, 0.7, 0.4, 0.5, 0.0)
        f2 = vignette_factor(0.45, 0.45, 0.7, 0.4, 1.0, 0.0)
        assert f1 < 1.0 and f2 < f1

    def test_roundness_changes_distance_metric(self):
        # Point on the diagonal vs on the axis: roundness compresses the
        # superellipse toward a square, changing which point is darker.
        diag = vignette_factor(0.4, 0.4, 0.7, 0.4, 1.0, 0.0)
        diag_round = vignette_factor(0.4, 0.4, 0.7, 0.4, 1.0, 1.0)
        assert diag != diag_round


class TestSerialization:
    def test_ca_roundtrip(self, qapp):
        node = ChromaticAberrationNode(node_id="ca")
        node.set_parameter("offset_r", 5.0)
        node.set_parameter("edge_boost", 0.7)
        clone = ChromaticAberrationNode.from_dict(node.to_dict())
        assert clone.parameters["offset_r"] == pytest.approx(5.0)
        assert clone.parameters["edge_boost"] == pytest.approx(0.7)

    def test_vignette_roundtrip(self, qapp):
        node = VignetteNode(node_id="v")
        node.set_parameter("roundness", 0.6)
        clone = VignetteNode.from_dict(node.to_dict())
        assert clone.parameters["roundness"] == pytest.approx(0.6)

    def test_flare_roundtrip(self, qapp):
        node = LensFlareNode(node_id="f")
        node.set_parameter("ghost_spacing", -0.8)
        node.set_parameter("blend_mode", "add")
        clone = LensFlareNode.from_dict(node.to_dict())
        assert clone.parameters["ghost_spacing"] == pytest.approx(-0.8)
        assert clone.parameters["blend_mode"] == "add"


# ── Real GL ────────────────────────────────────────────────────────────────

class _LensHarnessBase:
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


class TestGLChromaticAberration(_LensHarnessBase):
    def test_constant_shift_displaces_channels(self, harness, qapp):
        harness
        # Horizontal ramp: bilinear sampling at fractional offsets is exact.
        img = np.zeros((_H, _W, 4), dtype=np.float32)
        ramp = np.linspace(0.0, 1.0, _W, dtype=np.float32)[None, :]
        img[..., 0] = ramp
        img[..., 1] = 0.5
        img[..., 2] = ramp  # ramp in B too so its shift is observable
        img[..., 3] = 1.0
        src = self._upload(harness, img)
        params = {"offset_r": 4.0, "offset_b": -4.0, "center": (0.5, 0.5),
                  "edge_boost": 0.0}
        out = harness._apply_chromatic_aberration(src, params, _W, _H)
        got = self._read(harness, out["result"])

        col = 20
        # Radial dir at the left half points left; R shifts 4 px toward
        # the centre (right), B 4 px away (left).
        # uv x of col 20 = (20+0.5)/64; +4 px in uv = 4/64.
        x_uv = (col + 0.5) / _W
        r_expected = np.interp(
            (x_uv + 4.0 / _W) * _W - 0.5,
            np.arange(_W), np.linspace(0.0, 1.0, _W))
        b_expected = np.interp(
            (x_uv - 4.0 / _W) * _W - 0.5,
            np.arange(_W), np.linspace(0.0, 1.0, _W))
        assert got[col // 1, col, 0] == pytest.approx(r_expected,
                                                      abs=0.02)
        assert got[24, col, 2] == pytest.approx(b_expected, abs=0.02)
        # G unchanged.
        assert got[24, col, 1] == pytest.approx(0.5, abs=1e-5)


class TestGLVignette(_LensHarnessBase):
    def test_centre_untouched_corner_darkens(self, harness, qapp):
        harness
        img = np.full((_H, _W, 4), 0.8, dtype=np.float32)
        img[..., 3] = 1.0
        src = self._upload(harness, img)
        params = {"amount": 1.0, "radius": 0.5, "softness": 0.5,
                  "roundness": 0.0}
        out = harness._apply_vignette(src, params, _W, _H)
        got = self._read(harness, out["result"])

        # Centre pixel: d≈0 → factor 1.
        assert got[_H // 2, _W // 2, 0] == pytest.approx(0.8, abs=0.01)
        # Corner: well past the radius → factor 1 - amount = 0.
        assert got[1, 1, 0] < 0.05
        # Alpha untouched.
        np.testing.assert_allclose(got[..., 3], 1.0, atol=1e-5)

    def test_amount_zero_is_passthrough(self, harness, qapp):
        harness
        img = np.full((_H, _W, 4), 0.8, dtype=np.float32)
        img[..., 3] = 1.0
        src = self._upload(harness, img)
        params = {"amount": 0.0, "radius": 0.5, "softness": 0.5,
                  "roundness": 0.0}
        out = harness._apply_vignette(src, params, _W, _H)
        got = self._read(harness, out["result"])
        np.testing.assert_allclose(got[..., 0], 0.8, atol=1e-5)


class TestGLLensFlare(_LensHarnessBase):
    def test_ghost_chain_lies_on_centre_light_axis(self, harness, qapp):
        harness
        img = np.zeros((_H, _W, 4), dtype=np.float32)
        img[..., 3] = 1.0
        src = self._upload(harness, img)
        params = {"light_pos": (0.2, 0.5), "light_color": (1.0, 1.0, 1.0),
                  "intensity": 3.0, "ghost_count": 4,
                  "ghost_spacing": 0.5, "ghost_decay": 1.0,
                  "ghost_size": 0.05, "halo_radius": 0.25,
                  "halo_strength": 0.0, "streak_strength": 0.0,
                  "streak_width": 0.01, "use_occlusion": False,
                  "blend_mode": 1}
        out = harness._apply_lens_flare(src, params, _W, _H)
        got = self._read(harness, out["result"])

        # Ghost i sits at light + (centre - light)*(1 + spacing·i)
        # = x_uv = 0.2 + 0.3*(1+0.5i) → i=1..4 → 0.65, 0.80, 0.95, 1.10.
        # The glow blob is circular in screen space, so allow a width
        # tolerance of one ghost radius rather than checking a hard edge.
        row = _H // 2
        bright_cols = np.where(got[row, :, 0] > 0.1)[0]
        assert len(bright_cols) > 0
        bright_x = (bright_cols + 0.5) / _W
        for expected in (0.65, 0.80, 0.95):
            assert np.any(np.abs(bright_x - expected) < 0.06), (
                f"no ghost found near x={expected}")

    def test_zero_ghosts_and_streaks_yields_black(self, harness, qapp):
        harness
        img = np.zeros((_H, _W, 4), dtype=np.float32)
        img[..., 3] = 1.0
        src = self._upload(harness, img)
        params = {"light_pos": (0.5, 0.5), "light_color": (1.0, 1.0, 1.0),
                  "intensity": 3.0, "ghost_count": 0,
                  "ghost_spacing": 0.5, "ghost_decay": 1.0,
                  "ghost_size": 0.05, "halo_radius": 0.25,
                  "halo_strength": 0.0, "streak_strength": 0.0,
                  "streak_width": 0.01, "use_occlusion": False,
                  "blend_mode": 1}
        out = harness._apply_lens_flare(src, params, _W, _H)
        got = self._read(harness, out["result"])
        np.testing.assert_allclose(got[..., :3], 0.0, atol=1e-5)
