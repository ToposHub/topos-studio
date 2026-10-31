"""2D particle emitter — stateless, deterministic simulation (C1).

Fusion pEmitter / AE Particular counterpart.  The composite evaluation is
pull-based over immutable snapshots, so the simulation must be
replayable for ANY frame independently — no per-frame state is carried.
Every particle's birth is a closed-form function of the seed and rate
(``birth_i = i / rate``); evaluating frame ``t`` replays the fixed
force field (gravity / wind / deterministic turbulence) from each
particle's birth to ``t`` on a unit-step time grid, fully vectorized in
numpy.  Same seed + same params + same frame ⇒ bit-identical output,
and jumping straight to frame 100 equals stepping 0→100.

Output is a canvas-sized premultiplied RGBA float texture (black base,
straight colors multiplied by coverage) ready for Merge-over.

Rasterization (P2-11): the splat is an instanced quad draw on the GPU —
one instance per alive particle, additive coverage accumulation into an
R32F texture, then a full-screen normalize pass writing premultiplied
RGBA.  This replaces the per-particle Python/numpy raster loop (~170ms
at 1080p/500 particles) with a single sub-ms draw pair and removes the
per-frame 33MB CPU→GPU texture upload.  The CPU raster is kept as a
compatibility fallback for mock/no-GL contexts and GPU failures.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

from .base_node import Node, ParamDef, Port, TextureResult
from src.features.composite.core.format import BBox, ImageFormat


SHAPES = ("dot", "soft_dot")

_DEFAULTS = {
    # Emission
    "region": "point",
    "region_width": 0.2,
    "region_height": 0.2,
    "rate": 4.0,            # particles per frame
    "seed": 1,
    "lifetime": 40.0,       # frames
    "lifetime_variance": 0.25,
    "speed": 6.0,           # px / frame
    "speed_variance": 0.3,
    "direction": -90.0,     # degrees, 0 = +x, −90 = up (y-down canvas)
    "spread": 15.0,         # degrees half-angle
    # Forces (px / frame²)
    "gravity_x": 0.0,
    "gravity_y": 0.4,
    "wind_x": 0.0,
    "wind_y": 0.0,
    "turb_amount": 0.0,     # px / frame² peak
    "turb_scale": 0.01,     # noise spatial frequency (per px)
    # Appearance
    "size_start": 6.0,
    "size_end": 1.0,
    "opacity_start": 1.0,
    "opacity_end": 0.0,
    "shape": "soft_dot",
    "color_r": 1.0,
    "color_g": 0.85,
    "color_b": 0.4,
    # Output
    "output_width": 1920,
    "output_height": 1080,
    "particle_cap": 5000,
}


def _hash01(*ints: int) -> float:
    """Deterministic hash of integer tuples → [0, 1).

    SplitMix64-style finalizer on the XOR-folded inputs; pure integer
    math so results are identical across platforms and processes.
    """
    h = 0x9E3779B97F4A7C15
    for v in ints:
        x = (int(v) & 0xFFFFFFFFFFFFFFFF) ^ h
        x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9
        x = (x ^ (x >> 27)) * 0x94D049BB133111EB
        x = x ^ (x >> 31)
        h = x
    return (h & 0xFFFFFFFF) / 4294967296.0


def _noise01(seed: int, x: float, y: float) -> float:
    """Deterministic 2D value noise in [0, 1] (bilinear, smoothstep)."""
    return float(_noise01_batch(seed,
                                np.array([x], dtype=np.float64),
                                np.array([y], dtype=np.float64))[0])


def _hash01_u64(seed: int, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Wrapping-64 SplitMix64 fold of (seed, xi, yi) arrays → [0, 1).

    AUD-7107: vectorized twin of ``_hash01``'s SplitMix64 finalizer with
    every multiply wrapping at 2**64 (numpy uint64 arithmetic wraps exactly
    like the canonical hash).  The scalar ``_hash01`` keeps its
    arbitrary-precision fold — it defines birth position/velocity/lifetime
    identity, which must not change — so the turbulence field (this hash)
    is a separate, equally deterministic stream.
    """
    h = np.full(xs.shape, 0x9E3779B97F4A7C15, dtype=np.uint64)
    seed_lane = np.full(xs.shape, int(seed) & 0xFFFFFFFFFFFFFFFF,
                        dtype=np.uint64)
    for lane in (seed_lane, xs.astype(np.uint64), ys.astype(np.uint64)):
        x = lane ^ h
        x = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        h = x ^ (x >> np.uint64(31))
    return (h & np.uint64(0xFFFFFFFF)).astype(np.float64) / 4294967296.0


