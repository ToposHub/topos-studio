"""G10 旧库/脚本格式统一测试：迁移、脚本 capture、apply/list 协议。

门槛映射：未知 payload 原样保留；断电（journal 中断）可重跑且无重复；
不删来源；脚本抓帧产出 Gallery 可见 bundle；apply/list 与 Gallery 同一
资源；只读来源可查询不被误写。
"""
import json
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.color.core.gallery_legacy_migration import (
    KIND_CONVERT,
    KIND_COPY,
    GalleryLegacyMigration,
)
from src.features.color.core.gallery_repository import GalleryFolderRepository


def _img(seed=0, h=16, w=24):
    rng = np.random.default_rng(seed)
    return (rng.random((h, w, 3)) * 255).astype(np.uint8)


def _thumb(path):
    import cv2
    cv2.imwrite(path, _img(seed=3))


def _legacy_script_preset(root, name, *, layers=None):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "grade.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": "1.0",
                   "layers": layers if layers is not None
                   else [{"name": "l1", "grading_params": {"exposure": .3}}],
                   "active_layer_index": 0}, fh)
    _thumb(os.path.join(d, "thumbnail.jpg"))
    return d


def _flat_topos(root, stem):
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{stem}.topos")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": []}))
    with open(os.path.join(root, f"{stem}.thumb.jpg"), "wb") as fh:
        fh.write(b"t")
    return path


@pytest.fixture
def migration(tmp_path):
    root = tmp_path / "Presets"
    (root / "User").mkdir(parents=True)
    repository = GalleryFolderRepository(str(root))
    return GalleryLegacyMigration(repository), str(root)


class TestScanAndMigrate:
    def test_scan_inventories_both_formats_with_conflict_preview(
            self, migration, tmp_path):
        service, root = migration
        legacy = tmp_path / "legacy"
        _legacy_script_preset(str(legacy), "Scripted")
        _flat_topos(str(legacy), "Flat")
        _flat_topos(os.path.join(root, "User"), "Conflict")  # 目标已有同名
        items = {i.name: i for i in service.scan(str(legacy))}
        assert items["Scripted"].kind == KIND_CONVERT
        assert items["Flat"].kind == KIND_COPY
        assert items["Scripted"].conflict is None
        # scan 只读：目标/来源都没动
        assert not os.path.exists(os.path.join(root, "User", "Scripted.topos"))

    def test_migrate_converts_script_format_visible_to_gallery(
            self, migration, tmp_path):
        service, root = migration
        legacy = tmp_path / "legacy"
        layers = [{"name": "l1", "grading_params": {"exposure": .3},
                   "unknown_future_field": {"keep": [1, 2]}}]
        _legacy_script_preset(str(legacy), "Scripted", layers=layers)
        result = service.migrate(str(legacy))
        assert result.succeeded
        user = os.path.join(root, "User")
        assert os.path.isfile(os.path.join(user, "Scripted.topos"))
        assert os.path.isfile(os.path.join(user, "Scripted.thumb.jpg"))
        with zipfile.ZipFile(os.path.join(user, "Scripted.topos")) as zf:
            project = json.loads(zf.read("project.json"))
        # 未知字段原样保留
        assert project["layers"][0]["unknown_future_field"] == {"keep": [1, 2]}
        # Gallery 查询可见
        from src.features.color.core.gallery_contracts import GalleryQuery
        stills = repository_query(GalleryFolderRepository(root))
        assert "Scripted" in stills
        # 来源不删
        assert os.path.isfile(os.path.join(legacy, "Scripted", "grade.json"))

    def test_migrate_copies_flat_topos_family(self, migration, tmp_path):
        service, root = migration
        legacy = tmp_path / "legacy"
        _flat_topos(str(legacy), "Flat")
        result = service.migrate(str(legacy))
        assert result.succeeded
        user = os.path.join(root, "User")
        assert os.path.isfile(os.path.join(user, "Flat.topos"))
        assert os.path.isfile(os.path.join(user, "Flat.thumb.jpg"))

    def test_rerun_is_idempotent_no_duplicates(self, migration, tmp_path):
        service, root = migration
        legacy = tmp_path / "legacy"
        _legacy_script_preset(str(legacy), "Once")
        assert service.migrate(str(legacy)).succeeded
        again = service.migrate(str(legacy))
        assert again.succeeded and again.affected_still_ids == ()
        user = os.path.join(root, "User")
        names = sorted(n for n in os.listdir(user) if n.startswith("Once"))
        assert names == ["Once.meta.json", "Once.still.jpg",
                         "Once.thumb.jpg", "Once.topos"]

    def test_interrupted_migration_recovers_without_duplicates(
            self, migration, tmp_path, monkeypatch):
        """journal installing 中断：恢复后重跑无重复（journal-first）。"""
        service, root = migration
        legacy = tmp_path / "legacy"
        _legacy_script_preset(str(legacy), "Crash")
        _legacy_script_preset(str(legacy), "Second")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:      # 第一个条目安装中途崩溃
                raise KeyboardInterrupt
            return real_replace(src, dst)
        monkeypatch.setattr(os, "replace", flaky)
        try:
            service.migrate(str(legacy))
        except KeyboardInterrupt:
            pass
        monkeypatch.setattr(os, "replace", real_replace)
        result = service.migrate(str(legacy))   # 重跑：先恢复再迁移
        assert result.succeeded
        user = os.path.join(root, "User")
        tops = sorted(n for n in os.listdir(user) if n.endswith(".topos"))
        assert tops == ["Crash.topos", "Second.topos"]

    def test_conflict_policy_skip_and_suffix(self, migration, tmp_path):
        service, root = migration
        legacy = tmp_path / "legacy"
        _flat_topos(str(legacy), "Taken")
        _flat_topos(str(legacy), "Blocked")
        _flat_topos(os.path.join(root, "User"), "Taken")  # 目标已有同名
        suffix = service.migrate(str(legacy))
        assert suffix.succeeded
        assert "Taken_1" in suffix.affected_still_ids   # 审阅可见
        assert "Blocked" in suffix.affected_still_ids
        # 另一个未迁移来源 + 目标同名 + skip 策略：如实跳过并提示
        _flat_topos(str(legacy), "Blocked")             # 重新种源（未迁移）
        skip = service.migrate(str(legacy), conflict_policy="skip")
        assert skip.succeeded
        assert any("Blocked" in w and "skipped" in w
                   for w in skip.warnings)

    def test_readonly_source_is_never_written(self, migration, tmp_path):
        service, root = migration
        legacy = tmp_path / "ro"
        _legacy_script_preset(str(legacy), "RO")
        if os.geteuid() == 0:
            pytest.skip("root ignores permission bits")
        os.chmod(str(legacy), 0o555)
        try:
            result = service.migrate(str(legacy))
            assert result.succeeded
            assert os.path.isfile(os.path.join(root, "User", "RO.topos"))
            # 来源只读未被触碰（无新文件）
            assert sorted(os.listdir(os.path.join(str(legacy), "RO"))) == \
                ["grade.json", "thumbnail.jpg"]
        finally:
            os.chmod(str(legacy), 0o755)


