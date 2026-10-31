"""C7 mesh warp tests — identity pass-through, single-point locality on a
real GL context, tile padding bounds, dynamic point channels, and
serialization.
"""

import json
import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.core.node_engine import NODE_REGISTRY, NODE_CATEGORIES
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.warp_node import WarpNode

pytestmark = pytest.mark.composite_core


def _make_node(**overrides):
    node = WarpNode(node_id="w1")
    for k, v in overrides.items():
        node.set_parameter(k, v)
    return node


class TestRegistration:
    def test_registered_in_filter_category(self):
        assert NODE_REGISTRY.get("mesh_warp") is WarpNode
        assert any(t == "mesh_warp"
                   for t, _ in NODE_CATEGORIES.get("Filter", []))

    def test_aliases(self):
        assert "网格变形" in WarpNode.SEARCH_ALIASES
        assert "变形" in WarpNode.SEARCH_ALIASES


class TestPointChannels:
    def test_dynamic_defs_cover_lattice(self, qapp):
        node = _make_node(grid_size=4)
        defs = node.get_param_defs()
        assert "warp_point_0_dx" in defs and "warp_point_24_dy" in defs
        assert len(defs["warp_point_0_dx"].group or "") >= 0
        assert defs["warp_point_0_dx"].is_animatable()

    def test_numeric_channel_updates_json(self, qapp):
        node = _make_node(grid_size=4)
        node.set_parameter("warp_point_5_dx", 0.05)
        node.set_parameter("warp_point_5_dy", -0.02)
        offsets = node.get_offsets()
        assert offsets[5] == [pytest.approx(0.05), pytest.approx(-0.02)]
        # Reverse direction: writing JSON refreshes the channels.
        node.set_offsets([[0.1, 0.2]] + [[0.0, 0.0]] * 24)
        assert node.parameters["warp_point_0_dx"] == pytest.approx(0.1)

    def test_overrides_apply_interpolated_values(self, qapp):
        node = _make_node(grid_size=4)
        node.set_parameter("warp_point_3_dx", 0.5)
        offsets = WarpNode.apply_point_parameter_overrides(
            node.get_offsets(),
            {"warp_point_3_dx": 0.25, "warp_point_3_dy": -0.25})
        assert offsets[3] == [pytest.approx(0.25), pytest.approx(-0.25)]

    def test_grid_change_rebuilds_channels(self, qapp):
        node = _make_node(grid_size=4)
        node.set_parameter("grid_size", 8)
        assert "warp_point_80_dx" in node.get_param_defs()


class TestTilePadding:
    def test_identity_grid_needs_no_padding(self):
        node = _make_node(grid_size=4)
        params = dict(node.parameters, width=1920.0)
        assert node._tile_padding(params) == 0
        assert node.requires_roi_expansion(params) == 0

    def test_padding_scales_with_max_displacement(self):
        node = _make_node(grid_size=4)
        offsets = [[0.0, 0.0]] * 25
        offsets[12] = [0.1, 0.0]  # 10% of width
        node.set_offsets(offsets)
        params = dict(node.parameters, width=1000.0)
        assert node._tile_padding(params) == 100

    def test_compute_input_roi_expands(self):
        node = _make_node(grid_size=4)
        offsets = [[0.0, 0.0]] * 25
        offsets[12] = [0.1, 0.0]
        node.set_offsets(offsets)
        roi = BBox(x=10, y=10, width=50, height=50)
        ref = BBox(x=0, y=0, width=1000, height=1000)
        out = node.compute_input_roi(roi, dict(node.parameters),
                                     {"image": ref})
        assert out["image"].width >= roi.width + 100
        assert out["image"].x <= roi.x - 100


class TestSerialization:
    def test_roundtrip_preserves_offsets(self, qapp):
        node = _make_node(grid_size=4)
        offsets = [[0.0, 0.0]] * 25
        offsets[7] = [0.03, -0.04]
        node.set_offsets(offsets)
        clone = WarpNode.from_dict(node.to_dict())
        assert clone.get_offsets()[7] == node.get_offsets()[7]
        assert clone.parameters["grid_size"] == 4

    def test_legacy_dict_without_offsets(self, qapp):
        clone = WarpNode.from_dict({"id": "w", "type": "mesh_warp",
                                    "parameters": {"grid_size": 4}})
        assert all(dx == 0.0 and dy == 0.0 for dx, dy in clone.get_offsets())


