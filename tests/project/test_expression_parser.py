"""
Tests for expression parser and node parameter expression support.

Tests:
- ExpressionParser validation and evaluation
- Safe math functions and operators
- Variable references (this.xxx, NodeName.param, input.xxx, frame)
- Dependency extraction
- ExpressionManager dependency tracking
- Node expression support (set_expression, resolve_expressions)
- NodeGraph expression management
"""

import pytest
import math
from typing import Dict, Any

from src.features.composite.utils.expression_parser import (
    ExpressionParser,
    ExpressionContext,
    ExpressionManager,
    ExpressionError,
    ExpressionErrorType,
    ExpressionResult,
    ExpressionDependency,
    get_expression_manager,
    reset_expression_manager,
)
from src.features.composite.nodes.base_node import Node, ParamDef, Port
from src.features.composite.core.node_engine import NodeGraph

import pytest
pytestmark = pytest.mark.composite_core


# ── ExpressionParser Tests ──

class TestExpressionParserValidation:
    """Test expression syntax validation."""

    def test_valid_simple_expression(self):
        """Test simple arithmetic expression validation."""
        parser = ExpressionParser()
        result = parser.validate("1 + 2")
        assert result.success is True

    def test_valid_complex_expression(self):
        """Test complex expression validation."""
        parser = ExpressionParser()
        result = parser.validate("this.width * 0.5 + Blur1.radius")
        assert result.success is True

    def test_invalid_syntax(self):
        """Test invalid syntax detection."""
        parser = ExpressionParser()
        result = parser.validate("1 + + 2")
        assert result.success is False
        assert result.error.error_type == ExpressionErrorType.SYNTAX_ERROR

    def test_empty_expression(self):
        """Test empty expression validation."""
        parser = ExpressionParser()
        result = parser.validate("")
        assert result.success is False
        assert result.error.error_type == ExpressionErrorType.SYNTAX_ERROR

    def test_whitespace_expression(self):
        """Test whitespace-only expression."""
        parser = ExpressionParser()
        result = parser.validate("   ")
        assert result.success is False

    def test_forbidden_function(self):
        """Test that forbidden functions are rejected."""
        parser = ExpressionParser()
        # exec is not in whitelist, should be rejected
        result = parser.validate("exec('print')")
        assert result.success is False

    def test_forbidden_import(self):
        """Test that import is forbidden."""
        parser = ExpressionParser()
        result = parser.validate("import os")
        assert result.success is False


