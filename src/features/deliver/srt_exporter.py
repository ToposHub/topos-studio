"""
SRT 字幕导出器.

把时间线所有字幕轨的字幕片段导出为 SubRip (.srt) 文件格式。

SRT 格式约定
------------
- 序号从 1 开始
- 时间码：HH:MM:SS,mmm（毫秒，逗号分隔）
- 每个 cue 由序号 + 时间码行 + 文本（一行或多行）+ 空行组成

多个字幕轨的合并规则：
- 按轨道顺序，每轨内按 timeline_start 排序
- 全局再按 start 时间合并（同一时刻多个轨的字幕各占一行 cue）
- 禁用的字幕片段（is_enabled=False）跳过
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

from src.features.edit.core.subtitle_renderer import frames_to_srt_timecode

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import TimelineSequence, SubtitleClip

logger = logging.getLogger(__name__)


def collect_subtitle_clips(sequence: "TimelineSequence") -> List["SubtitleClip"]:
    """收集所有字幕轨上的可用字幕片段（按 timeline_start 排序）。

    跳过：
    - 禁用的轨道（is_enabled=False / is_visible=False）
    - 禁用的字幕片段（is_enabled=False）
    """
    result: list = []
    for track in sequence.subtitle_tracks:
        if not track.is_enabled or not track.is_visible:
            continue
        for clip in track.subtitle_clips:
            if not clip.is_enabled:
                continue
            if not (clip.text or "").strip():
                continue
            result.append(clip)
    result.sort(key=lambda c: c.timeline_start)
    return result


def export_srt(sequence: "TimelineSequence", output_path: str) -> bool:
    """把时间线所有字幕导出为 SRT 文件。

    Args:
        sequence: 时间线序列
        output_path: 输出 .srt 文件路径

    Returns:
        True 成功，False 失败（已记录日志）
    """
    try:
        clips = collect_subtitle_clips(sequence)
        # fps 取 sequence.fps（Rational）
        fps = 24.0
        seq_fps = getattr(sequence, 'fps', None)
        if seq_fps is not None:
            if hasattr(seq_fps, 'to_fps'):
                fps = float(seq_fps.to_fps())
            else:
                try:
                    fps = float(seq_fps)
                except (TypeError, ValueError):
                    logger.warning(
                        f"Cannot read sequence fps ({seq_fps!r}); "
                        f"SRT timecodes fallback to 24 fps")

        lines: list[str] = []
        for idx, clip in enumerate(clips, start=1):
            start_tc = frames_to_srt_timecode(clip.timeline_start, fps)
            end_tc = frames_to_srt_timecode(clip.timeline_end, fps)
            # SRT cue
            lines.append(str(idx))
            lines.append(f"{start_tc} --> {end_tc}")
            # 文本（可能多行）
            text = (clip.text or "").rstrip()
            lines.append(text)
            lines.append("")  # 空行分隔

        # 末尾换行
        content = "\n".join(lines)
        if content and not content.endswith("\n"):
            content += "\n"

        # UTF-8 with BOM（部分播放器需要 BOM 才能正确识别编码）
        with open(output_path, "w", encoding="utf-8-sig") as f:
            f.write(content)

        logger.info(f"Exported {len(clips)} subtitle clips to {output_path}")
        return True
    except Exception:
        logger.exception(f"SRT export failed: {output_path}")
        return False
