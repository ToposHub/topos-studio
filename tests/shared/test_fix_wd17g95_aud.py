# -*- coding: utf-8 -*-
"""WD17-G95 regression tests for the 16 audited findings in src/shared/project.

One positive and one negative case per finding ID:

- AUD-36805  library.read_project_meta malformed timestamps
- AUD-36806  library.get_project_backup_dir mkdir guard
- AUD-36807  library rename partial-move rollback
- AUD-36814  offline_asset_manager revision carry-over
- AUD-36815  offline_asset_manager virtual asset guards
- AUD-36816  offline_asset_manager unknown project type
- AUD-36901  payload_cache lazy asset preservation
- AUD-36902  payload_cache unreadable archive must not become empty payload
- AUD-36910  project_manager atomic save + status return
- AUD-36911  project_manager autosave thread model
- AUD-36921  save_router orphan color stack reuse
- AUD-37001  schema_v1_spec fail-closed validation
- AUD-37003  topos_project with_* revision preservation
- AUD-37004  topos_project frozen dict sharing
- AUD-37005  topos_project DeliverPresetRef resolution guard
- AUD-37008  topos_project_controller autosave interval bounds
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import types
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.shared.project.color_stack import ColorStackData
from src.shared.project.library import (
    get_project_backup_dir,
    list_project_backups,
    read_project_meta,
    rename_project,
    scan_video_projects,
)
from src.shared.project.offline_asset_manager import OfflineAssetManager
from src.shared.project.payload_cache import (
    ProjectArchivePayload,
    ProjectArchiveReadError,
)
from src.shared.project.project_manager import ProjectManager
from src.shared.project.save_router import SaveRouter
from src.shared.project.schema_v1_spec import validate_json
from src.shared.project.topos_project import (
    ColorStackRef,
    CompositionRef,
    DeliverPresetRef,
    MediaAssetRef,
    TimelineRef,
    ToposProject,
)


# ======================================================================
# Helpers
# ======================================================================

class _ConfigStub:
    """Minimal config double supporting .get(key, default)."""

    def __init__(self, values: dict | None = None) -> None:
        self._values = values or {}

    def get(self, key: str, default=None):
        return self._values.get(key, default)


class _EngineDouble:
    """Minimal ImageEngine stand-in for ProjectManager save paths."""

    def __init__(self) -> None:
        self.layers: list = []
        self.active_layer_index: int = 0
        self.current_raw_params = None

    def get_source_bake_state(self):
        return None

    def get_flip_state(self):
        return None

    def get_video_denoise_state(self):
        return None


def _write_meta_zip(path, manifest: dict, project: dict) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("project.json", json.dumps(project))


@dataclass
class _EditProjectDouble:
    """Lightweight EditProject stand-in accepted by SaveRouter's fallback."""

    project_id: str = "proj-1"
    name: str = "Test"
    assets: dict = field(default_factory=dict)
    bins: dict = field(default_factory=dict)
    timelines: dict = field(default_factory=dict)
    compositions: dict = field(default_factory=dict)
    color_stacks: dict = field(default_factory=dict)
    comp_clips: dict = field(default_factory=dict)
    root_bin_id: str | None = None
    active_timeline_id: str | None = None
    active_comp_clip_id: str | None = None
    created_at: float = 0.0
    modified_at: float = 0.0

    def update_color_stack(self, **kwargs) -> "_EditProjectDouble":
        return replace(self, modified_at=1.0)


# ======================================================================
# AUD-36805 — read_project_meta float() outside protected try
# ======================================================================