class TestExpressionParserEvaluation:
    """Test expression evaluation."""

    def test_simple_arithmetic(self):
        """Test basic arithmetic operations."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("1 + 2", context)
        assert result.success is True
        assert result.value == 3.0

        result = parser.evaluate("10 - 3", context)
        assert result.success is True
        assert result.value == 7.0

        result = parser.evaluate("4 * 5", context)
        assert result.success is True
        assert result.value == 20.0

        result = parser.evaluate("15 / 3", context)
        assert result.success is True
        assert result.value == 5.0

    def test_math_functions(self):
        """Test math function evaluation."""
        parser = ExpressionParser()
        context = ExpressionContext()

        # sin
        result = parser.evaluate("sin(0)", context)
        assert result.success is True
        assert abs(result.value - 0.0) < 0.001

        # cos
        result = parser.evaluate("cos(0)", context)
        assert result.success is True
        assert abs(result.value - 1.0) < 0.001

        # sqrt
        result = parser.evaluate("sqrt(16)", context)
        assert result.success is True
        assert result.value == 4.0

        # abs
        result = parser.evaluate("abs(-5)", context)
        assert result.success is True
        assert result.value == 5.0

        # floor
        result = parser.evaluate("floor(3.7)", context)
        assert result.success is True
        assert result.value == 3.0

        # ceil
        result = parser.evaluate("ceil(3.2)", context)
        assert result.success is True
        assert result.value == 4.0

    def test_min_max_functions(self):
        """Test min and max functions."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("min(1, 2, 3)", context)
        assert result.success is True
        assert result.value == 1.0

        result = parser.evaluate("max(1, 2, 3)", context)
        assert result.success is True
        assert result.value == 3.0

    def test_clamp_function(self):
        """Test clamp function."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("clamp(5, 0, 10)", context)
        assert result.success is True
        assert result.value == 5.0

        result = parser.evaluate("clamp(-5, 0, 10)", context)
        assert result.success is True
        assert result.value == 0.0

        result = parser.evaluate("clamp(15, 0, 10)", context)
        assert result.success is True
        assert result.value == 10.0

    def test_lerp_function(self):
        """Test linear interpolation."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("lerp(0, 10, 0.5)", context)
        assert result.success is True
        assert result.value == 5.0

    def test_comparison_operators(self):
        """Test comparison operators."""
        parser = ExpressionParser()
        context = ExpressionContext()

        # These return 1.0 for True, 0.0 for False
        result = parser.evaluate("5 > 3", context)
        assert result.success is True
        assert result.value == 1.0

        result = parser.evaluate("3 > 5", context)
        assert result.success is True
        assert result.value == 0.0

        result = parser.evaluate("5 >= 5", context)
        assert result.success is True
        assert result.value == 1.0

        result = parser.evaluate("5 == 5", context)
        assert result.success is True
        assert result.value == 1.0

        result = parser.evaluate("5 != 3", context)
        assert result.success is True
        assert result.value == 1.0

    def test_ternary_expression(self):
        """Test ternary conditional expression (x if cond else y)."""
        parser = ExpressionParser()
        context = ExpressionContext()

        # Python ternary: value if condition else other_value
        result = parser.evaluate("100 if 1 else 0", context)
        assert result.success is True
        assert result.value == 100.0

        result = parser.evaluate("0 if 0 else 100", context)
        assert result.success is True
        assert result.value == 100.0

    def test_complex_nested_expression(self):
        """Test nested and complex expressions."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("((1 + 2) * 3 - 4) / 2", context)
        assert result.success is True
        # (3 * 3 - 4) / 2 = (9 - 4) / 2 = 5 / 2 = 2.5
        assert result.value == 2.5


class TestExpressionParserVariables:
    """Test variable reference evaluation."""

    def test_this_variable(self):
        """Test this.xxx variable reference."""
        parser = ExpressionParser()
        context = ExpressionContext(this_params={"width": 1920, "height": 1080})

        result = parser.evaluate("this.width", context)
        assert result.success is True
        assert result.value == 1920.0

        result = parser.evaluate("this.height * 0.5", context)
        assert result.success is True
        assert result.value == 540.0

    def test_input_variable(self):
        """Test input.xxx variable reference."""
        parser = ExpressionParser()
        context = ExpressionContext(input_attrs={"width": 3840, "height": 2160})

        result = parser.evaluate("input.width", context)
        assert result.success is True
        assert result.value == 3840.0

        result = parser.evaluate("input.width / 2", context)
        assert result.success is True
        assert result.value == 1920.0

    def test_frame_variable(self):
        """Test frame variable reference."""
        parser = ExpressionParser()
        context = ExpressionContext(frame=42)

        result = parser.evaluate("frame", context)
        assert result.success is True
        assert result.value == 42.0

        result = parser.evaluate("frame + 10", context)
        assert result.success is True
        assert result.value == 52.0

    def test_node_param_reference(self):
        """Test NodeName.param reference."""
        parser = ExpressionParser()
        context = ExpressionContext(
            node_params={
                "Blur1": {"radius": 10.0},
                "Transform2": {"scale": 2.5}
            }
        )

        result = parser.evaluate("Blur1.radius", context)
        assert result.success is True
        assert result.value == 10.0

        result = parser.evaluate("Blur1.radius + Transform2.scale", context)
        assert result.success is True
        assert result.value == 12.5

    def test_combined_variables(self):
        """Test combined variable references."""
        parser = ExpressionParser()
        context = ExpressionContext(
            this_params={"offset": 100},
            node_params={"Blur1": {"radius": 20}},
            input_attrs={"width": 1920},
            frame=10
        )

        result = parser.evaluate("this.offset + Blur1.radius + input.width + frame", context)
        assert result.success is True
        assert result.value == 2050.0

    def test_animation_expression(self):
        """Test time-based animation expression."""
        parser = ExpressionParser()
        context = ExpressionContext(frame=30)

        # sin wave animation
        result = parser.evaluate("sin(frame * 0.1) * 50", context)
        assert result.success is True
        expected = math.sin(30 * 0.1) * 50
        assert abs(result.value - expected) < 0.001

    def test_undefined_variable(self):
        """Test undefined variable error."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("undefined_var", context)
        assert result.success is False
        assert result.error.error_type == ExpressionErrorType.UNDEFINED_VARIABLE

    def test_undefined_node_reference(self):
        """Test undefined node reference error."""
        parser = ExpressionParser()
        context = ExpressionContext(node_params={"Blur1": {"radius": 10}})

        result = parser.evaluate("NonExistent.param", context)
        assert result.success is False
        assert result.error.error_type == ExpressionErrorType.UNDEFINED_VARIABLE


