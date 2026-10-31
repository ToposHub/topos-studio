"""
Timeline Render Plan Module
时间线渲染计划计算模块。

## 职责

- TimelineRenderPlan: 计算给定帧需要渲染的内容
- FrameRenderPlan: 单帧渲染计划（可见 clip 列表、transition 信息）
- ClipLayerInfo: 单个 clip 在特定帧的渲染信息
- TransitionBlendInfo: transition 混合信息
- BlendMode: 混合模式枚举

## 设计原则

- 纯函数设计，不依赖 PyQt
- 完整类型注解
- 性能优化：只计算可见区域，使用缓存
- 线程安全：不可变数据类

## 功能

1. 给定 frame index，返回该帧可见的 clip 列表
2. 单轨：返回 clip + source frame 映射
3. 多轨：返回所有可见 clip 层（考虑 opacity/transform）
4. 有 transition 时返回混合信息
5. 考虑 clip enabled/disabled 状态
6. 考虑 track mute/solo 状态

## 多轨叠加规则

- 上方视频轨覆盖下方视频轨（track_index 大的在上层）
- opacity < 1.0 时进行 alpha blend
- disabled clip 不渲染
- muted audio track 不影响 video
- solo video track 存在时只渲染 solo track

## Transition 处理

- Cross Dissolve: 两 clip 按 blend factor 混合
- Dip To Color: left -> color -> right
- Wipe: 逐像素空间遮罩混合
- Motion Blur / Whip Pan: 逐像素空间遮罩混合，并在遮罩前应用方向性拖影
- Blur/Defocus/Film Dissolve: 全画面双源合成，不经过形状遮罩
- Smooth Cut: 全画面光流中间帧合成，适合相似构图和人物跳剪
"""

from __future__ import annotations

import re
from collections import OrderedDict
import bisect
import weakref

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Tuple

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence, TransitionInstance
from src.features.edit.core.transition_catalog import (
    FULL_FRAME_TRANSITION_TYPES,
    SPATIAL_TRANSITION_TYPES,
    is_spatial_transition,
)


# ── 混合模式枚举 ──


class BlendMode(Enum):
    """
    混合模式。

    定义层与层之间的混合方式。
    """
    NORMAL = "normal"           # 标准 alpha blend
    CROSS_DISSOLVE = "cross_dissolve"  # 两 clip 混合
    DIP_TO_COLOR = "dip_to_color"      # 通过颜色过渡
    WIPE = "wipe"               # 划像过渡（逐像素遮罩）


class TransitionType(Enum):
    """转场类型枚举"""
    CROSS_DISSOLVE = "cross_dissolve"
    DIP_TO_COLOR = "dip_to_color"
    BLUR_DISSOLVE = "blur_dissolve"
    DEFOCUS_DISSOLVE = "defocus_dissolve"
    FILM_DISSOLVE = "film_dissolve"
    SMOOTH_CUT = "smooth_cut"
    WIPE = "wipe"
    IRIS_CIRCLE = "iris_circle"
    IRIS_DIAMOND = "iris_diamond"
    IRIS_HEXAGON = "iris_hexagon"
    IRIS_PLUS = "iris_plus"
    IRIS_SQUARE = "iris_square"
    IRIS_ELLIPSE = "iris_ellipse"
    IRIS_TRIANGLE = "iris_triangle"
    IRIS_ARROW = "iris_arrow"
    IRIS_STAR = "iris_star"
    IRIS_HEART = "iris_heart"
    SLIDE_LEFT = "slide_left"
    SLIDE_RIGHT = "slide_right"
    SLIDE_UP = "slide_up"
    SLIDE_DOWN = "slide_down"
    MOTION_BLUR_LEFT = "motion_blur_left"
    MOTION_BLUR_RIGHT = "motion_blur_right"
    MOTION_BLUR_UP = "motion_blur_up"
    MOTION_BLUR_DOWN = "motion_blur_down"
    WHIP_PAN_LEFT = "whip_pan_left"
    WHIP_PAN_RIGHT = "whip_pan_right"
    ZOOM_BLUR_IN = "zoom_blur_in"
    ZOOM_BLUR_OUT = "zoom_blur_out"
    SPIN_BLUR_CW = "spin_blur_cw"
    SPIN_BLUR_CCW = "spin_blur_ccw"
    RGB_SPLIT_LEFT = "rgb_split_left"
    RGB_SPLIT_RIGHT = "rgb_split_right"


# ── 数据类定义 ──


@dataclass(frozen=True)
class ClipLayerInfo:
    """
    单个 clip 在特定帧的渲染信息。

    包含渲染该 clip 所需的所有信息。

    Attributes:
        clip: ClipInstance 引用
        source_frame: 对应的源帧号
        track_index: 轨道索引（用于叠加顺序）
        track_name: 轨道名称
        opacity: 该 clip 的透明度（已考虑 clip 自身 opacity）
        transform: 变换参数
        crop: 裁切参数
        is_in_transition: 是否在转场区域内
    """
    clip: ClipInstance
    source_frame: int
    track_index: int
    track_name: str
    opacity: float
    transform: tuple  # translate_x, translate_y, scale_x, scale_y, rotation, pivot_mode, pivot_x, pivot_y
    crop: tuple[float, float, float, float, float]  # left, right, top, bottom, feather
    is_in_transition: bool = False
    # Decoder cursor identity. Usually this is the clip id, but contiguous
    # same-source cuts share the first clip's stream to avoid a cold seek.
    decode_stream_key: str = ""

    @property
    def clip_id(self) -> str:
        """Clip ID"""
        return self.clip.clip_id

    @property
    def asset_id(self) -> str:
        """Asset ID"""
        return self.clip.asset_id

    @property
    def composite_id(self) -> str | None:
        """Composite graph ID bound to this clip, if any."""
        return self.clip.composite_id

    @property
    def composite_bypassed(self) -> bool:
        """Whether the composite is bypassed for this clip."""
        return getattr(self.clip, 'composite_bypassed', False)

    @property
    def decoder_stream_key(self) -> str:
        """Return the stable decoder-cursor identity for this layer."""
        return self.decode_stream_key or self.clip_id


