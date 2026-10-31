"""A4: Lens Distortion node — model math, registration, tile safety."""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_capabilities import (
    get_node_capability, is_placeholder_node_type)
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.nodes.lens_distortion_node import (
    LensDistortionNode, distortion_delta, map_point)

pytestmark = pytest.mark.composite_core


class TestRegistration:

    def test_registered_in_filter_category(self):
        assert "lens_distortion" in NODE_REGISTRY
        assert NODE_REGISTRY["lens_distortion"] is LensDistortionNode
        assert not is_placeholder_node_type("lens_distortion")
        cap = get_node_capability("lens_distortion")
        assert cap.action_type == "effect"
        assert cap.effect_type == "lens_distortion"

    def test_shader_registered(self):
        from src.features.composite.evaluators import shader_library
        names = getattr(shader_library, "SHADER_SOURCES", None) or \
            getattr(shader_library, "SHADERS", None)
        src = None
        for attr in ("SHADER_SOURCES", "SHADERS", "_SHADERS"):
            d = getattr(shader_library, attr, None)
            if isinstance(d, dict) and "lens_distortion" in d:
                src = d
                break
        if src is None:
            # fall back: the shader source itself must exist
            from src.features.composite.core import shaders
            assert hasattr(shaders, "_LENS_DISTORTION_FRAGMENT")
            assert "distortion_delta" in shaders._LENS_DISTORTION_FRAGMENT
            # constant loop bound (GLSL rule)
            assert "i < 12" in shaders._LENS_DISTORTION_FRAGMENT
        else:
            assert "lens_distortion" in src


class TestModelMath:

    def test_identity_when_all_zero(self):
        for px, py in ((100.0, 100.0), (0.0, 0.0), (640.0, 360.0)):
            for mode in ("undistort", "distort"):
                x, y = map_point(px, py, 1280.0, 720.0,
                                 0, 0, 0, 0, 0, mode=mode)
                assert (x, y) == pytest.approx((px, py))

    def test_positive_k1_pushes_points_outward_in_distort(self):
        # a corner is farther from center after distortion with k1 > 0
        cx, cy, w, h = 640.0, 360.0, 1280.0, 720.0
        px, py = 1200.0, 650.0
        ax, ay = (px - cx) / w, (py - cy) / h
        # forward position: undistorted + displacement; k1 > 0 must push it
        # radially outward (monotone in k1 for r > 0)
        def rad(k1):
            dx, dy = distortion_delta(ax, ay, k1, 0, 0, 0, 0)
            return math.hypot(ax + dx, ay + dy)
        assert rad(0.0) == pytest.approx(math.hypot(ax, ay))
        assert rad(0.3) > rad(0.0) > 0.0
        assert rad(-0.3) < rad(0.0)

    def test_undistort_inverts_distort(self):
        # undistort( distort(p) ) ≈ p  — point round trip, small k
        w, h = 1920.0, 1080.0
        k = (0.15, -0.05, 0.0, 0.005, -0.003)
        max_err = 0.0
        for px, py in ((100.0, 80.0), (1700.0, 990.0), (960.0, 540.0),
                       (300.0, 900.0)):
            # forward: closed form
            fx, fy = map_point(px, py, w, h, *k, mode="distort")
            # back: iterative inverse
            bx, by = map_point(fx, fy, w, h, *k, mode="undistort")
            max_err = max(max_err, math.hypot(bx - px, by - py))
        # well below half a pixel → PSNR ≥ 45 dB equivalent on a test image
        assert max_err < 0.5

    def test_undistort_uses_forward_map_cv2_convention(self):
        # Direction regression: undistort (REMOVE) samples the input at the
        # FORWARD distorted position — the map cv2.initUndistortRectifyMap
        # builds — so with k1 > 0 an off-center output pixel reads FARTHER
        # from center; distort (ADD) is the inverse and reads CLOSER. The
        # two mode branches were shipped swapped at first.
        w, h = 1920.0, 1080.0
        cx, cy = w * 0.5, h * 0.5
        px, py = 1600.0, 800.0
        r_out = math.hypot(px - cx, py - cy)
        k = (0.2, 0.0, 0.0, 0.0, 0.0)
        ux, uy = map_point(px, py, w, h, *k, mode="undistort")
        dx_, dy_ = map_point(px, py, w, h, *k, mode="distort")
        assert math.hypot(ux - cx, uy - cy) > r_out * 1.05
        assert math.hypot(dx_ - cx, dy_ - cy) < r_out * 0.95

    def test_center_offset_moves_symmetry_point(self):
        # with center at the tested point, the undistort map is identity
        w, h = 1000.0, 1000.0
        px, py = 700.0, 300.0
        cx, cy = px / w, py / h
        x, y = map_point(px, py, w, h, 0.4, 0, 0, 0, 0,
                         cx=cx, cy=cy, mode="undistort")
        assert (x, y) == pytest.approx((px, py), abs=1e-6)


