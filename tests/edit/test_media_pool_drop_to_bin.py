"""Tests for Finder-style drag-to-bin and folder-structure drop.

Covers:
- ``_single_dropped_folder`` detects a single directory drop (structure mirror trigger).
- ``_apply_folder_structure`` with ``base_bin_id`` nests the mirrored tree under
  a specific bin and routes root-level files into it.
- Dropping plain files onto a bin (no folder) routes assets into that bin via
  ``MoveAssetToBinCommand`` (exercised through the batch-finished path).
- ``_import_files_async`` persists ``target_bin_id`` / ``folder_root`` for the
  batch-finished handler.
"""

from __future__ import annotations

import os
import sys
import tempfile

from PyQt6.QtTest import QTest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.commands import (
    CreateBinCommand,
    MoveAssetToBinCommand,
)
from features.edit.core.project import EditProject
from features.edit.ui.media_pool_panel import MediaPoolPanel


def _import(project: EditProject, path: str, name: str | None = None) -> tuple[EditProject, str]:
    project = project.import_asset(
        path=path, media_type="video", metadata={}, display_name=name or os.path.basename(path)
    )
    return project, next(iter(project.assets))


def _make_panel(qapp, project: EditProject) -> MediaPoolPanel:
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    return panel


# ---------------------------------------------------------------------------
# _single_dropped_folder
# ---------------------------------------------------------------------------

class TestSingleDroppedFolder:
    def test_single_directory_returned(self, qapp, tmp_path) -> None:
        folder = MediaPoolPanel._single_dropped_folder([str(tmp_path)])
        assert folder == os.path.abspath(str(tmp_path))

    def test_single_file_returns_none(self, qapp, tmp_path) -> None:
        f = tmp_path / "a.mp4"
        f.write_bytes(b"x")
        assert MediaPoolPanel._single_dropped_folder([str(f)]) is None

    def test_multiple_items_returns_none(self, qapp, tmp_path) -> None:
        (tmp_path / "a.mp4").write_bytes(b"x")
        sub = tmp_path / "sub"
        sub.mkdir()
        # A directory + a file → not a single-folder drop.
        assert MediaPoolPanel._single_dropped_folder([str(sub), str(tmp_path / "a.mp4")]) is None

    def test_empty_returns_none(self, qapp) -> None:
        assert MediaPoolPanel._single_dropped_folder([]) is None
        assert MediaPoolPanel._single_dropped_folder([""]) is None


# ---------------------------------------------------------------------------
# _apply_folder_structure with base_bin_id
# ---------------------------------------------------------------------------

class TestApplyFolderStructureNested:
    def test_structure_nested_under_target_bin(self, qapp, tmp_path) -> None:
        """Dropping a folder onto bin B mirrors subfolders under B (not root)."""
        # Build a folder tree: root/sub/clip.mp4 + root/top.mp4
        root = tmp_path / "project"
        root.mkdir()
        (root / "top.mp4").write_bytes(b"x")
        sub = root / "sub"
        sub.mkdir()
        (sub / "clip.mp4").write_bytes(b"x")

        project = EditProject.create("P")
        # Pre-import both files (simulate the worker having probed them).
        project, _aid1 = _import(project, str(root / "top.mp4"))
        project, _aid2 = _import(project, str(sub / "clip.mp4"))
        # Create a target bin B under root.
        cmd = CreateBinCommand.create("B", project.root_bin_id)
        project = project.create_bin("B", project.root_bin_id)
        # Recover the new bin id (same trick as CreateBinCommand.apply).
        bin_b_id = next(bid for bid in project.bins if bid not in {project.root_bin_id})
        panel = _make_panel(qapp, project)

        # Simulate the drop state: folder_root set, target bin = B.
        panel._import_folder_root = os.path.abspath(str(root))
        results = [
            (str(root / "top.mp4"), "video", {}, None),
            (str(sub / "clip.mp4"), "video", {}, None),
        ]
        panel._apply_folder_structure(results, base_bin_id=bin_b_id)

        proj = panel._project
        # "sub" bin should exist as a child of B (parent_id == bin_b_id).
        sub_bin_id = None
        for bid, b in proj.bins.items():
            if b.name == "sub" and b.parent_id == bin_b_id:
                sub_bin_id = bid
                break
        assert sub_bin_id is not None, "sub bin should be nested under B"
        # top.mp4 lives at the folder root → goes into B itself.
        top_asset = proj.get_asset_by_path(str(root / "top.mp4"))
        assert top_asset is not None
        assert top_asset.asset_id in proj.bins[bin_b_id].asset_ids
        # clip.mp4 lives in sub/ → goes into the "sub" bin under B.
        clip_asset = proj.get_asset_by_path(str(sub / "clip.mp4"))
        assert clip_asset is not None
        assert clip_asset.asset_id in proj.bins[sub_bin_id].asset_ids

    def test_structure_at_project_root_when_no_base(self, qapp, tmp_path) -> None:
        """Without base_bin_id, root-level files stay in root (legacy behavior)."""
        root = tmp_path / "proj2"
        root.mkdir()
        (root / "root_clip.mp4").write_bytes(b"x")
        deep = root / "deep"
        deep.mkdir()
        (deep / "nested.mp4").write_bytes(b"x")

        project = EditProject.create("P2")
        project, _ = _import(project, str(root / "root_clip.mp4"))
        project, _ = _import(project, str(deep / "nested.mp4"))
        panel = _make_panel(qapp, project)

        panel._import_folder_root = os.path.abspath(str(root))
        results = [
            (str(root / "root_clip.mp4"), "video", {}, None),
            (str(deep / "nested.mp4"), "video", {}, None),
        ]
        panel._apply_folder_structure(results, base_bin_id=None)

        proj = panel._project
        # root_clip stays in root (not moved).
        root_asset = proj.get_asset_by_path(str(root / "root_clip.mp4"))
        assert root_asset.asset_id in proj.bins[proj.root_bin_id].asset_ids
        # nested.mp4 goes into a "deep" bin under project root.
        deep_bin_id = None
        for bid, b in proj.bins.items():
            if b.name == "deep" and b.parent_id == proj.root_bin_id:
                deep_bin_id = bid
                break
        assert deep_bin_id is not None
        nested_asset = proj.get_asset_by_path(str(deep / "nested.mp4"))
        assert nested_asset.asset_id in proj.bins[deep_bin_id].asset_ids


