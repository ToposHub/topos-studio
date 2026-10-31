"""P1 自由对话测试：unknown 分支 → LLM chat（DeepSeek 普通文本回复）。

> 验证用户发送普通文本（未命中任何意图关键词）时：
> - 已配置 profile → 走 LLM 自由对话（make_client + _ChatWorker 同步路径）
>   → assistant 回复以 AssistantMessage 形式追加 + 写会话摘要
> - 未配置 profile → 仍走 set_skill_recommendation（行为不变）
> - LLM 调用失败 → 显示红色错误消息（error_code=llm_error）+ 仍出推荐卡兜底
> - 多轮历史正确拼装（system + 最近 N 条 user/assistant 消息）

用 fake panel + monkeypatch make_client 注入 fake client，TOPOS_TEST_MODE 走同步路径。
"""
from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.invocation_router import InvocationRouter
from src.features.ai_harness.skill_compiler import SkillCompiler
from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
from src.features.ai_harness.help_assistant import HelpAssistant
from src.features.ai_harness.skill_catalog import SkillCatalogView
from src.features.ai_harness.editorial_skills import register_p2_skills
from src.features.edit.ai_editing.skill_registry import SkillRegistry
from src.features.edit.ai_editing.provider_profiles import AiProviderProfile
from src.features.edit.ai_editing.provider_profiles import ProviderLimits
from src.features.edit.ai_editing.assistant_events import AssistantState

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# 辅助：构造完整 P1 + profile 装配的 controller
# ---------------------------------------------------------------------- #


def _make_profile() -> AiProviderProfile:
    """构造一个测试用 DeepSeek profile（不含真实 secret）。"""
    return AiProviderProfile(
        profile_id="profile-test",
        display_name="DeepSeek-Test",
        provider_kind="openai",
        endpoint="https://api.deepseek.com",
        model_id="deepseek-test",
        credential_ref="topos:profile-test",
    )


class _FakeRegistry:
    """最小 ProfileRegistry 替身：active_profile 恒返回测试 profile。"""

    def __init__(self, profile):
        self.active_profile = profile
        self._profile = profile

    def get(self, pid):  # noqa: ARG002
        return self._profile

    def set_active(self, pid):  # noqa: ARG002
        pass

    def save_to_user_file(self):
        pass


class _FakeCredentialStore:
    """最小 CredentialStore 替身：恒返回 fake key。"""

    def get_secret(self, ref):  # noqa: ARG002
        return "sk-fake-test-key"


class _FakeClient:
    """可编程的 LLM client：reply / raise 按预设行为返回。"""

    def __init__(self, *, reply="你好，我是 AI 剪辑助手。", raise_exc=None):
        self._reply = reply
        self._raise = raise_exc
        self.calls = []

    def complete(self, messages, *, json_mode=False, temperature=0.7, max_tokens=None):
        self.calls.append({
            "messages": messages, "json_mode": json_mode,
            "temperature": temperature, "max_tokens": max_tokens,
        })
        if self._raise is not None:
            raise self._raise
        return self._reply


