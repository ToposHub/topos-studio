"""E3（FUP-07）：播放完成语义——只有实际呈现完成才发布 EOF。

覆盖三层：
* BasePlayer 子类（libav/topos）的 `_poll_ring` 接线：末帧呈现 + 音频
  排空 → `_on_playback_finished`（图标/音频收尾）恰好一次；
* 音频管线 `drained()`：音源读完 + 待写清空 + sink 缓冲放完（fake sink
  驱动 ToposAudioPipeline 真实线程循环；libav 管线同构字段单测）；
* UltimatePlayer：EndOfMedia（自然播完）与用户停止分离，片尾重播语义。
"""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("PyQt6.QtWidgets")

qtwidgets = pytest.importorskip("PyQt6.QtMultimedia")

from PyQt6.QtCore import QCoreApplication  # noqa: E402
from PyQt6.QtMultimedia import QAudio, QMediaPlayer  # noqa: E402

from src.shared.playback import topos_audio as topos_audio_module  # noqa: E402
from src.shared.playback.libav_player import LibavPlayer, _AudioPipeline  # noqa: E402
from src.shared.playback.player_qt import UltimatePlayer  # noqa: E402
from src.shared.playback.topos_audio import OUT_RATE, ToposAudioPipeline  # noqa: E402
from src.shared.playback.topos_player import ToposPlayer  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _fake_session(frame_count: int = 2, fps: float = 25.0):
    return SimpleNamespace(frame_count=frame_count, fps=fps,
                           duration_sec=frame_count / fps)


# ---------------------------------------------------------------------------
# LibavPlayer：呈现完成接线（无音轨路径）
# ---------------------------------------------------------------------------

def test_libav_poll_ring_publishes_after_presentation(qtbot):
    player = LibavPlayer()
    qtbot.addWidget(player)
    player._session = _fake_session()
    player._audio = None
    player._audio_active = False
    player._playing = True
    player._shown_index = 1            # 末帧（共 2 帧）已实际呈现
    player._decode_exhausted = True
    finished = []
    player._on_playback_finished = lambda: finished.append(True)
    player._poll_ring()
    assert finished == [True]
    assert player._eof_published and not player._playing


def test_libav_poll_ring_holds_while_frames_pending(qtbot):
    player = LibavPlayer()
    qtbot.addWidget(player)
    player._session = _fake_session()
    player._audio = None
    player._audio_active = False
    player._playing = True
    player._shown_index = 0            # 末帧还在队列里没呈现
    player._pending.append((1, object()))
    player._decode_exhausted = True
    finished = []
    player._on_playback_finished = lambda: finished.append(True)
    player._poll_ring()
    assert finished == [] and player._playing   # FUP-07：不许提前宣布播完


# ---------------------------------------------------------------------------
# ToposPlayer：音频主时钟路径 + 严格按序路径
# ---------------------------------------------------------------------------

class _FakeAudio:
    def __init__(self, position, drained):
        self._position = position
        self._drained = drained

    def position(self):
        return self._position

    def drained(self):
        return self._drained

    def pause(self):
        pass


def test_topos_audio_clock_path_waits_for_drain(qtbot):
    player = ToposPlayer()
    qtbot.addWidget(player)
    player._session = _fake_session(frame_count=2, fps=25.0)
    player._playing = True
    player._shown_index = 1
    player._decode_exhausted = True
    finished = []
    player._on_playback_finished = lambda: finished.append(True)

    # 末帧已呈现、但音频缓冲还没放完 → 不发布
    player._audio = _FakeAudio(position=0.08, drained=False)
    player._poll_ring()
    assert finished == [] and player._playing

    # 音频排空 → 发布恰好一次
    player._audio = _FakeAudio(position=0.08, drained=True)
    player._poll_ring()
    player._poll_ring()
    assert finished == [True] and not player._playing


def test_topos_sequential_path_publishes_without_audio(qtbot):
    player = ToposPlayer()
    qtbot.addWidget(player)
    player._session = _fake_session(frame_count=1)
    player._playing = True
    player._audio = None
    player._shown_index = 0
    player._decode_exhausted = True
    player._poll_ring()
    # 真实收尾钩子：图标复位 + 片尾标记（重播语义的依据）
    assert player._eof_published and not player._playing and player._at_eof


def test_topos_toggle_play_at_end_replays_from_zero(qtbot):
    player = ToposPlayer()
    qtbot.addWidget(player)
    player._session = _fake_session()
    player._playing = False
    player._at_eof = True
    seeks = []
    player._request_seek = lambda target: (seeks.append(target),
                                           setattr(player, '_at_eof', False))
    player.toggle_play()
    assert seeks == [0]                # 片尾按播放 → 从头重播


# ---------------------------------------------------------------------------
# 音频管线 drained()：音源读完 + 待写清空 + sink 缓冲放完
# ---------------------------------------------------------------------------

