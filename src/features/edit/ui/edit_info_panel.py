"""Contextual, read-only inspector for the Edit page selection."""

from __future__ import annotations

from math import gcd
from typing import Any, Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.media.metadata import format_codec_label
from src.shared.ui.style_sheet import C


def _present(value: Any) -> bool:
    """Whether an optional metadata value is useful to show."""
    if value is None or value == "":
        return False
    if isinstance(value, str) and value.lower() in {"unknown", "n/a", "-"}:
        return False
    return not (isinstance(value, (list, tuple, dict)) and not value)


def _text(value: Any, fallback: str = "—") -> str:
    if not _present(value):
        return fallback
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return "—"


def _format_duration(seconds: Any) -> str:
    try:
        total = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(int(total), 3600)
    minutes, secs = divmod(remainder, 60)
    milliseconds = int(round((total - int(total)) * 1000.0))
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"
    return f"{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def _format_frame_time(frame: Any, fps: Any) -> str:
    try:
        frame_number = max(0, int(frame))
        frame_rate = float(fps)
        if frame_rate <= 0.0:
            raise ValueError
    except (TypeError, ValueError):
        return _text(frame)
    hours, remainder = divmod(frame_number, int(frame_rate * 3600.0))
    minutes, remainder = divmod(remainder, int(frame_rate * 60.0))
    seconds, frames = divmod(remainder, max(1, int(round(frame_rate))))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d} ({frame_number}f)"


