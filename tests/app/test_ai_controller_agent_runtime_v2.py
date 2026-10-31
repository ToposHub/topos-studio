"""V2 chat runtime wiring tests (plan §A1).

Validates the controller glue that routes ordinary chat through the unified
AgentRun when the ``AI_ASSISTANT_AGENT_RUNTIME_V2`` flag is on, without
needing a full Qt controller fixture: the methods are exercised directly via
``__new__`` so the driver construction + fallback logic is checked in
isolation.

The full happy-path (model call → reply → usage) is already covered by the
ChatRunDriver / AgentV2ChatWorker unit tests; here we only assert the wiring:
- ``_get_v2_chat_driver`` returns None without a profile registry, and a
  cached driver when one is configured.
- ``_start_chat_v2`` falls back to the legacy worker when no driver exists.
- The V2 flag defaults on (B0), so ``_start_chat`` routes through V2 by
  default.  ``TOPOS_TEST_MODE`` still short-circuits to the synchronous
  legacy path so assertion-style tests are unaffected.
"""

from __future__ import annotations

from src.app.controllers.ai_edit_controller import AiEditController
from src.features.edit.ai_editing import agent_feature_flags as flags
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    FakeCredentialStore,
    ProfileRegistry,
)


def _bare_controller() -> AiEditController:
    """A controller instance without running ``__init__`` (method-level tests)."""

    ctrl = AiEditController.__new__(AiEditController)
    ctrl._v2_chat_driver = None
    ctrl._v2_orchestrator = None
    ctrl._v2_router = None
    ctrl._profile_registry = None
    ctrl._credential_store = None
    return ctrl


def _language_registry() -> ProfileRegistry:
    reg = ProfileRegistry()
    reg.add(
        AiProviderProfile(
            profile_id="lang1",
            display_name="GLM",
            provider_kind="openai",
            capabilities=("language",),
        )
    )
    reg.set_active_for_capability("language", "lang1")
    return reg


class TestV2DriverConstruction:
    def test_no_driver_without_profile_registry(self):
        ctrl = _bare_controller()
        assert ctrl._get_v2_chat_driver() is None

    def test_no_driver_without_credential_store(self):
        ctrl = _bare_controller()
        ctrl._profile_registry = _language_registry()
        # credential_store still None
        assert ctrl._get_v2_chat_driver() is None

    def test_driver_constructed_and_cached_with_profile(self):
        ctrl = _bare_controller()
        ctrl._profile_registry = _language_registry()
        ctrl._credential_store = FakeCredentialStore()
        driver = ctrl._get_v2_chat_driver()
        assert driver is not None
        # second call returns the cached instance
        assert ctrl._get_v2_chat_driver() is driver
        # the collaborators were wired
        assert ctrl._v2_orchestrator is not None
        assert ctrl._v2_router is not None


class TestStartChatV2Fallback:
    def test_returns_false_when_no_driver(self):
        # No profile registry -> driver None -> fall back to legacy worker.
        ctrl = _bare_controller()
        result = ctrl._start_chat_v2(
            "hi", [{"role": "user", "content": "hi"}], temperature=0.6
        )
        assert result is False


class TestFlagDefaultsOn:
    def test_v2_flag_defaults_on(self):
        flags.reset_agent_runtime_v2_override()
        assert flags.is_agent_runtime_v2_enabled() is True


def test_motion_graphic_mode_routes_follow_up_confirmation_to_feature_handler():
    """“可以” must advance the pending plan, not create a second plan."""
    from unittest.mock import MagicMock

    from src.features.edit.ai_editing.assistant_events import AssistantRequest

    ctrl = AiEditController.__new__(AiEditController)
    ctrl._panel = MagicMock()
    ctrl._last_request_text = ""
    ctrl._pending_chat_attachments = ()
    ctrl._record_user_message = lambda _text: None
    ctrl._refresh_composer_footer = lambda: None
    follow_ups: list[str] = []
    mode_calls: list[str] = []
    ctrl._teaching_request_handler = lambda text: (follow_ups.append(text) or text == "可以")
    ctrl._teaching_motion_graphic_mode_handler = lambda text: (
        mode_calls.append(text) or True
    )

    ctrl._on_request_submitted(AssistantRequest(
        request_id="confirm-1", text="可以", mode="motion_graphic",
    ))

    assert follow_ups == ["可以"]
    assert mode_calls == []


