"""T11 · 视频镜头、对白与 marker 聚合总表（纯 DTO + 三格式一致导出）。

纪律（计划 T11）：

- 记录原始帧区间（半开）、sequence fps 有理数/DF 标志、显示 TC、source
  与 timeline 身份；复杂变速映射只展示已记录区间 + ``source_mapping``
  口径（unknown 不推导）；
- 文件名（file_basename）与显示名（display_name）分开；素材源路径默认
  不写入外发资料（DTO 本身只有 basename）；
- 多轨重叠各自独立成行；任何"节目时长"都必须来自覆盖并集或序列
  duration，禁止把各行时长相加当串行节目时长（T12 负责并集口径）；
- 三种输出（CSV/JSON/HTML）行数/ID/区间一致：同一 :class:`InsightsReport`
  序列化，不允许格式间重算；
- HTML 为静态转义文档（无脚本/宏）；CSV 用标准 csv 模块转义（表格软件
  安全），JSON 保留原始文本；
- 音频 cue 导出复用既有 ``interchange.CueSheet``，本模块不重写音频 cue
  算法（见 ``build_audio_cue_sheet`` 薄封装）。
"""

from __future__ import annotations

import csv
import html as html_module
import json
from dataclasses import dataclass
from typing import Any

from src.features.edit.insights.dto import (
    LOAD_STATE_LOADED,
    InsightSnapshot,
    SequenceEntry,
)

REPORT_SCHEMA = "insights-report/1"

_CSV_COLUMNS = (
    "section", "sequence_id", "sequence_name", "track_id", "track_name",
    "track_type", "entity_id", "label", "file", "text",
    "tc_in", "tc_out", "frame_start", "frame_end",
    "source_start", "source_end", "source_mapping", "is_enabled",
)


@dataclass(frozen=True)
class SequenceMetaRow:
    sequence_id: str
    name: str
    fps_num: int
    fps_den: int
    drop_frame: bool
    duration_frames: int | None
    load_state: str


@dataclass(frozen=True)
class ClipRow:
    """镜头/clip 行（视频与音频轨各 clip 独立成行；track_type 区分）。"""

    sequence_id: str
    sequence_name: str
    track_id: str
    track_name: str
    track_type: str          # video/audio
    clip_id: str
    asset_id: str
    asset_display_name: str
    file_basename: str
    timeline_start: int
    timeline_end: int
    source_start: int
    source_end: int
    source_mapping: str
    is_enabled: bool
    tc_in: str
    tc_out: str


@dataclass(frozen=True)
class DialogueRow:
    sequence_id: str
    sequence_name: str
    track_id: str
    track_name: str
    cue_id: str
    text: str
    timeline_start: int
    timeline_end: int
    is_enabled: bool
    tc_in: str
    tc_out: str


@dataclass(frozen=True)
class MarkerRow:
    sequence_id: str
    sequence_name: str
    marker_id: str
    name: str
    comment: str
    color: str
    kind: str
    frame: int
    tc: str


@dataclass(frozen=True)
class InsightsReport:
    schema: str
    project_id: str
    project_name: str
    sequences: tuple[SequenceMetaRow, ...]
    clips: tuple[ClipRow, ...]
    dialogues: tuple[DialogueRow, ...]
    markers: tuple[MarkerRow, ...]
    excluded_unloaded: tuple[str, ...]

    @property
    def total_rows(self) -> int:
        return len(self.clips) + len(self.dialogues) + len(self.markers)


def _tc(frame: int, seq: SequenceEntry) -> str:
    """显示时间码：复用既有 Timecode（Rational + DF/NDF），不另造公式。"""
    from src.shared.media.metadata import Rational
    from src.shared.media.timecode import Timecode, TimecodeType

    rational = Rational(seq.fps.num, seq.fps.den)
    tc_type = (
        TimecodeType.DROP_FRAME if seq.fps.drop_frame
        else TimecodeType.NON_DROP_FRAME
    )
    return Timecode.from_frame_number(max(frame, 0), rational, tc_type).to_string()


def _track_name(snapshot: InsightSnapshot, seq: SequenceEntry, track_id: str) -> str:
    for track in seq.tracks:
        if track.track_id == track_id:
            return track.name
    return ""


