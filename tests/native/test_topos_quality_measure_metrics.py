"""质量报告优化后仍须保持原有指标定义。"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter

from tools.measure_topos_baseline_quality import block_index_y, ssim_y


def test_block_index_matches_original_boundary_definition() -> None:
    rng = np.random.default_rng(23)
    y = rng.integers(0, 1024, (31, 39), dtype=np.uint16)
    f = y.astype(np.float64)
    dx = np.abs(np.diff(f, axis=1))
    dy = np.abs(np.diff(f, axis=0))
    original = 0.5 * (
        dx[:, 7::8].mean() / np.delete(dx, np.s_[7::8], axis=1).mean()
        + dy[7::8, :].mean() / np.delete(dy, np.s_[7::8], axis=0).mean()
    )
    assert abs(block_index_y(y) - original) < 1e-12


def test_float32_ssim_matches_previous_float64_metric() -> None:
    rng = np.random.default_rng(42)
    a = rng.uniform(0, 255, (31, 39))
    b = a + rng.normal(0, 2, a.shape)
    mu_a, mu_b = gaussian_filter(a, 1.5), gaussian_filter(b, 1.5)
    va = gaussian_filter(a * a, 1.5) - mu_a * mu_a
    vb = gaussian_filter(b * b, 1.5) - mu_b * mu_b
    cov = gaussian_filter(a * b, 1.5) - mu_a * mu_b
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    original = np.mean(
        ((2 * mu_a * mu_b + c1) * (2 * cov + c2))
        / ((mu_a**2 + mu_b**2 + c1) * (va + vb + c2))
    )
    assert abs(ssim_y(a, b) - original) < 1e-4
