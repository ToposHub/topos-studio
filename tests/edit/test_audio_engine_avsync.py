"""
Final-5 (FINAL-P0-06 / FINAL-P1-13): AudioEngine + AVSyncController。

验收点：
1. AudioRingBuffer 接入 AudioPlaybackThread（__init__ 持有 ring 字段）。
2. seek 命令处理时 ring.clear() 被调用（无残留旧样本）。
3. _play_cycle 从 ring 读取（水位足够时不直接 mix_samples）。
4. AVSyncController 决策：
   - drift < 20ms → IGNORE
   - audio ahead 20-80ms → DROP
   - audio behind 20-80ms → HOLD（≥ hold_ms）
   - drift ≥ 150ms → REBUFFER
5. AudioRingBuffer 在 __init__.py 中导出。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest


# =============================================================================
# 1. AudioRingBuffer 接入 AudioPlaybackThread
# =============================================================================


def test_audio_playback_thread_has_ring_field():
    """AudioPlaybackThread.__init__ 必须持有 _ring（AudioRingBuffer 实例）。"""
    from src.features.edit.core.audio_playback import AudioPlaybackThread
    from src.features.edit.core.realtime_playback.audio_ring_buffer import (
        AudioRingBuffer,
    )

    thread = AudioPlaybackThread()
    assert hasattr(thread, "_ring"), "thread 必须有 _ring 字段"
    assert isinstance(thread._ring, AudioRingBuffer), "_ring 必须是 AudioRingBuffer"
    # prefill 目标必须 > 0
    assert thread._ring_prefill_target > 0


def test_audio_ring_buffer_exported_from_realtime_playback():
    """AudioRingBuffer 必须从 realtime_playback 包导出。"""
    from src.features.edit.core.realtime_playback import AudioRingBuffer
    assert AudioRingBuffer is not None

    from src.features.edit.core.realtime_playback import (
        AVSyncController,
        AVSyncAction,
        AVSyncDecision,
        DiskProxyServiceAdapter,
    )
    assert AVSyncController is not None
    assert AVSyncAction is not None
    assert AVSyncDecision is not None
    assert DiskProxyServiceAdapter is not None


# =============================================================================
# 2. seek 命令清空 ring + 设置 fade_in
# =============================================================================


def test_seek_command_clears_ring_and_sets_fade_in():
    """seek 命令必须 ring.clear() 并设置 _pending_fade_in=True。"""
    from src.features.edit.core.audio_playback import AudioPlaybackThread

    thread = AudioPlaybackThread()
    # 模拟 ring + clock + output
    thread._ring = MagicMock()
    thread._clock = MagicMock()
    thread._clock.sample_rate = 48000
    thread._output = MagicMock()
    thread._paused = False

    # 先写入一些数据
    cleared = []
    thread._ring.clear.side_effect = lambda: cleared.append(True)

    thread._process_command(("seek", (100, 25.0)))

    thread._ring.clear.assert_called_once()
    assert thread._pending_fade_in is True, "seek 后必须设置 _pending_fade_in"
    assert thread._next_audio_frame == 100


# =============================================================================
# 3. _play_cycle 从 ring 读取
# =============================================================================


def test_play_cycle_reads_from_ring_when_filled():
    """ring 水位足够时 _play_cycle 必须从 ring 取样本（输出路径）。

    AUD-15512 后输出路径为 peek → write → 按实际写入量 read 提交（写失败时
    样本留在 ring 不丢失）。注意：_prefill_ring 仍可能调 mix_samples 补 ring，
    所以这里只验证输出路径走 ring（write_samples 收到的是 ring.peek 的结果，
    且 write 成功后按 written 提交消费）。
    """
    from src.features.edit.core.audio_playback import AudioPlaybackThread

    thread = AudioPlaybackThread()
    thread._samples_per_frame = 1920  # 48kHz/25fps
    thread._samples_per_frame_exact = 1920.0
    thread._fps = 25.0
    thread._channels = 2
    thread._next_audio_frame = 10
    thread._last_sync_frame = 10
    thread._ring_prefill_target = 0  # 关闭 prefill（不调 mix）

    # 构造 ring：filled >= chunk_samples，peek 返回一个 (2, N) 数组
    fake_ring = MagicMock()
    fake_ring.filled = 5000
    sample_block = np.zeros((2, 1920), dtype=np.float32)
    fake_ring.peek.return_value = sample_block
    fake_ring.read.return_value = sample_block
    fake_ring.apply_fade_in = MagicMock()
    thread._ring = fake_ring

    thread._output = MagicMock()
    thread._output.bytes_per_frame.return_value = 8  # stereo int32
    thread._output.bytes_free.return_value = 100000
    thread._output._channels = 2
    # write_samples 必须收到 ring.peek 的结果（而不是 mix_samples 的结果）
    written_arg = []
    thread._output.write_samples.side_effect = lambda s: (written_arg.append(s), 1920)[1]

    thread._clock = MagicMock()
    thread._clock.sample_rate = 48000
    # mixer 被调也只是为了"电平采样"兜底（ring 满但 _latest_per_track_peaks 为空时），
    # 返回 (None, {}) 不影响输出路径——输出仍必须走 ring。
    thread._mixer = MagicMock()
    thread._mixer._decoders = {}
    thread._mixer.mix_samples.return_value = (None, {})
    thread._pending_fade_in = False

    thread._play_cycle()

    # ring.peek 必须被调用，write_samples 收到的就是 ring.peek 的结果。
    # 批量写契约（GIL 争用下一次循环最多写 3 个 chunk）：ring 水位充足时
    # 每周期 peek 1-3 次，全部走 ring 路径（不直混）。
    assert 1 <= fake_ring.peek.call_count <= 3, (
        f"ring.peek 应被调用 1-3 次，实际 {fake_ring.peek.call_count}")
    assert len(written_arg) == fake_ring.peek.call_count
    assert all(a is sample_block for a in written_arg), (
        "write_samples 必须收到 ring.peek 的结果")
    # AUD-15512：写成功后按实际写入量提交消费（不提前消费）
    assert fake_ring.read.call_count == fake_ring.peek.call_count
    for call in fake_ring.read.call_args_list:
        assert call.args[0] == 1920


def test_play_cycle_falls_back_to_mix_when_ring_underrun():
    """ring 水位不足时必须退化为直接 mix_samples（避免静音断音）。"""
    from src.features.edit.core.audio_playback import AudioPlaybackThread

    thread = AudioPlaybackThread()
    thread._samples_per_frame = 1920
    thread._samples_per_frame_exact = 1920.0
    thread._fps = 25.0
    thread._channels = 2
    thread._next_audio_frame = 10
    thread._last_sync_frame = 10
    thread._ring_prefill_target = 100

    # ring 水位极低（不足以满足 chunk_samples）
    fake_ring = MagicMock()
    fake_ring.filled = 10  # 远小于 chunk_samples
    # AUD-15512：直混结果写入 ring 后经 peek 取出写设备
    sample_block = np.zeros((2, 1920), dtype=np.float32)
    fake_ring.peek.return_value = sample_block
    thread._ring = fake_ring

    thread._output = MagicMock()
    thread._output.bytes_per_frame.return_value = 8
    thread._output.bytes_free.return_value = 100000
    thread._output._channels = 2
    thread._output.write_samples.return_value = 1920

    thread._clock = MagicMock()
    thread._clock.sample_rate = 48000

    # mixer 返回有效样本（mix_samples 现在返回 (output, per_track_peaks) 元组）
    thread._mixer = MagicMock()
    thread._mixer._decoders = {}
    thread._mixer.mix_samples.return_value = (sample_block, {})
    thread._pending_fade_in = False

    thread._play_cycle()

    # prefill_ring 会调 mix_samples 一次，外加 underrun 兜底再调一次
    assert thread._mixer.mix_samples.call_count >= 1
    # AUD-15512：直混样本先写入 ring（失败可保留），再经 peek 消费
    assert fake_ring.write.called
    assert fake_ring.peek.called


# =============================================================================
# 4. AVSyncController 决策
# =============================================================================


def test_avsync_ignore_small_drift():
    """drift < 20ms 必须 IGNORE。"""
    from src.features.edit.core.realtime_playback import (
        AVSyncAction,
        AVSyncController,
    )

    ctrl = AVSyncController(fps=25.0)
    # 0 帧差 → IGNORE
    d = ctrl.evaluate(audio_frame=100, video_frame=100)
    assert d.action is AVSyncAction.IGNORE


def test_avsync_drop_when_audio_ahead():
    """audio 领先 2 帧（25fps 2 帧=80ms，在 ignore/rebuffer 之间）必须 DROP。"""
    from src.features.edit.core.realtime_playback import (
        AVSyncAction,
        AVSyncController,
    )

    ctrl = AVSyncController(fps=25.0)
    # 2 帧差 = 80ms，处于 (20, 150) → DROP
    d = ctrl.evaluate(audio_frame=102, video_frame=100)
    assert d.action is AVSyncAction.DROP
    assert d.audio_frame == 102
    assert d.video_frame == 100


def test_avsync_rebuffer_on_large_drift():
    """drift ≥ 150ms（4 帧@25fps=160ms）必须 REBUFFER。"""
    from src.features.edit.core.realtime_playback import (
        AVSyncAction,
        AVSyncController,
    )

    ctrl = AVSyncController(fps=25.0)
    # 4 帧差 = 160ms > 150ms
    d = ctrl.evaluate(audio_frame=104, video_frame=100)
    assert d.action is AVSyncAction.REBUFFER


def test_avsync_hold_when_audio_behind():
    """audio 落后 2 帧（80ms，在 ignore/rebuffer 之间）必须 HOLD。"""
    from src.features.edit.core.realtime_playback import (
        AVSyncAction,
        AVSyncController,
    )

    ctrl = AVSyncController(fps=25.0)
    # video 领先 2 帧（audio 落后）= 80ms
    d = ctrl.evaluate(audio_frame=100, video_frame=102)
    assert d.action is AVSyncAction.HOLD, f"audio behind 2 frames 应 HOLD，实际 {d}"


def test_avsync_stats_tracks_actions():
    """stats 必须正确统计 evaluations/ignores/drops/holds/rebuffers。"""
    from src.features.edit.core.realtime_playback import (
        AVSyncAction,
        AVSyncController,
    )

    ctrl = AVSyncController(fps=25.0)
    ctrl.evaluate(audio_frame=100, video_frame=100)  # 0 drift → ignore
    ctrl.evaluate(audio_frame=102, video_frame=100)  # +2 ahead → drop (80ms)
    ctrl.evaluate(audio_frame=100, video_frame=102)  # -2 behind → hold (80ms)
    ctrl.evaluate(audio_frame=104, video_frame=100)  # +4 ahead → rebuffer (160ms)

    stats = ctrl.stats()
    assert stats["evaluations"] == 4
    assert stats["ignores"] == 1
    assert stats["drops"] == 1
    assert stats["holds"] == 1
    assert stats["rebuffers"] == 1


def test_avsync_reset_stats():
    """reset_stats 必须清零所有计数。"""
    from src.features.edit.core.realtime_playback import AVSyncController

    ctrl = AVSyncController(fps=25.0)
    ctrl.evaluate(audio_frame=105, video_frame=100)
    assert ctrl.stats()["evaluations"] == 1

    ctrl.reset_stats()
    stats = ctrl.stats()
    assert stats["evaluations"] == 0
    assert stats["drops"] == 0


# =============================================================================
# 5. AudioRingBuffer sine continuity（模拟连续写入无断裂）
# =============================================================================


def test_ring_sine_continuity_no_discontinuity():
    """连续 sine 写入后读取，样本必须连续无跳变。"""
    from src.features.edit.core.realtime_playback import AudioRingBuffer

    sr = 48000
    # 生成 100ms 连续 sine（单声道），shape (1, n)
    n = int(sr * 0.1)
    t = np.arange(n) / sr
    sine = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    ring = AudioRingBuffer(channels=1, capacity=n * 2)
    ring.write(sine.reshape(1, -1))

    # 分两次读
    half = n // 2
    chunk1 = ring.read(half).reshape(-1)
    chunk2 = ring.read(half).reshape(-1)
    # 拼接后应与原 sine 完全一致（无断裂）
    combined = np.concatenate([chunk1, chunk2])
    np.testing.assert_allclose(combined, sine[:half * 2], atol=1e-6)


def test_ring_clear_removes_residue():
    """ring.clear() 后 filled 必须为 0。"""
    from src.features.edit.core.realtime_playback import AudioRingBuffer

    ring = AudioRingBuffer(channels=2, capacity=4800)
    samples = np.ones((2, 1000), dtype=np.float32) * 0.5
    ring.write(samples)
    assert ring.filled == 1000

    ring.clear()
    assert ring.filled == 0


def test_ring_apply_fade_in_reduces_initial_peak():
    """apply_fade_in 必须把开头样本从全幅衰减（防爆音）。"""
    from src.features.edit.core.realtime_playback import AudioRingBuffer

    ring = AudioRingBuffer(channels=1, capacity=4800)
    samples = np.ones((1, 1000), dtype=np.float32)  # 全幅 1.0
    ring.write(samples)
    ring.apply_fade_in(fade_samples=100)

    out = ring.read(200).reshape(-1)
    # 前 100 样本应 < 1.0（fade 生效）
    assert out[0] < 0.1, f"fade-in 首样本应接近 0，实际 {out[0]}"
    # fade 区间外应保持 1.0
    assert abs(out[150] - 1.0) < 0.01
