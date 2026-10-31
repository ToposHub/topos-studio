"""Explicit AI Assistant composer mode routing tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.features.edit.ai_editing.assistant_events import AssistantRequest
from src.features.edit.ai_editing.assistant_modes import (
    DEFAULT_ASSISTANT_MODE,
    MEDIA_ASSISTANT_MODES,
    normalize_assistant_mode,
)


def test_unknown_mode_falls_back_to_safe_chat():
    assert normalize_assistant_mode("") == DEFAULT_ASSISTANT_MODE
    assert normalize_assistant_mode("future_provider_mode") == DEFAULT_ASSISTANT_MODE
    assert MEDIA_ASSISTANT_MODES == {"video", "music", "image"}


def test_media_mode_is_routed_before_natural_language_router():
    from src.app.controllers.ai_edit_controller import AiEditController

    controller = AiEditController.__new__(AiEditController)
    controller._panel = MagicMock()
    controller._teaching_motion_graphic_mode_handler = None
    controller._teaching_request_handler = None
    controller._teaching_motion_graphic_discussion_handler = None
    controller._teaching_scene_edit_request_handler = None
    controller._current_assistant_mode = "chat"
    controller._record_user_message = MagicMock()
    controller._refresh_composer_footer = MagicMock()
    controller._build_chat_scope = MagicMock(return_value="scope")
    controller._request_ai_generation = MagicMock()
    controller.router = MagicMock()

    controller._on_request_submitted(AssistantRequest(
        request_id="r1", text="一段海边日落", mode="video",
    ))

    controller._request_ai_generation.assert_called_once()
    result, scope, text = controller._request_ai_generation.call_args.args
    assert result.extracted_inputs == {"modality": "video"}
    assert scope == "scope"
    assert text == "一段海边日落"
    controller.router.route.assert_not_called()


def test_motion_graphic_mode_uses_feature_handler_without_router():
    from src.app.controllers.ai_edit_controller import AiEditController

    controller = AiEditController.__new__(AiEditController)
    controller._panel = MagicMock()
    received = []
    controller._teaching_motion_graphic_mode_handler = (
        lambda text: received.append(text) or True
    )
    controller._teaching_request_handler = None
    controller._teaching_motion_graphic_discussion_handler = None
    controller._teaching_scene_edit_request_handler = None
    controller._current_assistant_mode = "chat"
    controller._record_user_message = MagicMock()
    controller._refresh_composer_footer = MagicMock()
    controller.router = MagicMock()

    controller._on_request_submitted(AssistantRequest(
        request_id="r2", text="做一份 HTML5 教学演示", mode="motion_graphic",
    ))

    assert received == ["做一份 HTML5 教学演示"]
    controller.router.route.assert_not_called()
