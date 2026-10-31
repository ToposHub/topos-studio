"""TeachingAnimationAIService HTML generation loop (W2 / AQ-09 → FH-01).

The generation pipeline (sanitize → static-analysis → bind-assets → preview →
approve) and SetHtmlLayerCommand were both built and unit-tested but had no
production caller: a user could not reach Freeform HTML through the assistant.
These tests pin the two service methods that close that loop:

* ``generate_html`` runs the controlled pipeline and returns an outcome whose
  ``writable`` flag is the single gate;
* ``apply_html_layer`` builds a FreeformHtmlMotionLayer from a *writable*
  outcome and attaches it to the scene via the immutable SetHtmlLayerCommand.

No browser is required: the PREVIEW stage is optional, so these are fast,
offline, deterministic tests of the wiring.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.teaching_animation.ai.html_generation_protocol import HtmlGenerationConstraints
from src.features.teaching_animation.ai.html_llm_generator import LlmGenerationRequest
from src.features.teaching_animation.ai.service import TeachingAnimationAIService
from src.features.teaching_animation.core.model import TeachingSceneDocument


class _FakeLlmClient:
    """A minimal stand-in for the edit LLM client: returns canned HTML."""

    def __init__(self, html: str) -> None:
        self._html = html
        self.calls = 0

    def complete(self, messages: Any, *, temperature: float = 0.0, max_tokens: int = 0) -> str:
        self.calls += 1
        return self._html


def _service() -> TeachingAnimationAIService:
    scene = TeachingSceneDocument.create_default("HTML lesson", width=480, height=270, duration_frames=120)
    return TeachingAnimationAIService(scene)


def test_generate_html_returns_writable_outcome_for_approved_clean_html() -> None:
    """An approved, sanitizer-clean page produces a writable outcome the host
    may gate a project write on."""
    service = _service()
    client = _FakeLlmClient('<div data-topos-node-id="title"><h1>Hello</h1></div>')
    outcome = service.generate_html(
        client,
        request=LlmGenerationRequest(user_prompt="a title card", temperature=0.2, max_tokens=1000),
        constraints=HtmlGenerationConstraints(viewport_width=480, viewport_height=270, max_duration_seconds=5.0),
        approved=True,
    )
    assert client.calls == 1
    assert outcome.writable, outcome.blocking_reasons
    assert "<h1>Hello</h1>" in outcome.sanitized_html
    # authored_source (the raw model output) is carried for FH-01 provenance.
    assert outcome.authored_source


def test_generate_html_stages_unapproved_page_as_not_writable() -> None:
    """approved=False stages the page for preview but never lets it be written."""
    service = _service()
    client = _FakeLlmClient("<p>preview me</p>")
    outcome = service.generate_html(client, approved=False)
    assert not outcome.writable
    assert "not yet approved" in " ".join(outcome.blocking_reasons)


def test_apply_html_layer_attaches_layer_to_scene_with_provenance() -> None:
    """A writable outcome becomes the scene's html_layer via SetHtmlLayerCommand;
    the layer carries the authored-source provenance hash and matches the
    scene viewport."""
    import hashlib
    service = _service()
    raw = '<section data-topos-node-id="hero"><h1>Title</h1></section>'
    outcome = service.generate_html(_FakeLlmClient(raw), approved=True)
    layer = service.apply_html_layer(outcome)
    assert service.scene.html_layer is layer
    assert layer.sanitized_html == outcome.sanitized_html
    # The authored (pre-sanitization) source is stamped as a sha256 on the layer
    # for FH-01 audit provenance — it equals sha256(raw), proving the outcome's
    # authored_source flowed through build_html_layer_from_pipeline.
    assert layer.source_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # Viewport/duration default to the scene's so the layer matches its clip.
    assert layer.viewport_width == service.scene.width
    assert layer.viewport_height == service.scene.height


def test_apply_html_layer_rejects_non_writable_outcome() -> None:
    """Only an approved, sanitized, clean page may become a project layer."""
    service = _service()
    outcome = service.generate_html(_FakeLlmClient("<p>not approved</p>"), approved=False)
    with pytest.raises(ValueError, match="non-writable"):
        service.apply_html_layer(outcome)


def test_apply_html_layer_replaces_existing_layer() -> None:
    """SetHtmlLayerCommand set/replace semantics flow through the service:
    applying a second layer swaps the scene's html_layer rather than stacking."""
    service = _service()
    first = service.apply_html_layer(
        service.generate_html(_FakeLlmClient("<p>first</p>"), approved=True))
    assert service.scene.html_layer is first
    second = service.apply_html_layer(
        service.generate_html(_FakeLlmClient("<p>second</p>"), approved=True))
    assert service.scene.html_layer is second
    assert first is not second
    assert "second" in service.scene.html_layer.sanitized_html
