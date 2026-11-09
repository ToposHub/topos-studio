"""CV-B02：Color 时间域——clip_local／source／timeline 精确映射与源 TC。

缺口（计划 CV-B02）：Color transport 的时间码是 float fps 的近似
（``VideoControlBar._update_timecode`` 手工取整），源起始 TC／DF、VFR、
变速／反向 clip 的帧域换算没有具名域的单一事实源。

本模块是纯领域层：

- 帧域映射函数包住 ``ClipInstance`` 的权威有理数映射
  （``clip_local_to_source_frame`` 等，处理 trim／retime／reverse／
  freeze），并把 ``effective_source_start`` 偏移与夹取语义集中到一处，
  供 FileOpsMixin 委托调用（不再各自 inline）；
- ``SourceTimecodeContext`` 从 ``VideoMetadata`` 建立源 TC 域：Rational
  fps、起始 TC、DF/NDF（共享 ``timecode.py``，非法字符串按相对 TC 回
  落，不伪造片头绝对码）；格式化／解析均为精确有理数运算；
- VFR 走 duck 协议（``points`` ＋ ``source_frame_to_seconds``，与 Edit
  的 ``SourceFrameTimeIndex`` 结构一致）——按秒反查帧用二分，不做全片
  扫描，也不要求预先建索引。
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Optional

from src.shared.media.metadata import Rational, VideoMetadata
from src.shared.media.timecode import Timecode, TimecodeType


# ── 帧域映射（包住 ClipInstance 权威方法） ─────────────────────────────


def _clip_total(clip, total_frames: Optional[int]) -> int:
    """播放总帧数：显式传入优先，否则按 clip 时长属性序列推导。"""
    if total_frames is not None and int(total_frames) > 0:
        return max(1, int(total_frames))
    for attr in ("timeline_duration", "duration", "source_duration"):
        try:
            value = int(getattr(clip, attr, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return max(1, value)
    return 1


def _source_offset(clip, effective_source_start: Optional[int]) -> int:
    """effective_source_start 相对 clip.source_start 的偏移（trim 生效位）。"""
    clip_start = int(getattr(clip, "source_start", 0) or 0)
    if effective_source_start is None:
        return 0
    return int(effective_source_start) - clip_start


def local_to_source_frame(
        clip,
        local_frame: int,
        *,
        effective_source_start: Optional[int] = None,
        total_frames: Optional[int] = None) -> int:
    """clip-local → 源帧。无 clip（独立视频）＝恒等映射。"""
    if clip is None:
        return int(local_frame)
    total = _clip_total(clip, total_frames)
    local = max(0, min(int(local_frame), total - 1))
    offset = _source_offset(clip, effective_source_start)
    clip_start = int(getattr(clip, "source_start", 0) or 0)
    try:
        return int(clip.clip_local_to_source_frame(local)) + offset
    except (AttributeError, TypeError, ValueError):
        # 测试替身/旧 clip 无权威方法：保持与旧 inline 路径一致。
        return clip_start + offset + local


def source_to_local_frame(
        clip,
        source_frame: int,
        *,
        effective_source_start: Optional[int] = None,
        total_frames: Optional[int] = None) -> int:
    """源帧 → clip-local（夹取到 [0, total-1]）。无 clip ＝恒等。"""
    if clip is None:
        return int(source_frame)
    total = _clip_total(clip, total_frames)
    offset = _source_offset(clip, effective_source_start)
    clip_start = int(getattr(clip, "source_start", 0) or 0)
    try:
        local = int(clip.source_to_clip_local_frame(int(source_frame) - offset))
    except (AttributeError, TypeError, ValueError):
        local = int(source_frame) - clip_start - offset
    return max(0, min(local, total - 1))


def local_to_timeline_frame(
        clip,
        local_frame: int,
        *,
        total_frames: Optional[int] = None) -> Optional[int]:
    """clip-local → 时间线帧。无 clip（独立视频/图片）→ None（无此域）。"""
    if clip is None:
        return None
    total = _clip_total(clip, total_frames)
    local = max(0, min(int(local_frame), total - 1))
    return int(getattr(clip, "timeline_start", 0) or 0) + local


def timeline_to_local_frame(
        clip,
        timeline_frame: int,
        *,
        total_frames: Optional[int] = None) -> Optional[int]:
    """时间线帧 → clip-local。无 clip → None；越界 → None（不夹取，
    由调用方决定是否切换 clip）。"""
    if clip is None:
        return None
    total = _clip_total(clip, total_frames)
    timeline_start = int(getattr(clip, "timeline_start", 0) or 0)
    local = int(timeline_frame) - timeline_start
    if local < 0 or local >= total:
        return None
    return local


# ── 源时间码域 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceTimecodeContext:
    """源媒体 TC 域：精确格式化／解析。

    ``start_frames`` 为起始 TC 的帧偏移；0 表示相对 TC（素材从
    00:00:00:00 计）。缺 fps 的 metadata 由 ``from_metadata`` 返回
    None（显式诊断，不伪造 24fps）。
    """

    fps: Rational
    tc_type: TimecodeType = TimecodeType.NON_DROP_FRAME
    start_frames: int = 0
    total_frames: int = 0

    @classmethod
    def from_metadata(
            cls, metadata: Optional[VideoMetadata]
    ) -> Optional["SourceTimecodeContext"]:
        if metadata is None:
            return None
        fps = getattr(metadata, "fps", None)
        if not isinstance(fps, Rational) or fps.num <= 0 or fps.den <= 0:
            return None
        drop_flag = getattr(metadata, "timecode_drop_frame", None)
        if drop_flag is None:
            tc_type = Timecode.auto_detect_type(fps)
        else:
            tc_type = (TimecodeType.DROP_FRAME if drop_flag
                       else TimecodeType.NON_DROP_FRAME)
        start_frames = 0
        tc_text = getattr(metadata, "timecode", None)
        if tc_text:
            try:
                parsed = Timecode.from_string(str(tc_text), fps)
                start_frames = int(parsed.to_frame_number())
                # 字符串里的 ';' 分隔符是 DF 的权威信号（优先于旗标）。
                if ";" in str(tc_text):
                    tc_type = TimecodeType.DROP_FRAME
            except (ValueError, TypeError):
                start_frames = 0  # 非法起始码 → 相对 TC，不伪造绝对码
        total = int(getattr(metadata, "total_frames", 0) or 0)
        return cls(fps=fps, tc_type=tc_type, start_frames=start_frames,
                   total_frames=max(0, total))

    def format_frame(self, source_frame: int) -> str:
        """源帧 → 精确 TC 字符串（含起始偏移与 DF 跳帧）。"""
        frame = int(source_frame)
        if frame < 0:
            frame = 0
        if self.total_frames > 0:
            frame = min(frame, self.total_frames - 1)
        absolute = frame + max(0, self.start_frames)
        return Timecode.from_frame_number(
            absolute, self.fps, self.tc_type).to_string()

    def parse_to_frame(self, text: str) -> Optional[int]:
        """TC 字符串 → 源帧。非法输入返回 None（调用方保持原播放头）。"""
        if not text or not isinstance(text, str):
            return None
        try:
            parsed = Timecode.from_string(text.strip(), self.fps)
        except (ValueError, TypeError):
            return None
        frame = int(parsed.to_frame_number()) - max(0, self.start_frames)
        if frame < 0:
            return None
        if self.total_frames > 0 and frame >= self.total_frames:
            return None
        return frame


# ── VFR duck 协议适配（与 Edit SourceFrameTimeIndex 结构一致） ─────────


def vfr_seconds_for_frame(index, source_frame: int) -> Optional[float]:
    """VFR：帧 → 秒（直接走索引方法；无索引/无点 → None）。"""
    if index is None:
        return None
    to_seconds = getattr(index, "source_frame_to_seconds", None)
    if callable(to_seconds):
        try:
            return float(to_seconds(int(source_frame)))
        except (ValueError, IndexError, KeyError):
            return None
    return None


def vfr_frame_for_seconds(index, seconds: float) -> Optional[int]:
    """VFR：秒 → 帧。对 ``index.points``（按 source_frame 升序）二分，
    不做全片线性扫描。"""
    if index is None:
        return None
    points = getattr(index, "points", None)
    if not points:
        return None
    seconds = float(seconds)
    if seconds < 0.0:
        return None
    # 预取秒序列（Points 是 tuple，通常已按帧序即时间序）。
    times: list[float] = []
    for point in points:
        value = getattr(point, "seconds", None)
        if value is None:
            return None
        times.append(float(value))
    position = bisect_left(times, seconds)
    if position >= len(times):
        return int(points[-1].source_frame)
    if position == 0:
        return int(points[0].source_frame)
    before, after = times[position - 1], times[position]
    if seconds - before <= after - seconds:
        return int(points[position - 1].source_frame)
    return int(points[position].source_frame)


# ── transport 显示格式化器 ────────────────────────────────────────────


class TransportTimecodeFormatter:
    """Color transport（clip-local 帧）→ 源域精确 TC 字符串。

    ``local_to_source`` 缺省恒等（独立视频：本地帧即源帧）。只做显示
    换算，不发 seek；异常由调用方回落旧 float 行为（显示不中断）。
    """

    def __init__(self, context: SourceTimecodeContext,
                 local_to_source=None) -> None:
        self.context = context
        self._local_to_source = local_to_source

    def format(self, local_frame: int) -> str:
        source = int(local_frame)
        if self._local_to_source is not None:
            source = int(self._local_to_source(source))
        return self.context.format_frame(source)
