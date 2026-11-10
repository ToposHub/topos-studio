"""Second review diagnostics. Uses temporary files and an owned worker only.

Run from the repository with .venv/bin/python <this file> [--strict].
finding=true is an outstanding defect, not a passing acceptance test.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.features.color.core.gallery_contracts import GalleryQuery
from src.features.color.core.gallery_query_service import AsyncGalleryQueryRunner
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.gallery_transfer_service import (
    GalleryTransferService, IMPORT_JOURNAL_NAME, IMPORT_TXN_PREFIX,
)
from src.features.color.core.still_capture_transaction import StillCaptureTransaction

findings: list[bool] = []


def report(case: str, finding: bool, **facts: object) -> None:
    findings.append(finding)
    print(json.dumps(dict(case=case, finding=finding, **facts)))


def setup(base: Path) -> tuple[Path, GalleryTransferService, Path]:
    root = base / "Presets"
    (root / "User").mkdir(parents=True)
    (root / "System").mkdir()
    lut = base / "original.cube"
    lut.write_bytes(b"LUT_3D_SIZE 2\n0 0 0\n1 1 1\n")
    with zipfile.ZipFile(root / "User" / "Look.topos", "w") as grade:
        grade.writestr("project.json", json.dumps({"layers": [
            {"name": "look", "active_lut_path": str(lut)}]}))
    service = GalleryTransferService(GalleryFolderRepository(str(root)))
    archive = base / "Look.toposbundle"
    assert service.export_bundle(str(root / "User"), "Look", str(archive)).succeeded
    return root, service, archive


with tempfile.TemporaryDirectory() as directory:
    first = StillCaptureTransaction(directory)
    staged = Path(first.staged_path(str(Path(directory) / "A.topos")))
    staged.write_bytes(b"CAPTURED FRAME")
    first.add(str(Path(directory) / "A.topos"))
    second = StillCaptureTransaction(directory)
    report("active_same_thread_transaction", not staged.exists(),
           staging_preserved=staged.exists())
    second.rollback()
    try:
        first.rollback()
    except OSError:
        pass

with tempfile.TemporaryDirectory() as directory:
    root, service, _ = setup(Path(directory))
    folder = root / "System"
    outside = Path(directory) / "outside.txt"
    outside.write_bytes(b"KEEP")
    txn = folder / (IMPORT_TXN_PREFIX + "bad-path")
    txn.mkdir()
    (txn / IMPORT_JOURNAL_NAME).write_text(json.dumps({
        "schema": 1, "state": "installing", "finals": [
            {"path": str(outside), "size": 4}]}))
    service._recover_import_txns(str(folder))
    report("import_recovery_outside_root", not outside.exists(),
           outside_preserved=outside.exists(), journal_preserved=txn.exists())

with tempfile.TemporaryDirectory() as directory:
    root, service, _ = setup(Path(directory))
    folder = root / "System"
    foreign = folder / "Replaced.topos"
    foreign.write_bytes(b"USER")
    txn = folder / (IMPORT_TXN_PREFIX + "same-size")
    txn.mkdir()
    (txn / IMPORT_JOURNAL_NAME).write_text(json.dumps({
        "schema": 1, "state": "installing", "finals": [
            {"path": str(foreign), "size": 4}]}))
    service._recover_import_txns(str(folder))
    report("import_recovery_same_size_foreign_file", not foreign.exists(),
           foreign_file_preserved=foreign.exists(), journal_preserved=txn.exists())

with tempfile.TemporaryDirectory() as directory:
    root, service, archive = setup(Path(directory))
    external = Path(directory) / "external-luts"
    external.mkdir()
    (root / "LUTs").symlink_to(external, target_is_directory=True)
    result = service.import_bundle(str(archive), str(root / "System"))
    written = sorted(path.name for path in external.iterdir())
    report("dependency_directory_symlink", bool(written), status=result.status,
           outside_written=written)

with tempfile.TemporaryDirectory() as directory:
    root, service, archive = setup(Path(directory))

    def cancel_after_lut_install() -> bool:
        return (root / "LUTs").exists() and any((root / "LUTs").iterdir())

    result = service.import_bundle(str(archive), str(root / "System"),
                                   cancel_check=cancel_after_lut_install)
    luts = list((root / "LUTs").glob("*.cube"))
    report("cancel_after_dependency_install", bool(luts), status=result.status,
           dependencies_left=len(luts), bundle_files_left=len(list((root / "System").iterdir())))

with tempfile.TemporaryDirectory() as directory:
    root, service, archive = setup(Path(directory))
    result = service.import_bundle(str(archive), str(root / "System"))
    assert result.succeeded
    moved_root = Path(directory) / "MovedPresets"
    root.rename(moved_root)
    with zipfile.ZipFile(moved_root / "System" / "Look.topos") as grade:
        reference = json.loads(grade.read("project.json"))["layers"][0]["active_lut_path"]
    report("managed_dependency_absolute_reference", str(root) in reference,
           reference_is_absolute=os.path.isabs(reference), reference_exists=Path(reference).exists(),
           moved_dependency_exists=bool(list((moved_root / "LUTs").glob("*.cube"))))

started = threading.Event()
release = threading.Event()
delivered: list[int] = []


class BlockingIndex:
    def query(self, query: GalleryQuery) -> object:
        if query.album_id == "block":
            started.set()
            if not release.wait(3):
                raise RuntimeError("diagnostic worker timed out")
        return object()


runner = AsyncGalleryQueryRunner(BlockingIndex(), on_result=lambda query, _: delivered.append(query.request_revision))
try:
    runner.submit(GalleryQuery(album_id="block"))
    assert started.wait(3)
    runner.submit(GalleryQuery(album_id="A", request_revision=1))
    runner.begin_generation()
    runner.submit(GalleryQuery(album_id="A", request_revision=2))
    release.set()
    runner._executor.submit(lambda: None).result(timeout=3)
    report("latest_query_lost_after_generation_change", 2 not in delivered,
           latest_delivered=2 in delivered, stats=runner.stats)
finally:
    release.set()
    runner.shutdown()

if "--strict" in sys.argv and any(findings):
    raise SystemExit(1)
