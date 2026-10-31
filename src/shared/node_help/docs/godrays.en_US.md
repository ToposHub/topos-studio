# God Rays (Volumetric Light Shafts)

Screen-space radial march: each pixel steps toward the light position, accumulating the occlusion (alpha) it crosses with `decay^i` falloff; output `image + rays × weight × density × light color` (linear-domain add).

## Inputs

- `image`: base image (occluder = input alpha)
- `mask` (optional): explicit occluder mask — its alpha replaces the input alpha when connected

## Parameters

- **Light X / Y**: screen-space UV (0-1, may exceed the frame), animatable and expression-capable.
- **Light Color / Density / Weight / Decay**: energy controls; decay 1.0 disables distance falloff.
- **Samples**: 16-64; halved automatically in preview (the draft downgrade path).

## Notes

- Full-frame-only post node: the radial march is global, so this node opts out of ROI clipping (always evaluated full-frame) and tile padding is 0.
