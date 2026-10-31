"""Video batch export panel — the compact render queue on the Deliver page.

This panel is the right-hand queue of the Deliver workspace. It shows the
serialized list of video tasks with status / progress / actions, keeps the
header quiet, and anchors the primary render action at the bottom.

Architecture
============

* :class:`VideoQueueModel(QAbstractTableModel)` — thin adapter over the
  :class:`ExportQueue`. It does not own the queue; it observes queue
  signals and mirrors the job list. This keeps a single source of truth.
* :class:`_RenderQueueCard` — renders one Resolve-style task card with the
  source thumbnail, project/timeline, output path, status and actions.
* :class:`VideoBatchExportPanel(QWidget)` — owns the queue header, compact
  footer, card stack, empty state and wires user actions to the controller via
  signals (it has no direct queue reference).

The panel is deliberately controller-free: it emits commands as signals
(``start_requested``, ``pause_requested`` …) and reflects state through
:meth:`refresh_from_queue`. The table model remains as a compatibility/data
adapter, while the workspace / controller owns the queue and connects the two
sides.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from PyQt6.QtWidgets import QDockWidget

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QPainter, QPen, QBrush, QAction
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.features.deliver.core.export_queue import ExportQueue
from src.features.deliver.core.video_export_job import (
    VideoExportJob,
    VideoJobStatus,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C

logger = logging.getLogger(__name__)

__all__ = ["VideoBatchExportPanel", "VideoQueueModel"]


# ── Status palette (from the plan, §10.2) ──

STATUS_COLORS = {
    VideoJobStatus.PENDING: "#9AA0A6",
    VideoJobStatus.RUNNING: "#4EA1FF",
    VideoJobStatus.DONE: "#51B56A",
    VideoJobStatus.FAILED: "#E05A5A",
    VideoJobStatus.CANCELLED: "#D59A43",
    VideoJobStatus.PAUSED: "#C8A951",
    VideoJobStatus.NEEDS_RELINK: "#D59A43",
}

STATUS_GLYPH = {
    VideoJobStatus.PENDING: "○",
    VideoJobStatus.RUNNING: "●",
    VideoJobStatus.DONE: "✓",
    VideoJobStatus.FAILED: "!",
    VideoJobStatus.CANCELLED: "⊘",
    VideoJobStatus.PAUSED: "⏸",
    VideoJobStatus.NEEDS_RELINK: "?",
}

STATUS_LABEL_KEYS = {
    VideoJobStatus.PENDING: "deliver.queue.status_pending",
    VideoJobStatus.RUNNING: "deliver.queue.status_running",
    VideoJobStatus.DONE: "deliver.queue.status_done",
    VideoJobStatus.FAILED: "deliver.queue.status_failed",
    VideoJobStatus.CANCELLED: "deliver.queue.status_cancelled",
    VideoJobStatus.PAUSED: "deliver.queue.status_paused",
    VideoJobStatus.NEEDS_RELINK: "deliver.queue.status_needs_relink",
}


# ── Columns ──

COL_STATUS = 0
COL_SOURCE = 1
COL_PRESET = 2
COL_OUTPUT = 3
COL_VIDEO_SPEC = 4
COL_AUDIO = 5
COL_PROGRESS = 6
COL_STATE = 7
COL_ACTIONS = 8

COLUMN_HEADERS = [
    ("deliver.queue.col_status", "Status"),
    ("deliver.queue.col_source", "Source"),
    ("deliver.queue.col_preset", "Preset"),
    ("deliver.queue.col_output", "Output File"),
    ("deliver.queue.col_video", "Video"),
    ("deliver.queue.col_audio", "Audio"),
    ("deliver.queue.col_progress", "Progress"),
    ("deliver.queue.col_state", "State"),
    ("deliver.queue.col_actions", "Actions"),
]


# =============================================================================
# Model
# =============================================================================


class VideoQueueModel(QAbstractTableModel):
    """Table model mirroring the :class:`ExportQueue`.

    The model does not own the jobs — it snapshots the queue's job list on
    :meth:`refresh` and re-snapshots whenever a queue signal arrives. This
    avoids duplicating state while keeping the view reactive.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._jobs: List[VideoExportJob] = []

    # ── QAbstractTableModel API ──

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._jobs)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMN_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(COLUMN_HEADERS):
            key, fallback = COLUMN_HEADERS[section]
            return I18N.tr(key, fallback)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._jobs)):
            return None
        job = self._jobs[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.UserRole:
            return job  # full snapshot for the delegate

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_text(job, col)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (COL_PROGRESS, COL_STATE):
                return Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        return None

    # ── public refresh ──

    def refresh(self, jobs: List[VideoExportJob]) -> None:
        """Replace the whole snapshot and notify the view."""
        self.beginResetModel()
        self._jobs = list(jobs)
        self.endResetModel()

    def update_row(self, row: int, job: VideoExportJob) -> None:
        """Patch a single row in place (avoids resetting the whole model).

        Falls back to a full refresh if ``row`` is out of range — callers
        don't need to be defensive.
        """
        if 0 <= row < len(self._jobs):
            self._jobs[row] = job
            ix = self.index(row, 0)
            ix2 = self.index(row, self.columnCount() - 1)
            self.dataChanged.emit(ix, ix2, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.UserRole])
        else:
            self.refresh(self._jobs)

    def get_job_at(self, row: int) -> Optional[VideoExportJob]:
        if 0 <= row < len(self._jobs):
            return self._jobs[row]
        return None

    @property
    def jobs(self) -> List[VideoExportJob]:
        return list(self._jobs)

    # ── helpers ──

    def _display_text(self, job: VideoExportJob, col: int) -> str:
        if col == COL_SOURCE:
            return job.timeline_name or job.timeline_id or "—"
        if col == COL_PRESET:
            return job.preset_id or "custom"
        if col == COL_OUTPUT:
            import os
            return os.path.basename(job.output_path) or job.output_path or "—"
        if col == COL_VIDEO_SPEC:
            return self._format_video_spec(job)
        if col == COL_AUDIO:
            return self._format_audio(job)
        if col == COL_PROGRESS:
            return ""  # painted by delegate
        if col == COL_STATE:
            return I18N.tr(STATUS_LABEL_KEYS.get(job.status, "deliver.queue.status_pending"),
                           job.status.capitalize())
        # COL_STATUS / COL_ACTIONS are painted by the delegate
        return ""

    def _format_video_spec(self, job: VideoExportJob) -> str:
        vc = job.video_config
        parts = []
        if vc.resolution:
            parts.append(f"{vc.resolution[0]}×{vc.resolution[1]}")
        if vc.fps:
            parts.append(f"{vc.fps:g}fps")
        elif job.range_type:
            parts.append("orig fps")
        parts.append(vc.codec)
        return " / ".join(parts) if parts else "—"

    def _format_audio(self, job: VideoExportJob) -> str:
        ac = job.audio_config
        if not ac.include_audio:
            return I18N.tr("deliver.queue.no_audio", "no audio")
        return f"{ac.codec.upper()} {ac.bitrate}k"


