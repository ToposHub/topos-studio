"""
Test Video Pipeline Fallback Types (P0-4)

测试 VideoPipelineFallbackReason 枚举和 FallbackEvent 数据类：
- 枚举值正确定义
- FallbackEvent 创建和验证
- FallbackLogger 线程安全记录
- log_fallback 便捷函数
- extract_* 辅助函数

验收命令：
.venv/bin/python -m pytest tests/test_video_pipeline_fallback_types.py -q
"""

from __future__ import annotations

import sys
import os
import pytest
from unittest.mock import MagicMock, PropertyMock
import threading
import time

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# =============================================================================
# Test: VideoPipelineFallbackReason Enum
# =============================================================================

class TestVideoPipelineFallbackReason:
    """测试 VideoPipelineFallbackReason 枚举"""

    def test_enum_values_exist(self) -> None:
        """所有必要的枚举值存在"""
        from src.shared.video.fallback_types import VideoPipelineFallbackReason

        # 检查文档中要求的枚举值
        assert VideoPipelineFallbackReason.NONE.value == "none"
        assert VideoPipelineFallbackReason.USER_FORCED_V2.value == "user_forced_v2"
        assert VideoPipelineFallbackReason.V3_INIT_FAILED.value == "v3_init_failed"
        assert VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED.value == "coordinator_load_failed"
        assert VideoPipelineFallbackReason.SOURCE_OPEN_FAILED.value == "source_open_failed"
        assert VideoPipelineFallbackReason.CODEC_UNSUPPORTED_BY_V3.value == "codec_unsupported_by_v3"
        assert VideoPipelineFallbackReason.FIRST_FRAME_TIMEOUT.value == "first_frame_timeout"
        assert VideoPipelineFallbackReason.FIRST_FRAME_UNAVAILABLE.value == "first_frame_unavailable"
        assert VideoPipelineFallbackReason.FRAME_CONVERSION_FAILED.value == "frame_conversion_failed"
        assert VideoPipelineFallbackReason.RENDERER_UNAVAILABLE.value == "renderer_unavailable"
        # 新增的枚举值
        assert VideoPipelineFallbackReason.PLAYBACK_START_FAILED.value == "playback_start_failed"
        assert VideoPipelineFallbackReason.SESSION_INVALID.value == "session_invalid"
        assert VideoPipelineFallbackReason.UNKNOWN.value == "unknown"

    def test_is_user_forced(self) -> None:
        """测试 is_user_forced() 方法"""
        from src.shared.video.fallback_types import VideoPipelineFallbackReason

        assert VideoPipelineFallbackReason.USER_FORCED_V2.is_user_forced() is True
        assert VideoPipelineFallbackReason.V3_INIT_FAILED.is_user_forced() is False
        assert VideoPipelineFallbackReason.NONE.is_user_forced() is False

    def test_is_technical_failure(self) -> None:
        """测试 is_technical_failure() 方法"""
        from src.shared.video.fallback_types import VideoPipelineFallbackReason

        # NONE 和 USER_FORCED_V2 不是技术故障
        assert VideoPipelineFallbackReason.NONE.is_technical_failure() is False
        assert VideoPipelineFallbackReason.USER_FORCED_V2.is_technical_failure() is False

        # 其他都是技术故障
        assert VideoPipelineFallbackReason.V3_INIT_FAILED.is_technical_failure() is True
        assert VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED.is_technical_failure() is True
        assert VideoPipelineFallbackReason.FRAME_CONVERSION_FAILED.is_technical_failure() is True

    def test_requires_investigation(self) -> None:
        """测试 requires_investigation() 方法"""
        from src.shared.video.fallback_types import VideoPipelineFallbackReason

        # 需要调查的原因
        assert VideoPipelineFallbackReason.UNKNOWN.requires_investigation() is True
        assert VideoPipelineFallbackReason.CODEC_UNSUPPORTED_BY_V3.requires_investigation() is True
        assert VideoPipelineFallbackReason.FIRST_FRAME_UNAVAILABLE.requires_investigation() is True
        assert VideoPipelineFallbackReason.FRAME_CONVERSION_FAILED.requires_investigation() is True

        # 不需要调查的原因
        assert VideoPipelineFallbackReason.NONE.requires_investigation() is False
        assert VideoPipelineFallbackReason.USER_FORCED_V2.requires_investigation() is False
        assert VideoPipelineFallbackReason.V3_INIT_FAILED.requires_investigation() is False


# =============================================================================
# Test: FallbackEvent Data Class
# =============================================================================