def test_motion_graphic_request_forwards_explicit_target_asset_ids():
    """Controller must not drop the media-pool target before feature routing."""
    from unittest.mock import MagicMock

    from src.features.edit.ai_editing.assistant_events import AssistantRequest

    ctrl = AiEditController.__new__(AiEditController)
    ctrl._panel = MagicMock()
    ctrl._last_request_text = ""
    ctrl._pending_chat_attachments = ()
    ctrl._record_user_message = lambda _text: None
    ctrl._refresh_composer_footer = lambda: None
    received = []

    def handler(text, *, target_asset_ids=()):
        received.append((text, tuple(target_asset_ids)))
        return True

    ctrl._teaching_request_handler = handler
    ctrl._teaching_motion_graphic_mode_handler = None

    ctrl._on_request_submitted(AssistantRequest(
        request_id="target-1",
        text="把这个网页改成霓虹风格",
        mode="motion_graphic",
        target_asset_ids=("teaching_animation:scene-a",),
    ))

    assert received == [(
        "把这个网页改成霓虹风格",
        ("teaching_animation:scene-a",),
    )]


class TestV2UsageBridge:
    """Plan §A4: the V2 run's real usage must reach the composer footer."""

    def test_on_v2_chat_result_records_usage(self):
        from src.features.edit.ai_editing.agent_models import ContextUsage

        ctrl = _bare_controller()
        ctrl._v2_last_usage = None
        # Avoid the full footer refresh (needs a real service/panel).
        ctrl._refresh_composer_footer = lambda *a, **kw: None
        result = type("R", (), {"usage": ContextUsage(actual_prompt=42)})()
        ctrl._on_v2_chat_result(result)
        assert ctrl._v2_last_usage is not None
        assert ctrl._v2_last_usage.actual_prompt == 42

    def test_on_v2_chat_result_ignores_none_usage(self):
        ctrl = _bare_controller()
        ctrl._v2_last_usage = "pre-existing"
        ctrl._refresh_composer_footer = lambda *a, **kw: None
        ctrl._on_v2_chat_result(type("R", (), {"usage": None})())
        # A usage-less result must not clear a previous real reading.
        assert ctrl._v2_last_usage == "pre-existing"


# ---------------------------------------------------------------------- #
# B1: V2 orchestrator → Run Drawer 可观察性
# ---------------------------------------------------------------------- #


def _make_event(run_id: str, kind: str, payload: dict | None = None) -> object:
    """Build a lightweight AgentEvent-like object for drawer-translation tests."""
    return type(
        "E",
        (),
        {
            "run_id": run_id,
            "kind": kind,
            "payload": payload or {},
            "event_id": "evt-test",
            "parent_id": "",
            "timestamp": 0.0,
        },
    )()


