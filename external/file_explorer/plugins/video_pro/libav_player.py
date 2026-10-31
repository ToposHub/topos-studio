"""LibavPlayer —— PyAV(FFmpeg) 多线程解码的通用视频播放器。

为 ProRes / DNxHR 等 mezzanine 编码而生：Qt Multimedia 的 FFmpeg 后端
单线程软解，4K 只有 17~23fps；本播放器解码线程 thread_count=auto，
实测 4K ProRes422HQ 52fps、DNxHR 54fps、ProRes4444 31fps，全部实时。

架构与 topos_player.ToposPlayer 一致（[ARCH-2] 骨架统一在
base_player.BasePlayer）：后台解码线程 + 环形缓冲背压 + 浮层控件
（3 秒无操作自动隐藏）。本文件保留：_LibavSession 解码会话 +
_AudioPipeline 音频管线 + 主时钟门控的帧消费。
二期加入音频：墙钟主时钟驱动画面节奏，_AudioPipeline 独立线程按 pts
预写入 QAudioSink（写入领先时钟 ≤0.25s，欠载丢音追时钟），无音轨素材
自动退化为无声墙钟播放。
"""
from __future__ import annotations

import os

import logging
logger = logging.getLogger(__name__)
import time
import threading

import numpy as np
import av
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices

from . import icons as _icons
from .base_player import (BasePlayer, DecoderBackend, STYLESHEET, _fmt_time,
                          _frame_to_qimage)

_MEZZANINE_CODECS = {'prores', 'dnxhd'}
_SLOW_UHD_CODECS = {'hevc', 'h265'}  # Qt 软解 4K HEVC 只有 ~17fps

# —— 音频（二期）：统一重采样 48kHz 立体声 s16 推给 QAudioSink ——
_AUDIO_RATE = 48000
_AUDIO_BYTES_PER_SEC = _AUDIO_RATE * 4  # s16 双声道
_AUDIO_LEAD = 0.25      # 写入领先主时钟的上限（秒），抗解码/IO 抖动
_AUDIO_AHEAD_CAP = 1.0  # 解码前置缓冲上限（秒），防积压


