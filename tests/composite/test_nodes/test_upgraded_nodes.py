"""
Upgraded-node regression tests for crop, cornerpin, reformat, glow, shuffle, keyer.

Covers the behaviour added by the "complete the shell nodes" upgrade:
- Crop: mirror outside-policy option.
- CornerPin: identity bypass, homography computation, tile padding.
- Reformat: cubic/lanczos filter modes + correct filter index mapping.
- Glow: full param set in the effect marker + preview mode radius scaling.
- Shuffle: identity bypass + non-identity effect marker with all 4 channel maps.
- Keyer (Keylight 1.2): new params (screen_balance, clip_black/white, gain,
  despill_strength/method, alpha_offset) + per-mode param visibility helper +
  chroma/luma/difference dispatch paths.

Also covers shader compilation sanity for the new/modified fragment shaders
(glow_h, glow_v, keylight, matte_cleanup, blur_motion, reformat, crop, roto,
compare, 3d_aov, 3d_motion) using a real GL 3.3 context.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.crop_node import CropNode, OUTSIDE_POLICIES
from src.features.composite.nodes.cornerpin_node import (
    CornerPinNode, INTERPOLATION_MODES, _apply_homography_point,
)
from src.features.composite.nodes.reformat_node import ReformatNode, FILTER_MODES, RESIZE_TYPES
from src.features.composite.nodes.glow_node import GlowNode
from src.features.composite.nodes.shuffle_node import ShuffleNode, CHANNEL_SOURCES
from src.features.composite.nodes.keyer_node import (
    KeyerNode, KEYER_MODES, DESILL_METHODS,
)
from src.features.composite.core.format import BBox

pytestmark = pytest.mark.composite_core


# ── Crop ────────────────────────────────────────────────────────────────────

class TestCropUpgraded(NodeTestBase):
    node_type = "crop"
    node_class = CropNode

    def test_outside_policies_include_mirror(self):
        """`outside` 现在支持 black/transparent/repeat/mirror 4 种."""
        assert OUTSIDE_POLICIES == ["black", "transparent", "repeat", "mirror"]

    def test_default_outside_is_transparent(self):
        node = self.create_node()
        assert node.parameters["outside"] == "transparent"

    def test_i18n_title_key_present(self):
        node = self.create_node()
        assert node.i18n_title_key == "nodes.crop.title"

    def test_reformat_true_emits_effect_marker(self):
        """reformat=True 时应生成 crop effect marker."""
        node = self.create_node(params={"reformat": True, "outside": "mirror"})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert outputs.get("_effect_type") == "crop"
        assert outputs["_effect_params"]["outside"] == OUTSIDE_POLICIES.index("mirror")

    def test_reformat_false_is_metadata_only(self):
        """reformat=False 时不应生成 effect marker(只更新 bbox 元数据)."""
        node = self.create_node(params={"reformat": False})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert "_effect_type" not in outputs
        assert outputs["result"].is_valid


# ── CornerPin ───────────────────────────────────────────────────────────────

class TestCornerPinUpgraded(NodeTestBase):
    node_type = "cornerpin"
    node_class = CornerPinNode

    def test_interpolation_modes(self):
        assert INTERPOLATION_MODES == ["bilinear", "bicubic"]

    def test_i18n_title_key_present(self):
        node = self.create_node()
        assert node.i18n_title_key == "nodes.cornerpin.title"

    def test_identity_passes_through(self):
        """identity 变换(默认参数)应直接返回输入."""
        node = self.create_node()
        assert node._is_identity() is True
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert "_effect_type" not in outputs
        assert outputs["result"].texture_id == inputs["image"].texture_id

    def test_non_identity_emits_marker(self):
        """移动一个角点后应生成 cornerpin effect marker + homography."""
        node = self.create_node(params={"to2_x": 0.9, "to2_y": 0.1})
        assert node._is_identity() is False
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert outputs["_effect_type"] == "cornerpin"
        hp = outputs["_effect_params"]["homography"]
        assert len(hp) == 8  # 8-element homography (h22=1 implied)

    def test_homography_identity_for_identity_corners(self):
        """identity 四角应得到 identity 单应矩阵."""
        node = self.create_node()
        src, dst = node._get_corner_points()
        h = CornerPinNode._compute_homography(src, dst)
        # Identity transform: m00=1, m01=0, m02=0, m10=0, m11=1, m12=0, m20=0, m21=0
        assert abs(h[0] - 1.0) < 1e-6
        assert abs(h[4] - 1.0) < 1e-6
        assert abs(h[2]) < 1e-6
        assert abs(h[5]) < 1e-6

    def test_tile_padding_for_bicubic(self):
        """bicubic 插值需要 2 像素 padding."""
        node = self.create_node(params={"interpolation": "bicubic"})
        assert node._tile_padding(node.parameters) == 2
        node2 = self.create_node(params={"interpolation": "bilinear"})
        assert node2._tile_padding(node2.parameters) == 1

    def test_homography_acts_in_uv_y_up_space(self):
        """角点参数是 Y-down（y=0 = 图片顶边），着色器 UV 空间 Y-up。

        发出的单应矩阵必须在 UV 空间生效，否则上方手柄会变形图片的下角
        （垂直镜像，回归测试）。与 Transform 的 _pixel_matrix_to_uv_matrix
        约定一致。
        """
        # to2 = top-right corner moved down to normalized y=0.25
        node = self.create_node(params={"to2_x": 1.0, "to2_y": 0.25})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        h = outputs["_effect_params"]["homography"]
        # from2 (top-right) in UV space: normalized (1, 0) -> (1, 1)
        dx, dy = _apply_homography_point(h, 1.0, 1.0)
        # to2 normalized (1, 0.25) -> UV (1, 0.75)
        assert dx == pytest.approx(1.0, abs=1e-4)
        assert dy == pytest.approx(0.75, abs=1e-4)


# ── Reformat ────────────────────────────────────────────────────────────────

class TestReformatUpgraded(NodeTestBase):
    node_type = "reformat"
    node_class = ReformatNode

    def test_filter_modes_ordered_correctly(self):
        """filter 索引必须与 shader u_filter 一致:0=nearest, 1=linear, 2=cubic, 3=lanczos."""
        assert FILTER_MODES == ["nearest", "linear", "cubic", "lanczos"]

    def test_resize_types_ordered_correctly(self):
        assert RESIZE_TYPES == ["no_resize", "fit", "fill", "distort", "center_crop"]

    def test_i18n_title_key_present(self):
        node = self.create_node()
        assert node.i18n_title_key == "nodes.reformat.title"

    def test_each_filter_emits_correct_index(self):
        """每个 filter 选项在 effect_params 中应给出正确的整数索引."""
        for idx, name in enumerate(FILTER_MODES):
            node = self.create_node(params={
                "format_mode": "custom",
                "width": 200, "height": 100,
                "filter": name,
            })
            inputs = self.create_valid_input(width=100, height=100)
            outputs = node.evaluate(
                inputs, self.get_mock_context(),
                self.get_mock_fb_manager(), self.get_mock_resource_pool(),
            )
            assert outputs["_effect_type"] == "reformat"
            assert outputs["_effect_params"]["filter"] == idx, \
                f"filter={name} should map to idx {idx}"

    def test_each_resize_mode_emits_correct_index(self):
        for idx, name in enumerate(RESIZE_TYPES):
            node = self.create_node(params={
                "format_mode": "custom",
                "width": 200, "height": 100,
                "resize_mode": name,
            })
            inputs = self.create_valid_input(width=100, height=100)
            outputs = node.evaluate(
                inputs, self.get_mock_context(),
                self.get_mock_fb_manager(), self.get_mock_resource_pool(),
            )
            assert outputs["_effect_params"]["resize_mode"] == idx

    def test_legacy_none_alias_maps_to_no_resize(self):
        node = self.create_node(params={"resize_mode": "none"})
        assert node._normalise_resize_mode("none") == "no_resize"


# ── Glow ────────────────────────────────────────────────────────────────────

class TestGlowUpgraded(NodeTestBase):
    node_type = "glow"
    node_class = GlowNode

    def test_i18n_title_key_present(self):
        node = self.create_node()
        assert node.i18n_title_key == "nodes.glow.title"

    def test_full_param_set_in_effect_marker(self):
        """glow effect marker 必须包含完整参数(threshold/radius/intensity/tint/mix/sample_count)."""
        node = self.create_node(params={
            "threshold": 0.8, "radius": 15.0, "intensity": 2.0,
            "tint_r": 1.0, "tint_g": 0.9, "tint_b": 0.8,
            "mix": 1.0, "quality": "high",
        })
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        params = outputs["_effect_params"]
        assert params["threshold"] == 0.8
        # radius 15 < 20 so preview mode does not shrink it.
        assert params["radius"] == 15.0
        assert params["intensity"] == 2.0
        assert params["tint_r"] == 1.0
        assert params["tint_g"] == 0.9
        assert params["tint_b"] == 0.8
        assert params["mix"] == 1.0
        # high quality → 32 samples
        assert params["sample_count"] == 32

    def test_quality_samples_mapping(self):
        cases = {"fast": 8, "normal": 16, "high": 32}
        for q, expect in cases.items():
            node = self.create_node(params={"quality": q})
            inputs = self.create_valid_input()
            outputs = node.evaluate(
                inputs, self.get_mock_context(),
                self.get_mock_fb_manager(), self.get_mock_resource_pool(),
            )
            assert outputs["_effect_params"]["sample_count"] == expect

    def test_zero_intensity_passes_through(self):
        """intensity=0 应直接返回输入,不触发 effect marker."""
        node = self.create_node(params={"intensity": 0.0})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert "_effect_type" not in outputs
        assert outputs["result"].texture_id == inputs["image"].texture_id

    def test_preview_mode_scales_radius(self):
        """preview 模式下,radius > 20 时应被缩放(响应优化)."""
        node = self.create_node(params={"radius": 100.0})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
            render_mode="preview",
        )
        # radius 100 → scaled to ~35
        assert outputs["_effect_params"]["radius"] < 100.0


# ── Shuffle ─────────────────────────────────────────────────────────────────

class TestShuffleUpgraded(NodeTestBase):
    node_type = "shuffle"
    node_class = ShuffleNode

    def test_channel_sources_ordered(self):
        """源选项顺序必须与 shader 索引一致."""
        assert CHANNEL_SOURCES == ["r", "g", "b", "a", "zero", "one", "luma"]

    def test_i18n_title_key_present(self):
        node = self.create_node()
        assert node.i18n_title_key == "nodes.shuffle.title"

    def test_identity_mapping_passes_through(self):
        """默认映射(r→r,g→g,b→b,a→a)应直通."""
        node = self.create_node()
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert "_effect_type" not in outputs
        assert outputs["image"].texture_id == inputs["image"].texture_id

    def test_non_identity_emits_marker(self):
        """任意非默认映射应生成 shuffle effect marker + 4 个 channel 索引."""
        node = self.create_node(params={
            "r_from": "luma", "g_from": "g", "b_from": "b", "a_from": "one",
        })
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert outputs["_effect_type"] == "shuffle"
        params = outputs["_effect_params"]
        assert params["r_from"] == CHANNEL_SOURCES.index("luma")  # 6
        assert params["g_from"] == CHANNEL_SOURCES.index("g")     # 1
        assert params["b_from"] == CHANNEL_SOURCES.index("b")     # 2
        assert params["a_from"] == CHANNEL_SOURCES.index("one")   # 5

    def test_safe_lookup_handles_unknown_value(self):
        """未知 channel 值应回退到默认索引,不抛异常."""
        node = self.create_node()
        node.parameters["r_from"] = "invalid"  # bypass type validation
        inputs = self.create_valid_input()
        # Should not crash; r_from falls back to 0 (r).
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert outputs["_effect_params"]["r_from"] == 0


# ── Keyer (Keylight 1.2) ────────────────────────────────────────────────────

class TestKeyerUpgraded(NodeTestBase):
    node_type = "keyer"
    node_class = KeyerNode

    def test_modes(self):
        assert KEYER_MODES == ["auto", "chroma", "luma", "difference"]

    def test_despill_methods(self):
        assert DESILL_METHODS == ["none", "subtract", "scale"]

    def test_i18n_title_key_present(self):
        node = self.create_node()
        assert node.i18n_title_key == "nodes.keyer.title"

    def test_full_param_set_default(self):
        """新 Keylight 参数应在节点默认参数中存在."""
        node = self.create_node()
        for key in ("keyer_mode", "screen_color_r", "screen_color_g", "screen_color_b",
                    "screen_balance", "clip_black", "clip_white", "gain", "softness",
                    "alpha_offset", "despill_strength", "despill_method",
                    "invert", "output_mode"):
            assert key in node.parameters, f"missing key: {key}"

    def test_chroma_mode_passes_full_chroma_params(self):
        """chroma 模式应把 Keylight 风格参数全数传给 effect_params."""
        node = self.create_node(params={
            "keyer_mode": "chroma",
            "screen_color_r": 0.1, "screen_color_g": 0.9, "screen_color_b": 0.1,
            "screen_balance": 0.3,
            "clip_black": 0.2, "clip_white": 0.8, "gain": 1.5, "softness": 0.2,
            "alpha_offset": 0.05,
            "despill_strength": 0.7, "despill_method": "scale",
        })
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        params = outputs["_effect_params"]
        assert params["keyer_mode"] == "chroma"
        assert params["screen_color"] == (0.1, 0.9, 0.1)
        assert abs(params["screen_balance"] - 0.3) < 1e-6
        assert abs(params["clip_black"] - 0.2) < 1e-6
        assert abs(params["clip_white"] - 0.8) < 1e-6
        assert abs(params["gain"] - 1.5) < 1e-6
        assert params["despill_method"] == DESILL_METHODS.index("scale")

    def test_luma_mode_uses_clip_points(self):
        """luma 模式应使用 clip_black/white 作为亮度键控点."""
        node = self.create_node(params={
            "keyer_mode": "luma",
            "clip_black": 0.1, "clip_white": 0.9,
        })
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        params = outputs["_effect_params"]
        assert params["keyer_mode"] == "luma"
        assert params["clip_black"] == 0.1
        assert params["clip_white"] == 0.9

    def test_difference_mode_requires_reference(self):
        """difference 模式无 reference 输入时,应直通返回原图."""
        node = self.create_node(params={"keyer_mode": "difference"})
        inputs = self.create_valid_input()  # no reference
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert outputs["result"].texture_id == inputs["image"].texture_id

    def test_difference_mode_with_reference_emits_inputs(self):
        """difference 模式有 reference 时,应在 marker 中给出 input_a / input_b."""
        node = self.create_node(params={"keyer_mode": "difference"})
        inputs = {
            "image": make_texture_result(texture_id=11),
            "reference": make_texture_result(texture_id=22),
        }
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert outputs["_effect_input_a"].texture_id == 11
        assert outputs["_effect_input_b"].texture_id == 22
        assert "_effect_input" not in outputs

    def test_auto_mode_defaults_to_chroma(self):
        """auto 模式无 reference 输入时应选择 chroma."""
        node = self.create_node(params={"keyer_mode": "auto"})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert outputs["_effect_params"]["keyer_mode"] == "chroma"

    def test_auto_mode_picks_difference_with_reference(self):
        """auto 模式有 reference 输入时应选择 difference."""
        node = self.create_node(params={"keyer_mode": "auto"})
        inputs = {
            "image": make_texture_result(texture_id=1),
            "reference": make_texture_result(texture_id=2),
        }
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        assert outputs["_effect_params"]["keyer_mode"] == "difference"

    def test_param_visibility_chroma_shows_screen_params(self):
        """chroma 模式应显示所有 screen 参数."""
        hidden = KeyerNode.keyer_mode_param_visibility("chroma")
        assert "screen_color_r" not in hidden
        assert "despill_strength" not in hidden
        assert "clip_black" not in hidden

    def test_param_visibility_luma_hides_screen(self):
        """luma 模式应隐藏 screen + despill 参数."""
        hidden = KeyerNode.keyer_mode_param_visibility("luma")
        assert "screen_color_r" in hidden
        assert "despill_strength" in hidden
        assert "despill_method" in hidden
        assert "clip_black" not in hidden

    def test_param_visibility_difference_hides_all_chroma(self):
        """difference 模式应隐藏所有色度相关参数."""
        hidden = KeyerNode.keyer_mode_param_visibility("difference")
        assert "screen_color_r" in hidden
        assert "gain" in hidden
        assert "clip_black" in hidden
        assert "softness" in hidden

    def test_screen_color_hex_param_exists(self):
        """新 screen_color_hex 参数应存在,默认 #00ff00(纯绿)."""
        node = self.create_node()
        assert node.parameters.get("screen_color_hex") == "#00ff00"
        assert any(p.name == "screen_color_hex" for p in KeyerNode.PARAM_DEFS)

    def test_screen_color_hex_hidden_from_generic_ui(self):
        """screen_color_hex ParamDef 应标记为 hidden(由自定义颜色行接管)."""
        pdef = next(p for p in KeyerNode.PARAM_DEFS if p.name == "screen_color_hex")
        assert getattr(pdef, "hidden", False) is True

    def test_screen_color_rgb_hex_stay_in_sync_after_set(self):
        """设置 screen_color_hex 不影响 RGB;但 UI 颜色行会同步两者."""
        node = self.create_node()
        # RGB 默认 (0, 1, 0) → 对应 hex #00ff00
        assert node.parameters["screen_color_hex"] == "#00ff00"
        # 改 RGB 后,hex 保持旧值(由 UI 负责同步,节点本身不自动联动)
        node.set_parameter("screen_color_r", 1.0)
        assert node.parameters["screen_color_r"] == 1.0
        # 节点评估时只用 RGB,hex 只是 UI 辅助
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool(),
        )
        sc = outputs["_effect_params"]["screen_color"]
        assert sc[0] == 1.0  # R
        assert sc[1] == 1.0  # G
        assert sc[2] == 0.0  # B