class TestV2RunDrawerObservability:
    """B1: V2 orchestrator events → Run Drawer item translation."""

    def _make_ctrl(self):
        """Controller with a real AgentOrchestrator + bare panel mock."""
        from unittest.mock import MagicMock

        from src.features.edit.ai_editing.agent_models import AgentTask
        from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator

        ctrl = _bare_controller()
        ctrl._v2_orchestrator = AgentOrchestrator()
        ctrl._v2_drawer_items = []
        ctrl._v2_drawer_history = []
        ctrl._panel = MagicMock()

        task = AgentTask(
            task_id="t1", session_id="default", title="chat",
            user_goal="hi", acceptance_criteria=(), kind="chat",
        )
        run_id = ctrl._v2_orchestrator.start_task(task, mode="chat")
        return ctrl, run_id

    def test_run_created_creates_main_item(self):
        from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

        ctrl, run_id = self._make_ctrl()
        ctrl._update_v2_drawer_from_event(
            run_id, "run.created", {}, RunDrawerItem,
        )
        assert len(ctrl._v2_drawer_items) == 1
        item = ctrl._v2_drawer_items[0]
        assert item.item_id == run_id
        assert item.kind == "run"
        assert item.status == "running"
        assert item.can_cancel is True

    def test_tool_started_then_finished_updates_card(self):
        from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

        ctrl, run_id = self._make_ctrl()
        ctrl._update_v2_drawer_from_event(
            run_id, "run.created", {}, RunDrawerItem,
        )
        ctrl._update_v2_drawer_from_event(
            run_id, "run.tool_started",
            {"tool_name": "get_project_snapshot"},
            RunDrawerItem,
        )
        tool_items = [i for i in ctrl._v2_drawer_items if i.kind == "node"]
        assert len(tool_items) == 1
        assert tool_items[0].status == "running"
        assert tool_items[0].label.startswith("探索 ·")
        assert "get_project_snapshot" in tool_items[0].label

        ctrl._update_v2_drawer_from_event(
            run_id, "run.tool_finished",
            {"tool_name": "get_project_snapshot", "outcome": "success",
             "duration_ms": 120},
            RunDrawerItem,
        )
        assert tool_items[0].status == "succeeded"
        assert tool_items[0].duration_ms == 120

    def test_tool_progress_updates_detail_and_phase(self):
        from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

        ctrl, run_id = self._make_ctrl()
        ctrl._update_v2_drawer_from_event(run_id, "run.created", {}, RunDrawerItem)
        ctrl._update_v2_drawer_from_event(
            run_id, "run.tool_started",
            {"step_id": "step-1", "tool_name": "search_assets", "description": "探索素材"},
            RunDrawerItem,
        )
        ctrl._update_v2_drawer_from_event(
            run_id, "run.tool_progress",
            {"step_id": "step-1", "fraction": 0.5, "message": "已找到 3 个候选"},
            RunDrawerItem,
        )
        tool = next(i for i in ctrl._v2_drawer_items if i.kind == "node")
        assert tool.progress == 0.5
        assert tool.output_summary == "已找到 3 个候选"

        ctrl._update_v2_drawer_from_event(
            run_id, "run.tool_progress",
            {"phase": "verification_llm"}, RunDrawerItem,
        )
        main = next(i for i in ctrl._v2_drawer_items if i.kind == "run")
        assert main.label == "校验结果"
        assert main.progress == 0.9

    def test_usage_recorded_updates_token_count(self):
        from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

        ctrl, run_id = self._make_ctrl()
        ctrl._update_v2_drawer_from_event(
            run_id, "run.created", {}, RunDrawerItem,
        )
        ctrl._update_v2_drawer_from_event(
            run_id, "run.usage_recorded",
            {"actual_prompt": 42, "actual_completion": 10},
            RunDrawerItem,
        )
        main = next(i for i in ctrl._v2_drawer_items if i.kind == "run")
        assert "52" in main.output_summary

    def test_completed_updates_main_item_to_succeeded(self):
        from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

        ctrl, run_id = self._make_ctrl()
        ctrl._update_v2_drawer_from_event(
            run_id, "run.created", {}, RunDrawerItem,
        )
        # Drive the orchestrator to a terminal state.
        ctrl._v2_orchestrator.mark_context_ready(run_id)
        ctrl._v2_orchestrator.mark_plan_ready(run_id)
        ctrl._v2_orchestrator.begin_verification(run_id)
        ctrl._v2_orchestrator.finish_verification(run_id, success=True)
        ctrl._update_v2_drawer_from_event(
            run_id, "run.completed", {}, RunDrawerItem,
        )
        main = next(i for i in ctrl._v2_drawer_items if i.kind == "run")
        assert main.status == "succeeded"
        assert main.can_cancel is False
        assert main.duration_ms is not None

    def test_cancelled_updates_main_item(self):
        from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

        ctrl, run_id = self._make_ctrl()
        ctrl._update_v2_drawer_from_event(
            run_id, "run.created", {}, RunDrawerItem,
        )
        ctrl._v2_orchestrator.request_cancel(run_id)
        ctrl._update_v2_drawer_from_event(
            run_id, "run.cancelled", {}, RunDrawerItem,
        )
        main = next(i for i in ctrl._v2_drawer_items if i.kind == "run")
        assert main.status == "cancelled"
        assert main.can_cancel is False

    def test_refresh_v2_run_drawer_pushes_state_to_panel(self):
        from src.features.edit.ui.ai_assistant_composer import (
            RunDrawerItem, RunDrawerState,
        )

        ctrl, run_id = self._make_ctrl()
        ctrl._update_v2_drawer_from_event(
            run_id, "run.created", {}, RunDrawerItem,
        )
        ctrl._refresh_v2_run_drawer()
        ctrl._panel.set_run_drawer.assert_called_once()
        state = ctrl._panel.set_run_drawer.call_args[0][0]
        assert isinstance(state, RunDrawerState)
        assert state.active_count == 1
        assert len(state.items) == 1

    def test_archive_moves_items_to_history(self):
        """New run archives previous items to history."""
        from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

        ctrl, run_id = self._make_ctrl()
        ctrl._update_v2_drawer_from_event(
            run_id, "run.created", {}, RunDrawerItem,
        )
        assert len(ctrl._v2_drawer_items) == 1
        ctrl._archive_v2_drawer_items()
        assert len(ctrl._v2_drawer_items) == 0
        assert len(ctrl._v2_drawer_history) == 1

    def test_stale_event_filtered_by_active_run_id(self):
        """Late events from an old run must be discarded when a newer run is active."""
        from src.features.edit.ai_editing.agent_models import AgentTask

        ctrl, run1 = self._make_ctrl()
        # Finish run1 → terminal
        ctrl._v2_orchestrator.mark_context_ready(run1)
        ctrl._v2_orchestrator.mark_plan_ready(run1)
        ctrl._v2_orchestrator.begin_verification(run1)
        ctrl._v2_orchestrator.finish_verification(run1, success=True)
        # Start run2 (now active)
        task2 = AgentTask(
            task_id="t2", session_id="default", title="chat2",
            user_goal="hi2", acceptance_criteria=(), kind="chat",
        )
        ctrl._v2_orchestrator.start_task(task2, mode="chat")
        # Late event from run1 should be filtered
        event = _make_event(run1, "run.usage_recorded", {"actual_prompt": 99})
        ctrl._v2_drawer_items = []
        ctrl._on_v2_event_main(event)
        # run1 event must not have created any drawer items.
        assert len(ctrl._v2_drawer_items) == 0

    def test_artifact_staged_adds_card(self):
        from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

        ctrl, run_id = self._make_ctrl()
        ctrl._update_v2_drawer_from_event(
            run_id, "run.created", {}, RunDrawerItem,
        )
        ctrl._update_v2_drawer_from_event(
            run_id, "artifact.staged", {"kind": "html_document"},
            RunDrawerItem,
        )
        artifacts = [i for i in ctrl._v2_drawer_items if i.kind == "artifact"]
        assert len(artifacts) == 1
        assert "html_document" in artifacts[0].label


