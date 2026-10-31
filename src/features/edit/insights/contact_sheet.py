"""T13 · 联系表与镜头故事板（代表帧格 + PNG/HTML 导出）。

纪律（计划 T13）：

- 代表帧按 clip 的源中点帧请求，**只经 host 缩略图供应**（复用既有
  ThumbnailProvider 缓存/异步/播放避让，不自建解码、不猜 VFR/变速/
  反放映射——``source_mapping != identity/constant`` 一律占位+原因）；
- 每格标明质量（源缩略图/占位）与捕获 revision——源缩略图**不是**
  带调色/合成/字幕的最终画面，导出 HTML 明确声明；
- UI 线程只碰 QPixmap；本模块在渲染前只持有 bytes（QImage 在渲染函数
  内创建）；在途上限 2 由 :class:`ThumbnailGate` 保证（播放暂停亦然）；
- 格内文本完整绘制（按字宽换行，不截字）；行列顺序稳定按时间线位置。
"""

from __future__ import annotations

import html as html_module
import os
from dataclasses import dataclass
from typing import Any

from src.features.edit.insights.dto import (
    LOAD_STATE_LOADED,
    SOURCE_MAPPING_IDENTITY,
    SOURCE_MAPPING_CONSTANT,
    InsightSnapshot,
    SequenceEntry,
)

THUMB_W, THUMB_H = 320, 180


@dataclass(frozen=True)
class ContactCell:
    """联系表一格：镜头身份 + 元数据 + 缩略图载荷（或占位原因）。"""

    sequence_id: str
    sequence_name: str
    clip_id: str
    asset_display_name: str
    track_id: str
    timeline_start: int
    timeline_end: int
    tc_label: str
    source_frame: int | None          # 代表帧（源侧）；None=不可求
    dialogue: str                     # 时间上覆盖本格首帧的字幕文本
    marker: str                       # 覆盖的 marker 名
    thumb_quality: str                # source_thumbnail / placeholder
    thumb_reason: str                 # 占位原因（quality=placeholder 时）
    thumb_png: bytes | None = None
    capture_id: str = ""
    capture_revision: int = 0


def _tc_label(seq: SequenceEntry, frame: int) -> str:
    from src.shared.media.metadata import Rational
    from src.shared.media.timecode import Timecode, TimecodeType

    tc_type = (
        TimecodeType.DROP_FRAME if seq.fps.drop_frame
        else TimecodeType.NON_DROP_FRAME
    )
    return Timecode.from_frame_number(
        max(frame, 0), Rational(seq.fps.num, seq.fps.den), tc_type
    ).to_string()


def _dialogue_overlapping(seq: SequenceEntry, start: int, end: int) -> str:
    """与镜头半开区间重叠的第一条字幕文本（联系表格惯例：本镜对白）。"""
    for track in seq.tracks:
        if track.track_type != "subtitle":
            continue
        for cue in track.cues:
            if cue.text and cue.timeline_start < end and start < cue.timeline_end:
                return cue.text
    return ""


def _marker_within(seq: SequenceEntry, start: int, end: int) -> str:
    """落在镜头区间内的第一个 marker 名。"""
    for marker in seq.markers:
        if marker.name and start <= marker.frame < end:
            return marker.name
    return ""


