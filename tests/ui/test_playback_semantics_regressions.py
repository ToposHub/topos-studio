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
from src.shared.playback.topos_player import ToposMovieSession, _DecodeBuffer  # noqa: E402


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
    session = SimpleNamespace(frame_count=frame_count)
    state = SimpleNamespace(
        _cond=threading.Condition(), _ring_lock=threading.Lock(),
        _ring_space=None, _running=True, _playing=True, _seek_target=None,
        _next_index=0, _decode_floor=0, _session=session, _ring=[], RING_SIZE=4096,   # 测试无 UI 消费者，避免背压卡死
        _decode_exhausted=False, _eof_published=False,
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
        args=(state, session))
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


def _presentation_state(frame_count: int, *, shown=-1, frames=(), audio=True):
    """呈现完成判定所需的假状态（_maybe_publish_eof/_request_seek 只读这些字段）。"""
    state = SimpleNamespace(
        _cond=threading.Condition(),
        _ring_lock=threading.Lock(),
        _session=SimpleNamespace(frame_count=frame_count),
        _shown_index=shown,
        _ring=list(frames),
        _decode_exhausted=True,
        _eof_published=False,
        _playing=True,
    )
    state._audio_drained = (lambda: True) if audio else (lambda: False)
    state._on_playback_finished = lambda: setattr(state, 'finished', True)
    state._ring_space = threading.Condition(state._ring_lock)
    state.finished = False
    return state


def test_e3_eof_published_only_after_presentation_completes():
    # 末帧未呈现 → 不发布（FUP-07：2 帧会话 shown=-1、队列 [0,1] 的实况）
    state = _presentation_state(2, shown=-1, frames=[(0, object()), (1, object())])
    BasePlayer._maybe_publish_eof(state)
    assert not state.finished and state._playing

    # 队列还有未呈现帧 → 不发布
    state = _presentation_state(2, shown=0, frames=[(1, object())])
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
            pixel_format=0, bit_depth=10, alpha_mode=0, color_matrix=1,
            color_range=0, chroma_siting=0)

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


def _session_with_movie(movie: _FakeMovie, *, prefetch: bool = False) -> ToposMovieSession:
    movie_cls = lambda codec, path: movie
    with patch('src.shared.playback.topos_player._load_binding',
               return_value=(_FakeCodec, movie_cls, _FakeDecoder)):
        session = ToposMovieSession('/not-opened/audit.mov', prefetch=prefetch)
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


