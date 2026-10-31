import pytest

from src.app.app_context import AppContext
from src.app.feature_registry import FeatureRegistry
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.teaching_animation.ai.commands import (
    TeachingAICommand,
    TeachingAICommandRegistry,
    TeachingAIProposalError,
)
from src.features.teaching_animation.ai.context_provider import TeachingSceneContextProvider
from src.features.teaching_animation.ai.provider_registry import get_teaching_ai_provider, unregister_teaching_ai_provider
from src.features.teaching_animation.ai.service import TeachingAnimationAIService
from src.features.teaching_animation.ai.session import TeachingAssistantSessionRegistry
from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.ui.editor_controller import SceneEditorController
from src.shared.feature_flags import FeatureFlags


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default("AI lesson", width=64, height=48, duration_frames=24)


def test_context_provider_exposes_scene_selection_and_capabilities() -> None:
    scene = _scene()
    provider = TeachingSceneContextProvider(scene, project_id="project-1")
    provider.select_element(scene.elements[0].element_id)
    context = provider.get_context()
    assert context["project_id"] == "project-1"
    assert context["scene"]["scene_id"] == scene.scene_id
    assert context["selected_element_id"] == scene.elements[0].element_id
    # The full edit-command vocabulary lives in ``commands`` (TM-06); high-level
    # verbs live in ``capabilities``.
    assert "update_element" in context["commands"]
    assert "apply_template" in context["commands"]
    assert "render_preview" in context["capabilities"]


def test_context_provider_exports_template_and_preset_catalog() -> None:
    """TM-06: the model receives each built-in template's parameter schema and
    the motion-preset ids, derived from the registries — not a stale list."""
    scene = _scene()
    context = TeachingSceneContextProvider(scene).get_context()
    # Every template carries its id + parameter schema.
    template_ids = {t["template_id"] for t in context["templates"]}
    assert "title_card" in template_ids
    title = next(t for t in context["templates"] if t["template_id"] == "title_card")
    assert "params" in title and "display_name" in title
    # Motion presets are advertised so the model can pick e.g. "pop"/"fade".
    assert "pop" in context["motion_presets"] and "fade" in context["motion_presets"]



def test_model_independent_dry_run_compiles_scene_commands() -> None:
    scene = _scene()
    registry = TeachingAICommandRegistry()
    element = SceneElement("ai-shape", "shape", transform=SceneTransform(width=20, height=20), content={"kind": "circle"})
    proposal = registry.dry_run(scene, [
        TeachingAICommand("add_element", {"element": element.to_dict()}),
        TeachingAICommand("update_element", {"element_id": scene.elements[0].element_id, "updates": {"content": "Changed"}}),
    ])
    assert not proposal.is_empty
    assert proposal.proposed_scene.element_by_id("ai-shape") is not None
    assert proposal.proposed_scene.elements[0].content == "Changed"
    assert proposal.diff_hash


def test_animation_command_and_scene_creation_are_available_without_model() -> None:
    registry = TeachingAICommandRegistry()
    scene = registry.create_scene("Generated", width=80, height=60)
    command = TeachingAICommand("set_animation", {
        "element_id": scene.elements[0].element_id,
        "property": "opacity",
        "keyframes": [{"frame": 0, "value": 0.0}, {"frame": 5, "value": 1.0}],
    })
    proposal = registry.dry_run(scene, [command])
    assert proposal.proposed_scene.animations[0].property == "opacity"


def test_apply_requires_confirmation_and_rejects_human_changes() -> None:
    scene = _scene()
    controller = SceneEditorController(scene)
    registry = TeachingAICommandRegistry()
    command = TeachingAICommand("update_element", {"element_id": scene.elements[0].element_id, "updates": {"content": "AI"}})
    proposal = registry.dry_run(scene, [command])
    with pytest.raises(TeachingAIProposalError, match="confirmation"):
        registry.apply(proposal, controller)
    registry.apply(proposal, controller, confirmed=True)
    assert controller.scene.elements[0].content == "AI"
    registry.undo_applied(proposal, controller)
    assert controller.scene.elements[0].content == scene.elements[0].content
    stale = registry.dry_run(scene, [command])
    controller.update_element(scene.elements[0].element_id, {"content": "Human"})
    with pytest.raises(TeachingAIProposalError, match="changed"):
        registry.apply(stale, controller, confirmed=True)


