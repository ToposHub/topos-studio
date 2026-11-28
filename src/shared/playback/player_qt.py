import os
import time
import subprocess
import unicodedata
import threading

import logging
logger = logging.getLogger(__name__)
from PIL import Image
from PyQt6.QtCore import QEvent, QPoint, pyqtSignal, Qt, QTimer, QUrl, QRectF
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QPushButton, QHBoxLayout, QSlider, QLabel, QWidget, QLineEdit, QMenu, QSizePolicy
from PyQt6.QtGui import QCursor, QEnterEvent, QFont, QMouseEvent, QPainter, QImage, QDesktopServices
from src.shared.i18n import I18N
from .base_player import (
    PLAYBACK_RESOLUTION_DIVISORS, TRANSPORT_IDLE_MS, _fmt_time,
    playback_resolution_menu, transport_hot_zone, utils_get_tool_path,
)
from .thumb_cache import thumbnail_path

# 播放器基于 Qt Multimedia（PyQt6 自带 FFmpeg 后端），打包后无需用户安装 VLC。
from PyQt6.QtMultimedia import QMediaFormat, QMediaMetaData

# 播放器 UI 样式
STYLESHEET = """
    QFrame#FixedHeader {
        background-color: rgba(0, 0, 0, 0.8); border: none;
    }
    QFrame#ControlsBar {
        background-color: rgba(0, 0, 0, 0.8); border: none; border-radius: 10px;
    }
    QFrame#FloatingPanel {
        background-color: rgba(0, 0, 0, 0.8); border: none; border-radius: 8px;
    }
    QLabel { color: rgba(255, 255, 255, 0.9); font-family: -apple-system, sans-serif; background: transparent; }
    QLabel#Title { font-weight: 600; font-size: 13px; background: transparent; }

    QLabel.MetaBadge {
        background: transparent; color: rgba(255, 255, 255, 0.5);
        padding: 0 8px; font-size: 11px; font-weight: 600; font-family: monospace;
        border: none;
    }
    QSlider { background: transparent; }
    QSlider::groove:horizontal { height: 3px; background: rgba(255, 255, 255, 0.2); border-radius: 1px; }
    QSlider::handle:horizontal { background: #fff; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
    QSlider::sub-page:horizontal { background: #0A84FF; border-radius: 1px; }
    QPushButton { background: transparent; border: none; padding: 5px; border-radius: 6px; color: rgba(255, 255, 255, 0.8); }
    QPushButton:hover { background: rgba(255, 255, 255, 0.15); color: #fff; }
    QLineEdit { background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.15); color: #fff; border-radius: 6px; padding: 2px 5px; }
    QPushButton#BtnPrimary { background: #0A84FF; color: white; padding: 4px 12px; font-weight: 600; border-radius: 6px; font-size: 12px;}
"""

