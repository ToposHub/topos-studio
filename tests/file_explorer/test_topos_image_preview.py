"""Topos .toos integration tests for the external asset browser."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
FILE_EXPLORER = ROOT / "external" / "file_explorer"
if str(FILE_EXPLORER) not in sys.path:
    sys.path.insert(0, str(FILE_EXPLORER))

from plugins.topos_image.topos_image_media import (  # noqa: E402
    _planes_to_rgb,
    generate_topos_image_thumbnail,
    get_topos_image_metadata,
)
from routes import classify_ext  # noqa: E402


def test_toos_is_classified_as_an_image() -> None:
    assert classify_ext(".toos") == "image"


def test_gbr_preview_keeps_spec_channel_order() -> None:
    info = SimpleNamespace(pixel_format=2, bit_depth=10, has_alpha=False)
    planes = [
        np.full((2, 2), 128, dtype=np.uint16),  # G
        np.full((2, 2), 256, dtype=np.uint16),  # B
        np.full((2, 2), 512, dtype=np.uint16),  # R
    ]
    rgb, _ = _planes_to_rgb(planes, info)
    assert tuple(rgb[0, 0]) == (128, 32, 64)


def test_topos_provider_passes_bounded_target_size(monkeypatch, tmp_path: Path) -> None:
    import plugins.topos_image.topos_image_media as media

    requested = []

    class FakeCodec:
        def decode(self, path, target_size=None):
            requested.append(target_size)
            info = SimpleNamespace(pixel_format=2, bit_depth=8,
                                   has_alpha=False)
            planes = [np.full((2, 4), 128, dtype=np.uint16) for _ in range(3)]
            return planes, info, {}

    monkeypatch.setattr(
        "src.shared.codec.topos_image_binding.get_image_codec",
        lambda: FakeCodec(),
    )
    target = tmp_path / "image.toos_lg.webp"
    assert media.generate_topos_image_thumbnail("source.toos", str(target))
    assert requested == [(600, 600)]
    with Image.open(target) as preview:
        assert preview.size == (4, 2)


def test_thumbnail_sidecar_records_version_and_hash(tmp_path: Path) -> None:
    from thumb_engine import ThumbnailEngine
    import json

    source = tmp_path / "source.toos"
    target = tmp_path / "source.toos.webp"
    source.write_bytes(b"first payload")
    engine = object.__new__(ThumbnailEngine)
    engine.write_sidecar(str(source), str(target))
    payload = json.loads((tmp_path / "source.toos.webp.v").read_text())
    assert payload["schema_version"] == 1
    assert len(payload["source_sha256"]) == 64
    assert engine.is_stale(str(source), str(target)) is False
    source.write_bytes(b"changed payload")
    assert engine.is_stale(str(source), str(target)) is True


@pytest.mark.skipif(
    not (ROOT / "native/topos_codec/build/intra-bench/vertical-v3.toos").exists(),
    reason="Topos image fixture is not present",
)
def test_real_toos_thumbnail_and_metadata(tmp_path: Path) -> None:
    source = ROOT / "native/topos_codec/build/intra-bench/vertical-v3.toos"
    metadata = get_topos_image_metadata(str(source))
    assert metadata["Codec"] == "Topos Image"
    assert metadata["Resolution"] == "256x256"

    target = tmp_path / "vertical.toos_preview.webp"
    assert generate_topos_image_thumbnail(str(source), str(target))
    with Image.open(target) as preview:
        assert preview.format == "WEBP"
        assert preview.mode == "RGB"
        assert preview.size == (256, 256)


@pytest.mark.skipif(
    not (ROOT / "native/topos_codec/build/intra-bench/vertical-v3.toos").exists(),
    reason="Topos image fixture is not present",
)
def test_thumbnail_engine_uses_the_registered_provider(tmp_path: Path) -> None:
    import plugins.topos_image as plugin
    from thumb_engine import engine

    plugin.setup(None)
    source = ROOT / "native/topos_codec/build/intra-bench/vertical-v3.toos"
    target = tmp_path / "vertical.toos.webp"
    assert engine.generate(str(source), str(target))
    assert target.exists()
    assert Path(str(target) + ".v").exists()
