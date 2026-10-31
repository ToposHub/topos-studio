"""
Comp Clip settings 数据层测试。

覆盖：
1. 空白 Comp Clip 的 constant 节点背景色正确写入分通道参数（color_r/g/b/a）
   —— 回归此前 "color: [...]" 列表格式与 ConstantNode 读取方式不匹配的 bug。
2. create_empty_comp_clip 透传 background_color。
3. update_comp_clip 同步更新 CompClipData / MediaAsset / CompositeGraphData
   及 constant 节点的 width/height/color_*。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.project import EditProject
from src.features.edit.core.comp_clip_data import CompClipData
from src.features.edit.core.composite_graph_data import CompositeGraphData
from src.features.edit.core.composite_graph_data import PROJECT_BACKGROUND_PARAM
from src.features.composite.nodes.constant_node import ConstantNode


# ================================================================
# Fixtures
# ================================================================


def _make_project() -> EditProject:
    return EditProject.create("Test Comp Clip Settings")


def _get_constant_node(graph_data: dict) -> dict | None:
    """从合成图数据中找出 constant 节点。"""
    for node in graph_data.get("nodes", []):
        if node.get("type") == "constant":
            return node
    return None


# ================================================================
# Test: create_empty_comp_clip background color
# ================================================================


class TestCreateEmptyCompClipBackgroundColor:
    """create_empty_comp_clip 正确设置 constant 节点背景色。"""

    def test_default_color_is_opaque_white(self):
        """默认背景色为不透明白色 (1,1,1,1)。"""
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip()

        graph = project.compositions[graph_id]
        const_node = _get_constant_node(graph)
        assert const_node is not None
        params = const_node["parameters"]
        assert params.get("color_r") == 1.0
        assert params.get("color_g") == 1.0
        assert params.get("color_b") == 1.0
        assert params.get("color_a") == 1.0

    def test_custom_background_color_applied(self):
        """自定义背景色写入 constant 节点分通道参数。"""
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip(
            background_color=(0.2, 0.4, 0.6, 0.8)
        )

        graph = project.compositions[graph_id]
        const_node = _get_constant_node(graph)
        params = const_node["parameters"]
        assert params.get("color_r") == 0.2
        assert params.get("color_g") == 0.4
        assert params.get("color_b") == 0.6
        assert params.get("color_a") == 0.8

    def test_no_legacy_color_list_format(self):
        """constant 节点不应再有旧式 "color": [...] 列表参数。"""
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip()

        graph = project.compositions[graph_id]
        const_node = _get_constant_node(graph)
        params = const_node["parameters"]
        assert "color" not in params, (
            "Legacy 'color' list param must be removed; ConstantNode reads "
            "color_r/g/b/a channels instead."
        )

    def test_constant_node_has_solid_mode(self):
        """constant 节点 mode 应为 solid。"""
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip()

        graph = project.compositions[graph_id]
        const_node = _get_constant_node(graph)
        assert const_node["parameters"].get("mode") == "solid"

    def test_background_constant_is_marked(self):
        """Project Settings can distinguish the default background Constant."""
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip()

        const_node = _get_constant_node(project.compositions[graph_id])
        assert const_node["parameters"][PROJECT_BACKGROUND_PARAM] is True

    def test_constant_primary_color_loads_created_background_color(self):
        """The background Constant UI must start from the clip's saved color."""
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip(
            background_color=(0.21, 0.42, 0.63, 0.84)
        )

        const_data = _get_constant_node(project.compositions[graph_id])
        constant = ConstantNode.from_dict(const_data)

        assert (
            constant.parameters["color_r"],
            constant.parameters["color_g"],
            constant.parameters["color_b"],
            constant.parameters["color_a"],
        ) == (0.21, 0.42, 0.63, 0.84)


# ================================================================
# Test: graph structure
# ================================================================


