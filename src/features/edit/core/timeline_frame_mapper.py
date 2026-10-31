"""Unified Timeline Frame Mapper (Phase 10).

Origin
------
This module was introduced in Phase 10 of the Topos-Color multi-task editing
pipeline.  Its purpose is to provide a *single, well-tested* place for
converting a timeline frame number into the corresponding source frame,
handling all the complexities that arise in a real-world NLE:

* **Mixed frame-rates** -- a 24 fps timeline hosting 30 fps, 25 fps, or 60 fps
  source media.
* **Speed changes** -- clips played at 0.5x, 2x, or any arbitrary rational
  speed ratio.
* **Reverse playback** -- source frames read backwards.
* **Variable frame-rate (VFR) content** -- source media whose PTS timestamps
  are not evenly spaced (common in screen-recordings and mobile footage).

Design Principles
-----------------
1. *Rational arithmetic.*  All intermediate calculations use
   ``fractions.Fraction`` so that a 29.97 / 23.976 cadence never drifts due
   to IEEE-754 rounding.
2. *Immutable dataclasses.*  ``FrameMapRequest`` and ``FrameMapResult`` are
   frozen, making them safe to cache, hash (where applicable), and pass
   across thread boundaries.
3. *VFR-aware.*  When the caller supplies a ``source_pts_index`` the mapper
   switches to a bisection-based algorithm that honours the actual PTS
   timestamps of the source.
4. *Cadence diagnostics.*  ``compute_cadence`` lets tests and the UI preview
   the "exact / repeat / skip" pattern for any clip configuration.

Public API
----------
``map_frame`` is the main entry point.  It auto-selects CFR or VFR mode
based on whether ``source_pts_index`` is provided in the request.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from fractions import Fraction


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrameMapRequest:
    """All parameters needed to map a single timeline frame to a source frame.

    Attributes
    ----------
    timeline_frame:
        The frame number on the editor's timeline (0-based absolute).
    timeline_fps_num / timeline_fps_den:
        Timeline frame-rate expressed as a rational
        (e.g. 24/1 for 24 fps, 24000/1001 for 23.976 fps).
    clip_timeline_start:
        The timeline frame at which this clip begins.
    clip_timeline_duration:
        Length of the clip on the timeline in frames.
    source_start_frame:
        First frame of the source media to use (0-based).
    source_duration_frames:
        Total number of usable frames in the source.
    source_fps_num / source_fps_den:
        Source frame-rate as a rational.  ``None`` signals "assume same as
        timeline" (the CFR fallback path).
    speed:
        Playback speed as a ``Fraction`` (default 1/1).
    reverse:
        If ``True``, source frames are consumed in reverse order.
    source_pts_index:
        An ordered tuple of PTS values (in seconds) for VFR sources.
        ``None`` triggers the constant-frame-rate path.  Combined with
        ``reverse=True`` the VFR path is bypassed in favour of the exact
        rational CFR transform (AUD-18508).
    """

    timeline_frame: int
    timeline_fps_num: int
    timeline_fps_den: int
    clip_timeline_start: int
    clip_timeline_duration: int
    source_start_frame: int
    source_duration_frames: int
    source_fps_num: int | None = None
    source_fps_den: int | None = None
    speed: Fraction = Fraction(1, 1)
    reverse: bool = False
    source_pts_index: tuple[float, ...] | None = None
    source_frame_phase: Fraction = Fraction(0)


@dataclass(frozen=True)
class FrameMapResult:
    """The outcome of mapping one timeline frame to a source frame.

    Attributes
    ----------
    source_frame:
        Integer source frame number (clamped to valid range).
    source_time_seconds:
        Clip-local playback time in seconds (AUD-18511): the timeline
        offset from the clip's in-point, scaled by ``speed``. It
        intentionally does NOT include the ``source_start_frame`` offset
        and is NOT flipped to the clip tail for ``reverse`` clips — use
        ``source_frame`` / ``fractional_source_frame`` for absolute
        source-media positioning (e.g. decoder seeks). For VFR sources
        this carries the PTS-domain target time (see ``map_vfr_frame``).
    timeline_time_seconds:
        Time offset from the clip's start on the timeline, in seconds.
    cadence_action:
        One of ``"exact"`` (perfect match), ``"repeat"`` (this source frame
        is reused to fill a timeline frame), or ``"skip"`` (a source frame
        is dropped to stay in sync).
    fractional_source_frame:
        The *exact* fractional source frame before rounding -- useful for
        diagnostics and sub-frame interpolation.
    """

    source_frame: int
    source_time_seconds: float
    timeline_time_seconds: float
    cadence_action: str  # "exact" | "repeat" | "skip"
    fractional_source_frame: Fraction


# ---------------------------------------------------------------------------
# CFR mapping
# ---------------------------------------------------------------------------

def map_cfr_frame(request: FrameMapRequest) -> FrameMapResult | None:
    """Map a timeline frame to a source frame assuming constant frame-rate.

    Uses ``fractions.Fraction`` throughout to avoid float accumulation
    errors when timeline and source fps differ (e.g. 29.97 vs 23.976).

    Returns ``None`` when the requested timeline frame falls outside the
    clip's range.
    """
    # -- range check --------------------------------------------------------
    local = request.timeline_frame - request.clip_timeline_start
    if local < 0 or local >= request.clip_timeline_duration:
        return None

    # -- timeline local time (rational) -------------------------------------
    timeline_fps = Fraction(request.timeline_fps_num, request.timeline_fps_den)
    timeline_local_time = Fraction(local, 1) / timeline_fps

    # -- source local time --------------------------------------------------
    source_local_time = timeline_local_time * request.speed

    # -- fractional source frame --------------------------------------------
    if request.source_fps_num is not None and request.source_fps_den is not None:
        source_fps = Fraction(request.source_fps_num, request.source_fps_den)
    else:
        source_fps = timeline_fps  # fallback: assume same fps

    # Phase is optional for older request adapters/duck-typed clients.
    source_frac = source_local_time * source_fps + getattr(request, "source_frame_phase", Fraction(0))

    # -- apply source start offset ------------------------------------------
    total_source_frac = source_frac + request.source_start_frame

    # -- handle reverse -----------------------------------------------------
    if request.reverse:
        total_source_frac = (
            Fraction(request.source_start_frame + request.source_duration_frames - 1, 1)
            - source_frac
        )

    # -- round to nearest integer frame -------------------------------------
    # R07：一致的四舍五入（half-up）。Python round() 对 Fraction 是
    # banker's rounding——同一精确位置 .5 会因 source_start 偏移翻转
    # 奇偶（37.5→38 但 76.5→76），切分/修剪前后子窗口映射分叉。
    source_frame = (
        (total_source_frac + Fraction(1, 2)).__floor__()
        if total_source_frac >= 0
        else -((-total_source_frac + Fraction(1, 2)).__floor__())
    )

    # -- clamp to valid range -----------------------------------------------
    source_frame = max(
        request.source_start_frame,
        min(source_frame, request.source_start_frame + request.source_duration_frames - 1),
    )

    # -- cadence action -----------------------------------------------------
    diff = abs(float(source_frame - total_source_frac))
    if diff < 0.01:
        action = "exact"
    elif float(source_frac) % 1.0 < 0.5:
        action = "repeat"
    else:
        action = "skip"

    return FrameMapResult(
        source_frame=source_frame,
        source_time_seconds=float(source_local_time),
        timeline_time_seconds=float(timeline_local_time),
        cadence_action=action,
        fractional_source_frame=total_source_frac,
    )


# ---------------------------------------------------------------------------
# VFR mapping
# ---------------------------------------------------------------------------

def map_vfr_frame(request: FrameMapRequest) -> FrameMapResult | None:
    """Map a timeline frame to a source frame for variable frame-rate content.

    Uses binary search (``bisect``) on the ``source_pts_index`` to find the
    nearest actual source frame for the target presentation time-stamp.

    Falls back to :func:`map_cfr_frame` when ``source_pts_index`` is not
    provided.

    AUD-18508: ``reverse`` VFR requests also fall back to the CFR formula.
    The PTS index's time base relative to ``source_start_frame`` is not a
    defined contract (see AUD-18512), so a reverse time-flip cannot be
    computed honestly on top of it; the CFR path implements the reverse
    transform exactly with rational arithmetic.  Without this guard the
    VFR branch silently mapped reverse clips with *forward* frame order.
    """
    if request.source_pts_index is None:
        return map_cfr_frame(request)
    if request.reverse:
        return map_cfr_frame(request)

    # -- range check --------------------------------------------------------
    local = request.timeline_frame - request.clip_timeline_start
    if local < 0 or local >= request.clip_timeline_duration:
        return None

    # -- target source time -------------------------------------------------
    timeline_fps = Fraction(request.timeline_fps_num, request.timeline_fps_den)
    timeline_time = Fraction(local, 1) / timeline_fps
    source_time = float(timeline_time * request.speed)

    # -- bisect to nearest PTS ----------------------------------------------
    target_pts = source_time
    idx = bisect.bisect_left(request.source_pts_index, target_pts)

    if idx == 0:
        source_frame = 0
    elif idx >= len(request.source_pts_index):
        source_frame = len(request.source_pts_index) - 1
    else:
        before = request.source_pts_index[idx - 1]
        after = request.source_pts_index[idx]
        if abs(target_pts - before) <= abs(target_pts - after):
            source_frame = idx - 1
        else:
            source_frame = idx

    return FrameMapResult(
        source_frame=source_frame + request.source_start_frame,
        source_time_seconds=target_pts,
        timeline_time_seconds=float(timeline_time),
        cadence_action="exact",
        fractional_source_frame=Fraction(source_frame),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def map_frame(request: FrameMapRequest) -> FrameMapResult | None:
    """Unified frame mapping entry point.

    Automatically selects CFR or VFR based on ``request.source_pts_index``.
    """
    if request.source_pts_index is not None:
        return map_vfr_frame(request)
    return map_cfr_frame(request)


# ---------------------------------------------------------------------------
# Cadence diagnostic helper
# ---------------------------------------------------------------------------

def compute_cadence(
    request_template: FrameMapRequest,
    num_frames: int = 100,
) -> list[FrameMapResult]:
    """Compute frame mapping for a range of timeline frames.

    Starting from ``request_template.clip_timeline_start``, maps
    ``num_frames`` consecutive timeline frames and returns all successful
    results.  Useful for diagnostics, unit-tests, and UI previews of
    cadence patterns (e.g. 3:2 pulldown).
    """
    results: list[FrameMapResult] = []
    for i in range(num_frames):
        r = FrameMapRequest(
            timeline_frame=request_template.clip_timeline_start + i,
            timeline_fps_num=request_template.timeline_fps_num,
            timeline_fps_den=request_template.timeline_fps_den,
            clip_timeline_start=request_template.clip_timeline_start,
            clip_timeline_duration=request_template.clip_timeline_duration,
            source_start_frame=request_template.source_start_frame,
            source_duration_frames=request_template.source_duration_frames,
            source_fps_num=request_template.source_fps_num,
            source_fps_den=request_template.source_fps_den,
            speed=request_template.speed,
            reverse=request_template.reverse,
            source_pts_index=request_template.source_pts_index,
            source_frame_phase=getattr(request_template, "source_frame_phase", Fraction(0)),
        )
        result = map_frame(r)
        if result is not None:
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# A3.2: Frame blend 双帧采样 API
# ---------------------------------------------------------------------------

def map_frame_pair(request: FrameMapRequest):
    """A3.2: 将时间线帧映射为两个相邻源帧及混合权重（frame blend）。

    返回 ``(result_a, result_b, weight)``：
    - ``result_a`` 对应 floor(精确源位置)，``result_b`` 为其后一帧；
    - ``weight`` ∈ [0, 1] 是 result_b 的混合权重（= 精确源位置的小数部分）；
    - 整速段（速度对齐帧边界）权重恒为 0 —— 等价 nearest，零额外采样收益损失；
    - 慢速/斜坡路径小数部分非零，双帧 lerp 可消除频闪。

    ``result_b`` 由 source_start + a + 1 的合法化版本构造（cadence_action
    复用 a 的判定）。时间线帧超出片段范围返回 ``None``（与 map_frame 一致）。
    """
    result = map_frame(request)
    if result is None:
        return None

    exact = result.fractional_source_frame
    a = int(exact)  # floor（源位置非负）
    frac = float(exact - a)

    source_last = request.source_start_frame + request.source_duration_frames - 1
    b = min(a + 1, source_last)
    a_clamped = max(request.source_start_frame, min(a, source_last))

    # 构造 b 的结果（同域、位置 +1）
    result_b = FrameMapResult(
        source_frame=b,
        source_time_seconds=result.source_time_seconds + 1.0 / _effective_source_fps(request),
        timeline_time_seconds=result.timeline_time_seconds,
        cadence_action="repeat" if frac > 0.0 else "exact",
        fractional_source_frame=exact + 1,
    )
    result_a = FrameMapResult(
        source_frame=a_clamped,
        source_time_seconds=result.source_time_seconds,
        timeline_time_seconds=result.timeline_time_seconds,
        cadence_action=result.cadence_action,
        fractional_source_frame=exact,
    )
    weight = 0.0 if frac <= 0.0 else min(frac, 1.0)
    return result_a, result_b, weight


def _effective_source_fps(request: FrameMapRequest) -> float:
    """源帧率的浮点近似（元数据缺失时回退 timeline fps）。"""
    if request.source_fps_num and request.source_fps_den:
        return request.source_fps_num / float(request.source_fps_den)
    return request.timeline_fps_num / float(request.timeline_fps_den)


def _apply_frame_blend_layer(
    layer,
    layer_frame,
    frame_idx: int,
    get_layer_frame,
):
    """A3.2: frame_blend 双源帧加权（CPU 路径共用工具）。

    interpolation == "frame_blend" 时对 (a, b, w) 双解码帧线性混合；
    权重 0 / b 帧不可用 / 尺寸不一致时原样返回（诚实降级）。
    """
    import dataclasses

    clip = getattr(layer, "clip", None)
    speed_obj = getattr(clip, "speed", None)
    if speed_obj is None or getattr(speed_obj, "interpolation", "") != "frame_blend":
        return layer_frame
    if layer_frame is None:
        return layer_frame
    # 冻结片段全程定格：帧映射已被 freeze 短路，这里也不能再按变速
    # 混合两个运动帧（否则冻结失效、画面继续动）。
    if getattr(clip, "freeze_source_frame", None) is not None:
        return layer_frame
    a_rel, b_rel, weight = clip.timeline_to_source_frame_pair(int(frame_idx))
    if weight <= 0.0 or b_rel == a_rel:
        return layer_frame
    source_start = int(clip.source_start)
    # 管线解码的 layer.source_frame 是 round(exact)，而 a 帧是 floor
    # (exact)；frac > 0.5 时两者相差 1，直接拿传入帧当 a 帧会把混合
    # 退化成纯 nearest。基准帧不匹配时显式重取 a 帧。
    frame_a = layer_frame
    if int(getattr(layer, "source_frame", source_start + a_rel)) != source_start + a_rel:
        fetched_a = get_layer_frame(
            dataclasses.replace(layer, source_frame=source_start + a_rel))
        if fetched_a is not None and fetched_a.shape == layer_frame.shape:
            frame_a = fetched_a
    frame_b = get_layer_frame(
        dataclasses.replace(layer, source_frame=source_start + b_rel))
    if frame_b is None or frame_b.shape != layer_frame.shape:
        return layer_frame
    import numpy as np
    blended = (
        frame_a.astype(np.float32) * (1.0 - weight)
        + frame_b.astype(np.float32) * weight
    )
    return np.clip(blended + 0.5, 0, 255).astype(np.uint8, copy=False)




