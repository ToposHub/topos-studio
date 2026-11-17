"""NR-05 回归：播放语义与 GPU 边界（FR-18~21）。

来源：docs/validation/files_replacement_audit_20261116/repro/playback_semantics.py
的隔离复现转 pytest。全部使用假会话/合成帧，不依赖 GL 与真实媒体文件；
真机/真 GPU 验证仍按计划单独执行（V 项）。
"""
from __future__ import annotations

import threading
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import patch

import pytest

av = pytest.importorskip("av")

from src.shared.playback.base_player import BasePlayer  # noqa: E402
from src.shared.playback.libav_player import _LibavSession, _fit_aspect_size  # noqa: E402
from src.shared.playback.topos_player import ToposMovieSession  # noqa: E402


# ---------------------------------------------------------------------------
# FR-18 + E3/FUP-07：末帧推进、producer 耗尽与"实际呈现完成才发 EOF"
# ---------------------------------------------------------------------------

def _run_decode_loop(frame_count: int, max_decodes: int = 64):
    """后台线程驱动 BasePlayer._decode_loop，收集解码序号与耗尽/收尾状态。

    [E3] 哨兵分支只标记 ``_decode_exhausted`` 并泊车——线程不再自行退出，
    所以循环必须放后台线程，由本函数观测到耗尽后停机。
    """
    import time as _time

    targets, finishes = [], []
    state = SimpleNamespace(
        _cond=threading.Condition(), _ring_lock=threading.Lock(),
        _ring_space=None, _running=True, _playing=True, _seek_target=None,
        _next_index=0, _ring=[], RING_SIZE=4096,   # 测试无 UI 消费者，避免背压卡死
        _decode_exhausted=False, _eof_published=False,
        _on_seek_jump=lambda: None,
        _on_playback_finished=lambda: finishes.append(True),
        _audio_drained=lambda: True,
        _on_decode_error=lambda: None, _on_decode_stop=lambda: None,
    )
    state._ring_space = threading.Condition(state._ring_lock)

    def decode(session, index):
        targets.append(index)
        if len(targets) >= max_decodes:
            state._running = False
        return object()

    state._decode_frame = decode
    thread = threading.Thread(
        target=BasePlayer._decode_loop,
        args=(state, SimpleNamespace(frame_count=frame_count)))
    thread.start()
    deadline = _time.monotonic() + 5
    while not state._decode_exhausted and _time.monotonic() < deadline:
        _time.sleep(0.01)
    state._running = False
    with state._cond:
        state._cond.notify()
    thread.join(timeout=5)
    return targets, finishes, state


def test_fr18_two_frame_session_reads_each_frame_once_then_exhausts():
    targets, finishes, state = _run_decode_loop(frame_count=2)
    assert targets == [0, 1]          # 不再是 0,1,1,1…
    assert state._decode_exhausted     # producer 耗尽（解码侧事实）
    assert state._playing              # [E3/FUP-07] 耗尽不再立即停播（帧还在队列）
    assert finishes == []              # EOF 不由 producer 侧发布


def test_fr18_single_frame_session_exhausts_without_pause():
    targets, _, state = _run_decode_loop(frame_count=1)
    assert targets == [0]
    assert state._decode_exhausted and state._playing


def test_fr18_empty_session_exhausts_without_decode():
    targets, _, state = _run_decode_loop(frame_count=0)
    assert targets == []
    assert state._decode_exhausted


def test_fr18_long_session_last_frame_then_exhausts():
    frame_count = 24
    targets, _, state = _run_decode_loop(frame_count=frame_count)
    assert targets == list(range(frame_count))
    assert state._decode_exhausted


def _presentation_state(frame_count: int, *, shown=-1, pending=(), audio=True):
    """呈现完成判定所需的假状态（_maybe_publish_eof/_request_seek 只读这些字段）。"""
    state = SimpleNamespace(
        _cond=threading.Condition(),
        _ring_lock=threading.Lock(),
        _session=SimpleNamespace(frame_count=frame_count),
        _shown_index=shown,
        _ring=[],
        _pending=list(pending),
        _decode_exhausted=True,
        _eof_published=False,
        _playing=True,
    )
    state._audio_drained = (lambda: True) if audio else (lambda: False)
    state._on_playback_finished = lambda: setattr(state, 'finished', True)
    state.finished = False
    return state


def test_e3_eof_published_only_after_presentation_completes():
    # 末帧未呈现 → 不发布（FUP-07：2 帧会话 shown=-1、队列 [0,1] 的实况）
    state = _presentation_state(2, shown=-1, pending=[(0, object()), (1, object())])
    BasePlayer._maybe_publish_eof(state)
    assert not state.finished and state._playing

    # 队列还有未呈现帧 → 不发布
    state = _presentation_state(2, shown=0, pending=[(1, object())])
    BasePlayer._maybe_publish_eof(state)
    assert not state.finished

    # 末帧已呈现但音频未排空 → 不发布（有声素材的收尾要等声音放完）
    state = _presentation_state(2, shown=1, audio=False)
    BasePlayer._maybe_publish_eof(state)
    assert not state.finished and state._playing

    # 全部成立 → 发布恰好一次：停播 + GUI 收尾钩子
    state = _presentation_state(2, shown=1)
    BasePlayer._maybe_publish_eof(state)
    BasePlayer._maybe_publish_eof(state)  # 第二次不得重复发布
    assert state.finished and not state._playing and state._eof_published


