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
    "trim": '<path d="M6 3H3v18h3M18 3h3v18h-3M12 4v16M9 8l-3 4 3 4M15 8l3 4-3 4"/>',
    "blade": '<path d="M3 6h18v4l-2 2 2 2v4H3v-4l2-2-2-2zM7 6v3M17 6v3M7 15v3M17 15v3M7 12h10M12 9v6"/>',
    "dynamic_trim": '<path d="M4 5v14M20 5v14M10 8l-4 4 4 4M14 8l4 4-4 4M6 12h12"/>',
    "insert": '<rect x="7" y="2" width="10" height="7" rx=".5"/><path d="M12 10v8m-3-3 3 3 3-3M2 14v7h8M22 14v7h-8M2 18h5M17 18h5"/>',
    "overwrite": '<rect x="7" y="2" width="10" height="7" rx=".5"/><path d="M12 10v6m-3-3 3 3 3-3"/><rect x="2" y="18" width="20" height="4" rx=".5"/>',
    "replace": '<rect x="2" y="7" width="7" height="10" rx=".5"/><rect x="15" y="7" width="7" height="10" rx=".5"/><path d="M6 4h12l-3-3m3 19H6l3 3M12 9v6"/>',
    "view_options": '<path d="M2 4h20M2 9h20M2 14h20M4 19l3 3 5-6M17 18h5"/><path d="M5 4v5M13 9v5" stroke-width="3"/>',
    "keyframes": '<path d="m4 12 6-7 6 7-6 7zM10 5l6-2 6 9-6 9-6-2"/>',
    "marker": '<path d="M7 3h10v10l-5 6-5-6z" fill="currentColor"/>',
    "zoom_full": '<rect x="2" y="3" width="15" height="12"/><path d="M5 3v12M14 3v12M2 7h3M2 11h3M14 7h3M14 11h3"/><circle cx="16" cy="15" r="5"/><path d="m20 19 3 3"/>',
    "zoom_detail": '<rect x="2" y="3" width="15" height="12"/><path d="M7 3v12M12 3v12"/><circle cx="16" cy="15" r="5"/><path d="m20 19 3 3M14 15h4M16 13v4"/>',
    "zoom_custom": '<path d="M2 7V3h4M13 3h4v4M2 11v4h4M13 15h4v-4"/><circle cx="16" cy="15" r="5"/><path d="m20 19 3 3"/>',
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
