from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from src.features.edit.core.project_package import (
    ProjectPackageCancelled,
    export_project_package,
    inspect_project_package,
)
from src.shared.project.topos_project_io import ToposProjectIO


def _write_source(source: Path) -> Path:
    """Write an archive with three different ways a file may be used."""
    source.mkdir()
    for filename, data in {
        "video.mov": b"video",
        "unused.wav": b"unused",
        "bound.wav": b"bound",
        "graph.png": b"graph",
        "grade.cube": b"TITLE an-external-lut",
    }.items():
        (source / filename).write_bytes(data)
    media = {
        asset_id: {"asset_id": asset_id, "path": path, "path_relative": path}
        for asset_id, path in {
            "clip": "video.mov",
            "unused": "unused.wav",
            "sync": "bound.wav",
        }.items()
    }
    project = source / "Demo.topos"
    with zipfile.ZipFile(project, "w") as archive:
        for name, data in {
            "manifest.json": {"project_id": "demo", "project_mode": "edit_project"},
            "project.json": {
                "project_id": "demo",
                "name": "Demo",
                "assets": media,
                "settings": {"audio_sync_bindings": {"clip": {"wav_asset_id": "sync"}}},
            },
            "edit/media_pool.json": media,
            "edit/timeline_sequences.json": {
                "t1": {"video_tracks": [{"clips": [{"asset_id": "clip"}]}]},
            },
            "color/color_stacks.json": {
                "look": {"lut_path": "grade.cube", "layers": []},
            },
            "composite/graphs.json": {
                "graph": {"nodes": [
                    {"id": "photo", "type": "image_input", "parameters": {"file_path": "graph.png"}},
                    {"id": "lut", "type": "lut", "parameters": {"lut_path": "grade.cube"}},
                    {"id": "out", "type": "file_output", "parameters": {"file_path": "no-copy.exr"}},
                ]},
            },
        }.items():
            archive.writestr(name, json.dumps(data))
        archive.writestr("future/state.json", b'{"opaque":    [1, 2, 3]}\n')
        archive.writestr("assets/embedded.bin", b"original payload")
    return project


