"""
Tests for group_node.py — Group node creation, serialization, and port mapping.

Covers:
- Group creation and member management
- Port mapping (input/output)
- Serialization round-trip (to_dict / from_dict)
- Group-specific attributes

Note: Tests focus on pure logic; UI expansion/collapse is not tested.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.group_node import GroupNode
from src.features.composite.core.node_engine import NODE_REGISTRY, NodeGraph

import pytest
pytestmark = pytest.mark.composite_core


class TestGroupNodeRegistration:
    """Test GroupNode is registered in NODE_REGISTRY."""

    def test_registered(self):
        assert "group" in NODE_REGISTRY

    def test_registry_entry_is_group_class(self):
        from src.features.composite.nodes.group_node import GroupNode
        assert NODE_REGISTRY["group"] is GroupNode

    def test_can_instantiate(self):
        node = GroupNode()
        assert node.node_type == "group"
        assert node.node_title == "Group"

    def test_has_param_defs(self):
        node = GroupNode()
        assert hasattr(node, 'PARAM_DEFS')
        assert len(node.PARAM_DEFS) >= 3


class TestGroupCreation:
    """Group node creation and initialization."""

    def test_create_group_with_defaults(self):
        """创建 group 使用默认值。"""
        group = GroupNode()

        assert group.node_type == "group"
        assert group.node_title == "Group"
        assert group.header_color == "#4A90D9"
        assert group.member_node_ids == []
        assert group.input_mappings == {}
        assert group.output_mappings == {}
        assert group.is_expanded == False
        assert group.group_color == "#4A90D9"

    def test_create_group_with_id(self):
        """创建 group 使用指定 ID。"""
        group = GroupNode(node_id="group-123")

        assert group.id == "group-123"

    def test_group_has_default_ports(self):
        """Group 应有默认输入输出端口。"""
        group = GroupNode()

        assert len(group.inputs) == 1
        assert group.inputs[0].name == "input_1"
        assert len(group.outputs) == 1
        assert group.outputs[0].name == "output_1"

    def test_group_metadata(self):
        """Group 应有正确的 metadata。"""
        assert "group" in GroupNode.SEARCH_ALIASES
        assert "组" in GroupNode.SEARCH_ALIASES
        assert GroupNode.DESCRIPTION is not None
        assert "utility" in GroupNode.TAGS


class TestGroupMemberManagement:
    """Add/remove members."""

    def test_add_member(self):
        """添加成员到 group。"""
        group = GroupNode()
        group.add_member("node-1")

        assert "node-1" in group.member_node_ids
        assert len(group.member_node_ids) == 1

    def test_add_multiple_members(self):
        """添加多个成员。"""
        group = GroupNode()
        group.add_member("node-1")
        group.add_member("node-2")
        group.add_member("node-3")

        assert len(group.member_node_ids) == 3
        assert group.member_node_ids == ["node-1", "node-2", "node-3"]

    def test_add_member_no_duplicate(self):
        """重复添加不应创建重复项。"""
        group = GroupNode()
        group.add_member("node-1")
        group.add_member("node-1")

        assert len(group.member_node_ids) == 1

    def test_remove_member(self):
        """从 group 移除成员。"""
        group = GroupNode()
        group.add_member("node-1")
        group.add_member("node-2")

        group.remove_member("node-1")

        assert "node-1" not in group.member_node_ids
        assert "node-2" in group.member_node_ids

    def test_remove_member_clears_mappings(self):
        """移除成员时应清理相关端口映射。"""
        group = GroupNode()
        group.add_member("node-1")
        group.add_input_port("input_a", "node-1", "image")
        group.add_output_port("output_a", "node-1", "result")

        group.remove_member("node-1")

        assert "input_a" not in group.input_mappings
        assert "output_a" not in group.output_mappings

    def test_get_member_ids(self):
        """获取成员 ID 列表。"""
        group = GroupNode()
        group.add_member("node-1")
        group.add_member("node-2")

        members = group.get_member_ids()
        assert members == ["node-1", "node-2"]

    def test_contains_node(self):
        """检查节点是否在 group 中。"""
        group = GroupNode()
        group.add_member("node-1")

        assert group.contains_node("node-1") == True
        assert group.contains_node("node-2") == False


class TestGroupPortMapping:
    """Port mapping: add/remove input/output ports."""

    def test_add_input_port(self):
        """添加输入端口映射。"""
        group = GroupNode()
        group.add_member("blur-1")
        group.add_input_port("blur_input", "blur-1", "image")

        assert "blur_input" in group.input_mappings
        assert group.input_mappings["blur_input"] == ("blur-1", "image")
        assert len(group.inputs) == 2  # input_1 + blur_input

    def test_add_output_port(self):
        """添加输出端口映射。"""
        group = GroupNode()
        group.add_member("color-1")
        group.add_output_port("color_output", "color-1", "result")

        assert "color_output" in group.output_mappings
        assert group.output_mappings["color_output"] == ("color-1", "result")
        assert len(group.outputs) == 2  # output_1 + color_output

    def test_add_multiple_input_ports(self):
        """添加多个输入端口。"""
        group = GroupNode()
        group.add_member("merge-1")
        group.add_input_port("A", "merge-1", "A")
        group.add_input_port("B", "merge-1", "B")

        assert len(group.input_mappings) == 2
        assert group.input_mappings["A"] == ("merge-1", "A")
        assert group.input_mappings["B"] == ("merge-1", "B")

    def test_add_same_port_name_twice(self):
        """重复添加同名端口不应创建重复端口。"""
        group = GroupNode()
        group.add_member("node-1")
        group.add_input_port("input_a", "node-1", "image")
        group.add_input_port("input_a", "node-1", "mask")  # Update mapping

        # Port count should stay the same
        assert len(group.inputs) == 2  # input_1 + input_a
        # Mapping should be updated
        assert group.input_mappings["input_a"] == ("node-1", "mask")

    def test_remove_input_port(self):
        """移除输入端口。"""
        group = GroupNode()
        group.add_member("node-1")
        group.add_input_port("input_a", "node-1", "image")

        group.remove_input_port("input_a")

        assert "input_a" not in group.input_mappings
        assert len(group.inputs) == 1  # Only input_1 left

    def test_remove_output_port(self):
        """移除输出端口。"""
        group = GroupNode()
        group.add_member("node-1")
        group.add_output_port("output_a", "node-1", "result")

        group.remove_output_port("output_a")

        assert "output_a" not in group.output_mappings
        assert len(group.outputs) == 1  # Only output_1 left

    def test_port_mapping_updates_dirty_flag(self):
        """端口映射操作应标记 dirty。"""
        group = GroupNode()
        group._dirty = False

        group.add_input_port("new_input", "node-1", "image")
        assert group._dirty == True

        group._dirty = False
        group.remove_input_port("new_input")
        assert group._dirty == True


class TestGroupSerialization:
    """Serialization: to_dict / from_dict."""

    def test_serialize_empty_group(self):
        """序列化空 group。"""
        group = GroupNode(node_id="group-1")
        data = group.to_dict()

        assert data["id"] == "group-1"
        assert data["type"] == "group"
        assert data["member_node_ids"] == []
        assert data["input_mappings"] == {}
        assert data["output_mappings"] == {}
        assert data["is_expanded"] == False
        assert data["group_color"] == "#4A90D9"

    def test_serialize_group_with_members(self):
        """序列化有成员的 group。"""
        group = GroupNode(node_id="group-1")
        group.add_member("node-1")
        group.add_member("node-2")

        data = group.to_dict()

        assert data["member_node_ids"] == ["node-1", "node-2"]

    def test_serialize_group_with_port_mappings(self):
        """序列化有端口映射的 group。"""
        group = GroupNode(node_id="group-1")
        group.add_member("merge-1")
        group.add_input_port("A", "merge-1", "A")
        group.add_output_port("output", "merge-1", "result")

        data = group.to_dict()

        # Mappings are serialized as lists (tuple -> list)
        assert data["input_mappings"]["A"] == ["merge-1", "A"]
        assert data["output_mappings"]["output"] == ["merge-1", "result"]

    def test_serialize_group_with_custom_color(self):
        """序列化自定义颜色的 group。"""
        group = GroupNode(node_id="group-1")
        group.group_color = "#FF5500"
        group.is_expanded = True

        data = group.to_dict()

        assert data["group_color"] == "#FF5500"
        assert data["is_expanded"] == True

    def test_deserialize_empty_group(self):
        """反序列化空 group。"""
        data = {
            "id": "group-1",
            "type": "group",
            "position": [100, 200],
            "parameters": {},
            "member_node_ids": [],
            "input_mappings": {},
            "output_mappings": {},
            "is_expanded": False,
            "group_color": "#4A90D9"
        }

        group = GroupNode.from_dict(data)

        assert group.id == "group-1"
        assert group.position == (100, 200)
        assert group.member_node_ids == []
        assert group.input_mappings == {}
        assert group.output_mappings == {}

    def test_deserialize_group_with_members(self):
        """反序列化有成员的 group。"""
        data = {
            "id": "group-1",
            "type": "group",
            "position": [0, 0],
            "parameters": {},
            "member_node_ids": ["blur-1", "grade-1"],
            "input_mappings": {},
            "output_mappings": {},
            "is_expanded": False,
            "group_color": "#4A90D9"
        }

        group = GroupNode.from_dict(data)

        assert group.member_node_ids == ["blur-1", "grade-1"]

    def test_deserialize_group_with_port_mappings(self):
        """反序列化有端口映射的 group。"""
        data = {
            "id": "group-1",
            "type": "group",
            "position": [0, 0],
            "parameters": {},
            "member_node_ids": ["merge-1"],
            "input_mappings": {
                "A": ["merge-1", "A"],
                "B": ["merge-1", "B"]
            },
            "output_mappings": {
                "result": ["merge-1", "result"]
            },
            "is_expanded": False,
            "group_color": "#4A90D9"
        }

        group = GroupNode.from_dict(data)

        # Mappings should be tuples (list -> tuple)
        assert group.input_mappings["A"] == ("merge-1", "A")
        assert group.input_mappings["B"] == ("merge-1", "B")
        assert group.output_mappings["result"] == ("merge-1", "result")

    def test_roundtrip_serialization(self):
        """序列化和反序列化应保留所有属性。"""
        original = GroupNode(node_id="group-test")
        original.position = (150, 250)
        original.add_member("node-1")
        original.add_member("node-2")
        original.add_input_port("input_a", "node-1", "image")
        original.add_output_port("output_a", "node-2", "result")
        original.group_color = "#FF5500"
        original.is_expanded = True
        original.parameters["group_name"] = "My Group"

        data = original.to_dict()
        restored = GroupNode.from_dict(data)

        assert restored.id == original.id
        assert restored.position == original.position
        assert restored.member_node_ids == original.member_node_ids
        assert restored.input_mappings == original.input_mappings
        assert restored.output_mappings == original.output_mappings
        assert restored.group_color == original.group_color
        assert restored.is_expanded == original.is_expanded
        assert restored.parameters["group_name"] == "My Group"

    def test_deserialize_with_defaults(self):
        """反序列化时缺少的属性应使用默认值。"""
        data = {
            "id": "group-1",
            "type": "group",
            "position": [0, 0],
            "parameters": {}
        }

        group = GroupNode.from_dict(data)

        # Missing fields should use defaults
        assert group.member_node_ids == []
        assert group.input_mappings == {}
        assert group.output_mappings == {}
        assert group.is_expanded == False
        assert group.group_color == "#4A90D9"


class TestGroupEvaluation:
    """_do_evaluate behavior."""

    def test_group_does_not_render(self):
        """Group 节点不参与渲染（pass-through）。"""
        group = GroupNode(node_id="group-1")

        # Group's _do_evaluate should pass through first input
        result = group._do_evaluate(
            inputs={"input_1": "dummy_texture"},
            ctx=None,
            fb_manager=None,
            resource_pool=None,
            render_mode="preview"
        )

        # Should return empty or pass-through (placeholder behavior)
        assert isinstance(result, dict)

    def test_group_is_container(self):
        """Group 是容器节点，不是渲染节点。"""
        group = GroupNode()

        # Group should not have complex evaluation logic
        # It's a UI-level grouping mechanism
        assert group.node_type == "group"


class TestGroupDisabledState:
    """Disabled state handling."""

    def test_group_disabled_state(self):
        """Group 可以被禁用。"""
        group = GroupNode()
        group.disabled = True

        assert group.disabled == True

    def test_disabled_in_serialization(self):
        """禁用状态应被序列化。"""
        group = GroupNode(node_id="group-1")
        group.disabled = True

        data = group.to_dict()
        assert data["disabled"] == True

        restored = GroupNode.from_dict(data)
        assert restored.disabled == True


class TestGroupPosition:
    """Position handling."""

    def test_set_position(self):
        """设置 group 位置。"""
        group = GroupNode()
        group.position = (100, 200)

        assert group.position == (100, 200)

    def test_position_in_serialization(self):
        """位置应被序列化。"""
        group = GroupNode(node_id="group-1")
        group.position = (300, 400)

        data = group.to_dict()
        assert data["position"] == [300, 400]

        restored = GroupNode.from_dict(data)
        assert restored.position == (300, 400)


class TestGroupNodeNodeGraphIntegration:
    """Test GroupNode integration with NodeGraph."""

    def test_set_node_graph(self):
        graph = NodeGraph()
        group = GroupNode()
        graph.add_node(group)
        assert group._node_graph is graph

    def test_group_in_graph_can_serialize(self):
        graph = NodeGraph()
        group = GroupNode()
        graph.add_node(group)
        data = graph.to_dict()
        assert any(n["type"] == "group" for n in data["nodes"])

    def test_group_roundtrip_via_graph(self):
        graph = NodeGraph()
        group = GroupNode()
        group.add_member("blur_001")
        group.add_input_port("input_a", "blur_001", "image")
        graph.add_node(group)

        data = graph.to_dict()
        graph2 = NodeGraph.from_dict(data)

        groups = [n for n in graph2.nodes.values() if n.node_type == "group"]
        assert len(groups) == 1
        restored = groups[0]
        assert restored.member_node_ids == ["blur_001"]
        assert restored.input_mappings["input_a"] == ("blur_001", "image")


class TestGroupNodeDirtyFlag:
    """Test that group modifications mark it dirty."""

    def test_add_member_marks_dirty(self):
        node = GroupNode()
        node._dirty = False
        node.add_member("blur_001")
        assert node._dirty is True

    def test_remove_member_marks_dirty(self):
        node = GroupNode()
        node._dirty = False
        node.add_member("blur_001")
        node._dirty = False
        node.remove_member("blur_001")
        assert node._dirty is True

    def test_add_input_port_marks_dirty(self):
        node = GroupNode()
        node._dirty = False
        node.add_member("blur_001")
        node._dirty = False
        node.add_input_port("input_a", "blur_001", "image")
        assert node._dirty is True

    def test_add_output_port_marks_dirty(self):
        node = GroupNode()
        node._dirty = False
        node.add_member("blur_001")
        node._dirty = False
        node.add_output_port("output_a", "blur_001", "result")
        assert node._dirty is True

    def test_remove_input_port_marks_dirty(self):
        node = GroupNode()
        node.add_member("blur_001")
        node.add_input_port("input_a", "blur_001", "image")
        node._dirty = False
        node.remove_input_port("input_a")
        assert node._dirty is True

    def test_remove_output_port_marks_dirty(self):
        node = GroupNode()
        node.add_member("blur_001")
        node.add_output_port("output_a", "blur_001", "result")
        node._dirty = False
        node.remove_output_port("output_a")
        assert node._dirty is True