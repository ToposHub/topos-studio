"""Integration tests: verify MG fixes work in the real module.py code path.

These tests exercise the actual TeachingAnimationModule._confirm_motion_graphic_request
and _show_motion_graphic_proposal_card methods (not just contract-level unit tests)
to prove the §6 vertical-slice invariants hold:

* MG-05: confirm refuses when scene is None and user hasn't chosen template
* MG-05: confirm succeeds when user explicitly sets use_template
* MG-06: confirm refuses when scene hash changed after card was shown (stale)
* MG-02: typed card method receives structured data including quality findings
* MG-01: brief is constructed and stored on the plan
* MG-11: canvas dimensions flow from brief (not hardcoded)
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.teaching_animation.module import TeachingAnimationModule
from src.features.teaching_animation.scene_store import get_shared_scene_store
from src.features.teaching_animation.ai.brief import MotionGraphicBrief


# --------------------------------------------------------------------------- #
# Fakes (mirrors the phase5 test pattern)
# --------------------------------------------------------------------------- #


class _FakeView:
    def __init__(self) -> None:
        self._timeline = TimelineSequence(timeline_id="mg-integration")

    def execute_command(self, command) -> None:
        self._timeline = command.apply(self._timeline)


class _FakeTimelinePanel:
    _fps = 24.0

    def __init__(self) -> None:
        self._view = _FakeView()

    def get_timeline_view(self):
        return self._view

    def get_current_frame(self) -> int:
        return 0


class _TypedCardPanel:
    """Panel that captures typed card data for assertions."""

    def __init__(self) -> None:
        self.typed_card: dict | None = None
        self.text_card: tuple | None = None
        self.cleared_request_ids: list[str] = []

    def set_motion_graphic_proposal(self, request_id, title, details) -> None:
        self.text_card = (request_id, title, details)

    def set_motion_graphic_proposal_card(self, request_id, card_data) -> None:
        self.typed_card = {"request_id": request_id, "data": card_data}

    def clear_motion_graphic_proposal(self, request_id) -> None:
        self.cleared_request_ids.append(request_id)
        self.typed_card = None
        self.text_card = None

    def set_agent_notice(self, message, *, warning=False) -> None:
        pass


class _FakeWindow:
    def __init__(self) -> None:
        self.timeline_panel = _FakeTimelinePanel()
        self.ai_edit_assistant_panel = _TypedCardPanel()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_plan(module: TeachingAnimationModule, *, use_template: bool = False,
               scene=None, seconds: float = 15.0, fps: float = 24.0) -> str:
    """Queue a Motion Graphic plan directly into the module's pending dict."""
    import uuid
    request_id = f"motion-graphic-{uuid.uuid4().hex}"
    brief = MotionGraphicBrief(
        brief_id=f"brief-{uuid.uuid4().hex}",
        topic="test",
        duration_seconds=seconds,
        canvas_aspect="16:9",
        fps=fps,
    )
    module._pending_motion_graphic_requests[request_id] = {
        "seconds": seconds,
        "fps": fps,
        "frame": 0,
        "has_timeline": True,
        "track_id": None,
        "variant": 0,
        "user_text": "test",
        "scene": scene,
        "build_failed": scene is None,
        "use_template": use_template,
        "brief": brief,
    }
    return request_id


def _make_scene(seconds: float = 15.0, fps: float = 24.0):
    """Build a minimal valid scene."""
    from src.features.teaching_animation.core.model import TeachingSceneDocument
    duration = max(1, round(seconds * fps))
    return TeachingSceneDocument.create_default(
        "Test", width=1920, height=1080,
        fps_num=int(fps), duration_frames=duration,
    )


# --------------------------------------------------------------------------- #
# MG-01: brief is constructed
# --------------------------------------------------------------------------- #