class _FakeIO:
    def __init__(self) -> None:
        self.written = 0

    def write(self, data: bytes) -> int:
        self.written += len(data)
        return len(data)               # 假 sink 即写即耗


class _FakeSink:
    def __init__(self) -> None:
        self._buffer_size = 8192
        self.io = _FakeIO()

    def setBufferSize(self, n: int) -> None:
        self._buffer_size = int(n)

    def bufferSize(self) -> int:
        return self._buffer_size

    def bytesFree(self) -> int:
        return self._buffer_size       # 即写即耗：无积压

    def setVolume(self, v) -> None:
        pass

    def start(self):
        return self.io

    def stop(self) -> None:
        pass

    def suspend(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def state(self):
        return QAudio.State.ActiveState

    def processedUSecs(self) -> int:
        return int(self.io.written / (OUT_RATE * 4) * 1_000_000)


class _FakeSource:
    """读 3 块 int16 立体声后读尽；duration 0.1s（3×4096 样本 @48k）。

    闸门在首次 read：不放开则线程停在预填，排空状态不会被置位——
    测试得以先断言"未排空"，再放行观察真实排空过程。
    """

    duration = 3 * 4096 / OUT_RATE

    def __init__(self) -> None:
        self._reads = 0
        self.gate = threading.Event()

    def read(self, frames: int):
        import numpy as np
        self.gate.wait(5)
        if self._reads >= 3:
            return np.zeros(0, np.int16)
        self._reads += 1
        return np.zeros(frames, np.int16)

    def seek(self, seconds: float) -> None:
        self._reads = 0                # 回跳后又有数据（排空状态重置）

    def close(self) -> None:
        pass


def test_topos_audio_pipeline_drains_after_source_end(qtbot, monkeypatch):
    sink = _FakeSink()
    monkeypatch.setattr(topos_audio_module.QMediaDevices, "defaultAudioOutput",
                        lambda: SimpleNamespace(isNull=lambda: False))
    monkeypatch.setattr(topos_audio_module, "_make_sink",
                        lambda device, fmt: sink)
    source = _FakeSource()
    pipeline = ToposAudioPipeline(source)
    try:
        assert not pipeline.drained()  # 音源闸着：未读尽必不排空
        source.gate.set()
        qtbot.waitUntil(pipeline.drained, timeout=8000)
        # 排空 = 三者同时：音源读完 + 待写 0 + sink 积压 0
        stats = (pipeline._source_done, pipeline._pending_len,
                 pipeline._sink_backlog)
        assert stats == (True, 0, 0)
    finally:
        source.gate.set()
        pipeline.stop()


def test_libav_audio_pipeline_drained_reflects_tracked_state():
    """libav 管线同构：drained 由音频线程维护的三项状态决定（字段单测；
    循环接线与 topos 管线对称，见上）。"""
    pipeline = _AudioPipeline.__new__(_AudioPipeline)  # 不启线程，只验判定
    import threading as _t
    pipeline._slock = _t.Lock()
    pipeline._source_eof = False
    pipeline._pend_len = 1024
    pipeline._sink_backlog = 2048
    assert not pipeline.drained()
    pipeline._source_eof = True        # 音源读完，但待写与积压还在
    assert not pipeline.drained()
    pipeline._pend_len = 0
    assert not pipeline.drained()      # sink 缓冲还没放完
    pipeline._sink_backlog = 0
    assert pipeline.drained()


# ---------------------------------------------------------------------------
# UltimatePlayer：EndOfMedia（自然播完）与用户停止分离
# ---------------------------------------------------------------------------

def test_ultimate_end_of_media_sets_replay_flag(qtbot, monkeypatch):
    player = UltimatePlayer()
    qtbot.addWidget(player)
    player._on_playback_state.__self__  # 类型自检（真实槽）
    # 用户停止（无 EndOfMedia）：不置片尾标记
    player._on_playback_state(QMediaPlayer.PlaybackState.StoppedState)
    assert not player._at_eof
    seeks, plays = [], []
    monkeypatch.setattr(player.mediaplayer, "setPosition", seeks.append)
    monkeypatch.setattr(player.mediaplayer, "play", lambda: plays.append(True))
    player.is_playing = False
    player.toggle_play()
    assert seeks == [] and plays == [True]   # 中途续播：原地 play，不回零

    # 自然播完（EndOfMedia）：片尾标记 → 重播从零
    player.is_playing = False
    player._on_media_status(QMediaPlayer.MediaStatus.EndOfMedia)
    assert player._at_eof
    player.toggle_play()
    assert seeks == [0] and len(plays) == 2
    assert not player._at_eof


def test_ultimate_slider_seek_clears_replay_flag(qtbot):
    player = UltimatePlayer()
    qtbot.addWidget(player)
    player._at_eof = True
    player.mediaplayer.duration = lambda: 1000  # 供换算
    player.slider.setValue(500)
    player.on_slider_released()
    assert not player._at_eof      # 片尾回跳后继续播，不再触发从头重播
