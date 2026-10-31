"""Render a preset scene to an animated GIF preview.

Pure-CPU path (SvgSceneRenderer → QSvgRenderer raster → Pillow GIF encode):
no WebEngine, no OpenGL, safe to run inside the editor process.  Requires a
QApplication to exist (the editor always has one; batch scripts create an
offscreen one).

AUD-29708: :func:`render_preset_gif` is the synchronous worker core — full
scene-resolution rasterization, LANCZOS downscaling and GIF encoding over up
to 24 frames — and must not be called from a UI slot (a 4K scene freezes the
editor for seconds).  UI callers use :class:`PresetGifPreviewJob`, which runs
the same function on a ``QThread`` and reports through queued signals so Qt
widgets are only touched on the UI thread; headless/batch callers keep the
plain function.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PIL import Image
from PyQt6.QtCore import QThread, pyqtSignal

from ..core.model import TeachingSceneDocument

PREVIEW_WIDTH = 320
PREVIEW_MAX_FRAMES = 24
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _background_color(scene: TeachingSceneDocument) -> str:
    color = scene.background.get("color") if isinstance(scene.background, dict) else None
    return color if isinstance(color, str) and _HEX_COLOR.match(color) else "#111827"


def _frame_indices(duration_frames: int, max_frames: int) -> list[int]:
    count = max(1, min(max_frames, duration_frames))
    if count == 1:
        return [0]
    indices = [round(index * (duration_frames - 1) / (count - 1)) for index in range(count)]
    return sorted(set(indices))


def render_preset_gif(scene: TeachingSceneDocument, out_path: Path | str, *,
                      width: int = PREVIEW_WIDTH,
                      max_frames: int = PREVIEW_MAX_FRAMES) -> Path:
    """Sample the scene across its duration and write a looping GIF."""
    from PyQt6.QtCore import QByteArray, Qt
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer

    from ..core.contracts import AnimationFrame, RenderContext
    from ..render.svg_renderer import SvgSceneRenderer

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fps = float(scene.fps_num) / float(scene.fps_den or 1)
    context = RenderContext(fps=fps, width=scene.width, height=scene.height,
                            backend="svg", strict_assets=False,
                            render_profile="interactive")
    renderer = SvgSceneRenderer()
    frames: list[Image.Image] = []
    try:
        renderer.prepare(scene, context)
        for index in _frame_indices(scene.duration_frames, max_frames):
            rendered = renderer.render_frame(AnimationFrame(index, fps))
            svg_renderer = QSvgRenderer(QByteArray(rendered.data))
            image = QImage(scene.width, scene.height, QImage.Format.Format_RGBA8888)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            svg_renderer.render(painter)
            painter.end()
            # Same conversion idiom as FrameAdapter: sip.voidptr -> asstring.
            raw = image.constBits().asstring(image.sizeInBytes())
            stride = image.bytesPerLine()
            if stride != scene.width * 4:
                raw = b"".join(
                    raw[row * stride:row * stride + scene.width * 4]
                    for row in range(scene.height))
            pil = Image.frombytes("RGBA", (scene.width, scene.height), raw)
            scaled_width = int(width)
            scaled_height = max(1, round(scene.height * scaled_width / scene.width))
            pil = pil.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)
            composed = Image.new("RGB", pil.size, _background_color(scene))
            composed.paste(pil, mask=pil.split()[3])
            frames.append(composed)
    finally:
        renderer.cleanup()
    if not frames:
        raise ValueError("no preview frames rendered")
    step = max(1.0, scene.duration_frames / len(frames))
    frame_ms = max(20, round(1000.0 * step / fps))
    frames[0].save(out_path, format="GIF", save_all=True,
                   append_images=frames[1:], duration=frame_ms, loop=0,
                   optimize=True)
    return out_path


class PresetGifPreviewJob(QThread):
    """Render one preset preview GIF off the UI thread (AUD-29708).

    ``render_preset_gif`` is a synchronous pure-CPU path — SVG rasterization
    at scene resolution (up to 4K), LANCZOS scaling and GIF encoding over up
    to 24 frames — so calling it directly from a UI slot freezes the whole
    editor for seconds.  This job runs it on a ``QThread`` worker and reports
    through queued signals, so connected slots execute on the receiver's
    (UI) thread.  The scene document is snapshotted at construction; later
    edits to the scene cannot race the render (only this job reads it).

    Usage (the caller must keep the job referenced until a terminal signal
    arrives, and clear the reference there — an unreferenced running
    ``QThread`` aborts the process on garbage collection)::

        job = PresetGifPreviewJob(scene, out_path)
        job.succeeded.connect(self._on_preview_ready)   # receives Path
        job.failed.connect(self._on_preview_error)      # receives str
        job.start()
    """

    succeeded = pyqtSignal(object)  # Path of the written GIF
    failed = pyqtSignal(str)

    def __init__(self, scene: TeachingSceneDocument, out_path: Path | str, *,
                 width: int = PREVIEW_WIDTH,
                 max_frames: int = PREVIEW_MAX_FRAMES,
                 parent: Any = None) -> None:
        super().__init__(parent)
        self._scene = scene
        self._out_path = Path(out_path)
        self._width = int(width)
        self._max_frames = int(max_frames)

    def run(self) -> None:
        try:
            out_path = render_preset_gif(self._scene, self._out_path,
                                         width=self._width,
                                         max_frames=self._max_frames)
        except Exception as exc:  # noqa: BLE001 — any failure is surfaced on the UI thread
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(out_path)


__all__ = [
    "PREVIEW_MAX_FRAMES", "PREVIEW_WIDTH", "PresetGifPreviewJob", "render_preset_gif",
]
