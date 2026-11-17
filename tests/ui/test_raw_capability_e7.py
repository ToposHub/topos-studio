"""E7：RAW 能力按真实后端报告 + 打开/探测有界——不无限等待不黑屏。

FUP-07 实测：8K ProRes RAW HQ 的 MOV，metadata 打开 ~0.55s，首帧 RGB
35s+ 未返回，矩阵 40s deadline 超时。本批口径：按 codec tag 在**打开
时**拒绝无解码后端的 RAW 家族；av.open / 首帧探测全部有界。
"""
from __future__ import annotations

import shutil
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("av")

from src.shared.playback import libav_player as lp  # noqa: E402
from src.shared.playback.libav_player import (  # noqa: E402
    UndecodableRawError, _LibavSession, raw_capability,
)

FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# 能力矩阵：按 codec tag / 名识别（常规 ProRes 不被连坐）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tag,name,decodable", [
    ('aprh', '', False),            # ProRes RAW 8K HQ 相机
    ('aprn', '', False),            # ProRes RAW 家族
    ('BRAW', '', False),            # Blackmagic RAW（大小写不敏感）
    ('R3D ', '', False),            # RED（含尾随空格的四字符码）
    ('apco', '', True),             # 常规 ProRes 422 Proxy——可软解
    ('apcs', '', True),
    ('apcn', '', True),
    ('apch', 'prores', True),       # 常规 ProRes 422 HQ——FUP-07 已实测通过
    ('ap4h', '', True),             # ProRes 4444
    ('', 'prores', True),
    ('', 'dnxhd', True),
    ('', 'prores_raw', False),      # 按名兜底（tag 缺失的变体）
])
def test_raw_capability_matrix(tag, name, decodable):
    ok, reason = raw_capability(tag, name)
    assert ok is decodable
    if not decodable:
        assert '无解码后端' in reason   # 明确诊断：不冒充可播
    else:
        assert reason == ''


def test_raw_rejection_reason_mentions_no_silent_fallback():
    _, reason = raw_capability('aprh', '')
    assert 'ProRes RAW' in reason and 'SDK' in reason


# ---------------------------------------------------------------------------
# 会话层：打开即拒绝（不进解码）、常规 ProRes 照常
# ---------------------------------------------------------------------------

@pytest.fixture()
def prores_clip(tmp_path):
    if FFMPEG is None:
        pytest.skip("本机无 ffmpeg CLI")
    import subprocess

    clip = tmp_path / "prores.mov"
    subprocess.run(
        [FFMPEG, '-y', '-loglevel', 'error', '-f', 'lavfi',
         '-i', 'testsrc2=size=128x72:duration=0.4:rate=10',
         '-c:v', 'prores_ks', '-profile:v', '2', str(clip)],
        check=True)
    return clip


def test_session_rejects_raw_before_decoding(prores_clip, monkeypatch):
    """带 RAW tag 的会话在打开阶段拒绝——绝不解码、绝不黑屏假成功。"""
    decode_calls = []

    def _no_decode(*args, **kwargs):
        decode_calls.append(True)
        raise AssertionError("RAW 素材不得进入解码路径")

    monkeypatch.setattr(lp, 'raw_capability',
                        lambda tag, name: (False, 'ProRes RAW（aprh）'
                                           '无解码后端：SDK 未接入，不支持预览'))
    with pytest.raises(UndecodableRawError, match='无解码后端'):
        _LibavSession(str(prores_clip))
    assert decode_calls == []


def test_regular_prores_session_opens_and_decodes(prores_clip):
    session = _LibavSession(str(prores_clip))
    try:
        assert session.codec_name == 'prores'
        rgb = session.frame_rgb(0)
        assert rgb.shape[0] > 0 and rgb.shape[2] == 3
    finally:
        session.close()


def test_play_failure_carries_raw_reason(prores_clip, monkeypatch, qtbot):
    """播放失败经 play_failed 如实上抛（不静默、不回退系统播放器）。"""
    from src.shared.playback.libav_player import LibavPlayer

    monkeypatch.setattr(lp, 'raw_capability',
                        lambda tag, name: (False, 'BRAW 无解码后端：'
                                           'SDK 未接入，不支持预览'))
    player = LibavPlayer()
    qtbot.addWidget(player)
    failures = []
    player.play_failed.connect(lambda reason: failures.append(reason))
    with qtbot.waitSignal(player.play_failed, timeout=5000):
        player.play_video(str(prores_clip))
    assert failures and '无解码后端' in failures[0]
    assert player.isHidden()


# ---------------------------------------------------------------------------
# 有界打开 / 有界旋转探测：挂起源按超时收尾，不无限等待
# ---------------------------------------------------------------------------

def test_av_open_timeout_fails_fast(monkeypatch):
    def _hanging_open(path):
        time.sleep(30)
        return object()

    monkeypatch.setattr(lp.av, "open", _hanging_open)
    start = time.monotonic()
    with pytest.raises(TimeoutError, match='打开超时'):
        _LibavSession("/hanging/source.mov")
    assert time.monotonic() - start < 7   # 5s 默认预算内收尾（含余量）


def test_mezzanine_route_bounded_on_hanging_open(monkeypatch):
    def _hanging_open(path, timeout=5.0):
        raise TimeoutError('打开超时（>5s）')

    monkeypatch.setattr(lp, "_av_open_with_timeout", _hanging_open)
    lp._mezzanine_cache.clear()
    # 路由询问不挂：超时按"非 mezzanine"收尾 → 走 Qt 引擎（其自身有
    # 1.5s 开播看门狗报错）
    start = time.monotonic()
    assert lp.is_mezzanine("/hanging/source.mov") is False
    assert time.monotonic() - start < 1


def test_rotation_probe_timeout_returns_unrotated():
    session = _LibavSession.__new__(_LibavSession)  # 只测探测路径
    release = __import__("threading").Event()

    class _HangingContainer:
        def decode(self, *args, **kwargs):
            while True:   # 首帧永不返回（8K RAW 实测形态）
                release.wait(30)
                yield None

    session._container = _HangingContainer()
    start = time.monotonic()
    assert session._probe_rotation(timeout=0.3) == 0
    assert time.monotonic() - start < 2
    release.set()
