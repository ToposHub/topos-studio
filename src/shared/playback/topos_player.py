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
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
from .frame_geometry import fit_aspect_size
from .base_player import (BasePlayer, DecoderBackend, _fmt_time,
                          _frame_to_qimage,
                          utils_get_tool_path)  # noqa: F401  (libav/外部沿用旧导入名)
from .topos_media import topos_color_info  # noqa: F401  (topos_media 元数据面板沿用)

if TYPE_CHECKING:
    import numpy as np

    from src.shared.codec.decode_worker_budget import DecodeWorkerLease
    from src.shared.codec.topos_binding import ToposDecoder
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


@dataclass
class _DecodeBuffer:
    """One plane slot, including the array owners backing its C views."""

    geometry: tuple[tuple[int, int], ...] = ()
    planes: tuple[np.ndarray, ...] = ()
    views: Any = None

    def ensure(self, decoder: ToposDecoder, geometry: tuple[tuple[int, int], ...]) -> None:
        if self.geometry == geometry:
            return
        import numpy as np

        planes = tuple(np.empty((height, width), dtype='<u2') for width, height in geometry)
        self.views = decoder.make_views(planes)
        self.planes = planes
        self.geometry = geometry

    @property
    def nbytes(self) -> int:
        return sum(plane.nbytes for plane in self.planes)


@dataclass(frozen=True)
class _PreviewRequest:
    index: int
    slot: int
    result: Future[_DecodeBuffer]


