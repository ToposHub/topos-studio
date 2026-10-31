# P1-25：能力矩阵同时作为导入前置检查和 loader backend 的生产契约。
"""
F11 格式能力矩阵 + 可插拔 loader backend 接口（纯 Python，无重依赖）。

设计目标：
- FormatCapability：每种模型格式（glTF/FBX/OBJ/USD/Alembic/...）支持哪些 feature
  （mesh/material/skeleton/animation/camera/light/morph/instancing）。
- FORMAT_CAPABILITIES：格式 → 能力矩阵（查表，UI 和 loader 据此判断「能导什么」）。
- ModelLoaderBackend：可插拔 loader 接口（ABC）。trimesh/pyassimp/未来 USD/Alembic
  各实现一个 Backend，注册到 LOADER_REGISTRY，load_model 按 extension 分发。
- detect_format / supported_extensions / can_import：UI 拖拽前快速判断。

关联模块：
- model_loader.py：现有 _load_trimesh/_load_assimp 可逐步重构为 Backend 实现；
  本模块定义接口，不强制立即迁移（向后兼容）。
- model_import_report.py：Backend 加载后产 ModelImportReport。
- node_editor_actions.py：拖拽导入时 detect_format 判断是否支持。

约定：
- 能力分三档：CAP_FULL（完整支持）/ CAP_PARTIAL（尽力，可能丢）/ CAP_NONE（不支持）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from .model_import_report import (
    LOADER_BACKEND_ASSIMP_CLI_GLB,
    LOADER_BACKEND_PYASSIMP,
    LOADER_BACKEND_TRIMESH,
    LOADER_BACKEND_UNKNOWN,
)


# ──────────────────────────────────────────────────────────────────────
# 能力等级
# ──────────────────────────────────────────────────────────────────────

CAP_FULL = "full"        # 完整支持
CAP_PARTIAL = "partial"  # 部分支持（尽力提取，可能不完整）
CAP_NONE = "none"        # 不支持

_ALL_CAPS = (CAP_FULL, CAP_PARTIAL, CAP_NONE)


# ──────────────────────────────────────────────────────────────────────
# 能力字段名（矩阵列）
# ──────────────────────────────────────────────────────────────────────

# 这些是 FormatCapability 的字段名，也是 UI 能力矩阵的列。
CAP_MESH = "mesh"
CAP_MATERIAL = "material"
CAP_TEXTURE = "texture"            # 嵌入/外置纹理
CAP_PBR = "pbr"                    # 完整 PBR（metallic/roughness/normal/ORM/emissive）
CAP_SKELETON = "skeleton"          # 骨骼/蒙皮
CAP_SKINNING = "skinning"          # GPU 蒙皮（顶点权重）
CAP_ANIMATION = "animation"        # 骨骼/节点动画
CAP_MORPH = "morph"                # 融合变形/blendshape
CAP_CAMERA = "camera"             # 相机导入
CAP_LIGHT = "light"               # 灯光导入
CAP_INSTANCING = "instancing"      # 实例化（glTF EXT_mesh_gpu_instancing）
CAM_ANIMATION = "node_anim"        # 节点变换动画（与骨骼动画区分）
CAP_HIERARCHY = "hierarchy"        # 场景层级（父子 transform）
CAP_UV = "uv"                      # UV 坐标
CAP_UV2 = "uv2"                    # 第二套 UV（lightmap/AO）

ALL_CAPABILITY_FIELDS = (
    CAP_MESH, CAP_MATERIAL, CAP_TEXTURE, CAP_PBR,
    CAP_SKELETON, CAP_SKINNING, CAP_ANIMATION, CAP_MORPH,
    CAP_CAMERA, CAP_LIGHT, CAP_INSTANCING,
    CAM_ANIMATION, CAP_HIERARCHY, CAP_UV, CAP_UV2,
)


@dataclass(frozen=True)
class FormatCapability:
    """单种格式的能力描述（不可变，可做 dict key）。

    Attributes:
        format_id: 格式标识（"gltf"/"glb"/"fbx"/"obj"/"usd"/"usda"/"usdc"/"usdz"/"abc"/...）。
        extensions: 该格式对应的文件扩展名集合（含点，小写；如 {".gltf"}）。
        backend: 首选 loader backend（LOADER_BACKEND_* 常量；未来可扩展 USD/Alembic）。
        mesh/material/...: 各能力字段，取值 CAP_FULL/PARTIAL/NONE。
        notes: 补充说明（如「FBX 骨骼依赖 pyassimp 可靠性」）。
    """
    format_id: str
    extensions: FrozenSet[str]
    backend: str
    mesh: str = CAP_NONE
    material: str = CAP_NONE
    texture: str = CAP_NONE
    pbr: str = CAP_NONE
    skeleton: str = CAP_NONE
    skinning: str = CAP_NONE
    animation: str = CAP_NONE
    morph: str = CAP_NONE
    camera: str = CAP_NONE
    light: str = CAP_NONE
    instancing: str = CAP_NONE
    node_anim: str = CAP_NONE
    hierarchy: str = CAP_NONE
    uv: str = CAP_NONE
    uv2: str = CAP_NONE
    notes: str = ""

    def capability(self, field_name: str) -> str:
        """按字段名查能力（返回 CAP_*；未知字段返回 CAP_NONE）。"""
        if hasattr(self, field_name) and field_name in ALL_CAPABILITY_FIELDS:
            return getattr(self, field_name)
        return CAP_NONE

    def supports(self, field_name: str, min_level: str = CAP_PARTIAL) -> bool:
        """是否支持某能力（达到 min_level）。

        等级排序：NONE < PARTIAL < FULL。
        """
        order = {CAP_NONE: 0, CAP_PARTIAL: 1, CAP_FULL: 2}
        actual = self.capability(field_name)
        return order.get(actual, 0) >= order.get(min_level, 1)

    def summary(self) -> Dict[str, str]:
        """返回 {field: level} dict（UI 矩阵渲染用）。"""
        return {f: self.capability(f) for f in ALL_CAPABILITY_FIELDS}


@dataclass(frozen=True)
class FormatImportProbe:
    """导入前置检查结果。

    ``can_import`` 是比扩展名判断更严格的运行时结论：它同时考虑格式是否已
    注册、后端依赖是否可用，以及调用方是否要求文件当前存在。该对象只包含
    可跨线程/Qt 信号边界传递的标量，适合直接用于 UI 提示和预检报告。
    """

    path: str
    format_id: Optional[str]
    backend: str
    extension_supported: bool
    backend_available: bool
    file_exists: bool
    can_import: bool
    reason_code: str
    reason: str
    capability: Optional[FormatCapability] = None

    @property
    def ready(self) -> bool:
        """``can_import`` 的语义别名，便于 UI 代码表达意图。"""
        return self.can_import

    @property
    def is_planned(self) -> bool:
        """格式已知但尚未接入 loader backend。"""
        return bool(self.capability and self.backend == LOADER_BACKEND_UNKNOWN)

    def to_dict(self) -> dict:
        """返回可跨线程/Qt/JSON 边界传递的能力探测摘要。"""
        capability = self.capability
        return {
            "path": str(self.path),
            "format_id": (str(self.format_id)
                           if self.format_id is not None else None),
            "backend": str(self.backend),
            "extension_supported": bool(self.extension_supported),
            "backend_available": bool(self.backend_available),
            "file_exists": bool(self.file_exists),
            "can_import": bool(self.can_import),
            "reason_code": str(self.reason_code),
            "reason": str(self.reason),
            "capability": (capability.summary() if capability is not None
                            else None),
            "capability_notes": (str(capability.notes)
                                  if capability is not None else ""),
        }


# ──────────────────────────────────────────────────────────────────────
# 格式能力矩阵（查表）
# ──────────────────────────────────────────────────────────────────────

# glTF / GLB —— 实时 PBR 主力，能力最全
_CAP_GLTF = FormatCapability(
    format_id="gltf",
    extensions=frozenset({".gltf"}),
    backend=LOADER_BACKEND_TRIMESH,
    mesh=CAP_FULL, material=CAP_FULL, texture=CAP_FULL, pbr=CAP_FULL,
    skeleton=CAP_FULL, skinning=CAP_FULL, animation=CAP_FULL,
    morph=CAP_PARTIAL,           # KHR 融合变形扩展，trimesh 提取有限
    camera=CAP_FULL, light=CAP_PARTIAL,  # KHR_lights_punctual 扩展
    instancing=CAP_PARTIAL,       # EXT_mesh_gpu_instancing
    node_anim=CAP_FULL, hierarchy=CAP_FULL,
    uv=CAP_FULL, uv2=CAP_PARTIAL,
    notes="实时 PBR 主力格式；骨骼/动画/相机完整；灯光/morph/instancing 依赖扩展",
)

_CAP_GLB = FormatCapability(
    format_id="glb",
    extensions=frozenset({".glb"}),
    backend=LOADER_BACKEND_TRIMESH,
    mesh=CAP_FULL, material=CAP_FULL, texture=CAP_FULL, pbr=CAP_FULL,
    skeleton=CAP_FULL, skinning=CAP_FULL, animation=CAP_FULL,
    morph=CAP_PARTIAL,
    camera=CAP_FULL, light=CAP_PARTIAL,
    instancing=CAP_PARTIAL,
    node_anim=CAP_FULL, hierarchy=CAP_FULL,
    uv=CAP_FULL, uv2=CAP_PARTIAL,
    notes="glTF 二进制单文件；嵌入纹理自动提取",
)

# FBX —— DCC 互通，但 pyassimp 不稳定
_CAP_FBX = FormatCapability(
    format_id="fbx",
    extensions=frozenset({".fbx"}),
    backend=LOADER_BACKEND_PYASSIMP,
    mesh=CAP_FULL, material=CAP_PARTIAL, texture=CAP_PARTIAL, pbr=CAP_PARTIAL,
    skeleton=CAP_PARTIAL, skinning=CAP_PARTIAL, animation=CAP_PARTIAL,
    morph=CAP_NONE,
    camera=CAP_PARTIAL, light=CAP_PARTIAL,
    instancing=CAP_NONE,
    node_anim=CAP_PARTIAL, hierarchy=CAP_PARTIAL,
    uv=CAP_FULL, uv2=CAP_PARTIAL,
    notes="DCC 互通主力；但 pyassimp/libassimp 不稳定，骨骼/动画可能丢失；"
          "CLI fallback 会静态化（丢骨骼动画）",
)

# OBJ —— 静态 mesh fallback
_CAP_OBJ = FormatCapability(
    format_id="obj",
    extensions=frozenset({".obj"}),
    backend=LOADER_BACKEND_TRIMESH,
    mesh=CAP_FULL, material=CAP_PARTIAL, texture=CAP_PARTIAL, pbr=CAP_NONE,
    skeleton=CAP_NONE, skinning=CAP_NONE, animation=CAP_NONE,
    morph=CAP_NONE,
    camera=CAP_NONE, light=CAP_NONE,
    instancing=CAP_NONE,
    node_anim=CAP_NONE, hierarchy=CAP_PARTIAL,
    uv=CAP_FULL, uv2=CAP_NONE,
    notes="静态 mesh fallback；无骨骼/动画/PBR；MTL 材质基础",
)

# STL —— 纯几何（3D 打印）
_CAP_STL = FormatCapability(
    format_id="stl",
    extensions=frozenset({".stl"}),
    backend=LOADER_BACKEND_TRIMESH,
    mesh=CAP_FULL, material=CAP_NONE, texture=CAP_NONE, pbr=CAP_NONE,
    skeleton=CAP_NONE, skinning=CAP_NONE, animation=CAP_NONE,
    morph=CAP_NONE,
    camera=CAP_NONE, light=CAP_NONE,
    instancing=CAP_NONE,
    node_anim=CAP_NONE, hierarchy=CAP_NONE,
    uv=CAP_NONE, uv2=CAP_NONE,
    notes="纯几何（3D 打印）；无材质/UV/动画",
)

# PLY —— 点云/彩色 mesh
_CAP_PLY = FormatCapability(
    format_id="ply",
    extensions=frozenset({".ply"}),
    backend=LOADER_BACKEND_TRIMESH,
    mesh=CAP_FULL, material=CAP_PARTIAL, texture=CAP_NONE, pbr=CAP_NONE,
    skeleton=CAP_NONE, skinning=CAP_NONE, animation=CAP_NONE,
    morph=CAP_NONE,
    camera=CAP_NONE, light=CAP_NONE,
    instancing=CAP_NONE,
    node_anim=CAP_NONE, hierarchy=CAP_NONE,
    uv=CAP_PARTIAL, uv2=CAP_NONE,
    notes="点云/彩色顶点 mesh；顶点色支持；无骨骼/动画",
)

# DAE (Collada)
_CAP_DAE = FormatCapability(
    format_id="dae",
    extensions=frozenset({".dae"}),
    backend=LOADER_BACKEND_TRIMESH,
    mesh=CAP_FULL, material=CAP_PARTIAL, texture=CAP_PARTIAL, pbr=CAP_NONE,
    skeleton=CAP_PARTIAL, skinning=CAP_PARTIAL, animation=CAP_PARTIAL,
    morph=CAP_NONE,
    camera=CAP_PARTIAL, light=CAP_PARTIAL,
    instancing=CAP_NONE,
    node_anim=CAP_PARTIAL, hierarchy=CAP_PARTIAL,
    uv=CAP_FULL, uv2=CAP_NONE,
    notes="Collada；trimesh 能解析但能力不如 glTF 完整",
)

# 3DS
_CAP_3DS = FormatCapability(
    format_id="3ds",
    extensions=frozenset({".3ds"}),
    backend=LOADER_BACKEND_PYASSIMP,
    mesh=CAP_PARTIAL, material=CAP_PARTIAL, texture=CAP_PARTIAL, pbr=CAP_NONE,
    skeleton=CAP_NONE, skinning=CAP_NONE, animation=CAP_NONE,
    morph=CAP_NONE,
    camera=CAP_NONE, light=CAP_NONE,
    instancing=CAP_NONE,
    node_anim=CAP_NONE, hierarchy=CAP_PARTIAL,
    uv=CAP_PARTIAL, uv2=CAP_NONE,
    notes="老旧格式；仅基础几何/材质",
)

# blend (Blender) —— pyassimp 尽力
_CAP_BLEND = FormatCapability(
    format_id="blend",
    extensions=frozenset({".blend"}),
    backend=LOADER_BACKEND_PYASSIMP,
    mesh=CAP_PARTIAL, material=CAP_PARTIAL, texture=CAP_PARTIAL, pbr=CAP_NONE,
    skeleton=CAP_PARTIAL, skinning=CAP_PARTIAL, animation=CAP_PARTIAL,
    morph=CAP_NONE,
    camera=CAP_PARTIAL, light=CAP_PARTIAL,
    instancing=CAP_NONE,
    node_anim=CAP_PARTIAL, hierarchy=CAP_PARTIAL,
    uv=CAP_PARTIAL, uv2=CAP_NONE,
    notes="Blender 原生格式；pyassimp 解析不稳定；建议导出 glTF",
)

# USD（USDA/USDC/USDZ）—— 尚未实现，标记为 planned
_CAP_USD = FormatCapability(
    format_id="usd",
    extensions=frozenset({".usd", ".usda", ".usdc", ".usdz"}),
    backend=LOADER_BACKEND_UNKNOWN,  # 未来：LOADER_BACKEND_USD
    mesh=CAP_NONE, material=CAP_NONE, texture=CAP_NONE, pbr=CAP_NONE,
    skeleton=CAP_NONE, skinning=CAP_NONE, animation=CAP_NONE,
    morph=CAP_NONE,
    camera=CAP_NONE, light=CAP_NONE,
    instancing=CAP_NONE,
    node_anim=CAP_NONE, hierarchy=CAP_NONE,
    uv=CAP_NONE, uv2=CAP_NONE,
    notes="Planned：影视镜头级场景交换；需 USD Python API（pxr）",
)

# Alembic (.abc) —— 尚未实现
_CAP_ALEMBIC = FormatCapability(
    format_id="abc",
    extensions=frozenset({".abc"}),
    backend=LOADER_BACKEND_UNKNOWN,  # 未来：LOADER_BACKEND_ALEMBIC
    mesh=CAP_NONE, material=CAP_NONE, texture=CAP_NONE, pbr=CAP_NONE,
    skeleton=CAP_NONE, skinning=CAP_NONE, animation=CAP_NONE,
    morph=CAP_NONE,
    camera=CAP_NONE, light=CAP_NONE,
    instancing=CAP_NONE,
    node_anim=CAP_NONE, hierarchy=CAP_NONE,
    uv=CAP_NONE, uv2=CAP_NONE,
    notes="Planned：几何缓存/变形动画；需 Alembic Python 绑定",
)


# 能力矩阵注册表（format_id → FormatCapability）
FORMAT_CAPABILITIES: Dict[str, FormatCapability] = {
    fc.format_id: fc for fc in [
        _CAP_GLTF, _CAP_GLB, _CAP_FBX, _CAP_OBJ, _CAP_STL, _CAP_PLY,
        _CAP_DAE, _CAP_3DS, _CAP_BLEND, _CAP_USD, _CAP_ALEMBIC,
    ]
}


# ──────────────────────────────────────────────────────────────────────
# 查询工具
# ──────────────────────────────────────────────────────────────────────


def detect_format(path: str) -> Optional[FormatCapability]:
    """从文件扩展名推断格式能力。

    Args:
        path: 文件路径（取扩展名，小写）。

    Returns:
        FormatCapability 或 None（不支持的扩展名）。
    """
    import os
    ext = os.path.splitext(path)[1].lower()
    if not ext:
        return None
    for fc in FORMAT_CAPABILITIES.values():
        if ext in fc.extensions:
            return fc
    return None


def supported_extensions() -> Set[str]:
    """所有已注册格式支持的扩展名集合（含点，小写）。"""
    out: Set[str] = set()
    for fc in FORMAT_CAPABILITIES.values():
        out |= fc.extensions
    return out


def importable_extensions() -> Set[str]:
    """当前可导入的扩展名（backend != UNKNOWN）。

    planned 格式（USD/Alembic）的扩展名不在其中（UI 灰显）。
    """
    out: Set[str] = set()
    for fc in FORMAT_CAPABILITIES.values():
        if fc.backend != LOADER_BACKEND_UNKNOWN:
            out |= fc.extensions
    return out


def can_import(path: str, *, check_runtime: bool = False,
               require_exists: bool = False) -> bool:
    """判断文件是否可导入。

    默认保留历史的静态查询语义（只看扩展名和能力矩阵），因此可以用于构造
    文件选择器的过滤列表。需要做真实导入前检查时传入 ``check_runtime=True``；
    ``require_exists=True`` 还会检查当前路径是否为文件。
    """
    fc = detect_format(path)
    if fc is None or fc.backend == LOADER_BACKEND_UNKNOWN:
        return False
    if check_runtime or require_exists:
        return probe_import(path, require_exists=require_exists).can_import
    return True


# ──────────────────────────────────────────────────────────────────────
# F11-4：可插拔 loader backend 接口
# ──────────────────────────────────────────────────────────────────────


class ModelLoaderBackend:
    """可插拔模型 loader backend 接口（ABC）。

    现有 _load_trimesh / _load_assimp 是函数式实现；未来可包装成本接口的实现类，
    注册到 LOADER_REGISTRY，load_model 按 extension 分发。

    子类必须实现：
        - extensions(): 该 backend 处理的扩展名集合
        - load(path, normalize): 返回 SceneGraphRoot（含 import_report）
        - is_available(): 依赖是否已装（trimesh/pyassimp/pxr/...）
    """

    @property
    def backend_id(self) -> str:
        """Backend 标识（LOADER_BACKEND_* 常量；子类覆盖）。"""
        return LOADER_BACKEND_UNKNOWN

    def extensions(self) -> Set[str]:
        """该 backend 处理的扩展名集合（含点，小写）。子类必须实现。"""
        raise NotImplementedError

    def is_available(self) -> bool:
        """依赖是否已安装（软依赖检查）。子类必须实现。"""
        raise NotImplementedError

    def load(self, path: str, normalize: bool = False):
        """加载模型，返回 SceneGraphRoot（含 import_report）。

        子类必须实现。失败应 raise ModelLoaderUnavailable 或 ModelLoadError。
        """
        raise NotImplementedError

    def can_handle(self, path: str) -> bool:
        """是否处理该文件（扩展名匹配 + 依赖可用）。"""
        import os
        ext = os.path.splitext(path)[1].lower()
        return ext in self.extensions() and self.is_available()


# Backend 注册表（backend_id → ModelLoaderBackend 实例）
LOADER_REGISTRY: Dict[str, ModelLoaderBackend] = {}


def register_loader_backend(backend: ModelLoaderBackend) -> None:
    """注册一个 loader backend 到全局注册表。

    Args:
        backend: ModelLoaderBackend 实例。
    """
    LOADER_REGISTRY[backend.backend_id] = backend


class _BuiltinLoaderBackend(ModelLoaderBackend):
    """把现有函数式 loader 暴露为统一 backend 接口。

    这里故意采用延迟导入：能力矩阵会被 UI 和纯 Python 预检复用，而
    ``model_loader`` 自身又依赖场景数据结构。这样注册 backend 不会在 UI 启动
    时强制加载 trimesh/pyassimp，也不会形成模块导入环。
    """

    def __init__(self, backend_id: str, extensions: Set[str],
                 availability: Callable[[], bool]) -> None:
        self._backend_id = backend_id
        self._extensions = frozenset(extensions)
        self._availability = availability

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def extensions(self) -> Set[str]:
        return set(self._extensions)

    def is_available(self) -> bool:
        try:
            return bool(self._availability())
        except Exception:
            return False

    def load(self, path: str, normalize: bool = False):
        from .model_loader import load_model
        return load_model(path, normalize=normalize)


def _trimesh_backend_available() -> bool:
    """延迟查询 trimesh 后端，避免能力查询在导入时产生重依赖。"""
    try:
        from .model_loader import trimesh_available
        return bool(trimesh_available())
    except Exception:
        return False


def _assimp_backend_available() -> bool:
    """pyassimp 或 assimp CLI 任一可用即可走当前 FBX fallback。"""
    try:
        from .model_loader import assimp_available
        if assimp_available():
            return True
    except Exception:
        pass
    return shutil.which("assimp") is not None


def ensure_builtin_loader_backends() -> None:
    """注册内建 trimesh/assimp backend（幂等且不覆盖用户 backend）。"""
    builtin_specs = (
        (LOADER_BACKEND_TRIMESH,
         {ext for fc in FORMAT_CAPABILITIES.values()
          if fc.backend == LOADER_BACKEND_TRIMESH for ext in fc.extensions},
         _trimesh_backend_available),
        (LOADER_BACKEND_PYASSIMP,
         {ext for fc in FORMAT_CAPABILITIES.values()
          if fc.backend == LOADER_BACKEND_PYASSIMP for ext in fc.extensions},
         _assimp_backend_available),
    )
    for backend_id, extensions, availability in builtin_specs:
        if backend_id not in LOADER_REGISTRY:
            register_loader_backend(
                _BuiltinLoaderBackend(backend_id, extensions, availability))


def find_backend_for_path(path: str) -> Optional[ModelLoaderBackend]:
    """按文件扩展名找已注册的可用 backend。

    Args:
        path: 文件路径。

    Returns:
        第个能处理该文件的 backend，或 None（无匹配/依赖未装）。
    """
    ensure_builtin_loader_backends()
    for backend in LOADER_REGISTRY.values():
        if backend.can_handle(path):
            return backend
    return None


def _backend_is_available(backend_id: str) -> bool:
    """查询 backend 运行时依赖，不向上层泄漏导入异常。"""
    ensure_builtin_loader_backends()
    backend = LOADER_REGISTRY.get(backend_id)
    if backend is not None:
        return backend.is_available()
    if backend_id == LOADER_BACKEND_TRIMESH:
        return _trimesh_backend_available()
    if backend_id == LOADER_BACKEND_PYASSIMP:
        return _assimp_backend_available()
    if backend_id == LOADER_BACKEND_ASSIMP_CLI_GLB:
        return shutil.which("assimp") is not None
    return False


def probe_import(path: str, *, require_exists: bool = True) -> FormatImportProbe:
    """执行一次真实导入前检查，不读取或修改模型内容。

    ``reason_code`` 是稳定的机器可读诊断码：
    ``format.unknown``、``format.planned``、``asset.missing``、
    ``backend.unavailable`` 或 ``ready``。调用方可据此决定是阻止拖入、提示
    安装依赖，还是允许后台 loader 继续解析。
    """
    normalized_path = os.fspath(path) if path else ""
    capability = detect_format(normalized_path)
    file_exists = bool(normalized_path and os.path.isfile(normalized_path))

    if capability is None:
        return FormatImportProbe(
            path=normalized_path, format_id=None, backend=LOADER_BACKEND_UNKNOWN,
            extension_supported=False, backend_available=False,
            file_exists=file_exists, can_import=False,
            reason_code="format.unknown", reason="未注册的模型文件格式。")

    if capability.backend == LOADER_BACKEND_UNKNOWN:
        return FormatImportProbe(
            path=normalized_path, format_id=capability.format_id,
            backend=capability.backend, extension_supported=True,
            backend_available=False, file_exists=file_exists, can_import=False,
            reason_code="format.planned",
            reason="该格式已列入能力矩阵，但尚未接入可用的导入后端。",
            capability=capability)

    backend_available = _backend_is_available(capability.backend)
    if require_exists and not file_exists:
        return FormatImportProbe(
            path=normalized_path, format_id=capability.format_id,
            backend=capability.backend, extension_supported=True,
            backend_available=backend_available, file_exists=False,
            can_import=False, reason_code="asset.missing",
            reason="模型文件不存在或不是普通文件。", capability=capability)

    if not backend_available:
        return FormatImportProbe(
            path=normalized_path, format_id=capability.format_id,
            backend=capability.backend, extension_supported=True,
            backend_available=False, file_exists=file_exists, can_import=False,
            reason_code="backend.unavailable",
            reason="当前 Python 环境或系统中没有可用的模型导入后端。",
            capability=capability)

    return FormatImportProbe(
        path=normalized_path, format_id=capability.format_id,
        backend=capability.backend, extension_supported=True,
        backend_available=True, file_exists=file_exists, can_import=True,
        reason_code="ready", reason="格式、后端和文件路径均已就绪。",
        capability=capability)


def runtime_importable_extensions() -> Set[str]:
    """返回当前运行环境真正具备后端的扩展名集合。"""
    ensure_builtin_loader_backends()
    out: Set[str] = set()
    for fc in FORMAT_CAPABILITIES.values():
        if fc.backend != LOADER_BACKEND_UNKNOWN and _backend_is_available(fc.backend):
            out |= fc.extensions
    return out


@dataclass(frozen=True)
class AssetPathResolution:
    """项目资源路径解析结果（纯查询，无文件系统写入）。"""

    stored_path: str
    resolved_path: Optional[str]
    status: str
    candidates: Tuple[str, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.resolved_path is not None


def resolve_asset_path(stored_path: str, *, project_dir: Optional[str] = None,
                       search_roots: Iterable[str] = ()) -> AssetPathResolution:
    """解析绝对/项目相对/重链接资源路径。

    查找顺序固定为：原始路径、项目目录下的相对路径、各搜索根下的相对路径，
    最后才尝试各搜索根下的 basename。这样既能恢复项目移动后的相对资源，
    也不会用模糊 basename 结果覆盖更精确的路径。
    """
    raw = os.fspath(stored_path) if stored_path else ""
    if not raw:
        return AssetPathResolution(stored_path="", resolved_path=None,
                                   status="missing")

    roots = tuple(os.path.abspath(os.path.expanduser(os.fspath(root)))
                  for root in search_roots if root)
    candidates: List[str] = []

    def add(candidate: str) -> None:
        normalized = os.path.abspath(os.path.expanduser(candidate))
        if normalized not in candidates:
            candidates.append(normalized)

    expanded_raw = os.path.expanduser(raw)
    if os.path.isabs(expanded_raw):
        add(expanded_raw)
    elif project_dir:
        add(os.path.join(os.path.expanduser(os.fspath(project_dir)), expanded_raw))
    add(expanded_raw)
    for root in roots:
        add(os.path.join(root, expanded_raw))

    basename = os.path.basename(expanded_raw)
    if basename != expanded_raw:
        for root in roots:
            add(os.path.join(root, basename))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return AssetPathResolution(
                stored_path=raw, resolved_path=candidate, status="found",
                candidates=tuple(candidates))
    return AssetPathResolution(stored_path=raw, resolved_path=None,
                               status="missing", candidates=tuple(candidates))


# ──────────────────────────────────────────────────────────────────────
# F11-5：场景单位 / 坐标轴 / 时间码统一
# ──────────────────────────────────────────────────────────────────────

# 坐标轴约定（up 轴）
AXIS_Y_UP = "y_up"    # glTF 默认（Y up, -Z forward）
AXIS_Z_UP = "z_up"    # FBX/3ds Max 默认（Z up）
AXIS_AUTO = "auto"    # 自动（loader 判断；多数情况按 Y up）

# 常见文件单位 → 米的缩放系数
UNIT_METERS = 1.0
UNIT_CENTIMETERS = 0.01     # FBX 默认 cm
UNIT_MILLIMETERS = 0.001
UNIT_INCHES = 0.0254
UNIT_FEET = 0.3048

_UNIT_TO_METER = {
    "meter": UNIT_METERS, "m": UNIT_METERS, "meters": UNIT_METERS,
    "centimeter": UNIT_CENTIMETERS, "cm": UNIT_CENTIMETERS,
    "millimeter": UNIT_MILLIMETERS, "mm": UNIT_MILLIMETERS,
    "inch": UNIT_INCHES, "in": UNIT_INCHES,
    "foot": UNIT_FEET, "ft": UNIT_FEET,
}


def unit_to_meter(unit: str) -> float:
    """文件单位名 → 米缩放系数（未知返回 1.0）。"""
    return _UNIT_TO_METER.get(unit.strip().lower(), UNIT_METERS)


@dataclass
class SceneUnitConverter:
    """场景单位 / 坐标轴 / 时间码转换器（F11-5）。

    不同格式的单位/轴向/帧率不一致；本工具统一到项目内部约定：
    - 单位：米（unit_scale 应用到所有顶点/平移）
    - 轴向：Y up（Z up 源需翻转 Y/Z 轴）
    - 帧率：fps 参数（动画时间码 → 秒）

    Attributes:
        source_unit: 源文件单位（"cm"/"mm"/"m"/...）。
        source_axis: 源文件轴向（AXIS_Y_UP/AXIS_Z_UP/AXIS_AUTO）。
        source_fps: 源文件帧率（动画关键帧用；0=按秒存储，不转换）。
        target_fps: 目标帧率（合成时间轴；默认 24）。
    """
    source_unit: str = "meter"
    source_axis: str = AXIS_AUTO
    source_fps: float = 0.0
    target_fps: float = 24.0

    @property
    def unit_scale(self) -> float:
        """单位缩放系数（源单位 → 米）。"""
        return unit_to_meter(self.source_unit)

    @property
    def needs_axis_flip(self) -> bool:
        """是否需要翻转 Y/Z 轴（源 Z up → 目标 Y up）。"""
        return self.source_axis == AXIS_Z_UP

    def frame_to_seconds(self, frame: float) -> float:
        """源文件帧号 → 秒（按 source_fps；source_fps<=0 时 frame 视为秒）。"""
        if self.source_fps <= 0.0:
            return float(frame)
        return float(frame) / self.source_fps

    def seconds_to_target_frame(self, seconds: float) -> float:
        """秒 → 目标帧号（按 target_fps）。"""
        return float(seconds) * self.target_fps

    def convert_frame(self, source_frame: float) -> float:
        """源帧号 → 目标帧号（完整链：frame→秒→目标帧）。"""
        sec = self.frame_to_seconds(source_frame)
        return self.seconds_to_target_frame(sec)

    def convert_vertices(self, vertices):
        """顶点数组单位/轴向转换（返回新数组）。

        Args:
            vertices: (N,3) numpy array（源空间）。

        Returns:
            (N,3) float32 转换后顶点（单位米 + Y up）。
        """
        import numpy as np
        v = np.asarray(vertices, dtype=np.float32).copy()
        if self.needs_axis_flip:
            # Z up → Y up：绕 X 轴 -90° 旋转（x,y,z → x,z,-y）。
            # AUD-88010：旧的纯分量交换 [0,2,1] 是行列式 -1 的镜像——
            # 旧 +Y 映射到新 +Z（前后颠倒）、三角形绕序反转、光照法线
            # 反向。旋转 (x, z, -y) 保持右手系（det=+1）：旧 +Z（Z-up
            # 的上方向）→ 新 +Y，旧 +Y（前向）→ 新 -Z（OpenGL 相机看向
            # -Z，即"前"）。
            v = v[:, [0, 2, 1]] * np.array([1.0, 1.0, -1.0], dtype=np.float32)
        if self.unit_scale != 1.0:
            v *= self.unit_scale
        return v

    def signature(self) -> tuple:
        """转换参数签名（cache key 用）。"""
        return (
            self.source_unit, self.source_axis,
            float(self.source_fps), float(self.target_fps),
        )
