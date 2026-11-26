"""ADR workspace for configuring, cueing, recording and comparing takes."""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from src.features.edit.core.take_sheet import TakeSheet
    from src.features.edit.core.timeline_v2 import TimelineSequence

import numpy as np
from PyQt6.QtCore import QObject, QSignalBlocker, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from src.features.edit.core.adr_cues import AdrCue, read_adr_cues
from src.features.edit.core.interchange import frames_to_timecode, timecode_to_frames
from src.features.edit.core.recording_manager import is_drop_frame_rate
from src.shared.assets.icon_manager import icon_refresh
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.compact_scrollbar import CompactVerticalScrollBar

logger = logging.getLogger(__name__)

_STATE_LABELS = {
    "idle": ("edit.adr.state_idle", "Ready"),
    "rehearsing": ("edit.adr.state_rehearsing", "Rehearsing"),
    "pre_roll": ("edit.adr.state_preroll", "Pre-roll…"),
    "recording": ("edit.adr.state_recording", "● REC"),
    "stopping": ("edit.adr.state_postroll", "Post-roll…"),
    "flushing": ("edit.adr.state_saving", "Saving take…"),
    "failed": ("edit.adr.state_failed", "Failed"),
}
_STATE_COLORS = {
    "idle": QColor("#8a8a8a"),
    "pre_roll": QColor("#d4a017"),
    "recording": QColor("#e53935"),
    "stopping": QColor("#d4a017"),
    "failed": QColor("#e53935"),
}


