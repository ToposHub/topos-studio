"""
3D 场景图（Scene Graph）—— glTF/FBX 导入后的内部场景表示。

设计目标：
- 纯 numpy 数据结构，无 GL / PyQt 依赖（可独立单测）。
- 表达父子变换层级：flatten_scene 递归乘 world_matrix（= parent_world @ local）。
- 承载 PBR 材质标量参数 + 蒙皮（SkinData）+ 动画（AnimationClip）。

与本项目其它 3D 模块的关系：
- model_loader.py 把 glTF/FBX 解析成本模块的 SceneGraphRoot。
- scene3d_node.py 用 flatten_scene 取扁平 mesh 列表做渲染。
- primitives.MeshData 是叶子几何；这里引用但不修改它。

约定（与 math_utils.py 一致）：
- 所有 4x4 矩阵为行主序 numpy float32，应用顺序 M @ v。
- world = parent_world @ local（先 local 再 parent）。
"""

import logging
import hashlib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


def stable_scene_node_id(source_path: str, node_path: str) -> str:
    """Return the stable id shared by hierarchy and render mesh metadata."""
    digest = hashlib.md5()
    digest.update(str(source_path).encode("utf-8", "ignore"))
    digest.update(b"|")
    digest.update(str(node_path).encode("utf-8", "ignore"))
    return digest.hexdigest()[:12]


def mat4_identity() -> np.ndarray:
    """4x4 单位矩阵（行主序 float32）。"""
    return np.eye(4, dtype=np.float32)


# ──────────────────────────────────────────────────────────────────────
# 材质（PBR 标量参数 + 纹理描述符）
# ──────────────────────────────────────────────────────────────────────

# 纹理在材质中的角色（决定 sRGB↔linear 转换策略）。
# data（normal/ORM/AO）保持线性；color（baseColor/emissive）需 sRGB→linear。
TEXTURE_ROLE_DATA = "data"
TEXTURE_ROLE_COLOR = "color"


@dataclass
class TextureDescriptor:
    """材质纹理描述符（路径 + 颜色空间角色）。

    loader 提取导入纹理时填 path + role；render 线程的材质纹理加载器据此
    解析相对路径（模型文件旁）并按 role 做颜色转换。GL texture 对象不在
    这里（render 线程上传后存 flat_mesh 的 texture handle）。

    Attributes:
        path: 纹理文件绝对路径（loader 解析后）。
        role: "color"（baseColor/emissive，需 sRGB→linear）或 "data"（normal/ORM/AO，线性）。
        tex_coord_set: glTF 用的 UV 集（0 或 1，默认 0；AO/lightmap 常用第二 UV）。
    """
    path: str = ""
    role: str = TEXTURE_ROLE_DATA
    tex_coord_set: int = 0


