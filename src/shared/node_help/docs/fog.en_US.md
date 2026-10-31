# Fog (Depth Fog)

Exponential Beer–Lambert depth fog: `out = mix(image, fog color, 1 - exp(-density × t))`, `t = clamp((Z-start)/(end-start), 0, 1)`. No fog before `start`, saturating toward `end`.

## Inputs

- `image`: source image
- `depth`: depth map (red channel)

## Parameters

- **Fog Color / Density**: linear-domain mix; density 0 disables fog.
- **Start / End**: normalized depth window.
- **Depth Units**: `normalized` / `world distance` (with `near`/`far` linearization).

Purely per-pixel (padding 0). A missing depth input produces no output rather than a silent passthrough.
