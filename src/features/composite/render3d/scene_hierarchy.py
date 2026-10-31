# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Scene Hierarchy — F1：SceneGraphRoot → 轻量层级树摘要（纯 Python）。

设计目标（master_plan §4 / §21.4）：
- 把 SceneGraphRoot 的场景树转成 UI Outliner 可直接渲染的轻量摘要。
- 每个节点含稳定 id / name / type / children / mesh/material 统计 / 显隐锁。
- 不复制 GL 对象或 numpy 大数组，只做元数据摘要。
- 纯 Python，无 GL/PyQt 依赖，可独立单测。

与 scene_graph.py 的关系：
- SceneGraphRoot 是完整场景数据（含 mesh 顶点数组、skin、动画）。
- SceneHierarchyItem 是「摘要视图」（只含名称/类型/计数，给 UI 树显示）。
- build_hierarchy(root) 递归生成摘要树。

与 Model3DNode 的关系：
- Model3DNode 调用 build_hierarchy 把层级摘要放进 ModelPayload.hierarchy。
- Scene3D / Outliner 面板据此渲染树视图 + 选中同步。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# 节点类型常量（SceneHierarchyItem.node_type 取值）
NODE_TYPE_ROOT = "root"
NODE_TYPE_MESH = "mesh"
NODE_TYPE_GROUP = "group"       # 纯变换节点（无 mesh，有子节点）
NODE_TYPE_EMPTY = "empty"       # 空节点（无 mesh 无子节点）
NODE_TYPE_SKINNED = "skinned"   # 蒙皮 mesh（含 skin 数据）
NODE_TYPE_SKELETON = "skeleton"  # 骨骼根（F6 联动）
NODE_TYPE_BONE = "bone"          # 单根骨骼（F6 联动）
NODE_TYPE_CAMERA = "camera"     # 相机（F8 联动）
NODE_TYPE_LIGHT = "light"       # 灯光（F8 联动）


@dataclass
class SceneHierarchyItem:
    """层级树的一个节点摘要（master_plan §21.4 F1-2）。

    Attributes:
        node_id: 稳定唯一 id（导入路径 + 节点路径生成的 hash，选中同步用）。
        name: 节点名（UI 显示）。
        node_type: NODE_TYPE_* 常量。
        children: 子节点摘要列表。
        vertex_count: 该节点 mesh 的顶点数（非 mesh=0）。
        triangle_count: 该节点 mesh 的三角面数（非 mesh=0）。
        material_name: 该节点绑定的材质名（无材质=空）。
        has_texture: 该节点材质是否含贴图。
        visible: 显隐（viewport-only 状态，默认 True）。
        locked: 锁定（默认 False）。
        bounds_min: 该节点 mesh 的局部 AABB min（None=无 mesh）。
        bounds_max: 该节点 mesh 的局部 AABB max（None=无 mesh）。
    """
    node_id: str
    name: str
    node_type: str = NODE_TYPE_EMPTY
    children: List["SceneHierarchyItem"] = field(default_factory=list)
    vertex_count: int = 0
    triangle_count: int = 0
    material_name: str = ""
    has_texture: bool = False
    visible: bool = True
    locked: bool = False
    bounds_min: Optional[list] = None  # [x,y,z]
    bounds_max: Optional[list] = None

    def total_vertex_count(self) -> int:
        """该子树所有 mesh 顶点总和。"""
        return self.vertex_count + sum(c.total_vertex_count() for c in self.children)

    def total_triangle_count(self) -> int:
        """该子树所有 mesh 三角面总和。"""
        return self.triangle_count + sum(c.total_triangle_count() for c in self.children)

    def total_mesh_count(self) -> int:
        """该子树的 mesh 节点数。"""
        count = 1 if self.node_type in (NODE_TYPE_MESH, NODE_TYPE_SKINNED) else 0
        return count + sum(c.total_mesh_count() for c in self.children)

    def find_by_id(self, node_id: str) -> Optional["SceneHierarchyItem"]:
        """在子树中按 node_id 查找节点。"""
        if self.node_id == node_id:
            return self
        for c in self.children:
            found = c.find_by_id(node_id)
            if found is not None:
                return found
        return None

    def collect_mesh_ids(self) -> List[str]:
        """收集子树中所有 mesh 节点的 id（选中/显隐用）。"""
        ids = []
        if self.node_type in (NODE_TYPE_MESH, NODE_TYPE_SKINNED):
            ids.append(self.node_id)
        for c in self.children:
            ids.extend(c.collect_mesh_ids())
        return ids


# ──────────────────────────────────────────────────────────────────────
# 稳定 id 生成
# ──────────────────────────────────────────────────────────────────────

def _make_stable_id(source_path: str, node_path: str) -> str:
    """生成稳定节点 id（source_path + 节点路径 hash）。

    同一导入文件的同一节点路径 → 相同 id（跨会话稳定，选中/显隐持久化用）。
    """
    from .scene_graph import stable_scene_node_id
    return stable_scene_node_id(source_path, node_path)


# ──────────────────────────────────────────────────────────────────────
# 从 SceneGraphRoot 生成层级摘要
# ──────────────────────────────────────────────────────────────────────

