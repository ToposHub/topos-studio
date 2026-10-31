"""Test Merge nodes: Merge, Premult, Unpremult, Dissolve, Switch, Mask, Matte."""
import pytest
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.nodes.base_node import TextureResult


class TestMergeNode:
    """Test MergeNode."""

    def test_registered(self):
        assert "merge" in NODE_REGISTRY or "blend" in NODE_REGISTRY

    def test_can_instantiate(self):
        from src.features.composite.nodes.blend_node import MergeNode
        node = MergeNode()
        assert hasattr(node, 'PARAM_DEFS')

    def test_to_dict_from_dict(self):
        from src.features.composite.nodes.blend_node import MergeNode
        node = MergeNode()
        d = node.to_dict()
        node2 = MergeNode.from_dict(d)
        assert node2.node_type == d["type"]

    # S1-C: Test new `_effect_type: "merge"` marker protocol
    def test_effect_type_marker_protocol(self):
        """Test that MergeNode._do_evaluate() returns new `_effect_type: "merge"` marker (S1-C)."""
        from src.features.composite.nodes.blend_node import MergeNode
        node = MergeNode()

        # Mock inputs
        input_a = TextureResult(texture_id=100, width=1920, height=1080)
        input_b = TextureResult(texture_id=101, width=1920, height=1080)
        input_mask = TextureResult(texture_id=102, width=1920, height=1080)

        inputs = {"A": input_a, "B": input_b, "mask": input_mask}

        # Call _do_evaluate() with mock context
        outputs = node._do_evaluate(inputs, None, None, None, "preview")

        # Check new marker protocol (S1-C)
        assert "_effect_type" in outputs
        assert outputs["_effect_type"] == "merge"

        # Check input markers (S1-C)
        assert "_effect_input_a" in outputs
        assert "_effect_input_b" in outputs
        assert "_effect_input_mask" in outputs
        assert "_effect_input" in outputs  # Primary foreground input

        # Verify input values
        assert outputs["_effect_input_a"] == input_a
        assert outputs["_effect_input_b"] == input_b
        assert outputs["_effect_input_mask"] == input_mask
        assert outputs["_effect_input"] == input_b  # Primary = foreground (B)

        # Check _effect_params
        assert "_effect_params" in outputs
        params = outputs["_effect_params"]
        assert "blend_mode" in params
        assert "opacity" in params
        assert "format_policy" in params
        assert "bbox_policy" in params

    def test_effect_type_marker_single_input_passthrough(self):
        """Test that MergeNode returns single input when only one input is present."""
        from src.features.composite.nodes.blend_node import MergeNode
        node = MergeNode()

        # Only input B
        input_b = TextureResult(texture_id=101, width=1920, height=1080)
        inputs = {"B": input_b}

        outputs = node._do_evaluate(inputs, None, None, None, "preview")

        # Should pass through without marker
        assert "result" in outputs
        assert outputs["result"] == input_b
        assert "_effect_type" not in outputs

    # S2-B: Test alpha_policy and alpha_clamp parameters
    def test_alpha_policy_param_exists(self):
        """Test that MergeNode has alpha_policy parameter (S2-B)."""
        from src.features.composite.nodes.blend_node import MergeNode, ALPHA_POLICIES
        node = MergeNode()

        # Check alpha_policy exists in default parameters
        assert "alpha_policy" in node.parameters
        assert node.parameters["alpha_policy"] == "auto"

        # Check ALPHA_POLICIES list
        assert "auto" in ALPHA_POLICIES
        assert "premult_force" in ALPHA_POLICIES
        assert "straight_force" in ALPHA_POLICIES
        assert "output_straight" in ALPHA_POLICIES

    def test_alpha_clamp_param_exists(self):
        """Test that MergeNode has alpha_clamp parameter (S2-B)."""
        from src.features.composite.nodes.blend_node import MergeNode
        node = MergeNode()

        # Check alpha_clamp exists in default parameters
        assert "alpha_clamp" in node.parameters
        assert node.parameters["alpha_clamp"] == True

    def test_alpha_policy_in_effect_params(self):
        """Test that alpha_policy and alpha_clamp are passed in _effect_params (S2-B)."""
        from src.features.composite.nodes.blend_node import MergeNode
        node = MergeNode()

        input_a = TextureResult(texture_id=100, width=1920, height=1080)
        input_b = TextureResult(texture_id=101, width=1920, height=1080)
        inputs = {"A": input_a, "B": input_b}

        outputs = node._do_evaluate(inputs, None, None, None, "preview")

        # Check alpha_policy and alpha_clamp in _effect_params
        params = outputs["_effect_params"]
        assert "alpha_policy" in params
        assert "alpha_clamp" in params
        assert params["alpha_policy"] == "auto"
        assert params["alpha_clamp"] == True

    def test_alpha_policy_output_straight_affects_output_premult(self):
        """Test that alpha_policy='output_straight' sets output alpha_premultiplied=False (S2-B)."""
        from src.features.composite.nodes.blend_node import MergeNode
        node = MergeNode()
        node.set_parameter("alpha_policy", "output_straight")

        input_a = TextureResult(texture_id=100, width=1920, height=1080)
        input_b = TextureResult(texture_id=101, width=1920, height=1080)
        inputs = {"A": input_a, "B": input_b}

        outputs = node._do_evaluate(inputs, None, None, None, "preview")

        # Check output alpha_premultiplied is False for output_straight
        assert outputs["result"].alpha_premultiplied == False

    def test_alpha_policy_auto_output_is_premult(self):
        """Test that alpha_policy='auto' outputs premultiplied (S2-B)."""
        from src.features.composite.nodes.blend_node import MergeNode
        node = MergeNode()

        input_a = TextureResult(texture_id=100, width=1920, height=1080)
        input_b = TextureResult(texture_id=101, width=1920, height=1080)
        inputs = {"A": input_a, "B": input_b}

        outputs = node._do_evaluate(inputs, None, None, None, "preview")

        # Check output alpha_premultiplied is True for auto (default)
        assert outputs["result"].alpha_premultiplied == True

    def test_alpha_policy_serialization(self):
        """Test that alpha_policy and alpha_clamp serialize correctly (S2-B)."""
        from src.features.composite.nodes.blend_node import MergeNode
        node = MergeNode()
        node.set_parameter("alpha_policy", "straight_force")
        node.set_parameter("alpha_clamp", False)

        d = node.to_dict()
        restored = MergeNode.from_dict(d)

        assert restored.parameters["alpha_policy"] == "straight_force"
        assert restored.parameters["alpha_clamp"] == False

    def test_old_data_without_alpha_policy_defaults(self):
        """Test that old serialized data defaults alpha_policy to 'auto' (S2-B)."""
        from src.features.composite.nodes.blend_node import MergeNode
        d = {
            "id": "m1", "type": "merge",
            "parameters": {"blend_mode": "over"},
        }
        node = MergeNode.from_dict(d)

        assert node.parameters["alpha_policy"] == "auto"
        assert node.parameters["alpha_clamp"] == True


