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

# Each edit diagram uses the same clip shape. The gap, solid destination
# strip and exchange arrows describe what happens at the edit point.
_LENS = (
    '<circle cx="15" cy="14.6" r="4.2" stroke-width="2"/>'
    '<path d="m18.1 17.7 3.4 3.4" stroke-width="2.6"/>'
)
_PATHS: dict[str, str] = {
    "envelope": (
        '<path d="M3 17h2c3 0 4-10 7-10s4 6 7 6h2" stroke-width="2"/>'
        '<path d="M3 14l3 3-3 3-3-3zm9-10 3 3-3 3-3-3zm9 6 3 3-3 3-3-3z" '
        'fill="currentColor" stroke="none"/>'
    ),
    "select": (
        '<path d="M5.4 2.2 20 13.4l-6.7.8 3.4 6-3.1 1.7-3.4-6.3-4.8 4z" '
        'fill="currentColor" stroke="none"/>'
    ),
    "trim": (
        '<path d="M7.4 4.8H2.6v14.4h4.8m9.2-14.4h4.8v14.4h-4.8M12 2.5v19"/>'
        '<path d="m5.6 8.4 4 3.6-4 3.6zm12.8 0-4 3.6 4 3.6z" fill="currentColor" stroke="none"/>'
    ),
    "blade": (
        '<g transform="rotate(-13 12 12)">'
        '<path fill="currentColor" stroke="none" fill-rule="evenodd" '
        'd="M1.5 5h21v4.6c-2.3 0-2.3 4.8 0 4.8V19h-21v-4.6c2.3 0 2.3-4.8 0-4.8z'
        'M5.5 11.2h4.2l1.5-1.5h1.6l1.5 1.5h4.2v1.6h-4.2l-1.5 1.5h-1.6l-1.5-1.5H5.5z"/>'
        '<path d="M4 7.2h16M4 16.8h16" stroke="black" stroke-opacity=".35" stroke-width="1"/>'
        '</g>'
    ),
    "dynamic_trim": (
        '<rect x="7.7" y="5.3" width="8.6" height="13.4" rx="1.1"/>'
        '<path d="m1 12 4.2-4.2v8.4zm22 0-4.2-4.2v8.4z" fill="currentColor" stroke="none"/>'
    ),
    "ripple": (
        '<path d="M2 6.5h6.8v11H2z" fill="currentColor" stroke="none"/>'
        '<path d="M9 3.5v17M12 12h8m-3.6-3.6L20 12l-3.6 3.6M22 6v12"/>'
    ),
    "rate_stretch": (
        '<circle cx="8.6" cy="11" r="6.2"/><path d="M8.6 7.1V11l2.7 1.6"/>'
        '<path d="M9 19.8h12m-3.3-3.1 3.3 3.1-3.3 3.1" stroke-width="2.1"/>'
    ),
    "roll": (
        '<path d="M11.8 3.5v17M4.5 7.7 1 12l3.5 4.3m15-8.6L23 12l-3.5 4.3M2.4 12h19.2"/>'
        '<path d="M6.7 4.8h3m4.1 14.4h3.5" stroke-width="2.2"/>'
    ),
    "slip": (
        '<rect x="2" y="5" width="20" height="14" rx=".9"/>'
        '<path d="m8 9-3 3 3 3m8-6 3 3-3 3M6 12h12"/>'
    ),
    "slide": (
        '<rect x="8" y="6" width="8" height="12" rx="1" fill="currentColor" stroke="none"/>'
        '<path d="M1.4 12h4m-1.6-2.4L1.4 12l2.4 2.4m14.8-2.4h4m-2.4-2.4 2.4 2.4-2.4 2.4"/>'
    ),
    "hand": (
        '<path d="M7.3 11.2V6.7a1.2 1.2 0 0 1 2.4 0v4.1h.8V4.4a1.3 1.3 0 0 1 2.6 0v6.3h.8V5.6a1.25 1.25 0 0 1 2.5 0v5.5h.8V8.2a1.2 1.2 0 0 1 2.4 0v6c0 4-2.1 6.9-5.3 6.9h-1.9c-2 0-3.6-.8-4.8-2.5l-4.1-5.8c-.8-1.3.7-2.6 1.8-1.6l2 1.8z" '
        'fill="currentColor" stroke-width=".8"/>'
    ),
    "zoom": '<circle cx="10.2" cy="10.2" r="6.5"/><path d="m15.1 15.1 5.6 5.6" stroke-width="2.8"/>',
    "insert": (
        '<rect x="8" y="2.2" width="8" height="5.6" rx=".8" fill="currentColor" stroke="none"/>'
        '<path d="M12 9.7v8.5m-3.1-3.1 3.1 3.1 3.1-3.1"/>'
        '<path d="M2 13.2h4v7.3H2zm16 0h4v7.3h-4z" fill="currentColor" stroke="none"/>'
    ),
    "overwrite": (
        '<rect x="2.2" y="2.8" width="10.2" height="6.7" rx=".8" fill="currentColor" stroke="none"/>'
        '<path d="M14.7 5.7h1.7c1.9 0 3 1.2 3 3v4m-2.6-2.6 2.6 2.6 2.6-2.6"/>'
        '<rect x="3" y="15.5" width="18" height="5.2" rx=".65"/>'
        '<path d="M4 16.5h8v3.2H4z" fill="currentColor" fill-opacity=".55" stroke="none"/>'
    ),
    "replace": (
        '<rect x="2.2" y="8.6" width="7.1" height="6.8" rx=".8" fill="currentColor" stroke="none"/>'
        '<rect x="14.6" y="8.6" width="7.1" height="6.8" rx=".8"/>'
        '<path d="M5.6 5.8q.3-1.9 2.6-1.9h8.3m-2.3-2.2 2.3 2.2-2.3 2.2M18.3 18.2q-.3 1.9-2.6 1.9H7.4m2.3-2.2-2.3 2.2 2.3 2.2"/>'
    ),
    "snap": (
        '<g transform="rotate(38 12 12)" fill="currentColor" stroke="none">'
        '<path d="M4 8.2h4.5v5a3.5 3.5 0 1 0 7 0v-5H20v5a8 8 0 1 1-16 0z"/>'
        '<path d="M4 3.5h4.5v3.1H4zm11.5 0H20v3.1h-4.5z"/></g>'
    ),
    "link": (
        # Each ring shows its inside bend through the other's opening. At
        # opposite crossings the far strand falls into shadow on either
        # side of the near strand, even against a colored clip background.
        '<g transform="rotate(-33 12 12)" stroke-width="2.35" stroke-linecap="butt">'
        '<path d="M5.6 8.3h5.9m1 7.4h5.9" stroke-opacity=".20"/>'
        '<path d="M5.8 8.3h-.4a4.6 4.6 0 0 0 0 9.2h5.9a4.6 4.6 0 0 0 0-9.2h-.2"/>'
        '<path d="M18.2 15.7h.4a4.6 4.6 0 0 0 0-9.2h-5.9a4.6 4.6 0 0 0 0 9.2h.2"/>'
        '</g>'
    ),
    "unlink": (
        '<g transform="rotate(-28 12 12)" stroke-width="2.2">'
        '<path d="M9 10.2H5.1a3.6 3.6 0 0 0 0 7.2H9q1.4 0 2.3-1"/>'
        '<path d="M15 13.8h3.9a3.6 3.6 0 0 0 0-7.2H15q-1.4 0-2.3 1"/>'
        '</g>'
    ),
    "position_lock": (
        '<path d="M7.3 10V7a4.7 4.7 0 0 1 9.4 0v3" stroke-width="2.2"/>'
        '<path d="M5 9.5h14a1.5 1.5 0 0 1 1.5 1.5v8a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 19v-8A1.5 1.5 0 0 1 5 9.5z'
        'M6 15l3.3 3v-2.1h5.4V18l3.3-3-3.3-3v2.1H9.3V12z" fill="currentColor" fill-rule="evenodd" stroke="none"/>'
    ),
    "view_options": (
        '<path fill="currentColor" stroke="none" fill-rule="evenodd" '
        'd="M5 3h14a3 3 0 0 1 3 3v12a3 3 0 0 1-3 3H5a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3z'
        'M6 7.5h12v3H6zM6 13.5h5v3H6zM13 13.5h5v3h-5z"/>'
    ),
    "keyframes": (
        '<path d="M2.4 15.5v3.6h19.2v-3.6"/>'
        '<path d="m6.6 5.2 3.6 4.6-3.6 4.6L3 9.8z" fill="currentColor" stroke="none"/>'
        '<path d="m16.8 5.2 3.6 4.6-3.6 4.6-3.6-4.6z"/>'
    ),
    "microphone": (
        '<rect x="8.8" y="2" width="6.4" height="12.1" rx="3.2" fill="currentColor" stroke="none"/>'
        '<path d="M5.3 10.8v.8a6.7 6.7 0 0 0 13.4 0v-.8M12 18.4v3.2m-3.4 0h6.8"/>'
    ),
    "flag": (
        '<path d="M5.4 21V3.1" stroke-width="2"/>'
        '<path d="M6 4h13.8l-3.3 4.2 3.3 4.2H6z" fill="currentColor" stroke="none"/>'
    ),
    "marker": (
        '<path d="M6.4 3.2h11.2v9.9L12 21l-5.6-7.9zM10 8a2 2 0 1 0 4 0 2 2 0 1 0-4 0z" '
        'fill="currentColor" fill-rule="evenodd" stroke="none"/>'
    ),
    "zoom_full": (
        '<path d="M8.5 16H2.3V4h17.5v3.6M6.2 4v12m9.7-12v3.6M2.3 8.1h3.9M2.3 12h3.9"/>'
        + _LENS
    ),
    "zoom_detail": (
        '<path d="M8 14.3H3.7V4H15v3.7" stroke-width="2.6"/>'
        + _LENS + '<path d="M13 14.6h4m-2-2v4" stroke-width="1.2"/>'
    ),
    "zoom_custom": (
        '<circle cx="10.3" cy="10.3" r="7.1" stroke-width="2"/>'
        '<path d="m15.5 15.5 5 5" stroke-width="2.8"/>'
        '<path d="M6.6 10.3H14" stroke-width="1.4"/>'
        '<circle cx="11.8" cy="10.3" r="1.9" fill="currentColor" stroke="none"/>'
    ),
    "zoom_out": '<circle cx="12" cy="12" r="8.5" fill="currentColor" fill-opacity=".17" stroke="none"/><path d="M8 12h8" stroke-width="2"/>',
    "zoom_in": '<circle cx="12" cy="12" r="8.5" fill="currentColor" fill-opacity=".17" stroke="none"/><path d="M8 12h8m-4-4v8" stroke-width="2"/>',
    "speaker": '<path d="M3.5 9h4l5-4v14l-5-4h-4zM16 8.5a5 5 0 0 1 0 7m2.8-9.5a8.5 8.5 0 0 1 0 12"/>',
    "speaker_muted": '<path d="M3.5 9h4l5-4v14l-5-4h-4zM16 9l5 6m0-6-5 6"/>',
    "more": '<g fill="currentColor" stroke="none"><circle cx="5" cy="12" r="1.85"/><circle cx="12" cy="12" r="1.85"/><circle cx="19" cy="12" r="1.85"/></g>',
}


@lru_cache(maxsize=96)
def timeline_icon(name: str, color: str = IconColors.DEFAULT) -> QIcon:
    """Render a cached high-DPI toolbar icon from a small SVG drawing."""
    body = _PATHS[name]
    ink = QColor(color).name()
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" color="{ink}"><g fill="none" stroke="{ink}" stroke-width="1.85" stroke-linejoin="round" stroke-linecap="round">{body}</g></svg>'
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