@dataclass(frozen=True)
class TransitionBlendInfo:
    """
    转场混合信息。

    定义两个 clip 之间的转场效果，包含渲染所需的所有信息。

    Attributes:
        transition: TransitionInstance 引用
        left_clip: 左侧 clip（过渡前）
        right_clip: 右侧 clip（过渡后）
        blend_factor: 混合比例（0.0 = 完全左侧，1.0 = 完全右侧）
        left_source_frame: 左侧 clip 的源帧
        right_source_frame: 右侧 clip 的源帧
        left_opacity: 左侧 clip 在此帧的有效 opacity（考虑 transition）
        right_opacity: 右侧 clip 在此帧的有效 opacity（考虑 transition）
        blend_color: Dip To Color 的颜色值 (R, G, B)，默认黑色
        track_index: 轨道索引
        track_name: 轨道名称
    """
    transition: TransitionInstance
    left_clip: Optional[ClipInstance]
    right_clip: Optional[ClipInstance]
    blend_factor: float
    left_source_frame: int
    right_source_frame: int
    left_opacity: float = 1.0
    right_opacity: float = 1.0
    blend_color: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    track_index: int = 0
    track_name: str = ""

    @property
    def transition_type(self) -> str:
        """转场类型"""
        return self.transition.transition_type

    @property
    def transition_id(self) -> str:
        """转场 ID"""
        return self.transition.transition_id

    @property
    def left_clip_id(self) -> str:
        """左侧 clip ID"""
        return self.left_clip.clip_id if self.left_clip is not None else ""

    @property
    def right_clip_id(self) -> str:
        """右侧 clip ID"""
        return self.right_clip.clip_id if self.right_clip is not None else ""

    def get_blend_mode(self) -> BlendMode:
        """
        获取对应的 BlendMode。

        Returns:
            BlendMode 枚举值
        """
        type_map = {
            "cross_dissolve": BlendMode.CROSS_DISSOLVE,
            "dip_to_color": BlendMode.DIP_TO_COLOR,
            **{transition_type: BlendMode.WIPE for transition_type in SPATIAL_TRANSITION_TYPES},
            **{
                transition_type: BlendMode.CROSS_DISSOLVE
                for transition_type in FULL_FRAME_TRANSITION_TYPES
            },
        }
        mode = type_map.get(self.transition_type)
        if mode is not None:
            return mode
        # C2: 用户自定义转场按 base 的空间属性归入 WIPE / NORMAL
        from src.features.edit.core.custom_transitions import (
            is_user_transition_spatial,
        )
        user_spatial = is_user_transition_spatial(self.transition_type)
        return BlendMode.WIPE if user_spatial else BlendMode.NORMAL

    def get_color_factor(self) -> float:
        """
        获取 Dip To Color 的颜色混合比例。

        Dip To Color 的三阶段混合：
        - 0.0-0.5: left fade out, color fade in
        - 0.5: pure color
        - 0.5-1.0: color fade out, right fade in

        Returns:
            颜色的混合比例（0.0 = 无颜色，1.0 = 纯颜色）
        """
        if self.transition_type != "dip_to_color":
            return 0.0

        # 在过渡中间时颜色最强
        if self.blend_factor <= 0.5:
            # 从 left 到 color：颜色逐渐增强
            return self.blend_factor * 2.0
        else:
            # 从 color 到 right：颜色逐渐减弱
            return (1.0 - self.blend_factor) * 2.0

    def get_left_visibility(self) -> float:
        """
        获取左侧 clip 的可见度。

        对于 Cross Dissolve：直接按 blend_factor 计算
        对于 Dip To Color：0-0.5 逐渐消失

        Returns:
            左侧 clip 的可见比例
        """
        if self.transition_type == "cross_dissolve":
            return 1.0 - self.blend_factor
        elif self.transition_type == "dip_to_color":
            # 0-0.5: left 从 1.0 降到 0.0
            if self.blend_factor <= 0.5:
                return 1.0 - self.blend_factor * 2.0
            return 0.0
        else:
            return 1.0 - self.blend_factor

    def get_right_visibility(self) -> float:
        """
        获取右侧 clip 的可见度。

        对于 Cross Dissolve：直接按 blend_factor 计算
        对于 Dip To Color：0.5-1.0 逐渐出现

        Returns:
            右侧 clip 的可见比例
        """
        if self.transition_type == "cross_dissolve":
            return self.blend_factor
        elif self.transition_type == "dip_to_color":
            # 0.5-1.0: right 从 0.0 升到 1.0
            if self.blend_factor >= 0.5:
                return (self.blend_factor - 0.5) * 2.0
            return 0.0
        else:
            return self.blend_factor


@dataclass(frozen=True)
class FrameRenderPlan:
    """
    单帧渲染计划。

    包含渲染某一帧所需的所有信息。

    Attributes:
        frame: 时间线帧号
        layers: 可见 clip 层列表（按轨道顺序，从下到上）。注意：调节片段
            (Adjustment Clip) 的 layer 也包含在此列表中（render plan 无法
            访问 project 来按 media_type 过滤）；消费者 (preview renderer)
            负责在构建 GPU 任务时按 asset_type 把 adjustment layer 分离到
            ``adjustment_layers``，避免被当作普通素材解码。
        transitions: 该帧的转场混合信息列表
        has_content: 该帧是否有可见内容
        solo_active: 是否有 solo 轨道激活
    """
    frame: int
    layers: tuple[ClipLayerInfo, ...]
    transitions: tuple[TransitionBlendInfo, ...]
    has_content: bool
    solo_active: bool

    @property
    def layer_count(self) -> int:
        """可见层数量"""
        return len(self.layers)

    @property
    def transition_count(self) -> int:
        """转场数量"""
        return len(self.transitions)

    def get_layers_by_track(self, track_id: str) -> tuple[ClipLayerInfo, ...]:
        """获取指定轨道的所有层"""
        return tuple(layer for layer in self.layers if layer.clip.track_id == track_id)


# ── 辅助函数 ──


def get_effective_speed(clip: ClipInstance) -> float:
    """
    获取 clip 的有效播放速度。

    Delegates to clip.effective_speed().

    Args:
        clip: ClipInstance

    Returns:
        播放速度（1.0 = 正常速度）
    """
    return clip.effective_speed()


def calculate_source_frame(clip: ClipInstance, timeline_frame: int) -> int:
    """
    计算给定时间线帧对应的源帧。

    Delegates to clip.timeline_to_source_frame().

    Args:
        clip: ClipInstance
        timeline_frame: 时间线帧号

    Returns:
        源帧号

    Raises:
        ValueError: 帧不在 clip 范围内
    """
    return clip.timeline_to_source_frame(timeline_frame)


def is_frame_in_clip(clip: ClipInstance, frame: int) -> bool:
    """
    判断帧是否在 clip 范围内。

    Args:
        clip: ClipInstance
        frame: 时间线帧号

    Returns:
        是否在范围内
    """
    return clip.timeline_start <= frame < clip.timeline_end


class _TransitionIndex:
    """F06（2026-09-27）：轨道转场区间索引（按 start 排序 + 前缀最大 end）。

    查询 = 二分 + 前缀最大 end 剪枝的回扫：无转场覆盖的帧（绝大多数）
    O(log n) 即判空；命中时只回扫到前缀最大 end 仍 > frame 的深度，
    收集全部包含区间后取原始序最小者——与历史线性扫描"元组序首个
    命中"语义逐项一致（重叠转场优先规则 = 声明序）。
    """

    __slots__ = ("transitions_tuple", "order", "starts", "ends", "prefix_max_end")

    def __init__(self, transitions: tuple) -> None:
        order = sorted(range(len(transitions)),
                       key=lambda i: (transitions[i].start_frame, i))
        self.transitions_tuple = transitions
        self.order = order
        self.starts = [transitions[i].start_frame for i in order]
        self.ends = [transitions[i].end_frame for i in order]
        prefix_max_end: list[int] = []
        current_max = -1
        for end in self.ends:
            if end > current_max:
                current_max = end
            prefix_max_end.append(current_max)
        self.prefix_max_end = prefix_max_end


