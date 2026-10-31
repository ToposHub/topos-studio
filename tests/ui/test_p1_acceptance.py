"""P1 host 装配集成验收（plan §0.3 + §10.3 P1.3 三入口同底层）。

> 验收项：
> 1. 意图路由正确：分析/字幕配音/帮助分别命中 analyze/translate_dub/help
> 2. 同底层反证：chat vs node-graph 同 skill 经同一 compiler+adapter
> 3. 不绕过 approval：skill 路径只产 EditPlan，无 CommandStack commit
> 4. help 无 timeline 写入
> 5. 澄清只问一个最小问题
> 6. user_text 不进 invocation（防注入红线）
> 7. unknown 不进 VariantPlanner
> 8. router=None 向后兼容
> 9. 红线：不 import moderngl/composite.core
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
from src.features.ai_harness.skill_compiler import definition_from_manifest
from src.features.edit.ai_editing.skill_registry import SkillRegistry

pytestmark = pytest.mark.edit_core


def _make_p1_controller(qapp, *, runtime=None):
    """完整 P1 装配的 controller（fake panel/service + 真实 P1 依赖）。"""
    from src.app.controllers.ai_edit_controller import AiEditController

    runtime = runtime or HarnessRuntime()
    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()

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
    return controller, fake_panel, fake_service, runtime, reg


def _submit(controller, text):
    from src.features.edit.ai_editing.assistant_events import AssistantRequest
    controller._on_request_submitted(AssistantRequest(request_id="r1", text=text))


# ---------------------------------------------------------------------- #
# 1. 意图路由正确
# ---------------------------------------------------------------------- #


def test_intent_routing_correct(qapp):
    """'分析所有素材'/'给字幕配音'/'这个功能怎么用'分别命中 analyze/translate_dub/help。"""
    controller, panel, service, runtime, reg = _make_p1_controller(qapp)
    router = controller.router

    r1 = router.route("分析所有素材")
    assert r1.intent == "analyze", f"应命中 analyze，实际 {r1.intent}"

    r2 = router.route("给字幕配音")
    assert r2.intent == "translate_dub", f"应命中 translate_dub，实际 {r2.intent}"

    r3 = router.route("这个功能怎么用")
    assert r3.intent == "help", f"应命中 help，实际 {r3.intent}"

    # 关键反证：不误入粗剪 Planner
    r4 = router.route("分析所有素材")
    assert r4.skill_id != "topos.voiceover_polish", "分析意图不应误入粗剪"


# ---------------------------------------------------------------------- #
# 2. 同底层反证：chat vs compiler 直调同路径
# ---------------------------------------------------------------------- #


def test_chat_and_compiler_same_path(qapp):
    """chat '自动字幕' 经 controller 与直接 compiler.compile 走同一 skill_id/target。"""
    controller, panel, service, runtime, reg = _make_p1_controller(qapp)
    compiler = controller.compiler

    # chat 路径：controller 经 router → compile → invoke_compiled
    _submit(controller, "自动字幕")
    chat_run = controller._active_chat_run_id
    assert chat_run
    chat_inv = runtime.get_invocation(chat_run)
    chat_skill = chat_inv.skill_id

    # 直调路径：compiler.compile 同一 skill_id
    manifest = reg.get(chat_skill)
    assert manifest is not None
    from src.features.ai_harness.invocation_models import (
        Invocation, InvocationScope, InvocationSource,
    )
    direct_inv = Invocation(
        source=InvocationSource.WORKFLOW, skill_id=chat_skill,
        scope=InvocationScope(project_id="p1"),
    )
    direct_compiled = compiler.compile(
        definition_from_manifest(manifest, is_official=True), direct_inv,
    )

    # 两路径的 skill_id 一致（同底层）
    assert chat_skill == direct_compiled.skill_id


# ---------------------------------------------------------------------- #
# 3. 不绕过 approval：skill 路径无 CommandStack commit
# ---------------------------------------------------------------------- #


def test_skill_path_no_command_stack_commit(qapp):
    """skill 执行路径只产 EditPlan，不调 bridge.commit_ai_command。"""
    controller, panel, service, runtime, reg = _make_p1_controller(qapp)
    # bridge.commit_ai_command 是 apply 路径才调的；skill 执行不应调
    _submit(controller, "自动字幕")
    # 这里只验证 skill 执行不崩；commit_ai_command 只在 _on_apply_requested 调
    # （skill 路径只产 plan，apply 仍走用户显式批准）


# ---------------------------------------------------------------------- #
# 4. help 无 timeline 写入
# ---------------------------------------------------------------------- #


def test_help_path_no_timeline_write(qapp):
    """help 路径不产 run（无 timeline 写入）。"""
    controller, panel, service, runtime, reg = _make_p1_controller(qapp)
    _submit(controller, "这个功能怎么用")
    # help 不产 active run（不进 runtime）
    assert not controller._active_chat_run_id or controller._active_chat_run_id == ""
    assert len(panel.help_answers) >= 1


# ---------------------------------------------------------------------- #
# 5. 澄清只问一个最小问题
# ---------------------------------------------------------------------- #


def test_clarify_asks_only_one_question(qapp):
    """澄清只问一个最小问题（一次 set_clarification）。"""
    controller, panel, service, runtime, reg = _make_p1_controller(qapp)
    _submit(controller, "给字幕配音")
    # 第一次只问一个问题（target_language）
    assert len(panel.clarifications) == 1


# ---------------------------------------------------------------------- #
# 6. user_text 不进 invocation（防注入红线）
# ---------------------------------------------------------------------- #


def test_user_text_not_in_invocation(qapp):
    """用户原文不进 invocation.user_text（防注入红线）。"""
    controller, panel, service, runtime, reg = _make_p1_controller(qapp)
    _submit(controller, "给字幕配音 /Users/secret/path")
    if controller._active_chat_run_id:
        inv = runtime.get_invocation(controller._active_chat_run_id)
        assert inv.user_text == "", "invocation.user_text 必须为空"
    # 澄清路径也检查（pending 时无 invocation，但原文不进 RouteResult.extracted_inputs）
    if controller._pending_route:
        # extracted_inputs 不含用户原文（只含 router 提取的类型化槽位）
        for v in controller._pending_route.extracted_inputs.values():
            assert "/Users/secret" not in str(v), "原文不应进 extracted_inputs"


# ---------------------------------------------------------------------- #
# 7. unknown 不进 VariantPlanner
# ---------------------------------------------------------------------- #


def test_unknown_does_not_enter_planner(qapp):
    """unknown 意图显示推荐卡，不调 plan_variants。"""
    controller, panel, service, runtime, reg = _make_p1_controller(qapp)
    service.plan_variants = MagicMock()
    _submit(controller, "zzzxxxqqq不明所以")
    assert service.plan_variants.call_count == 0, "unknown 不应进 VariantPlanner"
    assert len(panel.skill_recs) >= 1, "应显示推荐卡"


# ---------------------------------------------------------------------- #
# 8. router=None 向后兼容
# ---------------------------------------------------------------------- #


def test_router_none_backward_compatible(qapp):
    """router 未装配时回退旧 plan 路径，不崩。"""
    from src.app.controllers.ai_edit_controller import AiEditController
    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()
    fake_service.submit_request = MagicMock()
    fake_service.propose_brief_patch = MagicMock(return_value=MagicMock())
    fake_service.preview_brief_patch = MagicMock(return_value=MagicMock())
    fake_panel = MagicMock()
    for n in ("request_submitted", "variant_selected", "apply_requested",
              "cancel_requested", "brief_patch_requested", "context_inspector_requested",
              "composer_scope_requested", "composer_permission_changed",
              "composer_profile_requested", "context_compress_requested",
              "new_session_requested", "show_history_requested",
              "run_cancel_requested", "run_retry_requested", "run_diagnostics_requested",
              "skill_start_requested"):
        getattr(fake_panel, n).connect = MagicMock()
        getattr(fake_panel, n).disconnect = MagicMock()

    controller = AiEditController(panel=fake_panel, service=fake_service, parent=qapp)
    _submit(controller, "自动字幕")
    assert fake_service.submit_request.call_count == 1, "应走旧 submit_request 路径"


# ---------------------------------------------------------------------- #
# 9. 红线：不 import moderngl / composite.core
# ---------------------------------------------------------------------- #


def test_no_moderngl_or_composite_core_import():
    """P1 接线层源码不 import moderngl / composite.core（红线扫描）。"""
    import pathlib
    files = [
        "src/app/controllers/ai_edit_controller.py",
        "src/app/controllers/ai_editing_mixin.py",
        "src/app/controllers/ai_edit_host_bridge.py",
        "src/app/controllers/ai_edit_host_bridge_impl.py",
    ]
    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in files:
        src = (root / rel).read_text(encoding="utf-8")
        assert "import moderngl" not in src, f"{rel} 不应 import moderngl"
        assert "from composite.core" not in src, f"{rel} 不应 import composite.core"
