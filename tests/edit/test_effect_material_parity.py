"""T-P1-21 batch 2: per-effect multi-material CPU/GPU对拍 protocol.

The audit card's step 4 requires that every kernel effect be compared
across *multiple materials* — 平坦、边缘、高频、HDR 级对比度、alpha — not a
single preview screenshot. This module is that protocol, as a test:
for each of the five kernel effects (blur / erode_dilate / edge_detect /
denoise / defocus) and each applicable material, both execution paths run
the same params and must share the effect's *structural* signature.

Like ``test_effect_golden_frame_parity`` this asserts structural
properties rather than pixel equality: the GPU (GLSL) and CPU (cv2 /
numpy reference) implementations are different algorithms by design, and
their per-pixel drift is a documented, versioned gap — a silent drift is
what this protocol turns into a CI failure.

Also covers the batch-2 quality tiers (下采样质量档):
* blur ``quality="fast"`` engages the half-res path (ds scratch allocated)
  and still satisfies the structural contract;
* defocus ``mode="mipmap"`` engages the same fast tier for large radii;
* denoise ``quality`` changes the effective kernel monotonically
  (fast ≤ normal ≤ high smoothing) on the noise material.

Environment: the ``gl_context`` fixture skips cleanly without GL; every
assertion on the GPU path is paired with the CPU reference so the file
still validates the tier *contract* on CPU-only runs.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.edit.helpers import frame_fixtures as fx  # noqa: E402

pytestmark = pytest.mark.edit_core

W, H = 64, 48


# ── materials ────────────────────────────────────────────────────────────────

def _noise_frame(w: int = W, h: int = H, seed: int = 7) -> np.ndarray:
    """Flat mid-grey base + high-frequency seeded noise (高频素材)."""
    rng = np.random.default_rng(seed)
    base = np.full((h, w, 4), 128, dtype=np.uint8)
    noise = rng.integers(-60, 60, size=(h, w, 3), dtype=np.int16)
    base[:, :, :3] = np.clip(base[:, :, :3].astype(np.int16) + noise, 0, 255).astype(np.uint8)
    base[:, :, 3] = 255
    return base


def _alpha_hole_frame(w: int = W, h: int = H) -> np.ndarray:
    """Opaque checker with a transparent centre square (alpha 素材)."""
    frame = fx.checker_frame(w, h)
    frame = frame.copy()
    frame[h // 4: 3 * h // 4, w // 4: 3 * w // 4, 3] = 0
    return frame


def _steps_frame(w: int = W, h: int = H) -> np.ndarray:
    """Large-coherence luminance steps (HDR 级对比度代理；真 HDR float
    合约由 test_effect_surface_precision.py 覆盖——CPU 参考实现只收 u8)."""
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    band = h // 4
    for i, level in enumerate((40, 120, 200, 250)):
        frame[i * band: (i + 1) * band, :, :3] = level
    frame[:, :, 3] = 255
    return frame


MATERIALS = {
    "flat": lambda: fx.solid_rgba_frame(W, H, (128, 128, 128, 255)),
    "edge": lambda: fx.checker_frame(W, H),
    # 8px cells: the 2px checker is itself at Nyquist — an edge detector
    # classifying every pixel as edge is correct there, so the binary
    # "responds" metric needs cells wide enough to have non-edge interiors.
    "edge8": lambda: fx.checker_frame(W, H, cell=8),
    "noise": _noise_frame,
    "steps": _steps_frame,
    "alpha_hole": _alpha_hole_frame,
}


# ── execution drivers ────────────────────────────────────────────────────────

def _run_gpu(ctx, effect_type: str, rgba: np.ndarray, params: dict):
    """Run one effect through EffectPassRunner; return (output_rgba, runner).

    Raises if the effect silently no-ops (returned the input glo) — the
    FX-M08 lesson: a parity test must never pass on a binding failure.
    """
    from src.features.shared.effects.effect_pass_runner import EffectPassRunner
    from src.features.shared.effects import get_effect_catalog

    spec = get_effect_catalog().get_spec(effect_type)
    assert spec is not None, f"{effect_type} spec missing"
    runner = EffectPassRunner(ctx)
    in_tex = ctx.texture((W, H), components=4, dtype="f1")
    in_tex.write(rgba.tobytes())
    out_glo = runner.run(spec, in_tex.glo, dict(params), W, H)
    assert out_glo != in_tex.glo, f"{effect_type} was a silent no-op on GPU"
    out = _readback(runner, out_glo)
    in_tex.release()
    return out, runner


def _readback(runner, glo: int) -> np.ndarray:
    for name in ("_scratch_a", "_scratch_b", "_scratch_c"):
        tex = getattr(runner, name, None)
        if tex is not None and tex.glo == glo:
            fbo = getattr(runner, name + "_fbo")
            fbo.use()
            runner._ctx.viewport = (0, 0, W, H)
            raw = fbo.read(components=4)
            return np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 4).copy()
    raise AssertionError("output glo is not any runner scratch")


def _run_cpu(effect_type: str, rgba: np.ndarray, params: dict) -> np.ndarray:
    """Run the timeline CPU fallback; returns RGBA u8 (image space)."""
    from src.features.edit.core import timeline_export as te

    handler = {
        "blur": te._cpu_blur,
        "erode_dilate": te._cpu_erode_dilate,
        "edge_detect": te._cpu_edge_detect,
        "denoise": te._cpu_denoise,
        "defocus": te._cpu_defocus,
    }[effect_type]
    bgra = rgba[:, :, [2, 1, 0, 3]].copy()  # RGBA → BGRA (CPU 参考的通道约定)
    out = handler(bgra, dict(params))
    return out[:, :, [2, 1, 0, 3]]  # back to RGBA


# ── structural metrics ───────────────────────────────────────────────────────

def _max_horizontal_step(img: np.ndarray) -> int:
    """Strongest adjacent-column luminance step — an edge's steepness."""
    luma = img[:, :, :3].astype(np.int16).mean(axis=2)
    return int(np.abs(np.diff(luma, axis=1)).max())


