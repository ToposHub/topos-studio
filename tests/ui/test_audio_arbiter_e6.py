"""E6：音频共享设备互斥 + 截取作业的预算/取消/有界输出/安全交付。"""
from __future__ import annotations

import os
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.shared.playback import audio_arbiter as arbiter_module  # noqa: E402
from src.shared.playback.audio_arbiter import AudioOutputArbiter  # noqa: E402
from src.shared.playback.clip_export import ClipExportJob  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


@pytest.fixture()
def fresh_arbiter(monkeypatch):
    """隔离共享仲裁者（管线内部走 shared_arbiter() 单例）。"""
    arbiter = AudioOutputArbiter()
    monkeypatch.setattr(arbiter_module, "_SHARED", arbiter)
    yield arbiter


# ---------------------------------------------------------------------------
# 仲裁者：切源/播放互斥（latest-wins + 陈旧释放不误伤）
# ---------------------------------------------------------------------------

def test_arbiter_takeover_and_stale_release():
    arbiter = AudioOutputArbiter()
    takeovers = []
    assert arbiter.acquire("topos-audio:1",
                           lambda: takeovers.append("topos")) is False
    assert arbiter.current_owner() == "topos-audio:1"
    # 新引擎接管：旧拥有者回调退役，拥有权转移
    assert arbiter.acquire("libav-audio:2",
                           lambda: takeovers.append("libav")) is True
    assert takeovers == ["topos"]
    assert arbiter.current_owner() == "libav-audio:2"
    # 陈旧释放（旧 id）：不得清掉新拥有者
    arbiter.release("topos-audio:1")
    assert arbiter.current_owner() == "libav-audio:2"
    # 拥有者释放后再接管：无人可退役
    arbiter.release("libav-audio:2")
    assert arbiter.current_owner() is None
    assert arbiter.acquire("qt-audio:3", lambda: None) is False


def test_arbiter_same_owner_reacquire_does_not_retire_self():
    arbiter = AudioOutputArbiter()
    takeovers = []
    arbiter.acquire("topos-audio:1", lambda: takeovers.append(1))
    arbiter.acquire("topos-audio:1", lambda: takeovers.append(1))
    assert takeovers == []       # 自己重登记不触发退役


def test_topos_pipeline_retires_when_taken_over(qtbot, monkeypatch, fresh_arbiter):
    """[E6] 新拥有者接管 → Topos 音频管线退役（线程自行收尾，不 join 调用方）。"""
    from tests.ui.test_playback_eof_e3 import _FakeSink, _FakeSource
    from src.shared.playback import topos_audio as topos_audio_module
    from src.shared.playback.topos_audio import ToposAudioPipeline

    sink = _FakeSink()
    monkeypatch.setattr(topos_audio_module.QMediaDevices, "defaultAudioOutput",
                        lambda: SimpleNamespace(isNull=lambda: False))
    monkeypatch.setattr(topos_audio_module, "_make_sink",
                        lambda device, fmt: sink)
    source = _FakeSource()
    source.gate.set()   # 不闸：让它自己跑到读尽/退役
    pipeline = ToposAudioPipeline(source)
    assert fresh_arbiter.current_owner() == pipeline._arbiter_owner
    fresh_arbiter.acquire("libav-audio:other", lambda: None)   # 模拟切源
    qtbot.waitUntil(lambda: not pipeline._thread.is_alive(), timeout=5000)
    assert fresh_arbiter.current_owner() == "libav-audio:other"


def test_libav_pipeline_registers_and_retires(fresh_arbiter):
    from src.shared.playback.libav_player import _AudioPipeline

    pipeline = _AudioPipeline("/nonexistent/clip.mov", lambda: 0.0)
    pipeline.start()
    owner = pipeline._arbiter_owner
    assert owner.startswith("libav-audio:")
    assert fresh_arbiter.current_owner() == owner
    fresh_arbiter.acquire("topos-audio:other", lambda: None)
    qtbot_free_wait(lambda: not pipeline._running)
    assert fresh_arbiter.current_owner() == "topos-audio:other"
    pipeline.stop()


def qtbot_free_wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


# ---------------------------------------------------------------------------
# 截取作业：预算 / 取消 / 有界输出 / 安全交付
# ---------------------------------------------------------------------------

