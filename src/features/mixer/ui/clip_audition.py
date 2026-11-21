"""Isolated audition playback for the Mixer Clip Editor (P0-2 §2.3).

One session owns the Qt audio device for clip auditioning.  It consumes the
same planar float32 PCM the editor visualizes (see
``src.features.edit.core.clip_pcm``), so the waveform, the spectrogram and
what you hear share one coordinate system.

Behaviour contract (roadmap §2.3, P0-4 of the 2026-11-21 handoff):

- Play/stop/seek/loop/skip-selection all target this one session; loop uses
  the half-open window ``[start, end)`` (whole clip when no selection).
- **Chunked feeding**: the session keeps a bounded transmit queue and pulls
  small interleaved blocks from the PCM on demand (``_CHUNK_FRAMES``).  The
  device buffer is capped (``_MAX_BUFFER_SECONDS``), which also defines the
  maximum audible latency for live monitor-matrix / skip-range changes —
  memory no longer grows with clip length.
- The consumption cursor only advances by frames the device actually
  accepted (short writes re-produce their tail from the cursor), and the
  playhead maps device-consumed frames back to PCM positions through the
  queued-chunk records, so skipped spans are reflected exactly.
- **Hot-swap load**: loading new PCM while playing (A/B bypass, stack
  commits) keeps the position and the playing state. On an unchanged time
  axis, the already queued tail plays out before a short blend into the new
  source. A format or coordinate change restarts at the mapped playhead.
- Position is derived from the device clock (``processedUSecs``), not from
  wall time, so playhead feedback tracks what the listener hears.
- Stop returns to the position auditioning started from; every lifecycle
  exit (stop, device loss, project switch) releases the sink.
"""
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

logger = logging.getLogger(__name__)


class ClipAuditionSession(QObject):
    """QAudioSink push-mode playback over an in-memory PCM slice."""

    #: 单块入队帧数（供块粒度）：监听矩阵/跳区切换在下一块立即生效，
    #: 亦即未入队数据的变化延迟上界。
    _CHUNK_FRAMES = 2048
    #: 设备缓冲时长上限：已入队字节的最大可听延迟（防爆音与 underrun
    #: 的折中；循环/跳转长源时内存不随片长增长）。
    _MAX_BUFFER_SECONDS = 0.12
    _SWAP_FADE_SECONDS = 0.005

    position_changed = pyqtSignal(int)      # 片段局部样本位置
    playing_changed = pyqtSignal(bool)
    finished = pyqtSignal()
    device_failed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pcm: Optional[np.ndarray] = None
        self._sample_rate = 48000
        self._channels = 1
        self._sink = None
        self._io = None
        self._playing = False
        self._paused = False
        self._loop = False
        self._loop_start = 0
        self._loop_end: int | None = None
        self._play_end: int | None = None
        self._origin_sample = 0
        self._return_sample = 0   # 停止后返回的位置（开始试听的游标）
        # P0-4 · 分块供块状态：游标 = 下一个待产块的 PCM 起点；只按设备
        # 实际接受的帧数推进块记录（短写的字节残留在 ``_pending``，整块
        # 转移完成后游标才前进到块尾——重试不重复、不丢帧）。
        # ``_chunks`` 记录 (设备帧起点, PCM 起点, 帧数)，把设备消费时钟
        # 精确映射回 PCM 位置（跳区跨越不产生播放头跳变之外的误差）。
        # ``_source_exhausted`` = 源侧已无可产数据（配合设备 Idle 判定
        # 自然播尽）。
        self._cursor = 0
        self._pending = b""
        self._pending_pcm_start = 0
        self._pending_block_end = 0
        self._chunks: deque[tuple[int, int, int]] = deque()
        self._queued_frames = 0
        self._queued_partial_bytes = 0
        self._source_exhausted = False
        self._swap_fade: tuple[int, np.ndarray] | None = None
        # P0-4 · 供块层跳过的半开区间（跳选区试听；None=不跳）。
        self._skip_range: Optional[tuple[int, int]] = None
        # P1 · 监听态（L/R Mute/Solo）：只作用于送入设备 buffer 的字节，
        # 不改 PCM 源、不入工程——导出/成片零副作用。
        self._monitor_muted: frozenset[int] = frozenset()
        self._monitor_solo: frozenset[int] = frozenset()
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
    def is_paused(self) -> bool:
        return self._paused

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def has_audio(self) -> bool:
        return self._pcm is not None and self._pcm.shape[1] > 0

    def position(self) -> int:
        """当前播放头（片段局部样本）；由设备消费时钟经块记录映射。"""
        if self._pcm is None:
            return 0
        if self._sink is None or not self._playing:
            return self._return_sample
        consumed = self._consumed_frames()
        position = self._origin_sample
        if self._chunks:
            first_dev, first_pcm, _ = self._chunks[0]
            if consumed < first_dev:
                position = first_pcm
            else:
                for dev_start, pcm_start, frames in reversed(self._chunks):
                    if consumed >= dev_start:
                        position = pcm_start + min(consumed - dev_start, frames)
                        break
        return int(max(0, min(self._pcm.shape[1], position)))

    def return_sample(self) -> int:
        return self._return_sample

    # ------------------------------------------------------------------
    # 装载与播放
    # ------------------------------------------------------------------
    def load(self, pcm: np.ndarray, sample_rate: int, *,
             position_map: Callable[[int], int] | None = None) -> None:
        """Swap the PCM, optionally mapping old sample positions into the new clip.

        For an unchanged time axis, keep the already queued audio and swap at
        the next frame the device needs. For a changed coordinate system,
        restart the sink at the mapped audible position.
        """
        rate = int(sample_rate) if sample_rate > 0 else 48000
        channels = max(1, pcm.shape[0])
        total = pcm.shape[1]
        previous = self._pcm
        old_rate = self._sample_rate
        format_changed = rate != old_rate or channels != self._channels
        if pcm is self._pcm and not format_changed and position_map is None:
            return
        position = self.position()
        if position_map is None and rate != old_rate:
            def rescale(sample: int) -> int:
                return (sample * rate + old_rate // 2) // old_rate

            position_map = rescale
        if position_map is not None:
            position = position_map(position)
            self._return_sample = position_map(self._return_sample)
            self._loop_start = max(0, position_map(self._loop_start))
            if self._loop_end is not None:
                self._loop_end = position_map(self._loop_end)
            if self._play_end is not None:
                self._play_end = position_map(self._play_end)
            if self._skip_range is not None:
                start, end = map(position_map, self._skip_range)
                self._skip_range = (max(0, start), end) if end > max(0, start) else None
        self._pcm = pcm
        self._sample_rate = rate
        self._channels = channels
        self._return_sample = max(0, min(self._return_sample, total))
        if not self._playing:
            return
        if not 0 <= position < total:
            self.stop()
        elif format_changed or position_map is not None:
            self._play_from(position)
        else:
            self._discard_unqueued_frames()
            if previous is not None:
                self._prepare_swap_fade(previous)
            self._source_exhausted = False
            self._feed()

    def clear(self) -> None:
        self.stop()
        self._pcm = None
        self._return_sample = 0
        self._skip_range = None
        self._loop = False
        self._loop_start, self._loop_end = 0, None

    def set_loop(self, enabled: bool, start: int = -1, end: int = -1) -> None:
        """循环窗口为半开 [start, end)；无选区时用整段。

        P0-4 · 播放中更新窗口不打断：供块层从下一块起按新窗口产数
        （游标在窗外时回绕到窗首），无需重建设备缓冲。
        """
        state = (bool(enabled), max(0, start), end if end > 0 else None)
        if state != (self._loop, self._loop_start, self._loop_end):
            self._loop, self._loop_start, self._loop_end = state
            self._discard_unqueued_frames()
            self._swap_fade = None
            self._source_exhausted = False

    def set_skip_range(self, start: Optional[int] = None,
                       end: Optional[int] = None) -> None:
        """P0-4 · 供块层跳过半开区间 ``[start, end)``（None=清除）。

        跳过发生在填充设备缓冲**之前**——选区内容不进设备（与旧的事后
        seek 不同，选区开头不会漏进设备）；只影响未入队数据，已入队
        字节在设备缓冲延迟内自然播完。
        """
        skip = None
        if start is not None and end is not None and end > max(0, start):
            skip = (max(0, int(start)), int(end))
        if skip != self._skip_range:
            self._skip_range = skip
            self._discard_unqueued_frames()
            self._swap_fade = None
            self._source_exhausted = False

    def play(self, start_sample: Optional[int] = None, *, end_sample: int | None = None) -> bool:
        """Play from a PCM sample up to an optional exclusive endpoint."""
        if self._pcm is None or self._pcm.shape[1] == 0:
            return False
        if start_sample is None:
            start_sample = self._return_sample
        start_sample = max(0, int(start_sample))
        self._paused = False
        self._play_end = end_sample
        loop = self._loop_bounds()
        if loop is not None and not loop[0] <= start_sample < loop[1]:
            start_sample = loop[0]
        elif end_sample is None and start_sample >= self._pcm.shape[1]:
            start_sample = 0
        self._return_sample = min(start_sample, self._pcm.shape[1])
        return self._play_from(start_sample)

    def seek(self, sample: int) -> None:
        """播放中跳转（重建缓冲丢弃在途旧数据）；停止态只移动返回点。"""
        if self._pcm is None:
            return
        sample = max(0, min(self._pcm.shape[1], int(sample)))
        if self._playing:
            self._play_from(sample)
        else:
            self._return_sample = sample
            self.position_changed.emit(sample)

    def pause(self) -> None:
        if self._sink is not None and self._playing and not self._paused:
            try:
                self._sink.suspend()
            except RuntimeError:
                self._fail_device()
            else:
                self._paused = True

    def resume(self) -> None:
        if self._sink is not None and self._paused:
            try:
                self._sink.resume()
            except RuntimeError:
                self._fail_device()
            else:
                self._paused = False
                self._feed()

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
        self._paused = False
        self._play_end = None
        self._pending = b""
        self._chunks.clear()
        self._queued_frames = 0
        self._queued_partial_bytes = 0
        self._source_exhausted = False
        self._swap_fade = None
        self._release_sink()
        if was_playing:
            self.playing_changed.emit(False)
            self.position_changed.emit(self._return_sample)
            self.finished.emit()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _frame_bytes(self) -> int:
        return max(1, self._channels) * 4  # float32 交错

    def _buffer_bytes(self) -> int:
        return max(1, int(self._sample_rate * self._MAX_BUFFER_SECONDS)) * self._frame_bytes()

    def _consumed_frames(self) -> int:
        if self._sink is None:
            return 0
        try:
            usecs = max(0, int(self._sink.processedUSecs()))
        except RuntimeError:
            return 0
        return min(self._queued_frames, (usecs * self._sample_rate + 500_000) // 1_000_000)

    def _play_limit(self) -> int:
        total = self._pcm.shape[1] if self._pcm is not None else 0
        return total if self._play_end is None else min(total, self._play_end)

    def _loop_bounds(self) -> tuple[int, int] | None:
        end = self._play_limit()
        if self._loop_end is not None:
            end = min(end, self._loop_end)
        if self._loop and self._loop_start < end:
            return self._loop_start, end
        return None

    def _discard_unqueued_frames(self) -> None:
        """Retain only the tail of any frame the sink has partially accepted."""
        if self._pending:
            frame_bytes = self._frame_bytes()
            if self._queued_partial_bytes:
                self._pending = self._pending[:frame_bytes - self._queued_partial_bytes]
                self._pending_block_end = self._pending_pcm_start + 1
            else:
                self._pending = b""
                self._cursor = self._pending_pcm_start

    def _prepare_swap_fade(self, previous: np.ndarray) -> None:
        """Blend only the first few newly queued frames on the same time axis."""
        next_sample = self._pending_block_end if self._pending else self._cursor
        frames = max(2, round(self._sample_rate * self._SWAP_FADE_SECONDS))
        start, block = self._produce_block(frames, cursor=next_sample)
        previous_fade, self._swap_fade = self._swap_fade, None
        if block is None:
            return
        stop = min(start + block.shape[1], previous.shape[1])
        if stop - start < 2:
            return
        old = previous[:, start:stop].copy()
        if previous_fade is not None:
            fade_start, fade_pcm = previous_fade
            lo = max(start, fade_start)
            hi = min(stop, fade_start + fade_pcm.shape[1])
            if hi > lo:
                old[:, lo - start:hi - start] = fade_pcm[:, lo - fade_start:hi - fade_start]
        gain = np.linspace(0.0, 1.0, stop - start, dtype=np.float32)
        self._swap_fade = (start, old * (1.0 - gain) + block[:, :gain.size] * gain)

    def _play_from(self, sample: int) -> bool:
        self._release_sink()
        limit = self._play_limit()
        if limit <= 0:
            self.stop()
            return False
        sample = max(0, min(limit, int(sample)))
        loop = self._loop_bounds()
        if loop is not None and not loop[0] <= sample < loop[1]:
            sample = loop[0]
        elif sample == limit:
            self.stop()
            return False
        try:
            from PyQt6.QtMultimedia import QAudioFormat, QAudioSink
            fmt = QAudioFormat()
            fmt.setSampleRate(self._sample_rate)
            fmt.setChannelCount(self._channels)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
            sink = QAudioSink(fmt)
            try:
                # P0-4 · 有界设备缓冲：监听/跳区变更的最大可听延迟。
                sink.setBufferSize(self._buffer_bytes())
            except RuntimeError:
                logger.debug("audition sink buffer setup failed", exc_info=True)
            io = sink.start()
            if io is None:
                self.stop()
                return False
            self._sink = sink
            self._io = io
            if self._paused:
                sink.suspend()
        except Exception as exc:  # noqa: BLE001
            logger.warning("clip audition start failed: %s", exc)
            self.stop()
            return False
        self._cursor = sample
        self._origin_sample = sample
        self._pending = b""
        self._pending_pcm_start = sample
        self._pending_block_end = sample
        self._chunks.clear()
        self._queued_frames = 0
        self._queued_partial_bytes = 0
        self._source_exhausted = False
        self._swap_fade = None
        if not self._playing:
            self._playing = True
            self.playing_changed.emit(True)
        self._feed()
        if self._playing:
            self._timer.start()
        return self._playing

    def set_monitor(self, muted: Iterable[int] | None = None,
                    solo: Iterable[int] | None = None) -> None:
        """P1 · 监听态（设备 buffer 域）：Mute/Solo 只影响试听输出。

        - ``muted``: 静音声道下标集合（送设备前置零）；
        - ``solo``: 独奏声道下标集合（非 solo 声道置零）；
        纯监听状态——不改 ``_pcm``、不触工程命令；导出/成片无副作用。
        P0-4 · 分块供块下切换在**下一块**（≤ ``_CHUNK_FRAMES`` + 设备
        缓冲延迟）立即生效，不再等待重新装载。
        """
        matrix = (frozenset(int(ch) for ch in (muted or ())),
                  frozenset(int(ch) for ch in (solo or ())))
        if matrix != (self._monitor_muted, self._monitor_solo):
            self._monitor_muted, self._monitor_solo = matrix
            self._discard_unqueued_frames()

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

    def _produce_block(self, want: int, *, cursor: int | None = None) -> tuple[int, Optional[np.ndarray]]:
        """从游标产下一块 ``(pcm_start, block)``；无数据返回 ``(0, None)``。

        按循环窗口与跳区调整：游标在窗外回绕窗首；进入跳区则越过到
        跳区末（跳过的样本不进设备）；块尾不越过窗口右端/跳区左端。
        """
        pcm = self._pcm
        if pcm is None:
            return 0, None
        if cursor is None:
            cursor = self._cursor
        loop = self._loop_bounds()
        if loop is not None:
            loop_start, end_limit = loop
            if not loop_start <= cursor < end_limit:
                cursor = loop_start
        else:
            end_limit = self._play_limit()
        skip = self._skip_range
        if skip is not None and skip[0] <= cursor < skip[1]:
            cursor = skip[1]
            if cursor >= end_limit and loop is not None and loop[0] < skip[0]:
                cursor = loop[0]
        block_end = min(end_limit, cursor + want)
        if skip is not None and cursor < skip[0] < block_end:
            block_end = skip[0]
        if block_end <= cursor:
            return 0, None
        return cursor, pcm[:, cursor:block_end]

    def _feed(self) -> None:
        """按设备剩余空间拉块入队；游标只前进实际写入的帧数。

        短写（设备只接受块的一部分）把未写字节留在 ``_pending``，下轮
        tick 续写——FIFO 只前进已实际写入的帧，重试不引入重复/丢失。
        """
        if self._io is None or self._sink is None or not self._playing or self._paused:
            return
        frame_bytes = self._frame_bytes()
        buffered = (self._queued_frames - self._consumed_frames()) * frame_bytes
        remaining = self._buffer_bytes() - buffered - self._queued_partial_bytes
        while remaining > 0:
            try:
                free = min(self._sink.bytesFree(), remaining)
            except RuntimeError:
                self._fail_device()
                return
            if free <= 0:
                break
            if not self._pending:
                pcm_start, block = self._produce_block(max(
                    1, min(self._CHUNK_FRAMES, free // frame_bytes or 1)))
                if block is None:
                    self._source_exhausted = True
                    break
                self._source_exhausted = False
                self._pending = self._encode_block(pcm_start, block)
                self._pending_pcm_start = pcm_start
                self._pending_block_end = pcm_start + block.shape[1]
            try:
                written = self._io.write(self._pending[:free])
            except (OSError, RuntimeError):
                self._fail_device()
                return
            if written is None or written < 0:
                self._fail_device()
                return
            written = int(written)
            if written == 0:
                break  # 设备暂时拒收：等下一轮 tick 重试
            remaining -= written
            self._pending = self._pending[written:]
            frames, self._queued_partial_bytes = divmod(
                self._queued_partial_bytes + written, frame_bytes)
            if frames:
                self._chunks.append(
                    (self._queued_frames, self._pending_pcm_start, frames))
                self._queued_frames += frames
                self._pending_pcm_start += frames
            if not self._pending:
                # 整块转移完成：消费游标前进到块尾（下一块由此产出）。
                self._cursor = self._pending_block_end
                if self._swap_fade is not None:
                    start, fade = self._swap_fade
                    if self._cursor >= start + fade.shape[1]:
                        self._swap_fade = None
                self._prune_chunks()

    def _encode_block(self, pcm_start: int, block: np.ndarray) -> bytes:
        fade = self._swap_fade
        if fade is not None:
            fade_start, fade_pcm = fade
            lo = max(pcm_start, fade_start)
            hi = min(pcm_start + block.shape[1], fade_start + fade_pcm.shape[1])
            if hi > lo:
                block = block.copy()
                block[:, lo - pcm_start:hi - pcm_start] = fade_pcm[:, lo - fade_start:hi - fade_start]
        return self._monitor_block(np.ascontiguousarray(block.T, dtype=np.float32)).tobytes()

    def _prune_chunks(self) -> None:
        """丢弃远早于设备消费位置的块记录（位置映射只需近期历史）。"""
        consumed = self._consumed_frames()
        while len(self._chunks) > 1:
            dev_start, _pcm_start, frames = self._chunks[0]
            if dev_start + frames >= consumed:
                break
            self._chunks.popleft()

    def _interleaved_bytes(self, from_sample: int) -> bytes:
        """受监听矩阵作用的交错字节（从样本到循环窗/段尾；测试与诊断）。"""
        pcm = self._pcm
        if pcm is None:
            return b""
        total = pcm.shape[1]
        from_sample = max(0, min(total, int(from_sample)))
        end = total
        loop = self._loop_bounds()
        if loop is not None and loop[1] > from_sample:
            end = loop[1]
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

    def _fail_device(self) -> None:
        self.stop()
        self.device_failed.emit()

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
            self._fail_device()
            return
        if state == QAudio.State.StoppedState:
            self._fail_device()
        elif self._paused:
            return
        elif state == QAudio.State.IdleState and self._source_exhausted:
            # 写侧已排空且源侧无可产数据：自然播尽（循环模式供块层已
            # 回绕，不会走到这里；到达即窗口为空）。
            self.stop()
        else:
            self.position_changed.emit(self.position())