def _make_p1_controller_with_profile(qapp, fake_client):
    """构造装配完整 P1 依赖 + profile 的 controller。

    返回 (controller, fake_panel, fake_service)。
    用 MagicMock service（与 test_p1_router_dispatch.py 一致），但 stub 出
    append_chat_message / recent_chat_messages / _set_state 供 chat 路径使用，
    并维护一个真实 _messages 列表便于断言。
    """
    from src.app.controllers.ai_edit_controller import AiEditController
    from src.features.edit.ai_editing.assistant_events import AssistantMessage

    runtime = HarnessRuntime()
    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()

    # 维护真实 messages 列表 + stub append_chat_message / recent_chat_messages
    messages: list[AssistantMessage] = []
    fake_service._messages = messages

    def _append(role, text, *, error_code=None, attachments=()):
        messages.append(AssistantMessage(
            message_id=f"m{len(messages)}", role=role, text=text, error_code=error_code,
            attachments=attachments,
        ))
        # 触发 on_state_changed 回调（如有）
        if fake_service.on_state_changed:
            fake_service.on_state_changed(MagicMock())
    fake_service.append_chat_message = _append

    def _recent(limit=10):
        return [m for m in messages if m.role in ("user", "assistant") and (m.text or m.attachments)][-limit:]
    fake_service.recent_chat_messages = _recent

    fake_service._set_state = MagicMock()
    fake_service._emit_state = MagicMock()

    fake_panel = _make_recording_panel()

    reg = SkillRegistry()
    register_p2_skills(reg)
    help_assistant = HelpAssistant()
    router = InvocationRouter(
        help_search=help_assistant.entry_ids_for_query,
        skill_resolver=reg.is_enabled,
    )
    compiler = SkillCompiler()
    adapter = LegacyHarnessAdapter(fake_service, runtime)
    catalog = SkillCatalogView(reg)

    profile = _make_profile()
    profile_registry = _FakeRegistry(profile)
    credential_store = _FakeCredentialStore()

    controller = AiEditController(
        panel=fake_panel, service=fake_service, runtime=runtime,
        router=router, compiler=compiler, adapter=adapter,
        help_assistant=help_assistant, catalog=catalog,
        skill_registry=reg, profile_registry=profile_registry,
        credential_store=credential_store, parent=qapp,
    )
    return controller, fake_panel, fake_service


def _make_recording_panel():
    """构造 fake panel，记录渲染入口 + 提供所有 signal connect。"""
    fake_panel = MagicMock()
    fake_panel.help_answers = []
    fake_panel.clarifications = []
    fake_panel.skill_recs = []
    fake_panel.set_help_answer = lambda a: fake_panel.help_answers.append(a)
    fake_panel.set_clarification = lambda q, s: fake_panel.clarifications.append((q, s))
    fake_panel.set_skill_recommendation = lambda c: fake_panel.skill_recs.append(c)
    fake_panel.set_composer_footer = MagicMock()
    fake_panel.set_run_drawer = MagicMock()
    fake_panel.refresh_state = MagicMock()
    fake_panel.set_pending_brief = MagicMock()
    fake_panel.set_context_bundle = MagicMock()
    fake_panel._bridge = None
    sig_names = (
        "request_submitted", "variant_selected", "apply_requested",
        "cancel_requested", "brief_patch_requested", "context_inspector_requested",
        "composer_scope_requested", "composer_permission_changed",
        "composer_profile_requested", "composer_profile_changed",
        "composer_reasoning_changed", "context_compress_requested",
        "new_session_requested", "show_history_requested",
        "run_cancel_requested", "run_retry_requested", "run_diagnostics_requested",
        "skill_start_requested", "result_action_triggered",
    )
    for n in sig_names:
        getattr(fake_panel, n).connect = MagicMock()
        getattr(fake_panel, n).disconnect = MagicMock()
    return fake_panel


def _submit(controller, text):
    """模拟 panel emit request_submitted。"""
    from src.features.edit.ai_editing.assistant_events import AssistantRequest
    controller._on_request_submitted(AssistantRequest(request_id="r1", text=text))


# ---------------------------------------------------------------------- #
# unknown 分支：已配置 profile → LLM 自由对话
# ---------------------------------------------------------------------- #


def test_unknown_with_profile_calls_llm_and_appends_assistant_reply(
    qapp, monkeypatch,
):
    """unknown + profile 已配置 → make_client 被调 + assistant 回复追加到 messages。"""
    fake_client = _FakeClient(reply="你好！有什么可以帮你的吗？")
    captured = {}

    def _fake_make_client(profile, credential_store):
        captured["profile"] = profile
        captured["credential_store"] = credential_store
        return fake_client

    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client", _fake_make_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    _submit(controller, "你好")

    # make_client 被调（用 profile + credential_store）
    assert captured["profile"].profile_id == "profile-test"
    # LLM 被调一次，且 json_mode=False（普通文本对话）
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["json_mode"] is False
    # assistant 回复已追加到 service 消息列表
    msgs = [m for m in service._messages if m.role == "assistant" and m.text]
    assert any("你好" in m.text for m in msgs), "应看到模型回复"
    # 状态回 IDLE（_set_state 最终调用是 IDLE）
    assert service._set_state.called
    assert service._set_state.call_args_list[-1].args[0] == AssistantState.IDLE


