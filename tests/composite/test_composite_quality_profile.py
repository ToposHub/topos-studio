"""P2-04 quality profile and cache isolation contracts."""

from types import SimpleNamespace

import pytest

from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.core.quality_profile import (
    CompositeQualityProfile,
    normalize_quality_profile,
    quality_profile_for_request,
)
from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.render.cache_manager import CacheManager
from src.features.composite.render.render_scheduler import RenderMode, RenderRequest


pytestmark = pytest.mark.composite_core


def test_quality_profile_resolution_is_deterministic():
    assert quality_profile_for_request("preview", 1.0) == "preview"
    assert quality_profile_for_request("preview", 0.5) == "draft"
    assert quality_profile_for_request("full", 0.25) == "final"
    assert quality_profile_for_request("export", 1.0) == "final"
    assert normalize_quality_profile("unknown") == "preview"
    assert normalize_quality_profile(CompositeQualityProfile.FINAL) == "final"


def test_render_request_derives_and_normalizes_quality_profile():
    draft = RenderRequest(snapshot=None, request_id=1, timestamp=0.0,
                          proxy_scale=0.5)
    assert draft.quality_profile == "draft"

    final = RenderRequest(snapshot=None, request_id=2, timestamp=0.0,
                          render_mode=RenderMode.EXPORT, proxy_scale=0.25,
                          quality_profile="draft")
    assert final.quality_profile == "final"

    explicit = RenderRequest(snapshot=None, request_id=3, timestamp=0.0,
                             quality_profile="draft")
    assert explicit.quality_profile == "draft"


def test_quality_profile_is_part_of_evaluation_cache_key():
    manager = CacheManager(None)
    node = SimpleNamespace(
        node_type="blur", node_id="blur_1", parameters={}, disabled=False)
    preview = manager.compute_key(
        node, {}, render_mode="preview", quality_profile="preview")
    draft = manager.compute_key(
        node, {}, render_mode="preview", quality_profile="draft")
    assert preview != draft
    assert ":qppreview" in preview
    assert ":qpdraft" in draft


def test_scene3d_maps_idle_draft_request_to_reduced_shading_profile():
    eval_ctx = CompositeEvalContext(quality_profile="draft")
    resolved = Scene3DNode._resolve_quality_profile(
        eval_ctx, "preview", model_payloads=None)
    assert resolved.value == "navigation"
