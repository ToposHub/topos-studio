"""FX-H02 regression: render-cache invalidation on effect mutation.

Audit FX-H02: the effect signature/hash tool existed
(build_effects_signature) but the clip-effect-edit path only synced
preview/UI — the project's cache manifest had no effect revision/hash, so
a manual cache could stay VALID and keep showing the pre-effect frame
after the user changed an effect.

The fix adds an ``effect_signature`` field to ``RenderCacheManifest`` and a
scoped ``invalidate_render_cache_for_clip`` helper, and tightens
``is_valid_for`` to honour the signature. These tests pin:

  - effect_signature round-trips through to_dict/from_dict (back-compat);
  - is_valid_for flips to False when the signature changes;
  - invalidate_render_cache_for_clip flips ONLY the matching clip's VALID
    manifests to STALE (other clips untouched);
  - the helper is a no-op when nothing matches.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.project import EditProject
from src.features.edit.core.realtime_playback.render_cache_adapter import (
    build_effects_signature,
)
from src.features.edit.core.render_cache import (
    RenderCacheManifest,
    RenderCacheStatus,
)

pytestmark = pytest.mark.edit_core


def _manifest(clip_id: str, status: str = RenderCacheStatus.VALID,
              effect_signature: str = "") -> RenderCacheManifest:
    return RenderCacheManifest(
        cache_id=f"cache-{clip_id}",
        timeline_id="tl", clip_id=clip_id,
        effect_signature=effect_signature,
        status=status,
    )


class TestEffectSignatureField:
    def test_round_trip_preserves_effect_signature(self) -> None:
        m = _manifest("c1", effect_signature="abc123")
        round_tripped = RenderCacheManifest.from_dict(m.to_dict())
        assert round_tripped.effect_signature == "abc123"

    def test_old_save_without_effect_signature_loads_empty(self) -> None:
        """Back-compat: a pre-FX-H02 save file lacks effect_signature."""
        legacy = {
            "cache_id": "x", "timeline_id": "tl", "clip_id": "c1",
            "status": RenderCacheStatus.VALID,
        }
        m = RenderCacheManifest.from_dict(legacy)
        assert m.effect_signature == ""


class TestIsValidForHonoursEffectSignature:
    def test_matching_signature_is_valid(self) -> None:
        m = _manifest("c1", effect_signature="sig")
        assert m.is_valid_for(0, 0, effect_signature="sig") is True

    def test_changed_signature_is_invalid(self) -> None:
        """FX-H02 core: an effect mutation changes the signature, so a
        previously-VALID cache must be treated as invalid."""
        m = _manifest("c1", effect_signature="old-sig")
        assert m.is_valid_for(0, 0, effect_signature="new-sig") is False

    def test_none_signature_skips_check(self) -> None:
        """Legacy callers that don't pass a signature get the old behaviour."""
        m = _manifest("c1", effect_signature="anything")
        assert m.is_valid_for(0, 0, effect_signature=None) is True

    def test_stale_status_is_invalid_regardless(self) -> None:
        m = _manifest("c1", status=RenderCacheStatus.STALE, effect_signature="sig")
        assert m.is_valid_for(0, 0, effect_signature="sig") is False


class TestSignatureChangesOnEffectEdit:
    """build_effects_signature must change when the effect chain changes —
    otherwise the cache key wouldn't catch the mutation."""

    def test_adding_an_effect_changes_signature(self) -> None:
        empty = build_effects_signature(())
        with_blur = build_effects_signature(
            (EffectInstance(effect_type="blur", params={"radius": 2.0}),)
        )
        assert empty == ""
        assert with_blur != empty

    def test_changing_a_param_changes_signature(self) -> None:
        e1 = (EffectInstance(effect_type="blur", params={"radius": 2.0}),)
        e2 = (EffectInstance(effect_type="blur", params={"radius": 5.0}),)
        assert build_effects_signature(e1) != build_effects_signature(e2)


class TestInvalidateRenderCacheForClip:
    def _project_with_two_clip_manifests(self) -> EditProject:
        project = EditProject.create("Cache")
        m_a = _manifest("clip-a", effect_signature="sig-a")
        m_b = _manifest("clip-b", effect_signature="sig-b")
        return EditProject(
            project_id=project.project_id, name=project.name,
            assets=project.assets, bins=project.bins,
            timelines=project.timelines, compositions=project.compositions,
            color_stacks=project.color_stacks, comp_clips=project.comp_clips,
            render_cache_manifests={m_a.cache_id: m_a.to_dict(), m_b.cache_id: m_b.to_dict()},
            render_cache_settings=project.render_cache_settings,
            root_bin_id=project.root_bin_id,
            active_timeline_id=project.active_timeline_id,
            active_comp_clip_id=project.active_comp_clip_id,
            created_at=project.created_at, modified_at=project.modified_at,
        )

    def _status(self, project: EditProject, clip_id: str) -> str:
        for m_dict in project.render_cache_manifests.values():
            m = RenderCacheManifest.from_dict(m_dict) if isinstance(m_dict, dict) else m_dict
            if isinstance(m, RenderCacheManifest) and m.clip_id == clip_id:
                return m.status
        return RenderCacheStatus.MISSING

    def test_only_matching_clip_is_invalidated(self) -> None:
        """FX-H02 scoped invalidation: clip-a's cache goes STALE, clip-b
        stays VALID — the whole-timeline invalidator would nuke both."""
        project = self._project_with_two_clip_manifests()
        result = project.invalidate_render_cache_for_clip("clip-a")
        assert self._status(result, "clip-a") == RenderCacheStatus.STALE
        assert self._status(result, "clip-b") == RenderCacheStatus.VALID

    def test_already_stale_manifest_untouched(self) -> None:
        project = self._project_with_two_clip_manifests()
        # Make clip-a already STALE.
        m_a = RenderCacheManifest.from_dict(project.render_cache_manifests["cache-clip-a"])
        project = EditProject(
            project_id=project.project_id, name=project.name,
            assets=project.assets, bins=project.bins,
            timelines=project.timelines, compositions=project.compositions,
            color_stacks=project.color_stacks, comp_clips=project.comp_clips,
            render_cache_manifests={
                "cache-clip-a": m_a.to_dict() | {"status": RenderCacheStatus.STALE} if False else
                {**m_a.to_dict(), "status": RenderCacheStatus.STALE},
                "cache-clip-b": project.render_cache_manifests["cache-clip-b"],
            },
            render_cache_settings=project.render_cache_settings,
            root_bin_id=project.root_bin_id,
            active_timeline_id=project.active_timeline_id,
            active_comp_clip_id=project.active_comp_clip_id,
            created_at=project.created_at, modified_at=project.modified_at,
        )
        result = project.invalidate_render_cache_for_clip("clip-a")
        assert self._status(result, "clip-a") == RenderCacheStatus.STALE

    def test_no_match_is_noop(self) -> None:
        project = self._project_with_two_clip_manifests()
        result = project.invalidate_render_cache_for_clip("clip-z")
        assert result is project  # unchanged
        assert self._status(result, "clip-a") == RenderCacheStatus.VALID
