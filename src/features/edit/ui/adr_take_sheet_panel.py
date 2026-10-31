"""ADR Take Sheet 面板（P1-10）。

专业 ADR/VO 录音控制台：
- 输入 patch：设备 / 声道（mono/stereo）选择
- 录音设置：pre-roll / post-roll / 循环录音 / 输入监听
- 延迟校准状态（≤10ms 合格）
- 输入电平表（实时 dBFS）
- take 列表：时长 / 峰值 / 入点时间码 / 状态 / 最佳 take ★
- 操作：试听（PyAV 解码 + QAudioSink 播放）、标记最佳、替换所选片段

面板只发信号和暴露设置读取接口，录音编排（RecordingManager、命令栈、
时间线替换）由 edit_controller 完成——与混音器 R 按钮共用同一条链路。
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from src.features.edit.core.take_sheet import TakeSheet

import numpy as np
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)

_STATE_LABELS = {
    "idle": ("edit.adr.state_idle", "Ready"),
    "pre_roll": ("edit.adr.state_preroll", "Pre-roll…"),
    "recording": ("edit.adr.state_recording", "● REC"),
    "stopping": ("edit.adr.state_postroll", "Post-roll…"),
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

    _COLS = ("#", "Take", "Length", "Peak", "In TC", "Status", "Best")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._take_sheet = None  # lazy: core.TakeSheet
        self._audition = TakeAuditionPlayer(self)
        self._monitor_sink = InputMonitorSink(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        root.addWidget(self._build_settings_group())
        root.addWidget(self._build_status_row())
        root.addWidget(self._build_take_table(), stretch=1)
        root.addLayout(self._build_action_row())

        self.set_recording_state("idle")

    # ── 构建 UI ──

    def _build_settings_group(self) -> QWidget:
        from PyQt6.QtWidgets import QCheckBox, QGroupBox

        box = QGroupBox(I18N.tr("edit.adr.settings", "ADR Input"), self)
        form = QFormLayout(box)

        self._device_combo = QComboBox(box)
        self._refresh_btn = QPushButton(I18N.tr("edit.adr.refresh", "⟳"), box)
        self._refresh_btn.setFixedWidth(28)
        self._refresh_btn.setToolTip(I18N.tr("edit.adr.refresh_tip", "Refresh devices"))
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
        self._loop_check = QCheckBox(I18N.tr("edit.adr.loop", "Loop record"), box)
        self._monitor_check = QCheckBox(I18N.tr("edit.adr.monitor", "Input monitor"), box)
        options_row.addWidget(self._loop_check)
        options_row.addWidget(self._monitor_check)
        options_row.addStretch(1)
        form.addRow("", options_row)

        calib_row = QHBoxLayout()
        self._calib_label = QLabel(I18N.tr("edit.adr.uncalibrated", "Latency: not calibrated"), box)
        self._calibrate_btn = QPushButton(I18N.tr("edit.adr.calibrate", "Calibrate"), box)
        self._calibrate_btn.clicked.connect(self.calibrate_requested.emit)
        calib_row.addWidget(self._calib_label, stretch=1)
        calib_row.addWidget(self._calibrate_btn)
        form.addRow("", calib_row)

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
        self._audition_btn = QPushButton(I18N.tr("edit.adr.audition", "▶ Audition"), self)
        self._audition_btn.clicked.connect(self._on_audition_clicked)
        self._mark_best_btn = QPushButton(I18N.tr("edit.adr.mark_best", "★ Mark Best"), self)
        self._mark_best_btn.clicked.connect(self._on_mark_best)
        self._replace_btn = QPushButton(I18N.tr("edit.adr.replace_clip", "Replace Selected Clip"), self)
        self._replace_btn.clicked.connect(self._on_replace_clip)
        for btn in (self._audition_btn, self._mark_best_btn, self._replace_btn):
            row.addWidget(btn)
        return row

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
        }

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
        key, default = _STATE_LABELS.get(state, ("edit.adr.state_idle", "Ready"))
        self._state_label.setText(I18N.tr(key, default))
        self._state_label.setStyleSheet(
            f"font-weight: bold; color: {_STATE_COLORS.get(state, _STATE_COLORS['idle']).name()};")
        if state in ("idle", "failed"):
            self._level_bar.reset()

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
            values = (
                str(entry.take_number),
                entry.name or entry.file_path,
                f"{entry.duration_seconds:.1f}s",
                peak,
                entry.start_timecode or "—",
                entry.status or "—",
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
