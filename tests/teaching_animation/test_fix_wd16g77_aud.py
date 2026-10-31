"""Regression tests for remediation batch D (wd16-g77, teaching render v2 / scene domain).

Covers the Major findings fixed in this batch:

* AUD-30301  _append_validation_background locates </head> on the original
             document (no lower()-index offset from Unicode case expansion)
* AUD-30304  blank-capture classification is structural (HtmlBlankCaptureError)
             instead of English message substrings
* AUD-30311  _html_with_native_overlay locates </body> on the original document
* AUD-30401  precomp instances expand recursively (cycle + depth guarded)
* AUD-30402  a mask targeting an instance id masks the instance <g>
* AUD-30403  unsupported effects in a referenced scene fail closed in final
             profile, same as the parent scene
* AUD-30404  image data URIs resolve once per asset; nested resolvers are
             built once per referenced scene, not once per frame
* AUD-30405  effect/gradient definition ids are namespaced per instance so
             parent and nested scenes cannot collide inside one SVG
* AUD-30410  SvgSceneRendererV2.render honours the protocol cancel_token
* AUD-30501  shared cache coordinator lazy init is lock-protected
* AUD-30504  store revisions come from a process-wide monotonic sequence and
             never repeat across replace_all()/clear()
* AUD-30600  "regenerate preview" renders the GIF off the UI thread
* AUD-30610  a keyframe that does not fit the existing track widens the range
             instead of being silently clamped at evaluation time
* AUD-30611  drag + auto-key is one undo record, not up to six
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from src.features.teaching_animation.core.composition_schema import (
    Mask,
    MaskKind,
    SceneInstance,
)
from src.features.teaching_animation.core.contracts import (
    AnimationFrame,
    CanvasSpace,
    FrameContext,
    RenderContext,
    RenderError,
    RenderErrorCode,
    RenderSurface,
)
from src.features.teaching_animation.core.model import (
    SceneAssetRef,
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.render.asset_resolver import AssetResolver
from src.features.teaching_animation.render.html_scene_renderer_v2 import (
    HtmlBlankCaptureError,
    HtmlRenderError,
    _append_validation_background,
)
from src.features.teaching_animation.render.html_validation import (
    _is_blank_capture_error,
    validate_html_scene,
)
from src.features.teaching_animation.render.scene_renderer import RenderCancelled
from src.features.teaching_animation.render.scene_renderer_router import (
    _html_with_native_overlay,
)
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer
from src.features.teaching_animation.render.v2_adapter import SvgSceneRendererV2
from src.features.teaching_animation.scene_cache import get_shared_scene_cache_coordinator
from src.features.teaching_animation.scene_store import (
    TeachingSceneStore,
    get_shared_scene_store,
)
from src.features.teaching_animation.ui.editor_controller import SceneEditorController


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _doc(
    scene_id: str,
    elements: tuple[SceneElement, ...],
    *,
    metadata: dict | None = None,
    asset_refs: tuple[SceneAssetRef, ...] = (),
    duration: int = 24,
) -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id=scene_id, schema_version=3, name=scene_id, width=320, height=180,
        fps_num=24, fps_den=1, duration_frames=duration, background={"color": "#111827"},
        animations=(), elements=elements, metadata=metadata or {}, asset_refs=asset_refs,
    )


def _text(element_id: str, label: str) -> SceneElement:
    return SceneElement(
        element_id, "text", transform=SceneTransform(10.0, 10.0, 200.0, 40.0),
        content={"text": label}, style={},
    )


@pytest.fixture()
def shared_store():
    """The process-wide scene store, with its contents restored afterwards."""
    store = get_shared_scene_store()
    snapshot = store.scenes()
    yield store
    store.replace_all(snapshot)


# ---------------------------------------------------------------------------
# AUD-30301 — validation background insertion offset
# ---------------------------------------------------------------------------

_VALIDATION_STYLE = (
    '<style id="topos-html-validation-background">'
    'html,body{background:#101827 !important;}'
    '</style>'
)


def test_validation_background_inserts_before_head_with_case_expanding_char():
    # 'İ' (U+0130) lowercases to 2 characters, shifting any index taken on
    # document.lower() past the position in the original document.
    document = '<html><head><title>İ</title></head><body><p>ok</p></body></html>'
    result = _append_validation_background(document, "#101827")
    closing = document.rfind("</head>")
    assert result == document[:closing] + _VALIDATION_STYLE + document[closing:]
    # The </head> tag itself stays intact and terminates the document's head.
    assert "</head><body>" in result.replace(_VALIDATION_STYLE, "")


def test_validation_background_matches_case_insensitive_last_head():
    document = '<html><head></head><head >İ</HEAD ><body></body></html>'
    result = _append_validation_background(document, "#101827")
    closing = document.rfind("</HEAD >")
    assert result == document[:closing] + _VALIDATION_STYLE + document[closing:]


def test_validation_background_without_head_prepends_and_none_is_identity():
    assert _append_validation_background("<p>İ</p>", "#101827") == _VALIDATION_STYLE + "<p>İ</p>"
    document = "<html><head>İ</head></html>"
    assert _append_validation_background(document, None) is document
    assert _append_validation_background(document, "") is document


# ---------------------------------------------------------------------------
# AUD-30304 — structural blank-capture classification
# ---------------------------------------------------------------------------

def _render_error(message: str, code: RenderErrorCode = RenderErrorCode.RENDER_FAILED) -> RenderError:
    return RenderError(code, message, frame=0)


def test_blank_capture_classifier_is_structural():
    blank = HtmlBlankCaptureError(_render_error("capture fehlgeschlagen"))
    assert blank.blank_capture is True
    assert _is_blank_capture_error(blank) is True
    # A structured render error that merely *mentions* uniform frames is NOT
    # a blank capture — the old substring match misclassified both ways.
    structured = HtmlRenderError(_render_error("capture returned uniform frame (fake)"))
    assert _is_blank_capture_error(structured) is False
    # Plain exceptions without a structured marker keep the legacy contract
    # (injected test doubles and legacy raisers).
    assert _is_blank_capture_error(RuntimeError("capture returned uniform frame")) is True
    assert _is_blank_capture_error(RuntimeError("near-uniform frame 3")) is True
    assert _is_blank_capture_error(RuntimeError("timeout")) is False


def _html_gate_scene():
    return SimpleNamespace(
        scene_id="wd16-gate", fps=24.0, width=320, height=180, duration_frames=48,
        html_layer=SimpleNamespace(
            sanitized_html="<html><body><h1>Deck</h1></body></html>",
            metadata={}, viewport_width=320, viewport_height=180,
        ),
    )


def test_gate_retries_structured_blank_capture_without_matching_words():
    def render(_scene_id, _frame, *, context):
        raise HtmlBlankCaptureError(_render_error("leerer frame"))

    with pytest.raises(RuntimeError, match="HTML 验证捕获为空"):
        validate_html_scene(
            _html_gate_scene(),
            context=RenderContext(fps=24, width=64, height=36, transparent=True),
            render_frame=render,
        )


def test_gate_propagates_structured_non_blank_mentioning_uniform():
    calls: list[int] = []

    def render(_scene_id, frame, *, context):
        calls.append(frame)
        raise HtmlRenderError(_render_error("capture returned uniform frame (not really)"))

    with pytest.raises(HtmlRenderError):
        validate_html_scene(
            _html_gate_scene(),
            context=RenderContext(fps=24, width=64, height=36, transparent=True),
            render_frame=render,
        )
    # Failed on the very first candidate instead of silently retrying.
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# AUD-30311 — native overlay insertion offset
# ---------------------------------------------------------------------------

def _overlay_markup(payload: str) -> str:
    return (
        '<div id="topos-native-vector-overlay" '
        'style="position:fixed;inset:0;width:100%;height:100%;pointer-events:none;'
        f'z-index:2147483647;">{payload}</div>'
    )


def test_native_overlay_inserts_before_body_with_case_expanding_char():
    document = '<html><body><p>İ content</p></body></html>'
    result = _html_with_native_overlay(document, "X")
    closing = document.rfind("</body>")
    assert result == document[:closing] + _overlay_markup("X") + document[closing:]
    assert result.endswith("</body></html>")


def test_native_overlay_matches_last_body_case_insensitive():
    document = '<html><body>İ</BODY ><span>x</span></body></html>'
    result = _html_with_native_overlay(document, "Y")
    closing = document.rfind("</body>")
    assert result == document[:closing] + _overlay_markup("Y") + document[closing:]


def test_native_overlay_without_body_appends():
    document = "<div>İ</div>"
    assert _html_with_native_overlay(document, "Z") == document + _overlay_markup("Z")


# ---------------------------------------------------------------------------
# AUD-30401 / 30402 / 30403 / 30404 / 30405 — SVG precomp rendering
# ---------------------------------------------------------------------------

def _render_svg(
    scene: TeachingSceneDocument,
    *,
    profile: str = "preview",
    frames: tuple[int, ...] = (0,),
    asset_root: str | None = None,
) -> list[str]:
    renderer = SvgSceneRenderer()
    renderer.prepare(
        scene.to_dict(),
        RenderContext(fps=24.0, width=320, height=180, render_profile=profile,
                      asset_root=asset_root),
    )
    try:
        return [renderer.render_frame(AnimationFrame(frame, 24.0)).data.decode("utf-8")
                for frame in frames]
    finally:
        renderer.cleanup()


def _instances_meta(*instances: SceneInstance, masks: tuple[Mask, ...] = ()) -> dict:
    meta: dict = {"scene_instances": [item.to_dict() for item in instances]}
    if masks:
        meta["scene_masks"] = [item.to_dict() for item in masks]
    return meta


def test_precomp_instances_recurse_beyond_one_level(shared_store):
    deep = _doc("wd16_c", (_text("c_elem", "nested-c"),))
    mid = _doc(
        "wd16_b", (_text("b_elem", "nested-b"),),
        metadata=_instances_meta(SceneInstance(instance_id="inst_c", source_scene_id="wd16_c")),
    )
    top = _doc(
        "wd16_a", (_text("a_elem", "top-a"),),
        metadata=_instances_meta(SceneInstance(instance_id="inst_b", source_scene_id="wd16_b")),
    )
    shared_store.replace_all((deep, mid, top))
    svg = _render_svg(top)[0]
    assert 'data-element-id="b_elem"' in svg
    # The second-level instance (inside the referenced scene) must render too;
    # it used to be silently dropped by the one-level expansion.
    assert 'data-element-id="c_elem"' in svg
    assert 'data-instance-id="inst_b"' in svg
    assert 'data-instance-id="inst_c"' in svg


def test_precomp_reference_cycles_terminate_without_dropping_siblings(shared_store):
    b = _doc(
        "wd16_cyc_b", (_text("cyc_b", "b"),),
        metadata=_instances_meta(SceneInstance(instance_id="inst_a", source_scene_id="wd16_cyc_a")),
    )
    a = _doc(
        "wd16_cyc_a", (_text("cyc_a", "a"),),
        metadata=_instances_meta(SceneInstance(instance_id="inst_b", source_scene_id="wd16_cyc_b")),
    )
    shared_store.replace_all((a, b))
    svg = _render_svg(a)[0]
    assert 'data-element-id="cyc_a"' in svg
    assert 'data-element-id="cyc_b"' in svg
    # The cycle back to the root (B declares an instance of A) is skipped by
    # the visited set — not expanded, so rendering terminates.
    assert svg.count('data-instance-id="inst_a"') == 0
    assert 'data-instance-id="inst_b"' in svg


def test_precomp_depth_cap_fails_closed(shared_store):
    chain: list[TeachingSceneDocument] = []
    total = 20
    for index in range(total):
        metadata: dict = {}
        if index + 1 < total:
            metadata = _instances_meta(
                SceneInstance(instance_id=f"inst_{index}", source_scene_id=f"wd16_deep_{index + 1}"),
            )
        chain.append(_doc(f"wd16_deep_{index}", (_text(f"deep_{index}", "d"),), metadata=metadata))
    shared_store.replace_all(tuple(chain))
    with pytest.raises(RuntimeError, match="precomp instance nesting exceeds"):
        _render_svg(chain[0])


def test_mask_bound_to_instance_masks_the_instance_group(shared_store):
    nested = _doc("wd16_mask_b", (_text("mask_b", "b"),))
    top = _doc(
        "wd16_mask_a", (_text("mask_a", "a"),),
        metadata=_instances_meta(
            SceneInstance(instance_id="inst_masked", source_scene_id="wd16_mask_b"),
            masks=(Mask(mask_id="m1", target="inst_masked", kind=MaskKind.ALPHA,
                        path_ref="M0,0L100,0L100,100L0,100Z"),),
        ),
    )
    shared_store.replace_all((nested, top))
    svg = _render_svg(top)[0]
    assert 'data-instance-id="inst_masked"' in svg
    # The instance wrapper references the mask definition instead of
    # rendering unmasked.
    assert 'mask="url(#mask_m1)"' in svg
    assert '<mask id="mask_m1"' in svg


def test_final_profile_blocked_effect_in_nested_scene_fails_closed(shared_store):
    unsupported = {"effects": [{"effect_type": "motion_blur", "params": {}}]}
    nested = _doc(
        "wd16_fx_b",
        (SceneElement("fx_b", "text", transform=SceneTransform(0, 0, 100, 40),
                      content={"text": "b"}, style=unsupported),),
    )
    top = _doc(
        "wd16_fx_a", (_text("fx_a", "a"),),
        metadata=_instances_meta(SceneInstance(instance_id="inst_fx", source_scene_id="wd16_fx_b")),
    )
    shared_store.replace_all((nested, top))
    # Preview profile: dropped silently, renders fine (unchanged behaviour).
    assert 'data-element-id="fx_b"' in _render_svg(top, profile="preview")[0]
    # Final profile: the referenced scene's blocked effect fails closed
    # exactly like the parent scene would.
    with pytest.raises(RuntimeError, match="unsupported SVG effect in final profile"):
        _render_svg(top, profile="final")


def test_image_data_uri_resolved_once_across_frames(shared_store, tmp_path, monkeypatch):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG-not-really-but-irrelevant-bytes")
    asset = SceneAssetRef(asset_id="a1", kind="image", path="photo.png")
    element = SceneElement(
        "img1", "image", transform=SceneTransform(0.0, 0.0, 100.0, 80.0),
        content={}, style={}, asset_ref="a1",
    )
    scene = _doc("wd16_io", (element,), asset_refs=(asset,))
    shared_store.replace_all((scene,))

    calls: list[str] = []
    original = AssetResolver.data_uri

    def counting(self: AssetResolver, asset_ref: SceneAssetRef) -> str | None:
        calls.append(asset_ref.asset_id)
        return original(self, asset_ref)

    monkeypatch.setattr(AssetResolver, "data_uri", counting)
    frames = _render_svg(scene, frames=(0, 6, 12, 18), asset_root=str(tmp_path))
    assert len(frames) == 4
    assert calls.count("a1") == 1  # encoded once, reused for the other frames


def test_nested_resolver_built_once_across_frames(shared_store, monkeypatch):
    nested = _doc("wd16_res_b", (_text("res_b", "b"),))
    top = _doc(
        "wd16_res_a", (_text("res_a", "a"),),
        metadata=_instances_meta(SceneInstance(instance_id="inst_res", source_scene_id="wd16_res_b")),
    )
    shared_store.replace_all((nested, top))

    constructions: list[str] = []
    original_init = AssetResolver.__init__

    def counting_init(self: AssetResolver, context: RenderContext) -> None:
        constructions.append("resolver")
        original_init(self, context)

    monkeypatch.setattr(AssetResolver, "__init__", counting_init)
    _render_svg(top, frames=(0, 6, 12, 18))
    # One for the parent (prepare) + one for the referenced scene — not one
    # per instance per frame.
    assert len(constructions) == 2


def test_definition_ids_namespaced_across_scenes(shared_store):
    style = {
        "effects": [{"effect_type": "blur", "params": {"radius": 4.0}}],
        "gradient": {"kind": "linear", "stops": [
            {"position": 0.0, "color": "#ff0000"},
            {"position": 1.0, "color": "#0000ff"},
        ]},
    }
    parent_element = SceneElement(
        "shape_1", "shape", transform=SceneTransform(0.0, 0.0, 100.0, 100.0),
        content={"kind": "rect"}, style=style,
    )
    nested_element = SceneElement(
        "shape_1", "shape", transform=SceneTransform(0.0, 0.0, 100.0, 100.0),
        content={"kind": "rect"}, style=style,
    )
    nested = _doc("wd16_ns_b", (nested_element,))
    top = _doc(
        "wd16_ns_a", (parent_element,),
        metadata=_instances_meta(SceneInstance(instance_id="inst1", source_scene_id="wd16_ns_b")),
    )
    shared_store.replace_all((nested, top))
    svg = _render_svg(top)[0]
    # Parent keeps the legacy ids; the nested subtree is namespaced.
    assert 'id="effect_shape_1"' in svg
    assert 'id="effect_inst1__shape_1"' in svg
    assert 'id="gradient_shape_1"' in svg
    assert 'id="gradient_inst1__shape_1"' in svg
    # The nested element references its own filter, not the parent's.
    assert 'filter="url(#effect_inst1__shape_1)"' in svg


# ---------------------------------------------------------------------------
# AUD-30410 — V2 adapter cancel token
# ---------------------------------------------------------------------------

def _frame_context(frame: int) -> FrameContext:
    return FrameContext(
        frame=frame, fps_num=24, fps_den=1,
        canvas=CanvasSpace.from_dimensions(320, 180),
        surface=RenderSurface(pixel_width=320, pixel_height=180),
    )


def test_v2_render_honours_protocol_cancel_token(qapp):
    renderer = SvgSceneRendererV2()
    session = renderer.prepare(_doc("wd16_v2", (_text("v2", "v"),)), _frame_context(0))
    try:
        with pytest.raises(RenderCancelled):
            renderer.render(session, _frame_context(0),
                            cancel_token=SimpleNamespace(is_cancelled=True))
        # A live token does not block rendering.
        result = renderer.render(session, _frame_context(0),
                                 cancel_token=SimpleNamespace(is_cancelled=False))
        assert result.frame.width == 320
    finally:
        renderer.close(session)


def test_v2_render_cancel_token_is_a_runtime_error_subclass(qapp):
    renderer = SvgSceneRendererV2()
    session = renderer.prepare(_doc("wd16_v2b", (_text("v2b", "v"),)), _frame_context(0))
    try:
        with pytest.raises(RuntimeError):
            renderer.render(session, _frame_context(0),
                            cancel_token=SimpleNamespace(is_cancelled=True))
    finally:
        renderer.close(session)


# ---------------------------------------------------------------------------
# AUD-30501 — coordinator lazy init race
# ---------------------------------------------------------------------------

def test_shared_coordinator_concurrent_init_builds_exactly_one(monkeypatch):
    import src.features.teaching_animation.scene_cache as scene_cache_module

    built: list[int] = []

    class _SlowCoordinator:
        def __init__(self) -> None:
            built.append(1)
            # Widen the race window the lock must close.
            import time
            time.sleep(0.05)

    previous = scene_cache_module._SHARED_COORDINATOR
    monkeypatch.setattr(scene_cache_module, "TeachingSceneCacheCoordinator", _SlowCoordinator)
    monkeypatch.setattr(scene_cache_module, "_SHARED_COORDINATOR", None)
    try:
        barrier = threading.Barrier(8)
        results: list[object] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            coordinator = get_shared_scene_cache_coordinator()
            with lock:
                results.append(id(coordinator))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        assert len(built) == 1
        assert len(set(results)) == 1
    finally:
        scene_cache_module._SHARED_COORDINATOR = previous


# ---------------------------------------------------------------------------
# AUD-30504 — revision monotonicity across replace_all / clear
# ---------------------------------------------------------------------------

def test_revisions_never_repeat_across_project_switch():
    store = TeachingSceneStore()
    scene = _doc("wd16_rev", (_text("rev", "r"),))
    first = store.revision(scene.scene_id)
    assert first == 0  # not registered yet
    store.register(scene)
    r1 = store.revision(scene.scene_id)
    assert r1 >= 1  # process-monotonic: not necessarily 1, never repeated
    # Simulate closing the project and reopening another one that reuses the
    # same scene id: the new revision must never collide with the old one.
    store.replace_all(())
    assert store.revision(scene.scene_id) == 0
    store.register(scene)
    r2 = store.revision(scene.scene_id)
    assert r2 > r1
    store.clear()
    store.register(scene)
    r3 = store.revision(scene.scene_id)
    assert r3 > r2


def test_revisions_bump_on_change_and_not_on_identity(shared_store):
    store = TeachingSceneStore()
    scene = _doc("wd16_rev2", (_text("a", "a"),))
    store.register(scene)
    r1 = store.revision(scene.scene_id)
    store.register(scene)  # identical document: no bump, no duplicate notify
    assert store.revision(scene.scene_id) == r1
    moved = _doc("wd16_rev2", (_text("b", "b"),))
    store.register(moved)
    assert store.revision(scene.scene_id) == r1 + 1


# ---------------------------------------------------------------------------
# AUD-30600 — off-thread preview regeneration
# ---------------------------------------------------------------------------

def _library_panel(qapp, monkeypatch):
    from src.features.teaching_animation.presets import MgPresetLibrary, bundled_preset_root
    from src.features.teaching_animation.ui import asset_library_panel

    panel = asset_library_panel.AssetLibraryPanel(library=MgPresetLibrary([bundled_preset_root()]))
    record = panel._library.records()[0]
    panel.select_preset(record.preset_id)
    assert panel.current_preset_id() == record.preset_id
    return panel, record, asset_library_panel


def test_regenerate_preview_renders_off_ui_thread(qapp, monkeypatch):
    panel, record, module = _library_panel(qapp, monkeypatch)
    assert panel._regenerate_button.isEnabled()

    render_threads: list[int] = []
    refreshed: list[int] = []

    def fake_load(manifest, folder):
        return SimpleNamespace(preset=manifest)

    def fake_render(scene, out_path):
        render_threads.append(threading.get_ident())

    monkeypatch.setattr(module, "load_preset_scene", fake_load)
    monkeypatch.setattr(module, "render_preset_gif", fake_render)
    monkeypatch.setattr(panel, "_refresh_item_thumbnail", lambda: refreshed.append(1))

    panel._regenerate_preview()
    # The handler returned immediately; the button is disabled while running.
    assert panel._regenerate_button.isEnabled() is False
    job = panel._preview_job
    assert job is not None
    assert job.wait(10_000)
    qapp.processEvents()
    # The GIF render ran exactly once, on a worker thread — never the UI thread.
    assert len(render_threads) == 1
    assert render_threads[0] != threading.get_ident()
    assert refreshed == [1]
    assert panel._preview_job is None
    assert panel._regenerate_button.isEnabled() is True


def test_regenerate_preview_failure_surfaces_off_thread(qapp, monkeypatch):
    panel, record, module = _library_panel(qapp, monkeypatch)
    warnings: list[tuple[str, str]] = []

    def fake_load(manifest, folder):
        return SimpleNamespace(preset=manifest)

    def fake_render(scene, out_path):
        raise ValueError("no preview frames rendered")

    monkeypatch.setattr(module, "load_preset_scene", fake_load)
    monkeypatch.setattr(module, "render_preset_gif", fake_render)
    monkeypatch.setattr(module.QMessageBox, "warning",
                        lambda parent, title, text: warnings.append((title, text)))

    panel._regenerate_preview()
    job = panel._preview_job
    assert job is not None
    assert job.wait(10_000)
    qapp.processEvents()
    assert warnings and "no preview frames rendered" in warnings[0][1]
    assert panel._preview_job is None
    assert panel._regenerate_button.isEnabled() is True


# ---------------------------------------------------------------------------
# AUD-30610 / AUD-30611 — auto-key semantics
# ---------------------------------------------------------------------------

def _controller() -> tuple[SceneEditorController, str]:
    scene = _doc("wd16_ctrl", (_text("el", "e"),))
    return SceneEditorController(scene), "el"


def test_keyframe_outside_existing_track_range_widens_the_range():
    controller, element_id = _controller()
    # Create a narrow [0, 1] track, e.g. left behind by an AI set_animation.
    controller.add_keyframe(element_id, "x", 0, 0.5)
    controller.add_keyframe(element_id, "x", 12, 850.0)
    animation = next(item for item in controller.scene.animations
                     if item.element_id == element_id and item.property == "x")
    assert animation.track.min_val == 0.0
    assert animation.track.max_val == 850.0
    # What is authored is what renders — no silent clamping at evaluation.
    assert animation.track.get_value(12) == 850.0


def test_keyframe_range_widening_respects_caller_bounds_and_never_narrows():
    controller, element_id = _controller()
    wide = {"min_val": -1_000_000.0, "max_val": 1_000_000.0}
    controller.add_keyframe(element_id, "y", 0, 0.5)
    controller.add_keyframe(element_id, "y", 6, 1200.0, **wide)
    animation = next(item for item in controller.scene.animations
                     if item.element_id == element_id and item.property == "y")
    assert animation.track.min_val == -1_000_000.0
    assert animation.track.max_val == 1_000_000.0
    # A later in-range keyframe does not shrink the widened range.
    controller.add_keyframe(element_id, "y", 12, 3.0)
    animation = next(item for item in controller.scene.animations
                     if item.element_id == element_id and item.property == "y")
    assert animation.track.min_val == -1_000_000.0
    assert animation.track.max_val == 1_000_000.0


def test_drag_with_auto_key_is_one_undo_record():
    controller, element_id = _controller()
    controller.set_auto_key(True)
    controller.set_frame(6)
    original = controller.scene
    controller.begin_preview(element_id)
    controller.preview_element(element_id, {"transform": SceneTransform(120.0, 240.0, 200.0, 40.0)})
    diff = controller.commit_preview(element_id)
    assert diff is not None
    # The drag animated x and y…
    animated = {(item.element_id, item.property) for item in controller.scene.animations}
    assert (element_id, "x") in animated and (element_id, "y") in animated
    x_track = next(item.track for item in controller.scene.animations
                   if item.element_id == element_id and item.property == "x")
    assert x_track.get_keyframe_at(6) is not None
    assert x_track.get_value(6) == 120.0
    # …as ONE undo record: a single undo returns to the pre-drag document
    # (transform AND keyframes), and there is nothing further to undo.
    assert len(controller._stack._undo_stack) == 1
    controller.undo()
    assert controller.scene == original
    assert controller.undo() is None


def test_drag_without_auto_key_still_single_undo():
    controller, element_id = _controller()
    controller.set_frame(6)
    controller.begin_preview(element_id)
    controller.preview_element(element_id, {"transform": SceneTransform(15.0, 25.0, 200.0, 40.0)})
    controller.commit_preview(element_id)
    assert controller.scene.animations == ()
    assert len(controller._stack._undo_stack) == 1
    assert controller.undo() is not None
    assert controller.undo() is None
