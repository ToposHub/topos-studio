"""
Noise Node - Generates procedural noise textures.

Creates Perlin-like noise patterns with configurable scale, octaves,
and animation support for dynamic effects.

Supported noise types:
  - perlin: Classic gradient noise, smooth and natural
  - simplex: Improved gradient noise, fewer artifacts
  - fractal: Multi-layer noise (fBm), detailed and organic
  - turbulence: Absolute fractal noise, turbulent patterns
  - wave: Wave-like patterns using sine functions

Uses GPU-accelerated shader for real-time generation.
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef


# Noise types
NOISE_TYPES = ["perlin", "simplex", "fractal", "turbulence", "wave"]

# Output channels
OUTPUT_CHANNELS = ["1", "3", "4"]


class NoiseNode(Node):
    """Generates procedural noise textures using GPU shader."""

    node_type = "noise"
    node_title = "Noise"
    header_color = "#6a8a5a"  # Green-ish

    # Metadata
    SEARCH_ALIASES = ["noise", "噪声", "perlin", "simplex", "fractal", "turbulence", "纹理", "texture"]
    DESCRIPTION = "生成程序化噪声纹理（Perlin/Simplex/分形噪声）"
    TAGS = ["generator", "texture", "effect"]

    PARAM_DEFS = [
        ParamDef("noise_type", "option", "perlin", labels=NOISE_TYPES),
        ParamDef("scale", "float", 100.0, min_val=1.0, max_val=1000.0, step=1.0),
        ParamDef("octaves", "int", 4, min_val=1, max_val=8, step=1),
        ParamDef("persistence", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("lacunarity", "float", 2.0, min_val=1.0, max_val=4.0, step=0.1),
        ParamDef("seed", "int", 0, min_val=0, max_val=9999, step=1),
        ParamDef("output_channels", "option", "4", labels=OUTPUT_CHANNELS),
        ParamDef("animate", "bool", False),
        ParamDef("animation_speed", "float", 1.0, min_val=0.0, max_val=10.0, step=0.1),
        ParamDef("width", "int", 1920, min_val=1, max_val=16384, step=1),
        ParamDef("height", "int", 1080, min_val=1, max_val=16384, step=1),
        ParamDef("offset_x", "float", 0.0, min_val=-10000.0, max_val=10000.0, step=1.0),
        ParamDef("offset_y", "float", 0.0, min_val=-10000.0, max_val=10000.0, step=1.0),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "noise_type": "perlin",
            "scale": 100.0,
            "octaves": 4,
            "persistence": 0.5,
            "lacunarity": 2.0,
            "seed": 0,
            "output_channels": "4",
            "animate": False,
            "animation_speed": 1.0,
            "width": 1920,
            "height": 1080,
            "offset_x": 0.0,
            "offset_y": 0.0,
        }

    @classmethod
    def params_require_frame_cache(cls, params) -> bool:
        """AUD-7101: animated noise is a pure function of the playhead.

        When ``animate`` is enabled the shader's ``u_time`` advances with the
        frame, so the evaluator's cache key must include the frame number or
        playback keeps reusing the first frame's cached texture.
        """
        try:
            return bool(params.get("animate", False))
        except (TypeError, ValueError):
            return False

    def _resolve_time(self, ctx) -> float:
        """Resolve the animation clock from the evaluation context.

        Source nodes receive a CompositeEvalContext carrying
        ``frame_number``; a bare ModernGL context carries nothing (the
        effect-marker evaluation path), in which case time stays 0.0 —
        identical to the shader's uniform default.
        """
        frame = getattr(ctx, "frame_number", None)
        if frame is None:
            return 0.0
        try:
            return float(frame)
        except (TypeError, ValueError):
            return 0.0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Generate noise texture using GPU shader.

        Returns a marker dict for the renderer to execute the shader.
        """
        # Get parameters
        noise_type = self.parameters.get("noise_type", "perlin")
        scale = max(1.0, float(self.parameters.get("scale", 100.0)))
        octaves = max(1, min(8, int(self.parameters.get("octaves", 4))))
        persistence = float(self.parameters.get("persistence", 0.5))
        lacunarity = float(self.parameters.get("lacunarity", 2.0))
        seed = int(self.parameters.get("seed", 0))
        output_channels = self.parameters.get("output_channels", "4")
        animate = self.parameters.get("animate", False)
        animation_speed = float(self.parameters.get("animation_speed", 1.0))
        width = max(1, int(self.parameters.get("width", 1920)))
        height = max(1, int(self.parameters.get("height", 1080)))
        offset_x = float(self.parameters.get("offset_x", 0.0))
        offset_y = float(self.parameters.get("offset_y", 0.0))
        # AUD-7101: feed the playhead into the marker — the dispatcher
        # (filter_evaluator._apply_noise) forwards ``time`` to ``u_time``,
        # which the shader advances by ``u_time * u_animation_speed`` when
        # ``u_animate`` is on.
        time_value = self._resolve_time(ctx)

        # Preview mode: reduce quality for faster response
        if render_mode == "preview":
            octaves = max(1, octaves - 2)

        # Return effect marker for GPU renderer
        return {
            "result": TextureResult(
                texture_id=-1,
                width=width,
                height=height,
                alpha_premultiplied=False,
                color_space="linear",
            ),
            "_effect_type": "noise",
            "_effect_params": {
                "noise_type": noise_type,
                "scale": scale,
                "octaves": octaves,
                "persistence": persistence,
                "lacunarity": lacunarity,
                "seed": seed,
                "output_channels": output_channels,
                "animate": animate,
                "animation_speed": animation_speed,
                "time": time_value,
                "width": width,
                "height": height,
                "offset_x": offset_x,
                "offset_y": offset_y,
            },
        }
