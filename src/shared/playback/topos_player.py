"""
Topos Video Codec 原生播放器（应用级共享播放核心）。

解码：src/shared/codec/topos_binding（libtopos_codec，O(1) 索引 + 多线程切片解码）
显示：swscale（yuv422/444p10le → rgb24）→ QImage 直绘

- 全 intra：任意帧独立解码，进度条拖动零寻址延迟；
- 解码线程后台预取（环形缓冲 6 帧 + 背压）；有音轨时以 QAudioSink
  播放位置为主时钟，无音轨时按源 fps 严格按序显示；
- 播放用 auto2k 档（显示尺寸优先），快照/封面走全尺寸；
- 快照原生全尺寸直出（不依赖 ffmpeg CLI）；
- 片段导出走自编译 FFmpeg（libtopos 解码 → ProRes 422 HQ 交付格式）；
- 信息面板（编解码/分辨率/帧率/位深/色彩/码率）、设为封面、点击暂停。

[ARCH-2] UI 骨架/解码线程生命周期/快照/封面统一在 base_player.BasePlayer，
本文件保留：ToposMovieSession 解码会话 + 严格按序的帧消费 + 片段截取面板。
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import logging
logger = logging.getLogger(__name__)
import sys
import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path

# ``src/shared/playback`` is inside the repository package tree; when this file
# is imported directly (tools/tests), make the repository root importable.
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton)

from . import icons as _icons
from .base_player import (BasePlayer, DecoderBackend, _fmt_time,
                          _frame_to_qimage,
                          utils_get_tool_path)  # noqa: F401  (libav/外部沿用旧导入名)
from .topos_media import topos_color_info  # noqa: F401  (topos_media 元数据面板沿用)

if TYPE_CHECKING:
    from .yuv_frame import YuvFrame

# —— H.273 码值 → 显示名（元数据面板用；与 AVCodecContext 枚举同源） ——
_MATRIX_NAMES = {1: 'BT.709', 5: 'BT.601', 6: 'SMPTE 170M', 9: 'BT.2020 NC'}
_TRANSFER_NAMES = {1: 'BT.709', 8: 'Linear', 16: 'PQ (HDR)', 18: 'HLG (HDR)'}
_PRIMARIES_NAMES = {1: 'BT.709', 5: 'BT.470 BG', 6: 'SMPTE 170M', 9: 'BT.2020'}

_PIX_FMT_BY_GEOMETRY = {
    (0, 10): 'yuv422p10le', (1, 10): 'yuv444p10le',
    (0, 12): 'yuv422p12le', (1, 12): 'yuv444p12le',
}


def _load_binding():
    from src.shared.codec.topos_binding import (
        ToposCodec, ToposDecoder, ToposMovieFile)
    return ToposCodec, ToposMovieFile, ToposDecoder


class ToposMovieSession(DecoderBackend):
    """一次打开的 Topos MOV：绑定层句柄 + 解码视图 + 帧→RGB 转换。

    单实例不可重入（对齐绑定层契约）；解码与转换都发生在此对象的
    持有线程内。像素格式当前支持 10/12-bit 4:2:2 / 4:4:4。
    """

    def __init__(self, path: str, slice_workers: int = 8) -> None:
        ToposCodec, ToposMovieFile, ToposDecoder = _load_binding()
        import av  # 延迟到真正播放时再加载（~200ms）
        self._av = av

        self.path = path
        self._codec = ToposCodec()
        self._movie = ToposMovieFile(self._codec, path)
        info = self._movie.info()
        self.info = info
        self.width = info.width
        self.height = info.height
        self.frame_count = info.sample_count
        # [FR-19] timescale 不是 fps：MOV media timescale 是"每秒 tick 数"，
        # 与帧率无关（timescale=24000、每帧 1000 ticks 的 24fps 片会算出
        # 24000fps/0.01s）。复用 ToposSource 的精确时基公式：
        # duration_ticks = 末帧 pts + 末帧时长；fps = timescale × 帧数 ÷
        # duration_ticks（含 NTSC 等 Non-整数帧率的精确换算）。
        duration_ticks = 0
        try:
            last_pts, last_dur = self._movie.pts(max(0, int(info.sample_count) - 1))
            duration_ticks = int(last_pts) + int(last_dur)
        except Exception:
            duration_ticks = 0
        if duration_ticks > 0 and info.sample_count > 0 and info.timescale:
            self.fps = float(Fraction(int(info.timescale) * int(info.sample_count),
                                      duration_ticks))
        else:
            self.fps = 24.0
        try:
            self.vfmt = _PIX_FMT_BY_GEOMETRY[(info.pixel_format, info.bit_depth)]
        except KeyError:
            self.close()
            raise ValueError(
                f"暂不支持的几何: pixel_format={info.pixel_format} "
                f"bit_depth={info.bit_depth} alpha={info.alpha_mode}")

        import numpy as np
        self._np = np
        # [CRASH-4] 会话非重入：解码线程与 GUI（截图/封面）并发调用
        # frame_rgb 会写同一共享缓冲——帧撕裂最坏原生崩溃。全访问串行化。
        self._lock = threading.Lock()
        self._audio_lock = threading.Lock()
        self._y = np.empty((self.height, self.width), dtype='<u2')
        chroma_w = self.width if int(info.pixel_format) == 1 else (self.width + 1) // 2
        self._u = np.empty((self.height, chroma_w), dtype='<u2')
        self._v = np.empty((self.height, chroma_w), dtype='<u2')
        self._decoder = ToposDecoder(self._codec, max_slice_workers=slice_workers)
        self._views = self._decoder.make_views([self._y, self._u, self._v])
        self._out_w, self._out_h = self.width, self.height
        self._auto_request = None
        self._auto_shape = None
        try:
            from src.shared.codec.topos_binding import TC_DECODE_MODE_AUTO_2K
            if getattr(self._codec, '_has_decoder_request', False):
                self._auto_request = self._decoder.make_request(
                    TC_DECODE_MODE_AUTO_2K)
        except Exception:
            self._auto_request = None

        # 音频使用独立的 movie 句柄，避免视频帧解码的长锁阻塞音频读包。
        self.audio_info = None
        self.audio_track_count = 0
        self._audio_codec = None
        self._audio_movie = None
        try:
            count = int(self._movie.audio_track_count())
            if count > 0:
                audio_codec = ToposCodec()
                audio_movie = ToposMovieFile(audio_codec, path)
                track_info = audio_movie.audio_info_at(0)
                if getattr(track_info, 'has_audio', False):
                    self.audio_info = track_info
                    self.audio_track_count = count
                    self._audio_codec = audio_codec
                    self._audio_movie = audio_movie
                else:
                    audio_movie.close()
        except Exception:
            logger.warning('Topos 音轨不可用（视频继续播放）', exc_info=True)

    def _alloc_planes(self, width: int, height: int, info=None):
        """按帧/平面几何分配 uint16 平面，返回 (y, u, v, views, shape)。"""
        if info is not None:
            y = self._np.empty((int(info.visible_height),
                                int(info.visible_width)), dtype='<u2')
            planes = [y]
            for plane in (1, 2):
                pw, ph = self._codec.plane_geometry(info, plane)
                planes.append(self._np.empty((ph, pw), dtype='<u2'))
        else:
            y = self._np.empty((height, width), dtype='<u2')
            chroma_w = width if int(self.info.pixel_format) == 1 else (width + 1) // 2
            planes = [y,
                      self._np.empty((height, chroma_w), dtype='<u2'),
                      self._np.empty((height, chroma_w), dtype='<u2')]
        self._y, self._u, self._v = planes
        self._views = self._decoder.make_views(planes)
        self._out_w, self._out_h = width, height
        return planes

    def _ensure_full_planes(self) -> None:
        if ((self._out_w, self._out_h) != (self.width, self.height)
                or self._auto_shape is not None):
            self._alloc_planes(self.width, self.height)
            self._auto_shape = None

    def frame_rgb(self, index: int, size=None):
        """解码第 index 帧 → (H, W, 3) uint8 RGB。

        size=(w, h) 时 swscale 一步完成转色+缩放到显示分辨率
        （4K 预览的实时性关键路径）；None = 原生全尺寸（快照用）。
        """
        with self._lock:
            return self._frame_rgb_locked(index, size)

    def frame_yuv(self, index: int, size=None) -> YuvFrame:
        """解码第 index 帧 → :class:`YuvFrame`（GPU 预览路径）。

        与 ``frame_rgb`` 共用同一条 decode/auto2k 路径，但不经过
        swscale，也不把 10/12-bit 压成 8-bit；三平面复制成本帧独立数组，
        避免解码器缓冲被下一帧覆盖。``size`` 非空时启用 AUTO_2K。
        """
        from .yuv_frame import YuvFrame

        with self._lock:
            self._decode_planes_locked(index, size)
            pixel_format = '444' if int(self.info.pixel_format) == 1 else '422'
            return YuvFrame(
                y=self._y.copy(),
                u=self._u.copy(),
                v=self._v.copy(),
                bit_depth=int(self.info.bit_depth),
                subsampling=pixel_format,
                matrix=int(self.info.color_matrix),
                color_range=int(self.info.color_range),
                chroma_siting=int(self.info.chroma_siting),
                pixel_format=pixel_format,
                source_width=self.width,
                source_height=self.height,
            )

    def _decode_planes_locked(self, index: int, size=None) -> None:
        """解码到当前 Y/U/V 平面（调用方必须持有 ``_lock``）。"""
        buf, size_n = self._movie.packet_into(index)
        req = self._auto_request if size is not None else None
        if req is not None:
            try:
                prep = self._decoder.prepare_request(buf, req, size_n)
                shape = (int(prep.visible_width), int(prep.visible_height))
                if shape != self._auto_shape:
                    self._alloc_planes(shape[0], shape[1], prep)
                    self._auto_shape = shape
                self._decoder.decode_views_request(buf, self._views, req, size_n)
            except Exception:
                # 旧 dylib 没有 request 接口时退回全尺寸解码，不影响播放。
                logger.warning('Topos auto2k 解码不可用，回退全尺寸', exc_info=True)
                self._auto_request = None
                req = None
        if req is None:
            self._ensure_full_planes()
            self._decoder.decode_views(buf, self._views, 3, size=size_n)

    def _frame_rgb_locked(self, index: int, size=None):
        self._decode_planes_locked(index, size)
        frame = self._av.VideoFrame(self._out_w, self._out_h, format=self.vfmt)
        frame.planes[0].update(self._y.data)
        frame.planes[1].update(self._u.data)
        frame.planes[2].update(self._v.data)
        # H.273 码值与 AVColorSpace 同源直传；topos range: 0=limited(tv), 1=full
        frame.colorspace = int(self.info.color_matrix)
        frame.color_range = 2 if self.info.color_range == 1 else 1
        if size:
            out = frame.reformat(width=int(size[0]), height=int(size[1]),
                                 format='rgb24')
        else:
            out = frame.reformat(format='rgb24')
        return out.to_ndarray()

    # -- 音频读取（独立句柄；供顶层播放器的音频线程调用） ---------------

    def audio_chunk_size(self, index: int) -> int:
        with self._audio_lock:
            if self._audio_movie is None:
                raise RuntimeError('无音轨')
            return int(self._audio_movie.audio_chunk_size(int(index)))

    def read_audio_chunk(self, index: int) -> bytes:
        with self._audio_lock:
            if self._audio_movie is None:
                raise RuntimeError('无音轨')
            return self._audio_movie.read_audio_samples_at(0, int(index), 1)

    def close(self) -> None:
        with self._lock:
            dec = getattr(self, '_decoder', None)
            if dec is not None:
                dec.close()
                self._decoder = None
            with self._audio_lock:
                audio_movie = getattr(self, '_audio_movie', None)
                if audio_movie is not None:
                    audio_movie.close()
                    self._audio_movie = None
            movie = getattr(self, '_movie', None)
            if movie is not None:
                movie.close()
                self._movie = None

    @property
    def duration_sec(self) -> float:
        return self.frame_count / self.fps if self.fps else 0.0


# ================================================================
# 播放器 UI（骨架在 BasePlayer；此处只接差异钩子）
# ================================================================

class ToposPlayer(BasePlayer):
    """Topos MOV 原生播放器（接口对齐 UltimatePlayer 的使用面）。"""

    PLAYER_TITLE = 'Topos Player'
    BADGE_TEXT = 'TOPOS'
    HAS_AUDIO = True  # 有音轨时显示音量条；无音轨在 _configure_session 隐藏
    # [BUG-P1-15] _on_eof 在解码线程内被调用：图标更新经信号回 GUI 线程
    # （与 libav _eof_sig 同模式，P0-09）
    _eof_signal = pyqtSignal()
    RING_SIZE = 6  # 音频主时钟下多留两帧，吸收解码抖动

    def __init__(self, parent=None):
        self._at_eof = False
        super().__init__(parent)
        self._eof_signal.connect(self._on_eof_ui)

    def _open_session(self, path: str):
        return ToposMovieSession(path)

    def _open_failed_prefix(self) -> str:
        # 不回退系统播放器——QuickTime 无法解码 TPIC，静默转交只会
        # 制造"打开了但报错"的困惑。原因经 play_failed 上抛为可见 toast。
        return 'Topos 解码失败'

    def _create_canvas(self):
        # M3：Topos 的 10/12-bit YUV 直接上 GPU；GL 不可用时自动回退
        # base_player 的 QImage 画布，播放链路不受影响。
        from ._canvas_factory import make_canvas
        return make_canvas(self)

    def _decode_frame(self, session: DecoderBackend, target: int):
        """解码线程：GPU 就绪时产出 YUV 平面，否则回退 RGB QImage。

        BasePlayer 默认 ``frame_rgb(target)`` 不传 size，会让 Topos 的
        AUTO_2K 请求完全失效（M2 遗留问题）。这里统一传画布显示尺寸，
        4K 素材预览解码到 1920×1080，快照仍走 ``_native_frame`` 全尺寸。
        """
        canvas = self.canvas
        if getattr(canvas, 'gpu_active', False) and \
                hasattr(session, 'frame_yuv'):
            try:
                return session.frame_yuv(target, size=canvas.display_size())
            except Exception:
                logger.warning('Topos GPU YUV 帧失败，回退 RGB', exc_info=True)
        rgb = session.frame_rgb(target, size=canvas.display_size())
        return _frame_to_qimage(rgb)

    def _populate_header_buttons(self, hl) -> None:
        btn_clip = _icons.make_tool_button(
            self, 'fa5s.cut', '片段截取（I 设入点 / O 设出点，导出 ProRes 422 HQ）', '✂️')
        btn_clip.clicked.connect(self._toggle_clip_panel)
        hl.addWidget(btn_clip)
        super()._populate_header_buttons(hl)

    def _build_extra_ui(self) -> None:
        # —— 片段截取面板（✂️ 切换；I/O 快捷键设点） ——
        self.clip_panel = QFrame(self)
        self.clip_panel.setObjectName('ToposPanel')
        clip = QHBoxLayout(self.clip_panel)
        clip.setContentsMargins(15, 6, 15, 6)
        self.inp_a = QLineEdit(); self.inp_a.setPlaceholderText('A --.-'); self.inp_a.setFixedWidth(78)
        self.inp_b = QLineEdit(); self.inp_b.setPlaceholderText('B --.-'); self.inp_b.setFixedWidth(78)
        btn_set_a = QPushButton('设A'); btn_set_a.setToolTip('把当前帧设为入点 (I)')
        btn_set_a.clicked.connect(lambda: self._set_clip_point('a'))
        btn_set_b = QPushButton('设B'); btn_set_b.setToolTip('把当前帧设为出点 (O)')
        btn_set_b.clicked.connect(lambda: self._set_clip_point('b'))
        self.lbl_clip_dur = QLabel('时长 --')
        btn_export = QPushButton('导出 ProRes')
        btn_export.setToolTip('libtopos 解码 → ProRes 422 HQ .mov（自编译 FFmpeg）')
        btn_export.clicked.connect(self._export_clip)
        for w in (QLabel('片段'), self.inp_a, btn_set_a, QLabel('—'),
                  self.inp_b, btn_set_b, self.lbl_clip_dur, btn_export):
            clip.addWidget(w)
        self.clip_panel.hide()

        self._clip_a: float | None = None
        self._clip_b: float | None = None

    # ---------- EOF / 播放控制（与 LibavPlayer 行为对齐，[BUG-P1-15]） ----------

    def _on_eof(self) -> None:
        # 播到片尾（解码线程内）：音频停泵，图标更新走 GUI 信号
        audio = getattr(self, '_audio', None)
        if audio is not None:
            audio.pause()
        self._eof_signal.emit()

    def _on_eof_ui(self) -> None:
        self._at_eof = True
        _icons.set_play_icon(self.btn_play, playing=False)

    def toggle_play(self) -> None:
        # 片尾按播放 → 从头重播（与 libav _on_play_state_changed 行为对齐）
        if self._at_eof and not self._playing:
            self._at_eof = False
            self._request_seek(0)
        super().toggle_play()

    def _on_seek_requested(self, target: int) -> None:
        self._at_eof = False
        audio = getattr(self, '_audio', None)
        if audio is not None and self._session is not None:
            audio.seek(target / max(1e-6, self._session.fps))

    def _ui_interval(self, session) -> int:
        if getattr(self, '_audio', None) is not None:
            return 16
        return super()._ui_interval(session)

    def _teardown_output(self) -> None:
        audio, self._audio = getattr(self, '_audio', None), None
        if audio is not None:
            audio.stop()
        source, self._audio_source = getattr(self, '_audio_source', None), None
        if source is not None:
            source.close()

    def _configure_session(self, session) -> None:
        self._at_eof = False
        self.lbl_badge.setText(
            f"TOPOS · {session.width}×{session.height} · "
            f"{session.fps:.0f}fps · {session.info.bit_depth}-bit")
        self._clip_a = self._clip_b = None
        self.inp_a.clear(); self.inp_b.clear()
        self.lbl_clip_dur.setText('时长 --')
        self.clip_panel.hide()

        # 音频：源在 GUI 线程准备，输出线程在 _after_decoder_started 启动。
        self._audio_source = None
        self._audio = None
        has_audio = bool(getattr(session, 'audio_info', None)
                         and session.audio_info.has_audio)
        if has_audio:
            try:
                from .topos_audio import make_audio_source
                self._audio_source = make_audio_source(session, session.path)
            except Exception:
                logger.warning('Topos 音频源初始化失败', exc_info=True)
                self._audio_source = None
        self.lbl_vol.setVisible(has_audio)
        self.slider_vol.setVisible(has_audio)
        if has_audio and self._audio_source is None:
            self._toast('音轨无法解码，继续静音预览')

    def _after_decoder_started(self) -> None:
        source = getattr(self, '_audio_source', None)
        if source is None:
            return
        try:
            from .topos_audio import ToposAudioPipeline
            self._audio = ToposAudioPipeline(source, self)
            self._audio.set_volume(self.slider_vol.value() / 100.0)
            self._audio.failed.connect(self._on_audio_failed)
            self._ui_timer.setInterval(16)
        except Exception:
            logger.warning('Topos 音频输出初始化失败', exc_info=True)
            self._audio = None
            self._on_audio_failed('音频输出初始化失败')

    def _on_audio_failed(self, message: str) -> None:
        self._audio = None
        self.lbl_vol.setVisible(False)
        self.slider_vol.setVisible(False)
        if self._session is not None:
            self._ui_timer.setInterval(
                max(4, int(1000 / max(1e-6, self._session.fps))))
        self._toast(message)

    def _on_play_state_changed(self) -> None:
        audio = getattr(self, '_audio', None)
        if audio is None:
            return
        if self._playing:
            audio.resume()
        else:
            audio.pause()

    def _on_volume_changed(self, value: int) -> None:
        audio = getattr(self, '_audio', None)
        if audio is not None:
            audio.set_volume(value / 100.0)

    # ---------- 帧消费：严格按序（显示速度 = 源 fps，解码由背压钳制） ----------

    def _poll_ring(self) -> None:
        audio = getattr(self, '_audio', None)
        if audio is not None and self._playing and self._session is not None:
            pos = audio.position()
            if pos is not None:
                # 帧门控以音频播放位置为准；解码前端只保留目标帧及之后的
                # 少量未来帧，落后帧直接丢弃（视频追音频，不拖慢音频）。
                fps = max(1e-6, self._session.fps)
                target = min(self._session.frame_count - 1,
                             max(0, int(pos * fps)))
                pick = None
                with self._ring_lock:
                    while self._ring:
                        self._pending.append(self._ring.popleft())
                    while self._pending:
                        idx, img = self._pending[0]
                        if idx <= self._shown_index:
                            self._pending.popleft()
                        elif idx <= target:
                            pick = self._pending.popleft()
                        else:
                            break
                    self._ring_space.notify()
                if pick is not None:
                    self._show_frame(pick)
                self.lbl_pos.setText(_fmt_time(pos))
                self.slider.blockSignals(True)
                self.slider.setValue(target)
                self.slider.blockSignals(False)
                return
        # 无音频或音频尚未起播：沿用严格的逐帧时钟。
        pick = None
        with self._ring_lock:
            while self._ring:
                self._pending.append(self._ring.popleft())
            while self._pending:
                idx = self._pending[0][0]
                if idx <= self._shown_index:      # seek 重置后的过期帧
                    self._pending.popleft()
                elif idx == self._shown_index + 1:
                    pick = self._pending.popleft()
                    break
                else:
                    # seek 竞态残留（解码前沿越过 shown+1）→ 跳显首帧
                    pick = self._pending.popleft()
                    break
            self._ring_space.notify()
        if pick is None:
            return
        self._show_frame(pick)

    def _show_frame(self, pick) -> None:
        idx, img = pick
        self._shown_index = idx
        self.canvas.set_image(img)
        self.slider.blockSignals(True)
        self.slider.setValue(idx)
        self.slider.blockSignals(False)
        if self._session:
            self.lbl_pos.setText(_fmt_time(idx / self._session.fps))

    def _on_seek_jump(self) -> None:
        # seek 后丢弃过期帧——否则满 ring 会让目标帧永远进不了队列
        with self._ring_lock:
            self._ring.clear()
            self._ring_space.notify()

    def _handle_key(self, key) -> bool:
        from PyQt6.QtCore import Qt as _Qt
        if key == _Qt.Key.Key_I:
            self._set_clip_point('a')
        elif key == _Qt.Key.Key_O:
            self._set_clip_point('b')
        elif key == _Qt.Key.Key_C:
            self._toggle_clip_panel()
        elif key == _Qt.Key.Key_S:
            self.snapshot()
        elif key == _Qt.Key.Key_Up:
            self.request_prev.emit()
        elif key == _Qt.Key.Key_Down:
            self.request_next.emit()
        else:
            return False
        return True

    # ---------- 扩展浮层 ----------

    def _overlay_panels_open(self) -> bool:
        return self.clip_panel.isVisible()

    def _relayout_extra(self) -> None:
        self.clip_panel.adjustSize()
        self.clip_panel.move((self.width() - self.clip_panel.width()) // 2,
                             self.height() - self.clip_panel.height() - 90)

    def _on_info_unavailable(self) -> None:
        self._toast('无媒体信息')

    def _info_html(self) -> str:
        s = self._session
        info = s.info
        size = os.path.getsize(s.path) if os.path.exists(s.path) else 0
        dur = s.duration_sec
        bitrate = f"{size * 8 / dur / 1e6:.0f} Mbps" if dur > 0 else '--'
        chroma = '4:4:4' if info.pixel_format == 1 else '4:2:2'
        rows = [
            ('编码', f"<b style='color:#0A84FF'>Topos Video Codec 2.0</b> · profile {info.profile}"),
            ('分辨率', f"{s.width}×{s.height}"),
            ('帧率', f"{s.fps:.2f} fps（{s.frame_count} 帧）"),
            ('位深', f"{info.bit_depth}-bit · {chroma}"),
            ('色彩', topos_color_info(info)),
            ('码率', bitrate),
            ('时长', f"{_fmt_time(dur)}.{int(dur * 100 % 100):02d}"),
            ('文件', f"{size / 1e6:.1f} MB"),
        ]
        ai = getattr(s, 'audio_info', None)
        if ai is not None and getattr(ai, 'has_audio', False):
            codec = 'LPCM' if int(ai.codec) == 1 else 'AAC'
            rows.insert(7, ('音频', f"{codec} · {ai.channel_count}ch · "
                               f"{ai.sample_rate} Hz · {ai.bits_per_sample}-bit"))
        body = ''.join(
            f"<tr><td style='color:#8a8a8a;padding-right:14px'>{k}</td>"
            f"<td>{v}</td></tr>" for k, v in rows)
        return (f"<table cellspacing='0' cellpadding='2'>{body}</table>"
                f"<div style='color:#666;margin-top:6px'>{s.path}</div>")

    # ---------- 片段截取（自编译 FFmpeg：libtopos 解码 → ProRes） ----------

    def _toggle_clip_panel(self) -> None:
        self.clip_panel.setVisible(not self.clip_panel.isVisible())
        self._relayout()

    def _set_clip_point(self, which: str) -> None:
        if self._session is None or self._shown_index < 0:
            self._toast('无可用帧')
            return
        t = self._shown_index / self._session.fps
        (self.inp_a if which == 'a' else self.inp_b).setText(f'{t:.2f}')
        self._update_clip_dur()

    @staticmethod
    def _parse_t(text: str) -> float:
        s = text.strip()
        if ':' in s:
            m, sec = s.split(':', 1)
            return float(m) * 60 + float(sec)
        return float(s)

    def _update_clip_dur(self) -> None:
        try:
            a = self._parse_t(self.inp_a.text())
            b = self._parse_t(self.inp_b.text())
            self.lbl_clip_dur.setText(f'时长 {abs(b - a):.2f}s')
        except ValueError:
            self.lbl_clip_dur.setText('时长 --')

    def _export_clip(self) -> None:
        if self._session is None or not self._current_path:
            self._toast('无媒体')
            return
        try:
            a = self._parse_t(self.inp_a.text())
            b = self._parse_t(self.inp_b.text())
        except ValueError:
            self._toast('请先设置 A/B 点（设A/设B 或 I/O 键）')
            return
        if b <= a:
            a, b = b, a
        dur = round(b - a, 3)
        ffmpeg = utils_get_tool_path('ffmpeg')
        if not ffmpeg:
            self._toast('未找到内置 FFmpeg（bin/ 缺失）')
            return
        folder, filename = os.path.split(self._current_path)
        stem = os.path.splitext(filename)[0]
        out = os.path.join(folder, f'{stem}_clip_{int(time.time())}.mov')
        tmp = out + '.part'  # 原子交付：写完才改名，避免半成品文件
        cmd = [ffmpeg, '-y', '-ss', f'{a:.3f}', '-i', self._current_path,
               '-t', f'{dur:.3f}', '-c:v', 'prores_ks', '-profile:v', '3',
               '-pix_fmt', 'yuv422p10le', '-f', 'mov', tmp]
        self._toast(f'正在导出片段 {dur:.1f}s → ProRes 422 HQ …')

        def run():
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                os.replace(tmp, out)
                # [BUG-P0-07] 工作线程直接 _toast（setText + QTimer）间歇崩溃，
                # 经信号投递到 GUI 线程
                self.toast_requested.emit(f'片段导出完成: {os.path.basename(out)}')
            except subprocess.CalledProcessError as e:
                logger.error('ffmpeg probe 失败: %s', e.stderr.decode(errors='ignore')[-800:])
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                self.toast_requested.emit('片段导出失败（详见控制台）')
        threading.Thread(target=run, daemon=True).start()