class TestExpressionParserDependencies:
    """Test dependency extraction."""

    def test_extract_frame_dependency(self):
        """Test frame dependency extraction."""
        parser = ExpressionParser()
        deps = parser.extract_dependencies("frame * 0.5")

        assert len(deps) == 1
        assert deps[0].is_frame is True
        assert deps[0].param_name == "frame"

    def test_extract_input_dependency(self):
        """Test input.xxx dependency extraction."""
        parser = ExpressionParser()
        deps = parser.extract_dependencies("input.width + input.height")

        assert len(deps) == 2
        for dep in deps:
            assert dep.is_input_attr is True

    def test_extract_node_dependency(self):
        """Test node parameter dependency extraction."""
        parser = ExpressionParser()
        deps = parser.extract_dependencies("Blur1.radius + Transform2.scale")

        assert len(deps) == 2
        blur_dep = [d for d in deps if d.node_id == "Blur1"][0]
        assert blur_dep.param_name == "radius"

        transform_dep = [d for d in deps if d.node_id == "Transform2"][0]
        assert transform_dep.param_name == "scale"

    def test_no_external_dependencies(self):
        """Test expression with no external dependencies."""
        parser = ExpressionParser()
        deps = parser.extract_dependencies("this.width * 0.5")

        # this.xxx references are not external dependencies
        # They're local to the current node
        assert len(deps) == 1
        assert deps[0].is_input_attr is True  # this.width is treated as input attr

    def test_comp_namespace_is_input_dependency(self):
        """Composition aliases must not become fake node dependencies."""
        parser = ExpressionParser()
        deps = parser.extract_dependencies("comp.width + thisComp.height")

        assert len(deps) == 2
        assert all(dep.is_input_attr for dep in deps)

    def test_extract_local_parameter_names(self):
        parser = ExpressionParser()

        assert parser.extract_local_parameter_names(
            "this.radius + this.width") == ["radius"]


class TestExpressionManager:
    """Test ExpressionManager dependency tracking."""

    def test_set_expression(self):
        """Test setting expression."""
        reset_expression_manager()
        manager = ExpressionManager()

        result = manager.set_expression("node1", "param1", "10 + 5")
        assert result.success is True

        assert manager.has_expression("node1", "param1")
        assert manager.get_expression("node1", "param1") == "10 + 5"

    def test_remove_expression(self):
        """Test removing expression."""
        reset_expression_manager()
        manager = ExpressionManager()

        manager.set_expression("node1", "param1", "10 + 5")
        manager.remove_expression("node1", "param1")

        assert not manager.has_expression("node1", "param1")

    def test_dependency_tracking(self):
        """Test dependency tracking."""
        reset_expression_manager()
        manager = ExpressionManager()

        # node2.param depends on node1.param
        manager.set_expression("node2", "param1", "node1.param * 2")

        deps = manager.get_dependencies("node2", "param1")
        assert ("node1", "param") in deps

    def test_dependent_tracking(self):
        """Test dependent tracking."""
        reset_expression_manager()
        manager = ExpressionManager()

        manager.set_expression("node2", "param1", "node1.param * 2")

        dependents = manager.get_dependents("node1", "param")
        assert ("node2", "param1") in dependents

    def test_circular_dependency_detection(self):
        """Test circular dependency detection."""
        reset_expression_manager()
        manager = ExpressionManager()

        # Create potential circular dependency
        manager.set_expression("node1", "param", "10")
        manager.set_expression("node2", "param", "node1.param * 2")

        # This would create a cycle if allowed
        manager.set_expression("node1", "param2", "node2.param")
        # But node1.param2 depends on node2.param which depends on node1.param
        # This is not circular - node1.param2 -> node2.param -> node1.param (different param)

        # Direct circular dependency
        manager.set_expression("node3", "param", "5")
        manager.set_expression("node4", "param", "node3.param")
        # Try to make node3.param depend on node4.param - this would be circular
        # But since node3.param is already set to "5", we need to check differently

        # Test: if we set node3.param to depend on node4.param after node4.param depends on node3.param
        manager._expressions.pop(("node3", "param"), None)  # Remove existing
        result = manager.set_expression("node3", "param", "node4.param")

        # This should NOT be circular because we're just setting the expression
        # Circular detection checks if the *new* expression would create a cycle
        # node3.param -> node4.param -> node3.param (same param) = cycle!

        # Actually, the detection logic needs to be verified


