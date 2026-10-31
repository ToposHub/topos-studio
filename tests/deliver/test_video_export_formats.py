"""Focused tests for the shared Deliver format/encoder matrix."""

from __future__ import annotations

from src.features.deliver.core.video_export_formats import (
    CODEC_CONTAINER_TABLE,
    allowed_codecs_for_container,
    is_codec_container_compatible,
)
from src.features.deliver.core.video_export_preflight import _check_codec_container


def test_mp4_exposes_only_supported_gop_codecs() -> None:
    assert allowed_codecs_for_container("mp4") == ["h264", "hevc"]
    assert "prores_422" not in allowed_codecs_for_container("mp4")


def test_ui_matrix_and_preflight_matrix_agree() -> None:
    for codec, containers in CODEC_CONTAINER_TABLE.items():
        for container in ("mp4", "mov", "mkv", "mxf", "avi", "webm"):
            expected = is_codec_container_compatible(codec, container)
            actual, _reason = _check_codec_container(codec, container)
            assert actual is expected, (codec, container)


def test_topos_444_tiers_in_container_matrix() -> None:
    """R4.6：Pro444/Extreme 进容器矩阵（自研 MOV only）。"""
    assert is_codec_container_compatible("topos_4444", "mov")
    assert is_codec_container_compatible("topos_4444xq", "mov")
    assert not is_codec_container_compatible("topos_4444", "mp4")
    assert "topos_4444" in allowed_codecs_for_container("mov")
    assert "topos_4444xq" in allowed_codecs_for_container("mov")


def test_prores_is_rejected_in_mp4_by_preflight() -> None:
    valid, reason = _check_codec_container("prores_422", "mp4")
    assert not valid
    assert reason is not None
