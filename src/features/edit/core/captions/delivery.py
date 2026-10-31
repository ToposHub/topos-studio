"""P1-21 序列级字幕交付：一次导出全部语言轨。

轨道级右键导出（``export.write_subtitle_track``）一次只写一个文件；
交付场景（送审/播出/多语言发行）需要把序列的全部字幕轨按语言命名
批量落盘，并附带机器可读 manifest（文件↔语言↔角色↔cue 数）供下游
平台校验："目标平台导入后……语言轨正确"。

命名约定沿用广播/播放器生态的 sidecar 惯例：
``<时间线名>.<BCP-47 语言码><格式扩展名>``（如 ``Movie.zh-Hans.srt``），
播放器据此自动匹配语言轨；语言未标注的轨道退回 ``<时间线名>_<轨名>``。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.features.edit.core.captions.export import (
    FORMAT_EXTENSIONS,
    SUPPORTED_FORMATS,
    write_subtitle_track,
)

logger = logging.getLogger(__name__)

DELIVERY_MANIFEST_NAME = "subtitle_manifest.json"
DELIVERY_SCHEMA = "topos.subtitle_delivery.v1"

#: 交付文件状态
STATUS_WRITTEN = "written"
STATUS_SKIPPED_EMPTY = "skipped_empty"
STATUS_SKIPPED_ROLE = "skipped_role"
STATUS_FAILED = "failed"

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


@dataclass(frozen=True)
class SubtitleTrackSummary:
    """字幕轨交付摘要（语言/角色/cue 数）。"""

    track_id: str
    label: str
    language: str  # BCP-47；未标注为 "und"
    role: str      # primary / secondary / forced
    cue_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "track_id": self.track_id,
            "label": self.label,
            "language": self.language,
            "role": self.role,
            "cue_count": self.cue_count,
        }


@dataclass(frozen=True)
class SubtitleDeliveryFile:
    """单轨交付结果。"""

    summary: SubtitleTrackSummary
    file_name: str  # 相对输出目录；skipped/failed 为空
    format_name: str
    status: str
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.summary.to_dict(),
            "file_name": self.file_name,
            "format_name": self.format_name,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class SubtitleDeliveryReport:
    """序列级字幕交付报告。"""

    sequence_name: str = ""
    format_name: str = "srt"
    output_dir: str = ""
    files: Tuple[SubtitleDeliveryFile, ...] = ()
    manifest_path: str = ""

    @property
    def written(self) -> Tuple[SubtitleDeliveryFile, ...]:
        return tuple(f for f in self.files if f.status == STATUS_WRITTEN)

    @property
    def failed(self) -> Tuple[SubtitleDeliveryFile, ...]:
        return tuple(f for f in self.files if f.status == STATUS_FAILED)

    @property
    def passed(self) -> bool:
        return not self.failed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": DELIVERY_SCHEMA,
            "sequence_name": self.sequence_name,
            "format_name": self.format_name,
            "output_dir": self.output_dir,
            "manifest_path": self.manifest_path,
            "files": [f.to_dict() for f in self.files],
        }


def list_subtitle_tracks(sequence: Any) -> Tuple[SubtitleTrackSummary, ...]:
    """汇总序列全部字幕轨的交付元数据（保持轨道顺序）。"""
    summaries: List[SubtitleTrackSummary] = []
    for track in getattr(sequence, "subtitle_tracks", ()) or ():
        if getattr(track, "track_type", "") != "subtitle":
            continue
        metadata = getattr(track, "caption_metadata", None)
        language = str(getattr(metadata, "language", "") or "").strip() or "und"
        role = str(getattr(metadata, "role", "") or "primary").strip().lower()
        label = (
            str(getattr(metadata, "label", "") or "").strip()
            or str(getattr(track, "name", "") or "").strip()
            or str(getattr(track, "track_id", ""))
        )
        summaries.append(SubtitleTrackSummary(
            track_id=str(track.track_id),
            label=label,
            language=language,
            role=role,
            cue_count=len(getattr(track, "subtitle_clips", ()) or ()),
        ))
    return tuple(summaries)


def _sanitize(name: str, fallback: str) -> str:
    cleaned = _UNSAFE_FILENAME.sub("_", str(name or "")).strip("._")
    return cleaned[:100] or fallback


def delivery_file_name(
    summary: SubtitleTrackSummary,
    sequence_name: str,
    format_name: str,
) -> str:
    """按 sidecar 惯例生成分轨文件名（不含去重后缀）。

    有语言码 → ``<时间线>.<语言>.srt``（播放器按语言自动匹配）；
    未标注 → ``<时间线>_<轨名>.srt``。
    """
    base = _sanitize(sequence_name, fallback="timeline")
    ext = FORMAT_EXTENSIONS.get(str(format_name).lower(), ".srt")
    if summary.language and summary.language != "und":
        lang = _sanitize(summary.language, fallback="und")
        return f"{base}.{lang}{ext}"
    track = _sanitize(summary.label, fallback=summary.track_id[:8] or "track")
    return f"{base}_{track}{ext}"


def export_subtitle_tracks(
    sequence: Any,
    *,
    output_dir: str | Path,
    format_name: str = "srt",
    track_ids: Optional[Iterable[str]] = None,
    include_roles: Optional[Sequence[str]] = None,
    include_empty: bool = False,
    write_manifest: bool = True,
) -> SubtitleDeliveryReport:
    """批量导出序列字幕轨到 output_dir，返回交付报告。

    Args:
        sequence: TimelineSequence（fps 取 ``sequence.fps``）
        output_dir: 输出目录（自动创建）
        format_name: srt/vtt/ass/ttml/dfxp/scc
        track_ids: 只导出这些轨道（None = 全部字幕轨）
        include_roles: 只导出这些角色（小写；None = 不限）。广播惯例
            primary+forced 必交付，secondary（导演注释等）按需
        include_empty: 是否为 0 cue 轨道落盘空文件（默认跳过并记录）
        write_manifest: 同时写出 ``subtitle_manifest.json``

    Raises:
        ValueError: 格式不支持或序列没有字幕轨
    """
    normalized = str(format_name).lower().lstrip(".")
    if normalized not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported subtitle format: {format_name!r}")

    tracks = [
        t for t in (getattr(sequence, "subtitle_tracks", ()) or ())
        if getattr(t, "track_type", "") == "subtitle"
    ]
    if not tracks:
        raise ValueError("Sequence has no subtitle tracks")

    wanted = set(track_ids) if track_ids is not None else None
    roles = (
        {str(r).strip().lower() for r in include_roles}
        if include_roles is not None else None
    )
    fps = getattr(sequence, "fps", None)
    sequence_name = str(getattr(sequence, "name", "") or "timeline")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    summaries = list_subtitle_tracks(sequence)
    used_names: set = set()
    files: List[SubtitleDeliveryFile] = []

    for track, summary in zip(tracks, summaries):
        if wanted is not None and summary.track_id not in wanted:
            continue
        if roles is not None and summary.role not in roles:
            files.append(SubtitleDeliveryFile(
                summary=summary, file_name="", format_name=normalized,
                status=STATUS_SKIPPED_ROLE,
            ))
            continue
        if summary.cue_count == 0 and not include_empty:
            files.append(SubtitleDeliveryFile(
                summary=summary, file_name="", format_name=normalized,
                status=STATUS_SKIPPED_EMPTY,
            ))
            continue

        base_name = delivery_file_name(summary, sequence_name, normalized)
        name = base_name
        counter = 2
        while name in used_names:
            stem, ext = os.path.splitext(base_name)
            name = f"{stem}_{counter}{ext}"
            counter += 1
        used_names.add(name)
        dest = out / name
        try:
            write_subtitle_track(track, normalized, fps, dest)
            files.append(SubtitleDeliveryFile(
                summary=summary, file_name=name, format_name=normalized,
                status=STATUS_WRITTEN,
            ))
        except (OSError, ValueError) as exc:
            logger.warning("Subtitle delivery failed for %s: %s", name, exc)
            files.append(SubtitleDeliveryFile(
                summary=summary, file_name="", format_name=normalized,
                status=STATUS_FAILED, error=str(exc),
            ))

    report = SubtitleDeliveryReport(
        sequence_name=sequence_name,
        format_name=normalized,
        output_dir=str(out),
        files=tuple(files),
    )
    if write_manifest:
        manifest_path = out / DELIVERY_MANIFEST_NAME
        try:
            manifest_path.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            report.manifest_path = str(manifest_path)
        except OSError as exc:
            logger.warning("Subtitle manifest write failed: %s", exc)
    return report


__all__ = [
    "DELIVERY_MANIFEST_NAME",
    "SubtitleDeliveryReport",
    "SubtitleDeliveryFile",
    "SubtitleTrackSummary",
    "delivery_file_name",
    "export_subtitle_tracks",
    "list_subtitle_tracks",
]
