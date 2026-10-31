"""
Topos Video Codec 原生播放器（video_pro 插件）。

解码：src/shared/codec/topos_binding（libtopos_codec，O(1) 索引 + 多线程切片解码）
显示：swscale（yuv422/444p10le → rgb24）→ QImage 直绘

- 全 intra：任意帧独立解码，进度条拖动零寻址延迟；
- 解码线程后台预取（环形缓冲 4 帧 + 背压），UI 定时器按源 fps 严格按序显示；
- 快照原生全尺寸直出（不依赖 ffmpeg CLI）；
- 片段导出走自编译 FFmpeg（libtopos 解码 → ProRes 422 HQ 交付格式）；
- 信息面板（编解码/分辨率/帧率/位深/色彩/码率）、设为封面、点击暂停。

[ARCH-2] UI 骨架/解码线程生命周期/快照/封面统一在 base_player.BasePlayer，
本文件保留：ToposMovieSession 解码会话 + 严格按序的帧消费 + 片段截取面板。
"""
from __future__ import annotations

import os

import logging
logger = logging.getLogger(__name__)
import sys
import subprocess
import threading
import time
from pathlib import Path

# 让 external/file_explorer 下的运行环境能 import 仓库共享层
_REPO_ROOT = str(Path(__file__).resolve().parents[4])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QVBoxLayout)

from . import icons as _icons
from .base_player import (BasePlayer, DecoderBackend, STYLESHEET, _fmt_time,
                          _frame_to_qimage, _VideoCanvas as _ToposCanvas,
                          utils_get_tool_path)  # noqa: F401  (libav/外部沿用旧导入名)
from .topos_media import topos_color_info  # noqa: F401  (topos_media 元数据面板沿用)

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
        self.fps = float(info.timescale) if info.timescale else 24.0
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
        self._y = np.empty((self.height, self.width), dtype='<u2')
        self._u = np.empty((self.height, (self.width + 1) // 2), dtype='<u2')
        self._v = np.empty((self.height, (self.width + 1) // 2), dtype='<u2')
        self._decoder = ToposDecoder(self._codec, max_slice_workers=slice_workers)
        self._views = self._decoder.make_views([self._y, self._u, self._v])

    def frame_rgb(self, index: int, size=None):
        """解码第 index 帧 → (H, W, 3) uint8 RGB。

        size=(w, h) 时 swscale 一步完成转色+缩放到显示分辨率
        （4K 预览的实时性关键路径）；None = 原生全尺寸（快照用）。
        """
        with self._lock:
            return self._frame_rgb_locked(index, size)

    def _frame_rgb_locked(self, index: int, size=None):
        buf, size_n = self._movie.packet_into(index)
        self._decoder.decode_views(buf, self._views, 3, size=size_n)
        frame = self._av.VideoFrame(self.width, self.height, format=self.vfmt)
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

    def close(self) -> None:
        with self._lock:
            dec = getattr(self, '_decoder', None)
            if dec is not None:
                dec.close()
                self._decoder = None
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
    HAS_AUDIO = False  # [BUG-P1-17] TPIC 容器无音轨：隐藏音量条
    # [BUG-P1-15] _on_eof 在解码线程内被调用：图标更新经信号回 GUI 线程
    # （与 libav _eof_sig 同模式，P0-09）
    _eof_signal = pyqtSignal()

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
        # 播到片尾（解码线程内）：只发信号
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

    def _configure_session(self, session) -> None:
        self._at_eof = False
        self.lbl_badge.setText(
            f"TOPOS · {session.width}×{session.height} · "
            f"{session.fps:.0f}fps · {session.info.bit_depth}-bit")
        self._clip_a = self._clip_b = None
        self.inp_a.clear(); self.inp_b.clear()
        self.lbl_clip_dur.setText('时长 --')
        self.clip_panel.hide()

    # ---------- 帧消费：严格按序（显示速度 = 源 fps，解码由背压钳制） ----------

    def _poll_ring(self) -> None:
        # 严格按序消费：每 tick 至多前进一帧（显示速度 = 源 fps）。
        # 解码速率由 ring 背压天然钳制，无需跳帧。
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