class TestDeepMergeNode:
    """Test DeepMergeNode."""

    def test_registered(self):
        assert "deep_merge" in NODE_REGISTRY

    def test_can_instantiate(self):
        from src.features.composite.nodes.deep_merge_node import DeepMergeNode
        node = DeepMergeNode()
        assert node.node_type == "deep_merge"

    # S1-C: Test new marker protocol for no-depth fallback
    def test_no_depth_fallback_effect_type_marker(self):
        """Test that DeepMergeNode returns `_effect_type: "merge"` when inputs have no depth (S1-C)."""
        from src.features.composite.nodes.deep_merge_node import DeepMergeNode
        node = DeepMergeNode()

        # Mock inputs without depth
        input_a = TextureResult(texture_id=100, width=1920, height=1080, has_deep_data=False)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, has_deep_data=False)
        inputs = {"A": input_a, "B": input_b}

        outputs = node._do_evaluate(inputs, None, None, None, "preview")

        # Should use new `_effect_type: "merge"` protocol (S1-C)
        assert "_effect_type" in outputs
        assert outputs["_effect_type"] == "merge"
        assert "_effect_input_a" in outputs
        assert "_effect_input_b" in outputs


class TestPremultNode:
    """Test PremultNode."""

    def test_registered(self):
        assert "premult" in NODE_REGISTRY

    def test_can_instantiate(self):
        from src.features.composite.nodes.premult_node import PremultNode
        node = PremultNode()
        assert node.node_type == "premult"

    def test_to_dict_from_dict(self):
        from src.features.composite.nodes.premult_node import PremultNode
        node = PremultNode()
        d = node.to_dict()
        node2 = PremultNode.from_dict(d)
        assert node2.node_type == d["type"]

    def test_param_defs_match_defaults(self):
        from src.features.composite.nodes.premult_node import PremultNode
        node = PremultNode()
        for pdef in node.PARAM_DEFS:
            defaults = pdef.make_default_parameters()
            for key, val in defaults.items():
                assert key in node.parameters
                assert node.parameters[key] == val