def test_service_preview_and_confirmed_timeline_insert() -> None:
    service = TeachingAnimationAIService(_scene())
    assert service.render_preview(0).pixel_format == "svg"
    timeline = TimelineSequence(timeline_id="timeline")
    with pytest.raises(PermissionError):
        service.insert_into_timeline(timeline)
    result = service.insert_into_timeline(timeline, confirmed=True)
    assert result.manifest.scene_id == service.scene.scene_id
    replaced = service.replace_clip(result.timeline, result.clip.clip_id, confirmed=True)
    assert replaced.manifest.scene_id == service.scene.scene_id


def test_edit_composite_and_window_surfaces_share_one_assistant_session() -> None:
    registry = TeachingAssistantSessionRegistry()
    first = registry.get_or_create("project")
    first.attach("edit")
    second = registry.get_or_create("project")
    second.attach("composite")
    assert first is second
    assert second.attached_surfaces == {"edit", "composite"}


def test_ai_provider_registration_is_optional_feature_registry_hook() -> None:
    unregister_teaching_ai_provider()
    flags = FeatureFlags(teaching_animation=True, composite=False)
    registry = FeatureRegistry()
    registry.initialize(AppContext(feature_flags=flags))
    from src.features.teaching_animation.module import TeachingAnimationModule
    assert registry.register_module(TeachingAnimationModule())
    assert get_teaching_ai_provider() is not None
    registry.get_module("teaching_animation").dispose()
    assert get_teaching_ai_provider() is None


def test_direct_motion_graphic_request_requires_plan_confirmation(monkeypatch) -> None:
    """先讨论，用户明确授权后才出计划卡，确认后才写入时间线。"""
    from src.features.edit.core.timeline_v2 import TimelineSequence
    from src.features.teaching_animation.module import TeachingAnimationModule
    from src.features.teaching_animation.scene_store import get_shared_scene_store

    class FakeView:
        def __init__(self) -> None:
            self._timeline = TimelineSequence(timeline_id="ai-test")

        def execute_command(self, command) -> None:
            self._timeline = command.apply(self._timeline)

    class FakePanel:
        _fps = 24.0

        def __init__(self) -> None:
            self._view = FakeView()
            self.notice = ""

        def get_timeline_view(self):
            return self._view

        def get_current_frame(self) -> int:
            return 48

    class FakeWindow:
        def __init__(self) -> None:
            self.timeline_panel = FakePanel()
            self.ai_edit_assistant_panel = FakeAssistantPanel()

    class FakeAssistantPanel:
        def __init__(self) -> None:
            self.proposal = None
            self.notices: list[tuple[str, bool]] = []

        def set_motion_graphic_proposal(self, request_id, title, details) -> None:
            self.proposal = (request_id, title, details)

        def clear_motion_graphic_proposal(self, request_id) -> None:
            return None

        def set_agent_notice(self, message, *, warning=False) -> None:
            self.notices.append((message, warning))

    prompt = "直接帮我生成 Motion Graphic Clip 动画片段，时长 15 秒"
    module = TeachingAnimationModule()
    window = FakeWindow()
    opened: list[str] = []
    monkeypatch.setattr(module, "_open_scene", lambda _window, scene: opened.append(scene.scene_id))
    store = get_shared_scene_store()
    store.clear()
    try:
        assert module._is_ai_agent_animation_request(prompt)
        assert not module._is_ai_agent_animation_request("Motion Graphic Clip 如何制作？")
        # 第一句只开启规划讨论，不能立刻弹执行卡。
        assert not module._handle_ai_request(window, prompt)
        timeline = window.timeline_panel.get_timeline_view()._timeline
        clips = [clip for track in timeline.video_tracks for clip in track.clips]
        assert clips == []
        assert module._motion_graphic_discussion_active is True
        assert window.ai_edit_assistant_panel.proposal is None

        # 用户讨论满意后明确授权，才显示最终确认卡。
        assert module._handle_ai_request(window, "我觉得规划没问题，可以开始生成了")
        assert window.ai_edit_assistant_panel.proposal is not None
        request_id = window.ai_edit_assistant_panel.proposal[0]
        # MG-05: LLM unavailable (no controller in test) → failure card shown,
        # scene is None.  User must explicitly choose "use template" for
        # confirm to proceed; confirm without that choice returns False.
        assert not module._confirm_motion_graphic_request(window, request_id)
        # Re-queue and simulate user explicitly choosing the built-in template.
        assert module._handle_ai_request(window, "我觉得规划没问题，可以开始生成了")
        request_id = window.ai_edit_assistant_panel.proposal[0]
        module._pending_motion_graphic_requests[request_id]["use_template"] = True
        assert module._confirm_motion_graphic_request(window, request_id)
        timeline = window.timeline_panel.get_timeline_view()._timeline
        clips = [clip for track in timeline.video_tracks for clip in track.clips]
        assert len(clips) == 1
        assert clips[0].timeline_start == 48
        assert clips[0].timeline_duration == 15 * 24
        assert clips[0].asset_id.startswith("teaching_animation:")
        assert opened
        module._open_timeline_clip(
            window,
            window.timeline_panel,
            clips[0].clip_id,
            "",
        )

        edit_prompt = "把标题改成“智能协作助手”"
        assert module._handle_ai_request(window, edit_prompt)
        scene_id = clips[0].asset_id.removeprefix("teaching_animation:")
        before = store.get(scene_id)
        assert before is not None
        assert before.element_by_id("ai_agent_title").content == "AI Agent"
        assert module._pending_scene_edit_requests
        edit_request_id = next(iter(module._pending_scene_edit_requests))
        assert module._confirm_scene_edit_request(window, edit_request_id)
        after = store.get(scene_id)
        assert after is not None
        assert after.element_by_id("ai_agent_title").content == "智能协作助手"
    finally:
        store.clear()


