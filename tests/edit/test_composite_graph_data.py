"""
Composite Graph Data Tests (P9.2 + P1 序列化补齐)
合成图数据模型测试。

测试覆盖：
- CompositeGraphData 创建和验证
- CompositeGraphData 序列化/反序列化（完整）
- viewer_state、viewport_state、backdrops 序列化
- ClipInstance composite_id 字段
- EditProject compositions 管理
- 默认合成图创建
- NodeGraph 格式转换
- 向后兼容（旧格式节点）

验收标准：
- 所有边界情况覆盖
- 纯函数无 PyQt 依赖
"""

import pytest
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.composite_graph_data import CompositeGraphData
from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.edit.core.project import EditProject


# ================================================================
# CompositeGraphData Tests
# ================================================================


class TestCompositeGraphDataCreation:
    """测试 CompositeGraphData 创建。"""

    def test_create_basic_composite(self):
        """测试基本合成图创建。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        assert comp.composite_id == "comp1"
        assert comp.source_clip_id == "clip1"
        assert comp.source_asset_id == "asset1"
        assert comp.source_asset_path == "/path/to/image.png"

    def test_create_with_name(self):
        """测试带名称的合成图创建。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            name="My Composite",
        )

        assert comp.name == "My Composite"

    def test_create_with_nodes_and_connections(self):
        """测试带节点和连接的合成图创建（新格式）。"""
        nodes = [
            {"id": "n1", "type": "image_input", "position": [100, 200], "parameters": {}},
            {"id": "n2", "type": "output", "position": [400, 200], "parameters": {}},
        ]
        connections = [
            {"src_node_id": "n1", "src_port_name": "output", "dst_node_id": "n2", "dst_port_name": "input"},
        ]

        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            nodes=nodes,
            connections=connections,
        )

        assert len(comp.nodes) == 2
        assert len(comp.connections) == 1

    def test_create_with_viewer_state(self):
        """测试带 viewer_state 的合成图创建。"""
        viewer_state = {
            "zoom": 1.5,
            "pan_x": 100.0,
            "pan_y": 50.0,
            "viewing_node_id": "n1",
        }

        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            viewer_state=viewer_state,
        )

        assert comp.viewer_state["zoom"] == 1.5
        assert comp.viewer_state["viewing_node_id"] == "n1"

    def test_create_with_viewport_state(self):
        """测试带 viewport_state 的合成图创建。"""
        viewport_state = {
            "zoom": 0.8,
            "pan_x": 200.0,
            "pan_y": 100.0,
        }

        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            viewport_state=viewport_state,
        )

        assert comp.viewport_state["zoom"] == 0.8

    def test_create_with_backdrops_and_sticky_notes(self):
        """测试带背景框和便签的合成图创建。"""
        backdrops = {
            "bd1": {"title": "Group 1", "pos": [0, 0], "size": [500, 300]},
        }
        sticky_notes = {
            "sn1": {"text": "Note", "pos": [100, 100]},
        }

        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            backdrops=backdrops,
            sticky_notes=sticky_notes,
        )

        assert comp.backdrops["bd1"]["title"] == "Group 1"
        assert comp.sticky_notes["sn1"]["text"] == "Note"

    def test_invalid_empty_composite_id(self):
        """测试空 composite_id 验证。"""
        with pytest.raises(ValueError):
            CompositeGraphData(
                composite_id="",
                source_clip_id="clip1",
                source_asset_id="asset1",
                source_asset_path="/path/to/image.png",
            )

    def test_invalid_empty_source_clip_id(self):
        """测试空 source_clip_id 验证。"""
        with pytest.raises(ValueError):
            CompositeGraphData(
                composite_id="comp1",
                source_clip_id="",
                source_asset_id="asset1",
                source_asset_path="/path/to/image.png",
            )

    def test_invalid_resolution(self):
        """测试无效分辨率验证。"""
        with pytest.raises(ValueError):
            CompositeGraphData(
                composite_id="comp1",
                source_clip_id="clip1",
                source_asset_id="asset1",
                source_asset_path="/path/to/image.png",
                width=0,
                height=1080,
            )


