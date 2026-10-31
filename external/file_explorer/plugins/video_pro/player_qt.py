import os
import sys
import time
import subprocess
import unicodedata
import threading

import logging
logger = logging.getLogger(__name__)
from PIL import Image
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QUrl, QRectF
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QPushButton, QHBoxLayout, QSlider, QLabel, QWidget, QLineEdit
from PyQt6.QtGui import QCursor, QFont, QPainter, QImage
import utils
from thumb_engine import engine

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
        self.hide_cursor_timer.setInterval(3000)
        self.hide_cursor_timer.timeout.connect(self.hide_ui)
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
        self.video_sink = QVideoSink(self)
        self.video_sink.videoFrameChanged.connect(self._on_video_frame)
        self.mediaplayer.setVideoSink(self.video_sink)
        # 顶部工具栏
        self.header_container = QFrame(self); self.header_container.setObjectName("FixedHeader")
        self.header_layout = QHBoxLayout(self.header_container); self.header_layout.setContentsMargins(20, 0, 20, 0)
        self.lbl_title = QLabel("Video Player"); self.lbl_title.setObjectName("Title"); self.header_layout.addWidget(self.lbl_title); self.header_layout.addSpacing(15)
        self.meta_badge_container = QWidget(); self.meta_layout = QHBoxLayout(self.meta_badge_container); self.meta_layout.setContentsMargins(0,0,0,0); self.meta_layout.setSpacing(5)
        # 容器是普通 QWidget，会被宿主全局 QWidget{background:#27272b} 垫成
        # 一条灰带；内联透明压掉（文字标签自身已透明）
        self.meta_badge_container.setStyleSheet("background: transparent;")
        self.header_layout.addWidget(self.meta_badge_container); self.header_layout.addStretch()
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
        self.slider.sliderPressed.connect(self.on_slider_pressed); self.slider.sliderReleased.connect(self.on_slider_released); self.slider.sliderMoved.connect(self.on_slider_moved); self.is_seeking = False
        self.lbl_total = QLabel("00:00"); self.slider_vol = QSlider(Qt.Orientation.Horizontal); self.slider_vol.setRange(0, 100); self.slider_vol.setValue(80); self.slider_vol.setFixedWidth(60)
        self.slider_vol.valueChanged.connect(lambda v: self.audio_output.setVolume(v / 100.0))
        self.lbl_vol = QLabel()
        _vol = _icons.icon("fa5s.volume-up")
        if _vol is not None:
            self.lbl_vol.setPixmap(_vol.pixmap(13, 13))
        else:
            self.lbl_vol.setText("🔊")
        for w in [self.btn_play, self.lbl_curr, self.slider, self.lbl_total, self.lbl_vol, self.slider_vol]: self.ctrl_layout.addWidget(w)
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
        ctrl_w = min(w - 120, 1600)  # 加长播放条，拖拽进度更好用
        self.controls_container.setGeometry((w - ctrl_w)//2, h - 70, ctrl_w, 50)
        self.controls_container.raise_()
        clip_w = 400
        self.clip_panel.setGeometry((w - clip_w)//2, h - 140, clip_w, 50)
        self.clip_panel.raise_()
        super().resizeEvent(event)

    def create_btn(self, text, tooltip, func):
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.clicked.connect(func)
        btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        font = QFont("Arial", 12)
        btn.setFont(font)
        return btn

    def play_video(self, path):
        # Standardize path to NFC immediately
        self.current_path = unicodedata.normalize('NFC', path)
        self.duration = 0
        self._error_handled = False
        self.lbl_total.setText("00:00")
        self.lbl_title.setStyleSheet("")
        self.mediaplayer.setSource(QUrl.fromLocalFile(self.current_path))
        self.mediaplayer.play()

        self.is_playing = True
        self.show()
        self.raise_()
        self.setFocus()
        self.timer.start()
        self.hide_cursor_timer.start()
        self.lbl_title.setText(os.path.basename(path))
        # [U10] 真实标题记账（同 base_player._toast 修法，见其注释）
        self._real_title = os.path.basename(path)
        self.clear_badges()
        self.clip_panel.hide()
        self.is_clipping_mode = False
        from . import icons as _icons; _icons.set_play_icon(self.btn_play, playing=True)
        self.resizeEvent(None)
        # 兜底检测：部分后端解码失败不发错误信号，超时仍未开播则在本界面报错
        QTimer.singleShot(1500, lambda: self._check_playback_started(self.current_path))

    def _check_playback_started(self, original_path):
        # PyQt6 槽内未捕获异常会 abort 整个进程，这里必须自行兜住
        try:
            if self._error_handled or self.current_path != original_path:
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

    def _on_player_error(self, error, error_string):
        self._show_play_error(error_string or "解码失败")

    def _on_media_status(self, status):
        # 元数据就绪后刷新徽标
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            QTimer.singleShot(300, self.update_metadata)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._show_play_error("媒体格式不受支持")

    def _on_video_frame(self, frame):
        img = frame.toImage()
        if not img.isNull():
            self.video_frame.setFrame(img)

    def _on_playback_state(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            from . import icons as _icons; _icons.set_play_icon(self.btn_play, playing=True)
            self.is_playing = True
        elif state == QMediaPlayer.PlaybackState.PausedState:
            from . import icons as _icons; _icons.set_play_icon(self.btn_play, playing=False)
            self._wake_ui()
            self.is_playing = False
        elif state == QMediaPlayer.PlaybackState.StoppedState:
            # [BUG-P1-13] 自然播完走 StoppedState：此前不处理导致 is_playing
            # 恒 True——播放键停留"暂停"图标、控件照常自动隐藏、空格误走暂停分支
            from . import icons as _icons; _icons.set_play_icon(self.btn_play, playing=False)
            self._wake_ui()
            self.is_playing = False

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
                info.append(f"{resolution.width()}x{resolution.height()}")
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
        self.mediaplayer.stop()
        self.timer.stop()
        self.video_frame.clear()
        # [BUG-P2-31] 此前未停 hide_cursor_timer：关闭后每 3s 触发 hide_ui
        # 空转（与基类 stop 行为不一致）
        self.hide_cursor_timer.stop()
        self.hide()
        self.is_playing = False
        if self.parent(): self.parent().setFocus()

    def toggle_play(self):
        if self.is_playing:
            self.mediaplayer.pause()
        else:
            self.mediaplayer.play()

    def _capture_frame_ffmpeg(self, input_path, output_path, at_time_sec):
        """通用 FFmpeg 截图函数"""
        ffmpeg_bin = utils.get_tool_path('ffmpeg')
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
        ffmpeg_bin = utils.get_tool_path('ffmpeg')
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
        target_small = engine.get_thumb_path(self.current_path, size_type='small')
        target_large = engine.get_thumb_path(self.current_path, size_type='large')

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
        folder, filename = os.path.split(self.current_path)
        name_stem, ext = os.path.splitext(filename)
        out_name = f"{name_stem}_clip_{int(time.time())}{ext}"
        out_path = os.path.join(folder, out_name)
        ffmpeg_bin = utils.get_tool_path('ffmpeg')
        if not ffmpeg_bin:
            self.show_toast("FFmpeg not found")
            return
        cmd = [ffmpeg_bin, '-y', '-ss', start, '-to', end, '-i', self.current_path, '-c', 'copy', '-avoid_negative_ts', '1', out_path]
        self.show_toast("正在截取...")
        def run():
            # [BUG-P2-32] 后台线程此前 except: pass 吞掉 ffmpeg 全部错误——
            # "正在截取..."之后永久沉默，成功也无反馈。结果经信号回 GUI 线程。
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                self.toast_requested.emit(f"截取完成: {out_name}")
                utils.open_in_os(out_path)
            except subprocess.CalledProcessError as e:
                err = (e.stderr or b'')[-300:].decode(errors='ignore')
                logger.error(f"截取失败: {err}")
                self.toast_requested.emit("截取失败（详见控制台）")
            except Exception as e:
                logger.error(f"截取异常: {e}")
                self.toast_requested.emit("截取失败")
        import threading
        threading.Thread(target=run, daemon=True).start()
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
        self.lbl_title.setText(text if text is not None
                               else getattr(self, '_real_title', self.lbl_title.text()))
        self.lbl_title.setStyleSheet("")

    def update_ui_state(self):
        # [BUG-P2-33] 去掉 not is_playing 短路：右键帧进先 pause()，此后
        # 时间标签/进度条冻结（与基类不一致）。is_seeking 拖动中仍跳过。
        if self.is_seeking: return
        duration = self.mediaplayer.duration()
        if duration > 0 and duration != self.duration:
            self.duration = duration
            self.lbl_total.setText(utils.format_time(duration // 1000))
        curr_ms = self.mediaplayer.position()
        if duration > 0:
            self.lbl_curr.setText(utils.format_time(curr_ms // 1000))
            self.slider.setValue(int(curr_ms / duration * 1000))

    def on_slider_pressed(self): self.is_seeking = True
    def on_slider_released(self):
        duration = self.mediaplayer.duration()
        if duration > 0:
            pos = self.slider.value() / 1000.0
            self.mediaplayer.setPosition(int(pos * duration))
        self.is_seeking = False
    def on_slider_moved(self, val):
        duration = self.mediaplayer.duration()
        if duration > 0:
            sec = (val / 1000.0) * (duration / 1000.0)
            self.lbl_curr.setText(utils.format_time(int(sec)))

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
        from PyQt6.QtCore import QEvent
        if ev.type() in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                         QEvent.Type.MouseButtonRelease, QEvent.Type.Wheel):
            self._wake_ui()
        return super().eventFilter(obj, ev)

    def _wake_ui(self):
        self.ui_visible = True
        self.header_container.show()
        self.controls_container.show()
        self.header_container.raise_()
        self.controls_container.raise_()
        self.clip_panel.raise_()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.hide_cursor_timer.start()

    def mouseMoveEvent(self, event):
        self._wake_ui()
        super().mouseMoveEvent(event)

    def hide_ui(self):
        # 仅播放中隐藏；暂停常显；截取面板打开时不藏
        if not self.is_playing or self.clip_panel.isVisible():
            return
        self.ui_visible = False
        self.header_container.hide()
        self.controls_container.hide()
        self.setCursor(Qt.CursorShape.BlankCursor)
