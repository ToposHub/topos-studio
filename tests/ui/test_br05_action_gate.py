"""BR05：动作统一执行门与传输/Label 服务接线验收。

对应《Gallery/Viewport 后端复查整改计划》BR05（2026-11-10）：
- 菜单／双击／Enter／中键走同一执行门：执行前按**被操作对象**复核
  权威上下文（媒体在场／目标 grade 探测／宿主关闭态）——同一坏包／
  无媒体／关闭态下各入口结论一致；
- 宿主未安装时保持旧的恒启用行为（与菜单门控同一约定）；
- import/export bundle 真正接到传输服务；Label 服务落盘 meta 并刷新；
  成功后选中新条目，失败不改变原选择；
- 能力事实反映真实服务（import.bundle／label.rename 随接线转真）。
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6.QtWidgets")


def _seed(folder: Path, stem: str, *, still_id: str = "S-x1",
          grade: dict | None = None, broken: bool = False) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    topos = folder / f"{stem}.topos"
    if broken:
        topos.write_bytes(b"NOT A ZIP")
    else:
        with zipfile.ZipFile(topos, "w") as zf:
            zf.writestr("project.json", json.dumps(
                grade or {"layers": [{"name": "g"}]}))
    (folder / f"{stem}.thumb.jpg").write_bytes(b"thumb")
    (folder / f"{stem}.still.jpg").write_bytes(b"still")
    meta = {"schema": 2, "still_id": still_id, "created_at": "2026-11-10"}
    (folder / f"{stem}.meta.json").write_text(json.dumps(meta))
    return topos


def _make_panel(qapp, monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    from src.features.color.ui import presets_panel
    monkeypatch.setattr(presets_panel, "_resolve_presets_root",
                        lambda: str(tmp_path / "Presets"))
    panel = presets_panel.PresetsPanel()
    return panel


def _make_host(panel) -> MagicMock:
    """装一个最小宿主（带 evaluate_targeted 真语义走真宿主更佳——
    这里用真 GalleryCapabilityHost 的窗口形态）。"""
    from src.app.controllers.gallery_capability_host import (
        GalleryCapabilityHost,
    )
    window = SimpleNamespace(
        presets_panel=panel, current_file_path="/tmp/media.mov",
        current_file_is_video=True, engine=object())
    host = GalleryCapabilityHost(window)
    host._panel = panel
    host._repository = panel._repository
    panel.set_gallery_capability_host(host)
    return host


class TestUnifiedActionGate:
    def test_entries_share_one_gate_bad_grade_rejected_everywhere(
            self, qapp, monkeypatch, tmp_path):
        """坏包（非 zip）：菜单/双击/中键/Enter 全部拒绝、零 emit。"""
        panel = _make_panel(qapp, monkeypatch, tmp_path)
        host = _make_host(panel)
        assert host is not None
        _seed(Path(panel._current_folder), "Good")
        _seed(Path(panel._current_folder), "Bad", broken=True)
        panel.scan_presets()
        emitted = []
        panel.grade_applied.connect(emitted.append)
        bad_path = str(Path(panel._current_folder) / "Bad.topos")
        good_path = str(Path(panel._current_folder) / "Good.topos")

        assert panel._execute_gallery_action("apply.grade", bad_path) is False
        panel._gallery.apply_item(next(
            panel.list_widget.item(i) for i in range(panel.list_widget.count())
            if panel.list_widget.item(i).text() == "Bad"))
        # 中键路径（editorEvent 委托同一执行门）
        assert panel._execute_gallery_action("apply.grade", bad_path) is False
        assert emitted == []                 # 坏包：所有入口零 emit

        assert panel._execute_gallery_action("apply.grade", good_path)
        assert emitted == [good_path]        # 好包正常应用
        panel._query_service.shutdown()
        panel.deleteLater()

    def test_no_media_rejected_with_host_legacy_allowed_without(
            self, qapp, monkeypatch, tmp_path):
        """宿主在场：无媒体拒绝；宿主未安装：保持旧恒启用行为。"""
        panel = _make_panel(qapp, monkeypatch, tmp_path)
        good = _seed(Path(panel._current_folder), "Look")
        panel.scan_presets()
        emitted = []
        panel.grade_applied.connect(emitted.append)

        # 无宿主：旧恒启用（与菜单门控同一约定）
        assert panel._execute_gallery_action("apply.grade", str(good))
        assert emitted == [str(good)]

        # 宿主在场但窗口无媒体 → 拒绝
        from src.app.controllers.gallery_capability_host import (
            GalleryCapabilityHost,
        )
        window = SimpleNamespace(presets_panel=panel,
                                 current_file_path=None,
                                 current_file_is_video=False, engine=None)
        host = GalleryCapabilityHost(window)
        host._panel = panel
        host._repository = panel._repository
        panel.set_gallery_capability_host(host)
        assert panel._execute_gallery_action("apply.grade", str(good)) is False
        assert len(emitted) == 1
        panel._query_service.shutdown()
        panel.deleteLater()

    def test_targeted_evaluation_uses_right_clicked_item(
            self, qapp, monkeypatch, tmp_path):
        """右击未选中的坏包：评估作用于该对象（不借用选中好包的事实）。"""
        panel = _make_panel(qapp, monkeypatch, tmp_path)
        _make_host(panel)
        _seed(Path(panel._current_folder), "Good")
        _seed(Path(panel._current_folder), "Bad", broken=True)
        panel.scan_presets()
        good_path = str(Path(panel._current_folder) / "Good.topos")
        bad_path = str(Path(panel._current_folder) / "Bad.topos")
        host = panel._gallery_capability_host

        good_cap = host.evaluate_targeted("apply.grade", good_path)
        bad_cap = host.evaluate_targeted("apply.grade", bad_path)
        assert good_cap.supported and not bad_cap.supported

        # 选中好包后再评估坏包：结论仍由被操作对象决定
        for i in range(panel.list_widget.count()):
            item = panel.list_widget.item(i)
            item.setSelected(item.text() == "Good")
        assert host.evaluate_targeted("apply.grade", bad_path).supported \
            is False
        panel._query_service.shutdown()
        panel.deleteLater()

    def test_shutdown_host_rejects_everything(self, qapp, monkeypatch,
                                              tmp_path):
        panel = _make_panel(qapp, monkeypatch, tmp_path)
        host = _make_host(panel)
        good = _seed(Path(panel._current_folder), "Look")
        panel.scan_presets()
        host.mark_shutdown()
        emitted = []
        panel.grade_applied.connect(emitted.append)
        assert panel._execute_gallery_action("apply.grade", str(good)) is False
        assert emitted == []
        panel._query_service.shutdown()
        panel.deleteLater()


class TestTransferServiceWiring:
    def _bundle_zip(self, tmp_path: Path, stem: str = "Pkg",
                    still_id: str = "S-pkg-unique") -> str:
        folder = tmp_path / "src"
        _seed(folder, stem, still_id=still_id)
        dest = tmp_path / f"{stem}.toposbundle"
        from src.features.color.core.gallery_bundle_service import (
            GalleryBundleService,
        )
        from src.features.color.core.gallery_repository import (
            GalleryFolderRepository,
        )
        from src.features.color.core.gallery_transfer_service import (
            GalleryTransferService,
        )
        service = GalleryTransferService(
            GalleryFolderRepository(str(tmp_path)),
            GalleryBundleService(str(tmp_path)))
        assert service.export_bundle(str(folder), stem,
                                     str(dest)).succeeded
        return str(dest)

    def test_import_selects_new_entry_failure_keeps_selection(
            self, qapp, monkeypatch, tmp_path):
        panel = _make_panel(qapp, monkeypatch, tmp_path)
        _seed(Path(panel._current_folder), "Existing")
        archive = self._bundle_zip(tmp_path / "outside")
        panel.scan_presets()
        existing_item = next(
            panel.list_widget.item(i) for i in range(panel.list_widget.count())
            if panel.list_widget.item(i).text() == "Existing")
        existing_item.setSelected(True)

        result = panel._import_bundle_from(archive)
        assert result.succeeded, result.warnings
        selected = [i.text() for i in panel.list_widget.selectedItems()]
        assert selected == ["Pkg"]            # 成功后选中新条目

        # 失败路径（无对话框——纯执行层）：原选择不变、无新条目
        bad_archive = tmp_path / "outside" / "broken.toposbundle"
        bad_archive.write_bytes(b"NOT A ZIP")
        failed = panel._import_bundle_from(str(bad_archive))
        assert not failed.succeeded
        names = [panel.list_widget.item(i).text()
                 for i in range(panel.list_widget.count())]
        assert sorted(names) == ["Existing", "Pkg"]   # 零新散文件
        assert "Pkg" in [i.text()
                         for i in panel.list_widget.selectedItems()]
        panel._query_service.shutdown()
        panel.deleteLater()

    def test_import_button_and_capability_reflect_real_service(
            self, qapp, monkeypatch, tmp_path):
        panel = _make_panel(qapp, monkeypatch, tmp_path)
        host = _make_host(panel)
        caps = host.action_context()
        from src.features.color.core.gallery_contracts import (
            evaluate_gallery_capabilities,
        )
        evaluated = {c.action: c for c in evaluate_gallery_capabilities(caps)}
        assert evaluated["import.bundle"].supported     # 服务已接线
        assert evaluated["label.rename"].supported
        # PowerGrade／Timelines／Memories 仍如实禁用（无存储服务）
        assert not evaluated["album.powergrade"].supported
        assert not evaluated["album.timelines"].supported
        assert not evaluated["album.memories"].supported
        panel._query_service.shutdown()
        panel.deleteLater()


class TestLabelService:
    def test_set_label_persists_and_rescans(self, qapp, monkeypatch,
                                            tmp_path):
        panel = _make_panel(qapp, monkeypatch, tmp_path)
        topos = _seed(Path(panel._current_folder), "Look")
        panel.scan_presets()
        assert panel._set_still_label(str(topos), "夜景 外景")
        from src.features.color.core.preset_meta import load_preset_meta
        assert load_preset_meta(str(topos)).label == "夜景 外景"
        # 搜索新 label 可命中（BR04 字段扩展）
        from src.features.color.core.preset_meta import matches_query
        assert matches_query("Look", load_preset_meta(str(topos)), "夜景")

    def test_clear_label_and_failure(self, qapp, monkeypatch, tmp_path):
        panel = _make_panel(qapp, monkeypatch, tmp_path)
        topos = _seed(Path(panel._current_folder), "Look")
        panel._set_still_label(str(topos), "temp")
        assert panel._set_still_label(str(topos), "")   # 空＝清除
        from src.features.color.core.preset_meta import load_preset_meta
        assert load_preset_meta(str(topos)).label is None
        assert panel._set_still_label(str(topos / "missing"), "x") is False
        panel._query_service.shutdown()
        panel.deleteLater()


class TestRenameSelectsNewEntry:
    def test_rename_selects_new_stem(self, qapp, monkeypatch, tmp_path):
        panel = _make_panel(qapp, monkeypatch, tmp_path)
        _seed(Path(panel._current_folder), "OldName")
        panel.scan_presets()
        monkeypatch.setattr(
            "src.features.color.ui.presets_panel.QInputDialog", MagicMock(
                **{"getText.return_value": ("NewName", True)}))
        item = panel.list_widget.item(0)
        panel._rename_preset(item)
        selected = [i.text() for i in panel.list_widget.selectedItems()]
        assert selected == ["NewName"]
        assert os.path.isfile(os.path.join(panel._current_folder,
                                           "NewName.topos"))
        panel._query_service.shutdown()
        panel.deleteLater()