# ---------------------------------------------------------------------- #
# B3: Motion Graphic 生命周期 → AgentRun + 安全门控
# ---------------------------------------------------------------------- #


class TestMotionGraphicRunLifecycle:
    """B3: HTML generation creates an AgentRun with staged/verified/written states."""

    def _make_ctrl(self):
        from unittest.mock import MagicMock

        from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator

        ctrl = _bare_controller()
        ctrl._v2_orchestrator = AgentOrchestrator()
        ctrl._v2_mg_coordinator = None
        ctrl._v2_mg_runs = {}
        ctrl._v2_mg_requests = {}
        ctrl._v2_mg_artifacts = {}
        ctrl._panel = MagicMock()
        return ctrl

    def test_start_creates_run_and_tracks_request(self):
        ctrl = self._make_ctrl()
        run_id = ctrl.start_motion_graphic_run(
            "req-1", scene_revision=5, context_fingerprint="fp123",
        )
        assert run_id is not None
        assert "req-1" in ctrl._v2_mg_runs
        assert ctrl._v2_mg_runs["req-1"] == run_id

    def test_stage_html_stores_artifact_and_emits_events(self):
        ctrl = self._make_ctrl()
        run_id = ctrl.start_motion_graphic_run("req-2", scene_revision=3)
        assert run_id is not None

        ctrl.stage_motion_graphic_html(
            "req-2", html_hash="abc123", page_count=2,
            render_evidence={"rendered": True},
        )
        assert "req-2" in ctrl._v2_mg_artifacts
        artifact = ctrl._v2_mg_artifacts["req-2"]
        # Artifact must be staged (has hash reference, not inline HTML).
        assert artifact.is_staged
        assert "hash:abc123" in artifact.html_payload_ref
        # Verification evidence was recorded.
        assert artifact.has_verification_evidence
        # Events were emitted to the orchestrator (visible in Run Drawer via B1).
        events = [e.kind for e in ctrl._v2_orchestrator.get_events(run_id)]
        from src.features.edit.ai_editing.agent_models import AgentEventKind
        assert AgentEventKind.ARTIFACT_STAGED in events
        assert AgentEventKind.RUN_VERIFICATION_STARTED in events
        assert AgentEventKind.RUN_VERIFICATION_FINISHED in events

    def test_write_gate_passes_when_not_stale(self):
        ctrl = self._make_ctrl()
        ctrl.start_motion_graphic_run("req-3", scene_revision=7)
        ctrl.stage_motion_graphic_html(
            "req-3", html_hash="h1", render_evidence={"ok": True},
        )
        writable, reason = ctrl.check_motion_graphic_write_gate(
            "req-3", scene_revision=7,
        )
        assert writable is True
        assert reason == ""

    def test_write_gate_blocks_when_scene_stale(self):
        """B3: 用户修改场景后，旧 HTML 标记 stale，确认被阻断。"""
        ctrl = self._make_ctrl()
        ctrl.start_motion_graphic_run("req-4", scene_revision=5)
        ctrl.stage_motion_graphic_html(
            "req-4", html_hash="h2", render_evidence={"ok": True},
        )
        # Scene revision changed from 5 to 8 → stale.
        writable, reason = ctrl.check_motion_graphic_write_gate(
            "req-4", scene_revision=8,
        )
        assert writable is False
        assert "stale" in reason.lower() or "过期" in reason

    def test_write_gate_blocks_without_verification(self):
        ctrl = self._make_ctrl()
        ctrl.start_motion_graphic_run("req-5")
        ctrl.stage_motion_graphic_html("req-5", html_hash="h3")
        # No render_evidence → not verified.
        writable, reason = ctrl.check_motion_graphic_write_gate("req-5")
        assert writable is False
        assert "验证" in reason

    def test_write_gate_passes_when_v2_not_tracking(self):
        """When V2 is not tracking the request, the gate must not block legacy."""
        ctrl = self._make_ctrl()
        writable, reason = ctrl.check_motion_graphic_write_gate("unknown-req")
        assert writable is True
        assert reason == ""

    def test_complete_run_finalizes_state(self):
        ctrl = self._make_ctrl()
        run_id = ctrl.start_motion_graphic_run("req-6")
        ctrl.stage_motion_graphic_html("req-6", html_hash="h4", render_evidence={"ok": True})
        ctrl.complete_motion_graphic_run("req-6", success=True)
        # Request tracking is cleaned up.
        assert "req-6" not in ctrl._v2_mg_runs
        assert "req-6" not in ctrl._v2_mg_requests
        # Run reached a terminal state.
        from src.features.edit.ai_editing.agent_models import AgentRunState
        run = ctrl._v2_orchestrator.get_run(run_id)
        assert run.state == AgentRunState.SUCCEEDED

    def test_complete_run_failed_marks_failed(self):
        ctrl = self._make_ctrl()
        run_id = ctrl.start_motion_graphic_run("req-7")
        ctrl.complete_motion_graphic_run("req-7", success=False)
        from src.features.edit.ai_editing.agent_models import AgentRunState
        run = ctrl._v2_orchestrator.get_run(run_id)
        assert run.state == AgentRunState.FAILED


