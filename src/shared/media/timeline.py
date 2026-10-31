"""
Timeline Data Models Module
视频编辑时间线数据模型。

## 职责

- TimelineClip: 剪辑片段数据模型
- TimelineTrack: 轨道数据模型
- Timeline: 时间线数据模型

## 设计原则

- 不可变数据类（frozen=True）确保线程安全
- 完整类型注解
- 与 VideoMetadata/Timecode 保持一致的设计风格
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional
import uuid

from .metadata import VideoMetadata, Rational


# 速度范围常量
MIN_SPEED: float = 0.1   # 最小速度（10%）
MAX_SPEED: float = 4.0   # 最大速度（400%）

# 浮点误差吸附阈值：速度换算结果到最近整数的距离小于该值时视为整数。
# 远小于半帧（0.5），不会改变真实小数结果的 floor 语义。
_SPEED_EPSILON: float = 1e-9


def _speed_floor(value: float) -> int:
    """速度换算帧数的取整（保留 floor 语义，吸收二进制浮点误差）。

    0.3 等十进制步长的二进制浮点表示略小于真值：3 / 0.3 == 9.999…998、
    10 * 0.3 == 2.999…996，直接 ``int()`` 截断会产生 off-by-one
    （AUD-35806）。对距最近整数的偏差小于 ``_SPEED_EPSILON`` 的结果先
    吸附到该整数再返回；真实小数部分仍按 floor 截断，与既有取整
    契约一致（如 (199-100)/2 == 49.5 → 49）。

    Args:
        value: 速度换算出的浮点帧数（非负）

    Returns:
        取整后的帧数
    """
    nearest = round(value)
    if abs(value - nearest) <= _SPEED_EPSILON:
        return int(nearest)
    return math.floor(value)


@dataclass(frozen=True)
class TimelineClip:
    """
    时间线剪辑片段。

    表示时间线上的一个剪辑，包含源媒体引用、时间映射和播放参数。

    时间映射关系：
    - source_start/source_end: 源媒体中的帧范围
    - timeline_start/timeline_end: 时间线中的帧位置
    - speed: 播放速度，影响源帧与时间线帧的对应关系

    Attributes:
        clip_id: 唯一标识符
        source_path: 源媒体文件路径
        source_start: 源媒体中的起始帧号（包含）
        source_end: 源媒体中的结束帧号（包含）
        timeline_start: 时间线中的起始帧号
        timeline_end: 时间线中的结束帧号
        speed: 播放速度（1.0 = 正常，2.0 = 两倍速，0.5 = 半速）
        is_enabled: 是否启用（禁用的剪辑不参与渲染）
        metadata: 缓存的视频元数据（可选）
    """

    clip_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_path: str = ""
    source_start: int = 0
    source_end: int = 0
    timeline_start: int = 0
    timeline_end: int = 1  # 默认 1 帧，使默认构造合法 (timeline_start < timeline_end)
    speed: float = 1.0
    is_enabled: bool = True
    metadata: Optional[VideoMetadata] = None

    def __post_init__(self) -> None:
        """验证剪辑数据有效性"""
        # 帧范围验证
        if self.source_start < 0:
            raise ValueError(f"source_start cannot be negative: {self.source_start}")
        if self.source_end < 0:
            raise ValueError(f"source_end cannot be negative: {self.source_end}")
        if self.source_start > self.source_end:
            raise ValueError(
                f"source_start ({self.source_start}) cannot be greater than "
                f"source_end ({self.source_end})"
            )
        if self.timeline_start < 0:
            raise ValueError(f"timeline_start cannot be negative: {self.timeline_start}")
        if self.timeline_end < 0:
            raise ValueError(f"timeline_end cannot be negative: {self.timeline_end}")
        if self.timeline_start >= self.timeline_end:
            raise ValueError(
                f"timeline_start ({self.timeline_start}) must be less than "
                f"timeline_end ({self.timeline_end})"
            )

        # 速度验证（AUD-35805：NaN 与任何比较都为 False，`<`/`>` 链放行
        # NaN——用 `not (lo <= x <= hi)` 形式让 NaN/inf 全部被拒绝）
        if not (MIN_SPEED <= self.speed <= MAX_SPEED):
            raise ValueError(
                f"speed must be between {MIN_SPEED} and {MAX_SPEED}, got {self.speed}"
            )

        # 元数据帧范围验证（如果有元数据）
        if self.metadata is not None and self.metadata.total_frames is not None:
            if self.source_end >= self.metadata.total_frames:
                raise ValueError(
                    f"source_end ({self.source_end}) exceeds source total frames "
                    f"({self.metadata.total_frames})"
                )

    # ==================== 计算属性 ====================

    @property
    def duration_frames(self) -> int:
        """
        时间线上的剪辑长度（帧数）。

        Returns:
            时间线上的帧数 = timeline_end - timeline_start
        """
        return self.timeline_end - self.timeline_start

    @property
    def source_duration(self) -> int:
        """
        源媒体中的剪辑长度（帧数）。

        Returns:
            源媒体帧数 = source_end - source_start + 1
        """
        return self.source_end - self.source_start + 1

    @property
    def effective_duration(self) -> int:
        """
        考虑速度后的有效时长（帧数）。

        实际使用的源帧数 = source_duration
        时间线占用帧数 = source_duration / speed

        Returns:
            有效时长（帧数）
        """
        return _speed_floor(self.source_duration / self.speed)

    # ==================== 帧转换函数 ====================

    def timeline_to_source_frame(self, timeline_frame: int) -> int:
        """
        将时间线帧号转换为源媒体帧号。

        Args:
            timeline_frame: 时间线中的帧号

        Returns:
            源媒体中的帧号

        Raises:
            ValueError: 如果时间线帧不在剪辑范围内

        Examples:
            >>> clip = TimelineClip(
            ...     source_start=100, source_end=199,
            ...     timeline_start=0, timeline_end=100,
            ...     speed=1.0
            ... )
            >>> clip.timeline_to_source_frame(0)
            100
            >>> clip.timeline_to_source_frame(50)
            150
        """
        if not (self.timeline_start <= timeline_frame < self.timeline_end):
            raise ValueError(
                f"Timeline frame {timeline_frame} is outside clip range "
                f"[{self.timeline_start}, {self.timeline_end})"
            )

        # 计算剪辑内的相对帧号
        relative_frame = timeline_frame - self.timeline_start

        # 考虑速度转换到源帧（AUD-35806：10 * 0.3 == 2.999…996，
        # 直接 int() 截断差 1 帧）
        source_relative = _speed_floor(relative_frame * self.speed)

        # 加上源起始帧
        return self.source_start + source_relative

    def source_to_timeline_frame(self, source_frame: int) -> int:
        """
        将源媒体帧号转换为时间线帧号。

        Args:
            source_frame: 源媒体中的帧号

        Returns:
            时间线中的帧号

        Raises:
            ValueError: 如果源帧不在剪辑范围内

        Examples:
            >>> clip = TimelineClip(
            ...     source_start=100, source_end=199,
            ...     timeline_start=0, timeline_end=100,
            ...     speed=1.0
            ... )
            >>> clip.source_to_timeline_frame(100)
            0
            >>> clip.source_to_timeline_frame(150)
            50
        """
        if not (self.source_start <= source_frame <= self.source_end):
            raise ValueError(
                f"Source frame {source_frame} is outside clip range "
                f"[{self.source_start}, {self.source_end}]"
            )

        # 计算源内的相对帧号
        source_relative = source_frame - self.source_start

        # 考虑速度转换到时间线帧（AUD-35806：3 / 0.3 == 9.999…998，
        # 直接 int() 截断差 1 帧）
        timeline_relative = _speed_floor(source_relative / self.speed)

        # 加上时间线起始帧
        return self.timeline_start + timeline_relative

    # ==================== 工厂方法 ====================

    def with_speed(self, speed: float) -> TimelineClip:
        """
        创建新的剪辑实例，使用不同的速度。

        速度改变会影响时间线上的占用时长。

        Args:
            speed: 新的播放速度

        Returns:
            新的 TimelineClip 实例

        Examples:
            >>> clip = TimelineClip(
            ...     source_start=0, source_end=99,
            ...     timeline_start=0, timeline_end=100,
            ...     speed=1.0
            ... )
            >>> fast_clip = clip.with_speed(2.0)
            >>> fast_clip.speed
            2.0
            >>> fast_clip.duration_frames
            50
        """
        # 重新计算时间线结束帧（AUD-35806：浮点截断吸附，见 _speed_floor）
        new_duration = _speed_floor(self.source_duration / speed)
        new_timeline_end = self.timeline_start + new_duration

        return TimelineClip(
            clip_id=self.clip_id,
            source_path=self.source_path,
            source_start=self.source_start,
            source_end=self.source_end,
            timeline_start=self.timeline_start,
            timeline_end=new_timeline_end,
            speed=speed,
            is_enabled=self.is_enabled,
            metadata=self.metadata,
        )

    def with_trim(
        self,
        source_start: Optional[int] = None,
        source_end: Optional[int] = None,
    ) -> TimelineClip:
        """
        创建新的剪辑实例，调整源媒体范围。

        Args:
            source_start: 新的源起始帧（None 表示不变）
            source_end: 新的源结束帧（None 表示不变）

        Returns:
            新的 TimelineClip 实例
        """
        new_source_start = source_start if source_start is not None else self.source_start
        new_source_end = source_end if source_end is not None else self.source_end

        # 重新计算时间线长度（AUD-35806：浮点截断吸附，见 _speed_floor）
        new_source_duration = new_source_end - new_source_start + 1
        new_duration = _speed_floor(new_source_duration / self.speed)
        new_timeline_end = self.timeline_start + new_duration

        return TimelineClip(
            clip_id=self.clip_id,
            source_path=self.source_path,
            source_start=new_source_start,
            source_end=new_source_end,
            timeline_start=self.timeline_start,
            timeline_end=new_timeline_end,
            speed=self.speed,
            is_enabled=self.is_enabled,
            metadata=self.metadata,
        )

    def with_offset(self, timeline_offset: int) -> TimelineClip:
        """
        创建新的剪辑实例，移动时间线位置。

        Args:
            timeline_offset: 时间线偏移量（正数向右移动，负数向左移动）

        Returns:
            新的 TimelineClip 实例
        """
        return TimelineClip(
            clip_id=self.clip_id,
            source_path=self.source_path,
            source_start=self.source_start,
            source_end=self.source_end,
            timeline_start=self.timeline_start + timeline_offset,
            timeline_end=self.timeline_end + timeline_offset,
            speed=self.speed,
            is_enabled=self.is_enabled,
            metadata=self.metadata,
        )

    def freeze(self) -> TimelineClip:
        """
        返回不可变实例。

        由于 dataclass 已经是 frozen=True，直接返回 self。

        Returns:
            不可变的 TimelineClip 实例
        """
        return self

    # ==================== 比较和哈希 ====================

    def __hash__(self) -> int:
        """哈希值，基于 clip_id"""
        return hash(self.clip_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 clip_id"""
        if not isinstance(other, TimelineClip):
            return NotImplemented
        return self.clip_id == other.clip_id

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"TimelineClip(clip_id={self.clip_id!r}, "
            f"source={self.source_path!r}, "
            f"source=[{self.source_start}-{self.source_end}], "
            f"timeline=[{self.timeline_start}-{self.timeline_end}], "
            f"speed={self.speed})"
        )


