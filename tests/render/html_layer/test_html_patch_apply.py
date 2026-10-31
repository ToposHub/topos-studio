"""FH-10 apply half: validated HtmlPatch → re-sanitized layer (bumped revision).

``validate_patch`` is the fail-before-write boundary tested in test_html_patch.py;
this file pins the *apply* — the reversible write that turns a validated patch
into a new ``FreeformHtmlMotionLayer`` with a bumped revision, re-hashed source
and a re-sanitized page (so an edit can never ship content the sanitizer would
have stripped), plus the ``ApplyHtmlPatchCommand`` that lands it on a scene.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.html_layer_commands import ApplyHtmlPatchCommand
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.render.html_layer.html_patch import (
    HtmlPatch,
    HtmlPatchError,
    HtmlPatchOperation,
)
from src.features.teaching_animation.render.html_layer.html_patch_apply import apply_html_patch


HTML = (
    '<style>:root{--brand:#06c;--gap:8px}</style>'
    '<div data-topos-node-id="title"><h1>Hello</h1></div>'
)


def _layer(html: str = HTML) -> "FreeformHtmlMotionLayer":  # type: ignore[name-defined]
    from src.features.teaching_animation.core.html_layer import FreeformHtmlMotionLayer
    layer = build_html_layer(
        layer_id="L", sanitized_html=html, authored_source=html,
        viewport_width=64, viewport_height=48, fps_num=24, fps_den=1, duration_frames=6)
    assert isinstance(layer, FreeformHtmlMotionLayer)
    return layer


def _patch(op: HtmlPatchOperation, target: str, value, layer, sub_target: str = "") -> HtmlPatch:
    return HtmlPatch(op, target, value, layer.revision, layer.source_hash, sub_target)


def test_replace_text_changes_content_and_bumps_revision() -> None:
    layer = _layer()
    new = apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "title", "World", layer))
    assert "World" in new.sanitized_html
    assert "Hello" not in new.sanitized_html
    assert new.revision == layer.revision + 1
    assert new.source_hash != layer.source_hash


def test_set_token_updates_css_variable_preserving_others() -> None:
    layer = _layer()
    new = apply_html_patch(layer, _patch(HtmlPatchOperation.SET_TOKEN, "brand", "#ff0000", layer))
    assert "--brand:#ff0000" in new.sanitized_html.replace(" ", "")
    # The unrelated token survives.
    assert "--gap" in new.sanitized_html


def test_set_asset_on_void_img_swaps_topos_asset_ref() -> None:
    # Asset slots most naturally live on <img> (a void element).  This used to
    # fail with UNKNOWN_NODE because _locate_element skipped void tags in its
    # target search — the void-fix lets void elements be located, so set_asset
    # now swaps the topos-asset:// id on an <img>.  Guards against regression.
    html = ('<style>.t{color:#fff}</style>'
            '<div data-topos-node-id="title">Hello</div>'
            '<img src="topos-asset://logo" data-topos-node-id="logo">')
    layer = build_html_layer(
        layer_id="L", sanitized_html=html, authored_source=html,
        viewport_width=64, viewport_height=48, fps_num=24, fps_den=1,
        duration_frames=6, bound_asset_ids=("logo", "photo"))
    new = apply_html_patch(layer, _patch(HtmlPatchOperation.SET_ASSET, "logo", "photo", layer))
    assert "topos-asset://photo" in new.sanitized_html
    assert "topos-asset://logo" not in new.sanitized_html
    # The void img tag is preserved (not mangled into a paired tag).
    assert "<img" in new.sanitized_html


def test_stale_revision_is_rejected() -> None:
    layer = _layer()
    evolved = apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "title", "x", layer))
    # A patch authored against the OLD revision must not apply to the evolved layer.
    stale = HtmlPatch(HtmlPatchOperation.REPLACE_TEXT, "title", "y", layer.revision, layer.source_hash)
    with pytest.raises(HtmlPatchError) as exc:
        apply_html_patch(evolved, stale)
    assert exc.value.code.value == "stale_revision"


def test_unknown_node_is_rejected() -> None:
    layer = _layer()
    with pytest.raises(HtmlPatchError) as exc:
        apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "ghost", "x", layer))
    assert exc.value.code.value in {"unknown_node", "precondition_mismatch"}


def test_resanitize_strips_script_injected_via_text() -> None:
    """Defence in depth: even if a patch value tries to inject markup, the result
    is re-run through the sanitizer before it ships."""
    layer = _layer()
    new = apply_html_patch(
        layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "title", "x<script>alert(1)</script>", layer))
    assert "<script" not in new.sanitized_html.lower()
    # The layer is still writable (non-empty, sanitized).
    assert new.sanitized_html.strip()


def test_motion_graphic_metadata_keeps_browser_capabilities_during_edit() -> None:
    """Editing a freeform clip must not silently downgrade it to generic HTML."""
    html = (
        '<style>@property --progress{syntax:"<number>";initial-value:0;inherits:false}'
        '.title{view-timeline-name:--page;animation-timeline:--page}</style>'
        '<script>window.__motionGraphicEdited = true;</script>'
        '<div data-topos-node-id="title">Hello</div>'
    )
    layer = build_html_layer(
        layer_id="freeform", sanitized_html=html, authored_source=html,
        viewport_width=64, viewport_height=48, fps_num=24, fps_den=1,
        duration_frames=6,
        metadata={
            "network_access": True,
            "allow_inline_scripts": True,
            "allow_event_handlers": True,
            "allow_freeform_css": True,
        },
    )
    new = apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "title", "World", layer))
    assert "<script>" in new.sanitized_html
    assert "view-timeline-name" in new.sanitized_html
    assert "World" in new.sanitized_html


def test_apply_html_patch_command_lands_on_scene_and_undo_restores() -> None:
    """ApplyHtmlPatchCommand applies the patch to the scene's layer and undoes it
    back to the previous layer exactly (FH-10 reversibility)."""
    from dataclasses import replace
    scene = replace(
        TeachingSceneDocument.create_default("s", width=64, height=48, duration_frames=6),
        html_layer=_layer())
    before = scene.html_layer
    cmd = ApplyHtmlPatchCommand(_patch(HtmlPatchOperation.SET_TOKEN, "brand", "#ff0000", before))
    applied = cmd.apply(scene)
    assert "--brand:#ff0000" in applied.html_layer.sanitized_html.replace(" ", "")
    assert applied.html_layer.revision == before.revision + 1
    restored = cmd.undo(applied)
    assert restored.html_layer is before


def test_apply_html_patch_command_requires_html_layer() -> None:
    scene = TeachingSceneDocument.create_default("s", width=64, height=48, duration_frames=6)
    cmd = ApplyHtmlPatchCommand(HtmlPatch(HtmlPatchOperation.REPLACE_TEXT, "title", "x", 1, "h"))
    # P1-RENDER-01：断言对齐结构化错误文案（命令族统一
    # "scene has no html_layer" 后缀，便于错误码检索）。
    with pytest.raises(ValueError, match="scene has no html_layer"):
        cmd.apply(scene)


def test_sanitizer_preserves_css_custom_properties() -> None:
    """Regression for the sanitizer fix that lets design tokens survive: custom
    property declarations (--name: value) are kept, not stripped."""
    from src.features.teaching_animation.render.html_layer.sanitizer import HtmlSanitizer
    out = HtmlSanitizer().sanitize(
        '<style>:root{--brand:#06c;--gap:8px;color:#fff}</style>').html
    assert "--brand:#06c" in out.replace(" ", "")
    assert "--gap:8px" in out.replace(" ", "")
    assert "color:#fff" in out.replace(" ", "")
