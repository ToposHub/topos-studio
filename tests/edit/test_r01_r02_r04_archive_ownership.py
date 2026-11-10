"""R01/R02/R04：归档所有权与 Media Management 任务身份回归。

对应《媒体池后端完成情况复核与整改计划》（2026-11-10）：
- R01：收集/整理完成路径把发起时冻结（或整理重映射）的 timeline
  sequences 随归档工程包保存；保存失败按失败报告，不提示完成；
- R02：collect_project 目标目录预检——既有媒体/LUT/manifest 绝不被
  覆盖（冲突追加序号），失败只清理本任务刚创建的半成品；
- R04：排队任务携带发起时工程+序列快照（切工程不混用）；切工程/
  关闭使在场任务过期（代际递增 + 协作取消），晚到完成回调被丢弃。
"""
from __future__ import annotations

import errno
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.features.edit.core.consolidate import ConsolidateReport
from src.features.edit.core.project import EditProject
from src.features.edit.core.project_archive import (
    STATUS_COPY_FAILED,
    collect_project,
)


def _project_with_asset(tmp_path: Any, name: str = "Audit") -> EditProject:
    source = tmp_path / "source.mov"
    source.write_bytes(b"NEW_SOURCE_CONTENT")
    project = EditProject.create(name).import_asset(
        str(source), "video", {"fps": 25.0, "total_frames": 100})
    return project.create_timeline("Audit timeline")


class TestR02TargetOwnership:
    def test_existing_media_file_never_overwritten(self, tmp_path: Any) -> None:
        project = _project_with_asset(tmp_path)
        target = tmp_path / "existing_target"
        (target / "media").mkdir(parents=True)
        existing = target / "media" / "source.mov"
        existing.write_bytes(b"PREEXISTING_ARCHIVE_KEEP_ME")

        archived, report = collect_project(
            project, target_dir=str(target), sequences=[])
        assert existing.read_bytes() == b"PREEXISTING_ARCHIVE_KEEP_ME"
        # 冲突策略 = 追加序号改名；新副本可验证
        assert (target / "media" / "source_2.mov").read_bytes() == \
            b"NEW_SOURCE_CONTENT"
        assert report.passed

    def test_case_insensitive_conflict_also_renames(self, tmp_path: Any) -> None:
        project = _project_with_asset(tmp_path)
        target = tmp_path / "ci_target"
        (target / "media").mkdir(parents=True)
        (target / "media" / "SOURCE.MOV").write_bytes(b"case-kept")

        collect_project(project, target_dir=str(target), sequences=[])
        assert (target / "media" / "SOURCE.MOV").read_bytes() == b"case-kept"

    def test_preexisting_dependencies_manifest_kept(self, tmp_path: Any) -> None:
        project = _project_with_asset(tmp_path)
        target = tmp_path / "manifest_target"
        (target / "media").mkdir(parents=True)
        manifest = target / "dependencies.json"
        manifest.write_text("KEEP")

        with patch(
            "src.features.edit.core.project_archive.build_identity_manifest",
            return_value={"marker": "second"},
        ):
            _, report = collect_project(
                project, target_dir=str(target), sequences=[])
        assert manifest.read_text() == "KEEP"
        assert any("dependencies.json" in w for w in report.warnings)

    def test_existing_lut_not_overwritten(self, tmp_path: Any) -> None:
        from src.features.edit.core.project_archive import lut_dependency_paths
        lut = tmp_path / "grade.cube"
        lut.write_bytes(b"LUT-BYTES")
        project = _project_with_asset(tmp_path)
        project = project.with_color_stacks({
            "s1": SimpleNamespace(stack_id="s1", lut_path=str(lut),
                                  layers=())})
        assert lut_dependency_paths(project) == {str(lut)}

        target = tmp_path / "lut_target"
        (target / "media").mkdir(parents=True)
        (target / "luts").mkdir()
        (target / "luts" / "grade.cube").write_bytes(b"PREEXISTING-LUT")

        _, report = collect_project(
            project, target_dir=str(target), sequences=[])
        assert (target / "luts" / "grade.cube").read_bytes() == \
            b"PREEXISTING-LUT"
        assert (target / "luts" / "grade_2.cube").read_bytes() == b"LUT-BYTES"

    def test_failed_copy_removes_only_own_partial(self, tmp_path: Any,
                                                  monkeypatch: Any) -> None:
        """复制中途失败：既有文件不动，本任务半成品即时清理并如实报告。"""
        project = _project_with_asset(tmp_path)
        target = tmp_path / "fail_target"
        (target / "media").mkdir(parents=True)
        existing = target / "media" / "source.mov"
        existing.write_bytes(b"PREEXISTING")

        real_copy2 = __import__("shutil").copy2

        def partial_then_fail(src: Any, dst: Any, **kwargs: Any) -> None:
            with open(dst, "wb") as fh:
                fh.write(b"TRUNCATED-PARTIAL")
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(
            "src.features.edit.core.project_archive.shutil.copy2",
            partial_then_fail)
        _, report = collect_project(
            project, target_dir=str(target), sequences=[])
        monkeypatch.setattr(
            "src.features.edit.core.project_archive.shutil.copy2",
            real_copy2)
        assert existing.read_bytes() == b"PREEXISTING"
        # 目标名唯一分配 → 半成品（source_2.mov）已清理，不留伪完整归档
        assert not (target / "media" / "source_2.mov").exists()
        assert [r.status for r in report.records] == [STATUS_COPY_FAILED]
        assert not report.passed

    def _disk_full(self, monkeypatch: Any, tmp_path: Any) -> None:
        """把目标卷剩余空间伪装成 0（持续空间检查按文件拒绝）。"""
        import src.features.edit.core.project_archive as pa

        monkeypatch.setattr(pa, "_free_bytes", lambda _path: 0)

    def test_insufficient_space_fails_loudly_per_file(
            self, tmp_path: Any, monkeypatch: Any) -> None:
        project = _project_with_asset(tmp_path)
        target = tmp_path / "nospace_target"
        self._disk_full(monkeypatch, tmp_path)

        _, report = collect_project(
            project, target_dir=str(target), sequences=[])
        assert not report.passed
        assert any("空间不足" in w for w in report.warnings)
        # 未写出的目标目录里没有半成品
        assert list((target / "media").iterdir()) == []