def build_report(snapshot: InsightSnapshot) -> InsightsReport:
    """从快照构建聚合总表（未加载序列只进 excluded_unloaded，不出行）。"""
    asset_by_id = {a.asset_id: a for a in snapshot.assets}
    meta_rows: list[SequenceMetaRow] = []
    clip_rows: list[ClipRow] = []
    dialogue_rows: list[DialogueRow] = []
    marker_rows: list[MarkerRow] = []
    excluded: list[str] = []

    for seq in snapshot.sequences:
        meta_rows.append(SequenceMetaRow(
            sequence_id=seq.sequence_id,
            name=seq.name,
            fps_num=seq.fps.num,
            fps_den=seq.fps.den,
            drop_frame=seq.fps.drop_frame,
            duration_frames=seq.duration_frames,
            load_state=seq.load_state,
        ))
        if seq.load_state != LOAD_STATE_LOADED:
            excluded.append(seq.sequence_id)
            continue
        for track in seq.tracks:
            for clip in track.clips:
                asset = asset_by_id.get(clip.asset_id)
                clip_rows.append(ClipRow(
                    sequence_id=seq.sequence_id,
                    sequence_name=seq.name,
                    track_id=track.track_id,
                    track_name=track.name,
                    track_type=track.track_type,
                    clip_id=clip.clip_id,
                    asset_id=clip.asset_id,
                    asset_display_name=asset.display_name if asset else clip.asset_id,
                    file_basename=asset.file_basename if asset else "",
                    timeline_start=clip.timeline_start,
                    timeline_end=clip.timeline_end,
                    source_start=clip.source_start,
                    source_end=clip.source_end,
                    source_mapping=clip.source_mapping,
                    is_enabled=clip.is_enabled,
                    tc_in=_tc(clip.timeline_start, seq),
                    tc_out=_tc(clip.timeline_end, seq),
                ))
            for cue in track.cues:
                dialogue_rows.append(DialogueRow(
                    sequence_id=seq.sequence_id,
                    sequence_name=seq.name,
                    track_id=track.track_id,
                    track_name=track.name,
                    cue_id=cue.clip_id,
                    text=cue.text,
                    timeline_start=cue.timeline_start,
                    timeline_end=cue.timeline_end,
                    is_enabled=cue.is_enabled,
                    tc_in=_tc(cue.timeline_start, seq),
                    tc_out=_tc(cue.timeline_end, seq),
                ))
        for marker in seq.markers:
            marker_rows.append(MarkerRow(
                sequence_id=seq.sequence_id,
                sequence_name=seq.name,
                marker_id=marker.marker_id,
                name=marker.name,
                comment=marker.comment,
                color=marker.color,
                kind=marker.kind,
                frame=marker.frame,
                tc=_tc(marker.frame, seq),
            ))

    return InsightsReport(
        schema=REPORT_SCHEMA,
        project_id=snapshot.project.project_id,
        project_name=snapshot.project.name,
        sequences=tuple(meta_rows),
        clips=tuple(clip_rows),
        dialogues=tuple(dialogue_rows),
        markers=tuple(marker_rows),
        excluded_unloaded=tuple(excluded),
    )


# ── JSON ─────────────────────────────────────────────────────────


def report_to_dict(report: InsightsReport) -> dict[str, Any]:
    return {
        "schema": report.schema,
        "project": {"project_id": report.project_id, "name": report.project_name},
        "sequences": [
            {
                "sequence_id": s.sequence_id, "name": s.name,
                "fps": {"num": s.fps_num, "den": s.fps_den, "drop_frame": s.drop_frame},
                "duration_frames": s.duration_frames, "load_state": s.load_state,
            }
            for s in report.sequences
        ],
        "clips": [row.__dict__ for row in report.clips],
        "dialogues": [row.__dict__ for row in report.dialogues],
        "markers": [row.__dict__ for row in report.markers],
        "excluded_unloaded": list(report.excluded_unloaded),
    }


def write_json(report: InsightsReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report_to_dict(report), fh, ensure_ascii=False, indent=1)


# ── CSV（标准 csv 转义；一个文件三段合一，section 列区分）───────


