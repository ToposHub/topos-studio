"""R10/R14/R15：查询过期保护与 Smart Bin 异步 / Smart Bin 编辑器 / 转录协议链。

对应《媒体池后端完成情况复核与整改计划》（2026-11-10）可落地核心：
- R10：Smart Bin 视图进入统一异步查询（scope=smart_bin，查询线程
  求值规则）；指纹变化即清空上一轮结果（加载期旧项不可作为新范围
  操作目标）；结果接受面板侧 revision 复验；
- R14：Smart Bin 创建/编辑共用能力分组表单（媒体类型/关键词/评级/
  旗标/颜色/注释/技术属性），编辑经 UpdateSmartBinRulesCommand 单步
  撤销；规则树 Smart Bin 只允许改名（不静默降级）；
- R15：请求级转录选项（协议键）经 TranscriptionJobRequest fail-loud
  校验后作为 provider options 下发（语言/帧域范围折算秒域/说话人/
  隐私策略）——UI→provider 的转录设置不再有旁路。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.features.edit.core.project import EditProject, SmartBin


def _project(tmp_path: Any, name: str = "Q") -> EditProject:
    source = tmp_path / "clip.mov"
    source.write_bytes(b"c")
    return EditProject.create(name).import_asset(
        str(source), "video", {"fps": 25.0, "total_frames": 100})


class TestR10QueryStalenessAndSmartBin:
    def test_smart_bin_scope_in_query_engine(self, tmp_path: Any) -> None:
        from src.features.edit.core.media_pool_query import (
            MediaPoolQuery,
            execute_media_pool_query,
        )
        project = _project(tmp_path)
        aid = next(iter(project.assets))
        smart = SmartBin(
            bin_id="sb-x", name="Rated", filter_type="custom",
            filter_params={"min_rating": 3})
        project = project.add_smart_bin(smart)
        project = project.update_asset_log_info(aid, rating=4)

        result = execute_media_pool_query(project, MediaPoolQuery(
            scope="smart_bin", bin_id="sb-x", sort_key="name"))
        assert result.asset_ids == (aid,)
        assert not result.cancelled

    def test_panel_query_active_includes_smart_bins(
            self, tmp_path: Any, qapp: Any, monkeypatch: Any) -> None:
        pytest.importorskip("PyQt6.QtWidgets")
        from src.features.edit.ui import media_pool_panel as mpp
        monkeypatch.setattr(mpp, "MEDIA_POOL_ASYNC_QUERY_THRESHOLD", 1)
        panel = mpp.MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        panel.set_project(_project(tmp_path))
        panel._current_bin_is_smart = True
        panel._current_bin_id = "sb-x"
        assert panel._large_pool_query_active() is True
        panel.close()
        panel.deleteLater()

    def test_fingerprint_change_clears_stale_ids(
            self, tmp_path: Any, qapp: Any, monkeypatch: Any) -> None:
        pytest.importorskip("PyQt6.QtWidgets")
        from src.features.edit.ui import media_pool_panel as mpp
        monkeypatch.setattr(mpp, "MEDIA_POOL_ASYNC_QUERY_THRESHOLD", 1)
        panel = mpp.MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        project = _project(tmp_path)
        panel.set_project(project)
        panel._current_bin_is_smart = True
        panel._current_bin_id = "sb-x"

        submitted: list = []

        class FakeSession:
            def submit(self, project: Any, query: Any) -> None:
                submitted.append(query)

            def shutdown(self) -> None:
                pass

        panel._query_session = FakeSession()
        panel._query_asset_ids = ("old", "ids")           # 上一轮结果
        panel._last_query_fingerprint = ("stale",)
        panel._ensure_pool_query()
        # 指纹变化：旧结果立即丢弃（加载期素材区为空）+ 新查询提交
        assert panel._query_asset_ids is None
        assert submitted and submitted[-1].scope == "smart_bin"
        panel.close()
        panel.deleteLater()

    def test_result_with_stale_revision_dropped(
            self, tmp_path: Any, qapp: Any) -> None:
        pytest.importorskip("PyQt6.QtWidgets")
        from src.features.edit.ui import media_pool_panel as mpp
        panel = mpp.MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        project = _project(tmp_path)
        panel.set_project(project)
        result = type("R", (), {
            "project_id": project.project_id,
            "revision": project.revision + 5,      # 排队送达期间工程前进
            "asset_ids": ("a",),
            "cancelled": False,
        })()
        panel._query_asset_ids = ("previous",)
        refresh = MagicMock()
        panel._delayed_refresh = refresh
        panel._on_pool_query_results(result)
        assert panel._query_asset_ids == ("previous",)   # 晚到结果不覆盖
        refresh.assert_not_called()
        panel.close()
        panel.deleteLater()


class TestR14SmartBinEditor:
    def _panel(self, qapp: Any, project: EditProject) -> Any:
        pytest.importorskip("PyQt6.QtWidgets")
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel
        panel = MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        panel.set_project(project)
        return panel

    def test_edit_round_trips_capability_fields(
            self, tmp_path: Any, qapp: Any, monkeypatch: Any) -> None:
        """预填的能力字段经表单编辑往返保留（UpdateSmartBinRulesCommand）。"""
        from PyQt6.QtWidgets import QDialog
        from src.features.edit.core.commands import (
            ProjectCommandStack,
            UpdateSmartBinRulesCommand,
        )
        project = _project(tmp_path)
        params = {
            "media_type": "video", "keywords": ["interview"],
            "min_rating": 3, "flag": "reject", "color_label": "red",
            "comment_contains": "keep", "min_width": 1920, "min_fps": 23.976,
        }
        smart = SmartBin(bin_id="sb-edit", name="Interviews",
                         filter_type="custom", filter_params=dict(params))
        project = project.add_smart_bin(smart)

        panel = self._panel(qapp, project)
        monkeypatch.setattr(
            QDialog, "exec",
            lambda self: QDialog.DialogCode.Accepted.value)
        edited = panel._smart_bin_dialog(project.smart_bins["sb-edit"])
        assert edited is not None
        # 预填值全部往返（表单读写每个能力字段）
        for key, value in params.items():
            assert edited.filter_params.get(key) == value

        stack = ProjectCommandStack()
        project2 = stack.execute(
            UpdateSmartBinRulesCommand.create(edited), project)
        assert project2.smart_bins["sb-edit"].filter_params == \
            edited.filter_params
        undone = stack.undo(project2)
        assert undone.smart_bins["sb-edit"].filter_params == params
        panel.close()
        panel.deleteLater()

    def test_rule_tree_bin_edit_keeps_rules(self, tmp_path: Any, qapp: Any,
                                            monkeypatch: Any) -> None:
        """v2 规则树 Smart Bin：只改名，规则原样保留（不静默降级）。"""
        from PyQt6.QtWidgets import QDialog
        from src.features.edit.core.smart_bin_rules import has_rule_tree
        project = _project(tmp_path)
        rules = {"and": [
            {"field": "rating", "cmp": ">=", "value": 3},
            {"field": "keywords", "cmp": "in", "value": ["interview"]},
        ]}
        smart = SmartBin(bin_id="sb-tree", name="Tree",
                         filter_type="custom",
                         filter_params={"rules": rules})
        project = project.add_smart_bin(smart)

        panel = self._panel(qapp, project)
        monkeypatch.setattr(
            QDialog, "exec",
            lambda self: QDialog.DialogCode.Accepted.value)
        edited = panel._smart_bin_dialog(project.smart_bins["sb-tree"])
        assert edited is not None
        assert edited.name == "Tree"
        assert has_rule_tree(edited.filter_params)
        assert edited.filter_params["rules"] == rules   # 树原样保留
        panel.close()
        panel.deleteLater()


class TestR15TranscriptionProtocolChain:
    def test_options_normalized_through_protocol(self) -> None:
        from src.features.edit.ai_editing.transcription_job import (
            transcription_options_from_request,
        )
        out = transcription_options_from_request(
            "a1", "p1", 7,
            {"language": "zh", "time_range_frames": (10, 49),
             "speaker_diarization": True, "data_policy": "local_only",
             "custom_key": "kept"},
            fps=25.0)
        assert out["language"] == "zh"
        assert out["time_range_seconds"] == (10 / 25.0, 50 / 25.0)  # 含尾→半开
        assert out["speaker_diarization"] is True
        assert out["data_policy"] == "local_only"
        assert out["custom_key"] == "kept"                 # 未识别键透传
        assert out["transcription_request_id"]

    def test_invalid_options_fail_loud(self) -> None:
        from src.features.edit.ai_editing.transcription_job import (
            TranscriptionJobError,
            transcription_options_from_request,
        )
        with pytest.raises(TranscriptionJobError):   # 倒序范围
            transcription_options_from_request(
                "a", "p", 0, {"time_range_frames": (50, 10)}, fps=25.0)
        with pytest.raises(TranscriptionJobError):   # 帧域范围需要 fps
            transcription_options_from_request(
                "a", "p", 0, {"time_range_frames": (0, 10)}, fps=None)
        with pytest.raises(TranscriptionJobError):   # 未知隐私策略
            transcription_options_from_request(
                "a", "p", 0, {"data_policy": "cloud"}, fps=25.0)

    def test_scheduler_passes_normalized_options_to_provider(self) -> None:
        """请求 options → 协议校验 → provider options（执行链贯通）。"""
        from src.features.edit.ai_editing.analysis_request import (
            AnalysisRecipe,
            AnalysisRequest,
            RequestState,
        )
        from src.features.edit.ai_editing.analysis_scheduler import ResourceLane
        from src.features.edit.ai_editing.analysis_service import AnalysisService
        from src.features.edit.ai_editing.jobs import JobKind

        class Scheduler:
            def __init__(self) -> None:
                self.items = []

            def submit(self, lane: Any, job: Any, **kwargs: Any) -> None:
                self.items.append((job, kwargs))

            def notify_finished(self, job_id: str) -> None:
                pass

        request = AnalysisRequest.create(
            project_id="p1", asset_ids=("a1",),
            recipe_id=AnalysisRecipe.FAST_LOCAL,
            project_revision=7,
            options={"language": "zh", "time_range_frames": (0, 24),
                     "speaker_diarization": True})
        request = request.with_state(RequestState.RUNNING)
        service = AnalysisService()
        service._requests[request.request_id] = request
        scheduler = Scheduler()
        service._execute_via_scheduler(
            request.request_id, request, scheduler=scheduler,
            lane=ResourceLane.ANALYSIS_LOCAL, fps="25/1",
            provider_ids=(), on_asset_done=None, on_request_done=None)

        captured: dict = {}

        def provider(asset_id: str, fingerprint: Any, **kwargs: Any):
            captured.update(kwargs.get("options") or {})
            job = service.jobs.submit(JobKind.ANALYSIS, label="t")
            job.mark_running()
            job.complete({})
            return job, ""

        service.request_analysis = provider
        scheduler.items[0][1]["work"]()
        assert captured["language"] == "zh"
        assert captured["time_range_seconds"] == (0.0, 1.0)   # 25fps × 25 帧
        assert captured["speaker_diarization"] is True
        assert captured["transcription_request_id"]

    def test_invalid_options_fail_the_asset_not_silently(self) -> None:
        from src.features.edit.ai_editing.analysis_request import (
            AnalysisRecipe,
            AnalysisRequest,
            RequestState,
        )
        from src.features.edit.ai_editing.analysis_scheduler import ResourceLane
        from src.features.edit.ai_editing.analysis_service import AnalysisService

        class Scheduler:
            def __init__(self) -> None:
                self.items = []

            def submit(self, lane: Any, job: Any, **kwargs: Any) -> None:
                self.items.append((job, kwargs))

            def notify_finished(self, job_id: str) -> None:
                pass

        request = AnalysisRequest.create(
            project_id="p1", asset_ids=("a1",),
            recipe_id=AnalysisRecipe.FAST_LOCAL,
            options={"time_range_frames": (50, 10)})   # 倒序范围
        request = request.with_state(RequestState.RUNNING)
        service = AnalysisService()
        service._requests[request.request_id] = request
        scheduler = Scheduler()
        service._execute_via_scheduler(
            request.request_id, request, scheduler=scheduler,
            lane=ResourceLane.ANALYSIS_LOCAL, fps="25/1",
            provider_ids=(), on_asset_done=None, on_request_done=None)
        provider_called = []
        service.request_analysis = (
            lambda *a, **k: provider_called.append(1) or (None, "boom"))
        scheduler.items[0][1]["work"]()
        assert not provider_called          # 非法选项在 provider 前拦截
        from src.features.edit.ai_editing.analysis_request import RequestState
        assert service.get_request(request.request_id).state \
            == RequestState.FAILED          # 按素材失败上报
