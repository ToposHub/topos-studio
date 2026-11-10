"""P0-8 AI 功能生命周期——per-asset 取消验收（补齐 M5 留白）。

对应《媒体池后端交接计划》P0 第 8 项。既有覆盖（复用并核验）：
- tests/app/test_media_analysis_controller.py：三步流/未确认拒绝执行/
  工程切换 cancel_all/默认导入零推理/不写 timeline；
- tests/edit/test_media_pool_analysis_ui.py：面板只发意图信号、无 commit
  路径、默认关闭；
- tests/edit/test_media_pool_resolve_parity.py：AI 范围隔离（全范围搜索
  不扩大分析范围）。
本套件验收本轮补齐的 per-asset 定向取消（此前只能 cancel_all）：
scheduler 按标签取消 → controller 暴露 per-asset API → edit_page 路由。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = [pytest.mark.app, pytest.mark.ai_edit]

from src.app.controllers.media_analysis_controller import (
    MediaAnalysisController,
)
from src.features.edit.ai_editing.analysis_scheduler import (
    AnalysisJobScheduler,
    ResourceLane,
)
from src.features.edit.ai_editing.jobs import JobHandle, JobKind


def _pending_analysis_job(label: str) -> JobHandle:
    return JobHandle(kind=JobKind.ANALYSIS, label=label)


def test_scheduler_cancel_jobs_by_tags_only_targets_tagged(qapp) -> None:
    """标签取消只命中打标 job；未打标/他标 job 不受影响。"""
    scheduler = AnalysisJobScheduler(asynchronous=False)
    try:
        # 播放中 → ANALYSIS lane 不出队，job 停在 pending（可精确取消）
        scheduler.set_playback_active(True)
        j1 = _pending_analysis_job("a1")
        j2 = _pending_analysis_job("a2")
        j3 = _pending_analysis_job("untagged")
        scheduler.submit(ResourceLane.ANALYSIS_LOCAL, j1, work=lambda: None,
                         tags=("asset-1",))
        scheduler.submit(ResourceLane.ANALYSIS_LOCAL, j2, work=lambda: None,
                         tags=("asset-2",))
        scheduler.submit(ResourceLane.ANALYSIS_LOCAL, j3, work=lambda: None)

        cancelled = scheduler.cancel_jobs_by_tags(("asset-1",))
        assert cancelled == 1
        assert not j1.status.is_active
        assert j2.status.is_active
        assert j3.status.is_active
        # 空标签集合是 no-op
        assert scheduler.cancel_jobs_by_tags(()) == 0
    finally:
        scheduler.shutdown(wait=True)


def test_controller_cancel_asset_analysis_targets_single_asset(qapp) -> None:
    """controller.cancel_asset_analysis：只取消该素材的分析 job。"""
    from src.features.edit.ai_editing.analysis_service import AnalysisService

    class _FakeProject:
        project_id = "p1"
        revision = 3

    scheduler = AnalysisJobScheduler(asynchronous=False)
    controller = MediaAnalysisController(
        analysis_service=AnalysisService(),
        scheduler=scheduler,
        project_getter=lambda: _FakeProject(),
    )
    try:
        scheduler.set_playback_active(True)
        job_a = _pending_analysis_job("analyze a1")
        job_b = _pending_analysis_job("analyze b1")
        scheduler.submit(ResourceLane.ANALYSIS_LOCAL, job_a, work=lambda: None,
                         tags=("asset-a",))
        scheduler.submit(ResourceLane.ANALYSIS_LOCAL, job_b, work=lambda: None,
                         tags=("asset-b",))

        assert controller.cancel_asset_analysis("asset-a") == 1
        assert not job_a.status.is_active
        assert job_b.status.is_active  # 同批其它素材不受影响
        assert controller.cancel_asset_analysis("asset-a") == 0  # 无活动分析
    finally:
        scheduler.shutdown(wait=True)


def test_edit_page_cancel_routes_to_per_asset_not_cancel_all(qapp) -> None:
    """edit_page 的取消路由到 per-asset 定向取消（不再误伤全部分析）。"""
    from src.features.edit.ui.edit_page import EditPage

    calls: list[str] = []
    cancel_all_calls: list[int] = []

    class _StubController:
        def cancel_asset_analysis(self, asset_id: str) -> int:
            calls.append(asset_id)
            return 1

        def cancel_all(self) -> int:  # 不应再被走到的旧路径
            cancel_all_calls.append(1)
            return 0

    class _PageStub:
        def _get_media_analysis_controller(self):
            return _StubController()

    EditPage._on_analysis_cancel_requested(_PageStub(), "asset-x")
    assert calls == ["asset-x"]
    assert cancel_all_calls == []


def test_service_submits_jobs_with_asset_tags(qapp) -> None:
    """AnalysisService 走 scheduler 的提交带 asset 标签（源码契约锁定）。"""
    import inspect
    from src.features.edit.ai_editing import analysis_service as svc_mod
    source = inspect.getsource(svc_mod)
    assert "tags=(asset_id,)" in source, (
        "AnalysisService 的 scheduler.submit 必须带 asset 标签，"
        "否则 per-asset 取消退化为 cancel_all")