@pytest.mark.skipif(FFMPEG is None, reason="本机无 ffmpeg CLI")
def test_clip_export_success_delivers_atomically(tmp_path):
    destination = str(tmp_path / "clip.mov")
    job = ClipExportJob(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=64x36:duration=0.4:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p"],
        destination=destination)
    results = []
    assert job.start(lambda ok, msg: results.append((ok, msg)))
    assert not job.start(lambda *a: None)   # 单作业只跑一次
    qtbot_free_wait(lambda: bool(results))
    ok, message = results[0]
    assert ok and message == destination
    assert os.path.exists(destination)
    assert not os.path.exists(job.tmp_path)  # 无半成品残留


@pytest.mark.skipif(FFMPEG is None, reason="本机无 ffmpeg CLI")
def test_clip_export_cancel_terminates_and_cleans(tmp_path):
    destination = str(tmp_path / "long.mov")
    job = ClipExportJob(
        [FFMPEG, "-y", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", "testsrc2=size=64x36:duration=120:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast"],
        destination=destination)
    results = []
    job.start(lambda ok, msg: results.append((ok, msg)))
    time.sleep(0.4)                 # 让编码跑起来
    job.cancel()
    assert qtbot_free_wait(lambda: bool(results))
    ok, message = results[0]
    assert not ok and "取消" in message
    assert not os.path.exists(destination)
    assert not os.path.exists(job.tmp_path)


@pytest.mark.skipif(FFMPEG is None, reason="本机无 ffmpeg CLI")
def test_clip_export_deadline_is_budgeted(tmp_path):
    destination = str(tmp_path / "slow.mov")
    job = ClipExportJob(
        [FFMPEG, "-y", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", "testsrc2=size=64x36:duration=60:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast"],
        destination=destination, deadline_sec=0.6)
    results = []
    job.start(lambda ok, msg: results.append((ok, msg)))
    assert qtbot_free_wait(lambda: bool(results), timeout=10)
    ok, message = results[0]
    assert not ok and "预算" in message
    assert not os.path.exists(job.tmp_path)


@pytest.mark.skipif(FFMPEG is None, reason="本机无 ffmpeg CLI")
def test_clip_export_failure_reports_and_bounds_capture(tmp_path):
    destination = str(tmp_path / "broken.mov")
    job = ClipExportJob(
        [FFMPEG, "-y", "-i", "/nonexistent/source.mov", "-c", "copy"],
        destination=destination, max_capture=2048)
    results = []
    job.start(lambda ok, msg: results.append((ok, msg)))
    assert qtbot_free_wait(lambda: bool(results), timeout=10)
    ok, message = results[0]
    assert not ok and "失败" in message
    assert len(job.stderr_tail) <= 2048      # 有界：异常输出不吞内存
    assert not os.path.exists(destination)


# ---------------------------------------------------------------------------
# 播放器接线：截取互斥（再点一次=取消）与 Topos 导出换轨
# ---------------------------------------------------------------------------

class _FakeJob:
    def __init__(self) -> None:
        self.cancelled = False
        self.cancels = 0

    def is_alive(self) -> bool:
        return not self.cancelled

    def cancel(self) -> None:
        self.cancelled = True
        self.cancels += 1


def test_topos_export_reclick_cancels_running_job(qtbot):
    from src.shared.playback.topos_player import ToposPlayer

    player = ToposPlayer()
    qtbot.addWidget(player)
    player._session = SimpleNamespace()
    player._current_path = "/m/a.mov"
    fake = _FakeJob()
    player._clip_job = fake
    toasts = []
    player.toast_requested.connect(toasts.append)
    player._export_clip()                # 有活着的作业 → 再点 = 取消
    assert fake.cancels == 1
    assert any("取消" in t for t in toasts)
    player.shutdown if hasattr(player, "shutdown") else None


def test_qt_player_clip_reclick_cancels_running_job(qtbot):
    from src.shared.playback.player_qt import UltimatePlayer

    player = UltimatePlayer()
    qtbot.addWidget(player)
    player.current_path = "/m/b.mov"
    player.inp_start.setText("0")
    player.inp_end.setText("1")
    fake = _FakeJob()
    player._clip_job = fake
    player.execute_clip()                # 有活着的作业 → 再点 = 取消
    assert fake.cancels == 1