def test_directory_preserves_unknown_members_and_relocates_used_files(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "original")
    before = source.read_bytes()
    destinations = tmp_path / "destinations"
    destinations.mkdir()
    plan = inspect_project_package(str(source), "used")
    assert not plan.missing
    assert plan.estimated_bytes > len(before)
    progress: list[tuple[int, int]] = []

    exported = export_project_package(
        str(source), str(destinations), scope="used", zip_output=False,
        allow_missing=False, progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert source.read_bytes() == before
    assert progress[-1] == (100, 100)
    assert (Path(exported.path) / "media" / "bound.wav").is_file()
    assert not (Path(exported.path) / "media" / "unused.wav").exists()
    moved = tmp_path / "separate"
    shutil.move(exported.path, moved)
    shutil.rmtree(source.parent)
    relocated = moved / "Demo.topos"
    with zipfile.ZipFile(relocated) as archive:
        assert archive.read("future/state.json") == b'{"opaque":    [1, 2, 3]}\n'
        assert archive.read("assets/embedded.bin") == b"original payload"
        pool = json.loads(archive.read("edit/media_pool.json"))
        light = json.loads(archive.read("project.json"))["assets"]
        graph = json.loads(archive.read("composite/graphs.json"))["graph"]
        color = json.loads(archive.read("color/color_stacks.json"))["look"]
        assert color["lut_path"].startswith("topos-res://")
        assert graph["nodes"][2]["parameters"]["file_path"] == "no-copy.exr"
        _, full = ToposProjectIO.load(str(relocated), activate_resources=False)
        assert full is not None
        assert Path(full["assets"]["clip"]["path"]).read_bytes() == b"video"
        assert Path(full["compositions"]["graph"]["nodes"][0]["parameters"]["file_path"]).read_bytes() == b"graph"
        assert Path(full["compositions"]["graph"]["nodes"][1]["parameters"]["lut_path"]).read_bytes().startswith(b"TITLE")
    for assets in (pool, light):
        for asset_id in ("clip", "sync"):
            ref = assets[asset_id]["path_relative"]
            assert not Path(ref).is_absolute()
            assert (moved / ref).is_file()
    assert (moved / graph["nodes"][0]["parameters"]["file_path"]).is_file()
    assert (moved / "luts" / "grade.cube").is_file()


def test_zip_is_stored_and_does_not_overwrite_previous_export(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "original")
    marker = tmp_path / "Demo Package.zip"
    marker.write_bytes(b"existing")
    result = export_project_package(
        str(source), str(tmp_path), scope="all", zip_output=True, allow_missing=False,
    )
    assert marker.read_bytes() == b"existing"
    assert Path(result.path) != marker
    assert not list(tmp_path.glob(".topos-package-*"))
    with zipfile.ZipFile(result.path) as archive:
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
        assert archive.read("Demo Package/media/unused.wav") == b"unused"
        assert archive.testzip() is None


def test_project_only_rebases_relative_media_without_copying_it(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "original")
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    result = export_project_package(
        str(source), str(destination), scope="project", zip_output=False, allow_missing=False,
    )
    output = Path(result.path)
    assert list(destination.iterdir()) == [output]
    assert output.suffix == ".topos"
    with zipfile.ZipFile(output) as archive:
        assets = json.loads(archive.read("edit/media_pool.json"))
        ref = assets["clip"]["path_relative"]
        assert (destination / ref).resolve() == (source.parent / "video.mov")
        assert archive.read("future/state.json") == b'{"opaque":    [1, 2, 3]}\n'


def test_missing_sources_require_consent_and_cancellation_cleans_staging(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "original")
    (source.parent / "video.mov").unlink()
    assert inspect_project_package(str(source), "all").missing
    destination = tmp_path / "exports"
    destination.mkdir()
    with pytest.raises(FileNotFoundError):
        export_project_package(
            str(source), str(destination), scope="all", zip_output=False, allow_missing=False,
        )
    assert not list(destination.iterdir())
    cancelled = False

    def cancel_after_copy(_done: int, _total: int) -> None:
        nonlocal cancelled
        cancelled = True

    with pytest.raises(ProjectPackageCancelled):
        export_project_package(
            str(source), str(destination), scope="all", zip_output=True, allow_missing=True,
            progress_callback=cancel_after_copy, cancelled_fn=lambda: cancelled,
        )
    assert not list(destination.iterdir())
    result = export_project_package(
        str(source), str(destination), scope="all", zip_output=False, allow_missing=True,
    )
    assert result.missing
    assert (Path(result.path) / "dependencies.json").is_file()


def test_sequences_and_proxy_settings_reopen_with_only_packaged_frames(tmp_path: Path) -> None:
    from src.features.edit.core.sequence_classify import still_sequence_frame_path

    original = tmp_path / "source"
    original.mkdir()
    for filename in ("shot.0001.png", "shot.0002.png", "shot.0003.png", "cloud.0001.vdb", "cloud.0002.vdb", "proxy.mov"):
        (original / filename).write_bytes(filename.encode())
    source = original / "Sequence.topos"
    footage = {
        "path": "shot.0001.png",
        "path_relative": "shot.0001.png",
        "media_type": "image_sequence",
        "proxy_path": str(original / "proxy.mov"),
        "proxy_status": "ready",
        "metadata": {
            "sequence": {
                "frames": [str(original / f"shot.{frame:04d}.png") for frame in (1, 2, 3)],
                "first_number": 1,
                "last_number": 3,
                "missing_numbers": [],
            },
        },
    }
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("manifest.json", '{"project_id":"seq","project_mode":"edit_project"}')
        archive.writestr("project.json", '{"project_id":"seq","name":"Sequence","assets":{}}')
        archive.writestr("edit/media_pool.json", json.dumps({"frames": footage}))
        archive.writestr("composite/graphs.json", json.dumps({
            "g": {"nodes": [{"id": "volume", "type": "volume3d", "parameters": {"file_path": "cloud.%04d.vdb"}}]}
        }))

    assert not inspect_project_package(str(source), "all").missing
    result = export_project_package(
        str(source), str(tmp_path), scope="all", zip_output=False, allow_missing=False,
    )
    root = Path(result.path)
    shutil.rmtree(original)
    _, full = ToposProjectIO.load(str(root / "Sequence.topos"), activate_resources=False)
    assert full is not None
    asset = full["assets"]["frames"]
    assert asset["proxy_path"] is None and asset["proxy_status"] == "none"
    assert not list(root.rglob("proxy.mov"))
    assert Path(still_sequence_frame_path(asset["path"], asset["metadata"], 2)).read_bytes() == b"shot.0003.png"
    pattern = full["compositions"]["g"]["nodes"][0]["parameters"]["file_path"]
    assert Path(str(pattern) % 2).read_bytes() == b"cloud.0002.vdb"


def test_export_does_not_proceed_when_newly_missing_files_were_not_approved(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "original")
    (source.parent / "video.mov").unlink()
    approved = inspect_project_package(str(source), "all").missing
    (source.parent / "bound.wav").unlink()
    destination = tmp_path / "output"
    destination.mkdir()
    with pytest.raises(FileNotFoundError, match="Additional"):
        export_project_package(
            str(source), str(destination), scope="all", zip_output=False,
            allow_missing=True, expected_missing=approved,
        )
    assert not list(destination.iterdir())


def test_pinned_linked_composition_is_copied_verbatim_and_bad_revision_is_reported(tmp_path: Path) -> None:
    from hashlib import sha256

    original = tmp_path / "source"
    original.mkdir()
    linked = original / "Comp.topos"
    with zipfile.ZipFile(linked, "w") as archive:
        archive.writestr("manifest.json", '{"project_id":"comp","project_mode":"edit_project"}')
        archive.writestr("project.json", '{"project_id":"comp","assets":{}}')
        archive.writestr("composite/graphs.json", '{"child":{"nodes":[]}}')
    linked_bytes = linked.read_bytes()
    expected_fingerprint = sha256(linked_bytes).hexdigest()
    source = original / "Host.topos"

    def write_host(fingerprint: str) -> None:
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("manifest.json", '{"project_id":"host","project_mode":"edit_project"}')
            archive.writestr("project.json", '{"project_id":"host","name":"Host","assets":{}}')
            archive.writestr("composite/graphs.json", json.dumps({
                "parent": {"nodes": [{
                    "id": "precomp", "type": "precomp", "parameters": {"external_comp_ref": {
                        "project_id": "comp", "comp_id": "child", "relative_path": "Comp.topos",
                        "revision_fingerprint": fingerprint, "update_policy": "pinned",
                    }},
                }]},
            }))

    write_host("0" * 64)
    assert any("pinned" in issue for issue in inspect_project_package(str(source), "all").missing)
    write_host(expected_fingerprint)
    assert not inspect_project_package(str(source), "all").missing
    result = export_project_package(
        str(source), str(tmp_path), scope="all", zip_output=False, allow_missing=False,
    )
    root = Path(result.path)
    with zipfile.ZipFile(root / "Host.topos") as archive:
        graphs = json.loads(archive.read("composite/graphs.json"))
        ref = graphs["parent"]["nodes"][0]["parameters"]["external_comp_ref"]
    assert (root / ref["relative_path"]).read_bytes() == linked_bytes
    assert ref["revision_fingerprint"] == expected_fingerprint