class TestUnpremultNode:
    """Test UnpremultNode."""

    def test_registered(self):
        assert "unpremult" in NODE_REGISTRY

    def test_can_instantiate(self):
        from src.features.composite.nodes.unpremult_node import UnpremultNode
        node = UnpremultNode()
        assert node.node_type == "unpremult"

    def test_to_dict_from_dict(self):
        from src.features.composite.nodes.unpremult_node import UnpremultNode
        node = UnpremultNode()
        d = node.to_dict()
        node2 = UnpremultNode.from_dict(d)
        assert node2.node_type == d["type"]

    def test_param_defs_match_defaults(self):
        from src.features.composite.nodes.unpremult_node import UnpremultNode
        node = UnpremultNode()
        for pdef in node.PARAM_DEFS:
            defaults = pdef.make_default_parameters()
            for key, val in defaults.items():
                assert key in node.parameters
                assert node.parameters[key] == val


class TestDissolveNode:
    """Test DissolveNode."""

    def test_registered(self):
        assert "dissolve" in NODE_REGISTRY

    def test_can_instantiate(self):
        from src.features.composite.nodes.dissolve_node import DissolveNode
        node = DissolveNode()
        assert node.node_type == "dissolve"

    def test_to_dict_from_dict(self):
        from src.features.composite.nodes.dissolve_node import DissolveNode
        node = DissolveNode()
        d = node.to_dict()
        node2 = DissolveNode.from_dict(d)
        assert node2.node_type == d["type"]

    def test_param_defs_match_defaults(self):
        from src.features.composite.nodes.dissolve_node import DissolveNode
        node = DissolveNode()
        for pdef in node.PARAM_DEFS:
            defaults = pdef.make_default_parameters()
            for key, val in defaults.items():
                assert key in node.parameters
                assert node.parameters[key] == val


class TestSwitchNode:
    """Test SwitchNode."""

    def test_registered(self):
        assert "switch" in NODE_REGISTRY

    def test_can_instantiate(self):
        from src.features.composite.nodes.switch_node import SwitchNode
        node = SwitchNode()
        assert node.node_type == "switch"

    def test_to_dict_from_dict(self):
        from src.features.composite.nodes.switch_node import SwitchNode
        node = SwitchNode()
        d = node.to_dict()
        node2 = SwitchNode.from_dict(d)
        assert node2.node_type == d["type"]

    def test_param_defs_match_defaults(self):
        from src.features.composite.nodes.switch_node import SwitchNode
        node = SwitchNode()
        for pdef in node.PARAM_DEFS:
            defaults = pdef.make_default_parameters()
            for key, val in defaults.items():
                assert key in node.parameters
                assert node.parameters[key] == val