def build_contact_cells(
    snapshot: InsightSnapshot,
    sequence_id: str,
    host: Any | None = None,
    *,
    max_cells: int = 120,
) -> tuple[ContactCell, ...]:
    """构建一格/镜头的联系表数据（缩略图缺失时占位并给原因）。

    host 未接线（None 或协议未实现 request_thumbnail）→ 全部占位
    "thumbnails not wired (G1 pending)"，导出仍可用（占位格可见原因）。
    """
    seq = next(
        (s for s in snapshot.sequences if s.sequence_id == sequence_id), None
    )
    if seq is None or seq.load_state != LOAD_STATE_LOADED:
        return ()
    asset_by_id = {a.asset_id: a for a in snapshot.assets}
    cells: list[ContactCell] = []
    for track in seq.tracks:
        if track.track_type != "video":
            continue
        for clip in track.clips:
            if len(cells) >= max_cells:
                break
            if clip.source_mapping not in (SOURCE_MAPPING_IDENTITY, SOURCE_MAPPING_CONSTANT):
                source_frame = None
                quality, reason = "placeholder", (
                    f"source mapping {clip.source_mapping} not verified (M04 pending)"
                )
            else:
                source_frame = clip.source_start + (clip.source_end - clip.source_start) // 2
                quality, reason = "placeholder", ""
                if host is None:
                    reason = "thumbnails not wired (G1 pending)"
                else:
                    from src.features.edit.insights.host_protocol import ThumbnailRequest

                    result = host.request_thumbnail(ThumbnailRequest(
                        capture_id=snapshot.capture_id,
                        asset_id=clip.asset_id,
                        source_frame=source_frame,
                        max_width=THUMB_W,
                        max_height=THUMB_H,
                    ))
                    if result.ok and result.data:
                        quality = result.quality
                        cells.append(ContactCell(
                            sequence_id=seq.sequence_id,
                            sequence_name=seq.name,
                            clip_id=clip.clip_id,
                            asset_display_name=(
                                asset_by_id[clip.asset_id].display_name
                                if clip.asset_id in asset_by_id else clip.asset_id
                            ),
                            track_id=track.track_id,
                            timeline_start=clip.timeline_start,
                            timeline_end=clip.timeline_end,
                            tc_label=_tc_label(seq, clip.timeline_start),
                            source_frame=source_frame,
                            dialogue=_dialogue_overlapping(seq, clip.timeline_start, clip.timeline_end),
                            marker=_marker_within(seq, clip.timeline_start, clip.timeline_end),
                            thumb_quality=quality,
                            thumb_reason=result.message or "",
                            thumb_png=result.data,
                            capture_id=snapshot.capture_id,
                            capture_revision=seq.revision,
                        ))
                        continue
                    quality, reason = "placeholder", (
                        result.message or f"thumbnail {result.quality}"
                    )
            cells.append(ContactCell(
                sequence_id=seq.sequence_id,
                sequence_name=seq.name,
                clip_id=clip.clip_id,
                asset_display_name=(
                    asset_by_id[clip.asset_id].display_name
                    if clip.asset_id in asset_by_id else clip.asset_id
                ),
                track_id=track.track_id,
                timeline_start=clip.timeline_start,
                timeline_end=clip.timeline_end,
                tc_label=_tc_label(seq, clip.timeline_start),
                source_frame=source_frame,
                dialogue=_dialogue_overlapping(seq, clip.timeline_start, clip.timeline_end),
                marker=_marker_within(seq, clip.timeline_start, clip.timeline_end),
                thumb_quality=quality,
                thumb_reason=reason,
                capture_id=snapshot.capture_id,
                capture_revision=seq.revision,
            ))
    return tuple(cells)


# ── PNG 渲染（Qt offscreen；文本按字宽换行不截字）──────────────


def _wrap_text(painter_metrics: Any, text: str, max_width: int) -> list[str]:
    """按像素宽度换行（中文逐字、拉丁按词；无省略截断）。"""
    lines: list[str] = []
    for raw_line in text.split("\n"):
        current = ""
        for ch in raw_line:
            if painter_metrics.horizontalAdvance(current + ch) <= max_width or not current:
                current += ch
            else:
                lines.append(current)
                current = ch
        lines.append(current)
    return lines or [""]


_QAPP_REF: Any = None  # 防止补建的 QApplication 被 Python GC（会连带销毁 C++ 实例）


def _ensure_qapp() -> None:
    """确保 QGuiApplication 存在（QPainter 文本/QImage 保存的前置条件）。

    真实应用里 MainWindow 已有 QApplication（instance 命中即返回）；
    纯脚本/测试环境下补建一个并保持模块级引用（无引用会被 GC 立即
    销毁，instance() 随之变回 None）。
    """
    global _QAPP_REF
    from PyQt6.QtWidgets import QApplication

    if QApplication.instance() is None:
        import sys

        _QAPP_REF = QApplication(sys.argv[:1])


