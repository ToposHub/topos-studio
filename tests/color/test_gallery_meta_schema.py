"""G02 Gallery metadata schema 2 测试：稳定身份／独立 label／timeline 来源。

覆盖：schema 1→2 读写兼容与未知字段保留、原子写盘、still_id 分配政策
（随机、不复用、跨 rename 稳定）、rename 首写原子补齐、repository 身份
口径切换、抓帧 meta 的 timeline/工程/album 快照。
"""
import json
import os
import zipfile

import pytest

from src.features.color.core.gallery_contracts import GalleryQuery
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.preset_meta import (
    PresetMeta,
    STILL_META_SCHEMA,
    allocate_still_id,
    ensure_still_identity,
    load_preset_meta,
    meta_path_for,
    save_preset_meta,
)


def _make_zip_topos(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": []}))


def _write_raw_meta(topos, payload):
    with open(meta_path_for(topos), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


def _read_raw_meta(topos):
    with open(meta_path_for(topos), "r", encoding="utf-8") as fh:
        return json.load(fh)


class TestSchema2:
    def test_save_writes_schema2_atomically(self, tmp_path):
        topos = str(tmp_path / "S1.topos")
        _make_zip_topos(topos)
        assert save_preset_meta(topos, PresetMeta(
            still_id=allocate_still_id(), label="My Label",
            timeline_name="Main", display_name="A.mov"))
        raw = _read_raw_meta(topos)
        assert raw["schema"] == STILL_META_SCHEMA == 2
        assert raw["label"] == "My Label"
        assert raw["timeline_name"] == "Main"
        # 原子写：目录里无 tmp 残留
        assert [n for n in os.listdir(tmp_path) if ".tmp-" in n] == []

    def test_schema1_and_missing_schema_load_compatible(self, tmp_path):
        topos = str(tmp_path / "old.topos")
        _make_zip_topos(topos)
        _write_raw_meta(topos, {"schema": 1, "display_name": "A.mov",
                                "clip_id": "c1"})
        meta = load_preset_meta(topos)
        assert meta.display_name == "A.mov"
        assert meta.still_id is None and meta.label is None
        _write_raw_meta(topos, {"display_name": "no-schema"})
        assert load_preset_meta(topos).display_name == "no-schema"

    def test_unknown_fields_preserved_through_roundtrip(self, tmp_path):
        topos = str(tmp_path / "future.topos")
        _make_zip_topos(topos)
        _write_raw_meta(topos, {
            "schema": 99, "display_name": "n", "fancy_new_field": {"a": 1},
            "another": [1, 2]})
        meta = load_preset_meta(topos)
        assert meta.extra_fields == {"fancy_new_field": {"a": 1},
                                     "another": [1, 2]}
        # 重写（schema 升到 2）后未知字段仍在
        meta.label = "edited"
        assert save_preset_meta(topos, meta)
        raw = _read_raw_meta(topos)
        assert raw["schema"] == 2
        assert raw["fancy_new_field"] == {"a": 1} and raw["another"] == [1, 2]
        assert raw["label"] == "edited"
        assert load_preset_meta(topos).extra_fields["another"] == [1, 2]

    def test_to_dict_merges_extra_and_drops_none(self):
        meta = PresetMeta(label="L", extra_fields={"x": 1})
        data = meta.to_dict()
        assert data == {"label": "L", "x": 1}


class TestStillIdentity:
    def test_allocation_format_and_uniqueness(self):
        ids = {allocate_still_id() for _ in range(200)}
        assert len(ids) == 200          # 随机分配：并发/复制不冲突
        for value in ids:
            assert value.startswith("S-") and len(value) == 18

    def test_ensure_identity_idempotent_and_atomic(self, tmp_path):
        topos = str(tmp_path / "Legacy.topos")
        _make_zip_topos(topos)
        still_id, backfilled = ensure_still_identity(topos)
        assert still_id and backfilled is True
        first_raw = _read_raw_meta(topos)
        again, backfilled2 = ensure_still_identity(topos)
        assert (again, backfilled2) == (still_id, False)   # 幂等
        assert _read_raw_meta(topos) == first_raw

    def test_ensure_preserves_existing_fields(self, tmp_path):
        topos = str(tmp_path / "Rich.topos")
        _make_zip_topos(topos)
        _write_raw_meta(topos, {"schema": 1, "display_name": "A.mov",
                                "label": "Keep Me", "custom": 7})
        still_id, _ = ensure_still_identity(topos)
        raw = _read_raw_meta(topos)
        assert raw["still_id"] == still_id
        assert raw["label"] == "Keep Me"
        assert raw["display_name"] == "A.mov"
        assert raw["custom"] == 7 and raw["schema"] == 2

    def test_ensure_write_failure_returns_none_not_fake_id(
            self, tmp_path, monkeypatch):
        topos = str(tmp_path / "RO.topos")
        _make_zip_topos(topos)
        monkeypatch.setattr(
            "src.features.color.core.preset_meta.save_preset_meta",
            lambda *a, **k: False)
        still_id, backfilled = ensure_still_identity(topos)
        assert still_id is None and backfilled is False
        assert not os.path.exists(meta_path_for(topos))


class TestRepositoryIdentity:
    @pytest.fixture
    def repo(self, tmp_path):
        (tmp_path / "Presets" / "User").mkdir(parents=True)
        return GalleryFolderRepository(str(tmp_path / "Presets"))

    def _seed(self, repo, name, meta=None):
        folder = os.path.join(repo.root, "User")
        topos = os.path.join(folder, f"{name}.topos")
        _make_zip_topos(topos)
        with open(os.path.join(folder, f"{name}.thumb.jpg"), "wb") as fh:
            fh.write(b"thumb")
        if meta is not None:
            save_preset_meta(topos, meta)
        return topos

    def test_stable_id_and_label_flow_from_meta(self, repo):
        self._seed(repo, "Tagged", meta=PresetMeta(
            still_id="S-deadbeef12345678", label="Hero Look",
            timeline_id="t1", timeline_name="Main",
            project_id="p1", album_id="legacy:User"))
        still = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        assert still.still_id == "S-deadbeef12345678"
        assert still.label == "Hero Look"
        assert still.provenance.timeline_id == "t1"
        assert still.provenance.timeline_name == "Main"

    def test_identity_survives_rename_when_meta_present(self, repo):
        topos = self._seed(repo, "Before", meta=PresetMeta(
            still_id="S-aaaabbbbccccdddd", label="Keep",
            timeline_name="Doc"))
        folder = os.path.dirname(topos)
        for suffix in (".topos", ".meta.json"):
            os.rename(os.path.join(folder, f"Before{suffix}"),
                      os.path.join(folder, f"After{suffix}"))
        stills = repo.query(GalleryQuery(album_id="legacy:User")).stills
        assert len(stills) == 1
        assert stills[0].display_name == "After"
        assert stills[0].still_id == "S-aaaabbbbccccdddd"
        assert stills[0].label == "Keep"
        assert stills[0].provenance.timeline_name == "Doc"

    def test_legacy_without_meta_uses_provisional_identity(self, repo):
        self._seed(repo, "Legacy")    # 无 meta
        first = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        assert first.still_id and not first.still_id.startswith("S-")
        assert first.label is None
        # 重扫稳定（同名同 album 派生一致）
        again = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        assert again.still_id == first.still_id

    def test_backfilled_identity_replaces_provisional(self, repo):
        topos = self._seed(repo, "Legacy")
        before = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        still_id, _ = ensure_still_identity(topos)
        after = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        assert after.still_id == still_id != before.still_id
        assert after.still_id.startswith("S-")

    def test_sort_by_label_uses_meta_label(self, repo):
        self._seed(repo, "Zeta", meta=PresetMeta(label="AAA Look"))
        self._seed(repo, "Alpha", meta=PresetMeta(label="ZZZ Look"))
        result = repo.query(GalleryQuery(
            album_id="legacy:User", sort_key="label"))
        assert [s.display_name for s in result.stills] == ["Zeta", "Alpha"]


class TestRenameBackfillViaPanel:
    def _make_panel(self, tmp_path, monkeypatch, qtbot):
        monkeypatch.chdir(tmp_path)
        from src.features.color.ui import presets_panel
        monkeypatch.setattr(
            presets_panel, "_resolve_presets_root",
            lambda: str(tmp_path / "Presets"))
        widget = presets_panel.PresetsPanel()
        qtbot.addWidget(widget)
        return widget

    def test_rename_of_legacy_preset_backfills_identity(self, tmp_path,
                                                        monkeypatch, qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        folder = panel.get_current_folder()
        topos = os.path.join(folder, "Old.topos")
        _make_zip_topos(topos)
        panel.scan_presets()
        item = panel.list_widget.item(0)

        class _FakeInput:
            @staticmethod
            def getText(*a, **k):
                return "New", True
        from PyQt6.QtWidgets import QInputDialog
        monkeypatch.setattr(QInputDialog, "getText", _FakeInput.getText)
        panel._rename_preset(item)

        meta = load_preset_meta(os.path.join(folder, "New.topos"))
        assert meta is not None and meta.still_id.startswith("S-")
        # 身份已落盘：再改名不再变
        item2 = panel.list_widget.item(0)
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("Third", True)))
        panel._rename_preset(item2)
        meta2 = load_preset_meta(os.path.join(folder, "Third.topos"))
        assert meta2.still_id == meta.still_id

    def test_rename_backfill_failure_warns_not_fakes(self, tmp_path,
                                                     monkeypatch, qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        folder = panel.get_current_folder()
        _make_zip_topos(os.path.join(folder, "Old.topos"))
        panel.scan_presets()
        item = panel.list_widget.item(0)

        warnings = []
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        monkeypatch.setattr(
            QInputDialog, "getText", staticmethod(lambda *a, **k: ("New", True)))
        monkeypatch.setattr(QMessageBox, "warning",
                            lambda *a, **k: warnings.append(a) or 0)
        monkeypatch.setattr(
            "src.features.color.core.preset_meta.save_preset_meta",
            lambda *a, **k: False)
        panel._rename_preset(item)
        # 文件改名成功（用户动作），但身份补写失败必须可见
        assert os.path.isfile(os.path.join(folder, "New.topos"))
        assert not os.path.exists(os.path.join(folder, "New.meta.json"))
        assert len(warnings) == 1


class TestCaptureMetaProvenance:
    def _make_ctrl(self):
        from src.app.controllers.layer_controller import LayerOpsMixin
        ctrl = LayerOpsMixin.__new__(LayerOpsMixin)

        class _Timeline:
            name = "Main Sequence"
        project = type("P", (), {
            "project_id": "proj-1", "assets": {},
            "get_timeline_by_id": lambda self, tid: _Timeline(),
        })()
        panel = type("Panel", (), {
            "get_current_folder": lambda self: "/x/Presets/User",
            "_repository": GalleryFolderRepository("/x/Presets"),
        })
        ctx = type("Ctx", (), {
            "clip_id": "c1", "asset_id": "a1", "timeline_frame": 5,
            "source_frame": 9, "timeline_id": "t-main",
        })()
        ctrl._page_context_manager = type("M", (), {
            "get_current_context": lambda self: ctx})()
        ctrl._current_edit_project = project
        ctrl.presets_panel = panel()
        return ctrl

    def test_capture_meta_includes_identity_context_snapshots(self):
        ctrl = self._make_ctrl()
        meta = ctrl._collect_still_capture_meta()
        assert meta["timeline_id"] == "t-main"
        assert meta["timeline_name"] == "Main Sequence"   # 快照，非反查
        assert meta["project_id"] == "proj-1"
        assert meta["album_id"] == "legacy:User"
        assert meta["clip_id"] == "c1" and meta["source_frame"] == 9

    def test_capture_meta_without_context_stays_honest(self):
        from src.app.controllers.layer_controller import LayerOpsMixin
        ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
        ctrl._page_context_manager = None
        ctrl._current_edit_project = None
        ctrl.presets_panel = None
        meta = ctrl._collect_still_capture_meta()
        # 无上下文：不推断任何来源字段
        assert "timeline_id" not in meta and "project_id" not in meta
        assert "album_id" not in meta
