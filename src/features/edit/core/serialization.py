"""
Edit Project Serialization Module
编辑项目序列化模块（P2）。

## 职责

- EditProjectSerializer: JSON 序列化/反序列化
- 支持项目保存和加载
- 版本管理（支持未来迁移）
- UI 状态保存（播放头位置、缩放级别）
- 完整 TimelineSequence 序列化（轨道、剪辑、转场）

## 序列化格式

```json
{
  "version": 4,
  "project_type": "edit_project",
  "project": {
    "project_id": "...",
    "name": "...",
    "assets": {...},
    "bins": {...},
    "timelines": {...},
    "timeline_sequences": {...},
    "root_bin_id": "...",
    "active_timeline_id": "...",
    "created_at": 0.0,
    "modified_at": 0.0
  },
  "ui_state": {
    "active_timeline_id": "...",
    "playhead_frame": 0,
    "zoom_level": 1.0,
    "scroll_position": 0,
    "selected_clip_ids": [],
    "selected_track_ids": []
  }
}
```

## 设计原则

- JSON roundtrip 测试必须通过
- 不破坏旧 .topos 图片项目格式
- 统一工程包格式（Edit/Color/Composite 项目）
- 版本号便于未来迁移
- UI 状态可选，不影响核心数据恢复

## 文件扩展名约定

- `.topos`: 统一工程包格式（Edit/Color/Composite 项目）
- `.topos.autosave`: 自动保存临时文件
- `.topos.bak`: 备份文件

## 后续扩展

- P10: 项目保存/恢复与 UI 集成
- 版本迁移策略（v3 -> v4）
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
from typing import Any, Optional
from pathlib import Path

from src.features.edit.core.project import (
    MediaAsset,
    ProjectBin,
    EditProject,
    SmartBin,
    TimelineSequenceRef,
    ColorStackData,
)
from src.features.edit.core.timeline_v2 import TimelineSequence
# SUB-P2-10: caption-schema migration runs independently of the project
# SERIALIZATION_VERSION so caption evolution does not bump the project schema.
from src.features.edit.core.captions.schema import upgrade_captions_in_project
# DESK-04A：未知字段捕获（读取侧 split_extras 与各数据类 _KNOWN_KEYS 配对）
from src.shared.extra_fields import split_extras

from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ================================================================
# M02（S01）：较新工程表的 JSON 序列化助手
# ================================================================
# 复用 edit_project_io 的既有 serialize/deserialize（同一份识别键与
# 未知载荷保留语义，不建平行实现）。懒导入避免循环依赖——
# edit_project_io 顶层 import 本模块。

def _io_serialize_color_groups(project: Any) -> dict[str, Any]:
    from src.features.edit.core.edit_project_io import _serialize_color_groups
    return _serialize_color_groups(project.color_groups or {})


def _sanitize_sync_diagnostics(raw: Any) -> dict[str, Any]:
    """M03.4：同步诊断表反序列化（逐条形状校验，非法丢弃）。"""
    from src.features.edit.core.sync_diagnostics import (
        sanitize_sync_diagnostics,
    )
    return sanitize_sync_diagnostics(raw)


def _io_serialize_table(project: Any, field_name: str) -> dict[str, Any]:
    """source_patch_overrides / interpretation_overrides / audio_sync_bindings
    三张覆盖表共用（edit_project_io 按字段名分派到对应助手）。"""
    from src.features.edit.core import edit_project_io
    table = getattr(project, field_name, None) or {}
    if field_name == "source_patch_overrides":
        return edit_project_io._serialize_source_patch_overrides(table)
    if field_name == "interpretation_overrides":
        return edit_project_io._serialize_interpretation_overrides(table)
    if field_name == "audio_sync_bindings":
        return edit_project_io._serialize_audio_sync_bindings(table)
    return copy.deepcopy(table)


def _io_deserialize_new_tables(data: dict[str, Any]) -> dict[str, Any]:
    """反向：JSON dict → 六张表的运行时形态（未知载荷原样保留）。"""
    from src.features.edit.core import edit_project_io
    return {
        "color_groups": edit_project_io._deserialize_color_groups(
            data.get("color_groups", {})),
        "color_stack_versions": copy.deepcopy(
            data.get("color_stack_versions", {})),
        "source_patch_overrides": (
            edit_project_io._deserialize_source_patch_overrides(
                data.get("source_patch_overrides", {}))),
        "interpretation_overrides": (
            edit_project_io._deserialize_interpretation_overrides(
                data.get("interpretation_overrides", {}))),
        "audio_sync_bindings": (
            edit_project_io._deserialize_audio_sync_bindings(
                data.get("audio_sync_bindings", {}))),
        "sync_diagnostics": (
            _sanitize_sync_diagnostics(
                data.get("sync_diagnostics", {}))),
        "project_snapshots": copy.deepcopy(
            data.get("project_snapshots", {})),
    }


# ================================================================
# UIState - UI 线状态数据类
# ================================================================

@dataclass
class UIState:
    """
    编辑项目 UI 状态。

    存储时间线的 UI 相关状态，不影响核心数据恢复，
    但能恢复用户的编辑上下文（播放头位置、缩放等）。

    Attributes:
        active_timeline_id: 当前活动时间线 ID
        playhead_frame: 播放头位置（帧号）
        zoom_level: 时间线缩放级别（像素/帧）
        scroll_position: 时间线水平滚动位置（像素）
        selected_clip_ids: 选中的剪辑 ID 列表
        selected_track_ids: 选中的轨道 ID 列表
        expanded_bins: 展开的素材箱 ID 列表
        view_mode: 媒体池视图模式（list/grid）
    """
    active_timeline_id: str | None = None
    playhead_frame: int = 0
    zoom_level: float = 1.0
    scroll_position: int = 0
    selected_clip_ids: list[str] = field(default_factory=list)
    selected_track_ids: list[str] = field(default_factory=list)
    expanded_bins: list[str] = field(default_factory=list)
    view_mode: str = "list"  # list 或 grid
    # SUB-P1-06: per-timeline layout (track heights / scroll / area ratios) so
    # reopening a project restores the user's arrangement. None = not saved.
    timeline_layout: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "active_timeline_id": self.active_timeline_id,
            "playhead_frame": self.playhead_frame,
            "zoom_level": self.zoom_level,
            "scroll_position": self.scroll_position,
            "selected_clip_ids": self.selected_clip_ids,
            "selected_track_ids": self.selected_track_ids,
            "expanded_bins": self.expanded_bins,
            "view_mode": self.view_mode,
            "timeline_layout": self.timeline_layout,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UIState:
        """从字典创建实例"""
        return UIState(
            active_timeline_id=data.get("active_timeline_id"),
            playhead_frame=data.get("playhead_frame", 0),
            zoom_level=data.get("zoom_level", 1.0),
            scroll_position=data.get("scroll_position", 0),
            selected_clip_ids=data.get("selected_clip_ids", []),
            selected_track_ids=data.get("selected_track_ids", []),
            expanded_bins=data.get("expanded_bins", []),
            view_mode=data.get("view_mode", "list"),
            timeline_layout=data.get("timeline_layout"),
        )

# 序列化版本号
# v5: clip TransformParams 从归一化模型（position_x/y, anchor_x/y）改为像素模型
#     （translate_x/y, pivot_mode/pivot_x/y），与合成页 TransformNode 统一。
SERIALIZATION_VERSION = 5
PROJECT_TYPE = "edit_project"

# 文件扩展名
EDIT_PROJECT_EXTENSION = ".topos"


class EditProjectSerializer:
    """
    编辑项目序列化器。

    支持将 EditProject 序列化为 JSON，以及从 JSON 反序列化。

    Features:
    - JSON roundtrip
    - 版本管理
    - 错误处理
    """

    # ==================== 序列化 ====================

    @staticmethod
    def to_json(project: EditProject) -> str:
        """
        将项目序列化为 JSON 字符串（基础版本）。

        Args:
            project: EditProject 实例

        Returns:
            JSON 字符串
        """
        data = EditProjectSerializer._project_to_dict(project)
        wrapper = {
            "version": SERIALIZATION_VERSION,
            "project_type": PROJECT_TYPE,
            "project": data,
        }
        return json.dumps(wrapper, indent=2, ensure_ascii=False)

    @staticmethod
    def to_json_with_ui_state(
        project: EditProject,
        ui_state: UIState,
        timeline_sequences: dict[str, TimelineSequence] | None = None,
    ) -> str:
        """
        将项目序列化为 JSON 字符串（包含 UI 状态和完整时间线数据）。

        Args:
            project: EditProject 实例
            ui_state: UI 状态实例
            timeline_sequences: 完整的 TimelineSequence 数据字典（可选）

        Returns:
            JSON 字符串
        """
        data = EditProjectSerializer._project_to_dict(project)
        wrapper = {
            "version": SERIALIZATION_VERSION,
            "project_type": PROJECT_TYPE,
            "project": data,
            "ui_state": ui_state.to_dict(),
        }

        # 添加完整的 TimelineSequence 数据（如果有）
        if timeline_sequences:
            wrapper["timeline_sequences"] = {
                seq_id: seq.to_dict()
                for seq_id, seq in timeline_sequences.items()
            }

        return json.dumps(wrapper, indent=2, ensure_ascii=False)

    @staticmethod
    def to_dict(project: EditProject) -> dict[str, Any]:
        """
        将项目转换为字典（不包含版本包装）。

        Args:
            project: EditProject 实例

        Returns:
            项目数据字典
        """
        return EditProjectSerializer._project_to_dict(project)

    @staticmethod
    def _project_to_dict(project: EditProject) -> dict[str, Any]:
        """将 EditProject 转换为字典"""
        cache_settings = project.render_cache_settings
        if hasattr(cache_settings, "to_dict"):
            cache_settings = cache_settings.to_dict()
        proxy_policy = project.proxy_policy
        if hasattr(proxy_policy, "to_dict"):
            proxy_policy = proxy_policy.to_dict()
        return {
            "project_id": project.project_id,
            "name": project.name,
            "assets": {
                asset_id: EditProjectSerializer._asset_to_dict(asset)
                for asset_id, asset in project.assets.items()
            },
            "bins": {
                bin_id: EditProjectSerializer._bin_to_dict(bin_obj)
                for bin_id, bin_obj in project.bins.items()
            },
            "timelines": {
                timeline_id: EditProjectSerializer._timeline_ref_to_dict(timeline)
                for timeline_id, timeline in project.timelines.items()
            },
            "root_bin_id": project.root_bin_id,
            "active_timeline_id": project.active_timeline_id,
            "created_at": project.created_at,
            "modified_at": project.modified_at,
            # P9.1: Color stacks
            "color_stacks": {
                stack_id: EditProjectSerializer._color_stack_to_dict(stack)
                for stack_id, stack in project.color_stacks.items()
            },
            # P9.2: Compositions
            "compositions": dict(project.compositions),
            # Comp Clips
            "comp_clips": dict(project.comp_clips),
            # P1-12: 用户自建 Smart Bins
            "smart_bins": {
                bin_id: EditProjectSerializer._smart_bin_to_dict(smart_bin)
                for bin_id, smart_bin in project.smart_bins.items()
            },
            "render_cache_manifests": dict(project.render_cache_manifests),
            "render_cache_settings": cache_settings,
            "proxy_policy": proxy_policy,
            "active_comp_clip_id": project.active_comp_clip_id,
            # F-R09/R28：兼容 JSON 保留 project revision（此前 from_json(
            # to_json(p revision7)) 变 0，旧 AI plan base_revision 失效判断
            # 被绕过）
            "revision": int(project.revision),
            # M14.3b：工程级采样率随工程落盘（0 = 未设置，由
            # project_sample_rate 访问器解析默认 48000——序列化层不猜）
            "project_sample_rate": int(project.project_sample_rate or 0),
            # M02（S01）：六张较新表此前双向都不序列化——to_json/from_json
            # 往返会静默清空调色组、栈版本、外录绑定、源 patch、解释覆盖
            # 与工程快照表。序列化复用 edit_project_io 的既有助手（懒导入
            # 避免循环依赖：edit_project_io 顶层 import 本模块）。
            "color_groups": _io_serialize_color_groups(project),
            "color_stack_versions": copy.deepcopy(project.color_stack_versions or {}),
            "source_patch_overrides": _io_serialize_table(
                project, "source_patch_overrides"),
            "interpretation_overrides": _io_serialize_table(
                project, "interpretation_overrides"),
            "audio_sync_bindings": _io_serialize_table(
                project, "audio_sync_bindings"),
            # M03.4：同步诊断随工程 JSON 往返（纯 dict 表）
            "sync_diagnostics": copy.deepcopy(
                project.sync_diagnostics or {}),
            "project_snapshots": copy.deepcopy(project.project_snapshots or {}),
        }

    @staticmethod
    def _asset_to_dict(asset: MediaAsset) -> dict[str, Any]:
        """将 MediaAsset 转换为字典。

        注意：必须写出全部持久化字段。此前该方法遗漏了
        reel/scene/shot/take/is_offline/proxy_path，导致保存工程后
        production info 与离线/代理状态丢失（P1-12 修复）。
        """
        return {
            "asset_id": asset.asset_id,
            "path": asset.path,
            "media_type": asset.media_type,
            "display_name": asset.display_name,
            "metadata": asset.metadata,
            "thumbnail_key": asset.thumbnail_key,
            "proxy_path": asset.proxy_path,
            "proxy_status": asset.proxy_status,
            "is_offline": asset.is_offline,
            "color_metadata": asset.color_metadata,
            "created_at": asset.created_at,
            "reel": asset.reel,
            "scene": asset.scene,
            "shot": asset.shot,
            "take": asset.take,
            # P1-12 素材日志字段
            "keywords": list(asset.keywords),
            "rating": asset.rating,
            "flag": asset.flag,
            "color_label": asset.color_label,
            "comment": asset.comment,
            # DESK-01B：内嵌资源身份随工程落盘（None = 外部路径素材，
            # 旧工程不受影响）。path 的缓存守卫在 ToposProjectIO 侧。
            **({"resource_uri": asset.resource_uri}
               if asset.resource_uri else {}),
            **({"path_original": asset.path_original}
               if asset.path_original else {}),
            # DESK-04A：未知键原样回写（load→save 不丢）
            **getattr(asset, "_extra_fields", {}),
        }

    @staticmethod
    def _bin_to_dict(bin_obj: ProjectBin) -> dict[str, Any]:
        """将 ProjectBin 转换为字典"""
        return {
            "bin_id": bin_obj.bin_id,
            "name": bin_obj.name,
            "parent_id": bin_obj.parent_id,
            "asset_ids": list(bin_obj.asset_ids),
            "timeline_ids": list(bin_obj.timeline_ids),
            # DESK-04A：未知键原样回写
            **getattr(bin_obj, "_extra_fields", {}),
        }

    @staticmethod
    def _smart_bin_to_dict(smart_bin: SmartBin) -> dict[str, Any]:
        """将 SmartBin 转换为字典（P1-12 用户自建已保存搜索）"""
        return {
            "bin_id": smart_bin.bin_id,
            "name": smart_bin.name,
            "filter_type": smart_bin.filter_type,
            "filter_params": dict(smart_bin.filter_params),
            # DESK-04A：未知键原样回写
            **getattr(smart_bin, "_extra_fields", {}),
        }

    @staticmethod
    def _timeline_ref_to_dict(timeline: TimelineSequenceRef) -> dict[str, Any]:
        """将 TimelineSequenceRef 转换为字典"""
        return {
            "timeline_id": timeline.timeline_id,
            "name": timeline.name,
            "fps": timeline.fps,
            "width": timeline.width,
            "height": timeline.height,
            "sample_rate": timeline.sample_rate,
            "format_settings": dict(timeline.format_settings),
            "created_at": timeline.created_at,
            # DESK-04A：未知键原样回写
            **getattr(timeline, "_extra_fields", {}),
        }

    @staticmethod
    def _color_stack_to_dict(stack: Any) -> dict[str, Any]:
        """将 ColorStackData 转换为字典"""
        return stack.to_dict()

    # ==================== 反序列化 ====================

    @staticmethod
    def from_json(json_str: str) -> EditProject:
        """
        从 JSON 字符串反序列化项目（基础版本）。

        Args:
            json_str: JSON 字符串

        Returns:
            EditProject 实例

        Raises:
            ValueError: JSON 格式无效或版本不兼容
        """
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")

        # 检查版本和类型
        version = data.get("version")
        project_type = data.get("project_type")

        if version is None:
            raise ValueError("Missing version field")

        if version > SERIALIZATION_VERSION:
            raise ValueError(
                f"Unsupported version: {version}, "
                f"max supported: {SERIALIZATION_VERSION}"
            )

        if project_type != PROJECT_TYPE:
            raise ValueError(
                f"Invalid project_type: {project_type}, "
                f"expected: {PROJECT_TYPE}"
            )

        # 提取项目数据
        project_data = data.get("project", {})
        if not project_data:
            raise ValueError("Missing project data")

        # AUD-18206: v4→v5 迁移作用于完整 wrapper——clip 数据位于顶层
        # ``timeline_sequences``，而 ``project["timelines"]`` 只是
        # TimelineSequenceRef 元数据字典（不含 clip），在 project 子字典
        # 上迭代迁移既会崩溃也迁不到真实数据。
        if version < SERIALIZATION_VERSION:
            data = EditProjectSerializer._migrate_data(data, version)

        return EditProjectSerializer._dict_to_project(
            data.get("project", {}) or project_data, SERIALIZATION_VERSION
        )

    @staticmethod
    def from_json_with_ui_state(
        json_str: str
    ) -> tuple[EditProject, UIState | None, dict[str, TimelineSequence] | None]:
        """
        从 JSON 字符串反序列化项目（包含 UI 状态和完整时间线数据）。

        Args:
            json_str: JSON 字符串

        Returns:
            元组：(EditProject, UIState 或 None, TimelineSequence 字典或 None)

        Raises:
            ValueError: JSON 格式无效或版本不兼容
        """
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")

        # 检查版本和类型
        version = data.get("version")
        project_type = data.get("project_type")

        if version is None:
            raise ValueError("Missing version field")

        if version > SERIALIZATION_VERSION:
            raise ValueError(
                f"Unsupported version: {version}, "
                f"max supported: {SERIALIZATION_VERSION}"
            )

        if project_type != PROJECT_TYPE:
            raise ValueError(
                f"Invalid project_type: {project_type}, "
                f"expected: {PROJECT_TYPE}"
            )

        # 提取项目数据
        project_data = data.get("project", {})
        if not project_data:
            raise ValueError("Missing project data")

        # AUD-18206: 迁移作用于完整 wrapper（clip 位于 timeline_sequences），
        # 参见 from_json 中的说明。
        if version < SERIALIZATION_VERSION:
            data = EditProjectSerializer._migrate_data(data, version)

        project = EditProjectSerializer._dict_to_project(
            data.get("project", {}) or project_data, SERIALIZATION_VERSION
        )

        # 提取 UI 状态（可选）
        ui_state_data = data.get("ui_state")
        ui_state = None
        if ui_state_data:
            ui_state = UIState.from_dict(ui_state_data)

        # SUB-P2-10: upgrade caption blocks (track/clip) to the current caption
        # schema version before TimelineSequence.from_dict parses them. This is
        # decoupled from the project SERIALIZATION_VERSION migration chain and
        # idempotent, so a project saved by this build is a no-op on reload.
        upgrade_captions_in_project(data)

        # 提取完整的 TimelineSequence 数据（可选）
        timeline_sequences_data = data.get("timeline_sequences")
        timeline_sequences = None
        if timeline_sequences_data:
            timeline_sequences = {
                seq_id: TimelineSequence.from_dict(seq_data)
                for seq_id, seq_data in timeline_sequences_data.items()
            }

        return (project, ui_state, timeline_sequences)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> EditProject:
        """
        从字典反序列化项目（不包含版本包装）。

        Args:
            data: 项目数据字典

        Returns:
            EditProject 实例
        """
        return EditProjectSerializer._dict_to_project(data, SERIALIZATION_VERSION)

    @staticmethod
    def _dict_to_project(
        data: dict[str, Any],
        version: int = SERIALIZATION_VERSION
    ) -> EditProject:
        """将字典转换为 EditProject"""
        # 版本迁移（如果需要）
        if version < SERIALIZATION_VERSION:
            data = EditProjectSerializer._migrate_data(data, version)

        # 解析 assets
        assets: dict[str, MediaAsset] = {}
        for asset_id, asset_data in data.get("assets", {}).items():
            assets[asset_id] = EditProjectSerializer._dict_to_asset(asset_data)

        # 解析 bins
        bins: dict[str, ProjectBin] = {}
        for bin_id, bin_data in data.get("bins", {}).items():
            bins[bin_id] = EditProjectSerializer._dict_to_bin(bin_data)

        # P1-12: 解析用户自建 Smart Bins
        smart_bins: dict[str, SmartBin] = {}
        for bin_id, bin_data in data.get("smart_bins", {}).items():
            smart_bins[bin_id] = EditProjectSerializer._dict_to_smart_bin(bin_data)

        # 解析 timelines
        timelines: dict[str, TimelineSequenceRef] = {}
        for timeline_id, timeline_data in data.get("timelines", {}).items():
            timelines[timeline_id] = EditProjectSerializer._dict_to_timeline_ref(timeline_data)

        # P9.1: 解析 color_stacks
        color_stacks: dict[str, ColorStackData] = {}
        for stack_id, stack_data in data.get("color_stacks", {}).items():
            color_stacks[stack_id] = ColorStackData.from_dict(stack_data)

        # M02（S01）：六张较新表随 JSON 往返（旧 JSON 无键 = 空表）
        new_tables = _io_deserialize_new_tables(data)

        return EditProject(
            project_id=data.get("project_id", ""),
            name=data.get("name", "Untitled Project"),
            assets=assets,
            bins=bins,
            timelines=timelines,
            compositions=data.get("compositions", {}),
            color_stacks=color_stacks,
            comp_clips=data.get("comp_clips", {}),
            smart_bins=smart_bins,
            render_cache_manifests=data.get("render_cache_manifests", {}),
            render_cache_settings=data.get("render_cache_settings"),
            proxy_policy=data.get("proxy_policy"),
            root_bin_id=data.get("root_bin_id", ""),
            active_timeline_id=data.get("active_timeline_id"),
            active_comp_clip_id=data.get("active_comp_clip_id"),
            created_at=data.get("created_at", 0.0),
            modified_at=data.get("modified_at", 0.0),
            revision=int(data.get("revision", 0) or 0),
            project_sample_rate=int(data.get("project_sample_rate", 0) or 0),
            interpretation_overrides=new_tables["interpretation_overrides"],
            audio_sync_bindings=new_tables["audio_sync_bindings"],
            sync_diagnostics=new_tables["sync_diagnostics"],
            source_patch_overrides=new_tables["source_patch_overrides"],
            color_groups=new_tables["color_groups"],
            color_stack_versions=new_tables["color_stack_versions"],
            project_snapshots=new_tables["project_snapshots"],
        )

    @staticmethod
    def _dict_to_asset(data: dict[str, Any]) -> MediaAsset:
        """将字典转换为 MediaAsset（兼容不含日志字段的旧工程）"""
        # AUD-18210：手改/异常工具产出的工程 JSON 属外部数据，字段可能
        # 是显式 null——data.get("keywords", ()) 在字段存在且为 null 时
        # 返回 None，tuple(None)/dict(None) 直接 TypeError，metadata=None
        # 传入后下游 .get 崩溃。逐字段做 isinstance 校验 + 默认值兜底。
        raw_keywords = data.get("keywords")
        keywords: tuple[str, ...] = (
            tuple(raw_keywords)
            if isinstance(raw_keywords, (list, tuple)) else ()
        )
        raw_metadata = data.get("metadata")
        metadata: dict[str, Any] = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        try:
            rating = int(data.get("rating", 0))
        except (TypeError, ValueError):
            rating = 0
        return MediaAsset(
            asset_id=data.get("asset_id", ""),
            path=data.get("path", ""),
            media_type=data.get("media_type", "image"),
            display_name=data.get("display_name", ""),
            metadata=metadata,
            thumbnail_key=data.get("thumbnail_key"),
            proxy_path=data.get("proxy_path"),
            proxy_status=data.get("proxy_status", "none"),
            is_offline=data.get("is_offline", False),
            color_metadata=data.get("color_metadata"),
            created_at=data.get("created_at", 0.0),
            reel=data.get("reel", ""),
            scene=data.get("scene", ""),
            shot=data.get("shot", ""),
            take=data.get("take", ""),
            keywords=keywords,
            rating=rating,
            flag=data.get("flag", "none"),
            color_label=data.get("color_label", "none"),
            comment=data.get("comment", ""),
            # DESK-01B：内嵌资源身份（缺省 None = 外部路径素材）
            resource_uri=data.get("resource_uri"),
            path_original=data.get("path_original"),
            # DESK-04A：捕获未知键（load→save 不丢）
            _extra_fields=split_extras(data, MediaAsset._KNOWN_KEYS),
        )

    @staticmethod
    def _dict_to_smart_bin(data: dict[str, Any]) -> SmartBin:
        """将字典转换为 SmartBin（兼容旧数据缺字段）"""
        return SmartBin(
            bin_id=data.get("bin_id", ""),
            name=data.get("name", ""),
            filter_type=data.get("filter_type", "custom"),
            filter_params=dict(data.get("filter_params", {})),
            # DESK-04A：捕获未知键（load→save 不丢）
            _extra_fields=split_extras(data, SmartBin._KNOWN_KEYS),
        )

    @staticmethod
    def _dict_to_bin(data: dict[str, Any]) -> ProjectBin:
        """将字典转换为 ProjectBin"""
        # 处理 asset_ids 和 timeline_ids（可能是 list 或 tuple）
        asset_ids = tuple(data.get("asset_ids", []))
        timeline_ids = tuple(data.get("timeline_ids", []))

        return ProjectBin(
            bin_id=data.get("bin_id", ""),
            name=data.get("name", ""),
            parent_id=data.get("parent_id"),
            asset_ids=asset_ids,
            timeline_ids=timeline_ids,
            # DESK-04A：捕获未知键（load→save 不丢）
            _extra_fields=split_extras(data, ProjectBin._KNOWN_KEYS),
        )

    @staticmethod
    def _dict_to_timeline_ref(data: dict[str, Any]) -> TimelineSequenceRef:
        """将字典转换为 TimelineSequenceRef"""
        return TimelineSequenceRef(
            timeline_id=data.get("timeline_id", ""),
            name=data.get("name", ""),
            fps=data.get("fps", 24.0),
            width=data.get("width", 1920),
            height=data.get("height", 1080),
            sample_rate=data.get("sample_rate", 48000),
            format_settings=dict(data.get("format_settings", {})),
            created_at=data.get("created_at", 0.0),
            # DESK-04A：捕获未知键（load→save 不丢）
            _extra_fields=split_extras(data, TimelineSequenceRef._KNOWN_KEYS),
        )

    # ==================== 版本迁移 ====================

    @staticmethod
    def _migrate_data(data: dict[str, Any], from_version: int) -> dict[str, Any]:
        """
        迁移旧版本数据到当前版本。

        Args:
            data: 旧版本数据
            from_version: 旧版本号

        Returns:
            迁移后的数据

        Note:
            目前版本为 3，如果未来有新版本，需要在这里添加迁移逻辑。
        """
        # v3 是初始版本，无需迁移
        if from_version == SERIALIZATION_VERSION:
            return data

        # v4 → v5: clip TransformParams 归一化模型 → 像素模型。
        # animated_params 的 key 从 transform.position_x/y 改名为
        # transform.translate_x/y，并把关键帧的归一化值乘以参考分辨率换算成像素。
        # clip 的 transform 块本身由 TransformParams.from_dict 做字段级兜底，
        # 这里只处理 from_dict 管不到的 animated_params key 改名。
        if from_version < 5:
            data = EditProjectSerializer._migrate_v4_to_v5(data)

        return data

    @staticmethod
    def _migrate_v4_to_v5(data: dict[str, Any]) -> dict[str, Any]:
        """v4→v5: rename animated_params keys for the pixel-space transform model.

        ``transform.position_x`` → ``transform.translate_x`` (value × REF_W)
        ``transform.position_y`` → ``transform.translate_y`` (value × REF_H)
        Scale/rotation keys are unchanged. The clip ``transform`` dict itself
        is migrated lazily by ``TransformParams.from_dict`` (legacy field
        detection), so we only touch animated_params key names + values here.

        结构先验（AUD-18206）：clip 及其 ``animated_params`` 实际存放在
        wrapper 顶层的 ``timeline_sequences: {seq_id: seq_dict}``，seq_dict
        按 ``video_tracks``/``audio_tracks``/``subtitle_tracks``（旧布局为
        统一 ``tracks``）分轨，每轨 ``clips`` 为 clip 字典列表。
        ``project["timelines"]`` 是 TimelineSequenceRef 元数据字典——ref
        从不携带 clip，绝不能当序列列表迭代（旧实现迭代 ref 字典得到
        字符串 key，``tl.get("clips")`` 抛 AttributeError，旧版工程加载
        必败）。
        """
        REF_W, REF_H = 1920.0, 1080.0
        key_map = {
            "transform.position_x": ("transform.translate_x", REF_W),
            "transform.position_y": ("transform.translate_y", REF_H),
        }

        # 兼容传入完整 wrapper 或 project 子字典两种调用位置：
        # clip 容器可能在顶层，也可能嵌套在 "project" 键下。
        scopes: list[dict[str, Any]] = [data]
        nested_project = data.get("project")
        if isinstance(nested_project, dict):
            scopes.append(nested_project)

        for scope in scopes:
            # 序列字典来源 1：timeline_sequences（现代布局）。
            seq_dicts: list[Any] = []
            seqs = scope.get("timeline_sequences")
            if isinstance(seqs, dict):
                seq_dicts.extend(seqs.values())
            # 序列字典来源 2：timelines 作为序列列表（极旧布局的防御性
            # 回退）。若是 dict 则为 TimelineSequenceRef 映射——无 clip，
            # 跳过。
            legacy_timelines = scope.get("timelines")
            if isinstance(legacy_timelines, list):
                seq_dicts.extend(legacy_timelines)

            for seq_data in seq_dicts:
                if not isinstance(seq_data, dict):
                    continue
                for track_key in (
                    "video_tracks", "audio_tracks", "subtitle_tracks", "tracks",
                ):
                    tracks = seq_data.get(track_key)
                    if not isinstance(tracks, list):
                        continue
                    for track in tracks:
                        if not isinstance(track, dict):
                            continue
                        clips = track.get("clips")
                        if not isinstance(clips, list):
                            continue
                        for clip in clips:
                            if not isinstance(clip, dict):
                                continue
                            EditProjectSerializer._migrate_clip_anim_keys(
                                clip, key_map
                            )
        return data

    @staticmethod
    def _migrate_clip_anim_keys(
        clip: dict[str, Any],
        key_map: dict[str, tuple[str, float]],
    ) -> None:
        """Rename/scale one clip dict's animated_params keys (v4→v5, in place)."""
        anim = clip.get("animated_params")
        if not isinstance(anim, dict):
            return
        for old_key, (new_key, scale) in list(key_map.items()):
            if old_key in anim:
                entry = anim.pop(old_key)
                # Scale keyframe values from normalized to pixels.
                # AnimatableParam.to_dict 把 keyframes 序列化为 KeyframeTrack
                # 字典（内层 "keyframes" 才是列表）——两种形状都兼容。
                kfs = entry.get("keyframes") if isinstance(entry, dict) else None
                if isinstance(kfs, dict):
                    kfs = kfs.get("keyframes")
                if isinstance(kfs, list):
                    for kf in kfs:
                        if isinstance(kf, dict) and "value" in kf:
                            try:
                                kf["value"] = float(kf["value"]) * scale
                            except (TypeError, ValueError):
                                pass
                anim[new_key] = entry

    # ==================== 文件操作 ====================

    @staticmethod
    def _atomic_write_text(filepath: Path, text: str) -> None:
        """Atomically replace ``filepath`` with ``text`` (AUD-18208).

        Implements the module's documented ``.topos.autosave``/``.topos.bak``
        convention: the payload is written to a temp file in the same
        directory and fsynced, the previous file (if any) is copied to
        ``<name>.bak``, and the temp file is ``os.replace``d into place.
        A crash at any point leaves either the old file, the backup, or
        the new file — never a truncated half-written project.

        Raises:
            OSError: any filesystem failure (caller decides how to report).
            TypeError: ``text`` is not writable text (serialization bug).
        """
        filepath.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = filepath.with_name(filepath.name + ".autosave")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

        if filepath.exists():
            backup_path = filepath.with_name(filepath.name + ".bak")
            # Best-effort rotation: a failed backup must not block the
            # atomic replace of the main file.
            try:
                shutil.copy2(filepath, backup_path)
            except OSError as e:
                logger.warning(
                    f"Failed to rotate backup for {filepath}: {e}"
                )

        os.replace(tmp_path, filepath)

    @staticmethod
    def save_to_file(project: EditProject, filepath: str | Path) -> bool:
        """
        将项目保存到文件（基础版本）。

        Args:
            project: EditProject 实例
            filepath: 文件路径

        Returns:
            是否成功
        """
        filepath = Path(filepath)
        try:
            # 序列化并原子写入（AUD-18208：中断不再损坏唯一工程文件）
            json_str = EditProjectSerializer.to_json(project)
            EditProjectSerializer._atomic_write_text(filepath, json_str)

            logger.info(f"Project saved to: {filepath}")
            return True
        except OSError as e:
            logger.error(f"Failed to write project file: {filepath}, {e}")
            return False
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize project: {filepath}, {e}")
            return False

    @staticmethod
    def save_to_file_with_ui_state(
        project: EditProject,
        filepath: str | Path,
        ui_state: UIState,
        timeline_sequences: dict[str, TimelineSequence] | None = None,
    ) -> bool:
        """
        将项目保存到文件（包含 UI 状态和完整时间线数据）。

        Args:
            project: EditProject 实例
            filepath: 文件路径
            ui_state: UI 状态实例
            timeline_sequences: 完整的 TimelineSequence 数据字典（可选）

        Returns:
            是否成功
        """
        filepath = Path(filepath)
        try:
            # 序列化并原子写入（AUD-18208）
            json_str = EditProjectSerializer.to_json_with_ui_state(
                project, ui_state, timeline_sequences
            )
            EditProjectSerializer._atomic_write_text(filepath, json_str)

            logger.info(f"Project saved to: {filepath}")
            return True
        except OSError as e:
            logger.error(f"Failed to write project file: {filepath}, {e}")
            return False
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize project: {filepath}, {e}")
            return False

    @staticmethod
    def load_from_file(filepath: str | Path) -> Optional[EditProject]:
        """
        从文件加载项目（基础版本）。

        Args:
            filepath: 文件路径

        Returns:
            EditProject 实例，失败返回 None
        """
        filepath = Path(filepath)
        try:
            if not filepath.exists():
                logger.warning(f"Project file not found: {filepath}")
                return None

            json_str = filepath.read_text(encoding="utf-8")
            project = EditProjectSerializer.from_json(json_str)

            logger.info(f"Project loaded from: {filepath}")
            return project
        except OSError as e:
            # AUD-18209: 分层上报——文件系统错误与数据损坏不再混为一条
            # 泛化日志（UnicodeDecodeError 属 ValueError，走下一分支）。
            logger.error(f"Failed to read project file: {filepath}, {e}")
            return None
        except ValueError as e:
            # 含 json.JSONDecodeError、版本/类型不兼容、显式 ValueError。
            logger.error(
                f"Corrupted or unsupported project file: {filepath}, {e}"
            )
            return None
        except (TypeError, KeyError) as e:
            logger.error(f"Malformed project data: {filepath}, {e!r}")
            return None

    @staticmethod
    def load_from_file_with_ui_state(
        filepath: str | Path
    ) -> Optional[tuple[EditProject, UIState | None, dict[str, TimelineSequence] | None]]:
        """
        从文件加载项目（包含 UI 状态和完整时间线数据）。

        Args:
            filepath: 文件路径

        Returns:
            元组：(EditProject, UIState 或 None, TimelineSequence 字典或 None)
            失败返回 None
        """
        filepath = Path(filepath)
        try:
            if not filepath.exists():
                logger.warning(f"Project file not found: {filepath}")
                return None

            json_str = filepath.read_text(encoding="utf-8")
            result = EditProjectSerializer.from_json_with_ui_state(json_str)

            logger.info(f"Project loaded from: {filepath}")
            return result
        except OSError as e:
            logger.error(f"Failed to read project file: {filepath}, {e}")
            return None
        except ValueError as e:
            logger.error(
                f"Corrupted or unsupported project file: {filepath}, {e}"
            )
            return None
        except (TypeError, KeyError) as e:
            logger.error(f"Malformed project data: {filepath}, {e!r}")
            return None

    @staticmethod
    def is_edit_project_file(filepath: str | Path) -> bool:
        """
        判断文件是否为编辑项目文件。

        Args:
            filepath: 文件路径

        Returns:
            是否为编辑项目文件
        """
        filepath = Path(filepath)
        if not filepath.exists():
            return False

        try:
            json_str = filepath.read_text(encoding="utf-8")
            data = json.loads(json_str)
            return data.get("project_type") == PROJECT_TYPE
        except Exception:
            return False

    # ==================== Roundtrip 验证 ====================

    @staticmethod
    def validate_roundtrip(project: EditProject) -> bool:
        """
        验证序列化 roundtrip 是否一致。

        Args:
            project: EditProject 实例

        Returns:
            roundtrip 是否一致
        """
        try:
            json_str = EditProjectSerializer.to_json(project)
            restored = EditProjectSerializer.from_json(json_str)

            # 比较 project_id（唯一标识）
            if project.project_id != restored.project_id:
                logger.error("Roundtrip failed: project_id mismatch")
                return False

            # 比较 assets 数量
            if len(project.assets) != len(restored.assets):
                logger.error(
                    f"Roundtrip failed: assets count mismatch "
                    f"{len(project.assets)} vs {len(restored.assets)}"
                )
                return False

            # 比较 bins 数量
            if len(project.bins) != len(restored.bins):
                logger.error(
                    f"Roundtrip failed: bins count mismatch "
                    f"{len(project.bins)} vs {len(restored.bins)}"
                )
                return False

            # 比较 timelines 数量
            if len(project.timelines) != len(restored.timelines):
                logger.error(
                    f"Roundtrip failed: timelines count mismatch "
                    f"{len(project.timelines)} vs {len(restored.timelines)}"
                )
                return False

            # 比较每个 asset 的关键字段
            for asset_id, asset in project.assets.items():
                restored_asset = restored.assets.get(asset_id)
                if restored_asset is None:
                    logger.error(f"Roundtrip failed: missing asset {asset_id}")
                    return False
                if asset.path != restored_asset.path:
                    logger.error(f"Roundtrip failed: asset path mismatch {asset_id}")
                    return False
                if asset.media_type != restored_asset.media_type:
                    logger.error(f"Roundtrip failed: asset media_type mismatch {asset_id}")
                    return False

            # 比较每个 bin 的关键字段
            for bin_id, bin_obj in project.bins.items():
                restored_bin = restored.bins.get(bin_id)
                if restored_bin is None:
                    logger.error(f"Roundtrip failed: missing bin {bin_id}")
                    return False
                if set(bin_obj.asset_ids) != set(restored_bin.asset_ids):
                    logger.error(f"Roundtrip failed: bin asset_ids mismatch {bin_id}")
                    return False

            return True
        except Exception as e:
            logger.error(f"Roundtrip validation failed: {e}")
            return False