def test_motion_graphic_generation_variants_are_not_identical() -> None:
    from src.features.teaching_animation.module import TeachingAnimationModule

    module = TeachingAnimationModule()
    first = module._build_ai_agent_demo_scene(15.0, 24.0, variant=0)
    second = module._build_ai_agent_demo_scene(15.0, 24.0, variant=1)
    third = module._build_ai_agent_demo_scene(15.0, 24.0, variant=2)

    assert first.to_dict() != second.to_dict()
    assert second.to_dict() != third.to_dict()
    assert first.element_by_id("ai_agent_background").content != third.element_by_id("ai_agent_background").content


def test_timeline_asset_resolver_treats_scene_reference_as_non_file_asset() -> None:
    from types import SimpleNamespace

    from src.features.edit.core.timeline_preview_renderer import AssetResolver

    resolver = AssetResolver(SimpleNamespace(get_asset_by_id=lambda _asset_id: None))

    assert resolver.get_asset_type("teaching_animation:scene-1") == "teaching_animation"
    assert resolver.get_asset_path("teaching_animation:scene-1") is None


def test_model_scene_edit_response_is_reviewed_applied_and_preserves_style() -> None:
    """模型只返回局部 style 时也要生成可确认的精确场景修改。"""
    import json

    from src.features.teaching_animation.module import TeachingAnimationModule
    from src.features.teaching_animation.scene_store import get_shared_scene_store

    module = TeachingAnimationModule()
    scene = module._build_ai_agent_demo_scene(15.0, 24.0)
    store = get_shared_scene_store()
    store.clear()
    store.register(scene)

    class _Panel:
        def __init__(self) -> None:
            self.proposal = None
            self.notices = []

        def set_motion_graphic_proposal(self, request_id, title, details) -> None:
            self.proposal = (request_id, title, details)

        def clear_motion_graphic_proposal(self, _request_id) -> None:
            return None

        def set_agent_notice(self, message, *, warning=False) -> None:
            self.notices.append((message, warning))

    class _Window:
        def __init__(self) -> None:
            self.ai_edit_assistant_panel = _Panel()

    window = _Window()
    original = scene.element_by_id("ai_agent_title")
    assert original is not None
    response = json.dumps({
        "scene_id": scene.scene_id,
        "summary": "将标题改为智能协作助手并使用蓝色",
        "commands": [{
            "name": "update_element",
            "arguments": {
                "element_id": "ai_agent_title",
                "updates": {
                    "content": "智能协作助手",
                    "style": {"color": "#3b82f6"},
                },
            },
        }],
    }, ensure_ascii=False)
    try:
        assert module._handle_model_scene_edit_response(window, "修改当前标题", response)
        request_id = window.ai_edit_assistant_panel.proposal[0]
        assert store.get(scene.scene_id).element_by_id("ai_agent_title").content == "AI Agent"
        assert module._confirm_scene_edit_request(window, request_id)
        updated = store.get(scene.scene_id)
        assert updated is not None
        title = updated.element_by_id("ai_agent_title")
        assert title is not None
        assert title.content == "智能协作助手"
        assert title.style["color"] == "#3b82f6"
        assert title.style["font_size"] == original.style["font_size"]
    finally:
        store.clear()


