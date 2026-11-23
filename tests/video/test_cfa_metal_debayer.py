"""TRAW CFA debayer on Metal (plan 7.2), against the CPU reference.

The Metal debayer shares the semantics of ``cfa_cpu_debayer.debayer_cfa_planes``
and the GL ``CfaGpuDebayer``: RGGB bilinear demosaic, black level, 12-bit
log inverse or 16-bit normalisation, white balance and exposure, the IDT
gamut matrix, then the viewing gamma.  Output is RGBA16F, so the tolerance
is half-float rounding.  The gamut matrix is deliberately far from
symmetric: the first version of the kernel built it transposed (MSL's
``float3x3`` takes columns), which a symmetric matrix would have hidden.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("Metal")

from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.frame_transaction import FrameTransactionState  # noqa: F401 - resource contract lives next door
from src.shared.video.cfa_cpu_debayer import debayer_cfa_planes
from src.shared.video.cfa_metal_debayer import (
    SUPPORTED_BIT_DEPTHS,
    CfaDebayerParams,
    CfaMetalDebayer,
    CfaMetalDebayerError,
    cfa_metal_debayer_enabled,
    prepare_cfa_planes,
)
from tests.video.test_cfa_gpu_debayer import _synthetic_rggb

W, H = 32, 24
HALF_FLOAT = 5e-4          # a few fp16 ulps at 1.0

#: camera-native -> BT.709 linear, row-major and clearly not symmetric
GAMUT = np.array([[1.2, -0.20, -0.05],
                  [-0.05, 1.0, -0.10],
                  [-0.02, -0.30, 1.1]])


@pytest.fixture(scope="module")
def context():
    if not MetalComputeContext.is_supported():
        pytest.skip(f"no Metal device: {MetalComputeContext.availability_reason()[1]}")
    return MetalComputeContext()


@pytest.fixture()
def debayer(context):
    made = CfaMetalDebayer(context)
    yield made
    assert made.drain(timeout_s=5.0) is True
    made.release()


def _planes16(planes):
    return [(plane.astype(np.uint32) * 16).astype(np.uint16) for plane in planes]


def _metal(debayer, planes, **params) -> np.ndarray:
    """(H, W, 3) float64 from the Metal debayer."""
    frame = debayer.debayer(planes, W, H, params=CfaDebayerParams(**params), frame_index=9)
    image = debayer.readback(frame)
    assert image.dtype == np.float16 and image.shape == (H, W, 4)
    assert np.all(image[..., 3] == np.asarray(1.0, np.float16))
    return image[..., :3].astype(np.float64)


def _assert_matches(got: np.ndarray, reference: np.ndarray, tolerance: float = HALF_FLOAT) -> None:
    difference = np.abs(got - reference)
    assert difference.max() <= tolerance, (float(difference.max()), float(reference.min()), float(reference.max()))


class TestAgainstTheCpuReference:
    def test_12_bit_with_the_log_inverse(self, debayer) -> None:
        planes = _synthetic_rggb(W, H)
        got = _metal(debayer, planes, bit_depth=12, gamma_mode=0)
        reference = debayer_cfa_planes(planes, W, H, bit_depth=12, gamma_mode=0)
        assert 0.0 < float(reference.min()) and float(reference.max()) < 1.0   # a real signal
        _assert_matches(got, reference)

    @pytest.mark.parametrize(("gamma_mode", "name"), [(1, "gamma 2.2"), (2, "sRGB")])
    def test_viewing_gammas(self, debayer, gamma_mode: int, name: str) -> None:
        planes = _synthetic_rggb(W, H)
        got = _metal(debayer, planes, bit_depth=12, gamma_mode=gamma_mode)
        _assert_matches(got, debayer_cfa_planes(planes, W, H, bit_depth=12, gamma_mode=gamma_mode))

    def test_16_bit_normalises_the_codes(self, debayer) -> None:
        planes = _planes16(_synthetic_rggb(W, H, seed=7))
        got = _metal(debayer, planes, bit_depth=16, gamma_mode=0)
        _assert_matches(got, debayer_cfa_planes(planes, W, H, bit_depth=16, gamma_mode=0))

    def test_white_balance_exposure_and_black_level(self, debayer) -> None:
        planes = _synthetic_rggb(W, H)
        settings = dict(wb_gains=(1.5, 1.0, 0.8), exposure_stops=1.0, black_level=60.0)
        got = _metal(debayer, planes, bit_depth=12, gamma_mode=0, **settings)
        reference = debayer_cfa_planes(planes, W, H, bit_depth=12, gamma_mode=0, **settings)
        _assert_matches(got, reference)
        plain = debayer_cfa_planes(planes, W, H, bit_depth=12, gamma_mode=0)
        assert np.abs(reference - plain).max() > 0.05              # the settings do something

    def test_the_gamut_matrix_is_applied_row_major(self, debayer) -> None:
        planes = _synthetic_rggb(W, H)
        got = _metal(debayer, planes, bit_depth=12, gamma_mode=0, gamut_matrix=GAMUT)
        reference = debayer_cfa_planes(planes, W, H, bit_depth=12, gamma_mode=0, gamut_matrix=GAMUT)
        _assert_matches(got, reference)
        transposed = debayer_cfa_planes(planes, W, H, bit_depth=12, gamma_mode=0, gamut_matrix=GAMUT.T)
        # Far outside the tolerance: a transposed matrix would have shown.
        assert np.abs(reference - transposed).max() > 0.02

    def test_everything_together_in_the_display_gamma(self, debayer) -> None:
        planes = _synthetic_rggb(W, H, seed=3)
        settings = dict(wb_gains=(1.8, 1.0, 0.7), exposure_stops=-0.5, black_level=48.0,
                        gamut_matrix=GAMUT, gamma_mode=2)
        got = _metal(debayer, planes, bit_depth=12, **settings)
        _assert_matches(got, debayer_cfa_planes(planes, W, H, bit_depth=12, **settings))


class TestFrames:
    def test_a_frame_is_submitted_and_completes_once(self, debayer) -> None:
        planes = _synthetic_rggb(W, H)
        frame = debayer.debayer(planes, W, H, params=CfaDebayerParams(bit_depth=16, gamma_mode=0))
        assert frame.wait(5.0) and frame.completed and frame.error is None
        assert (frame.width, frame.height, frame.frame_index) == (W, H, -1)
        assert frame.native_descriptor().resource is frame.texture
        with pytest.raises(CfaMetalDebayerError, match="已提交过"):
            debayer.submit(frame)

    def test_several_frames_in_a_row_on_the_ring(self, debayer) -> None:
        """Three frames without waiting in between, each read back whole."""
        reference = None
        for seed in (1, 2, 3):
            planes = _synthetic_rggb(W, H, seed=seed)
            frame = debayer.debayer(planes, W, H, params=CfaDebayerParams(bit_depth=12, gamma_mode=1),
                                    frame_index=seed)
            image = debayer.readback(frame)
            reference = debayer_cfa_planes(planes, W, H, bit_depth=12, gamma_mode=1)
            _assert_matches(image[..., :3].astype(np.float64), reference)
        assert debayer.in_flight == 0

    def test_a_frame_is_released_exactly_once(self, debayer) -> None:
        frame = debayer.debayer(_synthetic_rggb(16, 16), 16, 16, params=CfaDebayerParams(bit_depth=16))
        assert frame.idle is False                   # still on the GPU
        assert frame.wait(5.0) is True
        assert frame.idle is True                    # done, and nobody holds it
        frame.retain()
        assert frame.retained is True and frame.idle is False
        frame.release()
        assert frame.retained is False and frame.idle is True
        frame.release()                              # the second call is a no-op
        assert frame.retained is False

    def test_after_release_nothing_is_encoded(self, context) -> None:
        made = CfaMetalDebayer(context)
        assert made.drain(timeout_s=5.0) is True
        made.release()
        with pytest.raises(CfaMetalDebayerError, match="已释放"):
            made.encode(_synthetic_rggb(16, 16), 16, 16, params=CfaDebayerParams())


class TestInputChecks:
    def test_planes_must_be_four_integer_planes_of_half_the_frame(self) -> None:
        planes = _synthetic_rggb(W, H)
        assert [plane.shape for plane in prepare_cfa_planes(planes, W, H)] == [(H // 2, W // 2)] * 4
        with pytest.raises(ValueError, match="4 相位平面"):
            prepare_cfa_planes(planes[:3], W, H)
        with pytest.raises(ValueError, match="整数 dtype"):
            prepare_cfa_planes([plane.astype(np.float32) for plane in planes], W, H)
        with pytest.raises(ValueError, match="形状"):
            prepare_cfa_planes(planes, W + 2, H)
        with pytest.raises(ValueError, match="尺寸必须为正"):
            prepare_cfa_planes(planes, 0, H)

    def test_an_odd_frame_size_takes_ceil_half_planes(self) -> None:
        planes = [np.zeros((3, 4), np.uint16) for _ in range(4)]   # 7 x 5 frame
        assert [plane.shape for plane in prepare_cfa_planes(planes, 7, 5)] == [(3, 4)] * 4

    @pytest.mark.parametrize("change", [
        {"bit_depth": 10}, {"gamma_mode": 3}, {"wb_gains": (1.0, 1.0)},
        {"exposure_stops": float("nan")},
        {"gamut_matrix": np.eye(2)},
        {"gamut_matrix": np.array([[1.0, 0.0, float("inf")], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])},
    ])
    def test_parameters_outside_the_contract_are_refused(self, debayer, change: dict) -> None:
        params = CfaDebayerParams(**{"bit_depth": 12, **change})
        with pytest.raises(ValueError):
            debayer.encode(_synthetic_rggb(16, 16), 16, 16, params=params)

    def test_the_upload_is_bounded(self, context) -> None:
        made = CfaMetalDebayer(context, max_upload_bytes=1024)
        try:
            with pytest.raises(ValueError, match="超过上限"):
                made.encode(_synthetic_rggb(W, H), W, H, params=CfaDebayerParams())   # 4 x 16x12 x 2 B
        finally:
            assert made.drain(timeout_s=5.0) is True
            made.release()

    def test_construction_is_checked(self, context) -> None:
        with pytest.raises(ValueError, match="输出格式"):
            CfaMetalDebayer(context, output_format="rgba8")
        with pytest.raises(ValueError, match="ring_size"):
            CfaMetalDebayer(context, ring_size=1)
        with pytest.raises(ValueError, match="max_upload_bytes"):
            CfaMetalDebayer(context, max_upload_bytes=0)

    def test_supported_depths_are_12_and_16(self) -> None:
        assert SUPPORTED_BIT_DEPTHS == (12, 16)


class TestSwitch:
    def test_off_unless_asked_for(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOPOS_CFA_METAL_DEBAYER", raising=False)
        assert cfa_metal_debayer_enabled() is False
        monkeypatch.setenv("TOPOS_CFA_METAL_DEBAYER", "0")
        assert cfa_metal_debayer_enabled() is False
        monkeypatch.setenv("TOPOS_CFA_METAL_DEBAYER", "1")
        assert cfa_metal_debayer_enabled() is True
