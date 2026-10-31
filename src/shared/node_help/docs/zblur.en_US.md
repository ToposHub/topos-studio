# ZBlur (Depth of Field)

Depth-driven defocus: the focal plane stays sharp while pixels away from it blur with a circle-of-confusion radius `r = max_radius × smoothstep(0, 1, |Z-focus|/range)`.

## Inputs

- `image`: image to defocus
- `depth`: depth map (Scene3D `depth` output, or a shuffled Z channel; red channel)

## Parameters

- **Focus Distance / Focal Range**: normalized depth (0 = nearest, 1 = farthest).
- **Max Blur Radius**: gather disc radius at full defocus (pixels); also the tile padding.
- **Aperture Disc Sampling**: off = soft Gaussian-style gather, on = aperture-disc shaped samples (harder bokeh edges).
- **Depth Units**: `normalized` / `world distance` (world linearizes via `near`/`far`).
- **Quality**: gather samples (fast 8 / normal 16 / high 32); halved automatically in preview.

## Notes

- A missing depth input produces no output (a visible broken link), never a silent passthrough.
- The variable-radius MVP is a single-pass spiral disc gather (correctness first); very large radii (>40px) halve in preview mode.
