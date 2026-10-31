"""FX-C06 / FX-H01 regression: effect catalog & UI capability contract.

Audit FX-C06: the Effects panel used to register four "ghost" effects —
``transform``, ``crop``, ``opacity``, ``vignette`` — that were attachable
from the UI but NOT in the EffectCatalog. T-P1-20 step 4 removed them from
the registry (audit P6-step-1); the clip-native properties remain on the
clip itself, legacy project data is stripped at deserialisation, and
``NON_ATTACHABLE_VIDEO_EFFECT_TYPES`` stays as an attach-path guard.
Generator definitions (``adjustment_clip`` / ``solid_color``) remain
registered as clip *templates* — never attachable effects.

Audit FX-H01: a video filter that appears in the attach UI must declare
at least one working preview path AND one working export path. Defocus
still lacks an export handler; Denoise and Edge Detect have timeline export
but no CPU preview handlers.

These tests pin the *intended* capability contract (audit table 3.3 /
§4 P4) and encode the current broken state as a ledger so the migration
is auditable. The key behavioural guard: a generator must never enter
the ``AddEffectCommand`` attach path, and every attachable video filter
must be catalog-known.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.effect_registry import (
    EffectRegistry,
    GENERATOR_EFFECT_TYPES,
    NON_ATTACHABLE_VIDEO_EFFECT_TYPES,
    is_attachable_video_effect,
)
from src.features.shared.effects import get_effect_catalog


# ── Effect capability classification (audit §3.3) ────────────────────

# Clip-native properties that must NOT be attachable as video filters.
# They belong in the Inspector, not the Effects browser.
CLIP_PROPERTY_EFFECT_TYPES = frozenset({"transform", "crop", "opacity"})

# Legacy stylize entry that is not wired to any backend today.
LEGACY_NON_RENDERED_TYPES = frozenset({"vignette"})


@pytest.fixture(scope="module")
def registry() -> EffectRegistry:
    return EffectRegistry()


@pytest.fixture(scope="module")
def catalog():
    return get_effect_catalog()


# ── FX-C06 → T-P1-20: ghost effects are fully removed ────────────────

class TestGhostEffects:
    """The four ghost effects are no longer registered (T-P1-20 step 4).

    Audit P6-step-1 removal landed: ``transform`` / ``crop`` / ``opacity``
    live on as clip-native properties (Inspector), ``vignette`` was a never-
    wired legacy entry. None may re-enter the registry; legacy project data
    carrying them is stripped at deserialisation with a warning, and
    ``NON_ATTACHABLE_VIDEO_EFFECT_TYPES`` stays as an attach-path guard.
    """

    @pytest.mark.parametrize("effect_type", sorted(CLIP_PROPERTY_EFFECT_TYPES | LEGACY_NON_RENDERED_TYPES))
    def test_ghost_effect_is_unregistered_and_catalog_unknown(
        self, registry, catalog, effect_type
    ) -> None:
        """Each ghost is neither in the registry nor the catalog."""
        assert registry.get_definition(effect_type) is None, (
            f"{effect_type} re-entered the registry — ghosts must stay "
            f"removed (T-P1-20: 没有 registry 可见但语义不明的效果)"
        )
        assert catalog.is_known(effect_type) is False

    @pytest.mark.parametrize("effect_type", sorted(CLIP_PROPERTY_EFFECT_TYPES | LEGACY_NON_RENDERED_TYPES))
    def test_ghost_execution_capability_is_unsupported(
        self, effect_type
    ) -> None:
        """统一能力契约把 ghost 类型判为 unsupported（无任何后端），
        Deliver preflight/AI/UI 三处据此一致拒绝。"""
        from src.features.shared.effects import resolve_execution_capability

        execution = resolve_execution_capability(
            effect_type, catalog_known=False)
        assert execution.unsupported is True
        assert execution.exportable is False
        assert execution.delivery_restriction()[0] == "effect_unknown"


# ── FX-C06: generators must not be attachable ────────────────────────

class TestGeneratorNotAttachable:
    """Generator definitions create clips, they are not effects.

    The panel double-click handler currently emits
    ``effect_add_requested`` for ANY registered effect_type, including
    generators — which would create a useless EffectInstance. The fix
    (audit P3 step 5) blocks generator attach and ``AddEffectCommand``
    must reject them. These tests pin that contract.
    """

    def test_generator_effect_types_set_exists(self) -> None:
        assert "adjustment_clip" in GENERATOR_EFFECT_TYPES
        assert "solid_color" in GENERATOR_EFFECT_TYPES

    @pytest.mark.parametrize("gen_type", sorted(GENERATOR_EFFECT_TYPES))
    def test_generator_is_not_a_video_filter(self, registry, catalog, gen_type) -> None:
        """A generator definition exists (for the panel) but is NOT a
        catalog-renderable video filter — it has no shader / backend."""
        assert registry.get_definition(gen_type) is not None
        assert catalog.is_known(gen_type) is False

    def test_generator_category_marks_them_as_generators(self, registry) -> None:
        """All generator definitions share the 'generator' category, which
        the panel must use to route them to drag-to-timeline, never attach."""
        for gen_type in GENERATOR_EFFECT_TYPES:
            definition = registry.get_definition(gen_type)
            assert definition is not None
            assert definition.category == "generator", (
                f"{gen_type} category is {definition.category!r}, expected "
                f"'generator' so the panel can keep it out of the attach path"
            )


# ── FX-H01: attachable video filters must declare a backend ─────────

class TestVideoFilterCapability:
    """Every catalog-known video filter is a real, renderable effect.

    The audit found that GPU-preview effects (denoise / edge_detect /
    erode_dilate / defocus) have no CPU preview. Erode/dilate, Edge Detect,
    and Denoise now also have CPU export and bounded Metal paths. This test
    documents the
    CURRENT per-backend coverage so the parity work (audit P4) has a
    concrete checklist. Today's expected state is captured below; when a
    backend is added, update the snapshot.
    """

    # Current backend coverage (audit FX-H01 evidence):
    #   - CPU preview handlers: blur, sharpen  (timeline_preview_renderer)
    #   - Export handlers:      blur, sharpen, glow, erode_dilate, edge_detect, denoise, defocus
    #   - GPU preview:          all catalog effects
    CPU_PREVIEW_HANDLERS = frozenset({"blur", "sharpen"})
    EXPORT_HANDLERS = frozenset({
        "blur", "sharpen", "glow", "erode_dilate", "edge_detect", "denoise", "defocus",
    })

    def test_every_catalog_effect_has_gpu_preview(self, catalog) -> None:
        """All catalog effects ship a GPU fragment shader, so GPU preview
        is the universal baseline. If an effect lacks a shader it must be
        a non-renderable entry and should not be in the catalog."""
        for spec in catalog.get_all_specs():
            assert spec.shader_sources, (
                f"{spec.effect_type} has no shader sources — it cannot run "
                f"on any backend and should not be catalog-known"
            )

    @pytest.mark.parametrize("effect_type", [
        "blur", "sharpen", "denoise", "edge_detect",
        "erode_dilate", "defocus", "glow",
    ])
    def test_backend_coverage_ledger(self, effect_type) -> None:
        """Snapshot of which catalog effect has which CPU/export backend.

        Current coverage: only blur/sharpen have CPU preview;
        blur/sharpen/glow/erode_dilate/edge_detect/denoise/defocus have export.
        """
        cpu = effect_type in self.CPU_PREVIEW_HANDLERS
        export = effect_type in self.EXPORT_HANDLERS
        expected = {
            "blur":         (True,  True),
            "sharpen":      (True,  True),
            "denoise":      (False, True),
            "edge_detect":  (False, True),
            "erode_dilate": (False, True),
            "defocus":      (False, True),
            "glow":         (False, True),
        }[effect_type]
        assert (cpu, export) == expected, (
            f"{effect_type} backend coverage changed: cpu={cpu} export={export}. "
            f"Update the ledger (and FX-H01 once every effect is fully covered)."
        )


# ── FX-C06: attachable classifier ────────────────────────────────────

class TestAttachableClassifier:
    """``is_attachable_video_effect`` decides what the panel may show as an
    attachable video filter. Generators and the four ghost types are not
    attachable; real catalog filters are."""

    @pytest.mark.parametrize("ghost", sorted(NON_ATTACHABLE_VIDEO_EFFECT_TYPES))
    def test_ghost_types_are_not_attachable(self, ghost) -> None:
        assert is_attachable_video_effect(ghost) is False

    @pytest.mark.parametrize("gen", sorted(GENERATOR_EFFECT_TYPES))
    def test_generators_are_not_attachable(self, gen) -> None:
        assert is_attachable_video_effect(gen) is False

    @pytest.mark.parametrize("real", ["blur", "sharpen", "denoise", "glow"])
    def test_catalog_filters_are_attachable(self, real) -> None:
        assert is_attachable_video_effect(real) is True

    def test_non_attachable_set_is_the_four_ghosts(self) -> None:
        """Pin the exact ghost set so an accidental re-add is caught."""
        assert NON_ATTACHABLE_VIDEO_EFFECT_TYPES == frozenset(
            {"transform", "crop", "opacity", "vignette"}
        )


# ── T-P1-20: legacy project data migration ───────────────────────────

class TestLegacyGhostMigration:
    """Old projects carrying ghost effect instances load clean.

    The ghosts never rendered, so stripping them at deserialisation is
    visually lossless — and it stops them from blocking Deliver preflight
    with ``effect_unknown`` forever.
    """

    @staticmethod
    def _clip_dict(effect_types) -> dict:
        return {
            "clip_id": "clip_legacy",
            "asset_id": "asset_1",
            "track_id": "v1",
            "timeline_start": 0,
            "timeline_duration": 10,
            "source_start": 0,
            "source_duration": 10,
            "effects": [
                {"effect_type": t, "effect_id": f"fx_{t}", "params": {}}
                for t in effect_types
            ],
        }

    @pytest.mark.parametrize("ghost", sorted(CLIP_PROPERTY_EFFECT_TYPES | LEGACY_NON_RENDERED_TYPES))
    def test_from_dict_strips_ghost_instances(self, ghost) -> None:
        from src.features.edit.core.timeline_v2 import ClipInstance

        clip = ClipInstance.from_dict(self._clip_dict([ghost, "blur"]))
        loaded_types = {
            getattr(effect, "effect_type", None) for effect in clip.effects
        }
        assert loaded_types == {"blur"}, (
            f"ghost effect {ghost!r} survived deserialisation"
        )

    def test_from_dict_keeps_real_effects(self) -> None:
        from src.features.edit.core.timeline_v2 import ClipInstance

        clip = ClipInstance.from_dict(self._clip_dict(["blur", "sharpen"]))
        assert len(clip.effects) == 2
