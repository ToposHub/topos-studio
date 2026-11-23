"""Measure the real Library player host; no production behavior is changed.

Run from the repository root with .venv/bin/python. A native preview window
is displayed. JSON is checkpointed after every file. Timings are diagnostics,
not assertions; frameSwapped is a presentation proxy, not a display capture.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if sys.platform == "darwin":
    sys.setdlopenflags((sys.getdlopenflags() & ~getattr(os, "RTLD_GLOBAL", 0)) | os.RTLD_LOCAL)

import numpy as np
from PyQt6.QtCore import QTimer, Qt, QT_VERSION_STR
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtWidgets import QApplication, QWidget

parser = argparse.ArgumentParser(__doc__)
parser.add_argument("--filter", default="")
parser.add_argument("--width", type=int, default=1920)
parser.add_argument("--height", type=int, default=1080)
parser.add_argument("--seconds", type=float, default=4.0)
parser.add_argument("--warmup", type=float, default=1.0)
parser.add_argument("--output", default="matrix.json")
parser.add_argument("--skip", type=int, default=0)
parser.add_argument("--trace", action="store_true")
parser.add_argument("--poll-ms", type=int, default=0,
                    help="Diagnostic A/B only: override the production poll interval")
args = parser.parse_args()

# Match src/main.py's application format and context-sharing policy.
fmt = QSurfaceFormat()
fmt.setVersion(4, 5)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
fmt.setRedBufferSize(8)
fmt.setGreenBufferSize(8)
fmt.setBlueBufferSize(8)
fmt.setAlphaBufferSize(8)
fmt.setDepthBufferSize(24)
fmt.setStencilBufferSize(8)
fmt.setSamples(4)
fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
QSurfaceFormat.setDefaultFormat(fmt)
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
app = QApplication([])

from src.shared.playback.base_player import BasePlayer
from src.shared.playback.libav_player import LibavPlayer
from src.shared.playback.topos_player import ToposPlayer
from src.shared.playback.gl_render_worker import _RenderThread
from src.shared.playback.gl_video_canvas import GLVideoCanvas
from src.shared.playback.player_host import NativePlayerHost
from src.shared.playback.player_qt import UltimatePlayer, _VideoCanvas

active: dict[str, Any] = {}


def timed_method(cls: type, name: str, key: str) -> None:
    original = getattr(cls, name)

    def wrapped(self: Any, *values: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        result = original(self, *values, **kwargs)
        ended = time.perf_counter()
        if active and ended >= active["measure_start"]:
            active.setdefault(key, []).append((ended - started) * 1000.0)
            if key == "decode_ms":
                active["payload_type"] = type(result).__name__
        return result

    setattr(cls, name, wrapped)


for player_class in (LibavPlayer, ToposPlayer):
    timed_method(player_class, "_decode_frame", "decode_ms")
timed_method(_RenderThread, "_render_frame", "render_ms")
timed_method(GLVideoCanvas, "paintGL", "paint_ms")
timed_method(UltimatePlayer, "_on_video_frame", "qt_frame_to_image_ms")
timed_method(_VideoCanvas, "paintEvent", "qt_paint_ms")

original_show = BasePlayer._show_frame


def show_frame(self: Any, frame: Any) -> None:
    now = time.perf_counter()
    if active and now >= active["measure_start"]:
        active.setdefault("submitted", []).append((now, int(frame[0])))
    original_show(self, frame)


BasePlayer._show_frame = show_frame

original_poll = LibavPlayer._poll_ring


def poll_ring(self: Any) -> None:
    now = time.perf_counter()
    if args.trace and active and now >= active["measure_start"] and self._session:
        with self._ring_lock:
            queued = [frame[0] for frame in self._ring]
        active.setdefault("tick_trace", []).append({
            "time": now, "clock": self._master_clock(),
            "shown": self._shown_index, "queued": queued})
    original_poll(self)


LibavPlayer._poll_ring = poll_ring


def summary(values: list[float]) -> dict[str, float | int]:
    return {"count": len(values), "mean": float(np.mean(values)),
            "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95)), "max": float(max(values))} if values else {"count": 0}


window = QWidget()
window.setWindowTitle("Topos Library playback diagnostic")
window.resize(args.width, args.height)
window.show()
host = NativePlayerHost(window, lightbox=True)
host.failed.connect(lambda message: active.setdefault("errors", []).append(message))
files = sorted(path for path in (ROOT / "tests/test_videos").iterdir()
               if path.suffix.lower() in {".mov", ".mp4"} and args.filter.lower() in path.name.lower())
files = files[args.skip:]
rows: list[dict[str, Any]] = []
index = 0
output = Path(__file__).parent / args.output


def swapped() -> None:
    now = time.perf_counter()
    if not active or now < active["measure_start"]:
        return
    player = host._current
    canvas = getattr(player, "canvas", None)
    frame = getattr(canvas, "_frame", None)
    worker = getattr(canvas, "_render_worker", None)
    if type(frame).__name__ == "YuvFrame" and worker is not None:
        front = worker.front_texture()
        identity = front[-1] if front else None
    else:
        identity = id(frame)
    if identity is not None and identity != active.get("last_identity"):
        active["last_identity"] = identity
        active.setdefault("swapped", []).append(now)


def heartbeat() -> None:
    now = time.perf_counter()
    if active and now >= active["measure_start"]:
        active.setdefault("heartbeat", []).append(now)


beat = QTimer()
beat.setTimerType(Qt.TimerType.PreciseTimer)
beat.setInterval(10)
beat.timeout.connect(heartbeat)
beat.start()


def next_file() -> None:
    global active, index
    if index >= len(files):
        players = list(host._players.values())
        host.shutdown()
        for player in players:
            canvas = getattr(player, "canvas", None)
            if canvas is not None and hasattr(canvas, "_release_gl"):
                canvas._release_gl()
            player.close()
        window.close()
        app.quit()
        return
    path = files[index]
    index += 1
    active = {"file": path.name, "measure_start": float("inf")}
    started = time.perf_counter()
    host.play(str(path))
    player = host._current
    if args.poll_ms and hasattr(player, "_ui_timer"):
        player._ui_timer.setInterval(args.poll_ms)
    active["open_ms"] = (time.perf_counter() - started) * 1000.0
    active["route"] = host.current_route
    canvas = getattr(player, "canvas", None)
    if canvas is not None and not getattr(canvas, "_probe_connected", False):
        canvas.frameSwapped.connect(swapped)
        canvas._probe_connected = True
    if isinstance(player, UltimatePlayer) and not getattr(player, "_probe_connected", False):
        player.video_sink.videoFrameChanged.connect(lambda frame: active.setdefault("qt_frames", []).append(time.perf_counter()) if active and time.perf_counter() >= active["measure_start"] and frame.isValid() else None)
        player._probe_connected = True
    active["measure_start"] = time.perf_counter() + args.warmup
    QTimer.singleShot(int(args.seconds * 1000), finish_file)


def finish_file() -> None:
    elapsed = time.perf_counter() - active["measure_start"]
    player = host._current
    session = getattr(player, "_session", None)
    canvas = getattr(player, "canvas", None)
    worker = getattr(canvas, "_render_worker", None)
    row = {key: value for key, value in active.items() if key in
           {"file", "route", "open_ms", "payload_type", "errors"}}
    row.update(measured_seconds=elapsed, window=[window.width(), window.height()],
               canvas_size=canvas.display_size() if canvas else None,
               gpu_active=getattr(canvas, "gpu_active", None),
               gpu_error=getattr(canvas, "gpu_error", None))
    if session:
        row.update(source_size=[session.width, session.height], source_fps=session.fps,
                   codec=getattr(session, "codec_name", "topos"),
                   has_alpha=getattr(session, "has_alpha", None),
                   shown_index=player._shown_index)
        if hasattr(player, "_master_clock"):
            row["master_clock_seconds"] = player._master_clock()
            row["behind_clock_frames"] = max(
                0, int(player._master_clock() * session.fps) - player._shown_index)
    if worker:
        front = worker.front_texture()
        row["worker_mode"] = front[0] if front else None
        row["worker_alive"] = worker.alive
    for key in ("decode_ms", "render_ms", "paint_ms", "qt_frame_to_image_ms", "qt_paint_ms"):
        row[key] = summary(active.get(key, []))
    for key in ("swapped", "heartbeat", "qt_frames"):
        timestamps = active.get(key, [])
        row[key + "_fps"] = len(timestamps) / elapsed
        row[key + "_interval_ms"] = summary((np.diff(timestamps) * 1000.0).tolist())
    submissions = active.get("submitted", [])
    row["submitted_fps"] = len(submissions) / elapsed
    row["submitted_frame_indices"] = [entry[1] for entry in submissions]
    if args.trace:
        row["tick_trace"] = active.get("tick_trace", [])
        row["submission_trace"] = submissions
    rows.append(row)
    output.write_text(json.dumps({"environment": {"machine": platform.machine(), "qt": QT_VERSION_STR,
        "platform": platform.platform()}, "arguments": vars(args), "results": rows}, indent=2))
    print(json.dumps({key: row.get(key) for key in ("file", "route", "submitted_fps", "swapped_fps", "qt_frames_fps", "decode_ms", "worker_mode", "errors")}), flush=True)
    active.clear()
    host.stop()
    QTimer.singleShot(150, next_file)


QTimer.singleShot(200, next_file)
app.exec()