class TestEmptyCompClipGraphStructure:
    """空白 Comp Clip 图结构为 constant -> output。"""

    def test_two_nodes_constant_and_output(self):
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip()

        graph = project.compositions[graph_id]
        types = [n.get("type") for n in graph["nodes"]]
        assert "constant" in types
        assert "output" in types
        assert len(types) == 2

    def test_constant_connected_to_output(self):
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip()

        graph = project.compositions[graph_id]
        connections = graph["connections"]
        assert len(connections) == 1
        conn = connections[0]
        # verify src is constant, dst is output
        node_map = {n["id"]: n["type"] for n in graph["nodes"]}
        assert node_map[conn["src_node_id"]] == "constant"
        assert node_map[conn["dst_node_id"]] == "output"
        assert conn["src_port_name"] == "result"
        assert conn["dst_port_name"] == "image"


class TestOutputNodeResolutionAuthority:
    """Output 节点是合成分辨率的权威源（format_mode=custom）。"""

    def _get_output_node(self, graph_data: dict) -> dict | None:
        for node in graph_data.get("nodes", []):
            if node.get("type") == "output":
                return node
        return None

    def test_empty_comp_clip_output_is_custom_mode(self):
        """空白 Comp Clip 的 output 节点 format_mode 应为 custom。"""
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip()

        out = self._get_output_node(project.compositions[graph_id])
        assert out is not None
        assert out["parameters"].get("format_mode") == "custom"

    def test_empty_comp_clip_output_has_resolution(self):
        """空白 Comp Clip 的 output 节点带 width/height 参数。"""
        project = _make_project()
        project, _, _, graph_id = project.create_empty_comp_clip(
            width=2560, height=1440)

        out = self._get_output_node(project.compositions[graph_id])
        assert out["parameters"]["width"] == 2560
        assert out["parameters"]["height"] == 1440

    def test_update_comp_clip_sets_output_custom_mode(self):
        """update_comp_clip 改分辨率时同步更新 output 节点。"""
        import dataclasses as _dc
        project = _make_project()
        project, comp_clip_id, _, graph_id = \
            project.create_empty_comp_clip()

        # Manually reset output to input mode (simulate legacy graph)
        graph = dict(project.compositions[graph_id])
        nodes = list(graph["nodes"])
        for node in nodes:
            if node.get("type") == "output":
                node["parameters"] = {}
        graph["nodes"] = nodes
        project = _dc.replace(
            project,
            compositions={**project.compositions, graph_id: graph})

        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id, width=3840, height=2160)

        out = self._get_output_node(new_project.compositions[graph_id])
        assert out["parameters"]["format_mode"] == "custom"
        assert out["parameters"]["width"] == 3840
        assert out["parameters"]["height"] == 2160


# ================================================================
# Test: update_comp_clip
# ================================================================