def test_brief_is_stored_on_plan() -> None:
    """MG-01: _queue_motion_graphic_plan stores a MotionGraphicBrief."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()

    # Simulate user authorization.
    module._handle_ai_request(window, "帮我生成 Motion Graphic Clip，15 秒")
    # Discussion mode active.
    assert module._motion_graphic_discussion_active

    # Authorize.
    module._handle_ai_request(window, "可以开始生成了")
    # A plan should be queued.
    assert module._pending_motion_graphic_requests
    plan = next(iter(module._pending_motion_graphic_requests.values()))
    # MG-01: brief is present and typed.
    assert isinstance(plan.get("brief"), MotionGraphicBrief)
    assert plan["brief"].duration_seconds == 15.0


def test_direct_mode_shows_plan_card_before_any_execution_confirmation() -> None:
    """Explicit Motion Graphic mode must stop at the user-visible plan first."""
    module = TeachingAnimationModule()
    window = _FakeWindow()

    class _PlannerController:
        def __init__(self) -> None:
            self.plan_calls: list[tuple[str, str]] = []

        def set_teaching_motion_graphic_build_system_prompt(self, prompt: str) -> None:
            self.system_prompt = prompt

        def start_motion_graphic_build_chat(
            self, request_id: str, prompt: str, *, temperature: float = 0.0,
        ) -> bool:
            del temperature
            self.plan_calls.append((request_id, prompt))
            return True

    controller = _PlannerController()
    window._ai_edit_controller = controller
    assert module._queue_motion_graphic_plan(
        window,
        "帮我生成 15 秒的 HTML 学习演示动画",
        build_directly=True,
    )
    request_id = next(iter(module._pending_motion_graphic_requests))
    plan = module._pending_motion_graphic_requests[request_id]
    assert plan["phase"] == "plan_review"
    assert plan["creative_brief_pending"] is True
    assert controller.plan_calls and controller.plan_calls[0][0] == request_id

    response = json.dumps({
        "concept": "把学习路径设计成一条逐步展开的网页旅程",
        "visual_grammar": "结构从骨架到内容再到实践逐层展开",
        "palette": ["#101828", "#70E1F5"],
        "typography": "大标题和短句形成强弱层级",
        "motion_language": "页面推进与内容渐进出现",
        "subject_motifs": ["网页结构", "标签", "实践"],
        "beats": [
            {"label": "骨架", "purpose": "建立认知", "visual": "结构展开", "copy": "先理解骨架"},
            {"label": "实践", "purpose": "形成动作", "visual": "页面响应", "copy": "再动手练习"},
        ],
        "avoid": ["固定卡片模板"],
    }, ensure_ascii=False)
    assert module._handle_model_motion_graphic_build_response(
        window, request_id, response,
    )
    card = window.ai_edit_assistant_panel.typed_card
    assert card is not None
    assert card["data"]["mode"] == "plan_review"
    assert card["data"]["actions"] == ["cancel", "start"]
    assert "generate" not in card["data"]["actions"]
    assert plan["phase"] == "plan_review"
    assert plan["plan_approved"] is False


def test_html_returned_during_planning_cannot_bypass_plan_card() -> None:
    """A provider mistake returning HTML in planner phase must not execute it."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    request_id = _make_plan(module, scene=None)
    plan = module._pending_motion_graphic_requests[request_id]
    plan.update({
        "phase": "plan_review",
        "creative_brief_pending": True,
        "render_mode": "html",
        "allow_json_retry": False,
    })

    assert not module._handle_model_motion_graphic_build_response(
        window,
        request_id,
        "<!doctype html><html><body>unexpected planner output</body></html>",
    )
    card = window.ai_edit_assistant_panel.typed_card
    assert card is not None
    assert card["data"].get("is_fallback") is False
    assert "choose_template" not in card["data"].get("actions", ())
    assert not set(card["data"].get("actions", ())) & {"confirm", "execute", "generate"}
    assert plan["scene"] is None
    assert plan["build_failed"] is True


def test_build_prompt_keeps_canvas_hard_constraint_but_opens_visual_design() -> None:
    """The model should not be steered into the old card-grid template."""
    prompt = TeachingAnimationModule._build_motion_graphic_build_user_prompt(
        "做一支有电影感的产品片头", seconds=12.0, fps=25.0,
        width=1280, height=720,
    )
    assert "1280x720" in prompt
    assert "0 <= frame < 300" in prompt
    assert "自由设计" in prompt
    assert "基础技术约束" in prompt
    assert "3~5" not in prompt
    assert "标题和副标题各 1 个" not in prompt
    assert "所有元素必须有入场动画" not in prompt
    assert "不要套固定模板" not in prompt


def test_html_build_prompt_is_webpage_first_and_only_canvas_is_hard() -> None:
    """The browser hand-off gives the model the website, not a host template."""
    prompt = TeachingAnimationModule._build_motion_graphic_html_user_prompt(
        "做一支像产品发布会网页的 HTML5 学习演示",
        seconds=15.0, fps=25.0, width=1440, height=900,
    )
    system = TeachingAnimationModule._MOTION_GRAPHIC_HTML_BUILD_SYSTEM_PROMPT
    assert "1440x900" in prompt
    assert "视频交付视口" in prompt
    assert "不是固定像素布局要求" in prompt
    assert "响应式布局" in prompt
    assert "每一页都要推进叙事" in prompt
    assert "自行决定页面数量" in prompt
    assert "完整 HTML 文档" in prompt
    assert "网页设计、内容和视觉表现全部由你自由发挥" in prompt
    assert "唯一的硬约束只有两项" in system
    assert "宿主不规定任何模板" in system
    assert "没有任何审美、风格、结构、页数" in system
    assert "一张张可以切换的网页" in system
    assert "data-topos-page" in system
    assert "自动切换到下一页" in system
    assert "3-5" not in prompt
    assert "保持比例" in system
    assert "topos-page-durations" not in prompt
    assert "topos-page-durations" not in system


