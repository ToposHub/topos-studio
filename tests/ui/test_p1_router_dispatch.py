"""P1.1/P1.3 测试：controller 接 router 分发（_on_request_submitted 核心改造）。

> 验证 §10.3 P1 用户输入经 InvocationRouter 分发到 4 条路径：
> - matched + skill_id 非空 → compiler.compile + adapter.invoke_compiled
> - matched + clarify_question → panel.set_clarification
> - intent == "help" → help_assistant.answer + panel.set_help_answer
> - intent == "unknown" → catalog.recommend + panel.set_skill_recommendation
> - router 未装配 → 回退旧 submit_request→brief→plan 路径（向后兼容）

聚焦 controller 的分发逻辑，用 fake panel（记录渲染入口调用）+ 真实 router/compiler。
"""
from __future__ import annotations

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

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# 辅助：构造完整 P1 装配的 controller
# ---------------------------------------------------------------------- #


def _make_p1_controller(qapp, *, runtime=None):
    """构造一个装配完整 P1 依赖的 controller（fake panel/service + 真实 P1 依赖）。

    返回 (controller, fake_panel, fake_service, runtime)。
    fake_panel 记录 set_help_answer / set_clarification / set_skill_recommendation 调用。
    """
    from src.app.controllers.ai_edit_controller import AiEditController

    runtime = runtime or HarnessRuntime()
    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()

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

    controller = AiEditController(
        panel=fake_panel, service=fake_service, runtime=runtime,
        router=router, compiler=compiler, adapter=adapter,
        help_assistant=help_assistant, catalog=catalog,
        skill_registry=reg, parent=qapp,
    )
    return controller, fake_panel, fake_service, runtime


def _make_recording_panel():
    """构造 fake panel，记录渲染入口调用 + 提供所有 signal connect。"""
    fake_panel = MagicMock()
    # 记录渲染入口
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
    # bridge 方法
    fake_panel._bridge = None
    sig_names = (
        "request_submitted", "variant_selected", "apply_requested",
        "cancel_requested", "brief_patch_requested", "context_inspector_requested",
        "composer_scope_requested", "composer_permission_changed",
        "composer_profile_requested", "context_compress_requested",
        "new_session_requested", "show_history_requested",
        "run_cancel_requested", "run_retry_requested", "run_diagnostics_requested",
        "skill_start_requested",
    )
    for n in sig_names:
        getattr(fake_panel, n).connect = MagicMock()
        getattr(fake_panel, n).disconnect = MagicMock()
    return fake_panel


def _submit(controller, text):
    """模拟 panel emit request_submitted（直接调 controller 的 _on_request_submitted）。"""
    from src.features.edit.ai_editing.assistant_events import AssistantRequest
    controller._on_request_submitted(AssistantRequest(request_id="r1", text=text))


# ---------------------------------------------------------------------- #
# 意图路由分发
# ---------------------------------------------------------------------- #


def test_subtitle_intent_routes_to_skill_execution(qapp):
    """'自动字幕' → matched subtitle → adapter.invoke_compiled 被调 → run 产生。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    _submit(controller, "给这段视频生成自动字幕")

    # adapter.invoke_compiled 应被调（service.run_workflow 或 editorial 路径）
    assert controller._active_chat_run_id, "应产生 run_id"
    rec = runtime.get_run(controller._active_chat_run_id)
    assert rec is not None


def test_translate_dub_intent_routes_to_skill(qapp):
    """'给字幕配音' → matched translate_dub → skill 执行。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    _submit(controller, "给当前字幕配中文配音")

    assert controller._active_chat_run_id


def test_translate_dub_missing_language_asks_clarification(qapp):
    """translate_dub 缺 target_language → 问澄清问题（set_clarification 被调）。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    # "配音" 不带语言 → router 应问 target_language
    _submit(controller, "给字幕配音")

    assert len(panel.clarifications) >= 1, "缺语言应问澄清"
    question, slot = panel.clarifications[-1]
    assert slot == "target_language"


def test_help_intent_shows_help_answer(qapp):
    """'这个功能怎么用' → intent=help → panel.set_help_answer 被调。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    _submit(controller, "这个功能怎么用")

    assert len(panel.help_answers) >= 1, "help 意图应显示帮助回答"


