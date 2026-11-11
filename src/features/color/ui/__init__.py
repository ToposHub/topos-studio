"""Color UI components for Topos Color.

This module contains all UI panels and widgets related to color grading:
- ColorWheel: Primary color grading wheel (Lift/Gamma/Gain)
- WarperPanel: Independent Color Warper palette
- MixerPanel: Independent channel mixer palette
- CurvesPanel: RGB curves + four-parameter Soft Clip sidebar
- HSLCurvesPanel: HSL curve adjustments
- LUTPanel: LUT file loading and management
- MaskPanel: Mask creation and editing
- QualifierPanel: Color qualification and selection
- RawPanel: RAW image processing controls

旧独立 SoftClipPanel 已删除（2026-11-01 UI 整合计划任务 9）：柔化裁切
四参数 UI 由 CurvesPanel 侧栏承载。
"""

from .color_wheel import ColorWheel
from .warper_panel import WarperPanel
from .mixer_panel import MixerPanel
from .curves_panel import CurvesPanel
from .hsl_curves_panel import HSLCurvesPanel
from .lut_panel import LutPanel
from .mask_panel import MaskPanel
from .qualifier_panel import QualifierPanel
from .raw_panel import RawPanel

__all__ = [
    "ColorWheel",
    "WarperPanel",
    "MixerPanel",
    "CurvesPanel",
    "HSLCurvesPanel",
    "LutPanel",
    "MaskPanel",
    "QualifierPanel",
    "RawPanel",
]
