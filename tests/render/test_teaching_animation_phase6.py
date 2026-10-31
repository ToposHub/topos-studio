from pathlib import Path

import pytest

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.export_service import ExportOptions, TeachingAnimationExportService, TeachingExportError
from src.features.teaching_animation.facade import offline_scene_frame_fallback
from src.features.teaching_animation.render.cache import SceneRenderCache, build_cache_manifest
from src.features.teaching_animation.render.render_worker import RenderWorker


def _scene(name: str = "Cache lesson") -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(name, width=48, height=32, duration_frames=6)


def test_cache_manifest_contains_scene_renderer_and_asset_identity() -> None:
    scene = _scene()
    context = RenderContext(fps=24, width=48, height=32)
    first = build_cache_manifest(scene, 0, context)
    second = build_cache_manifest(TeachingSceneDocument.create_default("Other", width=48, height=32, duration_frames=6), 0, context)
    assert first.scene_hash != second.scene_hash
    assert first.cache_key != second.cache_key
    assert first.to_dict()["renderer_version"] == context.renderer_version


def test_cache_key_is_sensitive_to_renderer_build_and_font_set() -> None:
    """renderer_build + font_set_hash participate in the cache key (plan §3.1):
    a renderer or packaged-font upgrade must invalidate stale frames rather than
    serve bytes captured under a different build/font set."""
    from src.features.teaching_animation.render.cache import RenderCacheManifest
    base = dict(scene_id="s", scene_hash="h", renderer_version="v", backend="html",
                frame=0, width=48, height=32)
    m = RenderCacheManifest(**base, renderer_build="b1", font_set_hash="f1")
    assert m.cache_key != RenderCacheManifest(**base, renderer_build="b2", font_set_hash="f1").cache_key
    assert m.cache_key != RenderCacheManifest(**base, renderer_build="b1", font_set_hash="f2").cache_key
    # Idempotent: identical inputs produce an identical key.
    assert RenderCacheManifest(**base, renderer_build="b1", font_set_hash="f1").cache_key == m.cache_key


def test_cache_key_is_sensitive_to_dpr() -> None:
    """DPR participates in the cache key (determinism contract §3.1): two renders
    at the same physical dimensions but a different DPR rasterise a different CSS
    layout, so they must not collide.  ``build_cache_manifest`` threads the
    context's DPR through; ``RenderContext`` defaults it to 1.0."""
    from src.features.teaching_animation.render.cache import RenderCacheManifest
    base = dict(scene_id="s", scene_hash="h", renderer_version="v", backend="html",
                frame=0, width=48, height=32)
    one_x = RenderCacheManifest(**base, dpr=1.0)
    two_x = RenderCacheManifest(**base, dpr=2.0)
    assert one_x.cache_key != two_x.cache_key, "DPR change must invalidate the cache key"
    assert RenderCacheManifest(**base, dpr=1.0).cache_key == one_x.cache_key
    # build_cache_manifest sources DPR from the RenderContext.
    scene = _scene()
    ctx_1x = RenderContext(fps=24, width=48, height=32)
    ctx_2x = RenderContext(fps=24, width=48, height=32, dpr=2.0)
    assert build_cache_manifest(scene, 0, ctx_1x).cache_key != build_cache_manifest(scene, 0, ctx_2x).cache_key
    assert build_cache_manifest(scene, 0, ctx_1x).dpr == 1.0
    assert build_cache_manifest(scene, 0, ctx_2x).dpr == 2.0


def test_cache_key_is_sensitive_to_declared_and_bound_asset_ids() -> None:
    """Declared/bound asset identity participates in the cache key (W2 audit
    D-4): a bound image that is undeclared/missing renders the deterministic
    1×1 placeholder and the frame is cached; when the asset is later declared
    and resolved, the key must change so the stale placeholder is never served.
    """
    from dataclasses import replace
    from src.features.teaching_animation.core.html_layer import build_html_layer
    from src.features.teaching_animation.core.model import SceneAssetRef
    base = TeachingSceneDocument.create_default("html", width=48, height=32, duration_frames=6)
    layer = build_html_layer(
        layer_id="L", sanitized_html="<p>x</p>", authored_source="<p>x</p>",
        viewport_width=48, viewport_height=32, fps_num=24, fps_den=1, duration_frames=6,
        bound_asset_ids=("image/logo",))
    scene_bound = replace(base, html_layer=layer)
    scene_plain = replace(base, html_layer=build_html_layer(
        layer_id="L", sanitized_html="<p>x</p>", authored_source="<p>x</p>",
        viewport_width=48, viewport_height=32, fps_num=24, fps_den=1, duration_frames=6))
    ctx = RenderContext(fps=24, width=48, height=32, backend="html")
    m_bound = build_cache_manifest(scene_bound, 0, ctx)
    m_plain = build_cache_manifest(scene_plain, 0, ctx)
    assert "image/logo" in m_bound.declared_asset_ids
    assert m_plain.declared_asset_ids == ()
    assert m_bound.cache_key != m_plain.cache_key
    # Declaring a scene asset_ref also changes the declared identity.  Use a
    # non-strict (preview) context: the asset is unresolved, but the manifest
    # still records the declared id so a later resolution invalidates the key.
    ctx_nonstrict = RenderContext(fps=24, width=48, height=32, backend="html", strict_assets=False)
    scene_with_ref = replace(base, asset_refs=(SceneAssetRef(asset_id="image/photo", path="photo.png", kind="image"),))
    assert "image/photo" in build_cache_manifest(scene_with_ref, 0, ctx_nonstrict).declared_asset_ids


