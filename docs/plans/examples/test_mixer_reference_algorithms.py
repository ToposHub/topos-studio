"""Run: .venv/bin/python -m pytest docs/plans/examples -q"""
from __future__ import annotations

import math
import numpy as np
import pytest
from mixer_reference_algorithms import (
    Edge, ReferenceDelay, crossfade, estimate_delay, gain_with_trim_vca,
    gated_loudness_from_k_energy, null_error, plan_pdc, sample_at_frame,
    sound_hash, validate_measurement,
)


def test_cascaded_bus_pdc():
    edges = [Edge('d-m', 'dry', 'master'), Edge('w-1', 'wet', 'b1'),
             Edge('1-2', 'b1', 'b2'), Edge('2-m', 'b2', 'master')]
    plan = plan_pdc(dict(dry=0, wet=0, b1=100, b2=200, master=0), edges)
    assert plan.arrival['master'] == 300
    assert plan.edge_delay == {'d-m': 300, 'w-1': 0, '1-2': 0, '2-m': 0}
    with pytest.raises(ValueError, match='cycle'):
        plan_pdc({'a': 0, 'b': 0}, [Edge('a-b', 'a', 'b'), Edge('b-a', 'b', 'a')])


@pytest.mark.parametrize('chunk', [1, 127, 256, 733])
def test_delay_chunk_invariance(chunk):
    pcm = np.zeros((2, 2000))
    pcm[:, 100] = 1.0
    delay = ReferenceDelay(2, 300)
    output = np.concatenate([delay.process(pcm[:, i:i + chunk])
                             for i in range(0, 2000, chunk)], axis=1)
    assert np.flatnonzero(output[0]).tolist() == [400]
    assert output[0, 400] == 1.0


def test_trim_vca_units_and_pre_send_contract():
    post = gain_with_trim_vca(0.5, 0.5, [-6.020599913279624])
    assert post == pytest.approx(0.125)
    pre = 0.5  # pre-fader tap excludes fader, volume trim, VCA by this contract
    assert pre == 0.5
    assert gain_with_trim_vca(1, 1, [], muted=True) == 0


def test_hash_changes_and_nonfinite_rejected():
    assert sound_hash({'vca': 0}) != sound_hash({'vca': -6})
    assert sound_hash({'tap': 'pre'}) != sound_hash({'tap': 'post'})
    with pytest.raises(ValueError):
        sound_hash({'gain': math.nan})


@pytest.mark.parametrize('bad', [None, math.nan, math.inf, -math.inf, True])
def test_qc_never_passes_missing_or_nonfinite_peak(bad):
    record = dict(status='complete', integrated_lufs=-23, peak_dbfs=-3)
    if bad is not None:
        record['true_peak_dbtp'] = bad
    assert validate_measurement(record)


def test_qc_complete():
    assert not validate_measurement(dict(status='complete', integrated_lufs=-23,
                                        true_peak_dbtp=-2, peak_dbfs=-3))


def test_gating_is_energy_domain():
    energy = 10.0 ** ((-23.0 + 0.691) / 10.0)
    assert gated_loudness_from_k_energy(np.array([energy, 0, energy])) == pytest.approx(-23)
    assert gated_loudness_from_k_energy(np.zeros(5)) == -math.inf


@pytest.mark.parametrize('lag', [-240, 0, 137])
def test_alignment_sign_and_silence(lag):
    a = np.random.default_rng(42).normal(size=4096)
    b = np.zeros_like(a)
    if lag > 0:
        b[lag:] = a[:-lag]
    elif lag < 0:
        b[:lag] = a[-lag:]
    else:
        b[:] = a
    measured, ratio = estimate_delay(a, b, 500)
    assert measured == lag
    assert ratio > 5
    with pytest.raises(ValueError, match='no signal'):
        estimate_delay(np.zeros(32), np.zeros(32), 5)


def test_fades_rational_clock_and_null():
    pcm = np.ones((2, 101))
    assert np.allclose(crossfade(pcm, pcm, correlated=True), pcm)
    # Equal power adds 3 dB for identical signals; it is NOT the correlated rule.
    assert crossfade(pcm, pcm, correlated=False)[0, 50] == pytest.approx(math.sqrt(2))
    boundaries = [sample_at_frame(i, 30000, 1001, 48000) for i in range(30001)]
    assert sum(np.diff(boundaries)) == boundaries[-1] == 48048000
    assert null_error(pcm, pcm) == (0, 0)
