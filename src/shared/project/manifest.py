"""
Topos Manifest - .topos 工程包元数据。

manifest.json 位于 .topos ZIP 包根目录，包含工程基本信息：
- 格式标识和版本
- 工程 ID 和模式
- 主要素材和时间线
- 功能特性列表

设计原则:
- 不可变数据类
- 简洁的元数据（完整数据在 project.json）
- 用于快速识别工程类型和版本
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
import time


# Schema v3 constants
TOPOS_SCHEMA_VERSION = 3

# P2-13: 编辑内容 schema 当前版本（载荷契约：assets/timelines/effects 等）。
# 由 feature 侧 project_migration 注册的迁移链定义到该版本；共享层只持数值，
# 避免反向依赖。v1 = 无版本标记的历史工程。
CURRENT_EDIT_CONTENT_SCHEMA_VERSION = 4
PROJECT_MODE_STANDALONE_IMAGE = "standalone_image"
PROJECT_MODE_EDIT_PROJECT = "edit_project"

# ProjectMode 类型定义 (schema v3)
ProjectMode = Literal[
    "standalone_image",  # 单张图片调色工程
    "edit_project",      # 多素材剪辑工程
    "composite_shot",    # 以合成为核心的镜头工程
    "mixed_project",     # 同时使用剪辑、调色、合成、导出
]


@dataclass(frozen=True)
class ToposManifest:
    """
    .topos 工程包元数据（manifest.json）。

    manifest.json 位于 ZIP 包根目录，用于快速识别工程类型和版本。
    完整工程数据存储在 project.json 和各子目录中。

    Attributes:
        format: 格式标识（固定为 "topos_project_archive"）
        schema_version: Schema 版本号（从 1 开始）
        created_by: 创建软件标识
        project_id: 工程唯一 ID（UUID）
        project_mode: 工程使用形态
        features: 功能特性列表
        primary_asset_id: 主要素材 ID（单素材工程）
        active_timeline_id: 当前活动时间线 ID
        created_at: 创建时间戳
        modified_at: 最后修改时间戳
        last_saved_by: 最后保存时的 product_mode（Phase 3B）
        required_features: 打开工程时必须启用的功能列表（Phase 3B）
        optional_features: 可选增强功能列表（Phase 3B）
        present_modules: 当前存在的模块标识列表（Phase 3B）
    """
    format: str = "topos_project_archive"
    schema_version: int = TOPOS_SCHEMA_VERSION
    created_by: str = "Topos Color"
    project_id: str = ""
    project_mode: ProjectMode = "standalone_image"
    features: tuple[str, ...] = ()
    primary_asset_id: str | None = None
    active_timeline_id: str | None = None
    created_at: float = 0.0
    modified_at: float = 0.0
    # Phase 3B additions
    last_saved_by: str = ""
    required_features: tuple[str, ...] = ()
    optional_features: tuple[str, ...] = ()
    present_modules: tuple[str, ...] = ()
    # P2-13: 编辑内容 schema 版本（assets/timelines/effects 等载荷契约）。
    # 与包传输版本 schema_version 正交；缺省 1 = 无版本标记的历史工程。
    # 常量定义在 edit 侧 project_migration，此处仅给导入兜底默认值，
    # 避免共享层反向依赖 feature 模块。
    edit_content_schema_version: int = 1

    def __post_init__(self) -> None:
        """验证 manifest 数据有效性"""
        if self.format != "topos_project_archive":
            raise ValueError(f"Invalid format: {self.format}")
        if self.schema_version < 1:
            raise ValueError(f"Invalid schema_version: {self.schema_version}")
        if not self.project_id:
            raise ValueError("project_id cannot be empty")
        valid_modes = ("standalone_image", "edit_project", "composite_shot", "mixed_project")
        if self.project_mode not in valid_modes:
            raise ValueError(f"Invalid project_mode: {self.project_mode}")

    # ==================== 序列化 ====================

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "format": self.format,
            "schema_version": self.schema_version,
            "created_by": self.created_by,
            "project_id": self.project_id,
            "project_mode": self.project_mode,
            "features": list(self.features),
            "primary_asset_id": self.primary_asset_id,
            "active_timeline_id": self.active_timeline_id,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "last_saved_by": self.last_saved_by,
            "required_features": list(self.required_features),
            "optional_features": list(self.optional_features),
            "present_modules": list(self.present_modules),
            "edit_content_schema_version": self.edit_content_schema_version,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ToposManifest:
        """从字典反序列化（带旧版迁移）"""
        # 迁移旧版 project_mode 名称
        project_mode = data.get("project_mode", "standalone_image")
        if project_mode == "single_asset":
            project_mode = "standalone_image"

        # v2 → v3 auto-migration: bump schema version silently.
        # v3 semantics (Topos Color in default graph, no standalone video,
        # explicit comp clip) are already enforced at runtime; this just
        # updates the persisted version number on load.
        schema_version = data.get("schema_version", 1)
        if schema_version < TOPOS_SCHEMA_VERSION:
            schema_version = TOPOS_SCHEMA_VERSION

        return ToposManifest(
            format=data.get("format", "topos_project_archive"),
            schema_version=schema_version,
            created_by=data.get("created_by", "Topos Color"),
            project_id=data.get("project_id", ""),
            project_mode=project_mode,
            features=tuple(data.get("features", [])),
            primary_asset_id=data.get("primary_asset_id"),
            active_timeline_id=data.get("active_timeline_id"),
            created_at=data.get("created_at", 0.0),
            modified_at=data.get("modified_at", 0.0),
            last_saved_by=data.get("last_saved_by", ""),
            required_features=tuple(data.get("required_features", [])),
            optional_features=tuple(data.get("optional_features", [])),
            present_modules=tuple(data.get("present_modules", [])),
            edit_content_schema_version=int(
                data.get("edit_content_schema_version", 1) or 1),
        )

    # ==================== 工厂方法 ====================

    @staticmethod
    def create(
        project_id: str,
        project_mode: ProjectMode = "standalone_image",
        features: tuple[str, ...] = (),
        primary_asset_id: str | None = None,
        active_timeline_id: str | None = None,
        last_saved_by: str = "",
        required_features: tuple[str, ...] = (),
        optional_features: tuple[str, ...] = (),
        present_modules: tuple[str, ...] = (),
    ) -> ToposManifest:
        """
        创建新的 Manifest。

        Args:
            project_id: 工程唯一 ID
            project_mode: 工程模式
            features: 功能特性列表
            primary_asset_id: 主要素材 ID
            last_saved_by: 最后保存时的 product_mode
            required_features: 打开工程时必须启用的功能
            optional_features: 可选增强功能
            present_modules: 当前存在的模块标识

        Returns:
            新的 ToposManifest 实例
        """
        now = time.time()
        return ToposManifest(
            project_id=project_id,
            project_mode=project_mode,
            features=features or ("media_pool", "color"),
            primary_asset_id=primary_asset_id,
            active_timeline_id=active_timeline_id,
            created_at=now,
            modified_at=now,
            last_saved_by=last_saved_by,
            required_features=required_features,
            optional_features=optional_features,
            present_modules=present_modules,
            edit_content_schema_version=CURRENT_EDIT_CONTENT_SCHEMA_VERSION,
        )

    def with_modified(self) -> ToposManifest:
        """创建新实例，更新修改时间"""
        return ToposManifest(
            format=self.format,
            schema_version=self.schema_version,
            created_by=self.created_by,
            project_id=self.project_id,
            project_mode=self.project_mode,
            features=self.features,
            primary_asset_id=self.primary_asset_id,
            active_timeline_id=self.active_timeline_id,
            created_at=self.created_at,
            modified_at=time.time(),
            last_saved_by=self.last_saved_by,
            required_features=self.required_features,
            optional_features=self.optional_features,
            present_modules=self.present_modules,
            edit_content_schema_version=self.edit_content_schema_version,
        )

    def with_active_timeline(self, timeline_id: str | None) -> ToposManifest:
        """创建新实例，更新活动时间线"""
        return ToposManifest(
            format=self.format,
            schema_version=self.schema_version,
            created_by=self.created_by,
            project_id=self.project_id,
            project_mode=self.project_mode,
            features=self.features,
            primary_asset_id=self.primary_asset_id,
            active_timeline_id=timeline_id,
            created_at=self.created_at,
            modified_at=time.time(),
            last_saved_by=self.last_saved_by,
            required_features=self.required_features,
            optional_features=self.optional_features,
            present_modules=self.present_modules,
            edit_content_schema_version=self.edit_content_schema_version,
        )

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"ToposManifest(project_id={self.project_id!r}, "
            f"project_mode={self.project_mode!r}, "
            f"schema_version={self.schema_version})"
        )