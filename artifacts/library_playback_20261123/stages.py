"""Split actual Libav session costs and compare reusable swscale state."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

import numpy as np
import av
from av.video.reformatter import VideoReformatter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.shared.playback.libav_player import _LibavSession
from src.shared.playback.frame_geometry import fit_aspect_size

parser = argparse.ArgumentParser(__doc__)
parser.add_argument("--filter", default="4K_")
parser.add_argument("--frames", type=int, default=80)
parser.add_argument("--width", type=int, default=1920)
parser.add_argument("--height", type=int, default=1080)
parser.add_argument("--output", default="stages.json")
parser.add_argument("--thread-type", default="")
parser.add_argument("--threads", type=int, default=-1)
args = parser.parse_args()
rows: list[dict[str, Any]] = []


class Session(_LibavSession):
    def _probe_rotation_unlocked(self) -> int:
        if args.thread_type:
            self._stream.thread_type = args.thread_type
        if args.threads >= 0:
            self._stream.thread_count = args.threads
        return super()._probe_rotation_unlocked()


def stats(values: list[float]) -> dict[str, float]:
    return {"mean": float(np.mean(values)), "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95))}


class TimedIterator:
    def __init__(self, source: Any, samples: list[float]) -> None:
        self.source = source
        self.samples = samples

    def __next__(self) -> Any:
        started = time.perf_counter()
        value = next(self.source)
        self.samples.append((time.perf_counter() - started) * 1000.0)
        return value


for path in sorted((ROOT / "tests/test_videos").glob("*.mov")):
    if args.filter.lower() not in path.name.lower() or "topos" in path.name:
        continue
    for reuse in (False, True):
        session = Session(str(path))
        original = session._decode_frame_locked
        decode_samples: list[float] = []
        resize_samples: list[float] = []
        total_samples: list[float] = []
        reformatter = VideoReformatter()

        def decode(index: int, size: Any = None, *, pixel_format: str | None = None) -> Any:
            # Use exactly the session's sequential decode and index handling.
            frame = original(index, None)
            started = time.perf_counter()
            w, h = fit_aspect_size(frame, args.width, args.height, session.sar,
                                  rotation_quarters=session.rotation_quarters)
            if reuse:
                result = reformatter.reformat(frame, width=w, height=h,
                                               format=pixel_format or frame.format.name)
            else:
                result = frame.reformat(width=w, height=h,
                                        format=pixel_format or frame.format.name)
            resize_samples.append((time.perf_counter() - started) * 1000.0)
            return result

        session._decode_frame_locked = decode
        pull = session.frame_rgba if session.has_alpha else session.frame_yuv
        try:
            for i in range(8):
                pull(i, size=(args.width, args.height))
            session._iter = TimedIterator(session._iter, decode_samples)
            resize_samples.clear()
            for i in range(8, min(args.frames + 8, session.frame_count)):
                started = time.perf_counter()
                pull(i, size=(args.width, args.height))
                total_samples.append((time.perf_counter() - started) * 1000.0)
            row = {"file": path.name, "reuse_reformatter": reuse,
                   "thread_type": str(session._stream.thread_type),
                   "thread_count": session._stream.thread_count,
                   "output_size": [args.width, args.height], "has_alpha": session.has_alpha,
                   "decode_next_ms": stats(decode_samples), "resize_ms": stats(resize_samples),
                   "total_ms": stats(total_samples), "frames": len(total_samples)}
            row["copy_and_bookkeeping_ms"] = float(np.mean(total_samples) - np.mean(decode_samples) - np.mean(resize_samples))
            rows.append(row)
            Path(__file__).with_name(args.output).write_text(json.dumps(rows, indent=2))
            print(json.dumps(row), flush=True)
        finally:
            session.close()
