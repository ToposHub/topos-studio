"""
Audio Ring Buffer
音频环形缓冲（缺陷计划 RP-P0-08 / §3.4）。

## 解决的问题

旧路径在 QAudioSink 需要数据时才 seek/decode，导致：
- underrun（缓冲空）→ 杂音/断裂。
- seek 后残留旧音频。
- 多轨混音延迟不稳定。

## 本模块

- AudioRingBuffer：固定容量 float32 interleaved ring，提前混音填充。
- write/read/clear/fill_level_ms，underrun 统计。
- 线程安全（decode thread 写，audio output thread 读）。

## 集成

AudioEngine（后续）：decode thread 提前混音写入 ring，
QAudioSink 回调从 ring 读，不触发重解码。
"""

from __future__ import annotations

import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)


class AudioRingBuffer:
    """
    固定容量 float32 interleaved 音频环形缓冲。

    samples 形状：(channels, capacity)。
    write 追加到尾部，read 从头部消费，容量满时覆盖最旧（drop）。

    用法：
        rb = AudioRingBuffer(channels=2, capacity=48000)  # 1s @ 48kHz
        rb.write(mixed_samples)   # decode thread
        chunk = rb.read(1920)     # audio output thread
    """

    def __init__(self, channels: int = 2, capacity: int = 48000) -> None:
        self._channels = max(1, int(channels))
        self._capacity = max(256, int(capacity))
        self._buffer = np.zeros((self._channels, self._capacity), dtype=np.float32)
        self._write_pos = 0  # 下一个写入位置
        self._read_pos = 0   # 下一个读取位置
        self._filled = 0     # 已填充样本数（per channel）
        self._lock = threading.RLock()

        # 统计
        self._total_written = 0
        self._total_read = 0
        self._total_dropped = 0  # 容量满时覆盖的样本数
        self._underruns = 0       # read 时数据不足次数
        self._overflows = 0       # write 时容量不足次数

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def filled(self) -> int:
        """当前已填充样本数（per channel）。"""
        with self._lock:
            return self._filled

    @property
    def available(self) -> int:
        """可写入的剩余容量。"""
        with self._lock:
            return self._capacity - self._filled

    def fill_level_ms(self, sample_rate: int = 48000) -> float:
        """当前填充时长（ms）。"""
        with self._lock:
            return (self._filled / max(1, sample_rate)) * 1000.0

    # ------------------------------------------------------------------
    # 内部工具：环形段切片
    # ------------------------------------------------------------------

    def _copy_in_segments(self, src: np.ndarray, start_pos: int) -> None:
        """把 src (channels, n) 写入 buffer 的 start_pos 起（最多两段 slice）。"""
        n = src.shape[-1]
        first = min(n, self._capacity - start_pos)
        self._buffer[:, start_pos:start_pos + first] = src[:, :first]
        if first < n:
            self._buffer[:, :n - first] = src[:, first:]

    def _copy_out_segments(self, dst: np.ndarray, start_pos: int, n: int) -> None:
        """从 buffer 的 start_pos 起读 n 个样本到 dst（最多两段 slice）。"""
        first = min(n, self._capacity - start_pos)
        dst[:, :first] = self._buffer[:, start_pos:start_pos + first]
        if first < n:
            dst[:, first:] = self._buffer[:, :n - first]

    # ------------------------------------------------------------------
    # write / read
    # ------------------------------------------------------------------

    def write(self, samples: np.ndarray) -> int:
        """
        写入样本（追加到尾部）。

        Args:
            samples: (channels, n) 或 (1, n) float32，范围 [-1, 1]。

        Returns:
            实际写入的样本数（per channel）。容量满时覆盖最旧（drop）。
        """
        if samples is None or samples.size == 0:
            return 0
        # 确保 shape 是 (channels, n)
        if samples.ndim == 1:
            samples = samples.reshape(1, -1)
        n = samples.shape[-1]
        # 通道不匹配时广播/截断
        if samples.shape[0] != self._channels:
            if samples.shape[0] == 1:
                samples = np.repeat(samples, self._channels, axis=0)
            else:
                samples = samples[:self._channels]

        with self._lock:
            # 容量满：整批覆盖最旧（drop read pointer），记 overflow（每次 write 一次）。
            if self._filled >= self._capacity:
                overflow_n = min(n, self._capacity)
                self._total_dropped += overflow_n
                self._overflows += 1
                # 只保留尾部最多 capacity 个样本（超过容量的头部直接丢弃）。
                if n > self._capacity:
                    samples = samples[:, -self._capacity:]
                    n = self._capacity
                self._copy_in_segments(samples, self._write_pos)
                self._write_pos = (self._write_pos + n) % self._capacity
                self._read_pos = self._write_pos  # 全满：read 紧跟 write
                self._total_written += n
                return n

            # 正常路径：向量化两段 slice 拷贝（替代逐样本 Python 循环）。
            self._copy_in_segments(samples, self._write_pos)
            self._write_pos = (self._write_pos + n) % self._capacity
            new_filled = self._filled + n
            if new_filled > self._capacity:
                # 写入中途超出容量：覆盖最旧样本（drop read pointer）。
                overflow_n = new_filled - self._capacity
                self._read_pos = (self._read_pos + overflow_n) % self._capacity
                self._total_dropped += overflow_n
                self._overflows += 1
                self._filled = self._capacity
            else:
                self._filled = new_filled
            self._total_written += n
            return n

    def read(self, num_samples: int) -> np.ndarray:
        """
        读取 num_samples 样本（从头部消费）。

        数据不足时返回实际可用的（短于 num_samples），不足部分记 underrun。

        Returns:
            (channels, actual) float32。
        """
        num_samples = max(0, int(num_samples))
        if num_samples == 0:
            return np.zeros((self._channels, 0), dtype=np.float32)

        with self._lock:
            if self._filled < num_samples:
                self._underruns += 1
            actual = min(num_samples, self._filled)
            if actual == 0:
                return np.zeros((self._channels, 0), dtype=np.float32)
            result = np.empty((self._channels, actual), dtype=np.float32)
            self._copy_out_segments(result, self._read_pos, actual)
            self._read_pos = (self._read_pos + actual) % self._capacity
            self._filled -= actual
            self._total_read += actual
            return result

    def peek(self, num_samples: int) -> np.ndarray:
        """预览（不消费）。"""
        num_samples = max(0, int(num_samples))
        with self._lock:
            actual = min(num_samples, self._filled)
            result = np.zeros((self._channels, actual), dtype=np.float32)
            if actual > 0:
                self._copy_out_segments(result, self._read_pos, actual)
            return result

    # ------------------------------------------------------------------
    # clear
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """清空（seek 后调用，避免残留旧音频）。"""
        with self._lock:
            self._buffer.fill(0.0)
            self._write_pos = 0
            self._read_pos = 0
            self._filled = 0

    # ------------------------------------------------------------------
    # fade（seek 后避免爆音）
    # ------------------------------------------------------------------

    def apply_fade_in(self, fade_samples: int = 64) -> None:
        """对头部已填充数据应用 fade-in（seek 后避免爆音）。"""
        fade_samples = max(0, int(fade_samples))
        with self._lock:
            actual = min(fade_samples, self._filled)
            if actual <= 0:
                return
            # 向量化：构造 gain 曲线后按环形段相乘（替代逐样本 Python 循环）。
            gain = np.linspace(1.0 / actual, 1.0, actual, dtype=np.float32)
            first = min(actual, self._capacity - self._read_pos)
            self._buffer[:, self._read_pos:self._read_pos + first] *= gain[:first]
            if first < actual:
                rest = actual - first
                self._buffer[:, :rest] *= gain[first:]

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "channels": self._channels,
                "capacity": self._capacity,
                "filled": self._filled,
                "available": self._capacity - self._filled,
                "total_written": self._total_written,
                "total_read": self._total_read,
                "total_dropped": self._total_dropped,
                "underruns": self._underruns,
                "overflows": self._overflows,
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._total_written = 0
            self._total_read = 0
            self._total_dropped = 0
            self._underruns = 0
            self._overflows = 0
