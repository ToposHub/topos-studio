"""AssistantService exposes the controlled role-executor injection seam."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.assistant_service import AssistantService


def test_default_skill_runtime_keeps_injected_role_executor() -> None:
    role_executor = MagicMock()
    service = AssistantService(MagicMock(), role_executor=role_executor)
    runtime = service._build_default_skill_runtime()
    assert runtime._role_executor is role_executor
