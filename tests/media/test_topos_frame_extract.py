"""Zero-reencode Topos MOV sample → .toos extraction tests."""

from __future__ import annotations

import ctypes

import numpy as np
import pytest

from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import (
    TOPOS_CODEC_ABI_VERSION,
    ToposCodec,
    ToposMuxFile,
)
from src.shared.codec.topos_image_binding import ToposImageCodec
from src.shared.media.topos_frame_extract import (
    ToposFrameExtractor,
    extract_topos_frame_to_toos,
)


def _lib_available() -> bool:
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


pytestmark = pytest.mark.skipif(
    not _lib_available(), reason="libtopos_codec 未构建"
)


def _make_movie(codec: ToposCodec, path: str, coding_selection: int = 0) -> list[bytes]:
    width, height = 64, 40
    frame_cfg = topos_binding._CFrameConfig()
    frame_cfg.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
    frame_cfg.abi_version = TOPOS_CODEC_ABI_VERSION
    frame_cfg.visible_width = width
    frame_cfg.visible_height = height
    frame_cfg.qp_base = 20
    frame_cfg.qmatrix_id = 1
    frame_cfg.profile = 3
    frame_cfg.bit_depth = 10
    frame_cfg.pixel_format = 0
    frame_cfg.color_range = 1
    frame_cfg.color_primaries = 1
    frame_cfg.color_transfer = 1
    frame_cfg.color_matrix = 1
    frame_cfg.sar_num = 1
    frame_cfg.sar_den = 1
    frame_cfg.reserved[0] = coding_selection  # 0=V1 legacy；8=V7-R2；9=V8

    movie_cfg = codec.movie_config(
        width, height, qp=20, profile=3, qmatrix=1,
        color_range=1, color_primaries=1, color_transfer=1,
        color_matrix=1, timescale=24, bit_depth=10, pixel_format=0,
    )
    packets: list[bytes] = []
    with ToposMuxFile(codec, path, movie_cfg) as mux:
        for index in range(3):
            y = np.full((height, width), 300 + index * 17, dtype="<u2")
            u = np.full((height, width // 2), 480 + index * 11, dtype="<u2")
            v = np.full((height, width // 2), 520 + index * 13, dtype="<u2")
            packet, _ = codec.encode_frame(
                frame_cfg, [y.tobytes(), u.tobytes(), v.tobytes()]
            )
            packets.append(packet)
            mux.add_packet(packet, pts=index, dur=1)
        mux.finish()
    return packets


def test_extract_wraps_original_packet_without_decode(tmp_path) -> None:
    codec = ToposCodec()
    movie_path = str(tmp_path / "source.mov")
    packets = _make_movie(codec, movie_path)
    output = tmp_path / "frame_0002.toos"

    result = extract_topos_frame_to_toos(movie_path, 1, str(output))

    assert result.frame_index == 1
    assert result.pts == 1
    assert result.duration == 1
    assert result.image_profile == 0
    assert result.packet_bytes == len(packets[1])
    assert result.file_bytes == output.stat().st_size

    image_codec = ToposImageCodec()
    assert image_codec.validate(str(output), deep=True).visible_width == 64
    wrapped_planes, _, _ = image_codec.decode(str(output))
    direct_planes = codec.decode(packets[1]).planes
    assert [a.tobytes() == b for a, b in zip(wrapped_planes, direct_planes)] == [
        True, True, True
    ]


def test_extract_forwards_source_tier_to_tmet(tmp_path) -> None:
    """v1.7：源 MOV 的 tpcD 档位随帧提取转发进 .toos 的 TMET chunk。

    提取零重编码——档位语义不变，转发后读侧（图片源探测）恢复同一
    展示名；旧源（无 tpcD）落 tier_id=0 + vendor。"""
    from src.shared.codec.topos_image_binding import get_image_codec
    from src.shared.codec.topos_profiles import (
        get_topos_tier,
        topos_tier_numeric_id,
    )

    codec = ToposCodec()
    movie_path = str(tmp_path / "source_hq.mov")
    width, height = 64, 40
    frame_cfg = topos_binding._CFrameConfig()
    frame_cfg.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
    frame_cfg.abi_version = TOPOS_CODEC_ABI_VERSION
    frame_cfg.visible_width = width
    frame_cfg.visible_height = height
    frame_cfg.qp_base = 20
    frame_cfg.qmatrix_id = 1
    frame_cfg.profile = 3
    frame_cfg.bit_depth = 10
    frame_cfg.pixel_format = 0
    frame_cfg.color_range = 1
    frame_cfg.color_primaries = 1
    frame_cfg.color_transfer = 1
    frame_cfg.color_matrix = 1
    frame_cfg.sar_num = 1
    frame_cfg.sar_den = 1
    movie_cfg = codec.movie_config(
        width, height, qp=20, profile=3, qmatrix=1,
        color_range=1, color_primaries=1, color_transfer=1,
        color_matrix=1, timescale=24, bit_depth=10, pixel_format=0,
    )
    tier = get_topos_tier("hq")
    with ToposMuxFile(codec, movie_path, movie_cfg) as mux:
        y = np.full((height, width), 320, dtype="<u2")
        u = np.full((height, width // 2), 480, dtype="<u2")
        v = np.full((height, width // 2), 520, dtype="<u2")
        packet, _ = codec.encode_frame(
            frame_cfg, [y.tobytes(), u.tobytes(), v.tobytes()]
        )
        mux.add_packet(packet, pts=0, dur=1)
        mux.set_movie_meta(tier_id=topos_tier_numeric_id("hq"),
                           vendor="TOPOS", label=tier.label)
        mux.finish()

    output = tmp_path / "frame_hq.toos"
    extract_topos_frame_to_toos(movie_path, 0, str(output))

    meta = get_image_codec().read_export_meta(str(output))
    assert meta is not None
    assert meta["tier_id"] == topos_tier_numeric_id("hq")
    assert meta["vendor"] == "TOPOS"
    assert meta["label"] == "Topos 422 HQ"


@pytest.mark.parametrize("coding_selection,major", [(8, 7), (9, 8)])
def test_extract_production_entropy_movies(tmp_path, coding_selection: int,
                                           major: int) -> None:
    """生产熵档（V7-R2/V8）MOV → .toos 零重编码提取。

    TPIM v1 信封解封内层 V1..V8（spec §14 内层版本独立演进）之前，
    现行生产 MOV（默认 V7-R2）的帧提取会撞 IDSC payload_major 拒收——
    本测试钉死解封后语义：提取 == 原包字节、信封解码 == 直接帧解码。
    """
    codec = ToposCodec()
    movie_path = str(tmp_path / "source.mov")
    packets = _make_movie(codec, movie_path, coding_selection=coding_selection)
    assert int(packets[1][6]) == major  # 包头 major 与配置一致
    output = tmp_path / f"frame_{coding_selection}.toos"

    result = extract_topos_frame_to_toos(movie_path, 2, str(output))

    assert result.image_profile == 0
    assert result.packet_bytes == len(packets[2])
    image_codec = ToposImageCodec()
    image_codec.validate(str(output), deep=True)
    wrapped_planes, _, _ = image_codec.decode(str(output))
    direct_planes = codec.decode(packets[2]).planes
    assert [a.tobytes() == b for a, b in zip(wrapped_planes, direct_planes)] == [
        True, True, True
    ]


def test_reusable_extractor_rejects_overwrite_and_bad_requests(tmp_path) -> None:
    codec = ToposCodec()
    movie_path = str(tmp_path / "source.mov")
    _make_movie(codec, movie_path)
    output = tmp_path / "frame.toos"

    with ToposFrameExtractor(movie_path, codec=codec) as extractor:
        assert extractor.frame_count == 3
        extractor.extract(0, str(output))
        with pytest.raises(FileExistsError):
            extractor.extract(0, str(output))
        extractor.extract(0, str(output), overwrite=True, durable=False)
        with pytest.raises(IndexError):
            extractor.extract(3, str(tmp_path / "bad.toos"))
        with pytest.raises(ValueError, match=r"\.toos"):
            extractor.extract(0, str(tmp_path / "bad.png"))
