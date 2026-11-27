from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import DelayEffect, EffectChain, ReverbEffect
from src.features.edit.core.audio_effects.reverb_analysis import predict_reverb_reflections
from src.features.edit.core.timeline_v2 import ReverbParams
from src.features.mixer.effects.panels.delay import _cross_feedback_echoes
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget


@pytest.mark.parametrize("rate", [44100, 96000])
def test_room_prediction_matches_reflections_and_late_onset(rate: int) -> None:
    params = ReverbParams(
        height=3.0, length=7.0, width=10.0, distance=14.0,
        pre_delay=-70.0, modulation=0.0, dry_wet=100.0,
        er_hi_gain=0.0, rev_lo_gain=0.0, rev_hi_gain=0.0,
        reverb_db=-60.0,
    ).to_dict()
    reflections, late_ms = predict_reverb_reflections(params, rate)
    pulse = np.zeros((2, rate // 4), dtype=np.float64)
    pulse[:, 0] = 1.0
    effect = ReverbEffect(rate)
    effect.set_params(params)
    early = effect.process(pulse)
    for (begin, end), track in zip(((0, 10), (10, 20)), early):
        predicted = np.zeros_like(track)
        for arrival_ms, weight in reflections[begin:end]:
            predicted[round(arrival_ms * rate / 1000.0)] += weight
        np.testing.assert_allclose(track, predicted, atol=1e-15)

    effect.reset_state()
    effect.set_params(params | {"er_db": -60.0, "reverb_db": 0.0})
    late = effect.process(pulse)
    onset = np.flatnonzero(np.any(late != 0.0, axis=0))[0]
    assert onset == pytest.approx(late_ms * rate / 1000.0, abs=1.0)


@pytest.mark.parametrize("taps", [(1.0, 1.0), (1.0, 80.25), (17.8, 1.0)])
def test_zero_delay_feedback_matches_independent_sample_recurrence(
    taps: tuple[float, float],
) -> None:
    rate = 48000
    effect = DelayEffect(rate)
    params = {"left_delay": taps[0] * 1000 / rate if taps[0] > 1 else 0.0,
              "right_delay": taps[1] * 1000 / rate if taps[1] > 1 else 0.0,
              "dry_wet": 100.0,
              "left_feedback": 88.0, "right_feedback": -93.0,
              "stereo_blend": 72.0, "high_ratio": 91.0}
    effect.set_params(params)
    effect._in_sos = None
    data = np.random.default_rng(42).normal(0.0, 0.1, (2, 2607))
    wet = np.empty_like(data)
    history = np.zeros_like(data)
    filter_state = np.zeros(2)
    for i in range(data.shape[1]):
        for ch, delay in enumerate(taps):
            read = i - delay
            before = int(np.floor(read))
            fraction = read - before
            if before >= 0:
                wet[ch, i] = history[ch, before] * (1.0 - fraction)
            else:
                wet[ch, i] = 0.0
            if fraction and before + 1 >= 0:
                wet[ch, i] += history[ch, before + 1] * fraction
        left, right = wet[:, i]
        feedback = np.array([
            left * 0.28 * 0.88 + right * 0.72 * -0.93,
            right * 0.28 * -0.93 + left * 0.72 * 0.88,
        ])
        filter_state = effect._damp_b[0] * feedback - effect._damp_a[1] * filter_state
        history[:, i] = data[:, i] + filter_state

    chunks = np.split(data, [1, 10, 256, 1999], axis=1)
    actual = np.concatenate([effect.process(block) for block in chunks], axis=1)
    np.testing.assert_allclose(actual, wet, atol=2e-14)
    assert min(effect.collect_graph_data()[f"read_head_{ch}_samples"]
               for ch in ("left", "right")) == 1
    assert effect.tail_estimate_samples() > 0


def test_delay_switching_off_feedback_preserves_filter_release_between_blocks() -> None:
    params = {"left_delay": 0.07, "right_delay": 0.11, "high_ratio": 100.0,
              "left_feedback": 85.0, "right_feedback": -90.0, "dry_wet": 100.0}
    data = np.random.default_rng(77).normal(0.0, 0.1, (2, 500))
    released = []
    for cuts in ([], [2, 4, 18, 51]):
        effect = DelayEffect()
        effect.set_params(params)
        effect.process(data[:, :256])
        effect.set_params(params | {"left_feedback": 0.0, "right_feedback": 0.0})
        released.append(np.concatenate(
            [effect.process(block) for block in np.split(data[:, 256:], cuts, axis=1)],
            axis=1))
    np.testing.assert_allclose(*released, atol=1e-14)


def test_delay_rate_change_initializes_taps_at_new_sample_rate() -> None:
    params = {"left_delay": 12.3, "right_delay": 18.5, "dry_wet": 100.0}
    changed, fresh = DelayEffect(48000), DelayEffect(44100)
    changed.set_params(params)
    data = np.random.default_rng(43).normal(0.0, 0.1, (2, 4800))
    changed.process(data)
    changed.set_params(None, sample_rate=44100)
    changed.set_params(params, sample_rate=44100)
    fresh.set_params(params)
    np.testing.assert_array_equal(changed.process(data), fresh.process(data))


def test_room_tail_budget_includes_large_room_prediction() -> None:
    params = ReverbParams(
        height=30.0, length=80.0, width=80.0, distance=30.0,
        pre_delay=200.0, reverb_time=100.0, modulation=0.0,
        er_hi_gain=0.0, reverb_db=-60.0,
    ).to_dict()
    rate = 48000
    reflections, _ = predict_reverb_reflections(params, rate)
    last = round(max(ms for ms, _ in reflections) * rate / 1000.0)
    effect = ReverbEffect(rate)
    effect.set_params(params)
    assert last > round(0.2 * rate)
    assert effect.tail_estimate_samples() > last
    pulse = np.zeros((2, effect.tail_estimate_samples()), dtype=np.float32)
    pulse[:, 0] = 1.0
    assert np.any(effect.process(pulse)[:, last] > 0.0)


def test_delay_graph_shows_successive_ping_pong_echoes() -> None:
    left, right = _cross_feedback_echoes((30.0, 60.0), (0.5, -0.75), 1.0, 1.0, 2000.0)
    assert left[90] == pytest.approx(-0.75)
    assert right[90] == pytest.approx(0.5)
    assert left[120] == right[150] == pytest.approx(-0.375)
    assert left[180] == pytest.approx(-0.75 * -0.375)


def test_short_delay_tail_budget_covers_damped_feedback() -> None:
    effect = DelayEffect()
    effect.set_params({"left_delay": 0.0, "right_delay": 0.0,
                       "left_feedback": 99.0, "right_feedback": 99.0,
                       "high_ratio": 100.0, "stereo_blend": 0.0, "dry_wet": 100.0})
    impulse = np.zeros((2, 9000), dtype=np.float64)
    impulse[0, 0] = 1.0
    output = np.abs(effect.process(impulse))
    assert np.max(output[:, effect.tail_estimate_samples():]) < output.max() * 1e-3


def test_delay_rack_drag_is_one_preview_and_meters_follow_each_channel(qtbot: Any) -> None:
    strip = TrackMixerWidget("space", "Audio")
    qtbot.addWidget(strip)
    strip._effect_add("delay")
    instance = strip._effect_chain.effects[0]
    strip._open_insert_effect(instance)
    _, _, dialog = next(strip._iter_insert_dialogs())
    try:
        dialog.panel.btn_link.click()
        edits = []
        dialog.panel.stateChanged.connect(edits.append)
        with qtbot.waitSignal(dialog.preview, timeout=1000):
            dialog.panel.tap_graph.tapDragged.emit("L", 8.5, -30.0)
        assert len(edits) == 1
        params = dialog.current_params()
        assert params["left_delay"] == params["right_delay"] == 8.5
        assert params["left_feedback"] == params["right_feedback"] == -30.0
        strip.set_engine_sample_rate(44100)
        assert dialog.panel.tap_graph._sample_rate == 44100
        assert not dialog.panel.tap_graph.grab().isNull()

        chain = EffectChain(44100)
        chain.set_params([("delay", params | {"dry_wet": 0.0}, True, instance.instance_id)])
        data = np.repeat([[0.5], [0.0625]], 512, axis=1)
        chain.process(data)
        tap = chain.tap_levels()[instance.instance_id]
        assert tap["out_db_lr"] == pytest.approx((-6.0206, -24.0824))
        assert tap["out_db"] == tap["out_db_lr"][0]
        strip.update_track_telemetry({"taps": chain.tap_levels()})
        meter = dialog.panel.output_meter
        assert (meter._l_db, meter._r_db) == pytest.approx(tap["out_db_lr"])
    finally:
        dialog.close()
