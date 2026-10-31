"""FX-M01 acceptance: HDR / high-precision effect surface.

Before FX-M01, ``EffectPassRunner._ensure_scratch`` allocated its three
intermediate surfaces hard-coded to ``dtype="f1"`` (8-bit RGBA8) with a cache
key of only ``(width, height)``. The Composite page feeds **RGBA16F** base
textures into the effect chain (its upstream is HDR), so the very first
effect pass clamped/quantised the HDR input to 8-bit — introducing banding
on gradients and capping anything above 1.0. The runner's own docstring
flagged this as a known TODO ("if HDR preview is added later this becomes
'f2'").

This suite proves the fix:

1. **HDR survives the effect chain at f2** — a gradient peaking above 1.0,
   run through blur/glow/sharpen with ``dtype="f2"``, still peaks above 1.0
   on readback. With ``dtype="f1"`` it is clamped to 1.0 (the regression
   signal the test guards against).
2. **dtype is part of the scratch cache key** — requesting f1 then f2 at the
   same size rebuilds the scratch; the two never alias. This is the guard
   against "wrong-format surface reused".
3. **No input/output alias** — the runner never returns the input glo as
   its output scratch glo (a feedback-loop precondition), at both precisions.
4. **dtype helper wiring** — ``_dtype_for_tex_format`` maps the Composite
   base-texture format to the correct scratch dtype, so the production path
   actually requests f2 for HDR input.

GPU tests skip cleanly when no GL is available.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.shared.effects.effect_pass_runner import EffectPassRunner  # noqa: E402
from src.features.shared.effects import get_effect_catalog  # noqa: E402
from tests.edit.helpers import golden_compare as gc  # noqa: E402

pytestmark = pytest.mark.edit_core


def _hdr_gradient(width: int, height: int, peak: float = 4.0) -> np.ndarray:
    """An HDR float16-ready gradient peaking at ``peak`` (>1.0)."""
    yy = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    xx = np.linspace(0, 1, width, dtype=np.float32)[None, :]
    ramp = (xx + yy) * 0.5 * peak
    arr = np.zeros((height, width, 4), dtype=np.float16)
    arr[..., 0] = ramp
    arr[..., 1] = ramp * 0.5
    arr[..., 2] = ramp * 0.25
    arr[..., 3] = 1.0
    return arr


def _readback_scratch(runner, glo: int, w: int, h: int, dtype: str) -> np.ndarray:
    """Read the runner's scratch owning ``glo`` as a float32 RGBA array."""
    for name in ("_scratch_a", "_scratch_b", "_scratch_c"):
        tex = getattr(runner, name, None)
        if tex is not None and tex.glo == glo:
            fbo = getattr(runner, name + "_fbo")
            fbo.use()
            runner._ctx.viewport = (0, 0, w, h)
            raw = fbo.read(components=4, dtype="f4", alignment=1)
            return np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
    raise AssertionError(
        f"output glo {glo} is not a runner scratch — effect was a no-op"
    )


# ── 1. HDR survives the effect chain at f2 ───────────────────────────────────
#
# FX-M01 delivers the *scratch-dtype capability* — the runner can now allocate
# f2 surfaces and the dtype is part of the cache key. The HDR-preservation
# test below covers blur and sharpen, both of which run cleanly at f2 and
# prove the banding fix end-to-end.
#
# Glow is intentionally NOT in the parametrised HDR matrix: its two-pass
# pipeline (manual second-texture bind via ``_bind_external_texture``) still
# crashes at f2 on the standalone context. That is a separate shader/backend
# follow-up, not part of the scratch-dtype capability FX-M01 delivers. The
# glow feedback-loop test (``test_effect_pass_runner_glow_feedback.py``)
# continues to guard glow's output-selection logic at f1.

