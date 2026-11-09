"""Geometric Edit-page instruments, independent of platform font glyphs.

These are local vector drawings: trim brackets, a razor blade, three source
edit diagrams and the three filmstrip zoom scopes. Colors come from the
existing timeline palette; no external images or Qt style theme are needed.
"""
from __future__ import annotations

from functools import lru_cache

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from src.shared.assets.icon_manager import IconColors

_PATHS: dict[str, str] = {
    "select": '<path d="m7 3 12 10-6 1 3 6-2.5 1-3-6-3.5 4z" fill="currentColor" stroke-width=".8"/>',
    "snap": '<path d="M5 3v10a7 7 0 0 0 14 0V3h-4v10a3 3 0 0 1-6 0V3z"/><path d="M5 7h4M15 7h4"/>',
    "link": '<path d="m10 7 2-2a5 5 0 0 1 7 7l-3 3a5 5 0 0 1-7 0M14 17l-2 2a5 5 0 0 1-7-7l3-3a5 5 0 0 1 7 0M8 16l8-8"/>',
    "unlink": '<path d="m12 5 1-1a5 5 0 0 1 7 7l-3 3M12 19l-1 1a5 5 0 0 1-7-7l3-3M3 3l18 18"/>',
    "position_lock": '<path d="M7 10V7a5 5 0 0 1 10 0v3"/><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M7 16h10m-8-2-2 2 2 2m6-4 2 2-2 2"/>',
    "voiceover": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 10v2a6 6 0 0 0 12 0v-2M12 18v3M8 21h8"/>',
    "flag": '<path d="M6 21V4h13l-3 4 3 4H6"/>',
    "trim": '<path d="M6 3H3v18h3M18 3h3v18h-3M12 4v16M9 8l-3 4 3 4M15 8l3 4-3 4"/>',
    "blade": '<path d="M3 6h18v4l-2 2 2 2v4H3v-4l2-2-2-2zM7 6v3M17 6v3M7 15v3M17 15v3M7 12h10M12 9v6"/>',
    "dynamic_trim": '<path d="M4 5v14M20 5v14M10 8l-4 4 4 4M14 8l4 4-4 4M6 12h12"/>',
    "insert": '<rect x="7" y="2" width="10" height="7" rx=".5"/><path d="M12 10v8m-3-3 3 3 3-3M2 14v7h8M22 14v7h-8M2 18h5M17 18h5"/>',
    "overwrite": '<rect x="7" y="2" width="10" height="7" rx=".5"/><path d="M12 10v6m-3-3 3 3 3-3"/><rect x="2" y="18" width="20" height="4" rx=".5"/>',
    "replace": '<rect x="3" y="8" width="6" height="9" rx=".5"/><rect x="15" y="8" width="6" height="9" rx=".5"/><path d="M6 4h12l-2-2m2 18H6l2 2M12 9v6"/>',
    "view_options": '<path d="M4 6h16M4 12h16M4 18h16"/><path d="M8 4v4M16 10v4M10 16v4" stroke-width="2.4"/>',
    "keyframes": '<path d="m3 12 5-7 5 7-5 7zM12 5l5 7-5 7M17 5l5 7-5 7"/>',
    "marker": '<path d="M7 4h10v10l-5 6-5-6z" fill="currentColor"/>',
    "zoom_full": '<rect x="3" y="4" width="14" height="11" rx=".5"/><path d="M6 4v11M14 4v5M3 8h3M3 11h3"/><circle cx="15" cy="15" r="4.5"/><path d="m18.5 18.5 3 3"/>',
    "zoom_detail": '<rect x="3" y="4" width="14" height="11" rx=".5"/><path d="M8 4v11M12 4v5"/><circle cx="15" cy="15" r="4.5"/><path d="m18.5 18.5 3 3M13 15h4M15 13v4"/>',
    "zoom_custom": '<path d="M3 8V4h4M13 4h4v4M3 11v4h4"/><circle cx="15" cy="15" r="4.5"/><path d="m18.5 18.5 3 3"/>',
    "zoom_out": '<path d="M6 12h12"/>',
    "zoom_in": '<path d="M6 12h12M12 6v12"/>',
    "mixer": '<path d="M6 4v16M12 4v16M18 4v16"/><path d="M4 9h4M10 15h4M16 7h4" stroke-width="3"/>',
    "more": '<g fill="currentColor" stroke="none"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></g>',
}


@lru_cache(maxsize=96)
def timeline_icon(name: str, color: str = IconColors.DEFAULT) -> QIcon:
    """Render a cached high-DPI toolbar icon from a small SVG drawing."""
    body = _PATHS[name]
    ink = QColor(color).name()
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" color="{ink}"><g fill="none" stroke="{ink}" stroke-width="1.35" stroke-linejoin="round" stroke-linecap="round">{body}</g></svg>'
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(48, 48)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        renderer.render(painter, QRectF(0.0, 0.0, 24.0, 24.0))
    finally:
        painter.end()
    return QIcon(pixmap)


def icon_trim_tool(color: str = IconColors.DEFAULT) -> QIcon:
    return timeline_icon("trim", color)


def icon_blade_tool(color: str = IconColors.DEFAULT) -> QIcon:
    return timeline_icon("blade", color)


def icon_insert_edit(color: str = IconColors.DEFAULT) -> QIcon:
    return timeline_icon("insert", color)


def icon_overwrite_edit(color: str = IconColors.DEFAULT) -> QIcon:
    return timeline_icon("overwrite", color)


def icon_marker(color: str = IconColors.DEFAULT) -> QIcon:
    return timeline_icon("marker", color)


def icon_select_tool(color: str = IconColors.DEFAULT) -> QIcon:
    return timeline_icon("select", color)


def icon_snap(color: str = IconColors.DEFAULT) -> QIcon:
    return timeline_icon("snap", color)


def icon_link(color: str = IconColors.DEFAULT) -> QIcon:
    return timeline_icon("link", color)


def icon_unlink(color: str = IconColors.DEFAULT) -> QIcon:
    return timeline_icon("unlink", color)
