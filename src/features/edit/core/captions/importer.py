"""Parse common subtitle files into timeline-frame cue data."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


SUPPORTED_SUBTITLE_EXTENSIONS: frozenset[str] = frozenset({
    ".srt", ".vtt", ".ass", ".ssa", ".ttml", ".dfxp", ".xml",
})


@dataclass(frozen=True)
class ParsedSubtitleCue:
    """One parsed subtitle cue in timeline frame coordinates."""

    start_frame: int
    duration_frames: int
    text: str


_TIMING_LINE = re.compile(
    r"^\s*(?P<start>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})"
    r"\s*-->\s*(?P<end>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})"
)
_ASS_TAGS = re.compile(r"\{[^}]*\}")


def _fps_fraction(fps: Any) -> Fraction:
    if hasattr(fps, "num") and hasattr(fps, "den"):
        value = Fraction(int(fps.num), int(fps.den))
    else:
        try:
            value = Fraction(str(float(fps)))
        except (TypeError, ValueError, ZeroDivisionError):
            value = Fraction(24, 1)
    return value if value > 0 else Fraction(24, 1)


def _timestamp_to_milliseconds(value: str) -> int | None:
    """Parse SRT/VTT timestamps, accepting both MM:SS and HH:MM:SS."""
    normalized = value.strip().replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes_text, seconds_text = parts
    elif len(parts) == 3:
        hours_text, minutes_text, seconds_text = parts
        try:
            hours = int(hours_text)
        except ValueError:
            return None
    else:
        return None
    try:
        minutes = int(minutes_text)
        seconds = float(seconds_text)
    except ValueError:
        return None
    if hours < 0 or minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
        return None
    return int(round((hours * 3600 + minutes * 60 + seconds) * 1000))


def _milliseconds_to_frame(milliseconds: int, fps: Any) -> int:
    rate = _fps_fraction(fps)
    numerator = max(0, int(milliseconds)) * rate.numerator
    denominator = 1000 * rate.denominator
    return (numerator + denominator // 2) // denominator


def _cue_from_timestamps(start_ms: int | None, end_ms: int | None,
                         text: str, fps: Any) -> ParsedSubtitleCue | None:
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        return None
    start_frame = _milliseconds_to_frame(start_ms, fps)
    end_frame = max(start_frame + 1, _milliseconds_to_frame(end_ms, fps))
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized_text:
        return None
    return ParsedSubtitleCue(start_frame, end_frame - start_frame, normalized_text)


def _parse_srt_or_vtt(content: str, fps: Any) -> list[ParsedSubtitleCue]:
    cues: list[ParsedSubtitleCue] = []
    normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n\s*\n", normalized_content):
        lines = block.split("\n")
        timing_index = next(
            (index for index, line in enumerate(lines) if _TIMING_LINE.match(line)),
            None,
        )
        if timing_index is None:
            continue
        match = _TIMING_LINE.match(lines[timing_index])
        if match is None:
            continue
        cue = _cue_from_timestamps(
            _timestamp_to_milliseconds(match.group("start")),
            _timestamp_to_milliseconds(match.group("end")),
            "\n".join(lines[timing_index + 1:]),
            fps,
        )
        if cue is not None:
            cues.append(cue)
    return cues


def _ass_timestamp_to_milliseconds(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2})\.(\d{1,2})\s*", value)
    if match is None:
        return None
    hours, minutes, seconds, centiseconds = match.groups()
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(centiseconds.ljust(2, "0")) * 10
    )


def _ass_events_column_count(content_lines: list[str]) -> int:
    """D08（第六轮审计）：按 [Events] Format 行确定 Dialogue 列数。

    标准 ASS Format 共 10 列（Layer..Text，Text 前恰 9 个逗号）；SSA 带
    Marked 共 11 列。缺 Format 行时回退嗅探首个 Dialogue：第二列能解析
    为时间戳 → 10 列，否则视为 SSA 的 11 列。
    """
    format_columns: int | None = None
    first_dialogue: str | None = None
    in_events = False
    for line in content_lines:
        stripped = line.strip()
        low = stripped.lower()
        if stripped.startswith("["):
            in_events = low.startswith("[events]")
            continue
        if not in_events:
            continue
        if low.startswith("format:"):
            names = [n.strip().lower() for n in stripped.split(":", 1)[1].split(",")]
            names = [n for n in names if n]
            if names:
                format_columns = len(names)
        elif low.startswith("dialogue:") and first_dialogue is None:
            first_dialogue = stripped
    if format_columns and format_columns >= 4:
        return format_columns
    if first_dialogue is not None:
        head = first_dialogue.split(":", 1)[1].lstrip().split(",", 3)
        if len(head) >= 3 and _ass_timestamp_to_milliseconds(head[1]) is not None:
            return 10  # ASS：Layer, Start, ...
        return 11  # SSA：Marked=0, Start, ...
    return 10


def _parse_ass_or_ssa(content: str, fps: Any) -> list[ParsedSubtitleCue]:
    cues: list[ParsedSubtitleCue] = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    # D08（第六轮审计）：Text 本身允许 ASCII 逗号。旧代码固定 split(",", 10)
    # 比 10 列布局多切一刀，"Hello, world" 的 "Hello" 被当第 10 列丢弃。
    # 按实际列数 maxsplit=columns-1，Text 一列完整保留全部逗号/标签/换行。
    column_count = _ass_events_column_count(lines)
    for line in lines:
        if not line.lstrip().lower().startswith("dialogue:"):
            continue
        fields = line.split(":", 1)[1].lstrip().split(",", column_count - 1)
        if len(fields) < 4:
            continue
        # ASS: Layer, Start, End, ..., Text. SSA adds Marked=0 before Start.
        time_indexes = [
            index for index, field in enumerate(fields[:-1])
            if _ass_timestamp_to_milliseconds(field) is not None
        ]
        if len(time_indexes) < 2:
            continue
        start_ms = _ass_timestamp_to_milliseconds(fields[time_indexes[0]])
        end_ms = _ass_timestamp_to_milliseconds(fields[time_indexes[1]])
        text = _ASS_TAGS.sub("", fields[-1])
        text = text.replace(r"\N", "\n").replace(r"\n", "\n")
        cue = _cue_from_timestamps(start_ms, end_ms, text, fps)
        if cue is not None:
            cues.append(cue)
    return cues


_TTML_CLOCK = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d{2}):"
    r"(?P<seconds>\d{2})(?::(?P<frames>\d+))?"
    r"(?:\.(?P<fraction>\d+))?$"
)
_TTML_OFFSET = re.compile(r"^(?P<value>[0-9]+(?:\.[0-9]+)?)(?P<unit>h|m|s|ms|f|t)$")


def _parse_positive_rate(name: str, raw: str) -> float:
    """R13（第七轮审计）：时基参数必须是正有限数，否则明确拒绝。

    旧实现把 0/负值夹到 1e-9——``tickRate="0"`` 会让 ``1t`` 变成
    10 亿秒（24 万亿帧起点），静默产出垃圾字幕。
    """
    import math

    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid TTML {name}={raw!r}: not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Invalid TTML {name}={raw!r}: must be positive")
    return value


def _ttml_time_context(root: ET.Element, fps: Any) -> dict:
    """D09/R12/R13：读取文件声明的时间基准参数。

    - ``ttp:tickRate``：ticks/秒。缺省推导（R12，按 W3C TTML1）：
      声明了 frameRate 时 = effectiveFrameRate × subFrameRate（含
      subFrameRateMultiplier），否则 1（1 tick = 1 秒）。
    - ``ttp:frameRate``（× ``ttp:frameRateMultiplier``，如 ``1000 1001``）：
      'f' 单位与帧式时钟（``HH:MM:SS:FF``）的帧率；缺省沿用目标时间线
      fps（保持既有行为）。
    - ``ttp:subFrameRate``（× ``subFrameRateMultiplier``）：缺省 1。
    - ``ttp:timeBase``：仅支持 ``media``；``smpte``/``clock`` 显式拒绝
      （映射语义未定义，不能静默按 media 解析）。
    - 非正/非有限/非数值的时基声明明确抛错（R13），不夹值不忽略。
    """
    declared: dict[str, str] = {}
    for key, value in root.attrib.items():
        name = _xml_local_name(key)
        if name in {
            "tickrate", "framerate", "frameratemultiplier",
            "subframerate", "subframeratemultiplier", "timebase",
        }:
            declared[name] = str(value).strip()

    time_base = declared.get("timebase", "media").lower()
    if time_base not in {"", "media"}:
        raise ValueError(
            f"Unsupported TTML timeBase '{time_base}': only media time is supported"
        )

    frame_rate: float | None = None
    if "framerate" in declared:
        frame_rate = _parse_positive_rate(
            "ttp:frameRate", declared["framerate"])
        multiplier = declared.get("frameratemultiplier", "")
        parts = multiplier.replace(",", " ").split()
        if parts:
            if len(parts) != 2:
                raise ValueError(
                    f"Invalid TTML ttp:frameRateMultiplier={multiplier!r}")
            num = _parse_positive_rate("ttp:frameRateMultiplier", parts[0])
            den = _parse_positive_rate("ttp:frameRateMultiplier", parts[1])
            frame_rate = frame_rate * num / den

    sub_frame_rate = 1.0
    if "subframerate" in declared:
        sub_frame_rate = _parse_positive_rate(
            "ttp:subFrameRate", declared["subframerate"])
        multiplier = declared.get("subframeratemultiplier", "")
        parts = multiplier.replace(",", " ").split()
        if parts:
            if len(parts) != 2:
                raise ValueError(
                    f"Invalid TTML ttp:subFrameRateMultiplier={multiplier!r}")
            num = _parse_positive_rate(
                "ttp:subFrameRateMultiplier", parts[0])
            den = _parse_positive_rate(
                "ttp:subFrameRateMultiplier", parts[1])
            sub_frame_rate = sub_frame_rate * num / den

    if "tickrate" in declared:
        tick_rate = _parse_positive_rate(
            "ttp:tickRate", declared["tickrate"])
    elif frame_rate is not None:
        # R12：缺省 tickRate = effectiveFrameRate × subFrameRate
        tick_rate = frame_rate * sub_frame_rate
    else:
        tick_rate = 1.0

    return {
        "tick_rate": tick_rate,
        "frame_rate": frame_rate,  # None → 用目标时间线 fps
    }


def _ttml_timestamp_to_milliseconds(
    value: str, fps: Any, context: dict | None = None
) -> int | None:
    """Parse common TTML clock-time and offset-time expressions.

    D09：``f``/``t`` 单位按文件声明的 frameRate/tickRate 换算（此前忽略
    声明值，24fps 序列下 ``10t`` 被当成 10ms → 0 帧）。
    """
    normalized = str(value or "").strip()
    clock = _TTML_CLOCK.fullmatch(normalized)
    if clock is not None:
        hours = int(clock.group("hours") or 0)
        minutes = int(clock.group("minutes"))
        seconds = int(clock.group("seconds"))
        frames = clock.group("frames")
        fraction = clock.group("fraction")
        if minutes >= 60 or seconds >= 60:
            return None
        base_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000
        extra_ms = 0
        if frames is not None:
            # R13：帧式时钟 HH:MM:SS:FF——按文件声明帧率（缺省时间线
            # fps）换算帧分量；不再静默当 0。
            frame_rate = (context or {}).get("frame_rate")
            if frame_rate is None:
                frame_rate = float(_fps_fraction(fps))
            extra_ms += int(frames) * 1000.0 / float(frame_rate)
        if fraction is not None:
            # 复查补齐：帧号与小数秒同时出现（非标准但可解析）时两者
            # 都计入——旧分支互斥会静默丢掉帧分量。
            extra_ms += int((fraction + "000")[:3])
        return int(round(base_ms + extra_ms))

    offset = _TTML_OFFSET.fullmatch(normalized)
    if offset is None:
        return None
    number = float(offset.group("value"))
    unit = offset.group("unit")
    context = context or {}
    if unit == "h":
        milliseconds = number * 3_600_000
    elif unit == "m":
        milliseconds = number * 60_000
    elif unit == "s":
        milliseconds = number * 1000
    elif unit == "ms":
        milliseconds = number
    elif unit == "f":
        frame_rate = context.get("frame_rate")
        if frame_rate is None:
            frame_rate = float(_fps_fraction(fps))
        milliseconds = number * 1000.0 / float(frame_rate)
    else:  # media ticks
        tick_rate = float(context.get("tick_rate") or 1.0)
        milliseconds = number * 1000.0 / tick_rate
    return int(round(milliseconds)) if milliseconds >= 0 else None


def _xml_local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _ttml_text(element: ET.Element) -> str:
    """Collect TTML paragraph text while turning ``<br/>`` into newlines."""
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        if _xml_local_name(child.tag) == "br":
            parts.append("\n")
        else:
            parts.append(_ttml_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _parse_ttml(content: str, fps: Any) -> list[ParsedSubtitleCue]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid TTML/XML subtitle file: {exc}") from exc
    if _xml_local_name(root.tag) not in {"tt", "ttml"}:
        raise ValueError("XML subtitle file does not contain a TTML root")

    cues: list[ParsedSubtitleCue] = []

    # D09：先读文件时间基准（tickRate/frameRate/multiplier/timeBase），
    # 再映射目标时间线。
    time_context = _ttml_time_context(root, fps)

    def visit(
        element: ET.Element,
        parent_begin_ms: int = 0,
        parent_end_ms: int | None = None,
    ) -> None:
        begin_ms = _ttml_timestamp_to_milliseconds(
            element.get("begin", ""), fps, time_context)
        begin_ms = parent_begin_ms + (begin_ms or 0)
        end_ms = _ttml_timestamp_to_milliseconds(
            element.get("end", ""), fps, time_context)
        dur_ms = _ttml_timestamp_to_milliseconds(
            element.get("dur", ""), fps, time_context)
        if end_ms is not None:
            end_ms += parent_begin_ms
        elif dur_ms is not None:
            end_ms = begin_ms + dur_ms

        # R13（第七轮审计）：子元素活动区间被父元素（div/body）的
        # [begin, end) 裁切——父 1–3s / 子相对 1–9s 时字幕只在 2–3s
        # 可见，不再越过父范围。
        if parent_end_ms is not None:
            if end_ms is None or end_ms > parent_end_ms:
                end_ms = parent_end_ms
            if begin_ms > parent_end_ms:
                begin_ms = parent_end_ms

        if _xml_local_name(element.tag) == "p":
            cue = _cue_from_timestamps(
                begin_ms,
                end_ms,
                _ttml_text(element),
                fps,
            )
            if cue is not None:
                cues.append(cue)
            return
        for child in element:
            visit(child, begin_ms, end_ms)

    visit(root)
    if not cues:
        raise ValueError("TTML/XML subtitle file contains no timed paragraphs")
    return cues


def parse_subtitle_text(content: str, extension: str, fps: Any) -> list[ParsedSubtitleCue]:
    """Parse subtitle text according to its file extension."""
    normalized_extension = str(extension).lower()
    if not normalized_extension.startswith("."):
        normalized_extension = "." + normalized_extension
    if normalized_extension in (".srt", ".vtt"):
        cues = _parse_srt_or_vtt(content, fps)
    elif normalized_extension in (".ass", ".ssa"):
        cues = _parse_ass_or_ssa(content, fps)
    elif normalized_extension in (".ttml", ".dfxp", ".xml"):
        cues = _parse_ttml(content, fps)
    else:
        raise ValueError(f"Unsupported subtitle extension: {extension!r}")
    return sorted(cues, key=lambda cue: (cue.start_frame, cue.text))


def parse_subtitle_file(path: str | Path, fps: Any) -> list[ParsedSubtitleCue]:
    """Read and parse a supported subtitle file."""
    source = Path(path)
    extension = source.suffix.lower()
    if extension not in SUPPORTED_SUBTITLE_EXTENSIONS:
        raise ValueError(f"Unsupported subtitle extension: {extension!r}")
    content = _read_subtitle_content(source)
    return parse_subtitle_text(content, extension, fps)


def _read_subtitle_content(source: Path) -> str:
    content = None
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030"):
        try:
            content = source.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        raise UnicodeError(f"Unable to decode subtitle file: {source}")
    return content


def parse_subtitle_file_language(path: str | Path) -> str:
    """Read an optional TTML ``xml:lang``/``lang`` tag from a subtitle file."""
    source = Path(path)
    if source.suffix.lower() not in {".ttml", ".dfxp", ".xml"}:
        return ""
    content = _read_subtitle_content(source)
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return ""
    return str(
        root.get("{http://www.w3.org/XML/1998/namespace}lang")
        or root.get("lang") or ""
    ).strip()


def partition_subtitle_cues(
    cues: list[ParsedSubtitleCue],
) -> list[list[ParsedSubtitleCue]]:
    """Split overlapping cues into the fewest non-overlapping tracks.

    The timeline model intentionally forbids overlaps inside one subtitle
    track, while real SRT/VTT/ASS files often contain simultaneous cues
    (bilingual lines, karaoke layers, or speaker labels).  Importing all of
    them into one track would make the overwrite command silently trim data.
    This stable first-fit partition preserves every original time range and
    uses additional tracks only when the source actually overlaps.
    """
    groups: list[list[ParsedSubtitleCue]] = []
    group_ends: list[int] = []
    for cue in sorted(cues, key=lambda item: (item.start_frame, item.text)):
        target = next(
            (index for index, end in enumerate(group_ends)
             if cue.start_frame >= end),
            None,
        )
        if target is None:
            target = len(groups)
            groups.append([])
            group_ends.append(0)
        groups[target].append(cue)
        group_ends[target] = cue.start_frame + cue.duration_frames
    return groups


__all__ = [
    "ParsedSubtitleCue",
    "SUPPORTED_SUBTITLE_EXTENSIONS",
    "parse_subtitle_file",
    "parse_subtitle_file_language",
    "parse_subtitle_text",
    "partition_subtitle_cues",
]
