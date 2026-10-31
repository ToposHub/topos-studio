"""M04.4 · 通用序列分类（连号照片 → 单一 image_sequence）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.sequence_classify import classify_import_batch


def _paths(prefix="frame_", ext=".jpg", numbers=range(1, 6), d="/media/a"):
    return [f"{d}/{prefix}{n:04d}{ext}" for n in numbers]


class TestClassify:
    def test_consecutive_grouped_with_metadata(self):
        groups, singles = classify_import_batch(_paths())
        assert len(groups) == 1 and singles == []
        g = groups[0]
        assert g.frame_count == 5
        assert g.numbers == (1, 2, 3, 4, 5)
        assert g.missing_numbers == ()
        meta = g.asset_metadata()
        assert meta["sequence"]["frame_count"] == 5
        assert meta["sequence"]["first_number"] == 1

    def test_gap_diagnosed_still_grouped(self):
        """1001 序列缺 1007：仍成组，缺口显式登记（可诊断）。"""
        nums = [1001, 1002, 1003, 1004, 1005, 1008, 1009, 1010, 1011, 1012]
        groups, _ = classify_import_batch(_paths(numbers=nums))
        assert len(groups) == 1
        assert groups[0].missing_numbers == (1006, 1007)

    def test_too_few_stay_singles(self):
        groups, singles = classify_import_batch(_paths(numbers=[1, 2]))
        assert groups == []
        assert len(singles) == 2

    def test_different_prefix_not_grouped(self):
        paths = _paths(numbers=[1, 2, 3]) + _paths(
            prefix="other_", numbers=[1, 2, 3])
        groups, singles = classify_import_batch(paths)
        assert len(groups) == 2 and singles == []

    def test_mixed_batch_singles_kept(self):
        paths = _paths(numbers=[1, 2, 3, 4]) + [
            "/media/a/random.png", "/media/a/no_trailing_num.jpg"]
        groups, singles = classify_import_batch(paths)
        assert len(groups) == 1
        assert set(singles) == {"/media/a/random.png",
                                "/media/a/no_trailing_num.jpg"}

    def test_sparse_coincidence_not_grouped(self):
        """实到帧占标称域过半才成组——稀疏编号巧合不成组。"""
        paths = _paths(numbers=[10, 50, 90])  # 跨 81 只实到 3
        groups, singles = classify_import_batch(paths)
        assert groups == [] and len(singles) == 3


class TestImportBatchWiring:
    def test_batch_groups_sequence_into_single_asset(self, qtbot):
        """导入批接线：连号静帧 → 一个 image_sequence 素材，其余照常。"""
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel
        from src.features.edit.core.project import EditProject

        project = EditProject(project_id="p", name="P")
        panel = MediaPoolPanel.__new__(MediaPoolPanel)
        panel._project = project
        panel._active_import_batch_id = "b1"
        panel._active_import_target_bin_id = None
        panel._import_folder_root = None
        panel._import_cancelled = False
        panel._import_in_progress = True
        panel._pending_import_files = []
        finished = []
        panel.project_changed = type("S", (), {
            "emit": staticmethod(lambda *a: finished.append(a))})()
        panel.asset_imported = type("S", (), {
            "emit": staticmethod(lambda *a, **k: None)})()
        panel._maybe_auto_ensure_disk_proxy = lambda asset: None
        panel._refresh_tree = lambda **k: None
        panel._finish_import_batch = lambda: None

        seq = _paths(numbers=[1, 2, 3, 4])
        results = [(p, "image", {"width": 1920, "height": 1080}, None)
                   for p in seq]
        panel._on_import_batch_finished("b1", "p", results)

        updated = panel._project
        images = [a for a in updated.assets.values()
                  if a.media_type == "image"]
        seqs = [a for a in updated.assets.values()
                if a.media_type == "image_sequence"]
        assert len(seqs) == 1, "连号静帧应合成单一 image_sequence"
        assert images == []
        meta = seqs[0].metadata["sequence"]
        assert meta["frame_count"] == 4
        # 首帧探测 metadata 的域参考保留在顶层
        assert seqs[0].metadata["width"] == 1920
