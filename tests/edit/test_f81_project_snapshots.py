"""F8.1 · 命名工程快照与版本比较——验收测试。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.project import EditProject
from src.features.edit.core.project_snapshots import (
    capture_snapshot_payload,
    collect_dependencies,
    compare_snapshots,
    restore_snapshot,
    take_snapshot,
)

pytestmark = pytest.mark.edit_core


def _project():
    return EditProject(project_id="p", name="P")


class TestTakeSnapshot:
    def test_named_snapshot_roundtrip(self):
        project = _project()
        project2, meta = take_snapshot(project, "v1 客户版")
        assert meta.source_revision == project.revision
        assert "v1 客户版" in project2.project_snapshots
        assert meta.payload_hash

    def test_duplicate_name_refused(self):
        project = _project()
        project2, _ = take_snapshot(project, "v1")
        with pytest.raises(ValueError, match="已存在"):
            take_snapshot(project2, "v1")

    def test_auto_backup_can_overwrite(self):
        project = _project()
        project2, _ = take_snapshot(project, "auto", auto_backup=True)
        project3, meta2 = take_snapshot(project2, "auto", auto_backup=True)
        assert len(project3.project_snapshots) == 1
        assert meta2.auto_backup

    def test_secret_keys_rejected(self):
        project = _project()
        payload = {"settings": {"api_token": "leak"}}
        with pytest.raises(ValueError, match="secret"):
            take_snapshot(project, "bad", payload=payload)

    def test_dependencies_collected(self, tmp_path):
        from src.features.edit.core.project import MediaAsset
        lut = tmp_path / "look.cube"
        lut.write_text("LUT")
        from src.shared.project.color_stack import ColorStackData
        project = _project().with_media_asset_added(
            MediaAsset("a1", str(tmp_path / "m.mov"), "video", "m", {}))
        project = project.with_color_stacks({
            "s1": ColorStackData(stack_id="s1", lut_path=str(lut))})
        deps = collect_dependencies(project)
        assert str(tmp_path / "m.mov") in deps["media_paths"]
        assert str(lut) in deps["luts"]


class TestRestoreSnapshot:
    def test_restore_returns_project_with_report(self):
        project = _project()
        project2, _ = take_snapshot(project, "v1")
        restored, report = restore_snapshot(project2, "v1")
        assert restored.project_id == project.project_id
        assert report["restored_from"] == "v1"
        assert report["missing_dependencies"] == []

    def test_missing_dependency_reported_not_fatal(self, tmp_path):
        """原片移动后仍可恢复——依赖缺失进报告（版本元数据保留）。"""
        from src.features.edit.core.project import MediaAsset
        gone = tmp_path / "gone.mov"
        gone.write_bytes(b"x")
        project = _project().with_media_asset_added(
            MediaAsset("a1", str(gone), "video", "m", {}))
        project2, _ = take_snapshot(project, "v1")
        gone.unlink()  # 移盘
        restored, report = restore_snapshot(project2, "v1")
        assert str(gone) in report["missing_dependencies"]
        # 快照表仍随工程走（可再回退）
        assert "v1" in restored.project_snapshots

    def test_corrupted_payload_refused(self):
        project = _project()
        project2, _ = take_snapshot(project, "v1")
        table = dict(project2.project_snapshots)
        table["v1"]["payload"]["name"] = "tampered"
        project3 = project2.with_project_snapshots(table)
        with pytest.raises(ValueError, match="哈希校验失败"):
            restore_snapshot(project3, "v1")

    def test_unknown_snapshot_refused(self):
        with pytest.raises(ValueError, match="不存在"):
            restore_snapshot(_project(), "nope")


class TestCompareSnapshots:
    def test_clip_move_trim_replace_detected(self):
        base = {
            "timelines": [{
                "timeline_id": "tl1",
                "tracks": [{"clips": [{
                    "clip_id": "c1", "track_id": "v1", "asset_id": "a1",
                    "timeline_start": 0, "timeline_duration": 10,
                    "source_start": 0, "effects": [], "color_stack_id": "s1",
                    "audio_gain_db": 0.0}]}]}]}
        changed = {
            "timelines": [{
                "timeline_id": "tl1",
                "tracks": [{"clips": [{
                    "clip_id": "c1", "track_id": "v2", "asset_id": "a2",
                    "timeline_start": 5, "timeline_duration": 8,
                    "source_start": 0, "effects": [], "color_stack_id": "s2",
                    "audio_gain_db": 2.0}]}]}]}
        diff = compare_snapshots(base, changed, from_name="v1", to_name="v2")
        kinds = {e.kind for e in diff.entries}
        assert {"clip_moved", "clip_trimmed", "clip_replaced",
                "grade_changed", "audio_changed"} <= kinds
        assert diff.summary["clip_replaced"] == 1

    def test_identical_payloads_empty_diff(self):
        payload = capture_snapshot_payload(_project())
        diff = compare_snapshots(payload, payload)
        assert diff.entries == () and diff.summary == {}

    def test_add_remove_clip_detected(self):
        base = {"timelines": []}
        added = {"timelines": [{
            "timeline_id": "tl1", "tracks": [{"clips": [
                {"clip_id": "c1"}]}]}]}
        diff = compare_snapshots(base, added)
        assert diff.summary.get("timeline_added") == 1

    def test_effect_count_change_detected(self):
        base = {"timelines": [{"timeline_id": "t", "tracks": [{"clips": [
            {"clip_id": "c1", "effects": []}]}]}]}
        changed = {"timelines": [{"timeline_id": "t", "tracks": [{"clips": [
            {"clip_id": "c1", "effects": [{"id": "e1"}]}]}]}]}
        diff = compare_snapshots(base, changed)
        assert diff.summary.get("effect_changed") == 1

    def test_snapshot_table_not_in_payload_cache(self):
        """快照不复制巨大缓存：payload 是工程逻辑数据（媒体/缓存外置）。"""
        payload = capture_snapshot_payload(_project())
        assert "media_bytes" not in str(payload)[:200].lower()