class TestR01PanelSavesSequences:
    def _panel(self, qapp: Any, project: EditProject) -> Any:
        pytest.importorskip("PyQt6.QtWidgets")
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel
        panel = MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        panel.set_project(project)
        return panel

    def test_collect_finish_persists_frozen_sequences(
            self, tmp_path: Any, qapp: Any, monkeypatch: Any) -> None:
        from src.features.edit.core.edit_project_io import EditProjectIO
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            Rational,
            TimelineSequence,
        )
        project = _project_with_asset(tmp_path)
        tid = next(iter(project.timelines))
        aid = project.get_asset_by_path(str(tmp_path / "source.mov")).asset_id
        clip = ClipInstance(
            "c", aid, "v", 0, 20, 10, 20,
            _source_fps_num=25, _source_fps_den=1,
            _timeline_fps_num=25, _timeline_fps_den=1)
        sequence = TimelineSequence(
            timeline_id=tid, name="Audit timeline", fps=Rational(25, 1),
            video_tracks=(EditTrack("v", "V", "video", 0, clips=(clip,)),))

        panel = self._panel(qapp, project)
        panel._collect_timeline_sequences = lambda: [sequence]
        queued: list = []
        panel._run_media_management = (
            lambda kind, work, finish, **kw: queued.append((work, finish)))
        target = tmp_path / "archive_out"
        monkeypatch.setattr(
            "src.features.edit.ui.media_pool_panel.QFileDialog."
            "getExistingDirectory", lambda *a, **k: str(target))
        monkeypatch.setattr(
            "src.features.edit.ui.media_pool_panel.QMessageBox", MagicMock())
        panel._on_collect_project()

        archived, report = queued[0][0](lambda *a: None, lambda: False)
        queued[0][1](archived, report)
        saved = target / "Audit.topos"
        assert saved.exists()
        loaded = EditProjectIO().load_project(str(saved))
        assert list((loaded[2] or {}).keys()) == [tid]
        panel.clear_project()
        panel.close()
        panel.deleteLater()

    def test_consolidate_finish_persists_remapped_sequences(
            self, tmp_path: Any, qapp: Any, monkeypatch: Any) -> None:
        from src.features.edit.core.edit_project_io import EditProjectIO
        from src.features.edit.core.timeline_v2 import Rational, TimelineSequence
        project = _project_with_asset(tmp_path, name="Consolidated")
        tid = next(iter(project.timelines))
        remapped = TimelineSequence(
            timeline_id=tid, name="Remapped", fps=Rational(25, 1))
        panel = self._panel(qapp, project)
        target = tmp_path / "consolidate_out"
        target.mkdir()
        report = ConsolidateReport()
        monkeypatch.setattr(
            "src.features.edit.ui.media_pool_panel.QMessageBox", MagicMock())
        panel._finish_consolidate(
            project, report, str(target),
            project_name="Consolidated",
            timeline_sequences={tid: remapped})
        saved = target / "Consolidated.topos"
        assert saved.exists()
        loaded = EditProjectIO().load_project(str(saved))
        assert list((loaded[2] or {}).keys()) == [tid]
        panel.clear_project()
        panel.close()
        panel.deleteLater()

    def test_save_failure_reports_warning_not_success(
            self, tmp_path: Any, qapp: Any, monkeypatch: Any) -> None:
        project = _project_with_asset(tmp_path, name="SaveFail")
        panel = self._panel(qapp, project)
        target = tmp_path / "savefail_out"
        target.mkdir()
        box = MagicMock()
        monkeypatch.setattr(
            "src.features.edit.ui.media_pool_panel.QMessageBox", box)
        monkeypatch.setattr(
            "src.features.edit.core.edit_project_io.EditProjectIO.save_project",
            lambda *a, **k: False)
        report = collect_project(
            project, target_dir=str(target), sequences=[])[1]
        panel._finish_collect(
            project, report, str(target),
            project_name="SaveFail", timeline_sequences={})
        # 保存失败 → warning 级弹窗（不是 information 级"完成"）
        assert box.warning.called
        assert not box.information.called
        assert any("保存失败" in w for w in report.warnings)
        panel.clear_project()
        panel.close()
        panel.deleteLater()

    def test_topos_path_avoids_existing_files(self, tmp_path: Any,
                                              qapp: Any) -> None:
        project = _project_with_asset(tmp_path, name="Dup")
        panel = self._panel(qapp, project)
        target = tmp_path / "dup_out"
        target.mkdir()
        (target / "Dup.topos").write_bytes(b"existing")
        path = panel._unique_archive_topos_path(str(target), "Dup")
        assert path.endswith("Dup_2.topos")
        panel.clear_project()
        panel.close()
        panel.deleteLater()