class TestAud36805ReadProjectMeta:
    def test_malformed_timestamps_degrade_to_fallback(self, tmp_path):
        """A malformed created_at/modified_at must not raise (negative)."""
        path = tmp_path / "bad.topos"
        _write_meta_zip(
            path,
            manifest={"project_mode": "edit_project", "created_at": "not-a-number"},
            project={"name": "Bad", "created_at": {"nested": True}, "modified_at": None},
        )
        meta = read_project_meta(str(path))
        assert meta is not None
        assert meta.created_at == 0.0
        # modified_at falls back to the file mtime (>= 0), never raises.
        assert meta.modified_at == meta.file_modified_at

    def test_valid_timestamps_parse_unchanged(self, tmp_path):
        """Well-formed timestamps still parse exactly (positive)."""
        path = tmp_path / "good.topos"
        _write_meta_zip(
            path,
            manifest={"project_mode": "edit_project", "created_at": 100.5},
            project={"name": "Good", "modified_at": 200.5},
        )
        meta = read_project_meta(str(path))
        assert meta is not None
        assert meta.created_at == 100.5

    def test_scan_survives_one_malformed_archive(self, tmp_path):
        """One malformed archive must not abort the whole library scan.

        Pre-fix, the float() conversion raised ValueError and killed the
        entire scan. Post-fix the broken archive degrades to fallback
        timestamps and is still listed.
        """
        lib = tmp_path / "lib"
        lib.mkdir()
        _write_meta_zip(
            lib / "broken.topos",
            manifest={"project_mode": "edit_project", "created_at": "garbage"},
            project={"name": "Broken"},
        )
        _write_meta_zip(
            lib / "healthy.topos",
            manifest={"project_mode": "edit_project", "created_at": 5.0},
            project={"name": "Healthy"},
        )
        metas = scan_video_projects(_ConfigStub({"project_library_directory": str(lib)}))
        by_name = {m.name: m for m in metas}
        assert set(by_name) == {"Broken", "Healthy"}
        assert by_name["Broken"].created_at == 0.0
        assert by_name["Healthy"].created_at == 5.0


# ======================================================================
# AUD-36806 — get_project_backup_dir mkdir without OSError guard
# ======================================================================

class TestAud36806BackupDirGuard:
    def test_mkdir_failure_does_not_raise(self, tmp_path):
        """A regular file occupying the backup dir path must not propagate."""
        parent = tmp_path / "projdir"
        parent.mkdir()
        (parent / "backups").write_bytes(b"i am a file, not a directory")
        project = parent / "p.topos"
        project.write_bytes(b"PK\x03\x04")

        result = get_project_backup_dir(str(project))
        assert str(result).endswith("backups")

        # Scan/list paths built on top also stay exception-free.
        assert list_project_backups(str(project)) == []

    def test_mkdir_success_creates_directory(self, tmp_path):
        parent = tmp_path / "fresh"
        parent.mkdir()
        project = parent / "p.topos"
        result = get_project_backup_dir(str(project))
        assert result.is_dir()

    def test_scan_uses_backup_dir_without_raising(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "backups").write_bytes(b"file blocking the dir")
        _write_meta_zip(
            lib / "v.topos",
            manifest={"project_mode": "edit_project"},
            project={"name": "V"},
        )
        metas = scan_video_projects(_ConfigStub({"project_library_directory": str(lib)}))
        assert [m.name for m in metas] == ["V"]


# ======================================================================
# AUD-36807 — rename_project loses partial-move state on failure
# ======================================================================

class TestAud36807RenameRollback:
    def _setup_project(self, tmp_path):
        source = tmp_path / "Alpha.topos"
        _write_meta_zip(
            source,
            manifest={"project_mode": "edit_project"},
            project={"name": "Alpha"},
        )
        (tmp_path / "Alpha.topos.bak").write_bytes(b"bak")
        (tmp_path / "Alpha.topos.autosave").write_bytes(b"autosave")
        return source, tmp_path / "Beta.topos"

    def test_partial_companion_move_is_rolled_back(self, tmp_path, monkeypatch):
        """Failure mid-companion-move restores every completed move."""
        source, target = self._setup_project(tmp_path)

        real_replace = os.replace

        def failing_replace(src, dst, *args, **kwargs):
            if str(dst).endswith(".autosave"):
                raise OSError("injected autosave move failure")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError):
            rename_project(str(source), "Beta")

        # Nothing may remain stranded under the new name.
        assert source.exists()
        assert (tmp_path / "Alpha.topos.bak").exists()
        assert not (tmp_path / "Beta.topos.bak").exists()
        assert not (tmp_path / "Beta.topos.autosave").exists()
        assert not target.exists()

    def test_successful_rename_moves_companions(self, tmp_path):
        """Normal rename still relocates companions (positive control)."""
        source, target = self._setup_project(tmp_path)
        result = rename_project(str(source), "Beta")
        assert result == str(target)
        assert target.exists()
        assert (tmp_path / "Beta.topos.bak").exists()
        assert not source.exists()


