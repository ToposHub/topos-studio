"""
F6 骨架与蒙皮 —— SkeletonData + joint matrix 计算（纯 numpy，无 GL 依赖）。

设计目标：
- 表达 bone hierarchy（parent/children + rest pose local/world）。
- 从 glTF joints 节点树构建 SkeletonData（loader 调用）。
- compute_joint_matrices：给当前 pose 的 world matrices，算出 GPU 上传的
  joint_matrices[B] = current_world[b] @ inverse_bind[b]，供 skinning shader 用。
- build_skeleton_overlay：构建骨骼线段 + 关节点 buffer（skeleton_renderer 上传用）。
- build_weight_colors：把顶点 4 权重转成热力色（weight display mode 用）。

约定（与 scene_graph.py 一致）：
- 所有 4x4 矩阵为行主序 numpy float32，应用顺序 M @ v。
- world = parent_world @ local。

关联模块：
- scene_graph.py：SkinData 存 inverse_bind_matrices / bone_indices / bone_weights。
- model_loader.py：glTF skin 节点树 → SkeletonData（本模块的 from_gltf_joints）。
- scene3d_node.py / model3d_node.py：compute_joint_matrices 的结果上传到 u_bone_matrices。
- skeleton_renderer.py（F6-6）：build_skeleton_overlay 的 buffer 上传 GL 绘制。
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .scene_graph import (
    MAX_BONES,
    SkinData,
    mat4_identity,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# BoneData：单根骨骼（关节）
# ──────────────────────────────────────────────────────────────────────


@dataclass
class BoneData:
    """单根骨骼（关节）数据。

    Attributes:
        name: 骨骼名（glTF node name；可能重复，由 SkeletonData 统一化）。
        index: 在 SkeletonData.bones 数组中的下标（与 inverse_bind 行对应）。
        parent_index: 父骨骼下标（-1=根骨骼，无父）。
        rest_local: 4x4 相对父骨骼的 rest pose local matrix（绑定姿态）。
        rest_world: 4x4 rest pose world matrix（= 祖先 rest_local 链乘积）。
        children_indices: 子骨骼下标列表。
    """
    name: str = ""
    index: int = 0
    parent_index: int = -1
    rest_local: np.ndarray = field(default_factory=mat4_identity)
    rest_world: np.ndarray = field(default_factory=mat4_identity)
    children_indices: List[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rest_local = np.asarray(self.rest_local, dtype=np.float32).reshape(4, 4)
        self.rest_world = np.asarray(self.rest_world, dtype=np.float32).reshape(4, 4)


# ──────────────────────────────────────────────────────────────────────
# SkeletonData：整个骨架
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SkeletonData:
    """骨架数据（bone hierarchy + rest pose）。

    一个 SkeletonData 对应 glTF 的一个 skin。多个 mesh 可共享同一 skeleton。
    SkinData 的 bone_names 顺序与 SkeletonData.bones 的 index 对齐（loader 保证）。

    Attributes:
        name: 骨架名（glTF skin name；空=未命名）。
        bones: 骨骼列表（顺序 = glTF joints 顺序 = SkinData.bone_names 顺序）。
        root_indices: 根骨骼下标列表（parent_index == -1 的骨骼）。
    """
    name: str = ""
    bones: List[BoneData] = field(default_factory=list)
    root_indices: List[int] = field(default_factory=list)

    @property
    def bone_count(self) -> int:
        """骨骼总数。"""
        return len(self.bones)

    @property
    def is_empty(self) -> bool:
        """无骨骼（空骨架；不应发生，但防御性检查）。"""
        return len(self.bones) == 0

    def bone_index_by_name(self, name: str) -> int:
        """按名查骨骼下标（首个匹配；未找到返回 -1）。

        骨骼名可能重复（glTF 不保证唯一）；loader 应调用 unique_bone_names 统一化。
        """
        for i, b in enumerate(self.bones):
            if b.name == name:
                return i
        return -1

    def rest_world_matrices(self) -> np.ndarray:
        """收集所有骨骼的 rest_world → (B,4,4) float32（GPU 上传用）。"""
        if not self.bones:
            return np.zeros((0, 4, 4), dtype=np.float32)
        return np.stack([b.rest_world for b in self.bones]).astype(np.float32)


# ──────────────────────────────────────────────────────────────────────
# 构建：从 glTF joints 节点列表 → SkeletonData
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _JointNode:
    """glTF joint 的中间表示（loader 从 scene node 树抽取）。

    Attributes:
        name: 节点名。
        local_matrix: 4x4 rest pose local（相对父；TRS 或 matrix）。
        parent_joint_index: 父 joint 在 joints 数组中的下标（-1=根）。
    """
    name: str = ""
    local_matrix: np.ndarray = field(default_factory=mat4_identity)
    parent_joint_index: int = -1


def build_skeleton_from_joints(
    name: str,
    joints: List[_JointNode],
) -> SkeletonData:
    """从 glTF joints 列表构建 SkeletonData（计算 rest_world）。

    Args:
        name: skin 名。
        joints: glTF joints 列表（顺序 = inverse_bind_matrices 行顺序）。
            每个 joint 的 parent_joint_index 必须指向更早出现的 joint（DAG 无前向引用）。

    Returns:
        SkeletonData：bones 的 rest_world 由 parent.rest_world @ local 累乘得到。
        空列表返回空 SkeletonData（is_empty=True）。
    """
    skel = SkeletonData(name=name)
    if not joints:
        return skel

    bones: List[BoneData] = []
    for i, jn in enumerate(joints):
        rest_local = np.asarray(jn.local_matrix, dtype=np.float32).reshape(4, 4)
        if jn.parent_joint_index < 0:
            # 根骨骼：world = local
            rest_world = rest_local.copy()
            skel.root_indices.append(i)
        else:
            parent = bones[jn.parent_joint_index]
            parent.children_indices.append(i)
            rest_world = (parent.rest_world @ rest_local).astype(np.float32)
        bones.append(BoneData(
            name=jn.name,
            index=i,
            parent_index=jn.parent_joint_index,
            rest_local=rest_local,
            rest_world=rest_world,
        ))
    skel.bones = bones
    return skel


# ──────────────────────────────────────────────────────────────────────
# Joint matrix 计算：当前 pose → GPU 上传矩阵
# ──────────────────────────────────────────────────────────────────────


def compute_joint_matrices(
    skeleton: SkeletonData,
    skin: SkinData,
    current_worlds: Dict[str, np.ndarray],
) -> np.ndarray:
    """计算 skinning shader 的 joint_matrices[B] = current_world[b] @ inverse_bind[b]。

    这是 GPU skinning 的核心：顶点 v_world = joint_matrices[bone_id] @ v_bind。
    inverse_bind 把顶点从 bind 模型空间转到 bone local，current_world 再转到当前
    pose 的世界空间。

    Args:
        skeleton: 骨架（bones 顺序与 skin.bone_names 对齐）。
        skin: 蒙皮数据（inverse_bind_matrices (B,4,4)）。
        current_worlds: 当前 pose 的每骨骼世界矩阵 {bone_name: 4x4}。
            通常由 sample_animation + apply_animation_to_scene 得到；
            bind pose 时传 skeleton.rest_world_matrices 对应的 {name: rest_world}。

    Returns:
        (MAX_BONES,4,4) float32 joint_matrices，固定 MAX_BONES 行（shader uniform
        上限）。骨骼数 < MAX_BONES 时尾部用 identity 填充（顶点不动）；
        骨骼数 > MAX_BONES 时截断到前 MAX_BONES（超出的骨骼不参与形变）。
        骨骼名查不到时用 rest_world（bind pose，顶点不动），记 warning。
    """
    b = skeleton.bone_count
    if b == 0 or skin.inverse_bind_matrices.shape[0] == 0:
        # 空骨架仍返回 MAX_BONES 个 identity（shader uniform 数组大小固定）
        return np.broadcast_to(np.eye(4, dtype=np.float32), (MAX_BONES, 4, 4)).copy()

    ibm = skin.inverse_bind_matrices  # (B,4,4)
    n = min(b, ibm.shape[0], MAX_BONES)
    # 固定输出 MAX_BONES 行，identity 填充（保证 bone_id >= n 时顶点不动）
    out = np.broadcast_to(np.eye(4, dtype=np.float32), (MAX_BONES, 4, 4)).copy()

    for i in range(n):
        bone = skeleton.bones[i]
        cur_world = current_worlds.get(bone.name)
        if cur_world is None:
            # 骨骼名查不到（动画未覆盖或名称不匹配）→ 用 rest_world（顶点保持 bind pose）
            logger.warning(
                "compute_joint_matrices: bone '%s' (idx=%d) not in current_worlds, "
                "using rest_world (bind pose)", bone.name, i)
            cur_world = bone.rest_world
        cur_world = np.asarray(cur_world, dtype=np.float32).reshape(4, 4)
        out[i] = (cur_world @ ibm[i]).astype(np.float32)

    return out


def rest_pose_worlds(skeleton: SkeletonData) -> Dict[str, np.ndarray]:
    """返回 bind pose 的 {bone_name: rest_world}（无动画时 compute_joint_matrices 用）。"""
    return {b.name: b.rest_world for b in skeleton.bones}


# ──────────────────────────────────────────────────────────────────────
# 骨骼名唯一化（glTF 不保证 bone name 唯一）
# ──────────────────────────────────────────────────────────────────────


def unique_bone_names(names: List[str]) -> List[str]:
    """骨骼名去重：重复名加后缀 _2/_3/...。

    glTF 不保证 node name 唯一，但 SkinData.bone_names 与 SkeletonData.bones 的
    name 必须一一对应（compute_joint_matrices 靠 name 查 current_worlds）。
    动画 channel 的 node_name 也用统一化后的名（loader 调用此函数后回填）。

    Args:
        names: 原始骨骼名列表（可能有重复）。

    Returns:
        去重后的名列表（顺序不变；首个出现的不加后缀，后续重复加 _2/_3/...）。
    """
    seen: Dict[str, int] = {}
    out: List[str] = []
    used: set = set()  # 所有已输出名（含原始 + 去重生成的），防生成名与后续原始名冲突
    for n in names:
        if n not in seen and n not in used:
            seen[n] = 1
            out.append(n)
            used.add(n)
        else:
            # 重复：后缀从 _2 开始（首次重复 = _2，第二次 = _3，...）
            seen[n] = seen.get(n, 1) + 1
            # 生成唯一名：若 {n}_{count} 已被占用，继续递增直到不冲突
            candidate = f"{n}_{seen[n]}"
            while candidate in used:
                seen[n] += 1
                candidate = f"{n}_{seen[n]}"
            out.append(candidate)
            used.add(candidate)
    return out


# ──────────────────────────────────────────────────────────────────────
# F6-6：骨骼 overlay buffer 构建（线段 + 关节点）
# ──────────────────────────────────────────────────────────────────────


def build_skeleton_overlay(
    skeleton: SkeletonData,
    current_worlds: Optional[Dict[str, np.ndarray]] = None,
    bone_size: float = 0.02,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """构建骨骼 overlay 的线段 + 关节点 buffer。

    线段：每根非根骨骼画一条 parent_head → bone_head（骨骼连接线）。
    关节点：每根骨骼的 head 位置画一个小八面体/十字（用单点 + GL_POINTS 放大即可）。

    Args:
        skeleton: 骨架数据。
        current_worlds: 当前 pose 世界矩阵（None=用 rest_world = bind pose）。
        bone_size: 关节点显示大小（GL_POINTS size；线段用细线）。

    Returns:
        (line_vertices, joint_positions, joint_sizes)
        - line_vertices: (2*K, 3) float32 线段端点（每对为一个线段）
        - joint_positions: (B, 3) float32 关节点位置（GL_POINTS）
        - joint_sizes: (B,) float32 每个关节点的大小（根骨骼稍大）
    """
    if skeleton.is_empty:
        empty3 = np.zeros((0, 3), dtype=np.float32)
        return empty3, empty3, np.zeros((0,), dtype=np.float32)

    worlds = current_worlds if current_worlds is not None else rest_pose_worlds(skeleton)

    # 关节点位置 = 每根骨骼 world matrix 的 translation
    joint_pos = np.zeros((skeleton.bone_count, 3), dtype=np.float32)
    for i, bone in enumerate(skeleton.bones):
        w = worlds.get(bone.name, bone.rest_world)
        w = np.asarray(w, dtype=np.float32).reshape(4, 4)
        joint_pos[i] = w[:3, 3]

    # 线段：每根非根骨骼画 parent_head → bone_head
    line_verts: List[np.ndarray] = []
    for bone in skeleton.bones:
        if bone.parent_index < 0:
            continue
        parent_head = joint_pos[bone.parent_index]
        bone_head = joint_pos[bone.index]
        line_verts.append(parent_head)
        line_verts.append(bone_head)

    if line_verts:
        line_vertices = np.stack(line_verts).astype(np.float32).reshape(-1, 3)
    else:
        line_vertices = np.zeros((0, 3), dtype=np.float32)

    # 关节点大小：根骨骼稍大（便于识别 hierarchy root）
    joint_sizes = np.full((skeleton.bone_count,), bone_size, dtype=np.float32)
    for ri in skeleton.root_indices:
        joint_sizes[ri] = bone_size * 1.5

    return line_vertices, joint_pos, joint_sizes


# ──────────────────────────────────────────────────────────────────────
# F6-7：蒙皮权重热力色（weight display mode）
# ──────────────────────────────────────────────────────────────────────

# 热力色查找表（0→蓝，0.5→绿，1→红；与权重强度对应）
_WEIGHT_LUT = np.array([
    [0.0, 0.0, 1.0],   # 0.0 蓝（无影响）
    [0.0, 1.0, 1.0],   # 0.25 青
    [0.0, 1.0, 0.0],   # 0.5 绿（中等）
    [1.0, 1.0, 0.0],   # 0.75 黄
    [1.0, 0.0, 0.0],   # 1.0 红（强影响）
], dtype=np.float32)


def weight_to_color(weight: float) -> np.ndarray:
    """单个权重值 → RGB 热力色（0=蓝，1=红）。"""
    t = max(0.0, min(1.0, float(weight)))
    idx = t * (len(_WEIGHT_LUT) - 1)
    i0 = int(np.floor(idx))
    i1 = min(i0 + 1, len(_WEIGHT_LUT) - 1)
    frac = idx - i0
    return _WEIGHT_LUT[i0] * (1.0 - frac) + _WEIGHT_LUT[i1] * frac


def build_weight_colors(
    skin: SkinData,
    bone_index: int = 0,
) -> np.ndarray:
    """把指定骨骼对每顶点的影响权重 → 热力色 (N,3) float32。

    顶点受 bone_index 影响的权重来自 4 个 influences 中匹配 bone_index 的那个；
    若 bone_index 不在该顶点的 4 个 influences 里，权重=0（蓝色）。

    Args:
        skin: 蒙皮数据（bone_indices (N,4) + bone_weights (N,4)）。
        bone_index: 要查看的骨骼下标。

    Returns:
        (N,3) float32 每顶点热力色（0=蓝无影响，1=红强影响）。
    """
    if skin.bone_indices.shape[0] == 0:
        return np.zeros((0, 3), dtype=np.float32)

    bi = skin.bone_indices  # (N,4) uint32
    bw = skin.bone_weights  # (N,4) float32

    # 每顶点：在 4 个 influences 里找 bone_index 的权重（找不到=0）
    mask = (bi == bone_index)  # (N,4) bool
    # 若同一顶点有多个匹配（不该发生），取最大
    weights = np.where(mask, bw, 0.0).max(axis=1)  # (N,)
    weights = np.clip(weights, 0.0, 1.0)

    # 向量化热力色：weights (N,) → 查表
    idx = weights * (len(_WEIGHT_LUT) - 1)
    i0 = np.floor(idx).astype(np.int32)
    i1 = np.minimum(i0 + 1, len(_WEIGHT_LUT) - 1)
    frac = (idx - i0).astype(np.float32).reshape(-1, 1)
    colors = _WEIGHT_LUT[i0] * (1.0 - frac) + _WEIGHT_LUT[i1] * frac
    return colors.astype(np.float32)


def build_all_bone_weights_summary(skin: SkinData) -> Dict[int, float]:
    """统计每根骨骼的总影响权重（用于 weight display mode 的骨骼列表排序）。

    Args:
        skin: 蒙皮数据。

    Returns:
        {bone_index: total_weight}（所有顶点上该骨骼权重的和；用于 UI 排序「影响最大的骨骼」）。
    """
    if skin.bone_indices.shape[0] == 0:
        return {}
    bi = skin.bone_indices.flatten()  # (N*4,)
    bw = skin.bone_weights.flatten()  # (N*4,)
    summary: Dict[int, float] = {}
    for bone_idx in range(int(bi.max()) + 1 if bi.size > 0 else 0):
        mask = bi == bone_idx
        summary[bone_idx] = float(bw[mask].sum())
    return summary