@dataclass
class MaterialData:
    """PBR 材质：标量参数 + 可选纹理描述符（Model3D 材质升级）。

    glTF material.pbrMetallicRoughness 映射：
      baseColorFactor → base_color（线性）
      metallicFactor  → metallic
      roughnessFactor → roughness
      baseColorTexture → base_color_tex（TextureDescriptor，role=color）
      metallicRoughnessTexture → orm_tex（Occlusion/Roughness/Metallic 打包，role=data）
      normalTexture → normal_tex（role=data）
      occlusionTexture → ao_tex（或并入 orm_tex 的 R 通道）
      emissiveFactor → emissive（线性）
      emissiveTexture → emissive_tex（role=color）

    纹理描述符为空 path 时表示无该贴图（fallback 到标量）。Scene3D 路径 A
    读这些字段上传纹理 + 设 has_map uniform；无纹理时退化为纯标量 PBR。
    """
    base_color: np.ndarray = field(default_factory=lambda: np.array([0.8, 0.8, 0.8], dtype=np.float32))
    metallic: float = 0.0
    roughness: float = 0.5
    # P8-3：不透明度（1.0=完全不透明）。<1.0 视为透明，参与 painter's order 排序，
    # 并禁用 backface culling（透明物体双面可见）。glTF alphaMode=BLEND/MASK 映射到此。
    alpha: float = 1.0
    # P8-3：双面渲染标志（glTF doubleSided=true → two_sided=True）。
    # True=不剔除背面；False=剔除背面（闭网格默认，省 ~50% 片元）。
    two_sided: bool = True
    # Model3D 材质升级：纹理描述符（空 path = 无贴图，用标量）
    base_color_tex: TextureDescriptor = field(default_factory=TextureDescriptor)
    normal_tex: TextureDescriptor = field(default_factory=TextureDescriptor)
    orm_tex: TextureDescriptor = field(default_factory=TextureDescriptor)  # glTF ORM 打包（O/R/M）
    ao_tex: TextureDescriptor = field(default_factory=TextureDescriptor)
    emissive_tex: TextureDescriptor = field(default_factory=TextureDescriptor)
    # emissive 标量（无贴图时用）
    emissive: np.ndarray = field(default_factory=lambda: np.array([0.0, 0.0, 0.0], dtype=np.float32))
    # P1-23：材质编辑器使用的稳定槽位标识。旧 loader 不提供时由
    # Model3D 退化到 mesh node id，保持历史工程可读。
    material_id: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        self.base_color = np.asarray(self.base_color, dtype=np.float32).reshape(3)
        self.emissive = np.asarray(self.emissive, dtype=np.float32).reshape(3)

    def has_any_texture(self) -> bool:
        """是否含任何材质纹理（决定 Scene3D 是否走纹理采样路径）。"""
        return any(t.path for t in (self.base_color_tex, self.normal_tex,
                                    self.orm_tex, self.ao_tex, self.emissive_tex))


# ──────────────────────────────────────────────────────────────────────
# 蒙皮（Skinning）
# ──────────────────────────────────────────────────────────────────────

# 顶点最多受 4 根骨骼影响（GPU 顶点布局 4i 4f）
MAX_BONES_PER_VERTEX = 4
# 上传到 shader 的骨骼矩阵数组上限
MAX_BONES = 64


@dataclass
class SkinData:
    """蒙皮数据。

    Attributes:
        bone_names: 骨骼名列表（与 inverse_bind_matrices 行对应）。
        inverse_bind_matrices: (B,4,4) 每根骨骼的逆绑定姿态（世界空间）。
        bone_indices: (N,4) uint32 每顶点影响的骨骼索引（不足 4 个补 0）。
        bone_weights: (N,4) float32 每顶点对应的权重（不足补 0，行和≈1）。
    """
    bone_names: List[str]
    inverse_bind_matrices: np.ndarray  # (B,4,4) float32
    bone_indices: np.ndarray           # (N,4) uint32
    bone_weights: np.ndarray           # (N,4) float32

    def __post_init__(self) -> None:
        self.inverse_bind_matrices = np.ascontiguousarray(
            self.inverse_bind_matrices, dtype=np.float32)
        self.bone_indices = np.ascontiguousarray(self.bone_indices, dtype=np.uint32)
        self.bone_weights = np.ascontiguousarray(self.bone_weights, dtype=np.float32)

    @property
    def bone_count(self) -> int:
        return int(self.inverse_bind_matrices.shape[0])


# ──────────────────────────────────────────────────────────────────────
# 动画（关键帧）
# ──────────────────────────────────────────────────────────────────────

@dataclass
class AnimationChannel:
    """单个节点的动画轨道（TRS 关键帧）。

    Attributes:
        node_name: 受影响的场景节点名。
        times: (K,) float32 关键帧时间（秒）。
        translations: (K,3) float32 平移。
        rotations: (K,4) float32 旋转四元数 (x,y,z,w)。
        scales: (K,3) float32 缩放。
    """
    node_name: str
    times: np.ndarray
    translations: np.ndarray
    rotations: np.ndarray
    scales: np.ndarray

    def __post_init__(self) -> None:
        self.times = np.ascontiguousarray(self.times, dtype=np.float32)
        self.translations = np.ascontiguousarray(self.translations, dtype=np.float32)
        self.rotations = np.ascontiguousarray(self.rotations, dtype=np.float32)
        self.scales = np.ascontiguousarray(self.scales, dtype=np.float32)


