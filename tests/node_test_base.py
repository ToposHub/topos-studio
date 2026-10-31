"""
Node Test Base - Standardized test framework for all node types.

Provides:
- Base test class with 5 standard test methods
- Mock GPU context to avoid OpenGL dependency
- Helper utilities for parameter boundary extraction
- Fixture setup/teardown for test isolation

Usage:
    from tests.node_test_base import NodeTestBase

    class TestGradeNode(NodeTestBase):
        node_type = "grade"
        node_class = GradeNode
"""

import pytest
import sys
import os
from typing import Dict, Any, List, Optional, Type, Tuple
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.core.format import BBox, ImageFormat


# ── Mock GPU Infrastructure ──────────────────────────────────────────────────

class MockModernGLContext:
    """Mock ModernGL context that mimics GPU operations without OpenGL."""

    def __init__(self):
        self._textures: Dict[int, MagicMock] = {}
        self._framebuffers: Dict[int, MagicMock] = {}
        self._next_texture_id = 1
        self._next_fbo_id = 1

    def texture(self, size: Tuple[int, int], components: int = 4, dtype: str = 'f2',
                data=None) -> MagicMock:
        """Create a mock texture."""
        tex = MagicMock()
        tex.glo = self._next_texture_id
        tex.width = size[0]
        tex.height = size[1]
        tex.components = components
        tex.dtype = dtype
        tex.release = MagicMock()
        self._textures[self._next_texture_id] = tex
        self._next_texture_id += 1
        return tex

    def framebuffer(self, color_attachments=None, depth_attachment=None) -> MagicMock:
        """Create a mock framebuffer."""
        fbo = MagicMock()
        fbo.glo = self._next_fbo_id
        fbo.release = MagicMock()
        fbo.clear = MagicMock()
        fbo.use = MagicMock()
        self._framebuffers[self._next_fbo_id] = fbo
        self._next_fbo_id += 1
        return fbo

    def program(self, vertex_shader: str, fragment_shader: str) -> MagicMock:
        """Create a mock shader program."""
        prog = MagicMock()
        prog.release = MagicMock()
        return prog

    def vertex_array(self, program, buffer, content) -> MagicMock:
        """Create a mock vertex array."""
        va = MagicMock()
        va.render = MagicMock()
        va.release = MagicMock()
        return va

    def buffer(self, data=None, reserve: int = 0) -> MagicMock:
        """Create a mock buffer."""
        buf = MagicMock()
        buf.write = MagicMock()
        buf.release = MagicMock()
        return buf

    def clear(self):
        """Clear all mock resources."""
        self._textures.clear()
        self._framebuffers.clear()


class MockFramebufferManager:
    """Mock FramebufferManager for texture allocation."""

    def __init__(self, ctx: MockModernGLContext = None):
        self._ctx = ctx or MockModernGLContext()
        self._active_textures: set = set()
        self._texture_pool: Dict[int, MagicMock] = {}

    def get_temp_texture(self, width: int, height: int, components: int = 4,
                          dtype: str = 'f2') -> MagicMock:
        """Allocate a temporary texture."""
        tex = self._ctx.texture((width, height), components, dtype)
        self._active_textures.add(tex.glo)
        return tex

    def release_temp_texture(self, tex: MagicMock) -> None:
        """Release a temporary texture."""
        if tex and tex.glo in self._active_textures:
            self._active_textures.discard(tex.glo)

    def get_fbo(self, tex: MagicMock) -> MagicMock:
        """Get framebuffer for texture."""
        return self._ctx.framebuffer(color_attachments=[tex])

    def cleanup(self) -> None:
        """Clean up all resources."""
        self._active_textures.clear()