def test_shared_approval_card_dispatches_scene_edit_plan() -> None:
    """同一张确认卡点击后应执行场景修改，而不是只尝试新建 Clip。"""
    from types import SimpleNamespace

    from src.features.teaching_animation.module import TeachingAnimationModule
    from src.features.teaching_animation.scene_store import get_shared_scene_store

    module = TeachingAnimationModule()
    scene = module._build_ai_agent_demo_scene(15.0, 24.0)
    store = get_shared_scene_store()
    store.clear()
    store.register(scene)

    class _Panel:
        def set_motion_graphic_proposal(self, *_args) -> None:
            return None

        def clear_motion_graphic_proposal(self, *_args) -> None:
            return None

        def set_agent_notice(self, *_args, **_kwargs) -> None:
            return None

    window = SimpleNamespace(ai_edit_assistant_panel=_Panel())
    response = (
        '{"scene_id":"%s","summary":"改标题",'
        '"commands":[{"name":"update_element","arguments":{'
        '"element_id":"ai_agent_title","updates":{"content":"新版标题"}}}]}'
    ) % scene.scene_id
    try:
        assert module._handle_model_scene_edit_response(window, "改标题", response)
        request_id = next(iter(module._pending_scene_edit_requests))
        assert module._confirm_motion_graphic_plan(window, request_id)
        updated = store.get(scene.scene_id)
        assert updated is not None
        assert updated.element_by_id("ai_agent_title").content == "新版标题"
    finally:
        store.clear()


def test_teaching_context_exposes_timeline_scene_reference() -> None:
    from types import SimpleNamespace

    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.teaching_animation.module import TeachingAnimationModule
    from src.features.teaching_animation.scene_store import get_shared_scene_store

    scene = _scene()
    store = get_shared_scene_store()
    store.clear()
    store.register(scene)
    timeline = TimelineSequence(timeline_id="context-timeline").with_track_added(
        EditTrack(track_id="graphics", name="Graphics", track_role="graphics")
    )
    timeline = timeline.with_clip_added(
        "graphics",
        ClipInstance(
            clip_id="clip-1", asset_id=f"teaching_animation:{scene.scene_id}",
            track_id="graphics", timeline_start=0, timeline_duration=24,
            source_start=0, source_duration=24,
        ),
    )

    class _Panel:
        class _Signal:
            def connect(self, _slot):
                return None

        teaching_animation_open_requested = _Signal()

        def get_timeline_view(self):
            return SimpleNamespace(_timeline=timeline, _selection=SimpleNamespace(selected_clip_ids=("clip-1",)))

    class _Window:
        timeline_panel = _Panel()

    module = TeachingAnimationModule()
    module.attach_main_window(_Window())
    context = module.get_ai_context()
    try:
        assert context["active_scene_id"] == scene.scene_id
        assert context["clip_references"][0]["clip_id"] == "clip-1"
    finally:
        store.clear()