@dataclass
class AnimationClip:
    """一段动画（含多个 channel）。"""
    name: str
    duration: float  # 秒
    channels: List[AnimationChannel] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# 场景节点 & 根
# ──────────────────────────────────────────────────────────────────────

# 延迟引用 MeshData（避免与 primitives 循环导入）
MeshData = "MeshData"  # type: ignore  # 仅用于类型注解


@dataclass
class SceneNode:
    """场景树节点（父子层级）。

    local_transform 是相对父节点的 4x4 矩阵；flatten_scene 累乘到 world。

    Attributes:
        name: 节点名（glTF/FBX 节点名）。
        node_id: F1 稳定唯一 id（build_hierarchy 生成，选中同步用；默认空=未设置）。
        mesh: primitives.MeshData；叶子节点几何。
        material: 该节点的材质。
        skin: 蒙皮数据（仅 skinned mesh）。
        local_transform: 相对父节点的 4x4 变换。
        children: 子节点列表。
    """
    name: str = ""
    node_id: str = ""                    # F1：稳定 id（build_hierarchy 填充）
    mesh: Optional[object] = None        # primitives.MeshData；叶子节点几何
    material: Optional[MaterialData] = None
    skin: Optional[SkinData] = None
    local_transform: np.ndarray = field(default_factory=mat4_identity)
    children: List["SceneNode"] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.local_transform = np.asarray(self.local_transform, dtype=np.float32).reshape(4, 4)


# ──────────────────────────────────────────────────────────────────────
# 相机 / 灯光（F8：外部文件导入的相机和灯光）
# ──────────────────────────────────────────────────────────────────────

# 相机类型
CAMERA_TYPE_PERSPECTIVE = "perspective"
CAMERA_TYPE_ORTHOGRAPHIC = "orthographic"

# 灯光类型（与 scene_payload.LightPayload 对齐）
LIGHT_TYPE_DIRECTIONAL = "directional"
LIGHT_TYPE_POINT = "point"
LIGHT_TYPE_SPOT = "spot"
LIGHT_TYPE_AMBIENT = "ambient"
LIGHT_TYPE_AREA = "area"


@dataclass
class CameraData:
    """导入的相机数据（master_plan §21.11 F8-1）。

    glTF/FBX 文件中的相机参数，由 loader 提取。拖拽导入时若 report.camera_count>0
    才创建 Camera3D 节点（master_plan §21.11）。

    Attributes:
        name: 相机名。
        camera_type: CAMERA_TYPE_* 常量。
        transform: 4x4 世界变换矩阵（位置+朝向）。
        focal_length: 焦距（mm）。
        sensor_width: 传感器宽度（mm）。
        sensor_height: 传感器高度（mm）。
        fov_y: 垂直视场角（度；从 focal+sensor 或文件直接读取）。
        near: 近裁剪面。
        far: 远裁剪面。
        orthographic_height: 正交模式下的视野高度。
    """
    name: str = ""
    camera_type: str = CAMERA_TYPE_PERSPECTIVE
    transform: np.ndarray = field(default_factory=mat4_identity)
    focal_length: float = 50.0
    sensor_width: float = 36.0
    sensor_height: float = 24.0
    fov_y: float = 0.0  # 0=未设置（从 focal/sensor 计算）
    near: float = 0.1
    far: float = 1000.0
    orthographic_height: float = 2.0

    def __post_init__(self) -> None:
        self.transform = np.asarray(self.transform, dtype=np.float32).reshape(4, 4)
        # 若未设 fov，从 focal_length + sensor_height 推算
        if self.fov_y <= 0.0 and self.focal_length > 0.0:
            self.fov_y = float(2.0 * np.degrees(np.arctan(
                self.sensor_height * 0.5 / self.focal_length)))


