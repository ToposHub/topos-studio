"""Observe producer EOF and paused consumption with synthetic frames, no GPU/media IO."""
from __future__ import annotations

import json
import sys
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.shared.playback.base_player import BasePlayer
from src.shared.playback.topos_player import ToposPlayer


def main() -> None:
    events: list[dict[str, object]] = []
    state = SimpleNamespace(
        _cond=threading.Condition(), _ring_lock=threading.Lock(),
        _running=True, _playing=True, _seek_target=None, _next_index=0,
        _ring=deque(), RING_SIZE=6, _shown_index=-1,
        _on_seek_jump=lambda: None, _on_decode_error=lambda: None,
        _on_decode_stop=lambda: None, _decode_frame=lambda session, index: object(),
    )
    state._ring_space = threading.Condition(state._ring_lock)
    state._audio = SimpleNamespace(pause=lambda: events.append({"event": "audio.pause"}))
    state._eof_signal = SimpleNamespace(emit=lambda: events.append({"event": "eof_signal"}))

    def eof() -> None:
        ToposPlayer._on_eof(state)
        events.append({"event": "producer_eof", "playing": state._playing,
                       "shown_index": state._shown_index,
                       "queued_indices": [item[0] for item in state._ring]})
        state._running = False

    state._on_eof = eof
    BasePlayer._decode_loop(state, SimpleNamespace(frame_count=2))
    print(json.dumps({"case": "FR-18 producer EOF before presentation", "events": events}))

    shown: list[int] = []
    state._audio = None
    state._pending = deque()
    state._session = SimpleNamespace(frame_count=2, fps=24.0)
    state._show_frame = lambda pick: shown.append(pick[0])
    ToposPlayer._poll_ring(state)
    print(json.dumps({"case": "Topos poll while playing is false", "shown_indices": shown}))


if __name__ == "__main__":
    main()
