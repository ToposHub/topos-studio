"""G03 Gallery bundle 事务服务测试：rename／move／delete 的统一文件事务。

覆盖计划 §5 G03 门槛：逐文件失败注入后原 bundle 可读或有明确可恢复状
态；alpha 声明（.still.exr.json）随 rename/move/delete；同名／大小写
变体／孤立目标 sidecar／只读卷／symlink 越界拒绝；删除走可恢复 trash
且刷新不假称全部成功；跨卷 copy→校验→提交→清源。
"""
import json
import os
import shutil
import zipfile

import pytest

from src.features.color.core.gallery_bundle_service import (
    GalleryBundleService,
    MANIFEST_NAME,
    REASON_NAME_CONFLICT,
    REASON_NAME_CONFLICT_CASE,
    REASON_ORPHAN_SIDECAR,
    REASON_OUTSIDE_ROOT,
    REASON_READONLY,
    REASON_ROLLBACK_FAILED,
    REASON_SOURCE_MISSING,
    REASON_SYMLINK_MEMBER,
    REASON_UNSAFE_NAME,
    STATE_INSTALLING,
    TXN_DIR_PREFIX,
    _MoveTransaction,
)
from src.features.color.core.gallery_contracts import (
    BUNDLE_SUFFIX_BY_ROLE,
    OP_SUCCESS,
)

ALL_SUFFIXES = tuple(BUNDLE_SUFFIX_BY_ROLE.values())


def _make_zip_topos(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": []}))


def _seed_bundle(folder, stem, *, members=ALL_SUFFIXES, content=None):
    os.makedirs(folder, exist_ok=True)
    for suffix in members:
        path = os.path.join(folder, f"{stem}{suffix}")
        if suffix == ".topos":
            _make_zip_topos(path)
        elif suffix.endswith(".json"):
            payload = content.get(suffix) if content else None
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload or {"schema": 2, "still_id": "S-x"}, fh)
        else:
            with open(path, "wb") as fh:
                fh.write(b"data-" + suffix.encode())


@pytest.fixture
def svc(tmp_path):
    root = tmp_path / "Presets"
    (root / "User").mkdir(parents=True)
    (root / "System").mkdir(parents=True)
    return GalleryBundleService(str(root))


def _folder(svc, name="User"):
    return os.path.join(svc._root, name)


def _exists(folder, stem, suffix):
    return os.path.exists(os.path.join(folder, f"{stem}{suffix}"))