# ── Shader Compilation Sanity ───────────────────────────────────────────────
# Verify that the new/modified fragment shaders compile in a real GL context.

@pytest.fixture(scope="module")
def gl_ctx():
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
    except Exception as e:
        pytest.skip(f"Cannot create standalone GL context: {e}")
    yield ctx
    ctx.release()


@pytest.mark.parametrize("shader_name", [
    "glow_h", "glow_v", "keylight", "matte_cleanup", "blur_motion", "reformat", "crop", "roto", "compare", "light_wrap", "relight", "cryptomatte_select", "zblur", "fog", "depth_matte", "godrays", "chromatic_aberration", "vignette", "lens_flare", "3d_aov", "3d_motion",
])
def test_shader_compiles(gl_ctx, shader_name):
    """新增/修改的 shader 必须能在真实 GL 3.3 上下文中编译通过."""
    from src.features.composite.core.shaders import (
        _PASSTHROUGH_VERTEX,
        _GLOW_H_FRAGMENT, _GLOW_V_FRAGMENT,
        _KEYLIGHT_FRAGMENT, _REFORMAT_FRAGMENT, _CROP_FRAGMENT,
        _MATTE_CLEANUP_FRAGMENT, _BLUR_MOTION_FRAGMENT, _ROTO_FRAGMENT,
        _COMPARE_FRAGMENT, _LIGHT_WRAP_FRAGMENT, _RELIGHT_FRAGMENT,
        _CRYPTOMATTE_SELECT_FRAGMENT,
        _ZBLUR_FRAGMENT, _FOG_FRAGMENT, _DEPTH_MATTE_FRAGMENT,
        _GODRAYS_FRAGMENT, _CHROMATIC_ABERRATION_FRAGMENT,
        _VIGNETTE_FRAGMENT, _LENS_FLARE_FRAGMENT,
        _MESH_AOV_FRAGMENT,
        _MESH_MOTION_VERTEX, _MESH_MOTION_VERTEX_INSTANCED,
        _MESH_MOTION_VERTEX_SKINNED, _MESH_MOTION_FRAGMENT,
        _MESH_VERTEX, _MESH_VERTEX_SKINNED,
    )
    sources = {
        "glow_h":   (_PASSTHROUGH_VERTEX, _GLOW_H_FRAGMENT),
        "glow_v":   (_PASSTHROUGH_VERTEX, _GLOW_V_FRAGMENT),
        "keylight": (_PASSTHROUGH_VERTEX, _KEYLIGHT_FRAGMENT),
        "matte_cleanup": (_PASSTHROUGH_VERTEX, _MATTE_CLEANUP_FRAGMENT),
        "blur_motion": (_PASSTHROUGH_VERTEX, _BLUR_MOTION_FRAGMENT),
        "reformat": (_PASSTHROUGH_VERTEX, _REFORMAT_FRAGMENT),
        "crop":     (_PASSTHROUGH_VERTEX, _CROP_FRAGMENT),
        "roto":     (_PASSTHROUGH_VERTEX, _ROTO_FRAGMENT),
        "compare":  (_PASSTHROUGH_VERTEX, _COMPARE_FRAGMENT),
        "light_wrap": (_PASSTHROUGH_VERTEX, _LIGHT_WRAP_FRAGMENT),
        "relight":  (_PASSTHROUGH_VERTEX, _RELIGHT_FRAGMENT),
        "cryptomatte_select": (_PASSTHROUGH_VERTEX,
                               _CRYPTOMATTE_SELECT_FRAGMENT),
        "zblur":     (_PASSTHROUGH_VERTEX, _ZBLUR_FRAGMENT),
        "fog":       (_PASSTHROUGH_VERTEX, _FOG_FRAGMENT),
        "depth_matte": (_PASSTHROUGH_VERTEX, _DEPTH_MATTE_FRAGMENT),
        "godrays":   (_PASSTHROUGH_VERTEX, _GODRAYS_FRAGMENT),
        "chromatic_aberration": (_PASSTHROUGH_VERTEX,
                                 _CHROMATIC_ABERRATION_FRAGMENT),
        "vignette":  (_PASSTHROUGH_VERTEX, _VIGNETTE_FRAGMENT),
        "lens_flare": (_PASSTHROUGH_VERTEX, _LENS_FLARE_FRAGMENT),
        "3d_aov":   (_MESH_VERTEX, _MESH_AOV_FRAGMENT),
        "3d_motion": (_MESH_MOTION_VERTEX, _MESH_MOTION_FRAGMENT),
    }
    vert, frag = sources[shader_name]
    prog = gl_ctx.program(vertex_shader=vert, fragment_shader=frag)
    # Smoke-test that the expected critical uniforms exist.
    uniform_names = set(prog)
    if shader_name == "glow_v":
        assert "u_texture" in uniform_names
        assert "u_bloom_tex" in uniform_names
        assert "u_intensity" in uniform_names
        assert "u_tint" in uniform_names
    elif shader_name == "keylight":
        assert "u_screen_color" in uniform_names
        assert "u_screen_balance" in uniform_names
        assert "u_clip_black" in uniform_names
        assert "u_clip_white" in uniform_names
        assert "u_despill_method" in uniform_names
    elif shader_name == "matte_cleanup":
        assert "u_black_point" in uniform_names
        assert "u_white_point" in uniform_names
        assert "u_gamma" in uniform_names
    elif shader_name == "blur_motion":
        assert "u_velocity" in uniform_names
        assert "u_use_velocity" in uniform_names
        assert "u_shutter" in uniform_names
    elif shader_name == "reformat":
        assert "u_filter" in uniform_names
        assert "u_resize_mode" in uniform_names
    elif shader_name == "crop":
        assert "u_outside" in uniform_names
        assert "u_crop_rect" in uniform_names
    elif shader_name == "roto":
        assert "u_points" in uniform_names
        assert "u_point_feathers" in uniform_names
    elif shader_name == "compare":
        assert "u_texture_a" in uniform_names
        assert "u_texture_b" in uniform_names
        assert "u_mode" in uniform_names
    elif shader_name == "light_wrap":
        assert "u_background" in uniform_names
        assert "u_foreground" in uniform_names
        assert "u_width" in uniform_names
        assert "u_intensity" in uniform_names
    elif shader_name == "relight":
        assert "u_normal_tex" in uniform_names
        assert "u_position_tex" in uniform_names
        assert "u_light_type" in uniform_names
        assert "u_light_position" in uniform_names
        assert "u_normal_decode" in uniform_names
    elif shader_name == "cryptomatte_select":
        assert "u_selected" in uniform_names
        assert "u_count" in uniform_names
        assert "u_id_encoding" in uniform_names
        assert "u_output_mode" in uniform_names
    elif shader_name == "zblur":
        assert "u_depth" in uniform_names
        assert "u_depth_units" in uniform_names
        assert "u_focus_distance" in uniform_names
        assert "u_sample_count" in uniform_names
    elif shader_name == "fog":
        assert "u_depth" in uniform_names
        assert "u_fog_color" in uniform_names
        assert "u_density" in uniform_names
    elif shader_name == "depth_matte":
        assert "u_depth" in uniform_names
        assert "u_range_start" in uniform_names
        assert "u_feather" in uniform_names
    elif shader_name == "godrays":
        assert "u_light_pos" in uniform_names
        assert "u_decay" in uniform_names
        assert "u_sample_count" in uniform_names
    elif shader_name == "chromatic_aberration":
        assert "u_offset_r" in uniform_names
        assert "u_offset_b" in uniform_names
        assert "u_edge_boost" in uniform_names
    elif shader_name == "vignette":
        assert "u_amount" in uniform_names
        assert "u_roundness" in uniform_names
    elif shader_name == "lens_flare":
        assert "u_ghost_count" in uniform_names
        assert "u_halo_radius" in uniform_names
        assert "u_streak_strength" in uniform_names
        assert "u_blend_mode" in uniform_names
    elif shader_name == "3d_aov":
        assert "u_aov_mode" in uniform_names
    elif shader_name == "3d_motion":
        assert "u_prev_proj" in uniform_names
        assert "u_prev_view" in uniform_names
        assert "u_prev_model" in uniform_names
        assert "u_motion_scale" in uniform_names

    # The same fragment must also link with the skinned vertex contract.
    if shader_name == "3d_aov":
        skinned_prog = gl_ctx.program(
            vertex_shader=_MESH_VERTEX_SKINNED,
            fragment_shader=_MESH_AOV_FRAGMENT)
        assert "u_aov_mode" in set(skinned_prog)
        skinned_prog.release()
    elif shader_name == "3d_motion":
        instanced_prog = gl_ctx.program(
            vertex_shader=_MESH_MOTION_VERTEX_INSTANCED,
            fragment_shader=_MESH_MOTION_FRAGMENT)
        assert "u_motion_scale" in set(instanced_prog)
        instanced_prog.release()
        skinned_prog = gl_ctx.program(
            vertex_shader=_MESH_MOTION_VERTEX_SKINNED,
            fragment_shader=_MESH_MOTION_FRAGMENT)
        assert "u_prev_bone_matrices" in set(skinned_prog)
        skinned_prog.release()
    prog.release()


# ── i18n Coverage ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("node_type,title_key", [
    ("crop", "nodes.crop.title"),
    ("cornerpin", "nodes.cornerpin.title"),
    ("reformat", "nodes.reformat.title"),
    ("glow", "nodes.glow.title"),
    ("shuffle", "nodes.shuffle.title"),
    ("keyer", "nodes.keyer.title"),
])
def test_i18n_keys_present_in_both_locales(node_type, title_key):
    """每个升级节点都应在 zh_CN + en_US 两份 locale 文件中有标题翻译."""
    import json
    base = os.path.join(os.path.dirname(__file__), '..', '..', '..',
                        'src', 'features', 'composite', 'locales')
    for fname in ("zh_CN.json", "en_US.json"):
        with open(os.path.join(base, fname)) as fh:
            data = json.load(fh)
        # title_key like "nodes.crop.title" → data["nodes"]["crop"]["title"]
        parts = title_key.split(".")
        cursor = data
        for p in parts:
            assert p in cursor, f"{fname}: missing key path {title_key} (at {p})"
            cursor = cursor[p]
        assert isinstance(cursor, str), f"{fname}: {title_key} should be a string"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