_TRANSITION_INDEX_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _transition_index(track: EditTrack) -> _TransitionIndex:
    """按 track 实例缓存的转场索引；transitions 元组被替换时重建。"""
    index = _TRANSITION_INDEX_CACHE.get(track)
    if index is None or index.transitions_tuple is not track.transitions:
        index = _TransitionIndex(track.transitions)
        _TRANSITION_INDEX_CACHE[track] = index
    return index


def get_transition_at_frame(
    track: EditTrack,
    frame: int
) -> Optional[TransitionInstance]:
    """
    获取轨道上指定帧处的转场。

    F06：按 start 排序索引二分定位 + 前缀最大 end 剪枝回扫——
    历史对每帧线性扫描全部转场（2000 转场末尾查询 ≈0.167 ms/次，
    全片外推随转场数线性膨胀）。命中语义与线性扫描一致。

    Args:
        track: EditTrack
        frame: 时间线帧号

    Returns:
        TransitionInstance 或 None
    """
    transitions = track.transitions
    if not transitions:
        return None
    index = _transition_index(track)
    p = bisect.bisect_right(index.starts, frame) - 1
    if p < 0:
        return None
    if index.prefix_max_end[p] <= frame:
        return None  # start ≤ frame 的所有转场 end 均 ≤ frame：无候选
    best: Optional[TransitionInstance] = None
    best_order: Optional[int] = None
    q = p
    while q >= 0 and index.prefix_max_end[q] > frame:
        original = index.order[q]
        trans = transitions[original]
        if trans.start_frame <= frame < trans.end_frame:
            if best_order is None or original < best_order:
                best_order = original
                best = trans
        q -= 1
    return best


def calculate_transition_blend(
    transition: TransitionInstance,
    frame: int
) -> float:
    """
    计算转场在指定帧的混合比例。

    Args:
        transition: TransitionInstance
        frame: 时间线帧号

    Returns:
        混合比例（0.0 = 完全左侧，1.0 = 完全右侧）
    """
    if transition.duration <= 0:
        return 1.0

    # Keep the established NLE convention used by the existing timeline
    # model: the transition's end frame is exclusive, so the final sampled
    # frame is just below 1.0. This also keeps the cut-centered frame exactly
    # at 0.5 for even durations.
    relative_pos = frame - transition.start_frame
    progress = max(0.0, min(1.0, relative_pos / transition.duration))

    curve = str((transition.params or {}).get("curve", "linear")).lower()
    if curve == "ease_in":
        return progress * progress
    if curve == "ease_out":
        return 1.0 - (1.0 - progress) * (1.0 - progress)
    if curve == "ease_in_out":
        return progress * progress * (3.0 - 2.0 * progress)
    return progress


def is_track_renderable(track: EditTrack, has_solo: bool) -> bool:
    """
    判断轨道是否应该渲染。

    考虑以下条件：
    - 轨道是否可见
    - 轨道是否启用
    - 轨道是否静音
    - 是否有 solo 轨道激活

    Args:
        track: EditTrack
        has_solo: 是否有 solo 轨道激活

    Returns:
        是否应该渲染
    """
    # 轨道不可见时不渲染
    if not track.is_visible:
        return False

    # 轨道禁用时不渲染
    if not track.is_enabled:
        return False

    # 静音轨道不渲染
    if track.is_muted:
        return False

    # 如果有 solo 轨道，只有 solo 轨道渲染
    if has_solo and not track.is_solo:
        return False

    return True


# ── TimelineRenderPlan 类 ──