def test_e3_empty_session_publishes_without_frames():
    # 0 帧会话：shown 初值 -1 == frame_count-1 == -1，无帧可呈现即完成
    state = _presentation_state(0, shown=-1)
    BasePlayer._maybe_publish_eof(state)
    assert state.finished


def test_e3_seek_rearms_eof_state():
    state = _presentation_state(2, shown=1)
    BasePlayer._maybe_publish_eof(state)
    assert state._eof_published and state._decode_exhausted
    state._on_seek_requested = lambda target: None
    BasePlayer._request_seek(state, 0)  # 片尾重播 / 回跳
    assert not state._eof_published and not state._decode_exhausted
    assert state._shown_index == -1     # 显示基准回到目标前一帧


# ---------------------------------------------------------------------------
# FR-19：Topos 时基（timescale ≠ fps）
# ---------------------------------------------------------------------------

class _FakeMovie:
    """frames: 帧数；ticks_per_frame: 每帧 tick 数；timescale: MOV media timescale。"""

    def __init__(self, *, sample_count, ticks_per_frame, timescale,
                 has_pts=True, width=16, height=8):
        self._sample_count = sample_count
        self._ticks = ticks_per_frame
        self._timescale = timescale
        self._has_pts = has_pts
        self._w, self._h = width, height

    def info(self):
        return SimpleNamespace(
            width=self._w, height=self._h, sample_count=self._sample_count,
            timescale=self._timescale, duration_ticks=self._sample_count * self._ticks,
            pixel_format=0, bit_depth=10, alpha_mode=0)

    def audio_track_count(self):
        return 0

    def pts(self, index):
        if not self._has_pts:
            raise AttributeError('no pts')
        return index * self._ticks, self._ticks

    def close(self):
        pass


class _FakeCodec:
    def __init__(self):
        pass


class _FakeDecoder:
    def __init__(self, *args, **kwargs):
        pass

    def make_views(self, arrays):
        return arrays

    def close(self):
        pass


def _session_with_movie(movie):
    movie_cls = lambda codec, path: movie
    with patch('src.shared.playback.topos_player._load_binding',
               return_value=(_FakeCodec, movie_cls, _FakeDecoder)):
        session = ToposMovieSession('/not-opened/audit.mov')
    return session


def _close_session(session):
    try:
        session.close()
    except Exception:
        pass


def test_fr19_timescale_is_not_fps():
    """timescale=24000、每帧 1000 ticks、240 帧 → 24fps / 10 秒，而非 24000fps。"""
    movie = _FakeMovie(sample_count=240, ticks_per_frame=1000, timescale=24000)
    session = _session_with_movie(movie)
    try:
        assert session.fps == pytest.approx(24.0)
        assert session.duration_sec == pytest.approx(10.0)
    finally:
        _close_session(session)


def test_fr19_ntsc_drop_frame_rate():
    """timescale=30000、每帧 1001 ticks → 30000/1001 ≈ 29.97fps。"""
    movie = _FakeMovie(sample_count=30000, ticks_per_frame=1001, timescale=30000)
    session = _session_with_movie(movie)
    try:
        assert session.fps == pytest.approx(30000 / 1001, rel=1e-6)
        assert session.duration_sec == pytest.approx(1001.0, rel=1e-6)
    finally:
        _close_session(session)


def test_fr19_missing_pts_falls_back_to_sane_default():
    movie = _FakeMovie(sample_count=240, ticks_per_frame=1000, timescale=24000,
                       has_pts=False)
    session = _session_with_movie(movie)
    try:
        # 兜底不得是 timescale（24000）；退回 24fps 安全默认
        assert session.fps == 24.0
    finally:
        _close_session(session)


# ---------------------------------------------------------------------------
# FR-20：等比解码尺寸（不拉伸、保留几何）
# ---------------------------------------------------------------------------

def _yuv_session_with_frame(frame):
    session = _LibavSession.__new__(_LibavSession)
    session._lock = threading.Lock()
    session._iter = iter([frame])
    session._next_idx = 0
    session.fps = 24
    session.sar = None
    return session


def test_fr20_wide_video_in_square_box_keeps_aspect():
    frame = av.VideoFrame(160, 80, 'yuv420p')
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    session = _yuv_session_with_frame(frame)
    result = session.frame_yuv(0, size=(400, 400))
    assert (result.width, result.height) == (400, 200)   # 2:1，不再是 400×400
    assert result.width / result.height == pytest.approx(2.0)


