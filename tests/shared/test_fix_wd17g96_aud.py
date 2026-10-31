# -*- coding: utf-8 -*-
"""WD17-G96 regression tests for the 6 audited findings in src/shared/project.

One positive and one negative case per finding ID:

- AUD-37009  topos_project_controller thread-safety contract (state lock)
- AUD-37101  topos_project_io manifest merge direction resets provenance
- AUD-37102  topos_project_io load() cache swap race + save() lost update
- AUD-37103  topos_project_io save() silently overwrites unreadable .topos
- AUD-37104  topos_project_io extraction has no zip-bomb caps
- AUD-37112  topos_project_read_service routes untrusted ZIP into extractall
"""

from __future__ import annotations

import json
import os
import threading
import time
import zipfile

import pytest
from PyQt6.QtCore import Qt

from src.shared.project.manifest import ToposManifest
from src.shared.project.payload_cache import ProjectArchivePayload
from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_controller import (
    PageType,
    ToposProjectController,
)
from src.shared.project.topos_project_io import (
    _ARCHIVE_LOCK,
    ToposProjectIO,
)
from src.shared.project.topos_project_read_service import ToposProjectReadService


# ======================================================================
# Helpers
# ======================================================================

def _make_project() -> ToposProject:
    return ToposProject.create_single_asset(
        name="G96",
        asset_path="/tmp/g96.jpg",
        media_type="image",
    )


def _make_base_archive(
    path: str,
    project: ToposProject,
    *,
    manifest_overrides: dict | None = None,
    extra_members: dict[str, bytes] | None = None,
) -> str:
    """Create a minimal valid .topos archive at *path*."""
    manifest_data = ToposManifest.create(
        project_id=project.project_id,
        project_mode=project.project_mode,
        features=project.features,
        primary_asset_id=project.primary_asset_id,
    ).to_dict()
    if manifest_overrides:
        manifest_data.update(manifest_overrides)

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest_data))
        zf.writestr("project.json", json.dumps(project.to_dict()))
        if extra_members:
            for name, raw in extra_members.items():
                zf.writestr(name, raw)
    return path


def _read_zip_json(path: str, arc_name: str) -> dict:
    with zipfile.ZipFile(path, "r") as zf:
        return json.loads(zf.read(arc_name))


def _cache_debris(tmp_path) -> list[str]:
    cache_root = tmp_path / ".topos_cache"
    if not cache_root.exists():
        return []
    return sorted(
        name
        for name in os.listdir(cache_root)
        if ".loading-" in name or ".retired-" in name
    )


# ======================================================================
# AUD-37009 — controller state transitions must be lock-guarded
# ======================================================================

_ALL_PAGES = tuple(PageType)