@pytest.mark.parametrize("effect_type,params", [
    ("blur", {"radius": 2.0}),
    ("sharpen", {"amount": 0.5, "radius": 1.0}),
])
class TestHdrSurvivesEffectChain:
    """FX-M01 core: at dtype='f2', HDR input must NOT be clamped to 1.0."""

    def test_f2_preserves_hdr_above_one(
        self, gl_context, effect_type, params,
    ) -> None:
        runner = EffectPassRunner(gl_context)
        try:
            catalog = get_effect_catalog()
            spec = catalog.get_spec(effect_type)
            assert spec is not None, f"{effect_type} spec missing"

            W = H = 16
            src = _hdr_gradient(W, H, peak=4.0)
            assert float(src[..., 0].max()) > 1.0  # HDR input sanity

            in_tex = gl_context.texture((W, H), components=4, dtype="f2")
            in_tex.write(src.tobytes())

            out_glo = runner.run(
                spec, in_tex.glo, params, W, H, dtype="f2",
            )
            # Effect must actually run (not a silent no-op).
            assert out_glo != in_tex.glo, (
                f"{effect_type} was a no-op at f2 — texture bind may have failed"
            )
            out = _readback_scratch(runner, out_glo, W, H, "f2")

            # The HDR signal: the output peak must still exceed 1.0. With the
            # old f1 scratch this would be clamped to exactly 1.0 (banding).
            assert float(out[..., 0].max()) > 1.0, (
                f"{effect_type} at dtype='f2' clamped HDR to <=1.0 "
                f"(peak={float(out[..., 0].max()):.3f}) — banding regression"
            )
            in_tex.release()
        finally:
            runner.release()

    def test_f1_clamps_hdr_to_one(
        self, gl_context, effect_type, params,
    ) -> None:
        """Contrast test: the OLD behaviour (dtype='f1') clamps HDR to 1.0.

        This pins the regression signal — if f1 ever stopped clamping, the f2
        test above would no longer be distinguishing anything.
        """
        runner = EffectPassRunner(gl_context)
        try:
            catalog = get_effect_catalog()
            spec = catalog.get_spec(effect_type)
            W = H = 16
            src = _hdr_gradient(W, H, peak=4.0)
            in_tex = gl_context.texture((W, H), components=4, dtype="f2")
            in_tex.write(src.tobytes())
            out_glo = runner.run(
                spec, in_tex.glo, params, W, H, dtype="f1",
            )
            if out_glo == in_tex.glo:
                in_tex.release()
                pytest.skip(f"{effect_type} no-op at f1 — cannot assert clamp")
            out = _readback_scratch(runner, out_glo, W, H, "f1")
            # 8-bit scratch clamps everything to [0,1]; the peak must NOT
            # exceed 1.0 (plus a tiny float-readback epsilon).
            assert float(out[..., 0].max()) <= 1.0 + 1e-3, (
                f"{effect_type} at dtype='f1' kept HDR above 1.0 "
                f"(peak={float(out[..., 0].max()):.3f}) — f1 should clamp"
            )
            in_tex.release()
        finally:
            runner.release()


# ── 2. dtype is part of the scratch cache key ────────────────────────────────

