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
from .frame_geometry import fit_aspect_size as _fit_aspect_size
from .base_player import BasePlayer, DecoderBackend, _frame_to_qimage

_MEZZANINE_CODECS = {'prores', 'dnxhd'}
_SLOW_UHD_CODECS = {'hevc', 'h265'}

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
        # [E3] 排空状态（音频线程写、GUI 线程读，_slock 保护）：
        # EOF 收尾要等"音源读完 + 待写清空 + sink 缓冲放完"三者同时成立
        self._source_eof = False
        self._pend_len = 0
        self._sink_backlog = 0

    # -- 线程安全接口（GUI 线程调用） --

    def start(self) -> None:
        from .audio_arbiter import shared_arbiter
        # [E6] 共享设备互斥：登记为本进程当前唯一音频拥有者；被接管时退役
        self._arbiter_owner = f'libav-audio:{id(self):x}'
        shared_arbiter().acquire(self._arbiter_owner, on_takeover=self.retire)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def retire(self) -> None:
        """[E6] 被音频仲裁者接管：置退役标志——线程 4ms 轮询自行收尾。"""
        self._running = False

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

    def drained(self) -> bool:
        """[E3] 音频排空：音源读完 + 待写清空 + sink 缓冲放完（三者同时）。"""
        with self._slock:
            return self._source_eof and self._pend_len == 0 \
                and self._sink_backlog == 0

    def stop(self) -> None:
        from .audio_arbiter import shared_arbiter
        shared_arbiter().release(getattr(self, '_arbiter_owner', ''))
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
            if self._io is None:
                raise RuntimeError('无法启动音频输出')
        except Exception:
            logger.warning('无法启动 Libav 音频输出', exc_info=True)
            with self._slock:
                self._source_eof = True
            if self._container is not None:
                self._container.close()
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
                    with self._slock:  # [E3] seek 后重新判定排空
                        self._source_eof = False
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
                    else:
                        with self._slock:  # [E3] 音源读尽（EOF/持续失败）
                            self._source_eof = True
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
                with self._slock:  # [E3] 排空观测：待写量 + sink 缓冲积压
                    self._pend_len = len(pend)
                    try:
                        self._sink_backlog = self._sink.bufferSize() - self._sink.bytesFree()
                    except Exception:
                        self._sink_backlog = 0
                time.sleep(0.004)
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            with self._slock:
                self._source_eof = True
                self._pend_len = self._sink_backlog = 0
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


_mezzanine_cache: dict[tuple[str, float | None], tuple[bool, str]] = {}
_MEZZANINE_CACHE_MAX = 512


def _probe_playback_stream(path: str) -> tuple[bool, str]:
    try:
        container = _av_open_with_timeout(path, header_only=True)
    except (OSError, ValueError, TimeoutError):
        return False, ''
    try:
        if not container.streams.video:
            return False, ''
        codec = container.streams.video[0].codec_context
        if codec is None:
            return False, ''
        name = (codec.name or '').lower()
        decodable, reason = raw_capability(codec.codec_tag, name)
        stream = container.streams.video[0]
        uhd = (codec.width or 0) >= 3840
        constant_rate_h264 = (name == 'h264' and stream.average_rate is not None
                              and stream.average_rate == stream.base_rate)
        return (not decodable or name in _MEZZANINE_CODECS or
                (uhd and (name in _SLOW_UHD_CODECS or constant_rate_h264))), reason
    finally:
        container.close()


def _playback_stream_info(path: str) -> tuple[bool, str]:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    key = (path, mtime)
    hit = _mezzanine_cache.get(key)
    if hit is not None:
        return hit
    result = _probe_playback_stream(path)
    if len(_mezzanine_cache) >= _MEZZANINE_CACHE_MAX:
        _mezzanine_cache.clear()
    _mezzanine_cache[key] = result
    return result


def is_mezzanine(path: str) -> bool:
    """ProRes、DNxHR、4K H.264/HEVC 和需提示缺少解码器的 RAW 路由到 Libav。"""
    return _playback_stream_info(path)[0]


# [E7] RAW 家族按四字符码（codec tag）识别——FFmpeg 按 tag 判定解码器
# 兼容性（伪造 tag 的封装直接拒绝写头），与本判定同源。
_PRORES_RAW_TAGS = {'aprh', 'aprn', 'apro'}
_UNDECODABLE_TAGS = {'braw', 'r3d', 'r3d2'}
_UNDECODABLE_CODECS = {'prores_raw', 'braw', 'r3d'}