def test_teaching_context_exposes_current_timeline_canvas_for_motion_graphic_discussion() -> None:
    """The planning model gets the active canvas instead of asking for aspect."""
    from types import SimpleNamespace

    from src.features.teaching_animation.module import TeachingAnimationModule

    timeline = TimelineSequence(
        timeline_id="discussion-canvas",
        width=1440,
        height=900,
    )

    class _Panel:
        class _Signal:
            def connect(self, _slot):
                return None

        teaching_animation_open_requested = _Signal()

        def get_timeline_view(self):
            return SimpleNamespace(_timeline=timeline, _selection=SimpleNamespace(selected_clip_ids=()))

    class _Window:
        timeline_panel = _Panel()

    module = TeachingAnimationModule()
    module.attach_main_window(_Window())
    context = module.get_ai_context()
    assert context["current_timeline"] == {
        "canvas_width": 1440,
        "canvas_height": 900,
        "aspect_ratio": "1440:900",
        "fps": 24.0,
        "current_frame": 0,
    }


def test_optional_teaching_host_is_rebound_after_ai_controller_rebuild() -> None:
    """重建 AiEditController 后仍保留 Teaching Animation 宿主 handler。"""
    from types import SimpleNamespace

    from src.app.controllers.ai_editing_mixin import AiEditingMixin

    class FakeController:
        def __init__(self) -> None:
            self.handler = None

        def set_teaching_request_handler(self, handler) -> None:
            self.handler = handler

    class FakeTeachingModule:
        def attach_main_window(self, host) -> None:
            host._ai_edit_controller.set_teaching_request_handler("teaching-handler")

    host = AiEditingMixin.__new__(AiEditingMixin)
    host._ai_edit_controller = FakeController()
    host.feature_registry = SimpleNamespace(
        get_module=lambda module_id: FakeTeachingModule()
        if module_id == "teaching_animation" else None
    )

    host._attach_optional_ai_hosts()

    assert host._ai_edit_controller.handler == "teaching-handler"


class _HtmlAssistantPanel:
    def __init__(self) -> None:
        self.proposal = None
        self.cleared: list[str] = []
        self.notices: list[tuple[str, bool]] = []

    def set_motion_graphic_proposal(self, request_id, title, details) -> None:
        self.proposal = (request_id, title, details)

    def clear_motion_graphic_proposal(self, request_id) -> None:
        self.cleared.append(str(request_id))

    def set_agent_notice(self, message, *, warning=False) -> None:
        self.notices.append((str(message), bool(warning)))


class _HtmlControllerStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def start_html_generate_chat(self, request_id, user_prompt, *, system_instruction="") -> bool:
        self.calls.append((str(request_id), str(user_prompt), str(system_instruction)))
        return True


class _HtmlWindow:
    def __init__(self) -> None:
        self._ai_edit_controller = _HtmlControllerStub()
        self.ai_edit_assistant_panel = _HtmlAssistantPanel()
        self.edit_project_dirty = False


