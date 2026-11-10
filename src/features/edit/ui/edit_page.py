"""
Edit Page - Main container and signal hub for the editing interface.

This page coordinates the Media Pool, Timeline, Properties, and Effects panels,
and the Program Viewer (节目监视器).

The Program Viewer displays:
- Current playhead position frame
- Timecode overlay (HH:MM:SS:FF)
- Safe frame indicators (90% and 100%, optional)
- Fullscreen preview mode

Architecture:
    MainWindow (Dock Layout)
    ├── Media Pool Dock
    ├── Viewport Dock (Source + Timeline, single/dual view)
    ├── Timeline Dock
    ├── Properties Dock
    └── Effects Dock

    EditPage (Signal Hub + Timeline Frame Coordinator)
    └── EditSignalHub (coordinates signals)
        ├── playhead_changed → timeline_frame_requested
        └── clip_selected → ProgramViewer clip info update

Frame Request Flow:
    1. TimelinePanel.playhead_changed(frame)
    2. EditPage._on_playhead_changed(frame)
    3a. Normal seek/playback: EditPage.timeline_frame_requested(frame)
    3b. Scrub drag: EditPage.timeline_scrub_frame_requested(frame)
        then EditPage.timeline_scrub_finished(frame) on mouse release
    4. TimelinePreviewRenderer renders exact frames or lightweight scrub previews
    5. TimelinePreviewRenderer.frame_ready(frame, result)
    6. ProgramViewerWidget.set_frame(frame, result)
"""

import inspect
import logging
import time
from pathlib import Path
from typing import Optional, Any

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QFileDialog,
    QMessageBox,
)
from PyQt6.QtCore import pyqtSignal, QObject, QTimer

from src.features.edit.core.playback_debug import playback_trace
from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES
from src.features.edit.core.source_patch import patch_for_asset

logger = logging.getLogger(__name__)


def _resolve_clip_effective_speed(clip: Any) -> float:
    """Return a clip's effective speed across current and legacy clip models.

    ``ClipInstance.effective_speed`` is a method, while a few older adapter
    objects expose the same value as an attribute.  The audio registration
    path accepts both representations because it is also used while opening
    projects created by earlier versions.
    """
    speed = getattr(clip, "effective_speed", None)
    if callable(speed):
        speed = speed()
    return float(speed if speed is not None else 1.0)


class EditSignalHub(QObject):
    """
    Signal hub for coordinating Edit page components.

    This class centralizes all signals that flow between Edit page panels,
    providing a clean decoupling mechanism. UI panels should connect to
    this hub rather than directly to each other.

    Signals:
        asset_imported: An asset was imported to the media pool
        asset_selected: An asset was selected in media pool
        assets_selection_changed: Asset selection changed (multi-select)
        bin_created: A new bin was created
        bin_deleted: A bin was deleted
        bin_renamed: A bin was renamed
        asset_moved_to_bin: An asset was moved to a bin
        current_bin_changed: Current selected bin changed
        timeline_deleted: A timeline was deleted
        timeline_switched: Active timeline changed
        clip_added: A clip was added to timeline
        clip_removed: A clip was removed from timeline
        clip_selected: A clip was selected
        clip_param_changed: A clip parameter was changed
        transition_added: A transition was added
        transition_selected: A transition was selected
        effect_added: An effect was added to a clip
        effect_removed: An effect was removed from a clip
        playback_started: Timeline playback started
        playback_stopped: Timeline playback stopped
        playhead_changed: Playhead position changed (NEW)
        seek_requested: Seek to specific frame
        tool_changed: Editing tool changed
    """

    # Media Pool signals - Asset
    asset_imported = pyqtSignal(str, dict)  # asset_id, metadata
    # E22: hub 的 asset_removed 已删除（声明后 0 emit 0 connect）
    asset_selected = pyqtSignal(str)  # asset_id (single selection)
    assets_selection_changed = pyqtSignal(list)  # list of asset_ids (multi-selection)

    # Media Pool signals - Bin
    bin_created = pyqtSignal(str, str)  # bin_id, name
    bin_deleted = pyqtSignal(str)  # bin_id
    bin_renamed = pyqtSignal(str, str)  # bin_id, new_name
    asset_moved_to_bin = pyqtSignal(str, str)  # asset_id, bin_id
    current_bin_changed = pyqtSignal(str)  # bin_id (current selected bin)

    # Timeline signals
    # F1.3/R8：timeline_created 死信号已移除——真实创建路径为媒体池
    # timeline_create_requested → edit_controller._on_timeline_create_requested
    # （NewTimelineDialog）；原 EditPage.create_timeline 发射后无消费者。
    # F3.1：嵌套音频展开诊断（预览不再静默——父 retime 嵌套音频跳过等，
    # 供 UI 波形/状态提示消费；payload = tuple[NestedAudioDiagnostic]）。
    nested_audio_diagnostics_changed = pyqtSignal(tuple)
    timeline_deleted = pyqtSignal(str)  # timeline_id
    timeline_switched = pyqtSignal(str)  # timeline_id
    timeline_selected = pyqtSignal(str)  # timeline_id (Media Pool inspection)

    # Clip signals
    clip_added = pyqtSignal(str, str, dict)  # clip_id, track_id, params
    clip_removed = pyqtSignal(str)  # clip_id
    clip_selected = pyqtSignal(str)  # clip_id
    clip_param_changed = pyqtSignal(str, str, object)  # clip_id, param_path, value

    # Transition signals
    transition_added = pyqtSignal(str, str, str)  # transition_id, left_clip_id, right_clip_id
    transition_selected = pyqtSignal(str)  # transition_id
    program_guide_changed = pyqtSignal(str, str, dict)  # guide_type, guide_id, params

    # Effect signals
    effect_added = pyqtSignal(str, str)  # clip_id, effect_id
    effect_removed = pyqtSignal(str, str)  # clip_id, effect_id

    # Playback signals
    playback_started = pyqtSignal()
    playback_stopped = pyqtSignal()
    playhead_changed = pyqtSignal(int)  # frame number (NEW)
    seek_requested = pyqtSignal(int)  # frame number

    # Tool signals
    tool_changed = pyqtSignal(str)  # tool name

    # E22: P3.2 的 hub 级 insert/overwrite/append_requested 已删除——
    # 只有 EditPage 内部的转发 emit、无任何接收者；真实链路是
    # viewer.*_requested → EditPage._on_*_requested 直接处理。

    # P9.2: Edit → Composite page linkage
    open_in_composite_requested = pyqtSignal(str, str)  # clip_id, track_id

    # T7: Edit → Color page linkage via right-click
    open_in_color_requested = pyqtSignal(str, str, str)  # clip_id, track_id, stack_id
    # AI voice-over from a subtitle track context menu.
    subtitle_to_speech_requested = pyqtSignal(str)  # track_id

    # Phase 2: Comp Clip actions via right-click
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

    # P1-02: 嵌套序列（Nested Sequence / Compound Clip）工作流
    create_nested_from_selection_requested = pyqtSignal(list)  # clip_ids
    enter_nested_sequence_requested = pyqtSignal(str, str)  # child_timeline_id, clip_id
    # P1-01: 多机位
    multicam_angle_key_requested = pyqtSignal(int)  # angle index (1-based)

    # P10.3: 字幕片段手柄交互（viewer → controller）
    subtitle_interaction_started = pyqtSignal(str)             # clip_id
    subtitle_interaction_updated = pyqtSignal(str, dict)       # clip_id, delta
    subtitle_interaction_ended = pyqtSignal(str, dict, dict)   # clip_id, final, original

    # Clip Transform on-canvas handle interaction (viewer → controller).
    # The controller reports a whole new TransformParams each update (the
    # drag math recomputes params absolutely), so the payload is an object,
    # not a delta dict.
    clip_transform_interaction_updated = pyqtSignal(str, object)          # clip_id, TransformParams
    clip_transform_interaction_ended = pyqtSignal(str, object, object)    # clip_id, final, original
    transform_tool_toggled = pyqtSignal(bool)                             # active