# ======================================================================
# AUD-36814 / AUD-36815 / AUD-36816 — offline_asset_manager
# ======================================================================

def _project_with_assets(revision: int = 0) -> ToposProject:
    return ToposProject(
        project_id="proj-1",
        name="P",
        project_mode="edit_project",
        assets={
            "a-real": MediaAssetRef(
                asset_id="a-real",
                path="/nonexistent/clip.mp4",
                media_type="video",
                display_name="clip.mp4",
                is_offline=False,
            ),
            "a-virtual": MediaAssetRef(
                asset_id="a-virtual",
                path="topos://motion_graphic/scene-1",
                media_type="motion_graphic",
                display_name="MG",
                is_offline=False,
            ),
        },
        revision=revision,
    )


class TestAud36814RevisionCarryOver:
    def test_update_offline_status_preserves_revision(self):
        updated = OfflineAssetManager.update_offline_status(_project_with_assets(revision=7))
        assert updated.revision == 7
        # Offline state itself still refreshes for the real asset.
        assert updated.assets["a-real"].is_offline is True

    def test_revision_is_not_reset_to_zero(self):
        updated = OfflineAssetManager.update_offline_status(_project_with_assets(revision=42))
        assert updated.revision != 0


class TestAud36815VirtualAssetGuard:
    def test_get_offline_asset_list_skips_virtual_assets(self):
        offline = OfflineAssetManager.get_offline_asset_list(_project_with_assets())
        assert [a.asset_id for a in offline] == ["a-real"]

    def test_update_offline_status_keeps_virtual_asset_online(self):
        updated = OfflineAssetManager.update_offline_status(_project_with_assets())
        assert updated.assets["a-virtual"].is_offline is False

    def test_check_report_matches_virtual_guard(self):
        report = OfflineAssetManager.check_offline_assets(_project_with_assets())
        assert report.total_assets == 2
        assert report.offline_count == 1
        assert report.offline_assets[0]["asset_id"] == "a-real"


class TestAud36816UnknownProjectType:
    def test_check_offline_assets_raises_for_unknown_type(self):
        with pytest.raises(ValueError, match="Unsupported project type"):
            OfflineAssetManager.check_offline_assets(object())

    def test_consistent_with_relink_error(self):
        errors = []
        for op in (
            lambda: OfflineAssetManager.relink_asset(object(), "x", "/y"),
            lambda: OfflineAssetManager.batch_relink(object(), {}),
            lambda: OfflineAssetManager.check_offline_assets(object()),
        ):
            try:
                op()
            except ValueError as exc:
                errors.append(str(exc))
        assert len(errors) == 3
        assert all("Unsupported project type" in e for e in errors)


# ======================================================================
# AUD-36901 / AUD-36902 — payload_cache
# ======================================================================

_BIG_ASSET = bytes(range(256)) * 512  # 128 KiB stand-in for a large binary asset


def _make_archive_with_assets(path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"project_id": "p1"}))
        zf.writestr("assets/baked/big.bin", _BIG_ASSET)


