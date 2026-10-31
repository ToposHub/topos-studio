# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Video Control Panel UI
视频播放控制面板
"""
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QSlider, QLabel, QToolButton
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from src.shared.i18n.i18n import I18N


class VideoControlPanel(QWidget):
    """
    视频播放控制面板

    信号（与下方 pyqtSignal 声明一致；AUD-38100 修正文档契约）:
        seek_requested(int) - 请求跳转到指定帧
        play_toggled() - 播放/暂停切换（无参数）
        next_frame() - 下一帧
        prev_frame() - 上一帧
        first_frame() - 跳转到首帧
        last_frame() - 跳转到末帧
    """

    # 信号
    seek_requested = pyqtSignal(int)
    play_toggled = pyqtSignal()
    next_frame = pyqtSignal()
    prev_frame = pyqtSignal()
    first_frame = pyqtSignal()
    last_frame = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.total_frames = 0
        self.current_frame = 0
        self.is_playing = False

        self._setup_ui()
        self._update_display()

    def _setup_ui(self):
        """设置 UI"""
        # 主布局
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(4)

        # 控制按钮行
        control_layout = QHBoxLayout()
        control_layout.setSpacing(4)

        # 首帧按钮
        self.btn_first = QToolButton()
        self.btn_first.setText("⏮")
        self.btn_first.setToolTip(I18N.tr("video.first_frame", "跳到第一帧"))
        self.btn_first.clicked.connect(self.first_frame.emit)
        self.btn_first.setMinimumWidth(32)

        # 上一帧按钮
        self.btn_prev = QToolButton()
        self.btn_prev.setText("◀")
        self.btn_prev.setToolTip(I18N.tr("video.prev_frame", "上一帧 (←)"))
        self.btn_prev.clicked.connect(self.prev_frame.emit)
        self.btn_prev.setMinimumWidth(32)

        # 播放/暂停按钮
        self.btn_play = QPushButton()
        self.btn_play.setText(I18N.tr("video.play", "▶ 播放"))
        self.btn_play.setToolTip(I18N.tr("video.play_pause", "播放/暂停 (Space)"))
        self.btn_play.setMinimumWidth(80)
        self.btn_play.clicked.connect(self.play_toggled.emit)

        # 下一帧按钮
        self.btn_next = QToolButton()
        self.btn_next.setText("▶")
        self.btn_next.setToolTip(I18N.tr("video.next_frame", "下一帧 (→)"))
        self.btn_next.clicked.connect(self.next_frame.emit)
        self.btn_next.setMinimumWidth(32)

        # 末帧按钮
        self.btn_last = QToolButton()
        self.btn_last.setText("⏭")
        self.btn_last.setToolTip(I18N.tr("video.last_frame", "跳到最后一帧"))
        self.btn_last.clicked.connect(self.last_frame.emit)
        self.btn_last.setMinimumWidth(32)

        # 添加到控制布局
        control_layout.addWidget(self.btn_first)
        control_layout.addWidget(self.btn_prev)
        control_layout.addWidget(self.btn_play)
        control_layout.addWidget(self.btn_next)
        control_layout.addWidget(self.btn_last)
        control_layout.addStretch()

        main_layout.addLayout(control_layout)

        # 进度条行
        slider_layout = QHBoxLayout()
        slider_layout.setSpacing(8)

        # 帧滑块
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(0)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(10)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.valueChanged.connect(self._on_slider_changed)

        # 时间标签
        self.label_time = QLabel("00:00:00 / 00:00:00")
        self.label_time.setMinimumWidth(140)
        self.label_time.setStyleSheet("color: #888;")

        # 帧号标签
        self.label_frame = QLabel(I18N.tr("video.frame_initial", "帧: 0 / 0"))
        self.label_frame.setMinimumWidth(80)
        self.label_frame.setStyleSheet("color: #888;")

        slider_layout.addWidget(self.slider, 1)
        slider_layout.addWidget(self.label_frame)
        slider_layout.addWidget(self.label_time)

        main_layout.addLayout(slider_layout)

        # 设置样式
        self.setStyleSheet("""
            VideoControlPanel {
                background: #2a2a2a;
                border-top: 1px solid #444;
            }
            QPushButton, QToolButton {
                background: #3a3a3a;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 4px 8px;
                color: #ddd;
            }
            QPushButton:hover, QToolButton:hover {
                background: #4a4a4a;
            }
            QPushButton:pressed, QToolButton:pressed {
                background: #2a2a2a;
            }
        """)

        self.setLayout(main_layout)

        # 滑块拖动状态
        self._slider_dragging = False

    def set_total_frames(self, total: int):
        """设置总帧数"""
        self.total_frames = total
        self.slider.setMaximum(max(0, total - 1))
        self._update_display()

    def set_current_frame(self, frame: int):
        """设置当前帧"""
        self.current_frame = frame
        if not self._slider_dragging:
            self.slider.blockSignals(True)
            self.slider.setValue(frame)
            self.slider.blockSignals(False)
        self._update_display()

    def set_playing(self, playing: bool):
        """设置播放状态"""
        self.is_playing = playing
        if playing:
            self.btn_play.setText(I18N.tr("video.pause", "⏸ 暂停"))
        else:
            self.btn_play.setText(I18N.tr("video.play", "▶ 播放"))

    def set_time_info(self, time_str: str):
        """设置时间信息"""
        self.label_time.setText(time_str)

    def _update_display(self):
        """更新显示"""
        frame_text = I18N.tr("video.frame_count", f"帧: {self.current_frame + 1} / {max(1, self.total_frames)}")
        self.label_frame.setText(frame_text)

    def _on_slider_pressed(self):
        """滑块按下"""
        self._slider_dragging = True

    def _on_slider_released(self):
        """滑块释放"""
        self._slider_dragging = False
        self.seek_requested.emit(self.slider.value())

    def _on_slider_changed(self, value: int):
        """滑块值变化"""
        if self._slider_dragging:
            self.label_frame.setText(f"帧: {value + 1} / {max(1, self.total_frames)}")

    @pyqtSlot(bool)
    def on_play_state_changed(self, is_playing: bool):
        """播放状态变化"""
        self.set_playing(is_playing)

    @pyqtSlot(int)
    def on_frame_changed(self, frame: int):
        """帧变化"""
        self.set_current_frame(frame)

    def set_enabled(self, enabled: bool):
        """设置面板是否启用"""
        self.btn_first.setEnabled(enabled)
        self.btn_prev.setEnabled(enabled)
        self.btn_play.setEnabled(enabled)
        self.btn_next.setEnabled(enabled)
        self.btn_last.setEnabled(enabled)
        self.slider.setEnabled(enabled)
