"""Decisions of the Metal playback path (plan 7.1): pure logic, no GPU.

``metal_playback`` answers three questions without creating a Metal object,
a GL context or a widget: may this session show frames on Metal, may this
frame take the Metal path, and what does the kernel need to draw it.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.shared.playback import metal_playback as M
from src.shared.playback.gl_video_canvas import _h273_matrix_to_uploader
from src.shared.playback.yuv_frame import YuvFrame
from src.shared.video.yuv_gpu_upload import topos_chroma_sampling_params

W, H = 64, 36


def _frame(w=W, h=H, sub="420", depth=8, planes: dict | None = None, **fields) -> YuvFrame:
    """A frame of zero planes in the right layout; ``planes`` replaces some."""
    cw, ch = {"420": (w // 2, h // 2), "422": (w // 2, h), "444": (w, h)}[sub]
    dtype = np.uint8 if depth == 8 else np.uint16
    base = dict(bit_depth=depth, subsampling=sub, matrix=1, color_range=0, chroma_siting=1,
                pixel_format=sub, source_width=w, source_height=h)
    base.update(fields)
    arrays = {"y": np.zeros((h, w), dtype), "u": np.zeros((ch, cw), dtype), "v": np.zeros((ch, cw), dtype)}
    arrays.update(planes or {})
    return YuvFrame(arrays["y"], arrays["u"], arrays["v"], **base)


class TestOptIn:
    def test_off_unless_asked_for(self) -> None:
        assert M.playback_metal_requested({}) is False
        assert M.playback_metal_requested({M.ENV_PLAYBACK_METAL: "0"}) is False
        assert M.playback_metal_requested({M.ENV_PLAYBACK_METAL: "1"}) is True
        assert M.playback_metal_requested({M.ENV_PLAYBACK_METAL: " YES "}) is True
        assert M.playback_metal_requested({M.ENV_PLAYBACK_METAL: "on"}) is True

    @pytest.mark.parametrize(("settings", "reason"), [
        (dict(requested=False, platform="darwin", qt_platform="cocoa", native_viewport_disabled=False),
         "not_requested"),
        (dict(requested=True, platform="linux", qt_platform="cocoa", native_viewport_disabled=False),
         "platform_unsupported:linux"),
        (dict(requested=True, platform="darwin", qt_platform="offscreen", native_viewport_disabled=False),
         "qt_platform_unsupported:offscreen"),
        (dict(requested=True, platform="darwin", qt_platform="cocoa", native_viewport_disabled=True),
         "disabled_by_switch:native_viewport"),
        (dict(requested=True, platform="darwin", qt_platform="cocoa", native_viewport_disabled=False), ""),
    ])
    def test_the_session_verdict_names_what_stops_it(self, settings: dict, reason: str) -> None:
        assert M.session_refusal(**settings) == reason

    def test_the_order_of_the_checks(self) -> None:
        # The cheapest reason first, and an unknown platform or Qt platform is named.
        assert M.session_refusal(requested=True, platform="", qt_platform="",
                                 native_viewport_disabled=True) == "platform_unsupported:unknown"
        assert M.session_refusal(requested=True, platform="darwin", qt_platform="",
                                 native_viewport_disabled=True) == "qt_platform_unsupported:none"

    def test_this_process_honours_the_switch_and_the_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(M.sys, "platform", "darwin")
        monkeypatch.delenv(M.ENV_PLAYBACK_METAL, raising=False)
        monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)
        assert M.current_session_refusal("cocoa") == "not_requested"
        monkeypatch.setenv(M.ENV_PLAYBACK_METAL, "1")
        assert M.current_session_refusal("cocoa") == ""
        monkeypatch.setenv("TOPOS_METAL_DISABLE", "native_viewport")
        assert M.current_session_refusal("cocoa") == "disabled_by_switch:native_viewport"
        monkeypatch.setenv("TOPOS_METAL_DISABLE", "edit_metal_composite")     # another capability
        assert M.current_session_refusal("cocoa") == ""


class TestFrameVerdict:
    @pytest.mark.parametrize(("label", "frame"), [
        ("8-bit 4:2:0 BT.709 limited", _frame()),
        ("8-bit 4:2:2", _frame(sub="422")),
        ("8-bit 4:4:4 full range", _frame(sub="444", color_range=1)),
        ("10-bit 4:2:2", _frame(sub="422", depth=10)),
        ("12-bit 4:4:4", _frame(sub="444", depth=12)),
        ("16-bit 4:2:2", _frame(sub="422", depth=16)),
        ("GBR planes", _frame(sub="444", pixel_format="gbrp10le", depth=10, color_range=1)),
    ])
    def test_the_formats_the_players_hand_over_are_taken(self, label: str, frame: YuvFrame) -> None:
        assert M.frame_refusal(frame) == "", label

    @pytest.mark.parametrize("matrix", sorted(M.SUPPORTED_MATRICES))
    def test_every_matrix_with_a_defined_outcome(self, matrix: int) -> None:
        assert M.frame_refusal(_frame(matrix=matrix)) == ""

    @pytest.mark.parametrize(("label", "frame", "reason"), [
        ("9-bit", _frame(depth=9), "bit_depth_unsupported:9"),
        ("4:1:1", _frame(sub="420", subsampling="411"), "subsampling_unsupported:411"),
        ("no subsampling", _frame(subsampling=""), "subsampling_unsupported:none"),
        ("GBR that is not 4:4:4", _frame(sub="420", pixel_format="gbrp"), "gbr_requires_444:420"),
        ("unknown range", _frame(color_range=2), "color_range_unsupported:2"),
        ("matrix 4 has no defined outcome", _frame(matrix=4), "matrix_unsupported:4"),
        ("4:2:0 planes for a 4:2:2 frame", _frame(sub="420", subsampling="422"), "plane_geometry_mismatch"),
    ])
    def test_what_is_refused_and_why(self, label: str, frame: YuvFrame, reason: str) -> None:
        # A refused frame is routed to the GL worker: it is shown as today.
        assert M.frame_refusal(frame) == reason, label

    def test_422_needs_a_chroma_position_the_gl_path_knows(self) -> None:
        assert M.frame_refusal(_frame(sub="422", chroma_siting=2)) == ""
        assert M.frame_refusal(_frame(sub="422", chroma_siting=3)) == "chroma_siting_unsupported:3"
        # Other subsamplings do not depend on it.
        assert M.frame_refusal(_frame(sub="420", chroma_siting=3)) == ""

    @pytest.mark.parametrize(("change", "reason"), [
        ({"y": np.zeros((H, W), np.float32)}, "plane_dtype_unsupported:y:float32"),
        ({"y": np.zeros((H, W), np.uint16)}, "plane_dtype_unsupported:y:uint16"),
        ({"y": np.zeros((0, W), np.uint8)}, "plane_empty:y"),
        ({"y": np.zeros((H, W), np.uint8).ravel()}, "plane_shape_unsupported:y"),
        ({"v": np.zeros((H // 2, W // 2), np.int16)}, "plane_dtype_unsupported:v:int16"),
    ])
    def test_planes_are_checked_from_their_headers(self, change: dict, reason: str) -> None:
        assert M.frame_refusal(_frame(planes=change)) == reason

    def test_a_plane_beyond_the_device_limit_is_refused(self) -> None:
        frame = _frame()
        assert M.frame_refusal(frame, max_texture=W) == ""
        assert M.frame_refusal(frame, max_texture=W - 1) == "plane_too_large:y"

    def test_a_10_bit_frame_needs_16_bit_planes(self) -> None:
        frame = _frame(depth=10)
        for name in ("y", "u", "v"):
            planes = {name: np.zeros(getattr(frame, name).shape, np.uint8)}
            assert M.frame_refusal(_frame(depth=10, planes=planes)).startswith(
                f"plane_dtype_unsupported:{name}:uint8")

    def test_the_verdict_needs_no_pixel_data(self) -> None:
        """Only metadata and array headers are read: a frame whose pixels are
        not in memory yet can be judged."""
        class _Lazy:
            dtype = np.dtype(np.uint8)
            shape = (H // 2, W // 2)
        frame = _frame()
        frame.u = _Lazy()          # type: ignore[assignment]
        assert M.frame_refusal(frame) == ""


class TestDisplayAndDevice:
    def test_display_sizes(self) -> None:
        assert M.display_size_refusal(1920, 1080) == ""
        assert M.display_size_refusal(0, 1080) == "display_size_unusable:0x1080"
        assert M.display_size_refusal(-4, 1080) == "display_size_unusable:-4x1080"
        assert M.display_size_refusal(20000, 100, max_texture=16384) == "display_size_unsupported:20000x100:limit=16384"

    def test_the_device_limit_is_read_when_the_binding_has_it(self) -> None:
        assert M.device_max_texture(SimpleNamespace(maxTextureDimension2D=lambda: 8192)) == 8192
        assert M.device_max_texture(SimpleNamespace()) == M.DEFAULT_MAX_TEXTURE == 16384
        assert M.device_max_texture(SimpleNamespace(maxTextureDimension2D=lambda: "many")) == 16384


class TestGeometry:
    def test_a_picture_that_fits_exactly(self) -> None:
        assert M.letterbox_rect(64, 36, 64, 36) == (0, 0, 64, 36)

    def test_it_is_the_gl_viewport_seen_from_the_top(self) -> None:
        """The GL worker sets its viewport from the bottom; a leftover row
        stays there, so in a top-left picture it is at the top."""
        for frame_w, frame_h, out_w, out_h in ((64, 36, 80, 36), (64, 36, 64, 48), (64, 36, 100, 41),
                                               (1920, 1080, 1281, 721), (64, 36, 10, 10), (36, 64, 64, 36)):
            scale = min(out_w / frame_w, out_h / frame_h)
            view_w, view_h = max(1, int(frame_w * scale)), max(1, int(frame_h * scale))
            left, bottom = (out_w - view_w) // 2, (out_h - view_h) // 2
            assert M.letterbox_rect(frame_w, frame_h, out_w, out_h) == (left, out_h - bottom - view_h, view_w, view_h)
            # The picture keeps its aspect ratio and sits inside the output.
            left, top, width, height = M.letterbox_rect(frame_w, frame_h, out_w, out_h)
            assert left >= 0 and top >= 0
            if width >= 32:                       # below that, whole pixels round the ratio
                assert abs(width / height - frame_w / frame_h) < 0.03    # whole pixels
            assert left + width <= out_w and top + height <= out_h

    def test_an_odd_leftover_row_stays_at_the_top(self) -> None:
        # 64x36 in 64x49: 13 spare rows, 6 below and 7 above.
        assert M.letterbox_rect(64, 36, 64, 49) == (0, 7, 64, 36)

    @pytest.mark.parametrize("matrix", [0, 1, 2, 5, 6, 9, 10])
    def test_the_matrix_decision_is_the_gl_one(self, matrix: int) -> None:
        assert M.uploader_matrix(matrix) == _h273_matrix_to_uploader(matrix)

    def test_the_matrix_constants(self) -> None:
        assert M.uploader_matrix(9) == 1 and M.uploader_matrix(5) == 2 and M.uploader_matrix(6) == 2
        assert M.uploader_matrix(1) == 0 and M.uploader_matrix(0) == 0

    def test_chroma_sampling_is_read_from_the_function_the_gl_path_uses(self) -> None:
        for siting in (0, 1, 2):
            frame = _frame(sub="422", depth=10, chroma_siting=siting, source_width=128, source_height=72)
            assert M.chroma_sampling(frame) == topos_chroma_sampling_params("422", 128, 72, siting)
        # Only 4:2:2 carries a horizontal chroma phase.
        assert M.chroma_sampling(_frame(sub="420")) == ((1.0, 1.0), (0.0, 0.0))
        assert M.chroma_sampling(_frame(sub="444", depth=10)) == ((1.0, 1.0), (0.0, 0.0))


class TestKernelParameters:
    @pytest.mark.parametrize(("depth", "expected"), [
        (8, (255.0, 16.0 / 255.0, 255.0 / 219.0, 128.0 / 255.0, 255.0 / 224.0)),
        (10, (1023.0, 64.0 / 1023.0, 1023.0 / 876.0, 512.0 / 1023.0, 1023.0 / 896.0)),
        (12, (4095.0, 256.0 / 4095.0, 4095.0 / 3504.0, 2048.0 / 4095.0, 4095.0 / 3584.0)),
        (16, (65535.0, 4096.0 / 65535.0, 65535.0 / 56064.0, 32768.0 / 65535.0, 65535.0 / 57344.0)),
    ])
    def test_range_constants_as_float32(self, depth: int, expected: tuple) -> None:
        constants = M.range_constants(depth)
        for got, value in zip(constants, expected):
            assert got == float(np.float32(value)), (depth, got, value)

    def test_an_undefined_depth_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="bit depth"):
            M.range_constants(14)

    def test_the_layout_is_the_msl_struct(self) -> None:
        assert M.YUV_PARAMS_DTYPE.itemsize == 112
        assert M.YUV_PARAMS_DTYPE.names[:8] == (
            "rect", "coeffs", "luma_texel_scale", "chroma_texel_scale",
            "chroma_offset_texels", "chroma_scale", "chroma_offset", "pad")

    def test_parameters_of_a_1_to_1_picture(self) -> None:
        values = M.build_yuv_params(_frame(), (0, 0, W, H))
        assert values.dtype == M.YUV_PARAMS_DTYPE and values.shape == (1,)
        assert values["rect"][0].tolist() == [0.0, 0.0, W, H]
        np.testing.assert_allclose(values["coeffs"][0], [1.5748, -0.1873, -0.4681, 1.8556], rtol=0, atol=1e-6)  # BT.709
        assert values["luma_texel_scale"][0].tolist() == [1.0, 1.0]                       # exact
        assert values["chroma_texel_scale"][0].tolist() == [0.5, 0.5]
        assert values["chroma_offset_texels"][0].tolist() == [0.0, 0.0]
        assert values["color_range"][0] == 0 and values["gbr_mode"][0] == 0
        assert values["max_val"][0] == 255.0

    def test_parameters_follow_the_picture_rectangle(self) -> None:
        frame = _frame(sub="422", depth=10, matrix=9, color_range=1)
        rect = (8, 0, 96, 54)
        values = M.build_yuv_params(frame, rect)
        assert values["rect"][0].tolist() == [8.0, 0.0, 96.0, 54.0]
        assert values["luma_texel_scale"][0].tolist() == [float(np.float32(W / 96)), float(np.float32(H / 54))]
        assert values["chroma_texel_scale"][0].tolist() == [float(np.float32(W / 2 / 96)), float(np.float32(H / 54))]
        np.testing.assert_allclose(values["coeffs"][0], [1.4746, -0.1646, -0.5714, 1.8814], rtol=0, atol=1e-6)  # BT.2020
        assert values["color_range"][0] == 1 and values["gbr_mode"][0] == 0
        assert values["max_val"][0] == 1023.0

    def test_gbr_planes_are_flagged(self) -> None:
        frame = _frame(sub="444", depth=10, pixel_format="gbrp10le", color_range=1)
        assert M.build_yuv_params(frame, (0, 0, W, H))["gbr_mode"][0] == 1

    def test_the_chroma_phase_is_handed_over_in_texels(self) -> None:
        frame = _frame(sub="422", depth=10, chroma_siting=2)              # offset 1/128 of a chroma plane
        scale, offset = M.chroma_sampling(frame)
        values = M.build_yuv_params(frame, (0, 0, W, H))
        assert values["chroma_scale"][0].tolist() == list(scale) and values["chroma_offset"][0].tolist() == list(offset)
        assert values["chroma_offset_texels"][0].tolist() == [offset[0] * (W // 2), 0.0]
        assert values["chroma_offset_texels"][0][0] == 0.25                # a quarter of a chroma texel

    def test_a_refused_frame_never_reaches_the_kernel(self) -> None:
        with pytest.raises(ValueError, match="matrix_unsupported:4"):
            M.build_yuv_params(_frame(matrix=4), (0, 0, W, H))

    def test_the_cache_key_changes_with_everything_that_changes_the_picture(self) -> None:
        base = _frame(sub="422", depth=10)
        rect = (0, 0, W, H)
        key = M.params_key(base, rect)
        assert key == M.params_key(_frame(sub="422", depth=10), rect)
        assert key != M.params_key(base, (0, 0, 96, 54))                               # rectangle
        assert key != M.params_key(_frame(sub="422", depth=12), rect)                 # depth
        assert key != M.params_key(_frame(sub="420", depth=10), rect)                 # subsampling
        assert key != M.params_key(_frame(sub="422", depth=10, matrix=9), rect)       # matrix
        assert key != M.params_key(_frame(sub="422", depth=10, color_range=1), rect)  # range
        assert key != M.params_key(_frame(sub="422", depth=10, chroma_siting=2), rect)
        assert key != M.params_key(_frame(sub="422", depth=10, pixel_format="gbrp10le"), rect)
        assert key != M.params_key(_frame(sub="422", depth=10, source_width=128), rect)
