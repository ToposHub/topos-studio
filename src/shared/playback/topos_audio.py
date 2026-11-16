"""Topos 音轨读取与播放输出。

Topos MOV 的音轨有两种形态：

- ``lpcm``：无损 PCM（16/24/32-bit 整数或 float32，容器内为大端）；
- ``mp4a``：AAC 包流，Python 侧无法直接从裸包解码。

LPCM 走 native 分块读取（独立 movie 句柄，避开视频解码线程）；
AAC 回退到外部 FFmpeg 解码为 48 kHz / 立体声 / s16le。两种来源最终都
输出统一的 48 kHz 立体声 s16 数据，交给 ``QAudioSink``。

播放位置以 ``QAudioSink.processedUSecs()`` 为主时钟；暂停、seek、
音量都由音频线程统一处理。UI 只读取线程维护的 ``position`` 快照。
"""
from __future__ import annotations

import bisect
import logging
import shutil
import subprocess
import threading
import time
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices

from .base_player import utils_get_tool_path

logger = logging.getLogger(__name__)

OUT_RATE = 48000
OUT_CHANNELS = 2
OUT_BYTES_PER_FRAME = OUT_CHANNELS * 2
OUT_BYTES_PER_SEC = OUT_RATE * OUT_BYTES_PER_FRAME
_AUDIO_LEAD = 0.25
_AUDIO_AHEAD_CAP = 0.75
_LPCM_CODEC = 1
_AAC_CODEC = 2


def _make_sink(device, fmt):
    return QAudioSink(device, fmt)


def _ffmpeg_path() -> Optional[str]:
    found = utils_get_tool_path('ffmpeg')
    if found:
        return found
    return shutil.which('ffmpeg')