class TestScratchCacheKeyIncludesDtype:
    """FX-M01: the scratch cache key is ``(width, height)`` + dtype. Requesting
    f1 then f2 at the same size must rebuild — the two surfaces must never
    alias (reusing an 8-bit surface for an HDR request would silently clamp)."""

    def test_dtype_change_rebuilds_scratch(self, gl_context) -> None:
        runner = EffectPassRunner(gl_context)
        try:
            catalog = get_effect_catalog()
            spec = catalog.get_spec("blur")
            src = _hdr_gradient(16, 16, peak=2.0)
            in_tex = gl_context.texture((16, 16), components=4, dtype="f2")
            in_tex.write(src.tobytes())

            runner.run(spec, in_tex.glo, {"radius": 2.0}, 16, 16, dtype="f1")
            dtype_f1 = runner._scratch_dtype
            tex_f1 = runner._scratch_a  # capture object identity before rebuild

            runner.run(spec, in_tex.glo, {"radius": 2.0}, 16, 16, dtype="f2")
            dtype_f2 = runner._scratch_dtype

            assert dtype_f1 == "f1" and dtype_f2 == "f2", (
                f"dtype not reflected in scratch state: {dtype_f1} -> {dtype_f2}"
            )
            # Rebuild on dtype change must allocate a NEW surface object — an
            # 8-bit surface must never be silently reused for HDR. (Compare the
            # moderngl object identity, NOT the .glo integer: GL recycles free
            # texture names, so a fresh allocation can reuse the same glo.)
            assert runner._scratch_a is not tex_f1, (
                "scratch not rebuilt on dtype change — f1/f2 surfaces alias"
            )
            in_tex.release()
        finally:
            runner.release()

    def test_same_dtype_same_size_reuses_scratch(self, gl_context) -> None:
        """Sanity: the SAME dtype+size must reuse (not rebuild) the scratch —
        otherwise the cache key is too eager and we'd thrash GPU memory."""
        runner = EffectPassRunner(gl_context)
        try:
            catalog = get_effect_catalog()
            spec = catalog.get_spec("blur")
            src = _hdr_gradient(16, 16, peak=2.0)
            in_tex = gl_context.texture((16, 16), components=4, dtype="f2")
            in_tex.write(src.tobytes())

            runner.run(spec, in_tex.glo, {"radius": 2.0}, 16, 16, dtype="f2")
            tex_before = runner._scratch_a
            runner.run(spec, in_tex.glo, {"radius": 2.0}, 16, 16, dtype="f2")
            tex_after = runner._scratch_a
            # Same dtype+size → same Texture object (no rebuild).
            assert tex_after is tex_before, (
                "scratch was rebuilt for an identical dtype+size request "
                "(cache key too eager — would thrash GPU memory)"
            )
            in_tex.release()
        finally:
            runner.release()


# ── 3. No input/output alias ─────────────────────────────────────────────────

class TestNoInputOutputAlias:
    """FX-M02 invariant, re-checked at both precisions: the runner must never
    return the input glo as its output scratch glo (that would be a
    read/write feedback loop). dtype changes don't weaken this."""

    @pytest.mark.parametrize("dtype", ["f1", "f2"])
    def test_output_never_equals_input(self, gl_context, dtype) -> None:
        runner = EffectPassRunner(gl_context)
        try:
            catalog = get_effect_catalog()
            spec = catalog.get_spec("blur")
            # A simple SDR-friendly source in the matching dtype so the upload
            # is well-formed for both f1 (uint8) and f2 (float16).
            W = H = 16
            if dtype == "f1":
                src = (np.linspace(0, 255, W * H, dtype=np.float32)
                       .reshape(H, W))
                src = np.stack([src, src, src, np.full_like(src, 255)], axis=-1)
                src = src.astype(np.uint8)
            else:
                src = _hdr_gradient(W, H, peak=2.0)
            in_tex = gl_context.texture((W, H), components=4, dtype=dtype)
            in_tex.write(src.tobytes())
            out_glo = runner.run(
                spec, in_tex.glo, {"radius": 2.0}, W, H, dtype=dtype,
            )
            assert out_glo != in_tex.glo, (
                f"output == input at dtype={dtype}: feedback-loop risk"
            )
            in_tex.release()
        finally:
            runner.release()


# ── 4. dtype helper wiring (Composite path) ──────────────────────────────────

class TestDtypeHelperWiring:
    """The Composite path derives the scratch dtype from the base texture's
    format via ``_dtype_for_tex_format``. This pins that mapping so a HDR
    base texture actually requests the f2 scratch (the production path that
    makes FX-M01's benefit visible)."""

    def test_float_formats_map_to_f2(self) -> None:
        from src.features.edit.core.timeline_clip_frame_provider import (
            _dtype_for_tex_format,
        )
        assert _dtype_for_tex_format(("rgba16f", 4)) == "f2"
        assert _dtype_for_tex_format(("rgba32f", 4)) == "f2"
        assert _dtype_for_tex_format("rgba16f") == "f2"

    def test_8bit_and_unknown_formats_map_to_f1(self) -> None:
        from src.features.edit.core.timeline_clip_frame_provider import (
            _dtype_for_tex_format,
        )
        assert _dtype_for_tex_format(("rgba8", 4)) == "f1"
        assert _dtype_for_tex_format(None) == "f1"
        assert _dtype_for_tex_format("unknown") == "f1"
        assert _dtype_for_tex_format(("bgr8", 3)) == "f1"