class TestAud36901LazyAssetPreservation:
    def test_load_from_file_defers_assets_out_of_memory(self, tmp_path):
        src = tmp_path / "src.topos"
        _make_archive_with_assets(src)

        payload = ProjectArchivePayload.load_from_file(str(src))

        # Asset bytes are NOT resident; a lazy member reference is kept.
        assert "assets/baked/big.bin" not in payload.unknown_files
        assert "assets/baked/big.bin" in payload.preserved_members
        # Small JSON modules are still parsed eagerly.
        assert payload.manifest == {"project_id": "p1"}

    def test_write_to_zip_streams_preserved_assets_verbatim(self, tmp_path):
        src = tmp_path / "src.topos"
        _make_archive_with_assets(src)
        payload = ProjectArchivePayload.load_from_file(str(src))

        dest = tmp_path / "dest.topos"
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            payload.write_to_zip(zf)

        with zipfile.ZipFile(dest) as zf:
            assert zf.read("assets/baked/big.bin") == _BIG_ASSET
            # No re-compression: preserved assets are written STORED.
            assert zf.getinfo("assets/baked/big.bin").compress_type == zipfile.ZIP_STORED
            assert json.loads(zf.read("manifest.json")) == {"project_id": "p1"}

    def test_vanished_source_fails_loudly(self, tmp_path):
        src = tmp_path / "src.topos"
        _make_archive_with_assets(src)
        payload = ProjectArchivePayload.load_from_file(str(src))

        os.replace(src, tmp_path / "gone.topos")
        with pytest.raises(ProjectArchiveReadError):
            with zipfile.ZipFile(tmp_path / "out.topos", "w") as zf:
                payload.write_to_zip(zf)

    def test_read_from_zip_default_stays_eager_for_compat(self, tmp_path):
        src = tmp_path / "src.topos"
        _make_archive_with_assets(src)
        with zipfile.ZipFile(src) as zf:
            payload = ProjectArchivePayload.read_from_zip(zf)
        assert payload.unknown_files["assets/baked/big.bin"] == _BIG_ASSET
        assert payload.preserved_members == {}

    def test_stream_opened_archive_falls_back_to_eager(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("assets/x.bin", b"XYZ")
        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            payload = ProjectArchivePayload.read_from_zip(zf, defer_assets=True)
        # No re-openable path -> eager fallback keeps the payload complete.
        assert payload.unknown_files["assets/x.bin"] == b"XYZ"


class TestAud36902UnreadableArchive:
    def test_corrupt_existing_file_raises(self, tmp_path):
        corrupt = tmp_path / "corrupt.topos"
        corrupt.write_bytes(b"this is not a zip")
        with pytest.raises(ProjectArchiveReadError):
            ProjectArchivePayload.load_from_file(str(corrupt))

    def test_missing_file_still_returns_empty_payload(self):
        payload = ProjectArchivePayload.load_from_file("/no/such/file.topos")
        assert payload.manifest == {}
        assert payload.unknown_files == {}

    def test_save_does_not_clobber_corrupt_archive(self, tmp_path):
        """ToposProjectIO.save must fail (not overwrite) a corrupt .topos."""
        from src.shared.project.topos_project_io import ToposProjectIO

        corrupt = tmp_path / "bad.topos"
        corrupt.write_bytes(b"NOT-A-ZIP")
        project = ToposProject.create_single_asset("T", "/tmp/test.jpg")

        result = ToposProjectIO.save(project=project, filepath=str(corrupt))

        assert result is False
        assert corrupt.read_bytes() == b"NOT-A-ZIP"


# ======================================================================
# AUD-36910 / AUD-36911 — project_manager
# ======================================================================

class TestAud36910AtomicSave:
    def test_save_project_returns_true_and_writes_valid_zip(self, tmp_path):
        image = tmp_path / "img.png"
        image.write_bytes(b"png")
        pm = ProjectManager()
        assert pm.save_project(str(image), _EngineDouble()) is True
        project_path = pm.get_project_path(str(image))
        assert zipfile.is_zipfile(project_path)
        with zipfile.ZipFile(project_path) as zf:
            data = json.loads(zf.read("project.json"))
        assert data["layers"] == []

    def test_save_project_failure_is_atomic_and_reported(self, tmp_path, monkeypatch):
        image = tmp_path / "img.png"
        image.write_bytes(b"png")
        pm = ProjectManager()
        project_path = Path(pm.get_project_path(str(image)))
        project_path.write_bytes(b"PREVIOUS-CONTENT")

        real_replace = os.replace

        def failing_replace(src, dst, *args, **kwargs):
            if str(dst) == str(project_path):
                raise OSError("injected final swap failure")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", failing_replace)

        assert pm.save_project(str(image), _EngineDouble()) is False
        # The previous archive survives untouched...
        assert project_path.read_bytes() == b"PREVIOUS-CONTENT"
        # ...and no temporary files are left behind.
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_save_project_to_path_returns_status(self, tmp_path):
        pm = ProjectManager()
        out = tmp_path / "sub" / "out.topos"
        assert pm.save_project_to_path(str(out), _EngineDouble()) is True
        assert zipfile.is_zipfile(out)
        assert pm.save_project_to_path(str(out), None) is False

    def test_save_project_to_path_failure_reported(self, tmp_path, monkeypatch):
        out = tmp_path / "out.topos"
        out.write_bytes(b"OLD")
        real_replace = os.replace

        def failing_replace(src, dst, *args, **kwargs):
            if str(dst) == str(out):
                raise OSError("injected final swap failure")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", failing_replace)
        assert pm_save_to_path(str(out)) is False
        assert out.read_bytes() == b"OLD"


def pm_save_to_path(out: str) -> bool:
    return ProjectManager().save_project_to_path(out, _EngineDouble())


class TestAud36911AutosaveThreading:
    def test_autosave_uses_main_thread_qtimer(self, qapp, monkeypatch):
        pm = ProjectManager()
        monkeypatch.setattr(ProjectManager, "AUTOSAVE_DEBOUNCE_SECONDS", 0.02)

        fired: dict = {}

        def fake_save(image_path, engine, **kwargs):
            fired["called"] = True
            fired["thread_ident"] = threading.get_ident()
            return True

        monkeypatch.setattr(pm, "save_project", fake_save)

        pm.schedule_autosave("/tmp/pic.png", _EngineDouble())

        # The Qt debounce path must be chosen, not the background timer.
        assert pm._autosave_qtimer is not None and pm._autosave_qtimer.isActive()
        assert pm.autosave_timer is None

        from PyQt6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        pm._autosave_qtimer.timeout.connect(loop.quit)
        QTimer.singleShot(5000, loop.quit)  # watchdog
        loop.exec()

        assert fired.get("called") is True
        assert fired["thread_ident"] == threading.main_thread().ident

    def test_fallback_timer_without_qapplication(self, qapp, monkeypatch):
        monkeypatch.setattr(ProjectManager, "AUTOSAVE_DEBOUNCE_SECONDS", 60)
        monkeypatch.setattr(
            "PyQt6.QtCore.QCoreApplication.instance", lambda: None
        )
        pm = ProjectManager()
        pm.schedule_autosave("/tmp/pic.png", _EngineDouble())
        try:
            assert isinstance(pm.autosave_timer, threading.Timer)
            assert pm._autosave_qtimer is None
        finally:
            if pm.autosave_timer:
                pm.autosave_timer.cancel()


# ======================================================================
# AUD-36921 — save_router orphan color stacks
# ======================================================================

class TestAud36921OrphanStacks:
    def test_find_existing_color_stack_id_matches(self):
        project = _EditProjectDouble(
            color_stacks={
                "s-clip": ColorStackData(stack_id="s-clip", asset_id="a1", clip_id="clip-1"),
                "s-asset": ColorStackData(stack_id="s-asset", asset_id="a1", clip_id=None),
            }
        )
        finder = SaveRouter._find_existing_color_stack_id
        assert finder(project, asset_id="a1", clip_id="clip-1") == "s-clip"
        assert finder(project, asset_id="a1", clip_id=None) == "s-asset"
        assert finder(project, asset_id="zzz", clip_id=None) is None
        assert finder(_EditProjectDouble(), asset_id="a1", clip_id="clip-9") is None

    def _make_router(self):
        router = SaveRouter()
        engine = MagicMock()
        engine.layers = []
        engine.active_layer_index = 0
        project = _EditProjectDouble()
        io = MagicMock()
        io.save_project.return_value = True
        created_ids: list[str] = []
        current: dict = {"p": project}

        router.set_callbacks({
            "get_engine": lambda: engine,
            "get_current_edit_project": lambda: current["p"],
            "get_edit_project_io": lambda: io,
            "get_ui_state": lambda: None,
            "get_current_timeline_sequences": lambda: None,
            "on_edit_project_updated": lambda updated: current.__setitem__("p", updated),
            "on_color_stack_id_created": created_ids.append,
        })
        return router, engine, current, created_ids

    def test_repeated_saves_without_stack_id_reuse_one_stack(self):
        router, engine, current, created_ids = self._make_router()

        ok1 = router._save_color_to_edit_project(
            "/x/p.topos", current["p"], engine,
            asset_id="a1", clip_id="clip-1", stack_id=None, timeline_id=None,
        )
        assert ok1 is True
        assert len(created_ids) == 1  # one fresh id minted + reported

        ok2 = router._save_color_to_edit_project(
            "/x/p.topos", current["p"], engine,
            asset_id="a1", clip_id="clip-1", stack_id=None, timeline_id=None,
        )
        assert ok2 is True
        # AUD-36921: the second save reuses the existing clip stack instead
        # of piling up another orphan.
        assert len(created_ids) == 1
        assert len(current["p"].color_stacks) == 1
        stack = next(iter(current["p"].color_stacks.values()))
        assert stack.clip_id == "clip-1"
        assert stack.asset_id == "a1"


# ======================================================================
# AUD-37001 — schema_v1_spec fail-open
# ======================================================================

class _StubValidationError(Exception):
    pass


def _install_jsonschema_stub(monkeypatch, *, fail_on_empty: bool) -> None:
    stub = types.ModuleType("jsonschema")

    def _validate(data, schema):  # minimal stand-in validator
        if fail_on_empty and not data:
            raise _StubValidationError("empty data")

    stub.validate = _validate
    stub.ValidationError = _StubValidationError
    monkeypatch.setitem(sys.modules, "jsonschema", stub)


class TestAud37001FailClosed:
    def test_missing_jsonschema_raises_instead_of_passing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "jsonschema", None)  # force ImportError
        with pytest.raises(RuntimeError, match="AUD-37001 fail-closed"):
            validate_json({"a": 1}, "manifest")

    def test_valid_data_passes_with_validator_present(self, monkeypatch):
        _install_jsonschema_stub(monkeypatch, fail_on_empty=False)
        assert validate_json({"a": 1}, "manifest") is True

    def test_invalid_data_fails_with_validator_present(self, monkeypatch):
        _install_jsonschema_stub(monkeypatch, fail_on_empty=True)
        assert validate_json({}, "manifest") is False

    def test_unknown_schema_name_fails(self, monkeypatch):
        _install_jsonschema_stub(monkeypatch, fail_on_empty=False)
        assert validate_json({"a": 1}, "no-such-schema") is False


