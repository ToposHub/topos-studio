"""P0-06：BWF bext chunk（EBU Tech 3285）单元测试。

用独立的字节级断言验证（不依赖 PyAV）：
- bext 固定头 602 字节布局与字段偏移
- 注入后 RIFF 尺寸修正、chunk 可被独立解析器读回
- 奇数长度 coding history 的 word 对齐
- 非 RIFF/WAVE 文件被拒绝且原文件不变
"""

from __future__ import annotations

import struct

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.bwf import (
    BextMetadata,
    BEXT_FIXED_SIZE,
    build_bext_chunk,
    default_coding_history,
    inject_bext_chunk,
    read_bext_chunk,
)


def _minimal_wav() -> bytes:
    """16 字节 fmt chunk + 8 字节 data chunk 的最小 WAV。"""
    fmt_body = struct.pack("<HHIIHH", 1, 1, 48000, 48000, 2, 16)
    return (
        b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt_body) + 8 + 4) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        + b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
    )


class TestBuildBextChunk:
    def test_fixed_header_layout(self):
        meta = BextMetadata(
            description="ADR take",
            originator="Topos Color",
            originator_reference="ABC123",
            origination_date="2026-08-27",
            origination_time="14:30:00",
            time_reference_samples=123456,
            coding_history="A=PCM,F=48000,W=24,M=mono,T=Topos Color\r\n",
        )
        chunk = build_bext_chunk(meta)
        assert chunk[:4] == b"bext"
        size = struct.unpack("<I", chunk[4:8])[0]
        history = meta.coding_history.encode("ascii")
        assert size == BEXT_FIXED_SIZE + len(history) + (len(history) % 2)
        body = chunk[8:]
        assert len(body) == size
        # 字段偏移
        assert body[0:8] == b"ADR take"
        assert body[256:256 + 11] == b"Topos Color"
        assert body[288:288 + 6] == b"ABC123"
        assert body[320:330] == b"2026-08-27"
        assert body[330:338] == b"14:30:00"
        assert struct.unpack("<Q", body[338:346])[0] == 123456
        assert struct.unpack("<H", body[346:348])[0] == 0
        assert body[348:BEXT_FIXED_SIZE] == b"\x00" * 254
        assert body[BEXT_FIXED_SIZE:BEXT_FIXED_SIZE + 41] == meta.coding_history.encode("ascii")

    def test_odd_coding_history_padded(self):
        meta = BextMetadata(coding_history="A=PCM")  # 5 字节，奇数
        chunk = build_bext_chunk(meta)
        size = struct.unpack("<I", chunk[4:8])[0]
        assert size % 2 == 0
        assert chunk[8:][BEXT_FIXED_SIZE:BEXT_FIXED_SIZE + 5] == b"A=PCM"

    def test_long_description_truncated(self):
        meta = BextMetadata(description="x" * 300)
        chunk = build_bext_chunk(meta)
        assert chunk[8:8 + 256] == b"x" * 256

    def test_defaults_fill_date_time_and_reference(self):
        meta = BextMetadata()
        assert len(meta.origination_date) == 10
        assert len(meta.origination_time) == 8
        assert meta.originator_reference


class TestInjectAndRead:
    def test_inject_then_read_roundtrip(self, tmp_path):
        wav = tmp_path / "take.wav"
        wav.write_bytes(_minimal_wav())
        original_size = len(_minimal_wav())

        meta = BextMetadata(
            description="roundtrip",
            time_reference_samples=48000 * 3600 + 5,
            coding_history=default_coding_history(48000, 24, 2),
        )
        assert inject_bext_chunk(str(wav), meta)

        data = wav.read_bytes()
        assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
        assert struct.unpack("<I", data[4:8])[0] == len(data) - 8
        history_len = len(meta.coding_history.encode("ascii"))
        assert len(data) == original_size + 8 + BEXT_FIXED_SIZE + history_len + (history_len % 2)
        # bext 位于 fmt 之后、data 之前
        assert data[12:16] == b"fmt "
        assert data[36:40] == b"bext"

        parsed = read_bext_chunk(str(wav))
        assert parsed is not None
        assert parsed.description == "roundtrip"
        assert parsed.time_reference_samples == 48000 * 3600 + 5
        assert parsed.coding_history == meta.coding_history
        assert parsed.originator == "Topos Color"

    def test_read_missing_bext_returns_none(self, tmp_path):
        wav = tmp_path / "plain.wav"
        wav.write_bytes(_minimal_wav())
        assert read_bext_chunk(str(wav)) is None

    def test_inject_rejects_non_wav(self, tmp_path):
        blob = tmp_path / "not.wav"
        blob.write_bytes(b"JUNKJUNKJUNKJUNK")
        assert inject_bext_chunk(str(blob), BextMetadata()) is False
        assert blob.read_bytes() == b"JUNKJUNKJUNKJUNK"  # 原文件不变

    def test_inject_rejects_missing_file(self, tmp_path):
        assert inject_bext_chunk(str(tmp_path / "missing.wav"), BextMetadata()) is False


class TestCapabilityConsistency:
    def test_interchange_capability_matches_writer(self):
        """P1-17 预热：能力表 BWF 声明必须与 writer 实际能力一致。"""
        from src.features.edit.core.interchange import INTERCHANGE_CAPABILITIES
        from src.features.edit.core.recording_manager import is_true_bwf_supported
        bwf = next(f for f in INTERCHANGE_CAPABILITIES if "BWF" in f.name)
        assert bwf.available == is_true_bwf_supported()