class InputLevelBar(QWidget):
    """轻量输入电平条（dBFS → 水平条 + 峰值保持）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._dbfs = -60.0
        self._peak = -60.0
        self.setMinimumHeight(14)
        self.setFixedHeight(14)

    def set_level(self, dbfs: float) -> None:
        if dbfs is None or not np.isfinite(dbfs):
            dbfs = -60.0
        self._dbfs = float(np.clip(dbfs, -60.0, 0.0))
        self._peak = max(self._peak - 0.35, self._dbfs)
        self.update()

    def reset(self) -> None:
        self._dbfs = -60.0
        self._peak = -60.0
        self.update()

    @staticmethod
    def _pos(db: float, width: int) -> int:
        frac = (db + 60.0) / 60.0
        return int(np.clip(frac, 0.0, 1.0) * width)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2a2a2e"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        width = max(1, self.width() - 2)
        level = self._pos(self._dbfs, width)
        peak = self._pos(self._peak, width)
        if self._dbfs > -1.0:
            painter.setBrush(QColor("#e53935"))
        elif self._dbfs > -12.0:
            painter.setBrush(QColor("#7cb342"))
        else:
            painter.setBrush(QColor("#558b2f"))
        painter.drawRect(1, 1, level, self.height() - 2)
        painter.setBrush(QColor("#ffffff"))
        painter.drawRect(peak, 1, 2, self.height() - 2)
        painter.end()


class TakeAuditionPlayer(QObject):
    """take 试听：PyAV 解码 → QAudioSink 推流播放（短 take 全量解码）。

    AUD-19400/19401 护栏：解码前先按文件大小与容器时长探测拒绝超长 take，
    解码循环再以采样数硬上限兜底（防容器元数据说谎/损坏），保证：
    - 异常路径 container 句柄必然关闭（try/finally）；
    - UI 线程上的同步解码时长与内存占用有界。
    """

    # 单次试听上限：5 分钟音频（48k stereo float32 ≈ 115 MB，与"短 take"
    # 设计取舍一致）；超过则拒绝全量解码。
    _MAX_AUDITION_SECONDS = 300.0
    # 采样数硬上限（按 48kHz 计算，解码循环中强制截断）。
    _MAX_AUDITION_SAMPLES = int(48000 * 300)
    # 文件大小上限（256 MB）：超过直接拒绝，避免极端容器在探测阶段就拖死 UI。
    _MAX_AUDITION_FILE_BYTES = 256 * 1024 * 1024

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._sink: Any = None
        self._io: Any = None
        self._pending = b""
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._feed)

    def is_playing(self) -> bool:
        return self._io is not None

    def stop(self) -> None:
        self._timer.stop()
        self._pending = b""
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:
                pass
        self._sink = None
        self._io = None

    def play(self, path: str) -> bool:
        """解码并播放 take 文件；失败（无设备/解码错误）返回 False。"""
        self.stop()
        samples, sample_rate, channels = self._decode(path)
        if samples is None or samples.size == 0:
            return False
        try:
            from PyQt6.QtMultimedia import QAudioSink, QAudioFormat
            fmt = QAudioFormat()
            fmt.setSampleRate(sample_rate)
            fmt.setChannelCount(channels)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
            self._sink = QAudioSink(fmt)
            self._io = self._sink.start()
            if self._io is None:
                self._sink = None
                return False
            interleaved = np.ascontiguousarray(samples.T, dtype=np.float32)
            self._pending = interleaved.tobytes()
            self._feed()
            self._timer.start()
            return True
        except Exception as e:
            logger.warning("Take audition start failed: %s", e)
            self.stop()
            return False

    def _feed(self) -> None:
        if self._io is None:
            self.stop()
            return
        free = self._sink.bytesFree()
        if free > 0:
            chunk, self._pending = self._pending[:free], self._pending[free:]
            if chunk:
                self._io.write(chunk)
                return
        if not self._pending:
            # 缓冲排空（Idle）→ 播放结束
            try:
                from PyQt6.QtMultimedia import QAudio
                if self._sink.state() == QAudio.State.IdleState:
                    self.stop()
            except Exception:
                self.stop()

    @staticmethod
    def _decode(path: str) -> tuple[np.ndarray | None, int, int]:
        """解码 take WAV → (planar float32, sample_rate, channels)。

        AUD-19400：container 在 try/finally 中关闭，decode 中途抛异常
        （损坏/半写文件）不再泄漏文件句柄与内部缓冲。
        AUD-19401：超长/超大 take 在解码前被拒绝；解码循环带采样数硬上限，
        界面不会为一次双击试听冻结数秒并驻留数百 MB 音频数据。
        """
        container = None
        try:
            import av
            try:
                size_bytes = os.path.getsize(path)
            except OSError as exc:
                logger.warning("Take audition stat failed for %s: %s", path, exc)
                return None, 48000, 1
            if size_bytes > TakeAuditionPlayer._MAX_AUDITION_FILE_BYTES:
                logger.warning(
                    "Take audition rejected (file too large %.1f MB): %s",
                    size_bytes / (1024 * 1024), path,
                )
                return None, 48000, 1
            container = av.open(path)
            # PyAV 的 container.duration 单位是微秒；仅用于廉价预检。
            duration_us = getattr(container, "duration", None)
            if duration_us and duration_us / 1_000_000.0 > TakeAuditionPlayer._MAX_AUDITION_SECONDS:
                logger.warning(
                    "Take audition rejected (duration %.1f s exceeds %.0f s): %s",
                    duration_us / 1_000_000.0,
                    TakeAuditionPlayer._MAX_AUDITION_SECONDS, path,
                )
                return None, 48000, 1
            blocks: List[np.ndarray] = []
            channels = 1
            sample_rate = 48000
            total_samples = 0
            for frame in container.decode(audio=0):
                arr = frame.to_ndarray()  # (channels, n) for fltp
                if arr.ndim == 1:
                    arr = arr.reshape(1, -1)
                channels = arr.shape[0]
                sample_rate = int(frame.sample_rate or sample_rate)
                total_samples += arr.shape[1]
                if total_samples > TakeAuditionPlayer._MAX_AUDITION_SAMPLES:
                    logger.warning(
                        "Take audition truncated at sample cap (%d): %s",
                        TakeAuditionPlayer._MAX_AUDITION_SAMPLES, path,
                    )
                    break
                blocks.append(arr.astype(np.float32, copy=False))
            if not blocks:
                return None, sample_rate, channels
            return np.concatenate(blocks, axis=1), sample_rate, channels
        except Exception as e:
            logger.warning("Take audition decode failed for %s: %s", path, e)
            return None, 48000, 1
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:  # noqa: BLE001 - 关闭失败的句柄只能放弃
                    logger.debug("Take audition container close failed", exc_info=True)


class InputMonitorSink(QObject):
    """输入监听：把采集块实时转发到输出（QAudioSink push 回放）。

    录音时演员需要听到自己的输入；核心 RecordingManager 通过
    on_monitor_block 回调把 planar 块交给本类，这里交错化后写往默认
    输出。无音频设备/解码环境时 start() 返回 False，链路安全降级。
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._sink: Any = None
        self._io: Any = None
        self._channels = 1

    @property
    def active(self) -> bool:
        return self._io is not None

    def start(self, sample_rate: int, channels: int) -> bool:
        self.stop()
        try:
            from PyQt6.QtMultimedia import QAudioSink, QAudioFormat
            fmt = QAudioFormat()
            fmt.setSampleRate(int(sample_rate))
            fmt.setChannelCount(int(channels))
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
            self._sink = QAudioSink(fmt)
            self._channels = int(channels)
            self._io = self._sink.start()
            if self._io is None:
                self._sink = None
                return False
            return True
        except Exception as e:
            logger.warning("Input monitor start failed: %s", e)
            self.stop()
            return False

    def write_block(self, planar: np.ndarray) -> None:
        """on_monitor_block 回调目标：planar (channels, n) → 交错写入。"""
        if self._io is None or planar is None or planar.size == 0:
            return
        try:
            interleaved = np.ascontiguousarray(planar.T, dtype=np.float32)
            self._io.write(interleaved.tobytes())
        except Exception as e:
            logger.warning("Input monitor write failed: %s", e)

    def stop(self) -> None:
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:
                pass
        self._sink = None
        self._io = None


