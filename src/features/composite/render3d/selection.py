"""Selection — F3：对象与组件选择状态数据层（纯 Python）。

设计目标（master_plan §6 / §21.6）：
- 管理选择状态：object / point / edge / polygon 四种组件选择。
- 支持单选、加选（Shift）、减选（Alt）、反选、框选。
- 选择集可保存/命名（selection set），供 Deformer 的 SelectionField3D 用。
- 纯 Python，无 GL/PyQt 依赖，可独立单测。

与 viewport3d_state.ComponentMode 的关系：
- ComponentMode（OBJECT/POINT/EDGE/POLYGON）定义「当前选择模式」。
- 本模块的 SelectionState 存储「已选中的 id 集合」（object_ids/vertex_ids/...）。
- viewport3d_controller 点击后更新 SelectionState；viewport_overlays 据此高亮。

与 mesh_picking.py 的关系：
- mesh_picking 做 ray-triangle 求交，返回命中 triangle index。
- 本模块接收命中结果，按 component_mode 转成 vertex/edge/face id 并更新选择。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import numpy as np


# ──────────────────────────────────────────────────────────────────────
# 选择操作类型
# ──────────────────────────────────────────────────────────────────────

SELECT_SET = "set"       # 替换选择（默认，清除旧选择后设新选择）
SELECT_ADD = "add"       # 加选（Shift）
SELECT_SUBTRACT = "subtract"  # 减选（Alt/Option）
SELECT_TOGGLE = "toggle"  # 切换（有则删，无则加）


# ──────────────────────────────────────────────────────────────────────
# 选择状态
# ──────────────────────────────────────────────────────────────────────

@dataclass
class SelectionState:
    """当前选择状态（master_plan §21.6 F3-4）。

    按 component 类型分别存储选中 id。active_object 是最后选中的对象
   （transform gizmo 锚点用）。

    Attributes:
        object_ids: 选中的对象 node_id 集合（object mode）。
        vertex_ids: {mesh_node_id: set(vertex_index)}（point mode）。
        edge_ids: {mesh_node_id: set((v0,v1) edge)}（edge mode）。
        face_ids: {mesh_node_id: set(triangle_index)}（polygon mode）。
        active_object: 最后选中的对象 id（gizmo 锚点）。
    """
    object_ids: Set[str] = field(default_factory=set)
    vertex_ids: Dict[str, Set[int]] = field(default_factory=dict)
    edge_ids: Dict[str, Set[tuple]] = field(default_factory=dict)
    face_ids: Dict[str, Set[int]] = field(default_factory=dict)
    active_object: Optional[str] = None

    # ── 对象选择 ──

    def select_object(self, node_id: str, mode: str = SELECT_SET) -> None:
        """选择/加选/减选/切换一个对象。"""
        if mode == SELECT_SET:
            self.clear()
            self.object_ids.add(node_id)
            self.active_object = node_id
        elif mode == SELECT_ADD:
            self.object_ids.add(node_id)
            self.active_object = node_id
        elif mode == SELECT_SUBTRACT:
            self.object_ids.discard(node_id)
            if self.active_object == node_id:
                self.active_object = next(iter(self.object_ids), None)
        elif mode == SELECT_TOGGLE:
            if node_id in self.object_ids:
                self.object_ids.discard(node_id)
                if self.active_object == node_id:
                    self.active_object = next(iter(self.object_ids), None)
            else:
                self.object_ids.add(node_id)
                self.active_object = node_id

    def select_objects_box(self, node_ids: List[str], mode: str = SELECT_SET) -> None:
        """框选多个对象。"""
        target = set(node_ids)
        if mode == SELECT_SET:
            self.clear()
            self.object_ids.update(target)
            if target:
                self.active_object = next(iter(target))
        elif mode == SELECT_ADD:
            self.object_ids.update(target)
            if target:
                self.active_object = next(iter(target))
        elif mode == SELECT_SUBTRACT:
            self.object_ids -= target
            # 减选后若 active_object 已不在选集中，重置为剩余首个（与单选路径一致）
            if self.active_object is not None and self.active_object not in self.object_ids:
                self.active_object = next(iter(self.object_ids), None)

    # ── 组件选择（vertex/edge/face）──

    def select_vertex(self, mesh_id: str, vertex_idx: int,
                      mode: str = SELECT_SET) -> None:
        """选择单个顶点。"""
        if mode == SELECT_SET:
            self.vertex_ids.clear()
            self.edge_ids.clear()
            self.face_ids.clear()
        vs = self.vertex_ids.setdefault(mesh_id, set())
        if mode == SELECT_SUBTRACT:
            vs.discard(vertex_idx)
        elif mode == SELECT_TOGGLE:
            if vertex_idx in vs:
                vs.discard(vertex_idx)
            else:
                vs.add(vertex_idx)
        else:
            vs.add(vertex_idx)
        self.active_object = mesh_id

    def select_face(self, mesh_id: str, face_idx: int,
                    mode: str = SELECT_SET) -> None:
        """选择单个面（triangle index）。"""
        if mode == SELECT_SET:
            self.vertex_ids.clear()
            self.edge_ids.clear()
            self.face_ids.clear()
        fs = self.face_ids.setdefault(mesh_id, set())
        if mode == SELECT_SUBTRACT:
            fs.discard(face_idx)
        elif mode == SELECT_TOGGLE:
            if face_idx in fs:
                fs.discard(face_idx)
            else:
                fs.add(face_idx)
        else:
            fs.add(face_idx)
        self.active_object = mesh_id

    def select_edge(self, mesh_id: str, v0: int, v1: int,
                    mode: str = SELECT_SET) -> None:
        """选择单条边（规范化为 (min,max)）。"""
        if mode == SELECT_SET:
            self.vertex_ids.clear()
            self.edge_ids.clear()
            self.face_ids.clear()
        edge = (min(v0, v1), max(v0, v1))
        es = self.edge_ids.setdefault(mesh_id, set())
        if mode == SELECT_SUBTRACT:
            es.discard(edge)
        elif mode == SELECT_TOGGLE:
            if edge in es:
                es.discard(edge)
            else:
                es.add(edge)
        else:
            es.add(edge)
        self.active_object = mesh_id

    # ── 查询 ──

    def is_empty(self) -> bool:
        return (not self.object_ids and not self.vertex_ids
                and not self.edge_ids and not self.face_ids)

    def selected_vertex_count(self, mesh_id: str) -> int:
        return len(self.vertex_ids.get(mesh_id, set()))

    def selected_face_count(self, mesh_id: str) -> int:
        return len(self.face_ids.get(mesh_id, set()))

    def selected_edge_count(self, mesh_id: str) -> int:
        return len(self.edge_ids.get(mesh_id, set()))

    def get_vertex_mask(self, mesh_id: str, vertex_count: int) -> np.ndarray:
        """返回 (vertex_count,) bool 掩码（SelectionField3D / Deformer 用）。

        选中顶点 True，其余 False。
        """
        mask = np.zeros(vertex_count, dtype=bool)
        for vi in self.vertex_ids.get(mesh_id, set()):
            if 0 <= vi < vertex_count:
                mask[vi] = True
        return mask

    def get_face_mask(self, mesh_id: str, face_count: int) -> np.ndarray:
        """返回 (face_count,) bool 面掩码。"""
        mask = np.zeros(face_count, dtype=bool)
        for fi in self.face_ids.get(mesh_id, set()):
            if 0 <= fi < face_count:
                mask[fi] = True
        return mask

    # ── 清空 ──

    def clear(self) -> None:
        """清空所有选择。"""
        self.object_ids.clear()
        self.vertex_ids.clear()
        self.edge_ids.clear()
        self.face_ids.clear()
        self.active_object = None

    def clear_components(self) -> None:
        """只清空组件选择（保留对象选择）。"""
        self.vertex_ids.clear()
        self.edge_ids.clear()
        self.face_ids.clear()

    def invert_objects(self, all_object_ids: List[str]) -> None:
        """反选对象（在 all_object_ids 范围内）。"""
        all_set = set(all_object_ids)
        self.object_ids = all_set - self.object_ids
        self.active_object = next(iter(self.object_ids), None)

    def signature(self) -> tuple:
        """选择签名（触发 selection overlay 重绘用）。"""
        return (
            tuple(sorted(self.object_ids)),
            tuple((k, tuple(sorted(v))) for k, v in sorted(self.vertex_ids.items())),
            tuple((k, tuple(sorted(v))) for k, v in sorted(self.edge_ids.items())),
            tuple((k, tuple(sorted(v))) for k, v in sorted(self.face_ids.items())),
            self.active_object,
        )


# ──────────────────────────────────────────────────────────────────────
# 选择集（命名保存）
# ──────────────────────────────────────────────────────────────────────

@dataclass
class SelectionSet:
    """命名的选择集（master_plan §6.1 selection set 可保存）。

    可保存顶点/边/面选择，供 Deformer SelectionField3D 或后续批量操作用。

    Attributes:
        name: 选择集名。
        mesh_id: 关联的 mesh node id。
        vertex_ids: 选中的顶点索引。
        face_ids: 选中的面索引。
        edge_ids: 选中的边。
    """
    name: str
    mesh_id: str = ""
    vertex_ids: Set[int] = field(default_factory=set)
    face_ids: Set[int] = field(default_factory=set)
    edge_ids: Set[tuple] = field(default_factory=set)

    def is_empty(self) -> bool:
        return not self.vertex_ids and not self.face_ids and not self.edge_ids

    def vertex_mask(self, vertex_count: int) -> np.ndarray:
        mask = np.zeros(vertex_count, dtype=bool)
        for vi in self.vertex_ids:
            if 0 <= vi < vertex_count:
                mask[vi] = True
        return mask

    def from_selection_state(self, state: SelectionState, name: str,
                             mesh_id: str) -> "SelectionSet":
        """从 SelectionState 派生选择集。"""
        self.name = name
        self.mesh_id = mesh_id
        self.vertex_ids = set(state.vertex_ids.get(mesh_id, set()))
        self.face_ids = set(state.face_ids.get(mesh_id, set()))
        self.edge_ids = set(state.edge_ids.get(mesh_id, set()))
        return self


class SelectionSetLibrary:
    """选择集库（管理多个命名选择集）。"""

    def __init__(self) -> None:
        self._sets: Dict[str, SelectionSet] = {}

    def save(self, name: str, state: SelectionState, mesh_id: str) -> SelectionSet:
        """保存当前选择为新选择集。"""
        ss = SelectionSet(name=name, mesh_id=mesh_id)
        ss.from_selection_state(state, name, mesh_id)
        self._sets[name] = ss
        return ss

    def get(self, name: str) -> Optional[SelectionSet]:
        return self._sets.get(name)

    def names(self) -> List[str]:
        return list(self._sets.keys())

    def remove(self, name: str) -> None:
        self._sets.pop(name, None)

    def count(self) -> int:
        return len(self._sets)


def apply_component_hit(state: SelectionState, mesh_id: str,
                        component_mode: Any, hit: Any,
                        selection_mode: str = SELECT_SET) -> bool:
    """Apply a :class:`mesh_picking.ComponentHit` to ``SelectionState``.

    This adapter is intentionally duck-typed so the pure selection layer does
    not import the picking implementation (and does not pull in GL/Qt). It is
    the single mode-to-storage mapping used by future Viewer and Deformer
    integrations.
    """
    if state is None or hit is None or not bool(getattr(hit, "hit", False)):
        return False
    mode = str(getattr(component_mode, "value", component_mode)).lower()
    mode = {"face": "polygon", "vertex": "point"}.get(mode, mode)
    mesh_key = str(mesh_id)
    if mode == "point":
        vertex_index = getattr(hit, "vertex_index", None)
        if vertex_index is None:
            return False
        state.select_vertex(mesh_key, int(vertex_index), selection_mode)
    elif mode == "edge":
        edge = getattr(hit, "edge", None)
        if edge is None or len(edge) != 2:
            return False
        state.select_edge(mesh_key, int(edge[0]), int(edge[1]), selection_mode)
    elif mode == "polygon":
        triangle_index = getattr(hit, "triangle_index", -1)
        if int(triangle_index) < 0:
            return False
        state.select_face(mesh_key, int(triangle_index), selection_mode)
    else:
        return False
    return True