class _VideoCanvas(QWidget):
    """自绘视频帧画布。

    不用 QVideoWidget：它是原生窗口层（macOS NSView），会压住非原生的兄弟
    控件——顶部工具栏/底部进度条从此在视频下面再也 raise 不上来。自绘画布
    和控件同属普通层级，工具栏/进度条始终可见。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: black;")
        self.setMouseTracking(True)
        self._img = None

    def setFrame(self, img: QImage):
        self._img = img
        self.update()

    def clear(self):
        self._img = None
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._img is not None and not self._img.isNull():
            # 目标矩形直绘（与 _ToposCanvas 同路径）：4K 源每帧不再
            # 分配/重采样中间图，缩放由 drawImage 一次完成
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            iw, ih = self._img.width(), self._img.height()
            scale = min(self.width() / iw, self.height() / ih)
            tw, th = iw * scale, ih * scale
            p.drawImage(QRectF((self.width() - tw) / 2, (self.height() - th) / 2,
                               tw, th), self._img)
        # [BUG-P3-59] 此前不像基类画布那样 p.end()（Painter 生命周期悬空）
        p.end()


class UltimatePlayer(QFrame):
    request_prev = pyqtSignal()
    request_next = pyqtSignal()
    closed = pyqtSignal()
    play_failed = pyqtSignal(str)
    # [BUG-P2-32] 工作线程 toast 经信号投递 GUI 线程（P0-07 同款修复）
    toast_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mediaplayer = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.mediaplayer.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.8)

        self.current_path = ""
        self.duration = 0
        self.is_playing = False
        self.is_clipping_mode = False
        self._lightbox_mode = False
        self._transport_hot = False
        self._preview_divisor = 1
        self._decoded_image: QImage | None = None
        self._source_size: tuple[int, int] | None = None
        # [E3/FUP-07] 自然播完（EndOfMedia）与用户停止分离：
        # 只有前者置位 _at_eof——片尾按播放=从头重播，用户停止后再按=原地续播
        self._at_eof = False

        self.setup_ui()
        self.setStyleSheet(STYLESHEET)

        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.update_ui_state)

        self.setMouseTracking(True)
        self.video_frame.setMouseTracking(True)
        for w in (self.video_frame, self.header_container, self.controls_container):
            w.installEventFilter(self)
        self.hide_cursor_timer = QTimer(self)
        self.hide_cursor_timer.setInterval(TRANSPORT_IDLE_MS)
        self.hide_cursor_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.hide_cursor_timer.setSingleShot(True)
        self.hide_cursor_timer.timeout.connect(self.hide_ui)
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.setInterval(1500)
        self._startup_timer.timeout.connect(self._check_playback_started)
        self.ui_visible = True

        # 播放失败（无法解码 / 无效媒体）只在本界面报错，绝不转交系统播放器
        self.mediaplayer.errorOccurred.connect(self._on_player_error)
        self.mediaplayer.mediaStatusChanged.connect(self._on_media_status)
        self.mediaplayer.playbackStateChanged.connect(self._on_playback_state)
        self._error_handled = False
        self.toast_requested.connect(self.show_toast)

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.video_frame = _VideoCanvas(self); self.video_frame.mousePressEvent = self.on_video_click; self.video_frame.lower()
        self.video_frame.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.video_frame.customContextMenuRequested.connect(
            lambda point: self.open_playback_settings(self.video_frame.mapToGlobal(point)))
        self.video_sink = QVideoSink(self)
        self.video_sink.videoFrameChanged.connect(self._on_video_frame)
        self.mediaplayer.setVideoSink(self.video_sink)
        # 顶部工具栏
        self.header_container = QFrame(self); self.header_container.setObjectName("FixedHeader")
        self.header_layout = QHBoxLayout(self.header_container); self.header_layout.setContentsMargins(20, 0, 20, 0)
        self.lbl_title = QLabel("Video Player")
        self.lbl_title.setObjectName("Title")
        self.lbl_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.header_layout.addWidget(self.lbl_title, 1)
        self.meta_badge_container = QWidget(); self.meta_layout = QHBoxLayout(self.meta_badge_container); self.meta_layout.setContentsMargins(0,0,0,0); self.meta_layout.setSpacing(5)
        # 容器是普通 QWidget，会被宿主全局 QWidget{background:#27272b} 垫成
        # 一条灰带；内联透明压掉（文字标签自身已透明）
        self.meta_badge_container.setStyleSheet("background: transparent;")
        self.header_layout.addWidget(self.meta_badge_container)
        from . import icons as _icons
        self.btn_cover = _icons.make_tool_button(self, "fa5s.image", "设为封面", "🖼")
        self.btn_cover.clicked.connect(self.set_as_cover)
        self.btn_clip = _icons.make_tool_button(self, "fa5s.cut", "截取", "✂")
        self.btn_clip.clicked.connect(self.toggle_clip_mode)
        self.btn_snap = _icons.make_tool_button(self, "fa5s.camera", "截图", "📷")
        self.btn_snap.clicked.connect(self.snapshot)
        btn_close = _icons.make_tool_button(self, "fa5s.times", "关闭", "✕", color=_icons.WHITE)
        btn_close.clicked.connect(self.stop)
        for b in (self.btn_cover, self.btn_clip, self.btn_snap, btn_close):
            self.header_layout.addWidget(b)
        # 底部控制栏
        self.controls_container = QFrame(self); self.controls_container.setObjectName("ControlsBar")
        self.ctrl_layout = QHBoxLayout(self.controls_container); self.ctrl_layout.setContentsMargins(15, 0, 15, 0)
        from . import icons as _icons
        self.btn_play = self.create_btn("", "播放/暂停", self.toggle_play); self.btn_play.setFixedSize(30, 30)
        _icons.set_play_icon(self.btn_play, playing=False)
        self.lbl_curr = QLabel("00:00"); self.slider = QSlider(Qt.Orientation.Horizontal); self.slider.setRange(0, 1000)
        self.slider.setMinimumWidth(1)
        self.slider.actionTriggered.connect(self._on_slider_action)
        self.slider.sliderPressed.connect(self.on_slider_pressed); self.slider.sliderReleased.connect(self.on_slider_released); self.slider.sliderMoved.connect(self.on_slider_moved); self.is_seeking = False
        self.lbl_total = QLabel("00:00"); self.slider_vol = QSlider(Qt.Orientation.Horizontal); self.slider_vol.setRange(0, 100); self.slider_vol.setValue(80); self.slider_vol.setFixedWidth(60)
        self.slider_vol.valueChanged.connect(lambda v: self.audio_output.setVolume(v / 100.0))
        self.lbl_vol = QLabel()
        _vol = _icons.icon("fa5s.volume-up")
        if _vol is not None:
            self.lbl_vol.setPixmap(_vol.pixmap(13, 13))
        else:
            self.lbl_vol.setText("🔊")
        for widget in (self.btn_play, self.lbl_curr, self.slider, self.lbl_total,
                       self.lbl_vol, self.slider_vol):
            self.ctrl_layout.addWidget(widget, 1 if widget is self.slider else 0)
        self.btn_playback_settings = _icons.make_tool_button(
            self, "fa5s.cog", I18N.tr("playback_resolution.settings", "播放设置"), "⚙", size=14)
        self.btn_playback_settings.clicked.connect(
            lambda: self.open_playback_settings(
                self.btn_playback_settings.mapToGlobal(self.btn_playback_settings.rect().topRight())))
        self.ctrl_layout.addWidget(self.btn_playback_settings)
        self.lbl_playback_resolution = QLabel(self)
        self.lbl_playback_resolution.setStyleSheet(
            "background: rgba(0, 0, 0, 0.72); color: #eee;"
            "padding: 2px 6px; border-radius: 4px; font-size: 11px;")
        self.lbl_playback_resolution.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.lbl_playback_resolution.hide()
        # 视频剪切面板
        self.clip_panel = QFrame(self); self.clip_panel.setObjectName("FloatingPanel"); self.clip_panel.hide()
        self.clip_layout = QHBoxLayout(self.clip_panel); self.clip_layout.setContentsMargins(15, 5, 15, 5)
        self.inp_start = QLineEdit(); self.inp_start.setPlaceholderText("0.0"); self.inp_start.setFixedWidth(60)
        self.inp_end = QLineEdit(); self.inp_end.setPlaceholderText("0.0"); self.inp_end.setFixedWidth(60)
        for w in [QLabel("Start:"), self.inp_start, self.create_btn("设入点", "Set", lambda: self.set_clip_time('start')), QLabel("End:"), self.inp_end, self.create_btn("设出点", "Set", lambda: self.set_clip_time('end'))]: self.clip_layout.addWidget(w)
        btn_do_clip = QPushButton("Cut"); btn_do_clip.setObjectName("BtnPrimary"); btn_do_clip.clicked.connect(self.execute_clip); self.clip_layout.addWidget(btn_do_clip)

    def resizeEvent(self, event):
        w, h = self.width(), self.height()
        self.video_frame.setGeometry(0, 0, w, h)
        self.header_container.setGeometry(0, 0, w, 50)
        self.header_container.raise_()
        ctrl_w = max(1, min(w - 16, max(560, min(int(w * 0.7), 1200))))
        self.meta_badge_container.setVisible(w >= 580)
        self.controls_container.setGeometry((w - ctrl_w)//2, h - 70, ctrl_w, 50)
        self.controls_container.raise_()
        clip_w = 400
        self.clip_panel.setGeometry((w - clip_w)//2, h - 140, clip_w, 50)
        self.clip_panel.raise_()
        self._layout_playback_resolution()
        super().resizeEvent(event)

    def create_btn(self, text, tooltip, func):
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.clicked.connect(func)
        btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        font = QFont("Arial", 12)
        btn.setFont(font)
        return btn

    def play_video(self, path: str) -> None:
        self._startup_timer.stop()
        self.mediaplayer.stop()
        self.video_frame.clear()
        self._decoded_image = None
        self._source_size = None
        self._preview_divisor = 1
        self.lbl_playback_resolution.hide()
        self.current_path = unicodedata.normalize("NFC", path)
        self.duration = 0
        self._error_handled = False
        self._at_eof = False
        self.is_seeking = False
        self._toast_gen = getattr(self, '_toast_gen', 0) + 1
        self.slider.setValue(0)
        self.lbl_curr.setText("00:00")
        self.lbl_total.setText("00:00")
        self.lbl_title.setStyleSheet("")
        self._real_title = os.path.basename(path)
        self.lbl_title.setText(self._real_title)
        self.lbl_title.setToolTip(self._real_title)
        self.clear_badges()
        self.clip_panel.hide()
        self.is_clipping_mode = False
        self.mediaplayer.setSource(QUrl.fromLocalFile(self.current_path))
        if self._error_handled:
            return
        self.show()
        self.raise_()
        self.setFocus()
        self._wake_ui()
        self.timer.start()
        self._startup_timer.start()
        self.mediaplayer.play()

    def _check_playback_started(self) -> None:
        try:
            if self._error_handled or not self.current_path:
                return
            status = self.mediaplayer.mediaStatus()
            # play() 已调用 1.5s 仍未开播：加载级失败（Loaded+Stopped+0ms = 播放未生效）
            stalled = (status == QMediaPlayer.MediaStatus.LoadedMedia
                       and self.mediaplayer.playbackState() == QMediaPlayer.PlaybackState.StoppedState
                       and self.mediaplayer.position() == 0)
            failed = status in (QMediaPlayer.MediaStatus.InvalidMedia,
                                QMediaPlayer.MediaStatus.NoMedia)
            if failed or stalled:
                self._show_play_error("内置解码器无法播放该文件")
        except Exception as e:
            logger.warning(f"播放状态兜底检查异常（已忽略）: {e}")

    def _show_play_error(self, reason):
        """失败只停在自己这里：黑屏 + 标题栏红字提示；系统播放器仅能右键手动打开"""
        if self._error_handled or not self.current_path:
            return
        self._error_handled = True
        logger.warning(f"内置播放器无法播放: {self.current_path} ({reason})")
        self.mediaplayer.stop()
        self.timer.stop()
        self.is_playing = False
        from . import icons as _icons; _icons.set_play_icon(self.btn_play, playing=False)
        self.lbl_title.setText("⚠ 无法播放，请右键文件选择系统播放器打开")
        self.lbl_title.setStyleSheet("color: #FF453A; font-weight: bold;")
        self.play_failed.emit(reason)

    def _on_player_error(self, error, error_string):
        self._show_play_error(error_string or "解码失败")

    def _on_media_status(self, status):
        # 元数据就绪后刷新徽标
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            QTimer.singleShot(300, self.update_metadata)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._show_play_error("媒体格式不受支持")
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            # [E3/FUP-07] 自然播完：EndOfMedia 才是"实际呈现完成"的权威信号
            # （随后到来的 StoppedState 不再与之混淆）；片尾重播语义见 toggle_play
            self._at_eof = True

    def _on_video_frame(self, frame):
        img = frame.toImage()
        if self.current_path and not img.isNull():
            self._decoded_image = img
            self._show_preview_frame()

    def _layout_playback_resolution(self) -> None:
        label = self.lbl_playback_resolution
        label.adjustSize()
        label.move(8, max(0, self.height() - label.height() - 3))
        label.raise_()

    def _show_preview_frame(self) -> None:
        image = self._decoded_image
        if image is None:
            return
        decoded_size = (image.width(), image.height())
        native_size = f"{decoded_size[0]}×{decoded_size[1]}"
        divisor = self._preview_divisor
        if divisor > 1:
            image = image.scaled(
                (image.width() + divisor - 1) // divisor,
                (image.height() + divisor - 1) // divisor,
                Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            text = I18N.tr("playback_resolution.qt_scaled",
                           "预览 {preview} · 解码 {decoded}",
                           preview=f"{image.width()}×{image.height()}", decoded=native_size)
        elif self._source_size is None:
            text = I18N.tr("playback_resolution.qt_decoded", "播放 {decoded} · 解码尺寸",
                           decoded=native_size)
        elif decoded_size in (self._source_size, self._source_size[::-1]):
            text = I18N.tr("playback_resolution.original", "播放 {frame} · 原分辨率",
                           frame=native_size)
        else:
            text = I18N.tr("playback_resolution.reduced", "播放 {frame} · 源 {source} · 降分辨率",
                           frame=native_size, source=f"{self._source_size[0]}×{self._source_size[1]}")
        self.video_frame.setFrame(image)
        label = self.lbl_playback_resolution
        if text != label.text() or label.isHidden():
            label.setText(text)
            label.show()
            self._layout_playback_resolution()

    def _set_preview_resolution(self, divisor: int) -> None:
        if divisor not in PLAYBACK_RESOLUTION_DIVISORS:
            raise ValueError(f"Unsupported preview divisor: {divisor}")
        if divisor != self._preview_divisor:
            self._preview_divisor = divisor
            self._show_preview_frame()

    def _build_resolution_menu(self) -> QMenu:
        menu = playback_resolution_menu(
            self, I18N.tr("playback_resolution.qt_menu", "预览分辨率（解码后缩放）"),
            self._preview_divisor, self._set_preview_resolution)
        info = menu.addMenu(I18N.tr("playback_resolution.info", "视频信息"))
        if self._decoded_image is not None:
            if self._source_size is not None:
                source = f"{self._source_size[0]}×{self._source_size[1]}"
                info.addAction(I18N.tr("playback_resolution.qt_source", "源 {source}",
                                       source=source)).setEnabled(False)
            info.addAction(self.lbl_playback_resolution.text()).setEnabled(False)
        else:
            info.addAction(I18N.tr("playback_resolution.qt_pending", "等待播放帧")).setEnabled(False)
        return menu

    def open_playback_settings(self, global_position: QPoint) -> None:
        if not self.current_path:
            return
        self._wake_ui()
        self.hide_cursor_timer.stop()
        menu = self._build_resolution_menu()
        try:
            menu.exec(global_position)
        finally:
            menu.deleteLater()
            self.observe_transport_pointer(QCursor.pos())
            self._wake_ui()

    def _on_playback_state(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            from . import icons as _icons; _icons.set_play_icon(self.btn_play, playing=True)
            self.is_playing = True
            self._wake_ui()
        elif state == QMediaPlayer.PlaybackState.PausedState:
            from . import icons as _icons; _icons.set_play_icon(self.btn_play, playing=False)
            self.is_playing = False
            self._wake_ui()
        elif state == QMediaPlayer.PlaybackState.StoppedState:
            # [BUG-P1-13] 自然播完走 StoppedState：此前不处理导致 is_playing
            # 恒 True——播放键停留"暂停"图标、控件照常自动隐藏、空格误走暂停分支
            from . import icons as _icons; _icons.set_play_icon(self.btn_play, playing=False)
            self.is_playing = False
            self._wake_ui()

    def clear_badges(self):
        for i in reversed(range(self.meta_layout.count())):
            self.meta_layout.itemAt(i).widget().setParent(None)

    def update_metadata(self):
        self.clear_badges()
        info = []
        try:
            meta = self.mediaplayer.metaData()
            resolution = meta.value(QMediaMetaData.Key.Resolution)
            if resolution is not None and resolution.width() > 0:
                self._source_size = (resolution.width(), resolution.height())
                info.append(f"{resolution.width()}x{resolution.height()}")
                self._show_preview_frame()
            fps = meta.value(QMediaMetaData.Key.VideoFrameRate)
            if fps:
                try: fps = float(fps)
                except (TypeError, ValueError): fps = 0
                if fps > 0: info.append(f"{int(round(fps))}fps")
            codec_value = meta.value(QMediaMetaData.Key.VideoCodec)
            codec = ""
            if codec_value is not None:
                try: codec = (QMediaFormat.videoCodecName(codec_value) or "").lower()
                except Exception: codec = str(codec_value).lower()
            if codec in ['avc1', 'h264']: codec = 'H.264'
            elif codec in ['hvc1', 'hevc']: codec = 'H.265'
            elif codec in ['apcn', 'apch', 'prores']: codec = 'ProRes'
            if codec: info.append(str(codec).upper())
        except Exception:
            pass
        try:
            sz = os.path.getsize(self.current_path)
            if sz < 1024**3: info.append(f"{round(sz/1024/1024, 1)} MB")
            else: info.append(f"{round(sz/1024**3, 2)} GB")
        except OSError: pass
        for text in info:
            lbl = QLabel(text)
            lbl.setProperty("class", "MetaBadge")
            # 内联透明：主程序全局 QWidget{background:#27272b} 会给每个标签垫小色块
            lbl.setStyleSheet("background: transparent; border: none;")
            self.meta_layout.addWidget(lbl)

    def stop(self):
        was_visible = self.isVisible()
        self.current_path = ""
        self._startup_timer.stop()
        self.mediaplayer.stop()
        self.mediaplayer.setSource(QUrl())
        self.timer.stop()
        self.video_frame.clear()
        self._decoded_image = None
        self.lbl_playback_resolution.hide()
        self._transport_hot = False
        # [BUG-P2-31] 此前未停 hide_cursor_timer：关闭后每 3s 触发 hide_ui
        # 空转（与基类 stop 行为不一致）
        self.hide_cursor_timer.stop()
        self.hide()
        self.is_playing = False
        if self.parent(): self.parent().setFocus()
        if was_visible:
            self.closed.emit()

    def toggle_play(self):
        if self.is_playing:
            self.mediaplayer.pause()
        else:
            # [E3] 片尾（EndOfMedia 已到）按播放 → 从头重播；
            # 用户中途停止后再按 → 原地续播（与 topos/libav 语义对齐）
            if self._at_eof:
                self._at_eof = False
                self.mediaplayer.setPosition(0)
            self.mediaplayer.play()

    def _capture_frame_ffmpeg(self, input_path, output_path, at_time_sec):
        """通用 FFmpeg 截图函数"""
        ffmpeg_bin = utils_get_tool_path('ffmpeg')
        if not ffmpeg_bin:
            self.show_toast("FFmpeg not found")
            return False

        cmd = [
            ffmpeg_bin, '-y',
            '-ss', str(at_time_sec),
            '-i', input_path,
            '-vframes', '1',
            '-q:v', '2',
            output_path
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg Error: {e}")
            return False

    def snapshot(self):
        ffmpeg_bin = utils_get_tool_path('ffmpeg')
        if not ffmpeg_bin:
            self.show_toast("FFmpeg not found")
            return

        folder = os.path.dirname(self.current_path)
        curr_time = self.mediaplayer.position() / 1000.0
        final_name = f"snapshot_{int(time.time())}.jpg"
        final_path = os.path.join(folder, final_name)
        if self._capture_frame_ffmpeg(self.current_path, final_path, curr_time):
            self.show_toast(f"截图成功: {final_name}")
        else:
            self.show_toast("截图失败")

    def set_as_cover(self):
        """利用引擎策略生成封面：优先本地，只读则兜底全局"""
        # 1. 通过引擎获取目标路径 (它会自动判断是存 .cache 还是 .assetdeck)
        target_small = thumbnail_path(self.current_path, 'small')
        target_large = thumbnail_path(self.current_path, 'large')

        # 2. 确保目标目录存在
        try:
            os.makedirs(os.path.dirname(target_small), exist_ok=True)
        except OSError:
            pass

        # 3. 截图到临时文件 (为了原子写入，避免半途失败)
        temp_dir = os.path.dirname(target_small)
        temp_jpg = os.path.join(temp_dir, f"temp_cover_{int(time.time())}.jpg")

        curr_time = self.mediaplayer.position() / 1000.0

        if self._capture_frame_ffmpeg(self.current_path, temp_jpg, curr_time):
            # 异步处理转换
            QTimer.singleShot(10, lambda: self._process_cover(temp_jpg, target_small, target_large))
        else:
            self.show_toast("封面截取失败")

    def _process_cover(self, temp_path, target_small, target_large):
        try:
            if os.path.exists(temp_path):
                img = Image.open(temp_path)

                # 生成 Large (600px)
                img_lg = img.copy()
                img_lg.thumbnail((600, 600))
                img_lg.save(target_large, 'WEBP', quality=90)

                # 生成 Small (300px)
                img.thumbnail((300, 300))
                img.save(target_small, 'WEBP', quality=80)

                img.close()
                os.remove(temp_path)

                self.show_toast("封面已更新")

                # 通知前端刷新
                # 注意：路径需转义，确保为 NFC
                js_path = unicodedata.normalize('NFC', self.current_path).replace('\\', '/').replace("'", "\\'")
                js_code = f"refreshThumbnail('{js_path}');"
                if self.parent():
                    self.parent().trigger_js(js_code)
        except Exception as e:
            logger.error(f"Cover Error: {e}")
            self.show_toast("封面处理失败")

    def execute_clip(self):
        start = self.inp_start.text()
        end = self.inp_end.text()
        if not start or not end: return
        if getattr(self, '_clip_job', None) is not None and \
                self._clip_job.is_alive() and not self._clip_job.cancelled:
            self._clip_job.cancel()   # 再点一次 = 取消进行中的截取
            self.show_toast("正在取消截取…")
            return
        folder, filename = os.path.split(self.current_path)
        name_stem, ext = os.path.splitext(filename)
        out_name = f"{name_stem}_clip_{int(time.time())}{ext}"
        out_path = os.path.join(folder, out_name)
        ffmpeg_bin = utils_get_tool_path('ffmpeg')
        if not ffmpeg_bin:
            self.show_toast("FFmpeg not found")
            return
        # [E6] 预算/取消/有界输出/原子交付统一走 ClipExportJob（此前
        # 直接写目标路径 + 无预算不可取消 + capture_output 无上限）
        from .clip_export import ClipExportJob
        job = ClipExportJob(
            [ffmpeg_bin, '-y', '-ss', start, '-to', end, '-i',
             self.current_path, '-c', 'copy', '-avoid_negative_ts', '1'],
            destination=out_path)
        self._clip_job = job
        self.show_toast("正在截取…（可再点一次取消）")

        def on_done(ok: bool, message: str) -> None:
            # [BUG-P2-32] 结果经信号回 GUI 线程（此前 except: pass 永久沉默）
            if ok:
                self.toast_requested.emit(f"截取完成: {out_name}")
                QDesktopServices.openUrl(QUrl.fromLocalFile(out_path))
            else:
                logger.error("截取未完成: %s | ffmpeg 尾部输出: %s",
                             message, job.stderr_tail[-300:])
                self.toast_requested.emit(f"截取未完成：{message}")
        job.start(on_done)
        self.toggle_clip_mode()

    def toggle_clip_mode(self):
        self.is_clipping_mode = not self.is_clipping_mode
        self.clip_panel.setVisible(self.is_clipping_mode)

    def set_clip_time(self, type_):
        curr = self.mediaplayer.position() / 1000.0
        txt = f"{curr:.2f}"
        if type_ == 'start': self.inp_start.setText(txt)
        else: self.inp_end.setText(txt)

    def show_toast(self, text):
        # [U10] 代次令牌 + 真实标题还原（照搬 base_player 修法）：截取流程
        # "正在截取..."→"截取完成"两次叠加时，旧 singleShot 捕获的标题会把
        # toast 文案"复活"成永久标题
        self._toast_gen = getattr(self, '_toast_gen', 0) + 1
        gen = self._toast_gen
        self.lbl_title.setText(f"✓ {text}")
        self.lbl_title.setStyleSheet("color: #0A84FF; font-weight: bold; background: transparent; border: none;")
        QTimer.singleShot(2500, lambda: self.reset_title(None, gen))
    def reset_title(self, text=None, gen=-1):
        if gen != -1 and gen != getattr(self, '_toast_gen', gen):
            return  # 已有更新的 toast 接管标题
        try:
            self.lbl_title.setText(text if text is not None
                                   else getattr(self, '_real_title', self.lbl_title.text()))
            self.lbl_title.setStyleSheet("")
        except RuntimeError:
            # [E6] toast 后 2.5s 内关窗：标签随窗口销毁，迟到计时器无事可做
            pass

    def update_ui_state(self):
        # [BUG-P2-33] 去掉 not is_playing 短路：右键帧进先 pause()，此后
        # 时间标签/进度条冻结（与基类不一致）。is_seeking 拖动中仍跳过。
        if self.is_seeking: return
        duration = self.mediaplayer.duration()
        if duration > 0 and duration != self.duration:
            self.duration = duration
            self.lbl_total.setText(_fmt_time(duration // 1000))
        curr_ms = self.mediaplayer.position()
        if duration > 0:
            self.lbl_curr.setText(_fmt_time(curr_ms // 1000))
            self.slider.setValue(int(curr_ms / duration * 1000))

    def on_slider_pressed(self): self.is_seeking = True
    def _on_slider_action(self, action: int) -> None:
        if action != QSlider.SliderAction.SliderMove.value:
            self._seek_from_slider()

    def _seek_from_slider(self) -> None:
        duration = self.mediaplayer.duration()
        if duration > 0:
            position = self.slider.sliderPosition() / self.slider.maximum()
            self.mediaplayer.setPosition(int(position * duration))
            self._at_eof = False

    def on_slider_released(self) -> None:
        self._seek_from_slider()
        self.is_seeking = False

    def on_slider_moved(self, val):
        duration = self.mediaplayer.duration()
        if duration > 0:
            sec = (val / 1000.0) * (duration / 1000.0)
            self.lbl_curr.setText(_fmt_time(int(sec)))

    def on_video_click(self, event):
        if event.button() == Qt.MouseButton.LeftButton: self.toggle_play()

    def keyPressEvent(self, event):
        self._wake_ui()
        key = event.key()
        if key == Qt.Key.Key_Space: self.toggle_play()
        elif key == Qt.Key.Key_Escape: self.stop()
        elif key == Qt.Key.Key_Right:
            # 逐帧前进（约 40ms 步进，与左键后退对齐）
            self.mediaplayer.pause()
            self.mediaplayer.setPosition(min(self.mediaplayer.position() + 40, self.mediaplayer.duration()))
        elif key == Qt.Key.Key_Left:
            t = self.mediaplayer.position() - 40
            self.mediaplayer.setPosition(max(t, 0))
        elif key == Qt.Key.Key_Up: self.request_prev.emit()
        elif key == Qt.Key.Key_Down: self.request_next.emit()

    def eventFilter(self, obj, ev):
        if isinstance(ev, (QMouseEvent, QEnterEvent)):
            self.observe_transport_pointer(ev.globalPosition().toPoint())
        kind = ev.type()
        if (kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.Wheel)
                or (not self._lightbox_mode and kind in (QEvent.Type.Enter, QEvent.Type.MouseMove))):
            self._wake_ui()
        return super().eventFilter(obj, ev)

    def observe_transport_pointer(self, global_position: QPoint) -> None:
        hot = transport_hot_zone(self, global_position)
        if hot != self._transport_hot:
            self._transport_hot = hot
            self._wake_ui()

    def leave_transport(self) -> None:
        if self._transport_hot:
            self._transport_hot = False
            self._wake_ui()

    def leaveEvent(self, event: QEvent) -> None:
        self.leave_transport()
        super().leaveEvent(event)

    def set_lightbox_tools(self, _visible: bool) -> None:
        """Leave the lightbox header with the host and manage transport locally."""
        self._lightbox_mode = True
        self.header_container.hide()

    def _wake_ui(self) -> None:
        self.ui_visible = True
        if not self._lightbox_mode:
            self.header_container.show()
            self.header_container.raise_()
        self.controls_container.show()
        self.controls_container.raise_()
        self.clip_panel.raise_()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self.is_playing and not self._transport_hot:
            self.hide_cursor_timer.start()
        else:
            self.hide_cursor_timer.stop()

    def mouseMoveEvent(self, event):
        if not self._lightbox_mode:
            self._wake_ui()
        super().mouseMoveEvent(event)

    def hide_ui(self):
        # 仅播放中隐藏；暂停常显；截取面板打开时不藏
        if not self.is_playing or self.clip_panel.isVisible() or self._transport_hot:
            return
        self.ui_visible = False
        self.header_container.hide()
        self.controls_container.hide()
        self.setCursor(Qt.CursorShape.BlankCursor)