class TimelineRenderPlan:
    """
    时间线渲染计划计算器。

    计算给定帧需要渲染的内容，包括：
    - 可见 clip 列表
    - Source frame 映射
    - Transition 混合信息
    - 轨道叠加顺序

    使用缓存优化性能，避免重复计算。

    Example:
        >>> plan = TimelineRenderPlan(sequence)
        >>> frame_plan = plan.get_frame_plan(100)
        >>> for layer in frame_plan.layers:
        ...     print(f"Clip {layer.clip_id} at source frame {layer.source_frame}")
    """

    def __init__(self, sequence: TimelineSequence) -> None:
        """
        初始化渲染计划计算器。

        Args:
            sequence: TimelineSequence 引用
        """
        self._sequence = sequence

        # 缓存：frame -> FrameRenderPlan。长会话里逐帧渲染会无限增长
        # （1 小时 25fps 时间线 = 9 万条），用 OrderedDict 做 LRU 上限。
        self._frame_cache: OrderedDict[int, FrameRenderPlan] = OrderedDict()
        self._frame_cache_max: int = 4096

        # 缓存：track_id -> (sorted clips list)
        self._track_clip_cache: dict[str, tuple[ClipInstance, ...]] = {}

        # 缓存：track_id -> clip_id -> decoder stream key. Building this once
        # keeps a timeline containing many blade cuts O(1) per rendered frame.
        self._track_decode_stream_key_cache: dict[str, dict[str, str]] = {}

        # 缓存：是否有 solo 轨道
        self._has_solo_cache: bool | None = None

    @property
    def sequence(self) -> TimelineSequence:
        """TimelineSequence 引用"""
        return self._sequence

    @property
    def duration_frames(self) -> int:
        """时间线总帧数"""
        return self._sequence.duration_frames

    def clear_cache(self) -> None:
        """清除所有缓存"""
        self._frame_cache.clear()
        self._track_clip_cache.clear()
        self._track_decode_stream_key_cache.clear()
        self._has_solo_cache = None

    # ── 主要接口 ──

    def get_frame_plan(self, frame: int) -> FrameRenderPlan:
        """
        获取指定帧的渲染计划。

        Args:
            frame: 时间线帧号

        Returns:
            FrameRenderPlan 包含所有渲染信息
        """
        # 检查缓存（LRU：命中移到末尾）
        if frame in self._frame_cache:
            self._frame_cache.move_to_end(frame)
            return self._frame_cache[frame]

        # 计算
        plan = self._compute_frame_plan(frame)

        # 缓存（超上限淘汰最旧）
        self._frame_cache[frame] = plan
        while len(self._frame_cache) > self._frame_cache_max:
            self._frame_cache.popitem(last=False)

        return plan

    def get_visible_clips(self, frame: int) -> tuple[ClipInstance, ...]:
        """
        获取指定帧可见的所有 clip。

        Args:
            frame: 时间线帧号

        Returns:
            可见 clip 元组
        """
        plan = self.get_frame_plan(frame)
        return tuple(layer.clip for layer in plan.layers)

    def get_source_frame(self, clip: ClipInstance, timeline_frame: int) -> int:
        """
        计算 clip 在指定时间线帧对应的源帧。

        Args:
            clip: ClipInstance
            timeline_frame: 时间线帧号

        Returns:
            源帧号

        Raises:
            ValueError: 帧不在 clip 范围内
        """
        return calculate_source_frame(clip, timeline_frame)

    # ── 缓存辅助方法 ──

    def _has_solo_track(self) -> bool:
        """检查是否有 solo 轨道"""
        if self._has_solo_cache is not None:
            return self._has_solo_cache

        for track in self._sequence.all_tracks:
            if track.is_solo:
                self._has_solo_cache = True
                return True

        self._has_solo_cache = False
        return False

    def _get_sorted_clips(self, track: EditTrack) -> tuple[ClipInstance, ...]:
        """获取轨道上按时间排序的 clip 列表（缓存）"""
        if track.track_id not in self._track_clip_cache:
            sorted_clips = tuple(sorted(track.clips, key=lambda c: c.timeline_start))
            self._track_clip_cache[track.track_id] = sorted_clips
        return self._track_clip_cache[track.track_id]

    def _decode_stream_key_for_clip(
        self,
        track: EditTrack,
        clip: ClipInstance,
    ) -> str:
        """Return the decoder stream shared by one contiguous source run.

        Editorial clip IDs remain distinct. Decoder cursors are separate: a
        blade split of forward-contiguous source frames can reuse the left
        clip's cursor, while gaps, speed/PTS discontinuities, and different
        assets retain isolated streams.
        """
        stream_keys = self._track_decode_stream_key_cache.get(track.track_id)
        if stream_keys is None:
            stream_keys = {}
            previous: ClipInstance | None = None
            for candidate in self._get_sorted_clips(track):
                if (
                    previous is not None
                    and self._clips_share_forward_decode_cursor(previous, candidate)
                ):
                    stream_keys[candidate.clip_id] = stream_keys[previous.clip_id]
                else:
                    stream_keys[candidate.clip_id] = candidate.clip_id
                previous = candidate
            self._track_decode_stream_key_cache[track.track_id] = stream_keys
        return stream_keys.get(clip.clip_id, clip.clip_id)

    @staticmethod
    def _clips_share_forward_decode_cursor(
        previous: ClipInstance,
        current: ClipInstance,
    ) -> bool:
        """Return whether two adjacent clips continue one forward cursor."""
        if (
            previous.asset_id != current.asset_id
            or previous.timeline_end != current.timeline_start
            or not previous.is_enabled
            or not current.is_enabled
        ):
            return False
        try:
            previous_last_source = calculate_source_frame(
                previous,
                previous.timeline_end - 1,
            )
            current_first_source = calculate_source_frame(
                current,
                current.timeline_start,
            )
        except ValueError:
            return False
        return current_first_source == previous_last_source + 1

    # ── 计算方法 ──

    def _compute_frame_plan(self, frame: int) -> FrameRenderPlan:
        """
        计算指定帧的渲染计划。

        Args:
            frame: 时间线帧号

        Returns:
            FrameRenderPlan
        """
        has_solo = self._has_solo_track()
        layers: list[ClipLayerInfo] = []
        transitions: list[TransitionBlendInfo] = []

        # 获取视频轨道（按索引排序，索引大的在上面）
        video_tracks = sorted(
            self._sequence.video_tracks,
            key=lambda t: t.index,
            reverse=False  # 索引小的在下层
        )

        # 遍历所有视频轨道
        for track in video_tracks:
            # 检查轨道是否应该渲染
            if not is_track_renderable(track, has_solo):
                continue

            # 获取该帧的 clip 和 transition
            track_layers, track_transitions = self._compute_track_at_frame(
                track, frame, has_solo
            )

            layers.extend(track_layers)
            transitions.extend(track_transitions)

        # 构建结果
        has_content = len(layers) > 0

        return FrameRenderPlan(
            frame=frame,
            layers=tuple(layers),
            transitions=tuple(transitions),
            has_content=has_content,
            solo_active=has_solo
        )

    def _compute_track_at_frame(
        self,
        track: EditTrack,
        frame: int,
        has_solo: bool
    ) -> tuple[list[ClipLayerInfo], list[TransitionBlendInfo]]:
        """
        计算轨道在指定帧的内容。

        转场区域处理：
        - 只添加在范围内的 clip 到 layers（用于定位当前帧的 clip）
        - transitions 信息包含两个 clip 的完整混合信息（用于渲染器合成）

        Args:
            track: EditTrack
            frame: 时间线帧号
            has_solo: 是否有 solo 轨道

        Returns:
            (ClipLayerInfo 列表, TransitionBlendInfo 列表)
        """
        layers: list[ClipLayerInfo] = []
        transitions: list[TransitionBlendInfo] = []

        # 检查是否有转场覆盖该帧
        transition = get_transition_at_frame(track, frame)

        if transition is not None:
            # 有转场：获取两个 clip 并计算混合
            trans_info = self._compute_transition_blend(transition, track, frame)
            if trans_info is not None:
                transitions.append(trans_info)

                # 只添加在范围内的 clip 到 layers
                # （transitions 已包含完整混合信息，渲染器可使用 transitions 进行合成）
                left_clip = track.get_clip_by_id(transition.left_clip_id)
                right_clip = track.get_clip_by_id(transition.right_clip_id)

                if left_clip is not None:
                    left_layer = self._create_layer_info_for_clip(
                        left_clip, track, frame, in_transition=True,
                        allow_transition_edge=True,
                    )
                    if left_layer is not None:
                        layers.append(left_layer)

                if right_clip is not None:
                    right_layer = self._create_layer_info_for_clip(
                        right_clip, track, frame, in_transition=True,
                        allow_transition_edge=True,
                    )
                    if right_layer is not None:
                        layers.append(right_layer)
        else:
            # 无转场：查找覆盖该帧的单个 clip
            clip = self._find_clip_at_frame(track, frame)
            if clip is not None and clip.is_enabled:
                layer = self._create_layer_info_for_clip(clip, track, frame, in_transition=False)
                if layer is not None:
                    layers.append(layer)

        return layers, transitions

    def _create_layer_info_for_clip(
        self,
        clip: ClipInstance,
        track: EditTrack,
        frame: int,
        in_transition: bool,
        allow_transition_edge: bool = False,
    ) -> Optional[ClipLayerInfo]:
        """
        为指定的 clip 创建 ClipLayerInfo。

        Args:
            clip: ClipInstance
            track: EditTrack
            frame: 时间线帧号
            in_transition: 是否在转场区域

        Returns:
            ClipLayerInfo 或 None
        """
        # A centered transition can extend across the cut even though the two
        # ClipInstances remain adjacent and non-overlapping on the timeline.
        # Sample the nearest valid edge frame in that case. This gives the
        # native renderer both inputs for every transition frame without
        # mutating editorial clip boundaries.
        sample_frame = frame
        if allow_transition_edge:
            sample_frame = max(clip.timeline_start, min(frame, clip.timeline_end - 1))

        # 检查 clip 是否启用
        if not clip.is_enabled:
            return None

        # 计算源帧
        try:
            source_frame = calculate_source_frame(clip, sample_frame)
        except ValueError:
            # 帧不在 clip 范围内
            return None

        # 获取变换参数 —— 评估关键帧动画（支持缓动曲线），
        # 若参数无关键帧则回退到 transform 静态值。
        # 像素空间模型（与合成页 TransformNode 一致）：translate/scale/
        # rotation 可动画，pivot_mode/pivot 用静态值。
        transform = clip.transform
        transform_tuple = (
            float(clip.get_param_at_frame("transform.translate_x", sample_frame)),
            float(clip.get_param_at_frame("transform.translate_y", sample_frame)),
            float(clip.get_param_at_frame("transform.scale_x", sample_frame)),
            float(clip.get_param_at_frame("transform.scale_y", sample_frame)),
            float(clip.get_param_at_frame("transform.rotation", sample_frame)),
            transform.pivot_mode,  # pivot_mode 不参与动画
            transform.pivot_x,     # pivot 不参与动画
            transform.pivot_y,
        )

        # 获取裁切参数
        crop = clip.crop
        crop_tuple = (
            crop.left,
            crop.right,
            crop.top,
            crop.bottom,
            crop.feather
        )

        # Calculate fade opacity factor based on clip's fade in/out
        fade_opacity = 1.0
        clip_relative = sample_frame - clip.timeline_start
        clip_duration = clip.timeline_duration
        if clip.fade_in_duration > 0 and clip_relative < clip.fade_in_duration:
            fade_opacity = min(fade_opacity, clip_relative / clip.fade_in_duration)
        if clip.fade_out_duration > 0:
            frames_from_end = clip_duration - clip_relative
            if frames_from_end < clip.fade_out_duration:
                fade_opacity = min(fade_opacity, frames_from_end / clip.fade_out_duration)
        fade_opacity = max(0.0, min(1.0, fade_opacity))

        # opacity 评估关键帧动画，再乘以 fade 因子
        animated_opacity = float(clip.get_param_at_frame("opacity", sample_frame))

        return ClipLayerInfo(
            clip=clip,
            source_frame=source_frame,
            track_index=track.index,
            track_name=track.name,
            opacity=animated_opacity * fade_opacity,
            transform=transform_tuple,
            crop=crop_tuple,
            is_in_transition=in_transition,
            decode_stream_key=self._decode_stream_key_for_clip(track, clip),
        )

    def _find_clip_at_frame(
        self,
        track: EditTrack,
        frame: int
    ) -> Optional[ClipInstance]:
        """
        在轨道上查找覆盖指定帧的 clip。

        使用二分查找优化性能。

        Args:
            track: EditTrack
            frame: 时间线帧号

        Returns:
            ClipInstance 或 None
        """
        sorted_clips = self._get_sorted_clips(track)

        # 二分查找
        left, right = 0, len(sorted_clips)
        while left < right:
            mid = (left + right) // 2
            clip = sorted_clips[mid]

            if frame < clip.timeline_start:
                right = mid
            elif frame >= clip.timeline_end:
                left = mid + 1
            else:
                return clip

        return None

    def _compute_transition_blend(
        self,
        transition: TransitionInstance,
        track: EditTrack,
        frame: int
    ) -> Optional[TransitionBlendInfo]:
        """
        计算转场混合信息。

        根据转场类型计算两个 clip 的混合参数：
        - Cross Dissolve: 直接按 blend_factor 混合
        - Dip To Color: 通过颜色过渡，提取颜色参数
        - Wipe: 逐像素空间遮罩混合（合成阶段应用 compute_wipe_mask）

        Args:
            transition: TransitionInstance
            track: EditTrack
            frame: 时间线帧号

        Returns:
            TransitionBlendInfo 或 None
        """
        left_clip = track.get_clip_by_id(transition.left_clip_id) if transition.left_clip_id else None
        right_clip = track.get_clip_by_id(transition.right_clip_id) if transition.right_clip_id else None

        if left_clip is None and right_clip is None:
            return None

        # 检查 clip 是否启用
        if (left_clip is not None and not left_clip.is_enabled) or (
            right_clip is not None and not right_clip.is_enabled
        ):
            return None

        # 计算混合比例
        blend_factor = calculate_transition_blend(transition, frame)

        # 计算源帧。转场边界帧允许采样相邻 clip 的首/尾帧，避免普通
        # adjacent clips 在 centered transition 中只剩一侧输入。
        left_source_frame = 0
        if left_clip is not None:
            try:
                left_source_frame = calculate_source_frame(
                    left_clip,
                    max(left_clip.timeline_start, min(frame, left_clip.timeline_end - 1)),
                )
            except ValueError:
                left_source_frame = left_clip.source_start

        right_source_frame = 0
        if right_clip is not None:
            try:
                right_source_frame = calculate_source_frame(
                    right_clip,
                    max(right_clip.timeline_start, min(frame, right_clip.timeline_end - 1)),
                )
            except ValueError:
                right_source_frame = right_clip.source_start

        # 提取颜色参数（Dip To Color）
        blend_color = (0.0, 0.0, 0.0)  # 默认黑色
        if transition.transition_type == "dip_to_color":
            color_params = transition.params.get("color", {"r": 0.0, "g": 0.0, "b": 0.0})
            # 支持多种颜色格式
            if isinstance(color_params, dict):
                blend_color = (
                    color_params.get("r", 0.0),
                    color_params.get("g", 0.0),
                    color_params.get("b", 0.0),
                )
            elif isinstance(color_params, (list, tuple)) and len(color_params) >= 3:
                blend_color = tuple(color_params[:3])
            elif isinstance(color_params, str):
                # 支持 "#RRGGBB" 格式
                blend_color = self._parse_color_string(color_params)

        # 计算有效的 opacity（考虑转场）
        # Cross Dissolve: opacity 由 blend_factor 决定
        # Dip To Color: opacity 由阶段决定
        # Wipe: 空间遮罩混合，opacity 全程保留（真实遮罩在合成阶段应用）
        left_base_opacity = self._get_clip_effective_opacity(left_clip, frame) if left_clip else 0.0
        right_base_opacity = self._get_clip_effective_opacity(right_clip, frame) if right_clip else 0.0
        if transition.transition_type == "cross_dissolve":
            left_opacity = left_base_opacity * (1.0 - blend_factor)
            right_opacity = right_base_opacity * blend_factor
        elif transition.transition_type == "dip_to_color":
            # Dip To Color 的三阶段
            if blend_factor <= 0.5:
                # 阶段 1: left fade out
                left_opacity = left_base_opacity * (1.0 - blend_factor * 2.0)
                right_opacity = 0.0
            else:
                # 阶段 2: right fade in
                left_opacity = 0.0
                right_opacity = right_base_opacity * (blend_factor - 0.5) * 2.0
        elif (
            transition.transition_type in SPATIAL_TRANSITION_TYPES
            or transition.transition_type in FULL_FRAME_TRANSITION_TYPES
            or is_spatial_transition(transition.transition_type)
        ):
            # 几何/全画面转场由 GPU procedural pass 在合成阶段混合，
            # 双方保持完整 opacity。
            left_opacity = left_base_opacity
            right_opacity = right_base_opacity
        else:
            # 未知类型回退为简单 blend
            left_opacity = left_base_opacity * (1.0 - blend_factor)
            right_opacity = right_base_opacity * blend_factor

        return TransitionBlendInfo(
            transition=transition,
            left_clip=left_clip,
            right_clip=right_clip,
            blend_factor=blend_factor,
            left_source_frame=left_source_frame,
            right_source_frame=right_source_frame,
            left_opacity=left_opacity,
            right_opacity=right_opacity,
            blend_color=blend_color,
            track_index=track.index,
            track_name=track.name
        )

    @staticmethod
    def _get_clip_effective_opacity(clip: ClipInstance, frame: int) -> float:
        """Evaluate clip opacity/fades for a transition sample frame."""
        sample_frame = max(clip.timeline_start, min(frame, clip.timeline_end - 1))
        clip_relative = sample_frame - clip.timeline_start
        fade_opacity = 1.0
        if clip.fade_in_duration > 0 and clip_relative < clip.fade_in_duration:
            fade_opacity = min(fade_opacity, clip_relative / clip.fade_in_duration)
        if clip.fade_out_duration > 0:
            frames_from_end = clip.timeline_duration - clip_relative
            if frames_from_end < clip.fade_out_duration:
                fade_opacity = min(fade_opacity, frames_from_end / clip.fade_out_duration)
        animated_opacity = float(clip.get_param_at_frame("opacity", sample_frame))
        return max(0.0, min(1.0, animated_opacity * fade_opacity))

    def _parse_color_string(self, color_str: str) -> Tuple[float, float, float]:
        """
        解析颜色字符串。

        支持 "#RRGGBB" 格式。

        Args:
            color_str: 颜色字符串

        Returns:
            (R, G, B) 元组，范围 0.0-1.0
        """
        if color_str.startswith("#") and len(color_str) == 7:
            try:
                r = int(color_str[1:3], 16) / 255.0
                g = int(color_str[3:5], 16) / 255.0
                b = int(color_str[5:7], 16) / 255.0
                return (r, g, b)
            except ValueError:
                return (0.0, 0.0, 0.0)
        return (0.0, 0.0, 0.0)

    # ── 批量查询接口 ──

    def get_frame_range_plan(
        self,
        start_frame: int,
        end_frame: int
    ) -> dict[int, FrameRenderPlan]:
        """
        获取帧范围内的渲染计划。

        Args:
            start_frame: 起始帧
            end_frame: 结束帧（不包含）

        Returns:
            帧 -> FrameRenderPlan 的字典
        """
        result: dict[int, FrameRenderPlan] = {}
        for frame in range(start_frame, end_frame):
            result[frame] = self.get_frame_plan(frame)
        return result

    def get_clip_frame_range(
        self,
        clip_id: str
    ) -> tuple[int, int]:
        """
        获取 clip 的时间线帧范围。

        Args:
            clip_id: Clip ID

        Returns:
            (起始帧, 结束帧) 或 (0, 0) 如果未找到
        """
        clip = self._sequence.get_clip_by_id(clip_id)
        if clip is None:
            return (0, 0)
        return (clip.timeline_start, clip.timeline_end)

    def find_clips_in_range(
        self,
        start_frame: int,
        end_frame: int,
        track_type: str = "video"
    ) -> tuple[ClipInstance, ...]:
        """
        查找帧范围内的所有 clip。

        Args:
            start_frame: 起始帧
            end_frame: 结束帧（不包含）
            track_type: 轨道类型 ("video" 或 "audio")

        Returns:
            ClipInstance 元组
        """
        tracks = (
            self._sequence.video_tracks
            if track_type == "video"
            else self._sequence.audio_tracks
        )

        result: list[ClipInstance] = []

        for track in tracks:
            for clip in track.clips:
                # 检查 clip 是否与范围重叠
                if clip.timeline_start < end_frame and clip.timeline_end > start_frame:
                    result.append(clip)

        return tuple(result)