class UndecodableRawError(ValueError):
    """[E7] 后端无解码器的 RAW 素材——按能力拒绝，不冒充可播。"""


def raw_capability(codec_tag: str, codec_name: str) -> tuple[bool, str]:
    """[E7] 按真实后端报告 RAW 可解码性 → ``(可否, 原因)``。

    ProRes RAW（aprh 家族）/BRAW/R3D 在当前 PyAV/FFmpeg 构建没有解码
    器：metadata 能开（~0.5s），尝试解码只会长时间挂起或黑屏（FUP-07
    实测 8K ProRes RAW 首帧 35s+ 未返回）。常规 ProRes（apco/apcs/apcn/
    apch/ap4x）可正常软解——不把"常规 ProRes 通过"推广为"ProRes RAW
    可播"。SDK 接入后从这里的白名单放行。
    """
    tag = (codec_tag or '').strip().lower()
    name = (codec_name or '').strip().lower()
    if tag in _PRORES_RAW_TAGS:
        return False, (f'ProRes RAW（{codec_tag.strip()}）无解码后端：'
                       'SDK 未接入，不支持预览')
    if tag in _UNDECODABLE_TAGS or name in _UNDECODABLE_CODECS:
        label = 'ProRes RAW' if name == 'prores_raw' else (tag or name).upper()
        return False, (f'{label} 无解码后端：'
                       'SDK 未接入，不支持预览')
    return True, ''


def _av_open_with_timeout(path: str, timeout: float = 5.0, *, header_only: bool = False):
    """[E7] 有界 ``av.open``：异常源挂起时按超时失败收尾，不无限等待。

    打开线程 daemon 化（有界泄漏纪律）：杀不死的卡死打开留给 OS 回收，
    绝不阻塞 GUI 等它。
    """
    box: dict = {}

    def _open() -> None:
        try:
            if header_only:
                box['ctx'] = av.open(path, options={
                    'probesize': '32768', 'analyzeduration': '1',
                }, timeout=timeout)
            else:
                box['ctx'] = av.open(path)
        except BaseException as exc:  # noqa: BLE001 - 原样转递给调用方
            box['error'] = exc

    thread = threading.Thread(target=_open, daemon=True, name='av-open')
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(
            f'打开超时（>{timeout:.0f}s）：文件可能损坏或在慢速存储上')
    if 'error' in box:
        raise box['error']
    return box['ctx']


