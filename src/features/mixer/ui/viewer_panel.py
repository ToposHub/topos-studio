"""Video-monitor placeholder and explicit silent UI transport."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QElapsedTimer, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QHideEvent, QPainter, QPaintEvent, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QVBoxLayout, QWidget,
)

from src.features.edit.ui.edit_style import EDIT_COLORS
from src.shared.assets.icon_manager import (
    IconColors, icon_folder_open, icon_jump_end, icon_jump_start, icon_loop,
    icon_pause, icon_play, icon_step_back, icon_step_forward, icon_stop,
)
from src.shared.ui.style_sheet import UI_FONT_FAMILIES
from .workspace_style import PANEL_QSS, compact_combo, tool_button, toolbar_divider
from .session import DISPLAY_RATES, MixerUiSession


class PicturePreview(QWidget):
    def __init__(self, session: MixerUiSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.picture = QPixmap()
        self.caption = "未载入参考画面"
        self.setMinimumSize(180, 80)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#17171B"))
        if not self.picture.isNull():
            scaled = self.picture.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2, scaled)
        else:
            painter.setPen(QColor("#A4A4B2"))
            painter.setFont(QFont(UI_FONT_FAMILIES, 11))
            painter.drawText(self.rect().adjusted(10, 10, -10, -10), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.caption)


class ViewerPanel(QWidget):
    def __init__(self, session: MixerUiSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setObjectName("MixerViewer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_QSS)
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.clock = QElapsedTimer()
        self._last_ns = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(5)
        header = QFrame()
        header.setObjectName("MixerToolbar")
        header.setFixedHeight(32)
        options = QHBoxLayout(header)
        options.setContentsMargins(6, 0, 6, 0)
        options.setSpacing(6)
        options.addWidget(tool_button("打开参考画面", icon_folder_open(), self._choose_reference))
        options.addStretch()
        self.format_combo = compact_combo()
        self.format_combo.addItems(["时间码 / 帧", "秒", "小节 / 拍"])
        self.format_combo.setFixedWidth(96)
        self.format_combo.setToolTip("时间线标尺单位")
        self.format_combo.currentIndexChanged.connect(self._format)
        options.addWidget(self.format_combo)
        self.fps_combo = compact_combo()
        self.fps_combo.addItems([rate.label for rate in DISPLAY_RATES])
        self.fps_combo.setCurrentIndex(1)
        self.fps_combo.setFixedWidth(100)
        self.fps_combo.setToolTip("界面时间码帧率，不改变素材时长")
        self.fps_combo.currentIndexChanged.connect(session.set_rate)
        options.addWidget(self.fps_combo)
        layout.addWidget(header)
        self.preview = PicturePreview(session)
        layout.addWidget(self.preview, 1)
        transport_bar = QFrame()
        transport_bar.setObjectName("MixerToolbar")
        transport_bar.setFixedHeight(36)
        transport = QHBoxLayout(transport_bar)
        transport.setContentsMargins(6, 0, 6, 0)
        transport.setSpacing(2)
        transport.addStretch()
        transport.addWidget(tool_button("回到起点", icon_jump_start(), lambda: session.seek(0), glyph=15))
        transport.addWidget(tool_button("前一帧", icon_step_back(), lambda: self.step(-1), glyph=15))
        self.play = tool_button("走带预览（无声）", icon_play(EDIT_COLORS["accent"]), size=29, glyph=16)
        self.play.setCheckable(True)
        self.play.toggled.connect(self._play)
        transport.addWidget(self.play)
        transport.addWidget(tool_button("停止", icon_stop(), self.stop_preview, glyph=13))
        transport.addWidget(tool_button("后一帧", icon_step_forward(), lambda: self.step(1), glyph=15))
        transport.addWidget(tool_button("跳到结尾", icon_jump_end(), lambda: session.seek(session.duration), glyph=15))
        self.loop = tool_button("循环选区", icon_loop(), glyph=15)
        self.loop.setCheckable(True)
        self.loop.toggled.connect(self._loop)
        transport.addWidget(self.loop)
        transport.addWidget(toolbar_divider())
        self.timecode = QLineEdit()
        self.timecode.setFixedWidth(104)
        self.timecode.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timecode.setToolTip("输入时间码定位，DF 使用分号")
        self.timecode.setStyleSheet("font: 12px 'Menlo'; color: #D8D9DE; background: transparent; border: none;")
        self.timecode.returnPressed.connect(self._seek_text)
        transport.addWidget(self.timecode)
        transport.addStretch()
        layout.addWidget(transport_bar)
        meters = QGridLayout()
        for column, (name, unit) in enumerate((("Integrated", "LUFS"), ("Short-term", "LUFS"), ("True Peak", "dBTP"))):
            title, value = QLabel(name), QLabel(f"— {unit}")
            title.setStyleSheet("color: #9696A4; font-size: 10px;")
            value.setStyleSheet("font: 11px 'Menlo';")
            meters.addWidget(title, 0, column)
            meters.addWidget(value, 1, column)
        layout.addLayout(meters)
        self.status = QLabel("UI 预览 · 音频 / 视频 / 表计后端未连接")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #A5A5B1; font-size: 10px;")
        layout.addWidget(self.status)
        session.playhead_changed.connect(self.sync_timecode)
        session.timeline_format_changed.connect(self.sync_timecode)
        self.sync_timecode()

    def sync_timecode(self, *_args: object) -> None:
        if not self.timecode.hasFocus():
            self.timecode.setText(self.session.rate.format(self.session.playhead))

    def _seek_text(self) -> None:
        try:
            seconds = self.session.rate.parse(self.timecode.text())
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.timecode.clearFocus()
        self.session.seek(seconds)

    def step(self, direction: int) -> None:
        self.stop_preview()
        frame = max(0, int(self.session.playhead * self.session.fps + 1e-7) + direction)
        self.session.seek(frame / self.session.fps)

    def _loop(self, checked: bool) -> None:
        self.session.loop_enabled = checked
        self.loop.setIcon(icon_loop(EDIT_COLORS["accent"] if checked else IconColors.DEFAULT))
        self.session.touch()

    def _format(self, index: int) -> None:
        self.session.time_format = ("timecode", "seconds", "bars")[index]
        self.session.timeline_format_changed.emit()

    def _play(self, enabled: bool) -> None:
        self.session.playing = enabled
        glyph = icon_pause if enabled else icon_play
        self.play.setIcon(glyph(EDIT_COLORS["accent"]))
        if enabled:
            self.clock.start()
            self._last_ns = 0
            self.timer.start()
        else:
            self.timer.stop()

    def _tick(self) -> None:
        now = self.clock.nsecsElapsed()
        next_time = self.session.playhead + max(0, now - self._last_ns) / 1_000_000_000
        self._last_ns = now
        if self.session.loop_enabled and self.session.loop_end > self.session.loop_start:
            if next_time >= self.session.loop_end:
                next_time = self.session.loop_start + (next_time - self.session.loop_start) % (self.session.loop_end - self.session.loop_start)
        elif next_time >= self.session.duration:
            self.stop_preview()
        self.session.seek(next_time)

    def stop_preview(self) -> None:
        self.timer.stop()
        self.play.setChecked(False)
        self.session.playing = False

    def hideEvent(self, event: QHideEvent) -> None:
        self.stop_preview()
        super().hideEvent(event)

    def _choose_reference(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择参考画面", "", "媒体 (*.png *.jpg *.jpeg *.webp *.mp4 *.mov *.mkv);;所有文件 (*)")
        if not path:
            return
        if Path(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            picture = QPixmap(path)
            if picture.isNull():
                self.status.setText("无法读取该图片")
                return
            self.preview.picture = picture
            self.status.setText(f"参考图片：{Path(path).name} · 静态画面")
        else:
            self.preview.picture = QPixmap()
            self.preview.caption = f"{Path(path).name}\n已选择视频；解码与同步等待后端接线"
            self.status.setText("视频后端未连接，未开始播放")
        self.preview.update()
