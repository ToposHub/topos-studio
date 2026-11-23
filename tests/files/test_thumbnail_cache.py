import json
from pathlib import Path
from zipfile import ZipFile

import cv2
import numpy as np
import pytest

from src.features.files.repository.catalog import FilesCatalog
from src.features.files.repository.locator import FilesDataLocator
from src.features.files.services import thumbnail_cache
from src.features.files.services.facade import FilesFacade
from src.features.files.services.thumbnail_cache import GradedThumbnailStamp, ThumbnailCache
from src.shared.media.graded_thumbnail import is_webp_preview


def test_thumbnail_cache_reuses_disk_and_invalidates_after_source_change(tmp_path: Path) -> None:
    locator = FilesDataLocator.for_config_root(tmp_path)
    source = tmp_path / "clip.mov"
    source.write_bytes(b"first source")
    first = b"\x89PNG\r\n\x1a\nfirst"
    second = b"\x89PNG\r\n\x1a\nsecond"
    assert ThumbnailCache(locator).get_or_create(str(source), 1, lambda: first) == first
    reopened = ThumbnailCache(locator)
    assert reopened.get_or_create(str(source), 1, lambda: pytest.fail("decoded on hit")) == first
    source.write_bytes(b"updated source data")
    assert reopened.get_or_create(str(source), 1, lambda: second) == second
    assert reopened.get_or_create(str(source), 2, lambda: first) == first


def test_clear_prevents_a_decode_in_progress_from_refilling_cache(tmp_path: Path) -> None:
    locator = FilesDataLocator.for_config_root(tmp_path)
    source = tmp_path / "clip.mov"
    source.write_bytes(b"source")
    image = b"\x89PNG\r\n\x1a\npreview"
    cache = ThumbnailCache(locator)
    cache.get_or_create(str(source), 1, lambda: image)

    def generate_after_clear() -> bytes:
        assert ThumbnailCache(locator).clear() == 1
        return image

    assert cache.get_or_create(str(source), 2, generate_after_clear) == image
    assert ThumbnailCache(locator).clear() == 0
    assert source.read_bytes() == b"source"


def test_graded_webp_matches_saved_source_and_sidecar_independent_of_original_cache(tmp_path: Path) -> None:
    locator = FilesDataLocator.for_config_root(tmp_path)
    cache = ThumbnailCache(locator)
    source = tmp_path / "photo.png"
    sidecar = source.with_suffix(".topos")
    source.write_bytes(b"original")
    with ZipFile(sidecar, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"project_mode": "standalone_image"}))
        archive.writestr("project.json", json.dumps({"assets": {"1": {"path": source.name}}}))
    stamp = GradedThumbnailStamp.capture(str(source), str(sidecar))
    webp = b"RIFF\x00\x00\x00\x00WEBPgraded"
    original = b"\x89PNG\r\n\x1a\nsource"
    assert cache.get_or_create(str(source), 0, lambda: original) == original
    assert cache.store_graded(stamp, webp, long_edge=450)
    assert ThumbnailCache(locator).read_graded(str(source)) == webp
    assert cache.get_or_create(str(source), 0, lambda: pytest.fail("lost original")) == original

    with ZipFile(sidecar, "a") as archive:
        archive.writestr("change.txt", "newer grade")
    assert cache.read_graded(str(source)) is None
    assert not cache.store_graded(stamp, webp, long_edge=450)
    assert cache.store_graded(GradedThumbnailStamp.capture(str(source), str(sidecar)), webp, long_edge=450)
    source.write_bytes(b"replaced source photo")
    assert cache.read_graded(str(source)) is None
    sidecar.unlink()
    assert cache.read_graded(str(source)) is None


@pytest.mark.parametrize("legacy_version", ("unversioned", "v2"))
def test_new_capture_upgrades_legacy_preview_and_preserves_larger_cache(
    tmp_path: Path, legacy_version: str,
) -> None:
    locator = FilesDataLocator.for_config_root(tmp_path)
    cache = ThumbnailCache(locator)
    source = tmp_path / "photo.jpg"
    sidecar = source.with_suffix(".topos")
    source.touch()
    with ZipFile(sidecar, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"project_mode": "standalone_image"}))
        archive.writestr("project.json", json.dumps({"assets": {"1": {"path": source.name}}}))
    stamp = GradedThumbnailStamp.capture(str(source), str(sidecar))
    rgb = np.random.default_rng(0).integers(0, 256, (450, 450, 3), dtype=np.uint8)
    ok, legacy = cv2.imencode(".jpg", rgb[::2, ::2])
    fingerprint = f"v2:{stamp.fingerprint}:225" if legacy_version == "v2" else stamp.fingerprint
    assert ok and cache._store(stamp.key, fingerprint, legacy.tobytes(), 0)
    assert cache.read_graded(str(source)) == legacy.tobytes()
    assert cache.read_graded(str(source), minimum_edge=225) is None
    facade = FilesFacade(FilesCatalog(locator))
    try:
        assert facade.cache_graded_thumbnail(stamp, rgb)
        preview = facade.graded_thumbnail(str(source))
        assert preview is not None and is_webp_preview(preview)
        assert len(preview) <= thumbnail_cache.MAX_GRADED_THUMBNAIL_BYTES
        assert cv2.imdecode(np.frombuffer(preview, np.uint8), cv2.IMREAD_COLOR).shape == rgb.shape
        assert facade.cache_graded_thumbnail(stamp, rgb[::2, ::2])
        ok, smaller = cv2.imencode(".webp", rgb[::2, ::2], [cv2.IMWRITE_WEBP_QUALITY, 80])
        assert ok and cache.store_graded(stamp, smaller.tobytes(), long_edge=225)
        assert facade.graded_thumbnail(str(source)) == preview
    finally:
        facade.shutdown(wait=True)


def test_thumbnail_cache_evicts_oldest_when_above_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(thumbnail_cache, "MAX_CACHE_BYTES", 40)
    cache = ThumbnailCache(FilesDataLocator.for_config_root(tmp_path))
    paths = [tmp_path / f"{index}.jpg" for index in range(3)]
    image = b"\x89PNG\r\n\x1a\n" + b"x" * 8
    for path in paths:
        path.touch()
        cache.get_or_create(str(path), 0, lambda: image)
    assert cache.get_or_create(str(paths[1]), 0, lambda: pytest.fail("recent entry was evicted")) == image
    assert cache.get_or_create(str(paths[0]), 0, lambda: b"regenerated") == b"regenerated"
