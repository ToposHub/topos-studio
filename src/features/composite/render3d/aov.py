"""AOV (Arbitrary Output Variables) / Render Pass — F9 数据层（纯 Python）。

设计目标（master_plan §12 / §21.12）：
- 定义专业 3D 合成所需的多种 render pass（不只是 beauty）。
- 提供 object/material stable id 分配（mattes 用）。
- 定义 object render flags（shadow catcher / holdout / matte，F10 联动）。
- 纯 Python，无 GL/PyQt 依赖，可独立单测。

核心数据结构：
    AOVPass enum — 渲染通道类型（beauty/depth/normal/position/uv/id/motion）
    AOVConfig — 哪些 pass 启用（Scene3D 节点参数）
    ObjectRenderFlags — 每对象的渲染标志（F10 shadow catcher/holdout/matte）
    IDAllocator — 稳定 object/material id 分配（master_plan §21.12 F9-4）

与 scene3d_node.py 的关系：
- Scene3D 读 AOVConfig 决定渲染哪些 pass（多 pass 策略，master_plan §21.12）。
- 每帧把 ObjectRenderFlags 传给 shader（shadow catcher pass 等）。
- object/material id 编码到 RGBA（id pass 用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


# Object render roles are intentionally a small, serializable vocabulary.  The
# detailed ObjectRenderFlags below remains the render-thread contract, while
# nodes expose this role instead of six loosely-related booleans.
RENDER_ROLE_SOLID = "solid"
RENDER_ROLE_HOLDOUT = "holdout"
RENDER_ROLE_SHADOW_CATCHER = "shadow_catcher"
RENDER_ROLE_MATTE = "matte"
RENDER_ROLES = (
    RENDER_ROLE_SOLID,
    RENDER_ROLE_HOLDOUT,
    RENDER_ROLE_SHADOW_CATCHER,
    RENDER_ROLE_MATTE,
)


# ──────────────────────────────────────────────────────────────────────
# AOV 通道类型
# ──────────────────────────────────────────────────────────────────────

class AOVPass(Enum):
    """渲染通道类型（master_plan §12.1）。

    BEAUTY:        主图（完整 PBR + 光照）。
    DEPTH:         线性深度（景深、深度合成）。
    NORMAL:        世界法线（encode 到 [0,1]）。
    POSITION:      世界位置（HDR float）。
    UV:            UV 坐标（debug/后期替换）。
    OBJECT_ID:     对象 id（matte 用，encode 到 RGBA）。
    MATERIAL_ID:   材质 id（matte 用）。
    MOTION_VECTOR: 运动向量（运动模糊，需上一帧矩阵）。
    DIFFUSE:       diffuse 直接+间接（分层合成）。
    SPECULAR:      specular 直接+间接（分层合成）.
    EMISSION:      自发光。
    AO:            环境光遮蔽。
    SHADOW:        阴影（F10 shadow catcher 用）。
    """
    BEAUTY = "beauty"
    DEPTH = "depth"
    NORMAL = "normal"
    POSITION = "position"
    UV = "uv"
    OBJECT_ID = "object_id"
    MATERIAL_ID = "material_id"
    MOTION_VECTOR = "motion_vector"
    DIFFUSE = "diffuse"
    SPECULAR = "specular"
    EMISSION = "emission"
    AO = "ao"
    SHADOW = "shadow"


# pass 的显示标签（UI AOV 选择器用）
AOV_PASS_LABELS = {
    AOVPass.BEAUTY: "Beauty",
    AOVPass.DEPTH: "Depth",
    AOVPass.NORMAL: "Normal",
    AOVPass.POSITION: "Position",
    AOVPass.UV: "UV",
    AOVPass.OBJECT_ID: "Object ID",
    AOVPass.MATERIAL_ID: "Material ID",
    AOVPass.MOTION_VECTOR: "Motion Vector",
    AOVPass.DIFFUSE: "Diffuse",
    AOVPass.SPECULAR: "Specular",
    AOVPass.EMISSION: "Emission",
    AOVPass.AO: "Ambient Occlusion",
    AOVPass.SHADOW: "Shadow",
}

# pass 是否需要 float HDR 纹理（vs 8bit）
AOV_FLOAT_PASSES = {
    AOVPass.POSITION, AOVPass.UV, AOVPass.MOTION_VECTOR, AOVPass.DEPTH,
}

# 第一版支持的 pass（master_plan §21.12 多 pass 策略，第一版容易调试）
AOV_FIRST_BATCH = {
    AOVPass.BEAUTY, AOVPass.DEPTH, AOVPass.NORMAL,
    AOVPass.OBJECT_ID, AOVPass.MATERIAL_ID, AOVPass.MOTION_VECTOR,
}

# Passes currently backed by real Scene3D GPU textures and safe for direct
# export. The remaining enum values are reserved for later shader passes and
# must not be presented as if they were already renderable.
AOV_EXPORTABLE_PASSES = (
    AOVPass.BEAUTY,
    AOVPass.DEPTH,
    AOVPass.NORMAL,
    AOVPass.POSITION,
    AOVPass.UV,
    AOVPass.OBJECT_ID,
    AOVPass.MATERIAL_ID,
    AOVPass.MOTION_VECTOR,
)


@dataclass
class AOVConfig:
    """哪些 AOV pass 启用（master_plan §21.12 F9-1/F9-5）。

    Scene3D 节点参数。beauty 始终启用；其余按需开启（未开不渲染，省性能）。
    """
    beauty: bool = True
    depth: bool = False
    normal: bool = False
    position: bool = False
    uv: bool = False
    object_id: bool = False
    material_id: bool = False
    motion_vector: bool = False
    diffuse: bool = False
    specular: bool = False
    emission: bool = False
    ao: bool = False
    shadow: bool = False

    def enabled_passes(self) -> set:
        """返回启用的 pass 集合。"""
        passes = set()
        if self.beauty: passes.add(AOVPass.BEAUTY)
        if self.depth: passes.add(AOVPass.DEPTH)
        if self.normal: passes.add(AOVPass.NORMAL)
        if self.position: passes.add(AOVPass.POSITION)
        if self.uv: passes.add(AOVPass.UV)
        if self.object_id: passes.add(AOVPass.OBJECT_ID)
        if self.material_id: passes.add(AOVPass.MATERIAL_ID)
        if self.motion_vector: passes.add(AOVPass.MOTION_VECTOR)
        if self.diffuse: passes.add(AOVPass.DIFFUSE)
        if self.specular: passes.add(AOVPass.SPECULAR)
        if self.emission: passes.add(AOVPass.EMISSION)
        if self.ao: passes.add(AOVPass.AO)
        if self.shadow: passes.add(AOVPass.SHADOW)
        return passes

    def enabled_count(self) -> int:
        return len(self.enabled_passes())

    def is_any_extra_enabled(self) -> bool:
        """是否启用了除 beauty 外的 pass。"""
        return self.enabled_count() > 1 or (
            self.enabled_count() == 1 and not self.beauty)

    def signature(self) -> tuple:
        return tuple(sorted(p.value for p in self.enabled_passes()))


# ──────────────────────────────────────────────────────────────────────
# Object / Material ID 分配（master_plan §21.12 F9-4）
# ──────────────────────────────────────────────────────────────────────

def encode_id_to_rgba(object_id: int) -> tuple:
    """把整数 id 编码到 RGBA（id pass 用，GPU readback 后解码）。

    24bit id 拆到 RGB 三通道（每通道 8bit），alpha=1。
    id=0 保留给「无对象/背景」。

    Args:
        object_id: 非负整数（0-16777215）。

    Returns:
        (r, g, b, a) float tuple [0,1]。
    """
    object_id = max(0, min(int(object_id), 0xFFFFFF))
    r = (object_id & 0xFF) / 255.0
    g = ((object_id >> 8) & 0xFF) / 255.0
    b = ((object_id >> 16) & 0xFF) / 255.0
    return (r, g, b, 1.0)


def decode_id_from_rgba(r: float, g: float, b: float) -> int:
    """从 RGBA 解码回整数 id（encode_id_to_rgba 的逆运算）。"""
    ri = int(round(r * 255.0)) & 0xFF
    gi = int(round(g * 255.0)) & 0xFF
    bi = int(round(b * 255.0)) & 0xFF
    return ri | (gi << 8) | (bi << 16)


class IDAllocator:
    """稳定的 object/material id 分配器（master_plan §21.12 F9-4）。

    给每个对象/材质分配唯一整数 id（从 1 开始，0 保留给背景）。
    同一名称多次分配返回相同 id。

    稳定性范围（AUD-8607）：id 按**首次分配顺序**递增，因此只在"同一分配器
    实例 + 相同的场景遍历顺序"下稳定（跨帧一致）。跨会话/跨工程的 id 不做
    任何保证——场景图求值顺序随节点增删、图重排变化时，同一对象会拿到不同
    id。需要跨渲染稳定的 id matte 请用 cryptomatte（按名 murmurhash）。
    """

    def __init__(self) -> None:
        self._name_to_id: Dict[str, int] = {}
        self._next_id: int = 1

    def allocate(self, name: str) -> int:
        """分配或复用 id。name 已存在则返回已分配的 id。"""
        if name in self._name_to_id:
            return self._name_to_id[name]
        oid = self._next_id
        self._name_to_id[name] = oid
        self._next_id += 1
        return oid

    def get_id(self, name: str) -> Optional[int]:
        """查询已分配的 id（不存在返回 None）。"""
        return self._name_to_id.get(name)

    def reset(self) -> None:
        """重置分配器。"""
        self._name_to_id.clear()
        self._next_id = 1

    def count(self) -> int:
        return len(self._name_to_id)


# ──────────────────────────────────────────────────────────────────────
# Object Render Flags（F10 shadow catcher / holdout / matte 联动）
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ObjectRenderFlags:
    """每对象的渲染标志（master_plan §13 / §21.13 F10-1）。

    控制对象在 beauty/depth/matte/shadow pass 中的行为。

    Attributes:
        visible_to_camera: beauty/主图是否渲染该对象。
        visible_to_shadow: 是否参与投影/接收阴影。
        holdout: 遮挡后方 CG（写 depth/matte，beauty 中扣 alpha）。
        shadow_catcher: 接收阴影但自身透明（只输出阴影贡献）。
        matte_object: 输出 mask/id（不显示 beauty）。
        matte_id: matte pass 的 id（用于下游 Grade/Blur/Keyer）。
    """
    visible_to_camera: bool = True
    visible_to_shadow: bool = True
    holdout: bool = False
    shadow_catcher: bool = False
    matte_object: bool = False
    matte_id: int = 0
    shadow_intensity: float = 1.0
    shadow_softness: float = 0.2

    def signature(self) -> tuple:
        return (
            bool(self.visible_to_camera), bool(self.visible_to_shadow),
            bool(self.holdout), bool(self.shadow_catcher),
            bool(self.matte_object), int(self.matte_id),
            round(max(0.0, float(self.shadow_intensity)), 6),
            round(max(0.0, min(1.0, float(self.shadow_softness))), 6),
        )


def render_flags_from_role(role: str = RENDER_ROLE_SOLID,
                           matte_id: int = 0,
                           shadow_intensity: float = 1.0,
                           shadow_softness: float = 0.2) -> ObjectRenderFlags:
    """Build a validated per-object render contract from a node role.

    Keeping this conversion in the pure data layer makes the UI/node schema
    and the renderer agree on the same semantics. Unknown roles deliberately
    fall back to ``solid`` so old projects remain renderable.
    """
    normalized = str(role or RENDER_ROLE_SOLID).strip().lower()
    if normalized == RENDER_ROLE_HOLDOUT:
        return default_holdout_object()
    if normalized == RENDER_ROLE_SHADOW_CATCHER:
        flags = default_ground_shadow_catcher()
        flags.shadow_intensity = max(0.0, float(shadow_intensity))
        flags.shadow_softness = max(0.0, min(1.0, float(shadow_softness)))
        return flags
    if normalized == RENDER_ROLE_MATTE:
        return ObjectRenderFlags(
            visible_to_camera=False,
            visible_to_shadow=False,
            matte_object=True,
            matte_id=max(0, int(matte_id)),
        )
    return ObjectRenderFlags()


def render_role_from_flags(flags: Optional[ObjectRenderFlags]) -> str:
    """Return the canonical node role represented by ``flags``."""
    if flags is None:
        return RENDER_ROLE_SOLID
    if flags.holdout:
        return RENDER_ROLE_HOLDOUT
    if flags.shadow_catcher:
        return RENDER_ROLE_SHADOW_CATCHER
    if flags.matte_object:
        return RENDER_ROLE_MATTE
    return RENDER_ROLE_SOLID


def default_ground_shadow_catcher() -> ObjectRenderFlags:
    """快速创建地面 shadow catcher（master_plan §13.2 Ground Plane Helper）。"""
    return ObjectRenderFlags(
        visible_to_camera=False,  # 自身不显示（透明）
        visible_to_shadow=True,   # 接收阴影
        shadow_catcher=True,
    )


def default_holdout_object() -> ObjectRenderFlags:
    """快速创建 holdout 遮挡对象。"""
    return ObjectRenderFlags(
        visible_to_camera=False,
        visible_to_shadow=True,
        holdout=True,
    )


# ──────────────────────────────────────────────────────────────────────
# F10-3：Catcher 材质（shadow catcher / reflection catcher）
# ──────────────────────────────────────────────────────────────────────

# Catcher 类型常量
CATCHER_TYPE_SHADOW = "shadow"        # 阴影捕手：接收阴影，输出阴影 alpha
CATCHER_TYPE_REFLECTION = "reflection"  # 反射捕手：接收反射
CATCHER_TYPE_CONTACT = "contact"      # 接触阴影：轻量实时 AO


@dataclass
class CatcherMaterialData:
    """捕手材质参数（F10-3 shadow catcher / reflection catcher）。

    用于创建「接收阴影/反射但自身透明」的地面/物体。配合 ObjectRenderFlags
    使用：shadow_catcher=True 的对象用此材质渲染 shadow pass。

    Attributes:
        catcher_type: CATCHER_TYPE_* 常量。
        shadow_color: 阴影颜色（默认黑色；偏蓝可模拟环境光遮挡）。
        shadow_intensity: 阴影强度（0=无阴影，1=全黑；>1 增强对比）。
        falloff: 阴影衰减（0=硬边，1=完全软化；控制 shadow blur）。
        light_wrapping: 光线包裹（阴影边缘渗入环境光色；0=无）。
        ground_plane: 是否作为无限地面（True=忽略 transform，铺满地平面）。
        plane_height: 地面 Y 高度（ground_plane=True 时用）。
        plane_size: 地面尺寸（米；ground_plane=True 时用；0=无限）。
    """
    catcher_type: str = CATCHER_TYPE_SHADOW
    shadow_color: tuple = (0.0, 0.0, 0.0)
    shadow_intensity: float = 1.0
    falloff: float = 0.2
    light_wrapping: float = 0.0
    ground_plane: bool = True
    plane_height: float = 0.0
    plane_size: float = 0.0

    def signature(self) -> tuple:
        """材质参数签名（cache key 用）。"""
        return (
            self.catcher_type,
            tuple(self.shadow_color),
            float(self.shadow_intensity),
            float(self.falloff),
            float(self.light_wrapping),
            bool(self.ground_plane),
            float(self.plane_height),
            float(self.plane_size),
        )


def default_shadow_catcher_material() -> CatcherMaterialData:
    """默认阴影捕手材质（黑色阴影 + 软化 + 地面）。"""
    return CatcherMaterialData(
        catcher_type=CATCHER_TYPE_SHADOW,
        shadow_color=(0.0, 0.0, 0.0),
        shadow_intensity=1.0,
        falloff=0.2,
        ground_plane=True,
    )


def default_reflection_catcher_material() -> CatcherMaterialData:
    """默认反射捕手材质。"""
    return CatcherMaterialData(
        catcher_type=CATCHER_TYPE_REFLECTION,
        shadow_intensity=0.5,  # 反射强度
        falloff=0.1,
        ground_plane=True,
    )


def default_contact_shadow_material() -> CatcherMaterialData:
    """默认接触阴影材质（轻量实时，无地面限制）。"""
    return CatcherMaterialData(
        catcher_type=CATCHER_TYPE_CONTACT,
        shadow_intensity=0.8,
        falloff=0.5,  # 接触阴影更软
        ground_plane=False,
    )


# ──────────────────────────────────────────────────────────────────────
# F10-6：Matte 工作流辅助（merge 指导）
# ──────────────────────────────────────────────────────────────────────

# Matte 输出用途（UI 提示用）
MATTE_USE_MASK = "mask"       # 用作遮罩（下游 Grade/Blur 限定区域）
MATTE_USE_KEYER = "keyer"     # 抠像（绿幕/亮度键）
MATTE_USE_DEPTH = "depth"     # 深度合成
MATTE_USE_ID = "id"           # 对象/材质 id 分离


@dataclass
class MatteWorkflow:
    """Matte 工作流描述（F10-6 文档化；指导用户如何 merge）。

    描述一个 matte 对象的输出如何与 beauty 合成。UI 面板显示步骤说明。
    """
    matte_use: str = MATTE_USE_MASK
    description: str = ""
    merge_node_type: str = "merge"  # 推荐的 merge 节点类型
    merge_operation: str = "multiply"  # over/in/multiply/...

    def signature(self) -> tuple:
        return (self.matte_use, self.merge_operation)


# 常用 matte 工作流预设
MATTE_PRESETS = {
    "shadow_catcher": MatteWorkflow(
        matte_use=MATTE_USE_MASK,
        description="阴影捕手：把 shadow pass 用 multiply 合到实拍地面",
        merge_operation="multiply",
    ),
    "holdout": MatteWorkflow(
        matte_use=MATTE_USE_MASK,
        description="遮挡对象：用 matte mask 扣除 CG 被遮挡部分",
        merge_operation="out",
    ),
    "object_id": MatteWorkflow(
        matte_use=MATTE_USE_ID,
        description="对象 id：用 id matte 分离特定对象做单独调色",
        merge_operation="over",
    ),
    "depth_comp": MatteWorkflow(
        matte_use=MATTE_USE_DEPTH,
        description="深度合成：用 depth matte 做 Z-order 合成",
        merge_operation="over",
    ),
}
