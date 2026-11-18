"""Isolated audition playback for the Mixer Clip Editor (P0-2 §2.3).

One session owns the Qt audio device for clip auditioning.  It consumes the
same planar float32 PCM the editor visualizes (see
``src.features.edit.core.clip_pcm``), so the waveform, the spectrogram and
what you hear share one coordinate system.

Behaviour contract (roadmap §2.3):

- Play/stop/seek/loop/skip-selection all target this one session; loop uses
  the half-open window ``[start, end)`` (whole clip when no selection).
- Position is derived from the device clock (``processedUSecs``), not from
  wall time, so playhead feedback tracks what the listener hears.
- Stop returns to the position auditioning started from; every lifecycle
  exit (stop, device loss, project switch) releases the sink.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

logger = logging.getLogger(__name__)


class ClipAuditionSession(QObject):
    """QAudioSink push-mode playback over an in-memory PCM slice."""

    position_changed = pyqtSignal(int)      # 片段局部样本位置
    playing_changed = pyqtSignal(bool)
    finished = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pcm: Optional[np.ndarray] = None
        self._sample_rate = 48000
        self._channels = 1
        self._sink = None
        self._io = None
        self._pending = memoryview(b"")
        self._playing = False
        self._loop = False
        self._loop_start = 0
        self._loop_end = 0
        self._origin_sample = 0   # 本轮 sink 启动时的片段局部样本
        self._return_sample = 0   # 停止后返回的位置（开始试听的游标）
        # P1 · 监听态（L/R Mute/Solo）：只作用于送入设备 buffer 的字节，
        # 不改 PCM 源、不入工程——导出/成片零副作用。
        self._monitor_muted: frozenset = frozenset()
        self._monitor_solo: frozenset = frozenset()
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def has_audio(self) -> bool:
        return self._pcm is not None and self._pcm.shape[1] > 0

    def position(self) -> int:
        """当前播放头（片段局部样本）；由设备消费时钟推导。"""
        if self._pcm is None:
            return 0
        if self._sink is None or not self._playing:
            return self._return_sample
        usecs = 0
        try:
            usecs = max(0, int(self._sink.processedUSecs()))
        except Exception:  # noqa: BLE001
            usecs = 0
        return int(min(self._pcm.shape[1],
                       self._origin_sample + usecs * self._sample_rate // 1_000_000))

    def return_sample(self) -> int:
        return self._return_sample

    # ------------------------------------------------------------------
    # 装载与播放
    # ------------------------------------------------------------------
    def load(self, pcm: np.ndarray, sample_rate: int) -> None:
        """装载 PCM（平面 float32 [C, N]）；装载即停止在途播放。"""
        self.stop()
        self._pcm = pcm
        self._sample_rate = int(sample_rate) if sample_rate > 0 else 48000
        self._channels = max(1, pcm.shape[0])
        self._return_sample = 0

    def clear(self) -> None:
        self.stop()
        self._pcm = None
        self._return_sample = 0

    def set_loop(self, enabled: bool, start: int = -1, end: int = -1) -> None:
        """循环窗口为半开 [start, end)；无选区时用整段。"""
        previous = (self._loop, self._loop_start, self._loop_end)
        self._loop = bool(enabled)
        if self._pcm is not None:
            total = self._pcm.shape[1]
            self._loop_start = max(0, start if start >= 0 else 0)
            self._loop_end = min(total, end if end > 0 else total)
            if self._loop_end <= self._loop_start:
                self._loop = False
        if self._playing and previous != (self._loop, self._loop_start, self._loop_end):
            # 更新窗口立即生效：重建后续缓冲
            self._play_from(self.position())

    def play(self, start_sample: Optional[int] = None) -> bool:
        """从指定样本开始播放；无设备/无 PCM 返回 False。"""
        if self._pcm is None or self._pcm.shape[1] == 0:
            return False
        if start_sample is None:
            start_sample = self._return_sample
        start_sample = max(0, int(start_sample))
        if self._loop and not self._loop_start <= start_sample < self._loop_end:
            start_sample = self._loop_start
        elif start_sample >= self._pcm.shape[1]:
            start_sample = 0
        self._return_sample = start_sample
        return self._play_from(start_sample)

    def seek(self, sample: int) -> None:
        """播放中跳转；停止状态下只移动返回点。"""
        if self._pcm is None:
            return
        sample = max(0, min(self._pcm.shape[1], int(sample)))
        if self._playing:
            self._play_from(sample)
        else:
            self._return_sample = sample
            self.position_changed.emit(sample)

    def pause(self) -> None:
        if self._sink is not None and self._playing:
            try:
                self._sink.suspend()
            except Exception:  # noqa: BLE001
                logger.debug("audition suspend failed", exc_info=True)

    def resume(self) -> None:
        if self._sink is not None and self._playing:
            try:
                self._sink.resume()
            except Exception:  # noqa: BLE001
                logger.debug("audition resume failed", exc_info=True)

    def toggle(self, sample: Optional[int] = None) -> bool:
        if self._playing:
            self.stop()
            return False
        return self.play(sample)

    def stop(self) -> None:
        """停止并释放设备；播放头停在开始试听的位置。"""
        was_playing = self._playing
        self._timer.stop()
        self._playing = False
        self._pending = memoryview(b"")
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:  # noqa: BLE001
                logger.debug("audition sink stop failed", exc_info=True)
        self._sink = None
        self._io = None
        if was_playing:
            self.playing_changed.emit(False)
            self.position_changed.emit(self._return_sample)
            self.finished.emit()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _play_from(self, sample: int) -> bool:
        self._release_sink()
        pcm = self._pcm
        if pcm is None or pcm.shape[1] == 0:
            self.stop()
            return False
        total = pcm.shape[1]
        sample = max(0, min(total, int(sample)))
        if self._loop and not self._loop_start <= sample < self._loop_end:
            sample = self._loop_start
        elif sample == total:
            self.stop()
            return False
        try:
            from PyQt6.QtMultimedia import QAudioFormat, QAudioSink
            fmt = QAudioFormat()
            fmt.setSampleRate(self._sample_rate)
            fmt.setChannelCount(self._channels)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
            sink = QAudioSink(fmt)
            io = sink.start()
            if io is None:
                self.stop()
                return False
            self._sink = sink
            self._io = io
        except Exception as exc:  # noqa: BLE001
            logger.warning("clip audition start failed: %s", exc)
            self.stop()
            return False
        self._pending = memoryview(self._interleaved_bytes(sample))
        self._origin_sample = sample
        if not self._playing:
            self._playing = True
            self.playing_changed.emit(True)
        self._feed()
        if self._playing:
            self._timer.start()
        return self._playing

    def set_monitor(self, muted=None, solo=None) -> None:
        """P1 · 监听态（设备 buffer 域）：Mute/Solo 只影响试听输出。

        - ``muted``: 静音声道下标集合（送设备前置零）；
        - ``solo``: 独奏声道下标集合（非 solo 声道置零）；
        纯监听状态——不改 ``_pcm``、不触工程命令；导出/成片无副作用。
        """
        self._monitor_muted = frozenset(int(ch) for ch in (muted or ()))
        self._monitor_solo = frozenset(int(ch) for ch in (solo or ()))

    def _monitor_block(self, block: np.ndarray) -> np.ndarray:
        """对将要送设备的 (n, channels) 交错块应用监听矩阵。"""
        if not self._monitor_muted and not self._monitor_solo:
            return block
        muted = set(self._monitor_muted)
        if self._monitor_solo:
            muted.update(ch for ch in range(block.shape[1])
                         if ch not in self._monitor_solo)
        if not muted:
            return block
        audible = block.copy()
        for ch in muted:
            if 0 <= ch < audible.shape[1]:
                audible[:, ch] = 0.0
        return audible

    def _interleaved_bytes(self, from_sample: int) -> bytes:
        pcm = self._pcm
        total = pcm.shape[1]
        from_sample = max(0, min(total, int(from_sample)))
        end = total
        if self._loop and self._loop_end > from_sample:
            end = max(from_sample, self._loop_end)
        block = np.ascontiguousarray(pcm[:, from_sample:end].T, dtype=np.float32)
        block = self._monitor_block(block)
        return block.tobytes()

    def _release_sink(self) -> None:
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:  # noqa: BLE001
                logger.debug("audition sink release failed", exc_info=True)
        self._sink = None
        self._io = None

    def _tick(self) -> None:
        if not self._playing:
            self._timer.stop()
            return
        self._feed()
        if not self._playing or self._sink is None:
            return
        from PyQt6.QtMultimedia import QAudio
        try:
            state = self._sink.state()
        except RuntimeError:
            self.stop()
            return
        if state == QAudio.State.StoppedState:
            self.stop()
        elif not self._pending and state == QAudio.State.IdleState:
            if self._loop:
                self._play_from(self._loop_start)
            else:
                self.stop()
        else:
            self.position_changed.emit(self.position())

    def _feed(self) -> None:
        if self._io is None or self._sink is None:
            return
        try:
            free = self._sink.bytesFree()
        except Exception:  # noqa: BLE001
            self.stop()
            return
        if free <= 0 or not self._pending:
            return
        try:
            written = self._io.write(self._pending[:free].tobytes())
        except (OSError, RuntimeError):
            self.stop()
            return
        if written < 0:
            self.stop()
        else:
            self._pending = self._pending[written:]
