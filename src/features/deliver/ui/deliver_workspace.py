"""Complete Deliver workspace for video export.

The workspace follows the Deliver arrangement used by DaVinci Resolve:
settings stay in a fixed left inspector, while the right side is split into a
viewer/queue row above a full-width timeline.  The queue can still be moved
into a regular QDockWidget through ``DetachablePanelHost``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSplitter, QWidget

from src.features.deliver.core.video_batch_export_controller import (
    VideoBatchExportController,
)
from src.features.deliver.core.video_export_job import VideoExportJob
from src.features.deliver.ui.detachable_panel_host import DetachablePanelHost
from src.features.deliver.ui.video_batch_export_panel import VideoBatchExportPanel
from src.features.deliver.ui.video_export_settings_panel import VideoExportSettingsPanel
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C

logger = logging.getLogger(__name__)

__all__ = ["DeliverWorkspace"]


class DeliverWorkspace(QWidget):
    """The complete video Deliver page.

    The left pane creates one or more immutable video task snapshots.  The
    centre pane summarizes the active delivery context, and the right pane
    displays and controls the same queue owned by the controller.  The right
    pane starts embedded, but can be detached without rebuilding the widget
    or losing the queue.
    """

    export_started = pyqtSignal()
    export_completed = pyqtSignal(bool)
    export_cancelled = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("DeliverWorkspace")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#DeliverWorkspace { background-color: #27272B; }"
        )
        # The nested Deliver layout needs room for the settings inspector,
        # viewer and a readable queue while leaving a substantial timeline.
        self.setMinimumSize(1240, 620)

        self._controller = VideoBatchExportController(parent=self)
        self._settings_panel = VideoExportSettingsPanel(self)
        self._settings_panel.range_type_changed.connect(self._on_range_type_changed)
        self._batch_panel = VideoBatchExportPanel(self)
        self._batch_panel_host = DetachablePanelHost(self)
        self._main_window: Optional[Any] = None
        # AUD-12209: legacy signal contract tracking — export_started is
        # emitted when the queue actually starts running (not on enqueue),
        # export_completed/export_cancelled when it stops / a cancel is
        # forwarded.
        self._queue_running: bool = False
        self._run_active: bool = False
        self._run_failed: bool = False
        # Top row: the real Edit viewer occupies the large area and the queue
        # sits in a narrower Resolve-style right column.
        self._edit_preview_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._edit_preview_splitter.setObjectName("DeliverEditPreviewSplitter")
        self._edit_preview_splitter.setChildrenCollapsible(False)
        self._edit_preview_splitter.setHandleWidth(1)
        self._edit_preview_splitter.setStyleSheet(
            f"QSplitter#DeliverEditPreviewSplitter::handle {{"
            f"background: {C.PANEL_DIVIDER}; }}"
            f"QSplitter#DeliverEditPreviewSplitter::handle:hover {{"
            f"background: {C.PANEL_DIVIDER}; }}"
        )
        self._edit_preview_placeholder = self._make_placeholder(
            "Edit Program Viewer will appear here"
        )
        self._edit_preview_splitter.addWidget(self._edit_preview_placeholder)
        self._edit_preview_splitter.addWidget(self._batch_panel)
        self._edit_preview_splitter.setStretchFactor(0, 1)
        self._edit_preview_splitter.setStretchFactor(1, 0)
        self._edit_preview_splitter.setSizes([620, 420])

        # Bottom row: the timeline is deliberately outside the top splitter,
        # so it spans under both the viewer and the render queue.
        self._timeline_placeholder = self._make_placeholder(
            "Edit Timeline will appear here"
        )
        self._right_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._right_splitter.setObjectName("DeliverRightSplitter")
        self._right_splitter.setChildrenCollapsible(False)
        self._right_splitter.setHandleWidth(1)
        self._right_splitter.setStyleSheet(
            f"QSplitter#DeliverRightSplitter::handle {{"
            f"background: {C.PANEL_DIVIDER}; }}"
            f"QSplitter#DeliverRightSplitter::handle:hover {{"
            f"background: {C.PANEL_DIVIDER}; }}"
        )
        self._right_splitter.addWidget(self._edit_preview_splitter)
        self._right_splitter.addWidget(self._timeline_placeholder)
        self._right_splitter.setStretchFactor(0, 0)
        self._right_splitter.setStretchFactor(1, 1)
        self._right_splitter.setSizes([470, 560])

        self._edit_panels_attached = False
        self._shared_program_viewer: Optional[QWidget] = None
        self._shared_timeline_panel: Optional[QWidget] = None
        self._program_viewer_dock: Optional[Any] = None
        self._timeline_dock: Optional[Any] = None
        self._deliver_range_type: str = "entire"

        self._settings_panel.setMinimumWidth(420)
        self._batch_panel.setMinimumWidth(390)

        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setObjectName("DeliverHorizontalSplitter")
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(1)
        self._splitter.addWidget(self._settings_panel)
        self._splitter.addWidget(self._right_splitter)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([440, 1000])
        self._splitter.setStyleSheet(
            "QSplitter#DeliverHorizontalSplitter::handle {"
            f"background: {C.PANEL_DIVIDER}; }}"
            "QSplitter#DeliverHorizontalSplitter::handle:hover {"
            f"background: {C.PANEL_DIVIDER}; }}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._splitter)

        self._setup_connections()

    # ------------------------------------------------------------------
    # Public handles
    # ------------------------------------------------------------------

    @property
    def controller(self) -> VideoBatchExportController:
        return self._controller

    @property
    def settings_panel(self) -> VideoExportSettingsPanel:
        return self._settings_panel

    @property
    def batch_panel(self) -> VideoBatchExportPanel:
        return self._batch_panel

    @property
    def edit_preview_splitter(self) -> QSplitter:
        """The top-right splitter containing viewer and render queue."""
        return self._edit_preview_splitter

    @property
    def timeline_splitter(self) -> QSplitter:
        """The right-side vertical splitter whose bottom row is the timeline."""
        return self._right_splitter

    def _make_placeholder(self, text: str) -> QLabel:
        placeholder = QLabel(text)
        placeholder.setObjectName("DeliverEditPreviewPlaceholder")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: {C.BG_DEEPEST}; font-size: 12px;"
        )
        return placeholder

    @property
    def splitter(self) -> QSplitter:
        return self._splitter

    def ensure_deliver_layout(self) -> None:
        """Restore the Resolve-style proportions after Qt state restore."""
        width = max(self._splitter.width(), self.minimumWidth())
        settings_width = 440
        right_width = max(760, width - settings_width - 8)
        self._splitter.setSizes([settings_width, right_width])
        if self._edit_panels_attached:
            right_height = max(self._right_splitter.height(), 520)
            top_height = max(360, int(right_height * 0.46))
            self._right_splitter.setSizes([top_height, max(240, right_height - top_height)])
            top_width = max(self._edit_preview_splitter.width(), 760)
            queue_width = 420
            self._edit_preview_splitter.setSizes(
                [max(420, top_width - queue_width), queue_width]
            )

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _setup_connections(self) -> None:
        self._settings_panel.add_to_queue_requested.connect(self._on_add_jobs)
        self._settings_panel.add_and_start_requested.connect(self._on_add_and_start)

        self._batch_panel.start_requested.connect(self._controller.start_queue)
        self._batch_panel.pause_requested.connect(self._controller.pause_queue)
        self._batch_panel.resume_requested.connect(self._controller.resume_queue)
        self._batch_panel.cancel_current_requested.connect(self._controller.cancel_current)
        self._batch_panel.clear_completed_requested.connect(self._controller.clear_completed)
        self._batch_panel.retry_requested.connect(self._controller.retry_job)
        self._batch_panel.remove_requested.connect(self._controller.remove_job)
        self._batch_panel.edit_job_requested.connect(self._on_edit_job)
        self._batch_panel.open_output_requested.connect(self._on_open_output)
        self._batch_panel.detach_requested.connect(self.float_batch_panel)
        self._batch_panel.return_to_deliver_requested.connect(self.return_batch_panel_to_deliver)

        self._controller.jobs_changed.connect(self._refresh_batch_panel)
        self._controller.job_progress.connect(self._batch_panel.update_job_row)
        self._controller.queue_running_changed.connect(self._batch_panel.set_queue_running)
        # AUD-12209: bridge the controller's real run state onto the legacy
        # page-level signals so export_started/export_completed/
        # export_cancelled are actually emitted with truthful semantics.
        self._controller.queue_running_changed.connect(self._on_queue_running_changed)
        self._controller.job_finished.connect(self._on_job_finished)
        self._controller.runner.cancel_requested.connect(self._on_export_cancel_requested)

    def connect_batch_panel(self, batch_panel: Any = None) -> None:
        """Compatibility hook for older MainWindow wiring.

        The workspace owns exactly one queue panel.  Ignoring a foreign panel
        prevents a second disconnected queue Dock from being created by stale
        callers.
        """
        if batch_panel is not None and batch_panel is not self._batch_panel:
            logger.warning(
                "Ignoring external video batch panel; DeliverWorkspace owns the panel"
            )
        self._refresh_batch_panel()

    def attach_batch_panel_host(self, main_window: Any) -> None:
        """Enable queue detaching after the Deliver dock is in MainWindow."""
        if self._main_window is main_window and self._batch_panel_host.mode == "embedded":
            return
        self._main_window = main_window
        # M21.2c 接线：批队列排空后的逐版本 QC 回填主窗口缓存——
        # Deliver/Quality 菜单的多版本 QC 面板经 _last_multi_version_qc
        # 呈现本轮实测（幂等重连安全）。
        try:
            self._controller.multi_version_qc_ready.disconnect(
                self._on_multi_version_qc_ready)
        except (TypeError, RuntimeError):
            pass
        self._controller.multi_version_qc_ready.connect(
            self._on_multi_version_qc_ready)
        # The queue is embedded in the top-right splitter. Detaching it leaves
        # the timeline row spanning the full width of the right side.
        self._batch_panel_host.attach(
            self._batch_panel, self._edit_preview_splitter, main_window
        )
        try:
            self._batch_panel_host.mode_changed.disconnect(self._on_batch_host_mode_changed)
        except (TypeError, RuntimeError):
            pass
        self._batch_panel_host.mode_changed.connect(self._on_batch_host_mode_changed)
        self._refresh_batch_panel()

    def _on_multi_version_qc_ready(self, report: dict) -> None:
        """M21.2c：逐版本 QC 报告 → 主窗口缓存（面板呈现数据源）。"""
        mw = self._main_window
        if mw is not None:
            try:
                mw._last_multi_version_qc = dict(report or {})
            except Exception:  # noqa: BLE001 — 缓存失败只影响徽标数据源
                logger.debug("stash multi-version QC report failed",
                             exc_info=True)

    def float_batch_panel(self) -> None:
        """Detach only the queue into a floatable/dockable QDockWidget."""
        if self._main_window is not None:
            self._batch_panel_host.float_panel()

    def return_batch_panel_to_deliver(self) -> None:
        """Return the queue to the right pane of this Deliver workspace."""
        self._batch_panel_host.return_to_splitter()

    def _on_batch_host_mode_changed(self, mode: str) -> None:
        """Register a dynamic floating queue dock with the dock manager."""
        if self._main_window is None:
            return
        docks = getattr(self._main_window, "all_docks", None)
        dock = self._batch_panel_host.dock
        if not isinstance(docks, list):
            return

        if mode == "floating" and dock is not None and dock not in docks:
            docks.append(dock)
            setattr(self._main_window, "dockVideoBatchExport", dock)
            refresh = getattr(self._main_window, "_perform_update_all_titlebars", None)
            if callable(refresh):
                refresh()
        elif mode == "embedded":
            old_dock = getattr(self._main_window, "dockVideoBatchExport", None)
            if old_dock in docks:
                docks.remove(old_dock)
            setattr(self._main_window, "dockVideoBatchExport", None)

    def _refresh_batch_panel(self) -> None:
        self._batch_panel.refresh_from_queue(self._controller.queue)

    def refresh_batch_panel(self) -> None:
        """Force the queue panel to re-read the queue."""
        self._refresh_batch_panel()

    # ------------------------------------------------------------------
    # Legacy DeliverPage API
    # ------------------------------------------------------------------

    def attach_edit_panels(
        self,
        program_viewer: Optional[QWidget],
        timeline_panel: Optional[QWidget],
        program_viewer_dock: Optional[Any] = None,
        timeline_dock: Optional[Any] = None,
    ) -> None:
        """Move the real Edit viewer and TimelinePanel into Deliver.

        The widgets are moved, not copied.  This preserves the existing
        playback/render connections and ensures the playhead in Deliver is
        the same playhead used by Edit.
        """
        if self._edit_panels_attached:
            return
        self._shared_program_viewer = program_viewer
        self._shared_timeline_panel = timeline_panel
        self._program_viewer_dock = program_viewer_dock
        self._timeline_dock = timeline_dock

        if self._edit_preview_placeholder is not None:
            self._edit_preview_placeholder.setParent(None)
            self._edit_preview_placeholder.deleteLater()
            self._edit_preview_placeholder = None

        if self._timeline_placeholder is not None:
            self._timeline_placeholder.setParent(None)
            self._timeline_placeholder.deleteLater()
            self._timeline_placeholder = None

        if program_viewer is not None:
            if program_viewer_dock is not None and program_viewer_dock.widget() is program_viewer:
                program_viewer_dock.setWidget(None)
                program_viewer_dock.hide()
            program_viewer.setParent(None)
            self._edit_preview_splitter.insertWidget(0, program_viewer)
            program_viewer.show()

        if timeline_panel is not None:
            if timeline_dock is not None and timeline_dock.widget() is timeline_panel:
                timeline_dock.setWidget(None)
                timeline_dock.hide()
            set_deliver_controls = getattr(
                timeline_panel, "set_deliver_timeline_controls", None
            )
            get_deliver_controls = getattr(
                self._settings_panel, "get_deliver_timeline_controls", None
            )
            if callable(set_deliver_controls) and callable(get_deliver_controls):
                set_deliver_controls(get_deliver_controls())
            set_display_mode = getattr(timeline_panel, "set_display_mode", None)
            if callable(set_display_mode):
                # Deliver reuses the live Edit panel, but its toolbar is a
                # display concern.  Keep only the zoom controls and Deliver's
                # Timeline/Range controls visible here; the panel restores
                # Edit's original visibility on detach.
                set_display_mode("deliver")
            timeline_panel.setParent(None)
            self._right_splitter.insertWidget(1, timeline_panel)
            timeline_panel.show()

        self._edit_preview_splitter.setStretchFactor(0, 3)
        self._edit_preview_splitter.setStretchFactor(1, 0)
        self._edit_preview_splitter.setSizes([620, 420])
        self._right_splitter.setStretchFactor(0, 0)
        self._right_splitter.setStretchFactor(1, 1)
        self._right_splitter.setSizes([470, 560])
        self._edit_panels_attached = True
        timeline_changed = getattr(timeline_panel, "timeline_changed", None)
        if timeline_changed is not None:
            timeline_changed.connect(self._on_shared_timeline_changed)
        self._on_range_type_changed(self._settings_panel.current_range_type())
        self.ensure_deliver_layout()

    def detach_edit_panels(self) -> None:
        """Return the shared viewer/timeline to their original Edit docks."""
        if not self._edit_panels_attached:
            return
        if self._shared_program_viewer is not None:
            self._shared_program_viewer.setParent(None)
            if self._program_viewer_dock is not None:
                self._program_viewer_dock.setWidget(self._shared_program_viewer)
                self._shared_program_viewer.show()

        if self._shared_timeline_panel is not None:
            timeline_changed = getattr(self._shared_timeline_panel, "timeline_changed", None)
            if timeline_changed is not None:
                try:
                    timeline_changed.disconnect(self._on_shared_timeline_changed)
                except (TypeError, RuntimeError):
                    pass
            self._shared_timeline_panel.setParent(None)
            if self._timeline_dock is not None:
                self._timeline_dock.setWidget(self._shared_timeline_panel)
            set_display_mode = getattr(self._shared_timeline_panel, "set_display_mode", None)
            if callable(set_display_mode):
                set_display_mode("edit")
            set_range_mode = getattr(
                self._shared_timeline_panel,
                "set_deliver_range_type",
                None,
            )
            if callable(set_range_mode):
                set_range_mode(None)
            self._shared_timeline_panel.show()

        self._edit_preview_placeholder = self._make_placeholder(
            "Edit Program Viewer will appear here"
        )
        self._edit_preview_splitter.insertWidget(0, self._edit_preview_placeholder)
        self._timeline_placeholder = self._make_placeholder(
            "Edit Timeline will appear here"
        )
        self._right_splitter.insertWidget(1, self._timeline_placeholder)
        self._edit_preview_splitter.setSizes([1, 0])
        self._right_splitter.setSizes([1, 0])
        self._edit_panels_attached = False

    def _on_shared_timeline_changed(self, timeline: Any) -> None:
        """Keep Deliver's draft context current while its shared timeline is live."""
        if self._edit_panels_attached and timeline is not None:
            # R01（第七轮审计）：Deliver 页内继续编辑（Split/Delete/Undo/
            # Redo）时 live 序列更新也走这里——队列上下文按运行时读取
            # （_resolve_context），不同步 register 会让任务 done 但导出
            # 的是编辑前的旧时间线。
            register_sequence = getattr(self._controller, "register_sequence", None)
            if callable(register_sequence) and (
                hasattr(timeline, "video_tracks")
                or hasattr(timeline, "audio_tracks")
            ):
                register_sequence(timeline)
            self._settings_panel.set_sequence(timeline)

    def set_project(self, project: Any) -> None:
        """Set active project and populate the available timeline choices."""
        project_id = ""
        project_revision = 0
        project_name = ""
        sequences: List[Any] = []

        if project is not None:
            project_id = str(
                getattr(project, "project_id", "")
                or getattr(project, "id", "")
                or ""
            )
            project_revision = int(getattr(project, "revision", 0) or 0)
            project_name = str(getattr(project, "name", "") or "")

            for attr in ("sequences", "timelines", "timeline_sequences"):
                seqs = getattr(project, attr, None)
                if seqs:
                    # EditProject stores ``timelines`` as a mapping from ID
                    # to TimelineSequenceRef. Iterating the mapping directly
                    # yields only IDs, which later makes the Deliver combo and
                    # context resolver lose the real timeline identifiers.
                    if isinstance(seqs, dict):
                        sequences = list(seqs.values())
                    else:
                        sequences = list(seqs)
                    break

            active_timeline = getattr(project, "active_timeline", None)
            if active_timeline is not None and active_timeline not in sequences:
                sequences.insert(0, active_timeline)

        self._controller.set_active_project(
            project,
            project_id=project_id,
            project_revision=project_revision,
            sequences=sequences,
        )
        self._settings_panel.set_project(
            project,
            project_id=project_id,
            project_revision=project_revision,
            project_name=project_name,
        )
        self._settings_panel.set_timelines(self._sequence_choices(sequences))
        # 复验 P1-14：preflight 的 nested 音频展开与真实导出同源
        seq_map = {}
        for sq in sequences:
            tid = getattr(sq, "timeline_id", None)
            if tid:
                seq_map[str(tid)] = sq
        if seq_map:
            self._settings_panel.set_timeline_sequences(seq_map)

        # Restore persisted tasks once per project identity.  The queue file
        # is shared by the application, so the in-memory queue is cleared
        # before loading the current project's tasks.
        previous_project_id = getattr(self, "_last_restored_project_id", None)
        is_new_project = previous_project_id is None or previous_project_id != project_id
        if project is not None and is_new_project:
            for legacy in list(self._controller.queue.jobs):
                self._controller.queue.remove(legacy.job_id)
            self._controller.restore_from_disk()
            self._last_restored_project_id = project_id
        self._refresh_batch_panel()

    def set_sequence(self, sequence: Any) -> None:
        if sequence is not None:
            register_sequence = getattr(self._controller, "register_sequence", None)
            if callable(register_sequence):
                # Program-viewer timeline switches arrive here with the full
                # TimelineSequence. Keep the queue context in lock-step with
                # the timeline that the user is actually viewing.
                register_sequence(sequence)
        self._settings_panel.set_sequence(sequence)
        if sequence is not None:
            sequence_id = getattr(sequence, "timeline_id", None) or getattr(
                sequence, "sequence_id", None
            )
            if sequence_id:
                self._settings_panel.set_active_timeline(str(sequence_id))
        self._on_range_type_changed(self._settings_panel.current_range_type())

    def set_timeline_sequences(self, sequences: Any) -> None:
        """接收宿主的全部完整时间线（U02，第六轮审计）。

        ``page_controller`` 在进入 Deliver 时以 callable 守卫调用本方法，
        但此前 Workspace 没有这个方法而被静默跳过——嵌套序列导出因此拿
        不到子序列 resolver 而失败。这里把完整序列 map 同时交给：
        1. settings panel（preflight 的 nested 展开，与真实导出同源）；
        2. batch controller（promote 成完整序列，替换 set_project 从
           ``project.timelines`` 拿到的轻量 ``TimelineSequenceRef``）。

        Args:
            sequences: ``{timeline_id: TimelineSequence}`` map 或可迭代的
                完整序列集合。
        """
        if not sequences:
            return
        seq_map: Dict[str, Any] = {}
        if isinstance(sequences, dict):
            candidates = list(sequences.values())
        else:
            candidates = list(sequences)
        for seq in candidates:
            tid = getattr(seq, "timeline_id", None)
            # 只接收完整序列；Ref 由 set_project 维护 combo 显示，这里
            # 不得用 Ref 覆盖已 promote 的完整序列（U03 失败模式）。
            if not tid or not (
                hasattr(seq, "video_tracks") or hasattr(seq, "audio_tracks")
            ):
                continue
            seq_map[str(tid)] = seq
        if not seq_map:
            return
        self._settings_panel.set_timeline_sequences(seq_map)
        register_sequence = getattr(self._controller, "register_sequence", None)
        if callable(register_sequence):
            for seq in seq_map.values():
                register_sequence(seq)
        # combo 当前选择可能刚刚才能解析到完整序列（例如 Ref-only 时间线
        # 此刻被 promote），刷新 draft 序列上下文让时长/preflight 立即正确。
        self._settings_panel._on_timeline_changed(
            self._settings_panel._timeline_combo.currentIndex()
        )

    def get_export_config(self) -> Optional[Any]:
        """Return a TimelineExportConfig for the current draft."""
        jobs, _ = self._settings_panel.build_jobs(preflight=False)
        if not jobs:
            return None
        try:
            return jobs[0].to_timeline_export_config()
        except Exception:  # noqa: BLE001 - legacy API must remain defensive
            return None

    def is_exporting(self) -> bool:
        return self._controller.queue.is_running

    # ------------------------------------------------------------------
    # Internal slots
    # ------------------------------------------------------------------

    def _on_range_type_changed(self, range_type: str) -> None:
        """Keep the shared timeline's Deliver range UI in sync with the draft."""
        self._deliver_range_type = str(range_type or "entire")
        if not self._edit_panels_attached or self._shared_timeline_panel is None:
            return
        set_range_mode = getattr(
            self._shared_timeline_panel, "set_deliver_range_type", None
        )
        if callable(set_range_mode):
            set_range_mode(self._deliver_range_type)

    def _on_add_jobs(self, jobs: List[VideoExportJob]) -> None:
        if jobs:
            self._controller.add_jobs(jobs)
            # AUD-12209: enqueueing is not "started" — export_started is
            # emitted by _on_queue_running_changed when the queue runs.

    def _on_add_and_start(self, jobs: List[VideoExportJob]) -> None:
        if jobs:
            self._controller.add_jobs(jobs)
            self._controller.start_queue()
            # export_started fires from the running-state transition below.

    def _on_queue_running_changed(self, running: bool, paused: bool) -> None:
        """Emit the legacy page signals from real queue state (AUD-12209)."""
        was_running = self._queue_running
        self._queue_running = running
        if running and not self._run_active:
            # Idle → running: a fresh export run begins.
            self._run_active = True
            self._run_failed = False
            self.export_started.emit()
        elif not running and not paused and self._run_active and was_running:
            # Running → drained (not merely parked on pause): report the
            # overall outcome of the run.
            self._run_active = False
            self.export_completed.emit(not self._run_failed)

    def _on_job_finished(self, job_id: str, success: bool) -> None:
        if not success:
            self._run_failed = True

    def _on_export_cancel_requested(self, job_id: str) -> None:
        self.export_cancelled.emit()

    def _on_edit_job(self, job_id: str) -> None:
        job = self._controller.get_job(job_id)
        if job is not None:
            self._settings_panel.load_job(job)

    def _on_open_output(self, job_id: str) -> None:
        job = self._controller.get_job(job_id)
        if job is None or not job.output_path:
            return

        path = job.output_path
        if sys.platform == "darwin":
            target = path if os.path.exists(path) else os.path.dirname(path) or path
            subprocess.Popen(["open", "-R", target])  # noqa: S603
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", path])  # noqa: S603
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or path])  # noqa: S603

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sequence_choices(self, sequences: List[Any]) -> List[Tuple[str, str]]:
        choices: List[Tuple[str, str]] = []
        for sequence in sequences:
            sequence_id = (
                getattr(sequence, "timeline_id", None)
                or getattr(sequence, "sequence_id", None)
                or str(id(sequence))
            )
            name = getattr(sequence, "name", None) or I18N.tr(
                "deliver.timeline_unnamed", "Unnamed"
            )
            choices.append((str(sequence_id), str(name)))
        return choices
