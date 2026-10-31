"""Tests for LayerStackValidator (plan §9.4 / §13.1 / §13.4)."""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_model import Layer, LayerStack
from src.features.composite.core.layer_stack_validator import (
    LayerStackValidator, ValidationReport, SEVERITY_ERROR, SEVERITY_WARNING,
)
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode

pytestmark = pytest.mark.composite_core


def _good_graph_and_stack():
    graph = NodeGraph()
    out = OutputNode(node_id="out")
    graph.add_node(out)
    src = ConstantNode(node_id="src0")
    graph.add_node(src)
    t = TransformNode(node_id="t0")
    graph.add_node(t)
    stack = LayerStack(stack_id="main", output_node_id="out")
    layer = Layer(layer_id="L0", name="L0", root_node_id="t0",
                  source_node_id="src0")
    layers = {"L0": layer}
    stack.layer_ids.append("L0")
    return graph, stack, layers


class TestValidatorHappyPath(unittest.TestCase):

    def test_valid_stack_passes(self):
        graph, stack, layers = _good_graph_and_stack()
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertTrue(report.ok, msg=[i.message for i in report.issues])
        self.assertEqual(report.errors, [])


class TestValidatorReferencedNodes(unittest.TestCase):

    def test_missing_root_node_is_error(self):
        graph, stack, layers = _good_graph_and_stack()
        layers["L0"].root_node_id = "ghost"
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertFalse(report.ok)
        self.assertTrue(report.has("missing_root_node"))

    def test_missing_source_node_is_warning(self):
        graph, stack, layers = _good_graph_and_stack()
        layers["L0"].source_node_id = "ghost_src"
        report = LayerStackValidator(graph).validate(stack, layers)
        # Missing source is a warning, not an error — layer still renders.
        self.assertTrue(report.ok)
        self.assertTrue(report.has("missing_source_node"))

    def test_missing_layer_in_map_is_error(self):
        graph, stack, layers = _good_graph_and_stack()
        stack.layer_ids.append("LX")  # not in layers map
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertFalse(report.ok)
        self.assertTrue(report.has("missing_layer"))


class TestValidatorDuplicates(unittest.TestCase):

    def test_duplicate_layer_id(self):
        graph, stack, layers = _good_graph_and_stack()
        stack.layer_ids.append("L0")  # duplicate
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertFalse(report.ok)
        self.assertTrue(report.has("duplicate_layer_id"))


class TestValidatorParenting(unittest.TestCase):

    def test_parent_cycle_is_error(self):
        graph = NodeGraph()
        graph.add_node(OutputNode(node_id="out"))
        graph.add_node(TransformNode(node_id="t0"))
        graph.add_node(TransformNode(node_id="t1"))
        stack = LayerStack(stack_id="main", output_node_id="out",
                           layer_ids=["L0", "L1"])
        layers = {
            "L0": Layer(layer_id="L0", name="L0", root_node_id="t0",
                        parent_layer_id="L1"),
            "L1": Layer(layer_id="L1", name="L1", root_node_id="t1",
                        parent_layer_id="L0"),
        }
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertFalse(report.ok)
        self.assertTrue(report.has("parent_cycle"))

    def test_parent_on_unsupported_node_is_warning(self):
        graph = NodeGraph()
        graph.add_node(OutputNode(node_id="out"))
        # BlurNode is an effect — does not support_parent_transform.
        graph.add_node(BlurNode(node_id="blur0"))
        stack = LayerStack(stack_id="main", output_node_id="out",
                           layer_ids=["L0", "L1"])
        layers = {
            "L0": Layer(layer_id="L0", name="L0", root_node_id="blur0",
                        parent_layer_id="L1"),
            "L1": Layer(layer_id="L1", name="L1", root_node_id="blur0"),
        }
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertTrue(report.ok)
        self.assertTrue(report.has("parent_unsupported_node"))

    def test_dangling_parent_is_warning(self):
        graph, stack, layers = _good_graph_and_stack()
        layers["L0"].parent_layer_id = "nonexistent"
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertTrue(report.ok)
        self.assertTrue(report.has("dangling_parent"))

    def test_three_node_cycle_detected(self):
        # A deeper cycle that a broken Floyd walker would miss.
        graph = NodeGraph()
        graph.add_node(OutputNode(node_id="out"))
        graph.add_node(TransformNode(node_id="t0"))
        graph.add_node(TransformNode(node_id="t1"))
        graph.add_node(TransformNode(node_id="t2"))
        stack = LayerStack(stack_id="main", output_node_id="out",
                           layer_ids=["L0", "L1", "L2"])
        layers = {
            "L0": Layer(layer_id="L0", name="L0", root_node_id="t0",
                        parent_layer_id="L1"),
            "L1": Layer(layer_id="L1", name="L1", root_node_id="t1",
                        parent_layer_id="L2"),
            "L2": Layer(layer_id="L2", name="L2", root_node_id="t2",
                        parent_layer_id="L0"),
        }
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertFalse(report.ok)
        self.assertTrue(report.has("parent_cycle"))


class TestValidatorSharedRoots(unittest.TestCase):
    """Two layers sharing a root is flagged (review BUG-4)."""

    def test_shared_root_is_warning(self):
        graph = NodeGraph()
        graph.add_node(OutputNode(node_id="out"))
        graph.add_node(TransformNode(node_id="shared_t"))
        stack = LayerStack(stack_id="main", output_node_id="out",
                           layer_ids=["L0", "L1"])
        layers = {
            "L0": Layer(layer_id="L0", name="L0", root_node_id="shared_t"),
            "L1": Layer(layer_id="L1", name="L1", root_node_id="shared_t"),
        }
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertTrue(report.ok)  # warning, not error
        self.assertTrue(report.has("shared_root_node"))


class TestValidatorOutput(unittest.TestCase):

    def test_missing_output_node(self):
        graph, stack, layers = _good_graph_and_stack()
        stack.output_node_id = "ghost_out"
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertFalse(report.ok)
        self.assertTrue(report.has("missing_output_node"))


class TestValidatorMergePorts(unittest.TestCase):

    def test_invalid_merge_node_flagged(self):
        from src.features.composite.nodes.blend_node import MergeNode
        graph, stack, layers = _good_graph_and_stack()
        # Register a non-merge node id as an auto-merge.
        graph.add_node(ConstantNode(node_id="not_a_merge"))
        stack.generated_merge_node_ids.append("not_a_merge")
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertFalse(report.ok)
        self.assertTrue(report.has("invalid_merge_ports"))

    def test_missing_merge_node_flagged(self):
        graph, stack, layers = _good_graph_and_stack()
        stack.generated_merge_node_ids.append("ghost_merge")
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertFalse(report.ok)
        self.assertTrue(report.has("missing_merge_node"))


class TestValidatorLayerFields(unittest.TestCase):

    def test_bad_blend_mode_is_warning(self):
        graph, stack, layers = _good_graph_and_stack()
        layers["L0"].blend_mode = "nuclear_fusion"
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertTrue(report.ok)
        self.assertTrue(report.has("unknown_blend_mode"))

    def test_opacity_out_of_range(self):
        graph, stack, layers = _good_graph_and_stack()
        layers["L0"].opacity = 2.5
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertTrue(report.ok)
        self.assertTrue(report.has("opacity_out_of_range"))

    def test_inverted_time_range(self):
        graph, stack, layers = _good_graph_and_stack()
        layers["L0"].in_frame = 100
        layers["L0"].out_frame = 10
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertTrue(report.ok)
        self.assertTrue(report.has("time_range_inverted"))


if __name__ == "__main__":
    unittest.main()
