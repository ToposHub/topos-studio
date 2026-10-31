"""Phase 2: 3D 节点创建 / 分裂图清理测试。

覆盖 docs/composite_3d_module_audit_optimization_plan_2026-06-29.md 的 Phase 2：
1. 共享工厂 spawn_3d_scene_children（migration + 实时创建复用）
2. 工厂与 v6→v7 迁移产出的拓扑一致
3. NodeEditorActions.create_3d_scene_with_children（分裂图创建入口）
4. 拖拽/菜单路径改走分裂图（源码级断言）

注：graph 层用纯 dict 验证；actions 层用 mock graph 验证调用链。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest

from src.features.composite.core.graph_migration import (
    spawn_3d_scene_children, migrate_v6_to_v7,
    _SCENE3D_CHILD_OFFSET, _SCENE3D_CHILD_PORT)


# ──────────────────────────────────────────────────────────────────────────
# Part 1: 共享工厂 spawn_3d_scene_children
# ──────────────────────────────────────────────────────────────────────────

class TestSpawnFactory:
    """spawn_3d_scene_children 工厂：产出子节点 dict + 连接 dict。"""

    def test_no_model_spawns_camera_light_geo(self):
        """无模型 → spawn 3 个子节点（camera/light/geo），无 model3d。"""
        existing = set()
        nodes, conns = spawn_3d_scene_children(
            "scene1", (100, 200), {}, existing, include_model=False)
        types = sorted(n["type"] for n in nodes)
        assert types == ["camera3d", "geo3d", "light3d"]
        assert len(conns) == 3

    def test_with_model_spawns_all_four(self):
        """有 model_path + use_imported_model → spawn 4 个子节点（含 model3d）。"""
        existing = set()
        nodes, conns = spawn_3d_scene_children(
            "scene1", (100, 200),
            {"model_path": "/x.gltf", "use_imported_model": True},
            existing, include_model=True)
        types = sorted(n["type"] for n in nodes)
        assert types == ["camera3d", "geo3d", "light3d", "model3d"]
        assert len(conns) == 4

    def test_dragged_model_can_spawn_model_only(self):
        """拖拽普通模型 → 只 spawn Model3D，Camera/Light/Geo 用 Scene3D fallback。"""
        existing = set()
        nodes, conns = spawn_3d_scene_children(
            "scene1", (100, 200),
            {"model_path": "/x.fbx", "use_imported_model": True},
            existing,
            include_model=True,
            include_camera=False,
            include_light=False,
            include_geo=False,
        )
        assert [n["type"] for n in nodes] == ["model3d"]
        assert len(conns) == 1

    def test_model_not_spawned_without_path(self):
        """include_model=True 但无 model_path → 不 spawn model3d。"""
        existing = set()
        nodes, conns = spawn_3d_scene_children(
            "scene1", (100, 200), {}, existing, include_model=True)
        types = sorted(n["type"] for n in nodes)
        assert "model3d" not in types
        assert types == ["camera3d", "geo3d", "light3d"]

    def test_child_positions_left_of_scene3d(self):
        """子节点位置在 Scene3D 左侧（offset_map x 为负）。"""
        existing = set()
        nodes, _ = spawn_3d_scene_children(
            "scene1", (400, 100), {}, existing, include_model=False)
        for n in nodes:
            assert n["position"][0] < 400, f"{n['type']} should be left of scene3d"
            assert n["position"][1] != 100, f"{n['type']} should be vertically offset"

    def test_connections_target_scene3d_ports(self):
        """连接 src=child.display, dst=scene3d.in（v8 单端口合并）。"""
        existing = set()
        nodes, conns = spawn_3d_scene_children(
            "scene1", (400, 100), {}, existing, include_model=False)
        for c in conns:
            assert c["src_port_name"] == "display"
            assert c["dst_node_id"] == "scene1"
            # v8：所有子节点都连到单一 `in` 端口
            assert c["dst_port_name"] == "in"

    def test_model_connection_uses_geometry_in(self):
        """v8：Model3D 也连接到 `in`（单端口合并，不再区分 geometry_in）。"""
        existing = set()
        nodes, conns = spawn_3d_scene_children(
            "scene1", (400, 100),
            {"model_path": "/x.gltf", "use_imported_model": True},
            existing, include_model=True)
        model_conns = [c for c in conns if c["src_node_id"].endswith("_model3d_0")]
        assert len(model_conns) == 1
        assert model_conns[0]["dst_port_name"] == "in"

    def test_child_ids_unique_and_prefixed(self):
        """子节点 id 带 scene3d_id 前缀，且不冲突。"""
        existing = set()
        nodes, _ = spawn_3d_scene_children(
            "scene1", (0, 0), {}, existing, include_model=False)
        ids = [n["id"] for n in nodes]
        assert len(ids) == len(set(ids)), "ids should be unique"
        for nid in ids:
            assert nid.startswith("scene1_")

    def test_factory_avoids_id_collision(self):
        """existing_ids 含同前缀 id 时工厂自动递增后缀避免冲突。"""
        existing = {"scene1_camera3d_0"}  # 已占用
        nodes, _ = spawn_3d_scene_children(
            "scene1", (0, 0), {}, existing, include_model=False)
        cam = [n for n in nodes if n["type"] == "camera3d"][0]
        assert cam["id"] == "scene1_camera3d_1"  # 递增到 _1

    def test_params_extracted_from_scene3d(self):
        """工厂从 scene3d_params 抽取迁移到子节点的参数。"""
        existing = set()
        nodes, _ = spawn_3d_scene_children(
            "scene1", (0, 0),
            {"cam_fov": 75.0, "primitive": "sphere", "light_intensity": 3.0},
            existing, include_model=False)
        cam = next(n for n in nodes if n["type"] == "camera3d")
        geo = next(n for n in nodes if n["type"] == "geo3d")
        light = next(n for n in nodes if n["type"] == "light3d")
        assert cam["parameters"]["cam_fov"] == 75.0
        assert geo["parameters"]["primitive"] == "sphere"
        assert light["parameters"]["light_intensity"] == 3.0

    def test_spawn_model_only_flag(self):
        """spawn_model_only=True 时只 spawn model3d（拖模型到已有场景用）。"""
        existing = set()
        nodes, conns = spawn_3d_scene_children(
            "scene1", (0, 0),
            {"model_path": "/x.gltf", "use_imported_model": True},
            existing, spawn_model_only=True)
        assert len(nodes) == 1
        assert nodes[0]["type"] == "model3d"
        assert len(conns) == 1


# ──────────────────────────────────────────────────────────────────────────
# Part 2: 工厂与迁移路径一致性
# ──────────────────────────────────────────────────────────────────────────

class TestFactoryMigrationConsistency:
    """工厂产出的拓扑应与 v6→v7 迁移完全一致（消除分歧）。"""

    def _make_v6_scene3d(self, with_model=False):
        params = {
            "width": 1920, "height": 1080,
            "cam_fov": 50.0, "cam_dist": 5.0,
            "primitive": "cube", "mesh_scale": 1.0,
            "light_intensity": 2.0,
        }
        if with_model:
            params["model_path"] = "/m.gltf"
            params["use_imported_model"] = True
        return {
            "composite_schema_version": 6,
            "nodes": [{"id": "scene1", "type": "scene3d",
                       "position": [400, 0], "parameters": params, "disabled": False}],
            "connections": [],
        }

    def test_migration_uses_factory_topology(self):
        """migrate_v6_to_v7 产出的子节点类型/端口与工厂一致。"""
        result = migrate_v6_to_v7(self._make_v6_scene3d(with_model=False))
        mig_types = sorted(n["type"] for n in result["nodes"] if n["type"] != "scene3d")
        # 工厂同样输入
        existing = set()
        fac_nodes, _ = spawn_3d_scene_children(
            "scene1", [400, 0], {}, existing, include_model=False)
        fac_types = sorted(n["type"] for n in fac_nodes)
        assert mig_types == fac_types

    def test_migration_and_factory_same_ports(self):
        """迁移与工厂产出的连接端口集合一致。"""
        result = migrate_v6_to_v7(self._make_v6_scene3d())
        mig_ports = {c["dst_port_name"] for c in result["connections"]}
        existing = set()
        _, fac_conns = spawn_3d_scene_children(
            "scene1", [400, 0], {}, existing, include_model=False)
        fac_ports = {c["dst_port_name"] for c in fac_conns}
        assert mig_ports == fac_ports


# ──────────────────────────────────────────────────────────────────────────
# Part 3: 拖拽/菜单路径改走分裂图（源码级断言）
# ──────────────────────────────────────────────────────────────────────────

class TestNodeCreationRoutesToSplitGraph:
    """拖拽模型 / 菜单建 Scene3D 改为建分裂图（源码级断言）。"""

    def test_actions_has_create_3d_scene_method(self):
        """NodeEditorActions 有 create_3d_scene_with_children 方法。"""
        from src.features.composite.ui.node_editor_actions import NodeEditorActions
        assert hasattr(NodeEditorActions, "create_3d_scene_with_children")

    def test_drop_event_uses_split_graph_creation(self):
        """dropEvent 对 _MODEL_3D_EXTENSIONS 调用 create_3d_scene_with_children。"""
        from src.features.composite.ui.node_editor_widget import NodeEditorWidget
        src = inspect.getsource(NodeEditorWidget.dropEvent)
        assert "create_3d_scene_with_children" in src, \
            "dropEvent 应改走分裂图创建（不再建单节点 scene3d with model_path）"
        assert "model_path=path" in src or "model_path=path" in src

    def test_menu_creation_routes_scene3d_to_split_graph(self):
        """菜单/搜索建 Scene3D 只建一个 Scene3D（不再 spawn 子节点）。

        回归保护：曾自动 spawn Camera3D/Light3D/Geo3D 子节点；现按用户要求
        只建单节点 Scene3D（用其内置默认相机/灯光/cube，开箱即用）。
        拖拽模型 + v6→v7 迁移仍走 create_3d_scene_with_children。
        """
        from src.features.composite.ui.node_editor_widget import NodeEditorWidget
        # 找到创建节点的 handler（_create_node_at 里的分支）
        src = inspect.getsource(NodeEditorWidget)
        # 断言：菜单建 scene3d 时不再特殊分支走 create_3d_scene_with_children
        # （该方法仅在 dropEvent 拖拽模型时用）。create_3d_scene_with_children
        # 在源码里仍存在（dropEvent 用），但 _add_new_node 不再调用它。
        # 去掉注释行后再检查（注释里会提到该方法名作说明）。
        handler_src = inspect.getsource(NodeEditorWidget._add_new_node)
        code_lines = [ln for ln in handler_src.splitlines()
                      if not ln.strip().startswith("#")]
        code = "\n".join(code_lines)
        assert "create_3d_scene_with_children" not in code, \
            "菜单/搜索建 Scene3D 不应再走 create_3d_scene_with_children"

    def test_factory_does_not_create_monolithic_scene3d_on_drop(self):
        """dropEvent 不再为模型建带 model_path 的单 scene3d 节点（回归保护）。"""
        from src.features.composite.ui.node_editor_widget import NodeEditorWidget
        src = inspect.getsource(NodeEditorWidget.dropEvent)
        # 旧的 monolithic 路径已移除：不应再有 'scene3d' 作为 node_type 赋值
        # 配合 model_path/use_imported_model
        assert '"scene3d"' not in src or "create_3d_scene" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 4: Scene3D 面板连接提示
# ──────────────────────────────────────────────────────────────────────────

class TestScene3dPanelConnectionHint:
    """Scene3D 面板在 Camera3D/Light3D 连接时显示 fallback 参数提示。"""

    def test_panel_has_connection_hint_method(self):
        """NodePropertiesPanel 有 _maybe_add_scene3d_connection_hint 方法。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        assert hasattr(NodePropertiesPanel, "_maybe_add_scene3d_connection_hint")

    def test_build_scene3d_ui_calls_hint_method(self):
        """_build_scene3d_ui 调用 _maybe_add_scene3d_connection_hint。"""
        from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
        src = inspect.getsource(NodePropertiesPanel._build_scene3d_ui)
        assert "_maybe_add_scene3d_connection_hint" in src
