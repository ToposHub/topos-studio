"""
Playback HUD Panel Module
播放性能监控 HUD 面板，实时显示视频播放管线的关键指标。

## 职责

- PlaybackHUDPanel: 播放性能监控 HUD UI 面板
- PlaybackHUDModel: 数据模型，聚合各组件的性能统计

## 设计原则（P5-1）

- 显示关键播放指标，避免 AI "凭感觉优化"
- p50/p95/max 统计，反映真实性能分布
- 导出日志功能，支持后续分析
- 低开销：仅在有播放时更新
- 可隐藏/显示（不影响正常使用）

## 显示指标

```text
pipeline: v3/coordinator or v2
backend: ffmpeg/gstreamer
codec: prores/hevc/h264/...
pixel_format: yuv422p10le/...
source_type: disk_proxy/memory_proxy/master_only
proxy_size: 1920x1080
frame_path: planar_yuv_gpu/packed_rgb_u8/packed_rgb_f16
hwaccel: videotoolbox/nvdec/cpu
fallback_reason: none/...
decode_ms p50/p95/max
upload_ms p50/p95/max
render_ms p50/p95/max
ui_tick_ms p50/p95/max
queue_depth p95
frames_displayed
frames_dropped
frames_held
```

## 使用示例

```python
from src.shared.ui.playback_hud import PlaybackHUDPanel, PlaybackHUDModel

# 创建 HUD 面板
hud_panel = PlaybackHUDPanel()

# AUD-38009: 数据模型是 PlaybackHUDModel（无参构造），面板内部自建并
# 经 model 属性暴露；不要用 PlaybackHUDPanel 构造"数据模型"。
hud_model = hud_panel.model
hud_model.set_video_info(metadata, pipeline_decision)
hud_model.update_stats(perf_stats)

# 显示/隐藏
hud_panel.toggle_visibility()

# 导出日志
hud_panel.export_log()
```
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Sequence
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer

from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


# =============================================================================
# 数据结构
# =============================================================================

@dataclass
class PercentileStats:
    """
    百分位统计数据。

    用于记录 p50/p95/max 等关键性能指标。

    Attributes:
        p50: 第 50 百分位（中位数）
        p95: 第 95 百分位（接近最差）
        max: 最大值
        avg: 平均值（可选）
        count: 样本数
    """

    p50: float = 0.0
    p95: float = 0.0
    max: float = 0.0
    avg: float = 0.0
    count: int = 0

    def to_dict(self) -> Dict[str, float]:
        """转换为字典"""
        return {
            'p50': self.p50,
            'p95': self.p95,
            'max': self.max,
            'avg': self.avg,
            'count': self.count,
        }

    def format_display(self, unit: str = 'ms') -> str:
        """
        格式化显示字符串。

        Args:
            unit: 单位（默认 'ms'）

        Returns:
            格式化字符串，如 "p50=15.0/p95=28.0/max=45.0 ms"
        """
        return f"p50={self.p50:.1f}/p95={self.p95:.1f}/max={self.max:.1f} {unit}"


@dataclass
class VideoPipelineInfo:
    """
    视频管线信息。

    记录当前播放使用的管线配置。

    Attributes:
        pipeline: 管线版本（v3/v2）
        backend: 后端类型（ffmpeg/gstreamer）
        codec: 编码格式（prores/hevc/h264/...）
        pixel_format: 像素格式（yuv422p10le/...）
        source_type: 源类型（disk_proxy/memory_proxy/master_only）
        proxy_size: 代理分辨率
        frame_path: 帧路径类型
        hwaccel: 硬件加速类型
        fallback_reason: 降级原因
    """

    pipeline: str = "unknown"
    backend: str = "unknown"
    codec: str = "unknown"
    pixel_format: str = "unknown"
    source_type: str = "unknown"
    proxy_size: str = "unknown"
    frame_path: str = "unknown"
    hwaccel: str = "unknown"
    fallback_reason: str = "none"
    # Phase A: 零拷贝追踪字段
    decode_path: str = "unknown"
    zero_copy_on: bool = False
    cpu_download_frames: int = 0
    cpu_upload_frames: int = 0
    external_texture_frames: int = 0

    def to_dict(self) -> Dict[str, str]:
        """转换为字典"""
        return {
            'pipeline': self.pipeline,
            'backend': self.backend,
            'codec': self.codec,
            'pixel_format': self.pixel_format,
            'source_type': self.source_type,
            'proxy_size': self.proxy_size,
            'frame_path': self.frame_path,
            'hwaccel': self.hwaccel,
            'fallback_reason': self.fallback_reason,
            'decode_path': self.decode_path,
            'zero_copy_on': str(self.zero_copy_on),
            'cpu_download_frames': str(self.cpu_download_frames),
            'cpu_upload_frames': str(self.cpu_upload_frames),
            'external_texture_frames': str(self.external_texture_frames),
        }


@dataclass
class PlaybackPerformanceStats:
    """
    播放性能统计数据。

    记录播放管线的各阶段耗时和统计。

    Attributes:
        decode_ms: 解码耗时统计
        upload_ms: GPU 上传耗时统计
        render_ms: 渲染耗时统计
        ui_tick_ms: UI tick 耗时统计
        queue_depth: 队列深度统计
        frames_displayed: 显示帧数
        frames_dropped: 丢帧数
        frames_held: 持有帧数
        cache_hit_rate: 缓存命中率
        session_duration: 会话时长（秒）
        fps: 当前帧率
        degradation_level: 降级级别
    """

    decode_ms: PercentileStats = field(default_factory=PercentileStats)
    upload_ms: PercentileStats = field(default_factory=PercentileStats)
    render_ms: PercentileStats = field(default_factory=PercentileStats)
    ui_tick_ms: PercentileStats = field(default_factory=PercentileStats)
    queue_depth: PercentileStats = field(default_factory=PercentileStats)
    frames_displayed: int = 0
    frames_dropped: int = 0
    frames_held: int = 0
    cache_hit_rate: float = 0.0
    session_duration: float = 0.0
    fps: float = 0.0
    degradation_level: int = 0
    proxy_ladder_index: int = -1
    proxy_long_edge: int = 0
    original_long_edge: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'decode_ms': self.decode_ms.to_dict(),
            'upload_ms': self.upload_ms.to_dict(),
            'render_ms': self.render_ms.to_dict(),
            'ui_tick_ms': self.ui_tick_ms.to_dict(),
            'queue_depth': self.queue_depth.to_dict(),
            'frames_displayed': self.frames_displayed,
            'frames_dropped': self.frames_dropped,
            'frames_held': self.frames_held,
            'cache_hit_rate': self.cache_hit_rate,
            'session_duration': self.session_duration,
            'fps': self.fps,
            'degradation_level': self.degradation_level,
            'proxy_ladder_index': self.proxy_ladder_index,
            'proxy_long_edge': self.proxy_long_edge,
            'original_long_edge': self.original_long_edge,
        }


# =============================================================================
# PlaybackHUDModel: 数据模型
# =============================================================================

class PlaybackHUDModel:
    """
    播放 HUD 数据模型。

    聚合各组件的性能统计，为 UI 提供统一的数据接口。

    Attributes:
        pipeline_info: 视频管线信息
        perf_stats: 播放性能统计
        update_time: 最后更新时间

    数据来源：
    - VideoSession: 管线信息、源类型、帧统计
    - VideoPerformanceMonitor: decode/upload/tick 等耗时
    - AsyncRenderer: render 队列深度、丢帧
    - PlaybackClock: 帧率、时钟状态
    """

    def __init__(self) -> None:
        """初始化数据模型"""
        self._pipeline_info: VideoPipelineInfo = VideoPipelineInfo()
        self._perf_stats: PlaybackPerformanceStats = PlaybackPerformanceStats()
        self._update_time: float = 0.0
        self._session_start_time: float = 0.0
        self._video_path: Optional[str] = None
        self._metadata: Optional[Any] = None

        # 历史记录（用于导出）
        # AUD-38007: _history 由 record_sample 写入（此前从未被填充，导出
        # 恒为空），同样受 _max_history 上限约束。
        self._history: List[Dict[str, Any]] = []
        self._max_history: int = 1000  # 最多保留 1000 条记录

        # 性能数据累积
        # AUD-38007: 各指标样本改用有界 deque（maxlen=_max_history），
        # 超限自动丢弃最旧样本，长会话不再无界增长。
        self._decode_times: deque = deque(maxlen=self._max_history)
        self._upload_times: deque = deque(maxlen=self._max_history)
        self._render_times: deque = deque(maxlen=self._max_history)
        self._tick_times: deque = deque(maxlen=self._max_history)
        self._queue_depths: deque = deque(maxlen=self._max_history)

        logger.debug("PlaybackHUDModel initialized")

    @property
    def pipeline_info(self) -> VideoPipelineInfo:
        """获取管线信息"""
        return self._pipeline_info

    @property
    def perf_stats(self) -> PlaybackPerformanceStats:
        """获取性能统计"""
        return self._perf_stats

    @property
    def video_path(self) -> Optional[str]:
        """获取视频路径"""
        return self._video_path

    def reset(self) -> None:
        """
        重置所有数据。

        在新视频加载或会话结束时调用。
        """
        self._pipeline_info = VideoPipelineInfo()
        self._perf_stats = PlaybackPerformanceStats()
        self._decode_times.clear()
        self._upload_times.clear()
        self._render_times.clear()
        self._tick_times.clear()
        self._queue_depths.clear()
        self._session_start_time = time.time()
        self._update_time = 0.0
        self._history.clear()

        logger.debug("PlaybackHUDModel reset")

    def start_session(
        self,
        video_path: str,
        metadata: Any,
        pipeline_decision: Any,
        hwaccel_status: Any = None,
    ) -> None:
        """
        开始新的播放会话。

        Args:
            video_path: 视频文件路径
            metadata: 视频元数据（VideoMetadata）
            pipeline_decision: 管线决策（VideoPipelineDecision）
            hwaccel_status: 硬件解码状态（HardwareDecodeStatus）
        """
        self.reset()
        self._video_path = video_path
        self._metadata = metadata
        self._session_start_time = time.time()

        # 设置管线信息
        self._set_pipeline_info(metadata, pipeline_decision, hwaccel_status)

        logger.info(
            f"PlaybackHUDModel session started: {video_path}, "
            f"pipeline={self._pipeline_info.pipeline}"
        )

    def _set_pipeline_info(
        self,
        metadata: Any,
        pipeline_decision: Any,
        hwaccel_status: Any = None,
    ) -> None:
        """
        设置管线信息。

        Args:
            metadata: 视频元数据
            pipeline_decision: 管线决策
            hwaccel_status: 硬件解码状态
        """
        # 管线版本
        if hasattr(pipeline_decision, 'effective_mode'):
            self._pipeline_info.pipeline = pipeline_decision.effective_mode.value
        elif hasattr(pipeline_decision, 'is_v3_active'):
            self._pipeline_info.pipeline = 'v3' if pipeline_decision.is_v3_active() else 'v2'

        # Backend（从 MediaSource 获取）
        # 暂时使用 ffmpeg 作为默认值，后续可从 SourceManager 获取
        self._pipeline_info.backend = 'ffmpeg'

        # Codec 和 pixel format（从元数据获取）
        if metadata:
            self._pipeline_info.codec = getattr(metadata, 'codec_name', 'unknown')
            self._pipeline_info.pixel_format = getattr(metadata, 'pixel_format', 'unknown')

            # Proxy size（如果有）
            width = getattr(metadata, 'width', 0)
            height = getattr(metadata, 'height', 0)
            if width and height:
                self._pipeline_info.proxy_size = f"{width}x{height}"

        # Hardware acceleration
        if hwaccel_status:
            if hasattr(hwaccel_status, 'hwaccel_enabled') and hwaccel_status.hwaccel_enabled:
                self._pipeline_info.hwaccel = getattr(hwaccel_status, 'decoder_type', 'hw')
            else:
                self._pipeline_info.hwaccel = 'cpu'
            # Fallback reason
            reason = getattr(hwaccel_status, 'fallback_reason', None)
            if reason and reason != 'none':
                self._pipeline_info.fallback_reason = str(reason)
        else:
            self._pipeline_info.hwaccel = 'cpu'

    def set_source_type(
        self,
        source_type: str,
        frame_path: str = 'unknown',
    ) -> None:
        """
        设置源类型和帧路径。

        Args:
            source_type: disk_proxy/memory_proxy/master_only
            frame_path: planar_yuv_gpu/packed_rgb_u8/packed_rgb_f16
        """
        self._pipeline_info.source_type = source_type
        self._pipeline_info.frame_path = frame_path

    def update_from_monitor(
        self,
        monitor: Any,  # VideoPerformanceMonitor
    ) -> None:
        """
        从性能监控器更新数据。

        P5-1: 使用新的完整百分位统计。

        Args:
            monitor: VideoPerformanceMonitor 实例
        """
        if monitor is None:
            return

        # 获取聚合统计（P5-1: 包含完整百分位）
        stats = monitor.get_aggregate_stats()

        # 更新 decode 统计（使用新方法）
        self._perf_stats.decode_ms = PercentileStats(
            p50=stats.get('decode_p50_ms', 0.0),
            p95=stats.get('decode_p95_ms', 0.0),
            max=stats.get('decode_max_ms', 0.0),
            avg=stats.get('decode_avg_ms', 0.0),
            count=stats.get('frames_processed', 0),
        )

        # 更新 upload 统计
        self._perf_stats.upload_ms = PercentileStats(
            p50=stats.get('upload_p50_ms', 0.0),
            p95=stats.get('upload_p95_ms', 0.0),
            max=stats.get('upload_max_ms', 0.0),
            avg=stats.get('upload_avg_ms', 0.0),
            count=stats.get('frames_processed', 0),
        )

        # 更新 tick 统计
        self._perf_stats.ui_tick_ms = PercentileStats(
            p50=stats.get('tick_p50_ms', 0.0),
            p95=stats.get('tick_p95_ms', 0.0),
            max=stats.get('tick_max_ms', 0.0),
            avg=stats.get('tick_avg_ms', 0.0),
            count=stats.get('tick_count', 0),
        )

        # 更新队列深度
        self._perf_stats.queue_depth = PercentileStats(
            p50=stats.get('queue_p50_depth', 0.0),
            p95=stats.get('queue_p95_depth', 0.0),
            max=stats.get('queue_max_depth', 0.0),
            avg=stats.get('queue_avg_depth', 0.0),
            count=stats.get('tick_count', 0),
        )

        # 更新 render 统计
        self._perf_stats.render_ms = PercentileStats(
            p50=stats.get('render_p50_ms', 0.0),
            p95=stats.get('render_p95_ms', 0.0),
            max=stats.get('render_max_ms', 0.0),
            avg=stats.get('render_avg_ms', 0.0),
            count=stats.get('frames_processed', 0),
        )

        # 更新帧统计
        self._perf_stats.frames_displayed = stats.get('frames_processed', 0)
        self._perf_stats.frames_dropped = stats.get('dropped_frames', 0)
        self._perf_stats.frames_held = int(stats.get('held_frames', 0))
        self._perf_stats.cache_hit_rate = stats.get('cache_hit_rate', 0.0)
        self._perf_stats.degradation_level = stats.get('degradation_level', 0)

        # P2.2: 代理阶梯状态
        self._perf_stats.proxy_ladder_index = stats.get('proxy_ladder_index', -1)
        self._perf_stats.proxy_long_edge = stats.get('proxy_long_edge', 0)
        self._perf_stats.original_long_edge = stats.get('original_long_edge', 0)

        # Phase A: 零拷贝追踪
        zero_copy_frames = stats.get('zero_copy_frames', 0)
        cpu_download_frames = stats.get('cpu_download_frames', 0)
        cpu_upload_frames = stats.get('cpu_upload_frames', 0)
        external_texture_frames = stats.get('external_texture_frames', 0)

        self._pipeline_info.zero_copy_on = zero_copy_frames > 0 and cpu_download_frames == 0
        self._pipeline_info.cpu_download_frames = cpu_download_frames
        self._pipeline_info.cpu_upload_frames = cpu_upload_frames
        self._pipeline_info.external_texture_frames = external_texture_frames

        decode_path_counts = stats.get('decode_path_counts', {})
        if decode_path_counts:
            dominant_decode = max(decode_path_counts, key=decode_path_counts.get)
            self._pipeline_info.decode_path = dominant_decode
        else:
            self._pipeline_info.decode_path = "unknown"

        # 更新时长
        self._perf_stats.session_duration = time.time() - self._session_start_time

        self._update_time = time.time()

    def update_from_session(
        self,
        session: Any,  # VideoSession
    ) -> None:
        """
        从视频会话更新数据。

        Args:
            session: VideoSession 实例
        """
        if session is None:
            return

        # 更新帧率和帧数
        stats = session.stats
        self._perf_stats.frames_displayed = stats.total_frames_played
        self._perf_stats.frames_dropped = stats.total_frames_dropped
        self._perf_stats.fps = stats.current_fps

        # 更新时长
        self._perf_stats.session_duration = stats.total_play_time

    def update_fps(self, fps: float) -> None:
        """更新当前帧率"""
        self._perf_stats.fps = fps

    def update_frame_stats(
        self,
        displayed: int,
        dropped: int,
        held: int = 0,
    ) -> None:
        """更新帧统计"""
        self._perf_stats.frames_displayed = displayed
        self._perf_stats.frames_dropped = dropped
        self._perf_stats.frames_held = held

    def record_sample(
        self,
        decode_ms: Optional[float] = None,
        upload_ms: Optional[float] = None,
        render_ms: Optional[float] = None,
        tick_ms: Optional[float] = None,
        queue_depth: Optional[int] = None,
    ) -> None:
        """
        记录单个样本。

        用于手动记录性能数据。

        Args:
            decode_ms: 解码耗时
            upload_ms: 上传耗时
            render_ms: 渲染耗时
            tick_ms: tick 耗时
            queue_depth: 队列深度
        """
        if decode_ms is not None:
            self._decode_times.append(decode_ms)

        if upload_ms is not None:
            self._upload_times.append(upload_ms)

        if render_ms is not None:
            self._render_times.append(render_ms)

        if tick_ms is not None:
            self._tick_times.append(tick_ms)

        if queue_depth is not None:
            self._queue_depths.append(queue_depth)

        # AUD-38007: 同步写入 _history（导出用），受 _max_history 上限约束，
        # 丢弃最旧的整条样本记录。
        sample: Dict[str, Any] = {'timestamp': datetime.now().isoformat()}
        if decode_ms is not None:
            sample['decode_ms'] = decode_ms
        if upload_ms is not None:
            sample['upload_ms'] = upload_ms
        if render_ms is not None:
            sample['render_ms'] = render_ms
        if tick_ms is not None:
            sample['tick_ms'] = tick_ms
        if queue_depth is not None:
            sample['queue_depth'] = queue_depth
        self._history.append(sample)
        overflow = len(self._history) - self._max_history
        if overflow > 0:
            del self._history[:overflow]

        # 更新百分位统计
        self._perf_stats.decode_ms = self._calculate_percentiles(self._decode_times)
        self._perf_stats.upload_ms = self._calculate_percentiles(self._upload_times)
        self._perf_stats.render_ms = self._calculate_percentiles(self._render_times)
        self._perf_stats.ui_tick_ms = self._calculate_percentiles(self._tick_times)
        self._perf_stats.queue_depth = self._calculate_int_percentiles(self._queue_depths)

    def _calculate_percentiles(
        self,
        values: Sequence[float],
    ) -> PercentileStats:
        """
        计算百分位统计。

        Args:
            values: 数值列表

        Returns:
            PercentileStats
        """
        if not values:
            return PercentileStats()

        sorted_vals = sorted(values)
        count = len(sorted_vals)

        # 计算百分位索引
        p50_idx = int(count * 0.50)
        p95_idx = int(count * 0.95)

        # 确保索引在有效范围内
        p50_idx = min(p50_idx, count - 1)
        p95_idx = min(p95_idx, count - 1)

        return PercentileStats(
            p50=sorted_vals[p50_idx],
            p95=sorted_vals[p95_idx],
            max=sorted_vals[-1],
            avg=sum(values) / count,
            count=count,
        )

    def _calculate_int_percentiles(
        self,
        values: Sequence[int],
    ) -> PercentileStats:
        """
        计算整数百分位统计。

        Args:
            values: 整数列表

        Returns:
            PercentileStats（值转换为 float）
        """
        float_vals = [float(v) for v in values]
        return self._calculate_percentiles(float_vals)

    def get_full_report(self) -> Dict[str, Any]:
        """
        获取完整报告。

        用于导出日志。

        Returns:
            包含所有信息的字典
        """
        return {
            'timestamp': datetime.now().isoformat(),
            'video_path': self._video_path or 'unknown',
            'pipeline_info': self._pipeline_info.to_dict(),
            'performance_stats': self._perf_stats.to_dict(),
        }

    def export_log(
        self,
        output_path: Optional[str] = None,
    ) -> str:
        """
        导出性能日志。

        Args:
            output_path: 输出路径（可选，默认使用临时目录）

        Returns:
            导出文件路径
        """
        if output_path is None:
            # 使用临时目录
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = str(
                Path.home() / 'Documents' / f'playback_perf_{timestamp}.json'
            )

        report = self.get_full_report()

        # 添加历史记录
        if self._history:
            report['history'] = self._history[-100:]  # 最近 100 条

        # 写入文件
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False)

            logger.info(f"Playback log exported to: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"Failed to export log: {e}")
            return ""


# =============================================================================
# PlaybackHUDPanel: UI 面板
# =============================================================================

class PlaybackHUDPanel(QWidget):
    """
    播放性能监控 HUD 面板。

    实时显示视频播放管线的关键性能指标。

    Features:
    - 管线信息显示（pipeline/backend/codec/...）
    - 性能统计显示（p50/p95/max）
    - 帧统计显示（displayed/dropped/held）
    - 导出日志功能
    - 可隐藏/显示
    - 可配置显示项

    Signals:
        export_requested: 请求导出日志
        visibility_changed: 可见性变化
    """

    export_requested = pyqtSignal()
    visibility_changed = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化 HUD 面板"""
        super().__init__(parent)

        self._model: PlaybackHUDModel = PlaybackHUDModel()
        self._visible: bool = False
        self._update_timer: QTimer = QTimer(self)
        self._update_interval: int = 500  # 500ms 更新一次
        self._labels: Dict[str, QLabel] = {}

        self._setup_ui()
        self._setup_timer()

        # 默认隐藏
        self.setVisible(False)

        logger.debug("PlaybackHUDPanel initialized")

    @property
    def model(self) -> PlaybackHUDModel:
        """获取数据模型"""
        return self._model

    def _setup_ui(self) -> None:
        """设置 UI"""
        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(4)

        # 标题栏
        title_layout = QHBoxLayout()
        title_label = QLabel(I18N.tr("hud.title", "播放性能监控"))
        title_label.setStyleSheet("""
            color: #fff;
            font-size: 12px;
            font-weight: bold;
        """)

        # 导出按钮
        export_btn = QPushButton(I18N.tr("hud.export", "导出日志"))
        export_btn.setStyleSheet("""
            QPushButton {
                background: #3a3a3a;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 2px 8px;
                color: #ddd;
                font-size: 11px;
            }
            QPushButton:hover {
                background: #4a4a4a;
            }
        """)
        export_btn.clicked.connect(self._on_export_clicked)

        # 关闭按钮
        close_btn = QPushButton("X")
        close_btn.setStyleSheet("""
            QPushButton {
                background: #3a3a3a;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 2px 6px;
                color: #ddd;
                font-size: 11px;
            }
            QPushButton:hover {
                background: #5a5a5a;
            }
        """)
        # AUD-38003: 关闭按钮走 hide_hud（而非直连 QWidget.hide）——
        # 直连会绕过状态同步：_visible 残留 True、更新定时器不停、
        # visibility_changed 不发射，之后 toggle_visibility 语义反转。
        close_btn.clicked.connect(self.hide_hud)

        title_layout.addWidget(title_label)
        title_layout.addStretch()
        title_layout.addWidget(export_btn)
        title_layout.addWidget(close_btn)

        main_layout.addLayout(title_layout)

        # 内容区域（滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        content_widget = QWidget()
        content_widget.setStyleSheet("background: #27272b;")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(8, 8, 8, 8)
        content_layout.setSpacing(3)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 创建显示项
        display_items = [
            # 管线信息组
            ("group_pipeline", I18N.tr("hud.group_pipeline", "管线信息")),
            ("pipeline", I18N.tr("hud.pipeline", "管线版本")),
            ("backend", I18N.tr("hud.backend", "后端")),
            ("codec", I18N.tr("hud.codec", "编码")),
            ("pixel_format", I18N.tr("hud.pixel_format", "像素格式")),
            ("source_type", I18N.tr("hud.source_type", "源类型")),
            ("proxy_size", I18N.tr("hud.proxy_size", "代理分辨率")),
            ("frame_path", I18N.tr("hud.frame_path", "帧路径")),
            ("hwaccel", I18N.tr("hud.hwaccel", "硬件加速")),
            ("fallback_reason", I18N.tr("hud.fallback_reason", "降级原因")),
            # Phase A: 零拷贝追踪
            ("decode_path", I18N.tr("hud.decode_path", "解码路径")),
            ("zero_copy_on", I18N.tr("hud.zero_copy", "零拷贝")),
            ("cpu_download_frames", I18N.tr("hud.cpu_download", "CPU下载帧")),
            ("cpu_upload_frames", I18N.tr("hud.cpu_upload", "CPU上传帧")),
            ("external_texture_frames", I18N.tr("hud.ext_tex", "外贴图帧")),

            # 性能统计组
            ("group_performance", I18N.tr("hud.group_performance", "性能统计")),
            ("decode_ms", I18N.tr("hud.decode_ms", "解码耗时")),
            ("upload_ms", I18N.tr("hud.upload_ms", "上传耗时")),
            ("render_ms", I18N.tr("hud.render_ms", "渲染耗时")),
            ("ui_tick_ms", I18N.tr("hud.ui_tick_ms", "UI Tick")),
            ("queue_depth", I18N.tr("hud.queue_depth", "队列深度")),

            # 帧统计组
            ("group_frames", I18N.tr("hud.group_frames", "帧统计")),
            ("frames_displayed", I18N.tr("hud.frames_displayed", "显示帧数")),
            ("frames_dropped", I18N.tr("hud.frames_dropped", "丢帧数")),
            ("frames_held", I18N.tr("hud.frames_held", "持有帧数")),
            ("fps", I18N.tr("hud.fps", "当前帧率")),
            ("cache_hit_rate", I18N.tr("hud.cache_hit_rate", "缓存命中率")),
            ("session_duration", I18N.tr("hud.session_duration", "会话时长")),
            ("degradation_level", I18N.tr("hud.degradation_level", "降级级别")),
            ("proxy_ladder", I18N.tr("hud.proxy_ladder", "代理阶梯")),
        ]

        for key, label_text in display_items:
            if key.startswith("group_"):
                # 分组标题
                group_label = QLabel(label_text)
                group_label.setStyleSheet("""
                    color: #5a8a5a;
                    font-size: 11px;
                    font-weight: bold;
                    background: transparent;
                    margin-top: 4px;
                """)
                content_layout.addWidget(group_label)
            else:
                # 数据行
                row = QWidget()
                row.setStyleSheet("background: transparent;")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 1, 0, 1)

                label = QLabel(label_text)
                label.setStyleSheet("""
                    color: #888;
                    font-size: 10px;
                    background: transparent;
                """)

                value = QLabel("-")
                value.setStyleSheet("""
                    color: #ddd;
                    font-size: 10px;
                    font-family: 'Menlo', 'Monaco', 'Consolas', monospace;
                    background: transparent;
                """)
                value.setAlignment(Qt.AlignmentFlag.AlignRight)

                row_layout.addWidget(label)
                row_layout.addStretch()
                row_layout.addWidget(value)

                content_layout.addWidget(row)
                self._labels[key] = value

        scroll.setWidget(content_widget)
        main_layout.addWidget(scroll)

        # 设置面板样式
        self.setStyleSheet("""
            PlaybackHUDPanel {
                background: #27272b;
                border: 1px solid #444;
                border-radius: 4px;
            }
        """)

        # 设置大小
        self.setMinimumWidth(320)
        self.setMaximumWidth(400)
        self.setMinimumHeight(200)

    def _setup_timer(self) -> None:
        """设置更新定时器"""
        self._update_timer.timeout.connect(self._update_display)
        self._update_timer.setInterval(self._update_interval)

    def toggle_visibility(self) -> None:
        """切换可见性"""
        self._visible = not self._visible
        self.setVisible(self._visible)
        self.visibility_changed.emit(self._visible)

        if self._visible:
            self._update_timer.start()
        else:
            self._update_timer.stop()

    def show_hud(self) -> None:
        """显示 HUD"""
        self._visible = True
        self.setVisible(True)
        self.visibility_changed.emit(True)
        self._update_timer.start()

    def hide_hud(self) -> None:
        """隐藏 HUD"""
        self._visible = False
        self.setVisible(False)
        self.visibility_changed.emit(False)
        self._update_timer.stop()

    def is_visible(self) -> bool:
        """检查是否可见"""
        return self._visible

    def set_update_interval(self, interval_ms: int) -> None:
        """
        设置更新间隔。

        Args:
            interval_ms: 更新间隔（毫秒），最小 100ms
        """
        self._update_interval = max(100, interval_ms)
        self._update_timer.setInterval(self._update_interval)

    def start_session(
        self,
        video_path: str,
        metadata: Any,
        pipeline_decision: Any,
        hwaccel_status: Any = None,
    ) -> None:
        """
        开始新播放会话。

        Args:
            video_path: 视频文件路径
            metadata: 视频元数据
            pipeline_decision: 管线决策
            hwaccel_status: 硬件解码状态
        """
        self._model.start_session(
            video_path,
            metadata,
            pipeline_decision,
            hwaccel_status,
        )
        self._update_display()

    def end_session(self) -> None:
        """结束播放会话"""
        # 可以在这里自动导出日志
        pass

    def update_from_monitor(self, monitor: Any) -> None:
        """
        从性能监控器更新数据。

        Args:
            monitor: VideoPerformanceMonitor 实例
        """
        self._model.update_from_monitor(monitor)

    def update_from_session(self, session: Any) -> None:
        """
        从视频会话更新数据。

        Args:
            session: VideoSession 实例
        """
        self._model.update_from_session(session)

    def _update_display(self) -> None:
        """更新显示"""
        pipeline_info = self._model.pipeline_info
        perf_stats = self._model.perf_stats

        # 更新管线信息
        self._update_label("pipeline", pipeline_info.pipeline)
        self._update_label("backend", pipeline_info.backend)
        self._update_label("codec", pipeline_info.codec)
        self._update_label("pixel_format", pipeline_info.pixel_format)
        self._update_label("source_type", pipeline_info.source_type)
        self._update_label("proxy_size", pipeline_info.proxy_size)
        self._update_label("frame_path", pipeline_info.frame_path)
        self._update_label("hwaccel", pipeline_info.hwaccel)
        self._update_label(
            "fallback_reason",
            pipeline_info.fallback_reason or "none"
        )
        self._update_label("decode_path", pipeline_info.decode_path)
        self._update_label(
            "zero_copy_on", "ON" if pipeline_info.zero_copy_on else "OFF"
        )
        self._update_label(
            "cpu_download_frames", str(pipeline_info.cpu_download_frames)
        )
        self._update_label(
            "cpu_upload_frames", str(pipeline_info.cpu_upload_frames)
        )
        self._update_label(
            "external_texture_frames", str(pipeline_info.external_texture_frames)
        )

        # 更新性能统计
        self._update_label("decode_ms", perf_stats.decode_ms.format_display())
        self._update_label("upload_ms", perf_stats.upload_ms.format_display())
        self._update_label("render_ms", perf_stats.render_ms.format_display())
        self._update_label("ui_tick_ms", perf_stats.ui_tick_ms.format_display())
        self._update_label(
            "queue_depth",
            f"p95={perf_stats.queue_depth.p95:.0f}"
        )

        # 更新帧统计
        self._update_label("frames_displayed", str(perf_stats.frames_displayed))
        self._update_label("frames_dropped", str(perf_stats.frames_dropped))
        self._update_label("frames_held", str(perf_stats.frames_held))
        self._update_label("fps", f"{perf_stats.fps:.1f}")
        self._update_label(
            "cache_hit_rate",
            f"{perf_stats.cache_hit_rate:.1%}"
        )
        self._update_label(
            "session_duration",
            f"{perf_stats.session_duration:.1f}s"
        )
        self._update_label(
            "degradation_level",
            str(perf_stats.degradation_level)
        )

        # P2.2: 代理阶梯显示
        ladder_index = perf_stats.proxy_ladder_index
        proxy_long_edge = perf_stats.proxy_long_edge
        original_long_edge = perf_stats.original_long_edge
        if original_long_edge > 0 and proxy_long_edge > 0:
            if proxy_long_edge >= original_long_edge:
                ladder_text = "original"
            else:
                ladder_text = f"{proxy_long_edge}p(idx={ladder_index})"
        else:
            ladder_text = "-"
        self._update_label("proxy_ladder", ladder_text)

    def _update_label(self, key: str, value: str) -> None:
        """
        更新标签值。

        Args:
            key: 标签键名
            value: 显示值
        """
        label = self._labels.get(key)
        if label:
            label.setText(value)

    def _on_export_clicked(self) -> None:
        """导出按钮点击"""
        output_path = self._model.export_log()
        if output_path:
            self.export_requested.emit()
            # 显示成功消息
            QApplication.instance().clipboard().setText(output_path)
            logger.info(f"Log exported: {output_path}")

    def export_log(self, output_path: Optional[str] = None) -> str:
        """
        导出性能日志。

        Args:
            output_path: 输出路径（可选）

        Returns:
            导出文件路径
        """
        return self._model.export_log(output_path)


# =============================================================================
# 工具函数
# =============================================================================

def create_playback_hud_panel(
    parent: Optional[QWidget] = None,
    auto_show: bool = False,
) -> PlaybackHUDPanel:
    """
    创建播放 HUD 面板。

    Args:
        parent: 父控件
        auto_show: 是否自动显示

    Returns:
        PlaybackHUDPanel 实例
    """
    panel = PlaybackHUDPanel(parent)

    if auto_show:
        panel.show_hud()

    return panel


# =============================================================================
# 测试入口
# =============================================================================

if __name__ == '__main__':
    import sys

    app = QApplication(sys.argv)

    panel = PlaybackHUDPanel()
    panel.show_hud()

    # 测试数据
    panel.start_session(
        video_path='/test/video.mp4',
        metadata=None,
        pipeline_decision=None,
    )

    # 模拟更新
    panel.model.record_sample(
        decode_ms=15.5,
        upload_ms=2.3,
        render_ms=1.8,
        tick_ms=16.7,
        queue_depth=8,
    )
    panel._update_display()

    sys.exit(app.exec())