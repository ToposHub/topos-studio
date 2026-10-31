"""Pure tests for Topos planar GPU sampling metadata."""

import pytest

from src.features.color.core.async_renderer import AsyncRenderer
from src.shared.video.yuv_gpu_upload import topos_chroma_sampling_params


def test_topos_422_left_siting_has_half_luma_phase():
    scale, offset = topos_chroma_sampling_params("topos422p10", 17, 9, 0)

    assert scale == (1.0, 1.0)
    assert offset == pytest.approx((0.5 / 17.0, 0.0))


def test_topos_422_center_siting_has_no_phase():
    assert topos_chroma_sampling_params("topos422p10", 17, 9, 1) == (
        (1.0, 1.0),
        (0.0, 0.0),
    )


@pytest.mark.parametrize("pixel_format", ["topos444p10", "topos_gbrp10"])
def test_full_resolution_planar_formats_have_no_chroma_phase(pixel_format):
    assert topos_chroma_sampling_params(pixel_format, 17, 9, 0) == (
        (1.0, 1.0),
        (0.0, 0.0),
    )


def test_invalid_topos_chroma_siting_is_rejected():
    with pytest.raises(ValueError, match="unsupported Topos chroma_siting"):
        topos_chroma_sampling_params("topos422p10", 17, 9, 3)


@pytest.mark.parametrize("width,height", [(0, 9), (17, 0)])
def test_invalid_source_dimensions_are_rejected(width, height):
    with pytest.raises(ValueError, match="source dimensions must be positive"):
        topos_chroma_sampling_params("topos422p10", width, height, 0)


class _RecordingUploader:
    def __init__(self):
        self.render_kwargs = None

    def get_last_upload_timing(self):
        return {"mode": "r16ui_direct"}

    def set_yuv_matrix(self, value):
        pass

    def set_color_range(self, value):
        pass

    def render_yuv_r16ui_to_rgb(self, *args, **kwargs):
        self.render_kwargs = kwargs


@pytest.mark.parametrize(
    "pixel_format, expected_offset, expected_gbr",
    [
        ("topos422p10", (0.5 / 17.0, 0.0), False),
        ("topos_gbrp10", (0.0, 0.0), True),
    ],
)
def test_async_renderer_forwards_topos_sampling_semantics(
    pixel_format, expected_offset, expected_gbr
):
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    renderer._yuv_gpu_uploader = _RecordingUploader()
    renderer.current_video_bit_depth = 10
    renderer.current_video_chroma_siting = 0
    renderer.current_video_alpha_bit_depth = 10
    renderer.current_video_alpha_premultiplied = False
    renderer._video_source_uv_transform = lambda visible_rect, width, height: (
        (1.0, 1.0),
        (0.0, 0.0),
    )

    renderer._render_yuv420_to_rgb(
        object(), object(), object(), object(),
        17, 9, 17, 9, 0, 0, pixel_format,
    )

    assert renderer._yuv_gpu_uploader.render_kwargs == {
        "chroma_uv_scale": (1.0, 1.0),
        "chroma_uv_offset": expected_offset,
        "bit_depth": 10,
        "source_uv_scale": (1.0, 1.0),
        "source_uv_offset": (0.0, 0.0),
        "a_tex": None,
        "alpha_bit_depth": 10,
        "alpha_premultiplied": False,
        "gbr_mode": expected_gbr,
    }