@dataclass
class LightData:
    """导入的灯光数据（master_plan §21.11 F8-1）。

    glTF/FBX 文件中的灯光参数。area 类型第一版 fallback 为 point/spot + 警告。

    Attributes:
        name: 灯光名。
        light_type: LIGHT_TYPE_* 常量。
        transform: 4x4 世界变换矩阵（位置+朝向）。
        color: (3,) RGB 线性颜色。
        intensity: 强度。
        range_: 有效半径（point/spot；directional 忽略）。
        inner_cone: 聚光内锥角（度，spot）。
        outer_cone: 聚光外锥角（度，spot）。
    """
    name: str = ""
    light_type: str = LIGHT_TYPE_DIRECTIONAL
    transform: np.ndarray = field(default_factory=mat4_identity)
    color: np.ndarray = field(default_factory=lambda: np.array([1.0, 1.0, 1.0], dtype=np.float32))
    intensity: float = 1.0
    range_: float = 10.0
    inner_cone: float = 30.0
    outer_cone: float = 45.0
    # P1-26 physical-light metadata. These fields survive import even when a
    # backend does not expose a native IES/shadow object.
    decay_mode: str = "inverse_square"
    shadow_enabled: bool = True
    shadow_quality: str = "medium"
    shadow_bias: float = 0.002
    shadow_softness: float = 0.2
    unit_mode: str = "m"

    def __post_init__(self) -> None:
        self.transform = np.asarray(self.transform, dtype=np.float32).reshape(4, 4)
        self.color = np.asarray(self.color, dtype=np.float32).reshape(3)


@dataclass
class SceneGraphRoot:
    """整个导入模型的根。

    Attributes:
        root: 虚拟根节点（其 children 是顶层场景对象）。
        skins: 场景内所有蒙皮（glTF skins 数组）。
        animations: 动画片段。
        bbox_min / bbox_max: 所有 mesh 顶点（在各自 world 空间）的包围盒。
        import_report: 可选的导入报告（F0）。None 表示未生成（旧路径向后兼容）。
            loader 在每个 backend 分支填充；Model3DNode 据此向 UI 显示导入状态。
    """
    root: SceneNode
    skins: List[SkinData] = field(default_factory=list)
    animations: List[AnimationClip] = field(default_factory=list)
    cameras: List[CameraData] = field(default_factory=list)
    lights: List[LightData] = field(default_factory=list)
    # F6：骨架层级（骨骼树 + rest pose）。由 _extract_trimesh_skeletons 填充；
    # 与 skins 平行（skins 是顶点权重数据，skeletons 是骨骼层级数据）。
    # 空列表=无骨架（非蒙皮模型）。skeleton overlay 渲染用。
    skeletons: List[object] = field(default_factory=list)  # List[SkeletonData]
    bbox_min: np.ndarray = field(default_factory=lambda: np.array([0, 0, 0], dtype=np.float32))
    bbox_max: np.ndarray = field(default_factory=lambda: np.array([0, 0, 0], dtype=np.float32))
    import_report: Optional[object] = None  # model_import_report.ModelImportReport | None


@dataclass
class FlattenedMesh:
    """flatten_scene 的输出：一个带世界矩阵的 mesh（可直接渲染）。

    world_matrix 是 4x4（= 祖先 local 链乘积），渲染时 effective_model =
    node_model @ world_matrix，从而实现父子联动。
    """
    mesh: object                  # primitives.MeshData
    world_matrix: np.ndarray      # (4,4) float32
    material: Optional[MaterialData] = None
    skin: Optional[SkinData] = None
    node_name: str = ""
    node_id: str = ""

    def __post_init__(self) -> None:
        self.world_matrix = np.asarray(self.world_matrix, dtype=np.float32).reshape(4, 4)


