"""Deterministic segment render cache for Windows export scheduling.

The key deliberately includes every semantic input that can change pixels or
metadata.  The cache stores encoded/serialized artifacts, never raw GPU
handles; GPU source and intermediate reuse remains an in-process pool concern.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

__all__ = ["SegmentRenderKey", "RenderCacheStats", "DiskRenderCache"]


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_canonical(item) for item in sorted(value, key=repr)]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _canonical(value.to_dict())
    if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _freeze(value: object) -> object:
    """把字段值归一化为可哈希结构（P2-17）。

    dict → 按 key 排序的 (k, v) 元组；list/set → 元组；其余原样。
    归一化在构造时进行一次，使 ``__eq__`` / ``__hash__`` / ``digest``
    对 tuple/list、dict 等价形式判定一致（此前 from_mapping 读回的
    list 与构造时的 tuple 会出现 == 与 digest 不一致的裂缝）。
    """
    if isinstance(value, Mapping):
        return tuple(sorted(
            (str(k), _freeze(value[k])) for k in value.keys()
        ))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


@dataclass(frozen=True)
class SegmentRenderKey:
    """Immutable semantic identity for one renderable timeline segment.

    P2-17：所有结构字段在构造时经 :func:`_freeze` 归一化——键实例可直接
    做 dict key / set 成员（P2-18 精确失效图依赖），且等价比较、哈希与
    ``digest`` 三者永远一致。
    """

    source_fingerprint: str
    in_frame: int
    out_frame: int
    retime: object
    layer_stack: object
    effects: object
    grade: object
    color_management: object
    output_format: object
    renderer_backend: str
    renderer_version: str
    source_kind: str = "original"
    delivery: bool = True
    # P2-17 媒体版本：((path, size, mtime_ns), ...) —— 图引用的外部文件
    # （媒体/LUT/HDRI/模型）内容身份。文件被替换/重链接而图 JSON 不变时
    # 靠它避免旧帧误命中。
    media_versions: object = ()

    def __post_init__(self) -> None:
        if not self.source_fingerprint:
            raise ValueError("SegmentRenderKey 缺少 source_fingerprint")
        if self.in_frame < 0 or self.out_frame <= self.in_frame:
            raise ValueError("SegmentRenderKey 帧范围非法")
        if self.source_kind not in {"original", "proxy", "optimized"}:
            raise ValueError(f"未知 source_kind: {self.source_kind}")
        if not self.renderer_backend or not self.renderer_version:
            raise ValueError("SegmentRenderKey 缺少 renderer/backend 版本")
        if self.delivery and self.source_kind != "original":
            raise ValueError("delivery cache 默认禁止 proxy/optimized source")
        # 构造期归一化（frozen dataclass，用 object.__setattr__ 一次性写回）
        for field in (
            "retime", "layer_stack", "effects", "grade",
            "color_management", "output_format", "media_versions",
        ):
            object.__setattr__(self, field, _freeze(getattr(self, field)))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "SegmentRenderKey":
        required = {
            "source_fingerprint", "in_frame", "out_frame", "retime", "layer_stack",
            "effects", "grade", "color_management", "output_format",
            "renderer_backend", "renderer_version",
        }
        missing = required - set(payload)
        if missing:
            raise ValueError(f"SegmentRenderKey 缺少字段: {sorted(missing)}")
        field_values = {
            key: payload[key] for key in required if key not in {"in_frame", "out_frame"}
        }
        return cls(**field_values,  # type: ignore[arg-type]
                   in_frame=int(payload["in_frame"]),
                   out_frame=int(payload["out_frame"]),
                   source_kind=str(payload.get("source_kind", "original")),
                   delivery=bool(payload.get("delivery", True)),
                   media_versions=payload.get("media_versions", ()))

    def to_dict(self) -> dict[str, object]:
        return {
            "source_fingerprint": self.source_fingerprint,
            "in_frame": self.in_frame,
            "out_frame": self.out_frame,
            "retime": _canonical(self.retime),
            "layer_stack": _canonical(self.layer_stack),
            "effects": _canonical(self.effects),
            "grade": _canonical(self.grade),
            "color_management": _canonical(self.color_management),
            "output_format": _canonical(self.output_format),
            "renderer_backend": self.renderer_backend,
            "renderer_version": self.renderer_version,
            "source_kind": self.source_kind,
            "delivery": self.delivery,
            "media_versions": _canonical(self.media_versions),
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RenderCacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    corruptions: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "corruptions": self.corruptions,
        }


class DiskRenderCache:
    """Atomic, content-verified cache for serialized segment results."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._corruptions = 0

    @property
    def root(self) -> Path:
        return self._root

    def _paths(self, key: SegmentRenderKey) -> tuple[Path, Path]:
        stem = self._root / key.digest
        return stem.with_suffix(".bin"), stem.with_suffix(".json")

    def get(self, key: SegmentRenderKey) -> Optional[tuple[bytes, dict[str, object]]]:
        data_path, meta_path = self._paths(key)
        with self._lock:
            if not data_path.is_file() or not meta_path.is_file():
                self._misses += 1
                return None
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    raise ValueError("cache metadata must be an object")
                data = data_path.read_bytes()
                if metadata.get("key_digest") != key.digest:
                    raise ValueError("key_digest mismatch")
                if metadata.get("sha256") != hashlib.sha256(data).hexdigest():
                    raise ValueError("artifact sha256 mismatch")
                if int(metadata.get("size", -1)) != len(data):
                    raise ValueError("artifact size mismatch")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self._corruptions += 1
                self._misses += 1
                return None
            nested = metadata.get("metadata", {})
            if not isinstance(nested, dict):
                self._corruptions += 1
                self._misses += 1
                return None
            self._hits += 1
            return data, dict(nested)

    def put(
        self,
        key: SegmentRenderKey,
        data: bytes,
        metadata: Optional[Mapping[str, object]] = None,
    ) -> None:
        if not isinstance(data, bytes):
            raise TypeError("render cache 只接受 bytes artifact")
        data_path, meta_path = self._paths(key)
        digest = hashlib.sha256(data).hexdigest()
        record = {
            "schema_version": 1,
            "key_digest": key.digest,
            "key": key.to_dict(),
            "sha256": digest,
            "size": len(data),
            "metadata": _canonical(dict(metadata or {})),
        }
        with self._lock:
            self._atomic_write(data_path, data)
            self._atomic_write(
                meta_path,
                json.dumps(record, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            self._writes += 1

    def invalidate(self, key: SegmentRenderKey) -> None:
        data_path, meta_path = self._paths(key)
        with self._lock:
            for path in (data_path, meta_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def stats(self) -> RenderCacheStats:
        with self._lock:
            return RenderCacheStats(self._hits, self._misses, self._writes, self._corruptions)

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
