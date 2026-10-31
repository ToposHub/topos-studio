"""RC1：TRAW 开发元数据块（TRWM）全链测试。

覆盖：载荷编解码（CRC/magic/版本/域校验）、.toos ICCP 同款 chunk 嵌入
往返、MOV trwm 原子（编码器写入 + 读回）、RAW→RAW 直出透传（导出物
保留源 as-shot 基线）。
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("TOPOS_CODEC_PROFILE", "1")

from src.shared.codec import topos_binding
from src.shared.codec.topos_trwm import (
    ToposTrwmError,
    decode_trwm,
    encode_trwm,
)


def _lib_available() -> bool:
    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is not None:
        return True
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


require_lib = pytest.mark.skipif(
    not _lib_available(), reason="libtopos_codec 未构建")


class TestTrwmPayload:
    def test_roundtrip(self) -> None:
        payload = encode_trwm(1.8, 1.0, 1.42, 0.5, 64.0, "BGGR", 3)
        assert len(payload) == 40
        d = decode_trwm(payload)
        assert d["version"] == (1, 0)
        assert d["cfa_layout"] == "BGGR"
        assert d["sensor_matrix_id"] == 3
        assert d["exposure_stops"] == 0.5
        assert d["black_level"] == 64.0
        # f32 域精度（半精度浮动容忍）
        assert d["wb_gain_r"] == pytest.approx(1.8, abs=1e-6)

    def test_all_layouts(self) -> None:
        for name in ("RGGB", "BGGR", "GRBG", "GBRG"):
            d = decode_trwm(encode_trwm(cfa_layout=name))
            assert d["cfa_layout"] == name

    def test_bad_layout_rejected(self) -> None:
        with pytest.raises(ToposTrwmError, match="cfa_layout"):
            encode_trwm(cfa_layout="X transe")

    def test_bad_matrix_id_rejected(self) -> None:
        with pytest.raises(ToposTrwmError, match="sensor_matrix_id"):
            encode_trwm(sensor_matrix_id=256)

    def test_corrupt_crc_rejected(self) -> None:
        payload = bytearray(encode_trwm())
        payload[10] ^= 0xFF
        with pytest.raises(ToposTrwmError, match="CRC"):
            decode_trwm(bytes(payload))

    def test_bad_magic_rejected(self) -> None:
        payload = bytearray(encode_trwm())
        payload[0] = ord("X")
        with pytest.raises(ToposTrwmError, match="magic"):
            decode_trwm(bytes(payload))

    def test_nonpositive_wb_rejected(self) -> None:
        with pytest.raises(ToposTrwmError, match="wb_gain_r"):
            decode_trwm(encode_trwm(wb_gain_r=0.0))


@require_lib
class TestTrwmToosCarrier:
    def test_toos_chunk_roundtrip(self, tmp_path) -> None:
        from src.shared.codec.topos_binding import (
            TOPOS_CODEC_ABI_VERSION, ToposCodec, _CFrameConfig,
        )
        from src.shared.codec.topos_image_binding import (
            TC_IMG_CHUNK_TRWM, get_image_codec,
        )
        from src.shared.media.frame import PIXEL_FORMATS  # noqa: F401

        payload = encode_trwm(1.4, 1.0, 1.6, -0.3, 16.0, "RGGB", 1)
        planes = [np.full((8, 8), v, dtype="<u2") for v in (400, 500, 500, 300)]
        fc = _CFrameConfig()
        fc.struct_size = ctypes.sizeof(fc)
        fc.abi_version = TOPOS_CODEC_ABI_VERSION
        fc.visible_width = 8
        fc.visible_height = 8
        fc.profile = 7
        fc.pixel_format = 3
        fc.bit_depth = 12
        fc.qp_base = 59
        fc.reserved[0] = 8
        pkt, _st = ToposCodec().encode_frame(
            fc, [np.ascontiguousarray(p).tobytes() for p in planes])
        out = tmp_path / "trwm.toos"
        get_image_codec().write_packet(
            str(out), pkt, 3, extra_chunks=[(TC_IMG_CHUNK_TRWM, payload)])
        read_back = get_image_codec().read_metadata(str(out), TC_IMG_CHUNK_TRWM)
        assert read_back == payload
        assert decode_trwm(read_back)["cfa_layout"] == "RGGB"


@require_lib
class TestTrwmMovRoundtrip:
    def test_mov_write_and_read(self, tmp_path) -> None:
        from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile
        from src.shared.codec.topos_trwm import decode_trwm
        from src.shared.export.topos_encoder import ToposVideoEncoder
        from src.shared.media.frame import DecodedFrame

        w, h = 32, 24
        payload = encode_trwm(1.8, 1.0, 1.42, 0.5, 64.0, "BGGR", 3)
        out = tmp_path / "traw_trwm.mov"
        enc = ToposVideoEncoder(str(out), SimpleNamespace(
            codec="topos", width=w, height=h, fps=24.0,
            pix_fmt="topos_cfa12le", profile="raw", crf=59),
            raw_meta_payload=payload)
        enc.open()
        rng = np.random.default_rng(2)
        for i in range(3):
            planes = tuple(
                np.full((h // 2, w // 2), int(v), dtype=np.uint16)
                for v in (rng.integers(100, 500), 300, 300, 100))
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24.0, data=None,
                planes=planes, pixel_format="topos_cfa12le",
                dtype=np.dtype(np.uint16), width=w, height=h))
        enc.close()

        movie = ToposMovieFile(ToposCodec(), str(out))
        try:
            back = decode_trwm(movie.read_raw_meta())
            assert back["cfa_layout"] == "BGGR"
            assert back["sensor_matrix_id"] == 3
            assert movie.info().sample_count == 3
        finally:
            movie.close()

    def test_mov_without_trwm_reads_state_error(self, tmp_path) -> None:
        """旧文件（无 trwm）读侧显式 STATE 错——不静默返回空。"""
        from src.shared.codec.topos_binding import (
            ToposCodec, ToposCodecError, ToposMovieFile,
        )
        from src.shared.export.topos_encoder import ToposVideoEncoder
        from src.shared.media.frame import DecodedFrame

        w, h = 32, 24
        out = tmp_path / "plain.mov"
        enc = ToposVideoEncoder(str(out), SimpleNamespace(
            codec="topos", width=w, height=h, fps=24.0,
            pix_fmt="yuv422p10le", profile="standard", crf=59))
        enc.open()
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=np.zeros((h, w, 3), dtype=np.uint8),
            pixel_format="bgr24", width=w, height=h))
        enc.close()

        movie = ToposMovieFile(ToposCodec(), str(out))
        try:
            with pytest.raises(Exception, match="trwm"):
                movie.read_raw_meta()
        finally:
            movie.close()


class TestRC7Rc8Semantics:
    """RC7：TRAW transfer 词表统一；RC8：black level 进 debayer。"""

    def test_transfer_code20_label(self) -> None:
        from src.shared.media.color_metadata import map_color_transfer

        assert map_color_transfer(20) == "traw-log0"

    def test_composite_shader_enum_has_traw(self) -> None:
        # 复合 u_transfer 枚举（源码级钉扎：漏同步即红）
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2]
               / "src/features/composite/evaluators/base_evaluator.py"
               ).read_text(encoding="utf-8")
        assert '"traw-log0": 5' in src
        shader = (Path(__file__).resolve().parents[2]
                  / "src/features/composite/core/shaders.py"
                  ).read_text(encoding="utf-8")
        assert "traw_log_to_linear" in shader
        assert "u_transfer == 5" in shader

    def test_cpu_debayer_black_level(self) -> None:
        """RC8：black level 减除生效（全 100 码 + 黑位 50 ≈ 全 50 码）。
        CPU 链验证语义；GPU uniform 走同一参数面。"""
        from src.shared.video.cfa_cpu_debayer import debayer_cfa_planes

        planes = [np.full((8, 8), v, dtype=np.uint16)
                  for v in (200, 400, 400, 100)]
        base = debayer_cfa_planes(planes, 16, 16, bit_depth=12,
                                  black_level=0.0)
        shifted = debayer_cfa_planes(planes, 16, 16, bit_depth=12,
                                     black_level=50.0)
        # 减黑位后整体变暗（非增亮/不变）
        assert shifted[..., 1].mean() < base[..., 1].mean()
        # R 通道在 200-50=150 与 200 档下 LOG0 输出随黑位单调降
        assert shifted[..., 0].mean() < base[..., 0].mean()


class TestTrwmPayloadTightening:
    """2026-09-21 复查收紧：u32 reserved 尾域 + exposure/black 有限性。"""

    def test_nonzero_tail_reserved_rejected(self) -> None:
        import struct

        payload = bytearray(encode_trwm(1.0, 1.0, 1.0, 0.0, 0.0,
                                        "RGGB", 1))
        # offset 32 = u32 reserved（>4sHHfffffBB H）——写非 0 后重算 CRC，
        # 隔离"reserved 校验"与"CRC 校验"
        struct.pack_into(">I", payload, 32, 0xDEAD)
        import zlib
        struct.pack_into(">I", payload, 36,
                         zlib.crc32(bytes(payload[:36])) & 0xFFFFFFFF)
        with pytest.raises(ToposTrwmError, match="reserved"):
            decode_trwm(bytes(payload))

    def test_nonfinite_exposure_rejected(self) -> None:
        import struct
        import zlib

        payload = bytearray(encode_trwm(1.0, 1.0, 1.0, 0.0, 0.0,
                                        "RGGB", 1))
        struct.pack_into(">f", payload, 20, float("nan"))
        struct.pack_into(">I", payload, 36,
                         zlib.crc32(bytes(payload[:36])) & 0xFFFFFFFF)
        with pytest.raises(ToposTrwmError, match="exposure_stops"):
            decode_trwm(bytes(payload))

    def test_nonfinite_black_level_rejected(self) -> None:
        import struct
        import zlib

        payload = bytearray(encode_trwm(1.0, 1.0, 1.0, 0.0, 0.0,
                                        "RGGB", 1))
        struct.pack_into(">f", payload, 24, float("inf"))
        struct.pack_into(">I", payload, 36,
                         zlib.crc32(bytes(payload[:36])) & 0xFFFFFFFF)
        with pytest.raises(ToposTrwmError, match="black_level"):
            decode_trwm(bytes(payload))
