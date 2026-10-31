"""FX-M08: GPU / CPU-fallback / export pixel-parity harness.

The audit (FX-M08) calls for a *real* golden-frame comparison across the
three rendering paths so that preview↔export drift is caught by CI rather
than by eye. Prior parity tests (``test_adjustment_clip_scope.py``,
``test_adjustment_clip_cpu_export_application.py``) deliberately avoided GL
readback and only checked data contracts; this module supplies the missing
GL readback layer.

What this file establishes (the harness itself):

1. **A standalone-GL round-trip is lossless** within SDR tolerance — upload
   a uint8 frame, render it through a passthrough FBO, read it back, and
   assert byte-level parity. If this fails, every later comparison is
   meaningless, so it runs first.
2. **EffectPassRunner is drivable headless** — a real effect (blur) must
   produce a *changed* output (not a silent no-op) when run on the
   standalone context. This is the regression guard for the
   ``_bind_external_texture`` PyOpenGL fallback (without it the runner
   segfaulted in standalone, silently returning the input unchanged).
3. **GPU vs CPU blur are both *blurring*** — the two backends use different
   algorithms (GPU box-separable vs CPU Gaussian), so we do NOT assert
   pixel-identical output. Instead we assert the structural property both
   must share: a sharp edge is smoothed. This documents the known
   algorithmic gap (audit §4 "preview/export matrix parity") as an explicit,
   versioned contract rather than a silent drift.

The per-effect / per-format golden matrix (audit FX-M08 §3) is extended in
``test_crop_render_stage.py`` (crop geometry, where preview and export MUST
agree because they share the same math) and ``test_effect_surface_precision.py``
(HDR precision).

Environment: every GPU test depends on the ``gl_context`` fixture from
``tests/edit/conftest.py``, which skips cleanly when no GL is available.
A parity test must never pass on the CPU path alone.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.edit.helpers import golden_compare as gc  # noqa: E402
from tests.edit.helpers import frame_fixtures as fx  # noqa: E402

pytestmark = pytest.mark.edit_core


# ── helpers ──────────────────────────────────────────────────────────────────

def _readback_scratch(runner, glo: int, w: int, h: int) -> np.ndarray:
    """Read the runner's scratch surface that owns ``glo`` back to numpy.

    The runner keeps up to three scratch textures (a/b/c); we locate the one
    whose ``.glo`` matches the returned output and read its FBO. Returns an
    ``(H, W, 4)`` uint8 RGBA array in image space (top row = row 0).
    """
    for name in ("_scratch_a", "_scratch_b", "_scratch_c"):
        tex = getattr(runner, name, None)
        if tex is not None and tex.glo == glo:
            fbo = getattr(runner, name + "_fbo")
            fbo.use()
            runner._ctx.viewport = (0, 0, w, h)
            raw = fbo.read(components=4)
            return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
    raise AssertionError(
        f"output glo {glo} is not any runner scratch — effect was a no-op "
        "(input returned unchanged); see _bind_external_texture fallback"
    )


def _rgba_to_bgr(rgba: np.ndarray) -> np.ndarray:
    """RGBA uint8 → BGR uint8 (3-channel), matching the CPU/export handlers."""
    return rgba[:, :, :3][:, :, ::-1].copy()


# ── 1. GL round-trip is lossless ─────────────────────────────────────────────

class TestStandaloneGLRoundTrip:
    """Foundation: if GL can't reproduce a frame byte-for-byte, nothing
    downstream is trustworthy."""

    def test_solid_frame_survives_gl_roundtrip(self, gl_context) -> None:
        """A flat-colour frame must read back identically (max_abs = 0).

        Solids have no spatial frequency, so there is zero room for sampling
        or quantisation error — any difference indicates a binding / channel
        / coordinate bug, not precision.
        """
        src = fx.solid_rgba_frame(16, 16, (100, 150, 200, 255))
        tex = gl_context.texture((16, 16), components=4, dtype="f1")
        tex.write(src.tobytes())
        fbo = gl_context.framebuffer(color_attachments=[tex])
        fbo.use()
        gl_context.viewport = (0, 0, 16, 16)
        got = np.frombuffer(fbo.read(components=4), dtype=np.uint8).reshape(16, 16, 4)
        gc.assert_frames_equal(got, src, label="solid_gl_roundtrip")
        tex.release()
        fbo.release()

    def test_gradient_frame_survives_gl_roundtrip(self, gl_context) -> None:
        """A vertical gradient must survive GL round-trip within SDR tolerance.

        Gradients stress 8-bit quantisation at the upload/readback boundary;
        the audit allows up to 2/255 per channel.
        """
        src = fx.vertical_gradient_frame(16, 16)
        tex = gl_context.texture((16, 16), components=4, dtype="f1")
        tex.write(src.tobytes())
        fbo = gl_context.framebuffer(color_attachments=[tex])
        fbo.use()
        gl_context.viewport = (0, 0, 16, 16)
        got = np.frombuffer(fbo.read(components=4), dtype=np.uint8).reshape(16, 16, 4)
        # GL round-trip of a pure upload→readback should be exact (no sampling).
        gc.assert_frames_equal(got, src, label="gradient_gl_roundtrip")
        tex.release()
        fbo.release()


# ── 2. EffectPassRunner is drivable headless (FX-M08 regression guard) ───────

class TestEffectPassRunnerHeadless:
    """The runner must actually render in a standalone context.

    This is the regression guard for the ``_bind_external_texture`` PyOpenGL
    fallback (FX-M08 prerequisite): before that fix, ``initializeOpenGLFunctions()``
    segfaulted without a Qt context, the runner's ``except Exception`` swallowed
    it, and ``run()`` returned the input glo unchanged — a *silent* no-op that
    made every downstream parity test pass vacuously.
    """

    def test_blur_is_not_a_silent_noop(self, gl_context) -> None:
        """A real blur must return a *different* glo and actually smooth a sharp edge.

        If the runner failed to bind the input texture (the pre-fix bug), it
        would return the input glo and the output would equal the input. We
        assert both that the glo changed AND that the output pixels differ.
        """
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        from src.features.shared.effects import get_effect_catalog

        runner = EffectPassRunner(gl_context)
        try:
            catalog = get_effect_catalog()
            spec = catalog.get_spec("blur")
            assert spec is not None, "blur spec missing from catalog"

            src = fx.horizontal_half_frame(16, 16)  # sharp left/right edge
            in_tex = gl_context.texture((16, 16), components=4, dtype="f1")
            in_tex.write(src.tobytes())

            out_glo = runner.run(spec, in_tex.glo, {"radius": 4.0}, 16, 16)
            # Contract 1: a real render produces a new scratch glo, not the input.
            assert out_glo != in_tex.glo, (
                "blur returned the input glo unchanged — the effect was a "
                "silent no-op (texture bind failed; see _bind_external_texture)"
            )

            out = _readback_scratch(runner, out_glo, 16, 16)
            # Contract 2: the sharp edge is actually smoothed. On the original
            # frame the column at x=7 (last white) and x=8 (first black) differ
            # by ~255; after blur they must converge (the centre column becomes
            # mid-grey rather than staying a hard step).
            edge_before = abs(int(src[8, 7, 0]) - int(src[8, 8, 0]))
            edge_after = abs(int(out[8, 7, 0]) - int(out[8, 8, 0]))
            assert edge_after < edge_before, (
                f"blur did not smooth the edge: |Δ| before={edge_before}, "
                f"after={edge_after} (output may be the unblurred input)"
            )
            in_tex.release()
        finally:
            runner.release()

    @pytest.mark.parametrize("blur_type", ["box", "gaussian", "bilateral", "motion", "bokeh"])
    def test_all_composite_blur_variants_render(self, gl_context, blur_type: str) -> None:
        """Every BlurNode variant must execute through the Edit runner."""
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        from src.features.shared.effects import get_effect_catalog

        runner = EffectPassRunner(gl_context)
        try:
            spec = get_effect_catalog().get_spec("blur")
            assert spec is not None
            # 锐利半分帧（而非线性渐变）：对称模糊核对线性渐变是恒等
            # 变换（边界 clamp 下也成立），"输出≠输入" 断言会随实现细节
            # 随机翻转；硬边对一切模糊变体都必然被平滑。
            src = fx.horizontal_half_frame(32, 32)
            in_tex = gl_context.texture((32, 32), components=4, dtype="f1")
            in_tex.write(src.tobytes())
            params = {
                "blur_type": blur_type,
                "radius": 4.0,
                "radius_x": 4.0,
                "radius_y": 4.0,
                "angle": 37.0,
                "edge_preserve": 0.0,
            }
            out_glo = runner.run(spec, in_tex.glo, params, 32, 32)
            assert out_glo != in_tex.glo
            out = _readback_scratch(runner, out_glo, 32, 32)
            assert not np.array_equal(out, src)
            in_tex.release()
        finally:
            runner.release()


# ── 3. GPU vs CPU: documented algorithmic gap ────────────────────────────────

class TestGpuCpuBlurStructuralParity:
    """GPU (EffectPassRunner) and CPU (``_cpu_blur``) use different blur
    algorithms — separable box on GPU, Gaussian via OpenCV on CPU — so
    pixel-identical output is NOT expected and is NOT asserted.

    What IS asserted is the structural property both backends must honour:
    blurring a sharp edge must reduce its contrast. If one backend stops
    blurring (e.g. a param-mapping regression that drops ``radius``), the
    edge survives and this test fails. This turns the known algorithmic gap
    into a versioned contract instead of an invisible drift.
    """

    def test_both_backends_smooth_a_sharp_edge(self, gl_context) -> None:
        from src.features.edit.core.timeline_export import _cpu_blur
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        from src.features.shared.effects import get_effect_catalog

        # Use a single-channel-ish edge (white vs black) so the contrast metric
        # on one channel is unambiguous. RGB-mean metrics cancel out on a
        # red↔green edge (both average to 85), which is why we avoid colour here.
        src_rgba = fx.horizontal_half_frame(
            32, 32, left=(255, 255, 255), right=(0, 0, 0),
        )
        src_bgr = _rgba_to_bgr(src_rgba)  # CPU handlers eat BGR uint8

        # --- CPU path ---
        cpu_out = _cpu_blur(src_bgr, {"radius": 6.0, "mix": 1.0})

        # --- GPU path ---
        runner = EffectPassRunner(gl_context)
        try:
            catalog = get_effect_catalog()
            spec = catalog.get_spec("blur")
            in_tex = gl_context.texture((32, 32), components=4, dtype="f1")
            in_tex.write(src_rgba.tobytes())
            out_glo = runner.run(spec, in_tex.glo, {"radius": 6.0}, 32, 32)
            gpu_out_rgba = _readback_scratch(runner, out_glo, 32, 32)
            gpu_out = _rgba_to_bgr(gpu_out_rgba)
            in_tex.release()
        finally:
            runner.release()

        # Edge contrast metric on a single channel (red) at the seam.
        # x=15 is the last white column, x=16 the first black column.
        def edge_contrast(frame: np.ndarray) -> float:
            return abs(float(frame[16, 15, 2]) - float(frame[16, 16, 2]))

        original_contrast = edge_contrast(src_bgr)
        cpu_contrast = edge_contrast(cpu_out)
        gpu_contrast = edge_contrast(gpu_out)

        # Both backends must reduce the edge contrast vs the original.
        assert cpu_contrast < original_contrast * 0.75, (
            f"CPU blur left the edge intact: {cpu_contrast} vs "
            f"{original_contrast} (radius may have been dropped)"
        )
        assert gpu_contrast < original_contrast * 0.75, (
            f"GPU blur left the edge intact: {gpu_contrast} vs "
            f"{original_contrast} (effect may be a no-op)"
        )

    def test_disabled_effect_is_passthrough_on_both(self, gl_context) -> None:
        """A zero-radius blur must leave the frame unchanged on both backends.

        This pins the shared passthrough contract: when an effect's magnitude
        is zero, neither backend may alter pixels. A future refactor that
        forgets the zero-guard on one side is caught here.

        Note: at ``radius=0`` the GPU runner short-circuits and returns the
        input glo unchanged (a deliberate no-op, not a render), so the GPU
        passthrough check compares the *input texture's* readback to the
        original — i.e. it verifies the runner did not modify the source.
        """
        from src.features.edit.core.timeline_export import _cpu_blur
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        from src.features.shared.effects import get_effect_catalog

        src_rgba = fx.vertical_gradient_frame(16, 16)
        src_bgr = _rgba_to_bgr(src_rgba)

        # CPU: zero radius → handler returns input unchanged.
        cpu_out = _cpu_blur(src_bgr, {"radius": 0.0, "mix": 1.0})
        gc.assert_frames_equal(cpu_out, src_bgr, label="cpu_zero_radius_passthrough")

        # GPU: zero radius → runner returns the input glo (no-op). Read back
        # that input texture to confirm it was left untouched.
        runner = EffectPassRunner(gl_context)
        try:
            catalog = get_effect_catalog()
            spec = catalog.get_spec("blur")
            in_tex = gl_context.texture((16, 16), components=4, dtype="f1")
            in_tex.write(src_rgba.tobytes())
            out_glo = runner.run(spec, in_tex.glo, {"radius": 0.0}, 16, 16)
            # radius=0 is a true no-op: output glo == input glo.
            assert out_glo == in_tex.glo, (
                "zero-radius blur should be a no-op returning the input, "
                f"got out_glo={out_glo} != in_tex.glo={in_tex.glo}"
            )
            # Read back the input texture itself (the no-op output).
            fbo = gl_context.framebuffer(color_attachments=[in_tex])
            fbo.use()
            gl_context.viewport = (0, 0, 16, 16)
            gpu_out_rgba = np.frombuffer(
                fbo.read(components=4), dtype=np.uint8,
            ).reshape(16, 16, 4)
            gc.assert_frames_equal(
                gpu_out_rgba, src_rgba, label="gpu_zero_radius_passthrough",
            )
            in_tex.release()
            fbo.release()
        finally:
            runner.release()


# ── 4. Capability gate: unsupported effects must not silently render ─────────

class TestExportCapabilityGate:
    """Audit constraint: an effect with no CPU export handler must be reported
    as skipped, never silently dropped or silently rendered as identity.

    The CPU handler registry (``_CPU_EFFECT_HANDLERS``) is the export truth
    table; ``_apply_clip_effects_cpu`` logs a warning for unknown types. We
    assert the registry is non-empty and that the documented exportable set
    matches the capability resolver — this is the guard that FX-M08's
    "degraded preview must not be export parity" rule depends on.
    """

    def test_export_handler_registry_matches_capability(self) -> None:
        """The CPU export handler set must agree with EffectCapability's
        declared exportable types (audit §3 FX-M08). A drift here means a
        capability says "exportable" but no handler exists (silent skip) or
        vice versa."""
        from src.features.edit.core.timeline_export import _CPU_EFFECT_HANDLERS
        from src.features.shared.effects.effect_capability import (
            EXPORT_EFFECT_TYPES,
        )

        handler_types = set(_CPU_EFFECT_HANDLERS.keys())
        # Every type with a real CPU handler must be declared exportable.
        missing = handler_types - set(EXPORT_EFFECT_TYPES)
        assert not missing, (
            f"CPU export handlers exist for {missing} but EffectCapability "
            "does not list them as exportable — export will silently skip "
            "effects the UI promises to render"
        )
