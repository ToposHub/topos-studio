"""P0.6 接线层测试：job_cards_provider 装配 + AnalysisService Job → snapshot.jobs。

> 验证 §10.2 P0.6 在装配侧的契约：
> - ``_job_cards_from_registry`` 把 JobRegistry 的 JobHandle 转成 JobCard
> - AnalysisService 有 Job 提交后，经 provider 注入，snapshot.jobs 非空
> - Job 状态变化 → snapshot.jobs 实时刷新
"""
from __future__ import annotations

import pytest

from src.features.edit.ai_editing.jobs import JobKind, JobRegistry, JobStatus
from src.features.edit.ai_editing.assistant_events import JobCard

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# _job_cards_from_registry 转换契约
# ---------------------------------------------------------------------- #


def test_job_cards_from_empty_registry():
    """空 JobRegistry → 空 JobCard 元组。"""
    from src.app.controllers.ai_editing_mixin import _job_cards_from_registry

    registry = JobRegistry()
    cards = _job_cards_from_registry(registry)
    assert cards == ()


def test_job_cards_from_registry_maps_all_fields():
    """JobHandle 的 job_id/kind/status/progress/label 全部映射到 JobCard。"""
    from src.app.controllers.ai_editing_mixin import _job_cards_from_registry

    registry = JobRegistry()
    handle = registry.submit(JobKind.ANALYSIS, label="ASR 转录 a1")
    handle.mark_running()
    handle.update_progress(0.4)

    cards = _job_cards_from_registry(registry)
    assert len(cards) == 1
    card = cards[0]
    assert isinstance(card, JobCard)
    assert card.job_id == handle.job_id
    assert card.kind == "analysis"
    assert card.status == "running"
    assert card.progress == 0.4
    assert card.label == "ASR 转录 a1"


def test_job_cards_reflect_status_changes():
    """Job 状态变化后重新读取 provider，得到新状态。"""
    from src.app.controllers.ai_editing_mixin import _job_cards_from_registry

    registry = JobRegistry()
    handle = registry.submit(JobKind.GENERATION, label="TTS")
    handle.mark_running()

    cards1 = _job_cards_from_registry(registry)
    assert cards1[0].status == "running"

    handle.complete({"path": "/tmp/x.wav"})
    cards2 = _job_cards_from_registry(registry)
    assert cards2[0].status == "succeeded"
    assert cards2[0].progress == 1.0


def test_job_cards_handles_none_registry_gracefully():
    """registry 为 None 或异常时不抛（保护 snapshot 构造）。"""
    from src.app.controllers.ai_editing_mixin import _job_cards_from_registry

    assert _job_cards_from_registry(None) == ()


# ---------------------------------------------------------------------- #
# AssistantService snapshot.jobs 经 provider 实时刷新
# ---------------------------------------------------------------------- #


def test_snapshot_jobs_refresh_via_provider():
    """service.set_job_cards_provider 注入后，snapshot.jobs 随 registry 刷新。"""
    from unittest.mock import MagicMock
    from src.features.edit.ai_editing.assistant_service import AssistantService
    from src.app.controllers.ai_editing_mixin import _job_cards_from_registry

    fake_session = MagicMock()
    fake_session.get_snapshot.return_value = MagicMock(
        active_timeline=None, project_id="p1", assets={},
    )
    service = AssistantService(fake_session)

    registry = JobRegistry()
    service.set_job_cards_provider(
        lambda: _job_cards_from_registry(registry)
    )

    captured = []
    service.on_state_changed = lambda snap: captured.append(snap)
    service._emit_state()
    assert captured[-1].jobs == ()

    # 提交一个 Job 后再 emit，snapshot.jobs 应出现
    registry.submit(JobKind.ANALYSIS, label="ASR")
    service._emit_state()
    assert len(captured[-1].jobs) == 1
    assert captured[-1].jobs[0].label == "ASR"
