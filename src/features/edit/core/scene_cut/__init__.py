"""
Scene Cut Detection 模块。

实现非 AI 的场景剪切检测功能，基于帧差、直方图、边缘、亮度和时间序列规则。

## 模块职责

- models: 数据模型定义
- frame_sampler: 视频帧采样器
- metrics: 帧差异指标计算
- filters: 噪声抑制和误判过滤
- detector: 检测器主逻辑
- cache: 分析结果和缩略图缓存
- timeline_mapping: 源帧到时间线帧映射

## 设计原则

- 纯 Python，不依赖 UI
- 完整类型注解
- 支持 cancel token 和 progress callback
- 使用 PyAV/OpenCV/Numpy
- 所有数据类使用 frozen=True 确保不可变
- JSON 序列化（不使用 pickle）
- VFR 映射误差不超过 1 帧

## 参考

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md
"""

from __future__ import annotations

# Models
from .models import (
    SceneCutSettings,
    FrameTimePoint,
    SceneCutMetrics,
    SceneCutKind,
    SceneCutDecision,
    SceneCutCandidate,
    SceneCutAnalysis,
    SceneSubclipSpec,
    SourceFrameTimeIndex,
    DETECTOR_VERSION,
    generate_analysis_id,
    generate_scene_subclips,
    timecode_to_frames,
)

# Frame Sampler
from .frame_sampler import (
    CancelToken,
    CancelledError,
    SampledFrame,
    VideoFrameSampler,
    create_sampler,
    sample_frames_for_detection,
    build_time_index_for_source,
)

# Metrics
from .metrics import (
    FrameMetricsCalculator,
    AdaptiveThresholdCalculator,
    calculate_metrics_for_frame_pair,
    calculate_metrics_sequence,
    compute_adaptive_thresholds,
    extract_frame_features,
    compare_frame_features,
    WEIGHT_LUMA_DELTA,
    WEIGHT_HIST_DELTA,
    WEIGHT_EDGE_DELTA,
    WEIGHT_COLOR_DELTA,
)

# Filters
from .filters import (
    SceneCutFilterPipeline,
    FrameMetricsEntry,
    RollingStats,
    DetectionState,
    adaptive_threshold,
    min_scene_distance,
    flash_guard,
    transition_guard,
    black_gap_detector,
    fast_motion_suppressor,
)

# Detector
from .detector import (
    SceneCutDetector,
    DetectorProgressCallback,
    DetectionPhase,
    DetectionProgress,
    get_accepted_candidates,
    get_pending_candidates,
    get_hard_cut_candidates,
    get_warning_candidates,
    get_high_confidence_candidates,
)

# Worker
from .worker import (
    SceneCutWorker,
    SceneCutWorkerManager,
    SceneCutWorkerSignals,
    SceneCutError,
    SceneCutErrorKind,
    classify_error,
    ProgressThrottler,
    DetectionTaskKey,
    ActiveTasksRegistry,
    get_active_tasks_registry,
)

# Cache
from .cache import (
    AnalysisCache,
    ThumbnailCache,
    SceneCutCache,
    MediaFingerprint,
    CacheKeyComponents,
    generate_cache_key,
    get_scene_cut_cache,
    check_cache_validity,
)

# Timeline Mapping
from .timeline_mapping import (
    ClipMappingConfig,
    ClipSourceMapper,
    SourceToTimelineResult,
    TimelineToSourceResult,
    create_mapper,
    create_mapper_from_clip,
    fps_to_fraction,
    estimate_fps_conversion_error,
    is_vfr_content,
    validate_mapping_tolerance,
)

# Apply Operations
from .apply_ops import (
    ApplyMarkersResult,
    ApplySplitsResult,
    ApplyAllResult,
    ConflictReport,
    apply_markers,
    apply_splits,
    apply_all,
    detect_conflicts,
    get_accepted_count,
    get_hard_cut_count,
    validate_apply_prerequisites,
    estimate_result_clips,
    MARKER_COLOR_HARD_CUT,
    MARKER_COLOR_SOFT_TRANSITION,
    MARKER_COLOR_BLACK_GAP,
    MARKER_COLOR_WARNING,
    MARKER_NAME_PREFIX,
)

# Subclips (P6 - Add Cuts to Media Pool)
from .subclips import (
    DUPLICATE_REPLACE,
    DUPLICATE_APPEND,
    DUPLICATE_CANCEL,
    SUBCLIP_IS_SUBCLIP,
    SUBCLIP_PARENT_ASSET_ID,
    SUBCLIP_PARENT_PATH,
    SUBCLIP_SOURCE_START,
    SUBCLIP_SOURCE_DURATION,
    SUBCLIP_SOURCE_END,
    SUBCLIP_START_SECONDS,
    SUBCLIP_DURATION_SECONDS,
    SUBCLIP_END_SECONDS,
    SUBCLIP_SCENE_INDEX,
    SUBCLIP_ANALYSIS_ID,
    SUBCLIP_CONFIDENCE,
    SUBCLIP_KIND,
    SUBCLIP_START_TIMECODE,
    SUBCLIP_END_TIMECODE,
    SUBCLIP_THUMB_FRAME,
    ExistingSubclipReport,
    AddSubclipsResult,
    create_subclip_asset,
    find_parent_bin,
    check_existing_subclips,
    remove_existing_subclips,
    add_subclips_to_bin,
    add_subclips_to_project,
    is_subclip,
    get_parent_asset_id,
    get_subclip_source_range,
    get_subclips_for_analysis,
    remove_subclips_for_analysis,
    get_scene_index,
    validate_subclip_for_timeline,
)

