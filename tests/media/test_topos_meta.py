"""topos_meta：tpcD / TMET 共享载荷编解码（container_spec v1.7）。

- encode ↔ decode 往返逐字节自洽；
- 解码严格镜像 C 侧规则（magic/version/保留域/tier 域/串长一致性/CRC），
  任何违反抛 ToposMetaError；
- 图片档位命名（'toos Low 10bit' 口径）与 MOV 视频档位 id 互不影响。
"""

from __future__ import annotations

import struct
import zlib

import pytest

from src.shared.codec.topos_meta import (
    LABEL_MAX,
    TOPOS_IMAGE_META_CHUNK,
    TOPOS_IMAGE_META_CHUNK_TYPE,
    TOPOS_IMAGE_TIER_IDS,
    ToposMetaError,
    decode_topos_meta,
    encode_topos_meta,
    topos_image_tier_label,
)


class TestPayloadRoundtrip:
    def test_encode_decode_roundtrip(self) -> None:
        payload = encode_topos_meta(tier_id=4, vendor="TOPOS", label="Topos 422 HQ")
        decoded = decode_topos_meta(payload)
        assert decoded == {"tier_id": 4, "vendor": "TOPOS", "label": "Topos 422 HQ"}

    def test_lp_tier_roundtrip(self) -> None:
        """v1.8（ADR-C056）：tier 域扩展到 7（TC_TIER_LP，帧间档）。"""
        payload = encode_topos_meta(tier_id=7, vendor="TOPOS", label="Topos 422 LP")
        assert decode_topos_meta(payload) == {
            "tier_id": 7, "vendor": "TOPOS", "label": "Topos 422 LP",
        }

    def test_vendor_only(self) -> None:
        payload = encode_topos_meta(vendor="TOPOS")
        assert decode_topos_meta(payload) == {
            "tier_id": 0, "vendor": "TOPOS", "label": "",
        }

    def test_encode_rejects_out_of_domain(self) -> None:
        with pytest.raises(ToposMetaError):
            encode_topos_meta(tier_id=9)   # M4-R5：域 0..8（RAW=8 合法），9 越界
        with pytest.raises(ToposMetaError):
            encode_topos_meta(vendor="x" * 16)
        with pytest.raises(ToposMetaError):
            encode_topos_meta(label="x" * (LABEL_MAX + 1))


class TestDecodeStrictness:
    """解码镜像 C 侧 parse 规则：任何字段违反都拒收。"""

    def _corrupt(self, payload: bytes, offset: int, value: int) -> bytes:
        bad = bytearray(payload)
        bad[offset] = value
        return bytes(bad)

    def test_bad_magic(self) -> None:
        with pytest.raises(ToposMetaError):
            decode_topos_meta(self._corrupt(encode_topos_meta(tier_id=1), 0, ord("X")))

    def test_bad_version(self) -> None:
        with pytest.raises(ToposMetaError):
            decode_topos_meta(self._corrupt(encode_topos_meta(tier_id=1), 5, 2))

    def test_bad_reserved(self) -> None:
        with pytest.raises(ToposMetaError):
            decode_topos_meta(self._corrupt(encode_topos_meta(tier_id=1), 7, 1))

    def test_bad_tier(self) -> None:
        with pytest.raises(ToposMetaError):
            decode_topos_meta(self._corrupt(encode_topos_meta(tier_id=1), 6, 99))

    def test_length_field_mismatch(self) -> None:
        payload = encode_topos_meta(tier_id=1, vendor="TOPOS")
        bad = self._corrupt(payload, 9, 4)  # vendor_len 5 → 4，总长不再一致
        with pytest.raises(ToposMetaError):
            decode_topos_meta(bad)

    def test_crc_mismatch(self) -> None:
        payload = bytearray(encode_topos_meta(tier_id=2, vendor="TOPOS",
                                              label="Topos 422"))
        payload[12] ^= 0x01  # vendor 首字节（CRC 覆盖域）
        with pytest.raises(ToposMetaError):
            decode_topos_meta(bytes(payload))

    def test_truncated(self) -> None:
        with pytest.raises(ToposMetaError):
            decode_topos_meta(encode_topos_meta(tier_id=1)[:-1])


class TestImageTierNaming:
    def test_chunk_fourcc(self) -> None:
        assert TOPOS_IMAGE_META_CHUNK == b"TMET"
        assert TOPOS_IMAGE_META_CHUNK_TYPE == int.from_bytes(b"TMET", "big")

    def test_tier_ids(self) -> None:
        assert TOPOS_IMAGE_TIER_IDS == {"low": 1, "medium": 2, "high": 3,
                                        "ultra": 4}

    @pytest.mark.parametrize(
        "tier_id, bit_depth, expected",
        [
            (1, 10, "toos Low 10bit"),
            (2, 10, "toos Medium 10bit"),
            (3, 10, "toos High 10bit"),
            (4, 10, "toos Ultra 10bit"),
            (1, 12, "toos Low 12bit"),
            (3, 12, "toos High 12bit"),
            (4, 12, "toos Ultra 12bit"),
        ],
    )
    def test_label_composition(self, tier_id: int, bit_depth: int,
                               expected: str) -> None:
        assert topos_image_tier_label(tier_id, bit_depth) == expected

    @pytest.mark.parametrize(
        "tier_id, bit_depth, expected",
        [
            (1, 16, "toos Low 16bit float"),
            (2, 16, "toos Medium 16bit float"),
            (3, 16, "toos High 16bit float"),
            (4, 16, "toos Ultra 16bit float"),
        ],
    )
    def test_float_label_composition(self, tier_id: int, bit_depth: int,
                                     expected: str) -> None:
        """v1.8（ADR-C057）：浮点样本域展示名——'toos <Quality> 16bit float'。

        与整数阶梯共用 tier id 1..4，浮点域由 IDSC sample_kind=HALF
        自描述；未来 FLOAT32 同词表换位深（'toos Low 32bit float'）。"""
        assert topos_image_tier_label(tier_id, bit_depth,
                                      floating=True) == expected
        # 整数口径不受影响（同 id 不带后缀）
        assert "float" not in topos_image_tier_label(tier_id, bit_depth)

    def test_float_label_roundtrips_through_payload(self) -> None:
        label = topos_image_tier_label(4, 16, floating=True)
        payload = encode_topos_meta(tier_id=4, vendor="TOPOS", label=label)
        decoded = decode_topos_meta(payload)
        assert decoded["tier_id"] == 4
        assert decoded["label"] == "toos Ultra 16bit float"

    def test_unknown_tier_returns_none(self) -> None:
        assert topos_image_tier_label(0, 10) is None
        assert topos_image_tier_label(99, 10) is None

    def test_label_roundtrips_through_payload(self) -> None:
        label = topos_image_tier_label(3, 12)
        payload = encode_topos_meta(tier_id=3, vendor="TOPOS", label=label)
        assert decode_topos_meta(payload)["label"] == "toos High 12bit"
