"""Autonomous HTML-edit pipeline: dossier → classify → plan → steps → verify.

Edit-flow responses return ``None`` to the controller (the asynchronous
self-driving contract): the pipeline applies directly and manages its own
AgentRun lifecycle, so the chat transport must not add confirmation
bookkeeping on top.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from src.features.teaching_animation.ai.html_edit_autonomy import (
    build_edit_dossier,
    classify_edit_complexity,
    deterministic_minimal_html_edit,
    edit_postcondition_issues,
    explicit_noop_reason,
    generation_intent_report,
    parse_edit_plan,
)
from src.features.teaching_animation.core.html_layer import FreeformHtmlMotionLayer
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.module import TeachingAnimationModule
from src.features.teaching_animation.scene_store import get_shared_scene_store


def _html_scene(scene_id: str, html: str) -> TeachingSceneDocument:
    return replace(
        TeachingSceneDocument.create_default(
            "Autonomy Clip", scene_id=scene_id, width=640, height=360,
            fps_num=25, duration_frames=50,
        ),
        html_layer=FreeformHtmlMotionLayer(
            layer_id=f"layer-{scene_id}",
            sanitized_html=html,
            viewport_width=640,
            viewport_height=360,
            fps_num=25,
            fps_den=1,
            duration_frames=50,
        ),
    )


def _deck(pages: tuple[str, ...]) -> str:
    body = "".join(
        f'<main data-topos-page="{i}">{text}</main>'
        for i, text in enumerate(pages, start=1)
    )
    return f"<!doctype html><html><body>{body}</body></html>"


# ---------------------------------------------------------------------- #
# Unit: explore + classify
# ---------------------------------------------------------------------- #

def test_dossier_reads_pages_and_title() -> None:
    dossier = build_edit_dossier(
        "<!doctype html><html><head><title>发布页</title></head><body>"
        '<main data-topos-page="1"><h1>倒计时</h1>距发布还有 3 天</main>'
        '<main data-topos-page="2">团队介绍</main>'
        '<script>void(0)</script>'
        "</body></html>"
    )
    assert dossier.page_count == 2
    assert dossier.title == "发布页"
    assert "倒计时" in dossier.page_texts[0]
    assert "团队介绍" in dossier.page_texts[1]
    assert dossier.has_inline_script is True
    assert "页数：2" in dossier.summary()


def test_classifier_simple_vs_complex() -> None:
    dossier = build_edit_dossier(_deck(("一", "二")))
    assert classify_edit_complexity("把标题改成「发布倒计时」", dossier)[0] == "simple"
    assert classify_edit_complexity("背景换成 #112233", dossier)[0] == "simple"
    assert classify_edit_complexity(
        "把标题改成 A，然后颜色换成 B，再把副标题改成 C", dossier,
    )[0] == "complex"
    assert classify_edit_complexity("加一页总结", dossier)[0] == "complex"
    assert classify_edit_complexity("整体风格改成赛博朋克", dossier)[0] == "complex"


def test_explicit_noop_reason_is_narrow_and_page_scoped() -> None:
    dossier = build_edit_dossier(_deck(("旧任务看板", "第二页")))
    assert "第 1 页" in explicit_noop_reason(
        "把第 1 页主标题改成「旧任务看板」，如果已经是这个标题则不要改动。", dossier,
    )
    assert explicit_noop_reason(
        "把第 2 页主标题改成「旧任务看板」，如果已经是这个标题则不要改动。", dossier,
    ) == ""
    # No page scope or no explicit conditional wording must remain an LLM edit.
    assert explicit_noop_reason("把标题改成「旧任务看板」", dossier) == ""


def test_deterministic_minimal_edit_changes_only_plain_target_h1() -> None:
    original = (
        "<!doctype html><html><head><style>body{background:#111827}</style></head><body>"
        '<main data-topos-page="1"><h1>旧标题</h1><p>不动</p></main>'
        '<main data-topos-page="2"><h1>第二页</h1></main></body></html>'
    )
    revised = deterministic_minimal_html_edit(
        "只把第 1 页主标题改成「新品发布」，其他页面与视觉不要改变。", original,
    )
    assert revised is not None
    assert revised == original.replace("旧标题", "新品发布", 1)
    # Ambiguous/nested title markup must stay model-driven rather than risk a
    # source-changing host rewrite.
    assert deterministic_minimal_html_edit(
        "只把第 1 页主标题改成「新品发布」，其他页面与视觉不要改变。",
        original.replace("旧标题", "<span>旧标题</span>"),
    ) is None


def test_parse_edit_plan_tolerates_fences_and_caps_steps() -> None:
    plan = parse_edit_plan(
        "好的，计划如下：\n```json\n"
        '{"understanding": "换风格", "issues_found": "无", "steps": ['
        + ",".join(
            f'{{"summary": "步{i}", "instruction": "做{i}"}}' for i in range(1, 7)
        )
        + "]}\n```"
    )
    assert plan is not None
    assert plan.understanding == "换风格"
    assert len(plan.steps) == 4  # capped at MAX_EDIT_PLAN_STEPS
    assert plan.steps[0].instruction == "做1"
    assert parse_edit_plan("直接改就行") is None
    assert parse_edit_plan("") is None
    assert parse_edit_plan('{"steps": [{"summary": "x"}]}') is None


def test_postconditions_from_user_words() -> None:
    original = _deck(("旧文案", "二"))
    revised_ok = _deck(("新品发布", "二"))
    assert edit_postcondition_issues(
        "把标题改成「新品发布」", original, revised_ok,
    ) == []
    issues = edit_postcondition_issues(
        "把标题改成「新品发布」", original, original,
    )
    assert any("新品发布" in issue for issue in issues)
    issues = edit_postcondition_issues(
        "主色换成 #ff8800", original, revised_ok,
    )
    assert any("#ff8800" in issue for issue in issues)
    # Page add/remove intents check the actual page count change.
    assert edit_postcondition_issues("加一页总结", original, revised_ok)
    assert edit_postcondition_issues("加一页总结", original, _deck(("一", "二", "三"))) == []
    assert edit_postcondition_issues(
        "删掉「旧文案」这一页内容", original, _deck(("旧文案", "二")),
    )


def test_postconditions_preserve_explicitly_untargeted_pages_and_visuals() -> None:
    original = (
        "<!doctype html><html><head><style>body{background:#111827}.card{padding:20px}</style>"
        "</head><body><main data-topos-page=\"1\"><h1>旧标题</h1></main>"
        "<main data-topos-page=\"2\"><p>保持原样</p></main></body></html>"
    )
    only_title_changed = original.replace("旧标题", "新品发布")
    request = "只把第 1 页主标题改成「新品发布」，其他页面与视觉不要改变。"
    assert edit_postcondition_issues(request, original, only_title_changed) == []

    restyled = (
        only_title_changed
        .replace("background:#111827", "background:#0b0f19")
        .replace("保持原样", "被意外改写")
    )
    issues = edit_postcondition_issues(request, original, restyled)
    assert any("第 2 页" in issue for issue in issues)
    assert any("样式表" in issue for issue in issues)


def test_generation_intent_report_gates_only_literal_new_deck_requirements() -> None:
    request = "标题写「新品发布」，主色使用 #ff8800，并补充第 4 页总结。"
    complete = (
        "<!doctype html><html><head><style>body{color:#ff8800}</style></head><body>"
        + "".join(
            f'<main data-topos-page="{index}">{"新品发布" if index == 1 else "内容"}</main>'
            for index in range(1, 5)
        )
        + "</body></html>"
    )
    report = generation_intent_report(request, complete)
    assert report.passed is True
    assert report.issues == ()
    assert report.requirements == (
        "包含文案「新品发布」", "使用颜色 #ff8800", "至少 4 页",
    )

    missing = generation_intent_report(request, _deck(("普通标题", "第二页")))
    assert missing.passed is False
    assert any("新品发布" in issue for issue in missing.issues)
    assert any("#ff8800" in issue for issue in missing.issues)
    assert any("第 4 页" in issue for issue in missing.issues)


def test_generation_intent_report_checks_named_palette_and_rich_pages() -> None:
    request = "把蓝色色系改成橙色色系，并让每一页内容更丰富。"
    orange_rich = """<!doctype html><html><head><style>
      body { color: #fff7ed; background: #7c2d12; }
      .grid { display: grid; grid-template-columns: repeat(3, 1fr); }
      .accent { color: #f97316; }
    </style></head><body>
      <main data-topos-page="1"><h1>洞察</h1><p>先理解用户的目标与限制，再把模糊需求拆成可验证的行动。</p>
      <div class="grid"><article>上下文</article><article>目标</article><article>约束</article></div><svg><circle /></svg></main>
      <main data-topos-page="2"><h1>行动</h1><p>调用合适的工具执行任务，并把过程中的新信息及时反馈到下一轮决策。</p>
      <div class="grid"><article>计划</article><article>执行</article><article>复盘</article></div><svg><path /></svg></main>
    </body></html>"""
    report = generation_intent_report(request, orange_rich)
    assert report.passed is True
    assert report.requirements == ("使用橙色色系", "每页信息丰富")

    blue_and_thin = orange_rich.replace("#7c2d12", "#172554").replace(
        "#f97316", "#2563eb",
    ).replace(
        "<p>先理解用户的目标与限制，再把模糊需求拆成可验证的行动。</p>",
        '<p>一句话</p>',
    ).replace(
        '<div class="grid"><article>上下文</article><article>目标</article><article>约束</article></div><svg><circle /></svg>',
        "",
    )
    missing = generation_intent_report(request, blue_and_thin)
    assert any("橙色色系" in issue for issue in missing.issues)
    assert any("第 1 页信息密度" in issue for issue in missing.issues)


# ---------------------------------------------------------------------- #
# Flow: planner → steps → apply (module level, fake host)
# ---------------------------------------------------------------------- #

class _Controller:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.completed: list[tuple[str, bool]] = []

    def start_html_generate_chat(self, request_id, user_prompt, **kwargs):
        self.calls.append({
            "request_id": request_id,
            "prompt": user_prompt,
            "system": str(kwargs.get("system_instruction", "")),
            "temperature": kwargs.get("temperature"),
        })
        return True

    def complete_motion_graphic_run(self, request_id, *, success=True):
        self.completed.append((str(request_id), bool(success)))


class _Panel:
    def __init__(self) -> None:
        self.proposals = []
        self.statuses: list[tuple] = []

    def set_motion_graphic_proposal(self, *args) -> None:
        self.proposals.append(args)

    def set_activity_status(self, text, *, detail="", active=True, status="running"):
        self.statuses.append((text, detail, active, status))


class _Service:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def append_chat_message(self, role, text, **_kwargs) -> None:
        self.messages.append((role, text))


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, text, timeout=0):
        self.messages.append(str(text))


def _window(controller, panel, service, scene_id, with_status_bar=True):
    window = SimpleNamespace(
        _current_edit_project=SimpleNamespace(assets={
            f"teaching_animation:{scene_id}": SimpleNamespace(
                asset_id=f"teaching_animation:{scene_id}",
                display_name="自主修改网页", media_type="motion_graphic",
                metadata={"scene_id": scene_id},
            ),
        }),
        _ai_edit_controller=controller,
        ai_edit_assistant_panel=panel,
        _ai_assistant_service=service,
    )
    if with_status_bar:
        window.status_bar = _StatusBar()
    return window


def _queue_edit(module, window, scene_id, text):
    assert module._queue_motion_graphic_edit(
        window, text, (f"teaching_animation:{scene_id}",),
    )
    return window._ai_edit_controller.calls[-1]["request_id"]


def _respond(module, window, request_id, response) -> None:
    handled = module._handle_model_html_generate_response(
        window, request_id, response,
    )
    assert handled is None, "edit-flow responses are self-driving (None contract)"


def _status_texts(panel: _Panel) -> list[str]:
    return [text for text, _detail, _active, _status in panel.statuses]


def test_explicitly_satisfied_edit_skips_model_rewrite(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    original = _deck(("旧任务看板", "第二页"))
    store.register(_html_scene("scene-noop", original))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-noop")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        assert module._queue_motion_graphic_edit(
            window,
            "把第 1 页主标题改成「旧任务看板」，如果已经是这个标题则不要改动。",
            ("teaching_animation:scene-noop",),
        )
        assert controller.calls == []
        assert store.get("scene-noop").html_layer.sanitized_html == original
        assert any("没有发起模型重写" in text for _, text in service.messages)
        assert panel.statuses[-1][0] == "无需修改"
    finally:
        store.clear()


def test_targeted_edit_carries_prior_clip_feedback(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-memory", _deck(("旧标题",))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-memory")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        module._record_user_feedback(
            "此前要求：保持留白并避免拥挤。",
            artifact_refs=("teaching_animation:scene-memory", "scene-memory"),
        )
        request_id = _queue_edit(
            module, window, "scene-memory", "把标题改成「新品发布」",
        )
        assert request_id
        assert "保持留白并避免拥挤" in controller.calls[0]["prompt"]
        _respond(module, window, request_id, _deck(("新品发布",)))
        assert any("本步差异" in text for _, text in service.messages)
    finally:
        store.clear()


def test_applied_edit_persists_clip_feedback_for_a_reopened_module(monkeypatch) -> None:
    """A saved layer, not the process-local store, carries critique forward."""
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-persist-memory", _deck(("旧标题",))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-persist-memory")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        first_request_id = _queue_edit(
            module, window, "scene-persist-memory",
            "把标题改成「新品发布」，保持大量留白。",
        )
        _respond(module, window, first_request_id, _deck(("新品发布",)))
        saved_layer = store.get("scene-persist-memory").html_layer
        memory_key = TeachingAnimationModule._HTML_EDIT_FEEDBACK_MEMORY_KEY
        assert "保持大量留白" in saved_layer.metadata[memory_key][0]

        # Simulate closing/reopening the assistant: it owns an empty runtime
        # FeedbackStore and can only recover the preference from the scene.
        reopened_module = TeachingAnimationModule()
        second_request_id = _queue_edit(
            reopened_module, window, "scene-persist-memory",
            "把第 1 页副标题改成「面向团队的智能协作」",
        )
        assert second_request_id
        assert "保持大量留白" in controller.calls[-1]["prompt"]
    finally:
        store.clear()


def test_exact_title_preservation_uses_verified_minimal_patch(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    original = (
        "<!doctype html><html><head><style>body{background:#111827}</style></head><body>"
        '<main data-topos-page="1"><h1>旧标题</h1></main>'
        '<main data-topos-page="2">第二页保持不变</main></body></html>'
    )
    store.register(_html_scene("scene-exact-patch", original))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-exact-patch")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        assert module._queue_motion_graphic_edit(
            window,
            "只把第 1 页主标题改成「新品发布」，其他页面与视觉不要改变。",
            ("teaching_animation:scene-exact-patch",),
        )
        final = store.get("scene-exact-patch").html_layer.sanitized_html
        assert controller.calls == []
        assert "新品发布" in final
        assert "第二页保持不变" in final
        assert "background:#111827" in final
        assert any("不重写其他页面或样式" in text for _, text in service.messages)
    finally:
        store.clear()


def test_complex_edit_planner_collapses_to_single_step(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-plan", _deck(("旧标题", "二"))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-plan")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-plan", "整体风格改成赛博朋克",
        )
        # Planner call first: cool temperature, planner system prompt, dossier.
        first = controller.calls[0]
        assert first["temperature"] == 0.3
        assert "规划" in first["system"]
        assert "页数：2" in first["prompt"]
        # The planner says one step is enough.
        plan_json = (
            '{"understanding": "整套换成赛博朋克", "issues_found": "风格当前偏简约", '
            '"steps": [{"summary": "全站换风格", '
            '"instruction": "两页统一改成赛博朋克配色与霓虹标题"}]}'
        )
        _respond(module, window, request_id, plan_json)
        assert any("一次改完" in text for _, text in service.messages)
        assert "网页设计模型" in controller.calls[1]["system"]
        assert "赛博朋克配色与霓虹标题" in controller.calls[1]["prompt"]
        # Execute the collapsed step: applied directly, no card.
        _respond(module, window, request_id, _deck(("霓虹标题", "霓虹第二页")))
        assert panel.proposals == []
        assert "霓虹标题" in store.get("scene-plan").html_layer.sanitized_html
        assert request_id not in module._pending_html_generate_requests
        # The run lifecycle is retired with success.
        assert controller.completed and controller.completed[-1] == (request_id, True)
    finally:
        store.clear()


def test_complex_edit_multi_step_executes_in_order(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-steps", _deck(("旧一", "旧二"))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-steps")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-steps", "整体风格改成杂志风，然后加一页总结",
        )
        plan_json = (
            '{"understanding": "改版", "issues_found": "无", "steps": ['
            '{"summary": "换风格", "instruction": "前两页统一改成杂志风排版"}, '
            '{"summary": "加总结页", "instruction": "新增第三页作为总结页"}'
            "]}"
        )
        _respond(module, window, request_id, plan_json)
        assert any("2 步修改计划" in text for _, text in service.messages)

        # Step 1 prompt is scoped to its instruction only.
        step1 = controller.calls[1]
        assert "第 1/2 步" in step1["prompt"]
        assert "杂志风排版" in step1["prompt"]

        _respond(module, window, request_id, _deck(("杂志风一", "杂志风二")))
        applied = store.get("scene-steps").html_layer.sanitized_html
        assert "杂志风一" in applied

        # Step 2 is auto-continued with the step-1 result as its base.
        step2 = controller.calls[-1]
        assert "第 2/2 步" in step2["prompt"]
        assert "总结页" in step2["prompt"]
        assert "杂志风一" in step2["prompt"]

        _respond(module, window, request_id, _deck(("杂志风一", "杂志风二", "总结")))
        final = store.get("scene-steps").html_layer.sanitized_html
        assert "总结" in final
        assert any("分步修改全部完成" in text for _, text in service.messages)
        assert request_id not in module._pending_html_generate_requests
    finally:
        store.clear()


def test_planner_junk_falls_back_to_single_shot(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-fallback", _deck(("旧",))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-fallback")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-fallback", "重新设计成极简风",
        )
        _respond(module, window, request_id, "我直接帮你改吧")
        assert any("一次性直接修改" in text for _, text in service.messages)
        # The fallback call is a normal revision prompt carrying the user text.
        fallback = controller.calls[-1]
        assert "重新设计成极简风" in fallback["prompt"]
        assert "规划" not in fallback["system"]
        _respond(module, window, request_id, _deck(("极简内容",)))
        assert "极简内容" in store.get("scene-fallback").html_layer.sanitized_html
    finally:
        store.clear()


def test_postcondition_violation_retries_then_refuses(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-post", _deck(("旧标题", "二"))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-post")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-post", "把标题改成「新品发布」",
        )
        # First attempt drops the requested copy: one diagnosed retry.
        _respond(module, window, request_id, _deck(("还是旧标题", "二")))
        assert len(controller.calls) == 2
        assert "「新品发布」没有出现在修改后的网页中" in controller.calls[1]["prompt"]
        # Second attempt still misses: refused, scene untouched, run failed.
        _respond(module, window, request_id, _deck(("依旧没改", "二")))
        assert "旧标题" in store.get("scene-post").html_layer.sanitized_html
        assert any("not applied" in text for _, text in service.messages)
        assert request_id not in module._pending_html_generate_requests
        assert controller.completed[-1] == (request_id, False)
    finally:
        store.clear()


def test_explicit_preservation_violation_retries_with_precise_diagnosis(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    original = (
        "<!doctype html><html><head><style>body{background:#111827}</style></head><body>"
        '<main data-topos-page="1"><h1>旧标题</h1></main>'
        '<main data-topos-page="2">第二页保持不变</main></body></html>'
    )
    store.register(_html_scene("scene-preserve", original))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-preserve")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-preserve",
            "把第 1 页主标题改成「新品发布」，其他页面与视觉不要改变。",
        )
        # The requested title exists, but the model changed page 2 and global
        # CSS, so the preservation postcondition must trigger a repair pass.
        _respond(module, window, request_id, (
            original.replace("旧标题", "新品发布")
            .replace("#111827", "#0b0f19")
            .replace("第二页保持不变", "第二页被重写")
        ))
        assert len(controller.calls) == 2
        repair_prompt = controller.calls[-1]["prompt"]
        assert "第 2 页" in repair_prompt
        assert "样式表" in repair_prompt

        _respond(module, window, request_id, original.replace("旧标题", "新品发布"))
        assert "新品发布" in store.get("scene-preserve").html_layer.sanitized_html
        assert request_id not in module._pending_html_generate_requests
    finally:
        store.clear()


def test_late_step_failure_keeps_prior_steps_and_reports_scope(monkeypatch) -> None:
    """A planned edit must not pretend earlier committed work was rolled back."""
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-partial", _deck(("旧一", "旧二"))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-partial")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-partial", "整体改成霓虹风，然后加一页总结",
        )
        _respond(module, window, request_id, (
            '{"understanding":"改版","issues_found":"无","steps":['
            '{"summary":"换风格","instruction":"前两页改成霓虹风"},'
            '{"summary":"加总结","instruction":"新增第三页总结"}]}'
        ))
        _respond(module, window, request_id, _deck(("霓虹一", "霓虹二")))
        # Step 2 fails the page-count intent twice.  Step 1 has already been
        # written, so the failure must keep it and say exactly where it stopped.
        _respond(module, window, request_id, _deck(("霓虹一", "霓虹二")))
        _respond(module, window, request_id, _deck(("霓虹一", "霓虹二")))
        final = store.get("scene-partial").html_layer.sanitized_html
        assert "霓虹一" in final
        assert request_id not in module._pending_html_generate_requests
        messages = [text for _, text in service.messages]
        assert any("第 2/2 步停止" in text and "前 1 步已经写入" in text for text in messages)
        assert controller.completed[-1] == (request_id, False)
    finally:
        store.clear()


def test_empty_step_retries_then_clears_pending_request(monkeypatch) -> None:
    """An empty model answer follows the same bounded repair contract."""
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-empty", _deck(("旧标题",))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-empty")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-empty", "把标题改成「新品发布」",
        )
        _respond(module, window, request_id, "")
        assert len(controller.calls) == 2
        _respond(module, window, request_id, "")
        assert request_id not in module._pending_html_generate_requests
        assert "旧标题" in store.get("scene-empty").html_layer.sanitized_html
        assert controller.completed[-1] == (request_id, False)
        assert any("returned no content" in text for _, text in service.messages)
    finally:
        store.clear()


def test_browser_validation_failure_retries_once_then_stops(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-browser", _deck(("旧标题",))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-browser")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    # AUD-28005: the confirm path may finish its browser gate off-thread and
    # reports the final outcome through ``completion``.  The stub refuses and
    # delivers that refusal synchronously, like the real synchronous path.
    def _refuse_confirm(_window, _request_id, completion=None):
        if completion is not None:
            completion(False)
        return False

    monkeypatch.setattr(module, "_confirm_html_generate_request", _refuse_confirm)

    try:
        request_id = _queue_edit(
            module, window, "scene-browser", "把标题改成「新品发布」",
        )
        _respond(module, window, request_id, _deck(("新品发布",)))
        assert len(controller.calls) == 2
        assert "真实浏览器逐页验证没有通过" in controller.calls[-1]["prompt"]
        _respond(module, window, request_id, _deck(("新品发布",)))
        assert request_id not in module._pending_html_generate_requests
        assert "旧标题" in store.get("scene-browser").html_layer.sanitized_html
        assert controller.completed[-1] == (request_id, False)
        assert panel.statuses[-1][0] == "浏览器验证失败"
    finally:
        store.clear()


# ---------------------------------------------------------------------- #
# Bottom status display: every phase is visible to the user
# ---------------------------------------------------------------------- #

def test_simple_edit_publishes_phase_statuses(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-status", _deck(("旧标题", "二"))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-status")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-status", "把标题改成「新品发布」",
        )
        _respond(
            module, window, request_id, _deck(("新品发布", "第二页")),
        )
        texts = _status_texts(panel)
        assert any(t.startswith("修改「自主修改网页」") for t in texts)
        assert "正在修改网页" in texts
        assert "正在验证修改" in texts
        assert texts[-1] == "修改已完成"
        # Terminal status is not active.
        last = panel.statuses[-1]
        assert last[2] is False and last[3] == "succeeded"
        # The main-window status bar mirrors the same phases.
        assert any("修改「自主修改网页」" in m for m in window.status_bar.messages)
        assert "修改已完成" in window.status_bar.messages[-1]
    finally:
        store.clear()


def test_complex_edit_publishes_planner_and_step_statuses(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-status2", _deck(("旧一", "旧二"))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-status2")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-status2", "整体风格改成杂志风，然后加一页总结",
        )
        texts = _status_texts(panel)
        assert "正在制定分步修改计划" in texts
        plan_json = (
            '{"understanding": "改版", "issues_found": "无", "steps": ['
            '{"summary": "换风格", "instruction": "改成杂志风"}, '
            '{"summary": "加总结页", "instruction": "加一页总结"}'
            "]}"
        )
        _respond(module, window, request_id, plan_json)
        _respond(module, window, request_id, _deck(("杂志风一", "杂志风二")))
        details = [detail for _t, detail, _a, _s in panel.statuses]
        assert any("第 1/2 步" in d for d in details)
        assert any("第 2/2 步" in d for d in details)
        _respond(module, window, request_id, _deck(("杂志风一", "杂志风二", "总结")))
        assert panel.statuses[-1][0] == "修改已完成"
    finally:
        store.clear()


def test_refused_edit_publishes_failed_status(monkeypatch) -> None:
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-status3", _deck(("旧标题", "二"))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-status3")
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-status3", "把标题改成「新品发布」",
        )
        _respond(module, window, request_id, _deck(("还是旧标题", "二")))
        _respond(module, window, request_id, _deck(("依旧没改", "二")))
        last = panel.statuses[-1]
        assert last[0] == "Edit not applied"
        assert last[2] is False and last[3] == "failed"
    finally:
        store.clear()


def test_status_display_never_requires_a_status_bar(monkeypatch) -> None:
    """Hosts without a panel/status bar still run the pipeline fine."""
    store = get_shared_scene_store()
    store.clear()
    store.register(_html_scene("scene-nobar", _deck(("旧标题",))))
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window(controller, panel, service, "scene-nobar", with_status_bar=False)
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

    try:
        request_id = _queue_edit(
            module, window, "scene-nobar", "把标题改成「新品发布」",
        )
        _respond(module, window, request_id, _deck(("新品发布",)))
        assert "新品发布" in store.get("scene-nobar").html_layer.sanitized_html
    finally:
        store.clear()