# ======================================================================
# AUD-37003 — with_* factories drop revision
# ======================================================================

class TestAud37003RevisionPreserved:
    def test_all_with_factories_carry_revision(self):
        project = ToposProject(project_id="p", name="n", revision=9)
        asset = MediaAssetRef(asset_id="a", path="/tmp/a.jpg", media_type="image",
                              display_name="a")
        derived = [
            project.with_name("n2"),
            project.with_asset_added(asset),
            project.with_color_stack_added(ColorStackRef(stack_id="s")),
            project.with_timeline_added(TimelineRef(timeline_id="t", name="T")),
            project.with_composition_added(CompositionRef(composition_id="c")),
            project.with_active_timeline("t"),
            project.with_ui_state({"k": 1}),
        ]
        assert len(derived) == 7
        for d in derived:
            assert d.revision == 9, f"{type(d).__name__} lost revision"

    def test_revision_defaults_to_zero_for_new_projects(self):
        project = ToposProject(project_id="p", name="n")
        assert project.revision == 0
        assert project.with_name("x").revision == 0


# ======================================================================
# AUD-37004 — frozen dataclass shares mutable dicts
# ======================================================================

class TestAud37004NoSharedDicts:
    def test_with_name_does_not_share_ui_state(self):
        project = ToposProject(
            project_id="p", name="n", ui_state={"a": 1}, settings={"b": 2}
        )
        derived = project.with_name("n2")
        assert derived.ui_state is not project.ui_state
        assert derived.settings is not project.settings

        project.ui_state["mutated"] = True
        project.settings["mutated"] = True
        assert "mutated" not in derived.ui_state
        assert "mutated" not in derived.settings

    def test_constructor_copies_caller_dicts(self):
        ui = {"a": 1}
        project = ToposProject(project_id="p", name="n", ui_state=ui)
        ui["mutated"] = True
        assert "mutated" not in project.ui_state

    def test_from_dict_breaks_shared_reference(self):
        raw = {
            "project_id": "p",
            "name": "n",
            "ui_state": {"x": 1},
            "settings": {"y": 2},
        }
        project = ToposProject.from_dict(raw)
        raw["ui_state"]["mutated"] = True
        assert "mutated" not in project.ui_state

    def test_to_dict_returns_state_copies(self):
        project = ToposProject(project_id="p", name="n", ui_state={"a": 1})
        snapshot = project.to_dict()
        snapshot["ui_state"]["mutated"] = True
        assert "mutated" not in project.ui_state

    def test_frozen_fields_still_reject_assignment(self):
        project = ToposProject(project_id="p", name="n")
        with pytest.raises(Exception):  # FrozenInstanceError
            project.name = "other"


