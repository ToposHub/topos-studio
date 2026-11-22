"""Library grade markers follow the original's current standalone sidecar."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from src.app.ui.library_grade_badges import LibraryGradeBadges
from src.features.files.services.facade import FacadeOutcome
from src.shared.project.image_sidecar import resolve_image_sidecar


def _project(path: Path, source: Path, mode: str | None = "standalone_image") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        if mode is not None:
            archive.writestr("manifest.json", json.dumps({"project_mode": mode}))
        archive.writestr("project.json", json.dumps({"assets": {"1": {"path": source.name}}}))


def test_same_stem_raw_and_jpeg_resolve_their_own_archive(tmp_path: Path) -> None:
    jpeg = tmp_path / "photo.jpg"
    raw = tmp_path / "photo.dng"
    for source in (jpeg, raw):
        source.touch()
    shared = tmp_path / "photo.topos"
    _project(shared, jpeg)
    assert resolve_image_sidecar(str(jpeg)) == (str(shared), True)
    qualified = tmp_path / "photo.dng.topos"
    assert resolve_image_sidecar(str(raw)) == (str(qualified), False)
    _project(qualified, raw, mode="edit_project")
    qualified_own = tmp_path / "photo.dng.2.topos"
    _project(qualified_own, raw)
    assert resolve_image_sidecar(str(raw)) == (str(qualified_own), True)


def test_invalid_and_unrelated_packages_leave_photo_unmarked(tmp_path: Path) -> None:
    source = tmp_path / "photo.jpg"
    unrelated = tmp_path / "other.jpg"
    source.touch()
    traditional = tmp_path / "photo.topos"
    qualified = tmp_path / "photo.jpg.topos"
    _project(traditional, unrelated)
    _project(qualified, source, mode=None)
    assert resolve_image_sidecar(str(source)) == (str(tmp_path / "photo.jpg.2.topos"), False)
    qualified.write_bytes(b"not an archive")
    assert not resolve_image_sidecar(str(source))[1]


def test_invalidation_cannot_publish_an_older_worker_result(qtbot, tmp_path: Path) -> None:
    class _DeferredFacade:
        def __init__(self) -> None:
            self.jobs = []

        def run(self, key, request_id, work, listener) -> None:
            self.jobs.append((key, request_id, work, listener))

        def finish(self, index: int, *, value: dict[str, bool] | None = None) -> None:
            key, request, work, listener = self.jobs[index]
            listener(FacadeOutcome(request, key, index, result=work() if value is None else value))

    source = tmp_path / "photo.jpg"
    source.touch()
    path = str(source)
    facade = _DeferredFacade()
    badges = LibraryGradeBadges(lambda: facade)
    try:
        assert not badges.is_graded(path)
        qtbot.waitUntil(lambda: len(facade.jobs) == 1)
        badges.invalidate(path)
        assert not badges.is_graded(path)
        facade.finish(0, value={path: True})
        qtbot.waitUntil(lambda: len(facade.jobs) == 2)
        assert not badges.is_graded(path)
        _project(tmp_path / "photo.topos", source)
        facade.finish(1)
        qtbot.waitUntil(lambda: badges.is_graded(path))

        badges.reset()
        assert not badges.is_graded(path)
        qtbot.waitUntil(lambda: len(facade.jobs) == 3)
        badges.reset()
        facade.finish(2, value={path: True})
        assert not badges.is_graded(path)
    finally:
        badges.shutdown()
