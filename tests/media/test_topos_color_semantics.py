"""O1 scalar/reference tests for Topos color and alpha semantics."""
from __future__ import annotations

import numpy as np
import pytest

from src.shared.color.types import (
    ColorMetadataAuthority,
    ColorMatrix,
    ColorTransfer,
)
from src.shared.media.color_decode import (
    ColorConverter,
    ToposColorMetadataError,
    alpha_association,
    premultiply_alpha,
    resolve_topos_color_semantics,
    unpremultiply_alpha,
)


def _idsc(**overrides: int) -> dict[str, int]:
    value = {
        "pixel_format": 2,
        "bit_depth": 12,
        "color_range": 1,
        "color_primaries": 9,
        "color_transfer": 16,
        "color_matrix": 0,
        "chroma_siting": 0,
    }
    value.update(overrides)
    return value


def _icc(color_space: bytes = b"RGB ") -> bytes:
    profile = bytearray(132)
    profile[16:20] = color_space
    profile[36:40] = b"acsp"
    return bytes(profile)


def test_tpict_numeric_fields_are_authoritative_and_icc_applies_after_rgb() -> None:
    result = resolve_topos_color_semantics(_idsc(), icc_profile=_icc(), ocio_colorspace="scene_linear")
    assert result.color_matrix is ColorMatrix.GBR
    assert result.color_transfer is ColorTransfer.PQ
    assert result.display_authority is ColorMetadataAuthority.ICCP
    assert result.ocio_colorspace == "scene_linear"


def test_unknown_cicp_is_a_hard_error_instead_of_srgb_guess() -> None:
    with pytest.raises(ToposColorMetadataError, match="unknown CICP transfer"):
        resolve_topos_color_semantics(_idsc(color_transfer=99))


def test_gbr_and_yuv_matrix_combinations_are_checked() -> None:
    # TRAW 拍板（2026-09-13）：pf=3（CFA）同 GBR 走 identity 契约，
    # 错误信息合并为 "GBR/CFA payload"
    with pytest.raises(ToposColorMetadataError, match="GBR/CFA payload"):
        resolve_topos_color_semantics(_idsc(color_matrix=1))
    with pytest.raises(ToposColorMetadataError, match="YUV payload"):
        resolve_topos_color_semantics(_idsc(pixel_format=0, color_matrix=0))


def test_icc_and_ocio_validation_is_explicit() -> None:
    with pytest.raises(ToposColorMetadataError, match="acsp"):
        resolve_topos_color_semantics(_idsc(), icc_profile=b"not-an-icc")
    with pytest.raises(ToposColorMetadataError, match="path"):
        resolve_topos_color_semantics(_idsc(), ocio_colorspace="/tmp/look.ocio")
    with pytest.raises(ToposColorMetadataError, match="OCIO/CICP conflict"):
        resolve_topos_color_semantics(_idsc(), ocio_colorspace="scene_linear",
                                      ocio_cicp={"color_transfer": 1})


def test_transfer_api_separates_pq_absolute_domain_from_display_policy() -> None:
    converter = ColorConverter()
    pq = np.array([0.0, 1.0], dtype=np.float32)
    nits = converter.decode_transfer_to_linear(pq, ColorTransfer.PQ)
    assert float(nits[0]) == pytest.approx(0.0, abs=1e-6)
    assert float(nits[1]) == pytest.approx(10000.0, rel=1e-5)
    with pytest.raises(ToposColorMetadataError, match="pq_display_peak_nits"):
        converter.decode_to_display(pq, ColorTransfer.PQ)
    display = converter.decode_to_display(pq, ColorTransfer.PQ, pq_display_peak_nits=100.0)
    assert np.all((display >= 0.0) & (display <= 1.0))


def test_alpha_mode_and_zero_alpha_rgb_preservation() -> None:
    assert alpha_association(0).value == "absent"
    assert alpha_association(1).value == "straight"
    assert alpha_association(2).value == "premultiplied"
    with pytest.raises(ToposColorMetadataError):
        alpha_association(9)
    rgb = np.array([[[0.25, 0.5, 0.75], [0.1, 0.2, 0.3]]], dtype=np.float32)
    alpha = np.array([[0.0, 0.5]], dtype=np.float32)
    premultiplied = premultiply_alpha(rgb, alpha)
    assert np.allclose(premultiplied[0, 0], 0.0)
    # File decode/unpremultiply must preserve RGB at alpha=0.  The caller must
    # not expect an irreversible display premultiply to restore it.
    premultiplied[0, 0] = rgb[0, 0]  # preserved file RGB at alpha=0
    restored = unpremultiply_alpha(premultiplied, alpha)
    assert np.allclose(restored[0, 0], rgb[0, 0])
    assert np.allclose(restored[0, 1], rgb[0, 1])