# ======================================================================
# AUD-37005 — DeliverPresetRef.from_dict IndexError
# ======================================================================

class TestAud37005PresetResolutionGuard:
    @pytest.mark.parametrize(
        "resolution,expected",
        [
            ([1920], (1920, 1080)),           # too short -> default
            ([], (1920, 1080)),               # empty -> default
            ("1920x1080", (1920, 1080)),      # wrong type -> default
            (["x", "y"], (1920, 1080)),       # non-numeric -> default
            ([3840, 2160], (3840, 2160)),     # valid
            ((3840, 2160), (3840, 2160)),     # tuple accepted
            (["1920", "1080"], (1920, 1080)), # numeric strings coerced
        ],
    )
    def test_resolution_variants(self, resolution, expected):
        preset = DeliverPresetRef.from_dict(
            {"preset_id": "pid", "resolution": resolution}
        )
        assert preset.resolution == expected

    def test_bad_preset_does_not_kill_project_load(self):
        data = ToposProject(project_id="p", name="n").to_dict()
        data["deliver_presets"] = {
            "good": DeliverPresetRef(preset_id="good").to_dict(),
            "bad": {"preset_id": "bad", "resolution": [1280]},
        }
        project = ToposProject.from_dict(data)
        assert project.deliver_presets["good"].resolution == (1920, 1080)
        assert project.deliver_presets["bad"].resolution == (1920, 1080)


