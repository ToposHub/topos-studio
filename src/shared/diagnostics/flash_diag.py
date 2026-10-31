"""Environment-gated flash diagnostics for the Color preview chain.

Enable by setting ``TOPOS_FLASH_DIAG=<path.jsonl>`` before launching the
app.  Every front-texture publish, render-command drop and playback submit
is appended to that file as one JSON object per line, so a scripted run can
reconstruct exactly which command painted each frame the viewer showed.

When the variable is unset the helpers are no-ops with negligible cost, so
the instrumentation can stay in the tree permanently.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

__all__ = ["enabled", "event", "now_ms"]

_LOCK = threading.Lock()
_PATH = os.environ.get("TOPOS_FLASH_DIAG", "")
_START = time.monotonic()


def enabled() -> bool:
    return bool(_PATH)


def now_ms() -> float:
    return (time.monotonic() - _START) * 1000.0


def event(kind: str, **fields: Any) -> None:
    """Append one diagnostic event.  Never raises into the caller."""
    if not _PATH:
        return
    record = {"t": round(now_ms(), 3), "k": kind}
    record.update(fields)
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            with open(_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass
