"""Keyframes timeline for the Motion Graphic editor.

Layout follows the composite page's proven Keyframes panel interaction model:
a left element/property tree and a right ruler + keyframe-track canvas share
one row grid (24 px rows under a 30 px ruler/header band) so both halves stay
pixel-aligned, one vertical scroll offset and one frame↔pixel mapper.

The panel is host-decoupled: it never touches the scene store or the command
stack directly.  Every mutation leaves through :signal:`tracks_changed` as a
batch of whole-track edits (``None`` removes the animation), so the window
can land the whole batch as one undo step; playback/scrubbing leaves through
:signal:`frame_changed`; element selection through :signal:`element_selected`.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QIcon, QKeySequence, QMouseEvent, QPainter, QPen, QPolygonF,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QHBoxLayout, QInputDialog, QMenu, QSizePolicy, QSlider,
    QScrollBar, QToolButton, QVBoxLayout, QWidget,
)

from src.shared.ui.panel_splitter import PanelSplitter
from src.features.edit.ui.edit_style import EDIT_COLORS, EDIT_SIZES
from src.shared.media.metadata import Rational
from src.shared.media.timecode import Timecode
from src.shared.i18n.i18n import I18N
from .mg_shortcuts import (
    PLAY_PAUSE_ACTION_ID,
    PLAY_PAUSE_DEFAULT,
    configured_shortcut,
    event_matches,
)
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack
from src.shared.ui.style_sheet import C

from ..core.model import TeachingSceneDocument
from .layer_tree_model import HTML_LAYER_ROW_ID
from .mg_keyframes_model import (
    FrameMapper, MgKeyframesRow, build_keyframe_rows, effective_tracks,
    static_value_for, track_with_interpolation, track_with_keyframe_added,
    track_with_keyframe_copied, track_with_keyframe_value,
    track_with_keyframes_moved, track_with_keyframes_removed,
)

_ROW_HEIGHT = 24
_RULER_HEIGHT = EDIT_SIZES["ruler_h"]
_KEYFRAME_COLUMN = 36          # tree-side summary-diamond column width
_EXPAND_ZONE = 18              # tree-side expand-arrow click zone
_DIAMOND = 4.5                 # keyframe diamond half-size (px)
_HIT_RADIUS = 7.0              # keyframe hit radius (px)
_SEEK_THROTTLE_MS = 16
_MARQUEE_THRESHOLD = 4.0
_MIN_PPF = 0.01
_MAX_PPF = 50.0
_ZOOM_SLIDER_MAX = 1000
# The Edit timeline opens with a useful navigation range instead of fitting
# the entire sequence into one viewport.  Keep roughly two viewports of
# timeline content visible so the bottom scrollbar has a real draggable range.
_INITIAL_TIMELINE_VIEWPORTS = 2.0

_INTERPOLATION_MODES: tuple[InterpolationType, ...] = (
    InterpolationType.LINEAR, InterpolationType.BEZIER, InterpolationType.HOLD,
    InterpolationType.EASE_IN, InterpolationType.EASE_OUT, InterpolationType.EASE_IN_OUT,
)


def _tr(key: str, fallback: str, **kwargs) -> str:  # type: ignore[no-untyped-def]
    return I18N.tr(f"motion_graphic.keyframes.{key}", fallback, **kwargs)


def _property_label(property_name: str) -> str:
    return I18N.tr(
        f"motion_graphic.keyframes.prop.{property_name}",
        property_name.replace("_", " ").title(),
    )


def _diamond_polygon(cx: float, cy: float, half: float = _DIAMOND) -> QPolygonF:
    return QPolygonF([
        QPointF(cx, cy - half), QPointF(cx + half, cy), QPointF(cx, cy + half),
        QPointF(cx - half, cy),
    ])


def _tool_icon(name: str) -> QIcon:
    try:
        from src.shared.assets.icon_manager import IconColors, get_icon
        return get_icon(name, IconColors.DEFAULT)
    except Exception:  # noqa: BLE001 - an icon must never break the toolbar
        return QIcon()


@dataclass
class _KeyframeDrag:
    """Snapshot-first keyframe drag (validate-all-then-apply on release)."""

    start_x: float
    delta: int = 0
    applied_delta: int = 0
    moved: bool = False
    duplicated: bool = False
    rows: dict[str, list[int]] = field(default_factory=dict)  # row_id → selected frames


@dataclass
class _PageResizeDrag:
    """One page-edge drag, committed as a single duration command on release."""

    page_id: str
    start_frame: int
    original_duration: int
    duration_frames: int


class MgKeyframesPanel(QWidget):
    """Element tree + keyframe tracks + ruler/playhead for one MG scene."""

    frame_changed = pyqtSignal(int)
    element_selected = pyqtSignal(str)
    tracks_changed = pyqtSignal(list)  # list[(element_id, property, track|None)]
    pageDurationChangeRequested = pyqtSignal(str, int)
    playbackToggleRequested = pyqtSignal()
    pageExpansionChanged = pyqtSignal(tuple)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mgKeyframesPanel")
        self.setMinimumHeight(190)
        self._scene: TeachingSceneDocument | None = None
        self._rows: list[MgKeyframesRow] = []
        # AUD-30802: row_id → index map rebuilt with _rows so hit-testing /
        # lookups are O(1) instead of a linear rescan per call.
        self._row_index_by_id: dict[str, int] = {}
        self._expanded: frozenset[str] = frozenset()
        self._expanded_pages: frozenset[str] = frozenset()
        self._selected_elements: frozenset[str] = frozenset()
        self._selected_rows: set[str] = set()
        self._point_selection: set[tuple[str, int]] = set()
        self._property_filter = "all"
        self._current_frame = 0
        self._duration = 1
        self._fps = 24.0
        self._mapper = FrameMapper(origin_locked=True)
        self._page_resize: _PageResizeDrag | None = None
        self._v_scroll = 0.0
        self._clipboard: list[tuple[str, str, int, Keyframe]] = []
        self._fitted = False
        self._initial_zoom_needs_layout = False
        # AUD-30907：父属单发定时器——面板销毁后初始缩放回调必不触发
        self._initial_zoom_timer = QTimer(self)
        self._initial_zoom_timer.setSingleShot(True)
        self._initial_zoom_timer.setInterval(0)
        self._initial_zoom_timer.timeout.connect(
            self._apply_initial_zoom_after_layout)
        self._time_display = "00:00:00:00"
        self._zoom_slider_updating = False
        self._horizontal_scrollbar_updating = False
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        # Panel-scoped on purpose: a window/application-wide QShortcut would
        # swallow the key before any event filter — including the host's —
        # could route it (see src/shared/ui/shortcut_scope.py).
        self._space_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._space_shortcut.activated.connect(self.playbackToggleRequested.emit)
        self._build_ui()

    def apply_play_shortcut(self) -> None:
        """Follow the user-configured play/pause key (motion_graphic/play_pause)."""
        self._space_shortcut.setKey(
            configured_shortcut(PLAY_PAUSE_ACTION_ID, PLAY_PAUSE_DEFAULT)
        )

    # ── public API (host drives everything through these) ───────────────────

    def set_scene(self, scene: TeachingSceneDocument, *,
                  selected_ids: frozenset[str] | set[str] = frozenset(),
                  expanded_page_ids: frozenset[str] | set[str] | None = None) -> None:
        selected = frozenset(selected_ids)
        duration = max(1, scene.duration_frames)
        if expanded_page_ids is not None:
            expanded_pages = frozenset(expanded_page_ids) & frozenset(scene.page_ids)
        elif self._scene is None:
            expanded_pages = frozenset(scene.page_ids)
        else:
            expanded_pages = self._expanded_pages & frozenset(scene.page_ids)
        expansion_changed = expanded_pages != self._expanded_pages
        # Documents are immutable: the same object with the same selection and
        # duration cannot have changed.  The host calls set_scene for every
        # frame refresh on the SVG path, so this fast path keeps playback from
        # rebuilding all rows 24× a second.
        if (scene is self._scene and scene is not None
                and selected == self._selected_elements
                and duration == self._duration
                and not expansion_changed):
            return
        first = self._scene is None or self._scene.scene_id != scene.scene_id
        duration_changed = self._duration != duration
        self._scene = scene
        self._expanded_pages = expanded_pages
        self._selected_elements = selected
        self._duration = duration
        self._fps = scene.fps
        self._current_frame = max(0, min(self._current_frame, self._duration - 1))
        self._time_display = self._format_timecode(self._current_frame)
        if not self._fitted or (first and duration_changed):
            self._fitted = True
            self._initial_zoom_needs_layout = not self.isVisible()
            self._set_initial_zoom()
        self._rebuild()
        self._ensure_selected_visible()
        self._sync_horizontal_scrollbar()

    def set_selection(self, ids: frozenset[str] | set[str]) -> None:
        """Highlight-only selection update; never re-emits element_selected."""

        self._selected_elements = frozenset(ids)
        if self._scene is not None:
            selected_page_ids = {
                element.page_id for element in self._scene.elements
                if element.element_id in self._selected_elements
            }
            new_pages = self._expanded_pages | selected_page_ids
            if new_pages != self._expanded_pages:
                self._expanded_pages = frozenset(new_pages)
                self.pageExpansionChanged.emit(self.expanded_page_ids)
        self._rebuild()
        self._ensure_selected_visible()

    @property
    def expanded_page_ids(self) -> tuple[str, ...]:
        if self._scene is None:
            return tuple(self._expanded_pages)
        return tuple(page_id for page_id in self._scene.page_ids
                     if page_id in self._expanded_pages)

    def set_expanded_pages(self, page_ids: frozenset[str] | set[str]) -> None:
        """Apply expansion from the Layers tree without emitting."""
        if self._scene is None:
            self._expanded_pages = frozenset(page_ids)
            return
        self._expanded_pages = frozenset(page_ids) & frozenset(self._scene.page_ids)
        self._rebuild()

    def set_current_frame(self, frame: int) -> None:
        """Silent playhead update (the host already knows the frame)."""

        self._current_frame = max(0, min(int(frame), self._duration - 1))
        self._time_display = self._format_timecode(self._current_frame)
        self._tree.update()
        self._tracks.update()

    @property
    def transport_layout(self) -> QHBoxLayout:
        """Toolbar slot used by the host to dock shared playback controls."""

        return self._transport_layout

    @property
    def time_display(self) -> str:
        return self._time_display

    def _format_timecode(self, frame: int) -> str:
        """Format the readout exactly like Edit's fixed timecode panel."""

        try:
            scene = self._scene
            fps = Rational(
                int(scene.fps_num) if scene is not None else max(1, round(self._fps)),
                int(scene.fps_den) if scene is not None else 1,
            )
            return Timecode.from_frame_number(
                max(0, int(frame)), fps, Timecode.auto_detect_type(fps),
            ).to_string()
        except (TypeError, ValueError, ZeroDivisionError):
            fps_int = max(1, round(self._fps))
            total = max(0, int(frame))
            hours, remainder = divmod(total, fps_int * 3600)
            minutes, remainder = divmod(remainder, fps_int * 60)
            seconds, frames = divmod(remainder, fps_int)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    @property
    def current_frame(self) -> int:
        return self._current_frame

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QWidget(self)
        bar.setObjectName("mgKeyframesToolbar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(6, 2, 6, 2)
        bar_layout.setSpacing(2)

        def _button(tooltip: str, icon: QIcon = None, text: str = "") -> QToolButton:  # type: ignore[assignment]
            button = QToolButton(bar)
            button.setObjectName("mgKeyframesToolButton")
            button.setToolTip(tooltip)
            button.setFixedSize(EDIT_SIZES["icon_btn"], EDIT_SIZES["icon_btn"])
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if icon is not None and not icon.isNull():
                button.setIcon(icon)
                button.setIconSize(QSize(15, 15))
            else:
                button.setText(text)
            bar_layout.addWidget(button)
            return button

        self._toggle_key_button = _button(
            _tr("toggle_keyframe", "Toggle keyframe at playhead for the selected rows"),
            text="◆")
        self._toggle_key_button.clicked.connect(self._toggle_keyframe_at_playhead)
        self._delete_button = _button(
            _tr("delete_keyframes", "Delete selected keyframes"),
            _tool_icon("fa5s.trash"), "✕")
        self._delete_button.clicked.connect(self.delete_selected)
        bar_layout.addSpacing(8)
        self._zoom_out_button = _button(
            _tr("zoom_out", "Zoom out"), _tool_icon("fa5s.search-minus"), "−")
        self._zoom_out_button.clicked.connect(lambda: self._zoom(1 / 1.25))
        self._fit_button = _button(
            _tr("zoom_fit", "Fit the whole duration on screen"),
            _tool_icon("fa5s.expand-arrows-alt"), "Fit")
        self._fit_button.clicked.connect(self.fit_time)
        self._zoom_in_button = _button(
            _tr("zoom_in", "Zoom in"), _tool_icon("fa5s.search-plus"), "+")
        self._zoom_in_button.clicked.connect(lambda: self._zoom(1.25))
        self._center_button = _button(
            _tr("center_playhead", "Center the playhead"),
            _tool_icon("fa5s.crosshairs"), "◎")
        self._center_button.clicked.connect(self.center_playhead)
        # Edit's timeline keeps frame zero pinned; a separate center command
        # would contradict that navigation model, so retain the API for
        # compatibility but remove the button from the visible toolbar.
        bar_layout.removeWidget(self._zoom_out_button)
        bar_layout.removeWidget(self._fit_button)
        bar_layout.removeWidget(self._zoom_in_button)
        bar_layout.removeWidget(self._center_button)
        self._center_button.hide()

        self._zoom_slider = QSlider(Qt.Orientation.Horizontal, bar)
        self._zoom_slider.setObjectName("mgKeyframesZoomSlider")
        self._zoom_slider.setRange(0, _ZOOM_SLIDER_MAX)
        self._zoom_slider.setValue(self._ppf_to_slider(self._mapper.pixels_per_frame))
        self._zoom_slider.setFixedWidth(100)
        self._zoom_slider.setToolTip(_tr("zoom_slider", "Timeline Zoom"))
        self._zoom_slider.valueChanged.connect(self._on_zoom_slider_changed)
        bar_layout.addSpacing(8)
        expand_all = _button(
            _tr("expand_all", "Expand all elements"),
            _tool_icon("fa5s.angle-double-down"), "▼")
        expand_all.clicked.connect(lambda: self._set_all_expanded(True))
        collapse_all = _button(
            _tr("collapse_all", "Collapse all elements"),
            _tool_icon("fa5s.angle-double-up"), "▲")
        collapse_all.clicked.connect(lambda: self._set_all_expanded(False))
        self._filter_button = QToolButton(bar)
        self._filter_button.setObjectName("mgKeyframesFilterButton")
        self._filter_button.setToolTip(
            _tr("filter_properties", "Filter visible property rows"))
        self._filter_button.setFixedSize(84, 24)
        self._filter_button.setCursor(Qt.CursorShape.PointingHandCursor)
        filter_menu = QMenu(self._filter_button)
        self._filter_actions: dict[str, object] = {}
        for mode, label_key, fallback in (
            ("all", "filter_all", "All"),
            ("animated", "filter_animated", "Animated"),
        ):
            action = filter_menu.addAction(_tr(label_key, fallback))
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, value=mode: self.set_property_filter(value))
            self._filter_actions[mode] = action
        self._filter_button.setMenu(filter_menu)
        self._filter_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        bar_layout.addWidget(self._filter_button)
        transport_host = QWidget(bar)
        transport_host.setObjectName("mgKeyframesTransportHost")
        self._transport_layout = QHBoxLayout(transport_host)
        self._transport_layout.setContentsMargins(4, 0, 0, 0)
        self._transport_layout.setSpacing(4)
        bar_layout.addWidget(transport_host)
        bar_layout.addStretch(1)
        # Match Edit's right-aligned zoom group: zoom-out, slider, zoom-in,
        # then fit.  Horizontal navigation is provided by the bottom scrollbar
        # and the unmodified mouse wheel.
        bar_layout.addWidget(self._zoom_out_button)
        bar_layout.addWidget(self._zoom_slider)
        bar_layout.addWidget(self._zoom_in_button)
        bar_layout.addWidget(self._fit_button)
        self._panel_title = _ToolLabel(_tr("panel_title", "Keyframes"), bar)
        self._panel_title.hide()
        layout.addWidget(bar)

        self._tree = _TreeSide(self)
        self._tracks = _TrackSide(self)
        track_container = QWidget(self)
        track_container.setObjectName("mgKeyframesTrackContainer")
        track_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        track_layout = QVBoxLayout(track_container)
        track_layout.setContentsMargins(0, 0, 0, 0)
        track_layout.setSpacing(0)
        track_layout.addWidget(self._tracks, 1)
        splitter = PanelSplitter(Qt.Orientation.Horizontal, self)
        splitter.setObjectName("mgKeyframesSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._tree)
        splitter.addWidget(track_container)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([EDIT_SIZES["track_header_w"], 720])
        layout.addWidget(splitter, 1)
        self._splitter = splitter
        self._track_container = track_container
        self._horizontal_scrollbar = QScrollBar(Qt.Orientation.Horizontal, track_container)
        self._horizontal_scrollbar.setObjectName("mgKeyframesHorizontalScrollBar")
        # Match Edit's timeline: the scrollbar belongs only to the track
        # viewport, so it never occupies the left layer-tree column.
        self._horizontal_scrollbar.setFixedHeight(8)
        self._horizontal_scrollbar.setSingleStep(24)
        self._horizontal_scrollbar.setPageStep(1)
        self._horizontal_scrollbar.valueChanged.connect(
            self._on_horizontal_scroll_changed,
        )
        track_layout.addWidget(self._horizontal_scrollbar)
        self.set_property_filter("all")

    # ── row model plumbing ───────────────────────────────────────────────────

    def set_property_filter(self, property_filter: str) -> None:
        """Show every property or only properties that contain keyframes."""

        if property_filter not in {"all", "animated"}:
            raise ValueError(f"Unknown keyframe property filter: {property_filter}")
        self._property_filter = property_filter
        labels = {
            "all": _tr("filter_all", "All"),
            "animated": _tr("filter_animated", "Animated"),
        }
        self._filter_button.setText(labels[property_filter])
        for mode, action in self._filter_actions.items():
            action.setChecked(mode == property_filter)
        self._rebuild()

    def _rebuild(self) -> None:
        scene = self._scene
        self._rows = build_keyframe_rows(
            scene,
            selected_ids=self._selected_elements,
            expanded_ids=self._expanded,
            expanded_page_ids=self._expanded_pages,
            property_filter=self._property_filter,
        ) if scene is not None else []
        # AUD-30802: keep the row_id → index map in step with _rows so
        # _row_by_id / hit tests stop rescanning the whole table per call.
        self._row_index_by_id = {
            row.row_id: index for index, row in enumerate(self._rows)
        }
        # Re-apply keyframe-point selection onto the fresh points and prune
        # selections whose (row, frame) no longer exists.
        live: set[tuple[str, int]] = set()
        for row in self._rows:
            for point in row.points:
                key = (row.row_id, point.frame)
                if key in self._point_selection:
                    point.selected = True
                    live.add(key)
        self._point_selection = live
        self._selected_rows &= {row.row_id for row in self._rows}
        self._clamp_scroll()
        self._refresh_action_states()
        self._tree.update()
        self._tracks.update()

    def _refresh_action_states(self) -> None:
        has_points = bool(self._point_selection)
        self._delete_button.setEnabled(has_points)
        self._toggle_key_button.setEnabled(bool(self._keyframe_target_rows()))

    def _keyframe_target_rows(self) -> list[MgKeyframesRow]:
        """Rows the ◆ toolbar button acts on: selected property rows, else
        every property row of the currently selected elements."""

        property_rows = [row for row in self._rows if row.kind == "property"]
        selected = [row for row in property_rows if row.row_id in self._selected_rows]
        if selected:
            return selected
        element_ids = self._selected_elements - {HTML_LAYER_ROW_ID}
        if element_ids:
            return [row for row in property_rows if row.element_id in element_ids]
        return []

    def _row_by_id(self, row_id: str) -> MgKeyframesRow | None:
        # AUD-30802: O(1) via the map maintained by _rebuild.
        index = self._row_index_by_id.get(row_id)
        return self._rows[index] if index is not None else None

    def _row_at_y(self, y: float) -> MgKeyframesRow | None:
        index = int((y - _RULER_HEIGHT + self._v_scroll) // _ROW_HEIGHT)
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def _row_y(self, index: int) -> float:
        return _RULER_HEIGHT + index * _ROW_HEIGHT - self._v_scroll

    # ── vertical scroll (shared by both halves) ──────────────────────────────

    def _max_scroll(self) -> float:
        viewport = max(0.0, self._tracks.height() - _RULER_HEIGHT)
        return max(0.0, len(self._rows) * _ROW_HEIGHT - viewport)

    def _clamp_scroll(self) -> None:
        self._v_scroll = max(0.0, min(self._v_scroll, self._max_scroll()))

    def scroll_by(self, dy: float) -> None:
        self._v_scroll = max(0.0, min(self._v_scroll + dy, self._max_scroll()))
        self._tree.update()
        self._tracks.update()

    def scroll_horizontally(self, delta: float) -> None:
        """Move the timeline viewport using the Edit-style scrollbar."""
        if delta == 0:
            return
        bar = self._horizontal_scrollbar
        bar.setValue(max(bar.minimum(), min(
            bar.maximum(), bar.value() + round(delta),
        )))

    def _horizontal_max_scroll(self) -> int:
        viewport_width = max(1, self._tracks.width())
        content_width = max(
            float(viewport_width),
            float(self._duration) * self._mapper.pixels_per_frame,
        )
        return max(0, math.ceil(content_width - viewport_width))

    def _sync_horizontal_scrollbar(self) -> None:
        """Keep the scrollbar and frame mapper in one coordinate space."""
        if not hasattr(self, "_horizontal_scrollbar"):
            return
        bar = self._horizontal_scrollbar
        maximum = self._horizontal_max_scroll()
        value = max(0, min(maximum, round(-self._mapper.scroll_offset)))
        self._horizontal_scrollbar_updating = True
        try:
            bar.setPageStep(max(1, self._tracks.width()))
            bar.setRange(0, maximum)
            bar.setValue(value)
            # Keep the control visible like Edit's timeline.  When the whole
            # duration fits, it remains disabled; zooming or resizing beyond
            # the viewport enables the same handle for horizontal panning.
            bar.setVisible(True)
        finally:
            self._horizontal_scrollbar_updating = False
        self._mapper.scroll_offset = -float(bar.value())
        bar.setEnabled(maximum > 0)

    def _on_horizontal_scroll_changed(self, value: int) -> None:
        if self._horizontal_scrollbar_updating:
            return
        self._mapper.scroll_offset = -float(value)
        self._tracks.update()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._sync_horizontal_scrollbar()

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        if self._initial_zoom_needs_layout and self._scene is not None:
            # The splitter's child widths are finalized after showEvent on
            # some Qt styles; defer once so the initial range uses the real
            # track viewport instead of the construction-time width.
            self._initial_zoom_timer.start()

    def _apply_initial_zoom_after_layout(self) -> None:
        if not self.isVisible() or not self._initial_zoom_needs_layout or self._scene is None:
            return
        self._set_initial_zoom()
        self._initial_zoom_needs_layout = False

    def _page_row_active(self, row: MgKeyframesRow) -> bool:
        """True when the playhead sits inside this page row's extent."""
        return any(start <= self._current_frame <= end
                   for start, end in self._page_spans_for_row(row))

    def _page_spans_for_row(self, row: MgKeyframesRow) -> tuple[tuple[int, int], ...]:
        """Return page spans, including the in-progress edge-drag preview."""
        if row.kind != "page" or not row.page_spans or self._scene is None:
            return row.page_spans
        drag = self._page_resize
        if drag is None or drag.page_id not in self._scene.page_ids:
            return row.page_spans
        try:
            resized_index = self._scene.page_ids.index(drag.page_id)
            row_index = self._scene.page_ids.index(row.page_id)
        except ValueError:
            return row.page_spans
        delta = drag.duration_frames - drag.original_duration
        start, end = row.page_spans[0]
        if row_index > resized_index:
            start += delta
            end += delta
        if row_index == resized_index:
            end = start + drag.duration_frames - 1
        return ((start, max(start, end)),)

    def _seek_to_page_row(self, row: MgKeyframesRow) -> None:
        """Page-header click: jump the playhead to the page start."""
        spans = self._page_spans_for_row(row)
        if not spans:
            return
        frame = max(0, min(spans[0][0], self._duration - 1))
        self._current_frame = frame
        self.frame_changed.emit(frame)
        self._tree.update()
        self._tracks.update()

    def _ensure_selected_visible(self) -> None:
        if not self._rows:
            return
        index = next((i for i, row in enumerate(self._rows)
                      if row.kind == "element" and row.selected), None)
        if index is None:
            # Any other selected row (e.g. a property row) still scrolls in.
            index = next((i for i, row in enumerate(self._rows) if row.selected), None)
        if index is None:
            index = next((i for i, row in enumerate(self._rows)
                          if row.row_id in self._selected_rows), None)
        if index is None:
            return
        top = self._row_y(index)
        bottom = top + _ROW_HEIGHT
        if top < _RULER_HEIGHT:
            self._v_scroll -= _RULER_HEIGHT - top
        elif bottom > self._tracks.height():
            self._v_scroll += bottom - self._tracks.height()
        self._clamp_scroll()
        self._tree.update()
        self._tracks.update()

    # ── expansion ────────────────────────────────────────────────────────────

    def _toggle_expanded(self, element_id: str) -> None:
        self._expanded = self._expanded ^ {element_id}
        self._rebuild()

    def _set_all_expanded(self, expanded: bool) -> None:
        scene = self._scene
        if scene is None:
            return
        self._expanded = frozenset(e.element_id for e in scene.elements) if expanded else frozenset()
        self._expanded_pages = frozenset(scene.page_ids) if expanded else frozenset()
        self._rebuild()
        self.pageExpansionChanged.emit(self.expanded_page_ids)

    def _toggle_page_expanded(self, page_id: str) -> None:
        if self._scene is None or page_id not in self._scene.page_ids:
            return
        pages = set(self._expanded_pages)
        if page_id in pages:
            pages.remove(page_id)
        else:
            pages.add(page_id)
        self._expanded_pages = frozenset(pages)
        self._rebuild()
        self.pageExpansionChanged.emit(self.expanded_page_ids)

    # ── zoom helpers ─────────────────────────────────────────────────────────

    def _ppf_to_slider(self, pixels_per_frame: float) -> int:
        """Map zoom logarithmically like Edit's timeline slider."""

        ppf = max(_MIN_PPF, min(_MAX_PPF, float(pixels_per_frame)))
        ratio = (math.log(ppf) - math.log(_MIN_PPF)) / (
            math.log(_MAX_PPF) - math.log(_MIN_PPF))
        return max(0, min(_ZOOM_SLIDER_MAX, round(ratio * _ZOOM_SLIDER_MAX)))

    def _slider_to_ppf(self, value: int) -> float:
        """Map the Edit-style logarithmic slider back to pixels per frame."""

        ratio = max(0, min(_ZOOM_SLIDER_MAX, int(value))) / _ZOOM_SLIDER_MAX
        return math.exp(
            math.log(_MIN_PPF)
            + ratio * (math.log(_MAX_PPF) - math.log(_MIN_PPF))
        )

    def _sync_zoom_slider(self) -> None:
        self._zoom_slider_updating = True
        try:
            self._zoom_slider.setValue(self._ppf_to_slider(self._mapper.pixels_per_frame))
        finally:
            self._zoom_slider_updating = False

    def _set_zoom_ppf(self, pixels_per_frame: float) -> None:
        """Apply zoom while keeping frame zero anchored at x=0."""

        self._mapper.pixels_per_frame = min(
            _MAX_PPF, max(_MIN_PPF, float(pixels_per_frame)))
        self._mapper.scroll_offset = 0.0
        self._sync_zoom_slider()
        self._sync_horizontal_scrollbar()
        self._tracks.update()

    def _on_zoom_slider_changed(self, value: int) -> None:
        if self._zoom_slider_updating:
            return
        self._set_zoom_ppf(self._slider_to_ppf(value))

    def fit_time(self) -> None:
        width = max(80.0, float(self._tracks.width()))
        self._mapper.pixels_per_frame = min(_MAX_PPF, max(_MIN_PPF, width / self._duration))
        self._mapper.scroll_offset = 0.0
        self._sync_zoom_slider()
        self._sync_horizontal_scrollbar()
        self._tracks.update()

    def _set_initial_zoom(self) -> None:
        """Open with an Edit-style scrollable viewport, anchored at frame 0."""
        width = max(80.0, float(self._tracks.width()))
        self._mapper.pixels_per_frame = min(
            _MAX_PPF,
            max(_MIN_PPF, width * _INITIAL_TIMELINE_VIEWPORTS / self._duration),
        )
        self._mapper.scroll_offset = 0.0
        self._sync_zoom_slider()
        self._sync_horizontal_scrollbar()
        self._tracks.update()

    def center_playhead(self) -> None:
        # Kept as a compatibility slot for callers of the previous panel API.
        # The Edit-style timeline always keeps frame zero fixed at the split.
        self._mapper.scroll_offset = 0.0
        self._sync_horizontal_scrollbar()
        self._tracks.update()

    def _zoom(self, factor: float) -> None:
        self._set_zoom_ppf(self._mapper.pixels_per_frame * factor)

    # ── selection ────────────────────────────────────────────────────────────

    def _select_element_row(self, row: MgKeyframesRow) -> None:
        self._selected_rows.clear()
        # The window will not echo "keyframes"-source selections back, so the
        # panel updates its own highlight before rebuilding.
        self._selected_elements = frozenset({row.element_id})
        self.element_selected.emit(row.element_id)
        self._rebuild()

    def _select_property_row(self, row: MgKeyframesRow, additive: bool) -> None:
        if additive:
            if row.row_id in self._selected_rows:
                self._selected_rows.discard(row.row_id)
            else:
                self._selected_rows.add(row.row_id)
        else:
            self._selected_rows = {row.row_id}
        self._selected_elements = frozenset({row.element_id})
        self.element_selected.emit(row.element_id)
        self._rebuild()

    def _select_point(self, row: MgKeyframesRow, frame: int,
                      modifiers: Qt.KeyboardModifier) -> None:
        key = (row.row_id, frame)
        if modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            if key in self._point_selection:
                self._point_selection.discard(key)
            else:
                self._point_selection.add(key)
        else:
            self._point_selection = {key}
        for candidate in self._rows:
            for point in candidate.points:
                point.selected = (candidate.row_id, point.frame) in self._point_selection
        self._refresh_action_states()
        self._tree.update()
        self._tracks.update()

    def _apply_point_selection_flags(self) -> None:
        for row in self._rows:
            for point in row.points:
                point.selected = (row.row_id, point.frame) in self._point_selection

    def _clear_point_selection(self) -> None:
        self._point_selection.clear()
        self._apply_point_selection_flags()
        self._refresh_action_states()
        self._tree.update()
        self._tracks.update()

    # ── editing (all paths funnel into _emit_edits → one undo step) ─────────

    def _effective_track(self, row: MgKeyframesRow) -> KeyframeTrack | None:
        if self._scene is None:
            return None
        track, _source = effective_tracks(self._scene, row.element_id).get(
            row.property_name, (None, ""))
        return track

    def _sampled_value(self, row: MgKeyframesRow, frame: int) -> float:
        scene = self._scene
        if scene is None:
            return 0.0
        element = scene.element_by_id(row.element_id)
        if element is None:
            return 0.0
        static = static_value_for(element, row.property_name)
        track = self._effective_track(row)
        if track is None:
            return static
        return float(track.get_value(frame, default=static))

    def _emit_edits(self, edits: list[tuple[str, str, KeyframeTrack | None]]) -> None:
        if edits:
            self.tracks_changed.emit(edits)

    def _toggle_keyframe_for_row(self, row: MgKeyframesRow) -> None:
        if row.kind != "property" or not row.editable or self._scene is None:
            return
        track = self._effective_track(row)
        if any(point.frame == self._current_frame for point in row.points):
            new_track = track_with_keyframes_removed(track, [self._current_frame])
        else:
            value = self._sampled_value(row, self._current_frame)
            new_track = track_with_keyframe_added(
                track, self._current_frame, value, row.property_name)
        self._emit_edits([(row.element_id, row.property_name, new_track)])

    def _toggle_keyframe_at_playhead(self) -> None:
        rows = self._keyframe_target_rows()
        if not rows:
            return
        # All target rows toggle together; rows that already have a keyframe
        # at the playhead remove it, the others add one (AE summary behaviour).
        edits: list[tuple[str, str, KeyframeTrack | None]] = []
        for row in rows:
            track = self._effective_track(row)
            if any(point.frame == self._current_frame for point in row.points):
                edits.append((row.element_id, row.property_name,
                              track_with_keyframes_removed(track, [self._current_frame])))
            else:
                edits.append((row.element_id, row.property_name, track_with_keyframe_added(
                    track, self._current_frame,
                    self._sampled_value(row, self._current_frame), row.property_name)))
        self._emit_edits(edits)

    def add_keyframe_at(self, row: MgKeyframesRow, frame: int) -> None:
        if not row.editable:
            return
        frame = max(0, min(int(frame), self._duration - 1))
        new_track = track_with_keyframe_added(
            self._effective_track(row), frame, self._sampled_value(row, frame),
            row.property_name)
        self._point_selection = {(row.row_id, frame)}
        self._emit_edits([(row.element_id, row.property_name, new_track)])

    def delete_selected(self) -> None:
        if not self._point_selection:
            return
        by_row: dict[str, list[int]] = {}
        for row_id, frame in self._point_selection:
            by_row.setdefault(row_id, []).append(frame)
        edits: list[tuple[str, str, KeyframeTrack | None]] = []
        for row_id, frames in by_row.items():
            row = self._row_by_id(row_id)
            if row is None or not row.editable:
                continue
            edits.append((row.element_id, row.property_name,
                          track_with_keyframes_removed(self._effective_track(row), frames)))
        self._point_selection.clear()
        self._emit_edits(edits)

    def set_selected_interpolation(self, mode: InterpolationType) -> None:
        if not self._point_selection:
            return
        by_row: dict[str, list[int]] = {}
        for row_id, frame in self._point_selection:
            by_row.setdefault(row_id, []).append(frame)
        edits: list[tuple[str, str, KeyframeTrack | None]] = []
        for row_id, frames in by_row.items():
            row = self._row_by_id(row_id)
            track = self._effective_track(row) if row is not None else None
            if row is None or track is None or not row.editable:
                continue
            edits.append((row.element_id, row.property_name,
                          track_with_interpolation(track, frames, mode)))
        self._emit_edits(edits)

    def edit_keyframe_value(self, row: MgKeyframesRow, frame: int) -> None:
        """Edit one numeric keyframe value while preserving curve metadata."""

        if (self._scene is None or row.kind != "property" or not row.editable):
            return
        track = self._effective_track(row)
        keyframe = track.get_keyframe_at(int(frame)) if track is not None else None
        if keyframe is None:
            return
        try:
            current = float(keyframe.value)
            minimum = float(track.min_val) if track is not None else -1_000_000.0
            maximum = float(track.max_val) if track is not None else 1_000_000.0
        except (TypeError, ValueError):
            return
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        value, accepted = QInputDialog.getDouble(
            self,
            _tr("edit_value_title", "Edit Keyframe Value"),
            _property_label(row.property_name),
            max(minimum, min(maximum, current)), minimum, maximum, 6,
        )
        if not accepted:
            return
        self._point_selection = {(row.row_id, int(frame))}
        self._emit_edits([(
            row.element_id, row.property_name,
            track_with_keyframe_value(track, frame, value),
        )])

    def copy_selected(self) -> None:
        if not self._point_selection or self._scene is None:
            return
        entries: list[tuple[str, str, int, Keyframe]] = []
        frames = sorted(frame for _row_id, frame in self._point_selection)
        anchor = frames[0]
        for row_id, frame in self._point_selection:
            row = self._row_by_id(row_id)
            track = self._effective_track(row) if row is not None else None
            if row is None or track is None:
                continue
            keyframe = track.get_keyframe_at(frame)
            if keyframe is None:
                continue
            entries.append((row.element_id, row.property_name, frame - anchor, keyframe))
        self._clipboard = entries

    @property
    def can_paste(self) -> bool:
        return bool(self._clipboard)

    def paste_clipboard(self) -> None:
        if not self._clipboard:
            return
        edits: dict[tuple[str, str], KeyframeTrack] = {}
        new_selection: set[tuple[str, int]] = set()
        for element_id, property_name, relative, keyframe in self._clipboard:
            row = next((r for r in self._rows
                        if r.kind == "property"
                        and r.element_id == element_id
                        and r.property_name == property_name), None)
            if row is None or not row.editable:
                continue
            frame = max(0, min(self._current_frame + relative, self._duration - 1))
            base = edits.get((element_id, property_name), self._effective_track(row))
            edits[(element_id, property_name)] = track_with_keyframe_copied(
                base, keyframe, frame, property_name)
            new_selection.add((row.row_id, frame))
        if not edits:
            return
        self._point_selection = new_selection
        self._emit_edits([
            (element_id, property_name, track)
            for (element_id, property_name), track in edits.items()
        ])

    def select_all_points(self) -> None:
        self._point_selection = {
            (row.row_id, point.frame) for row in self._rows for point in row.points
        }
        self._apply_point_selection_flags()
        self._refresh_action_states()
        self._tree.update()
        self._tracks.update()

    # …drag plumbing lives on _TrackSide; the panel owns shared state so both
    # halves read the same mapper/scroll/frame.


class _ToolLabel(QWidget):
    """Plain non-interactive label painted without the global QLabel styling."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self._text) + 8, 20)

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setPen(QColor(C.TEXT_HINT))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                         self._text)
        painter.end()


class _TreeSide(QWidget):
    """Self-painted left half: element rows, property rows, summary diamonds."""

    def __init__(self, panel: MgKeyframesPanel) -> None:
        super().__init__(panel)
        self.setObjectName("mgKeyframesTree")
        self._panel = panel
        self.setMinimumWidth(220)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        panel = self._panel
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(EDIT_COLORS["timeline_canvas_bg"]))
        # Paint rows only in the body.  Without this clip, a negative
        # vertical scroll can let the first row overwrite the shared header
        # band, which makes the layer name appear inside the ruler row and
        # hides the time display.
        painter.save()
        painter.setClipRect(QRect(0, _RULER_HEIGHT, self.width(),
                                  max(0, self.height() - _RULER_HEIGHT)))
        playhead_frame = panel._current_frame
        for index, row in enumerate(panel._rows):
            top = panel._row_y(index)
            if top + _ROW_HEIGHT < _RULER_HEIGHT or top > self.height():
                continue
            self._paint_row(painter, row, top, playhead_frame)
        painter.restore()

        # Header band aligned with the ruler height on the right half.  Draw
        # it after the body so it remains visually authoritative at every
        # scroll position.
        header = QRect(0, 0, self.width(), _RULER_HEIGHT)
        painter.fillRect(header, QColor(EDIT_COLORS["ruler_bg"]))
        timecode_font = QFont(
            "SF Mono, Menlo, Consolas, Courier New, monospace", 17,
            QFont.Weight.DemiBold,
        )
        timecode_font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(timecode_font)
        painter.setPen(QColor(EDIT_COLORS["timecode_panel_text"]))
        painter.drawText(header, Qt.AlignmentFlag.AlignCenter, panel.time_display)
        painter.setPen(QColor(EDIT_COLORS["timecode_panel_bottom_line"]))
        painter.drawLine(0, _RULER_HEIGHT - 1, self.width(), _RULER_HEIGHT - 1)
        painter.setPen(QColor(EDIT_COLORS["border_subtle"]))
        painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        painter.end()

    def _paint_row(self, painter: QPainter, row: MgKeyframesRow,
                   top: float, playhead_frame: int) -> None:
        panel = self._panel
        rect = QRectF(0.0, top, float(self.width() - 1), float(_ROW_HEIGHT))
        highlighted = row.selected or row.row_id in panel._selected_rows
        if highlighted:
            painter.fillRect(rect, QColor(EDIT_COLORS["track_header_bg_selected"]))
        elif row.kind == "property":
            painter.fillRect(rect, QColor(255, 255, 255, 4))
        indent = 8 + row.depth * 14
        center_y = top + _ROW_HEIGHT / 2.0

        if row.kind == "element":
            painter.setPen(
                QColor(EDIT_COLORS["accent"])
                if row.expanded else QColor(EDIT_COLORS["text_muted"])
            )
            arrow = "▾" if row.expanded else "▸"
            painter.drawText(
                QRectF(indent, top, 12.0, float(_ROW_HEIGHT)),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, arrow)
            painter.setPen(QColor(EDIT_COLORS["text"]))
            painter.drawText(
                QRectF(indent + 14, top, self.width() - _KEYFRAME_COLUMN - indent - 26,
                       float(_ROW_HEIGHT)),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                _elide(row.label))
            return
        if row.kind == "page":
            painter.setPen(QColor(EDIT_COLORS["text"]))
            arrow = "▾" if row.expanded else "▸"
            active = "● " if panel._page_row_active(row) else "  "
            painter.drawText(
                QRectF(indent, top, self.width() - _KEYFRAME_COLUMN - indent - 10,
                       float(_ROW_HEIGHT)),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                f"{arrow} {active}{row.label}")
            return
        if row.kind == "background":
            painter.setPen(QColor(EDIT_COLORS["text_muted"]))
            marker = "● " if row.background_value.strip() else "○ "
            label = f"{marker}{row.label}"
            if row.page_id and not row.background_value.strip():
                label += "  · Global"
            painter.drawText(
                QRectF(indent + 14, top, self.width() - _KEYFRAME_COLUMN - indent - 26,
                       float(_ROW_HEIGHT)),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                _elide(label),
            )
            return

        # Property row: name + preset badge + summary diamond column.
        painter.setPen(QColor(EDIT_COLORS["text"]))
        painter.drawText(
            QRectF(indent, top, self.width() - _KEYFRAME_COLUMN - indent - 46,
                   float(_ROW_HEIGHT)),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            _elide(_property_label(row.property_name)))
        if row.track_source == "local":
            badge = _tr("preset_badge", "preset")
            painter.setPen(QColor(EDIT_COLORS["text_muted"]))
            painter.drawText(
                QRectF(self.width() - _KEYFRAME_COLUMN - 44, top, 40.0, float(_ROW_HEIGHT)),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, badge)
        diamond_cx = self.width() - _KEYFRAME_COLUMN / 2.0 - 4
        if row.has_track:
            at_playhead = any(point.frame == playhead_frame for point in row.points)
            painter.setPen(QPen(QColor(EDIT_COLORS["accent"]), 1.2))
            if at_playhead:
                painter.setBrush(QColor(EDIT_COLORS["accent"]))
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(_diamond_polygon(diamond_cx, center_y, 4.0))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        panel = self._panel
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        y = event.position().y()
        if y < _RULER_HEIGHT:
            return
        row = panel._row_at_y(y)
        if row is None:
            panel._selected_rows.clear()
            panel._rebuild()
            return
        x = event.position().x()
        if x < _EXPAND_ZONE and row.kind in {"page", "element"}:
            if row.kind == "page":
                panel._toggle_page_expanded(row.page_id)
            else:
                panel._toggle_expanded(row.element_id)
            return
        if row.kind == "page":
            panel._seek_to_page_row(row)
            return
        if row.kind == "background":
            return
        if x > self.width() - _KEYFRAME_COLUMN and row.kind == "property":
            panel._toggle_keyframe_for_row(row)
            return
        additive = bool(
            event.modifiers()
            & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier))
        if row.kind == "property":
            panel._select_property_row(row, additive)
        else:
            panel._select_element_row(row)

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        panel = self._panel
        steps = event.angleDelta().y() or event.pixelDelta().y()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.25 if steps > 0 else 1 / 1.25
            panel._set_zoom_ppf(panel._mapper.pixels_per_frame * factor)
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            if steps:
                panel.scroll_by(-steps / 2.0)
        elif steps:
            # Same direction convention as Edit's TimelineView: wheel motion
            # pans the time ruler horizontally; Shift remains vertical.
            panel.scroll_horizontally(-steps)
        event.accept()

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        # AUD-30800: match the user-configurable play/pause shortcut (the
        # panel QShortcut applies the same sequence), never a hardcoded Space
        # that would survive a user rebinding.
        if event_matches(event, PLAY_PAUSE_ACTION_ID, PLAY_PAUSE_DEFAULT):
            self._panel.playbackToggleRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def _elide(text: str, limit: int = 28) -> str:
    """Cheap label elision (rows are short; font metrics not critical)."""

    clean = text.replace("\n", " ")
    return clean if len(clean) <= limit else clean[: limit - 2] + "…"


class _TrackSide(QWidget):
    """Self-painted right half: ruler, page spans, keyframe diamonds, playhead."""

    def __init__(self, panel: MgKeyframesPanel) -> None:
        super().__init__(panel)
        self.setObjectName("mgKeyframesTracks")
        self._panel = panel
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._playhead_drag = False
        self._last_seek_ms = 0.0
        self._drag: _KeyframeDrag | None = None
        self._marquee: tuple[float, float, float, float] | None = None
        self._marquee_origin_selection: set[tuple[str, int]] = set()
        self._panning = False
        self._pan_start_x = 0.0
        self._pan_start_scroll = 0.0

    # ── coordinate helpers ───────────────────────────────────────────────────

    def _frame_at(self, x: float) -> int:
        frame = self._panel._mapper.x_to_frame(x)
        return int(round(frame))

    def _hit_keyframe(self, row: MgKeyframesRow, x: float, y: float) -> int | None:
        drag = self._drag
        # AUD-30802: the row's vertical center never varies per point —
        # compute it once instead of rescanning the row table per point.
        row_center = self._row_center(row)
        for point in row.points:
            frame = point.frame
            if drag is not None and point.selected and row.row_id in drag.rows:
                frame += drag.applied_delta
            px = self._panel._mapper.frame_to_x(frame)
            if abs(px - x) <= _HIT_RADIUS and abs(row_center - y) <= _ROW_HEIGHT / 2:
                return frame
        return None

    def _row_center(self, row: MgKeyframesRow) -> float:
        index = self._panel._row_index_by_id.get(row.row_id, 0)
        return self._panel._row_y(index) + _ROW_HEIGHT / 2.0

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        """Re-clamp selection scrolling after the splitter lays out the view.

        Scene selection can be applied before the splitter has a real
        viewport height.  In that window ``_ensure_selected_visible`` may
        temporarily scroll the selected row; once the viewport is laid out,
        the valid range can shrink back to zero.  Re-clamping here keeps the
        first layer row in the body instead of above the ruler.
        """

        super().resizeEvent(event)
        panel = self._panel
        previous = panel._v_scroll
        panel._clamp_scroll()
        if panel._v_scroll != previous:
            panel._tree.update()
            self.update()
        panel._sync_horizontal_scrollbar()

    # ── painting ─────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        panel = self._panel
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(EDIT_COLORS["timeline_canvas_bg"]))
        self._paint_grid(painter)
        panel_duration = panel._duration
        # Shade frames beyond the scene duration.
        end_x = panel._mapper.frame_to_x(panel_duration)
        if end_x < self.width():
            painter.fillRect(
                QRectF(end_x, _RULER_HEIGHT, self.width() - end_x,
                       self.height() - _RULER_HEIGHT),
                QColor(0, 0, 0, 90))
        # Keep track rows below the ruler.  This is especially important when
        # scrolling: a row may be partially above the body, but must never
        # paint over the ruler labels or ticks.
        painter.save()
        painter.setClipRect(QRect(0, _RULER_HEIGHT, self.width(),
                                  max(0, self.height() - _RULER_HEIGHT)))
        for index, row in enumerate(panel._rows):
            top = panel._row_y(index)
            if top + _ROW_HEIGHT < _RULER_HEIGHT or top > self.height():
                continue
            self._paint_row(painter, row, top)
        if self._marquee is not None:
            x0, y0, x1, y1 = self._marquee
            rect = QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            painter.setPen(QPen(QColor(C.ORANGE), 1.0, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(255, 136, 0, 24))
            painter.drawRect(rect)
        painter.restore()
        # Draw the ruler after the body clip so its header stays clean and
        # aligned with the tree-side header.
        self._paint_ruler(painter)
        self._paint_playhead(painter)
        painter.end()

    def _paint_grid(self, painter: QPainter) -> None:
        """Paint Edit's quiet major/minor vertical time grid in the body."""

        panel = self._panel
        ppf = panel._mapper.pixels_per_frame
        if ppf < 0.2:
            return
        fps = max(panel._fps, 0.001)
        pixels_per_second = ppf * fps
        if pixels_per_second > 80:
            major_interval = max(1, round(fps))
            minor_interval = major_interval
        elif pixels_per_second > 30:
            major_interval = max(1, round(5 * fps))
            minor_interval = max(1, round(fps))
        elif pixels_per_second > 10:
            major_interval = max(1, round(10 * fps))
            minor_interval = max(1, round(5 * fps))
        else:
            major_interval = max(1, round(30 * fps))
            minor_interval = max(1, round(10 * fps))

        last_frame = max(0, int(panel._mapper.x_to_frame(self.width())) + 1)
        if pixels_per_second > 8:
            painter.setPen(QPen(QColor(70, 74, 82, 22), 1))
            for frame in range(0, last_frame + 1, minor_interval):
                if frame % major_interval == 0:
                    continue
                painter.drawLine(
                    QPointF(panel._mapper.frame_to_x(frame), _RULER_HEIGHT),
                    QPointF(panel._mapper.frame_to_x(frame), self.height()),
                )
        painter.setPen(QPen(QColor(90, 95, 105, 42), 1))
        for frame in range(0, last_frame + 1, major_interval):
            x = panel._mapper.frame_to_x(frame)
            painter.drawLine(QPointF(x, _RULER_HEIGHT), QPointF(x, self.height()))

    def _calculate_tick_interval(self, ppf: float, fps: float) -> int:
        fps_int = max(1, round(fps))
        candidates = [1, 2, 5, 10, 15]
        candidates.extend([
            fps_int // 2, fps_int, fps_int * 2, fps_int * 5,
            fps_int * 10, fps_int * 30, fps_int * 60, fps_int * 300,
            fps_int * 600,
        ])
        for step in sorted(set(max(1, value) for value in candidates)):
            if step * ppf >= EDIT_SIZES["ruler_target_minor_min_px"]:
                return step
        return max(1, fps_int * 600)

    def _calculate_major_tick_interval(self, ppf: float, fps: float) -> int:
        fps_int = max(1, round(fps))
        candidates = [
            1, 2, 5, 10, fps_int // 2, fps_int, fps_int * 2,
            fps_int * 5, fps_int * 10, fps_int * 30, fps_int * 60,
            fps_int * 300, fps_int * 600,
        ]
        for step in sorted(set(max(1, value) for value in candidates)):
            if step * ppf >= EDIT_SIZES["ruler_target_major_min_px"]:
                return step
        return max(1, fps_int * 600)

    def _calculate_medium_tick_interval(
        self, ppf: float, major_step: int, minor_step: int,
    ) -> int:
        if major_step <= minor_step:
            return major_step
        best = major_step
        for divisor in (2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30):
            candidate = max(1, major_step // divisor)
            px = candidate * ppf
            if EDIT_SIZES["ruler_target_minor_min_px"] <= px <= EDIT_SIZES["ruler_target_minor_max_px"]:
                best = candidate
                break
            if px >= EDIT_SIZES["ruler_target_minor_min_px"] and best == major_step:
                best = candidate
        if major_step % best != 0:
            best = max(1, major_step // 2)
        return max(minor_step, best)

    @staticmethod
    def _format_timecode_compact(frame: int, fps: float, ppf: float,
                                 major_step: int) -> str:
        """Use the same compact ruler labels as Edit's TimecodeRulerItem."""

        fps_int = max(1, round(fps))
        total = max(0, int(frame))
        hours, remainder = divmod(total, 3600 * fps_int)
        minutes, remainder = divmod(remainder, 60 * fps_int)
        seconds, frames = divmod(remainder, fps_int)
        major_px = major_step * ppf
        if major_px >= 400:
            if hours:
                return f"{hours}:{minutes:02d}:{seconds:02d}:{frames:02d}"
            if minutes:
                return f"{minutes}:{seconds:02d}:{frames:02d}"
            return f"{seconds:02d}:{frames:02d}"
        if major_px >= 150:
            if hours:
                return f"{hours}:{minutes:02d}:{seconds:02d}:{frames:02d}"
            if minutes:
                return f"{minutes}:{seconds:02d}:{frames:02d}"
            return f"{seconds:02d}:{frames:02d}"
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        if minutes:
            return f"{minutes}:{seconds:02d}"
        return f"00:{seconds:02d}"

    def _paint_ruler(self, painter: QPainter) -> None:
        panel = self._panel
        mapper = panel._mapper
        ppf = mapper.pixels_per_frame
        fps = max(panel._fps, 0.001)
        minor_step = self._calculate_tick_interval(ppf, fps)
        major_step = self._calculate_major_tick_interval(ppf, fps)
        medium_step = self._calculate_medium_tick_interval(ppf, major_step, minor_step)

        painter.fillRect(
            QRect(0, 0, self.width(), _RULER_HEIGHT),
            QColor(EDIT_COLORS["ruler_bg"]),
        )
        font = QFont("SF Mono, Menlo, Consolas, Courier New, monospace", 9)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)

        tick_top = 2
        label_top = 18
        major_h = EDIT_SIZES["ruler_major_tick_h"]
        medium_h = EDIT_SIZES["ruler_medium_tick_h"]
        minor_h = EDIT_SIZES["ruler_minor_tick_h"]
        last_frame = max(0, int(mapper.x_to_frame(self.width())) + major_step * 2)
        last_label_right = float("-inf")
        frame = 0
        while frame <= last_frame:
            x = mapper.frame_to_x(frame)
            if frame % major_step == 0:
                painter.setPen(QPen(QColor(EDIT_COLORS["ruler_major_tick"]), 1))
                painter.drawLine(QPointF(x, tick_top), QPointF(x, tick_top + major_h))
                text = self._format_timecode_compact(frame, fps, ppf, major_step)
                text_width = painter.fontMetrics().horizontalAdvance(text) + 4
                text_x = x + 4
                if text_x > last_label_right + 76:
                    painter.setPen(QPen(QColor(EDIT_COLORS["ruler_text"])))
                    painter.drawText(
                        QRectF(text_x, label_top, text_width + 20, 14),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        text,
                    )
                    last_label_right = text_x + text_width
            elif frame % medium_step == 0:
                painter.setPen(QPen(QColor(EDIT_COLORS["ruler_medium_tick"]), 1))
                painter.drawLine(QPointF(x, tick_top), QPointF(x, tick_top + medium_h))
            else:
                painter.setPen(QPen(QColor(EDIT_COLORS["ruler_minor_tick"]), 1))
                painter.drawLine(QPointF(x, tick_top), QPointF(x, tick_top + minor_h))
            frame += minor_step

        scene = panel._scene
        if scene is not None:
            painter.setPen(QPen(QColor(EDIT_COLORS["accent"]), 1.4))
            for marker in scene.markers:
                x = mapper.frame_to_x(marker.frame)
                if -4 <= x <= self.width() + 4:
                    painter.drawLine(
                        QPointF(x, _RULER_HEIGHT - 12),
                        QPointF(x, _RULER_HEIGHT),
                    )
        painter.setPen(QPen(QColor(EDIT_COLORS["ruler_bottom_line"]), 1))
        painter.drawLine(0, _RULER_HEIGHT - 1, self.width(), _RULER_HEIGHT - 1)

    def _paint_row(self, painter: QPainter, row: MgKeyframesRow, top: float) -> None:
        panel = self._panel
        mapper = panel._mapper
        center_y = top + _ROW_HEIGHT / 2.0
        if row.kind == "page":
            for index, (start, end) in enumerate(panel._page_spans_for_row(row)):
                x0 = mapper.frame_to_x(start)
                x1 = mapper.frame_to_x(end + 1)
                rect = QRectF(x0, center_y - 6.0, max(2.0, x1 - x0), 12.0)
                active = panel._page_row_active(row)
                fill = QColor(92, 96, 110, 70)
                border = QColor(157, 162, 172)
                if active:
                    fill = QColor(EDIT_COLORS["accent"])
                    fill.setAlpha(90)
                    border = QColor(EDIT_COLORS["accent"])
                painter.setBrush(fill)
                painter.setPen(QPen(border, 1.0))
                painter.drawRoundedRect(rect, 3.0, 3.0)
                # A short bright edge makes the duration handle discoverable
                # without adding another button to the page row.
                painter.setPen(QPen(border, 1.6))
                painter.drawLine(
                    QPointF(x1, center_y - 5.0),
                    QPointF(x1, center_y + 5.0),
                )
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(
                    QColor(C.TEXT_PRIMARY) if active else QColor(C.TEXT_HINT))
                painter.drawText(
                    rect.adjusted(5, 0, -3, 0),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    _tr("page_segment", "P{n}", n=index + 1))
            return
        if row.kind == "background":
            for start, end in row.page_spans:
                x0 = mapper.frame_to_x(start)
                x1 = mapper.frame_to_x(end + 1)
                rect = QRectF(x0, center_y - 5.0, max(2.0, x1 - x0), 10.0)
                fill = QColor(74, 78, 90, 72)
                border = QColor(115, 120, 132, 150)
                if row.background_value.strip():
                    fill = QColor(118, 122, 135, 100)
                painter.setBrush(fill)
                painter.setPen(QPen(border, 1.0))
                painter.drawRoundedRect(rect, 3.0, 3.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            return
        if row.kind == "element":
            return
        # Property track: mirror the tree-side row highlight so the selected
        # property is readable on the track half too.
        if row.row_id in panel._selected_rows:
            painter.fillRect(
                QRectF(0.0, top, float(self.width()), float(_ROW_HEIGHT)),
                QColor(42, 42, 48, 110))
        # Baseline over [0, duration) then the diamonds.
        x0 = mapper.frame_to_x(0)
        x1 = mapper.frame_to_x(panel._duration)
        painter.setPen(QPen(QColor(52, 54, 62), 2.0))
        painter.drawLine(QPointF(x0, center_y), QPointF(min(x1, float(self.width())), center_y))
        drag = self._drag
        dragging_row = drag is not None and row.row_id in drag.rows

        def _draw_diamond(frame: int, selected: bool) -> None:
            x = mapper.frame_to_x(frame)
            if x < -_DIAMOND or x > self.width() + _DIAMOND:
                return
            if selected:
                painter.setBrush(QColor(EDIT_COLORS["accent"]))
                painter.setPen(QPen(QColor(EDIT_COLORS["accent_hover"]), 1.0))
            else:
                painter.setBrush(QColor(190, 192, 198))
                painter.setPen(QPen(QColor(106, 110, 118), 1.0))
            painter.drawPolygon(_diamond_polygon(x, center_y))

        for point in row.points:
            if dragging_row and point.selected:
                if drag.duplicated:
                    # Alt-drag: the original stays visible in place while the
                    # dragged copy moves, so the gesture reads as a copy.
                    _draw_diamond(point.frame, False)
                _draw_diamond(point.frame + drag.applied_delta, True)
            else:
                _draw_diamond(point.frame, point.selected)

    def _paint_playhead(self, painter: QPainter) -> None:
        panel = self._panel
        x = panel._mapper.frame_to_x(panel._current_frame)
        if x < -1 or x > self.width() + 1:
            return
        painter.setPen(QPen(QColor(EDIT_COLORS["playhead"]), 1.4))
        painter.drawLine(QPointF(x, 12.0), QPointF(x, float(self.height())))
        cap = QPolygonF([
            QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 9.0),
        ])
        painter.setBrush(QColor(EDIT_COLORS["playhead"]))
        painter.drawPolygon(cap)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    # ── interaction ──────────────────────────────────────────────────────────

    def _page_resize_hit(self, row: MgKeyframesRow, x: float) -> bool:
        """Return whether ``x`` is on the draggable right edge of a page bar."""
        if row.kind != "page":
            return False
        spans = self._panel._page_spans_for_row(row)
        if not spans:
            return False
        edge_x = self._panel._mapper.frame_to_x(spans[0][1] + 1)
        return abs(float(x) - edge_x) <= 7.0

    def _update_page_resize_cursor(self, pos: QPointF) -> None:
        row = self._panel._row_at_y(pos.y())
        if row is not None and self._page_resize_hit(row, pos.x()):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def _begin_page_resize(self, row: MgKeyframesRow) -> None:
        spans = self._panel._page_spans_for_row(row)
        if not spans:
            return
        start, end = spans[0]
        self._panel._page_resize = _PageResizeDrag(
            page_id=row.page_id,
            start_frame=start,
            original_duration=max(1, end - start + 1),
            duration_frames=max(1, end - start + 1),
        )

    def _do_page_resize(self, x: float) -> None:
        drag = self._panel._page_resize
        if drag is None:
            return
        boundary_frame = max(drag.start_frame + 1, self._frame_at(x))
        drag.duration_frames = max(1, boundary_frame - drag.start_frame)
        self._panel._tree.update()
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        panel = self._panel
        self.setFocus()
        pos = event.position()
        if pos.y() < _RULER_HEIGHT:
            if event.button() == Qt.MouseButton.LeftButton:
                self._playhead_drag = True
                self.grabMouse()
                self._seek_to(pos.x(), force=True)
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start_x = pos.x()
            self._pan_start_scroll = panel._mapper.scroll_offset
            self.grabMouse()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        row = panel._row_at_y(pos.y())
        frame = self._frame_at(pos.x())
        if row is not None and row.kind == "page" and self._page_resize_hit(row, pos.x()):
            self._begin_page_resize(row)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.grabMouse()
            return
        if row is not None and row.kind == "property" and row.editable:
            hit = self._hit_keyframe(row, pos.x(), pos.y())
            if hit is not None:
                panel._select_point(row, hit, event.modifiers())
                self._begin_keyframe_drag(pos.x(), event.modifiers())
                self.grabMouse()
                return
            if 0 <= frame < panel._duration:
                # Empty track space: marquee (or plain click → seek) below.
                self._marquee = (pos.x(), pos.y(), pos.x(), pos.y())
                self._marquee_origin_selection = set(panel._point_selection)
                self.grabMouse()
                return
        # Everywhere else (element rows, beyond duration): seek.
        self._playhead_drag = True
        self.grabMouse()
        self._seek_to(pos.x(), force=True)

    def _begin_keyframe_drag(self, x: float, modifiers: Qt.KeyboardModifier) -> None:
        panel = self._panel
        drag = _KeyframeDrag(start_x=x)
        for row in panel._rows:
            frames = [point.frame for point in row.points if point.selected]
            if frames:
                drag.rows[row.row_id] = frames
        drag.duplicated = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        self._drag = drag

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        panel = self._panel
        pos = event.position()
        if panel._page_resize is not None:
            self._do_page_resize(pos.x())
            return
        if self._playhead_drag:
            self._seek_to(pos.x())
            return
        if self._panning:
            # Frame zero is a fixed anchor, so horizontal hand-panning is
            # intentionally disabled.  Keep the state machine alive so a
            # middle-button release remains well-defined.
            panel._mapper.scroll_offset = 0.0
            self.update()
            return
        if self._drag is not None:
            self._do_keyframe_drag(pos.x())
            return
        if self._marquee is not None:
            x0, y0, _x1, _y1 = self._marquee
            self._marquee = (x0, y0, pos.x(), pos.y())
            if (abs(pos.x() - x0) > _MARQUEE_THRESHOLD
                    or abs(pos.y() - y0) > _MARQUEE_THRESHOLD):
                self._apply_marquee_selection()
            self.update()
            return
        self._update_page_resize_cursor(pos)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        panel = self._panel
        self.releaseMouse()
        if panel._page_resize is not None:
            drag = panel._page_resize
            panel._page_resize = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if drag.duration_frames != drag.original_duration:
                panel.pageDurationChangeRequested.emit(
                    drag.page_id, drag.duration_frames,
                )
            panel._tree.update()
            self.update()
            return
        if self._playhead_drag:
            self._playhead_drag = False
            self._seek_to(event.position().x(), force=True)
            return
        if self._panning:
            self._panning = False
            return
        if self._drag is not None:
            self._finish_keyframe_drag()
            return
        if self._marquee is not None:
            x0, y0, x1, y1 = self._marquee
            self._marquee = None
            if (abs(x1 - x0) <= _MARQUEE_THRESHOLD and abs(y1 - y0) <= _MARQUEE_THRESHOLD):
                # Plain click on empty track: seek there and clear selection.
                panel._clear_point_selection()
                self._seek_to(x0, force=True)
            self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        panel = self._panel
        pos = event.position()
        if pos.y() < _RULER_HEIGHT or event.button() != Qt.MouseButton.LeftButton:
            return
        row = panel._row_at_y(pos.y())
        frame = self._frame_at(pos.x())
        if row is None or row.kind != "property" or not row.editable:
            return
        hit = self._hit_keyframe(row, pos.x(), pos.y())
        if hit is not None:
            # The preceding press may have started a zero-distance drag.
            # Cancel it before opening the value editor so the trailing
            # release cannot emit a second edit.
            self._drag = None
            self._marquee = None
            panel.edit_keyframe_value(row, hit)
        elif 0 <= frame < panel._duration:
            # Qt delivers press/release/press/dblclick/release: the second
            # press opened a marquee.  Cancel it so the trailing release does
            # not treat the double-click as a plain seek that clears the new
            # keyframe's selection.
            self._marquee = None
            panel.add_keyframe_at(row, frame)

    def _seek_to(self, x: float, *, force: bool = False) -> None:
        panel = self._panel
        frame = max(0, min(panel._duration - 1, self._frame_at(x)))
        now = time.monotonic() * 1000.0
        if not force and now - self._last_seek_ms < _SEEK_THROTTLE_MS:
            panel.set_current_frame(frame)  # keep the visual head responsive
            return
        self._last_seek_ms = now
        panel._current_frame = frame
        panel.frame_changed.emit(frame)
        panel._tree.update()
        self.update()

    def _do_keyframe_drag(self, x: float) -> None:
        drag = self._drag
        if drag is None:
            return
        delta = self._frame_at(x) - self._frame_at(drag.start_x)
        if delta == drag.applied_delta:
            return
        if self._drag_delta_valid(drag, delta):
            drag.applied_delta = delta
            drag.moved = delta != 0
        self.update()

    def _drag_delta_valid(self, drag: _KeyframeDrag, delta: int) -> bool:
        """Whole-group validation: every row must stay collision-free and
        inside the duration, otherwise the move is refused entirely."""

        panel = self._panel
        for row in panel._rows:
            selected = drag.rows.get(row.row_id)
            if not selected:
                continue
            selected_set = set(selected)
            unselected = {point.frame for point in row.points
                          if point.frame not in selected_set}
            if drag.duplicated:
                # Duplicates leave the originals behind, so destinations must
                # also avoid every original (selected) frame.
                unselected |= selected_set
            for frame in selected:
                target = frame + delta
                if not (0 <= target < panel._duration):
                    return False
                if target in unselected:
                    return False
                unselected.add(target)
        return True

    def _finish_keyframe_drag(self) -> None:
        panel = self._panel
        drag = self._drag
        self._drag = None
        if drag is None or not drag.moved or drag.applied_delta == 0:
            self.update()
            return
        edits: list[tuple[str, str, KeyframeTrack | None]] = []
        new_selection: set[tuple[str, int]] = set()
        for row_id, frames in drag.rows.items():
            row = panel._row_by_id(row_id)
            track = panel._effective_track(row) if row is not None else None
            if row is None or track is None or not row.editable:
                continue
            if drag.duplicated:
                # Alt-drag: the originals stay; copies land at frame + delta.
                new_track = track
                for frame in frames:
                    original = track.get_keyframe_at(frame)
                    if original is None:
                        continue
                    new_track = track_with_keyframe_copied(
                        new_track, original, frame + drag.applied_delta,
                        row.property_name)
                    new_selection.add((row_id, frame + drag.applied_delta))
                edits.append((row.element_id, row.property_name, new_track))
            else:
                moves = [(frame, frame + drag.applied_delta) for frame in frames]
                edits.append((row.element_id, row.property_name,
                              track_with_keyframes_moved(track, moves)))
                new_selection.update((row_id, frame + drag.applied_delta) for frame in frames)
        # Keep the dragged keyframes selected across the rebuild the emit is
        # about to trigger (AE behaviour: the edit target stays highlighted).
        if new_selection:
            panel._point_selection = new_selection
        if edits:
            panel.tracks_changed.emit(edits)
        self.update()

    def _apply_marquee_selection(self) -> None:
        panel = self._panel
        assert self._marquee is not None
        x0, y0, x1, y1 = self._marquee
        left, right = min(x0, x1) - _HIT_RADIUS, max(x0, x1) + _HIT_RADIUS
        top, bottom = min(y0, y1), max(y0, y1)
        selected = set(self._marquee_origin_selection)
        for row in panel._rows:
            if row.kind != "property":
                continue
            # AUD-30802: O(1) index lookup instead of a full-table rescan
            # per row (marquee was O(rows²)).
            index = panel._row_index_by_id.get(row.row_id)
            if index is None:
                continue
            row_top = panel._row_y(index)
            if row_top + _ROW_HEIGHT < top or row_top > bottom:
                continue
            drag = self._drag
            for point in row.points:
                frame = point.frame
                if drag is not None and point.selected and row.row_id in drag.rows:
                    frame += drag.applied_delta
                x = panel._mapper.frame_to_x(frame)
                if left <= x <= right:
                    selected.add((row.row_id, point.frame))
        panel._point_selection = selected
        panel._apply_point_selection_flags()
        panel._refresh_action_states()
        panel._tree.update()

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        panel = self._panel
        steps = event.angleDelta().y() or event.pixelDelta().y()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.25 if steps > 0 else 1 / 1.25
            panel._set_zoom_ppf(panel._mapper.pixels_per_frame * factor)
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            if steps:
                panel.scroll_by(-steps / 2.0)
        else:
            if steps:
                panel.scroll_horizontally(-steps)
        event.accept()

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        panel = self._panel
        key = event.key()
        # AUD-30800: follow the configured play/pause binding instead of a
        # hardcoded Space (see _TreeSide.keyPressEvent above).
        if event_matches(event, PLAY_PAUSE_ACTION_ID, PLAY_PAUSE_DEFAULT):
            panel.playbackToggleRequested.emit()
            event.accept()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            panel.delete_selected()
            event.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End):
            if key == Qt.Key.Key_Home:
                target = 0
            elif key == Qt.Key.Key_End:
                target = panel._duration - 1
            else:
                step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
                if key == Qt.Key.Key_Left:
                    step = -step
                target = panel._current_frame + step
            target = max(0, min(target, panel._duration - 1))
            if target != panel._current_frame:
                panel._current_frame = target
                panel.frame_changed.emit(target)
                panel._tree.update()
                self.update()
            event.accept()
            return
        if key == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            panel.copy_selected()
            event.accept()
            return
        if key == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            panel.paste_clipboard()
            event.accept()
            return
        if key == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            panel.select_all_points()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        panel = self._panel
        local_pos = event.pos()
        row = panel._row_at_y(local_pos.y())
        hit = (
            self._hit_keyframe(row, local_pos.x(), local_pos.y())
            if row is not None and row.kind == "property" and row.editable
            else None
        )
        menu = QMenu(self)
        edit_action = menu.addAction(_tr("menu_edit_value", "Edit Value…"))
        edit_action.setEnabled(hit is not None)
        if row is not None and hit is not None:
            edit_action.triggered.connect(
                lambda _checked=False, target=row, target_frame=hit:
                panel.edit_keyframe_value(target, target_frame))
        menu.addSeparator()
        copy_action = menu.addAction(_tr("menu_copy", "Copy"))
        copy_action.triggered.connect(panel.copy_selected)
        copy_action.setEnabled(bool(panel._point_selection))
        paste_action = menu.addAction(_tr("menu_paste", "Paste"))
        paste_action.triggered.connect(panel.paste_clipboard)
        paste_action.setEnabled(panel.can_paste)
        delete_action = menu.addAction(_tr("menu_delete", "Delete"))
        delete_action.triggered.connect(panel.delete_selected)
        delete_action.setEnabled(bool(panel._point_selection))
        interpolation_menu = menu.addMenu(_tr("menu_interpolation", "Interpolation"))
        for mode in _INTERPOLATION_MODES:
            action = interpolation_menu.addAction(_interpolation_label(mode))
            action.triggered.connect(
                lambda _checked=False, m=mode: panel.set_selected_interpolation(m))
        interpolation_menu.setEnabled(bool(panel._point_selection))
        menu.exec(event.globalPos())


def _interpolation_label(mode: InterpolationType) -> str:
    names = {
        InterpolationType.LINEAR: ("interp_linear", "Linear"),
        InterpolationType.BEZIER: ("interp_bezier", "Bezier"),
        InterpolationType.HOLD: ("interp_hold", "Hold"),
        InterpolationType.EASE_IN: ("interp_ease_in", "Ease In"),
        InterpolationType.EASE_OUT: ("interp_ease_out", "Ease Out"),
        InterpolationType.EASE_IN_OUT: ("interp_ease_in_out", "Ease In-Out"),
    }
    key, fallback = names.get(mode, (mode.value, str(mode.value)))
    return _tr(key, fallback)


__all__ = ["MgKeyframesPanel"]