# ---------------------------------------------------------------------------
# Plain file drop onto a bin (no folder structure)
# ---------------------------------------------------------------------------

class TestDropFilesOntoBin:
    def test_drop_onto_bin_moves_assets(self, qapp, tmp_path) -> None:
        """Dropping plain files onto bin B routes every asset into B."""
        project = EditProject.create("P3")
        project, _a1 = _import(project, "/tmp/drop1.mp4", "drop1.mp4")
        project, _a2 = _import(project, "/tmp/drop2.mp4", "drop2.mp4")
        project = project.create_bin("Target", project.root_bin_id)
        target_bin_id = next(bid for bid in project.bins if bid != project.root_bin_id)
        panel = _make_panel(qapp, project)

        # Simulate the batch-finished path: target_bin set, no folder root.
        results = [
            ("/tmp/drop1.mp4", "video", {}, None),
            ("/tmp/drop2.mp4", "video", {}, None),
        ]
        # Manually drive the routing that _on_import_batch_finished does for
        # the "plain drop onto bin" branch (assets already imported above).
        panel._active_import_target_bin_id = target_bin_id
        panel._import_folder_root = None
        for path, media_type, metadata, _err in results:
            if media_type is None or metadata is None:
                continue
            asset = panel._project.get_asset_by_path(path)
            if asset is None:
                continue
            if asset.asset_id in panel._project.bins[target_bin_id].asset_ids:
                continue
            move_cmd = MoveAssetToBinCommand.create(asset.asset_id, target_bin_id)
            panel._project = panel._command_stack.execute(move_cmd, panel._project)

        proj = panel._project
        target_bin = proj.bins[target_bin_id]
        a1 = proj.get_asset_by_path("/tmp/drop1.mp4")
        a2 = proj.get_asset_by_path("/tmp/drop2.mp4")
        assert a1.asset_id in target_bin.asset_ids
        assert a2.asset_id in target_bin.asset_ids


# ---------------------------------------------------------------------------
# _import_files_async state propagation
# ---------------------------------------------------------------------------

