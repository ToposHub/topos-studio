"""Read-only source audit; all filesystem/SQLite experiments use TemporaryDirectory.

Run from the repository root with .venv/bin/python. Injected failures simulate
specific interleavings; they do not constitute a real cross-volume/device test.
"""
from __future__ import annotations

import errno
import json
import runpy
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from external.file_explorer import file_tasks  # noqa: E402


def service_reproductions(root: Path) -> dict[str, object]:
    results: dict[str, object] = {}
    service = file_tasks.FileTaskService()
    try:
        source = root / "directory"
        source.mkdir()
        (source / "clip").write_bytes(b"x")
        try:
            service._do_rename(file_tasks.FileTaskItem(
                str(source), str(root / "renamed")), "error", {})
        except OSError as exc:
            results["directory_rename"] = {
                "error": type(exc).__name__, "source_preserved": source.exists()}

        source, destination = root / "x.mov", root / "y.mov"
        source.write_bytes(b"x")
        with patch.object(file_tasks.os, "replace", side_effect=OSError(errno.EXDEV, "cross device")):
            try:
                service._do_move(file_tasks.FileTaskItem(
                    str(source), str(destination)), "overwrite", {})
            except OSError as exc:
                results["overwrite_exdev_injection"] = {
                    "errno": exc.errno, "source_preserved": source.exists()}

        source, destination = root / "source", root / "destination"
        source.write_bytes(b"SOURCE")
        rename = file_tasks.os.rename

        def injected_rename(old: str, new: str) -> None:
            Path(new).unlink()
            Path(new).write_bytes(b"THIRD PARTY")
            rename(old, new)

        with patch.object(file_tasks.os, "rename", side_effect=injected_rename):
            service._do_move(file_tasks.FileTaskItem(
                str(source), str(destination)), "error", {})
        results["no_replace_target_swap_injection"] = {
            "destination": destination.read_text(), "source_exists": source.exists(),
            "third_party_preserved": destination.read_bytes() == b"THIRD PARTY"}

        tree, output = root / "tree", root / "out"
        tree.mkdir()
        (tree / "a").write_bytes(b"A")
        (tree / "b").write_bytes(b"B")
        copy_entry = service._copy_entry
        count = 0

        def cancelled_copy(old: str, new: str, overwrite: bool,
                           context: file_tasks._RunContext) -> None:
            nonlocal count
            count += 1
            if count > 1:
                raise file_tasks.TaskCancelledError("injected cancel")
            copy_entry(old, new, overwrite, context)

        with patch.object(service, "_copy_entry", side_effect=cancelled_copy):
            try:
                service._do_copy(file_tasks.FileTaskItem(str(tree), str(output)), "error", {})
            except file_tasks.TaskCancelledError:
                pass
        results["directory_cancel_injection"] = {
            "destination_exists": output.exists(),
            "remaining_files": sorted(str(p.relative_to(output)) for p in output.rglob("*"))}
    finally:
        service.shutdown()
    return results


def backend_reproductions(root: Path) -> dict[str, object]:
    import pytest

    # Reuse the existing isolated SQLite fixture, without starting HTTP/Qt runtimes.
    fixture = runpy.run_path(str(ROOT / "tests/files/test_file_safety_regressions.py"))
    results: dict[str, object] = {}
    with pytest.MonkeyPatch.context() as monkeypatch:
        backend = fixture["backend"].__wrapped__(root, monkeypatch)
        client, headers = fixture["_client"](backend)
        for mode in ("single", "batch"):
            source = root / f"{mode}.mov"
            source.write_bytes(b"media")
            with backend.connect() as connection:
                connection.execute("INSERT INTO file_meta(path) VALUES(?)", (str(source),))
                connection.execute("INSERT INTO markers(file_path,t_in,label) VALUES(?,1,'marker')",
                                   (str(source),))
            if mode == "single":
                response = client.post("/api/rename", json={
                    "old_path": str(source), "new_name": "single-new.mov"}, headers=headers)
            else:
                response = client.post("/api/batch/rename", json={
                    "files": [str(source)], "mode": "replace",
                    "params": {"find": "batch", "replace": "batch-new"}}, headers=headers)
            connection = backend.connect()
            results[f"{mode}_rename_metadata"] = {
                "response": response.get_json(),
                "file_meta": [Path(row[0]).name for row in connection.execute(
                    "SELECT path FROM file_meta WHERE path LIKE ?", (f"%{mode}%",))],
                "marker_paths": [Path(row[0]).name for row in connection.execute(
                    "SELECT file_path FROM markers WHERE file_path LIKE ?", (f"%{mode}%",))]}
            connection.close()

        media = root / "trash-order.mov"
        media.write_bytes(b"media")
        with backend.connect() as connection:
            connection.execute("INSERT INTO file_meta(path,stack_id) VALUES(?,22)", (str(media),))
            connection.execute("INSERT INTO collections(id,name) VALUES(22,'collection')")
            connection.execute("INSERT INTO collection_items(collection_id,file_path,sort_order,added_at) "
                               "VALUES(22,?,17,123)", (str(media),))
            connection.execute("INSERT INTO stacks(id,cover_path,created_at) VALUES(22,?,456)",
                               (str(media),))
        records, errors = backend.trash.trash_items([str(media)])
        restored, restore_errors = backend.trash.restore_items([records[0]["id"]])
        connection = backend.connect()
        results["trash_restore_organization_fields"] = {
            "trash_errors": errors, "restore_errors": restore_errors,
            "restored_count": len(restored), "expected_collection": [17, 123.0],
            "actual_collection": connection.execute(
                "SELECT sort_order,added_at FROM collection_items WHERE collection_id=22").fetchone(),
            "expected_stack_created_at": 456.0,
            "actual_stack_created_at": connection.execute(
                "SELECT created_at FROM stacks WHERE id=22").fetchone()[0]}
        connection.close()
    return results


def import_reproduction() -> dict[str, object]:
    fixture = runpy.run_path(str(ROOT / "tests/edit/test_external_import_compensation.py"))
    view = fixture["_make_view"](fixture["EditProject"].create("audit"), fail_macro=False)

    def broken(*args: object) -> bool:
        raise RuntimeError("clip construction injected error")

    view._add_asset_as_clip = broken
    error = ""
    try:
        fixture["_run"](view, [("/audit/clip-fails.mov", True)])
    except RuntimeError as exc:
        error = str(exc)
    return {"error": error, "assets_left": len(view._project.assets),
            "published_projects": len(view._recorder["published"]),
            "messages": view._recorder["messages"]}


if __name__ == "__main__":
    with TemporaryDirectory(prefix="topos-files-review-") as folder:
        base = Path(folder)
        service_root, backend_root = base / "service", base / "backend"
        service_root.mkdir()
        backend_root.mkdir()
        report = {"service": service_reproductions(service_root),
                  "backend": backend_reproductions(backend_root),
                  "import_clip_failure": import_reproduction()}
        print(json.dumps(report, ensure_ascii=False, indent=2))