# =============================================================================
# Resolve-style queue cards
# =============================================================================


class _QueueThumbnail(QFrame):
    """Small neutral filmstrip placeholder used when no preview frame exists."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(40, 36)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#19191d"))
        painter.setPen(QPen(QColor("#777982"), 1))
        painter.setBrush(QBrush(QColor("#44454d")))
        frame = self.rect().adjusted(5, 4, -5, -4)
        painter.drawRoundedRect(frame, 2, 2)
        painter.setPen(QPen(QColor("#9a9ba2"), 1))
        for y in (7, 13, 19, 25):
            painter.drawLine(7, y, 10, y)
            painter.drawLine(self.width() - 10, y, self.width() - 7, y)
        painter.setPen(QPen(QColor("#c8c9ce"), 1))
        painter.drawLine(frame.left() + 7, frame.center().y(), frame.right() - 7, frame.center().y())


class _RenderQueueCard(QFrame):
    """A compact Resolve/Deliver-style card for one queued render job."""

    edit_requested = pyqtSignal(str)
    remove_requested = pyqtSignal(str)
    retry_requested = pyqtSignal(str)
    open_requested = pyqtSignal(str)
    cancel_requested = pyqtSignal(str)

    def __init__(self, job: VideoExportJob, number: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._job = job
        self._number = number
        self.setObjectName("RenderQueueCard")
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"""
            QFrame#RenderQueueCard {{
                background-color: {C.BG_PANEL};
                border: 1px solid {C.BORDER_DARK};
                border-radius: 2px;
            }}
            QFrame#RenderQueueCard:hover {{
                border-color: {C.BORDER_MID};
            }}
            QToolButton#QueueCardAction {{
                background: transparent;
                border: none;
                color: {C.TEXT_HINT};
                font-size: 15px;
                padding: 0px;
            }}
            QToolButton#QueueCardAction:hover {{
                color: {C.TEXT_BRIGHT};
                background: {C.BG_ACTIVE};
                border-radius: 3px;
            }}
            QProgressBar#QueueCardProgress {{
                background: {C.BG_PANEL};
                border: none;
                border-radius: 2px;
                min-height: 4px;
                max-height: 4px;
            }}
            QProgressBar#QueueCardProgress::chunk {{
                background: {C.ORANGE};
                border-radius: 2px;
            }}
        """)
        self._build_ui()
        self.update_job(job, number)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(29)
        header.setStyleSheet(f"background: {C.BG_PANEL}; border-bottom: 1px solid {C.BORDER_DARK};")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(9, 0, 6, 0)
        header_layout.setSpacing(5)

        grip = QLabel("≡")
        grip.setStyleSheet(f"color: {C.TEXT_HINT}; background: transparent; font-size: 13px;")
        grip.setFixedWidth(12)
        header_layout.addWidget(grip)

        self._job_label = QLabel()
        self._job_label.setStyleSheet(f"color: {C.TEXT_BRIGHT}; background: transparent; font-size: 11px; font-weight: 600;")
        header_layout.addWidget(self._job_label)
        header_layout.addStretch()

        self._edit_button = QToolButton()
        self._edit_button.setObjectName("QueueCardAction")
        self._edit_button.setText("✎")
        self._edit_button.setToolTip(I18N.tr("deliver.queue.action_edit", "Edit Settings"))
        self._edit_button.setFixedSize(24, 24)
        self._edit_button.clicked.connect(self._on_edit)
        header_layout.addWidget(self._edit_button)

        self._remove_button = QToolButton()
        self._remove_button.setObjectName("QueueCardAction")
        self._remove_button.setText("×")
        self._remove_button.setToolTip(I18N.tr("deliver.queue.action_remove", "Remove"))
        self._remove_button.setFixedSize(24, 24)
        self._remove_button.clicked.connect(self._on_remove)
        header_layout.addWidget(self._remove_button)
        root.addWidget(header)

        body = QWidget()
        body.setObjectName("QueueCardBody")
        body.setStyleSheet(f"background: {C.BG_PANEL};")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(10, 8, 10, 8)
        body_layout.setSpacing(10)

        body_layout.addWidget(_QueueThumbnail())

        details = QVBoxLayout()
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(2)
        self._title_label = QLabel()
        self._title_label.setStyleSheet(f"color: {C.TEXT_PRIMARY}; background: transparent; font-size: 11px; font-weight: 600;")
        self._title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details.addWidget(self._title_label)
        self._path_label = QLabel()
        self._path_label.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; font-size: 10px;")
        self._path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details.addWidget(self._path_label)
        self._spec_label = QLabel()
        self._spec_label.setStyleSheet(f"color: {C.TEXT_HINT}; background: transparent; font-size: 9px;")
        self._spec_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details.addWidget(self._spec_label)
        self._progress = QProgressBar()
        self._progress.setObjectName("QueueCardProgress")
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(False)
        details.addWidget(self._progress)
        body_layout.addLayout(details, 1)

        status = QVBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(2)
        status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._status_label = QLabel()
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._status_label.setMinimumWidth(62)
        self._status_label.setStyleSheet(f"font-size: 10px; color: {C.TEXT_DIM}; background: transparent;")
        status.addWidget(self._status_label)
        self._percent_label = QLabel()
        self._percent_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._percent_label.setStyleSheet(f"font-size: 9px; color: {C.TEXT_HINT}; background: transparent;")
        status.addWidget(self._percent_label)
        self._fps_label = QLabel()
        self._fps_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._fps_label.setStyleSheet(f"font-size: 9px; color: {C.TEXT_HINT}; background: transparent;")
        status.addWidget(self._fps_label)
        body_layout.addLayout(status)
        root.addWidget(body)

    def update_job(self, job: VideoExportJob, number: int) -> None:
        self._job = job
        self._number = number
        self._job_label.setText(f"{I18N.tr('deliver.queue.job', 'Job')} {number}")

        timeline = job.timeline_name or job.name or job.timeline_id or I18N.tr("deliver.queue.untitled", "Untitled timeline")
        project = getattr(job, "project_name", "") or job.project_id
        title = f"{project} | {timeline}" if project else timeline
        self._title_label.setText(self._elide(title, self._title_label))
        self._title_label.setToolTip(title)
        output = job.output_path or job.name or "—"
        self._path_label.setText(self._elide(output, self._path_label))
        self._path_label.setToolTip(output)

        config = job.video_config
        spec = [str(config.container or "video").upper(), str(config.codec or "codec").upper()]
        if config.resolution:
            spec.append(f"{config.resolution[0]}×{config.resolution[1]}")
        if config.fps:
            spec.append(f"{config.fps:g} fps")
        self._spec_label.setText(" · ".join(spec))

        status_color = STATUS_COLORS.get(job.status, C.TEXT_DIM)
        status_text = I18N.tr(
            STATUS_LABEL_KEYS.get(job.status, "deliver.queue.status_pending"),
            job.status.replace("_", " ").capitalize(),
        )
        self._status_label.setText(f"● {status_text}")
        self._status_label.setStyleSheet(
            f"font-size: 10px; color: {status_color}; background: transparent;"
        )
        progress = max(0.0, min(1.0, float(job.progress)))
        self._progress.setValue(round(progress * 100))
        self._progress.setVisible(job.status in (VideoJobStatus.RUNNING, VideoJobStatus.DONE) or progress > 0)
        self._percent_label.setText(f"{round(progress * 100)}%" if self._progress.isVisible() else "")
        fps = max(0.0, float(getattr(job, "export_fps", 0.0)))
        self._fps_label.setText(
            f"{fps:.1f} fps"
            if fps > 0.0 and job.status in (VideoJobStatus.RUNNING, VideoJobStatus.DONE)
            else ""
        )

        self._edit_button.setVisible(
            job.status in (
                VideoJobStatus.PENDING,
                VideoJobStatus.PAUSED,
                VideoJobStatus.NEEDS_RELINK,
                VideoJobStatus.FAILED,
                VideoJobStatus.CANCELLED,
                VideoJobStatus.DONE,
            )
        )
        if job.status in (VideoJobStatus.FAILED, VideoJobStatus.CANCELLED):
            self._edit_button.setText("↻")
            self._edit_button.setToolTip(I18N.tr("deliver.queue.action_retry", "Retry"))
        elif job.status == VideoJobStatus.DONE:
            self._edit_button.setText("↗")
            self._edit_button.setToolTip(I18N.tr("deliver.queue.action_open", "Open Output"))
        else:
            self._edit_button.setText("✎")
            self._edit_button.setToolTip(I18N.tr("deliver.queue.action_edit", "Edit Settings"))
        self._remove_button.setVisible(True)
        self._remove_button.setToolTip(
            I18N.tr("deliver.queue.action_cancel", "Cancel") if job.is_running
            else I18N.tr("deliver.queue.action_remove", "Remove")
        )

    def _elide(self, text: str, label: QLabel) -> str:
        width = max(80, label.width())
        return label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, width)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.update_job(self._job, self._number)

    def _on_edit(self) -> None:
        if self._job.status == VideoJobStatus.DONE:
            self.open_requested.emit(self._job.job_id)
        elif self._job.status in (VideoJobStatus.FAILED, VideoJobStatus.CANCELLED):
            self.retry_requested.emit(self._job.job_id)
        else:
            self.edit_requested.emit(self._job.job_id)

    def _on_remove(self) -> None:
        if self._job.is_running:
            self.cancel_requested.emit(self._job.job_id)
        else:
            self.remove_requested.emit(self._job.job_id)


# =============================================================================
# Panel
# =============================================================================


class VideoBatchExportPanel(QWidget):
    """Lower Deliver panel: the video render queue.

    Signals (commands the controller handles):
        start_requested(): start the queue (process pending jobs).
        pause_requested(): pause the current frame loop and the queue.
        resume_requested(): resume the current frame loop and queue.
        cancel_current_requested(): cooperatively cancel the running job.
        clear_completed_requested(): remove done/failed/cancelled jobs.
        retry_requested(str): retry a specific job_id.
        remove_requested(str): remove a specific job_id.
        open_output_requested(str): open the output of a done job_id.
        edit_job_requested(str): load a job back into the settings panel.
    """

    start_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    cancel_current_requested = pyqtSignal()
    clear_completed_requested = pyqtSignal()
    retry_requested = pyqtSignal(str)
    remove_requested = pyqtSignal(str)
    open_output_requested = pyqtSignal(str)
    edit_job_requested = pyqtSignal(str)
    detach_requested = pyqtSignal()  # ask workspace to float the panel
    return_to_deliver_requested = pyqtSignal()  # return below Deliver settings

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("VideoBatchExportPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"QWidget#VideoBatchExportPanel {{ background: {C.BG_PANEL}; }}")

        self._setup_ui()

    # =========================================================================
    # UI
    # =========================================================================

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_toolbar())
        # Keep the table model as the queue snapshot/API used by the
        # controller and tests. The visible surface is intentionally a
        # vertical card stack: this is the hierarchy users expect from a
        # Deliver render queue and leaves the technical columns out of sight.
        self._model = VideoQueueModel(self)
        self._cards: dict[str, _RenderQueueCard] = {}
        self._queue: Optional[ExportQueue] = None

        self._view = QScrollArea()
        self._view.setObjectName("VideoQueueView")
        self._view.setWidgetResizable(True)
        self._view.setFrameShape(QFrame.Shape.NoFrame)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._view.setStyleSheet(f"""
            QScrollArea#VideoQueueView {{
                background: {C.BG_PANEL};
                border: none;
            }}
            QScrollBar:vertical {{
                background: {C.BG_PANEL};
                width: 8px;
                margin: 2px 0;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BG_BUTTON};
                min-height: 24px;
                border-radius: 4px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)
        self._cards_host = QWidget()
        self._cards_host.setObjectName("VideoQueueCards")
        self._cards_host.setStyleSheet(f"background: {C.BG_PANEL};")
        self._cards_layout = QVBoxLayout(self._cards_host)
        self._cards_layout.setContentsMargins(10, 10, 10, 10)
        self._cards_layout.setSpacing(8)
        self._cards_layout.addStretch(1)
        self._view.setWidget(self._cards_host)
        layout.addWidget(self._view, 1)

        # Empty state overlay (shown when the model is empty)
        self._empty_state = self._build_empty_state()
        self._empty_state.setVisible(True)
        self._view.setVisible(False)
        layout.insertWidget(layout.count() - 1, self._empty_state, 1)

        # DaVinci-style queue footer: the queue itself stays visually quiet,
        # while the one primary action is anchored to the bottom edge.
        layout.addWidget(self._build_footer())

        # The first density pass happens before the view exists because the
        # toolbar is built first. Run it again after all queue widgets exist.
        self._update_toolbar_density()

    def resizeEvent(self, event) -> None:
        """Use compact queue actions when the right Deliver column is narrow."""
        super().resizeEvent(event)
        self._update_toolbar_density()

    def _update_toolbar_density(self) -> None:
        # Keep one stable Resolve-like header at every width. The cards
        # reflow naturally, so no technical table columns are exposed here.
        self._start_btn.setText(I18N.tr("deliver.queue.render_all", "Render All"))
        self._more_btn.setText("⋯")
        self._more_btn.setToolTip(I18N.tr("deliver.queue.more", "More"))
        self._more_btn.setFixedWidth(34)
        self._summary_label.setVisible(False)

    def _apply_queue_density(self) -> None:
        """Compatibility hook retained for callers from the former table UI."""
        return

    def _build_toolbar(self) -> QWidget:
        host = QWidget()
        host.setObjectName("VideoQueueToolbar")
        host.setStyleSheet(f"""
            QWidget#VideoQueueToolbar {{
                background-color: {C.BG_PANEL};
                border-bottom: 1px solid {C.BORDER_DARK};
            }}
            QToolButton#QueueMoreButton {{
                background: transparent;
                border: none;
                color: {C.TEXT_HINT};
                font-size: 18px;
                padding: 0px;
            }}
            QToolButton#QueueMoreButton:hover {{
                background: {C.BG_BUTTON};
                color: {C.TEXT_BRIGHT};
                border-radius: 3px;
            }}
            QToolButton#QueueMoreButton::menu-indicator {{
                image: none;
            }}
            QLabel {{
                background: transparent;
            }}
        """)
        layout = QHBoxLayout(host)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)

        title = QLabel(I18N.tr("deliver.queue.title", "Render Queue"))
        title.setObjectName("VideoQueueTitle")
        title.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        title.setStyleSheet(
            f"color: {C.TEXT_BRIGHT}; background: transparent; font-size: 12px; font-weight: 600;"
        )
        layout.addWidget(title)

        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; font-size: 11px;"
        )
        layout.addWidget(self._summary_label)

        layout.addStretch()

        self._start_btn = QPushButton(I18N.tr("deliver.queue.start", "Start Queue"))
        self._start_btn.setObjectName("PrimaryButton")
        self._start_btn.clicked.connect(self.start_requested.emit)
        self._pause_btn = QPushButton(I18N.tr("deliver.queue.pause", "Pause"))
        self._pause_btn.clicked.connect(self.pause_requested.emit)
        self._pause_btn.setParent(self)
        self._pause_btn.setVisible(False)

        self._resume_btn = QPushButton(I18N.tr("deliver.queue.resume", "Resume"))
        self._resume_btn.clicked.connect(self.resume_requested.emit)
        self._resume_btn.setParent(self)
        self._resume_btn.setVisible(False)

        self._cancel_btn = QPushButton(I18N.tr("deliver.queue.cancel_current", "Cancel Current"))
        self._cancel_btn.clicked.connect(self.cancel_current_requested.emit)
        self._cancel_btn.setParent(self)
        self._cancel_btn.setVisible(False)

        self._clear_btn = QPushButton(I18N.tr("deliver.queue.clear_completed", "Clear Completed"))
        self._clear_btn.clicked.connect(self.clear_completed_requested.emit)
        self._clear_btn.setParent(self)
        self._clear_btn.setVisible(False)

        # More menu (float, retry-all, etc.)
        self._more_btn = QToolButton()
        self._more_btn.setObjectName("QueueMoreButton")
        self._more_btn.setText("⋯")
        self._more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(self._more_btn)
        self._pause_action = QAction(I18N.tr("deliver.queue.pause", "Pause"), self)
        self._pause_action.triggered.connect(self.pause_requested.emit)
        more_menu.addAction(self._pause_action)
        self._resume_action = QAction(I18N.tr("deliver.queue.resume", "Resume"), self)
        self._resume_action.triggered.connect(self.resume_requested.emit)
        more_menu.addAction(self._resume_action)
        self._cancel_action = QAction(
            I18N.tr("deliver.queue.cancel_current", "Cancel Current"), self
        )
        self._cancel_action.triggered.connect(self.cancel_current_requested.emit)
        more_menu.addAction(self._cancel_action)
        self._clear_action = QAction(
            I18N.tr("deliver.queue.clear_completed", "Clear Completed"), self
        )
        self._clear_action.triggered.connect(self.clear_completed_requested.emit)
        more_menu.addAction(self._clear_action)
        more_menu.addSeparator()
        act_float = QAction(I18N.tr("deliver.queue.float", "Float Queue Panel"), self)
        act_float.triggered.connect(self.detach_requested.emit)
        more_menu.addAction(act_float)
        act_return = QAction(I18N.tr("deliver.queue.return_to_deliver", "Return to Deliver"), self)
        act_return.triggered.connect(self.return_to_deliver_requested.emit)
        more_menu.addAction(act_return)
        self._more_btn.setMenu(more_menu)
        layout.addWidget(self._more_btn)

        self._toolbar_compact = False
        self._update_toolbar_density()

        return host

    def _build_footer(self) -> QWidget:
        """Build the quiet bottom action strip used by the embedded queue."""
        host = QWidget()
        host.setObjectName("VideoQueueFooter")
        host.setStyleSheet(f"""
            QWidget#VideoQueueFooter {{
                background-color: {C.BG_PANEL};
                border-top: 1px solid {C.BORDER_DARK};
            }}
            QPushButton {{
                background-color: {C.ORANGE_PRESS};
                border: 1px solid {C.ORANGE_PRESS};
                border-radius: 3px;
                color: white;
                padding: 5px 14px;
                min-height: 26px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background-color: {C.ORANGE_HOVER}; }}
            QPushButton:disabled {{
                background-color: {C.BG_BUTTON};
                border-color: {C.BORDER_MID};
                color: {C.TEXT_DIM};
            }}
        """)
        layout = QHBoxLayout(host)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(8)

        self._footer_hint = QLabel(I18N.tr("deliver.queue.footer_hint", "Jobs render in order"))
        self._footer_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; font-size: 10px;")
        layout.addWidget(self._footer_hint)
        layout.addStretch()

        self._start_btn.setMinimumWidth(106)
        layout.addWidget(self._start_btn)
        return host

    def _build_empty_state(self) -> QWidget:
        host = QWidget()
        host.setObjectName("VideoQueueEmptyState")
        host.setStyleSheet(f"background: {C.BG_PANEL};")
        v = QVBoxLayout(host)
        v.setContentsMargins(40, 40, 40, 40)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.setSpacing(6)

        line1 = QLabel(I18N.tr("deliver.queue.empty_title", "No video export tasks yet"))
        line1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line1.setStyleSheet("color: #6D7177; font-size: 14px; font-weight: 600;")
        v.addWidget(line1)

        line2 = QLabel(
            I18N.tr("deliver.queue.empty_hint",
                    "Choose a timeline and a video preset above, then click “Add to Video Queue”.")
        )
        line2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line2.setWordWrap(True)
        line2.setStyleSheet("color: #4D525A; font-size: 12px;")
        v.addWidget(line2)
        return host

    # =========================================================================
    # Public refresh (called by the controller)
    # =========================================================================

    def refresh_from_queue(self, queue: ExportQueue) -> None:
        """Rebuild the model from the queue's current job list."""
        self._queue = queue
        jobs = [VideoExportJob.from_legacy_job(j) for j in queue.jobs]
        self._model.refresh(jobs)
        self._rebuild_cards(jobs)
        self._update_summary(queue, jobs)
        self._empty_state.setVisible(len(jobs) == 0)
        self._view.setVisible(len(jobs) > 0)

    def _rebuild_cards(self, jobs: List[VideoExportJob]) -> None:
        """Recreate the small visible card stack from the model snapshot."""
        for card in self._cards.values():
            card.deleteLater()
        self._cards.clear()

        # The final stretch is kept at the end so cards stay top-aligned.
        while self._cards_layout.count() > 0:
            item = self._cards_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for number, job in enumerate(jobs, start=1):
            card = _RenderQueueCard(job, number, self._cards_host)
            card.edit_requested.connect(self.edit_job_requested.emit)
            card.remove_requested.connect(self.remove_requested.emit)
            card.retry_requested.connect(self.retry_requested.emit)
            card.open_requested.connect(self.open_output_requested.emit)
            card.cancel_requested.connect(lambda _job_id: self.cancel_current_requested.emit())
            self._cards[job.job_id] = card
            self._cards_layout.addWidget(card)
        self._cards_layout.addStretch(1)

    def _refresh_card(self, job: VideoExportJob) -> None:
        card = self._cards.get(job.job_id)
        if card is None:
            return
        number = next(
            (index + 1 for index, visible_job in enumerate(self._model.jobs)
             if visible_job.job_id == job.job_id),
            1,
        )
        card.update_job(job, number)

    def update_job_row(self, job_id: str) -> None:
        """Patch a single job row after a progress/status update.

        The controller sends progress as a lightweight signal, so read the
        current live job once and patch both the compatibility model and its
        card.
        """
        if self._queue is None:
            return
        legacy = self._queue.get_job(job_id)
        if legacy is None:
            return
        job = VideoExportJob.from_legacy_job(legacy)
        for i, visible_job in enumerate(self._model.jobs):
            if visible_job.job_id == job_id:
                self._model.update_row(i, job)
                self._refresh_card(job)
                return

    # =========================================================================
    # Toolbar state
    # =========================================================================

    def set_queue_running(self, running: bool, paused: bool = False) -> None:
        self._start_btn.setEnabled(not running and not paused)
        # These compatibility buttons remain hidden; their commands live in
        # More so the compact queue never grows a second toolbar row.
        self._pause_btn.setVisible(False)
        self._resume_btn.setVisible(False)
        self._cancel_btn.setVisible(False)
        self._pause_action.setEnabled(running and not paused)
        self._resume_action.setEnabled(paused)
        self._cancel_action.setEnabled(running)

    # =========================================================================
    # Internal slots
    # =========================================================================

    def _update_summary(self, queue: ExportQueue, jobs: List[VideoExportJob]) -> None:
        total = len(jobs)
        running = sum(1 for j in jobs if j.status == VideoJobStatus.RUNNING)
        pending = sum(1 for j in jobs if j.status == VideoJobStatus.PENDING)
        done = sum(1 for j in jobs if j.status == VideoJobStatus.DONE)
        failed = sum(1 for j in jobs if j.status == VideoJobStatus.FAILED)

        parts = [f"{total} {I18N.tr('deliver.queue.tasks', 'tasks')}"]
        if running:
            parts.append(f"{running} {I18N.tr('deliver.queue.running', 'rendering')}")
        if pending:
            parts.append(f"{pending} {I18N.tr('deliver.queue.pending', 'pending')}")
        if done:
            parts.append(f"{done} {I18N.tr('deliver.queue.done', 'done')}")
        if failed:
            parts.append(f"{failed} {I18N.tr('deliver.queue.failed', 'failed')}")
        summary = " · ".join(parts)
        self._summary_label.setText(summary)
        self._footer_hint.setText(
            summary if total else I18N.tr("deliver.queue.footer_hint", "Jobs render in order")
        )
        if not queue.is_running:
            self._start_btn.setEnabled(total > 0)

    # AUD-12300: the item-view-era slots ``_on_context_menu`` and
    # ``_on_double_clicked`` were removed. They were never connected after
    # the queue migrated from QTableView to the card stack, and
    # ``_on_context_menu`` called ``QScrollArea.indexAt()`` (a
    # QAbstractItemView-only API) so any future wiring would have crashed
    # with AttributeError. Their capabilities (edit / retry / open output /
    # remove / cancel) are covered by the per-card buttons in
    # ``_RenderQueueCard.update_job`` and the More menu actions.

    # =========================================================================
    # Dock helpers — the panel lives inside a QDockWidget; these let the
    # "More" menu float / re-dock it without a separate host controller.
    # =========================================================================

    def _find_enclosing_dock(self) -> Optional[QDockWidget]:
        """Walk up the parent chain to find the QDockWidget that hosts us."""
        from PyQt6.QtWidgets import QDockWidget
        node = self.parent()
        for _ in range(6):
            if node is None:
                return None
            if isinstance(node, QDockWidget):
                return node
            node = node.parent()
        return None

    def _toggle_dock_floating(self) -> None:
        """Float the hosting dock (or re-dock it if already floating)."""
        dock = self._find_enclosing_dock()
        if dock is not None:
            dock.setFloating(not dock.isFloating())

    def _dock_back_to_main_window(self) -> None:
        """Re-dock the panel into the main window (exit floating state)."""
        dock = self._find_enclosing_dock()
        if dock is not None:
            dock.setFloating(False)
