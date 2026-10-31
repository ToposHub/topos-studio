"""
Color Facade - Interface for accessing color grading output.

Phase 6: Modularization - Provides a clean interface for other modules
(Edit, Composite) to access color grading results without directly
accessing MainWindow.engine or ImageEngine internals.

This facade follows the principle of "wrapping without touching" - 
it provides access to color output but does NOT modify the core
rendering pipeline (ImageEngine, ShaderPipeline, AsyncRenderer).
"""

from typing import Optional, Tuple, Any
import logging

logger = logging.getLogger(__name__)

# AUD-24400: Color 页 TrackingMode（src/shared/tracking/types.py:25-32）→
# Composite tracker 词表（TRACKER_MODES = translation/trs/planar，
# tracker_node.py:42）。Composite 的 "Bake to Transform" 以 `mode == "trs"`
# 门控 rotation/scale 通道；CornerPin bake 消费逐帧 matrix（不受 mode 门控）。
# - affine → "trs"：Transform bake 保留 rotation/scale（剪切分量无对应
#   通道，超出 trs 表达能力的部分由 CornerPin 的 matrix 路径覆盖）；
# - homography → "planar"：透视对应 Composite 的平面模式，逐帧 3x3 matrix；
# - camera_3d → "planar"：阶段 9 未开放，防御性归入 planar（matrix 可用）。
_COMPOSITE_TRACKING_MODE_MAP = {
    "translation": "translation",
    "trs": "trs",
    "affine": "trs",
    "homography": "planar",
    "camera_3d": "planar",
}


