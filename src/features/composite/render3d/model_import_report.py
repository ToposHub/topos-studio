"""ModelImportReport — 模型导入报告数据结构（F0：资产能力审计）。

设计目标：
- 纯 Python 数据结构（dataclass），无 GL / PyQt 依赖，可独立单测。
- 每次 loader 导入模型时生成，结构化记录「导入了什么 / 丢了什么 / 用了哪个 backend」。
- 挂到 ``SceneGraphRoot.import_report``，由 Model3DNode 传到 UI 面板显示。

字段参考 master_plan §3.3 ModelImportReport 示例：
    source_path / loader_backend / mesh_count / triangle_count /
    material_count / texture_count / embedded_textures_extracted /
    skeleton_count / bone_count / skin_weight_count /
    animation_clip_count / camera_count / light_count /
    unit_scale / axis_mode / warnings / lost_features

与 scene_graph.py 的关系：
- SceneGraphRoot 增加可选字段 ``import_report: ModelImportReport | None``。
- loader 在每个 backend 分支构造 report 并填入 root_data.import_report。
- 不破坏旧路径：report 为 None 时按「无报告」处理（向后兼容）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# ──────────────────────────────────────────────────────────────────────
# Backend 标识常量（loader_backend 字段取值）
# ──────────────────────────────────────────────────────────────────────
# 这些常量是稳定字符串标识，UI 和测试可据此判断「用了哪条导入路径」。
LOADER_BACKEND_TRIMESH = "trimesh"            # glTF/GLB/OBJ/STL/PLY/DAE（trimesh）
LOADER_BACKEND_PYASSIMP = "pyassimp"          # FBX/3DS/blend（pyassimp + libassimp）
LOADER_BACKEND_ASSIMP_CLI_GLB = "assimp_cli_glb"  # FBX CLI fallback → GLB
LOADER_BACKEND_UNKNOWN = "unknown"            # 未设置（向后兼容）


@dataclass
class ModelImportReport:
    """结构化导入报告。

    所有计数字段默认 0；warnings / lost_features 默认空列表。
    loader 在解析过程中按实际读取结果填充；失败/降级时把原因写入
    ``warnings`` 或 ``lost_features``，而非抛异常中断。

    Attributes:
        source_path: 被导入文件的绝对路径。
        loader_backend: 实际生效的 backend 名，取值见 LOADER_BACKEND_* 常量。
        mesh_count: 导入的 mesh 数量（叶子几何体）。
        triangle_count: 总三角面数（所有 mesh index_count / 3）。
        vertex_count: 总顶点数（所有 mesh vertices 行数）。
        material_count: 材质数量（含纹理或纯标量）。
        texture_count: 已解析路径的纹理描述符数量。
        morph_target_count: 可用的 blend-shape/morph target 数量。
        embedded_textures_extracted: 从 GLB/FBX 提取并落地的嵌入贴图数。
        skeleton_count: 蒙皮数量（glTF skins 数组长度）。
        bone_count: 所有 skin 的骨骼总数（最大 joints 数）。
        skin_weight_count: 受蒙皮影响的顶点数（有 bone_weights 的 mesh 顶点和）。
        animation_clip_count: 动画片段数。
        camera_count: 场景中的相机数（当前多数 backend 不提取，记 0）。
        light_count: 场景中的灯光数（当前多数 backend 不提取，记 0）。
        unit_scale: 文件单位缩放（1.0=米；FBX 常用 cm=0.01）。
        axis_mode: 坐标轴约定（"auto"/"y_up"/"z_up"）。
        warnings: 非致命问题（如某 mesh 无 UV、某材质贴图路径为空）。
        lost_features: 因 backend 限制丢失的功能（如 FBX CLI fallback 丢了骨骼/动画）。
    """

    source_path: str = ""
    loader_backend: str = LOADER_BACKEND_UNKNOWN
    mesh_count: int = 0
    triangle_count: int = 0
    vertex_count: int = 0
    material_count: int = 0
    texture_count: int = 0
    morph_target_count: int = 0
    embedded_textures_extracted: int = 0
    skeleton_count: int = 0
    bone_count: int = 0
    skin_weight_count: int = 0
    animation_clip_count: int = 0
    camera_count: int = 0
    light_count: int = 0
    unit_scale: float = 1.0
    axis_mode: str = "auto"
    warnings: List[str] = field(default_factory=list)
    lost_features: List[str] = field(default_factory=list)

    def add_warning(self, msg: str) -> None:
        """追加一条警告（去重，保持插入顺序）。"""
        if msg and msg not in self.warnings:
            self.warnings.append(msg)

    def add_lost_feature(self, feature: str) -> None:
        """追加一条丢失功能（去重，保持插入顺序）。"""
        if feature and feature not in self.lost_features:
            self.lost_features.append(feature)

    def summary_dict(self) -> dict:
        """返回扁平 dict（UI 面板展示用）。

        把 warnings / lost_features 也平铺进 dict，方便面板逐行渲染。
        """
        return {
            "source_path": self.source_path,
            "loader_backend": self.loader_backend,
            "mesh_count": self.mesh_count,
            "triangle_count": self.triangle_count,
            "vertex_count": self.vertex_count,
            "material_count": self.material_count,
            "texture_count": self.texture_count,
            "morph_target_count": self.morph_target_count,
            "embedded_textures_extracted": self.embedded_textures_extracted,
            "skeleton_count": self.skeleton_count,
            "bone_count": self.bone_count,
            "skin_weight_count": self.skin_weight_count,
            "animation_clip_count": self.animation_clip_count,
            "camera_count": self.camera_count,
            "light_count": self.light_count,
            "unit_scale": self.unit_scale,
            "axis_mode": self.axis_mode,
            "warnings": list(self.warnings),
            "lost_features": list(self.lost_features),
        }

    @classmethod
    def from_summary_dict(cls, data: object) -> Optional["ModelImportReport"]:
        """Rebuild a report from the render-thread-safe summary payload.

        Render diagnostics cross the Qt signal boundary as plain dictionaries;
        keeping this conversion here gives UI consumers one canonical schema
        and ignores forward-compatible fields added by newer loaders.
        """
        if not isinstance(data, dict):
            return None
        scalar_fields = (
            "source_path", "loader_backend", "mesh_count", "triangle_count",
            "vertex_count", "material_count", "texture_count",
            "morph_target_count",
            "embedded_textures_extracted", "skeleton_count", "bone_count",
            "skin_weight_count", "animation_clip_count", "camera_count",
            "light_count", "unit_scale", "axis_mode",
        )
        values = {key: data[key] for key in scalar_fields if key in data}
        values["warnings"] = list(data.get("warnings") or [])
        values["lost_features"] = list(data.get("lost_features") or [])
        try:
            return cls(**values)
        except (TypeError, ValueError):
            return None

    def has_warnings(self) -> bool:
        """是否有警告或丢失功能（UI 决定是否显示告警图标）。"""
        return bool(self.warnings or self.lost_features)
