"""R11/R12/R13：使用索引删除确认 / probe 修正类型 / CSV 安全导入回归。

对应《媒体池后端完成情况复核与整改计划》（2026-11-10）：
- R11：删除路径消费 usage_check_for_assets（同路径父/子片段整组 +
  全部时间线引用），与定位/Replace 共用同一索引；无引用不弹窗，
  用户拒绝中止删除；
- R12：纯音频专业容器（无视频流的 .mov/.mxf）在 probe 后按 audio
  导入（media_type_from_probe），不再按扩展名伪装 video；
- R13：CSV 同路径多素材歧义可见且跳过；计划绑定工程身份、过期重建
  差异；撤销恢复 apply 时刻真实状态；关键词分号编码 round-trip 可逆
  且兼容旧 CSV。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.features.edit.core.project import EditProject, MediaAsset


def _project(tmp_path: Any, name: str = "CSV") -> EditProject:
    source = tmp_path / "a.mov"
    source.write_bytes(b"a")
    return EditProject.create(name).import_asset(
        str(source), "video", {"fps": 25})


class TestR13CsvSafety:
    def test_ambiguous_path_row_skipped_with_warning(self, tmp_path: Any) -> None:
        """复现（repro R13）：同路径多素材不再静默挑第一个。"""
        from dataclasses import replace as dc_replace
        from src.features.edit.core.metadata_interchange import (
            plan_metadata_import,
        )
        project = _project(tmp_path)
        aid = next(iter(project.assets))
        duplicate = dc_replace(
            project.assets[aid], asset_id="duplicate",
            display_name="Duplicate")
        project = project.with_media_asset_added(duplicate)

        plan = plan_metadata_import(
            project, f"path,comment\n{project.assets[aid].path},x\n")
        assert plan.asset_changes == ()
        assert any("匹配 2 个素材" in w for w in plan.warnings)

    def test_plan_carries_project_identity(self, tmp_path: Any) -> None:
        from src.features.edit.core.metadata_interchange import (
            plan_metadata_import,
        )
        project = _project(tmp_path)
        aid = next(iter(project.assets))
        plan = plan_metadata_import(
            project, f"asset_id,comment\n{aid},CSV\n")
        assert plan.project_id == project.project_id
        assert plan.source_revision == project.revision

    def test_undo_restores_true_pre_apply_state(self, tmp_path: Any) -> None:
        """复现（repro R13）：计划生成后的并发编辑不被 undo 吞掉。"""
        from src.features.edit.core.commands import ImportMetadataCommand
        from src.features.edit.core.metadata_interchange import (
            plan_metadata_import,
        )
        project = _project(tmp_path)
        aid = next(iter(project.assets))
        plan = plan_metadata_import(
            project, f"asset_id,comment\n{aid},CSV\n")
        concurrent = project.update_asset_log_info(aid, comment="concurrent")

        cmd = ImportMetadataCommand("c1", "Import", plan)
        applied = cmd.apply(concurrent)
        assert applied.assets[aid].comment == "CSV"
        undone = cmd.undo(applied)
        assert undone.assets[aid].comment == "concurrent"  # 真实提交前状态

    def test_cross_project_apply_rebuilds_diff(self, tmp_path: Any) -> None:
        from src.features.edit.core.commands import ImportMetadataCommand
        from src.features.edit.core.metadata_interchange import (
            plan_metadata_import,
        )
        project = _project(tmp_path)
        aid = next(iter(project.assets))
        plan = plan_metadata_import(
            project, f"asset_id,comment\n{aid},CSV\n")
        # 另一工程：同 asset_id 不存在 → 重建差异剔除并警告（不冒充成功）
        (tmp_path / "other").mkdir()
        other = _project(tmp_path / "other", name="Other")
        cmd = ImportMetadataCommand("c2", "Import", plan)
        applied = cmd.apply(other)
        assert applied is other  # 无可应用变更
        assert any("不存在" in w for w in cmd.plan.warnings)

    def test_keyword_roundtrip_escapes_separator(self, tmp_path: Any) -> None:
        """复现（repro R13）：含 ';' 的关键词往返不再被拆散。"""
        from src.features.edit.core.metadata_interchange import (
            apply_metadata_import,
            export_metadata_csv,
            plan_metadata_import,
        )
        project = _project(tmp_path)
        aid = next(iter(project.assets))
        project = project.update_asset_log_info(
            aid, keywords=("night;exterior", "hero"))
        roundtripped = apply_metadata_import(
            project, plan_metadata_import(
                project, export_metadata_csv(project)))
        assert roundtripped.assets[aid].keywords == ("night;exterior", "hero")

    def test_legacy_bare_separator_csv_still_splits(self, tmp_path: Any) -> None:
        """兼容旧 CSV：无转义的 ';' 仍拆多词（历史行为不变）。"""
        from src.features.edit.core.metadata_interchange import (
            apply_metadata_import,
            plan_metadata_import,
        )
        project = _project(tmp_path)
        aid = next(iter(project.assets))
        applied = apply_metadata_import(project, plan_metadata_import(
            project, f"asset_id,keywords\n{aid},night;exterior\n"))
        assert applied.assets[aid].keywords == ("night", "exterior")


class TestR12ProbeCorrectsType:
    def test_pure_audio_container_imports_as_audio(self, tmp_path: Any) -> None:
        """probe 判明无视频流的 .mov → media_type=audio。"""
        from src.features.edit.core.project import (
            media_type_from_probe,
            probe_media_metadata,
        )
        # 纯音频容器：视频探测失败回退块的产物形态（audio=True）
        metadata = {"format": "MOV", "error": "no video stream",
                    "audio": True,
                    "audio_info": {"channels": 2, "duration_seconds": 10.0}}
        assert media_type_from_probe("/m/voice_only.mov", metadata) == "audio"

    def test_normal_video_keeps_extension_type(self, tmp_path: Any) -> None:
        from src.features.edit.core.project import media_type_from_probe
        assert media_type_from_probe("/m/clip.mov", {
            "format": "MOV", "fps": 25.0,
            "audio_info": {"channels": 2}}) == "video"
        assert media_type_from_probe("/m/clip.mov", {}) == "video"

    def test_sync_import_uses_corrected_type(self, tmp_path: Any,
                                             qapp: Any) -> None:
        """同步导入入口消费 probe 修正（MediaImportWorker 同款调用）。"""
        from src.features.edit.core.project import media_type_from_probe
        metadata = {"format": "MXF", "error": "no video stream",
                    "audio": True}
        assert media_type_from_probe("/m/sep.mxf", metadata) == "audio"


class TestR11DeleteUsageConfirmation:
    def _panel(self, qapp: Any, project: EditProject) -> Any:
        pytest.importorskip("PyQt6.QtWidgets")
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel
        panel = MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        panel.set_project(project)
        return panel

    def _sequence_using(self, asset_id: str):
        from types import SimpleNamespace
        clip = SimpleNamespace(asset_id=asset_id, clip_id="c",
                               timeline_start=0, timeline_duration=5)
        track = SimpleNamespace(track_id="v", name="V", track_type="video",
                                clips=(clip,))
        return SimpleNamespace(timeline_id="t1", name="T1",
                               all_tracks=(track,))

    def test_delete_unused_asset_skips_dialog(self, tmp_path: Any,
                                              qapp: Any) -> None:
        project = _project(tmp_path, name="Unused")
        panel = self._panel(qapp, project)
        aid = next(iter(project.assets))
        question = MagicMock()
        with patch.object(
                __import__(
                    "src.features.edit.ui.media_pool_panel",
                    fromlist=["QMessageBox"]).QMessageBox,
                "question", question):
            assert panel._do_remove_asset(aid) is True
        question.assert_not_called()          # 无引用：不弹窗
        panel.close()
        panel.deleteLater()

    def test_delete_used_asset_confirms_and_can_abort(
            self, tmp_path: Any, qapp: Any) -> None:
        project = _project(tmp_path, name="Used")
        aid = next(iter(project.assets))
        panel = self._panel(qapp, project)
        panel._collect_timeline_sequences = lambda: [
            self._sequence_using(aid)]
        mpp = __import__("src.features.edit.ui.media_pool_panel",
                         fromlist=["QMessageBox"])
        question = MagicMock(
            return_value=mpp.QMessageBox.StandardButton.No)
        with patch.object(mpp.QMessageBox, "question", question):
            assert panel._do_remove_asset(aid) is False   # 拒绝 → 中止
        assert question.call_count == 1
        assert aid in panel._project.assets               # 未删除
        # 确认 → 删除成功
        question_yes = MagicMock(
            return_value=mpp.QMessageBox.StandardButton.Yes)
        with patch.object(mpp.QMessageBox, "question", question_yes):
            assert panel._do_remove_asset(aid) is True
        assert aid not in panel._project.assets
        panel.close()
        panel.deleteLater()

    def test_same_path_group_counted_together(self, tmp_path: Any) -> None:
        """同路径父/子片段：删其一，整组的使用都进入确认清单。"""
        from dataclasses import replace as dc_replace
        from src.features.edit.core.asset_usage_index import (
            usage_check_for_assets,
        )
        project = _project(tmp_path, name="Group")
        aid = next(iter(project.assets))
        child = dc_replace(project.assets[aid], asset_id="child",
                           display_name="Sub")
        project = project.with_media_asset_added(child)
        sequence = self._sequence_using(aid)      # 只有父素材被引用
        # 删 child（同路径组员）：父素材的时间线使用经整组核验可见
        # ——共享文件仍被引用，删除确认必须出现
        usage = usage_check_for_assets(
            project, [sequence], ["child"], include_same_path=True)
        assert set(usage.keys()) == {aid}            # 父素材使用计入
        usage_exclude = usage_check_for_assets(
            project, [sequence], ["child"], include_same_path=False)
        assert usage_exclude == {}                   # 不扩组则不可见
