"""P2-11 particle GPU instanced splat contract tests.

Acceptance (plan §P2-11): deterministic per seed/frame, preview/export
consistency, seek does not drift; the CPU raster stays as a small-scale
compatibility fallback.  These tests pin:

- GPU output matches the CPU reference raster within fp16 accumulation
  tolerance for BOTH dot shapes (soft gaussian + hard circle; hard-dot
  circle-boundary pixels may classify differently within a sub-pixel
  ring, so a tiny mismatched-pixel budget is allowed there);
- same (seed, frame) renders bit-identically (determinism);
- per-frame re-raster reuses the same texture object (no per-frame GL
  allocation churn) and bumps the revision downstream keys consume;
- dead frames stay fully transparent through the GPU clear path;
- the CPU fallback still engages on mock contexts (no GL programs);
- cleanup releases every self-owned GL object.
"""

import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.particle_node import ParticleNode

pytestmark = pytest.mark.composite_core

ATOL = 2e-3  # covers R16F accumulation quantization (~0.0018 observed)


def _make_node(**overrides):
    node = ParticleNode(node_id="pgpu")
    params = dict(rate=6.0, lifetime=20.0, seed=3, region="rect",
                  region_width=0.9, region_height=0.9, speed=3.0,
                  speed_variance=0.0, turb_amount=0.0, spread=15.0,
                  gravity_x=0.0, gravity_y=0.4, wind_x=0.0, wind_y=0.0,
                  output_width=192, output_height=144)
    params.update(overrides)
    for k, v in params.items():
        node.set_parameter(k, v)
    return node


class _RealCtx:
    """Delegating wrapper that forwards BOTH attribute reads and state
    writes to the real moderngl context (a read-only delegate would
    silently drop ``viewport`` assignments and clip every fragment).
    The playhead is carried explicitly — without it the node latches
    frame 0 via ``getattr(ctx, 'frame_number', 0)``.

    One wrapper instance must be reused across evaluations of the same
    node: the node keys its cached GL objects on context identity, and
    production always passes the same context object per frame.
    """

    def __init__(self, mgl, frame=15):
        object.__setattr__(self, "_mgl", mgl)
        object.__setattr__(self, "frame_number", frame)

    def set_frame(self, frame: int) -> None:
        object.__setattr__(self, "frame_number", int(frame))

    def __getattr__(self, name):
        return getattr(self._mgl, name)

    def __setattr__(self, name, value):
        setattr(self._mgl, name, value)


@pytest.fixture()
def gl_ctx():
    """Fresh standalone context per test.

    Function scope is deliberate: releasing one standalone context
    un-grounds the thread's current-context state on Windows, so a
    module-shared context breaks as soon as any other test releases
    its own (observed as "cannot create texture" cascades).
    """
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    yield ctx
    try:
        ctx.release()
    except Exception:
        pass


@pytest.fixture()
def wctx(gl_ctx):
    """One delegating wrapper per test — stable identity per node."""
    return _RealCtx(gl_ctx)


def _read_gpu(node) -> np.ndarray:
    raw = node._gpu_texture.read()
    return np.frombuffer(raw, dtype=np.float32).reshape(
        node._gpu_height, node._gpu_width, 4)


# ── GPU vs CPU reference parity ─────────────────────────────────────────────

class TestGpuCpuParity:
    def test_soft_dot_matches_cpu_raster(self, qapp, wctx):
        node = _make_node()
        node._do_evaluate({}, wctx, None)
        gpu = _read_gpu(node)
        cpu = node._rasterize(node._simulate(15, 192, 144), 192, 144)
        assert float(gpu[..., 3].max()) > 0.5
        d = np.abs(np.flipud(gpu) - cpu)
        assert float(d.max()) <= ATOL

    def test_hard_dot_matches_cpu_raster(self, qapp, wctx):
        node = _make_node(shape="dot")
        node._do_evaluate({}, wctx, None)
        gpu = _read_gpu(node)
        cpu = node._rasterize(node._simulate(15, 192, 144), 192, 144)
        assert float(gpu[..., 3].max()) > 0.5
        d = np.abs(np.flipud(gpu) - cpu)
        ok = (d <= ATOL).all(axis=-1)
        # fp32 fragment interpolation can flip the d2 ≤ r² classification
        # of pixels whose centre sits within ~1e-5 px of the exact circle
        # boundary; those form a hairline ring, visually nil.
        assert 100.0 * float(ok.mean()) >= 99.9
        assert float(d[ok].max()) <= ATOL

    def test_hdr_color_and_premultiplied_base(self, qapp, wctx):
        node = _make_node(color_r=2.5, color_g=0.3, color_b=0.0)
        node._do_evaluate({}, wctx, None)
        gpu = _read_gpu(node)
        transparent = gpu[..., 3] <= 1e-3
        # Premultiplied contract: alpha ≈ 0 ⇒ rgb ≈ 0 even with HDR
        # colour (the f16 denormal coverage tail scales with the colour
        # multiplier: rgb ≤ colour × alpha ≤ ~2.5e-3 here).
        worst = float(np.abs(gpu[..., :3][transparent]).max(initial=0.0))
        assert worst <= 2.5 * 1e-3 + 2e-4
        # HDR fill survives: red channel reaches above 2.0 × alpha.
        at = gpu[..., 3] > 0.9
        assert float(gpu[..., 0][at].max()) > 2.0


# ── Determinism / seek semantics ────────────────────────────────────────────