class _AudioPipeline:
    """音频解码线程 + QAudioSink 推送播放。

    主时钟由 LibavPlayer 持有（墙钟），本类只负责让声音跟上时钟：
    按 pts 门控写入（领先时钟 ≤ _AUDIO_LEAD）；sink 欠载时丢弃落后
    数据追平时钟。所有 QAudioSink 调用都发生在本工作线程内。
    """

    def __init__(self, path: str, clock_fn):
        self._path = path
        self._clock_fn = clock_fn
        self.audio_codec = ''
        self.audio_rate = 0
        self.audio_layout = ''
        self._container = None
        self._stream = None
        self._resampler = None
        self._iter = None
        self._sink = None
        self._io = None
        self._thread = None
        self._slock = threading.Lock()
        self._running = False
        self._paused = False
        self._seek_to: float | None = None
        self._seek_trim: float | None = None
        self._volume: float | None = None  # [BUG-P1-17] 待应用的音量（None=未变）

    # -- 线程安全接口（GUI 线程调用） --

    def probe(self) -> bool:
        """是否存在可用音轨（只读元数据，不启动线程）。"""
        try:
            with av.open(self._path) as c:
                if not c.streams.audio:
                    return False
                st = c.streams.audio[0]
                self.audio_codec = st.codec_context.name or '?'
                self.audio_rate = int(st.rate or 48000)
                self.audio_layout = st.layout.name
                return True
        except Exception:
            return False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        with self._slock:
            self._paused = True

    def resume(self) -> None:
        with self._slock:
            self._paused = False

    def seek(self, t: float) -> None:
        with self._slock:
            self._seek_to = max(0.0, t)

    def set_volume(self, v: float) -> None:
        """[BUG-P1-17] 设置音量 0.0-1.0；sink 在工作线程手中，这里只记录
        待应用值，由 _loop 在自己的线程内调 QAudioSink.setVolume。
        [U10] 删掉 GUI 线程直接 sink.setVolume 的旁路（与 docstring 矛盾）：
        sink 由音频线程创建持有，按 Qt 线程亲和性不可跨线程调用。"""
        with self._slock:
            self._volume = max(0.0, min(1.0, v))

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            # [BUG-P2-28] 此前在 GUI 线程 join 1.5s（拖动进度条/关播放器
            # 卡 UI）。线程主循环 4ms 轮询 _running，正常几十 ms 内退出；
            # 罕见卡在解码调用时放弃等待，sink 由线程 finally 自行清理。
            self._thread.join(timeout=0.25)
            self._thread = None

    # -- 工作线程 --

    def _make_resampler(self) -> av.AudioResampler:
        return av.AudioResampler(format='s16', layout='stereo',
                                 rate=_AUDIO_RATE)

    def _pull(self):
        """解出下一块 s16 立体声数据 → (data, 起始秒|None)；EOF → (None, None)。"""
        while True:
            try:
                frame = next(self._iter)
            except (StopIteration, av.error.EOFError):
                return None, None
            t0 = (float(frame.pts * self._stream.time_base)
                  if frame.pts is not None else None)
            outs = self._resampler.resample(frame)
            if outs:
                return b''.join(bytes(o.planes[0]) for o in outs), t0
            # 重采样器可能吞包（仅元数据的首包等），继续读下一包

    def _loop(self) -> None:
        try:
            self._container = av.open(self._path)
            self._stream = self._container.streams.audio[0]
            self._resampler = self._make_resampler()
            self._iter = self._container.decode(audio=0)
            fmt = QAudioFormat()
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            fmt.setSampleRate(_AUDIO_RATE)
            fmt.setChannelCount(2)
            self._sink = QAudioSink(QMediaDevices.defaultAudioOutput(), fmt)
            self._sink.setBufferSize(int(0.5 * _AUDIO_BYTES_PER_SEC))
            # [BUG-P1-17] 管线启动前记录的音量在此应用
            with self._slock:
                if self._volume is not None:
                    try: self._sink.setVolume(self._volume)
                    except Exception: pass
            self._io = self._sink.start()
        except Exception:
            import traceback
            traceback.print_exc()
            return
        pend = bytearray()
        pend_t0 = 0.0
        suspended = False
        applied_vol = None
        try:
            while self._running:
                with self._slock:
                    tgt, paused = self._seek_to, self._paused
                    self._seek_to = None
                    vol = self._volume
                # [U10] 音量在本线程应用（set_volume 只记账）；
                # 变更检测避免每 4ms 重复 setVolume
                if vol is not None and vol != applied_vol:
                    try:
                        self._sink.setVolume(vol)
                        applied_vol = vol
                    except Exception:
                        pass
                if tgt is not None:
                    # [BUG-P2-29] seek 路径异常此前直接逃逸杀死整个音频线程
                    # （画面继续、声音死亡、无提示）。失败记日志后继续运行。
                    try:
                        self._sink.stop()  # 丢弃缓冲
                        pend.clear()
                        st = self._stream
                        self._container.seek(
                            int(tgt / float(st.time_base)) + (st.start_time or 0),
                            stream=st, backward=True)
                        self._resampler = self._make_resampler()
                        self._iter = self._container.decode(audio=0)
                        self._seek_trim = tgt
                        pend_t0 = tgt
                        self._sink.setBufferSize(int(0.5 * _AUDIO_BYTES_PER_SEC))
                        self._io = self._sink.start()
                    except Exception:
                        import traceback
                        traceback.print_exc()
                        logger.warning("音频 seek 失败（声音继续，位置可能短暂不同步）")
                    suspended = False
                    continue
                if paused:
                    if not suspended:
                        self._sink.suspend()
                        suspended = True
                    time.sleep(0.02)
                    continue
                if suspended:
                    self._sink.resume()
                    suspended = False
                clock = self._clock_fn()
                # 1) 解码前置（限幅防积压）
                if len(pend) < int(_AUDIO_AHEAD_CAP * _AUDIO_BYTES_PER_SEC):
                    try:
                        data, t0 = self._pull()
                    except Exception:
                        import traceback
                        traceback.print_exc()
                        data, t0 = None, None
                        time.sleep(0.05)
                    if data is not None:
                        if self._seek_trim is not None:
                            if t0 is not None and t0 < self._seek_trim:
                                cut = self._seek_trim - t0
                                data = data[int(cut * _AUDIO_RATE) * 4:]
                                t0 += cut
                            self._seek_trim = None
                        if data:
                            if not pend and t0 is not None:
                                pend_t0 = t0  # 与真实 pts 对齐（含跳跃/间隙）
                            pend.extend(data)
                # 2) 欠载快进：sink 空转且待写数据已落后时钟 → 丢音追平
                if (self._sink.state() == QAudio.State.IdleState
                        and pend and pend_t0 < clock - 0.02):
                    del pend[:int((clock - pend_t0) * _AUDIO_RATE) * 4]
                    pend_t0 = clock
                # 3) 按 pts 门控写入（领先 ≤ _AUDIO_LEAD）
                while pend and self._running:
                    with self._slock:
                        if self._seek_to is not None or self._paused:
                            break
                    if pend_t0 > clock + _AUDIO_LEAD:
                        break
                    free = self._sink.bytesFree()
                    if free <= 0:
                        break
                    n = min(free, len(pend))
                    w = self._io.write(bytes(pend[:n]))
                    if w <= 0:
                        break
                    pend_t0 += w / _AUDIO_BYTES_PER_SEC
                    del pend[:w]
                time.sleep(0.004)
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            try:
                if self._sink is not None:
                    self._sink.stop()
            except Exception:
                pass
            try:
                if self._container is not None:
                    self._container.close()
            except Exception:
                pass


