"""A2 Cryptomatte Select node tests — registration, selection-set API,
manifest remap by name, serialization, and GPU matte/isolate verification.
"""

import json
import os
import struct
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.core.node_capabilities import get_node_capability
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.cryptomatte_select_node import (
    MAX_SELECTED,
    CryptomatteSelectNode,
    decode_rgba24_id,
)
from src.features.composite.render3d.cryptomatte import (
    CryptomatteManifest,
    cryptomatte_float_id,
)

pytestmark = pytest.mark.composite_core

_W, _H = 64, 48
_ID_A = 17      # left half
_ID_B = 523     # right half


def _make_node(**overrides):
    node = CryptomatteSelectNode(node_id="cs1")
    for k, v in overrides.items():
        node.set_parameter(k, v)
    return node


def _id_image_rgba24():
    """Left half object A, right half object B, background id 0."""
    img = np.zeros((_H, _W, 4), dtype=np.float32)
    img[..., 3] = 1.0
    img[:, :_W // 2, 0] = _ID_A % 256 / 255.0
    img[:, :_W // 2, 1] = (_ID_A // 256) % 256 / 255.0
    img[:, _W // 2:, 0] = _ID_B % 256 / 255.0
    img[:, _W // 2:, 1] = (_ID_B // 256) % 256 / 255.0
    return img


class TestRegistration:
    def test_registered_in_keyer_category(self):
        assert NODE_REGISTRY.get("cryptomatte_select") is CryptomatteSelectNode
        assert any(t == "cryptomatte_select"
                   for t, _ in NODE_CATEGORIES.get("Keyer", []))

    def test_aliases(self):
        assert "Cryptomatte 抠选" in CryptomatteSelectNode.SEARCH_ALIASES
        assert "ID 抠像" in CryptomatteSelectNode.SEARCH_ALIASES

    def test_capability(self):
        cap = get_node_capability("cryptomatte_select")
        assert cap.effect_type == "cryptomatte_select"
        assert cap.action_type == "effect"
        assert cap.tile_padding == 0

    def test_decode_roundtrip(self):
        assert decode_rgba24_id((_ID_A % 256 / 255.0,
                                 (_ID_A // 256) % 256 / 255.0, 0.0,
                                 1.0)) == _ID_A
        assert decode_rgba24_id((0.0, 0.0, 0.0, 1.0)) == 0


class TestSelectionAPI:
    def test_add_toggle_remove(self):
        node = _make_node()
        assert node.toggle_selection(float(_ID_A), "cube") is True
        assert node.get_selected_ids() == [float(_ID_A)]
        # Idempotent add.
        node.add_selection(float(_ID_A), "cube")
        assert len(node.get_selected_ids()) == 1
        assert node.toggle_selection(float(_ID_A)) is False
        assert node.get_selected_ids() == []

    def test_multi_select_union_order(self):
        node = _make_node()
        node.add_selection(float(_ID_B), "sphere")
        node.add_selection(float(_ID_A), "cube")
        assert node.get_selected_ids() == [float(_ID_B), float(_ID_A)]
        assert node.get_selected_names() == ["sphere", "cube"]

    def test_selection_capped_at_max(self):
        node = _make_node()
        for i in range(MAX_SELECTED + 10):
            node.add_selection(float(i + 1))
        assert len(node.get_selected_ids()) == MAX_SELECTED


class TestManifestRemap:
    def test_float_r_selection_resolved_by_name(self):
        node = _make_node(id_encoding="float_r")
        manifest = CryptomatteManifest.from_names(["cube", "sphere"])
        node.set_manifest(manifest)
        # Simulate a pick: the manifest float id for "cube".
        cube_id = cryptomatte_float_id("cube")
        node.add_selection(cube_id, "cube")
        # Manifest change: same name, hash resolves to the same float id.
        node.set_manifest(CryptomatteManifest.from_names(
            ["cube", "sphere", "floor"]))
        assert node.get_selected_ids() == [pytest.approx(cube_id)]
        assert node.remap_selection() == []

    def test_offline_name_keeps_id_and_is_reported(self):
        node = _make_node(id_encoding="float_r")
        node.set_manifest(CryptomatteManifest.from_names(["cube"]))
        node.add_selection(cryptomatte_float_id("cube"), "cube")
        # New manifest without "cube": remap reports it offline, id kept.
        node.set_manifest(CryptomatteManifest.from_names(["sphere"]))
        offline = node.remap_selection()
        assert offline == ["cube"]
        assert node.get_selected_ids() == [
            pytest.approx(cryptomatte_float_id("cube"))]

    def test_name_for_id_reverse_lookup(self):
        node = _make_node(id_encoding="float_r")
        node.set_manifest(CryptomatteManifest.from_names(["cube", "ball"]))
        assert node.name_for_id(
            cryptomatte_float_id("ball")) == "ball"
        assert node.name_for_id(12345.0) == ""

    def test_rgba24_ids_are_kept_as_is(self):
        node = _make_node(id_encoding="rgba24")
        node.set_manifest(CryptomatteManifest.from_names(["cube"]))
        node.add_selection(float(_ID_A), "cube")
        node.remap_selection()
        assert node.get_selected_ids() == [float(_ID_A)]


class TestMarkerAndSerialization:
    def _tex(self, tex_id=9):
        return TextureResult(texture_id=tex_id, width=_W, height=_H,
                             bbox=BBox.full_frame(_W, _H),
                             alpha_premultiplied=False,
                             color_space="linear")

    def test_marker_contract(self):
        node = _make_node()
        node.add_selection(float(_ID_A), "cube")
        out = node.evaluate({"image": self._tex()}, object(), object(),
                            object(), render_mode="full")
        assert out["_effect_type"] == "cryptomatte_select"
        assert out["_effect_params"]["selected_ids"] == [float(_ID_A)]
        assert out["_effect_params"]["output_mode"] == "matte"
        assert out["result"].alpha_premultiplied is True

    def test_empty_selection_still_emits(self):
        node = _make_node()
        out = node.evaluate({"image": self._tex()}, object(), object(),
                            object(), render_mode="full")
        assert out["_effect_params"]["selected_ids"] == []

    def test_missing_input_emits_nothing(self):
        node = _make_node()
        assert node.evaluate({}, object(), object(), object()) == {}

    def test_serialization_roundtrip(self, qapp):
        node = _make_node(id_encoding="float_r", output_mode="isolate")
        node.set_manifest(CryptomatteManifest.from_names(["cube"]))
        node.add_selection(cryptomatte_float_id("cube"), "cube")
        clone = CryptomatteSelectNode.from_dict(node.to_dict())
        assert clone.parameters["id_encoding"] == "float_r"
        assert clone.parameters["output_mode"] == "isolate"
        assert clone.get_selected_ids() == node.get_selected_ids()
        assert clone.get_selected_names() == ["cube"]
        assert clone.get_manifest() == node.get_manifest()
        # The clone is a plain roto-like data node: no AI dependencies.
        assert clone.remap_selection() == []


# ── Real GL: matte / isolate correctness ───────────────────────────────────

class TestGLCryptomatteSelect:
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

    def test_matte_is_exactly_selected_region(self, harness, qapp):
        harness
        img = _id_image_rgba24()
        src = self._upload(harness, img)
        out = harness._apply_cryptomatte_select(
            src, {"id_encoding": "rgba24", "output_mode": "matte",
                  "selected_ids": [float(_ID_A)]}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :_W // 2, 3], 1.0)
        assert np.allclose(got[:, _W // 2:, 3], 0.0)
        # Matte is white where covered.
        assert np.allclose(got[:, :_W // 2, :3], 1.0)

    def test_multi_select_union(self, harness, qapp):
        harness
        img = _id_image_rgba24()
        src = self._upload(harness, img)
        out = harness._apply_cryptomatte_select(
            src, {"id_encoding": "rgba24", "output_mode": "matte",
                  "selected_ids": [float(_ID_A), float(_ID_B)]}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got[..., 3], 1.0)

    def test_empty_selection_is_black_matte(self, harness, qapp):
        harness
        img = _id_image_rgba24()
        src = self._upload(harness, img)
        out = harness._apply_cryptomatte_select(
            src, {"id_encoding": "rgba24", "output_mode": "matte",
                  "selected_ids": []}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got, 0.0)

    def test_isolate_keeps_selected_pixels_only(self, harness, qapp):
        harness
        img = _id_image_rgba24()
        src = self._upload(harness, img)
        out = harness._apply_cryptomatte_select(
            src, {"id_encoding": "rgba24", "output_mode": "isolate",
                  "selected_ids": [float(_ID_A)]}, _W, _H)
        got = self._read(harness, out["result"])
        np.testing.assert_allclose(got[:, :_W // 2], img[:, :_W // 2],
                                   atol=1e-6)
        assert np.allclose(got[:, _W // 2:, :3], 0.0)

    def test_float_r_mode_matches_hash_ids(self, harness, qapp):
        harness
        name_l, name_r = "cube", "sphere"
        fid_l = cryptomatte_float_id(name_l)
        fid_r = cryptomatte_float_id(name_r)
        img = np.zeros((_H, _W, 4), dtype=np.float32)
        img[..., 0] = fid_l
        img[:, _W // 2:, 0] = fid_r
        img[..., 3] = 1.0
        src = self._upload(harness, img)
        out = harness._apply_cryptomatte_select(
            src, {"id_encoding": "float_r", "output_mode": "matte",
                  "selected_ids": [fid_r]}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, _W // 2:, 3], 1.0)
        assert np.allclose(got[:, :_W // 2, 3], 0.0)
