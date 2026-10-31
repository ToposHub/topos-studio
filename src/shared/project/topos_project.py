"""
ToposProject - 统一工程数据模型。

唯一的工程对象，整合 Edit/Color/Composite/Deliver/UI 状态。
所有工程数据统一存储在 .topos ZIP 包中。

设计原则:
- 不可变数据类（frozen=True）确保线程安全
- 完整类型注解
- 工厂方法返回新实例（不修改原实例）
- 支持多种工程模式：single_asset, edit_project, composite_shot, mixed_project

参考文档:
- docs/library_media_pool_project_architecture_plan_2026-05-19.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal
import os
import uuid
import time
import logging

from src.shared.extra_fields import preserves_extras, split_extras

logger = logging.getLogger(__name__)


def is_virtual_asset_path(path: str) -> bool:
    """Return whether *path* names an app-generated, non-file asset."""
    return isinstance(path, str) and path.startswith("topos://")


# ================================================================
# 类型定义
# ================================================================

# ProjectMode 类型定义 (schema v2)
ProjectMode = Literal[
    "standalone_image",  # 单张图片调色工程 (schema v2)
    "edit_project",      # 多素材剪辑工程
    "composite_shot",    # 以合成为核心的镜头工程
    "mixed_project",     # 同时使用剪辑、调色、合成、导出
]

# Feature 类型定义（功能特性）
FeatureType = Literal[
    "media_pool",      # 媒体池
    "color",           # 调色
    "composite",       # 合成
    "edit",            # 剪辑时间线
    "deliver",         # 导出
]


# ================================================================
# MediaAssetRef - 媒体素材引用（简化版）
# ================================================================

@dataclass(frozen=True)
@preserves_extras
class MediaAssetRef:
    """
    媒体素材引用（存储在 project.json 中的简化版）。

    完整的 MediaAsset 数据在 edit/media_pool.json 中，
    这里只保留核心索引字段。

    Attributes:
        asset_id: 唯一标识符（UUID）
        path: 源媒体文件路径
        media_type: 媒体类型（image, video, audio, subtitle, image_sequence, comp_clip）
        display_name: 显示名称
        is_offline: 是否离线（源文件不存在）
    """
    asset_id: str
    path: str
    media_type: str  # image, video, audio, subtitle, image_sequence, comp_clip, nested_sequence, adjustment, solid
    display_name: str
    is_offline: bool = False
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + from_dict 消费键（两者一致，无 legacy 迁移键）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "asset_id", "path", "media_type", "display_name", "is_offline",
    })

    def __post_init__(self) -> None:
        """验证素材数据有效性"""
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if not self.path:
            raise ValueError("path cannot be empty")
        if self.media_type not in (
            "image", "video", "audio", "subtitle", "image_sequence",
            "comp_clip", "nested_sequence", "adjustment", "solid", "motion_graphic",
        ):
            raise ValueError(f"Invalid media_type: {self.media_type}")

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典"""
        return {
            "asset_id": self.asset_id,
            "path": self.path,
            "media_type": self.media_type,
            "display_name": self.display_name,
            "is_offline": self.is_offline,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MediaAssetRef:
        """从字典反序列化"""
        return MediaAssetRef(
            asset_id=data.get("asset_id", ""),
            path=data.get("path", ""),
            media_type=data.get("media_type", "image"),
            display_name=data.get("display_name", ""),
            is_offline=data.get("is_offline", False),
            _extra_fields=split_extras(data, MediaAssetRef._KNOWN_KEYS),
        )

    def __repr__(self) -> str:
        return f"MediaAssetRef(asset_id={self.asset_id!r}, media_type={self.media_type!r})"


# ================================================================
# ProjectBinRef - 素材箱引用（简化版）
# ================================================================

@dataclass(frozen=True)
@preserves_extras
class ProjectBinRef:
    """
    素材箱引用（存储在 project.json 中的简化版）。

    完整的 ProjectBin 数据在 edit/bins.json 中，
    这里只保留核心索引字段。

    Attributes:
        bin_id: 唯一标识符（UUID）
        name: 素材箱名称
        parent_id: 父素材箱 ID（None 表示根级）
        asset_count: 包含的素材数量
    """
    bin_id: str
    name: str
    parent_id: str | None = None
    asset_count: int = 0
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + from_dict 消费键（两者一致，无 legacy 迁移键）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "bin_id", "name", "parent_id", "asset_count",
    })

    def __post_init__(self) -> None:
        """验证素材箱数据有效性"""
        if not self.bin_id:
            raise ValueError("bin_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典"""
        return {
            "bin_id": self.bin_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "asset_count": self.asset_count,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ProjectBinRef:
        """从字典反序列化"""
        return ProjectBinRef(
            bin_id=data.get("bin_id", ""),
            name=data.get("name", ""),
            parent_id=data.get("parent_id"),
            asset_count=data.get("asset_count", 0),
            _extra_fields=split_extras(data, ProjectBinRef._KNOWN_KEYS),
        )


# ================================================================
# TimelineRef - 时间线引用（简化版）
# ================================================================

@dataclass(frozen=True)
@preserves_extras
class TimelineRef:
    """
    时间线引用（存储在 project.json 中的简化版）。

    完整的 TimelineSequence 数据在 edit/timelines.json 中，
    这里只保留核心索引字段。

    Attributes:
        timeline_id: 唯一标识符（UUID）
        name: 时间线名称
        fps: 帧率
        width: 视频宽度
        height: 视频高度
    """
    timeline_id: str
    name: str
    fps: float = 24.0
    width: int = 1920
    height: int = 1080
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + from_dict 消费键（两者一致，无 legacy 迁移键）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "timeline_id", "name", "fps", "width", "height",
    })

    def __post_init__(self) -> None:
        """验证时间线数据有效性"""
        if not self.timeline_id:
            raise ValueError("timeline_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if self.fps <= 0:
            raise ValueError(f"Invalid fps: {self.fps}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid resolution: {self.width}x{self.height}")

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典"""
        return {
            "timeline_id": self.timeline_id,
            "name": self.name,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TimelineRef:
        """从字典反序列化"""
        return TimelineRef(
            timeline_id=data.get("timeline_id", ""),
            name=data.get("name", ""),
            fps=data.get("fps", 24.0),
            width=data.get("width", 1920),
            height=data.get("height", 1080),
            _extra_fields=split_extras(data, TimelineRef._KNOWN_KEYS),
        )

    @property
    def resolution_str(self) -> str:
        """分辨率字符串"""
        return f"{self.width}x{self.height}"


# ================================================================
# ColorStackRef - 调色栈引用（简化版）
# ================================================================

@dataclass(frozen=True)
@preserves_extras
class ColorStackRef:
    """
    调色栈引用（存储在 project.json 中的简化版）。

    完整的 ColorStackData 在 color/color_stacks.json 中，
    这里只保留核心索引字段。

    Attributes:
        stack_id: 调色栈唯一标识符（UUID）
        asset_id: 关联素材 ID（可选）
        name: 调色栈名称
        scope: 调色栈作用域（asset_default, clip_local, timeline_adjustment）
    """
    stack_id: str
    asset_id: str | None = None
    name: str = "Default"
    scope: str = "asset_default"  # asset_default, clip_local, timeline_adjustment
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + from_dict 消费键（两者一致，无 legacy 迁移键）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "stack_id", "asset_id", "name", "scope",
    })

    def __post_init__(self) -> None:
        """验证调色栈数据有效性"""
        if not self.stack_id:
            raise ValueError("stack_id cannot be empty")
        if self.scope not in ("asset_default", "clip_local", "timeline_adjustment"):
            raise ValueError(f"Invalid scope: {self.scope}")

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典"""
        return {
            "stack_id": self.stack_id,
            "asset_id": self.asset_id,
            "name": self.name,
            "scope": self.scope,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ColorStackRef:
        """从字典反序列化"""
        return ColorStackRef(
            stack_id=data.get("stack_id", ""),
            asset_id=data.get("asset_id"),
            name=data.get("name", "Default"),
            scope=data.get("scope", "asset_default"),
            _extra_fields=split_extras(data, ColorStackRef._KNOWN_KEYS),
        )


# ================================================================
# CompositionRef - 合成图引用（简化版）
# ================================================================

@dataclass(frozen=True)
@preserves_extras
class CompositionRef:
    """
    合成图引用（存储在 project.json 中的简化版）。

    完整的 CompositeGraphData 在 composite/graphs.json 中，
    这里只保留核心索引字段。

    Attributes:
        composition_id: 合成图唯一标识符（UUID）
        name: 合成图名称
        source_kind: 来源类型（single_asset, timeline_clip, standalone）
        asset_id: 关联素材 ID（可选）
        timeline_id: 关联时间线 ID（可选）
        clip_id: 关联 Clip ID（可选）
    """
    composition_id: str
    name: str = "Default Composition"
    source_kind: str = "standalone"  # single_asset, timeline_clip, standalone
    asset_id: str | None = None
    timeline_id: str | None = None
    clip_id: str | None = None
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + from_dict 消费键（两者一致，无 legacy 迁移键）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "composition_id", "name", "source_kind",
        "asset_id", "timeline_id", "clip_id",
    })

    def __post_init__(self) -> None:
        """验证合成图数据有效性"""
        if not self.composition_id:
            raise ValueError("composition_id cannot be empty")
        if self.source_kind not in ("single_asset", "timeline_clip", "standalone"):
            raise ValueError(f"Invalid source_kind: {self.source_kind}")

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典"""
        return {
            "composition_id": self.composition_id,
            "name": self.name,
            "source_kind": self.source_kind,
            "asset_id": self.asset_id,
            "timeline_id": self.timeline_id,
            "clip_id": self.clip_id,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CompositionRef:
        """从字典反序列化"""
        return CompositionRef(
            composition_id=data.get("composition_id", ""),
            name=data.get("name", "Default Composition"),
            source_kind=data.get("source_kind", "standalone"),
            asset_id=data.get("asset_id"),
            timeline_id=data.get("timeline_id"),
            clip_id=data.get("clip_id"),
            _extra_fields=split_extras(data, CompositionRef._KNOWN_KEYS),
        )


# ================================================================
# DeliverPresetRef - 导出预设引用（简化版）
# ================================================================

@dataclass(frozen=True)
@preserves_extras
class DeliverPresetRef:
    """
    导出预设引用（存储在 project.json 中的简化版）。

    完整的导出预设在 deliver/presets.json 中，
    这里只保留核心索引字段。

    Attributes:
        preset_id: 预设唯一标识符（UUID）
        name: 预设名称
        format_type: 导出格式（h264, prores, dnxhr, exr, etc.）
        resolution: 输出分辨率
    """
    preset_id: str
    name: str = "Default"
    format_type: str = "h264"  # h264, prores, dnxhr, exr, etc.
    resolution: tuple[int, int] = (1920, 1080)
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + from_dict 消费键（畸形 resolution 在 from_dict 内
    # 降级为默认值，键本身仍为已知键）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "preset_id", "name", "format_type", "resolution",
    })

    def __post_init__(self) -> None:
        """验证导出预设数据有效性"""
        if not self.preset_id:
            raise ValueError("preset_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典"""
        return {
            "preset_id": self.preset_id,
            "name": self.name,
            "format_type": self.format_type,
            "resolution": list(self.resolution),
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeliverPresetRef:
        """从字典反序列化。

        AUD-37005: 畸形 resolution（长度不足、非列表、元素不可转 int）
        降级为默认 (1920, 1080)，单个坏预设字段不再让整个工程加载
        失败（IndexError/TypeError 逃逸）。
        """
        default_resolution = (1920, 1080)
        resolution_data = data.get("resolution", default_resolution)
        resolution = default_resolution
        if isinstance(resolution_data, (list, tuple)) and len(resolution_data) >= 2:
            try:
                resolution = (int(resolution_data[0]), int(resolution_data[1]))
            except (TypeError, ValueError):
                resolution = default_resolution
        return DeliverPresetRef(
            preset_id=data.get("preset_id", ""),
            name=data.get("name", "Default"),
            format_type=data.get("format_type", "h264"),
            resolution=resolution,
            _extra_fields=split_extras(data, DeliverPresetRef._KNOWN_KEYS),
        )


# ================================================================
# ToposProject - 统一工程模型
# ================================================================

@dataclass(frozen=True)
@preserves_extras
class ToposProject:
    """
    统一工程模型。

    唯一的工程对象，整合 Edit/Color/Composite/Deliver/UI 状态。
    所有数据存储在 .topos ZIP 包中。

    project.json 是工程核心索引，各页面完整数据存储在子目录 JSON 中：
    - edit/: media_pool.json, bins.json, timelines.json, timeline_sequences.json
    - color/: color_stacks.json, looks/
    - composite/: graphs.json, viewer_state.json
    - deliver/: presets.json
    - ui/: workspace_state.json

    Attributes:
        project_id: 工程唯一 ID（UUID）
        name: 工程名称
        project_mode: 工程模式
        assets: 素材字典 {asset_id: MediaAssetRef}
        bins: 素材箱字典 {bin_id: ProjectBinRef}
        timelines: 时间线字典 {timeline_id: TimelineRef}
        color_stacks: 调色栈字典 {stack_id: ColorStackRef}
        compositions: 合成图字典 {composition_id: CompositionRef}
        deliver_presets: 导出预设字典 {preset_id: DeliverPresetRef}
        ui_state: UI 状态字典
        settings: 工程设置字典
        primary_asset_id: 主要素材 ID（单素材工程）
        primary_color_stack_id: 主要调色栈 ID（单素材工程）
        active_timeline_id: 当前活动时间线 ID
        active_composition_id: 当前活动合成图 ID
        created_at: 创建时间戳
        modified_at: 最后修改时间戳
    """
    project_id: str
    name: str
    project_mode: ProjectMode = "standalone_image"

    # 核心数据引用（索引）
    assets: dict[str, MediaAssetRef] = field(default_factory=dict)
    bins: dict[str, ProjectBinRef] = field(default_factory=dict)
    timelines: dict[str, TimelineRef] = field(default_factory=dict)
    color_stacks: dict[str, ColorStackRef] = field(default_factory=dict)
    compositions: dict[str, CompositionRef] = field(default_factory=dict)
    deliver_presets: dict[str, DeliverPresetRef] = field(default_factory=dict)
    
    # 状态
    ui_state: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    
    # 活动引用
    primary_asset_id: str | None = None
    primary_color_stack_id: str | None = None
    active_timeline_id: str | None = None
    active_composition_id: str | None = None
    
    # 时间戳
    created_at: float = 0.0
    modified_at: float = 0.0
    # TAEP P0: 工程逻辑 revision（乐观锁，单调递增）。每次成功落地一次原子
    # timeline 变更后递增；不属于 schema 版本。AI/脚本/XML 导入经统一提交入口
    # 时保证 bump。保留未知字段策略保证旧工程无此字段时回退 0。
    revision: int = 0
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）。project.json 中
    # 高版本写入者的新键经 from_dict 捕获、to_dict 原样回写。
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + from_dict 消费键（两者一致；schema_version 由包级
    # manifest/IO 层管理，不在 project.json 的本类字典里）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "project_id", "name", "project_mode",
        "assets", "bins", "timelines", "color_stacks",
        "compositions", "deliver_presets",
        "ui_state", "settings",
        "primary_asset_id", "primary_color_stack_id",
        "active_timeline_id", "active_composition_id",
        "created_at", "modified_at", "revision",
    })

    def __post_init__(self) -> None:
        """验证工程数据有效性"""
        # AUD-37004: ``frozen=True`` alone does not make the object safe to
        # share across threads — the dict fields were passed by reference, so
        # two instances derived via ``with_*``/``from_dict`` mutated the same
        # underlying dict. Break the sharing here with defensive (shallow)
        # copies. Note: values inside ``ui_state``/``settings`` may still be
        # mutable; treat them as read-only snapshots owned by this instance.
        for dict_field in (
            "assets", "bins", "timelines", "color_stacks",
            "compositions", "deliver_presets", "ui_state", "settings",
            "_extra_fields",
        ):
            object.__setattr__(self, dict_field, dict(getattr(self, dict_field) or {}))

        if not self.project_id:
            raise ValueError("project_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")

        valid_modes = ("standalone_image", "edit_project", "composite_shot", "mixed_project")
        if self.project_mode not in valid_modes:
            raise ValueError(f"Invalid project_mode: {self.project_mode}")

    # ==================== 序列化 ====================

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（project.json 格式）"""
        return {
            "project_id": self.project_id,
            "name": self.name,
            "project_mode": self.project_mode,
            "assets": {k: v.to_dict() for k, v in self.assets.items()},
            "bins": {k: v.to_dict() for k, v in self.bins.items()},
            "timelines": {k: v.to_dict() for k, v in self.timelines.items()},
            "color_stacks": {k: v.to_dict() for k, v in self.color_stacks.items()},
            "compositions": {k: v.to_dict() for k, v in self.compositions.items()},
            "deliver_presets": {k: v.to_dict() for k, v in self.deliver_presets.items()},
            # AUD-37004: hand out copies so callers cannot reach into the
            # frozen instance's state through the serialized dict.
            "ui_state": dict(self.ui_state),
            "settings": dict(self.settings),
            "primary_asset_id": self.primary_asset_id,
            "primary_color_stack_id": self.primary_color_stack_id,
            "active_timeline_id": self.active_timeline_id,
            "active_composition_id": self.active_composition_id,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "revision": self.revision,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ToposProject:
        """从字典反序列化"""
        assets = {k: MediaAssetRef.from_dict(v) for k, v in data.get("assets", {}).items()}
        bins = {k: ProjectBinRef.from_dict(v) for k, v in data.get("bins", {}).items()}
        timelines = {k: TimelineRef.from_dict(v) for k, v in data.get("timelines", {}).items()}
        color_stacks = {k: ColorStackRef.from_dict(v) for k, v in data.get("color_stacks", {}).items()}
        compositions = {k: CompositionRef.from_dict(v) for k, v in data.get("compositions", {}).items()}
        deliver_presets = {k: DeliverPresetRef.from_dict(v) for k, v in data.get("deliver_presets", {}).items()}

        return ToposProject(
            project_id=data.get("project_id", ""),
            name=data.get("name", "Untitled"),
            project_mode=data.get("project_mode", "standalone_image"),
            assets=assets,
            bins=bins,
            timelines=timelines,
            color_stacks=color_stacks,
            compositions=compositions,
            deliver_presets=deliver_presets,
            ui_state=data.get("ui_state", {}),
            settings=data.get("settings", {}),
            primary_asset_id=data.get("primary_asset_id"),
            primary_color_stack_id=data.get("primary_color_stack_id"),
            active_timeline_id=data.get("active_timeline_id"),
            active_composition_id=data.get("active_composition_id"),
            created_at=data.get("created_at", 0.0),
            modified_at=data.get("modified_at", 0.0),
            revision=int(data.get("revision", 0) or 0),
            _extra_fields=split_extras(data, ToposProject._KNOWN_KEYS),
        )

    # ==================== 工厂方法 ====================

    @staticmethod
    def create(
        name: str = "Untitled",
        project_mode: ProjectMode = "standalone_image",
        primary_asset_id: str | None = None,
    ) -> ToposProject:
        """
        创建新的工程。

        Args:
            name: 工程名称
            project_mode: 工程模式
            primary_asset_id: 主要素材 ID（单素材工程）

        Returns:
            新的 ToposProject 实例
        """
        now = time.time()
        project_id = str(uuid.uuid4())
        
        # 创建默认 root bin
        root_bin_id = str(uuid.uuid4())
        root_bin = ProjectBinRef(
            bin_id=root_bin_id,
            name="Root",
            parent_id=None,
        )
        
        return ToposProject(
            project_id=project_id,
            name=name,
            project_mode=project_mode,
            bins={root_bin_id: root_bin},
            primary_asset_id=primary_asset_id,
            created_at=now,
            modified_at=now,
        )

    @staticmethod
    def create_standalone_image(
        name: str,
        image_path: str,
    ) -> ToposProject:
        """
        创建单图片调色工程 (schema v2)。

        仅允许图片文件。视频文件必须通过 Edit timeline clip 流程调色。

        Args:
            name: 工程名称
            image_path: 图片文件路径

        Returns:
            新的 ToposProject 实例

        Raises:
            ValueError: 如果路径不是图片文件
        """
        from src.shared.media.format_registry import IMAGE_EXTENSIONS
        ext = os.path.splitext(image_path)[1].lower()
        if ext not in IMAGE_EXTENSIONS:
            raise ValueError(
                f"create_standalone_image only accepts image files, "
                f"got '{ext}'. Video files must go through Edit timeline clips."
            )

        now = time.time()
        project_id = str(uuid.uuid4())
        asset_id = str(uuid.uuid4())
        stack_id = str(uuid.uuid4())
        root_bin_id = str(uuid.uuid4())

        # 创建素材引用
        asset = MediaAssetRef(
            asset_id=asset_id,
            path=image_path,
            media_type="image",
            display_name=name,
        )

        # 创建调色栈引用
        color_stack = ColorStackRef(
            stack_id=stack_id,
            asset_id=asset_id,
            name="Default",
            scope="asset_default",
        )

        # 创建 root bin
        root_bin = ProjectBinRef(
            bin_id=root_bin_id,
            name="Root",
            parent_id=None,
            asset_count=1,
        )

        return ToposProject(
            project_id=project_id,
            name=name,
            project_mode="standalone_image",
            assets={asset_id: asset},
            bins={root_bin_id: root_bin},
            color_stacks={stack_id: color_stack},
            primary_asset_id=asset_id,
            primary_color_stack_id=stack_id,
            created_at=now,
            modified_at=now,
        )

    @staticmethod
    def create_single_asset(
        name: str,
        asset_path: str,
        media_type: str = "image",
    ) -> ToposProject:
        """
        创建单素材工程（已废弃，保留兼容性）。

        Schema v2 规则：仅允许图片文件。视频文件必须通过 Edit timeline clip 流程。
        请使用 create_standalone_image() 替代。

        Args:
            name: 工程名称
            asset_path: 素材路径
            media_type: 媒体类型

        Returns:
            新的 ToposProject 实例

        Raises:
            ValueError: 如果 media_type 不是 "image"
        """
        if media_type != "image":
            raise ValueError(
                f"create_single_asset no longer supports media_type='{media_type}'. "
                f"Video files must go through Edit timeline clips."
            )
        # Delegate to create_standalone_image
        return ToposProject.create_standalone_image(name=name, image_path=asset_path)

    def with_name(self, name: str) -> ToposProject:
        """创建新实例，修改名称"""
        return ToposProject(
            project_id=self.project_id,
            name=name,
            project_mode=self.project_mode,
            assets=self.assets,
            bins=self.bins,
            timelines=self.timelines,
            color_stacks=self.color_stacks,
            compositions=self.compositions,
            deliver_presets=self.deliver_presets,
            ui_state=self.ui_state,
            settings=self.settings,
            primary_asset_id=self.primary_asset_id,
            primary_color_stack_id=self.primary_color_stack_id,
            active_timeline_id=self.active_timeline_id,
            active_composition_id=self.active_composition_id,
            created_at=self.created_at,
            modified_at=time.time(),
            # AUD-37003: preserve the optimistic-lock revision across derived
            # instances; dropping it silently reset the counter to 0.
            revision=self.revision,
        )

    def with_asset_added(self, asset: MediaAssetRef) -> ToposProject:
        """创建新实例，添加素材"""
        new_assets = dict(self.assets)
        new_assets[asset.asset_id] = asset
        
        # 更新 root bin 的 asset_count
        new_bins = dict(self.bins)
        root_bin_id = next((bid for bid, b in self.bins.items() if b.parent_id is None), None)
        if root_bin_id and root_bin_id in new_bins:
            root_bin = new_bins[root_bin_id]
            new_bins[root_bin_id] = ProjectBinRef(
                bin_id=root_bin.bin_id,
                name=root_bin.name,
                parent_id=root_bin.parent_id,
                asset_count=root_bin.asset_count + 1,
            )

        return ToposProject(
            project_id=self.project_id,
            name=self.name,
            project_mode=self.project_mode,
            assets=new_assets,
            bins=new_bins,
            timelines=self.timelines,
            color_stacks=self.color_stacks,
            compositions=self.compositions,
            deliver_presets=self.deliver_presets,
            ui_state=self.ui_state,
            settings=self.settings,
            primary_asset_id=self.primary_asset_id,
            primary_color_stack_id=self.primary_color_stack_id,
            active_timeline_id=self.active_timeline_id,
            active_composition_id=self.active_composition_id,
            created_at=self.created_at,
            modified_at=time.time(),
            # AUD-37003: preserve the optimistic-lock revision across derived
            # instances; dropping it silently reset the counter to 0.
            revision=self.revision,
        )

    def with_color_stack_added(self, stack: ColorStackRef) -> ToposProject:
        """创建新实例，添加调色栈"""
        new_stacks = dict(self.color_stacks)
        new_stacks[stack.stack_id] = stack

        return ToposProject(
            project_id=self.project_id,
            name=self.name,
            project_mode=self.project_mode,
            assets=self.assets,
            bins=self.bins,
            timelines=self.timelines,
            color_stacks=new_stacks,
            compositions=self.compositions,
            deliver_presets=self.deliver_presets,
            ui_state=self.ui_state,
            settings=self.settings,
            primary_asset_id=self.primary_asset_id,
            primary_color_stack_id=self.primary_color_stack_id,
            active_timeline_id=self.active_timeline_id,
            active_composition_id=self.active_composition_id,
            created_at=self.created_at,
            modified_at=time.time(),
            # AUD-37003: preserve the optimistic-lock revision across derived
            # instances; dropping it silently reset the counter to 0.
            revision=self.revision,
        )

    def with_timeline_added(self, timeline: TimelineRef) -> ToposProject:
        """创建新实例，添加时间线"""
        new_timelines = dict(self.timelines)
        new_timelines[timeline.timeline_id] = timeline

        return ToposProject(
            project_id=self.project_id,
            name=self.name,
            project_mode=self.project_mode,
            assets=self.assets,
            bins=self.bins,
            timelines=new_timelines,
            color_stacks=self.color_stacks,
            compositions=self.compositions,
            deliver_presets=self.deliver_presets,
            ui_state=self.ui_state,
            settings=self.settings,
            primary_asset_id=self.primary_asset_id,
            primary_color_stack_id=self.primary_color_stack_id,
            active_timeline_id=self.active_timeline_id,
            active_composition_id=self.active_composition_id,
            created_at=self.created_at,
            modified_at=time.time(),
            # AUD-37003: preserve the optimistic-lock revision across derived
            # instances; dropping it silently reset the counter to 0.
            revision=self.revision,
        )

    def with_composition_added(self, composition: CompositionRef) -> ToposProject:
        """创建新实例，添加合成图"""
        new_compositions = dict(self.compositions)
        new_compositions[composition.composition_id] = composition

        return ToposProject(
            project_id=self.project_id,
            name=self.name,
            project_mode=self.project_mode,
            assets=self.assets,
            bins=self.bins,
            timelines=self.timelines,
            color_stacks=self.color_stacks,
            compositions=new_compositions,
            deliver_presets=self.deliver_presets,
            ui_state=self.ui_state,
            settings=self.settings,
            primary_asset_id=self.primary_asset_id,
            primary_color_stack_id=self.primary_color_stack_id,
            active_timeline_id=self.active_timeline_id,
            active_composition_id=self.active_composition_id,
            created_at=self.created_at,
            modified_at=time.time(),
            # AUD-37003: preserve the optimistic-lock revision across derived
            # instances; dropping it silently reset the counter to 0.
            revision=self.revision,
        )

    def with_active_timeline(self, timeline_id: str | None) -> ToposProject:
        """创建新实例，设置活动时间线"""
        return ToposProject(
            project_id=self.project_id,
            name=self.name,
            project_mode=self.project_mode,
            assets=self.assets,
            bins=self.bins,
            timelines=self.timelines,
            color_stacks=self.color_stacks,
            compositions=self.compositions,
            deliver_presets=self.deliver_presets,
            ui_state=self.ui_state,
            settings=self.settings,
            primary_asset_id=self.primary_asset_id,
            primary_color_stack_id=self.primary_color_stack_id,
            active_timeline_id=timeline_id,
            active_composition_id=self.active_composition_id,
            created_at=self.created_at,
            modified_at=time.time(),
            # AUD-37003: preserve the optimistic-lock revision across derived
            # instances; dropping it silently reset the counter to 0.
            revision=self.revision,
        )

    def with_ui_state(self, ui_state: dict[str, Any]) -> ToposProject:
        """创建新实例，更新 UI 状态"""
        return ToposProject(
            project_id=self.project_id,
            name=self.name,
            project_mode=self.project_mode,
            assets=self.assets,
            bins=self.bins,
            timelines=self.timelines,
            color_stacks=self.color_stacks,
            compositions=self.compositions,
            deliver_presets=self.deliver_presets,
            ui_state=ui_state,
            settings=self.settings,
            primary_asset_id=self.primary_asset_id,
            primary_color_stack_id=self.primary_color_stack_id,
            active_timeline_id=self.active_timeline_id,
            active_composition_id=self.active_composition_id,
            created_at=self.created_at,
            modified_at=time.time(),
            # AUD-37003: preserve the optimistic-lock revision across derived
            # instances; dropping it silently reset the counter to 0.
            revision=self.revision,
        )

    # ==================== 计算属性 ====================

    @property
    def asset_count(self) -> int:
        """素材数量"""
        return len(self.assets)

    @property
    def timeline_count(self) -> int:
        """时间线数量"""
        return len(self.timelines)

    @property
    def color_stack_count(self) -> int:
        """调色栈数量"""
        return len(self.color_stacks)

    @property
    def composition_count(self) -> int:
        """合成图数量"""
        return len(self.compositions)

    @property
    def features(self) -> tuple[FeatureType, ...]:
        """根据工程模式推断功能特性"""
        if self.project_mode == "standalone_image":
            features = ["media_pool", "color"]
            if self.compositions:
                features.append("composite")
            return tuple(features)
        elif self.project_mode == "edit_project":
            return ("media_pool", "edit", "color")
        elif self.project_mode == "composite_shot":
            return ("media_pool", "composite", "color")
        elif self.project_mode == "mixed_project":
            return ("media_pool", "edit", "color", "composite", "deliver")
        return ("media_pool", "color")

    @property
    def root_bin_id(self) -> str | None:
        """获取 root bin ID"""
        for bin_id, bin_ref in self.bins.items():
            if bin_ref.parent_id is None:
                return bin_id
        return None

    @property
    def primary_asset(self) -> MediaAssetRef | None:
        """获取主要素材"""
        if self.primary_asset_id and self.primary_asset_id in self.assets:
            return self.assets[self.primary_asset_id]
        return None

    @property
    def primary_color_stack(self) -> ColorStackRef | None:
        """获取主要调色栈"""
        if self.primary_color_stack_id and self.primary_color_stack_id in self.color_stacks:
            return self.color_stacks[self.primary_color_stack_id]
        return None

    @property
    def active_timeline(self) -> TimelineRef | None:
        """获取活动时间线"""
        if self.active_timeline_id and self.active_timeline_id in self.timelines:
            return self.timelines[self.active_timeline_id]
        return None

    @property
    def active_composition(self) -> CompositionRef | None:
        """获取活动合成图"""
        if self.active_composition_id and self.active_composition_id in self.compositions:
            return self.compositions[self.active_composition_id]
        return None

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"ToposProject(project_id={self.project_id!r}, "
            f"name={self.name!r}, "
            f"project_mode={self.project_mode!r}, "
            f"assets={self.asset_count}, "
            f"timelines={self.timeline_count})"
        )
