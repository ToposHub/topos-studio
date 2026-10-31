"""Recovery contract for interrupted Motion Graphic creation tasks."""

from __future__ import annotations

from types import SimpleNamespace

from src.app.controllers.file_controller import FileOpsMixin
from src.features.teaching_animation.ai.brief import MotionGraphicBrief
from src.features.teaching_animation.module import TeachingAnimationModule


class _ChatService:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str | None]] = []

    def append_chat_message(
        self, role: str, text: str, *, error_code: str | None = None,
    ) -> None:
        self.messages.append((role, text, error_code))


class _PlanningController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []
        self.system_prompts: list[str] = []

    def set_teaching_motion_graphic_build_system_prompt(self, prompt: str) -> None:
        self.system_prompts.append(prompt)

    def start_motion_graphic_build_chat(
        self, request_id: str, prompt: str, *, temperature: float = 0.0,
    ) -> bool:
        self.calls.append((request_id, prompt, temperature))
        return True


def _pending_module() -> tuple[TeachingAnimationModule, str]:
    module = TeachingAnimationModule()
    brief = MotionGraphicBrief(
        brief_id="brief-recovery",
        topic="什么是 AI Agent？",
        goal="解释从目标到工具执行的循环",
        duration_seconds=15.0,
        canvas_aspect="16:9",
        width=1920,
        height=1080,
        fps=24.0,
    )
    request_id = "motion-recovery-1"
    module._pending_motion_graphic_requests[request_id] = {
        "seconds": 15.0,
        "fps": 24.0,
        "frame": 120,
        "has_timeline": True,
        "track_id": "graphics",
        "variant": 1,
        "user_text": "创建一个 15 秒、1080p 的 AI Agent 动画",
        "brief": brief,
        "scene": object(),  # Must never be persisted or revived.
        "phase": "build",
        "plan_approved": True,  # Must become false after recovery.
        "generation_feedback_memory": ("每页再丰富一些", "改成橙色色系"),
    }
    return module, request_id


def test_pending_task_snapshot_keeps_intent_but_excludes_unsafe_runtime_state() -> None:
    module, request_id = _pending_module()

    snapshot = module._export_motion_graphic_task_memory()

    assert snapshot is not None
    assert snapshot["version"] == 1
    saved = snapshot["tasks"][0]
    assert saved["request_id"] == request_id
    assert saved["brief"]["width"] == 1920
    assert saved["brief"]["height"] == 1080
    assert saved["generation_feedback_memory"] == ["每页再丰富一些", "改成橙色色系"]
    assert "scene" not in saved
    assert "plan_approved" not in saved


def test_restored_task_requires_explicit_continue_before_any_model_call() -> None:
    source, request_id = _pending_module()
    snapshot = source._export_motion_graphic_task_memory()
    assert snapshot is not None

    controller = _PlanningController()
    service = _ChatService()
    window = SimpleNamespace(
        _ai_edit_controller=controller,
        _ai_assistant_service=service,
    )
    restored = TeachingAnimationModule()
    restored._main_window = window

    assert restored._restore_motion_graphic_task_memory(snapshot) == 1
    plan = restored._pending_motion_graphic_requests[request_id]
    assert plan["phase"] == "resume_required"
    assert plan["resume_phase"] == "build"
    assert plan["scene"] is None
    assert plan["plan_approved"] is False
    assert controller.calls == []

    # Additional requirements are retained, but they cannot spend provider
    # budget or reapply stale output until the user explicitly resumes.
    assert restored._handle_ai_request(window, "第 4 页补一个行动清单") is True
    assert controller.calls == []
    assert "第 4 页补一个行动清单" in plan["generation_feedback_memory"]

    assert restored._handle_ai_request(window, "继续") is True
    assert plan["phase"] == "plan_review"
    assert plan["creative_brief_pending"] is True
    assert len(controller.calls) == 1
    assert controller.calls[0][0] == request_id
    assert "reviewable production plan" in "\n".join(text for _, text, _ in service.messages)


def test_corrupt_task_row_is_ignored_without_losing_other_recoverable_tasks() -> None:
    module, request_id = _pending_module()
    snapshot = module._export_motion_graphic_task_memory()
    assert snapshot is not None
    snapshot["tasks"].insert(0, {
        "request_id": "bad-row",
        "brief": {"topic": "损坏任务"},
        "frame": "not-a-frame",
    })

    restored = TeachingAnimationModule()

    assert restored._restore_motion_graphic_task_memory(snapshot) == 1
    assert set(restored._pending_motion_graphic_requests) == {request_id}


def test_project_payload_round_trip_uses_active_host_callbacks() -> None:
    """Task memory travels inside the existing project payload, not a dormant store."""

    task_memory = {
        "version": 1,
        "tasks": [{"request_id": "recover-me", "brief": {"topic": "AI Agent"}}],
    }

    class _Host(FileOpsMixin):
        composite_page = None

        def _get_timeline_sequences(self):
            return {}

        def _export_motion_graphic_task_memory(self):
            return task_memory

    host = _Host()
    payload = host._get_teaching_animation_payload()
    assert payload is not None
    assert payload["metadata"]["motion_graphic_task_memory_v1"] == task_memory

    received: list[object] = []
    receiver = _Host()
    receiver._restore_motion_graphic_task_memory = received.append
    receiver._restore_teaching_animation_payload(payload)
    assert received == [task_memory]
