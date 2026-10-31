"""WorkflowRunner 阶段事件测试（节点画布诚实状态映射的数据源）。

验收：

- ``emit_stage`` 在真实阶段边界触发（workflow_started / evidence_* /
  planning_started / variants_ready / degraded / validation_failed）。
- 无回调时行为不变（默认 no-op）。
- ``AssistantService.run_workflow`` 把 stage 经 ``emit_node_event`` 的
  payload 透传（kind=progress, node_id="", payload={"stage": ...}）；
  仅接受两参的旧 emitter 也能容忍。
- ``VisualTokens.status_color`` 认识 queued/skipped（此前落回 idle 灰）。

fixture 复用 ``test_workflow_runner.py`` 的 inline 构造器。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.workflow import WorkflowInput, WorkflowId

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from test_workflow_runner import (  # noqa: E402,F401  (同目录 fixture 复用)
    _analysis_service_with,
    _brief_service,
    _runner,
    _shot_only_analysis,
    _transcript_analysis,
    _voiceover_input,
)


@pytest.fixture()
def ready_runner():
    return _runner(analysis=_analysis_service_with("a1", _transcript_analysis()))


@pytest.fixture()
def degraded_runner():
    return _runner(analysis=_analysis_service_with("a1", _shot_only_analysis()))


class TestRunnerStageEvents:
    def test_no_callback_still_runs(self, ready_runner) -> None:
        result = ready_runner.run(_voiceover_input())
        assert result is not None
        assert result.variants  # 证据齐备 → 有 variant

    def test_stages_fire_in_order(self, ready_runner) -> None:
        stages: list[str] = []
        result = ready_runner.run(_voiceover_input(), emit_stage=stages.append)
        assert "workflow_started" in stages
        assert "evidence_ready" in stages
        assert "planning_started" in stages
        assert "variants_ready" in stages
        assert stages.index("evidence_ready") < stages.index("planning_started")
        assert stages.index("planning_started") < stages.index("variants_ready")
        assert result.variants

    def test_evidence_missing_fires(self, degraded_runner) -> None:
        stages: list[str] = []
        result = degraded_runner.run(_voiceover_input(), emit_stage=stages.append)
        assert "evidence_missing" in stages
        assert "degraded" in stages
        assert "planning_started" not in stages
        assert result.degradation

    def test_validation_failure_short_circuits(self, ready_runner) -> None:
        stages: list[str] = []
        ready_runner.run(
            _voiceover_input(asset_ids=()), emit_stage=stages.append,
        )
        assert stages == ["validation_failed"]


class TestServiceStageBridge:
    def _service(self, runner):
        from src.features.edit.ai_editing.assistant_service import AssistantService

        service = AssistantService.__new__(AssistantService)
        service._workflow_runner = runner
        service._messages = []
        service._variants = {}
        service._decisions = {}
        service._plans = None
        service._set_state = lambda *a, **k: None  # type: ignore[method-assign]
        return service

    def test_run_forwards_stage_via_payload(self, ready_runner) -> None:
        service = self._service(ready_runner)
        events: list[tuple[str, str, dict | None]] = []

        def emit_node_event(kind: str, node_id: str, payload: dict | None = None) -> None:
            events.append((kind, node_id, payload))

        service.run_workflow(_voiceover_input(), emit_node_event=emit_node_event)
        stages = [p.get("stage") for _k, _n, p in events if p and "stage" in p]
        assert "workflow_started" in stages
        assert "planning_started" in stages
        assert "variants_ready" in stages

    def test_old_two_arg_emitter_tolerated(self, ready_runner) -> None:
        service = self._service(ready_runner)
        calls: list[tuple[str, str]] = []

        def emit_node_event(kind: str, node_id: str) -> None:
            calls.append((kind, node_id))

        result = service.run_workflow(
            _voiceover_input(), emit_node_event=emit_node_event,
        )
        assert result is not None  # 旧 emitter 不抛 TypeError


class TestStatusColors:
    def test_queued_and_skipped_have_colors(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import (
            DEFAULT_VISUAL_TOKENS, StatusColor,
        )

        tokens = DEFAULT_VISUAL_TOKENS
        assert tokens.status_color("queued") != tokens.status_color("idle")
        assert tokens.status_color("skipped") != tokens.status_color("idle")
        # 全部 plan §6.1 状态词都有对应色（未知回落 idle）。
        for word in (
            "idle", "queued", "running", "cached", "succeeded",
            "failed", "cancelled", "waiting_input", "skipped",
        ):
            assert tokens.status_color(word) == getattr(StatusColor(), word)
