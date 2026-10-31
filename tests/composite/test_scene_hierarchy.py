"""F1 Scene Hierarchy / Outliner 单测（master_plan §4 / §21.4）。

覆盖：
1. build_hierarchy：从 SceneGraphRoot 生成层级摘要树。
2. SceneHierarchyItem：节点类型、计数累加、find_by_id、collect_mesh_ids。
3. 稳定 node_id：同一导入路径+节点路径 → 相同 id。
4. compute_hierarchy_stats：统计摘要（mesh/tri/mat/tex/skin/anim）。

纯 Python / numpy，无 GL/PyQt 依赖。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.render3d import scene_graph as sg
from src.features.composite.render3d import primitives as prim
from src.features.composite.render3d.scene_hierarchy import (
    SceneHierarchyItem, HierarchyStats,
    build_hierarchy, compute_hierarchy_stats, _make_stable_id,
    NODE_TYPE_ROOT, NODE_TYPE_MESH, NODE_TYPE_GROUP, NODE_TYPE_SKINNED,
    NODE_TYPE_EMPTY,
)

pytestmark = pytest.mark.composite_core


def _make_scene_with_meshes():
    """造一个含 2 mesh + 1 group 的场景树。"""
    root = sg.SceneNode(name="root")
    mat = sg.MaterialData()
    mat.name = "mat0"
    mat.base_color_tex.path = "/bc.png"
    m1 = sg.SceneNode(name="body", mesh=prim.cube(1.0), material=mat)
    m2 = sg.SceneNode(name="head", mesh=prim.sphere(1.0, 8, 4))
    group = sg.SceneNode(name="parts")
    group.children.append(m2)
    root.children.append(m1)
    root.children.append(group)
    return sg.SceneGraphRoot(root=root)


# ── build_hierarchy ──────────────────────────────────────────────────

class TestBuildHierarchy:
    def test_root_item(self):
        root_data = _make_scene_with_meshes()
        h = build_hierarchy(root_data, source_path="/m.glb")
        assert h.node_type == NODE_TYPE_ROOT
        assert h.name == "root"
        assert len(h.children) == 2

    def test_mesh_item_counts(self):
        root_data = _make_scene_with_meshes()
        h = build_hierarchy(root_data, source_path="/m.glb")
        body = h.children[0]
        assert body.node_type == NODE_TYPE_MESH
        assert body.name == "body"
        assert body.vertex_count == 24  # cube
        assert body.triangle_count == 12
        assert body.material_name == "mat0"
        assert body.has_texture is True

    def test_group_item(self):
        root_data = _make_scene_with_meshes()
        h = build_hierarchy(root_data, source_path="/m.glb")
        parts = h.children[1]
        assert parts.node_type == NODE_TYPE_GROUP
        assert len(parts.children) == 1
        head = parts.children[0]
        assert head.node_type == NODE_TYPE_MESH

    def test_stable_id_same_source(self):
        """同一 source_path + 节点路径 → 相同 node_id。"""
        root_data = _make_scene_with_meshes()
        h1 = build_hierarchy(root_data, source_path="/m.glb")
        h2 = build_hierarchy(root_data, source_path="/m.glb")
        assert h1.children[0].node_id == h2.children[0].node_id

    def test_stable_id_differs_per_source(self):
        """不同 source_path → 不同 node_id。"""
        root_data = _make_scene_with_meshes()
        h1 = build_hierarchy(root_data, source_path="/a.glb")
        h2 = build_hierarchy(root_data, source_path="/b.glb")
        assert h1.children[0].node_id != h2.children[0].node_id

    def test_bounds_populated(self):
        root_data = _make_scene_with_meshes()
        h = build_hierarchy(root_data, source_path="/m.glb")
        body = h.children[0]
        assert body.bounds_min is not None
        assert body.bounds_max is not None
        assert len(body.bounds_min) == 3

    def test_mesh_node_keeps_children(self):
        """mesh 节点有子节点时不应丢弃子节点（回归 CRITICAL）。

        之前 mesh 分支提前 return，子节点全部消失。
        """
        root = sg.SceneNode(name="root")
        # mesh 节点带子 mesh（attachment/LOD 场景）
        parent_mesh = sg.SceneNode(name="parent_mesh", mesh=prim.cube(1.0))
        child_mesh = sg.SceneNode(name="child_mesh", mesh=prim.sphere(0.5, 6, 4))
        parent_mesh.children.append(child_mesh)
        root.children.append(parent_mesh)
        root_data = sg.SceneGraphRoot(root=root)

        h = build_hierarchy(root_data, source_path="/m.glb")
        pm = h.children[0]
        assert pm.node_type == NODE_TYPE_MESH
        # 关键：子节点不应丢失
        assert len(pm.children) == 1
        assert pm.children[0].name == "child_mesh"
        assert pm.children[0].node_type == NODE_TYPE_MESH


# ── SceneHierarchyItem 方法 ──────────────────────────────────────────

class TestSceneHierarchyItem:
    def test_total_counts_recursive(self):
        root_data = _make_scene_with_meshes()
        h = build_hierarchy(root_data, source_path="/m.glb")
        # 2 mesh：cube(24 verts) + sphere(45 verts @ 8x4)
        assert h.total_mesh_count() == 2
        assert h.total_vertex_count() == 24 + h.children[1].children[0].vertex_count
        assert h.total_triangle_count() > 0

    def test_find_by_id(self):
        root_data = _make_scene_with_meshes()
        h = build_hierarchy(root_data, source_path="/m.glb")
        body = h.children[0]
        found = h.find_by_id(body.node_id)
        assert found is not None
        assert found.name == "body"
        assert h.find_by_id("nonexistent") is None

    def test_collect_mesh_ids(self):
        root_data = _make_scene_with_meshes()
        h = build_hierarchy(root_data, source_path="/m.glb")
        ids = h.collect_mesh_ids()
        assert len(ids) == 2

    def test_empty_item(self):
        item = SceneHierarchyItem(node_id="x", name="empty")
        assert item.node_type == NODE_TYPE_EMPTY
        assert item.total_mesh_count() == 0


# ── compute_hierarchy_stats ──────────────────────────────────────────

class TestHierarchyStats:
    def test_basic_stats(self):
        root_data = _make_scene_with_meshes()
        stats = compute_hierarchy_stats(root_data)
        assert stats.mesh_count == 2
        assert stats.triangle_count > 0
        assert stats.vertex_count > 0
        assert stats.material_count == 1  # 去重后
        assert stats.texture_count == 1   # body 有 base_color_tex

    def test_stats_with_skeleton(self):
        skin = sg.SkinData(
            bone_names=["b0", "b1"],
            inverse_bind_matrices=np.tile(np.eye(4, dtype=np.float32), (2, 1, 1)),
            bone_indices=np.zeros((4, 4), dtype=np.uint32),
            bone_weights=np.zeros((4, 4), dtype=np.float32),
        )
        node = sg.SceneNode(name="m", mesh=prim.cube(1.0), skin=skin)
        root_data = sg.SceneGraphRoot(root=node, skins=[skin])
        stats = compute_hierarchy_stats(root_data)
        assert stats.skeleton_count == 1
        assert stats.bone_count == 2

    def test_stats_with_animation(self):
        anim = sg.AnimationClip(name="idle", duration=1.0)
        node = sg.SceneNode(name="m", mesh=prim.cube(1.0))
        root_data = sg.SceneGraphRoot(root=node, animations=[anim])
        stats = compute_hierarchy_stats(root_data)
        assert stats.animation_clip_count == 1

    def test_stats_to_dict(self):
        root_data = _make_scene_with_meshes()
        stats = compute_hierarchy_stats(root_data)
        d = stats.to_dict()
        assert "mesh_count" in d
        assert "triangle_count" in d
        assert d["mesh_count"] == 2

    def test_stats_camera_light_from_report(self):
        """camera/light 数量从 import_report 取（F0 联动）。"""
        from src.features.composite.render3d.model_import_report import ModelImportReport
        node = sg.SceneNode(name="m", mesh=prim.cube(1.0))
        root_data = sg.SceneGraphRoot(root=node)
        report = ModelImportReport(camera_count=2, light_count=3)
        root_data.import_report = report
        stats = compute_hierarchy_stats(root_data)
        assert stats.camera_count == 2
        assert stats.light_count == 3


# ── _make_stable_id ──────────────────────────────────────────────────

class TestStableId:
    def test_same_input_same_id(self):
        id1 = _make_stable_id("/a.glb", "/body")
        id2 = _make_stable_id("/a.glb", "/body")
        assert id1 == id2

    def test_diff_path_diff_id(self):
        id1 = _make_stable_id("/a.glb", "/body")
        id2 = _make_stable_id("/a.glb", "/head")
        assert id1 != id2

    def test_diff_source_diff_id(self):
        id1 = _make_stable_id("/a.glb", "/body")
        id2 = _make_stable_id("/b.glb", "/body")
        assert id1 != id2

    def test_id_length(self):
        """稳定 id 长度固定（12 字符 md5 截断）。"""
        assert len(_make_stable_id("/x", "/y")) == 12
