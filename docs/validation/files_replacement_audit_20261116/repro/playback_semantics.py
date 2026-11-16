"""Reproduce playback bookkeeping using fake sessions; no GL or real media IO."""
from fractions import Fraction
from pathlib import Path
from threading import Condition, Lock
from types import SimpleNamespace
from unittest.mock import patch
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import av
from src.shared.playback.base_player import BasePlayer
from src.shared.playback.libav_player import _LibavSession
from src.shared.playback.topos_player import ToposMovieSession


def report(case, **fields):
    print(json.dumps({"case": case, **fields}))


def check_eof():
    targets, eof = [], []
    ring_lock = Lock()
    state = SimpleNamespace(
        _cond=Condition(), _ring_lock=ring_lock, _ring_space=Condition(ring_lock),
        _running=True, _playing=True, _seek_target=None, _next_index=0,
        _ring=[], RING_SIZE=20, _on_seek_jump=lambda: None,
        _on_eof=lambda: eof.append(True),
        _on_decode_error=lambda: None, _on_decode_stop=lambda: None,
    )

    def decode(session, index):
        targets.append(index)
        if len(targets) >= 6:
            state._running = False
        return object()

    state._decode_frame = decode
    BasePlayer._decode_loop(state, SimpleNamespace(frame_count=2))
    report("eof_never_reached", targets=targets, eof_calls=len(eof))


def check_topos_fps():
    class Codec:
        pass

    class Movie:
        def __init__(self, codec, path):
            pass

        def info(self):
            return SimpleNamespace(width=16, height=8, sample_count=240,
                                   timescale=24000, duration_ticks=240000,
                                   pixel_format=0, bit_depth=10, alpha_mode=0)

        def audio_track_count(self):
            return 0

        def close(self):
            pass

    class Decoder:
        def __init__(self, *args, **kwargs):
            pass

        def make_views(self, arrays):
            return arrays

        def close(self):
            pass

    with patch("src.shared.playback.topos_player._load_binding", return_value=(Codec, Movie, Decoder)):
        session = ToposMovieSession("/not-opened/audit.mov")
        report("timescale_is_not_fps", expected_fps=24, actual_fps=session.fps,
               expected_duration=10, actual_duration=session.duration_sec)
        session.close()


def check_aspect():
    frame = av.VideoFrame(160, 80, "yuv420p")
    frame.pts, frame.time_base = 0, Fraction(1, 24)
    session = _LibavSession.__new__(_LibavSession)
    session._lock = Lock()
    session._iter = iter([frame])
    session._next_idx = 0
    session.fps = 24
    result = session.frame_yuv(0, size=(400, 400))
    report("decode_stretches_aspect", source=[160, 80], display=[400, 400],
           decoded=[result.width, result.height],
           source_aspect=2, decoded_aspect=result.width / result.height)


if __name__ == "__main__":
    check_eof()
    check_topos_fps()
    check_aspect()
