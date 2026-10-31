"""P0.4 接线层测试：会话历史接线（record_run / add_message_summary 生产调用）。

> 验证 §10.2 P0.4 在 controller 侧的契约：
> - controller 接受 session_store 参数
> - run 终态后调 store.record_run + add_message_summary（assistant 摘要）
> - _on_request_submitted 调 add_message_summary（user 摘要）
> - 文本脱敏（绝对路径不落盘）
> - new_session / show_history signal 接通
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
    RunStatus,
)
from src.features.edit.ai_editing.assistant_session_store import AssistantSessionStore
from src.features.edit.ai_editing.session_models import MessageSummary, RunRef

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #


def _make_controller(qapp, *, runtime=None, store=None):
    """构造 AiEditController + fake service/panel + 可选 runtime/store。"""
    from src.app.controllers.ai_edit_controller import AiEditController

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()

    fake_panel = MagicMock()
    sig_names = (
        "request_submitted", "variant_selected", "apply_requested",
        "cancel_requested", "brief_patch_requested", "context_inspector_requested",
        "composer_scope_requested", "composer_permission_changed",
        "composer_profile_requested", "context_compress_requested",
        "new_session_requested", "show_history_requested",
        "run_cancel_requested", "run_retry_requested", "run_diagnostics_requested",
    )
    for n in sig_names:
        getattr(fake_panel, n).connect = MagicMock()
        getattr(fake_panel, n).disconnect = MagicMock()

    controller = AiEditController(
        panel=fake_panel, service=fake_service, runtime=runtime,
        session_store=store, parent=qapp,
    )
    return controller, fake_panel, fake_service


def _start_and_finish_run(runtime, *, success=True, skill_id="topos.test"):
    inv = Invocation(
        source=InvocationSource.CHAT, skill_id=skill_id,
        scope=InvocationScope(project_id="p1"),
    )
    run_id = runtime.start(inv)
    runtime.mark_running(run_id)
    runtime.finish(run_id, success=success)
    return run_id


# ---------------------------------------------------------------------- #
# controller 接受 session_store
# ---------------------------------------------------------------------- #


def test_controller_accepts_session_store_kwarg(qapp):
    """AiEditController.__init__ 接受 session_store 关键字参数。"""
    store = AssistantSessionStore()
    controller, _, _ = _make_controller(qapp, store=store)
    assert controller.session_store is store


def test_controller_session_store_defaults_none(qapp):
    """未传 session_store 时为 None（向后兼容）。"""
    controller, _, _ = _make_controller(qapp)
    assert controller.session_store is None


# ---------------------------------------------------------------------- #
# run 终态 → record_run + add_message_summary
# ---------------------------------------------------------------------- #


def test_run_terminal_records_run_ref(qapp):
    """run 终态后 store.record_run 被调，entry.run_refs 含该 run。"""
    runtime = HarnessRuntime()
    store = AssistantSessionStore()
    controller, _, _ = _make_controller(qapp, runtime=runtime, store=store)
    run_id = controller.start_chat_run(skill_id="topos.auto_caption")
    controller._active_chat_run_id = run_id
    runtime.mark_running(run_id)
    runtime.finish(run_id, success=True)

    # 触发终态记录（_on_run_state_changed_main 在 run 终态时调）
    controller._record_run_finalization(run_id)

    entry = store.get_active_session()
    run_ref_ids = [r.run_id for r in entry.run_refs]
    assert run_id in run_ref_ids
    # skill_id 保留
    ref = next(r for r in entry.run_refs if r.run_id == run_id)
    assert ref.skill_id == "topos.auto_caption"
    assert ref.status == RunStatus.SUCCEEDED


def test_run_terminal_adds_assistant_message_summary(qapp):
    """run 终态后 store.add_message_summary 写 assistant 摘要。"""
    runtime = HarnessRuntime()
    store = AssistantSessionStore()
    controller, _, _ = _make_controller(qapp, runtime=runtime, store=store)
    run_id = controller.start_chat_run(skill_id="topos.test")
    controller._active_chat_run_id = run_id
    runtime.mark_running(run_id)
    runtime.finish(run_id, success=True)

    controller._record_run_finalization(run_id, summary="生成 3 个方案")

    entry = store.get_active_session()
    assistant_msgs = [m for m in entry.messages if m.role == "assistant"]
    assert any(run_id == m.run_id for m in assistant_msgs)


def test_record_run_finalization_noop_without_store(qapp):
    """store 未注入时 _record_run_finalization 不抛错（向后兼容）。"""
    runtime = HarnessRuntime()
    controller, _, _ = _make_controller(qapp, runtime=runtime, store=None)
    # 不抛错即可
    controller._record_run_finalization("fake-run-id")


# ---------------------------------------------------------------------- #
# user 请求 → add_message_summary（user 摘要）
# ---------------------------------------------------------------------- #


def test_request_submitted_records_user_message(qapp):
    """_on_request_submitted 调 store.add_message_summary（role=user）。"""
    store = AssistantSessionStore()
    controller, fake_panel, fake_service = _make_controller(qapp, store=store)
    # 模拟 panel emit request_submitted（直接调 slot）
    fake_service.submit_request = MagicMock()
    fake_service.propose_brief_patch = MagicMock(return_value=MagicMock())
    fake_service.preview_brief_patch = MagicMock(return_value=MagicMock())

    from src.features.edit.ai_editing.assistant_events import AssistantRequest
    controller._on_request_submitted(
        AssistantRequest(request_id="r1", text="帮我剪 30 秒竖版")
    )

    entry = store.get_active_session()
    user_msgs = [m for m in entry.messages if m.role == "user"]
    assert len(user_msgs) >= 1
    assert "帮我剪 30 秒竖版" in user_msgs[-1].text


def test_user_message_text_is_sanitized(qapp):
    """user 摘要落盘时脱敏绝对路径（红线：不落盘绝对路径）。"""
    store = AssistantSessionStore()
    controller, fake_panel, fake_service = _make_controller(qapp, store=store)
    fake_service.submit_request = MagicMock()
    fake_service.propose_brief_patch = MagicMock(return_value=MagicMock())
    fake_service.preview_brief_patch = MagicMock(return_value=MagicMock())

    from src.features.edit.ai_editing.assistant_events import AssistantRequest
    controller._on_request_submitted(
        AssistantRequest(request_id="r1", text="处理 /Users/testuser/secret/video.mp4")
    )

    entry = store.get_active_session()
    user_msgs = [m for m in entry.messages if m.role == "user"]
    # 落盘的 to_dict 经 sanitize_for_storage
    dumped = user_msgs[-1].to_dict()
    assert "/Users/testuser/secret" not in dumped["text"]


# ---------------------------------------------------------------------- #
# new_session / show_history signal
# ---------------------------------------------------------------------- #


def test_new_session_creates_session_without_deleting_history(qapp):
    """new_session_requested → store.create_session；旧会话保留（红线）。"""
    store = AssistantSessionStore()
    old_session = store.get_active_session()
    controller, _, _ = _make_controller(qapp, store=store)

    controller._on_new_session()

    sessions = store.list_sessions()
    assert len(sessions) >= 2, "应新建会话，旧会话保留"
    assert any(s.session_id == old_session.session_id for s in sessions), (
        "旧会话不应被删除"
    )


def test_new_session_noop_without_store(qapp):
    """store 未注入时 _on_new_session 不抛错。"""
    controller, _, _ = _make_controller(qapp, store=None)
    controller._on_new_session()  # 不抛错


def test_show_history_switches_active_session(qapp):
    """show_history_requested → 列出会话 → 切换 active。"""
    store = AssistantSessionStore()
    first = store.get_active_session()
    second = store.create_session("会话 2")

    controller, _, _ = _make_controller(qapp, store=store)
    # 模拟用户选了 second（直接调内部切换方法，跳过 QInputDialog）
    controller._switch_to_session(second.session_id)

    assert store.get_active_session().session_id == second.session_id


def test_show_history_archived_cannot_be_active(qapp):
    """归档会话不能设为 active（红线 §10.2 P0.4）。"""
    store = AssistantSessionStore()
    archived = store.create_session("已归档")
    store.archive_session(archived.session_id)

    controller, _, _ = _make_controller(qapp, store=store)
    result = controller._switch_to_session(archived.session_id)
    assert result is False, "归档会话不应能设为 active"


# ---------------------------------------------------------------------- #
# dispose 断开新 signal
# ---------------------------------------------------------------------- #


def test_controller_disposes_cleanly_with_store(qapp):
    """dispose 不抛错（store + runtime 都注入时）。"""
    runtime = HarnessRuntime()
    store = AssistantSessionStore()
    controller, _, _ = _make_controller(qapp, runtime=runtime, store=store)
    controller.dispose()  # 不抛错即可
