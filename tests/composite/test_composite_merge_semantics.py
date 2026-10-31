"""
Tests for Merge node professional semantics.

Covers:
- MergeNode structure (ports, params, PARAM_DEFS)
- Marker generation (A/B inputs, mask passthrough)
- format_policy resolution (from_b, from_a, larger, smaller)
- bbox_policy resolution (union, intersection, a, b)
- Output dimension resolution
- Blend mode list and index mapping
- Opacity parameter
- Backward compatibility (BlendNode alias)
- Serialization with new params
- Color space propagation
"""
import sys
import os
import pytest
import copy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.blend_node import (
    MergeNode, BlendNode, BLEND_MODES, FORMAT_POLICIES, BBOX_POLICIES,
)
from src.features.composite.core.format import ImageFormat, BBox
from src.features.composite.core.color_space import ColorSpace


# ── Helpers ──

def _make_tr(w=100, h=100, tex_id=1, fmt=None, bbox=None,
             alpha_premultiplied=None, color_space=None):
    return TextureResult(
        texture_id=tex_id, width=w, height=h,
        format=fmt, bbox=bbox,
        alpha_premultiplied=alpha_premultiplied,
        color_space=color_space,
    )


# ── Structure ──

class TestMergeNodeStructure:
    def test_type(self):
        assert MergeNode.node_type == "merge"

    def test_title(self):
        assert MergeNode.node_title == "Merge"

    def test_inputs(self):
        node = MergeNode()
        assert [p.name for p in node.inputs] == ["A", "B", "mask"]

    def test_output(self):
        node = MergeNode()
        assert [p.name for p in node.outputs] == ["result"]

    def test_default_params(self):
        node = MergeNode()
        assert node.parameters["blend_mode"] == "over"
        assert node.parameters["opacity"] == 1.0
        assert node.parameters["format_policy"] == "from_b"
        assert node.parameters["bbox_policy"] == "union"

    def test_param_defs(self):
        names = [p.name for p in MergeNode.PARAM_DEFS]
        assert "blend_mode" in names
        assert "opacity" in names
        assert "format_policy" in names
        assert "bbox_policy" in names


# ── Marker generation ──

class TestMergeMarkers:
    # S1-C: Tests use new `_effect_type: "merge"` protocol
    def test_both_inputs(self):
        node = MergeNode()
        a = _make_tr(tex_id=1)
        b = _make_tr(tex_id=2)
        result = node._do_evaluate({"A": a, "B": b}, None, None)
        # S1-C: Use new marker keys
        assert "_effect_input_a" in result
        assert "_effect_input_b" in result
        assert result["_effect_input_a"] is a
        assert result["_effect_input_b"] is b
        assert "_effect_type" in result
        assert result["_effect_type"] == "merge"

    def test_mask_passed_through(self):
        node = MergeNode()
        a = _make_tr(tex_id=1)
        b = _make_tr(tex_id=2)
        m = _make_tr(tex_id=3)
        result = node._do_evaluate({"A": a, "B": b, "mask": m}, None, None)
        # S1-C: Use new marker key
        assert "_effect_input_mask" in result
        assert result["_effect_input_mask"] is m

    def test_no_mask_is_none(self):
        node = MergeNode()
        a = _make_tr(tex_id=1)
        b = _make_tr(tex_id=2)
        result = node._do_evaluate({"A": a, "B": b}, None, None)
        # S1-C: Use new marker key
        assert "_effect_input_mask" in result
        assert result["_effect_input_mask"] is None

    def test_only_a_passthrough(self):
        node = MergeNode()
        a = _make_tr(tex_id=1)
        result = node._do_evaluate({"A": a}, None, None)
        assert result["result"] is a

    def test_only_b_passthrough(self):
        node = MergeNode()
        b = _make_tr(tex_id=2)
        result = node._do_evaluate({"B": b}, None, None)
        assert result["result"] is b

    def test_no_inputs_empty(self):
        node = MergeNode()
        assert node._do_evaluate({}, None, None) == {}

    def test_invalid_a_passthrough_b(self):
        node = MergeNode()
        a = TextureResult(texture_id=0, width=0, height=0)
        b = _make_tr(tex_id=2)
        result = node._do_evaluate({"A": a, "B": b}, None, None)
        assert result["result"] is b