# ---------------------------------------------------------------------------
# T-P1-10：TOPOS identity 直通交付计划
# ---------------------------------------------------------------------------

_TOPOS_DIRECT_PLANAR_RE = re.compile(
    r"^(?:yuv(?:422|444)|gbr)p(?:10|12|16)le$"
    r"|^topos_(?:yuva(?:422|444)p|gbrap)(?:10|12|16)a"
    r"(?:8|10|12|16)$"
)
_TOPOS_DIRECT_COLOR_DEPTH_RE = re.compile(
    r"p(10|12|16)(?:le|a(?:8|10|12|16))$"
)
_TOPOS_DIRECT_ALPHA_DEPTH_RE = re.compile(r"a(8|10|12|16)$")
_TOPOS_DIRECT_CFA_RE = re.compile(r"^topos_cfa(12|16)le$")


def _topos_direct_cfa_spec(pixel_format: str) -> Optional[int]:
    """Return the CFA bit depth for a TRAW 相位平面 format, else None.

    M4-R2：直通车道的 RAW→RAW 判定域——``topos_cfa12le``/``topos_cfa16le``
    之外一律 None（phase planes 无 alpha 位，返回值即位深）。
    """
    value = str(pixel_format or "").strip().lower()
    m = _TOPOS_DIRECT_CFA_RE.fullmatch(value)
    return int(m.group(1)) if m is not None else None