class EditInfoPanel(QWidget):
    """Show only the useful information for the selected Edit object."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._content_layout: QVBoxLayout
        self._empty_label: QLabel
        self._value_labels: dict[str, QLabel] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ background: {C.BG_PANEL}; border: none; }}")

        content = QWidget()
        content.setStyleSheet(f"background: {C.BG_PANEL};")
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(12, 10, 12, 12)
        self._content_layout.setSpacing(10)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._add_empty_state()
        scroll.setWidget(content)
        layout.addWidget(scroll)

    def _add_empty_state(self) -> None:
        """Add the empty-state message after clearing the content layout."""
        self._empty_label = QLabel(I18N.tr(
            "edit.info.no_selection", "Select a clip or media asset to inspect its information."
        ))
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {C.TEXT_HINT}; font-size: 11px; padding: 20px;")
        self._content_layout.addWidget(self._empty_label)
        self._content_layout.addStretch(1)

    def clear(self) -> None:
        """Clear the inspector when the current selection is not media."""
        self._clear_content()
        self._add_empty_state()

    def set_clip(self, clip: Any, asset: Any | None, timeline_fps: float = 24.0) -> None:
        """Show the selected clip first, followed by useful source details."""
        self._clear_content()
        if asset is None:
            self._add_section(I18N.tr("edit.info.clip", "Clip"), [
                (I18N.tr("edit.info.source", "Source"), "Media unavailable"),
            ])
            return

        metadata = dict(getattr(asset, "metadata", {}) or {})
        source_fps = metadata.get("fps") or timeline_fps
        self._add_clip_section(
            clip, timeline_fps, source_fps, getattr(asset, "display_name", "")
        )
        self._add_asset_sections(asset, include_media_overview=False)

    def set_asset(self, asset: Any) -> None:
        """Show useful source metadata for a Media Pool selection."""
        self._clear_content()
        self._add_asset_sections(asset)

    def set_timeline(self, timeline: Any) -> None:
        """Show project-facing settings for the selected timeline."""
        self._clear_content()
        fps = self._timeline_fps(timeline)
        rows = [
            (I18N.tr("edit.info.name", "Name"), getattr(timeline, "name", "Timeline")),
            (I18N.tr("edit.info.resolution", "Resolution"), self._timeline_resolution(timeline)),
            (I18N.tr("edit.info.frame_rate", "Frame Rate"), self._format_fps(fps)),
            (I18N.tr("edit.info.sample_rate", "Sample Rate"),
             self._sample_rate(getattr(timeline, "sample_rate", None))),
        ]
        if hasattr(timeline, "duration_frames"):
            duration = timeline.duration_frames
            rows.append((
                I18N.tr("edit.info.duration", "Duration"),
                f"{_format_duration(self._frames_to_seconds(duration, fps))} ({duration} frames)",
            ))
        if any(hasattr(timeline, name) for name in ("video_tracks", "audio_tracks", "subtitle_tracks")):
            video_tracks = len(getattr(timeline, "video_tracks", ()) or ())
            audio_tracks = len(getattr(timeline, "audio_tracks", ()) or ())
            subtitle_tracks = len(getattr(timeline, "subtitle_tracks", ()) or ())
            track_summary = f"{video_tracks} video · {audio_tracks} audio"
            if subtitle_tracks:
                track_summary += f" · {subtitle_tracks} subtitles"
            rows.append((I18N.tr("edit.info.tracks", "Tracks"), track_summary))
        self._add_section(I18N.tr("edit.info.timeline", "Timeline"), rows)

    def _clear_content(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._value_labels.clear()

    def _add_clip_section(self, clip: Any, timeline_fps: float, source_fps: float,
                          source_name: str) -> None:
        speed = getattr(clip, "effective_speed", None)
        try:
            speed = speed() if callable(speed) else getattr(clip, "speed", 1.0)
            speed = getattr(speed, "speed", speed)
            speed_text = f"{float(speed):.2f}×"
        except (TypeError, ValueError):
            speed_text = "—"
        duration = getattr(clip, "timeline_duration", None)
        rows = [
            (I18N.tr("edit.info.name", "Name"), source_name),
            (I18N.tr("edit.info.timeline_in", "Timeline Start"),
             _format_frame_time(getattr(clip, "timeline_start", None), timeline_fps)),
            (I18N.tr("edit.info.timeline_duration", "Timeline Duration"),
             f"{_format_frame_time(duration, timeline_fps)} / {_format_duration(self._frames_to_seconds(duration, timeline_fps))}"),
            (I18N.tr("edit.info.source_in", "Source Start"),
             _format_frame_time(getattr(clip, "source_start", None), source_fps)),
            (I18N.tr("edit.info.source_duration", "Source Duration"),
             _format_frame_time(getattr(clip, "source_duration", None), source_fps)),
            (I18N.tr("edit.info.speed", "Speed"), speed_text),
        ]
        self._add_section(I18N.tr("edit.info.clip", "Clip"), rows)

    @staticmethod
    def _frames_to_seconds(frames: Any, fps: float) -> float | None:
        try:
            return int(frames) / float(fps) if float(fps) > 0.0 else None
        except (TypeError, ValueError):
            return None

    def _add_asset_sections(self, asset: Any, *, include_media_overview: bool = True) -> None:
        metadata = dict(getattr(asset, "metadata", {}) or {})
        width, height = metadata.get("width"), metadata.get("height")
        resolution = "—"
        aspect_ratio = "—"
        try:
            width, height = int(width), int(height)
            resolution = f"{width} × {height}"
            divisor = gcd(width, height)
            aspect_ratio = f"{width // divisor}:{height // divisor}"
        except (TypeError, ValueError, ZeroDivisionError):
            pass

        if include_media_overview:
            self._add_section(I18N.tr("edit.info.media", "Media"), [
                (I18N.tr("edit.info.name", "Name"), getattr(asset, "display_name", "—")),
                (I18N.tr("edit.info.type", "Type"), getattr(asset, "media_type", "—")),
            ])

        video_rows = [
            (I18N.tr("edit.info.resolution", "Resolution"), resolution),
            (I18N.tr("edit.info.aspect_ratio", "Aspect Ratio"), aspect_ratio),
            (I18N.tr("edit.info.frame_rate", "Frame Rate"), self._fps_text(metadata)),
            (I18N.tr("edit.info.duration", "Duration"), _format_duration(metadata.get("duration_seconds"))),
            (I18N.tr("edit.info.total_frames", "Total Frames"), metadata.get("total_frames")),
            (I18N.tr("edit.info.codec", "Codec"), format_codec_label(
                metadata.get("codec"), metadata.get("codec_profile")) or "—"),
            (I18N.tr("edit.info.pixel_format", "Pixel Format"), metadata.get("pixel_format")),
            (I18N.tr("edit.info.bit_depth", "Bit Depth"), self._bit_depth(metadata.get("bit_depth"))),
            (I18N.tr("edit.info.scan", "Scan"), metadata.get("field_order")),
            (I18N.tr("edit.info.timecode", "Timecode"), metadata.get("timecode")),
            (I18N.tr("edit.info.rotation", "Rotation"), self._degrees(metadata.get("rotation"))),
            (I18N.tr("edit.info.alpha", "Alpha"), True if metadata.get("has_alpha") else None),
        ]
        if getattr(asset, "media_type", "") in {"video", "image", "image_sequence"}:
            self._add_section(I18N.tr("edit.info.video", "Video"), video_rows)

        color_rows = [
            (I18N.tr("edit.info.hdr", "HDR"), True if metadata.get("is_hdr") else None),
            (I18N.tr("edit.info.primaries", "Primaries"), metadata.get("color_primaries")),
            (I18N.tr("edit.info.transfer", "Transfer"), metadata.get("color_transfer")),
            (I18N.tr("edit.info.matrix", "Matrix"), metadata.get("color_matrix")),
            (I18N.tr("edit.info.range", "Range"), metadata.get("color_range")),
        ]
        if any(_present(value) for _, value in color_rows):
            self._add_section(I18N.tr("edit.info.color", "Color"), color_rows)

        audio_info = metadata.get("audio_info")
        if isinstance(audio_info, dict) and audio_info:
            self._add_section(I18N.tr("edit.info.audio", "Audio"), [
                (I18N.tr("edit.info.codec", "Codec"), audio_info.get("codec")),
                (I18N.tr("edit.info.sample_rate", "Sample Rate"), self._sample_rate(audio_info.get("sample_rate"))),
                (I18N.tr("edit.info.channels", "Channels"), audio_info.get("channels")),
                (I18N.tr("edit.info.channel_layout", "Channel Layout"), audio_info.get("channel_layout")),
                (I18N.tr("edit.info.bit_depth", "Bit Depth"), self._bit_depth(audio_info.get("bit_depth"))),
                (I18N.tr("edit.info.duration", "Duration"), _format_duration(audio_info.get("duration_seconds"))),
            ])

        if include_media_overview:
            proxy_status = getattr(asset, "proxy_status", None)
            self._add_section(I18N.tr("edit.info.file", "File"), [
                (I18N.tr("edit.info.container", "Container"), metadata.get("format")),
                (I18N.tr("edit.info.file_size", "File Size"), _format_bytes(metadata.get("file_size"))),
                (I18N.tr("edit.info.proxy", "Proxy"),
                 proxy_status if proxy_status not in {None, "none"} else None),
                (I18N.tr("edit.info.offline", "Offline"),
                 True if getattr(asset, "is_offline", False) else None),
                (I18N.tr("edit.info.path", "Location"), getattr(asset, "path", "—")),
            ])

    @staticmethod
    def _fps_text(metadata: dict[str, Any]) -> str:
        fps = metadata.get("fps")
        if not _present(fps):
            return "—"
        try:
            fps_text = f"{float(fps):.3f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return _text(fps)
        numerator, denominator = metadata.get("fps_num"), metadata.get("fps_den")
        if _present(numerator) and _present(denominator):
            return f"{fps_text} ({numerator}/{denominator}) fps"
        return f"{fps_text} fps"

    @staticmethod
    def _bit_depth(value: Any) -> str:
        return f"{value}-bit" if _present(value) else "—"

    @staticmethod
    def _degrees(value: Any) -> str:
        if not _present(value):
            return "—"
        try:
            return f"{value}°" if float(value) != 0.0 else "—"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _sample_rate(value: Any) -> str:
        try:
            return f"{int(value):,} Hz"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _timeline_fps(timeline: Any) -> float:
        value = getattr(timeline, "fps", 24.0)
        if hasattr(value, "num") and hasattr(value, "den"):
            return float(value.num) / float(value.den or 1)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 24.0

    @staticmethod
    def _timeline_resolution(timeline: Any) -> str:
        width, height = getattr(timeline, "width", None), getattr(timeline, "height", None)
        return f"{width} × {height}" if _present(width) and _present(height) else "—"

    @staticmethod
    def _format_fps(value: float) -> str:
        return f"{value:.3f}".rstrip("0").rstrip(".") + " fps"

    def _add_section(self, title: str, rows: Iterable[tuple[str, Any]]) -> None:
        visible_rows = [(label, value) for label, value in rows if _present(value)]
        if not visible_rows:
            return

        section = QWidget()
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(2)

        header = QLabel(title)
        header.setStyleSheet(
            f"color: {C.TEXT_PRIMARY}; font-size: 11px; font-weight: bold; padding: 1px 0 3px 0;"
        )
        section_layout.addWidget(header)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"color: {C.BORDER_DARK}; background: {C.BORDER_DARK};")
        section_layout.addWidget(divider)

        for label_text, raw_value in visible_rows:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 2, 0, 2)
            row_layout.setSpacing(10)
            label = QLabel(label_text)
            label.setStyleSheet(f"color: {C.TEXT_HINT}; font-size: 10px;")
            label.setMinimumWidth(92)
            value = QLabel(_text(raw_value))
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setToolTip(_text(raw_value))
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setStyleSheet(f"color: {C.TEXT_PRIMARY}; font-size: 11px;")
            row_layout.addWidget(label)
            row_layout.addWidget(value, 1)
            section_layout.addWidget(row)
            self._value_labels[label_text] = value

        self._content_layout.addWidget(section)
