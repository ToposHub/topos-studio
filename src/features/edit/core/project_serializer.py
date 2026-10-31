# SUPERSEDED(2026-08-19 审计): 已被新实现取代且无生产引用，仅存档。
"""
EditProjectSerializer - 序列化/反序列化 .topos 项目文件。

## 文件格式

.topos 是 JSON 格式的项目文件，包含：
- 项目元数据（名称、创建时间、修改时间）
- 素材列表（MediaAsset）
- 素材箱（ProjectBin）
- 时间线引用（TimelineSequenceRef）
- 项目设置（分辨率、帧率等）

## 设计原则

- 与旧工程格式区分，.topos 是图像/视频的项目包文件
- 支持相对路径和绝对路径转换（项目移动时素材路径自动调整）
- 纀录素材原始路径，支持离线素材检测和重链接
"""

import json
import os
import logging
import time
from typing import Optional, Dict, List

from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin, TimelineSequenceRef

# 导入统一工程包 IO
from src.shared.project import ToposProjectIO, ToposProjectIOConfig

logger = logging.getLogger(__name__)


class EditProjectSerializer:
    """
    EditProject 序列化器。

    负责 .topos 文件的读写，支持：
    - JSON 序列化/反序列化
    - 相对路径转换（存储时转为相对路径，加载时转为绝对路径）
    - 离线素材检测
    """

    # 项目文件扩展名（统一工程包格式）
    PROJECT_EXTENSION = ".topos"

    # 项目文件格式版本
    FORMAT_VERSION = "1.0"

    # 支持的视频分辨率预设
    RESOLUTION_PRESETS = {
        "4K DCI": (4096, 2160),
        "4K UHD": (3840, 2160),
        "2K DCI": (2048, 1080),
        "2K QHD": (2560, 1440),
        "1080p": (1920, 1080),
        "720p": (1280, 720),
        "480p": (854, 480),
        "Custom": None,
    }

    # 支持的帧率预设
    FPS_PRESETS = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]

    # 音频采样率预设
    SAMPLE_RATE_PRESETS = [44100, 48000, 96000]

    @staticmethod
    def get_project_path(project_dir: str, project_name: str) -> str:
        """
        生成项目文件路径。

        Args:
            project_dir: 项目目录
            project_name: 项目名称

        Returns:
            完整的项目文件路径
        """
        # 确保项目名称合法（去除特殊字符）
        safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in project_name)
        filename = f"{safe_name}{EditProjectSerializer.PROJECT_EXTENSION}"
        return os.path.join(project_dir, filename)

    @staticmethod
    def is_project_file(filepath: str) -> bool:
        """检查文件是否是 .topos 项目文件"""
        return filepath.lower().endswith(EditProjectSerializer.PROJECT_EXTENSION)

    @staticmethod
    def save_project(project: EditProject, filepath: str) -> bool:
        """
        保存项目到文件（ZIP 包格式）。

        现在使用 ToposProjectIO 保存到 .topos ZIP 包格式，
        确保统一工程包架构。

        Args:
            project: EditProject 实例
            filepath: 项目文件路径

        Returns:
            True if successful, False otherwise
        """
        try:
            # 导入转换函数
            from src.features.edit.core.edit_project_io import convert_edit_project_to_topos_project

            # 转换 EditProject 为 ToposProject
            topos_project = convert_edit_project_to_topos_project(project)

            # 准备完整数据
            full_assets_data = {
                asset_id: {
                    "asset_id": asset.asset_id,
                    "path": asset.path,
                    "media_type": asset.media_type,
                    "display_name": asset.display_name,
                    "metadata": asset.metadata,
                    "thumbnail_key": asset.thumbnail_key,
                    "proxy_status": asset.proxy_status,
                    "color_metadata": asset.color_metadata,
                    "created_at": asset.created_at,
                }
                for asset_id, asset in project.assets.items()
            }

            full_bins_data = {
                bin_id: {
                    "bin_id": bin_obj.bin_id,
                    "name": bin_obj.name,
                    "parent_id": bin_obj.parent_id,
                    "asset_ids": list(bin_obj.asset_ids),
                    "timeline_ids": list(bin_obj.timeline_ids),
                }
                for bin_id, bin_obj in project.bins.items()
            }

            full_timelines_data = {
                timeline_id: {
                    "timeline_id": timeline.timeline_id,
                    "name": timeline.name,
                    "fps": timeline.fps,
                    "width": timeline.width,
                    "height": timeline.height,
                    "sample_rate": timeline.sample_rate,
                    "format_settings": dict(timeline.format_settings),
                    "created_at": timeline.created_at,
                }
                for timeline_id, timeline in project.timelines.items()
            }

            # 使用 ToposProjectIO 保存
            config = ToposProjectIOConfig(
                serialization_format="json",
                compress_level=6,
                relative_paths=True,
                include_cache_assets=False,
            )

            success = ToposProjectIO.save(
                topos_project,
                filepath,
                config,
                full_assets_data=full_assets_data,
                full_bins_data=full_bins_data,
                full_timelines_data=full_timelines_data,
            )

            if success:
                logger.info(f"Project saved as ZIP package: {filepath}")
            return success

        except Exception as e:
            logger.error(f"Failed to save project: {e}")
            import traceback
            traceback.print_exc()
            return False

    @staticmethod
    def load_project(filepath: str) -> Optional[EditProject]:
        """
        从文件加载项目。

        将相对路径转换为绝对路径，并检测离线素材。

        Args:
            filepath: 项目文件路径

        Returns:
            EditProject 实例，或 None if loading failed
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 获取项目目录（用于计算绝对路径）
            project_dir = os.path.dirname(os.path.abspath(filepath))

            # 检查格式版本

            # 构建素材字典
            assets: Dict[str, MediaAsset] = {}
            for asset_id, asset_data in data.get("assets", {}).items():
                # 转换为绝对路径
                path_relative = asset_data.get("path_relative", "")
                path_original = asset_data.get("path_original", "")
                absolute_path = EditProjectSerializer._to_absolute_path(path_relative, project_dir)

                # 如果相对路径转换失败，尝试使用原始路径
                if not absolute_path or not os.path.exists(absolute_path):
                    if path_original and os.path.exists(path_original):
                        absolute_path = path_original
                    elif path_original:
                        # 离线素材：保留原始路径，但标记为离线
                        absolute_path = path_original

                asset = MediaAsset(
                    asset_id=asset_data["asset_id"],
                    path=absolute_path,
                    media_type=asset_data["media_type"],
                    display_name=asset_data["display_name"],
                    metadata=asset_data.get("metadata", {}),
                    thumbnail_key=asset_data.get("thumbnail_key"),
                    proxy_status=asset_data.get("proxy_status", "none"),
                    color_metadata=asset_data.get("color_metadata"),
                    created_at=asset_data.get("created_at", 0.0),
                )
                assets[asset_id] = asset

            # 构建素材箱字典
            bins: Dict[str, ProjectBin] = {}
            for bin_id, bin_data in data.get("bins", {}).items():
                bin_obj = ProjectBin(
                    bin_id=bin_data["bin_id"],
                    name=bin_data["name"],
                    parent_id=bin_data.get("parent_id"),
                    asset_ids=tuple(bin_data.get("asset_ids", [])),
                    timeline_ids=tuple(bin_data.get("timeline_ids", [])),
                )
                bins[bin_id] = bin_obj

            # 构建时间线字典
            timelines: Dict[str, TimelineSequenceRef] = {}
            for timeline_id, timeline_data in data.get("timelines", {}).items():
                timeline = TimelineSequenceRef(
                    timeline_id=timeline_data["timeline_id"],
                    name=timeline_data["name"],
                    fps=timeline_data.get("fps", 24.0),
                    width=timeline_data.get("width", 1920),
                    height=timeline_data.get("height", 1080),
                    sample_rate=timeline_data.get("sample_rate", 48000),
                    format_settings=dict(timeline_data.get("format_settings", {})),
                    created_at=timeline_data.get("created_at", 0.0),
                )
                timelines[timeline_id] = timeline

            # 构建 EditProject
            project = EditProject(
                project_id=data["project_id"],
                name=data["name"],
                assets=assets,
                bins=bins,
                timelines=timelines,
                root_bin_id=data.get("root_bin_id", ""),
                active_timeline_id=data.get("active_timeline_id"),
                created_at=data.get("created_at", 0.0),
                modified_at=data.get("modified_at", 0.0),
            )

            logger.info(f"Project loaded: {filepath}, {len(assets)} assets, {len(timelines)} timelines")
            return project

        except Exception as e:
            logger.error(f"Failed to load project: {e}")
            import traceback
            traceback.print_exc()
            return None

    @staticmethod
    def check_offline_assets(project: EditProject) -> List[MediaAsset]:
        """
        检查项目中的离线素材。

        Args:
            project: EditProject 实例

        Returns:
            离线素材列表（路径不存在的素材）
        """
        offline_assets = []
        for asset in project.assets.values():
            if not os.path.exists(asset.path):
                offline_assets.append(asset)
        return offline_assets

    @staticmethod
    def relink_assets(project: EditProject, relink_map: Dict[str, str]) -> EditProject:
        """
        重新链接素材。

        Args:
            project: EditProject 实例
            relink_map: {asset_id: new_path} 映射

        Returns:
            新的 EditProject 实例（更新后的素材路径）
        """
        new_assets = dict(project.assets)
        for asset_id, new_path in relink_map.items():
            if asset_id in new_assets:
                new_assets[asset_id] = new_assets[asset_id].with_path(new_path)

        from dataclasses import replace as _dc_replace
        # M02：replace 语义保留六张较新表与 revision（显式构造会抹掉）
        return _dc_replace(
            project, assets=new_assets, modified_at=time.time())

    @staticmethod
    def _to_relative_path(absolute_path: str, base_dir: str) -> str:
        """
        将绝对路径转换为相对路径。

        Args:
            absolute_path: 绝对路径
            base_dir: 基准目录（通常是项目目录）

        Returns:
            相对路径，如果无法转换则返回原始路径
        """
        try:
            # 规范化路径
            abs_path = os.path.normpath(os.path.abspath(absolute_path))
            base = os.path.normpath(os.path.abspath(base_dir))

            # 计算相对路径
            rel_path = os.path.relpath(abs_path, base)

            # 如果相对路径以 .. 开头且超出项目目录，返回原始路径
            if rel_path.startswith(".."):
                return absolute_path

            return rel_path

        except Exception:
            return absolute_path

    @staticmethod
    def _to_absolute_path(relative_path: str, base_dir: str) -> str:
        """
        将相对路径转换为绝对路径。

        Args:
            relative_path: 相对路径
            base_dir: 基准目录（通常是项目目录）

        Returns:
            绝对路径
        """
        try:
            # 如果是绝对路径，直接返回
            if os.path.isabs(relative_path):
                return os.path.normpath(relative_path)

            # 组合相对路径和基准目录
            abs_path = os.path.normpath(os.path.join(base_dir, relative_path))
            return abs_path

        except Exception:
            return relative_path

    @staticmethod
    def create_default_project(
        name: str,
        project_dir: str,
        fps: float = 24.0,
        width: int = 1920,
        height: int = 1080,
        sample_rate: int = 48000,
    ) -> EditProject:
        """
        创建新的默认项目。

        Args:
            name: 项目名称
            project_dir: 项目目录
            fps: 默认帧率
            width: 默认视频宽度
            height: 默认视频高度
            sample_rate: 默认音频采样率

        Returns:
            新的 EditProject 实例
        """
        # 使用 EditProject.create() 创建基础项目
        project = EditProject.create(name)

        # 创建默认时间线（使用指定参数）
        project = project.create_timeline(
            name=f"{name} Timeline",
            fps=fps,
            width=width,
            height=height,
        )

        return project