class TestTileSafety:

    def test_padding_grows_with_coefficients(self):
        node = LensDistortionNode(node_id="ld")
        zero = node._tile_padding({"k1": 0.0})
        small = node._tile_padding({"k1": 0.1})
        large = node._tile_padding({"k1": 0.8})
        assert zero == 0
        assert 0 < small < large

    def test_evaluate_marker_and_passthrough(self):
        node = LensDistortionNode(node_id="ld")
        image = _tex(7, 64, 48)
        out = node._do_evaluate({"image": image}, None, None)
        assert out["_effect_type"] == "lens_distortion"
        assert out["_effect_input"] is image

        node.set_parameter("filter_enabled", False)
        out = node._do_evaluate({"image": image}, None, None)
        assert out["result"] is image
        node.set_parameter("filter_enabled", True)
        assert node._do_evaluate({}, None, None) == {}


class _tex:
    def __init__(self, tex_id, w, h):
        from src.features.composite.nodes.base_node import TextureResult
        self._t = TextureResult(texture_id=tex_id, width=w, height=h)

    def __getattr__(self, name):
        return getattr(self._t, name)


class TestShaderParity:
    """The GPU shader is the render truth; the CPU model must mirror it."""

    def test_mirror_edge_formula_is_triangle_wave(self):
        """1-abs(mod(uv,2)-1) keeps in-frame uv identity; the previous
        abs(mod(uv,2)-1) flipped the whole image."""
        from src.features.composite.core import shaders
        src = shaders._LENS_DISTORTION_FRAGMENT
        assert "1.0 - abs(mod(uv, 2.0) - 1.0)" in src
        assert "return texture(u_texture, abs(mod(uv, 2.0) - 1.0));" not in src

    def test_cpu_model_uses_height_normalization_like_gpu(self):
        """CPU map_point normalizes BOTH axes by frame height, matching the
        shader's (px - c) / u_resolution.y — a width/height split made the
        two models diverge on non-square frames."""
        from src.features.composite.core import shaders
        assert "/ u_resolution.y" in shaders._LENS_DISTORTION_FRAGMENT
        # displacement at the right edge centre of a 1280x720 frame must
        # match the GPU convention (r computed against height units)
        px, py = 1280.0, 360.0   # centre-right pixel
        dx, dy = distortion_delta((px - 0.5 * 1280) / 720.0,
                                  (py - 0.5 * 720) / 720.0,
                                  0.1, 0.0, 0.0, 0.0, 0.0)
        gpu_r2 = ((px - 0.5 * 1280) / 720.0) ** 2 + ((py - 0.5 * 720) / 720.0) ** 2
        assert dx == pytest.approx((px - 0.5 * 1280) / 720.0 * 0.1 * gpu_r2)
        assert dy == pytest.approx((py - 0.5 * 720) / 720.0 * 0.1 * gpu_r2)

    def test_padding_scales_with_reference_height(self):
        node = LensDistortionNode(node_id="ld")
        small = node._tile_padding({"k1": 0.2, "height": 1080})
        large = node._tile_padding({"k1": 0.2, "height": 2160})
        assert large == pytest.approx(small * 2.0)
        # unknown height uses the conservative 4K reference
        assert node._tile_padding({"k1": 0.2}) == \
            node._tile_padding({"k1": 0.2, "height": 2160})
