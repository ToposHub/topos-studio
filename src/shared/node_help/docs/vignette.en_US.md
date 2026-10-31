# Vignette

Multiplicative lens darkening (linear-domain exposure semantics): `v = 1 - amount × smoothstep(radius-softness, radius, d)`. `Roundness` interpolates the superellipse distance between circular (0) and rectangular (1). Purely per-pixel, padding 0.

- **Amount / Radius / Softness / Roundness** are all animatable.
