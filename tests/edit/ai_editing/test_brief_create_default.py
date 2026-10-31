"""TAEP M1.3 — BriefService.create_default + AssistantService brief 流程测试。

验收（审查 M1.3 / TAEP §5.3）：

- ``create_default`` 返回合理默认 brief；幂等（已存在不覆盖）。
- ``apply_user_brief`` 直接 save 用户编辑的完整 brief（用户是真相源，不走 patch）。
- ``preview_brief_patch`` 把 AI 提议合并成预览 brief（不 save、不 bump）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.brief_service import BriefService, BriefPatch
from src.features.edit.ai_editing.editorial_models import (
    DurationTarget,
    EditorialBrief,
    Platform,
)
from src.features.edit.ai_editing.assistant_service import (
    AssistantConfig,
    AssistantService,
)
from src.features.edit.ai_editing.edit_api import EditSession


# ================================================================
# create_default（BriefService 级）
# ================================================================


def test_create_default_returns_sensible_brief():
    bs = BriefService()
    brief = bs.create_default("default")
    assert brief.brief_id == "default"
    assert brief.revision == 0
    assert brief.platform == Platform.VERTICAL_SOCIAL
    assert brief.target_duration is not None
    assert brief.target_duration.ideal == 720
    assert brief.required_beats == ("hook", "product_value", "call_to_action")


def test_create_default_is_idempotent():
    """已存在的 brief 不被覆盖（保留用户修改）。"""
    bs = BriefService()
    bs.create_default("default")
    # 用户修改了它（save 一个 revision=5 的版本）
    modified = EditorialBrief(
        brief_id="default", revision=5, primary_goal="我的目标",
    )
    bs.save(modified)
    # 再次 create_default → 返回已保存的，不覆盖
    again = bs.create_default("default")
    assert again.revision == 5
    assert again.primary_goal == "我的目标"


def test_create_default_separate_ids_independent():
    bs = BriefService()
    a = bs.create_default("brief-a")
    b = bs.create_default("brief-b")
    assert a.brief_id == "brief-a"
    assert b.brief_id == "brief-b"


# ================================================================
# AssistantService.preview_brief_patch / apply_user_brief
# ================================================================


def _service():
    class _Host:
        def get_state(self):
            return None

        def get_manifest(self):
            from src.features.edit.ai_editing.capability_manifest import build_manifest
            return build_manifest()

        def on_committed(self, result):
            pass

        def request_user_approval(self, plan, diff):
            return None

    EditSession.unbind()
    session = EditSession.bind(_Host())
    return AssistantService(session), session


def test_service_preview_brief_patch_creates_default_if_missing():
    """brief 不存在时 preview 先 create_default 再合并 patch；不 save。"""
    svc, session = _service()
    patch = BriefPatch(base_revision=0, changes={"primary_goal": "30 秒竖版"})
    preview = svc.preview_brief_patch("default", patch)
    assert preview.primary_goal == "30 秒竖版"
    assert preview.platform == Platform.VERTICAL_SOCIAL  # 来自 create_default
    # 预览返回的是合并结果，未额外 bump
    assert preview.revision == 0
    EditSession.unbind()


def test_service_apply_user_brief_saves_directly():
    """apply_user_brief 直接 save 用户编辑的完整 brief。"""
    from src.features.edit.ai_editing.assistant_events import AssistantState

    svc, session = _service()
    svc.start_session(
        AssistantConfig(),
        active_profile_id="p", profile_connected=True,
    )
    base = svc.preview_brief_patch(
        "default", BriefPatch(base_revision=0, changes={})
    )
    user_brief = EditorialBrief(
        brief_id="default", revision=base.revision + 1,
        primary_goal="用户确认的目标", platform=Platform.HORIZONTAL,
        target_duration=DurationTarget(480, 432, 528),
        required_beats=("hook", "call_to_action"),
    )
    saved = svc.apply_user_brief("default", user_brief)
    assert saved.primary_goal == "用户确认的目标"
    assert saved.platform == Platform.HORIZONTAL
    # 已 save（service 内部 brief service 持有）
    assert svc.get_brief("default") is saved
    # apply_user_brief 不进 PLANNING（由 controller 单独触发）
    assert svc.state != AssistantState.PLANNING
    EditSession.unbind()