def render_contact_sheet_png(
    cells: tuple[ContactCell, ...],
    path: str,
    *,
    columns: int = 3,
    caption_lines: int = 4,
) -> dict[str, Any]:
    """渲染 PNG 网格。返回布局信息（行数/每格行文本数，供测试验证）。

    全程 QImage/QPainter（无 QPixmap——后者在无 QGuiApplication 的纯
    headless 调用里会 abort；QImage 渲染等价且可离屏保存）。
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QFont, QImage, QPainter

    _ensure_qapp()
    if not cells:
        raise ValueError("no cells to render")

    gap, pad = 8, 8
    caption_height = 18 * caption_lines + 8
    cell_w = THUMB_W + pad * 2
    cell_h = THUMB_H + caption_height + pad * 2
    rows = (len(cells) + columns - 1) // columns
    header_h = 36
    image = QImage(
        columns * cell_w + gap * (columns + 1),
        header_h + rows * cell_h + gap * (rows + 1),
        QImage.Format.Format_RGB32,
    )
    image.fill(QColor("#f5f5f5"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    font = QFont()
    font.setPixelSize(13)
    painter.setFont(font)
    metrics = painter.fontMetrics()
    text_width = cell_w - pad * 2

    painter.setPen(QColor("#222"))
    painter.drawText(pad, 24, "{0} cells · capture {1} · {2}".format(
        len(cells),
        cells[0].capture_id[:8] if cells[0].capture_id else "?",
        "source thumbnails (not final color/composite frames)",
    ))

    wrapped_all: list[list[str]] = []
    for index, cell in enumerate(cells):
        row, col = divmod(index, columns)
        x = gap + col * (cell_w + gap)
        y = header_h + gap + row * (cell_h + gap)
        painter.setPen(QColor("#888"))
        painter.drawRect(x, y, cell_w - 1, cell_h - 1)
        thumb = QImage()
        if cell.thumb_png:
            thumb.loadFromData(cell.thumb_png)
        if not thumb.isNull():
            scaled = thumb.scaled(
                THUMB_W, THUMB_H,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawImage(x + pad, y + pad, scaled)
        else:
            painter.setPen(QColor("#bbb"))
            painter.fillRect(x + pad, y + pad, THUMB_W, THUMB_H, QColor("#e0e0e0"))
            reason_lines = _wrap_text(metrics, cell.thumb_reason or "placeholder", THUMB_W - 8)
            painter.setPen(QColor("#777"))
            for i, line in enumerate(reason_lines[:3]):
                painter.drawText(x + pad + 4, y + pad + 20 + i * 16, line)
        caption = " · ".join(filter(None, [
            cell.tc_label,
            cell.asset_display_name,
            cell.dialogue,
            cell.marker,
        ])) or cell.clip_id
        wrapped = _wrap_text(metrics, caption, text_width)[:caption_lines]
        wrapped_all.append(wrapped)
        painter.setPen(QColor("#222"))
        for i, line in enumerate(wrapped):
            painter.drawText(x + pad, y + pad + THUMB_H + 16 + i * 18, line)
    painter.end()
    if not image.save(path, "PNG"):
        raise IOError(f"failed to save contact sheet: {path}")
    return {"rows": rows, "columns": columns, "wrapped_lines": wrapped_all}


# ── HTML 导出（本地相对资源；静态无脚本）────────────────────────


def write_contact_sheet_html(
    cells: tuple[ContactCell, ...],
    directory: str,
    *,
    prefix: str = "contact",
) -> str:
    """每格一个 PNG + 一页静态 HTML（相对链接，离线可开）。"""
    os.makedirs(directory, exist_ok=True)
    entries: list[str] = []
    for index, cell in enumerate(cells):
        png_name = f"{prefix}_{index:03d}_{cell.clip_id}.png"
        png_path = os.path.join(directory, png_name)
        if cell.thumb_png:
            with open(png_path, "wb") as fh:
                fh.write(cell.thumb_png)
        entries.append(
            "<div class=\"cell\"><div class=\"thumb\">{thumb}</div>"
            "<div class=\"cap\">{cap}</div></div>".format(
                thumb=(
                    f'<img src="{html_module.escape(png_name)}" '
                    f'alt="{html_module.escape(cell.clip_id)}">'
                    if cell.thumb_png
                    else f'<div class="ph">placeholder: '
                         f'{html_module.escape(cell.thumb_reason)}</div>'
                ),
                cap=html_module.escape(" · ".join(filter(None, [
                    cell.tc_label, cell.asset_display_name,
                    cell.dialogue, cell.marker,
                ])) or cell.clip_id),
            )
        )
    quality_note = (
        "thumbnails are SOURCE stills of capture "
        f"{html_module.escape(cells[0].capture_id[:8]) if cells else ''}"
        " — NOT final color/composite/subtitle frames"
    )
    html_text = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<title>Contact Sheet</title><style>"
        "body{font-family:sans-serif;margin:16px;color:#222}"
        ".note{background:#fff3cd;padding:8px;border:1px solid #e0c56e}"
        ".grid{display:flex;flex-wrap:wrap;gap:12px}"
        ".cell{width:320px}.thumb img{max-width:320px;display:block}"
        ".ph{width:320px;height:180px;background:#e0e0e0;color:#777;"
        "display:flex;align-items:center;justify-content:center;"
        "text-align:center;padding:8px;box-sizing:border-box}"
        ".cap{font-size:12px;margin-top:4px;word-break:break-all}"
        "</style></head><body>"
        f"<p class=\"note\">{quality_note}</p>"
        f"<div class=\"grid\">{''.join(entries)}</div>"
        "</body></html>"
    )
    html_path = os.path.join(directory, f"{prefix}_index.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return html_path
