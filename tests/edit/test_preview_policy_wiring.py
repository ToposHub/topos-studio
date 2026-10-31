"""Production-wiring regressions for FX-M04 preview policy."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.features.shared.effects.preview_policy import DegradationMode


def _renderer(*, playing: bool, budget_ms: float = 0.0) -> EditGpuPreviewRenderer:
    renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
    renderer._effect_runner = MagicMock()
    renderer._effect_runner.run.return_value = 77
    renderer._effect_hdr = False
    renderer._preview_degraded = playing
    renderer._preview_gpu_budget_ms = budget_ms
    renderer._last_preview_policy_decisions = ()
    return renderer


def test_playback_state_reaches_gpu_policy_switch() -> None:
    owner = TimelinePreviewRenderer.__new__(TimelinePreviewRenderer)
    owner._gpu_renderer = MagicMock()
    owner._realtime_engine = MagicMock()
    owner._prewarm_current_playback_window = MagicMock()
    owner.playback_mode_changed = MagicMock()
    owner._quality_transition_token = 0
    owner._full_quality_refresh_pending = False
    owner._full_quality_refresh_timer = None
    owner._scrub_active = False
    owner._decode_quality_stage = "auto2k"
    owner._full_quality_delay_ms = 200
    owner._worker = None
    owner._idle_prebuffer_enabled_flag = False

    owner.notify_playback_state(True)
    owner.notify_playback_state(False)

    assert owner._gpu_renderer.set_preview_degraded.call_args_list == [
        ((True,), {}),
        ((False,), {}),
    ]


def test_playing_budget_bypasses_denoise_without_mutating_params() -> None:
    renderer = _renderer(playing=True)
    source = MagicMock(glo=11)
    effect = EffectInstance(
        effect_id="denoise-1",
        effect_type="denoise",
        params={"strength": 0.8, "radius": 3},
    )
    before = effect.to_dict()

    result = renderer._apply_clip_effects(source, (effect,), 3840, 2160)

    assert result is source
    renderer._effect_runner.run.assert_not_called()
    decision = renderer.get_preview_policy_diagnostics()[0]
    assert decision.mode == DegradationMode.BYPASS
    assert effect.to_dict() == before


def test_paused_preview_forces_full_quality() -> None:
    renderer = _renderer(playing=False)
    source = MagicMock(glo=11)
    effect = EffectInstance(
        effect_id="denoise-1",
        effect_type="denoise",
        params={"strength": 0.8, "radius": 3},
    )

    renderer._apply_clip_effects(source, (effect,), 3840, 2160)

    renderer._effect_runner.run.assert_called_once()
    decision = renderer.get_preview_policy_diagnostics()[0]
    assert decision.mode == DegradationMode.FULL


def test_playing_budget_passes_capped_params_to_runner() -> None:
    renderer = _renderer(playing=True)
    source = MagicMock(glo=11)
    effect = EffectInstance(
        effect_id="blur-1",
        effect_type="blur",
        params={"radius_x": 40.0, "radius_y": 30.0, "mix": 1.0},
    )
    before = dict(effect.params)

    renderer._apply_clip_effects(source, (effect,), 3840, 2160)

    call = renderer._effect_runner.run.call_args
    frame_params = call.args[2]
    assert frame_params["radius_x"] == 8.0
    assert frame_params["radius_y"] == 8.0
    assert effect.params == before
    assert renderer.get_preview_policy_diagnostics()[0].mode == DegradationMode.PARAMETER_CAP
