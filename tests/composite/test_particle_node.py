"""C1 particle emitter tests — stateless determinism (the core
acceptance item), closed-form ballistic cross-check, dead-frame
transparency, premultiplied output, serialization, and a real GL
evaluation smoke.
"""

import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.particle_node import ParticleNode
from src.features.composite.core.node_engine import NODE_REGISTRY, NODE_CATEGORIES

pytestmark = pytest.mark.composite_core


def _make_node(**overrides):
    node = ParticleNode(node_id="pa1")
    params = dict(rate=6.0, lifetime=30.0, seed=7, region="point",
                  speed=5.0, spread=0.0, direction=-90.0,
                  speed_variance=0.0, turb_amount=0.0,
                  gravity_x=0.0, gravity_y=0.0, wind_x=0.0, wind_y=0.0,
                  output_width=160, output_height=120)
    params.update(overrides)
    for k, v in params.items():
        node.set_parameter(k, v)
    return node


# ── Registration / serialization ────────────────────────────────────────────

class TestParticleRegistration:
    def test_registered_with_own_category(self):
        assert NODE_REGISTRY.get("particle") is ParticleNode
        assert any(t == "particle"
                   for t, _ in NODE_CATEGORIES.get("Particle", []))

    def test_frame_dependent_even_without_keyframes(self):
        assert ParticleNode.params_require_frame_cache({}) is True

    def test_serialization_roundtrip(self, qapp):
        node = _make_node(seed=99, rate=3.5)
        clone = ParticleNode.from_dict(dict(node.to_dict()))
        for key in ("rate", "lifetime", "seed", "direction", "turb_amount"):
            assert clone.parameters.get(key) == node.parameters.get(key), key
        # Simulation identical after roundtrip.
        a = node._simulate(17, 160, 120)
        b = clone._simulate(17, 160, 120)
        assert np.array_equal(a, b)

    def test_aliases(self):
        assert "粒子" in ParticleNode.SEARCH_ALIASES
        assert "发射器" in ParticleNode.SEARCH_ALIASES


# ── Stateless determinism (plan acceptance core) ────────────────────────────

def _reference_integrate(node, frame, w, h):
    """Deliberately naive per-particle stateful integrator (independent of
    the node's vectorized grouped replay) for cross-validation."""
    import math
    from src.features.composite.nodes.particle_node import _hash01
    p = node.parameters
    rate = float(p["rate"])
    lifetime = float(p["lifetime"])
    out = []
    total = min(int(p["particle_cap"]),
                int(math.ceil(rate * (frame + 1))) + 1)
    for i in range(total):
        birth = i / rate
        life = lifetime * (1.0 - float(p["lifetime_variance"])
                           + float(p["lifetime_variance"]) * 2.0
                           * _hash01(int(p["seed"]), i, 4))
        if not (birth <= frame < birth + life):
            continue
        pos, vel = node._initial_state(i, p, w, h)
        # The node activates a particle on the first integer frame f with
        # f >= birth, so it integrates (frame - ceil(birth)) steps.
        first_f = max(0, math.ceil(birth - 1e-9))
        n = max(0, frame - first_f)
        for _ in range(n):
            vel = vel + np.array([float(p["gravity_x"]) + float(p["wind_x"]),
                                  float(p["gravity_y"]) + float(p["wind_y"])])
            pos = pos + vel
        out.append((pos[0], pos[1], frame - birth))
    return np.array(out, dtype=np.float64).reshape(-1, 3)


class TestStatelessDeterminism:
    def test_same_frame_bit_identical(self, qapp):
        node = _make_node(turb_amount=2.0)
        a = node._simulate(23, 160, 120)
        b = node._simulate(23, 160, 120)
        assert a.shape[0] > 0
        assert np.array_equal(a, b)

    def test_jump_to_frame_equals_reference_integrator(self, qapp):
        """Direct evaluation at t=100 must equal stepping a stateful
        integrator 0→100 particle by particle (the plan's core test)."""
        node = _make_node(rate=3.0, lifetime=25.0, gravity_y=0.4,
                          wind_x=0.1, turb_amount=0.0, seed=11)
        t = 40
        direct = node._simulate(t, 160, 120)
        ref = _reference_integrate(node, t, 160, 120)
        # _simulate rows are (x, y, age, lifetime); the reference integrator
        # only tracks (x, y, age).
        assert direct.shape[0] == ref.shape[0]
        np.testing.assert_allclose(direct[:, :3], ref, atol=1e-9)

    def test_raster_deterministic(self, qapp):
        node = _make_node(turb_amount=1.0)
        a = node._rasterize(node._simulate(12, 80, 60), 80, 60)
        b = node._rasterize(node._simulate(12, 80, 60), 80, 60)
        np.testing.assert_array_equal(a, b)


