"""Explicit media-pool target binding for Motion Graphic revisions."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from src.features.teaching_animation.core.html_layer import FreeformHtmlMotionLayer
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.module import TeachingAnimationModule
from src.features.teaching_animation.scene_store import get_shared_scene_store


def _html_scene(scene_id: str) -> TeachingSceneDocument:
    scene = replace(
        TeachingSceneDocument.create_default(
            "Target Clip", scene_id=scene_id, width=640, height=360,
            fps_num=25, duration_frames=50,
        ),
        html_layer=FreeformHtmlMotionLayer(
            layer_id=f"layer-{scene_id}",
            sanitized_html=(
                '<!doctype html><html><body>'
                '<main data-topos-page="0">当前网页</main>'
                '</body></html>'
            ),
            viewport_width=640,
            viewport_height=360,
            fps_num=25,
            fps_den=1,
            duration_frames=50,
        ),
    )
    return scene


class _Controller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def start_html_generate_chat(self, request_id, user_prompt, **_kwargs):
        self.calls.append((request_id, user_prompt))
        return True


class _Panel:
    def __init__(self) -> None:
        self.proposals = []
        self.notices = []

    def set_motion_graphic_proposal(self, *args) -> None:
        self.proposals.append(args)

    def set_agent_notice(self, message, *, warning=False) -> None:
        self.notices.append((message, warning))


class _Service:
    def __init__(self) -> None:
        self.messages = []

    def append_chat_message(self, role, text, **_kwargs) -> None:
        self.messages.append((role, text))


def _window(assets, controller, panel, service):
    return SimpleNamespace(
        _current_edit_project=SimpleNamespace(assets=assets),
        _ai_edit_controller=controller,
        ai_edit_assistant_panel=panel,
        _ai_assistant_service=service,
    )


def test_target_edit_uses_stable_asset_and_existing_scene(monkeypatch):
    store = get_shared_scene_store()
    store.clear()
    scene = _html_scene("scene-target")
    store.register(scene)
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window({
        "teaching_animation:scene-target": SimpleNamespace(
            asset_id="teaching_animation:scene-target",
            display_name="课程网页 Clip",
            media_type="motion_graphic",
            metadata={"scene_id": "scene-target"},
        ),
    }, controller, panel, service)
    module = TeachingAnimationModule()

    try:
        assert module._queue_motion_graphic_edit(
            window,
            "把背景改成深色霓虹",
            ("teaching_animation:scene-target",),
        )
        assert len(controller.calls) == 1
        request_id, prompt = controller.calls[0]
        plan = module._pending_html_generate_requests[request_id]
        assert plan["scene_id"] == "scene-target"
        assert plan["target_asset_id"] == "teaching_animation:scene-target"
        assert "CURRENT HTML" in prompt
        assert "深色霓虹" in prompt
        # The edit is announced as a DIRECT in-place update (no card ceremony).
        assert "直接更新这个片段" in service.messages[-1][1]
        assert "不新建素材" in service.messages[-1][1]
    finally:
        store.clear()


def test_multiple_explicit_targets_are_rejected_instead_of_guessing():
    store = get_shared_scene_store()
    store.clear()
    first = _html_scene("scene-one")
    second = _html_scene("scene-two")
    store.register(first)
    store.register(second)
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window({
        "teaching_animation:scene-one": SimpleNamespace(
            asset_id="teaching_animation:scene-one", display_name="Clip A",
            media_type="motion_graphic", metadata={"scene_id": "scene-one"},
        ),
        "teaching_animation:scene-two": SimpleNamespace(
            asset_id="teaching_animation:scene-two", display_name="Clip B",
            media_type="motion_graphic", metadata={"scene_id": "scene-two"},
        ),
    }, controller, panel, service)

    try:
        assert TeachingAnimationModule()._queue_motion_graphic_edit(
            window, "修改网页", (
                "teaching_animation:scene-one",
                "teaching_animation:scene-two",
            ),
        )
        assert controller.calls == []
        assert any("multiple Motion Graphic clips" in text for _, text in service.messages)
    finally:
        store.clear()


def test_target_edit_applies_directly_without_confirmation_card(monkeypatch):
    """An edit of an existing clip updates the scene in place, card-free."""
    store = get_shared_scene_store()
    store.clear()
    scene = _html_scene("scene-confirm")
    store.register(scene)
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window({
        "teaching_animation:scene-confirm": SimpleNamespace(
            asset_id="teaching_animation:scene-confirm",
            display_name="待修改网页",
            media_type="motion_graphic",
            metadata={"scene_id": "scene-confirm"},
        ),
    }, controller, panel, service)
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _scene: None)

    try:
        assert module._queue_motion_graphic_edit(
            window, "把主标题改成「修改后的网页」", ("teaching_animation:scene-confirm",),
        )
        request_id = controller.calls[0][0]
        response = (
            "<!doctype html><html><body>"
            '<main data-topos-page="0"><h1>修改后的网页</h1></main>'
            "</body></html>"
        )
        assert module._handle_model_html_generate_response(window, request_id, response) is None
        # Direct apply: no proposal card, scene updated in place, request done.
        assert panel.proposals == []
        updated = store.get("scene-confirm")
        assert updated is not None
        assert "修改后的网页" in updated.html_layer.sanitized_html
        assert request_id not in module._pending_html_generate_requests
    finally:
        store.clear()


def test_target_edit_page_shrink_triggers_one_diagnosed_retry(monkeypatch):
    """A revision that collapses the deck is sent back once with the counts."""
    store = get_shared_scene_store()
    store.clear()
    scene = replace(
        _html_scene("scene-shrink"),
        html_layer=replace(
            _html_scene("scene-shrink").html_layer,
            sanitized_html=(
                '<!doctype html><html><body>'
                '<main data-topos-page="1">一</main>'
                '<main data-topos-page="2">二</main>'
                '<main data-topos-page="3">三</main>'
                "</body></html>"
            ),
        ),
    )
    store.register(scene)
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window({
        "teaching_animation:scene-shrink": SimpleNamespace(
            asset_id="teaching_animation:scene-shrink",
            display_name="多页网页", media_type="motion_graphic",
            metadata={"scene_id": "scene-shrink"},
        ),
    }, controller, panel, service)
    module = TeachingAnimationModule()
    monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _scene: None)

    try:
        assert module._queue_motion_graphic_edit(
            window, "把标题改成发布倒计时", ("teaching_animation:scene-shrink",),
        )
        request_id = controller.calls[0][0]
        collapsed = (
            "<!doctype html><html><body>"
            '<main data-topos-page="1">只有一页了</main>'
            "</body></html>"
        )
        assert module._handle_model_html_generate_response(
            window, request_id, collapsed,
        ) is None
        # The deck shrank 3 -> 1: one automatic retry with the exact counts,
        # and the collapsed revision was NOT applied.
        assert len(controller.calls) == 2
        assert "3 页" in controller.calls[1][1]
        assert "只输出了 1 页" in controller.calls[1][1]
        assert "发布倒计时" in controller.calls[1][1]
        assert "只有一页了" not in store.get("scene-shrink").html_layer.sanitized_html
        assert request_id in module._pending_html_generate_requests
    finally:
        store.clear()


def test_untargeted_edit_routed_to_sole_html_clip():
    """A plain revision sentence locks onto the editable clip directly."""
    store = get_shared_scene_store()
    store.clear()
    scene = _html_scene("scene-auto")
    store.register(scene)
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window({
        "teaching_animation:scene-auto": SimpleNamespace(
            asset_id="teaching_animation:scene-auto",
            display_name="自动锁定网页", media_type="motion_graphic",
            metadata={"scene_id": "scene-auto"},
        ),
    }, controller, panel, service)
    module = TeachingAnimationModule()

    try:
        assert module._handle_ai_request(window, "把标题改成「发布倒计时」")
        assert len(controller.calls) == 1
        assert "CURRENT HTML" in controller.calls[0][1]
        assert "发布倒计时" in controller.calls[0][1]
        plan = module._pending_html_generate_requests[controller.calls[0][0]]
        assert plan["auto_apply"] is True
    finally:
        store.clear()


def test_fresh_generation_phrasing_is_not_hijacked_into_an_edit():
    store = get_shared_scene_store()
    store.clear()
    scene = _html_scene("scene-fresh")
    store.register(scene)
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window({
        "teaching_animation:scene-fresh": SimpleNamespace(
            asset_id="teaching_animation:scene-fresh",
            display_name="网页", media_type="motion_graphic",
            metadata={"scene_id": "scene-fresh"},
        ),
    }, controller, panel, service)
    module = TeachingAnimationModule()

    try:
        assert not module._handle_ai_request(window, "重新生成一个全新的科技感片头")
        assert module._handle_ai_request(window, "你好") is False
        assert controller.calls == []
    finally:
        store.clear()


def test_untargeted_edit_falls_back_to_last_generated_clip():
    store = get_shared_scene_store()
    store.clear()
    scene = _html_scene("scene-last")
    store.register(scene)
    controller = _Controller()
    panel = _Panel()
    service = _Service()
    window = _window({}, controller, panel, service)
    module = TeachingAnimationModule()
    module._last_motion_graphic_target = {
        "asset_id": "teaching_animation:scene-last",
        "scene_id": "scene-last",
        "display_name": "上一次的网页",
    }

    try:
        target = module._motion_graphic_edit_target(window, "字号改大一点")
        assert target is not None
        assert target[1].scene_id == "scene-last"
    finally:
        store.clear()
