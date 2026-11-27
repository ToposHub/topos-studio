"""Video Color tool groups and transparent resize handles."""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QEvent, QObject, QRect, Qt, QTimer
from PyQt6.QtWidgets import QDockWidget, QMainWindow

from src.shared.ui.dock_resize_handle import DockResizeHandle


VIDEO_COLOR_PRIMARY = ("dock_raw", "dock_grading", "dock_base_grade", "dock_chromogen", "dock_denoise")
VIDEO_COLOR_SECONDARY = (
    "dock_curves", "dock_warper", "dock_qualifier", "dock_masks", "dock_tracker",
    "dock_color_keyframes", "dock_script",
)
VIDEO_COLOR_SCOPES = ("dock_analysis", "dock_effects", "dock_grain")


class VideoColorResizeHandles(QObject):
    def __init__(self, host: QMainWindow, *, gallery: tuple[QDockWidget, ...],
                 viewer: QDockWidget, nodes: QDockWidget, clips: QDockWidget,
                 primary: tuple[QDockWidget, ...], secondary: tuple[QDockWidget, ...],
                 scopes: tuple[QDockWidget, ...],
                 fit_layout: Callable[[int, int], None]) -> None:
        super().__init__(host)
        self._host = host
        self._fit_layout = fit_layout
        self._available_size: tuple[int, int] | None = None
        self._gallery, self._primary, self._secondary = gallery, primary, secondary
        self._viewer, self._nodes, self._clips, self._scopes = viewer, nodes, clips, scopes
        self._clip_height_limits = (clips.minimumHeight(), clips.maximumHeight())
        horizontal = Qt.Orientation.Horizontal
        vertical = Qt.Orientation.Vertical
        self._top_handles = [DockResizeHandle(host, horizontal) for _ in range(2)]
        self._bottom_handles = [DockResizeHandle(host, horizontal) for _ in range(2)]
        self._row_handles = [DockResizeHandle(host, vertical) for _ in range(2)]
        self._handles = [*self._top_handles, *self._bottom_handles, *self._row_handles]
        self._active = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._refresh)
        for widget in (host, viewer, nodes, clips, *gallery, *primary, *secondary, *scopes):
            widget.installEventFilter(self)

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._available_size = None
        self._active = active
        self._sync_clip_height()
        if active:
            self._timer.start(0)
        else:
            self._timer.stop()
            for handle in self._handles:
                handle.hide()

    def _sync_clip_height(self) -> None:
        # Preserve the thumbnail preset. A fixed inner widget alone lets
        # QMainWindow pad the surrounding dock when the splitters move.
        clips = self._clips
        if self._active and not clips.isFloating():
            title = clips.titleBarWidget()
            strip = clips.widget()
            height = max(strip.sizeHint().height(), strip.minimumHeight())
            height += title.minimumHeight() if title is not None else 0
            if height > 0 and (clips.minimumHeight(), clips.maximumHeight()) != (height, height):
                clips.setFixedHeight(height)
        elif (clips.minimumHeight(), clips.maximumHeight()) != self._clip_height_limits:
            minimum, maximum = self._clip_height_limits
            clips.setMinimumHeight(minimum)
            clips.setMaximumHeight(maximum)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._active and event.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show,
                                              QEvent.Type.Hide, QEvent.Type.LayoutRequest):
            if not self._timer.isActive():
                self._timer.start(0)
        return False

    @staticmethod
    def _front(docks: tuple[QDockWidget, ...]) -> QDockWidget | None:
        return next((dock for dock in docks if dock.isVisible() and not dock.isFloating()
                     and not dock.visibleRegion().isEmpty()), None)

    def _refresh(self) -> None:
        if self._host.isVisible():
            screen = self._host.screen()
            bounds = screen.availableGeometry() if screen is not None else self._host.rect()
            available = min(bounds.width(), self._host.width()), min(bounds.height(), self._host.height())
            if available != self._available_size:
                self._available_size = available
                self._fit_layout(*available)
        self._sync_clip_height()
        gallery = self._front(self._gallery)
        viewer = self._front((self._viewer,))
        nodes = self._front((self._nodes,))
        clips = self._front((self._clips,))
        primary = self._front(self._primary)
        secondary = self._front(self._secondary)
        scopes = self._front(self._scopes)
        self._top_handles[0].place_column(gallery, viewer)
        self._top_handles[1].place_column(viewer, nodes)
        self._bottom_handles[0].place_column(primary, secondary)
        self._bottom_handles[1].place_column(secondary or primary, scopes)

        self._place_row_handles(viewer, clips, primary or secondary)

    def _place_row_handles(self, viewer: QDockWidget | None, clips: QDockWidget | None,
                           tools: QDockWidget | None) -> None:
        if viewer is not None and clips is not None and tools is not None:
            clip_rect = clips.geometry()
            viewer_gap = clip_rect.top() - viewer.geometry().bottom() - 1
            tool_gap = tools.y() - clip_rect.bottom() - 1
            # The clip dock may be moved. Only link the rows when the strip
            # still sits immediately below the viewer and above the tool tabs.
            if 0 <= viewer_gap <= 6 and 0 <= tool_gap <= 40:
                radius = DockResizeHandle.HIT_RADIUS
                for handle, edge in zip(self._row_handles, (clip_rect.top(), clip_rect.bottom() + 1)):
                    handle.place(QRect(clip_rect.x(), edge - radius, clip_rect.width(), radius * 2),
                                 viewer, tools, tool_tab_height=max(0, tool_gap - viewer_gap))
                return
        for handle in self._row_handles:
            handle.hide()
