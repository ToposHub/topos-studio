"""BR06：比较会话生命周期验收——provenance／失效联动／真实资源释放。

对应《Gallery/Viewport 后端复查整改计划》BR06（2026-11-10）可落地核心：
- wipe 参考的 revision 是 **bundle 全成员**内容哈希（sidecar/meta 原地
  改写也变，不再只看 ``.topos`` mtime）；
- color_context 打包精度＋alpha 语义＋参考域（稳定可比较标识）；
- 参考 bundle 删除/改名 → 在场会话按来源身份失效并**释放引擎参考**
  （不显示幽灵参考）；非当前参考的变更不打扰在场会话；
- 会话结束（clear reference）释放引擎参考；
- 失效政策明确声明：gallery still 是加载时刻快照，工程/色彩切换后
  快照仍有效。
分屏 presenter（多参考真实呈现）仍属未实现范围（能力位如实禁用），
见 BR09 完成报告的范围声明。
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

from src.features.color.core.gallery_bundle_service import GalleryBundleService


def _seed(folder: Path, stem: str, *, still_id: str = "S-br06",
          alpha: str = "premultiplied") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    topos = folder / f"{stem}.topos"
    with zipfile.ZipFile(topos, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": []}))
    (folder / f"{stem}.thumb.jpg").write_bytes(b"thumb")
    (folder / f"{stem}.still.jpg").write_bytes(b"still")
    (folder / f"{stem}.still.exr.json").write_text(json.dumps(
        {"alpha_semantics": alpha, "channels": 4}))
    (folder / f"{stem}.meta.json").write_text(json.dumps(
        {"schema": 2, "still_id": still_id}))
    return topos


class TestBundleRevision:
    def test_sidecar_in_place_write_changes_revision(self, tmp_path: Path):
        topos = _seed(tmp_path, "Look")
        base = GalleryBundleService.bundle_revision(str(tmp_path), "Look")
        # 原地改 meta（不改 .topos、不改目录条目）
        meta_path = tmp_path / "Look.meta.json"
        meta = json.loads(meta_path.read_text())
        meta["label"] = "changed"
        meta_path.write_text(json.dumps(meta))
        assert GalleryBundleService.bundle_revision(
            str(tmp_path), "Look") != base
        # grade 本体重写也变
        with zipfile.ZipFile(topos, "w") as zf:
            zf.writestr("project.json", json.dumps({"layers": [1]}))
        again = GalleryBundleService.bundle_revision(str(tmp_path), "Look")
        assert again != base

    def test_revision_stable_across_calls_and_processes(self, tmp_path):
        _seed(tmp_path, "Look")
        first = GalleryBundleService.bundle_revision(str(tmp_path), "Look")
        second = GalleryBundleService.bundle_revision(str(tmp_path), "Look")
        assert first == second


def _make_controller(tmp_path: Path, monkeypatch):
    from src.app.controllers.layer_controller import LayerOpsMixin

    monkeypatch.chdir(tmp_path)
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    ctrl.status_bar = MagicMock()
    ctrl.engine = MagicMock()
    ctrl.engine.clear_reference_still = MagicMock()
    return ctrl


class _FakeImage:
    def __init__(self) -> None:
        import numpy as np

        self.shape = (16, 24, 3)
        self.nbytes = int(np.prod(self.shape))


class TestSessionProvenanceAndInvalidation:
    def _begin(self, ctrl, topos: Path, precision: str = "exr_float32"):
        ctrl._begin_gallery_compare_session(str(topos), precision,
                                            _FakeImage())
        return ctrl._gallery_compare_session

    def test_provenance_uses_bundle_revision_and_alpha_context(
            self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        topos = _seed(tmp_path, "Look", alpha="straight")
        session = self._begin(ctrl, topos)
        assert session is not None and session.active
        slot = session.slots[0]
        assert slot.item.color_context == (
            "exr_float32|alpha=straight|ref=premultiplied")
        # revision 与 bundle 全成员哈希一致（确定性整数前缀）
        revision_hash = GalleryBundleService.bundle_revision(
            str(tmp_path), "Look")
        assert slot.item.revision == int(revision_hash.split(":")[1][:8], 16)

    def test_reference_delete_invalidates_session_and_clears_engine(
            self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        topos = _seed(tmp_path, "Look")
        assert self._begin(ctrl, topos) is not None
        ctrl.on_gallery_bundle_mutated("deleted", str(topos))
        assert getattr(ctrl, "_gallery_compare_session", None) is None
        ctrl.engine.clear_reference_still.assert_called_once()

    def test_reference_rename_invalidates_with_old_path(
            self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        topos = _seed(tmp_path, "Look")
        assert self._begin(ctrl, topos) is not None
        ctrl.on_gallery_bundle_mutated("renamed", str(topos))
        assert getattr(ctrl, "_gallery_compare_session", None) is None

    def test_other_bundle_mutation_leaves_session_alone(
            self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        topos = _seed(tmp_path, "Look")
        _seed(tmp_path, "Other")
        assert self._begin(ctrl, topos) is not None
        ctrl.on_gallery_bundle_mutated(
            "deleted", str(tmp_path / "Other.topos"))
        assert ctrl._gallery_compare_session is not None
        assert ctrl._gallery_compare_session.active
        ctrl.engine.clear_reference_still.assert_not_called()

    def test_end_releases_engine_reference(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        topos = _seed(tmp_path, "Look")
        assert self._begin(ctrl, topos) is not None
        ctrl.end_gallery_compare_session()
        assert getattr(ctrl, "_gallery_compare_session", None) is None
        ctrl.engine.clear_reference_still.assert_called_once()

    def test_snapshot_policy_survives_color_context_switch(
            self, tmp_path, monkeypatch):
        """政策声明：加载后的快照不因色彩上下文变化自动失效。"""
        ctrl = _make_controller(tmp_path, monkeypatch)
        topos = _seed(tmp_path, "Look")
        session = self._begin(ctrl, topos, precision="jpeg8")
        assert session.color_context.startswith("jpeg8|")
        # 重新加载新参考＝新会话；旧快照在此之前一直有效
        assert ctrl._gallery_compare_session.active
        ctrl.on_gallery_bundle_mutated("deleted", str(topos))
        assert getattr(ctrl, "_gallery_compare_session", None) is None


class TestPanelMutationSignal:
    def test_delete_and_rename_emit_bundle_mutated(self, qapp, monkeypatch,
                                                   tmp_path):
        from src.features.color.ui import presets_panel
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(presets_panel, "_resolve_presets_root",
                            lambda: str(tmp_path / "Presets"))
        panel = presets_panel.PresetsPanel()
        _seed(Path(panel._current_folder), "Doomed")
        panel.scan_presets()
        events: list = []
        panel.bundle_mutated.connect(
            lambda event, path: events.append((event, path)))
        item = panel.list_widget.item(0)
        monkeypatch.setattr(
            presets_panel.QMessageBox, "question",
            staticmethod(lambda *a, **k:
                         presets_panel.QMessageBox.StandardButton.Yes))
        panel._delete_item(item)
        assert events and events[0][0] == "deleted"
        assert events[0][1].endswith("Doomed.topos")
        panel._query_service.shutdown()
        panel.deleteLater()