def test_non_multimodal_profile_rejects_file_with_actionable_notice(qapp, monkeypatch, tmp_path):
    """普通文本模型不能悄悄接收附件，必须提示切换多模态模型。"""
    from src.features.edit.ai_editing.assistant_events import AssistantRequest
    from src.features.edit.ai_editing.image_attachment import FileAttachment

    monkeypatch.setenv("TOPOS_TEST_MODE", "1")
    controller, panel, service = _make_p1_controller_with_profile(qapp, _FakeClient())
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    controller._on_request_submitted(AssistantRequest(
        request_id="file-request",
        text="请分析这个视频",
        attachments=(FileAttachment(
            path=str(path), mime="video/mp4", filename="clip.mp4", size_bytes=5,
        ),),
    ))

    notice = panel.set_agent_notice.call_args.args[0]
    assert "不支持多模态" in notice
    assert service._messages == []


def test_isolated_html_generation_keeps_dropped_files_in_model_message(
    qapp, monkeypatch, tmp_path,
):
    """Motion/HTML artifact calls must receive files outside ordinary chat history."""
    from src.features.edit.ai_editing.image_attachment import FileAttachment

    fake_client = _FakeClient(reply="<!doctype html><html><body>ok</body></html>")
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda _profile, _credentials: fake_client,
    )
    controller, _panel, _service = _make_p1_controller_with_profile(qapp, fake_client)
    profile = controller._profile_registry.active_profile
    controller._profile_registry.active_profile = replace(
        profile,
        provider_kind="volcengine_responses",
        modalities=("text", "file"),
        capabilities=("language", "file_input", "document_understanding"),
    )
    first = tmp_path / "前端知识概要.html"
    second = tmp_path / "动态演示PPT.html"
    first.write_text("<h1>HTML</h1>", encoding="utf-8")
    second.write_text("<section class='slide'>CSS</section>", encoding="utf-8")
    controller._pending_chat_attachments = (
        FileAttachment(path=str(first), mime="text/html", filename=first.name),
        FileAttachment(path=str(second), mime="text/html", filename=second.name),
    )

    controller._start_chat(
        "请根据附件制作 Motion Graphic",
        chat_mode="teaching_html_generate",
        system_instruction="只输出完整 HTML",
    )

    assert len(fake_client.calls) == 1
    content = fake_client.calls[0]["messages"][-1]["content"]
    assert [part["type"] for part in content] == ["text", "file", "file"]
    assert content[1]["file_path"] == str(first)
    assert content[2]["file_path"] == str(second)


def test_html_generation_prefers_explicit_language_profile(qapp, monkeypatch):
    """Motion Graphic HTML honors the user's capability-specific model route."""
    active = _make_profile()
    language = replace(
        active,
        profile_id="profile-html-author",
        display_name="HTML Author",
        model_id="deepseek-html-author",
    )
    selected_profiles = []
    fake_client = _FakeClient(reply="<!doctype html><html><body>ok</body></html>")
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda profile, _credentials: (selected_profiles.append(profile) or fake_client),
    )
    controller, _panel, _service = _make_p1_controller_with_profile(qapp, fake_client)
    controller._profile_registry.active_profile = active
    controller._profile_registry.active_profile_for_capability = (
        lambda capability: language if capability == "language" else None
    )

    controller._start_chat(
        "请生成网页",
        chat_mode="teaching_html_generate",
        system_instruction="只输出完整 HTML",
    )

    assert selected_profiles == [language]


def test_unknown_with_profile_appends_user_message_first(qapp, monkeypatch):
    """unknown + profile → user 消息也追加到 messages（供多轮历史）。"""
    fake_client = _FakeClient(reply="收到。")
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda p, c: fake_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    _submit(controller, "帮我看看这个视频")

    user_msgs = [m for m in service._messages if m.role == "user" and m.text]
    assert any("帮我看看这个视频" in m.text for m in user_msgs)


# ---------------------------------------------------------------------- #
# unknown 分支：LLM 调用失败 → 错误消息 + 推荐卡兜底
# ---------------------------------------------------------------------- #


