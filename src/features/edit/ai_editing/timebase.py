"""TAEP 时基转换工具（Phase 1，补齐 §4.1 / §13.2）。

边界层秒/时码 → 整帧的转换，策略固定（TAEP §4.1）：

- 开始点 ``floor``、结束点 ``ceil``，并把转换结果与 warning 返回。
- 内部不存浮点秒位置；所有写入时刻是非负 int 帧号。
- 29.97/59.94 的 drop-frame 只影响显示/时码，不改变内部帧位置。

§13.2 必须覆盖的时间场景：24/25/24000/1001/30000/1001；一小时边界；相邻 clip；
floor/ceil；drop-frame 显示。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.shared.media.metadata import Rational


@dataclass(frozen=True)
class FrameConversionResult:
    """秒→帧转换结果（TAEP §4.1：返回转换结果 + warning）。"""

    start_frame: int
    end_frame: int  # 半开区间右端（已 ceil）
    duration_frames: int
    warnings: tuple[str, ...] = ()

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)


def seconds_to_frame_range(
    start_seconds: float,
    end_seconds: float,
    fps: Rational,
    *,
    drop_frame: bool = False,
) -> FrameConversionResult:
    """把 ``[start_seconds, end_seconds]`` 区间转为整帧 ``[start, end)``。

    策略（TAEP §4.1）：开始点 floor、结束点 ceil。``drop_frame`` 只影响显示，
    不改变内部帧位置（drop-frame timecode 与帧号解耦）。
    """
    warnings: list[str] = []
    if start_seconds < 0 or end_seconds < 0:
        raise ValueError(f"seconds must be non-negative: start={start_seconds}, end={end_seconds}")
    if end_seconds < start_seconds:
        raise ValueError(f"end_seconds ({end_seconds}) < start_seconds ({start_seconds})")

    fps_value = fps.to_fps()
    if fps_value <= 0:
        raise ValueError(f"fps must be positive: {fps}")

    raw_start = start_seconds * fps_value
    raw_end = end_seconds * fps_value
    start_frame = math.floor(raw_start)
    end_frame = math.ceil(raw_end)
    # 至少 1 帧
    if end_frame <= start_frame:
        end_frame = start_frame + 1
        warnings.append(f"区间过短，已扩展为最少 1 帧（{start_seconds}→{end_seconds}s @ {fps}）")

    # 精度丢失 warning（秒到帧非整数对齐）
    if abs(raw_start - start_frame) > 1e-9:
        warnings.append(f"开始点 {start_seconds}s 非整帧对齐，floor 到 {start_frame}")
    if abs(raw_end - (end_frame)) > 1e-9 and end_frame > 0:
        warnings.append(f"结束点 {end_seconds}s 非整帧对齐，ceil 到 {end_frame}")

    return FrameConversionResult(
        start_frame=start_frame,
        end_frame=end_frame,
        duration_frames=end_frame - start_frame,
        warnings=tuple(warnings),
    )


def _drop_frame_params(fps: Rational) -> tuple[int, int]:
    """SMPTE drop-frame 参数（仅 NTSC 1001 系帧率有定义）。

    Returns:
        (nominal, drop_per_minute)——nominal=30/60，drop=2/4。
        非 1001 系帧率抛 ValueError（25/24 等按标准恒为 NDF，不猜）。
    """
    fps_value = fps.to_fps()
    nominal = round(fps_value)
    if fps.den != 1001 or fps.num != nominal * 1000:
        raise ValueError(
            f"drop-frame timecode undefined for {fps_value:g}fps "
            f"(SMPTE DF 仅 1001 系 NTSC 帧率；此帧率按 NDF)")
    return nominal, max(1, round(nominal / 15))


def timecode_to_frames(timecode: str, fps: Rational, *, drop_frame: bool = False) -> int:
    """时码（HH:MM:SS:FF 或 HH:MM:SS;FF）→ 整帧。

    换算纪律（AUD-14700）：与 ``seconds_to_frame_range`` 落在同一帧号体系——
    时码的墙钟部分（HH:MM:SS）按**真实**帧率换算（``round(seconds * fps)``，
    有理数整数运算到最后一刻取整），帧字段 FF 直接作为帧偏移叠加。``;``
    分隔符与 ``drop_frame`` 是时码显示约定（TAEP §4.1：不改变内部帧位置），
    显示侧补偿由 ``frames_to_timecode`` 的 ``drop_frame`` 标志承担。

    M03 DF/NDF：``;`` 分隔或 ``drop_frame=True`` 时按 SMPTE drop-frame
    语义校验字段——非 10 整分钟的第 0 秒前 ``drop_per_minute`` 个帧号
    （29.97 为 FF 0/1）是被跳过的非法码，抛 ValueError 不静默映射为
    错误帧号（合法 DF 码的墙钟映射与上述真实帧率换算天然一致）。

    字段域（AUD-14701）：``0 <= MM,SS <= 59``、``0 <= FF < round(fps)``（名义
    帧率）、``HH >= 0``；越界的畸形时码抛 ``ValueError``，不静默映射为错误帧号。
    """
    uses_df = drop_frame or ";" in timecode
    parts = timecode.replace(";", ":").split(":")
    if len(parts) != 4:
        raise ValueError(f"invalid timecode {timecode!r} (expected HH:MM:SS:FF)")
    try:
        h, m, s, f = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(
            f"invalid timecode {timecode!r} (fields must be integers)"
        ) from exc
    fps_value = fps.to_fps()
    if fps_value <= 0:
        raise ValueError(f"fps must be positive: {fps}")
    nominal = round(fps_value)  # 29.97 -> 30（帧字段的域上界）
    if h < 0 or not 0 <= m <= 59 or not 0 <= s <= 59 or not 0 <= f < max(nominal, 1):
        raise ValueError(
            f"invalid timecode {timecode!r} (field out of range: "
            f"h={h}, m={m}, s={s}, f={f} for fps={fps_value:g})"
        )
    if uses_df:
        nominal_df, drop_per_minute = _drop_frame_params(fps)
        total_minutes = h * 60 + m
        if s == 0 and f < drop_per_minute \
                and total_minutes % 10 != 0 and total_minutes != 0:
            raise ValueError(
                f"invalid drop-frame timecode {timecode!r}: "
                f"FF 0..{drop_per_minute - 1} 在非 10 整分钟的第 0 秒被跳过"
                "（SMPTE DF 编号）")
        # DF 逆映射：名义帧数（字段按名义帧率计数）− 跳过数。
        # 分钟内与 AUD-14700 的墙钟实率法差至多 drop-1 帧——DF 编号
        # 的定义即"名义计数内跳号"，display↔parse 恒等往返以此为准
        # （锚点：00:01:00;02→1800、00:10:00;00→17982）。
        nominal_total = ((h * 60 + m) * 60 + s) * nominal_df + f
        dropped = drop_per_minute * (total_minutes - total_minutes // 10)
        return nominal_total - dropped
    # AUD-14700：墙钟部分按真实 fps 换算（整数 num/den + half-away-from-zero），
    # 不再按名义帧率——否则 NTSC 下时码入口与秒入口对同一墙钟时刻给出不同帧号
    # （如 29.97 的 01:00:00:00 旧算法差 108 帧）。
    seconds = (h * 60 + m) * 60 + s
    num = seconds * fps.num
    den = fps.den
    return (num + den // 2) // den + f


def frames_to_timecode(frames: int, fps: Rational, *, drop_frame: bool = False) -> str:
    """整帧 → 时码（HH:MM:SS:FF；``drop_frame=True`` 为 SMPTE DF 显示）。

    M03 DF/NDF：NTSC 1001 系帧率下 ``drop_frame=True`` 输出真实 DF 编号
    ——内部帧号不变（TAEP §4.1），显示侧跳过非 10 整分钟第 0 秒的
    0..drop-1 号（29.97 每分钟 2 帧、每 10 分钟不跳；60fps 对应 4 帧）。
    """
    fps_value = fps.to_fps()
    nominal = round(fps_value)
    sep = ";" if drop_frame else ":"
    if not drop_frame:
        f = frames % nominal
        total_seconds = frames // nominal
        s = total_seconds % 60
        m = (total_seconds // 60) % 60
        h = total_seconds // 3600
        return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"
    _, drop_per_minute = _drop_frame_params(fps)
    # DF 显示重编号（内部帧号不变）：internal → nominal（补回被跳过
    # 的帧号）。锚点（29.97）：1800→00:01:00;02、17982→00:10:00;00、
    # 17985→00:10:00;03。
    frames_per_10min = 10 * 60 * nominal - 9 * drop_per_minute
    frames_per_min_inner = 60 * nominal - drop_per_minute
    d, m10 = divmod(int(frames), frames_per_10min)
    nominal_frames = int(frames) + drop_per_minute * 9 * d
    if m10 > drop_per_minute:
        nominal_frames += drop_per_minute * (
            (m10 - drop_per_minute) // frames_per_min_inner)
    f = nominal_frames % nominal
    total_seconds = nominal_frames // nominal
    s = total_seconds % 60
    m = (total_seconds // 60) % 60
    h = total_seconds // 3600
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"


# ---------------------------------------------------------------------- #
# TimebaseService：provider 原始 PTS/timebase → canonical source FrameRange
# （M2 分析数据地基 / §18.2）
# ---------------------------------------------------------------------- #
#
# 现有 ``seconds_to_frame_range`` 只做"秒 → 帧"，依赖调用方已把 provider 时间换算成
# 浮点秒。但产品计划 §18.2 明确：**精确时间戳不能仅保存浮点秒**，provider 原始输出
# 应先保留 PTS + 有理数 timebase / sample offset，再由统一时间基转换为 Topos 的整帧
# 半开区间；对 VFR 素材绝不使用简单的 ``seconds × fps``。
#
# 本节提供：
# - ``ProviderTimebase``：provider 原始时间基（PTS + timebase / sample_rate）。
# - ``TimebaseService``：把 provider 原始时间换算为 canonical source ``FrameRange``，
#   rounding policy 可审计（返回 warning），VFR 标注 limitation 而非静默用 seconds×fps。
#
# 与 ``seconds_to_frame_range`` 并存：旧入口仍是边界层（用户输入秒/时码）的转换工具；
# 新入口服务 provider 证据（转录/OCR/shot 的原始 PTS）。


@dataclass(frozen=True)
class ProviderTimebase:
    """provider 原始时间基（§18.2）。

    provider（WhisperX / FFmpeg / OCR）输出时间戳时携带的原始坐标。常见形态：

    - 视频/容器 PTS：``pts``（整数）+ ``timebase``（Rational，如 ``1/90000``）。
    - 音频 sample offset：``pts``（sample 数）+ ``sample_rate``（如 48000）。
    - 已换算秒：``pts`` 留空，调用方直接用旧 ``seconds_to_frame_range``。

    至少提供 (pts + timebase) 或 (pts + sample_rate) 之一，否则视为"无原始坐标"，
    调用方应回退到秒路径并在证据上标注低精度。

    Attributes:
        pts_start: 起点 PTS（含）。None 表示 provider 未给出原始 PTS。
        pts_end: 终点 PTS（不含）。
        timebase: PTS 的有理数时间基（如 ``Rational(1, 90000)``）。
        sample_rate: 音频 sample rate（当 PTS 是 sample offset 时）。
    """

    pts_start: int | None = None
    pts_end: int | None = None
    timebase: Rational | None = None
    sample_rate: int | None = None

    @property
    def has_pts(self) -> bool:
        """是否携带可换算的原始 PTS。"""
        return self.pts_start is not None and (
            self.timebase is not None or self.sample_rate is not None
        )

    @property
    def timebase_label(self) -> str | None:
        """供 ``SemanticSegment.source_timebase`` 的人类可读标签。"""
        if self.timebase is not None:
            return str(self.timebase)
        if self.sample_rate is not None:
            return f"sample_rate={self.sample_rate}"
        return None


@dataclass(frozen=True)
class TimebaseConversionResult:
    """provider PTS → source FrameRange 转换结果（含 warning，可审计）。

    Attributes:
        source_range: canonical 整帧半开区间 ``[start, start+duration)``。
        warnings: 换算过程中的精度/局限提示（VFR、非整帧对齐等）。
    """

    source_range: Any  # FrameRange；用 Any 避免 import 循环
    warnings: tuple[str, ...] = ()

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)


class TimebaseService:
    """provider 原始时间 → canonical source ``FrameRange``（整帧半开区间）。

    设计（§18.2）：

    1. **不用 seconds × fps**。换算公式：``frame = round(pts * timebase * media_fps)``，
      全程有理数/整数运算到最后一刻才取整；rounding policy（``round``）可审计。
    2. **VFR 安全**。当 ``media_fps`` 为 CFR（恒定帧率）时直接换算；若调用方声明素材
       是 VFR，返回 warning 标注 limitation（首版按 CFR 近似，不静默假装精确）。后续
       里程碑可扩展为查帧时间码表（frame timestamp map）。
    3. **音频 sample offset**。当 provider 给的是音频 sample（如词级对齐），用
       ``sample_rate`` 把 sample → 秒 → 帧，并记录 warning（跨域换算精度）。
    4. rounding 一致：起点 ``round``、终点 ``round``，至少 1 帧。

    无状态、线程安全；可单例复用。
    """

    def to_source_frame_range(
        self,
        provider_time: ProviderTimebase,
        media_fps: Rational,
        *,
        is_vfr: bool = False,
    ) -> TimebaseConversionResult:
        """把 ``ProviderTimebase`` 转为 source ``FrameRange``（``[start, end)``）。

        Args:
            provider_time: provider 原始时间（PTS + timebase / sample_rate）。
            media_fps: 素材帧率（CFR；VFR 见 ``is_vfr``）。
            is_vfr: 素材是否为可变帧率。True 时按 CFR 近似但标注 limitation warning。
        """
        warnings: list[str] = []

        if not provider_time.has_pts:
            raise ValueError(
                "ProviderTimebase has no usable PTS (need pts_start + timebase or sample_rate)"
            )
        if is_vfr:
            warnings.append(
                "asset is VFR: converting provider PTS with CFR assumption; "
                "frame boundaries are approximate (precise VFR mapping is a later milestone)"
            )

        pts_start = provider_time.pts_start
        assert pts_start is not None  # has_pts 已保证
        pts_end = provider_time.pts_end if provider_time.pts_end is not None else pts_start

        start_frame = self._pts_to_frame(pts_start, provider_time, media_fps)
        end_frame = self._pts_to_frame(pts_end, provider_time, media_fps)
        if end_frame <= start_frame:
            end_frame = start_frame + 1
            warnings.append(
                f"provider PTS range collapsed to <1 frame (pts {pts_start}->{pts_end}); "
                f"expanded to 1 frame at {start_frame}"
            )

        # 精度提示：若 timebase 不是 media_fps 的整数约数，单 PTS 可能不整帧对齐
        if provider_time.timebase is not None and not _is_frame_aligned(
            provider_time.timebase, media_fps
        ):
            warnings.append(
                f"provider timebase {provider_time.timebase} is not an integer divisor "
                f"of media fps {media_fps}; frame alignment is rounded"
            )

        # 局部 import 避免与 editorial_models 的潜在循环
        from src.features.edit.ai_editing.editorial_models import FrameRange

        source_range = FrameRange(start_frame, end_frame - start_frame)
        return TimebaseConversionResult(source_range=source_range, warnings=tuple(warnings))

    def from_pts(
        self, pts: int, provider_timebase: Rational, media_fps: Rational,
    ) -> int:
        """单个 PTS → 帧号（``round``）。

        公式：``frame = round(pts * provider_timebase * media_fps)``。
        ``provider_timebase`` 是 PTS 单位（如 ``1/90000``）；乘以 media_fps 得帧号。
        """
        return _pts_to_frame_via_timebase(pts, provider_timebase, media_fps)

    def from_audio_sample(
        self, sample: int, sample_rate: int, media_fps: Rational,
    ) -> int:
        """音频 sample offset → 帧号。``frame = round(sample / sample_rate * media_fps)``。

        AUD-14703：遵守本类"全程有理数/整数运算到最后一刻才取整"的承诺——
        ``frame = sample * fps.num / (sample_rate * fps.den)``，只在最后做一次
        常规四舍五入（half away from zero，与 ``_pts_to_frame_via_timebase``
        同一策略族），不再经 ``to_fps()`` 浮点中转。
        """
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive: {sample_rate}")
        # frame = sample * (fps.num/fps.den) / sample_rate
        #       = sample * fps.num / (sample_rate * fps.den)
        num = sample * media_fps.num
        den = sample_rate * media_fps.den
        if den == 0:
            raise ValueError(
                f"zero denominator in sample_rate/fps: {sample_rate} / {media_fps}"
            )
        # 常规四舍五入（half away from zero），与 _pts_to_frame_via_timebase 一致
        return (num + den // 2) // den if num >= 0 else -((-num + den // 2) // den)

    # ---------------- internal ---------------- #

    def _pts_to_frame(
        self, pts: int, provider_time: ProviderTimebase, media_fps: Rational,
    ) -> int:
        if provider_time.timebase is not None:
            return _pts_to_frame_via_timebase(pts, provider_time.timebase, media_fps)
        if provider_time.sample_rate is not None:
            return self.from_audio_sample(pts, provider_time.sample_rate, media_fps)
        # has_pts 已保证不会走到这里
        raise ValueError("ProviderTimebase has no timebase or sample_rate")


def _pts_to_frame_via_timebase(
    pts: int, timebase: Rational, media_fps: Rational,
) -> int:
    """PTS → 帧号（有理数运算到最后一刻取整）。

    ``frame = pts * timebase * media_fps``，全部用整数 num/den 运算避免浮点误差，
    最后用 banker's rounding 之外的常规 round（与既有 seconds_to_frame_range 一致性）。
    """
    # frame = pts * (timebase.num/timebase.den) * (fps.num/fps.den)
    #       = pts * timebase.num * fps.num / (timebase.den * fps.den)
    num = pts * timebase.num * media_fps.num
    den = timebase.den * media_fps.den
    if den == 0:
        raise ValueError(f"zero denominator in timebase/fps: {timebase} / {media_fps}")
    # 常规四舍五入（half away from zero），与 seconds_to_frame_range 的策略族一致
    return (num + den // 2) // den if num >= 0 else -((-num + den // 2) // den)


def _is_frame_aligned(timebase: Rational, media_fps: Rational) -> bool:
    """provider timebase 是否是 media_fps 的整数约数（即每帧 PTS 恰为整数）。

    每帧 PTS = 1/fps / timebase = (1/fps) * (1/timebase)。
    当 ``media_fps.den % timebase.num == 0`` 且结果为整数时对齐。
    """
    # 每帧的 timebase-tick 数 = media_fps.den / (timebase.num * ...) 简化判定：
    # 若 timebase == 1/fps（即 timebase.num*fps.num == fps.den*timebase.den 的约数关系），
    # 则整帧对齐。这里用一个充分条件：fps 转 timebase 域是整数。
    if timebase.num == 0:
        return False
    ticks_per_frame_num = media_fps.den * timebase.den
    ticks_per_frame_den = media_fps.num * timebase.num
    if ticks_per_frame_den == 0:
        return False
    return ticks_per_frame_num % ticks_per_frame_den == 0
