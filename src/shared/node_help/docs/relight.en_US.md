# Relight (AOV Re-lighting)

Relight re-lights a 2D image using the **NORMAL** and **POSITION** AOVs from the same render — move the light in comp instead of going back to the 3D package.

## Inputs

| Port | Description |
|---|---|
| image | beauty / albedo image |
| normal | world-space normal (encoded to [0,1] by this project's AOVs, required) |
| position | world-space position (HDR float, recommended for point lights) |

Connect Scene3D's `normal` / `position` outputs directly, or feed channels read back from a multi-layer EXR via Shuffle.

## Parameters

- **Light Type**: `point` (uses the angle between light_position and the per-pixel POSITION) / `directional` (direction = negated light_direction).
- **Light Position / Direction**: world units; all animatable and expression-capable.
- **Intensity / Light Color / Ambient**: output = image × (max(dot(N,L),0) × intensity × color + ambient), computed in the linear domain.
- **Normal Decode**: `encoded` (this project's [0,1] AOV encode, default) / `signed` (raw -1~1 float normals from external DCCs).
- **Flip Normal Y**: enable when an exported Blender normal map appears upside down.

## Notes

- The MVP implements Lambert diffuse only (specular highlights are a later release); in point mode without a position input the light position parameter is treated as a direction from the world origin.
- Purely per-pixel with no spatial sampling — negligible interactive cost.
