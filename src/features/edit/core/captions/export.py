"""Export subtitle tracks to interchange caption formats.

The timeline stores caption timing as integer frames.  This module is the
format boundary: it converts those frames to the millisecond/centisecond
timebases used by SRT, WebVTT and ASS/SSA without importing any Qt UI code.
Keeping the conversion here makes the right-click export action easy to test
and keeps file dialogs out of the edit core.
"""

from __future__ import annotations

from fractions import Fraction
from html import escape
from pathlib import Path
from typing import Any, Iterable

from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip


SUPPORTED_FORMATS: tuple[str, ...] = (
    "srt", "vtt", "ass", "ttml", "dfxp", "scc",
)
FORMAT_EXTENSIONS: dict[str, str] = {
    "srt": ".srt",
    "vtt": ".vtt",
    "ass": ".ass",
    "ttml": ".ttml",
    "dfxp": ".dfxp",
    "scc": ".scc",
}


def _fps_fraction(fps: Any) -> Fraction:
    """Normalize a Rational, numeric FPS or FPS-like object to a Fraction."""
    if hasattr(fps, "num") and hasattr(fps, "den"):
        value = Fraction(int(fps.num), int(fps.den))
    else:
        try:
            value = Fraction(str(float(fps)))
        except (TypeError, ValueError, ZeroDivisionError):
            value = Fraction(24, 1)
    return value if value > 0 else Fraction(24, 1)


