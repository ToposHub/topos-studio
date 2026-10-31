"""BasePlayer —— ToposPlayer / LibavPlayer 共用的播放器骨架 + DecoderBackend 接口。

[ARCH-2] topos_player 与 libav_player 的浮层 UI（画布/顶栏/播放条/信息面板）、
自动隐藏、进度条、快照/封面、解码线程生命周期在此统一；两家差异经钩子注入：

- 解码会话：_open_session() 返回 DecoderBackend 实现（TPIC 原生或 PyAV 多线程）；
- 帧消费策略：_poll_ring 由子类实现（topos 严格按序；libav 主时钟门控）；
- 解码线程差异：_decode_frame / _on_seek_jump / _on_eof / 解码异常钩子；
- 音频与主时钟：libav 经 _on_play_state_changed / _on_seek_requested 接入；
- 面板扩展：topos 的片段截取面板经 _overlay_panels_open / _relayout_extra 接入。

UltimatePlayer（player_qt，QMediaPlayer 后端）刻意不并入本骨架：其 UI 面貌
不同（音量条/meta 徽标/比例进度条），进度由 Qt Multimedia 直接驱动，强行
套壳只会得到参数泥球；三引擎的路由互斥在 player_host.VideoProEngine。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import deque

from PyQt6.QtCore import Qt, QTimer, QRectF, pyqtSignal
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QSlider, QVBoxLayout, QWidget)

from . import icons as _icons

logger = logging.getLogger(__name__)

STYLESHEET = """
    QFrame#ToposHeader {
        background-color: rgba(0, 0, 0, 0.8); border: none;
    }
    QFrame#ToposControls {
        background-color: rgba(0, 0, 0, 0.8); border: none; border-radius: 10px;
    }
    QFrame#ToposPanel {
        background-color: rgba(0, 0, 0, 0.8); border: none; border-radius: 8px;
    }
    QLabel { color: rgba(255, 255, 255, 0.9); font-family: -apple-system, sans-serif; background: transparent; }
    QLabel#ToposTitle { font-weight: 600; font-size: 13px; }
    QLabel.ToposMetaBadge {
        background: transparent; color: rgba(255, 255, 255, 0.55);
        padding: 0; font-size: 11px; font-weight: 600; font-family: monospace;
    }
    QSlider { background: transparent; }
    QSlider::groove:horizontal { height: 3px; background: rgba(255, 255, 255, 0.2); border-radius: 1px; }
    QSlider::handle:horizontal { background: #fff; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
    QSlider::sub-page:horizontal { background: #0A84FF; border-radius: 1px; }
    QPushButton { background: transparent; border: none; padding: 5px; border-radius: 6px; color: rgba(255, 255, 255, 0.8); font-size: 12px; }
    QPushButton:hover { background: rgba(255, 255, 255, 0.15); color: #fff; }
    QLineEdit { background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.15);
                color: #fff; border-radius: 6px; padding: 2px 5px;
                font-family: monospace; font-size: 12px; }
"""


def _fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f'{sec // 60:02d}:{sec % 60:02d}'


def _frame_to_qimage(rgb) -> QImage:
    h, w, _ = rgb.shape
    img = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return img.copy()  # 脱离 numpy 缓冲生命周期


def utils_get_tool_path(tool: str):
    """file_explorer 工具链查找（bin/ 内置的自编译 FFmpeg 优先）。"""
    try:
        import utils
        return utils.get_tool_path(tool)
    except Exception:
        return None


class _VideoCanvas(QWidget):
    """当前帧绘制面（等比缩放 + 黑底）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: QImage | None = None
        self.setStyleSheet('background: #000;')

    def set_image(self, image: QImage) -> None:
        self._image = image
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._image is None:
            p.setPen(Qt.GlobalColor.gray)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       'Topos Video Codec 2.0')
            p.end()
            return
        # 等比适配 + 单次 drawImage（避免 scaled()/fromImage 中间拷贝）
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        iw, ih = self._image.width(), self._image.height()
        scale = min(self.width() / iw, self.height() / ih)
        tw, th = int(iw * scale), int(ih * scale)
        p.drawImage(QRectF((self.width() - tw) / 2, (self.height() - th) / 2,
                           tw, th), self._image)
        p.end()

    def current_image(self) -> QImage | None:
        return self._image

    def mousePressEvent(self, event):
        # 点击画面 = 播放/暂停（VLC 习惯）
        if callable(self._on_click):
            self._on_click()
        super().mousePressEvent(event)

    def display_size(self):
        """当前绘制目标尺寸（设备像素），供解码线程按显示分辨率渲染。

        [U10] 只读缓存：width()/height()/devicePixelRatioF() 属 QWidget
        主线程 API，此前解码线程直接调进来（窗口 resize 期间可读到撕裂值，
        且触及屏幕对象查询）。缓存由 refresh_size_cache 在 GUI 线程维护。"""
        return getattr(self, '_size_cache', None)

    def refresh_size_cache(self):
        """[U10] GUI 线程更新尺寸缓存（resize/show/play 时调用）。"""
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            self._size_cache = None
        else:
            dpr = self.devicePixelRatioF()
            self._size_cache = (int(w * dpr), int(h * dpr))


# 旧名（topos_player 里诞生，libav_player 曾从那里导入）
_ToposCanvas = _VideoCanvas


class DecoderBackend(ABC):
    """解码会话接口：BasePlayer 只通过该接口消费帧（[ARCH-2]）。

    实现：topos_player.ToposMovieSession（libtopos 原生解码）、
    libav_player._LibavSession（PyAV 多线程解码）。
    单实例不可重入：frame_rgb 只允许在持有线程调用；close 前必须确认
    解码线程已退出（use-after-free 防线，见 BasePlayer.stop_decoder）。
    """

    width: int
    height: int
    fps: float
    frame_count: int
    duration_sec: float

    @abstractmethod
    def frame_rgb(self, index: int, size=None):
        """解码第 index 帧 → (H, W, 3) uint8 RGB。

        size=(w, h) 时转换+缩放到显示分辨率一步完成（4K 实时预览关键
        路径）；None = 原生全尺寸（快照/封面用）。
        """

    @abstractmethod
    def close(self) -> None:
        """释放解码句柄；必须幂等（延迟回收路径会再次调用）。"""


class BasePlayer(QFrame):
    """ToposPlayer / LibavPlayer 共用骨架：浮层 UI + 解码线程生命周期。"""

    request_prev = pyqtSignal()
    request_next = pyqtSignal()
    play_failed = pyqtSignal(str)  # 播放失败（原因），由宿主转前端 toast
    # [BUG-P0-07] 后台线程（如 topos 片段导出）请求 toast：经信号投递到
    # GUI 线程——QWidget.setText/样式与 QTimer.singleShot 均不可跨线程
    toast_requested = pyqtSignal(str)

    RING_SIZE = 4
    JOIN_TIMEOUT = 3.0     # 解码线程退出等待（libav 2.0）
    PLAYER_TITLE = 'Player'
    BADGE_TEXT = ''
    HAS_AUDIO = True       # [BUG-P1-17] 无音频引擎（topos）置 False 隐藏音量条

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(STYLESHEET)
        self.setVisible(False)

        self._session: DecoderBackend | None = None
        self._ring: deque = deque(maxlen=self.RING_SIZE)
        self._ring_lock = threading.Lock()
        self._ring_space = threading.Condition(self._ring_lock)
        self._pending: deque = deque()
        self._cond = threading.Condition()
        self._running = False
        self._playing = False
        self._seek_target: int | None = None
        self._next_index = 0
        self._shown_index = -1
        self._current_path = ''

        self._build_ui()
        self.toast_requested.connect(self._toast)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(40)
        self._ui_timer.timeout.connect(self._poll_ring)
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # UI 骨架（浮层：顶栏/播放条悬浮在画布上，自动隐藏时画面不跳变）
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.canvas = _VideoCanvas(self)
        self.canvas._on_click = self.toggle_play
        self.canvas.setCursor(Qt.CursorShape.PointingHandCursor)

        header = QFrame(self)
        header.setObjectName('ToposHeader')
        header.setFixedHeight(44)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 16, 0)
        self.lbl_title = QLabel(self.PLAYER_TITLE)
        self.lbl_title.setObjectName('ToposTitle')
        self.lbl_badge = QLabel(self.BADGE_TEXT)
        self.lbl_badge.setProperty('class', 'ToposMetaBadge')
        self.lbl_badge.setStyleSheet(
            'color:rgba(255,255,255,0.55); font-size:11px; font-weight:600;'
            ' font-family:monospace; background:transparent; border:none;')
        btn_info = _icons.make_tool_button(self, 'fa5s.info-circle', '视频信息', 'ℹ️')
        btn_info.clicked.connect(self._toggle_info)
        btn_close = _icons.make_tool_button(self, 'fa5s.times', '关闭 (Esc)', '✕', color=_icons.WHITE)
        btn_close.clicked.connect(self.stop)
        hl.addWidget(self.lbl_badge)
        hl.addWidget(self.lbl_title)
        hl.addStretch(1)
        hl.addWidget(btn_info)
        self._populate_header_buttons(hl)
        hl.addWidget(btn_close)

        controls = QFrame(self)
        controls.setObjectName('ToposControls')
        cl = QHBoxLayout(controls)
        cl.setContentsMargins(16, 6, 16, 6)
        btn_prev = _icons.make_tool_button(self, 'fa5s.step-backward', '上一个 (↑)', '⏮', size=14)
        btn_prev.clicked.connect(self.request_prev.emit)
        self.btn_play = QPushButton('')
        self.btn_play.setToolTip('播放/暂停 (空格)')
        self.btn_play.setFixedSize(30, 30)
        _icons.set_play_icon(self.btn_play, playing=True)
        self.btn_play.clicked.connect(self.toggle_play)
        btn_next = _icons.make_tool_button(self, 'fa5s.step-forward', '下一个 (↓)', '⏭', size=14)
        btn_next.clicked.connect(self.request_next.emit)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderReleased.connect(self._on_slider_seek)
        self.slider.sliderPressed.connect(self._on_slider_scrub)
        self.lbl_pos = QLabel('00:00')
        self.lbl_total = QLabel('00:00')
        cl.addWidget(btn_prev)
        cl.addWidget(self.btn_play)
        cl.addWidget(btn_next)
        cl.addWidget(self.lbl_pos)
        cl.addWidget(self.slider, 1)
        cl.addWidget(self.lbl_total)
        # —— [BUG-P1-17] 音量条（libav 的 QAudioSink 此前全文件无一处
        # setVolume，播放音量固定不可调不可静音）——
        self.slider_vol = QSlider(Qt.Orientation.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(100)
        self.slider_vol.setFixedWidth(64)
        self.slider_vol.setToolTip('音量')
        self.slider_vol.valueChanged.connect(self._on_volume_changed)
        self.lbl_vol = QLabel()
        _vol_icon = _icons.icon('fa5s.volume-up')
        if _vol_icon is not None:
            self.lbl_vol.setPixmap(_vol_icon.pixmap(13, 13))
        else:
            self.lbl_vol.setText('🔊')
        for w in (self.lbl_vol, self.slider_vol):
            cl.addWidget(w)
        if not self.HAS_AUDIO:
            self.lbl_vol.hide()
            self.slider_vol.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.canvas, 1)

        self._header = header
        self._header.setFixedHeight(44)
        self._header.raise_()

        self._controls = controls
        controls.setParent(self)
        controls.raise_()
        controls.adjustSize()

        # 播放中 3 秒无操作自动隐藏顶栏/播放条/光标，动鼠标即恢复
        self.setMouseTracking(True)
        self.canvas.setMouseTracking(True)
        for w in (self.canvas, self._header, self._controls):
            w.installEventFilter(self)
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(3000)
        self._idle_timer.timeout.connect(self._auto_hide_ui)

        # —— 信息浮层（ℹ️ 切换） ——
        self.info_panel = QFrame(self)
        self.info_panel.setObjectName('ToposPanel')
        self.info_panel.setFixedWidth(380)
        il = QVBoxLayout(self.info_panel)
        il.setContentsMargins(14, 10, 14, 10)
        self.lbl_info = QLabel('')
        self.lbl_info.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setStyleSheet('font-size:12px; font-family:monospace;')
        il.addWidget(self.lbl_info)
        self.info_panel.hide()

        self._build_extra_ui()

    def _build_extra_ui(self) -> None:
        """子类扩展浮层（topos：片段截取面板）在 UI 构建尾部挂载。"""

    def _populate_header_buttons(self, hl) -> None:
        """info 之后、close 之前的按钮（默认：截图 + 封面）。"""
        btn_snap = _icons.make_tool_button(self, 'fa5s.camera', '截图（原生全尺寸）', '📷')
        btn_snap.clicked.connect(self.snapshot)
        btn_cover = _icons.make_tool_button(self, 'fa5s.image', '设为封面', '🖼️')
        btn_cover.clicked.connect(self._set_as_cover)
        hl.addWidget(btn_snap)
        hl.addWidget(btn_cover)

    # ------------------------------------------------------------------
    # 播放控制
    # ------------------------------------------------------------------

    def play_video(self, path: str) -> None:
        # [BUG-P1-14] 先停旧会话再开新文件：旧实现打开新文件失败时只 hide()，
        # 上一会话的解码线程继续后台解码（耗 CPU，libav 音频也不停）
        self.stop_decoder()
        try:
            session = self._open_session(path)
        except Exception as e:
            # 不回退系统播放器——静默转交只会制造"打开了但报错"的困惑。
            # 原因经 play_failed 上抛为可见 toast。
            import traceback
            traceback.print_exc()
            # [U10] 与 stop() 的清理对齐：旧会话的 _ui_timer 继续空转、
            # _idle_timer 未停且 _playing 残留 True（隐藏窗口仍自动隐藏控件）
            self._ui_timer.stop()
            self._idle_timer.stop()
            self._playing = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.play_failed.emit(f'{self._open_failed_prefix()}: {e}')
            self.hide()
            return
        self._session = session
        self._current_path = path
        self._ring.clear()
        self._pending.clear()  # 上一文件的残留帧会堵死新文件的显示队列
        self._shown_index = -1
        self._next_index = 0
        self._seek_target = None
        self.slider.setRange(0, session.frame_count - 1)
        self.lbl_total.setText(_fmt_time(session.duration_sec))
        self.lbl_title.setText(os.path.basename(path))
        # [U10] 真实标题记账：_toast 的 orig 捕获法在两次叠加 toast 时会把
        # 上一条 toast 文案当"原标题"还原（标题永久残留"正在截取..."等）
        self._real_title = os.path.basename(path)
        self._configure_session(session)
        self.info_panel.hide()
        self._ui_timer.setInterval(self._ui_interval(session))
        self._playing = True
        _icons.set_play_icon(self.btn_play, playing=True)
        self._running = True
        # [U10] 解码线程启动前预热尺寸缓存（display_size 只读缓存）
        try:
            self.canvas.refresh_size_cache()
        except Exception:
            pass
        self._thread = threading.Thread(
            target=self._decode_loop, args=(session,), daemon=True)
        self._thread.start()
        self._after_decoder_started()
        self._ui_timer.start()
        self.show()
        self.raise_()
        self.setFocus()
        self._wake_ui()

    def toggle_play(self) -> None:
        self._playing = not self._playing
        self._on_play_state_changed()
        _icons.set_play_icon(self.btn_play, playing=self._playing)
        self._wake_ui()
        with self._cond:
            self._cond.notify()

    def stop(self) -> None:
        self.stop_decoder()
        self.hide()
        self._ui_timer.stop()
        self._idle_timer.stop()
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def stop_decoder(self) -> None:
        with self._cond:
            self._running = False
            self._cond.notify()
        self._teardown_output()
        with self._ring_lock:
            self._ring.clear()
            self._pending.clear()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self.JOIN_TIMEOUT)
            if self._thread.is_alive():
                # 线程仍卡在解码调用里——绝不能在此刻 close 解码器
                # （use-after-free 原生崩溃）。泄漏会话，交给延迟回收。
                logger.warning("[%s] 解码线程未在 %.0fs 内退出，会话延迟回收",
                               type(self).__name__, self.JOIN_TIMEOUT)
                orphan = self._session
                self._session = None

                def _late_close(sess, th):
                    th.join(timeout=10.0)
                    if th.is_alive():
                        # [BUG-P0-05] 线程 10s 后仍卡在原生调用里：此刻 close
                        # 解码器 = 另一线程释放原生句柄（use-after-free 原生崩溃）。
                        # 选有界泄漏（会话小、可控）而绝不强 close。
                        logger.error(
                            "[%s] 解码线程 %0.f+10s 仍未退出，放弃 close（有界泄漏）",
                            type(self).__name__, self.JOIN_TIMEOUT)
                        return
                    if sess is not None:
                        sess.close()

                threading.Thread(target=_late_close,
                                 args=(orphan, self._thread),
                                 daemon=True).start()
                self._thread = None
                self._reset_stream_state()
                return
        self._thread = None
        if self._session is not None:
            self._session.close()
            self._session = None
        self._reset_stream_state()

    def snapshot(self) -> None:
        rgb = self._native_frame()
        if rgb is None:
            self._toast('无可用帧')
            return
        folder = os.path.dirname(self._current_path)
        path = os.path.join(folder, f'snapshot_{int(time.time())}.jpg')
        try:
            h, w, _ = rgb.shape
            from PyQt6.QtGui import QImage as _QImage
            img = _QImage(rgb.data, w, h, 3 * w, _QImage.Format.Format_RGB888).copy()
            ok = img.save(path, 'JPG', 95)
        except Exception as e:
            self._toast(f'截图失败: {e}')
            return
        if ok:
            self._toast(f'已保存 {os.path.basename(path)}')
        else:
            self._toast('截图失败')

    # ------------------------------------------------------------------
    # 子类钩子：会话/输出/节奏差异都在这里注入
    # ------------------------------------------------------------------

    def _open_session(self, path: str) -> DecoderBackend:
        """打开解码会话；失败抛异常（play_failed 上抛）。"""
        raise NotImplementedError

    def _open_failed_prefix(self) -> str:
        return '播放失败'

    def _configure_session(self, session: DecoderBackend) -> None:
        """会话就绪后的徽标/音频/扩展面板初始化（slider/标题已设好）。"""

    def _ui_interval(self, session: DecoderBackend) -> int:
        """UI 消费定时器间隔（ms）。"""
        return max(4, int(1000 / session.fps))

    def _after_decoder_started(self) -> None:
        """解码线程启动后的收尾（libav：启动音频管线）。"""

    def _on_play_state_changed(self) -> None:
        """播放/暂停切换后的子类逻辑（主时钟/音频）。"""

    def _on_volume_changed(self, value: int) -> None:
        """[BUG-P1-17] 音量条变化（0-100）；有音频的引擎覆写接线。"""

    def _teardown_output(self) -> None:
        """停止解码前的输出端清理（libav：停音频管线）。"""

    def _reset_stream_state(self) -> None:
        """stop_decoder 收尾的状态复位；子类可追加（libav：_t_mark）。"""
        self._shown_index = -1
        self._next_index = 0
        self._seek_target = None

    def _on_seek_jump(self) -> None:
        """解码线程消费到 seek 请求时（丢弃过期帧等）。"""

    def _on_eof(self) -> None:
        """自然播到片尾（libav：暂停音频 + 播放键归位）。"""

    def _decode_frame(self, session: DecoderBackend, target: int) -> QImage:
        """解码 target 帧 → QImage（在工作线程内调用）。"""
        return _frame_to_qimage(session.frame_rgb(target))

    def _on_decode_stop(self) -> None:
        """_decode_frame 抛 StopIteration（迭代式后端的正常收尾）。"""

    def _on_decode_error(self) -> None:
        """_decode_frame 抛其他异常（可退避；默认静默跳帧）。"""

    def _on_seek_requested(self, target: int) -> None:
        """_request_seek 前置钩子（libav：主时钟重锚 + audio.seek）。"""

    def _native_frame(self):
        """当前帧原生全尺寸重解（显示帧是预览分辨率）；无帧返回 None。"""
        if self._session is None or self._shown_index < 0:
            return None
        return self._session.frame_rgb(self._shown_index)

    def _overlay_panels_open(self) -> bool:
        """打开时禁止自动隐藏的扩展面板（topos：片段截取面板）。"""
        return False

    def _relayout_extra(self) -> None:
        """扩展浮层的布局（topos：clip_panel）。"""

    def _handle_key(self, key) -> bool:
        """子类专属按键；返回 True 表示已消费（topos：I/O/C/S，导航键）。"""
        return False

    def _info_html(self) -> str:
        return ''

    def _on_info_unavailable(self) -> None:
        """无会话时按 ℹ️ 的反馈（topos：toast 提示）。"""

    # ------------------------------------------------------------------
    # 信息面板
    # ------------------------------------------------------------------

    def _toggle_info(self) -> None:
        if self._session is None:
            self._on_info_unavailable()
            return
        if self.info_panel.isVisible():
            self.info_panel.hide()
            return
        self.lbl_info.setText(self._info_html())
        self.info_panel.move(16, 56)
        self.info_panel.adjustSize()
        self.info_panel.show()
        self.info_panel.raise_()

    # ------------------------------------------------------------------
    # 快照 / 封面（原生全尺寸重解，不依赖 ffmpeg CLI）
    # ------------------------------------------------------------------

    def _set_as_cover(self) -> None:
        rgb = self._native_frame()
        if rgb is None:
            self._toast('无可用帧')
            return
        try:
            from PIL import Image
            from thumb_engine import engine
            img = Image.fromarray(rgb)
            t_small = engine.get_thumb_path(self._current_path, size_type='small')
            t_large = engine.get_thumb_path(self._current_path, size_type='large')
            os.makedirs(os.path.dirname(t_small), exist_ok=True)
            lg = img.copy(); lg.thumbnail((600, 600))
            lg.save(t_large, 'WEBP', quality=90)
            img.thumbnail((300, 300))
            img.save(t_small, 'WEBP', quality=80)
            self._toast('封面已更新')
            self._notify_frontend_thumbnail()
        except Exception as e:
            self._toast(f'封面失败: {e}')

    def _notify_frontend_thumbnail(self) -> None:
        """封面更新后通知宿主前端刷新缩略图。"""
        import unicodedata
        parent = self.parent()
        if hasattr(parent, 'trigger_js'):
            jp = unicodedata.normalize('NFC', self._current_path) \
                .replace('\\', '/').replace("'", "\\'")
            parent.trigger_js(f"refreshThumbnail('{jp}');")

    # ------------------------------------------------------------------
    # 解码线程（模板）：等待/seek/EOF 骨架固定，帧解码与异常经钩子注入
    # ------------------------------------------------------------------

    def _decode_loop(self, session: DecoderBackend) -> None:
        # 节奏由 UI 定时器消费速率决定（背压），不依赖 sleep 精度：
        # 解码全速跑，ring 满即阻塞等待 UI 消费。
        while True:
            with self._cond:
                while self._running and not self._playing and \
                        self._seek_target is None:
                    self._cond.wait(timeout=0.2)
                if not self._running:
                    return
                target = self._seek_target
                seek_jump = target is not None
                if target is None:
                    target = self._next_index
                self._seek_target = None
            if seek_jump:
                self._on_seek_jump()
            if target >= session.frame_count:
                with self._cond:
                    self._playing = False
                self._on_eof()
                continue
            try:
                img = self._decode_frame(session, target)
            except StopIteration:
                self._on_decode_stop()
                continue
            except Exception:
                self._on_decode_error()
                continue
            # 背压：ring 满（UI 未消费）则挂起；有 seek 请求则丢弃本帧
            with self._ring_lock:
                while self._running and len(self._ring) >= self.RING_SIZE \
                        and self._seek_target is None:
                    self._ring_space.wait(timeout=0.05)
                if not self._running or self._seek_target is not None:
                    continue
                self._ring.append((target, img))
                self._ring_space.notify()
            with self._cond:
                if self._seek_target is None and \
                        target + 1 < session.frame_count:
                    self._next_index = target + 1

    def _poll_ring(self) -> None:
        """UI 消费钩子：由子类实现（topos 严格按序 / libav 时钟门控）。"""
        raise NotImplementedError

    def _request_seek(self, target: int) -> None:
        self._on_seek_requested(target)
        with self._cond:
            self._seek_target = target
            self._cond.notify()
        # 显示基准重置到目标前一帧（下一帧恰好是 target）。
        # pending 与 ring 都在本（UI）线程清空——与 _poll_ring 无竞争；
        # 解码线程醒来后按 seek 目标重新产出。
        self._shown_index = target - 1
        with self._ring_lock:
            self._ring.clear()
            self._pending.clear()

    def _on_slider_scrub(self) -> None:
        pass  # 拖动中不处理，松手后一次性 seek

    def _on_slider_seek(self) -> None:
        if self._session is None:
            return
        self._request_seek(int(self.slider.value()))

    # ------------------------------------------------------------------
    # UI 刷新 / 自动隐藏 / 布局
    # ------------------------------------------------------------------

    def eventFilter(self, obj, ev):
        from PyQt6.QtCore import QEvent
        if ev.type() in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                         QEvent.Type.MouseButtonRelease, QEvent.Type.Wheel):
            self._wake_ui()
        return super().eventFilter(obj, ev)

    def _wake_ui(self) -> None:
        """显示顶栏/播放条并重置空闲计时；暂停状态下保持常显。"""
        self._header.show()
        self._controls.show()
        self._header.raise_()
        self._controls.raise_()
        if self.info_panel.isVisible():
            self.info_panel.raise_()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._relayout()
        if self._playing:
            self._idle_timer.start()

    def _auto_hide_ui(self) -> None:
        # 仅播放中隐藏；暂停常显；信息/扩展面板打开时不藏（避免误伤操作）
        if (not self._playing or self.info_panel.isVisible()
                or self._overlay_panels_open()):
            return
        self._header.hide()
        self._controls.hide()
        self.setCursor(Qt.CursorShape.BlankCursor)

    def _relayout(self) -> None:
        self._header.setGeometry(0, 0, self.width(), 44)
        # 播放条拉长到播放器宽度的七成（560~1200px），拖拽进度更好用
        target = max(560, min(int(self.width() * 0.7), 1200))
        self._controls.setFixedWidth(target)
        self._controls.move((self.width() - target) // 2,
                            self.height() - self._controls.height() - 24)
        if self.info_panel.isVisible():
            self.info_panel.move(16, 56)
        self._relayout_extra()
        # [U10] 尺寸缓存随布局刷新（GUI 线程侧），解码线程只读缓存
        try:
            self.canvas.refresh_size_cache()
        except Exception:
            pass

    def resizeEvent(self, event):
        self._relayout()
        super().resizeEvent(event)

    def keyPressEvent(self, event):
        self._wake_ui()
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_play()
        elif key == Qt.Key.Key_Escape:
            self.stop()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Left):
            step = 1 if key == Qt.Key.Key_Right else -1
            last = (self._session.frame_count - 1) if self._session else 0
            self._request_seek(max(0, min(self._shown_index + step, last)))
        elif not self._handle_key(key):
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # 轻提示（2.5s 自动还原，不永久顶掉标题）
    # ------------------------------------------------------------------

    def _toast(self, text: str) -> None:
        # [BUG-P3-58] 代次令牌：连续 toast 时各 singleShot 捕获的旧标题会把
        # 更早的标题"复活"覆盖新 toast——只还原最后一次之前的标题
        # [U10] 还原目标改为 _real_title（play_video 时记账）：orig 捕获法
        # 在两次叠加 toast 时把上一条 toast 文案当"原标题"还原
        self._toast_gen = getattr(self, '_toast_gen', 0) + 1
        gen = self._toast_gen
        self.lbl_title.setText(text)
        self.lbl_title.setStyleSheet('color: #0A84FF; font-weight: bold;'
                                     'background: transparent; border: none;')
        QTimer.singleShot(2500, lambda: self._reset_title(None, gen))

    def _reset_title(self, text: str = None, gen: int = -1) -> None:
        if gen != -1 and gen != getattr(self, '_toast_gen', gen):
            return  # 已有更新的 toast 接管标题
        self.lbl_title.setText(text if text is not None
                               else getattr(self, '_real_title', self.lbl_title.text()))
        self.lbl_title.setStyleSheet('')