def build_hierarchy(root_data, source_path: str = "") -> SceneHierarchyItem:
    """从 SceneGraphRoot 生成层级摘要树（master_plan §21.4 F1-2）。

    Args:
        root_data: scene_graph.SceneGraphRoot。
        source_path: 源文件路径（生成稳定 node_id 用）。

    Returns:
        根节点的 SceneHierarchyItem（递归含所有子节点摘要）。
    """
    # 延迟导入避免循环依赖
    from . import scene_graph as sg

    def _visit(node: sg.SceneNode, path: str) -> SceneHierarchyItem:
        node_path = f"{path}/{node.name}" if path else node.name
        node_id = _make_stable_id(source_path, node_path)
        # 先递归子节点（无论本节点是否有 mesh，子节点都应出现在树中）
        children_items = [_visit(c, node_path) for c in node.children]
        # 判断节点类型
        if node.mesh is not None:
            node_type = NODE_TYPE_SKINNED if node.skin is not None else NODE_TYPE_MESH
            mesh = node.mesh
            vc = int(mesh.vertices.shape[0])
            tc = int(mesh.indices.shape[0]) // 3
            mat_name = ""
            has_tex = False
            if node.material is not None:
                mat_name = getattr(node.material, "name", "") or ""
                if hasattr(node.material, "has_any_texture"):
                    has_tex = node.material.has_any_texture()
            # 局部 AABB
            bmin, bmax = None, None
            try:
                bmin = [float(x) for x in mesh.vertices.min(axis=0)]
                bmax = [float(x) for x in mesh.vertices.max(axis=0)]
            except Exception:
                pass
            return SceneHierarchyItem(
                node_id=node_id, name=node.name, node_type=node_type,
                vertex_count=vc, triangle_count=tc,
                material_name=mat_name, has_texture=has_tex,
                bounds_min=bmin, bounds_max=bmax,
                # mesh 节点也可能有子节点（attachment/LOD/装饰件），不能丢弃
                children=children_items,
            )
        # 非叶子节点（无 mesh）
        node_type = NODE_TYPE_GROUP if children_items else NODE_TYPE_EMPTY
        return SceneHierarchyItem(
            node_id=node_id, name=node.name, node_type=node_type,
            children=children_items,
        )

    root_item = _visit(root_data.root, "")
    root_item.node_type = NODE_TYPE_ROOT
    return root_item


def hierarchy_to_dict(item: Optional[SceneHierarchyItem]) -> Optional[Dict[str, Any]]:
    """Serialize hierarchy metadata for the UI/render-thread boundary."""
    if item is None:
        return None
    return {
        "node_id": item.node_id,
        "name": item.name,
        "node_type": item.node_type,
        "vertex_count": int(item.vertex_count),
        "triangle_count": int(item.triangle_count),
        "material_name": item.material_name,
        "has_texture": bool(item.has_texture),
        "visible": bool(item.visible),
        "locked": bool(item.locked),
        "bounds_min": list(item.bounds_min) if item.bounds_min is not None else None,
        "bounds_max": list(item.bounds_max) if item.bounds_max is not None else None,
        "children": [hierarchy_to_dict(child) for child in item.children],
    }


# ──────────────────────────────────────────────────────────────────────
# 层级统计摘要（Outliner footer 用，master_plan §4.2 F1-6）
# ──────────────────────────────────────────────────────────────────────

@dataclass
class HierarchyStats:
    """层级统计摘要（Outliner footer 显示）。"""
    mesh_count: int = 0
    triangle_count: int = 0
    vertex_count: int = 0
    material_count: int = 0
    texture_count: int = 0
    skeleton_count: int = 0
    bone_count: int = 0
    animation_clip_count: int = 0
    camera_count: int = 0
    light_count: int = 0

    def to_dict(self) -> dict:
        return {
            "mesh_count": self.mesh_count,
            "triangle_count": self.triangle_count,
            "vertex_count": self.vertex_count,
            "material_count": self.material_count,
            "texture_count": self.texture_count,
            "skeleton_count": self.skeleton_count,
            "bone_count": self.bone_count,
            "animation_clip_count": self.animation_clip_count,
            "camera_count": self.camera_count,
            "light_count": self.light_count,
        }


def compute_hierarchy_stats(root_data) -> HierarchyStats:
    """从 SceneGraphRoot 计算层级统计（master_plan §4.2 F1-6）。

    AUD-9120：删除从未被使用的 ``hierarchy`` 形参——原形参声称"可选预构建
    层级树（避免重复遍历）"，但函数体从不读取它（统计需要去重材质/纹理
    计数，层级树摘要不含这些维度，无法直接复用）。全仓库无调用方传该参数。

    Args:
        root_data: SceneGraphRoot（取 skins/animations/cameras/lights 顶层字段）。

    Returns:
        HierarchyStats。
    """
    from . import scene_graph as sg

    stats = HierarchyStats()
    material_names = set()
    texture_count = 0

    def _visit(node: sg.SceneNode) -> None:
        nonlocal texture_count
        if node.mesh is not None:
            stats.mesh_count += 1
            stats.vertex_count += int(node.mesh.vertices.shape[0])
            stats.triangle_count += int(node.mesh.indices.shape[0]) // 3
        if node.material is not None:
            stats.material_count += 1
            mat_name = getattr(node.material, "name", "") or f"mat_{stats.material_count}"
            material_names.add(mat_name)
            if hasattr(node.material, "has_any_texture") and node.material.has_any_texture():
                for t in (node.material.base_color_tex, node.material.normal_tex,
                          node.material.orm_tex, node.material.ao_tex,
                          node.material.emissive_tex):
                    if t.path:
                        texture_count += 1
        for c in node.children:
            _visit(c)

    _visit(root_data.root)
    stats.material_count = len(material_names)  # 去重材质
    stats.texture_count = texture_count
    stats.skeleton_count = len(root_data.skins)
    stats.bone_count = sum(s.bone_count for s in root_data.skins)
    stats.animation_clip_count = len(root_data.animations)
    # camera/light：F8 联动，当前多数 backend 不提取
    report = getattr(root_data, "import_report", None)
    if report is not None:
        stats.camera_count = getattr(report, "camera_count", 0)
        stats.light_count = getattr(report, "light_count", 0)
    return stats