def test_export_preflight_blocks_unsafe_html_layer() -> None:
    """The sanitizer + HTML quality gate ran at generation time, but a loaded/
    migrated/hand-edited layer flows to the renderer verbatim unless re-gated.
    Export preflight re-runs the gate and fail-closes on ERROR findings (a
    tampered layer with a surviving <script> must never ship). AQ-10 / §7.2 #3."""
    from dataclasses import replace
    from src.features.teaching_animation.core.html_layer import build_html_layer
    from src.features.teaching_animation.core.model import TeachingSceneDocument
    base = TeachingSceneDocument.create_default("html", width=48, height=32, duration_frames=6)
    clean_layer = build_html_layer(
        layer_id="L", sanitized_html="<p>safe</p>", authored_source="<p>safe</p>",
        viewport_width=48, viewport_height=32, fps_num=24, fps_den=1, duration_frames=6)
    clean_scene = replace(base, html_layer=clean_layer)
    service = TeachingAnimationExportService()
    try:
        # A clean, sanitized layer passes the export quality gate.
        service.preflight(clean_scene, ExportOptions())
        # A tampered layer (script survived into the stored sanitized_html) is
        # caught by the fixed-point re-scan and blocks the export.
        tampered = replace(clean_layer, sanitized_html="<p>x</p><script>alert(1)</script>")
        tampered_scene = replace(base, html_layer=tampered)
        with pytest.raises(TeachingExportError, match="quality gate"):
            service.preflight(tampered_scene, ExportOptions())
        # A non-HTML scene has no layer to gate; preflight is unaffected.
        service.preflight(_scene(), ExportOptions())
    finally:
        service.close()


def test_build_cache_manifest_carries_renderer_build_and_html_font_set() -> None:
    """build_cache_manifest threads renderer_build through and populates
    font_set_hash for HTML scenes (the font lock makes pixels font-dependent);
    non-HTML scenes have no font lock so the field stays empty."""
    from dataclasses import replace
    from src.features.teaching_animation.core.html_layer import build_html_layer
    base = TeachingSceneDocument.create_default("html", width=48, height=32, duration_frames=6)
    scene = replace(base, html_layer=build_html_layer(
        layer_id="L", sanitized_html="<p>x</p>", authored_source="<p>x</p>",
        viewport_width=48, viewport_height=32, fps_num=24, fps_den=1, duration_frames=6))
    ctx_html = RenderContext(fps=24, width=48, height=32, backend="html")
    m = build_cache_manifest(scene, 0, ctx_html, renderer_build="chromium-151")
    assert m.renderer_build == "chromium-151"
    assert m.font_set_hash, "HTML scene manifest must carry a font_set_hash"
    plain = build_cache_manifest(_scene(), 0, RenderContext(fps=24, width=48, height=32))
    assert plain.font_set_hash == ""


def test_render_worker_reuses_cache_reports_progress_and_cleans_up() -> None:
    scene = _scene()
    cache = SceneRenderCache()
    worker = RenderWorker(cache=cache)
    progress = []
    try:
        first = worker.render_sync(scene, (0, 1, 2), RenderContext(fps=24, width=48, height=32), progress.append)
        second = worker.render_sync(scene, (0, 1, 2), RenderContext(fps=24, width=48, height=32))
        assert len(first.frames) == 3
        assert first.frames == second.frames
        assert [item.completed for item in progress] == [1, 2, 3]
        assert cache.stats()["hits"] >= 3
        assert cache.invalidate_scene(scene.scene_id) == 3
        assert cache.stats()["entries"] == 0
    finally:
        worker.shutdown()


def test_cache_invalidates_only_dirty_frame_range() -> None:
    scene = _scene()
    cache = SceneRenderCache()
    worker = RenderWorker(cache=cache)
    try:
        worker.render_sync(scene, (0, 1, 2, 3), RenderContext(fps=24, width=48, height=32))
        assert cache.invalidate_frame_range(scene.scene_id, 1, 3) == 2
        worker.render_sync(scene, (0, 1, 2, 3), RenderContext(fps=24, width=48, height=32))
        stats = cache.stats()
        assert stats["invalidations"] == 2
        assert stats["hits"] >= 2  # frames 0 and 3 remained reusable
        assert stats["misses"] >= 6  # four cold frames + two dirty frames
    finally:
        worker.shutdown()


