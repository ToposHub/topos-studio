# 状态(2026-08-27 P1-16): 可选适配器，已接入生产入口（File 菜单导入/导出）。
# opentimelineio 未安装时入口预检提示并禁用，不阻塞主程序。
"""OTIO Adapter（Phase 8，按需）。

OpenTimelineIO 是**可选**适配器（TAEP §9.5）：用于和 Python/DCC 工具交换
clip/track/marker/transition，不替代 FCPXML 的 NLE 交接，也不把 OTIO schema
作为 Topos 内部模型。

依赖策略：``opentimelineio`` 是可选依赖。未安装时 ``inspect`` 报告不可用（graceful
degrade），不阻塞主程序。安装后实现 read/write 的共有子集。

实现与 FCPXML 同一个 ``InterchangeAdapter`` 协议，便于统一注册。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.features.edit.adapters.interchange_types import (
    ExportResult,
    InterchangeEntry,
    InterchangeEntryCategory,
    InterchangeInspection,
    InterchangeReport,
    ImportResult,
)
from src.shared.media.metadata import Rational


OTIO_FORMAT_ID = "otio"

#: AUD-12617：常见帧率 → 精确有理数（避免 29.97 浮点近似污染帧换算）。
_COMMON_RATE_RATIONALS: tuple[tuple[float, Rational], ...] = (
    (23.976, Rational(24000, 1001)),
    (23.98, Rational(24000, 1001)),
    (29.97, Rational(30000, 1001)),
    (59.94, Rational(60000, 1001)),
    (119.88, Rational(120000, 1001)),
)


def _rate_to_rational(rate: float) -> Rational:
    """OTIO 帧率（float）→ 精确 Rational（NTSC 用 1001 基有理数）。"""
    try:
        value = float(rate)
    except (TypeError, ValueError):
        return Rational(24, 1)
    if value <= 0.0:
        return Rational(24, 1)
    for candidate, rational in _COMMON_RATE_RATIONALS:
        if abs(value - candidate) < 0.01:
            return rational
    return Rational(int(round(value)), 1)


def _rescale_to_frames(value: float, from_rate: float, to_rate: float) -> int:
    """把 *from_rate* 率下的 RationalTime.value 换算为 *to_rate* 率下的整帧。

    AUD-12617：OTIO 的 ``RationalTime.value`` 以其自身 ``rate`` 为单位，
    直接当序列帧使用会在非序列率素材上引入倍数误差。
    """
    try:
        v = float(value)
        src = float(from_rate) if from_rate else 0.0
        dst = float(to_rate) if to_rate else 0.0
    except (TypeError, ValueError):
        return 0
    if dst <= 0.0:
        return 0
    if src <= 0.0:
        src = dst
    return int(round(v * src / dst))


def _rational_time_parts(rt: Any) -> tuple[float, float]:
    """安全读取 OTIO RationalTime 的 (value, rate)；缺失字段回退 (0, 0)。"""
    try:
        value = float(getattr(rt, "value", 0.0) or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    rate = getattr(rt, "rate", None)
    try:
        rate_value = float(rate) if rate is not None else 0.0
    except (TypeError, ValueError):
        rate_value = 0.0
    return value, rate_value


def _timeline_rate(timeline: Any, default: float = 24.0) -> float:
    """取 OTIO timeline 的序列帧率（duration().rate 优先，global_start_time 次之）。"""
    candidates = []
    try:
        candidates.append(_rational_time_parts(timeline.duration())[1])
    except (AttributeError, TypeError, ValueError):
        pass
    gst = getattr(timeline, "global_start_time", None)
    if gst is not None:
        candidates.append(_rational_time_parts(gst)[1])
    for rate in candidates:
        if rate and rate > 0.0:
            return float(rate)
    return default


def _otio_available() -> bool:
    """检查 opentimelineio 是否安装（可选依赖）。"""
    try:
        import opentimelineio  # noqa: F401
        return True
    except ImportError:
        return False


def otio_available() -> bool:
    """公开的可用性检查（P1-16 UI 入口预检用）。"""
    return _otio_available()


class OTIOAdapter:
    """OTIO 可选适配器（TAEP §9.5）。

    未安装 opentimelineio 时，所有方法报告不可用；不抛异常、不阻塞主程序。
    """

    format_id = OTIO_FORMAT_ID

    def inspect(self, source: str | Path) -> InterchangeInspection:
        if not _otio_available():
            return InterchangeInspection(
                format_id=OTIO_FORMAT_ID, supported=False,
                report=InterchangeReport(
                    format_id=OTIO_FORMAT_ID,
                    diagnostics=[{
                        "severity": "warning", "code": "TAEP_INTERCHANGE_LOSSY",
                        "message": "opentimelineio not installed; OTIO adapter unavailable",
                    }],
                ),
            )
        try:
            import opentimelineio as otio
            timeline = otio.adapters.read_from_file(str(source))
            return InterchangeInspection(
                format_id=OTIO_FORMAT_ID, supported=True,
                version=getattr(otio, "version", "unknown"),
                asset_count=len(timeline.tracks) if hasattr(timeline, "tracks") else 0,
                timeline_count=1,
            )
        except Exception as exc:
            return InterchangeInspection(
                format_id=OTIO_FORMAT_ID, supported=False,
                report=InterchangeReport(
                    format_id=OTIO_FORMAT_ID,
                    diagnostics=[{"severity": "error", "code": "TAEP_INTERCHANGE_LOSSY",
                                  "message": f"OTIO read failed: {exc}"}],
                ),
            )

    def import_to_canonical(
        self, source: str | Path, *, options: dict[str, Any] | None = None
    ) -> ImportResult:
        if not _otio_available():
            return ImportResult(
                sequences=(), assets=(),
                report=InterchangeReport(
                    format_id=OTIO_FORMAT_ID,
                    diagnostics=[{"severity": "warning", "code": "TAEP_INTERCHANGE_LOSSY",
                                  "message": "opentimelineio not installed"}],
                ),
            )
        from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
        from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
        import opentimelineio as otio

        timeline = otio.adapters.read_from_file(str(source))
        entries: list[InterchangeEntry] = []
        # AUD-12617：序列帧率取自 timeline 自身（duration/global_start_time 的
        # rate），不再硬编码 24fps。
        seq_rate = _timeline_rate(timeline)
        fps = _rate_to_rational(seq_rate)
        video_tracks: list[EditTrack] = []
        audio_tracks: list[EditTrack] = []
        v_idx = 0
        a_idx = 0
        clip_idx = 0

        for track in timeline.tracks:
            kind_str = str(getattr(track, "kind", ""))
            track_kind = "video" if kind_str.endswith("Video") else "audio"
            track_clips: list[ClipInstance] = []
            # AUD-12617：timeline_start 由轨道游标按前序 item 累加得出
            # （clip 自身 duration 是其裁剪后时长，不是轨道位置）。
            cursor = 0.0  # 序列率下的帧游标
            for child in track:
                sr = getattr(child, "source_range", None)
                dur_value, dur_rate = _rational_time_parts(
                    getattr(sr, "duration", None)
                )
                if not dur_rate:
                    # 无 source_range 的 item（如某些 Gap）：尝试整体 duration
                    # 属性（OTIO 的 duration() 是方法，不可直接 getattr 取值）。
                    try:
                        dur_value, dur_rate = _rational_time_parts(child.duration())
                    except (AttributeError, TypeError, ValueError):
                        dur_value, dur_rate = 0.0, seq_rate
                dur_frames = _rescale_to_frames(dur_value, dur_rate or seq_rate, seq_rate)
                if isinstance(child, otio.schema.Transition):
                    # OTIO 转场与前后 clip 重叠：cut_overlap 是被折叠掉的时长
                    co_value, co_rate = _rational_time_parts(
                        getattr(child, "cut_overlap", None)
                    )
                    cursor += dur_frames - _rescale_to_frames(
                        co_value, co_rate or seq_rate, seq_rate,
                    )
                    continue
                if isinstance(child, otio.schema.Clip):
                    start_value, start_rate = _rational_time_parts(
                        getattr(sr, "start_time", None)
                    )
                    mr = getattr(child, "media_reference", None)
                    asset_id = getattr(mr, "target_url", None) or f"otio-{clip_idx}"
                    clip = ClipInstance(
                        clip_id=f"otio-{clip_idx}", asset_id=str(asset_id),
                        track_id="v1" if track_kind == "video" else "a1",
                        timeline_start=int(round(cursor)),
                        timeline_duration=max(1, dur_frames),
                        source_start=_rescale_to_frames(
                            start_value, start_rate or seq_rate, seq_rate,
                        ),
                        source_duration=max(1, dur_frames),
                        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
                    )
                    clip_idx += 1
                    track_clips.append(clip)
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.PRESERVED, element="clip",
                        description=f"imported otio clip {clip.clip_id}",
                    ))
                # Gap 等非 Clip item：不产出 clip，但推进游标保持轨道时序
                cursor += dur_frames
            if not track_clips:
                continue
            track_clips.sort(key=lambda c: c.timeline_start)
            if track_kind == "video":
                v_idx += 1
                video_tracks.append(EditTrack(
                    track_id=f"v{v_idx}", name=f"V{v_idx}", track_type="video",
                    index=v_idx - 1, clips=tuple(track_clips),
                ))
            else:
                # AUD-12618：音频轨不再静默丢弃——与视频轨同法建轨。
                a_idx += 1
                audio_tracks.append(EditTrack(
                    track_id=f"a{a_idx}", name=f"A{a_idx}", track_type="audio",
                    index=a_idx - 1, clips=tuple(track_clips),
                ))

        seq = TimelineSequence(
            timeline_id="otio-seq", name=getattr(timeline, "name", "OTIO Import"),
            fps=fps, width=1920, height=1080, sample_rate=48000,
            video_tracks=tuple(video_tracks),
            audio_tracks=tuple(audio_tracks),
        )
        return ImportResult(
            sequences=(seq,),
            report=InterchangeReport(format_id=OTIO_FORMAT_ID, entries=tuple(entries)),
        )

    def export_from_canonical(
        self, sequence: Any, *, options: dict[str, Any] | None = None
    ) -> ExportResult:
        if not _otio_available():
            raise RuntimeError("opentimelineio not installed; cannot export OTIO")
        import opentimelineio as otio

        options = options or {}
        output_path = options.get("output_path", "")
        if not output_path:
            raise ValueError("export requires output_path in options")

        timeline = otio.schema.Timeline(name=sequence.name or "Topos Export")
        fps_value = sequence.fps.to_fps() if hasattr(sequence.fps, "to_fps") else 24.0

        # AUD-12619：引用 URL 必须指向磁盘真实文件。调用方可通过
        # options["asset_paths"] 传入 asset_id → 本地路径映射；无映射时写
        # MissingReference 并记 REQUIRES_USER_ACTION，绝不用内部 uuid 伪造
        # file:// URL（第三方工具无法据此重连媒体）。
        asset_paths: dict[str, str] = {
            str(k): str(v)
            for k, v in (options.get("asset_paths") or {}).items()
        }
        missing_assets: set[str] = set()
        entries: list[InterchangeEntry] = []

        for track in sequence.video_tracks:
            otio_track = otio.schema.Track(name=track.name, kind="Video")
            for clip in track.clips:
                # item_reference_range / source_range
                src = otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(clip.source_start, fps_value),
                    duration=otio.opentime.RationalTime(clip.timeline_duration, fps_value),
                )
                local_path = asset_paths.get(str(clip.asset_id), "")
                if local_path:
                    media_ref: Any = otio.schema.ExternalReference(
                        target_url=Path(local_path).expanduser().resolve().as_uri(),
                        available_range=src,
                    )
                else:
                    media_ref = otio.schema.MissingReference()
                    missing_assets.add(str(clip.asset_id))
                otio_clip = otio.schema.Clip(
                    name=clip.clip_id, media_reference=media_ref, source_range=src,
                )
                otio_track.append(otio_clip)
            timeline.tracks.append(otio_track)

        entries.append(InterchangeEntry(
            category=InterchangeEntryCategory.PRESERVED, element="timeline",
            description="exported to OTIO",
        ))
        for asset_id in sorted(missing_assets):
            entries.append(InterchangeEntry(
                category=InterchangeEntryCategory.REQUIRES_USER_ACTION, element="clip",
                description=(
                    f"素材 {asset_id} 无可解析路径（options['asset_paths'] 未提供），"
                    f"OTIO 引用为 MissingReference，外部工具需手动重连媒体"
                ),
            ))

        otio.adapters.write_to_file(timeline, output_path)
        return ExportResult(
            output_path=str(output_path),
            report=InterchangeReport(
                format_id=OTIO_FORMAT_ID, entries=tuple(entries),
            ),
        )


# ---------------------------------------------------------------------- #
# Nested sequence 映射策略（TAEP §9 / Phase 8）
# ---------------------------------------------------------------------- #


NESTED_SEQUENCE_POLICY = ("flatten", "retain", "reference")
"""嵌套时间线映射策略（TAEP §12 P8）：