def test_unknown_llm_failure_shows_error_and_recommendation(qapp, monkeypatch):
    """LLM 抛异常 → 追加 error_code=llm_error 的消息 + 仍出推荐卡。"""
    from src.features.edit.ai_editing.llm_client import LLMError

    fake_client = _FakeClient(raise_exc=LLMError("401 Unauthorized"))
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda p, c: fake_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    _submit(controller, "随便聊聊")

    # 错误消息追加（error_code=llm_error）
    err_msgs = [
        m for m in service._messages
        if m.role == "assistant" and m.error_code == "llm_error"
    ]
    assert len(err_msgs) == 1
    assert "401" in err_msgs[0].text or "Unauthorized" in err_msgs[0].text
    # 推荐卡仍出现（兜底）
    assert len(panel.skill_recs) >= 1, "失败时应兜底出推荐卡"
    # 状态回 IDLE（不死锁在 busy 态）
    assert service._set_state.call_args_list[-1].args[0] == AssistantState.IDLE


def test_unknown_empty_reply_shows_placeholder(qapp, monkeypatch):
    """模型返回空字符串 → 显示占位文案（不显示空卡）。"""
    fake_client = _FakeClient(reply="")
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda p, c: fake_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    _submit(controller, "你好")

    asst = [m for m in service._messages if m.role == "assistant" and m.text]
    assert asst, "应有 assistant 消息"
    assert "模型返回为空" in asst[-1].text


# ---------------------------------------------------------------------- #
# 未配置 profile → 仍走推荐卡（行为不变）
# ---------------------------------------------------------------------- #


def test_unknown_without_profile_falls_back_to_recommendation(qapp):
    """未注入 profile_registry / credential_store → unknown 仍走推荐卡（向后兼容）。"""
    from src.app.controllers.ai_edit_controller import AiEditController

    runtime = HarnessRuntime()
    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()
    fake_service.plan_variants = MagicMock()

    fake_panel = _make_recording_panel()
    reg = SkillRegistry()
    register_p2_skills(reg)
    help_assistant = HelpAssistant()
    router = InvocationRouter(
        help_search=help_assistant.entry_ids_for_query,
        skill_resolver=reg.is_enabled,
    )
    catalog = SkillCatalogView(reg)

    # 不注入 profile_registry / credential_store
    controller = AiEditController(
        panel=fake_panel, service=fake_service, runtime=runtime,
        router=router, compiler=SkillCompiler(),
        adapter=LegacyHarnessAdapter(fake_service, runtime),
        help_assistant=help_assistant, catalog=catalog,
        skill_registry=reg, parent=qapp,
    )
    _submit(controller, "随便说点什么unknown")

    assert len(fake_panel.skill_recs) >= 1, "未配置 profile 应走推荐卡"
    # LLM 不应被触发
    assert fake_service.plan_variants.call_count == 0


def test_can_use_llm_chat_gates_on_profile_registry(qapp, monkeypatch):
    """_can_use_llm_chat 在缺 profile_registry / credential_store 时返回 False。"""
    from src.app.controllers.ai_edit_controller import AiEditController

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_panel = _make_recording_panel()

    controller = AiEditController(
        panel=fake_panel, service=fake_service, parent=qapp,
    )
    # 无 registry / store
    assert controller._can_use_llm_chat() is False


# ---------------------------------------------------------------------- #
# 多轮对话历史拼装
# ---------------------------------------------------------------------- #


def test_build_chat_messages_includes_system_and_history(qapp, monkeypatch):
    """_build_chat_messages 包含 system prompt + 最近 user/assistant 历史。"""
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda p, c: fake_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    # 手动塞几条历史到 service
    service.append_chat_message("user", "第一句")
    service.append_chat_message("assistant", "第一答")
    service.append_chat_message("user", "第二句")
    service.append_chat_message("assistant", "第二答")

    messages = controller._build_chat_messages()

    # 第一条是 system
    assert messages[0]["role"] == "system"
    assert "Topos Color" in messages[0]["content"]
    # 之后是历史，按正序
    contents = [m["content"] for m in messages[1:]]
    assert contents == ["第一句", "第一答", "第二句", "第二答"]


