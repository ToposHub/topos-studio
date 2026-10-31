"""
Edit Module
剪辑系统核心模块。

## 职责

（AUD-15200：以下清单与 ``__all__`` 实际导出对齐，新增导出模块时同步更新。）

- project: EditProject, MediaAsset, ProjectBin, TimelineSequenceRef (P2)
- serialization: JSON 序列化/反序列化 (P2)
- edit_project_io: 工程文件读写 (P10)
- clip_params: TransformParams, CropParams, SpeedParams (P3)
- timeline_v2: TimelineSequence, EditTrack, ClipInstance, TransitionInstance (P3)
- timeline_render_plan: 时间线渲染计划与纯函数求值 (P3)
- commands: 编辑命令和 Undo (P4)
- selection: 选择模型 (P4)
- audio_waveform: 音频波形提取和缓存 (P8)

## 设计原则

- 所有数据类使用 @dataclass(frozen=True) 确保不可变
- 不依赖 PyQt，纯 Python 数据模型（audio_waveform 除外，需要 QThread）
- 完整类型注解
"""

from __future__ import annotations

# P2: Project management
from .project import MediaAsset, ProjectBin, EditProject, TimelineSequenceRef
from .serialization import EditProjectSerializer, UIState

# P10: Project IO management
from .edit_project_io import (
    EditProjectIO,
    EditProjectIOConfig,
    EDIT_PROJECT_EXTENSION,
)

# P3: Timeline data models
from .clip_params import TransformParams, CropParams, SpeedParams, SpeedPoint
from .timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    TransitionInstance,
    TimelineMarker,
    Keyframe,
)

# P3: Timeline render plan
from .timeline_render_plan import (
    TimelineRenderPlan,
    FrameRenderPlan,
    ClipLayerInfo,
    TransitionBlendInfo,
    calculate_source_frame,
    get_effective_speed,
    is_frame_in_clip,
    get_transition_at_frame,
    calculate_transition_blend,
    is_track_renderable,
)

# P4: Commands and Selection
from .commands import (
    EditCommand,
    AddClipCommand,
    DeleteClipCommand,
    MoveClipCommand,
    TrimClipCommand,
    RetimeMotionGraphicClipCommand,
    SplitClipCommand,
    # Transition commands
    AddTransitionCommand,
    RemoveTransitionCommand,
    UpdateTransitionCommand,
    CommandStack,
)
from .selection import (
    TimelineSelection,
    SelectionType,
    SelectionItem,
)

# P8: Audio waveform extraction
from .audio_waveform import (
    WaveformData,
    WaveformCache,
    WaveformExtractor,
    WaveformExtractorWorker,
    request_waveform_async,
    get_cached_waveform,
    is_waveform_pending,
    generate_placeholder_waveform,
    build_range_cache_key,
    get_ranged_waveform,
    cache_ranged_waveform,
)

__all__ = [
    # project (P2)
    "MediaAsset",
    "ProjectBin",
    "EditProject",
    "TimelineSequenceRef",
    # serialization (P2)
    "EditProjectSerializer",
    "UIState",
    # edit_project_io (P10)
    "EditProjectIO",
    "EditProjectIOConfig",
    "EDIT_PROJECT_EXTENSION",
    # clip_params (P3)
    "TransformParams",
    "CropParams",
    "SpeedParams",
    "SpeedPoint",
    # timeline_v2 (P3)
    "TimelineSequence",
    "EditTrack",
    "ClipInstance",
    "TransitionInstance",
    "TimelineMarker",
    "Keyframe",
    # timeline_render_plan (P3)
    "TimelineRenderPlan",
    "FrameRenderPlan",
    "ClipLayerInfo",
    "TransitionBlendInfo",
    "calculate_source_frame",
    "get_effective_speed",
    "is_frame_in_clip",
    "get_transition_at_frame",
    "calculate_transition_blend",
    "is_track_renderable",
    # commands (P4)
    "EditCommand",
    "AddClipCommand",
    "DeleteClipCommand",
    "MoveClipCommand",
    "TrimClipCommand",
    "RetimeMotionGraphicClipCommand",
    "SplitClipCommand",
    # Transition commands
    "AddTransitionCommand",
    "RemoveTransitionCommand",
    "UpdateTransitionCommand",
    "CommandStack",
    # selection (P4)
    "TimelineSelection",
    "SelectionType",
    "SelectionItem",
    # audio_waveform (P8)
    "WaveformData",
    "WaveformCache",
    "WaveformExtractor",
    "WaveformExtractorWorker",
    "request_waveform_async",
    "get_cached_waveform",
    "is_waveform_pending",
    "generate_placeholder_waveform",
    "build_range_cache_key",
    "get_ranged_waveform",
    "cache_ranged_waveform",
]