class TestFallbackEvent:
    """测试 FallbackEvent 数据类"""

    def test_create_basic_event(self) -> None:
        """创建基本的 FallbackEvent"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
        )

        event = FallbackEvent(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            video_path="/path/to/video.mp4",
            codec="prores",
        )

        assert event.reason == VideoPipelineFallbackReason.V3_INIT_FAILED
        assert event.video_path == "/path/to/video.mp4"
        assert event.codec == "prores"
        assert event.user_forced is False
        assert event.effective_pipeline == "v2"
        assert event.timestamp is not None

    def test_create_full_event(self) -> None:
        """创建完整的 FallbackEvent"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
        )

        event = FallbackEvent(
            reason=VideoPipelineFallbackReason.FRAME_CONVERSION_FAILED,
            video_path="/path/to/prores.mov",
            codec="prores_422",
            pixel_format="yuv422p10le",
            backend="ffmpeg",
            source_type="master_only",
            frame_path="planar_yuv_gpu",
            exception_str="PyAV conversion failed",
            exception_type="ValueError",
            user_forced=False,
            effective_pipeline="v3_degraded",
            degradation_level=1,
            notes="YUV444P12LE RGB conversion error",
        )

        assert event.reason == VideoPipelineFallbackReason.FRAME_CONVERSION_FAILED
        assert event.pixel_format == "yuv422p10le"
        assert event.backend == "ffmpeg"
        assert event.source_type == "master_only"
        assert event.frame_path == "planar_yuv_gpu"
        assert event.exception_str == "PyAV conversion failed"
        assert event.degradation_level == 1
        assert event.notes == "YUV444P12LE RGB conversion error"

    def test_to_dict(self) -> None:
        """测试 to_dict() 方法"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
        )

        event = FallbackEvent(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            codec="h264",
        )

        d = event.to_dict()
        assert d["reason"] == "v3_init_failed"
        assert d["codec"] == "h264"
        assert d["user_forced"] is False
        assert "timestamp" in d

    def test_to_log_string(self) -> None:
        """测试 to_log_string() 方法"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
        )

        event = FallbackEvent(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            video_path="/path/to/video.mp4",
            codec="prores",
            backend="ffmpeg",
        )

        log_str = event.to_log_string()
        assert "reason=v3_init_failed" in log_str
        assert "pipeline=v2" in log_str
        assert "path=/path/to/video.mp4" in log_str
        assert "codec=prores" in log_str
        assert "backend=ffmpeg" in log_str

    def test_user_forced_warning(self) -> None:
        """测试 user_forced=True 但 reason 不是 USER_FORCED_V2 时产生警告"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
        )

        # 这个应该产生警告但不报错
        event = FallbackEvent(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            user_forced=True,
        )

        assert event.user_forced is True
        # 警告应该在 __post_init__ 中记录


# =============================================================================
# Test: FallbackLogger
# =============================================================================

class TestFallbackLogger:
    """测试 FallbackLogger 单例记录器"""

    def test_singleton(self) -> None:
        """测试单例模式"""
        from src.shared.video.fallback_types import FallbackLogger, fallback_logger

        logger1 = FallbackLogger()
        logger2 = FallbackLogger()

        assert logger1 is logger2
        assert logger1 is fallback_logger

    def test_log_event(self) -> None:
        """测试记录事件"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
            fallback_logger,
        )

        # 清除旧记录
        fallback_logger.clear()

        event = FallbackEvent(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            codec="prores",
        )

        fallback_logger.log(event)

        events = fallback_logger.get_recent(limit=1)
        assert len(events) == 1
        assert events[0].reason == VideoPipelineFallbackReason.V3_INIT_FAILED

    def test_get_recent(self) -> None:
        """测试获取最近事件"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
            fallback_logger,
        )

        fallback_logger.clear()

        # 记录多个事件
        for i in range(5):
            event = FallbackEvent(
                reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
                codec=f"codec{i}",
            )
            fallback_logger.log(event)

        # 获取最近 3 个
        events = fallback_logger.get_recent(limit=3)
        assert len(events) == 3

        # 应是最新的在前（按添加顺序）
        # 注意：get_recent 返回的是列表末尾的元素
        assert events[0].codec == "codec2"
        assert events[1].codec == "codec3"
        assert events[2].codec == "codec4"

    def test_get_statistics(self) -> None:
        """测试统计功能"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
            fallback_logger,
        )

        fallback_logger.clear()

        # 记录不同类型的事件
        fallback_logger.log(FallbackEvent(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
        ))
        fallback_logger.log(FallbackEvent(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
        ))
        fallback_logger.log(FallbackEvent(
            reason=VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED,
        ))
        fallback_logger.log(FallbackEvent(
            reason=VideoPipelineFallbackReason.USER_FORCED_V2,
            user_forced=True,
        ))

        stats = fallback_logger.get_statistics()
        assert stats["total_events"] == 4
        assert stats["reason_counts"]["v3_init_failed"] == 2
        assert stats["reason_counts"]["coordinator_load_failed"] == 1
        assert stats["reason_counts"]["user_forced_v2"] == 1
        assert stats["user_forced_count"] == 1
        assert stats["technical_failure_count"] == 3

    def test_max_events_limit(self) -> None:
        """测试最大事件数限制"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
            fallback_logger,
        )

        fallback_logger.clear()

        # 记录超过限制的事件（默认 100）
        # 这里只测试 10 个来节省时间
        fallback_logger._max_events = 10  # 临时降低限制

        for i in range(15):
            event = FallbackEvent(
                reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
                codec=f"codec{i}",
            )
            fallback_logger.log(event)

        events = fallback_logger.get_all()
        assert len(events) == 10

        # 恢复默认值
        fallback_logger._max_events = 100

    def test_thread_safety(self) -> None:
        """测试线程安全"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
            fallback_logger,
        )

        fallback_logger.clear()

        # 多线程同时记录
        def log_events(start_idx: int) -> None:
            for i in range(10):
                event = FallbackEvent(
                    reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
                    codec=f"codec{start_idx + i}",
                )
                fallback_logger.log(event)

        threads = [
            threading.Thread(target=log_events, args=(i * 10,))
            for i in range(3)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = fallback_logger.get_all()
        assert len(events) == 30  # 3 threads * 10 events

    def test_clear(self) -> None:
        """测试清除记录"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
            fallback_logger,
        )

        fallback_logger.log(FallbackEvent(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
        ))
        fallback_logger.clear()

        events = fallback_logger.get_all()
        assert len(events) == 0


# =============================================================================
# Test: Convenience Functions
# =============================================================================

class TestConvenienceFunctions:
    """测试便捷函数"""

    def test_log_fallback_basic(self) -> None:
        """测试 log_fallback 便捷函数"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            log_fallback,
            fallback_logger,
            get_recent_fallback_events,
        )

        fallback_logger.clear()

        event = log_fallback(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            video_path="/path/to/video.mp4",
            codec="prores",
        )

        assert event.reason == VideoPipelineFallbackReason.V3_INIT_FAILED
        assert event.video_path == "/path/to/video.mp4"

        # 检查是否被记录
        events = get_recent_fallback_events(limit=1)
        assert len(events) == 1
        assert events[0] == event

    def test_log_fallback_with_exception(self) -> None:
        """测试 log_fallback 包含异常信息"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            log_fallback,
            fallback_logger,
        )

        fallback_logger.clear()

        exc = ValueError("Test error message")
        event = log_fallback(
            reason=VideoPipelineFallbackReason.FRAME_CONVERSION_FAILED,
            exception=exc,
        )

        assert event.exception_str == "Test error message"
        assert event.exception_type == "ValueError"

    def test_get_recent_fallback_events(self) -> None:
        """测试 get_recent_fallback_events 函数"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            log_fallback,
            fallback_logger,
            get_recent_fallback_events,
        )

        fallback_logger.clear()

        log_fallback(reason=VideoPipelineFallbackReason.V3_INIT_FAILED)
        log_fallback(reason=VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED)

        events = get_recent_fallback_events(limit=2)
        assert len(events) == 2

    def test_get_fallback_statistics(self) -> None:
        """测试 get_fallback_statistics 函数"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            log_fallback,
            fallback_logger,
            get_fallback_statistics,
        )

        fallback_logger.clear()

        log_fallback(reason=VideoPipelineFallbackReason.V3_INIT_FAILED)
        log_fallback(reason=VideoPipelineFallbackReason.V3_INIT_FAILED)

        stats = get_fallback_statistics()
        assert stats["total_events"] == 2
        assert stats["reason_counts"]["v3_init_failed"] == 2

    def test_has_recent_fallback(self) -> None:
        """测试 has_recent_fallback 函数"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            log_fallback,
            fallback_logger,
            has_recent_fallback,
        )

        fallback_logger.clear()
        assert has_recent_fallback() is False

        log_fallback(reason=VideoPipelineFallbackReason.NONE)
        assert has_recent_fallback() is False  # NONE 不算 fallback

        log_fallback(reason=VideoPipelineFallbackReason.V3_INIT_FAILED)
        assert has_recent_fallback() is True

    def test_get_last_fallback_reason(self) -> None:
        """测试 get_last_fallback_reason 函数"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            log_fallback,
            fallback_logger,
            get_last_fallback_reason,
        )

        fallback_logger.clear()
        assert get_last_fallback_reason() is None

        log_fallback(reason=VideoPipelineFallbackReason.V3_INIT_FAILED)
        assert get_last_fallback_reason() == VideoPipelineFallbackReason.V3_INIT_FAILED

        log_fallback(reason=VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED)
        assert get_last_fallback_reason() == VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED


# =============================================================================
# Test: Extract Helper Functions
# =============================================================================

class TestExtractHelperFunctions:
    """测试信息提取辅助函数"""

    def test_extract_codec_info_from_metadata(self) -> None:
        """测试从 metadata 提取 codec"""
        from src.shared.video.fallback_types import extract_codec_info

        source = MagicMock()
        source.metadata = MagicMock()
        source.metadata.codec_name = "prores"

        assert extract_codec_info(source) == "prores"

    def test_extract_codec_info_from_attribute(self) -> None:
        """测试从 codec_name 属性提取"""
        from src.shared.video.fallback_types import extract_codec_info

        source = MagicMock()
        source.metadata = None
        source.codec_name = "h264"

        assert extract_codec_info(source) == "h264"

    def test_extract_codec_info_none(self) -> None:
        """测试空 source"""
        from src.shared.video.fallback_types import extract_codec_info

        assert extract_codec_info(None) is None

        source = MagicMock(spec=[])  # 无任何属性
        assert extract_codec_info(source) is None

    def test_extract_pixel_format_info(self) -> None:
        """测试提取 pixel format"""
        from src.shared.video.fallback_types import extract_pixel_format_info

        source = MagicMock()
        source.metadata = MagicMock()
        source.metadata.pixel_format = "yuv422p10le"

        assert extract_pixel_format_info(source) == "yuv422p10le"

    def test_extract_backend_info_ffmpeg(self) -> None:
        """测试识别 FFmpeg 后端"""
        from src.shared.video.fallback_types import extract_backend_info

        source = MagicMock()
        source.__class__.__name__ = "FFmpegMediaSource"

        assert extract_backend_info(source) == "ffmpeg"

    def test_extract_backend_info_gstreamer(self) -> None:
        """测试识别 GStreamer 后端"""
        from src.shared.video.fallback_types import extract_backend_info

        source = MagicMock()
        source.__class__.__name__ = "GStreamerMediaSource"

        assert extract_backend_info(source) == "gstreamer"

    def test_extract_backend_info_from_attribute(self) -> None:
        """测试从 backend 属性提取"""
        from src.shared.video.fallback_types import extract_backend_info

        source = MagicMock()
        source.__class__.__name__ = "SomeOtherSource"
        source.backend = "ffmpeg"

        assert extract_backend_info(source) == "ffmpeg"


# =============================================================================
# Test: Integration with video_playback
# =============================================================================

class TestIntegrationWithVideoPlayback:
    """测试与 video_playback.py 的集成"""

    def test_enum_importable_from_video(self) -> None:
        """测试从 src.core.video 导入枚举"""
        from src.shared.video import (
            VideoPipelineFallbackReason,
            FallbackEvent,
            log_fallback,
            get_recent_fallback_events,
        )

        # 确保枚举值可用
        assert VideoPipelineFallbackReason.NONE.value == "none"
        assert VideoPipelineFallbackReason.V3_INIT_FAILED.value == "v3_init_failed"

    def test_enum_importable_from_video_playback(self) -> None:
        """测试从 video_playback.py 导入枚举"""
        from src.shared.video.video_playback import VideoPipelineFallbackReason

        # 确保枚举值与 fallback_types 一致
        assert VideoPipelineFallbackReason.NONE.value == "none"
        assert VideoPipelineFallbackReason.V3_INIT_FAILED.value == "v3_init_failed"


# =============================================================================
# Test: Enum Compatibility
# =============================================================================

class TestEnumCompatibility:
    """测试新旧枚举兼容性"""

    def test_enum_comparison(self) -> None:
        """测试枚举对象比较"""
        from src.shared.video.fallback_types import VideoPipelineFallbackReason

        # 对象比较应该工作
        assert VideoPipelineFallbackReason.NONE == VideoPipelineFallbackReason.NONE
        assert VideoPipelineFallbackReason.NONE != VideoPipelineFallbackReason.V3_INIT_FAILED

    def test_enum_in_dataclass(self) -> None:
        """测试枚举在数据类中的使用"""
        from src.shared.video.fallback_types import (
            VideoPipelineFallbackReason,
            FallbackEvent,
        )

        # 创建两个相同的事件
        event1 = FallbackEvent(reason=VideoPipelineFallbackReason.NONE)
        event2 = FallbackEvent(reason=VideoPipelineFallbackReason.NONE)

        # reason 比较
        assert event1.reason == event2.reason

        # 非 NONE 检查
        event3 = FallbackEvent(reason=VideoPipelineFallbackReason.V3_INIT_FAILED)
        assert event3.reason != VideoPipelineFallbackReason.NONE


# =============================================================================
# Cleanup
# =============================================================================

@pytest.fixture(autouse=True)
def cleanup_fallback_logger():
    """每个测试后清除 fallback_logger"""
    from src.shared.video.fallback_types import fallback_logger
    yield
    fallback_logger.clear()