class AdrTakeSheetPanel(QWidget):
    """ADR/VO 录音 take sheet 面板（P1-10）。

    面板不持有 RecordingManager——它暴露 adr_settings() 供控制器建配置，
    通过 set_recording_state/set_input_level/add_take_entry 接收会话状态。
    """

    settings_changed = pyqtSignal()
    calibrate_requested = pyqtSignal()
    mark_best_requested = pyqtSignal(str)        # take_id
    replace_clip_requested = pyqtSignal(str)     # take_id
    cue_changed = pyqtSignal(object)             # AdrCue
    cue_removed = pyqtSignal(str)                # cue_id
    cue_selected = pyqtSignal(object)             # AdrCue
    rehearse_requested = pyqtSignal(object)       # AdrCue
    record_requested = pyqtSignal(object)         # AdrCue
    playhead_record_requested = pyqtSignal(str)   # optional destination track ID
    stop_requested = pyqtSignal()

    _COLS = ("#", "Take", "Length", "Peak", "In TC", "Status", "Best")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._take_sheet = None  # lazy: core.TakeSheet
        self._audition = TakeAuditionPlayer(self)
        self._monitor_sink = InputMonitorSink(self)
        self._timeline: TimelineSequence | None = None
        self._cues: tuple[AdrCue, ...] = ()
        self._record_state = "idle"
        self._current_frame = 0
        self._caption_workspace = False

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(6)

        self._tabs = QTabWidget(self)
        self._tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self._cue_page = self._build_cue_list()
        self._record_page = self._build_record_tab()
        self._tabs.addTab(self._cue_page, I18N.tr("edit.adr.list_tab", "Cues"))
        self._tabs.addTab(self._record_page, I18N.tr("edit.adr.record_tab", "Record"))
        self._settings_page = self._build_settings_group()
        self._tabs.addTab(self._settings_page, I18N.tr("edit.adr.setup_tab", "Setup"))
        self._layout.addWidget(self._tabs)

        self.set_recording_state("idle")

    def host_caption_tools(self, page: QWidget, label: str) -> None:
        """Present the Edit subtitle controls beside the shared ADR pages."""
        self._caption_workspace = True
        if self._tabs.indexOf(page) < 0:
            self._tabs.addTab(page, label)
        self._tabs.setTabVisible(self._tabs.indexOf(page), True)
        self._tabs.tabBar().moveTab(self._tabs.indexOf(self._record_page), 0)
        self._tabs.tabBar().moveTab(self._tabs.indexOf(page), 1)
        record_index = self._tabs.indexOf(self._record_page)
        use_english = I18N.get_current_locale() != "zh_CN"
        self._tabs.setTabText(record_index, I18N.tr(
            "edit.effects.voiceover", "Voiceover", use_english=use_english
        ))
        self._tabs.setTabText(self._tabs.indexOf(self._settings_page), I18N.tr(
            "edit.effects.recording_setup", "Setup", use_english=use_english
        ))
        self._playhead_record_btn.show()
        self._tabs.setCurrentWidget(self._record_page)
        self._layout.setContentsMargins(4, 0, 0, 0)
        self._tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self._tabs.tabBar().setUsesScrollButtons(False)
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: 0; }}
            QTabBar {{ font-size: 12px; }}
            QTabBar::tab {{
                background: {C.BG_PANEL}; color: {C.TEXT_LABEL};
                border: 0; border-bottom: 2px solid transparent;
                border-radius: 0; padding: 3px 6px;
            }}
            QTabBar::tab:selected {{
                color: {C.TEXT_PRIMARY}; border-bottom: 2px solid {C.BORDER_MID};
            }}
            QPushButton {{ padding: 4px 7px; font-size: 12px; }}
            QPushButton:disabled {{ color: {C.TEXT_HINT}; }}
            QTableWidget::item:selected {{
                color: {C.TEXT_PRIMARY}; background: {C.BG_BUTTON};
            }}
        """)
        self._refresh_table()

    def leave_caption_workspace(self) -> None:
        """Restore the standalone ADR tabs for the Mixer dock."""
        self._caption_workspace = False
        self._tabs.setCurrentWidget(self._cue_page)
        for index in range(self._tabs.count()):
            page = self._tabs.widget(index)
            self._tabs.setTabVisible(index, page in (self._cue_page, self._record_page, self._settings_page))
        self._tabs.tabBar().moveTab(self._tabs.indexOf(self._cue_page), 0)
        self._tabs.setTabText(self._tabs.indexOf(self._record_page), I18N.tr("edit.adr.record_tab", "Record"))
        self._tabs.setTabText(self._tabs.indexOf(self._settings_page), I18N.tr("edit.adr.setup_tab", "Setup"))
        self._playhead_record_btn.hide()
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self._tabs.tabBar().setUsesScrollButtons(True)
        self._tabs.setStyleSheet("")
        self._refresh_table()

    # ── 构建 UI ──

    def _build_settings_group(self) -> QWidget:
        box = QGroupBox(I18N.tr("edit.adr.settings", "ADR Input"), self)
        form = QFormLayout(box)

        self._device_combo = QComboBox(box)
        self._refresh_btn = QPushButton(icon_refresh(C.TEXT_LABEL), "", box)
        self._refresh_btn.setFixedWidth(28)
        self._refresh_btn.setIconSize(QSize(12, 12))
        self._refresh_btn.setToolTip(I18N.tr("edit.adr.refresh_tip", "Refresh devices"))
        self._refresh_btn.setAccessibleName(self._refresh_btn.toolTip())
        self._refresh_btn.clicked.connect(self.reload_devices)
        device_row = QHBoxLayout()
        device_row.addWidget(self._device_combo, stretch=1)
        device_row.addWidget(self._refresh_btn)
        form.addRow(I18N.tr("edit.adr.device", "Device"), device_row)

        self._channels_combo = QComboBox(box)
        self._channels_combo.addItem(I18N.tr("edit.adr.mono", "Mono"), 1)
        self._channels_combo.addItem(I18N.tr("edit.adr.stereo", "Stereo"), 2)
        form.addRow(I18N.tr("edit.adr.channels", "Channels"), self._channels_combo)

        self._pre_roll_spin = QDoubleSpinBox(box)
        self._pre_roll_spin.setRange(0.0, 10.0)
        self._pre_roll_spin.setSingleStep(0.5)
        self._pre_roll_spin.setSuffix(I18N.tr("edit.seconds_suffix", " s"))
        self._pre_roll_spin.setValue(2.0)
        form.addRow(I18N.tr("edit.adr.pre_roll", "Pre-roll"), self._pre_roll_spin)

        self._post_roll_spin = QDoubleSpinBox(box)
        self._post_roll_spin.setRange(0.0, 5.0)
        self._post_roll_spin.setSingleStep(0.5)
        self._post_roll_spin.setSuffix(I18N.tr("edit.seconds_suffix", " s"))
        self._post_roll_spin.setValue(1.0)
        form.addRow(I18N.tr("edit.adr.post_roll", "Post-roll"), self._post_roll_spin)

        options_row = QHBoxLayout()
        self._loop_check = QCheckBox(I18N.tr("edit.adr.loop_cue", "Loop cue recording"), box)
        self._monitor_check = QCheckBox(I18N.tr("edit.adr.monitor", "Input monitor"), box)
        options_row.addWidget(self._loop_check)
        options_row.addWidget(self._monitor_check)
        options_row.addStretch(1)
        form.addRow(options_row)

        self._count_in_check = QCheckBox(I18N.tr("edit.adr.count_in_beeps", "Count-in beeps"), box)
        form.addRow(self._count_in_check)

        calib_row = QHBoxLayout()
        self._calib_label = QLabel(I18N.tr("edit.adr.uncalibrated", "Latency: not calibrated"), box)
        self._calibrate_btn = QPushButton(I18N.tr("edit.adr.calibrate", "Calibrate"), box)
        self._calibrate_btn.clicked.connect(self.calibrate_requested.emit)
        calib_row.addWidget(self._calib_label, stretch=1)
        calib_row.addWidget(self._calibrate_btn)
        form.addRow(calib_row)

        self._pre_roll_spin.valueChanged.connect(
            lambda _v: self.settings_changed.emit())
        self._post_roll_spin.valueChanged.connect(
            lambda _v: self.settings_changed.emit())
        self._device_combo.currentIndexChanged.connect(
            lambda _idx: self.settings_changed.emit())
        self._channels_combo.currentIndexChanged.connect(
            lambda _idx: self.settings_changed.emit())
        self._loop_check.toggled.connect(lambda _on: self.settings_changed.emit())
        self._monitor_check.toggled.connect(lambda _on: self.settings_changed.emit())

        # AUD-19403: 设备枚举（QMediaDevices.audioInputs()）是同步系统调用，
        # 不再在面板构造期直接执行 —— 延迟到事件循环跑起来后一次性补扫，
        # 构造函数保持零阻塞；⟳ 按钮保留手动重扫入口。
        QTimer.singleShot(0, self.reload_devices)
        return box

    def _build_cue_list(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self._cue_table = QTableWidget(0, 4, page)
        self._cue_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._cue_table.setVerticalScrollBar(CompactVerticalScrollBar())
        self._cue_table.setHorizontalHeaderLabels([
            I18N.tr("edit.adr.cue_in", "In"),
            I18N.tr("edit.adr.cue_out", "Out"),
            I18N.tr("edit.adr.character", "Character"),
            I18N.tr("edit.adr.dialog", "Dialog"),
        ])
        self._cue_table.verticalHeader().hide()
        cue_header = self._cue_table.horizontalHeader()
        for column in range(3):
            cue_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        cue_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._cue_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._cue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._cue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._cue_table.itemSelectionChanged.connect(self._on_cue_selection_changed)
        self._cue_table.itemDoubleClicked.connect(self._edit_cue)
        layout.addWidget(self._cue_table)

        actions = QHBoxLayout()
        self._add_cue_btn = QPushButton(I18N.tr("edit.adr.add_cue", "Add Cue"), page)
        self._edit_cue_btn = QPushButton(I18N.tr("edit.adr.edit_cue", "Edit Cue"), page)
        self._remove_cue_btn = QPushButton(I18N.tr("edit.adr.remove_cue", "Remove Cue"), page)
        self._add_cue_btn.clicked.connect(self._add_cue)
        self._edit_cue_btn.clicked.connect(self._edit_cue)
        self._remove_cue_btn.clicked.connect(self._remove_cue)
        for button in (self._add_cue_btn, self._edit_cue_btn, self._remove_cue_btn):
            actions.addWidget(button)
        layout.addLayout(actions)
        return page

    def _build_record_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 4, 4)
        layout.setSpacing(6)

        track_row = QFormLayout()
        self._track_combo = QComboBox(page)
        self._track_combo.currentIndexChanged.connect(self._update_session_actions)
        track_row.addRow(I18N.tr("edit.adr.record_track", "Record Track"), self._track_combo)
        layout.addLayout(track_row)

        actions = QHBoxLayout()
        self._playhead_record_btn = QPushButton(I18N.tr(
            "edit.effects.record_at_playhead", "Record at Playhead",
            use_english=I18N.get_current_locale() != "zh_CN",
        ), page)
        self._playhead_record_btn.setToolTip(I18N.tr(
            "edit.effects.record_at_playhead_tip",
            "Record to the selected audio track, or use the timeline's audio target.",
            use_english=I18N.get_current_locale() != "zh_CN",
        ))
        self._playhead_record_btn.clicked.connect(
            lambda: self.playhead_record_requested.emit(self._track_combo.currentData() or "")
        )
        self._playhead_record_btn.hide()
        self._stop_btn = QPushButton(I18N.tr("edit.adr.stop", "Stop"), page)
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        actions.addWidget(self._playhead_record_btn, 1)
        actions.addWidget(self._stop_btn)
        layout.addLayout(actions)
        layout.addWidget(self._build_status_row())

        self._record_cue_combo = QComboBox(page)
        self._record_cue_combo.setAccessibleName(I18N.tr("edit.adr.list_tab", "Cues"))
        self._record_cue_combo.setPlaceholderText(I18N.tr("edit.adr.select_cue", "Select a cue"))
        self._record_cue_combo.activated.connect(self._cue_table.selectRow)
        layout.addWidget(self._record_cue_combo)
        self._prompt_label = QLabel(page)
        self._prompt_label.setTextFormat(Qt.TextFormat.PlainText)
        self._prompt_label.setWordWrap(True)
        layout.addWidget(self._prompt_label)

        actions = QHBoxLayout()
        self._rehearse_btn = QPushButton(I18N.tr("edit.adr.rehearse", "Rehearse"), page)
        self._record_btn = QPushButton(I18N.tr("edit.adr.record_cue", "Record Cue"), page)
        self._rehearse_btn.clicked.connect(self._rehearse_cue)
        self._record_btn.clicked.connect(self._record_cue)
        for button in (self._rehearse_btn, self._record_btn):
            actions.addWidget(button)
        layout.addLayout(actions)
        layout.addWidget(self._build_take_table(), stretch=1)
        layout.addLayout(self._build_action_row())
        return page

    def _build_status_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        self._state_label = QLabel("—", row)
        self._state_label.setStyleSheet("font-weight: bold;")
        self._level_bar = InputLevelBar(row)
        layout.addWidget(self._state_label)
        layout.addSpacing(12)
        layout.addWidget(self._level_bar, stretch=1)
        return row

    def _build_take_table(self) -> QTableWidget:
        self._table = QTableWidget(0, len(self._COLS), self)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setVerticalScrollBar(CompactVerticalScrollBar())
        self._table.setHorizontalHeaderLabels([
            I18N.tr(f"edit.adr.col_{key.lower().replace(' ', '_')}", key)
            for key in self._COLS
        ])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.itemDoubleClicked.connect(self._on_audition_clicked)
        return self._table

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._audition_btn = QPushButton(I18N.tr("edit.adr.audition", "Audition"), self)
        self._audition_btn.clicked.connect(self._on_audition_clicked)
        self._mark_best_btn = QPushButton(I18N.tr("edit.adr.mark_best", "Mark Best"), self)
        self._mark_best_btn.clicked.connect(self._on_mark_best)
        self._replace_btn = QPushButton(I18N.tr("edit.adr.replace_clip", "Replace Clip"), self)
        self._replace_btn.clicked.connect(self._on_replace_clip)
        for btn in (self._audition_btn, self._mark_best_btn, self._replace_btn):
            row.addWidget(btn)
        return row

    def set_adr_context(self, timeline: TimelineSequence | None) -> None:
        """Refresh the cue list and eligible destinations from the active timeline."""
        if self._timeline is timeline:
            return
        previous_id = self._timeline.timeline_id if self._timeline is not None else None
        timeline_id = timeline.timeline_id if timeline is not None else None
        current = self.current_cue()
        selected_id = current.cue_id if current is not None and previous_id == timeline_id else None
        track_id = self._track_combo.currentData() if previous_id == timeline_id else None
        self._timeline = timeline
        self._cues = read_adr_cues(timeline) if timeline is not None else ()

        with QSignalBlocker(self._track_combo):
            self._track_combo.clear()
            self._track_combo.addItem(I18N.tr("edit.adr.select_track", "Select a track"), None)
            if timeline is not None:
                for track in timeline.audio_tracks:
                    if not track.is_bus and not track.is_locked:
                        self._track_combo.addItem(track.name or track.track_id, track.track_id)
            self._track_combo.setCurrentIndex(max(0, self._track_combo.findData(track_id)))
        with QSignalBlocker(self._cue_table), QSignalBlocker(self._record_cue_combo):
            self._cue_table.setRowCount(len(self._cues))
            self._record_cue_combo.clear()
            selected_row = 0
            for index, cue in enumerate(self._cues):
                timecode = self._frame_to_timecode(cue.in_frame)
                self._record_cue_combo.addItem(f"{timecode} · {cue.character or cue.dialog}", cue.cue_id)
                for column, value in enumerate((timecode, self._frame_to_timecode(cue.out_frame),
                                                cue.character, cue.dialog)):
                    self._cue_table.setItem(index, column, QTableWidgetItem(value))
                if cue.cue_id == selected_id:
                    selected_row = index
            if self._cues:
                self._cue_table.selectRow(selected_row)
                self._record_cue_combo.setCurrentIndex(selected_row)
        self._refresh_cue_selection()

    def set_timeline_frame(self, frame: int) -> None:
        self._current_frame = max(0, frame)

    def current_cue(self) -> AdrCue | None:
        row = self._cue_table.currentRow()
        if 0 <= row < len(self._cues):
            return self._cues[row]
        return None

    def set_count_in(self, seconds: int) -> None:
        cue = self.current_cue()
        lines = [value for value in (cue.character, cue.dialog) if value] if cue is not None else []
        if seconds > 0:
            lines.insert(0, I18N.tr("edit.adr.count_in", "Starts in {seconds}").format(seconds=seconds))
        self._prompt_label.setText("\n".join(lines))

    def _on_cue_selection_changed(self) -> None:
        self._refresh_cue_selection()
        cue = self.current_cue()
        if cue is not None and self._record_state in {"idle", "failed"}:
            self.cue_selected.emit(cue)

    def _refresh_cue_selection(self) -> None:
        with QSignalBlocker(self._record_cue_combo):
            self._record_cue_combo.setCurrentIndex(self._cue_table.currentRow())
        self.set_count_in(0)
        self._refresh_table()
        self._update_session_actions()

    def _update_session_actions(self) -> None:
        ready = self._record_state in {"idle", "failed"}
        has_cue = self.current_cue() is not None
        self._cue_table.setEnabled(ready)
        self._record_cue_combo.setEnabled(ready)
        self._track_combo.setEnabled(ready)
        self._playhead_record_btn.setEnabled(ready and self._timeline is not None)
        self._add_cue_btn.setEnabled(ready and self._timeline is not None)
        self._edit_cue_btn.setEnabled(ready and has_cue)
        self._remove_cue_btn.setEnabled(ready and has_cue)
        self._rehearse_btn.setEnabled(ready and has_cue)
        self._record_btn.setEnabled(ready and has_cue and bool(self._track_combo.currentData()))
        self._stop_btn.setEnabled(self._record_state in {"rehearsing", "pre_roll", "recording"})
        self._audition_btn.setEnabled(ready)

    def _frame_to_timecode(self, frame: int) -> str:
        fps = float(self._timeline.fps) if self._timeline is not None else 24.0
        return frames_to_timecode(frame, fps, drop_frame=is_drop_frame_rate(fps))

    def _add_cue(self) -> None:
        timeline = self._timeline
        if timeline is None:
            return
        start, end = timeline.range_start, timeline.range_end
        if start is None or end is None or end <= start:
            start = self._current_frame
            end = start + round(float(timeline.fps) * 5)
        self._show_cue_editor(AdrCue(str(uuid.uuid4()), start, end), new=True)

    def _edit_cue(self) -> None:
        cue = self.current_cue()
        if cue is not None:
            self._show_cue_editor(cue, new=False)

    def _remove_cue(self) -> None:
        cue = self.current_cue()
        if cue is not None:
            self.cue_removed.emit(cue.cue_id)

    def _show_cue_editor(self, cue: AdrCue, *, new: bool) -> None:
        timeline = self._timeline
        if timeline is None:
            return
        fps = float(timeline.fps)
        dialog = QDialog(self)
        key, title = ("add_cue", "Add Cue") if new else ("edit_cue", "Edit Cue")
        dialog.setWindowTitle(I18N.tr(f"edit.adr.{key}", title))
        form = QFormLayout(dialog)
        in_edit = QLineEdit(self._frame_to_timecode(cue.in_frame), dialog)
        out_edit = QLineEdit(self._frame_to_timecode(cue.out_frame), dialog)
        character_edit = QLineEdit(cue.character, dialog)
        dialog_edit = QPlainTextEdit(cue.dialog, dialog)
        form.addRow(I18N.tr("edit.adr.cue_in", "In"), in_edit)
        form.addRow(I18N.tr("edit.adr.cue_out", "Out"), out_edit)
        form.addRow(I18N.tr("edit.adr.character", "Character"), character_edit)
        form.addRow(I18N.tr("edit.adr.dialog", "Dialog"), dialog_edit)
        error = QLabel(dialog)
        error.setWordWrap(True)
        form.addRow(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog)

        def apply() -> None:
            try:
                in_tc, out_tc = in_edit.text().strip(), out_edit.text().strip()
                if not in_tc or not out_tc:
                    raise ValueError("Missing timecode")
                start = timecode_to_frames(in_tc, fps, drop_frame=";" in in_tc)
                end = timecode_to_frames(out_tc, fps, drop_frame=";" in out_tc)
                updated = AdrCue(cue.cue_id, start, end, character_edit.text().strip(), dialog_edit.toPlainText())
            except ValueError:
                error.setText(I18N.tr("edit.adr.invalid_timecodes", "Enter valid timecodes with Out after In."))
                return
            dialog.accept()
            if self._timeline is not None and self._timeline.timeline_id == timeline.timeline_id:
                self.cue_changed.emit(updated)
                row = next((i for i, item in enumerate(self._cues) if item.cue_id == updated.cue_id), -1)
                if row >= 0:
                    self._cue_table.selectRow(row)

        buttons.accepted.connect(apply)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        dialog.exec()

    def _rehearse_cue(self) -> None:
        cue = self.current_cue()
        if cue is not None:
            self._audition.stop()
            self.rehearse_requested.emit(cue)

    def _record_cue(self) -> None:
        cue = self.current_cue()
        if cue is not None:
            self._audition.stop()
            self.record_requested.emit(cue)

    # ── 设置（控制器读取）──

    def adr_settings(self) -> Dict[str, Any]:
        """当前输入 patch / 录音设置（供控制器构建 RecordingConfig）。"""
        device_data = self._device_combo.currentData()
        return {
            "device_id": (str(device_data) if device_data else None),
            "channels": int(self._channels_combo.currentData() or 1),
            "pre_roll_seconds": float(self._pre_roll_spin.value()),
            "post_roll_seconds": float(self._post_roll_spin.value()),
            "loop": self._loop_check.isChecked(),
            "monitor": self._monitor_check.isChecked(),
            "record_track_id": self._track_combo.currentData(),
            "count_in_beeps": self._count_in_check.isChecked(),
        }

    def apply_settings(self, values: Dict[str, Any]) -> None:
        """§7 P1：外部（混音侧录音准备弹窗）回填设置——不发信号。

        面板是录音设置的展示面；混音通道条的录音准备弹窗提交后经此
        同步，两个表面读同一组值。
        """
        for combo, key, default in (
                (self._device_combo, "device_id", ""), (self._channels_combo, "channels", 1)):
            if key in values:
                index = combo.findData(values[key] or default)
                if index >= 0:
                    with QSignalBlocker(combo):
                        combo.setCurrentIndex(index)
        for spinner, key in ((self._pre_roll_spin, "pre_roll_seconds"),
                             (self._post_roll_spin, "post_roll_seconds")):
            value = values.get(key)
            if isinstance(value, (int, float)) and np.isfinite(value):
                with QSignalBlocker(spinner):
                    spinner.setValue(float(value))
        for checkbox, key in ((self._loop_check, "loop"), (self._monitor_check, "monitor")):
            if isinstance(values.get(key), bool):
                with QSignalBlocker(checkbox):
                    checkbox.setChecked(values[key])

    def reload_devices(self) -> None:
        """枚举输入设备（默认项 = 系统默认设备）。

        AUD-19403: 构造后的延迟补扫与本方法（⟳ 手动刷新）都在事件循环期
        执行 —— 枚举环境相对构造期可能已变化（如测试替身替换核心类），
        失败只降级为 System Default 并记日志，不向上抛。
        """
        from src.features.edit.core.recording_manager import RecordingManager

        current = self._device_combo.currentData()
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        self._device_combo.addItem(
            I18N.tr("edit.adr.default_device", "System Default"), "")
        try:
            devices = RecordingManager.list_input_devices()
        except (AttributeError, RuntimeError, OSError) as exc:
            logger.warning(f"Failed to enumerate input devices: {exc}")
            devices = []
        for device_id, description in devices:
            self._device_combo.addItem(description, device_id)
        if current:
            index = self._device_combo.findData(current)
            if index >= 0:
                self._device_combo.setCurrentIndex(index)
        self._device_combo.blockSignals(False)

    # ── 会话状态（控制器写入）──

    def set_recording_state(self, state: str) -> None:
        self._record_state = state
        key, default = _STATE_LABELS.get(state, ("edit.adr.state_idle", "Ready"))
        self._state_label.setText(I18N.tr(key, default))
        self._state_label.setStyleSheet(
            f"font-weight: bold; color: {_STATE_COLORS.get(state, _STATE_COLORS['idle']).name()};")
        if state in ("idle", "failed"):
            self._level_bar.reset()
            self.set_count_in(0)
        self._update_session_actions()

    def set_input_level(self, dbfs: float) -> None:
        self._level_bar.set_level(dbfs)

    def set_calibration(self, calibration: Any) -> None:
        """显示延迟校准结果（input_ms ≤10ms 合格）。"""
        if calibration is None:
            self._calib_label.setText(
                I18N.tr("edit.adr.uncalibrated", "Latency: not calibrated"))
            self._calib_label.setStyleSheet("")
            return
        input_ms = float(getattr(calibration, "input_latency_ms", 0.0) or 0.0)
        ok = input_ms <= 10.0
        text = I18N.tr(
            "edit.adr.calibrated", "Latency: {ms:.1f} ms input").format(ms=input_ms)
        self._calib_label.setText(text + ("" if ok else " ⚠"))
        color = "#7cb342" if ok else "#e53935"
        self._calib_label.setStyleSheet(f"color: {color};")

    def set_monitor_enabled(self, enabled: bool) -> None:
        """输入监听开关（控制器在采集开始/结束后调用）。"""
        self._monitor_check.blockSignals(True)
        self._monitor_check.setChecked(enabled)
        self._monitor_check.blockSignals(False)

    # ── 输入监听回放（控制器接线）──

    def begin_monitor(self, sample_rate: int, channels: int) -> bool:
        """开启输入监听回放；返回采集回调可用的写入函数或 None。"""
        return self._monitor_sink.start(int(sample_rate), int(channels))

    def monitor_write_block(self):
        """on_monitor_block 的目标（未激活时 None）。"""
        return self._monitor_sink.write_block if self._monitor_sink.active else None

    def end_monitor(self) -> None:
        self._monitor_sink.stop()

    # ── take sheet ──

    def _ensure_sheet(self) -> "TakeSheet":
        if self._take_sheet is None:
            from src.features.edit.core.take_sheet import TakeSheet
            self._take_sheet = TakeSheet()
        return self._take_sheet

    def set_take_sheet(self, sheet: Any) -> None:
        """整表替换（如从工程重建）。"""
        self._take_sheet = sheet
        self._refresh_table()

    def add_take_entry(self, take: Any, asset_id: str = "",
                       track_id: str = "") -> None:
        """新增/更新一行 take（录音完成回调时调用）。"""
        self._ensure_sheet().add_take(take, asset_id=asset_id, track_id=track_id)
        self._refresh_table(select_take_id=take.take_id)

    def take_sheet(self) -> "TakeSheet | None":
        return self._take_sheet

    def current_take_id(self) -> str:
        item = self._table.item(self._table.currentRow(), 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _refresh_table(self, select_take_id: str = "") -> None:
        sheet = self._take_sheet
        entries = sheet.entries if sheet is not None else []
        cue = self.current_cue()
        if cue is not None and not self._caption_workspace:
            entries = [entry for entry in entries if entry.scene_name == cue.scene_name]
        # 审计修复：未指定选择目标时保持当前选择（重建后 ★/替换/试听
        # 按钮不至于静默失效）
        if not select_take_id:
            select_take_id = self.current_take_id()
        self._table.setRowCount(0)
        select_row = -1
        for row, entry in enumerate(entries):
            self._table.insertRow(row)
            peak = ("—" if entry.peak_dbfs == float("-inf")
                    else f"{entry.peak_dbfs:.1f}")
            status = I18N.tr(f"edit.adr.take_status.{entry.status}", entry.status) if entry.status else "—"
            values = (
                str(entry.take_number),
                entry.name or entry.file_path,
                f"{entry.duration_seconds:.1f}s",
                peak,
                entry.start_timecode or "—",
                status,
                "★" if entry.is_best else "",
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.take_id)
                if col == 6 and entry.is_best:
                    item.setForeground(QColor("#ffb300"))
                self._table.setItem(row, col, item)
            if select_take_id and entry.take_id == select_take_id:
                select_row = row
        if select_row >= 0:
            self._table.selectRow(select_row)

    # ── 操作 ──

    def _current_entry(self):
        sheet = self._take_sheet
        if sheet is None:
            return None
        return sheet.entry_for_take(self.current_take_id())

    def _on_audition_clicked(self, *_args) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        if self._audition.is_playing():
            self._audition.stop()
            return
        self._audition.play(entry.file_path)

    def _on_mark_best(self) -> None:
        take_id = self.current_take_id()
        if take_id:
            self.mark_best_requested.emit(take_id)

    def _on_replace_clip(self) -> None:
        take_id = self.current_take_id()
        if take_id:
            self.replace_clip_requested.emit(take_id)

    def mark_best_locally(self, take_id: str) -> None:
        """命令执行后同步面板标记（以工程为准的回显）。"""
        self._ensure_sheet().mark_best(take_id)
        self._refresh_table()


__all__ = [
    "AdrTakeSheetPanel", "InputLevelBar", "InputMonitorSink",
    "TakeAuditionPlayer",
]