def _topos_direct_planar_spec(
    pixel_format: str,
) -> Optional[tuple[int, Optional[int]]]:
    """Return ``(color_depth, alpha_depth)`` for a supported planar format.

    This parser only validates the native planar domain.  The direct delivery
    planner may apply the small, explicit YUV resampling/bit-depth conversions
    implemented by the timeline path; GBR↔YUV remains outside that domain.
    """
    value = str(pixel_format or "").strip().lower()
    if _TOPOS_DIRECT_PLANAR_RE.fullmatch(value) is None:
        return None
    color_match = _TOPOS_DIRECT_COLOR_DEPTH_RE.search(value)
    if color_match is None:
        return None
    alpha_match = _TOPOS_DIRECT_ALPHA_DEPTH_RE.search(value)
    return (
        int(color_match.group(1)),
        int(alpha_match.group(1)) if alpha_match is not None else None,
    )


def _topos_direct_planar_layout(pixel_format: str) -> Optional[str]:
    """Return the planar color layout (``yuv422``, ``yuv444`` or ``gbr``)."""
    value = str(pixel_format or "").strip().lower()
    if value.startswith(("yuv422p", "topos_yuva422p")):
        return "yuv422"
    if value.startswith(("yuv444p", "topos_yuva444p")):
        return "yuv444"
    if value.startswith(("gbrp", "topos_gbrap")):
        return "gbr"
    return None

@dataclass(frozen=True)
class DirectToposDeliveryPlan:
    """单一 TOPOS clip → TOPOS 输出的 planar 直通计划（T-P1-10）。

    由 :func:`build_direct_topos_delivery_plan` 严格判定后构建；
    TimelineExporter 据此跳过逐帧渲染机器（render plan/provider/BGR），
    直接 ``ToposMediaSource.read_frame → ToposVideoEncoder.encode_frame``。
    语义约束：时间线帧 ``t`` 映射源帧 ``source_start + (t - timeline_start)``，
    输出帧数/fps/色彩元数据与源一致；源高于时间线时，允许严格等比例
    缩小，由 Topos native decoder 直接产出目标几何，避免完整源平面进入
    普通渲染器。
    """

    asset_path: str
    clip_id: str
    timeline_start: int
    source_start: int
    frames: int
    width: int
    height: int
    fps_num: int
    fps_den: int
    source_pixel_format: str
    source_bit_depth: int
    source_color_layout: str = ""
    target_pixel_format: str = ""
    target_color_layout: str = ""
    target_bit_depth: Optional[int] = None
    source_alpha_depth: Optional[int] = None
    target_alpha_depth: Optional[int] = None
    target_width: int = 0
    target_height: int = 0
    decoder_resize: bool = False
    # 平面重采样/位深量化会在每帧产生额外 CPU 工作；这类组合在
    # decode 与 encode 之间有可重叠的工作量，供导出器选择流水线。
    requires_planar_conversion: bool = False