def test_render_worker_cancel_returns_partial_result() -> None:
    scene = _scene()
    worker = RenderWorker()
    try:
        def cancel_after_first(_progress: object) -> bool:
            return True
        handle = worker.submit(scene, range(scene.duration_frames), RenderContext(fps=24, width=48, height=32), cancel_after_first)
        result = handle.result(timeout=10)
        assert result.cancelled or len(result.frames) < scene.duration_frames
    finally:
        worker.shutdown()


def test_export_service_writes_svg_html_and_rgba_sequences(tmp_path: Path) -> None:
    scene = _scene()
    service = TeachingAnimationExportService()
    try:
        svg = service.export(scene, tmp_path / "svg", options=ExportOptions(format="svg_sequence"))
        html = service.export(scene, tmp_path / "html", options=ExportOptions(format="html_sequence"))
        rgba = service.export(scene, tmp_path / "rgba", options=ExportOptions(format="rgba_sequence"))
    finally:
        service.close()
    assert svg.frame_count == html.frame_count == rgba.frame_count == scene.duration_frames
    assert (tmp_path / "svg" / "frame_000000.svg").is_file()
    assert (tmp_path / "html" / "frame_000000.html").is_file()
    assert (tmp_path / "rgba" / "frame_000000.rgba").stat().st_size == scene.width * scene.height * 4


def test_export_service_writes_single_html_document_from_html_layer(tmp_path: Path) -> None:
    from dataclasses import replace
    from src.features.teaching_animation.core.html_layer import build_html_layer

    html = (
        "<!doctype html><html><head><style>"
        "body{margin:0;background:#07111f;color:#fff}"
        "@keyframes reveal{from{opacity:0}to{opacity:1}}"
        ".title{animation:reveal 2s ease both}"
        "</style></head><body><h1 class='title' "
        "data-topos-node-id='title'>HTML deck</h1></body></html>"
    )
    base = TeachingSceneDocument.create_default(
        "HTML source", width=320, height=180, duration_frames=48,
    )
    scene = replace(base, html_layer=build_html_layer(
        layer_id="html-source", sanitized_html=html, authored_source=html,
        viewport_width=320, viewport_height=180, duration_frames=48,
    ))
    output = tmp_path / "motion.html"
    service = TeachingAnimationExportService()
    try:
        result = service.export(
            scene, output, options=ExportOptions(format="html_document"),
        )
    finally:
        service.close()
    assert result.format == "html_document"
    assert result.frame_count == 0
    exported = output.read_text(encoding="utf-8")
    assert exported == html + "\n"
    assert "@keyframes reveal" in exported
    assert "data-topos-node-id='title'" in exported


def test_export_html_document_honours_freeform_browser_profile(tmp_path: Path) -> None:
    """AI Motion Graphic HTML may legitimately retain browser-side motion JS."""
    from dataclasses import replace
    from src.features.teaching_animation.core.html_layer import build_html_layer
    from src.features.teaching_animation.render.html_layer.sanitizer import HtmlSanitizer

    raw_html = (
        "<!doctype html><html><body><div>Motion Graphic</div>"
        "<script>document.body.dataset.ready='1'</script></body></html>"
    )
    sanitized = HtmlSanitizer(
        allow_inline_scripts=True, allow_freeform_css=True,
    ).sanitize(raw_html).html
    base = TeachingSceneDocument.create_default(
        "Freeform HTML", width=320, height=180, duration_frames=12,
    )
    scene = replace(base, html_layer=build_html_layer(
        layer_id="freeform", sanitized_html=sanitized, authored_source=raw_html,
        viewport_width=320, viewport_height=180, duration_frames=12,
        metadata={"allow_inline_scripts": True, "allow_freeform_css": True},
    ))
    output = tmp_path / "freeform.html"
    service = TeachingAnimationExportService()
    try:
        service.export(scene, output, options=ExportOptions(format="html_document"))
    finally:
        service.close()
    assert "document.body.dataset.ready" in output.read_text(encoding="utf-8")


def test_export_preflight_blocks_missing_external_asset(tmp_path: Path) -> None:
    from src.features.teaching_animation.core.model import SceneAssetRef
    scene = TeachingSceneDocument.from_dict({**_scene().to_dict(), "asset_refs": [SceneAssetRef("img", path="missing.svg").to_dict()]})
    service = TeachingAnimationExportService()
    try:
        with pytest.raises(TeachingExportError, match="missing"):
            service.export(scene, tmp_path / "out", options=ExportOptions(), asset_root=str(tmp_path))
    finally:
        service.close()


def test_offline_fallback_is_explicit_and_preserves_cache_state() -> None:
    fallback = offline_scene_frame_fallback("scene", "cache-1", cached_frame_available=True)
    assert fallback.cached_frame_available
    assert fallback.cache_id == "cache-1"
