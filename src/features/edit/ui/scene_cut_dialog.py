# -*- coding: utf-8 -*-
"""
Scene Cut Detection Dialog.

Main dialog for scene cut detection review UI.

## Layout Structure

    ┌─────────────────────────────────────────────────────────────────┐
    │  Scene Detect                                     [×] [...]    │
    ├─────────────────────────────────────────────────────────────────┤
    │  [SceneCutPreviewWidget - Three-panel preview] (~40%)           │
    │  Before │ Current │ After                                       │
    ├─────────────────────────────────────────────────────────────────┤
    │  [TransportBar - Playback controls] (~12%)                      │
    │  [Prev Cut] [<] [Play/Pause] [>] [Next Cut] [Cut] [List] [▶]   │
    ├─────────────────────────────────────────────────────────────────┤
    │  [Split area - Graph + Scene List] (~38%)                       │
    │  ┌────────────────────────────┬─────────────────────────────┐   │
    │  │ [GraphWidget]              │ [SceneList]                 │   │
    │  │ Green peaks, threshold     │ Scene | Frame | TC         │   │
    │  │ Red playhead               │                             │   │
    │  └────────────────────────────┴─────────────────────────────┘   │
    ├─────────────────────────────────────────────────────────────────┤
    │  [BottomBar - Action buttons] (~10%)                            │
    │  [Auto Detect] [Add] [Delete]  [Sensitivity: ▬▬▬▬]  [Add Cuts] │
    └─────────────────────────────────────────────────────────────────┘

## Features

1. Three-panel preview (before/current/after frames)
2. Transport bar with playback controls
3. Detection graph with peaks and playhead
4. Scene list table with editable decisions
5. Action buttons: Auto Detect, Add, Delete, Add Cuts to Media Pool
6. Keyboard shortcuts: Enter/Delete (review), Up/Down (cuts), Left/Right (frames), Space (play)
7. Batch operations: Accept high confidence, Reject warnings
8. i18n support (en_US, zh_CN)

## Signal Connections

- Uses SceneCutWorker for background detection
- UI non-blocking during detection
- Progress display in dialog

## Reference

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section 2.1 and P4
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from typing import Any, Optional, List
from collections import OrderedDict

import numpy as np

from PyQt6.QtWidgets import (
    QDialog,
    QApplication,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QFrame,
    QPushButton,
    QToolButton,
    QTableView,
    QHeaderView,
    QSplitter,
    QProgressBar,
    QMessageBox,
    QSizePolicy,
    QMenu,
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QSlider,
    QTextEdit,
)
from PyQt6.QtCore import (
    Qt,
    QEvent,
    pyqtSignal,
    QObject,
    QSize,
    QTimer,
    QThread,
    QModelIndex,
)
from PyQt6.QtGui import (
    QKeyEvent,
    QAction,
    QImage,
    QShortcut,
)

from src.features.edit.core.scene_cut.models import (
    SceneCutSettings,
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutDecision,
    SceneCutKind,
)
from src.features.edit.core.scene_cut.cache import SceneCutCache, generate_cache_key
from src.features.edit.core.scene_cut.worker import (
    SceneCutWorker,
    DetectionProgress,
    SceneCutError,
)
from src.features.edit.core.scene_cut.frame_sampler import (
    VideoFrameSampler,
)

from .scene_cut_result_model import SceneCutResultModel, Column
from . import scene_cut_shortcuts
from .scene_cut_preview_widget import SceneCutPreviewWidget
from .scene_cut_graph_widget import (
    SceneCutGraphWidget,
    ThresholdSlider,
)

from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import get_icon, IconColors
from src.features.edit.ui.edit_style import (
    EDIT_COLORS,
    button_style,
    accent_button_style,
    tool_button_style,
)


logger = logging.getLogger(__name__)


# ================================================================
# Preview Frame Provider
# ================================================================

class PreviewFrameProvider:
    """
    Provides video frames for preview display.

    Uses VideoFrameSampler to extract specific frames and converts
    them to QImage for the three-panel preview widget.

    Features:
    - LRU memory cache for decoded frames
    - Boundary clamping (frame < 0, frame >= total_frames)
    - Safe BGR->RGB->QImage conversion with lifetime safety

    Usage:
        provider = PreviewFrameProvider(source_path, settings)
        provider.open()
        before_img = provider.get_frame(frame_number - 1)
        current_img = provider.get_frame(frame_number)
        after_img = provider.get_frame(frame_number + 1)
        provider.close()
    """

    MAX_CACHE_SIZE = 300  # LRU cache capacity

    def __init__(
        self,
        source_path: str,
        settings: Optional[SceneCutSettings] = None,
    ) -> None:
        self._source_path = source_path
        self._settings = settings or SceneCutSettings()
        self._sampler: Optional[VideoFrameSampler] = None
        self._total_frames: int = 0
        self._fps: float = 24.0
        self._time_index = None  # SourceFrameTimeIndex for VFR seek
        self._lock = threading.RLock()
        # Logical next frame for the forward playback cursor.  Random seeks
        # remain supported, but consecutive playback requests use the same
        # decoder iterator instead of seeking to a keyframe for every frame.
        self._sequential_next_frame: Optional[int] = None

        # LRU cache: frame_number -> QImage
        self._cache: OrderedDict[int, QImage] = OrderedDict()

    def set_time_index(self, time_index: Optional[Any]) -> None:
        """Set SourceFrameTimeIndex for VFR-accurate seeking."""
        with self._lock:
            self._time_index = time_index

    def open(self) -> bool:
        """Open the video source. Returns True on success."""
        with self._lock:
            try:
                self._sampler = VideoFrameSampler(
                    self._source_path,
                    self._settings,
                )
                self._sampler.open()
                self._total_frames = self._sampler.total_frames
                self._fps = self._sampler.fps or 24.0
                self._sequential_next_frame = 0
                return True
            except Exception as e:
                logger.warning(f"PreviewFrameProvider: failed to open {self._source_path}: {e}")
                self._sampler = None
                self._sequential_next_frame = None
                return False

    def close(self) -> None:
        """Close and release resources."""
        with self._lock:
            if self._sampler is not None:
                try:
                    self._sampler.close()
                except Exception:
                    pass
                self._sampler = None
            self._cache.clear()
            self._sequential_next_frame = None

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def is_open(self) -> bool:
        return self._sampler is not None and self._sampler.is_open

    def get_frame(self, frame_number: int) -> Optional[QImage]:
        """
        Get a QImage for the given frame number.

        Clamps to valid range [0, total_frames-1].
        Uses LRU cache to avoid re-decoding.
        """
        with self._lock:
            if self._sampler is None or not self._sampler.is_open:
                return None

            # Boundary clamping
            clamped = max(0, min(frame_number, self._total_frames - 1))

            # Check cache
            if clamped in self._cache:
                self._cache.move_to_end(clamped)
                return self._cache[clamped]

            # Decode frame
            try:
                sampled = self._sampler.sample_single_frame(clamped, time_index=self._time_index)
                if sampled is None:
                    return None

                qimg = self._bgr_to_qimage(sampled.frame_data)
                if qimg is not None:
                    self._cache[clamped] = qimg
                    self._evict_if_needed()
                    self._sequential_next_frame = clamped + 1
                return qimg
            except Exception as e:
                logger.debug(f"PreviewFrameProvider: failed to get frame {clamped}: {e}")
                return None

    def get_preview_frames(
        self,
        cut_frame: int,
    ) -> tuple[Optional[QImage], Optional[QImage], Optional[QImage]]:
        """Get before/current/after frames for a cut point."""
        before = self.get_frame(cut_frame - 1)
        current = self.get_frame(cut_frame)
        after = self.get_frame(cut_frame + 1)
        return before, current, after

    def get_sequential_preview_frames(
        self,
        frame_number: int,
    ) -> tuple[Optional[QImage], Optional[QImage], Optional[QImage]]:
        """Get a preview triplet while advancing a forward decode cursor.

        Cached frames satisfy the before/current panels.  Only a missing
        after-frame is decoded from the existing iterator, which makes the
        next playback tick mostly a cache lookup plus one sequential decode.
        If playback resumed after a random seek, the cursor falls back to one
        exact seek and then continues sequentially again.
        """
        with self._lock:
            if self._sampler is None or not self._sampler.is_open:
                return None, None, None

            clamped = max(0, min(frame_number, self._total_frames - 1))

            def _cached(index: int) -> Optional[QImage]:
                bounded = max(0, min(index, self._total_frames - 1))
                image = self._cache.get(bounded)
                if image is not None:
                    self._cache.move_to_end(bounded)
                return image

            def _store(index: int, image: Optional[QImage]) -> Optional[QImage]:
                if image is None:
                    return None
                bounded = max(0, min(index, self._total_frames - 1))
                self._cache[bounded] = image
                self._cache.move_to_end(bounded)
                self._evict_if_needed()
                self._sequential_next_frame = bounded + 1
                return image

            current = _cached(clamped)
            if current is None:
                sampled = None
                if self._sequential_next_frame == clamped:
                    sampled = self._sampler.sample_next_frame(clamped)
                if sampled is None:
                    sampled = self._sampler.sample_single_frame(
                        clamped,
                        time_index=self._time_index,
                    )
                current = _store(
                    clamped,
                    self._bgr_to_qimage(sampled.frame_data)
                    if sampled is not None else None,
                )

            before = _cached(clamped - 1)
            if before is None:
                # This is normally only needed for a random seek.  It is
                # intentionally done after the current frame so the common
                # forward-playback path keeps its sequential cursor.
                before = self.get_frame(clamped - 1)

            after = _cached(clamped + 1)
            if after is None and clamped + 1 < self._total_frames:
                next_frame = clamped + 1
                sampled = None
                if self._sequential_next_frame == next_frame:
                    sampled = self._sampler.sample_next_frame(next_frame)
                if sampled is None:
                    sampled = self._sampler.sample_single_frame(
                        next_frame,
                        time_index=self._time_index,
                    )
                after = _store(
                    next_frame,
                    self._bgr_to_qimage(sampled.frame_data)
                    if sampled is not None else None,
                )

            return before, current, after

    def clear_cache(self) -> None:
        """Clear the frame cache."""
        self._cache.clear()
        self._sequential_next_frame = None

    @staticmethod
    def _bgr_to_qimage(bgr_array: np.ndarray) -> Optional[QImage]:
        """
        Convert BGR numpy array to QImage with safe lifetime.

        The data is copied to ensure the QImage owns its memory.
        """
        if bgr_array is None or bgr_array.size == 0:
            return None

        try:
            # BGR -> RGB
            rgb = bgr_array[:, :, ::-1].copy()
            h, w, ch = rgb.shape
            bytes_per_line = ch * w
            # Use Format_RGB888 and copy data for lifetime safety
            qimg = QImage(
                rgb.data,
                w,
                h,
                bytes_per_line,
                QImage.Format.Format_RGB888,
            )
            # Critical: copy to decouple from numpy lifetime
            return qimg.copy()
        except Exception as e:
            logger.debug(f"BGR to QImage conversion failed: {e}")
            return None

    def _evict_if_needed(self) -> None:
        """Evict oldest entries if cache exceeds limit."""
        while len(self._cache) > self.MAX_CACHE_SIZE:
            self._cache.popitem(last=False)


# ================================================================
# Dialog Dimensions
# ================================================================

DIALOG_SIZE = {
    "default_width": 900,
    "default_height": 520,
    "min_width": 760,
    "min_height": 420,
}

# Preview frames are intentionally sampled independently from the detector.
# The detector stays at 256x144 for throughput; these larger frames are only
# decoded on demand for the three preview panels.
PREVIEW_SAMPLE_WIDTH = 512
PREVIEW_SAMPLE_HEIGHT = 288

# Layout proportions (percentages)
LAYOUT_PROPS = {
    "preview_height": 40,
    "transport_height": 12,
    "graph_list_height": 38,
    "bottom_height": 10,
}


# ================================================================
# I18N Keys for Scene Cut Dialog
# ================================================================

SCENE_CUT_I18N_KEYS = {
    # Title
    "title": "scene_cut.title",

    # Buttons
    "auto_detect": "scene_cut.auto_detect",
    "auto_detect_running": "scene_cut.auto_detect_running",
    "add_cut": "scene_cut.add_cut",
    "delete_cut": "scene_cut.delete_cut",
    "add_to_media_pool": "scene_cut.add_to_media_pool",
    "show_on_timeline": "scene_cut.show_on_timeline",

    # Transport
    "prev_cut": "scene_cut.prev_cut",
    "next_cut": "scene_cut.next_cut",
    "prev_frame": "scene_cut.prev_frame",
    "next_frame": "scene_cut.next_frame",
    "play": "scene_cut.play",
    "pause": "scene_cut.pause",
    "add_manual_cut": "scene_cut.add_manual_cut",
    "toggle_list": "scene_cut.toggle_list",
    "help": "scene_cut.help",

    # Status
    "no_candidates": "scene_cut.no_candidates",
    "detecting": "scene_cut.detecting",
    "detection_complete": "scene_cut.detection_complete",
    "detection_cancelled": "scene_cut.detection_cancelled",
    "detection_error": "scene_cut.detection_error",
    "candidates_found": "scene_cut.candidates_found",
    "accepted_count": "scene_cut.accepted_count",
    "rejected_count": "scene_cut.rejected_count",
    "pending_count": "scene_cut.pending_count",

    # Batch operations
    "accept_high_confidence": "scene_cut.accept_high_confidence",
    "reject_warnings": "scene_cut.reject_warnings",
    "reset_all": "scene_cut.reset_all",

    # Confirmations
    "confirm_add_cuts": "scene_cut.confirm_add_cuts",
    "confirm_add_cuts_message": "scene_cut.confirm_add_cuts_message",
    "no_accepted_cuts": "scene_cut.no_accepted_cuts",
    "no_accepted_cuts_message": "scene_cut.no_accepted_cuts_message",

    # Sensitivity
    "sensitivity": "scene_cut.sensitivity",
    "sensitivity_low": "scene_cut.sensitivity_low",
    "sensitivity_medium": "scene_cut.sensitivity_medium",
    "sensitivity_high": "scene_cut.sensitivity_high",
}


def _format_cut_confirmation_message(count: int) -> str:
    """Format the localized add-cuts prompt with positional or named fields.

    Existing module translations use ``{0}``, while the shared i18n helper
    formats keyword arguments.  Pass both forms here so the UI never exposes
    a literal placeholder (and so a locale cannot make a valid count appear
    as ``{0}``).
    """
    return _format_count_message(
        SCENE_CUT_I18N_KEYS["confirm_add_cuts_message"],
        "Add {0} cuts to media pool as virtual subclips?",
        count,
    )


def _format_count_message(i18n_key: str, default_template: str, count: int) -> str:
    """Format a localized ``count`` message with positional or named fields.

    AUD-20602: 本模块翻译文件使用 ``{0}`` 位置占位符，而 I18N.tr 只在
    收到 kwargs 时才做 ``format(**kwargs)``——``{0}`` 无法被命名实参填充，
    命中翻译的弹窗会原样显示字面 ``{0}``。这里同时传位置与命名两种
    形式（与 _format_cut_confirmation_message 相同的双格式兜底）。
    """
    template = I18N.tr(i18n_key, default=default_template)
    try:
        return template.format(count, count=count)
    except (IndexError, KeyError, ValueError):
        return default_template.format(count, count=count)


# ================================================================
# Transport Bar Widget
# ================================================================

class TransportBar(QFrame):
    """
    Playback transport control bar.

    Contains:
    - Jump to previous cut
    - Frame backward
    - Play/Pause
    - Frame forward
    - Jump to next cut
    - Add manual cut (scissors icon)
    - Toggle list visibility
    - Mini scrubber

    Signals:
        prev_cut_clicked: Jump to previous detection point.
        next_cut_clicked: Jump to next detection point.
        prev_frame_clicked: Step back one frame.
        next_frame_clicked: Step forward one frame.
        play_pause_clicked: Toggle play/pause.
        add_manual_cut_clicked: Add cut at current frame.
        toggle_list_clicked: Toggle scene list visibility.
        frame_seek_requested: Seek to specific frame.
    """

    prev_cut_clicked = pyqtSignal()
    next_cut_clicked = pyqtSignal()
    prev_frame_clicked = pyqtSignal()
    next_frame_clicked = pyqtSignal()
    play_pause_clicked = pyqtSignal()
    add_manual_cut_clicked = pyqtSignal()
    toggle_list_clicked = pyqtSignal()
    help_clicked = pyqtSignal()
    frame_seek_requested = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initialize transport bar.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)

        self._is_playing: bool = False
        self._list_visible: bool = True

        self._setup_ui()
        self._apply_style()

    def _setup_ui(self) -> None:
        """Set up UI layout."""
        self.setFixedHeight(36)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        # Previous cut button
        self._prev_cut_btn = self._create_tool_button(
            "prev_cut",
            SCENE_CUT_I18N_KEYS["prev_cut"],
        )
        self._prev_cut_btn.clicked.connect(self.prev_cut_clicked.emit)
        layout.addWidget(self._prev_cut_btn)

        # Frame backward
        self._prev_frame_btn = self._create_tool_button(
            "prev_frame",
            SCENE_CUT_I18N_KEYS["prev_frame"],
        )
        self._prev_frame_btn.clicked.connect(self.prev_frame_clicked.emit)
        layout.addWidget(self._prev_frame_btn)

        # Play/Pause button
        self._play_btn = self._create_tool_button(
            "play",
            SCENE_CUT_I18N_KEYS["play"],
            checkable=True,
        )
        self._play_btn.clicked.connect(self._on_play_clicked)
        layout.addWidget(self._play_btn)

        # Frame forward
        self._next_frame_btn = self._create_tool_button(
            "next_frame",
            SCENE_CUT_I18N_KEYS["next_frame"],
        )
        self._next_frame_btn.clicked.connect(self.next_frame_clicked.emit)
        layout.addWidget(self._next_frame_btn)

        # Next cut button
        self._next_cut_btn = self._create_tool_button(
            "next_cut",
            SCENE_CUT_I18N_KEYS["next_cut"],
        )
        self._next_cut_btn.clicked.connect(self.next_cut_clicked.emit)
        layout.addWidget(self._next_cut_btn)

        layout.addSpacing(16)

        # Add manual cut (scissors)
        self._cut_btn = self._create_tool_button(
            "scissors",
            SCENE_CUT_I18N_KEYS["add_manual_cut"],
        )
        self._cut_btn.clicked.connect(self.add_manual_cut_clicked.emit)
        layout.addWidget(self._cut_btn)

        # Toggle list
        self._list_btn = self._create_tool_button(
            "list",
            SCENE_CUT_I18N_KEYS["toggle_list"],
            checkable=True,
        )
        self._list_btn.setObjectName("sceneListToggle")
        self._list_btn.setStyleSheet(f"""
            QToolButton#sceneListToggle {{
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 4px;
                margin: 1px;
            }}
            QToolButton#sceneListToggle:hover {{
                background-color: {EDIT_COLORS["bg_panel_alt"]};
                border: 1px solid {EDIT_COLORS["border_subtle"]};
            }}
            QToolButton#sceneListToggle:checked,
            QToolButton#sceneListToggle:checked:hover {{
                background: transparent;
                border: none;
            }}
        """)
        self._list_btn.setChecked(True)
        self._set_list_button_icon(True)
        self._list_btn.clicked.connect(self._on_list_clicked)
        layout.addWidget(self._list_btn)

        # Help — use the same info-circle treatment as CornerPin node help.
        self._help_btn = self._create_tool_button(
            "help",
            SCENE_CUT_I18N_KEYS["help"],
        )
        self._help_btn.clicked.connect(self.help_clicked.emit)
        layout.addWidget(self._help_btn)

        layout.addStretch()

        # Mini scrubber (simple slider)
        self._scrubber = QSlider(Qt.Orientation.Horizontal)
        self._scrubber.setMinimum(0)
        self._scrubber.setMaximum(1000)
        self._scrubber.setValue(0)
        self._scrubber.setFixedWidth(150)
        self._scrubber.valueChanged.connect(self.frame_seek_requested.emit)
        layout.addWidget(self._scrubber)

        # Apply styling to scrubber
        self._scrubber.setStyleSheet("""
            QSlider::groove:horizontal {
                background-color: #17181c;
                height: 4px;
            }
            QSlider::handle:horizontal {
                background-color: #FF3333;
                width: 10px;
                height: 10px;
                margin: -3px 0;
                border-radius: 5px;
            }
        """)

    def _create_tool_button(
        self,
        icon_name: str,
        tooltip_key: str,
        checkable: bool = False,
    ) -> QToolButton:
        """Create styled tool button."""
        btn = QToolButton()
        btn.setAutoRaise(True)
        btn.setCheckable(checkable)
        btn.setFixedSize(28, 28)

        # Map logical icon names to qtawesome icon names
        _ICON_MAP = {
            "prev_cut": "fa5s.step-backward",
            "next_cut": "fa5s.step-forward",
            "prev_frame": "fa5s.backward",
            "next_frame": "fa5s.forward",
            "play": "fa5s.play",
            "scissors": "fa5s.cut",
            "list": "fa5s.list",
            "help": "fa5s.info-circle",
        }
        icon_id = _ICON_MAP.get(icon_name, icon_name)

        try:
            icon = get_icon(icon_id)
            if icon:
                btn.setIcon(icon)
        except Exception:
            # Fallback to text
            btn.setText(icon_name[:2].upper())

        btn.setToolTip(I18N.tr(tooltip_key, default=icon_name))

        btn.setStyleSheet(tool_button_style())

        return btn

    def _apply_style(self) -> None:
        """Apply widget style."""
        self.setStyleSheet(f"""
            TransportBar {{
                background-color: {EDIT_COLORS["bg_toolbar"]};
                border: none;
                border-bottom: 1px solid {EDIT_COLORS["border_subtle"]};
            }}
        """)

    def _on_play_clicked(self) -> None:
        """Handle play button click."""
        self._is_playing = self._play_btn.isChecked()
        self.play_pause_clicked.emit()

        # Update button appearance
        if self._is_playing:
            self._play_btn.setToolTip(I18N.tr(SCENE_CUT_I18N_KEYS["pause"], default="Pause"))
        else:
            self._play_btn.setToolTip(I18N.tr(SCENE_CUT_I18N_KEYS["play"], default="Play"))

    def _on_list_clicked(self) -> None:
        """Handle list toggle."""
        self._list_visible = self._list_btn.isChecked()
        self._set_list_button_icon(self._list_visible)
        self.toggle_list_clicked.emit()

    def _set_list_button_icon(self, checked: bool) -> None:
        """Color only the scene-list icon when the list is active."""
        color = IconColors.ORANGE if checked else IconColors.DEFAULT
        self._list_btn.setIcon(get_icon("fa5s.list", color=color))

    def set_playing(self, playing: bool) -> None:
        """
        Set play state.

        Args:
            playing: True if playing.
        """
        self._is_playing = playing
        self._play_btn.setChecked(playing)
        self._play_btn.setToolTip(
            I18N.tr(
                SCENE_CUT_I18N_KEYS["pause" if playing else "play"],
                default="Pause" if playing else "Play",
            )
        )

    def is_playing(self) -> bool:
        """Return whether transport playback is active."""
        return self._is_playing

    def current_frame(self) -> int:
        """Return the current source frame shown by the scrubber."""
        return self._scrubber.value()

    def set_scrubber_range(self, max_frame: int) -> None:
        """
        Set scrubber maximum frame.

        Args:
            max_frame: Maximum frame number.
        """
        self._scrubber.setMaximum(max_frame)

    def set_scrubber_value(self, frame: int) -> None:
        """
        Set scrubber current frame.

        Args:
            frame: Current frame number.
        """
        self._scrubber.setValue(frame)

    def is_list_visible(self) -> bool:
        """Check if list is visible."""
        return self._list_visible


# ================================================================
# Scene List Widget
# ================================================================

class SceneListWidget(QFrame):
    """
    Scene list table widget.

    Displays candidates in a table with columns:
    - Scene number
    - Frame
    - Start TC
    - Decision (editable)
    - Score
    - Type

    Signals:
        candidate_selected: Emitted when selection changes.
        decision_double_clicked: Emitted when decision is double-clicked.
    """

    candidate_selected = pyqtSignal(int)  # index
    decision_double_clicked = pyqtSignal(int)  # index

    # Compact identifier columns with enough room for the values users need
    # to read at a glance.  Previously all fixed columns inherited 80px,
    # leaving Scene too wide and Start TC too narrow.
    COLUMN_WIDTHS = {
        Column.SCENE.value: 52,
        Column.FRAME.value: 68,
        Column.TIMECODE.value: 102,
        Column.DECISION.value: 82,
        Column.SCORE.value: 58,
        Column.KIND.value: 78,
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initialize scene list.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)

        self._updating: bool = False

        self._setup_ui()
        self._apply_style()

    def _setup_ui(self) -> None:
        """Set up UI layout."""
        self.setMinimumWidth(250)
        self.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding,
            QSizePolicy.Policy.Expanding,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Table view
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._table.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        # Column widths
        header = self._table.horizontalHeader()
        header.setDefaultSectionSize(68)
        header.setMinimumSectionSize(40)
        header.setSectionResizeMode(
            Column.SCENE.value,
            QHeaderView.ResizeMode.Fixed,
        )
        header.setSectionResizeMode(
            Column.FRAME.value,
            QHeaderView.ResizeMode.Fixed,
        )
        header.setSectionResizeMode(
            Column.TIMECODE.value,
            QHeaderView.ResizeMode.Fixed,
        )
        header.setSectionResizeMode(
            Column.DECISION.value,
            QHeaderView.ResizeMode.Fixed,
        )
        header.setSectionResizeMode(
            Column.SCORE.value,
            QHeaderView.ResizeMode.Fixed,
        )
        header.setSectionResizeMode(
            Column.KIND.value,
            QHeaderView.ResizeMode.Fixed,
        )
        layout.addWidget(self._table)

        # Double-click can be connected immediately (doesn't need a model)
        self._table.doubleClicked.connect(self._on_double_click)

    def _apply_style(self) -> None:
        """Apply widget style."""
        self.setStyleSheet(f"""
            SceneListWidget {{
                background-color: {EDIT_COLORS["bg_panel"]};
                border: none;
                border-left: 1px solid {EDIT_COLORS["border_subtle"]};
            }}

            QTableView {{
                background-color: {EDIT_COLORS["bg_panel"]};
                alternate-background-color: {EDIT_COLORS["bg_panel_alt"]};
                border: none;
                gridline-color: {EDIT_COLORS["border_subtle"]};
                font-size: 12px;
            }}

            /* Flatten the corner where the two scrollbars meet. */
            QTableCornerButton::section,
            QAbstractScrollArea::corner {{
                background: #15161a;
                border: none;
            }}

            QTableView::item {{
                height: 24px;
                padding: 2px 4px;
            }}

            QTableView::item:selected {{
                background-color: {EDIT_COLORS["accent"]};
                color: #ffffff;
            }}

            QTableView::item:hover {{
                /* Keep hover from painting a gray cell over selected rows. */
                background-color: transparent;
            }}

            QTableView::item:selected:hover {{
                background-color: {EDIT_COLORS["accent"]};
                color: #ffffff;
            }}

            QHeaderView::section {{
                background-color: {EDIT_COLORS["bg_toolbar"]};
                color: {EDIT_COLORS["text_muted"]};
                font-size: 11px;
                font-weight: bold;
                padding: 4px 8px;
                border: none;
                border-bottom: 1px solid {EDIT_COLORS["border_subtle"]};
            }}

            /* Compact dark scrollbars for both table directions. */
            QScrollBar:vertical {{
                background: #15161a;
                width: 7px;
                margin: 0;
            }}

            QScrollBar:horizontal {{
                background: #15161a;
                height: 7px;
                margin: 0;
            }}

            QScrollBar::handle:vertical,
            QScrollBar::handle:horizontal {{
                background: #55575e;
                border-radius: 3px;
                min-height: 22px;
                min-width: 22px;
            }}

            QScrollBar::handle:vertical:hover,
            QScrollBar::handle:horizontal:hover {{
                background: #62646b;
            }}

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {{
                width: 0;
                height: 0;
                border: none;
                background: none;
            }}

            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical,
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
        """)

    def set_model(self, model: SceneCutResultModel) -> None:
        """
        Set table model.

        Args:
            model: SceneCutResultModel.
        """
        self._table.setModel(model)
        # Apply widths after installing the model; QHeaderView creates its
        # sections at that point and may otherwise restore the default size.
        for column, width in self.COLUMN_WIDTHS.items():
            self._table.setColumnWidth(column, width)
        self._table.selectionModel().selectionChanged.connect(
            self._on_selection_changed
        )

    def select_row(self, index: int) -> None:
        """
        Select row by index.

        Args:
            index: Row index.
        """
        if self._updating:
            return

        self._updating = True
        model = self._table.model()
        if model and 0 <= index < model.rowCount():
            row_index = model.index(index, 0)
            self._table.selectRow(index)
            self._table.scrollTo(row_index)
        self._updating = False

    def get_selected_index(self) -> int:
        """Get currently selected row index."""
        selected = self._table.selectionModel().selectedRows()
        if selected:
            return selected[0].row()
        return -1

    def _on_selection_changed(
        self,
        selected: Any,
        deselected: Any,
    ) -> None:
        """Handle selection change."""
        if self._updating:
            return

        index = self.get_selected_index()
        if index >= 0:
            self.candidate_selected.emit(index)

    def _on_double_click(self, index: QModelIndex) -> None:
        """Handle double click."""
        if index.column() == Column.DECISION.value:
            self.decision_double_clicked.emit(index.row())


# ================================================================
# Bottom Action Bar Widget
# ================================================================

class BottomActionBar(QFrame):
    """
    Bottom action bar with main buttons.

    Layout:
        [Auto Detect] [Add] [Delete]  |  [Sensitivity Slider]  |  [Add Cuts to Media Pool]

    Signals:
        auto_detect_clicked: Start/restart detection.
        add_cut_clicked: Add manual cut at current frame.
        delete_cut_clicked: Delete/reject current cut.
        add_to_media_pool_clicked: Add accepted cuts to media pool.
        sensitivity_changed: Sensitivity slider changed.
        batch_accept_clicked: Batch accept high confidence.
        batch_reject_clicked: Batch reject warnings.
        reset_all_clicked: Reset all decisions.
    """

    auto_detect_clicked = pyqtSignal()
    add_cut_clicked = pyqtSignal()
    delete_cut_clicked = pyqtSignal()
    add_to_media_pool_clicked = pyqtSignal()
    sensitivity_changed = pyqtSignal(float)
    batch_accept_clicked = pyqtSignal()
    batch_reject_clicked = pyqtSignal()
    reset_all_clicked = pyqtSignal()
    # B1: toggle the timeline candidate overlay from the dialog.
    show_on_timeline_toggled = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initialize bottom action bar.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)

        self._updating: bool = False
        self._detection_running: bool = False

        self._setup_ui()
        self._apply_style()

    def _setup_ui(self) -> None:
        """Set up UI layout."""
        self.setFixedHeight(44)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        # Left section: Auto Detect, Add, Delete
        left_section = QHBoxLayout()
        left_section.setSpacing(4)

        # Auto Detect button (main action)
        self._auto_detect_btn = QPushButton(
            I18N.tr(SCENE_CUT_I18N_KEYS["auto_detect"], default="Auto Scene Detect")
        )
        self._auto_detect_btn.setStyleSheet(accent_button_style())
        self._auto_detect_btn.clicked.connect(self.auto_detect_clicked.emit)
        left_section.addWidget(self._auto_detect_btn)

        # Add button
        self._add_btn = QPushButton(
            I18N.tr(SCENE_CUT_I18N_KEYS["add_cut"], default="Add")
        )
        self._add_btn.setStyleSheet(button_style())
        self._add_btn.clicked.connect(self.add_cut_clicked.emit)
        left_section.addWidget(self._add_btn)

        # Delete button
        self._delete_btn = QPushButton(
            I18N.tr(SCENE_CUT_I18N_KEYS["delete_cut"], default="Delete")
        )
        self._delete_btn.setStyleSheet(button_style())
        self._delete_btn.clicked.connect(self.delete_cut_clicked.emit)
        left_section.addWidget(self._delete_btn)

        layout.addLayout(left_section)

        # Batch operations menu button
        self._batch_btn = QToolButton()
        self._batch_btn.setObjectName("sceneCutBatchMenu")
        self._batch_btn.setToolTip(
            I18N.tr("scene_cut.batch_operations", default="Batch operations")
        )
        self._batch_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._batch_btn.setAutoRaise(True)
        self._batch_btn.setFixedSize(28, 28)
        self._batch_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        batch_icon = get_icon("fa5s.ellipsis-h", color=IconColors.DEFAULT)
        if batch_icon.isNull():
            self._batch_btn.setText("…")
        else:
            self._batch_btn.setIcon(batch_icon)
            self._batch_btn.setIconSize(QSize(16, 16))
        # The button still owns a QMenu, but the shared QToolButton menu
        # indicator adds a distracting down-chevron beside the ellipsis.
        # Keep the menu popup behavior and hide only that indicator.
        self._batch_btn.setStyleSheet(
            tool_button_style()
            + "\nQToolButton#sceneCutBatchMenu::menu-indicator { image: none; width: 0px; }"
        )
        self._setup_batch_menu()
        layout.addWidget(self._batch_btn)

        layout.addStretch()

        # Center section: Sensitivity slider
        self._sensitivity_slider = ThresholdSlider()
        self._sensitivity_slider.threshold_changed.connect(self.sensitivity_changed.emit)
        layout.addWidget(self._sensitivity_slider)

        layout.addStretch()

        # Right section: timeline annotation (B1) + Add Cuts to Media Pool
        self._show_on_timeline_btn = QPushButton(
            I18N.tr(SCENE_CUT_I18N_KEYS["show_on_timeline"], default="Show on Timeline")
        )
        self._show_on_timeline_btn.setStyleSheet(button_style())
        self._show_on_timeline_btn.setCheckable(True)
        # Enabled once an analysis is available (detection finished or
        # restored from session memory).  Toggling it drives the same
        # EditPage-owned switch as the View menu entry.
        self._show_on_timeline_btn.setEnabled(False)
        self._show_on_timeline_btn.toggled.connect(
            self.show_on_timeline_toggled.emit
        )
        layout.addWidget(self._show_on_timeline_btn)

        # Right section: Add Cuts to Media Pool
        self._add_cuts_btn = QPushButton(
            I18N.tr(SCENE_CUT_I18N_KEYS["add_to_media_pool"], default="Add Cuts to Media Pool")
        )
        self._add_cuts_btn.setStyleSheet(accent_button_style())
        self._add_cuts_btn.clicked.connect(self.add_to_media_pool_clicked.emit)
        layout.addWidget(self._add_cuts_btn)

    def _setup_batch_menu(self) -> None:
        """Set up batch operations menu."""
        menu = QMenu(self._batch_btn)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1F1F22;
                border: 1px solid #333333;
            }
            QMenu::item {
                padding: 6px 20px;
                color: #D6D8DE;
            }
            QMenu::item:selected {
                background-color: #E29227;
            }
        """)

        accept_action = QAction(
            I18N.tr(SCENE_CUT_I18N_KEYS["accept_high_confidence"], default="Accept High Confidence"),
            self,
        )
        accept_action.triggered.connect(self.batch_accept_clicked.emit)
        menu.addAction(accept_action)

        reject_action = QAction(
            I18N.tr(SCENE_CUT_I18N_KEYS["reject_warnings"], default="Reject Warnings"),
            self,
        )
        reject_action.triggered.connect(self.batch_reject_clicked.emit)
        menu.addAction(reject_action)

        reset_action = QAction(
            I18N.tr(SCENE_CUT_I18N_KEYS["reset_all"], default="Reset All"),
            self,
        )
        reset_action.triggered.connect(self.reset_all_clicked.emit)
        menu.addAction(reset_action)

        self._batch_btn.setMenu(menu)

    def _apply_style(self) -> None:
        """Apply widget style."""
        self.setStyleSheet(f"""
            BottomActionBar {{
                background-color: {EDIT_COLORS["bg_toolbar"]};
                border: none;
                border-top: 1px solid {EDIT_COLORS["border_subtle"]};
            }}
        """)

    def set_detection_running(self, running: bool) -> None:
        """
        Set detection running state.

        Args:
            running: True if detection is in progress.
        """
        self._detection_running = running
        if running:
            self._auto_detect_btn.setText(
                I18N.tr(SCENE_CUT_I18N_KEYS["auto_detect_running"], default="Detecting...")
            )
            self._auto_detect_btn.setEnabled(False)
        else:
            self._auto_detect_btn.setText(
                I18N.tr(SCENE_CUT_I18N_KEYS["auto_detect"], default="Auto Scene Detect")
            )
            self._auto_detect_btn.setEnabled(True)

    def set_sensitivity(self, value: float) -> None:
        """
        Set sensitivity slider value.

        Args:
            value: Sensitivity (0.35-0.75).
        """
        self._sensitivity_slider.set_threshold(value)

    def set_add_cuts_enabled(self, enabled: bool) -> None:
        """
        Enable/disable Add Cuts button.

        Args:
            enabled: True if button should be enabled.
        """
        self._add_cuts_btn.setEnabled(enabled)

    def set_show_on_timeline_enabled(self, enabled: bool) -> None:
        """
        Enable/disable the Show-on-Timeline toggle (B1).

        Args:
            enabled: True once an analysis result is available.
        """
        self._show_on_timeline_btn.setEnabled(enabled)

    def set_show_on_timeline_checked(self, checked: bool) -> None:
        """
        Sync the Show-on-Timeline toggle without re-emitting the signal.

        Args:
            checked: New checked state of the timeline annotation toggle.
        """
        btn = self._show_on_timeline_btn
        if btn.isChecked() == bool(checked):
            return
        btn.blockSignals(True)
        btn.setChecked(bool(checked))
        btn.blockSignals(False)

    def is_show_on_timeline_checked(self) -> bool:
        """Return the current Show-on-Timeline toggle state."""
        return self._show_on_timeline_btn.isChecked()

    def get_sensitivity(self) -> float:
        """Get current sensitivity value."""
        return self._sensitivity_slider.get_threshold()