- ``flatten``：把嵌套 sequence 展平到父时间线（丢失嵌套结构，但简单）。
- ``retain``：保留嵌套结构（需要目标格式支持 nested/compound）。
- ``reference``：引用外部 sequence 文件（适合 pipeline 工具链）。

默认 ``flatten``；``retain``/``reference`` 需要目标格式支持且用户明确选择。
"""


def flatten_nested_sequence(sequence: Any, sequences_by_id: dict[str, Any]) -> Any:
    """把嵌套 sequence（引用其他 sequence 的 clip）展平到单层。

    TAEP §12 P8：``flatten`` 策略。保留 clip placement 与 source range；丢失
    嵌套元数据。供导出到不支持嵌套的格式（如某些 OTIO pipeline）时使用。
    """
    from src.features.edit.core.timeline_v2 import EditTrack

    def _resolve_clips(track):
        new_clips = []
        for clip in track.clips:
            # 若 clip 引用了嵌套 sequence（通过 asset_id 指向另一 sequence）
            nested = sequences_by_id.get(clip.asset_id)
            if nested is not None and hasattr(nested, "video_tracks"):
                # 递归展平：把嵌套的 clips 按 timeline_start 偏移加入
                offset = clip.timeline_start
                for ntrack in nested.video_tracks:
                    for nclip in ntrack.clips:
                        from dataclasses import replace
                        new_clips.append(replace(
                            nclip,
                            clip_id=f"{clip.clip_id}_{nclip.clip_id}",
                            timeline_start=nclip.timeline_start + offset,
                        ))
            else:
                new_clips.append(clip)
        return EditTrack(
            track_id=track.track_id, name=track.name, track_type=track.track_type,
            index=track.index, clips=tuple(new_clips),
        )

    return sequence._rebuild(
        video_tracks=tuple(_resolve_clips(t) for t in sequence.video_tracks),
    )