def repository_query(repository):
    from src.features.color.core.gallery_contracts import GalleryQuery
    return {s.display_name for s in repository.query(
        GalleryQuery(album_id="legacy:User")).stills}


class TestScriptCaptureUnified:
    def _make_mw(self, tmp_path, monkeypatch):
        from src.app.controllers.layer_controller import LayerOpsMixin
        from src.features.color.core.gallery_repository import (
            GalleryFolderRepository,
        )
        monkeypatch.chdir(tmp_path)
        mw = LayerOpsMixin.__new__(LayerOpsMixin)

        class _Engine:
            img_high = _img(seed=9)
            img_full_original = _img(seed=9)

            def grab_viewport_image(self, *a, **k):
                return _img(seed=9)

            def render_full_image_for_scope(self, max_dim):
                return None

        mw.engine = _Engine()
        mw.status_bar = MagicMock()
        mw.current_file_is_video = False
        mw.save_router = MagicMock()

        def _fake_save(path, *_a, **_k):
            with open(path, "wb") as fh:
                fh.write(b"PK-fake")
            return True
        mw.save_router._save_single_asset_project.side_effect = _fake_save
        root = os.path.join(os.getcwd(), "Presets")
        os.makedirs(os.path.join(root, "User"), exist_ok=True)
        mw.presets_panel = MagicMock()
        mw.presets_panel.get_current_folder.return_value = os.path.join(
            root, "User")
        mw.presets_panel._repository = GalleryFolderRepository(root)
        mw.presets_panel.presets_root = root
        mw._page_context_manager = None
        mw._current_edit_project = None
        mw._get_composite_graph_data = lambda: None
        mw._get_composite_state_data = lambda: None
        return mw, root

    def test_script_capture_produces_gallery_visible_bundle(self, tmp_path,
                                                            monkeypatch):
        mw, root = self._make_mw(tmp_path, monkeypatch)
        path = mw.script_capture_still("ScriptedLook")
        assert path and path.endswith("ScriptedLook.topos")
        user = os.path.join(root, "User")
        for suffix in (".topos", ".thumb.jpg", ".still.jpg", ".meta.json"):
            assert os.path.exists(os.path.join(
                user, f"ScriptedLook{suffix}"))
        # Gallery 扫描可见；旧 grade.json 子目录格式不再产生
        assert not os.path.isdir(os.path.join(user, "ScriptedLook"))
        names = repository_query(GalleryFolderRepository(root))
        assert "ScriptedLook" in names
        from src.features.color.core.preset_meta import load_preset_meta
        assert load_preset_meta(path).still_id.startswith("S-")

    def test_script_capture_name_conflict_rejected(self, tmp_path,
                                                   monkeypatch):
        mw, root = self._make_mw(tmp_path, monkeypatch)
        assert mw.script_capture_still("Dup") is not None
        assert mw.script_capture_still("Dup") is None    # 重名拒绝不覆盖

    def test_script_capture_failure_returns_none(self, tmp_path,
                                                 monkeypatch):
        mw, root = self._make_mw(tmp_path, monkeypatch)
        mw.engine.img_high = None
        assert mw.script_capture_still("Nope") is None