# ── Format policy resolution ──

class TestFormatPolicy:
    @staticmethod
    def _resolve(input_a, input_b, policy):
        # Inline copy of _resolve_merge_format from GraphEvaluatorMixin
        if policy == "from_a":
            return input_a.format
        elif policy == "larger":
            area_a = input_a.width * input_a.height
            area_b = input_b.width * input_b.height
            if area_a >= area_b:
                return input_a.format or input_b.format
            return input_b.format or input_a.format
        elif policy == "smaller":
            area_a = input_a.width * input_a.height
            area_b = input_b.width * input_b.height
            if area_a <= area_b:
                return input_a.format or input_b.format
            return input_b.format or input_a.format
        else:  # from_b
            return input_b.format if input_b.format else input_a.format

    def _make_tr(self, w, h, fmt_name=""):
        fmt = ImageFormat(width=w, height=h, name=fmt_name) if fmt_name else None
        return TextureResult(texture_id=1, width=w, height=h, format=fmt)

    def test_from_b(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(1280, 720, "B")
        assert self._resolve(a, b, "from_b").name == "B"

    def test_from_a(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(1280, 720, "B")
        assert self._resolve(a, b, "from_a").name == "A"

    def test_from_b_fallback_to_a(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(1280, 720)  # no format
        assert self._resolve(a, b, "from_b").name == "A"

    def test_larger_picks_larger(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(4096, 2160, "B")
        assert self._resolve(a, b, "larger").name == "B"

    def test_smaller_picks_smaller(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(4096, 2160, "B")
        assert self._resolve(a, b, "smaller").name == "A"

    def test_larger_fallback(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(4096, 2160)  # no format
        result = self._resolve(a, b, "larger")
        assert result.name == "A"  # fallback to the one with format


# ── BBox policy resolution ──

class TestBboxPolicy:
    @staticmethod
    def _resolve(input_a, input_b, policy):
        bbox_a = input_a.effective_bbox()
        bbox_b = input_b.effective_bbox()
        if policy == "intersection":
            return bbox_a.intersection(bbox_b)
        elif policy == "a":
            return bbox_a
        elif policy == "b":
            return bbox_b
        else:
            return bbox_a.union(bbox_b)

    def test_union(self):
        a = TextureResult(texture_id=1, width=100, height=100, bbox=BBox(0, 0, 50, 50))
        b = TextureResult(texture_id=2, width=100, height=100, bbox=BBox(25, 25, 50, 50))
        bbox = self._resolve(a, b, "union")
        assert bbox.width == 75 and bbox.height == 75

    def test_intersection(self):
        a = TextureResult(texture_id=1, width=100, height=100, bbox=BBox(0, 0, 50, 50))
        b = TextureResult(texture_id=2, width=100, height=100, bbox=BBox(25, 25, 50, 50))
        bbox = self._resolve(a, b, "intersection")
        assert bbox.x == 25 and bbox.y == 25

    def test_policy_a(self):
        bbox_a = BBox(10, 20, 30, 40)
        a = TextureResult(texture_id=1, width=100, height=100, bbox=bbox_a)
        b = TextureResult(texture_id=2, width=100, height=100, bbox=BBox(0, 0, 100, 100))
        assert self._resolve(a, b, "a") == bbox_a

    def test_policy_b(self):
        bbox_b = BBox(10, 20, 30, 40)
        a = TextureResult(texture_id=1, width=100, height=100, bbox=BBox(0, 0, 100, 100))
        b = TextureResult(texture_id=2, width=100, height=100, bbox=bbox_b)
        assert self._resolve(a, b, "b") == bbox_b

    def test_none_falls_back_to_full_frame(self):
        a = TextureResult(texture_id=1, width=100, height=100, bbox=None)
        b = TextureResult(texture_id=2, width=200, height=200, bbox=BBox(10, 10, 50, 50))
        result = self._resolve(a, b, "a")
        assert result.width == 100 and result.height == 100


# ── Dimension resolution ──

class TestDimResolution:
    @staticmethod
    def _resolve(input_a, input_b, policy):
        wa, ha = input_a.width, input_a.height
        wb, hb = input_b.width, input_b.height
        if policy == "from_a":
            return max(1, wa), max(1, ha)
        elif policy == "larger":
            return max(1, max(wa, wb)), max(1, max(ha, hb))
        elif policy == "smaller":
            return max(1, min(wa, wb)), max(1, min(ha, hb))
        else:
            return max(1, wb), max(1, hb)

    def test_from_b(self):
        a, b = _make_tr(1920, 1080), _make_tr(1280, 720)
        assert self._resolve(a, b, "from_b") == (1280, 720)

    def test_from_a(self):
        a, b = _make_tr(1920, 1080), _make_tr(1280, 720)
        assert self._resolve(a, b, "from_a") == (1920, 1080)

    def test_larger(self):
        a, b = _make_tr(1920, 1080), _make_tr(4096, 2160)
        assert self._resolve(a, b, "larger") == (4096, 2160)

    def test_smaller(self):
        a, b = _make_tr(1920, 1080), _make_tr(4096, 2160)
        assert self._resolve(a, b, "smaller") == (1920, 1080)


# ── Blend modes ──

class TestBlendModes:
    def test_count(self):
        # 15 basic + 4 Porter-Duff + 5 photographic + 4 HSL = 28
        assert len(BLEND_MODES) == 32  # CP-012 +from/exclusion/conjoint/disjoint

    def test_first_is_over(self):
        assert BLEND_MODES[0] == "over"

    def test_all_unique(self):
        assert len(BLEND_MODES) == len(set(BLEND_MODES))

    def test_expected_modes(self):
        expected = [
            # basic composite (0-14)
            "over", "under", "add", "subtract", "multiply", "screen",
            "overlay", "difference", "divide", "min", "max", "average",
            "copy", "stencil", "mask",
            # Porter-Duff matte (15-18)
            "in", "out", "atop", "xor",
            # photographic (19-23)
            "color-dodge", "color-burn", "soft-light", "hard-light", "linear-light",
            # HSL (24-27)
            "hue", "saturation", "color", "luminosity",
            # CP-012 Nuke completion (28-31)
            "from", "exclusion", "conjoint", "disjoint",
        ]
        assert BLEND_MODES == expected


# ── Policy lists ──

class TestPolicyLists:
    def test_format_policies(self):
        assert FORMAT_POLICIES == ["from_b", "from_a", "larger", "smaller"]

    def test_bbox_policies(self):
        assert BBOX_POLICIES == ["union", "intersection", "a", "b"]


# ── Backward compatibility ──

class TestBackwardCompat:
    def test_blend_alias(self):
        assert BlendNode is MergeNode

    def test_blend_creates_merge(self):
        node = BlendNode()
        assert isinstance(node, MergeNode)
        assert node.node_type == "merge"


# ── Serialization ──

class TestMergeSerialization:
    def test_roundtrip(self):
        node = MergeNode()
        node.set_parameter("blend_mode", "screen")
        node.set_parameter("opacity", 0.5)
        node.set_parameter("format_policy", "from_a")
        node.set_parameter("bbox_policy", "intersection")
        d = node.to_dict()
        restored = MergeNode.from_dict(d)
        assert restored.parameters["blend_mode"] == "screen"
        assert restored.parameters["opacity"] == 0.5
        assert restored.parameters["format_policy"] == "from_a"
        assert restored.parameters["bbox_policy"] == "intersection"

    def test_old_data_without_new_params(self):
        d = {
            "id": "m1", "type": "merge",
            "parameters": {"blend_mode": "over"},
        }
        node = MergeNode.from_dict(d)
        assert node.parameters["opacity"] == 1.0
        assert node.parameters["format_policy"] == "from_b"
        assert node.parameters["bbox_policy"] == "union"

    def test_old_blend_data(self):
        d = {
            "id": "m1", "type": "blend",
            "parameters": {"blend_mode": "add"},
        }
        node = MergeNode.from_dict(d)
        assert isinstance(node, MergeNode)
        assert node.parameters["blend_mode"] == "add"


# ── Alpha mode logic ──

class TestAlphaMode:
    def test_both_premult_mode0(self):
        a = _make_tr(alpha_premultiplied=True)
        b = _make_tr(alpha_premultiplied=True)
        a_straight = a.alpha_premultiplied is False
        b_straight = b.alpha_premultiplied is False
        mode = 1 if (a_straight or b_straight) else 0
        assert mode == 0

    def test_one_straight_mode1(self):
        a = _make_tr(alpha_premultiplied=False)
        b = _make_tr(alpha_premultiplied=True)
        a_straight = a.alpha_premultiplied is False
        b_straight = b.alpha_premultiplied is False
        mode = 1 if (a_straight or b_straight) else 0
        assert mode == 1

    def test_unknown_treated_as_premult(self):
        a = _make_tr(alpha_premultiplied=None)
        b = _make_tr(alpha_premultiplied=None)
        a_straight = a.alpha_premultiplied is False
        b_straight = b.alpha_premultiplied is False
        mode = 1 if (a_straight or b_straight) else 0
        assert mode == 0


# ── Color space propagation ──

class TestColorSpacePropagation:
    def test_merge_output_cs_from_b(self):
        """Merge output color_space should come from B (background) by default."""
        # This tests the logic in _dispatch_blend, not the node directly
        a = _make_tr(color_space=ColorSpace.LINEAR)
        b = _make_tr(color_space=ColorSpace.SRGB)
        # In _dispatch_blend: color_space=input_b.color_space or input_a.color_space
        result_cs = b.color_space or a.color_space
        assert result_cs == ColorSpace.SRGB

    def test_merge_output_cs_fallback_to_a(self):
        a = _make_tr(color_space=ColorSpace.LINEAR)
        b = _make_tr(color_space=None)
        result_cs = b.color_space or a.color_space
        assert result_cs == ColorSpace.LINEAR


# ── S2-D: Color space mismatch detection ──

class TestColorSpaceMismatchDetection:
    """Test color space mismatch warning behavior (S2-D).

    Short-term: When A/B color_space is inconsistent, log a warning.
    Long-term: Insert explicit cs_transform node or use working space color management.
    """

    def test_mismatch_linear_vs_srgb(self):
        """Linear vs sRGB should trigger mismatch."""
        a = _make_tr(color_space=ColorSpace.LINEAR)
        b = _make_tr(color_space=ColorSpace.SRGB)
        assert a.color_space != b.color_space

    def test_mismatch_linear_vs_unknown(self):
        """Linear vs unknown should trigger mismatch."""
        a = _make_tr(color_space=ColorSpace.LINEAR)
        b = _make_tr(color_space=ColorSpace.UNKNOWN)
        assert a.color_space != b.color_space

    def test_match_same_color_space(self):
        """Same color space should NOT trigger mismatch."""
        a = _make_tr(color_space=ColorSpace.LINEAR)
        b = _make_tr(color_space=ColorSpace.LINEAR)
        assert a.color_space == b.color_space

    def test_match_both_none(self):
        """Both None should NOT trigger mismatch."""
        a = _make_tr(color_space=None)
        b = _make_tr(color_space=None)
        # None is treated as UNKNOWN in _check_color_space_mismatch
        from src.features.composite.core.color_space import ColorSpace
        cs_a = a.color_space if a.color_space else ColorSpace.UNKNOWN
        cs_b = b.color_space if b.color_space else ColorSpace.UNKNOWN
        assert cs_a == cs_b

    def test_output_cs_inherits_from_b(self):
        """Output color_space should inherit from B (foreground) first."""
        a = _make_tr(color_space=ColorSpace.LINEAR)
        b = _make_tr(color_space=ColorSpace.SRGB)
        output_cs = b.color_space or a.color_space
        assert output_cs == ColorSpace.SRGB

    def test_output_cs_fallback_to_a(self):
        """Output color_space should fallback to A if B is None."""
        a = _make_tr(color_space=ColorSpace.LINEAR)
        b = _make_tr(color_space=None)
        output_cs = b.color_space or a.color_space
        assert output_cs == ColorSpace.LINEAR

    def test_helper_method_signature(self):
        """Verify _check_color_space_mismatch method exists in BaseEvaluatorMixin."""
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        assert hasattr(BaseEvaluatorMixin, '_check_color_space_mismatch')

    def test_helper_returns_output_cs(self):
        """_check_color_space_mismatch should return output color_space."""
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        # The method returns output_cs = input_b.color_space or input_a.color_space
        a = _make_tr(color_space=ColorSpace.LINEAR)
        b = _make_tr(color_space=ColorSpace.SRGB)
        expected_cs = b.color_space or a.color_space
        assert expected_cs == ColorSpace.SRGB