def test_direct_generation_does_not_pass_private_brief_to_html_author() -> None:
    """Direct mode gives the model only the canvas/page browser contract."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    request_id = _make_plan(module, scene=None, seconds=15.0, fps=25.0)
    plan = module._pending_motion_graphic_requests[request_id]
    plan.update({
        "render_mode": "html",
        "creative_brief_pending": False,
        "phase": "build",
        "user_text": "帮我生成 15 秒如何学好 HTML5 的演示动画",
        "creative_brief": {
            "concept": "把页面做成标签积木",
            "visual_grammar": "重复矩形和标签",
            "subject_motifs": ["DOM 树"],
        },
    })

    class _Controller:
        def __init__(self) -> None:
            self.prompt = ""
            self.system_instruction = ""

        def start_html_generate_chat(
            self, request_id: str, prompt: str, *, system_instruction: str,
        ) -> bool:
            del request_id
            self.prompt = prompt
            self.system_instruction = system_instruction
            return True

    controller = _Controller()
    window._ai_edit_controller = controller
    assert module._trigger_motion_graphic_llm_build(window, request_id)
    assert "1920x1080" in controller.prompt
    assert "DOM 树" not in controller.prompt
    assert "标签积木" not in controller.prompt
    assert "不要默认使用深色科技风" not in controller.system_instruction
    assert "唯一的画布要求" in controller.system_instruction


def test_html_motion_graphic_response_stages_layer_and_card_without_timeline_write() -> None:
    """HTML output is staged as a scene and waits for the real confirm button."""
    from src.features.teaching_animation.core.model import TeachingSceneDocument

    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()
    request_id = _make_plan(module, scene=None, seconds=8.0, fps=24.0)
    module._pending_motion_graphic_requests[request_id]["render_mode"] = "html"
    html = """<!doctype html>
    <html><head><style>
      :root { --accent: #67e8f9; }
      html, body { margin: 0; width: 100%; height: 100%; }
      body { background: #08111f; color: white; font-family: sans-serif; }
      .deck { width: 100%; height: 100%; position: relative; overflow: hidden; }
      .page { position: absolute; inset: 0; display: grid; place-items: center;
              animation: page-in 8s ease both; }
      .page:nth-child(2) { animation-delay: 2s; }
      @keyframes page-in { from { opacity: 0; transform: translateY(24px); }
                           to { opacity: 1; transform: translateY(0); } }
    </style></head><body><main class="deck">
      <section class="page" data-topos-page="1"><h1 data-topos-node-id="hero">HTML5</h1>
      <p>用语义结构、样式层级和逐帧动效，把想法组织成一段可交付的网页演示。</p>
      <div class="grid"><article>结构</article><article>样式</article><article>交互</article></div></section>
      <section class="page" data-topos-page="2"><h2 data-topos-node-id="copy">结构与动效</h2>
      <p>每个页面都推进一个新信息点：先建立概念，再展示组成部分，最后给出行动线索。</p>
      <div class="grid"><article>层级</article><article>节奏</article><article>验证</article></div></section>
    </main></body></html>"""
    try:
        assert module._handle_model_motion_graphic_html_response(
            window, request_id, html,
        )
        plan = module._pending_motion_graphic_requests[request_id]
        scene = plan["scene"]
        assert isinstance(scene, TeachingSceneDocument)
        assert scene.width == 1920 and scene.height == 1080
        assert scene.html_layer is not None
        assert "@keyframes" in scene.html_layer.sanitized_html
        assert store.get(scene.scene_id) == scene
        card = window.ai_edit_assistant_panel.typed_card["data"]
        assert card["render_mode"] == "html"
        assert card["source_label"] == "html_css"
        assert "confirm" in card["actions"]
        timeline = window.timeline_panel.get_timeline_view()._timeline
        assert [clip for track in timeline.video_tracks for clip in track.clips] == []
    finally:
        store.clear()


def test_sparse_html_generation_gets_one_design_completeness_repair(monkeypatch) -> None:
    """A title-only draft is returned to the author with concrete page evidence."""
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")
    module = TeachingAnimationModule()
    window = _FakeWindow()
    request_id = _make_plan(module, scene=None, seconds=8.0, fps=24.0)
    module._pending_motion_graphic_requests[request_id]["render_mode"] = "html"

    class _Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def start_html_generate_chat(
            self, request_id: str, prompt: str, *, system_instruction: str = "",
        ) -> bool:
            del system_instruction
            self.calls.append((request_id, prompt))
            return True

    controller = _Controller()
    window._ai_edit_controller = controller
    sparse = """<!doctype html><html><body>
      <section data-topos-page=\"1\"><h1>什么是 AI Agent？</h1></section>
    </body></html>"""

    assert module._handle_model_motion_graphic_html_response(window, request_id, sparse)
    plan = module._pending_motion_graphic_requests[request_id]
    assert plan["scene"] is None
    assert len(controller.calls) == 1
    assert "设计完整度检查" in controller.calls[0][1]
    assert "至少两组有意义的信息单元" in controller.calls[0][1]


def test_fixed_canvas_html_gets_one_responsive_layout_repair(monkeypatch) -> None:
    """The delivery viewport must not silently become a fixed web canvas."""
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")
    module = TeachingAnimationModule()
    window = _FakeWindow()
    request_id = _make_plan(module, scene=None, seconds=8.0, fps=24.0)
    module._pending_motion_graphic_requests[request_id]["render_mode"] = "html"

    class _Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def start_html_generate_chat(
            self, request_id: str, prompt: str, *, system_instruction: str = "",
        ) -> bool:
            del system_instruction
            self.calls.append((request_id, prompt))
            return True

    controller = _Controller()
    window._ai_edit_controller = controller
    fixed_canvas = """<!doctype html><html><head><style>
      html, body { width: 1920px; height: 1080px; background: #111827; color: #f8fafc; }
      .grid { display: grid; grid-template-columns: repeat(2, 1fr); }
    </style></head><body><section data-topos-page="1"><main class="grid">
      <h1>什么是 AI Agent？</h1><p>它会理解目标、制定计划、调用工具，并根据结果继续迭代。</p>
      <article>感知上下文</article><article>执行与复盘</article><svg><circle /></svg>
    </main></section></body></html>"""

    assert module._handle_model_motion_graphic_html_response(window, request_id, fixed_canvas)
    report = module._pending_motion_graphic_requests[request_id]["html_design_quality"]
    assert report["fixed_canvas_rules"] == 2
    assert len(controller.calls) == 1
    assert "不能写死为交付宽高的 px 画布" in controller.calls[0][1]


def test_rich_html_design_quality_is_shown_on_motion_graphic_card(monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")
    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()
    request_id = _make_plan(module, scene=None, seconds=8.0, fps=24.0)
    module._pending_motion_graphic_requests[request_id]["render_mode"] = "html"
    module._pending_motion_graphic_requests[request_id]["user_text"] = (
        "制作标题「什么是 AI Agent？」、使用 #f8fafc，并完善第 2 页的演示。"
    )
    module._pending_motion_graphic_requests[request_id]["generation_feedback_memory"] = (
        "改用橙色色系，并保留充分留白。",
    )
    html = """<!doctype html><html><head><style>
      body { margin: 0; color: #f8fafc; background: linear-gradient(135deg, #7c2d12, #f97316); }
      .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
      .card { display: flex; flex-direction: column; padding: 48px; }
      @keyframes rise { from { opacity: 0; } to { opacity: 1; } }
    </style></head><body>
      <section data-topos-page=\"1\"><main class=\"card\"><h1>什么是 AI Agent？</h1>
      <p>它会理解目标、制定计划、调用工具，并根据执行结果继续迭代。</p>
      <div class=\"grid\"><article>感知上下文</article><article>拆分任务</article><article>验证结果</article></div><svg><circle /></svg></main></section>
      <section data-topos-page=\"2\"><main class=\"card\"><h1>从目标到行动</h1>
      <p>每一轮都读取最新状态，发现偏差后重新规划，而不是只输出一次答案。</p>
      <div class=\"grid\"><article>计划</article><article>执行</article><article>复盘</article></div><svg><path /></svg></main></section>
    </body></html>"""
    try:
        assert module._handle_model_motion_graphic_html_response(window, request_id, html)
        card = window.ai_edit_assistant_panel.typed_card["data"]
        report = card["html_design_quality"]
        assert report["needs_repair"] is False
        assert report["overall_score"] >= 0.58
        assert "第 1、2 页信息密度偏低" not in card["quality_warnings"]
        assert card["html_intent_checks"] == {
            "requirements": [
                "包含文案「什么是 AI Agent？」", "使用颜色 #f8fafc", "使用橙色色系", "至少 2 页",
            ],
            "issues": [],
            "passed": True,
        }
        scene = module._pending_motion_graphic_requests[request_id]["scene"]
        assert scene.html_layer.metadata["assistant_feedback_memory_v1"] == [
            "改用橙色色系，并保留充分留白。"
        ]
    finally:
        store.clear()


def test_explicit_generation_requirements_trigger_a_precise_repair(monkeypatch) -> None:
    """A visually adequate deck still cannot ignore literal user requirements."""
    monkeypatch.setenv("TOPOS_TEST_MODE", "1")
    module = TeachingAnimationModule()
    window = _FakeWindow()
    request_id = _make_plan(module, scene=None, seconds=8.0, fps=24.0)
    plan = module._pending_motion_graphic_requests[request_id]
    plan["render_mode"] = "html"
    plan["user_text"] = "主标题写「新品发布」，主色使用 #ff8800。"
    plan["generation_feedback_memory"] = ("请补充第 4 页总结。",)

    class _Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def start_html_generate_chat(
            self, request_id: str, prompt: str, *, system_instruction: str = "",
        ) -> bool:
            del system_instruction
            self.calls.append((request_id, prompt))
            return True

    controller = _Controller()
    window._ai_edit_controller = controller
    html = """<!doctype html><html><head><style>
      body { margin: 0; color: #f8fafc; background: linear-gradient(135deg, #111827, #312e81); }
      .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
      .card { display: flex; flex-direction: column; padding: 48px; }
      @keyframes rise { from { opacity: 0; } to { opacity: 1; } }
    </style></head><body>
      <section data-topos-page="1"><main class="card"><h1>AI 工作流</h1>
      <p>它会理解目标、制定计划、调用工具，并根据执行结果继续迭代。</p>
      <div class="grid"><article>感知上下文</article><article>拆分任务</article><article>验证结果</article></div><svg><circle /></svg></main></section>
      <section data-topos-page="2"><main class="card"><h1>从目标到行动</h1>
      <p>每一轮都读取最新状态，发现偏差后重新规划，而不是只输出一次答案。</p>
      <div class="grid"><article>计划</article><article>执行</article><article>复盘</article></div><svg><path /></svg></main></section>
    </body></html>"""

    assert module._handle_model_motion_graphic_html_response(window, request_id, html)
    assert plan["scene"] is None
    assert plan["html_intent_checks"]["passed"] is False
    assert len(controller.calls) == 1
    assert "明确需求验收未通过" in controller.calls[0][1]
    assert "新品发布" in controller.calls[0][1]
    assert "#ff8800" in controller.calls[0][1]
    assert "第 4 页" in controller.calls[0][1]


def test_html_motion_graphic_response_queues_browser_work_in_production(monkeypatch) -> None:
    """The model callback must return before Chromium validation completes."""
    monkeypatch.delenv("TOPOS_TEST_MODE", raising=False)
    module = TeachingAnimationModule()
    window = _FakeWindow()
    request_id = _make_plan(module, scene=None, seconds=8.0, fps=24.0)
    module._pending_motion_graphic_requests[request_id]["render_mode"] = "html"
    started: list[tuple[str, str]] = []

    class _Controller:
        def start_motion_graphic_postprocess(
            self, request_id, response, processor, on_finished, on_failed,
        ) -> bool:
            del processor, on_finished, on_failed
            started.append((request_id, response))
            return True

    window._ai_edit_controller = _Controller()
    result = module._handle_model_motion_graphic_html_response(
        window, request_id, "<!doctype html><html><body>async</body></html>",
    )
    assert result is None
    assert started == [
        (request_id, "<!doctype html><html><body>async</body></html>")
    ]
    assert module._pending_motion_graphic_requests[request_id]["scene"] is None


def test_html_motion_graphic_response_normalizes_fence_and_preserves_svg() -> None:
    """Real providers may fence HTML; static SVG must survive the pipeline."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()
    request_id = _make_plan(module, scene=None, seconds=8.0, fps=24.0)
    module._pending_motion_graphic_requests[request_id]["render_mode"] = "html"
    html = """```html
    <!doctype html><html><head><style>
      body { margin: 0; background: #101827; }
      @keyframes in { from { opacity: 0; } to { opacity: 1; } }
      svg { width: 100%; height: 100%; animation: in 1s ease both; }
    </style></head><body>
      <svg viewBox="0 0 100 60" xmlns="http://www.w3.org/2000/svg">
        <rect x="2" y="2" width="96" height="56" fill="#2563eb"/>
        <text x="50" y="34" text-anchor="middle" fill="#fff">HTML5</text>
      </svg>
    </body></html>
    ```"""
    try:
        assert module._handle_model_motion_graphic_html_response(window, request_id, html)
        scene = module._pending_motion_graphic_requests[request_id]["scene"]
        assert scene.html_layer is not None
        assert scene.html_layer.sanitized_html.lstrip().startswith("<html")
        assert "<svg" in scene.html_layer.sanitized_html
        assert "<text" in scene.html_layer.sanitized_html
        assert "},to{" not in scene.html_layer.sanitized_html
    finally:
        store.clear()


def test_queue_uses_full_hd_resolution_for_html_plan() -> None:
    """New AI Motion Graphics keep their Full-HD delivery canvas."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    window.timeline_panel._view._timeline = TimelineSequence(
        timeline_id="mg-resolution", width=1440, height=900,
    )

    class _Controller:
        def start_motion_graphic_build_chat(self, request_id, prompt, *, temperature=0.0):
            self.request_id = request_id
            self.prompt = prompt
            self.temperature = temperature
            return True

    controller = _Controller()
    window._ai_edit_controller = controller
    try:
        module._handle_ai_request(window, "帮我创建一个 8 秒网页 Motion Graphic Clip")
        module._handle_ai_request(window, "可以生成了")
        plan = next(iter(module._pending_motion_graphic_requests.values()))
        assert plan["brief"].canvas_size() == (1920, 1080)
        assert "1920x1080" in controller.prompt
        assert plan["phase"] == "plan_review"
        assert not plan["plan_approved"]
    finally:
        get_shared_scene_store().clear()


def test_plan_card_start_button_enters_html_build_without_second_confirmation() -> None:
    """The plan card button is the direct equivalent of sending “可以”."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    request_id = _make_plan(module, scene=None, seconds=8.0, fps=24.0)
    plan = module._pending_motion_graphic_requests[request_id]
    plan.update({
        "phase": "plan_review",
        "creative_brief_pending": False,
        "render_mode": "html",
    })

    class _Controller:
        def __init__(self) -> None:
            self.html_calls: list[str] = []

        def start_html_generate_chat(
            self, request_id: str, prompt: str, *, system_instruction: str = "",
        ) -> bool:
            del request_id, system_instruction
            self.html_calls.append(prompt)
            return True

    controller = _Controller()
    window._ai_edit_controller = controller
    assert module._confirm_motion_graphic_plan(window, request_id)
    assert plan["phase"] == "build"
    assert plan["plan_approved"] is True
    assert controller.html_calls


def test_discussion_generation_shows_draft_and_new_feedback_supersedes_it() -> None:
    """A discussion can build one draft and revise it through the HTML author.

    This is the normal conversational path: preserve the original brief across
    two discussion turns, accept "开始帮我生成吧", show a review card, then
    send later feedback together with the current HTML back to the model.
    """
    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()

    class _Controller:
        def __init__(self) -> None:
            self.plan_calls: list[tuple[str, str]] = []
            self.html_calls: list[tuple[str, str]] = []

        def set_teaching_motion_graphic_build_system_prompt(self, _prompt: str) -> None:
            return None

        def start_html_generate_chat(
            self, request_id: str, prompt: str, *, system_instruction: str = "",
        ) -> bool:
            del system_instruction
            self.html_calls.append((request_id, prompt))
            return True

        def start_motion_graphic_build_chat(
            self, request_id: str, prompt: str, *, temperature: float = 0.0,
        ) -> bool:
            del temperature
            self.plan_calls.append((request_id, prompt))
            return True

    controller = _Controller()
    window._ai_edit_controller = controller
    try:
        first_turn = "帮我创建一个 12 秒 Motion Graphic Clip，介绍新产品"
        second_turn = "深色科技风，标题要简洁，节奏快一点"

        assert not module._handle_ai_request(window, first_turn)
        assert not module._handle_ai_request(window, second_turn)
        assert module._is_generation_authorization("开始帮我生成吧")
        assert module._is_generation_authorization("可以")
        assert module._handle_ai_request(window, "可以")

        request_id = next(iter(module._pending_motion_graphic_requests))
        plan = module._pending_motion_graphic_requests[request_id]
        assert plan["phase"] == "plan_review"
        assert controller.plan_calls and controller.plan_calls[0][0] == request_id
        plan_prompt = controller.plan_calls[0][1]
        assert "1920x1080" in plan_prompt

        # The plan is visible and editable; it must not start HTML authoring.
        plan_response = json.dumps({
            "concept": "用浏览器窗口展开学习旅程",
            "visual_grammar": "从问题到结构再到实践的连续网页叙事",
            "palette": ["#101828", "#70E1F5"],
            "typography": "大标题与短句并置",
            "motion_language": "页面推进与内容渐变",
            "subject_motifs": ["标签", "结构"],
            "beats": [
                {"label": "问题", "purpose": "建立动机", "visual": "打开网页", "copy": "先理解结构"},
                {"label": "实践", "purpose": "落地", "visual": "内容展开", "copy": "再动手练习"},
            ],
            "avoid": ["固定卡片模板"],
        }, ensure_ascii=False)
        assert module._handle_model_motion_graphic_build_response(
            window, request_id, plan_response,
        )
        assert plan["phase"] == "plan_review"
        assert controller.html_calls == []

        # A dissatisfied user edits the plan itself. The second planning call
        # must happen before any HTML call and the request remains reviewable.
        assert module._handle_ai_request(window, "把叙事改得更适合零基础学习者")
        assert plan["phase"] == "plan_review"
        assert plan["creative_brief_pending"] is True
        assert len(controller.plan_calls) == 2
        assert "把叙事改得更适合零基础学习者" in controller.plan_calls[-1][1]
        assert module._handle_model_motion_graphic_build_response(
            window, request_id, plan_response,
        )
        assert plan["phase"] == "plan_review"
        assert controller.html_calls == []
        assert any(
            row["label"] == "已采纳反馈"
            and "零基础学习者" in row["value"]
            for row in module._motion_graphic_plan_rows(plan)
        )

        # Approving the visible plan shows one explicit generation checkpoint;
        # it still must not start the browser author by itself.
        assert module._handle_ai_request(window, "可以")
        assert plan["phase"] == "generation_confirmation"
        assert plan["plan_approved"] is True
        assert controller.html_calls == []

        # The second confirmation starts HTML authoring. Timeline insertion is
        # still deferred to the final confirmation card after preview.
        assert module._handle_ai_request(window, "确认生成")
        assert plan["phase"] == "build"
        assert controller.html_calls and controller.html_calls[0][0] == request_id
        build_prompt = controller.html_calls[0][1]
        assert first_turn in build_prompt
        assert second_turn in build_prompt
        assert "开始帮我生成吧" not in build_prompt
        assert "用户审阅并确认的制作计划" in build_prompt
        assert "连续网页叙事" in build_prompt
        assert "用户在本次创作流程中追加并保留的要求" in build_prompt
        assert "把叙事改得更适合零基础学习者" in build_prompt

        # A built, reviewed draft is not a timeline mutation.
        plan["scene"] = module._build_ai_agent_demo_scene(12.0, 24.0)
        plan["raw_html"] = "<!doctype html><html><body><div data-topos-page=\"1\">draft</div></body></html>"
        module._show_motion_graphic_proposal_card(
            window, request_id, variant=0, seconds=12.0, frame=0,
            graphics_track=None,
        )
        assert window.ai_edit_assistant_panel.typed_card is not None
        timeline = window.timeline_panel.get_timeline_view()._timeline
        assert [clip for track in timeline.video_tracks for clip in track.clips] == []

        # For this direct path, a chat message that resembles confirmation is
        # deliberately not execution.  The card button is the only approval
        # surface for applying the draft.
        assert module._handle_ai_request(window, "确认执行")
        assert request_id in module._pending_motion_graphic_requests
        assert [clip for track in timeline.video_tracks for clip in track.clips] == []

        # New feedback clears the stale card and asks the HTML author to revise
        # the current document.  It must not silently confirm or insert the
        # previously generated scene.
        assert module._handle_ai_request(window, "把标题改得更有冲击力")
        assert request_id in module._pending_motion_graphic_requests
        assert request_id in window.ai_edit_assistant_panel.cleared_request_ids
        assert window.ai_edit_assistant_panel.typed_card is None
        assert [clip for track in timeline.video_tracks for clip in track.clips] == []
        assert not module._pending_scene_edit_requests
        assert plan["html_revision_feedback"] == "把标题改得更有冲击力"
        assert len(controller.html_calls) == 2
        assert "CURRENT HTML" in controller.html_calls[-1][1]
        assert "把标题改得更有冲击力" in controller.html_calls[-1][1]
    finally:
        store.clear()


def test_model_scene_build_registers_before_shared_preview_and_shows_card() -> None:
    """A valid model response must reach the real confirmation-card path."""
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform

    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()
    request_id = _make_plan(module, scene=None)
    payload = {
        "name": "HTML5 学习演示",
        "commands": [{
            "name": "add_element",
            "arguments": {"element": SceneElement(
                "title", "text",
                transform=SceneTransform(x=120, y=120, width=800, height=120),
                content="HTML5 核心标签",
                style={"font_size": 64, "color": "#ffffff"},
            ).to_dict()},
        }],
    }
    try:
        assert module._handle_model_motion_graphic_build_response(
            window, request_id, json.dumps(payload, ensure_ascii=False),
        )
        plan = module._pending_motion_graphic_requests[request_id]
        scene = plan["scene"]
        assert store.get(scene.scene_id) == scene
        assert window.ai_edit_assistant_panel.typed_card is not None
        card = window.ai_edit_assistant_panel.typed_card["data"]
        assert "confirm" in card["actions"]
        assert not card.get("is_fallback", False)
    finally:
        store.clear()


def test_model_scene_build_accepts_deepseek_short_add_element_shape() -> None:
    """Equivalent shorthand from JSON mode must not reject a valid design."""
    module = TeachingAnimationModule()
    scene = module._build_scene_from_commands(
        {
            "name": "自由构图",
            "commands": [{
                "name": "add_element",
                "element": {
                    "element_id": "hero",
                    "type": "text",
                    "transform": {"x": 20, "y": 20, "width": 300, "height": 60},
                    "style": {"color": "#ffffff", "font_size": 42, "font_weight": "bold"},
                    "content": "Hero",
                },
            }],
        },
        seconds=4.0,
        fps=24.0,
        width=1280,
        height=720,
    )
    assert scene.width == 1280 and scene.height == 720
    assert scene.element_by_id("hero") is not None


def test_motion_graphic_json_parser_repairs_transport_wrapping() -> None:
    payload = TeachingAnimationModule._parse_motion_graphic_json_response(
        "```json\n{\"name\": \"x\", \"commands\": [],}\n```"
    )
    assert payload == {"name": "x", "commands": []}


# --------------------------------------------------------------------------- #
# MG-05: confirm refuses without scene unless user chose template
# --------------------------------------------------------------------------- #


def test_confirm_refuses_without_scene() -> None:
    """MG-05: confirm returns False when scene is None and no template chosen."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()

    request_id = _make_plan(module, scene=None, use_template=False)
    assert not module._confirm_motion_graphic_request(window, request_id)


def test_confirm_succeeds_with_explicit_template() -> None:
    """MG-05: confirm succeeds when user explicitly chose template."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()

    request_id = _make_plan(module, scene=None, use_template=True)
    opened: list[str] = []
    module._open_scene = lambda _w, s: opened.append(s.scene_id)
    assert module._confirm_motion_graphic_request(window, request_id)

    # Clip should be inserted.
    timeline = window.timeline_panel.get_timeline_view()._timeline
    clips = [c for t in timeline.video_tracks for c in t.clips]
    assert len(clips) == 1


# --------------------------------------------------------------------------- #
# MG-06: confirm refuses when scene hash is stale
# --------------------------------------------------------------------------- #


def test_confirm_refuses_stale_scene() -> None:
    """MG-06: confirm returns False when scene changed after card was shown."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()

    scene = _make_scene()
    request_id = _make_plan(module, scene=scene)

    # Stamp the approved hash.
    import hashlib
    import json
    payload = json.dumps(scene.to_dict(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    module._pending_motion_graphic_requests[request_id]["approved_scene_hash"] = (
        hashlib.sha256(payload.encode("utf-8")).hexdigest()
    )

    # Now mutate the scene (simulating a concurrent edit).
    from dataclasses import replace
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform
    mutated = replace(scene, elements=scene.elements + (
        SceneElement("extra", "text", transform=SceneTransform(x=100, y=100, width=200, height=50)),
    ))
    module._pending_motion_graphic_requests[request_id]["scene"] = mutated

    # Confirm should refuse — stale.
    assert not module._confirm_motion_graphic_request(window, request_id)


def test_confirm_succeeds_matching_hash() -> None:
    """MG-06: confirm succeeds when scene hash matches exactly."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()

    scene = _make_scene()
    request_id = _make_plan(module, scene=scene)

    # Stamp the approved hash matching the current scene.
    import hashlib
    import json
    payload = json.dumps(scene.to_dict(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    module._pending_motion_graphic_requests[request_id]["approved_scene_hash"] = (
        hashlib.sha256(payload.encode("utf-8")).hexdigest()
    )

    opened: list[str] = []
    module._open_scene = lambda _w, s: opened.append(s.scene_id)
    assert module._confirm_motion_graphic_request(window, request_id)


def test_html_validation_retries_uniform_intro_frame() -> None:
    """A fade-in blank frame must not block Motion Graphic confirmation."""
    from src.features.teaching_animation.core.contracts import RenderContext

    scene = SimpleNamespace(
        scene_id="html-validation",
        duration_frames=120,
        fps=24.0,
        html_layer=SimpleNamespace(
            sanitized_html="<html><body><h1>Hi</h1></body></html>",
            metadata={}, viewport_width=640, viewport_height=360,
        ),
    )
    calls: list[int] = []
    contexts: list[tuple[int, int]] = []
    transparencies: list[bool] = []

    def render(_scene_id, frame, *, context):
        calls.append(frame)
        contexts.append((context.width, context.height))
        transparencies.append(bool(context.transparent))
        if frame == 0:
            raise RuntimeError("[render_failed] capture returned uniform frame (11, 15, 26, 255)")
        return SimpleNamespace(data=b"pixels", width=64, height=36)

    rendered, frame = TeachingAnimationModule._render_motion_graphic_validation_frame(
        scene,
        context=RenderContext(fps=24, width=64, height=36, transparent=True),
        render_teaching_scene_frame=render,
    )
    assert frame == 24
    assert calls == [0, 24]
    assert contexts == [(640, 360), (640, 360)]
    assert transparencies == [True, True]
    assert rendered.width == 64


def test_html_validation_does_not_mask_real_renderer_errors() -> None:
    """Real renderer errors fail immediately; only uniform frames retry."""
    scene = SimpleNamespace(
        scene_id="html-validation-error",
        duration_frames=120,
        fps=24.0,
        html_layer=SimpleNamespace(sanitized_html="<html><body>Hi</body></html>", metadata={}),
    )
    calls: list[int] = []

    def render(_scene_id, frame, *, context):
        del context
        calls.append(frame)
        raise RuntimeError("[render_failed] playwright timeout")

    with pytest.raises(RuntimeError, match="playwright timeout"):
        TeachingAnimationModule._render_motion_graphic_validation_frame(
            scene,
            context=object(),
            render_teaching_scene_frame=render,
        )
    assert calls == [0]


def test_html_validation_uses_contrast_probe_without_disabling_alpha() -> None:
    """A transparent-only capture gets one host-background validation probe."""
    from src.features.teaching_animation.core.contracts import RenderContext

    scene = SimpleNamespace(
        scene_id="html-validation-probe",
        duration_frames=24,
        fps=24.0,
        html_layer=SimpleNamespace(
            sanitized_html="<html><body><h1>Hi</h1></body></html>",
            metadata={}, viewport_width=640, viewport_height=360,
        ),
    )
    calls: list[tuple[int, str | None, bool]] = []

    def render(_scene_id, frame, *, context):
        calls.append((frame, context.html_validation_background, context.transparent))
        if context.html_validation_background is None:
            raise RuntimeError("[render_failed] capture returned uniform frame (0, 0, 0, 0)")
        return SimpleNamespace(data=b"probe-pixels", width=640, height=360)

    rendered, frame = TeachingAnimationModule._render_motion_graphic_validation_frame(
        scene,
        context=RenderContext(fps=24, width=64, height=36, transparent=True),
        render_teaching_scene_frame=render,
    )
    assert frame == 0
    assert rendered.width == 640
    assert calls[-1] == (0, "#101827", True)
    assert all(transparent for _, _, transparent in calls)


# --------------------------------------------------------------------------- #
# MG-02: typed card receives structured data
# --------------------------------------------------------------------------- #


def test_typed_card_receives_element_list() -> None:
    """MG-02: set_motion_graphic_proposal_card receives structured card_data."""
    module = TeachingAnimationModule()
    window = _FakeWindow()
    store = get_shared_scene_store()
    store.clear()

    scene = _make_scene()
    request_id = _make_plan(module, scene=scene)

    module._show_motion_graphic_proposal_card(
        window, request_id, variant=0, seconds=15.0, frame=0,
        graphics_track=None, fallback=False,
    )

    panel = window.ai_edit_assistant_panel
    assert panel.typed_card is not None
    data = panel.typed_card["data"]
    assert data["source_label"] == "llm"
    assert not data["is_fallback"]
    assert data["duration_seconds"] == 15.0
    assert data["canvas_size"] == [1920, 1080]


def test_typed_card_failure_does_not_offer_host_template() -> None:
    """A browser-authoring failure must not substitute a host template."""
    module = TeachingAnimationModule()
    window = _FakeWindow()

    request_id = _make_plan(module, scene=None)
    module._show_motion_graphic_failure_card(
        window, request_id, seconds=15.0, frame=0, reason="LLM timeout",
    )

    panel = window.ai_edit_assistant_panel
    assert panel.typed_card is not None
    data = panel.typed_card["data"]
    assert data["is_fallback"] is False
    assert "choose_template" not in data["actions"]
    assert "confirm" not in data["actions"]


# --------------------------------------------------------------------------- #
# MG-11: canvas dimensions come from brief, not hardcoded
# --------------------------------------------------------------------------- #


def test_brief_canvas_kwargs_returns_brief_dimensions() -> None:
    """MG-11: _brief_canvas_kwargs reads from brief, not hardcoded 1920x1080."""
    from src.features.teaching_animation.ai.brief import MotionGraphicBrief
    brief = MotionGraphicBrief(
        brief_id="b1", topic="test",
        duration_seconds=10.0, canvas_aspect="9:16",
    )
    plan = {"brief": brief}
    kwargs = TeachingAnimationModule._brief_canvas_kwargs(plan)
    assert kwargs == {"width": 1080, "height": 1920}


def test_brief_canvas_kwargs_falls_back_to_1920() -> None:
    """MG-11: without brief, defaults to 1920x1080 for backward compat."""
    kwargs = TeachingAnimationModule._brief_canvas_kwargs({})
    assert kwargs == {"width": 1920, "height": 1080}