def test_html_generation_is_production_routed_and_approval_is_undoable() -> None:
    """HTML output reaches the service gate and writes only after approval."""
    import hashlib

    from src.features.teaching_animation.module import TeachingAnimationModule
    from src.features.teaching_animation.scene_store import get_shared_scene_store

    store = get_shared_scene_store()
    store.clear()
    scene = _scene()
    store.register(scene)
    module = TeachingAnimationModule()
    window = _HtmlWindow()
    from src.features.teaching_animation.ui.editor_controller import SceneEditorController
    window.editor_controller = SceneEditorController(scene)
    module._editor_registry = type(
        "Registry", (), {"get": lambda _self, _scene_id: window, "_windows": {}}
    )()
    prompt = "请帮我生成一个 Freeform HTML 动画科技标题页"
    raw = '<div data-topos-node-id="title"><h1>AI</h1></div>'
    try:
        assert module._is_html_generate_request(prompt)
        assert not module._is_html_generate_request("如何制作 HTML 动画教程？")
        assert not module._is_html_generate_request("帮我生成一个 Motion Graphic Clip")
        assert module._handle_ai_request(window, prompt)
        assert len(module._pending_html_generate_requests) == 1
        request_id = next(iter(module._pending_html_generate_requests))
        assert window._ai_edit_controller.calls

        assert module._handle_model_html_generate_response(window, request_id, raw)
        assert window.ai_edit_assistant_panel.proposal is not None
        assert store.get(scene.scene_id).html_layer is None

        assert module._confirm_motion_graphic_plan(window, request_id)
        updated = store.get(scene.scene_id)
        assert updated is not None and updated.html_layer is not None
        assert updated.html_layer.source_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert request_id not in module._pending_html_generate_requests
        assert window.edit_project_dirty is True
        assert window.editor_controller.scene.html_layer is not None
        window.editor_controller.undo()
        assert window.editor_controller.scene.html_layer is None
    finally:
        store.clear()
        module.dispose()


def test_html_generation_cancel_and_stale_scene_are_fail_closed() -> None:
    from dataclasses import replace

    from src.features.teaching_animation.module import TeachingAnimationModule
    from src.features.teaching_animation.scene_store import get_shared_scene_store

    store = get_shared_scene_store()
    store.clear()
    scene = _scene()
    store.register(scene)
    module = TeachingAnimationModule()
    window = _HtmlWindow()
    raw = '<div data-topos-node-id="title"><h1>Stable</h1></div>'
    try:
        assert module._handle_ai_request(window, "帮我生成一个 HTML 动效页面")
        request_id = next(iter(module._pending_html_generate_requests))
        module._handle_model_html_generate_response(window, request_id, raw)
        store.register(replace(scene, name="changed after preview"))
        assert not module._confirm_motion_graphic_plan(window, request_id)
        assert store.get(scene.scene_id).html_layer is None
        assert module._cancel_motion_graphic_plan(window, request_id)
        assert not module._pending_html_generate_requests
    finally:
        store.clear()
        module.dispose()


def test_html_chat_controller_mode_forwards_success_failure_and_cancel() -> None:
    from src.app.controllers.ai_edit_controller import AiEditController

    class Service:
        def __init__(self) -> None:
            self.states = []
            self.messages = []

        def _set_state(self, state) -> None:
            self.states.append(state)

        def append_chat_message(self, role, text, **kwargs) -> None:
            self.messages.append((role, text, kwargs))

    class Panel:
        def clear_streaming_reply(self) -> None:
            return None

    def make_controller() -> AiEditController:
        controller = AiEditController.__new__(AiEditController)
        controller._chat_worker = None
        controller._panel = Panel()
        controller._service = Service()
        controller._chat_mode = "teaching_html_generate"
        controller._teaching_html_generate_request_id = "html-1"
        controller._teaching_html_generate_system_prompt = "system"
        controller._streaming_reply_text = ""
        return controller

    success = make_controller()
    received: list[tuple[str, str]] = []
    success._teaching_html_generate_response_handler = lambda rid, text: received.append((rid, text)) or True
    success._record_assistant_message = lambda _text: None
    success._on_chat_finished("<div>ok</div>")
    assert received == [("html-1", "<div>ok</div>")]
    assert success._chat_mode == "normal"

    failed = make_controller()
    failed_received: list[tuple[str, str]] = []
    failed._teaching_html_generate_response_handler = lambda rid, text: failed_received.append((rid, text)) or True
    failed._on_chat_failed("network")
    assert failed_received == [("html-1", "")]
    assert failed._chat_mode == "normal"

    cancelled = make_controller()
    cancelled_received: list[tuple[str, str]] = []
    cancelled._teaching_html_generate_response_handler = lambda rid, text: cancelled_received.append((rid, text)) or True
    cancelled._on_chat_cancelled()
    assert cancelled_received == [("html-1", "")]
    assert cancelled._chat_mode == "normal"
