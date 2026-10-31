# Depth Matte

Outputs a white matte for the depth window `[range start, range end]` with smoothstep `feather` boundaries, ready for any downstream mask input (Keyer, merge mask, grade mask...).

## Inputs

- `image`: reference image (defines output canvas/metadata)
- `depth`: depth map (red channel)

## Parameters

- **Range Start / Range End**: normalized depth window.
- **Feather**: boundary transition width.
- **Depth Units**: `normalized` / `world distance` (with `near`/`far` linearization).

Purely per-pixel (padding 0); output is a premultiplied white matte (RGB=A=coverage). A missing depth input produces no output.
