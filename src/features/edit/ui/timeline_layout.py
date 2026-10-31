# -*- coding: utf-8 -*-
"""
Timeline Layout Engine - Unified coordinate system for timeline visualization.

This module is the SINGLE source of truth for all coordinate calculations
in the timeline view. No other module should compute track Y positions,
frame-to-X mappings, or area rectangles independently.

Design rules:
- Video tracks live in the video area (top).
- Audio tracks live in the audio area (bottom).
- Splitter sits between them (default 50/50).
- Each track has independent height.
- Zoom only affects X (frame-to-pixel), never Y.
- All queries go through this engine.

Reference: docs/davinci_like_timeline_optimization_plan_2026-05-28.md §10
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

from PyQt6.QtCore import QRectF

from src.features.edit.ui.edit_style import EDIT_SIZES

logger = logging.getLogger(__name__)


# ── Default constants ───────────────────────────────────────────────────────

TRACK_HEADER_WIDTH: int = EDIT_SIZES["track_header_w"]
RULER_HEIGHT: int = EDIT_SIZES["ruler_h"]
TRACK_AREA_TOP: float = float(EDIT_SIZES["track_area_top"])
TRACK_GAP: int = 2
SPLITTER_HEIGHT: int = 6  # visual + hit area for the V/A splitter

DEFAULT_VIDEO_TRACK_HEIGHT: int = EDIT_SIZES["track_h_video"]
DEFAULT_AUDIO_TRACK_HEIGHT: int = EDIT_SIZES["track_h_audio"]
# P10.3: Subtitle tracks are shorter (text preview only, no waveforms/thumbnails)
DEFAULT_SUBTITLE_TRACK_HEIGHT: int = 40

MIN_VIDEO_TRACK_HEIGHT: int = 36
MAX_VIDEO_TRACK_HEIGHT: int = 160
MIN_AUDIO_TRACK_HEIGHT: int = 28
MAX_AUDIO_TRACK_HEIGHT: int = 120
# SUB-P1-06: subtitle lanes are resizable within a compact range so the track-
# height presets (small/medium/large) actually take effect. The bounds stay
# tight (24..80) because subtitles are an overlay annotation lane, not a second
# video lane — keeping them compact prevents them from visually covering video.
MIN_SUBTITLE_TRACK_HEIGHT: int = 24
MAX_SUBTITLE_TRACK_HEIGHT: int = 80
MIN_AREA_HEIGHT: int = 40  # minimum for each V/A area

# P10.3: Subtitle area gets a fixed fraction of the non-video space.
# Video area uses video_area_ratio; the remainder is split between audio and
# subtitle using subtitle_area_ratio (fraction of the non-video remainder).
SUBTITLE_AREA_RATIO: float = 0.25  # 25% of (1 - video_ratio) goes to subtitles


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class TrackInfo:
    """Minimal track descriptor consumed by the layout engine."""
    track_id: str
    track_type: str  # "video" | "audio" | "subtitle"
    default_height: int = 0  # filled by engine if 0


@dataclass
class AreaLayout:
    """Computed area rectangles."""
    video_area: QRectF = field(default_factory=QRectF)
    audio_area: QRectF = field(default_factory=QRectF)
    subtitle_area: QRectF = field(default_factory=QRectF)  # P10.3
    splitter_rect: QRectF = field(default_factory=QRectF)  # V/A splitter
    subtitle_splitter_rect: QRectF = field(default_factory=QRectF)  # S/V splitter (P10.3)
    total_track_area: QRectF = field(default_factory=QRectF)


@dataclass
class TimelineLayoutState:
    """
    Mutable UI layout state persisted across reloads.

    Attributes:
        video_area_ratio: fraction [0..1] of track area allocated to video (default 0.5)
        subtitle_area_ratio: fraction [0..1] of the non-video remainder allocated
            to subtitles (default 0.25). Audio gets the rest.
        track_heights: track_id -> pixel height
    """
    video_area_ratio: float = 0.5
    subtitle_area_ratio: float = SUBTITLE_AREA_RATIO
    track_heights: Dict[str, int] = field(default_factory=dict)
    # -1 means "bottom-align video stack" on first layout so V1 stays near splitter.
    video_scroll_offset: int = -1
    audio_scroll_offset: int = 0
    subtitle_scroll_offset: int = 0  # P10.3

    def to_dict(self) -> dict:
        """SUB-P1-06: serialize layout so reopening a project restores it."""
        return {
            "video_area_ratio": self.video_area_ratio,
            "subtitle_area_ratio": self.subtitle_area_ratio,
            "track_heights": dict(self.track_heights),
            "video_scroll_offset": self.video_scroll_offset,
            "audio_scroll_offset": self.audio_scroll_offset,
            "subtitle_scroll_offset": self.subtitle_scroll_offset,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "TimelineLayoutState":
        """SUB-P1-06: 从持久化 JSON 恢复布局。

        AUD-21201: 数据源是工程文件（磁盘 JSON → UIState → set_layout_state
        → from_dict），属外部输入——字段类型损坏（如 "abc"）时 float()/int()
        会直接抛异常打断工程加载，越界比率则破坏布局。逐字段校验并回退
        默认值：比率钳制到 [0.1, 0.9] / [0.05, 0.6]（与 set_splitter_ratio
        / set_subtitle_splitter_ratio 的运行时钳制口径一致），track_heights
        逐值 int() 转换并丢弃非法项（回退默认高度）。
        """
        if not isinstance(data, dict):
            return cls()

        def _ratio(value: object, default: float, lo: float, hi: float) -> float:
            try:
                ratio = float(value)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, ratio))

        def _int(value: object, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        track_heights: Dict[str, int] = {}
        raw_heights = data.get("track_heights")
        if isinstance(raw_heights, dict):
            for track_id, height in raw_heights.items():
                try:
                    track_heights[str(track_id)] = int(height)
                except (TypeError, ValueError):
                    continue  # 丢弃非法项，该轨回退默认高度

        return cls(
            video_area_ratio=_ratio(
                data.get("video_area_ratio", 0.5), 0.5, 0.1, 0.9),
            subtitle_area_ratio=_ratio(
                data.get("subtitle_area_ratio", SUBTITLE_AREA_RATIO),
                SUBTITLE_AREA_RATIO, 0.05, 0.6),
            track_heights=track_heights,
            video_scroll_offset=_int(data.get("video_scroll_offset", -1), -1),
            audio_scroll_offset=_int(data.get("audio_scroll_offset", 0), 0),
            subtitle_scroll_offset=_int(data.get("subtitle_scroll_offset", 0), 0),
        )


# ── Unified hit / drop data structures ────────────────────────────────────────

@dataclass(frozen=True)
class TimelineHit:
    """Result of a unified hit-test query on the timeline scene."""
    area: Literal["ruler", "splitter", "track", "clip", "header", "empty", "invalid"]
    track_id: Optional[str] = None
    clip_id: Optional[str] = None
    frame: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class TimelineDropTarget:
    """Result of a unified drop-target resolution."""
    frame: int
    track_id: Optional[str] = None
    track_type: Optional[str] = None
    is_valid: bool = True
    create_track: bool = False
    reason: str = ""


# ── Engine ────────────────────────────────────────────────────────────────────

class TimelineLayoutEngine:
    """
    Pure layout engine — no Qt widget dependencies, fully testable.

    Usage:
        engine = TimelineLayoutEngine()
        engine.configure(tracks, state, viewport_width, viewport_height, pixels_per_frame)
        rect = engine.track_rect("v1")
        x = engine.frame_to_x(100)
    """

    def __init__(self) -> None:
        self._tracks: List[TrackInfo] = []
        self._video_tracks: List[TrackInfo] = []
        self._audio_tracks: List[TrackInfo] = []
        self._subtitle_tracks: List[TrackInfo] = []  # P10.3
        self._state = TimelineLayoutState()
        self._viewport_width: float = 800.0
        self._viewport_height: float = 400.0
        self._pixels_per_frame: float = 5.0
        self._track_area_height: float = 0.0  # available height for tracks (below ruler)
        self._dirty: bool = True
        self._area = AreaLayout()
        # Cached per-track rects keyed by track_id
        self._track_rects: Dict[str, QRectF] = {}
        self._track_tops: Dict[str, float] = {}

    # ── Configuration ────────────────────────────────────────────────────────

    def configure(
        self,
        tracks: List[TrackInfo],
        state: TimelineLayoutState,
        viewport_width: float,
        viewport_height: float,
        pixels_per_frame: float,
    ) -> None:
        """(Re)configure the engine — call whenever tracks, state, zoom, or size change."""
        self._tracks = tracks
        self._video_tracks = [t for t in tracks if t.track_type == "video"]
        self._audio_tracks = [t for t in tracks if t.track_type == "audio"]
        self._subtitle_tracks = [t for t in tracks if t.track_type == "subtitle"]
        self._state = state
        self._viewport_width = viewport_width
        self._viewport_height = viewport_height
        # AUD-21204: refuse zero/negative zoom (division-by-zero / mirrored
        # coordinates in x_to_frame); keep the previous value instead.
        if pixels_per_frame > 0:
            self._pixels_per_frame = pixels_per_frame
        else:
            logger.warning(
                "TimelineLayoutEngine.configure: ignoring invalid "
                f"pixels_per_frame {pixels_per_frame!r}"
            )

        # Fill default heights
        for t in self._tracks:
            if t.default_height == 0:
                if t.track_type == "audio":
                    t.default_height = DEFAULT_AUDIO_TRACK_HEIGHT
                elif t.track_type == "subtitle":
                    t.default_height = DEFAULT_SUBTITLE_TRACK_HEIGHT
                else:
                    t.default_height = DEFAULT_VIDEO_TRACK_HEIGHT

        self._track_area_height = max(0.0, viewport_height - RULER_HEIGHT)
        self._dirty = True

    def set_pixels_per_frame(self, ppf: float) -> None:
        # AUD-21204: a zero or negative zoom would make x_to_frame divide by
        # zero (ppf == 0) or produce a mirrored coordinate system (ppf < 0).
        # Reject it and keep the previous value.
        if ppf <= 0:
            logger.warning(
                f"TimelineLayoutEngine: ignoring invalid pixels_per_frame {ppf!r}"
            )
            return
        if self._pixels_per_frame != ppf:
            self._pixels_per_frame = ppf
            # No need to recompute track rects; X mapping is independent
            # but mark dirty for scene rect update convenience.
            self._dirty = True

    def set_viewport_size(self, w: float, h: float) -> None:
        if self._viewport_width != w or self._viewport_height != h:
            self._viewport_width = w
            self._viewport_height = h
            self._track_area_height = max(0.0, h - RULER_HEIGHT)
            self._dirty = True

    def set_state(self, state: TimelineLayoutState) -> None:
        self._state = state
        self._dirty = True

    @property
    def state(self) -> TimelineLayoutState:
        return self._state

    # ── Area computation ──────────────────────────────────────────────────────

    def _ensure_computed(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        self._compute_areas()
        self._compute_track_rects()

    def _compute_areas(self) -> None:
        """Compute the three area rectangles.

        Vertical order (top → bottom):
            [subtitle area]  ← visual overlay layer (rendered on top of video)
            [S/V splitter]
            [video area]     ← V1 near V/A splitter, V3 at top of video area
            [V/A splitter]
            [audio area]     ← A1 near V/A splitter, A3 at bottom

        Subtitles sit ABOVE video because they are visual overlays that render
        on top of the picture — matching DaVinci Resolve / Final Cut Pro.
        """
        area_h = self._track_area_height
        has_sub = bool(self._subtitle_tracks)
        usable = max(0.0, area_h - SPLITTER_HEIGHT)
        ratio = self._state.video_area_ratio

        # A caption lane belongs to the video panel: it stays immediately
        # above the video stack and moves with it when the V/A divider moves.
        # This avoids both the old oversized subtitle reservation and a dead,
        # non-resizable video panel.
        sub_h = self._stack_height(self._subtitle_tracks) if has_sub else 0.0
        # SUB-P1-06: cap the visible subtitle lane so that many cue tracks
        # overflow and become scrollable via the dedicated subtitle scrollbar
        # instead of growing unbounded and squeezing the video area.
        if has_sub:
            sub_cap = max(float(MIN_AREA_HEIGHT), usable * 0.4)
            sub_h = min(sub_h, sub_cap)
        video_stack_h = self._stack_height(self.ordered_video_tracks())
        if has_sub:
            subtitle_gap = TRACK_GAP if self._video_tracks else 0.0
            video_group_h = sub_h + subtitle_gap + video_stack_h
            max_video_h = max(MIN_AREA_HEIGHT, usable - MIN_AREA_HEIGHT)
            min_video_h = min(max_video_h, max(MIN_AREA_HEIGHT, video_group_h))
            video_h = max(usable * ratio, min_video_h)
        else:
            subtitle_gap = 0.0
            video_group_h = 0.0
            video_h = max(usable * ratio, MIN_AREA_HEIGHT)
        audio_h = max(usable - video_h, MIN_AREA_HEIGHT)

        top = TRACK_AREA_TOP
        total_w = self._total_width()

        if has_sub:
            # The V/A divider is still resizable. Subtitle and video tracks
            # are bottom-anchored together inside the video panel, with the
            # compact subtitle lane directly above the top video track.
            video_top = top
            va_splitter_top = video_top + video_h
            # AUD-21200: 组高超过面板上限（max_video_h）时，无钳制的
            # subtitle_top 会低于 TRACK_AREA_TOP，字幕/最上方视频轨被画进
            # 标尺区且 track_at_y 对该区域返回 "ruler"。钳制到标尺下沿，
            # 溢出部分改由 video_scroll_offset 在 _compute_track_rects 滚动。
            subtitle_top = max(
                TRACK_AREA_TOP, va_splitter_top - video_group_h)
            sv_splitter_top = subtitle_top + sub_h
            audio_top = va_splitter_top + SPLITTER_HEIGHT
            total_h = video_h + SPLITTER_HEIGHT + audio_h
        else:
            sv_splitter_top = 0.0
            video_top = top
            va_splitter_top = video_top + video_h
            audio_top = va_splitter_top + SPLITTER_HEIGHT
            total_h = video_h + SPLITTER_HEIGHT + audio_h

        self._area = AreaLayout(
            subtitle_area=QRectF(0, subtitle_top if has_sub else 0.0, total_w,
                                 sub_h if has_sub else 0.0),
            subtitle_splitter_rect=QRectF(0, sv_splitter_top, total_w,
                                          subtitle_gap if has_sub else 0.0),
            video_area=QRectF(0, video_top, total_w, video_h),
            splitter_rect=QRectF(0, va_splitter_top, total_w, SPLITTER_HEIGHT),
            audio_area=QRectF(0, audio_top, total_w, audio_h),
            total_track_area=QRectF(0, top, total_w, total_h),
        )

    def _compute_track_rects(self) -> None:
        self._track_rects.clear()
        self._track_tops.clear()

        # P10.3: Subtitle tracks — top-to-bottom (S1 at top, S2 below, etc.)
        # Subtitle and video tracks form one group anchored to the V/A divider.
        full_sub_stack_h = self._stack_height(self._subtitle_tracks)
        if self._subtitle_tracks:
            sub_offset = max(0.0, min(
                max(0.0, full_sub_stack_h - self._area.subtitle_area.height()),
                float(self._state.subtitle_scroll_offset),
            ))
            y = self._area.subtitle_area.top() - sub_offset
            for t in self._subtitle_tracks:
                h = self._height_for(t)
                self._track_tops[t.track_id] = y
                self._track_rects[t.track_id] = QRectF(0, y, self._total_width(), h)
                y += h + TRACK_GAP

        # Video tracks: V1 near V/A splitter (bottom of video area), then V2, V3 upward.
        # With subtitles present, start immediately after the compact S/V gap
        # so the two stacks move together as the V/A divider is dragged.
        video_ordered = list(reversed(self._video_tracks))
        full_video_stack_h = self._stack_height(video_ordered)
        if self._subtitle_tracks:
            # AUD-21200: 字幕在场时同样尊重 video_scroll_offset。视频栈的
            # 可用区间是 [S/V 分隔条底, V/A 分隔条顶]；栈高超过该区间时
            # 按 offset 钳制滚动（-1 保持"底部对齐"语义），否则视频滚动条
            # 只改 state 不改布局（死滚动条），溢出轨道也无法查看。
            va_top = self._area.splitter_rect.top()
            sv_bottom = self._area.subtitle_splitter_rect.bottom()
            avail_h = max(0.0, va_top - sv_bottom)
            if full_video_stack_h <= avail_h:
                y = va_top - full_video_stack_h
            else:
                max_scroll = full_video_stack_h - avail_h
                offset = (
                    max_scroll
                    if self._state.video_scroll_offset < 0
                    else self._state.video_scroll_offset
                )
                offset = max(0.0, min(max_scroll, float(offset)))
                y = sv_bottom - offset
        elif full_video_stack_h <= self._area.video_area.height():
            y = self._area.splitter_rect.top() - full_video_stack_h
        else:
            max_scroll = max(0.0, full_video_stack_h - self._area.video_area.height())
            offset = max_scroll if self._state.video_scroll_offset < 0 else self._state.video_scroll_offset
            offset = max(0.0, min(max_scroll, float(offset)))
            y = self._area.video_area.top() - offset
        for t in video_ordered:
            h = self._height_for(t)
            self._track_tops[t.track_id] = y
            self._track_rects[t.track_id] = QRectF(0, y, self._total_width(), h)
            y += h + TRACK_GAP

        # Audio tracks: A1 near V/A splitter (top of audio area), then A2, A3 downward.
        full_audio_stack_h = self._stack_height(self._audio_tracks)
        audio_offset = max(0.0, min(
            max(0.0, full_audio_stack_h - self._area.audio_area.height()),
            float(self._state.audio_scroll_offset),
        ))
        y = self._area.audio_area.top() - audio_offset
        for t in self._audio_tracks:
            h = self._height_for(t)
            self._track_tops[t.track_id] = y
            self._track_rects[t.track_id] = QRectF(0, y, self._total_width(), h)
            y += h + TRACK_GAP

    def _stack_height(self, tracks: List[TrackInfo]) -> float:
        return (
            sum(self._height_for(t) for t in tracks)
            + TRACK_GAP * max(0, len(tracks) - 1)
        )

    def scroll_range_for_area(self, track_type: str) -> Tuple[int, int, int]:
        """Return (minimum, maximum, page_step) for an independent V/A/S scrollbar."""
        self._ensure_computed()
        if track_type == "video":
            tracks = self.ordered_video_tracks()
            if self._subtitle_tracks:
                # AUD-21200: 字幕轨道占据面板上部；视频栈的可滚动区间是
                # [S/V 分隔条底, V/A 分隔条顶]，滚动范围必须与
                # _compute_track_rects 的钳制口径一致，否则滚动条启用后
                # 拖动不改布局（死滚动条）。
                area_h = max(
                    0.0,
                    self._area.splitter_rect.top()
                    - self._area.subtitle_splitter_rect.bottom(),
                )
            else:
                area_h = self._area.video_area.height()
        elif track_type == "subtitle":
            tracks = self.ordered_subtitle_tracks()
            area_h = self._area.subtitle_area.height()
        else:  # audio
            tracks = self.ordered_audio_tracks()
            area_h = self._area.audio_area.height()
        max_scroll = int(math.ceil(max(0.0, self._stack_height(tracks) - area_h)))
        return (0, max_scroll, max(1, int(area_h)))

    def _height_for(self, track: TrackInfo) -> int:
        h = self._state.track_heights.get(track.track_id)
        if h is not None:
            # Clamp by track type
            if track.track_type == "video":
                return max(MIN_VIDEO_TRACK_HEIGHT, min(MAX_VIDEO_TRACK_HEIGHT, h))
            if track.track_type == "subtitle":
                # SUB-P1-06: honor the stored height (preset/drag) within the
                # compact subtitle range instead of forcing DEFAULT always.
                return max(MIN_SUBTITLE_TRACK_HEIGHT, min(MAX_SUBTITLE_TRACK_HEIGHT, h))
            return max(MIN_AUDIO_TRACK_HEIGHT, min(MAX_AUDIO_TRACK_HEIGHT, h))
        return track.default_height

    def _total_width(self) -> float:
        return max(self._viewport_width, 2000.0)

    # ── Public queries ───────────────────────────────────────────────────────

    def track_rect(self, track_id: str) -> QRectF:
        """Full track rect in scene coordinates."""
        self._ensure_computed()
        return self._track_rects.get(track_id, QRectF())

    def track_top(self, track_id: str) -> float:
        """Top Y of a track in scene coordinates."""
        self._ensure_computed()
        return self._track_tops.get(track_id, TRACK_AREA_TOP)

    def track_height(self, track_id: str) -> int:
        """Current height of a track."""
        self._ensure_computed()
        for t in self._tracks:
            if t.track_id == track_id:
                return self._height_for(t)
        return DEFAULT_VIDEO_TRACK_HEIGHT

    def set_track_height(self, track_id: str, height: int) -> None:
        """Update a single track height and mark dirty."""
        self._state.track_heights[track_id] = height
        self._dirty = True

    TRACK_BOUNDARY_THRESHOLD: int = 5  # pixels from track edge to count as boundary

    def track_boundary_at_y(
        self, scene_y: float, threshold: int = 0,
    ) -> Optional[Tuple[str, float, str]]:
        """
        Return ``(track_id, boundary_y, edge)`` if *scene_y* is near a track boundary.

        Checks both top and bottom edges of every track.  The returned
        *track_id* is the track whose edge is closest to *scene_y*.

        *edge* is ``"top"`` or ``"bottom"`` indicating which edge matched.

        Returns ``None`` if scene_y is not within *threshold* pixels of any edge.
        """
        self._ensure_computed()
        th = threshold or self.TRACK_BOUNDARY_THRESHOLD

        best: Optional[Tuple[str, float, float, str]] = None  # (tid, boundary_y, dist, edge)
        for tid, rect in self._track_rects.items():
            # Check top edge
            top_y = rect.top()
            dist_top = abs(scene_y - top_y)
            if dist_top <= th and (best is None or dist_top < best[2]):
                best = (tid, float(top_y), dist_top, "top")
            # Check bottom edge
            bot_y = rect.bottom()
            dist_bot = abs(scene_y - bot_y)
            if dist_bot <= th and (best is None or dist_bot < best[2]):
                best = (tid, float(bot_y), dist_bot, "bottom")

        if best is not None:
            return (best[0], best[1], best[3])
        return None

    def track_at_y(self, scene_y: float) -> Optional[str]:
        """Return track_id at scene Y, or None if in splitter/ruler/empty area."""
        self._ensure_computed()
        _, area = self.track_at_y_with_area(scene_y)
        if area not in ("video", "audio", "subtitle"):
            return None
        for tid, rect in self._track_rects.items():
            if rect.top() <= scene_y < rect.bottom():
                if area == "video" and tid in {t.track_id for t in self._video_tracks}:
                    return tid
                if area == "audio" and tid in {t.track_id for t in self._audio_tracks}:
                    return tid
                if area == "subtitle" and tid in {t.track_id for t in self._subtitle_tracks}:
                    return tid
        return None

    def closest_track_at_y(
        self,
        scene_y: float,
        track_type: str,
        *,
        max_distance: float | None = None,
    ) -> Optional[str]:
        """
        Return the nearest track of *track_type* by vertical center.

        Dragging a clip between tracks should not require the pointer to land
        deep inside the target rectangle.  NLE timelines usually switch target
        lanes around the midpoint between two lane centers, which makes vertical
        moves feel much lighter than strict rectangle hit-testing.
        """
        self._ensure_computed()
        if track_type == "audio":
            tracks = self.ordered_audio_tracks()
        elif track_type == "subtitle":
            tracks = self.ordered_subtitle_tracks()
        else:
            tracks = self.ordered_video_tracks()

        best: Optional[tuple[float, str]] = None
        for track in tracks:
            rect = self._track_rects.get(track.track_id)
            if rect is None or rect.isNull():
                continue
            distance = abs(scene_y - rect.center().y())
            if best is None or distance < best[0]:
                best = (distance, track.track_id)

        if best is None:
            return None
        if max_distance is not None and best[0] > max_distance:
            return None
        return best[1]

    def track_at_y_with_area(self, scene_y: float) -> Tuple[Optional[str], str]:
        """
        Return (track_id, area_name) at scene Y.
        area_name is "video", "audio", "subtitle", "splitter", "ruler", or "empty".
        """
        self._ensure_computed()
        if scene_y < TRACK_AREA_TOP:
            return (None, "ruler")
        # V/A splitter
        if self._area.splitter_rect.top() <= scene_y < self._area.splitter_rect.bottom():
            return (None, "splitter")
        # S/V splitter (only present when subtitles exist)
        sr2 = self._area.subtitle_splitter_rect
        if sr2.height() > 0 and sr2.top() <= scene_y < sr2.bottom():
            return (None, "splitter")
        # Subtitle area sits inside the video panel, so it must be checked
        # before the enclosing video area.
        if self._area.subtitle_area.height() > 0:
            if self._area.subtitle_area.top() <= scene_y < self._area.subtitle_area.bottom():
                for t in self._subtitle_tracks:
                    rect = self._track_rects.get(t.track_id)
                    if rect is not None and rect.top() <= scene_y < rect.bottom():
                        return (t.track_id, "subtitle")
                return (None, "subtitle")
        if self._area.video_area.top() <= scene_y < self._area.video_area.bottom():
            for t in self._video_tracks:
                rect = self._track_rects.get(t.track_id)
                if rect is not None and rect.top() <= scene_y < rect.bottom():
                    return (t.track_id, "video")
            return (None, "video")
        if self._area.audio_area.top() <= scene_y < self._area.audio_area.bottom():
            for t in self._audio_tracks:
                rect = self._track_rects.get(t.track_id)
                if rect is not None and rect.top() <= scene_y < rect.bottom():
                    return (t.track_id, "audio")
            return (None, "audio")
        return (None, "empty")

    def area_layout(self) -> AreaLayout:
        """Return computed area rectangles."""
        self._ensure_computed()
        return self._area

    # ── Frame ↔ X mapping ────────────────────────────────────────────────────

    def frame_to_x(self, frame: int) -> float:
        """Convert frame number to scene X (accounting for header offset)."""
        return TRACK_HEADER_WIDTH + frame * self._pixels_per_frame

    def x_to_frame(self, x: float) -> int:
        """Convert scene X to frame number (rounded to nearest frame)."""
        return round((x - TRACK_HEADER_WIDTH) / self._pixels_per_frame)

    def frames_to_width(self, frames: int) -> float:
        """Convert frame count to pixel width."""
        return frames * self._pixels_per_frame

    # ── Splitter ─────────────────────────────────────────────────────────────

    def splitter_rect(self) -> QRectF:
        self._ensure_computed()
        return self._area.splitter_rect

    def is_in_splitter(self, scene_y: float) -> bool:
        """Return True if scene_y is inside ANY splitter (V/A or S/V)."""
        self._ensure_computed()
        sr = self._area.splitter_rect
        if sr.top() <= scene_y < sr.bottom():
            return True
        # P10.3: S/V splitter
        sr2 = self._area.subtitle_splitter_rect
        if sr2.height() > 0 and sr2.top() <= scene_y < sr2.bottom():
            return True
        return False

    def is_in_subtitle_splitter(self, scene_y: float) -> bool:
        """P10.3: Return True if scene_y is inside the S/V splitter only."""
        self._ensure_computed()
        sr2 = self._area.subtitle_splitter_rect
        return sr2.height() > 0 and sr2.top() <= scene_y < sr2.bottom()

    def subtitle_splitter_rect(self) -> QRectF:
        """P10.3: Return the S/V splitter rect."""
        self._ensure_computed()
        return self._area.subtitle_splitter_rect

    def set_splitter_ratio(self, ratio: float) -> None:
        """Set V/A area ratio (0.0..1.0, where 1.0 = all video)."""
        ratio = max(0.1, min(0.9, ratio))
        self._state.video_area_ratio = ratio
        self._dirty = True

    def set_subtitle_splitter_ratio(self, ratio: float) -> None:
        """P10.3: Set subtitle area ratio (fraction of non-video remainder)."""
        ratio = max(0.05, min(0.6, ratio))
        self._state.subtitle_area_ratio = ratio
        self._dirty = True

    def subtitle_splitter_ratio_from_y(self, scene_y: float) -> float:
        """Compute subtitle ratio from a proposed S/V splitter Y position.

        Subtitle tracks are above video, so the splitter moves down from
        ``TRACK_AREA_TOP`` through the non-video remainder while the video
        area keeps its current height for this drag.
        """
        self._ensure_computed()
        usable = self._track_area_height - 2.0 * SPLITTER_HEIGHT
        non_video_height = usable - self._area.video_area.height()
        if non_video_height <= 0:
            return SUBTITLE_AREA_RATIO
        ratio = (scene_y - TRACK_AREA_TOP) / non_video_height
        return max(0.05, min(0.6, ratio))

    def splitter_ratio_from_y(self, scene_y: float) -> float:
        """Compute ratio from a proposed splitter Y position."""
        usable = self._track_area_height - SPLITTER_HEIGHT
        if usable <= 0:
            return 0.5
        ratio = (scene_y - TRACK_AREA_TOP) / usable
        return max(0.1, min(0.9, ratio))

    # ── Scene rect ───────────────────────────────────────────────────────────

    def scene_rect(self) -> QRectF:
        """Recommended scene rect encompassing all content."""
        self._ensure_computed()
        bottom = self._area.total_track_area.bottom()
        for rect in self._track_rects.values():
            bottom = max(bottom, rect.bottom())
        return QRectF(
            0, 0,
            self._total_width(),
            bottom + 200,  # Extra padding for dragging audio clips below to create new tracks
        )

    # ── Ordered track lists ──────────────────────────────────────────────────

    def ordered_video_tracks(self) -> List[TrackInfo]:
        """Video tracks in display order: V3, V2, V1 (V1 near splitter)."""
        return list(reversed(self._video_tracks))

    def ordered_audio_tracks(self) -> List[TrackInfo]:
        """Audio tracks in display order: A1, A2, A3 (A1 near splitter)."""
        return list(self._audio_tracks)

    def ordered_subtitle_tracks(self) -> List[TrackInfo]:
        """Subtitle tracks in display order: S1, S2, S3 (S1 at top)."""
        return list(self._subtitle_tracks)

    def all_ordered_tracks(self) -> List[TrackInfo]:
        """All tracks in visual top-to-bottom order: subtitle → video → audio."""
        return (self.ordered_subtitle_tracks()
                + self.ordered_video_tracks()
                + self.ordered_audio_tracks())

    @property
    def pixels_per_frame(self) -> float:
        return self._pixels_per_frame

    @property
    def viewport_width(self) -> float:
        return self._viewport_width

    @property
    def viewport_height(self) -> float:
        return self._viewport_height

    # ── Unified hit-test ─────────────────────────────────────────────────────

    def resolve_timeline_hit(
        self,
        scene_x: float,
        scene_y: float,
        header_width: float | None = None,
        clip_id_at: "callable | None" = None,
    ) -> TimelineHit:
        """
        Resolve what's at (scene_x, scene_y) in the timeline scene.

        Args:
            scene_x: X in scene coordinates.
            scene_y: Y in scene coordinates.
            header_width: Width of the track-header column (defaults to TRACK_HEADER_WIDTH).
            clip_id_at: Optional callable(scene_x, scene_y, track_id) -> clip_id|None
                        to check for clip hits.

        Priority order (AUD-21203, aligned with is_in_splitter / the
        track_at_y_with_area convention): ruler zone > V/A splitter >
        S/V splitter > clip > track/header > empty (between or below tracks).
        """
        self._ensure_computed()
        hw = header_width if header_width is not None else TRACK_HEADER_WIDTH
        frame = self.x_to_frame(scene_x)

        # 1. Ruler area (above track area)
        if scene_y < TRACK_AREA_TOP:
            # Header column in ruler zone
            if scene_x < hw:
                return TimelineHit(area="ruler", reason="ruler-header corner")
            return TimelineHit(area="ruler", frame=frame)

        # 2. V/A splitter (highest priority inside the track zone)
        sr = self._area.splitter_rect
        if sr.top() <= scene_y < sr.bottom():
            return TimelineHit(area="splitter", frame=frame, reason="splitter")

        # 2b. S/V splitter (only present when subtitles exist) — AUD-21203:
        # previously only the V/A splitter was checked here, so an S/V
        # splitter hit fell through to "empty"/"between tracks" instead of
        # matching is_in_subtitle_splitter semantics.
        sr2 = self._area.subtitle_splitter_rect
        if sr2.height() > 0 and sr2.top() <= scene_y < sr2.bottom():
            return TimelineHit(area="splitter", frame=frame, reason="subtitle splitter")

        # 3. Track area — check each track
        for tid, rect in self._track_rects.items():
            if rect.top() <= scene_y < rect.bottom():
                # Header column
                if scene_x < hw:
                    return TimelineHit(area="header", track_id=tid, frame=frame)

                # Clip check (via optional callback)
                if clip_id_at is not None:
                    cid = clip_id_at(scene_x, scene_y, tid)
                    if cid:
                        return TimelineHit(
                            area="clip", track_id=tid, clip_id=cid, frame=frame
                        )

                # Track background
                return TimelineHit(area="track", track_id=tid, frame=frame)

        # 4. Empty space within total track area
        ta = self._area.total_track_area
        if ta.top() <= scene_y < ta.bottom():
            return TimelineHit(area="empty", frame=frame, reason="between tracks")

        # 5. Below everything
        return TimelineHit(area="empty", frame=frame, reason="below track area")

    # ── Unified drop-target resolution ───────────────────────────────────────

    def resolve_drop_target(
        self,
        scene_x: float,
        scene_y: float,
        media_type: str,
        locked_track_ids: "set[str] | None" = None,
    ) -> TimelineDropTarget:
        """
        Resolve the drop target for a given media type at (scene_x, scene_y).

        Args:
            scene_x: X in scene coordinates.
            scene_y: Y in scene coordinates.
            media_type: "video", "audio", or "image".
            locked_track_ids: Set of currently locked track IDs.

        Returns:
            TimelineDropTarget with is_valid=True if the drop is legal.
        """
        self._ensure_computed()
        locked = locked_track_ids or set()
        frame = self.x_to_frame(scene_x)
        desired_type = "video" if media_type in ("video", "image", "comp_clip", "motion_graphic") else "audio"

        # Check splitters — never a valid drop target
        if self.is_in_splitter(scene_y):
            return TimelineDropTarget(
                frame=frame, is_valid=False, reason="Cannot drop on splitter"
            )
        # P10.3: S/V splitter is also invalid (check via subtitle_splitter_rect)
        sr2 = self._area.subtitle_splitter_rect
        if sr2.height() > 0 and sr2.top() <= scene_y < sr2.bottom():
            return TimelineDropTarget(
                frame=frame, is_valid=False, reason="Cannot drop on splitter"
            )

        # Check if Y is in ruler area
        if scene_y < TRACK_AREA_TOP:
            return TimelineDropTarget(
                frame=frame, is_valid=False, reason="Cannot drop on ruler"
            )

        # P10.3: Subtitle area does not accept media drops (clips are created
        # via right-click context menu, not drag-and-drop in Phase 1).
        if self._area.subtitle_area.height() > 0:
            sa = self._area.subtitle_area
            if sa.top() <= scene_y < sa.bottom():
                return TimelineDropTarget(
                    frame=frame, is_valid=False,
                    reason="Subtitle tracks use right-click to add clips",
                )

        # Find track at Y
        hit_tid, hit_area = self.track_at_y_with_area(scene_y)

        if hit_tid is not None:
            # Check locked
            if hit_tid in locked:
                return TimelineDropTarget(
                    frame=frame, track_id=hit_tid, is_valid=False,
                    reason="Track is locked",
                )

            # Check type compatibility
            track_type = "video" if hit_area == "video" else "audio"
            if track_type != desired_type:
                # Wrong area — try to snap to correct area's default track
                fallback = self._default_track_for_type(desired_type)
                if fallback:
                    return TimelineDropTarget(
                        frame=frame, track_id=fallback,
                        track_type=desired_type, is_valid=True,
                        reason=f"Snapped from {hit_area} to {desired_type} target",
                    )
                return TimelineDropTarget(
                    frame=frame, is_valid=False,
                    track_type=desired_type,
                    reason=f"No {desired_type} track available",
                    create_track=True,
                )

            return TimelineDropTarget(
                frame=frame, track_id=hit_tid,
                track_type=desired_type, is_valid=True,
            )

        # No track at Y — check if we're in the right area to create a track
        if hit_area == "video" and desired_type == "video":
            return TimelineDropTarget(
                frame=frame, is_valid=False,
                track_type="video",
                reason="Empty video area — can create track",
                create_track=True,
            )
        if hit_area == "audio" and desired_type == "audio":
            return TimelineDropTarget(
                frame=frame, is_valid=False,
                track_type="audio",
                reason="Empty audio area — can create track",
                create_track=True,
            )

        # Wrong area or empty
        return TimelineDropTarget(
            frame=frame, is_valid=False,
            track_type=desired_type,
            reason=f"Cannot drop {media_type} here",
        )

    def _default_track_for_type(self, track_type: str) -> Optional[str]:
        """Return the first (default) track ID for a given type."""
        if track_type == "video":
            tracks = self._video_tracks
        elif track_type == "subtitle":
            tracks = self._subtitle_tracks
        else:
            tracks = self._audio_tracks
        if tracks:
            if track_type == "video":
                return self._video_tracks[0].track_id
            return tracks[0].track_id
        return None