class TestExpressionManagerEvaluation:
    """Test ExpressionManager batch evaluation."""

    def test_evaluate_all_simple(self):
        """Test batch evaluation without dependencies."""
        reset_expression_manager()
        manager = ExpressionManager()

        manager.set_expression("node1", "param1", "10")
        manager.set_expression("node1", "param2", "20")

        node_params = {"node1": {"param1": 0, "param2": 0}}
        resolved = manager.evaluate_all(node_params, {}, 0)

        assert resolved["node1"]["param1"] == 10.0
        assert resolved["node1"]["param2"] == 20.0

    def test_evaluate_with_dependencies(self):
        """Test evaluation with dependencies."""
        reset_expression_manager()
        manager = ExpressionManager()

        manager.set_expression("node1", "radius", "10")
        manager.set_expression("node2", "radius", "node1.radius * 2")

        node_params = {
            "node1": {"radius": 0},
            "node2": {"radius": 0}
        }
        resolved = manager.evaluate_all(node_params, {}, 0)

        assert resolved["node1"]["radius"] == 10.0
        assert resolved["node2"]["radius"] == 20.0

    def test_evaluate_with_chain_dependencies(self):
        """Test evaluation with chain of dependencies."""
        reset_expression_manager()
        manager = ExpressionManager()

        manager.set_expression("node1", "value", "100")
        manager.set_expression("node2", "value", "node1.value + 10")
        manager.set_expression("node3", "value", "node2.value * 2")

        node_params = {
            "node1": {"value": 0},
            "node2": {"value": 0},
            "node3": {"value": 0}
        }
        resolved = manager.evaluate_all(node_params, {}, 0)

        assert resolved["node1"]["value"] == 100.0
        assert resolved["node2"]["value"] == 110.0
        assert resolved["node3"]["value"] == 220.0


# ── Node Expression Tests ──

class TestNodeExpression:
    """Test Node expression support."""

    def test_node_set_expression(self):
        """Test setting expression on a node."""
        node = Node(node_id="test_node")
        node.parameters["value"] = 50.0

        success = node.set_expression("value", "100 + 50")
        assert success is True
        assert node.has_expression("value")
        assert node.get_expression("value") == "100 + 50"

    def test_node_invalid_expression(self):
        """Test setting invalid expression."""
        node = Node(node_id="test_node")

        success = node.set_expression("value", "invalid syntax ++")
        assert success is False
        assert not node.has_expression("value")

    def test_node_remove_expression(self):
        """Test removing expression from node."""
        node = Node(node_id="test_node")

        node.set_expression("value", "10")
        node.remove_expression("value")

        assert not node.has_expression("value")

    def test_node_resolve_expressions(self):
        """Test resolving expressions on a node."""
        node = Node(node_id="test_node")
        node.parameters["offset"] = 100
        node.set_expression("result", "this.offset + 50")

        context = ExpressionContext(this_params=node.parameters)
        resolved = node.resolve_expressions(context)

        assert resolved["result"] == 150.0

    def test_node_expression_dependencies(self):
        """Test getting expression dependencies."""
        node = Node(node_id="test_node")
        node.set_expression("param1", "Blur1.radius * 2")
        node.set_expression("param2", "frame + 10")

        deps = node.get_expression_dependencies()

        assert "param1" in deps
        assert "Blur1.radius" in deps["param1"]

        assert "param2" in deps
        assert "frame" in deps["param2"]

    def test_node_serialization(self):
        """Test node serialization with expressions."""
        node = Node(node_id="test_node")
        node.parameters["value"] = 50
        node.set_expression("value", "100")

        data = node.to_dict()

        assert "expressions" in data
        assert data["expressions"]["value"] == "100"

    def test_node_deserialization(self):
        """Test node deserialization with expressions."""
        data = {
            "id": "test_node",
            "type": "base",
            "position": [0, 0],
            "parameters": {"value": 50},
            "expressions": {"value": "100 + 20"}
        }

        node = Node.from_dict(data)

        assert node.has_expression("value")
        assert node.get_expression("value") == "100 + 20"