__all__ = [
    # Models
    "SceneCutSettings",
    "FrameTimePoint",
    "SceneCutMetrics",
    "SceneCutKind",
    "SceneCutDecision",
    "SceneCutCandidate",
    "SceneCutAnalysis",
    "SceneSubclipSpec",
    "SourceFrameTimeIndex",
    "DETECTOR_VERSION",
    "generate_analysis_id",
    "generate_scene_subclips",
    "timecode_to_frames",
    # Frame Sampler
    "CancelToken",
    "CancelledError",
    "SampledFrame",
    "VideoFrameSampler",
    "create_sampler",
    "sample_frames_for_detection",
    "build_time_index_for_source",
    # Metrics
    "FrameMetricsCalculator",
    "AdaptiveThresholdCalculator",
    "calculate_metrics_for_frame_pair",
    "calculate_metrics_sequence",
    "compute_adaptive_thresholds",
    "extract_frame_features",
    "compare_frame_features",
    "WEIGHT_LUMA_DELTA",
    "WEIGHT_HIST_DELTA",
    "WEIGHT_EDGE_DELTA",
    "WEIGHT_COLOR_DELTA",
    # Filters
    "SceneCutFilterPipeline",
    "FrameMetricsEntry",
    "RollingStats",
    "DetectionState",
    "adaptive_threshold",
    "min_scene_distance",
    "flash_guard",
    "transition_guard",
    "black_gap_detector",
    "fast_motion_suppressor",
    # Detector
    "SceneCutDetector",
    "DetectorProgressCallback",
    "DetectionPhase",
    "DetectionProgress",
    "get_accepted_candidates",
    "get_pending_candidates",
    "get_hard_cut_candidates",
    "get_warning_candidates",
    "get_high_confidence_candidates",
    # Worker
    "SceneCutWorker",
    "SceneCutWorkerManager",
    "SceneCutWorkerSignals",
    "SceneCutError",
    "SceneCutErrorKind",
    "classify_error",
    "ProgressThrottler",
    "DetectionTaskKey",
    "ActiveTasksRegistry",
    "get_active_tasks_registry",
    # Cache
    "AnalysisCache",
    "ThumbnailCache",
    "SceneCutCache",
    "MediaFingerprint",
    "CacheKeyComponents",
    "generate_cache_key",
    "get_scene_cut_cache",
    "check_cache_validity",
    # Timeline Mapping
    "ClipMappingConfig",
    "ClipSourceMapper",
    "SourceToTimelineResult",
    "TimelineToSourceResult",
    "create_mapper",
    "create_mapper_from_clip",
    "fps_to_fraction",
    "estimate_fps_conversion_error",
    "is_vfr_content",
    "validate_mapping_tolerance",
    # Apply Operations
    "ApplyMarkersResult",
    "ApplySplitsResult",
    "ApplyAllResult",
    "ConflictReport",
    "apply_markers",
    "apply_splits",
    "apply_all",
    "detect_conflicts",
    "get_accepted_count",
    "get_hard_cut_count",
    "validate_apply_prerequisites",
    "estimate_result_clips",
    "MARKER_COLOR_HARD_CUT",
    "MARKER_COLOR_SOFT_TRANSITION",
    "MARKER_COLOR_BLACK_GAP",
    "MARKER_COLOR_WARNING",
    "MARKER_NAME_PREFIX",
    # Subclips (P6)
    "DUPLICATE_REPLACE",
    "DUPLICATE_APPEND",
    "DUPLICATE_CANCEL",
    "SUBCLIP_IS_SUBCLIP",
    "SUBCLIP_PARENT_ASSET_ID",
    "SUBCLIP_PARENT_PATH",
    "SUBCLIP_SOURCE_START",
    "SUBCLIP_SOURCE_DURATION",
    "SUBCLIP_SOURCE_END",
    "SUBCLIP_START_SECONDS",
    "SUBCLIP_DURATION_SECONDS",
    "SUBCLIP_END_SECONDS",
    "SUBCLIP_SCENE_INDEX",
    "SUBCLIP_ANALYSIS_ID",
    "SUBCLIP_CONFIDENCE",
    "SUBCLIP_KIND",
    "SUBCLIP_START_TIMECODE",
    "SUBCLIP_END_TIMECODE",
    "SUBCLIP_THUMB_FRAME",
    "ExistingSubclipReport",
    "AddSubclipsResult",
    "create_subclip_asset",
    "find_parent_bin",
    "check_existing_subclips",
    "remove_existing_subclips",
    "add_subclips_to_bin",
    "add_subclips_to_project",
    "is_subclip",
    "get_parent_asset_id",
    "get_subclip_source_range",
    "get_subclips_for_analysis",
    "remove_subclips_for_analysis",
    "get_scene_index",
    "validate_subclip_for_timeline",
]