"""
Geometry Math Utilities - Pure mathematical functions for geometric calculations.

This module provides zero-coupling mathematical utilities that can be safely
used anywhere in the project without introducing dependencies.

Functions:
    get_largest_inscribed_rect: Calculate the largest rectangle that can be
        inscribed within a rotated rectangle while maintaining aspect ratio.
"""

import math
from typing import Optional


def get_largest_inscribed_rect(
    w: float,
    h: float,
    angle_degrees: float,
    aspect_ratio: Optional[float] = None,
) -> tuple[float, float]:
    """
    Calculate the largest inscribed rectangle within a rotated frame while strictly 
    maintaining the target aspect ratio.
    
    This is the core algorithm for Lightroom-style adaptive rotation cropping.
    Given an image of size (w, h), a rotation angle, and an optional aspect ratio,
    returns the dimensions of the largest axis-aligned rectangle that fits entirely 
    within the rotated image bounds (no transparent edges visible) while preserving
    the specified aspect ratio.
    
    Algorithm:
        Uses projection boundary formulas:
        - Width constraint: w_c * cos(a) + h_c * sin(a) <= w
        - Height constraint: w_c * sin(a) + h_c * cos(a) <= h
        - Substitute h_c = w_c / aspect_ratio to solve for max w_c
    
    Args:
        w: Width of the original image in pixels.
        h: Height of the original image in pixels.
        angle_degrees: Rotation angle in degrees. Can be positive or negative.
            The algorithm uses absolute value and normalizes to [0, 90°).
        aspect_ratio: Target aspect ratio (width / height). If None or invalid,
            defaults to the original image's aspect ratio (w / h).
    
    Returns:
        tuple: (inscribed_width, inscribed_height) - The dimensions of the
            largest rectangle that fits within the rotated image while 
            maintaining the target aspect ratio.
            Returns (0, 0) if input dimensions are invalid.
    
    Examples:
        >>> get_largest_inscribed_rect(1920, 1080, 0)
        (1920.0, 1080.0)
        >>> get_largest_inscribed_rect(1920, 1080, 15, aspect_ratio=16/9)
        (1346.3777014096195, 757.337457042911)
        >>> get_largest_inscribed_rect(1920, 1080, 45)
        (977.5044143122833, 549.8462330506594)
    """
    if w <= 0 or h <= 0:
        return 0.0, 0.0
    
    # Normalize angle to [0, 90°) range
    angle_rad = abs(math.radians(angle_degrees)) % (math.pi / 2)
    sin_a = math.sin(angle_rad)
    cos_a = math.cos(angle_rad)
    
    # Default to original image aspect ratio if none is provided
    if aspect_ratio is None or aspect_ratio <= 0:
        aspect_ratio = w / h
    
    # No special-casing for 0° / 90° (AUD-38501): the projection formulas
    # below degenerate exactly to the correct limits:
    #   sin_a == 0  -> w_c = min(w, h * aspect_ratio)   (fits inside w x h)
    #   cos_a == 0  -> w_c = min(h, w * aspect_ratio)   (fits inside h x w)
    # The former 0°/90° branches had those two limits swapped, so e.g. a
    # 2:1 preset on a 1000x1000 image returned (2000, 1000) — a "safe"
    # crop rect twice as wide as the image itself.
    
    # Calculate the maximum width constrained by the original image's width
    # From: w_c * cos(a) + h_c * sin(a) <= w, with h_c = w_c / aspect_ratio
    # => w_c * (cos(a) + sin(a) / aspect_ratio) <= w
    w_c_width = w / (cos_a + sin_a / aspect_ratio)
    
    # Calculate the maximum width constrained by the original image's height
    # From: w_c * sin(a) + h_c * cos(a) <= h, with h_c = w_c / aspect_ratio
    # => w_c * (sin(a) + cos(a) / aspect_ratio) <= h
    w_c_height = h / (sin_a + cos_a / aspect_ratio)
    
    # The actual width must satisfy both constraints (use the smaller)
    wr = min(w_c_width, w_c_height)
    hr = wr / aspect_ratio
    
    return abs(wr), abs(hr)
