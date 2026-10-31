"""SceneView RGBA display (HTML live-preview phase-1).

SceneView used to hard-reject anything but SVG.  HTML captures arrive as
tightly-packed RGBA8888 bytes (``pixel_format="rgba8"``); these tests pin the
new raster branch (QImage built + detached via .copy()) and that the SVG path
is untouched.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.contracts import RenderedFrame
from src.features.teaching_animation.ui.scene_view import SceneView


def _rgba(width: int, height: int, rgba_pixel: bytes = b"\xff\x00\x00\xff") -> RenderedFrame:
    return RenderedFrame(
        frame=0, width=width, height=height, pixel_format="rgba8",
        data=rgba_pixel * (width * height), backend="html")


@pytest.fixture
def view(qapp):
    v = SceneView()
    yield v
    v.deleteLater()
    qapp.processEvents()


class TestRasterDisplay:
    def test_rgba_frame_builds_image_without_raising(self, view) -> None:
        frame = _rgba(8, 4)
        view.set_frame(frame)
        assert view._image is not None and not view._image.isNull()
        assert view._renderer is None          # SVG renderer cleared
        assert view._frame is frame

    def test_rgba_image_has_frame_dimensions(self, view) -> None:
        view.set_frame(_rgba(12, 7))
        assert view._image.size().width() == 12
        assert view._image.size().height() == 7

    def test_svg_frame_still_builds_svg_renderer(self, view) -> None:
        svg = b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>"
        frame = RenderedFrame(frame=0, width=10, height=10, pixel_format="svg", data=svg)
        view.set_frame(frame)
        assert view._renderer is not None
        assert view._image is None              # raster cleared on SVG frame

    def test_unknown_format_still_raises(self, view) -> None:
        frame = RenderedFrame(frame=0, width=2, height=2, pixel_format="rgb24", data=b"\x00" * 12)
        with pytest.raises(ValueError):
            view.set_frame(frame)

    def test_set_scene_clears_raster(self, view) -> None:
        from src.features.teaching_animation.core.model import TeachingSceneDocument
        view.set_frame(_rgba(4, 3))
        assert view._image is not None
        view.set_scene(TeachingSceneDocument.create_default("s", width=10, height=10))
        assert view._image is None              # stale raster invalidated on scene switch
