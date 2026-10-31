"""W2: undoable HTML-layer commands + the approve → scene → undo closed loop.

Covers SetHtmlLayerCommand / RemoveHtmlLayerCommand (FH-01 write path) and
build_html_layer_from_pipeline, which turns a writable AQ-09 outcome into a
project-owned FreeformHtmlMotionLayer.  The end-to-end test exercises the full
chain: model HTML → pipeline → outcome → layer → command → scene → undo/redo.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.ai.html_generation_protocol import (
    run_html_generation_pipeline,
)
from src.features.teaching_animation.core import (
    HtmlEditSlot, RemoveHtmlLayerCommand, SCENE_SCHEMA_VERSION,
    SceneCommandStack, SetHtmlLayerCommand, TeachingSceneDocument,
    build_html_layer, build_html_layer_from_pipeline,
)


def _layer(layer_id: str = "hero") -> object:
    return build_html_layer(
        layer_id=layer_id,
        sanitized_html='<div data-topos-node-id="t1">Hi</div>',
        authored_source='<div data-topos-node-id="t1">Hi</div><script>x()</script>',
        viewport_width=1920, viewport_height=1080, duration_frames=120,
        bound_asset_ids=(), editable_slots=(HtmlEditSlot("t1", "content", "text"),),
        sanitizer_version="html-san/1.0",
        renderer_build="playwright-chromium/py1.62.0/chrome-151.0.7922.34",
        csp="default-src 'none'; style-src 'unsafe-inline'",
    )


class TestSetHtmlLayerCommand:
    def test_set_on_scene_without_layer(self):
        scene = TeachingSceneDocument.create_default("S")
        cmd = SetHtmlLayerCommand(_layer())
        applied = cmd.apply(scene)
        assert applied.html_layer is not None
        assert applied.html_layer.layer_id == "hero"
        undone = cmd.undo(applied)
        assert undone.html_layer is None  # previous was None, restored exactly

    def test_replace_layer_restores_previous_on_undo(self):
        scene = TeachingSceneDocument.create_default("S")
        stack = SceneCommandStack()
        first = stack.execute(SetHtmlLayerCommand(_layer("a")), scene)
        second = stack.execute(SetHtmlLayerCommand(_layer("b")), first)
        assert second.html_layer.layer_id == "b"
        restored = stack.undo(second)
        assert restored.html_layer.layer_id == "a"  # previous layer restored

    def test_undo_before_apply_raises(self):
        cmd = SetHtmlLayerCommand(_layer())
        with pytest.raises(RuntimeError):
            cmd.undo(TeachingSceneDocument.create_default("S"))

    def test_execute_undo_redo_via_stack(self):
        scene = TeachingSceneDocument.create_default("S")
        stack = SceneCommandStack()
        applied = stack.execute(SetHtmlLayerCommand(_layer()), scene)
        assert stack.can_undo()
        undone = stack.undo(applied)
        assert undone.html_layer is None
        assert stack.can_redo()
        redone = stack.undo(undone) if False else stack.redo(undone)
        assert redone.html_layer is not None and redone.html_layer.layer_id == "hero"

    def test_layer_round_trips_through_save_after_command(self):
        scene = TeachingSceneDocument.create_default("S")
        applied = SetHtmlLayerCommand(_layer()).apply(scene)
        reopened = TeachingSceneDocument.from_json(applied.to_json())
        assert reopened.html_layer is not None
        assert reopened.html_layer.sanitizer_hash == applied.html_layer.sanitizer_hash


class TestRemoveHtmlLayerCommand:
    def test_remove_then_undo_restores(self):
        scene = TeachingSceneDocument.create_default("S")
        stack = SceneCommandStack()
        with_layer = stack.execute(SetHtmlLayerCommand(_layer()), scene)
        removed = stack.execute(RemoveHtmlLayerCommand(), with_layer)
        assert removed.html_layer is None
        restored = stack.undo(removed)
        assert restored.html_layer is not None and restored.html_layer.layer_id == "hero"

    def test_remove_on_scene_without_layer_is_noop_then_undo(self):
        scene = TeachingSceneDocument.create_default("S")
        cmd = RemoveHtmlLayerCommand()
        removed = cmd.apply(scene)
        assert removed.html_layer is None
        undone = cmd.undo(removed)
        assert undone.html_layer is None  # was None before, stays None


class TestBuildLayerFromPipeline:
    def test_writable_outcome_builds_layer(self):
        raw = '<div data-topos-node-id="t1">AI Agent</div><script>bad()</script>'
        out = run_html_generation_pipeline(raw, approved=True)
        assert out.writable is True
        layer = build_html_layer_from_pipeline(
            out, layer_id="hero", authored_source=raw,
            viewport_width=1920, viewport_height=1080, duration_frames=120,
            sanitizer_version="html-san/1.0",
        )
        assert "<script" not in layer.sanitized_html          # sanitizer stripped it
        assert layer.source_hash != layer.sanitizer_hash        # provenance captures the change
        assert layer.sanitizer_version == "html-san/1.0"
        assert [slot.node_id for slot in layer.editable_slots] == ["t1"]

    def test_renderer_build_pulled_from_preview_evidence(self):
        from tests.ai.test_html_generation_protocol import _FakeBackend
        raw = "<div>ok</div>"
        backend = _FakeBackend(["cc" * 16, "cc" * 16])  # deterministic
        out = run_html_generation_pipeline(raw, approved=True, preview_backend=backend)
        layer = build_html_layer_from_pipeline(out, layer_id="L", authored_source=raw)
        assert layer.renderer_build == "fake/test"

    def test_non_writable_outcome_refused(self):
        raw = "<div>ok</div>"
        out = run_html_generation_pipeline(raw, approved=False)  # not approved → not writable
        with pytest.raises(ValueError):
            build_html_layer_from_pipeline(out, layer_id="L", authored_source=raw)


def test_end_to_end_closed_loop():
    """model HTML → pipeline → outcome → layer → command → scene → undo/redo."""
    raw = '<div data-topos-node-id="t1">AI Agent</div><script>bad()</script>'
    outcome = run_html_generation_pipeline(raw, approved=True)
    assert outcome.writable
    layer = build_html_layer_from_pipeline(
        outcome, layer_id="hero", authored_source=raw,
        viewport_width=1920, viewport_height=1080, duration_frames=120,
        sanitizer_version="html-san/1.0",
    )
    scene = TeachingSceneDocument.create_default("Project")
    assert scene.html_layer is None

    stack = SceneCommandStack()
    applied = stack.execute(SetHtmlLayerCommand(layer), scene)
    assert applied.html_layer.layer_id == "hero"
    # save/reopen preserves the approved layer
    reopened = TeachingSceneDocument.from_json(applied.to_json())
    assert reopened.html_layer.sanitized_html == applied.html_layer.sanitized_html
    # undo removes the layer; redo puts it back
    undone = stack.undo(applied)
    assert undone.html_layer is None
    redone = stack.redo(undone)
    assert redone.html_layer.layer_id == "hero"
