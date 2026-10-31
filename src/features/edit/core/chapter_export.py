"""C5: 章节标记导出（YouTube 章节文本 / FFMetadata）。

纯函数（无 Qt 依赖），输入时间线章节标记（``TimelineMarker.kind ==
"chapter"``）与 fps，输出两种通用交付格式：

- **YouTube 章节文本**：``h:mm:ss 标题`` 逐行。YouTube 要求第一章节从
  0:00 开始且至少 3 条——缺 0:00 时自动补 ``0:00 Chapters``（可在调用方
  关闭）；章节数 1–2 条时仍原样输出（调用方自行决定是否使用），但会记
  logger.warning 提示整份章节会被 YouTube 忽略（AUD-16112）。
  时间码进位正确（≥1h 的 ``h:mm:ss``）。
- **FFMetadata**：ffmpeg 通用章节格式（``;FFMETADATA1`` 头 +
  ``[CHAPTER]`` 块，TIMEBASE=1/1000，START/END 以毫秒计——AUD-16111：
  实现与 format_ffmetadata 函数 docstring 一致，模块头旧文的帧号直存
  描述已订正），可直接供
  ``ffmpeg -i in.mp4 -i chapters.ffmeta -map_chapters 1 out.mp4`` 封装。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "chapter_markers",
    "format_timestamp_hms",
    "format_youtube_chapters",
    "format_ffmetadata",
]


def chapter_markers(markers: Sequence[Any]) -> list[Any]:
    """筛出章节标记并按帧号排序（时间线级；clip 级标记不参与章节）。"""
    chapters = [
        m for m in markers
        if getattr(m, "kind", "standard") == "chapter"
        and getattr(m, "clip_id", None) is None
    ]
    return sorted(chapters, key=lambda m: m.frame)


def format_timestamp_hms(frame: int, fps: float) -> str:
    """帧号 → ``h:mm:ss``（YouTube 章节时间码，秒向下取整）。"""
    if fps <= 0:
        fps = 24.0
    total_seconds = max(0, int(frame)) / float(fps)
    whole = int(total_seconds)
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    seconds = whole % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def format_youtube_chapters(
    markers: Sequence[Any],
    fps: float,
    *,
    auto_first: bool = True,
) -> str:
    """章节标记 → YouTube 章节文本。

    Args:
        markers: 全部标记（内部筛选 kind == chapter）。
        fps: 时间线帧率。
        auto_first: 章节未覆盖 0:00 时自动补一行 ``0:00 Chapters``
            （YouTube 要求第一章节从 0:00 开始）。

    Note:
        AUD-16112: YouTube 要求至少 3 条章节；1–2 条时本函数仍输出（交由
        调用方决定是否采用），但会记 logger.warning——静默产出一份会被
        YouTube 整份忽略的章节文本是排查陷阱。
    """
    chapters = chapter_markers(markers)
    if not chapters:
        return ""
    if len(chapters) < 3:
        logger.warning(
            "format_youtube_chapters: only %d chapter(s); YouTube requires "
            "at least 3 and will ignore the whole chapter list",
            len(chapters),
        )
    lines: list[str] = []
    # 首章若本就落在 0:00（格式化时间码为 0:00:00），不能再补
    # "0:00 Chapters"——两条 0:00 时间戳会让 YouTube 拒绝整份章节。
    # AUD-16110: 判定必须基于格式化后的时间码而非 int(fps) 截断的帧数：
    # NTSC 分数帧率（如 29.97）下 int(fps)=29，第 29 帧的时间码仍是
    # 0:00:00，帧数判定会误补出第二条 0:00。
    effective_fps = fps if fps > 0 else 24.0
    first_timecode = format_timestamp_hms(chapters[0].frame, effective_fps)
    if auto_first and first_timecode != "0:00:00":
        lines.append("0:00 Chapters")
    for marker in chapters:
        name = (marker.name or "").strip() or "Chapter"
        lines.append(f"{format_timestamp_hms(marker.frame, fps)} {name}")
    return "\n".join(lines) + "\n"


def format_ffmetadata(
    markers: Sequence[Any],
    fps: float,
    duration_frames: int = 0,
) -> str:
    """章节标记 → FFMetadata 文本。

    Args:
        markers: 全部标记（内部筛选 kind == chapter）。
        fps: 时间线帧率（TIMEBASE=1/1000，START/END 以毫秒计——ffmpeg
            接受任意有理时间基，用毫秒避免 NTSC 帧率取整造成的章节
            时间漂移）。
        duration_frames: 时间线总帧数；最后一章 END 取它（缺省则
            取下一章起点，单章无时长信息时 END=start+1）。
    """
    if fps <= 0:
        fps = 24.0
    chapters = chapter_markers(markers)
    if not chapters:
        return ""
    out = [";FFMETADATA1"]
    for i, marker in enumerate(chapters):
        start = int(marker.frame)
        if i + 1 < len(chapters):
            end = int(chapters[i + 1].frame)
        elif duration_frames and duration_frames > start:
            end = int(duration_frames)
        else:
            end = start + 1
        title = _ffmeta_escape(
            (marker.name or "").strip() or "Chapter")
        out.append("[CHAPTER]")
        out.append("TIMEBASE=1/1000")
        out.append(f"START={int(round(start * 1000.0 / fps))}")
        out.append(f"END={max(int(round(end * 1000.0 / fps)), int(round(start * 1000.0 / fps)) + 1)}")
        out.append(f"title={title}")
    return "\n".join(out) + "\n"


def _ffmeta_escape(text: str) -> str:
    """FFMetadata 语法保留字符转义（对齐 ffmpeg ffmetadata writer）。

    ``\\``、``=``、``;``、``#`` 及换行必须反斜杠转义，否则标题在
    "Part 1 = Intro"、"Scene #3"、"A;B" 这类名字下被截断/解析错乱。
    """
    text = text.replace("\\", "\\\\")
    text = text.replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return text
