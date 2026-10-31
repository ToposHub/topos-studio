"""P2-9/P2-11 deflicker GPU-assisted path — execution-level tests.

The default (luma_only) evaluation must move both frame transfers to
single channel (GPU luma extraction → R32F readback; GPU gain composite)
while keeping the CPU percentile semantics of the legacy chain.  These
tests run the real node against a standalone GL context and compare the
GPU-assisted output with the force-CPU reference chain (env
``TOPOS_DEFLICKER_CPU``), assert the default path never performs the
full-frame RGBA readback, and pin the fallback and warming semantics.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import moderngl

from src.features.composite.nodes import _gpu_readback
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.deflicker_node import DeflickerNode

pytestmark = pytest.mark.composite_core

_H, _W = 24, 32
_WINDOW = 5
_ATOL = 1e-4          # GPU/CPU float path agreement
_HARD_ATOL = 1e-3     # only a semantically wrong path exceeds this


class _RealCtx:
    """Delegating context wrapper carrying a frame number (particle
    precedent).  ``ctx.gl`` resolves to THIS wrapper (not the raw
    ModernGL context) so every ``gl.texture(...)`` creation inside the
    node registers into ``registry`` and stays readable by the test;
    everything else falls through to ModernGL."""

    def __init__(self, mgl, frame=0, registry=None):
        object.__setattr__(self, "_mgl", mgl)
        object.__setattr__(self, "frame_number", int(frame))
        object.__setattr__(self, "_registry", registry)

    def set_frame(self, frame: int) -> None:
        object.__setattr__(self, "frame_number", int(frame))

    @property
    def gl(self):
        return self

    def texture(self, *args, **kwargs):
        tex = self._mgl.texture(*args, **kwargs)
        if self._registry is not None:
            # glo → newest live object: GL name reuse can only shadow a
            # stale entry with the newer texture, never the reverse.
            self._registry[tex.glo] = tex
        return tex

    def __getattr__(self, name):
        return getattr(self._mgl, name)

    def __setattr__(self, name, value):
        setattr(self._mgl, name, value)


class _FakeFB:
    """Minimal FramebufferManager stand-in.

    ``find_texture`` scans ``_active_textures`` (a set of ModernGL
    textures); ``get_fbo`` builds and caches a real FBO per texture and
    registers it so result readbacks can resolve it too.
    """

    def __init__(self, gl):
        self._gl = gl
        self._active_textures = set()
        self._fbos = {}

    def get_fbo(self, tex):
        fbo = self._fbos.get(tex.glo)
        if fbo is None:
            fbo = self._gl.framebuffer(tex)
            self._fbos[tex.glo] = fbo
            self._active_textures.add(tex)
        return fbo


@pytest.fixture(scope="module")
def _gl_available():
    try:
        moderngl.create_standalone_context()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    yield True


@pytest.fixture()
def gl_ctx(_gl_available):
    """Fresh standalone context per test (function scope — releasing one
    standalone context un-grounds the thread's current-context state on
    Windows; a shared context cascades failures)."""
    ctx = moderngl.create_standalone_context()
    yield ctx
    try:
        ctx.release()
    except Exception:
        pass


def _flicker_sequence(frames=_WINDOW + 1, bump_frame=3, bump=1.6):
    """RGB frames with a global brightness spike at ``bump_frame``."""
    rng = np.random.default_rng(7)
    base = rng.random((_H, _W, 3)).astype(np.float32) * 0.5 + 0.15
    seq = []
    for f in range(frames):
        factor = bump if f == bump_frame else 1.0
        seq.append(np.clip(base * factor, 0.0, None))
    return seq


def _to_rgba(rgb):
    return np.concatenate(
        [rgb, np.ones((_H, _W, 1), dtype=np.float32)], axis=-1)


def _upload(gl, fb, rgba):
    """Upload a top-down RGBA f4 array; returns (texture, TextureResult)."""
    h, w = rgba.shape[:2]
    data = np.ascontiguousarray(np.flipud(rgba))
    tex = gl.texture((w, h), 4, data=data.tobytes(), dtype="f4")
    fb._active_textures.add(tex)
    tr = TextureResult(texture_id=tex.glo, width=w, height=h,
                       format="f4", bbox=None,
                       alpha_premultiplied=False,
                       color_space="linear")
    return tex, tr


def _read_result(registry, tex_result):
    """Read a result texture back via the creation registry (test-owned,
    independent of the node's readback helper so read_rgba spies only see
    node-internal calls).  Textures are kept alive for the whole run —
    releasing mid-run lets GL recycle names and hijack glo-based lookups."""
    tex = registry.get(tex_result.texture_id)
    if tex is None:
        raise AssertionError("result texture not in creation registry")
    fbo = tex.ctx.framebuffer(tex)
    raw = fbo.read(components=4, dtype="f4")
    arr = np.frombuffer(raw, dtype=np.float32).reshape(
        int(tex_result.height), int(tex_result.width), 4)
    return np.ascontiguousarray(np.flipud(arr))


def _run_sequence(gl, node, seq):
    """Evaluate every frame through the node; returns top-down RGBA list.

    All textures stay alive until the context dies: releasing mid-run
    lets GL recycle names and hijack glo-based lookups."""
    fb = _FakeFB(gl)
    registry = {}
    wctx = _RealCtx(gl, registry=registry)
    outs = []
    for f, rgb in enumerate(seq):
        tex, tr = _upload(gl, fb, _to_rgba(rgb))
        wctx.set_frame(f)
        result = node._do_evaluate({"image": tr}, wctx, fb, None)
        assert "result" in result and result["result"] is not None
        outs.append(_read_result(registry, result["result"]))
    return outs


class TestGpuCpuEquivalence:
    def test_per_pixel_luma_matches_legacy(self, gl_ctx):
        """GPU-assisted output ≈ force-CPU chain for the default mode."""
        seq = _flicker_sequence()
        gpu_out = _run_sequence(gl_ctx, DeflickerNode("gpu"), seq)

        os.environ["TOPOS_DEFLICKER_CPU"] = "1"
        try:
            cpu_out = _run_sequence(gl_ctx, DeflickerNode("cpu"), seq)
        finally:
            os.environ.pop("TOPOS_DEFLICKER_CPU", None)

        inputs = [_to_rgba(rgb) for rgb in seq]
        # Frame 0's trailing window is just itself → gain exactly 1.0:
        # both paths must reproduce the input bit-exactly.
        np.testing.assert_array_equal(gpu_out[0], inputs[0])
        np.testing.assert_array_equal(cpu_out[0], inputs[0])
        # Corrected frames agree between the two float paths.
        for f in range(1, len(seq)):
            d = float(np.abs(gpu_out[f] - cpu_out[f]).max())
            assert d <= _ATOL, f"frame {f}: max diff {d}"

    def test_global_gain_scalar_path_matches_legacy(self, gl_ctx):
        seq = _flicker_sequence()
        gpu_node = DeflickerNode("gpu")
        gpu_node.parameters["mode"] = "global_gain"
        gpu_out = _run_sequence(gl_ctx, gpu_node, seq)

        os.environ["TOPOS_DEFLICKER_CPU"] = "1"
        try:
            cpu_node = DeflickerNode("cpu")
            cpu_node.parameters["mode"] = "global_gain"
            cpu_out = _run_sequence(gl_ctx, cpu_node, seq)
        finally:
            os.environ.pop("TOPOS_DEFLICKER_CPU", None)

        for f in range(1, len(seq)):
            d = float(np.abs(gpu_out[f] - cpu_out[f]).max())
            assert d <= _ATOL, f"frame {f}: max diff {d}"
        # The bright spike frame is visibly pulled back.
        inputs = [_to_rgba(rgb) for rgb in seq]
        assert float(np.abs(gpu_out[3] - inputs[3]).max()) > 0.01

    def test_flicker_actually_corrected(self, gl_ctx):
        """Sanity: the corrected bright frame differs from passthrough."""
        seq = _flicker_sequence()
        node = DeflickerNode("gpu")
        outs = _run_sequence(gl_ctx, node, seq)
        inputs = [_to_rgba(rgb) for rgb in seq]
        assert float(np.abs(outs[3] - inputs[3]).max()) > 0.01
        assert node.warming is False
        # Never exceeds the hard bound vs the input (bounded gain clamp).
        for f in range(len(seq)):
            d = float(np.abs(outs[f] - inputs[f]).max())
            assert d <= _HARD_ATOL + 1.0  # gain ≤ 8: bounded, not blowup


class TestNoFullFrameReadback:
    def test_default_mode_never_reads_rgba(self, gl_ctx, monkeypatch):
        """The legacy RGBA readback must not run on the default path."""
        calls = {"n": 0}
        real = _gpu_readback.read_rgba

        def _spy(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(_gpu_readback, "read_rgba", _spy)
        seq = _flicker_sequence()
        node = DeflickerNode("gpu")
        outs = _run_sequence(gl_ctx, node, seq)
        inputs = [_to_rgba(rgb) for rgb in seq]
        # Corrected frame proves the GPU path produced real output.
        assert float(np.abs(outs[3] - inputs[3]).max()) > 0.01
        assert calls["n"] == 0, "default path hit the RGBA readback"

    def test_rgb_mode_still_uses_legacy_chain(self, gl_ctx, monkeypatch):
        """per_pixel + luma_only=False keeps the full-frame CPU chain."""
        calls = {"n": 0}
        real = _gpu_readback.read_rgba

        def _spy(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(_gpu_readback, "read_rgba", _spy)
        seq = _flicker_sequence()
        node = DeflickerNode("gpu")
        node.parameters["luma_only"] = False
        _run_sequence(gl_ctx, node, seq)
        assert calls["n"] == len(seq)


class TestFallbackAndWarming:
    def test_gpu_failure_falls_back_to_cpu_chain(self, gl_ctx, monkeypatch):
        """A dead luma pass degrades to the legacy chain, not passthrough."""
        monkeypatch.setattr(DeflickerNode, "_luma_plane_gpu",
                            lambda self, *a, **k: None)
        seq = _flicker_sequence()
        node = DeflickerNode("gpu")
        outs = _run_sequence(gl_ctx, node, seq)
        inputs = [_to_rgba(rgb) for rgb in seq]
        assert float(np.abs(outs[3] - inputs[3]).max()) > 0.01
        assert node.warming is False

    def test_no_fb_manager_warming_passthrough(self, gl_ctx):
        """Without an FBO manager both paths degrade with the same message."""
        node = DeflickerNode("gpu")
        _, tr = _upload(gl_ctx, _FakeFB(gl_ctx),
                        _to_rgba(np.full((_H, _W, 3), 0.4, dtype=np.float32)))
        result = node._do_evaluate({"image": tr}, _RealCtx(gl_ctx, 0),
                                   None, None)
        assert result["result"] is tr
        assert node.warming is True
        assert "readback unavailable" in node.error_message

    def test_warming_window_semantics_unchanged(self, gl_ctx):
        """Sequential frames correct immediately (the trailing window
        grows from one frame); a JUMP to a far frame finds the window
        missing and degrades to the visible warming passthrough."""
        seq = _flicker_sequence()
        node = DeflickerNode("gpu")
        fb = _FakeFB(gl_ctx)
        registry = {}
        wctx = _RealCtx(gl_ctx, registry=registry)
        for f, rgb in enumerate(seq):
            tex, tr = _upload(gl_ctx, fb, _to_rgba(rgb))
            wctx.set_frame(f)
            result = node._do_evaluate({"image": tr}, wctx, fb, None)
            assert node.warming is False
            assert result["result"] is not tr
        # Jump: frame 30's trailing window [26..30] is absent → passthrough.
        tex, tr = _upload(gl_ctx, fb, _to_rgba(seq[0]))
        wctx.set_frame(30)
        result = node._do_evaluate({"image": tr}, wctx, fb, None)
        assert node.warming is True
        assert "warming" in node.error_message
        assert result["result"] is tr


class TestDeterminism:
    def test_same_frame_bit_identical(self, gl_ctx):
        seq = _flicker_sequence()
        node = DeflickerNode("gpu")
        fb = _FakeFB(gl_ctx)
        registry = {}
        wctx = _RealCtx(gl_ctx, registry=registry)
        # Seed the window so frame 4 is corrected, not warming.
        for f in range(4):
            tex0, tr0 = _upload(gl_ctx, fb, _to_rgba(seq[f]))
            wctx.set_frame(f)
            node._do_evaluate({"image": tr0}, wctx, fb, None)
        tex1, tr1 = _upload(gl_ctx, fb, _to_rgba(seq[4]))
        wctx.set_frame(4)
        a = node._do_evaluate({"image": tr1}, wctx, fb, None)
        b = node._do_evaluate({"image": tr1}, wctx, fb, None)
        ra = _read_result(registry, a["result"])
        rb = _read_result(registry, b["result"])
        np.testing.assert_array_equal(ra, rb)
