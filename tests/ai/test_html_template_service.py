"""TeachingAnimationAIService HTML template promote/apply wiring (FH-09).

The promotion engine (``core/html_template.py`` + ``render/html_layer/
template_promote.py``) is pure logic and unit-tested in isolation; these
tests pin its **reachability** through the assistant service — the gap that
turns a verified one-off HTML layer into a reusable, re-applicable template
without a browser.

* ``promote_html_template`` distils the scene's current layer and registers
  it on the session registry (and refuses when there is no layer);
* ``apply_html_template`` re-instantiates a registered template onto the
  scene via the same undoable ``SetHtmlLayerCommand`` path as
  ``apply_html_layer``, and rejects unknown templates / bad params;
* ``list_html_templates`` exposes the session registry.

No browser, no Qt — fast, offline, deterministic.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.ai.service import TeachingAnimationAIService
from src.features.teaching_animation.core.html_layer import (
    HtmlEditSlot, build_html_layer,
)
from src.features.teaching_animation.core.model import TeachingSceneDocument


_HTML = (
    "<style>:root{--brand:#00e676}"
    ".t{color:#fff;background:var(--brand)}"
    "@keyframes slide{0%{left:0}100%{left:200px}}"
    ".bar{animation:slide 1s linear infinite}"
    "</style>"
    '<div class="bar" data-topos-node-id="title">Hello</div>'
    '<img src="topos-asset://logo" data-topos-node-id="logo">'
)


def _scene_with_layer() -> TeachingSceneDocument:
    layer = build_html_layer(
        layer_id="L", sanitized_html=_HTML, authored_source=_HTML,
        viewport_width=320, viewport_height=180, duration_frames=48,
        bound_asset_ids=("logo", "photo"),
        editable_slots=(HtmlEditSlot(node_id="title", kind="text"),
                        HtmlEditSlot(node_id="logo", kind="asset")),
        editable_tokens=("brand",),
        sanitizer_version="test-1.0",
    )
    return replace(
        TeachingSceneDocument.create_default("s", width=320, height=180, duration_frames=48),
        html_layer=layer,
    )


class TestPromoteHtmlTemplate:
    def test_promote_registers_and_returns_template(self) -> None:
        svc = TeachingAnimationAIService(_scene_with_layer())
        tpl = svc.promote_html_template(
            template_id="hero", display_name="Hero", description="d", version=2)
        assert tpl.template_id == "hero" and tpl.display_name == "Hero" and tpl.version == 2
        # Registered on the session registry.
        assert [t.template_id for t in svc.list_html_templates()] == ["hero"]
        # Parameterization captured the editable surface.
        assert {p.name for p in tpl.params_schema} == {"title", "logo", "brand"}

    def test_promote_does_not_mutate_source_layer(self) -> None:
        scene = _scene_with_layer()
        before = scene.html_layer.sanitized_html
        svc = TeachingAnimationAIService(scene)
        svc.promote_html_template(template_id="hero", display_name="Hero")
        assert svc.scene.html_layer.sanitized_html == before

    def test_promote_without_html_layer_raises(self) -> None:
        svc = TeachingAnimationAIService(TeachingSceneDocument.create_default("s"))
        with pytest.raises(ValueError, match="no HTML layer"):
            svc.promote_html_template(template_id="x", display_name="X")

    def test_promote_twice_keeps_latest_version(self) -> None:
        svc = TeachingAnimationAIService(_scene_with_layer())
        svc.promote_html_template(template_id="hero", display_name="Hero", version=1)
        svc.promote_html_template(template_id="hero", display_name="Hero", version=2)
        latest = svc.html_templates.get("hero")
        assert latest is not None and latest.version == 2


class TestApplyHtmlTemplate:
    def test_apply_with_overrides_attaches_new_layer(self) -> None:
        svc = TeachingAnimationAIService(_scene_with_layer())
        svc.promote_html_template(template_id="hero", display_name="Hero")
        original_id = svc.scene.html_layer.layer_id
        new = svc.apply_html_template(
            "hero", {"title": "World", "logo": "photo", "brand": "#1122ff"})
        # A fresh layer is attached to the scene with new content.
        assert new.layer_id != original_id
        html = svc.scene.html_layer.sanitized_html
        assert "World" in html and "#1122ff" in html and "topos-asset://photo" in html
        assert "Hello" not in html  # old content replaced

    def test_apply_with_defaults_reproduces_source_content(self) -> None:
        svc = TeachingAnimationAIService(_scene_with_layer())
        svc.promote_html_template(template_id="hero", display_name="Hero")
        svc.apply_html_template("hero")
        html = svc.scene.html_layer.sanitized_html
        assert "Hello" in html and "topos-asset://logo" in html and "#00e676" in html

    def test_apply_unknown_template_raises(self) -> None:
        svc = TeachingAnimationAIService(_scene_with_layer())
        with pytest.raises(KeyError, match="unknown HTML template"):
            svc.apply_html_template("nope")

    def test_apply_missing_required_param_raises(self) -> None:
        svc = TeachingAnimationAIService(_scene_with_layer())
        # A template whose only param is required cannot be applied without it.
        from src.features.teaching_animation.core.executable_template import ParamSpec
        from src.features.teaching_animation.core.html_template import HtmlTemplate
        svc.html_templates.register(HtmlTemplate(
            template_id="req", display_name="Req",
            html_template="<div>{{slot:x}}</div>",
            params_schema=(ParamSpec(name="x", type="string", required=True),),
            viewport_width=10, viewport_height=10,
        ))
        with pytest.raises(ValueError, match="invalid params"):
            svc.apply_html_template("req")

    def test_apply_preserves_template_geometry(self) -> None:
        svc = TeachingAnimationAIService(_scene_with_layer())
        svc.promote_html_template(template_id="hero", display_name="Hero")
        svc.apply_html_template("hero")
        layer = svc.scene.html_layer
        assert layer.viewport_width == 320 and layer.viewport_height == 180
        assert layer.duration_frames == 48
        assert layer.bound_asset_ids == ("logo", "photo")


class TestSessionRegistry:
    def test_list_returns_promoted_templates(self) -> None:
        svc = TeachingAnimationAIService(_scene_with_layer())
        svc.promote_html_template(template_id="a", display_name="A")
        svc.promote_html_template(template_id="b", display_name="B")
        ids = {t.template_id for t in svc.list_html_templates()}
        assert ids == {"a", "b"}

    def test_registries_are_session_scoped(self) -> None:
        # Two services do not share promoted templates.
        svc1 = TeachingAnimationAIService(_scene_with_layer())
        svc2 = TeachingAnimationAIService(_scene_with_layer())
        svc1.promote_html_template(template_id="hero", display_name="Hero")
        assert svc1.html_templates.get("hero") is not None
        assert svc2.html_templates.get("hero") is None
