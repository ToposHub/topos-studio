"""
OfflineAssetManager - 离线素材统一管理入口。

P3 统一工程包架构方案 - Relink/离线素材统一管理入口。

提供统一的离线素材检查和重链接入口，支持：
- ToposProject 和 EditProject 两种工程类型
- 批量离线检查
- 统一重链接对话框入口
- 离线状态报告

设计原则：
- 统一入口，所有离线检查通过 OfflineAssetManager
- 支持多种工程类型（ToposProject/EditProject）
- 不可变操作，返回新的工程实例
- 完整类型注解

参考文档：
- docs/library_media_pool_project_architecture_plan_2026-05-19.md
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union, TYPE_CHECKING

from .edit_project_adapter import edit_project_adapter
from .topos_project import is_virtual_asset_path

if TYPE_CHECKING:
    from .topos_project import ToposProject, MediaAssetRef

logger = logging.getLogger(__name__)


# ================================================================
# OfflineAssetReport - 离线素材报告
# ================================================================

@dataclass(frozen=True)
class OfflineAssetReport:
    """
    离线素材检查报告。

    包含离线素材的详细信息和建议。

    Attributes:
        total_assets: 素材总数
        offline_count: 离线素材数量
        online_count: 在线素材数量
        offline_assets: 离线素材列表（包含详细信息）
        missing_paths: 缺失路径列表
        suggested_search_dirs: 建议搜索目录
        timestamp: 检查时间戳
        project_path: 工程文件路径
    """
    total_assets: int
    offline_count: int
    online_count: int
    offline_assets: list[dict[str, Any]]
    missing_paths: list[str]
    suggested_search_dirs: list[str]
    timestamp: float
    project_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典"""
        return {
            "total_assets": self.total_assets,
            "offline_count": self.offline_count,
            "online_count": self.online_count,
            "offline_assets": self.offline_assets,
            "missing_paths": self.missing_paths,
            "suggested_search_dirs": self.suggested_search_dirs,
            "timestamp": self.timestamp,
            "project_path": self.project_path,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> OfflineAssetReport:
        """从字典反序列化"""
        return OfflineAssetReport(
            total_assets=data.get("total_assets", 0),
            offline_count=data.get("offline_count", 0),
            online_count=data.get("online_count", 0),
            offline_assets=data.get("offline_assets", []),
            missing_paths=data.get("missing_paths", []),
            suggested_search_dirs=data.get("suggested_search_dirs", []),
            timestamp=data.get("timestamp", 0.0),
            project_path=data.get("project_path"),
        )

    def has_offline_assets(self) -> bool:
        """是否有离线素材"""
        return self.offline_count > 0

    def get_summary(self) -> str:
        """获取摘要文本"""
        if self.offline_count == 0:
            return "All assets are online"
        return f"{self.offline_count}/{self.total_assets} assets offline"


# ================================================================
# RelinkResult - 重链接结果
# ================================================================

@dataclass(frozen=True)
class RelinkResult:
    """
    重链接操作结果。

    Attributes:
        success: 是否成功
        relinked_count: 成功重链接数量
        failed_count: 失败数量
        relink_map: 重链接映射（asset_id -> new_path）
        failed_assets: 失败的素材列表
        timestamp: 操作时间戳
    """
    success: bool
    relinked_count: int
    failed_count: int
    relink_map: dict[str, str]
    failed_assets: list[dict[str, Any]]
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典"""
        return {
            "success": self.success,
            "relinked_count": self.relinked_count,
            "failed_count": self.failed_count,
            "relink_map": self.relink_map,
            "failed_assets": self.failed_assets,
            "timestamp": self.timestamp,
        }


# ================================================================
# OfflineAssetManager - 离线素材统一管理入口
# ================================================================

class OfflineAssetManager:
    """
    离线素材统一管理入口。

    提供统一的离线素材检查和重链接入口，支持多种工程类型。

    ## 核心功能

    1. **离线素材检查**
       - `check_offline_assets(project)` - 检查工程中的离线素材
       - `generate_report(project)` - 生成详细的离线状态报告

    2. **重链接操作**
       - `relink_asset(project, asset_id, new_path)` - 单个素材重链接
       - `batch_relink(project, relink_map)` - 批量重链接

    3. **辅助功能**
       - `suggest_search_dirs(project)` - 建议搜索目录
       - `find_matching_file(filename, search_dirs)` - 搜索匹配文件

    ## 使用方法

    ```python
    from src.shared.project.offline_asset_manager import OfflineAssetManager

    # 检查离线素材
    report = OfflineAssetManager.check_offline_assets(topos_project)

    if report.has_offline_assets():
        # 显示重链接对话框
        OfflineAssetManager.show_relink_dialog(project, parent_widget)

    # 执行重链接
    result = OfflineAssetManager.batch_relink(project, relink_map)
    ```

    ## 工程类型支持

    支持 ToposProject 和 EditProject 两种工程类型。
    ToposProject 使用 MediaAssetRef，EditProject 使用 MediaAsset。
    EditProject operations are dispatched through edit_project_adapter to
    maintain clean layer boundaries (src/shared does not import src/features).
    """

    # ==================== 离线素材检查 ====================

    @staticmethod
    def check_offline_assets(
        project: Union["ToposProject", Any]
    ) -> OfflineAssetReport:
        """
        检查工程中的离线素材。

        Args:
            project: ToposProject 或 EditProject 实例

        Returns:
            OfflineAssetReport 离线素材报告
        """
        from .topos_project import ToposProject

        timestamp = time.time()
        offline_assets: list[dict[str, Any]] = []
        missing_paths: list[str] = []

        if isinstance(project, ToposProject):
            # ToposProject 使用 MediaAssetRef
            total_assets = len(project.assets)
            for asset_id, asset in project.assets.items():
                is_offline = (
                    not is_virtual_asset_path(asset.path)
                    and not os.path.exists(asset.path)
                )
                if is_offline:
                    offline_assets.append({
                        "asset_id": asset.asset_id,
                        "path": asset.path,
                        "media_type": asset.media_type,
                        "display_name": asset.display_name,
                        "is_offline": True,
                    })
                    missing_paths.append(asset.path)

        elif edit_project_adapter.is_edit_project(project):
            # EditProject 使用 MediaAsset (via adapter)
            total_assets = len(project.assets)
            for asset_id, asset in project.assets.items():
                is_offline = (
                    not is_virtual_asset_path(asset.path)
                    and not os.path.exists(asset.path)
                )
                if is_offline:
                    offline_assets.append({
                        "asset_id": asset.asset_id,
                        "path": asset.path,
                        "media_type": asset.media_type,
                        "display_name": asset.display_name,
                        "is_offline": True,
                        "metadata": asset.metadata,
                    })
                    missing_paths.append(asset.path)
        else:
            # AUD-36816: an unrecognized project type is a programming error.
            # relink_asset / batch_relink already raise ValueError for it;
            # silently returning an empty report here would let callers
            # believe "everything is online" for an object we never inspected.
            raise ValueError(f"Unsupported project type: {type(project)}")

        offline_count = len(offline_assets)
        online_count = total_assets - offline_count

        # 生成建议搜索目录
        suggested_search_dirs = OfflineAssetManager._suggest_search_dirs(missing_paths)

        return OfflineAssetReport(
            total_assets=total_assets,
            offline_count=offline_count,
            online_count=online_count,
            offline_assets=offline_assets,
            missing_paths=missing_paths,
            suggested_search_dirs=suggested_search_dirs,
            timestamp=timestamp,
            project_path=None,
        )

    @staticmethod
    def check_offline_assets_with_path(
        project: Union["ToposProject", Any],
        project_path: str,
    ) -> OfflineAssetReport:
        """
        检查工程中的离线素材（带工程路径）。

        Args:
            project: ToposProject 或 EditProject 实例
            project_path: 工程文件路径

        Returns:
            OfflineAssetReport 离线素材报告
        """
        report = OfflineAssetManager.check_offline_assets(project)
        # 创建带工程路径的新报告
        return OfflineAssetReport(
            total_assets=report.total_assets,
            offline_count=report.offline_count,
            online_count=report.online_count,
            offline_assets=report.offline_assets,
            missing_paths=report.missing_paths,
            suggested_search_dirs=report.suggested_search_dirs,
            timestamp=report.timestamp,
            project_path=project_path,
        )

    @staticmethod
    def get_offline_asset_list(
        project: Union["ToposProject", Any]
    ) -> list[Any]:
        """
        获取离线素材列表（返回原始素材对象）。

        Args:
            project: ToposProject 或 EditProject 实例

        Returns:
            离线素材列表（MediaAssetRef 或 MediaAsset）
        """
        from .topos_project import ToposProject

        offline_list: list[Any] = []

        if isinstance(project, ToposProject):
            for asset in project.assets.values():
                # AUD-36815: topos:// virtual assets are app-generated and
                # never exist on disk; they must not count as offline.
                if not is_virtual_asset_path(asset.path) and not os.path.exists(asset.path):
                    offline_list.append(asset)
        elif edit_project_adapter.is_edit_project(project):
            for asset in project.assets.values():
                if not is_virtual_asset_path(asset.path) and not os.path.exists(asset.path):
                    offline_list.append(asset)

        return offline_list

    # ==================== 重链接操作 ====================

    @staticmethod
    def relink_asset(
        project: Union["ToposProject", Any],
        asset_id: str,
        new_path: str,
    ) -> Union["ToposProject", Any]:
        """
        重链接单个素材。

        Args:
            project: ToposProject 或 EditProject 实例
            asset_id: 素材 ID
            new_path: 新路径

        Returns:
            新的工程实例

        Raises:
            ValueError: 如果素材不存在
        """
        from .topos_project import ToposProject
        from .topos_project_io import ToposProjectIO

        if isinstance(project, ToposProject):
            return ToposProjectIO.relink_assets(project, {asset_id: new_path})
        elif edit_project_adapter.is_edit_project(project):
            return edit_project_adapter.relink_asset(project, asset_id, new_path)
        else:
            raise ValueError(f"Unsupported project type: {type(project)}")

    @staticmethod
    def batch_relink(
        project: Union["ToposProject", Any],
        relink_map: dict[str, str],
    ) -> tuple[Union["ToposProject", Any], RelinkResult]:
        """
        批量重链接素材。

        Args:
            project: ToposProject 或 EditProject 实例
            relink_map: {asset_id: new_path} 映射

        Returns:
            (新工程实例, RelinkResult)
        """
        from .topos_project import ToposProject
        from .topos_project_io import ToposProjectIO

        timestamp = time.time()
        failed_assets: list[dict[str, Any]] = []
        relinked_count = 0

        if isinstance(project, ToposProject):
            new_project = ToposProjectIO.relink_assets(project, relink_map)
            for asset_id, new_path in relink_map.items():
                asset = new_project.assets.get(asset_id)
                if asset and not asset.is_offline:
                    relinked_count += 1
                else:
                    failed_assets.append({
                        "asset_id": asset_id,
                        "path": new_path,
                    })

        elif edit_project_adapter.is_edit_project(project):
            new_project = edit_project_adapter.batch_relink(project, relink_map)
            for asset_id, new_path in relink_map.items():
                asset = new_project.assets.get(asset_id)
                if asset and not asset.is_offline:
                    relinked_count += 1
                else:
                    failed_assets.append({
                        "asset_id": asset_id,
                        "path": new_path,
                    })
        else:
            raise ValueError(f"Unsupported project type: {type(project)}")

        failed_count = len(relink_map) - relinked_count
        result = RelinkResult(
            success=failed_count == 0,
            relinked_count=relinked_count,
            failed_count=failed_count,
            relink_map=relink_map,
            failed_assets=failed_assets,
            timestamp=timestamp,
        )

        return new_project, result

    # ==================== 辅助功能 ====================

    @staticmethod
    def _suggest_search_dirs(missing_paths: list[str]) -> list[str]:
        """
        根据缺失路径生成建议搜索目录。

        Args:
            missing_paths: 缺失路径列表

        Returns:
            建议搜索目录列表
        """
        import os.path as osp

        suggested: list[str] = []

        # 从缺失路径的父目录推导
        for path in missing_paths:
            parent = osp.dirname(path)
            grandparent = osp.dirname(parent)
            if osp.isdir(grandparent):
                suggested.append(grandparent)

        # 常用目录
        home = osp.expanduser("~")
        common_dirs = [
            osp.join(home, "Desktop"),
            osp.join(home, "Documents"),
            osp.join(home, "Downloads"),
            osp.join(home, "Movies"),
            osp.join(home, "Pictures"),
        ]
        for d in common_dirs:
            if osp.isdir(d) and d not in suggested:
                suggested.append(d)

        return suggested

    @staticmethod
    def find_matching_file(
        filename: str,
        search_dirs: list[str],
        recursive: bool = False,
        max_depth: int = 2,
        deadline: Optional[float] = None,
    ) -> str | None:
        """
        在搜索目录中查找匹配的文件。

        Args:
            filename: 文件名
            search_dirs: 搜索目录列表
            recursive: 是否递归搜索
            max_depth: 最大搜索深度（如果递归）
            deadline: 可选的 ``time.monotonic()`` 截止时刻（AUD-36817
                扫描预算）；超过后立即放弃返回 None

        Returns:
            找到的文件路径，或 None
        """
        import os.path as osp

        for search_dir in search_dirs:
            if not osp.isdir(search_dir):
                continue

            # 直接匹配
            direct_path = osp.join(search_dir, filename)
            if osp.isfile(direct_path):
                return direct_path

            if recursive:
                # 递归搜索
                found = OfflineAssetManager._recursive_search(
                    search_dir, filename, current_depth=0, max_depth=max_depth,
                    deadline=deadline,
                )
                if found:
                    return found

        return None

    @staticmethod
    def _recursive_search(
        directory: str,
        filename: str,
        current_depth: int,
        max_depth: int,
        deadline: Optional[float] = None,
    ) -> str | None:
        """
        递归搜索文件。

        Args:
            directory: 当前目录
            filename: 文件名
            current_depth: 当前深度
            max_depth: 最大深度
            deadline: 可选的 ``time.monotonic()`` 截止时刻（AUD-36817）
                —— 家目录级搜索可能达秒级~分钟级，超时即止损

        Returns:
            找到的文件路径，或 None
        """
        import os.path as osp

        if current_depth >= max_depth:
            return None
        if deadline is not None and time.monotonic() >= deadline:
            return None

        try:
            for entry in os.listdir(directory):
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                entry_path = osp.join(directory, entry)
                if osp.isfile(entry_path) and entry == filename:
                    return entry_path
                elif osp.isdir(entry_path):
                    found = OfflineAssetManager._recursive_search(
                        entry_path, filename, current_depth + 1, max_depth,
                        deadline=deadline,
                    )
                    if found:
                        return found
        except OSError:
            pass

        return None

    @staticmethod
    def auto_search_for_missing(
        project: Union["ToposProject", Any],
        budget_seconds: Optional[float] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict[str, str]:
        """
        自动搜索缺失文件并生成重链接映射。

        AUD-36817: 搜索目录含 ~/Documents、~/Downloads 等家目录大路径，
        逐目录 listdir+isfile 的同步 IO 在素材多、目录大时可达秒级~分钟级。
        新增可选的扫描预算（``budget_seconds``）与取消点（``cancelled``），
        供 UI 层在后台线程调用时限制单次扫描耗时；不传参时行为与旧版一致。
        找到的部分映射照常返回 —— 重链接是逐资产生效的，宁缺毋滥。

        Args:
            project: ToposProject 或 EditProject 实例
            budget_seconds: 整个搜索的时间预算（秒）；None 表示不限制
            cancelled: 可选取消探针，每个资产扫描前检查；True 时提前结束

        Returns:
            {asset_id: new_path} 映射（找到的文件）
        """

        found_map: dict[str, str] = {}

        offline_list = OfflineAssetManager.get_offline_asset_list(project)
        search_dirs = OfflineAssetManager._suggest_search_dirs(
            [a.path for a in offline_list]
        )

        deadline = (
            time.monotonic() + float(budget_seconds)
            if budget_seconds is not None else None
        )

        for asset in offline_list:
            if cancelled is not None and cancelled():
                logger.info("Offline asset auto-search cancelled by caller")
                break
            filename = os.path.basename(asset.path)
            found_path = OfflineAssetManager.find_matching_file(
                filename, search_dirs, recursive=True, max_depth=3,
                deadline=deadline,
            )
            if found_path:
                found_map[asset.asset_id] = found_path

        return found_map

    # ==================== 更新离线状态 ====================

    @staticmethod
    def update_offline_status(
        project: Union["ToposProject", Any],
    ) -> Union["ToposProject", Any]:
        """
        更新工程中所有素材的离线状态。

        Args:
            project: ToposProject 或 EditProject 实例

        Returns:
            新的工程实例（包含更新的离线状态）
        """
        from .topos_project import ToposProject, MediaAssetRef

        if isinstance(project, ToposProject):
            # 为 ToposProject 更新离线状态
            new_assets: dict[str, MediaAssetRef] = {}
            for asset_id, asset in project.assets.items():
                # AUD-36815: virtual (topos://) assets are always online.
                is_offline = (
                    not is_virtual_asset_path(asset.path)
                    and not os.path.exists(asset.path)
                )
                if asset.is_offline != is_offline:
                    new_assets[asset_id] = MediaAssetRef(
                        asset_id=asset.asset_id,
                        path=asset.path,
                        media_type=asset.media_type,
                        display_name=asset.display_name,
                        is_offline=is_offline,
                    )
                else:
                    new_assets[asset_id] = asset

            return ToposProject(
                project_id=project.project_id,
                name=project.name,
                project_mode=project.project_mode,
                assets=new_assets,
                bins=project.bins,
                timelines=project.timelines,
                color_stacks=project.color_stacks,
                compositions=project.compositions,
                deliver_presets=project.deliver_presets,
                ui_state=project.ui_state,
                settings=project.settings,
                primary_asset_id=project.primary_asset_id,
                primary_color_stack_id=project.primary_color_stack_id,
                active_timeline_id=project.active_timeline_id,
                active_composition_id=project.active_composition_id,
                created_at=project.created_at,
                modified_at=time.time(),
                # AUD-36814: carry the optimistic-lock revision across the
                # rebuild; dropping it silently reset the counter to 0.
                revision=project.revision,
            )

        elif edit_project_adapter.is_edit_project(project):
            return edit_project_adapter.update_offline_status(project)

        else:
            raise ValueError(f"Unsupported project type: {type(project)}")


# ================================================================
# 导出
# ================================================================

__all__ = [
    "OfflineAssetReport",
    "RelinkResult",
    "OfflineAssetManager",
]