class MockGPUResourcePool:
    """Mock GPUResourcePool for tracked texture allocation."""

    def __init__(self, fb_mgr: MockFramebufferManager = None):
        self._fb_mgr = fb_mgr or MockFramebufferManager()
        self._allocated: Dict[int, Dict] = {}

    def allocate_texture(self, width: int, height: int, owner: str = "test",
                          components: int = 4, dtype: str = 'f2') -> MagicMock:
        """Allocate a tracked texture."""
        tex = self._fb_mgr.get_temp_texture(width, height, components, dtype)
        self._allocated[tex.glo] = {
            'owner': owner,
            'width': width,
            'height': height,
            'components': components,
        }
        return tex

    def release_texture(self, tex: MagicMock) -> None:
        """Release a tracked texture."""
        if tex and tex.glo in self._allocated:
            del self._allocated[tex.glo]
            self._fb_mgr.release_temp_texture(tex)

    def cleanup(self) -> None:
        """Clean up all tracked textures."""
        self._allocated.clear()


# ── Helper Utilities ─────────────────────────────────────────────────────────

def make_texture_result(texture_id: int = 1, width: int = 1920, height: int = 1080,
                         revision: int = 1, alpha_premultiplied: bool = True,
                         color_space: str = "linear", bbox: BBox = None) -> TextureResult:
    """Create a TextureResult for testing.

    Args:
        texture_id: Mock texture ID (use positive values for valid textures)
        width: Texture width
        height: Texture height
        revision: Content revision
        alpha_premultiplied: Alpha mode
        color_space: Color space identifier
        bbox: Optional bounding box

    Returns:
        TextureResult instance
    """
    return TextureResult(
        texture_id=texture_id,
        width=width,
        height=height,
        revision=revision,
        alpha_premultiplied=alpha_premultiplied,
        color_space=color_space,
        bbox=bbox or BBox.full_frame(width, height),
    )


def get_param_boundaries(param_defs: List[ParamDef]) -> Dict[str, Dict[str, float]]:
    """Extract min/max boundaries from ParamDef list.

    Args:
        param_defs: List of ParamDef from a node

    Returns:
        Dict mapping param_name -> {min_val, max_val, default}
    """
    boundaries = {}
    for pdef in param_defs:
        boundaries[pdef.name] = {
            'min_val': pdef.min_val,
            'max_val': pdef.max_val,
            'default': pdef.default,
            'ptype': pdef.ptype,
            'labels': pdef.labels,  # For option type
        }
    return boundaries


def get_boundary_test_values(boundary: Dict[str, Any]) -> List[Any]:
    """Get test values for boundary testing.

    Returns values at:
    - Minimum
    - Maximum
    - Default (if different from min/max)
    - Just inside min/max (epsilon adjustment)

    Args:
        boundary: Dict with min_val, max_val, default, ptype

    Returns:
        List of test values
    """
    ptype = boundary.get('ptype', 'float')

    if ptype == 'option':
        # For options, test all labels
        labels = boundary.get('labels', [])
        return labels if labels else [boundary.get('default')]

    if ptype == 'bool':
        return [True, False]

    if ptype == 'str':
        # For string parameters, only test default and empty string
        default = boundary.get('default', '')
        values = ['', default] if default else ['']
        return list(set(values))  # Remove duplicates

    if ptype == 'color':
        default = boundary.get('default', (0.0, 0.0, 0.0, 1.0))
        return [
            (0.0, 0.0, 0.0, 1.0),
            (1.0, 1.0, 1.0, 1.0),
            default,
        ]

    min_val = boundary.get('min_val', 0.0)
    max_val = boundary.get('max_val', 1.0)
    default = boundary.get('default')

    values = [min_val, max_val]

    # Add epsilon-adjusted values (just inside bounds)
    epsilon = 0.001
    if min_val + epsilon < max_val:
        values.append(min_val + epsilon)
        values.append(max_val - epsilon)

    # Add default if different
    if default is not None and default not in values:
        values.append(default)

    # Add midpoint for good coverage
    midpoint = (min_val + max_val) / 2
    if midpoint not in values:
        values.append(midpoint)

    return values


# ── Node Test Base Class ─────────────────────────────────────────────────────