class ColorFacade:
    """
    Facade for Color grading output access.
    
    Provides a clean API for modules to:
    - Get current rendered texture
    - Request rendering
    - Query color output metadata
    
    This facade wraps MainWindow.engine and canvas, providing
    controlled access without exposing internal state.
    
    Attributes:
        _main_window: MainWindow reference (passed during creation)
    """
    
    def __init__(self, main_window: Any = None):
        """
        Initialize ColorFacade.
        
        Args:
            main_window: MainWindow instance (optional, set later)
        """
        self._main_window = main_window
    
    def set_main_window(self, main_window: Any) -> None:
        """
        Set MainWindow reference.
        
        Args:
            main_window: MainWindow instance
        """
        self._main_window = main_window
    
    def get_current_texture_id(self) -> Optional[int]:
        """
        Get current color output texture ID.
        
        Returns the OpenGL texture ID of the current rendered output.
        This is used by Composite to get Color output as input.
        
        Returns:
            Texture ID if available, None if no output or engine unavailable
        """
        if self._main_window is None:
            return None
        
        engine = getattr(self._main_window, 'engine', None)
        if engine is None:
            return None
        
        color_output = getattr(engine, 'color_output', None)
        if color_output is None or not color_output.is_valid:
            return None
        
        return getattr(color_output, 'texture_id', None)
    
    def get_current_output_size(self) -> Optional[Tuple[int, int]]:
        """
        Get current color output size.
        
        Returns:
            (width, height) tuple if available, None otherwise
        """
        if self._main_window is None:
            return None
        
        engine = getattr(self._main_window, 'engine', None)
        if engine is None:
            return None
        
        img = getattr(engine, 'img_full_original', None)
        if img is None:
            return None
        
        h, w = img.shape[:2]
        return (w, h)
    
    def get_current_revision(self) -> int:
        """
        Get current color output revision number.
        
        Revision increases with each render, used to detect changes.
        
        Returns:
            Revision number (0 if unavailable)
        """
        if self._main_window is None:
            return 0
        
        engine = getattr(self._main_window, 'engine', None)
        if engine is None:
            return 0
        
        color_output = getattr(engine, 'color_output', None)
        if color_output is None:
            return 0
        
        return getattr(color_output, 'revision', 0)
    
    def request_render(self, reason: str = "external_request") -> None:
        """
        Request color output rendering.
        
        Triggers a render pipeline execution for the current state.
        
        Args:
            reason: Reason for render request (for logging/debugging)
        """
        if self._main_window is None:
            logger.warning("Cannot request render: MainWindow not set")
            return
        
        engine = getattr(self._main_window, 'engine', None)
        if engine is None:
            logger.warning("Cannot request render: Engine unavailable")
            return
        
        if hasattr(engine, 'request_color_output'):
            engine.request_color_output()
            logger.debug(f"Color render requested: {reason}")
        elif hasattr(engine, 'render_pipeline'):
            engine.render_pipeline(render_reason=reason)
            logger.debug(f"Color render triggered: {reason}")
    
    def is_output_valid(self) -> bool:
        """
        Check if current color output is valid.
        
        Returns:
            True if output is valid and ready for use
        """
        if self._main_window is None:
            return False
        
        engine = getattr(self._main_window, 'engine', None)
        if engine is None:
            return False
        
        color_output = getattr(engine, 'color_output', None)
        if color_output is None:
            return False
        
        return color_output.is_valid
    
    def get_engine(self) -> Optional[Any]:
        """
        Get ImageEngine reference (for advanced use).
        
        Warning: Direct engine access should be limited to cases
        where facade methods are insufficient.
        
        Returns:
            ImageEngine instance if available
        """
        if self._main_window is None:
            return None
        return getattr(self._main_window, 'engine', None)
    
    def export_tracking_bindings(self) -> list:
        """A1: read-only export of Color-page tracking results.

        Returns a list of tracker-compatible payload dicts (see
        ``src.features.composite.nodes.tracker_node``): one per binding,
        each carrying the mode, reference frame and per-frame transforms
        so a Composite tracker node can import them.  Media geometry is
        not tracked per binding — the composite side keeps its own
        ``width``/``height`` when present.

        Empty list when the Color page has no store (page not created).
        """
        engine = self.get_engine()
        store = getattr(engine, "tracking_store", None)
        if store is None:
            return []
        payloads = []
        for binding in store.all_bindings():
            transforms = getattr(binding, "transforms", None) or {}
            raw_mode = getattr(getattr(binding, "mode", None), "value",
                               "translation")
            # AUD-24400: Color 侧 TrackingMode 词表（translation/trs/affine/
            # homography/camera_3d）必须映射到 Composite tracker 词表
            # （translation/trs/planar）。此前 "affine"/"homography" 原样写入
            # track_data：既超词表，又不满足 bake 的 `mode == "trs"` 门控，
            # rotation/scale 通道被静默丢弃。
            mode = _COMPOSITE_TRACKING_MODE_MAP.get(str(raw_mode))
            if mode is None:
                logger.warning(
                    "export_tracking_bindings: unmapped tracking mode %r, "
                    "falling back to 'translation'", raw_mode)
                mode = "translation"
            corrections = getattr(binding, "manual_corrections", None) or {}
            merged: dict = {}
            for frame, tr in transforms.items():
                merged[int(frame)] = tr
            for frame, tr in corrections.items():  # manual wins (store semantics)
                merged[int(frame)] = tr
            entry = {
                "binding_id": getattr(binding, "binding_id", ""),
                "shape_name": getattr(binding, "shape_name", ""),
                "clip_id": getattr(binding, "clip_id", ""),
                "reference_frame": int(getattr(binding, "reference_frame", 0) or 0),
                "mode": mode,
                "transforms": {},
            }
            # AUD-24401: 此前此处残留 `from src.shared.tracking.types import
            # TrackStatus`——死代码（status 经 getattr 取值，TrackStatus 从未
            # 使用），且位于逐 binding 迭代内重复执行 import 查找，已删除。
            for frame, tr in sorted(merged.items()):
                status = getattr(getattr(tr, "status", None), "value", "valid")
                translation = getattr(tr, "translation", (0.0, 0.0))
                scale = getattr(tr, "scale", (1.0, 1.0))
                frame_entry = {
                    "tx": float(translation[0]),
                    "ty": float(translation[1]),
                    "rotation": float(getattr(tr, "rotation", 0.0)),
                    "scale_x": float(scale[0]),
                    "scale_y": float(scale[1]),
                    "status": status,
                }
                # The homography is required by the Composite page's
                # "Bake to CornerPin" — without it planar imports are
                # silently unusable downstream.
                matrix = getattr(tr, "matrix", None)
                if matrix is not None:
                    if len(matrix) == 9:
                        frame_entry["matrix"] = [
                            [float(matrix[0]), float(matrix[1]), float(matrix[2])],
                            [float(matrix[3]), float(matrix[4]), float(matrix[5])],
                            [float(matrix[6]), float(matrix[7]), float(matrix[8])],
                        ]
                    else:
                        try:
                            frame_entry["matrix"] = [
                                [float(v) for v in row] for row in matrix]
                        except TypeError as e:
                            logger.warning(
                                f"Malformed transform matrix for frame {frame}; "
                                f"homography omitted: {e}")
                entry["transforms"][str(frame)] = frame_entry
            payloads.append(entry)
        return payloads

    def get_canvas(self) -> Optional[Any]:
        """
        Get Canvas reference (for viewport interaction).
        
        Returns:
            Canvas instance if available
        """
        if self._main_window is None:
            return None
        return getattr(self._main_window, 'canvas', None)