def build_direct_topos_delivery_plan(
    sequence: TimelineSequence,
    project: Any,
    config: Any,
    encoder_config: Any,
    start_frame: int,
    end_frame: int,
) -> tuple[Optional[DirectToposDeliveryPlan], str]:
    """严格 eligibility 判定（第一版收缩，任一不满足返回 (None, reason)）。

    条件（总计划 T-P1-10）：单一启用视频轨、单一连续 clip、源/输出均为
    TOPOS 且 planar 颜色布局可由专用 YUV 重采样覆盖、输出 fps 与源一致，
    尺寸相同或为严格等比例缩小、无 effects、
    无 color stack、无 transform/crop/opacity/fade、无关键帧动画
    （animated_params）、无 transition、无字幕、
    无 Alpha 语义变化、无音频。range 必须被单一 clip 完整覆盖。

    这里允许不同 Topos profile（例如 HQ → Proxy），以及同一颜色布局内
    的颜色/Alpha 位深重标定（只在直通循环内做必要的整数域转换）；YUV
    4:2:2↔4:4:4 只使用专用水平重采样，不做 YUV↔GBR 语义转换。

    Returns:
        (plan, "") 或 (None, reason_code)。
    """
    import os as _os

    from src.features.edit.core.clip_params import CropParams, TransformParams

    # --- 输出必须是支持的 Topos planar 格式 ---
    codec_name = str(getattr(encoder_config, "codec", "")).lower()
    pix_fmt = str(getattr(encoder_config, "pix_fmt", "")).lower()
    if codec_name != "topos":
        return None, f"codec_not_topos:{codec_name}"
    # M4-R2：RAW→RAW CFA 直通分支——输出为 TRAW 相位平面时走独立判定域
    # （无 YUV 色度布局/Alpha 语义；fail-closed：本车道不做任何像素级
    # 处理，源/输出位深不同、尺寸不同一律拒绝）。
    out_cfa_depth = _topos_direct_cfa_spec(pix_fmt)
    output_spec = _topos_direct_planar_spec(pix_fmt)
    if out_cfa_depth is not None:
        output_layout = "cfa"
        output_has_alpha = False
        output_alpha_depth = None
        output_color_depth = out_cfa_depth
    else:
        if output_spec is None:
            return None, f"output_pix_fmt_not_supported:{pix_fmt}"
        output_layout = _topos_direct_planar_layout(pix_fmt)
        if output_layout is None:
            return None, f"output_pix_fmt_not_supported:{pix_fmt}"
        output_has_alpha = (
            output_spec[1] is not None
            or bool(getattr(encoder_config, "has_alpha", False))
        )
        # Topos 的裸 planar pix_fmt + has_alpha 语义使用产品默认 a12；显式
        # topos_*aN 格式则由名字声明 Alpha 位深。
        output_alpha_depth = output_spec[1] if output_has_alpha else None
        if output_has_alpha and output_alpha_depth is None:
            from src.shared.codec.topos_profiles import TOPOS_DEFAULT_ALPHA_BIT_DEPTH

            output_alpha_depth = int(TOPOS_DEFAULT_ALPHA_BIT_DEPTH)
        output_color_depth = output_spec[0]

    # --- alpha / 音频 ---
    # v1.1 音频轨接线（plan M-A6）：include_audio 不再拦截直通——导出循环
    # 有音频时渲染母带并喂入编码器（_export_direct_topos）。
    # alpha 拦截保留语义：请求 Alpha 而输出档带不了 Alpha 时不可直通；
    # 输出自身带 Alpha 的档位允许（alpha→alpha 位深重标定直通）。
    if bool(getattr(config, "has_alpha", False)) and not output_has_alpha:
        return None, "alpha_requested"

    # --- 字幕内容 ---
    for t in getattr(sequence, "subtitle_tracks", ()) or ():
        if any(getattr(c, "is_enabled", True)
               for c in getattr(t, "clips", ()) or ()):
            return None, "subtitles_present"

    # --- 单一启用视频轨、无转场、单一连续 clip 覆盖整个 range ---
    video_tracks = [
        track for track in getattr(sequence, "video_tracks", ()) or ()
        if bool(getattr(track, "is_enabled", True))
    ]
    if len(video_tracks) != 1:
        return None, f"video_track_count:{len(video_tracks)}"
    track = video_tracks[0]
    if getattr(track, "transitions", ()):
        return None, "transitions_present"
    clips = [
        clip for clip in getattr(track, "clips", ()) or ()
        if bool(getattr(clip, "is_enabled", True))
    ]
    if len(clips) != 1:
        return None, f"video_clip_count:{len(clips)}"
    clip = clips[0]
    if clip.timeline_start > start_frame or clip.timeline_end < end_frame:
        return None, "range_not_covered_by_single_clip"
    if get_effective_speed(clip) != 1.0:
        return None, "non_identity_speed"

    # --- clip 级效果/几何 ---
    if (getattr(clip, "effects", ())
            or getattr(clip, "color_stack_id", None)
            or getattr(clip, "composite_id", None)):
        return None, "clip_effects_present"
    if getattr(clip, "transform", None) != TransformParams():
        return None, "non_identity_transform"
    if getattr(clip, "crop", None) != CropParams():
        return None, "non_identity_crop"
    opacity = getattr(clip, "opacity", 1.0)
    try:
        if abs(float(opacity) - 1.0) > 1e-6:
            return None, "non_identity_opacity"
    except (TypeError, ValueError):
        return None, "non_identity_opacity"
    if (float(getattr(clip, "fade_in_duration", 0.0) or 0.0) > 0.0
            or float(getattr(clip, "fade_out_duration", 0.0) or 0.0) > 0.0):
        return None, "fades_present"
    # AUD-19000：关键帧动画（opacity/transform.* 等）。渲染路径按
    # get_param_at_frame 逐帧求值（关键帧优先于静态值），直通车道不消费
    # 任何动画参数——animated_params 非空即 fail-closed 拒绝，避免
    # 静态 opacity==1.0、transform 恒等的动画 clip 静默丢失动画。
    if getattr(clip, "animated_params", None):
        return None, "animated_params_present"

    # --- 源资产必须是可由直通转换器消费的 TOPOS planar 文件 ---
    asset = None
    if project is not None:
        try:
            asset = project.get_asset_by_id(clip.asset_id)
        except Exception:
            asset = None
    if asset is None or str(getattr(asset, "media_type", "")) != "video":
        return None, "non_video_clip"
    metadata = getattr(asset, "metadata", {}) or {}
    source_path = str(getattr(asset, "path", "") or "")
    if not source_path or not _os.path.isfile(source_path):
        return None, "source_file_missing"
    # 元数据不全（旧工程/外部构造）时用共享 probe 缓存补全——T-P0-10 之后
    # 同一文件签名只产生一次 native probe，不引入重复探测。
    required_keys = ("codec", "pixel_format", "width", "height",
                     "fps_num", "fps_den")
    if any(not metadata.get(key) for key in required_keys):
        try:
            from src.shared.media.factory import MediaSourceFactory

            probe = MediaSourceFactory.probe_video_metadata_cached(source_path)
            if probe.metadata is not None:
                merged = dict(metadata)
                vm = probe.metadata
                fps = getattr(vm, "fps", None)
                merged.setdefault("codec", vm.codec_name)
                merged.setdefault("pixel_format", vm.pixel_format)
                merged.setdefault("width", vm.width)
                merged.setdefault("height", vm.height)
                merged.setdefault("bit_depth", vm.bit_depth)
                if fps is not None:
                    merged.setdefault("fps_num", getattr(fps, "num", None))
                    merged.setdefault("fps_den", getattr(fps, "den", None))
                merged.setdefault("total_frames", vm.total_frames)
                metadata = merged
        except Exception:  # noqa: BLE001 — 补全失败按原元数据 fail-closed
            pass
    source_codec = str(metadata.get("codec", "") or "").lower()
    source_fmt = str(metadata.get("pixel_format", "") or "").strip().lower()
    source_cfa_depth = _topos_direct_cfa_spec(source_fmt)
    source_spec = _topos_direct_planar_spec(source_fmt)
    if source_cfa_depth is not None:
        # M4-R2：RAW 源——只允许 RAW→RAW 同位深直通（任何位深/布局换算、
        # planar 输出乃至 debayer 都属像素级处理，必须走渲染车道并显式
        # 选择非 RAW 档；此处拒绝保持直出物与源同几何/同相位/同位深
        # 语义——重编码仍按比率锚 qp 有界损失（非逐位复制；clip 级 RAW
        # 参数不参与直出，2026-09-21 复查注记）。
        if source_codec != "topos":
            return None, f"raw_source_requires_topos_container:{source_codec}"
        if out_cfa_depth is None:
            return None, f"raw_source_requires_raw_output:{source_fmt}"
        if source_cfa_depth != out_cfa_depth:
            return None, (
                f"raw_bit_depth_mismatch:{source_cfa_depth}:{out_cfa_depth}")
        source_layout = "cfa"
        source_color_depth = source_cfa_depth
    elif out_cfa_depth is not None:
        return None, f"raw_output_requires_raw_source:{source_fmt}"
    else:
        if source_codec != "topos" or source_spec is None:
            return None, f"source_not_topos_planar:{source_codec}:{source_fmt}"
        source_layout = _topos_direct_planar_layout(source_fmt)
        if source_layout != output_layout and {
            source_layout, output_layout
        } != {"yuv422", "yuv444"}:
            return None, f"source_format_mismatch:{source_fmt}:{pix_fmt}"
        # Opaque proxy is allowed to drop source Alpha at the planar boundary.
        # It is an explicit export choice (``has_alpha=False``), so it does not
        # require the RGB/render fallback.  The opposite direction still fails
        # closed: an Alpha deliverable cannot be synthesized from an opaque source.
        if output_has_alpha and source_spec[1] is None:
            return None, "alpha_format_mismatch"
        source_color_depth = source_spec[0]
    if source_cfa_depth is None:
        metadata_depth = int(metadata.get("bit_depth", 0) or 0)
        if metadata_depth and metadata_depth != source_spec[0]:
            return None, "source_bit_depth_mismatch"
    else:
        metadata_depth = int(metadata.get("bit_depth", 0) or 0)
        if metadata_depth and metadata_depth != source_cfa_depth:
            return None, "source_bit_depth_mismatch"

    # --- 输出尺寸/fps：同尺寸直通，或严格等比例缩小直通 ---
    source_width = int(metadata.get("width", 0) or 0)
    source_height = int(metadata.get("height", 0) or 0)
    out_width = int(getattr(encoder_config, "width", 0) or 0)
    out_height = int(getattr(encoder_config, "height", 0) or 0)
    decoder_resize = False
    if (out_width, out_height) != (source_width, source_height):
        if out_cfa_depth is not None:
            # M4-R2：CFA 相位平面是半几何——缩放语义在本车道无定义
            # （fail-closed：不做任何像素级处理），尺寸不同即拒绝。
            return None, "raw_output_size_mismatch"
        source_aspect = source_width / max(source_height, 1)
        output_aspect = out_width / max(out_height, 1)
        decoder_resize = (
            source_width > 0 and source_height > 0
            and out_width > 0 and out_height > 0
            and out_width <= source_width and out_height <= source_height
            and abs(source_aspect - output_aspect) <= 1e-6
        )
        if not decoder_resize:
            return None, "output_size_mismatch"
    fps_num = metadata.get("fps_num")
    fps_den = metadata.get("fps_den")
    if not fps_num or not fps_den:
        return None, "source_fps_unknown"
    out_fps = float(getattr(encoder_config, "fps", 0.0) or 0.0)
    if out_fps <= 0 or abs(out_fps - float(fps_num) / float(fps_den)) > 0.01:
        return None, "output_fps_mismatch"

    # --- 源帧数足够覆盖 range ---
    frames = end_frame - start_frame
    source_offset = (start_frame - clip.timeline_start) + clip.source_start
    total_source_frames = int(metadata.get("total_frames", 0) or 0)
    if total_source_frames and source_offset + frames > total_source_frames:
        return None, "source_frames_insufficient"

    if out_cfa_depth is not None:
        # RAW→RAW 直通：位深已逐项一致（前置判定），无任何平面转换。
        requires_planar_conversion = False
    else:
        requires_planar_conversion = (
            source_layout != output_layout
            or source_spec[0] != output_spec[0]
            or (source_spec[1] if source_spec[1] is not None else None)
            != output_alpha_depth
        )

    return DirectToposDeliveryPlan(
        asset_path=source_path,
        clip_id=str(getattr(clip, "clip_id", "")),
        timeline_start=int(clip.timeline_start),
        source_start=int(clip.source_start),
        frames=frames,
        width=source_width,
        height=source_height,
        fps_num=int(fps_num),
        fps_den=int(fps_den),
        source_pixel_format=source_fmt,
        source_bit_depth=source_color_depth,
        source_color_layout=source_layout,
        target_pixel_format=pix_fmt,
        target_color_layout=output_layout,
        target_bit_depth=output_color_depth,
        source_alpha_depth=(
            None if out_cfa_depth is not None else source_spec[1]),
        target_alpha_depth=None if out_cfa_depth is not None
        else output_alpha_depth,
        target_width=out_width,
        target_height=out_height,
        decoder_resize=decoder_resize,
        requires_planar_conversion=requires_planar_conversion,
    ), ""
