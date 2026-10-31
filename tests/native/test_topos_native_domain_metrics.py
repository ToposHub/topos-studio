"""原生域报告须显露 half-float 带符号零的失真。"""

from __future__ import annotations

import numpy as np

from tools.measure_topos_native_domains import _float_metrics


def test_float_metrics_separates_zero_sign_and_zero_magnitude_changes() -> None:
    source = np.array([0x8000, 0x0000, 0x0001], dtype=np.uint16)
    decoded = np.array([0x0000, 0x0001, 0x0000], dtype=np.uint16)
    channel = _float_metrics((source,) * 3, (decoded,) * 3)["per_channel"][0]
    assert channel["zero_sign_changes"] == 1
    assert channel["source_zero_to_nonzero"] == 1
    assert channel["negative_zero_to_nonzero"] == 0
    assert channel["source_nonzero_to_zero"] == 1

    decoded[0] = 0x0001
    channel = _float_metrics((source,) * 3, (decoded,) * 3)["per_channel"][0]
    assert channel["negative_zero_to_nonzero"] == 1
