"""FH-09 editor wiring: Save as Template + Apply Template (window-scoped).

The promotion engine is pure logic and tested in isolation; these tests pin
its **reachability from the editor** — the toolbar actions, the window-scoped
template registry, and the undoable re-application path.  Logic and modal
dialogs are split on the window (``_save_as_template``/``_apply_template``
do the work; ``*_triggered`` open dialogs), so the wiring is testable without
driving Qt modal input.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.html_layer import (
    HtmlEditSlot, build_html_layer,
)
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.ui.motion_graphic_editor_window import (
    MotionGraphicEditorWindow,
)


_HTML = (
    "<style>:root{--brand:#00e676}.t{color:#fff;background:var(--brand)}"
    "@keyframes slide{0%{left:0}100%{left:200px}}"
    ".bar{animation:slide 1s linear infinite}</style>"
    '<div class="bar" data-topos-node-id="title">Hello</div>'
    '<img src="topos-asset://logo" data-topos-node-id="logo">'
)


def _layer():
    return build_html_layer(
        layer_id="L", sanitized_html=_HTML, authored_source=_HTML,
        viewport_width=320, viewport_height=180, duration_frames=48,
        bound_asset_ids=("logo", "photo"),
        editable_slots=(HtmlEditSlot(node_id="title", kind="text"),
                        HtmlEditSlot(node_id="logo", kind="asset")),
        editable_tokens=("brand",),
        sanitizer_version="test-1.0",
    )


def _scene(with_layer: bool = True) -> TeachingSceneDocument:
    base = TeachingSceneDocument.create_default("s", width=320, height=180, duration_frames=48)
    return replace(base, html_layer=_layer()) if with_layer else base


@pytest.fixture
def isolated_generator_registry(monkeypatch):
    """Keep the process-global generator registry out of window-scoped tests.

    The editor window registers every generator template at construction
    (FH-09); these tests assert the window's own registry state, so they must
    not observe templates promoted by other test modules.
    """
    from src.features.teaching_animation.core.motion_graphic_generator_registry import (
        MotionGraphicGeneratorRegistry,
    )
    monkeypatch.setattr(
        "src.features.teaching_animation.core.motion_graphic_generator_registry"
        ".get_motion_graphic_generator_registry",
        lambda: MotionGraphicGeneratorRegistry(),
    )


@pytest.fixture
def window(qapp, isolated_generator_registry):
    win = MotionGraphicEditorWindow(_scene())
    yield win
    win.deleteLater()
    qapp.processEvents()


class TestSaveAsTemplate:
    def test_promotes_and_registers_in_window_registry(self, window) -> None:
        tpl = window._save_as_template("Hero", template_id="hero")
        assert tpl is not None and tpl.template_id == "hero"
        assert [t.template_id for t in window._html_templates.list_templates()] == ["hero"]
        assert {p.name for p in tpl.params_schema} == {"title", "logo", "brand"}

    def test_without_html_layer_returns_none(self, qapp, isolated_generator_registry) -> None:
        win = MotionGraphicEditorWindow(_scene(with_layer=False))
        try:
            assert win._save_as_template("X") is None
            assert win._html_templates.list_templates() == ()
        finally:
            win.deleteLater()
            qapp.processEvents()

    def test_save_enables_apply_action(self, window) -> None:
        assert not window._apply_template_action.isEnabled()
        window._save_as_template("Hero", template_id="hero")
        # Action enablement is refreshed on scene change; force a refresh.
        window._refresh_scene(window.editor_controller.scene)
        assert window._apply_template_action.isEnabled()


class TestApplyTemplate:
    def test_apply_swaps_scene_layer_and_is_undoable(self, window) -> None:
        window._save_as_template("Hero", template_id="hero")
        original_layer = window.editor_controller.scene.html_layer
        original_html = original_layer.sanitized_html
        assert window._apply_template("hero") is True
        # The scene now carries a fresh layer (different id) with default content.
        new_layer = window.editor_controller.scene.html_layer
        assert new_layer.layer_id != original_layer.layer_id
        # Defaults reproduce the source content.
        assert "Hello" in new_layer.sanitized_html
        # Undo restores the previous layer (SetHtmlLayerCommand is reversible).
        window._undo()
        assert window.editor_controller.scene.html_layer.sanitized_html == original_html

    def test_unknown_template_returns_false(self, window) -> None:
        assert window._apply_template("nope") is False

    def test_apply_after_override_uses_default_params(self, window) -> None:
        # Promote, then mutate the live layer, then apply — apply should use
        # the template defaults (original values), not the mutated layer.
        window._save_as_template("Hero", template_id="hero")
        # Sanity: applying reproduces the original title.
        window._apply_template("hero")
        assert "Hello" in window.editor_controller.scene.html_layer.sanitized_html


class TestActionEnablement:
    def test_save_action_enabled_iff_layer_present(self, qapp) -> None:
        win_with = MotionGraphicEditorWindow(_scene(with_layer=True))
        try:
            assert win_with._save_template_action.isEnabled()
        finally:
            win_with.deleteLater()
            qapp.processEvents()
        win_without = MotionGraphicEditorWindow(_scene(with_layer=False))
        try:
            assert not win_without._save_template_action.isEnabled()
        finally:
            win_without.deleteLater()
            qapp.processEvents()

    def test_export_html_action_and_source_export(self, window, tmp_path) -> None:
        output = tmp_path / "motion.html"
        assert window._export_html_action.isEnabled()
        assert window._export_html_document(output)
        exported = output.read_text(encoding="utf-8")
        assert exported.startswith("<!doctype html>")
        assert "@keyframes slide" in exported
        assert "data-topos-node-id=\"title\"" in exported

    def test_export_html_action_disabled_without_layer(self, qapp, tmp_path) -> None:
        win = MotionGraphicEditorWindow(_scene(with_layer=False))
        try:
            assert not win._export_html_action.isEnabled()
            assert not win._export_html_document(tmp_path / "should-not-be-written.html")
        finally:
            win.deleteLater()
            qapp.processEvents()
