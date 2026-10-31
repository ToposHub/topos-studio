from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.shared.export.gpu_encode_surface import GpuEncodeSurface
from src.shared.media.gpu_frame import (
    GpuDecodedFrame,
    GpuFramePlane,
    GpuFrameRelease,
    GpuPlaneResource,
)


def _frame(device: str = "metal-device-0") -> GpuDecodedFrame:
    return GpuDecodedFrame(
        frame_index=7,
        pts=21,
        time_seconds=0.84,
        width=16,
        height=8,
        visible_rect=None,
        pixel_format="nv12",
        bit_depth=8,
        planes=(
            GpuFramePlane(
                "Y", "metal", 0, "2d", 16, 8,
                resource=GpuPlaneResource(
                    "metal", object(), "r8unorm", 16, 8,
                    device_identity=device,
                ),
            ),
        ),
        decode_path="macos_vt_cvmetal_zero_copy",
        zero_copy=True,
        release=GpuFrameRelease(),
        device_identity=device,
    )


def test_surface_validates_device_and_releases_exactly_once() -> None:
    frame = _frame()
    released = MagicMock()
    frame.release.release_callback = released
    surface, reason = GpuEncodeSurface.from_decoded_frame(
        frame,
        backend="videotoolbox",
        pixel_format="nv12",
        device_identity="metal-device-0",
        submit_callback=lambda value: "submitted",
    )
    assert reason == ""
    assert surface is not None
    assert surface.submit() == "submitted"
    surface.release()
    surface.release()
    released.assert_called_once()


def test_surface_rejects_cross_device_frame() -> None:
    surface, reason = GpuEncodeSurface.from_decoded_frame(
        _frame("decode-device"),
        backend="videotoolbox",
        pixel_format="nv12",
        device_identity="encode-device",
        submit_callback=lambda value: None,
    )
    assert surface is None
    assert reason.startswith("device_mismatch:")


def test_surface_cannot_submit_twice() -> None:
    # W01：submit 回调必须返回真值——falsy 返回按 AUD-33208 契约立即
    # release，第二次 submit 会先命中 "released" 守卫而非 "more than once"
    # （与平台无关；本用例测的是二次提交守卫本身）。
    surface, reason = GpuEncodeSurface.from_decoded_frame(
        _frame(), backend="videotoolbox", pixel_format="nv12",
        device_identity="metal-device-0", submit_callback=lambda value: True,
    )
    assert reason == ""
    assert surface is not None
    surface.submit()
    with pytest.raises(RuntimeError, match="more than once"):
        surface.submit()
    surface.release()