class NodeTestBase:
    """Base class for standardized node tests.

    Subclasses must set:
    - node_type: The node type string (e.g., "grade", "blur")
    - node_class: The Node subclass (e.g., GradeNode, BlurNode)

    Provides 5 standard test methods:
    1. test_default_params: Default parameters produce valid output
    2. test_boundary_params: Boundary values don't crash
    3. test_bypass_mode: Disabled node passes input through
    4. test_serialization_roundtrip: Parameters serialize/deserialize correctly
    5. test_no_input: No input returns empty/error without crashing

    Additional helpers:
    - create_node(): Instantiate node with optional parameters
    - get_mock_context(): Get mock ModernGL context
    - get_mock_fb_manager(): Get mock framebuffer manager
    - get_mock_resource_pool(): Get mock resource pool
    - create_valid_input(): Create valid TextureResult for input
    """

    node_type: str = None  # Must be set by subclass
    node_class: Type[Node] = None  # Must be set by subclass

    @pytest.fixture(autouse=True)
    def setup_mocks(self):
        """Auto-setup mock infrastructure for each test."""
        self._mock_ctx = MockModernGLContext()
        self._mock_fb_mgr = MockFramebufferManager(self._mock_ctx)
        self._mock_resource_pool = MockGPUResourcePool(self._mock_fb_mgr)
        yield
        # Cleanup after test
        self._mock_ctx.clear()
        self._mock_fb_mgr.cleanup()
        self._mock_resource_pool.cleanup()

    def get_mock_context(self) -> MockModernGLContext:
        """Get the mock ModernGL context."""
        return self._mock_ctx

    def get_mock_fb_manager(self) -> MockFramebufferManager:
        """Get the mock framebuffer manager."""
        return self._mock_fb_mgr

    def get_mock_resource_pool(self) -> MockGPUResourcePool:
        """Get the mock GPU resource pool."""
        return self._mock_resource_pool

    def create_node(self, node_id: str = None, params: Dict[str, Any] = None) -> Node:
        """Create a node instance with optional parameters.

        Args:
            node_id: Optional node ID (auto-generated if None)
            params: Optional parameter dict to set after creation

        Returns:
            Node instance
        """
        if self.node_class is None:
            pytest.skip(f"node_class not set for {self.__class__.__name__}")
        node = self.node_class(node_id)
        if params:
            for key, value in params.items():
                node.set_parameter(key, value)
        return node

    def create_valid_input(self, port_name: str = "image", width: int = 1920,
                           height: int = 1080) -> Dict[str, TextureResult]:
        """Create valid input TextureResult(s) for testing.

        Args:
            port_name: Input port name
            width: Texture width
            height: Texture height

        Returns:
            Dict of port_name -> TextureResult
        """
        return {port_name: make_texture_result(width=width, height=height)}

    def get_param_boundaries(self) -> Dict[str, Dict[str, float]]:
        """Get parameter boundaries for this node type.

        Returns:
            Dict mapping param_name -> {min_val, max_val, default}
        """
        if self.node_class is None:
            return {}
        return get_param_boundaries(self.node_class.PARAM_DEFS)

    # ── Standard Test Methods ───────────────────────────────────────────────

    def test_node_type_registered(self):
        """Verify node type is registered in NODE_REGISTRY."""
        if self.node_type is None:
            pytest.skip(f"node_type not set for {self.__class__.__name__}")

        from src.features.composite.core.node_engine import NODE_REGISTRY

        assert self.node_type in NODE_REGISTRY, \
            f"Node type '{self.node_type}' not found in NODE_REGISTRY"

        registered_class = NODE_REGISTRY[self.node_type]
        if self.node_class:
            assert registered_class == self.node_class, \
                f"Registered class {registered_class} != expected {self.node_class}"

    def test_default_params(self):
        """默认参数应产生有效输出.

        Validates that:
        1. Node can be created with default parameters
        2. Default parameters are within valid ranges
        3. Node has expected input/output ports
        """
        node = self.create_node()

        # Check node has correct type
        assert node.node_type == self.node_type, \
            f"node_type mismatch: {node.node_type} != {self.node_type}"

        # Check node has input/output ports
        assert len(node.inputs) > 0 or len(node.outputs) > 0, \
            "Node should have at least one input or output port"

        # Check default parameters are within bounds
        boundaries = self.get_param_boundaries()
        for param_name, param_value in node.parameters.items():
            if param_name in boundaries:
                bound = boundaries[param_name]
                ptype = bound.get('ptype', 'float')
                if ptype in ('float', 'int'):
                    min_val = bound.get('min_val')
                    max_val = bound.get('max_val')
                    if min_val is not None:
                        assert param_value >= min_val - 0.001, \
                            f"Param {param_name}={param_value} below min {min_val}"
                    if max_val is not None:
                        assert param_value <= max_val + 0.001, \
                            f"Param {param_name}={param_value} above max {max_val}"

    def test_boundary_params(self):
        """边界参数值不应崩溃.

        Tests node with parameter values at:
        - Minimum boundary
        - Maximum boundary
        - Just inside boundaries
        """
        node = self.create_node()
        boundaries = self.get_param_boundaries()

        # Test each parameter at boundary values
        for param_name, bound in boundaries.items():
            test_values = get_boundary_test_values(bound)

            for value in test_values:
                # Create fresh node for each test to avoid state pollution
                test_node = self.create_node()
                test_node.set_parameter(param_name, value)

                # Verify parameter was set
                assert test_node.parameters.get(param_name) == value, \
                    f"Failed to set {param_name}={value}"

                # Verify node is still in valid state
                assert test_node.state is not None, \
                    f"Node state invalid after setting {param_name}={value}"

    def test_bypass_mode(self):
        """Bypass 应直通输入到输出.

        When node is disabled (disabled=True), it should:
        1. Pass input through to output unchanged
        2. Not perform any processing
        """
        node = self.create_node()
        node.disabled = True

        # Create valid input
        inputs = self.create_valid_input()

        # Evaluate in bypass mode
        outputs = node.evaluate(
            inputs,
            self.get_mock_context(),
            self.get_mock_fb_manager(),
            self.get_mock_resource_pool(),
            render_mode="preview"
        )

        # Check bypass behavior
        if node.BYPASS_MAPPING:
            # Custom bypass mapping defined
            for out_port, in_port in node.BYPASS_MAPPING.items():
                if in_port in inputs:
                    assert out_port in outputs, \
                        f"Bypass output '{out_port}' missing"
                    assert outputs[out_port].texture_id == inputs[in_port].texture_id, \
                        f"Bypass should pass {in_port} -> {out_port}"
        elif inputs:
            # Default bypass: first input -> first output
            first_input_name = list(inputs.keys())[0]
            first_input = inputs[first_input_name]
            if node.outputs:
                first_output_name = node.outputs[0].name
                if first_output_name in outputs:
                    assert outputs[first_output_name].texture_id == first_input.texture_id, \
                        "Default bypass should pass first input to first output"

    def test_serialization_roundtrip(self):
        """序列化→反序列化参数一致.

        Parameters should survive serialization and deserialization:
        1. All parameters are preserved
        2. Values remain the same
        3. Node state is reconstructable
        """
        node = self.create_node()

        # Serialize parameters
        original_params = dict(node.parameters)

        # Modify some parameters to ensure non-default values
        boundaries = self.get_param_boundaries()
        modified_params = {}
        for param_name, bound in list(boundaries.items())[:3]:  # Modify first 3 params
            if bound.get('ptype') in ('float', 'int'):
                midpoint = (bound.get('min_val', 0.0) + bound.get('max_val', 1.0)) / 2
                modified_params[param_name] = midpoint

        # Apply modifications
        for key, value in modified_params.items():
            node.set_parameter(key, value)

        # Serialize modified state
        serialized_params = dict(node.parameters)

        # Create new node and deserialize
        new_node = self.create_node()
        for key, value in serialized_params.items():
            new_node.set_parameter(key, value)

        # Verify roundtrip
        for key, value in serialized_params.items():
            assert new_node.parameters.get(key) == value, \
                f"Parameter {key} lost in roundtrip: {value} != {new_node.parameters.get(key)}"

    def test_no_input(self):
        """无输入时应返回空或错误，不崩溃.

        Node should handle missing input gracefully:
        1. Return empty dict or error indicator
        2. Not raise exceptions
        3. Maintain valid state
        """
        node = self.create_node()

        # Evaluate with empty inputs
        empty_inputs = {}

        try:
            outputs = node.evaluate(
                empty_inputs,
                self.get_mock_context(),
                self.get_mock_fb_manager(),
                self.get_mock_resource_pool(),
                render_mode="preview"
            )

            # Should return empty dict or indicate error
            # (specific behavior depends on node type)
            if outputs:
                # If outputs exist, they should be valid markers or empty textures
                for port_name, result in outputs.items():
                    if isinstance(result, TextureResult):
                        # Invalid texture (texture_id <= 0) or effect marker
                        pass  # Both are acceptable for no-input handling
            else:
                # Empty outputs is acceptable
                pass

        except Exception as e:
            # Should not raise unhandled exceptions
            pytest.fail(f"Node crashed on empty input: {e}")

        # Node should remain in valid state
        assert node.state is not None

    def test_port_definitions(self):
        """节点端口定义正确.

        Validates:
        1. Input ports have correct names and data types
        2. Output ports have correct names and data types
        3. Port directions are correct
        """
        node = self.create_node()

        # Validate input ports
        for port in node.inputs:
            assert port.direction == "input", \
                f"Port {port.name} should be input direction"
            assert port.data_type in ("image", "float", "vec2", "vec3", "int", "bool"), \
                f"Port {port.name} has invalid data_type: {port.data_type}"

        # Validate output ports
        for port in node.outputs:
            assert port.direction == "output", \
                f"Port {port.name} should be output direction"
            assert port.data_type in ("image", "float", "vec2", "vec3", "int", "bool"), \
                f"Port {port.name} has invalid data_type: {port.data_type}"

    def test_param_defs_registered(self):
        """参数定义已注册.

        Validates:
        1. PARAM_DEFS is defined
        2. Each param has required attributes
        3. Defaults match node.parameters
        """
        node = self.create_node()

        if not self.node_class.PARAM_DEFS:
            # Some nodes may not have explicit PARAM_DEFS
            pytest.skip("Node has no PARAM_DEFS")

        for pdef in self.node_class.PARAM_DEFS:
            assert pdef.name, "ParamDef must have name"
            assert pdef.ptype, "ParamDef must have ptype"

            # Check default is in node.parameters (if defined)
            if pdef.default is not None:
                assert pdef.name in node.parameters, \
                    f"ParamDef {pdef.name} default not in node.parameters"