class TestScriptApiProtocol:
    """apply_preset/list_presets 与 Gallery 同一资源（协议级测试）。"""

    def _api_self(self, mw):
        class _Bridge:
            @staticmethod
            def call_sync(fn):
                return fn()
        return SimpleNamespace(_bridge=_Bridge,
                               _find_main_window=lambda: mw)

    def test_apply_resolves_gallery_topos_not_cwd(self, tmp_path, monkeypatch):
        mw, root = TestScriptCaptureUnified()._make_mw(tmp_path, monkeypatch)
        _flat_topos(os.path.join(root, "User"), "API")
        applied = []
        mw.on_apply_grade_from_preset = applied.append
        api = self._api_self(mw)

        from src.features.scripting import api as api_module
        api_module.ToposAPI.apply_preset(
            api, "API", folder="User")
        assert applied and applied[0].endswith("API.topos")
        # cwd 无关性：从别的目录解析同一资源
        monkeypatch.chdir(tmp_path)
        api_module.ToposAPI.apply_preset(api, "API", folder="User")
        assert len(applied) == 2

    def test_apply_legacy_format_gets_migration_guidance(self, tmp_path,
                                                         monkeypatch):
        mw, root = TestScriptCaptureUnified()._make_mw(tmp_path, monkeypatch)
        legacy_dir = os.path.join(root, "User", "OldStyle")
        os.makedirs(legacy_dir)
        with open(os.path.join(legacy_dir, "grade.json"), "w") as fh:
            fh.write("{}")
        api = self._api_self(mw)
        from src.features.scripting import api as api_module
        with pytest.raises(RuntimeError, match="migrate_legacy_presets"):
            api_module.ToposAPI.apply_preset(api, "OldStyle",
                                                  folder="User")

    def test_list_presets_reads_gallery_repository(self, tmp_path,
                                                   monkeypatch):
        mw, root = TestScriptCaptureUnified()._make_mw(tmp_path, monkeypatch)
        _flat_topos(os.path.join(root, "User"), "Zed")
        _flat_topos(os.path.join(root, "User"), "Alpha")
        api = self._api_self(mw)
        from src.features.scripting import api as api_module
        names = api_module.ToposAPI.list_presets(api)
        assert names == ["Alpha", "Zed"]

    def test_migrate_legacy_presets_end_to_end(self, tmp_path, monkeypatch):
        mw, root = TestScriptCaptureUnified()._make_mw(tmp_path, monkeypatch)
        legacy_root = os.path.join(root, "User")
        _legacy_script_preset(legacy_root, "OldStyle")
        api = self._api_self(mw)
        from src.features.scripting import api as api_module
        summary = api_module.ToposAPI.migrate_legacy_presets(api)
        assert summary["status"] == "success"
        assert "OldStyle" in repository_query(
            GalleryFolderRepository(root))
        # 迁移后 apply 走 .topos（不再要求迁移指引）
        applied = []
        mw.on_apply_grade_from_preset = applied.append
        api_module.ToposAPI.apply_preset(api, "OldStyle", folder="User")
        assert applied[0].endswith("OldStyle.topos")