def _row_to_csv_values(section: str, row: Any) -> list[Any]:
    """列固定 18 列。dialogue 的 text 列带原始多行文本（csv 模块引号
    转义，表格软件安全）；clip 的显示名(label)与文件名(file)分列。"""
    if section == "clip":
        return [
            section, row.sequence_id, row.sequence_name, row.track_id,
            row.track_name, row.track_type, row.clip_id,
            row.asset_display_name, row.file_basename, "",
            row.tc_in, row.tc_out,
            row.timeline_start, row.timeline_end, row.source_start, row.source_end,
            row.source_mapping, row.is_enabled,
        ]
    if section == "dialogue":
        return [
            section, row.sequence_id, row.sequence_name, row.track_id,
            row.track_name, "subtitle", row.cue_id,
            "", "", row.text,
            row.tc_in, row.tc_out,
            row.timeline_start, row.timeline_end, "", "", "", row.is_enabled,
        ]
    return [  # marker
        section, row.sequence_id, row.sequence_name, "", "", "",
        row.marker_id, row.name, "", row.comment,
        row.tc, row.tc, row.frame, row.frame, "", "", "", True,
    ]


def write_csv(report: InsightsReport, path: str) -> None:
    """列固定：section + 16 列。dialogue 的 text 列带原始多行文本（csv
    模块引号转义，表格软件安全）；clip 的显示名与文件名分列。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_CSV_COLUMNS)
        for row in report.clips:
            writer.writerow(_row_to_csv_values("clip", row))
        for row in report.dialogues:
            writer.writerow(_row_to_csv_values("dialogue", row))
        for row in report.markers:
            writer.writerow(_row_to_csv_values("marker", row))


# ── HTML（静态、全转义、无脚本）──────────────────────────────────


def _esc(value: Any) -> str:
    return html_module.escape(str(value), quote=True)


def _html_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(report: InsightsReport) -> str:
    clip_headers = [
        "Sequence", "Track", "Type", "Clip ID", "Asset", "File",
        "TC In", "TC Out", "Timeline", "Source", "Mapping", "Enabled",
    ]
    clip_rows = [
        [
            row.sequence_name, row.track_name, row.track_type, row.clip_id,
            row.asset_display_name, row.file_basename,
            row.tc_in, row.tc_out,
            f"[{row.timeline_start},{row.timeline_end})",
            f"[{row.source_start},{row.source_end})",
            row.source_mapping, row.is_enabled,
        ]
        for row in report.clips
    ]
    dialogue_headers = ["Sequence", "Track", "Cue ID", "Text", "TC In", "TC Out", "Frames"]
    dialogue_rows = [
        [
            row.sequence_name, row.track_name, row.cue_id, row.text,
            row.tc_in, row.tc_out,
            f"[{row.timeline_start},{row.timeline_end})",
        ]
        for row in report.dialogues
    ]
    marker_headers = ["Sequence", "Marker", "Frame", "TC", "Kind", "Color", "Comment"]
    marker_rows = [
        [row.sequence_name, row.name, row.frame, row.tc, row.kind, row.color, row.comment]
        for row in report.markers
    ]
    seq_meta = "; ".join(
        f"{_esc(s.name)} {_esc(s.fps_num)}/{_esc(s.fps_den)}"
        f"{' DF' if s.drop_frame else ''}"
        f" dur={_esc(s.duration_frames if s.duration_frames is not None else '未收集')}"
        for s in report.sequences
    )
    parts = [
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">",
        "<title>Insights Report</title>",
        "<style>body{font-family:sans-serif;margin:16px;color:#222}"
        "table{border-collapse:collapse;margin:12px 0 24px}"
        "th,td{border:1px solid #bbb;padding:4px 8px;font-size:13px;text-align:left}"
        "th{background:#f0f0f0}h2{margin-top:24px}</style></head><body>",
        f"<h1>{_esc(report.project_name)} · Insights Report</h1>",
        f"<p>schema {_esc(report.schema)} · rows {_esc(report.total_rows)}"
        f" · unloaded excluded: {_esc(', '.join(report.excluded_unloaded) or '—')}</p>",
        f"<p>{seq_meta}</p>",
        "<h2>Clips / Shots</h2>", _html_table(clip_headers, clip_rows),
        "<h2>Dialogue / Subtitles</h2>", _html_table(dialogue_headers, dialogue_rows),
        "<h2>Markers</h2>", _html_table(marker_headers, marker_rows),
        "</body></html>",
    ]
    return "".join(parts)


def write_html(report: InsightsReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_html(report))


# 音频 cue 导出说明（非代码）：复用 ``interchange.CueSheet`` /
# ``build_cue_sheet_from_sequence``。CueSheet 构建需要宿主
# TimelineSequence 对象（快照 DTO 不持有模型），因此音频 cue 导出在
# T02 host adapter 层完成（拿宿主对象 → interchange → CSV），本包不
# 重写音频 cue 算法，也不在纯 DTO 层伪造等效实现。