# ======================================================================
# AUD-37008 — autosave interval bounds
# ======================================================================

class TestAud37008AutoSaveIntervalBounds:
    def test_start_auto_save_rejects_sub_minute_interval(self, qapp):
        from src.shared.project.topos_project_controller import (
            ToposProjectController,
        )
        controller = ToposProjectController()
        with pytest.raises(ValueError):
            controller.start_auto_save(0)
        with pytest.raises(ValueError):
            controller.start_auto_save(-5)

    def test_start_auto_save_rejects_non_int(self, qapp):
        from src.shared.project.topos_project_controller import (
            ToposProjectController,
        )
        controller = ToposProjectController()
        with pytest.raises(ValueError):
            controller.start_auto_save("5")

    def test_set_auto_save_interval_validates(self, qapp):
        from src.shared.project.topos_project_controller import (
            ToposProjectController,
        )
        controller = ToposProjectController()
        with pytest.raises(ValueError):
            controller.set_auto_save_interval(0)
        controller.set_auto_save_interval(10)
        assert controller._auto_save_interval_ms == 10 * 60 * 1000

    def test_from_dict_clamps_truncated_interval(self, qapp):
        """30000 ms truncates to 0 minutes; the clamp must prevent a 0 timer."""
        from src.shared.project.topos_project_controller import (
            ToposProjectController,
        )
        controller = ToposProjectController()
        controller.from_dict({
            "auto_save_enabled": True,
            "auto_save_interval_ms": 30000,
        })
        assert controller._auto_save_interval_ms >= 60 * 1000
        assert controller._auto_save_timer is not None
        assert controller._auto_save_timer.isActive()
        assert controller._auto_save_timer.interval() >= 60 * 1000

    def test_from_dict_sanitizes_garbage_interval(self, qapp):
        from src.shared.project.topos_project_controller import (
            DEFAULT_AUTO_SAVE_INTERVAL_MINUTES,
            ToposProjectController,
        )
        controller = ToposProjectController()
        controller.from_dict({
            "auto_save_enabled": False,
            "auto_save_interval_ms": "not-a-number",
        })
        assert controller._auto_save_interval_ms == (
            DEFAULT_AUTO_SAVE_INTERVAL_MINUTES * 60 * 1000
        )