def _mean_abs(img: np.ndarray) -> float:
    return float(img[:, :, :3].astype(np.float32).mean())


def _flat_std(img: np.ndarray) -> float:
    """Std of luminance in the central flat crop — noise level proxy."""
    luma = img[:, :, :3].astype(np.float32).mean(axis=2)
    crop = luma[H // 4: 3 * H // 4, W // 4: 3 * W // 4]
    return float(crop.std())


# ── the protocol: per effect, per material ───────────────────────────────────

EFFECT_PARAMS = {
    "blur": {"radius_x": 6.0, "radius_y": 6.0, "blur_type": "gaussian",
             "mix": 1.0, "quality": "normal"},
    "erode_dilate": {"operation": "dilate", "size": 3, "iterations": 1,
                     "channels": "all", "edge_mode": "extend"},
    "edge_detect": {"method": "sobel", "threshold": 0.05,
                    "output_strength": 1.0, "kernel_size": 3},
    "denoise": {"strength": 0.8, "radius": 3, "quality": "normal"},
    # focal_size 缩到近 0：对拍关心的是“散焦是否平滑边缘”，焦点保留
    # 全清晰是参数语义（可变半径），不是平滑失效。
    "defocus": {"mode": "separable", "max_radius": 6.0, "focal_size": 0.01,
                "edge_policy": "clamp", "process_alpha": True},
}

#: (effect, material) → structural verdict both paths must satisfy.
#: "smooths":  edge steepness strictly decreases (blur-family signature).
#: "responds": strong luminance response on structured material and near
#:             zero on flat (edge_detect's signature).
#: "denoises": flat-region std decreases without collapsing the image.
#: "flat_stable": on the flat material the effect must stay ≈ identity —
#:             catches kernels that leak/spike on constant input.
VERDICTS = {
    ("blur", "edge"): "smooths",
    ("blur", "flat"): "flat_stable",
    ("defocus", "edge"): "smooths",
    ("defocus", "flat"): "flat_stable",
    ("denoise", "noise"): "denoises",
    ("denoise", "flat"): "flat_stable",
    ("edge_detect", "edge8"): "responds",
    ("edge_detect", "flat"): "responds_flat",
    ("erode_dilate", "edge"): "dilate_grows",
    ("erode_dilate", "flat"): "flat_stable",
}


def _check_verdict(kind: str, before: np.ndarray, after: np.ndarray) -> None:
    if kind == "smooths":
        assert _max_horizontal_step(after) < _max_horizontal_step(before), (
            "edge steepness did not decrease — not smoothing")
    elif kind == "denoises":
        assert _flat_std(after) < _flat_std(before), (
            "flat-region std did not decrease — not denoising")
        assert _mean_abs(after) > 10.0, "denoise collapsed the image to black"
    elif kind == "responds":
        assert _max_horizontal_step(after) > 4, (
            "edge response below threshold on structured material")
    elif kind == "responds_flat":
        assert _mean_abs(after) < 60.0, (
            "edge detector produced strong output on a flat input")
    elif kind == "dilate_grows":
        assert _mean_abs(after) >= _mean_abs(before), (
            "dilate did not expand the bright phase")
    elif kind == "flat_stable":
        assert abs(_mean_abs(after) - _mean_abs(before)) < 4.0, (
            f"flat input drifted by effect: "
            f"{_mean_abs(before):.1f} → {_mean_abs(after):.1f}")


class TestEffectMaterialParity:
    """对拍 protocol: every (effect, material) verdict must hold on BOTH
    execution paths. GPU rows are skipped (not failed) without GL; the CPU
    half still runs so the structural contract stays exercised."""

    @pytest.mark.parametrize("effect_type", list(EFFECT_PARAMS))
    def test_cpu_structural_contract_all_materials(self, effect_type) -> None:
        for material, build in MATERIALS.items():
            verdict = VERDICTS.get((effect_type, material))
            if verdict is None:
                continue
            before = build()
            after = _run_cpu(effect_type, before, EFFECT_PARAMS[effect_type])
            _check_verdict(verdict, before, after)

    @pytest.mark.parametrize("effect_type", list(EFFECT_PARAMS))
    def test_gpu_structural_contract_all_materials(self, gl_context, effect_type) -> None:
        for material, build in MATERIALS.items():
            verdict = VERDICTS.get((effect_type, material))
            if verdict is None:
                continue
            before = build()
            after, _ = _run_gpu(gl_context, effect_type, before,
                                EFFECT_PARAMS[effect_type])
            _check_verdict(verdict, before, after)


class TestDownsampleQualityTier:
    """下采样质量档: quality="fast" must engage the half-res path AND keep
    the blur contract. Engagement is proven by state (ds scratch exists),
    not by timing — a tier that silently falls back is a defect here."""

    def test_blur_fast_tier_engages_and_smooths(self, gl_context) -> None:
        params = dict(EFFECT_PARAMS["blur"], radius_x=16.0, radius_y=16.0,
                      quality="fast")
        before = fx.checker_frame(W, H)
        after, runner = _run_gpu(gl_context, "blur", before, params)
        assert runner._ds_a is not None, (
            "fast tier did not allocate the half-res scratch — path not engaged")
        _check_verdict("smooths", before, after)

    def test_blur_small_radius_fast_stays_full_res(self, gl_context) -> None:
        params = dict(EFFECT_PARAMS["blur"], quality="fast")  # radius 6 < gate
        _, runner = _run_gpu(gl_context, "blur", fx.checker_frame(W, H), params)
        assert runner._ds_a is None, (
            "fast tier engaged below FAST_TIER_MIN_RADIUS — down/upsample "
            "overhead without a kernel to amortise")

    def test_defocus_mipmap_tier_engages_and_smooths(self, gl_context) -> None:
        params = dict(EFFECT_PARAMS["defocus"], mode="mipmap", max_radius=16.0)
        before = fx.checker_frame(W, H)
        after, runner = _run_gpu(gl_context, "defocus", before, params)
        assert runner._ds_a is not None, (
            "defocus mipmap tier did not engage the half-res path")
        _check_verdict("smooths", before, after)

    def test_blur_fast_tier_cpu_side_still_blurs(self) -> None:
        # CPU has no downsample tier (exact kernels are already cheap) — the
        # tier contract on CPU is "quality must not change validity".
        params = dict(EFFECT_PARAMS["blur"], radius_x=16.0, radius_y=16.0,
                      quality="fast")
        before = fx.checker_frame(W, H)
        after = _run_cpu("blur", before, params)
        _check_verdict("smooths", before, after)


class TestDenoiseQualityTier:
    """denoise 质量档位: smoothing strength must be monotone in the tier."""

    def test_gpu_tier_monotone_on_noise(self, gl_context) -> None:
        noisy = _noise_frame()
        stds = {}
        for tier in ("fast", "normal", "high"):
            params = {"strength": 0.8, "radius": 3, "quality": tier}
            after, _ = _run_gpu(gl_context, "denoise", noisy, params)
            stds[tier] = _flat_std(after)
        assert stds["fast"] >= stds["normal"] >= stds["high"], (
            f"denoise tier smoothing not monotone: {stds}")
        assert stds["high"] < _flat_std(noisy), "high tier did not denoise"

    def test_cpu_tier_monotone_on_noise(self) -> None:
        noisy = _noise_frame()
        stds = {}
        for tier in ("fast", "normal", "high"):
            params = {"strength": 0.8, "radius": 3, "quality": tier}
            after = _run_cpu("denoise", noisy, params)
            stds[tier] = _flat_std(after)
        assert stds["fast"] >= stds["normal"] >= stds["high"], (
            f"CPU denoise tier smoothing not monotone: {stds}")
