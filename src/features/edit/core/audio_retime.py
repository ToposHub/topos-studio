"""D01/D04/D06（第六轮审计）：音频 retime 与动画 FX 的共享渲染原语。

问题背景（审计实测）：
- 旧导出/Stem 读源公式 ``source_start + d * scalar_speed`` 完全忽略
  reverse / speed_points / 混合 fps / 帧相位，倒放与速度曲线的音频和
  1x 普通 PCM 逐样本相同；2x 未保调 440Hz 正弦实测仍 ~430Hz（块级跳读
  而非样本域重采样）。
- 音频片段 FX 的 animated_params 只在 UI 保存，预览/导出/Stem 均按静态
  params 处理，关键帧对声音零影响。

本模块提供与视频路径同一真相（``ClipInstance.timeline_to_source_frame_pair``
canonical 映射）的样本域原语，供 timeline_export（成片）、stem_export
（Stem）与实时预览共用：

- :func:`needs_retime` / :func:`position_span` / :func:`source_positions`
- :func:`decode_source_pcm` / :func:`resample_segment`
- :class:`ClipRetimeReader`（按 clip 缓存连续源 PCM + 逐帧重采样读取）
- :func:`clip_fx_entries` / :func:`has_animated_fx`（动画 FX 求值）

样本坐标约定：decoder 以 ``decode_samples_for_frame(sf, fps, n)`` 返回源帧
``sf`` 起的 n 个样本（输出采样率），连续帧块拼接即连续 PCM；源帧 k 的
样本位置 = ``k * samples_per_frame``（与既有 WSOLA 预解码同口径）。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

_EPS = 1e-9


def clip_source_fps(clip: Any, fallback_fps: float) -> float:
    """clip 素材的源帧率；fps 元数据缺失时退化用 fallback（时间线帧率）。

    R06：源帧位置 → 采样位的换算必须用源时钟。50fps 素材放进 25fps
    序列时若按序列 fps 换算，1 秒时间线会读到 2 秒源。
    """
    getter = getattr(clip, "source_fps_fraction", None)
    if callable(getter):
        try:
            frac = getter()
        except Exception:  # noqa: BLE001 — 元数据异常退化 fallback
            frac = None
        if frac is not None and float(frac) > 0.0:
            return float(frac)
    return float(fallback_fps)


def needs_retime(clip: Any) -> bool:
    """True 当 clip 的音频不能用 1x 逐帧直读（需要样本域重采样）。

    冻结片段由调用方整体跳过（无音频进度），不属于 retime 范畴。
    """
    if getattr(clip, "freeze_source_frame", None) is not None:
        return False
    speed = getattr(clip, "speed", None)
    if speed is None:
        return False
    if isinstance(speed, (int, float)):
        return abs(float(speed) - 1.0) > _EPS
    return (
        bool(getattr(speed, "reverse", False))
        or bool(getattr(speed, "speed_points", ()) or ())
        or abs(float(getattr(speed, "speed", 1.0) or 1.0) - 1.0) > _EPS
    )


def _pair_position(clip: Any, timeline_frame: int) -> Optional[float]:
    """``timeline_to_source_frame_pair`` 的浮点位置值（clip 源相对帧）。

    pair 语义 value = a + w * (b - a)：freeze 返回 (rel, rel, 0)，常规
    路径返回 floor/floor+1 与小数权重，reverse 路径已在 pair 内翻转，
    公式统一成立。范围外返回 None（由调用方处理边界）。
    """
    try:
        a, b, w = clip.timeline_to_source_frame_pair(timeline_frame)
    except (ValueError, AttributeError, TypeError):
        return None
    try:
        return float(a) + float(w) * (float(b) - float(a))
    except (TypeError, ValueError):
        return None


def _per_frame_source_advance(clip: Any) -> float:
    """一个时间线帧的源帧推进量（带方向）。

    pair 在源窗口末段会把 a/b 同时钳到最后一帧（a==b），小数位置
    丢失；末端外推遇到零差时用它兜底，保持局部速度。
    """
    try:
        advance = float(clip.source_frames_per_timeline_frame())
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        advance = 1.0
    if getattr(clip, "is_reverse_playback", None) and clip.is_reverse_playback():
        advance = -abs(advance)
    return advance


def position_span(clip: Any, timeline_frame: int) -> Tuple[float, float]:
    """输出帧 ``f`` 覆盖的源位置区间 [pos(f), pos(f+1))（源相对帧）。

    f+1 越过片段尾时用最后一个有效位置外推（保持末帧局部速度/方向），
    单帧片段回退到有效速度（reverse 取负）。所有位置钳制在
    [0, source_duration-1]——末帧外推越界（reverse 走到负值）会把
    重采样索引整体钳成常量块（成串零样本）。
    """
    source_duration = max(1, int(getattr(clip, "source_duration", 1) or 1))
    # 上界允许到 source_duration（闭区间末端=下一帧边界，自然外推位置）；
    # 下界钳制 0——reverse 末帧外推为负时把重采样索引整体钳成常量块。
    hi_limit = float(source_duration)

    def _clamp(value: float) -> float:
        return max(0.0, min(value, hi_limit))

    lo = _pair_position(clip, timeline_frame)
    if lo is None:
        spd = float(clip.effective_speed()) if hasattr(clip, "effective_speed") else 1.0
        if getattr(clip, "is_reverse_playback", None) and clip.is_reverse_playback():
            spd = -abs(spd)
        return (0.0, _clamp(spd))

    hi = _pair_position(clip, timeline_frame + 1)
    if hi is not None:
        return (_clamp(lo), _clamp(hi))

    # 末端外推：区间终点允许越出源窗口（R07——reverse 末帧外推到 -1
    # 被钳回 0 会令 x0 == x1，整帧输出常量块；resample_segment 内部把
    # 索引钳回 [0, N-1]，越界端点自然落到可用样本上）。pair 在窗口
    # 末段钳掉小数（a==b）使相邻差为 0 时，用每帧源推进量兜底（R03：
    # 0.5x 末帧不再塌成常量）。
    prev = _pair_position(clip, timeline_frame - 1)
    if prev is not None:
        delta = lo - prev
        if delta == 0.0:
            delta = _per_frame_source_advance(clip)
        return (_clamp(lo), lo + delta)
    spd = _per_frame_source_advance(clip)
    return (_clamp(lo), lo + spd)


def source_positions(
    clip: Any, frame_lo: int, frame_hi: int
) -> Optional[np.ndarray]:
    """[frame_lo, frame_hi]（闭区间）逐帧源相对位置的浮点数组。

    length = frame_hi - frame_lo + 1；用于一次性确定解码窗口与逐帧
    重采样端点。全部不可得时返回 None。
    """
    count = frame_hi - frame_lo + 1
    if count <= 0:
        return None
    positions = np.empty(count, dtype=np.float64)
    for i, f in enumerate(range(frame_lo, frame_hi + 1)):
        pos = _pair_position(clip, f)
        if pos is None:
            if i == 0:
                return None
            # 端点外推：保持与 position_span 相同的边界语义
            positions[i] = positions[i - 1] + (
                positions[i - 1] - positions[i - 2] if i >= 2 else 1.0
            )
        else:
            positions[i] = pos
    return positions


def decode_source_pcm(
    decoder: Any,
    first_frame: int,
    frame_count: int,
    fps: float,
    samples_per_frame: int,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Optional[np.ndarray]:
    """连续解码源帧窗口为 (channels, frame_count * spf) PCM。

    逐帧失败即停（返回已解部分不完整会破坏重采样端点语义，返回 None
    由调用方降级）；支持取消检查（D07：预解码循环也响应取消）。
    """
    if first_frame < 0 or frame_count <= 0 or samples_per_frame <= 0:
        return None
    blocks = []
    for sf in range(first_frame, first_frame + frame_count):
        if cancel_check is not None and cancel_check():
            return None
        block = decoder.decode_samples_for_frame(sf, fps, samples_per_frame)
        if block is None:
            return None
        blocks.append(block)
    pcm = blocks[0] if len(blocks) == 1 else np.concatenate(blocks, axis=-1)
    if pcm.ndim == 1:
        pcm = pcm[np.newaxis, :]
    return np.ascontiguousarray(pcm, dtype=np.float32)


def decode_pcm_span(
    decoder: Any,
    start_frame: int,
    source_fps: float,
    total_samples: int,
    cancel_check: Optional[Callable[[], bool]] = None,
    chunk_samples: int = 32768,
) -> Optional[np.ndarray]:
    """按源时钟从源帧 ``start_frame`` 起连续解码 ``total_samples`` 个输出采样。

    R06：窗口帧号是素材源时钟帧——seek 与逐块读取都按 ``source_fps``
    换算，与视频解码同一时间域。旧实现把源帧当序列帧传给 decoder
    （按序列 fps 定位），50fps 源在 25fps 序列上 1 秒时间线读到 2 秒源。
    """
    if total_samples <= 0 or source_fps <= 0:
        return None
    try:
        if not decoder.seek_to_time(start_frame / float(source_fps)):
            return None
    except Exception:  # noqa: BLE001 — 定位失败由调用方降级
        return None
    blocks = []
    got = 0
    token = int(start_frame)
    while got < total_samples:
        if cancel_check is not None and cancel_check():
            return None
        need = min(int(chunk_samples), total_samples - got)
        block = decoder.decode_samples_for_frame(token, float(source_fps), need)
        if block is None:
            break
        if block.ndim == 1:
            block = block[np.newaxis, :]
        blocks.append(block)
        got += block.shape[-1]
        token += 1
    if not blocks:
        return None
    pcm = blocks[0] if len(blocks) == 1 else np.concatenate(blocks, axis=-1)
    # 流尾短读（AAC 流先于视频结束等）补零到 total_samples——重采样
    # 索引钳制在真实内容内，越界输出静音而不是钳到末样本的直流。
    if pcm.shape[-1] < total_samples:
        pcm = np.pad(
            pcm, ((0, 0), (0, total_samples - pcm.shape[-1])))
    return np.ascontiguousarray(pcm, dtype=np.float32)


def resample_segment(
    src_pcm: np.ndarray, x0: float, x1: float, out_len: int
) -> np.ndarray:
    """把 src_pcm 的样本区间 [x0, x1) 线性插值重采样为 out_len 个样本。

    半开区间约定：输出样本 i 的源位置 = x0 + i * (x1-x0)/out_len，
    相邻输出帧的边界样本不重复（帧 k 覆盖 [k*spf, (k+1)*spf)）。

    - 常规变速：x1 - x0 = speed * out_len 的比例自然成立（speed=2 即
      每 2 个源样本取 1 → 频率 ×2；speed=0.5 → 频率 ×0.5）。
    - 倒放：x1 < x0，索引倒序即倒放（样本值不变、时间反转）。
    - 端点越界钳制到 [0, N-1]。
    """
    if out_len <= 0:
        return src_pcm[:, :0]
    total = int(src_pcm.shape[-1])
    if total == 0:
        return np.zeros((src_pcm.shape[0], out_len), dtype=np.float32)
    step = (x1 - x0) / float(out_len)
    idx = x0 + step * np.arange(out_len, dtype=np.float64)
    np.clip(idx, 0.0, float(total - 1), out=idx)
    i0 = np.floor(idx).astype(np.int64)
    frac = (idx - i0).astype(np.float32)
    i1 = np.minimum(i0 + 1, total - 1)
    a = src_pcm[:, i0]
    b = src_pcm[:, i1]
    return (a + (b - a) * frac[np.newaxis, :]).astype(np.float32)


class ClipRetimeReader:
    """按 clip 缓存连续源 PCM，并按 canonical 映射逐帧重采样读取。

    成片（timeline_export）与 Stem（stem_export）共用：同一 clip 在
    两条路径下得到逐样本相同的 retime 结果（D05 验收的前置条件）。

    坐标约定（R03/R06，第七轮审计）：
    - ``timeline_to_source_frame_pair`` 返回 clip 源窗口内的**相对帧**
      （0 基），解码窗口与重采样端点都在该相对坐标内换算——旧实现
      在采样端再叠加绝对 ``source_start``，非零入点时整窗越界钳到
      尾样本，输出整段 DC 常量。
    - 相对帧 → 采样位用**源帧率**时钟（``sample_rate / source_fps``），
      与视频读源同域；按序列 fps 换算会让混帧率素材倍速读源。
    """

    def __init__(
        self,
        decoder: Any,
        clip: Any,
        fps: float,
        samples_per_frame: int,
        cancel_check: Optional[Callable[[], bool]] = None,
        margin_frames: int = 2,
        sample_rate: Optional[float] = None,
        source_offset_seconds: float = 0.0,
    ) -> None:
        self._decoder = decoder
        self._clip = clip
        self._timeline_fps = float(fps)
        self._spf_out = int(samples_per_frame)
        self._cancel = cancel_check
        if sample_rate is not None and sample_rate > 0:
            self._sample_rate = float(sample_rate)
        else:
            self._sample_rate = self._spf_out * self._timeline_fps
        self._source_fps = clip_source_fps(clip, self._timeline_fps)
        if self._source_fps <= 0:
            self._source_fps = self._timeline_fps
        self._spf_src = self._sample_rate / self._source_fps
        self._pcm: Optional[np.ndarray] = None
        self._window: Optional[Tuple[int, int]] = None

        # M03（S05）：外录绑定的源窗平移。offset 是"WAV 内容相对视频
        # 0 点提前量（秒）"——位置映射与速度无关，整窗按
        # offset*source_fps 平移后重采样数学原样成立（与实时预览
        # source_start+bound_audio_frame 同口径；此前导出/Stem 对变速
        # 绑定片段静默回退机内声，与预览音源不一致）。
        offset_frames = 0
        if source_offset_seconds:
            offset_frames = int(round(
                source_offset_seconds * self._source_fps))

        # 片段整个时间线跨度上的源位置范围决定解码窗口（reverse/曲线
        # 都取 min/max，天然覆盖）。
        positions = source_positions(
            clip, int(clip.timeline_start), int(clip.timeline_end)
        )
        if positions is None or positions.size == 0 or self._spf_out <= 0:
            return
        lo = float(np.min(positions))
        hi = float(np.max(positions))
        first = max(0, int(np.floor(lo)) - margin_frames)
        last = int(np.ceil(hi)) + margin_frames
        # 钳制到源窗口（source_duration 之外是邻居内容，不参与本片段）。
        # window 是相对帧坐标——绑定平移只改绝对起点（_source_start），
        # 相对窗边界（clip 源窗宽）不受 offset 影响。
        source_duration = int(getattr(clip, "source_duration", 0) or 0)
        if source_duration > 0:
            last = min(last, source_duration)
        first = min(first, last)
        self._window = (first, last)
        self._source_start = int(getattr(clip, "source_start", 0) or 0) \
            + offset_frames
        self._base_sample = first * self._spf_src

    @property
    def available(self) -> bool:
        """窗口参数有效（PCM 延迟到首次读取再解码）。"""
        return self._window is not None

    def _ensure_pcm(self) -> Optional[np.ndarray]:
        if self._pcm is None:
            if self._window is None:
                return None
            first, last = self._window
            total = int(math.ceil((last - first) * self._spf_src)) + 1
            # 解码起点换到绝对源帧：pair 位置是 clip 源窗口内相对帧，
            # decoder 按整个素材的绝对时间定位（R03）。
            self._pcm = decode_pcm_span(
                self._decoder, self._source_start + first,
                self._source_fps, total,
                cancel_check=self._cancel,
            )
        return self._pcm

    def read_frame(self, timeline_frame: int, out_len: int) -> Optional[np.ndarray]:
        """读取输出帧 ``timeline_frame`` 的 out_len 个重采样样本。"""
        pcm = self._ensure_pcm()
        if pcm is None:
            return None
        lo, hi = position_span(self._clip, timeline_frame)
        x0 = lo * self._spf_src - self._base_sample
        x1 = hi * self._spf_src - self._base_sample
        if x1 < x0:
            # 倒放半开块语义：正向帧 k 覆盖 [k*spf, (k+1)*spf)，其倒放
            # 首样本应是边界前最后一个样本 ((k+1)*spf - 1)——帧级位置
            # 表示"正在播帧 k"，直接乘 spf 会多出 1 帧的偏移。移位量
            # 用源时钟 spf（混帧率时与输出 spf 不同）。
            x0 += self._spf_src - 1.0
            x1 += self._spf_src - 1.0
        return resample_segment(pcm, x0, x1, out_len)


# ── D04：音频片段 FX 动画参数求值 ──


def has_animated_fx(effects: Any) -> bool:
    """effects 中是否存在带关键帧的音频 FX 参数。"""
    for effect in effects or ():
        animated = getattr(effect, "animated_params", None) or {}
        for param in animated.values():
            if (
                hasattr(param, "is_animated")
                and param.is_animated()
            ):
                return True
    return False


def clip_fx_entries(
    effects: Any,
    frame: Optional[int] = None,
    audio_types: Optional[frozenset] = None,
) -> list:
    """把 clip.effects 转成 EffectChain.set_params 条目（按 order 排序）。

    ``frame=None`` 时只用静态 params；给定帧号时 animated_params 中的
    关键帧参数以 :meth:`EffectInstance.get_param_at_frame` 求值覆盖
    （绝对时间线帧域，与 UI ◆ 写入一致）。``audio_types`` 提供时只保留
    音频 mixer 效果（实时路径与导出路径同一过滤口径）。
    """
    from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES

    allowed = audio_types if audio_types is not None else AUDIO_MIXER_EFFECT_TYPES
    entries: list = []
    for effect in sorted(effects or (), key=lambda ef: getattr(ef, "order", 0)):
        effect_type = str(getattr(effect, "effect_type", "") or "")
        if effect_type not in allowed:
            continue
        params = dict(getattr(effect, "params", None) or {})
        if frame is not None:
            animated = getattr(effect, "animated_params", None) or {}
            get_at = getattr(effect, "get_param_at_frame", None)
            if callable(get_at):
                for name, param in animated.items():
                    if hasattr(param, "is_animated") and param.is_animated():
                        try:
                            params[name] = get_at(name, frame)
                        except Exception:  # noqa: BLE001 — 单参数失败不阻断链
                            logger.debug(
                                "animated fx param %s evaluate failed", name,
                                exc_info=True,
                            )
        inst_enabled = bool(getattr(effect, "is_enabled", True))
        dsp_enabled = inst_enabled and bool(params.get("enabled", True))
        entries.append((effect_type, params, dsp_enabled))
    return entries


def animated_fx_sampler(effects: Any) -> Optional[Callable[[int], list]]:
    """返回 (frame -> entries) 采样器；无动画时 None（静态设置一次即可）。"""
    if not has_animated_fx(effects):
        return None

    def _sample(frame: int) -> list:
        return clip_fx_entries(effects, frame)

    return _sample


# ── R05（第七轮审计）：保调变速（WSOLA）共享读取器 ──


def preserve_pitch_request(clip: Any) -> Tuple[bool, Optional[float]]:
    """(是否请求保调, 可用的常量速度)。

    请求保调但属于离线不支持组合（倒放/速度曲线）时返回
    ``(True, None)``——调用方需显式回退变调并留痕，不能静默吞掉。
    1x 或未请求保调返回 ``(False, None)``。
    """
    speed = getattr(clip, "speed", None)
    if speed is None or isinstance(speed, (int, float)):
        return False, None
    if not bool(getattr(speed, "preserve_pitch", False)):
        return False, None
    if bool(getattr(speed, "reverse", False)) or bool(
        getattr(speed, "speed_points", ()) or ()
    ):
        return True, None
    spd = float(getattr(speed, "speed", 1.0) or 1.0)
    if abs(spd - 1.0) <= _EPS:
        return False, None
    return True, spd


class PreservedPitchReader:
    """保调变速（WSOLA）共享读取器：成片导出与 Stem 同一实现。

    R05 背景：旧成片 helper 运行时 ``np`` 未定义被 except 静默吞掉、
    实际回退变调；Stem 路径则明确写着"回退变调"。保调工程导出的
    成片与 Stem 必须听感一致，因此收敛到本类：

    - 预解码 clip 时间线跨度覆盖的源窗口（源时钟，R06）；
    - 整段 ``wsola_time_stretch`` 伸缩到时间线时长；
    - ``read_frame`` 按时间线帧顺序切片（跳帧时按帧号重定位游标）。

    倒放/速度曲线等离线不支持组合 ``available=False``（调用方回退
    变调并留痕）。
    """

    def __init__(
        self,
        decoder: Any,
        clip: Any,
        timeline_fps: float,
        samples_per_frame: int,
        sample_rate: float,
        cancel_check: Optional[Callable[[], bool]] = None,
        margin_frames: int = 2,
        source_offset_seconds: float = 0.0,
    ) -> None:
        self._clip = clip
        self._cancel = cancel_check
        self._timeline_fps = max(1e-6, float(timeline_fps))
        self._spf_out = float(sample_rate) / self._timeline_fps
        self._audio: Optional[np.ndarray] = None
        self._next_frame: Optional[int] = None
        self._cursor = 0

        requested, speed = preserve_pitch_request(clip)
        if not requested or speed is None:
            return
        source_fps = clip_source_fps(clip, self._timeline_fps)
        if source_fps <= 0:
            return
        spf_src = float(sample_rate) / source_fps
        # M03（S05）：外录绑定源窗平移（与 ClipRetimeReader 同口径）
        offset_frames = 0
        if source_offset_seconds:
            offset_frames = int(round(
                source_offset_seconds * source_fps))
        positions = source_positions(
            clip, int(clip.timeline_start), int(clip.timeline_end)
        )
        if positions is None or positions.size == 0:
            return
        lo = float(np.min(positions))
        hi = float(np.max(positions))
        first = max(0, int(np.floor(lo)) - margin_frames)
        last = int(np.ceil(hi)) + margin_frames
        # window 相对坐标钳制（同 ClipRetimeReader：平移只改绝对起点）
        source_duration = int(getattr(clip, "source_duration", 0) or 0)
        if source_duration > 0:
            last = min(last, source_duration)
        first = min(first, last)
        total_src = int(math.ceil((last - first) * spf_src))
        if total_src <= 0:
            return
        try:
            src_pcm = decode_pcm_span(
                decoder,
                int(getattr(clip, "source_start", 0) or 0)
                + offset_frames + first,
                source_fps, total_src,
                cancel_check=cancel_check,
            )
            if src_pcm is None or src_pcm.shape[-1] == 0:
                return
            from src.features.edit.core.audio_timestretch import (
                wsola_time_stretch,
            )

            stretched = wsola_time_stretch(
                src_pcm, float(speed), float(sample_rate))
        except Exception:  # noqa: BLE001 — 预解码/伸缩失败由调用方回退
            logger.warning(
                "preserve-pitch 预解码失败 clip %s",
                getattr(clip, "clip_id", "?"), exc_info=True,
            )
            return
        if stretched is None or stretched.ndim == 0 or stretched.shape[-1] <= 0:
            return
        if stretched.ndim == 1:
            stretched = stretched[np.newaxis, :]
        self._audio = np.ascontiguousarray(stretched, dtype=np.float32)

    @property
    def available(self) -> bool:
        return self._audio is not None

    def read_frame(self, timeline_frame: int, out_len: int) -> Optional[np.ndarray]:
        if self._audio is None or out_len <= 0:
            return None
        if timeline_frame != self._next_frame:
            rel = timeline_frame - int(self._clip.timeline_start)
            self._cursor = int(round(rel * self._spf_out))
            self._next_frame = timeline_frame
        block = self._audio[:, self._cursor:self._cursor + out_len]
        if block.shape[-1] < out_len:
            block = np.pad(
                block, ((0, 0), (0, out_len - block.shape[-1])))
        self._cursor += out_len
        self._next_frame = timeline_frame + 1
        return block
