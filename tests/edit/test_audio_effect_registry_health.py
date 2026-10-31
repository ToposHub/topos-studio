"""P1-07: audio effect registry health check + actionable empty state.

Contract (from the professional-gap audit):
- The builtin audio set (EQ / Limiter / Compressor / Reverb / Delay /
  Saturation / Imager / Noise Gate) is health-checked end-to-end: the
  EffectRegistry definition AND the audio_dsp processor class.
- An empty audio tab shows per-effect reasons (registration vs backend),
  never a generic "coming soon" placeholder.
- Transition CARDS double-click through the same apply path as the tree.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.audio_dsp import _ensure_effect_registry
from src.features.edit.core.effect_registry import (
    AUDIO_MIXER_EFFECT_TYPES,
    EffectRegistry,
    get_effect_registry,
    get_effect_registry_health,
)
from src.features.edit.ui.edit_effects_panel import EditEffectsPanel


def _registry_without_audio() -> EffectRegistry:
    """Fresh registry with the audio builtin set stripped (simulates a
    failed/skipped audio registration — the constructor always registers
    builtins, so we remove them explicitly)."""
    registry = EffectRegistry()
    registry._categories["audio"] = []
    for effect_type in AUDIO_MIXER_EFFECT_TYPES:
        registry._definitions.pop(effect_type, None)
    return registry


class TestRegistryHealth:
    def test_default_registry_is_healthy(self):
        health = get_effect_registry_health()

        assert health.is_healthy
        assert health.missing == ()
        assert {e.effect_type for e in health.effects} == set(
            AUDIO_MIXER_EFFECT_TYPES
        )
        assert all(e.registered and e.has_backend for e in health.effects)

    def test_backend_coverage_matches_builtin_types(self):
        # Every builtin type must have a DSP processor class — the panel
        # may show a definition but preview/export need the backend.
        backend = set(_ensure_effect_registry().keys())

        assert backend >= set(AUDIO_MIXER_EFFECT_TYPES)

    def test_missing_registration_is_reported_with_reason(self):
        empty = _registry_without_audio()

        health = get_effect_registry_health(empty)

        assert not health.is_healthy
        assert len(health.missing) == len(AUDIO_MIXER_EFFECT_TYPES)
        for status in health.missing:
            assert status.registered is False
            assert "missing from EffectRegistry" in status.reason

    def test_registered_without_backend_reports_silent_drop(self):
        registry = get_effect_registry()  # fully populated

        import src.features.edit.core.effect_registry as er
        import src.features.edit.core.audio_dsp as dsp

        health = er.get_effect_registry_health(registry)
        # sanity: healthy with the real backend
        assert health.is_healthy

        # Now make the backend probe return nothing and re-probe. The
        # health check imports _ensure_effect_registry at call time, so
        # patching the module attribute is enough (clearing the dict
        # would NOT work — the lazy initializer repopulates it).
        saved_probe = dsp._ensure_effect_registry
        try:
            dsp._ensure_effect_registry = lambda: {}
            health = er.get_effect_registry_health(registry)
            assert not health.is_healthy
            assert all(
                status.registered and not status.has_backend
                for status in health.missing
            )
            assert all(
                "backend class missing" in status.reason
                for status in health.missing
            )
        finally:
            dsp._ensure_effect_registry = saved_probe

    def test_to_dict_is_serializable(self):
        data = get_effect_registry_health().to_dict()

        assert data["is_healthy"] is True
        assert len(data["effects"]) == len(AUDIO_MIXER_EFFECT_TYPES)
        assert "reason" in data["effects"][0]


class TestPanelDiagnostics:
    def test_healthy_registry_shows_effects_not_placeholder(self, qapp):
        panel = EditEffectsPanel()

        panel._populate_audio_effects(panel.audio_effects_tree)

        texts = []
        tree = panel.audio_effects_tree
        for i in range(tree.topLevelItemCount()):
            texts.append(tree.topLevelItem(i).text(0))
        combined = " ".join(texts)
        assert "coming soon" not in combined.lower()

    def test_empty_registry_lists_missing_effects_with_reasons(self, qapp):
        panel = EditEffectsPanel()

        # Swap in a registry whose audio registration failed, then
        # repopulate the audio tree.
        original = panel._registry
        panel._registry = _registry_without_audio()
        try:
            panel._populate_audio_effects(panel.audio_effects_tree)
        finally:
            panel._registry = original

        tree = panel.audio_effects_tree
        assert tree.topLevelItemCount() == 1
        header = tree.topLevelItem(0)
        assert "failed to load" in header.text(0).lower()
        # One child per missing builtin effect, each with a reason.
        assert header.childCount() == len(AUDIO_MIXER_EFFECT_TYPES)
        for i in range(header.childCount()):
            text = header.child(i).text(0)
            assert "missing from EffectRegistry" in text


class TestTransitionCardDoubleClick:
    def test_card_double_click_emits_apply_signal(self, qapp):
        panel = EditEffectsPanel()
        panel.set_selected_clip("clip-xyz")

        emitted = []
        panel.transition_apply_requested.connect(
            lambda payload, clip_id: emitted.append((payload, clip_id))
        )

        panel._on_transition_card_double_clicked("cross_dissolve")

        assert emitted == [("cross_dissolve", "clip-xyz")]

    def test_card_double_click_without_selection_uses_playhead(self, qapp):
        panel = EditEffectsPanel()
        panel.set_selected_clip(None)

        emitted = []
        panel.transition_apply_requested.connect(
            lambda payload, clip_id: emitted.append((payload, clip_id))
        )

        panel._on_transition_card_double_clicked("wipe:left")

        assert emitted == [("wipe:left", "")]