def test_chat_context_usage_is_bounded_and_auto_compressed(qapp, monkeypatch):
    """对话超过 profile 上限时，发送上下文自动压缩且 token 数不越界。"""
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda p, c: fake_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, _panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    profile = controller._profile_registry.active_profile
    controller._profile_registry.active_profile = replace(
        profile, limits=ProviderLimits(max_context_tokens=1_024)
    )
    for index in range(18):
        service.append_chat_message("user", f"第 {index} 轮：" + "很长的上下文内容 " * 30)
        service.append_chat_message("assistant", "已收到。" + "补充说明 " * 30)

    messages = controller._build_chat_messages()
    assert controller._chat_context_compressed is True
    assert controller._chat_context_tokens <= 1_024
    assert any("自动摘要" in message["content"] for message in messages)


def test_multi_turn_chat_carries_context(qapp, monkeypatch):
    """连续两轮对话：第二轮发给模型的 messages 应包含第一轮的 user/assistant。"""
    fake_client = _FakeClient(reply="好的。")
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda p, c: fake_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    _submit(controller, "第一轮提问")
    _submit(controller, "第二轮提问")

    # 第二轮调用时，messages 应含第一轮的 user + assistant
    second_call_messages = fake_client.calls[1]["messages"]
    contents = [m["content"] for m in second_call_messages]
    assert "第一轮提问" in contents, "第二轮应带第一轮 user 历史"
    assert "好的。" in contents, "第二轮应带第一轮 assistant 回复"
    assert "第二轮提问" in contents


def test_teaching_scene_edit_chat_uses_json_and_delegates_review(qapp, monkeypatch):
    """复杂场景修改走 JSON 规划，并把结果交回 Teaching Animation 校验。"""
    fake_client = _FakeClient(reply=(
        '{"scene_id":"scene_demo","summary":"移动标题",'
        '"commands":[{"name":"update_element",'
        '"arguments":{"element_id":"title",'
        '"updates":{"transform":{"x":120}}}}]}'
    ))
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda p, c: fake_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    received = []
    controller.set_teaching_scene_edit_request_handler(lambda text: "标题" in text)
    controller.set_teaching_scene_edit_response_handler(
        lambda text, response: received.append((text, response)) or True,
    )
    _submit(controller, "把当前标题向右移动")

    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["json_mode"] is True
    assert received == [("把当前标题向右移动", fake_client._reply)]
    assert any("已读取当前场景上下文" in m.text for m in service._messages)


def test_motion_graphic_request_enters_design_discussion_before_approval(qapp, monkeypatch):
    """Motion Graphic 首轮请求走规划对话，不直接显示执行确认。"""
    fake_client = _FakeClient(reply="可以先确定风格、文案和镜头节奏，再由你决定是否生成。")
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda p, c: fake_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    controller.set_teaching_motion_graphic_discussion_handler(
        lambda text: "Motion Graphic" in text,
    )
    _submit(controller, "帮我创建一个15秒的 Motion Graphic Clip：AI Agent介绍动画")

    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["json_mode"] is False
    system_text = "\n".join(
        message["content"] for message in fake_client.calls[0]["messages"]
        if message["role"] == "system"
    )
    assert "规划讨论阶段" in system_text
    assert "确认按钮" in system_text
    assert "current_timeline" in system_text
    assert "严禁再次询问用户" in system_text
    assert not any("执行确认" in message.text for message in service._messages)


# ---------------------------------------------------------------------- #
# 关键词命中的意图不受影响（回归保护）
# ---------------------------------------------------------------------- #


def test_subtitle_intent_still_routes_to_skill_not_chat(qapp, monkeypatch):
    """'生成字幕' 命中 subtitle 关键词 → 走 skill，不进 LLM chat。"""
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda p, c: fake_client,
    )
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")

    controller, panel, service = _make_p1_controller_with_profile(qapp, fake_client)
    _submit(controller, "给这段视频生成自动字幕")

    # LLM chat 不应被调（走的是 skill 执行路径）
    assert len(fake_client.calls) == 0, "subtitle 意图不应触发 LLM chat"
    # 应产生 run（skill 执行）
    assert controller._active_chat_run_id, "应走 skill 执行产生 run_id"