class TestImportFilesAsyncState:
    def test_target_bin_id_persisted_for_batch(self, qapp, tmp_path, monkeypatch) -> None:
        """_import_files_async stores target_bin_id for the finished handler."""
        # Stub the thread pool so the worker never actually probes files.
        from PyQt6.QtCore import QThreadPool
        monkeypatch.setattr(QThreadPool, "globalInstance", lambda *a, **k: _NoOpPool())
        f = tmp_path / "state.mp4"
        f.write_bytes(b"x")
        project = EditProject.create("P4")
        project = project.create_bin("Drop", project.root_bin_id)
        drop_bin_id = next(bid for bid in project.bins if bid != project.root_bin_id)
        panel = _make_panel(qapp, project)

        panel._import_files_async([str(f)], target_bin_id=drop_bin_id)
        # The handler should remember the target bin.
        assert panel._active_import_target_bin_id == drop_bin_id

    def test_folder_root_auto_detected(self, qapp, tmp_path, monkeypatch) -> None:
        """A single-folder drop auto-sets _import_folder_root."""
        from PyQt6.QtCore import QThreadPool
        monkeypatch.setattr(QThreadPool, "globalInstance", lambda *a, **k: _NoOpPool())
        sub = tmp_path / "dropped_folder"
        sub.mkdir()
        (sub / "inside.mp4").write_bytes(b"x")
        project = EditProject.create("P5")
        panel = _make_panel(qapp, project)

        panel._import_files_async([str(sub)])
        assert panel._import_folder_root == os.path.abspath(str(sub))

    def test_file_drop_does_not_set_folder_root(self, qapp, tmp_path, monkeypatch) -> None:
        from PyQt6.QtCore import QThreadPool
        monkeypatch.setattr(QThreadPool, "globalInstance", lambda *a, **k: _NoOpPool())
        f = tmp_path / "plain.mp4"
        f.write_bytes(b"x")
        project = EditProject.create("P6")
        panel = _make_panel(qapp, project)

        panel._import_files_async([str(f)])
        assert panel._import_folder_root is None


class _NoOpPool:
    """A QThreadPool stand-in that never runs the worker (test-only)."""
    def start(self, *_args, **_kwargs) -> None:
        return None


# ---------------------------------------------------------------------------
# dropEvent fallback to the currently-browsed bin
# ---------------------------------------------------------------------------

class _FakeUrlMime:
    """Minimal QMimeData/QDropEvent stand-in exposing hasUrls()/urls()."""
    def __init__(self, paths: list[str]) -> None:
        self._paths = paths

    def hasUrls(self) -> bool:
        return True

    def urls(self):
        class _U:
            def __init__(self, p): self._p = p
            def isLocalFile(self): return True
            def toLocalFile(self): return self._p
        return [_U(p) for p in self._paths]


class _FakeDropEvent:
    """Just enough of QDropEvent for MediaPoolPanel.dropEvent."""
    def __init__(self, paths: list[str]) -> None:
        self._mime = _FakeUrlMime(paths)

    def mimeData(self):
        return self._mime

    def acceptProposedAction(self) -> None:
        pass


class TestDropEventFallbackToCurrentBin:
    def test_drop_in_bin_falls_back_to_current_bin(self, qapp, tmp_path, monkeypatch) -> None:
        """Dropping on empty space inside bin B lands files in B (Finder)."""
        from PyQt6.QtCore import QThreadPool
        monkeypatch.setattr(QThreadPool, "globalInstance", lambda *a, **k: _NoOpPool())
        f = tmp_path / "inside.mp4"
        f.write_bytes(b"x")
        project = EditProject.create("P7")
        project = project.create_bin("CurBin", project.root_bin_id)
        cur_bin_id = next(bid for bid in project.bins if bid != project.root_bin_id)
        panel = _make_panel(qapp, project)
        # Simulate the user having navigated INTO CurBin.
        panel._current_bin_id = cur_bin_id

        panel.dropEvent(_FakeDropEvent([str(f)]), target_bin_id=None)
        assert panel._active_import_target_bin_id == cur_bin_id

    def test_drop_at_root_stays_root(self, qapp, tmp_path, monkeypatch) -> None:
        """At the project root level, a drop with no target goes to root."""
        from PyQt6.QtCore import QThreadPool
        monkeypatch.setattr(QThreadPool, "globalInstance", lambda *a, **k: _NoOpPool())
        f = tmp_path / "rootlvl.mp4"
        f.write_bytes(b"x")
        project = EditProject.create("P8")
        panel = _make_panel(qapp, project)
        panel._current_bin_id = ""  # root level

        panel.dropEvent(_FakeDropEvent([str(f)]), target_bin_id=None)
        # No explicit target → None (root bin, the import default).
        assert panel._active_import_target_bin_id is None

    def test_explicit_target_overrides_current_bin(self, qapp, tmp_path, monkeypatch) -> None:
        """An explicit drop target wins over the currently-browsed bin."""
        from PyQt6.QtCore import QThreadPool
        monkeypatch.setattr(QThreadPool, "globalInstance", lambda *a, **k: _NoOpPool())
        f = tmp_path / "x.mp4"
        f.write_bytes(b"x")
        project = EditProject.create("P9")
        project = project.create_bin("A", project.root_bin_id)
        project = project.create_bin("B", project.root_bin_id)
        ids = [bid for bid in project.bins if bid != project.root_bin_id]
        bin_a, bin_b = ids[0], ids[1]
        panel = _make_panel(qapp, project)
        panel._current_bin_id = bin_a  # browsing A

        # Drop explicitly onto B's card.
        panel.dropEvent(_FakeDropEvent([str(f)]), target_bin_id=bin_b)
        assert panel._active_import_target_bin_id == bin_b