class _AudioSource:
    """统一输出 48 kHz / 立体声 / int16 的音频来源。"""

    duration: float = 0.0

    def seek(self, seconds: float) -> None:
        raise NotImplementedError

    def read(self, frames: int) -> np.ndarray:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _LpcmSource(_AudioSource):
    """LPCM 音轨：按 native chunk 读取，再转成统一输出格式。"""

    def __init__(self, session) -> None:
        self._session = session
        self._info = session.audio_info
        self._rate = int(self._info.sample_rate or 0)
        self._channels = int(self._info.channel_count or 0)
        self._bits = int(self._info.bits_per_sample or 16)
        self._is_float = int(getattr(self._info, 'sample_format', 0)) == 1
        self._bytes_per_sample = 4 if self._is_float else max(1, self._bits // 8)
        self._bytes_per_frame = self._bytes_per_sample * self._channels
        self._chunk_count = int(self._info.chunk_count or 0)
        self._sample_count = int(self._info.sample_count or 0)
        self.duration = (self._sample_count / self._rate
                         if self._rate > 0 else 0.0)
        self._prefix: list[int] = [0]
        self._prefix_lock = threading.Lock()
        self._next_chunk = 0
        self._skip_frames = 0
        self._out = np.empty((0, OUT_CHANNELS), dtype='<i2')

    def _ensure_prefix(self) -> None:
        if len(self._prefix) > 1 or self._chunk_count <= 0:
            return
        with self._prefix_lock:
            if len(self._prefix) > 1:
                return
            for index in range(self._chunk_count):
                try:
                    size = int(self._session.audio_chunk_size(index))
                except Exception:
                    logger.exception('Topos 音频 chunk %s 探测失败', index)
                    break
                frames = size // max(1, self._bytes_per_frame)
                self._prefix.append(self._prefix[-1] + frames)

    def seek(self, seconds: float) -> None:
        self._ensure_prefix()
        target = int(max(0.0, float(seconds)) * self._rate)
        target = min(target, max(0, self._prefix[-1]))
        index = max(0, bisect.bisect_right(self._prefix, target) - 1)
        if index >= self._chunk_count:
            index = max(0, self._chunk_count - 1)
        self._next_chunk = index
        self._skip_frames = target - self._prefix[index]
        self._out = np.empty((0, OUT_CHANNELS), dtype='<i2')

    def _raw_to_float(self, raw: bytes) -> np.ndarray:
        """大端 PCM → (frames, channels) float32 [-1, 1]。"""
        usable = len(raw) - (len(raw) % max(1, self._bytes_per_frame))
        raw = raw[:usable]
        if not raw:
            return np.empty((0, self._channels), dtype=np.float32)
        if self._is_float:
            values = np.frombuffer(raw, dtype='>f4').astype(np.float32)
        elif self._bits == 16:
            values = np.frombuffer(raw, dtype='>i2').astype(np.float32) / 32768.0
        elif self._bits == 24:
            packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            values = ((packed[:, 0].astype(np.int32) << 16)
                      | (packed[:, 1].astype(np.int32) << 8)
                      | packed[:, 2].astype(np.int32))
            values = np.where(values >= (1 << 23),
                              values - (1 << 24), values).astype(np.float32)
            values /= 8388608.0
        elif self._bits == 32:
            values = np.frombuffer(raw, dtype='>i4').astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f'不支持的 LPCM 位深: {self._bits}')
        return values.reshape(-1, self._channels)

    def _to_output(self, frames: np.ndarray) -> np.ndarray:
        """声道折叠 + 重采样到 48 kHz 立体声 int16。"""
        if frames.size == 0:
            return np.empty((0, OUT_CHANNELS), dtype='<i2')
        if frames.shape[1] == 1:
            stereo = np.repeat(frames, 2, axis=1)
        elif frames.shape[1] == 2:
            stereo = frames
        elif frames.shape[1] >= 6:
            # 5.1/7.1 的常见 L/R 折叠；环绕声只做预览级近似。
            stereo = np.stack([
                frames[:, 0] + 0.707 * frames[:, 2] + 0.707 * frames[:, 4],
                frames[:, 1] + 0.707 * frames[:, 2] + 0.707 * frames[:, 5],
            ], axis=1)
        else:
            stereo = frames[:, :2]
            if stereo.shape[1] == 1:
                stereo = np.repeat(stereo, 2, axis=1)
        if self._rate != OUT_RATE and len(stereo):
            count = max(1, int(round(len(stereo) * OUT_RATE / self._rate)))
            src_x = np.arange(len(stereo), dtype=np.float64)
            dst_x = np.arange(count, dtype=np.float64) * self._rate / OUT_RATE
            stereo = np.stack([
                np.interp(dst_x, src_x, stereo[:, 0]),
                np.interp(dst_x, src_x, stereo[:, 1]),
            ], axis=1)
        return np.clip(np.clip(stereo, -1.0, 1.0) * 32768.0,
                        -32768, 32767).astype('<i2')

    def _read_next_chunk(self) -> np.ndarray:
        while self._next_chunk < self._chunk_count:
            index = self._next_chunk
            self._next_chunk += 1
            try:
                raw = self._session.read_audio_chunk(index)
            except Exception:
                logger.exception('Topos 音频 chunk %s 读取失败', index)
                return np.empty((0, OUT_CHANNELS), dtype='<i2')
            frames = self._raw_to_float(raw)
            if self._skip_frames:
                skip = min(self._skip_frames, len(frames))
                frames = frames[skip:]
                self._skip_frames -= skip
            if len(frames):
                return self._to_output(frames)
        return np.empty((0, OUT_CHANNELS), dtype='<i2')

    def read(self, frames: int) -> np.ndarray:
        if frames <= 0:
            return np.empty((0, OUT_CHANNELS), dtype='<i2')
        while len(self._out) < frames:
            chunk = self._read_next_chunk()
            if len(chunk) == 0:
                break
            self._out = np.concatenate((self._out, chunk), axis=0)
        out = self._out[:frames]
        self._out = self._out[frames:]
        return out

    def close(self) -> None:
        self._session = None
        self._out = np.empty((0, OUT_CHANNELS), dtype='<i2')