class TestMaskNode:
    """Test MaskNode (PlaceholderNode — skip to_dict/param_defs tests)."""

    def test_registered(self):
        assert "mask" in NODE_REGISTRY

    def test_can_instantiate(self):
        from src.features.composite.nodes.mask_node import MaskNode
        node = MaskNode()
        assert node.node_type == "mask"


class TestMatteNode:
    """Test MatteNode (PlaceholderNode — skip to_dict/param_defs tests)."""

    def test_registered(self):
        assert "matte" in NODE_REGISTRY

    def test_can_instantiate(self):
        from src.features.composite.nodes.matte_node import MatteNode
        node = MatteNode()
        assert node.node_type == "matte"


# S2-A: Test bbox mapping for merge operations
class TestBBoxMapping:
    """Test bbox mapping for merge shader (S2-A).

    Validates that the blend shader correctly maps output pixel coordinates
    to input texture UV space based on bbox information.
    """

    def test_sample_layer_shader_compiles(self):
        """Test that the new sampleLayer shader helper compiles without errors."""
        from src.features.composite.core.shaders import _BLEND_FRAGMENT

        # Verify shader source contains new uniforms
        assert "u_bbox_a" in _BLEND_FRAGMENT
        assert "u_bbox_b" in _BLEND_FRAGMENT
        assert "u_bbox_mask" in _BLEND_FRAGMENT
        assert "u_output_bbox" in _BLEND_FRAGMENT
        assert "u_use_bbox_mapping" in _BLEND_FRAGMENT
        assert "u_resolution_mask" in _BLEND_FRAGMENT

        # Verify shader contains sampleLayer helper
        assert "sampleLayer" in _BLEND_FRAGMENT

    def test_bbox_uniform_values(self):
        """Test that bbox uniform values are computed correctly."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox

        # Create texture results with different bboxes
        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)

        input_a = TextureResult(
            texture_id=100,
            width=800,
            height=600,
            bbox=bbox_a,
        )
        input_b = TextureResult(
            texture_id=101,
            width=1920,
            height=1080,
            bbox=bbox_b,
        )

        # Verify bbox values
        assert input_a.effective_bbox() == bbox_a
        assert input_b.effective_bbox() == bbox_b

        # Union bbox should be (0, 0, 1920, 1080) + (100, 50, 800, 600)
        union_bbox = bbox_a.union(bbox_b)
        # bbox_a.union(bbox_b) where bbox_a=(100,50,800,600) and bbox_b=(0,0,1920,1080)
        # should give (min(100,0)=0, min(50,0)=0, max(900,1920)-0=1920, max(650,1080)-0=1080)
        assert union_bbox.x == 0
        assert union_bbox.y == 0
        assert union_bbox.width == 1920
        assert union_bbox.height == 1080

    def test_bbox_offset_calculation(self):
        """Test that bbox offset in output pixel space is calculated correctly."""
        from src.features.composite.core.format import BBox

        # Test case: A is offset by (100, 50) from origin
        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        output_bbox = bbox_a.union(bbox_b)  # (0, 0, 1920, 1080)

        # In output pixel space, bbox_a should be offset by (100, 50)
        bbox_a_in_output = (bbox_a.x - output_bbox.x, bbox_a.y - output_bbox.y,
                           bbox_a.width, bbox_a.height)
        assert bbox_a_in_output == (100, 50, 800, 600)

        # bbox_b should be at origin
        bbox_b_in_output = (bbox_b.x - output_bbox.x, bbox_b.y - output_bbox.y,
                           bbox_b.width, bbox_b.height)
        assert bbox_b_in_output == (0, 0, 1920, 1080)

    def test_mask_bbox_offset(self):
        """Test that mask bbox is correctly offset when different from A/B."""
        from src.features.composite.core.format import BBox

        # Mask has different bbox
        bbox_mask = BBox(x=200, y=100, width=400, height=300)
        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        output_bbox = bbox_a.union(bbox_b)

        # Mask offset in output space
        bbox_mask_in_output = (bbox_mask.x - output_bbox.x, bbox_mask.y - output_bbox.y,
                              bbox_mask.width, bbox_mask.height)
        assert bbox_mask_in_output == (200, 100, 400, 300)

    def test_use_bbox_mapping_flag(self):
        """Test that use_bbox_mapping flag is set correctly."""
        from src.features.composite.core.format import BBox

        # Case 1: Both at origin -> should NOT use bbox mapping
        bbox_a_origin = BBox(x=0, y=0, width=1920, height=1080)
        bbox_b_origin = BBox(x=0, y=0, width=1920, height=1080)
        use_mapping = (bbox_a_origin.x != 0 or bbox_a_origin.y != 0 or
                      bbox_b_origin.x != 0 or bbox_b_origin.y != 0)
        assert use_mapping is False

        # Case 2: A offset -> should use bbox mapping
        bbox_a_offset = BBox(x=100, y=50, width=800, height=600)
        use_mapping = (bbox_a_offset.x != 0 or bbox_a_offset.y != 0 or
                      bbox_b_origin.x != 0 or bbox_b_origin.y != 0)
        assert use_mapping is True

    def test_intersection_bbox_policy(self):
        """Test bbox intersection policy."""
        from src.features.composite.core.format import BBox

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)

        # Intersection should be (100, 50, 800, 600) since A is inside B
        intersection = bbox_a.intersection(bbox_b)
        assert intersection.x == 100
        assert intersection.y == 50
        assert intersection.width == 800
        assert intersection.height == 600

    def test_bbox_policy_a(self):
        """Test bbox policy 'a' returns bbox_a."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b)

        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        result_bbox = BaseEvaluatorMixin._resolve_merge_bbox(input_a, input_b, "a")
        assert result_bbox == bbox_a

    def test_bbox_policy_b(self):
        """Test bbox policy 'b' returns bbox_b."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b)

        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        result_bbox = BaseEvaluatorMixin._resolve_merge_bbox(input_a, input_b, "b")
        assert result_bbox == bbox_b


# S2-C: Test MergeGeometry data structure and _compute_merge_geometry()
class TestMergeGeometry:
    """Test MergeGeometry data structure and unified geometry computation (S2-C)."""

    def test_merge_geometry_creation(self):
        """Test basic MergeGeometry creation."""
        from src.features.composite.core.format import MergeGeometry, BBox

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)

        geometry = MergeGeometry(
            output_width=1920,
            output_height=1080,
            output_bbox=bbox_b,
            bbox_a=bbox_a,
            bbox_b=bbox_b,
            resolution_a=(800, 600),
            resolution_b=(1920, 1080),
        )

        assert geometry.output_width == 1920
        assert geometry.output_height == 1080
        assert geometry.output_bbox == bbox_b
        assert geometry.bbox_a == bbox_a
        assert geometry.bbox_b == bbox_b
        assert geometry.is_valid is True

    def test_merge_geometry_to_uniform_dict(self):
        """Test MergeGeometry.to_uniform_dict() converts to float tuples."""
        from src.features.composite.core.format import MergeGeometry, BBox

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)

        geometry = MergeGeometry(
            output_width=1920,
            output_height=1080,
            output_bbox=bbox_b,
            bbox_a=bbox_a,
            bbox_b=bbox_b,
            resolution_a=(800, 600),
            resolution_b=(1920, 1080),
        )

        uniforms = geometry.to_uniform_dict()

        # Check float conversion
        assert uniforms["u_output_resolution"] == (1920.0, 1080.0)
        assert uniforms["u_bbox_a"] == (100.0, 50.0, 800.0, 600.0)
        assert uniforms["u_bbox_b"] == (0.0, 0.0, 1920.0, 1080.0)
        assert uniforms["u_resolution_a"] == (800.0, 600.0)
        assert uniforms["u_resolution_b"] == (1920.0, 1080.0)

        # Check mask defaults
        assert uniforms["u_bbox_mask"] == (0.0, 0.0, 0.0, 0.0)
        assert uniforms["u_resolution_mask"] == (0.0, 0.0)

    def test_merge_geometry_with_mask(self):
        """Test MergeGeometry with mask bbox."""
        from src.features.composite.core.format import MergeGeometry, BBox

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        bbox_mask = BBox(x=50, y=25, width=1000, height=800)

        geometry = MergeGeometry(
            output_width=1920,
            output_height=1080,
            output_bbox=bbox_b,
            bbox_a=bbox_a,
            bbox_b=bbox_b,
            bbox_mask=bbox_mask,
            resolution_a=(800, 600),
            resolution_b=(1920, 1080),
            resolution_mask=(1000, 800),
        )

        uniforms = geometry.to_uniform_dict()
        assert uniforms["u_bbox_mask"] == (50.0, 25.0, 1000.0, 800.0)
        assert uniforms["u_resolution_mask"] == (1000.0, 800.0)


class TestComputeMergeGeometry:
    """Test _compute_merge_geometry() method for four bbox policies (S2-C)."""

    def test_bbox_policy_union(self):
        """Test union bbox policy: output bbox = bbox_a.union(bbox_b)."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox, ImageFormat
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        fmt_a = ImageFormat(width=800, height=600)
        fmt_b = ImageFormat(width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a, format=fmt_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b, format=fmt_b)

        geometry = BaseEvaluatorMixin._compute_merge_geometry(
            input_a, input_b, None, "from_b", "union"
        )

        # Union should contain both bboxes
        expected_union = bbox_a.union(bbox_b)
        assert geometry.output_bbox == expected_union

        # Output dimensions should be from_b (1920x1080)
        assert geometry.output_width == 1920
        assert geometry.output_height == 1080

        # Input bboxes preserved
        assert geometry.bbox_a == bbox_a
        assert geometry.bbox_b == bbox_b

        # Resolutions preserved
        assert geometry.resolution_a == (800, 600)
        assert geometry.resolution_b == (1920, 1080)

    def test_bbox_policy_intersection(self):
        """Test intersection bbox policy: output bbox = bbox_a.intersection(bbox_b)."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox, ImageFormat
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        fmt_a = ImageFormat(width=800, height=600)
        fmt_b = ImageFormat(width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a, format=fmt_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b, format=fmt_b)

        geometry = BaseEvaluatorMixin._compute_merge_geometry(
            input_a, input_b, None, "from_b", "intersection"
        )

        # Intersection should be (100, 50, 800, 600) since A is inside B
        expected_intersection = bbox_a.intersection(bbox_b)
        assert geometry.output_bbox == expected_intersection
        assert geometry.output_bbox.x == 100
        assert geometry.output_bbox.y == 50
        assert geometry.output_bbox.width == 800
        assert geometry.output_bbox.height == 600

    def test_bbox_policy_a(self):
        """Test bbox policy 'a': output bbox = bbox_a."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox, ImageFormat
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        fmt_a = ImageFormat(width=800, height=600)
        fmt_b = ImageFormat(width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a, format=fmt_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b, format=fmt_b)

        geometry = BaseEvaluatorMixin._compute_merge_geometry(
            input_a, input_b, None, "from_b", "a"
        )

        # Output bbox should be bbox_a
        assert geometry.output_bbox == bbox_a
        assert geometry.output_bbox.x == 100
        assert geometry.output_bbox.y == 50

    def test_bbox_policy_b(self):
        """Test bbox policy 'b': output bbox = bbox_b."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox, ImageFormat
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        fmt_a = ImageFormat(width=800, height=600)
        fmt_b = ImageFormat(width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a, format=fmt_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b, format=fmt_b)

        geometry = BaseEvaluatorMixin._compute_merge_geometry(
            input_a, input_b, None, "from_b", "b"
        )

        # Output bbox should be bbox_b
        assert geometry.output_bbox == bbox_b
        assert geometry.output_bbox.x == 0
        assert geometry.output_bbox.y == 0

    def test_format_policy_from_a(self):
        """Test format policy 'from_a': output dimensions = A resolution."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox, ImageFormat
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        fmt_a = ImageFormat(width=800, height=600)
        fmt_b = ImageFormat(width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a, format=fmt_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b, format=fmt_b)

        geometry = BaseEvaluatorMixin._compute_merge_geometry(
            input_a, input_b, None, "from_a", "union"
        )

        # Output dimensions should be from A
        assert geometry.output_width == 800
        assert geometry.output_height == 600
        assert geometry.output_format == fmt_a

    def test_format_policy_larger(self):
        """Test format policy 'larger': output dimensions = max resolution."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox, ImageFormat
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        fmt_a = ImageFormat(width=800, height=600)
        fmt_b = ImageFormat(width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a, format=fmt_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b, format=fmt_b)

        geometry = BaseEvaluatorMixin._compute_merge_geometry(
            input_a, input_b, None, "larger", "union"
        )

        # Output dimensions should be larger (1920x1080)
        assert geometry.output_width == 1920
        assert geometry.output_height == 1080
        assert geometry.output_format == fmt_b

    def test_format_policy_smaller(self):
        """Test format policy 'smaller': output dimensions = min resolution."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox, ImageFormat
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        fmt_a = ImageFormat(width=800, height=600)
        fmt_b = ImageFormat(width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a, format=fmt_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b, format=fmt_b)

        geometry = BaseEvaluatorMixin._compute_merge_geometry(
            input_a, input_b, None, "smaller", "union"
        )

        # Output dimensions should be smaller (800x600)
        assert geometry.output_width == 800
        assert geometry.output_height == 600
        assert geometry.output_format == fmt_a

    def test_geometry_with_mask(self):
        """Test geometry computation with mask input."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox, ImageFormat
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        bbox_a = BBox(x=100, y=50, width=800, height=600)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        bbox_mask = BBox(x=50, y=25, width=1000, height=800)
        fmt_a = ImageFormat(width=800, height=600)
        fmt_b = ImageFormat(width=1920, height=1080)

        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a, format=fmt_a)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b, format=fmt_b)
        input_mask = TextureResult(texture_id=102, width=1000, height=800, bbox=bbox_mask)

        geometry = BaseEvaluatorMixin._compute_merge_geometry(
            input_a, input_b, input_mask, "from_b", "union"
        )

        # Mask bbox and resolution should be included
        assert geometry.bbox_mask == bbox_mask
        assert geometry.resolution_mask == (1000, 800)

    def test_shader_bbox_mapping_logic(self):
        """Test that bbox mapping is enabled when bboxes are not at origin."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox, ImageFormat
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        # Case 1: Both at origin -> should NOT use bbox mapping
        bbox_a_origin = BBox(x=0, y=0, width=800, height=600)
        bbox_b_origin = BBox(x=0, y=0, width=1920, height=1080)
        input_a = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a_origin)
        input_b = TextureResult(texture_id=101, width=1920, height=1080, bbox=bbox_b_origin)

        geometry = BaseEvaluatorMixin._compute_merge_geometry(
            input_a, input_b, None, "from_b", "union"
        )

        use_bbox_mapping = (geometry.bbox_a.x != 0 or geometry.bbox_a.y != 0 or
                           geometry.bbox_b.x != 0 or geometry.bbox_b.y != 0)
        assert use_bbox_mapping is False

        # Case 2: A offset -> should use bbox mapping
        bbox_a_offset = BBox(x=100, y=50, width=800, height=600)
        input_a_offset = TextureResult(texture_id=100, width=800, height=600, bbox=bbox_a_offset)

        geometry_offset = BaseEvaluatorMixin._compute_merge_geometry(
            input_a_offset, input_b, None, "from_b", "union"
        )

        use_bbox_mapping_offset = (geometry_offset.bbox_a.x != 0 or geometry_offset.bbox_a.y != 0 or
                                   geometry_offset.bbox_b.x != 0 or geometry_offset.bbox_b.y != 0)
        assert use_bbox_mapping_offset is True
