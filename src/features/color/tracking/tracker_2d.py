"""Compatibility shim (A1 tracking migration).

The implementation moved to ``src.shared.tracking.tracker_2d`` so both the Color
page and the Composite page can use it.  This module re-exports everything
so existing ``src.features.color.tracking.tracker_2d`` imports keep working.
"""

from src.shared.tracking.tracker_2d import *  # noqa: F401,F403
from src.shared.tracking import tracker_2d as _impl  # noqa: F401

# Re-export private/underscore names too (some callers reach into helpers).
for _name in dir(_impl):
    if _name not in dir():
        globals()[_name] = getattr(_impl, _name)