class _AacSource(_AudioSource):
    """AAC 回退：由 FFmpeg 解出 48 kHz 立体声 s16，按需读取。"""

    def __init__(self, path: str, info) -> None:
        self._path = path
        self.duration = (float(info.sample_count) / float(info.sample_rate)
                         if info.sample_rate else 0.0)
        self._proc: Optional[subprocess.Popen] = None
        self._buffer = bytearray()
        self._eof = False
        self._ffmpeg = _ffmpeg_path()

    @property
    def available(self) -> bool:
        return bool(self._ffmpeg)

    def _spawn(self, seconds: float) -> None:
        self.close()
        if not self._ffmpeg:
            self._eof = True
            return
        cmd = [self._ffmpeg, '-v', 'error']
        if seconds > 0:
            cmd += ['-ss', f'{seconds:.3f}']
        cmd += ['-i', self._path, '-map', '0:a:0', '-vn',
                '-f', 's16le', '-acodec', 'pcm_s16le',
                '-ar', str(OUT_RATE), '-ac', str(OUT_CHANNELS), '-']
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0)
        except Exception:
            logger.exception('Topos AAC 音频解码启动失败')
            self._proc = None
        self._buffer.clear()
        self._eof = self._proc is None

    def seek(self, seconds: float) -> None:
        self._spawn(max(0.0, float(seconds)))

    def read(self, frames: int) -> np.ndarray:
        need = frames * OUT_BYTES_PER_FRAME
        while len(self._buffer) < need and self._proc is not None:
            chunk = self._proc.stdout.read(need - len(self._buffer))
            if not chunk:
                self._eof = True
                break
            self._buffer.extend(chunk)
        if len(self._buffer) < OUT_BYTES_PER_FRAME:
            return np.empty((0, OUT_CHANNELS), dtype='<i2')
        usable = len(self._buffer) - (len(self._buffer) % OUT_BYTES_PER_FRAME)
        take = min(usable, need)
        data = np.frombuffer(bytes(self._buffer[:take]), dtype='<i2')
        del self._buffer[:take]
        return data.reshape(-1, OUT_CHANNELS)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def make_audio_source(session, path: str) -> Optional[_AudioSource]:
    """按音轨编码选择来源；无音轨/不支持的编码返回 None。"""
    info = getattr(session, 'audio_info', None)
    if info is None or not getattr(info, 'has_audio', False):
        return None
    if int(info.codec) == _LPCM_CODEC:
        return _LpcmSource(session)
    if int(info.codec) == _AAC_CODEC:
        source = _AacSource(path, info)
        if not source.available:
            source.close()
            return None
        return source
    return None


