"""
Video Pipeline Decision Module (V-P1-1)
视频管线决策模块，提供统一的管线模式决策和 fallback 控制。

## 职责

- VideoPipelineMode: 用户请求的管线模式枚举 (auto/v3/v2)
- VideoPipelineEffectiveMode: 实际生效的管线模式枚举 (v3/v2)
- VideoPipelineDecision: 决策结果数据类
- resolve_video_pipeline_mode(): 决策函数
- fallback_to_v2_or_fail(): 统一的 fallback 处理函数 (V-P0-3)

## 设计原则

1. 单一决策入口: 所有管线选择通过 resolve_video_pipeline_mode()
2. 统一 fallback: 所有 v3→v2 fallback 走 fallback_to_v2_or_fail()
3. 强制模式保护: forced v3 不静默 fallback，直接报错
4. 可追溯: 每次 fallback 记录到 FallbackLogger

## 使用示例

```python
from src.shared.video.pipeline_decision import (
    VideoPipelineMode,
    VideoPipelineDecision,
    resolve_video_pipeline_mode,
    fallback_to_v2_or_fail,
    should_allow_fallback_for_reason,
)

# 决策管线模式
decision = resolve_video_pipeline_mode(config_manager)
if decision.is_v3_active():
    # 使用 v3 管线
    ...
else:
    # 使用 v2 管线
    ...

# 统一 fallback 处理 (V-P0-3)
result = fallback_to_v2_or_fail(
    reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
    path='/path/to/video.mp4',
    notes='PlaybackCoordinator 初始化失败',
    engine=self,  # EnhancedVideoPlaybackEngine 实例
    config_manager=config_manager,
)
if result.should_fallback:
    # 执行 fallback (调用 super().load_video() 等)
    ...
else:
    # forced v3，不 fallback，报错或保持 v3 状态
    ...
```

## V-P0-3: Fallback 统一控制

v2 GStreamer 链路已移除（解码吞吐与时钟节拍均劣于 v3，4K 无法维持 25fps）。
fallback_to_v2_or_fail() 保留为统一入口，但 should_fallback 恒为 False：
致命错误直接报错并保持 v3/placeholder，不再回退。

| 配置 | 情况 | 行为 |
|------|------|------|
| mode=v3 | fatal error | 报错，不 fallback |
| mode=v3 | 首帧超时 | placeholder，不 fallback |
| mode=auto | fatal error | 报错并保持 v3（v2 已移除） |
| mode=auto | 首帧超时 | placeholder，不 fallback |
| mode=v2 | - | 解析为 v3（v2 已移除，记录告警） |

fatal error 定义（允许 fallback）：
- CODEC_UNSUPPORTED_BY_V3
- BACKEND_UNAVAILABLE
- RENDERER_UNAVAILABLE
- SOURCE_OPEN_FAILED (文件损坏)

非 fatal error（不允许 fallback，只 placeholder）：
- FIRST_FRAME_TIMEOUT
- FIRST_FRAME_UNAVAILABLE (ProRes 高位深)
- FRAME_CONVERSION_FAILED (应降级 frame path)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Any, TYPE_CHECKING

from .fallback_types import (
    VideoPipelineFallbackReason,
    FallbackEvent,
    log_fallback,
)

if TYPE_CHECKING:
    pass  # 避免循环导入

logger = logging.getLogger(__name__)


# ================================================================
# 管线模式枚举
# ================================================================

class VideoPipelineMode(Enum):
    """
    视频管线模式枚举（用户请求的模式）。

    V-P0-1: 收敛为单一枚举，替代旧的 video_pipeline_version + v3_enabled。

    Values:
        AUTO: 优先 v3（v2 已移除，fatal 错误不再回退）
        V3: 强制 v3，不允许 fallback
        V2: 兼容值 — v2 GStreamer 链路已移除，resolve 时映射为 v3
    """
    AUTO = "auto"
    V3 = "v3"
    V2 = "v2"


class VideoPipelineEffectiveMode(Enum):
    """
    实际生效的管线模式。

    Values:
        V3: v3 实时引擎 (PlaybackCoordinator)
        V2: v2 兼容路径 (VideoPlaybackEngine 基类)
    """
    V3 = "v3"
    V2 = "v2"


# ================================================================
# 决策结果数据类
# ================================================================

@dataclass
class VideoPipelineDecision:
    """
    视频管线决策结果。

    resolve_video_pipeline_mode() 返回此对象，包含完整的决策信息，
    用于 UI 显示、日志记录和渲染路径选择。

    Attributes:
        requested_mode: 用户请求的模式（auto/v3/v2）
        effective_mode: 实际生效的模式（v3/v2）
        allow_fallback: v3 失败时是否允许自动回退到 v2
        reason: 决策原因（user_forced_v2, user_forced_v3, default_auto, fallback, debug）
        fallback_reason: 如果发生了 fallback，使用 VideoPipelineFallbackReason
        message: 用于 UI/日志显示的描述性消息
    """
    requested_mode: VideoPipelineMode
    effective_mode: VideoPipelineEffectiveMode
    allow_fallback: bool
    reason: str
    fallback_reason: VideoPipelineFallbackReason
    message: str

    def __str__(self) -> str:
        """返回用于日志/UI 的简洁描述。"""
        if self.fallback_reason != VideoPipelineFallbackReason.NONE:
            return (
                f"Video pipeline: {self.effective_mode.value} "
                f"(fallback from {self.requested_mode.value}, "
                f"reason={self.fallback_reason.name})"
            )
        return (
            f"Video pipeline: {self.effective_mode.value} "
            f"(requested={self.requested_mode.value}, reason={self.reason})"
        )

    def is_v3_active(self) -> bool:
        """检查实际是否使用 v3 管线。"""
        return self.effective_mode == VideoPipelineEffectiveMode.V3

    def to_log_dict(self) -> dict:
        """转换为用于日志记录的字典。"""
        return {
            "requested_mode": self.requested_mode.value,
            "effective_mode": self.effective_mode.value,
            "allow_fallback": self.allow_fallback,
            "reason": self.reason,
            "fallback_reason": self.fallback_reason.name,
            "message": self.message,
        }


# ================================================================
# Fallback 结果数据类
# ================================================================

@dataclass
class FallbackDecision:
    """
    V-P0-3: Fallback 决策结果。

    fallback_to_v2_or_fail() 返回此对象，让调用者知道如何处理。

    Attributes:
        should_fallback: 是否应该执行 fallback (调用 super().load_video() 等)
        reason: Fallback 原因
        is_fatal: 是否是致命错误 (codec/backend 不支持)
        message: 日志/UI 消息
        user_forced_v3: 用户是否强制 v3 (不允许 fallback)
        allow_fallback: 配置是否允许 fallback
        event: 记录的 FallbackEvent (如果有)
    """
    should_fallback: bool
    reason: VideoPipelineFallbackReason
    is_fatal: bool
    message: str
    user_forced_v3: bool = False
    allow_fallback: bool = True
    event: Optional[FallbackEvent] = None

    def __str__(self) -> str:
        if self.should_fallback:
            return f"FallbackDecision: fallback to v2 (reason={self.reason.name})"
        return f"FallbackDecision: stay v3 (reason={self.reason.name}, user_forced_v3={self.user_forced_v3})"


# ================================================================
# Fatal Reason 分类
# ================================================================

# 致命错误：允许 fallback (codec/backend 明确不支持)
FATAL_FALLBACK_REASONS: set[VideoPipelineFallbackReason] = {
    VideoPipelineFallbackReason.CODEC_UNSUPPORTED_BY_V3,
    VideoPipelineFallbackReason.BACKEND_SWITCH_FAILED,
    VideoPipelineFallbackReason.RENDERER_UNAVAILABLE,
    VideoPipelineFallbackReason.SOURCE_OPEN_FAILED,
    VideoPipelineFallbackReason.V3_INIT_FAILED,
    VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED,
    VideoPipelineFallbackReason.PLAYBACK_START_FAILED,
    VideoPipelineFallbackReason.SESSION_INVALID,
    VideoPipelineFallbackReason.HARDWARE_ACCEL_FAILED,
}

# 非致命错误：不允许 fallback，只显示 placeholder
NON_FATAL_REASONS: set[VideoPipelineFallbackReason] = {
    VideoPipelineFallbackReason.NONE,
    VideoPipelineFallbackReason.FIRST_FRAME_TIMEOUT,
    VideoPipelineFallbackReason.FIRST_FRAME_UNAVAILABLE,
    VideoPipelineFallbackReason.FRAME_CONVERSION_FAILED,
    VideoPipelineFallbackReason.PROXY_GENERATION_FAILED,
    VideoPipelineFallbackReason.DEGRADATION_LEVEL_EXCEEDED,
    VideoPipelineFallbackReason.QUEUE_STARVATION,
}


def is_fatal_fallback_reason(reason: VideoPipelineFallbackReason) -> bool:
    """
    判断是否是致命的 fallback 原因。

    V-P0-5: 致命错误才能 fallback v2，非致命只 placeholder。

    Args:
        reason: Fallback 原因枚举

    Returns:
        True 表示致命错误，允许 fallback
    """
    return reason in FATAL_FALLBACK_REASONS


def should_allow_fallback_for_reason(
    reason: VideoPipelineFallbackReason,
    decision: VideoPipelineDecision,
) -> bool:
    """
    V-P0-5: 判断给定原因是否允许 fallback。

    结合 reason 类型 和 decision 配置判断。

    Args:
        reason: Fallback 原因
        decision: 当前的管线决策

    Returns:
        True 表示允许 fallback v2
    """
    # 用户强制 v3，不允许任何 fallback
    if decision.requested_mode == VideoPipelineMode.V3:
        return False

    # 用户选择 v2，直接使用 v2（不是 fallback）
    if decision.requested_mode == VideoPipelineMode.V2:
        return False  # V2 模式不需要 fallback 概念

    # AUTO 模式：只有致命错误才 fallback
    if decision.requested_mode == VideoPipelineMode.AUTO:
        if not decision.allow_fallback:
            return False
        return is_fatal_fallback_reason(reason)

    return False


# ================================================================
# 决策函数
# ================================================================

def resolve_video_pipeline_mode(
    config_manager: Any,
    force_reason: Optional[VideoPipelineFallbackReason] = None
) -> VideoPipelineDecision:
    """
    解析视频管线模式决策。

    根据用户配置和运行时状态，决定实际使用的视频管线。
    这是 V-P0-1 要求的单一决策入口函数。

    Args:
        config_manager: ConfigManager 实例，用于读取配置
        force_reason: 如果外部检测到需要 fallback，传入具体原因

    Returns:
        VideoPipelineDecision 对象，包含完整的决策信息

    决策逻辑：
    1. mode=v2 -> 映射为 v3（v2 GStreamer 链路已移除），记录告警
    2. mode=v3 -> 使用 v3，allow_fallback=False（强制 v3）
    3. mode=auto -> 使用 v3，allow_fallback=True（推荐模式）
    4. force_reason 只影响记录，不再触发 v2 fallback
    """
    # 读取用户配置（使用新的单一配置项）
    mode_str = config_manager.get("video_pipeline_mode") or "auto"
    # 正确处理 False 值（不能使用 or True，因为 False or True = True）
    allow_fallback = config_manager.get("video_pipeline_allow_v2_fallback")
    if allow_fallback is None:
        allow_fallback = True  # 默认允许 fallback

    # 解析请求模式
    try:
        requested_mode = VideoPipelineMode(mode_str)
    except ValueError:
        logger.warning(
            f"Invalid video_pipeline_mode '{mode_str}', falling back to 'auto'"
        )
        requested_mode = VideoPipelineMode.AUTO

    # 根据请求模式确定初始决策
    if requested_mode == VideoPipelineMode.V2:
        # v2 GStreamer 链路已移除：显式选择 v2 时回落 v3 并告警
        logger.warning(
            "Video pipeline v2 (GStreamer) has been removed; "
            "resolving requested v2 to v3"
        )
        return VideoPipelineDecision(
            requested_mode=requested_mode,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=False,
            reason="v2_removed_resolved_to_v3",
            fallback_reason=VideoPipelineFallbackReason.USER_FORCED_V2,
            message="v2 GStreamer pipeline removed; using v3 realtime engine",
        )

    elif requested_mode == VideoPipelineMode.V3:
        # 用户强制 v3
        if force_reason and force_reason != VideoPipelineFallbackReason.NONE:
            # 有外部强制 fallback 原因，但用户不允许 fallback
            logger.error(
                f"v3 forced fallback requested ({force_reason.name}) "
                f"but user disabled fallback"
            )
            # 仍然使用 v3，但记录警告
            return VideoPipelineDecision(
                requested_mode=requested_mode,
                effective_mode=VideoPipelineEffectiveMode.V3,
                allow_fallback=False,
                reason="user_forced_v3",
                fallback_reason=VideoPipelineFallbackReason.NONE,
                message=(
                    f"Using v3 realtime engine (user forced, fallback disabled) "
                    f"- WARNING: {force_reason.name} detected"
                ),
            )
        return VideoPipelineDecision(
            requested_mode=requested_mode,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=False,
            reason="user_forced_v3",
            fallback_reason=VideoPipelineFallbackReason.NONE,
            message="Using v3 realtime engine (user forced, no fallback)",
        )

    else:  # AUTO
        # 推荐模式：优先 v3（v2 已移除，fatal 原因只记录不再回退）
        if force_reason and force_reason != VideoPipelineFallbackReason.NONE and allow_fallback:
            # 发生 fallback (但需要检查是否是致命错误)
            if is_fatal_fallback_reason(force_reason):
                logger.info(f"v3 fatal issue recorded (v2 removed): {force_reason.name}")
                return VideoPipelineDecision(
                    requested_mode=requested_mode,
                    effective_mode=VideoPipelineEffectiveMode.V3,
                    allow_fallback=True,
                    reason="fallback",
                    fallback_reason=force_reason,
                    message=f"Fatal issue kept on v3 (v2 removed, reason: {force_reason.name})",
                )
            else:
                # 非致命错误，保持 v3，不 fallback
                logger.info(
                    f"v3 non-fatal issue detected ({force_reason.name}), "
                    f"staying v3 with placeholder"
                )
                return VideoPipelineDecision(
                    requested_mode=requested_mode,
                    effective_mode=VideoPipelineEffectiveMode.V3,
                    allow_fallback=True,
                    reason="non_fatal_issue",
                    fallback_reason=force_reason,
                    message=(
                        f"Staying v3 with placeholder "
                        f"(reason: {force_reason.name}, non-fatal)"
                    ),
                )
        # 默认使用 v3
        return VideoPipelineDecision(
            requested_mode=requested_mode,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=True,
            reason="default_auto",
            fallback_reason=VideoPipelineFallbackReason.NONE,
            message="Using v3 realtime engine (auto mode, fallback enabled)",
        )


def fallback_to_v2_or_fail(
    reason: VideoPipelineFallbackReason,
    path: Optional[str] = None,
    notes: Optional[str] = None,
    decision: Optional[VideoPipelineDecision] = None,
    codec: Optional[str] = None,
    pixel_format: Optional[str] = None,
    backend: Optional[str] = None,
    exception: Optional[Exception] = None,
) -> FallbackDecision:
    """
    V-P0-3: 统一的 fallback 处理函数。

    所有 v3→v2 fallback 必须通过此函数，确保：
    1. forced v3 不静默 fallback
    2. 非 fatal reason 只 placeholder，不 fallback
    3. 每次 fallback 记录到 FallbackLogger

    Args:
        reason: Fallback 原因
        path: 视频文件路径
        notes: 附注信息
        decision: 当前的管线决策（如果有）
        codec: 编解码器名称
        pixel_format: 像素格式
        backend: 后端名称
        exception: 异常对象

    Returns:
        FallbackDecision 对象，告诉调用者：
        - should_fallback: 是否应该执行 fallback
        - is_fatal: 是否是致命错误
        - message: 日志消息

    Usage:
        ```python
        # 在 EnhancedVideoPlaybackEngine 中
        result = fallback_to_v2_or_fail(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            path=self.current_path,
            notes="PlaybackCoordinator 初始化失败",
            decision=self._pipeline_decision,
        )
        if result.should_fallback:
            # 执行 fallback
            return super().load_video(path)
        else:
            # forced v3，报错或保持状态
            logger.error(result.message)
            return False
        ```
    """
    # 判断是否是致命错误
    is_fatal = is_fatal_fallback_reason(reason)

    # 检查配置是否允许 fallback
    user_forced_v3 = False
    allow_fallback = True

    if decision is not None:
        user_forced_v3 = decision.requested_mode == VideoPipelineMode.V3
        allow_fallback = decision.allow_fallback

    # v2 GStreamer 链路已移除：任何情况都不再回退 v2。
    # 保留本函数与调用结构，统一记录事件并告知调用者留在 v3。
    should_fallback = False

    # 构建消息
    if user_forced_v3:
        message = (
            f"v3 forced, cannot fallback to v2 "
            f"(reason={reason.name}, is_fatal={is_fatal})"
        )
    elif not is_fatal:
        message = (
            f"Non-fatal issue, staying v3 with placeholder "
            f"(reason={reason.name})"
        )
    elif not allow_fallback:
        message = (
            f"Fallback disabled by config "
            f"(reason={reason.name}, is_fatal={is_fatal})"
        )
    else:
        message = (
            f"Fallback to v2 allowed "
            f"(reason={reason.name}, is_fatal={is_fatal})"
        )

    if notes:
        message += f" - {notes}"

    # 记录 fallback 事件
    event = None
    if reason != VideoPipelineFallbackReason.NONE:
        effective_pipeline = 'v2' if should_fallback else 'v3'
        event = log_fallback(
            reason=reason,
            video_path=path,
            codec=codec,
            pixel_format=pixel_format,
            backend=backend,
            exception=exception,
            user_forced=user_forced_v3,
            effective_pipeline=effective_pipeline,
            notes=notes,
        )

    return FallbackDecision(
        should_fallback=should_fallback,
        reason=reason,
        is_fatal=is_fatal,
        message=message,
        user_forced_v3=user_forced_v3,
        allow_fallback=allow_fallback,
        event=event,
    )


# ================================================================
# ProRes/高位深 格式判断辅助
# ================================================================

# 专业格式编解码器（不应因首帧慢而 fallback）
PROFESSIONAL_CODECS = ('prores', 'dnxhr', 'dnxhd', 'ffv1', 'rawvideo')

# 高位深像素格式（应降级 frame path，不应 fallback v2）
# 注意：不包含泛化的 '422'/'444' 以避免误判 8bit 格式
HIGH_BITDEPTH_FORMATS = (
    'p10le', 'p12le', 'p16le',
    '10le', '12le', '16le', 'f16',
    'gbrp10', 'gbrp12', 'gbrp16',
    'yuv422p10', 'yuv422p12', 'yuv444p10', 'yuv444p12',
    'rgb48', 'rgba64',
)


def is_professional_codec(codec: Optional[str]) -> bool:
    """
    判断是否是专业格式编解码器。

    ProRes/DNxHR 等专业格式首帧可能较慢，不应触发 fallback。

    Args:
        codec: 编解码器名称

    Returns:
        True 表示是专业格式
    """
    if codec is None:
        return False
    codec_lower = codec.lower()
    return codec_lower in PROFESSIONAL_CODECS or \
           any(c in codec_lower for c in PROFESSIONAL_CODECS)


def is_high_bitdepth_format(pixel_format: Optional[str]) -> bool:
    """
    判断是否是高位深像素格式。

    高位深格式应降级 frame path，不应 fallback v2。

    Args:
        pixel_format: 像素格式名称

    Returns:
        True 表示是高位深格式
    """
    if pixel_format is None:
        return False
    fmt_lower = pixel_format.lower()
    return any(f in fmt_lower for f in HIGH_BITDEPTH_FORMATS)


def should_prevent_fallback_for_codec_format(
    codec: Optional[str],
    pixel_format: Optional[str],
) -> bool:
    """
    V-P0-5: 判断是否应该阻止 fallback (ProRes/高位深)。

    Args:
        codec: 编解码器名称
        pixel_format: 像素格式

    Returns:
        True 表示应该阻止 fallback，使用 placeholder
    """
    if is_professional_codec(codec):
        return True
    if is_high_bitdepth_format(pixel_format):
        return True
    return False