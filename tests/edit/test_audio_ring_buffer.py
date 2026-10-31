"""
Batch 8 tests: AudioRingBuffer 音频环形缓冲。
对应缺陷计划 RP-P0-08 / §3.4。

核心验收：
- write/read 连续性（不丢样本）。
- fill_level_ms 正确。
- underrun 统计（read 超过 filled）。
- overflow 统计（write 超过 capacity，drop 最旧）。
- clear 清空（seek 后无残留）。
- fade-in 避免爆音。
- 多通道处理。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback.audio_ring_buffer import AudioRingBuffer


class TestAudioRingBuffer:
    def test_write_then_read(self) -> None:
        rb = AudioRingBuffer(channels=2, capacity=1000)
        samples = np.ones((2, 100), dtype=np.float32) * 0.5
        written = rb.write(samples)
        assert written == 100
        assert rb.filled == 100

        out = rb.read(100)
        assert out.shape == (2, 100)
        assert rb.filled == 0

    def test_no_sample_loss_sequential(self) -> None:
        """连续 write/read 不丢样本（核心验收）。"""
        rb = AudioRingBuffer(channels=1, capacity=2000)
        # 写入递增序列
        source = np.arange(500, dtype=np.float32).reshape(1, -1) / 1000.0
        rb.write(source)
        out = rb.read(500)
        # 读出的应与写入完全一致
        np.testing.assert_allclose(out[0], source[0])

    def test_fill_level_ms(self) -> None:
        rb = AudioRingBuffer(channels=2, capacity=48000)
        rb.write(np.ones((2, 4800), dtype=np.float32))  # 4800/48000 = 0.1s
        assert abs(rb.fill_level_ms(48000) - 100.0) < 0.1

    def test_underrun_recorded(self) -> None:
        """read 超过 filled 记 underrun。"""
        rb = AudioRingBuffer(channels=1, capacity=1000)
        rb.write(np.ones((1, 50), dtype=np.float32))
        out = rb.read(100)  # 只要 50
        assert out.shape[-1] == 50
        s = rb.stats()
        assert s["underruns"] == 1

    def test_overflow_drops_oldest(self) -> None:
        """容量满时 write 覆盖最旧（drop）。"""
        # capacity 有最小钳制（256），用足够小的写入测试
        rb = AudioRingBuffer(channels=1, capacity=300)
        # 写满 300
        rb.write(np.arange(300, dtype=np.float32).reshape(1, -1))
        assert rb.filled == 300
        # 再写 50（覆盖最旧 50）
        rb.write(np.full((1, 50), 999.0, dtype=np.float32))
        s = rb.stats()
        assert s["total_dropped"] == 50
        assert s["overflows"] == 1
        # filled 仍是 300
        assert rb.filled == 300
        # 读出应包含 999（新数据）
        out = rb.read(300)
        assert (out == 999.0).sum() == 50

    def test_clear_removes_all(self) -> None:
        """clear 后无残留（seek 后无旧音频）。"""
        rb = AudioRingBuffer(channels=2, capacity=500)
        rb.write(np.ones((2, 200), dtype=np.float32))
        assert rb.filled == 200
        rb.clear()
        assert rb.filled == 0
        out = rb.read(100)
        assert out.shape[-1] == 0  # 空

    def test_fade_in_applied(self) -> None:
        """fade-in 对头部应用增益渐变（避免爆音）。"""
        rb = AudioRingBuffer(channels=1, capacity=500)
        rb.write(np.ones((1, 100), dtype=np.float32))
        rb.apply_fade_in(fade_samples=64)
        out = rb.read(64)
        # 第一个样本增益最小（接近 0），最后一个接近 1
        assert out[0, 0] < 0.1
        assert out[0, -1] > 0.9

    def test_partial_read_preserves_rest(self) -> None:
        """部分 read 不影响剩余数据。"""
        rb = AudioRingBuffer(channels=1, capacity=500)
        rb.write(np.arange(100, dtype=np.float32).reshape(1, -1))
        out1 = rb.read(30)
        out2 = rb.read(30)
        # 数据连续
        np.testing.assert_allclose(out1[0], np.arange(30, dtype=np.float32))
        np.testing.assert_allclose(out2[0], np.arange(30, 60, dtype=np.float32))

    def test_multi_channel_broadcast(self) -> None:
        """单通道输入广播到多通道。"""
        rb = AudioRingBuffer(channels=2, capacity=500)
        rb.write(np.ones((1, 50), dtype=np.float32) * 0.7)  # shape (1, 50)
        out = rb.read(50)
        assert out.shape == (2, 50)
        # 两通道数据一致
        np.testing.assert_allclose(out[0], out[1])

    def test_peek_does_not_consume(self) -> None:
        """peek 不消费数据。"""
        rb = AudioRingBuffer(channels=1, capacity=500)
        rb.write(np.ones((1, 100), dtype=np.float32))
        rb.peek(50)
        assert rb.filled == 100  # 未消费

    def test_stats(self) -> None:
        rb = AudioRingBuffer(channels=2, capacity=1000)
        rb.write(np.ones((2, 100), dtype=np.float32))
        rb.read(50)
        s = rb.stats()
        assert s["total_written"] == 100
        assert s["total_read"] == 50
        assert s["filled"] == 50

    def test_zero_read_returns_empty(self) -> None:
        rb = AudioRingBuffer(channels=2, capacity=100)
        rb.write(np.ones((2, 10), dtype=np.float32))
        out = rb.read(0)
        assert out.shape == (2, 0)
