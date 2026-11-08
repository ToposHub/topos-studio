"""内建 Edit 工作区预设（P1-24）。

Premiere 式任务导向布局：Editing / Audio / Captions / Review。本模块只做
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

# 与 dock_layout_controller._EDIT_TIMELINE_DOCK_DEFAULT_HEIGHT 保持一致；
# 时间线 dock 在四个预设中都应保持足够高度。
_TIMELINE_HEIGHT = 280


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


# 与出厂 Edit 布局（dock_layout_controller 硬编码分支）同构的默认预设，
# 因此"Editing"预设 = 重置回熟悉的剪辑布局，用户无学习成本。
EDITING_PRESET = WorkspacePreset(
    preset_id="editing",
    label="剪辑",
    label_en="Editing",
    description="默认剪辑布局：媒体池 + Viewport 单/双视图 + 时间线 + 右侧属性区",
    visible_docks=(
        "dockEditMediaPool",
        "dockProgramViewer",
        "dockEditTimeline",
        "dockProperties",
        "dockEditEffects",
        "dockEditInfo",
    ),
    dock_areas=(
        ("dockEditMediaPool", "left"),
        ("dockProgramViewer", "top"),
        ("dockEditTimeline", "bottom"),
        ("dockProperties", "right"),
        ("dockEditEffects", "right"),
        ("dockEditInfo", "right"),
    ),
    tabify_groups=(
        ("dockProperties", "dockEditEffects", "dockEditInfo"),
    ),
    focus_dock="dockProgramViewer",
    h_sizes=(
        ("dockEditMediaPool", 250),
        ("dockProgramViewer", 600),
        ("dockProperties", 280),
    ),
    v_sizes=(("dockEditTimeline", _TIMELINE_HEIGHT),),
)

# 音频混音工作区：Audio Mixer（EditTrack 轨道混音器）取代右侧属性区，
# 时间线保持可用，媒体池收窄；ADR Take Sheet 与混音器同栈（P1-10）。
AUDIO_PRESET = WorkspacePreset(
    preset_id="audio",
    label="音频",
    label_en="Audio",
    description="混音布局：音频混音器 + ADR take sheet + 时间线 + 节目监视器，弱化剪辑面板",
    visible_docks=(
        "dockEditMediaPool",
        "dockProgramViewer",
        "dockEditTimeline",
        "dockAudioMixer",
        "dockAdrTakeSheet",
    ),
    dock_areas=(
        ("dockEditMediaPool", "left"),
        ("dockProgramViewer", "top"),
        ("dockEditTimeline", "bottom"),
        ("dockAudioMixer", "right"),
        ("dockAdrTakeSheet", "right"),
    ),
    tabify_groups=(
        ("dockAudioMixer", "dockAdrTakeSheet"),
    ),
    focus_dock="dockAudioMixer",
    h_sizes=(
        ("dockEditMediaPool", 220),
        ("dockAudioMixer", 420),
    ),
    v_sizes=(("dockEditTimeline", _TIMELINE_HEIGHT),),
)

# 字幕工作区：Caption Workspace 独占右侧（不与属性区 tab 化，字幕编辑
# 需要常驻可见），节目监视器供逐句校对。
CAPTIONS_PRESET = WorkspacePreset(
    preset_id="captions",
    label="字幕",
    label_en="Captions",
    description="字幕布局：字幕工作区常驻右侧 + 时间线 + 节目监视器",
    visible_docks=(
        "dockEditMediaPool",
        "dockProgramViewer",
        "dockEditTimeline",
        "dockCaptionWorkspace",
    ),
    dock_areas=(
        ("dockEditMediaPool", "left"),
        ("dockProgramViewer", "top"),
        ("dockEditTimeline", "bottom"),
        ("dockCaptionWorkspace", "right"),
    ),
    focus_dock="dockCaptionWorkspace",
    h_sizes=(
        ("dockEditMediaPool", 240),
        ("dockCaptionWorkspace", 460),
    ),
    v_sizes=(("dockEditTimeline", _TIMELINE_HEIGHT),),
)

# 审阅工作区：最大化节目监视器，只保留时间线与只读 Info 面板，
# 隐藏所有编辑工具，适合客户/导演看片。
REVIEW_PRESET = WorkspacePreset(
    preset_id="review",
    label="审阅",
    label_en="Review",
    description="审片布局：大节目监视器 + 时间线 + 信息面板，隐藏编辑工具",
    visible_docks=(
        "dockProgramViewer",
        "dockEditTimeline",
        "dockEditInfo",
    ),
    dock_areas=(
        ("dockProgramViewer", "top"),
        ("dockEditTimeline", "bottom"),
        ("dockEditInfo", "right"),
    ),
    focus_dock="dockProgramViewer",
    h_sizes=(
        ("dockProgramViewer", 900),
        ("dockEditInfo", 240),
    ),
    v_sizes=(("dockEditTimeline", 300),),
)


_PRESETS: Tuple[WorkspacePreset, ...] = (
    EDITING_PRESET,
    AUDIO_PRESET,
    CAPTIONS_PRESET,
    REVIEW_PRESET,
)

_PRESETS_BY_ID: Dict[str, WorkspacePreset] = {p.preset_id: p for p in _PRESETS}


def list_workspace_presets() -> Tuple[WorkspacePreset, ...]:
    """返回全部内建工作区预设（展示顺序即菜单顺序）。"""
    return _PRESETS


def get_workspace_preset(preset_id: str) -> Optional[WorkspacePreset]:
    """按 preset_id 查找内建预设；不存在返回 None。"""
    return _PRESETS_BY_ID.get(preset_id)
