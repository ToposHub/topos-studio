"""Regression tests for the batch-A Critical audit fixes (w4-g3).

Covers:
  * AUD-30008 — ``_locate_element`` must recognize self-closing tags
    (``<div … />`` and the sanitizer's ``<rect/>``-style SVG geometry) with an
    *empty* inner region instead of falling back to "the rest of the
    document", which made ``replace_text`` wipe everything after the target.
    An unterminated (malformed) tag must locate as absent, never as
    rest-of-document.
  * AUD-30309 — ``RenderWorker._handles`` must evict a job when its future
    reaches a terminal state, so a long-lived worker cannot pin every
    completed job's full frame set until OOM.
  * AUD-30900 — ``_remove_page_requested`` must refuse to delete the last
    page with a visible status message instead of letting the model's
    ``ValueError`` escape into a Qt slot (unhandled slot exceptions abort
    the process).
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from dataclasses import replace

import pytest

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.core.html_layer import (
    FreeformHtmlMotionLayer,
    build_html_layer,
)
from src.features.teaching_animation.core.model import (
    ScenePage,
    TeachingSceneDocument,
)
from src.features.teaching_animation.render.html_layer.html_patch import (
    HtmlPatch,
    HtmlPatchError,
    HtmlPatchOperation,
)
from src.features.teaching_animation.render.html_layer.html_patch_apply import (
    _locate_element,
    apply_html_patch,
    read_node_inner_text,
)
from src.features.teaching_animation.render.render_worker import RenderWorker


# --------------------------------------------------------------------------- #
# AUD-30008: self-closing-aware node location
# --------------------------------------------------------------------------- #

def test_locate_element_self_closed_tag_has_empty_inner() -> None:
    html = ('<div data-topos-node-id="box"/>'
            '<div data-topos-node-id="after">tail</div>')
    span = _locate_element(html, "box")
    assert span is not None
    assert span.self_closed is True
    assert span.is_void is False
    # Empty inner region — NOT the remainder of the document.
    assert span.inner_start == span.inner_end == span.open_end
    assert span.close_end == span.open_end
    assert read_node_inner_text(html, "box") == ""


def test_locate_element_sanitizer_svg_geometry_locates_as_void() -> None:
    # The sanitizer re-emits SVG geometry self-closed (<rect/>): these must
    # locate like void elements (empty inner) instead of hunting a </rect>
    # that never exists and falling back to rest-of-document.
    html = ('<svg width="10" height="10">'
            '<rect data-topos-node-id="shape1" width="4" height="4"/>'
            '<circle data-topos-node-id="shape2" r="2"/>'
            '</svg>')
    for node_id in ("shape1", "shape2"):
        span = _locate_element(html, node_id)
        assert span is not None, node_id
        assert span.is_void is True
        assert span.inner_start == span.inner_end
        assert read_node_inner_text(html, node_id) == ""


def test_locate_element_unterminated_tag_is_absent_not_rest_of_document() -> None:
    html = '<div data-topos-node-id="x">lost tail text</div>'
    malformed = '<div data-topos-node-id="x">lost tail text'
    assert _locate_element(malformed, "x") is None
    assert read_node_inner_text(malformed, "x") is None
    # The well-formed form still locates normally.
    assert read_node_inner_text(html, "x") == "lost tail text"


def test_locate_element_self_closed_descendant_does_not_eat_parents_close() -> None:
    html = ('<div data-topos-node-id="outer">'
            '<div data-topos-node-id="inner"/>'
            '</div>'
            '<p data-topos-node-id="tail">T</p>')
    span = _locate_element(html, "outer")
    assert span is not None
    assert read_node_inner_text(html, "outer").strip() == '<div data-topos-node-id="inner"/>'


# --------------------------------------------------------------------------- #
# AUD-30008: apply-level — replace_text must not wipe the document
# --------------------------------------------------------------------------- #

def _layer(html: str) -> FreeformHtmlMotionLayer:
    return build_html_layer(
        layer_id="L", sanitized_html=html, authored_source=html,
        viewport_width=64, viewport_height=48, fps_num=24, fps_den=1, duration_frames=6)


def _patch(op: HtmlPatchOperation, target: str, value: object, layer: FreeformHtmlMotionLayer) -> HtmlPatch:
    return HtmlPatch(op, target, value, layer.revision, layer.source_hash)


def test_replace_text_on_self_closed_node_keeps_rest_of_document() -> None:
    html = ('<style>.t{color:#fff}</style>'
            '<div data-topos-node-id="box"/>'
            '<div data-topos-node-id="after"><p>tail</p></div>')
    layer = _layer(html)
    new = apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "box", "World", layer))
    # The text lands as the (now expanded) node's content …
    compact = new.sanitized_html.replace(" ", "")
    assert 'data-topos-node-id="box">World</div>' in compact
    # … and everything after the target survives (the old fallback replaced
    # the whole remainder of the document with the patch value).
    assert 'data-topos-node-id="after"' in new.sanitized_html
    assert "tail" in new.sanitized_html
    assert new.sanitized_html.rstrip().endswith("</div>")
    assert new.revision == layer.revision + 1


def test_replace_text_on_svg_geometry_is_rejected_not_wiping() -> None:
    html = ('<style>.t{color:#fff}</style>'
            '<svg width="10" height="10">'
            '<rect data-topos-node-id="shape1" width="4" height="4"/>'
            '</svg>'
            '<div data-topos-node-id="after">tail</div>')
    layer = _layer(html)
    with pytest.raises(HtmlPatchError) as exc:
        apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "shape1", "X", layer))
    assert exc.value.code.value == "edit_mode_forbids"


def test_replace_text_on_unterminated_node_raises_instead_of_wiping() -> None:
    # The sanitizer re-emits an authored <div/> as an unterminated <div>; a
    # patch against that node must fail diagnosably, not splice into "the rest
    # of the document".
    html = ('<style>.t{color:#fff}</style>'
            '<div data-topos-node-id="box">'
            '<div data-topos-node-id="after">tail</div>')
    layer = _layer(html)
    with pytest.raises(HtmlPatchError) as exc:
        apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "box", "X", layer))
    assert exc.value.code.value == "unknown_node"


# --------------------------------------------------------------------------- #
# AUD-30309: worker-side handle eviction on job completion
# --------------------------------------------------------------------------- #

def _tiny_scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(
        "aud-30309", scene_id="aud-30309", width=64, height=48, duration_frames=2)


def _wait_until_evicted(worker: RenderWorker, timeout: float = 10.0) -> None:
    # add_done_callback may run a hair after a result() waiter wakes up.
    deadline = time.monotonic() + timeout
    while worker._handles and time.monotonic() < deadline:
        time.sleep(0.02)


def test_completed_job_is_evicted_from_worker_handles() -> None:
    scene = _tiny_scene()
    context = RenderContext(fps=24, width=64, height=48)
    worker = RenderWorker(max_workers=1)
    try:
        handle = worker.submit(scene, (0, 1), context)
        result = handle.result(timeout=30)
        assert result.frames
        _wait_until_evicted(worker)
        # The worker no longer pins the finished job's frames.
        assert worker._handles == {}
        # The caller's own handle remains fully usable after eviction.
        assert handle.done is True
        assert len(handle.result().frames) == 2
    finally:
        worker.shutdown()


def test_cancelled_job_is_evicted_from_worker_handles() -> None:
    scene = TeachingSceneDocument.create_default(
        "aud-30309-cancel", scene_id="aud-30309-cancel", width=64, height=48, duration_frames=8)
    context = RenderContext(fps=24, width=64, height=48)

    def cancel_immediately(progress: object) -> bool:
        return True

    worker = RenderWorker(max_workers=1)
    try:
        handle = worker.submit(scene, range(scene.duration_frames), context, cancel_immediately)
        result = handle.result(timeout=30)
        assert result.cancelled or len(result.frames) < scene.duration_frames
        _wait_until_evicted(worker)
        assert worker._handles == {}
    finally:
        worker.shutdown()


# --------------------------------------------------------------------------- #
# AUD-30900: removing the last page is refused visibly, not fatally
# --------------------------------------------------------------------------- #

def _single_page_scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(
        "aud-30900", width=64, height=48, duration_frames=24)


def _two_page_scene() -> TeachingSceneDocument:
    return replace(
        TeachingSceneDocument.create_default(
            "aud-30900-two", width=64, height=48, duration_frames=48),
        pages=(ScenePage(page_id="p1", duration_frames=24),
               ScenePage(page_id="p2", duration_frames=24)),
    )


class TestRemoveLastPageGuard:
    def test_remove_last_page_is_refused_with_status_message(self, qapp) -> None:  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.ui.motion_graphic_editor_window import (
            MotionGraphicEditorWindow,
        )
        scene = _single_page_scene()
        only_page_id = scene.page_ids[0]
        window = MotionGraphicEditorWindow(scene)
        try:
            window.editor_controller.mark_clean()
            # Must not raise: an unhandled exception inside a Qt slot aborts
            # the process (AUD-30900).
            window._remove_page_requested(only_page_id)
            assert len(window.editor_controller.scene.pages) == 1
            assert window.editor_controller.scene.page_by_id(only_page_id) is not None
            assert window.statusBar().currentMessage().strip()
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_remove_page_still_removes_when_other_pages_remain(self, qapp) -> None:  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.ui.motion_graphic_editor_window import (
            MotionGraphicEditorWindow,
        )
        window = MotionGraphicEditorWindow(_two_page_scene())
        try:
            window.editor_controller.mark_clean()
            window._remove_page_requested("p1")
            assert [page.page_id for page in window.editor_controller.scene.pages] == ["p2"]
        finally:
            window.editor_controller.mark_clean()
            window.close()