class ToposMovieSession(DecoderBackend):
    """一次打开的 Topos MOV：绑定层句柄 + 解码视图 + 帧→RGB 转换。

    沿用独立播放器 ToposKit 的一帧预取和双平面缓冲设计。原生解码保持串行，
    下一帧解码与当前帧出图重叠。像素格式支持 10/12-bit 4:2:2 / 4:4:4。
    """

    _PREVIEW_PLANES_BUDGET = 96 * 1024 * 1024

    def __init__(self, path: str, slice_workers: int = 0, *, prefetch: bool = False) -> None:
        ToposCodec, ToposMovieFile, ToposDecoder = _load_binding()
        import av  # 延迟到真正播放时再加载（~200ms）
        self._av = av

        self._lock = threading.Lock()
        self._audio_lock = threading.Lock()
        self._closed = False
        self._pending: _PreviewRequest | None = None
        self._preview_worker: ThreadPoolExecutor | None = None
        self._preview_buffers = (_DecodeBuffer(), _DecodeBuffer())
        self._full_buffer = _DecodeBuffer()
        self._worker_lease: DecodeWorkerLease | None = None
        self._rgb_input = None
        self._prefetch = prefetch and 'TOPOS_DISABLE_PREFETCH' not in os.environ

        self.path = path
        self._codec = ToposCodec()
        self._movie = ToposMovieFile(self._codec, path)
        try:
            info = self._movie.info()
        except BaseException:
            self.close()
            raise
        self.info = info
        self.width = info.width
        self.height = info.height
        self.frame_count = info.sample_count
        # [E4] Topos 容器无旋转元数据概念：显示旋转恒 0（与 libav 会话
        # 的 rotation_quarters 契约对齐，播放器侧守卫统一生效）
        self.rotation_quarters = 0
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

        try:
            # 与独立播放器采用同一自动线程范围，并与剪辑/导出共享宿主预算。
            requested = slice_workers or min(max(4, os.cpu_count() or 1), 16)
            self._decoder = ToposDecoder(self._codec, max_slice_workers=requested)
            if getattr(self._codec, '_has_decoder_worker_budget', False):
                from src.shared.codec.decode_worker_budget import DECODE_WORKER_BUDGET

                self._worker_lease = DECODE_WORKER_BUDGET.acquire(
                    f'player:{id(self)}', role='playback', requested=requested,
                    apply=self._decoder.set_max_slice_workers)

            from src.shared.codec.topos_binding import TC_DECODE_MODE_AUTO_2K

            self._auto_request = (self._decoder.make_request(TC_DECODE_MODE_AUTO_2K)
                                  if getattr(self._codec, '_has_decoder_request', False) else None)
            self._try_base_layer = (
                getattr(info, 'bitstream_major', 0) == 7
                and getattr(self._codec, '_has_movie_packet_base', False)
                and 'TOPOS_DISABLE_BASE' not in os.environ)
        except BaseException:
            self.close()
            raise

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

    def frame_rgb(self, index: int, size: tuple[int, int] | None = None) -> np.ndarray:
        """解码第 index 帧 → (H, W, 3) uint8 RGB。

        size=(w, h) 时 swscale 一步完成转色+缩放到显示分辨率
        （4K 预览的实时性关键路径）；None = 原生全尺寸（快照用）。
        """
        with self._lock:
            return self._frame_rgb_locked(index, size)

    def frame_yuv(self, index: int, size: tuple[int, int] | None = None) -> YuvFrame:
        """解码第 index 帧 → :class:`YuvFrame`（GPU 预览路径）。

        与 ``frame_rgb`` 共用同一条 decode/auto2k 路径，但不经过
        swscale，也不把 10/12-bit 压成 8-bit；三平面复制成本帧独立数组，
        避免解码器缓冲被下一帧覆盖。``size`` 非空时启用 AUTO_2K。
        """
        from .yuv_frame import YuvFrame

        with self._lock:
            y, u, v = self._read_planes_locked(index, preview=size is not None).planes[:3]
            pixel_format = '444' if int(self.info.pixel_format) == 1 else '422'
            return YuvFrame(
                y=y.copy(),
                u=u.copy(),
                v=v.copy(),
                bit_depth=int(self.info.bit_depth),
                subsampling=pixel_format,
                matrix=int(self.info.color_matrix),
                color_range=int(self.info.color_range),
                chroma_siting=int(self.info.chroma_siting),
                pixel_format=pixel_format,
                source_width=self.width,
                source_height=self.height,
            )

    def _decode_into(self, index: int, preview: bool, buffer: _DecodeBuffer) -> _DecodeBuffer:
        """读包和解码串行。V7-B 预览只读 base 层，并直接按 base 尺寸解码。"""
        request = self._auto_request if preview else None
        if preview and self._try_base_layer:
            packet, size, is_base = self._movie.packet_base_into(index)
            # 对首个预览包探测基础层。V7-A 的接口返回完整包，之后走常规读取。
            self._try_base_layer = is_base
            if is_base:
                request = None
        else:
            packet, size = self._movie.packet_into(index)

        decoder = self._decoder
        geometry = (decoder.prepare_request(packet, request, size) if request is not None
                    else decoder.prepare(packet, size))
        if (int(geometry.pixel_format), int(geometry.bit_depth)) != (
                int(self.info.pixel_format), int(self.info.bit_depth)):
            raise ValueError('Topos 帧像素格式或位深与容器声明不一致')
        buffer.ensure(decoder, tuple(self._codec.plane_geometry(geometry, plane)
                                     for plane in range(int(geometry.plane_count))))
        if request is None:
            decoder.decode_views(packet, buffer.views, len(buffer.planes), size)
        else:
            decoder.decode_views_request(packet, buffer.views, request, size)
        return buffer

    def _read_planes_locked(self, index: int, *, preview: bool) -> _DecodeBuffer:
        if self._closed:
            raise RuntimeError('Topos 解码会话已关闭')

        pending, self._pending = self._pending, None
        slot = 0
        if pending is not None and preview and pending.index == index:
            slot = pending.slot
            buffer = pending.result.result()
        else:
            if pending is not None and not pending.result.cancel():
                try:
                    pending.result.result()
                except (RuntimeError, ValueError):
                    # 定位作废的帧不会交给调用方。同一原生 context 要等它退出。
                    logger.debug('Discarding failed Topos prefetch', exc_info=True)
            buffer = self._decode_into(index, preview,
                                       self._preview_buffers[slot] if preview else self._full_buffer)

        if (preview and self._prefetch and index + 1 < self.frame_count
                and buffer.nbytes * 2 <= self._PREVIEW_PLANES_BUDGET):
            next_slot = 1 - slot
            if self._preview_worker is None:
                self._preview_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='topos-prefetch')
            future = self._preview_worker.submit(
                self._decode_into, index + 1, True, self._preview_buffers[next_slot])
            self._pending = _PreviewRequest(index + 1, next_slot, future)
        return buffer

    def _frame_rgb_locked(self, index: int, size: tuple[int, int] | None = None) -> np.ndarray:
        buffer = self._read_planes_locked(index, preview=size is not None)
        width, height = buffer.geometry[0]
        frame = self._rgb_input
        if frame is None or (frame.width, frame.height) != (width, height):
            frame = self._av.VideoFrame(width, height, format=self.vfmt)
            self._rgb_input = frame
        for destination, source in zip(frame.planes, buffer.planes):
            if destination.buffer_size == source.nbytes:
                destination.update(source.data)
            else:
                # FFmpeg 会在行尾对齐 stride，尤其是非标准宽度和缩小后的色度平面。
                import numpy as np

                view = np.ndarray(source.shape, dtype=source.dtype, buffer=destination,
                                  strides=(destination.line_size, source.itemsize))
                view[:] = source
        # H.273 码值与 AVColorSpace 同源直传；topos range: 0=limited(tv), 1=full
        frame.colorspace = int(self.info.color_matrix)
        frame.color_range = 2 if self.info.color_range == 1 else 1
        if size:
            width, height = fit_aspect_size(self, int(size[0]), int(size[1]))
            out = frame.reformat(width=width, height=height, format='rgb24')
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
            if self._closed:
                return
            self._closed = True
            pending, self._pending = self._pending, None
            worker, self._preview_worker = self._preview_worker, None
            if worker is not None:
                worker.shutdown(wait=False, cancel_futures=True)
            with self._audio_lock:
                audio_movie = getattr(self, '_audio_movie', None)
                if audio_movie is not None:
                    audio_movie.close()
                    self._audio_movie = None
            if pending is not None and not pending.result.done():
                pending.result.add_done_callback(self._release_video)
            else:
                self._release_video()

    def _release_video(self, _result: Future[_DecodeBuffer] | None = None) -> None:
        """原生工作结束后才销毁 context，关闭窗口不等待预取。"""
        if self._worker_lease is not None:
            self._worker_lease.release()
            self._worker_lease = None
        decoder = getattr(self, '_decoder', None)
        if decoder is not None:
            decoder.close()
            self._decoder = None
        movie = getattr(self, '_movie', None)
        if movie is not None:
            movie.close()
            self._movie = None
        self._preview_buffers = (_DecodeBuffer(), _DecodeBuffer())
        self._full_buffer = _DecodeBuffer()
        self._rgb_input = None

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
    # [E3] EOF 不再由解码线程信号投递：呈现完成在 GUI 线程判定并收尾
    RING_SIZE = 6  # 音频主时钟下多留两帧，吸收解码抖动

    def __init__(self, parent=None):
        self._at_eof = False
        super().__init__(parent)

    def _open_session(self, path: str):
        return ToposMovieSession(path, prefetch=True)

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
        # [E4] 与 libav 同款守卫：有显示旋转走 RGB 路径（Topos 格式本身
        # 无旋转概念，rotation_quarters 恒 0——守卫保持两引擎语义一致）
        if getattr(canvas, 'gpu_active', False) and \
                hasattr(session, 'frame_yuv') and \
                getattr(session, 'rotation_quarters', 0) == 0:
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

    def _audio_drained(self) -> bool:
        """[E3] 音频主时钟引擎：排空 = 音源读完 + 待写清空 + sink 缓冲放完。"""
        audio = getattr(self, '_audio', None)
        if audio is None:
            return True
        return audio.drained()

    def _on_playback_finished(self) -> None:
        # [E3] 末帧已实际呈现且音频已排空（GUI 线程）：音频停泵 + 片尾标记
        audio = getattr(self, '_audio', None)
        if audio is not None:
            audio.pause()
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
                self._maybe_publish_eof()  # [E3] 音频排空 + 末帧呈现后才收尾
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
            self._maybe_publish_eof()  # [E3] 无帧可显也查：末帧已显+音频排空即收尾
            return
        self._show_frame(pick)
        self._maybe_publish_eof()

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
        if getattr(self, '_clip_job', None) is not None and \
                self._clip_job.is_alive() and not self._clip_job.cancelled:
            self.cancel_clip_export()   # 再点一次 = 取消进行中的导出
            self.toast_requested.emit('正在取消导出…')
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
        # [E6] 预算/取消/有界输出/原子交付统一走 ClipExportJob；命令写
        # 入 job 的 .part 临时文件，成功才 os.replace 到目标
        from .clip_export import ClipExportJob
        job = ClipExportJob(
            [ffmpeg, '-y', '-ss', f'{a:.3f}', '-i', self._current_path,
             '-t', f'{dur:.3f}', '-c:v', 'prores_ks', '-profile:v', '3',
             '-pix_fmt', 'yuv422p10le', '-f', 'mov'],   # 输出 = tmp_path
            destination=out)
        self._clip_job = job
        self._toast(f'正在导出片段 {dur:.1f}s → ProRes 422 HQ …（可再点一次取消）')

        def on_done(ok: bool, message: str) -> None:
            # [BUG-P0-07] 工作线程只经信号投递 GUI 线程
            if ok:
                self.toast_requested.emit(
                    f'片段导出完成: {os.path.basename(message)}')
            else:
                logger.error('片段导出失败: %s | ffmpeg 尾部输出: %s',
                             message, job.stderr_tail[-800:])
                self.toast_requested.emit(f'片段导出未完成：{message}')
        job.start(on_done)

    def cancel_clip_export(self) -> None:
        """[E6] 取消进行中的片段导出（预算内协作终止）。"""
        job = getattr(self, '_clip_job', None)
        if job is not None:
            job.cancel()
