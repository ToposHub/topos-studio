"""Regression tests for Adjustment Clip Transform/Opacity semantics."""

from __future__ import annotations

import numpy as np

from src.features.edit.core.adjustment_composite import (
    apply_transform_cpu,
    blend_adjustment_cpu,
)


IDENTITY = (0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)


def test_adjustment_opacity_blends_adjusted_result_with_lower_composite() -> None:
    base = np.full((2, 2, 3), 100.0, dtype=np.float32)
    adjusted = np.full((2, 2, 3), 200.0, dtype=np.float32)

    result = blend_adjustment_cpu(base, adjusted, IDENTITY, 0.25)

    assert np.allclose(result, 125.0)


def test_adjustment_transform_replaces_lower_composite_at_full_opacity() -> None:
    base = np.full((4, 4, 3), 100.0, dtype=np.float32)
    adjusted = np.full((4, 4, 3), 200.0, dtype=np.float32)
    translate_right = (2.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)

    transformed = apply_transform_cpu(adjusted, translate_right, 4, 4)
    result = blend_adjustment_cpu(base, transformed, translate_right, 1.0)

    # The transformed result replaces the original lower composite at 100%,
    # so the left border is transparent/black rather than the old full frame.
    assert np.all(result[:, 0] == 0.0)
    assert np.all(result[:, 2] == 200.0)