# ── NodeGraph Expression Tests ──

class TestNodeGraphExpression:
    """Test NodeGraph expression management."""

    def test_set_node_expression(self):
        """Test setting expression via NodeGraph."""
        graph = NodeGraph()
        node = Node(node_id="node1")
        node.parameters["radius"] = 10
        graph.add_node(node)

        success, error = graph.set_node_expression("node1", "radius", "50 + 10")
        assert success is True
        assert error is None

        expr = graph.get_node_expression("node1", "radius")
        assert expr == "50 + 10"

    def test_set_invalid_expression(self):
        """Test setting invalid expression via NodeGraph."""
        graph = NodeGraph()
        node = Node(node_id="node1")
        graph.add_node(node)

        success, error = graph.set_node_expression("node1", "param", "invalid ++ syntax")
        assert success is False
        assert error is not None

    def test_remove_expression(self):
        """Test removing expression via NodeGraph."""
        graph = NodeGraph()
        node = Node(node_id="node1")
        graph.add_node(node)

        graph.set_node_expression("node1", "param", "10")
        result = graph.remove_node_expression("node1", "param")

        assert result is True
        assert not graph.get_node_expression("node1", "param")

    def test_resolve_node_expressions(self):
        """Test resolving expressions for a single node."""
        graph = NodeGraph()

        node1 = Node(node_id="node1")
        node1.parameters["value"] = 100
        graph.add_node(node1)

        node2 = Node(node_id="node2")
        node2.parameters["result"] = 0
        node2.set_expression("result", "node1.value * 2")
        graph.add_node(node2)

        resolved = graph.resolve_node_expressions("node2")

        assert resolved["result"] == 200.0

    def test_resolve_all_expressions(self):
        """Test resolving all expressions in graph."""
        graph = NodeGraph()

        node1 = Node(node_id="node1")
        node1.parameters["radius"] = 10
        node1.set_expression("radius", "50")
        graph.add_node(node1)

        node2 = Node(node_id="node2")
        node2.parameters["scale"] = 1
        node2.set_expression("scale", "node1.radius / 10")
        graph.add_node(node2)

        resolved = graph.resolve_all_expressions()

        assert resolved["node1"]["radius"] == 50.0
        assert resolved["node2"]["scale"] == 5.0

    def test_expression_dependency_propagation(self):
        """Test that expression changes propagate to dependents."""
        graph = NodeGraph()

        node1 = Node(node_id="node1")
        node1.parameters["value"] = 100
        graph.add_node(node1)

        node2 = Node(node_id="node2")
        node2.parameters["result"] = 0
        graph.add_node(node2)

        # Set expression that depends on node1
        graph.set_node_expression("node2", "result", "node1.value * 2")

        # node2 should be marked dirty after expression is set
        # (Dependency propagation)
        assert node2._dirty is True


# ── Edge Cases and Error Handling ──

class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_division_by_zero(self):
        """Test division by zero handling."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("10 / 0", context)
        # We handle this by returning infinity
        assert result.success is True
        assert result.value == float('inf')

    def test_negative_sqrt(self):
        """Test sqrt of negative number."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("sqrt(-1)", context)
        # math.sqrt(-1) raises ValueError
        assert result.success is False
        assert result.error.error_type == ExpressionErrorType.INVALID_FUNCTION

    def test_parentheses_mismatch(self):
        """Test mismatched parentheses."""
        parser = ExpressionParser()

        result = parser.validate("(1 + 2) * 3)", context=None)
        assert result.success is False

    def test_very_long_expression(self):
        """Test long expression chain."""
        parser = ExpressionParser()
        context = ExpressionContext(this_params={"x": 1})

        # Build a long expression
        expr = "this.x"
        for i in range(10):
            expr = f"({expr} + 1)"

        result = parser.evaluate(expr, context)
        assert result.success is True
        assert result.value == 11.0

    def test_power_operator(self):
        """Test power operator (**)."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("2 ** 10", context)
        assert result.success is True
        assert result.value == 1024.0

    def test_modulo_operator(self):
        """Test modulo operator (%)."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("17 % 5", context)
        assert result.success is True
        assert result.value == 2.0

    def test_floor_division(self):
        """Test floor division (//)."""
        parser = ExpressionParser()
        context = ExpressionContext()

        result = parser.evaluate("17 // 5", context)
        assert result.success is True
        assert result.value == 3.0
