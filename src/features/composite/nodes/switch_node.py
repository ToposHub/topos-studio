"""
Switch Node - Selects one input from multiple sources.

Commonly used for:
  - Debugging and version switching
  - Conditional rendering
  - A/B comparisons
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class SwitchNode(Node):
    """Switches between multiple inputs based on 'which' parameter.

    The ``which`` index selects one of the dynamic ``inputN`` ports to route
    to the single output. ``input_count`` controls how many input ports are
    exposed (2..16). When disabled, the currently-selected input is passed
    through unchanged.
    """

    node_type = "switch"
    # CP-044: 动态路由节点不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "Switch"
    header_color = "#5a7e9e"  # Steel blue

    # Metadata
    SEARCH_ALIASES = ["switch", "select", "choose", "切换", "选择", "version"]
    DESCRIPTION = "在多个输入之间切换选择"
    TAGS = ["merge", "utility"]

    # Bypass: dynamic mapping resolved in _bypass_evaluate; BYPASS_MAPPING is
    # intentionally None so that the per-instance selected input is used.
    BYPASS_MAPPING = None

    PARAM_DEFS = [
        ParamDef("which", "int", 0, min_val=0, max_val=15),
        ParamDef("input_count", "int", 2, min_val=2, max_val=16),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self._update_ports()

    def _update_ports(self) -> None:
        """Update input ports based on input_count."""
        # Ensure integer count for range() - handles float input from boundary tests
        input_count = int(self.parameters.get("input_count", 2))
        self.inputs = [Port(f"input{i}", "input", "image") for i in range(input_count)]
        self.outputs = [Port("result", "output", "image")]

    def set_parameter(self, key: str, value: Any) -> None:
        """Handle parameter changes, rebuild ports if input_count changes.

        Uses local old-value comparison instead of relying on base class return value,
        because Node.set_parameter() returns None (implicit).
        """
        old_value = self.parameters.get(key)
        super().set_parameter(key, value)
        if key == "input_count" and old_value != value:
            self._update_ports()

    def get_parameter(self, key: str, default: Any = None) -> Any:
        """Get parameter value with fallback."""
        return self.parameters.get(key, default)

    @staticmethod
    def _normalize_which(params: Dict[str, Any]) -> int:
        """Normalize the ``which`` parameter to a clamped port index.

        AUD-7404 fix: the evaluation pipeline resolves animated/expressed
        int parameters to floats (keyframe interpolation returns ``1.0``,
        not ``1``), so every consumer must convert through ``float`` before
        ``int`` and clamp to the port range. Matches the ``int()`` handling
        in :meth:`compute_input_roi` and the clamp in :meth:`_do_evaluate`.
        """
        try:
            which = int(float(params.get("which", 0)))
        except (TypeError, ValueError, OverflowError):
            return 0
        try:
            input_count = int(float(params.get("input_count", 2)))
        except (TypeError, ValueError, OverflowError):
            input_count = 2
        input_count = max(2, input_count)
        return max(0, min(which, input_count - 1))

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox is from the selected input."""
        which = self._normalize_which(params)
        key = f"input{which}"

        bbox = input_bboxes.get(key)
        if bbox is None or bbox.is_empty:
            # Find first valid bbox
            for k, v in input_bboxes.items():
                if v and not v.is_empty:
                    return v
            return None
        return bbox

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Only the selected input needs the ROI.

        Returns the ROI for the selected port plus (defensively) any other
        connected input, since the UI may flip ``which`` between frames and
        upstream ROI re-evaluation is async.
        """
        result: Dict[str, "BBox"] = {}
        which = int(params.get("which", 0))
        result[f"input{which}"] = output_roi
        # Provide ROI for other connected inputs too so they are ready when
        # the user switches inputs without waiting for re-evaluation.
        if input_bboxes:
            for port_name in input_bboxes:
                if port_name not in result:
                    result[port_name] = output_roi
        return result

    def _bypass_evaluate(self, inputs: Dict[str, TextureResult]) -> Dict[str, TextureResult]:
        """When disabled, pass through the currently-selected input.

        Overrides the base Node._bypass_evaluate because Switch has a dynamic
        port layout: the generic ``first input -> first output`` rule would
        always expose input0, ignoring ``which``.
        """
        which = self._get_param_int("which", 0)
        input_count = self._get_param_int("input_count", 2)
        which = max(0, min(which, input_count - 1))

        selected = inputs.get(f"input{which}")
        if selected and selected.is_valid:
            return {"result": selected}
        # Fall back to any valid input (preserves prior behavior).
        for i in range(input_count):
            inp = inputs.get(f"input{i}")
            if inp and inp.is_valid:
                return {"result": inp}
        return {}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        which = self._get_param_int("which", 0)
        input_count = self._get_param_int("input_count", 2)

        # Clamp which to valid range
        which = max(0, min(which, input_count - 1))
        key = f"input{which}"

        selected = inputs.get(key)
        if selected and selected.is_valid:
            return {"result": selected}

        # If selected input is invalid, find first valid
        for i in range(input_count):
            inp = inputs.get(f"input{i}")
            if inp and inp.is_valid:
                return {"result": inp}

        return {}
