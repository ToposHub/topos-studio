"""
LUT Node - Apply 3D Look-Up Table for color transformation.

Applies a 3D LUT (.cube or .3dl format) to transform colors.
Supports intensity blending and input/output range mapping.

Key features:
- Support for .cube and .3dl file formats
- Intensity control (0-1 blend between original and LUT result)
- Input/output range mapping for HDR workflows
- Mask input support
- HDR-safe operations (preserves values outside [0,1] range)

The LUT application uses trilinear interpolation for smooth color transitions.
Values outside the LUT domain are linearly extrapolated to preserve HDR data.
"""

import os
from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef


class LutNode(Node):
    """Apply 3D LUT for professional color grading."""

    node_type = "lut"
    node_title = "LUT"
    header_color = "#8e44ad"  # Purple

    # Metadata
    SEARCH_ALIASES = ["lut", "look", "colorspace", "色彩空间", "调色", "风格"]
    DESCRIPTION = "应用 3D LUT 进行色彩变换（支持 .cube/.3dl 格式）"
    TAGS = ["color", "lut", "transform"]

    PARAM_DEFS = [
        ParamDef("lut_path", "str", ""),
        ParamDef("intensity", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("input_range_min", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("input_range_max", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("output_range_min", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("output_range_max", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("mix", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("mask", "input", "image"),  # Optional mask input
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "lut_path": "",
            "intensity": 1.0,
            "input_range_min": 0.0,
            "input_range_max": 1.0,
            "output_range_min": 0.0,
            "output_range_max": 1.0,
            "mix": 1.0,
        }
        # Track LUT file for cache invalidation
        self._lut_path: str = ""
        self._lut_mtime: float = 0.0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        mask_image = inputs.get("mask")  # Optional mask input
        lut_path = self.parameters.get("lut_path", "")

        # Check if LUT path is valid and file exists
        if not lut_path or not os.path.exists(lut_path):
            # No LUT - pass through
            return {"result": image}

        # Check file extension
        ext = os.path.splitext(lut_path)[1].lower()
        if ext not in (".cube", ".3dl"):
            # Unsupported format - pass through
            return {"result": image}

        # Check if intensity is 0 - no effect
        intensity = self.parameters.get("intensity", 1.0)
        if intensity <= 0.0:
            return {"result": image}

        # Return effect marker for GPU dispatch
        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "lut3d",
            "_effect_input": image,
            "_effect_mask": mask_image,
            "_effect_params": {
                "lut_path": lut_path,
                "intensity": intensity,
                "input_range_min": self.parameters.get("input_range_min", 0.0),
                "input_range_max": self.parameters.get("input_range_max", 1.0),
                "output_range_min": self.parameters.get("output_range_min", 0.0),
                "output_range_max": self.parameters.get("output_range_max", 1.0),
                "mix": self.parameters.get("mix", 1.0),
            },
        }

    def set_parameter(self, key: str, value) -> None:
        """Override to track LUT file changes."""
        if key == "lut_path":
            # 只接受 str 或 os.PathLike 类型，其他类型转为空字符串
            if isinstance(value, os.PathLike):
                value = os.fspath(value)
            elif not isinstance(value, str):
                value = ""

            if value != self._lut_path:
                # Track LUT path change for cache invalidation
                self._lut_path = value
                if value and os.path.exists(value):
                    self._lut_mtime = os.path.getmtime(value)
                else:
                    self._lut_mtime = 0.0
        super().set_parameter(key, value)

    def get_lut_info(self) -> Dict[str, any]:
        """Get current LUT file information for UI display."""
        lut_path = self.parameters.get("lut_path", "")
        if not lut_path or not os.path.exists(lut_path):
            return {"loaded": False, "name": "", "size": 0}

        try:
            size = self._parse_lut_size(lut_path)
            name = os.path.splitext(os.path.basename(lut_path))[0]
            return {
                "loaded": True,
                "name": name,
                "size": size,
                "path": lut_path,
            }
        except Exception:
            return {"loaded": False, "name": "", "size": 0}

    def _parse_lut_size(self, path: str) -> int:
        """Parse LUT file to get cube size."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            size = 0
            for line in lines:
                line = line.split('#')[0].strip()
                if line.startswith('LUT_3D_SIZE'):
                    parts = line.split()
                    if len(parts) >= 2:
                        size = int(parts[1])
                        break
            # Fallback: estimate from data count
            if size == 0:
                count = 0
                for line in lines:
                    line = line.split('#')[0].strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) == 3:
                        try:
                            float(parts[0])
                            count += 1
                        except ValueError:
                            continue
                if count > 0:
                    size = int(round((count ** (1.0/3.0))))
            return size
        except Exception:
            return 0