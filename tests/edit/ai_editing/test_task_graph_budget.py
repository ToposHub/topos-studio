"""TaskGraph deadline 与真实/fallback executor 来源可见性测试。"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.llm_role_executor import LlmRoleExecutor
from src.features.edit.ai_editing.skill_runtime import DeterministicRoleExecutor
from src.features.edit.ai_editing.task_graph import (
    ContextSlice,
    TaskBudget,
    TaskGraph,
    TaskGraphExecutor,
    TaskNode,
    TaskRole,
    TaskState,
)


class _SlowExecutor:
    def execute(self, role, context, upstream):
        time.sleep(0.01)
        return DeterministicRoleExecutor().execute(role, context, upstream)


class _OverBudgetExecutor:
    last_usage = {"tokens": 99, "cost_units": 0.0, "tool_calls": 9}

    def execute(self, role, context, upstream):
        return DeterministicRoleExecutor().execute(role, context, upstream)


def test_deadline_exceeded_is_needs_user_input() -> None:
    graph = TaskGraph(skill_id="deadline", nodes=(TaskNode(
        task_id="qa", role=TaskRole.QA_REVIEWER,
        budget=TaskBudget(timeout_ms=1),
    ),))
    result = TaskGraphExecutor(role_executor=_SlowExecutor()).execute(graph)[0]
    assert result.state == TaskState.NEEDS_USER_INPUT
    assert "deadline" in result.error_reason
    assert result.artifact is None


def test_llm_failure_reports_deterministic_fallback_source() -> None:
    graph = TaskGraph(skill_id="fallback", nodes=(TaskNode(
        task_id="creative", role=TaskRole.CREATIVE_DIRECTOR,
        context=ContextSlice(asset_ids=("a1",)),
    ),))
    result = TaskGraphExecutor(
        role_executor=LlmRoleExecutor(model_client=None, skill_id="fallback"),
    ).execute(graph)[0]
    assert result.state == TaskState.SUCCEEDED
    assert result.execution_source == "deterministic_fallback"
    assert result.degradation_reason == "no_model_client"


def test_executor_usage_budget_is_visible_and_blocks_dependents() -> None:
    graph = TaskGraph(skill_id="usage", nodes=(
        TaskNode(
            task_id="creative", role=TaskRole.CREATIVE_DIRECTOR,
            budget=TaskBudget(max_tokens=10, max_tool_calls=2),
        ),
        TaskNode(
            task_id="qa", role=TaskRole.QA_REVIEWER,
            depends_on=("creative",),
        ),
    ))
    results = TaskGraphExecutor(role_executor=_OverBudgetExecutor()).execute(graph)
    assert results[0].state == TaskState.NEEDS_USER_INPUT
    assert "max_tokens" in results[0].error_reason
    assert results[1].state == TaskState.CANCELLED