@dataclass(frozen=True)
class TimelineTrack:
    """
    时间线轨道。

    管理同一轨道上的多个剪辑片段。

    Attributes:
        track_id: 轨道唯一标识符
        name: 轨道名称
        clips: 剪辑列表（按时间线位置排序）
        is_enabled: 轨道是否启用
        is_locked: 轨道是否锁定（禁止编辑）
        track_type: 轨道类型（'video', 'audio', 'subtitle'）
        opacity: 轨道不透明度（0.0 - 1.0）
    """

    track_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    clips: tuple[TimelineClip, ...] = field(default_factory=tuple)
    is_enabled: bool = True
    is_locked: bool = False
    track_type: str = "video"
    opacity: float = 1.0

    def __post_init__(self) -> None:
        """验证轨道数据有效性"""
        # AUD-35805：NaN 通过 `<`/`>` 比较链——用 `not (0 <= x <= 1)`
        # 拒绝 NaN 与任何越界值
        if not (0.0 <= self.opacity <= 1.0):
            raise ValueError(f"opacity must be between 0.0 and 1.0, got {self.opacity}")

        if self.track_type not in ("video", "audio", "subtitle"):
            raise ValueError(f"Invalid track_type: {self.track_type}")

        # 验证剪辑不重叠
        sorted_clips = sorted(self.clips, key=lambda c: c.timeline_start)
        for i in range(len(sorted_clips) - 1):
            current = sorted_clips[i]
            next_clip = sorted_clips[i + 1]
            if current.timeline_end > next_clip.timeline_start:
                raise ValueError(
                    f"Clips overlap: {current.clip_id} ends at {current.timeline_end}, "
                    f"but {next_clip.clip_id} starts at {next_clip.timeline_start}"
                )

    # ==================== 计算属性 ====================

    @property
    def duration_frames(self) -> int:
        """
        轨道总时长（帧数）。

        Returns:
            最后一个剪辑的结束帧
        """
        if not self.clips:
            return 0
        return max(clip.timeline_end for clip in self.clips)

    @property
    def enabled_clips(self) -> tuple[TimelineClip, ...]:
        """
        获取所有启用的剪辑。

        Returns:
            启用的剪辑元组
        """
        return tuple(clip for clip in self.clips if clip.is_enabled)

    # ==================== 查询方法 ====================

    def get_clip_at(self, timeline_frame: int) -> Optional[TimelineClip]:
        """
        获取指定时间线帧位置的剪辑。

        Args:
            timeline_frame: 时间线帧号

        Returns:
            该位置的剪辑，如果没有则返回 None
        """
        for clip in self.clips:
            if clip.timeline_start <= timeline_frame < clip.timeline_end:
                return clip
        return None

    def get_clips_in_range(
        self, start_frame: int, end_frame: int
    ) -> tuple[TimelineClip, ...]:
        """
        获取指定时间范围内的所有剪辑。

        Args:
            start_frame: 起始帧（包含）
            end_frame: 结束帧（不包含）

        Returns:
            范围内的剪辑元组
        """
        result = []
        for clip in self.clips:
            # 剪辑与范围有交集
            if clip.timeline_start < end_frame and clip.timeline_end > start_frame:
                result.append(clip)
        return tuple(result)

    def get_clip_by_id(self, clip_id: str) -> Optional[TimelineClip]:
        """
        根据 clip_id 获取剪辑。

        Args:
            clip_id: 剪辑 ID

        Returns:
            剪辑实例，如果不存在则返回 None
        """
        for clip in self.clips:
            if clip.clip_id == clip_id:
                return clip
        return None

    # ==================== 编辑方法 ====================

    def with_clip_added(self, clip: TimelineClip) -> TimelineTrack:
        """
        创建新轨道，添加剪辑。

        Args:
            clip: 要添加的剪辑

        Returns:
            新的 TimelineTrack 实例

        Raises:
            ValueError: 如果新剪辑与现有剪辑重叠
        """
        # 检查重叠
        for existing in self.clips:
            if (clip.timeline_start < existing.timeline_end and
                clip.timeline_end > existing.timeline_start):
                raise ValueError(
                    f"Clip overlaps with existing clip {existing.clip_id}"
                )

        new_clips = tuple(sorted(self.clips + (clip,), key=lambda c: c.timeline_start))
        return TimelineTrack(
            track_id=self.track_id,
            name=self.name,
            clips=new_clips,
            is_enabled=self.is_enabled,
            is_locked=self.is_locked,
            track_type=self.track_type,
            opacity=self.opacity,
        )

    def with_clip_removed(self, clip_id: str) -> TimelineTrack:
        """
        创建新轨道，移除指定剪辑。

        Args:
            clip_id: 要移除的剪辑 ID

        Returns:
            新的 TimelineTrack 实例
        """
        new_clips = tuple(c for c in self.clips if c.clip_id != clip_id)
        return TimelineTrack(
            track_id=self.track_id,
            name=self.name,
            clips=new_clips,
            is_enabled=self.is_enabled,
            is_locked=self.is_locked,
            track_type=self.track_type,
            opacity=self.opacity,
        )

    def with_clip_updated(self, clip: TimelineClip) -> TimelineTrack:
        """
        创建新轨道，更新指定剪辑。

        Args:
            clip: 要更新的剪辑（通过 clip_id 匹配）

        Returns:
            新的 TimelineTrack 实例

        Raises:
            ValueError: 如果更新后的剪辑与其他剪辑重叠
        """
        # 先移除旧剪辑
        other_clips = tuple(c for c in self.clips if c.clip_id != clip.clip_id)

        # 检查新剪辑与其他剪辑的重叠
        for existing in other_clips:
            if (clip.timeline_start < existing.timeline_end and
                clip.timeline_end > existing.timeline_start):
                raise ValueError(
                    f"Updated clip overlaps with existing clip {existing.clip_id}"
                )

        new_clips = tuple(sorted(other_clips + (clip,), key=lambda c: c.timeline_start))
        return TimelineTrack(
            track_id=self.track_id,
            name=self.name,
            clips=new_clips,
            is_enabled=self.is_enabled,
            is_locked=self.is_locked,
            track_type=self.track_type,
            opacity=self.opacity,
        )

    def __hash__(self) -> int:
        """哈希值，基于 track_id"""
        return hash(self.track_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 track_id"""
        if not isinstance(other, TimelineTrack):
            return NotImplemented
        return self.track_id == other.track_id


@dataclass(frozen=True)
class Timeline:
    """
    视频编辑时间线。

    管理多个轨道，支持视频、音频和字幕轨道。

    Attributes:
        timeline_id: 时间线唯一标识符
        name: 时间线名称
        tracks: 轨道列表
        fps: 帧率
        sample_rate: 音频采样率（可选）
    """

    timeline_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    tracks: tuple[TimelineTrack, ...] = field(default_factory=tuple)
    fps: Rational = Rational(24, 1)  # 默认 24fps
    sample_rate: Optional[int] = 48000  # 默认 48kHz

    # ==================== 计算属性 ====================

    @property
    def duration_frames(self) -> int:
        """
        时间线总时长（帧数）。

        Returns:
            所有轨道中的最大时长
        """
        if not self.tracks:
            return 0
        return max(track.duration_frames for track in self.tracks)

    @property
    def video_tracks(self) -> tuple[TimelineTrack, ...]:
        """获取所有视频轨道"""
        return tuple(t for t in self.tracks if t.track_type == "video")

    @property
    def audio_tracks(self) -> tuple[TimelineTrack, ...]:
        """获取所有音频轨道"""
        return tuple(t for t in self.tracks if t.track_type == "audio")

    @property
    def subtitle_tracks(self) -> tuple[TimelineTrack, ...]:
        """获取所有字幕轨道"""
        return tuple(t for t in self.tracks if t.track_type == "subtitle")

    # ==================== 查询方法 ====================

    def get_track_by_id(self, track_id: str) -> Optional[TimelineTrack]:
        """
        根据 track_id 获取轨道。

        Args:
            track_id: 轨道 ID

        Returns:
            轨道实例，如果不存在则返回 None
        """
        for track in self.tracks:
            if track.track_id == track_id:
                return track
        return None

    def get_clip_at(
        self, timeline_frame: int, track_type: Optional[str] = None
    ) -> dict[str, TimelineClip]:
        """
        获取指定时间线帧位置的所有剪辑。

        Args:
            timeline_frame: 时间线帧号
            track_type: 轨道类型过滤（None 表示所有类型）

        Returns:
            字典 {track_id: clip}，包含该位置的所有剪辑
        """
        result = {}
        for track in self.tracks:
            if track_type is not None and track.track_type != track_type:
                continue
            clip = track.get_clip_at(timeline_frame)
            if clip is not None:
                result[track.track_id] = clip
        return result

    def get_clips_in_range(
        self, start_frame: int, end_frame: int, track_type: Optional[str] = None
    ) -> dict[str, tuple[TimelineClip, ...]]:
        """
        获取指定时间范围内的所有剪辑。

        Args:
            start_frame: 起始帧（包含）
            end_frame: 结束帧（不包含）
            track_type: 轨道类型过滤（None 表示所有类型）

        Returns:
            字典 {track_id: (clips...)}，包含范围内的所有剪辑
        """
        result = {}
        for track in self.tracks:
            if track_type is not None and track.track_type != track_type:
                continue
            clips = track.get_clips_in_range(start_frame, end_frame)
            if clips:
                result[track.track_id] = clips
        return result

    # ==================== 编辑方法 ====================

    def with_track_added(self, track: TimelineTrack) -> Timeline:
        """
        创建新时间线，添加轨道。

        Args:
            track: 要添加的轨道

        Returns:
            新的 Timeline 实例
        """
        return Timeline(
            timeline_id=self.timeline_id,
            name=self.name,
            tracks=self.tracks + (track,),
            fps=self.fps,
            sample_rate=self.sample_rate,
        )

    def with_track_removed(self, track_id: str) -> Timeline:
        """
        创建新时间线，移除指定轨道。

        Args:
            track_id: 要移除的轨道 ID

        Returns:
            新的 Timeline 实例
        """
        new_tracks = tuple(t for t in self.tracks if t.track_id != track_id)
        return Timeline(
            timeline_id=self.timeline_id,
            name=self.name,
            tracks=new_tracks,
            fps=self.fps,
            sample_rate=self.sample_rate,
        )

    def with_track_updated(self, track: TimelineTrack) -> Timeline:
        """
        创建新时间线，更新指定轨道。

        Args:
            track: 要更新的轨道（通过 track_id 匹配）

        Returns:
            新的 Timeline 实例

        Raises:
            ValueError: 如果 track_id 不存在（AUD-35804：此前静默无操作，
                更新被丢弃且调用方无感知——显式报错与 with_clip_added/
                with_clip_removed 的 "Track not found" 契约对齐）
        """
        if self.get_track_by_id(track.track_id) is None:
            raise ValueError(f"Track not found: {track.track_id}")
        new_tracks = tuple(
            track if t.track_id == track.track_id else t
            for t in self.tracks
        )
        return Timeline(
            timeline_id=self.timeline_id,
            name=self.name,
            tracks=new_tracks,
            fps=self.fps,
            sample_rate=self.sample_rate,
        )

    def with_clip_added(self, track_id: str, clip: TimelineClip) -> Timeline:
        """
        创建新时间线，在指定轨道添加剪辑。

        Args:
            track_id: 目标轨道 ID
            clip: 要添加的剪辑

        Returns:
            新的 Timeline 实例

        Raises:
            ValueError: 如果轨道不存在或剪辑重叠
        """
        track = self.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")

        new_track = track.with_clip_added(clip)
        return self.with_track_updated(new_track)

    def with_clip_removed(self, track_id: str, clip_id: str) -> Timeline:
        """
        创建新时间线，移除指定剪辑。

        Args:
            track_id: 轨道 ID
            clip_id: 剪辑 ID

        Returns:
            新的 Timeline 实例

        Raises:
            ValueError: 如果轨道不存在
        """
        track = self.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")

        new_track = track.with_clip_removed(clip_id)
        return self.with_track_updated(new_track)

    def with_clip_updated(self, track_id: str, clip: TimelineClip) -> Timeline:
        """
        创建新时间线，更新指定剪辑。

        Args:
            track_id: 轨道 ID
            clip: 要更新的剪辑

        Returns:
            新的 Timeline 实例

        Raises:
            ValueError: 如果轨道不存在或剪辑重叠
        """
        track = self.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")

        new_track = track.with_clip_updated(clip)
        return self.with_track_updated(new_track)

    def __hash__(self) -> int:
        """哈希值，基于 timeline_id"""
        return hash(self.timeline_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 timeline_id"""
        if not isinstance(other, Timeline):
            return NotImplemented
        return self.timeline_id == other.timeline_id