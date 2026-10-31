"""Qt UI for the Topos Decode Speed Test plugin."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Iterable, Optional

from PyQt6.QtCore import QMimeData, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QColor
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Reuse the same controls as the Composite Properties panel.  In particular,
# ``ChevronComboBox`` keeps the macOS popup/chevron rendering consistent with
# the rest of the application while retaining the normal QComboBox API.  The
# path bootstrap keeps a standalone ``external/file_explorer/main.py`` launch
# working, where the project root is not necessarily on ``sys.path`` yet.
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if (_PROJECT_ROOT / "src").is_dir() and str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from src.shared.ui.widgets import ChevronComboBox
from src.shared.ui.widgets.pref_drag_value import PrefDragValue

from .benchmark import (
    BenchmarkCancelled,
    BenchmarkConfig,
    BenchmarkResult,
    DecodeBenchmarkRunner,
    SUPPORTED_CODECS,
    export_results_csv,
    export_results_json,
)


STYLESHEET = """
/*
 * Topos Color palette, expressed with macOS-style flat surfaces.
 *
 * Every neutral surface below keeps the #27272b hue/saturation (240° / 4.9%)
 * and only changes lightness.  That keeps the panel coherent while making
 * the header, controls, content and separators readable at a glance.
 */
QMainWindow, QWidget {
    /* Match the Topos Properties/panel surface (DARK_THEME QWidget). */
    background: #27272b;
    color: #e0e0e0;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif;
    font-size: 11px;
}
QLabel { background: transparent; }
QFrame#Header {
    background: #27272b;
    border-bottom: none;
}
QLabel#Title { color: #f5f5f7; font-size: 18px; font-weight: 600; }
QLabel#Subtitle, QLabel#Muted { color: #888888; font-size: 11px; }
QLabel#Metric {
    color: #ff8800;
    font-family: "SF Mono", Menlo, monospace;
    font-size: 18px;
    font-weight: 700;
}
QGroupBox {
    background: transparent;
    border: none;
    border-radius: 0px;
    margin-top: 9px;
    padding: 8px 0px 6px 0px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: #b6b6bb;
    background: transparent;
    font-size: 12px;
}
QPushButton {
    /* Raised control: +1.5 lightness steps from the panel surface. */
    background: #2a2a2f;
    color: #f2f2f7;
    border: 1px solid #000000;
    border-radius: 6px;
    min-height: 24px;
    padding: 3px 8px;
}
QPushButton:hover { background: #2e2e33; border-color: #242428; }
QPushButton:pressed { background: #242428; }
QPushButton:focus { border: 1px solid #ff8800; }
QPushButton:disabled { color: #666666; background: #242428; border-color: #000000; }
QPushButton#Start {
    background: #ff8800;
    color: #ffffff;
    border-color: #ff8800;
    font-weight: 700;
    min-width: 88px;
}
QPushButton#Start:hover { background: #ffaa44; border-color: #ffaa44; }
QPushButton#Start:pressed { background: #cc6600; border-color: #cc6600; }
QPushButton#Stop { color: #ff6961; border-color: #713a3b; }
QPushButton#Stop:hover { background: #2e2e33; border-color: #a14b4a; }
/*
 * Input controls use the same compact surfaces as Composite Properties.
 * Combos additionally use ChevronComboBox below so the macOS chevron and
 * popup list are rendered by the shared Topos control implementation.
 */
QSpinBox {
    background-color: #27272b;
    color: #e0e0e0;
    border: 1px solid #000000;
    border-radius: 4px;
    min-height: 20px;
    padding: 2px 6px;
    font-size: 11px;
    selection-background-color: #ff8800;
}
QSpinBox:hover { border-color: #242428; background-color: #2e2e33; }
QSpinBox:focus { border-color: #ff8800; }
QSpinBox::up-button, QSpinBox::down-button {
    width: 0px;
    height: 0px;
    border: none;
}
QListWidget, QTableWidget {
    /* Content well: just below the panel surface, without a boxed panel. */
    background: #262629;
    alternate-background-color: #262629;
    border: 1px solid #000000;
    gridline-color: #000000;
    border-radius: 6px;
    outline: none;
}
QListWidget::item { padding: 3px 6px; border-radius: 3px; }
QListWidget::item:hover { background: #2e2e33; }
QListWidget::item:selected { background: rgba(255, 136, 0, 0.20); color: #f5f5f7; }
QListWidget#SourceList[dropActive="true"] {
    background: #2e2e33;
    border-color: #ff8800;
}
/* QTableWidget paints the last-column gridline at the same x as its outer
 * frame.  Drop only that outer edge so every visible table rule stays 1px. */
QTableWidget { border-right: none; }
QHeaderView::section {
    /* Header layer: +3 lightness steps from the #27272b base. */
    background: #2e2e33;
    color: #b6b6bb;
    border: none;
    border-right: 1px solid #000000;
    border-bottom: 1px solid #000000;
    padding: 5px 6px;
    font-size: 11px;
    font-weight: 600;
}
QTableWidget::item { padding: 3px 5px; }
QTableWidget::item:selected { background: rgba(255, 136, 0, 0.18); color: #f5f5f7; }
QTableWidget#Matrix::item { padding: 1px 6px; }
/* Keep the splitter separator visually identical to the 1px table rules. */
QSplitter::handle {
    background: #000000;
    height: 1px;
    min-height: 1px;
    max-height: 1px;
}
QSplitter::handle:vertical {
    height: 1px;
    min-height: 1px;
    max-height: 1px;
}
QScrollBar:vertical, QScrollBar:horizontal {
    background: transparent;
    border: none;
    margin: 0px;
}
QScrollBar:vertical { width: 7px; }
QScrollBar:horizontal { height: 7px; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #313135;
    border: none;
    border-radius: 3px;
    min-height: 22px;
    min-width: 22px;
}
QScrollBar::handle:hover { background: #35353b; }
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
    border: none;
    width: 0px;
    height: 0px;
}
QTableCornerButton::section { background: #2e2e33; border: none; }
QProgressBar {
    background: #262629;
    border: 1px solid #000000;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    color: #b6b6bb;
}
QProgressBar::chunk { background: #ff8800; border-radius: 4px; }
QToolTip { background: #2e2e33; color: #f2f2f7; border: 1px solid #000000; padding: 5px 7px; }
"""

# QAbstractScrollArea owns the table viewport separately from the table frame.
# Give that viewport the single right rule after removing the duplicate outer
# table edge, so both empty and populated tables have the same 1px boundary.
_TABLE_VIEWPORT_STYLE = "background: #262629; border-right: 1px solid #000000;"


class VideoDropList(QListWidget):
    """List widget accepting local video files dragged from Finder."""

    files_dropped = pyqtSignal(list)

    @staticmethod
    def _local_paths(mime_data: QMimeData) -> list[str]:
        """Return local filesystem paths represented by a drag payload."""

        if not mime_data.hasUrls():
            return []
        paths: list[str] = []
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if path:
                paths.append(path)
        return paths

    def _set_drop_active(self, active: bool) -> None:
        self.setProperty("dropActive", bool(active))
        # Dynamic QSS properties are re-evaluated after a polish cycle.
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._local_paths(event.mimeData()):
            self._set_drop_active(True)
            event.acceptProposedAction()
            return
        self._set_drop_active(False)
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._local_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._set_drop_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._set_drop_active(False)
        paths = self._local_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        self.files_dropped.emit(paths)
        event.acceptProposedAction()


class BenchmarkWorker(QThread):
    """Run media decoding off the Qt UI thread."""

    progress = pyqtSignal(str, int, int)
    result_ready = pyqtSignal(object)
    completed = pyqtSignal(object)
    cancelled = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, paths: Iterable[str], config: BenchmarkConfig) -> None:
        super().__init__()
        self._paths = tuple(paths)
        self._config = config
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation; the decoder checks this per frame."""

        self._cancel_event.set()

    def run(self) -> None:  # noqa: D401 - Qt override
        runner = DecodeBenchmarkRunner(self._cancel_event)
        results: list[BenchmarkResult] = []
        try:
            results = runner.run_many(
                self._paths,
                self._config,
                progress=lambda path, done, total: self.progress.emit(path, done, total),
                result_callback=lambda result: self.result_ready.emit(result),
            )
        except BenchmarkCancelled:
            self.cancelled.emit()
            return
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.completed.emit(results)


class DecodeBenchmarkWindow(QMainWindow):
    """Blackmagic-inspired benchmark panel embedded in file explorer."""

    MATRIX_ROWS = [label for label, _aliases in SUPPORTED_CODECS]
    MATRIX_COLUMNS = ["2K", "4K", "6K", "8K"]
    RESULT_COLUMNS = [
        "素材", "编码", "分辨率", "格式", "模式", "帧数", "FPS",
        "平均 ms", "P95 ms", "解码器", "实际 decode path", "状态",
    ]
    # Keep the default table focused on the comparison metrics.  All columns
    # remain available through the existing right-click visibility menu.
    DEFAULT_HIDDEN_RESULT_COLUMNS = frozenset({"格式", "帧数", "解码器", "状态"})

    def __init__(self, paths: Optional[Iterable[str]] = None, host_bridge: object = None) -> None:
        super().__init__()
        self.setWindowTitle("Topos Decode Speed Test")
        # The compact default keeps the result rows and capability matrix
        # visible without a wide empty margin on a typical laptop screen.
        self.resize(1120, 720)
        self.setMinimumSize(980, 600)
        self.setStyleSheet(STYLESHEET)
        self._paths: list[str] = []
        self._results: list[BenchmarkResult] = []
        # The capability matrix is result-driven: families are inserted only
        # after a completed measurement, in the canonical codec order below.
        self._matrix_families: list[str] = []
        self._worker: Optional[BenchmarkWorker] = None
        self._host_bridge = host_bridge
        self._edit_timer = QTimer(self)
        self._edit_timer.setInterval(100)
        self._edit_timer.timeout.connect(self._poll_edit_session)
        self._edit_row: Optional[int] = None
        self._edit_session_active = False
        self._edit_last_snapshot: Optional[dict[str, object]] = None
        self._build_ui()
        if paths:
            self.add_paths(paths)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame(objectName="Header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 8, 14, 8)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("Topos Decode Speed Test", objectName="Title")
        subtitle = QLabel(
            "支持 SourceManager 离线解码，以及当前 Edit 时间线的真实解码 / GPU 合成 / 呈现遥测",
            objectName="Subtitle",
        )
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box)
        header_layout.addStretch()
        metric_box = QVBoxLayout()
        self._headline_fps = QLabel("— FPS", objectName="Metric")
        self._headline_latency = QLabel("— ms / frame", objectName="Muted")
        metric_box.addWidget(self._headline_fps, alignment=Qt.AlignmentFlag.AlignRight)
        metric_box.addWidget(self._headline_latency, alignment=Qt.AlignmentFlag.AlignRight)
        header_layout.addLayout(metric_box)
        header_layout.addSpacing(20)
        self._status_label = QLabel("准备就绪", objectName="Muted")
        header_layout.addWidget(self._status_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(header)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(12, 6, 12, 5)
        controls_layout.setSpacing(6)

        source_group = QGroupBox("测试素材")
        source_layout = QVBoxLayout(source_group)
        source_layout.setContentsMargins(7, 6, 7, 6)
        source_layout.setSpacing(5)
        source_content = QHBoxLayout()
        source_content.setContentsMargins(0, 0, 0, 0)
        source_content.setSpacing(6)
        self._source_list = VideoDropList()
        self._source_list.setObjectName("SourceList")
        self._source_list.setAcceptDrops(True)
        self._source_list.setToolTip("可从 Finder 直接拖入视频文件")
        self._source_list.files_dropped.connect(self.add_paths)
        self._source_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._source_list.setUniformItemSizes(True)
        self._source_list.setMinimumWidth(330)
        self._source_list.setMinimumHeight(68)
        self._source_list.setMaximumHeight(112)
        source_content.addWidget(self._source_list, 1)
        source_buttons = QVBoxLayout()
        source_buttons.setContentsMargins(0, 0, 0, 0)
        source_buttons.setSpacing(5)
        add_button = QPushButton("添加视频…")
        add_button.setFixedWidth(92)
        add_button.clicked.connect(self._choose_files)
        remove_button = QPushButton("移除选中")
        remove_button.setFixedWidth(92)
        remove_button.clicked.connect(self._remove_selected)
        source_buttons.addWidget(add_button)
        source_buttons.addWidget(remove_button)
        source_buttons.addStretch()
        source_content.addLayout(source_buttons)
        source_layout.addLayout(source_content, 1)
        controls_layout.addWidget(source_group, 2)

        options_group = QGroupBox("测试设置")
        options_layout = QGridLayout(options_group)
        options_layout.setContentsMargins(7, 6, 7, 5)
        options_layout.setHorizontalSpacing(6)
        options_layout.setVerticalSpacing(5)
        options_layout.setColumnMinimumWidth(0, 56)
        options_layout.setColumnStretch(1, 1)

        mode_label = QLabel("解码模式")
        options_layout.addWidget(mode_label, 0, 0)
        self._mode_combo = ChevronComboBox()
        # Keep the full SourceManager mode label visible on compact windows.
        self._mode_combo.setMinimumWidth(270)
        self._mode_combo.setToolTip("SourceManager 离线代理（1920×1080）")
        self._mode_combo.addItem("SourceManager 代理（1920×1080）", "proxy")
        self._mode_combo.addItem("Master（原分辨率）", "master")
        self._mode_combo.addItem("当前 Edit 实时管线（宿主遥测）", "edit_realtime")
        options_layout.addWidget(self._mode_combo, 0, 1)

        pattern_label = QLabel("寻址模式")
        self._pattern_combo = ChevronComboBox()
        self._pattern_combo.setMinimumWidth(270)
        self._pattern_combo.addItem("顺序播放", "sequential")
        self._pattern_combo.addItem("随机 Seek", "random")
        options_layout.addWidget(pattern_label, 1, 0)
        options_layout.addWidget(self._pattern_combo, 1, 1)

        frame_label = QLabel("测试帧数")
        self._frame_spin = PrefDragValue(1, 10000, 120)
        self._frame_spin.setFixedWidth(70)
        self._frame_spin.setToolTip("正式测试阶段读取的帧数")
        self._frame_spin.setRange(1, 10000)
        self._frame_spin.setValue(120)
        options_layout.addWidget(frame_label, 2, 0)
        options_layout.addWidget(
            self._frame_spin, 2, 1, Qt.AlignmentFlag.AlignLeft)

        warmup_label = QLabel("预热帧数")
        self._warmup_spin = PrefDragValue(0, 240, 8)
        self._warmup_spin.setFixedWidth(70)
        self._warmup_spin.setToolTip("正式计时前用于建立解码器状态的帧数")
        self._warmup_spin.setRange(0, 240)
        self._warmup_spin.setValue(8)
        options_layout.addWidget(warmup_label, 3, 0)
        options_layout.addWidget(
            self._warmup_spin, 3, 1, Qt.AlignmentFlag.AlignLeft)
        controls_layout.addWidget(options_group, 2)

        actions = QVBoxLayout()
        actions.setContentsMargins(0, 22, 0, 0)
        actions.setSpacing(5)
        self._start_button = QPushButton("开始测试")
        self._start_button.setObjectName("Start")
        self._start_button.clicked.connect(self._start_benchmark)
        actions.addWidget(self._start_button)
        self._stop_button = QPushButton("停止")
        self._stop_button.setObjectName("Stop")
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._stop_benchmark)
        actions.addWidget(self._stop_button)
        self._export_button = QPushButton("导出结果…")
        self._export_button.setEnabled(False)
        self._export_button.clicked.connect(self._export_results)
        actions.addWidget(self._export_button)
        controls_layout.addLayout(actions)
        root.addWidget(controls)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        # Match the 1px black table rules; the handle remains draggable but
        # no longer creates a visually heavier band between the two sections.
        splitter.setHandleWidth(1)
        results_group = QGroupBox("素材测试结果")
        results_layout = QVBoxLayout(results_group)
        results_layout.setContentsMargins(7, 6, 7, 7)
        results_layout.setSpacing(5)
        self._results_table = QTableWidget(0, 12)
        self._results_table.setHorizontalHeaderLabels(self.RESULT_COLUMNS)
        self._results_table.viewport().setStyleSheet(_TABLE_VIEWPORT_STYLE)
        self._results_table.setAlternatingRowColors(True)
        self._results_table.setSortingEnabled(False)
        self._results_table.setToolTip("右键表头或表格区域，可设置显示哪些结果参数")
        self._results_table.verticalHeader().setVisible(False)
        self._results_table.verticalHeader().setDefaultSectionSize(24)
        self._results_table.setWordWrap(False)
        results_header = self._results_table.horizontalHeader()
        results_header.setStretchLastSection(False)
        results_header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        results_header.customContextMenuRequested.connect(
            lambda position: self._show_results_column_menu(
                results_header.mapToGlobal(position)))
        # Also expose the same menu from the table body so the setting is easy
        # to discover even when the header is scrolled out of view.
        self._results_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._results_table.customContextMenuRequested.connect(
            lambda position: self._show_results_column_menu(
                self._results_table.mapToGlobal(position)))
        # Keep descriptive columns readable while allowing the filename column
        # to absorb the remaining width.  Small metric/status columns stay
        # compact instead of pushing the path and resolution columns off-screen.
        results_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        result_widths = {
            1: 108, 2: 98, 3: 138, 4: 130, 5: 48, 6: 58,
            7: 68, 8: 68, 9: 56, 10: 136, 11: 64,
        }
        for column, width in result_widths.items():
            results_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self._results_table.setColumnWidth(column, width)
        for column, label in enumerate(self.RESULT_COLUMNS):
            self._results_table.setColumnHidden(
                column, label in self.DEFAULT_HIDDEN_RESULT_COLUMNS)
        results_layout.addWidget(self._results_table)
        splitter.addWidget(results_group)

        matrix_group = QGroupBox("解码能力矩阵（真实素材结果）")
        # The expanded codec taxonomy needs a little more room.  Keep the
        # section compact while allowing the matrix itself to scroll when a
        # smaller window cannot show every family at once.
        matrix_group.setMinimumHeight(180)
        matrix_group.setMaximumHeight(260)
        matrix_layout = QVBoxLayout(matrix_group)
        matrix_layout.setContentsMargins(7, 6, 7, 7)
        matrix_layout.setSpacing(5)
        matrix_hint = QLabel(
            "同一编码/分辨率可添加多条素材；仅显示已经完成测试的编码，"
            "单元格保留最近一次成功结果。"
        )
        matrix_hint.setObjectName("Muted")
        matrix_layout.addWidget(matrix_hint)
        self._matrix = QTableWidget(0, len(self.MATRIX_COLUMNS) + 1)
        self._matrix.setObjectName("Matrix")
        self._matrix.setHorizontalHeaderLabels(["编码 / 分辨率", *self.MATRIX_COLUMNS])
        self._matrix.viewport().setStyleSheet(_TABLE_VIEWPORT_STYLE)
        self._matrix.verticalHeader().setVisible(False)
        self._matrix.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._matrix.verticalHeader().setMinimumSectionSize(20)
        self._matrix.verticalHeader().setDefaultSectionSize(22)
        self._matrix.setWordWrap(True)
        self._matrix.setAlternatingRowColors(True)
        matrix_header = self._matrix.horizontalHeader()
        matrix_header.setStretchLastSection(False)
        matrix_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._matrix.setColumnWidth(0, 120)
        for column in range(1, self._matrix.columnCount()):
            # Equal stretch prevents the 8K column from consuming all leftover
            # space and keeps the four resolution buckets visually comparable.
            matrix_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        matrix_layout.addWidget(self._matrix)
        splitter.addWidget(matrix_group)
        # The matrix has a bounded height; the per-material table receives the
        # remaining space and can scroll when many clips are queued.
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        # Give the expanded codec matrix enough default height to show most
        # families; the thin vertical scrollbar exposes the remaining rows.
        splitter.setSizes([220, 253])
        root.addWidget(splitter, 1)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 5, 12, 6)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        footer_layout.addWidget(self._progress, 1)
        self._detail_label = QLabel("未开始", objectName="Muted")
        footer_layout.addWidget(self._detail_label)
        root.addWidget(footer)

    def _show_results_column_menu(self, global_pos) -> None:
        """Show the result-table column visibility menu at ``global_pos``."""

        menu = QMenu(self)
        heading = menu.addAction("显示结果参数")
        heading.setEnabled(False)
        menu.addSeparator()

        for column, label in enumerate(self.RESULT_COLUMNS):
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(not self._results_table.isColumnHidden(column))
            action.toggled.connect(
                lambda visible, index=column:
                self._results_table.setColumnHidden(index, not visible)
            )
            menu.addAction(action)

        menu.addSeparator()
        reset_action = menu.addAction("恢复默认列")
        reset_action.triggered.connect(self._reset_result_columns)
        menu.exec(global_pos)

    def _reset_result_columns(self) -> None:
        """Make every result column visible again."""

        for column in range(self._results_table.columnCount()):
            self._results_table.setColumnHidden(column, False)

    def add_paths(self, paths: Iterable[str]) -> None:
        """Add existing video paths to the queue, preserving order."""

        for raw_path in paths:
            path = os.path.abspath(os.path.expanduser(str(raw_path)))
            if not os.path.isfile(path) or path in self._paths:
                continue
            self._paths.append(path)
            item = QListWidgetItem(Path(path).name)
            item.setToolTip(path)
            self._source_list.addItem(item)
        self._status_label.setText(f"已添加 {len(self._paths)} 个素材")

    def _choose_files(self) -> None:
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "选择测试视频",
            str(Path.home()),
            "视频文件 (*.mp4 *.mov *.mxf *.mkv *.avi *.webm *.m4v *.ts);;所有文件 (*)",
        )
        self.add_paths(paths)

    def _remove_selected(self) -> None:
        selected_rows = sorted(
            (self._source_list.row(item) for item in self._source_list.selectedItems()),
            reverse=True,
        )
        for row in selected_rows:
            self._source_list.takeItem(row)
            del self._paths[row]
        self._status_label.setText(f"已添加 {len(self._paths)} 个素材")

    def _edit_service(self):
        """Resolve the host service lazily so late Edit initialization works."""

        bridge = self._host_bridge
        getter = getattr(bridge, "get_edit_benchmark_service", None)
        if callable(getter):
            try:
                return getter()
            except RuntimeError:
                return None
        return getattr(bridge, "_edit_benchmark_service", None)

    def _clear_result_views(self) -> None:
        self._results.clear()
        self._results_table.setRowCount(0)
        self._matrix.setRowCount(0)
        self._matrix_families.clear()
        self._edit_row = None
        self._edit_last_snapshot = None
        self._headline_fps.setText("— FPS")
        self._headline_latency.setText("— ms / frame")

    def _start_edit_benchmark(self) -> None:
        service = self._edit_service()
        if service is None:
            QMessageBox.information(
                self,
                "Edit 管线不可用",
                "当前文件浏览器没有连接到主窗口的 Edit host service。\n"
                "请先打开 Edit 页面并等待实时渲染器初始化。",
            )
            return
        self._clear_result_views()
        try:
            response = service.start_session(
                frame_count=self._frame_spin.value(),
                warmup_frames=self._warmup_spin.value(),
            )
        except Exception as exc:  # pragma: no cover - defensive host boundary
            QMessageBox.warning(self, "Edit 跑分失败", str(exc))
            return
        if not isinstance(response, dict) or not response.get("ok"):
            QMessageBox.information(
                self,
                "无法开始 Edit 跑分",
                str((response or {}).get("error", "Edit host service 拒绝了请求")),
            )
            return
        self._edit_session_active = True
        self._start_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        self._export_button.setEnabled(False)
        self._progress.setValue(0)
        self._status_label.setText("Edit 实时管线测试中")
        self._detail_label.setText("正在预热时间线解码与 GPU 合成…")
        initial = response.get("snapshot")
        if isinstance(initial, dict):
            self._render_edit_snapshot(initial)
        self._edit_timer.start()

    def _poll_edit_session(self) -> None:
        if not self._edit_session_active:
            self._edit_timer.stop()
            return
        service = self._edit_service()
        if service is None:
            self._edit_timer.stop()
            self._edit_session_active = False
            self._status_label.setText("Edit host service 已断开")
            self._stop_button.setEnabled(False)
            self._start_button.setEnabled(True)
            return
        try:
            snapshot = service.snapshot()
        except Exception as exc:  # pragma: no cover - defensive host boundary
            self._detail_label.setText(str(exc))
            return
        if not isinstance(snapshot, dict):
            return
        self._edit_last_snapshot = snapshot
        self._render_edit_snapshot(snapshot)
        session = snapshot.get("session")
        phase = session.get("phase") if isinstance(session, dict) else None
        if phase in {"completed", "cancelled"}:
            self._edit_timer.stop()
            self._edit_session_active = False
            result = self._edit_result_from_snapshot(snapshot)
            if result is not None:
                self._results.append(result)
            self._status_label.setText(
                "Edit 测试完成"
                if result is not None and result.status == "ok"
                else ("Edit 测试完成（部分）" if phase == "completed" else "Edit 测试已停止")
            )
            self._detail_label.setText("真实呈现帧与 GPU 合成遥测已收集")
            self._stop_button.setEnabled(False)
            self._start_button.setEnabled(True)
            self._export_button.setEnabled(bool(self._results))

    @staticmethod
    def _source_kinds_text(source_kinds: object) -> str:
        if not isinstance(source_kinds, dict) or not source_kinds:
            return "—"
        parts: list[str] = []
        for key, value in sorted(source_kinds.items(), key=lambda item: str(item[0])):
            try:
                parts.append(f"{key} {float(value) * 100:.0f}%")
            except (TypeError, ValueError):
                parts.append(str(key))
        return ", ".join(parts)

    @staticmethod
    def _path_counts_text(path_counts: object) -> str:
        """Format frame-trace counts without confusing them with percentages."""

        if not isinstance(path_counts, dict) or not path_counts:
            return "—"
        return ", ".join(
            f"{key} {int(value)}"
            for key, value in sorted(path_counts.items(), key=lambda item: str(item[0]))
        )

    def _render_edit_snapshot(self, snapshot: dict[str, object]) -> None:
        """Render one live host-service snapshot into the existing result row."""

        timeline = snapshot.get("timeline") if isinstance(snapshot.get("timeline"), dict) else {}
        session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
        telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
        frame_trace = snapshot.get("frame_trace") if isinstance(snapshot.get("frame_trace"), dict) else {}
        width = int(timeline.get("width", 0) or 0)
        height = int(timeline.get("height", 0) or 0)
        requested = int(session.get("requested_frames", 0) or 0)
        presented = int(session.get("presented_frames", 0) or 0)
        misses = int(session.get("misses_delta", 0) or 0)
        fps = float(session.get("presented_fps", 0.0) or telemetry.get("effective_fps", 0.0) or 0.0)
        avg_ms = float(telemetry.get("total_ms_mean", 0.0) or 0.0)
        p95_ms = float(telemetry.get("total_ms_p95", 0.0) or 0.0)
        decode_paths = frame_trace.get("decode_path_counts")
        render_paths = frame_trace.get("render_path_counts")
        trace_gpu_frames = int(frame_trace.get("gpu_frames", 0) or 0)
        gpu_frames = max(
            int(telemetry.get("gpu_composite_frames", 0) or 0),
            int(session.get("gpu_composite_frames_delta", 0) or 0),
            trace_gpu_frames,
        )
        render_path = "gpu_composite" if gpu_frames else (
            "cpu_qimage"
            if max(
                int(telemetry.get("cpu_qimage_frames", 0) or 0),
                int(session.get("cpu_qimage_frames_delta", 0) or 0),
            )
            else "unknown"
        )
        phase = str(session.get("phase", "idle"))
        status = {
            "warmup": "预热",
            "measuring": "测试中",
            "draining": "收尾",
            "completed": "完成",
            "cancelled": "已停止",
        }.get(phase, phase)
        if phase == "completed" and requested and presented < requested:
            status = "完成（部分）"
        if str(session.get("frame_signal_source", "")).endswith("fallback"):
            status += " · 提交计数"
        if bool((snapshot.get("invariants") or {}).get("ok", True)) is False:
            status += " · invariant 警告"

        self._headline_fps.setText(f"{fps:.1f} FPS")
        self._headline_latency.setText(f"{avg_ms:.2f} ms / frame")
        if requested:
            self._progress.setValue(min(100, int(presented * 100 / requested)))
        self._detail_label.setText(
            f"呈现 {presented}/{requested or '—'} 帧 · 丢帧 {misses} · "
            f"decode {float(telemetry.get('decode_ms_mean', 0.0) or 0.0):.2f} ms · "
            f"GPU 合成 {float(telemetry.get('composite_ms_mean', 0.0) or 0.0):.2f} ms"
        )

        values = [
            str(timeline.get("name") or "当前时间线"),
            "Mixed",
            f"{width}×{height}" if width and height else "—",
            f"{float(timeline.get('fps', 0.0) or 0.0):.3g} fps",
            "Edit 实时",
            str(presented),
            f"{fps:.1f}",
            f"{avg_ms:.2f}",
            f"{p95_ms:.2f}",
            self._path_counts_text(decode_paths) if decode_paths else self._source_kinds_text(telemetry.get("source_kinds")),
            self._path_counts_text(render_paths) if render_paths else render_path,
            status,
        ]
        if self._edit_row is None:
            self._edit_row = self._results_table.rowCount()
            self._results_table.insertRow(self._edit_row)
        for column, value in enumerate(values):
            item = self._results_table.item(self._edit_row, column)
            if item is None:
                item = QTableWidgetItem()
                self._results_table.setItem(self._edit_row, column, item)
            item.setText(value)
            if column == 10:
                item.setToolTip("来自 TimelinePreviewRenderer / RealtimePlaybackEngine")

    def _edit_result_from_snapshot(self, snapshot: dict[str, object]) -> Optional[BenchmarkResult]:
        timeline = snapshot.get("timeline") if isinstance(snapshot.get("timeline"), dict) else {}
        session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
        telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
        frame_trace = snapshot.get("frame_trace") if isinstance(snapshot.get("frame_trace"), dict) else {}
        invariants = snapshot.get("invariants") if isinstance(snapshot.get("invariants"), dict) else {}
        width = int(timeline.get("width", 0) or 0)
        height = int(timeline.get("height", 0) or 0)
        presented = int(session.get("presented_frames", 0) or 0)
        misses = int(session.get("misses_delta", 0) or 0)
        phase = str(session.get("phase", "completed"))
        decode_path_counts = frame_trace.get("decode_path_counts")
        trace_gpu_frames = int(frame_trace.get("gpu_frames", 0) or 0)
        gpu_frames = max(
            int(telemetry.get("gpu_composite_frames", 0) or 0),
            int(session.get("gpu_composite_frames_delta", 0) or 0),
            trace_gpu_frames,
        )
        render_path = "gpu_composite" if gpu_frames else (
            "cpu_qimage"
            if max(
                int(telemetry.get("cpu_qimage_frames", 0) or 0),
                int(session.get("cpu_qimage_frames_delta", 0) or 0),
            )
            else "unknown"
        )
        if isinstance(decode_path_counts, dict) and decode_path_counts:
            decode_path = ", ".join(sorted(str(key) for key in decode_path_counts))
        else:
            decode_path = render_path
        decode_mean_ms = float(telemetry.get("decode_ms_mean", 0.0) or 0.0)
        return BenchmarkResult(
            path=f"edit://timeline/{timeline.get('id', '')}",
            filename=str(timeline.get("name") or "当前时间线"),
            codec="mixed",
            codec_family="Edit Timeline",
            resolution=(
                f"{width}×{height}" if width and height else "—"
            ),
            width=width,
            height=height,
            fps=float(timeline.get("fps", 0.0) or 0.0),
            bit_depth=0,
            pixel_format="timeline",
            mode="edit_realtime",
            pattern="sequential",
            requested_frames=int(session.get("requested_frames", 0) or 0),
            warmup_frames=int(session.get("warmup_frames", 0) or 0),
            frames_decoded=presented,
            failed_frames=misses,
            elapsed_ms=float(session.get("elapsed_ms", 0.0) or 0.0),
            decode_fps=1000.0 / decode_mean_ms if decode_mean_ms > 0.0 else 0.0,
            avg_frame_ms=float(telemetry.get("total_ms_mean", 0.0) or 0.0),
            p95_frame_ms=float(telemetry.get("total_ms_p95", 0.0) or 0.0),
            decode_path=decode_path,
            decoder="Edit host",
            hardware_acceleration=gpu_frames > 0,
            presented_fps=float(session.get("presented_fps", 0.0) or telemetry.get("effective_fps", 0.0) or 0.0),
            presented_frames=presented,
            render_path=render_path,
            composite_avg_ms=float(telemetry.get("composite_ms_mean", 0.0) or 0.0),
            composite_p95_ms=float(telemetry.get("composite_ms_p95", 0.0) or 0.0),
            dropped_frames=misses,
            invariants_ok=bool(invariants.get("ok", True)),
            frame_signal_source=str(session.get("frame_signal_source", "renderer.frame_ready")),
            trace_summary=frame_trace,
            status=(
                "ok"
                if phase == "completed"
                and presented >= int(session.get("requested_frames", 0) or 0)
                and bool(invariants.get("ok", True))
                else "partial"
            ),
            error="" if bool(invariants.get("ok", True)) else str(invariants.get("message", "")),
        )

    def _start_benchmark(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if self._edit_session_active:
            return
        mode = self._mode_combo.currentData()
        if mode == "edit_realtime":
            self._start_edit_benchmark()
            return
        if not self._paths:
            QMessageBox.information(self, "没有测试素材", "请先添加至少一个视频文件。")
            return

        self._clear_result_views()
        config = BenchmarkConfig(
            frame_count=self._frame_spin.value(),
            warmup_frames=self._warmup_spin.value(),
            mode=self._mode_combo.currentData(),
            pattern=self._pattern_combo.currentData(),
        ).normalized()
        self._worker = BenchmarkWorker(tuple(self._paths), config)
        self._worker.progress.connect(self._on_progress)
        self._worker.result_ready.connect(self._append_result)
        self._worker.completed.connect(self._on_completed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._start_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        self._export_button.setEnabled(False)
        self._progress.setValue(0)
        self._detail_label.setText("正在打开解码源…")
        self._status_label.setText("测试中")
        self._worker.start()

    def _stop_benchmark(self) -> None:
        if self._edit_session_active:
            service = self._edit_service()
            if service is not None:
                try:
                    service.stop_session()
                except Exception as exc:  # pragma: no cover - defensive host boundary
                    self._detail_label.setText(str(exc))
            self._stop_button.setEnabled(False)
            return
        if self._worker is not None:
            self._worker.cancel()
            self._detail_label.setText("正在停止（等待当前帧结束）…")
            self._stop_button.setEnabled(False)

    def _on_progress(self, path: str, done: int, total: int) -> None:
        self._progress.setValue(int(done * 100 / max(total, 1)))
        self._detail_label.setText(f"{Path(path).name}  ·  {done}/{total} 帧")

    def _append_result(self, result: BenchmarkResult) -> None:
        self._results.append(result)
        if result.status != "error":
            self._headline_fps.setText(f"{result.decode_fps:.1f} FPS")
            self._headline_latency.setText(f"{result.avg_frame_ms:.2f} ms / frame")
        row = self._results_table.rowCount()
        self._results_table.insertRow(row)
        values = [
            result.filename,
            result.codec_family,
            f"{result.width}×{result.height}" if result.width else "—",
            f"{result.bit_depth}bit {result.pixel_format}" if result.bit_depth else "—",
            (
                f"Proxy → {result.proxy_size}"
                if result.mode == "proxy" and result.proxy_size != "—"
                else ("Proxy" if result.mode == "proxy" else "Master")
            ),
            str(result.frames_decoded),
            f"{result.decode_fps:.1f}",
            f"{result.avg_frame_ms:.2f}",
            f"{result.p95_frame_ms:.2f}",
            f"{result.decoder}{' (HW)' if result.hardware_acceleration else ''}",
            result.decode_path,
            result.status if result.status != "error" else result.error,
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 10:
                item.setToolTip(result.path)
            self._results_table.setItem(row, column, item)
        self._update_matrix(result)

    def _update_matrix(self, result: BenchmarkResult) -> None:
        if result.status == "error":
            return
        try:
            family_index = self.MATRIX_ROWS.index(result.codec_family)
            column = self.MATRIX_COLUMNS.index(result.resolution) + 1
        except ValueError:
            return
        if result.codec_family not in self._matrix_families:
            # Keep rows in the stable SUPPORTED_CODECS order rather than in
            # whichever order the worker happens to finish its files.
            insert_at = sum(
                1
                for family in self._matrix_families
                if self.MATRIX_ROWS.index(family) < family_index
            )
            self._matrix.insertRow(insert_at)
            self._matrix_families.insert(insert_at, result.codec_family)
            self._matrix.setItem(insert_at, 0, QTableWidgetItem(result.codec_family))
            for matrix_column in range(1, self._matrix.columnCount()):
                self._matrix.setItem(insert_at, matrix_column, QTableWidgetItem("—"))
        row = self._matrix_families.index(result.codec_family)
        realtime_mark = "✓" if result.decode_fps >= result.fps > 0 else "×"
        text = f"{realtime_mark} {result.decode_fps:.1f} FPS · {result.avg_frame_ms:.1f} ms"
        item = self._matrix.item(row, column)
        item.setText(text)
        item.setForeground(QColor("#30d158" if realtime_mark == "✓" else "#ff6961"))
        item.setToolTip(result.path)

    def _on_completed(self, results: object) -> None:
        count = len(results) if isinstance(results, list) else len(self._results)
        self._progress.setValue(100)
        self._status_label.setText(f"完成：{count} 个素材")
        self._detail_label.setText("测试完成")
        self._stop_button.setEnabled(False)
        self._export_button.setEnabled(bool(self._results))

    def _on_cancelled(self) -> None:
        self._status_label.setText("已停止")
        self._detail_label.setText("测试已取消")
        self._stop_button.setEnabled(False)
        self._export_button.setEnabled(bool(self._results))

    def _on_failed(self, message: str) -> None:
        self._status_label.setText("失败")
        self._detail_label.setText(message)
        self._stop_button.setEnabled(False)
        self._export_button.setEnabled(bool(self._results))
        QMessageBox.warning(self, "解码跑分失败", message)

    def _on_worker_finished(self) -> None:
        """Release the QThread only after its native run() has returned."""

        worker = self.sender()
        if worker is self._worker:
            self._worker = None
            self._start_button.setEnabled(True)
        if isinstance(worker, QThread):
            worker.deleteLater()

    def _export_results(self) -> None:
        if not self._results:
            return
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出跑分结果",
            str(Path.home() / "topos_decode_benchmark.json"),
            "JSON (*.json);;CSV (*.csv)",
        )
        if not path:
            return
        try:
            if "CSV" in selected_filter or path.lower().endswith(".csv"):
                export_results_csv(path, self._results)
            else:
                if not path.lower().endswith(".json"):
                    path += ".json"
                export_results_json(path, self._results)
            self._detail_label.setText(f"已导出：{Path(path).name}")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._edit_session_active:
            self._edit_timer.stop()
            service = self._edit_service()
            if service is not None:
                try:
                    service.stop_session()
                except Exception:
                    pass
            self._edit_session_active = False
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            if not self._worker.wait(2000):
                # Never destroy a QWidget while its QThread may still own a
                # PyAV/FFmpeg decoder. Keep the window alive and let the user
                # close it again once the native call returns.
                self._status_label.setText("解码器仍在退出，请稍后再关闭")
                event.ignore()
                return
        event.accept()


__all__ = ["BenchmarkWorker", "DecodeBenchmarkWindow"]