def _noise01_batch(seed: int, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Vectorized value noise over coordinate arrays → [0, 1).

    Same bilinear-smoothstep construction as the scalar ``_noise01`` (which
    now delegates here), evaluated for a whole particle batch in numpy.
    Replaces the per-particle Python list comprehension in the turbulence
    replay loop — O((frame - t0) x N x 2) interpreter calls collapse to
    O((frame - t0)) vectorized passes.
    """
    xi = np.floor(xs)
    yi = np.floor(ys)
    fx = xs - xi
    fy = ys - yi
    sx = fx * fx * (3.0 - 2.0 * fx)
    sy = fy * fy * (3.0 - 2.0 * fy)
    xi_i = xi.astype(np.int64)
    yi_i = yi.astype(np.int64)
    n00 = _hash01_u64(seed, xi_i, yi_i)
    n10 = _hash01_u64(seed, xi_i + 1, yi_i)
    n01 = _hash01_u64(seed, xi_i, yi_i + 1)
    n11 = _hash01_u64(seed, xi_i + 1, yi_i + 1)
    top = n00 + (n10 - n00) * sx
    bot = n01 + (n11 - n01) * sx
    return top + (bot - top) * sy


# ── GPU splat shaders (P2-11, GLSL 3.3 strict float literals) ───────────────

# Instanced quad: one instance per alive particle expands around the
# particle center by the per-life-phase support radius (ceil(r·k)+1, the
# same bound the CPU raster uses), so off-screen and dead particles are
# clipped without CPU-side culling.
_PARTICLE_SPLAT_VERT = """
#version 330
in vec2 in_corner;      // quad corner in {-1.0, +1.0}^2
in vec4 in_particle;    // (x, y) px y-down, t in [0,1], unused
uniform vec2 u_size;
uniform float u_radius_start;
uniform float u_radius_end;
uniform float u_opacity_start;
uniform float u_opacity_end;
uniform float u_soft;   // 1.0 gaussian soft dot, 0.0 hard dot
out vec2 v_local;
flat out float v_radius;
flat out float v_opacity;
void main() {
    float t = clamp(in_particle.z, 0.0, 1.0);
    float radius = mix(u_radius_start, u_radius_end, t);
    float opacity = mix(u_opacity_start, u_opacity_end, t);
    float visible = (radius >= 0.3 && opacity > 0.003) ? 1.0 : 0.0;
    float support = ceil(radius * mix(1.0, 3.0, u_soft)) + 1.0;
    v_radius = radius;
    v_opacity = opacity * visible;
    v_local = in_corner * support * visible;
    // GL fragments are rasterized at pixel-centre continuous coordinates
    // (pixel i ↔ i+0.5) while the CPU raster samples d = grid − px at
    // integer indices; shifting the centre by half a pixel makes
    // fragment v_local exactly equal the CPU's dx/dy.
    vec2 px = in_particle.xy + vec2(0.5, 0.5) + v_local;
    vec2 ndc = vec2(px.x / max(u_size.x, 1.0) * 2.0 - 1.0,
                    1.0 - px.y / max(u_size.y, 1.0) * 2.0);
    gl_Position = vec4(ndc, 0.0, 1.0);
}
"""

# Coverage accumulation (additive blending, R channel only).  The final
# color depends only on the coverage SUM because every particle shares
# one emitter color — CPU: rgb = (Σ color·cov / max(Σcov, ε)) · clamp(Σcov)
# simplifies exactly to color·clamp(Σcov, 0, 1).
_PARTICLE_SPLAT_FRAG = """
#version 330
in vec2 v_local;
flat in float v_radius;
flat in float v_opacity;
uniform float u_soft;
out vec4 fragColor;
void main() {
    if (v_opacity <= 0.0) {
        fragColor = vec4(0.0, 0.0, 0.0, 0.0);
        return;
    }
    float d2 = dot(v_local, v_local);
    float cov = 0.0;
    if (u_soft > 0.5) {
        float sigma = max(v_radius * 0.5, 0.5);
        cov = exp(-d2 / (2.0 * sigma * sigma));
    } else {
        cov = (d2 <= v_radius * v_radius) ? 1.0 : 0.0;
    }
    fragColor = vec4(cov * v_opacity, 0.0, 0.0, 0.0);
}
"""

_PARTICLE_NORM_VERT = """
#version 330
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

_PARTICLE_NORM_FRAG = """
#version 330
uniform sampler2D u_cov;
uniform vec3 u_color;
in vec2 v_uv;
out vec4 fragColor;
void main() {
    float c = texture(u_cov, v_uv).r;
    float a = clamp(c, 0.0, 1.0);
    fragColor = vec4(u_color * a, a);
}
"""

_QUAD_XYUV = np.array([
    -1.0, -1.0, 0.0, 0.0,
     1.0, -1.0, 1.0, 0.0,
    -1.0,  1.0, 0.0, 1.0,
     1.0,  1.0, 1.0, 1.0,
], dtype="f4")

_QUAD_CORNERS = np.array([
    -1.0, -1.0,
     1.0, -1.0,
    -1.0,  1.0,
     1.0,  1.0,
], dtype="f4")


class ParticleNode(Node):
    """Stateless deterministic 2D particle emitter (GPU splat, CPU fallback)."""

    node_type = "particle"
    node_title = "Particles"
    i18n_title_key = "nodes.particle.title"
    header_color = "#16A085"
    IS_PLACEHOLDER = False
    supports_layer_root = True

    SEARCH_ALIASES = [
        "particle", "particles", "emitter", "fx",
        "粒子", "发射器", "粒子发射",
    ]
    DESCRIPTION = "无状态确定性 2D 粒子发射器（重力/风/湍流，弹道可复现）"
    TAGS = ["input", "source", "generator", "fx"]

    PARAM_DEFS = [
        ParamDef("region", "option", "point", labels=["point", "rect", "ellipse"],
                 group="Emission"),
        ParamDef("region_width", "float", 0.2, min_val=0.0, max_val=1.0,
                 step=0.01, group="Emission"),
        ParamDef("region_height", "float", 0.2, min_val=0.0, max_val=1.0,
                 step=0.01, group="Emission"),
        ParamDef("rate", "float", 4.0, min_val=0.01, max_val=200.0,
                 step=0.5, group="Emission"),
        ParamDef("seed", "int", 1, min_val=0, max_val=999999, step=1,
                 group="Emission"),
        ParamDef("lifetime", "float", 40.0, min_val=1.0, max_val=2000.0,
                 step=1.0, group="Emission"),
        ParamDef("lifetime_variance", "float", 0.25, min_val=0.0, max_val=1.0,
                 step=0.05, group="Emission"),
        ParamDef("speed", "float", 6.0, min_val=0.0, max_val=200.0,
                 step=0.5, group="Emission"),
        ParamDef("speed_variance", "float", 0.3, min_val=0.0, max_val=1.0,
                 step=0.05, group="Emission"),
        ParamDef("direction", "float", -90.0, min_val=-360.0, max_val=360.0,
                 step=1.0, group="Emission"),
        ParamDef("spread", "float", 15.0, min_val=0.0, max_val=180.0,
                 step=1.0, group="Emission"),
        ParamDef("gravity_x", "float", 0.0, min_val=-20.0, max_val=20.0,
                 step=0.05, group="Forces"),
        ParamDef("gravity_y", "float", 0.4, min_val=-20.0, max_val=20.0,
                 step=0.05, group="Forces"),
        ParamDef("wind_x", "float", 0.0, min_val=-20.0, max_val=20.0,
                 step=0.05, group="Forces"),
        ParamDef("wind_y", "float", 0.0, min_val=-20.0, max_val=20.0,
                 step=0.05, group="Forces"),
        ParamDef("turb_amount", "float", 0.0, min_val=0.0, max_val=20.0,
                 step=0.1, group="Forces"),
        ParamDef("turb_scale", "float", 0.01, min_val=0.0005, max_val=0.2,
                 step=0.001, group="Forces"),
        ParamDef("size_start", "float", 6.0, min_val=0.1, max_val=512.0,
                 step=0.5, group="Appearance"),
        ParamDef("size_end", "float", 1.0, min_val=0.0, max_val=512.0,
                 step=0.5, group="Appearance"),
        ParamDef("opacity_start", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.05, group="Appearance"),
        ParamDef("opacity_end", "float", 0.0, min_val=0.0, max_val=1.0,
                 step=0.05, group="Appearance"),
        ParamDef("shape", "option", "soft_dot", labels=list(SHAPES),
                 group="Appearance"),
        ParamDef("color_r", "float", 1.0, min_val=0.0, max_val=65535.0,
                 step=0.01, group="Appearance"),
        ParamDef("color_g", "float", 0.85, min_val=0.0, max_val=65535.0,
                 step=0.01, group="Appearance"),
        ParamDef("color_b", "float", 0.4, min_val=0.0, max_val=65535.0,
                 step=0.01, group="Appearance"),
        ParamDef("output_width", "int", 1920, min_val=1, max_val=16384,
                 step=1, hidden=True),
        ParamDef("output_height", "int", 1080, min_val=1, max_val=16384,
                 step=1, hidden=True),
        ParamDef("particle_cap", "int", 5000, min_val=1, max_val=50000,
                 step=100, hidden=True),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = []
        self.outputs = [Port("result", "output", "image")]
        self.parameters = dict(_DEFAULTS)
        # Self-managed GPU texture (text-node pattern): released on
        # content/frame change and in cleanup().
        self._gpu_texture: Optional[Any] = None
        self._last_sig: Optional[tuple] = None
        self._revision: int = 0
        self._cache_invalidated_needed = False
        # Latched playhead (mirrors the text node's C3 pattern): folded
        # into the content signature so the texture re-rasters per frame.
        self._current_frame: float = 0.0
        # GPU splat resources (P2-11): instanced quads + coverage
        # accumulation.  All objects are self-owned (node-pattern, like
        # _gpu_texture) and rebuilt when the GL context identity or the
        # output size changes.
        self._gpu_ctx: Optional[Any] = None
        self._accum_texture: Optional[Any] = None
        self._fbo_accum: Optional[Any] = None
        self._fbo_final: Optional[Any] = None
        self._gpu_width: int = 0
        self._gpu_height: int = 0
        self._splat_prog: Optional[Any] = None
        self._norm_prog: Optional[Any] = None
        self._corner_vbo: Optional[Any] = None
        self._quad_vbo: Optional[Any] = None
        self._inst_vbo: Optional[Any] = None
        self._inst_size: int = 0
        self._gpu_fail_logged: bool = False

    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, object]) -> bool:
        """The particle field is a pure function of the playhead — every
        frame differs even with nothing keyframed."""
        return True

    # ── Simulation core (pure, deterministic, frame-independent) ────────

    def _particle_births(self, rate: float, total: int) -> np.ndarray:
        return np.arange(total, dtype=np.float64) / max(1e-6, rate)

    def _initial_state(self, i: int, p: Dict[str, Any],
                       w: int, h: int) -> Tuple[np.ndarray, np.ndarray]:
        """Birth position / velocity of particle ``i`` (closed form)."""
        seed = int(p.get("seed", 1))
        region = str(p.get("region", "point"))
        rw = float(p.get("region_width", 0.2)) * w
        rh = float(p.get("region_height", 0.2)) * h
        u1 = _hash01(seed, i, 1)
        u2 = _hash01(seed, i, 2)
        cx, cy = w * 0.5, h * 0.5
        if region == "rect":
            px = cx + (u1 - 0.5) * rw
            py = cy + (u2 - 0.5) * rh
        elif region == "ellipse":
            # Uniform-ish disc sampling via rejection-free polar mapping.
            ang = u1 * 2.0 * math.pi
            rr = math.sqrt(u2)
            px = cx + math.cos(ang) * rr * rw * 0.5
            py = cy + math.sin(ang) * rr * rh * 0.5
        else:  # point
            px, py = cx, cy
        direction = float(p.get("direction", -90.0))
        spread = float(p.get("spread", 15.0))
        speed = float(p.get("speed", 6.0)) * (
            1.0 + float(p.get("speed_variance", 0.3))
            * (_hash01(seed, i, 3) - 0.5) * 2.0)
        ang = math.radians(direction + spread * (u2 - 0.5) * 2.0)
        return (
            np.array([px, py], dtype=np.float64),
            np.array([math.cos(ang) * speed, math.sin(ang) * speed],
                     dtype=np.float64),
        )

    def _simulate(self, frame: int, w: int, h: int) -> np.ndarray:
        """Particle positions/ages at ``frame`` — replay from each birth.

        Returns an (N, 4) float64 array of (x, y, age, lifetime) for ALIVE
        particles only; N may be 0.  Deterministic: identical inputs
        produce bit-identical output, and evaluating ``frame`` directly
        equals stepping frame-by-frame (no carried state).
        """
        p = self.parameters
        frame = max(0, int(frame))
        rate = max(0.01, float(p.get("rate", 4.0)))
        lifetime = max(1.0, float(p.get("lifetime", 40.0)))
        life_var = max(0.0, min(1.0, float(p.get("lifetime_variance", 0.25))))
        seed = int(p.get("seed", 1))
        cap = max(1, int(p.get("particle_cap", 5000)))

        # The cap bounds the SIMULTANEOUSLY simulated window, not the
        # cumulative birth count — capping "first cap particles ever born"
        # permanently silenced the emitter after cap/rate frames on long
        # sequences.  Particle identity stays the absolute index i
        # (birth_i = i / rate), so any sequence shorter than the cap
        # replays identically to the original formulation.
        life_max = lifetime * (1.0 + life_var)
        i_last = int(math.floor(rate * frame + 1e-9))
        i_first = max(0, int(math.ceil(rate * max(0.0, frame - life_max))) - 1)
        if i_last < i_first:
            return np.zeros((0, 4), dtype=np.float64)
        if i_last - i_first + 1 > cap:
            i_first = i_last - cap + 1
        abs_ids = np.arange(i_first, i_last + 1)
        births = abs_ids.astype(np.float64) / max(1e-6, rate)
        lives = lifetime * (1.0 - life_var
                            + life_var * 2.0
                            * np.array([_hash01(seed, i, 4)
                                        for i in abs_ids],
                                       dtype=np.float64))
        alive = (births <= frame) & (frame < births + lives)
        if not np.any(alive):
            return np.zeros((0, 4), dtype=np.float64)

        idx = np.nonzero(alive)[0]
        pos = np.zeros((len(idx), 2), dtype=np.float64)
        vel = np.zeros((len(idx), 2), dtype=np.float64)
        for k, i in enumerate(idx):
            pos[k], vel[k] = self._initial_state(int(abs_ids[i]), p, w, h)

        g = np.array([float(p.get("gravity_x", 0.0)),
                      float(p.get("gravity_y", 0.4))], dtype=np.float64)
        wind = np.array([float(p.get("wind_x", 0.0)),
                         float(p.get("wind_y", 0.0))], dtype=np.float64)
        turb = float(p.get("turb_amount", 0.0))
        turb_scale = float(p.get("turb_scale", 0.01))

        birth_vals = births[idx]
        # Replay on the unit time grid. Invariant: entering iteration f,
        # (pos, vel) hold the state AT time f for activated particles;
        # the step advances them to f+1. Particles born mid-replay are
        # reset to their closed-form birth state when activated, so the
        # final state after (frame - t0) iterations is exactly the
        # frame-t solution of the same recurrence a frame-by-frame
        # evaluation would produce (statelessness).
        t0 = int(math.floor(birth_vals.min()))
        for k in range(len(idx)):
            if birth_vals[k] <= t0:
                pos[k], vel[k] = self._initial_state(int(idx[k]), p, w, h)
        activated = birth_vals <= t0
        accel = g + wind
        for f in range(t0, frame):
            newly = (birth_vals <= f) & ~activated
            if np.any(newly):
                for k in np.nonzero(newly)[0]:
                    pos[k], vel[k] = self._initial_state(int(idx[k]), p, w, h)
                activated = activated | newly
            if turb > 0.0:
                # AUD-7107: the turbulence field is evaluated for the whole
                # batch in numpy (was: per-particle Python list comprehension
                # calling _noise01 twice per particle per replayed frame).
                turb_seed = seed * 7919 + f
                field_x = pos[:, 0] * turb_scale
                field_y = pos[:, 1] * turb_scale
                nx = _noise01_batch(turb_seed, field_x, field_y) - 0.5
                ny = _noise01_batch(turb_seed + 101, field_x, field_y) - 0.5
                vel[activated] += (accel + np.stack(
                    [nx, ny], axis=1) * 2.0 * turb)[activated]
            else:
                vel[activated] += accel
            # Semi-implicit Euler: v += a·dt; p += v·dt (dt = 1 frame).
            pos[activated] += vel[activated]

        age = frame - birth_vals
        return np.column_stack([pos, age, lives[idx]])

    def _rasterize(self, particles: np.ndarray, w: int, h: int) -> np.ndarray:
        """Soft-dot splat → premultiplied RGBA float32 (black base)."""
        p = self.parameters
        canvas = np.zeros((h, w, 4), dtype=np.float32)
        if particles.shape[0] == 0:
            return canvas
        life = max(1.0, float(p.get("lifetime", 40.0)))  # fallback only
        s0 = max(0.05, float(p.get("size_start", 6.0)))
        s1 = max(0.05, float(p.get("size_end", 1.0)))
        o0 = max(0.0, min(1.0, float(p.get("opacity_start", 1.0))))
        o1 = max(0.0, min(1.0, float(p.get("opacity_end", 0.0))))
        soft = str(p.get("shape", "soft_dot")) == "soft_dot"
        color = np.array([float(p.get("color_r", 1.0)),
                          float(p.get("color_g", 1.0)),
                          float(p.get("color_b", 1.0))], dtype=np.float32)

        ys, xs = np.mgrid[0:h, 0:w]
        for row in particles:
            px, py, age = row[0], row[1], row[2]
            # Per-particle lifetime: with lifetime_variance > 0 the ramps
            # must follow each particle's own life or short-lived ones get
            # cut mid-fade and long-lived ones park at t=1 forever.
            life_i = float(row[3]) if row.shape[0] > 3 else life
            t = min(1.0, max(0.0, float(age) / max(1.0, life_i)))
            radius = s0 + (s1 - s0) * t
            opacity = o0 + (o1 - o0) * t
            if radius < 0.3 or opacity <= 0.003:
                continue
            r = int(math.ceil(radius * (3.0 if soft else 1.0))) + 1
            x0, x1 = int(px) - r, int(px) + r + 1
            y0, y1 = int(py) - r, int(py) + r + 1
            if x1 <= 0 or y1 <= 0 or x0 >= w or y0 >= h:
                continue
            cx0, cy0 = max(0, x0), max(0, y0)
            cx1, cy1 = min(w, x1), min(h, y1)
            dx = xs[cy0:cy1, cx0:cx1] - px
            dy = ys[cy0:cy1, cx0:cx1] - py
            d2 = dx * dx + dy * dy
            if soft:
                # Gaussian falloff, ~3σ support.
                sigma = max(radius / 2.0, 0.5)
                cov = np.exp(-d2 / (2.0 * sigma * sigma)).astype(np.float32)
            else:
                cov = (d2 <= radius * radius).astype(np.float32)
            cov *= opacity
            canvas[cy0:cy1, cx0:cx1, :3] += color * cov[:, :, np.newaxis]
            canvas[cy0:cy1, cx0:cx1, 3] += cov
        # Splat colours are already premultiplied (color·cov), so the
        # accumulated RGB must NOT be multiplied by alpha again — that
        # squared the falloff of every soft dot.  Normalize the weighted
        # colour by the accumulated coverage, then premultiply by the
        # clipped alpha (keeps overlapping splats in the premult contract).
        cov_sum = canvas[:, :, 3:4]
        alpha = np.clip(cov_sum, 0.0, 1.0)
        canvas[:, :, :3] = (canvas[:, :, :3]
                            / np.maximum(cov_sum, 1e-6)) * alpha
        canvas[:, :, 3] = alpha[:, :, 0]
        np.nan_to_num(canvas, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        return canvas

    # ── Evaluation (text-node pattern: self-managed GPU texture) ────────

    def _content_hash(self) -> int:
        p = self.parameters
        keys = tuple(sorted(_DEFAULTS.keys()))
        return hash((tuple(float(p.get(k, _DEFAULTS[k]))
                           if not isinstance(_DEFAULTS[k], str) else
                           str(p.get(k, _DEFAULTS[k]))
                           for k in keys),
                     int(self._current_frame)))

    def _update_current_frame(self, ctx) -> None:
        frame = getattr(ctx, "frame_number", 0)
        try:
            self._current_frame = float(frame)
        except (TypeError, ValueError):
            self._current_frame = 0.0

    # ── GPU splat (P2-11) ────────────────────────────────────────────────

    def _release_gpu_objects(self, release: bool) -> None:
        """Drop (and optionally release) every self-owned GL object.

        ``release=False`` is used when the owning context itself went
        away (context recreation): calling .release() on dead-context
        objects would raise, so the references are simply dropped.
        """
        for name in ("_fbo_final", "_fbo_accum", "_accum_texture",
                     "_gpu_texture", "_splat_prog", "_norm_prog",
                     "_corner_vbo", "_quad_vbo", "_inst_vbo"):
            obj = getattr(self, name, None)
            setattr(self, name, None)
            if obj is not None and release:
                try:
                    obj.release()
                except Exception:
                    pass
                if name == "_gpu_texture":
                    # Released glo may live on in evaluator cache entries
                    # (this node + downstream) — flag them for invalidation
                    # (see base_evaluator's self-managed source handling).
                    self._cache_invalidated_needed = True
        self._gpu_width = 0
        self._gpu_height = 0
        self._inst_size = 0

    def _ensure_gpu_targets(self, gl, w: int, h: int) -> None:
        """Allocate/reuse the final RGBA + coverage R32F textures and FBOs."""
        if (self._gpu_ctx is gl and self._gpu_texture is not None
                and self._fbo_final is not None
                and self._fbo_accum is not None
                and self._accum_texture is not None
                and self._gpu_width == w and self._gpu_height == h):
            return
        self._release_gpu_objects(release=(self._gpu_ctx is gl))
        import moderngl
        self._gpu_texture = gl.texture((w, h), 4, dtype="f4")
        self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
        # Coverage accumulates with additive blending; blending against a
        # 32F attachment needs EXT_float_blend (silently discarded on GL
        # 3.3), so the accumulation target is R16F — blendable and more
        # than precise enough for clamp(Σcov, 0, 1).
        self._accum_texture = gl.texture((w, h), 1, dtype="f2")
        self._fbo_final = gl.framebuffer(color_attachments=[self._gpu_texture])
        self._fbo_accum = gl.framebuffer(color_attachments=[self._accum_texture])
        self._gpu_width = w
        self._gpu_height = h
        self._gpu_ctx = gl

    def _ensure_splat_programs(self, gl) -> Any:
        if (self._splat_prog is not None and self._norm_prog is not None
                and self._corner_vbo is not None and self._quad_vbo is not None
                and self._gpu_ctx is gl):
            return self._splat_prog
        self._splat_prog = gl.program(
            vertex_shader=_PARTICLE_SPLAT_VERT,
            fragment_shader=_PARTICLE_SPLAT_FRAG)
        self._norm_prog = gl.program(
            vertex_shader=_PARTICLE_NORM_VERT,
            fragment_shader=_PARTICLE_NORM_FRAG)
        self._corner_vbo = gl.buffer(_QUAD_CORNERS.tobytes())
        self._quad_vbo = gl.buffer(_QUAD_XYUV.tobytes())
        return self._splat_prog

    def _ensure_instance_buffer(self, gl, n: int) -> Any:
        need = max(n, 1) * 16
        if self._inst_vbo is None or self._inst_size < need \
                or self._gpu_ctx is not gl:
            if self._inst_vbo is not None:
                try:
                    self._inst_vbo.release()
                except Exception:
                    pass
            self._inst_vbo = gl.buffer(reserve=need)
            self._inst_size = need
        return self._inst_vbo

    def _gpu_render(self, gl, particles: np.ndarray, w: int, h: int) -> bool:
        """Draw the splat entirely on the GPU.

        Splat pass: instanced quads, additive coverage accumulation into
        R32F.  Normalize pass: full-screen quad writes premultiplied RGBA
        (rgb = color·α, α = clamp(Σcov)).  Deterministic per (seed,
        frame, GPU): identical command stream ⇒ identical additive order.
        Returns False when GL is unavailable (mock context) — any GL
        exception propagates to the caller's fallback handler.
        """
        if getattr(gl, "program", None) is None:
            return False
        import moderngl
        from src.features.composite.core.gl_bind import bind_texture_unit

        n = int(particles.shape[0])
        self._ensure_gpu_targets(gl, w, h)
        self._ensure_splat_programs(gl)
        inst = np.zeros((max(n, 1), 4), dtype=np.float32)
        if n > 0:
            inst[:n, 0] = particles[:, 0]
            inst[:n, 1] = particles[:, 1]
            life = np.maximum(particles[:, 3], 1.0)
            inst[:n, 2] = np.clip(particles[:, 2] / life, 0.0, 1.0)
        inst_vbo = self._ensure_instance_buffer(gl, n)
        inst_vbo.write(inst.tobytes())

        p = self.parameters
        s0 = max(0.05, float(p.get("size_start", 6.0)))
        s1 = max(0.05, float(p.get("size_end", 1.0)))
        o0 = max(0.0, min(1.0, float(p.get("opacity_start", 1.0))))
        o1 = max(0.0, min(1.0, float(p.get("opacity_end", 0.0))))
        soft = 1.0 if str(p.get("shape", "soft_dot")) == "soft_dot" else 0.0
        color = (float(p.get("color_r", 1.0)),
                 float(p.get("color_g", 0.85)),
                 float(p.get("color_b", 0.4)))

        if n == 0:
            self._fbo_final.use()
            gl.viewport = (0, 0, w, h)
            gl.clear(0.0, 0.0, 0.0, 0.0)
            return True

        # ── Pass 1: coverage accumulation (additive) ────────────────────
        self._fbo_accum.use()
        gl.viewport = (0, 0, w, h)
        gl.clear(0.0, 0.0, 0.0, 0.0)
        gl.enable(moderngl.BLEND)
        gl.blend_func = moderngl.ONE, moderngl.ONE
        prog = self._splat_prog
        prog["u_size"].value = (float(w), float(h))
        prog["u_radius_start"].value = s0
        prog["u_radius_end"].value = s1
        prog["u_opacity_start"].value = o0
        prog["u_opacity_end"].value = o1
        prog["u_soft"].value = soft
        vao = gl.vertex_array(prog, [
            (self._corner_vbo, "2f", "in_corner"),
            (inst_vbo, "4f /i", "in_particle"),
        ])
        vao.render(moderngl.TRIANGLE_STRIP, instances=n)
        vao.release()
        gl.disable(moderngl.BLEND)

        # ── Pass 2: normalize to premultiplied RGBA ─────────────────────
        self._fbo_final.use()
        gl.viewport = (0, 0, w, h)
        nprog = self._norm_prog
        nprog["u_color"].value = color
        self._accum_texture.use(location=0)
        nprog["u_cov"].value = 0
        nvao = gl.vertex_array(nprog, [
            (self._quad_vbo, "2f 2f", "in_vert", "in_uv"),
        ])
        nvao.render(moderngl.TRIANGLE_STRIP)
        nvao.release()
        # P1-6: never leave a texture unit bound across evaluators.
        bind_texture_unit(0, 0)

        # The 2D composite pipeline composites in-shader and never relies
        # on fixed-function blend persisting across draws (scene3d/gizmo
        # enable it per-draw), so ending with BLEND off is unambiguous.
        gl.disable(moderngl.BLEND)
        return True

    def _cpu_raster_upload(self, ctx, particles: np.ndarray,
                           w: int, h: int) -> None:
        """Original CPU raster + upload path (compat fallback)."""
        import moderngl
        # Drop all GL-side splat objects first: the FBOs would otherwise
        # keep referencing the texture released below.
        self._release_gpu_objects(release=True)
        self._gpu_ctx = None
        data = self._rasterize(particles, w, h)
        data = np.ascontiguousarray(np.flipud(data))
        self._gpu_texture = ctx.texture(
            (w, h), 4, data=data.tobytes(), dtype="f4")
        self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        # Latch the playhead BEFORE unwrapping CompositeEvalContext — the
        # bare moderngl context has no frame_number, so latching after the
        # unwrap froze the particle field at frame 0 forever (TextNode
        # latches first for the same reason).
        self._update_current_frame(ctx)
        from src.features.composite.core.eval_context import CompositeEvalContext
        if isinstance(ctx, CompositeEvalContext):
            ctx = ctx.gl

        w = max(1, int(self.parameters.get("output_width", 1920)))
        h = max(1, int(self.parameters.get("output_height", 1080)))

        sig = self._content_hash()
        if sig != self._last_sig or self._gpu_texture is None:
            particles = self._simulate(int(self._current_frame), w, h)
            rendered = False
            try:
                rendered = self._gpu_render(ctx, particles, w, h)
            except Exception as exc:
                rendered = False
                if not self._gpu_fail_logged:
                    self._gpu_fail_logged = True
                    logger.warning(
                        "Particle GPU splat failed (%s); "
                        "falling back to CPU raster", exc)
            if not rendered:
                self._cpu_raster_upload(ctx, particles, w, h)
            self._last_sig = sig
            self._revision += 1

        return {
            "result": TextureResult(
                texture_id=self._gpu_texture.glo,
                width=w, height=h,
                revision=self._revision,
                format=ImageFormat(width=w, height=h, name="Particles"),
                bbox=BBox.full_frame(w, h),
                alpha_premultiplied=True,
                color_space="linear",
            ),
        }

    def cleanup(self) -> None:
        self._release_gpu_objects(release=True)
        self._gpu_ctx = None