# ──────────────────────────────────────────────────────────────────────
# 遍历：扁平化为带世界矩阵的 mesh 列表
# ──────────────────────────────────────────────────────────────────────

def flatten_scene(root: SceneNode,
                  parent_world: Optional[np.ndarray] = None,
                  source_path: str = "") -> List[FlattenedMesh]:
    """DFS 遍历场景树，返回每个叶子 mesh 的世界矩阵。

    world = parent_world @ local（先 local 再 parent，与 math_utils 的 M @ v 约定一致）。
    非叶子节点（mesh=None）只传递变换，不产生渲染项。

    AUD-9117：node_id 由 (source_path, 名字路径) 哈希而来，而 glTF/FBX 不要求
    节点名唯一——同名兄弟的名字路径完全相同会导致 node_id 冲突（scene3d 的
    hidden_mesh_ids/选中会同时作用到两个 mesh）。同一父节点下的同名子节点按
    出现次序追加 ``#n`` 后缀消歧（首个不加，无重名场景的 id 保持不变）。

    Args:
        root: 子树的根节点。
        parent_world: 父级世界矩阵；None 表示单位（顶层）。
        source_path: 导入源文件路径（参与 node_id 哈希）。

    Returns:
        该子树内所有 mesh 的扁平列表。
    """
    disambiguated = 0

    def _visit(node: SceneNode, parent: np.ndarray, path: str,
               suffix: str = "") -> List[FlattenedMesh]:
        nonlocal disambiguated
        node_path = f"{path}/{node.name}" if path else node.name
        if suffix:
            node_path = f"{node_path}#{suffix}"
            disambiguated += 1
        world = parent @ node.local_transform
        out: List[FlattenedMesh] = []
        if node.mesh is not None:
            out.append(FlattenedMesh(
                mesh=node.mesh,
                world_matrix=world,
                material=node.material,
                skin=node.skin,
                node_name=node.name,
                node_id=stable_scene_node_id(source_path, node_path),
            ))
        # AUD-9117：统计同名兄弟的出现次序，为第 2+ 个追加 #n 后缀。
        seen: dict = {}
        for child in node.children:
            count = seen.get(child.name, 0)
            seen[child.name] = count + 1
            child_suffix = "" if count == 0 else str(count)
            out.extend(_visit(child, world, node_path, child_suffix))
        return out

    result = _visit(root, parent_world if parent_world is not None else mat4_identity(), "")
    if disambiguated:
        logger.warning(
            "flatten_scene: 场景含 %d 个同名兄弟节点，已用 #n 后缀消歧 node_id"
            "（重名节点的动画通道仍按名字绑定，可能互相覆盖）", disambiguated)
    return result


# ──────────────────────────────────────────────────────────────────────
# 归一化：居中 + 等比缩放
# ──────────────────────────────────────────────────────────────────────

def compute_bbox(root: SceneNode,
                 parent_world: Optional[np.ndarray] = None) -> Tuple[np.ndarray, np.ndarray]:
    """计算场景在「flatten 后的 world 空间」的 AABB 包围盒。

    对每个 mesh 的顶点先乘 world_matrix 再取 min/max。空场景返回单位盒。
    """
    if parent_world is None:
        parent_world = mat4_identity()
    world = parent_world @ root.local_transform
    mins = [np.array([np.inf, np.inf, np.inf], dtype=np.float32)]
    maxs = [np.array([-np.inf, -np.inf, -np.inf], dtype=np.float32)]
    if root.mesh is not None:
        verts = np.asarray(root.mesh.vertices, dtype=np.float32)  # (N,3)
        if verts.shape[0] > 0:
            homog = np.hstack([verts, np.ones((verts.shape[0], 1), dtype=np.float32)])
            world_verts = (world @ homog.T).T[:, :3]
            mins.append(world_verts.min(axis=0))
            maxs.append(world_verts.max(axis=0))
    for child in root.children:
        cmin, cmax = compute_bbox(child, world)
        mins.append(cmin)
        maxs.append(cmax)
    bmin = np.min(mins, axis=0)
    bmax = np.max(maxs, axis=0)
    if not np.all(np.isfinite(bmin)):
        bmin = np.array([-1, -1, -1], dtype=np.float32)
        bmax = np.array([1, 1, 1], dtype=np.float32)
    return bmin, bmax


