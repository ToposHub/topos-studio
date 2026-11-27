"""内建 Edit 工作区预设（P1-24）。

本模块保存剪辑页的默认布局规格。它只做
声明式规格（不依赖 Qt），由 DockLayoutMixin.apply_edit_workspace_preset()
解释执行：隐藏受管 dock → 按预设摆放区域 → tab 化 → 显示 → 快照进
page_layouts["Edit"] 与 LayoutStore，切页/重启后布局不丢。

dock 以 objectName 引用（与 create_custom_dock 注册名一致），缺失的 dock
（如图像模式没有 Media Pool）由应用层静默跳过。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

__all__ = [
    "EDIT_WORKSPACE_MANAGED_DOCKS",
    "WorkspacePreset",
    "list_workspace_presets",
    "get_workspace_preset",
]


# Edit 工作区预设可以接管可见性的 dock（objectName）。
# 应用预设时会先把这批 dock 全部隐藏，再按预设显示；不在名单里的 dock
# （如 Color 页面板）不受影响。dock_ai_edit_assistant 的 objectName
# 由 AiEditAssistantPanel 自身设置（历史拼写与其它 dock 不同）。
EDIT_WORKSPACE_MANAGED_DOCKS: Tuple[str, ...] = (
    "dockEditMediaPool",
    "dockProgramViewer",
    "dockEditTimeline",
    "dockEditInfo",
    "dockEditEffects",
    "dockProperties",
    "dockCaptionWorkspace",
    "dock_ai_edit_assistant",
    "dockAudioMixer",
    "dockAdrTakeSheet",
    "dockMulticam",
)


@dataclass(frozen=True)
class WorkspacePreset:
    """单个工作区预设的声明式布局规格。

    Attributes:
        preset_id: 稳定标识（菜单/配置引用，不随语言变化）。
        label: 中文显示名（I18N 默认文案）。
        label_en: 英文显示名。
        description: 菜单 tooltip 说明。
        visible_docks: 要显示的 dock objectName。
        dock_areas: (objectName, area) 序列，area ∈ left/right/top/bottom。
        tabify_groups: 每组内 tab 化停靠，首成员为组内 raise 目标。
        focus_dock: 应用结束后 raise_() 的 dock。
        h_sizes: (objectName, width) 水平 resizeDocks 提示。
        v_sizes: (objectName, height) 垂直 resizeDocks 提示。
    """

    preset_id: str
    label: str
    label_en: str
    description: str
    visible_docks: Tuple[str, ...]
    dock_areas: Tuple[Tuple[str, str], ...]
    tabify_groups: Tuple[Tuple[str, ...], ...] = ()
    focus_dock: str = ""
    h_sizes: Tuple[Tuple[str, int], ...] = ()
    v_sizes: Tuple[Tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        visible = set(self.visible_docks)
        managed = set(EDIT_WORKSPACE_MANAGED_DOCKS)
        unknown = visible - managed
        if unknown:
            raise ValueError(
                f"Workspace preset {self.preset_id!r} references "
                f"unmanaged docks: {sorted(unknown)}"
            )
        if len(set(self.visible_docks)) != len(self.visible_docks):
            raise ValueError(
                f"Workspace preset {self.preset_id!r} has duplicate visible_docks"
            )
        area_names = {name for name, _area in self.dock_areas}
        missing_area = visible - area_names
        if missing_area:
            raise ValueError(
                f"Workspace preset {self.preset_id!r} visible docks without "
                f"dock_areas entry: {sorted(missing_area)}"
            )
        for name, area in self.dock_areas:
            if area not in ("left", "right", "top", "bottom"):
                raise ValueError(
                    f"Workspace preset {self.preset_id!r} invalid area "
                    f"{area!r} for {name!r}"
                )
        for group in self.tabify_groups:
            if len(group) < 2:
                raise ValueError(
                    f"Workspace preset {self.preset_id!r} tabify group "
                    f"needs >=2 members: {group}"
                )
            outside = set(group) - visible
            if outside:
                raise ValueError(
                    f"Workspace preset {self.preset_id!r} tabify group "
                    f"references non-visible docks: {sorted(outside)}"
                )
        if self.focus_dock and self.focus_dock not in visible:
            raise ValueError(
                f"Workspace preset {self.preset_id!r} focus_dock "
                f"{self.focus_dock!r} is not visible"
            )


# The default uses the screen-specific sizing in the dock controller.
EDITING_PRESET = WorkspacePreset(
    preset_id="editing",
    label="默认布局",
    label_en="Default Layout",
    description="媒体池、视图窗口、属性与全宽时间线",
    visible_docks=(
        "dockEditMediaPool", "dockEditEffects", "dockAudioMixer",
        "dockProgramViewer",
        "dockProperties", "dock_ai_edit_assistant", "dockEditInfo",
        "dockEditTimeline",
    ),
    dock_areas=(
        ("dockEditMediaPool", "top"),
        ("dockProgramViewer", "top"),
        ("dockProperties", "top"),
        ("dockEditTimeline", "bottom"),
        ("dockEditEffects", "top"),
        ("dockAudioMixer", "top"),
        ("dock_ai_edit_assistant", "top"),
        ("dockEditInfo", "top"),
    ),
    tabify_groups=(
        ("dockEditMediaPool", "dockEditEffects", "dockAudioMixer"),
        ("dockProperties", "dock_ai_edit_assistant", "dockEditInfo"),
    ),
    focus_dock="dockProgramViewer",
)

_PRESETS = (EDITING_PRESET,)
_PRESETS_BY_ID = {preset.preset_id: preset for preset in _PRESETS}


def list_workspace_presets() -> Tuple[WorkspacePreset, ...]:
    """返回全部内建工作区预设（展示顺序即菜单顺序）。"""
    return _PRESETS


def get_workspace_preset(preset_id: str) -> Optional[WorkspacePreset]:
    """按 preset_id 查找内建预设；不存在返回 None。"""
    return _PRESETS_BY_ID.get(preset_id)
