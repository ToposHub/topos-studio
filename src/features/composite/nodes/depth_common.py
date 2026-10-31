"""Depth tool nodes (A3): ZBlur / Fog / DepthMatte — shared depth contract.

All three nodes take a ``depth`` input (Scene3D DEPTH AOV or a shuffled Z
channel, depth in the red channel) plus the ``image``.  ``depth_units``
selects between already-normalized depth and world distance; world mode
linearizes with ``near``/``far``:

    z01 = clamp((Z - near) / max(far - near, eps), 0, 1)

Without a connected depth input the nodes emit no output — a visible
"missing input" blank downstream, never a silent unprocessed passthrough.
"""

from typing import Any, Dict

from .base_node import TextureResult


DEPTH_UNITS = ["normalized", "world"]

# Shared GLSL prelude for the depth tool shaders (declares u_depth and the
# depth01() helper; u_texture/u_depth bind to units 0/1).
DEPTH_INPUT_GLSL = """
uniform sampler2D u_texture;
uniform sampler2D u_depth;
uniform int u_depth_units;   // 0 = normalized, 1 = world
uniform float u_near;
uniform float u_far;

float depth01() {
    float z = texture(u_depth, v_uv).r;
    if (u_depth_units == 1) {
        return clamp((z - u_near) / max(u_far - u_near, 0.00001),
                     0.0, 1.0);
    }
    return clamp(z, 0.0, 1.0);
}
"""

# Python mirror of DEPTH_INPUT_GLSL for analytic tests / CPU paths.
def depth_to_normalized(z: float, depth_units: str = "normalized",
                        near: float = 0.0, far: float = 100.0) -> float:
    if depth_units == "world":
        span = max(float(far) - float(near), 1e-5)
        return min(1.0, max(0.0, (float(z) - float(near)) / span))
    return min(1.0, max(0.0, float(z)))


def depth_params(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the shared depth-conversion params from a node."""
    depth_units = parameters.get("depth_units", "normalized")
    if depth_units not in DEPTH_UNITS:
        depth_units = "normalized"
    return {
        "depth_units": depth_units,
        "near": float(parameters.get("near", 0.0)),
        "far": float(parameters.get("far", 100.0)),
    }


def depth_marker_inputs(inputs: Dict[str, TextureResult],
                        ) -> Dict[str, TextureResult]:
    """Validate image+depth inputs; returns the pair or an empty dict."""
    image = inputs.get("image")
    depth = inputs.get("depth")
    if image is None or not image.is_valid:
        return {}
    if depth is None or not depth.is_valid:
        return {}
    return {"image": image, "depth": depth}
