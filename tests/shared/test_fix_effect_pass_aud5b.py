"""Regression tests for batch A critical fixes (w5b-g1, shared/effects).

Covers two Critical GL feedback-loop findings from the batch-264 audit,
both in ``src/features/shared/effects/effect_pass_runner.py``:

  - **AUD-26401** — full-res ``_run_separable`` wrote H→scratch_b /
    V→scratch_a unconditionally. In a chained run whose input WAS
    scratch_a (e.g. [sharpen → blur]) the V pass wrote scratch_a while
    sampling it as ``u_original`` (unit 1); with an input of scratch_b
    (e.g. [sharpen → denoise → blur]) the H pass wrote scratch_b while
    sampling it as ``u_texture`` (unit 0). Rendering into a
    simultaneously-sampled attachment is undefined behaviour — the project
    itself documents the symptom as a black / corrupted frame.
  - **AUD-26402** — ``_run_glow`` pass 1 always wrote scratch_b, so a
    chained input of scratch_b (e.g. [single-pass → single-pass → glow])
    made pass 1 read+write scratch_b: the same feedback class FX-M02 had
    fixed for pass 2 only.

Strategy (extends the FX-M02 pattern from
``tests/edit/test_effect_pass_runner_glow_feedback.py``):

1. **Selection-contract tests (deterministic, no GL).** A mocked runner
   records every FBO bind, texture bind and draw; a checker then enforces
   the GL invariant directly: *at every draw, the bound framebuffer's
   texture must not be bound to any texture unit* (bindings persist until
   rebound, so the map is cumulative — conservative but false-positive
   free for these code paths, which rebind every unit they sample). The
   exact routing table and output glo are pinned per input alias.
2. **Real-GL chain tests (skip without GL).** End-to-end chains that
   reproduce the audited scenarios and assert byte-exact identity where
   the shader semantics make the expected output knowable: blur with
   ``mix=0`` passes ``u_original`` through untouched; glow with
   ``threshold=2.0`` (above any f1 luminance) composites a zero bloom.
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.shared.effects.effect_pass_runner import EffectPassRunner  # noqa: E402

pytestmark = [pytest.mark.regression, pytest.mark.critical]


# ─────────────────────────────────────────────────────────────────────────────
# Mocked-runner harness: records every GL-side decision as events.
# ─────────────────────────────────────────────────────────────────────────────

# Distinct glo ids per scratch so alias checks are unambiguous.
_GLOS: Dict[str, int] = {"a": 101, "b": 102, "c": 103}
_NAMES: Dict[int, str] = {glo: name for name, glo in _GLOS.items()}
_EXTERNAL_GLO = 999  # a texture the runner does not own

# event tuples:
#   ("write", scratch_name)          — an FBO became the render target
#   ("bind_tex", scratch_name, unit) — a runner scratch bound via .use(unit)
#   ("bind_ext", glo, unit)          — an external/input glo bound
#   ("draw", None)                   — a fullscreen quad was rendered
Event = Tuple[Any, ...]

_SEPARABLE_PACKED: Dict[str, Any] = {
    "u_radius_x": 4.0, "u_radius_y": 4.0,
    "u_kernel_mode": 0, "u_edge_policy": 0,
    "u_channel_mask": (1.0, 1.0, 1.0, 0.0), "u_mix": 1.0,
}

_GLOW_PACKED: Dict[str, Any] = {
    "u_threshold": 0.7, "u_radius": 8.0, "u_intensity": 1.5,
    "u_tint": (1.0, 0.85, 0.7), "u_mix": 1.0, "u_sample_count": 16,
}


def _recording_runner(mode: str) -> Tuple[EffectPassRunner, List[Event]]:
    """Build a bare ``EffectPassRunner`` that records GL-side decisions.

    ``mode="separable"`` packs blur-style uniforms (including the
    ``u_radius_x``/``u_radius_y`` keys ``_run_separable`` pops);
    ``mode="glow"`` packs glow-style uniforms. All GL-touching helpers are
    mocked to append events instead of touching a context.
    """
    runner = EffectPassRunner.__new__(EffectPassRunner)
    events: List[Event] = []

    for name, glo in _GLOS.items():
        tex = MagicMock(name=f"scratch_{name}")
        tex.glo = glo
        tex.use.side_effect = functools.partial(
            lambda n, unit=0: events.append(("bind_tex", n, unit)), name)
        setattr(runner, f"_scratch_{name}", tex)

        fbo = MagicMock(name=f"scratch_{name}_fbo")
        fbo.use.side_effect = functools.partial(
            lambda n: events.append(("write", n)), name)
        setattr(runner, f"_scratch_{name}_fbo", fbo)

    runner._ctx = MagicMock()
    runner._get_program = MagicMock(return_value=(MagicMock(), MagicMock()))
    runner._pack_params = MagicMock(
        return_value=dict(_SEPARABLE_PACKED if mode == "separable" else _GLOW_PACKED))
    runner._bind_external_texture = MagicMock(
        side_effect=lambda glo, unit=0: events.append(("bind_ext", glo, unit)))
    runner._set_uniform = MagicMock()
    runner._render_quad = MagicMock(
        side_effect=lambda program: events.append(("draw",)))
    runner._ensure_scratch = MagicMock()
    return runner, events


def _spec() -> MagicMock:
    spec = MagicMock(name="spec")
    spec.structural_param_names = ()
    return spec


def _run_separable_with_input(input_glo: int) -> Tuple[EffectPassRunner, List[Event]]:
    runner, events = _recording_runner("separable")
    runner._run_separable(_spec(), input_glo, {}, 8, 8)
    return runner, events


def _run_glow_with_input(input_glo: int) -> Tuple[EffectPassRunner, List[Event]]:
    runner, events = _recording_runner("glow")
    runner._run_glow(_spec(), input_glo, {}, 8, 8)
    return runner, events


def _write_order(events: List[Event]) -> List[str]:
    """Scratch names whose FBOs were bound, in order."""
    return [ev[1] for ev in events if ev[0] == "write"]


def _output_glo(runner: EffectPassRunner) -> int:
    """The glo of ``_last_output_tex`` (what run() would return)."""
    tex = runner._last_output_tex
    assert tex is not None, "effect declined — no output was produced"
    return tex.glo


def _assert_no_read_write_alias(events: List[Event]) -> None:
    """The GL feedback invariant, evaluated over the recorded events.

    GL texture-unit bindings persist until rebound, so maintain a cumulative
    unit→texture map; at every draw the current framebuffer's texture must
    not appear in any bound unit. These code paths rebind every unit they
    sample before drawing, so the conservative map has no false positives —
    and it is exactly the aliasing the audited defects introduced.
    """
    unit_map: Dict[int, str] = {}
    attachment: Optional[str] = None
    for ev in events:
        kind = ev[0]
        if kind == "write":
            attachment = ev[1]
        elif kind == "bind_tex":
            unit_map[ev[2]] = ev[1]
        elif kind == "bind_ext":
            unit_map[ev[2]] = _NAMES.get(ev[1], "external")
        elif kind == "draw":
            assert attachment is not None, "draw without a bound framebuffer"
            assert attachment not in set(unit_map.values()), (
                f"GL feedback loop: framebuffer '{attachment}' is also bound "
                f"for sampling at draw time (units={unit_map})"
            )


# ─────────────────────────────────────────────────────────────────────────────
# AUD-26401: _run_separable target selection.
# ─────────────────────────────────────────────────────────────────────────────

class TestSeparableTargetSelection:
    def test_external_input_keeps_canonical_routing(self) -> None:
        """Common path (external texture): H→scratch_b, V→scratch_a — the
        historical routing, unchanged so golden-frame parity is unaffected."""
        runner, events = _run_separable_with_input(_EXTERNAL_GLO)
        assert _write_order(events) == ["b", "a"]
        assert _output_glo(runner) == _GLOS["a"]

    def test_input_is_scratch_a_routes_v_pass_to_scratch_c(self) -> None:
        """AUD-26401 case 1: input==scratch_a (e.g. [sharpen → blur]) — the
        V pass must NOT write scratch_a (it samples scratch_a on unit 1 as
        u_original). It writes scratch_c instead."""
        runner, events = _run_separable_with_input(_GLOS["a"])
        assert _write_order(events) == ["b", "c"]
        assert _output_glo(runner) == _GLOS["c"]

    def test_input_is_scratch_b_routes_h_pass_to_scratch_a(self) -> None:
        """AUD-26401 case 2: input==scratch_b (e.g. [sharpen → denoise →
        blur]) — the H pass must NOT write scratch_b (it samples scratch_b
        on unit 0). It writes scratch_a; the V pass then writes scratch_c."""
        runner, events = _run_separable_with_input(_GLOS["b"])
        assert _write_order(events) == ["a", "c"]
        assert _output_glo(runner) == _GLOS["c"]

    def test_input_is_scratch_c_keeps_canonical_routing(self) -> None:
        """A previous effect's scratch_c output aliases neither b (H) nor a
        (V), so the canonical routing applies."""
        runner, events = _run_separable_with_input(_GLOS["c"])
        assert _write_order(events) == ["b", "a"]
        assert _output_glo(runner) == _GLOS["a"]

    def test_v_pass_samples_h_output_and_pristine_input(self) -> None:
        """The mask/mix contract is preserved by the rerouting: the V pass
        samples the H target on unit 0 and the pristine input on unit 1."""
        runner, events = _run_separable_with_input(_GLOS["a"])
        ext_binds = [(ev[1], ev[2]) for ev in events if ev[0] == "bind_ext"]
        assert (_GLOS["a"], 0) in ext_binds, "H pass must sample the input on unit 0"
        assert (_GLOS["a"], 1) in ext_binds, "V pass must keep u_original = pristine input (unit 1)"
        assert ("bind_tex", "b", 0) in events, "V pass u_texture must be the H output"

    @pytest.mark.parametrize("input_glo", [_GLOS["a"], _GLOS["b"], _GLOS["c"], _EXTERNAL_GLO],
                             ids=["scratch_a", "scratch_b", "scratch_c", "external"])
    def test_no_read_write_alias_for_any_input(self, input_glo: int) -> None:
        """The core invariant: no pass may read and write the same texture,
        whatever scratch the chained input happens to be."""
        runner, events = _run_separable_with_input(input_glo)
        _assert_no_read_write_alias(events)
        assert _output_glo(runner) != input_glo


# ─────────────────────────────────────────────────────────────────────────────
# AUD-26402: _run_glow pass-1 (bloom) target selection.
# ─────────────────────────────────────────────────────────────────────────────

class TestGlowTargetSelection:
    def test_external_input_keeps_canonical_routing(self) -> None:
        """Common path (external texture): bloom→scratch_b, output→scratch_a
        — the FX-M02 routing, unchanged."""
        runner, events = _run_glow_with_input(_EXTERNAL_GLO)
        assert _write_order(events) == ["b", "a"]
        assert _output_glo(runner) == _GLOS["a"]

    def test_input_is_scratch_a_routes_pass2_to_scratch_c(self) -> None:
        """FX-M02 regression guard (unchanged by this fix): input aliasing
        scratch_a → pass 2 writes scratch_c; pass 1 still writes scratch_b."""
        runner, events = _run_glow_with_input(_GLOS["a"])
        assert _write_order(events) == ["b", "c"]
        assert _output_glo(runner) == _GLOS["c"]

    def test_input_is_scratch_b_pass1_avoids_scratch_b(self) -> None:
        """AUD-26402: input==scratch_b (e.g. [single-pass → single-pass →
        glow], the second single-pass wrote scratch_b) — pass 1 must NOT
        write scratch_b (it samples scratch_b on unit 0). It writes
        scratch_a as the bloom; pass 2 writes scratch_c; the bloom bound on
        unit 1 is scratch_a."""
        runner, events = _run_glow_with_input(_GLOS["b"])
        assert _write_order(events) == ["a", "c"]
        assert _output_glo(runner) == _GLOS["c"]
        # Pass 2 samples {input (unit 0), bloom (unit 1)}; the bloom must be
        # the pass-1 target (scratch_a), never the input.
        assert ("bind_tex", "a", 1) in events, (
            "pass 2's u_bloom_tex (unit 1) must be the pass-1 target scratch_a"
        )
        assert ("bind_tex", "b", 1) not in events, (
            "pass 2 must not sample scratch_b — that is the chained input"
        )

    def test_input_is_scratch_c_keeps_canonical_routing(self) -> None:
        runner, events = _run_glow_with_input(_GLOS["c"])
        assert _write_order(events) == ["b", "a"]
        assert _output_glo(runner) == _GLOS["a"]

    @pytest.mark.parametrize("input_glo", [_GLOS["a"], _GLOS["b"], _GLOS["c"], _EXTERNAL_GLO],
                             ids=["scratch_a", "scratch_b", "scratch_c", "external"])
    def test_no_read_write_alias_for_any_input(self, input_glo: int) -> None:
        """Invariant for BOTH glow passes: the pass-1 target must differ
        from the input, and the pass-2 output must differ from {input,
        pass-1 target}."""
        runner, events = _run_glow_with_input(input_glo)
        _assert_no_read_write_alias(events)
        assert _output_glo(runner) != input_glo


# ─────────────────────────────────────────────────────────────────────────────
# Real-GL end-to-end chains (skipped when no GL is available).
# ─────────────────────────────────────────────────────────────────────────────

W, H = 48, 32


@pytest.fixture
def gl_context():
    """Standalone ModernGL context (mirrors tests/edit/conftest.py: skip,
    never silently pass, when GL is unavailable)."""
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # noqa: BLE001 — environment, not code
        pytest.skip(f"OpenGL context unavailable: {exc}")
    try:
        yield ctx
    finally:
        try:
            ctx.release()
        except Exception:  # noqa: BLE001
            pass


def _readback(runner: EffectPassRunner, ctx: Any, glo: int, w: int, h: int) -> np.ndarray:
    """Read the runner scratch owning ``glo`` back as a uint8 RGBA array."""
    for name in ("_scratch_a", "_scratch_b", "_scratch_c"):
        tex = getattr(runner, name, None)
        if tex is not None and tex.glo == glo:
            fbo = getattr(runner, name + "_fbo")
            fbo.use()
            ctx.viewport = (0, 0, w, h)
            raw = fbo.read(components=4)
            return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
    raise AssertionError(f"output glo {glo} is not a runner scratch — effect was a no-op")


def _make_input(ctx: Any, w: int, h: int, seed: int = 7) -> Any:
    rng = np.random.default_rng(seed)
    rgba = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
    tex = ctx.texture((w, h), components=4, dtype="f1")
    tex.write(rgba.tobytes())
    return tex


@pytest.mark.gpu_renderer
class TestRealChainNoFeedback:
    """跑真实 GL 效果链，复现审计场景并断言逐字节语义。"""

    def test_chain_sharpen_then_blur_mix0_is_identity(self, gl_context) -> None:
        """AUD-26401 场景 A：[sharpen → blur(mix=0)]。

        sharpen(外部输入) → scratch_a；blur 的输入即 scratch_a。mix=0 时
        V shader 输出 = u_original 直通 → blur 输出必须逐字节等于其输入。
        旧代码 V pass 写 scratch_a 的同时经 unit 1 采样 scratch_a（反馈
        回路），u_original 读到 clear 后的 0 → 输出被拉黑。
        """
        from src.features.shared.effects import get_effect_catalog
        catalog = get_effect_catalog()
        runner = EffectPassRunner(gl_context)
        try:
            in_tex = _make_input(gl_context, W, H)
            out1 = runner.run(
                catalog.get_spec("sharpen"), in_tex.glo,
                {"amount": 0.8, "radius": 1.0}, W, H)
            assert out1 == runner._scratch_a.glo  # canonical single-pass routing
            out2 = runner.run(
                catalog.get_spec("blur"), out1,
                {"blur_type": "gaussian", "radius": 4.0, "mix": 0.0}, W, H)
            assert out2 != out1, "blur was a silent no-op"
            assert out2 == runner._scratch_c.glo, (
                "input aliasing scratch_a must route the V pass to scratch_c"
            )
            np.testing.assert_array_equal(
                _readback(runner, gl_context, out2, W, H),
                _readback(runner, gl_context, out1, W, H),
                err_msg="blur(mix=0) must pass its input through byte-exactly; "
                        "a mismatch means u_original sampled the pass target "
                        "(feedback loop)",
            )
        finally:
            runner.release()

    def test_chain_sharpen_denoise_then_blur_mix0_is_identity(self, gl_context) -> None:
        """AUD-26401 场景 B：[sharpen → denoise → blur(mix=0)]。

        sharpen → scratch_a；denoise(输入 scratch_a) → scratch_b；blur 的
        输入即 scratch_b。旧代码 H pass 写 scratch_b 的同时采样 scratch_b
        （反馈回路）→ H 输出未定义。修复后 H 写 scratch_a、V 写 scratch_c，
        blur 输出必须逐字节等于 denoise 的输出。
        """
        from src.features.shared.effects import get_effect_catalog
        catalog = get_effect_catalog()
        runner = EffectPassRunner(gl_context)
        try:
            in_tex = _make_input(gl_context, W, H)
            out1 = runner.run(
                catalog.get_spec("sharpen"), in_tex.glo,
                {"amount": 0.8, "radius": 1.0}, W, H)
            out2 = runner.run(
                catalog.get_spec("denoise"), out1,
                {"strength": 0.6}, W, H)
            assert out2 == runner._scratch_b.glo  # single-pass ping-pong
            out3 = runner.run(
                catalog.get_spec("blur"), out2,
                {"blur_type": "gaussian", "radius": 4.0, "mix": 0.0}, W, H)
            assert out3 != out2, "blur was a silent no-op"
            assert out3 == runner._scratch_c.glo, (
                "input aliasing scratch_b must route H→scratch_a, V→scratch_c"
            )
            np.testing.assert_array_equal(
                _readback(runner, gl_context, out3, W, H),
                _readback(runner, gl_context, out2, W, H),
                err_msg="blur(mix=0) must pass its input through byte-exactly; "
                        "a mismatch means the H pass sampled its own target "
                        "(feedback loop)",
            )
        finally:
            runner.release()

    def test_chain_sharpen_denoise_then_glow_zero_bloom_is_identity(self, gl_context) -> None:
        """AUD-26402 场景：[sharpen → denoise → glow(threshold=2.0)]。

        sharpen → scratch_a；denoise → scratch_b；glow 的输入即 scratch_b。
        threshold=2.0 高于任何 f1 亮度 → bloom 恒 0 → glow 输出 = 原图直通
        （逐字节）。旧代码 pass 1 写 scratch_b 的同时采样 scratch_b（反馈
        回路），bloom 缓冲未定义。修复后 bloom 写 scratch_a、输出 scratch_c。
        """
        from src.features.shared.effects import get_effect_catalog
        catalog = get_effect_catalog()
        runner = EffectPassRunner(gl_context)
        try:
            in_tex = _make_input(gl_context, W, H)
            out1 = runner.run(
                catalog.get_spec("sharpen"), in_tex.glo,
                {"amount": 0.8, "radius": 1.0}, W, H)
            out2 = runner.run(
                catalog.get_spec("denoise"), out1,
                {"strength": 0.6}, W, H)
            assert out2 == runner._scratch_b.glo
            out3 = runner.run(
                catalog.get_spec("glow"), out2,
                {"threshold": 2.0, "radius": 6.0, "intensity": 1.5, "mix": 1.0},
                W, H)
            assert out3 != out2, "glow was a silent no-op"
            assert out3 == runner._scratch_c.glo, (
                "input aliasing scratch_b must route bloom→scratch_a, out→scratch_c"
            )
            np.testing.assert_array_equal(
                _readback(runner, gl_context, out3, W, H),
                _readback(runner, gl_context, out2, W, H),
                err_msg="glow(threshold=2.0) composites a zero bloom and must "
                        "pass its input through byte-exactly; a mismatch means "
                        "the bloom pass sampled its own target (feedback loop)",
            )
        finally:
            runner.release()