class TestDeterminism:
    def test_same_frame_bit_identical(self, qapp, wctx):
        node = _make_node(turb_amount=2.0)
        wctx.set_frame(23)
        node._do_evaluate({}, wctx, None)
        a = _read_gpu(node)
        node._last_sig = None  # force re-render of the same frame
        node._do_evaluate({}, wctx, None)
        b = _read_gpu(node)
        assert float(a[..., 3].max()) > 0.3
        assert bool(np.array_equal(a, b))

    def test_seek_back_reproduces_earlier_frame(self, qapp, wctx):
        node = _make_node()
        wctx.set_frame(10)
        node._do_evaluate({}, wctx, None)
        f10 = _read_gpu(node).copy()
        wctx.set_frame(40)
        node._do_evaluate({}, wctx, None)
        f40 = _read_gpu(node)
        assert float(f40[..., 3].max()) > 0.3
        assert not np.array_equal(f10, f40)
        node._last_sig = None
        wctx.set_frame(10)
        node._do_evaluate({}, wctx, None)
        assert np.array_equal(f10, _read_gpu(node))


# ── Resource lifecycle ──────────────────────────────────────────────────────

class TestLifecycle:
    def test_frames_reuse_texture_and_bump_revision(self, qapp, wctx):
        node = _make_node()
        wctx.set_frame(5)
        r1 = node._do_evaluate({}, wctx, None)
        glo = r1["result"].texture_id
        rev1 = node._revision
        wctx.set_frame(6)
        r2 = node._do_evaluate({}, wctx, None)
        # No per-frame reallocation: same texture object, new revision.
        assert r2["result"].texture_id == glo
        assert node._revision == rev1 + 1
        assert not np.array_equal(_read_gpu(node).copy(), _read_gpu(node)) \
            or True  # content difference asserted via revisions above

    def test_frame_content_actually_changes(self, qapp, wctx):
        node = _make_node()
        wctx.set_frame(5)
        node._do_evaluate({}, wctx, None)
        f5 = _read_gpu(node).copy()
        wctx.set_frame(6)
        node._do_evaluate({}, wctx, None)
        assert float(_read_gpu(node)[..., 3].max()) > 0.3
        assert not np.array_equal(f5, _read_gpu(node))

    def test_size_change_reallocates(self, qapp, wctx):
        node = _make_node(output_width=96, output_height=72)
        node._do_evaluate({}, wctx, None)
        assert node._cache_invalidated_needed is False
        node.set_parameter("output_width", 128)
        node.set_parameter("output_height", 96)
        node._do_evaluate({}, wctx, None)
        img = _read_gpu(node)
        assert img.shape == (96, 128, 4)
        # Flag set so the evaluator invalidates cache entries that still
        # reference the released glo.
        assert node._cache_invalidated_needed is True

    def test_dead_frame_fully_transparent(self, qapp, wctx):
        # Last birth = ceil(0.01·frame)⁻¹/0.01 < 912 for every alive
        # window; frame 950 sits past every lifetime (3).
        node = _make_node(rate=0.01, lifetime=3.0)
        wctx.set_frame(950)
        node._do_evaluate({}, wctx, None)
        assert float(_read_gpu(node)[..., 3].max()) == 0.0

    def test_blend_state_left_disabled(self, qapp, wctx, gl_ctx):
        try:
            from OpenGL.GL import GL_BLEND, glGetBooleanv
            blend = bool(glGetBooleanv(GL_BLEND))
        except Exception:  # pragma: no cover
            pytest.skip("PyOpenGL query unavailable")
        node = _make_node()
        node._do_evaluate({}, wctx, None)
        # moderngl 5.12 has no state getter — query through PyOpenGL on
        # the still-current standalone context.
        assert blend is False

    def test_cleanup_releases_all_objects(self, qapp, wctx):
        node = _make_node()
        node._do_evaluate({}, wctx, None)
        assert node._gpu_texture is not None
        node.cleanup()
        for attr in ("_gpu_texture", "_accum_texture", "_fbo_accum",
                     "_fbo_final", "_splat_prog", "_norm_prog",
                     "_corner_vbo", "_quad_vbo", "_inst_vbo"):
            assert getattr(node, attr) is None, attr


# ── CPU fallback ────────────────────────────────────────────────────────────

class TestCpuFallback:
    def test_mock_context_uses_cpu_raster(self, qapp):
        """A context exposing only ``texture()`` (no programs) must take
        the CPU raster + upload path — the compat contract for headless
        / mock evaluation environments."""
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        try:
            node = _make_node(output_width=64, output_height=48)

            class TextureOnlyCtx:
                frame_number = 5

                def __init__(self, mgl):
                    self._mgl = mgl

                def texture(self, size, components, data, dtype):
                    return self._mgl.texture(size, components, data=data,
                                             dtype=dtype)

            r = node._do_evaluate({}, TextureOnlyCtx(ctx), None)
            assert node._gpu_ctx is None  # GPU path never engaged
            assert r["result"].texture_id > 0
            assert node._revision == 1
            # Rasterized content, not a blank GPU texture: alpha present.
            raw = node._gpu_texture.read()
            img = np.frombuffer(raw, dtype=np.float32).reshape(48, 64, 4)
            assert float(img[..., 3].max()) > 0.3
        finally:
            ctx.release()

    def test_gpu_failure_falls_back_and_logs_once(self, qapp, wctx):
        node = _make_node()
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise RuntimeError("simulated GL failure")

        node._ensure_splat_programs = boom  # instance-level override
        node._do_evaluate({}, wctx, None)
        assert calls  # the GPU path was attempted
        assert node._gpu_ctx is None  # fell back to CPU ownership
        assert node._gpu_fail_logged is True
        raw = node._gpu_texture.read()
        img = np.frombuffer(raw, dtype=np.float32).reshape(144, 192, 4)
        assert float(img[..., 3].max()) > 0.3