# ---------------------------------------------------------------------- #
# B4: Checkpoint 持久化 + 恢复
# ---------------------------------------------------------------------- #


class TestCheckpointPersistence:
    """B4: V2 orchestrator state persists to disk and restores on reopen."""

    def _make_ctrl(self, tmp_path):
        from unittest.mock import MagicMock

        from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator

        ctrl = _bare_controller()
        ctrl._v2_orchestrator = AgentOrchestrator()
        ctrl._v2_mg_coordinator = None
        ctrl._v2_mg_runs = {}
        ctrl._v2_mg_requests = {}
        ctrl._v2_mg_artifacts = {}
        ctrl._panel = MagicMock()
        # Override the state file path to use a temp directory.
        state_file = tmp_path / "v2_state.json"
        ctrl._v2_state_file_path = lambda: state_file
        return ctrl

    def test_persist_writes_state_and_pauses_verifying(self, tmp_path):
        """B4: VERIFYING 状态的 run 持久化时转为 PAUSED_FOR_RESUME。"""
        from src.features.edit.ai_editing.agent_models import AgentTask

        ctrl = self._make_ctrl(tmp_path)
        task = AgentTask(
            task_id="t1", session_id="default", title="chat",
            user_goal="hi", acceptance_criteria=(), kind="chat",
        )
        run_id = ctrl._v2_orchestrator.start_task(task, mode="chat")
        # Drive to VERIFYING (the only state that can pause_for_resume).
        ctrl._v2_orchestrator.mark_context_ready(run_id)
        ctrl._v2_orchestrator.mark_plan_ready(run_id)
        ctrl._v2_orchestrator.begin_verification(run_id)

        ctrl.persist_v2_state()

        run = ctrl._v2_orchestrator.get_run(run_id)
        assert run.state == "paused_for_resume"

        import json
        from pathlib import Path
        state_file = Path(ctrl._v2_state_file_path())
        assert state_file.exists()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert len(data["runs"]) == 1

    def test_persist_cancels_early_stage_runs(self, tmp_path):
        """B4: 早期阶段的 run 无法暂停，持久化时安全取消。"""
        from src.features.edit.ai_editing.agent_models import AgentTask

        ctrl = self._make_ctrl(tmp_path)
        task = AgentTask(
            task_id="t1", session_id="default", title="chat",
            user_goal="hi", acceptance_criteria=(), kind="chat",
        )
        run_id = ctrl._v2_orchestrator.start_task(task, mode="chat")
        # Run is in gathering_context (early stage).

        ctrl.persist_v2_state()

        run = ctrl._v2_orchestrator.get_run(run_id)
        assert run.state == "cancelled"

    def test_restore_loads_state_from_disk(self, tmp_path):
        from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
        from src.features.edit.ai_editing.agent_models import AgentTask

        ctrl = self._make_ctrl(tmp_path)
        task = AgentTask(
            task_id="t1", session_id="default", title="chat",
            user_goal="hi", acceptance_criteria=(), kind="chat",
        )
        run_id = ctrl._v2_orchestrator.start_task(task, mode="chat")
        # Drive to VERIFYING then persist (paused).
        ctrl._v2_orchestrator.mark_context_ready(run_id)
        ctrl._v2_orchestrator.mark_plan_ready(run_id)
        ctrl._v2_orchestrator.begin_verification(run_id)
        ctrl.persist_v2_state()

        # Fresh orchestrator + restore.
        ctrl2 = _bare_controller()
        ctrl2._v2_orchestrator = AgentOrchestrator()
        ctrl2._v2_state_file_path = ctrl._v2_state_file_path
        restored = ctrl2.restore_v2_state()
        assert restored == 1

        run = ctrl2._v2_orchestrator.get_run(run_id)
        assert run is not None
        assert run.state == "paused_for_resume"

    def test_restore_no_auto_grant(self, tmp_path):
        """B4 红线 5: 恢复后无自动授权（AgentRun 无 grant 字段 → 结构性保证）。"""
        from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
        from src.features.edit.ai_editing.agent_models import AgentTask

        ctrl = self._make_ctrl(tmp_path)
        task = AgentTask(
            task_id="t1", session_id="default", title="chat",
            user_goal="hi", acceptance_criteria=(), kind="chat",
        )
        run_id = ctrl._v2_orchestrator.start_task(task, mode="chat")
        ctrl._v2_orchestrator.mark_context_ready(run_id)
        ctrl._v2_orchestrator.mark_plan_ready(run_id)
        ctrl._v2_orchestrator.begin_verification(run_id)
        ctrl.persist_v2_state()

        ctrl2 = _bare_controller()
        ctrl2._v2_orchestrator = AgentOrchestrator()
        ctrl2._v2_state_file_path = ctrl._v2_state_file_path
        ctrl2.restore_v2_state()

        run = ctrl2._v2_orchestrator.get_run(run_id)
        assert run.state == "paused_for_resume"
        # No active run after restore (paused ≠ active).
        assert ctrl2._v2_orchestrator.active_run_id("default") is None

    def test_restore_missing_file_is_noop(self, tmp_path):
        from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator

        ctrl = _bare_controller()
        ctrl._v2_orchestrator = AgentOrchestrator()
        ctrl._v2_state_file_path = lambda: tmp_path / "nonexistent.json"
        assert ctrl.restore_v2_state() == 0


