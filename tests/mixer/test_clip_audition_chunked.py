"""P0-4 · 分块试听、跳选区、A/B 热替换与监听即时性。

验收来自 docs/plans/2026-11-21-mixer-clip-editor-backend-handoff.md：

- 模拟短写和 underrun：每次 FIFO 只前进已实际写入的帧；
- 播放中切 L/R 监听：下一块未排队数据立即改变（工程与 Stem 恒等）；
- 循环/跳转长源：内存不随片长增长（块粒度 + 有界设备缓冲）；
- 跳选区在供块层完成：选区内容不进设备，播放头经块记录映射不落进
  选区；
- A/B 重开处理结果：load 热替换保留位置与播放状态，不打断。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RATE = 48000


class FakeDevice:
    """确定性伪设备：容量有界、全量接受写入、消费由测试驱动。"""

    def __init__(self, rate: int, channels: int,
                 capacity_frames: int = 8192) -> None:
        self.rate = rate
        self.channels = channels
        self.frame_bytes = channels * 4
        self.capacity = capacity_frames * self.frame_bytes
        self.stream = bytearray()   # 设备已接受的全部字节
        self.buffer = bytearray()   # 待消费
        self.consumed_frames = 0
        self.paused = False
        self.stopped = False

    # -- QAudioSink / QIODevice 表面 ------------------------------------
    def start(self):
        return self

    def setBufferSize(self, _size: int) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True

    def suspend(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def bytesFree(self) -> int:
        return max(0, self.capacity - len(self.buffer))

    def processedUSecs(self) -> int:
        return int(self.consumed_frames * 1_000_000 // self.rate)

    def write(self, data: bytes) -> int:
        room = self.bytesFree()
        take = min(len(data), room)
        if take:
            self.stream.extend(data[:take])
            self.buffer.extend(data[:take])
        return take

    def state(self):
        from PyQt6.QtMultimedia import QAudio
        if self.stopped:
            return QAudio.State.StoppedState
        if self.paused:
            return QAudio.State.SuspendedState
        if self.buffer:
            return QAudio.State.ActiveState
        return QAudio.State.IdleState

    # -- 测试驱动 ---------------------------------------------------------
    def consume(self, frames: int) -> None:
        drop = min(len(self.buffer), frames * self.frame_bytes)
        self.buffer = self.buffer[drop:]
        self.consumed_frames += drop // self.frame_bytes

    def frames(self) -> np.ndarray:
        data = bytes(self.stream)
        usable = len(data) - len(data) % self.frame_bytes
        return np.frombuffer(data[:usable], dtype=np.float32).reshape(
            -1, self.channels)


def _install(monkeypatch, device: FakeDevice) -> None:
    from PyQt6 import QtMultimedia

    monkeypatch.setattr(
        QtMultimedia, "QAudioSink",
        lambda _fmt: device)


def _ramp(frames: int, channels: int = 2) -> np.ndarray:
    """每样本唯一可辨识值（帧号/总帧数 + 声道偏置）。"""
    t = (np.arange(frames) / max(1, frames)).astype(np.float32)
    return np.stack([t, t * 0.5 + 0.25] if channels == 2
                    else [t] * channels)


class TestChunkedFeeding:
    def test_fifo_advances_only_written_frames(self, qtbot, monkeypatch) -> None:
        """短写：FIFO 只前进实际写入的帧，流内容无重复/丢失。"""
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(200)
        device = FakeDevice(RATE, 2, capacity_frames=8192)
        _install(monkeypatch, device)
        # 强制短写：每次 write 只接受 12 字节（1.5 帧——非对齐短写）。
        original_write = device.write

        def short_write(data: bytes) -> int:
            return original_write(data[:12])

        device.write = short_write
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        assert session.play(0)
        for _ in range(400):
            session._feed()
        got = device.frames()
        want = np.ascontiguousarray(pcm.T)
        np.testing.assert_array_equal(got, want)
        device.consume(153)
        assert session.position() == 153
        session.stop()

    def test_chunk_size_bounded(self, qtbot, monkeypatch) -> None:
        """块粒度有上界：单次写入不超过 _CHUNK_FRAMES 帧（内存不随片长增长）。"""
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(RATE * 2)  # 2 s 立体声
        device = FakeDevice(RATE, 2, capacity_frames=8192)
        writes: list[int] = []
        original_write = device.write

        def counting_write(data: bytes) -> int:
            writes.append(len(data))
            return original_write(data)

        device.write = counting_write
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        assert session.play(0)
        session._feed()
        max_chunk_frames = max(writes) // 8
        assert max_chunk_frames <= ClipAuditionSession._CHUNK_FRAMES
        # 挂起态字节总量不超过设备容量（有界传输队列）。
        assert len(device.buffer) == int(RATE * session._MAX_BUFFER_SECONDS) * device.frame_bytes
        session.stop()

    def test_underrun_during_short_write_waits_for_remaining_frames(self, qtbot, monkeypatch) -> None:
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        device = FakeDevice(RATE, 2, capacity_frames=200)
        _install(monkeypatch, device)
        pcm = _ramp(200)
        original_write = device.write
        room = 8 * 51

        def stalled_write(data: bytes) -> int:
            return original_write(data[:max(0, room - len(device.stream))])

        device.write = stalled_write
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        assert session.play(0)
        device.consume(51)
        session._tick()
        assert session.is_playing
        assert session.position() == 51

        device.write = original_write
        session._tick()
        np.testing.assert_array_equal(device.frames(), pcm.T)
        device.consume(149)
        session._tick()
        assert not session.is_playing

    def test_monitor_switch_applies_to_next_chunk(self, qtbot, monkeypatch) -> None:
        """播放中切 L 监听静音：下一块立即置零，之前的块不受影响。"""
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(RATE)  # 每帧唯一
        device = FakeDevice(RATE, 2, capacity_frames=2048)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        assert session.play(0)
        session._feed()
        before = device.frames().copy()
        assert np.any(before[:, 0] > 0.01)
        # 设备消费一半，播放中切监听（L 静音）。
        device.consume(1024)
        session.set_monitor(muted={0})
        session._feed()
        after = device.frames()
        boundary = before.shape[0]
        new_part = after[boundary:]
        assert new_part.shape[0] > 0
        assert np.all(new_part[:, 0] == 0.0)
        assert np.any(np.abs(new_part[:, 1]) > 0.01)  # R 仍在
        # 切换前的已入队块不被改写（源 PCM 恒等——导出/成片零副作用）。
        np.testing.assert_array_equal(before[:, 0], pcm[0, :boundary])
        session.stop()


class TestSkipSelection:
    def test_skip_region_never_reaches_device(self, qtbot, monkeypatch) -> None:
        """选区 [2000,3000) 不进设备：设备流 = 源去掉该区间后的拼接。"""
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(RATE)
        device = FakeDevice(RATE, 2, capacity_frames=8192)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        session.set_skip_range(2000, 3000)
        assert session.play(0)
        while not session._source_exhausted:
            device.consume(4096)
            session._feed()
        got = device.frames()
        want = np.ascontiguousarray(
            np.concatenate([pcm[:, :2000], pcm[:, 3000:]], axis=1).T)
        np.testing.assert_allclose(got, want, atol=1e-6)
        session.stop()

    def test_position_maps_across_skip(self, qtbot, monkeypatch) -> None:
        """播放头经块记录映射：设备消费跨过跳区时位置直接落在选区后。"""
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(RATE)
        device = FakeDevice(RATE, 2, capacity_frames=8192)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        session.set_skip_range(2000, 3000)
        assert session.play(0)
        session._feed()
        # 消费点取 usec 精确的帧数（48 帧/ms）：设备帧 1992/2016/2544
        # 对应 PCM 1992/3016/3544（跳区 [2000,3000) 不占设备帧）。
        for consumed, expected in ((1992, 1992), (2016, 3016), (2544, 3544)):
            device.consumed_frames = consumed
            assert session.position() == expected
        session.stop()

    def test_one_sample_skip_boundary(self, qtbot, monkeypatch) -> None:
        """1 样本跳区边界：跳区空区间与单样本都正确（无 off-by-one）。"""
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(100)
        device = FakeDevice(RATE, 2, capacity_frames=8192)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        session.set_skip_range(50, 51)
        assert session.play(0)
        while not session._source_exhausted:
            device.consume(64)
            session._feed()
        got = device.frames()
        assert got.shape[0] == 99
        # 第 50 帧（跳过）不存在：索引 49 是原 49 帧，索引 50 直接是
        # 原 51 帧。
        np.testing.assert_allclose(got[49], pcm[:, 49], atol=1e-6)
        np.testing.assert_allclose(got[50], pcm[:, 51], atol=1e-6)
        session.stop()

    def test_stop_sample_limits_bytes_sent_to_device(self, qtbot, monkeypatch) -> None:
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(2000)
        device = FakeDevice(RATE, 2)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        session.set_skip_range(301, 701)
        assert session.play(100, end_sample=1001)
        expected = np.concatenate([pcm[:, 100:301], pcm[:, 701:1001]], axis=1)
        np.testing.assert_array_equal(device.frames(), expected.T)
        device.consume(expected.shape[1])
        session._tick()
        assert not session.is_playing
        assert session.return_sample() == 100


class TestHotSwapAndLoop:
    def test_load_keeps_position_and_playing(self, qtbot, monkeypatch) -> None:
        """A/B 重开处理结果：同格式 load 不停止，续供新数据。"""
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm_a = _ramp(RATE)
        pcm_b = -pcm_a
        device = FakeDevice(RATE, 2, capacity_frames=2048)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm_a, RATE)
        assert session.play(0)
        session._feed()
        device.consume(1024)
        position = session.position()
        assert 1000 <= position <= 1100
        session.load(pcm_b, RATE)  # A/B 切换（同率同声道）
        assert session.is_playing  # 不打断
        assert session.position() == pytest.approx(position, abs=1)
        session._feed()
        after = device.frames()
        # 已入队尾部按原坐标播完，新 PCM 自第 2048 帧接上。
        assert after.shape[0] == 3072
        np.testing.assert_array_equal(after[:2048], pcm_a[:, :2048].T)
        fade_end = 2048 + round(RATE * session._SWAP_FADE_SECONDS)
        ratio = np.linspace(0.0, 1.0, fade_end - 2048, dtype=np.float32)
        blend = pcm_a[:, 2048:fade_end] * (1.0 - ratio) + pcm_b[:, 2048:fade_end] * ratio
        np.testing.assert_array_equal(after[2048:fade_end], blend.T)
        np.testing.assert_array_equal(after[fade_end:], pcm_b[:, fade_end:3072].T)
        for frames in (250, 800):
            device.consume(frames)
            assert session.position() == device.consumed_frames
        session.stop()

    def test_load_preserves_partially_accepted_frame(self, qtbot, monkeypatch) -> None:
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm_a = _ramp(25)
        pcm_b = -pcm_a
        device = FakeDevice(RATE, 2)
        original_write = device.write

        def stalled_write(data: bytes) -> int:
            return original_write(data[:max(0, 5 - len(device.stream))])

        device.write = stalled_write
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm_a, RATE)
        assert session.play(0)
        session.load(pcm_b, RATE)
        assert len(device.stream) == 5
        device.write = original_write
        session._feed()
        blend = pcm_a[:, 1:] * np.linspace(1.0, -1.0, 24, dtype=np.float32)
        expected = np.concatenate([pcm_a[:, :1], blend], axis=1)
        np.testing.assert_allclose(device.frames(), expected.T, atol=1e-7)
        device.consume(13)
        assert session.position() == 13
        session.stop()

    def test_load_shorter_than_position_stops(self, qtbot, monkeypatch) -> None:
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm_a = _ramp(RATE)
        pcm_b = _ramp(100)
        device = FakeDevice(RATE, 2, capacity_frames=2048)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm_a, RATE)
        assert session.play(0)
        session._feed()
        device.consume(1024)
        session.load(pcm_b, RATE)
        assert not session.is_playing
        assert session.return_sample() <= 100

    def test_loop_wraps_without_rebuilding(self, qtbot, monkeypatch) -> None:
        """循环模式：供块层回绕，设备流持续超过单圈长度。"""
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(2000)
        device = FakeDevice(RATE, 2, capacity_frames=4096)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        session.set_loop(True, 0, 2000)
        assert session.play(0)
        for _ in range(12):
            device.consume(1024)
            session._feed()
        assert session.is_playing
        frames = device.frames().shape[0]
        assert frames > 2000  # 越过一圈仍在供块
        # 第二圈内容与第一圈一致（回绕到窗首）。
        got = device.frames()
        np.testing.assert_allclose(got[2000:2500], got[0:500], atol=1e-6)
        session.stop()

    def test_loop_and_skip_wrap_around_skipped_tail(self, qtbot, monkeypatch) -> None:
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(2000)
        device = FakeDevice(RATE, 2, capacity_frames=512)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        session.set_loop(True, 0, 1000)
        session.set_skip_range(500, 1200)
        assert session.play(400)
        np.testing.assert_array_equal(device.frames(), np.concatenate(
            (pcm[:, 400:500], pcm[:, :412]), axis=1).T)
        device.consume(103)
        assert session.position() == 3
        session.stop()

    def test_whole_clip_loop_covers_new_pcm_length(self, qtbot, monkeypatch) -> None:
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        first = _ramp(200)
        longer = _ramp(300)
        device = FakeDevice(RATE, 2, capacity_frames=100)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(first, RATE)
        session.set_loop(True)
        assert session.play(50)
        session.load(longer, RATE)
        for _ in range(3):
            device.consume(100)
            session._feed()
        ratio = np.linspace(0.0, 1.0, 50, dtype=np.float32)
        blend = first[:, 150:] * (1.0 - ratio) + longer[:, 150:200] * ratio
        expected = np.concatenate((first[:, 50:150], blend, longer[:, 200:], longer[:, :150]), axis=1)
        np.testing.assert_array_equal(device.frames(), expected.T)
        session.stop()

    def test_stop_returns_to_play_origin(self, qtbot, monkeypatch) -> None:
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = _ramp(RATE)
        device = FakeDevice(RATE, 2, capacity_frames=2048)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(pcm, RATE)
        session.seek(1200)
        assert session.play()
        session._feed()
        device.consume(100)
        session.stop()
        assert not session.is_playing
        assert session.return_sample() == 1200

    def test_mapped_load_preserves_pause_and_maps_stop_position(self, qtbot, monkeypatch) -> None:
        from PyQt6 import QtMultimedia

        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        first = FakeDevice(RATE, 2, capacity_frames=100)
        second = FakeDevice(RATE, 2, capacity_frames=100)
        devices = iter((first, second))
        monkeypatch.setattr(QtMultimedia, "QAudioSink", lambda _fmt: next(devices))
        session = ClipAuditionSession()
        session.load(_ramp(2000), RATE)
        assert session.play(200)
        first.consume(50)
        session.pause()
        session._tick()
        assert session.is_paused
        assert session.position() == 250
        assert len(first.frames()) == 100
        longer = _ramp(4000)
        session.load(longer, RATE, position_map=lambda index: index * 2)
        assert session.is_playing and session.is_paused
        assert session.position() == 500
        assert second.paused and not second.stream
        session.resume()
        np.testing.assert_array_equal(second.frames(), longer[:, 500:600].T)
        session.stop()
        assert session.return_sample() == 400

    def test_device_loss_releases_sink_and_emits_failure_once(self, qtbot, monkeypatch) -> None:
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        device = FakeDevice(RATE, 2, capacity_frames=100)
        _install(monkeypatch, device)
        session = ClipAuditionSession()
        session.load(_ramp(2000), RATE)
        assert session.play(100)
        device.stopped = True
        with qtbot.waitSignal(session.device_failed, timeout=1000):
            session._tick()
        assert session.return_sample() == 100
        assert not session.is_playing
        assert session._sink is None
        assert not session._timer.isActive()
