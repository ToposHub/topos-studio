# Point Cloud 3D

Point cloud rendering from PLY/xyz or tracker data (match-move reference). MVP uses the deterministic CPU orthographic-fit rasterizer; the Scene3D GL_POINTS pass is follow-up integration.

- **Source**: `file` (trimesh PLY with vertex colours / xyz text) / `from tracker` (track_data: valid frame transforms → planar points)
- **Color modes**: flat / vertex color / depth ramp; point size and opacity adjustable
- Load failures and empty data produce a visible error state with no output (never silent)