class TestCompositeGraphDataSerialization:
    """测试 CompositeGraphData 序列化（完整）。"""

    def test_to_dict_basic(self):
        """测试基本序列化为字典。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            name="Test Composite",
            width=1920,
            height=1080,
        )

        data = comp.to_dict()

        assert data["composite_id"] == "comp1"
        assert data["source_clip_id"] == "clip1"
        assert data["name"] == "Test Composite"
        assert data["width"] == 1920
        assert data["height"] == 1080

        # 新增字段应存在（即使为空）
        assert "viewer_state" in data
        assert "viewport_state" in data
        assert "backdrops" in data
        assert "sticky_notes" in data
        assert "groups" in data

    def test_to_dict_with_nodes_and_connections(self):
        """测试带节点和连接的序列化。"""
        nodes = [
            {"id": "n1", "type": "image_input", "parameters": {}},
            {"id": "n2", "type": "blur", "parameters": {"radius": 5.0}},
        ]
        connections = [
            {"src_node_id": "n1", "src_port_name": "output", "dst_node_id": "n2", "dst_port_name": "input"},
        ]

        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            nodes=nodes,
            connections=connections,
        )

        data = comp.to_dict()

        assert len(data["nodes"]) == 2
        assert data["nodes"][0]["id"] == "n1"
        assert data["nodes"][1]["type"] == "blur"
        assert data["nodes"][1]["parameters"]["radius"] == 5.0
        assert len(data["connections"]) == 1

    def test_to_dict_with_viewer_state(self):
        """测试带 viewer_state 的完整序列化。"""
        viewer_state = {
            "zoom": 1.5,
            "pan_x": 100.0,
            "pan_y": 50.0,
            "viewing_node_id": "n1",
            "show_grid": True,
            "show_overlays": True,
        }

        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            viewer_state=viewer_state,
        )

        data = comp.to_dict()

        assert data["viewer_state"]["zoom"] == 1.5
        assert data["viewer_state"]["viewing_node_id"] == "n1"
        assert data["viewer_state"]["show_grid"] == True

    def test_to_dict_with_viewport_state(self):
        """测试带 viewport_state 的完整序列化。"""
        viewport_state = {
            "zoom": 0.8,
            "pan_x": 200.0,
            "pan_y": 100.0,
        }

        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            viewport_state=viewport_state,
        )

        data = comp.to_dict()

        assert data["viewport_state"]["zoom"] == 0.8
        assert data["viewport_state"]["pan_x"] == 200.0

    def test_to_dict_with_backdrops(self):
        """测试带背景框的序列化。"""
        backdrops = {
            "bd1": {
                "title": "Color Correction",
                "pos": [0, 0],
                "size": [500, 300],
                "color": "#336699",
            },
        }

        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            backdrops=backdrops,
        )

        data = comp.to_dict()

        assert "bd1" in data["backdrops"]
        assert data["backdrops"]["bd1"]["title"] == "Color Correction"

    def test_from_dict_basic(self):
        """测试从字典反序列化。"""
        data = {
            "composite_id": "comp1",
            "source_clip_id": "clip1",
            "source_asset_id": "asset1",
            "source_asset_path": "/path/to/image.png",
            "name": "Test Composite",
            "nodes": [{"id": "n1", "type": "image_input"}],
            "connections": [{"src_node_id": "n1", "dst_node_id": "n2"}],
            "width": 1920,
            "height": 1080,
        }

        comp = CompositeGraphData.from_dict(data)

        assert comp.composite_id == "comp1"
        assert comp.source_clip_id == "clip1"
        assert comp.name == "Test Composite"
        assert len(comp.nodes) == 1
        assert len(comp.connections) == 1

    def test_from_dict_with_viewer_state(self):
        """测试从字典反序列化 viewer_state。"""
        data = {
            "composite_id": "comp1",
            "source_clip_id": "clip1",
            "source_asset_id": "asset1",
            "source_asset_path": "/path/to/image.png",
            "viewer_state": {
                "zoom": 1.5,
                "viewing_node_id": "n1",
            },
        }

        comp = CompositeGraphData.from_dict(data)

        assert comp.viewer_state["zoom"] == 1.5
        assert comp.viewer_state["viewing_node_id"] == "n1"

    def test_from_dict_with_viewport_state(self):
        """测试从字典反序列化 viewport_state。"""
        data = {
            "composite_id": "comp1",
            "source_clip_id": "clip1",
            "source_asset_id": "asset1",
            "source_asset_path": "/path/to/image.png",
            "viewport_state": {
                "zoom": 0.8,
                "pan_x": 200.0,
            },
        }

        comp = CompositeGraphData.from_dict(data)

        assert comp.viewport_state["zoom"] == 0.8
        assert comp.viewport_state["pan_x"] == 200.0

    def test_from_dict_defaults(self):
        """测试从字典反序列化使用默认值。"""
        data = {
            "source_clip_id": "clip1",
            "source_asset_id": "asset1",
        }

        comp = CompositeGraphData.from_dict(data)

        assert comp.composite_id  # 自动生成 UUID
        assert comp.source_asset_path == ""
        assert comp.name == ""
        assert comp.nodes == []
        assert comp.connections == []
        assert comp.viewer_state == {}
        assert comp.viewport_state == {}

    def test_serialize_deserialize_roundtrip(self):
        """测试完整序列化/反序列化往返。"""
        original = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            name="Full Test",
            nodes=[
                {"id": "n1", "type": "image_input", "position": [100, 200], "parameters": {"file_path": "/test.png"}},
                {"id": "n2", "type": "blur", "position": [300, 200], "parameters": {"radius": 10.0}},
                {"id": "n3", "type": "output", "position": [500, 200], "parameters": {}},
            ],
            connections=[
                {"src_node_id": "n1", "src_port_name": "output", "dst_node_id": "n2", "dst_port_name": "input"},
                {"src_node_id": "n2", "src_port_name": "output", "dst_node_id": "n3", "dst_port_name": "input"},
            ],
            viewer_state={"zoom": 1.5, "viewing_node_id": "n2"},
            viewport_state={"zoom": 0.8, "pan_x": 200.0, "pan_y": 100.0},
            backdrops={"bd1": {"title": "Effects", "pos": [0, 0], "size": [600, 400]}},
            sticky_notes={"sn1": {"text": "TODO", "pos": [50, 50]}},
            groups={"g1": ["n1", "n2", "n3"]},
            width=3840,
            height=2160,
            created_at=time.time(),
            modified_at=time.time(),
        )

        data = original.to_dict()
        restored = CompositeGraphData.from_dict(data)

        # 验证所有字段完全一致
        assert restored.composite_id == original.composite_id
        assert restored.source_clip_id == original.source_clip_id
        assert restored.name == original.name
        assert len(restored.nodes) == 3
        assert restored.nodes[0]["id"] == "n1"
        assert restored.nodes[1]["parameters"]["radius"] == 10.0
        assert len(restored.connections) == 2
        assert restored.viewer_state == original.viewer_state
        assert restored.viewport_state == original.viewport_state
        assert restored.backdrops == original.backdrops
        assert restored.sticky_notes == original.sticky_notes
        assert restored.groups == original.groups
        assert restored.width == 3840
        assert restored.height == 2160


class TestCompositeGraphDataBackwardCompatibility:
    """测试向后兼容（旧格式节点）。"""

    def test_from_dict_old_node_format(self):
        """测试旧格式节点（node_id/node_type）反序列化。"""
        data = {
            "composite_id": "comp1",
            "source_clip_id": "clip1",
            "source_asset_id": "asset1",
            "source_asset_path": "/path/to/image.png",
            "nodes": [
                {"node_id": "n1", "node_type": "image_input", "position": [100, 200]},
                {"node_id": "n2", "node_type": "output", "position": {"x": 400, "y": 200}},
            ],
            "connections": [
                {"src_node_id": "n1", "dst_node_id": "n2"},
            ],
        }

        comp = CompositeGraphData.from_dict(data)

        # 节点应被规范化为新格式
        assert comp.nodes[0]["id"] == "n1"
        assert comp.nodes[0]["type"] == "image_input"
        assert comp.nodes[1]["id"] == "n2"
        assert comp.nodes[1]["type"] == "output"

        # position 格式也应被规范化
        assert comp.nodes[1]["position"] == [400, 200]

    def test_from_dict_missing_new_fields(self):
        """测试缺少新增字段的旧数据反序列化。"""
        data = {
            "composite_id": "comp1",
            "source_clip_id": "clip1",
            "source_asset_id": "asset1",
            "source_asset_path": "/path/to/image.png",
            "nodes": [{"id": "n1", "type": "image_input"}],
            "connections": [],
        }

        comp = CompositeGraphData.from_dict(data)

        # 新字段应有默认空值
        assert comp.viewer_state == {}
        assert comp.viewport_state == {}
        assert comp.backdrops == {}
        assert comp.sticky_notes == {}
        assert comp.groups == {}


class TestCompositeGraphDataDefaultCreation:
    """测试默认合成图创建。"""

    def test_create_default_for_clip(self):
        """Phase 4: 默认图始终包含 TimelineClipInput -> Topos Color -> Output。"""
        comp = CompositeGraphData.create_default_for_clip(
            clip_id="clip1",
            asset_id="asset1",
            asset_path="/path/to/image.png",
        )

        assert comp.source_clip_id == "clip1"
        assert comp.source_asset_id == "asset1"
        assert comp.source_asset_path == "/path/to/image.png"

        # Phase 4: 默认图始终包含三个节点
        assert len(comp.nodes) == 3
        assert comp.nodes[0]["type"] == "timeline_clip_input"
        assert comp.nodes[1]["type"] == "topos_color"
        assert comp.nodes[2]["type"] == "output"

        # Phase 4: Topos Color bypass when no stack
        topos_params = comp.nodes[1]["parameters"]
        assert topos_params["color_stack_id"] == ""  # pass-through when no stack
        assert topos_params["bypass"] is True  # bypass mode when no stack

        # 应有两个连接
        assert len(comp.connections) == 2

        # 新字段应存在
        assert comp.viewer_state == {}
        assert comp.viewport_state == {}

    def test_create_default_with_name(self):
        """测试带名称的默认合成图创建。"""
        comp = CompositeGraphData.create_default_for_clip(
            clip_id="clip1",
            asset_id="asset1",
            asset_path="/path/to/image.png",
            name="Custom Name",
        )

        assert comp.name == "Custom Name"

    def test_create_default_with_resolution(self):
        """测试指定分辨率的默认合成图创建。"""
        comp = CompositeGraphData.create_default_for_clip(
            clip_id="clip1",
            asset_id="asset1",
            asset_path="/path/to/image.png",
            width=3840,
            height=2160,
        )

        assert comp.width == 3840
        assert comp.height == 2160


class TestCompositeGraphDataMethods:
    """测试 CompositeGraphData 方法。"""

    def test_with_name(self):
        """测试 with_name 方法。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        new_comp = comp.with_name("New Name")

        assert new_comp.name == "New Name"
        assert new_comp.composite_id == comp.composite_id

    def test_with_nodes(self):
        """测试 with_nodes 方法。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        new_nodes = [{"id": "n1", "type": "blur"}]
        new_comp = comp.with_nodes(new_nodes)

        assert len(new_comp.nodes) == 1
        assert new_comp.nodes[0]["type"] == "blur"

    def test_with_graph_state(self):
        """测试 with_graph_state 方法。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        nodes = [{"id": "n1", "type": "blur"}]
        connections = [{"src_node_id": "n1", "dst_node_id": "n2"}]

        new_comp = comp.with_graph_state(nodes, connections)

        assert len(new_comp.nodes) == 1
        assert len(new_comp.connections) == 1

    def test_with_viewer_state(self):
        """测试 with_viewer_state 方法。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        viewer_state = {"zoom": 2.0, "viewing_node_id": "n1"}
        new_comp = comp.with_viewer_state(viewer_state)

        assert new_comp.viewer_state["zoom"] == 2.0
        assert comp.viewer_state == {}  # 原 comp 不变

    def test_with_viewport_state(self):
        """测试 with_viewport_state 方法。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        viewport_state = {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0}
        new_comp = comp.with_viewport_state(viewport_state)

        assert new_comp.viewport_state["zoom"] == 1.0
        assert comp.viewport_state == {}  # 原 comp 不变

    def test_with_full_graph_data(self):
        """测试 with_full_graph_data 方法。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        new_comp = comp.with_full_graph_data(
            nodes=[{"id": "n1", "type": "blur"}],
            connections=[],
            viewer_state={"zoom": 1.5},
            viewport_state={"zoom": 0.8},
            backdrops={"bd1": {"title": "Test"}},
        )

        assert len(new_comp.nodes) == 1
        assert new_comp.viewer_state["zoom"] == 1.5
        assert new_comp.viewport_state["zoom"] == 0.8
        assert new_comp.backdrops["bd1"]["title"] == "Test"

    def test_hash_and_equality(self):
        """测试哈希和相等性。"""
        comp1 = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )
        comp2 = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip2",  # 不同的 source_clip_id
            source_asset_id="asset2",
            source_asset_path="/path/to/other.png",
        )
        comp3 = CompositeGraphData(
            composite_id="comp3",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        # 相等性基于 composite_id
        assert comp1 == comp2
        assert comp1 != comp3

        # 哈希基于 composite_id
        assert hash(comp1) == hash(comp2)
        assert hash(comp1) != hash(comp3)


class TestCompositeGraphDataNodeGraphConversion:
    """测试 NodeGraph 格式转换。"""

    def test_from_node_graph(self):
        """测试从 NodeGraph.to_dict() 创建。"""
        node_graph_dict = {
            "nodes": [
                {"id": "n1", "type": "image_input", "position": [100, 200], "parameters": {}},
                {"id": "n2", "type": "blur", "position": [300, 200], "parameters": {"radius": 5.0}},
            ],
            "connections": [
                {"src_node_id": "n1", "src_port_name": "output", "dst_node_id": "n2", "dst_port_name": "input"},
            ],
            "viewer_state": {"zoom": 1.5, "viewing_node_id": "n2"},
            "viewport_state": {"zoom": 0.8, "pan_x": 200.0},
            "backdrops": {"bd1": {"title": "Effects"}},
            "sticky_notes": {},
            "groups": {},
        }

        comp = CompositeGraphData.from_node_graph(
            node_graph_dict,
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            name="From NodeGraph",
            width=1920,
            height=1080,
        )

        assert len(comp.nodes) == 2
        assert comp.viewer_state["zoom"] == 1.5
        assert comp.viewport_state["zoom"] == 0.8
        assert comp.backdrops["bd1"]["title"] == "Effects"

    def test_to_node_graph_dict(self):
        """测试转换为 NodeGraph 格式。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            nodes=[{"id": "n1", "type": "blur"}],
            connections=[{"src_node_id": "n1", "dst_node_id": "n2"}],
            viewer_state={"zoom": 1.5},
            viewport_state={"zoom": 0.8},
            backdrops={"bd1": {"title": "Test"}},
        )

        node_graph_dict = comp.to_node_graph_dict()

        assert "nodes" in node_graph_dict
        assert "connections" in node_graph_dict
        assert "viewer_state" in node_graph_dict
        assert "viewport_state" in node_graph_dict
        assert node_graph_dict["viewer_state"]["zoom"] == 1.5

    def test_node_graph_roundtrip(self):
        """测试 NodeGraph 格式往返转换。"""
        original_dict = {
            "nodes": [
                {"id": "n1", "type": "image_input", "position": [100, 200], "parameters": {"file_path": "/test.png"}},
                {"id": "n2", "type": "output", "position": [400, 200], "parameters": {}},
            ],
            "connections": [
                {"src_node_id": "n1", "src_port_name": "output", "dst_node_id": "n2", "dst_port_name": "input"},
            ],
            "viewer_state": {"zoom": 1.0, "viewing_node_id": "n1"},
            "viewport_state": {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0},
            "backdrops": {},
            "sticky_notes": {},
            "groups": {},
        }

        # 从 NodeGraph 创建 CompositeGraphData
        comp = CompositeGraphData.from_node_graph(
            original_dict,
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/test.png",
        )

        # 转换回 NodeGraph 格式
        restored_dict = comp.to_node_graph_dict()

        # 验证数据完整
        assert len(restored_dict["nodes"]) == 2
        assert restored_dict["nodes"][0]["id"] == "n1"
        assert restored_dict["viewer_state"] == original_dict["viewer_state"]
        assert restored_dict["viewport_state"] == original_dict["viewport_state"]


