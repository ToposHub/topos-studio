"""Standalone native audio viewer using Qt's shared media backend."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget
from src.shared.playback.audio_arbiter import shared_arbiter


class AudioPreviewWindow(QDialog):
    closed = pyqtSignal()
    takeover = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("音频预览")
        self.resize(640, 180)
        self._owner = f"audio-preview:{id(self)}"
        self._player = QMediaPlayer(self)
        self._output = QAudioOutput(self)
        self._player.setAudioOutput(self._output)
        self._output.setVolume(0.8)
        root = QVBoxLayout(self)
        self.title = QLabel("请选择音频文件")
        root.addWidget(self.title)
        self.position = QSlider(Qt.Orientation.Horizontal)
        root.addWidget(self.position)
        row = QHBoxLayout()
        self.play_button = QPushButton("播放 / 暂停")
        stop = QPushButton("停止")
        row.addWidget(self.play_button)
        row.addWidget(stop)
        root.addLayout(row)
        self.notice = QLabel("")
        root.addWidget(self.notice)
        self._player.durationChanged.connect(lambda value: self.position.setRange(0, value))
        self._player.positionChanged.connect(self._position_changed)
        self._player.errorOccurred.connect(lambda error, text: self.notice.setText(text))
        self.position.sliderMoved.connect(self._player.setPosition)
        self.play_button.clicked.connect(self.toggle)
        stop.clicked.connect(self.stop)
        self.takeover.connect(self.stop, Qt.ConnectionType.QueuedConnection)

    def set_paths(self, paths: tuple[str, ...]) -> None:
        path = next((p for p in paths if p), "")
        if not path:
            raise ValueError("请先选择一个音频文件")
        if not Path(path).is_file():
            raise ValueError("音频文件不存在")
        self.stop()
        self.title.setText(Path(path).name)
        self._player.setSource(QUrl.fromLocalFile(path))
        self.notice.setText("已载入，按播放开始")

    def _position_changed(self, value: int) -> None:
        if not self.position.isSliderDown():
            self.position.setValue(value)

    def toggle(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            shared_arbiter().release(self._owner)
        else:
            shared_arbiter().acquire(self._owner, self.takeover.emit)
            self._player.play()

    def stop(self) -> None:
        self._player.stop()
        shared_arbiter().release(self._owner)

    def closeEvent(self, event: Any) -> None:
        self.stop()
        self._player.setSource(QUrl())
        self.closed.emit()
        super().closeEvent(event)
