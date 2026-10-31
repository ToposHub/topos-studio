"""
Video Context Module
Composite 系统的视频上下文管理。

## 职责（P7.A + P7.B）

- VideoContext: Composite 视频上下文管理器
- TimelineContext: 时间线上下文信息
- RenderQuality: 渲染质量级别

## 设计原则

- Composite 节点通过 VideoContext 访问 VideoFrameProviderService
- TimelineContext 提供时间线帧号、fps、播放质量信息
- VideoFrameNode 支持 source in/out、time offset、speed、loop/hold
- 所有 composite node evaluate 都能拿到 timeline frame

## 使用示例

```python
from src.features.composite.core.video_context import VideoContext, TimelineContext, RenderQuality

# 获取全局视频上下文
video_ctx = VideoContext.get_instance()

# 打开剪辑（节点调用）
clip_id = video_ctx.open_clip('/path/to/video.mp4')

# 获取当前时间线上下文
timeline = video_ctx.get_timeline_context()

# 请求帧（使用时间线帧号）
frame = video_ctx.request_frame_for_node(
    clip_id=clip_id,
    source_in=0,
    source_out=100,
    timeline_frame=timeline.current_frame,
    fps=timeline.fps,
    quality=RenderQuality.PREVIEW
)

# 导出时切换为高质量
video_ctx.set_render_quality(RenderQuality.EXPORT)
master_frame = video_ctx.request_frame_for_node(...)

# 关闭剪辑
video_ctx.close_clip(clip_id)

# 导出范围验证
timeline.validate_export_range(start_frame=0, end_frame=100)
```

## 与 VideoFrameProviderService 的关系

VideoContext 是 VideoFrameProviderService 的 Composite 专用封装：
- 提供节点级别的帧请求接口（支持 source in/out、time offset）
- 管理 TimelineContext（时间线帧号、fps）
- 管理渲染质量（preview vs export）

## 架构位置

```text
┌────────────────────────────────────────────────────────────────────┐
│ Composite NodeGraph                                                 │
│ - VideoFrameNode 通过 VideoContext 获取帧                           │
│ - 其他节点也能访问 TimelineContext                                   │
└───────────────────────────────┬────────────────────────────────────┘
                                │ request_frame_for_node
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│ VideoContext                                                        │
│ - timeline_frame 映射（source in/out、time offset、speed）           │
│ - RenderQuality 管理（preview/export）                               │
│ - 剪辑生命周期管理                                                    │
└───────────────┬────────────────────┬───────────────────────┬───────┘
                │                    │                       │
                ▼                    ▼                       ▼
┌──────────────────┐   ┌───────────────┐   ┌──────────────────────────┐
│ TimelineContext   │   │ RenderQuality │   │ VideoFrameProviderService│
│ - current_frame   │   │ - PREVIEW     │   │ - clip_id 管理           │
│ - fps             │   │ - EXPORT      │   │ - proxy/master 分离      │
│ - playback_quality│   │               │   │ - 帧缓存                 │
└──────────────────┘   └───────────────┘   └──────────────────────────┘
```

Note:
    Composite 从第 100 帧预览时，VideoFrameNode 能请求正确源帧
    导出范围第一帧不偏帧
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Optional, Dict, Tuple, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from src.shared.media.frame import DecodedFrame
    from src.shared.video.frame_provider_service import (
        VideoFrameProviderService,
        ClipInfo,
    )

logger = logging.getLogger(__name__)


# =============================================================================
# 渲染质量级别
# =============================================================================

class RenderQuality(Enum):
    """
    渲染质量级别。

    Values:
        PREVIEW: 预览质量（使用 proxy）
        EXPORT: 导出质量（使用 master）
        AUTO: 自动选择（根据 render_mode 决定）
    """
    PREVIEW = auto()
    EXPORT = auto()
    AUTO = auto()

    def to_frame_quality(self) -> str:
        """
        转换为 FrameQuality 字符串。

        Returns:
            'proxy' 或 'master'
        """
        if self == RenderQuality.EXPORT:
            return 'master'
        else:
            return 'proxy'


# =============================================================================
# 时间线上下文
# =============================================================================

@dataclass
class TimelineContext:
    """
    时间线上下文信息。

    P7.B: 定义 TimelineContext，所有 composite node evaluate 都能拿到。

    Attributes:
        current_frame: 当前时间线帧号（0-based）
        fps: 时间线帧率
        time_seconds: 当前时间（秒）
        playback_quality: 播放质量（'proxy' 或 'master'）
        render_quality: 渲染质量（RenderQuality）
        start_frame: 时间线起始帧（用于导出范围）
        end_frame: 时间线结束帧（用于导出范围）
        is_playing: 是否正在播放
        is_exporting: 是否正在导出
    """

    current_frame: int = 0
    fps: float = 24.0
    time_seconds: float = 0.0
    playback_quality: str = 'proxy'
    render_quality: RenderQuality = RenderQuality.PREVIEW
    start_frame: int = 0
    end_frame: int = -1  # -1 表示未定义
    is_playing: bool = False
    is_exporting: bool = False

    def update_frame(self, frame: int) -> None:
        """
        更新当前帧。

        Args:
            frame: 新的帧号
        """
        self.current_frame = frame
        if self.fps > 0:
            self.time_seconds = frame / self.fps

    def update_time(self, seconds: float) -> None:
        """
        更新当前时间。

        Args:
            seconds: 新的时间（秒）
        """
        self.time_seconds = seconds
        if self.fps > 0:
            self.current_frame = int(seconds * self.fps)

    def is_in_export_range(self, frame: int) -> bool:
        """
        检查帧是否在导出范围内。

        Args:
            frame: 帧号

        Returns:
            True 表示在范围内
        """
        if self.end_frame < 0:
            return True  # 未定义范围，默认全部有效
        return self.start_frame <= frame <= self.end_frame

    def validate_export_range(
        self,
        start_frame: Optional[int] = None,
        end_frame: Optional[int] = None
    ) -> Tuple[int, int]:
        """
        验证导出范围。

        Args:
            start_frame: 开始帧（可选，默认使用 self.start_frame）
            end_frame: 结束帧（可选，默认使用 self.end_frame）

        Returns:
            (validated_start, validated_end)

        Raises:
            ValueError: 范围无效

        Note:
            导出范围第一帧不偏帧
        """
        start = start_frame if start_frame is not None else self.start_frame
        end = end_frame if end_frame is not None else self.end_frame

        if end < 0:
            raise ValueError("Export end frame must be defined")

        if start > end:
            raise ValueError(f"Export start ({start}) > end ({end})")

        if start < 0:
            start = 0

        return (start, end)

    def get_quality_for_mode(self, render_mode: str = 'preview') -> str:
        """
        根据渲染模式获取帧质量。

        Args:
            render_mode: 渲染模式 ('preview', 'export')

        Returns:
            'proxy' 或 'master'
        """
        if render_mode == 'export':
            return 'master'
        elif self.render_quality == RenderQuality.EXPORT:
            return 'master'
        else:
            return 'proxy'

    def to_dict(self) -> Dict[str, int | float | str | bool]:
        """
        转换为字典。

        Returns:
            字典表示
        """
        return {
            'current_frame': self.current_frame,
            'fps': self.fps,
            'time_seconds': self.time_seconds,
            'playback_quality': self.playback_quality,
            'render_quality': self.render_quality.name,
            'start_frame': self.start_frame,
            'end_frame': self.end_frame,
            'is_playing': self.is_playing,
            'is_exporting': self.is_exporting,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, int | float | str | bool]) -> TimelineContext:
        """
        从字典创建。

        Args:
            data: 字典数据

        Returns:
            TimelineContext 实例
        """
        render_quality_str = data.get('render_quality', 'PREVIEW')
        render_quality = RenderQuality.PREVIEW
        try:
            render_quality = RenderQuality[render_quality_str.upper()]
        except KeyError:
            pass

        return cls(
            current_frame=int(data.get('current_frame', 0)),
            fps=float(data.get('fps', 24.0)),
            time_seconds=float(data.get('time_seconds', 0.0)),
            playback_quality=str(data.get('playback_quality', 'proxy')),
            render_quality=render_quality,
            start_frame=int(data.get('start_frame', 0)),
            end_frame=int(data.get('end_frame', -1)),
            is_playing=bool(data.get('is_playing', False)),
            is_exporting=bool(data.get('is_exporting', False)),
        )


# =============================================================================
# VideoContext: Composite 视频上下文管理器
# =============================================================================

class VideoContext:
    """
    Composite 视频上下文管理器。

    实现 P7.A + P7.B 的 VideoContext 设计：
    - Composite 节点通过 VideoContext 访问 VideoFrameProviderService
    - TimelineContext 提供时间线帧号、fps、播放质量信息
    - VideoFrameNode 支持 source in/out、time offset、speed、loop/hold
    - 所有 composite node evaluate 都能拿到 timeline frame

    Attributes:
        _service: VideoFrameProviderService 实例
        _timeline: TimelineContext 实例
        _node_clips: node_id -> clip_id 映射
        _clip_params: clip_id -> VideoNodeParams 映射
        _lock: 线程锁

    Note:
        Composite 从第 100 帧预览时，VideoFrameNode 能请求正确源帧
        导出范围第一帧不偏帧
    """

    # 全局实例（单例）
    _INSTANCE: Optional[VideoContext] = None
    _INSTANCE_LOCK: threading.Lock = threading.Lock()

    def __init__(
        self,
        service: Optional[VideoFrameProviderService] = None,
    ) -> None:
        """
        初始化视频上下文。

        Args:
            service: VideoFrameProviderService 实例（可选，默认使用全局）
        """
        if service is None:
            from src.shared.video.frame_provider_service import get_frame_provider_service
            service = get_frame_provider_service()

        self._service: VideoFrameProviderService = service
        self._timeline: TimelineContext = TimelineContext()
        self._node_clips: Dict[str, str] = {}  # node_id -> clip_id
        self._clip_params: Dict[str, VideoNodeParams] = {}  # clip_id -> params
        self._lock: threading.Lock = threading.Lock()

        # 异步帧就绪回调：VideoFrameNode 后台解码完成当前帧后通过它通知
        # 页面重新求值（合成页注册 Qt 信号 emit，跨线程经队列连接回到 UI 线程）。
        self._frame_ready_handler: Optional[Callable[[], None]] = None

        logger.debug("VideoContext initialized")

    def set_frame_ready_handler(self, handler: Optional[Callable[[], None]]) -> None:
        """注册/注销异步帧就绪回调（合成页在初始化时注册）。"""
        with self._lock:
            self._frame_ready_handler = handler

    def clear_frame_ready_handler(self, handler: Callable[[], None]) -> None:
        """注销帧就绪回调（仅当仍指向 handler 时）。

        T-P1-71：合成页在 destroyed 时调用，防止后续视频解码完成仍向
        已销毁页面投递通知。身份比较保证不误清新页面注册的 handler。
        """
        with self._lock:
            if self._frame_ready_handler is handler:
                self._frame_ready_handler = None

    def notify_frame_ready(self) -> None:
        """通知"某个视频节点的当前帧已在后台解码完成"（工作线程调用安全）。"""
        with self._lock:
            handler = self._frame_ready_handler
        if handler is None:
            return
        try:
            handler()
        except Exception as e:
            # 典型场景：页面已销毁而 worker 仍在收尾。吞掉并降级为调试日志。
            logger.debug(f"VideoContext frame-ready handler failed: {e}")

    # =============================================================================
    # 单例访问
    # =============================================================================

    @classmethod
    def get_instance(cls) -> VideoContext:
        """
        获取全局实例。

        Returns:
            VideoContext 实例
        """
        with cls._INSTANCE_LOCK:
            if cls._INSTANCE is None:
                cls._INSTANCE = VideoContext()
            return cls._INSTANCE

    @classmethod
    def reset_instance(cls) -> None:
        """
        重置全局实例。

        用于测试或应用重置。
        """
        with cls._INSTANCE_LOCK:
            if cls._INSTANCE is not None:
                cls._INSTANCE.shutdown()
                cls._INSTANCE = None

    # =============================================================================
    # 剪辑管理
    # =============================================================================

    def open_clip(
        self,
        path: str,
        node_id: str
    ) -> str:
        """
        打开视频剪辑（节点调用）。

        Args:
            path: 视频文件路径
            node_id: 节点 ID（用于追踪剪辑归属）

        Returns:
            clip_id: 剪辑标识

        Note:
            多个节点引用同一文件共享 clip_id
        """
        clip_id = self._service.open_clip(path)

        with self._lock:
            self._node_clips[node_id] = clip_id

            # 初始化节点参数（如果还没有）
            if clip_id not in self._clip_params:
                self._clip_params[clip_id] = VideoNodeParams()

        return clip_id

    def close_clip_for_node(self, node_id: str) -> bool:
        """
        关闭剪辑（节点调用）。

        Args:
            node_id: 节点 ID

        Returns:
            True 表示成功关闭
        """
        with self._lock:
            clip_id = self._node_clips.pop(node_id, None)
            if clip_id is None:
                return False

            # 清理节点参数（如果没有其他节点使用）
            other_nodes_using = [
                nid for nid, cid in self._node_clips.items()
                if cid == clip_id
            ]
            if not other_nodes_using:
                self._clip_params.pop(clip_id, None)

        # 关闭剪辑（减少引用计数）
        return self._service.close_clip(clip_id)

    def get_clip_id_for_node(self, node_id: str) -> Optional[str]:
        """
        获取节点的剪辑 ID。

        Args:
            node_id: 节点 ID

        Returns:
            clip_id 或 None
        """
        with self._lock:
            return self._node_clips.get(node_id)

    def get_clip_info(self, clip_id: str) -> Optional[ClipInfo]:
        """
        获取剪辑信息。

        Args:
            clip_id: 剪辑标识

        Returns:
            ClipInfo 或 None
        """
        return self._service.get_clip_info(clip_id)

    # =============================================================================
    # 时间线管理
    # =============================================================================

    def _timeline_snapshot(self) -> TimelineContext:
        """AUD-5502: 原子快照共享的 timeline 状态。

        _timeline 由 UI 线程写入（set_timeline_frame / set_exporting 等）、
        渲染/解码 worker 线程读取；所有访问必须经 _lock 串行化，双字段写
        （current_frame + time_seconds）与 fps 变更才不会撕裂。
        """
        with self._lock:
            return replace(self._timeline)

    def get_timeline_context(self) -> TimelineContext:
        """
        获取时间线上下文。

        Returns:
            TimelineContext 的防御性副本（AUD-5502：直接暴露共享可变单例
            会让调用方绕过 _lock 写入；副本修改不会影响上下文，请用
            set_timeline_* 系列方法更新）。
        """
        return self._timeline_snapshot()

    def set_timeline_frame(self, frame: int) -> None:
        """
        设置时间线帧号。

        Args:
            frame: 帧号
        """
        with self._lock:
            self._timeline.update_frame(frame)

    def set_timeline_fps(self, fps: float) -> None:
        """
        设置时间线帧率。

        Args:
            fps: 帧率
        """
        with self._lock:
            self._timeline.fps = fps

    def set_timeline_range(
        self,
        start_frame: int,
        end_frame: int
    ) -> None:
        """
        设置时间线范围（导出范围）。

        Args:
            start_frame: 开始帧
            end_frame: 结束帧
        """
        with self._lock:
            self._timeline.start_frame = start_frame
            self._timeline.end_frame = end_frame

    def set_exporting(self, is_exporting: bool) -> None:
        """
        设置导出状态。

        Args:
            is_exporting: 是否正在导出
        """
        # AUD-5502: 质量翻转与请求路径的读取用同一把锁串行化，避免导出
        # 期间 proxy/master 混取。
        with self._lock:
            self._timeline.is_exporting = is_exporting
            if is_exporting:
                self._timeline.render_quality = RenderQuality.EXPORT
            else:
                self._timeline.render_quality = RenderQuality.PREVIEW

    # =============================================================================
    # 渲染质量管理
    # =============================================================================

    def set_render_quality(self, quality: RenderQuality) -> None:
        """
        设置渲染质量。

        Args:
            quality: 渲染质量级别
        """
        # AUD-5502: 与请求路径的读取共用 _lock。
        with self._lock:
            self._timeline.render_quality = quality

    def get_render_quality(self) -> RenderQuality:
        """
        获取渲染质量。

        Returns:
            渲染质量级别
        """
        with self._lock:
            return self._timeline.render_quality

    def get_quality_for_render_mode(self, render_mode: str = 'preview') -> str:
        """
        根据渲染模式获取帧质量字符串。

        Args:
            render_mode: 渲染模式 ('preview', 'export')

        Returns:
            'proxy' 或 'master'

        Note:
            Composite 预览用 proxy quality，最终导出用 master quality
        """
        # AUD-5502: 基于锁内快照计算，避免与 set_exporting 的质量翻转
        # 竞争（导出中混取 proxy/master）。
        snapshot = self._timeline_snapshot()
        return snapshot.get_quality_for_mode(render_mode)

    # =============================================================================
    # 帧请求
    # =============================================================================

    def request_frame_for_node(
        self,
        clip_id: str,
        source_in: int = 0,
        source_out: int = -1,
        timeline_frame: Optional[int] = None,
        fps: Optional[float] = None,
        time_offset: float = 0.0,
        speed: float = 1.0,
        loop: bool = False,
        hold: bool = False,
        quality: Optional[str] = None,
        render_mode: str = 'preview'
    ) -> Optional[DecodedFrame]:
        """
        请求帧（节点专用接口）。

        支持 source in/out、time offset、speed、loop/hold 参数。

        Args:
            clip_id: 剪辑标识
            source_in: 源起始帧（0-based）
            source_out: 源结束帧（-1 表示使用全部）
            timeline_frame: 时间线帧号（可选，默认使用当前时间线）
            fps: 帧率（可选，默认使用剪辑帧率）
            time_offset: 时间偏移（秒）
            speed: 播放速度倍数
            loop: 是否循环播放
            hold: 是否保持最后一帧（超出范围时）
            quality: 质量级别（可选，默认根据 render_mode 选择）
            render_mode: 渲染模式 ('preview', 'export')

        Returns:
            DecodedFrame 或 None

        Note:
            S4.B: frame_index 语义：
            - timeline_frame 是时间线帧号（由 timeline/context 层管理）
            - 本方法将 timeline_frame 转换为 source_frame（源视频帧号）
            - 转换考虑：source in/out、time offset、speed、loop/hold
            - 最终调用 VideoFrameProviderService.request_frame(clip_id, source_frame)
            - VideoFrameProviderService.request_frame 的 frame_index 是源视频帧号，不做转换

            Composite 从第 100 帧预览时，VideoFrameNode 能请求正确源帧
            导出范围第一帧不偏帧
        """
        # 获取剪辑信息
        clip_info = self._service.get_clip_info(clip_id)
        if clip_info is None:
            logger.warning(f"Clip info not found: {clip_id}")
            return None

        # 确定帧率
        # fps<=0 是未初始化的节点参数默认值（PARAM_DEFS fps=0.0），必须
        # 回退到素材真实帧率：按 0 计算会把所有帧映射到源第 0 帧。
        if fps is None or fps <= 0:
            fps = clip_info.fps

        # 确定时间线帧号（AUD-5502: 锁内读取共享 timeline 状态）
        if timeline_frame is None:
            timeline_frame = self._timeline_snapshot().current_frame

        # 确定质量
        if quality is None:
            quality = self.get_quality_for_render_mode(render_mode)

        # 计算源帧号
        source_frame = self._compute_source_frame(
            timeline_frame=timeline_frame,
            fps=fps,
            source_in=source_in,
            source_out=source_out if source_out >= 0 else clip_info.total_frames - 1,
            time_offset=time_offset,
            speed=speed,
            loop=loop,
            hold=hold,
            total_frames=clip_info.total_frames
        )

        if source_frame < 0:
            return None

        # 请求帧
        return self._service.request_frame(clip_id, source_frame, quality)

    def request_gpu_frame_for_node(
        self,
        clip_id: str,
        source_in: int = 0,
        source_out: int = -1,
        timeline_frame: Optional[int] = None,
        fps: Optional[float] = None,
        time_offset: float = 0.0,
        speed: float = 1.0,
        loop: bool = False,
        hold: bool = False,
        render_mode: str = 'preview',
        interactive: bool = False,
        deadline_s: Optional[float] = None,
        is_stale: Optional[callable] = None,
        coarse: bool = False,
    ):
        """请求 GPU 帧（节点专用接口，macOS VT 零拷贝优先）。

        与 request_frame_for_node 相同的时间线→源帧映射；命中 VT 源时返回
        保留 CVPixelBuffer 的延迟导入 GpuDecodedFrame（deferred_import=
        'macos_vt_pixel_buffer'），由合成渲染线程完成 IOSurface→GL 绑定。
        无 VT 源时回退 CPU request_frame（返回普通 DecodedFrame）。
        interactive=True 走 VT 交互式读取（拖拽/快梭）：coarse=True 为
        关键帧快梭（回拖），False 为 deadline 顺序走（前向短跳）；is_stale
        返回 True 时中止被取代的解码。
        """
        clip_info = self._service.get_clip_info(clip_id)
        if clip_info is None:
            return None

        # fps<=0 是未初始化的节点参数默认值（PARAM_DEFS fps=0.0），必须
        # 回退到素材真实帧率：按 0 计算会把所有帧映射到源第 0 帧。
        if fps is None or fps <= 0:
            fps = clip_info.fps
        if timeline_frame is None:
            # AUD-5502: 锁内读取共享 timeline 状态
            timeline_frame = self._timeline_snapshot().current_frame

        quality = self.get_quality_for_render_mode(render_mode)
        source_frame = self._compute_source_frame(
            timeline_frame=timeline_frame,
            fps=fps,
            source_in=source_in,
            source_out=source_out if source_out >= 0 else clip_info.total_frames - 1,
            time_offset=time_offset,
            speed=speed,
            loop=loop,
            hold=hold,
            total_frames=clip_info.total_frames
        )
        if source_frame < 0:
            return None
        return self._service.request_gpu_frame(
            clip_id, source_frame, quality,
            interactive=interactive, deadline_s=deadline_s,
            is_stale=is_stale, coarse=coarse,
        )

    def has_vt_source_for_node(self, clip_id: str) -> bool:
        """该片段是否挂载了 macOS VT 硬解源（决定 CPU 帧的显示放行策略）。

        VT 挂载时 CPU 结果多为游标越过的瞬时未服务（重试即恢复），节点
        需双重未服务才放行上屏以避免 VT/CPU 颜色混显；未挂载（ProRes 等
        CPU 解码素材）时 CPU 结果是确定性的，应立即放行避免重复解码。
        """
        try:
            return self._service.has_vt_source(clip_id)
        except Exception:
            return False

    def prefetch_vt_sequential_for_node(
        self,
        clip_id: str,
        timeline_frame: int,
        source_in: int = 0,
        source_out: int = -1,
        fps: Optional[float] = None,
        time_offset: float = 0.0,
        speed: float = 1.0,
    ) -> bool:
        """VT 顺序预取（节点专用接口，播放前瞻流水线）。

        与 request_gpu_frame_for_node 相同的源帧映射；仅当目标恰为 VT
        游标下一帧时入 reorder 缓冲（不 seek、不超前 >1 帧）。
        """
        clip_info = self._service.get_clip_info(clip_id)
        if clip_info is None:
            return False
        # fps<=0 是未初始化的节点参数默认值（PARAM_DEFS fps=0.0），必须
        # 回退到素材真实帧率：按 0 计算会把所有帧映射到源第 0 帧。
        if fps is None or fps <= 0:
            fps = clip_info.fps
        source_frame = self._compute_source_frame(
            timeline_frame=timeline_frame,
            fps=fps,
            source_in=source_in,
            source_out=source_out if source_out >= 0 else clip_info.total_frames - 1,
            time_offset=time_offset,
            speed=speed,
            loop=False,
            hold=False,
            total_frames=clip_info.total_frames
        )
        if source_frame < 0:
            return False
        return self._service.prefetch_vt_sequential(clip_id, source_frame)

    def prefetch_for_node(
        self,
        clip_id: str,
        timeline_start: int,
        timeline_end: int,
        source_in: int = 0,
        source_out: int = -1,
        fps: Optional[float] = None,
        time_offset: float = 0.0,
        speed: float = 1.0,
        quality: Optional[str] = None,
        render_mode: str = 'preview'
    ) -> None:
        """
        预取帧范围（节点专用接口）。

        Args:
            clip_id: 剪辑标识
            timeline_start: 时间线起始帧
            timeline_end: 时间线结束帧
            source_in: 源起始帧
            source_out: 源结束帧
            fps: 帧率
            time_offset: 时间偏移
            speed: 播放速度
            quality: 质量级别
            render_mode: 渲染模式
        """
        clip_info = self._service.get_clip_info(clip_id)
        if clip_info is None:
            return

        # fps<=0 是未初始化的节点参数默认值（PARAM_DEFS fps=0.0），必须
        # 回退到素材真实帧率：按 0 计算会把所有帧映射到源第 0 帧。
        if fps is None or fps <= 0:
            fps = clip_info.fps

        if quality is None:
            quality = self.get_quality_for_render_mode(render_mode)

        # 计算源帧范围
        source_start = self._compute_source_frame(
            timeline_frame=timeline_start,
            fps=fps,
            source_in=source_in,
            source_out=source_out if source_out >= 0 else clip_info.total_frames - 1,
            time_offset=time_offset,
            speed=speed,
            loop=False,
            hold=False,
            total_frames=clip_info.total_frames
        )

        source_end = self._compute_source_frame(
            timeline_frame=timeline_end,
            fps=fps,
            source_in=source_in,
            source_out=source_out if source_out >= 0 else clip_info.total_frames - 1,
            time_offset=time_offset,
            speed=speed,
            loop=False,
            hold=False,
            total_frames=clip_info.total_frames
        )

        if source_start < 0 or source_end < 0:
            return

        # 预取
        self._service.prefetch(clip_id, source_start, source_end, quality)

    # =============================================================================
    # 帧号计算
    # =============================================================================

    def _compute_source_frame(
        self,
        timeline_frame: int,
        fps: float,
        source_in: int,
        source_out: int,
        time_offset: float,
        speed: float,
        loop: bool,
        hold: bool,
        total_frames: int
    ) -> int:
        """
        计算源帧号。

        支持 time offset、speed、loop、hold。

        Args:
            timeline_frame: 时间线帧号
            fps: 帧率
            source_in: 源起始帧
            source_out: 源结束帧
            time_offset: 时间偏移（秒）
            speed: 播放速度倍数
            loop: 是否循环
            hold: 是否保持最后一帧
            total_frames: 总帧数

        Returns:
            源帧号（-1 表示无效）
        """
        # fps<=0（未初始化的节点参数默认值）时按恒等映射处理：timeline
        # 帧号直接作为源帧号。此前该分支把所有帧映射到源第 0 帧——VT
        # 游标反复回跳 seek、pending 单槽恒 0、画面停滞（播放卡顿的直接
        # 根因之一）。
        if fps is None or fps <= 0:
            source_frame = int(timeline_frame)
        else:
            # 计算时间线时间
            timeline_time = timeline_frame / fps

            # 加上时间偏移
            source_time = timeline_time + time_offset

            # 乘以速度
            source_time *= speed

            # 转换为帧号（round 而非 int 截断：timeline/fps*fps 的浮点往返在
            # 部分帧号上得到 N-0.0000...1，截断会得到 N-1 —— 例如 201/25*25
            # = 200.999...9。差一帧在 CPU 预览路径肉眼不可见，但 VT 零拷贝的
            # 精确索引匹配会永远拿不到目标帧）
            source_frame = int(round(source_time * fps))

        # 加上 source_in 偏移
        source_frame += source_in

        # 检查范围
        source_duration_frames = source_out - source_in + 1

        # AUD-5501: source_in/out 交叉校验。节点参数只各自限界（source_in
        # >= 0、source_out >= -1），允许 source_out < source_in 的倒置组合；
        # duration <= 0 时 loop 路径的取模会 ZeroDivisionError（prefetch
        # worker 线程直接异常退出）或负数模映射出区间外错误帧。与节点侧
        # ``range_len > 0`` 守卫对齐：区间无效即无有效帧。
        if source_duration_frames <= 0:
            logger.debug(
                "_compute_source_frame: invalid source range "
                "[source_in=%s, source_out=%s] (duration=%s) -> no frame",
                source_in, source_out, source_duration_frames)
            return -1

        if source_frame > source_out:
            if loop:
                # 循环：计算偏移后的帧
                offset = (source_frame - source_out - 1) % source_duration_frames
                source_frame = source_in + offset
            elif hold:
                # 保持最后一帧
                source_frame = source_out
            else:
                # 不循环、不保持：返回无效
                return -1

        elif source_frame < source_in:
            if loop:
                # 循环：计算偏移后的帧
                offset = (source_frame - source_in) % source_duration_frames
                source_frame = source_out - offset
            else:
                # 不循环：返回无效
                return -1

        # Clamp 到有效范围
        if total_frames > 0:
            source_frame = max(0, min(source_frame, total_frames - 1))

        return source_frame

    # =============================================================================
    # 节点参数管理
    # =============================================================================

    def set_node_params(
        self,
        clip_id: str,
        source_in: int = 0,
        source_out: int = -1,
        time_offset: float = 0.0,
        speed: float = 1.0,
        loop: bool = False,
        hold: bool = False
    ) -> None:
        """
        设置节点参数。

        Args:
            clip_id: 剪辑标识
            source_in: 源起始帧
            source_out: 源结束帧
            time_offset: 时间偏移
            speed: 播放速度
            loop: 是否循环
            hold: 是否保持最后一帧
        """
        with self._lock:
            params = self._clip_params.get(clip_id)
            if params is None:
                params = VideoNodeParams()
                self._clip_params[clip_id] = params

            params.source_in = source_in
            params.source_out = source_out
            params.time_offset = time_offset
            params.speed = speed
            params.loop = loop
            params.hold = hold

    def get_node_params(self, clip_id: str) -> VideoNodeParams:
        """
        获取节点参数。

        Args:
            clip_id: 剪辑标识

        Returns:
            VideoNodeParams 实例
        """
        with self._lock:
            return self._clip_params.get(clip_id, VideoNodeParams())

    # =============================================================================
    # 统计与诊断
    # =============================================================================

    def get_stats(self) -> Dict[str, int | float | str]:
        """
        获取统计信息。

        Returns:
            统计信息字典
        """
        service_stats = self._service.get_stats()
        with self._lock:
            node_count = len(self._node_clips)
            clip_params_count = len(self._clip_params)
            # AUD-5502: timeline 字段与写入方共用 _lock 读取
            timeline_frame = self._timeline.current_frame
            timeline_fps = self._timeline.fps
            render_quality_name = self._timeline.render_quality.name

        return {
            **service_stats,
            'node_clip_count': node_count,
            'clip_params_count': clip_params_count,
            'timeline_frame': timeline_frame,
            'timeline_fps': timeline_fps,
            'render_quality': render_quality_name,
        }

    def shutdown(self) -> None:
        """
        关闭上下文。

        关闭所有剪辑，释放资源。
        """
        # AUD-5500: 先快照 (node_id -> clip_id) 再清空。旧实现清空
        # _node_clips 后才调 close_clip_for_node——它从已清空的字典 pop 到
        # None 直接 return False，service.close_clip 永远不会执行，所有
        # 剪辑在 VideoFrameProviderService 侧保持打开（解码器/文件句柄/
        # 帧缓存泄漏）。
        with self._lock:
            clip_ids = list(self._node_clips.values())
            self._node_clips.clear()
            self._clip_params.clear()

        # 关闭所有剪辑（service 侧按引用计数，按节点逐个释放）
        for clip_id in clip_ids:
            try:
                self._service.close_clip(clip_id)
            except Exception as e:
                # 尽力释放：单个剪辑关闭失败不阻断其余剪辑的回收。
                logger.warning(
                    "VideoContext shutdown: close_clip(%s) failed: %s",
                    clip_id, e)

        logger.info("VideoContext shutdown")


# =============================================================================
# 视频节点参数
# =============================================================================

@dataclass
class VideoNodeParams:
    """
    视频节点参数。

    P7.B: VideoFrameNode 支持 source in/out、time offset、speed、loop/hold。

    Attributes:
        source_in: 源起始帧（0-based）
        source_out: 源结束帧（-1 表示使用全部）
        time_offset: 时间偏移（秒）
        speed: 播放速度倍数（1.0 = 正常）
        loop: 是否循环播放
        hold: 是否保持最后一帧（超出范围时）
    """

    source_in: int = 0
    source_out: int = -1  # -1 表示使用全部
    time_offset: float = 0.0
    speed: float = 1.0
    loop: bool = False
    hold: bool = False

    def to_dict(self) -> Dict[str, int | float | bool]:
        """转换为字典"""
        return {
            'source_in': self.source_in,
            'source_out': self.source_out,
            'time_offset': self.time_offset,
            'speed': self.speed,
            'loop': self.loop,
            'hold': self.hold,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, int | float | bool]) -> VideoNodeParams:
        """从字典创建"""
        return cls(
            source_in=int(data.get('source_in', 0)),
            source_out=int(data.get('source_out', -1)),
            time_offset=float(data.get('time_offset', 0.0)),
            speed=float(data.get('speed', 1.0)),
            loop=bool(data.get('loop', False)),
            hold=bool(data.get('hold', False)),
        )