class TestValidation:
    @pytest.mark.parametrize("operation", ["move", "delete"])
    def test_source_stem_cannot_escape_root(self, svc, tmp_path, operation):
        outside = tmp_path / "Outside.topos"
        outside.write_bytes(b"must remain untouched")
        stem = "../../Outside"
        folder = _folder(svc)
        result = (svc.rename_bundle(folder, stem, "New")
                  if operation == "move" else svc.delete_bundle(folder, stem, permanent=True))
        assert result.reason == REASON_UNSAFE_NAME
        assert outside.read_bytes() == b"must remain untouched"
        assert not os.path.exists(os.path.join(folder, "New.topos"))

    def test_rename_moves_all_six_members_including_exr_declaration(self, svc):
        folder = _folder(svc)
        _seed_bundle(folder, "Old")
        result = svc.rename_bundle(folder, "Old", "New")
        assert result.succeeded and result.status == OP_SUCCESS
        for suffix in ALL_SUFFIXES:
            assert _exists(folder, "New", suffix), suffix
            assert not _exists(folder, "Old", suffix), suffix

    def test_unsafe_name_rejected(self, svc):
        folder = _folder(svc)
        _seed_bundle(folder, "A")
        for bad in ("../evil", "a/b", "a\\b", "..", "", "  "):
            result = svc.rename_bundle(folder, "A", bad)
            assert result.reason == REASON_UNSAFE_NAME

    def test_exact_name_conflict_rejected(self, svc):
        folder = _folder(svc)
        _seed_bundle(folder, "A")
        _seed_bundle(folder, "B")
        result = svc.rename_bundle(folder, "A", "B")
        assert result.reason == REASON_NAME_CONFLICT
        # 原 bundle 原样
        assert _exists(folder, "A", ".topos") and _exists(folder, "B", ".topos")

    def _fs_case_sensitive(folder) -> bool:
        probe_a = os.path.join(folder, ".case-probe-A")
        probe_b = os.path.join(folder, ".case-probe-a")
        with open(probe_a, "w") as fh:
            fh.write("x")
        if os.path.exists(probe_b):
            os.remove(probe_a)
            return False
        with open(probe_b, "w") as fh:
            fh.write("x")
        sensitive = os.path.exists(probe_a) and os.path.exists(probe_b)
        os.remove(probe_a)
        os.remove(probe_b)
        return sensitive

    def test_case_only_rename_allowed_on_insensitive_fs(self, svc):
        """纯大小写改名（目标无其他冲突）在大小写不敏感卷上允许。"""
        folder = _folder(svc)
        _seed_bundle(folder, "Still", members=(".topos",))
        result = svc.rename_bundle(folder, "Still", "STILL")
        assert result.succeeded
        if not TestValidation._fs_case_sensitive(folder):
            # 不敏感卷：文件以最后一次安装的大小写呈现
            assert os.path.exists(os.path.join(folder, "STILL.topos"))

    def test_case_variant_against_existing_rejected(self, svc):
        folder = _folder(svc)
        if not TestValidation._fs_case_sensitive(folder):
            pytest.skip("case-insensitive filesystem cannot host both")
        _seed_bundle(folder, "Alpha", members=(".topos",))
        with open(os.path.join(folder, "alpha.topos"), "wb") as fh:
            fh.write(b"other")   # 大小写不同的另一条目
        result = svc.rename_bundle(folder, "Alpha", "ALPHA")
        assert result.reason == REASON_NAME_CONFLICT_CASE

    def test_orphan_target_sidecar_rejected(self, svc):
        """目标名有 sidecar 无 .topos：不覆盖不属于本事务的文件。"""
        folder = _folder(svc)
        _seed_bundle(folder, "A")
        with open(os.path.join(folder, "New.still.exr.json"), "w") as fh:
            fh.write("{}")
        result = svc.rename_bundle(folder, "A", "New")
        assert result.reason == REASON_ORPHAN_SIDECAR
        assert os.path.exists(os.path.join(folder, "New.still.exr.json"))

    def test_source_missing_rejected(self, svc):
        result = svc.rename_bundle(_folder(svc), "Ghost", "X")
        assert result.reason == REASON_SOURCE_MISSING

    def test_outside_root_rejected(self, svc, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        _seed_bundle(str(outside), "A", members=(".topos",))
        result = svc.rename_bundle(str(outside), "A", "B")
        assert result.reason == REASON_OUTSIDE_ROOT

    def test_symlink_escaping_folder_rejected(self, svc, tmp_path):
        """目录 symlink 越界：realpath 归属拒绝（不止 startswith）。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        _seed_bundle(str(outside), "A", members=(".topos",))
        link = os.path.join(_folder(svc), "link")
        os.symlink(str(outside), link)
        result = svc.rename_bundle(link, "A", "B")
        assert result.reason == REASON_OUTSIDE_ROOT

    def test_symlink_member_rejected(self, svc):
        folder = _folder(svc)
        _seed_bundle(folder, "A", members=(".topos",))
        os.symlink(os.path.join(folder, "A.topos"),
                   os.path.join(folder, "A.thumb.jpg"))
        result = svc.rename_bundle(folder, "A", "B")
        assert result.reason == REASON_SYMLINK_MEMBER

    def test_readonly_folder_rejected(self, svc):
        if os.geteuid() == 0:
            pytest.skip("root ignores permission bits")
        folder = _folder(svc)
        _seed_bundle(folder, "A", members=(".topos",))
        os.chmod(folder, 0o555)
        try:
            result = svc.rename_bundle(folder, "A", "B")
            assert result.reason == REASON_READONLY
        finally:
            os.chmod(folder, 0o755)


class TestFailureInjection:
    def test_mid_install_failure_rolls_back_everything(self, svc, monkeypatch):
        folder = _folder(svc)
        _seed_bundle(folder, "Old")
        before = {p: open(os.path.join(folder, p), "rb").read()
                  for p in os.listdir(folder)}
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst, *a, **k):
            calls["n"] += 1
            if calls["n"] == 4:      # backing 完成后、installing 中段
                raise OSError(13, "Permission denied")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", flaky_replace)
        result = svc.rename_bundle(folder, "Old", "New")
        monkeypatch.setattr(os, "replace", real_replace)
        assert result.status == "rejected" and result.reason == "io_error"
        after = {p: open(os.path.join(folder, p), "rb").read()
                 for p in os.listdir(folder)}
        assert after == before            # 逐字节恢复，零残留
        assert not [n for n in after if n.startswith(TXN_DIR_PREFIX)]

    def test_rollback_failure_keeps_journal_and_reports(self, svc, monkeypatch):
        folder = _folder(svc)
        _seed_bundle(folder, "Old")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst, *a, **k):
            calls["n"] += 1
            if calls["n"] == 4:
                raise OSError(5, "IO error")
            if calls["n"] >= 5:
                raise OSError(5, "rollback also failing")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", flaky_replace)
        result = svc.rename_bundle(folder, "Old", "New")
        monkeypatch.setattr(os, "replace", real_replace)
        assert result.reason == REASON_ROLLBACK_FAILED
        assert result.recoverable is True
        assert any("journal kept" in w for w in result.warnings)
        # journal 保留：下一次操作先恢复，再正常执行
        assert svc.rename_bundle(folder, "Old", "New2").succeeded
        for suffix in ALL_SUFFIXES:
            assert _exists(folder, "New2", suffix)

    def test_crash_mid_install_recovered_on_next_op(self, svc):
        """进程退出遗留 installing journal：下次操作按 manifest 原位放回。"""
        folder = _folder(svc)
        _seed_bundle(folder, "Old", members=(".topos", ".thumb.jpg"))
        # 手工构造中断现场：.topos 已到新名、thumb 还在 staging
        import tempfile
        txn_dir = tempfile.mkdtemp(prefix=TXN_DIR_PREFIX, dir=folder)
        staging = os.path.join(txn_dir, "Old.thumb.jpg")
        os.rename(os.path.join(folder, "Old.thumb.jpg"), staging)
        os.rename(os.path.join(folder, "Old.topos"),
                  os.path.join(folder, "New.topos"))
        with open(os.path.join(txn_dir, MANIFEST_NAME), "w") as fh:
            json.dump({
                "txn_id": "crashed", "state": STATE_INSTALLING,
                "entries": [
                    {"src": os.path.join(folder, "Old.topos"), "staged":
                     os.path.join(txn_dir, "Old.topos"),
                     "dst": os.path.join(folder, "New.topos"),
                     "installed": True},
                    {"src": os.path.join(folder, "Old.thumb.jpg"),
                     "staged": staging,
                     "dst": os.path.join(folder, "New.thumb.jpg"),
                     "installed": False},
                ]}, fh)
        assert svc.rename_bundle(folder, "Existing", "Whatever").reason == \
            REASON_SOURCE_MISSING   # 触发 recover
        # 崩溃事务已收敛：旧名回来了，新名清掉了
        assert _exists(folder, "Old", ".topos")
        assert _exists(folder, "Old", ".thumb.jpg")
        assert not _exists(folder, "New", ".topos")


class TestMoveAndCopy:
    def test_move_between_folders_same_root(self, svc):
        src = _folder(svc, "User")
        dst = _folder(svc, "System")
        _seed_bundle(src, "Keep", content={".meta.json": {
            "schema": 2, "still_id": "S-aaa", "label": "L"}})
        result = svc.move_bundle(src, "Keep", dst)
        assert result.succeeded
        assert _exists(dst, "Keep", ".topos") and _exists(dst, "Keep", ".meta.json")
        assert not _exists(src, "Keep", ".topos")
        with open(os.path.join(dst, "Keep.meta.json")) as fh:
            assert json.load(fh)["still_id"] == "S-aaa"   # 身份随 bundle

    def test_cross_volume_copy_commit_then_remove_source(self, svc):
        src = _folder(svc, "User")
        dst = _folder(svc, "System")
        _seed_bundle(src, "Big")
        result = svc.move_bundle(src, "Big", dst, _prefer_copy=True)
        assert result.succeeded
        for suffix in ALL_SUFFIXES:
            assert _exists(dst, "Big", suffix)
            assert not _exists(src, "Big", suffix)

    def test_cross_volume_size_mismatch_aborts_and_keeps_source(self, svc,
                                                                monkeypatch):
        src = _folder(svc, "User")
        dst = _folder(svc, "System")
        _seed_bundle(src, "Odd")
        real_getsize = os.path.getsize

        def lying_getsize(path):
            size = real_getsize(path)
            if os.path.basename(path).startswith("copy-"):
                return size + 1     # 校验失败
            return size

        monkeypatch.setattr(os.path, "getsize", lying_getsize)
        result = svc.move_bundle(src, "Odd", dst, _prefer_copy=True)
        monkeypatch.setattr(os.path, "getsize", real_getsize)
        assert result.status == "rejected" and result.reason == "io_error"
        for suffix in ALL_SUFFIXES:      # 源完好，目标零残留
            assert _exists(src, "Odd", suffix)
            assert not _exists(dst, "Odd", suffix)


class TestDelete:
    def test_delete_to_recoverable_trash_and_restore(self, svc):
        folder = _folder(svc)
        _seed_bundle(folder, "Doomed")
        result = svc.delete_bundle(folder, "Doomed")
        assert result.succeeded
        for suffix in ALL_SUFFIXES:
            assert not _exists(folder, "Doomed", suffix)
        records = svc.list_trash()
        assert len(records) == 1
        assert records[0]["stem"] == "Doomed"
        restored = svc.restore_from_trash(records[0]["trash_id"])
        assert restored.succeeded
        for suffix in ALL_SUFFIXES:
            assert _exists(folder, "Doomed", suffix)

    def test_restore_rejected_when_slot_taken(self, svc):
        folder = _folder(svc)
        _seed_bundle(folder, "A")
        svc.delete_bundle(folder, "A")
        _seed_bundle(folder, "A", members=(".topos",))   # 原位已重建
        record = svc.list_trash()[0]
        result = svc.restore_from_trash(record["trash_id"])
        assert result.reason == REASON_NAME_CONFLICT

    def test_permanent_delete_leaves_no_trash(self, svc):
        folder = _folder(svc)
        _seed_bundle(folder, "Burn")
        result = svc.delete_bundle(folder, "Burn", permanent=True)
        assert result.succeeded
        assert svc.list_trash() == []
        assert not _exists(folder, "Burn", ".topos")

    def test_partial_delete_reports_failures_honestly(self, svc, monkeypatch):
        folder = _folder(svc)
        _seed_bundle(folder, "Half")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst, *a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(13, "denied")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", flaky_replace)
        result = svc.delete_bundle(folder, "Half")
        monkeypatch.setattr(os, "replace", real_replace)
        assert result.status == "partial"
        assert result.warnings and any("Half." in w for w in result.warnings)
        assert not _exists(folder, "Half", ".topos")     # 第一个成员已进 trash
        assert svc.list_trash()                          # 已进 trash 的可恢复


class TestPanelIntegration:
    def _make_panel(self, tmp_path, monkeypatch, qtbot):
        monkeypatch.chdir(tmp_path)
        from src.features.color.ui import presets_panel
        monkeypatch.setattr(
            presets_panel, "_resolve_presets_root",
            lambda: str(tmp_path / "Presets"))
        widget = presets_panel.PresetsPanel()
        qtbot.addWidget(widget)
        return widget

    def test_panel_delete_goes_to_trash_not_raw_remove(self, tmp_path,
                                                       monkeypatch, qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        folder = panel.get_current_folder()
        _seed_bundle(folder, "ViaPanel")
        result = panel._delete_preset_files(
            os.path.join(folder, "ViaPanel.topos"))
        assert result.succeeded
        assert not _exists(folder, "ViaPanel", ".topos")
        assert panel._bundle_service.list_trash()

    def test_panel_delete_partial_shows_warning(self, tmp_path, monkeypatch,
                                                qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        folder = panel.get_current_folder()
        _seed_bundle(folder, "Partial")

        class _FailResult:
            status = "partial"
            reason = "io_error"
            warnings = ("Partial.still.jpg: [Errno 13] denied",)

        monkeypatch.setattr(
            panel._bundle_service, "delete_bundle",
            lambda folder, stem, permanent=False: _FailResult())
        warnings = []
        from PyQt6.QtWidgets import QMessageBox
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        monkeypatch.setattr(QMessageBox, "warning",
                            lambda *a, **k: warnings.append(a) or 0)

        class _Item:
            @staticmethod
            def data(role):
                return os.path.join(folder, "Partial.topos")

            @staticmethod
            def text():
                return "Partial"
        panel._delete_item(_Item())
        assert len(warnings) == 1

    def test_panel_rename_conflict_reports_reason(self, tmp_path, monkeypatch,
                                                  qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        folder = panel.get_current_folder()
        _seed_bundle(folder, "A")
        _seed_bundle(folder, "B")
        panel.scan_presets()
        item = panel.list_widget.item(0)   # "A"
        from PyQt6.QtWidgets import QInputDialog
        monkeypatch.setattr(
            QInputDialog, "getText", staticmethod(lambda *a, **k: ("B", True)))
        criticals = []
        from PyQt6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "critical",
                            lambda *a, **k: criticals.append(a) or 0)
        panel._rename_preset(item)
        assert len(criticals) == 1
        assert "name_conflict" in criticals[0][2]
        # 双方 bundle 原样
        assert _exists(folder, "A", ".topos") and _exists(folder, "B", ".topos")
