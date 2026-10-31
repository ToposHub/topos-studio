"""
Unit tests for the redesigned Merge node (Step 4 of Phase 2).

Tests cover:
- MergeNode class structure (ports, params)
- Marker generation with mask
- format_policy logic
- bbox_policy logic
- Blend mode list and index mapping
- Backward compatibility (BlendNode alias)
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.blend_node import MergeNode, BlendNode, BLEND_MODES, FORMAT_POLICIES, BBOX_POLICIES
from src.features.composite.core.format import ImageFormat, BBox

import pytest
pytestmark = pytest.mark.composite_core


class TestMergeNodeStructure(unittest.TestCase):
    """Test MergeNode class structure."""

    def test_node_type(self):
        node = MergeNode()
        self.assertEqual(node.node_type, "merge")

    def test_node_title(self):
        node = MergeNode()
        self.assertEqual(node.node_title, "Merge")

    def test_inputs(self):
        """MergeNode has A, B, mask inputs."""
        node = MergeNode()
        port_names = [p.name for p in node.inputs]
        self.assertEqual(port_names, ["A", "B", "mask"])

    def test_output(self):
        """MergeNode has single 'result' output."""
        node = MergeNode()
        port_names = [p.name for p in node.outputs]
        self.assertEqual(port_names, ["result"])

    def test_default_params(self):
        """Default parameters match NUKE conventions."""
        node = MergeNode()
        self.assertEqual(node.parameters["blend_mode"], "over")
        self.assertAlmostEqual(node.parameters["opacity"], 1.0)
        self.assertEqual(node.parameters["format_policy"], "from_b")
        self.assertEqual(node.parameters["bbox_policy"], "union")


class TestMergeNodeMarkers(unittest.TestCase):
    """Test MergeNode marker generation."""

    def _make_valid_tr(self, w=100, h=100, tex_id=1):
        return TextureResult(texture_id=tex_id, width=w, height=h)

    def test_both_inputs_returns_markers(self):
        """Both A and B valid → returns blend markers."""
        node = MergeNode()
        a = self._make_valid_tr(tex_id=1)
        b = self._make_valid_tr(tex_id=2)
        result = node._do_evaluate({"A": a, "B": b}, None, None)
        self.assertIn("_effect_input_a", result)
        self.assertIn("_effect_input_b", result)
        self.assertIs(result["_effect_input_a"], a)
        self.assertIs(result["_effect_input_b"], b)

    def test_mask_in_markers(self):
        """Mask input is passed through markers."""
        node = MergeNode()
        a = self._make_valid_tr(tex_id=1)
        b = self._make_valid_tr(tex_id=2)
        m = self._make_valid_tr(tex_id=3)
        result = node._do_evaluate({"A": a, "B": b, "mask": m}, None, None)
        self.assertIn("_effect_input_mask", result)
        self.assertIs(result["_effect_input_mask"], m)

    def test_no_mask_gives_none(self):
        """No mask input -> _effect_input_mask is None."""
        node = MergeNode()
        a = self._make_valid_tr(tex_id=1)
        b = self._make_valid_tr(tex_id=2)
        result = node._do_evaluate({"A": a, "B": b}, None, None)
        self.assertIsNone(result["_effect_input_mask"])

    def test_only_a_returns_a(self):
        """Only A connected → passthrough A."""
        node = MergeNode()
        a = self._make_valid_tr(tex_id=1)
        result = node._do_evaluate({"A": a}, None, None)
        self.assertIs(result["result"], a)

    def test_only_b_returns_b(self):
        """Only B connected → passthrough B."""
        node = MergeNode()
        b = self._make_valid_tr(tex_id=2)
        result = node._do_evaluate({"B": b}, None, None)
        self.assertIs(result["result"], b)

    def test_no_inputs_returns_empty(self):
        """No inputs → empty dict."""
        node = MergeNode()
        result = node._do_evaluate({}, None, None)
        self.assertEqual(result, {})


class TestFormatPolicy(unittest.TestCase):
    """Test format_policy resolution logic (duplicated from GraphEvaluatorMixin)."""

    def _make_tr(self, w, h, fmt_name=""):
        fmt = ImageFormat(width=w, height=h, name=fmt_name) if fmt_name else None
        return TextureResult(texture_id=1, width=w, height=h, format=fmt)

    @staticmethod
    def _resolve_format(input_a, input_b, policy):
        """Inline version of GraphEvaluatorMixin._resolve_merge_format."""
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
        else:  # "from_b"
            return input_b.format if input_b.format else input_a.format

    def test_from_b_uses_b_format(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(1280, 720, "B")
        fmt = self._resolve_format(a, b, "from_b")
        self.assertEqual(fmt.name, "B")

    def test_from_a_uses_a_format(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(1280, 720, "B")
        fmt = self._resolve_format(a, b, "from_a")
        self.assertEqual(fmt.name, "A")

    def test_from_b_fallback_to_a(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(1280, 720, "")
        fmt = self._resolve_format(a, b, "from_b")
        self.assertEqual(fmt.name, "A")

    def test_larger_uses_larger_area(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(4096, 2160, "B")
        fmt = self._resolve_format(a, b, "larger")
        self.assertEqual(fmt.name, "B")

    def test_smaller_uses_smaller_area(self):
        a = self._make_tr(1920, 1080, "A")
        b = self._make_tr(4096, 2160, "B")
        fmt = self._resolve_format(a, b, "smaller")
        self.assertEqual(fmt.name, "A")


class TestBboxPolicy(unittest.TestCase):
    """Test bbox_policy resolution logic (duplicated from GraphEvaluatorMixin)."""

    @staticmethod
    def _resolve_bbox(input_a, input_b, policy):
        """Inline version of GraphEvaluatorMixin._resolve_merge_bbox."""
        bbox_a = input_a.effective_bbox()
        bbox_b = input_b.effective_bbox()
        if policy == "intersection":
            return bbox_a.intersection(bbox_b)
        elif policy == "a":
            return bbox_a
        elif policy == "b":
            return bbox_b
        else:  # "union"
            return bbox_a.union(bbox_b)

    def test_union(self):
        a = TextureResult(texture_id=1, width=100, height=100, bbox=BBox(0, 0, 50, 50))
        b = TextureResult(texture_id=2, width=100, height=100, bbox=BBox(25, 25, 50, 50))
        bbox = self._resolve_bbox(a, b, "union")
        self.assertEqual(bbox.x, 0)
        self.assertEqual(bbox.y, 0)
        self.assertEqual(bbox.width, 75)
        self.assertEqual(bbox.height, 75)

    def test_intersection(self):
        a = TextureResult(texture_id=1, width=100, height=100, bbox=BBox(0, 0, 50, 50))
        b = TextureResult(texture_id=2, width=100, height=100, bbox=BBox(25, 25, 50, 50))
        bbox = self._resolve_bbox(a, b, "intersection")
        self.assertEqual(bbox.x, 25)
        self.assertEqual(bbox.y, 25)
        self.assertEqual(bbox.width, 25)
        self.assertEqual(bbox.height, 25)

    def test_policy_a(self):
        bbox_a = BBox(10, 20, 30, 40)
        bbox_b = BBox(0, 0, 100, 100)
        a = TextureResult(texture_id=1, width=100, height=100, bbox=bbox_a)
        b = TextureResult(texture_id=2, width=100, height=100, bbox=bbox_b)
        result = self._resolve_bbox(a, b, "a")
        self.assertEqual(result, bbox_a)

    def test_policy_b(self):
        bbox_a = BBox(10, 20, 30, 40)
        bbox_b = BBox(0, 0, 100, 100)
        a = TextureResult(texture_id=1, width=100, height=100, bbox=bbox_a)
        b = TextureResult(texture_id=2, width=100, height=100, bbox=bbox_b)
        result = self._resolve_bbox(a, b, "b")
        self.assertEqual(result, bbox_b)

    def test_none_bbox_falls_back_to_full_frame(self):
        a = TextureResult(texture_id=1, width=100, height=100, bbox=None)
        b = TextureResult(texture_id=2, width=200, height=200, bbox=BBox(10, 10, 50, 50))
        result = self._resolve_bbox(a, b, "a")
        self.assertEqual(result.width, 100)
        self.assertEqual(result.height, 100)


class TestResolveDims(unittest.TestCase):
    """Test output dimension resolution (duplicated from GraphEvaluatorMixin)."""

    def _make_tr(self, w, h):
        return TextureResult(texture_id=1, width=w, height=h)

    @staticmethod
    def _resolve_dims(input_a, input_b, policy):
        """Inline version of GraphEvaluatorMixin._resolve_merge_dims."""
        wa, ha = input_a.width, input_a.height
        wb, hb = input_b.width, input_b.height
        if policy == "from_a":
            return max(1, wa), max(1, ha)
        elif policy == "larger":
            return max(1, max(wa, wb)), max(1, max(ha, hb))
        elif policy == "smaller":
            return max(1, min(wa, wb)), max(1, min(ha, hb))
        else:  # "from_b"
            return max(1, wb), max(1, hb)

    def test_from_b(self):
        a, b = self._make_tr(1920, 1080), self._make_tr(1280, 720)
        self.assertEqual(self._resolve_dims(a, b, "from_b"), (1280, 720))

    def test_from_a(self):
        a, b = self._make_tr(1920, 1080), self._make_tr(1280, 720)
        self.assertEqual(self._resolve_dims(a, b, "from_a"), (1920, 1080))

    def test_larger(self):
        a, b = self._make_tr(1920, 1080), self._make_tr(4096, 2160)
        self.assertEqual(self._resolve_dims(a, b, "larger"), (4096, 2160))

    def test_smaller(self):
        a, b = self._make_tr(1920, 1080), self._make_tr(4096, 2160)
        self.assertEqual(self._resolve_dims(a, b, "smaller"), (1920, 1080))


class TestBlendModes(unittest.TestCase):
    """Test blend mode list and index mapping."""

    def test_modes_count(self):
        # 15 basic + 4 Porter-Duff + 5 photographic + 4 HSL = 28
        self.assertEqual(len(BLEND_MODES), 32)  # CP-012 +from/exclusion/conjoint/disjoint

    def test_over_is_first(self):
        self.assertEqual(BLEND_MODES[0], "over")

    def test_all_modes_unique(self):
        self.assertEqual(len(BLEND_MODES), len(set(BLEND_MODES)))

    def test_index_lookup(self):
        for i, mode in enumerate(BLEND_MODES):
            self.assertEqual(BLEND_MODES.index(mode), i)

    def test_expected_modes_present(self):
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
        self.assertEqual(BLEND_MODES, expected)


class TestPolicyLists(unittest.TestCase):
    """Test policy option lists."""

    def test_format_policies(self):
        self.assertEqual(FORMAT_POLICIES, ["from_b", "from_a", "larger", "smaller"])

    def test_bbox_policies(self):
        self.assertEqual(BBOX_POLICIES, ["union", "intersection", "a", "b"])


class TestBackwardCompatibility(unittest.TestCase):
    """Test that BlendNode alias works."""

    def test_blend_node_is_merge_node(self):
        self.assertIs(BlendNode, MergeNode)

    def test_blend_node_creates_merge(self):
        node = BlendNode()
        self.assertIsInstance(node, MergeNode)
        self.assertEqual(node.node_type, "merge")


class TestParamDefs(unittest.TestCase):
    """Test PARAM_DEFS includes new parameters."""

    def test_has_blend_mode(self):
        names = [p.name for p in MergeNode.PARAM_DEFS]
        self.assertIn("blend_mode", names)

    def test_has_opacity(self):
        names = [p.name for p in MergeNode.PARAM_DEFS]
        self.assertIn("opacity", names)

    def test_has_format_policy(self):
        names = [p.name for p in MergeNode.PARAM_DEFS]
        self.assertIn("format_policy", names)

    def test_has_bbox_policy(self):
        names = [p.name for p in MergeNode.PARAM_DEFS]
        self.assertIn("bbox_policy", names)

    def test_blend_mode_labels_match(self):
        pd = {p.name: p for p in MergeNode.PARAM_DEFS}
        self.assertEqual(pd["blend_mode"].labels, BLEND_MODES)

    def test_format_policy_labels_match(self):
        pd = {p.name: p for p in MergeNode.PARAM_DEFS}
        self.assertEqual(pd["format_policy"].labels, FORMAT_POLICIES)

    def test_bbox_policy_labels_match(self):
        pd = {p.name: p for p in MergeNode.PARAM_DEFS}
        self.assertEqual(pd["bbox_policy"].labels, BBOX_POLICIES)


if __name__ == "__main__":
    unittest.main()