class _LibavSession(DecoderBackend):
    """PyAV 会话：顺序解码 + 断点自动 seek，接口对齐 ToposMovieSession。"""

    def __init__(self, path: str):
        self._path = path
        # [E7] 有界打开：av.open 对异常源（网络盘/半损坏头）可能挂起，
        # 不能让 GUI 无限等待。超时按失败收尾；卡住的打开线程泄漏交给
        # OS（daemon，有界泄漏纪律——与 stop_decoder 同款取舍）
        self._container = _av_open_with_timeout(path)
        try:
            # [BUG-P0-06] 纯音频/损坏文件没有视频流：IndexError 上抛前
            # 必须 close container，否则每次误开都泄漏 native demuxer + 句柄
            self._stream = self._container.streams.video[0]
            self._stream.thread_type = 'FRAME'
            self._stream.thread_count = 0
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
            if self.codec_name == 'dnxhd' and vs.width >= 3840:
                # 4K DNxHR 的帧体积大，限制同时解码的帧降低内存带宽竞争。
                self._stream.thread_count = 4
            self.audio_info = None
            if self._container.streams.audio:
                audio = self._container.streams.audio[0]
                if audio.codec_context is not None:
                    self.audio_info = (audio.codec_context.name or '?', audio.rate,
                                       audio.layout.name if audio.layout is not None else '')
            # [E7] RAW 能力按真实后端报告：ProRes RAW/BRAW/R3D 在当前
            # PyAV/FFmpeg 构建无解码器——metadata 开得了（~0.5s）但首帧
            # 35s+ 挂起（FUP-07 实测 8K ProRes RAW）。打开时按 codec tag
            # 拒绝：快、明确、不冒充可播，绝不用黑屏/无限等待兜底。
            decodable, reason = raw_capability(
                str(getattr(vs, 'codec_tag', '') or ''), self.codec_name)
            if not decodable:
                self.close()
                raise UndecodableRawError(reason)
            self.width, self.height = vs.width, vs.height
            self.pix_fmt = str(vs.pix_fmt)
            pixel_format = vs.format
            self.has_alpha = bool(pixel_format and any(
                component.is_alpha for component in pixel_format.components))
            self.bit_depth = max(
                (component.bits for component in pixel_format.components),
                default=8) if pixel_format else (getattr(vs, 'bits_per_raw_sample', 0) or 8)
            # [FR-20] VideoFrame 不暴露 SAR；等比解码尺寸需要源 SAR，
            # 打开时从 CodecContext 记录一次（Fraction 或 None）
            self.sar = getattr(vs, 'sample_aspect_ratio', None)
            # [E4] 显示旋转（MOV tkhd → FFmpeg DISPLAYMATRIX）：只挂在解码
            # 帧的 side data 上（包上没有），打开时解一帧探取；探测失败/
            # 非法值按未旋转处理——绝不静默转 90° 凑比例
            self.rotation_quarters = self._probe_rotation()
        except Exception:
            self.close()
            raise
        self._iter = None
        self._next_idx = 0  # 迭代器当前指向的帧号
        self._lock = threading.Lock()

    # -- 索引/时间换算 --

    def _probe_rotation(self, timeout: float = 3.0) -> int:
        """[E4] 解码首帧读 DISPLAYMATRIX → 90° 象限（顺时针显示旋转）。

        [E7] 有界探测：首帧解码对慢源可能长时间不返回（8K RAW 实测
        35s+）——超时按未旋转收尾并告警（旋转是尽力而为的元数据，不是
        播放前置条件）。探测线程 daemon（有界泄漏纪律）。
        """
        box: dict = {}

        def _probe() -> None:
            try:
                box['quarters'] = self._probe_rotation_unlocked()
            except BaseException as exc:  # noqa: BLE001 - 探测失败按未旋转
                box['error'] = exc

        thread = threading.Thread(target=_probe, daemon=True,
                                  name='rotation-probe')
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            logger.warning('rotation probe timed out (>%.0fs); '
                           'treating as unrotated', timeout)
            return 0
        if 'error' in box:
            logger.debug('rotation probe failed; treating as unrotated',
                         exc_info=box['error'])
        return int(box.get('quarters', 0))

    def _probe_rotation_unlocked(self) -> int:
        from .frame_geometry import display_rotation_quarters
        for probe in self._container.decode(video=0):
            for data in probe.side_data:
                if 'DISPLAYMATRIX' in str(getattr(data, 'type', '')):
                    return display_rotation_quarters(
                        np.frombuffer(bytes(data), dtype=np.int32))
            break
        return 0

    def _pts_of(self, index: int) -> int:
        tb = float(self._stream.time_base)
        offset = self._stream.start_time or 0
        return offset + int(max(0.0, index / self.fps - 0.08 / max(1.0, self.fps)) / tb)

    def _idx_of(self, frame) -> int:
        t = frame.time
        if t is None:
            # [BUG-P3-60] seek 后 _next_idx 可能是 None：此前原样返回，
            # 后续 idx >= index 比较即 TypeError，陷入 0.05s 退避死循环
            return max(0, self._next_idx or 0)
        offset = (self._stream.start_time or 0) * self._stream.time_base
        return max(0, int(round((t - float(offset)) * self.fps)))

    # -- 随机访问解码（顺序命中时不 seek，享受多线程吞吐） --

    def frame_rgb(self, index: int, size=None):
        from .frame_geometry import rotate_rgb_quarters
        with self._lock:
            frame = self._decode_frame_locked(index, size)
            if size:
                # [FR-20] _decode_frame_locked 已等比重采样到框内尺寸，
                # 这里不再二次拉伸到整个框
                rgb = np.ascontiguousarray(frame.to_ndarray(format='rgb24'))
            else:
                rgb = frame.to_ndarray(format='rgb24')
        # [E4] 显示旋转在 RGB 路径落地（YUV/GPU 路径不支持旋转，播放器
        # 侧已按 rotation_quarters 回退 RGB）
        return rotate_rgb_quarters(rgb, self.rotation_quarters)

    def frame_rgba(self, index: int, size: tuple[int, int] | None = None) -> np.ndarray:
        from .frame_geometry import rotate_rgb_quarters

        with self._lock:
            frame = self._decode_frame_locked(index, size, pixel_format='rgba')
            rgba = np.ascontiguousarray(frame.to_ndarray())
        return rotate_rgb_quarters(rgba, self.rotation_quarters)

    def frame_yuv(self, index: int, size=None):
        """解码第 index 帧 → ``YuvFrame``（GPU 预览路径）。

        PyAV 保留源平面位深：8-bit 用 uint8，10/12/16-bit 用 uint16
        （码值不左移）。这里按 ``size`` 让 swscale 只做缩放，不做
        YUV→RGB，随后把三平面复制成本帧独立数组供 GUI 线程上传。
        """
        from .yuv_frame import YuvFrame

        with self._lock:
            frame = self._decode_frame_locked(index, size)
            fmt = str(getattr(frame.format, 'name', '') or '')
            depth = 16 if 'p16' in fmt else 12 if 'p12' in fmt else \
                10 if 'p10' in fmt else 8
            dtype = np.uint16 if depth > 8 else np.uint8
            planes = [
                _plane_to_ndarray(plane, dtype)
                for plane in frame.planes[:3]
            ]
            if len(planes) != 3:
                raise ValueError(f'unsupported non-planar format: {fmt}')
            subsampling = '444' if ('444' in fmt or 'gbr' in fmt) else \
                '422' if '422' in fmt else '420'
            matrix = int(getattr(frame, 'colorspace', 1) or 1)
            # PyAV color_range: 0=unspecified, 1=MPEG(limited), 2=JPEG(full)
            full_range = 1 if int(getattr(frame, 'color_range', 0) or 0) == 2 else 0
            siting = int(getattr(frame, 'chroma_location', 1) or 1)
            if subsampling == '444':
                siting = 1
            return YuvFrame(
                y=planes[0], u=planes[1], v=planes[2],
                bit_depth=depth, subsampling=subsampling,
                matrix=matrix, color_range=full_range, chroma_siting=siting,
                pixel_format='gbr' if 'gbr' in fmt else subsampling,
                source_width=int(frame.width), source_height=int(frame.height))

    def _decode_frame_locked(self, index: int, size: tuple[int, int] | None = None,
                             *, pixel_format: str | None = None) -> av.VideoFrame:
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
            # [FR-20] 等比限制解码尺寸：把源（含 SAR）按显示宽高比装进
            # 目标框，绝不拉伸——160×80 的 2:1 源在 400×400 框里解码成
            # 400×200（letterbox 由呈现层处理）。PyAV VideoFrame 不暴露
            # SAR（只有 CodecContext 有），用会话打开时记录的源 SAR；
            # 解码结果即方形像素，画布用解码尺寸即可恢复几何。
            w, h = _fit_aspect_size(frame, int(size[0]), int(size[1]),
                                    getattr(self, 'sar', None),
                                    rotation_quarters=getattr(
                                        self, 'rotation_quarters', 0))
            return frame.reformat(width=w, height=h, format=pixel_format or frame.format.name)
        return frame.reformat(format=pixel_format) if pixel_format else frame

    def close(self):
        try:
            self._container.close()
        except Exception:
            pass