_mezzanine_cache: dict = {}
_MEZZANINE_CACHE_MAX = 512


def _compute_mezzanine(path: str) -> bool:
    try:
        with av.open(path) as c:
            st = c.streams.video[0]
            name = (st.codec_context.name or '').lower()
            if name in _MEZZANINE_CODECS:
                return True
            return (name in _SLOW_UHD_CODECS
                    and (st.codec_context.width or 0) >= 3840)
    except Exception:
        return False


def is_mezzanine(path: str) -> bool:
    """按编码判定路由：ProRes/DNxHR 及 4K+ HEVC 走 LibavPlayer 多线程解码。

    Qt 单线程软解 4K HEVC 只有 ~17fps（顿挫），PyAV 多线程实测满帧
    且音画同步，故 4K+ HEVC 一并路由到本播放器。

    [BUG-P2-30] 缓存键含源文件 mtime：此前 lru_cache(path) 让原位重编码
    （同一文件换成普通编码）后的路由结果永不更新。
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    key = (path, mtime)
    hit = _mezzanine_cache.get(key)
    if hit is not None:
        return hit
    result = _compute_mezzanine(path)
    if len(_mezzanine_cache) >= _MEZZANINE_CACHE_MAX:
        _mezzanine_cache.clear()
    _mezzanine_cache[key] = result
    return result


class _LibavSession(DecoderBackend):
    """PyAV 会话：顺序解码 + 断点自动 seek，接口对齐 ToposMovieSession。"""

    def __init__(self, path: str):
        self._path = path
        self._container = av.open(path)
        try:
            # [BUG-P0-06] 纯音频/损坏文件没有视频流：IndexError 上抛前
            # 必须 close container，否则每次误开都泄漏 native demuxer + 句柄
            self._stream = self._container.streams.video[0]
            self._stream.thread_type = 'FRAME'
            self._stream.thread_count = 0  # 0 = 自动多线程
            self.fps = float(self._stream.average_rate or 25)
            dur = None
            if self._stream.duration:
                dur = float(self._stream.duration * self._stream.time_base)
            elif self._container.duration:
                dur = self._container.duration / av.time_base
            if dur is None:
                # [BUG-P2-41] 流/容器时长全缺失（live/mkv 变体）时此前
                # frame_count=1：进度条坍缩、每次 seek 全量重探。按文件体积
                # 与总码率估算兜底。
                try:
                    br = getattr(self._container, 'bit_rate', 0) or 0
                    size = os.path.getsize(self._path)
                    if br > 0 and size > 0:
                        dur = size * 8.0 / float(br)
                except OSError:
                    pass
            self.duration_sec = dur or 0.0
            fc = self._stream.frames
            self.frame_count = int(fc) if fc else max(1, int(self.duration_sec * self.fps))
            vs = self._stream.codec_context
            self.codec_name = (vs.name or '?').lower()
            self.width, self.height = vs.width, vs.height
            self.pix_fmt = str(vs.pix_fmt)
            self.bit_depth = (getattr(vs, 'bits_per_raw_sample', 0)
                              or getattr(vs, 'bits_per_coded_sample', 0) or 8)
        except Exception:
            self.close()
            raise
        self._iter = None
        self._next_idx = 0  # 迭代器当前指向的帧号
        self._lock = threading.Lock()

    # -- 索引/时间换算 --

    def _pts_of(self, index: int) -> int:
        tb = float(self._stream.time_base)
        return int(max(0.0, index / self.fps - 0.08 / max(1.0, self.fps)) / tb)

    def _idx_of(self, frame) -> int:
        t = frame.time
        if t is None:
            # [BUG-P3-60] seek 后 _next_idx 可能是 None：此前原样返回，
            # 后续 idx >= index 比较即 TypeError，陷入 0.05s 退避死循环
            return max(0, self._next_idx or 0)
        return max(0, int(round(t * self.fps)))

    # -- 随机访问解码（顺序命中时不 seek，享受多线程吞吐） --

    def frame_rgb(self, index: int, size=None):
        with self._lock:
            need_seek = self._iter is None or self._next_idx != index
            if need_seek:
                self._container.seek(self._pts_of(index), stream=self._stream,
                                     backward=True)
                self._iter = self._container.decode(video=0)
                self._next_idx = None
            # 跳过 seek 落点之前的帧（关键帧对齐）
            while True:
                frame = next(self._iter)
                idx = self._idx_of(frame)
                if self._next_idx is None:
                    self._next_idx = idx
                if idx >= index:
                    break
            self._next_idx = index + 1
            if size:
                w, h = size
                # 偶数对齐（swscale 要求）
                w -= w % 2
                h -= h % 2
                out = frame.to_ndarray(format='rgb24', width=w, height=h)
                # 带 size 的转换可能返回非连续 buffer，QImage 拒收 —— 归一化
                return np.ascontiguousarray(out)
            return frame.to_ndarray(format='rgb24')

    def close(self):
        try:
            self._container.close()
        except Exception:
            pass


class LibavPlayer(BasePlayer):
    """ProRes/DNxHR 等 mezzanine 的多线程解码播放器（音频同步，UI 对齐 ToposPlayer）。"""

    JOIN_TIMEOUT = 2.0
    PLAYER_TITLE = 'Libav Player'
    BADGE_TEXT = 'MEZZANINE'
    HAS_AUDIO = True
    # [BUG-P0-09] _on_eof 在解码线程内被调用：QPushButton setIcon/重绘
    # 必须回 GUI 线程，经信号投递
    _eof_sig = pyqtSignal()

    def __init__(self, parent=None):
        self._audio: _AudioPipeline | None = None
        self._audio_active = False
        self._t_base = 0.0        # 主时钟：最近一次起步的时间轴位置（秒）
        self._t_mark: float | None = None  # 播放中的墙钟锚点；暂停时为 None
        super().__init__(parent)
        self._eof_sig.connect(self._on_eof_ui)

    # ---------- 会话 / 输出接线 ----------

    def _open_session(self, path: str):
        return _LibavSession(path)

    def _open_failed_prefix(self) -> str:
        return '打开失败'

    def _configure_session(self, session) -> None:
        # 二期：音频管线（有音轨才启动，无音轨自动退化为无声墙钟）
        self._audio = _AudioPipeline(self._current_path, self._master_clock)
        self._audio_active = self._audio.probe()
        # [U10] 新管线继承滑杆音量：管线启动时 _volume=None → sink 默认 1.0，
        # 上一文件调到 20% 后换文件会以 100% 播放直到用户再动滑杆
        try:
            self._audio.set_volume(self.slider_vol.value() / 100.0)
        except Exception:
            pass
        self._reset_clock(0.0, playing=True)
        self.lbl_badge.setText(
            f"{session.codec_name.upper()} · {session.width}×{session.height} · "
            f"{session.fps:.0f}fps · {session.bit_depth}-bit · "
            + ('有声' if self._audio_active else '无声'))
        self.lbl_badge.setToolTip(
            '多线程解码 + 音频同步预览' if self._audio_active
            else '该素材无音轨，画面按主时钟播放')

    def _ui_interval(self, session) -> int:
        # 消费定时器跑 2×帧率：时钟门控下更平滑
        return max(8, int(500 / session.fps))

    def _after_decoder_started(self) -> None:
        if self._audio_active:
            self._audio.start()

    # ---------- 主时钟（墙钟） ----------

    def _reset_clock(self, t: float, playing: bool) -> None:
        self._t_base = t
        self._t_mark = time.monotonic() if playing else None

    def _master_clock(self) -> float:
        """主时钟（秒）：播放中走墙钟，暂停/待播时冻结。"""
        base, mark = self._t_base, self._t_mark
        if mark is not None:
            return base + (time.monotonic() - mark)
        return base

    def _on_play_state_changed(self) -> None:
        a = self._audio
        if self._playing:
            if self._session is not None and \
                    self._master_clock() >= self._session.duration_sec - 0.02:
                self._request_seek(0)  # 已在片尾按播放 → 从头再来
            else:
                self._t_mark = time.monotonic()
            if a is not None:
                a.resume()
        else:
            self._t_base = self._master_clock()
            self._t_mark = None
            if a is not None:
                a.pause()

    def _teardown_output(self) -> None:
        if self._audio is not None:
            self._audio.stop()
            self._audio = None

    def _on_volume_changed(self, value: int) -> None:
        # [BUG-P1-17] 音量条接线 QAudioSink（0-100 → 0.0-1.0）
        if self._audio is not None:
            self._audio.set_volume(value / 100.0)

    def _reset_stream_state(self) -> None:
        super()._reset_stream_state()
        self._t_mark = None

    # ---------- 帧消费：主时钟门控 ----------

    def _on_eof(self) -> None:
        # 播到片尾（解码线程内）：只发信号，UI/音频操作由 _on_eof_ui 执行
        self._eof_sig.emit()

    def _on_eof_ui(self) -> None:
        # 播到片尾：音频暂停（时钟继续走，重新播放时从头再来）
        a = self._audio
        if a is not None:
            a.pause()
        _icons.set_play_icon(self.btn_play, playing=False)

    def _decode_frame(self, session, target: int):
        size = self.canvas.display_size()
        rgb = session.frame_rgb(target, size=size)
        return _frame_to_qimage(rgb)

    def _on_decode_stop(self) -> None:
        import traceback
        traceback.print_exc()
        with self._cond:
            self._playing = False
        # [BUG-P1-19] 文件截断/时长高估（duration×fps 估 frame_count）触发
        # StopIteration 时此前只静默暂停——用户只见画面冻结不知原因
        self.toast_requested.emit('播放停止：文件可能被截断或元数据时长异常')
        # [U10] 与 _on_eof 路径对齐：此前画面冻结后音频继续播到自然 EOF、
        # 播放键图标停留"播放中"——复用 _eof_sig 走 _on_eof_ui 收尾
        # （音频 pause + 图标复位；主时钟冻结由 _playing=False 侧完成）
        self._eof_sig.emit()

    def _on_decode_error(self) -> None:
        import traceback
        traceback.print_exc()
        time.sleep(0.05)

    def _on_seek_requested(self, target: int) -> None:
        fps = self._session.fps if self._session else 25.0
        t = max(0.0, target / fps)
        self._t_base = t
        self._t_mark = time.monotonic() if self._playing else None
        a = self._audio
        if a is not None:
            a.seek(t)

    def _poll_ring(self) -> None:
        if self._session is None:
            return
        t = self._master_clock()
        fps = self._session.fps
        pick = None
        with self._ring_lock:
            while self._ring:
                self._pending.append(self._ring.popleft())
            # 时钟门控：只显示 pts ≤ 主时钟的最新一帧；过期序号丢弃；
            # 同一 tick 积压多帧时丢旧显新（解码偶发卡顿后的追赶）
            while self._pending:
                idx = self._pending[0][0]
                if idx <= self._shown_index:
                    self._pending.popleft()
                elif idx / fps <= t + 1e-6:
                    pick = self._pending.popleft()
                else:
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
        self.lbl_pos.setText(_fmt_time(min(t, self._session.duration_sec)))

    def _handle_key(self, key) -> bool:
        from PyQt6.QtCore import Qt as _Qt
        # [BUG-P1-16] 统一三引擎方向语义：Up→上一个 / Down→下一个
        # （此前本引擎与 topos/qt 相反）
        if key == _Qt.Key.Key_Up:
            self.request_prev.emit()
        elif key == _Qt.Key.Key_Down:
            self.request_next.emit()
        else:
            return False
        return True

    def _info_html(self) -> str:
        s = self._session
        a = self._audio
        if a is not None and a.audio_rate:
            audio_line = (f"音频: {a.audio_codec} {a.audio_rate}Hz "
                          f"{a.audio_layout} → 48kHz 立体声（QAudioSink）<br>")
        else:
            audio_line = "音频: 无<br>"
        return (f"<b>{os.path.basename(self._current_path)}</b><br>"
                f"编码: {s.codec_name}<br>"
                f"分辨率: {s.width}×{s.height}<br>"
                f"帧率: {s.fps:.2f} fps（{s.frame_count} 帧）<br>"
                f"像素格式: {s.pix_fmt}<br>"
                f"位深: {s.bit_depth}-bit<br>"
                f"时长: {s.duration_sec:.1f} s<br>"
                f"{audio_line}"
                f"解码: PyAV 多线程（音频同步）")
