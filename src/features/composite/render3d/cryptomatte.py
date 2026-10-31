"""Small, render-thread-independent Cryptomatte data contract.

This module does not allocate a GPU pass. It provides the stable name hashing
and manifest metadata needed to pair the existing object-ID texture with a
future Cryptomatte multi-layer writer. Keeping it pure Python makes the
contract testable without an OpenGL context and prevents names/manifest JSON
from leaking across the render/UI boundary as mutable scene objects.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_UINT32_MASK = 0xFFFFFFFF


def _rotate_left(value: int, bits: int) -> int:
    value &= _UINT32_MASK
    return ((value << bits) | (value >> (32 - bits))) & _UINT32_MASK


def murmurhash3_32(value: str, seed: int = 0) -> int:
    """Return the deterministic MurmurHash3 x86 32-bit hash of ``value``."""
    data = str(value).encode("utf-8", "surrogatepass")
    h1 = int(seed) & _UINT32_MASK
    c1 = 0xCC9E2D51
    c2 = 0x1B873593

    block_end = len(data) - (len(data) % 4)
    for offset in range(0, block_end, 4):
        k1 = int.from_bytes(data[offset:offset + 4], "little")
        k1 = (k1 * c1) & _UINT32_MASK
        k1 = _rotate_left(k1, 15)
        k1 = (k1 * c2) & _UINT32_MASK
        h1 ^= k1
        h1 = _rotate_left(h1, 13)
        h1 = (h1 * 5 + 0xE6546B64) & _UINT32_MASK

    tail = data[block_end:]
    k1 = 0
    if len(tail) == 3:
        k1 ^= tail[2] << 16
    if len(tail) >= 2:
        k1 ^= tail[1] << 8
    if tail:
        k1 ^= tail[0]
        k1 = (k1 * c1) & _UINT32_MASK
        k1 = _rotate_left(k1, 15)
        k1 = (k1 * c2) & _UINT32_MASK
        h1 ^= k1

    h1 ^= len(data)
    h1 ^= h1 >> 16
    h1 = (h1 * 0x85EBCA6B) & _UINT32_MASK
    h1 ^= h1 >> 13
    h1 = (h1 * 0xC2B2AE35) & _UINT32_MASK
    h1 ^= h1 >> 16
    return h1 & _UINT32_MASK


def uint32_to_safe_float(bits: int) -> float:
    """hash 位模式 → 有限 float（N-10）。

    直接 reinterpret 32 位 hash 有约 0.4% 概率落进 NaN/Inf 指数段
    （object_440/572/924 实测）——NaN 与自身比较不相等，使合法对象
    无法被选择/取消，json 默认还把它写成非标准 ``NaN``。指数压缩到
    [1, 254]、符号清零：转换确定、float 恒有限；选择/匹配按位模式
    （floatBitsToUint / struct uint32）进行，压缩不引入误选。
    """
    bits = int(bits) & _UINT32_MASK
    exponent = (bits >> 23) & 0xFF
    exponent = 1 if exponent < 1 else (254 if exponent > 254 else exponent)
    safe = (bits & 0x007FFFFF) | (exponent << 23)
    return struct.unpack("<f", struct.pack("<I", safe))[0]


def cryptomatte_float_id(name: str) -> float:
    """Return the finite float ID used by Cryptomatte shaders (N-10)."""
    hash_value = murmurhash3_32(name)
    return uint32_to_safe_float(hash_value)


@dataclass(frozen=True)
class CryptomatteEntry:
    """One stable object name and its Cryptomatte hash."""

    name: str
    hash_value: int

    @property
    def hex_id(self) -> str:
        """Eight-digit manifest key used by Cryptomatte metadata."""
        return f"{int(self.hash_value) & _UINT32_MASK:08x}"

    @property
    def float_id(self) -> float:
        """Finite float representation for a shader/AOV writer (N-10)."""
        return uint32_to_safe_float(int(self.hash_value) & _UINT32_MASK)


@dataclass(frozen=True)
class CryptomatteManifest:
    """Stable object manifest for one Cryptomatte layer."""

    layer_name: str = "crypto_object"
    entries: tuple[CryptomatteEntry, ...] = ()

    @classmethod
    def from_names(
        cls, names: Iterable[str], layer_name: str = "crypto_object"
    ) -> "CryptomatteManifest":
        normalized = sorted({
            str(name).strip() for name in names if str(name).strip()
        })
        entries: list[CryptomatteEntry] = []
        seen_hashes: dict[int, str] = {}
        for name in normalized:
            hash_value = murmurhash3_32(name)
            collision = seen_hashes.get(hash_value)
            if collision is not None and collision != name:
                raise ValueError(
                    "Cryptomatte hash collision: "
                    f"{collision!r} and {name!r}"
                )
            seen_hashes[hash_value] = name
            entries.append(CryptomatteEntry(name, hash_value))
        return cls(str(layer_name or "crypto_object"), tuple(entries))

    @property
    def manifest(self) -> dict[str, str]:
        """Return the JSON manifest mapping hash hex IDs to object names."""
        return {entry.hex_id: entry.name for entry in self.entries}

    def to_metadata(self) -> dict[str, Any]:
        """Return JSON-safe metadata for a Cryptomatte-capable writer."""
        return {
            "layer": self.layer_name,
            "hash_algorithm": "MurmurHash3_32",
            "manifest": json.dumps(
                self.manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "object_count": len(self.entries),
        }


def _object_name(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("path", "node_path", "name", "node_name", "node_id"):
            name = str(value.get(key, "") or "").strip()
            if name:
                return name
        return ""
    return str(value or "").strip()


def build_cryptomatte_manifest(
    objects: Iterable[Any], layer_name: str = "crypto_object"
) -> CryptomatteManifest:
    """Build a manifest from scene-node names or flat-mesh dictionaries."""
    return CryptomatteManifest.from_names(
        (_object_name(value) for value in objects), layer_name=layer_name
    )
