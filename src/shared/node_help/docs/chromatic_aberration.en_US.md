# Chromatic Aberration

Radial RGB channel split simulating real lens dispersion: the R and B channels shift independently along the radial direction (G stays). `Edge Boost` ramps the shift with distance from the centre (centre stays clean); at 0 the shift is constant. Tile padding follows the largest offset.

- **R Offset / B Offset** (pixels, negative = inverted): halved automatically in preview.
- **Center X / Y**: radial centre (UV).
- Combine with lens_distortion / vignette / lens_flare into a lens-simulation chain.
