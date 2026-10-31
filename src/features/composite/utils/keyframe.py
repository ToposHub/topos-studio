"""
Keyframe Animation System for Topos Color.

⚠️ DEPRECATED: This module has been moved to src/shared/keyframe.py
This file is kept for backward compatibility and will be removed in a future version.
Please use: from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType
"""

from src.shared.keyframe import (
    Keyframe,
    KeyframeTrack,
    ColorKeyframeTrack,
    VectorKeyframeTrack,
    DiscreteKeyframeTrack,
    InterpolationType,
    interpolate,
    bezier_interpolate,
    ease_in_interpolate,
    ease_out_interpolate,
    ease_in_out_interpolate,
    create_simple_track,
    validate_track,
    keyframe_track_class,
    make_keyframe_track,
    keyframe_track_from_dict,
)

__all__ = [
    "Keyframe",
    "KeyframeTrack",
    "ColorKeyframeTrack",
    "VectorKeyframeTrack",
    "DiscreteKeyframeTrack",
    "InterpolationType",
    "interpolate",
    "bezier_interpolate",
    "ease_in_interpolate",
    "ease_out_interpolate",
    "ease_in_out_interpolate",
    "create_simple_track",
    "validate_track",
    "keyframe_track_class",
    "make_keyframe_track",
    "keyframe_track_from_dict",
]