def normalize_scene(root_data: SceneGraphRoot,
                    target_size: float = 2.0) -> SceneGraphRoot:
    """在根节点加一个「居中 + 等比缩放」的 local_transform，使模型放进 [-t/2, t/2]³。

    策略：把归一化矩阵塞进 root_data.root.local_transform（顶层父），
    这样 flatten_scene 的所有 mesh 都会被它影响，且不破坏模型内部相对层级。
    缩放因子避免退化（bbox 尺寸为 0 时跳过）。

    Args:
        root_data: 待归一化的场景根。
        target_size: 目标最长边长度（世界单位）。默认 2.0 ≈ 默认相机距离合理。

    Returns:
        同一对象（原地修改 root.transform）。
    """
    bmin, bmax = compute_bbox(root_data.root)
    center = (bmin + bmax) * 0.5
    size = bmax - bmin
    max_extent = float(np.max(size))
    root_data.bbox_min = bmin
    root_data.bbox_max = bmax
    if max_extent < 1e-6:
        return root_data  # 退化，不缩放
    scale = target_size / max_extent
    # 归一化矩阵：先平移到原点（-center），再缩放。M = S @ T(-center)（先平移再缩放）
    t_neg = np.eye(4, dtype=np.float32)
    t_neg[0, 3] = -float(center[0])
    t_neg[1, 3] = -float(center[1])
    t_neg[2, 3] = -float(center[2])
    s_mat = np.eye(4, dtype=np.float32)
    s_mat[0, 0] = s_mat[1, 1] = s_mat[2, 2] = float(scale)
    norm_m = s_mat @ t_neg
    # 把归一化塞到根节点 local（左乘，使其先作用于子树）
    root_data.root.local_transform = (norm_m @ root_data.root.local_transform).astype(np.float32)
    return root_data


# ──────────────────────────────────────────────────────────────────────
# 动画采样：按时间插值 channel 关键帧 → 每节点 local TRS
# ──────────────────────────────────────────────────────────────────────

def _lerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """线性插值（numpy 数组）。"""
    return a + (b - a) * t


def _slerp(q0: np.ndarray, q1: np.ndarray, t: float) -> np.ndarray:
    """四元数球面线性插值 (x,y,z,w)。

    处理双覆盖：若 dot<0 取反 q1 保证最短路径。
    """
    dot = float(np.dot(q0, q1))
    if dot < 0.0:
        q1 = -q1
        dot = -dot
    # 接近平行时退化为 lerp（避免数值不稳）
    if dot > 0.9995:
        return _lerp(q0, q1, t) / float(np.linalg.norm(_lerp(q0, q1, t)) + 1e-12)
    theta_0 = float(np.arccos(np.clip(dot, -1.0, 1.0)))
    sin_theta_0 = float(np.sin(theta_0))
    theta = theta_0 * t
    s0 = float(np.sin((1.0 - t) * theta_0)) / sin_theta_0
    s1 = float(np.sin(theta)) / sin_theta_0
    return s0 * q0 + s1 * q1