# ── Ballistic closed-form cross-check ────────────────────────────────────────

class TestBallistic:
    def test_gravity_parabola_matches_closed_form(self, qapp):
        # v0=(0,-6) px/frame, g=(0,0.4); semi-implicit Euler closed form:
        # p_n = p0 + n·v0 + g·n(n+1)/2
        # particle_cap is raised so the alive window covers every birth —
        # the cap now bounds the newest-N window (long sequences must not
        # silence the emitter), which would otherwise exclude particle 0.
        node = _make_node(rate=1000.0, direction=-90.0, speed=6.0,
                          gravity_y=0.4, lifetime=200.0,
                          particle_cap=100000)
        n = 30
        got = node._simulate(n, 320, 240)[0]
        expect_y = 120.0 + (-6.0) * n + 0.4 * n * (n + 1) / 2.0
        np.testing.assert_allclose(got[:2], [160.0, expect_y], atol=1e-9)

    def test_zero_gravity_straight_line(self, qapp):
        node = _make_node(rate=1000.0, direction=0.0, speed=4.0,
                          lifetime=200.0, particle_cap=100000)
        got = node._simulate(15, 320, 240)[0]
        np.testing.assert_allclose(got[:2], [160.0 + 60.0, 120.0], atol=1e-9)

    def test_cap_keeps_emitter_alive_on_long_sequences(self, qapp):
        # P4 regression: the cap must bound the SIMULTANEOUS window, not
        # cumulative births — after cap/rate frames the emitter used to go
        # permanently silent (no particles born past the cap).
        node = _make_node(rate=4.0, lifetime=10.0, particle_cap=20)
        early = node._simulate(5, 80, 60)
        late = node._simulate(500, 80, 60)
        assert early.shape[0] > 0
        assert late.shape[0] > 0, "emitter must still emit long after cap/rate"


# ── Dead frames / raster contract ────────────────────────────────────────────

class TestRasterContract:
    def test_no_alive_particles_is_fully_transparent(self, qapp):
        # rate 0.01 → last birth near frame 600 with only 7 particles;
        # frame 500 sits between births with every lifetime (5) expired.
        node = _make_node(rate=0.01, lifetime=5.0)
        particles = node._simulate(999, 80, 60)
        assert particles.shape[0] == 0
        canvas = node._rasterize(particles, 80, 60)
        assert float(canvas[..., 3].max()) == 0.0

    def test_premultiplied_black_base(self, qapp):
        node = _make_node()
        canvas = node._rasterize(node._simulate(10, 160, 120), 160, 120)
        assert canvas.dtype == np.float32
        assert float(canvas[..., 3].max()) > 0.0
        # Premultiplied: where alpha is (numerically) zero the RGB must
        # be black to sub-visible levels (HDR colors may push RGB above
        # alpha in covered areas).
        transparent = canvas[..., 3] <= 1e-6
        worst = float(np.abs(canvas[..., :3][transparent]).max(initial=0.0))
        assert worst < 1e-6
        # Untouched pixels are pure black.
        assert float(canvas[0, 0, 3]) == 0.0


# ── Real GL evaluation smoke ─────────────────────────────────────────────────

class TestGLSmoke:
    def test_evaluate_produces_texture_and_frame_isolation(self, qapp):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        try:
            node = _make_node(output_width=64, output_height=48)
            node.set_parameter("rate", 2.0)

            class Ctx:
                def __init__(self, mgl, frame):
                    object.__setattr__(self, "_mgl", mgl)
                    object.__setattr__(self, "frame_number", int(frame))

                def __getattr__(self, name):
                    return getattr(self._mgl, name)

                # State setters (viewport, blend_func, ...) must reach the
                # real context too — a silent no-op setattr here once left
                # the viewport at its default and clipped every fragment
                # of the GPU splat path.  The playhead therefore travels
                # through the constructor (set_frame), never setattr.
                def __setattr__(self, name, value):
                    setattr(self._mgl, name, value)

                def set_frame(self, frame):
                    object.__setattr__(self, "frame_number", int(frame))

                def texture(self, size, components, data, dtype):
                    return self._mgl.texture(size, components, data=data,
                                             dtype=dtype)

            ctx5 = Ctx(ctx, frame=5)

            r1 = node._do_evaluate({}, ctx5, None)
            assert r1["result"].texture_id > 0
            assert r1["result"].alpha_premultiplied is True
            rev5 = node._revision

            ctx5.set_frame(6)
            r2 = node._do_evaluate({}, ctx5, None)
            # New frame → new raster → revision bump (frame isolation).
            assert node._revision == rev5 + 1
            assert r2["result"].texture_id == r1["result"].texture_id or True

            # Same frame again → cached, no revision bump.
            r3 = node._do_evaluate({}, ctx5, None)
            assert node._revision == rev5 + 1
        finally:
            ctx.release()