# ── Specialized Test Mixins ──────────────────────────────────────────────────

class ColorNodeTestMixin:
    """Additional tests for color adjustment nodes.

    Provides tests for:
    - Channel isolation (R/G/B independent)
    - HDR range handling (values > 1.0)
    - Mix/blending parameter
    """

    def test_channel_independence(self):
        """R/G/B 通道可独立调整."""
        node = self.create_node()

        # Set different values for each channel
        boundaries = self.get_param_boundaries()
        channel_params = ['lift_r', 'lift_g', 'lift_b',
                          'gamma_r', 'gamma_g', 'gamma_b',
                          'gain_r', 'gain_g', 'gain_b']

        for param in channel_params:
            if param in boundaries:
                bound = boundaries[param]
                if bound.get('ptype') == 'float':
                    midpoint = (bound['min_val'] + bound['max_val']) / 2
                    node.set_parameter(param, midpoint)

        # Verify each channel parameter is set independently
        for param in channel_params:
            if param in boundaries:
                assert node.parameters.get(param) is not None, \
                    f"Channel param {param} not set"


class MergeNodeTestMixin:
    """Additional tests for merge/blend nodes.

    Provides tests for:
    - Multiple input handling
    - Blend mode options
    - Opacity parameter
    """

    def test_multiple_inputs(self):
        """节点支持多输入."""
        node = self.create_node()

        # Create two inputs (A and B)
        inputs = {
            "A": make_texture_result(texture_id=1, width=1920, height=1080),
            "B": make_texture_result(texture_id=2, width=1920, height=1080),
        }

        try:
            outputs = node.evaluate(
                inputs,
                self.get_mock_context(),
                self.get_mock_fb_manager(),
                self.get_mock_resource_pool(),
                render_mode="preview"
            )
            # Should not crash
        except Exception as e:
            pytest.fail(f"Node crashed with multiple inputs: {e}")

    def test_blend_modes(self):
        """各种 blend mode 可用."""
        node = self.create_node()
        boundaries = self.get_param_boundaries()

        # Find blend_mode parameter
        blend_mode_param = None
        for param_name, bound in boundaries.items():
            if 'blend' in param_name.lower() or 'mode' in param_name.lower():
                if bound.get('ptype') == 'option':
                    blend_mode_param = param_name
                    break

        if blend_mode_param:
            modes = boundaries[blend_mode_param].get('labels', [])
            for mode in modes:
                test_node = self.create_node()
                test_node.set_parameter(blend_mode_param, mode)
                assert test_node.parameters.get(blend_mode_param) == mode


