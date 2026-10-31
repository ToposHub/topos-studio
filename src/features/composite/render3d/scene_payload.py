"""ScenePayload — 阶段七：Scene3D 节点拆分的结构化场景数据。

本模块定义 4 个 dataclass，分别承载 Camera3D / Light3D / Geo3D / Model3D
源节点产出的场景数据，由 Scene3D 聚合器（ScanlineRender 角色）消费。

设计要点（双轨制 / Two-Track）：
    现有求值管线全程硬编码 ``Dict[port_name, TextureResult]``，scene 数据
    无法走标准缓存轨道（会被 ``_filter_marker_keys`` 丢弃、或让
    ``_tex_meta`` 崩溃）。因此 scene 源节点在 ``_do_evaluate`` 内：
      1. 仍返回 ``{"display": TextureResult}``（占位/预览图，让管线不报错）
      2. 同时把真正的 ``ScenePayload`` 写入求值器实例上的
         ``scene_payloads`` 旁路字典（``CompositeEvalContext.scene_payloads``）

    Scene3D 聚合器通过该旁路读取上游 4 个节点的 payload，执行真实 draw call。

    payload 含 GL 对象引用（VAO/VBO/cube map），本就不可序列化/缓存，
    故旁路是正确的工程权衡。删掉 ``scene_payloads`` 旁路即可回退到纯 2D 管线。

字段命名与 ``scene3d_node.py``（拆分前）的局部变量一一对应，便于逐行迁移：
    - ``CameraPayload``   ← ``scene3d_node.py:918-923`` 的 proj/view/eye/target
    - ``LightPayload``    ← ``scene3d_node.py:822-843`` 组装的 dirs/colors/intensities
                            + ``_ensure_ibl`` 产出的 IBL 三件套
    - ``GeometryPayload`` ← procedural 路径（``_ensure_mesh_geometry`` + instance VBO）
    - ``ModelPayload``    ← 导入模型路径（``_ensure_flat_meshes_gl`` + 动画世界矩阵）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from .aov import ObjectRenderFlags


# ──────────────────────────────────────────────────────────────────────────
# Camera3D payload
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class CameraPayload:
    """Camera3D 节点产出：相机投影/视图矩阵 + 视点。

    由 Camera3D 用自身 width/height 算 aspect 后调用
    ``math_utils.perspective + orbit_to_eye + look_at`` 得到。
    Scene3D 渲染、视锥剔除（``extract_frustum_planes(proj @ view)``）、
    深度 pass（near/far）、gizmo（target）均依赖此 payload。
    """

    proj: np.ndarray            # 4x4 投影矩阵（已含 aspect + lens shift）
    view: np.ndarray            # 4x4 视图矩阵
    eye: Any                    # (x, y, z) 相机世界坐标
    target: Any                 # (x, y, z) 注视中心（gizmo 用）
    near: float = 0.1
    far: float = 1000.0
    fov: float = 50.0
    # P1 修复：grid 地平线渐隐用此 pitch（从 eye-target 反算），而非 Scene3D
    # 自身 fallback 的 cam_orbit_pitch——外接 Camera3D 时两者可能不一致，
    # 用错值会让 grid fade 视觉错位。Camera3D 填充；默认 20 对应内置相机默认。
    pitch: float = 20.0
    # P1 相机模式扩展：透视轨道 / 透视自由 / 正交。Scene3D 仅据此选 grid 行为，
    # proj/view 已是最终矩阵（正交模式下 fov 无意义但保留默认值不报错）。
    camera_mode: str = "orbit"  # "orbit" | "free" | "ortho"
    # P3 专业摄影机扩展：
    # 曝光（EV，Scene3D 渲染后乘 pow(2,exposure) 控制整体明度）
    exposure: float = 0.0
    # 景深 DOF（Scene3D 后处理用：采样深度纹理 → 按 coc 做散景模糊）
    dof_enable: bool = False
    focus_distance: float = 5.0
    fstop: float = 2.8
    focal_length: float = 38.6      # mm（与 coc 计算 + 图标比例用）
    sensor_size: float = 36.0       # mm（传感器高度，coc 计算用）
    sensor_width: float = 36.0      # mm（filmback 宽度）
    filmback: str = "custom"
    gate_fit: str = "vertical"
    match_mode: str = "preserve_fov"
    unit_mode: str = "m"
    unit_scale: float = 1.0         # input world unit -> project meters
    lens_shift_x: float = 0.0       # 镜头偏移（已烘焙进 proj，此字段仅诊断用）
    lens_shift_y: float = 0.0
    # 安全框开关（视口 UI overlay 用，不进渲染）
    show_safe_areas: bool = False
    # 来源 Camera3D 节点 id（由 base_node._collect_connected_scene_payloads 在
    # 收集时注入，Scene3D 据此匹配 active_camera_id 选择活动渲染相机）。
    # 不进 signature（切活动相机靠 active_camera_id 变化触发重渲染，避免
    # 同一相机参数不变但 node_id 变化时无谓重渲染）。
    node_id: str = ""

    def signature(self) -> tuple:
        """内容签名（进 Scene3D content_sig，变更触发重渲染）。

        proj/view 是 numpy 矩阵 → tobytes；eye/target/near/far/fov 是标量/元组。
        相机参数变化（FOV/距离/角度/注视点/曝光/景深）→ 签名变化 → Scene3D 不复用陈旧纹理。
        """
        return (
            "camera_v2",
            bytes(self.proj.astype(np.float32).tobytes()),
            bytes(self.view.astype(np.float32).tobytes()),
            tuple(self.eye) if self.eye is not None else (),
            tuple(self.target) if self.target is not None else (),
            float(self.near), float(self.far), float(self.fov),
            float(self.pitch), str(self.camera_mode),
            float(self.exposure), bool(self.dof_enable),
            float(self.focus_distance), float(self.fstop),
            float(self.focal_length), float(self.sensor_size),
            float(self.sensor_width), str(self.filmback), str(self.gate_fit),
            str(self.match_mode), str(self.unit_mode), float(self.unit_scale),
            float(self.lens_shift_x), float(self.lens_shift_y),
            bool(self.show_safe_areas),
        )


# ──────────────────────────────────────────────────────────────────────────
# Light3D payload
# ──────────────────────────────────────────────────────────────────────────

# 灯光类型常量（与 shader u_light_type 对应：0=dir 1=point 2=spot）
LIGHT_TYPE_DIRECTIONAL = "directional"
LIGHT_TYPE_POINT = "point"
LIGHT_TYPE_SPOT = "spot"
LIGHT_TYPE_AMBIENT = "ambient"

# shader 端 u_light_type 数组取值（int），与 _MESH_PBR_FRAGMENT 的分支一致
LIGHT_SHADER_TYPE_ID = {
    LIGHT_TYPE_DIRECTIONAL: 0,
    LIGHT_TYPE_POINT: 1,
    LIGHT_TYPE_SPOT: 2,
}

LIGHT_DECAY_NONE = "none"
LIGHT_DECAY_LINEAR = "linear"
LIGHT_DECAY_INVERSE_SQUARE = "inverse_square"
LIGHT_DECAY_SHADER_TYPE_ID = {
    LIGHT_DECAY_NONE: 0,
    LIGHT_DECAY_LINEAR: 1,
    LIGHT_DECAY_INVERSE_SQUARE: 2,
}

SHADOW_QUALITY_MAP = {
    "low": 512,
    "medium": 1024,
    "high": 2048,
    "ultra": 4096,
}


@dataclass
class LightPayload:
    """Light3D 节点产出：单盏灯光（一节点一灯）+ 可选 HDRI/IBL 环境。

    P2 重构：从原来的"4 槽位方向光定长数组"改为"单灯光描述"。
    一个 Light3D 节点对应一盏灯，Scene3D 聚合器收集所有连接的 LightPayload
    后合并为 shader 需要的定长数组（最多 4 盏直接光）。

    灯光类型（light_type）：
        - directional：平行光，无位置，用 dir 方向（归一化），无衰减。
        - point：点光源，有位置 pos，按 range 做距离衰减（UE4 光滑倒数平方）。
        - spot：聚光灯，有位置 pos + 方向 dir + 锥角（inner/outer cone，度）。
        - ambient：环境光，作为全局常量色加到 ambient 项（不进直接光数组）。

    HDRI/IBL 字段（irradiance/prefilter/brdf_lut）保留：仅当本节点启用 IBL
    时填充，Scene3D 取第一个非空的 IBL 三件套（多灯场景 IBL 由首个带 HDRI
    的 Light 节点提供，避免重复烘焙）。
    """

    light_type: str = LIGHT_TYPE_DIRECTIONAL
    # 单灯光参数
    dir: tuple = (-0.6, 0.7, 0.4)     # (x,y,z) 方向（directional/spot 主轴，归一化）
    pos: tuple = (0.0, 5.0, 0.0)      # (x,y,z) 位置（point/spot；directional/ambient 忽略）
    color: tuple = (1.0, 1.0, 1.0)    # (r,g,b) 线性颜色
    intensity: float = 2.0            # 强度（>=0）
    range: float = 10.0               # 有效半径（point/spot；0=无限）
    inner_cone: float = 30.0          # 聚光内锥角（度，spot）
    outer_cone: float = 45.0          # 聚光外锥角（度，spot）
    decay_mode: str = LIGHT_DECAY_INVERSE_SQUARE
    shadow_enabled: bool = True
    shadow_quality: str = "medium"
    shadow_bias: float = 0.002
    shadow_softness: float = 0.2
    ies_profile_path: str = ""
    ies_enabled: bool = False
    ies_samples: tuple = (1.0, 1.0, 1.0, 1.0)
    # 图标锚点（directional/ambient 无空间位置，用此锚点画图标；point/spot 用 pos）
    icon_pos: tuple = (0.0, 5.0, 0.0)
    # IBL（Image-Based Lighting）— GL cubemap / 2D texture 对象（或其 glo）
    irradiance_cube: Any = None
    prefilter_cube: Any = None
    brdf_lut: Any = None
    env_intensity: float = 1.0
    prefilter_max_lod: float = 4.0
    use_ibl: bool = True

    def signature(self) -> tuple:
        """内容签名（进 Scene3D content_sig，变更触发重渲染）。

        灯光类型/方向/位置/颜色/强度/范围/锥角为标量 tuple（直接可哈希）；
        IBL 三件套是 GL 对象，用 ``id()`` 标识（同一 cube map 缓存命中，重建后
        id 变化触发重渲染）。灯光参数变化（类型/方向/强度/颜色/HDRI 切换）
        → 签名变化 → 重渲染。
        """
        return (
            "light_v2",
            str(self.light_type),
            tuple(self.dir), tuple(self.pos), tuple(self.color),
            float(self.intensity), float(self.range),
            float(self.inner_cone), float(self.outer_cone),
            str(self.decay_mode), bool(self.shadow_enabled),
            str(self.shadow_quality), float(self.shadow_bias),
            float(self.shadow_softness),
            str(self.ies_profile_path), bool(self.ies_enabled),
            tuple(float(value) for value in self.ies_samples),
            id(self.irradiance_cube), id(self.prefilter_cube), id(self.brdf_lut),
            float(self.env_intensity), bool(self.use_ibl),
        )


# ──────────────────────────────────────────────────────────────────────────
# Geo3D payload
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class GeometryPayload:
    """Geo3D 节点产出：程序化几何（cube/plane/grid/sphere）的 draw descriptor。

    Scene3D 在无 Model3D 连接（或 Model3D 无模型）时走 procedural 路径，
    bind 此处的 VAO 并按 ``index_count`` + ``is_lines`` 执行 draw call。
    ``model_matrix`` 已包含 object_pos/rot + mesh_scale（placement 合成）。
    ``material`` 为该几何的默认 PBR 材质（导入模型 mesh 会覆盖）。
    """

    vao: Any                   # moderngl.VertexArray（procedural mesh）
    program: Any = None        # VAO 绑定的 moderngl.Program（Scene3D 设 uniform 必须用它）
    index_count: int = 0
    is_lines: bool = False     # grid 用 LINES，其余用 TRIANGLES
    instance_vbo: Any = None   # 实例化偏移 VBO（grid_x×grid_z），可空
    instance_count: int = 1
    instance_enabled: bool = False
    # CPU mirror of the local-space offsets. Auxiliary picking passes use this
    # instead of reading a render-thread-owned GL instance buffer.
    instance_offsets: Optional[np.ndarray] = None
    model_matrix: np.ndarray = None    # 4x4（object 变换 + mesh_scale）
    # CPU-side MeshData mirror used by Scene3D auxiliary passes. The beauty VAO
    # remains owned by Geo3D; carrying the immutable geometry here lets AOV
    # passes bind their own shader without borrowing another node's VAO.
    mesh: Any = None
    material: Optional[Dict[str, float]] = None  # {albedo:(r,g,b),metallic,roughness}
    # 指示 Scene3D 该 bind 哪个 shader 变体
    variant: str = "plain"     # "plain" | "instanced"
    # 内容修订号：Geo3D 每次 primitive/transform/instance/材质变化时递增。
    # 用于 signature（GL 对象用 id，但 VAO 可能复用 → 靠 revision 捕获参数变化）。
    content_revision: int = 0
    # P7：来源节点 id（_collect_connected_scene_payloads 注入）。GPU ID picking 用
    # 此把 pick_id 映射回可选中的节点。不进 signature（纯运行时身份）。
    node_id: str = ""
    # Object-level render contract.  It is pure data and safe to cross the
    # render/UI boundary; GL resources remain owned by the source node.
    render_flags: ObjectRenderFlags = field(default_factory=ObjectRenderFlags)

    def signature(self) -> tuple:
        """内容签名（进 Scene3D content_sig，变更触发重渲染）。

        model_matrix 用 tobytes；material 是标量 dict（可哈希化）；
        GL 对象（vao/instance_vbo）用 id，但 VAO 在 primitive 未变时会复用，
        故额外带 content_revision（Geo3D 每帧参数变化时递增）+ index_count/variant。

        AUD-9204：instance_offsets 不再全量 astype+tobytes（500×500 实例网格
        每帧 MB 级 memcpy）。偏移内容由 (grid_x, grid_z, spacing) 决定，全部
        进 Geo3D 的 content_sig——任何变化都会递增 content_revision（见
        geo3d_node 的 content_sig 契约），故签名只带 shape + 既有
        content_revision/instance_count 即可捕获实例变化。
        """
        mat = self.material or {}
        mat_sig = (
            tuple(mat.get("albedo", ())), mat.get("metallic"), mat.get("roughness"),
        )
        model_sig = (self.model_matrix.astype(np.float32).tobytes()
                     if self.model_matrix is not None else b"")
        return (
            "geo",
            self.variant, int(self.index_count), bool(self.is_lines),
            bool(self.instance_enabled), int(self.instance_count),
            # AUD-9204：O(1) shape 标识替代全量 tobytes（内容由 content_revision 捕获）
            (tuple(self.instance_offsets.shape)
             if self.instance_offsets is not None else ()),
            model_sig, mat_sig, int(self.content_revision),
            self.render_flags.signature(),
        )


# ──────────────────────────────────────────────────────────────────────────
# Model3D payload
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class ModelPayload:
    """Model3D 节点产出：导入模型（glTF/FBX/...）的 draw descriptor 列表。

    ``flat_meshes`` 每项是一个 dict，与拆分前 ``scene3d_node.py:_flat_meshes_gl``
    的元素结构一致：``{vao, index_count, is_skinned, can_instance, world_matrix,
    material, skin, node_name, mesh}``。Scene3D 走 path A 时遍历此列表。
    ``anim_worlds`` 为按 ``anim_time`` 采样后的每节点世界矩阵（骨骼动画）。

    当 ``has_model=False`` 时，Scene3D 退化为走 Geo3D 的 procedural 路径。
    """

    flat_meshes: List[Dict[str, Any]] = field(default_factory=list)
    anim_worlds: Dict[str, np.ndarray] = field(default_factory=dict)
    has_model: bool = False
    model_error: Optional[str] = None
    # 资源标识：模型路径 + mtime（缓存命中判定）；anim_time 标识动画帧。
    # 用于 signature（GL 对象用 id，但 flat_meshes 缓存复用 → 靠 path/mtime/anim 捕获变化）。
    model_path: str = ""
    model_mtime: float = 0.0
    anim_time: float = 0.0
    # P0 缺口：物体变换签名（object_pos/rot/scale）。Model3D 应用变换后填，
    # Scene3D 据此在 transform 变化时重渲染。
    transform_sig: tuple = ()
    # Model3D 材质升级：外部纹理 override（端口输入的 GL texture id，None=未连）。
    # Scene3D 路径 A 据此 + material_mode 决定覆盖/替换/混合导入纹理。
    texture_overrides: Optional[Dict[str, Any]] = None  # {port_name: texture_id}
    # Model3D 材质升级：材质控制参数（material_mode/normal_format/orm_layout/
    # texture_wrap/filter/emissive_intensity）。Scene3D 据此配置采样器/解码。
    material_params: Optional[Dict[str, Any]] = None
    # P1-23：按材质槽保存的非破坏覆盖。值是 JSON-safe dict，避免把编辑器
    # 对象或 Qt 状态跨 render/UI 边界传递。
    material_overrides: Optional[Dict[str, Any]] = None
    # P1-23：材质面板使用的导入态摘要（路径/颜色空间/缺失状态等）。
    materials: tuple = ()
    # P1-24：Model3D 已应用到当前 mesh 的 morph 权重（JSON-safe 元数据）。
    morph_weights: Optional[Dict[str, float]] = None
    # Milestone E / P4-6：模型导入缓存状态（"in_memory"/"disk_hit"/"miss"/None）。
    # 供 Scene3D diagnostics / UI 显示 cache hit/miss。不进 signature（纯诊断）。
    model_cache_status: Optional[str] = None
    # P2-03：后台导入状态只携带 JSON-safe 标量，避免 Future/SceneGraph
    # 跨越 render/UI 边界。phase 典型值为 queued/probe/parse/handoff/ready。
    model_import_status: Optional[str] = None
    model_import_phase: str = ""
    model_import_progress: float = 0.0
    model_import_diagnostics: tuple = ()
    model_import_resource_estimate: Optional[Dict[str, Any]] = None
    model_import_probe: Optional[Dict[str, Any]] = None
    # P7：来源节点 id（_collect_connected_scene_payloads 注入）。GPU ID picking 用。
    node_id: str = ""
    # F1：场景层级摘要（Outliner 面板渲染树用）。None=未构建（向后兼容）。
    # 由 Model3DNode 调用 build_hierarchy 填充；纯数据，不进 signature。
    hierarchy: Optional[Any] = None  # scene_hierarchy.SceneHierarchyItem | None
    # F6：骨架列表（skeleton overlay 渲染用）。None=无骨架（非蒙皮模型）。
    # 由 Model3DNode 从 _model_root.skeletons 填充；纯数据，不进 signature
    # （overlay 是辅助显示，不影响 beauty 渲染结果）。
    skeletons: Optional[list] = None  # List[SkeletonData] | None
    # P0-10：导入/变形降级诊断。使用稳定代码而不是异常对象，便于
    # Model3D → Deformer3D → Scene3D → UI/日志传递。
    diagnostics: tuple = ()
    # F7：渲染线程之外的 UI 只需要 clip 名称和时长；不要把 AnimationClip
    # 或 SceneGraphRoot 的可变对象跨线程暴露出去。
    animation_clips: tuple = ()  # ((name: str, duration: float), ...)
    # F0：同样以纯字典携带导入报告，供 UI 通过 metadata_ready 重建报告。
    import_report: Optional[Dict[str, Any]] = None
    # Outliner viewport visibility overrides. These are stable hierarchy ids,
    # not GL handles, so the setting can cross the render/UI boundary safely.
    hidden_mesh_ids: tuple = ()
    # Keep user-authored visibility separate from the effective set used by
    # the renderer while a temporary solo target is active.
    user_hidden_mesh_ids: tuple = ()
    # Outliner edit state. Lock is UI/edit protection; solo is a render-time
    # visibility override resolved by Model3D before Scene3D consumes it.
    locked_mesh_ids: tuple = ()
    solo_mesh_id: str = ""
    # Object-level render contract shared by all meshes in this source node.
    render_flags: ObjectRenderFlags = field(default_factory=ObjectRenderFlags)

    def signature(self) -> tuple:
        """内容签名（进 Scene3D content_sig，变更触发重渲染）。

        模型 path+mtime 捕获资源变化（重导入/文件改动）；anim_time 捕获动画帧；
        flat_meshes 数量捕获加载结果；transform_sig 捕获物体变换；material_params
        捕获材质控制参数（material_mode/normal_format/orm_layout/filter/wrap/
        emissive_intensity 等，值统一 str 规约保证可哈希）；GL 对象不在签名里
        （用 path/mtime 等价表达）。
        has_model=False（无模型）时签名固定，Scene3D 退化为 procedural 不受影响。
        """
        # 动画世界矩阵：每节点 tobytes（按节点名排序保证稳定）
        anim_sig = tuple(
            (name, worlds.astype(np.float32).tobytes())
            for name, worlds in sorted(self.anim_worlds.items())
        )
        return (
            "model",
            bool(self.has_model), self.model_path, float(self.model_mtime),
            float(self.anim_time), int(len(self.flat_meshes)),
            tuple(sorted(str(value) for value in self.hidden_mesh_ids)),
            tuple(sorted(str(value) for value in self.locked_mesh_ids)),
            str(self.solo_mesh_id or ""),
            self.render_flags.signature(),
            self.transform_sig,
            anim_sig,
            tuple(sorted((str(key), str(value))
                         for key, value in (self.material_params or {}).items())),
            tuple(sorted((str(key), repr(value))
                         for key, value in (self.material_overrides or {}).items())),
            tuple(sorted((str(key), float(value))
                         for key, value in (self.morph_weights or {}).items())),
        )


# ──────────────────────────────────────────────────────────────────────────
# 便捷工厂：空 payload（用于占位/降级场景）
# ──────────────────────────────────────────────────────────────────────────

def empty_light_payload() -> LightPayload:
    """构造一个全黑、无 IBL 的 LightPayload（Scene3D 无 Light3D 连接时降级用）。

    P2 重构后单灯光结构：默认 directional、零强度、无 IBL。Scene3D 在没有
    连接任何 Light3D 节点时用它填充，保证 shader 不报错（灯光数组至少有一项）。
    """
    return LightPayload(
        light_type=LIGHT_TYPE_DIRECTIONAL,
        dir=(0.0, 1.0, 0.0),
        pos=(0.0, 0.0, 0.0),
        color=(0.0, 0.0, 0.0),
        intensity=0.0,
        range=10.0,
        inner_cone=30.0,
        outer_cone=45.0,
        icon_pos=(0.0, 0.0, 0.0),
        irradiance_cube=None,
        prefilter_cube=None,
        brdf_lut=None,
        env_intensity=0.0,
        use_ibl=False,
    )