# ── Real GL: identity pass-through + single-point locality ──────────────────

class TestGLWarp:
    @pytest.fixture()
    def harness(self):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )

        class _Pool:
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

        class _H(BaseEvaluatorMixin, FilterEvaluatorMixin):
            def __init__(self, ctx):
                # Only the pieces _apply_mesh_warp touches.
                self.ctx = ctx
                self.utility_shaders = {}
                self._resource_pool = _Pool(ctx)

                from src.shared.gpu.gl_backend import FramebufferManager
                self.fb_manager = FramebufferManager(ctx)

                self._load_shaders()

                def _bind_external_texture(tex_id):
                    tex = self._get_texture_by_id(tex_id)
                    tex.use(0)

                self._bind_external_texture = _bind_external_texture

        yield _H(ctx)
        ctx.release()

    def _upload(self, harness, rgba_premult):
        h, w = rgba_premult.shape[:2]
        tex = harness.fb_manager.get_temp_texture(w, h, components=4,
                                                  dtype="f4")
        tex.write(np.ascontiguousarray(np.flipud(rgba_premult)).tobytes())
        return TextureResult(
            texture_id=tex.glo, width=w, height=h,
            bbox=BBox.full_frame(w, h),
            alpha_premultiplied=True, color_space="linear")

    def _read(self, harness, tex_result):
        tex = harness._get_texture_by_id(tex_result.texture_id)
        fbo = harness.fb_manager.get_fbo(tex)
        fbo.use()
        harness.ctx.viewport = (0, 0, tex_result.width, tex_result.height)
        raw = fbo.read(components=4, dtype="f4")
        arr = np.frombuffer(raw, dtype=np.float32).reshape(
            tex_result.height, tex_result.width, 4)
        return np.flipud(arr).copy()

    def test_identity_grid_is_exact_pass_through(self, harness, qapp):
        harness  # loads shaders
        yy, xx = np.mgrid[0:48, 0:64].astype(np.float32)
        img = np.stack([xx / 64, yy / 48, np.full_like(xx, 0.5)],
                       axis=-1)[..., :3]
        img = np.concatenate([img, np.full((48, 64, 1), 0.8,
                                           dtype=np.float32)], axis=-1)
        img[..., :3] *= img[..., 3:4]
        src = self._upload(harness, img)

        node = _make_node(grid_size=4)
        params = dict(node.parameters,
                      offsets=node.get_offsets(),
                      grid_size=4)
        # Marker nodes dispatch via _dispatch_effect; here we call the
        # warp application directly (the dispatch branch forwards to it).
        out = harness._apply_mesh_warp(src, params, 64, 48)
        got = self._read(harness, out["result"])
        np.testing.assert_allclose(got, img, atol=1e-5)

    def test_single_point_displacement_is_local(self, harness, qapp):
        harness
        yy, xx = np.mgrid[0:48, 0:64].astype(np.float32)
        img = np.stack([xx / 64, yy / 48, np.full_like(xx, 0.5)],
                       axis=-1)[..., :3]
        img = np.concatenate([img, np.full((48, 64, 1), 0.8,
                                           dtype=np.float32)], axis=-1)
        img[..., :3] *= img[..., 3:4]
        src = self._upload(harness, img)

        node = _make_node(grid_size=4)
        # Move the centre lattice point (row 2, col 2 → index 12) by 10%
        # of width to the right. Sample = dst - d, so content flows left.
        offsets = [[0.0, 0.0]] * 25
        offsets[12] = [0.1, 0.0]
        node.set_offsets(offsets)
        params = dict(node.parameters,
                      offsets=node.get_offsets(),
                      grid_size=4)
        out = harness._apply_mesh_warp(src, params, 64, 48)
        got = self._read(harness, out["result"])

        # Far corner (inside cell 0,0 — its corners are undisplaced) must
        # be untouched; centre pixels must change.
        np.testing.assert_allclose(got[4, 4], img[4, 4], atol=1e-4)
        assert not np.allclose(got[24, 24:40], img[24, 24:40], atol=1e-4)
        # Identity elsewhere: top row (cell row 0) untouched.
        np.testing.assert_allclose(got[2, :], img[2, :], atol=1e-4)
