"""Professional subtitle quality checks.

The editor's timeline model deliberately keeps cues lightweight.  This module
adds a pure, host-independent QC pass that can be used before sidecar export,
from the timeline UI, or by an automated delivery workflow.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip


@dataclass(frozen=True)
class SubtitleQualityIssue:
    code: str
    severity: str
    cue_id: str | None
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "cue_id": self.cue_id,
            "message": self.message,
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class SubtitleQualityReport:
    track_id: str
    cue_count: int
    issues: tuple[SubtitleQualityIssue, ...] = ()

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def error_count(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "cue_count": self.cue_count,
            "passed": self.passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _reading_units(text: str) -> int:
    """Count CJK glyphs and Latin/number words as readable units."""
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = re.sub(r"[\u3400-\u9fff]", " ", text)
    return cjk + len(re.findall(r"[^\W_]+", non_cjk, flags=re.UNICODE))


def inspect_subtitle_track(
    track: EditTrack,
    fps: Any,
    *,
    timeline_duration_frames: int | None = None,
    max_chars_per_line: int = 42,
    max_lines: int = 2,
    max_chars_per_second: float = 20.0,
    min_duration_seconds: float = 0.8,
    max_duration_seconds: float = 7.0,
) -> SubtitleQualityReport:
    """Inspect a subtitle track and return cue-specific QC issues.

    Defaults are conservative editorial limits, not hard-coded export rules;
    callers can pass a platform-specific profile when needed.
    """
    if track.track_type != "subtitle":
        raise ValueError("Only subtitle tracks can be inspected")
    try:
        rate = float(fps)
    except (TypeError, ValueError):
        rate = 0.0
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("fps must be finite and positive")
    if max_chars_per_line <= 0 or max_lines <= 0:
        raise ValueError("line limits must be positive")
    if not math.isfinite(max_chars_per_second) or max_chars_per_second <= 0:
        raise ValueError("max_chars_per_second must be finite and positive")

    issues: list[SubtitleQualityIssue] = []
    cues = sorted(track.subtitle_clips, key=lambda cue: (cue.timeline_start, cue.clip_id))
    for cue in cues:
        cue_id = cue.clip_id
        start = int(cue.timeline_start)
        end = int(cue.timeline_end)
        text = str(cue.text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if start < 0 or end <= start:
            issues.append(SubtitleQualityIssue(
                "invalid_timing", "error", cue_id,
                "字幕时间范围无效", {"start_frame": start, "end_frame": end},
            ))
            continue
        if timeline_duration_frames is not None and end > timeline_duration_frames:
            issues.append(SubtitleQualityIssue(
                "out_of_range", "error", cue_id,
                "字幕超出时间线范围", {"end_frame": end, "timeline_duration": timeline_duration_frames},
            ))
        if not text:
            issues.append(SubtitleQualityIssue(
                "empty_text", "error", cue_id, "字幕文本为空",
            ))
            continue

        lines = text.splitlines() or [text]
        if len(lines) > max_lines:
            issues.append(SubtitleQualityIssue(
                "too_many_lines", "warning", cue_id,
                f"字幕超过 {max_lines} 行", {"line_count": len(lines)},
            ))
        longest_line = max((len(line.replace(" ", "")) for line in lines), default=0)
        if longest_line > max_chars_per_line:
            issues.append(SubtitleQualityIssue(
                "line_too_long", "warning", cue_id,
                f"单行字幕超过 {max_chars_per_line} 个字符",
                {"max_line_chars": longest_line},
            ))

        duration_seconds = (end - start) / rate
        units_per_second = _reading_units(text) / max(duration_seconds, 1e-9)
        if units_per_second > max_chars_per_second:
            issues.append(SubtitleQualityIssue(
                "reading_speed", "warning", cue_id,
                "字幕阅读速度过快",
                {"units_per_second": round(units_per_second, 2),
                 "limit": max_chars_per_second},
            ))
        if duration_seconds < min_duration_seconds:
            issues.append(SubtitleQualityIssue(
                "too_short", "warning", cue_id,
                "字幕显示时间过短", {"duration_seconds": round(duration_seconds, 3)},
            ))
        if duration_seconds > max_duration_seconds:
            issues.append(SubtitleQualityIssue(
                "too_long", "warning", cue_id,
                "字幕显示时间过长", {"duration_seconds": round(duration_seconds, 3)},
            ))

    previous: SubtitleClip | None = None
    for cue in cues:
        if previous is not None and cue.timeline_start < previous.timeline_end:
            issues.append(SubtitleQualityIssue(
                "overlap", "error", cue.clip_id,
                "字幕与前一条字幕重叠",
                {"previous_cue_id": previous.clip_id},
            ))
        if previous is None or cue.timeline_end > previous.timeline_end:
            previous = cue

    return SubtitleQualityReport(track.track_id, len(cues), tuple(issues))


__all__ = [
    "SubtitleQualityIssue",
    "SubtitleQualityReport",
    "inspect_subtitle_track",
]
