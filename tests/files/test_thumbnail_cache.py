import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from src.features.files.repository.locator import FilesDataLocator
from src.features.files.services import thumbnail_cache
from src.features.files.services.thumbnail_cache import GradedThumbnailStamp, ThumbnailCache


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


def test_graded_jpeg_matches_saved_source_and_sidecar_independent_of_original_cache(tmp_path: Path) -> None:
    locator = FilesDataLocator.for_config_root(tmp_path)
    cache = ThumbnailCache(locator)
    source = tmp_path / "photo.png"
    sidecar = source.with_suffix(".topos")
    source.write_bytes(b"original")
    with ZipFile(sidecar, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"project_mode": "standalone_image"}))
        archive.writestr("project.json", json.dumps({"assets": {"1": {"path": source.name}}}))
    stamp = GradedThumbnailStamp.capture(str(source), str(sidecar))
    jpg = b"\xff\xd8\xffgraded"
    original = b"\x89PNG\r\n\x1a\nsource"
    assert cache.get_or_create(str(source), 0, lambda: original) == original
    assert cache.store_graded(stamp, jpg)
    assert ThumbnailCache(locator).read_graded(str(source)) == jpg
    assert cache.get_or_create(str(source), 0, lambda: pytest.fail("lost original")) == original

    with ZipFile(sidecar, "a") as archive:
        archive.writestr("change.txt", "newer grade")
    assert cache.read_graded(str(source)) is None
    assert not cache.store_graded(stamp, jpg)
    assert cache.store_graded(GradedThumbnailStamp.capture(str(source), str(sidecar)), jpg)
    source.write_bytes(b"replaced source photo")
    assert cache.read_graded(str(source)) is None
    sidecar.unlink()
    assert cache.read_graded(str(source)) is None


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
