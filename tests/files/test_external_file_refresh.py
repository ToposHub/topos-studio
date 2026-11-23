"""External application saves use a bounded, local reindex path."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.features.files.repository import FilesCatalog, FilesDataLocator
from src.features.files.services.domain import FilesMetadataService, FilesScanService
from src.features.files.services.facade import FilesFacade


@pytest.fixture()
def env(tmp_path: Path):
    root = tmp_path / "素材 库"
    root.mkdir()
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    catalog.open()
    library = catalog.register_library(root)
    yield SimpleNamespace(root=root, catalog=catalog, library=library,
                          scan=FilesScanService(catalog), tmp=tmp_path)
    catalog.close()


def test_refresh_only_requested_file_preserves_organization(env, monkeypatch):
    source = env.root / "方案.docx"
    source.write_bytes(b"changed document")
    unrelated = env.root / "untouched.png"
    unrelated.write_bytes(b"unrelated")
    library_id = env.library.library_id
    env.catalog.upsert_asset(library_id, source, size=1, width=99, codec="old")
    env.catalog.upsert_asset(library_id, unrelated, size=2, width=42)
    FilesMetadataService(env.catalog).patch_metadata(
        [str(source)], {"rating": 5, "tags": ["稿件"], "comments": "保留"},
        library_id=library_id)
    collection = env.catalog.create_collection(library_id, "文档")
    env.catalog.add_to_collection(collection, str(source))
    with env.catalog._write() as cursor:
        cursor.execute("UPDATE assets SET missing = 1 WHERE path = ?", (str(source),))
    other_before = env.catalog.asset(library_id, unrelated)
    before = env.catalog.revision

    def no_scan(*args, **kwargs):
        raise AssertionError("External saves must not scan the library")

    monkeypatch.setattr("src.features.files.services.domain.os.walk", no_scan)
    result = env.scan.refresh_external_paths([str(source), str(source)])

    assert result.ok and len(result.items) == 1
    assert result.items[0].result == {"indexed": True, "library_id": library_id}
    row = env.catalog.asset(library_id, source)
    assert row["size"] == source.stat().st_size
    assert row["mtime"] == source.stat().st_mtime
    assert row["missing"] == 0
    assert row["width"] is None and row["codec"] is None
    assert row["rating"] == 5 and row["comment"] == "保留"
    assert env.catalog.tags_of(library_id, source) == ["稿件"]
    assert env.catalog.collection_member_paths(collection) == [str(source)]
    assert env.catalog.asset(library_id, unrelated) == other_before
    assert result.revision == before + 1


def test_outside_library_is_reported_without_registering_or_writing(env):
    source = env.tmp / "outside.docx"
    source.write_bytes(b"outside")
    libraries = env.catalog.libraries()
    before = env.catalog.revision
    result = env.scan.refresh_external_paths([str(source)])
    assert result.ok and result.items[0].path == str(source)
    assert result.items[0].result == {"indexed": False, "library_id": ""}
    assert env.catalog.libraries() == libraries
    assert env.catalog.revision == before


def test_no_libraries_allows_view_refresh_only(tmp_path: Path):
    source = tmp_path / "preview.png"
    source.write_bytes(b"image")
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    try:
        result = FilesScanService(catalog).refresh_external_paths([str(source)])
        assert result.ok and not result.items[0].result["indexed"]
        assert not catalog.libraries()
    finally:
        catalog.close()


@pytest.mark.parametrize("paths", [[], "file", [123], ["relative.docx"], ["/a\0b"], ["/x"] * 129])
def test_invalid_batch_is_rejected_before_writes(env, paths):
    before = env.catalog.revision
    result = env.scan.refresh_external_paths(paths)
    assert not result.accepted and result.error_code == "INVALID_PATHS"
    assert env.catalog.revision == before


def test_missing_directory_and_symlink_results_are_explicit_failures(env):
    missing = env.root / "missing.docx"
    actual = env.root / "actual.docx"
    actual.write_bytes(b"ok")
    symlink = env.root / "link.docx"
    symlink.symlink_to(actual)
    result = env.scan.refresh_external_paths([str(missing), str(env.root), str(symlink), str(actual)])
    assert result.accepted and not result.ok
    assert [item.error_code for item in result.items] == ["NOT_FOUND", "NOT_A_FILE", "NOT_A_FILE", ""]
    assert result.items[-1].ok


def test_facade_runs_refresh_on_worker_with_frozen_selection(env):
    source = env.root / "save.docx"
    source.write_bytes(b"saved")
    facade = FilesFacade(env.catalog, max_workers=1)
    caller = threading.get_ident()
    done = threading.Event()
    received = []

    def deliver(outcome):
        received.append((threading.get_ident(), outcome))
        done.set()

    original = facade.scan.refresh_external_paths
    entered = threading.Event()
    gate = threading.Event()

    def gated(paths):
        entered.set()
        gate.wait(5)
        return original(paths)

    facade.scan.refresh_external_paths = gated
    try:
        paths = [str(source)]
        facade.refresh_external_paths(paths, request_id="save-1", listener=deliver)
        assert entered.wait(5)
        paths.clear()
        gate.set()
        assert done.wait(5)
        thread_id, outcome = received[0]
        assert thread_id != caller
        assert outcome.request_id == "save-1" and outcome.result.ok
        assert outcome.result.items[0].path == str(source)
    finally:
        gate.set()
        facade.shutdown()
        drained = threading.Event()
        facade.when_idle(drained.set)
        assert drained.wait(5)


def test_transport_refresh_is_gui_delivered_and_drops_old_thumbnail(qtbot, env):
    from PyQt6.QtCore import QThread
    from src.app.ui.native_files_transport import NativeFilesTransport, _ThumbnailRead

    source = env.root / "result.png"
    source.write_bytes(b"image")
    facade = FilesFacade(env.catalog, max_workers=1)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    transport.set_thumbnail_dispatch_paused(True)
    transport._thumbnail_attempted = {(str(source), 0), ("unrelated", 0)}
    transport._thumbnail_replies["old"] = _ThumbnailRead(str(source), 0, transport._generation)
    observed = []
    failures = []
    transport.external_files_refreshed.connect(
        lambda paths: observed.append((paths, QThread.currentThread())))
    transport.external_files_refresh_failed.connect(failures.append)
    try:
        transport.refresh_external_paths([str(source), str(env.root / "missing")])
        qtbot.waitUntil(lambda: bool(observed) and bool(failures), timeout=5000)
        assert observed == [([str(source)], transport.thread())]
        assert "missing" in failures[0]
        assert (str(source), 0) not in transport._thumbnail_attempted
        assert ("unrelated", 0) in transport._thumbnail_attempted
        assert "old" not in transport._thumbnail_replies
    finally:
        transport.shutdown()
        assert transport.wait_for_shutdown(5)


def test_transport_reports_invalid_batch_and_drops_shutdown_callback(qtbot, env):
    from src.app.ui.native_files_transport import NativeFilesTransport
    from src.features.files.services.facade import FacadeOutcome
    from src.shared.plugins.services import ServiceResult, ItemResult

    facade = FilesFacade(env.catalog)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    successes = []
    failures = []
    transport.external_files_refreshed.connect(successes.append)
    transport.external_files_refresh_failed.connect(failures.append)
    transport.refresh_external_paths([])
    qtbot.waitUntil(lambda: bool(failures), timeout=5000)
    assert not successes
    transport.shutdown()
    assert transport.wait_for_shutdown(5)
    transport._finish_external_files(FacadeOutcome(
        "late", "late", 1, result=ServiceResult(
            "test", ok=True, accepted=True,
            items=(ItemResult("/late", ok=True),))))
    assert not successes
