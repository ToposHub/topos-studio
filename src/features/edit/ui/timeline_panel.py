"""
Timeline Panel - Timeline view and editing tools.

This panel contains the timeline toolbar and timeline view for editing.
P4/P6 phase: Integrated with TimelineView (QGraphicsView).

Transport Controls (moved to ProgramViewerWidget's TransportControlBar):
- Play/Pause, frame stepping, jump, loop, timecode are in ProgramViewerWidget
- JKL shuttle control (J=backward, K=pause, L=forward, repeated=accelerate)
- Space key global play/pause
- Arrow keys for frame stepping
- Playhead auto-scroll during playback

Toolbar Structure (single row):
- Display options | selection / trim / blade / more tools | source edits
- Snapping / linked selection / position lock | markers
- Full / detail / custom zoom | slider | live audio meter | overflow
"""

import logging
from typing import Optional, Any, List
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QToolButton,
    QSlider, QFrame, QLineEdit, QDialog, QDialogButtonBox,
    QApplication, QTreeWidgetItem, QMenu, QWidgetAction, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QSize
import math
from PyQt6.QtGui import QAction, QActionGroup, QShortcut, QKeySequence

from src.shared.i18n.i18n import I18N
from src.shared.ui.panel_divider import PanelDockWidget
from src.shared.ui.shortcut_scope import shortcut_owner_window_active, text_input_focused
from src.features.edit.ui.timeline_view import TimelineView, EditTool, MIN_ZOOM, MAX_ZOOM, DEFAULT_ZOOM
from src.features.edit.ui.timeline_items import TRACK_HEADER_WIDTH
from src.features.edit.ui.edit_style import EDIT_COLORS, EDIT_SIZES
from src.shared.assets.icon_manager import (
    icon_ripple, icon_roll, icon_slip, icon_slide, icon_hand_tool, icon_zoom_tool,
    icon_zoom_in, icon_zoom_out,
    icon_append_edit,
    get_icon, IconColors
)
from src.features.edit.ui.timeline_icons import (
    timeline_icon, icon_select_tool, icon_trim_tool, icon_blade_tool,
    icon_snap, icon_link, icon_unlink,
    icon_insert_edit, icon_overwrite_edit, icon_marker,
)
from src.features.edit.core.edit_playback_controller import (
    EditPlaybackController,
    PlaybackControllerTimer,
    PlaybackState,
    PlaybackDirection,
)
from src.features.edit.core.playback_debug import playback_trace, reset_playback_trace
from src.features.edit.core.timeline_v2 import effective_timeline_duration_frames

logger = logging.getLogger(__name__)

# AUD-21306: TTL for the proxy-HUD stats cache (seconds). Diagnostics data,
# so a slightly stale ready/generating/missing split is acceptable in
# exchange for not re-running up to 200 disk validations per HUD tick.
_PROXY_HUD_CACHE_TTL_S = 10.0


# =============================================================================
# Constants
# =============================================================================

# Playback speed multipliers for JKL control
PLAYBACK_SPEEDS = [1.0, 2.0, 4.0, 8.0]

# Default FPS for an empty timeline and timecode calculation
DEFAULT_FPS = 25.0

# At the zoom slider's minimum, keep the longest timeline content compact so
# the user has room to navigate around it.  This is measured against the
# timeline content area (excluding the fixed track-header column).
MIN_ZOOM_CONTENT_FILL_RATIO = 0.10


# =============================================================================
# TimelinePanel
# =============================================================================

