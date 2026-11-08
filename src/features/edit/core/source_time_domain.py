"""VP-B04：Source 时间域——帧/PTS/时间码换算、In/Out 校验与按资产状态。

计划：docs/plans/2026-11-09-edit-viewport-backend-plan.md §VP-B04。

背景缺口：Source 侧维护独立 float fps，无源起始时间码概念；VFR 索引
（Scene Cut 的 ``SourceFrameTimeIndex``）只服务场景切割、不参与普通播
放；In/Out 提交前的区间校验（空范围、In>Out、负值、超末帧、单帧、只
音频）没有统一权威；换源后的监看状态恢复没有媒体版本校验。

本模块是纯领域件（无 Qt）：

- ``rational_from_float_fps``：23.976/29.97/59.94 → 24000/1001 等
  Rational（替换 ``round(fps)`` 近似）；
- ``SourceTimeDomain``：帧 ↔ ``Timecode``（NDF/DF 按帧率自动判定，
  支持非零源起始 TC 偏移；非法 DF 时间码显式拒绝）、帧 ↔ 秒
  （带 ``SourceFrameTimeIndex`` 走 PTS，CFR 回退 ``frame/fps``）、
  In/Out 区间校验（Out 包含语义，编辑长度 = out − in + 1）；
- ``SourceMonitorStateStore``：按 asset_id + 媒体指纹保存/恢复播放头
  与 In/Out；媒体版本（指纹/帧数）变化即作废；只存源域状态，不触碰
  时间线范围（时间线范围权威在 TimelineSequence，见 timeline_ops）。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from src.features.edit.core.scene_cut.models import SourceFrameTimeIndex
from src.shared.media.metadata import COMMON_FRAME_RATES, Rational
from src.shared.media.timecode import Timecode, TimecodeType

# ── 区间拒绝原因（稳定键） ─────────────────────────────────────────

REASON_RANGE_EMPTY = "range_empty"
REASON_IN_AFTER_OUT = "in_after_out"
REASON_NEGATIVE = "negative"
REASON_PAST_END = "past_end"
REASON_NO_FRAMES = "no_frames"

# float 帧率 → Rational 的容差：必须小于任意两个常见帧率的最小间隔
# （29.97 与 30 相差约 0.03），否则会把 NTSC 帧率吸成整数帧率。
_FPS_MATCH_TOLERANCE = 0.005


def rational_from_float_fps(fps: float) -> Rational:
    """把浮点帧率映射到最近的常见 Rational 帧率，否则有界分母兜底。"""
    for candidate in COMMON_FRAME_RATES:
        if abs(float(candidate) - float(fps)) <= _FPS_MATCH_TOLERANCE:
            return candidate
    frac = Fraction(float(fps)).limit_denominator(1001)
    return Rational(frac.numerator, frac.denominator)


@dataclass(frozen=True)
class SourceRangeCheck:
    """In/Out 区间校验结果（Out 为包含语义）。"""

    ok: bool
    reason: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    length: Optional[int] = None


@dataclass(frozen=True)
class SourceTimeDomain:
    """单个源素材的时间域权威（帧索引 / PTS 秒 / 时间码）。"""

    asset_id: str
    media_fingerprint: str
    fps: Rational
    total_frames: int
    start_timecode: Optional[Timecode] = None
    time_index: Optional[SourceFrameTimeIndex] = None

    def __post_init__(self) -> None:
        if self.total_frames < 0:
            raise ValueError(
                f"total_frames must be >= 0, got {self.total_frames}")

    # ── 时间码 ──────────────────────────────────────────────────────

    @property
    def tc_type(self) -> TimecodeType:
        if self.start_timecode is not None:
            return self.start_timecode.tc_type
        return Timecode.auto_detect_type(self.fps)

    def _start_frame_offset(self) -> int:
        if self.start_timecode is None:
            return 0
        return self.start_timecode.to_frame_number()

    def frame_to_timecode(self, frame: int) -> Timecode:
        """源帧 → 时间码（含源起始 TC 偏移；NDF/DF 由帧率/起始 TC 决定）。"""
        return Timecode.from_frame_number(
            self._start_frame_offset() + int(frame), self.fps, self.tc_type)

    def timecode_to_frame(self, tc_str: str) -> int:
        """解析时间码 → 源帧（相对源起始 TC）。

        非法 DF 时间码（整点分钟被跳过的首帧）、起始 TC 之前、超过末帧
        都显式拒绝（ValueError），不用 None/0 混淆错误。
        """
        tc = Timecode.from_string(tc_str, self.fps)  # 非法 DF 在此抛出
        frame = tc.to_frame_number() - self._start_frame_offset()
        if frame < 0:
            raise ValueError(
                f"timecode {tc_str!r} is before the source start timecode")
        if frame >= self.total_frames:
            raise ValueError(
                f"timecode {tc_str!r} is past the end of media "
                f"({self.total_frames} frames)")
        return frame

    # ── 秒（VFR-aware） ─────────────────────────────────────────────

    def frame_to_seconds(self, frame: int) -> float:
        """源帧 → 秒：VFR 走 PTS 索引（精确命中/线性插值），CFR 回退。"""
        if self.time_index is not None:
            return self.time_index.source_frame_to_seconds(int(frame))
        return int(frame) / self.fps.to_float()

    def seconds_to_frame(self, seconds: float) -> int:
        """秒 → 最近源帧：VFR 走索引最近邻，CFR 回退 round。"""
        if self.time_index is not None:
            return self.time_index.seconds_to_nearest_source_frame(
                float(seconds))
        return int(float(seconds) * self.fps.to_float() + 0.5)

    # ── In/Out 区间（Out 包含语义） ─────────────────────────────────

    def validate_range(self, in_frame: Optional[int],
                       out_frame: Optional[int]) -> SourceRangeCheck:
        """提交前校验源区间；Out 帧本身计入长度（out − in + 1）。"""
        if self.total_frames <= 0:
            return SourceRangeCheck(False, REASON_NO_FRAMES)
        if in_frame is None or out_frame is None:
            return SourceRangeCheck(False, REASON_RANGE_EMPTY)
        if in_frame < 0 or out_frame < 0:
            return SourceRangeCheck(False, REASON_NEGATIVE)
        if in_frame > out_frame:
            return SourceRangeCheck(False, REASON_IN_AFTER_OUT)
        if out_frame >= self.total_frames:
            return SourceRangeCheck(False, REASON_PAST_END)
        return SourceRangeCheck(
            True, None, start=int(in_frame), end=int(out_frame),
            length=int(out_frame) - int(in_frame) + 1)


@dataclass(frozen=True)
class SourceMonitorState:
    """单个素材的 Source 监看状态（只含源域，无时间线数据）。"""

    frame: int
    in_point: Optional[int] = None
    out_point: Optional[int] = None


@dataclass
class _StoredState:
    fingerprint: str
    total_frames: int
    state: SourceMonitorState


class SourceMonitorStateStore:
    """按资产保存/恢复 Source 监看状态（媒体版本变化即作废）。

    恢复前验证指纹与帧数——资产换版后旧状态（播放头/In/Out）不再回填
    假位置。本存储只管理源域；时间线范围权威在 TimelineSequence，本类
    不持有也不写任何时间线对象。
    """

    def __init__(self, max_assets: int = 64) -> None:
        if max_assets <= 0:
            raise ValueError("max_assets must be positive")
        self._max_assets = int(max_assets)
        self._entries: "OrderedDict[str, _StoredState]" = OrderedDict()

    def save(self, asset_id: str, media_fingerprint: str,
             total_frames: int, *, frame: int,
             in_point: Optional[int] = None,
             out_point: Optional[int] = None) -> None:
        frame = max(0, min(int(frame), max(0, int(total_frames) - 1)))
        self._entries[asset_id] = _StoredState(
            fingerprint=media_fingerprint,
            total_frames=int(total_frames),
            state=SourceMonitorState(
                frame=frame,
                in_point=(None if in_point is None else int(in_point)),
                out_point=(None if out_point is None else int(out_point)),
            ),
        )
        self._entries.move_to_end(asset_id)
        while len(self._entries) > self._max_assets:
            self._entries.popitem(last=False)

    def restore(self, asset_id: str, media_fingerprint: str,
                total_frames: int) -> Optional[SourceMonitorState]:
        entry = self._entries.get(asset_id)
        if entry is None:
            return None
        if (entry.fingerprint != media_fingerprint
                or entry.total_frames != int(total_frames)):
            return None  # 媒体已变更：旧状态作废，不回填假位置
        return entry.state

    def forget(self, asset_id: str) -> None:
        self._entries.pop(asset_id, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
