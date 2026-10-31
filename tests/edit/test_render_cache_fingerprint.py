"""P2-18 semantic render-cache fingerprint tests."""

from dataclasses import replace
from types import SimpleNamespace

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.render_cache import (
    RenderCacheManifest,
    RenderCacheStatus,
    build_render_cache_fingerprint,
)
from src.features.edit.core.render_cache_status import (
    STATUS_CACHED,
    STATUS_UNCACHED,
    cache_status_for_clip,
)
from src.features.edit.core.timeline_v2 import ClipInstance


def _project() -> EditProject:
    project = EditProject.create("Fingerprint")
    asset = MediaAsset(
        asset_id="asset-1", path="/tmp/fingerprint-source.mp4",
        media_type="video", display_name="Source", metadata={},
    )
    return replace(project, assets={asset.asset_id: asset})


def _clip(*, effects=()):
    return ClipInstance(
        clip_id="clip-1", asset_id="asset-1", track_id="v1",
        timeline_start=0, timeline_duration=25, source_start=0,
        source_duration=25, composite_id="graph-1", effects=effects,
    )


def test_fingerprint_changes_when_effect_changes():
    project = _project()
    first = _clip()
    second = replace(
        first,
        effects=(EffectInstance(effect_type="blur", params={"radius": 4.0}),),
    )
    assert build_render_cache_fingerprint(project, "tl-1", first) != (
        build_render_cache_fingerprint(project, "tl-1", second)
    )


def test_current_semantic_fingerprint_keeps_cache_green():
    project = _project()
    clip = _clip()
    fingerprint = build_render_cache_fingerprint(project, "tl-1", clip)
    manifest = RenderCacheManifest(
        cache_id="cache-1", timeline_id="tl-1", clip_id=clip.clip_id,
        composite_id="graph-1", status=RenderCacheStatus.VALID,
        render_fingerprint=fingerprint,
    )
    project = project.set_render_cache_manifest(manifest)
    assert cache_status_for_clip(project, "tl-1", clip) == STATUS_CACHED
    edited = replace(
        clip,
        effects=(EffectInstance(effect_type="sharpen", params={"amount": 1.0}),),
    )
    assert cache_status_for_clip(project, "tl-1", edited) == STATUS_UNCACHED


def test_undo_reuses_previous_fingerprinted_variant():
    project = _project()
    original = _clip()
    original_fingerprint = build_render_cache_fingerprint(project, "tl-1", original)
    project = project.set_render_cache_manifest(RenderCacheManifest(
        cache_id="cache-original",
        timeline_id="tl-1",
        clip_id=original.clip_id,
        composite_id=original.composite_id,
        path="",
        status=RenderCacheStatus.VALID,
        render_fingerprint=original_fingerprint,
    ))

    edited = replace(
        original,
        effects=(EffectInstance(effect_type="blur", params={"radius": 4.0}),),
    )
    edited_fingerprint = build_render_cache_fingerprint(project, "tl-1", edited)
    project = project.set_render_cache_manifest(RenderCacheManifest(
        cache_id="cache-edited",
        timeline_id="tl-1",
        clip_id=edited.clip_id,
        composite_id=edited.composite_id,
        path="",
        status=RenderCacheStatus.VALID,
        render_fingerprint=edited_fingerprint,
    ))

    assert len(project.render_cache_manifests) == 2
    assert cache_status_for_clip(project, "tl-1", edited) == STATUS_CACHED
    # Restoring the old clip state selects the old valid manifest instead of
    # treating the most recently rendered effect state as the only cache.
    assert cache_status_for_clip(project, "tl-1", original) == STATUS_CACHED


def test_fingerprinted_history_survives_imperative_invalidation():
    project = _project()
    clip = _clip()
    fingerprint = build_render_cache_fingerprint(project, "tl-1", clip)
    project = project.set_render_cache_manifest(RenderCacheManifest(
        cache_id="cache-history",
        timeline_id="tl-1",
        clip_id=clip.clip_id,
        composite_id=clip.composite_id,
        status=RenderCacheStatus.VALID,
        render_fingerprint=fingerprint,
    ))

    invalidated = project.invalidate_render_cache_for_clip("tl-1", clip.clip_id)
    restored = RenderCacheManifest.from_dict(
        invalidated.render_cache_manifests["cache-history"]
    )
    assert restored.status == RenderCacheStatus.VALID
    assert cache_status_for_clip(invalidated, "tl-1", clip) == STATUS_CACHED


def test_render_cache_variant_history_is_bounded():
    project = _project()
    clip = _clip()
    for index in range(10):
        project = project.set_render_cache_manifest(RenderCacheManifest(
            cache_id=f"cache-{index}",
            timeline_id="tl-1",
            clip_id=clip.clip_id,
            composite_id=clip.composite_id,
            path=f"/tmp/cache-{index}.mov",
            status=RenderCacheStatus.VALID,
            render_fingerprint=f"fingerprint-{index}",
        ))

    pruned, removed_paths = project.prune_render_cache_variants(
        "tl-1", clip.clip_id, max_variants=4,
    )
    assert len(pruned.render_cache_manifests) == 4
    assert tuple(sorted(removed_paths)) == tuple(
        f"/tmp/cache-{index}.mov" for index in range(6)
    )
    assert set(pruned.render_cache_manifests) == {
        "cache-6", "cache-7", "cache-8", "cache-9",
    }
