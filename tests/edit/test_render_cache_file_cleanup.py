"""Generated render-cache files are removed when manifests become obsolete."""

from __future__ import annotations

from dataclasses import replace

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.project import EditProject
from src.features.edit.core.render_cache import RenderCacheManifest, RenderCacheStatus


def _manifest(cache_id: str, clip_id: str, path: str, status: str) -> RenderCacheManifest:
    return RenderCacheManifest(
        cache_id=cache_id,
        timeline_id="timeline-1",
        clip_id=clip_id,
        path=path,
        status=status,
        render_kind="motion_graphic",
        scene_id="scene-1",
    )


def test_cleanup_removes_stale_file_but_preserves_other_clip(tmp_path) -> None:
    stale = tmp_path / "stale.mp4"
    keep = tmp_path / "keep.mp4"
    stale.write_bytes(b"old")
    keep.write_bytes(b"keep")
    project = EditProject.create("Cache cleanup")
    project = replace(
        project,
        render_cache_manifests={
            "old": _manifest("old", "clip-1", str(stale), RenderCacheStatus.STALE).to_dict(),
            "keep": _manifest("keep", "clip-2", str(keep), RenderCacheStatus.VALID).to_dict(),
        },
    )

    EditControllerMixin._cleanup_obsolete_render_cache_files(project, clip_id="clip-1")

    assert not stale.exists()
    assert keep.exists()


def test_cleanup_preserves_valid_fingerprinted_history(tmp_path) -> None:
    old_path = tmp_path / "old.mov"
    current_path = tmp_path / "current.mov"
    old_path.write_bytes(b"old")
    current_path.write_bytes(b"current")
    project = replace(
        EditProject.create("Cache history"),
        render_cache_manifests={
            "old": _manifest(
                "old", "clip-1", str(old_path), RenderCacheStatus.VALID,
            ).to_dict() | {"render_fingerprint": "fingerprint-old"},
            "current": _manifest(
                "current", "clip-1", str(current_path), RenderCacheStatus.VALID,
            ).to_dict() | {"render_fingerprint": "fingerprint-current"},
        },
    )

    EditControllerMixin._cleanup_obsolete_render_cache_files(
        project,
        clip_id="clip-1",
        preserve_valid_fingerprinted=True,
    )

    assert old_path.exists()
    assert current_path.exists()


def test_cleanup_does_not_remove_active_rendering_without_explicit_cancel(tmp_path) -> None:
    active = tmp_path / "active.mp4"
    active.write_bytes(b"active")
    project = replace(
        EditProject.create("Cache cleanup"),
        render_cache_manifests={
            "active": _manifest(
                "active", "clip-1", str(active), RenderCacheStatus.RENDERING,
            ).to_dict(),
        },
    )

    EditControllerMixin._cleanup_obsolete_render_cache_files(project, clip_id="clip-1")
    assert active.exists()

    EditControllerMixin._cleanup_obsolete_render_cache_files(
        project, clip_id="clip-1", include_rendering=True,
    )
    assert not active.exists()


def test_cleanup_removes_interrupted_realtime_sidecars(tmp_path) -> None:
    output = tmp_path / "clip.mp4"
    sidecar = tmp_path / "clip.mp4.realtime.tmp.mp4"
    sidecar.write_bytes(b"partial")
    project = replace(
        EditProject.create("Realtime cleanup"),
        render_cache_manifests={
            "orphan": _manifest(
                "orphan", "clip-1", str(output), RenderCacheStatus.RENDERING,
            ).to_dict(),
        },
    )

    EditControllerMixin._cleanup_obsolete_render_cache_files(
        project, clip_id="clip-1", include_rendering=True,
    )
    assert not sidecar.exists()
