"""FX-M02 regression: glow feedback loop (read/write same texture).

Audit FX-M02: glow's pass 2 composites the original input (texture unit
0) over the bloom (scratch_b, unit 1) and writes the result to a scratch.
When the input IS scratch_a (glow-on-glow, or glow after any effect whose
output is scratch_a), pass 2 reads scratch_a on unit 0 AND writes scratch_a
— a GL feedback loop that yields a black/corrupted frame. The audit's
_known limitation_ is now resolved with a third scratch (scratch_c) used as
the pass-2 output whenever the input aliases scratch_a.

Real GL validation needs the offscreen fixture the audit (§7) excludes
from P0 scope. These tests instead pin the OUTPUT-SELECTION contract that
breaks the feedback: which scratch surface pass 2 writes to, as a function
of the input glo. The selection is pure logic over glo ids, so a mock
context is sufficient and deterministic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.features.shared.effects.effect_pass_runner import EffectPassRunner

pytestmark = pytest.mark.edit_core


def _make_runner_with_scratch() -> EffectPassRunner:
    """Build a runner whose three scratch surfaces have distinct glo ids.

    We bypass __init__ (which needs a real moderngl context) and stub just
    enough for _run_glow's output selection: three scratch textures each
    with a unique .glo, and matching FBOs. The GL draw calls are no-ops
    (mocked), so we observe only the OUTPUT decision.
    """
    runner = EffectPassRunner.__new__(EffectPassRunner)

    def tex(glo: int):
        t = MagicMock()
        t.glo = glo
        return t

    # Distinct glo ids so the input-alias check is unambiguous.
    runner._scratch_a = tex(101)
    runner._scratch_b = tex(102)
    runner._scratch_c = tex(103)
    runner._scratch_a_fbo = MagicMock()
    runner._scratch_b_fbo = MagicMock()
    runner._scratch_c_fbo = MagicMock()
    # FX-M01: the scratch cache key is now (width, height, dtype); the value
    # itself is unused by this test (we only assert the OUTPUT glo choice),
    # but the attribute must exist so _ensure_scratch (if reached) doesn't
    # AttributeError. Kept as a 3-tuple to match the renamed _scratch_key.
    runner._scratch_key = (8, 8, "f1")

    # Stub the GL-touching helpers so _run_glow runs without a real context.
    runner._ctx = MagicMock()
    runner._get_program = MagicMock(return_value=(MagicMock(), MagicMock()))
    runner._pack_params = MagicMock(return_value={
        "u_radius": 4.0, "u_intensity": 1.0, "u_mix": 1.0,
        "u_threshold": 0.5, "u_sample_count": 8, "u_tint": (1.0, 0.8, 0.6),
    })
    runner._bind_external_texture = MagicMock()
    runner._set_uniform = MagicMock()
    runner._render_quad = MagicMock()
    runner._ensure_scratch = MagicMock()  # already set up above
    return runner


def _stub_gl():
    """Patch QOpenGLFunctions so pass 2's manual texture bind is a no-op.

    _run_glow imports QOpenGLFunctions_4_1_Core inside the function and
    calls initializeOpenGLFunctions(); without a real GL context that
    segfaults. We replace the class so the import resolves to a mock that
    reports init failure (the code then skips the GL bind gracefully).
    """
    fake_gl = MagicMock()
    fake_gl.initializeOpenGLFunctions.return_value = False
    fake_module = MagicMock()
    fake_module.QOpenGLFunctions_4_1_Core.return_value = fake_gl
    return patch("PyQt6.QtOpenGL.QOpenGLFunctions_4_1_Core", fake_module.QOpenGLFunctions_4_1_Core)


def _run_glow_output_for_input(input_glo: int) -> int:
    """Run glow with the given input glo and return the OUTPUT glo chosen."""
    runner = _make_runner_with_scratch()
    spec = MagicMock()
    with _stub_gl():
        out = runner._run_glow(spec, input_glo, {}, 8, 8)
    return out


class TestGlowFeedbackSafeOutput:
    def test_non_alias_input_writes_scratch_a(self) -> None:
        """Common path: input is an external texture (not scratch_a) →
        pass 2 writes scratch_a. No feedback, no extra surface needed."""
        out = _run_glow_output_for_input(input_glo=999)  # external
        assert out == 101, "non-alias input should write scratch_a (glo 101)"

    def test_input_is_scratch_a_writes_scratch_c(self) -> None:
        """FX-M02 fix: when the input IS scratch_a (glow-on-glow / glow after
        a scratch_a-output effect), pass 2 must NOT write scratch_a (that
        would read+write the same texture → black frame). It writes scratch_c
        instead, which is neither the input nor the bloom (scratch_b)."""
        out = _run_glow_output_for_input(input_glo=101)  # == scratch_a.glo
        assert out == 103, (
            "input aliasing scratch_a must route output to scratch_c (glo 103) "
            "to avoid the read/write feedback loop"
        )

    def test_output_never_equals_input(self) -> None:
        """The core invariant: glow's output glo must never equal its input
        glo, regardless of which scratch the input is. This is what makes
        glow chainable after itself or after any scratch-output effect."""
        for input_glo in (101, 102, 103, 999):
            out = _run_glow_output_for_input(input_glo)
            assert out != input_glo, (
                f"feedback invariant broken: output {out} == input {input_glo}"
            )