def _plane_to_ndarray(plane, dtype) -> np.ndarray:
    """PyAV VideoPlane → 紧凑二维数组（按 line_size 裁掉行尾对齐填充）。"""
    itemsize = np.dtype(dtype).itemsize
    row_values = int(plane.line_size) // itemsize
    raw = np.frombuffer(plane, dtype=dtype)
    height = int(plane.height)
    width = int(plane.width)
    if row_values != width:
        raw = raw.reshape(height, row_values)[:, :width]
    else:
        raw = raw.reshape(height, width)
    return raw.copy(order='C')


class LibavPlayer(BasePlayer):
    """ProRes/DNxHR 等 mezzanine 的多线程解码播放器（音频同步，UI 对齐 ToposPlayer）。"""

    JOIN_TIMEOUT = 2.0
    PLAYER_TITLE = 'Libav Player'
    BADGE_TEXT = 'MEZZANINE'
    HAS_AUDIO = True
    # [E3] _eof_sig 更名 _decode_stopped_sig：只承载"解码提前停止（文件截
    # 断/时长高估）"的 GUI 收尾；自然播完改由呈现完成（_on_playback_finished）
    # 发布，两者不再共用一个信号（FUP-07：StopIteration 与 EOF 混淆）
    _decode_stopped_sig = pyqtSignal()

    def __init__(self, parent=None):
        self._audio: _AudioPipeline | None = None
        self._audio_active = False
        self._t_base = 0.0        # 主时钟：最近一次起步的时间轴位置（秒）
        self._t_mark: float | None = None  # 播放中的墙钟锚点；暂停时为 None
        super().__init__(parent)
        self._decode_stopped_sig.connect(self._on_decode_stopped_ui)

    # ---------- 会话 / 输出接线 ----------

    def _open_session(self, path: str) -> DecoderBackend:
        extension = os.path.splitext(path)[1].lower().lstrip('.')
        decodable, reason = raw_capability(extension, '')
        if decodable:
            _, reason = _playback_stream_info(path)
        if reason:
            raise UndecodableRawError(reason)
        return _LibavSession(path)

    def _open_failed_prefix(self) -> str:
        return '打开失败'

    def _configure_session(self, session) -> None:
        # 二期：音频管线（有音轨才启动，无音轨自动退化为无声墙钟）
        self._audio = _AudioPipeline(self._current_path, self._master_clock)
        self._audio_active = session.audio_info is not None
        self._audio.pause()
        # [U10] 新管线继承滑杆音量：管线启动时 _volume=None → sink 默认 1.0，
        # 上一文件调到 20% 后换文件会以 100% 播放直到用户再动滑杆
        try:
            self._audio.set_volume(self.slider_vol.value() / 100.0)
        except Exception:
            pass
        self._reset_clock(0.0, playing=False)
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
        audio = self._audio
        if self._playing:
            if self._eof_published:
                self._request_seek(0)
            elif self._frame_to_present is None:
                self._t_mark = time.monotonic()
                if audio is not None:
                    audio.resume()
        else:
            self._reset_clock(self._master_clock(), playing=False)
            if audio is not None:
                audio.pause()

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

    def _audio_drained(self) -> bool:
        """[E3] 墙钟主时钟引擎：无声恒排空；有声按管线（音源读完+缓冲放完）。"""
        if not self._audio_active or self._audio is None:
            return True
        return self._audio.drained()

    def _on_playback_finished(self) -> None:
        self._reset_clock(self._master_clock(), playing=False)
        a = self._audio
        if a is not None:
            a.pause()
        _icons.set_play_icon(self.btn_play, playing=False)
        self._wake_ui()

    def _on_decode_stopped_ui(self) -> None:
        # 解码提前停止（截断/时长高估）的 GUI 收尾：与自然播完区分（E3）
        _icons.set_play_icon(self.btn_play, playing=False)

    def _create_canvas(self):
        from ._canvas_factory import make_canvas
        return make_canvas(self)

    def _decode_frame(self, session, target: int):
        canvas = self.canvas
        size = canvas.display_size()
        if getattr(session, 'has_alpha', False):
            return _frame_to_qimage(session.frame_rgba(target, size=size))
        # [E4] YUV/GPU 上传路径不支持旋转：有显示旋转时直接走 RGB 路径
        # （frame_rgb 内完成旋转）
        if getattr(canvas, 'gpu_active', False) and \
                hasattr(session, 'frame_yuv') and \
                getattr(session, 'rotation_quarters', 0) == 0:
            try:
                return session.frame_yuv(target, size=size)
            except Exception:
                logger.warning('Libav GPU YUV 帧失败，回退 RGB', exc_info=True)
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
        # [E3] 解码提前停止 ≠ 自然播完：不再复用 EOF 路径——只做图标复位；
        # 播放完成（_on_playback_finished）只属于实际呈现完成的自然收尾
        self._decode_stopped_sig.emit()

    def _on_decode_error(self) -> None:
        import traceback
        traceback.print_exc()
        time.sleep(0.05)

    def _on_seek_requested(self, target: int) -> None:
        fps = self._session.fps if self._session else 25.0
        t = max(0.0, target / fps)
        self._reset_clock(t, playing=False)
        if self._audio is not None:
            self._audio.pause()
            self._audio.seek(t)

    def _poll_ring(self) -> None:
        if self._session is None:
            return
        target = int(self._master_clock() * self._session.fps + 1e-6)
        frame = self._take_due_frame(target)
        if frame is not None:
            self._show_frame(frame)
            if self._playing and self._t_mark is None:
                self._t_mark = time.monotonic()
                if self._audio is not None:
                    self._audio.resume()
        self._maybe_publish_eof()

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
        if s.audio_info is not None:
            codec, rate, layout = s.audio_info
            audio_line = f"音频: {codec} {rate}Hz {layout} → 48kHz 立体声（QAudioSink）<br>"
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
