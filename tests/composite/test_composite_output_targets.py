"""P2-05 named terminal output contracts."""

from types import SimpleNamespace

import pytest

from src.features.composite.core.output_targets import (
    DEFAULT_OUTPUT_NAME,
    normalize_output_name,
    output_name_for_node,
    output_dependency_records,
    selected_output_dependency_ids,
    output_target_records,
)
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.render.render_scheduler import RenderRequest


pytestmark = pytest.mark.composite_core


def test_legacy_and_blank_output_names_resolve_to_display():
    assert DEFAULT_OUTPUT_NAME == "display"
    assert normalize_output_name("") == "display"
    assert normalize_output_name("  beauty  ") == "beauty"
    assert output_name_for_node(SimpleNamespace(parameters={})) == "display"


def test_output_node_exposes_persisted_named_target_parameter():
    node = OutputNode(node_id="out")
    assert node.parameters["output_name"] == "display"
    node.parameters["output_name"] = "foreground"
    assert output_name_for_node(node) == "foreground"


def test_output_target_records_keep_duplicate_names_addressable_by_node_id():
    first = OutputNode(node_id="out_a")
    second = OutputNode(node_id="out_b")
    second.parameters["output_name"] = "matte"
    records = output_target_records((first, second))
    assert records == (
        {"name": "display", "node_id": "out_a", "node_type": "output"},
        {"name": "matte", "node_id": "out_b", "node_type": "output"},
    )


def test_render_request_normalizes_named_target():
    request = RenderRequest(
        snapshot=None, request_id=1, timestamp=0.0, output_name="  beauty  "
    )
    assert request.output_name == "beauty"
    assert request.output_node_id is None


def test_serialized_graph_output_dependencies_are_json_safe_and_transitive():
    nodes = [
        {"id": "source_a", "type": "constant", "parameters": {}},
        {"id": "grade_a", "type": "grade", "parameters": {}},
        {"id": "source_b", "type": "constant", "parameters": {}},
        {"id": "out_a", "type": "output",
         "parameters": {"output_name": "beauty"}},
        {"id": "out_b", "type": "output",
         "parameters": {"output_name": "matte"}},
    ]
    connections = [
        {"src_node_id": "source_a", "src_port_name": "result",
         "dst_node_id": "grade_a", "dst_port_name": "image"},
        {"src_node_id": "grade_a", "src_port_name": "result",
         "dst_node_id": "out_a", "dst_port_name": "image"},
        {"src_node_id": "source_b", "src_port_name": "result",
         "dst_node_id": "out_b", "dst_port_name": "image"},
    ]

    records = output_dependency_records(nodes, connections)

    assert records[0]["dependency_node_ids"] == ["source_a", "grade_a"]
    assert records[0]["direct_input_node_ids"] == ["grade_a"]
    assert records[0]["dependency_count"] == 2
    assert records[1]["dependency_node_ids"] == ["source_b"]
    assert selected_output_dependency_ids(
        nodes, connections, "beauty") == frozenset({"source_a", "grade_a", "out_a"})
    assert selected_output_dependency_ids(
        nodes, connections, "matte", "out_b") == frozenset({"source_b", "out_b"})
    assert selected_output_dependency_ids(nodes, connections, "missing") is None


def test_output_target_records_accept_serialized_nodes():
    records = output_target_records((
        {"id": "out", "type": "output", "parameters": {}},
    ))
    assert records == (
        {"name": "display", "node_id": "out", "node_type": "output"},
    )


def test_composite_page_exposes_dependency_graph_to_host_integrations():
    from src.features.composite.ui.composite_page import CompositePage

    page = CompositePage.__new__(CompositePage)
    page._node_graph = SimpleNamespace(
        nodes={
            "source": {"id": "source", "type": "constant", "parameters": {}},
            "out": {"id": "out", "type": "output", "parameters": {}},
        },
        connections=({
            "src_node_id": "source", "src_port_name": "result",
            "dst_node_id": "out", "dst_port_name": "image",
        },),
    )

    result = CompositePage.output_dependency_graph(page)

    assert result[0]["name"] == "display"
    assert result[0]["dependency_node_ids"] == ["source"]
