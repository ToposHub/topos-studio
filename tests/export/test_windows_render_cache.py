"""Segment render key and disk cache contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.export.render_cache import DiskRenderCache, SegmentRenderKey


def make_key(**overrides: object) -> SegmentRenderKey:
    values: dict[str, object] = {
        "source_fingerprint": "sha256:source",
        "in_frame": 0,
        "out_frame": 48,
        "retime": {"speed": 1.0},
        "layer_stack": ("clip-a", "clip-b"),
        "effects": ({"id": "blur", "radius": 3.0},),
        "grade": {"exposure": 0.25},
        "color_management": {"input": "bt709", "output": "bt709"},
        "output_format": {"codec": "h264", "pix_fmt": "nv12"},
        "renderer_backend": "d3d11",
        "renderer_version": "d3d11-renderer-v1",
    }
    values.update(overrides)
    return SegmentRenderKey(**values)  # type: ignore[arg-type]


def test_key_contains_all_semantic_dimensions_and_is_order_stable() -> None:
    first = make_key(grade={"b": 2, "a": 1})
    second = make_key(grade={"a": 1, "b": 2})
    assert first.digest == second.digest
    assert "renderer_backend" in first.to_dict()
    assert "source_fingerprint" in first.to_dict()
    assert make_key(renderer_version="d3d11-renderer-v2").digest != first.digest
    assert make_key(effects=()).digest != first.digest


def test_proxy_is_not_usable_for_delivery_cache() -> None:
    with pytest.raises(ValueError, match="delivery cache"):
        make_key(source_kind="proxy")
    proxy = make_key(source_kind="proxy", delivery=False)
    assert proxy.source_kind == "proxy"


def test_disk_cache_atomic_round_trip_and_stats(tmp_path: Path) -> None:
    cache = DiskRenderCache(tmp_path / "render-cache")
    key = make_key()
    assert cache.get(key) is None
    cache.put(key, b"encoded-artifact", {"frames": 48, "cache_mode": "cold"})
    hit = cache.get(key)
    assert hit == (b"encoded-artifact", {"frames": 48, "cache_mode": "cold"})
    stats = cache.stats()
    assert stats.misses == 1 and stats.hits == 1 and stats.writes == 1


def test_corrupt_cache_is_a_miss_not_a_silent_success(tmp_path: Path) -> None:
    cache = DiskRenderCache(tmp_path / "render-cache")
    key = make_key()
    cache.put(key, b"good")
    data_path = cache.root / f"{key.digest}.bin"
    data_path.write_bytes(b"corrupt")
    assert cache.get(key) is None
    assert cache.stats().corruptions == 1


def test_from_mapping_validates_required_fields() -> None:
    with pytest.raises(ValueError, match="缺少字段"):
        SegmentRenderKey.from_mapping({"source_fingerprint": "x"})