class TestUpdateCompClip:
    """update_comp_clip 同步更新所有关联数据。"""

    def test_update_resolution(self):
        project = _make_project()
        project, comp_clip_id, comp_asset_id, graph_id = \
            project.create_empty_comp_clip()

        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id,
            width=3840,
            height=2160,
        )

        # CompClipData updated
        cc = CompClipData.from_dict(new_project.comp_clips[comp_clip_id])
        assert cc.width == 3840
        assert cc.height == 2160

        # MediaAsset metadata updated
        asset = new_project.assets[comp_asset_id]
        assert asset.metadata["width"] == 3840
        assert asset.metadata["height"] == 2160

        # Graph level resolution updated
        graph = new_project.compositions[graph_id]
        assert graph["width"] == 3840
        assert graph["height"] == 2160

        # Constant node width/height synced
        const_node = _get_constant_node(graph)
        assert const_node["parameters"]["width"] == 3840
        assert const_node["parameters"]["height"] == 2160

    def test_update_fps_and_duration(self):
        project = _make_project()
        project, comp_clip_id, _, _ = project.create_empty_comp_clip()

        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id,
            fps=30.0,
            duration_frames=300,
        )

        cc = CompClipData.from_dict(new_project.comp_clips[comp_clip_id])
        assert cc.fps == 30.0
        assert cc.duration_frames == 300

    def test_update_background_color(self):
        project = _make_project()
        project, comp_clip_id, _, graph_id = \
            project.create_empty_comp_clip()

        new_color = (0.1, 0.2, 0.3, 1.0)
        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id,
            background_color=new_color,
        )

        graph = new_project.compositions[graph_id]
        const_node = _get_constant_node(graph)
        params = const_node["parameters"]
        assert params["color_r"] == 0.1
        assert params["color_g"] == 0.2
        assert params["color_b"] == 0.3
        assert params["color_a"] == 1.0

    def test_update_name(self):
        project = _make_project()
        project, comp_clip_id, comp_asset_id, graph_id = \
            project.create_empty_comp_clip()

        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id,
            name="Renamed Comp",
        )

        cc = CompClipData.from_dict(new_project.comp_clips[comp_clip_id])
        assert cc.name == "Renamed Comp"

        asset = new_project.assets[comp_asset_id]
        assert asset.display_name == "Renamed Comp"

    def test_update_does_not_mutate_original(self):
        """frozen 不可变：原 project 不受影响。"""
        project = _make_project()
        project, comp_clip_id, _, _ = project.create_empty_comp_clip()

        original_cc = CompClipData.from_dict(
            project.comp_clips[comp_clip_id])
        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id,
            width=3840,
            height=2160,
        )

        # original project unchanged
        cc_after = CompClipData.from_dict(
            project.comp_clips[comp_clip_id])
        assert cc_after.width == original_cc.width
        assert cc_after.width == 1920  # default
        # new project reflects update
        new_cc = CompClipData.from_dict(
            new_project.comp_clips[comp_clip_id])
        assert new_cc.width == 3840

    def test_update_nonexistent_comp_clip_returns_self(self):
        """不存在的 comp_clip_id 返回原 project。"""
        project = _make_project()
        result = project.update_comp_clip(
            comp_clip_id="nonexistent",
            width=3840,
        )
        assert result is project

    def test_update_preserves_other_nodes(self):
        """更新分辨率/背景色只改 constant 节点，不影响其他节点。"""
        project = _make_project()
        project, comp_clip_id, _, graph_id = \
            project.create_empty_comp_clip()

        # Build a graph with an extra node and store it directly via
        # CompositeGraphData (avoid update_composition which has an
        # unrelated field-drop bug for comp_clips).
        from src.features.edit.core.composite_graph_data import (
            CompositeGraphData,
        )
        graph = project.compositions[graph_id]
        gd = CompositeGraphData.from_dict(graph)
        extra_nodes = list(gd.nodes) + [{
            "id": "extra-node",
            "type": "blur",
            "position": [600, 200],
            "parameters": {"radius": 5},
            "disabled": False,
        }]
        gd = gd.with_nodes(extra_nodes)
        # Store the modified graph back into compositions dict manually.
        new_compositions = dict(project.compositions)
        new_compositions[graph_id] = gd.to_dict()
        import dataclasses as _dc
        project = _dc.replace(project, compositions=new_compositions)

        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id,
            width=1280,
            height=720,
        )

        new_graph = new_project.compositions[graph_id]
        types = [n.get("type") for n in new_graph["nodes"]]
        assert "blur" in types  # extra node preserved
        const_node = _get_constant_node(new_graph)
        assert const_node["parameters"]["width"] == 1280  # constant updated

    def test_update_background_does_not_recolor_added_constant(self):
        """Background settings must not overwrite other Constant generators."""
        project = _make_project()
        project, comp_clip_id, _, graph_id = project.create_empty_comp_clip()

        graph = dict(project.compositions[graph_id])
        graph["nodes"] = list(graph["nodes"]) + [{
            "id": "foreground-constant",
            "type": "constant",
            "position": [100, 400],
            "parameters": {
                "mode": "solid",
                "color_r": 1.0,
                "color_g": 0.0,
                "color_b": 0.0,
                "color_a": 1.0,
            },
            "disabled": False,
        }]
        import dataclasses as _dc
        project = _dc.replace(
            project,
            compositions={**project.compositions, graph_id: graph},
        )

        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id,
            background_color=(0.1, 0.2, 0.3, 1.0),
        )

        new_graph = new_project.compositions[graph_id]
        added = next(
            node for node in new_graph["nodes"]
            if node.get("id") == "foreground-constant"
        )
        assert added["parameters"]["color_r"] == 1.0
        assert added["parameters"]["color_g"] == 0.0
        assert added["parameters"]["color_b"] == 0.0