class ToposAudioPipeline(QObject):
    """``QAudioSink`` 输出线程；``position`` 是 UI 可读的主时钟快照。"""

    failed = pyqtSignal(str)

    def __init__(self, source: _AudioSource, parent=None) -> None:
        super().__init__(parent)
        self._source = source
        self._lock = threading.Condition()
        self._running = True
        self._paused = False
        self._seek_to: Optional[float] = None
        self._volume = 1.0
        self._position = 0.0
        self._thread = threading.Thread(
            target=self._run, name='topos-audio', daemon=True)
        self._thread.start()

    @property
    def duration(self) -> float:
        return self._source.duration

    def position(self) -> Optional[float]:
        with self._lock:
            return self._position

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._lock.notify()

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            self._lock.notify()

    def seek(self, seconds: float) -> None:
        with self._lock:
            self._seek_to = max(0.0, float(seconds))
            self._lock.notify()

    def set_volume(self, value: float) -> None:
        with self._lock:
            self._volume = min(1.0, max(0.0, float(value)))

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._lock.notify()
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            logger.warning('Topos 音频线程未在 2s 内退出（延迟回收）')
        self._source.close()

    def _run(self) -> None:
        sink = None
        io = None
        fmt = QAudioFormat()
        fmt.setSampleRate(OUT_RATE)
        fmt.setChannelCount(OUT_CHANNELS)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            self.failed.emit('没有可用的音频输出设备')
            return
        try:
            sink = _make_sink(device, fmt)
            sink.setBufferSize(int(0.5 * OUT_BYTES_PER_SEC))
            sink.setVolume(self._volume)
            io = sink.start()
            if io is None:
                self.failed.emit('音频输出启动失败')
                return
            self._source.seek(0.0)
            base = 0.0
            decoded_end = 0.0
            pending = bytearray()
            pending_t = 0.0
            suspended = False
            # 预先填半秒缓冲，避免起播瞬间欠载。
            while len(pending) < int(_AUDIO_AHEAD_CAP * OUT_BYTES_PER_SEC):
                data = self._source.read(4096)
                if len(data) == 0:
                    break
                pending.extend(data.tobytes())
                decoded_end += len(data) / OUT_RATE
            while True:
                with self._lock:
                    if not self._running:
                        break
                    seek_target = self._seek_to
                    self._seek_to = None
                    paused = self._paused
                    volume = self._volume
                if seek_target is not None:
                    try:
                        sink.stop()
                    except Exception:
                        pass
                    self._source.seek(seek_target)
                    base = seek_target
                    self._position = seek_target
                    decoded_end = seek_target
                    pending.clear()
                    pending_t = seek_target
                    sink.setVolume(volume)
                    sink.setBufferSize(int(0.5 * OUT_BYTES_PER_SEC))
                    io = sink.start()
                    if io is None:
                        self.failed.emit('音频 seek 后输出启动失败')
                        break
                    if paused:
                        sink.suspend()
                        suspended = True
                    else:
                        suspended = False
                sink.setVolume(volume)
                if paused:
                    if not suspended:
                        sink.suspend()
                        suspended = True
                    time.sleep(0.02)
                    continue
                if suspended:
                    sink.resume()
                    suspended = False
                clock = base + sink.processedUSecs() / 1_000_000.0
                with self._lock:
                    self._position = min(max(clock, base),
                                        max(base, self._source.duration))
                # 解码前置（限幅防积压）
                if len(pending) < int(_AUDIO_AHEAD_CAP * OUT_BYTES_PER_SEC):
                    data = self._source.read(4096)
                    if len(data):
                        if not pending:
                            pending_t = decoded_end
                        pending.extend(data.tobytes())
                        decoded_end += len(data) / OUT_RATE
                    elif decoded_end <= clock:
                        pass
                # 欠载丢音追时钟，避免越播越落后
                if (sink.state() == QAudio.State.IdleState and pending
                        and pending_t < clock - 0.02):
                    drop = min(len(pending),
                               int((clock - pending_t) * OUT_BYTES_PER_SEC))
                    del pending[:drop]
                    pending_t = clock
                while pending and self._running:
                    with self._lock:
                        if self._seek_to is not None or self._paused:
                            break
                    if pending_t > clock + _AUDIO_LEAD:
                        break
                    free = sink.bytesFree()
                    if free <= 0:
                        break
                    take = min(free, len(pending))
                    written = io.write(bytes(pending[:take]))
                    if written <= 0:
                        break
                    pending_t += written / OUT_BYTES_PER_SEC
                    del pending[:written]
                time.sleep(0.004)
        except Exception:
            logger.exception('Topos 音频线程异常')
            self.failed.emit('Topos 音频线程异常，已关闭音频')
        finally:
            try:
                if sink is not None:
                    sink.stop()
            except Exception:
                pass
            self._source.close()


__all__ = ['ToposAudioPipeline', 'make_audio_source', 'OUT_RATE',
           'OUT_CHANNELS']