# ================================================================
# Async Preview Loader (SC-REM-10)
# ================================================================

class PreviewLoadWorker(QThread):
    """
    Background thread for loading preview frames.

    Prevents UI blocking when decoding frames from long GOP / 4K / network media.
    Emits frames_ready when done; the dialog checks that the candidate index
    hasn't changed before applying (prevents stale results on rapid clicks).
    """
    frames_ready = pyqtSignal(
        int, int, object, object, object
    )  # candidate index, frame, before, current, after

    def __init__(
        self,
        provider: PreviewFrameProvider,
        candidate_index: int,
        frame_number: int,
        sequential: bool = False,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self._candidate_index = candidate_index
        self._frame_number = frame_number
        self._sequential = sequential

    def run(self) -> None:
        if self._provider is None or not self._provider.is_open:
            self.frames_ready.emit(
                self._candidate_index,
                self._frame_number,
                None,
                None,
                None,
            )
            return
        if self.isInterruptionRequested():
            return
        if self._sequential:
            before, current, after = self._provider.get_sequential_preview_frames(
                self._frame_number
            )
        else:
            before, current, after = self._provider.get_preview_frames(self._frame_number)
        if self.isInterruptionRequested():
            return
        self.frames_ready.emit(
            self._candidate_index,
            self._frame_number,
            before,
            current,
            after,
        )


# ================================================================
# Scene Cut Dialog (Main)
# ================================================================

class SceneCutDialog(QDialog):
    """
    Main scene cut detection dialog.

    Integrates all components:
    - SceneCutPreviewWidget (three-panel preview)
    - TransportBar (playback controls)
    - SceneCutGraphWidget (detection graph)
    - SceneListWidget (candidate table)
    - BottomActionBar (action buttons)

    Signals (internal, for external connection):
        cuts_added_to_pool: Emitted when cuts are added to media pool.
            Parameters: (accepted_candidates_list)
        detection_completed: Emitted when detection finishes.
        manual_cut_added: Emitted when manual cut is added.

    Keyboard shortcuts:
        Enter: Accept current candidate
        Delete/Backspace: Reject current candidate
        Up: Select previous candidate
        Down: Select next candidate
        Space: Toggle play/pause

    Following .clinerules:
        - Strong typing
        - Signal anti-recursion with _updating flag
        - UI does not directly execute heavy detection
    """

    # Public signals for external connection
    cuts_added_to_pool = pyqtSignal(list)  # List[SceneCutCandidate]
    detection_completed = pyqtSignal(object)  # SceneCutAnalysis
    manual_cut_added = pyqtSignal(int)  # frame_number
    detection_started = pyqtSignal(str, str)  # source_path, asset_id
    # B1: user toggled the timeline candidate overlay from this dialog.
    show_on_timeline_toggled = pyqtSignal(bool)

    def __init__(
        self,
        source_path: Optional[str] = None,
        asset_id: Optional[str] = None,
        settings: Optional[SceneCutSettings] = None,
        proxy_path: Optional[str] = None,
        use_persistent_cache: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        Initialize scene cut dialog.

        Args:
            source_path: Source video path (optional, can be set later).
            asset_id: Asset identifier (optional).
            settings: Detection settings (optional, uses defaults).
            proxy_path: Optional proxy media path for accelerated detection.
            use_persistent_cache: Whether to read/write the disk analysis cache.
            parent: Optional parent widget.
        """
        super().__init__(parent)

        # Data
        self._source_path: Optional[str] = source_path
        self._asset_id: Optional[str] = asset_id
        self._proxy_path: Optional[str] = proxy_path
        self._use_persistent_cache: bool = use_persistent_cache
        self._settings: SceneCutSettings = settings or SceneCutSettings()
        self._analysis: Optional[SceneCutAnalysis] = None

        # Worker management
        self._worker: Optional[SceneCutWorker] = None
        # 取消后有界等待仍不退出的探测线程：断开信号后保引用，由其
        # 自身 finished→deleteLater 连接自回收（连接持有 bound method
        # 的引用，线程结束前 Python 侧不会被回收）。
        self._zombie_detection_workers: list[SceneCutWorker] = []

        # Preview frame provider
        self._frame_provider: Optional[PreviewFrameProvider] = None
        self._preview_worker: Optional[PreviewLoadWorker] = None
        self._preview_workers: list[PreviewLoadWorker] = []
        # closeEvent 中 wait 超时仍未退出的预览线程：只保引用等其自然
        # 退出，绝不 deleteLater 运行中的 QThread（销毁运行中的线程是
        # 未定义行为，典型表现为进程崩溃）。线程结束后由其自身
        # finished 信号回调 _on_preview_worker_finished 做最终清理。
        self._zombie_preview_workers: list[PreviewLoadWorker] = []
        self._preview_pending_request: Optional[tuple[int, Optional[int], bool]] = None
        # AUD-20604: providers whose close was deferred because a preview
        # worker was still decoding (and holding the provider lock) when the
        # source was switched.  Drained by _on_preview_worker_finished.
        self._providers_pending_close: list[PreviewFrameProvider] = []
        # AUD-20604: bumped on every source switch so results from workers
        # started against the previous source can be recognised and dropped.
        self._preview_generation: int = 0

        # 探测运行期间冻结本对话框的预览解码：同一素材的并发解码会与
        # 探测线程争抢 CPU（4K 素材实测使探测耗时翻倍），完成后恢复。
        self._detection_decoding_paused: bool = False

        # 探测期间挂起了全局后台代理生成时置位，终态/关闭时恢复。
        self._proxy_generation_paused: bool = False

        # Playback is driven by the source FPS, while frame images are loaded
        # asynchronously by the preview provider.
        self._playback_fps: float = 24.0
        self._playback_timer = QTimer(self)
        self._playback_timer.timeout.connect(self._advance_playback)
        self._shortcut_filter_installed: bool = False
        self._frame_step_shortcuts: tuple[QShortcut, QShortcut] = ()
        if source_path:
            self._init_frame_provider(source_path)

        # UI state
        self._updating: bool = False
        self._current_index: int = -1
        self._list_visible: bool = True

        # Result model
        self._model = SceneCutResultModel()
        self._model.set_i18n_resolver(I18N.tr)
        self._model.decision_changed.connect(self._on_decision_changed)
        self._model.selection_changed.connect(self._on_selection_changed)

        # Set up UI
        self._setup_ui()
        self._apply_style()
        self._connect_signals()
        self._register_shortcuts()
        self._install_shortcut_filter()

        # Set window properties
        self.setWindowTitle(I18N.tr(SCENE_CUT_I18N_KEYS["title"], default="Scene Detect"))
        self.setMinimumSize(
            DIALOG_SIZE["min_width"],
            DIALOG_SIZE["min_height"],
        )
        self.resize(
            DIALOG_SIZE["default_width"],
            DIALOG_SIZE["default_height"],
        )
        self._sync_playback_source()

        # Show the source media immediately, before detection produces any
        # candidates. Candidate previews are loaded lazily below; the initial
        # frame follows the same background decode path to keep the UI
        # responsive for long-GOP and high-resolution media.
        self._load_initial_preview()

        logger.debug("SceneCutDialog initialized")

    def _setup_ui(self) -> None:
        """Set up main UI layout."""
        # Main vertical layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Top: Preview widget (~40%)
        self._preview = SceneCutPreviewWidget()
        layout.addWidget(self._preview, stretch=40)

        # Transport bar (~12%)
        self._transport = TransportBar()
        layout.addWidget(self._transport, stretch=1)

        # Middle: Splitter for graph + list (~38%)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setStyleSheet("""
            QSplitter {
                background-color: #1F1F22;
            }
            QSplitter::handle {
                background-color: #2a2a2a;
                width: 1px;
            }
        """)

        # Graph widget
        self._graph = SceneCutGraphWidget()
        self._splitter.addWidget(self._graph)

        # Scene list
        self._scene_list = SceneListWidget()
        self._scene_list.set_model(self._model)
        self._splitter.addWidget(self._scene_list)

        # Set initial splitter proportions (70% graph, 30% list)
        self._splitter.setSizes([600, 250])

        layout.addWidget(self._splitter, stretch=38)

        # Bottom: Action bar (~10%)
        self._bottom_bar = BottomActionBar()
        layout.addWidget(self._bottom_bar, stretch=1)

        # Progress bar (hidden by default)
        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(4)
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #17181c;
                border: none;
            }
            QProgressBar::chunk {
                background-color: #E29227;
            }
        """)
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

    def _apply_style(self) -> None:
        """Apply dialog style."""
        self.setStyleSheet(f"""
            SceneCutDialog {{
                background-color: {EDIT_COLORS["bg_app"]};
                color: {EDIT_COLORS["text"]};
            }}
        """)

    def _connect_signals(self) -> None:
        """Connect internal signals."""
        # Transport signals
        self._transport.prev_cut_clicked.connect(self._goto_prev_cut)
        self._transport.next_cut_clicked.connect(self._goto_next_cut)
        self._transport.prev_frame_clicked.connect(self._step_prev_frame)
        self._transport.next_frame_clicked.connect(self._step_next_frame)
        self._transport.play_pause_clicked.connect(self._toggle_play)
        self._transport.add_manual_cut_clicked.connect(self._add_manual_cut)
        self._transport.toggle_list_clicked.connect(self._toggle_list_visibility)
        self._transport.help_clicked.connect(self._show_help)
        self._transport.frame_seek_requested.connect(self._seek_to_frame)

        # Graph signals
        self._graph.candidate_clicked.connect(self._select_candidate)
        self._graph.frame_clicked.connect(self._seek_to_frame)
        self._graph.threshold_changed.connect(self._on_graph_threshold_changed)

        # Scene list signals
        self._scene_list.candidate_selected.connect(self._select_candidate)

        # Bottom bar signals
        self._bottom_bar.auto_detect_clicked.connect(self._start_detection)
        self._bottom_bar.add_cut_clicked.connect(self._add_manual_cut)
        self._bottom_bar.delete_cut_clicked.connect(self._delete_current_cut)
        self._bottom_bar.add_to_media_pool_clicked.connect(self._add_cuts_to_pool)
        self._bottom_bar.sensitivity_changed.connect(self._on_sensitivity_changed)
        self._bottom_bar.batch_accept_clicked.connect(self._batch_accept_high_confidence)
        self._bottom_bar.batch_reject_clicked.connect(self._batch_reject_warnings)
        self._bottom_bar.reset_all_clicked.connect(self._batch_reset_all)
        # B1: forward the timeline-annotation toggle to external wiring.
        self._bottom_bar.show_on_timeline_toggled.connect(
            self.show_on_timeline_toggled.emit
        )

    # ==================== Public Methods ====================

    def _init_frame_provider(self, source_path: str) -> None:
        """Initialize the preview frame provider for a source."""
        self._preview_pending_request = None
        # AUD-20604: invalidate any in-flight results from the previous
        # source before replacing the provider.
        self._preview_generation += 1
        self._cancel_active_preview_worker()
        old_provider = self._frame_provider
        self._frame_provider = None
        if self._preview_worker is not None and self._preview_worker.isRunning():
            # The provider owns the PyAV container.  Finish the old request
            # before closing that container so a source switch cannot let an
            # old worker read from (or present) the new provider.
            if not self._preview_worker.wait(1000):
                # AUD-20604: wait() timed out — the worker is still decoding
                # and holds the provider's decode lock.  Closing the old
                # provider here would block the UI thread until the in-flight
                # decode finishes; defer the close to the worker's finished
                # callback instead.
                if old_provider is not None:
                    self._providers_pending_close.append(old_provider)
                    old_provider = None
                logger.debug(
                    "SceneCutDialog: preview worker still decoding during "
                    "source switch; deferring old provider close until it exits"
                )
        # Close existing provider (only when no in-flight decode holds it)
        if old_provider is not None:
            old_provider.close()

        preview_settings = replace(
            self._settings,
            sample_width=PREVIEW_SAMPLE_WIDTH,
            max_sample_height=PREVIEW_SAMPLE_HEIGHT,
        )
        self._frame_provider = PreviewFrameProvider(source_path, preview_settings)
        if not self._frame_provider.open():
            logger.warning(f"Failed to open frame provider for {source_path}")
            self._frame_provider = None

    def set_source(
        self,
        source_path: str,
        asset_id: str,
        settings: Optional[SceneCutSettings] = None,
        proxy_path: Optional[str] = None,
    ) -> None:
        """
        Set source media for detection.

        Args:
            source_path: Source video file path.
            asset_id: Asset identifier.
            settings: Optional detection settings.
            proxy_path: Optional proxy media path for accelerated detection.
        """
        self._source_path = source_path
        self._asset_id = asset_id
        self._proxy_path = proxy_path
        self._stop_playback()
        if settings:
            self._settings = settings

        # AUD-20604: the old source's candidate index is meaningless for the
        # new source and would break the candidate guard for the initial
        # preview (candidate_index == -1 vs stale _current_index).  Reset it
        # so late frames from the old source cannot be presented.
        self._current_index = -1
        self._model.set_current_index(-1)

        self._init_frame_provider(source_path)
        self._sync_playback_source()
        self._bottom_bar.set_sensitivity(self._settings.sensitivity)
        self._load_initial_preview()

        logger.debug(f"Source set: {source_path}")

    def set_analysis(self, analysis: SceneCutAnalysis) -> None:
        """
        Set existing analysis data.

        Args:
            analysis: SceneCutAnalysis from cache or previous detection.
        """
        self._analysis = analysis
        self._model.set_analysis(analysis)
        self._graph.set_candidates(
            list(analysis.candidates),
            self._get_total_frames(),
        )

        # The detector uses an adaptive threshold. Display that actual value
        # on the graph instead of the user-facing sensitivity, which is only
        # an input parameter for the next detection run.
        threshold = (analysis.diagnostics or {}).get("threshold_value")
        if isinstance(threshold, (int, float)):
            self._graph.set_threshold(float(threshold))

        # Pass time index to preview provider for VFR-accurate seeking
        if self._frame_provider is not None:
            self._frame_provider.set_time_index(analysis.source_time_index)

        self._update_ui_counts()
        self._select_first_candidate()

        # B1: with an analysis available the timeline annotation toggle can
        # now be used (also covers session-restored results).
        self._bottom_bar.set_show_on_timeline_enabled(True)

        logger.debug(f"Analysis set: {len(analysis.candidates)} candidates")

    def set_show_on_timeline_checked(self, checked: bool) -> None:
        """
        Sync the Show-on-Timeline toggle with the external overlay state (B1).

        Used when the overlay is toggled from the View menu so the dialog
        button reflects the shared state without re-emitting its signal.

        Args:
            checked: Whether the timeline overlay is visible.
        """
        self._bottom_bar.set_show_on_timeline_checked(checked)

    def is_show_on_timeline_checked(self) -> bool:
        """Return whether the dialog's timeline toggle is checked."""
        return self._bottom_bar.is_show_on_timeline_checked()

    def get_accepted_candidates(self) -> List[SceneCutCandidate]:
        """
        Get all accepted candidates.

        Returns:
            List of accepted SceneCutCandidate.
        """
        return self._model.get_accepted_candidates()

    def get_analysis(self) -> Optional[SceneCutAnalysis]:
        """Get current analysis with all user decisions applied."""
        if self._analysis is None:
            return None
        # Always return analysis with current model decisions
        updated = self._model.to_updated_analysis()
        if updated is not None:
            self._analysis = updated
        return self._analysis

    def clear(self) -> None:
        """Clear all data."""
        self._stop_playback()
        self._analysis = None
        self._model.clear()
        self._graph.clear()
        self._preview.clear()
        self._current_index = -1
        self._update_ui_counts()

    # ==================== Detection Control ====================

    def _start_detection(self) -> None:
        """Start scene cut detection."""
        if self._source_path is None or self._asset_id is None:
            QMessageBox.warning(
                self,
                I18N.tr("scene_cut.no_source", default="No Source"),
                I18N.tr("scene_cut.no_source_message", default="Please select a video source first."),
            )
            return

        # Cancel any running worker
        if self._worker and self._worker.is_running:
            self._worker.cancel()
            # AUD-20603: 取消检查点仅位于采样循环内，容器打开/探测器
            # 构造阶段（网络盘、慢速代理）不响应取消；无参 wait() 会在
            # 该阶段无限阻塞 UI 线程。有界等待，超时则放弃旧 worker。
            if not self._worker.wait(2000):
                self._abandon_detection_worker(self._worker)

        # Update settings with current sensitivity
        self._settings = self._settings.with_sensitivity(
            self._bottom_bar.get_sensitivity()
        )

        # Try loading from cache first
        cached_analysis = self._try_load_cache() if self._use_persistent_cache else None
        if cached_analysis is not None:
            # AUD-20601: 直调无 sender 校验的应用入口；此前调
            # _on_detection_completed 会被陈旧信号守卫（sender 为
            # BottomActionBar 而非 worker）整体吞掉。
            self._apply_detection_completed(cached_analysis)
            logger.info(f"Loaded cached analysis: {len(cached_analysis.candidates)} candidates")
            return

        # Create worker
        try:
            self._worker = SceneCutWorker(
                source_path=self._source_path,
                asset_id=self._asset_id,
                settings=self._settings,
                proxy_path=self._proxy_path,
            )
        except Exception as e:
            logger.error(f"Failed to create SceneCutWorker: {e}")
            self._worker = None

        if self._worker is None:
            QMessageBox.critical(
                self,
                I18N.tr("scene_cut.error", default="Error"),
                I18N.tr("scene_cut.worker_create_failed", default="Failed to create detection worker."),
            )
            return

        # Connect worker signals
        self._worker.progress.connect(self._on_detection_progress)
        self._worker.completed.connect(self._on_detection_completed)
        self._worker.error.connect(self._on_detection_error)
        self._worker.cancelled.connect(self._on_detection_cancelled)

        # Start detection
        if self._worker.start_detection():
            # 冻结预览解码直到探测结束：预览面板与探测共用同一解码源，
            # 并发解码会争抢 CPU 使探测耗时近似翻倍（4K 实测 34s→70s）。
            self._detection_decoding_paused = True
            self._stop_playback()
            self._cancel_active_preview_worker()
            # 导入 >1080p 素材会自动触发后台代理转码（ffmpeg 吃满多核），
            # 与探测并发时实测把 38s 的探测拖到 100s+；探测期间挂起，
            # 结束后恢复（SIGSTOP 保留转码进度）。
            try:
                from src.shared.media.disk_proxy_service import get_disk_proxy_service

                get_disk_proxy_service().pause_all_generation()
                self._proxy_generation_paused = True
            except Exception as e:
                logger.debug(f"Pause proxy generation failed: {e}")
            self._bottom_bar.set_detection_running(True)
            self._progress_bar.setVisible(True)
            self._progress_bar.setValue(0)
            self.detection_started.emit(self._source_path, self._asset_id)
            logger.info(f"Detection started: {self._source_path}")
        else:
            QMessageBox.information(
                self,
                I18N.tr("scene_cut.already_running", default="Detection Running"),
                I18N.tr("scene_cut.already_running_message", default="Detection is already running for this source."),
            )

    def _cancel_detection(self) -> None:
        """Cancel current detection."""
        if self._worker and self._worker.is_running:
            self._worker.cancel()

    def _abandon_detection_worker(self, worker: SceneCutWorker) -> None:
        """放弃取消后仍未退出的探测线程（AUD-20603）。

        断开其全部信号（进度处理器 _on_detection_progress 无 sender
        守卫，陈旧进度会污染新一轮探测的进度条），保引用转入 zombie
        列表；finished→deleteLater 连接既触发 C++ 侧回收，也使线程
        结束前 Python 引用不被提前释放——绝不销毁运行中的 QThread。
        """
        for signal, slot in (
            (worker.progress, self._on_detection_progress),
            (worker.completed, self._on_detection_completed),
            (worker.error, self._on_detection_error),
            (worker.cancelled, self._on_detection_cancelled),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                # 连接不存在时 disconnect 抛 TypeError，容错继续
                pass
        worker.finished.connect(worker.deleteLater)
        self._zombie_detection_workers.append(worker)
        if self._worker is worker:
            self._worker = None
        logger.warning(
            "SceneCutDialog: detection worker did not stop within the "
            "bounded wait; abandoning it (late results will be dropped)"
        )

    def _try_load_cache(self) -> Optional[SceneCutAnalysis]:
        """Try to load analysis from cache. Returns None on miss."""
        if not self._source_path or not self._asset_id:
            return None
        try:
            import os
            stat = os.stat(self._source_path)
            cache_key = generate_cache_key(
                path=self._source_path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                asset_id=self._asset_id,
                settings=self._settings,
                proxy_path=self._proxy_path,
            )
            cache = SceneCutCache()
            return cache.load_analysis(cache_key)
        except Exception:
            return None

    def _save_analysis_to_cache(self, analysis: SceneCutAnalysis) -> None:
        """Save analysis to cache after detection (SC-REM-06).

        Failure is non-blocking — only logged.
        """
        try:
            cache = SceneCutCache()
            cache.save_analysis(analysis)
            logger.info(f"Saved analysis to cache: {analysis.analysis_id}")
        except Exception as e:
            logger.warning(f"Failed to save analysis to cache: {e}")

    # ==================== Selection and Navigation ====================

    def _load_initial_preview(self) -> None:
        """Load the first source frames while no cut candidate is selected."""
        if self._frame_provider is None or not self._frame_provider.is_open:
            return
        self._request_preview_frame(0, candidate_index=-1, sequential=False)

    def _select_candidate(self, index: int) -> None:
        """
        Select candidate by index.

        Args:
            index: Candidate index.
        """
        if self._updating:
            return

        self._updating = True
        self._current_index = index
        self._model.set_current_index(index)
        self._scene_list.select_row(index)
        self._graph.set_current_candidate_index(index)

        # Update preview
        candidate = self._model.get_candidate(index)
        if candidate:
            # Show candidate info immediately, load frames async
            # （fps 来自 provider，时间码/±1 帧偏移按真实帧率换算）
            self._preview.set_candidate(candidate, fps=self._playback_fps)
            self._transport.set_scrubber_value(candidate.source_frame)
            self._request_preview_frame(candidate.source_frame, index)

        self._updating = False

    def _cancel_active_preview_worker(self) -> None:
        """Ask the previous preview request to stop before a new seek."""
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._preview_worker.requestInterruption()

    def _request_preview_frame(
        self,
        frame: int,
        candidate_index: Optional[int] = None,
        sequential: Optional[bool] = None,
    ) -> None:
        """Load a three-frame preview around ``frame`` asynchronously."""
        if self._frame_provider is None or not self._frame_provider.is_open:
            return

        # 探测运行期间不发起预览解码（见 _start_detection）。
        if self._detection_decoding_paused:
            return

        if sequential is None:
            sequential = self._transport.is_playing()
        request = (
            int(frame),
            self._current_index if candidate_index is None else candidate_index,
            sequential,
        )
        # Keep exactly one decoder request in flight.  A timer or scrubber can
        # fire faster than a long-GOP decode; replacing the pending request is
        # cheaper and safer than creating a thread for every intermediate
        # frame.
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._preview_pending_request = request
            self._preview_worker.requestInterruption()
            return

        self._start_preview_request(*request)

    def _start_preview_request(
        self,
        frame: int,
        candidate_index: Optional[int],
        sequential: bool,
    ) -> None:
        """Start the single allowed preview decode request."""
        if self._frame_provider is None or not self._frame_provider.is_open:
            return
        worker = PreviewLoadWorker(
            self._frame_provider,
            self._current_index if candidate_index is None else candidate_index,
            frame,
            sequential=sequential,
        )
        self._preview_worker = worker
        self._preview_workers.append(worker)
        # AUD-20604: stamp the request with the current source generation so
        # results decoded from a previous source are discarded on arrival.
        generation = self._preview_generation
        worker.frames_ready.connect(
            lambda ci, fn, b, c, a, g=generation: self._on_preview_frames_ready(
                ci, fn, b, c, a, generation=g
            )
        )
        worker.finished.connect(
            lambda worker=worker: self._on_preview_worker_finished(worker)
        )
        worker.start()

    def _on_preview_worker_finished(self, worker: PreviewLoadWorker) -> None:
        """Release a finished preview loader thread safely."""
        if worker in self._preview_workers:
            self._preview_workers.remove(worker)
        if worker in self._zombie_preview_workers:
            self._zombie_preview_workers.remove(worker)
        if self._preview_worker is worker:
            self._preview_worker = None
        # closeEvent 的清理循环与 worker 自身的 finished 信号可能先后
        # 各调用一次本方法；C++ 对象已销毁时跳过即可。
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

        # AUD-20604: the worker no longer holds any provider lock — close the
        # providers whose close was deferred by _init_frame_provider.  Only
        # safe once no other preview worker is still running (a zombie may
        # still hold one of the deferred providers' decode locks).
        if not any(w.isRunning() for w in self._preview_workers):
            while self._providers_pending_close:
                deferred = self._providers_pending_close.pop()
                try:
                    deferred.close()
                except Exception as exc:  # noqa: BLE001 - close must not break cleanup
                    logger.warning(
                        f"SceneCutDialog: deferred provider close failed: {exc}"
                    )

        pending = self._preview_pending_request
        self._preview_pending_request = None
        if pending is not None and self._frame_provider is not None and self._frame_provider.is_open:
            QTimer.singleShot(0, lambda pending=pending: self._start_preview_request(*pending))

    def _on_preview_frames_ready(
        self,
        candidate_index: int,
        frame_number: int,
        before_img: Optional[QImage],
        current_img: Optional[QImage],
        after_img: Optional[QImage],
        generation: Optional[int] = None,
    ) -> None:
        """Handle async preview frames loaded. Only applies if still on same candidate."""
        # AUD-20604: drop results decoded from a previous source.
        if generation is not None and generation != self._preview_generation:
            return
        if candidate_index == -1 and self._current_index == -1:
            if frame_number == self._transport.current_frame():
                self._preview.set_frame_preview(
                    frame_number,
                    self._playback_fps,
                    before_img,
                    current_img,
                    after_img,
                )
            return

        if candidate_index != self._current_index:
            return  # User has moved to a different candidate, discard

        if frame_number != self._transport.current_frame():
            return  # A newer frame request is already active.

        candidate = self._model.get_candidate(candidate_index)
        if candidate:
            if frame_number == candidate.source_frame:
                self._preview.set_candidate(
                    candidate,
                    before_image=before_img,
                    current_image=current_img,
                    after_image=after_img,
                    fps=self._playback_fps,
                )
            else:
                self._preview.set_frame_preview(
                    frame_number,
                    self._playback_fps,
                    before_img,
                    current_img,
                    after_img,
                )

    def _select_first_candidate(self) -> None:
        """Select first candidate."""
        if self._model.has_data:
            self._select_candidate(0)

    def _goto_prev_cut(self) -> None:
        """Go to previous detection point."""
        if self._current_index > 0:
            self._select_candidate(self._current_index - 1)

    def _goto_next_cut(self) -> None:
        """Go to next detection point."""
        if self._current_index < self._model.rowCount() - 1:
            self._select_candidate(self._current_index + 1)

    def _step_prev_frame(self) -> None:
        """Step back one frame."""
        self._seek_to_frame(self._transport.current_frame() - 1)

    def _step_next_frame(self) -> None:
        """Step forward one frame."""
        self._seek_to_frame(self._transport.current_frame() + 1)

    def _seek_to_frame(self, frame: int) -> None:
        """Seek to specific frame."""
        if self._updating:
            return

        last_frame = self._get_playback_max_frame()
        frame = max(0, min(int(frame), last_frame))

        # QSlider emits synchronously.  Let that signal perform the actual
        # update once the value has changed, avoiding duplicate frame loads.
        if frame != self._transport.current_frame():
            self._transport.set_scrubber_value(frame)
            return

        self._graph.set_current_frame(frame)
        self._request_preview_frame(frame)

    def _toggle_play(self) -> None:
        """Toggle play/pause."""
        if self._transport.is_playing():
            self._stop_playback()
            return

        if self._get_playback_max_frame() <= 0:
            return

        self._set_playback_interval()
        self._transport.set_playing(True)
        self._playback_timer.start()

    def _stop_playback(self) -> None:
        """Stop playback and leave the current frame unchanged."""
        self._playback_timer.stop()
        self._transport.set_playing(False)
        # A queued request may have been produced by the playback timer just
        # before pause.  Do not let it jump the image after the user pauses.
        self._preview_pending_request = None

    def _advance_playback(self) -> None:
        """Advance one source frame at the source frame rate."""
        # The preview decoder is deliberately single-flight.  Hold the
        # playhead while the current frame is being decoded instead of
        # submitting a seek for every timer tick.
        if self._preview_worker is not None and self._preview_worker.isRunning():
            return
        if self._preview_pending_request is not None:
            return
        next_frame = self._transport.current_frame() + 1
        if next_frame > self._get_playback_max_frame():
            self._stop_playback()
            return
        self._seek_to_frame(next_frame)

    def _set_playback_interval(self) -> None:
        """Update timer cadence from the source FPS."""
        interval_ms = max(1, round(1000.0 / max(self._playback_fps, 1.0)))
        self._playback_timer.setInterval(interval_ms)

    def _get_playback_max_frame(self) -> int:
        """Return the inclusive last frame available to the transport."""
        if self._frame_provider is not None and self._frame_provider.total_frames > 0:
            return max(0, self._frame_provider.total_frames - 1)
        return max(0, self._get_total_frames() - 1)

    def _sync_playback_source(self) -> None:
        """Sync scrubber range and timer cadence with the opened source."""
        if self._frame_provider is not None and self._frame_provider.is_open:
            self._playback_fps = self._frame_provider.fps or 24.0
            self._transport.set_scrubber_range(self._get_playback_max_frame())
        else:
            self._playback_fps = 24.0
        # AUD-20800: 结果表的 Start TC 列与真实帧率同步（模型默认 24）。
        self._model.set_fps(self._playback_fps)
        self._set_playback_interval()

    def _toggle_list_visibility(self) -> None:
        """Toggle scene list visibility."""
        self._list_visible = self._transport.is_list_visible()
        self._scene_list.setVisible(self._list_visible)

    def _show_help(self) -> None:
        """Open the bilingual Scene Cut usage guide."""
        from src.shared.node_help import has_node_help
        from src.shared.node_help.dialog import NodeHelpDialog

        title = I18N.tr(
            SCENE_CUT_I18N_KEYS["title"],
            default="Scene Detect",
        )
        if not has_node_help("scene_cut"):
            QMessageBox.information(
                self,
                title,
                I18N.tr(
                    "scene_cut.help_unavailable",
                    default="No usage help is available for this tool yet.",
                ),
            )
            return

        dialog = NodeHelpDialog("scene_cut", title, parent=self)
        dialog.exec()

    # ==================== Decision Management ====================

    def _accept_current(self) -> None:
        """Accept current candidate."""
        if self._current_index >= 0:
            self._model.accept_candidate(self._current_index)

    def _reject_current(self) -> None:
        """Reject current candidate."""
        if self._current_index >= 0:
            self._model.reject_candidate(self._current_index)

    def _delete_current_cut(self) -> None:
        """Delete the selected cut and remove its graph marker."""
        if not (0 <= self._current_index < self._model.rowCount()):
            return

        removed_index = self._current_index
        removed = self._model.remove_candidate(removed_index)
        if removed is None:
            return

        # Keep the graph's candidate list in lockstep with the table model;
        # rejected candidates are intentionally still drawn, while deleted
        # candidates must disappear entirely.
        self._sync_graph_candidates()
        self._analysis = self._model.to_updated_analysis() or self._analysis

        next_index = min(removed_index, self._model.rowCount() - 1)
        if next_index >= 0:
            self._select_candidate(next_index)
        else:
            self._current_index = -1
            self._model.set_current_index(-1)
            self._graph.set_current_candidate_index(-1)
            self._graph.set_current_frame(self._transport.current_frame())
            self._preview.set_frame_preview(
                self._transport.current_frame(),
                self._playback_fps,
                None,
                None,
                None,
            )
            self._request_preview_frame(
                self._transport.current_frame(),
                candidate_index=-1,
                sequential=False,
            )
        self._update_ui_counts()

    def _add_manual_cut(self) -> None:
        """Add a manual cut at the current frame and draw its marker."""
        frame = self._transport.current_frame()
        # P1-11: clip-scoped detection is restricted to the analyzed source
        # range; a manual cut outside it would produce subclips/timeline
        # writes beyond the clip. Reject explicitly instead of silently
        # widening the analyzed range.
        range_start = self._settings.start_source_frame
        range_end = self._settings.end_source_frame
        if range_end is not None and not (range_start <= frame < range_end):
            QMessageBox.warning(
                self,
                I18N.tr("scene_cut.out_of_range_title", default="Out of Range"),
                I18N.tr(
                    "scene_cut.out_of_range_message",
                    default=(
                        "Detection is limited to the selected clip "
                        f"(frames {range_start}-{range_end}). "
                        "Move the playhead inside that range first."
                    ),
                ),
            )
            return
        existing = next(
            (candidate for candidate in self._model.candidates if candidate.source_frame == frame),
            None,
        )
        if existing is not None:
            self._select_candidate(self._model.candidates.index(existing))
            return

        candidate = SceneCutCandidate(
            source_frame=frame,
            pts=None,
            seconds=frame / max(self._playback_fps, 1.0),
            score=1.0,
            confidence=1.0,
            kind=SceneCutKind.HARD_CUT,
            decision=SceneCutDecision.ACCEPTED,
            reasons=("manual",),
        )
        index = self._model.insert_candidate(candidate)
        if index < 0:
            return

        self._sync_graph_candidates()
        self._analysis = self._model.to_updated_analysis() or self._analysis
        self._select_candidate(index)
        self.manual_cut_added.emit(frame)

    def _sync_graph_candidates(self) -> None:
        """Refresh graph markers from the current table-model candidates."""
        self._graph.set_candidates(
            list(self._model.candidates),
            self._get_total_frames(),
        )
        self._graph.set_current_candidate_index(self._current_index)

    # ==================== Batch Operations ====================

    def _batch_accept_high_confidence(self) -> None:
        """Batch accept high confidence candidates."""
        count = self._model.accept_high_confidence()
        self._update_ui_counts()
        QMessageBox.information(
            self,
            I18N.tr("scene_cut.batch_complete", default="Batch Complete"),
            _format_count_message(
                SCENE_CUT_I18N_KEYS["accepted_count"],
                "{0} candidates accepted.",
                count,
            ),
        )

    def _batch_reject_warnings(self) -> None:
        """Batch reject warning candidates."""
        count = self._model.reject_warnings()
        self._update_ui_counts()
        QMessageBox.information(
            self,
            I18N.tr("scene_cut.batch_complete", default="Batch Complete"),
            _format_count_message(
                SCENE_CUT_I18N_KEYS["rejected_count"],
                "{0} candidates rejected.",
                count,
            ),
        )

    def _batch_reset_all(self) -> None:
        """Reset all decisions."""
        self._model.reset_all()
        self._update_ui_counts()

    # ==================== Add Cuts to Media Pool ====================

    def _add_cuts_to_pool(self) -> None:
        """Add accepted cuts, or active pending cuts, to media pool."""
        accepted = self.get_accepted_candidates()

        if accepted:
            candidates_to_add = accepted
        else:
            # A fresh detection leaves candidates Pending by design. Use the
            # draggable graph threshold as the review boundary so the button
            # remains useful without silently adding rejected/weak candidates.
            threshold = self._graph.get_threshold()
            candidates_to_add = [
                candidate
                for candidate in self._model.candidates
                if candidate.is_pending and candidate.score >= threshold
            ]

        if not candidates_to_add:
            QMessageBox.warning(
                self,
                I18N.tr(SCENE_CUT_I18N_KEYS["no_accepted_cuts"], default="No Accepted Cuts"),
                I18N.tr(
                    SCENE_CUT_I18N_KEYS["no_accepted_cuts_message"],
                    default="Please accept or activate at least one cut before adding to media pool.",
                ),
            )
            return

        # Confirm action
        reply = QMessageBox.question(
            self,
            I18N.tr(SCENE_CUT_I18N_KEYS["confirm_add_cuts"], default="Confirm"),
            _format_cut_confirmation_message(len(candidates_to_add)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.cuts_added_to_pool.emit(candidates_to_add)
            logger.info(f"Cuts added to pool: {len(candidates_to_add)} subclips")

    # ==================== Signal Handlers ====================

    def _on_decision_changed(self, index: int, decision: SceneCutDecision) -> None:
        """Handle decision change."""
        # Sync updated decisions back to analysis
        updated = self._model.to_updated_analysis()
        if updated is not None:
            self._analysis = updated

        self._update_ui_counts()
        self._sync_graph_candidates()
        self._scene_list.update()  # Refresh display

    def _on_selection_changed(self, index: int) -> None:
        """Handle selection change from model."""
        if index >= 0:
            self._select_candidate(index)

    def _on_sensitivity_changed(self, value: float) -> None:
        """Handle sensitivity change."""
        # Before an analysis exists, use sensitivity as the provisional guide.
        # Once detection has completed, the graph line is the actual adaptive
        # threshold and can be adjusted directly with the mouse.
        if self._analysis is None:
            self._graph.set_threshold(value)

    def _on_graph_threshold_changed(self, value: float) -> None:
        """Handle direct dragging of the graph threshold line."""
        logger.debug("Scene cut graph threshold adjusted to %.3f", value)
        self._update_ui_counts()

    def _on_detection_progress(self, progress: DetectionProgress) -> None:
        """Handle detection progress."""
        self._progress_bar.setValue(progress.progress_percent)

        if progress.candidates_found > 0:
            self._bottom_bar.set_detection_running(True)

    def _resume_proxy_generation(self) -> None:
        """恢复为本次探测挂起的后台代理生成（若有）。"""
        if not self._proxy_generation_paused:
            return
        self._proxy_generation_paused = False
        try:
            from src.shared.media.disk_proxy_service import get_disk_proxy_service

            get_disk_proxy_service().resume_all_generation()
        except Exception as e:
            logger.warning(f"Resume proxy generation failed: {e}")

    def _on_detection_completed(self, analysis: SceneCutAnalysis) -> None:
        """Handle detection completion."""
        # 陈旧信号防护：连续两次探测时，上一轮被取消 worker 的终态信号
        # 可能晚于新一轮启动送达，按发送者身份丢弃旧结果。
        sender = self.sender()
        if sender is not None and sender is not self._worker:
            return
        self._apply_detection_completed(analysis)

    def _apply_detection_completed(self, analysis: SceneCutAnalysis) -> None:
        """应用探测完成结果（worker 信号与缓存命中直调共用，无 sender 校验）。

        AUD-20601: 缓存命中路径此前直调 _on_detection_completed，而该槽内
        的 sender() 在外层按钮信号激活期间返回 BottomActionBar，被陈旧
        信号守卫整体吞掉——缓存结果既不显示也不再启动新探测。
        """
        self._detection_decoding_paused = False
        self._resume_proxy_generation()
        self._analysis = analysis
        self._bottom_bar.set_detection_running(False)
        self._progress_bar.setVisible(False)

        self.set_analysis(analysis)
        self.detection_completed.emit(analysis)

        # Standalone callers may retain the legacy persistent cache behavior;
        # the controller-owned dialog uses session memory instead.
        if self._use_persistent_cache:
            self._save_analysis_to_cache(analysis)

        logger.info(f"Detection completed: {len(analysis.candidates)} candidates")

    def _on_detection_error(self, error: SceneCutError) -> None:
        """Handle detection error."""
        sender = self.sender()
        if sender is not None and sender is not self._worker:
            return
        self._detection_decoding_paused = False
        self._resume_proxy_generation()
        self._bottom_bar.set_detection_running(False)
        self._progress_bar.setVisible(False)
        self._request_preview_frame(self._transport.current_frame())

        QMessageBox.critical(
            self,
            I18N.tr(SCENE_CUT_I18N_KEYS["detection_error"], default="Detection Error"),
            error.message,
        )

        logger.error(f"Detection error: {error.message}")

    def _on_detection_cancelled(self) -> None:
        """Handle detection cancellation."""
        sender = self.sender()
        if sender is not None and sender is not self._worker:
            return
        self._detection_decoding_paused = False
        self._resume_proxy_generation()
        self._bottom_bar.set_detection_running(False)
        self._progress_bar.setVisible(False)
        self._request_preview_frame(self._transport.current_frame())

        logger.info("Detection cancelled")

    # ==================== Helper Methods ====================

    def _get_total_frames(self) -> int:
        """Get total frames from analysis or default."""
        if self._analysis and self._analysis.source_time_index:
            return len(self._analysis.source_time_index.points)
        return 1000  # Default placeholder

    def _update_ui_counts(self) -> None:
        """Update UI with current counts."""
        has_active_pending = any(
            candidate.is_pending
            and candidate.score >= self._graph.get_threshold()
            for candidate in self._model.candidates
        )
        self._bottom_bar.set_add_cuts_enabled(
            self._model.accepted_count > 0 or has_active_pending
        )

    # ==================== Keyboard Handling ====================

    def _register_shortcuts(self) -> None:
        """Ensure standalone dialog construction still registers shortcuts."""
        manager = None
        try:
            from src.shared.ui.shortcut_manager import ShortcutManager

            manager = ShortcutManager.instance()
            if manager is not None:
                scene_cut_shortcuts.register_scene_cut_shortcuts(manager)
        except Exception:  # pragma: no cover - settings must not block dialog
            logger.debug("Scene Cut shortcut registration skipped", exc_info=True)

        # Arrow keys are transport controls, not application commands.  Bind
        # them to this dialog's widget tree so a focused scrubber/list cannot
        # consume them before the QApplication event filter sees them.  The
        # key sequences still come from ShortcutManager and are refreshed when
        # the user changes them in Preferences.
        self._frame_step_shortcuts = (
            QShortcut(self),
            QShortcut(self),
        )
        for shortcut in self._frame_step_shortcuts:
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._frame_step_shortcuts[0].activated.connect(self._step_prev_frame)
        self._frame_step_shortcuts[1].activated.connect(self._step_next_frame)
        self._apply_frame_step_shortcuts()

        app = QApplication.instance()
        if manager is not None:
            manager.shortcuts_changed.connect(self._apply_frame_step_shortcuts)
        if app is not None:
            app.focusChanged.connect(self._update_frame_step_shortcuts_enabled)

    def _apply_frame_step_shortcuts(self) -> None:
        """Apply configured previous/next frame sequences to local shortcuts."""
        if len(self._frame_step_shortcuts) != 2:
            return
        self._frame_step_shortcuts[0].setKey(
            scene_cut_shortcuts.configured_shortcut(
                scene_cut_shortcuts.STEP_PREVIOUS_FRAME_ACTION_ID,
                "Left",
            )
        )
        self._frame_step_shortcuts[1].setKey(
            scene_cut_shortcuts.configured_shortcut(
                scene_cut_shortcuts.STEP_NEXT_FRAME_ACTION_ID,
                "Right",
            )
        )
        self._update_frame_step_shortcuts_enabled()

    def _update_frame_step_shortcuts_enabled(
        self,
        _old_focus: Optional[QWidget] = None,
        _new_focus: Optional[QWidget] = None,
    ) -> None:
        """Keep arrow shortcuts out of text/value editors."""
        if not self._frame_step_shortcuts:
            return
        enabled = not self._is_text_editing_focus(QApplication.focusWidget())
        for shortcut in self._frame_step_shortcuts:
            shortcut.setEnabled(enabled)

    def _install_shortcut_filter(self) -> None:
        """Install a dialog-local event filter for configurable shortcuts."""
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._shortcut_filter_installed = True

    @staticmethod
    def _is_text_editing_focus(widget: Optional[QWidget]) -> bool:
        """Keep transport keys available to active text/value editors."""
        return isinstance(
            widget,
            (QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit),
        )

    def _shortcut_target_is_active(self, watched: Any) -> bool:
        """Return whether an event belongs to this active dialog."""
        if not self.isVisible() or not self.isActiveWindow():
            return False
        if not isinstance(watched, QWidget):
            return False
        return watched is self or watched.window() is self or self.isAncestorOf(watched)

    def _handle_shortcut_event(self, event: QKeyEvent) -> bool:
        """Handle a manager-configured Scene Cut shortcut event."""
        if self._is_text_editing_focus(QApplication.focusWidget()):
            return False

        if scene_cut_shortcuts.event_matches(
            event,
            scene_cut_shortcuts.PLAY_PAUSE_ACTION_ID,
            "Space",
        ):
            if not event.isAutoRepeat():
                self._toggle_play()
                return True
            return False

        shortcut_handlers = (
            (
                scene_cut_shortcuts.STEP_PREVIOUS_FRAME_ACTION_ID,
                "Left",
                self._step_prev_frame,
            ),
            (
                scene_cut_shortcuts.STEP_NEXT_FRAME_ACTION_ID,
                "Right",
                self._step_next_frame,
            ),
            (
                scene_cut_shortcuts.PREVIOUS_CUT_ACTION_ID,
                "Up",
                self._goto_prev_cut,
            ),
            (
                scene_cut_shortcuts.NEXT_CUT_ACTION_ID,
                "Down",
                self._goto_next_cut,
            ),
            (
                scene_cut_shortcuts.ACCEPT_CURRENT_ACTION_ID,
                "Enter",
                self._accept_current,
            ),
            (
                scene_cut_shortcuts.REJECT_CURRENT_ACTION_ID,
                "Delete",
                self._reject_current,
            ),
        )
        for action_id, default, handler in shortcut_handlers:
            if scene_cut_shortcuts.event_matches(event, action_id, default):
                handler()
                return True
        return False

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802
        """Catch transport keys before child views consume arrow/space."""
        if (
            event.type() == QEvent.Type.KeyPress
            and self._shortcut_target_is_active(watched)
            and self._handle_shortcut_event(event)
        ):
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """
        Handle keyboard shortcuts.

        - Enter: Accept current
        - Delete/Backspace: Reject current
        - Up: Previous candidate
        - Down: Next candidate
        - Space: Toggle play
        """
        if self._handle_shortcut_event(event):
            event.accept()
            return
        super().keyPressEvent(event)

    # ==================== Cleanup ====================

    def closeEvent(self, event: Any) -> None:
        """Handle close event."""
        self._stop_playback()
        if self._shortcut_filter_installed:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
                try:
                    app.focusChanged.disconnect(self._update_frame_step_shortcuts_enabled)
                except (TypeError, RuntimeError):
                    pass
            self._shortcut_filter_installed = False

        # Cancel any running detection
        if self._worker and self._worker.is_running:
            self._worker.cancel()
            # 有界等待；超时则按 zombie 模式放弃，避免销毁运行中的
            # QThread（与 AUD-20603 的 _start_detection 路径一致）。
            if not self._worker.wait(1000):
                self._abandon_detection_worker(self._worker)

        # 对话框可能在 worker 的终态信号送达前销毁（queued connection），
        # 这里直接兜底恢复被挂起的后台代理生成。
        self._detection_decoding_paused = False
        self._resume_proxy_generation()

        for worker in list(self._preview_workers):
            if worker.isRunning():
                worker.requestInterruption()
                # 必须检查 wait() 返回值：PreviewLoadWorker.run 只在解码
                # 前后检查中断标志，长 GOP/4K/网络媒体的单帧解码可远超
                # 1 秒且无法中断。若忽略返回值直接走清理，会对仍在
                # 运行的 QThread 调 deleteLater —— 析构运行中的线程属
                # 未定义行为（"QThread: Destroyed while thread is still
                # running"），典型表现为进程崩溃。
                if not worker.wait(1000):
                    # 超时未退出：保引用转入 zombie 列表，交给其自身
                    # finished 信号（_start_preview_request 已连接）在
                    # 线程真正结束后调用 _on_preview_worker_finished
                    # 完成清理。此处不做任何销毁。
                    self._zombie_preview_workers.append(worker)
                    logger.warning(
                        "SceneCutDialog: preview worker still decoding at "
                        "close; deferring cleanup until it exits naturally"
                    )
                    continue
            self._on_preview_worker_finished(worker)

        if self._frame_provider is not None:
            self._frame_provider.close()

        # AUD-20604: drain providers whose close was deferred by a still
        # decoding worker.  Only close them once no preview worker is running,
        # otherwise close() could block on the worker's decode lock here.
        if not any(worker.isRunning() for worker in self._preview_workers):
            while self._providers_pending_close:
                deferred = self._providers_pending_close.pop()
                try:
                    deferred.close()
                except Exception as exc:  # noqa: BLE001 - close must not break cleanup
                    logger.warning(f"SceneCutDialog: deferred provider close failed: {exc}")
        else:
            logger.warning(
                "SceneCutDialog: preview worker still decoding at close; "
                "leaving deferred provider cleanup to its finished callback"
            )

        super().closeEvent(event)

    def reject(self) -> None:
        """Handle dialog rejection (Escape key)."""
        self.close()


# ================================================================
# Factory Functions
# ================================================================

def create_scene_cut_dialog(
    source_path: str,
    asset_id: str,
    settings: Optional[SceneCutSettings] = None,
    proxy_path: Optional[str] = None,
    parent: Optional[QWidget] = None,
) -> SceneCutDialog:
    """
    Create scene cut dialog with source set.

    Args:
        source_path: Source video path.
        asset_id: Asset identifier.
        settings: Optional settings.
        proxy_path: Optional proxy media path.
        parent: Optional parent widget.

    Returns:
        Configured SceneCutDialog.
    """
    return SceneCutDialog(
        source_path=source_path,
        asset_id=asset_id,
        settings=settings,
        proxy_path=proxy_path,
        parent=parent,
    )


def show_scene_cut_dialog(
    source_path: str,
    asset_id: str,
    settings: Optional[SceneCutSettings] = None,
    proxy_path: Optional[str] = None,
    parent: Optional[QWidget] = None,
) -> SceneCutDialog:
    """
    Create and show scene cut dialog.

    Args:
        source_path: Source video path.
        asset_id: Asset identifier.
        settings: Optional settings.
        proxy_path: Optional proxy media path.
        parent: Optional parent widget.

    Returns:
        Shown SceneCutDialog.
    """
    dialog = create_scene_cut_dialog(
        source_path,
        asset_id,
        settings,
        proxy_path=proxy_path,
        parent=parent,
    )
    dialog.show()
    return dialog