def _frame_to_milliseconds(frame: int, fps: Any) -> int:
    """Convert a frame index to milliseconds using half-up rounding."""
    rate = _fps_fraction(fps)
    numerator = max(0, int(frame)) * 1000 * rate.denominator
    denominator = rate.numerator
    return (numerator + denominator // 2) // denominator


def _frame_to_centiseconds(frame: int, fps: Any) -> int:
    """Convert a frame index to ASS centiseconds using half-up rounding."""
    rate = _fps_fraction(fps)
    numerator = max(0, int(frame)) * 100 * rate.denominator
    denominator = rate.numerator
    return (numerator + denominator // 2) // denominator


def _format_milliseconds(value: int, separator: str) -> str:
    """Format milliseconds as ``HH:MM:SS<separator>mmm``."""
    total_seconds, milliseconds = divmod(max(0, value), 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{milliseconds:03d}"


def _format_ass_time(frame: int, fps: Any) -> str:
    """Format a frame as the H:MM:SS.cc time used by ASS/SSA."""
    total_centiseconds = _frame_to_centiseconds(frame, fps)
    total_seconds, centiseconds = divmod(total_centiseconds, 100)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _cue_text(text: str) -> str:
    """Normalize line endings while preserving intentional line breaks."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _sorted_cues(track: EditTrack) -> list[SubtitleClip]:
    """Return a deterministic cue order, independent of tuple ordering."""
    return sorted(track.subtitle_clips, key=lambda cue: (cue.timeline_start, cue.clip_id))


def _render_srt(cues: Iterable[SubtitleClip], fps: Any) -> str:
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        start = _format_milliseconds(_frame_to_milliseconds(cue.timeline_start, fps), ",")
        end = _format_milliseconds(_frame_to_milliseconds(cue.timeline_end, fps), ",")
        blocks.append(f"{index}\n{start} --> {end}\n{_cue_text(cue.text)}")
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def _render_vtt(cues: Iterable[SubtitleClip], fps: Any) -> str:
    blocks: list[str] = ["WEBVTT", ""]
    for index, cue in enumerate(cues, start=1):
        start = _format_milliseconds(_frame_to_milliseconds(cue.timeline_start, fps), ".")
        end = _format_milliseconds(_frame_to_milliseconds(cue.timeline_end, fps), ".")
        blocks.extend((str(index), f"{start} --> {end}", _cue_text(cue.text), ""))
    return "\n".join(blocks) if len(blocks) > 2 else "WEBVTT\n"


def _ass_text(text: str) -> str:
    """Convert plain caption text to ASS line-break syntax safely."""
    return (_cue_text(text)
            .replace("{", r"\{")
            .replace("}", r"\}")
            .replace("\n", r"\N"))


def _render_ass(cues: Iterable[SubtitleClip], fps: Any) -> str:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,2,20,20,40,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        start = _format_ass_time(cue.timeline_start, fps)
        end = _format_ass_time(cue.timeline_end, fps)
        speaker = getattr(cue.caption_metadata, "speaker", "") or ""
        speaker = str(speaker).replace(",", " ").strip()
        text = _ass_text(cue.text)
        metadata = getattr(cue, "caption_metadata", None)
        word_timings = getattr(metadata, "word_timings", ())
        animation = getattr(metadata, "word_animation", "none")
        if word_timings and animation in {"highlight", "karaoke"}:
            # ASS \k durations are centiseconds.  The cue text remains
            # editable; the timing tags are only added at the delivery edge.
            karaoke_parts: list[str] = []
            for word in word_timings:
                duration = max(1, _frame_to_centiseconds(
                    word.end_frame - word.start_frame, fps
                ))
                karaoke_parts.append(f"{{\\k{duration}}}{_ass_text(word.text)}")
            if karaoke_parts:
                text = "".join(karaoke_parts)
        lines.append(
            f"Dialogue: 0,{start},{end},Default,{speaker},0,0,0,,{text}"
        )
    return "\n".join(lines) + "\n"


def _odd_parity(value: int) -> int:
    """Set the high bit so the 7-bit CEA-608 byte has odd parity."""
    value &= 0x7F
    return value | (0x80 if value.bit_count() % 2 == 0 else 0)


def _cea608_hex(text: str) -> str:
    """Encode printable ASCII as basic CEA-608 pop-on caption pairs.

    SCC is intentionally a conservative sidecar export: printable text and
    timing are preserved, while styling/positioning remains the responsibility
    of the broadcast caption encoder. Non-ASCII glyphs are replaced with ``?``
    because CEA-608 itself is not a Unicode transport.
    """
    clean = "".join(char if 0x20 <= ord(char) < 0x7F else "?" for char in text)
    values = [_odd_parity(ord(char)) for char in clean]
    if len(values) % 2:
        values.append(_odd_parity(0x20))
    return " ".join(
        f"{(values[index] << 8) | values[index + 1]:04X}"
        for index in range(0, len(values), 2)
    )


def _render_scc(cues: Iterable[SubtitleClip], fps: Any) -> str:
    lines = ["Scenarist_SCC V1.0"]
    for cue in cues:
        start = _format_scc_time(cue.timeline_start, fps)
        end = _format_scc_time(cue.timeline_end, fps)
        # AUD-15808: CEA-608 pop-on 时序。0x9420 = Resume Caption Loading
        # （开始写非显示缓冲），0x942F = End Of Caption（把缓冲上屏显示），
        # 0x942C = Erase Displayed Memory（清屏）。旧实现把 942f 误解为
        # "结束显示"放到 cue 结束时刻——字幕直到 cue 结束才上屏，且从不清
        # 屏导致后续 cue 逐条延迟并相互叠压。正确时序：cue 开始处
        # 9420 + 文本 + 942f（写完即上屏），cue 结束处 942c 清屏。
        payload = _cea608_hex(_cue_text(cue.text).replace("\n", " "))
        pop_on = " ".join(part for part in ("9420", payload, "942f") if part)
        lines.append(f"{start}\t{pop_on}")
        lines.append(f"{end}\t942c")
    return "\n".join(lines) + "\n"


def _format_scc_time(frame: int, fps: Any) -> str:
    rate = _fps_fraction(fps)
    absolute = max(0, int(frame))
    nominal = max(1, round(float(rate)))
    # SCC commonly uses 29.97 drop-frame timecode. Preserve the exact
    # broadcast label instead of treating 29.97 as a 30 fps integer clock.
    if abs(float(rate) - (30000.0 / 1001.0)) < 0.01:
        # AUD-15809: SMPTE ST 12-1 换算。规则：除整十分钟（00/10/20/...）
        # 外，每分钟开头跳过帧号 00 与 01（跳帧只体现在显示编号中）。
        # 因此一个十分钟块 = 1800 + 9*1798 = 17982 个实际帧；非整十分钟
        # 内显示帧号 = 分钟内实际偏移 + 2。旧实现把分钟边界当 1798 又对
        # 帧内偏移额外 +2，双重补偿：时间码恒偏约 2 帧，且十分钟块末尾
        # 2 帧被误标入下一分钟（17980//1798 == 10）。
        full_minute = nominal * 60
        drop = full_minute - int(round(float(rate) * 60))  # 29.97 → 2
        dropped_minute = full_minute - drop
        per_ten_minutes = full_minute + 9 * dropped_minute
        ten_count, rem = divmod(absolute, per_ten_minutes)
        if rem < full_minute:
            minute_in_block = 0
            display_in_minute = rem
        else:
            past = rem - full_minute
            minute_in_block = 1 + past // dropped_minute
            display_in_minute = past % dropped_minute + drop
        minutes_total = ten_count * 10 + minute_in_block
        hours, minutes = divmod(minutes_total, 60)
        seconds, frames = divmod(display_in_minute, nominal)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"
    total_seconds, frame_number = divmod(absolute, nominal)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frame_number:02d}"


def _format_ttml_time(frame: int, fps: Any) -> str:
    """Format a frame as a TTML clock time with millisecond precision."""
    milliseconds = _frame_to_milliseconds(frame, fps)
    total_seconds, millis = divmod(max(0, milliseconds), 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _ttml_text(text: str) -> str:
    """Escape cue text and preserve line breaks as TTML ``br`` elements."""
    lines = _cue_text(text).split("\n")
    return "<br/>".join(escape(line) for line in lines)


def _render_ttml(track: EditTrack, cues: Iterable[SubtitleClip], fps: Any) -> str:
    metadata = track.caption_metadata
    language = escape(
        str(getattr(metadata, "language", "") or "und"), quote=True
    )
    paragraphs: list[str] = []
    for cue in cues:
        begin = _format_ttml_time(cue.timeline_start, fps)
        end = _format_ttml_time(cue.timeline_end, fps)
        paragraphs.append(
            f'    <p begin="{begin}" end="{end}">'
            f"{_ttml_text(cue.text)}</p>"
        )
    body = "\n".join(paragraphs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        f'xml:lang="{language}">\n'
        "  <body>\n    <div>\n"
        f"{body}\n"
        "    </div>\n  </body>\n</tt>\n"
    )


def render_subtitle_track(track: EditTrack, format_name: str, fps: Any) -> str:
    """Render one subtitle track as SRT, WebVTT or ASS/SSA text.

    Args:
        track: The subtitle ``EditTrack`` to export.
        format_name: One of ``srt``, ``vtt`` or ``ass`` (case-insensitive).
        fps: Sequence FPS, either ``Rational`` or a positive numeric value.

    Raises:
        ValueError: If the track type or requested format is unsupported.
    """
    if track.track_type != "subtitle":
        raise ValueError("Only subtitle tracks can be exported")
    normalized = str(format_name).lower().lstrip(".")
    if normalized not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported subtitle format: {format_name!r}")

    cues = _sorted_cues(track)
    if normalized == "srt":
        return _render_srt(cues, fps)
    if normalized == "vtt":
        return _render_vtt(cues, fps)
    if normalized == "ass":
        return _render_ass(cues, fps)
    if normalized == "scc":
        return _render_scc(cues, fps)
    return _render_ttml(track, cues, fps)


def write_subtitle_track(track: EditTrack, format_name: str, fps: Any,
                         path: str | Path) -> Path:
    """Render and write one subtitle track as UTF-8, returning the path."""
    destination = Path(path)
    destination.write_text(
        render_subtitle_track(track, format_name, fps),
        encoding="utf-8",
        newline="\n",
    )
    return destination


__all__ = [
    "FORMAT_EXTENSIONS",
    "SUPPORTED_FORMATS",
    "render_subtitle_track",
    "write_subtitle_track",
]