class TestCompositeGraphDataValidation:
    """测试数据验证。"""

    def test_validate_valid_data(self):
        """测试有效数据验证。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            nodes=[{"id": "n1", "type": "blur"}],
            connections=[{"src_node_id": "n1", "src_port_name": "output", "dst_node_id": "n2", "dst_port_name": "input"}],
        )

        errors = comp.validate()
        assert len(errors) == 0
        assert comp.is_valid()

    def test_validate_missing_node_id(self):
        """测试节点缺少 id 的验证。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            nodes=[{"type": "blur"}],  # 缺少 id
            connections=[],
        )

        errors = comp.validate()
        assert len(errors) > 0
        assert any("missing id" in e for e in errors)

    def test_validate_missing_node_type(self):
        """测试节点缺少 type 的验证。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            nodes=[{"id": "n1"}],  # 缺少 type
            connections=[],
        )

        errors = comp.validate()
        assert len(errors) > 0
        assert any("missing type" in e for e in errors)

    def test_validate_missing_connection_field(self):
        """测试连接缺少字段的验证。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            nodes=[{"id": "n1", "type": "blur"}],
            connections=[{"src_node_id": "n1"}],  # 缺少其他字段
        )

        errors = comp.validate()
        assert len(errors) > 0
        assert any("missing" in e for e in errors)

    def test_node_count_and_connection_count(self):
        """测试节点和连接计数属性。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            nodes=[
                {"id": "n1", "type": "image_input"},
                {"id": "n2", "type": "blur"},
                {"id": "n3", "type": "output"},
            ],
            connections=[
                {"src_node_id": "n1", "src_port_name": "output", "dst_node_id": "n2", "dst_port_name": "input"},
                {"src_node_id": "n2", "src_port_name": "output", "dst_node_id": "n3", "dst_port_name": "input"},
            ],
        )

        assert comp.node_count == 3
        assert comp.connection_count == 2

    def test_get_node_types(self):
        """测试节点类型统计。"""
        comp = CompositeGraphData(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
            nodes=[
                {"id": "n1", "type": "image_input"},
                {"id": "n2", "type": "blur"},
                {"id": "n3", "type": "blur"},
                {"id": "n4", "type": "output"},
            ],
            connections=[],
        )

        type_counts = comp.get_node_types()

        assert type_counts["image_input"] == 1
        assert type_counts["blur"] == 2
        assert type_counts["output"] == 1


# ================================================================
# ClipInstance composite_id Tests
# ================================================================


class TestClipInstanceCompositeId:
    """测试 ClipInstance composite_id 字段。"""

    def test_clip_without_composite_id(self):
        """测试无 composite_id 的 clip。"""
        clip = ClipInstance(
            clip_id="clip1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        )

        assert clip.composite_id is None

    def test_clip_with_composite_id(self):
        """测试带 composite_id 的 clip。"""
        clip = ClipInstance(
            clip_id="clip1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            composite_id="comp1",
        )

        assert clip.composite_id == "comp1"

    def test_with_composite_id(self):
        """测试 with_composite_id 方法。"""
        clip = ClipInstance(
            clip_id="clip1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        )

        new_clip = clip.with_composite_id("comp1")

        assert new_clip.composite_id == "comp1"
        assert clip.composite_id is None  # 原 clip 不变

    def test_with_composite_id_none(self):
        """测试 with_composite_id(None) 移除合成引用。"""
        clip = ClipInstance(
            clip_id="clip1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            composite_id="comp1",
        )

        new_clip = clip.with_composite_id(None)

        assert new_clip.composite_id is None
        assert clip.composite_id == "comp1"  # 原 clip 不变

    def test_composite_id_preserved_in_other_operations(self):
        """测试其他操作保持 composite_id。"""
        clip = ClipInstance(
            clip_id="clip1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            composite_id="comp1",
        )

        # 测试各种 with_* 方法
        new_clip = clip.with_timeline_start(50)
        assert new_clip.composite_id == "comp1"

        new_clip = clip.with_opacity(0.5)
        assert new_clip.composite_id == "comp1"

        new_clip = clip.with_enabled(False)
        assert new_clip.composite_id == "comp1"


# ================================================================
# EditProject compositions Tests
# ================================================================


class TestEditProjectCompositions:
    """测试 EditProject compositions 管理。"""

    @pytest.fixture
    def empty_project(self) -> EditProject:
        """创建空项目。"""
        return EditProject.create("Test Project")

    def test_create_project_with_empty_compositions(self, empty_project):
        """测试新建项目 compositions 为空。"""
        assert empty_project.compositions == {}
        assert empty_project.composition_count == 0

    def test_create_composition(self, empty_project):
        """测试创建合成。"""
        project = empty_project.create_composition(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        assert "comp1" in project.compositions
        assert project.composition_count == 1

        comp_data = project.get_composition_by_id("comp1")
        assert comp_data is not None
        assert comp_data["source_clip_id"] == "clip1"

    def test_create_composition_with_color_stack_inserts_topos_color(self, empty_project):
        """创建有调色栈的合成时应自动插入 Topos Color 节点。"""
        project = empty_project.create_composition(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/video.mov",
            color_stack_id="stack1",
        )

        comp_data = project.get_composition_by_id("comp1")
        node_types = {node["type"] for node in comp_data["nodes"]}
        assert "timeline_clip_input" in node_types
        assert "topos_color" in node_types
        assert "output" in node_types
        source_node = next(node for node in comp_data["nodes"] if node["type"] == "timeline_clip_input")
        topos_node = next(node for node in comp_data["nodes"] if node["type"] == "topos_color")
        assert source_node["parameters"]["input_stage"] == "source_raw"
        assert topos_node["parameters"]["color_stack_id"] == "stack1"

    def test_from_dict_infers_source_fields_from_timeline_clip_input(self):
        """NodeGraph-only dicts should migrate source refs from TimelineClipInput."""
        graph = CompositeGraphData.from_dict({
            "composite_id": "comp1",
            "nodes": [
                {
                    "id": "src1",
                    "type": "timeline_clip_input",
                    "parameters": {
                        "timeline_id": "tl1",
                        "clip_id": "clip1",
                        "asset_id": "asset1",
                        "input_stage": "source_raw",
                    },
                },
                {"id": "out1", "type": "output", "parameters": {}},
            ],
            "connections": [],
        })

        assert graph.source_clip_id == "clip1"
        assert graph.source_asset_id == "asset1"
        assert graph.timeline_id == "tl1"
        assert graph.clip_id == "clip1"

    def test_get_composition_not_found(self, empty_project):
        """测试获取不存在的合成。"""
        comp = empty_project.get_composition_by_id("nonexistent")
        assert comp is None

    def test_update_composition(self, empty_project):
        """测试更新合成。"""
        project = empty_project.create_composition(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        # 更新节点
        new_nodes = [{"id": "n1", "type": "blur"}]
        project = project.update_composition("comp1", nodes=new_nodes)

        comp_data = project.get_composition_by_id("comp1")
        assert len(comp_data["nodes"]) == 1

    def test_update_composition_not_found(self, empty_project):
        """测试更新不存在的合成抛出错误。"""
        with pytest.raises(ValueError):
            empty_project.update_composition("nonexistent", nodes=[])

    def test_remove_composition(self, empty_project):
        """测试移除合成。"""
        project = empty_project.create_composition(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        project = project.remove_composition("comp1")

        assert "comp1" not in project.compositions
        assert project.composition_count == 0

    def test_remove_composition_not_found(self, empty_project):
        """测试移除不存在的合成返回原项目。"""
        project = empty_project.remove_composition("nonexistent")
        assert project == empty_project

    def test_composition_preserved_in_other_operations(self, empty_project):
        """测试其他操作保持 compositions。"""
        # 首先添加一个素材（需要有效的元数据）
        project = empty_project.create_composition(
            composite_id="comp1",
            source_clip_id="clip1",
            source_asset_id="asset1",
            source_asset_path="/path/to/image.png",
        )

        # 创建时间线
        project = project.create_timeline("Timeline 1")
        assert "comp1" in project.compositions

        # 创建 bin
        project = project.create_bin("New Bin")
        assert "comp1" in project.compositions


# ================================================================
# Integration Tests (完整序列化流程)
# ================================================================


class TestCompositeGraphDataIntegration:
    """测试完整序列化流程（P1 验收）。"""

    def test_full_save_load_cycle(self):
        """测试完整保存/加载周期（验收标准）。"""
        # 创建一个完整的合成图
        comp = CompositeGraphData(
            composite_id="comp_test_001",
            source_clip_id="clip_001",
            source_asset_id="asset_001",
            source_asset_path="/Users/test/Media/shot_001.exr",
            name="Main Shot Composite",
            nodes=[
                {
                    "id": "n_read",
                    "type": "image_input",
                    "position": [50, 200],
                    "parameters": {"file_path": "/Users/test/Media/shot_001.exr"},
                    "disabled": False,
                },
                {
                    "id": "n_grade",
                    "type": "grade",
                    "position": [200, 200],
                    "parameters": {
                        "lift": [0.0, 0.0, 0.0],
                        "gamma": [1.0, 1.0, 1.0],
                        "gain": [1.2, 1.1, 1.0],
                    },
                    "disabled": False,
                    "expressions": {"gain[0]": "this.width * 0.001"},
                },
                {
                    "id": "n_blur",
                    "type": "blur",
                    "position": [350, 200],
                    "parameters": {"radius": 5.0},
                    "disabled": True,
                },
                {
                    "id": "n_out",
                    "type": "output",
                    "position": [500, 200],
                    "parameters": {},
                    "disabled": False,
                },
            ],
            connections=[
                {"src_node_id": "n_read", "src_port_name": "output", "dst_node_id": "n_grade", "dst_port_name": "input"},
                {"src_node_id": "n_grade", "src_port_name": "output", "dst_node_id": "n_blur", "dst_port_name": "input"},
                {"src_node_id": "n_blur", "src_port_name": "output", "dst_node_id": "n_out", "dst_port_name": "input"},
            ],
            viewer_state={
                "zoom": 1.5,
                "pan_x": 0.0,
                "pan_y": 0.0,
                "viewing_node_id": "n_grade",
                "show_grid": True,
                "show_overlays": False,
            },
            viewport_state={
                "zoom": 0.8,
                "pan_x": 150.0,
                "pan_y": 50.0,
            },
            backdrops={
                "bd_color": {
                    "title": "Color Correction",
                    "pos": [180, 150],
                    "size": [180, 100],
                    "color": "#336699",
                },
            },
            sticky_notes={
                "sn_todo": {
                    "text": "TODO: Add final output node",
                    "pos": [550, 100],
                },
            },
            groups={
                "g_main": ["n_read", "n_grade", "n_out"],
            },
            width=3840,
            height=2160,
            created_at=time.time(),
            modified_at=time.time(),
        )

        # 序列化
        data = comp.to_dict()

        # 模拟保存到 JSON 文件
        import json
        json_str = json.dumps(data, indent=2)

        # 模拟从 JSON 文件加载
        loaded_data = json.loads(json_str)

        # 反序列化
        restored = CompositeGraphData.from_dict(loaded_data)

        # 验证：Project 内 Composite 关闭重开不丢数据
        assert restored.composite_id == comp.composite_id
        assert restored.name == comp.name
        assert restored.source_clip_id == comp.source_clip_id
        assert restored.width == comp.width
        assert restored.height == comp.height

        # 验证节点完整
        assert len(restored.nodes) == 4
        assert restored.nodes[0]["id"] == "n_read"
        assert restored.nodes[1]["type"] == "grade"
        assert restored.nodes[1]["parameters"]["gain"] == [1.2, 1.1, 1.0]
        assert restored.nodes[1]["expressions"]["gain[0]"] == "this.width * 0.001"
        assert restored.nodes[2]["disabled"] == True

        # 验证连接完整
        assert len(restored.connections) == 3

        # 验证 viewer_state 完整
        assert restored.viewer_state["zoom"] == 1.5
        assert restored.viewer_state["viewing_node_id"] == "n_grade"
        assert restored.viewer_state["show_grid"] == True

        # 验证 viewport_state 完整
        assert restored.viewport_state["zoom"] == 0.8
        assert restored.viewport_state["pan_x"] == 150.0

        # 验证 backdrops 完整
        assert restored.backdrops["bd_color"]["title"] == "Color Correction"

        # 验证 sticky_notes 完整
        assert restored.sticky_notes["sn_todo"]["text"] == "TODO: Add final output node"

        # 验证 groups 完整
        assert restored.groups["g_main"] == ["n_read", "n_grade", "n_out"]

    def test_composite_graphs_json_structure(self):
        """测试 composite/graphs.json 结构符合规范。"""
        comp = CompositeGraphData(
            composite_id="comp_001",
            source_clip_id="clip_001",
            source_asset_id="asset_001",
            source_asset_path="/path/to/media.exr",
            name="Test Composite",
            nodes=[{"id": "n1", "type": "image_input", "position": [100, 200]}],
            connections=[{"src_node_id": "n1", "src_port_name": "output", "dst_node_id": "n2", "dst_port_name": "input"}],
            viewer_state={"zoom": 1.0},
        )

        # 模拟 composite/graphs.json 结构
        graphs_data = {comp.composite_id: comp.to_dict()}

        # 验证 JSON 结构
        assert "comp_001" in graphs_data
        graph_entry = graphs_data["comp_001"]

        # 必须字段
        assert "composite_id" in graph_entry
        assert "nodes" in graph_entry
        assert "connections" in graph_entry

        # 节点格式符合 schema（使用 id/type）
        node = graph_entry["nodes"][0]
        assert "id" in node
        assert "type" in node
        assert "position" in node

        # 连接格式符合 schema
        conn = graph_entry["connections"][0]
        assert "src_node_id" in conn
        assert "src_port_name" in conn
        assert "dst_node_id" in conn
        assert "dst_port_name" in conn