class EditPage(QWidget):
    """
    Edit page container and coordinator.

    This class manages the overall Edit page layout and coordinates
    signals between the main panels:
    - Media Pool (project media management)
    - Program Viewer (playhead preview display)
    - Timeline (editing workspace)
    - Properties (selection properties)
    - Effects (effects library)

    The actual dock layout is managed by MainWindow. EditPage serves as:
    1. Signal hub for panel coordination
    2. Frame request coordinator (playhead → clip → frame request)
    3. Timecode generator (frame → HH:MM:SS:FF)

    Attributes:
        signal_hub: The central signal coordinator
        _is_active: Whether the page is currently active
        _program_viewer: ProgramViewerWidget reference (set by MainWindow)
        _timeline_panel: TimelinePanel reference (set by MainWindow)
        _current_timeline: Current TimelineSequence data model
        _fps: Current timeline frame rate

    Signals:
        timeline_frame_requested: Request frame render at timeline frame (P2.1)
    """

    # P2.1: Timeline-based frame request (direct frame number)
    timeline_frame_requested = pyqtSignal(int)  # frame number
    timeline_scrub_started = pyqtSignal(int)  # frame number
    timeline_scrub_frame_requested = pyqtSignal(int)  # frame number
    timeline_scrub_finished = pyqtSignal(int)  # frame number
    waveform_status_message = pyqtSignal(str, int)  # message, timeout_ms
    timeline_display_changed = pyqtSignal(object)  # visible monitor timebase

    def _on_waveform_status_from_singleton(self, message: str,
                                           timeout_ms: int) -> None:
        """转发全局波形单例状态到页面信号（T-P1-71 安全连接形态）。

        绑定方法让 PyQt 注册本页为接收者——页面析构时 Qt 自动断开，
        单例（长命对象）上不会残留指向已删除页面的悬空连接。
        """
        self.waveform_status_message.emit(message, timeout_ms)

    # B1: scene cut timeline overlay visibility changed (menu/dialog share
    # one switch).  MainWindow uses this to keep the checkable menu action
    # in sync with the persisted state.
    scene_cut_overlay_visibility_changed = pyqtSignal(bool)  # visible

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("EditPage")

        # Signal hub for coordinating all Edit page components
        self.signal_hub = EditSignalHub()
        # Waveform work is owned by the edit core, but status-bar presentation
        # belongs to the page/window boundary.
        try:
            from src.features.edit.core.audio_waveform import WaveformExtractor
            # T-P1-71（段错误最小复现修复）：单例是全局长命对象，页面是
            # 短命对象。此前连接到 ``self.waveform_status_message.emit``
            # ——signal→.emit 的 C 函数槽不带接收者上下文，页面析构后连接
            # 悬空，下一次单例 emit 就调进已删除对象（SIGSEGV，全套件在
            # test_audio_waveform 处崩溃的最小复现链）。signal→signal 直连
            # 也只是把段错误换成删除后的 AttributeError 代理残留——只有
            # 绑定方法会让 PyQt 注册接收者上下文，页面析构时 Qt 自动断开。
            WaveformExtractor.instance().status_changed.connect(
                self._on_waveform_status_from_singleton)
        except Exception:
            logger.debug("Waveform status integration unavailable", exc_info=True)

        # Active state tracking
        self._is_active: bool = False

        # Panel references (set by MainWindow after dock creation)
        self._program_viewer: Optional[Any] = None
        self._timeline_panel: Optional[Any] = None
        self._media_pool_panel: Optional[Any] = None
        self._source_viewer: Optional[Any] = None  # P3.1: Source Viewer

        # W09: centralized bind/unbind for Resolve-style toolbar requests.
        # Every attach increments the generation; re-attaching a panel first
        # disconnects the previous panel's request signals so stale panels
        # can never trigger a second execution.
        self._panel_request_connections: list[tuple[Any, Any]] = []
        self._panel_request_generation: int = 0

        # W08: current audio_level_changed → panel.update_audio_levels
        # connection, so re-attaching a panel drops the stale panel's feed
        # instead of leaving a duplicate/leaked connection on the manager.
        self._timeline_level_connection: Optional[tuple[Any, Any]] = None

        # Timeline data model
        self._current_timeline: Optional[Any] = None
        self._fps: float = 24.0

        # Playhead frame tracking
        self._current_frame: int = 0
        self._last_requested_frame: int = -1
        self._scrub_active: bool = False
        self._last_scrub_requested_frame: int = -1
        self._pending_scrub_request_frame: int | None = None
        self._scrub_request_timer_pending: bool = False
        self._scrub_request_timer_token: int = 0
        self._scrub_last_emit_at: float = 0.0
        # Keep input coalescing below one 60 Hz display interval. The decoder
        # remains latest-only, so this does not create an unbounded queue.
        self._scrub_request_interval_ms: int = 4
        self._scrub_audio_last_emit_at: float = 0.0
        self._scrub_audio_request_interval_ms: int = 60
        self._scrub_audio_preview_duration_ms: int = 72
        self._background_media_resume_token: int = 0

        # Audio playback manager (audio clock master when audio clips present)
        self._audio_manager: Optional[Any] = None  # AudioPlaybackManager, lazy-init

        # MainWindow reference (explicitly injected, since EditPage has no Qt parent
        # and self.window() returns EditPage itself). Used to access dock panels like
        # audio_mixer_panel for audio level signal connections.
        self._main_window: Optional[Any] = None

        # Audio-video synchronizer (coordinates audio master clock with video controller)
        self._audio_sync: Optional[Any] = None  # AudioVideoSynchronizer

        # Whether audio is active (has audio clips on timeline)
        self._audio_active: bool = False
        self._audio_timeline_signature: tuple[Any, ...] | None = None
        # P0-02: 当前音频路由图的校验错误；非空表示时间线处于非法路由状态，
        # 音频同步被阻断在上一张有效图（不静默回退 Master）。UI/保存可经
        # audio_routing_errors 读取可定位错误。
        self._audio_routing_errors: tuple[str, ...] = ()
        # P0-01: 已提交音频图的 revision（取 timeline revision），换图诊断用
        self._audio_graph_revision: int = -1
        # P1-02: 嵌套序列音频展开用的序列解析器（timeline_id → 子序列）。
        # 由 file_controller 在注入预览 resolver 的同一处注入；None = 无嵌套。
        self._audio_sequence_resolver: Any = None
        self._timeline_preview_signature: tuple[Any, ...] | None = None

        # Scene Cut Controller (P9) — lazy-initialized when panels are set
        self._scene_cut_controller: Optional[Any] = None
        # B1: scene cut timeline overlay switch.  Restored from config when
        # the timeline panel is attached; every change persists immediately.
        self._scene_cut_overlay_visible: bool = False

        # Placeholder layout
        self._setup_ui()

        logger.debug("EditPage initialized")

    def _setup_ui(self) -> None:
        """Set up the placeholder UI structure."""
        # Placeholder layout - actual layout is managed by MainWindow docks
        # This widget serves as a coordinator/signal hub, not a visible container
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Invisible placeholder - the real panels are docks
        self.setVisible(False)

    # ==================== Panel Registration ====================

    def set_program_viewer(self, viewer: Any) -> None:
        """
        Set ProgramViewerWidget reference (called by MainWindow).

        Args:
            viewer: ProgramViewerWidget instance
        """
        self._program_viewer = viewer

        self._sync_program_timeline_selector()

        # ProgramViewer is a passive display for the Edit timeline. Frame
        # requests and frame results flow only through TimelinePreviewRenderer.
        if viewer:
            logger.debug("ProgramViewerWidget connected to EditPage")
            # P10.3: 字幕手柄交互信号转发到 signal_hub
            try:
                viewer.subtitle_interaction_started.connect(
                    self.signal_hub.subtitle_interaction_started.emit)
                viewer.subtitle_interaction_updated.connect(
                    self.signal_hub.subtitle_interaction_updated.emit)
                viewer.subtitle_interaction_ended.connect(
                    self.signal_hub.subtitle_interaction_ended.emit)
            except AttributeError:
                logger.warning("ProgramViewer missing subtitle interaction signals")

            # Clip Transform tool: forward viewer signals to signal_hub.
            try:
                viewer.clip_transform_interaction_updated.connect(
                    self.signal_hub.clip_transform_interaction_updated.emit)
                viewer.clip_transform_interaction_ended.connect(
                    self.signal_hub.clip_transform_interaction_ended.emit)
                viewer.transform_tool_toggled.connect(
                    self.signal_hub.transform_tool_toggled.emit)
            except AttributeError:
                logger.warning("ProgramViewer missing clip transform signals")

            try:
                viewer.guide_changed.connect(
                    self.signal_hub.program_guide_changed.emit)
            except AttributeError:
                logger.warning("ProgramViewer missing Guides signal")

            self._sync_program_guide_from_timeline()

        self._connect_trim_monitor_to_viewer()
        # Connect transport bar (ProgramViewerWidget <-> TimelinePanel)
        self._connect_transport_to_timeline()

    def set_timeline_panel(self, panel: Any) -> None:
        """
        Set TimelinePanel reference (called by MainWindow).

        Args:
            panel: TimelinePanel instance
        """
        self._timeline_panel = panel

        # Connect signals
        if panel:
            # Connect playhead change to our handler (via TimelineView)
            timeline_view = panel.get_timeline_view()
            if timeline_view:
                timeline_view.playhead_changed.connect(self._on_playhead_changed)
                if hasattr(timeline_view, 'scrub_started'):
                    timeline_view.scrub_started.connect(self._on_scrub_started)
                if hasattr(timeline_view, 'scrub_moved'):
                    timeline_view.scrub_moved.connect(self._on_scrub_moved)
                if hasattr(timeline_view, 'scrub_finished'):
                    timeline_view.scrub_finished.connect(self._on_scrub_finished)
                # P1-04：trim 进行中的 cut 邻近帧预览（节目监视器显示，
                # 不移动播放头——复用 scrub 渲染管线）
                if hasattr(timeline_view, 'trim_frame_preview_requested'):
                    timeline_view.trim_frame_preview_requested.connect(
                        self.timeline_scrub_frame_requested.emit
                    )
                # F2.3：trim 会话结束转发 finish（renderer 清除 scrub 量化态）
                if hasattr(timeline_view, 'trim_frame_preview_finished'):
                    timeline_view.trim_frame_preview_finished.connect(
                        self.timeline_scrub_finished.emit
                    )
                # P9.2: Connect open in composite request
                timeline_view.open_in_composite_requested.connect(self._on_open_in_composite_requested)
                # T7: Connect open in color request
                timeline_view.open_in_color_requested.connect(self._on_open_in_color_requested)
                # E09: 双击片段 → Source Viewer 打开对应素材与源帧
                if hasattr(timeline_view, 'clip_open_in_source_requested'):
                    timeline_view.clip_open_in_source_requested.connect(
                        self._on_clip_open_in_source
                    )
                # Scene Cut Detection (P9): forward timeline clip scene cut request
                if hasattr(timeline_view, 'scene_cut_detect_requested'):
                    timeline_view.scene_cut_detect_requested.connect(
                        self._on_scene_cut_detect_from_clip
                    )
                if hasattr(timeline_view, 'speech_to_text_requested'):
                    timeline_view.speech_to_text_requested.connect(
                        self._on_speech_to_text_from_clip
                    )
                if hasattr(timeline_view, 'subtitle_to_speech_requested'):
                    timeline_view.subtitle_to_speech_requested.connect(
                        self._on_subtitle_to_speech_from_track
                    )
                if hasattr(timeline_view, 'subtitle_export_requested'):
                    timeline_view.subtitle_export_requested.connect(
                        self._on_subtitle_export_requested
                    )
                if hasattr(timeline_view, 'subtitle_quality_requested'):
                    timeline_view.subtitle_quality_requested.connect(
                        self._on_subtitle_quality_requested
                    )
                if hasattr(timeline_view, 'subtitle_language_requested'):
                    timeline_view.subtitle_language_requested.connect(
                        self._on_subtitle_language_requested
                    )
                # B1: re-apply the persisted scene cut overlay switch to the
                # (possibly newly attached) timeline view.
                self._restore_scene_cut_overlay_state()
            panel.seek_requested.connect(self._on_seek_requested)
            if hasattr(panel, 'playback_reanchored'):
                panel.playback_reanchored.connect(self._on_playback_reanchored_for_audio)

            # Phase 2: Forward Comp Clip context menu signals
            panel.create_comp_clip_requested.connect(self.signal_hub.create_comp_clip_requested.emit)
            panel.reset_composite_requested.connect(self.signal_hub.reset_composite_requested.emit)
            panel.remove_composite_binding_requested.connect(self.signal_hub.remove_composite_binding_requested.emit)
            panel.bypass_composite_requested.connect(self.signal_hub.bypass_composite_requested.emit)
            panel.reveal_comp_clip_in_media_pool_requested.connect(self.signal_hub.reveal_comp_clip_in_media_pool_requested.emit)
            panel.render_cache_clip_requested.connect(self.signal_hub.render_cache_clip_requested.emit)
            panel.rerender_cache_clip_requested.connect(self.signal_hub.rerender_cache_clip_requested.emit)
            panel.use_shared_grade_requested.connect(self.signal_hub.use_shared_grade_requested.emit)
            panel.make_local_grade_copy_requested.connect(self.signal_hub.make_local_grade_copy_requested.emit)
            # P1-02: Forward nested sequence signals
            panel.create_nested_from_selection_requested.connect(self.signal_hub.create_nested_from_selection_requested.emit)
            panel.enter_nested_sequence_requested.connect(self.signal_hub.enter_nested_sequence_requested.emit)
            # P1-01: Forward multicam angle key
            panel.multicam_angle_key_requested.connect(self.signal_hub.multicam_angle_key_requested.emit)

            # Source edit toolbar buttons and Resolve-style toolbar requests
            # (W09): bound through the centralized request binder so panel
            # re-attachment disconnects stale connections first.
            self._bind_timeline_panel_requests(panel)

            # Connect playback state to thumbnail scheduler
            self.signal_hub.playback_started.connect(
                lambda: self._set_thumbnail_playback(True)
            )
            self.signal_hub.playback_stopped.connect(
                lambda: self._set_thumbnail_playback(False)
            )

            logger.debug("TimelinePanel connected to EditPage")

        self._connect_trim_monitor_to_viewer()
        # Connect transport bar (ProgramViewerWidget <-> TimelinePanel)
        self._connect_transport_to_timeline()

        # W08：manager 已存在时，把电平订阅换到新附着的 panel 上
        if self._audio_manager is not None:
            self._reconnect_timeline_level_meter()

    def _connect_trim_monitor_to_viewer(self) -> None:
        """Register the canvas host in either panel/viewer attachment order."""
        from PyQt6 import sip
        def alive(value: Any) -> bool:
            return not isinstance(value, QObject) or not sip.isdeleted(value)
        previous = getattr(self, "_trim_monitor_timeline_view", None)
        panel = self._timeline_panel
        current = panel.get_timeline_view() if panel is not None and alive(panel) else None
        if previous is not None and previous is not current and alive(previous):
            previous.set_trim_monitor_host(None)
        viewer = self._program_viewer
        host = viewer if alive(viewer) and callable(getattr(viewer, "set_trim_monitor", None)) else None
        if current is not None and alive(current) and callable(getattr(current, "set_trim_monitor_host", None)):
            current.set_trim_monitor_host(host)
            self._trim_monitor_timeline_view = current
        else:
            self._trim_monitor_timeline_view = None

    # ==================== W09: Centralized Toolbar Request Binding ====================

    def _bind_timeline_panel_requests(self, panel: Any) -> None:
        """Bind Resolve-style toolbar request signals from ``panel``.

        Re-attaching first disconnects every signal bound by the previous
        call, so a replaced panel can never trigger a stale execution and
        each request fires exactly once per host generation.
        """
        self._unbind_timeline_panel_requests()
        if panel is None:
            return
        self._panel_request_generation += 1
        # B14：clip_speed_settings_requested 为可选请求——测试替身/旧
        # 宿主面板可能尚未提供该信号，缺席时跳过该绑定（其余请求不受
        # 影响），不因单一新请求阻断整个绑定批。
        requested_bindings = (
            ("source_insert_requested", self._on_timeline_source_insert),
            ("source_overwrite_requested", self._on_timeline_source_overwrite),
            ("source_append_requested", self._on_timeline_source_append),
            ("source_replace_requested", self._on_timeline_source_replace),
            ("source_patch_changed", self._on_timeline_source_patch_changed),
            ("keyframe_tray_requested", self._on_timeline_keyframe_tray_requested),
            ("voiceover_requested", self._on_timeline_voiceover_requested),
            ("flag_requested", self._on_timeline_flag_requested),
            ("clip_speed_settings_requested",
             self._on_timeline_clip_speed_settings),
        )
        bindings = [
            (getattr(panel, name), slot)
            for name, slot in requested_bindings
            if getattr(panel, name, None) is not None
        ]
        for signal, slot in bindings:
            signal.connect(slot)
            self._panel_request_connections.append((signal, slot))
        logger.debug(
            "TimelinePanel requests bound (generation %d)",
            self._panel_request_generation)

    def _unbind_timeline_panel_requests(self) -> None:
        """Disconnect all toolbar request signals bound by the last attach."""
        for signal, slot in self._panel_request_connections:
            try:
                signal.disconnect(slot)
            except TypeError:
                pass  # already disconnected (e.g. panel destroyed)
        self._panel_request_connections = []

    def _on_timeline_source_patch_changed(
        self, track_type: str, track_id: str, enabled: bool) -> None:
        """W02：宿主消费蓝徽章源目的轨请求。

        路由真值已由 view 按 (timeline_id, track_type) 写入并验证轨存在；
        宿主侧消费 = 校验当前上下文仍然有效（panel/view/timeline 在位），
        然后按新的有效路由同步源编辑按钮可用性。红 target 不受影响。
        """
        panel = self._timeline_panel
        view = panel.get_timeline_view() if panel is not None else None
        if view is None or view.get_timeline() is None:
            logger.debug(
                "Source destination change ignored: no active timeline context")
            return
        logger.debug(
            "Source destination changed: type=%s track=%s enabled=%s",
            track_type, track_id, enabled)
        self._sync_source_edit_buttons()

    def _on_timeline_source_replace(self) -> None:
        """W01：Replace Edit 入口（契约 D1）。

        无参信号只表达"当前上下文执行 Replace"——接收时读取最新
        Source Viewer 与时间线快照，不缓存 clip/source。目标解析、源
        对齐、linked 配对与源不足预检都在 view 的
        ``replace_clip_with_source`` 中按契约 D1 完成。
        """
        panel = self._timeline_panel
        if panel is None:
            logger.warning("Replace: no timeline panel")
            return
        asset_id, in_frame, out_frame = self._get_source_edit_params()
        if asset_id is None:
            return
        view = panel.get_timeline_view()
        if view is None or not hasattr(view, "replace_clip_with_source"):
            logger.warning("Replace: timeline view cannot host replace edits")
            return
        view.replace_clip_with_source(asset_id, in_frame, out_frame)

    def _on_timeline_keyframe_tray_requested(self, visible: bool) -> None:
        """W03：Keyframe Tray 可见性请求（契约 D5）。

        Tray 由 TimelinePanel 托管在视图下方；实际可见性由 host 回写
        工具栏按钮（用户点击 → 请求 → host 提供面板 → 回写），避免
        按钮长期显示"已打开"而宿主未创建面板。隐藏/显示不产生内容命令。
        """
        panel = self._timeline_panel
        if panel is None or not hasattr(panel, "set_keyframe_tray_visible"):
            logger.warning("Keyframe tray: timeline panel cannot host a tray")
            return
        panel.set_keyframe_tray_visible(visible)

    def _on_timeline_voiceover_requested(self) -> None:
        """W04：Voiceover——桥接既有 ADR/录音链（不做"立即采默认麦克风"）。

        目标音轨 = 有效音频目的轨（W02 蓝路由，无显式路由回退红 target；
        均无轨时经既有 AddTrackCommand 创建）。展示 ADR Take Sheet 面板
        （设备/预卷/监听/电平的既有入口），启动复用 controller 的
        ``_start_recording``（时间码打点 = 当前播放头帧）；再次触发同轨
        → ``_stop_recording_and_import``（take 完成唯一事件源仍是
        ``_on_take_recorded`` 回调，停止一次只导入一次）。录音会话的
        project/timeline/track 绑定与跨工程防错投由既有链保证。
        """
        panel = self._timeline_panel
        view = panel.get_timeline_view() if panel is not None else None
        if view is None or view.get_timeline() is None:
            logger.warning("Voiceover: no active timeline context")
            return
        main = self._main_window
        if main is None or not hasattr(main, "_start_recording"):
            logger.warning("Voiceover: host controller unavailable")
            return
        track_id = view.resolve_source_destination_track_id("audio")
        if track_id is not None and view.is_track_locked(track_id):
            # B02④：锁定的目的轨明确拒绝，不进入录音会话。
            logger.warning("Voiceover: destination track %s is locked", track_id)
            self.waveform_status_message.emit(self.tr(
                "Voiceover destination track is locked"), 4000)
            return
        created_track = False
        if track_id is None:
            # 蓝徽章显式关闭/时间线无音频轨：B02② 录音上下文（宿主
            # controller）已在上方确认可用，此时才允许建轨；B02① 按
            # track ID 契约消费 ensure_target_audio_track 的返回值。
            track_id = view.ensure_target_audio_track()
            if track_id is None:
                logger.warning("Voiceover: no audio destination track")
                self.waveform_status_message.emit(self.tr(
                    "Voiceover: no audio destination track"), 4000)
                return
            created_track = True
        # 打开既有 ADR 入口（设备选择/监听/输入电平都在该面板）
        dock = getattr(main, "dock_adr_take_sheet", None)
        if dock is not None:
            dock.setVisible(True)
            dock.raise_()
        managers = getattr(main, "_recording_managers", {})
        active = managers.get(track_id)
        if active is not None and getattr(active, "is_recording", False):
            main._stop_recording_and_import(track_id)
        else:
            try:
                # B10：时间线 Voiceover 入口——capture 会话，take 完成后
                # 落 clip 到该轨锚点（ADR 素材采集路径不受影响）
                main._start_recording(track_id, voiceover=True)
            except Exception:
                # B02③：启动失败不留下"不明轨"——仅撤销本次手势创建的
                # 轨道，反馈后重新抛出让宿主继续处理真实设备错误。
                if created_track and view.can_undo():
                    try:
                        view.undo()
                    except Exception:
                        logger.exception(
                            "Voiceover: failed to roll back created track")
                raise

    def _on_timeline_clip_speed_settings(
        self, clip_id: str, track_id: str, settings: object) -> None:
        """B14：高级速度 draft 的宿主接收链。

        EditPage 读取最新上下文（panel/view 宿主在位），编排交给
        ``EditControllerMixin.apply_clip_speed_settings``：契约校验/
        能力预检通过则**单事务**执行全部设置（单步 undo）；不支持或
        校验失败由编排层给出明确诊断，clip/history/revision 零变化。
        宿主缺少编排入口（旧宿主/独立预览窗）时保持"不支持"反馈，
        不假装完成。
        """
        from src.shared.i18n.i18n import I18N

        panel = self._timeline_panel
        view = panel.get_timeline_view() if panel is not None else None
        if view is None or view.get_timeline() is None:
            logger.warning("Speed settings: no active timeline context")
            return
        main = self._main_window
        apply_settings = getattr(main, "apply_clip_speed_settings", None)
        if not callable(apply_settings):
            view.status_message_requested.emit(I18N.tr(
                "edit.speed_dialog.unsupported",
                "The selected speed options are not supported yet. "
                "The clip is unchanged."))
            return
        apply_settings(clip_id, track_id, settings)

    def _on_timeline_flag_requested(self, color: str) -> None:
        """W05：彩色 Flag 入口（契约 D4）。

        面板发白名单 hex 或 ""（清除）；"" 在此归一为 None。目标解析
        （选中优先/播放头回退）、批量单步 undo、linked 不传播都在
        ``TimelineView.set_clip_flags`` 中完成；无目标零写入。
        """
        panel = self._timeline_panel
        view = panel.get_timeline_view() if panel is not None else None
        if view is None or view.get_timeline() is None:
            logger.warning("Flag: no active timeline context")
            return
        view.set_clip_flags(color or None)

    def set_media_pool_panel(self, panel: Any) -> None:
        """
        Set MediaPoolPanel reference (called by MainWindow).

        Args:
            panel: MediaPoolPanel instance
        """
        self._media_pool_panel = panel

        # Keep the Program monitor's timeline menu in sync with project-level
        # timeline changes such as create/delete/undo, including changes to a
        # non-active timeline that do not call set_timeline().
        if panel and hasattr(panel, "project_changed"):
            panel.project_changed.connect(self._sync_program_timeline_selector)
        self._sync_program_timeline_selector()

        # The Media Pool emits ``asset_selected`` for a concrete, single
        # selection (the same event used by Source Viewer preview).  Bridge it
        # into the Edit selection hub so contextual panels, including Info,
        # receive the selection as well.  Previously only timeline selections
        # reached the hub, leaving the Info dock empty for Media Pool assets.
        if panel and hasattr(panel, "asset_selected"):
            panel.asset_selected.connect(
                lambda asset_id: self.signal_hub.assets_selection_changed.emit([asset_id])
            )
        if panel and hasattr(panel, "timeline_selected"):
            panel.timeline_selected.connect(self.signal_hub.timeline_selected.emit)

        # Scene Cut Detection (P9): connect media pool scene cut request
        if hasattr(panel, 'scene_cut_detect_requested'):
            panel.scene_cut_detect_requested.connect(
                self._on_scene_cut_detect_from_asset
            )

        # M3: 媒体池可选快速分析（§17.2）。把 panel 的分析意图信号接到 host handler，
        # 后者经 _main_window（ai_editing_mixin 持有的 MediaAnalysisController）走三步流。
        if hasattr(panel, 'analysis_requested'):
            panel.analysis_requested.connect(self._on_analysis_requested)
        if hasattr(panel, 'analysis_cancel_requested'):
            panel.analysis_cancel_requested.connect(self._on_analysis_cancel_requested)
        if hasattr(panel, 'analysis_retry_requested'):
            panel.analysis_retry_requested.connect(self._on_analysis_retry_requested)
        if hasattr(panel, 'analysis_result_cleared'):
            panel.analysis_result_cleared.connect(self._on_analysis_result_cleared)
        # 把 ai_editing_mixin 装配的 controller 注入 panel（接驳状态信号刷 badge）。
        controller = self._get_media_analysis_controller()
        if controller is not None and hasattr(panel, 'set_media_analysis_controller'):
            panel.set_media_analysis_controller(controller)

        logger.debug("MediaPoolPanel connected to EditPage")

    def set_source_viewer(self, viewer: Any) -> None:
        """
        Set SourceViewerWidget reference (called by MainWindow).

        P3.1: Source Viewer for previewing media assets.
        P3.2: Connected to insert/overwrite/append operations.

        Args:
            viewer: SourceViewerWidget instance
        """
        self._source_viewer = viewer

        # Connect edit signals to handlers (P3.2)
        if viewer:
            viewer.insert_requested.connect(self._on_insert_requested)
            viewer.overwrite_requested.connect(self._on_overwrite_requested)
            viewer.append_requested.connect(self._on_append_requested)
            # F2.1: Source Patch 设置（V/A 流开关 + 源声道映射）
            viewer.source_patch_requested.connect(
                self._on_source_patch_requested)

            # E22: 到 signal_hub 的转发已删除（hub 信号无接收者）

            # Sync TimelinePanel source-edit button enabled state with In/Out points
            viewer.in_point_changed.connect(lambda _: self._sync_source_edit_buttons())
            viewer.out_point_changed.connect(lambda _: self._sync_source_edit_buttons())
            # FE-B05：统一范围状态信号——open/clear/close/单帧范围都同步
            viewer.range_changed.connect(lambda *_: self._sync_source_edit_buttons())

            logger.debug("SourceViewerWidget connected to EditPage")

    def set_main_window(self, main_win: Any) -> None:
        """
        Set MainWindow reference (called by DockLayoutController).

        EditPage has no Qt parent (it's a hidden coordinator widget), so
        self.window() returns EditPage itself. This explicit injection lets
        EditPage access dock-level panels (audio_mixer_panel, etc.) hosted
        on MainWindow.

        Args:
            main_win: MainWindow instance
        """
        self._main_window = main_win
        if main_win:
            # 如果 audio_manager 已经创建但当时没连上 mixer_panel，现在补连
            self._reconnect_audio_mixer_signals()
            logger.debug("MainWindow reference injected to EditPage")

    def _reconnect_audio_mixer_signals(self) -> None:
        """确保 audio_manager 的电平信号连到 audio_mixer_panel。

        在 set_main_window 注入后、或 audio_mixer_panel 延迟可用时补连。
        幂等：Qt 的 connect 默认不会重复连接（UniqueConnection 语义由
        signal.connect 重复调用时自动去重保证，除非显式 Qt.UniqueConnection
        且连接成功；这里靠 _mixer_signals_connected 标志位保证幂等）。
        """
        if self._audio_manager is None or self._main_window is None:
            return
        if getattr(self, '_mixer_signals_connected', False):
            return
        mixer_panel = getattr(self._main_window, 'audio_mixer_panel', None)
        if mixer_panel is not None:
            self._audio_manager.audio_level_changed.connect(
                mixer_panel.update_master_level
            )
            self._audio_manager.audio_track_levels_changed.connect(
                mixer_panel.update_per_track_levels
            )
            self._mixer_signals_connected = True
            # W11: LUFS 按需计算——表模式驱动（无 LUFS 表时省掉 K-weighting，
            # 占混音热路径近一半耗时）。先同步当前状态再监听变化。
            if hasattr(mixer_panel, "lufs_metering_changed"):
                mixer_panel.lufs_metering_changed.connect(
                    self._audio_manager.set_lufs_metering
                )
                self._audio_manager.set_lufs_metering(
                    mixer_panel.is_lufs_metering_active()
                )
            logger.debug("Audio level signals connected to mixer_panel")

    # ==================== Transport Bar Wiring ====================

    def _connect_transport_to_timeline(self) -> None:
        """
        Wire up the ProgramViewerWidget's transport bar to TimelinePanel.

        This connects the playback buttons (in the preview window bottom)
        to TimelinePanel's playback controller methods.
        Also connects TimelinePanel's playback state changes back to
        the transport bar's UI updates.

        Safe to call multiple times — guards against duplicate connections.
        """
        if self._program_viewer is None or self._timeline_panel is None:
            return

        # Guard: only connect once
        if getattr(self, '_transport_connected', False):
            return
        self._transport_connected = True

        viewer = self._program_viewer
        panel = self._timeline_panel

        # --- Transport button clicks → TimelinePanel methods ---
        viewer.transport_jump_start.connect(panel._on_jump_start_clicked)
        viewer.transport_prev_frame.connect(panel._on_prev_frame_clicked)
        viewer.transport_play.connect(panel._on_play_clicked)
        viewer.transport_next_frame.connect(panel._on_next_frame_clicked)
        viewer.transport_jump_end.connect(panel._on_jump_end_clicked)
        viewer.transport_loop_toggled.connect(panel.set_loop_enabled)
        viewer.transport_loop_toggled.connect(self._on_loop_toggled)
        viewer.transport_timecode_clicked.connect(panel.open_timecode_dialog)

        # --- TimelinePanel playback state → Transport bar UI updates ---
        # Connect playback controller signals directly to transport bar updates
        controller = panel.get_playback_controller()
        if controller is not None:
            controller.frame_changed.connect(
                lambda frame: viewer.update_transport_timecode(
                    panel._frame_to_timecode(frame)
                )
            )
            controller.state_changed.connect(
                lambda state: self._on_transport_playback_state_changed(state)
            )

            # Wire audio to video controller state changes
            controller.state_changed.connect(self._on_playback_state_for_audio)
            controller.speed_changed.connect(
                lambda speed: self._on_transport_speed_changed(speed)
            )

        logger.debug("Transport bar wired to TimelinePanel")

    def _on_transport_playback_state_changed(self, state: str) -> None:
        """Update transport bar play button when playback state changes."""
        if self._program_viewer is None or self._timeline_panel is None:
            return
        is_playing = self._timeline_panel.get_playback_controller().is_playing
        self._program_viewer.update_transport_play_button(is_playing)
        self._program_viewer.set_playback_state(is_playing, self._fps)

    def _on_transport_speed_changed(self, speed: float) -> None:
        """Update transport bar speed label when playback speed changes."""
        if self._program_viewer is None or self._timeline_panel is None:
            return
        controller = self._timeline_panel.get_playback_controller()
        from src.features.edit.core.edit_playback_controller import PlaybackDirection
        direction = 1 if controller.playback_direction == PlaybackDirection.FORWARD else -1
        self._program_viewer.update_transport_speed(speed, direction)

    def _on_playback_state_for_audio(self, state: str) -> None:
        """
        Coordinate audio manager with video controller state changes.

        When video controller plays/pauses/stops, mirror to audio manager.

        Args:
            state: Playback state string ("playing", "paused", "stopped")
        """
        # This callback is the authoritative transport-state path. Direct
        # controller playback must immediately give video rendering priority
        # over every non-essential timeline background task.
        self._set_thumbnail_playback(state == "playing")

        if state == "playing":
            start_frame = self._current_transport_frame()
            self._current_frame = start_frame
            # AUD-19901: 隐藏的 Edit 页不得驱动自己的预览渲染器（理由与
            # _on_playhead_changed 的门控一致：隐藏渲染器二次解码同一内容，
            # 其渲染背压拖慢共享播放钟）。音频管理器仍照常启动。
            if self._preview_refresh_allowed():
                playback_trace("edit_page.playback_start_request_frame", frame=start_frame)
                self.timeline_frame_requested.emit(start_frame)
            if not self._audio_active or self._audio_manager is None:
                return
            self._audio_manager.play(start_frame=start_frame)
        elif state == "paused":
            if self._audio_manager is None:
                return
            self._audio_manager.pause()
        elif state == "stopped":
            if self._audio_manager is None:
                return
            self._audio_manager.stop()

    def _current_transport_frame(self) -> int:
        """Return the timeline panel/controller frame used for transport sync."""
        if self._timeline_panel is None:
            return int(self._current_frame)
        try:
            controller = self._timeline_panel.get_playback_controller()
            if controller is not None:
                return int(controller.current_frame)
        except Exception:
            pass
        try:
            return int(self._timeline_panel.get_playhead_frame())
        except Exception:
            return int(self._current_frame)

    def _on_playback_reanchored_for_audio(self, frame: int) -> None:
        """
        Correct audio only when renderer backpressure created visible drift.

        Small corrections during active playback are intentionally ignored to
        avoid resetting the audio ring buffer. A larger video reanchor must
        seek audio as well; otherwise a stalled video preview keeps moving
        audio and leaves the two clocks permanently out of sync.
        """
        if not self._audio_active or self._audio_manager is None:
            return

        if self._timeline_panel is not None:
            controller = self._timeline_panel.get_playback_controller()
            if controller is not None and controller.is_playing:
                try:
                    audio_frame = int(self._audio_manager.get_current_frame())
                except Exception:
                    audio_frame = frame
                if abs(audio_frame - int(frame)) <= 3:
                    playback_trace(
                        "edit_page.audio_reanchor_skip_small_live_drift",
                        frame=frame,
                        audio_frame=audio_frame,
                    )
                    return
                playback_trace(
                    "edit_page.audio_reanchor_after_large_live_drift",
                    frame=frame,
                    audio_frame=audio_frame,
                )
                self._audio_manager.seek(int(frame))
                return
            if controller is not None:
                return

        try:
            audio_frame = int(self._audio_manager.get_current_frame())
        except Exception:
            audio_frame = frame

        if abs(audio_frame - int(frame)) <= 2:
            return

        playback_trace(
            "edit_page.audio_reanchor_after_video_backpressure",
            frame=frame,
            audio_frame=audio_frame,
        )
        self._audio_manager.seek(int(frame))

    def _prepare_or_seek_audio(self, frame: int) -> None:
        """Use paused pre-roll for manual positioning and real seek while playing."""
        if not self._audio_active or self._audio_manager is None:
            return

        try:
            state = self._audio_manager.get_state()
            state_name = getattr(state, "name", str(state)).lower()
        except Exception:
            state_name = ""

        if state_name == "playing":
            self._audio_manager.seek(int(frame))
            return

        prepare = getattr(self._audio_manager, "prepare_for_playback", None)
        if callable(prepare):
            prepare(int(frame))
            playback_trace("edit_page.audio_prepare_for_playback", frame=frame)
            return

        self._audio_manager.seek(int(frame))

    def _request_scrub_audio_preview(self, frame: int, *, force: bool = False) -> None:
        """Request a throttled, best-effort audible scrub preview."""
        if not self._audio_active or self._audio_manager is None:
            return

        try:
            state = self._audio_manager.get_state()
            state_name = getattr(state, "name", str(state)).lower()
        except Exception:
            state_name = ""
        if state_name == "playing":
            return

        now = time.monotonic()
        elapsed_ms = (now - self._scrub_audio_last_emit_at) * 1000.0
        if (
            not force
            and self._scrub_audio_last_emit_at > 0.0
            and elapsed_ms < self._scrub_audio_request_interval_ms
        ):
            return

        preview = getattr(self._audio_manager, "preview_scrub_audio", None)
        if not callable(preview):
            return

        self._scrub_audio_last_emit_at = now
        try:
            requested = preview(
                int(frame),
                duration_ms=self._scrub_audio_preview_duration_ms,
            )
        except TypeError:
            requested = preview(int(frame))
        playback_trace(
            "edit_page.audio_scrub_preview_request",
            frame=frame,
            requested=bool(requested),
        )

    def _on_loop_toggled(self, enabled: bool) -> None:
        """
        Propagate loop setting to audio manager.

        Args:
            enabled: Whether loop mode is enabled
        """
        if self._audio_manager is not None:
            self._audio_manager.set_loop(enabled)

    # ==================== Audio Playback Integration ====================

    def _ensure_audio_manager(self) -> Any:
        """
        Lazily create AudioPlaybackManager.

        Returns:
            AudioPlaybackManager instance, or None if unavailable.
        """
        if self._audio_manager is not None:
            return self._audio_manager

        try:
            from src.features.edit.core.audio_playback import (
                AudioPlaybackManager,
                AudioSyncMode,
            )
            self._audio_manager = AudioPlaybackManager(parent=self)
            self._audio_manager.set_sample_rate(48000)
            self._audio_manager.set_sync_mode(AudioSyncMode.AUDIO_MASTER)

            # Keep API call for compatibility; AudioOutput is thread-owned.
            self._audio_manager.prewarm_output()

            # 连接电平信号到 TimelinePanel 电平表
            # （W08：经统一重连入口，面板重附着/manager 重建都只保留
            # 一条有效连接）
            self._reconnect_timeline_level_meter()

            # 连接电平信号到 Audio Mixer 面板：
            #   - audio_level_changed（总和）→ Master 电平表
            #   - audio_track_levels_changed（per-track）→ 各轨电平表
            # 注意：EditPage 无 Qt parent，self.window() 返回自身。
            # 必须用显式注入的 _main_window 引用。
            self._reconnect_audio_mixer_signals()

            logger.debug("AudioPlaybackManager created for EditPage")
            return self._audio_manager
        except Exception as e:
            logger.warning(f"AudioPlaybackManager unavailable: {e}")
            return None

    def _reconnect_timeline_level_meter(self) -> None:
        """W08：电平信号跟随当前 timeline panel（先断旧连新，幂等）。

        manager/panel 任一缺席时只断不连；替换面板不会让旧 panel 继续
        收到更新，也不会在 manager 上累积重复连接。
        """
        self._disconnect_timeline_level_meter()
        manager = self._audio_manager
        panel = self._timeline_panel
        signal = getattr(manager, "audio_level_changed", None)
        slot = getattr(panel, "update_audio_levels", None)
        if signal is None or slot is None:
            return
        signal.connect(slot)
        self._timeline_level_connection = (signal, slot)

    def _disconnect_timeline_level_meter(self) -> None:
        if self._timeline_level_connection is None:
            return
        signal, slot = self._timeline_level_connection
        try:
            signal.disconnect(slot)
        except TypeError:
            pass  # already disconnected (e.g. panel destroyed)
        self._timeline_level_connection = None

    def shutdown_audio_playback(self) -> None:
        """Fully stop and release all edit-page audio worker resources.

        ``AudioPlaybackManager.stop()`` only joins the live playback thread;
        the audible scrub worker and its decoder graph remain alive.  The
        manager is parented to this QWidget, so leaving either worker alive
        until Qt destroys the widget can make ``QThread::~QThread()`` abort
        the process on macOS.  Keep this boundary idempotent so it is safe to
        call from both page transitions and the main-window shutdown path.
        """
        manager = self._audio_manager
        self._audio_active = False
        self._audio_timeline_signature = None
        self._disconnect_timeline_level_meter()  # W08：停机即断电平订阅
        if manager is None:
            return

        try:
            cleanup = getattr(manager, "cleanup", None)
            if callable(cleanup):
                cleanup()
            else:
                # Compatibility with older AudioPlaybackManager versions.
                manager.stop()
        except Exception:
            # Shutdown must continue far enough to let the remaining Qt
            # workers be stopped by their owners; never mask window close.
            logger.exception("Failed to clean up edit-page audio playback")

    def start_timeline_audio_playback(self, timeline: Any, start_frame: int) -> bool:
        """Start timeline audio for a transport driven outside the Edit page.

        The Color page plays timeline clips through its own graded video
        engine; this entry point lets that transport reuse Edit's audio
        graph.  ``on_leave`` releases the audio workers when Edit is hidden,
        so rebuild them lazily here exactly like ``on_enter`` does.

        Args:
            timeline: TimelineSequence whose audio tracks should play; None
                falls back to the currently bound timeline.
            start_frame: Timeline frame to start from.

        Returns:
            True when audio playback actually started.
        """
        if timeline is None:
            timeline = self._current_timeline
        if timeline is None:
            return False
        if getattr(timeline, "timeline_id", None) != getattr(
            self._current_timeline, "timeline_id", None
        ):
            # External transport grades a clip from another sequence; follow
            # it so the audio graph matches the video's timeline coordinates.
            self.set_timeline(timeline)
        if not self._audio_active:
            self._sync_audio_tracks_from_timeline()
        if not self._audio_active or self._audio_manager is None:
            return False
        self._audio_manager.play(start_frame=int(start_frame))
        return True

    def pause_timeline_audio_playback(self) -> None:
        """Pause timeline audio started for an external transport."""
        if self._audio_manager is not None:
            self._audio_manager.pause()

    def set_monitor_master_muted(self, muted: bool) -> bool:
        """Monitor-scoped master-bus mute for external transports (CV-B06).

        Color 页的监看静音走 Master 总线（设备输出无声、视频照常推进），
        不触碰任何轨道/片段 volume——用户在 Edit 混音器里静音的轨道在
        解除监看静音后保持原状。音频服务未就绪时返回 False（不虚报）。
        """
        if self._audio_manager is None:
            return False
        self._audio_manager.set_master_muted(bool(muted))
        return True

    def monitor_master_muted(self) -> bool | None:
        """Master 总线静音的真实状态；音频服务缺席返回 None。"""
        if self._audio_manager is None:
            return None
        return bool(self._audio_manager.is_master_muted())

    def reanchor_timeline_audio(self, frame: int, threshold_frames: int = 5) -> None:
        """Re-seek timeline audio after the external transport drifted away.

        The external transport owns its own clock, so audio can drift slowly
        or jump when the video wraps/loops its in/out range.  Small drift is
        ignored to keep the ring buffer running; larger offsets re-seek the
        audio clock to the video's timeline frame.
        """
        if not self._audio_active or self._audio_manager is None:
            return
        try:
            state_name = getattr(self._audio_manager.get_state(), "name", "").lower()
        except Exception:
            return
        if state_name != "playing":
            return
        try:
            audio_frame = int(self._audio_manager.get_current_frame())
        except Exception:
            return
        if abs(audio_frame - int(frame)) <= threshold_frames:
            return
        self._audio_manager.seek(int(frame))

    @property
    def audio_graph_revision(self) -> int:
        """P0-01: 当前已提交音频图的 revision（-1=尚未提交）。"""
        return self._audio_graph_revision

    @property
    def audio_routing_errors(self) -> tuple[str, ...]:
        """P0-02: 当前音频路由图校验错误（空元组=图合法）。

        非空时音频同步被阻断在上一张有效图，UI/保存工程应向用户展示
        这些可定位错误（含轨道/Bus 名称）。
        """
        return self._audio_routing_errors

    def _sync_audio_timebase_only(self, audio_mgr=None) -> None:
        """路由门阻断期间仅同步时间基准（fps/时长），不重建轨道。"""
        if audio_mgr is None:
            return
        try:
            timeline_fps = getattr(self._current_timeline, 'fps', self._fps)
            if hasattr(timeline_fps, 'to_fps'):
                timeline_fps = timeline_fps.to_fps()
            else:
                timeline_fps = float(timeline_fps)
            timeline_duration = getattr(
                self._current_timeline, 'duration_frames', 0) or 0
            if timeline_duration <= 0:
                timeline_duration = self._compute_timeline_duration()
            audio_mgr.set_timeline_fps(timeline_fps)
            audio_mgr.set_timeline_duration(timeline_duration)
        except Exception as e:
            logger.debug("Audio timebase sync skipped: %s", e)


    def set_audio_sequence_resolver(self, resolver: Any) -> None:
        """P1-02: 注入嵌套序列解析器（timeline_id → TimelineSequence）。

        音频同步用它把嵌套片段引用的子序列音频展开进父时间线混音
        （与预览渲染器的 set_sequence_resolver 同源，由 file_controller
        在同一处注入）。置 None 关闭嵌套音频展开。
        """
        self._audio_sequence_resolver = resolver if callable(resolver) else None

    def _sync_audio_tracks_from_timeline(self) -> None:
        """
        Rebuild audio manager tracks from the current timeline's audio clips.

        Called when timeline changes (set_timeline, clip added/removed).
        Only processes audio_tracks from the TimelineSequence.
        """
        if self._current_timeline is None:
            self._audio_active = False
            self._audio_timeline_signature = None
            return

        # Check if timeline has audio tracks with clips
        audio_tracks = getattr(self._current_timeline, 'audio_tracks', ())

        # P1-02: 嵌套序列音频展开——嵌套片段（可宿主在视频轨）引用的子序列
        # 音频轨平移进父坐标作为虚拟轨，与导出共用同一纯函数。展开先于
        # has_audio 判定：父轨无音频但嵌套贡献音频时仍需激活。
        project = self._get_project() if callable(getattr(self, "_get_project", None)) else None
        if self._audio_sequence_resolver is not None and project is not None:
            try:
                from src.features.edit.core.nested_sequence import (
                    flatten_nested_audio_tracks,
                )
                # F3.1：预览侧此前不收 diagnostics（父 retime 嵌套音频
                # 静默丢失只有日志）——现在结构化诊断落属性并发射信号。
                nested_issues: list = []
                audio_tracks = flatten_nested_audio_tracks(
                    self._current_timeline,
                    self._audio_sequence_resolver,
                    lambda aid: project.assets.get(aid) if project else None,
                    diagnostics=nested_issues,
                )
                self._last_nested_audio_diagnostics = tuple(nested_issues)
                if nested_issues:
                    self.signal_hub.nested_audio_diagnostics_changed.emit(
                        self._last_nested_audio_diagnostics)
                    for issue in nested_issues:
                        logger.warning(
                            "nested audio %s: %s (clip %s)",
                            issue.code, issue.message, issue.clip_id)
            except Exception:  # noqa: BLE001 — 展开失败退回原轨，不阻断音频
                logger.exception("nested audio expansion failed; using own tracks")
                audio_tracks = getattr(self._current_timeline, 'audio_tracks', ())

        has_audio = False
        for track in audio_tracks:
            if track.clips:
                has_audio = True
                break

        if not has_audio:
            self._audio_active = False
            self._audio_timeline_signature = None
            self._audio_routing_errors = ()  # 空图合法，清除门错误
            # Stop and clean up audio if previously active
            if self._audio_manager is not None:
                self._audio_manager.stop()
                for tid in self._audio_manager.get_all_track_ids():
                    self._audio_manager.remove_track(tid)
            return

        # P0-02: 路由图校验作为激活门。非法路由（环/未知 output/send 目标）
        # 不再只记录告警后继续重建——那会让错误路由静默回退 Master 播放出
        # 错误声音。新图无效时：保留上一张有效图的音频状态（已激活则继续
        # 用旧轨道播放，未激活则保持静音），记录可定位错误；修复/撤销回到
        # 有效 revision 后自动恢复同步。导出路径继续按 timeline_export 阻断。
        try:
            from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
            snap = build_audio_graph_snapshot(self._current_timeline)
        except Exception as e:
            logger.debug("Audio graph validation skipped: %s", e)
            snap = None

        if snap is not None and not snap.is_valid:
            self._audio_routing_errors = tuple(snap.validation_errors)
            logger.warning(
                "Audio routing graph invalid, keeping last valid graph "
                "(audio sync blocked): %s",
                "; ".join(snap.validation_errors),
            )
            playback_trace(
                "audio.routing_gate_blocked",
                errors=len(snap.validation_errors),
            )
            # 审计修复：路由被阻断只应冻结"轨道重建"，时间基准（fps/时长）
            # 仍须同步——否则图无效期间修改时间线时长/fps，AV 同步基准陈旧
            self._sync_audio_timebase_only(audio_mgr=self._ensure_audio_manager())
            return
        if snap is not None:
            # 图重新合法：清除错误并继续正常同步（含撤销/修复路径）
            self._audio_routing_errors = ()

        self._audio_active = True
        audio_mgr = self._ensure_audio_manager()
        if audio_mgr is None:
            return

        # Get project for asset path resolution
        project = self._get_project()
        if project is None:
            # 加载时序问题：edit_page 被通知同步音频但 project 引用尚未注入。
            # 属于一次性初始化噪音，不是错误，降级到 debug。
            logger.debug("Cannot sync audio tracks: no project reference")
            return

        timeline_fps = getattr(self._current_timeline, 'fps', self._fps)
        if hasattr(timeline_fps, 'to_fps'):
            timeline_fps = timeline_fps.to_fps()
        else:
            timeline_fps = float(timeline_fps)

        timeline_duration = getattr(self._current_timeline, 'duration_frames', 0) or 0
        if timeline_duration <= 0:
            timeline_duration = self._compute_timeline_duration()

        signature = self._build_audio_timeline_signature(
            audio_tracks,
            project,
            timeline_fps,
            timeline=self._current_timeline,
        )
        active_track_ids = tuple(sorted(audio_mgr.get_all_track_ids()))
        # AM-05: 签名结构为 (fps, master_volume, master_muted, tuple(track_entries))，
        # track_entries 在 index 3；每条 track entry 首元素是 track_id。
        signature_track_ids = tuple(sorted(track_sig[0] for track_sig in signature[3]))
        if (
            signature == self._audio_timeline_signature
            and active_track_ids == signature_track_ids
        ):
            audio_mgr.set_timeline_fps(timeline_fps)
            audio_mgr.set_timeline_duration(timeline_duration)
            playback_trace(
                "audio_manager.track_sync_skipped",
                tracks=len(active_track_ids),
                active=self._audio_active,
                fps=timeline_fps,
                duration=timeline_duration,
            )
            return

        self._audio_timeline_signature = signature
        # P0-01: 换图诊断——每次音频图提交记录 revision（映射 timeline
        # revision）与轨道规模，供回放问题定位与 SnapshotHistory 对账。
        graph_rev = int(getattr(self._current_timeline, "revision", 0) or 0)
        switched = graph_rev != self._audio_graph_revision
        self._audio_graph_revision = graph_rev
        playback_trace(
            "audio.graph_committed",
            revision=graph_rev,
            switched=switched,
            tracks=len(signature[3]) if len(signature) > 3 else 0,
        )

        # Clear existing tracks
        try:
            state = audio_mgr.get_state()
            state_name = getattr(state, "name", str(state)).lower()
        except Exception:
            state_name = ""
        existing_track_ids = tuple(audio_mgr.get_all_track_ids())
        if existing_track_ids:
            quiesce = getattr(audio_mgr, "quiesce_for_timeline_rebuild", None)
            if callable(quiesce):
                quiesce()
            elif state_name == "playing":
                audio_mgr.pause()

        for tid in existing_track_ids:
            audio_mgr.remove_track(tid)

        audio_mgr.set_timeline_fps(timeline_fps)
        audio_mgr.set_timeline_duration(timeline_duration)

        # Add clips from audio_tracks.  Keep one mixer track state per
        # timeline track, but register every clip decoder under that state.
        from src.features.edit.core.audio_playback import AudioTrackState
        for track in audio_tracks:
            is_bus = bool(getattr(track, "is_bus", False))

            # AM-02: Bus 轨没有 clips/decoder，但必须注册到引擎，否则其路由输入
            # 会被 mix_samples 阶段 2 丢弃。普通轨无 clips 且非 Bus 可跳过。
            if is_bus:
                bus_state = AudioTrackState(
                    track_id=track.track_id,
                    volume=track.volume,
                    muted=track.is_muted,
                    solo=track.is_solo,
                    pan=track.pan,
                    enabled=track.is_enabled,
                )
                try:
                    register_bus = getattr(audio_mgr, "register_bus_track", None)
                    if callable(register_bus):
                        register_bus(track.track_id, bus_state)
                    else:
                        # 旧版引擎无 register_bus_track：退回 add_track 不可行（需 decoder），
                        # 记录降级日志。新引擎已具备该方法。
                        logger.warning(
                            "engine lacks register_bus_track; bus %s will be silent",
                            track.track_id,
                        )
                    # AM-05: Bus 的 EQ/FX/automation/routing 同样需同步到引擎
                    self._sync_track_audio_params(audio_mgr, track)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Failed to register bus track %s: %s", track.track_id, e)
                continue

            if not track.clips:
                continue

            initial_state = AudioTrackState(
                track_id=track.track_id,
                volume=track.volume,
                muted=track.is_muted,
                solo=track.is_solo,
                pan=track.pan,
                enabled=track.is_enabled,
            )

            registered_any = False
            for clip in track.clips:
                asset = project.assets.get(clip.asset_id)
                if asset is None:
                    continue
                # A2: 冻结段无对应音频进度——混音跳过 freeze_source_frame
                # 非 None 的音频 clip（冻结画面保持、静音）。
                if getattr(clip, "freeze_source_frame", None) is not None:
                    continue
                # P1-02: 嵌套虚拟素材不直接解码（topos:// URI 无音频流）——
                # 其子序列音频已由 flatten_nested_audio_tracks 展开成虚拟轨。
                if getattr(asset, "is_nested_sequence", False):
                    continue

                # F2.10：外录绑定——音频消费切到 WAV 源（偏移折算进
                # source_start；预览/导出/Stem 同一 resolve 口径）
                from src.features.edit.core.audio_sync import (
                    bound_audio_frame,
                    resolve_audio_source,
                )
                audio_path_str, audio_offset = resolve_audio_source(
                    project, asset)
                asset_path = Path(audio_path_str or asset.path)
                if not asset_path.exists():
                    continue

                # 从 clip 和 asset 获取源参数，确保音频正确解码和重采样
                clip_source_start = getattr(clip, 'source_start', 0) or 0
                if audio_offset:
                    clip_source_start = bound_audio_frame(
                        clip_source_start, audio_offset,
                        float(getattr(asset, "fps", 0) or 0) or 24.0)
                clip_timeline_start = getattr(clip, 'timeline_start', 0) or 0
                clip_timeline_duration = getattr(clip, 'timeline_duration', 0) or 0
                asset_fps = getattr(asset, 'fps', None) or 0.0
                # RMA-03: clip 速度（effective_speed，resample 变调）与淡入淡出（帧）。
                # 实时路径此前完全不传这两项，导致与离线导出漂移（导出有 speed/fade，预览没有）。
                clip_speed = _resolve_clip_effective_speed(clip)
                clip_fade_in = int(getattr(clip, 'fade_in_duration', 0) or 0)
                clip_fade_out = int(getattr(clip, 'fade_out_duration', 0) or 0)
                # F3.2: 保调变速——与离线导出同口径（preserve_pitch_request）。
                # 请求保调但属不支持组合（倒放/速度曲线）时实时同样回退变调
                # 并留痕，不静默降级。
                from src.features.edit.core.audio_retime import (
                    preserve_pitch_request,
                )
                _pp_requested, _pp_speed = preserve_pitch_request(clip)
                clip_preserve_pitch = bool(
                    _pp_requested and _pp_speed is not None
                    and abs(clip_speed - 1.0) > 1e-9)
                if _pp_requested and not clip_preserve_pitch:
                    logger.warning(
                        "clip %s preserve-pitch 不支持倒放/速度曲线，"
                        "实时预览回退变调（与导出回退策略一致）", clip.clip_id)

                if audio_mgr.add_track(
                    track.track_id,
                    asset_path,
                    initial_state,
                    source_start=clip_source_start,
                    source_fps=float(asset_fps),
                    timeline_start=clip_timeline_start,
                    timeline_duration=clip_timeline_duration,
                    replace_existing=not registered_any,
                    clip_id=clip.clip_id,
                    speed=clip_speed,
                    fade_in_frames=clip_fade_in,
                    fade_out_frames=clip_fade_out,
                    gain_db=float(getattr(clip, "audio_gain_db", 0.0) or 0.0),
                    # W07：测试替身/旧会话工程可能没有源补丁覆盖表——
                    # 缺席按默认补丁（getattr 防御，同 timeline_view 修法）
                    source_channel_map=patch_for_asset(
                        getattr(project, 'source_patch_overrides', None)
                        or {},
                        clip.asset_id,
                    ).audio_channel_map,
                    preserve_pitch=clip_preserve_pitch,
                    fade_in_samples=getattr(clip, "fade_in_samples", None),
                    fade_out_samples=getattr(clip, "fade_out_samples", None),
                    audio_sample_offset=int(getattr(
                        clip, "audio_sample_offset", 0) or 0),
                    source_sample_offset=int(getattr(
                        clip, "source_sample_offset", 0) or 0),
                    # B16：淡化曲线（None=音频 legacy equal_power）；
                    # R09：两端独立曲线（优先于共享字段）
                    fade_curve=getattr(clip, "fade_curve", None),
                    fade_curve_in=getattr(
                        clip, "effective_fade_curve_in", None),
                    fade_curve_out=getattr(
                        clip, "effective_fade_curve_out", None),
                ):
                    registered_any = True
                    # P-clip-FX: 把该 clip 的 audio 效果同步到引擎
                    self._sync_clip_audio_effects(audio_mgr, clip, track.track_id)

            if registered_any:
                # AM-05: 轨道级 EQ/FX/automation/routing 同步到引擎（此前依赖
                # edit_controller 的被动路径，重开项目后丢失）。
                self._sync_track_audio_params(audio_mgr, track)
                logger.debug(f"Audio track registered: {track.track_id} ({track.name})")

        # AM-05: master 状态同步到引擎（与 edit_controller 一致，但此处是唯一真相源）。
        if self._current_timeline is not None:
            try:
                audio_mgr.set_master_volume(float(getattr(self._current_timeline, "master_volume", 1.0)))
                audio_mgr.set_master_muted(bool(getattr(self._current_timeline, "master_muted", False)))
                # §3.1: master automation + 模式同步
                set_master_auto = getattr(audio_mgr, "set_master_automation", None)
                if set_master_auto is not None:
                    set_master_auto(dict(getattr(self._current_timeline, "master_automation", {}) or {}))
                set_master_auto_mode = getattr(audio_mgr, "set_master_automation_mode", None)
                if set_master_auto_mode is not None:
                    set_master_auto_mode(str(getattr(self._current_timeline, "master_automation_mode", "off") or "off"))
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to sync master state to engine: %s", e)

        track_count = len(audio_mgr.get_all_track_ids())
        playback_trace(
            "audio_manager.track_sync",
            tracks=track_count,
            active=self._audio_active,
            fps=timeline_fps,
            duration=timeline_duration,
        )
        logger.debug(f"Audio tracks synced: {track_count} tracks")

        prepare = getattr(audio_mgr, "prepare_for_playback", None)
        if callable(prepare) and state_name != "playing":
            try:
                if prepare(int(self._current_frame)):
                    playback_trace(
                        "audio_manager.track_sync_preroll",
                        frame=int(self._current_frame),
                        tracks=track_count,
                    )
            except Exception as e:
                logger.debug(f"Audio preroll after track sync failed: {e}")

    def _sync_clip_audio_effects(self, audio_mgr: Any, clip: Any, track_id: str) -> None:
        """P-clip-FX: 把单个 clip 的 audio 效果同步到音频引擎。

        遍历 clip.effects，挑出属于 mixer audio 的 EffectInstance（effect_type
        在 AUDIO_MIXER_EFFECT_TYPES 内），按 order 排序，转成 EffectChain 的
        entries 格式 [(effect_type, params_dict, enabled), ...]，调
        audio_mgr.set_clip_effects。视频类效果与未知类型自动忽略。

        Args:
            audio_mgr: AudioPlaybackManager
            clip: ClipInstance
            track_id: 所属轨道 ID（用于 remove_track 反查清理）
        """
        set_clip_fx = getattr(audio_mgr, "set_clip_effects", None)
        if not callable(set_clip_fx):
            return  # 旧版引擎无此接口，安全跳过

        effects = getattr(clip, "effects", ()) or ()
        # D04（第六轮审计）：entries 与动画采样器统一走 audio_retime 共享
        # 构建——实时预览、成片导出、Stem 三条路径对 animated_params 的
        # 求值口径一致（此前预览只送静态 params，关键帧对声音零影响）。
        from src.features.edit.core.audio_retime import (
            animated_fx_sampler,
            clip_fx_entries,
        )

        entries = clip_fx_entries(effects, None)
        if not entries:
            return
        sampler = animated_fx_sampler(effects)

        try:
            accepts_sampler = "animated_sampler" in inspect.signature(set_clip_fx).parameters
        except (TypeError, ValueError):
            accepts_sampler = False
        try:
            if accepts_sampler:
                set_clip_fx(clip.clip_id, entries, track_id=track_id,
                            animated_sampler=sampler)
            else:
                set_clip_fx(clip.clip_id, entries, track_id=track_id)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"set_clip_effects failed for {clip.clip_id}: {e}")

    def _sync_track_audio_params(self, audio_mgr: Any, track: Any) -> None:
        """AM-05: 把轨道级 EQ/FX/automation/routing 同步到音频引擎。

        此前这些参数只在 edit_controller 的 mixer 面板交互路径同步（用户调
        EQ 对话框时），重开项目/undo 后不会进入实时引擎。本方法在注册循环里
        对每个轨（含 Bus）调用，使 edit_page 成为唯一真相源。

        各 set 方法都是幂等的（覆盖式 set_params），重复调用无副作用。

        Args:
            audio_mgr: AudioPlaybackManager
            track: EditTrack（普通轨或 Bus）
        """
        track_id = getattr(track, "track_id", "")
        # Strip EQ（轨道级）
        strip_eq = getattr(track, "strip_eq", None)
        set_eq = getattr(audio_mgr, "set_track_strip_eq", None)
        if callable(set_eq):
            try:
                set_eq(track_id, strip_eq)
            except Exception as e:  # noqa: BLE001
                logger.debug("set_track_strip_eq failed for %s: %s", track_id, e)
        # FX 机架链
        effect_chain = getattr(track, "effect_chain", None)
        set_chain = getattr(audio_mgr, "set_track_effect_chain", None)
        if callable(set_chain):
            try:
                set_chain(track_id, effect_chain)
            except Exception as e:  # noqa: BLE001
                logger.debug("set_track_effect_chain failed for %s: %s", track_id, e)
        # 自动化（Read 模式回放）
        automation = getattr(track, "automation", None)
        set_auto = getattr(audio_mgr, "set_track_automation", None)
        if callable(set_auto) and automation:
            try:
                set_auto(track_id, dict(automation))
            except Exception as e:  # noqa: BLE001
                logger.debug("set_track_automation failed for %s: %s", track_id, e)
        # AM-07: 自动化模式（off/read/write/touch/latch）同步到引擎
        set_auto_mode = getattr(audio_mgr, "set_track_automation_mode", None)
        if callable(set_auto_mode):
            try:
                set_auto_mode(track_id, str(getattr(track, "automation_mode", "off") or "off"))
            except Exception as e:  # noqa: BLE001
                logger.debug("set_track_automation_mode failed for %s: %s", track_id, e)
        # 路由（output_bus_id + sends + is_bus）
        set_routing = getattr(audio_mgr, "set_track_routing", None)
        if callable(set_routing):
            try:
                sends = getattr(track, "sends", ()) or ()
                sends_list = [
                    (s.target_bus_id, s.level)
                    for s in sends if getattr(s, "enabled", True)
                ]
                set_routing(
                    track_id,
                    getattr(track, "output_bus_id", None),
                    sends_list,
                    is_bus=bool(getattr(track, "is_bus", False)),
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("set_track_routing failed for %s: %s", track_id, e)
        # AT (Audio Transition): 把该轨的转场实例同步到音频引擎（仅 audio:*
        # 实例会被引擎消费；视频转场对音频无影响，过滤后即被丢弃）。
        transitions = getattr(track, "transitions", ()) or ()
        set_audio_tr = getattr(audio_mgr, "set_audio_transitions", None)
        if callable(set_audio_tr) and transitions:
            try:
                set_audio_tr(track_id, list(transitions))
            except Exception as e:  # noqa: BLE001
                logger.debug("set_audio_transitions failed for %s: %s", track_id, e)

    def _build_audio_timeline_signature(
        self,
        audio_tracks: Any,
        project: Any,
        timeline_fps: float,
        timeline: Any = None,
    ) -> tuple[Any, ...]:
        """Return a stable signature for the audio graph represented by timeline.

        AM-05: 签名必须覆盖**所有影响声音的字段**，否则签名相等时同步提前返回会
        导致模型修改（pan/EQ/FX/automation/routing/master/speed/fade）不下发引擎。
        每增补一个声音字段都应有对应的签名完整性测试（test_audio_signature_completeness）。
        """

        def _sig_value(v: Any) -> Any:
            """把不可哈希的 list/dict/对象 转成可哈希的 tuple，供 signature 比较。"""
            if isinstance(v, list):
                return tuple(_sig_value(x) for x in v)
            if isinstance(v, dict):
                return tuple(sorted((k, _sig_value(x)) for k, x in v.items()))
            # AM-05: StripEQParams / EffectChainParams / Send 等有 to_dict 的领域对象
            if hasattr(v, "to_dict") and callable(v.to_dict):
                return _sig_value(v.to_dict())
            return v

        def _strip_eq_sig(track: Any) -> Any:
            """轨道级 Strip EQ 签名（None=禁用）。"""
            eq = getattr(track, "strip_eq", None)
            return _sig_value(eq) if eq is not None else None

        def _effect_chain_sig(track: Any) -> Any:
            """轨道级 FX 机架链签名（None=未使用）。"""
            chain = getattr(track, "effect_chain", None)
            return _sig_value(chain) if chain is not None else None

        def _automation_sig(track: Any) -> Any:
            """轨道级自动化签名（dict of KeyframeTrack）。"""
            auto = getattr(track, "automation", None)
            if not auto:
                return None
            return _sig_value(auto)

        def _sends_sig(track: Any) -> Any:
            """Send 列表签名（target_bus_id + level + enabled）。"""
            sends = getattr(track, "sends", ()) or ()
            return tuple(
                (getattr(s, "target_bus_id", ""), float(getattr(s, "level", 0.0)),
                 bool(getattr(s, "enabled", True)))
                for s in sends
            )

        def _audio_transitions_sig(track: Any) -> Any:
            """AT: 该轨音频转场签名（仅 audio:* 实例影响声音）。

            transition_type / left_clip_id / right_clip_id / center_frame /
            duration / alignment 任一变化都需重新下发到引擎，否则 mix_samples
            不会感知到新增/修改/删除的 crossfade。
            """
            transitions = getattr(track, "transitions", ()) or ()
            entries: list = []
            for tr in transitions:
                ttype = getattr(tr, "transition_type", "")
                # 视频转场不影响音频，跳过以避免无谓重同步。
                if not ttype.startswith("audio:"):
                    continue
                entries.append((
                    ttype,
                    getattr(tr, "transition_id", ""),
                    getattr(tr, "left_clip_id", ""),
                    getattr(tr, "right_clip_id", ""),
                    int(getattr(tr, "center_frame", 0) or 0),
                    int(getattr(tr, "duration", 0) or 0),
                    str(getattr(tr, "alignment", "centered") or "centered"),
                ))
            return tuple(entries)

        track_entries: list[tuple[Any, ...]] = []
        for track in audio_tracks:
            clip_entries: list[tuple[Any, ...]] = []
            for clip in getattr(track, "clips", ()):
                asset = getattr(project, "assets", {}).get(getattr(clip, "asset_id", ""))
                asset_path = str(getattr(asset, "path", "")) if asset is not None else ""
                asset_fps = float(getattr(asset, "fps", 0.0) or 0.0) if asset is not None else 0.0
                # P-clip-FX: 纳入该 clip 的 audio 效果（effect_type + 有序参数），
                # 任一参数变化都会触发重新同步到音频引擎。视频效果不影响音频，
                # 不纳入以避免无谓重同步。
                fx_sig: list = []
                for effect in sorted(
                    getattr(clip, "effects", ()) or (),
                    key=lambda ef: getattr(ef, "order", 0),
                ):
                    etype = getattr(effect, "effect_type", "")
                    if etype not in AUDIO_MIXER_EFFECT_TYPES:
                        continue
                    fx_sig.append((
                        etype,
                        tuple(sorted(
                            (k, _sig_value(v)) for k, v in (getattr(effect, "params", None) or {}).items()
                        )),
                        bool(getattr(effect, "is_enabled", True)),
                    ))
                # AM-06: 纳入 clip speed 与淡入淡出时长（影响实时解码时长与包络）
                speed = getattr(clip, "speed", 1.0)
                # SpeedParams 或 float 都需稳定序列化
                if hasattr(speed, "to_dict"):
                    speed_sig: Any = _sig_value(speed.to_dict())
                else:
                    speed_sig = float(speed) if speed is not None else 1.0
                clip_entries.append((
                    getattr(clip, "clip_id", ""),
                    getattr(clip, "asset_id", ""),
                    asset_path,
                    asset_fps,
                    int(getattr(clip, "source_start", 0) or 0),
                    int(getattr(clip, "timeline_start", 0) or 0),
                    int(getattr(clip, "timeline_duration", 0) or 0),
                    bool(getattr(clip, "is_enabled", True)),
                    speed_sig,
                    int(getattr(clip, "fade_in_duration", 0) or 0),
                    int(getattr(clip, "fade_out_duration", 0) or 0),
                    getattr(clip, "fade_curve", None),
                    tuple(fx_sig),
                ))
            # AM-02: Bus 轨即使无 clips 也生成 entry（否则 Bus 变更不扰动签名）。
            # 普通轨无 clips 且非 Bus 仍可跳过（无声音贡献）。
            is_bus = bool(getattr(track, "is_bus", False))
            if not clip_entries and not is_bus:
                continue
            track_entries.append((
                getattr(track, "track_id", ""),
                float(getattr(track, "volume", 1.0) or 0.0),
                # AM-05: pan 此前遗漏，导致声像修改不下发引擎
                float(getattr(track, "pan", 0.0) or 0.0),
                bool(getattr(track, "is_muted", False)),
                bool(getattr(track, "is_solo", False)),
                bool(getattr(track, "is_enabled", True)),
                # AM-05: 轨道级 EQ / FX / automation 此前完全未纳入签名
                _strip_eq_sig(track),
                _effect_chain_sig(track),
                _automation_sig(track),
                # AM-07: 自动化模式（off/read/write/touch/latch）影响是否读取曲线
                str(getattr(track, "automation_mode", "off") or "off"),
                # AM-02/AM-05: 路由字段（is_bus / output_bus_id / sends）
                is_bus,
                getattr(track, "output_bus_id", None),
                _sends_sig(track),
                # AT: 音频转场（audio:* 实例）变化需重新下发 crossfade 到引擎
                _audio_transitions_sig(track),
                tuple(clip_entries),
            ))

        # AM-05: master 状态（master_volume / master_muted）纳入顶层签名
        master_volume = float(getattr(timeline, "master_volume", 1.0)) if timeline is not None else 1.0
        master_muted = bool(getattr(timeline, "master_muted", False)) if timeline is not None else False
        # §3.1: master automation + 模式纳入签名（修改触发重同步）
        master_auto = getattr(timeline, "master_automation", {}) or {} if timeline is not None else {}
        master_auto_mode = str(getattr(timeline, "master_automation_mode", "off") or "off") if timeline is not None else "off"
        master_auto_sig = (
            _sig_value(master_auto) if master_auto else None,
            master_auto_mode,
        )

        return (
            round(float(timeline_fps), 6),
            float(master_volume),
            bool(master_muted),
            tuple(track_entries),
            master_auto_sig,
        )

    def _build_timeline_preview_signature(self, timeline: Any) -> tuple[Any, ...]:
        """Return timeline state that can change visible program preview pixels."""
        if timeline is None:
            return ()

        def _float(value: Any, default: float = 0.0) -> float:
            try:
                return round(float(value), 6)
            except (TypeError, ValueError):
                return default

        video_tracks: list[tuple[Any, ...]] = []
        for track in getattr(timeline, "video_tracks", ()) or ():
            clips = tuple(getattr(track, "clips", ()) or ())
            transitions = tuple(getattr(track, "transitions", ()) or ())
            if not clips and not transitions:
                continue

            clip_entries: list[tuple[Any, ...]] = []
            for clip in clips:
                transform = getattr(clip, "transform", None)
                crop = getattr(clip, "crop", None)
                clip_entries.append((
                    getattr(clip, "clip_id", ""),
                    getattr(clip, "asset_id", ""),
                    int(getattr(clip, "timeline_start", 0) or 0),
                    int(getattr(clip, "timeline_duration", 0) or 0),
                    int(getattr(clip, "source_start", 0) or 0),
                    int(getattr(clip, "source_duration", 0) or 0),
                    _float(getattr(clip, "opacity", 1.0), 1.0),
                    int(getattr(clip, "fade_in_duration", 0) or 0),
                    int(getattr(clip, "fade_out_duration", 0) or 0),
                    getattr(clip, "fade_curve", None),
                    bool(getattr(clip, "is_enabled", True)),
                    repr(getattr(clip, "effects", ())),
                    repr(getattr(clip, "color_stack_id", None)),
                    repr(getattr(clip, "color_shared", None)),
                    repr(getattr(clip, "speed", None)),
                    getattr(clip, "freeze_source_frame", None),
                    repr(transform),
                    repr(crop),
                ))

            transition_entries = tuple(
                (
                    getattr(trans, "transition_id", ""),
                    int(getattr(trans, "center_frame", 0) or 0),
                    int(getattr(trans, "duration", 0) or 0),
                    repr(getattr(trans, "transition_type", None)),
                )
                for trans in transitions
            )
            video_tracks.append((
                getattr(track, "track_id", ""),
                int(getattr(track, "index", 0) or 0),
                bool(getattr(track, "is_enabled", True)),
                bool(getattr(track, "is_muted", False)),
                tuple(clip_entries),
                transition_entries,
            ))

        subtitle_tracks: list[tuple[Any, ...]] = []
        for track in getattr(timeline, "subtitle_tracks", ()) or ():
            # SUB-P0-05: subtitle tracks store cues in ``subtitle_clips``, not
            # ``clips``; and SubtitleClip has no ``params`` dict. Capture every
            # field that can change visible program pixels: timing, text,
            # font, layout, transform, fill/outline/shadow, enabled state, plus
            # track-level visibility/enabled/lock so hidden/locked tracks and
            # cue edits all invalidate the preview signature.
            clips = tuple(getattr(track, "subtitle_clips", ()) or ())
            subtitle_tracks.append((
                getattr(track, "track_id", ""),
                int(getattr(track, "index", 0) or 0),
                bool(getattr(track, "is_enabled", True)),
                bool(getattr(track, "is_visible", True)),
                bool(getattr(track, "is_locked", False)),
                repr(
                    getattr(
                        getattr(track, "caption_metadata", None),
                        "default_style",
                        None,
                    )
                ),
                tuple(
                    self._subtitle_clip_signature(clip)
                    for clip in clips
                ),
            ))

        return (
            int(getattr(timeline, "width", 0) or 0),
            int(getattr(timeline, "height", 0) or 0),
            _float(getattr(timeline, "fps", 0.0)),
            tuple(video_tracks),
            tuple(subtitle_tracks),
        )

    @staticmethod
    def _subtitle_clip_signature(clip: Any) -> tuple[Any, ...]:
        """Build a visual-content signature for a SubtitleClip.

        SUB-P0-05: every field that can change program-monitor pixels is
        included so the preview signature invalidates on text/style/transform/
        timing edits and stays stable for unrelated changes. Order matches the
        SubtitleClip field declaration for readability.
        """
        def _f(value: Any, default: float = 0.0) -> float:
            try:
                return round(float(value), 6)
            except (TypeError, ValueError):
                return default

        return (
            getattr(clip, "clip_id", ""),
            # timing
            int(getattr(clip, "timeline_start", 0) or 0),
            int(getattr(clip, "timeline_duration", 0) or 0),
            # content
            repr(getattr(clip, "text", "")),
            # font
            repr(getattr(clip, "font_family", "")),
            int(getattr(clip, "font_size", 0) or 0),
            repr(getattr(clip, "font_weight", "")),
            bool(getattr(clip, "italic", False)),
            # layout
            repr(getattr(clip, "halign", "")),
            repr(getattr(clip, "valign", "")),
            _f(getattr(clip, "letter_spacing", 0.0)),
            _f(getattr(clip, "line_spacing", 1.0), 1.0),
            # transform
            _f(getattr(clip, "translate_x", 0.0)),
            _f(getattr(clip, "translate_y", 0.0)),
            _f(getattr(clip, "scale_x", 1.0), 1.0),
            _f(getattr(clip, "scale_y", 1.0), 1.0),
            _f(getattr(clip, "rotation", 0.0)),
            repr(getattr(clip, "pivot_mode", "center")),
            _f(getattr(clip, "pivot_x", 0.0)),
            _f(getattr(clip, "pivot_y", 0.0)),
            # layered caption style (track default is captured by the track
            # signature above).  A cue can keep a partial override, so its
            # serialized representation must invalidate inherited fields too.
            repr(getattr(clip, "style_override", None)),
            # fill
            _f(getattr(clip, "fill_r", 1.0), 1.0),
            _f(getattr(clip, "fill_g", 1.0), 1.0),
            _f(getattr(clip, "fill_b", 1.0), 1.0),
            _f(getattr(clip, "fill_a", 1.0), 1.0),
            # outline
            bool(getattr(clip, "outline_enable", False)),
            _f(getattr(clip, "outline_width", 0.0)),
            _f(getattr(clip, "outline_r", 0.0)),
            _f(getattr(clip, "outline_g", 0.0)),
            _f(getattr(clip, "outline_b", 0.0)),
            _f(getattr(clip, "outline_a", 1.0), 1.0),
            # shadow
            bool(getattr(clip, "shadow_enable", False)),
            _f(getattr(clip, "shadow_offset_x", 0.0)),
            _f(getattr(clip, "shadow_offset_y", 0.0)),
            _f(getattr(clip, "shadow_blur", 0.0)),
            _f(getattr(clip, "shadow_r", 0.0)),
            _f(getattr(clip, "shadow_g", 0.0)),
            _f(getattr(clip, "shadow_b", 0.0)),
            _f(getattr(clip, "shadow_a", 1.0), 1.0),
            # state
            bool(getattr(clip, "is_enabled", True)),
        )

    def _get_project(self) -> Any:
        """
        Get the EditProject reference from the timeline panel.

        Returns:
            EditProject instance, or None.
        """
        if self._timeline_panel is None:
            return None
        tl_view = self._timeline_panel.get_timeline_view()
        if tl_view and hasattr(tl_view, '_project'):
            return tl_view._project
        return None

    def _compute_timeline_duration(self) -> int:
        """
        Compute effective timeline duration from tracks.

        Returns:
            Total frame count.
        """
        if self._current_timeline is None:
            return 0
        duration = getattr(self._current_timeline, 'duration_frames', 0) or 0
        if duration > 0:
            return duration
        # Fallback: compute from clips
        for track in self._current_timeline.video_tracks + self._current_timeline.audio_tracks:
            for clip in track.clips:
                end = clip.timeline_start + clip.timeline_duration
                if end > duration:
                    duration = end
        return duration

    def _setup_audio_video_sync(self) -> None:
        """
        Keep audio transport aligned without letting audio drive video frames.

        The Edit timeline already has a single video frame clock
        (EditPlaybackController + PlaybackControllerTimer).  Letting audio
        frame callbacks call video_controller.seek() during playback fights
        that clock and makes the Program Viewer jump between frames.  Audio is
        still started/stopped with the controller and explicitly seeked on
        manual timeline seeks.
        """
        if self._audio_manager is None:
            return

        if self._audio_sync is not None and hasattr(self._audio_sync, "_sync_video_to_audio"):
            try:
                self._audio_manager.frame_changed.disconnect(self._audio_sync._sync_video_to_audio)
            except (TypeError, RuntimeError):
                pass

        frame_changed = getattr(self._audio_manager, "frame_changed", None)
        if frame_changed is not None:
            try:
                frame_changed.disconnect(self._on_audio_frame_changed)
            except (TypeError, RuntimeError):
                pass

        self._audio_sync = None
        logger.debug("Audio frame-to-video sync disabled; video controller is timeline master")

    def _on_audio_frame_changed(self, frame: int) -> None:
        """
        Handle frame change from the audio clock.

        During playback the video controller remains the timeline master.
        Audio frame callbacks are only allowed to refresh paused/manual states.

        AVSyncController 接入（隐患 3 修复）：播放期间虽然丢弃 audio frame 不抢帧，
        但用 audio frame 与 video controller frame 计算 drift 并记录到 telemetry。
        drift ≥ rebuffer 阈值（150ms）时记 warning，让 A/V 不同步可观测。
        未来可以根据 drift 决策做真正的 hold/drop/reanchor。

        Args:
            frame: Frame number from audio clock
        """
        playback_trace("edit_page.audio_frame_changed", frame=frame)
        if self._timeline_panel is not None:
            controller = self._timeline_panel.get_playback_controller()
            if controller is not None and controller.is_playing:
                # 播放期间：不抢帧，但记录 A/V drift。
                self._evaluate_avsync_drift(audio_frame=frame)
                playback_trace("edit_page.audio_frame_ignored_during_playback", frame=frame)
                return

        # Forward to the existing playhead pipeline
        self._on_playhead_changed(frame)

    def _evaluate_avsync_drift(self, audio_frame: int) -> None:
        """隐患 3 修复：用 AVSyncController 评估 audio/video drift 并记录。

        当前策略：仅记录 telemetry，不主动 hold/drop（避免抢帧风险）。
        drift ≥ 150ms 记 warning 日志，便于排查"声画不同步"问题。
        """
        try:
            from src.features.edit.core.realtime_playback import (
                AVSyncAction,
                AVSyncController,
            )
            if not hasattr(self, "_avsync_controller") or self._avsync_controller is None:
                fps = float(getattr(self, "_fps", 25.0) or 25.0)
                self._avsync_controller = AVSyncController(fps=fps)
            video_frame = 0
            if self._timeline_panel is not None:
                controller = self._timeline_panel.get_playback_controller()
                if controller is not None:
                    video_frame = int(controller.current_frame)
            decision = self._avsync_controller.evaluate(
                audio_frame=int(audio_frame),
                video_frame=video_frame,
            )
            if decision.action is AVSyncAction.REBUFFER:
                logger.warning(
                    f"A/V drift rebuffer: audio={decision.audio_frame} "
                    f"video={decision.video_frame} drift={decision.drift_ms}ms"
                )
        except Exception as e:
            logger.debug(f"_evaluate_avsync_drift failed: {e}")

    def _update_audio_track_state(self, track_id: str) -> None:
        """
        Push track mute/solo/volume from timeline data model to audio manager.

        Called when track controls change in the UI.

        Args:
            track_id: Track ID to update
        """
        if self._audio_manager is None or self._current_timeline is None:
            return

        # Find track in timeline
        for track in self._current_timeline.audio_tracks:
            if track.track_id == track_id:
                from src.features.edit.core.audio_playback import AudioTrackState
                new_state = AudioTrackState(
                    track_id=track.track_id,
                    volume=track.volume,
                    muted=track.is_muted,
                    solo=track.is_solo,
                    pan=track.pan,
                    enabled=track.is_enabled,
                )
                self._audio_manager._mixer.set_track_state(track_id, new_state)
                logger.debug(f"Audio track state updated: {track_id}")
                return

    # ==================== Page Lifecycle ====================

    def on_enter(self) -> None:
        """
        Called when switching to Edit page.

        Initializes:
        - Frame request pipeline
        - Playhead tracking
        - Audio playback sync
        - Timeline preview service (future)
        """
        self._is_active = True
        logger.info("[EditPage] Entering Edit page")

        # Seeks driven from other pages (Color clip sync) moved only the
        # playback controller while this view was hidden; realign the
        # visible playhead/timecode before the preview refresh below.
        refresh_playhead = getattr(
            self._timeline_panel, "refresh_playhead_from_controller", None
        )
        if callable(refresh_playhead):
            refresh_playhead()

        # Sync audio tracks from current timeline
        self._sync_audio_tracks_from_timeline()

        # Setup audio-video synchronization
        if self._audio_active:
            self._setup_audio_video_sync()

        # Trigger initial frame request
        if self._program_viewer and self._current_frame >= 0:
            self._request_frame_at_playhead()

    def on_leave(self) -> None:
        """
        Called when leaving Edit page.

        Cleanup:
        - Stop playback
        - Stop audio manager
        - Clear pending requests
        """
        self._is_active = False
        logger.info("[EditPage] Leaving Edit page")

        # A hidden Edit page must not keep its playback clock, decode workers and
        # preview renderer alive while Color owns the shared GPU.  Leaving this
        # running made an Edit -> Color transition create two independent video
        # playback workloads for the same application window.
        if self._timeline_panel is not None:
            try:
                controller = self._timeline_panel.get_playback_controller()
                if controller is not None and controller.is_playing:
                    self._timeline_panel.pause_playback()
                    self.signal_hub.playback_stopped.emit()
            except (AttributeError, RuntimeError) as exc:
                logger.warning("[EditPage] Failed to pause playback on leave: %s", exc)

        # Stop and release both playback and scrub audio workers.  The audio
        # manager is recreated lazily/re-synced on the next Edit-page entry.
        self.shutdown_audio_playback()

    def is_active(self) -> bool:
        """Return whether the Edit page is currently active."""
        return self._is_active

    def _preview_page_is_active(self) -> bool:
        """Return whether Edit owns the visible timeline preview.

        ``_is_active`` is maintained by the page lifecycle hooks.  During
        startup/project restoration those hooks can briefly lag behind the
        window's canonical ``current_page`` value.  In that window the
        playback clock still advances, but the frame-request signal would be
        incorrectly suppressed, producing audio with a frozen picture.

        Keep the lifecycle flag as the fast path, and use the main window's
        page as a narrowly scoped fallback.  A hidden Edit page still cannot
        submit preview frames while Color or Composite is active.
        """
        if self._is_active:
            return True
        main_window = getattr(self, "_main_window", None)
        return getattr(main_window, "current_page", None) == "Edit"

    def _preview_refresh_allowed(self) -> bool:
        """Gate refresh-style frame requests (AUD-19901).

        ``set_timeline`` / playback-start refreshes must not wake the hidden
        Edit preview renderer while another page (Color/Composite) owns
        playback — the hidden renderer would decode the same content a
        second time and its backpressure slows the shared clock.  Unlike
        per-tick playhead requests, a page WITHOUT a multi-page host
        (standalone embedding / unit tests) has nothing that could hide it,
        so its refresh requests always go through.
        """
        if self._preview_page_is_active():
            return True
        return getattr(self, "_main_window", None) is None

    def get_signal_hub(self) -> EditSignalHub:
        """Return the signal hub for panel coordination."""
        return self.signal_hub

    # ==================== Playhead & Frame Logic ====================

    def _on_playhead_changed(self, frame: int) -> None:
        """
        Handle playhead position change from TimelinePanel.

        Triggers:
        1. Timecode update in ProgramViewer
        2. Emit timeline_frame_requested for TimelinePreviewRenderer

        Args:
            frame: New frame number
        """
        # Deduplicate: skip if same frame as last emitted
        if (
            not self._scrub_active
            and hasattr(self, '_last_requested_frame')
            and self._last_requested_frame == frame
        ):
            playback_trace("edit_page.playhead_changed_dedup", frame=frame)
            return
        self._last_requested_frame = frame

        self._current_frame = frame
        playback_trace(
            "edit_page.playhead_changed",
            frame=frame,
            fps=self._fps,
            has_program_viewer=self._program_viewer is not None,
            scrub_active=self._scrub_active,
        )

        # Update timecode in ProgramViewer
        if self._program_viewer:
            timecode = self._frame_to_timecode(frame)
            self._program_viewer.set_timecode(timecode, self._fps)

        # P10.3: 推送当前帧激活的字幕片段给 ProgramViewer，触发 overlay 重绘。
        self._update_subtitle_overlays(frame)

        if self._scrub_active:
            self._emit_scrub_frame_request(frame, source="playhead_changed")
            self.signal_hub.playhead_changed.emit(frame)
            return

        # Playback ticks and manual scrubbing share the same timeline-frame
        # render request path. No clip/source-frame request is emitted here.
        # A hidden Edit page must NOT drive its own preview renderer: while
        # Composite/Color owns playback, the hidden renderer would decode and
        # render every frame a second time, and its render backpressure
        # (should_defer_playback_tick) would slow down the shared playback
        # clock — the visible stutter on the Composite page. The page
        # re-renders the current frame on re-entry via on_enter().
        if self._preview_page_is_active():
            playback_trace("edit_page.timeline_frame_requested_emit", frame=frame)
            self.timeline_frame_requested.emit(frame)

        # Emit signal for other components
        self.signal_hub.playhead_changed.emit(frame)

    def _update_subtitle_overlays(self, frame: int) -> None:
        """P10.3: 把当前帧激活的字幕片段推送给 ProgramViewer 显示。

        - 查询 timeline.get_active_subtitle_clips(frame)
        - 同步字幕画布尺寸（用 timeline.width/height）
        - 解析每个 cue 的分层样式（track-default ⊕ cue-override），作为
          (clip, style) 对下发给 viewer，使 caption 样式继承真正生效
          （SUB-P2-01/06）
        - 触发 ProgramViewer 重绘 overlay
        """
        if self._program_viewer is None:
            return
        timeline = self._current_timeline
        if timeline is None:
            self._program_viewer.set_subtitle_overlays([])
            return
        try:
            subs = timeline.get_active_subtitle_clips(frame)
        except Exception:
            subs = ()
        # 同步画布尺寸（字幕渲染基准）
        try:
            w = int(getattr(timeline, 'width', 1920) or 1920)
            h = int(getattr(timeline, 'height', 1080) or 1080)
            self._program_viewer.set_subtitle_canvas_size(w, h)
        except Exception:
            pass
        # 解析分层样式 → (clip, style) 对
        pairs = []
        for clip in subs:
            pair = self._resolve_subtitle_overlay_pair(timeline, clip)
            graphic = self._subtitle_graphic_overlay_metadata(timeline, clip, frame)
            pairs.append((*pair, graphic) if graphic is not None else pair)
        self._program_viewer.set_subtitle_overlays(pairs)

    @staticmethod
    def _subtitle_graphic_overlay_metadata(timeline, clip, frame: int) -> dict[str, Any] | None:
        """Describe a lazy/both-mode graphic effect for the native overlay."""
        try:
            from src.features.edit.core.captions.graphics import template_parameters
            binding = timeline.get_caption_graphic_binding(clip.clip_id)
            if binding is not None:
                if clip.display_mode != "both":
                    return None
                template = timeline.get_caption_graphic_template_registry().get(
                    binding.template_id, binding.template_version
                )
                if template is None:
                    return None
                return {
                    "effect": template.effect,
                    "parameters": template_parameters(template, binding.parameters),
                    "local_frame": int(frame - clip.timeline_start),
                }
            track_style = next(
                (item for item in timeline.caption_track_graphic_styles
                 if item.track_id == clip.track_id and item.enabled), None
            )
            if track_style is None:
                return None
            template = timeline.get_caption_graphic_template_registry().get(
                track_style.template_id, track_style.template_version
            )
            if template is None:
                return None
            return {
                "effect": template.effect,
                "parameters": template_parameters(template, track_style.parameters),
                "local_frame": int(frame - clip.timeline_start),
            }
        except Exception:
            return None

    @staticmethod
    def _resolve_subtitle_overlay_pair(timeline, clip):
        """Resolve a SubtitleClip's layered style for preview rendering.

        Returns ``(clip, style_dict)`` where ``style_dict`` flattens
        track-default ⊕ cue-override (see captions.model.resolve_style). On
        any error falls back to ``(clip, None)`` (legacy flat-field path).
        """
        try:
            from src.features.edit.core.captions.model import (
                resolve_style, EMPTY_STYLE, clip_style_base,
            )
            track = None
            getter = getattr(timeline, "get_track_by_id", None)
            if callable(getter):
                track = getter(clip.track_id)
            track_default = (
                track.caption_metadata.default_style
                if track is not None and getattr(track, "caption_metadata", None) is not None
                and getattr(track.caption_metadata, "default_style", None) is not None
                else EMPTY_STYLE
            )
            style = resolve_style(track_default, getattr(clip, "style_override", None),
                                  base=clip_style_base(clip))
            return (clip, style)
        except Exception:
            return (clip, None)

    def _refresh_subtitle_overlay_after_commit(self) -> None:
        """Refresh subtitle overlay + invalidate glyph cache after a commit.

        Called from :meth:`set_timeline` so that subtitle edits committed
        through the unified timeline path are reflected immediately in the
        ProgramViewer, even when the playhead has not moved. Mirrors the
        behaviour the legacy subtitle bypass performed manually.
        """
        if self._program_viewer is None or self._current_timeline is None:
            return
        try:
            from src.features.edit.core.subtitle_renderer import invalidate_cache
            invalidate_cache()
        except Exception:
            pass
        try:
            frame = int(getattr(self, '_current_frame', 0) or 0)
            self._update_subtitle_overlays(frame)
        except Exception:
            logger.debug("Failed to refresh subtitle overlay after commit", exc_info=True)

    def _on_seek_requested(self, frame: int) -> None:
        """
        Handle seek request from TimelinePanel.

        Args:
            frame: Target frame number
        """
        self._current_frame = frame

        playback_trace("edit_page.seek_requested", frame=frame, audio_active=self._audio_active)
        if self._scrub_active:
            playback_trace("edit_page.seek_requested_deferred_during_scrub", frame=frame)
            return

        # TimelineView.playhead_changed is connected to both TimelinePanel and
        # EditPage. During a drag, EditPage._on_playhead_changed() has usually
        # already requested this exact frame before TimelinePanel emits
        # seek_requested. Keep audio seek behavior, but avoid doubling preview
        # render requests for the same scrub frame.
        if getattr(self, '_last_requested_frame', None) == frame:
            playback_trace("edit_page.seek_requested_preview_dedup", frame=frame)
        else:
            self._last_requested_frame = frame
            self.timeline_frame_requested.emit(frame)

        self._prepare_or_seek_audio(frame)

    def _on_scrub_started(self, frame: int) -> None:
        """Enter lightweight timeline scrub preview mode."""
        # Scrubbing is an interactive preview operation. Treat it with the
        # same priority as playback: waveform decoding and thumbnail commits
        # must not compete with the real-time playhead preview.
        self._background_media_resume_token += 1
        self._set_thumbnail_playback(True)
        self._scrub_active = True
        self._last_scrub_requested_frame = -1
        self._pending_scrub_request_frame = None
        self._scrub_request_timer_pending = False
        self._scrub_request_timer_token += 1
        self._scrub_last_emit_at = 0.0
        self._scrub_audio_last_emit_at = 0.0
        self._current_frame = frame
        playback_trace("edit_page.scrub_started", frame=frame)
        self.timeline_scrub_started.emit(frame)
        self._emit_scrub_frame_request(frame, source="scrub_started")
        self._request_scrub_audio_preview(frame, force=True)

    def _emit_scrub_frame_request_now(self, frame: int, *, source: str) -> None:
        """Emit one scrub preview request immediately after per-frame dedupe."""
        frame = int(frame)
        if self._last_scrub_requested_frame == frame:
            playback_trace(
                "edit_page.timeline_scrub_frame_requested_dedup",
                frame=frame,
                source=source,
            )
            return

        self._last_scrub_requested_frame = frame
        self._scrub_last_emit_at = time.monotonic()
        playback_trace(
            "edit_page.timeline_scrub_frame_requested_emit",
            frame=frame,
            source=source,
        )
        self.timeline_scrub_frame_requested.emit(frame)

    def _emit_scrub_frame_request(self, frame: int, *, source: str) -> None:
        """Coalesce high-frequency scrub signals into latest-only preview requests."""
        frame = int(frame)
        if (
            self._last_scrub_requested_frame == frame
            or self._pending_scrub_request_frame == frame
        ):
            playback_trace(
                "edit_page.timeline_scrub_frame_requested_dedup",
                frame=frame,
                source=source,
            )
            return

        elapsed_ms = (time.monotonic() - self._scrub_last_emit_at) * 1000.0
        if self._scrub_last_emit_at <= 0.0 or elapsed_ms >= self._scrub_request_interval_ms:
            self._pending_scrub_request_frame = None
            self._emit_scrub_frame_request_now(frame, source=source)
            return

        self._pending_scrub_request_frame = frame
        if self._scrub_request_timer_pending:
            playback_trace(
                "edit_page.timeline_scrub_frame_requested_coalesced",
                frame=frame,
                source=source,
            )
            return

        delay_ms = max(1, int(self._scrub_request_interval_ms - elapsed_ms))
        self._scrub_request_timer_pending = True
        self._scrub_request_timer_token += 1
        token = self._scrub_request_timer_token
        playback_trace(
            "edit_page.timeline_scrub_frame_requested_schedule",
            frame=frame,
            source=source,
            delay_ms=delay_ms,
        )
        QTimer.singleShot(delay_ms, lambda token=token: self._flush_pending_scrub_frame(token))

    def _flush_pending_scrub_frame(self, token: int) -> None:
        """Emit the latest coalesced scrub frame from the UI thread."""
        self._scrub_request_timer_pending = False
        if token != self._scrub_request_timer_token or not self._scrub_active:
            return
        frame = self._pending_scrub_request_frame
        self._pending_scrub_request_frame = None
        if frame is None:
            return
        self._emit_scrub_frame_request_now(int(frame), source="scrub_coalesced")

    def _on_scrub_moved(self, frame: int) -> None:
        """
        Drive ProgramViewer preview directly from TimelineView scrub movement.

        playhead_changed also fires for most drags, but it is shared with
        playback/controller synchronization. Scrub movement is the user's
        explicit preview gesture, so it must have its own direct frame request
        path and only use the shared playhead signal as a secondary fallback.
        """
        frame = int(frame)
        if not self._scrub_active:
            self._on_scrub_started(frame)

        self._current_frame = frame
        if self._program_viewer:
            timecode = self._frame_to_timecode(frame)
            self._program_viewer.set_timecode(timecode, self._fps)

        self._emit_scrub_frame_request(frame, source="scrub_moved")
        self._request_scrub_audio_preview(frame)
        self.signal_hub.playhead_changed.emit(frame)

    def _on_scrub_finished(self, frame: int) -> None:
        """Leave scrub mode and request one exact frame for the release point."""
        self._scrub_active = False
        self._last_scrub_requested_frame = -1
        self._pending_scrub_request_frame = None
        self._scrub_request_timer_pending = False
        self._scrub_request_timer_token += 1
        self._scrub_last_emit_at = 0.0
        self._scrub_audio_last_emit_at = 0.0
        self._last_requested_frame = frame
        self._current_frame = frame
        playback_trace("edit_page.scrub_finished", frame=frame, audio_active=self._audio_active)

        if self._program_viewer:
            timecode = self._frame_to_timecode(frame)
            self._program_viewer.set_timecode(timecode, self._fps)

        self.timeline_scrub_finished.emit(frame)

        self._prepare_or_seek_audio(frame)
        self._schedule_background_media_resume()

    def request_current_timeline_frame(self) -> None:
        """Request the current timeline frame through the single preview path."""
        frame = int(self._current_frame)
        # AUD-19901: 与 _on_playhead_changed 相同的门控。隐藏的 Edit 页
        # （如 Color 页播放另一时间线触发 start_timeline_audio_playback →
        # set_timeline → 此处）不得唤醒自己的预览渲染器：隐藏渲染器会对
        # 同一内容二次解码，其渲染背压会拖慢共享播放钟。on_enter 的重渲
        # 逻辑覆盖重新激活场景。
        if not self._preview_refresh_allowed():
            playback_trace(
                "edit_page.request_current_timeline_frame_skipped_hidden",
                frame=frame,
            )
            return
        playback_trace("edit_page.request_current_timeline_frame", frame=frame)
        self.timeline_frame_requested.emit(frame)

    def _request_frame_at_playhead(self) -> None:
        """Compatibility wrapper for older callers; uses timeline-frame path."""
        self.request_current_timeline_frame()

    def _emit_frame_request(self) -> None:
        """Compatibility wrapper for older controllers; uses timeline-frame path."""
        self.request_current_timeline_frame()

    # ==================== Legacy Frame Rendering API ====================

    def on_frame_ready(self, tex_id: int, width: int, height: int) -> None:
        """
        Handle frame ready from renderer (to be connected to VideoAsyncRenderer).

        Args:
            tex_id: OpenGL texture ID
            width: Texture width
            height: Texture height
        """
        playback_trace(
            "edit_page.legacy_frame_ready_ignored",
            tex_id=tex_id,
            width=width,
            height=height,
        )

    # ==================== Timecode Utilities ====================

    def _frame_to_timecode(self, frame: int) -> str:
        """
        Convert frame number to timecode string (HH:MM:SS:FF).

        Args:
            frame: Frame number

        Returns:
            Timecode string
        """
        # FE-B13：统一走 interchange.frames_to_timecode（Rational SMPTE）
        from src.features.edit.core.interchange import frames_to_timecode

        return frames_to_timecode(int(frame), float(self._fps))

    def set_fps(self, fps: float) -> None:
        """
        Set timeline frame rate.

        Args:
            fps: Frame rate (e.g., 24.0, 25.0, 30.0, 60.0)
        """
        self._fps = fps

        # Update timecode display
        if self._program_viewer:
            timecode = self._frame_to_timecode(self._current_frame)
            self._program_viewer.set_timecode(timecode, fps)
            controller = self._timeline_panel.get_playback_controller() if self._timeline_panel else None
            self._program_viewer.set_playback_state(
                bool(getattr(controller, "is_playing", False)), fps)

    # ==================== Timeline Management ====================

    def set_timeline(self, timeline: Any) -> None:
        """
        Set current timeline data model.

        Args:
            timeline: TimelineSequence instance
        """
        previous_preview_signature = self._timeline_preview_signature
        previous_audio_signature = self._audio_timeline_signature
        preview_signature = self._build_timeline_preview_signature(timeline)

        self._current_timeline = timeline
        self.timeline_display_changed.emit(timeline)

        # An empty timeline has no frame that can replace the previous
        # Program image. Clear it immediately instead of letting the viewer
        # hold the last rendered frame while the new timeline settles.
        if not self._timeline_has_program_content(timeline):
            clear_texture = getattr(self._program_viewer, "clear_texture", None)
            if callable(clear_texture):
                clear_texture()

        self._sync_program_timeline_selector()
        self._sync_program_guide_from_timeline()

        if timeline and hasattr(timeline, 'fps'):
            self._fps = float(timeline.fps)

        # Sync audio tracks from new timeline
        self._sync_audio_tracks_from_timeline()

        # Reconfigure audio-video sync if audio is active
        if self._audio_active:
            self._setup_audio_video_sync()

        self._timeline_preview_signature = preview_signature
        preview_changed = previous_preview_signature != preview_signature
        audio_changed = previous_audio_signature != self._audio_timeline_signature
        if previous_preview_signature is None or preview_changed or audio_changed:
            self._request_frame_at_playhead()
        else:
            playback_trace(
                "edit_page.set_timeline_preview_skipped",
                frame=int(self._current_frame),
                audio_changed=audio_changed,
            )

        # SUB-P0-01: now that subtitle commits flow through this unified
        # path, refresh the ProgramViewer subtitle overlay whenever the
        # timeline model changes. Subtitle content/style edits are not a
        # playhead change, so without this the overlay text would lag the
        # model until the next seek. Cache invalidation keeps stale glyph
        # textures from being reused after a text/style edit.
        self._refresh_subtitle_overlay_after_commit()

    @staticmethod
    def _timeline_has_program_content(timeline: Any) -> bool:
        """Return whether a timeline contains an enabled video clip."""
        if timeline is None:
            return False
        for track in getattr(timeline, "video_tracks", ()) or ():
            if not bool(getattr(track, "is_enabled", True)):
                continue
            if not bool(getattr(track, "is_visible", True)):
                continue
            for clip in getattr(track, "clips", ()) or ():
                if not bool(getattr(clip, "is_enabled", True)):
                    continue
                if int(getattr(clip, "timeline_duration", 0) or 0) > 0:
                    return True
        return False

    def _sync_program_timeline_selector(self, *_args: Any) -> None:
        """Refresh the Program monitor's active-timeline selector."""
        if self._program_viewer is None:
            return

        project = None
        if self._main_window is not None:
            project = getattr(self._main_window, "_current_edit_project", None)
        if project is None and self._media_pool_panel is not None:
            get_project = getattr(self._media_pool_panel, "get_project", None)
            if callable(get_project):
                project = get_project()

        timelines = getattr(project, "timelines", {}) if project is not None else {}
        active_id = getattr(project, "active_timeline_id", None)
        if active_id is None and self._current_timeline is not None:
            active_id = getattr(self._current_timeline, "timeline_id", None)
        set_choices = getattr(self._program_viewer, "set_timeline_choices", None)
        if callable(set_choices):
            set_choices(timelines, active_id)

    def _sync_program_guide_from_timeline(self) -> None:
        """Restore the persisted Program monitor guide for the active timeline."""
        if self._program_viewer is None:
            return
        settings = dict(getattr(self._current_timeline, "format_settings", {}) or {})
        guide = settings.get("program_guide")
        if not isinstance(guide, dict):
            self._program_viewer.set_guide("aspect_ratio", "default", {})
            return
        self._program_viewer.set_guide(
            str(guide.get("type", "aspect_ratio")),
            str(guide.get("id", "default")),
            dict(guide.get("params", {}) or {}),
        )

    # F1.3/R8：create_timeline 死方法已移除（发射 timeline_created 信号全库
    # 无消费者且恒返回 ""）。创建时间线的生产入口：媒体池右键 New Timeline
    # → timeline_create_requested → edit_controller._on_timeline_create_requested。

    def switch_timeline(self, timeline_id: str) -> None:
        """
        Switch to a different timeline.

        Args:
            timeline_id: The timeline to switch to
        """
        logger.debug(f"[EditPage] switch_timeline() called (id={timeline_id})")
        self.signal_hub.timeline_switched.emit(timeline_id)

    # ==================== Playback Controls ====================

    def _set_thumbnail_playback(self, active: bool) -> None:
        """Give playback priority over thumbnail and waveform background work."""
        if self._timeline_panel:
            tl_view = self._timeline_panel.get_timeline_view()
            if tl_view and hasattr(tl_view, 'set_playback_active_for_thumbnails'):
                tl_view.set_playback_active_for_thumbnails(active)
        # Waveform decoding uses PyAV and can otherwise contend with the video
        # decoder.  Queue new requests until playback stops; already cached
        # waveforms continue rendering without any decode work.
        try:
            from src.features.edit.core.audio_waveform import (
                WaveformExtractor, ClipEffectWaveformRenderer,
            )
            WaveformExtractor.instance().set_playback_active(active)
            ClipEffectWaveformRenderer.instance().set_playback_active(active)
        except Exception:
            logger.debug("Failed to update waveform playback priority", exc_info=True)

    def _schedule_background_media_resume(self) -> None:
        """Resume background media work only after the scrub gesture settles."""
        self._background_media_resume_token += 1
        token = self._background_media_resume_token

        def resume_if_idle() -> None:
            if token != self._background_media_resume_token or self._scrub_active:
                return
            if self._timeline_panel is not None:
                try:
                    if self._timeline_panel.get_playback_controller().is_playing:
                        return
                except Exception:
                    pass
            self._set_thumbnail_playback(False)

        # Avoid restarting PyAV/OpenCV repeatedly when the user makes a series
        # of short playhead drags.
        QTimer.singleShot(250, resume_if_idle)

    def start_playback(self) -> None:
        """Start timeline playback."""
        logger.debug("[EditPage] start_playback() called")
        self.signal_hub.playback_started.emit()

        # Start audio playback if audio is active
        if self._audio_active and self._audio_manager is not None:
            self._audio_manager.play(start_frame=self._current_frame)

    def stop_playback(self) -> None:
        """Stop timeline playback."""
        logger.debug("[EditPage] stop_playback() called")
        self.signal_hub.playback_stopped.emit()

        # Stop audio playback
        if self._audio_manager is not None:
            self._audio_manager.stop()

    def seek_to_frame(self, frame: int) -> None:
        """Seek to a specific frame in the timeline."""
        logger.debug(f"[EditPage] seek_to_frame() called (frame={frame})")
        self.signal_hub.seek_requested.emit(frame)

        self._prepare_or_seek_audio(frame)

    # ==================== P3.2: Insert/Overwrite/Append Operations ====================

    def _on_source_patch_requested(self) -> None:
        """F2.1：打开当前源素材的 Source Patch 设置对话框并提交命令。

        映射/流开关经 UpdateSourcePatchCommand 落工程（单步 Undo、随工程
        保存），非 UI 临时变量；等价默认设置提交为清除覆盖。提交成功后
        同步源监视器 scrub 试听的声道映射。
        """
        from src.features.edit.core.source_patch import DEFAULT_SOURCE_PATCH
        from src.features.edit.ui.source_patch_dialog import SourcePatchDialog

        viewer = self._source_viewer
        asset = getattr(viewer, "current_asset", None) if viewer else None
        if asset is None:
            return
        project = None
        if self._main_window is not None:
            project = getattr(self._main_window, "_current_edit_project", None)
        if project is None and self._media_pool_panel is not None:
            get_project = getattr(self._media_pool_panel, "get_project", None)
            if callable(get_project):
                project = get_project()
        current = (
            patch_for_asset(project.source_patch_overrides, asset.asset_id)
            if project is not None else DEFAULT_SOURCE_PATCH
        )
        n_channels = getattr(asset, "audio_channels", None) or 0
        spec = SourcePatchDialog.run(
            int(n_channels), current,
            self._source_viewer.window() if viewer else None)
        if spec is None:
            return
        pool = self._media_pool_panel
        if pool is None or not callable(getattr(pool, "apply_source_patch", None)):
            logger.warning("Source patch: media pool unavailable, not applied")
            return
        # 等价默认 = 清除覆盖（避免存无意义的默认覆盖条目）
        commit_spec = spec if spec != DEFAULT_SOURCE_PATCH else None
        if pool.apply_source_patch(asset.asset_id, commit_spec) and viewer is not None:
            viewer.set_source_channel_map(spec.audio_channel_map)

    def _on_insert_requested(self, asset_id: str, in_frame: int, out_frame: int) -> None:
        """
        Handle insert requested from SourceViewer.

        Args:
            asset_id: Asset ID to insert
            in_frame: Source start frame
            out_frame: Source end frame (inclusive)
        """
        logger.debug(
            f"[EditPage] _on_insert_requested: asset={asset_id}, "
            f"in={in_frame}, out={out_frame}"
        )

        if self._timeline_panel is None:
            logger.warning("Cannot insert: no timeline panel")
            return

        # Get current playhead
        playhead = self._timeline_panel.get_playhead_frame()

        # Execute insert operation
        success = self._timeline_panel.insert_clip(
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            playhead=playhead
        )

        if success:
            logger.info(f"Insert successful: asset={asset_id} at playhead={playhead}")
        else:
            logger.warning(f"Insert failed: asset={asset_id}")

    def _on_overwrite_requested(self, asset_id: str, in_frame: int, out_frame: int) -> None:
        """
        Handle overwrite requested from SourceViewer.

        Args:
            asset_id: Asset ID to overwrite
            in_frame: Source start frame
            out_frame: Source end frame (inclusive)
        """
        logger.debug(
            f"[EditPage] _on_overwrite_requested: asset={asset_id}, "
            f"in={in_frame}, out={out_frame}"
        )

        if self._timeline_panel is None:
            logger.warning("Cannot overwrite: no timeline panel")
            return

        # Get current playhead
        playhead = self._timeline_panel.get_playhead_frame()

        # Execute overwrite operation
        success = self._timeline_panel.overwrite_clip(
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            playhead=playhead
        )

        if success:
            logger.info(f"Overwrite successful: asset={asset_id} at playhead={playhead}")
        else:
            logger.warning(f"Overwrite failed: asset={asset_id}")

    def _on_append_requested(self, asset_id: str, in_frame: int, out_frame: int) -> None:
        """
        Handle append requested from SourceViewer.

        Args:
            asset_id: Asset ID to append
            in_frame: Source start frame
            out_frame: Source end frame (inclusive)
        """
        logger.debug(
            f"[EditPage] _on_append_requested: asset={asset_id}, "
            f"in={in_frame}, out={out_frame}"
        )

        if self._timeline_panel is None:
            logger.warning("Cannot append: no timeline panel")
            return

        # Execute append operation
        success = self._timeline_panel.append_clip(
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame
        )

        if success:
            logger.info(f"Append successful: asset={asset_id}")
        else:
            logger.warning(f"Append failed: asset={asset_id}")

    # ==================== Timeline Toolbar → Source Edit Bridge ====================

    def _on_timeline_source_insert(self) -> None:
        """Bridge: TimelinePanel Insert button → read SourceViewer data → insert."""
        asset_id, in_frame, out_frame = self._get_source_edit_params()
        if asset_id is not None:
            self._on_insert_requested(asset_id, in_frame, out_frame)

    def _on_timeline_source_overwrite(self) -> None:
        """Bridge: TimelinePanel Overwrite button → read SourceViewer data → overwrite."""
        asset_id, in_frame, out_frame = self._get_source_edit_params()
        if asset_id is not None:
            self._on_overwrite_requested(asset_id, in_frame, out_frame)

    def _on_timeline_source_append(self) -> None:
        """Bridge: TimelinePanel Append button → read SourceViewer data → append."""
        asset_id, in_frame, out_frame = self._get_source_edit_params()
        if asset_id is not None:
            self._on_append_requested(asset_id, in_frame, out_frame)

    def _get_source_edit_params(self) -> tuple:
        """Read current source asset ID and In/Out frames from SourceViewerWidget.

        Returns:
            (asset_id, in_frame, out_frame) or (None, None, None) if unavailable.
        """
        if self._source_viewer is None:
            logger.warning("Source edit: no source viewer")
            return (None, None, None)
        asset = self._source_viewer.current_asset
        if asset is None:
            logger.warning("Source edit: no source asset loaded")
            return (None, None, None)
        in_point = self._source_viewer.in_point
        out_point = self._source_viewer.out_point
        # FE-B05：Out 为 inclusive，I=O 允许单帧插入
        if in_point is None or out_point is None or in_point > out_point:
            logger.warning("Source edit: invalid In/Out points")
            return (None, None, None)
        return (asset.asset_id, in_point, out_point)

    def _sync_source_edit_buttons(self) -> None:
        """Sync TimelinePanel source-edit button enabled state with SourceViewer In/Out.

        W02: additionally requires at least one routable source stream —
        when both the video and the audio source destinations are explicitly
        disabled, insert/overwrite/append/replace have no stream to place.
        """
        if self._timeline_panel is None or self._source_viewer is None:
            return
        can_edit = (
            self._source_viewer.in_point is not None
            and self._source_viewer.out_point is not None
            and self._source_viewer.in_point <= self._source_viewer.out_point
        )
        if can_edit:
            view = self._timeline_panel.get_timeline_view()
            if view is not None:
                both_disabled = (
                    view.source_destination_stream_disabled("video")
                    and view.source_destination_stream_disabled("audio")
                )
                if both_disabled:
                    can_edit = False
        self._timeline_panel.set_source_edit_buttons_enabled(can_edit)

    # ==================== P9.2: Edit → Composite Page Linkage ====================

    def _on_open_in_composite_requested(self, clip_id: str, track_id: str) -> None:
        """
        Handle "Open in Composite" request from clip context menu (P9.2).

        Creates a new composite for the clip and emits signal to switch pages.

        Args:
            clip_id: Clip ID that was right-clicked
            track_id: Track ID containing the clip
        """

        logger.info(
            f"[EditPage P9.2] Open in Composite requested: "
            f"clip={clip_id}, track={track_id}"
        )

        # Emit signal for MainWindow to handle page switch
        # MainWindow will create the composite and switch to Composite page
        self.signal_hub.open_in_composite_requested.emit(clip_id, track_id)

    # ==================== T7: Edit → Color Page Linkage ====================

    def _on_open_in_color_requested(self, clip_id: str, track_id: str, stack_id: str) -> None:
        """
        Handle "Open in Color" request from clip context menu (T7).

        Forwards the request through the signal hub so that MainWindow
        can call open_timeline_clip_in_color with the active timeline.

        Args:
            clip_id: Clip ID that was right-clicked
            track_id: Track ID containing the clip
            stack_id: Color stack ID (may be empty string)
        """
        logger.info(
            f"[EditPage T7] Open in Color requested: "
            f"clip={clip_id}, track={track_id}, stack={stack_id}"
        )

        # Emit signal for MainWindow to handle page switch
        self.signal_hub.open_in_color_requested.emit(clip_id, track_id, stack_id)

    # ==================== E09: Clip double-click → Source Viewer ====================

    def _on_clip_open_in_source(self, clip_id: str, source_frame: int) -> None:
        """E09: 双击时间线片段 → 在 Source Viewer 打开对应素材与源帧。

        Motion Graphic 是场景虚拟素材，Source Viewer 无法解码 topos://
        引用，保持静默跳过（与媒体池双击路径一致）。
        """
        if self._source_viewer is None:
            logger.warning("Clip double-click: no source viewer")
            return
        asset_id = None
        timeline = self._current_timeline
        if timeline is not None:
            clip = timeline.get_clip_by_id(clip_id)
            if clip is not None:
                asset_id = clip.asset_id
        if not asset_id:
            logger.warning("Clip double-click: clip not found: %s", clip_id)
            return
        project = None
        if self._main_window is not None:
            project = getattr(self._main_window, "_current_edit_project", None)
        if project is None and self._media_pool_panel is not None:
            get_project = getattr(self._media_pool_panel, "get_project", None)
            if callable(get_project):
                project = get_project()
        asset = project.get_asset_by_id(asset_id) if project is not None else None
        if asset is None:
            logger.warning("Clip double-click: asset not found: %s", asset_id)
            return
        if getattr(asset, "is_motion_graphic", False):
            logger.debug("Clip double-click: motion graphic skipped: %s", asset_id)
            return
        try:
            _audio_path = None
            _audio_offset = 0.0
            if project is not None:
                from src.features.edit.core.audio_sync import (
                    resolve_audio_source,
                )
                _audio_path, _audio_offset = resolve_audio_source(
                    project, asset)
                # F4.1 顺手修复（与 F3.4 source_patch_overrides 同类）：
                # duck-typed 资产（测试替身/旧对象）无 .path 时直接
                # AttributeError 被外层吞掉——双击打开整体失效。
                if _audio_path == getattr(asset, "path", None):
                    _audio_path = None  # 未绑定/保留机内声
            self._source_viewer.open_asset(
                asset,
                # F4.1 顺手修复：duck-typed 工程无 source_patch_overrides
                # 时回退无映射（与成片导出 getattr 防御同口径）
                source_channel_map=patch_for_asset(
                    getattr(project, "source_patch_overrides", None) or {},
                    asset.asset_id,
                ).audio_channel_map if project is not None else None,
                audio_source_path=_audio_path,
                audio_offset_seconds=_audio_offset,
            )
            self._source_viewer.seek(max(0, int(source_frame)))
            viewport = getattr(self._main_window, "edit_viewport", None)
            if viewport is not None:
                viewport.show_source(focus=True)
        except Exception:  # noqa: BLE001 打开失败不阻断时间线交互
            logger.exception("Clip double-click: failed to open asset %s", asset_id)

    # ==================== P9: Scene Cut Detection ====================

    def _ensure_scene_cut_controller(self) -> Any:
        """
        Lazily initialize and return the SceneCutController.

        Creates the controller with project getter/setter bindings
        from the media pool panel.

        Returns:
            SceneCutController instance, or None if initialization fails.
        """
        if self._scene_cut_controller is not None:
            # E07: 偏好页保存后刷新下一次检测的默认值（读取是纯 dict 读，
            # 每次检测入口调用的开销可忽略）。
            self._apply_scene_cut_preferences(self._scene_cut_controller)
            return self._scene_cut_controller

        try:
            from src.features.edit.core.scene_cut_controller import (
                create_scene_cut_controller,
            )
            self._scene_cut_controller = create_scene_cut_controller(
                parent=self,
                edit_page=self,
            )
            # T1.3：把场景检测结果接入共享 AnalysisService，使 AI 规划器能拿到
            # SHOT 证据。SceneCutAnalysis 自带 asset_id；经
            # AnalysisService.ingest_scene_cut_analysis 转成 SHOT segment 灌入仓库。
            self._connect_scene_cut_to_analysis(self._scene_cut_controller)
            # B1: detection results feed the timeline overlay; the dialog's
            # "Show on Timeline" toggle shares the View-menu switch.
            self._scene_cut_controller.detection_completed.connect(
                self._on_scene_cut_analysis_for_overlay
            )
            self._scene_cut_controller.timeline_overlay_requested.connect(
                self.set_scene_cut_overlay_visible
            )
            # E07: 偏好页保存的 sensitivity/use_proxy/min_scene_frames 此前
            # 无生产读取点（控制器用硬编码 0.55/True/8）。创建控制器后注入
            # 偏好默认值，使检测参数与偏好页一致。
            self._apply_scene_cut_preferences(self._scene_cut_controller)
            logger.debug("SceneCutController initialized for EditPage")
            return self._scene_cut_controller
        except ImportError as e:
            logger.warning(f"SceneCutController unavailable: {e}")
            return None

    def _apply_scene_cut_preferences(self, controller: Any) -> None:
        """E07: 把偏好配置注入场景检测控制器的默认设置（best effort）。"""
        try:
            from src.features.edit.core.scene_cut.models import SceneCutSettings
            from src.features.edit.preferences import (
                get_scene_cut_settings_from_config,
            )
            from src.shared.config.config_manager import ConfigManager

            settings = get_scene_cut_settings_from_config(
                dict(getattr(ConfigManager(), "config", {}) or {})
            )
            controller.set_default_settings(SceneCutSettings(**settings))
        except Exception:  # noqa: BLE001 偏好读取失败不阻断检测
            logger.debug("apply scene cut preferences failed", exc_info=True)

    # ------------------------------------------------------------------
    # B1: Scene cut timeline overlay switch (single source of truth)
    # ------------------------------------------------------------------

    _SCENE_CUT_OVERLAY_CONFIG_KEY = "edit_scene_cut_overlay_visible"

    def set_scene_cut_overlay_visible(self, visible: bool) -> None:
        """Show/hide the scene cut candidate overlay and persist the choice.

        Single entry point shared by the checkable View menu action and the
        SceneCutDialog "Show on Timeline" toggle.

        Args:
            visible: True to show candidate markers on the timeline.
        """
        visible = bool(visible)
        if visible == self._scene_cut_overlay_visible:
            return
        self._scene_cut_overlay_visible = visible
        self._persist_scene_cut_overlay_state()
        timeline_view = self._get_scene_cut_timeline_view()
        if timeline_view is not None:
            timeline_view.set_scene_cut_overlay_visible(visible)
        # Keep the dialog's toggle in sync when the change came from the
        # View menu (blockSignals on its side prevents signal loops).
        get_dialog = getattr(self._scene_cut_controller, "get_active_dialog", None)
        if callable(get_dialog):
            dialog = get_dialog()
            if dialog is not None and hasattr(dialog, "set_show_on_timeline_checked"):
                dialog.set_show_on_timeline_checked(visible)
        self.scene_cut_overlay_visibility_changed.emit(visible)
        logger.debug("Scene cut overlay visible: %s", visible)

    def is_scene_cut_overlay_visible(self) -> bool:
        """Return whether the scene cut overlay is currently enabled."""
        return self._scene_cut_overlay_visible

    def _on_scene_cut_analysis_for_overlay(self, analysis: Any) -> None:
        """Store the latest detection candidates for the timeline overlay.

        AUD-19902: 候选携带的是**源媒体**帧号（scene_cut/models.py:
        ``source_frame: Zero-based frame index in source media``），而叠加
        层需要时间线坐标。此前直接把源帧当时间线帧建标记——检测自时间线
        片段时（source_start≠0 或素材多轨复用）标记错位，检测自媒体池
        素材时甚至串到无关时间线上。现在先按 ``analysis.asset_id``/
        ``clip_id`` 把结果绑定到当前时间线上的片段，再逐候选经
        ``clip.source_to_timeline_frame()``（含变速/retime 映射）换算；
        无匹配片段或落在所有片段源范围之外的候选被丢弃（已是时间线坐标的
        候选直接透传）。
        """
        timeline_view = self._get_scene_cut_timeline_view()
        if timeline_view is None:
            return
        timeline_view.set_scene_cut_candidates(
            self._map_scene_cut_candidates_for_overlay(analysis)
        )

    def _map_scene_cut_candidates_for_overlay(self, analysis: Any) -> tuple:
        """Map an analysis' source-frame candidates onto the current timeline.

        Returns a tuple of ``SceneCutCandidate`` copies carrying
        ``timeline_frame`` (the overlay prefers it over ``source_frame``).
        Candidates that already carry a ``timeline_frame`` pass through
        unchanged (upstream mapped them already); pure source-frame
        candidates are mapped through the clips bound to the analysis
        (``clip_id`` first, then video clips of ``asset_id``) and dropped
        when no binding covers them.
        """
        candidates = tuple(getattr(analysis, "candidates", ()) or ())
        if not candidates:
            return ()
        timeline = self._current_timeline
        mapped = []
        clips: list | None = None  # 惰性解析：首个需要映射的候选才绑定片段

        for candidate in candidates:
            if getattr(candidate, "timeline_frame", None) is not None:
                # 已是时间线坐标（上游按时间线片段生成），无需再映射。
                mapped.append(candidate)
                continue
            source_frame = getattr(candidate, "source_frame", None)
            with_timeline_frame = getattr(candidate, "with_timeline_frame", None)
            if source_frame is None or not callable(with_timeline_frame):
                continue
            if clips is None:
                clips = self._scene_cut_binding_clips(analysis, timeline)
            timeline_frame = None
            for clip in clips:
                try:
                    timeline_frame = int(
                        clip.source_to_timeline_frame(int(source_frame))
                    )
                except (ValueError, TypeError, ZeroDivisionError):
                    # Outside this clip's source range (or corrupt mapping
                    # data) — try the next clip of the same asset.
                    continue
                break
            if timeline_frame is None:
                continue  # 无任何匹配片段覆盖该源帧：丢弃而非错位放置
            mapped.append(candidate.with_timeline_frame(timeline_frame))
        return tuple(mapped)

    def _scene_cut_binding_clips(self, analysis: Any, timeline: Any) -> list:
        """Resolve the timeline clips an analysis binds to (clip_id 优先)."""
        if timeline is None:
            return []
        clip = timeline.get_clip_by_id(getattr(analysis, "clip_id", None) or "")
        if clip is not None:
            # Analysis ran on this timeline clip — it is the authoritative
            # source-range mapping.
            return [clip]
        asset_id = getattr(analysis, "asset_id", "") or ""
        clips: list = []
        if asset_id:
            for track in getattr(timeline, "video_tracks", ()) or ():
                for track_clip in getattr(track, "clips", ()) or ():
                    if getattr(track_clip, "asset_id", "") == asset_id:
                        clips.append(track_clip)
        return clips

    def _get_scene_cut_timeline_view(self) -> Any:
        """Return the timeline view, or None when panels are not attached."""
        panel = self._timeline_panel
        view = getattr(panel, "get_timeline_view", None)
        if callable(view):
            return view()
        return None

    def _persist_scene_cut_overlay_state(self) -> None:
        """Persist the overlay switch via ConfigManager (best effort)."""
        try:
            from src.shared.config.config_manager import ConfigManager

            ConfigManager().set(
                self._SCENE_CUT_OVERLAY_CONFIG_KEY,
                self._scene_cut_overlay_visible,
            )
        except Exception:  # noqa: BLE001
            logger.debug("persist scene cut overlay state failed", exc_info=True)

    def _restore_scene_cut_overlay_state(self) -> None:
        """Re-apply the overlay switch to the (possibly newly attached) view.

        First invocation adopts the persisted config; afterwards the
        in-memory flag is authoritative.  The state is always pushed to the
        current timeline view directly — the setter's equality early-return
        must not stop a rebuilt view from receiving the state.
        """
        if not getattr(self, "_scene_cut_overlay_state_loaded", False):
            self._scene_cut_overlay_state_loaded = True
            stored: Any = False
            try:
                from src.shared.config.config_manager import ConfigManager

                stored = ConfigManager().get(self._SCENE_CUT_OVERLAY_CONFIG_KEY)
            except Exception:  # noqa: BLE001
                logger.debug("read scene cut overlay state failed", exc_info=True)
            if bool(stored):
                self._scene_cut_overlay_visible = False
                self.set_scene_cut_overlay_visible(True)
                return
        timeline_view = self._get_scene_cut_timeline_view()
        if timeline_view is not None:
            timeline_view.set_scene_cut_overlay_visible(
                self._scene_cut_overlay_visible)

    def _connect_scene_cut_to_analysis(self, controller: Any) -> None:
        """把 SceneCutController.detection_completed 接到共享 AnalysisService。

        被动接收：用户从右键菜单触发场景检测后，结果灌入 AI 证据仓库
        （TAEP §5.4）。无 MainWindow / 无 AI 装配时静默跳过（不破坏纯剪辑场景）。
        """
        analysis_service = getattr(self._main_window, "_shared_analysis_service", None)
        if analysis_service is None or not hasattr(
            analysis_service, "ingest_scene_cut_analysis"
        ):
            return
        try:
            controller.detection_completed.connect(
                lambda analysis: self._on_scene_cut_detected(analysis, analysis_service)
            )
        except (AttributeError, TypeError):
            logger.debug("connect scene_cut.detection_completed failed", exc_info=True)

    def _on_scene_cut_detected(self, analysis: Any, analysis_service: Any) -> None:
        """SceneCutController.detection_completed 槽：灌入 AnalysisService。

        失败不抛（不破坏场景检测本身的成功路径），仅 debug 日志。
        """
        try:
            asset_id = getattr(analysis, "asset_id", "") or ""
            if not asset_id:
                return
            n = analysis_service.ingest_scene_cut_analysis(asset_id, analysis)
            logger.debug("ingested %d SHOT segments for asset %s", n, asset_id)
        except Exception:  # noqa: BLE001
            logger.debug("ingest_scene_cut_analysis failed", exc_info=True)

    def _on_scene_cut_detect_from_asset(self, asset_id: str) -> None:
        """
        Handle Scene Cut Detection request from Media Pool right-click (P9).

        Args:
            asset_id: Media asset ID to run detection on.
        """
        logger.info(f"[EditPage P9] Scene cut detect requested for asset: {asset_id}")
        controller = self._ensure_scene_cut_controller()
        if controller is not None:
            controller.start_detection_for_asset(asset_id)
        else:
            logger.warning("Scene cut controller not available")

    # ==================== M3: 媒体池可选快速分析（§17.2）==================== #
    #
    # panel 只发意图信号；此处接到 _main_window（ai_editing_mixin）持有的
    # MediaAnalysisController，走三步流（propose → confirm → execute）。controller
    # 负责 §17.3 红线（未确认不分析）+ scheduler 优先级；edit_page 只做信号桥接，
    # 不写 timeline、不解析结果（结果经 controller.analysis_status_changed 回灌 badge）。

    def _get_media_analysis_controller(self) -> Any:
        """取 ai_editing_mixin 装配的 MediaAnalysisController（可能为 None）。"""
        host = getattr(self, "_main_window", None)
        if host is None:
            return None
        return getattr(host, "_media_analysis_controller", None)

    def _on_analysis_requested(self, asset_ids: list, scope: str) -> None:
        """媒体池请求分析（selected/bin/all）。propose 一份未确认 request。

        "all" 范围：controller / 确认卡负责二次确认（§17.3 红线）。本批首版直接 propose，
        确认卡 UI 留 M5 Agent Dock。
        """
        controller = self._get_media_analysis_controller()
        if controller is None:
            logger.info("MediaAnalysisController not available; analysis request ignored")
            return
        if not asset_ids:
            return
        try:
            from src.features.edit.ai_editing.analysis_request import AnalysisRecipe

            controller.request_analysis_draft(
                asset_ids, recipe=AnalysisRecipe.FAST_LOCAL, requested_by="media_pool",
            )
        except Exception:
            logger.debug("analysis request_analysis_draft failed", exc_info=True)

    def _on_analysis_cancel_requested(self, asset_id: str) -> None:
        """取消某 asset 的分析（P0-8：per-asset 定向取消，不再 cancel_all）。"""
        controller = self._get_media_analysis_controller()
        if controller is None:
            return
        try:
            cancel_asset = getattr(controller, "cancel_asset_analysis", None)
            if callable(cancel_asset):
                cancel_asset(asset_id)
        except Exception:
            logger.debug("analysis cancel failed", exc_info=True)

    def _on_analysis_retry_requested(self, asset_id: str) -> None:
        """重试：等同发起一次新的 selected 分析。"""
        self._on_analysis_requested([asset_id], "selected")

    def _on_analysis_result_cleared(self, asset_id: str) -> None:
        """清空某 asset 的分析结果（不写 timeline；只清 service 记录 + badge）。"""
        controller = self._get_media_analysis_controller()
        if controller is None:
            return
        try:
            service = getattr(controller, "_service", None)
            if service is not None and hasattr(service, "invalidate"):
                service.invalidate(asset_id)
            # 刷新 badge：清掉 ready 状态
            if hasattr(controller, "_emit_status"):
                controller._emit_status(asset_id, "unknown")
        except Exception:
            logger.debug("analysis clear failed", exc_info=True)

    def _on_scene_cut_detect_from_clip(
        self, clip_id: str, track_id: str, asset_id: str
    ) -> None:
        """
        Handle Scene Cut Detection request from Timeline clip right-click (P9).

        Args:
            clip_id: Timeline clip ID.
            track_id: Track containing the clip.
            asset_id: Source media asset ID.
        """
        logger.info(
            f"[EditPage P9] Scene cut detect requested for clip: "
            f"{clip_id}, track={track_id}, asset={asset_id}"
        )
        controller = self._ensure_scene_cut_controller()
        if controller is not None:
            controller.start_detection_for_clip(clip_id, track_id, asset_id)
        else:
            logger.warning("Scene cut controller not available")

    def _on_speech_to_text_from_clip(
        self, clip_id: str, track_id: str, asset_id: str
    ) -> None:
        """Forward an audio-clip transcription request to the host controller.

        EditPage intentionally remains a signal bridge: ASR scheduling and
        timeline command execution belong to the MainWindow composition root,
        just like scene-cut detection.
        """
        host = getattr(self, "_main_window", None)
        handler = getattr(host, "_on_speech_to_text_requested", None)
        if callable(handler):
            handler(clip_id, track_id, asset_id)
        else:
            logger.warning("Speech-to-text handler is not available")

    def _on_subtitle_to_speech_from_track(self, track_id: str) -> None:
        """Forward a subtitle-track voice-over request to MainWindow."""
        host = getattr(self, "_main_window", None)
        handler = getattr(host, "_on_subtitle_to_speech_requested", None)
        if callable(handler):
            handler(track_id)
        else:
            logger.warning("Subtitle-to-speech handler is not available")

    def _on_subtitle_export_requested(self, track_id: str, format_name: str) -> None:
        """Export one subtitle track after the user chooses its destination."""
        if self._timeline_panel is None:
            return

        timeline_view = self._timeline_panel.get_timeline_view()
        timeline = getattr(timeline_view, "_timeline", None) or self._current_timeline
        if timeline is None:
            return
        track = timeline.get_track_by_id(track_id)
        if track is None or track.track_type != "subtitle":
            logger.warning("Cannot export missing subtitle track: %s", track_id)
            return

        from src.features.edit.core.captions.export import (
            FORMAT_EXTENSIONS,
            write_subtitle_track,
        )
        from src.shared.i18n.i18n import I18N

        normalized = str(format_name).lower().lstrip(".")
        extension = FORMAT_EXTENSIONS.get(normalized)
        if extension is None:
            logger.warning("Unsupported subtitle export format: %s", format_name)
            return

        safe_name = "".join(
            char if char.isalnum() or char in "-_ ." else "_"
            for char in (track.name or "subtitles")
        ).strip(" .") or "subtitles"
        format_labels = {
            "srt": "SRT",
            "vtt": "WebVTT",
            "ass": "ASS/SSA",
            "ttml": "TTML/DFXP",
            "dfxp": "DFXP",
            "scc": "SCC / CEA-608",
        }
        label = format_labels[normalized]
        destination, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr("edit.subtitle_export_title", "Export Subtitles"),
            str(Path.home() / f"{safe_name}{extension}"),
            f"{label} (*{extension})",
        )
        if not destination:
            return
        if not destination.lower().endswith(extension):
            destination += extension

        try:
            write_subtitle_track(track, normalized, timeline.fps, destination)
        except (OSError, ValueError) as exc:
            logger.exception("Subtitle export failed")
            QMessageBox.critical(
                self,
                I18N.tr("edit.subtitle_export_failed_title", "Subtitle Export Failed"),
                I18N.tr(
                    "edit.subtitle_export_failed",
                    "Could not export subtitles:\n{error}",
                    error=str(exc),
                ),
            )
            return

        QMessageBox.information(
            self,
            I18N.tr("edit.subtitle_export_complete_title", "Subtitle Export Complete"),
            I18N.tr(
                "edit.subtitle_export_complete",
                "Exported {count} cues to:\n{path}",
                count=len(track.subtitle_clips),
                path=destination,
            ),
        )

    def _on_subtitle_quality_requested(self, track_id: str) -> None:
        """Run cue-level QC from the subtitle track header."""
        if self._timeline_panel is None:
            return
        timeline_view = self._timeline_panel.get_timeline_view()
        timeline = getattr(timeline_view, "_timeline", None) or self._current_timeline
        track = timeline.get_track_by_id(track_id) if timeline is not None else None
        if track is None or track.track_type != "subtitle":
            return
        from src.features.edit.core.captions.quality import inspect_subtitle_track
        report = inspect_subtitle_track(
            track, timeline.fps,
            timeline_duration_frames=timeline.duration_frames,
        )
        if report.passed and not report.issues:
            message = f"{report.cue_count} cues checked. No issues found."
        else:
            lines = [
                f"{report.cue_count} cues: {report.error_count} errors, "
                f"{report.warning_count} warnings",
                "",
            ]
            for issue in report.issues[:40]:
                cue = f" [{issue.cue_id}]" if issue.cue_id else ""
                lines.append(f"{issue.severity.upper()} {issue.code}{cue}: {issue.message}")
            if len(report.issues) > 40:
                lines.append(f"... and {len(report.issues) - 40} more")
            message = "\n".join(lines)
        QMessageBox.information(
            self,
            "Subtitle Quality Check",
            message,
        )

    def _on_subtitle_language_requested(self, track_id: str) -> None:
        """Set the BCP-47 language tag used by TTML and language workflows."""
        if self._timeline_panel is None:
            return
        timeline_view = self._timeline_panel.get_timeline_view()
        timeline = getattr(timeline_view, "_timeline", None) or self._current_timeline
        track = timeline.get_track_by_id(track_id) if timeline is not None else None
        if track is None or track.track_type != "subtitle":
            return
        from PyQt6.QtWidgets import QInputDialog
        current = track.caption_metadata.language if track.caption_metadata else ""
        language, accepted = QInputDialog.getText(
            self, "Caption Language", "BCP-47 language tag (e.g. zh-CN, en-US):",
            text=current,
        )
        if not accepted:
            return
        from src.features.edit.core.captions.commands import UpdateSubtitleTrackMetadataCommand
        command = UpdateSubtitleTrackMetadataCommand.create(
            track_id, {"language": language.strip()},
        )
        # AUD-19900: EditPage 从不存在 ``_execute_subtitle_command``（它只在
        # EditController 上），旧代码在此抛 AttributeError——命令未进 undo
        # 栈、语言未写入模型。统一走 ``TimelineView.execute_command``：与
        # 控制器的字幕提交入口同一条命令链（undo 栈 / timeline_changed /
        # 项目标脏）。槽内不允许未处理异常，失败转为日志 + 用户可见提示。
        try:
            timeline_view.execute_command(command)
        except Exception as exc:
            logger.exception("Failed to set subtitle track language: %s", exc)
            QMessageBox.warning(
                self,
                "Caption Language",
                f"Failed to set language: {exc}",
            )

    def on_scene_cut_detect(self) -> None:
        """
        Handle Scene Cut Detection from Edit menu action (P9-1).

        Runs detection on the currently selected clip or playhead clip.
        Called via callback_name="on_scene_cut_detect" from module.py.
        """
        logger.info("[EditPage P9] Scene cut detect requested from menu")

        # Determine target clip from timeline panel
        if self._timeline_panel is None:
            logger.warning("No timeline panel available for scene cut detection")
            return

        timeline_view = self._timeline_panel.get_timeline_view()
        if timeline_view is None:
            logger.warning("No timeline view available")
            return

        # Try to get selected clip or clip at playhead
        selection = None
        if hasattr(timeline_view, '_selection'):
            selection = timeline_view._selection

        if selection and hasattr(selection, 'clip_ids') and selection.clip_ids:
            # Use first selected clip
            clip_id = selection.clip_ids[0]
            # Find the clip's track and asset
            timeline = getattr(timeline_view, '_timeline', None)
            if timeline:
                for track in timeline.all_tracks:
                    clip = next(
                        (c for c in track.clips if c.clip_id == clip_id),
                        None,
                    )
                    if clip:
                        asset_id = clip.asset_id or ""
                        controller = self._ensure_scene_cut_controller()
                        if controller is not None:
                            controller.start_detection_for_clip(
                                clip_id, track.track_id, asset_id
                            )
                        return

        logger.info("No clip selected — please select a clip on the timeline first")
