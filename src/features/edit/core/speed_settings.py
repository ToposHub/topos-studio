"""B14：高级速度设置的版本化契约与接收层规划。

接收链（计划 §B14）：EditPage 读取最新上下文 → EditController 编排 →
本模块做**契约校验 + 能力预检 + 计划构建** → 既有命令栈提交边界。

- 契约版本：``SPEED_SETTINGS_VERSION``。settings 是 UI 草稿（版本、
  类型、枚举、数值域在此归一校验），不是已提交语义；
- 一致性：Speed/Duration 是同一速度意图的两种表达——请求时长必须
  与完整速度积分（混合帧率统一换算）一致（容差 ±1 帧），否则诊断
  拒绝，不分别覆盖互相矛盾的值；
- 能力矩阵（stage-1）：基础 speed/reverse/pitch、keyframe_timing
  （stretch/maintain）、ripple、freeze（整段定格在源 In）**支持**；
  速度点曲线、既有 freeze 片段、retime_controls 的分段操作
  （stretch/clear_point 段语义、freeze/reverse/rewind/ramp/reset）
  **明确不支持**——返回诊断、零写入，不静默降级。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.timeline_v2 import ClipInstance, TimelineSequence

SPEED_SETTINGS_VERSION = 1

_DURATION_TOLERANCE_FRAMES = 1


@dataclass(frozen=True)
class SpeedSettingsPlan:
    """接收层输出：可执行的命令，或明确的不支持/诊断信息。

    三态：``command`` 非空 = 可单事务执行；``unsupported`` 非空 = 能力
    矩阵外（零写入 + 用户可读原因）；``error`` 非空 = 契约/一致性
    校验失败（零写入 + 诊断）。三者互斥。
    """

    command: Any | None = None
    unsupported: str | None = None
    error: str | None = None

    @property
    def executable(self) -> bool:
        return self.command is not None


def normalize_settings(settings: Any) -> dict[str, Any]:
    """契约校验：类型/枚举/数值域，非法抛 ``ValueError``。"""
    if not isinstance(settings, dict):
        raise ValueError("speed settings must be a dict")
    version = settings.get("version", SPEED_SETTINGS_VERSION)
    if version != SPEED_SETTINGS_VERSION:
        raise ValueError(
            f"unsupported speed settings version: {version!r}")
    if settings.get("operation", "dialog") != "dialog":
        raise ValueError(
            f"unknown operation: {settings.get('operation')!r}")
    speed = settings.get("speed")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise ValueError("speed must be numeric")
    speed = float(speed)
    if not 0.1 <= speed <= 8.0:
        raise ValueError(f"speed out of range [0.1, 8.0]: {speed}")
    duration = settings.get("duration_frames")
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ValueError("duration_frames must be an integer")
    if duration < 1:
        raise ValueError(f"duration_frames must be positive: {duration}")
    timing = settings.get("keyframe_timing", "stretch")
    if timing not in ("stretch", "maintain"):
        raise ValueError(f"invalid keyframe_timing: {timing!r}")
    for key in ("reverse", "freeze", "preserve_pitch", "ripple"):
        if not isinstance(settings.get(key, False), bool):
            raise ValueError(f"{key} must be a boolean")
    return {
        "version": SPEED_SETTINGS_VERSION,
        "operation": "dialog",
        "speed": speed,
        "reverse": bool(settings.get("reverse", False)),
        "freeze": bool(settings.get("freeze", False)),
        "preserve_pitch": bool(settings.get("preserve_pitch", False)),
        "ripple": bool(settings.get("ripple", False)),
        "keyframe_timing": timing,
        "duration_frames": int(duration),
    }


def check_capability(clip: ClipInstance,
                     normalized: dict[str, Any]) -> str | None:
    """能力预检：返回 None = 支持；否则用户可读的不支持原因。"""
    params = clip.speed if isinstance(clip.speed, SpeedParams) else None
    if params is not None and params.is_variable_speed:
        return ("Speed-point curves are not supported by this operation "
                "yet; the clip is unchanged.")
    if clip.freeze_source_frame is not None:
        return ("The clip is already frozen; speed settings are not "
                "supported yet. The clip is unchanged.")
    return None


def _derived_duration(clip: ClipInstance, timeline: TimelineSequence,
                      normalized: dict[str, Any]) -> int:
    from src.features.edit.core.timeline_ops import (
        timeline_duration_for_source,
    )

    params = SpeedParams(
        speed=normalized["speed"],
        reverse=normalized["reverse"],
        preserve_pitch=normalized["preserve_pitch"],
    )
    seq_fps = getattr(timeline, "fps", None)
    seq_fps_num = seq_fps.num if hasattr(seq_fps, "num") else None
    seq_fps_den = seq_fps.den if hasattr(seq_fps, "den") else 1
    return timeline_duration_for_source(
        clip.source_duration, params,
        clip._source_fps_num, clip._source_fps_den,
        clip._timeline_fps_num or seq_fps_num,
        clip._timeline_fps_den or seq_fps_den,
    )


def plan_speed_settings_edit(
    sequence: TimelineSequence,
    track_id: str,
    clip_id: str,
    settings: Any,
) -> SpeedSettingsPlan:
    """B14 接收层：校验 → 能力预检 → 一致性 → 构建单事务命令。"""
    try:
        normalized = normalize_settings(settings)
    except ValueError as exc:
        return SpeedSettingsPlan(error=f"Invalid speed settings: {exc}")

    track = sequence.get_track_by_id(track_id)
    clip = track.get_clip_by_id(clip_id) if track is not None else None
    if clip is None:
        return SpeedSettingsPlan(
            error=f"Clip not found: {clip_id!r}")

    unsupported = check_capability(clip, normalized)
    if unsupported is not None:
        return SpeedSettingsPlan(unsupported=unsupported)

    if not normalized["freeze"]:
        derived = _derived_duration(clip, sequence, normalized)
        if abs(derived - normalized["duration_frames"]) > \
                _DURATION_TOLERANCE_FRAMES:
            return SpeedSettingsPlan(error=(
                f"Speed {normalized['speed']:g}x implies {derived} frames, "
                f"but {normalized['duration_frames']} was requested; "
                f"the clip is unchanged."))

    from src.features.edit.core.commands import SetClipSpeedSettingsCommand

    return SpeedSettingsPlan(command=SetClipSpeedSettingsCommand.create(
        clip_id, track_id, normalized))
