"""
Media Error Handling Module
媒体错误处理，提供用户友好的错误消息和降级建议。

## 职责

- MediaErrorHandler: 错误处理器，统一错误消息格式
- FallbackStrategy: 降级策略，尝试替代方案
- 错误分类和用户提示

## 设计原则

- 错误消息用户友好（技术细节转为建议）
- 提供降级方案（不同后端、降低位深、代理分辨率）
- 不崩溃，优雅降级
- 完整类型注解

## 使用示例

```python
from src.shared.media import MediaErrorHandler, FallbackStrategy

# 处理错误
handler = MediaErrorHandler()
message = handler.get_user_message(error)
print(message['title'], message['solution'])

# 尝试降级
fallback = FallbackStrategy()
source = fallback.attempt(error, '/path/to/video.mp4')
```
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Any

from .errors import (
    MediaError,
    UnsupportedCodecError,
    DecodeError,
    SeekError,
    FileCorruptedError,
)
from .source import MediaSourceError, MediaOpenError, MediaDecodeError, MediaSeekError
from .factory import MediaSourceFactory

logger = logging.getLogger(__name__)


def _close_quietly(source: Any) -> None:
    """Best-effort close for a partially opened MediaSource (AUD-34519).

    降级路径里 ``MediaSourceFactory.create`` 成功而 ``open`` 失败时，已创建
    的 source 可能已持有原生解码器句柄/探测缓存——不关闭则每轮降级尝试
    都泄漏一个句柄。关闭本身失败不应掩盖原始 open 异常，只留痕。
    """
    close = getattr(source, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("降级路径 MediaSource 关闭失败", exc_info=True)


# ================================================================
# 错误消息模板
# ================================================================

@dataclass(frozen=True)
class ErrorMessageTemplate:
    """
    错误消息模板。

    Attributes:
        title: 错误标题（用户可见）
        solution: 解决方案建议
        can_retry: 是否可以重试
        can_fallback: 是否可以降级
        severity: 错误严重程度
    """
    title: str
    solution: str
    can_retry: bool = True
    can_fallback: bool = False
    severity: str = 'warning'  # 'error', 'warning', 'info'


class ErrorSeverity(Enum):
    """
    错误严重程度。

    Values:
        ERROR: 严重错误，无法继续
        WARNING: 警告，可以继续但效果受限
        INFO: 信息性提示
    """
    ERROR = 'error'
    WARNING = 'warning'
    INFO = 'info'


# 预定义的错误消息模板
ERROR_MESSAGE_TEMPLATES: dict[type[Exception], ErrorMessageTemplate] = {
    # 不支持的编解码器
    UnsupportedCodecError: ErrorMessageTemplate(
        title='不支持的编解码器',
        solution='请安装支持该格式的解码器，或转换为支持的格式（如 H.264、ProRes）',
        can_retry=False,
        can_fallback=True,
        severity='error',
    ),

    # 解码失败
    DecodeError: ErrorMessageTemplate(
        title='解码失败',
        solution='文件可能损坏，请尝试重新导入或检查文件完整性',
        can_retry=True,
        can_fallback=True,
        severity='warning',
    ),

    # 帧定位失败
    SeekError: ErrorMessageTemplate(
        title='帧定位失败',
        solution='此格式不支持精确定位，将使用最近的可用帧',
        can_retry=True,
        can_fallback=True,
        severity='info',
    ),

    # 文件损坏
    FileCorruptedError: ErrorMessageTemplate(
        title='文件损坏',
        solution='文件结构损坏无法解析，请尝试修复或使用备份文件',
        can_retry=False,
        can_fallback=False,
        severity='error',
    ),

    # 文件打开失败
    MediaOpenError: ErrorMessageTemplate(
        title='无法打开文件',
        solution='请检查文件是否存在、权限是否正确、文件是否被占用',
        can_retry=True,
        can_fallback=False,
        severity='error',
    ),

    # 解码失败（source 模块）
    MediaDecodeError: ErrorMessageTemplate(
        title='解码失败',
        solution='解码过程出错，可能是文件损坏或格式不支持',
        can_retry=True,
        can_fallback=True,
        severity='warning',
    ),

    # 定位失败（source 模块）
    MediaSeekError: ErrorMessageTemplate(
        title='帧定位失败',
        solution='无法定位到目标帧，将使用最近的可用帧',
        can_retry=True,
        can_fallback=True,
        severity='info',
    ),

    # 权限问题
    PermissionError: ErrorMessageTemplate(
        title='权限不足',
        solution='请检查文件访问权限，或关闭占用该文件的程序',
        can_retry=True,
        can_fallback=False,
        severity='error',
    ),
}

# 默认模板（未知错误）
DEFAULT_ERROR_TEMPLATE: ErrorMessageTemplate = ErrorMessageTemplate(
    title='未知错误',
    solution='请检查文件是否正确，或联系技术支持',
    can_retry=True,
    can_fallback=False,
    severity='error',
)


# ================================================================
# 用户消息结构
# ================================================================

@dataclass
class UserMessage:
    """
    用户友好的错误消息。

    Attributes:
        title: 错误标题（简短，用户可见）
        message: 原始错误消息（技术细节）
        solution: 解决方案建议
        can_retry: 是否可以重试操作
        can_fallback: 是否可以尝试降级方案
        severity: 严重程度
        error_type: 错误类型名称
        technical_details: 技术细节（可选，供开发者参考）
    """
    title: str
    message: str
    solution: str
    can_retry: bool
    can_fallback: bool
    severity: str
    error_type: str
    technical_details: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            'title': self.title,
            'message': self.message,
            'solution': self.solution,
            'can_retry': self.can_retry,
            'can_fallback': self.can_fallback,
            'severity': self.severity,
            'error_type': self.error_type,
            'technical_details': self.technical_details,
        }

    def __repr__(self) -> str:
        """字符串表示"""
        return (
            f"UserMessage(title='{self.title}', "
            f"severity='{self.severity}', "
            f"can_retry={self.can_retry}, "
            f"can_fallback={self.can_fallback})"
        )


# ================================================================
# 媒体错误处理器
# ================================================================

class MediaErrorHandler:
    """
    媒体错误处理器。

    提供统一的错误消息格式和用户友好的提示。

    Features:
    - 错误分类和映射
    - 用户友好的消息生成
    - 严重程度评估
    - 重试/降级建议

    Usage:
        handler = MediaErrorHandler()
        message = handler.get_user_message(error)
    """

    def __init__(
        self,
        custom_templates: Optional[dict[type[Exception], ErrorMessageTemplate]] = None
    ) -> None:
        """
        初始化错误处理器。

        Args:
            custom_templates: 自定义错误模板（覆盖默认）
        """
        # 合并默认和自定义模板
        self._templates = ERROR_MESSAGE_TEMPLATES.copy()
        if custom_templates:
            self._templates.update(custom_templates)

    def get_user_message(
        self,
        error: Exception,
        include_technical_details: bool = False
    ) -> UserMessage:
        """
        获取用户友好的错误消息。

        Args:
            error: 异常对象
            include_technical_details: 是否包含技术细节

        Returns:
            UserMessage 用户消息对象
        """
        # 获取错误类型
        error_type = type(error)

        # 查找模板
        template = self._templates.get(error_type, DEFAULT_ERROR_TEMPLATE)

        # 提取技术细节
        technical_details: Optional[dict[str, Any]] = None
        if include_technical_details:
            technical_details = self._extract_technical_details(error)

        # 构建消息
        return UserMessage(
            title=template.title,
            message=str(error),
            solution=self._customize_solution(error, template.solution),
            can_retry=template.can_retry,
            can_fallback=self._can_fallback(error, template.can_fallback),
            severity=template.severity,
            error_type=error_type.__name__,
            technical_details=technical_details,
        )

    def _customize_solution(
        self,
        error: Exception,
        template_solution: str
    ) -> str:
        """
        根据具体错误定制解决方案。

        Args:
            error: 异常对象
            template_solution: 模板解决方案

        Returns:
            定制后的解决方案
        """
        # UnsupportedCodecError: 添加具体编解码器信息
        if isinstance(error, UnsupportedCodecError):
            return f"{template_solution}（当前编解码器: {error.codec_name}）"

        # DecodeError: 添加帧号信息
        if isinstance(error, DecodeError):
            if error.frame_index is not None:
                return f"{template_solution}（发生在第 {error.frame_index} 帧）"
            return template_solution

        # SeekError: 添加目标帧信息
        if isinstance(error, SeekError):
            if error.actual_frame is not None:
                return f"{template_solution}（目标: {error.target_frame}, 实际: {error.actual_frame}）"
            return template_solution

        # MediaOpenError: 添加路径信息
        if isinstance(error, MediaOpenError):
            if error.source_path:
                return f"{template_solution}（文件: {error.source_path}）"

        return template_solution

    def _can_fallback(
        self,
        error: Exception,
        template_can_fallback: bool
    ) -> bool:
        """
        判断是否可以尝试降级方案。

        Args:
            error: 异常对象
            template_can_fallback: 模板默认值

        Returns:
            是否可以降级
        """
        # UnsupportedCodecError: 可以尝试其他后端
        if isinstance(error, UnsupportedCodecError):
            return True

        # DecodeError: 可以尝试降低位深或代理
        if isinstance(error, DecodeError):
            return True

        # SeekError: 可以使用最近的帧
        if isinstance(error, SeekError):
            return True

        # 其他情况使用模板默认值
        return template_can_fallback

    def _extract_technical_details(
        self,
        error: Exception
    ) -> dict[str, Any]:
        """
        提取技术细节。

        Args:
            error: 异常对象

        Returns:
            技术细节字典
        """
        details: dict[str, Any] = {
            'exception_type': type(error).__name__,
            'exception_module': type(error).__module__,
            'message': str(error),
        }

        # UnsupportedCodecError
        if isinstance(error, UnsupportedCodecError):
            details['codec_name'] = error.codec_name
            details['reason'] = error.reason

        # DecodeError
        if isinstance(error, DecodeError):
            details['frame_index'] = error.frame_index
            details['reason'] = error.reason

        # SeekError
        if isinstance(error, SeekError):
            details['target_frame'] = error.target_frame
            details['actual_frame'] = error.actual_frame

        # MediaSourceError
        if isinstance(error, MediaSourceError):
            details['source_path'] = error.source_path

        return details

    def get_severity_icon(self, severity: str) -> str:
        """
        获取严重程度对应的图标符号。

        Args:
            severity: 严重程度字符串

        Returns:
            图标符号
        """
        icons = {
            'error': '❌',
            'warning': '⚠️',
            'info': 'ℹ️',
        }
        return icons.get(severity, '❓')

    def format_for_display(
        self,
        message: UserMessage,
        include_solution: bool = True
    ) -> str:
        """
        格式化为显示文本。

        Args:
            message: 用户消息
            include_solution: 是否包含解决方案

        Returns:
            格式化文本
        """
        icon = self.get_severity_icon(message.severity)
        lines = [
            f"{icon} {message.title}",
            f"详情: {message.message}",
        ]

        if include_solution and message.solution:
            lines.append(f"建议: {message.solution}")

        return '\n'.join(lines)


# ================================================================
# 降级策略
# ================================================================

class FallbackLevel(Enum):
    """
    降级级别。

    Values:
        NONE: 不降级
        BACKEND: 切换后端
        BIT_DEPTH: 降低位深
        PROXY: 使用代理分辨率
        MINIMAL: 最小兼容模式
    """
    NONE = 'none'
    BACKEND = 'backend'
    BIT_DEPTH = 'bit_depth'
    PROXY = 'proxy'
    MINIMAL = 'minimal'


@dataclass
class FallbackResult:
    """
    降级尝试结果。

    Attributes:
        success: 是否成功
        level: 降级级别
        source: 成功时的媒体源
        error: 失败时的错误
        message: 用户消息
    """
    success: bool
    level: FallbackLevel
    source: Optional[Any] = None  # MediaSource，但避免导入循环
    error: Optional[Exception] = None
    message: str = ''

    def __repr__(self) -> str:
        """字符串表示"""
        status = "成功" if self.success else "失败"
        return f"FallbackResult({status}, level={self.level.value})"


class FallbackStrategy:
    """
    降级策略执行器。

    在错误发生时尝试多种降级方案。

    Features:
    - 后端切换（GStreamer 已移除，仅 FFmpeg）
    - 位深降低（10-bit -> 8-bit）
    - 代理分辨率（降低解码分辨率）
    - 最小兼容模式

    Usage:
        strategy = FallbackStrategy()
        result = strategy.attempt(error, '/path/to/video.mp4')
    """

    def __init__(
        self,
        max_attempts: int = 3,
        verbose: bool = False
    ) -> None:
        """
        初始化降级策略。

        Args:
            max_attempts: 最大尝试次数
            verbose: 是否输出详细日志
        """
        self.max_attempts = max_attempts
        self.verbose = verbose

        # 后端兜底顺序（factory 不可用时使用；正常路径按文件内容经
        # MediaSourceFactory._get_backend_order('auto', path) 动态计算——
        # Topos MOV 需要内容探测路由，R-17）
        self._backend_order: list[str] = ['ffmpeg']

    def attempt(
        self,
        error: Exception,
        path: str,
        original_backend: Optional[str] = None
    ) -> FallbackResult:
        """
        尝试降级方案。

        按优先级尝试多种方案：
        1. 切换后端
        2. 降低位深（通过代理）
        3. 最小兼容模式

        Args:
            error: 原始错误
            path: 媒体文件路径
            original_backend: 原始使用的后端

        Returns:
            FallbackResult 降级结果
        """
        if not os.path.exists(path):
            return FallbackResult(
                success=False,
                level=FallbackLevel.NONE,
                error=FileNotFoundError(f"文件不存在: {path}"),
                message=f"文件不存在: {path}",
            )

        # 记录尝试历史
        attempts: list[tuple[FallbackLevel, str]] = []

        # 1. 尝试切换后端
        result = self._try_backend_switch(path, original_backend)
        attempts.append((FallbackLevel.BACKEND, result.message))

        if result.success:
            logger.info(f"降级成功: 后端切换 -> {result.level.value}")
            return result

        # 2. 尝试代理分辨率
        result = self._try_proxy_resolution(path)
        attempts.append((FallbackLevel.PROXY, result.message))

        if result.success:
            logger.info(f"降级成功: 代理分辨率 -> {result.level.value}")
            return result

        # 3. 尝试最小兼容模式
        result = self._try_minimal_mode(path)
        attempts.append((FallbackLevel.MINIMAL, result.message))

        if result.success:
            logger.info(f"降级成功: 最小兼容模式 -> {result.level.value}")
            return result

        # 所有降级方案都失败
        logger.warning(f"所有降级方案失败: {path}")

        return FallbackResult(
            success=False,
            level=FallbackLevel.NONE,
            error=error,
            message="所有降级方案失败。尝试历史:\n" +
                    '\n'.join(f"  - {lvl.value}: {msg}" for lvl, msg in attempts),
        )

    def _try_backend_switch(
        self,
        path: str,
        original_backend: Optional[str] = None
    ) -> FallbackResult:
        """
        尝试切换后端。

        Args:
            path: 媒体文件路径
            original_backend: 原始后端

        Returns:
            FallbackResult
        """
        # 确定尝试的后端顺序：按文件内容走 factory auto 路由
        # （Topos MOV → ['topos', 'ffmpeg']，其余 → ['ffmpeg']，R-17）
        try:
            backends_to_try = MediaSourceFactory._get_backend_order('auto', path).copy()
        except Exception:
            backends_to_try = self._backend_order.copy()

        if original_backend:
            # 移除原始后端（已经失败）
            if original_backend in backends_to_try:
                backends_to_try.remove(original_backend)

        for backend in backends_to_try:
            try:
                if self.verbose:
                    logger.info(f"尝试后端: {backend}")

                source = MediaSourceFactory.create(path, backend=backend)
                try:
                    source.open()
                except Exception:
                    # AUD-34519：open 失败时已创建的 source 必须显式关闭
                    _close_quietly(source)
                    raise

                return FallbackResult(
                    success=True,
                    level=FallbackLevel.BACKEND,
                    source=source,
                    message=f"使用 {backend} 后端成功",
                )

            except Exception as e:
                logger.warning(f"后端 {backend} 失败: {e}")
                continue

        return FallbackResult(
            success=False,
            level=FallbackLevel.BACKEND,
            message="所有后端都无法打开文件",
        )

    def _try_proxy_resolution(
        self,
        path: str,
        target_size: tuple[int, int] = (1280, 720)
    ) -> FallbackResult:
        """
        尝试代理分辨率。

        使用较低的分辨率解码，减少内存和计算压力。

        Args:
            path: 媒体文件路径
            target_size: 目标尺寸

        Returns:
            FallbackResult
        """
        try:
            if self.verbose:
                logger.info(f"尝试代理分辨率: {target_size}")

            source = MediaSourceFactory.create(path, target_size=target_size)
            try:
                source.open()
            except Exception:
                # AUD-34519：同 backend 降级路径——open 失败先关闭再上报
                _close_quietly(source)
                raise

            return FallbackResult(
                success=True,
                level=FallbackLevel.PROXY,
                source=source,
                message=f"使用代理分辨率 {target_size} 成功",
            )

        except Exception as e:
            logger.warning(f"代理分辨率失败: {e}")
            return FallbackResult(
                success=False,
                level=FallbackLevel.PROXY,
                message=f"代理分辨率解码失败: {e}",
            )

    def _try_minimal_mode(
        self,
        path: str
    ) -> FallbackResult:
        """
        尝试最小兼容模式。

        使用最保守的配置尝试解码。

        Args:
            path: 媒体文件路径

        Returns:
            FallbackResult
        """
        try:
            if self.verbose:
                logger.info("尝试最小兼容模式")

            # 使用最小配置
            # 1. 使用任何可用后端
            # 2. 使用最小分辨率（512x288）
            # 3. 不要求精确 seek

            source = MediaSourceFactory.create(
                path,
                backend='auto',
                target_size=(512, 288)
            )
            try:
                source.open()
            except Exception:
                # AUD-34519：同 backend 降级路径——open 失败先关闭再上报
                _close_quietly(source)
                raise

            return FallbackResult(
                success=True,
                level=FallbackLevel.MINIMAL,
                source=source,
                message="使用最小兼容模式成功（分辨率 512x288）",
            )

        except Exception as e:
            logger.warning(f"最小兼容模式失败: {e}")
            return FallbackResult(
                success=False,
                level=FallbackLevel.MINIMAL,
                message=f"最小兼容模式失败: {e}",
            )

    def get_available_fallbacks(
        self,
        error: Exception
    ) -> list[FallbackLevel]:
        """
        根据错误类型获取可用的降级方案列表。

        Args:
            error: 异常对象

        Returns:
            可用的降级级别列表
        """
        fallbacks: list[FallbackLevel] = []

        # UnsupportedCodecError: 可以切换后端
        if isinstance(error, UnsupportedCodecError):
            fallbacks.append(FallbackLevel.BACKEND)

        # DecodeError: 可以降低位深或使用代理
        if isinstance(error, DecodeError):
            fallbacks.append(FallbackLevel.BACKEND)
            fallbacks.append(FallbackLevel.PROXY)

        # SeekError: 可以使用最近的帧（不需要特殊处理）
        if isinstance(error, SeekError):
            # SeekError 通常可以继续处理，不需要降级
            pass

        # MediaOpenError: 可能是权限问题，无法降级
        if isinstance(error, MediaOpenError):
            # 无法降级
            pass

        # 默认：尝试所有方案
        if not fallbacks:
            fallbacks = [
                FallbackLevel.BACKEND,
                FallbackLevel.PROXY,
                FallbackLevel.MINIMAL,
            ]

        return fallbacks


# ================================================================
# 错误处理辅助函数
# ================================================================

def handle_media_error(
    error: Exception,
    path: Optional[str] = None,
    attempt_fallback: bool = True
) -> tuple[UserMessage, Optional[Any]]:
    """
    处理媒体错误的便捷函数。

    Args:
        error: 异常对象
        path: 媒体文件路径（用于降级尝试）
        attempt_fallback: 是否尝试降级

    Returns:
        (UserMessage, MediaSource 或 None)
    """
    handler = MediaErrorHandler()
    message = handler.get_user_message(error)

    source = None

    if attempt_fallback and message.can_fallback and path:
        strategy = FallbackStrategy()
        result = strategy.attempt(error, path)

        if result.success:
            source = result.source
            # 更新消息
            message = UserMessage(
                title=f"{message.title}（已降级）",
                message=message.message,
                solution=f"{message.solution}\n已自动切换到降级模式。",
                can_retry=False,
                can_fallback=False,
                severity='warning',
                error_type=message.error_type,
                technical_details=message.technical_details,
            )

    return message, source


def safe_open_media(
    path: str,
    backend: str = 'auto',
    fallback: bool = True
) -> tuple[Any, Optional[UserMessage]]:
    """
    安全打开媒体文件的便捷函数。

    Args:
        path: 媒体文件路径
        backend: 后端选择
        fallback: 是否尝试降级

    Returns:
        (MediaSource 或 None, UserMessage 或 None)
    """
    try:
        source = MediaSourceFactory.create(path, backend=backend)
        source.open()
        return source, None

    except Exception as e:
        message, fallback_source = handle_media_error(
            e, path, attempt_fallback=fallback
        )

        if fallback_source:
            return fallback_source, message

        return None, message


# ================================================================
# 错误上下文管理器
# ================================================================

class MediaErrorContext:
    """
    媒体错误上下文管理器。

    提供自动错误处理和降级。

    Usage:
        with MediaErrorContext('/path/to/video.mp4') as ctx:
            if ctx.source:
                frame = ctx.source.read_frame(0)
            else:
                print(ctx.message)
    """

    def __init__(
        self,
        path: str,
        backend: str = 'auto',
        fallback: bool = True
    ) -> None:
        """
        初始化上下文管理器。

        Args:
            path: 媒体文件路径
            backend: 后端选择
            fallback: 是否尝试降级
        """
        self.path = path
        self.backend = backend
        self.fallback = fallback

        self.source: Optional[Any] = None
        self.message: Optional[UserMessage] = None
        self.error: Optional[Exception] = None

    def __enter__(self) -> 'MediaErrorContext':
        """进入上下文"""
        self.source, self.message = safe_open_media(
            self.path,
            self.backend,
            self.fallback
        )
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[Exception],
        exc_tb: Any
    ) -> bool:
        """退出上下文"""
        # 清理资源
        if self.source:
            try:
                self.source.close()
            except Exception:
                pass

        # 处理内部异常
        if exc_type and isinstance(exc_val, MediaError):
            handler = MediaErrorHandler()
            self.message = handler.get_user_message(exc_val)
            self.error = exc_val
            return True  # 抑制异常

        return False  # 不抑制其他异常

    @property
    def success(self) -> bool:
        """是否成功打开"""
        return self.source is not None