def test_topos_prefetch_overlaps_output_and_discards_stale_seek(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('TOPOS_DISABLE_PREFETCH', raising=False)
    session = _session_with_movie(_FakeMovie(sample_count=6, ticks_per_frame=1, timescale=24),
                                  prefetch=True)
    prefetched = threading.Event()
    convert_started = threading.Event()
    decoded = []
    av_module = session._av

    def allocate_frame(*args: object, **kwargs: object) -> av.VideoFrame:
        convert_started.set()
        assert prefetched.wait(timeout=5)
        return av_module.VideoFrame(*args, **kwargs)

    def decode(index: int, size: tuple[int, int] | None, buffer: _DecodeBuffer) -> _DecodeBuffer:
        decoded.append((index, size))
        if index == 1:
            assert convert_started.wait(timeout=5)
            prefetched.set()
        buffer.ensure(session._decoder, ((16, 8), (8, 8), (8, 8)))
        buffer.planes[0].fill(64 + index * 100)
        for plane in buffer.planes[1:]:
            plane.fill(512)
        return buffer

    monkeypatch.setattr(session, '_av', SimpleNamespace(VideoFrame=allocate_frame))
    monkeypatch.setattr(session, '_decode_into', decode)
    try:
        frame_0 = session.frame_rgb(0, size=(16, 8))
        frame_1 = session.frame_yuv(1, size=(16, 8))
        assert frame_1.y[0, 0] == 164
        sought = session.frame_yuv(5, size=(16, 8))
        snapshot = session.frame_rgb(5)
        assert sought.y[0, 0] == 564
        assert frame_1.y[0, 0] == 164
        assert snapshot[0, 0, 0] > frame_0[0, 0, 0]
        assert decoded[:2] == [(0, (16, 8)), (1, (16, 8))]
        assert decoded[-2:] == [(5, (16, 8)), (5, None)]
    finally:
        session.close()


def test_topos_close_releases_context_after_inflight_prefetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('TOPOS_DISABLE_PREFETCH', raising=False)
    session = _session_with_movie(_FakeMovie(sample_count=2, ticks_per_frame=1, timescale=24),
                                  prefetch=True)
    started = threading.Event()
    resume = threading.Event()
    closed = threading.Event()

    def decode(index: int, size: tuple[int, int] | None, buffer: _DecodeBuffer) -> _DecodeBuffer:
        if index == 1:
            started.set()
            assert resume.wait(timeout=5)
        buffer.ensure(session._decoder, ((16, 8), (8, 8), (8, 8)))
        return buffer

    monkeypatch.setattr(session, '_decode_into', decode)
    monkeypatch.setattr(session._decoder, 'close', closed.set)
    try:
        session.frame_yuv(0, size=(16, 8))
        assert started.wait(timeout=5)
        session.close()
        assert not closed.is_set()
        with pytest.raises(RuntimeError, match='已关闭'):
            session.frame_yuv(1, size=(16, 8))
    finally:
        resume.set()
        session.close()
    assert closed.wait(timeout=5)


def test_topos_v7b_preview_decodes_embedded_base_and_snapshot_reads_full() -> None:
    from unittest.mock import Mock

    movie = _FakeMovie(sample_count=2, ticks_per_frame=1, timescale=24, width=32, height=16)
    movie.packet_base_into = Mock(return_value=(b'base', 4, True))
    movie.packet_into = Mock(return_value=(b'full', 4))
    session = _session_with_movie(movie)
    session._try_base_layer = True
    decoder = session._decoder
    decoder.prepare_request = Mock(side_effect=AssertionError('base was already reduced'))
    decoder.prepare = Mock(side_effect=[
        SimpleNamespace(pixel_format=0, bit_depth=10, plane_count=3, width=16, height=8),
        SimpleNamespace(pixel_format=0, bit_depth=10, plane_count=3, width=16, height=8),
        SimpleNamespace(pixel_format=0, bit_depth=10, plane_count=3, width=32, height=16),
    ])

    def fill_planes(_packet: bytes, planes: tuple, _count: int, _size: int) -> None:
        for plane in planes:
            plane.fill(512)

    decoder.decode_views = Mock(side_effect=fill_planes)
    session._codec.plane_geometry = (
        lambda info, plane: (info.width // (2 if plane else 1), info.height))
    try:
        preview = session.frame_yuv(0, size=(16, 8))
        quarter = session.frame_yuv(1, size=(8, 4))
        snapshot = session.frame_yuv(0)
        assert (preview.width, preview.height) == (16, 8)
        assert (quarter.width, quarter.height) == (8, 4)
        assert quarter.decoded_size == (16, 8)
        assert (snapshot.width, snapshot.height) == (32, 16)
        assert [call.args[0] for call in decoder.decode_views.call_args_list] == [b'base', b'base', b'full']
        assert [call.args[0] for call in movie.packet_base_into.call_args_list] == [0, 1]
        movie.packet_into.assert_called_once_with(0)
    finally:
        session.close()


@pytest.mark.parametrize('factor', [2, 4])
def test_topos_preview_resamples_all_planes_after_full_decode(factor: int) -> None:
    import numpy as np
    from unittest.mock import Mock

    movie = _FakeMovie(sample_count=2, ticks_per_frame=1, timescale=24, width=32, height=16)
    movie.packet_into = Mock(side_effect=lambda index: (bytes([index]), 1))
    session = _session_with_movie(movie)
    decoder = session._decoder
    decoder.prepare = Mock(return_value=SimpleNamespace(pixel_format=0, bit_depth=10, plane_count=3))
    decoder.prepare_request = Mock(side_effect=AssertionError('requires full reconstruction'))
    session._codec.plane_geometry = lambda _info, plane: (16 if plane else 32, 16)

    def decode(packet: bytes, planes: tuple, _count: int, _size: int) -> None:
        for offset, plane in enumerate(planes):
            y, x = np.indices(plane.shape)
            plane[:] = 64 + 120 * offset + 64 * packet[0] + 4 * x + 8 * y

    decoder.decode_views = Mock(side_effect=decode)
    try:
        full = session.frame_yuv(0)
        size = (32 // factor, 16 // factor)
        preview = session.frame_yuv(0, size=size)
        rgb, decoded_size = session.frame_rgb_with_decode_size(0, size=size)
        session.frame_yuv(1, size=size)  # Reusing the decoder buffers must leave preview intact.
        assert preview.decoded_size == decoded_size == (32, 16)
        assert (preview.width, preview.height) == size
        assert rgb.shape == (size[1], size[0], 3)
        assert preview.is_valid()
        for native, resampled in zip((full.y, full.u, full.v), (preview.y, preview.u, preview.v), strict=True):
            h, w = resampled.shape
            exact = native.reshape(h, factor, w, factor).mean(axis=(1, 3))
            np.testing.assert_array_equal(resampled, exact)
            assert resampled.dtype == native.dtype
    finally:
        session.close()


def test_topos_prefetch_rejects_same_frame_from_previous_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('TOPOS_DISABLE_PREFETCH', raising=False)
    movie = _FakeMovie(sample_count=3, ticks_per_frame=1, timescale=24, width=32, height=16)
    session = _session_with_movie(movie, prefetch=True)
    old_request_started = threading.Event()

    def decode(index: int, size: tuple[int, int] | None, buffer: _DecodeBuffer) -> _DecodeBuffer:
        w, h = size or (32, 16)
        buffer.ensure(session._decoder, ((w, h), (w // 2, h), (w // 2, h)))
        if index == 1 and size == (16, 8):
            old_request_started.set()
        return buffer

    monkeypatch.setattr(session, '_decode_into', decode)
    try:
        first = session.frame_yuv(0, size=(16, 8))
        assert (first.width, first.height) == (16, 8)
        assert old_request_started.wait(timeout=5)
        full = session.frame_yuv(1)
        assert (full.width, full.height) == (32, 16)
        quarter = session.frame_yuv(2, size=(8, 4))
        assert (quarter.width, quarter.height) == (8, 4)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# FR-20：等比解码尺寸（不拉伸、保留几何）
# ---------------------------------------------------------------------------

def _yuv_session_with_frame(frame):
    session = _LibavSession.__new__(_LibavSession)
    session._lock = threading.Lock()
    session._reformatter = av.video.reformatter.VideoReformatter()
    session._iter = iter([frame])
    session._next_idx = 0
    session.fps = 24
    session._stream = SimpleNamespace(start_time=0, time_base=Fraction(1, 24))
    session.sar = None
    return session


def test_fr20_wide_video_in_square_box_keeps_aspect():
    frame = av.VideoFrame(160, 80, 'yuv420p')
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    session = _yuv_session_with_frame(frame)
    result = session.frame_yuv(0, size=(400, 400))
    assert (result.width, result.height) == (400, 200)   # 2:1，不再是 400×400
    assert result.width / result.height == pytest.approx(2.0)


def test_session_reformatter_preserves_visible_10bit_samples_when_resized() -> None:
    import numpy as np

    frame = av.VideoFrame(160, 80, 'yuv422p10le')
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    frame.colorspace, frame.color_range = 1, 1
    for index, plane in enumerate(frame.planes):
        values = (np.arange(plane.buffer_size // 2, dtype=np.uint16) + 64 + index) % 960
        plane.update(values.astype('<u2').tobytes())
    session = _yuv_session_with_frame(frame)
    for width, height in ((120, 60), (80, 40), (120, 60)):
        session._iter = iter([frame])
        session._next_idx = 0
        actual = session.frame_yuv(0, size=(width, height))
        expected = frame.reformat(width=width, height=height, format='yuv422p10le')
        assert (actual.width, actual.height, actual.bit_depth, actual.matrix) == (width, height, 10, 1)
        for samples, plane in zip((actual.y, actual.u, actual.v), expected.planes, strict=True):
            rows = np.frombuffer(plane, dtype='<u2').reshape(plane.height, plane.line_size // 2)
            np.testing.assert_array_equal(samples, rows[:, :plane.width])


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


def test_full_resolution_retains_encoded_dimensions_and_sample_aspect() -> None:
    frame = av.VideoFrame(160, 80, 'yuv420p')
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    session = _yuv_session_with_frame(frame)
    session.sar = Fraction(2, 1)
    result = session.frame_yuv(0)
    assert (result.width, result.height) == (160, 80)
    assert result.display_width == 320.0


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
    ((400, 400), (400, 200)), (None, (156, 78)),
])
def test_topos_rgb_fallback_keeps_source_aspect(box, expected):
    import numpy as np
    session = ToposMovieSession.__new__(ToposMovieSession)
    session._av = av
    session.width, session.height = 320, 160
    session.info = SimpleNamespace(color_matrix=1, color_range=0)
    session._rgb_input = None
    # 4:2:2 的 78 像素色度平面会带行尾 padding。
    session.vfmt = "yuv422p10le"
    planes = (np.full((78, 156), 64, dtype="<u2"),
              np.full((78, 78), 512, dtype="<u2"),
              np.full((78, 78), 512, dtype="<u2"))
    buffer = _DecodeBuffer(geometry=((156, 78), (78, 78), (78, 78)), planes=planes)
    session._read_planes_locked = lambda index, preview: buffer
    rgb, decoded_size = session._frame_rgb_locked(0, box)
    assert decoded_size == (156, 78)
    assert (rgb.shape[1], rgb.shape[0]) == expected
    assert not rgb.any()


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


def test_libav_alpha_preview_keeps_transparent_pixels() -> None:
    import numpy as np
    from src.shared.playback.base_player import _frame_to_qimage

    pixels = np.zeros((8, 18, 4), dtype=np.uint8)
    pixels[:, :, 0] = 255
    pixels[4:, :, 3] = 255
    frame = av.VideoFrame.from_ndarray(pixels, format='rgba')
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    session = _yuv_session_with_frame(frame)
    session.rotation_quarters = 0

    image = _frame_to_qimage(session.frame_rgba(0, size=(18, 8)))
    assert image.pixelColor(4, 2).alpha() == 0
    assert image.pixelColor(4, 6).getRgb() == (255, 0, 0, 255)
