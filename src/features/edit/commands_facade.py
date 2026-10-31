"""EditCommandsFacade -- thin re-export layer for edit commands used by controllers.

Instead of each controller method importing individually from
src.features.edit.core.commands (deep import), the controller imports
from this facade at the feature boundary.

This module is a *re-export shim*: it does not add behaviour, it merely
consolidates the surface area so the app layer has fewer deep-import
lines to maintain.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def create_teaching_animation_clip_command(scene: Any, timeline_start: int = 0,
                                           timeline_duration: int | None = None,
                                           track_id: str | None = None,
                                           mode: str = "append") -> Any:
    """Create a Teaching Animation graphics-clip command at the Edit boundary."""
    try:
        from src.features.teaching_animation.host_adapters import MotionGraphicClipCommand
        return MotionGraphicClipCommand(scene, timeline_start, timeline_duration, track_id, mode)
    except ImportError:
        logger.warning("Teaching Animation clip command unavailable")
        return None


def get_command_classes():
    """Return the edit command classes as a namespace dict.

    Returns None (with a warning) if the edit module is unavailable.
    """
    try:
        from src.features.edit.core.commands import (
            CreateColorStackCommand,
            UpdateClipColorStackCommand,
            UpdateColorStackCommand,
            UpdateClipParamCommand,
            ChangeClipSpeedCommand,
            UpdateEffectParamCommand,
            SetEffectEnabledCommand,
            RemoveEffectCommand,
            AddEffectCommand,
            ReorderEffectCommand,
            UpdateTransitionCommand,
            AddKeyframeCommand,
            RemoveKeyframeCommand,
            MacroCommand,
        )
        return {
            "CreateColorStackCommand": CreateColorStackCommand,
            "UpdateClipColorStackCommand": UpdateClipColorStackCommand,
            "UpdateColorStackCommand": UpdateColorStackCommand,
            "UpdateClipParamCommand": UpdateClipParamCommand,
            # N10/T3-02：此前 facade 未导出 ChangeClipSpeedCommand，
            # controller 的统一 retime 分支永远拿不到它、一路回退
            # UpdateClipParamCommand 的第二套时长公式——Properties 与菜
            # 单分叉、linked 伙伴不随动、锁轨整组拒绝也失效。
            "ChangeClipSpeedCommand": ChangeClipSpeedCommand,
            "UpdateEffectParamCommand": UpdateEffectParamCommand,
            "SetEffectEnabledCommand": SetEffectEnabledCommand,
            "RemoveEffectCommand": RemoveEffectCommand,
            "AddEffectCommand": AddEffectCommand,
            "ReorderEffectCommand": ReorderEffectCommand,
            "UpdateTransitionCommand": UpdateTransitionCommand,
            # E02：关键帧命令此前未导出，效果参数 ◆ 点击在控制器里
            # _cmds["AddKeyframeCommand"] 直接 KeyError。
            "AddKeyframeCommand": AddKeyframeCommand,
            "RemoveKeyframeCommand": RemoveKeyframeCommand,
            "MacroCommand": MacroCommand,
        }
    except ImportError:
        logger.warning("Edit commands module unavailable")
        return None


def get_clip_params_classes():
    """Return clip parameter classes.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.core.clip_params import (
            SpeedParams,
            TransformParams,
        )
        return {
            "SpeedParams": SpeedParams,
            "TransformParams": TransformParams,
        }
    except ImportError:
        logger.warning("Edit clip_params module unavailable")
        return None


def get_selection_classes():
    """Return selection classes.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.core.selection import (
            TimelineSelection,
            SelectionType,
        )
        return {
            "TimelineSelection": TimelineSelection,
            "SelectionType": SelectionType,
        }
    except ImportError:
        logger.warning("Edit selection module unavailable")
        return None


def get_effect_instance_class():
    """Return the EffectInstance class.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.core.effect_instance import EffectInstance
        return EffectInstance
    except ImportError:
        logger.warning("Edit effect_instance module unavailable")
        return None


def get_project_io_classes():
    """Return EditProjectIO-related classes and constants.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.core.edit_project_io import (
            EditProjectIO,
            EditProjectIOConfig,
            UIState,
            EDIT_PROJECT_EXTENSION,
        )
        return {
            "EditProjectIO": EditProjectIO,
            "EditProjectIOConfig": EditProjectIOConfig,
            "UIState": UIState,
            "EDIT_PROJECT_EXTENSION": EDIT_PROJECT_EXTENSION,
        }
    except ImportError:
        logger.warning("Edit edit_project_io module unavailable")
        return None


def get_project_classes():
    """Return EditProject and related classes.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.core.project import EditProject, TimelineSequenceRef
        return {
            "EditProject": EditProject,
            "TimelineSequenceRef": TimelineSequenceRef,
        }
    except ImportError:
        logger.warning("Edit project module unavailable")
        return None


def get_timeline_classes():
    """Return timeline-related classes.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
        return {
            "EditTrack": EditTrack,
            "TimelineSequence": TimelineSequence,
        }
    except ImportError:
        logger.warning("Edit timeline_v2 module unavailable")
        return None


def get_timeline_preview_renderer_factory():
    """Return the create_timeline_preview_renderer factory function.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.core.timeline_preview_renderer import create_timeline_preview_renderer
        return create_timeline_preview_renderer
    except ImportError:
        logger.warning("Edit timeline_preview_renderer module unavailable")
        return None


def get_view_mode_enum():
    """Return the ViewMode enum from media_pool_panel.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.ui.media_pool_panel import ViewMode
        return ViewMode
    except ImportError:
        logger.warning("Edit media_pool_panel ViewMode unavailable")
        return None


def get_quick_project_dialog_class():
    """Return the QuickProjectDialog class.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.ui.quick_project_dialog import QuickProjectDialog
        return QuickProjectDialog
    except ImportError:
        logger.warning("Edit quick_project_dialog unavailable")
        return None


def get_edit_ui_classes():
    """Return edit UI widget classes used by MainWindow.create_panels.

    Returns None if the edit module is unavailable.
    """
    try:
        from src.features.edit.ui.edit_page import EditPage
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel
        from src.features.edit.ui.timeline_panel import TimelinePanel
        from src.features.edit.ui.edit_info_panel import EditInfoPanel
        from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
        from src.features.edit.ui.edit_effects_panel import EditEffectsPanel
        from src.features.edit.ui.program_viewer import ProgramViewerWidget
        from src.features.edit.ui.source_viewer import SourceViewerWidget
        from src.features.edit.ui.caption_workspace import CaptionWorkspacePanel
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        from src.features.edit.ui.multicam_panel import MulticamPanel
        return {
            "EditPage": EditPage,
            "MediaPoolPanel": MediaPoolPanel,
            "TimelinePanel": TimelinePanel,
            "EditInfoPanel": EditInfoPanel,
            "EditPropertiesPanel": EditPropertiesPanel,
            "EditEffectsPanel": EditEffectsPanel,
            "ProgramViewerWidget": ProgramViewerWidget,
            "SourceViewerWidget": SourceViewerWidget,
            "CaptionWorkspacePanel": CaptionWorkspacePanel,
            "AdrTakeSheetPanel": AdrTakeSheetPanel,
            "MulticamPanel": MulticamPanel,
        }
    except ImportError:
        logger.warning("Edit UI module unavailable")
        return None
