"""
F7 动画系统 —— 播放控制 + 时间重映射 + 采样缓存（纯 numpy，无 GL 依赖）。

设计目标：
- 把 comp frame（合成时间轴帧号）映射到 anim time（动画时间秒）。
- 支持 loop / pingpong / once 播放模式。
- 支持 speed（变速）、time_offset（时间偏移）、start/end frame 裁剪。
- AnimationState：播放状态机（playing/paused + 当前 anim_time）。
- AnimationSamplerCache：采样结果缓存（相同 (clip, anim_time) 不重算插值）。

关联模块：
- scene_graph.py：AnimationClip / sample_animation / apply_animation_to_scene。
- skeleton.py：compute_joint_matrices（采样后世界矩阵 → GPU joint matrices）。
- model3d_node.py：直接调用 remap_frame_to_anim_time 做 frame→anim_time
  映射；AnimationState 播放状态机目前无生产驱动方（保留为 F7 预留接口，
  详见 AUD-8601 勘误），勿再据此推断调用链。
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .scene_graph import AnimationClip, sample_animation

logger = __import__("logging").getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 播放模式
# ──────────────────────────────────────────────────────────────────────

PLAY_MODE_LOOP = "loop"          # 循环播放（默认）
PLAY_MODE_PINGPONG = "pingpong"  # 乒乓（到尾反弹回头）
PLAY_MODE_ONCE = "once"          # 播一次后停在最后一帧
PLAY_MODE_CLAMP = "clamp"        # 不循环，超出范围钳制到端点

_ALL_PLAY_MODES = (PLAY_MODE_LOOP, PLAY_MODE_PINGPONG, PLAY_MODE_ONCE, PLAY_MODE_CLAMP)


# ──────────────────────────────────────────────────────────────────────
# 时间重映射：comp frame → anim time
# ──────────────────────────────────────────────────────────────────────


def remap_frame_to_anim_time(
    frame: float,
    fps: float,
    clip_duration: float,
    speed: float = 1.0,
    time_offset: float = 0.0,
    start_frame: float = 0.0,
    end_frame: Optional[float] = None,
    play_mode: str = PLAY_MODE_LOOP,
) -> float:
    """把合成时间轴的帧号映射到动画时间（秒）。

    流程：
        comp_time = (frame - start_frame) / fps   # 合成秒
        anim_time_raw = comp_time * speed + time_offset
        → 按 play_mode 做循环/钳制/乒乓

    Args:
        frame: 合成时间轴帧号（可含小数；timeline playhead）。
        fps: 帧率（如 24/25/30）。
        clip_duration: 动画片段时长（秒）。
        speed: 播放速度（1.0=原速，2.0=2 倍速，0.5=慢放）。
        time_offset: 时间偏移（秒；正=延后开始）。
        start_frame: 起始帧（frame < start_frame 时视为未开始）。
        end_frame: 结束帧（None=不裁剪；frame > end_frame 时按 play_mode 处理）。
        play_mode: PLAY_MODE_* 常量。

    Returns:
        anim_time（秒），已按 play_mode 处理。clip_duration<=0 时返回 0。
    """
    if clip_duration <= 1e-6 or fps <= 0.0:
        return 0.0

    # 裁剪到 [start_frame, end_frame]
    if frame < start_frame:
        return 0.0
    if end_frame is not None and frame > end_frame:
        frame = end_frame

    comp_time = (frame - start_frame) / fps
    raw = comp_time * speed + time_offset

    if play_mode == PLAY_MODE_CLAMP:
        return float(max(0.0, min(raw, clip_duration)))

    if play_mode == PLAY_MODE_ONCE:
        # 播一次，超出后停在最后一帧；负时间钳到 0（与 CLAMP 一致）
        return float(max(0.0, min(raw, clip_duration)))

    if play_mode == PLAY_MODE_PINGPONG:
        # 乒乓：在 [0, 2*duration] 上取模，超 duration 部分反转
        period = clip_duration * 2.0
        t = raw % period
        if t > clip_duration:
            t = period - t
        return float(t)

    # 默认 loop
    return float(raw % clip_duration)


# ──────────────────────────────────────────────────────────────────────
# AnimationState：播放状态机
# ──────────────────────────────────────────────────────────────────────


@dataclass
class AnimationState:
    """动画播放状态（驱动每帧 anim_time）。

    节点持有此对象，每帧 update(frame, fps) 计算 anim_time，再 sample_animation。

    Attributes:
        clip_name: 当前播放的 clip 名（空=无动画/bind pose）。
        playing: 是否播放中（False=暂停，anim_time 冻结）。
        speed: 播放速度。
        time_offset: 时间偏移（秒）。
        start_frame / end_frame: 裁剪范围（frame）。
        play_mode: PLAY_MODE_* 常量。
        anim_time: 当前动画时间（秒；update 后更新）。
        finished: once 模式下是否已播完（UI 可据此切回 bind pose）。
    """
    clip_name: str = ""
    playing: bool = True
    speed: float = 1.0
    time_offset: float = 0.0
    start_frame: float = 0.0
    end_frame: Optional[float] = None
    play_mode: str = PLAY_MODE_LOOP
    anim_time: float = 0.0
    finished: bool = False

    def update(
        self,
        frame: float,
        fps: float,
        clip_duration: float,
    ) -> float:
        """根据当前 comp frame 更新 anim_time，返回新的 anim_time。

        paused（playing=False）时 anim_time 不变（冻结当前帧）。
        clip_duration<=0 时 anim_time=0（无动画/bind pose）。

        Args:
            frame: 合成时间轴帧号。
            fps: 帧率。
            clip_duration: 当前 clip 时长（秒）。

        Returns:
            更新后的 anim_time（也写入 self.anim_time）。
        """
        if not self.playing or clip_duration <= 1e-6:
            # 暂停或无动画：保持当前 anim_time
            if clip_duration <= 1e-6:
                self.anim_time = 0.0
            return self.anim_time

        t = remap_frame_to_anim_time(
            frame, fps, clip_duration,
            speed=self.speed,
            time_offset=self.time_offset,
            start_frame=self.start_frame,
            end_frame=self.end_frame,
            play_mode=self.play_mode,
        )
        self.anim_time = t

        # once 模式播完标记（AUD-8601：判定前先把 frame 钳到 end_frame，
        # 与 remap_frame_to_anim_time 的裁剪一致——否则 end_frame 早于
        # clip 尾部时（如 fps=24、duration=5s、end_frame=48 → anim 只播
        # 到 2.0s），frame 超过 end_frame 后 raw 已 ≥ duration，动画仍应
        # 显示中段姿势却被提前判 finished）。
        eff_frame = frame
        if self.end_frame is not None and frame > self.end_frame:
            eff_frame = self.end_frame
        comp_time = max(0.0, (eff_frame - self.start_frame) / fps if fps > 0 else 0.0)
        raw = comp_time * self.speed + self.time_offset
        if self.play_mode == PLAY_MODE_ONCE and raw >= clip_duration:
            self.finished = True
        else:
            self.finished = False

        return t

    def reset(self) -> None:
        """重置到起始状态（anim_time=0, finished=False）。"""
        self.anim_time = 0.0
        self.finished = False

    def signature(self) -> tuple:
        """播放参数签名（cache key 用；anim_time 单独进，因为它每帧变）。"""
        return (
            self.clip_name, self.play_mode,
            float(self.speed), float(self.time_offset),
            float(self.start_frame),
            float(self.end_frame) if self.end_frame is not None else -1.0,
        )


# ──────────────────────────────────────────────────────────────────────
# AnimationSamplerCache：采样结果缓存
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    """采样缓存条目。"""
    local_matrices: dict   # {node_name: 4x4}
    quantized_time: float  # 实际缓存的时间（量化后）


class AnimationSamplerCache:
    """动画采样结果缓存（避免每帧重算插值）。

    场景：骨骼动画每帧 sample_animation（插值 + 矩阵计算），大角色（100+ 骨骼）
    时较费。缓存按 quantized_time（量化到 1/120 秒）存最后一次采样结果；
    timeline 连续拖动时相邻帧时间差 < 1/120 → 命中缓存。

    多 clip 时用 dict 存（key = clip_name）。线程安全由调用方保证（render thread 独占）。
    """

    def __init__(self, quantize_hz: float = 120.0, max_entries: int = 8) -> None:
        """
        Args:
            quantize_hz: 时间量化频率（Hz）。120 → 相邻 1/120 秒内的采样视为相同。
            max_entries: 最大缓存条目数（LRU；超过淘汰最久未访问）。
        """
        self._quantize = 1.0 / quantize_hz if quantize_hz > 0 else 0.0
        self._max = max(1, max_entries)
        # clip identity is part of the key.  Names are user data and are not
        # guaranteed to be unique inside an imported asset.
        self._cache: Dict[Tuple[int, str, float], _CacheEntry] = {}
        self._hits = 0
        self._misses = 0

    def _quantize_time(self, t: float) -> float:
        if self._quantize <= 0.0:
            return t
        return round(t / self._quantize) * self._quantize

    def sample(
        self,
        clip: AnimationClip,
        anim_time: float,
    ) -> dict:
        """采样 clip 在 anim_time 的状态（命中缓存则直接返回拷贝）。

        Args:
            clip: 动画片段。
            anim_time: 动画时间（秒）。

        Returns:
            {node_name: 4x4 local_transform}（深拷贝，调用方可安全修改）。
        """
        qt = self._quantize_time(anim_time)
        key = (id(clip), str(clip.name), qt)
        entry = self._cache.get(key)
        if entry is not None:
            self._hits += 1
            # LRU：命中时把 key 移到末尾（最新使用），避免被误淘汰
            del self._cache[key]
            self._cache[key] = entry
            # 返回深拷贝（调用方可能修改矩阵；缓存保持不可变）
            return {k: v.copy() for k, v in entry.local_matrices.items()}

        # miss → 采样 + 存缓存
        self._misses += 1
        sampled = sample_animation(clip, anim_time)
        self._cache[key] = _CacheEntry(
            local_matrices=sampled,
            quantized_time=qt,
        )
        # LRU 淘汰：移除最久未访问（dict 首项 = 最旧）
        if len(self._cache) > self._max:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        return {k: v.copy() for k, v in sampled.items()}

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    def clear(self) -> None:
        """清空缓存 + 重置统计。"""
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    @property
    def size(self) -> int:
        return len(self._cache)


# ──────────────────────────────────────────────────────────────────────
# 便利：clip 时长 + clip 列表选择
# ──────────────────────────────────────────────────────────────────────


def select_clip(clips, name: str):
    """从 clip 列表按名选 clip（未找到返回 None）。

    Args:
        clips: List[AnimationClip]。
        name: clip 名（空=返回第一个或 None）。

    Returns:
        AnimationClip 或 None（无 clip / 名不匹配）。
    """
    if not clips:
        return None
    if not name:
        return clips[0]
    for c in clips:
        if c.name == name:
            return c
    return None


def clip_names(clips) -> list:
    """返回 clip 名列表（UI 下拉用）。"""
    return [c.name for c in clips]
