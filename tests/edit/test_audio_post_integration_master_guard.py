"""The final peak guard must not normalize earlier samples by a future peak."""

import numpy as np

from src.features.edit.core.audio_dsp import MasterLimiter


def test_master_guard_is_causal_before_a_later_overload() -> None:
    signal = np.full((2, 6000), 0.2, np.float32)
    signal[:, 3500] = 3.0
    processed = MasterLimiter().process(signal.copy())
    np.testing.assert_array_equal(processed[:, :3500], signal[:, :3500])
    assert np.max(np.abs(processed)) <= 1.0


def test_master_guard_output_is_independent_of_block_partition() -> None:
    signal = np.full((2, 12000), 0.2, np.float64)
    signal[0, [7, 3500, 8193]] = [3.0, -4.0, 2.0]
    signal[1, [28, 9600]] = [-2.0, 3.0]
    reference = MasterLimiter().process(signal.copy())
    limiter = MasterLimiter()
    pieces = []
    position = 0
    sizes = (1, 71, 1920, 4093, 256, 3)
    index = 0
    while position < signal.shape[-1]:
        count = sizes[index % len(sizes)]
        pieces.append(limiter.process(signal[:, position:position + count].copy()))
        position += count
        index += 1
    np.testing.assert_allclose(np.concatenate(pieces, axis=-1), reference,
                               rtol=0.0, atol=1e-12)


def test_guard_preserves_stereo_ratio_at_overloaded_sample() -> None:
    signal = np.array([[3.0, 0.0], [0.6, 0.0]], dtype=np.float64)
    output = MasterLimiter().process(signal)
    assert abs(output[0, 0] - 1.0) < 1e-12
    assert abs(output[1, 0] - 0.2) < 1e-12
