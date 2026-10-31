# CornerPin — Four-Corner Perspective Warp

CornerPin warps an image with a perspective (homography) transform driven by four corner pairs. Typical uses: screen replacement, match-moving, and perspective correction.

## Core Concepts

The node has two sets of corners, all in normalized 0-1 coordinates (origin top-left, Y down):

- **Source corners from1-4 (gray handles)**: define *which region of the image* to take.
- **Destination corners to1-4 (gold handles)**: define *where that region maps to*.

Corner order is fixed: **1 = top-left, 2 = top-right, 3 = bottom-right, 4 = bottom-left**. Both corner sets must form a convex quad in this order; crossing edges flips or garbles the image.

## Viewport Controls

With the node selected, the viewer shows 8 corner handles:

| Action | Effect |
|---|---|
| Drag a gold corner | Move the destination corner; the image warps live |
| **Alt + drag a gray corner** | **Alignment mode: the source corner AND its matching destination corner move together, so the image never warps** (pin the corners onto an object in the frame, e.g. a TV screen) |
| Alt + Shift + drag a gray corner | Move the source corner only (redefine the source region) |
| Shift + drag a gold corner | Freeze mode: the image stays put, only the corner outline follows the pointer; applied on release |

## Common Workflows

### 1. Pin the corners onto an object in the frame (e.g. a TV screen)

1. Select the CornerPin node (the image starts unwarped);
2. **Alt + drag** each gray corner onto one corner of the TV screen — the image never deforms during alignment;
3. Once aligned, drag the gold corners to reposition/re-perspective that region.

### 2. Use only a region from the middle of the image

Shrink the source corners onto the region you want. For the central 50%:

- from1 (0.25, 0.25), from2 (0.75, 0.25), from3 (0.75, 0.75), from4 (0.25, 0.75)
- Keep the to corners at the full frame defaults → the region scales up to fill the frame.

You can also type `from1_x` … `from4_y` directly in the properties panel (normalized value = pixel position ÷ image size).

### 3. Perspective correction

Fit the source corners to a slanted plane in the footage (e.g. a screen) and set the destination corners to a clean rectangle to "rectify" that plane.

## Notes

- CornerPin warps within a fixed canvas (canvas size never changes); content warped outside the canvas is clipped to transparent.
- Content outside the source quad is not hard-cut; it extends along the projection geometry beyond the destination quad. To show only the sampled region, add a Mask or Crop node after CornerPin.
- The `interpolation` parameter controls sampling quality: bilinear (fast) / bicubic (smoother).
- All corner parameters support keyframe animation.