class TimelinePanel(QWidget):
    """
    Timeline panel for editing timeline sequences.

    Transport Controls:
    - Play/Pause: Start/stop playback, playhead moves at fps speed
    - Previous/Next frame: Step one frame at a time
    - Jump to start/end: Go to frame 0 or last frame
    - Loop: Toggle loop playback mode
    - JKL: Shuttle control (J=backward, K=pause, L=forward, repeat=accelerate)
    - Space: Global play/pause toggle
    - Arrow keys: Frame stepping
    - Timecode: Click to edit and jump to specific frame

    Signals:
        play_requested: User requested playback
        pause_requested: User requested pause
        seek_requested: User requested seek to frame
        tool_changed: User changed editing tool
        playback_state_changed: Playback state changed (is_playing, speed, direction)
    """

    # Signals for external coordination
    play_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    seek_requested = pyqtSignal(int)  # frame number
    tool_changed = pyqtSignal(str)  # tool name
    playback_state_changed = pyqtSignal(bool, float, int)  # is_playing, speed, direction (1=forward, -1=backward)
    playback_reanchored = pyqtSignal(int)  # stable frame after render backpressure
    timeline_changed = pyqtSignal(object)  # TimelineSequence - forwarded from TimelineView
    project_changed = pyqtSignal(object)  # EditProject - forwarded from TimelineView external imports
    project_needed = pyqtSignal()  # Forwarded request to auto-create a project for file drops
    # T7: Open in Color via right-click context menu
    open_in_color_requested = pyqtSignal(str, str, str)  # clip_id, track_id, stack_id

    # Phase 2: Comp Clip context menu signals (forwarded from TimelineView)
    create_comp_clip_requested = pyqtSignal(str, str)  # clip_id, track_id
    reset_composite_requested = pyqtSignal(str, str)  # clip_id, track_id
    remove_composite_binding_requested = pyqtSignal(str, str)  # clip_id, track_id
    bypass_composite_requested = pyqtSignal(str, str)  # clip_id, track_id
    reveal_comp_clip_in_media_pool_requested = pyqtSignal(str, str)  # clip_id, track_id
    render_cache_clip_requested = pyqtSignal(str, str)  # clip_id, track_id
    rerender_cache_clip_requested = pyqtSignal(str, str)  # clip_id, track_id
    # P9.1: 共享素材调色 / 本地调色副本
    use_shared_grade_requested = pyqtSignal(str, str)  # clip_id, track_id
    make_local_grade_copy_requested = pyqtSignal(str, str)  # clip_id, track_id
    teaching_animation_open_requested = pyqtSignal(str, str)
    # P1-02: 嵌套序列工作流（转发自 TimelineView）
    create_nested_from_selection_requested = pyqtSignal(list)  # clip_ids
    enter_nested_sequence_requested = pyqtSignal(str, str)  # child_timeline_id, clip_id
    # P1-01: 多机位角度键（转发自 TimelineView）
    multicam_angle_key_requested = pyqtSignal(int)

    # P10.3: Subtitle clip operations (forwarded from TimelineView)
    add_subtitle_clip_requested = pyqtSignal(str, int)  # track_id, at_frame
    subtitle_export_requested = pyqtSignal(str, str)  # track_id, format
    subtitle_quality_requested = pyqtSignal(str)  # track_id
    subtitle_language_requested = pyqtSignal(str)  # track_id
    subtitle_clip_clicked = pyqtSignal(str, str)  # clip_id, track_id
    subtitle_clip_drag_finished = pyqtSignal(str, str, int, int, str, bool)
    # clip_id, source_track_id, old_start, new_start, target_track_id, copy_requested
    subtitle_clip_trim_left_finished = pyqtSignal(str, str, int, int, int, int)  # clip_id, track_id, old_start, old_dur, new_start, new_dur
    subtitle_clip_trim_right_finished = pyqtSignal(str, str, int, int)  # clip_id, track_id, old_dur, new_dur
    subtitle_clip_delete = pyqtSignal(str, str)  # clip_id, track_id
    caption_graphic_action_requested = pyqtSignal(str, str)  # clip_id, action
    caption_track_graphic_template_requested = pyqtSignal(str, str)  # track_id, template_id
    subtitle_clip_edit_text_requested = pyqtSignal(str, str, str)  # clip_id, track_id, new_text

    # Source edit operations (forwarded from toolbar buttons to EditPage)
    source_insert_requested = pyqtSignal()
    source_overwrite_requested = pyqtSignal()
    source_append_requested = pyqtSignal()
    source_replace_requested = pyqtSignal()  # UI request; host supplies replace semantics
    clip_speed_settings_requested = pyqtSignal(str, str, object)  # advanced speed draft
    source_patch_changed = pyqtSignal(str, str, bool)
    keyframe_tray_requested = pyqtSignal(bool)
    voiceover_requested = pyqtSignal()
    flag_requested = pyqtSignal(str)  # chosen flag color; host commits clip metadata

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        
        # Timeline metadata
        self._fps: float = DEFAULT_FPS
        self._total_frames: int = 0
        self._zoom_background_resume_token: int = 0
        # Slider motion can emit hundreds of values per second.  Keep the
        # latest value and apply it on the next display-sized interval instead
        # of rebuilding the entire QGraphicsScene for every pixel crossed.
        self._pending_slider_zoom: Optional[float] = None
        self._applying_slider_zoom: bool = False
        self._zoom_apply_timer: QTimer = QTimer(self)
        self._zoom_apply_timer.setSingleShot(True)
        self._zoom_apply_timer.setInterval(16)
        self._zoom_apply_timer.timeout.connect(self._apply_pending_slider_zoom)
        # The slider's overview endpoint is recalculated from the actual last
        # clip whenever the timeline changes.  TimelineView still accepts the
        # global MIN_ZOOM for wheel/keyboard navigation beyond that endpoint.
        self._slider_min_zoom: float = MIN_ZOOM
        self._slider_max_zoom: float = MAX_ZOOM
        self._fit_first_content_pending: bool = False
        
        # EditPlaybackController (P2.3)
        self._playback_controller: EditPlaybackController = EditPlaybackController(self)
        self._playback_timer: PlaybackControllerTimer = PlaybackControllerTimer(
            self._playback_controller, self
        )
        # E06: playback_state_changed 桥接的去重状态（初始 False=未播放）
        self._last_emitted_playback_state: bool = False
        self._preview_renderer: Optional[Any] = None
        self._scrubbing_playhead: bool = False
        self._project: Optional[Any] = None
        # P1-23: 播放性能 HUD（懒创建，toggle_playback_hud 控制）
        self._playback_hud: Optional[Any] = None

        # Connect controller signals
        self._playback_controller.frame_changed.connect(self._on_controller_frame_changed)
        self._playback_controller.state_changed.connect(self._on_controller_state_changed)
        self._playback_controller.speed_changed.connect(self._on_controller_speed_changed)
        self._playback_controller.direction_changed.connect(self._on_controller_direction_changed)
        self._playback_controller.end_reached.connect(self._on_controller_end_reached)
        
        # Connect playback timer
        self._playback_timer.frame_ready.connect(self._on_timer_frame_ready)
        
        # JKL reset timer (for resetting JKL acceleration after idle)
        self._jkl_timer: QTimer = QTimer(self)
        self._jkl_timer.setSingleShot(True)
        self._jkl_timer.timeout.connect(self._on_jkl_reset_timeout)
        
        # Anti-recursion lock
        self._updating: bool = False
        # Deliver temporarily reuses this widget.  Its reduced toolbar is a
        # display-only mode; the original Edit toolbar visibility is restored
        # when the panel returns to the Edit dock.
        self._toolbar_display_mode: str = "edit"
        self._toolbar_visibility_backup: Optional[dict[QWidget, bool]] = None
        self._deliver_range_type: Optional[str] = None
        
        # Setup UI
        self._setup_ui()

        # Setup keyboard shortcuts
        self._setup_shortcuts()

        # B8: restore the persisted global insert/overwrite drop mode
        self._restore_edit_mode()

        logger.debug("TimelinePanel initialized with EditPlaybackController")

    def _setup_ui(self) -> None:
        """Set up the panel UI structure with TimelineView integration."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 按钮尺寸常量
        BTN_SIZE = EDIT_SIZES["icon_btn"]  # 26
        ICON_SIZE = 18

        # Zoom state
        self._zoom_updating = False  # Anti-recursion lock

        # AUD-21306: (asset_set_hash, monotonic_ts, stats) cache for the
        # proxy-HUD stats so the 500ms HUD poll does not re-run up to 200
        # locked disk validations per tick on the UI thread.
        self._proxy_hud_cache: Optional[Any] = None

        # Tool button map cache — per-instance (NOT a class attribute).
        # Storing as class attr caused cross-instance leakage when multiple
        # TimelinePanel instances existed (multi-window / tests): the first
        # instance to lazy-init would shadow the class attr on the instance
        # dict, and `is None` checks on later instances would read the
        # already-populated class attr and reuse stale button references.
        self._tool_button_map: Optional[List[tuple]] = None

        # ==================== Tools Toolbar (single row) ====================
        tools_bar = QHBoxLayout()
        tools_bar.setSpacing(2)
        self._tools_bar = tools_bar
        self._deliver_timeline_controls: Optional[QWidget] = None

        # --- Tool Group (mutually exclusive) ---
        # Tool order: Select(A) Trim(T) Blade(B) Ripple(R) Roll(W) Slip(Y) Slide(U) Hand(H) Zoom(Z)
        
        # Select tool
        self.btn_select = QToolButton()
        self.btn_select.setToolTip(I18N.tr("edit.timeline_tools.select_tooltip", "Select Tool (A)"))
        self.btn_select.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_select.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_select.setIcon(icon_select_tool(IconColors.DEFAULT))
        self.btn_select.setCheckable(True)
        self.btn_select.setChecked(True)
        self.btn_select.clicked.connect(lambda: self._set_tool(EditTool.SELECT))
        tools_bar.addWidget(self.btn_select)

        # Trim tool
        self.btn_trim = QToolButton()
        self.btn_trim.setToolTip(I18N.tr("edit.timeline_tools.trim_tooltip", "Trim Tool (T)"))
        self.btn_trim.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_trim.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_trim.setIcon(icon_trim_tool(IconColors.DEFAULT))
        self.btn_trim.setCheckable(True)
        self.btn_trim.clicked.connect(lambda: self._set_tool(EditTool.TRIM))
        tools_bar.addWidget(self.btn_trim)

        # Blade tool
        self.btn_blade = QToolButton()
        self.btn_blade.setToolTip(I18N.tr("edit.timeline_tools.blade_tooltip", "Blade Tool (B)"))
        self.btn_blade.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_blade.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_blade.setIcon(icon_blade_tool(IconColors.DEFAULT))
        self.btn_blade.setCheckable(True)
        self.btn_blade.clicked.connect(lambda: self._set_tool(EditTool.BLADE))
        tools_bar.addWidget(self.btn_blade)

        # Ripple tool
        self.btn_ripple = QToolButton()
        self.btn_ripple.setToolTip(I18N.tr("edit.timeline_tools.ripple_tooltip", "Ripple Tool (R)"))
        self.btn_ripple.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_ripple.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_ripple.setIcon(icon_ripple(IconColors.DEFAULT))
        self.btn_ripple.setCheckable(True)
        self.btn_ripple.clicked.connect(lambda: self._set_tool(EditTool.RIPPLE))
        tools_bar.addWidget(self.btn_ripple)

        # B4: Rate Stretch 工具（拖边缘改时长 → 速度换算，Shift+R）
        self.btn_rate_stretch = QToolButton()
        self.btn_rate_stretch.setToolTip(I18N.tr(
            "edit.timeline_tools.rate_stretch_tooltip",
            "Rate Stretch Tool (Shift+R) — 拖动片段边缘改变时长，速度按比例换算",
        ))
        self.btn_rate_stretch.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_rate_stretch.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_rate_stretch.setIcon(icon_ripple(IconColors.DEFAULT))
        self.btn_rate_stretch.setCheckable(True)
        self.btn_rate_stretch.clicked.connect(
            lambda: self._set_tool(EditTool.RATE_STRETCH))
        tools_bar.addWidget(self.btn_rate_stretch)

        # Roll tool
        self.btn_roll = QToolButton()
        self.btn_roll.setToolTip(I18N.tr("edit.timeline_tools.roll_tooltip", "Roll Tool (W)"))
        self.btn_roll.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_roll.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_roll.setIcon(icon_roll(IconColors.DEFAULT))
        self.btn_roll.setCheckable(True)
        self.btn_roll.clicked.connect(lambda: self._set_tool(EditTool.ROLL))
        tools_bar.addWidget(self.btn_roll)

        # Slip tool
        self.btn_slip = QToolButton()
        self.btn_slip.setToolTip(I18N.tr("edit.timeline_tools.slip_tooltip", "Slip Tool (Y)"))
        self.btn_slip.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_slip.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_slip.setIcon(icon_slip(IconColors.DEFAULT))
        self.btn_slip.setCheckable(True)
        self.btn_slip.clicked.connect(lambda: self._set_tool(EditTool.SLIP))
        tools_bar.addWidget(self.btn_slip)

        # Slide tool
        self.btn_slide = QToolButton()
        self.btn_slide.setToolTip(I18N.tr("edit.timeline_tools.slide_tooltip", "Slide Tool (U)"))
        self.btn_slide.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_slide.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_slide.setIcon(icon_slide(IconColors.DEFAULT))
        self.btn_slide.setCheckable(True)
        self.btn_slide.clicked.connect(lambda: self._set_tool(EditTool.SLIDE))
        tools_bar.addWidget(self.btn_slide)

        # Hand tool
        self.btn_hand = QToolButton()
        self.btn_hand.setToolTip(I18N.tr("edit.timeline_tools.hand_tooltip", "Hand Tool (H)"))
        self.btn_hand.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_hand.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_hand.setIcon(icon_hand_tool(IconColors.DEFAULT))
        self.btn_hand.setCheckable(True)
        self.btn_hand.clicked.connect(lambda: self._set_tool(EditTool.HAND))
        tools_bar.addWidget(self.btn_hand)

        # Zoom tool
        self.btn_zoom = QToolButton()
        self.btn_zoom.setToolTip(I18N.tr("edit.timeline_tools.zoom_tooltip", "Zoom Tool (Z)"))
        self.btn_zoom.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_zoom.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_zoom.setIcon(icon_zoom_tool(IconColors.DEFAULT))
        self.btn_zoom.setCheckable(True)
        self.btn_zoom.clicked.connect(lambda: self._set_tool(EditTool.ZOOM))
        tools_bar.addWidget(self.btn_zoom)

        # Separator (visual)
        separator2 = QFrame()
        separator2.setFrameShape(QFrame.Shape.VLine)
        separator2.setStyleSheet(f"color: {EDIT_COLORS['border_subtle']};")
        separator2.setFixedWidth(10)
        tools_bar.addWidget(separator2)

        # --- Toggle Group ---
        # Snap toggle
        self.btn_snap = QToolButton()
        self.btn_snap.setToolTip(I18N.tr("edit.timeline_tools.snap_tooltip", "Snap (N)"))
        self.btn_snap.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_snap.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_snap.setIcon(icon_snap(IconColors.DEFAULT))
        self.btn_snap.setCheckable(True)
        self.btn_snap.setChecked(True)
        self.btn_snap.clicked.connect(self._on_snap_toggled)
        tools_bar.addWidget(self.btn_snap)

        # Linked selection toggle
        self.btn_linked_selection = QToolButton()
        self.btn_linked_selection.setToolTip(I18N.tr("tooltip.linked_selection", "Linked Selection — toggles linked A/V clip drag"))
        self.btn_linked_selection.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_linked_selection.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_linked_selection.setIcon(icon_link(IconColors.DEFAULT))
        self.btn_linked_selection.setCheckable(True)
        self.btn_linked_selection.setChecked(True)
        self.btn_linked_selection.clicked.connect(self._on_linked_selection_toggled)
        tools_bar.addWidget(self.btn_linked_selection)

        # Marker toggle — toggles marker list panel
        self.btn_marker = QToolButton()
        self.btn_marker.setToolTip(I18N.tr("tooltip.show_markers", "Show Marker List"))
        self.btn_marker.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_marker.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_marker.setIcon(icon_marker(IconColors.DEFAULT))
        self.btn_marker.setCheckable(True)
        self.btn_marker.clicked.connect(self._on_marker_toggled)
        tools_bar.addWidget(self.btn_marker)

        # Separator before source edit buttons
        separator3 = QFrame()
        separator3.setFrameShape(QFrame.Shape.VLine)
        separator3.setStyleSheet(f"color: {EDIT_COLORS['border_subtle']};")
        separator3.setFixedWidth(10)
        tools_bar.addWidget(separator3)

        # --- Source Edit Buttons (Insert / Overwrite / Append) ---
        self.btn_insert = QToolButton()
        self.btn_insert.setToolTip(I18N.tr("edit.source_viewer.insert_tooltip", "Insert clip at playhead"))
        self.btn_insert.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_insert.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_insert.setIcon(icon_insert_edit(IconColors.DEFAULT))
        self.btn_insert.setEnabled(False)
        self.btn_insert.clicked.connect(self.source_insert_requested.emit)
        tools_bar.addWidget(self.btn_insert)

        self.btn_overwrite = QToolButton()
        self.btn_overwrite.setToolTip(I18N.tr("edit.source_viewer.overwrite_tooltip", "Overwrite clip at playhead"))
        self.btn_overwrite.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_overwrite.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_overwrite.setIcon(icon_overwrite_edit(IconColors.DEFAULT))
        self.btn_overwrite.setEnabled(False)
        self.btn_overwrite.clicked.connect(self.source_overwrite_requested.emit)
        tools_bar.addWidget(self.btn_overwrite)

        self.btn_append = QToolButton()
        self.btn_append.setToolTip(I18N.tr("edit.source_viewer.append_tooltip", "Append clip at end of timeline"))
        self.btn_append.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_append.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_append.setIcon(icon_append_edit(IconColors.DEFAULT))
        self.btn_append.setEnabled(False)
        self.btn_append.clicked.connect(self.source_append_requested.emit)
        tools_bar.addWidget(self.btn_append)

        # B8: global edit mode toggle — governs media-pool drop semantics
        # (Overwrite=默认安全；Insert=拖放落点波纹)。状态经 ConfigManager 持久化。
        self.btn_edit_mode = QToolButton()
        self.btn_edit_mode.setCheckable(True)
        self.btn_edit_mode.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.btn_edit_mode.clicked.connect(self.toggle_edit_mode)
        tools_bar.addWidget(self.btn_edit_mode)

        # Push tools left, zoom controls right
        tools_bar.addStretch()

        # --- Audio Level Meter (right side, before zoom) ---
        self._audio_level_meter = None
        try:
            from src.shared.ui.audio_mixer.audio_level_meter import AudioLevelMeter, TickLabelsMode
            self._audio_level_meter = AudioLevelMeter(
                orientation=Qt.Orientation.Horizontal,
                tick_labels_mode=TickLabelsMode.HIDE,
                show_clipping=True,
                compact_mode=True,
            )
            self._audio_level_meter.setFixedWidth(140)
            self._audio_level_meter.setFixedHeight(BTN_SIZE + 2)
            self._audio_level_meter.setToolTip(I18N.tr("edit.timeline_tools.audio_level", "Audio Level"))
            tools_bar.addWidget(self._audio_level_meter, alignment=Qt.AlignmentFlag.AlignVCenter)
        except ImportError:
            pass

        # Separator between audio controls and zoom controls
        separator_audio = QFrame()
        separator_audio.setFrameShape(QFrame.Shape.VLine)
        separator_audio.setStyleSheet(f"color: {EDIT_COLORS['border_subtle']};")
        separator_audio.setFixedWidth(10)
        tools_bar.addWidget(separator_audio)

        # --- Mixer Toggle Button ---
        self.btn_mixer = QToolButton()
        self.btn_mixer.setToolTip(I18N.tr("edit.timeline_tools.audio_mixer", "Audio Mixer"))
        self.btn_mixer.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_mixer.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_mixer.setIcon(get_icon('fa5s.sliders-h', IconColors.DEFAULT))
        self.btn_mixer.setCheckable(True)
        self.btn_mixer.clicked.connect(self._toggle_mixer_panel)
        tools_bar.addWidget(self.btn_mixer)

        # Separator between audio group and zoom controls
        separator_zoom = QFrame()
        separator_zoom.setFrameShape(QFrame.Shape.VLine)
        separator_zoom.setStyleSheet(f"color: {EDIT_COLORS['border_subtle']};")
        separator_zoom.setFixedWidth(10)
        tools_bar.addWidget(separator_zoom)

        # --- Zoom Controls (right side) ---
        # Zoom out
        self.btn_zoom_out = QToolButton()
        self.btn_zoom_out.setToolTip(I18N.tr("edit.timeline_tools.zoom_out", "Zoom Out"))
        self.btn_zoom_out.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_zoom_out.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_zoom_out.setIcon(icon_zoom_out(IconColors.DEFAULT))
        self.btn_zoom_out.clicked.connect(self._on_zoom_out_clicked)
        tools_bar.addWidget(self.btn_zoom_out)

        # Zoom slider (logarithmic mapping)
        self._log_min = math.log(self._slider_min_zoom)
        self._log_max = math.log(self._slider_max_zoom)
        self._log_range = self._log_max - self._log_min
        self._zoom_slider_max = 1000
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(0, self._zoom_slider_max)
        self.zoom_slider.setValue(self._ppf_to_slider(DEFAULT_ZOOM))
        self.zoom_slider.setFixedWidth(100)
        self.zoom_slider.setToolTip(I18N.tr("edit.timeline_tools.timeline_zoom", "Timeline Zoom"))
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        self.zoom_slider.sliderReleased.connect(self._on_zoom_slider_released)
        tools_bar.addWidget(self.zoom_slider)

        # Zoom in
        self.btn_zoom_in = QToolButton()
        self.btn_zoom_in.setToolTip(I18N.tr("edit.timeline_tools.zoom_in", "Zoom In"))
        self.btn_zoom_in.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_zoom_in.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_zoom_in.setIcon(icon_zoom_in(IconColors.DEFAULT))
        self.btn_zoom_in.clicked.connect(self._on_zoom_in_clicked)
        tools_bar.addWidget(self.btn_zoom_in)

        # Fit to view
        self.btn_zoom_fit = QToolButton()
        self.btn_zoom_fit.setToolTip(I18N.tr("edit.timeline_tools.zoom_fit", "Fit to View (F)"))
        self.btn_zoom_fit.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_zoom_fit.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_zoom_fit.setIcon(get_icon('fa5s.expand', IconColors.DEFAULT))
        self.btn_zoom_fit.clicked.connect(self._on_zoom_fit_clicked)
        tools_bar.addWidget(self.btn_zoom_fit)

        # Apply clean button style (transparent, no heavy backgrounds)
        self._timeline_toolbar_widgets = [
            self.btn_select,
            self.btn_trim,
            self.btn_blade,
            self.btn_ripple,
            self.btn_rate_stretch,
            self.btn_roll,
            self.btn_slip,
            self.btn_slide,
            self.btn_hand,
            self.btn_zoom,
            separator2,
            self.btn_snap,
            self.btn_linked_selection,
            self.btn_marker,
            separator3,
            self.btn_insert,
            self.btn_overwrite,
            self.btn_append,
            self.btn_edit_mode,
            self._audio_level_meter,
            separator_audio,
            self.btn_mixer,
            separator_zoom,
            self.btn_zoom_out,
            self.zoom_slider,
            self.btn_zoom_in,
            self.btn_zoom_fit,
        ]
        self._timeline_toolbar_widgets = [
            widget for widget in self._timeline_toolbar_widgets if widget is not None
        ]
        self._timeline_zoom_widgets = {
            self.btn_zoom_out,
            self.zoom_slider,
            self.btn_zoom_in,
            self.btn_zoom_fit,
        }
        tool_btn_style = """
            QToolButton {
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 3px;
                margin: 1px;
            }
            QToolButton:hover {
                background-color: rgba(255, 255, 255, 18);
            }
            QToolButton:pressed {
                background-color: rgba(255, 255, 255, 10);
            }
            QToolButton:checked {
                background: transparent;
            }
            QToolButton:checked:hover {
                background-color: rgba(255, 255, 255, 18);
            }
        """
        for i in range(tools_bar.count()):
            item = tools_bar.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), QToolButton):
                item.widget().setStyleSheet(tool_btn_style)

        self._configure_resolve_toolbar(layout, tools_bar)

        # ==================== Timeline View ====================
        self.timeline_view = TimelineView()
        self.timeline_view.setStyleSheet(f"""
            TimelineView {{
                background-color: {EDIT_COLORS['bg_app']};
                border: none;
            }}
        """)

        self._setup_timeline_menus()
        self.timeline_view.position_lock_changed.connect(self._sync_position_lock)
        self.timeline_view.source_patch_changed.connect(self.source_patch_changed.emit)
        self.timeline_view.clip_speed_settings_requested.connect(self.clip_speed_settings_requested.emit)
        self.timeline_view.horizontalScrollBar().valueChanged.connect(self._remember_custom_scroll)

        # Connect TimelineView signals
        self.timeline_view.playhead_changed.connect(self._on_playhead_changed)
        self.timeline_view.timeline_duration_extended.connect(self.set_total_frames)
        self.timeline_view.scrub_started.connect(self._on_scrub_started)
        self.timeline_view.scrub_finished.connect(self._on_scrub_finished)
        self.timeline_view.tool_changed.connect(self._on_view_tool_changed)
        self.timeline_view.zoom_changed.connect(self._on_view_zoom_changed)
        self.timeline_view.snap_changed.connect(self._on_view_snap_changed)
        self.timeline_view.linked_selection_changed.connect(self._sync_linked_selection)
        self.timeline_view.corner_timecode_clicked.connect(self._on_timecode_panel_clicked)
        # Forward timeline_changed signal to external listeners
        self.timeline_view.timeline_changed.connect(self.timeline_changed.emit)
        self.timeline_view.project_changed.connect(self.project_changed.emit)
        self.timeline_view.project_needed.connect(self.project_needed.emit)
        # T7: Forward open_in_color_requested signal
        self.timeline_view.open_in_color_requested.connect(self.open_in_color_requested.emit)

        # Phase 2: Forward Comp Clip context menu signals
        self.timeline_view.convert_to_comp_clip_requested.connect(self.create_comp_clip_requested.emit)
        self.timeline_view.reset_composite_requested.connect(self.reset_composite_requested.emit)
        self.timeline_view.remove_composite_binding_requested.connect(self.remove_composite_binding_requested.emit)
        self.timeline_view.bypass_composite_requested.connect(self.bypass_composite_requested.emit)
        self.timeline_view.reveal_comp_clip_in_media_pool_requested.connect(self.reveal_comp_clip_in_media_pool_requested.emit)
        self.timeline_view.render_cache_clip_requested.connect(self.render_cache_clip_requested.emit)
        self.timeline_view.rerender_cache_clip_requested.connect(self.rerender_cache_clip_requested.emit)
        self.timeline_view.use_shared_grade_requested.connect(self.use_shared_grade_requested.emit)
        self.timeline_view.make_local_grade_copy_requested.connect(self.make_local_grade_copy_requested.emit)
        self.timeline_view.teaching_animation_open_requested.connect(self.teaching_animation_open_requested.emit)
        # P1-02: Forward nested sequence signals
        self.timeline_view.create_nested_from_selection_requested.connect(self.create_nested_from_selection_requested.emit)
        self.timeline_view.enter_nested_sequence_requested.connect(self.enter_nested_sequence_requested.emit)
        # P1-01: Forward multicam angle key
        self.timeline_view.multicam_angle_key_requested.connect(self.multicam_angle_key_requested.emit)
        # P10.3: Forward subtitle clip signals
        self.timeline_view.add_subtitle_clip_requested.connect(self.add_subtitle_clip_requested.emit)
        self.timeline_view.subtitle_export_requested.connect(self.subtitle_export_requested.emit)
        self.timeline_view.subtitle_quality_requested.connect(self.subtitle_quality_requested.emit)
        self.timeline_view.subtitle_language_requested.connect(self.subtitle_language_requested.emit)
        self.timeline_view.subtitle_clip_clicked.connect(self.subtitle_clip_clicked.emit)
        self.timeline_view.subtitle_clip_drag_finished.connect(self.subtitle_clip_drag_finished.emit)
        self.timeline_view.subtitle_clip_trim_left_finished.connect(self.subtitle_clip_trim_left_finished.emit)
        self.timeline_view.subtitle_clip_trim_right_finished.connect(self.subtitle_clip_trim_right_finished.emit)
        self.timeline_view.subtitle_clip_delete.connect(self.subtitle_clip_delete.emit)
        self.timeline_view.caption_graphic_action_requested.connect(self.caption_graphic_action_requested.emit)
        self.timeline_view.caption_track_graphic_template_requested.connect(
            self.caption_track_graphic_template_requested.emit
        )
        self.timeline_view.subtitle_clip_edit_text_requested.connect(self.subtitle_clip_edit_text_requested.emit)
        # Update playback duration when timeline changes (clip add/delete/trim/move)
        self.timeline_view.timeline_changed.connect(self._on_timeline_content_changed)

        # Make the initial state explicit.  The Select button is checked
        # above, but the view/tool icon state must also be initialized so a
        # newly opened timeline never starts in a stale editing mode.
        self._update_tool_icons(EditTool.SELECT)
        self.timeline_view.set_tool(EditTool.SELECT)

        layout.addWidget(self.timeline_view, stretch=1)

        # W03：Keyframe Tray 宿主位（惰性创建、默认隐藏；EditPage 经
        # set_keyframe_tray_visible 控制，实际状态回写工具栏按钮）。
        self._keyframe_tray: Optional[Any] = None

    def _configure_resolve_toolbar(self, layout: QVBoxLayout, tools_bar: QHBoxLayout) -> None:
        """Group the editing instruments while preserving public button objects."""
        self._zoom_preset: str = "custom"
        self._applying_zoom_preset: bool = False
        self._custom_zoom: tuple[float, int] = (DEFAULT_ZOOM, 0)
        toolbar = QWidget(self)
        toolbar.setObjectName("timelineToolbar")
        toolbar.setFixedHeight(40)
        toolbar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        toolbar.setStyleSheet(f"""
            QWidget#timelineToolbar {{
                background: {EDIT_COLORS['bg_toolbar']};
                border-bottom: 1px solid {EDIT_COLORS['border_subtle']};
            }}
            QSlider::groove:horizontal {{ height: 3px; background: {EDIT_COLORS['border']}; }}
            QSlider::handle:horizontal {{ width: 9px; margin: -3px 0;
                background: {EDIT_COLORS['text_muted']}; border-radius: 4px; }}
        """)
        old_widgets: list[QWidget] = []
        while tools_bar.count():
            item = tools_bar.takeAt(0)
            if item.widget() is not None:
                old_widgets.append(item.widget())
        toolbar.setLayout(tools_bar)
        tools_bar.setContentsMargins(8, 0, 8, 0)
        tools_bar.setSpacing(3)
        for widget in old_widgets:
            widget.setParent(toolbar)
            widget.hide()

        def button(name: str, tooltip: str, icon: Any, checkable: bool = False) -> QToolButton:
            btn = QToolButton(toolbar)
            btn.setObjectName(name)
            btn.setToolTip(tooltip)
            btn.setAccessibleName(tooltip)
            btn.setIcon(icon)
            btn.setCheckable(checkable)
            btn.setStyleSheet(self.btn_select.styleSheet())
            return btn

        self.btn_view_options = button("timelineViewOptions",
            I18N.tr("timeline.view_options", "Timeline View Options"),
            timeline_icon("view_options"))
        self.btn_keyframe_tray = button("timelineKeyframeTray",
            I18N.tr("timeline.keyframe_tray", "Show Keyframe Tray"),
            timeline_icon("keyframes"), True)
        self.btn_keyframe_tray.toggled.connect(self.keyframe_tray_requested.emit)
        self.btn_voiceover = button("timelineVoiceover",
            I18N.tr("timeline.voiceover", "Voiceover"),
            timeline_icon("voiceover"))
        self.btn_voiceover.clicked.connect(self.voiceover_requested.emit)
        self.btn_advanced_tools = button("timelineAdvancedTools",
            I18N.tr("timeline.advanced_tools", "More Editing Tools"),
            timeline_icon("dynamic_trim"), True)
        self.btn_position_lock = button("timelinePositionLock",
            I18N.tr("timeline.position_lock", "Position Lock"),
            timeline_icon("position_lock"), True)
        self.btn_position_lock.toggled.connect(self._on_position_lock_toggled)
        self.btn_replace = button("timelineReplaceClip",
            I18N.tr("timeline.replace_clip", "Replace Clip"),
            timeline_icon("replace"))
        self.btn_replace.setEnabled(False)
        self.btn_replace.clicked.connect(self.source_replace_requested.emit)
        self._flag_color: str = "#4BA6DD"
        self.btn_flag = button("timelineFlags", I18N.tr("timeline.flags", "Flags"),
            timeline_icon("flag", self._flag_color))
        self.btn_flag.clicked.connect(lambda: self.flag_requested.emit(self._flag_color))
        self.btn_zoom_detail = button("timelineDetailZoom",
            I18N.tr("timeline.zoom.detail", "Detail Zoom"),
            timeline_icon("zoom_detail"), True)
        self.btn_zoom_custom = button("timelineCustomZoom",
            I18N.tr("timeline.zoom.custom", "Custom Zoom"),
            timeline_icon("zoom_custom"), True)
        self.btn_zoom_custom.setChecked(True)
        self.btn_zoom_fit.setCheckable(True)
        self.btn_zoom_fit.setToolTip(I18N.tr("timeline.zoom.full", "Full Extent Zoom"))
        self.btn_zoom_fit.setIcon(timeline_icon("zoom_full"))
        self.btn_zoom_detail.clicked.connect(lambda: self._set_zoom_preset("detail"))
        self.btn_zoom_custom.clicked.connect(lambda: self._set_zoom_preset("custom"))
        self.btn_zoom_out.setIcon(get_icon("fa5s.minus", IconColors.DEFAULT))
        self.btn_zoom_in.setIcon(get_icon("fa5s.plus", IconColors.DEFAULT))
        self.btn_overflow = button("timelineToolbarOverflow",
            I18N.tr("timeline.more_actions", "More Timeline Actions"),
            get_icon("fa5s.ellipsis-h", IconColors.DEFAULT))
        self._secondary_tool_buttons = [
            self.btn_ripple, self.btn_rate_stretch, self.btn_roll,
            self.btn_slip, self.btn_slide, self.btn_hand, self.btn_zoom,
        ]
        self._timeline_toolbar_widgets = []
        self._toolbar_separators: list[QWidget] = []

        def add(widget: QWidget) -> None:
            tools_bar.addWidget(widget)
            widget.show()
            self._timeline_toolbar_widgets.append(widget)

        def separator() -> None:
            line = QFrame(toolbar)
            line.setFrameShape(QFrame.Shape.NoFrame)
            line.setFixedSize(1, 18)
            line.setStyleSheet(f"background: {EDIT_COLORS['border_subtle']}; border: none;")
            line.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._toolbar_separators.append(line)
            add(line)

        add(self.btn_view_options)
        add(self.btn_keyframe_tray)
        add(self.btn_voiceover)
        tools_bar.addStretch(1)
        for widget in (self.btn_select, self.btn_trim, self.btn_blade, self.btn_advanced_tools):
            add(widget)
        separator()
        for widget in (self.btn_insert, self.btn_overwrite, self.btn_replace):
            add(widget)
        separator()
        for widget in (self.btn_snap, self.btn_linked_selection, self.btn_position_lock):
            add(widget)
        separator()
        add(self.btn_flag)
        add(self.btn_marker)
        separator()
        for widget in (self.btn_zoom_fit, self.btn_zoom_detail, self.btn_zoom_custom,
                       self.btn_zoom_out, self.zoom_slider, self.btn_zoom_in):
            add(widget)
        tools_bar.addStretch(1)
        if self._audio_level_meter is not None:
            self._audio_level_meter.setFixedSize(140, 20)
            add(self._audio_level_meter)
        add(self.btn_mixer)
        add(self.btn_overflow)
        for widget in self._timeline_toolbar_widgets + self._secondary_tool_buttons:
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if isinstance(widget, QToolButton):
                widget.setFixedSize(28, 28)
                widget.setIconSize(QSize(18, 18))
        self.zoom_slider.setFixedWidth(92)
        self.zoom_slider.setStyleSheet(f"""
            QSlider {{ background: transparent; border: none; padding: 0; margin: 0; }}
            QSlider::groove:horizontal {{ height: 3px; border: none; margin: 0;
                background: {EDIT_COLORS['border']}; border-radius: 1px; }}
            QSlider::sub-page:horizontal, QSlider::add-page:horizontal {{
                background: transparent; border: none; }}
            QSlider::handle:horizontal {{ width: 9px; height: 9px; margin: -3px 0;
                border: none; background: {EDIT_COLORS['text_muted']}; border-radius: 4px; }}
        """)
        self.btn_flag.setFixedWidth(36)
        self.btn_marker.setFixedWidth(36)
        self._timeline_zoom_widgets = {
            self.btn_zoom_out, self.zoom_slider, self.btn_zoom_in, self.btn_zoom_fit,
            self.btn_zoom_detail, self.btn_zoom_custom,
        }
        layout.addWidget(toolbar)

    def _setup_timeline_menus(self) -> None:
        """Expose display options and overflow actions without losing commands."""
        self._advanced_tool_actions: dict[EditTool, QAction] = {}
        advanced = QMenu(self)
        group = QActionGroup(advanced)
        group.setExclusive(True)
        for btn, factory, tool in self._get_tool_button_map()[3:]:
            action = advanced.addAction(factory(IconColors.DEFAULT), btn.toolTip())
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, value=tool: self._set_tool(value))
            self._advanced_tool_actions[tool] = action
        self.btn_advanced_tools.setMenu(advanced)
        self.btn_advanced_tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        advanced.aboutToHide.connect(lambda: self._update_tool_icons(self.timeline_view._active_tool))
        self._setup_annotation_menus()
        options = QMenu(self)
        self._thumbnail_actions: dict[str, QAction] = {}
        thumbnail_group = QActionGroup(options)
        thumbnails = options.addMenu(I18N.tr("timeline.video_view", "Video View"))
        for mode, title in (("auto", I18N.tr("timeline.thumbnails.auto", "Automatic")),
                            ("filmstrip", I18N.tr("timeline.thumbnails.filmstrip", "Filmstrip")),
                            ("start_end", I18N.tr("timeline.thumbnails.start_end", "First and Last Frame")),
                            ("none", I18N.tr("timeline.thumbnails.none", "Clip Names Only"))):
            action = thumbnails.addAction(title)
            action.setCheckable(True)
            thumbnail_group.addAction(action)
            action.triggered.connect(lambda _checked=False, value=mode:
                                     self.timeline_view.set_thumbnail_display_mode(value))
            self._thumbnail_actions[mode] = action
        self._waveforms_action = options.addAction(I18N.tr("timeline.show_waveforms", "Audio Waveforms"))
        self._waveforms_action.setCheckable(True)
        self._waveforms_action.triggered.connect(self.timeline_view.set_waveforms_visible)
        options.addSeparator()
        self._track_height_sliders: dict[str, QSlider] = {}
        for track_type, title, minimum, maximum, initial in (
            ("video", I18N.tr("timeline.video_track_height", "Video Track Height"), 36, 160, EDIT_SIZES["track_h_video"]),
            ("audio", I18N.tr("timeline.audio_track_height", "Audio Track Height"), 28, 120, EDIT_SIZES["track_h_audio"]),
        ):
            row = QWidget(options)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(14, 6, 14, 6)
            label = QLabel(title, row)
            label.setMinimumWidth(130)
            row_layout.addWidget(label)
            slider = QSlider(Qt.Orientation.Horizontal, row)
            slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            slider.setRange(minimum, maximum)
            slider.setValue(initial)
            slider.setFixedWidth(120)
            slider.setAccessibleName(title)
            slider.valueChanged.connect(lambda value, kind=track_type:
                                         self.timeline_view.set_track_height_for_type(kind, value))
            row_layout.addWidget(slider)
            action = QWidgetAction(options)
            action.setDefaultWidget(row)
            options.addAction(action)
            self._track_height_sliders[track_type] = slider
        options.addSeparator()
        self._follow_action = options.addAction(I18N.tr("timeline.follow_playhead", "Follow Playhead"))
        self._follow_action.setCheckable(True)
        self._follow_action.triggered.connect(self.timeline_view.set_playhead_follow_enabled)
        options.aboutToShow.connect(self._sync_view_options)
        self.btn_view_options.setMenu(options)
        self.btn_view_options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        overflow = QMenu(self)
        self._overflow_button_actions: dict[QToolButton, QAction] = {}
        for btn in (self.btn_keyframe_tray, self.btn_voiceover, self.btn_select, self.btn_trim, self.btn_blade,
                    self.btn_insert, self.btn_overwrite, self.btn_replace, self.btn_append, self.btn_snap,
                    self.btn_linked_selection, self.btn_position_lock, self.btn_marker,
                    self.btn_flag,
                    self.btn_zoom_fit, self.btn_zoom_detail, self.btn_zoom_custom,
                    self.btn_zoom_out, self.btn_zoom_in, self.btn_mixer):
            action = overflow.addAction(btn.icon(), btn.toolTip())
            action.setCheckable(btn.isCheckable())
            action.triggered.connect(lambda _checked=False, target=btn: target.click())
            self._overflow_button_actions[btn] = action
        overflow.addSeparator()
        overflow.addMenu(advanced).setText(I18N.tr("timeline.advanced_tools", "More Editing Tools"))
        self._insert_drop_action = overflow.addAction(I18N.tr("timeline.insert_drop", "Ripple Insert When Dropping Media"))
        self._insert_drop_action.setCheckable(True)
        self._insert_drop_action.triggered.connect(
            lambda checked: self.set_edit_mode("insert" if checked else "overwrite"))
        overflow.aboutToShow.connect(self._sync_overflow_menu)
        self.btn_overflow.setMenu(overflow)
        self.btn_overflow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

    def _setup_annotation_menus(self) -> None:
        """Make marker creation the primary action; use arrows for navigation."""
        self.btn_marker.clicked.disconnect(self._on_marker_toggled)
        self.btn_marker.setCheckable(False)
        self.btn_marker.setToolTip(I18N.tr("timeline.add_marker", "Add Marker (M)"))
        self.btn_marker.clicked.connect(self.timeline_view.add_marker_at_playhead)
        marker_menu = QMenu(self)
        marker_menu.addAction(I18N.tr("timeline.add_marker", "Add Marker (M)"),
                              self.timeline_view.add_marker_at_playhead)
        marker_menu.addAction(I18N.tr("timeline.previous_marker", "Previous Marker"),
                              self.timeline_view.jump_to_prev_marker)
        marker_menu.addAction(I18N.tr("timeline.next_marker", "Next Marker"),
                              self.timeline_view.jump_to_next_marker)
        marker_menu.addSeparator()
        self._marker_list_action = marker_menu.addAction(I18N.tr("tooltip.show_markers", "Show Marker List"))
        self._marker_list_action.setCheckable(True)
        self._marker_list_action.triggered.connect(self._set_marker_list_visible)
        self.btn_marker.setMenu(marker_menu)
        self.btn_marker.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        flags = QMenu(self)
        group = QActionGroup(flags)
        self._flag_actions: dict[str, QAction] = {}
        # W05：白名单单一真值来源在 core.timeline_v2（模型/命令/菜单共用）
        from src.features.edit.core.timeline_v2 import CLIP_FLAG_COLORS
        _flag_titles = {
            "#4BA6DD": I18N.tr("timeline.flag.blue", "Blue"),
            "#46C7CA": I18N.tr("timeline.flag.cyan", "Cyan"),
            "#72B66B": I18N.tr("timeline.flag.green", "Green"),
            "#E4C454": I18N.tr("timeline.flag.yellow", "Yellow"),
            "#E06B66": I18N.tr("timeline.flag.red", "Red"),
            "#D88BB7": I18N.tr("timeline.flag.pink", "Pink"),
            "#A38CCC": I18N.tr("timeline.flag.purple", "Purple"),
        }
        for color, title in (
            (value, _flag_titles.get(value, value))
            for value in CLIP_FLAG_COLORS
        ):
            action = flags.addAction(timeline_icon("flag", color), title)
            action.setCheckable(True)
            action.setChecked(color == self._flag_color)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, value=color: self._choose_flag_color(value))
            self._flag_actions[color] = action
        flags.addSeparator()
        flags.addAction(I18N.tr("timeline.clear_flags", "Clear Flags"), lambda: self.flag_requested.emit(""))
        self.btn_flag.setMenu(flags)
        self.btn_flag.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

    def _choose_flag_color(self, color: str) -> None:
        self._flag_color = color
        self.btn_flag.setIcon(timeline_icon("flag", color))
        self.flag_requested.emit(color)

    def _set_marker_list_visible(self, visible: bool) -> None:
        if visible:
            self._show_marker_panel()
        elif getattr(self, "_marker_list_dock", None) is not None:
            self._marker_list_dock.hide()

    def _sync_view_options(self) -> None:
        """Reflect the current view whenever its display popover is opened."""
        mode = self.timeline_view.thumbnail_display_mode()
        for value, action in self._thumbnail_actions.items():
            action.setChecked(mode == value)
        self._waveforms_action.setChecked(self.timeline_view.waveforms_visible())
        self._follow_action.setChecked(self.timeline_view.is_playhead_follow_enabled())
        timeline = self.timeline_view.get_timeline()
        if timeline is not None:
            heights = self.timeline_view.layout_state()["track_heights"]
            for track_type, slider in self._track_height_sliders.items():
                tracks = [track for track in timeline.all_tracks if track.track_type == track_type]
                if tracks:
                    default = EDIT_SIZES["track_h_" + track_type]
                    slider.blockSignals(True)
                    try:
                        slider.setValue(heights.get(tracks[0].track_id, default))
                    finally:
                        slider.blockSignals(False)

    def _sync_overflow_menu(self) -> None:
        """Keep hidden toolbar actions enabled and checked like their buttons."""
        for btn, action in self._overflow_button_actions.items():
            action.setEnabled(btn.isEnabled())
            action.setChecked(btn.isChecked())
            action.setIcon(btn.icon())
        self._insert_drop_action.setChecked(self.timeline_view.edit_mode() == "insert")

    def _on_position_lock_toggled(self, enabled: bool) -> None:
        self.timeline_view.set_position_lock_enabled(enabled)

    def _sync_position_lock(self, enabled: bool) -> None:
        self.btn_position_lock.blockSignals(True)
        try:
            self.btn_position_lock.setChecked(enabled)
            self.btn_position_lock.setIcon(timeline_icon("position_lock", IconColors.ORANGE if enabled else IconColors.DEFAULT))
        finally:
            self.btn_position_lock.blockSignals(False)

    def _remember_custom_scroll(self, value: int) -> None:
        if self._zoom_preset == "custom" and not self._applying_zoom_preset:
            self._custom_zoom = (self.timeline_view._pixels_per_frame, value)

    def export_presentation_state(self) -> dict:
        """W07：导出时间线展示偏好（view 偏好 + zoom/Tray 宿主状态）。

        只保存标识符与可重建数值（不含 Qt 对象/设备路径）；未知字段由
        UIState 原样往返。FileController 在保存工程时收集。
        """
        state = self.timeline_view.export_presentation_state()
        state.update({
            "zoom_preset": self._zoom_preset,
            "custom_pixels_per_frame": float(self._custom_zoom[0]),
            "custom_scroll": int(self._custom_zoom[1]),
            "keyframe_tray_visible": self.is_keyframe_tray_visible(),
        })
        return state

    def apply_presentation_state(self, state: dict) -> None:
        """恢复展示偏好（零内容命令、零 revision；非法值安全回退）。

        恢复顺序（计划 §6）：view 展示开关/源目的轨 → zoom（preset 或
        自定义 ppf+scroll）→ Tray 宿主状态。状态锁语义由各 setter 内部
        的 blocker/守卫保证（不重发工具栏请求）。
        """
        if not isinstance(state, dict):
            return
        self.timeline_view.apply_presentation_state(state)
        preset = state.get("zoom_preset")
        ppf = state.get("custom_pixels_per_frame")
        scroll = state.get("custom_scroll", 0)
        valid_ppf = (isinstance(ppf, (int, float)) and not isinstance(ppf, bool)
                     and (isinstance(ppf, int) or math.isfinite(ppf)) and ppf > 0)
        valid_scroll = isinstance(scroll, int) and not isinstance(scroll, bool) and scroll >= 0
        custom = (float(max(MIN_ZOOM, min(MAX_ZOOM, ppf))) if valid_ppf else self._custom_zoom[0],
                  min(scroll, 2**31 - 1) if valid_scroll else 0)
        # A saved Full/Detail view still owns a remembered Custom view.
        # Avoid capturing the just-loaded default view over that memory.
        self._custom_zoom = custom
        if preset in {"full", "detail"}:
            self._zoom_preset = preset
            self._set_zoom_preset(preset)
        else:
            if valid_ppf:
                self._zoom_preset = "custom"
                self._applying_zoom_preset = True
                try:
                    self.timeline_view.set_zoom(custom[0])
                    self.timeline_view.horizontalScrollBar().setValue(custom[1])
                finally:
                    self._applying_zoom_preset = False
            self._sync_zoom_preset_buttons()
        tray = state.get("keyframe_tray_visible")
        if isinstance(tray, bool):
            self.set_keyframe_tray_visible(tray)

    def _sync_zoom_preset_buttons(self) -> None:
        for btn, preset in ((self.btn_zoom_fit, "full"), (self.btn_zoom_detail, "detail"),
                            (self.btn_zoom_custom, "custom")):
            btn.setChecked(self._zoom_preset == preset)
            btn.setIcon(timeline_icon(f"zoom_{preset}", IconColors.ORANGE if self._zoom_preset == preset else IconColors.DEFAULT))

    def _set_zoom_preset(self, preset: str) -> None:
        """Switch between full edit, ten-second detail and remembered custom view."""
        if preset not in {"full", "detail", "custom"}:
            raise ValueError(f"Unknown timeline zoom preset: {preset!r}")
        self._apply_pending_slider_zoom()
        if self._zoom_preset == "custom":
            self._custom_zoom = (self.timeline_view._pixels_per_frame,
                                 self.timeline_view.horizontalScrollBar().value())
        self._applying_zoom_preset = True
        try:
            self._zoom_preset = preset
            if preset == "full":
                width = max(1, self.timeline_view.viewport().width() - TRACK_HEADER_WIDTH)
                duration = self._last_clip_end_frame(self.timeline_view.get_timeline()) or self._total_frames
                self.timeline_view.set_zoom(width * 0.95 / duration if duration > 0 else DEFAULT_ZOOM)
                self.timeline_view.scroll_to_timeline_start()
            elif preset == "detail":
                width = max(1, self.timeline_view.viewport().width() - TRACK_HEADER_WIDTH)
                zoom = width / max(1.0, self._fps * 10.0)
                self.timeline_view.set_zoom(zoom)
                playhead_x = self.timeline_view.get_playhead_frame() * self.timeline_view._pixels_per_frame
                self.timeline_view.horizontalScrollBar().setValue(int(playhead_x - width / 2))
            else:
                zoom, scroll = self._custom_zoom
                self.timeline_view.set_zoom(zoom)
                self.timeline_view.horizontalScrollBar().setValue(scroll)
        finally:
            self._applying_zoom_preset = False
        self._sync_zoom_preset_buttons()

    def set_display_mode(self, mode: str) -> None:
        """Switch between the full Edit toolbar and Deliver's compact view.

        The timeline model, command stack, selection, playhead and zoom state
        are deliberately untouched.  ``deliver`` only changes the visibility
        of widgets in this shared panel and records the previous explicit
        visibility so returning to Edit is lossless.

        Args:
            mode: ``"edit"`` or ``"deliver"``.

        Raises:
            ValueError: if an unsupported display mode is requested.
        """
        if mode not in {"edit", "deliver"}:
            raise ValueError(f"Unknown TimelinePanel display mode: {mode!r}")
        if not hasattr(self, "_timeline_toolbar_widgets"):
            self._toolbar_display_mode = mode
            return

        if mode == "deliver":
            if self._toolbar_display_mode != "deliver":
                self._toolbar_visibility_backup = {
                    widget: not widget.isHidden()
                    for widget in self._timeline_toolbar_widgets
                }
                self._toolbar_edit_width = self.width()
            self._toolbar_display_mode = "deliver"
            for widget in self._timeline_toolbar_widgets:
                widget.setVisible(widget in self._timeline_zoom_widgets)
            if self._deliver_timeline_controls is not None:
                self._deliver_timeline_controls.setVisible(True)
            return

        # Restore the explicit visibility captured before entering Deliver.
        # If no backup exists (e.g. a test or integration calls edit mode
        # directly), leave the normal responsive resize logic in charge.
        backup = self._toolbar_visibility_backup
        self._toolbar_display_mode = "edit"
        self._toolbar_visibility_backup = None
        if self._deliver_timeline_controls is not None:
            self._deliver_timeline_controls.hide()
        if backup is not None:
            previous_width = self._toolbar_edit_width
            # A hidden control that would normally have been visible at the
            # old width was explicitly hidden by its owner.  Preserve that
            # override while recomputing controls hidden only for lack of room.
            explicit_hidden = {
                widget for widget, threshold in self._responsive_toolbar_thresholds().items()
                if not backup.get(widget, True) and previous_width >= threshold
            }
            for widget, visible in backup.items():
                widget.setVisible(visible)
            self._refresh_responsive_toolbar()
            for widget in explicit_hidden:
                widget.hide()

    def set_deliver_timeline_controls(self, controls: Optional[QWidget]) -> None:
        """Place Deliver's Timeline/Range controls into this toolbar.

        The controls are display-only from the timeline panel's perspective;
        their owning settings panel still receives the combo-box signals and
        builds the export job.  The widget stays attached to this shared
        panel between page switches, but is hidden whenever Edit is active.
        """
        current = self._deliver_timeline_controls
        if current is controls:
            if controls is not None:
                controls.setVisible(self._toolbar_display_mode == "deliver")
            return

        if current is not None:
            self._tools_bar.removeWidget(current)
            current.hide()
            current.setParent(None)

        self._deliver_timeline_controls = controls
        if controls is None:
            return

        controls.setParent(self)
        anchor_index = self._tools_bar.indexOf(self.btn_zoom_out)
        self._tools_bar.insertWidget(anchor_index, controls)
        controls.setVisible(self._toolbar_display_mode == "deliver")

    def set_deliver_range_type(self, range_type: Optional[str]) -> None:
        """Apply Deliver's range mode to the shared timeline safely.

        Deliver uses the same In/Out range bar as Edit.  Selecting In/Out
        with no existing marks creates one undoable full-duration range so
        the handles are immediately visible and the exporter has a concrete
        default.  Existing In/Out marks are preserved.
        """
        self._deliver_range_type = range_type

        set_visibility = getattr(
            self.timeline_view,
            "set_deliver_range_visibility",
            None,
        )
        if callable(set_visibility):
            if range_type == "in_out":
                set_visibility(True)
            elif range_type == "entire":
                set_visibility(False)
            else:
                # Leaving Deliver restores the normal Edit visualization.
                set_visibility(None)

        if range_type != "in_out":
            return

        timeline = getattr(self.timeline_view, "_timeline", None)
        if timeline is None:
            return

        from src.features.edit.core.timeline_v2 import resolve_timeline_in_out

        duration = effective_timeline_duration_frames(timeline)
        if duration <= 0:
            return
        if resolve_timeline_in_out(
            getattr(timeline, "range_start", None),
            getattr(timeline, "range_end", None),
            duration,
        ) is not None:
            return

        from src.features.edit.core.commands import SetTimelineRangeCommand

        self.timeline_view.execute_command(
            SetTimelineRangeCommand.create(
                new_start=0,
                new_end=duration - 1,
                old_start=getattr(timeline, "range_start", None),
                old_end=getattr(timeline, "range_end", None),
            )
        )

    def _setup_shortcuts(self) -> None:
        """Setup keyboard shortcuts for transport controls."""
        # QShortcuts consume a key press before any event filter can see it,
        # and on macOS a parented Tool window (the Motion Graphic editor)
        # does NOT isolate WindowShortcut/ApplicationShortcut scope — the
        # owner window's shortcuts still fire while the Tool window is
        # active.  Space must therefore stay panel-scoped so the key can
        # reach the Motion Graphic editor's filter chain; J/K/L stay
        # window-wide for their editing ergonomics but are guarded by
        # _transport_context_active() so they stay silent while another
        # top-level window owns activation.
        # Space: Play/Pause
        self._shortcut_space = QShortcut(Qt.Key.Key_Space, self)
        self._shortcut_space.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut_space.activated.connect(self._on_space_pressed)

        # J: Backward play (shuttle)
        self._shortcut_j = QShortcut(Qt.Key.Key_J, self)
        self._shortcut_j.setContext(Qt.ShortcutContext.WindowShortcut)
        self._shortcut_j.activated.connect(self._on_j_pressed)

        # K: Pause
        self._shortcut_k = QShortcut(Qt.Key.Key_K, self)
        self._shortcut_k.setContext(Qt.ShortcutContext.WindowShortcut)
        self._shortcut_k.activated.connect(self._on_k_pressed)

        # L: Forward play (shuttle)
        self._shortcut_l = QShortcut(Qt.Key.Key_L, self)
        self._shortcut_l.setContext(Qt.ShortcutContext.WindowShortcut)
        self._shortcut_l.activated.connect(self._on_l_pressed)

        # B8: Shift+B toggles the global insert/overwrite drop mode
        self._shortcut_edit_mode = QShortcut(
            QKeySequence("Shift+B"), self
        )
        self._shortcut_edit_mode.setContext(Qt.ShortcutContext.WindowShortcut)
        self._shortcut_edit_mode.activated.connect(self.toggle_edit_mode)

    # ==================== B8: Global Edit Mode ====================

    #: ConfigManager key persisting the global insert/overwrite drop mode.
    _EDIT_MODE_CONFIG_KEY = "edit_drop_mode"

    def edit_mode(self) -> str:
        """Return the current global edit mode ("overwrite" | "insert")."""
        return self.timeline_view.edit_mode()

    def set_edit_mode(self, mode: str, persist: bool = True) -> None:
        """Set the global edit mode (B8).

        Args:
            mode: "overwrite" (default, safer) or "insert" (ripple).
            persist: Store the choice via ConfigManager.
        """
        mode = "insert" if mode == "insert" else "overwrite"
        self.timeline_view.set_edit_mode(mode)
        self._update_edit_mode_button()
        if persist:
            try:
                from src.shared.config.config_manager import ConfigManager

                ConfigManager().set(self._EDIT_MODE_CONFIG_KEY, mode)
            except Exception:  # noqa: BLE001 — 持久化失败不阻塞切换
                logger.debug("persist edit mode failed", exc_info=True)

    def toggle_edit_mode(self) -> None:
        """Toggle overwrite ↔ insert and surface the new mode on the button."""
        # F1.4：与 JKL 同域的窗口级 Shift+B，文本框聚焦时同样静默。
        if not (shortcut_owner_window_active(self) and not text_input_focused()):
            return
        new_mode = (
            "overwrite" if self.timeline_view.edit_mode() == "insert" else "insert"
        )
        self.set_edit_mode(new_mode)

    def _update_edit_mode_button(self) -> None:
        """Reflect the active mode: highlighted text + explicit tooltip."""
        mode = self.timeline_view.edit_mode()
        if mode == "insert":
            self.btn_edit_mode.setText(I18N.tr("edit.edit_mode.insert", "INSERT"))
            self.btn_edit_mode.setChecked(True)
            self.btn_edit_mode.setToolTip(I18N.tr(
                "edit.edit_mode.insert_tooltip",
                "插入模式：拖放素材在落点波纹（Shift+B 切回覆盖模式）",
            ))
        else:
            self.btn_edit_mode.setText(I18N.tr("edit.edit_mode.overwrite", "OVERWRITE"))
            self.btn_edit_mode.setChecked(False)
            self.btn_edit_mode.setToolTip(I18N.tr(
                "edit.edit_mode.overwrite_tooltip",
                "覆盖模式：拖放素材覆盖落点内容（Shift+B 切换为插入模式）",
            ))

    def _restore_edit_mode(self) -> None:
        """Apply the persisted edit mode at startup (default overwrite)."""
        stored: Any = None
        try:
            from src.shared.config.config_manager import ConfigManager

            stored = ConfigManager().get(self._EDIT_MODE_CONFIG_KEY)
        except Exception:  # noqa: BLE001
            logger.debug("read edit mode failed", exc_info=True)
        mode = stored if stored in ("insert", "overwrite") else "overwrite"
        self.set_edit_mode(mode, persist=False)

    # ==================== FPS and Duration ====================

    def set_fps(self, fps: float) -> None:
        """
        Set the timeline FPS for playback timing.

        Args:
            fps: Frames per second
        """
        self._fps = fps
        self._playback_controller.set_fps(fps)
        self._playback_timer.update_interval_for_fps()

    def set_fps_rational(self, fps) -> None:
        """
        Set the timeline FPS from a Rational (preserves NTSC fractional fps
        like 30000/1001 for accurate SMPTE drop-frame timecode).

        Args:
            fps: Rational fps (e.g. Rational(30000, 1001))
        """
        from src.shared.media.metadata import Rational
        if not isinstance(fps, Rational):
            # 降级：当作 float 处理
            self.set_fps(float(fps))
            return
        self._fps = fps.to_float()
        self._playback_controller.set_fps_rational(fps)
        self._playback_timer.update_interval_for_fps()

    def set_total_frames(self, total_frames: int) -> None:
        """
        Set the total frames for the timeline.

        Args:
            total_frames: Total number of frames in timeline
        """
        self._total_frames = total_frames
        self._playback_controller.set_timeline_duration(total_frames)

    # ==================== Responsive Layout ====================

    def resizeEvent(self, event: Any) -> None:
        """Move optional instruments to overflow while retaining the meter."""
        super().resizeEvent(event)

        # Deliver uses a reduced toolbar regardless of panel width.  Do not
        # let the normal Edit responsive rules bring hidden tools back.
        if getattr(self, "_toolbar_display_mode", "edit") == "deliver":
            for widget in getattr(self, "_timeline_toolbar_widgets", []):
                widget.setVisible(widget in getattr(self, "_timeline_zoom_widgets", set()))
            deliver_controls = getattr(self, "_deliver_timeline_controls", None)
            if deliver_controls is not None:
                deliver_controls.setVisible(True)
        else:
            self._refresh_responsive_toolbar()

        # The available timeline width changes with the panel size. Defer the
        # calculation until child widgets have received their resize event.
        if hasattr(self, "timeline_view"):
            QTimer.singleShot(0, self._refresh_zoom_slider_bounds)

    def _responsive_toolbar_thresholds(self) -> dict[QWidget, int]:
        """Minimum width for optional instruments; overflow retains each action."""
        thresholds: dict[QWidget, int] = {
            self.btn_insert: 900, self.btn_overwrite: 900, self.btn_replace: 900,
            self.btn_keyframe_tray: 1100, self.btn_voiceover: 1100,
            self.btn_flag: 1000, self.btn_advanced_tools: 700,
            self.btn_trim: 400, self.btn_blade: 400,
            self.btn_linked_selection: 500, self.btn_position_lock: 600,
            self.btn_marker: 650, self.btn_mixer: 600,
            self.zoom_slider: 750, self.btn_zoom_detail: 800,
            self.btn_zoom_custom: 800, self.btn_zoom_out: 560, self.btn_zoom_in: 560,
        }
        thresholds.update({separator: 900 for separator in self._toolbar_separators})
        return thresholds

    def _refresh_responsive_toolbar(self) -> None:
        """Apply the current width on resize and after restoring Edit from Deliver."""
        width = self.width()
        for button in self._secondary_tool_buttons:
            button.hide()
        for widget, threshold in self._responsive_toolbar_thresholds().items():
            widget.setVisible(width >= threshold)
        if self._audio_level_meter is not None:
            self._audio_level_meter.setFixedWidth(140 if width >= 1200 else 100 if width >= 700 else 56)
            self._audio_level_meter.show()

    # ==================== Playback Control ====================

    def start_playback(self, direction: int = 1, speed: float = 1.0) -> None:
        """
        Start playback with specified direction and speed.

        Args:
            direction: 1 for forward, -1 for backward
            speed: Speed multiplier (1.0, 2.0, 4.0, 8.0)
        """
        # TimelineView and the transport controller keep separate duration
        # values.  A newly-created empty timeline (or the first generator
        # clip committed in the same event turn) can update the view before
        # the controller receives its duration.  Resolve it at the playback
        # boundary so an empty timeline still plays a black frame instead of
        # being rejected as "empty".
        self._sync_playback_duration()
        if self._total_frames == 0:
            logger.warning("Cannot start playback: timeline is empty")
            playback_trace("timeline_panel.start_playback_blocked_empty")
            return

        # Use EditPlaybackController
        pb_direction = PlaybackDirection.FORWARD if direction > 0 else PlaybackDirection.BACKWARD
        reset_playback_trace(
            "timeline_panel.start_playback",
            current_frame=self._playback_controller.current_frame,
            direction=pb_direction.value,
            speed=speed,
            fps=self._fps,
            total_frames=self._total_frames,
        )
        self._playback_controller.play_with_speed(pb_direction, speed)

    def stop_playback(self) -> None:
        """Stop playback (go back to frame 0)."""
        self._playback_controller.stop()

    def pause_playback(self) -> None:
        """Pause playback."""
        self._playback_controller.pause()

    def toggle_playback(self) -> None:
        """Toggle play/pause state."""
        # Keep this before the state/cache branches: even a timeline with no
        # media must have a valid transport range, and a generator-only
        # timeline must not depend on a second clip to repair the duration.
        self._sync_playback_duration()
        if self._playback_controller.is_playing:
            playback_trace("timeline_panel.toggle_pause", current_frame=self._playback_controller.current_frame)
            self._playback_controller.pause()
        else:
            # 未缓存的 Motion Graphic 不再阻塞播放：播放中显示静帧，
            # 磁盘缓存由 EditController 的空闲调度器在后台补齐。
            reset_playback_trace(
                "timeline_panel.toggle_play",
                current_frame=self._playback_controller.current_frame,
                fps=self._fps,
                total_frames=self._total_frames,
            )
            self._playback_controller.play()

    def _sync_playback_duration(self) -> None:
        """Synchronize the transport duration from the loaded timeline.

        ``TimelineView`` owns the authoritative sequence object while the
        playback controller only stores an integer frame count.  During
        timeline creation and generator insertion those two updates can be
        delivered in different signal turns.  Recomputing the effective
        duration here is cheap and makes Space/button playback deterministic.
        """
        timeline = getattr(self.timeline_view, "_timeline", None)
        if timeline is None:
            return
        duration = self._effective_timeline_duration(timeline)
        if duration > 0 and duration != self._total_frames:
            self.set_total_frames(duration)
        # Keep FPS/range in sync as well when a timeline was inserted without
        # going through ``load_timeline`` (for example after undo/redo).
        fps = getattr(timeline, "fps", None)
        if fps is not None:
            from src.shared.media.metadata import Rational
            if isinstance(fps, Rational):
                if abs(self._fps - fps.to_float()) > 1e-9:
                    self.set_fps_rational(fps)
            else:
                try:
                    fps_value = float(fps)
                except (TypeError, ValueError):
                    fps_value = 0.0
                if fps_value > 0 and abs(self._fps - fps_value) > 1e-9:
                    self.set_fps(fps_value)
        self._sync_playback_range(timeline)

    def _scroll_to_playhead(self) -> None:
        """Scroll timeline to keep playhead visible (centered during playback)."""
        # Use TimelineView's built-in center_on_playhead method
        if hasattr(self.timeline_view, 'center_on_playhead'):
            # Enable playhead follow if not already
            if hasattr(self.timeline_view, 'is_playhead_follow_enabled'):
                if not self.timeline_view.is_playhead_follow_enabled():
                    self.timeline_view.set_playhead_follow_enabled(True)
            self.timeline_view.center_on_playhead()

    # ==================== JKL Control ====================

    def _transport_context_active(self) -> bool:
        """Window-wide J/K/L must stay silent behind another top-level window.

        Delegates to the shared guard; see src/shared/ui/shortcut_scope.py
        for why a Tool window parented to the project window (the Motion
        Graphic editor) does not isolate WindowShortcut scope on macOS.
        """
        # F1.4：文本输入框聚焦时窗口级 JKL 静默——字幕查找/轨道重命名等
        # QLineEdit 内输入 j/k/l 不得触发变速（此前仅查窗口归属）。
        return shortcut_owner_window_active(self) and not text_input_focused()

    def _on_j_pressed(self) -> None:
        """Handle J key press (backward shuttle)."""
        if not self._transport_context_active():
            return
        self._playback_controller.jkl_backward_press()
        # Start timer to reset counts after idle
        self._jkl_timer.start(500)

    def _on_k_pressed(self) -> None:
        """Handle K key press (pause)."""
        if not self._transport_context_active():
            return
        self._playback_controller.jkl_stop()

    def _on_l_pressed(self) -> None:
        """Handle L key press (forward shuttle)."""
        if not self._transport_context_active():
            return
        self._playback_controller.jkl_forward_press()
        # Start timer to reset counts after idle
        self._jkl_timer.start(500)

    def _on_space_pressed(self) -> None:
        """Handle Space key press (play/pause toggle)."""
        # BUG-08 fix: Use more reliable panel active check instead of isVisible()
        # isVisible() may return True for docked panels hidden behind other tabs
        if not self._is_panel_active():
            return
        # Reset JKL state
        self._playback_controller.jkl_reset()
        # Toggle playback
        self.toggle_playback()

    def _is_panel_active(self) -> bool:
        """
        Check if panel is active and should receive shortcuts.

        Returns:
            True if panel is visible and either has focus or a child widget has focus.
        """
        if not self.isVisible():
            return False

        # Check if this widget or any descendant has focus
        focus_widget = QApplication.focusWidget()
        if focus_widget is None:
            return False

        # Check if focus is within this panel
        return self.isAncestorOf(focus_widget) or focus_widget == self

    # ==================== Button Handlers ====================

    def _on_play_clicked(self) -> None:
        """Handle play button click."""
        playback_trace(
            "ui.play_button_clicked",
            current_frame=self._playback_controller.current_frame,
            is_playing=self._playback_controller.is_playing,
            fps=self._fps,
            total_frames=self._total_frames,
        )
        self._playback_controller.jkl_reset()
        self.toggle_playback()

    def _on_prev_frame_clicked(self) -> None:
        """Handle previous frame button click."""
        self._playback_controller.pause()
        self._playback_controller.step(-1)

    def _on_next_frame_clicked(self) -> None:
        """Handle next frame button click."""
        self._playback_controller.pause()
        self._playback_controller.step(1)

    def _on_jump_start_clicked(self) -> None:
        """Handle jump to start button click."""
        self._playback_controller.stop()

    def _on_jump_end_clicked(self) -> None:
        """Handle jump to end button click."""
        self._playback_controller.pause()
        if self._total_frames > 0:
            self._playback_controller.seek(self._total_frames - 1)

    def _on_loop_toggled(self, enabled: bool) -> None:
        """
        Handle loop toggle signal from external transport bar.

        The loop button itself now lives in ProgramViewerWidget (transport
        buttons migrated out of this panel). The authoritative loop state is
        applied by set_loop_enabled (which edit_page also connects to the same
        signal); this handler is retained only as a stable connection target
        and intentionally does NOT call set_loop again, to avoid a double
        application of the same state.

        Args:
            enabled: Loop state from the external transport bar (ignored)
        """
        logger.debug(f"Loop toggled via external transport bar: {enabled}")

    def set_loop_enabled(self, enabled: bool) -> None:
        """
        Set loop mode from external source (ProgramViewerWidget transport bar).

        Args:
            enabled: Whether loop mode is enabled
        """
        self._playback_controller.set_loop(enabled)
        # Note: the loop button lives in ProgramViewerWidget now (transport
        # buttons migrated out of this panel), so there is no local button
        # state to sync here.
        logger.debug(f"Loop mode (external): {enabled}")

    def open_timecode_dialog(self) -> None:
        """Open the timecode input dialog (called from ProgramViewerWidget transport bar)."""
        dialog = TimecodeInputDialog(
            self, self._playback_controller.current_frame, self._fps, self._total_frames
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            frame = dialog.get_frame()
            if frame is not None:
                self._seek_to_frame(frame)

    # ==================== Frame Navigation ====================

    def _move_frame(self, delta: int) -> None:
        """
        Move playhead by delta frames.

        Args:
            delta: Number of frames to move (positive=forward, negative=backward)
        """
        self._playback_controller.step(delta)

    def _seek_to_frame(self, frame: int) -> None:
        """
        Seek to a specific frame.

        Args:
            frame: Target frame number
        """
        self._playback_controller.seek(frame)

    # ==================== Timecode ====================
    # ==================== Timecode ====================

    def _update_timecode_display(self, frame: Optional[int] = None) -> None:
        """Update timecode label from current frame."""
        if frame is None:
            frame = self._playback_controller.current_frame
        timecode = self._frame_to_timecode(frame)
        # timecode_label moved to ProgramViewerWidget's TransportControlBar
        if hasattr(self, 'timecode_label'):
            self.timecode_label.setText(timecode)
        # P0-F: Update top-left timecode panel painted inside TimelineView.
        if hasattr(self, 'timeline_view'):
            self.timeline_view.set_corner_timecode(timecode)

    def _on_timecode_panel_clicked(self, event: Any = None) -> None:
        """P0-F: Handle click on top-left timecode panel to open jump dialog."""
        from PyQt6.QtWidgets import QInputDialog
        current_tc = self._frame_to_timecode(self._playback_controller.current_frame)
        tc_text, ok = QInputDialog.getText(
            self,
            I18N.tr("edit.timeline.jump_to_timecode", "Jump to Timecode"),
            I18N.tr("edit.timeline.enter_timecode", "Enter timecode (HH:MM:SS:FF) or frame number:"),
            text=current_tc
        )
        if ok and tc_text.strip():
            frame = self._timecode_to_frame(tc_text.strip())
            if frame is not None:
                self._seek_to_frame(frame)

    def _frame_to_timecode(self, frame: int) -> str:
        """
        Convert frame number to timecode string.

        Delegates to the playback controller so NTSC fractional fps render
        as SMPTE drop-frame timecode (semicolon-separated) consistently with
        the program viewer.

        Args:
            frame: Frame number

        Returns:
            Timecode string (HH:MM:SS:FF for NDF, HH:MM:SS;FF for DF)
        """
        return self._playback_controller.frame_to_timecode(frame)

    def _timecode_to_frame(self, timecode: str) -> Optional[int]:
        """
        Convert timecode string to frame number.

        Delegates to the playback controller, which accepts both NDF
        (colon) and DF (semicolon) formats as well as raw frame numbers.

        Args:
            timecode: Timecode string (HH:MM:SS:FF or HH:MM:SS;FF) or frame number

        Returns:
            Frame number or None if invalid
        """
        return self._playback_controller.timecode_to_frame(timecode)

    def _on_timecode_clicked(self, event: Any) -> None:
        """Handle timecode label click to open input dialog."""
        if event.button() != Qt.MouseButton.LeftButton:
            return

        # Create simple input dialog
        dialog = TimecodeInputDialog(self, self._playback_controller.current_frame, self._fps, self._total_frames)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            frame = dialog.get_frame()
            if frame is not None:
                self._seek_to_frame(frame)

    # ==================== Playback Controller Signal Handlers ====================

    def _on_controller_frame_changed(self, frame: int) -> None:
        """
        Handle frame change from playback controller.

        播放中：只更新时间码，可见播放头由 _on_timer_frame_ready 独占驱动，
        避免每个 tick 重复 set_playhead/重复渲染请求。

        暂停导航（U04，第六轮审计）：Next/Prev/Home/End/时间码/外部 seek
        只经过本信号——若不同步可见播放头，会出现 controller=26 而视图
        仍停在 25 的分叉，随后 Split 发生在不可见帧。这里在未播放时把
        播放头一并推到视图；set_playhead → playhead_changed 由
        EditPage/_on_playhead_changed 去重，不会重复请求渲染。
        """
        playback_trace(
            "timeline_panel.controller_frame_changed",
            frame=frame,
            is_playing=self._playback_controller.is_playing,
        )
        # The shared EditPlaybackController is also driven from the Color page
        # (timeline preview / clip card entry).  Repainting the Edit timeline's
        # GL viewport while this page is hidden forces its never-realised
        # QOpenGLWidget to initialise with no surface and aborts on macOS; the
        # Color page already reflects the playhead through its own transport.
        timeline_view = getattr(self, "timeline_view", None)
        if timeline_view is not None and not timeline_view.isVisible():
            return
        self._update_timecode_display(frame)
        if not self._playback_controller.is_playing:
            self._set_view_playhead_from_controller(frame)

    def _on_controller_state_changed(self, state: str) -> None:
        """Handle playback state change from controller."""
        playback_trace(
            "timeline_panel.controller_state_changed",
            state=state,
            current_frame=self._playback_controller.current_frame,
        )
        # E06: 把控制器状态桥接到 playback_state_changed(is_playing, speed,
        # direction)。该信号已有宿主接收者（MG 缓存互斥、自动化录制停止
        # 提交），但此处理器此前从不发射——外部监听者收不到任何状态。
        # 重复状态去重，保证 PLAYING/PAUSED/STOPPED 各触发一次。
        is_playing = state == PlaybackState.PLAYING.value
        if is_playing != self._last_emitted_playback_state:
            self._last_emitted_playback_state = is_playing
            direction = self._playback_controller.playback_direction
            self.playback_state_changed.emit(
                is_playing,
                float(self._playback_controller.playback_speed),
                1 if direction == PlaybackDirection.FORWARD else -1,
            )
        # Playback state is bridged once by TimelinePreviewRenderer via the
        # RealtimePlaybackEngine.  Keeping this panel on controller/UI duties
        # avoids a second direct renderer notification path and state races.
        # This is the authoritative path for transport-button, keyboard, and
        # JKL playback. Do not rely on the optional EditPage signal hub: that
        # left thumbnail decoding active during the first moments of playback.
        self.timeline_view.set_playback_active_for_thumbnails(
            state == PlaybackState.PLAYING.value
        )
        if state == PlaybackState.PLAYING.value:
            self._playback_timer.start()
        elif state in (PlaybackState.PAUSED.value, PlaybackState.STOPPED.value):
            self._playback_timer.stop()
        # P2-03: 后台代理 QoS——播放期间挂起磁盘代理生成（前台 p95 优先），
        # 暂停/停止后恢复。挂起/恢复是幂等的 best-effort，失败不影响播放。
        try:
            from src.shared.media.disk_proxy_service import get_disk_proxy_service
            service = get_disk_proxy_service()
            if state == PlaybackState.PLAYING.value:
                service.pause_all_generation()
            elif state in (PlaybackState.PAUSED.value, PlaybackState.STOPPED.value):
                service.resume_all_generation()
        except Exception:  # noqa: BLE001 QoS 失败不伤播放
            pass

    def _on_controller_speed_changed(self, speed: float) -> None:
        """Handle playback speed change from controller."""
        self._update_speed_label()

    def _on_controller_direction_changed(self, direction: str) -> None:
        """Handle playback direction change from controller."""
        self._update_speed_label()

    def _on_controller_end_reached(self) -> None:
        """Handle end reached from controller."""
        # Loop/transport buttons live in ProgramViewerWidget now; nothing to
        # update locally when playback end is reached.

    def _on_timer_frame_ready(self, frame: int) -> None:
        """Handle frame ready from playback timer."""
        playback_trace("timeline_panel.timer_frame_ready", frame=frame)
        # AUD-21302: 共享 EditPlaybackController 也被 Color 页驱动；视图隐藏
        # 时与 _on_controller_frame_changed 相同的守卫——既避免驱动隐藏的
        # Edit GL viewport（macOS 隐藏初始化 abort，见上方注释），也避免
        # 用隐藏视图的旧 playhead 触发背压 reanchor 令 Color 页播放回跳。
        timeline_view = getattr(self, "timeline_view", None)
        if timeline_view is not None and not timeline_view.isVisible():
            return
        if self._should_defer_playback_tick(frame):
            stable_frame = self._get_visible_playhead_frame()
            playback_trace(
                "timeline_panel.timer_frame_deferred",
                requested_frame=frame,
                stable_frame=stable_frame,
            )
            self._playback_controller.reanchor_to_frame(
                stable_frame,
                emit_frame_changed=False,
            )
            self.playback_reanchored.emit(stable_frame)
            self._update_timecode_display(stable_frame)
            return

        self._update_timecode_display(frame)
        self._set_view_playhead_from_controller(frame)

    def _get_visible_playhead_frame(self) -> int:
        """Return the playhead frame currently visible in TimelineView."""
        try:
            if hasattr(self.timeline_view, "get_playhead_frame"):
                return int(self.timeline_view.get_playhead_frame())
        except Exception:
            pass
        return int(self._playback_controller.current_frame)

    def _should_defer_playback_tick(self, frame: int) -> bool:
        """Ask the preview renderer whether playback should wait for render."""
        renderer = self._preview_renderer
        if renderer is None:
            return False

        should_defer = getattr(renderer, "should_defer_playback_tick", None)
        if not callable(should_defer):
            return False

        try:
            return bool(should_defer(frame))
        except Exception as e:
            logger.debug(f"Preview renderer backpressure check failed: {e}")
            return False

    def _on_jkl_reset_timeout(self) -> None:
        """Handle JKL reset timeout."""
        self._playback_controller.jkl_reset()

    def _set_view_playhead_from_controller(self, frame: int) -> None:
        """
        Update TimelineView from playback without treating it as manual seek.

        TimelineView emits playhead_changed for both user scrubbing and
        programmatic playback ticks. The panel's playhead handler pauses on
        manual seeks, so controller-driven updates must be guarded.

        Args:
            frame: Playback controller frame
        """
        if self._updating:
            playback_trace("timeline_panel.set_view_playhead_reentrant", frame=frame)
            self.timeline_view.set_playhead(frame)
            return

        self._updating = True
        try:
            playback_trace("timeline_panel.set_view_playhead", frame=frame)
            self.timeline_view.set_playhead(frame)
        finally:
            self._updating = False

    def set_timecode(self, timecode: str) -> None:
        """
        Update the timecode display (external API).

        Args:
            timecode: Timecode string to display
        """
        if hasattr(self, 'timecode_label'):
            self.timecode_label.setText(timecode)
        if hasattr(self, 'timeline_view'):
            self.timeline_view.set_corner_timecode(timecode)

    # ==================== UI Updates ====================

    def _update_speed_label(self) -> None:
        """Update speed indicator label."""
        if not hasattr(self, 'speed_label'):
            return
        if self._playback_controller.is_playing:
            direction = self._playback_controller.playback_direction
            direction_symbol = "" if direction == PlaybackDirection.FORWARD else "-"
            speed = self._playback_controller.playback_speed
            self.speed_label.setText(f"{direction_symbol}{speed}x")
            self.speed_label.setStyleSheet(
                f"color: {EDIT_COLORS['accent']}; font-size: 12px; font-weight: bold;"
                f" background: transparent;"
            )
        else:
            self.speed_label.setText("1x")
            self.speed_label.setStyleSheet(
                f"color: {EDIT_COLORS['text_muted']}; font-size: 12px;"
                f" background: transparent;"
            )

    # ==================== Tool Management ====================

    def _get_tool_button_map(self) -> List[tuple]:
        """Lazy-init tool button map: list of (button, icon_factory, EditTool)."""
        if self._tool_button_map is None:
            self._tool_button_map = [
                (self.btn_select, icon_select_tool, EditTool.SELECT),
                (self.btn_trim, icon_trim_tool, EditTool.TRIM),
                (self.btn_blade, icon_blade_tool, EditTool.BLADE),
                (self.btn_ripple, icon_ripple, EditTool.RIPPLE),
                (self.btn_rate_stretch, icon_ripple, EditTool.RATE_STRETCH),
                (self.btn_roll, icon_roll, EditTool.ROLL),
                (self.btn_slip, icon_slip, EditTool.SLIP),
                (self.btn_slide, icon_slide, EditTool.SLIDE),
                (self.btn_hand, icon_hand_tool, EditTool.HAND),
                (self.btn_zoom, icon_zoom_tool, EditTool.ZOOM),
            ]
        return self._tool_button_map

    def _update_tool_icons(self, active_tool: EditTool) -> None:
        """Update tool button icons: active=orange, inactive=default."""
        for btn, icon_fn, tool_enum in self._get_tool_button_map():
            color = IconColors.ORANGE if tool_enum == active_tool else IconColors.DEFAULT
            btn.setIcon(icon_fn(color))
        if hasattr(self, "_advanced_tool_actions"):
            for tool, action in self._advanced_tool_actions.items():
                action.setChecked(tool == active_tool)
            advanced = active_tool in self._advanced_tool_actions
            self.btn_advanced_tools.setChecked(advanced)
            if advanced:
                factory = next((factory for _btn, factory, tool in self._get_tool_button_map()
                                if tool == active_tool), icon_slip)
                self.btn_advanced_tools.setIcon(factory(IconColors.ORANGE))
            else:
                self.btn_advanced_tools.setIcon(timeline_icon("dynamic_trim"))

    def _set_tool(self, tool: EditTool) -> None:
        """
        Set active editing tool.

        Args:
            tool: Tool to activate
        """
        # Update button states (mutually exclusive - all tool buttons)
        self.btn_select.setChecked(tool == EditTool.SELECT)
        self.btn_trim.setChecked(tool == EditTool.TRIM)
        self.btn_blade.setChecked(tool == EditTool.BLADE)
        self.btn_ripple.setChecked(tool == EditTool.RIPPLE)
        self.btn_rate_stretch.setChecked(tool == EditTool.RATE_STRETCH)
        self.btn_roll.setChecked(tool == EditTool.ROLL)
        self.btn_slip.setChecked(tool == EditTool.SLIP)
        self.btn_slide.setChecked(tool == EditTool.SLIDE)
        self.btn_hand.setChecked(tool == EditTool.HAND)
        self.btn_zoom.setChecked(tool == EditTool.ZOOM)

        # Update icon colors
        self._update_tool_icons(tool)

        # Update view
        self.timeline_view.set_tool(tool)

        # Emit signal
        self.tool_changed.emit(tool.value)
        logger.debug(f"Tool set to {tool.value}")

    def _on_view_tool_changed(self, tool_name: str) -> None:
        """
        Handle tool change from TimelineView (keyboard shortcut).

        Args:
            tool_name: Tool name string
        """
        # AUD-21301: 经 _get_tool_button_map() 驱动互斥选中状态，避免与
        # _set_tool 的手写清单漂移——此前本函数手写 9 个按钮同步、唯独漏掉
        # btn_rate_stretch，键盘（Shift+R）切换 Rate Stretch 后工具栏
        # 互斥选中状态与实际工具不一致。
        for btn, _icon_fn, tool_enum in self._get_tool_button_map():
            btn.setChecked(tool_enum.value == tool_name)

        # Update icon colors
        try:
            active_tool = EditTool(tool_name)
            self._update_tool_icons(active_tool)
        except ValueError:
            pass

    # ==================== Zoom Management ====================

    def _defer_background_media_work_for_zoom(self) -> None:
        """Keep zoom gestures responsive by briefly pausing media background work."""
        self._zoom_background_resume_token += 1
        token = self._zoom_background_resume_token
        self.timeline_view.set_playback_active_for_thumbnails(True)
        try:
            from src.features.edit.core.audio_waveform import (
                WaveformExtractor, ClipEffectWaveformRenderer,
            )
            WaveformExtractor.instance().set_playback_active(True)
            ClipEffectWaveformRenderer.instance().set_playback_active(True)
        except Exception:
            logger.debug("Failed to pause waveform work for timeline zoom", exc_info=True)

        def resume_if_idle() -> None:
            if token != self._zoom_background_resume_token:
                return
            if self._scrubbing_playhead or self._playback_controller.is_playing:
                return
            # N16：DeferredDelete 冲刷后 view 的 C++ 对象可能已销毁而本
            # 回调（独立 QTimer）仍触发——访问前防御，已销毁即放弃恢复。
            view = self.timeline_view
            try:
                view.set_playback_active_for_thumbnails(False)
            except RuntimeError:
                logger.debug(
                    "timeline_view destroyed before zoom resume; skipping")
                return
            try:
                from src.features.edit.core.audio_waveform import (
                    WaveformExtractor, ClipEffectWaveformRenderer,
                )
                WaveformExtractor.instance().set_playback_active(False)
                ClipEffectWaveformRenderer.instance().set_playback_active(False)
            except Exception:
                logger.debug("Failed to resume waveform work after timeline zoom", exc_info=True)

        # Wheel/slider changes arrive in bursts. Wait until the final change
        # rather than restarting PyAV/OpenCV work between every tiny step.
        QTimer.singleShot(180, resume_if_idle)

    @staticmethod
    def _last_clip_end_frame(timeline: Any) -> int:
        """Return the furthest actual clip end across every timeline track.

        Sequence ``duration_frames`` can be a stale/default project duration.
        The zoom slider should instead be based on what the editor can see:
        the final video, audio, or subtitle clip on the timeline.
        """
        last_end = 0
        for track_group_name in ("video_tracks", "audio_tracks", "subtitle_tracks"):
            for track in getattr(timeline, track_group_name, ()) or ():
                for clip in getattr(track, "clips", ()) or ():
                    last_end = max(last_end, int(getattr(clip, "timeline_end", 0) or 0))
                for clip in getattr(track, "subtitle_clips", ()) or ():
                    last_end = max(last_end, int(getattr(clip, "timeline_end", 0) or 0))
        return last_end

    def _calculate_slider_min_zoom(self, timeline: Any) -> float:
        """Calculate the slider overview endpoint from the visible content.

        At the minimum, the furthest clip end occupies approximately 10% of
        the timeline content viewport.  This gives the overview state useful
        surrounding space while retaining a readable minimum zoom for short
        timelines.  The result never exceeds the global limits accepted by
        TimelineView.
        """
        last_end = self._last_clip_end_frame(timeline)
        if last_end <= 0:
            return MIN_ZOOM

        viewport_width = max(1, self.timeline_view.viewport().width())
        content_width = max(1, viewport_width - TRACK_HEADER_WIDTH)
        target_content_width = content_width * MIN_ZOOM_CONTENT_FILL_RATIO
        fit_zoom = target_content_width / last_end
        return max(MIN_ZOOM, min(DEFAULT_ZOOM, fit_zoom))

    def _refresh_zoom_slider_bounds(self) -> None:
        """Update logarithmic slider bounds while preserving the current view."""
        timeline = self.timeline_view._timeline
        if timeline is None:
            return

        new_min_zoom = self._calculate_slider_min_zoom(timeline)
        if math.isclose(new_min_zoom, self._slider_min_zoom, rel_tol=1e-6, abs_tol=1e-6):
            return

        self._slider_min_zoom = new_min_zoom
        self._log_min = math.log(self._slider_min_zoom)
        self._log_max = math.log(self._slider_max_zoom)
        self._log_range = self._log_max - self._log_min

        # Reposition the thumb only; changing timeline content must not alter
        # the user's current zoom level.  AUD-21304: try/finally so an
        # exception mid-update cannot leave the lock raised and permanently
        # disable slider-driven zoom.
        self._zoom_updating = True
        try:
            self.zoom_slider.setValue(self._ppf_to_slider(self.timeline_view._pixels_per_frame))
        finally:
            self._zoom_updating = False

        logger.debug(
            "Timeline zoom slider range: %.4f..%.1f px/frame (last clip ends at frame %d)",
            self._slider_min_zoom,
            self._slider_max_zoom,
            self._last_clip_end_frame(timeline),
        )

    def _reset_empty_timeline_zoom(self, timeline: Any) -> None:
        """Put an empty newly-created timeline at the overview endpoint."""
        if self._last_clip_end_frame(timeline) > 0:
            return

        self._fit_first_content_pending = True

        self._zoom_updating = True
        try:
            self.zoom_slider.setValue(self.zoom_slider.minimum())
        finally:
            self._zoom_updating = False
        self.timeline_view.set_zoom(self._slider_min_zoom)

    def _ppf_to_slider(self, ppf: float) -> int:
        """Convert pixels_per_frame to slider value (logarithmic mapping)."""
        ppf = max(self._slider_min_zoom, min(self._slider_max_zoom, ppf))
        t = (math.log(ppf) - self._log_min) / self._log_range  # 0..1
        return max(0, min(self._zoom_slider_max, int(t * self._zoom_slider_max)))

    def _slider_to_ppf(self, value: int) -> float:
        """Convert slider value to pixels_per_frame (logarithmic mapping)."""
        t = value / self._zoom_slider_max  # 0..1
        return math.exp(self._log_min + t * self._log_range)

    def _on_zoom_changed(self, value: int) -> None:
        """Queue a slider zoom update at most once per display frame."""
        if self._zoom_updating:
            return
        self._defer_background_media_work_for_zoom()
        self._pending_slider_zoom = self._slider_to_ppf(value)

        # Buttons, keyboard changes and programmatic slider updates should
        # still feel immediate.  Only a thumb drag needs coalescing.
        if not self.zoom_slider.isSliderDown():
            self._apply_pending_slider_zoom()
        elif not self._zoom_apply_timer.isActive():
            self._zoom_apply_timer.start()

    def _on_zoom_slider_released(self) -> None:
        """Apply the exact final zoom value when the user releases the thumb."""
        self._apply_pending_slider_zoom()

    def _apply_pending_slider_zoom(self) -> None:
        """Apply the latest coalesced zoom value without feedback work."""
        pixels_per_frame = self._pending_slider_zoom
        self._pending_slider_zoom = None
        if pixels_per_frame is None:
            return

        self._applying_slider_zoom = True
        try:
            self.timeline_view.set_zoom(pixels_per_frame)
        finally:
            self._applying_slider_zoom = False

    def _on_view_zoom_changed(self, pixels_per_frame: float) -> None:
        """Handle zoom change from TimelineView (wheel/keyboard)."""
        if not self._applying_slider_zoom:
            self._defer_background_media_work_for_zoom()
        if not self._applying_zoom_preset:
            self._zoom_preset = "custom"
            self._custom_zoom = (pixels_per_frame, self.timeline_view.horizontalScrollBar().value())
            self._sync_zoom_preset_buttons()
        # AUD-21304: try/finally guards the anti-recursion lock.
        self._zoom_updating = True
        try:
            self.zoom_slider.setValue(self._ppf_to_slider(pixels_per_frame))
        finally:
            self._zoom_updating = False

    def _on_zoom_out_clicked(self) -> None:
        """Handle zoom out button click."""
        current_value = self.zoom_slider.value()
        new_value = max(0, current_value - 40)
        self.zoom_slider.setValue(new_value)

    def _on_zoom_in_clicked(self) -> None:
        """Handle zoom in button click."""
        current_value = self.zoom_slider.value()
        new_value = min(self._zoom_slider_max, current_value + 40)
        self.zoom_slider.setValue(new_value)

    def _on_zoom_fit_clicked(self) -> None:
        """Handle fit to view button click - zoom to show entire timeline."""
        self._set_zoom_preset("full")

    # ==================== Snap Management ====================

    def _on_snap_toggled(self) -> None:
        """Handle snap button toggle."""
        enabled = self.btn_snap.isChecked()
        self.timeline_view.set_snap_enabled(enabled)
        # Update icon color
        self.btn_snap.setIcon(icon_snap(IconColors.ORANGE if enabled else IconColors.DEFAULT))

    def _on_view_snap_changed(self, enabled: bool) -> None:
        """Handle snap change from TimelineView."""
        self.btn_snap.setChecked(enabled)
        self.btn_snap.setIcon(icon_snap(IconColors.ORANGE if enabled else IconColors.DEFAULT))

    # ==================== Linked Selection & Marker ====================

    def _sync_linked_selection(self, enabled: bool) -> None:
        self.btn_linked_selection.blockSignals(True)
        try:
            self.btn_linked_selection.setChecked(enabled)
            self.btn_linked_selection.setIcon(icon_link(IconColors.DEFAULT) if enabled else icon_unlink(IconColors.DEFAULT))
        finally:
            self.btn_linked_selection.blockSignals(False)

    def _on_linked_selection_toggled(self) -> None:
        """Handle linked selection button toggle — controls whether linked A/V clips move together."""
        enabled = self.btn_linked_selection.isChecked()
        # Update icon: link when on, unlink when off, with color feedback
        color = IconColors.ORANGE if enabled else IconColors.DEFAULT
        if enabled:
            self.btn_linked_selection.setIcon(icon_link(color))
        else:
            self.btn_linked_selection.setIcon(icon_unlink(color))
        self.timeline_view.set_linked_selection_enabled(enabled)
        logger.debug(f"Linked selection: {enabled}")

    def _on_marker_toggled(self) -> None:
        """Compatibility entry point for the marker-list toggle."""
        self._set_marker_list_visible(self._marker_list_action.isChecked())

    def _show_marker_panel(self) -> None:
        """Create and show the marker list dock panel."""
        from PyQt6.QtWidgets import QTreeWidget

        if not hasattr(self, '_marker_list_dock') or self._marker_list_dock is None:
            from PyQt6.QtWidgets import QComboBox, QWidget

            self._marker_list_dock = PanelDockWidget(
                I18N.tr("edit.markers.title", "Markers"), self.window()
            )
            # C5: type filter (all | standard | chapter)
            filter_host = QWidget()
            filter_layout = QHBoxLayout(filter_host)
            filter_layout.setContentsMargins(4, 2, 4, 2)
            filter_layout.addWidget(QLabel(
                I18N.tr("edit.markers.filter_label", "类型")))
            self._marker_filter_combo = QComboBox()
            self._marker_filter_combo.addItem(
                I18N.tr("edit.markers.filter_all", "全部"), "all")
            self._marker_filter_combo.addItem(
                I18N.tr("edit.markers.filter_standard", "标准"), "standard")
            self._marker_filter_combo.addItem(
                I18N.tr("edit.markers.filter_chapter", "章节"), "chapter")
            self._marker_filter_combo.currentIndexChanged.connect(
                self._refresh_marker_list
            )
            filter_layout.addWidget(self._marker_filter_combo)
            filter_layout.addStretch()

            self._marker_tree = QTreeWidget()
            self._marker_tree.setHeaderLabels([
                I18N.tr("edit.markers.name", "Name"),
                I18N.tr("edit.markers.frame", "Frame"),
                I18N.tr("edit.markers.color", "Color"),
                I18N.tr("edit.markers.comment", "Comment"),
            ])
            self._marker_tree.header().setStretchLastSection(True)
            self._marker_tree.setRootIsDecorated(False)
            self._marker_tree.setAlternatingRowColors(True)
            self._marker_tree.setMinimumSize(350, 200)

            from PyQt6.QtWidgets import QVBoxLayout

            list_container = QWidget()
            list_layout = QVBoxLayout(list_container)
            list_layout.setContentsMargins(0, 0, 0, 0)
            list_layout.setSpacing(2)
            list_layout.addWidget(filter_host)
            list_layout.addWidget(self._marker_tree, 1)
            self._marker_list_dock.setWidget(list_container)
            self._marker_list_dock.setFloating(True)

            # Double-click to jump
            self._marker_tree.itemDoubleClicked.connect(self._on_marker_list_double_click)

            # Reflect dock closure in the marker menu without invoking its
            # triggered-only command connection again.
            self._marker_list_dock.visibilityChanged.connect(self._on_marker_dock_visibility_changed)

        # Populate markers from timeline
        self._refresh_marker_list()
        self._marker_list_dock.show()
        self._marker_list_dock.raise_()

    def _on_marker_dock_visibility_changed(self, visible: bool) -> None:
        """Keep the marker-list menu check in sync with its floating dock."""
        self._marker_list_action.setChecked(visible)
        logger.debug(f"Marker dock visibility synced to menu: {visible}")

    def _refresh_marker_list(self) -> None:
        """Refresh marker list from current timeline (honours C5 filter)."""
        if not hasattr(self, '_marker_tree') or self._marker_tree is None:
            return
        self._marker_tree.clear()

        timeline = self.timeline_view._timeline
        if timeline is None:
            return

        filter_kind = "all"
        combo = getattr(self, "_marker_filter_combo", None)
        if combo is not None:
            filter_kind = combo.currentData() or "all"

        for marker in sorted(timeline.markers, key=lambda m: m.frame):
            kind = getattr(marker, "kind", "standard")
            if filter_kind != "all" and kind != filter_kind:
                continue
            label = marker.name or "(unnamed)"
            if kind == "chapter":
                label = f"C · {label}"
            item = QTreeWidgetItem([
                label,
                str(marker.frame),
                marker.color,
                marker.comment or "",
            ])
            item.setData(0, 256, marker.marker_id)  # Store marker_id
            self._marker_tree.addTopLevelItem(item)

    def _on_marker_list_double_click(self, item: 'QTreeWidgetItem', _col: int) -> None:
        """Jump playhead to the double-clicked marker's frame."""
        marker_id = item.data(0, 256)
        timeline = self.timeline_view._timeline
        if timeline is None:
            return
        for m in timeline.markers:
            if m.marker_id == marker_id:
                self.timeline_view.seek_to_frame(m.frame)
                break

    # ==================== Audio Level Meter / Mixer ====================

    def update_audio_levels(self, levels: list) -> None:
        """
        更新音频电平表。

        Args:
            levels: [left_db, right_db] 电平值列表
        """
        if self._audio_level_meter is not None:
            self._audio_level_meter.set_audio_values(levels)

    def _toggle_mixer_panel(self) -> None:
        """切换主窗口 Audio Mixer 面板显示（toggle dock_audio_mixer）。"""
        main_win = self.window()
        dock = getattr(main_win, 'dock_audio_mixer', None)
        if dock is None:
            self.btn_mixer.setChecked(False)
            return

        if self.btn_mixer.isChecked():
            dock.show()
            dock.raise_()
            # 显示时懒同步：从当前 timeline 的音频轨填充 mixer strips。
            # 持续同步由 _on_edit_timeline_changed 维护，这里只补首次显示。
            self._sync_mixer_from_current_timeline(main_win)
        else:
            dock.hide()

    def _sync_mixer_from_current_timeline(self, main_win) -> None:
        """从当前 timeline 同步 Audio Mixer 面板（首次显示时懒填充）。"""
        syncer = getattr(main_win, '_sync_audio_mixer_from_current_timeline', None)
        if callable(syncer):
            try:
                syncer()
            except Exception as e:
                logger.warning(f"Audio mixer lazy sync failed: {e}")
            return
        # 兜底：直接读 edit_page._current_timeline 并调 mixer panel
        edit_page = getattr(main_win, 'edit_page', None)
        timeline = getattr(edit_page, '_current_timeline', None) if edit_page else None
        panel = getattr(main_win, 'audio_mixer_panel', None)
        if panel is None or timeline is None:
            return
        audio_tracks = getattr(timeline, 'audio_tracks', ())
        tracks = []
        for idx, track in enumerate(audio_tracks, start=1):
            tracks.append({
                "track_id": track.track_id,
                "name": getattr(track, 'name', '') or '',
                "tag": f"A{idx}",
                "volume": float(getattr(track, 'volume', 1.0)),
                "pan": float(getattr(track, 'pan', 0.0)),
                "is_muted": bool(getattr(track, 'is_muted', False)),
                "is_solo": bool(getattr(track, 'is_solo', False)),
            })
        try:
            panel.sync_tracks(tracks)
        except Exception as e:
            logger.warning(f"Audio mixer sync_tracks failed: {e}")

    def sync_mixer_button(self, visible: bool) -> None:
        """同步 Mixer 按钮状态（当 dock 通过菜单或其他方式切换可见性时调用）。"""
        self.btn_mixer.blockSignals(True)
        self.btn_mixer.setChecked(visible)
        self.btn_mixer.blockSignals(False)

    # ==================== Playhead Change Handler ====================

    def _on_playhead_changed(self, frame: int) -> None:
        """
        Handle playhead position change from view.

        Args:
            frame: New frame position
        """
        if self._updating:
            return
        
        self._updating = True
        try:
            # BUG-06 fix: Only pause if not already playing
            # This allows playback scrubbing without interrupting playback
            was_playing = self._playback_controller.is_playing
            
            # Update controller (pause only if not playing for exact positioning)
            if not was_playing:
                self._playback_controller.pause()
            self._playback_controller.seek(frame)
            
            # Update timecode display
            self._update_timecode_display()

            # Emit seek signal
            if not self._scrubbing_playhead:
                self.seek_requested.emit(frame)

        finally:
            self._updating = False

    def _on_scrub_started(self, frame: int) -> None:
        """Mark playhead dragging as scrub mode."""
        self._scrubbing_playhead = True
        if self._playback_controller.is_playing:
            self._playback_controller.pause()
        playback_trace("timeline_panel.scrub_started", frame=frame)

    def _on_scrub_finished(self, frame: int) -> None:
        """Leave scrub mode and emit one final exact seek."""
        was_scrubbing = self._scrubbing_playhead
        self._scrubbing_playhead = False
        playback_trace("timeline_panel.scrub_finished", frame=frame)
        if was_scrubbing:
            self.seek_requested.emit(frame)

    # ==================== Public API ====================

    def get_timeline_view(self) -> TimelineView:
        """Get the TimelineView widget."""
        return self.timeline_view

    def toggle_playback_hud(self) -> None:
        """P1-23: 切换播放性能 HUD（fps/丢帧/代理就绪度叠加层）。"""
        if self._playback_hud is not None:
            self._playback_hud.hide()
            self._playback_hud.deleteLater()
            self._playback_hud = None
            return
        from src.features.edit.ui.playback_hud import PlaybackHud

        self._playback_hud = PlaybackHud(
            controller_getter=lambda: self._playback_controller,
            stats_getter=self._collect_proxy_hud_stats,
            diagnostics_getter=self._collect_diagnostics_hud_stats,
            parent=self,
        )
        self._playback_hud.show()

    def _collect_diagnostics_hud_stats(self) -> dict:
        """P0-10: HUD 深度诊断行数据（p95/GPU time/拷贝字节/fallback）。"""
        diag: dict = {}
        renderer = self._preview_renderer
        engine = getattr(renderer, "_realtime_engine", None) if renderer else None
        if engine is not None:
            try:
                summary = engine.telemetry.summary()
                if summary.get("total_ms_p95"):
                    diag["total_ms_p95"] = float(summary["total_ms_p95"])
                mix = summary.get("source_kinds") or {}
                if mix:
                    top = sorted(mix.items(), key=lambda kv: -kv[1])[:2]
                    diag["source_mix"] = " ".join(
                        f"{k}:{v:.0%}" for k, v in top
                    )
            except Exception:  # noqa: BLE001 — 诊断失败静默
                pass
        gpu_renderer = getattr(renderer, "_gpu_renderer", None) if renderer else None
        if gpu_renderer is not None and hasattr(gpu_renderer, "gpu_pass_timer_stats"):
            try:
                gpu_stats = gpu_renderer.gpu_pass_timer_stats()
                if gpu_stats.get("unavailable"):
                    diag["gpu_time_unavailable"] = True
                elif gpu_stats.get("gpu_submit_ms") is not None:
                    diag["gpu_submit_ms"] = float(gpu_stats["gpu_submit_ms"])
            except Exception:  # noqa: BLE001
                pass
        try:
            from src.shared.video.pixel_copy_counters import snapshot_pixel_copies
            snap = snapshot_pixel_copies()
            diag["cpu_upload_mb"] = snap["cpu_upload"]["bytes"] / (1024 * 1024)
            diag["cpu_download_mb"] = snap["cpu_download"]["bytes"] / (1024 * 1024)
        except Exception:  # noqa: BLE001
            pass
        try:
            from src.shared.video.playback_frame_trace import get_trace_collector
            summary = get_trace_collector().get_summary(last_n=60)
            reasons = summary.get("fallback_reasons") or {}
            if reasons:
                top_reason, count = max(reasons.items(), key=lambda kv: kv[1])
                diag["top_fallback"] = f"{top_reason}×{count}"
        except Exception:  # noqa: BLE001
            pass
        return diag

    def export_performance_report(self, output_path: Optional[str] = None) -> Optional[str]:
        """P0-10: 一键导出脱敏性能报告（帧 trace + 遥测 + GPU/拷贝/资源）。

        Args:
            output_path: 目标 JSON 路径；None 时弹出保存对话框。

        Returns:
            实际写入路径；用户取消返回 None。
        """
        import os

        if not output_path:
            from PyQt6.QtWidgets import QFileDialog

            from src.shared.diagnostics.performance_bundle import (
                default_bundle_filename,
            )
            proposed = default_bundle_filename()
            output_path, _ = QFileDialog.getSaveFileName(
                self,
                I18N.tr("edit.export_perf_report.title", "导出脱敏性能报告"),
                proposed,
                "JSON (*.json)",
            )
            if not output_path:
                return None

        from src.shared.diagnostics.performance_bundle import (
            export_performance_bundle,
        )
        from src.shared.video.playback_frame_trace import get_trace_collector

        renderer = self._preview_renderer
        engine = getattr(renderer, "_realtime_engine", None) if renderer else None
        gpu_renderer = getattr(renderer, "_gpu_renderer", None) if renderer else None
        return export_performance_bundle(
            output_path=os.path.expanduser(output_path),
            trace_collector=get_trace_collector(),
            engine=engine,
            gpu_renderer=gpu_renderer,
        )

    def _collect_proxy_hud_stats(self) -> dict:
        """HUD 用磁盘代理就绪度统计（P1-14 诊断：当前实际可用哪一层）。

        AUD-21306: 结果按素材集合 + TTL 缓存。HUD 以 500ms 轮询本回调，
        冷缓存时每次都可能触发最多 200 次加锁磁盘校验，直接在 UI 线程
        造成可观测卡顿；素材集合未变时在 TTL 内直接复用上次统计。
        """
        import time

        project = getattr(self, "_project", None)
        assets = getattr(project, "assets", None) or {}

        video_paths = tuple(
            str(asset.path)
            for asset in list(assets.values())[:200]
            if getattr(asset, "media_type", "") == "video"
            and getattr(asset, "path", None)
        )
        cache_key = hash(video_paths)
        now = time.monotonic()
        cached = self._proxy_hud_cache
        if (
            cached is not None
            and cached[0] == cache_key
            and (now - cached[1]) < _PROXY_HUD_CACHE_TTL_S
        ):
            return cached[2]

        ready = generating = missing = 0
        try:
            from src.shared.media.disk_proxy_service import get_disk_proxy_service

            service = get_disk_proxy_service()
            for path in video_paths:
                info = service.get_disk_proxy_info(path)
                status = getattr(getattr(info, "status", None), "value", None)
                if getattr(info, "is_ready", False) or status == "ready":
                    ready += 1
                elif status == "generating":
                    generating += 1
                else:
                    missing += 1
        except Exception:  # noqa: BLE001 — HUD 统计失败静默（不打扰播放）
            return {}
        stats = {"ready": ready, "generating": generating, "missing": missing}
        self._proxy_hud_cache = (cache_key, now, stats)
        return stats

    def set_project(self, project: Optional[Any]) -> None:
        """
        Set the project reference for drag operations.

        Args:
            project: EditProject instance
        """
        self._project = project
        self.timeline_view.set_project(project)

    def clear_project(self) -> None:
        """Clear the project reference and reset timeline."""
        self.timeline_view.set_project(None)
        self.clear()

    def set_playhead_frame(self, frame: int) -> None:
        """
        Set playhead position from external source.

        Args:
            frame: Frame number
        """
        self._seek_to_frame(frame)

    def refresh_playhead_from_controller(self) -> None:
        """Realign the visible playhead and timecode with the controller.

        Programmatic seeks driven from other pages (Color clip playback
        sync, Composite entry) update only the playback controller while
        this view is hidden: ``_on_controller_frame_changed`` returns early
        on a hidden view and ``set_playhead_frame`` never touches the view.
        Returning to the Edit page would therefore show the playhead at its
        pre-departure position until the next tick or user interaction.
        """
        frame = self._playback_controller.current_frame
        self._set_view_playhead_from_controller(frame)
        self._update_timecode_display(frame)

    def get_current_frame(self) -> int:
        """Get current playhead frame."""
        return self._playback_controller.current_frame

    def get_playhead_frame(self) -> int:
        """Get current playhead frame (alias for get_current_frame)."""
        return self._playback_controller.current_frame

    def is_playing(self) -> bool:
        """Check if playback is active."""
        return self._playback_controller.is_playing

    def get_playback_controller(self) -> Any:
        """Get the EditPlaybackController instance for timeline playback state."""
        return self._playback_controller

    def set_preview_renderer(self, renderer: Optional[Any]) -> None:
        """Attach the timeline preview renderer for playback backpressure."""
        self._preview_renderer = renderer

    def get_playback_speed(self) -> float:
        """Get current playback speed."""
        return self._playback_controller.playback_speed

    def get_playback_direction(self) -> int:
        """Get current playback direction (1=forward, -1=backward)."""
        if self._playback_controller.playback_direction == PlaybackDirection.FORWARD:
            return 1
        return -1

    def is_loop_enabled(self) -> bool:
        """Check if loop mode is enabled."""
        return self._playback_controller.loop_enabled

    def set_source_edit_buttons_enabled(self, enabled: bool) -> None:
        """Enable or disable the source edit toolbar buttons (Insert/Overwrite/Append).

        Called by EditPage when SourceViewer's In/Out points change.
        """
        self.btn_insert.setEnabled(enabled)
        self.btn_overwrite.setEnabled(enabled)
        self.btn_append.setEnabled(enabled)
        self.btn_replace.setEnabled(enabled)

    def set_keyframe_tray_checked(self, checked: bool) -> None:
        """Write back the actual keyframe-tray host state to the toolbar button.

        W03/W09: the button must reflect what the host really shows, not the
        user's last click. Uses a signal blocker so the programmatic check
        does not re-emit ``keyframe_tray_requested``.
        """
        if self.btn_keyframe_tray.isChecked() == bool(checked):
            return
        self.btn_keyframe_tray.blockSignals(True)
        try:
            self.btn_keyframe_tray.setChecked(bool(checked))
        finally:
            self.btn_keyframe_tray.blockSignals(False)

    def set_keyframe_tray_visible(self, visible: bool) -> bool:
        """W03：显示/隐藏 Keyframe Tray，返回实际可见性（契约 D5）。

        Tray 惰性创建并托管在时间线视图下方；隐藏不产生任何内容命令，
        重新显示时从提交后的模型重投影。
        """
        if visible:
            if self._keyframe_tray is None:
                from src.features.edit.ui.edit_keyframe_tray import (
                    EditKeyframeTray,
                )
                self._keyframe_tray = EditKeyframeTray(
                    self.timeline_view, self)
                self._keyframe_tray.close_requested.connect(
                    lambda: self.set_keyframe_tray_visible(False))
                layout = self.layout()
                if layout is not None:
                    layout.insertWidget(
                        layout.indexOf(self.timeline_view) + 1,
                        self._keyframe_tray)
            self._keyframe_tray.refresh_from_model()
            self._keyframe_tray.show()
            self.set_keyframe_tray_checked(True)
            return True
        if self._keyframe_tray is not None:
            self._keyframe_tray.hide()
        self.set_keyframe_tray_checked(False)
        return False

    def is_keyframe_tray_visible(self) -> bool:
        """Tray 实际可见性（宿主回写与持久化的真值来源）。

        用 ``isHidden`` 判定宿主状态而非屏幕可见性——面板处于隐藏 dock
        或离屏测试中时，用户请求的展开状态仍然成立。
        """
        return bool(self._keyframe_tray is not None
                    and not self._keyframe_tray.isHidden())

    def clear(self) -> None:
        """Clear the timeline content."""
        self.stop_playback()
        self.timeline_view.clear()
        self._fit_first_content_pending = False
        self._total_frames = 0
        self._playback_controller.seek(0)
        self._update_timecode_display()
        logger.debug("TimelinePanel.clear() called")

    def show_provisional_timeline(self) -> None:
        """Show UI-only V1/A1 tracks for a project with no timeline yet."""
        self.stop_playback()
        timeline = self.timeline_view.show_provisional_timeline()

        from src.shared.media.metadata import Rational
        if isinstance(timeline.fps, Rational):
            self.set_fps_rational(timeline.fps)
        else:
            self.set_fps(float(timeline.fps))
        self.set_total_frames(self._effective_timeline_duration(timeline))
        self._refresh_zoom_slider_bounds()
        self._reset_empty_timeline_zoom(timeline)
        self._sync_playback_range(timeline)
        self._playback_controller.seek(0)
        self._update_timecode_display()

    def load_timeline(self, timeline: Any) -> None:
        """
        Load a timeline sequence into the view.

        Args:
            timeline: TimelineSequence to load
        """
        self.stop_playback()
        self._fit_first_content_pending = False
        
        # Get FPS and duration from timeline
        if hasattr(timeline, 'fps'):
            fps = timeline.fps
            # 优先保留 Rational（NTSC drop-frame 时间码需要精确比例）
            from src.shared.media.metadata import Rational
            if isinstance(fps, Rational):
                self.set_fps_rational(fps)
            elif hasattr(fps, 'to_fps'):
                self.set_fps(fps.to_fps())
            else:
                self.set_fps(float(fps))
        
        self.set_total_frames(self._effective_timeline_duration(timeline))
        
        # Load into view
        self.timeline_view.load_timeline(timeline)
        self._refresh_zoom_slider_bounds()

        # A newly created empty timeline should open at the overview endpoint.
        # Once clips exist, reloads preserve the user's current zoom instead of
        # repeatedly forcing the slider back to its minimum.
        self._reset_empty_timeline_zoom(timeline)

        # Loading a project always begins at frame zero. QGraphicsView may
        # retain a startup/layout scroll offset unless it is reset after the
        # new scene rectangle and scrollbar range have both been applied.
        self.timeline_view.scroll_to_timeline_start(deferred=True)

        # Sync playback range
        self._sync_playback_range(timeline)

        # Reset playhead to start
        self._playback_controller.seek(0)
        self._update_timecode_display()

    def _effective_timeline_duration(self, timeline: Any) -> int:
        """
        Get timeline duration from both sequence metadata and actual clip ends.

        Guarantees a minimum visible duration so the playback controller
        and zoom calculations are never zero (at least 10 seconds at
        sequence fps), matching TimelineView._effective_timeline_duration.

        Args:
            timeline: TimelineSequence-like object

        Returns:
            Duration in frames
        """
        duration = effective_timeline_duration_frames(timeline)
        fps_value = getattr(timeline, "fps", DEFAULT_FPS) or DEFAULT_FPS
        try:
            fps = float(fps_value.to_float() if hasattr(fps_value, "to_float") else fps_value)
        except (TypeError, ValueError):
            fps = DEFAULT_FPS
        return max(duration, int(round(max(1.0, fps) * 10.0)))

    def set_timeline_sequence(self, timeline: Any) -> None:
        """
        Set timeline sequence (alias for load_timeline).

        This method is called by MainWindow._load_edit_project().

        Args:
            timeline: TimelineSequence to load
        """
        self.load_timeline(timeline)

    def refresh_committed_timeline(self, timeline: Any) -> None:
        """Refresh combined-transaction UI after controller state is published."""
        self._on_timeline_content_changed(timeline)

    def _on_timeline_content_changed(self, timeline: Any) -> None:
        """
        Handle timeline content change from commands (add/delete/trim/move).

        Phase 7.2: 增量更新后触发，更新播放时长和状态栏信息。
        Phase 3.4: 同时更新缩放信息栏的片段数量和时间范围。

        Args:
            timeline: Updated TimelineSequence
        """
        if hasattr(timeline, 'duration_frames'):
            new_duration = self._effective_timeline_duration(timeline)
            logger.info(
                f"Timeline content changed: new_duration={new_duration}, "
                f"old_total_frames={self._total_frames}"
            )
            if new_duration != self._total_frames:
                self._total_frames = new_duration
                self._playback_controller.set_timeline_duration(new_duration)
                logger.info(f"Playback duration updated to {new_duration}")
        else:
            logger.warning("Timeline content changed but no duration_frames attribute")

        # Keep Marker list dock in sync if it is currently visible.
        # Previously the list only refreshed on open (_show_marker_panel),
        # so add/delete/move of clips (and marker edits via commands) left
        # the list stale until the user toggled the panel off and on.
        if getattr(self, '_marker_list_dock', None) is not None and self._marker_list_dock.isVisible():
            self._refresh_marker_list()

        # Sync playback range from sequence
        self._sync_playback_range(timeline)
        self._refresh_zoom_slider_bounds()
        if self._fit_first_content_pending and self._last_clip_end_frame(timeline) > 0:
            self._fit_first_content_pending = False
            self.timeline_view.zoom_fit_content()
        else:
            self._reset_empty_timeline_zoom(timeline)

    def _sync_playback_range(self, timeline: Any) -> None:
        """Sync timeline in/out range to the playback controller."""
        if timeline is not None and hasattr(timeline, 'range_start') and hasattr(timeline, 'range_end'):
            from src.features.edit.core.timeline_v2 import resolve_timeline_in_out

            resolved_range = resolve_timeline_in_out(
                timeline.range_start,
                timeline.range_end,
                effective_timeline_duration_frames(timeline),
            )
            if resolved_range is not None:
                self._playback_controller.set_playback_range(*resolved_range)
            else:
                self._playback_controller.clear_playback_range()
        else:
            self._playback_controller.clear_playback_range()

    def get_zoom_level(self) -> float:
        """
        Get current zoom level (pixels per frame).

        Returns:
            Zoom level in pixels per frame.
        """
        return self.timeline_view._pixels_per_frame

    def set_zoom_level(self, zoom_level: float) -> None:
        """
        Set zoom level (pixels per frame).

        Args:
            zoom_level: Zoom level in pixels per frame.
        """
        # AUD-21304: try/finally guards the anti-recursion lock.
        self._zoom_updating = True
        try:
            self.zoom_slider.setValue(self._ppf_to_slider(zoom_level))
        finally:
            self._zoom_updating = False
        self.timeline_view.set_zoom(zoom_level)

    def get_selected_clip_ids(self) -> List[str]:
        """
        Get IDs of currently selected clips.

        Returns:
            List of selected clip IDs.
        """
        return list(self.timeline_view._selection.selected_clip_ids)

    def get_selected_track_ids(self) -> List[str]:
        """
        Get IDs of currently selected tracks.

        Returns:
            List of selected track IDs.
        """
        return list(self.timeline_view._selection.selected_track_ids)

    # ==================== P3.2: Insert/Overwrite/Append Operations ====================

    def insert_clip(
        self,
        asset_id: str,
        in_frame: int,
        out_frame: int,
        playhead: Optional[int] = None,
        track_id: Optional[str] = None,
        source_fps_num: int | None = None,
        source_fps_den: int | None = None,
        timeline_fps_num: int | None = None,
        timeline_fps_den: int | None = None,
    ) -> bool:
        """
        Insert clip at playhead with ripple.

        Args:
            asset_id: Source asset ID
            in_frame: Source start frame
            out_frame: Source end frame (inclusive)
            playhead: Timeline position (defaults to current playhead)
            track_id: Target track (defaults to first video track)
            source_fps_num: Source fps numerator (optional)
            source_fps_den: Source fps denominator (optional)
            timeline_fps_num: Timeline fps numerator (optional)
            timeline_fps_den: Timeline fps denominator (optional)

        Returns:
            True if successful, False otherwise
        """
        return self.timeline_view.insert_clip(
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            playhead=playhead,
            track_id=track_id,
            source_fps_num=source_fps_num,
            source_fps_den=source_fps_den,
            timeline_fps_num=timeline_fps_num,
            timeline_fps_den=timeline_fps_den,
        )

    def overwrite_clip(
        self,
        asset_id: str,
        in_frame: int,
        out_frame: int,
        playhead: Optional[int] = None,
        track_id: Optional[str] = None
    ) -> bool:
        """
        Overwrite clip at playhead (no ripple).

        Args:
            asset_id: Source asset ID
            in_frame: Source start frame
            out_frame: Source end frame (inclusive)
            playhead: Timeline position (defaults to current playhead)
            track_id: Target track (defaults to first video track)

        Returns:
            True if successful, False otherwise
        """
        return self.timeline_view.overwrite_clip(
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            playhead=playhead,
            track_id=track_id
        )

    def append_clip(
        self,
        asset_id: str,
        in_frame: int,
        out_frame: int,
        track_id: Optional[str] = None
    ) -> bool:
        """
        Append clip to timeline end.

        Args:
            asset_id: Source asset ID
            in_frame: Source start frame
            out_frame: Source end frame (inclusive)
            track_id: Target track (defaults to first video track)

        Returns:
            True if successful, False otherwise
        """
        return self.timeline_view.append_clip(
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            track_id=track_id
        )