def _sample_channel(channel: AnimationChannel, time: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """采样单个 channel 在 time 的 TRS。

    超出范围时钳制到端点（不外插）。
    """
    times = channel.times
    n = times.shape[0]
    if n == 0:
        eye3 = np.zeros(3, dtype=np.float32)
        return eye3, np.array([0, 0, 0, 1], dtype=np.float32), np.ones(3, dtype=np.float32)
    if n == 1 or time <= times[0]:
        return (channel.translations[0].copy(),
                channel.rotations[0].copy(),
                channel.scales[0].copy())
    if time >= times[-1]:
        return (channel.translations[-1].copy(),
                channel.rotations[-1].copy(),
                channel.scales[-1].copy())
    # 二分找区间
    idx = int(np.searchsorted(times, time, side="right") - 1)
    idx = max(0, min(idx, n - 2))
    t0, t1 = float(times[idx]), float(times[idx + 1])
    denom = (t1 - t0) if (t1 - t0) > 1e-9 else 1.0
    a = float(np.clip((time - t0) / denom, 0.0, 1.0))
    return (_lerp(channel.translations[idx], channel.translations[idx + 1], a),
            _slerp(channel.rotations[idx], channel.rotations[idx + 1], a),
            _lerp(channel.scales[idx], channel.scales[idx + 1], a))


def quat_to_mat4(q: np.ndarray) -> np.ndarray:
    """四元数 (x,y,z,w) → 4x4 旋转矩阵（行主序）。"""
    x, y, z, w = float(q[0]), float(q[1]), float(q[2]), float(q[3])
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    m = np.eye(4, dtype=np.float32)
    m[0, 0] = 1.0 - 2.0 * (yy + zz)
    m[0, 1] = 2.0 * (xy - wz)
    m[0, 2] = 2.0 * (xz + wy)
    m[1, 0] = 2.0 * (xy + wz)
    m[1, 1] = 1.0 - 2.0 * (xx + zz)
    m[1, 2] = 2.0 * (yz - wx)
    m[2, 0] = 2.0 * (xz - wy)
    m[2, 1] = 2.0 * (yz + wx)
    m[2, 2] = 1.0 - 2.0 * (xx + yy)
    return m


def trs_to_mat4(translation: np.ndarray, rotation: np.ndarray, scale: np.ndarray) -> np.ndarray:
    """TRS → 4x4 变换矩阵（行主序）= T @ R @ S（先缩放、再旋转、再平移）。"""
    t = np.eye(4, dtype=np.float32)
    t[0, 3] = float(translation[0])
    t[1, 3] = float(translation[1])
    t[2, 3] = float(translation[2])
    r = quat_to_mat4(rotation)
    s = np.eye(4, dtype=np.float32)
    s[0, 0] = float(scale[0])
    s[1, 1] = float(scale[1])
    s[2, 2] = float(scale[2])
    return (t @ r @ s).astype(np.float32)


def sample_animation(clip: AnimationClip, time: float) -> dict:
    """采样整段动画在 time 的状态。

    Args:
        clip: 动画片段。
        time: 时间（秒）。会按 clip.duration 循环（loop）。

    Returns:
        {node_name: 4x4 local_transform} 该时间点每个受影响节点的本地矩阵。
        未被动画覆盖的节点保持其原 local_transform（由调用方合并）。
    """
    if clip.duration > 1e-6:
        time = float(time) % clip.duration  # 循环
    result: dict = {}
    for ch in clip.channels:
        t, r, s = _sample_channel(ch, time)
        result[ch.node_name] = trs_to_mat4(t, r, s)
    return result


def apply_animation_to_scene(root: SceneNode, sampled: dict,
                             parent_world: Optional[np.ndarray] = None) -> dict:
    """递归把采样结果合并进场景树，返回 {node_name: world_matrix}。

    受动画影响的节点用 sampled 的 local 替换；其余用原 local。
    不修改原树（返回新映射），便于动画切换。
    """
    if parent_world is None:
        parent_world = mat4_identity()
    local = sampled.get(root.name, root.local_transform)
    world = parent_world @ local
    out = {root.name: world}
    for child in root.children:
        out.update(apply_animation_to_scene(child, sampled, world))
    return out