class TestAud37009ControllerLock:
    def test_concurrent_marks_emit_exactly_one_transition(self, qapp):
        """check-then-act in mark_dirty must be atomic.

        Pre-fix, two threads could both observe the page as clean and both
        emit a clean->dirty transition. Post-fix exactly one transition is
        emitted per clean->dirty cycle, across many racing rounds.
        """
        controller = ToposProjectController()
        transitions: list[bool] = []
        # DirectConnection: the recorder must run in the emitting thread so
        # the count is observable without a spinning event loop.
        controller.dirty_changed.connect(
            lambda event: transitions.append(event.new_state),
            Qt.ConnectionType.DirectConnection,
        )

        worker_count = 8
        for _round in range(30):
            transitions.clear()
            controller.clear_dirty(PageType.COLOR)
            barrier = threading.Barrier(worker_count)

            def worker() -> None:
                barrier.wait()
                controller.mark_dirty(PageType.COLOR, "race")

            threads = [threading.Thread(target=worker) for _ in range(worker_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert controller.is_page_dirty(PageType.COLOR) is True
            # Exactly one clean->dirty transition despite 8 racing writers.
            assert transitions.count(True) == 1, f"round {_round} raced"

    def test_snapshots_complete_under_concurrent_mutation(self, qapp):
        """Snapshot methods must never observe a torn state dict."""
        controller = ToposProjectController()
        stop = threading.Event()

        def mutator() -> None:
            index = 0
            while not stop.is_set():
                page = _ALL_PAGES[index % len(_ALL_PAGES)]
                controller.mark_dirty(page, "churn")
                controller.clear_dirty(page)
                index += 1

        thread = threading.Thread(target=mutator)
        thread.start()
        try:
            for _ in range(500):
                states = controller.get_all_dirty_states()
                assert set(states) == set(_ALL_PAGES)
                snapshot = controller.to_dict()
                assert set(snapshot["page_dirty_states"]) == {
                    page.value for page in _ALL_PAGES
                }
        finally:
            stop.set()
            thread.join()

        controller.clear_all_dirty()
        assert controller.is_dirty() is False

    def test_save_flow_functional(self, qapp):
        """Positive control: save callback clears dirty + stamps timestamp."""
        controller = ToposProjectController()
        controller.set_save_callback(lambda: True)
        controller.mark_dirty(PageType.EDIT, "edit change")

        assert controller.save() is True
        assert controller.is_dirty() is False
        assert controller.get_last_save_timestamp() > 0.0

    def test_save_callback_runs_without_state_lock_starvation(self, qapp):
        """A callback that marks dirty again must not deadlock (reentrancy)."""
        controller = ToposProjectController()

        def callback() -> bool:
            controller.mark_dirty(PageType.DELIVER, "during save")
            return True

        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "before save")

        done = threading.Event()

        def run_save() -> None:
            controller.save()
            done.set()

        thread = threading.Thread(target=run_save)
        thread.start()
        assert done.wait(timeout=5.0), "save deadlocked on the state lock"
        thread.join()


# ======================================================================
# AUD-37101 — manifest merge must not reset provenance fields
# ======================================================================

class TestAud37101ManifestMergeDirection:
    def test_save_preserves_creation_provenance(self, tmp_path):
        """created_at/created_by/required/optional survive a re-save."""
        project = _make_project()
        zf_path = str(tmp_path / "p.topos")
        _make_base_archive(
            zf_path,
            project,
            manifest_overrides={
                "created_at": 123.0,
                "created_by": "Topos Edit",
                "required_features": ["edit_advanced"],
                "optional_features": ["ai_assist"],
                "schema_version": 99,
            },
        )
        time.sleep(0.01)  # ensure modified_at strictly moves forward

        assert ToposProjectIO.save(project=project, filepath=zf_path) is True

        manifest = _read_zip_json(zf_path, "manifest.json")
        assert manifest["created_at"] == 123.0
        assert manifest["created_by"] == "Topos Edit"
        assert manifest["required_features"] == ["edit_advanced"]
        assert manifest["optional_features"] == ["ai_assist"]
        # Version stamps from a newer writer must not be downgraded.
        assert manifest["schema_version"] == 99
        # Mutable state is still refreshed.
        assert manifest["modified_at"] > 123.0
        assert manifest["last_saved_by"]

    def test_fresh_save_gets_fresh_provenance(self, tmp_path):
        """No existing manifest -> defaults, no crash (negative control)."""
        project = _make_project()
        zf_path = str(tmp_path / "fresh.topos")

        assert ToposProjectIO.save(project=project, filepath=zf_path) is True

        manifest = _read_zip_json(zf_path, "manifest.json")
        assert 0.0 < manifest["created_at"] <= time.time() + 5.0
        assert manifest["required_features"] == []
        assert manifest["optional_features"] == []


# ======================================================================
# AUD-37102 — concurrent load/save: lost updates + cache swap race
# ======================================================================

class TestAud37102ArchiveLocking:
    def test_concurrent_saves_do_not_lose_module_updates(self, tmp_path, monkeypatch):
        """The read-modify-write cycle must be serialized per archive.

        Two concurrent saves touching different modules must both land in
        the final archive. Pre-fix, both could read the pre-save payload
        and the last writer would drop the other's module.
        """
        project = _make_project()
        zf_path = str(tmp_path / "c.topos")
        ToposProjectIO.save(project=project, filepath=zf_path)

        original_write = ProjectArchivePayload.write_to_zip

        def slowed_write(self, zf):
            time.sleep(0.05)  # widen the read-modify-write window
            original_write(self, zf)

        monkeypatch.setattr(ProjectArchivePayload, "write_to_zip", slowed_write)

        barrier = threading.Barrier(2)
        results: dict[str, bool] = {}

        def save_with(module_name: str, key: str) -> None:
            barrier.wait()
            results[key] = ToposProjectIO.save(
                project=project,
                filepath=zf_path,
                **{module_name: {"marker": key}},
            )

        thread_a = threading.Thread(
            target=save_with, args=("deliver_presets_data", "A")
        )
        thread_b = threading.Thread(
            target=save_with, args=("ui_state_data", "B")
        )
        thread_a.start()
        thread_b.start()
        thread_a.join()
        thread_b.join()

        assert results == {"A": True, "B": True}
        markers = set()
        with zipfile.ZipFile(zf_path) as zf:
            markers.add(json.loads(zf.read("deliver/presets.json"))["marker"])
            markers.add(json.loads(zf.read("ui/workspace_state.json"))["marker"])
        assert markers == {"A", "B"}, "one save lost the other's module update"

    def test_load_cache_swap_leaves_no_debris(self, tmp_path):
        """Cache dir replacement is a clean rename swap (positive)."""
        project = _make_project()
        zf_path = str(tmp_path / "s.topos")
        ToposProjectIO.save(project=project, filepath=zf_path)
        loaded, full_data = ToposProjectIO.load(zf_path)
        assert loaded is not None and full_data is not None

        # Save a second version with an extra module and reload.
        ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            ai_plans_data={"plans": [{"id": "v2"}]},
        )
        loaded2, full_data2 = ToposProjectIO.load(zf_path)
        assert loaded2 is not None
        assert full_data2["ai_plans"]["plans"][0]["id"] == "v2"
        assert _cache_debris(tmp_path) == []

    def test_failed_load_keeps_previous_cache_intact(self, tmp_path, monkeypatch):
        """A corrupt archive must not wipe the existing cache dir (negative)."""
        project = _make_project()
        zf_path = str(tmp_path / "k.topos")
        ToposProjectIO.save(project=project, filepath=zf_path)
        loaded, _ = ToposProjectIO.load(zf_path)
        assert loaded is not None
        cache_marker = tmp_path / ".topos_cache" / "k" / "project.json"
        assert cache_marker.exists()

        # Corrupt the archive on disk, then attempt to load.
        with open(zf_path, "r+b") as fh:
            fh.seek(0)
            fh.write(b"garbage-not-a-zip!!")

        loaded2, full_data2 = ToposProjectIO.load(zf_path)
        assert loaded2 is None and full_data2 is None
        # The previously extracted cache survives; no staging debris.
        assert cache_marker.exists()
        assert _cache_debris(tmp_path) == []

    def test_archive_lock_is_reentrant_for_same_thread(self, tmp_path):
        """Nested acquire on the same path must not self-deadlock."""
        path = str(tmp_path / "r.topos")
        completed = threading.Event()

        def nested() -> None:
            outer = _ARCHIVE_LOCK.acquire(path)
            try:
                inner = _ARCHIVE_LOCK.acquire(path)
                inner.release()
            finally:
                outer.release()
            completed.set()

        thread = threading.Thread(target=nested)
        thread.start()
        assert completed.wait(timeout=5.0), "archive lock self-deadlocked"
        thread.join()

    def test_archive_lock_serializes_threads(self, tmp_path):
        """Two threads on the same path are mutually exclusive."""
        path = str(tmp_path / "ser.topos")
        active = 0
        peak = 0
        guard = threading.Lock()

        def critical() -> None:
            nonlocal active, peak
            token = _ARCHIVE_LOCK.acquire(path)
            try:
                with guard:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.02)
                with guard:
                    active -= 1
            finally:
                token.release()

        threads = [threading.Thread(target=critical) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert peak == 1, "archive lock allowed concurrent critical sections"


# ======================================================================
# AUD-37103 — unreadable existing .topos must not be overwritten
# ======================================================================

class TestAud37103RefuseOverwriteUnreadable:
    def test_corrupt_existing_archive_save_refused(self, tmp_path):
        """Existing unreadable .topos: save fails, bytes untouched."""
        project = _make_project()
        zf_path = tmp_path / "bad.topos"
        zf_path.write_bytes(b"NOT-A-ZIP")

        result = ToposProjectIO.save(project=project, filepath=str(zf_path))

        assert result is False
        assert zf_path.read_bytes() == b"NOT-A-ZIP"
        # No temp files or lock debris beyond the advisory lock file.
        leftovers = [
            name
            for name in os.listdir(tmp_path)
            if name.endswith(".tmp") or ".loading-" in name
        ]
        assert leftovers == []

    def test_save_to_missing_file_still_succeeds(self, tmp_path):
        """Positive control: no existing file -> normal save path."""
        project = _make_project()
        zf_path = tmp_path / "new.topos"

        assert ToposProjectIO.save(project=project, filepath=str(zf_path)) is True
        assert zipfile.is_zipfile(zf_path)


# ======================================================================
# AUD-37104 — extraction needs zip-bomb caps
# ======================================================================

class TestAud37104ZipBombCaps:
    def _make_zip(self, path: str, members: dict[str, bytes]) -> None:
        with zipfile.ZipFile(path, "w") as zf:
            for name, raw in members.items():
                zf.writestr(name, raw)

    def test_entry_count_cap_enforced(self, tmp_path, monkeypatch):
        from src.shared.project import topos_project_io

        monkeypatch.setattr(topos_project_io, "MAX_ARCHIVE_ENTRIES", 3)
        project = _make_project()
        zf_path = str(tmp_path / "many.topos")
        self._make_zip(
            zf_path,
            {f"edit/part-{i}.json": b"{}" for i in range(5)},
        )

        loaded, full_data = ToposProjectIO.load(zf_path)

        assert loaded is None and full_data is None
        assert _cache_debris(tmp_path) == []
        # project still usable afterwards
        assert project.project_id

    def test_declared_total_size_cap_enforced(self, tmp_path, monkeypatch):
        from src.shared.project import topos_project_io

        monkeypatch.setattr(topos_project_io, "MAX_ARCHIVE_TOTAL_BYTES", 64)
        zf_path = str(tmp_path / "fat.topos")
        self._make_zip(zf_path, {"assets/bomb.bin": b"x" * 256})

        loaded, full_data = ToposProjectIO.load(zf_path)

        assert loaded is None and full_data is None
        assert _cache_debris(tmp_path) == []

    def test_actual_streamed_bytes_enforced_despite_lying_header(
        self, tmp_path, monkeypatch
    ):
        """Even if the declared-size check is bypassed, streaming stops."""
        from src.shared.project import topos_project_io

        monkeypatch.setattr(topos_project_io, "MAX_ARCHIVE_TOTAL_BYTES", 64)
        monkeypatch.setattr(
            ToposProjectIO, "validate_archive_limits", staticmethod(lambda zf: 0)
        )
        zf_path = str(tmp_path / "liar.topos")
        self._make_zip(zf_path, {"assets/bomb.bin": b"x" * 4096})

        loaded, full_data = ToposProjectIO.load(zf_path)

        assert loaded is None and full_data is None
        assert _cache_debris(tmp_path) == []

    def test_normal_archive_still_extracts(self, tmp_path):
        """Positive control: real caps do not reject a sane project."""
        project = _make_project()
        zf_path = str(tmp_path / "ok.topos")
        ToposProjectIO.save(
            project=project,
            filepath=zf_path,
            ai_plans_data={"plans": []},
        )

        loaded, full_data = ToposProjectIO.load(zf_path)

        assert loaded is not None
        assert full_data is not None
        assert full_data["ai_plans"] == {"plans": []}


# ======================================================================
# AUD-37112 — legacy extractall fallback must be guarded
# ======================================================================

class TestAud37112GuardedLegacyFallback:
    def _forbid_project_manager(self, monkeypatch):
        from src.shared.project.project_manager import ProjectManager

        def must_not_load(self, path):  # noqa: ANN001
            raise AssertionError(
                f"unsafe archive must not reach legacy extractall: {path}"
            )

        monkeypatch.setattr(ProjectManager, "load_project", must_not_load)

    def test_path_escape_archive_refused(self, tmp_path, monkeypatch):
        """ZIP with a parent-escaping member never reaches extractall."""
        self._forbid_project_manager(monkeypatch)
        zf_path = str(tmp_path / "escape.topos")
        with zipfile.ZipFile(zf_path, "w") as zf:
            zf.writestr("legacy/project.json", b"{}")
            zf.writestr("../evil.txt", b"payload")

        service = ToposProjectReadService()
        bundle = service.load(zf_path)

        assert bundle is None
        assert not (tmp_path.parent / "evil.txt").exists()

    def test_size_bomb_archive_refused(self, tmp_path, monkeypatch):
        """Over-cap archive is refused before the legacy extraction."""
        from src.shared.project import topos_project_io

        monkeypatch.setattr(topos_project_io, "MAX_ARCHIVE_TOTAL_BYTES", 64)
        self._forbid_project_manager(monkeypatch)
        zf_path = str(tmp_path / "bomb.topos")
        with zipfile.ZipFile(zf_path, "w") as zf:
            zf.writestr("legacy/project.json", b"x" * 256)

        service = ToposProjectReadService()
        assert service.load(zf_path) is None

    def test_safe_legacy_archive_still_routed(self, tmp_path, monkeypatch):
        """Positive control: a safe legacy ZIP still uses the fallback."""
        from src.shared.project.project_manager import ProjectManager

        monkeypatch.setattr(
            ProjectManager,
            "load_project",
            lambda self, path: {"layers": ["legacy-layer"]},
        )
        zf_path = str(tmp_path / "legacy.topos")
        with zipfile.ZipFile(zf_path, "w") as zf:
            zf.writestr("legacy/project.json", b"{}")

        service = ToposProjectReadService()
        bundle = service.load(zf_path)

        assert bundle is not None
        assert bundle.layers == ["legacy-layer"]
