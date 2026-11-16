"""M2：Topos 音频源与 QAudioSink 管线的纯 Python 回归。

这些测试不打开 native codec，也不依赖声卡：
- LPCM 分块/位深/声道/重采样用 fake movie 会话验证；
- 音频线程用 fake QAudioSink 验证写入门控、暂停、seek 和位置时钟。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtMultimedia import QAudio

from src.shared.playback import topos_audio
from src.shared.playback.topos_audio import (
    OUT_CHANNELS,
    OUT_RATE,
    ToposAudioPipeline,
    _AacSource,
    _LpcmSource,
    make_audio_source,
)


def _info(*, codec=1, rate=48000, channels=2, bits=16, fmt=0,
          count=0, chunks=0):
    return SimpleNamespace(
        codec=codec, sample_rate=rate, channel_count=channels,
        bits_per_sample=bits, sample_format=fmt, sample_count=count,
        chunk_count=chunks, has_audio=codec != 0,
    )


class _FakeSession:
    def __init__(self, info, chunks):
        self.audio_info = info
        self._chunks = chunks

    def audio_chunk_size(self, index):
        return len(self._chunks[index])

    def read_audio_chunk(self, index):
        return self._chunks[index]


def _pcm16_stereo(frames):
    return np.asarray(frames, dtype='>i2').tobytes()


def test_mono_lpcm_is_duplicated_to_stereo():
    frames = np.array([[1000], [-1000], [2000], [-2000]], dtype='>i2')
    info = _info(channels=1, count=4, chunks=1)
    source = _LpcmSource(_FakeSession(info, [frames.tobytes()]))
    out = source.read(4)
    assert out.shape == (4, 2)
    assert out[:, 0].tolist() == [1000, -1000, 2000, -2000]
    assert out[:, 1].tolist() == [1000, -1000, 2000, -2000]


def test_lpcm_24bit_big_endian_and_seek():
    values = [0, 0x100000, -0x100000, 0x7fffff, -0x800000]
    frames = []
    for value in values:
        packed = int(value) & 0xFFFFFF
        frames.append(bytes([(packed >> 16) & 0xFF,
                             (packed >> 8) & 0xFF, packed & 0xFF]) * 2)
    info = _info(bits=24, count=len(values), chunks=2)
    session = _FakeSession(info, [b''.join(frames[:3]), b''.join(frames[3:])])
    source = _LpcmSource(session)
    source.seek(1 / 48000)
    out = source.read(3)
    assert out.shape == (3, 2)
    # 24-bit 0x100000 = 0.125；-0x100000 = -0.125
    assert out[0, 0] == 4096
    assert out[1, 0] == -4096
    assert out[2, 0] == 32767


def test_lpcm_resamples_to_48k():
    rate = 44100
    frames = np.full((rate, 2), 1000, dtype='>i2')
    info = _info(rate=rate, count=rate, chunks=1)
    source = _LpcmSource(_FakeSession(info, [frames.tobytes()]))
    out = source.read(OUT_RATE)
    assert out.shape == (OUT_RATE, OUT_CHANNELS)
    assert out[0, 0] == 1000
    assert out[-1, 0] == 1000


def test_make_audio_source_dispatch(monkeypatch):
    monkeypatch.setattr(topos_audio, '_ffmpeg_path', lambda: '/fake/ffmpeg')
    lpcm = make_audio_source(
        _FakeSession(_info(), []), 'x.topos')
    assert isinstance(lpcm, _LpcmSource)
    aac = make_audio_source(
        _FakeSession(_info(codec=2, bits=0), []), 'x.topos')
    assert isinstance(aac, _AacSource)
    assert make_audio_source(_FakeSession(_info(codec=0), []), 'x.topos') is None


def test_aac_source_requires_ffmpeg(monkeypatch):
    monkeypatch.setattr(topos_audio, '_ffmpeg_path', lambda: None)
    assert make_audio_source(
        _FakeSession(_info(codec=2, bits=0), []), 'x.topos') is None


class _FakeIO:
    def __init__(self, sink):
        self._sink = sink

    def write(self, data):
        self._sink.written += len(data)
        return len(data)


class _FakeSink:
    def __init__(self, device, fmt):
        self.volume = 1.0
        self.written = 0
        self._started = time.monotonic()
        self._suspended = False
        self._state = QAudio.State.ActiveState

    def setBufferSize(self, size):
        self.buffer_size = size

    def setVolume(self, volume):
        self.volume = volume

    def start(self):
        self._started = time.monotonic()
        self._state = QAudio.State.ActiveState
        return _FakeIO(self)

    def stop(self):
        self._state = QAudio.State.StoppedState

    def suspend(self):
        self._suspended = True
        self._state = QAudio.State.SuspendedState

    def resume(self):
        self._suspended = False
        self._state = QAudio.State.ActiveState

    def state(self):
        return self._state

    def processedUSecs(self):
        if self._suspended:
            return getattr(self, '_frozen', 0)
        elapsed = int((time.monotonic() - self._started) * 1_000_000)
        self._frozen = elapsed
        return elapsed

    def bytesFree(self):
        return 10_000_000


class _FakeDevice:
    def isNull(self):
        return False


class _FakeSource:
    duration = 1.0

    def __init__(self):
        self.pos = 0
        self.closed = False

    def seek(self, seconds):
        self.pos = int(max(0.0, seconds) * OUT_RATE)

    def read(self, frames):
        remain = max(0, OUT_RATE - self.pos)
        take = min(int(frames), remain)
        if take <= 0:
            return np.empty((0, OUT_CHANNELS), dtype='<i2')
        out = np.full((take, OUT_CHANNELS), 1234, dtype='<i2')
        self.pos += take
        return out

    def close(self):
        self.closed = True


def test_audio_pipeline_writes_pauses_and_seeks(monkeypatch):
    monkeypatch.setattr(topos_audio, '_make_sink', lambda device, fmt: _FakeSink(device, fmt))
    monkeypatch.setattr(topos_audio.QMediaDevices, 'defaultAudioOutput',
                        staticmethod(lambda: _FakeDevice()))
    source = _FakeSource()
    pipeline = ToposAudioPipeline(source)
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and pipeline.position() < 0.05:
            time.sleep(0.01)
        assert pipeline.position() >= 0.03
        pipeline.pause()
        paused_at = pipeline.position()
        time.sleep(0.1)
        assert pipeline.position() == pytest.approx(paused_at, abs=0.02)
        pipeline.seek(0.5)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and pipeline.position() < 0.55:
            time.sleep(0.01)
        assert pipeline.position() >= 0.5
    finally:
        pipeline.stop()
    assert source.closed


class _FakeVideoInfo:
    bit_depth = 10
    pixel_format = 0
    color_matrix = 1
    color_range = 1


class _FakeAudioSession:
    path = 'fake.topos'
    width = 1920
    height = 1080
    fps = 25.0
    frame_count = 25

    def __init__(self, has_audio):
        self.info = _FakeVideoInfo()
        self.audio_info = _info(count=48000, chunks=1) if has_audio else None


def test_player_hides_volume_without_audio(qtbot):
    from src.shared.playback.topos_player import ToposPlayer
    player = ToposPlayer()
    qtbot.addWidget(player)
    player._configure_session(_FakeAudioSession(False))
    assert player._audio_source is None
    assert player.slider_vol.isHidden()
    assert player.lbl_vol.isHidden()


def test_player_prepares_audio_source_and_starts_pipeline(qtbot, monkeypatch):
    import src.shared.playback.topos_audio as audio_mod
    from src.shared.playback.topos_player import ToposPlayer

    started = []

    class _Pipeline:
        def __init__(self, source, parent=None):
            self.source = source
            self.volume = None
            started.append(self)

        class _Signal:
            def connect(self, *_args):
                pass

        failed = _Signal()

        def set_volume(self, value):
            self.volume = value

        def stop(self):
            self.source.close()

    monkeypatch.setattr(audio_mod, 'ToposAudioPipeline', _Pipeline)
    player = ToposPlayer()
    qtbot.addWidget(player)
    session = _FakeAudioSession(True)
    player._configure_session(session)
    assert player._audio_source is not None
    assert not player.slider_vol.isHidden()
    player._after_decoder_started()
    assert started and started[0].volume == pytest.approx(
        player.slider_vol.value() / 100.0)
    player._teardown_output()
    assert player._audio is None
    assert player._audio_source is None
