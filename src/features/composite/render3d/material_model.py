"""Material Model — F4：专业材质编辑数据层（纯 Python）。

设计目标（master_plan §7 / §21.7）：
- 把「导入材质」（scene_graph.MaterialData）升级为「可编辑材质」schema。
- 完整 PBR 字段：base color / metallic / roughness / specular / ior / normal
  strength / emissive / opacity / alpha mode / uv transform / two sided / cull。
- TextureSlot：贴图槽描述（路径/颜色空间/通道映射/UV set/tiling-offset-rotation）。
- MaterialOverride：用户编辑产生的材质覆盖（不改原始导入材质，非破坏）。
- MaterialLibrary：材质预设库（保存/加载常用材质配置）。

与 scene_graph.MaterialData 的关系：
- MaterialData 是 loader 产出的「导入态」材质（精简）。
- 本模块的 MaterialEditModel 是「编辑态」材质（完整 schema），从 MaterialData 派生。
- MaterialOverride 描述「编辑 vs 导入」的差异，可序列化保存到工程文件。

纯 Python / numpy，无 GL/PyQt 依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
import os
import time
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import numpy as np


# ──────────────────────────────────────────────────────────────────────
# 颜色空间常量
# ──────────────────────────────────────────────────────────────────────
COLOR_SPACE_SRGB = "srgb"        # 颜色贴图（baseColor/emissive）：上传时 sRGB→linear
COLOR_SPACE_LINEAR = "linear"    # 数据贴图（normal/ORM/AO）：线性
COLOR_SPACE_RAW = "raw"          # 原始数据（HDR/displacement）：不转换

# Alpha 混合模式（glTF alphaMode）
ALPHA_MODE_OPAQUE = "opaque"     # 不透明
ALPHA_MODE_MASK = "mask"         # alpha test（alphaCutoff 裁剪）
ALPHA_MODE_BLEND = "blend"       # alpha 混合（半透明）

# Cull 模式
CULL_MODE_BACK = "back"          # 剔除背面（默认，闭网格）
CULL_MODE_FRONT = "front"        # 剔除正面
CULL_MODE_NONE = "none"          # 双面

# Normal map 格式
NORMAL_FORMAT_OPENGL = "opengl"  # +Y up（GL 默认）
NORMAL_FORMAT_DIRECTX = "directx"  # -Y up（需翻转 G 通道）

# 通道打包约定（ORM 贴图）
ORM_LAYOUT_GLTF = "gltf"       # R=AO, G=Roughness, B=Metallic
ORM_LAYOUT_UNREAL = "unreal"   # R=AO, G=Roughness, B=Metallic（同 glTF，别名）


@dataclass
class UVTransform:
    """贴图 UV 变换（offset/scale/rotation）。

    变换顺序：先缩放、再旋转、再平移（与 GLSL texture transform 约定一致）。
    uv' = (uv * scale, rotated) + offset。
    """
    offset_u: float = 0.0
    offset_v: float = 0.0
    scale_u: float = 1.0
    scale_v: float = 1.0
    rotation: float = 0.0  # 弧度

    def to_matrix(self) -> np.ndarray:
        """返回 3x3 UV 变换矩阵（齐次 UV = (u,v,1)）。

        M = T · R · S（先缩放、再旋转、再平移）。
        展开后：
            [ su·c,  -sv·s, ou ]
            [ su·s,   sv·c, ov ]
            [ 0,      0,    1  ]
        """
        c = np.cos(self.rotation)
        s = np.sin(self.rotation)
        su, sv = float(self.scale_u), float(self.scale_v)
        return np.array([
            [su * c, -sv * s, self.offset_u],
            [su * s,  sv * c, self.offset_v],
            [0.0,     0.0,    1.0],
        ], dtype=np.float32)

    def signature(self) -> tuple:
        return (float(self.offset_u), float(self.offset_v),
                float(self.scale_u), float(self.scale_v), float(self.rotation))


@dataclass
class TextureSlot:
    """贴图槽描述（master_plan §21.7 F4-4）。

    扩展 TextureDescriptor：颜色空间/通道映射/UV set/UV 变换/丢失标记。

    Attributes:
        path: 贴图文件绝对路径（空=无贴图）。
        color_space: COLOR_SPACE_* 常量。
        uv_set: 使用的 UV 集（0=主 UV，1=第二 UV，如 lightmap）。
        uv_transform: UV 变换（tiling/offset/rotation）。
        channel_mapping: 通道重映射（如 {"r":"roughness"}，ORM 拆分用）。
        is_missing: 文件是否存在（UI 显示警告）。
        resolution: (w,h) 分辨率（UI 显示，懒填）。
    """
    path: str = ""
    color_space: str = COLOR_SPACE_LINEAR
    uv_set: int = 0
    uv_transform: UVTransform = field(default_factory=UVTransform)
    channel_mapping: Dict[str, str] = field(default_factory=dict)
    is_missing: bool = False
    resolution: tuple = (0, 0)

    def is_empty(self) -> bool:
        return not self.path

    def signature(self) -> tuple:
        return (self.path, self.color_space, int(self.uv_set),
                self.uv_transform.signature(),
                tuple(sorted(self.channel_mapping.items())),
                bool(self.is_missing))


@dataclass
class MaterialEditModel:
    """完整 PBR 材质编辑模型（master_plan §7.2 / §21.7）。

    从导入材质 MaterialData 派生，增加专业编辑字段。所有纹理用 TextureSlot 描述。
    用户编辑产生 MaterialOverride（不改本对象的导入态字段）。

    标量字段：
        base_color: (3,) RGB 线性基色。
        metallic: 金属度 [0,1]。
        roughness: 粗糙度 [0,1]。
        specular: 高光强度（非金属反射率缩放）。
        ior: 折射率（默认 1.5=玻璃）。
        normal_strength: 法线贴图强度（默认 1.0）。
        bump_scale: 凹凸贴图高度缩放。
        displacement_scale: 置换强度。
        emissive: (3,) 自发光颜色。
        emissive_intensity: 自发光强度。
        opacity: 不透明度 [0,1]。
        alpha_mode: ALPHA_MODE_* 常量。
        alpha_cutoff: alpha test 阈值（MASK 模式用）。
        transmission: 透射（玻璃材质）。
        clearcoat: 清漆层强度。
        clearcoat_roughness: 清漆粗糙度。
        sheen: 织物光泽。
        subsurface: 次表面散射强度。

    标志字段：
        two_sided: 双面渲染。
        cull_mode: CULL_MODE_* 常量。
    """
    # PBR 标量
    base_color: np.ndarray = field(default_factory=lambda: np.array([0.8, 0.8, 0.8], dtype=np.float32))
    metallic: float = 0.0
    roughness: float = 0.5
    specular: float = 0.5
    ior: float = 1.5
    normal_strength: float = 1.0
    bump_scale: float = 1.0
    displacement_scale: float = 1.0
    emissive: np.ndarray = field(default_factory=lambda: np.array([0.0, 0.0, 0.0], dtype=np.float32))
    emissive_intensity: float = 1.0
    opacity: float = 1.0
    alpha_mode: str = ALPHA_MODE_OPAQUE
    alpha_cutoff: float = 0.5
    transmission: float = 0.0
    clearcoat: float = 0.0
    clearcoat_roughness: float = 0.03
    sheen: float = 0.0
    subsurface: float = 0.0
    # 标志
    two_sided: bool = True
    cull_mode: str = CULL_MODE_BACK
    # 法线贴图格式
    normal_format: str = NORMAL_FORMAT_OPENGL
    orm_layout: str = ORM_LAYOUT_GLTF
    # 纹理槽
    base_color_slot: TextureSlot = field(default_factory=lambda: TextureSlot(color_space=COLOR_SPACE_SRGB))
    normal_slot: TextureSlot = field(default_factory=TextureSlot)
    orm_slot: TextureSlot = field(default_factory=TextureSlot)
    ao_slot: TextureSlot = field(default_factory=TextureSlot)
    emissive_slot: TextureSlot = field(default_factory=lambda: TextureSlot(color_space=COLOR_SPACE_SRGB))
    metallic_slot: TextureSlot = field(default_factory=TextureSlot)
    roughness_slot: TextureSlot = field(default_factory=TextureSlot)
    bump_slot: TextureSlot = field(default_factory=TextureSlot)
    displacement_slot: TextureSlot = field(default_factory=TextureSlot)
    # 材质元数据
    name: str = ""
    material_id: str = ""

    def __post_init__(self) -> None:
        self.base_color = np.asarray(self.base_color, dtype=np.float32).reshape(3)
        self.emissive = np.asarray(self.emissive, dtype=np.float32).reshape(3)

    def has_any_texture(self) -> bool:
        """是否含任何贴图槽。"""
        return not all(slot.is_empty() for slot in self.all_slots())

    def all_slots(self) -> List[TextureSlot]:
        """返回所有贴图槽列表（按固定顺序）。"""
        return [self.base_color_slot, self.normal_slot, self.orm_slot,
                self.ao_slot, self.emissive_slot, self.metallic_slot,
                self.roughness_slot, self.bump_slot, self.displacement_slot]

    def signature(self) -> tuple:
        """材质签名（缓存/比较用）。"""
        return (
            "mat_edit_v1",
            self.base_color.tobytes(),
            float(self.metallic), float(self.roughness), float(self.specular),
            float(self.ior), float(self.normal_strength),
            self.emissive.tobytes(), float(self.emissive_intensity),
            float(self.opacity), str(self.alpha_mode), float(self.alpha_cutoff),
            float(self.transmission), float(self.clearcoat), float(self.sheen),
            bool(self.two_sided), str(self.cull_mode),
            str(self.normal_format), str(self.orm_layout),
            tuple(s.signature() for s in self.all_slots()),
        )


# ──────────────────────────────────────────────────────────────────────
# 从导入材质派生编辑模型
# ──────────────────────────────────────────────────────────────────────

def edit_model_from_material_data(mat, name: str = "") -> MaterialEditModel:
    """从 scene_graph.MaterialData 派生 MaterialEditModel（非破坏）。

    把导入态的标量字段 + TextureDescriptor 复制到编辑态 schema。
    新增字段（specular/ior/clearcoat 等）用合理默认值。

    Args:
        mat: scene_graph.MaterialData（导入态）。
        name: 材质名（UI 显示）。

    Returns:
        MaterialEditModel：可编辑的完整 schema。
    """
    model = MaterialEditModel(name=name)
    # 标量
    model.base_color = np.asarray(mat.base_color, dtype=np.float32).reshape(3).copy()
    model.metallic = float(mat.metallic)
    model.roughness = float(mat.roughness)
    model.opacity = float(getattr(mat, "alpha", 1.0))
    model.two_sided = bool(getattr(mat, "two_sided", True))
    model.emissive = np.asarray(mat.emissive, dtype=np.float32).reshape(3).copy()
    # 纹理槽（TextureDescriptor.path → TextureSlot.path，推断颜色空间）
    model.base_color_slot.path = mat.base_color_tex.path
    model.base_color_slot.color_space = COLOR_SPACE_SRGB
    model.normal_slot.path = mat.normal_tex.path
    model.normal_slot.color_space = COLOR_SPACE_LINEAR
    model.orm_slot.path = mat.orm_tex.path
    model.orm_slot.color_space = COLOR_SPACE_LINEAR
    model.ao_slot.path = mat.ao_tex.path
    model.ao_slot.color_space = COLOR_SPACE_LINEAR
    model.emissive_slot.path = mat.emissive_tex.path
    model.emissive_slot.color_space = COLOR_SPACE_SRGB
    return model


# ──────────────────────────────────────────────────────────────────────
# 材质覆盖（MaterialOverride）— 非破坏编辑
# ──────────────────────────────────────────────────────────────────────

@dataclass
class MaterialOverride:
    """用户编辑产生的材质覆盖（master_plan §21.7 F4-7）。

    只记录「与导入材质不同」的字段。合并时 override 优先，未覆盖的用导入值。
    可序列化保存到工程文件（Material3DNode 参数）。

    Attributes:
        target_material_id: 要覆盖的材质 id（mesh 引用的 material）。
        target_material_name: 要覆盖的材质名（模糊匹配 fallback）。
        overrides: {字段名: 值} 覆盖字典。
        slot_overrides: {槽名: TextureSlot} 贴图槽覆盖。
    """
    target_material_id: str = ""
    target_material_name: str = ""
    overrides: Dict[str, Any] = field(default_factory=dict)
    slot_overrides: Dict[str, TextureSlot] = field(default_factory=dict)

    def set_scalar(self, field_name: str, value: Any) -> None:
        """设置标量/颜色覆盖。"""
        self.overrides[field_name] = value

    def set_slot(self, slot_name: str, slot: TextureSlot) -> None:
        """设置贴图槽覆盖。"""
        self.slot_overrides[slot_name] = slot

    def is_identity(self) -> bool:
        """是否为空覆盖（无任何修改）。"""
        return not self.overrides and not self.slot_overrides

    def signature(self) -> tuple:
        return (
            self.target_material_id, self.target_material_name,
            tuple(sorted((k, str(v)) for k, v in self.overrides.items())),
            tuple((k, v.signature()) for k, v in sorted(self.slot_overrides.items())),
        )


# Stable slot names are shared by the editor, project serialization and the
# render bridge.  Keeping this list in one place prevents a UI-only slot from
# silently being ignored by the render thread.
MATERIAL_SLOT_NAMES: Tuple[str, ...] = (
    "base_color_slot", "normal_slot", "orm_slot", "ao_slot",
    "emissive_slot", "metallic_slot", "roughness_slot", "bump_slot",
    "displacement_slot",
)


def texture_slot_to_dict(slot: TextureSlot) -> Dict[str, Any]:
    """Return a JSON-safe representation of one texture slot."""
    return {
        "path": str(slot.path or ""),
        "color_space": str(slot.color_space or COLOR_SPACE_LINEAR),
        "uv_set": int(slot.uv_set),
        "uv_transform": {
            "offset_u": float(slot.uv_transform.offset_u),
            "offset_v": float(slot.uv_transform.offset_v),
            "scale_u": float(slot.uv_transform.scale_u),
            "scale_v": float(slot.uv_transform.scale_v),
            "rotation": float(slot.uv_transform.rotation),
        },
        "channel_mapping": {
            str(key): str(value)
            for key, value in slot.channel_mapping.items()
        },
    }


def texture_slot_from_dict(value: Any) -> TextureSlot:
    """Parse a texture slot defensively; malformed fields use safe defaults."""
    if not isinstance(value, Mapping):
        return TextureSlot()
    raw_uv = value.get("uv_transform", {})
    if not isinstance(raw_uv, Mapping):
        raw_uv = {}

    def _float(key: str, default: float) -> float:
        try:
            return float(raw_uv.get(key, default))
        except (TypeError, ValueError):
            return default

    try:
        uv_set = max(0, int(value.get("uv_set", 0)))
    except (TypeError, ValueError):
        uv_set = 0
    mapping = value.get("channel_mapping", {})
    if not isinstance(mapping, Mapping):
        mapping = {}
    color_space = str(value.get("color_space", COLOR_SPACE_LINEAR))
    if color_space not in (COLOR_SPACE_SRGB, COLOR_SPACE_LINEAR, COLOR_SPACE_RAW):
        color_space = COLOR_SPACE_LINEAR
    return TextureSlot(
        path=str(value.get("path", "") or ""),
        color_space=color_space,
        uv_set=uv_set,
        uv_transform=UVTransform(
            offset_u=_float("offset_u", 0.0),
            offset_v=_float("offset_v", 0.0),
            scale_u=_float("scale_u", 1.0),
            scale_v=_float("scale_v", 1.0),
            rotation=_float("rotation", 0.0),
        ),
        channel_mapping={str(key): str(item) for key, item in mapping.items()},
    )


def material_override_to_dict(override: MaterialOverride) -> Dict[str, Any]:
    """Serialize a non-destructive material override to project JSON data.

    Scalar overrides may hold Python lists/tuples (the material panel writes
    ``base_color`` as a plain list, see material_panel.py) as well as numpy
    scalars/arrays.  Dropping any of these types would silently lose the
    user's edit on project save/reload, so every supported type is mapped to
    a JSON-safe representation.
    """
    scalar_values: Dict[str, Any] = {}
    for key, value in override.overrides.items():
        if isinstance(value, np.ndarray):
            scalar_values[str(key)] = value.tolist()
        elif isinstance(value, (np.floating, np.integer)):
            scalar_values[str(key)] = value.item()
        elif isinstance(value, (list, tuple)):
            # list/tuple 覆盖（base_color/emissive 等）转 JSON-safe list；
            # 元素可能是 numpy 标量（tolist/面板混用），统一规约为 Python 标量。
            scalar_values[str(key)] = [
                item.item() if isinstance(item, (np.floating, np.integer)) else item
                for item in value
            ]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            scalar_values[str(key)] = value
    return {
        "target_material_id": str(override.target_material_id or ""),
        "target_material_name": str(override.target_material_name or ""),
        "overrides": scalar_values,
        "slot_overrides": {
            str(key): texture_slot_to_dict(slot)
            for key, slot in override.slot_overrides.items()
            if key in MATERIAL_SLOT_NAMES
        },
    }


def material_override_from_dict(value: Any) -> MaterialOverride:
    """Deserialize one override while ignoring unknown/unsafe fields."""
    if not isinstance(value, Mapping):
        return MaterialOverride()
    raw_overrides = value.get("overrides", {})
    if not isinstance(raw_overrides, Mapping):
        raw_overrides = {}
    override = MaterialOverride(
        target_material_id=str(value.get("target_material_id", "") or ""),
        target_material_name=str(value.get("target_material_name", "") or ""),
    )
    for key, item in raw_overrides.items():
        key = str(key)
        if key in {
            "base_color", "metallic", "roughness", "specular", "ior",
            "normal_strength", "bump_scale", "displacement_scale",
            "emissive", "emissive_intensity", "opacity", "alpha_mode",
            "alpha_cutoff", "transmission", "clearcoat",
            "clearcoat_roughness", "sheen", "subsurface", "two_sided",
            "cull_mode", "normal_format", "orm_layout",
        }:
            override.set_scalar(key, item)
    raw_slots = value.get("slot_overrides", {})
    if isinstance(raw_slots, Mapping):
        for key, item in raw_slots.items():
            key = str(key)
            if key in MATERIAL_SLOT_NAMES and isinstance(item, Mapping):
                override.set_slot(key, texture_slot_from_dict(item))
    return override


def decode_material_overrides(value: Any) -> Tuple[Dict[str, MaterialOverride], Tuple[str, ...]]:
    """Decode the Model3D ``material_overrides`` parameter.

    The persisted shape is ``{material_id: override_dict}``.  A list of
    override dictionaries is also accepted for forward compatibility.  The
    second return value contains stable diagnostic codes for invalid input;
    malformed entries are skipped instead of making a render fail.
    """
    if value is None or value == "":
        return {}, ()
    raw = value
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, ("material.overrides_invalid_json",)
    decoded: Dict[str, MaterialOverride] = {}
    diagnostics: List[str] = []
    if isinstance(raw, list):
        entries: Iterable[Tuple[str, Any]] = (
            (str(item.get("target_material_id", "")), item)
            for item in raw if isinstance(item, Mapping)
        )
    elif isinstance(raw, Mapping):
        entries = ((str(key), item) for key, item in raw.items())
    else:
        return {}, ("material.overrides_invalid_shape",)
    for key, item in entries:
        override = material_override_from_dict(item)
        material_key = (override.target_material_id or key).strip()
        if not material_key or override.is_identity():
            if item not in (None, {}, {"overrides": {}, "slot_overrides": {}}):
                diagnostics.append("material.override_missing_target")
            continue
        if not override.target_material_id:
            override.target_material_id = material_key
        decoded[material_key] = override
    return decoded, tuple(dict.fromkeys(diagnostics))


def encode_material_overrides(overrides: Mapping[str, MaterialOverride]) -> str:
    """Serialize material overrides deterministically for a node parameter."""
    payload = {
        str(key): material_override_to_dict(value)
        for key, value in sorted(overrides.items())
        if isinstance(value, MaterialOverride) and not value.is_identity()
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def material_id_for(mat: Any, fallback: str = "") -> str:
    """Return a stable material slot id, falling back to the mesh node id."""
    material_id = str(getattr(mat, "material_id", "") or "").strip()
    if material_id:
        return material_id
    material_name = str(getattr(mat, "name", "") or "").strip()
    return material_name or str(fallback or "material")


# ── AUD-8904：texture stat TTL 缓存 ──────────────────────────────────
# material_summary 在"render-thread-safe"路径上对每个贴图槽做 os.path.isfile
# 磁盘 stat；网络盘/冷缓存时每材质 5 slot 的 stat 会阻塞调用线程。missing
# 标记只服务于面板诊断，短窗口 staleness 可接受——按 (路径 → (时刻, 存在))
# 缓存 stat 结果，TTL 过期才重新 stat。
_TEXTURE_STAT_TTL_S: float = 2.0
_texture_stat_cache: Dict[str, Tuple[float, bool]] = {}


def _path_exists_cached(path: str, ttl_s: float = _TEXTURE_STAT_TTL_S) -> bool:
    """os.path.isfile 的 TTL 缓存版（诊断专用，勿用于正确性关键路径）。

    Args:
        path: 要检测的路径（空字符串直接返回 False，不触碰缓存/磁盘）。
        ttl_s: 缓存有效期秒数；<=0 等价于直连 os.path.isfile。

    Returns:
        路径是否作为文件存在（TTL 窗口内可能滞后于磁盘真实状态）。
    """
    if not path:
        return False
    now = time.monotonic()
    cached = _texture_stat_cache.get(path)
    if cached is not None and ttl_s > 0 and (now - cached[0]) < ttl_s:
        return cached[1]
    exists = bool(os.path.isfile(path))
    _texture_stat_cache[path] = (now, exists)
    return exists


def clear_texture_stat_cache() -> None:
    """清空 texture stat TTL 缓存（测试用；生产代码无需主动调用）。"""
    _texture_stat_cache.clear()


def material_summary(mat: Any, material_id: str = "", model_dir: str = "",
                     mesh_count: int = 1) -> Dict[str, Any]:
    """Build the render-thread-safe summary consumed by the material panel.

    AUD-8904：``missing`` 标记经 :func:`_path_exists_cached` 的 TTL 缓存取值
    （默认 2s），避免每帧每槽位的磁盘 stat 阻塞调用线程；代价是贴图刚创建/
    删除后最多一个 TTL 窗口内面板标记可能滞后。
    """
    resolved_id = material_id or material_id_for(mat)
    slot_values: Dict[str, Dict[str, Any]] = {}
    slot_map = {
        "base_color": ("base_color_slot", "base_color_tex"),
        "normal": ("normal_slot", "normal_tex"),
        "orm": ("orm_slot", "orm_tex"),
        "ao": ("ao_slot", "ao_tex"),
        "emissive": ("emissive_slot", "emissive_tex"),
    }
    for label, attrs in slot_map.items():
        slot = getattr(mat, attrs[0], None)
        if slot is None:
            slot = getattr(mat, attrs[1], TextureSlot())
        path = str(getattr(slot, "path", "") or "")
        resolved_path = path
        if path and model_dir and not os.path.isabs(path):
            resolved_path = os.path.join(model_dir, path)
        role = str(getattr(slot, "role", "") or "")
        default_space = (COLOR_SPACE_SRGB if role == "color"
                         or label in ("base_color", "emissive")
                         else COLOR_SPACE_LINEAR)
        slot_values[label] = {
            "path": path,
            "color_space": str(getattr(slot, "color_space", default_space)),
            "missing": bool(path and not _path_exists_cached(resolved_path)),
        }
    base_color = np.asarray(getattr(mat, "base_color", (0.8, 0.8, 0.8)), dtype=np.float32)
    emissive = np.asarray(getattr(mat, "emissive", (0.0, 0.0, 0.0)), dtype=np.float32)
    return {
        "material_id": resolved_id,
        "name": str(getattr(mat, "name", "") or resolved_id),
        "mesh_count": max(1, int(mesh_count)),
        "base_color": base_color[:3].tolist(),
        "metallic": float(getattr(mat, "metallic", 0.0)),
        "roughness": float(getattr(mat, "roughness", 0.5)),
        "emissive": emissive[:3].tolist(),
        "opacity": float(getattr(mat, "alpha", 1.0)),
        "two_sided": bool(getattr(mat, "two_sided", True)),
        "slots": slot_values,
    }


def apply_override_to_material_data(mat: Any, override: MaterialOverride) -> Any:
    """Apply supported editor fields to a renderable ``MaterialData`` copy.

    ``MaterialEditModel`` intentionally contains more fields than the current
    shader.  This bridge maps the fields currently rendered by Scene3D and
    preserves extra values as attributes for later shader upgrades.
    """
    result = copy.deepcopy(mat)
    for field_name, value in override.overrides.items():
        if field_name in ("base_color", "emissive"):
            setattr(result, field_name, np.asarray(value, dtype=np.float32).reshape(3))
        elif field_name == "opacity":
            result.alpha = max(0.0, min(1.0, float(value)))
        elif hasattr(result, field_name) or field_name in {
                "specular", "ior", "normal_strength", "bump_scale",
                "displacement_scale", "emissive_intensity", "alpha_mode",
                "alpha_cutoff", "transmission", "clearcoat",
                "clearcoat_roughness", "sheen", "subsurface", "cull_mode",
                "normal_format", "orm_layout"}:
            setattr(result, field_name, value)
    from .scene_graph import (
        TEXTURE_ROLE_COLOR, TEXTURE_ROLE_DATA, TextureDescriptor,
    )
    role_by_slot = {
        "base_color_slot": TEXTURE_ROLE_COLOR,
        "emissive_slot": TEXTURE_ROLE_COLOR,
        "normal_slot": TEXTURE_ROLE_DATA,
        "orm_slot": TEXTURE_ROLE_DATA,
        "ao_slot": TEXTURE_ROLE_DATA,
    }
    descriptor_attrs = {
        "base_color_slot": "base_color_tex",
        "normal_slot": "normal_tex",
        "orm_slot": "orm_tex",
        "ao_slot": "ao_tex",
        "emissive_slot": "emissive_tex",
    }
    for slot_name, slot in override.slot_overrides.items():
        if slot_name not in descriptor_attrs:
            continue
        setattr(result, descriptor_attrs[slot_name], TextureDescriptor(
            path=slot.path,
            role=role_by_slot.get(slot_name, TEXTURE_ROLE_DATA),
            tex_coord_set=int(slot.uv_set),
        ))
    return result


def apply_override(model: MaterialEditModel,
                   override: MaterialOverride) -> MaterialEditModel:
    """把 MaterialOverride 应用到 MaterialEditModel，返回新模型（非破坏）。

    master_plan §21.7：override 优先，未覆盖的保留原值。
    """
    import copy
    result = copy.deepcopy(model)
    for field_name, value in override.overrides.items():
        if hasattr(result, field_name):
            if field_name in ("base_color", "emissive"):
                setattr(result, field_name, np.asarray(value, dtype=np.float32).reshape(3))
            else:
                setattr(result, field_name, value)
    for slot_name, slot in override.slot_overrides.items():
        if hasattr(result, slot_name):
            setattr(result, slot_name, copy.deepcopy(slot))
    return result


# ──────────────────────────────────────────────────────────────────────
# 材质预设库
# ──────────────────────────────────────────────────────────────────────

@dataclass
class MaterialPreset:
    """材质预设（master_plan §21.7 F4-8 材质库）。"""
    name: str
    category: str = "basic"  # basic/metal/glass/fabric/etc
    model: MaterialEditModel = field(default_factory=MaterialEditModel)

    def to_dict(self) -> dict:
        """序列化为 dict（保存到材质库 JSON）。"""
        m = self.model
        return {
            "name": self.name,
            "category": self.category,
            "base_color": m.base_color.tolist(),
            "metallic": m.metallic,
            "roughness": m.roughness,
            "specular": m.specular,
            "ior": m.ior,
            "opacity": m.opacity,
            "alpha_mode": m.alpha_mode,
            "two_sided": m.two_sided,
            "emissive": m.emissive.tolist(),
            "emissive_intensity": m.emissive_intensity,
        }


def default_material_presets() -> List[MaterialPreset]:
    """内置材质预设（UI 快速选择）。"""
    presets = []
    # 哑光塑料
    m = MaterialEditModel(name="Matte Plastic")
    m.base_color = np.array([0.7, 0.7, 0.7], dtype=np.float32)
    m.metallic = 0.0
    m.roughness = 0.8
    presets.append(MaterialPreset("Matte Plastic", "basic", m))
    # 光滑金属
    m = MaterialEditModel(name="Polished Metal")
    m.base_color = np.array([0.95, 0.95, 0.95], dtype=np.float32)
    m.metallic = 1.0
    m.roughness = 0.1
    presets.append(MaterialPreset("Polished Metal", "metal", m))
    # 粗糙金属（铸铁）
    m = MaterialEditModel(name="Rough Iron")
    m.base_color = np.array([0.5, 0.5, 0.5], dtype=np.float32)
    m.metallic = 1.0
    m.roughness = 0.7
    presets.append(MaterialPreset("Rough Iron", "metal", m))
    # 玻璃
    m = MaterialEditModel(name="Glass")
    m.base_color = np.array([0.9, 0.9, 0.9], dtype=np.float32)
    m.metallic = 0.0
    m.roughness = 0.05
    m.opacity = 0.3
    m.alpha_mode = ALPHA_MODE_BLEND
    m.ior = 1.5
    m.transmission = 0.9
    presets.append(MaterialPreset("Glass", "glass", m))
    # 织物
    m = MaterialEditModel(name="Fabric")
    m.base_color = np.array([0.4, 0.3, 0.2], dtype=np.float32)
    m.metallic = 0.0
    m.roughness = 0.9
    m.sheen = 0.5
    presets.append(MaterialPreset("Fabric", "fabric", m))
    return presets
