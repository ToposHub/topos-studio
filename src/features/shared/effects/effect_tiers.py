"""T-P1-21 batch 2: shared quality-tier contract for Edit effects.

The GPU path (``effect_pass_runner`` packers/runner) and the CPU fallbacks
(``timeline_export._cpu_*``) must agree on what each quality tier means,
so the tier arithmetic lives here instead of being re-derived on both
sides. Any change to a tier's semantics lands here once and both paths
follow.
"""
from __future__ import annotations

#: Full-resolution radius at/above which the ``fast`` tier routes a
#: sampling-heavy effect (separable blur, defocus) through the
#: half-resolution path. Below this the extra down/upsample draws cost
#: more than the quadratically cheaper kernel saves.
FAST_TIER_MIN_RADIUS: float = 8.0

#: Resolution scale of the fast-tier ping-pong surfaces. 0.5 quarters the
#: fragment count; the kernel radius is scaled by the same factor so the
#: *physical* blur extent is preserved.
FAST_TIER_SCALE: float = 0.5

#: Denoise node schema caps ``radius`` at 10 (ParamDef max_val).
_DENOISE_RADIUS_MAX: int = 10


def effective_denoise_radius(radius: int, quality: str) -> int:
    """Bilateral kernel radius for a denoise quality tier.

    Contract shared by the GLSL path (``_pack_denoise_params``) and the
    CPU fallback (``timeline_export._cpu_denoise``):

    * ``fast``   — kernel capped at 2 (cheap preview-grade smoothing);
    * ``normal`` — the user radius verbatim;
    * ``high``   — the user radius doubled, clamped to the schema max.

    Both sides consume the *effective* radius, so CPU/GPU parity holds
    per tier by construction.
    """
    radius = max(1, int(radius))
    if quality == "fast":
        return min(radius, 2)
    if quality == "high":
        return min(radius * 2, _DENOISE_RADIUS_MAX)
    return radius