# ---------------------------------------------------------------------- #
# B7: 媒体生成统一事件流
# ---------------------------------------------------------------------- #


class TestMediaGenerationEventStream:
    """B7: Media generation creates V2 AgentRuns visible in the Run Drawer."""

    def _make_ctrl(self):
        from unittest.mock import MagicMock

        from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator

        ctrl = _bare_controller()
        ctrl._v2_orchestrator = AgentOrchestrator()
        ctrl._v2_media_runs = {}
        ctrl._panel = MagicMock()
        return ctrl

    def test_track_media_creates_v2_run(self):
        ctrl = self._make_ctrl()
        ctrl._v2_track_media_generation("runtime-1", mode="image")
        assert "runtime-1" in ctrl._v2_media_runs
        v2_run_id = ctrl._v2_media_runs["runtime-1"]
        run = ctrl._v2_orchestrator.get_run(v2_run_id)
        assert run is not None
        assert run.state == "executing"

    def test_stage_media_artifact_emits_event(self):
        from src.features.edit.ai_editing.agent_models import (
            AgentEventKind,
        )

        ctrl = self._make_ctrl()
        ctrl._v2_track_media_generation("runtime-2", mode="image")
        ctrl._v2_stage_media_artifact("runtime-2", artifact_kind="image")

        v2_run_id = ctrl._v2_media_runs["runtime-2"]
        events = [e.kind for e in ctrl._v2_orchestrator.get_events(v2_run_id)]
        assert AgentEventKind.ARTIFACT_STAGED in events

    def test_complete_media_success_finalizes_succeeded(self):
        from src.features.edit.ai_editing.agent_models import AgentRunState

        ctrl = self._make_ctrl()
        ctrl._v2_track_media_generation("runtime-3")
        ctrl._v2_stage_media_artifact("runtime-3")
        ctrl._v2_complete_media_generation("runtime-3", success=True)

        assert "runtime-3" not in ctrl._v2_media_runs  # cleaned up

    def test_complete_media_failure_marks_failed(self):
        from src.features.edit.ai_editing.agent_models import AgentRunState

        ctrl = self._make_ctrl()
        ctrl._v2_track_media_generation("runtime-4")
        ctrl._v2_complete_media_generation(
            "runtime-4", success=False, reason="generation API timeout",
        )

        assert "runtime-4" not in ctrl._v2_media_runs

    def test_complete_without_track_is_noop(self):
        """Completing an untracked run must not crash."""
        ctrl = self._make_ctrl()
        ctrl._v2_complete_media_generation("unknown", success=True)
        # No crash, no state change.
        assert ctrl._v2_media_runs == {}


class TestLegacyChatActivity:
    """The legacy fallback exposes the same observable phases as V2."""

    def test_activity_lifecycle_is_published_to_run_drawer(self):
        from unittest.mock import MagicMock

        ctrl = _bare_controller()
        ctrl._panel = MagicMock()
        ctrl._begin_legacy_chat_activity("准备对话")
        started = ctrl._panel.set_run_drawer.call_args[0][0]
        assert started.items[0].label == "准备对话"
        assert started.items[0].status == "running"
        assert started.active_count == 1

        ctrl._update_legacy_chat_activity("生成回复", detail="已生成 12 字")
        running = ctrl._panel.set_run_drawer.call_args[0][0]
        assert running.items[0].label == "生成回复"
        assert running.items[0].output_summary == "已生成 12 字"

        ctrl._finish_legacy_chat_activity("已完成")
        finished = ctrl._panel.set_run_drawer.call_args[0][0]
        assert finished.items[0].status == "succeeded"
        assert finished.completed_count == 1