def test_fr20_portrait_video_in_square_box_keeps_aspect():
    frame = av.VideoFrame(80, 160, 'yuv420p')
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    session = _yuv_session_with_frame(frame)
    result = session.frame_yuv(0, size=(400, 400))
    assert (result.width, result.height) == (200, 400)


def test_fr20_small_source_upscales_to_fit_box_proportionally():
    """小源在远大于它的框内等比放大（解码尺寸≈显示尺寸，保持几何）。"""
    frame = av.VideoFrame(160, 80, 'yuv420p')
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    session = _yuv_session_with_frame(frame)
    result = session.frame_yuv(0, size=(1920, 1080))
    assert (result.width, result.height) == (1920, 960)  # 2:1 保持
    assert result.width / result.height == pytest.approx(2.0)


def test_fr20_source_sar_widens_display():
    frame = av.VideoFrame(160, 80, 'yuv420p')
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    session = _yuv_session_with_frame(frame)
    session.sar = Fraction(2, 1)                          # 显示 320×80 → 4:1
    result = session.frame_yuv(0, size=(400, 400))
    assert (result.width, result.height) == (400, 100)
    assert result.width / result.height == pytest.approx(4.0)


def test_fr20_odd_sizes_round_to_even():
    frame = av.VideoFrame(161, 81, 'yuv420p')
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    session = _yuv_session_with_frame(frame)
    result = session.frame_yuv(0, size=(400, 400))
    assert result.width % 2 == 0 and result.height % 2 == 0
    assert result.width / result.height == pytest.approx(161 / 81, rel=0.03)


def test_fr20_fit_size_pure_function_matrix():
    frame = SimpleNamespace(width=160, height=80)
    assert _fit_aspect_size(frame, 400, 400, None) == (400, 200)
    assert _fit_aspect_size(frame, 400, 200, None) == (400, 200)
    assert _fit_aspect_size(frame, 100, 400, None) == (100, 50)
    assert _fit_aspect_size(frame, 1920, 1080, None) == (1920, 960)  # 等比放大
    assert _fit_aspect_size(frame, 400, 400, Fraction(2, 1)) == (400, 100)
    assert _fit_aspect_size(frame, 0, 0, None)[0] >= 2               # 退化保护


@pytest.mark.parametrize("box,expected", [
    ((320, 240), (320, 160)), ((240, 320), (240, 120)),
    ((400, 400), (400, 200)), (None, (160, 80)),
])
def test_topos_rgb_fallback_keeps_source_aspect(box, expected):
    import numpy as np
    session = ToposMovieSession.__new__(ToposMovieSession)
    session._av = av
    session.width, session.height = 320, 160
    session._out_w, session._out_h = 160, 80  # auto decode may use fewer pixels
    session.vfmt = "yuv444p10le"
    session.info = SimpleNamespace(color_matrix=1, color_range=0)
    session._y = np.zeros((80, 160), dtype="<u2")
    session._u = np.full((80, 160), 512, dtype="<u2")
    session._v = np.full((80, 160), 512, dtype="<u2")
    session._decode_planes_locked = lambda index, size: None
    rgb = session._frame_rgb_locked(0, box)
    assert (rgb.shape[1], rgb.shape[0]) == expected


# ---------------------------------------------------------------------------
# FR-21：画布尺寸快照（工作线程不触碰 QWidget API）
# ---------------------------------------------------------------------------

def test_fr21_display_size_reads_gui_thread_snapshot(qtbot, monkeypatch):
    """display_size 必须只读快照：工作线程调用时不得触 QWidget 实时 API。"""
    pytest.importorskip("PyQt6.QtOpenGLWidgets")
    from src.shared.playback.gl_video_canvas import GLVideoCanvas

    canvas = GLVideoCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(640, 360)
    canvas.refresh_size_cache()          # GUI 线程建立快照
    snapshot = canvas.display_size()
    assert snapshot is not None and snapshot[0] >= 640

    # 之后即使 QWidget 实时 API 全部失效，工作线程读取仍安全
    def _explode(*args, **kwargs):
        raise AssertionError('worker thread touched QWidget API')

    monkeypatch.setattr(canvas, 'width', _explode)
    monkeypatch.setattr(canvas, 'height', _explode)
    monkeypatch.setattr(canvas, 'devicePixelRatioF', _explode)

    result = {}
    error = []

    def worker():
        try:
            result['size'] = canvas.display_size()
        except Exception as exc:  # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(3)
    assert not error
    assert result['size'] == snapshot


def test_fr21_refresh_size_cache_ignores_invalid_dims(qtbot):
    pytest.importorskip("PyQt6.QtOpenGLWidgets")
    from src.shared.playback.gl_video_canvas import GLVideoCanvas

    canvas = GLVideoCanvas()
    qtbot.addWidget(canvas)
    canvas.refresh_size_cache()          # 未布局时 0×0 → None，不产出假尺寸
    assert canvas.display_size() in (None,) or canvas.display_size()[0] > 0