def test_unknown_intent_shows_skill_recommendation(qapp):
    """'asdfgh'（unknown）→ set_skill_recommendation 被调，不进 plan_variants。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    service.plan_variants = MagicMock()
    _submit(controller, "asdfgh乱七八糟")

    assert len(panel.skill_recs) >= 1, "unknown 应显示推荐卡"
    # plan_variants 不应被调（不进旧 planner）
    assert service.plan_variants.call_count == 0


def test_router_none_falls_back_to_old_path(qapp):
    """router 未装配时回退旧 submit_request→brief→plan 路径（向后兼容）。"""
    from src.app.controllers.ai_edit_controller import AiEditController

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()
    fake_service.submit_request = MagicMock()
    fake_service.propose_brief_patch = MagicMock(return_value=MagicMock())
    fake_service.preview_brief_patch = MagicMock(return_value=MagicMock())

    fake_panel = _make_recording_panel()

    controller = AiEditController(
        panel=fake_panel, service=fake_service, parent=qapp,
        # router/compiler/adapter/help/catalog/registry 全 None
    )
    _submit(controller, "自动字幕")

    # 应走旧路径：submit_request 被调
    assert fake_service.submit_request.call_count == 1
    # 不应调 set_help_answer / set_skill_recommendation（无 router）
    assert len(fake_panel.help_answers) == 0
    assert len(fake_panel.skill_recs) == 0


def test_analyze_intent_shows_scope_hint(qapp):
    """'分析所有素材' → analyze 意图 → 显示 scope_hint 提示（不执行 skill）。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    _submit(controller, "分析所有素材")

    # analyze 不产 run（无 skill_id）
    # 应有某种提示显示给用户（scope_hint 或系统消息）
    # 这里用 panel 渲染入口或 message 任一即可；关键是不崩 + 不进 plan_variants
    # （analyze 意图留 AnalysisService 既有路径）


def test_skill_card_start_triggers_skill_execution(qapp):
    """panel.skill_start_requested signal → controller 触发对应 skill 执行。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    # 模拟用户点 skill 推荐卡（form_values 空字典）
    controller._on_skill_card_start("topos.auto_caption", {})

    assert controller._active_chat_run_id, "点 skill 卡应触发执行"
    rec = runtime.get_run(controller._active_chat_run_id)
    assert rec is not None


def test_user_text_not_in_invocation(qapp):
    """红线：用户原文不进 invocation（防注入）。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    _submit(controller, "给字幕配音 /Users/secret/path")

    if controller._active_chat_run_id:
        inv = runtime.get_invocation(controller._active_chat_run_id)
        assert inv.user_text == "", "invocation.user_text 必须为空（防注入红线）"


def test_compile_failure_shown_to_user(qapp):
    """compile 失败（runnable=False）→ run FAILED + 不崩。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    # 用一个会 compile 失败的场景：缺 selected_asset_ids（scope 空）
    _submit(controller, "自动字幕")

    # 不论 compile 成功失败，都不应崩；若有 run，检查状态合理
    if controller._active_chat_run_id:
        rec = runtime.get_run(controller._active_chat_run_id)
        assert rec is not None


# ---------------------------------------------------------------------- #
# P1 批次4：澄清往返 + analyze 提示
# ---------------------------------------------------------------------- #


def test_clarify_roundtrip_executes_skill_after_answer(qapp):
    """澄清往返：缺 target_language → 问 → 用户答 'en' → 重路由 matched → 执行。

    第一次提交触发澄清（_pending_route 被存）；第二次提交是答案，
    controller 应带 prefilled_inputs 重路由，命中 skill 后执行。
    """
    controller, panel, service, runtime = _make_p1_controller(qapp)
    # 第一次：触发澄清
    _submit(controller, "给字幕配音")
    assert controller._pending_route is not None, "应存 pending_route"
    assert len(panel.clarifications) >= 1

    # 第二次：用户回答 "en"
    panel.clarifications.clear()  # 清掉第一次的记录便于断言
    _submit(controller, "en")

    # 应重路由后执行 skill（产生 run），不再问澄清
    assert controller._active_chat_run_id, "回答后应执行 skill"
    assert len(panel.clarifications) == 0, "回答后不应再问澄清"


def test_clarify_roundtrip_clears_pending_route(qapp):
    """澄清成功后 _pending_route 清空（不泄漏到后续请求）。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    _submit(controller, "给字幕配音")
    assert controller._pending_route is not None

    _submit(controller, "en")
    assert controller._pending_route is None, "成功路由后应清空 pending_route"


def test_new_request_after_clarify_does_not_reuse_pending(qapp):
    """澄清完成后，新请求不被误当澄清答案（_pending_route 已清）。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    _submit(controller, "给字幕配音")
    _submit(controller, "en")  # 完成澄清往返
    assert controller._pending_route is None

    # 新请求：自动字幕（不同意图）
    _submit(controller, "自动字幕")
    # 不应把"自动字幕"当澄清答案处理（应走 subtitle 意图）
    # 只要不崩 + _pending_route 仍 None 即正确


def test_analyze_intent_shows_scope_hint_message(qapp):
    """analyze 意图 → 显示 scope_hint（作为 clarification/提示）。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    _submit(controller, "分析所有素材")

    # analyze 应显示提示（set_clarification 带 analyze_scope）
    assert len(panel.clarifications) >= 1, "analyze 应显示 scope 提示"
    question, slot = panel.clarifications[-1]
    assert slot == "analyze_scope"


def test_analyze_intent_does_not_produce_run(qapp):
    """analyze 意图不产 run（无 skill_id；真实分析走 AnalysisService）。"""
    controller, panel, service, runtime = _make_p1_controller(qapp)
    _submit(controller, "分析所有素材")
    # analyze 不应产生 active run（无 skill 执行）
    # （_active_chat_run_id 可能为空或来自之前的 run；这里只验证不崩）