# =============================================================================
# TimecodeInputDialog
# =============================================================================

class TimecodeInputDialog(QDialog):
    """
    Dialog for entering timecode or frame number to jump to.

    Supports two input modes:
    - Timecode: HH:MM:SS:FF format
    - Frame: Pure integer frame number
    """

    def __init__(
        self,
        parent: QWidget,
        current_frame: int,
        fps: float,
        total_frames: int
    ) -> None:
        super().__init__(parent)
        
        self._current_frame = current_frame
        self._fps = fps
        self._total_frames = total_frames
        self._result_frame: Optional[int] = None
        
        self.setWindowTitle(I18N.tr("edit.timeline.timecode_dialog_title", "Go to Frame"))
        self.setFixedSize(300, 150)
        
        self._setup_ui()

    def _safe_fps_int(self) -> int:
        """Return an integer fps >= 1 for timecode arithmetic.

        AUD-21303: ``round(fps)`` 在 fps∈(0, 0.5) 时为 0，``self._fps > 0``
        守卫不生效，后续 ``// fps_int`` / ``% fps_int`` 除零崩溃（对话框
        构造路径无捕获）。fps 来自工程文件等外部数据，非法值（含
        NaN/Inf）统一回退 DEFAULT_FPS。
        """
        try:
            candidate = round(self._fps)
        except (TypeError, ValueError, OverflowError):
            candidate = 0
        if candidate >= 1:
            return int(candidate)
        fallback = round(DEFAULT_FPS)
        return int(fallback) if fallback >= 1 else 25

    def _setup_ui(self) -> None:
        """Setup dialog UI."""
        layout = QVBoxLayout(self)
        
        # Label
        label = QLabel(I18N.tr(
            "edit.timeline.timecode_dialog_label",
            "Enter timecode (HH:MM:SS:FF) or frame number:"
        ))
        layout.addWidget(label)
        
        # Current position info
        current_label = QLabel(I18N.tr(
            "edit.timeline.timecode_dialog_current",
            "Current: frame {frame}, total {total}",
            frame=self._current_frame,
            total=self._total_frames,
        ))
        current_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(current_label)
        
        # Input field
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText(
            I18N.tr("edit.tc_input_placeholder", "00:00:10:00 or 240"))
        
        # Pre-fill with current timecode
        # BUG-04 fix: Use round() instead of int() for accurate non-integer FPS
        # AUD-21303: _safe_fps_int 保证 >= 1，fps∈(0,0.5) 不再除零
        fps_int = self._safe_fps_int()
        hours = self._current_frame // (fps_int * 3600)
        minutes = (self._current_frame // (fps_int * 60)) % 60
        seconds = (self._current_frame // fps_int) % 60
        frames = self._current_frame % fps_int
        self.input_field.setText(f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}")
        
        self.input_field.selectAll()
        layout.addWidget(self.input_field)
        
        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _on_accept(self) -> None:
        """Handle OK button click."""
        text = self.input_field.text().strip()
        
        if not text:
            self.reject()
            return
        
        # Parse input
        frame = self._parse_input(text)
        
        if frame is None:
            # Invalid input, show error
            self.input_field.setStyleSheet("QLineEdit { border: 1px solid red; }")
            return
        
        # Clamp to valid range
        if self._total_frames > 0:
            frame = max(0, min(self._total_frames - 1, frame))
        
        self._result_frame = frame
        self.accept()

    def _parse_input(self, text: str) -> Optional[int]:
        """
        Parse input text to frame number.

        Args:
            text: Input text (timecode or frame number)

        Returns:
            Frame number or None if invalid
        """
        try:
            # Try pure frame number
            if ':' not in text:
                return int(text)
            
            # Parse timecode format
            parts = text.split(':')
            if len(parts) == 4:
                hours, minutes, seconds, frames = [int(p) for p in parts]
                # BUG-04 fix: Use round() instead of int() for accurate non-integer FPS
                # AUD-21303: 与 _setup_ui 统一取 _safe_fps_int（>= 1）
                fps = self._safe_fps_int()
                return hours * fps * 3600 + minutes * fps * 60 + seconds * fps + frames
            
            return None
        except ValueError:
            return None

    def get_frame(self) -> Optional[int]:
        """Get the result frame number."""
        return self._result_frame