class TestR04FrozenSnapshotAndGuards:
    def _panel(self, qapp: Any, project: EditProject) -> Any:
        pytest.importorskip("PyQt6.QtWidgets")
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel
        panel = MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        panel.set_project(project)
        return panel

    def test_queued_work_uses_initiating_project(
            self, tmp_path: Any, qapp: Any, monkeypatch: Any) -> None:
        project = _project_with_asset(tmp_path)
        tid = next(iter(project.timelines))
        panel = self._panel(qapp, project)
        sequence = SimpleNamespace(timeline_id=tid)
        panel._collect_timeline_sequences = lambda: [sequence]
        queued: list = []
        panel._run_media_management = (
            lambda kind, work, finish, **kw: queued.append((work, finish)))
        monkeypatch.setattr(
            "src.features.edit.ui.media_pool_panel.QFileDialog."
            "getExistingDirectory", lambda *a, **k: str(tmp_path / "out"))
        monkeypatch.setattr(
            "src.features.edit.ui.media_pool_panel.QMessageBox", MagicMock())
        panel._on_collect_project()

        replacement = EditProject.create("Changed project")
        panel.set_project(replacement)      # 排队期间切工程
        seen: list = []
        with patch(
            "src.features.edit.ui.media_pool_panel.collect_project",
            side_effect=lambda actual, **kw: (
                seen.append((actual.project_id,
                             kw["sequences"][0].timeline_id)),
                (actual, None))[1],
        ):
            queued[0][0](lambda *a: None, lambda: False)
        assert seen and seen[0][0] == project.project_id   # 发起时工程
        assert seen[0][1] == tid                            # 同批序列
        panel.clear_project()
        panel.close()
        panel.deleteLater()

    def test_project_switch_invalidates_running_task(
            self, tmp_path: Any, qapp: Any) -> None:
        project = _project_with_asset(tmp_path)
        panel = self._panel(qapp, project)
        worker = MagicMock()
        panel._media_mgmt_state = {
            "worker": worker, "dialog": None, "done": False,
            "generation": panel._media_mgmt_session_generation,
            "project_id": project.project_id,
        }
        replacement = EditProject.create("Other")
        panel.set_project(replacement)
        assert panel._media_mgmt_session_generation == 1
        worker.cancel.assert_called_once()
        panel.clear_project()
        assert panel._media_mgmt_session_generation == 2
        panel.close()
        panel.deleteLater()

    def test_estimate_includes_luts_and_slack(self, tmp_path: Any) -> None:
        from src.features.edit.core.media_management_worker import (
            estimate_collect_bytes,
        )
        source = tmp_path / "media.mov"
        source.write_bytes(b"m" * 1000)
        lut = tmp_path / "grade.cube"
        lut.write_bytes(b"l" * 200)
        project = EditProject.create("Estimate").import_asset(
            str(source), "video", {})
        project = project.with_color_stacks({
            "s": SimpleNamespace(stack_id="s", lut_path=str(lut),
                                 layers=())})
        assert estimate_collect_bytes(project) > 1200