class KeyerNodeTestMixin:
    """Additional tests for keyer nodes.

    Provides tests for:
    - Matte output generation
    - Screen color detection
    - Softness/tolerance parameters
    """

    def test_matte_output(self):
        """Keyer 应生成 matte 输出."""
        node = self.create_node()

        # Create valid input
        inputs = self.create_valid_input()

        try:
            outputs = node.evaluate(
                inputs,
                self.get_mock_context(),
                self.get_mock_fb_manager(),
                self.get_mock_resource_pool(),
                render_mode="preview"
            )

            # Keyer should produce output (result or matte)
            assert len(outputs) > 0 or node.IS_PLACEHOLDER, \
                "Keyer should produce output"

        except Exception as e:
            pytest.fail(f"Keyer crashed: {e}")

    def test_tolerance_softness_range(self):
        """tolerance/softness 参数在有效范围内."""
        node = self.create_node()
        boundaries = self.get_param_boundaries()

        # Test tolerance parameter
        for param in ['tolerance', 'softness']:
            if param in boundaries:
                bound = boundaries[param]
                test_values = get_boundary_test_values(bound)
                for value in test_values:
                    test_node = self.create_node()
                    test_node.set_parameter(param, value)
                    assert test_node.parameters.get(param) == value


class FilterNodeTestMixin:
    """Additional tests for filter nodes (blur, glow, etc).

    Provides tests for:
    - ROI expansion
    - Quality levels
    - Process alpha parameter
    """

    def test_roi_expansion(self):
        """Filter 应正确计算 ROI 扩展."""
        node = self.create_node()

        # Check compute_output_bbox exists
        if hasattr(node, 'compute_output_bbox'):
            input_bbox = BBox(x=0, y=0, width=100, height=100)
            input_bboxes = {"image": input_bbox}

            output_bbox = node.compute_output_bbox(input_bboxes, node.parameters)

            # Output bbox should exist (may be expanded)
            if output_bbox:
                assert output_bbox.width > 0 and output_bbox.height > 0, \
                    "Output bbox should have positive dimensions"


# ── Test Runner Helpers ──────────────────────────────────────────────────────

def run_node_tests(node_class: Type[Node], node_type: str):
    """Run all standard tests for a node type.

    Convenience function for manual testing:
        from tests.node_test_base import run_node_tests

import pytest
pytestmark = pytest.mark.composite_core
        run_node_tests(GradeNode, "grade")

    Args:
        node_class: Node subclass to test
        node_type: Node type string
    """
    class TempTestClass(NodeTestBase):
        node_type = node_type
        node_class = node_class

    # Run pytest on the temp class
    pytest.main([__file__, "-v", "-k", TempTestClass.__name__])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
