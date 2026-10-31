"""FH-01: FreeformHtmlMotionLayer lands in the scene schema.

Pins the W2 contract: an HTML layer is versioned, sanitized data owned by the
scene via scene_id; it round-trips through save/reopen and migration, and the
validator enforces that bound assets resolve and provenance hashes are present.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core import (
    FreeformHtmlMotionLayer, HtmlEditSlot, SCENE_SCHEMA_VERSION,
    TeachingSceneDocument, build_html_layer, migrate_scene_json,
    validate_scene_json,
)


def _layer(**overrides) -> FreeformHtmlMotionLayer:
    base = dict(
        layer_id="hero",
        sanitized_html='<div data-topos-node-id="t1">Hi</div>',
        authored_source='<div data-topos-node-id="t1">Hi</div><script>x()</script>',
        viewport_width=1920, viewport_height=1080, duration_frames=120,
        bound_asset_ids=("logo",),
        editable_slots=(HtmlEditSlot("t1", "content", "text"),),
        editable_tokens=("--brand",),
        sanitizer_version="html-san/1.0",
        renderer_build="playwright-chromium/py1.62.0/chrome-151.0.7922.34",
        csp="default-src 'none'; style-src 'unsafe-inline'",
    )
    base.update(overrides)
    return build_html_layer(**base)


def _scene_with_layer(layer: FreeformHtmlMotionLayer | None = None) -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="s1", schema_version=SCENE_SCHEMA_VERSION, name="Hero",
        width=1920, height=1080, fps_num=24, fps_den=1, duration_frames=120,
        background={"color": "#0b1220"}, elements=(), animations=(),
        html_layer=layer,
    )


class TestLayerProvenance:
    def test_source_and_sanitizer_hashes_differ_when_sanitizer_changed_source(self):
        layer = _layer()
        # The authored source had a <script> the sanitizer stripped, so the two
        # hashes must differ — this is exactly what a reviewer audits.
        assert layer.source_hash != layer.sanitizer_hash

    def test_sanitizer_hash_is_stable_over_round_trip(self):
        layer = _layer()
        restored = FreeformHtmlMotionLayer.from_dict(layer.to_dict())
        assert restored == layer
        assert restored.sanitizer_hash == layer.sanitizer_hash

    def test_layer_rejects_empty_html(self):
        with pytest.raises(ValueError):
            FreeformHtmlMotionLayer(
                layer_id="x", sanitized_html="", viewport_width=10, viewport_height=10,
                fps_num=24, fps_den=1, duration_frames=10)

    def test_edit_slot_enforces_modes(self):
        with pytest.raises(ValueError):
            HtmlEditSlot("n", edit_mode="bogus")
        with pytest.raises(ValueError):
            HtmlEditSlot("n", kind="bogus")


class TestSceneRoundTrip:
    def test_html_layer_preserved_through_save_reopen(self):
        scene = _scene_with_layer(_layer())
        reopened = TeachingSceneDocument.from_json(scene.to_json())
        assert reopened.html_layer is not None
        assert reopened.html_layer.layer_id == "hero"
        assert reopened.html_layer.sanitizer_hash == scene.html_layer.sanitizer_hash

    def test_scene_without_html_layer_remains_none(self):
        scene = _scene_with_layer(None)
        assert scene.html_layer is None
        reopened = TeachingSceneDocument.from_json(scene.to_json())
        assert reopened.html_layer is None

    def test_html_layer_survives_migration(self):
        # A v2 scene dict carrying an html_layer round-trips through migration
        # unchanged (unknown-field preservation, AQ-06).
        raw = {
            "schema_version": 2, "scene_id": "x", "name": "x", "width": 10,
            "height": 10, "fps_num": 24, "fps_den": 1, "duration_frames": 10,
            "background": {"color": "#000"}, "elements": [], "animations": [],
            "markers": [], "asset_refs": [],
            "html_layer": {"layer_id": "L", "sanitized_html": "<p>",
                           "viewport_width": 10, "viewport_height": 10,
                           "fps_num": 24, "fps_den": 1, "duration_frames": 10},
        }
        migrated = migrate_scene_json(raw)
        assert migrated["html_layer"]["layer_id"] == "L"


class TestValidator:
    def test_valid_layer_with_bound_asset_passes(self):
        scene = _scene_with_layer(_layer(bound_asset_ids=("logo",)))
        # declare the asset so the layer's bound id resolves
        data = scene.to_dict()
        data["asset_refs"] = [{"asset_id": "logo", "kind": "image"}]
        result = validate_scene_json(data)
        assert not any("html_layer" in i.path for i in result.issues)

    def test_unbound_asset_rejected(self):
        scene = _scene_with_layer(_layer(bound_asset_ids=("ghost",)))
        result = validate_scene_json(scene.to_dict())
        assert any("unbound" in i.message for i in result.issues)

    def test_missing_hashes_rejected(self):
        scene = _scene_with_layer(_layer())
        data = scene.to_dict()
        data["html_layer"]["source_hash"] = ""
        result = validate_scene_json(data)
        assert any("source_hash" in i.path for i in result.issues)

    def test_empty_sanitized_html_rejected(self):
        # Constructed raw because the dataclass itself rejects empty html.
        scene = _scene_with_layer(_layer())
        data = scene.to_dict()
        data["html_layer"]["sanitized_html"] = ""
        result = validate_scene_json(data)
        assert any("sanitized_html" in i.path for i in result.issues)

    def test_non_positive_viewport_rejected(self):
        scene = _scene_with_layer(_layer())
        data = scene.to_dict()
        data["html_layer"]["viewport_width"] = 0
        result = validate_scene_json(data)
        assert any("viewport_width" in i.path for i in result.issues)
